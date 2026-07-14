from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from candlepilot.backtest.engine import ReplayIntent


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
