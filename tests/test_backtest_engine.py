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


def _position_action(action: TradeAction) -> TradeIntent:
    return TradeIntent(
        symbol="BTCUSDT",
        cadence="5m",
        action=action,
        confidence=0.8,
        leverage=3,
        risk_fraction="0.01" if action == TradeAction.ADD else "0",
        stop_loss="80" if action == TradeAction.ADD else None,
        rationale=f"{action.value} cached decision",
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


def test_backtest_add_increases_quantity_and_reweights_entry() -> None:
    candles = [
        _candle(0, "100", "101", "99", "100"),
        _candle(1, "100", "101", "99", "100"),
        _candle(2, "110", "111", "109", "110"),
        _candle(3, "110", "111", "109", "110"),
    ]
    opening = _long().model_copy(update={"stop_loss": Decimal("80"), "take_profit": None})
    result = BacktestEngine().run(
        candles,
        [
            ReplayIntent(candles[0].timestamp, opening),
            ReplayIntent(candles[1].timestamp, _position_action(TradeAction.ADD)),
            ReplayIntent(candles[2].timestamp, _position_action(TradeAction.CLOSE)),
        ],
    )

    trade = result.trades[0]
    opening_entry = Decimal("100") * Decimal("1.0005")
    assert len(result.trades) == 1
    assert trade.quantity > Decimal("8")
    assert opening_entry < trade.entry_price < Decimal("110") * Decimal("1.0005")
    assert trade.exit_reason == "model_exit"


def test_backtest_reduce_closes_half_and_preserves_remainder() -> None:
    candles = [
        _candle(0, "100", "101", "99", "100"),
        _candle(1, "100", "101", "99", "100"),
        _candle(2, "102", "103", "101", "102"),
        _candle(3, "104", "105", "103", "104"),
    ]
    opening = _long().model_copy(update={"stop_loss": Decimal("80"), "take_profit": None})
    result = BacktestEngine().run(
        candles,
        [
            ReplayIntent(candles[0].timestamp, opening),
            ReplayIntent(candles[1].timestamp, _position_action(TradeAction.REDUCE)),
            ReplayIntent(candles[2].timestamp, _position_action(TradeAction.CLOSE)),
        ],
    )

    assert len(result.trades) == 2
    reduced, remainder = result.trades
    assert reduced.quantity == remainder.quantity
    assert reduced.exit_time == candles[2].timestamp
    assert remainder.exit_time == candles[3].timestamp
    assert reduced.fees + remainder.fees == result.total_fees
    assert all(trade.exit_reason == "model_exit" for trade in result.trades)


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


def test_backtest_groups_trades_by_market_regime() -> None:
    def _rising(index: int) -> Candle:
        base = Decimal(100 + index)
        return Candle(
            timestamp=START + timedelta(minutes=5 * index),
            open=base,
            high=base + Decimal("2"),
            low=base - Decimal("1"),
            close=base + Decimal("0.5"),
            volume=Decimal("1000"),
        )

    candles = [_rising(index) for index in range(20)]
    intent = _long().model_copy(update={"stop_loss": Decimal("50"), "take_profit": None})
    # Decision at candle 16 executes at candle 17, whose trailing window is a
    # clean uptrend, so the entry regime must be classified as trend_up.
    result = BacktestEngine().run(candles, [ReplayIntent(candles[16].timestamp, intent)])

    assert "regime" in result.grouped_stats
    assert len(result.trades) == 1
    assert result.trades[0].regime == "trend_up"
    assert result.grouped_stats["regime"]["trend_up"].trade_count == 1
    grouped_total = sum(
        stats.trade_count for stats in result.grouped_stats["regime"].values()
    )
    assert grouped_total == len(result.trades)
