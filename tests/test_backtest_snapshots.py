from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from candlepilot.backtest.engine import Candle
from candlepilot.backtest.snapshots import (
    INTERVAL_MILLISECONDS,
    HistoricalSnapshotBuilder,
    required_history_start,
)
from candlepilot.market.features import DECISION_FEATURE_INTERVALS

START = datetime(2026, 6, 1, tzinfo=UTC)


def _series(interval: str, count: int, *, drift: str = "0.001") -> list[Candle]:
    """Bars ending at START, the way real history reaches back from a window."""

    step = timedelta(milliseconds=INTERVAL_MILLISECONDS[interval])
    candles = []
    price = Decimal("100")
    for index in range(count):
        price *= Decimal("1") + Decimal(drift)
        candles.append(
            Candle(
                timestamp=START - step * (count - index),
                open=price,
                high=price * Decimal("1.002"),
                low=price * Decimal("0.998"),
                close=price,
                volume=Decimal("100"),
            )
        )
    return candles


def _builder(count: int = 260) -> HistoricalSnapshotBuilder:
    series = {interval: _series(interval, count) for interval in DECISION_FEATURE_INTERVALS}
    series["1d"] = _series("1d", 40)
    return HistoricalSnapshotBuilder(series)


def _cutoff(interval: str, bars: int) -> datetime:
    """A decision time `bars` before START, inside the available history."""

    return START - timedelta(milliseconds=INTERVAL_MILLISECONDS[interval] * bars)


def test_snapshot_carries_the_same_ladder_the_live_system_sends() -> None:
    """The old backtest sent 15 unprefixed single-timeframe features.

    The prompt names the complete 5m through 4h ladder and daily levels, so that payload scored
    a strategy nobody runs. A historical snapshot has to carry what live sends,
    minus only what history genuinely lacks.
    """

    snapshot = _builder().build("BTCUSDT", "5m", _cutoff("5m", 5))

    for interval in DECISION_FEATURE_INTERVALS:
        assert f"{interval}_ema_spread" in snapshot.features
        assert f"{interval}_range_position_50" in snapshot.features
        assert f"{interval}_ema20_distance_atr" in snapshot.features
    assert snapshot.features["1d_range_high_20"] > 0
    assert "1d_range_position_20" in snapshot.features
    # No unprefixed leftovers, exactly as live.
    assert "ema_spread" not in snapshot.features


def test_snapshot_omits_the_flow_fields_history_cannot_supply() -> None:
    """Binance publishes no historical order book, so these cannot be faked."""

    snapshot = _builder().build("BTCUSDT", "5m", _cutoff("5m", 5))

    for name in ("book_imbalance", "recent_trade_imbalance", "basis_bps", "open_interest"):
        assert name not in snapshot.features


def test_snapshot_never_reads_a_bar_that_had_not_closed() -> None:
    """Lookahead is the one bug that makes every other number meaningless."""

    builder = _builder()
    cutoff = _cutoff("5m", 100)

    snapshot = builder.build("BTCUSDT", "5m", cutoff)

    # The mark is the close of the last bar that had finished, and _series
    # rises monotonically, so any future bar would show a higher price.
    everything = _series("5m", 260)
    closed = [c for c in everything if c.timestamp + timedelta(minutes=5) <= cutoff]
    assert snapshot.mark_price == closed[-1].close
    assert snapshot.mark_price < everything[-1].close


def test_snapshot_build_does_not_rescan_complete_history() -> None:
    class ScanGuard(list[Candle]):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    guarded = {
        interval: ScanGuard(_series(interval, 260))
        for interval in DECISION_FEATURE_INTERVALS
    }
    guarded["1d"] = ScanGuard(_series("1d", 40))
    builder = HistoricalSnapshotBuilder(guarded)
    for candles in guarded.values():
        candles.iterations = 0

    builder.build("BTCUSDT", "5m", _cutoff("5m", 5))

    assert all(candles.iterations == 0 for candles in guarded.values())


def test_snapshot_quotes_no_spread_it_cannot_know() -> None:
    """History has no book, so an invented spread would flatter every fill."""

    snapshot = _builder().build("BTCUSDT", "5m", _cutoff("5m", 5))

    assert snapshot.bid == snapshot.mark_price
    assert snapshot.ask == snapshot.mark_price


def test_a_window_without_enough_history_is_refused_not_approximated() -> None:
    """A short EMA and a narrow range are not the strategy under test."""

    builder = _builder()

    with pytest.raises(ValueError, match="closed 5m candles"):
        builder.build("BTCUSDT", "5m", _cutoff("5m", 255))


def test_daily_levels_are_refused_when_the_symbol_is_too_young() -> None:
    series = {interval: _series(interval, 260) for interval in DECISION_FEATURE_INTERVALS}
    series["1d"] = _series("1d", 8)
    builder = HistoricalSnapshotBuilder(series)

    with pytest.raises(ValueError, match="closed daily candles"):
        builder.build("BTCUSDT", "5m", _cutoff("5m", 5))


def test_warmup_reaches_back_far_enough_for_the_first_decision() -> None:
    """The first decision must get the same history every later one gets."""

    start = datetime(2026, 6, 10, tzinfo=UTC)

    reach = required_history_start(start, "5m")

    # Two hundred 4h closes need more than 33 days, longer than the daily range.
    assert reach == start - timedelta(hours=4 * 200)


def test_builder_refuses_a_series_missing_an_interval_the_rules_read() -> None:
    series = {interval: _series(interval, 260) for interval in DECISION_FEATURE_INTERVALS}

    with pytest.raises(ValueError, match="1d candles"):
        HistoricalSnapshotBuilder(series)


def test_historical_snapshot_declares_the_missing_order_book() -> None:
    """Public history cannot silently pretend that a live book was present."""

    when = _cutoff("5m", 6)
    series = {interval: _series(interval, 260) for interval in DECISION_FEATURE_INTERVALS}
    series["1d"] = _series("1d", 40)
    builder = HistoricalSnapshotBuilder(series)

    gap = builder.build("BTCUSDT", "5m", when)

    assert "book_imbalance" not in gap.features
    assert gap.bid == gap.ask == gap.mark_price
