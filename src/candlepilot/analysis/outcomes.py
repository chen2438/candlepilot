from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from candlepilot.analysis.models import MarketAnalysis
from candlepilot.market.features import Kline


class AnalysisOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "neutral_observation",
        "waiting_entry",
        "active",
        "target1_partial",
        "target2",
        "stopped",
        "breakeven_after_target1",
        "ambiguous",
    ]
    bars_observed: int
    entry_at: datetime | None = None
    target1_at: datetime | None = None
    resolved_at: datetime | None = None
    detail: str


def next_complete_5m_start(completed_at: datetime) -> datetime:
    completed_at = completed_at.astimezone(UTC)
    base = completed_at.replace(second=0, microsecond=0)
    minutes = ((base.minute // 5) + 1) * 5
    if minutes >= 60:
        return base.replace(minute=0) + timedelta(hours=1)
    return base.replace(minute=minutes)


def _touches(bar: Kline, value: float) -> bool:
    return float(bar.low) <= value <= float(bar.high)


def evaluate_outcome(
    analysis: MarketAnalysis,
    bars: list[Kline],
) -> AnalysisOutcome:
    if analysis.direction == "neutral":
        return AnalysisOutcome(
            status="neutral_observation",
            bars_observed=len(bars),
            detail="neutral analysis has no entry, stop, or target outcome",
        )
    assert analysis.entry_plan is not None
    plan = analysis.entry_plan
    state: Literal["waiting", "active", "partial"] = "waiting"
    entry_at: datetime | None = None
    target1_at: datetime | None = None
    for index, bar in enumerate(bars, start=1):
        entry = _touches(bar, plan.entry)
        stop = _touches(bar, plan.stop)
        target1 = _touches(bar, plan.target1)
        target2 = _touches(bar, plan.target2)
        if state == "waiting":
            if not entry:
                continue
            entry_at = bar.open_time
            exits = [stop, target1, target2]
            if any(exits):
                return AnalysisOutcome(
                    status="ambiguous",
                    bars_observed=index,
                    entry_at=entry_at,
                    resolved_at=bar.open_time,
                    detail="entry and an exit level were touched in the same 5m bar; order is unknowable",
                )
            state = "active"
            continue
        if state == "active":
            if sum((stop, target1, target2)) > 1:
                return AnalysisOutcome(
                    status="ambiguous",
                    bars_observed=index,
                    entry_at=entry_at,
                    resolved_at=bar.open_time,
                    detail="multiple exit levels were touched in the same 5m bar; order is unknowable",
                )
            if stop:
                return AnalysisOutcome(
                    status="stopped",
                    bars_observed=index,
                    entry_at=entry_at,
                    resolved_at=bar.open_time,
                    detail="entry was active before the structural stop was touched",
                )
            if target2:
                return AnalysisOutcome(
                    status="target2",
                    bars_observed=index,
                    entry_at=entry_at,
                    target1_at=bar.open_time,
                    resolved_at=bar.open_time,
                    detail="T2 was touched after entry; T1 lies on the same price path",
                )
            if target1:
                if entry:
                    return AnalysisOutcome(
                        status="ambiguous",
                        bars_observed=index,
                        entry_at=entry_at,
                        target1_at=bar.open_time,
                        resolved_at=bar.open_time,
                        detail="T1 and the breakeven price were touched in the same 5m bar; order is unknowable",
                    )
                target1_at = bar.open_time
                state = "partial"
            continue
        # After T1 the plan reduces roughly half and manages the remainder from
        # breakeven. The original structural stop is no longer the active level.
        breakeven = entry
        if breakeven and target2:
            return AnalysisOutcome(
                status="ambiguous",
                bars_observed=index,
                entry_at=entry_at,
                target1_at=target1_at,
                resolved_at=bar.open_time,
                detail="breakeven and T2 were touched in the same 5m bar; order is unknowable",
            )
        if target2:
            return AnalysisOutcome(
                status="target2",
                bars_observed=index,
                entry_at=entry_at,
                target1_at=target1_at,
                resolved_at=bar.open_time,
                detail="T1 partial was followed by T2 on the remainder",
            )
        if breakeven:
            return AnalysisOutcome(
                status="breakeven_after_target1",
                bars_observed=index,
                entry_at=entry_at,
                target1_at=target1_at,
                resolved_at=bar.open_time,
                detail="T1 partial was followed by a return to the entry price",
            )
    if state == "waiting":
        status = "waiting_entry"
        detail = "entry has not been touched in fully post-analysis 5m bars"
    elif state == "active":
        status = "active"
        detail = "entry is active; neither structural stop nor T1 has been touched"
    else:
        status = "target1_partial"
        detail = "T1 partial is recorded; the remainder has touched neither breakeven nor T2"
    return AnalysisOutcome(
        status=status,
        bars_observed=len(bars),
        entry_at=entry_at,
        target1_at=target1_at,
        detail=detail,
    )


def parse_closed_rows(rows: list[list[Any]]) -> list[Kline]:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    return [item for row in rows if (item := Kline.from_binance(row, now_ms=now_ms)).closed]
