import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from candlepilot.domain.models import (
    MarketSnapshot,
    PortfolioState,
    PositionState,
    TradeAction,
)
from candlepilot.providers.local import LocalRuleProvider


def _features(direction: int = 1) -> dict[str, float]:
    features: dict[str, float] = {}
    for interval in ("5m", "15m", "30m", "1h", "4h"):
        features[f"{interval}_ema_spread"] = direction * 0.01
        features[f"{interval}_atr_14"] = 2.0
    features.update(
        {
            "5m_return_1": direction * 0.002,
            "5m_return_5": direction * 0.004,
            "5m_quote_volume_ratio": 1.2,
            "5m_ema20_distance_atr": direction * 0.5,
            "5m_ema_20": 99.0 if direction > 0 else 101.0,
        }
    )
    return features


def _experimental_features(direction: int = 1) -> dict[str, float]:
    features = _features(direction)
    features.update(
        {
            "5m_last_bar_close_position": 0.8 if direction > 0 else 0.2,
            "5m_breakout_hold_above_20": float(direction > 0),
            "5m_breakdown_hold_below_20": float(direction < 0),
            "5m_breakout_hold_high_20": 99.0,
            "5m_breakout_hold_low_20": 101.0,
            "15m_range_high_20": 106.0,
            "15m_range_low_20": 94.0,
            "open_interest_change_5m": 0.02,
            "global_long_short_ratio": 1.2,
            "global_long_short_ratio_change_5m": -0.05 * direction,
            "top_long_short_position_ratio": 1.4,
            "top_long_short_position_ratio_change_5m": -0.03 * direction,
            "taker_buy_sell_ratio": 1.2 if direction > 0 else 0.8,
        }
    )
    return features


def _snapshot(features: dict[str, float] | None = None) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTCUSDT",
        cadence="5m",
        timestamp=datetime.now(UTC),
        mark_price="100",
        bid="99.9",
        ask="100.1",
        quote_volume_24h="1000000",
        features=features if features is not None else _features(),
    )


def _portfolio(**updates: object) -> PortfolioState:
    return PortfolioState(
        equity="10000",
        available_balance="10000",
        **updates,
    )


def test_local_rule_opens_with_deterministic_atr_protection_and_zero_usage() -> None:
    result = asyncio.run(
        LocalRuleProvider().generate_trade_intent(_snapshot(), _portfolio())
    )

    assert result.intent.action == TradeAction.OPEN_LONG
    assert result.intent.risk_fraction == Decimal("0.005")
    assert result.intent.leverage == 3
    assert result.intent.stop_loss == Decimal("97.00")
    assert result.intent.take_profit == Decimal("104.500")
    assert result.intent.decision_framework == "structure-v1"
    assert result.intent.setup_type == "TREND_CONTINUATION"
    assert result.intent.anchor_timeframe == "5m"
    assert result.intent.invalidation_type == "EMA"
    assert result.intent.invalidation_level == Decimal("99.0")
    assert result.usage["total_tokens"] == 0
    assert result.usage["cost_usd"] == 0
    assert result.input_payload is not None
    assert result.prompt is None


def test_local_rule_holds_without_the_shared_feature_ladder() -> None:
    result = asyncio.run(
        LocalRuleProvider().generate_trade_intent(
            _snapshot({"5m_ema_spread": 0.1}), _portfolio()
        )
    )

    assert result.intent.action == TradeAction.HOLD
    assert "missing required features" in result.intent.rationale


def test_local_rule_requires_both_five_minute_momentum_horizons_to_align() -> None:
    for field in ("5m_return_1", "5m_return_5"):
        features = _features()
        features[field] *= -1

        result = asyncio.run(
            LocalRuleProvider().generate_trade_intent(
                _snapshot(features), _portfolio()
            )
        )

        assert result.intent.action == TradeAction.HOLD
        assert "return_1 and return_5 must both align" in result.intent.rationale


def test_local_rule_closes_only_on_a_confirmed_opposing_signal() -> None:
    portfolio = _portfolio(
        open_positions=1,
        positions={
            "BTCUSDT": PositionState(
                side="LONG",
                quantity="1",
                entry_price="100",
                stop_loss="97",
                take_profit="104.5",
            )
        },
    )
    result = asyncio.run(
        LocalRuleProvider().generate_trade_intent(
            _snapshot(_features(-1)), portfolio
        )
    )

    assert result.intent.action == TradeAction.CLOSE
    assert result.intent.risk_fraction == 0


def test_local_rule_declares_no_external_probe_or_retry() -> None:
    provider = LocalRuleProvider()
    health = asyncio.run(provider.health_check())

    assert health.authenticated is True
    assert provider.capabilities.external_inference is False
    assert provider.capabilities.requires_backtest_probe is False
    assert provider.capabilities.retryable is False
    assert provider.capabilities.configurable_model is False
    assert provider.model == "trend-v2"
    assert health.version == "local-trend-v2"


def test_structure_experiment_uses_confirmed_level_for_protection() -> None:
    provider = LocalRuleProvider("structure")
    result = asyncio.run(
        provider.generate_trade_intent(
            _snapshot(_experimental_features()), _portfolio()
        )
    )

    assert result.intent.action == TradeAction.OPEN_LONG
    assert result.intent.setup_type == "TREND_BREAKOUT"
    assert result.intent.invalidation_type == "RANGE"
    assert result.intent.invalidation_level == Decimal("99.0")
    assert result.intent.stop_loss == Decimal("98.50")
    assert result.intent.take_profit == Decimal("106.0")
    assert result.usage["strategy_variant"] == "structure"
    assert provider.capabilities.live_shadow_only is True


def test_flow_experiment_classifies_fresh_positioning_and_rejects_contradiction() -> None:
    provider = LocalRuleProvider("flow")
    accepted = asyncio.run(
        provider.generate_trade_intent(
            _snapshot(_experimental_features()), _portfolio()
        )
    )
    assert accepted.intent.action == TradeAction.OPEN_LONG
    assert "flow fresh_long" in accepted.intent.rationale

    contradictory = _experimental_features()
    contradictory["taker_buy_sell_ratio"] = 0.8
    rejected = asyncio.run(
        provider.generate_trade_intent(_snapshot(contradictory), _portfolio())
    )
    assert rejected.intent.action == TradeAction.HOLD
    assert "taker flow contradict" in rejected.intent.rationale


def test_flow_experiment_holds_when_formal_positioning_was_not_recorded() -> None:
    result = asyncio.run(
        LocalRuleProvider("structure-flow").generate_trade_intent(
            _snapshot(_features()), _portfolio()
        )
    )

    assert result.intent.action == TradeAction.HOLD
    assert "positioning shadow unavailable" in result.intent.rationale
