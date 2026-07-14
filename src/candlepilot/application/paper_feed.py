from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from candlepilot.domain.models import MarketSnapshot
from candlepilot.execution.paper import PaperExecutor
from candlepilot.market.stream import BinanceMarketStream, MarketStreamEvent
from candlepilot.storage.database import AuditRepository


@dataclass(slots=True)
class _LiveQuote:
    mark: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    quote_volume: Decimal = Decimal("0")


StreamFactory = Callable[[list[str]], BinanceMarketStream]
BackfillLoader = Callable[[list[str]], Awaitable[list[MarketSnapshot]]]


class PaperMarketFeed:
    """Drive paper fills and protection from Binance public market events."""

    def __init__(
        self,
        executor: PaperExecutor,
        audit: AuditRepository,
        *,
        stream_factory: StreamFactory = BinanceMarketStream,
        backfill_loader: BackfillLoader | None = None,
    ) -> None:
        self.executor = executor
        self.audit = audit
        self.stream_factory = stream_factory
        self.backfill_loader = backfill_loader
        self.symbols: tuple[str, ...] = ()
        self.event_count = 0
        self.last_error: str | None = None
        self.backfill_count = 0
        self.last_backfill_at: datetime | None = None
        self._quotes: dict[str, _LiveQuote] = {}
        self._stream: BinanceMarketStream | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, symbols: list[str]) -> None:
        normalized = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
        if not normalized:
            await self.stop()
            return
        if self.running and normalized == self.symbols:
            return
        await self.stop()
        self.symbols = normalized
        self._stream = self.stream_factory(list(normalized))
        self._task = asyncio.create_task(self._run(), name="candlepilot-paper-market-feed")

    async def stop(self) -> None:
        if self._stream is not None:
            await self._stream.stop()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        self._stream = None
        self.symbols = ()

    async def _run(self) -> None:
        assert self._stream is not None
        seen_reconnects = 0
        try:
            async for event in self._stream.events():
                if self._stream.reconnect_count > seen_reconnects:
                    await self.backfill()
                    seen_reconnects = self._stream.reconnect_count
                await self.process(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)

    async def backfill(self) -> None:
        if self.backfill_loader is None or not self.symbols:
            return
        try:
            snapshots = await self.backfill_loader(list(self.symbols))
            for snapshot in snapshots:
                reports = await self.executor.mark_to_market(snapshot)
                for report in reports:
                    await self.audit.record_execution(snapshot.symbol, report)
            self.backfill_count += 1
            self.last_backfill_at = max(
                (snapshot.timestamp for snapshot in snapshots), default=self.last_backfill_at
            )
            self.last_error = None
        except Exception as exc:
            self.last_error = f"REST backfill failed: {exc}"

    async def process(self, event: MarketStreamEvent) -> None:
        quote = self._quotes.setdefault(event.symbol, _LiveQuote())
        payload = event.payload
        if event.event_type == "markPriceUpdate":
            quote.mark = Decimal(str(payload["p"]))
        elif event.event_type == "bookTicker":
            quote.bid = Decimal(str(payload["b"]))
            quote.ask = Decimal(str(payload["a"]))
        elif event.event_type == "24hrTicker":
            quote.quote_volume = Decimal(str(payload.get("q", "0")))
        elif event.event_type == "kline":
            quote.mark = Decimal(str(payload["k"]["c"]))
        else:
            return
        self.event_count += 1
        if quote.bid is None or quote.ask is None:
            return
        mark = quote.mark or ((quote.bid + quote.ask) / 2)
        snapshot = MarketSnapshot(
            symbol=event.symbol,
            cadence="1m",
            timestamp=event.event_time,
            mark_price=mark,
            bid=quote.bid,
            ask=quote.ask,
            quote_volume_24h=quote.quote_volume,
        )
        reports = await self.executor.mark_to_market(snapshot)
        for report in reports:
            await self.audit.record_execution(event.symbol, report)
