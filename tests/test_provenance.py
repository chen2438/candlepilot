from datetime import UTC, datetime
from decimal import Decimal

from candlepilot.backtest.engine import Candle
from candlepilot.provenance import BACKTEST_DATA_SCHEMA_VERSION, content_fingerprint


def _candles(close: str = "101") -> list[Candle]:
    return [
        Candle(
            datetime(2026, 1, 1, tzinfo=UTC),
            Decimal("100"),
            Decimal("102"),
            Decimal("99"),
            Decimal(close),
            Decimal("10"),
        )
    ]


def test_content_fingerprint_is_stable_for_identical_market_data() -> None:
    first = content_fingerprint(_candles(), schema_version=BACKTEST_DATA_SCHEMA_VERSION)
    second = content_fingerprint(_candles(), schema_version=BACKTEST_DATA_SCHEMA_VERSION)
    assert first == second
    assert first.startswith("backtest-candles-v1:sha256:")


def test_content_fingerprint_changes_with_market_data() -> None:
    assert content_fingerprint(
        _candles("101"), schema_version=BACKTEST_DATA_SCHEMA_VERSION
    ) != content_fingerprint(_candles("101.1"), schema_version=BACKTEST_DATA_SCHEMA_VERSION)
