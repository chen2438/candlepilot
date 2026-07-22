from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from candlepilot.market.binance import FundingRate


INTERVAL_MILLISECONDS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}


def build_backtest_candles(
    rows: list[list[Any]], funding_events: list[FundingRate], interval: str
) -> list[dict[str, Any]]:
    """Convert Binance rows and charge each funding event exactly once."""
    try:
        interval_ms = INTERVAL_MILLISECONDS[interval]
    except KeyError as exc:
        raise ValueError("unsupported kline interval") from exc

    rates_by_open: dict[int, Decimal] = {}
    for event in funding_events:
        event_ms = int(event.timestamp.timestamp() * 1000)
        # A funding timestamp on a bar boundary is charged before orders opened
        # from the just-closed bar's decision. Attach it to the candle ending at
        # that instant, not the candle beginning there, so settlement happens
        # before the new decision and cannot charge a position opened afterwards.
        candle_open = ((event_ms - 1) // interval_ms) * interval_ms
        rates_by_open[candle_open] = rates_by_open.get(candle_open, Decimal("0")) + event.rate

    return [
        {
            "timestamp": datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC),
            "open": row[1],
            "high": row[2],
            "low": row[3],
            "close": row[4],
            "volume": row[5],
            "quote_volume": row[7],
            "funding_rate": str(rates_by_open.get(int(row[0]), Decimal("0"))),
        }
        for row in rows
    ]
