from decimal import Decimal

from candlepilot.backtest.regime import (
    HIGH_VOLATILITY,
    RANGE,
    TREND_DOWN,
    TREND_UP,
    UNKNOWN,
    RegimeClassifier,
)


def test_short_history_is_unknown() -> None:
    classifier = RegimeClassifier()
    closes = [Decimal(100 + index) for index in range(10)]
    assert classifier.labels(closes) == [UNKNOWN] * 10


def test_steady_uptrend_is_trend_up() -> None:
    closes = [Decimal(100 + index) for index in range(20)]
    labels = RegimeClassifier().labels(closes)
    assert labels[-1] == TREND_UP


def test_steady_downtrend_is_trend_down() -> None:
    closes = [Decimal(200 - index) for index in range(20)]
    labels = RegimeClassifier().labels(closes)
    assert labels[-1] == TREND_DOWN


def test_choppy_market_is_range() -> None:
    closes = [Decimal("100") + Decimal(index % 2) for index in range(20)]
    labels = RegimeClassifier().labels(closes)
    assert labels[-1] == RANGE


def test_volatility_burst_is_high_volatility() -> None:
    calm = [Decimal("100") + Decimal(index % 2) * Decimal("0.01") for index in range(60)]
    burst: list[Decimal] = []
    for index in range(20):
        burst.append(Decimal("100") + Decimal(5) * Decimal(index % 2))
    labels = RegimeClassifier().labels(calm + burst)
    assert HIGH_VOLATILITY in labels[60:]


def test_labels_align_with_close_count() -> None:
    closes = [Decimal(100 + index) for index in range(30)]
    assert len(RegimeClassifier().labels(closes)) == len(closes)


def test_classifier_rejects_invalid_parameters() -> None:
    for kwargs in (
        {"trend_window": 1},
        {"trend_window": 60, "baseline_window": 60},
        {"trend_threshold": Decimal("2")},
        {"volatility_ratio": Decimal("1")},
    ):
        try:
            RegimeClassifier(**kwargs)
        except ValueError:
            continue
        raise AssertionError(f"expected ValueError for {kwargs}")
