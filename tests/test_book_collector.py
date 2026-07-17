import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from candlepilot.market.collector import (
    CAPTURE_INTERVAL_SECONDS,
    BookCapture,
    BookCollector,
    aligned_capture_times,
    capture,
    next_boundary,
)
from candlepilot.provenance import MICROSTRUCTURE_SCHEMA_VERSION

BOUNDARY = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


class _Market:
    def __init__(self, *, broken: set[str] | None = None) -> None:
        self.broken = broken or set()
        self.calls: list[str] = []

    async def book_ticker(self, symbol):
        self._check(symbol)
        return {"bidPrice": "59999.9", "askPrice": "60000.1"}

    async def premium_index(self, symbol):
        self._check(symbol)
        return {"markPrice": "60000.5", "indexPrice": "60000.0", "lastFundingRate": "0.0001"}

    async def depth(self, symbol, limit=20):
        self._check(symbol)
        return {"bids": [["59999", "3"]], "asks": [["60001", "1"]]}

    async def open_interest(self, symbol):
        self._check(symbol)
        return {"openInterest": "1234.5"}

    async def agg_trades(self, symbol, limit=1000):
        self._check(symbol)
        return [
            {"p": "60000", "q": "2", "m": False, "T": 1_784_040_000_000},
            {"p": "60000", "q": "1", "m": True, "T": 1_784_040_060_000},
        ]

    def _check(self, symbol: str) -> None:
        self.calls.append(symbol)
        if symbol in self.broken:
            raise RuntimeError(f"{symbol} is unavailable")


def test_captures_land_on_the_boundaries_every_cadence_shares() -> None:
    """Every decision boundary from 15m through 4h is also a 5m boundary."""

    assert CAPTURE_INTERVAL_SECONDS == 300
    assert next_boundary(datetime(2026, 6, 1, 12, 3, 20, tzinfo=UTC)) == datetime(
        2026, 6, 1, 12, 5, tzinfo=UTC
    )
    # Exactly on a boundary still moves to the next one: this one is being
    # captured now, not scheduled again.
    assert next_boundary(BOUNDARY) == BOUNDARY + timedelta(minutes=5)


def test_required_capture_times_span_the_window() -> None:
    times = aligned_capture_times(BOUNDARY, BOUNDARY + timedelta(minutes=20))

    assert times[0] == BOUNDARY
    assert times[-1] == BOUNDARY + timedelta(minutes=20)
    assert len(times) == 5


def test_a_capture_records_what_history_cannot_give_back() -> None:
    result = asyncio.run(capture(_Market(), "BTCUSDT", now=BOUNDARY))  # type: ignore[arg-type]

    assert result.symbol == "BTCUSDT"
    assert result.bid == Decimal("59999.9")
    assert result.ask == Decimal("60000.1")
    assert result.open_interest == Decimal("1234.5")
    # The raw book is kept whole: it is under a kilobyte and cannot be
    # reconstructed from anything else.
    assert result.depth["bids"] == [["59999", "3"]]
    # The tape is summarised, since 1000 trades is 97% of the volume.
    assert result.trade_imbalance == pytest.approx(1 / 3)
    assert result.trade_seconds == 60.0
    assert result.schema_version == MICROSTRUCTURE_SCHEMA_VERSION


def test_a_capture_rebuilds_the_live_microstructure_block() -> None:
    """A recorded capture has to produce what the live snapshot carried."""

    recorded = asyncio.run(capture(_Market(), "BTCUSDT", now=BOUNDARY))  # type: ignore[arg-type]

    features = recorded.features()

    assert set(features) == {
        "basis_bps",
        "open_interest",
        "book_imbalance",
        "recent_trade_imbalance",
        "recent_trade_seconds",
    }
    # Recomputed from the stored raw book...
    assert features["book_imbalance"] == pytest.approx(0.5)
    # ...and restored from the stored summary.
    assert features["recent_trade_imbalance"] == pytest.approx(1 / 3)


def test_one_broken_symbol_does_not_cost_the_others_their_boundary() -> None:
    """A gap in one series beats losing the whole instant."""

    stored: list[list[BookCapture]] = []

    async def store(batch):
        stored.append(batch)

    collector = BookCollector(
        _Market(broken={"ETHUSDT"}),  # type: ignore[arg-type]
        store=store,
    )
    collector.symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT")

    captured = asyncio.run(collector.capture_once(BOUNDARY))

    assert [item.symbol for item in captured] == ["BTCUSDT", "SOLUSDT"]
    assert collector.error_count == 1
    assert collector.last_error is not None and "ETHUSDT" in collector.last_error
    assert collector.capture_count == 2


def test_the_collector_refuses_a_symbol_pool_it_cannot_keep_up_with() -> None:
    collector = BookCollector(_Market(), store=lambda _: asyncio.sleep(0))  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="1 and 8 symbols"):
        collector.start([f"SYM{index}USDT" for index in range(9)])
    with pytest.raises(ValueError, match="1 and 8 symbols"):
        collector.start([])


def test_store_failure_is_reported_and_retried_without_a_false_running_state() -> None:
    async def scenario():
        sleeps = 0
        retry_wait = asyncio.Event()
        blocker = asyncio.Event()

        async def sleeper(_seconds: float) -> None:
            nonlocal sleeps
            sleeps += 1
            if sleeps > 1:
                retry_wait.set()
                await blocker.wait()

        async def failing_store(_batch) -> None:
            raise OSError("disk unavailable")

        collector = BookCollector(
            _Market(),  # type: ignore[arg-type]
            store=failing_store,
            sleeper=sleeper,
        )
        collector.start(["BTCUSDT"])
        await asyncio.wait_for(retry_wait.wait(), timeout=1)
        status_during_retry = collector.status()
        await collector.stop()
        return status_during_retry, collector.status()

    during, stopped = asyncio.run(scenario())
    assert during["running"] is True
    assert during["error_count"] == 1
    assert "OSError" in during["last_error"]
    assert stopped["running"] is False


def test_successful_boundary_clears_an_old_collector_error() -> None:
    async def scenario():
        collector = BookCollector(
            _Market(),  # type: ignore[arg-type]
            store=lambda _batch: asyncio.sleep(0),
        )
        collector.symbols = ("BTCUSDT",)
        collector.last_error = "old failure"
        await collector.capture_once(BOUNDARY)
        return collector.last_error

    assert asyncio.run(scenario()) is None


def test_the_collector_needs_no_provider_and_places_no_orders() -> None:
    """The data is worth most when nothing is trading, so it must stand alone."""

    import inspect

    from candlepilot.market import collector as module

    source = inspect.getsource(module)
    for forbidden in ("generate_trade_intent", "execute_with_stop", "ProviderRegistry"):
        assert forbidden not in source
