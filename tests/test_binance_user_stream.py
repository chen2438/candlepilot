import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest
from pydantic import SecretStr

from candlepilot.broker.binance_testnet import BinanceTestnetCredentials
from candlepilot.broker.user_stream import BinanceTestnetUserStream
from candlepilot.market.binance import BINANCE_FUTURES_TESTNET


def _credentials() -> BinanceTestnetCredentials:
    return BinanceTestnetCredentials(SecretStr("test-key"), SecretStr("test-secret"))


def test_user_stream_refuses_injected_production_client() -> None:
    client = httpx.AsyncClient(base_url="https://fapi.binance.com")
    with pytest.raises(ValueError, match="demo REST"):
        BinanceTestnetUserStream(_credentials(), client=client)
    asyncio.run(client.aclose())


class FakeSocket:
    def __init__(self, messages: list[str], *, block_when_empty: bool = False) -> None:
        self.messages = iter(messages)
        self.block_when_empty = block_when_empty
        self.closed = False
        self._closed = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.messages)
        except StopIteration:
            if self.block_when_empty and not self.closed:
                await self._closed.wait()
            raise StopAsyncIteration from None

    async def close(self) -> None:
        self.closed = True
        self._closed.set()


def _order_event(event_time: int, status: str = "NEW") -> str:
    return json.dumps(
        {
            "e": "ORDER_TRADE_UPDATE",
            "E": event_time,
            "T": event_time - 1,
            "o": {"s": "BTCUSDT", "X": status, "i": 123},
        }
    )


def test_parses_account_and_order_events() -> None:
    order = BinanceTestnetUserStream.parse_message(_order_event(1_700_000_000_000))
    account = BinanceTestnetUserStream.parse_message(
        json.dumps(
            {
                "e": "ACCOUNT_UPDATE",
                "E": 1_700_000_000_100,
                "T": 1_700_000_000_099,
                "a": {"B": [], "P": [{"s": "ETHUSDT", "pa": "1"}]},
            }
        )
    )

    assert order is not None and order.symbol == "BTCUSDT"
    assert account is not None and account.symbol == "ETHUSDT"
    assert BinanceTestnetUserStream.parse_message('{"e":"listenKeyExpired"}') is None


def test_user_stream_manages_listen_key_and_drops_stale_events() -> None:
    methods: list[str] = []
    urls: list[str] = []
    socket = FakeSocket([_order_event(2000), _order_event(1000), _order_event(3000, "FILLED")])

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        assert request.headers["X-MBX-APIKEY"] == "test-key"
        if request.method == "POST":
            return httpx.Response(200, json={"listenKey": "private-key"})
        return httpx.Response(200, json={})

    @asynccontextmanager
    async def connection(url: str):
        urls.append(url)
        yield socket

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        stream = BinanceTestnetUserStream(
            _credentials(),
            client=client,
            connection_factory=connection,
            reconnect_initial=0,
        )
        events = []
        async for event in stream.events():
            events.append(event)
            if len(events) == 2:
                await stream.stop()
        await client.aclose()
        return stream, events

    stream, events = asyncio.run(scenario())
    assert [event.payload["o"]["X"] for event in events] == ["NEW", "FILLED"]
    assert stream.dropped_event_count == 1
    assert urls == ["wss://demo-fstream.binance.com/private/ws/private-key"]
    assert methods == ["POST", "DELETE"]


def test_keepalive_failure_forces_reconnect() -> None:
    put_attempts = 0
    sockets = [FakeSocket([], block_when_empty=True), FakeSocket([_order_event(5000)])]

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal put_attempts
        if request.method == "POST":
            return httpx.Response(200, json={"listenKey": f"key-{len(sockets)}"})
        if request.method == "PUT":
            put_attempts += 1
            return httpx.Response(400, json={"code": -1125, "msg": "listen key missing"})
        return httpx.Response(200, json={})

    @asynccontextmanager
    async def connection(_url: str):
        yield sockets.pop(0)

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        stream = BinanceTestnetUserStream(
            _credentials(),
            client=client,
            connection_factory=connection,
            keepalive_interval=0.001,
            reconnect_initial=0,
        )
        async for event in stream.events():
            await stream.stop()
            await client.aclose()
            return stream, event
        raise AssertionError("event expected")

    stream, event = asyncio.run(scenario())
    assert event.symbol == "BTCUSDT"
    assert put_attempts >= 1
    assert stream.reconnect_count >= 1
