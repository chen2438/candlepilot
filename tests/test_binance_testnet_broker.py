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
        assert query["algoType"] == ["CONDITIONAL"]
        assert "quantity" not in query
    assert protective["STOP_MARKET"]["triggerPrice"] == ["98"]
    assert protective["TAKE_PROFIT_MARKET"]["triggerPrice"] == ["104"]
    assert all(
        request.url.path == "/fapi/v1/algoOrder"
        for request in signed
        if parse_qs(request.url.query.decode()).get("type", [""])[0]
        in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
    )


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


def test_add_replaces_existing_candlepilot_bracket_after_new_pair_is_active() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "orderType": "STOP_MARKET",
                        "closePosition": True,
                        "clientAlgoId": "cp-old-sl",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "orderType": "TAKE_PROFIT_MARKET",
                        "closePosition": True,
                        "clientAlgoId": "cp-old-tp",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "orderType": "STOP_MARKET",
                        "closePosition": True,
                        "clientAlgoId": "manual-stop",
                    },
                ],
            )
        if request.url.path == "/fapi/v1/marginType":
            return httpx.Response(400, json={"code": -4046, "msg": "No need to change"})
        if request.url.path == "/fapi/v1/leverage":
            return httpx.Response(200, json={"leverage": 3})
        query = parse_qs(request.url.query.decode())
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "CANCELED"})
        if query.get("type") in (["STOP_MARKET"], ["TAKE_PROFIT_MARKET"]):
            return httpx.Response(200, json={"status": "NEW"})
        return httpx.Response(
            200,
            json={
                "clientOrderId": "cp-add",
                "status": "FILLED",
                "executedQty": "0.5",
                "avgPrice": "101",
            },
        )

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        report = await broker.execute_with_stop(
            OrderPlan(
                client_order_id="cp-add",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("0.5"),
                order_type=OrderType.MARKET,
                stop_price=Decimal("97"),
                take_profit_price=Decimal("106"),
            ),
            leverage=3,
            replace_existing_protection=True,
        )
        await client.aclose()
        return report

    assert asyncio.run(scenario()).status == "FILLED"
    def lifecycle_item(request: httpx.Request) -> tuple[str, str, str | None, str | None]:
        query = parse_qs(request.url.query.decode())
        client_id = query.get("newClientOrderId", query.get("clientAlgoId", [None]))[0]
        canceled_id = (
            query.get("origClientOrderId", query.get("clientAlgoId", [None]))[0]
            if request.method == "DELETE"
            else None
        )
        return request.method, request.url.path, client_id, canceled_id

    lifecycle = [
        lifecycle_item(request)
        for request in requests
        if request.url.path not in {
            "/fapi/v1/time",
            "/fapi/v1/marginType",
            "/fapi/v1/leverage",
        }
    ]
    assert lifecycle == [
        ("GET", "/fapi/v1/openAlgoOrders", None, None),
        ("POST", "/fapi/v1/order", "cp-add", None),
        ("POST", "/fapi/v1/algoOrder", "cp-add-sl", None),
        ("POST", "/fapi/v1/algoOrder", "cp-add-tp", None),
        ("DELETE", "/fapi/v1/algoOrder", "cp-old-sl", "cp-old-sl"),
        ("DELETE", "/fapi/v1/algoOrder", "cp-old-tp", "cp-old-tp"),
    ]


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
        client_order_id = query.get("newClientOrderId", ["cp-tpfail"])[0]
        return httpx.Response(
            200,
            json={
                "clientOrderId": client_order_id,
                "status": "FILLED",
                "executedQty": "1",
                "avgPrice": "98" if client_order_id.endswith("-rescue") else "100",
            },
        )

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        with pytest.raises(ProtectiveStopError) as captured:
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
        return captured.value

    failure = asyncio.run(scenario())
    assert failure.exchange_error_code == -2021
    assert failure.entry.average_price == Decimal("100")
    assert failure.rescue is not None
    assert failure.rescue.average_price == Decimal("98")
    assert failure.estimated_loss_usdt == Decimal("2")
    rescue = [
        parse_qs(request.url.query.decode())
        for request in requests
        if parse_qs(request.url.query.decode()).get("newClientOrderId") == ["cp-tpfail-rescue"]
    ]
    assert rescue and rescue[0]["reduceOnly"] == ["true"]
    assert rescue[0]["newOrderRespType"] == ["RESULT"]


@pytest.mark.parametrize(
    ("orders", "unprotected"),
    [
        ([], ("BTCUSDT",)),
        (
            [
                {
                    "symbol": "BTCUSDT",
                    "orderType": "STOP_MARKET",
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
            return httpx.Response(200, json=[])
        if request.url.path == "/fapi/v1/openAlgoOrders":
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


def test_reconciliation_reports_pending_entry_orders() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v3/account":
            return httpx.Response(200, json={"positions": []})
        if request.url.path == "/fapi/v1/openOrders":
            return httpx.Response(
                200,
                json=[
                    {"symbol": "ETHUSDT", "reduceOnly": False},
                    {"symbol": "BTCUSDT", "reduceOnly": True},
                ],
            )
        if request.url.path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(200, json=[])
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
    assert report.pending_entry_symbols == ("ETHUSDT",)
    assert report.open_order_count == 2


def test_emergency_flatten_cancels_orphan_orders_before_closing_positions() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v3/account":
            return httpx.Response(
                200,
                json={"positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}]},
            )
        if request.url.path == "/fapi/v1/openOrders" and request.method == "GET":
            return httpx.Response(200, json=[{"symbol": "ETHUSDT"}])
        if request.url.path == "/fapi/v1/openAlgoOrders" and request.method == "GET":
            return httpx.Response(200, json=[{"symbol": "SOLUSDT"}])
        return httpx.Response(200, json={"status": "FILLED"})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        await broker.emergency_flatten()
        await client.aclose()

    asyncio.run(scenario())
    cancellations = {
        parse_qs(request.url.query.decode())["symbol"][0]
        for request in requests
        if request.method == "DELETE"
    }
    assert cancellations == {"BTCUSDT", "ETHUSDT", "SOLUSDT"}
    flatten = next(
        request
        for request in requests
        if request.method == "POST" and request.url.path == "/fapi/v1/order"
    )
    assert parse_qs(flatten.url.query.decode())["symbol"] == ["BTCUSDT"]
    assert max(
        index for index, request in enumerate(requests) if request.method == "DELETE"
    ) < requests.index(flatten)


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


def test_protective_levels_reads_live_brackets_from_the_exchange() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSDT",
                    "orderType": "STOP_MARKET",
                    "triggerPrice": "98",
                    "closePosition": "true",
                },
                {
                    "symbol": "BTCUSDT",
                    "orderType": "TAKE_PROFIT_MARKET",
                    "triggerPrice": "104",
                    "closePosition": "true",
                },
                {
                    "symbol": "ETHUSDT",
                    "orderType": "STOP_MARKET",
                    "triggerPrice": "3000",
                    "closePosition": "true",
                },
                # Reduce-only scale-outs are not the position's invalidation.
                {
                    "symbol": "SOLUSDT",
                    "orderType": "TAKE_PROFIT_MARKET",
                    "triggerPrice": "220",
                    "closePosition": "false",
                },
            ],
        )

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        levels = await broker.protective_levels()
        await client.aclose()
        return levels

    levels = asyncio.run(scenario())
    assert levels["BTCUSDT"].stop_loss == Decimal("98")
    assert levels["BTCUSDT"].take_profit == Decimal("104")
    # A position guarded on one side only reports that side rather than vanishing.
    assert levels["ETHUSDT"].stop_loss == Decimal("3000")
    assert levels["ETHUSDT"].take_profit is None
    assert "SOLUSDT" not in levels
