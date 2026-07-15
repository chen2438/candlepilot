from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from candlepilot.application.engine import DecisionOutcome, TradingEngine
from candlepilot.application.paper_feed import PaperMarketFeed
from candlepilot.application.testnet_feed import TestnetUserFeed
from candlepilot.domain.models import PortfolioState, TradingMode
from candlepilot.market.binance import BinancePublicClient


CADENCE_SECONDS = {"1m": 60, "5m": 300, "15m": 900}

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
        paper_feed: PaperMarketFeed | None = None,
        testnet_feed: TestnetUserFeed | None = None,
    ) -> None:
        if universe_refresh_seconds <= 0:
            raise ValueError("universe_refresh_seconds must be positive")
        self.engine = engine
        self.market = market
        self.candidates_per_cycle = _normalize_candidates_per_cycle(candidates_per_cycle)
        self.universe_refresh_seconds = universe_refresh_seconds
        self.paper_feed = paper_feed
        self.testnet_feed = testnet_feed
        self._tasks: list[asyncio.Task[None]] = []
        self._symbol_locks: dict[str, asyncio.Lock] = {}
        self._stop = asyncio.Event()
        self.last_error: str | None = None
        self.universe_last_error: str | None = None

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

    async def stop(self) -> None:
        self._stop.set()
        if self.paper_feed is not None:
            await self.paper_feed.stop()
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
                await self.run_cycle(cadence)
                self.last_error = None
            except Exception as exc:
                self.last_error = f"{cadence}: {exc}"

    async def _run_universe(self) -> None:
        while not self._stop.is_set():
            if self.engine.running:
                try:
                    await self.engine.refresh_universe()
                    await self.sync_market_feed()
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

    async def sync_market_feed(self) -> None:
        if self.paper_feed is None or self.engine.mode != TradingMode.PAPER:
            return
        symbols = [candidate.symbol for candidate in self.engine.candidates]
        symbols.extend(self.engine.paper_executor.position_symbols)
        await self.paper_feed.start(list(dict.fromkeys(symbols)))

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
        symbols.extend(portfolio.symbol_sides)
        outcomes = []
        for symbol in dict.fromkeys(symbols):
            contract = contracts.get(symbol)
            if contract is None:
                continue
            lock = self._symbol_locks.setdefault(symbol, asyncio.Lock())
            async with lock:
                snapshot = await self.market.market_snapshot(symbol, cadence)
                if self.engine.mode in {TradingMode.PAPER, TradingMode.BACKTEST}:
                    protective_reports = await self.engine.paper_executor.mark_to_market(snapshot)
                    for report in protective_reports:
                        await self.engine.audit.record_execution(symbol, report)
                portfolio = await self._portfolio()
                outcomes.append(
                    await self.engine.evaluate(snapshot, portfolio, contract.rules)
                )
        return outcomes

    async def _portfolio(self) -> PortfolioState:
        return await self.engine.current_portfolio()
