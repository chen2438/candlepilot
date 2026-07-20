"""Rebuild the payload the live system sends, from history alone.

The old backtest handed the model 15 unprefixed single-timeframe features while
the shared prompt named the decision feature ladder, daily levels and order flow. It was
scoring a strategy nobody runs. This module assembles the same ladder live
assembles -- and is explicit about the one part of it that history cannot
supply.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from candlepilot.backtest.engine import Candle
from candlepilot.domain.models import MarketSnapshot
from candlepilot.market.features import (
    DAILY_STRUCTURE_INTERVAL,
    DAILY_STRUCTURE_PERIOD,
    DECISION_FEATURE_INTERVALS,
    FeaturePipeline,
)

INTERVAL_MILLISECONDS: dict[str, int] = {
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

#: Fields that only a recorded book can supply.
#:
#: Binance publishes no order-book history, so downloaded candles can never
#: carry these. They are absent from a plain backtest -- and the prompt is told
#: so rather than being fed a substitute -- but present in a real backtest,
#: where the collector wrote the book down as it happened.
FLOW_FEATURES = (
    "book_imbalance",
    "recent_trade_imbalance",
    "recent_trade_seconds",
    "basis_bps",
    "open_interest",
)

# Warm-up the ladder needs before the first decision: enough closed bars on the
# slowest intraday interval, and enough daily bars for the 20-day range.
_INTRADAY_WARMUP_BARS = 200


def required_history_start(start: datetime, cadence: str) -> datetime:
    """How far before the window the candles must reach.

    The first decision needs the same warm-up every later one gets, otherwise
    it is made on a shorter EMA and a narrower range than the rest of the run.
    """

    slowest = max(INTERVAL_MILLISECONDS[item] for item in DECISION_FEATURE_INTERVALS)
    intraday = timedelta(milliseconds=slowest * _INTRADAY_WARMUP_BARS)
    daily = timedelta(days=DAILY_STRUCTURE_PERIOD + 5)
    return start - max(intraday, daily)


def coverage(required: list[datetime], recorded: set[datetime]) -> Coverage:
    """Which decision instants have a recorded book behind them.

    A partially covered window is the trap worth refusing: half the decisions
    would see order flow and half would not, which is two different strategies
    averaged into one number that does not mention it.
    """

    missing = tuple(when for when in required if when not in recorded)
    return Coverage(
        required=len(required), recorded=len(required) - len(missing), missing=missing
    )


def _rows(candles: list[Candle]) -> list[list[Any]]:
    """Render candles as Binance kline rows, all marked closed."""

    return [
        [
            int(candle.timestamp.timestamp() * 1000),
            str(candle.open),
            str(candle.high),
            str(candle.low),
            str(candle.close),
            str(candle.volume),
            int(candle.timestamp.timestamp() * 1000) + 1,
            str(candle.volume * candle.close),
        ]
        for candle in candles
    ]


@dataclass(frozen=True, slots=True)
class Coverage:
    """How much of a window the recorded book actually covers."""

    required: int
    recorded: int
    missing: tuple[datetime, ...]

    @property
    def complete(self) -> bool:
        return not self.missing

    @property
    def fraction(self) -> float:
        return self.recorded / self.required if self.required else 0.0


class HistoricalSnapshotBuilder:
    """Assembles a decision snapshot as of a point in time, with no lookahead.

    Every series is truncated to bars that had already closed when the decision
    was due. A bar is only usable once its close time has passed, so the cutoff
    is the open of the bar being decided on.

    With ``captures`` supplied, the snapshot also carries the order-book state
    that was recorded at that instant, making the payload identical to what live
    sent. Without them the flow fields are simply absent, and the prompt says so.
    """

    def __init__(
        self,
        series: dict[str, list[Candle]],
        captures: dict[datetime, dict[str, Any]] | None = None,
    ) -> None:
        missing = set(DECISION_FEATURE_INTERVALS) - set(series)
        if missing:
            raise ValueError(
                f"historical snapshots need {', '.join(sorted(missing))} candles"
            )
        if DAILY_STRUCTURE_INTERVAL not in series:
            raise ValueError("historical snapshots need 1d candles for the daily levels")
        self._series = series
        self._timestamps = {
            interval: [candle.timestamp for candle in candles]
            for interval, candles in series.items()
        }
        self._closed_at = {
            interval: [
                candle.timestamp
                + timedelta(milliseconds=INTERVAL_MILLISECONDS[interval])
                for candle in candles
            ]
            for interval, candles in series.items()
        }
        quote_prefix = [Decimal("0")]
        for candle in series["30m"]:
            quote_prefix.append(quote_prefix[-1] + candle.volume * candle.close)
        self._quote_volume_prefix = quote_prefix
        self._captures = captures or {}
        self._pipeline = FeaturePipeline()

    def _closed_count(self, interval: str, cutoff: datetime) -> int:
        return bisect_right(self._closed_at[interval], cutoff)

    def build(self, symbol: str, cadence: str, decided_at: datetime) -> MarketSnapshot:
        features: dict[str, float] = {}
        rows_by_interval: dict[str, list[list[Any]]] = {}
        for interval in DECISION_FEATURE_INTERVALS:
            closed_count = self._closed_count(interval, decided_at)
            if closed_count < 20:
                raise ValueError(
                    f"{symbol} has only {closed_count} closed {interval} candles before "
                    f"{decided_at.isoformat()}; the window needs more history"
                )
            start = max(0, closed_count - _INTRADAY_WARMUP_BARS)
            rows_by_interval[interval] = _rows(
                self._series[interval][start:closed_count]
            )
        features.update(self._pipeline.multitimeframe(rows_by_interval))

        mark = Decimal(rows_by_interval[cadence][-1][4])
        daily_count = self._closed_count(DAILY_STRUCTURE_INTERVAL, decided_at)
        if daily_count < DAILY_STRUCTURE_PERIOD:
            raise ValueError(
                f"{symbol} has only {daily_count} closed daily candles before "
                f"{decided_at.isoformat()}; the daily levels need "
                f"{DAILY_STRUCTURE_PERIOD}"
            )
        daily = self._series[DAILY_STRUCTURE_INTERVAL][
            daily_count - DAILY_STRUCTURE_PERIOD : daily_count
        ]
        features.update(self._pipeline.daily_structure(_rows(daily), mark_price=mark))

        recorded = self._captures.get(decided_at)
        if recorded is None:
            # No book was recorded here, so there is no spread to model either.
            # Quoting the mark on both sides says the spread is unknown rather
            # than inventing a favourable one.
            return MarketSnapshot(
                symbol=symbol,
                cadence=cadence,  # type: ignore[arg-type]
                timestamp=decided_at,
                mark_price=mark,
                bid=mark,
                ask=mark,
                quote_volume_24h=self._quote_volume_24h(decided_at),
                funding_rate=self._funding_rate(cadence, decided_at),
                features=features,
            )

        # A recorded book makes this payload identical to the live one: real
        # spread, real mark, and the flow fields history cannot otherwise give.
        features.update(recorded["features"])
        return MarketSnapshot(
            symbol=symbol,
            cadence=cadence,  # type: ignore[arg-type]
            timestamp=decided_at,
            mark_price=recorded["mark_price"],
            bid=recorded["bid"],
            ask=recorded["ask"],
            quote_volume_24h=self._quote_volume_24h(decided_at),
            funding_rate=recorded["funding_rate"],
            features=features,
        )

    def _quote_volume_24h(self, cutoff: datetime) -> Decimal:
        window = cutoff - timedelta(hours=24)
        timestamps = self._timestamps["30m"]
        start = bisect_left(timestamps, window)
        end = bisect_left(timestamps, cutoff)
        return self._quote_volume_prefix[end] - self._quote_volume_prefix[start]

    def _funding_rate(self, cadence: str, cutoff: datetime) -> Decimal:
        closed_count = self._closed_count(cadence, cutoff)
        if not closed_count:
            return Decimal("0")
        return self._series[cadence][closed_count - 1].funding_rate


def utc(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
