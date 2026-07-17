import asyncio
from datetime import UTC, datetime
from pathlib import Path

from candlepilot.application.testnet_feed import TestnetUserFeed
from candlepilot.broker.user_stream import UserStreamEvent
from candlepilot.storage.database import AuditRepository, Database


class FakeUserStream:
    reconnect_count = 0
    dropped_event_count = 0
    last_error = None

    async def events(self):
        if False:
            yield None

    async def stop(self) -> None:
        return None

    async def close(self) -> None:
        return None


def test_testnet_feed_persists_account_and_order_events(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'user-events.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        feed = TestnetUserFeed(FakeUserStream(), audit)  # type: ignore[arg-type]
        order_time = datetime(2026, 7, 14, 12, tzinfo=UTC)
        await feed.process(
            UserStreamEvent(
                event_type="ORDER_TRADE_UPDATE",
                event_time=order_time,
                transaction_time=order_time,
                symbol="BTCUSDT",
                payload={"e": "ORDER_TRADE_UPDATE", "o": {"X": "PARTIALLY_FILLED"}},
            )
        )
        await feed.process(
            UserStreamEvent(
                event_type="ACCOUNT_UPDATE",
                event_time=order_time,
                transaction_time=order_time,
                symbol=None,
                payload={"e": "ACCOUNT_UPDATE", "a": {"B": [{"a": "USDT"}]}},
            )
        )
        records = await audit.recent_user_events()
        await database.close()
        return feed, records

    feed, records = asyncio.run(scenario())
    assert feed.event_count == 2
    assert feed.last_event_at == datetime(2026, 7, 14, 12, tzinfo=UTC)
    assert [record["event_type"] for record in records] == [
        "ACCOUNT_UPDATE",
        "ORDER_TRADE_UPDATE",
    ]
    assert records[1]["payload"]["o"]["X"] == "PARTIALLY_FILLED"


def test_audit_failure_does_not_skip_the_safety_handler() -> None:
    handled: list[str] = []

    class FailingAudit:
        async def record_user_event(self, _event):
            raise RuntimeError("disk busy")

    async def handler(event: UserStreamEvent) -> None:
        handled.append(event.event_type)

    async def scenario():
        feed = TestnetUserFeed(
            FakeUserStream(), FailingAudit(), event_handler=handler  # type: ignore[arg-type]
        )
        now = datetime.now(UTC)
        await feed.process(UserStreamEvent("ORDER_TRADE_UPDATE", now, now, "BTCUSDT", {}))
        return feed.last_error

    error = asyncio.run(scenario())
    assert handled == ["ORDER_TRADE_UPDATE"]
    assert error == "user event audit failed: RuntimeError"


def test_handler_failure_terminates_the_feed_for_the_guard() -> None:
    class OneEventStream(FakeUserStream):
        async def events(self):
            now = datetime.now(UTC)
            yield UserStreamEvent("ORDER_TRADE_UPDATE", now, now, "BTCUSDT", {})

    class MemoryAudit:
        async def record_user_event(self, _event):
            return 1

    async def handler(_event: UserStreamEvent) -> None:
        raise RuntimeError("cancel failed")

    async def scenario():
        feed = TestnetUserFeed(
            OneEventStream(), MemoryAudit(), event_handler=handler  # type: ignore[arg-type]
        )
        feed.start()
        assert feed._task is not None
        await feed._task
        return feed.running, feed.last_error

    running, error = asyncio.run(scenario())
    assert running is False
    assert error is not None and "cancel failed" in error
