from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime

from candlepilot.broker.user_stream import BinanceTestnetUserStream, UserStreamEvent
from candlepilot.storage.database import AuditRepository


class TestnetUserFeed:
    """Runs the private testnet stream and appends every supported event to the audit log."""

    __test__ = False

    def __init__(
        self,
        stream: BinanceTestnetUserStream,
        audit: AuditRepository,
        *,
        event_handler: Callable[[UserStreamEvent], Awaitable[None]] | None = None,
    ) -> None:
        self.stream = stream
        self.audit = audit
        self.event_handler = event_handler
        self.event_count = 0
        self.last_event_at: datetime | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._run(), name="candlepilot-testnet-user-stream")

    async def _run(self) -> None:
        try:
            async for event in self.stream.events():
                await self.process(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)

    async def process(self, event: UserStreamEvent) -> None:
        audit_error: str | None = None
        try:
            await self.audit.record_user_event(event)
        except Exception as exc:
            # Partial-fill handling is the safety-critical consumer. A temporary
            # audit write failure must not prevent it from cancelling the rest of
            # an entry order.
            audit_error = f"user event audit failed: {type(exc).__name__}"
        self.event_count += 1
        self.last_event_at = event.event_time
        if self.event_handler is not None:
            try:
                await self.event_handler(event)
            except Exception as exc:
                self.last_error = f"user event handling failed: {exc}"
                raise RuntimeError(self.last_error) from exc
        self.last_error = audit_error

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        await self.stream.stop()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=2)
        except TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._task = None

    async def close(self) -> None:
        await self.stop()
        await self.stream.close()
