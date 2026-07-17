from datetime import UTC, datetime
from decimal import Decimal

from candlepilot.market.binance import FundingRate
from candlepilot.market.history import build_backtest_candles


def _row(timestamp_ms: int) -> list[object]:
    return [timestamp_ms, "100", "110", "90", "105", "12"]


def test_funding_is_charged_only_on_its_settlement_candle() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    rows = [_row(start_ms + offset * 300_000) for offset in range(4)]
    event = FundingRate(
        timestamp=datetime.fromtimestamp((start_ms + 420_000) / 1000, tz=UTC),
        rate=Decimal("0.0001"),
    )

    candles = build_backtest_candles(rows, [event], "5m")

    assert [item["funding_rate"] for item in candles] == ["0", "0.0001", "0", "0"]


def test_multiple_funding_events_in_one_candle_are_summed() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    events = [
        FundingRate(start.replace(minute=2), Decimal("0.0001")),
        FundingRate(start.replace(minute=1), Decimal("-0.00004")),
    ]

    candles = build_backtest_candles([_row(start_ms)], events, "5m")

    assert candles[0]["funding_rate"] == "0.00006"


def test_boundary_funding_is_settled_before_the_new_candle_opens() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    start_ms = int(start.timestamp() * 1000)
    event = FundingRate(start, Decimal("0.0001"))

    candles = build_backtest_candles(
        [_row(start_ms - 300_000), _row(start_ms)], [event], "5m"
    )

    assert [item["funding_rate"] for item in candles] == ["0.0001", "0"]
