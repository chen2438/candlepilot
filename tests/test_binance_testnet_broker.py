import asyncio
from decimal import Decimal
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr

from candlepilot.broker.binance_testnet import BinanceTestnetBroker, BinanceTestnetCredentials
from candlepilot.domain.models import OrderPlan, OrderType
from candlepilot.market.binance import BINANCE_FUTURES_TESTNET


def _credentials() -> BinanceTestnetCredentials:
    return BinanceTestnetCredentials(SecretStr("test-key"), SecretStr("test-secret"))


def test_broker_refuses_production_endpoint() -> None:
    with pytest.raises(ValueError, match="only permits"):
        BinanceTestnetBroker(_credentials(), base_url="https://fapi.binance.com")


def test_signed_testnet_entry_and_stop() -> None:
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
        if query.get("type") == ["STOP_MARKET"]:
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
