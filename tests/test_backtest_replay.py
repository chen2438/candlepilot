from datetime import UTC, datetime, timedelta

from candlepilot.backtest.replay import align_cached_intents
from candlepilot.domain.models import TradeIntent


def test_cached_decision_aligns_to_previous_closed_candle() -> None:
    candle = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    records = [
        {
            "created_at": candle + timedelta(minutes=5, milliseconds=250),
            "intent": TradeIntent.hold("BTCUSDT", "5m", "cached"),
        }
    ]

    replay = align_cached_intents(records, "5m", {candle})

    assert len(replay) == 1
    assert replay[0].decided_at == candle


def test_latest_cached_decision_wins_within_same_boundary() -> None:
    candle = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    records = [
        {
            "created_at": candle + timedelta(minutes=5, milliseconds=100),
            "intent": TradeIntent.hold("BTCUSDT", "5m", "first"),
        },
        {
            "created_at": candle + timedelta(minutes=5, milliseconds=900),
            "intent": TradeIntent.hold("BTCUSDT", "5m", "latest"),
        },
    ]

    replay = align_cached_intents(records, "5m", {candle})

    assert len(replay) == 1
    assert replay[0].intent.rationale == "latest"
