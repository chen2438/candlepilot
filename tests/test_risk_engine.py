from datetime import UTC, datetime, timedelta
from decimal import Decimal

from candlepilot.domain.models import (
    MarketSnapshot,
    PortfolioState,
    TradeAction,
    TradeIntent,
)
from candlepilot.risk.engine import AggressiveRiskPolicy, SymbolRules


RULES = SymbolRules(
    quantity_step=Decimal("0.001"),
    min_quantity=Decimal("0.001"),
    min_notional=Decimal("5"),
)


def _snapshot(*, age_seconds: int = 0) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTCUSDT",
        cadence="5m",
        timestamp=datetime.now(UTC) - timedelta(seconds=age_seconds),
        mark_price="100",
        bid="99.9",
        ask="100.1",
        quote_volume_24h="1000000",
    )


def _intent(action: TradeAction = TradeAction.OPEN_LONG) -> TradeIntent:
    return TradeIntent(
        symbol="BTCUSDT",
        cadence="5m",
        action=action,
        confidence=0.8,
        leverage=5,
        risk_fraction="0.02",
        stop_loss="98" if action != TradeAction.OPEN_SHORT else "102",
        take_profit="104",
        rationale="test signal",
    )


def _portfolio(**changes) -> PortfolioState:
    values = {
        "equity": "10000",
        "available_balance": "8000",
        "daily_pnl": "0",
        "open_positions": 0,
        "margin_used": "0",
    }
    values.update(changes)
    return PortfolioState(**values)


def test_sizes_position_from_stop_distance_and_rounds_down() -> None:
    result = AggressiveRiskPolicy().evaluate(_intent(), _snapshot(), _portfolio(), RULES)
    assert result.decision.accepted
    assert result.order is not None
    assert result.order.quantity == Decimal("95.238")
    assert result.order.stop_price == Decimal("98")
    assert result.order.take_profit_price == Decimal("104")


def test_rejects_stale_market_data() -> None:
    result = AggressiveRiskPolicy().evaluate(
        _intent(), _snapshot(age_seconds=30), _portfolio(), RULES
    )
    assert not result.decision.accepted
    assert "stale" in result.decision.reason


def test_daily_loss_circuit_breaker() -> None:
    result = AggressiveRiskPolicy().evaluate(
        _intent(), _snapshot(), _portfolio(equity="9200", daily_pnl="-800"), RULES
    )
    assert not result.decision.accepted
    assert "circuit breaker" in result.decision.reason


def test_opposing_position_must_close_first() -> None:
    result = AggressiveRiskPolicy().evaluate(
        _intent(TradeAction.OPEN_LONG),
        _snapshot(),
        _portfolio(open_positions=1, symbol_sides={"BTCUSDT": "SHORT"}),
        RULES,
    )
    assert not result.decision.accepted
    assert "closed" in result.decision.reason


def test_close_is_always_reduce_only() -> None:
    result = AggressiveRiskPolicy().evaluate(
        _intent(TradeAction.CLOSE),
        _snapshot(),
        _portfolio(
            open_positions=1,
            symbol_sides={"BTCUSDT": "LONG"},
            symbol_quantities={"BTCUSDT": "1.2345"},
        ),
        RULES,
    )
    assert result.decision.accepted
    assert result.order is not None and result.order.reduce_only
    assert result.order.side == "SELL"
    assert result.order.quantity == Decimal("1.234")
