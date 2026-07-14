from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, AsyncContextManager

import httpx
from websockets.asyncio.client import connect

from candlepilot.broker.binance_testnet import BinanceApiError, BinanceTestnetCredentials
from candlepilot.market.binance import BINANCE_FUTURES_TESTNET


BINANCE_FUTURES_TESTNET_WS = "wss://demo-fstream.binance.com"
SUPPORTED_USER_EVENTS = {"ACCOUNT_UPDATE", "ORDER_TRADE_UPDATE"}


@dataclass(frozen=True, slots=True)
class UserStreamEvent:
    event_type: str
    event_time: datetime
    transaction_time: datetime | None
    symbol: str | None
    payload: dict[str, Any]


class UserEventSequencer:
    """Reject duplicate or stale events independently for each Binance event type."""

    def __init__(self) -> None:
        self._last_event_time: dict[str, int] = {}
        self.dropped = 0

    def accept(self, event: UserStreamEvent) -> bool:
        event_ms = int(event.event_time.timestamp() * 1000)
        previous = self._last_event_time.get(event.event_type)
        if previous is not None and event_ms <= previous:
            self.dropped += 1
            return False
        self._last_event_time[event.event_type] = event_ms
        return True


ConnectionFactory = Callable[[str], AsyncContextManager[Any]]


class BinanceTestnetUserStream:
    """Reconnectable Binance USD-M demo user stream with listen-key renewal."""

    def __init__(
        self,
        credentials: BinanceTestnetCredentials,
        *,
        rest_base_url: str = BINANCE_FUTURES_TESTNET,
        websocket_base_url: str = BINANCE_FUTURES_TESTNET_WS,
        client: httpx.AsyncClient | None = None,
        connection_factory: ConnectionFactory | None = None,
        keepalive_interval: float = 30 * 60,
        reconnect_initial: float = 0.5,
        reconnect_maximum: float = 30,
    ) -> None:
        if rest_base_url.rstrip("/") != BINANCE_FUTURES_TESTNET:
            raise ValueError("user stream only permits the official futures demo REST endpoint")
        if websocket_base_url.rstrip("/") != BINANCE_FUTURES_TESTNET_WS:
            raise ValueError("user stream only permits the official futures demo WebSocket endpoint")
        if client is not None and str(client.base_url).rstrip("/") != BINANCE_FUTURES_TESTNET:
            raise ValueError("injected user stream client must use the futures demo REST endpoint")
        if keepalive_interval <= 0:
            raise ValueError("keepalive interval must be positive")
        if reconnect_initial < 0 or reconnect_maximum < reconnect_initial:
            raise ValueError("invalid reconnect delay")
        self.credentials = credentials
        self.websocket_base_url = websocket_base_url.rstrip("/")
        self.keepalive_interval = keepalive_interval
        self.reconnect_initial = reconnect_initial
        self.reconnect_maximum = reconnect_maximum
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=rest_base_url,
            timeout=httpx.Timeout(10),
            headers={"User-Agent": "CandlePilot/0.1"},
        )
        self._connection_factory = connection_factory or self._connection
        self._stop = asyncio.Event()
        self._socket: Any | None = None
        self._keepalive_error: Exception | None = None
        self.last_error: str | None = None
        self.reconnect_count = 0
        self.sequencer = UserEventSequencer()

    @property
    def dropped_event_count(self) -> int:
        return self.sequencer.dropped

    def _connection(self, url: str) -> AsyncContextManager[Any]:
        return connect(url, ping_interval=20, ping_timeout=20, close_timeout=5, max_queue=2048)

    async def _listen_key_request(
        self, method: str, listen_key: str | None = None
    ) -> str | None:
        params = {"listenKey": listen_key} if listen_key is not None else None
        response = await self._client.request(
            method,
            "/fapi/v1/listenKey",
            params=params,
            headers={"X-MBX-APIKEY": self.credentials.api_key.get_secret_value()},
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise BinanceApiError(0, response.text, response.status_code) from exc
        if response.is_error:
            raise BinanceApiError(
                int(payload.get("code", 0)),
                str(payload.get("msg", response.text)),
                response.status_code,
            )
        if method == "POST":
            key = payload.get("listenKey")
            if not isinstance(key, str) or not key:
                raise BinanceApiError(0, "listenKey missing from response", response.status_code)
            return key
        return None

    async def _keepalive(self, listen_key: str, socket: Any) -> None:
        try:
            while not self._stop.is_set():
                await asyncio.sleep(self.keepalive_interval)
                if self._stop.is_set():
                    return
                await self._listen_key_request("PUT", listen_key)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._keepalive_error = exc
            await socket.close()

    async def events(self) -> AsyncIterator[UserStreamEvent]:
        delay = self.reconnect_initial
        self._stop.clear()
        while not self._stop.is_set():
            listen_key: str | None = None
            keepalive_task: asyncio.Task[None] | None = None
            try:
                listen_key = await self._listen_key_request("POST")
                url = f"{self.websocket_base_url}/private/ws/{listen_key}"
                async with self._connection_factory(url) as socket:
                    self._socket = socket
                    self._keepalive_error = None
                    self.last_error = None
                    delay = self.reconnect_initial
                    keepalive_task = asyncio.create_task(self._keepalive(listen_key, socket))
                    async for message in socket:
                        if self._stop.is_set():
                            return
                        event = self.parse_message(message)
                        if event is not None and self.sequencer.accept(event):
                            yield event
                    if self._keepalive_error is not None:
                        raise self._keepalive_error
                    if not self._stop.is_set():
                        raise ConnectionError("Binance user stream closed")
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
                if keepalive_task is not None:
                    keepalive_task.cancel()
                    await asyncio.gather(keepalive_task, return_exceptions=True)
                if listen_key is not None:
                    try:
                        await self._listen_key_request("DELETE", listen_key)
                    except Exception:
                        pass

    async def stop(self) -> None:
        self._stop.set()
        if self._socket is not None:
            await self._socket.close()

    async def close(self) -> None:
        await self.stop()
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def parse_message(message: str | bytes) -> UserStreamEvent | None:
        try:
            payload = json.loads(message)
            event_type = str(payload["e"])
            if event_type not in SUPPORTED_USER_EVENTS:
                return None
            event_time = datetime.fromtimestamp(int(payload["E"]) / 1000, tz=UTC)
            transaction_value = payload.get("T")
            transaction_time = (
                datetime.fromtimestamp(int(transaction_value) / 1000, tz=UTC)
                if transaction_value is not None
                else None
            )
            if event_type == "ORDER_TRADE_UPDATE":
                symbol_value = payload["o"]["s"]
            else:
                positions = payload["a"].get("P", [])
                symbol_value = positions[0].get("s") if len(positions) == 1 else None
            symbol = str(symbol_value) if symbol_value else None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid Binance user stream message: {exc}") from exc
        return UserStreamEvent(
            event_type=event_type,
            event_time=event_time,
            transaction_time=transaction_time,
            symbol=symbol,
            payload=payload,
        )
