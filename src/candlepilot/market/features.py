from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from candlepilot.domain.models import MarketSnapshot


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
        far_span = far_high - far_low
        return {
            "range_high_20": near_high,
            "range_low_20": near_low,
            "range_high_50": far_high,
            "range_low_50": far_low,
            # 0 at the range low, 1 at the range high.
            "range_position_50": (last - far_low) / far_span if far_span else 0.5,
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
        if set(rows_by_interval) != {"1m", "5m", "15m", "30m"}:
            raise ValueError("multitimeframe features require 1m, 5m, 15m, and 30m rows")
        combined: dict[str, float] = {}
        for interval in ("1m", "5m", "15m", "30m"):
            for name, value in self.calculate(rows_by_interval[interval]).items():
                combined[f"{interval}_{name}"] = value
        return combined

    def snapshot(
        self,
        *,
        symbol: str,
        cadence: Literal["1m", "5m", "15m", "30m"],
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
