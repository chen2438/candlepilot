import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from pydantic import SecretStr

from candlepilot.domain.models import MarketSnapshot, PortfolioState, TradeAction
from candlepilot.providers.cli import ProviderError, ProviderInvocationError
from candlepilot.providers.openai_compatible import (
    OpenAICompatibleProvider,
    parse_chat_completion,
    parse_responses_response,
    validate_base_url,
)


def _market() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTCUSDT",
        cadence="5m",
        timestamp=datetime.now(UTC),
        mark_price="100",
        bid="99.9",
        ask="100.1",
        quote_volume_24h="1000000",
    )


def _eth_market() -> MarketSnapshot:
    return _market().model_copy(update={"symbol": "ETHUSDT", "mark_price": Decimal("200")})


def _portfolio() -> PortfolioState:
    return PortfolioState(equity="10000", available_balance="8000")


def _intent() -> dict:
    return {
        "symbol": "BTCUSDT",
        "cadence": "5m",
        "action": "HOLD",
        "confidence": 0,
        "leverage": 1,
        "risk_fraction": "0",
        "order_type": "MARKET",
        "entry_price": None,
        "stop_loss": None,
        "take_profit": None,
        "ttl_seconds": 60,
        "rationale": "no edge",
    }


def test_custom_provider_calls_chat_completions_and_parses_usage() -> None:
    secret = "test-secret-never-returned"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://llm.example/v1/chat/completions"
        assert request.headers["Authorization"] == f"Bearer {secret}"
        body = json.loads(request.content)
        assert secret not in request.content.decode()
        assert body["model"] == "vendor-model"
        assert body["reasoning_effort"] == "max"
        assert '"additionalProperties":false' in body["messages"][0]["content"]
        return httpx.Response(
            200,
            json={
                "model": "vendor-model-202607",
                "choices": [{"message": {"content": json.dumps(_intent())}}],
                "usage": {
                    "prompt_tokens": 1200,
                    "completion_tokens": 80,
                    "total_tokens": 1280,
                    "prompt_tokens_details": {"cached_tokens": 900},
                    "cost": 0.0123,
                },
            },
        )

    provider = OpenAICompatibleProvider(
        name="openai-compatible:test",
        base_url="https://llm.example/v1/",
        api_key=SecretStr(secret),
        model="vendor-model",
        reasoning_effort="max",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(provider.generate_trade_intent(_market(), _portfolio()))

    assert result.intent.action == TradeAction.HOLD
    assert result.provider == "openai-compatible:test"
    assert result.model == "vendor-model-202607"
    assert result.usage == {
        "input_tokens": 1200,
        "cached_input_tokens": 900,
        "output_tokens": 80,
        "total_tokens": 1280,
        "cost_usd": 0.0123,
    }
    assert result.input_payload is not None
    assert result.prompt is not None
    assert provider.reasoning_effort_options == (
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )


def test_custom_provider_analyzes_multiple_markets_in_one_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        prompt = json.loads(request.content)["messages"][0]["content"]
        assert '"markets":[{' in prompt
        assert prompt.count('"portfolio":') == 1
        assert "严格返回一个带有 intents 数组的对象" in prompt
        assert "每个市场必须对应一个 TradeIntent" in prompt
        assert "严格返回一个符合 TradeIntent Schema 的对象" not in prompt
        eth = {**_intent(), "symbol": "ETHUSDT", "rationale": "eth no edge"}
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps({"intents": [_intent(), eth]})}}],
                "usage": {"prompt_tokens": 101, "completion_tokens": 21, "total_tokens": 122},
            },
        )

    provider = OpenAICompatibleProvider(
        name="openai-compatible:test", base_url="https://llm.example/v1",
        api_key=SecretStr("secret"), model="vendor-model",
        transport=httpx.MockTransport(handler),
    )
    results = asyncio.run(
        provider.generate_trade_intents([_market(), _eth_market()], _portfolio())
    )

    assert calls == 1
    assert [result.intent.symbol for result in results] == ["BTCUSDT", "ETHUSDT"]
    assert sum(result.usage["input_tokens"] for result in results) == 101
    assert sum(result.usage["output_tokens"] for result in results) == 21
    assert all(result.usage["batch_shared_call"] is True for result in results)
    assert len({result.usage["physical_call_id"] for result in results}) == 1


def test_custom_provider_rejects_reordered_batch_intents() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        eth = {**_intent(), "symbol": "ETHUSDT"}
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps({"intents": [eth, _intent()]})}}]},
        )

    provider = OpenAICompatibleProvider(
        name="openai-compatible:test", base_url="https://llm.example/v1",
        api_key=SecretStr("secret"), model="vendor-model",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderInvocationError, match="invalid TradeIntent batch"):
        asyncio.run(provider.generate_trade_intents([_market(), _eth_market()], _portfolio()))


def test_custom_provider_calls_responses_with_optional_auth_and_headers() -> None:
    header_secret = "test-header-secret-never-returned"

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://llm.example/v1/responses"
        assert "Authorization" not in request.headers
        assert request.headers["x-openai-actor-authorization"] == header_secret
        body = json.loads(request.content)
        assert body["model"] == "vendor-model"
        assert body["store"] is False
        assert body["reasoning"] == {"effort": "high"}
        assert '"additionalProperties":false' in body["input"]
        assert "messages" not in body
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "model": "vendor-responses-model",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": json.dumps(_intent())}],
                    }
                ],
                "usage": {
                    "input_tokens": 1300,
                    "input_tokens_details": {"cached_tokens": 1000},
                    "output_tokens": 90,
                    "total_tokens": 1390,
                    "cost_usd": 0.02,
                },
            },
        )

    provider = OpenAICompatibleProvider(
        name="openai-compatible:test",
        base_url="https://llm.example/v1",
        api_key=None,
        model="vendor-model",
        reasoning_effort="high",
        wire_api="responses",
        require_api_key=False,
        extra_headers={"x-openai-actor-authorization": SecretStr(header_secret)},
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(provider.generate_trade_intent(_market(), _portfolio()))

    assert result.intent.action == TradeAction.HOLD
    assert result.model == "vendor-responses-model"
    assert result.provider_version == "openai-compatible-responses"
    assert result.usage == {
        "input_tokens": 1300,
        "cached_input_tokens": 1000,
        "output_tokens": 90,
        "total_tokens": 1390,
        "cost_usd": 0.02,
    }


def test_custom_provider_truncates_oversized_rationale_but_keeps_raw_output() -> None:
    oversized = _intent()
    oversized["rationale"] = "r" * 1_200

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output_text": json.dumps(oversized),
                "usage": {},
            },
        )

    provider = OpenAICompatibleProvider(
        name="openai-compatible:test",
        base_url="https://llm.example/v1",
        api_key=SecretStr("secret"),
        model="vendor-model",
        wire_api="responses",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(provider.generate_trade_intent(_market(), _portfolio()))

    assert len(result.intent.rationale) == 1_000
    assert result.usage["rationale_truncated"] is True
    assert "r" * 1_200 in result.raw_output


@pytest.mark.parametrize(
    "value",
    [
        "ftp://llm.example/v1",
        "http://llm.example/v1",
        "https://user:pass@llm.example/v1",
        "https://llm.example/v1?token=secret",
        "https://llm.example/v1#fragment",
    ],
)
def test_custom_base_url_rejects_unsafe_values(value: str) -> None:
    with pytest.raises(ValueError):
        validate_base_url(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://llm.example/v1/", "https://llm.example/v1"),
        ("http://localhost:9000/v1", "http://localhost:9000/v1"),
        ("http://127.0.0.1:9000/v1", "http://127.0.0.1:9000/v1"),
        ("http://[::1]:9000/v1", "http://[::1]:9000/v1"),
    ],
)
def test_custom_base_url_accepts_https_and_loopback(value: str, expected: str) -> None:
    assert validate_base_url(value) == expected


def test_custom_provider_health_reports_only_safe_configuration_state() -> None:
    provider = OpenAICompatibleProvider(
        name="openai-compatible:test",
        base_url="https://llm.example/v1",
        api_key=None,
        model=None,
    )
    health = asyncio.run(provider.health_check())
    rendered = health.model_dump_json()
    assert health.available is False
    assert "API key" in health.detail and "model" in health.detail
    assert "llm.example" not in rendered


def test_responses_provider_health_allows_header_only_auth() -> None:
    provider = OpenAICompatibleProvider(
        name="openai-compatible:test",
        base_url="https://llm.example/v1",
        api_key=None,
        model="vendor-model",
        wire_api="responses",
        require_api_key=False,
    )
    health = asyncio.run(provider.health_check())
    assert health.available is True
    assert health.authenticated is True
    assert health.version == "Responses"


def test_custom_provider_http_errors_do_not_expose_key_or_url() -> None:
    secret = "test-secret-never-returned"
    provider = OpenAICompatibleProvider(
        name="openai-compatible:test",
        base_url="https://private.example/v1",
        api_key=SecretStr(secret),
        model="vendor-model",
        transport=httpx.MockTransport(lambda request: httpx.Response(401)),
    )
    with pytest.raises(ProviderError) as caught:
        asyncio.run(provider.generate_trade_intent(_market(), _portfolio()))
    message = str(caught.value)
    assert "HTTP 401" in message
    assert secret not in message
    assert "private.example" not in message
    assert isinstance(caught.value, ProviderInvocationError)
    assert caught.value.model == "vendor-model"
    assert caught.value.prompt is not None
    assert caught.value.input_payload["market"]["symbol"] == "BTCUSDT"


def test_custom_provider_timeout_is_an_absolute_wall_clock_deadline() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        # MockTransport does not perform socket reads, so HTTPX's own phase
        # timeout cannot help here. The provider-level deadline still must.
        await asyncio.sleep(0.2)
        return httpx.Response(200, json={})

    provider = OpenAICompatibleProvider(
        name="openai-compatible:test",
        base_url="https://llm.example/v1",
        api_key=SecretStr("secret"),
        model="vendor-model",
        timeout=0.02,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ProviderInvocationError, match="timed out after 0.02s"):
        asyncio.run(provider.generate_trade_intent(_market(), _portfolio()))


def test_cancel_targets_the_running_request_not_a_serialized_waiter() -> None:
    async def scenario():
        started = asyncio.Event()
        release = asyncio.Event()
        calls = 0

        async def handler(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                started.set()
                await release.wait()
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output_text": json.dumps(_intent()),
                    "usage": {},
                },
            )

        provider = OpenAICompatibleProvider(
            name="openai-compatible:test",
            base_url="https://llm.example/v1",
            api_key=SecretStr("secret"),
            model="vendor-model",
            wire_api="responses",
            transport=httpx.MockTransport(handler),
        )
        running = asyncio.create_task(provider.generate_trade_intent(_market(), _portfolio()))
        await started.wait()
        queued = asyncio.create_task(provider.generate_trade_intent(_market(), _portfolio()))
        await asyncio.sleep(0)

        cancelled = await provider.cancel()
        release.set()
        queued_result = await queued
        return cancelled, running.cancelled(), queued_result.intent.action, calls

    cancelled, running_cancelled, queued_action, calls = asyncio.run(scenario())
    assert cancelled is True
    assert running_cancelled is True
    assert queued_action == TradeAction.HOLD
    assert calls == 2


def test_parse_chat_completion_rejects_missing_content() -> None:
    with pytest.raises(ProviderError, match="no assistant message"):
        parse_chat_completion({"choices": []})


def test_parse_responses_rejects_incomplete_or_missing_output() -> None:
    with pytest.raises(ProviderError, match="did not complete"):
        parse_responses_response({"status": "in_progress"})
    with pytest.raises(ProviderError, match="no output text"):
        parse_responses_response({"status": "completed", "output": []})
