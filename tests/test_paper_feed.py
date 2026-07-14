import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from candlepilot.application.paper_feed import PaperMarketFeed
from candlepilot.domain.models import MarketSnapshot, OrderPlan, OrderType
from candlepilot.execution.paper import PaperExecutor
from candlepilot.market.stream import MarketStreamEvent
from candlepilot.storage.database import AuditRepository, Database


def _event(event_type: str, payload: dict) -> MarketStreamEvent:
    values = {"e": event_type, "E": 1767225600000, "s": "BTCUSDT", **payload}
    return MarketStreamEvent(
        stream="fixture",
        event_type=event_type,
        symbol="BTCUSDT",
        event_time=datetime(2026, 1, 1, tzinfo=UTC),
        payload=values,
    )


def test_book_and_mark_events_drive_paper_stop(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'feed.db'}")
        await database.initialize()
        executor = PaperExecutor(slippage_fraction=Decimal("0"))
        audit = AuditRepository(database.sessions)
        feed = PaperMarketFeed(executor, audit)
        entry_snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            cadence="1m",
            timestamp=datetime.now(UTC),
            mark_price="100",
            bid="99.9",
            ask="100.1",
            quote_volume_24h="1000000",
        )
        await executor.execute(
            OrderPlan(
                client_order_id="feed-entry",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("1"),
                order_type=OrderType.MARKET,
                stop_price=Decimal("98"),
            ),
            entry_snapshot,
        )
        await feed.process(_event("bookTicker", {"b": "97.8", "a": "98"}))
        await feed.process(_event("markPriceUpdate", {"p": "97.9"}))
        state = executor.portfolio_state()
        await database.close()
        return feed, state

    feed, state = asyncio.run(scenario())
    assert feed.event_count == 2
    assert state.open_positions == 0


def test_unsupported_stream_event_is_ignored(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'ignored.db'}")
        await database.initialize()
        feed = PaperMarketFeed(PaperExecutor(), AuditRepository(database.sessions))
        await feed.process(_event("forceOrder", {}))
        await database.close()
        return feed.event_count

    assert asyncio.run(scenario()) == 0


def test_rest_backfill_updates_protection_before_live_resume(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'backfill.db'}")
        await database.initialize()
        executor = PaperExecutor(slippage_fraction=Decimal("0"))
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            cadence="1m",
            timestamp=datetime.now(UTC),
            mark_price="100",
            bid="99.9",
            ask="100.1",
            quote_volume_24h="1000000",
        )
        await executor.execute(
            OrderPlan(
                client_order_id="backfill-entry",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("1"),
                order_type=OrderType.MARKET,
                stop_price=Decimal("98"),
            ),
            snapshot,
        )

        async def loader(_):
            return [
                snapshot.model_copy(
                    update={
                        "mark_price": Decimal("97"),
                        "bid": Decimal("96.9"),
                        "ask": Decimal("97.1"),
                    }
                )
            ]

        feed = PaperMarketFeed(
            executor,
            AuditRepository(database.sessions),
            backfill_loader=loader,
        )
        feed.symbols = ("BTCUSDT",)
        await feed.backfill()
        state = executor.portfolio_state()
        await database.close()
        return feed, state

    feed, state = asyncio.run(scenario())
    assert feed.backfill_count == 1
    assert feed.last_backfill_at is not None
    assert state.open_positions == 0
