from datetime import UTC, datetime, timedelta
from decimal import Decimal

from candlepilot.backtest.engine import BacktestEngine, Candle, ReplayIntent
from candlepilot.domain.models import TradeAction, TradeIntent


START = datetime(2026, 1, 1, tzinfo=UTC)


def _candle(index: int, open_: str, high: str, low: str, close: str) -> Candle:
    return Candle(
        timestamp=START + timedelta(minutes=5 * index),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1000"),
    )


def _long() -> TradeIntent:
    return TradeIntent(
        symbol="BTCUSDT",
        cadence="5m",
        action=TradeAction.OPEN_LONG,
        confidence=0.8,
        leverage=3,
        risk_fraction="0.01",
        stop_loss="98",
        take_profit="104",
        rationale="cached decision",
    )


def test_decision_executes_at_next_bar_without_lookahead() -> None:
    candles = [
        _candle(0, "100", "103", "99", "102"),
        _candle(1, "102", "105", "101", "104"),
    ]
    result = BacktestEngine().run(candles, [ReplayIntent(candles[0].timestamp, _long())])
    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_time == candles[1].timestamp
    assert trade.entry_price > candles[1].open
    assert trade.exit_reason == "take_profit"


def test_stop_wins_when_stop_and_target_touch_same_bar() -> None:
    candles = [
        _candle(0, "100", "101", "99", "100"),
        _candle(1, "100", "105", "97", "102"),
    ]
    result = BacktestEngine().run(candles, [ReplayIntent(candles[0].timestamp, _long())])
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.trades[0].net_pnl < 0


def test_backtest_is_reproducible() -> None:
    candles = [
        _candle(0, "100", "101", "99", "100"),
        _candle(1, "100", "105", "99", "104"),
    ]
    decisions = [ReplayIntent(candles[0].timestamp, _long())]
    first = BacktestEngine().run(candles, decisions)
    second = BacktestEngine().run(candles, decisions)
    assert first == second


def test_backtest_reports_risk_adjusted_turnover_and_exposure_metrics() -> None:
    candles = [
        _candle(0, "100", "101", "99", "100"),
        _candle(1, "100", "102", "99", "101"),
        _candle(2, "101", "102", "99", "100"),
        _candle(3, "100", "103", "99", "102"),
    ]
    intent = _long().model_copy(update={"stop_loss": Decimal("90"), "take_profit": None})
    result = BacktestEngine().run(candles, [ReplayIntent(candles[0].timestamp, intent)])

    assert result.sharpe_ratio is not None
    assert result.sortino_ratio is not None
    assert result.turnover > 0
    assert Decimal("0") < result.exposure_fraction < Decimal("1")


def test_backtest_groups_trades_by_side_and_exit_reason() -> None:
    candles = [
        _candle(0, "100", "101", "99", "100"),
        _candle(1, "100", "105", "99", "104"),
    ]
    result = BacktestEngine().run(candles, [ReplayIntent(candles[0].timestamp, _long())])

    long_stats = result.grouped_stats["side"]["LONG"]
    target_stats = result.grouped_stats["exit_reason"]["take_profit"]
    assert long_stats.trade_count == 1
    assert long_stats.win_rate == Decimal("1")
    assert target_stats.net_pnl == result.trades[0].net_pnl
