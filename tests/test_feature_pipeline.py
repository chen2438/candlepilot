from datetime import UTC, datetime, timedelta
from decimal import Decimal

from candlepilot.market.features import FeaturePipeline, _ema


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


def test_multitimeframe_features_are_namespaced() -> None:
    features = FeaturePipeline().multitimeframe(
        {"1m": _rows(), "5m": _rows(), "15m": _rows(), "30m": _rows()}
    )

    assert len(features) == 4 * len(FeaturePipeline().calculate(_rows()))
    assert features["1m_ema_spread"] == features["5m_ema_spread"]
    assert "15m_rsi_14" in features
    assert "30m_rsi_14" in features


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
