from __future__ import annotations

import time
from datetime import timedelta
from decimal import Decimal

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


def _sign(value: float) -> int:
    return 1 if value > 0 else -1 if value < 0 else 0


class LocalRuleProvider(DecisionProvider):
    """Deterministic trend decisions from the snapshot CandlePilot already builds."""

    name = LOCAL_RULE_PROVIDER
    model = LOCAL_RULE_MODEL
    reasoning_effort = None
    reasoning_effort_options: tuple[str, ...] = ()
    timeout = 1.0

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
            live_shadow_only=False,
        )

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name,
            available=True,
            authenticated=True,
            version=LOCAL_RULE_VERSION,
            detail="deterministic local strategy; no external model call",
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
                "strategy_variant": "standard",
                "live_shadow_only": False,
            },
            prompt_version=None,
            data_version=content_fingerprint(
                payload, schema_version=MARKET_SNAPSHOT_SCHEMA_VERSION
            ),
            provider_version=LOCAL_RULE_VERSION,
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

        atr = Decimal(str(features[f"{snapshot.cadence}_atr_14"]))
        if atr <= 0:
            return TradeIntent.hold(
                snapshot.symbol,
                snapshot.cadence,
                "local trend strategy: decision-cadence ATR is not positive",
            )
        entry = snapshot.mark_price
        stop_distance = atr * _STOP_ATR
        target_distance = stop_distance * _TARGET_R
        stop = entry - stop_distance if direction > 0 else entry + stop_distance
        target = entry + target_distance if direction > 0 else entry - target_distance
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
            setup_type="TREND_CONTINUATION",
            anchor_timeframe="5m",
            anchor_price=entry,
            trigger_type="MARKET_CONFIRMED",
            trigger_price=entry,
            invalidation_type="EMA",
            invalidation_level=Decimal(str(features["5m_ema_20"])),
            target_type="R_MULTIPLE",
            rationale=(
                f"local trend strategy: {action.value.lower()} with weighted score "
                f"{score:+.2f}, "
                f"5m volume ratio {volume_ratio:.2f}, extension {extension:+.2f} ATR; "
                f"stop {_STOP_ATR} ATR and target {_TARGET_R}R"
            ),
        )

    @staticmethod
    def _confidence(score: float, volume_ratio: float) -> float:
        score_strength = max(0.0, abs(score) - _ENTRY_SCORE)
        volume_bonus = min(0.10, max(0.0, volume_ratio - 1.0) * 0.05)
        return round(min(0.95, 0.55 + score_strength * 0.5 + volume_bonus), 3)
