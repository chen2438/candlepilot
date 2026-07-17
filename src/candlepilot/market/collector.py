"""Records the order-book state that history does not keep.

Binance publishes no historical order book, so a backtest over downloaded
candles can never carry book_imbalance, basis_bps, open interest or a real
spread. The only way to have them for a past window is to have been recording
while it happened. That is all this does: no model calls, no orders, no
decisions -- it samples the same endpoints ``market_snapshot`` samples and
writes them down.

Captures land on 5-minute boundaries, which is every decision time the system
has: 15m and 30m boundaries are also 5m boundaries, so one cadence of sampling
covers all three.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from candlepilot.market.binance import BinancePublicClient
from candlepilot.market.features import FeaturePipeline
from candlepilot.provenance import MICROSTRUCTURE_SCHEMA_VERSION

#: Every decision time is a multiple of this, so sampling here covers 5m/15m/30m.
CAPTURE_INTERVAL_SECONDS = 300
MAX_COLLECTED_SYMBOLS = 8


@dataclass(frozen=True, slots=True)
class BookCapture:
    """One symbol's order-book state at one instant."""

    symbol: str
    captured_at: datetime
    bid: Decimal
    ask: Decimal
    mark_price: Decimal
    index_price: Decimal
    funding_rate: Decimal
    #: The raw 20-level book, kept whole: it is under a kilobyte and it is the
    #: thing that cannot be obtained any other way.
    depth: dict[str, list[list[str]]]
    #: The tape is summarised, not stored: 1000 trades is 54KB per capture and
    #: 97% of the volume. schema_version records which derivation produced these.
    trade_imbalance: float
    trade_seconds: float
    open_interest: Decimal
    schema_version: str = MICROSTRUCTURE_SCHEMA_VERSION

    def features(self) -> dict[str, float]:
        """Rebuild exactly the microstructure block a live snapshot carried."""

        derived = FeaturePipeline.microstructure(
            mark_price=self.mark_price,
            index_price=self.index_price,
            open_interest=self.open_interest,
            bids=self.depth["bids"],
            asks=self.depth["asks"],
            trades=[],
        )
        # The tape summary is restored from the capture; the rest is recomputed
        # from the raw book, so a formula change there is picked up for free.
        derived["recent_trade_imbalance"] = self.trade_imbalance
        derived["recent_trade_seconds"] = self.trade_seconds
        return derived


def aligned_capture_times(start: datetime, end: datetime) -> list[datetime]:
    """The capture instants a window needs, on 5-minute boundaries."""

    step = timedelta(seconds=CAPTURE_INTERVAL_SECONDS)
    first = start.replace(second=0, microsecond=0)
    first -= timedelta(minutes=first.minute % 5)
    if first < start:
        first += step
    times = []
    cursor = first
    while cursor <= end:
        times.append(cursor)
        cursor += step
    return times


def next_boundary(now: datetime) -> datetime:
    """The next 5-minute boundary strictly after ``now``."""

    step = timedelta(seconds=CAPTURE_INTERVAL_SECONDS)
    floor = now.replace(second=0, microsecond=0) - timedelta(minutes=now.minute % 5)
    return floor + step


async def capture(
    market: BinancePublicClient, symbol: str, *, now: datetime | None = None
) -> BookCapture:
    """Sample the endpoints that a live snapshot's microstructure comes from."""

    book, premium, depth, interest, trades = await asyncio.gather(
        market.book_ticker(symbol),
        market.premium_index(symbol),
        market.depth(symbol),
        market.open_interest(symbol),
        market.agg_trades(symbol),
    )
    summary = FeaturePipeline.microstructure(
        mark_price=Decimal(premium["markPrice"]),
        index_price=Decimal(premium["indexPrice"]),
        open_interest=Decimal(interest["openInterest"]),
        bids=depth["bids"],
        asks=depth["asks"],
        trades=trades,
    )
    return BookCapture(
        symbol=symbol,
        captured_at=now or datetime.now(UTC),
        bid=Decimal(book["bidPrice"]),
        ask=Decimal(book["askPrice"]),
        mark_price=Decimal(premium["markPrice"]),
        index_price=Decimal(premium["indexPrice"]),
        funding_rate=Decimal(premium["lastFundingRate"]),
        depth={"bids": depth["bids"], "asks": depth["asks"]},
        trade_imbalance=summary["recent_trade_imbalance"],
        trade_seconds=summary["recent_trade_seconds"],
        open_interest=Decimal(interest["openInterest"]),
    )


class BookCollector:
    """Samples a symbol pool on every 5-minute boundary until stopped.

    Deliberately independent of the engine: the data is most worth having when
    nothing is trading, and it touches no provider, so it never contends with a
    live run or a backtest for a model.
    """

    def __init__(
        self,
        market: BinancePublicClient,
        *,
        store: Callable[[list[BookCapture]], Awaitable[None]],
        sleeper: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._market = market
        self._store = store
        self._sleep = sleeper or asyncio.sleep
        self.symbols: tuple[str, ...] = ()
        self.running = False
        self.capture_count = 0
        self.error_count = 0
        self.last_capture_at: datetime | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self, symbols: list[str]) -> None:
        if self.running:
            raise RuntimeError("the collector is already running")
        chosen = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
        if not chosen or len(chosen) > MAX_COLLECTED_SYMBOLS:
            raise ValueError(f"choose between 1 and {MAX_COLLECTED_SYMBOLS} symbols")
        self.symbols = chosen
        self.running = True
        self._task = asyncio.create_task(self._loop(), name="candlepilot-collector")

    async def stop(self) -> None:
        self.running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _loop(self) -> None:
        while self.running:
            now = datetime.now(UTC)
            boundary = next_boundary(now)
            await self._sleep((boundary - now).total_seconds())
            if not self.running:
                return
            await self.capture_once(boundary)

    async def capture_once(self, boundary: datetime) -> list[BookCapture]:
        """Sample every symbol and store whatever succeeded.

        One symbol failing must not cost the others their capture: a gap in one
        series is a gap in one series, but skipping the batch would lose the
        whole boundary.
        """

        results = await asyncio.gather(
            *(capture(self._market, symbol, now=boundary) for symbol in self.symbols),
            return_exceptions=True,
        )
        captured: list[BookCapture] = []
        for symbol, result in zip(self.symbols, results, strict=True):
            if isinstance(result, BookCapture):
                captured.append(result)
            else:
                self.error_count += 1
                self.last_error = f"{symbol}: {result}"
        if captured:
            await self._store(captured)
            self.capture_count += len(captured)
            self.last_capture_at = boundary
        return captured

    def status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "symbols": list(self.symbols),
            "capture_count": self.capture_count,
            "error_count": self.error_count,
            "last_capture_at": self.last_capture_at.isoformat()
            if self.last_capture_at
            else None,
            "last_error": self.last_error,
            "interval_seconds": CAPTURE_INTERVAL_SECONDS,
        }
