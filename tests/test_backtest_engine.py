from datetime import UTC, datetime, timedelta
from decimal import Decimal

from candlepilot.backtest.engine import (
    BacktestConfig,
    Candle,
    EquityPoint,
    SimulatedExchange,
    summarize,
)
from candlepilot.domain.models import OrderPlan, OrderType

START = datetime(2026, 6, 1, tzinfo=UTC)


def _candle(index: int, *, open_=100, high=101, low=99, close=100, funding="0") -> Candle:
    return Candle(
        timestamp=START + timedelta(minutes=5 * index),
        open=Decimal(str(open_)),
        high=Decimal(str(high)),
        low=Decimal(str(low)),
        close=Decimal(str(close)),
        volume=Decimal("10"),
        funding_rate=Decimal(funding),
    )


def _order(
    side="BUY",
    *,
    quantity="1",
    stop="98",
    take="104",
    order_type=OrderType.MARKET,
    price=None,
) -> OrderPlan:
    return OrderPlan(
        client_order_id="cp-test",
        symbol="BTCUSDT",
        side=side,
        quantity=Decimal(quantity),
        order_type=order_type,
        price=Decimal(price) if price else None,
        stop_price=Decimal(stop) if stop else None,
        take_profit_price=Decimal(take) if take else None,
    )


def test_a_candle_touching_both_triggers_books_the_stop() -> None:
    """One candle cannot say which trigger came first, so assume the bad one.

    Preferring the take profit would turn every ambiguous bar into a winner and
    inflate every number downstream of it -- exactly the flattery a backtest
    exists to avoid.
    """

    exchange = SimulatedExchange(BacktestConfig(slippage_fraction=Decimal("0"), fee_rate=Decimal("0")))
    exchange.execute(_order(), _candle(0), leverage=1)

    # This bar reaches the take profit at 104 and the stop at 98.
    exchange.settle_candle("BTCUSDT", _candle(1, high=105, low=97))

    assert len(exchange.trades) == 1
    assert exchange.trades[0].exit_reason == "stop_loss"
    assert exchange.trades[0].exit_price == Decimal("98")


def test_entries_fill_on_the_next_candle_not_the_decided_one() -> None:
    """The model reasoned on a closed bar, so filling inside it reads the future."""

    exchange = SimulatedExchange(BacktestConfig(slippage_fraction=Decimal("0"), fee_rate=Decimal("0")))

    report = exchange.execute(_order(), _candle(1, open_=103), leverage=1)

    # Filled at the next bar's open, not the decision bar's close.
    assert report.average_price == Decimal("103")


def test_resting_limit_waits_until_a_later_candle_touches_its_price() -> None:
    exchange = SimulatedExchange(
        BacktestConfig(slippage_fraction=Decimal("0"), fee_rate=Decimal("0"))
    )
    order = _order(order_type=OrderType.LIMIT, price="50", stop="40", take="60")

    report = exchange.execute(order, _candle(1, open_=100, high=110, low=90), leverage=1)
    exchange.settle_candle("BTCUSDT", _candle(1, open_=100, high=110, low=90))
    before_touch = exchange.portfolio_state({})
    exchange.settle_candle("BTCUSDT", _candle(2, open_=55, high=58, low=45, close=52))
    after_touch = exchange.portfolio_state({"BTCUSDT": Decimal("52")})

    assert report.status == "NEW" and report.filled_quantity == 0
    assert not before_touch.positions
    assert after_touch.positions["BTCUSDT"].entry_price == Decimal("50")


def test_marketable_limit_fills_at_open_without_breaching_its_limit() -> None:
    exchange = SimulatedExchange(
        BacktestConfig(slippage_fraction=Decimal("0.001"), fee_rate=Decimal("0"))
    )
    report = exchange.execute(
        _order(order_type=OrderType.LIMIT, price="105"),
        _candle(1, open_=100),
        leverage=1,
    )

    assert report.status == "FILLED"
    assert report.average_price == Decimal("100.100")
    assert report.average_price <= Decimal("105")


def test_resting_limit_that_reaches_its_stop_in_the_fill_bar_is_stopped() -> None:
    exchange = SimulatedExchange(
        BacktestConfig(slippage_fraction=Decimal("0"), fee_rate=Decimal("0"))
    )
    exchange.execute(
        _order(order_type=OrderType.LIMIT, price="95", stop="92", take="105"),
        _candle(1, open_=100),
        leverage=1,
    )

    exchange.settle_candle("BTCUSDT", _candle(1, open_=100, high=110, low=90))

    assert exchange.trades[0].entry_price == Decimal("95")
    assert exchange.trades[0].exit_reason == "stop_loss"


def test_slippage_and_fees_are_charged_against_the_trade() -> None:
    exchange = SimulatedExchange(
        BacktestConfig(slippage_fraction=Decimal("0.001"), fee_rate=Decimal("0.0005"))
    )
    report = exchange.execute(_order(), _candle(0), leverage=1)

    # A buy pays up through the spread, never down.
    assert report.average_price == Decimal("100") * Decimal("1.001")
    assert exchange.cash < Decimal("10000")


def test_funding_accrues_by_side_and_lands_in_the_trade() -> None:
    exchange = SimulatedExchange(BacktestConfig(slippage_fraction=Decimal("0"), fee_rate=Decimal("0")))
    exchange.execute(_order(), _candle(0), leverage=1)

    # A long pays funding when the rate is positive.
    exchange.settle_candle("BTCUSDT", _candle(1, funding="0.001"))
    exchange.settle_candle("BTCUSDT", _candle(2, low=97))

    trade = exchange.trades[0]
    assert trade.funding > 0
    assert trade.exit_reason == "stop_loss"


def test_open_positions_are_flattened_so_no_unrealised_tail_is_counted() -> None:
    exchange = SimulatedExchange(BacktestConfig(slippage_fraction=Decimal("0"), fee_rate=Decimal("0")))
    exchange.execute(_order(), _candle(0), leverage=1)

    exchange.close_all({"BTCUSDT": Decimal("102")}, START + timedelta(hours=1))

    assert exchange.trades[0].exit_reason == "run_end"
    assert exchange.trades[0].exit_price == Decimal("102")
    assert not exchange.portfolio_state({}).positions


def test_run_end_reports_forced_closes_and_cancelled_pending_orders() -> None:
    exchange = SimulatedExchange(
        BacktestConfig(slippage_fraction=Decimal("0"), fee_rate=Decimal("0"))
    )
    exchange.execute(_order(), _candle(0), leverage=1)
    exchange.execute(
        _order(
            side="SELL",
            order_type=OrderType.LIMIT,
            price="150",
            stop="160",
            take="140",
        ),
        _candle(0),
        leverage=1,
    )

    cancelled = exchange.close_all(
        {"BTCUSDT": Decimal("102")}, START + timedelta(hours=1)
    )
    result = summarize(
        exchange.config,
        exchange.trades,
        [EquityPoint(START, exchange.cash)],
        cancelled_pending_orders=cancelled,
    )

    assert result.run_end_trade_count == 1
    assert result.cancelled_pending_orders == 1


def test_result_reconciles_gross_pnl_costs_and_final_equity() -> None:
    exchange = SimulatedExchange(
        BacktestConfig(slippage_fraction=Decimal("0"), fee_rate=Decimal("0.001"))
    )
    exchange.execute(_order(), _candle(0), leverage=1)
    exchange.settle_candle("BTCUSDT", _candle(1, funding="0.001"))
    exchange.close_all({"BTCUSDT": Decimal("102")}, START + timedelta(hours=1))
    result = summarize(
        exchange.config,
        exchange.trades,
        [EquityPoint(START, exchange.cash)],
    )

    assert result.net_pnl == result.final_equity - result.initial_equity
    assert (
        result.gross_price_pnl - result.total_fees - result.total_funding
        == result.net_pnl
    )


def test_position_state_carries_the_context_the_model_needs() -> None:
    """The backtest portfolio must look like the live one to the model."""

    exchange = SimulatedExchange(BacktestConfig(slippage_fraction=Decimal("0"), fee_rate=Decimal("0")))
    exchange.execute(_order(), _candle(0), leverage=3)

    position = exchange.portfolio_state({"BTCUSDT": Decimal("102")}).positions["BTCUSDT"]

    assert position.side == "LONG"
    assert position.entry_price == Decimal("100")
    assert position.unrealized_pnl == Decimal("2")
    assert position.stop_loss == Decimal("98")
    assert position.take_profit == Decimal("104")
    assert position.leverage == 3


def test_daily_pnl_resets_when_the_utc_date_changes() -> None:
    exchange = SimulatedExchange()
    exchange.portfolio_state({}, as_of=START)
    exchange.cash = Decimal("9200")

    same_day = exchange.portfolio_state({}, as_of=START + timedelta(hours=12))
    next_day = exchange.portfolio_state({}, as_of=START + timedelta(days=1))

    assert same_day.daily_pnl == Decimal("-800")
    assert next_day.daily_pnl == Decimal("0")


def test_profit_factor_is_undefined_rather_than_zero_without_losses() -> None:
    """Zero would read as the worst possible score for a flawless run."""

    exchange = SimulatedExchange(BacktestConfig(slippage_fraction=Decimal("0"), fee_rate=Decimal("0")))
    exchange.execute(_order(), _candle(0), leverage=1)
    exchange.settle_candle("BTCUSDT", _candle(1, high=105))

    result = summarize(
        exchange.config,
        exchange.trades,
        [EquityPoint(START, Decimal("10000")), EquityPoint(START, exchange.cash)],
    )

    assert result.trade_count == 1
    assert result.win_rate == Decimal("1")
    assert result.profit_factor is None


def test_drawdown_is_measured_from_the_running_peak() -> None:
    curve = [
        EquityPoint(START, Decimal("10000")),
        EquityPoint(START, Decimal("12000")),
        EquityPoint(START, Decimal("9000")),
        EquityPoint(START, Decimal("11000")),
    ]

    result = summarize(BacktestConfig(), [], curve)

    # 12000 -> 9000 is 25%, not the 10% the start-to-trough would suggest.
    assert result.max_drawdown == Decimal("0.25")
