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
