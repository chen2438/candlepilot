from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from candlepilot.market.binance import FundingRate


INTERVAL_MILLISECONDS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
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
        candle_open = event_ms - (event_ms % interval_ms)
        rates_by_open[candle_open] = rates_by_open.get(candle_open, Decimal("0")) + event.rate

    return [
        {
            "timestamp": datetime.fromtimestamp(int(row[0]) / 1000, tz=UTC),
            "open": row[1],
            "high": row[2],
            "low": row[3],
            "close": row[4],
            "volume": row[5],
            "funding_rate": str(rates_by_open.get(int(row[0]), Decimal("0"))),
        }
        for row in rows
    ]
