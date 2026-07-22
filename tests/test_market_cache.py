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
            "quote_volume": "1203.7",
            "funding_rate": "0.0001",
        }
    ]

    assert cache.load("BTCUSDT", "5m", start, end, 10_000) is None
    path = cache.store("BTCUSDT", "5m", start, end, 10_000, candles)
    loaded = cache.load("BTCUSDT", "5m", start, end, 10_000)

    assert path.suffix == ".parquet"
    assert "v2" in path.parts
    assert loaded == candles


def test_cache_clear_removes_parquet_files(tmp_path: Path) -> None:
    cache = HistoricalMarketCache(tmp_path / "market")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(minutes=5)
    candles = [
        {
            "timestamp": start,
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100",
            "volume": "1",
            "quote_volume": "100",
            "funding_rate": "0",
        }
    ]
    assert cache.clear() == 0  # nothing cached yet
    cache.store("BTCUSDT", "5m", start, end, 10_000, candles)
    cache.store("ETHUSDT", "1m", start, end, 10_000, candles)
    assert cache.clear() == 2
    assert cache.load("BTCUSDT", "5m", start, end, 10_000) is None
