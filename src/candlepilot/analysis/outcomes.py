from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from candlepilot.analysis.models import MarketAnalysis
from candlepilot.market.features import Kline


class AnalysisOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "neutral_observation",
        "waiting_entry",
        "stopped_before_entry",
        "target1_before_entry",
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


@dataclass(slots=True)
class _OutcomeState:
    phase: Literal["waiting", "active", "partial"] = "waiting"
    entry_at: datetime | None = None
    target1_at: datetime | None = None


def _evaluate_bar(
    analysis: MarketAnalysis,
    state: _OutcomeState,
    bar: Kline,
    *,
    bars_observed: int,
    interval_label: str,
) -> AnalysisOutcome | None:
    assert analysis.entry_plan is not None
    plan = analysis.entry_plan
    entry = _touches(bar, plan.entry)
    stop = _touches(bar, plan.stop)
    target1 = _touches(bar, plan.target1)
    target2 = _touches(bar, plan.target2)
    if state.phase == "waiting":
        if not entry:
            if stop:
                return AnalysisOutcome(
                    status="stopped_before_entry",
                    bars_observed=bars_observed,
                    resolved_at=bar.open_time,
                    detail="计划尚未入场，价格已先触及结构止损",
                )
            if target1:
                return AnalysisOutcome(
                    status="target1_before_entry",
                    bars_observed=bars_observed,
                    resolved_at=bar.open_time,
                    detail="计划尚未入场，价格已先触及 T1",
                )
            return None
        state.entry_at = bar.open_time
        if any((stop, target1, target2)):
            return AnalysisOutcome(
                status="ambiguous",
                bars_observed=bars_observed,
                entry_at=state.entry_at,
                resolved_at=bar.open_time,
                detail=(
                    f"入场价和退出价位在同一根完整 {interval_label} K 线内被触及，"
                    "无法确定先后顺序"
                ),
            )
        state.phase = "active"
        return None
    if state.phase == "active":
        if sum((stop, target1, target2)) > 1:
            return AnalysisOutcome(
                status="ambiguous",
                bars_observed=bars_observed,
                entry_at=state.entry_at,
                resolved_at=bar.open_time,
                detail=(
                    f"多个退出价位在同一根完整 {interval_label} K 线内被触及，"
                    "无法确定先后顺序"
                ),
            )
        if stop:
            return AnalysisOutcome(
                status="stopped",
                bars_observed=bars_observed,
                entry_at=state.entry_at,
                resolved_at=bar.open_time,
                detail="计划已入场，随后触及结构止损",
            )
        if target2:
            return AnalysisOutcome(
                status="target2",
                bars_observed=bars_observed,
                entry_at=state.entry_at,
                target1_at=bar.open_time,
                resolved_at=bar.open_time,
                detail="入场后触及 T2；价格路径同时经过 T1",
            )
        if target1:
            if entry:
                return AnalysisOutcome(
                    status="ambiguous",
                    bars_observed=bars_observed,
                    entry_at=state.entry_at,
                    target1_at=bar.open_time,
                    resolved_at=bar.open_time,
                    detail=(
                        f"T1 与保本价在同一根完整 {interval_label} K 线内被触及，"
                        "无法确定先后顺序"
                    ),
                )
            state.target1_at = bar.open_time
            state.phase = "partial"
        return None
    # After T1 the plan reduces roughly half and manages the remainder from
    # breakeven. The original structural stop is no longer the active level.
    breakeven = entry
    if breakeven and target2:
        return AnalysisOutcome(
            status="ambiguous",
            bars_observed=bars_observed,
            entry_at=state.entry_at,
            target1_at=state.target1_at,
            resolved_at=bar.open_time,
            detail=(
                f"保本价与 T2 在同一根完整 {interval_label} K 线内被触及，"
                "无法确定先后顺序"
            ),
        )
    if target2:
        return AnalysisOutcome(
            status="target2",
            bars_observed=bars_observed,
            entry_at=state.entry_at,
            target1_at=state.target1_at,
            resolved_at=bar.open_time,
            detail="T1 部分止盈后，剩余仓位触及 T2",
        )
    if breakeven:
        return AnalysisOutcome(
            status="breakeven_after_target1",
            bars_observed=bars_observed,
            entry_at=state.entry_at,
            target1_at=state.target1_at,
            resolved_at=bar.open_time,
            detail="T1 部分止盈后，剩余仓位回到入场价",
        )
    return None


def _with_refinement_note(outcome: AnalysisOutcome, used_refinement: bool) -> AnalysisOutcome:
    if not used_refinement or outcome.status == "ambiguous":
        return outcome
    return outcome.model_copy(
        update={
            "detail": (
                f"{outcome.detail}；相关同 K 线触发顺序已使用完整 1 分钟 K 线细分"
            )
        }
    )


def evaluate_outcome(
    analysis: MarketAnalysis,
    bars: list[Kline],
    *,
    minute_refinements: dict[datetime, list[Kline]] | None = None,
) -> AnalysisOutcome:
    if analysis.direction == "neutral":
        return AnalysisOutcome(
            status="neutral_observation",
            bars_observed=len(bars),
            detail="观望分析没有入场、止损或目标位结果",
        )
    state = _OutcomeState()
    refinements = minute_refinements or {}
    used_refinement = False
    for index, bar in enumerate(bars, start=1):
        refinement = refinements.get(bar.open_time)
        evaluation_bars = refinement or [bar]
        interval_label = "1 分钟" if refinement else "5 分钟"
        used_refinement = used_refinement or refinement is not None
        for evaluation_bar in evaluation_bars:
            outcome = _evaluate_bar(
                analysis,
                state,
                evaluation_bar,
                bars_observed=index,
                interval_label=interval_label,
            )
            if outcome is not None:
                return _with_refinement_note(outcome, used_refinement)
    if state.phase == "waiting":
        status = "waiting_entry"
        detail = "分析完成后的完整 5 分钟 K 线尚未触及入场价"
    elif state.phase == "active":
        status = "active"
        detail = "计划已入场，尚未触及结构止损或 T1"
    else:
        status = "target1_partial"
        detail = "已记录 T1 部分止盈，剩余仓位尚未触及保本价或 T2"
    return _with_refinement_note(
        AnalysisOutcome(
            status=status,
            bars_observed=len(bars),
            entry_at=state.entry_at,
            target1_at=state.target1_at,
            detail=detail,
        ),
        used_refinement,
    )


def parse_closed_rows(
    rows: list[list[Any]], *, now: datetime | None = None
) -> list[Kline]:
    now_ms = int((now or datetime.now(UTC)).timestamp() * 1000)
    return [item for row in rows if (item := Kline.from_binance(row, now_ms=now_ms)).closed]


def _five_minute_window(open_time: datetime) -> datetime:
    open_time = open_time.astimezone(UTC)
    return open_time.replace(minute=(open_time.minute // 5) * 5, second=0, microsecond=0)


def _complete_minute_window(bars: list[Kline], start: datetime) -> bool:
    expected = [start + timedelta(minutes=offset) for offset in range(5)]
    return len(bars) == 5 and [bar.open_time for bar in bars] == expected


class HistoricalKlineSource(Protocol):
    async def historical_klines(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        *,
        max_candles: int = 10_000,
    ) -> list[list[Any]]: ...


async def evaluate_outcome_from_market(
    market: HistoricalKlineSource,
    *,
    symbol: str,
    analysis: MarketAnalysis,
    completed_at: datetime,
    end: datetime | None = None,
) -> AnalysisOutcome:
    if analysis.direction == "neutral":
        return evaluate_outcome(analysis, [])
    start = next_complete_5m_start(completed_at)
    end = (end or datetime.now(UTC)).astimezone(UTC)
    bars: list[Kline] = []
    if end > start:
        rows = await market.historical_klines(
            symbol, "5m", start, end, max_candles=100_000
        )
        bars = parse_closed_rows(rows, now=end)

    refinements: dict[datetime, list[Kline]] = {}
    while True:
        outcome = evaluate_outcome(
            analysis,
            bars,
            minute_refinements=refinements,
        )
        if outcome.status != "ambiguous" or outcome.resolved_at is None:
            return outcome
        window_start = _five_minute_window(outcome.resolved_at)
        if window_start in refinements:
            return outcome
        rows = await market.historical_klines(
            symbol,
            "1m",
            window_start,
            window_start + timedelta(minutes=5),
            max_candles=5,
        )
        minute_bars = parse_closed_rows(rows, now=end)
        if not _complete_minute_window(minute_bars, window_start):
            return outcome.model_copy(
                update={
                    "detail": (
                        f"{outcome.detail}；对应完整 1 分钟 K 线不足，暂无法细分"
                    )
                }
            )
        refinements[window_start] = minute_bars
