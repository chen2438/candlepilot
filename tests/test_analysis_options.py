import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from candlepilot.analysis.options import (
    DeribitOptionsContextProvider,
    DeribitPublicOptionsClient,
    aggregate_option_snapshot,
)


def _chain(underlying: str, *, settlement: str = "USDC"):
    now = datetime.now(UTC)
    instruments = []
    summaries = []
    for expiry_index, days in enumerate((7, 30), start=1):
        expiration = int((now + timedelta(days=days)).timestamp() * 1000)
        for strike in (80.0, 100.0, 120.0):
            for option_type, suffix in (("call", "C"), ("put", "P")):
                name = f"{underlying}-E{expiry_index}-{strike:g}-{suffix}"
                instruments.append(
                    {
                        "instrument_name": name,
                        "base_currency": underlying,
                        "settlement_currency": settlement,
                        "kind": "option",
                        "is_active": True,
                        "expiration_timestamp": expiration,
                        "strike": strike,
                        "option_type": option_type,
                    }
                )
                summaries.append(
                    {
                        "instrument_name": name,
                        "underlying_price": 100,
                        "open_interest": 2 if option_type == "call" else 3,
                        "volume": 1,
                        "mark_iv": 50 + expiry_index,
                        "bid_price": 0.1,
                        "ask_price": 0.2,
                    }
                )
    return instruments, summaries


def test_option_snapshot_aggregates_qualified_chain() -> None:
    instruments, summaries = _chain("SOL")

    snapshot = aggregate_option_snapshot("SOL", instruments, summaries, now=datetime.now(UTC))

    assert snapshot["status"] == "available"
    assert snapshot["quality"]["eligible"] is True
    assert snapshot["call_open_interest"] == 12
    assert snapshot["put_open_interest"] == 18
    assert snapshot["put_call_open_interest_ratio"] == 1.5
    assert len(snapshot["nearest_expiries"]) == 2
    assert snapshot["nearest_expiries"][0]["atm_mark_iv"] == 51


def test_option_snapshot_rejects_sparse_chain_without_exposing_metrics() -> None:
    instruments, summaries = _chain("SOL")
    instruments = instruments[:2]
    summaries = summaries[:2]

    snapshot = aggregate_option_snapshot("SOL", instruments, summaries, now=datetime.now(UTC))

    assert snapshot["status"] == "unavailable"
    assert snapshot["quality"]["eligible"] is False
    assert "call_open_interest" not in snapshot


class FakeOptionsClient:
    def __init__(self) -> None:
        chains = [_chain(underlying) for underlying in ("BTC", "ETH", "SOL")]
        self.instrument_rows = [row for instruments, _ in chains for row in instruments]
        self.summary_rows = [row for _, summaries in chains for row in summaries]
        self.instrument_calls = 0
        self.summary_calls = 0

    async def instruments(self):
        self.instrument_calls += 1
        return self.instrument_rows

    async def summaries(self, currency):
        self.summary_calls += 1
        assert currency == "USDC"
        return self.summary_rows

    async def close(self):
        return None


def test_option_context_discovers_direct_underlying_and_shares_cached_refresh() -> None:
    async def scenario():
        client = FakeOptionsClient()
        provider = DeribitOptionsContextProvider(client)  # type: ignore[arg-type]
        results = await asyncio.gather(
            provider.context("SOLUSDT"),
            provider.context("BTCUSDT"),
            provider.context("UNKNOWNUSDT"),
        )
        return client, results

    client, (sol, btc, unknown) = asyncio.run(scenario())
    assert client.instrument_calls == 1
    assert client.summary_calls == 1
    assert sol["direct"] == {
        "underlying": "SOL",
        "available": True,
        "snapshot_key": "SOL",
        "reason": None,
    }
    assert set(sol["snapshots"]) == {"BTC", "ETH", "SOL"}
    assert btc["direct"]["available"] is True
    assert unknown["direct"]["available"] is False
    assert set(unknown["snapshots"]) == {"BTC", "ETH"}
    assert unknown["benchmark_underlyings"] == ["BTC", "ETH"]


def test_option_context_degrades_when_source_fails() -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503, json={"error": "unavailable"})

    async def scenario():
        transport = httpx.MockTransport(handler)
        http_client = httpx.AsyncClient(transport=transport, base_url="https://options.invalid")
        client = DeribitPublicOptionsClient(client=http_client)
        try:
            provider = DeribitOptionsContextProvider(client)
            return await asyncio.gather(
                provider.context("SOLUSDT"),
                provider.context("BTCUSDT"),
            )
        finally:
            await http_client.aclose()

    contexts = asyncio.run(scenario())
    assert requests == 1
    assert all(context["available"] is False for context in contexts)
    assert all(context["snapshots"] == {} for context in contexts)
    assert all(
        context["direct"]["reason"] == "options source temporarily unavailable"
        for context in contexts
    )
