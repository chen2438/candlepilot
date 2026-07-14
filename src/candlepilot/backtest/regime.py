from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

TREND_UP = "trend_up"
TREND_DOWN = "trend_down"
RANGE = "range"
HIGH_VOLATILITY = "high_volatility"
UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class RegimeClassifier:
    """Label each candle's market regime using only trailing data (no lookahead).

    The classifier is deterministic and scale-free so it generalises across
    cadences and symbols. Trend versus range is decided with Kaufman's
    efficiency ratio in ``[0, 1]``; a regime is flagged as high volatility when
    the recent return dispersion expands well beyond its own trailing baseline.
    Because every label depends solely on candles up to and including the target
    candle, tagging a trade with the regime at its entry candle never leaks
    information from the trade's future.
    """

    trend_window: int = 14
    baseline_window: int = 60
    trend_threshold: Decimal = Decimal("0.35")
    volatility_ratio: Decimal = Decimal("1.5")

    def __post_init__(self) -> None:
        if self.trend_window < 2:
            raise ValueError("trend_window must be at least 2")
        if self.baseline_window <= self.trend_window:
            raise ValueError("baseline_window must exceed trend_window")
        if self.trend_threshold < 0 or self.trend_threshold > 1:
            raise ValueError("trend_threshold must be within [0, 1]")
        if self.volatility_ratio <= 1:
            raise ValueError("volatility_ratio must exceed 1")

    def labels(self, closes: Sequence[Decimal]) -> list[str]:
        return [self._label_at(closes, index) for index in range(len(closes))]

    def _label_at(self, closes: Sequence[Decimal], index: int) -> str:
        if index < self.trend_window:
            return UNKNOWN
        window = closes[index - self.trend_window : index + 1]
        direction = window[-1] - window[0]
        path = sum(
            (abs(window[k] - window[k - 1]) for k in range(1, len(window))),
            Decimal("0"),
        )
        efficiency = abs(direction) / path if path > 0 else Decimal("0")

        recent_vol = self._stdev(self._returns(window))
        baseline_vol: Decimal | None = None
        if index >= self.baseline_window:
            baseline = closes[index - self.baseline_window : index + 1]
            baseline_vol = self._stdev(self._returns(baseline))

        if (
            baseline_vol is not None
            and baseline_vol > 0
            and recent_vol >= baseline_vol * self.volatility_ratio
        ):
            return HIGH_VOLATILITY
        if efficiency >= self.trend_threshold:
            return TREND_UP if direction > 0 else TREND_DOWN
        return RANGE

    @staticmethod
    def _returns(prices: Sequence[Decimal]) -> list[Decimal]:
        return [
            prices[k] / prices[k - 1] - Decimal("1")
            for k in range(1, len(prices))
            if prices[k - 1] > 0
        ]

    @staticmethod
    def _stdev(values: list[Decimal]) -> Decimal:
        if len(values) < 2:
            return Decimal("0")
        mean = sum(values, Decimal("0")) / Decimal(len(values))
        variance = sum(((value - mean) ** 2 for value in values), Decimal("0")) / Decimal(
            len(values)
        )
        return variance.sqrt()
