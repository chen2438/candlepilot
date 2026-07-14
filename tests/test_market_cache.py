from datetime import UTC, datetime, timedelta
from pathlib import Path

from candlepilot.market.cache import HistoricalMarketCache


def test_parquet_cache_round_trip(tmp_path: Path) -> None:
    cache = HistoricalMarketCache(tmp_path / "market")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(minutes=5)
    candles = [
        {
            "timestamp": start,
            "open": "100.1",
            "high": "101",
            "low": "99",
            "close": "100.5",
            "volume": "12",
            "funding_rate": "0.0001",
        }
    ]

    assert cache.load("BTCUSDT", "5m", start, end, 10_000) is None
    path = cache.store("BTCUSDT", "5m", start, end, 10_000, candles)
    loaded = cache.load("BTCUSDT", "5m", start, end, 10_000)

    assert path.suffix == ".parquet"
    assert loaded == candles
