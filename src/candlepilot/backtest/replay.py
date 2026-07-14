from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from candlepilot.backtest.engine import BacktestConfig, Candle, ReplayIntent
from candlepilot.domain.models import MarketSnapshot, PortfolioState
from candlepilot.market.features import FeaturePipeline
from candlepilot.providers.base import LLMProvider, ProviderResult


CADENCE_MILLISECONDS = {"1m": 60_000, "5m": 300_000, "15m": 900_000}


def align_cached_intents(
    records: list[dict[str, Any]],
    cadence: str,
    candle_timestamps: set[datetime],
) -> list[ReplayIntent]:
    """Align post-close inference times to their source candle without look-ahead."""
    try:
        cadence_ms = CADENCE_MILLISECONDS[cadence]
    except KeyError as exc:
        raise ValueError("unsupported replay cadence") from exc

    aligned: dict[datetime, ReplayIntent] = {}
    for record in records:
        created_at = record["created_at"]
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        created_ms = int(created_at.timestamp() * 1000)
        source_open_ms = (created_ms // cadence_ms) * cadence_ms - cadence_ms
        source_open = datetime.fromtimestamp(source_open_ms / 1000, tz=UTC)
        if source_open in candle_timestamps:
            aligned[source_open] = ReplayIntent(source_open, record["intent"])
    return [aligned[timestamp] for timestamp in sorted(aligned)]


def _feature_row(candle: Candle, interval_ms: int) -> list[Any]:
    open_ms = int(candle.timestamp.timestamp() * 1000)
    return [
        open_ms,
        str(candle.open),
        str(candle.high),
        str(candle.low),
        str(candle.close),
        str(candle.volume),
        open_ms + interval_ms - 1,
        str(candle.volume * candle.close),
    ]


async def generate_fresh_intents(
    provider: LLMProvider,
    candles: list[Candle],
    *,
    symbol: str,
    cadence: str,
    config: BacktestConfig,
    max_calls: int,
) -> tuple[list[ReplayIntent], list[ProviderResult]]:
    """Call an authenticated provider on rolling, past-only candle windows."""

    if cadence not in CADENCE_MILLISECONDS:
        raise ValueError("unsupported replay cadence")
    if max_calls < 1:
        raise ValueError("max_calls must be positive")
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    if any(
        int(candle.timestamp.timestamp() * 1000) + CADENCE_MILLISECONDS[cadence] > now_ms
        for candle in candles
    ):
        raise ValueError("fresh LLM replay only accepts fully closed historical candles")
    required = max(0, len(candles) - 19)
    if required > max_calls:
        raise ValueError(f"historical replay requires {required} LLM calls; max_calls is {max_calls}")
    interval_ms = CADENCE_MILLISECONDS[cadence]
    rows = [_feature_row(candle, interval_ms) for candle in candles]
    portfolio = PortfolioState(
        equity=config.initial_equity,
        available_balance=config.initial_equity,
    )
    decisions: list[ReplayIntent] = []
    results: list[ProviderResult] = []
    pipeline = FeaturePipeline()
    for index in range(19, len(candles)):
        candle = candles[index]
        features = pipeline.calculate(rows[max(0, index - 199) : index + 1])
        snapshot = MarketSnapshot(
            symbol=symbol,
            cadence=cadence,  # type: ignore[arg-type]
            timestamp=datetime.fromtimestamp(
                (int(candle.timestamp.timestamp() * 1000) + interval_ms) / 1000,
                tz=UTC,
            ),
            mark_price=candle.close,
            bid=candle.close,
            ask=candle.close,
            quote_volume_24h=sum(
                (item.volume * item.close for item in candles[max(0, index - 287) : index + 1]),
                Decimal("0"),
            ),
            funding_rate=candle.funding_rate,
            features=features,
        )
        result = await provider.generate_trade_intent(snapshot, portfolio)
        if result.intent.symbol != symbol or result.intent.cadence != cadence:
            raise ValueError("provider returned an intent for a different symbol or cadence")
        decisions.append(ReplayIntent(candle.timestamp, result.intent))
        results.append(result)
    return decisions, results
