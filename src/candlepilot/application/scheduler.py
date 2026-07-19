from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from candlepilot.application.engine import DecisionOutcome, TradingEngine
from candlepilot.application.testnet_feed import TestnetUserFeed
from candlepilot.domain.models import PortfolioState
from candlepilot.market.binance import BinancePublicClient


CADENCE_SECONDS = {"5m": 300, "15m": 900, "30m": 1_800, "1h": 3_600, "4h": 14_400}

DEFAULT_CANDIDATES_PER_CYCLE = 5
MAX_CANDIDATES_PER_CYCLE = 20


def _normalize_candidates_per_cycle(value: int) -> int:
    if value < 1:
        raise ValueError("candidates_per_cycle must be at least 1")
    if value > MAX_CANDIDATES_PER_CYCLE:
        raise ValueError(
            f"candidates_per_cycle must be at most {MAX_CANDIDATES_PER_CYCLE}"
        )
    return value


class TradingScheduler:
    def __init__(
        self,
        engine: TradingEngine,
        market: BinancePublicClient,
        *,
        candidates_per_cycle: int = DEFAULT_CANDIDATES_PER_CYCLE,
        universe_refresh_seconds: float = 60,
        guard_interval_seconds: float = 5,
        run_cost_loader: Callable[[], Awaitable[float | None]] | None = None,
        testnet_feed: TestnetUserFeed | None = None,
    ) -> None:
        if universe_refresh_seconds <= 0:
            raise ValueError("universe_refresh_seconds must be positive")
        if guard_interval_seconds <= 0:
            raise ValueError("guard_interval_seconds must be positive")
        self.engine = engine
        self.market = market
        self.candidates_per_cycle = _normalize_candidates_per_cycle(candidates_per_cycle)
        self.universe_refresh_seconds = universe_refresh_seconds
        self.guard_interval_seconds = guard_interval_seconds
        self.run_cost_loader = run_cost_loader
        self.testnet_feed = testnet_feed
        self._tasks: list[asyncio.Task[None]] = []
        self._symbol_locks: dict[str, asyncio.Lock] = {}
        self._stop = asyncio.Event()
        self._auto_stop_task: asyncio.Task[None] | None = None
        self.cadence_errors: dict[str, str] = {}
        self.universe_last_error: str | None = None
        self.guard_last_error: str | None = None
        self.current_cycles: dict[str, dict[str, object]] = {}
        self.last_cycle: dict[str, object] | None = None

    @property
    def current_cycle(self) -> dict[str, object] | None:
        """Return one active cycle for the compact status API.

        Cadence tasks are independent, so internal tracking remains keyed by
        cadence even though the current frontend only has room for one row.
        """

        return next(iter(self.current_cycles.values()), None)

    @property
    def last_error(self) -> str | None:
        return "; ".join(self.cadence_errors.values()) or None

    def select_candidates_per_cycle(self, value: int) -> None:
        if self.engine.running:
            raise RuntimeError("cannot change candidates_per_cycle while running")
        self.candidates_per_cycle = _normalize_candidates_per_cycle(value)

    def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        if self.testnet_feed is not None:
            self.testnet_feed.start()
        active = [cadence for cadence in CADENCE_SECONDS if cadence in self.engine.active_cadences]
        self._tasks = [
            asyncio.create_task(self._run_cadence(cadence), name=f"candlepilot-{cadence}")
            for cadence in active
        ]
        self._tasks.append(
            asyncio.create_task(self._run_universe(), name="candlepilot-universe")
        )
        self._tasks.append(
            asyncio.create_task(self._run_guard(), name="candlepilot-guard")
        )

    async def stop(self) -> None:
        self._stop.set()
        if self.testnet_feed is not None:
            await self.testnet_feed.stop()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def _run_cadence(self, cadence: str) -> None:
        seconds = CADENCE_SECONDS[cadence]
        while not self._stop.is_set():
            now = datetime.now(UTC).timestamp()
            delay = seconds - (now % seconds) + 0.25
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            if not self.engine.running:
                continue
            try:
                async with asyncio.timeout(max(1.0, seconds - 0.5)):
                    await self.run_cycle(cadence)
                self.cadence_errors.pop(cadence, None)
            except TimeoutError:
                self.cadence_errors[cadence] = (
                    f"{cadence}: cycle exceeded its {seconds:g}s cadence budget and was cancelled"
                )
            except Exception as exc:
                self.cadence_errors[cadence] = f"{cadence}: {exc}"

    async def _run_universe(self) -> None:
        while not self._stop.is_set():
            if self.engine.running:
                try:
                    await self.engine.refresh_universe()
                    self.universe_last_error = None
                except Exception as exc:
                    self.universe_last_error = str(exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.universe_refresh_seconds
                )
                return
            except TimeoutError:
                pass

    async def _run_guard(self) -> None:
        """Stop the run once a limit is hit or every provider route has failed.

        Polls independently of the cadence timers so a long cadence cannot delay a
        duration or budget stop by hours.
        """

        while not self._stop.is_set():
            if self.engine.running:
                try:
                    if self.testnet_feed is not None and not self.testnet_feed.running:
                        self.request_emergency_stop(
                            "testnet user stream stopped; account safety state is unknown"
                        )
                        return
                    reason = self.engine.evaluate_stop_reason(
                        run_cost_usd=await self._run_cost()
                    )
                    self.guard_last_error = None
                    if reason is not None:
                        self.request_auto_stop(reason)
                        return
                except Exception as exc:
                    self.guard_last_error = str(exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.guard_interval_seconds
                )
                return
            except TimeoutError:
                pass

    async def _run_cost(self) -> float | None:
        if self.run_cost_loader is None or self.engine.max_run_cost_usd is None:
            return None
        return await self.run_cost_loader()

    def request_auto_stop(self, reason: str) -> None:
        """Gracefully stop the run from inside a scheduler task.

        The stop runs in a detached task: ``stop()`` cancels and gathers
        ``self._tasks``, so a tracked task cannot await its own cancellation.
        """

        if self._auto_stop_task is not None and not self._auto_stop_task.done():
            return
        self.engine.auto_stop_reason = reason
        self._auto_stop_task = asyncio.create_task(
            self._auto_stop(), name="candlepilot-auto-stop"
        )

    def request_emergency_stop(self, reason: str) -> None:
        if self._auto_stop_task is not None and not self._auto_stop_task.done():
            return
        self.engine.auto_stop_reason = reason
        self._auto_stop_task = asyncio.create_task(
            self._auto_emergency_stop(), name="candlepilot-auto-emergency-stop"
        )

    async def _auto_stop(self) -> None:
        await self.engine.stop()
        await self.stop()

    async def _auto_emergency_stop(self) -> None:
        try:
            await self.stop()
        finally:
            # No decision task may survive the account flatten.  Flattening first
            # leaves a window in which an in-flight provider call can return and
            # submit a fresh entry after the broker has already closed exposure.
            await self.engine.emergency_stop()

    async def run_cycle(self, cadence: str) -> list[DecisionOutcome]:
        if cadence not in CADENCE_SECONDS:
            raise ValueError("unsupported cadence")
        if not self.engine.running:
            return []
        if not self.engine.candidates:
            await self.engine.refresh_universe()
        contracts = await self.market.exchange_info()
        portfolio = await self._portfolio()
        symbols = [
            candidate.symbol
            for candidate in self.engine.candidates[: self.candidates_per_cycle]
        ]
        symbols.extend(portfolio.positions)
        ordered_symbols = list(dict.fromkeys(symbols))
        started_at = datetime.now(UTC)
        cycle_state: dict[str, object] = {
            "cadence": cadence,
            "started_at": started_at.isoformat(),
            "symbol": None,
            "symbol_started_at": None,
            "stage": "preparing",
            "completed": 0,
            "total": len(ordered_symbols),
        }
        self.current_cycles[cadence] = cycle_state
        outcomes = []
        try:
            for symbol in ordered_symbols:
                if not self.engine.running or self.engine.auto_stop_reason is not None:
                    break
                contract = contracts.get(symbol)
                if contract is None:
                    continue
                cycle_state.update(
                    symbol=symbol,
                    symbol_started_at=datetime.now(UTC).isoformat(),
                    stage="market_snapshot",
                )
                lock = self._symbol_locks.setdefault(symbol, asyncio.Lock())
                async with lock:
                    snapshot = await self.market.market_snapshot(symbol, cadence)
                    cycle_state["stage"] = "portfolio"
                    portfolio = await self._portfolio()
                    cycle_state["stage"] = "decision"
                    outcome = await self.engine.evaluate(snapshot, portfolio, contract.rules)
                    outcomes.append(outcome)
                cycle_state["completed"] = int(cycle_state["completed"]) + 1
                stop_reason = self.engine.evaluate_stop_reason()
                if stop_reason is not None:
                    self.request_auto_stop(stop_reason)
                    break
            return outcomes
        finally:
            self.last_cycle = {
                **cycle_state,
                "ended_at": datetime.now(UTC).isoformat(),
            }
            self.current_cycles.pop(cadence, None)

    async def _portfolio(self) -> PortfolioState:
        return await self.engine.current_portfolio()
