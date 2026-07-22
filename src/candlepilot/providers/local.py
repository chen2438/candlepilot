from __future__ import annotations

import time
from datetime import timedelta
from decimal import Decimal
from typing import Literal

from candlepilot.domain.models import (
    MarketSnapshot,
    OrderType,
    PortfolioState,
    ProviderHealth,
    TradeAction,
    TradeIntent,
)
from candlepilot.providers.base import DecisionProvider, ProviderCapabilities, ProviderResult
from candlepilot.provenance import MARKET_SNAPSHOT_SCHEMA_VERSION, content_fingerprint


LOCAL_RULE_PROVIDER = "local-rule"
LOCAL_RULE_MODEL = "trend-v2"
LOCAL_RULE_VERSION = "local-trend-v2"
LOCAL_STRUCTURE_PROVIDER = "local-structure-shadow"
LOCAL_FLOW_PROVIDER = "local-flow-shadow"
LOCAL_STRUCTURE_FLOW_PROVIDER = "local-structure-flow-shadow"
LocalRuleVariant = Literal["standard", "structure", "flow", "structure-flow"]
_VARIANT_METADATA = {
    "standard": (LOCAL_RULE_PROVIDER, LOCAL_RULE_MODEL, LOCAL_RULE_VERSION),
    "structure": (
        LOCAL_STRUCTURE_PROVIDER,
        "trend-structure-v3",
        "local-trend-structure-v3",
    ),
    "flow": (LOCAL_FLOW_PROVIDER, "trend-flow-v3", "local-trend-flow-v3"),
    "structure-flow": (
        LOCAL_STRUCTURE_FLOW_PROVIDER,
        "trend-structure-flow-v3",
        "local-trend-structure-flow-v3",
    ),
}
_INTERVAL_WEIGHTS = {
    "5m": 0.10,
    "15m": 0.15,
    "30m": 0.20,
    "1h": 0.25,
    "4h": 0.30,
}
_ENTRY_SCORE = 0.45
_MIN_VOLUME_RATIO = 0.80
_MAX_EXTENSION_ATR = 2.50
_STOP_ATR = Decimal("1.5")
_TARGET_R = Decimal("1.5")
_RISK_FRACTION = Decimal("0.005")
_STRUCTURE_BUFFER_ATR = Decimal("0.25")
_FLOW_TAKER_LONG = 1.05
_FLOW_TAKER_SHORT = 0.95


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


class LocalRuleProvider(DecisionProvider):
    """Deterministic trend decisions from the snapshot CandlePilot already builds."""

    name = LOCAL_RULE_PROVIDER
    model = LOCAL_RULE_MODEL
    reasoning_effort = None
    reasoning_effort_options: tuple[str, ...] = ()
    timeout = 1.0

    def __init__(self, variant: LocalRuleVariant = "standard") -> None:
        if variant not in _VARIANT_METADATA:
            raise ValueError(f"unknown local rule variant: {variant}")
        self.variant = variant
        self.name, self.model, self._version = _VARIANT_METADATA[variant]

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            subscription_auth=False,
            cancellable=False,
            external_inference=False,
            configurable_model=False,
            requires_backtest_probe=False,
            retryable=False,
            # Include the local snapshot/risk/audit overhead in the preflight
            # baseline. A sub-millisecond pure function measured in isolation
            # would round a day-long run down to "0 minutes" in the frontend.
            estimated_seconds_per_decision=0.01,
            live_shadow_only=self.variant != "standard",
        )

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name,
            available=True,
            authenticated=True,
            version=self._version,
            detail=(
                "deterministic local strategy; no external model call"
                if self.variant == "standard"
                else "deterministic experimental strategy; live decisions are shadow-only"
            ),
        )

    async def generate_trade_intent(
        self,
        snapshot: MarketSnapshot,
        portfolio: PortfolioState,
    ) -> ProviderResult:
        started = time.perf_counter()
        payload = {
            "market": snapshot.model_dump(mode="json"),
            "portfolio": portfolio.model_dump(mode="json"),
        }
        intent = self._decide(snapshot, portfolio)
        duration = timedelta(seconds=time.perf_counter() - started)
        raw_output = intent.model_dump_json()
        return ProviderResult(
            intent=intent,
            provider=self.name,
            model=self.model,
            duration=duration,
            raw_output=raw_output,
            usage={
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cost_usd": 0.0,
                "local_decision": True,
                "strategy_variant": self.variant,
                "live_shadow_only": self.variant != "standard",
            },
            prompt_version=None,
            data_version=content_fingerprint(
                payload, schema_version=MARKET_SNAPSHOT_SCHEMA_VERSION
            ),
            provider_version=self._version,
            input_payload=payload,
            prompt=None,
            reasoning_effort=None,
        )

    def _decide(
        self,
        snapshot: MarketSnapshot,
        portfolio: PortfolioState,
    ) -> TradeIntent:
        features = snapshot.features
        required = {
            *(f"{interval}_ema_spread" for interval in _INTERVAL_WEIGHTS),
            "5m_return_1",
            "5m_return_5",
            "5m_quote_volume_ratio",
            "5m_ema20_distance_atr",
            "5m_ema_20",
            f"{snapshot.cadence}_atr_14",
        }
        missing = sorted(required - features.keys())
        if missing:
            return TradeIntent.hold(
                snapshot.symbol,
                snapshot.cadence,
                f"local trend strategy: missing required features: {', '.join(missing)}",
            )
        if snapshot.symbol in portfolio.pending_entry_symbols:
            return TradeIntent.hold(
                snapshot.symbol,
                snapshot.cadence,
                "local trend strategy: entry order already pending",
            )

        signs = {
            interval: _sign(features[f"{interval}_ema_spread"])
            for interval in _INTERVAL_WEIGHTS
        }
        score = sum(_INTERVAL_WEIGHTS[interval] * signs[interval] for interval in signs)
        direction = 1 if score >= _ENTRY_SCORE else -1 if score <= -_ENTRY_SCORE else 0
        reasons: list[str] = []
        if direction == 0:
            reasons.append(f"weighted trend score {score:+.2f} is inside entry threshold")
        elif signs["5m"] != direction:
            reasons.append("5m trigger does not align")
        elif signs["15m"] != direction and signs["30m"] != direction:
            reasons.append("neither 15m nor 30m confirms")
        elif signs["1h"] == -direction and signs["4h"] == -direction:
            reasons.append("1h and 4h both oppose")

        momentum = (
            _sign(features["5m_return_1"]),
            _sign(features["5m_return_5"]),
        )
        if direction and momentum != (direction, direction):
            reasons.append("5m return_1 and return_5 must both align")
        volume_ratio = features["5m_quote_volume_ratio"]
        if direction and volume_ratio < _MIN_VOLUME_RATIO:
            reasons.append(f"5m quote-volume ratio {volume_ratio:.2f} is below participation floor")
        extension = features["5m_ema20_distance_atr"]
        if direction and direction * extension >= _MAX_EXTENSION_ATR:
            reasons.append(f"5m price is extended {extension:+.2f} ATR from EMA20")

        position = portfolio.positions.get(snapshot.symbol)
        if position is not None:
            position_direction = 1 if position.side == "LONG" else -1
            if direction == -position_direction and not reasons:
                return TradeIntent(
                    symbol=snapshot.symbol,
                    cadence=snapshot.cadence,
                    action=TradeAction.CLOSE,
                    confidence=self._confidence(score, volume_ratio),
                    leverage=1,
                    risk_fraction=Decimal("0"),
                    order_type=OrderType.MARKET,
                    rationale=(
                        f"local trend strategy: close {position.side.lower()}; "
                        f"confirmed opposing score {score:+.2f}"
                    ),
                )
            return TradeIntent.hold(
                snapshot.symbol,
                snapshot.cadence,
                f"local trend strategy: keep existing {position.side.lower()}; score {score:+.2f}",
            )

        if direction == 0 or reasons:
            reason = "; ".join(reasons) or "no executable trend setup"
            return TradeIntent.hold(
                snapshot.symbol,
                snapshot.cadence,
                f"local trend strategy: {reason}",
            )

        flow_state: str | None = None
        flow_detail: str | None = None
        if self.variant in {"flow", "structure-flow"}:
            flow_state, flow_detail, flow_rejection = self._flow_assessment(
                features, direction
            )
            if flow_rejection is not None:
                return TradeIntent.hold(
                    snapshot.symbol,
                    snapshot.cadence,
                    f"local {self.variant} experiment: {flow_rejection}",
                )

        structure: dict[str, object] | None = None
        if self.variant in {"structure", "structure-flow"}:
            structure, structure_rejection = self._structure_plan(
                snapshot, direction
            )
            if structure is None:
                return TradeIntent.hold(
                    snapshot.symbol,
                    snapshot.cadence,
                    f"local {self.variant} experiment: {structure_rejection}",
                )

        atr = Decimal(str(features[f"{snapshot.cadence}_atr_14"]))
        if atr <= 0:
            return TradeIntent.hold(
                snapshot.symbol,
                snapshot.cadence,
                "local trend strategy: decision-cadence ATR is not positive",
            )
        entry = snapshot.mark_price
        if structure is None:
            stop_distance = atr * _STOP_ATR
            target_distance = stop_distance * _TARGET_R
            stop = entry - stop_distance if direction > 0 else entry + stop_distance
            target = entry + target_distance if direction > 0 else entry - target_distance
            setup_type = "TREND_CONTINUATION"
            anchor_price = entry
            trigger_type = "MARKET_CONFIRMED"
            invalidation_type = "EMA"
            invalidation_level = Decimal(str(features["5m_ema_20"]))
            target_type = "R_MULTIPLE"
        else:
            stop = structure["stop"]
            target = structure["target"]
            setup_type = structure["setup_type"]
            anchor_price = structure["anchor_price"]
            trigger_type = structure["trigger_type"]
            invalidation_type = structure["invalidation_type"]
            invalidation_level = structure["invalidation_level"]
            target_type = structure["target_type"]
            stop_distance = abs(entry - stop)  # type: ignore[operator]
        if stop <= 0 or target <= 0:
            return TradeIntent.hold(
                snapshot.symbol,
                snapshot.cadence,
                "local trend strategy: ATR protection would produce a non-positive price",
            )
        action = TradeAction.OPEN_LONG if direction > 0 else TradeAction.OPEN_SHORT
        return TradeIntent(
            symbol=snapshot.symbol,
            cadence=snapshot.cadence,
            action=action,
            confidence=self._confidence(score, volume_ratio),
            leverage=3,
            risk_fraction=_RISK_FRACTION,
            order_type=OrderType.MARKET,
            stop_loss=stop,
            take_profit=target,
            decision_framework="structure-v1",
            setup_type=setup_type,
            anchor_timeframe="5m",
            anchor_price=anchor_price,
            trigger_type=trigger_type,
            trigger_price=entry,
            invalidation_type=invalidation_type,
            invalidation_level=invalidation_level,
            target_type=target_type,
            rationale=(
                f"local {self.variant} strategy: {action.value.lower()} with weighted score "
                f"{score:+.2f}, "
                f"5m volume ratio {volume_ratio:.2f}, extension {extension:+.2f} ATR; "
                + (
                    f"stop {_STOP_ATR} ATR and target {_TARGET_R}R"
                    if structure is None
                    else f"{structure['detail']}"
                )
                + (f"; {flow_detail}" if flow_state is not None else "")
            ),
        )

    @staticmethod
    def _flow_assessment(
        features: dict[str, float], direction: int
    ) -> tuple[str | None, str | None, str | None]:
        required = {
            "open_interest_change_5m",
            "global_long_short_ratio",
            "global_long_short_ratio_change_5m",
            "top_long_short_position_ratio",
            "top_long_short_position_ratio_change_5m",
            "taker_buy_sell_ratio",
        }
        missing = sorted(required - features.keys())
        if missing:
            return None, None, f"positioning shadow unavailable: {', '.join(missing)}"

        oi_change = features["open_interest_change_5m"]
        taker_ratio = features["taker_buy_sell_ratio"]
        if direction > 0:
            if oi_change > 0 and taker_ratio >= _FLOW_TAKER_LONG:
                state = "fresh_long"
            elif oi_change < 0:
                state = "short_covering"
            else:
                state = "mixed_long"
            contradictory = oi_change >= 0 and taker_ratio <= _FLOW_TAKER_SHORT
        else:
            if oi_change > 0 and taker_ratio <= _FLOW_TAKER_SHORT:
                state = "fresh_short"
            elif oi_change < 0:
                state = "long_liquidation"
            else:
                state = "mixed_short"
            contradictory = oi_change >= 0 and taker_ratio >= _FLOW_TAKER_LONG

        detail = (
            f"flow {state}: OI {oi_change:+.4%}, taker {taker_ratio:.3f}, "
            f"global crowding {features['global_long_short_ratio']:.3f} "
            f"({features['global_long_short_ratio_change_5m']:+.3f}), top-position "
            f"{features['top_long_short_position_ratio']:.3f} "
            f"({features['top_long_short_position_ratio_change_5m']:+.3f})"
        )
        if contradictory:
            return state, detail, f"new positioning and taker flow contradict entry; {detail}"
        return state, detail, None

    @staticmethod
    def _structure_plan(
        snapshot: MarketSnapshot, direction: int
    ) -> tuple[dict[str, object] | None, str | None]:
        features = snapshot.features
        required = {
            "5m_atr_14",
            "5m_last_bar_close_position",
            "5m_breakout_hold_above_20",
            "5m_breakdown_hold_below_20",
            "5m_breakout_hold_high_20",
            "5m_breakout_hold_low_20",
        }
        missing = sorted(required - features.keys())
        if missing:
            return None, f"structure inputs unavailable: {', '.join(missing)}"
        atr = Decimal(str(features["5m_atr_14"]))
        if atr <= 0:
            return None, "5m ATR is not positive"
        entry = snapshot.mark_price

        breakout = bool(
            features[
                "5m_breakout_hold_above_20"
                if direction > 0
                else "5m_breakdown_hold_below_20"
            ]
        )
        if breakout:
            invalidation_level = Decimal(
                str(
                    features[
                        "5m_breakout_hold_high_20"
                        if direction > 0
                        else "5m_breakout_hold_low_20"
                    ]
                )
            )
            setup_type = "TREND_BREAKOUT"
            trigger_type = "BREAKOUT"
            invalidation_type = "RANGE"
            setup_detail = "two-bar breakout hold"
        else:
            signed_distance = direction * features["5m_ema20_distance_atr"]
            close_position = features["5m_last_bar_close_position"]
            strong_close = close_position >= 0.65 if direction > 0 else close_position <= 0.35
            if not 0 <= signed_distance <= 0.5 or not strong_close:
                return None, (
                    "no two-bar breakout or confirmed EMA20 pullback reclaim "
                    f"(distance {signed_distance:.2f} ATR, close-position {close_position:.2f})"
                )
            invalidation_level = Decimal(str(features["5m_ema_20"]))
            setup_type = "TREND_PULLBACK"
            trigger_type = "RECLAIM"
            invalidation_type = "EMA"
            setup_detail = "EMA20 pullback reclaim"

        buffer = atr * _STRUCTURE_BUFFER_ATR
        stop = (
            invalidation_level - buffer
            if direction > 0
            else invalidation_level + buffer
        )
        if (direction > 0 and stop >= entry) or (direction < 0 and stop <= entry):
            return None, "structure invalidation does not leave a valid stop beyond entry"
        risk_distance = abs(entry - stop)

        reward_side = "high" if direction > 0 else "low"
        target_candidates: list[tuple[Decimal, str]] = []
        for interval in ("15m", "30m", "1h", "4h"):
            for suffix in (f"range_{reward_side}_20", f"range_{reward_side}_50"):
                value = features.get(f"{interval}_{suffix}")
                if value is not None:
                    target_candidates.append((Decimal(str(value)), "RANGE"))
            swing_key = f"{interval}_last_swing_{reward_side}"
            if features.get(f"{swing_key}_confirmed") == 1 and swing_key in features:
                target_candidates.append((Decimal(str(features[swing_key])), "SWING"))
        for key in (f"1d_previous_{reward_side}", f"1d_range_{reward_side}_20"):
            if key in features:
                target_candidates.append((Decimal(str(features[key])), "DAILY_LEVEL"))
        valid_targets = [
            (value, kind)
            for value, kind in target_candidates
            if (direction > 0 and value > entry) or (direction < 0 and value < entry)
        ]
        if valid_targets:
            target, target_type = min(
                valid_targets, key=lambda item: abs(item[0] - entry)
            )
            reward_risk = abs(target - entry) / risk_distance
            if reward_risk <= Decimal("1.15"):
                return None, (
                    f"nearest structure target offers only {reward_risk:.2f}R"
                )
        else:
            target = entry + Decimal(direction) * risk_distance * _TARGET_R
            target_type = "R_MULTIPLE"
            reward_risk = _TARGET_R

        return {
            "stop": stop,
            "target": target,
            "setup_type": setup_type,
            "anchor_price": invalidation_level,
            "trigger_type": trigger_type,
            "invalidation_type": invalidation_type,
            "invalidation_level": invalidation_level,
            "target_type": target_type,
            "detail": (
                f"{setup_detail}, structure stop {_STRUCTURE_BUFFER_ATR} ATR beyond "
                f"invalidation and nearest target {reward_risk:.2f}R"
            ),
        }, None

    @staticmethod
    def _confidence(score: float, volume_ratio: float) -> float:
        score_strength = max(0.0, abs(score) - _ENTRY_SCORE)
        volume_bonus = min(0.10, max(0.0, volume_ratio - 1.0) * 0.05)
        return round(min(0.95, 0.55 + score_strength * 0.5 + volume_bonus), 3)
