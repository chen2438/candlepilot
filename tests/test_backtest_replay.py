import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from candlepilot.backtest.engine import BacktestConfig, Candle
from candlepilot.backtest.replay import align_cached_intents, generate_fresh_intents
from candlepilot.domain.models import ProviderHealth, TradeIntent
from candlepilot.providers.base import LLMProvider, ProviderResult


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


class ReplayProvider(LLMProvider):
    name = "replay"

    async def health_check(self):
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio):
        intent = TradeIntent.hold(snapshot.symbol, snapshot.cadence, "historical fixture")
        return ProviderResult(intent, self.name, "fixture", timedelta(0), "{}", {})


def test_fresh_replay_uses_only_rolling_past_data() -> None:
    candles = [
        Candle(
            timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * index),
            open=Decimal("100") + index,
            high=Decimal("102") + index,
            low=Decimal("99") + index,
            close=Decimal("101") + index,
            volume=Decimal("10"),
        )
        for index in range(21)
    ]

    decisions, results = asyncio.run(
        generate_fresh_intents(
            ReplayProvider(),
            candles,
            symbol="BTCUSDT",
            cadence="5m",
            config=BacktestConfig(),
            max_calls=2,
        )
    )

    assert [item.decided_at for item in decisions] == [candles[19].timestamp, candles[20].timestamp]
    assert len(results) == 2
