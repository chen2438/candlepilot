"""Rebuild the payload the live system sends, from history alone.

The old backtest handed the model 15 unprefixed single-timeframe features while
the shared prompt named 5m/15m/30m fields, daily levels and order flow. It was
scoring a strategy nobody runs. This module assembles the same ladder live
assembles -- and is explicit about the one part of it that history cannot
supply.
"""

from __future__ import annotations

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
    "1d": 86_400_000,
}

#: Live-only fields, and why each one cannot be reconstructed.
#:
#: Binance publishes no historical order book, so book_imbalance is gone for
#: good. basis_bps needs the index price series, open interest is retained for
#: 30 days, and the trade tape would be a separate paginated fetch per decision.
#: The prompt is told these are absent rather than being fed a plausible
#: substitute: every model in a comparison faces the same gap, so the ranking
#: still means something, but a fabricated book would not.
ABSENT_LIVE_FEATURES = (
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


class HistoricalSnapshotBuilder:
    """Assembles a decision snapshot as of a point in time, with no lookahead.

    Every series is truncated to bars that had already closed when the decision
    was due. A bar is only usable once its close time has passed, so the cutoff
    is the open of the bar being decided on.
    """

    def __init__(self, series: dict[str, list[Candle]]) -> None:
        missing = set(DECISION_FEATURE_INTERVALS) - set(series)
        if missing:
            raise ValueError(
                f"historical snapshots need {', '.join(sorted(missing))} candles"
            )
        if DAILY_STRUCTURE_INTERVAL not in series:
            raise ValueError("historical snapshots need 1d candles for the daily levels")
        self._series = series
        self._pipeline = FeaturePipeline()

    def _closed_before(self, interval: str, cutoff: datetime) -> list[Candle]:
        span = timedelta(milliseconds=INTERVAL_MILLISECONDS[interval])
        return [candle for candle in self._series[interval] if candle.timestamp + span <= cutoff]

    def build(self, symbol: str, cadence: str, decided_at: datetime) -> MarketSnapshot:
        features: dict[str, float] = {}
        rows_by_interval: dict[str, list[list[Any]]] = {}
        for interval in DECISION_FEATURE_INTERVALS:
            closed = self._closed_before(interval, decided_at)
            if len(closed) < 20:
                raise ValueError(
                    f"{symbol} has only {len(closed)} closed {interval} candles before "
                    f"{decided_at.isoformat()}; the window needs more history"
                )
            rows_by_interval[interval] = _rows(closed[-_INTRADAY_WARMUP_BARS:])
        features.update(self._pipeline.multitimeframe(rows_by_interval))

        mark = Decimal(rows_by_interval[cadence][-1][4])
        daily = self._closed_before(DAILY_STRUCTURE_INTERVAL, decided_at)
        if len(daily) < DAILY_STRUCTURE_PERIOD:
            raise ValueError(
                f"{symbol} has only {len(daily)} closed daily candles before "
                f"{decided_at.isoformat()}; the daily levels need "
                f"{DAILY_STRUCTURE_PERIOD}"
            )
        features.update(self._pipeline.daily_structure(_rows(daily), mark_price=mark))

        return MarketSnapshot(
            symbol=symbol,
            cadence=cadence,  # type: ignore[arg-type]
            timestamp=decided_at,
            mark_price=mark,
            # History carries no book, so there is no spread to model. Quoting
            # the mark on both sides is the honest version of that: it says the
            # spread is unknown rather than inventing a favourable one.
            bid=mark,
            ask=mark,
            quote_volume_24h=self._quote_volume_24h(decided_at),
            funding_rate=self._funding_rate(cadence, decided_at),
            features=features,
        )

    def _quote_volume_24h(self, cutoff: datetime) -> Decimal:
        window = cutoff - timedelta(hours=24)
        return sum(
            (
                candle.volume * candle.close
                for candle in self._series["30m"]
                if window <= candle.timestamp < cutoff
            ),
            Decimal("0"),
        )

    def _funding_rate(self, cadence: str, cutoff: datetime) -> Decimal:
        closed = self._closed_before(cadence, cutoff)
        return closed[-1].funding_rate if closed else Decimal("0")


def utc(milliseconds: int) -> datetime:
    return datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
