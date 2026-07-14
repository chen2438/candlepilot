from datetime import UTC, datetime, timedelta
from decimal import Decimal

from candlepilot.backtest.engine import BacktestConfig, Candle, ReplayIntent
from candlepilot.backtest.portfolio import PortfolioBacktestEngine
from candlepilot.domain.models import TradeAction, TradeIntent


def _leg(symbol: str, offset: int = 0):
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=offset)
    candles = [
        Candle(start, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("10")),
        Candle(
            start + timedelta(minutes=5),
            Decimal("100"),
            Decimal("105"),
            Decimal("99"),
            Decimal("104"),
            Decimal("10"),
        ),
    ]
    intent = TradeIntent(
        symbol=symbol,
        cadence="5m",
        action=TradeAction.OPEN_LONG,
        confidence=0.8,
        leverage=2,
        risk_fraction=Decimal("0.01"),
        stop_loss=Decimal("98"),
        take_profit=Decimal("104"),
        rationale="portfolio fixture",
    )
    return candles, [ReplayIntent(start, intent)]


def test_portfolio_backtest_allocates_capital_once_and_aligns_curves() -> None:
    result = PortfolioBacktestEngine(BacktestConfig(initial_equity=Decimal("10000"))).run(
        {"BTCUSDT": _leg("BTCUSDT"), "ETHUSDT": _leg("ETHUSDT", 1)}
    )

    assert result.allocation == "equal_weight_sleeves"
    assert result.per_symbol["BTCUSDT"].initial_equity == Decimal("5000")
    assert result.per_symbol["ETHUSDT"].initial_equity == Decimal("5000")
    assert result.final_equity == sum(
        (item.final_equity for item in result.per_symbol.values()), Decimal("0")
    )
    assert len(result.equity_curve) == 4
