"""Replays a window through one or more models and compares what they did.

Cost is the shaping constraint. A decision is one LLM call, calls inside a
provider are serialised by its own semaphore, and a real call takes tens of
seconds -- so a day of 5m bars on one symbol is hours, not seconds. Hence:
estimate before running, run in the background, and report progress.
"""

from __future__ import annotations

import asyncio
import copy
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from candlepilot.backtest.engine import (
    BacktestConfig,
    BacktestResult,
    Candle,
    EquityPoint,
    SimulatedExchange,
    summarize,
)
from candlepilot.backtest.snapshots import INTERVAL_MILLISECONDS, HistoricalSnapshotBuilder
from candlepilot.domain.models import TradeAction
from candlepilot.providers.base import LLMProvider, ProviderResult
from candlepilot.providers.retry import (
    DECISION_PROVIDER_MAX_ATTEMPTS,
    DECISION_PROVIDER_RETRY_DELAYS,
    validate_retry_delays,
)
from candlepilot.risk.engine import AggressiveRiskPolicy, SymbolRules

MAX_BACKTEST_SYMBOLS = 5
MAX_BACKTEST_MODELS = 4
MAX_BACKTEST_DAYS = 31
#: Refuse a window that would still be running tomorrow.
#:
#: The limit is on wall clock, not call count: the same 2,592 calls are 20
#: minutes against a fast endpoint and 17 hours against a slow one, so a call
#: cap would be far too loose for one and needlessly tight for the other. This
#: is measured against the install's own latency.
MAX_ESTIMATED_HOURS = 8.0

@dataclass(frozen=True, slots=True)
class BacktestSpec:
    symbols: tuple[str, ...]
    cadences: tuple[str, ...]
    start: datetime
    end: datetime
    providers: tuple[str, ...]
    config: BacktestConfig = field(default_factory=BacktestConfig)
    #: Use the recorded order book, making the payload identical to live.
    #:
    #: Only possible where the collector was running, so the window is checked
    #: for full coverage up front and refused if it has holes.
    use_recorded_book: bool = False
    #: Seconds one decision may take, for this run only.
    #:
    #: The console sets it from a probe of the endpoints the run will use, since
    #: the global default is one number for providers that differ by minutes.
    #: None keeps whatever each provider was configured with.
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class BacktestEstimate:
    decisions_per_model: int
    total_calls: int
    #: Wall-clock for the slowest model, since models run in parallel and each
    #: one serialises its own calls.
    estimated_seconds: float

    def as_dict(self) -> dict[str, object]:
        return {
            "decisions_per_model": self.decisions_per_model,
            "total_calls": self.total_calls,
            "estimated_seconds": round(self.estimated_seconds),
            "estimated_hours": round(self.estimated_seconds / 3600, 2),
        }


def estimate(spec: BacktestSpec, *, seconds_per_call: float) -> BacktestEstimate:
    """Count the calls the spec implies before any of them are paid for."""

    span_ms = (spec.end - spec.start).total_seconds() * 1000
    per_model = sum(
        int(span_ms // INTERVAL_MILLISECONDS[cadence]) * len(spec.symbols)
        for cadence in spec.cadences
    )
    return BacktestEstimate(
        decisions_per_model=per_model,
        total_calls=per_model * len(spec.providers),
        estimated_seconds=per_model * seconds_per_call,
    )


def validate(spec: BacktestSpec) -> None:
    """Reject a spec that cannot finish, before it burns a single call."""

    if not spec.symbols or len(spec.symbols) > MAX_BACKTEST_SYMBOLS:
        raise ValueError(f"choose between 1 and {MAX_BACKTEST_SYMBOLS} symbols")
    if not spec.providers or len(spec.providers) > MAX_BACKTEST_MODELS:
        raise ValueError(f"choose between 1 and {MAX_BACKTEST_MODELS} models")
    if len(set(spec.providers)) != len(spec.providers):
        raise ValueError("a model cannot be compared against itself")
    if not spec.cadences:
        raise ValueError("choose at least one cadence")
    if spec.end <= spec.start:
        raise ValueError("the window must end after it starts")
    if spec.end - spec.start > timedelta(days=MAX_BACKTEST_DAYS):
        raise ValueError(f"the window cannot exceed {MAX_BACKTEST_DAYS} days")
    if spec.end > datetime.now(UTC):
        raise ValueError("the window cannot reach into the future")


@dataclass(frozen=True, slots=True)
class BacktestLiveStats:
    """Provisional headline metrics without pretending close-out happened."""

    equity: Decimal
    unrealized_pnl: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    win_rate: Decimal
    trade_count: int


@dataclass
class ModelRun:
    """One model's pass over the window."""

    provider: str
    decisions_done: int = 0
    decisions_total: int = 0
    calls_failed: int = 0
    usage_calls: int = 0
    priced_calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cost_usd_total: float = 0.0
    duration_ms_total: float = 0.0
    result: BacktestResult | None = None
    error: str | None = None
    provider_failed: bool = False
    last_successful_at: datetime | None = None
    elapsed_seconds: float = 0.0
    remaining_seconds: float | None = None
    live_result: BacktestLiveStats | None = None

    @property
    def equivalent_cost_usd(self) -> float | None:
        """Complete equivalent cost, never a misleading partial subtotal."""

        if not self.usage_calls or self.priced_calls != self.usage_calls:
            return None
        return self.cost_usd_total

    def record_usage(
        self, usage: dict[str, Any], cost_usd: float | None, duration_ms: float = 0.0
    ) -> None:
        """Accumulate one completed provider call for live progress reporting."""

        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        self.usage_calls += 1
        self.input_tokens += input_tokens
        self.cached_input_tokens += int(
            usage.get("cached_input_tokens") or usage.get("cache_read_input_tokens") or 0
        )
        self.cache_creation_input_tokens += int(
            usage.get("cache_creation_input_tokens") or 0
        )
        self.output_tokens += output_tokens
        self.total_tokens += int(usage.get("total_tokens") or input_tokens + output_tokens)
        self.duration_ms_total += max(0.0, duration_ms)
        if cost_usd is not None:
            self.priced_calls += 1
            self.cost_usd_total += cost_usd

    def usage_dict(self) -> dict[str, int | float | None]:
        return {
            "call_count": self.usage_calls,
            "priced_call_count": self.priced_calls,
            "input_tokens": self.input_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "equivalent_cost_usd": self.equivalent_cost_usd,
            "duration_ms_total": self.duration_ms_total,
            "average_duration_ms": (
                self.duration_ms_total / self.usage_calls if self.usage_calls else 0.0
            ),
        }

    @property
    def progress(self) -> float:
        if not self.decisions_total:
            return 0.0
        return self.decisions_done / self.decisions_total

    def update_timing(self, elapsed_seconds: float) -> None:
        """Infer remaining wall time from this run's observed throughput."""

        self.elapsed_seconds = max(0.0, elapsed_seconds)
        if self.decisions_done <= 0 or self.decisions_total <= 0:
            self.remaining_seconds = None
            return
        remaining = max(0, self.decisions_total - self.decisions_done)
        self.remaining_seconds = self.elapsed_seconds / self.decisions_done * remaining

@dataclass
class BacktestDecision:
    """What one model did at one instant, and what came of it.

    The run's totals cannot answer "why zero trades": a model that held all
    day, one the risk policy vetoed every time, and one whose calls timed out
    all report the same zero. `outcome` is the field that separates them.
    """

    decided_at: datetime
    symbol: str
    cadence: str
    #: traded | pending | rejected | hold | no_snapshot | call_failed
    outcome: str = "hold"
    action: str | None = None
    confidence: float | None = None
    rationale: str | None = None
    detail: str | None = None
    fill: dict[str, Any] | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "decided_at": self.decided_at,
            "symbol": self.symbol,
            "cadence": self.cadence,
            "outcome": self.outcome,
            "action": self.action,
            "confidence": self.confidence,
            "rationale": self.rationale,
            "detail": self.detail,
            "fill": self.fill,
        }


def decision_times(spec: BacktestSpec, cadence: str) -> list[datetime]:
    """When each decision is due: the close of every bar inside the window."""

    step = timedelta(milliseconds=INTERVAL_MILLISECONDS[cadence])
    times: list[datetime] = []
    cursor = spec.start + step
    while cursor <= spec.end:
        times.append(cursor)
        cursor += step
    return times


class BacktestRunner:
    """Replays a spec for one model, against the real risk policy."""

    def __init__(
        self,
        *,
        spec: BacktestSpec,
        series: dict[str, dict[str, list[Candle]]],
        rules: dict[str, SymbolRules],
        risk: AggressiveRiskPolicy,
        captures: dict[str, dict[datetime, dict[str, Any]]] | None = None,
        cost_for_result: Callable[[ProviderResult], float | None] | None = None,
        provider_retry_delays: tuple[float, ...] | None = None,
        retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._spec = spec
        self._series = series
        self._rules = rules
        self._risk = risk
        self._cost_for_result = cost_for_result
        self._provider_retry_delays = validate_retry_delays(
            DECISION_PROVIDER_RETRY_DELAYS
            if provider_retry_delays is None
            else provider_retry_delays
        )
        self._retry_sleep = retry_sleep
        self._builders = {
            symbol: HistoricalSnapshotBuilder(candles, (captures or {}).get(symbol))
            for symbol, candles in series.items()
        }

    def _next_candle(self, symbol: str, cadence: str, after: datetime) -> Candle | None:
        for candle in self._series[symbol][cadence]:
            if candle.timestamp >= after:
                return candle
        return None

    def _marks(self, at: datetime) -> dict[str, Decimal]:
        marks: dict[str, Decimal] = {}
        for symbol, candles in self._series.items():
            span = timedelta(milliseconds=INTERVAL_MILLISECONDS["5m"])
            usable = [item for item in candles["5m"] if item.timestamp + span <= at]
            if usable:
                marks[symbol] = usable[-1].close
        return marks

    def _settle_until(
        self,
        exchange: SimulatedExchange,
        symbol: str,
        through: datetime,
        settled_through: dict[str, datetime],
    ) -> None:
        """Settle each completed 5m bar exactly once before decisions at ``through``."""

        span = timedelta(milliseconds=INTERVAL_MILLISECONDS["5m"])
        previous = settled_through.get(symbol)
        for candle in self._series[symbol]["5m"]:
            if candle.timestamp < self._spec.start:
                continue
            if previous is not None and candle.timestamp <= previous:
                continue
            if candle.timestamp + span > through:
                break
            exchange.settle_candle(symbol, candle)
            settled_through[symbol] = candle.timestamp

    async def run(
        self,
        provider: LLMProvider,
        progress: ModelRun,
        *,
        on_progress: Callable[[ModelRun, BacktestDecision | None], Awaitable[None]]
        | None = None,
    ) -> BacktestResult:
        """Replay the window. ``on_progress`` is awaited after each decision.

        It has to be reported from inside this loop: a caller that only looks
        once the run returns learns nothing while the run is the thing it wants
        to watch, and every decision here waits on a model call, so one small
        write per decision costs nothing next to it.

        The decision travels with the progress rather than on a hook of its
        own: they are produced together and written in the same trip, and two
        callbacks would let a reader see a counter move with no decision behind
        it.
        """

        exchange = SimulatedExchange(self._spec.config)
        started_at = time.monotonic()
        curve: list[EquityPoint] = []
        schedule = sorted(
            (when, symbol, cadence)
            for cadence in self._spec.cadences
            for when in decision_times(self._spec, cadence)
            for symbol in self._spec.symbols
        )
        settled_through: dict[str, datetime] = {}
        settled_decision_key: tuple[datetime, str] | None = None
        last_success_exchange = copy.deepcopy(exchange)
        previous_call_succeeded = False
        progress.decisions_total = len(schedule)
        # Publish the total before the first call: until it lands, progress has
        # no denominator and every reader has to show 0%.
        if on_progress is not None:
            await on_progress(progress, None)

        async def report(decision: BacktestDecision | None = None) -> None:
            progress.update_timing(time.monotonic() - started_at)
            if decision is not None:
                live_at = decision.decided_at
                if progress.provider_failed:
                    live_at = progress.last_successful_at or self._spec.start
                marks = self._marks(live_at)
                portfolio = exchange.portfolio_state(marks, as_of=live_at)
                live_equity = EquityPoint(live_at, portfolio.equity)
                if not curve or curve[-1] != live_equity:
                    curve.append(live_equity)
                # No close-out is performed here. final_equity/return therefore
                # include open-position PnL at the latest mark, while win rate
                # and trade count remain based on trades actually closed so far.
                live_summary = summarize(
                    self._spec.config, list(exchange.trades), curve
                )
                progress.live_result = BacktestLiveStats(
                    equity=portfolio.equity,
                    unrealized_pnl=sum(
                        (position.unrealized_pnl for position in portfolio.positions.values()),
                        Decimal("0"),
                    ),
                    total_return=live_summary.total_return,
                    max_drawdown=live_summary.max_drawdown,
                    win_rate=live_summary.win_rate,
                    trade_count=live_summary.trade_count,
                )
            if on_progress is not None:
                await on_progress(progress, decision)

        for when, symbol, cadence in schedule:
            if previous_call_succeeded:
                # Capture the fully processed result of the preceding successful
                # call before this decision can settle any later market data.
                last_success_exchange = copy.deepcopy(exchange)
                previous_call_succeeded = False
            # Kline timestamps are opens. Settle only bars whose close is at or
            # before this decision, and only once even when several cadences
            # produce decisions for the same symbol at the same instant.
            decision_key = (when, symbol)
            if decision_key != settled_decision_key:
                self._settle_until(exchange, symbol, when, settled_through)
                settled_decision_key = decision_key

            entry = BacktestDecision(decided_at=when, symbol=symbol, cadence=cadence)

            try:
                snapshot = self._builders[symbol].build(symbol, cadence, when)
            except ValueError as exc:
                entry.outcome = "no_snapshot"
                entry.detail = str(exc)[:200]
                progress.decisions_done += 1
                await report(entry)
                continue

            portfolio = exchange.portfolio_state(self._marks(when), as_of=when)
            result: ProviderResult | None = None
            last_error: Exception | None = None
            for attempt in range(DECISION_PROVIDER_MAX_ATTEMPTS):
                try:
                    result = await provider.generate_trade_intent(snapshot, portfolio)
                    break
                except Exception as exc:  # noqa: BLE001 - retry the decision in place
                    last_error = exc
                    if attempt < DECISION_PROVIDER_MAX_ATTEMPTS - 1:
                        await self._retry_sleep(self._provider_retry_delays[attempt])

            if result is None:
                assert last_error is not None
                entry.outcome = "call_failed"
                entry.detail = (
                    "provider unavailable after "
                    f"{DECISION_PROVIDER_MAX_ATTEMPTS} attempts: {last_error}"
                )[:200]
                progress.calls_failed += 1
                progress.decisions_done += 1
                progress.error = entry.detail
                progress.provider_failed = True
                exchange = last_success_exchange
                await report(entry)
                break

            cost_usd = self._cost_for_result(result) if self._cost_for_result else None
            progress.record_usage(
                result.usage, cost_usd, result.duration.total_seconds() * 1000
            )

            progress.decisions_done += 1
            progress.last_successful_at = when
            previous_call_succeeded = True
            intent = result.intent
            entry.action = intent.action.value
            entry.confidence = intent.confidence
            entry.rationale = intent.rationale
            if intent.action == TradeAction.HOLD:
                entry.outcome = "hold"
                curve.append(EquityPoint(when, exchange.equity(self._marks(when))))
                await report(entry)
                continue
            if exchange.has_pending(symbol):
                entry.outcome = "rejected"
                entry.detail = "resting limit order already pending"
                curve.append(EquityPoint(when, exchange.equity(self._marks(when))))
                await report(entry)
                continue

            # The live risk policy, not a copy of it: the daily-loss breaker,
            # the position cap, tick alignment and exchange minimums all have to
            # bite here or the run scores a system nobody runs.
            evaluation = self._risk.evaluate(
                intent, snapshot, portfolio, self._rules[symbol], now=when
            )
            if evaluation.order is None or not evaluation.decision.accepted:
                entry.outcome = "rejected"
                entry.detail = evaluation.decision.reason[:200]
                curve.append(EquityPoint(when, exchange.equity(self._marks(when))))
                await report(entry)
                continue

            fill_candle = self._next_candle(symbol, "5m", when)
            if fill_candle is None or fill_candle.timestamp >= self._spec.end:
                # Accepted with no bar left to fill against: the window ended.
                entry.outcome = "rejected"
                entry.detail = "no candle left in the window to fill against"
                await report(entry)
                continue
            execution = exchange.execute(
                evaluation.order, fill_candle, leverage=intent.leverage
            )
            entry.outcome = "traded" if execution.status == "FILLED" else "pending"
            entry.fill = {
                "status": execution.status,
                "price": str(execution.average_price or evaluation.order.price),
                "quantity": str(evaluation.order.quantity),
                "side": evaluation.order.side,
                "leverage": intent.leverage,
                "stop_loss": str(evaluation.order.stop_price)
                if evaluation.order.stop_price is not None
                else None,
                "take_profit": str(evaluation.order.take_profit_price)
                if evaluation.order.take_profit_price is not None
                else None,
            }
            curve.append(EquityPoint(when, exchange.equity(self._marks(when))))
            await report(entry)

        effective_end = self._spec.end
        if progress.provider_failed:
            effective_end = progress.last_successful_at or self._spec.start
        if not progress.provider_failed:
            for symbol in self._spec.symbols:
                self._settle_until(exchange, symbol, effective_end, settled_through)
        cancelled_pending_orders = exchange.close_all(
            self._marks(effective_end), effective_end
        )
        curve.append(EquityPoint(effective_end, exchange.equity({})))
        return summarize(
            self._spec.config,
            exchange.trades,
            curve,
            cancelled_pending_orders=cancelled_pending_orders,
        )


async def compare(
    *,
    spec: BacktestSpec,
    runner_for: Callable[[str], BacktestRunner],
    provider_for: Callable[[str], LLMProvider],
    on_progress: Callable[[ModelRun, BacktestDecision | None], Awaitable[None]]
    | None = None,
) -> list[ModelRun]:
    """Run every model over the same window, concurrently.

    Calls inside one provider are serialised by that provider, so the models
    only contend with each other if they share one -- which the spec forbids.
    Wall-clock is therefore the slowest model, not the sum.

    ``on_progress`` is handed down to each runner, which awaits it per decision.
    Reporting only from here would fire it once, after a run that can take hours
    has already finished -- which is exactly what the console had to watch.
    """

    runs = [ModelRun(provider=name) for name in spec.providers]

    async def one(run: ModelRun) -> None:
        try:
            run.result = await runner_for(run.provider).run(
                provider_for(run.provider), run, on_progress=on_progress
            )
            run.live_result = BacktestLiveStats(
                equity=run.result.final_equity,
                unrealized_pnl=Decimal("0"),
                total_return=run.result.total_return,
                max_drawdown=run.result.max_drawdown,
                win_rate=run.result.win_rate,
                trade_count=run.result.trade_count,
            )
            run.remaining_seconds = 0.0
        except Exception as exc:  # noqa: BLE001 - report, do not sink the comparison
            run.error = str(exc)[:500]
        # The final report carries the result and any error, which the
        # per-decision ones cannot have seen.
        if on_progress is not None:
            await on_progress(run, None)

    tasks = {asyncio.create_task(one(run)): run for run in runs}
    pending = set(tasks)
    try:
        while pending:
            done, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED
            )
            failed = [tasks[task] for task in done if tasks[task].provider_failed]
            if not failed:
                continue
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            break
    finally:
        unfinished = [task for task in tasks if not task.done()]
        for task in unfinished:
            task.cancel()
        if unfinished:
            await asyncio.gather(*unfinished, return_exceptions=True)
    return runs
