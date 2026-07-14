import asyncio
from datetime import UTC, datetime
from decimal import Decimal

from candlepilot.domain.models import MarketSnapshot, OrderPlan, OrderType
from candlepilot.execution.paper import PaperExecutor


def _snapshot(mark: str, bid: str, ask: str) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTCUSDT",
        cadence="1m",
        timestamp=datetime.now(UTC),
        mark_price=mark,
        bid=bid,
        ask=ask,
        quote_volume_24h="1000000",
    )


def test_resting_limit_order_fills_on_later_quote() -> None:
    async def scenario():
        executor = PaperExecutor(slippage_fraction=Decimal("0"))
        order = OrderPlan(
            client_order_id="limit-entry",
            symbol="BTCUSDT",
            side="BUY",
            quantity=Decimal("1"),
            order_type=OrderType.LIMIT,
            price=Decimal("99"),
            stop_price=Decimal("95"),
        )
        placed = await executor.execute(order, _snapshot("100", "99.9", "100.1"))
        fills = await executor.mark_to_market(_snapshot("98.9", "98.8", "98.9"))
        return executor, placed, fills

    executor, placed, fills = asyncio.run(scenario())
    assert placed.status == "NEW"
    assert fills[0].client_order_id == "limit-entry"
    assert fills[0].average_price == Decimal("99")
    assert executor.portfolio_state().open_positions == 1


def test_protective_stop_closes_paper_position() -> None:
    async def scenario():
        executor = PaperExecutor(slippage_fraction=Decimal("0"))
        await executor.execute(
            OrderPlan(
                client_order_id="market-entry",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("1"),
                order_type=OrderType.MARKET,
                stop_price=Decimal("98"),
                take_profit_price=Decimal("104"),
            ),
            _snapshot("100", "99.9", "100.1"),
            leverage=3,
        )
        reports = await executor.mark_to_market(_snapshot("97.9", "97.8", "98"))
        return executor, reports

    executor, reports = asyncio.run(scenario())
    assert len(reports) == 1
    assert reports[0].message == "paper stop_loss"
    assert executor.portfolio_state().open_positions == 0


def test_position_snapshots_report_unrealized_pnl_and_margin() -> None:
    async def scenario():
        executor = PaperExecutor(slippage_fraction=Decimal("0"), fee_rate=Decimal("0"))
        await executor.execute(
            OrderPlan(
                client_order_id="long-entry",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("2"),
                order_type=OrderType.MARKET,
                stop_price=Decimal("95"),
                take_profit_price=Decimal("110"),
            ),
            _snapshot("100", "99.9", "100.1"),
            leverage=4,
        )
        await executor.mark_to_market(_snapshot("105", "104.9", "105.1"))
        return executor.position_snapshots()

    snapshots = asyncio.run(scenario())
    assert len(snapshots) == 1
    position = snapshots[0]
    assert position["symbol"] == "BTCUSDT"
    assert position["side"] == "LONG"
    assert position["leverage"] == 4
    # Entered at the slippage-free ask of 100.1, mark now 105.
    assert position["unrealized_pnl"] == Decimal("2") * (Decimal("105") - Decimal("100.1"))
    assert position["notional"] == Decimal("2") * Decimal("105")
    assert position["margin_used"] == position["notional"] / Decimal("4")
    assert position["stop_loss"] == Decimal("95")
    assert position["take_profit"] == Decimal("110")


def test_take_profit_closes_short_position() -> None:
    async def scenario():
        executor = PaperExecutor(slippage_fraction=Decimal("0"))
        await executor.execute(
            OrderPlan(
                client_order_id="short-entry",
                symbol="BTCUSDT",
                side="SELL",
                quantity=Decimal("1"),
                order_type=OrderType.MARKET,
                stop_price=Decimal("102"),
                take_profit_price=Decimal("96"),
            ),
            _snapshot("100", "99.9", "100.1"),
        )
        reports = await executor.mark_to_market(_snapshot("95.9", "95.8", "96"))
        return executor, reports

    executor, reports = asyncio.run(scenario())
    assert reports[0].message == "paper take_profit"
    assert executor.portfolio_state().open_positions == 0
