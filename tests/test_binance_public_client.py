import asyncio
from datetime import UTC, datetime

import httpx

from candlepilot.market.binance import BinancePublicClient, BinanceRateLimit


def _response(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/fapi/v1/exchangeInfo":
        return httpx.Response(
            200,
            json={
                "symbols": [
                    {
                        "symbol": "BTCUSDT",
                        "contractType": "PERPETUAL",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                        "onboardDate": 1609459200000,
                        "filters": [
                            {
                                "filterType": "LOT_SIZE",
                                "stepSize": "0.001",
                                "minQty": "0.001",
                            },
                            {"filterType": "MIN_NOTIONAL", "notional": "5"},
                        ],
                    },
                    {
                        "symbol": "BTCUSDT_250926",
                        "contractType": "CURRENT_QUARTER",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                        "onboardDate": 1609459200000,
                        "filters": [],
                    },
                ]
            },
        )
    if request.url.path == "/fapi/v1/ticker/24hr":
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSDT",
                    "quoteVolume": "100000000",
                    "highPrice": "110",
                    "lowPrice": "90",
                    "lastPrice": "100",
                    "priceChangePercent": "5",
                }
            ],
        )
    if request.url.path == "/fapi/v1/ticker/bookTicker":
        return httpx.Response(
            200,
            json=[{"symbol": "BTCUSDT", "bidPrice": "99.9", "askPrice": "100.1"}],
        )
    return httpx.Response(404, json={"msg": "not found"})


def test_builds_candidate_inputs_from_official_endpoints() -> None:
    async def scenario():
        transport = httpx.MockTransport(_response)
        client = httpx.AsyncClient(transport=transport, base_url="https://example.test")
        adapter = BinancePublicClient(client=client)
        result = await adapter.candidate_inputs(now=datetime(2026, 1, 1, tzinfo=UTC))
        await client.aclose()
        return result

    candidates = asyncio.run(scenario())
    assert len(candidates) == 1
    assert candidates[0].symbol == "BTCUSDT"
    assert candidates[0].listing_age_days > 30
    assert str(candidates[0].volatility) == "0.2"


def test_rate_limit_exposes_retry_after() -> None:
    async def scenario():
        transport = httpx.MockTransport(
            lambda _: httpx.Response(429, headers={"Retry-After": "3"}, json={})
        )
        client = httpx.AsyncClient(transport=transport, base_url="https://example.test")
        adapter = BinancePublicClient(client=client)
        try:
            await adapter.server_time()
        finally:
            await client.aclose()

    try:
        asyncio.run(scenario())
        raise AssertionError("expected BinanceRateLimit")
    except BinanceRateLimit as exc:
        assert exc.retry_after == 3


def test_historical_klines_paginate_without_duplicates() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        start = int(request.url.params["startTime"])
        limit = int(request.url.params["limit"])
        rows = [[start + offset * 60_000, "1", "2", "0.5", "1.5", "10"] for offset in range(limit)]
        return httpx.Response(200, json=rows)

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://example.test"
        )
        adapter = BinancePublicClient(client=client)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        result = await adapter.historical_klines(
            "BTCUSDT",
            "1m",
            start,
            start.replace(hour=1),
            max_candles=2_000,
        )
        await client.aclose()
        return result

    rows = asyncio.run(scenario())
    assert len(rows) == 60
    assert len({row[0] for row in rows}) == 60
    assert len(requests) == 1


def test_historical_funding_rates_are_typed_and_paginated() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        start = int(request.url.params["startTime"])
        limit = int(request.url.params["limit"])
        return httpx.Response(
            200,
            json=[
                {
                    "symbol": "BTCUSDT",
                    "fundingTime": start + offset,
                    "fundingRate": "0.0001",
                    "markPrice": "50000.5",
                }
                for offset in range(limit)
            ],
        )

    async def scenario():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://example.test"
        )
        adapter = BinancePublicClient(client=client)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        result = await adapter.historical_funding_rates(
            "BTCUSDT",
            start,
            start.replace(day=2),
            max_events=1_200,
        )
        await client.aclose()
        return result

    events = asyncio.run(scenario())
    assert len(events) == 1_200
    assert events[0].timestamp.tzinfo is UTC
    assert str(events[0].rate) == "0.0001"
    assert str(events[0].mark_price) == "50000.5"
    assert len(requests) == 2
