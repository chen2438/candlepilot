import asyncio
from decimal import Decimal

from candlepilot.broker.binance_testnet import TrailingPosition
from candlepilot.risk.engine import SymbolRules
from candlepilot.risk.partial_take_profit import PartialTakeProfitManager
from candlepilot.storage.database import AuditRepository, Database


RULES = SymbolRules(
    quantity_step=Decimal("0.001"),
    min_quantity=Decimal("0.001"),
    min_notional=Decimal("5"),
    tick_size=Decimal("0.1"),
)


def _position(mark: str, *, quantity: str = "1") -> TrailingPosition:
    return TrailingPosition(
        symbol="BTCUSDT",
        side="LONG",
        quantity=Decimal(quantity),
        entry_price=Decimal("100"),
        mark_price=Decimal(mark),
        stop_loss=Decimal("98"),
    )


def test_partial_take_profit_profiles_record_target_and_breakeven_fills(
    tmp_path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'partial.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        manager = PartialTakeProfitManager(audit)
        await manager.maintain({"BTCUSDT": _position("102.4")}, {"BTCUSDT": RULES})
        await manager.maintain({"BTCUSDT": _position("99.9")}, {"BTCUSDT": RULES})
        events = await audit.recent_partial_take_profit_events()
        status = manager.status
        await database.close()
        return events, status

    events, status = asyncio.run(scenario())
    partials = [event for event in events if event["status"] == "partial_simulated_filled"]
    breakevens = [
        event for event in events if event["status"] == "breakeven_simulated_filled"
    ]
    assert len(partials) == 2
    assert {event["event"]["simulated_fill_price"] for event in partials} == {"102"}
    assert {event["event"]["observed_mark_price"] for event in partials} == {"102.4"}
    assert {event["event"]["partial_quantity"] for event in partials} == {
        "0.25",
        "0.50",
    }
    assert len(breakevens) == 2
    assert {event["event"]["simulated_fill_price"] for event in breakevens} == {
        "99.9"
    }
    assert {event["event"]["strategy_gross_pnl"] for event in breakevens} == {
        "0.425",
        "0.950",
    }
    assert status["partial_fills"] == 2
    assert status["breakeven_fills"] == 2


def test_partial_take_profit_state_survives_restart_without_duplicate_fill(
    tmp_path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'restart.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        first = PartialTakeProfitManager(audit)
        await first.maintain({"BTCUSDT": _position("102")}, {"BTCUSDT": RULES})
        restored = PartialTakeProfitManager(audit)
        await restored.maintain({"BTCUSDT": _position("103")}, {"BTCUSDT": RULES})
        events = await audit.recent_partial_take_profit_events()
        await database.close()
        return events

    events = asyncio.run(scenario())
    assert [event["status"] for event in events].count("partial_simulated_filled") == 2


def test_partial_take_profit_records_real_position_closure_after_partial_fill(
    tmp_path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'closed.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        manager = PartialTakeProfitManager(audit)
        await manager.maintain({"BTCUSDT": _position("102")}, {"BTCUSDT": RULES})
        await manager.maintain({}, {}, open_symbols=set())
        events = await audit.recent_partial_take_profit_events()
        status = manager.status
        await database.close()
        return events, status

    events, status = asyncio.run(scenario())
    assert [event["status"] for event in events].count("position_closed") == 2
    assert status["managed_positions"] == 0
