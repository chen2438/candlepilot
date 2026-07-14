import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from candlepilot.domain.models import MarketSnapshot, OrderPlan, OrderType
from candlepilot.execution.paper import PaperExecutor
from candlepilot.storage.database import AuditRepository, Database


def _snapshot(mark: str = "100", bid: str = "99.9", ask: str = "100.1") -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTCUSDT",
        cadence="1m",
        timestamp=datetime.now(UTC),
        mark_price=mark,
        bid=bid,
        ask=ask,
        quote_volume_24h="1000000",
    )


def test_paper_positions_and_pending_orders_survive_restart(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'paper-state.db'}")
        await database.initialize()
        store = AuditRepository(database.sessions)
        first = PaperExecutor(state_store=store, slippage_fraction=Decimal("0"))
        await first.execute(
            OrderPlan(
                client_order_id="persistent-position",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("1"),
                order_type=OrderType.MARKET,
                stop_price=Decimal("95"),
            ),
            _snapshot(),
            leverage=2,
        )
        await first.execute(
            OrderPlan(
                client_order_id="persistent-limit",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("0.5"),
                order_type=OrderType.LIMIT,
                price=Decimal("98"),
                stop_price=Decimal("94"),
            ),
            _snapshot(),
            leverage=2,
        )

        restored = PaperExecutor(state_store=store, slippage_fraction=Decimal("0"))
        loaded = await restored.restore()
        before_fill = restored.portfolio_state()
        fills = await restored.mark_to_market(_snapshot("97.9", "97.8", "97.9"))
        after_fill = restored.portfolio_state()
        await database.close()
        return loaded, before_fill, fills, after_fill

    loaded, before_fill, fills, after_fill = asyncio.run(scenario())
    assert loaded
    assert before_fill.open_positions == 1
    assert any(report.client_order_id == "persistent-limit" for report in fills)
    assert after_fill.symbol_quantities["BTCUSDT"] == Decimal("1.5")
