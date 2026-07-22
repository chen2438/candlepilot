from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from candlepilot.market.features import (
    DECISION_FEATURE_INTERVALS,
    FeaturePipeline,
    _ema,
)


def _rows(count: int = 60) -> list[list[object]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for index in range(count):
        price = Decimal("100") + Decimal(index) / 10
        open_ms = int((start + timedelta(minutes=index)).timestamp() * 1000)
        close_ms = open_ms + 59_999
        rows.append(
            [
                open_ms,
                str(price),
                str(price + 1),
                str(price - 1),
                str(price + Decimal("0.2")),
                "10",
                close_ms,
                str(1000 + index),
            ]
        )
    return rows


def test_feature_pipeline_produces_finite_multiscale_features() -> None:
    features = FeaturePipeline().calculate(_rows())
    assert set(features) == {
        "range_high_20",
        "range_low_20",
        "range_high_50",
        "range_low_50",
        "range_position_50",
        "prior_range_high_20",
        "prior_range_low_20",
        "breakout_above_20",
        "breakdown_below_20",
        "breakout_hold_high_20",
        "breakout_hold_low_20",
        "breakout_hold_above_20",
        "breakdown_hold_below_20",
        "last_swing_high",
        "last_swing_high_confirmed",
        "bars_since_swing_high",
        "last_swing_low",
        "last_swing_low_confirmed",
        "bars_since_swing_low",
        "last_bar_close_position",
        "ema20_distance_atr",
        "return_1",
        "return_5",
        "ema_20",
        "ema_50",
        "ema_spread",
        "rsi_14",
        "atr_14",
        "atr_fraction",
        "quote_volume_ratio",
    }
    assert features["ema_20"] > features["ema_50"]
    assert 50 < features["rsi_14"] <= 100
    assert features["atr_fraction"] > 0
    assert features["last_bar_close_position"] > 0.5
    assert features["prior_range_high_20"] < features["range_high_20"]


def test_snapshot_contains_exchange_and_derived_data() -> None:
    snapshot = FeaturePipeline().snapshot(
        symbol="BTCUSDT",
        cadence="5m",
        features=FeaturePipeline().calculate(_rows()),
        mark_price=Decimal("106"),
        bid=Decimal("105.9"),
        ask=Decimal("106.1"),
        quote_volume_24h=Decimal("1000000"),
        funding_rate=Decimal("0.0001"),
    )
    assert snapshot.features["rsi_14"] > 50
    assert snapshot.funding_rate == Decimal("0.0001")


def test_microstructure_features_capture_direction_and_basis() -> None:
    features = FeaturePipeline.microstructure(
        mark_price=Decimal("101"),
        index_price=Decimal("100"),
        open_interest=Decimal("1234.5"),
        bids=[["100", "8"], ["99", "2"]],
        asks=[["101", "4"], ["102", "1"]],
        trades=[
            {"p": "100", "q": "2", "m": False},
            {"p": "100", "q": "1", "m": True},
        ],
    )

    assert features["basis_bps"] == 100.0
    assert features["open_interest"] == 1234.5
    assert features["book_imbalance"] == 1 / 3
    assert features["recent_trade_imbalance"] == 1 / 3


def test_derivatives_positioning_uses_latest_closed_pair_and_omits_missing() -> None:
    features = FeaturePipeline.derivatives_positioning(
        open_interest_history=[
            {"sumOpenInterest": "100", "timestamp": 1},
            {"sumOpenInterest": "103", "timestamp": 2},
        ],
        global_long_short_history=[
            {"longShortRatio": "1.20", "timestamp": 1},
            {"longShortRatio": "1.15", "timestamp": 2},
        ],
        top_position_history=[
            {"longShortRatio": "1.50", "timestamp": 1},
            {"longShortRatio": "1.55", "timestamp": 2},
        ],
        taker_history=[
            {"buySellRatio": "0.8", "timestamp": 1},
            {"buySellRatio": "1.1", "timestamp": 2},
        ],
    )

    assert features["open_interest_change_5m"] == pytest.approx(0.03)
    assert features["global_long_short_ratio"] == 1.15
    assert features["global_long_short_ratio_change_5m"] == pytest.approx(-0.05)
    assert features["top_long_short_position_ratio"] == 1.55
    assert features["top_long_short_position_ratio_change_5m"] == pytest.approx(0.05)
    assert features["taker_buy_sell_ratio"] == 1.1
    assert features["taker_buy_sell_ratio_change_5m"] == pytest.approx(0.3)

    missing = FeaturePipeline.derivatives_positioning(
        open_interest_history=[{"sumOpenInterest": "100", "timestamp": 1}],
        global_long_short_history=[],
        top_position_history=[],
        taker_history=[],
    )
    assert missing == {}


def test_multitimeframe_features_are_namespaced() -> None:
    rows_by_interval = {interval: _rows() for interval in DECISION_FEATURE_INTERVALS}
    features = FeaturePipeline().multitimeframe(rows_by_interval)

    per_interval = len(FeaturePipeline().calculate(_rows()))
    assert len(features) == len(DECISION_FEATURE_INTERVALS) * per_interval
    assert features["15m_ema_spread"] == features["5m_ema_spread"]
    assert "15m_rsi_14" in features
    assert "30m_rsi_14" in features
    assert "1h_rsi_14" in features
    assert "4h_rsi_14" in features
    # 1m is not a decision interval: no setup rule reads it, so it is not sent.
    assert not [name for name in features if name.startswith("1m_")]


def test_multitimeframe_rejects_an_interval_set_the_rules_do_not_cover() -> None:
    pipeline = FeaturePipeline()
    rows_by_interval = {interval: _rows() for interval in DECISION_FEATURE_INTERVALS}

    with pytest.raises(ValueError, match="5m, 15m, 30m, 1h, 4h"):
        pipeline.multitimeframe({**rows_by_interval, "1m": _rows()})


def test_structure_features_locate_price_against_its_recent_range() -> None:
    """The setup rules ask whether price is extended or near a reference.

    Moving averages cannot answer that, so the range levels themselves are
    what make the prompt's structure conditions decidable rather than guessed.
    """

    rising = FeaturePipeline().calculate(_rows())
    # _rows climbs throughout, so the close sits near the top of its range --
    # not at 1.0, because the range is drawn from wicks and the last close sits
    # below its own bar's high.
    assert 0.85 < rising["range_position_50"] < 1
    assert rising["range_low_50"] < rising["range_low_20"]
    assert rising["range_high_20"] == rising["range_high_50"]

    flat = FeaturePipeline().calculate(_flat_rows())
    assert flat["range_high_50"] == flat["range_high_20"]
    # A range with no span reports the midpoint rather than dividing by zero.
    assert flat["range_position_50"] == 0.5


def _flat_rows(count: int = 60) -> list[list[object]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for index in range(count):
        open_ms = int((start + timedelta(minutes=index)).timestamp() * 1000)
        rows.append([open_ms, "100", "100", "100", "100", "10", open_ms + 59_999, "1000"])
    return rows


def test_trade_imbalance_reports_the_window_it_covers() -> None:
    """A fixed trade count spans a variable amount of time.

    The same imbalance means very different things over four minutes and over
    a fifth of a second, and only the tape's span tells the two apart.
    """

    minutes = FeaturePipeline.microstructure(
        mark_price=Decimal("100"),
        index_price=Decimal("100"),
        open_interest=Decimal("1"),
        bids=[["100", "1"]],
        asks=[["101", "1"]],
        trades=[
            {"p": "100", "q": "1", "m": False, "T": 1_784_040_000_000},
            {"p": "100", "q": "1", "m": True, "T": 1_784_040_240_000},
        ],
    )
    assert minutes["recent_trade_seconds"] == 240.0

    blink = FeaturePipeline.microstructure(
        mark_price=Decimal("100"),
        index_price=Decimal("100"),
        open_interest=Decimal("1"),
        bids=[["100", "1"]],
        asks=[["101", "1"]],
        trades=[
            {"p": "100", "q": "1", "m": False, "T": 1_784_040_000_000},
            {"p": "100", "q": "1", "m": False, "T": 1_784_040_000_200},
        ],
    )
    # Same one-sided imbalance, but over a window worth nothing.
    assert blink["recent_trade_imbalance"] == 1.0
    assert blink["recent_trade_seconds"] == 0.2


def test_ema_seed_does_not_leak_the_first_close_into_the_result() -> None:
    """A one-value seed keeps that value's noise in the average.

    A spike in the oldest bar of the window says nothing about the current
    trend, so it must not move the EMA that the setup rules read.
    """

    # A window only 2.5x the period is where seeding matters: there are not
    # enough steps for a bad seed to decay away before the value is read.
    spiked = [500.0, *([100.0] * 49)]

    assert _ema([100.0] * 50, 20) == 100.0
    assert abs(_ema(spiked, 20) - 100.0) < 1.5

    # Too short to seed on `period` values: report the plain average rather
    # than pretending to a smoothing that has not warmed up.
    assert _ema([100.0, 102.0], 20) == 101.0


def _daily_rows(count: int = 30, *, high: float = 120, low: float = 80) -> list[list[object]]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    rows = []
    for index in range(count):
        open_ms = int((start + timedelta(days=index)).timestamp() * 1000)
        close_ms = open_ms + 86_399_999
        # The extremes sit inside the last 20 bars so the window really covers them.
        bar_high = high if index == count - 5 else 105.0
        bar_low = low if index == count - 3 else 95.0
        rows.append(
            [open_ms, "100", str(bar_high), str(bar_low), "100", "10", close_ms, "1000"]
        )
    return rows


def test_daily_structure_places_the_live_mark_between_the_daily_extremes() -> None:
    """The daily levels are the ones real orders actually sit at.

    Position is measured off the live mark, not the last daily close: a close
    can be nearly a day old, and the question the setup rules ask is where
    price is now.
    """

    structure = FeaturePipeline().daily_structure(_daily_rows(), mark_price=Decimal("100"))

    assert structure["1d_range_high_20"] == 120.0
    assert structure["1d_range_low_20"] == 80.0
    assert structure["1d_range_position_20"] == 0.5
    # Levels only: a daily RSI or volume ratio would be a reading with no rule.
    assert set(structure) == {
        "1d_range_high_20",
        "1d_range_low_20",
        "1d_range_position_20",
        "1d_previous_high",
        "1d_previous_low",
        "1d_previous_close",
    }


def test_structure_features_confirm_pivots_and_breakouts_without_repainting() -> None:
    rows = _flat_rows()
    # Create a confirmed local high, then hold two closes beyond the same
    # pre-break boundary. Neither confirmation bar may redefine that boundary.
    rows[-8][2] = "103"
    rows[-8][4] = "102"
    rows[-2][2] = "104"
    rows[-2][4] = "104"
    rows[-1][2] = "104"
    rows[-1][4] = "104.5"

    features = FeaturePipeline().calculate(rows)

    assert features["last_swing_high"] == 103
    assert features["last_swing_high_confirmed"] == 1
    assert features["bars_since_swing_high"] == 7
    assert features["prior_range_high_20"] == 104
    assert features["breakout_above_20"] == 1
    assert features["breakout_hold_high_20"] == 103
    assert features["breakout_hold_above_20"] == 1


def test_breakout_hold_requires_two_closed_bars() -> None:
    rows = _flat_rows()
    rows[-8][2] = "103"
    rows[-8][4] = "102"
    rows[-1][2] = "104"
    rows[-1][4] = "104"

    features = FeaturePipeline().calculate(rows)

    assert features["breakout_above_20"] == 1
    assert features["breakout_hold_above_20"] == 0


def test_daily_position_is_not_clamped_when_price_breaks_the_range() -> None:
    """Above 1 means price is through the 20-day high. That is the signal."""

    pipeline = FeaturePipeline()
    broken = pipeline.daily_structure(_daily_rows(), mark_price=Decimal("130"))
    assert broken["1d_range_position_20"] > 1

    collapsed = pipeline.daily_structure(_daily_rows(), mark_price=Decimal("70"))
    assert collapsed["1d_range_position_20"] < 0


def test_daily_structure_refuses_a_symbol_too_young_to_have_the_levels() -> None:
    """20 daily closes is what the scanner's 30-day listing floor guarantees.

    Inventing levels from 10 bars and calling them the 20-day range would be
    worse than refusing: the model cannot tell a real level from a made-up one.
    """

    with pytest.raises(ValueError, match="20 closed daily klines"):
        FeaturePipeline().daily_structure(_daily_rows(10), mark_price=Decimal("100"))


def test_extension_is_distance_from_the_mean_not_position_in_the_range() -> None:
    """A trend sits at its own range edge; that is the trend, not extension.

    Conflating the two bars every trend entry at exactly the moment a trend
    exists, so the measure of "already run too far" has to be independent of
    where price sits in its range.
    """

    rising = FeaturePipeline().calculate(_rows())

    # _rows trends up the whole way: price is pinned at the range edge...
    assert rising["range_position_50"] > 0.85
    # ...while still tracking close to its own mean, so it is not chasing.
    assert abs(rising["ema20_distance_atr"]) < 2.5

    # A vertical finish leaves price at the same range edge but far from the mean.
    spiked = _rows()
    spiked[-1] = [*spiked[-1]]
    last_close = float(spiked[-1][4])
    spiked[-1][2] = str(last_close * 1.5)
    spiked[-1][4] = str(last_close * 1.5)
    chased = FeaturePipeline().calculate(spiked)

    assert chased["range_position_50"] > 0.85
    assert chased["ema20_distance_atr"] > 2.5
