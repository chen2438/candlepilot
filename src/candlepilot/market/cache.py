from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq


_SCHEMA = pa.schema(
    [
        pa.field("timestamp", pa.timestamp("ms", tz="UTC"), nullable=False),
        pa.field("open", pa.string(), nullable=False),
        pa.field("high", pa.string(), nullable=False),
        pa.field("low", pa.string(), nullable=False),
        pa.field("close", pa.string(), nullable=False),
        pa.field("volume", pa.string(), nullable=False),
        pa.field("funding_rate", pa.string(), nullable=False),
    ]
)


class HistoricalMarketCache:
    """Exact-range Parquet cache for immutable closed historical candles."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def load(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> list[dict[str, Any]] | None:
        path = self._path(symbol, interval, start, end, limit)
        if not path.is_file():
            return None
        return pq.read_table(path, schema=_SCHEMA).to_pylist()

    def store(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int,
        candles: list[dict[str, Any]],
    ) -> Path:
        path = self._path(symbol, interval, start, end, limit)
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(
            [{key: str(value) if key != "timestamp" else value for key, value in row.items()} for row in candles],
            schema=_SCHEMA,
        )
        temporary = path.with_suffix(".tmp.parquet")
        pq.write_table(table, temporary, compression="zstd")
        temporary.replace(path)
        return path

    def _path(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        limit: int,
    ) -> Path:
        if not symbol.isalnum() or not symbol.endswith("USDT"):
            raise ValueError("invalid cache symbol")
        if interval not in {"1m", "5m", "15m", "1h"}:
            raise ValueError("invalid cache interval")
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        return self.root / symbol / interval / f"{start_ms}-{end_ms}-{limit}.parquet"
