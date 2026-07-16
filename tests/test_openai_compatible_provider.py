import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest
from pydantic import SecretStr

from candlepilot.domain.models import MarketSnapshot, PortfolioState, TradeAction
from candlepilot.providers.cli import ProviderError
from candlepilot.providers.openai_compatible import (
    OpenAICompatibleProvider,
    parse_chat_completion,
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
        assert body["reasoning_effort"] == "high"
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
        base_url="https://llm.example/v1/",
        api_key=SecretStr(secret),
        model="vendor-model",
        reasoning_effort="high",
        transport=httpx.MockTransport(handler),
    )
    result = asyncio.run(provider.generate_trade_intent(_market(), _portfolio()))

    assert result.intent.action == TradeAction.HOLD
    assert result.provider == "openai-compatible"
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
        base_url="https://llm.example/v1",
        api_key=None,
        model=None,
    )
    health = asyncio.run(provider.health_check())
    rendered = health.model_dump_json()
    assert health.available is False
    assert "API key" in health.detail and "model" in health.detail
    assert "llm.example" not in rendered


def test_custom_provider_http_errors_do_not_expose_key_or_url() -> None:
    secret = "test-secret-never-returned"
    provider = OpenAICompatibleProvider(
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


def test_parse_chat_completion_rejects_missing_content() -> None:
    with pytest.raises(ProviderError, match="no assistant message"):
        parse_chat_completion({"choices": []})
