from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any


ANALYSIS_INTERVAL = timedelta(minutes=15)
AnalysisRound = Callable[[], Awaitable[dict[str, Any]]]


def next_analysis_boundary(now: datetime) -> datetime:
    now = now.astimezone(UTC)
    boundary = now.replace(
        minute=(now.minute // 15) * 15,
        second=0,
        microsecond=0,
    )
    return boundary + ANALYSIS_INTERVAL


class MarketAnalysisScheduler:
    """Run advisory candidate rounds on UTC quarter-hour boundaries."""

    def __init__(
        self,
        run_round: AnalysisRound,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._run_callback = run_round
        self._clock = clock or (lambda: datetime.now(UTC))
        self._loop_task: asyncio.Task[None] | None = None
        self._round_task: asyncio.Task[None] | None = None
        self._closing = False
        self.next_run_at: datetime | None = None
        self.last_started_at: datetime | None = None
        self.last_finished_at: datetime | None = None
        self.last_error: str | None = None
        self.last_result: dict[str, Any] | None = None

    @property
    def enabled(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    @property
    def round_running(self) -> bool:
        return self._round_task is not None and not self._round_task.done()

    def start(self) -> None:
        if self.enabled:
            raise RuntimeError("automatic market analysis is already enabled")
        self._closing = False
        self.next_run_at = next_analysis_boundary(self._clock())
        self._loop_task = asyncio.create_task(
            self._run_loop(),
            name="candlepilot-market-analysis-scheduler",
        )

    async def stop(self) -> None:
        task = self._loop_task
        self._loop_task = None
        self.next_run_at = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        self._closing = True
        await self.stop()
        task = self._round_task
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run_now(self) -> None:
        """Start one round for tests and the internal boundary loop."""

        if self.round_running:
            raise RuntimeError("an automatic market analysis round is already running")
        self._round_task = asyncio.create_task(
            self._execute_round(),
            name="candlepilot-market-analysis-round",
        )
        await self._round_task

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "interval_minutes": 15,
            "round_running": self.round_running,
            "next_run_at": self.next_run_at,
            "last_started_at": self.last_started_at,
            "last_finished_at": self.last_finished_at,
            "last_error": self.last_error,
            "last_result": self.last_result,
        }

    async def _run_loop(self) -> None:
        try:
            while True:
                target = self.next_run_at or next_analysis_boundary(self._clock())
                delay = max(0.0, (target - self._clock()).total_seconds())
                await asyncio.sleep(delay)
                now = self._clock().astimezone(UTC)
                next_target = target + ANALYSIS_INTERVAL
                while next_target <= now:
                    next_target += ANALYSIS_INTERVAL
                self.next_run_at = next_target
                if self.round_running:
                    self.last_error = "上一轮仍在运行，本次 15 分钟边界已跳过"
                    continue
                self._round_task = asyncio.create_task(
                    self._execute_round(),
                    name="candlepilot-market-analysis-round",
                )
        except asyncio.CancelledError:
            raise

    async def _execute_round(self) -> None:
        self.last_started_at = self._clock().astimezone(UTC)
        self.last_finished_at = None
        self.last_error = None
        try:
            self.last_result = await self._run_callback()
        except asyncio.CancelledError:
            if not self._closing:
                self.last_error = "本轮自动分析已取消"
                return
            raise
        except Exception as exc:
            self.last_error = str(exc)[:1000]
        finally:
            self.last_finished_at = self._clock().astimezone(UTC)
