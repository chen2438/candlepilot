import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from candlepilot.broker.binance_testnet import (
    BinanceTestnetBroker,
    BinanceTestnetCredentials,
    OrderStatusUnknown,
)
from candlepilot.broker.user_stream import UserStreamEvent
from candlepilot.domain.models import OrderPlan, OrderType
from candlepilot.market.binance import BINANCE_FUTURES_TESTNET


def _credentials() -> BinanceTestnetCredentials:
    return BinanceTestnetCredentials(SecretStr("test-key"), SecretStr("test-secret"))


def test_broker_refuses_production_endpoint() -> None:
    with pytest.raises(ValueError, match="only permits"):
        BinanceTestnetBroker(_credentials(), base_url="https://fapi.binance.com")


def test_broker_uses_current_official_demo_endpoint() -> None:
    assert BINANCE_FUTURES_TESTNET == "https://demo-fapi.binance.com"
    with pytest.raises(ValueError, match="only permits"):
        BinanceTestnetBroker(_credentials(), base_url="https://testnet.binancefuture.com")


def test_signed_testnet_entry_and_bracket() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v1/marginType":
            return httpx.Response(400, json={"code": -4046, "msg": "No need to change"})
        if request.url.path == "/fapi/v1/leverage":
            return httpx.Response(200, json={"leverage": 3})
        query = parse_qs(request.url.query.decode())
        if query.get("type") in (["STOP_MARKET"], ["TAKE_PROFIT_MARKET"]):
            return httpx.Response(200, json={"status": "NEW"})
        return httpx.Response(
            200,
            json={
                "clientOrderId": "cp-test",
                "status": "FILLED",
                "executedQty": "1.2",
                "avgPrice": "100.1",
            },
        )

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        report = await broker.execute_with_stop(
            OrderPlan(
                client_order_id="cp-test",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("1.2"),
                order_type=OrderType.MARKET,
                stop_price=Decimal("98"),
                take_profit_price=Decimal("104"),
            ),
            leverage=3,
        )
        await client.aclose()
        return report

    report = asyncio.run(scenario())
    assert report.status == "FILLED"
    assert report.average_price == Decimal("100.1")
    signed = [request for request in requests if request.url.path != "/fapi/v1/time"]
    assert signed
    for request in signed:
        assert request.headers["X-MBX-APIKEY"] == "test-key"
        assert "signature=" in str(request.url)
    protective = {
        parse_qs(request.url.query.decode())["type"][0]: parse_qs(request.url.query.decode())
        for request in signed
        if parse_qs(request.url.query.decode()).get("type", [""])[0]
        in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    }
    assert set(protective) == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    for query in protective.values():
        assert query["closePosition"] == ["true"]
        assert query["side"] == ["SELL"]
        assert "quantity" not in query
    assert protective["STOP_MARKET"]["stopPrice"] == ["98"]
    assert protective["TAKE_PROFIT_MARKET"]["stopPrice"] == ["104"]


def test_testnet_opening_requires_take_profit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "FILLED"})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        with pytest.raises(ValueError, match="take profit"):
            await broker.execute_with_stop(
                OrderPlan(
                    client_order_id="cp-no-tp",
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity=Decimal("1"),
                    order_type=OrderType.MARKET,
                    stop_price=Decimal("98"),
                ),
                leverage=3,
            )
        await client.aclose()

    asyncio.run(scenario())


def test_take_profit_failure_triggers_emergency_reduce() -> None:
    from candlepilot.broker.binance_testnet import ProtectiveStopError

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v1/marginType":
            return httpx.Response(400, json={"code": -4046, "msg": "No need to change"})
        if request.url.path == "/fapi/v1/leverage":
            return httpx.Response(200, json={"leverage": 3})
        query = parse_qs(request.url.query.decode())
        if query.get("type") == ["TAKE_PROFIT_MARKET"]:
            return httpx.Response(400, json={"code": -2021, "msg": "would immediately trigger"})
        return httpx.Response(
            200,
            json={
                "clientOrderId": "cp-tpfail",
                "status": "FILLED",
                "executedQty": "1",
                "avgPrice": "100",
            },
        )

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        with pytest.raises(ProtectiveStopError):
            await broker.execute_with_stop(
                OrderPlan(
                    client_order_id="cp-tpfail",
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity=Decimal("1"),
                    order_type=OrderType.MARKET,
                    stop_price=Decimal("98"),
                    take_profit_price=Decimal("104"),
                ),
                leverage=3,
            )
        await client.aclose()

    asyncio.run(scenario())
    rescue = [
        parse_qs(request.url.query.decode())
        for request in requests
        if parse_qs(request.url.query.decode()).get("newClientOrderId") == ["cp-tpfail-rescue"]
    ]
    assert rescue and rescue[0]["reduceOnly"] == ["true"]


@pytest.mark.parametrize(
    ("orders", "unprotected"),
    [
        ([], ("BTCUSDT",)),
        (
            [
                {
                    "symbol": "BTCUSDT",
                    "type": "STOP_MARKET",
                    "closePosition": True,
                }
            ],
            (),
        ),
    ],
)
def test_reconciles_protective_stops(orders, unprotected) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v3/account":
            return httpx.Response(
                200,
                json={"positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}]},
            )
        if request.url.path == "/fapi/v1/openOrders":
            return httpx.Response(200, json=orders)
        return httpx.Response(404, json={"code": -1, "msg": "not found"})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        report = await broker.reconcile_account()
        await client.aclose()
        return report

    report = asyncio.run(scenario())
    assert report.position_symbols == ("BTCUSDT",)
    assert report.unprotected_symbols == unprotected
    assert report.open_order_count == len(orders)


def test_recovers_timed_out_order_by_client_id() -> None:
    query_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal query_attempts
        if request.method == "POST":
            raise httpx.ReadTimeout("unknown", request=request)
        query_attempts += 1
        if query_attempts < 3:
            return httpx.Response(400, json={"code": -2013, "msg": "Order does not exist"})
        return httpx.Response(
            200,
            json={
                "clientOrderId": "cp-recover",
                "status": "FILLED",
                "executedQty": "1",
                "avgPrice": "100",
            },
        )

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(
            _credentials(), client=client, recovery_attempts=3, recovery_delay=0
        )
        report = await broker._place_order(
            OrderPlan(
                client_order_id="cp-recover",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("1"),
                order_type=OrderType.MARKET,
            )
        )
        await client.aclose()
        return report

    report = asyncio.run(scenario())
    assert report.status == "FILLED"
    assert query_attempts == 3


def test_unknown_order_is_not_resubmitted() -> None:
    post_attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal post_attempts
        if request.method == "POST":
            post_attempts += 1
            raise httpx.ReadTimeout("unknown", request=request)
        return httpx.Response(400, json={"code": -2013, "msg": "Order does not exist"})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(
            _credentials(), client=client, recovery_attempts=2, recovery_delay=0
        )
        with pytest.raises(OrderStatusUnknown):
            await broker._place_order(
                OrderPlan(
                    client_order_id="cp-unknown",
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity=Decimal("1"),
                    order_type=OrderType.MARKET,
                )
            )
        await client.aclose()

    asyncio.run(scenario())
    assert post_attempts == 1


def test_retries_rate_limit_and_resyncs_timestamp() -> None:
    account_attempts = 0
    time_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal account_attempts, time_requests
        if request.url.path == "/fapi/v1/time":
            time_requests += 1
            return httpx.Response(200, json={"serverTime": 1784040000000})
        account_attempts += 1
        if account_attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"code": -1003})
        if account_attempts == 2:
            return httpx.Response(400, json={"code": -1021, "msg": "Timestamp outside window"})
        return httpx.Response(200, json={"positions": []})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(
            _credentials(), client=client, rate_limit_attempts=4, recovery_delay=0
        )
        account = await broker.account()
        await client.aclose()
        return account

    assert asyncio.run(scenario()) == {"positions": []}
    assert account_attempts == 3
    assert time_requests == 1


def test_partial_entry_event_cancels_remainder_while_close_position_stop_protects_fill() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"status": "CANCELED"})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        now = datetime.now(UTC)
        await broker.handle_user_event(
            UserStreamEvent(
                event_type="ORDER_TRADE_UPDATE",
                event_time=now,
                transaction_time=now,
                symbol="BTCUSDT",
                payload={
                    "e": "ORDER_TRADE_UPDATE",
                    "o": {
                        "s": "BTCUSDT",
                        "c": "cp-partial-entry",
                        "X": "PARTIALLY_FILLED",
                        "R": False,
                    },
                },
            )
        )
        await client.aclose()

    asyncio.run(scenario())
    assert len(requests) == 1
    assert requests[0].method == "DELETE"
    query = parse_qs(requests[0].url.query.decode())
    assert query["origClientOrderId"] == ["cp-partial-entry"]
