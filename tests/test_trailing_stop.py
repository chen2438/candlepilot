import asyncio
from decimal import Decimal

import pytest

from candlepilot.broker.binance_testnet import (
    StopLossReplacement,
    TrailingPosition,
    TrailingStopReplacementError,
)
from candlepilot.risk.engine import SymbolRules
from candlepilot.risk.trailing import TrailingStopCriticalError, TrailingStopManager
from candlepilot.storage.database import AuditRepository, Database


RULES = SymbolRules(
    quantity_step=Decimal("0.001"),
    min_quantity=Decimal("0.001"),
    min_notional=Decimal("5"),
    tick_size=Decimal("0.1"),
)


def _position(mark: str, stop: str = "98") -> TrailingPosition:
    return TrailingPosition(
        symbol="BTCUSDT",
        side="LONG",
        quantity=Decimal("1"),
        entry_price=Decimal("100"),
        mark_price=Decimal(mark),
        stop_loss=Decimal(stop),
    )


class Broker:
    def __init__(self) -> None:
        self.replacements: list[Decimal] = []
        self.failure: TrailingStopReplacementError | None = None

    async def replace_stop_loss(self, symbol, side, trigger):
        if self.failure is not None:
            raise self.failure
        self.replacements.append(trigger)
        return StopLossReplacement(symbol, Decimal("98"), trigger, "cp-entry-sl")


def test_shadow_mode_records_2r_activation_without_touching_broker(tmp_path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'shadow.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        broker = Broker()
        manager = TrailingStopManager(broker, audit, mode="shadow")
        await manager.maintain({"BTCUSDT": _position("104")}, {"BTCUSDT": RULES})
        events = await audit.recent_trailing_stop_events()
        state = await audit.get_runtime_state("trailing_stop_states_v1")
        await database.close()
        return broker, manager.status, events, state

    broker, status, events, state = asyncio.run(scenario())
    assert broker.replacements == []
    assert status["active_positions"] == 1
    assert status["active_strategies"] == 5
    assert len(status["strategies"]) == 5
    assert len(events) == 5
    assert {event["event"]["profile_id"] for event in events} == {
        "0.5R / 0.5R",
        "0.5R / 0.75R",
        "1R / 1R",
        "1.5R / 0.5R",
        "2R / 1R",
    }
    assert {
        event["event"]["candidate_stop"]
        for event in events
        if event["event"]["profile_id"] == "0.5R / 0.5R"
    } == {"103.0"}
    assert '"best_mark":"104"' in state


def test_live_mode_applies_only_the_deterministic_candidate(tmp_path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'live.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        broker = Broker()
        manager = TrailingStopManager(broker, audit, mode="live")
        await manager.maintain({"BTCUSDT": _position("104")}, {"BTCUSDT": RULES})
        events = await audit.recent_trailing_stop_events()
        await database.close()
        return broker.replacements, events

    replacements, events = asyncio.run(scenario())
    assert replacements == [Decimal("102.0")]
    assert len(events) == 1
    assert events[0]["status"] == "applied"
    assert events[0]["event"]["profile_id"] == "2R / 1R"


def test_shadow_profile_records_first_simulated_fill_and_freezes(tmp_path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'shadow-fill.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        manager = TrailingStopManager(Broker(), audit, mode="shadow")
        await manager.maintain({"BTCUSDT": _position("104")}, {"BTCUSDT": RULES})
        await manager.maintain({"BTCUSDT": _position("102.9")}, {"BTCUSDT": RULES})
        await manager.maintain({"BTCUSDT": _position("105")}, {"BTCUSDT": RULES})
        events = await audit.recent_trailing_stop_events(limit=100)
        state = await audit.get_runtime_state("trailing_stop_states_v1")
        status = manager.status
        await database.close()
        return events, state, status

    events, state, status = asyncio.run(scenario())
    fills = [event for event in events if event["status"] == "simulated_filled"]
    assert {event["event"]["profile_id"] for event in fills} == {
        "0.5R / 0.5R",
        "1.5R / 0.5R",
    }
    assert {event["event"]["simulated_fill_price"] for event in fills} == {"102.9"}
    assert status["simulated_fills"] == 2
    assert status["active_strategies"] == 3
    assert '"simulated_triggered":true' in state


def test_unrestorable_live_stop_failure_escalates_to_emergency(tmp_path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'critical.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        broker = Broker()
        broker.failure = TrailingStopReplacementError(
            "old stop could not be restored", requires_emergency_lock=True
        )
        manager = TrailingStopManager(broker, audit, mode="live")
        with pytest.raises(TrailingStopCriticalError):
            await manager.maintain(
                {"BTCUSDT": _position("104")}, {"BTCUSDT": RULES}
            )
        events = await audit.recent_trailing_stop_events()
        await database.close()
        return events

    assert asyncio.run(scenario())[0]["status"] == "failed"


def test_recoverable_live_stop_failure_retries_the_same_candidate(tmp_path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'recoverable.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        broker = Broker()
        broker.failure = TrailingStopReplacementError(
            "old stop was restored", requires_emergency_lock=False
        )
        manager = TrailingStopManager(broker, audit, mode="live")
        errors = await manager.maintain(
            {"BTCUSDT": _position("104")}, {"BTCUSDT": RULES}
        )
        broker.failure = None
        retry_errors = await manager.maintain(
            {"BTCUSDT": _position("104")}, {"BTCUSDT": RULES}
        )
        events = await audit.recent_trailing_stop_events()
        await database.close()
        return errors, retry_errors, broker.replacements, events

    errors, retry_errors, replacements, events = asyncio.run(scenario())
    assert errors == [
        "BTCUSDT: TrailingStopReplacementError: old stop was restored"
    ]
    assert retry_errors == []
    assert replacements == [Decimal("102.0")]
    assert [event["status"] for event in events] == ["applied", "failed"]


def test_switching_from_shadow_to_live_applies_the_existing_candidate(tmp_path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'mode-switch.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        shadow_broker = Broker()
        shadow = TrailingStopManager(shadow_broker, audit, mode="shadow")
        await shadow.maintain({"BTCUSDT": _position("104")}, {"BTCUSDT": RULES})

        live_broker = Broker()
        live = TrailingStopManager(live_broker, audit, mode="live")
        await live.maintain({"BTCUSDT": _position("104")}, {"BTCUSDT": RULES})
        await database.close()
        return live_broker.replacements

    assert asyncio.run(scenario()) == [Decimal("102.0")]


def test_old_single_profile_state_is_migrated_without_losing_original_r(tmp_path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'old-state.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        await audit.set_runtime_state(
            "trailing_stop_states_v1",
            '{"BTCUSDT":{"side":"LONG","quantity":"1",'
            '"entry_price":"100","original_stop":"98",'
            '"risk_distance":"2","best_mark":"104","active":true,'
            '"last_candidate":"102"}}',
        )
        manager = TrailingStopManager(Broker(), audit, mode="shadow")
        await manager.maintain(
            {"BTCUSDT": _position("104", stop="102")}, {"BTCUSDT": RULES}
        )
        state = await audit.get_runtime_state("trailing_stop_states_v1")
        await database.close()
        return manager.status, state

    status, state = asyncio.run(scenario())
    assert len(status["strategies"]) == 5
    assert '"original_stop":"98"' in state
    assert '"profiles"' in state
