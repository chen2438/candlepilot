from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, AsyncContextManager

from websockets.asyncio.client import connect


BINANCE_FUTURES_WS = "wss://fstream.binance.com"
SUPPORTED_INTERVALS = {"1m", "5m", "15m"}


@dataclass(frozen=True, slots=True)
class MarketStreamEvent:
    stream: str
    event_type: str
    symbol: str
    event_time: datetime
    payload: dict[str, Any]


ConnectionFactory = Callable[[str], AsyncContextManager[Any]]


class BinanceMarketStream:
    """Reconnectable read-only combined stream for USD-M public market events."""

    def __init__(
        self,
        symbols: list[str],
        *,
        intervals: tuple[str, ...] = ("1m", "5m", "15m"),
        base_url: str = BINANCE_FUTURES_WS,
        connection_factory: ConnectionFactory | None = None,
        reconnect_initial: float = 0.5,
        reconnect_maximum: float = 30,
    ) -> None:
        normalized = tuple(dict.fromkeys(symbol.upper() for symbol in symbols))
        if not normalized:
            raise ValueError("at least one stream symbol is required")
        if any(not symbol.isalnum() or not symbol.endswith("USDT") for symbol in normalized):
            raise ValueError("stream symbols must be USDT contracts")
        if not intervals or any(interval not in SUPPORTED_INTERVALS for interval in intervals):
            raise ValueError("unsupported stream interval")
        if reconnect_initial < 0 or reconnect_maximum < reconnect_initial:
            raise ValueError("invalid reconnect delay")
        self.symbols = normalized
        self.intervals = intervals
        self.base_url = base_url.rstrip("/")
        self._connection_factory = connection_factory or self._connection
        self.reconnect_initial = reconnect_initial
        self.reconnect_maximum = reconnect_maximum
        self._stop = asyncio.Event()
        self._socket: Any | None = None
        self.last_error: str | None = None
        self.reconnect_count = 0

    @property
    def streams(self) -> tuple[str, ...]:
        names: list[str] = []
        for symbol in self.symbols:
            lower = symbol.lower()
            names.extend(f"{lower}@kline_{interval}" for interval in self.intervals)
            names.extend(
                (
                    f"{lower}@markPrice@1s",
                    f"{lower}@bookTicker",
                    f"{lower}@ticker",
                )
            )
        return tuple(names)

    @property
    def url(self) -> str:
        return f"{self.base_url}/stream?streams={'/'.join(self.streams)}"

    def _connection(self, url: str) -> AsyncContextManager[Any]:
        return connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_queue=2048,
        )

    async def events(self) -> AsyncIterator[MarketStreamEvent]:
        delay = self.reconnect_initial
        self._stop.clear()
        while not self._stop.is_set():
            try:
                async with self._connection_factory(self.url) as socket:
                    self._socket = socket
                    self.last_error = None
                    delay = self.reconnect_initial
                    async for message in socket:
                        if self._stop.is_set():
                            return
                        yield self.parse_message(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._stop.is_set():
                    return
                self.last_error = str(exc)
                self.reconnect_count += 1
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=delay)
                except TimeoutError:
                    delay = min(self.reconnect_maximum, max(delay * 2, 0.01))
            finally:
                self._socket = None

    async def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            await self._socket.close()

    @staticmethod
    def parse_message(message: str | bytes) -> MarketStreamEvent:
        try:
            envelope = json.loads(message)
            payload = envelope.get("data", envelope)
            stream = envelope.get("stream", "")
            event_type = str(payload["e"])
            symbol = str(payload["s"])
            event_time = datetime.fromtimestamp(int(payload["E"]) / 1000, tz=UTC)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid Binance stream message: {exc}") from exc
        return MarketStreamEvent(
            stream=stream,
            event_type=event_type,
            symbol=symbol,
            event_time=event_time,
            payload=payload,
        )
