from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from candlepilot.domain.models import MarketSnapshot


#: Intervals the decision model is given features for.
#:
#: Every setup rule in the prompt is written against these, and nothing goes in
#: here without a rule that reads it: an interval the rules never name is one
#: the model has to invent a use for, and it will invent a different one each
#: call. 1m was dropped for exactly that reason: no setup rule names it.
DECISION_FEATURE_INTERVALS = ("5m", "15m", "30m", "1h", "4h")

#: Interval supplying daily structure levels, and only those.
#:
#: The full feature ladder is deliberately *not* applied here. The levels are
#: what the daily bar is for -- the 20-day high and low are places real orders
#: sit, unlike a 50-minute high that nobody defends -- and a daily RSI or volume
#: ratio would be another reading with no rule to read it. It also keeps the
#: bar count honest: 20 daily closes is what MarketScanner's 30-day listing
#: floor guarantees, whereas a daily ema_50 would silently degrade to a plain
#: mean on anything younger than 50 days.
DAILY_STRUCTURE_INTERVAL = "1d"
DAILY_STRUCTURE_PERIOD = 20


@dataclass(frozen=True, slots=True)
class Kline:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    closed: bool = True

    @classmethod
    def from_binance(cls, row: list[Any], *, now_ms: int | None = None) -> Kline:
        now_ms = now_ms or int(datetime.now(UTC).timestamp() * 1000)
        return cls(
            open_time=datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC),
            open=Decimal(str(row[1])),
            high=Decimal(str(row[2])),
            low=Decimal(str(row[3])),
            close=Decimal(str(row[4])),
            volume=Decimal(str(row[5])),
            quote_volume=Decimal(str(row[7])),
            closed=int(row[6]) < now_ms,
        )


def _ema(values: list[float], period: int) -> float:
    """EMA seeded from the first ``period`` values rather than a single close.

    Seeding on one value leaves that value's noise in the result for as long as
    it takes to decay, so the seed must be an average and the series must be as
    long as the caller can supply.
    """

    if not values:
        raise ValueError("EMA requires values")
    if len(values) <= period:
        return sum(values) / len(values)
    result = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for value in values[period:]:
        result = alpha * value + (1 - alpha) * result
    return result


def _rsi(closes: list[float], period: int = 14) -> float:
    if len(closes) <= period:
        return 50.0
    changes = [current - previous for previous, current in zip(closes, closes[1:])]
    sample = changes[-period:]
    gains = sum(max(change, 0) for change in sample) / period
    losses = sum(max(-change, 0) for change in sample) / period
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    return 100 - (100 / (1 + gains / losses))


def _atr(klines: list[Kline], period: int = 14) -> float:
    if len(klines) < 2:
        return 0.0
    ranges = []
    for previous, current in zip(klines, klines[1:]):
        ranges.append(
            max(
                float(current.high - current.low),
                abs(float(current.high - previous.close)),
                abs(float(current.low - previous.close)),
            )
        )
    sample = ranges[-period:]
    return sum(sample) / len(sample)


def _range(klines: list[Kline], period: int) -> tuple[float, float]:
    window = klines[-period:]
    return (
        max(float(item.high) for item in window),
        min(float(item.low) for item in window),
    )


def _last_confirmed_swing(
    klines: list[Kline], *, high: bool, radius: int = 2
) -> tuple[float, int] | None:
    """Return the latest pivot whose bars on both sides are already closed."""

    if len(klines) < radius * 2 + 1:
        return None
    for index in range(len(klines) - radius - 1, radius - 1, -1):
        value = klines[index].high if high else klines[index].low
        neighbours = [
            klines[offset].high if high else klines[offset].low
            for offset in range(index - radius, index + radius + 1)
            if offset != index
        ]
        if (all(value > other for other in neighbours) if high else all(value < other for other in neighbours)):
            return float(value), len(klines) - 1 - index
    return None


class FeaturePipeline:
    def calculate(self, rows: list[list[Any]]) -> dict[str, float]:
        klines = [Kline.from_binance(row) for row in rows]
        closed = [item for item in klines if item.closed]
        if len(closed) < 20:
            raise ValueError("at least 20 closed klines are required")
        closes = [float(item.close) for item in closed]
        volumes = [float(item.quote_volume) for item in closed]
        last = closes[-1]
        ema_fast = _ema(closes, 20)
        ema_slow = _ema(closes, 50)
        atr = _atr(closed, 14)
        volume_mean = sum(volumes[-20:]) / 20
        # Price structure: moving averages say which way, not where. Judging
        # whether price is extended, or near a reference worth reclaiming or
        # rejecting, needs the levels themselves.
        near_high, near_low = _range(closed, 20)
        far_high, far_low = _range(closed, 50)
        prior = closed[:-1] or closed
        prior_high, prior_low = _range(prior, 20)
        swing_high = _last_confirmed_swing(closed, high=True)
        swing_low = _last_confirmed_swing(closed, high=False)
        far_span = far_high - far_low
        last_bar_span = float(closed[-1].high - closed[-1].low)
        return {
            "range_high_20": near_high,
            "range_low_20": near_low,
            "range_high_50": far_high,
            "range_low_50": far_low,
            # 0 at the range low, 1 at the range high.
            "range_position_50": (last - far_low) / far_span if far_span else 0.5,
            # Unlike range_high_20/range_low_20 these levels exclude the latest
            # bar, so an actual close through the old boundary is observable.
            "prior_range_high_20": prior_high,
            "prior_range_low_20": prior_low,
            "breakout_above_20": float(last > prior_high),
            "breakdown_below_20": float(last < prior_low),
            # A two-bars-each-side pivot cannot repaint. When none exists in the
            # fetched window, expose the prior range edge with an explicit false
            # flag rather than inventing a confirmed swing.
            "last_swing_high": swing_high[0] if swing_high else prior_high,
            "last_swing_high_confirmed": float(swing_high is not None),
            "bars_since_swing_high": float(swing_high[1] if swing_high else len(prior)),
            "last_swing_low": swing_low[0] if swing_low else prior_low,
            "last_swing_low_confirmed": float(swing_low is not None),
            "bars_since_swing_low": float(swing_low[1] if swing_low else len(prior)),
            "last_bar_close_position": (
                float(closed[-1].close - closed[-1].low) / last_bar_span
                if last_bar_span
                else 0.5
            ),
            # How far price has run from its own mean, in units of its own
            # volatility. This is what "extended" means: chasing a move that has
            # already travelled. Range position is *not* -- a trend puts price at
            # its range edge by definition, so reading the edge as "extended"
            # bars every trend entry at exactly the moment the trend exists.
            "ema20_distance_atr": (last - ema_fast) / atr if atr else 0.0,
            "return_1": (last / closes[-2]) - 1,
            "return_5": (last / closes[-6]) - 1 if len(closes) >= 6 else 0.0,
            "ema_20": ema_fast,
            "ema_50": ema_slow,
            "ema_spread": (ema_fast / ema_slow) - 1 if ema_slow else 0.0,
            "rsi_14": _rsi(closes),
            "atr_14": atr,
            "atr_fraction": atr / last if last else 0.0,
            "quote_volume_ratio": volumes[-1] / volume_mean if volume_mean else 0.0,
        }

    def multitimeframe(
        self, rows_by_interval: dict[str, list[list[Any]]]
    ) -> dict[str, float]:
        if set(rows_by_interval) != set(DECISION_FEATURE_INTERVALS):
            expected = ", ".join(DECISION_FEATURE_INTERVALS)
            raise ValueError(f"multitimeframe features require exactly {expected} rows")
        combined: dict[str, float] = {}
        for interval in DECISION_FEATURE_INTERVALS:
            for name, value in self.calculate(rows_by_interval[interval]).items():
                combined[f"{interval}_{name}"] = value
        return combined

    def daily_structure(
        self, rows: list[list[Any]], *, mark_price: Decimal
    ) -> dict[str, float]:
        """The 20-day high and low, and where the live mark sits between them.

        Position is measured against ``mark_price`` rather than the last daily
        close, which can be almost a day stale -- the question is where price is
        now, not where it finished yesterday. It is deliberately not clamped:
        above 1 means price is through the 20-day high, and that is the signal,
        not an error.
        """

        klines = [Kline.from_binance(row) for row in rows]
        closed = [item for item in klines if item.closed]
        if len(closed) < DAILY_STRUCTURE_PERIOD:
            raise ValueError(
                f"daily structure requires at least {DAILY_STRUCTURE_PERIOD} closed daily klines"
            )
        high, low = _range(closed, DAILY_STRUCTURE_PERIOD)
        span = high - low
        mark = float(mark_price)
        prefix = DAILY_STRUCTURE_INTERVAL
        return {
            f"{prefix}_range_high_{DAILY_STRUCTURE_PERIOD}": high,
            f"{prefix}_range_low_{DAILY_STRUCTURE_PERIOD}": low,
            f"{prefix}_range_position_{DAILY_STRUCTURE_PERIOD}": (
                (mark - low) / span if span else 0.5
            ),
            f"{prefix}_previous_high": float(closed[-1].high),
            f"{prefix}_previous_low": float(closed[-1].low),
            f"{prefix}_previous_close": float(closed[-1].close),
        }

    def snapshot(
        self,
        *,
        symbol: str,
        cadence: Literal["1m", "5m", "15m", "30m", "1h", "4h"],
        features: dict[str, float],
        mark_price: Decimal,
        bid: Decimal,
        ask: Decimal,
        quote_volume_24h: Decimal,
        funding_rate: Decimal,
        timestamp: datetime | None = None,
    ) -> MarketSnapshot:
        # Features arrive already assembled: computing the decision cadence's
        # own features here as well would repeat every ``<cadence>_`` key from
        # ``multitimeframe`` unprefixed, and the model would read one reading
        # as two independent ones.
        return MarketSnapshot(
            symbol=symbol,
            cadence=cadence,
            timestamp=timestamp or datetime.now(UTC),
            mark_price=mark_price,
            bid=bid,
            ask=ask,
            quote_volume_24h=quote_volume_24h,
            funding_rate=funding_rate,
            features=features,
        )

    @staticmethod
    def microstructure(
        *,
        mark_price: Decimal,
        index_price: Decimal,
        open_interest: Decimal,
        bids: list[list[Any]],
        asks: list[list[Any]],
        trades: list[dict[str, Any]],
    ) -> dict[str, float]:
        bid_quantity = sum(Decimal(str(level[1])) for level in bids)
        ask_quantity = sum(Decimal(str(level[1])) for level in asks)
        depth_total = bid_quantity + ask_quantity
        buy_notional = Decimal("0")
        sell_notional = Decimal("0")
        for trade in trades:
            notional = Decimal(str(trade["p"])) * Decimal(str(trade["q"]))
            if bool(trade.get("m")):
                sell_notional += notional
            else:
                buy_notional += notional
        trade_total = buy_notional + sell_notional
        # How much time the trade tape actually covers. The count is fixed, so
        # the window is not: on a busy symbol it can be seconds. Without this
        # the model cannot tell real flow from a noisy blink.
        stamps = [int(trade["T"]) for trade in trades if trade.get("T") is not None]
        trade_seconds = (max(stamps) - min(stamps)) / 1000 if len(stamps) >= 2 else 0.0
        return {
            "recent_trade_seconds": trade_seconds,
            "basis_bps": float(
                ((mark_price / index_price) - 1) * Decimal("10000")
                if index_price
                else Decimal("0")
            ),
            "open_interest": float(open_interest),
            "book_imbalance": float(
                (bid_quantity - ask_quantity) / depth_total if depth_total else Decimal("0")
            ),
            "recent_trade_imbalance": float(
                (buy_notional - sell_notional) / trade_total
                if trade_total
                else Decimal("0")
            ),
        }
