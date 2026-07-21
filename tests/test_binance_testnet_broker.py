import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from candlepilot.broker.binance_testnet import (
    AccountReconciliationError,
    BinanceApiError,
    BinanceTestnetBroker,
    BinanceTestnetCredentials,
    EmergencyFlattenError,
    ManualCloseError,
    OrderStatusUnknown,
    TrailingStopReplacementError,
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


def test_testnet_tradable_symbols_excludes_pending_and_non_usdt_contracts() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                        "filters": [
                            {
                                "filterType": "LOT_SIZE",
                                "stepSize": "0.010",
                                "minQty": "0.020",
                                "maxQty": "500",
                            },
                            {
                                "filterType": "MARKET_LOT_SIZE",
                                "stepSize": "0.10",
                                "minQty": "0.10",
                                "maxQty": "100",
                            },
                            {"filterType": "MIN_NOTIONAL", "notional": "100"},
                            {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        ],
                    },
                    {
                        "symbol": "ALLOUSDT",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                        "status": "PENDING_TRADING",
                    },
                    {
                        "symbol": "ETHUSDT_260925",
                        "contractType": "CURRENT_QUARTER",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                    },
                    {
                        "symbol": "ETHUSDC",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDC",
                        "status": "TRADING",
                    },
                ]
            },
        )

    async def scenario() -> tuple[frozenset[str], dict[str, object]]:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        rules = await broker.tradable_contract_rules()
        symbols = frozenset(rules)
        await client.aclose()
        return symbols, rules

    symbols, rules = asyncio.run(scenario())
    assert symbols == frozenset({"BTCUSDT"})
    assert rules["BTCUSDT"].quantity_step == Decimal("0.010")
    assert rules["BTCUSDT"].min_quantity == Decimal("0.020")
    assert rules["BTCUSDT"].min_notional == Decimal("100")
    assert rules["BTCUSDT"].tick_size == Decimal("0.10")
    assert rules["BTCUSDT"].max_quantity == Decimal("500")
    assert rules["BTCUSDT"].market_quantity_step == Decimal("0.10")
    assert rules["BTCUSDT"].market_min_quantity == Decimal("0.10")
    assert rules["BTCUSDT"].market_max_quantity == Decimal("100")


def test_position_risk_reads_signed_v3_prices() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.091",
                    "entryPrice": "64110.7",
                    "markPrice": "64425.1",
                    "unRealizedProfit": "28.61",
                }
            ],
        )

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        rows = await broker.position_risk()
        await client.aclose()
        return rows

    rows = asyncio.run(scenario())
    assert rows[0]["entryPrice"] == "64110.7"
    assert captured[0].url.path == "/fapi/v3/positionRisk"
    assert captured[0].headers["X-MBX-APIKEY"] == "test-key"
    assert "signature=" in str(captured[0].url)


def test_account_snapshot_enriches_positions_with_position_risk() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v3/account":
            return httpx.Response(
                200,
                json={
                    "totalMarginBalance": "4917.76",
                    "positions": [
                        {
                            "symbol": "BTCUSDT",
                            "positionAmt": "0.091",
                            "leverage": "1",
                        }
                    ],
                },
            )
        if request.url.path == "/fapi/v3/positionRisk":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "0.091",
                        "entryPrice": "64110.7",
                        "markPrice": "64503.7",
                        "unRealizedProfit": "35.76",
                    }
                ],
            )
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "leverage": 2,
                        "marginType": "ISOLATED",
                    }
                ],
            )
        return httpx.Response(404, json={"code": -1, "msg": "not found"})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        snapshot = await broker.account_snapshot()
        await client.aclose()
        return snapshot

    snapshot = asyncio.run(scenario())
    assert snapshot["totalMarginBalance"] == "4917.76"
    assert snapshot["positions"] == [
        {
            "symbol": "BTCUSDT",
            "positionAmt": "0.091",
            "leverage": 2,
            "marginType": "ISOLATED",
            "isolated": True,
            "entryPrice": "64110.7",
            "markPrice": "64503.7",
            "unRealizedProfit": "35.76",
            "unrealizedProfit": "35.76",
        }
    ]


def test_account_snapshot_rejects_open_position_without_risk_price() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v3/account":
            return httpx.Response(
                200,
                json={"positions": [{"symbol": "BTCUSDT", "positionAmt": "0.091"}]},
            )
        if request.url.path == "/fapi/v3/positionRisk":
            return httpx.Response(200, json=[])
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={"code": -1, "msg": "not found"})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        with pytest.raises(
            AccountReconciliationError,
            match="position risk response is missing entry price for BTCUSDT",
        ):
            await broker.account_snapshot()
        await client.aclose()

    asyncio.run(scenario())


def test_signed_testnet_entry_and_bracket() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(
                200,
                json=[{"symbol": "BTCUSDT", "marginType": "CROSSED", "leverage": 20}],
            )
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


def test_binance_configuration_error_names_the_failing_rest_path() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(
                200,
                json=[{"symbol": "BTCUSDT", "marginType": "CROSSED", "leverage": 3}],
            )
        return httpx.Response(
            400,
            json={"code": -4067, "msg": "Position side cannot be changed"},
        )

    async def scenario() -> BinanceApiError:
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        with pytest.raises(BinanceApiError) as captured:
            await broker.configure_symbol("BTCUSDT", 3)
        await client.aclose()
        return captured.value

    error = asyncio.run(scenario())
    assert error.method == "POST"
    assert error.path == "/fapi/v1/marginType"
    assert "Binance POST /fapi/v1/marginType error -4067" in str(error)


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


def test_new_opening_limit_waits_for_fill_before_canceling() -> None:
    requests: list[httpx.Request] = []
    order_queries = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal order_queries
        requests.append(request)
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(
                200,
                json=[{"symbol": "BTCUSDT", "marginType": "ISOLATED", "leverage": 3}],
            )
        if request.url.path == "/fapi/v1/algoOrder":
            return httpx.Response(200, json={"status": "NEW"})
        if request.url.path == "/fapi/v1/order" and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "clientOrderId": "cp-limit-fill-race",
                    "status": "NEW",
                    "executedQty": "0",
                    "avgPrice": "0",
                },
            )
        if request.url.path == "/fapi/v1/order" and request.method == "GET":
            order_queries += 1
            if order_queries == 1:
                return httpx.Response(
                    200,
                    json={"status": "NEW", "executedQty": "0", "avgPrice": "0"},
                )
            return httpx.Response(
                200,
                json={"status": "FILLED", "executedQty": "1", "avgPrice": "100"},
            )
        return httpx.Response(500, json={"code": -1000, "msg": "unexpected request"})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(
            _credentials(), client=client, recovery_attempts=3, recovery_delay=0
        )
        report = await broker.execute_with_stop(
            OrderPlan(
                client_order_id="cp-limit-fill-race",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("1"),
                order_type=OrderType.LIMIT,
                price=Decimal("101"),
                stop_price=Decimal("98"),
                take_profit_price=Decimal("104"),
            ),
            leverage=3,
        )
        await client.aclose()
        return report

    report = asyncio.run(scenario())
    assert report.status == "FILLED"
    assert report.filled_quantity == Decimal("1")
    assert order_queries == 2
    assert not any(request.method == "DELETE" for request in requests)
    assert sum(request.url.path == "/fapi/v1/algoOrder" for request in requests) == 2


def test_unfilled_opening_limit_is_canceled_without_creating_fake_protection() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(
                200,
                json=[{"symbol": "BTCUSDT", "marginType": "CROSSED", "leverage": 20}],
            )
        if request.url.path == "/fapi/v1/marginType":
            return httpx.Response(400, json={"code": -4046, "msg": "No need to change"})
        if request.url.path == "/fapi/v1/leverage":
            return httpx.Response(200, json={"leverage": 3})
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "CANCELED"})
        return httpx.Response(
            200,
            json={
                "clientOrderId": "cp-limit",
                "status": "NEW",
                "executedQty": "0",
                "avgPrice": "0",
            },
        )

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        report = await broker.execute_with_stop(
            OrderPlan(
                client_order_id="cp-limit",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("1"),
                order_type=OrderType.LIMIT,
                price=Decimal("101"),
                stop_price=Decimal("98"),
                take_profit_price=Decimal("104"),
            ),
            leverage=3,
        )
        await client.aclose()
        return report

    report = asyncio.run(scenario())
    assert report.status == "CANCELED"
    assert any(
        request.method == "DELETE" and request.url.path == "/fapi/v1/order"
        for request in requests
    )
    assert not any(request.url.path == "/fapi/v1/algoOrder" for request in requests)


def test_partial_opening_fill_cancels_remainder_before_installing_protection() -> None:
    lifecycle: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        lifecycle.append((request.method, request.url.path))
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(
                200,
                json=[{"symbol": "BTCUSDT", "marginType": "CROSSED", "leverage": 20}],
            )
        if request.url.path == "/fapi/v1/marginType":
            return httpx.Response(400, json={"code": -4046, "msg": "No need to change"})
        if request.url.path == "/fapi/v1/leverage":
            return httpx.Response(200, json={"leverage": 3})
        if request.method == "DELETE":
            return httpx.Response(
                200,
                json={"status": "CANCELED", "executedQty": "0.4", "avgPrice": "100"},
            )
        if request.url.path == "/fapi/v1/algoOrder":
            return httpx.Response(200, json={"status": "NEW"})
        return httpx.Response(
            200,
            json={
                "clientOrderId": "cp-partial",
                "status": "PARTIALLY_FILLED",
                "executedQty": "0.4",
                "avgPrice": "100",
            },
        )

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        report = await broker.execute_with_stop(
            OrderPlan(
                client_order_id="cp-partial",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("1"),
                order_type=OrderType.LIMIT,
                price=Decimal("101"),
                stop_price=Decimal("98"),
                take_profit_price=Decimal("104"),
            ),
            leverage=3,
        )
        await client.aclose()
        return report

    report = asyncio.run(scenario())
    cancel_index = lifecycle.index(("DELETE", "/fapi/v1/order"))
    protection_index = lifecycle.index(("POST", "/fapi/v1/algoOrder"))
    assert cancel_index < protection_index
    assert report.status == "PARTIALLY_FILLED"
    assert report.filled_quantity == Decimal("0.4")


def test_opening_limit_reconciles_fill_when_cancel_loses_the_matching_race() -> None:
    requests: list[httpx.Request] = []
    order_queries = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal order_queries
        requests.append(request)
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(
                200,
                json=[{"symbol": "BTCUSDT", "marginType": "ISOLATED", "leverage": 3}],
            )
        if request.url.path == "/fapi/v1/order" and request.method == "DELETE":
            return httpx.Response(400, json={"code": -2011, "msg": "Unknown order sent"})
        if request.url.path == "/fapi/v1/order" and request.method == "GET":
            order_queries += 1
            status = "NEW" if order_queries == 1 else "FILLED"
            return httpx.Response(
                200,
                json={
                    "clientOrderId": "cp-cancel-race",
                    "status": status,
                    "executedQty": "0" if status == "NEW" else "1",
                    "avgPrice": "0" if status == "NEW" else "100",
                },
            )
        if request.url.path == "/fapi/v1/algoOrder":
            return httpx.Response(200, json={"status": "NEW"})
        return httpx.Response(
            200,
            json={
                "clientOrderId": "cp-cancel-race",
                "status": "NEW",
                "executedQty": "0",
                "avgPrice": "0",
            },
        )

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(
            _credentials(), client=client, recovery_attempts=3, recovery_delay=0
        )
        report = await broker.execute_with_stop(
            OrderPlan(
                client_order_id="cp-cancel-race",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("1"),
                order_type=OrderType.LIMIT,
                price=Decimal("101"),
                stop_price=Decimal("98"),
                take_profit_price=Decimal("104"),
            ),
            leverage=3,
        )
        await client.aclose()
        return report

    report = asyncio.run(scenario())
    assert report.status == "FILLED"
    assert report.filled_quantity == Decimal("1")
    assert report.average_price == Decimal("100")
    assert order_queries == 2
    assert sum(request.url.path == "/fapi/v1/algoOrder" for request in requests) == 2


def test_opening_limit_keeps_unknown_status_when_cancel_reconciliation_is_not_terminal() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(
                200,
                json=[{"symbol": "BTCUSDT", "marginType": "ISOLATED", "leverage": 3}],
            )
        if request.url.path == "/fapi/v1/order" and request.method == "DELETE":
            raise httpx.ReadTimeout("cancel status unknown", request=request)
        return httpx.Response(
            200,
            json={
                "clientOrderId": "cp-cancel-unknown",
                "status": "NEW",
                "executedQty": "0",
                "avgPrice": "0",
            },
        )

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(
            _credentials(), client=client, recovery_attempts=2, recovery_delay=0
        )
        with pytest.raises(OrderStatusUnknown):
            await broker.execute_with_stop(
                OrderPlan(
                    client_order_id="cp-cancel-unknown",
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity=Decimal("1"),
                    order_type=OrderType.LIMIT,
                    price=Decimal("101"),
                    stop_price=Decimal("98"),
                    take_profit_price=Decimal("104"),
                ),
                leverage=3,
            )
        await client.aclose()

    asyncio.run(scenario())


def test_add_removes_existing_candlepilot_bracket_before_creating_replacement() -> None:
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
                        "triggerPrice": "96",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "orderType": "TAKE_PROFIT_MARKET",
                        "closePosition": True,
                        "clientAlgoId": "cp-old-tp",
                        "triggerPrice": "105",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "orderType": "STOP_MARKET",
                        "closePosition": True,
                        "clientAlgoId": "manual-stop",
                    },
                ],
            )
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(
                200,
                json=[{"symbol": "BTCUSDT", "marginType": "ISOLATED", "leverage": 3}],
            )
        if request.url.path == "/fapi/v1/marginType":
            return httpx.Response(
                400,
                json={"code": -4067, "msg": "Position side cannot be changed"},
            )
        if request.url.path == "/fapi/v1/leverage":
            return httpx.Response(500, json={"code": -1000, "msg": "must not be called"})
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
            "/fapi/v1/symbolConfig",
            "/fapi/v1/marginType",
            "/fapi/v1/leverage",
        }
    ]
    assert lifecycle == [
        ("GET", "/fapi/v1/openAlgoOrders", None, None),
        ("POST", "/fapi/v1/order", "cp-add", None),
        ("DELETE", "/fapi/v1/algoOrder", "cp-old-sl", "cp-old-sl"),
        ("DELETE", "/fapi/v1/algoOrder", "cp-old-tp", "cp-old-tp"),
        ("POST", "/fapi/v1/algoOrder", "cp-add-sl", None),
        ("POST", "/fapi/v1/algoOrder", "cp-add-tp", None),
    ]
    assert not any(
        request.url.path in {"/fapi/v1/marginType", "/fapi/v1/leverage"}
        for request in requests
    )


def test_trailing_stop_replaces_only_the_stop_and_keeps_take_profit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "orderType": "STOP_MARKET",
                        "closePosition": True,
                        "clientAlgoId": "cp-entry-sl",
                        "triggerPrice": "98",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "orderType": "TAKE_PROFIT_MARKET",
                        "closePosition": True,
                        "clientAlgoId": "cp-entry-tp",
                        "triggerPrice": "106",
                    },
                ],
            )
        return httpx.Response(200, json={"status": "NEW"})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        result = await broker.replace_stop_loss(
            "BTCUSDT", "LONG", Decimal("102")
        )
        await client.aclose()
        return result

    result = asyncio.run(scenario())
    assert result.previous_stop == Decimal("98")
    assert result.current_stop == Decimal("102")
    changed = [
        (request.method, request.url.path, parse_qs(request.url.query.decode()))
        for request in requests
        if request.url.path != "/fapi/v1/time"
    ]
    assert [item[:2] for item in changed] == [
        ("GET", "/fapi/v1/openAlgoOrders"),
        ("DELETE", "/fapi/v1/algoOrder"),
        ("POST", "/fapi/v1/algoOrder"),
    ]
    assert changed[-1][2]["clientAlgoId"] == ["cp-entry-sl"]
    assert changed[-1][2]["triggerPrice"] == ["102"]
    assert all("cp-entry-tp" not in str(item) for item in changed[1:])


def test_trailing_stop_restores_old_trigger_when_replacement_fails() -> None:
    posted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.method == "GET":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "orderType": "STOP_MARKET",
                        "closePosition": True,
                        "clientAlgoId": "cp-entry-sl",
                        "triggerPrice": "98",
                    }
                ],
            )
        if request.method == "POST":
            trigger = parse_qs(request.url.query.decode())["triggerPrice"][0]
            posted.append(trigger)
            if trigger == "102":
                return httpx.Response(400, json={"code": -4000, "msg": "rejected"})
        return httpx.Response(200, json={"status": "NEW"})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        with pytest.raises(TrailingStopReplacementError) as caught:
            await broker.replace_stop_loss("BTCUSDT", "LONG", Decimal("102"))
        await client.aclose()
        return caught.value

    error = asyncio.run(scenario())
    assert error.requires_emergency_lock is False
    assert posted == ["102", "98"]


def test_add_rejects_before_entry_when_previous_bracket_cannot_be_restored() -> None:
    from candlepilot.broker.binance_testnet import AccountReconciliationError

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
                    }
                ],
            )
        return httpx.Response(500, json={"code": -1000, "msg": "must not be called"})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        with pytest.raises(AccountReconciliationError):
            await broker.execute_with_stop(
                OrderPlan(
                    client_order_id="cp-invalid-old",
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

    asyncio.run(scenario())
    assert not any(
        request.url.path == "/fapi/v1/order" and request.method == "POST"
        for request in requests
    )


def test_add_protection_failure_rescues_increment_and_restores_previous_bracket() -> None:
    from candlepilot.broker.binance_testnet import ProtectiveStopError

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
                        "triggerPrice": "96",
                    },
                    {
                        "symbol": "BTCUSDT",
                        "orderType": "TAKE_PROFIT_MARKET",
                        "closePosition": True,
                        "clientAlgoId": "cp-old-tp",
                        "triggerPrice": "105",
                    },
                ],
            )
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(
                200,
                json=[{"symbol": "BTCUSDT", "marginType": "ISOLATED", "leverage": 3}],
            )
        query = parse_qs(request.url.query.decode())
        if request.method == "DELETE":
            return httpx.Response(200, json={"status": "CANCELED"})
        if request.url.path == "/fapi/v1/algoOrder":
            if query["clientAlgoId"] == ["cp-add-fail-sl"]:
                return httpx.Response(
                    400,
                    json={
                        "code": -4130,
                        "msg": "An open stop or take profit order with closePosition exists",
                    },
                )
            return httpx.Response(200, json={"status": "NEW"})
        client_order_id = query["newClientOrderId"][0]
        return httpx.Response(
            200,
            json={
                "clientOrderId": client_order_id,
                "status": "FILLED",
                "executedQty": "0.5",
                "avgPrice": "100" if client_order_id.endswith("-rescue") else "101",
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
                    client_order_id="cp-add-fail",
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
        return captured.value

    failure = asyncio.run(scenario())
    assert failure.exchange_error_code == -4130
    assert failure.rescue is not None
    assert failure.rescue.status == "FILLED"
    assert failure.requires_emergency_lock is False

    def request_lifecycle_item(
        request: httpx.Request,
    ) -> tuple[str, str, str | None, str | None]:
        query = parse_qs(request.url.query.decode())
        client_id = query.get("newClientOrderId", query.get("clientAlgoId", [None]))[0]
        canceled_id = (
            query.get("clientAlgoId", [None])[0] if request.method == "DELETE" else None
        )
        return request.method, request.url.path, client_id, canceled_id

    lifecycle = [
        request_lifecycle_item(request)
        for request in requests
        if request.url.path
        not in {"/fapi/v1/time", "/fapi/v1/symbolConfig"}
    ]
    assert lifecycle == [
        ("GET", "/fapi/v1/openAlgoOrders", None, None),
        ("POST", "/fapi/v1/order", "cp-add-fail", None),
        ("DELETE", "/fapi/v1/algoOrder", "cp-old-sl", "cp-old-sl"),
        ("DELETE", "/fapi/v1/algoOrder", "cp-old-tp", "cp-old-tp"),
        ("POST", "/fapi/v1/algoOrder", "cp-add-fail-sl", None),
        ("POST", "/fapi/v1/order", "cp-add-fail-rescue", None),
        ("POST", "/fapi/v1/algoOrder", "cp-old-sl", None),
        ("POST", "/fapi/v1/algoOrder", "cp-old-tp", None),
    ]
    restored = {
        parse_qs(request.url.query.decode())["clientAlgoId"][0]: parse_qs(
            request.url.query.decode()
        )["triggerPrice"][0]
        for request in requests
        if request.url.path == "/fapi/v1/algoOrder"
        and request.method == "POST"
        and parse_qs(request.url.query.decode())["clientAlgoId"][0]
        in {"cp-old-sl", "cp-old-tp"}
    }
    assert restored == {
        "cp-old-sl": "96",
        "cp-old-tp": "105",
    }


def test_take_profit_failure_triggers_emergency_reduce() -> None:
    from candlepilot.broker.binance_testnet import ProtectiveStopError

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(
                200,
                json=[{"symbol": "BTCUSDT", "marginType": "CROSSED", "leverage": 20}],
            )
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


def test_incomplete_emergency_reduce_requires_an_emergency_lock() -> None:
    from candlepilot.broker.binance_testnet import ProtectiveStopError

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(
                200,
                json=[{"symbol": "BTCUSDT", "marginType": "ISOLATED", "leverage": 3}],
            )
        query = parse_qs(request.url.query.decode())
        if request.url.path == "/fapi/v1/algoOrder":
            if request.method == "DELETE":
                return httpx.Response(200, json={"status": "CANCELED"})
            if query.get("type") == ["TAKE_PROFIT_MARKET"]:
                return httpx.Response(400, json={"code": -2021, "msg": "would trigger"})
            return httpx.Response(200, json={"status": "NEW"})
        client_order_id = query["newClientOrderId"][0]
        if client_order_id.endswith("-rescue"):
            return httpx.Response(
                200,
                json={
                    "clientOrderId": client_order_id,
                    "status": "PARTIALLY_FILLED",
                    "executedQty": "0.4",
                    "avgPrice": "99",
                },
            )
        return httpx.Response(
            200,
            json={
                "clientOrderId": client_order_id,
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
        with pytest.raises(ProtectiveStopError) as captured:
            await broker.execute_with_stop(
                OrderPlan(
                    client_order_id="cp-partial",
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
    assert failure.rescue is not None
    assert failure.rescue.status == "PARTIALLY_FILLED"
    assert failure.rescue.filled_quantity == Decimal("0.4")
    assert failure.failed_stage == "RESCUE"
    assert failure.requires_emergency_lock is True
    assert failure.estimated_loss_usdt is None
    assert "incomplete (PARTIALLY_FILLED, 0.4/1)" in str(failure)


def test_failed_protective_cleanup_requires_an_emergency_lock() -> None:
    from candlepilot.broker.binance_testnet import ProtectiveStopError

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(
                200,
                json=[{"symbol": "BTCUSDT", "marginType": "CROSSED", "leverage": 20}],
            )
        if request.url.path == "/fapi/v1/marginType":
            return httpx.Response(400, json={"code": -4046, "msg": "No need to change"})
        if request.url.path == "/fapi/v1/leverage":
            return httpx.Response(200, json={"leverage": 3})
        query = parse_qs(request.url.query.decode())
        if request.method == "DELETE" and request.url.path == "/fapi/v1/algoOrder":
            return httpx.Response(500, json={"code": -1000, "msg": "cleanup failed"})
        if query.get("type") == ["TAKE_PROFIT_MARKET"]:
            return httpx.Response(400, json={"code": -2021, "msg": "would trigger"})
        client_order_id = query.get("newClientOrderId", ["cp-cleanup"])[0]
        return httpx.Response(
            200,
            json={
                "clientOrderId": client_order_id,
                "status": "FILLED",
                "executedQty": "1",
                "avgPrice": "99" if client_order_id.endswith("-rescue") else "100",
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
                    client_order_id="cp-cleanup",
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
    assert failure.rescue is not None
    assert failure.requires_emergency_lock is True
    assert "cleanup failed" in str(failure)


@pytest.mark.parametrize(
    ("position_amount", "orders", "unprotected"),
    [
        ("1", [], ("BTCUSDT",)),
        (
            "1",
            [
                {
                    "symbol": "BTCUSDT",
                    "orderType": "STOP_MARKET",
                    "closePosition": True,
                    "side": "SELL",
                }
            ],
            (),
        ),
        (
            "1",
            [
                {
                    "symbol": "BTCUSDT",
                    "orderType": "STOP_MARKET",
                    "closePosition": True,
                    "side": "BUY",
                }
            ],
            ("BTCUSDT",),
        ),
        (
            "1",
            [
                {
                    "symbol": "BTCUSDT",
                    "orderType": "STOP_MARKET",
                    "reduceOnly": True,
                    "side": "SELL",
                    "quantity": "0.4",
                }
            ],
            ("BTCUSDT",),
        ),
        (
            "1",
            [
                {
                    "symbol": "BTCUSDT",
                    "orderType": "STOP_MARKET",
                    "reduceOnly": True,
                    "side": "SELL",
                    "origQty": "0.4",
                },
                {
                    "symbol": "BTCUSDT",
                    "orderType": "STOP",
                    "reduceOnly": "true",
                    "side": "SELL",
                    "quantity": "0.6",
                },
            ],
            (),
        ),
        (
            "-2",
            [
                {
                    "symbol": "BTCUSDT",
                    "orderType": "STOP_MARKET",
                    "closePosition": "true",
                    "side": "BUY",
                }
            ],
            (),
        ),
    ],
)
def test_reconciles_protective_stops(position_amount, orders, unprotected) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v3/account":
            return httpx.Response(
                200,
                json={
                    "positions": [
                        {"symbol": "BTCUSDT", "positionAmt": position_amount}
                    ]
                },
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
            return httpx.Response(
                200,
                json=[
                    {"symbol": "SOLUSDT", "closePosition": False},
                    {"symbol": "XRPUSDT", "closePosition": True},
                ],
            )
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
    assert report.pending_entry_symbols == ("ETHUSDT", "SOLUSDT")
    assert report.open_order_count == 4


def test_pending_entry_symbols_reads_live_non_reduce_only_orders() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/openOrders":
            return httpx.Response(
                200,
                json=[
                    {"symbol": "ETHUSDT", "reduceOnly": False},
                    {"symbol": "BTCUSDT", "reduceOnly": True},
                    {"symbol": "ETHUSDT", "reduceOnly": False},
                ],
            )
        assert request.url.path == "/fapi/v1/openAlgoOrders"
        return httpx.Response(
            200,
            json=[
                {"symbol": "SOLUSDT", "closePosition": False},
                {"symbol": "XRPUSDT", "closePosition": True},
            ],
        )

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        symbols = await broker.pending_entry_symbols()
        await client.aclose()
        return symbols

    assert asyncio.run(scenario()) == ("ETHUSDT", "SOLUSDT")


def test_income_24h_sums_trading_components_and_excludes_transfers() -> None:
    queries: list[dict[str, list[str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(parse_qs(request.url.query.decode()))
        return httpx.Response(
            200,
            json=[
                {"incomeType": "REALIZED_PNL", "income": "-30"},
                {"incomeType": "COMMISSION", "income": "-1.5"},
                {"incomeType": "FUNDING_FEE", "income": "0.5"},
                {"incomeType": "TRANSFER", "income": "1000"},
            ],
        )

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        result = await broker.income_24h(now=datetime(2026, 7, 17, 12, tzinfo=UTC))
        await client.aclose()
        return result

    assert asyncio.run(scenario()) == Decimal("-31")
    assert queries[0]["startTime"] == ["1784203200000"]
    assert queries[0]["endTime"] == ["1784289600000"]
    assert queries[0]["page"] == ["1"]


def test_emergency_flatten_cancels_orphan_orders_before_closing_positions() -> None:
    requests: list[httpx.Request] = []
    closed = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal closed
        requests.append(request)
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v3/account":
            positions = [] if closed else [{"symbol": "BTCUSDT", "positionAmt": "1"}]
            return httpx.Response(200, json={"positions": positions})
        if request.url.path == "/fapi/v1/openOrders" and request.method == "GET":
            return httpx.Response(200, json=[{"symbol": "ETHUSDT"}])
        if request.url.path == "/fapi/v1/openAlgoOrders" and request.method == "GET":
            return httpx.Response(200, json=[{"symbol": "SOLUSDT"}])
        if request.method == "POST" and request.url.path == "/fapi/v1/order":
            closed = True
            query = parse_qs(request.url.query.decode())
            return httpx.Response(
                200,
                json={
                    "clientOrderId": query["newClientOrderId"][0],
                    "status": "FILLED",
                    "executedQty": "1",
                    "avgPrice": "100",
                },
            )
        return httpx.Response(200, json={"status": "FILLED"})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        executions = await broker.emergency_flatten()
        await client.aclose()
        return executions

    executions = asyncio.run(scenario())
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
    assert len(executions) == 1
    assert executions[0].symbol == "BTCUSDT"
    assert executions[0].report.status == "FILLED"
    assert executions[0].report.filled_quantity == Decimal("1")
    assert executions[0].report.average_price == Decimal("100")
    assert max(
        index for index, request in enumerate(requests) if request.method == "DELETE"
    ) < requests.index(flatten)


def test_emergency_flatten_error_keeps_successful_execution_reports() -> None:
    closed = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal closed
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v3/account":
            positions = [] if closed else [{"symbol": "BTCUSDT", "positionAmt": "1"}]
            return httpx.Response(200, json={"positions": positions})
        if request.url.path == "/fapi/v1/openOrders" and request.method == "GET":
            return httpx.Response(200, json=[{"symbol": "ETHUSDT"}])
        if request.url.path == "/fapi/v1/openAlgoOrders" and request.method == "GET":
            return httpx.Response(200, json=[])
        if request.method == "DELETE" and "ETHUSDT" in str(request.url):
            return httpx.Response(500, json={"code": -1, "msg": "cancel failed"})
        if request.method == "POST" and request.url.path == "/fapi/v1/order":
            closed = True
            query = parse_qs(request.url.query.decode())
            return httpx.Response(
                200,
                json={
                    "clientOrderId": query["newClientOrderId"][0],
                    "status": "FILLED",
                    "executedQty": "1",
                    "avgPrice": "100",
                },
            )
        return httpx.Response(200, json={})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        with pytest.raises(EmergencyFlattenError) as caught:
            await broker.emergency_flatten()
        await client.aclose()
        return caught.value

    failure = asyncio.run(scenario())
    assert "cancel ETHUSDT" in str(failure)
    assert len(failure.executions) == 1
    assert failure.executions[0].symbol == "BTCUSDT"
    assert failure.executions[0].report.status == "FILLED"


def test_emergency_flatten_rejects_incomplete_close_and_remaining_position() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1784040000000})
        if request.url.path == "/fapi/v3/account":
            return httpx.Response(
                200,
                json={"positions": [{"symbol": "BTCUSDT", "positionAmt": "0.6"}]},
            )
        if request.url.path in {"/fapi/v1/openOrders", "/fapi/v1/openAlgoOrders"}:
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == "/fapi/v1/order":
            query = parse_qs(request.url.query.decode())
            return httpx.Response(
                200,
                json={
                    "clientOrderId": query["newClientOrderId"][0],
                    "status": "PARTIALLY_FILLED",
                    "executedQty": "0.4",
                    "avgPrice": "100",
                },
            )
        return httpx.Response(200, json={"status": "FILLED"})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(
            _credentials(), client=client, recovery_attempts=1, recovery_delay=0
        )
        with pytest.raises(EmergencyFlattenError) as captured:
            await broker.emergency_flatten()
        await client.aclose()
        return captured.value

    failure = asyncio.run(scenario())
    assert "incomplete order (PARTIALLY_FILLED, 0.4/0.6)" in str(failure)
    assert "remaining position 0.6" in str(failure)
    assert len(failure.executions) == 1
    assert failure.executions[0].report.status == "PARTIALLY_FILLED"


def test_manual_market_close_is_reduce_only_and_cleans_only_own_bracket() -> None:
    requests: list[httpx.Request] = []
    closed = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal closed
        requests.append(request)
        if request.url.path == "/fapi/v3/account":
            return httpx.Response(
                200,
                json={
                    "positions": []
                    if closed
                    else [{"symbol": "BTCUSDT", "positionAmt": "-2"}]
                },
            )
        if request.url.path == "/fapi/v3/positionRisk":
            return httpx.Response(
                200,
                json=[]
                if closed
                else [
                    {
                        "symbol": "BTCUSDT",
                        "entryPrice": "100",
                        "markPrice": "99",
                        "unRealizedProfit": "2",
                    }
                ],
            )
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "leverage": 2,
                        "marginType": "ISOLATED",
                    }
                ],
            )
        if request.url.path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "clientAlgoId": "cp-entry-sl",
                        "orderType": "STOP_MARKET",
                        "closePosition": True,
                    },
                    {
                        "symbol": "BTCUSDT",
                        "clientAlgoId": "cp-entry-tp",
                        "orderType": "TAKE_PROFIT_MARKET",
                        "closePosition": True,
                    },
                    {
                        "symbol": "BTCUSDT",
                        "clientAlgoId": "manual-stop",
                        "orderType": "STOP_MARKET",
                        "closePosition": True,
                    },
                ],
            )
        if request.url.path == "/fapi/v1/order" and request.method == "POST":
            closed = True
            query = parse_qs(request.url.query.decode())
            return httpx.Response(
                200,
                json={
                    "clientOrderId": query["newClientOrderId"][0],
                    "status": "FILLED",
                    "executedQty": "2",
                    "avgPrice": "99",
                },
            )
        return httpx.Response(200, json={"status": "CANCELED"})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        report = await broker.close_position_market("BTCUSDT")
        await client.aclose()
        return report

    report = asyncio.run(scenario())
    assert report.status == "FILLED"
    close_request = next(
        request
        for request in requests
        if request.url.path == "/fapi/v1/order" and request.method == "POST"
    )
    close_query = parse_qs(close_request.url.query.decode())
    assert close_query["side"] == ["BUY"]
    assert close_query["quantity"] == ["2"]
    assert close_query["reduceOnly"] == ["true"]
    cancelled_ids = {
        parse_qs(request.url.query.decode())["clientAlgoId"][0]
        for request in requests
        if request.url.path == "/fapi/v1/algoOrder" and request.method == "DELETE"
    }
    assert cancelled_ids == {"cp-entry-sl", "cp-entry-tp"}


def test_manual_market_close_keeps_protection_when_fill_is_incomplete() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/fapi/v3/account":
            return httpx.Response(
                200,
                json={"positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}]},
            )
        if request.url.path == "/fapi/v3/positionRisk":
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "entryPrice": "100",
                        "markPrice": "101",
                    }
                ],
            )
        if request.url.path == "/fapi/v1/symbolConfig":
            return httpx.Response(200, json=[])
        if request.url.path == "/fapi/v1/openAlgoOrders":
            return httpx.Response(
                200,
                json=[
                    {
                        "clientAlgoId": "cp-entry-sl",
                        "orderType": "STOP_MARKET",
                        "closePosition": True,
                    }
                ],
            )
        if request.url.path == "/fapi/v1/order":
            return httpx.Response(
                200,
                json={
                    "clientOrderId": "cp-manual-partial",
                    "status": "PARTIALLY_FILLED",
                    "executedQty": "0.5",
                    "avgPrice": "101",
                },
            )
        return httpx.Response(200, json={})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        with pytest.raises(ManualCloseError, match="did not fill completely") as caught:
            await broker.close_position_market("BTCUSDT")
        await client.aclose()
        return caught.value

    failure = asyncio.run(scenario())
    assert failure.stage == "FILL"
    assert failure.report.status == "PARTIALLY_FILLED"
    assert not any(request.method == "DELETE" for request in requests)


def test_completed_order_fill_event_uses_real_exchange_trades() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/fapi/v1/order":
            return httpx.Response(
                200,
                json={
                    "orderId": 42,
                    "clientOrderId": "cp-manual-close",
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "status": "FILLED",
                    "reduceOnly": True,
                },
            )
        if request.url.path == "/fapi/v1/userTrades":
            return httpx.Response(
                200,
                json=[
                    {
                        "orderId": 42,
                        "qty": "0.04",
                        "price": "61000",
                        "quoteQty": "2440",
                        "realizedPnl": "20",
                        "side": "SELL",
                        "time": 1784445088000,
                    },
                    {
                        "orderId": 42,
                        "qty": "0.06",
                        "price": "61100",
                        "quoteQty": "3666",
                        "realizedPnl": "31",
                        "side": "SELL",
                        "time": 1784445088123,
                    },
                ],
            )
        return httpx.Response(404, json={"code": -1, "msg": "unexpected"})

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url=BINANCE_FUTURES_TESTNET
        )
        broker = BinanceTestnetBroker(_credentials(), client=client)
        event = await broker.completed_order_fill_event("BTCUSDT", "cp-manual-close")
        await client.aclose()
        return event

    event = asyncio.run(scenario())
    assert event is not None
    assert event.payload["_source"] == "rest_trade_reconciliation"
    assert event.payload["o"] == {
        "s": "BTCUSDT",
        "c": "cp-manual-close",
        "S": "SELL",
        "x": "TRADE",
        "X": "FILLED",
        "z": "0.10",
        "ap": "6.106E+4",
        "R": True,
        "rp": "51",
        "i": 42,
    }
    assert event.event_time.isoformat() == "2026-07-19T07:11:28.123000+00:00"
    assert [request.url.path for request in requests] == [
        "/fapi/v1/order",
        "/fapi/v1/userTrades",
    ]


def test_completed_exit_fill_event_checks_bracket_ids_until_one_filled() -> None:
    async def scenario():
        client = httpx.AsyncClient(base_url=BINANCE_FUTURES_TESTNET)
        broker = BinanceTestnetBroker(_credentials(), client=client)
        attempts: list[str] = []
        now = datetime.now(UTC)
        expected = UserStreamEvent(
            "ORDER_TRADE_UPDATE",
            now,
            now,
            "AKEUSDT",
            {"o": {"c": "cp-entry-tp", "X": "FILLED", "x": "TRADE"}},
        )

        async def completed(_symbol: str, client_order_id: str):
            attempts.append(client_order_id)
            if client_order_id.endswith("-sl"):
                raise BinanceApiError(-2013, "Order does not exist", 400)
            return expected

        broker.completed_order_fill_event = completed  # type: ignore[method-assign]
        event = await broker.completed_exit_fill_event("AKEUSDT", "cp-entry")
        await client.aclose()
        return event, attempts, expected

    event, attempts, expected = asyncio.run(scenario())
    assert event is expected
    assert attempts == ["cp-entry-sl", "cp-entry-tp"]


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
