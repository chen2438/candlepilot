from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from statistics import fmean
from typing import Any

from candlepilot.market.features import Kline


EMA_PERIODS = (9, 21, 55)


def ema(values: Sequence[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = fmean(values[:period])
    result[period - 1] = current
    alpha = 2 / (period + 1)
    for index in range(period, len(values)):
        current = values[index] * alpha + current * (1 - alpha)
        result[index] = current
    return result


def macd(values: Sequence[float]) -> tuple[list[float | None], list[float | None], list[float | None]]:
    fast = ema(values, 12)
    slow = ema(values, 26)
    dif = [
        left - right if left is not None and right is not None else None
        for left, right in zip(fast, slow, strict=True)
    ]
    first = next((index for index, value in enumerate(dif) if value is not None), -1)
    dea: list[float | None] = [None] * len(values)
    if first >= 0:
        tail = ema([float(value) for value in dif[first:] if value is not None], 9)
        dea[first:] = tail
    hist = [
        2 * (left - right) if left is not None and right is not None else None
        for left, right in zip(dif, dea, strict=True)
    ]
    return dif, dea, hist


def swings(klines: Sequence[Kline], radius: int = 3) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    highs: list[dict[str, Any]] = []
    lows: list[dict[str, Any]] = []
    for index in range(radius, len(klines) - radius):
        window = klines[index - radius : index + radius + 1]
        item = klines[index]
        if item.high == max(row.high for row in window):
            highs.append({"time": item.open_time.isoformat(), "price": float(item.high)})
        if item.low == min(row.low for row in window):
            lows.append({"time": item.open_time.isoformat(), "price": float(item.low)})
    return highs, lows


def session_vwap(klines: Sequence[Kline]) -> float | None:
    if not klines:
        return None
    session = klines[-1].open_time.astimezone(UTC).date()
    selected = [row for row in klines if row.open_time.astimezone(UTC).date() == session]
    volume = sum(float(row.volume) for row in selected)
    if volume <= 0:
        return None
    weighted = sum(
        ((float(row.high) + float(row.low) + float(row.close)) / 3) * float(row.volume)
        for row in selected
    )
    return weighted / volume


def _last(values: Sequence[float | None]) -> float | None:
    return next((value for value in reversed(values) if value is not None), None)


def summarize(klines: Sequence[Kline], *, include_vwap: bool) -> dict[str, Any]:
    closes = [float(item.close) for item in klines]
    dif, dea, hist = macd(closes)
    ema_lines = [(period, ema(closes, period)) for period in EMA_PERIODS]
    high_points, low_points = swings(klines)
    crosses: list[dict[str, str]] = []
    previous: float | None = None
    for index, value in enumerate(hist):
        if value is None:
            continue
        if previous is not None:
            if previous <= 0 < value:
                crosses.append({"time": klines[index].open_time.isoformat(), "type": "golden"})
            elif previous >= 0 > value:
                crosses.append({"time": klines[index].open_time.isoformat(), "type": "death"})
        previous = value
    return {
        "last_close": closes[-1],
        "last_dif": _last(dif),
        "last_dea": _last(dea),
        "last_hist": _last(hist),
        "last_vwap": session_vwap(klines) if include_vwap else None,
        "emas": [{"period": period, "last": _last(line)} for period, line in ema_lines],
        "recent_swing_highs": high_points[-6:],
        "recent_swing_lows": low_points[-6:],
        "last_cross": crosses[-1] if crosses else None,
        # Kansoku Pro detectors are not part of its public implementation.  Empty
        # lists are explicit here so the prompt cannot confuse missing detectors
        # with a negative detector result.
        "pro_detectors_available": False,
        "candle_patterns": [],
        "divergence_candidates": [],
        "pattern_123": [],
        "second_breakouts": [],
    }


def raw_bar(item: Kline) -> dict[str, Any]:
    return {
        "time": item.open_time.isoformat(),
        "open": float(item.open),
        "high": float(item.high),
        "low": float(item.low),
        "close": float(item.close),
        "volume": float(item.volume),
        "quote_volume": float(item.quote_volume),
    }


def closed_klines(rows: list[list[Any]]) -> list[Kline]:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    return [item for row in rows if (item := Kline.from_binance(row, now_ms=now_ms)).closed]
