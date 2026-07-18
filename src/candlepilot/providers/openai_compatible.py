from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Mapping
from datetime import timedelta
from typing import Any

import httpx
from pydantic import SecretStr, ValidationError

from candlepilot.domain.models import MarketSnapshot, PortfolioState, ProviderHealth
from candlepilot.providers.base import DecisionProvider, ProviderCapabilities, ProviderResult
from candlepilot.providers.cli import (
    MAX_OUTPUT_BYTES,
    ProviderError,
    ProviderInvocationError,
    ProviderUnavailable,
    _decision_payload,
    _decision_prompt,
    _parse_intent,
)
from candlepilot.provenance import (
    DECISION_PROMPT_VERSION,
    MARKET_SNAPSHOT_SCHEMA_VERSION,
    content_fingerprint,
)


LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
WIRE_APIS = {"chat-completions", "responses"}


def validate_base_url(value: str) -> str:
    """Validate a user-supplied API root without ever including it in errors."""

    try:
        url = httpx.URL(value.strip())
    except Exception as exc:
        raise ValueError("custom LLM base URL is invalid") from exc
    if not url.host or url.scheme not in {"http", "https"}:
        raise ValueError("custom LLM base URL must use http or https")
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("custom LLM base URL cannot contain credentials, query, or fragment")
    if url.scheme == "http" and url.host.lower() not in LOOPBACK_HOSTS:
        raise ValueError("custom LLM base URL must use HTTPS unless it is a loopback address")
    return str(url).rstrip("/")


def _parse_usage(raw_usage: Any, *, details_key: str) -> dict[str, Any]:
    raw_usage = raw_usage or {}
    if not isinstance(raw_usage, dict):
        raise ProviderError("OpenAI-compatible endpoint returned invalid token usage")
    input_details = raw_usage.get(details_key) or {}
    if not isinstance(input_details, dict):
        raise ProviderError("OpenAI-compatible endpoint returned invalid token usage")
    try:
        input_tokens = int(
            raw_usage.get("prompt_tokens", raw_usage.get("input_tokens", 0)) or 0
        )
        output_tokens = int(
            raw_usage.get("completion_tokens", raw_usage.get("output_tokens", 0)) or 0
        )
        cached_tokens = int(
            input_details.get("cached_tokens", raw_usage.get("cached_input_tokens", 0)) or 0
        )
        total_tokens = int(raw_usage.get("total_tokens") or input_tokens + output_tokens)
    except (TypeError, ValueError) as exc:
        raise ProviderError("OpenAI-compatible endpoint returned invalid token usage") from exc
    if min(input_tokens, output_tokens, cached_tokens, total_tokens) < 0:
        raise ProviderError("OpenAI-compatible endpoint returned invalid token usage")
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }
    reported_cost = raw_usage.get("cost_usd", raw_usage.get("cost"))
    if (
        isinstance(reported_cost, (int, float))
        and not isinstance(reported_cost, bool)
        and math.isfinite(reported_cost)
        and reported_cost >= 0
    ):
        usage["cost_usd"] = float(reported_cost)
    return usage


def parse_chat_completion(payload: dict[str, Any]) -> tuple[str, str | None, dict[str, Any]]:
    try:
        content = payload["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError("OpenAI-compatible endpoint returned no assistant message") from exc
    if not isinstance(content, str) or not content.strip():
        raise ProviderError("OpenAI-compatible endpoint returned an empty assistant message")
    usage = _parse_usage(payload.get("usage"), details_key="prompt_tokens_details")
    model = payload.get("model")
    return content, model if isinstance(model, str) and model else None, usage


def parse_responses_response(
    payload: dict[str, Any],
) -> tuple[str, str | None, dict[str, Any]]:
    if payload.get("status") not in {None, "completed"}:
        raise ProviderError("OpenAI-compatible Responses request did not complete")
    output_text = payload.get("output_text")
    texts = [output_text] if isinstance(output_text, str) and output_text.strip() else []
    if not texts:
        output = payload.get("output") or []
        if not isinstance(output, list):
            raise ProviderError("OpenAI-compatible Responses endpoint returned invalid output")
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content") or []
            if not isinstance(content, list):
                continue
            texts.extend(
                part["text"]
                for part in content
                if isinstance(part, dict)
                and part.get("type") == "output_text"
                and isinstance(part.get("text"), str)
                and part["text"].strip()
            )
    if not texts:
        raise ProviderError("OpenAI-compatible Responses endpoint returned no output text")
    usage = _parse_usage(payload.get("usage"), details_key="input_tokens_details")
    model = payload.get("model")
    return "".join(texts), model if isinstance(model, str) and model else None, usage


class OpenAICompatibleProvider(DecisionProvider):
    """User-configured Responses or Chat Completions endpoint with local validation."""

    name = "openai-compatible"
    reasoning_effort_options = ("low", "medium", "high", "xhigh")

    def __init__(
        self,
        *,
        base_url: str | None,
        api_key: SecretStr | None,
        model: str | None,
        name: str | None = None,
        reasoning_effort: str | None = None,
        wire_api: str = "chat-completions",
        require_api_key: bool = True,
        extra_headers: Mapping[str, SecretStr] | None = None,
        timeout: float = 45,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # Several endpoints can be configured at once, so the name is per instance
        # and shadows the class default used by the single legacy configuration.
        if name:
            self.name = name
        self.base_url: str | None = None
        self._configuration_error: str | None = None
        if base_url:
            try:
                self.base_url = validate_base_url(base_url)
            except ValueError as exc:
                self._configuration_error = str(exc)
        self.api_key = api_key
        self.model = model.strip() if model and model.strip() else None
        self.reasoning_effort = (
            reasoning_effort.strip()
            if reasoning_effort and reasoning_effort.strip()
            else None
        )
        if wire_api not in WIRE_APIS:
            raise ValueError("custom LLM wire API must be chat-completions or responses")
        self.wire_api = wire_api
        self.require_api_key = require_api_key
        self.extra_headers = dict(extra_headers or {})
        self.timeout = timeout
        self._transport = transport
        self._semaphore = asyncio.Semaphore(1)
        self._active_task: asyncio.Task[Any] | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            subscription_auth=False,
            structured_output=False,
            tools_disabled=True,
            cancellable=True,
        )

    def _missing_configuration(self) -> list[str]:
        missing = []
        if self.base_url is None:
            missing.append("base URL")
        if self.require_api_key and (
            self.api_key is None or not self.api_key.get_secret_value()
        ):
            missing.append("API key")
        if not self.model:
            missing.append("model")
        return missing

    async def health_check(self) -> ProviderHealth:
        if self._configuration_error is not None:
            return ProviderHealth(
                provider=self.name,
                available=False,
                authenticated=False,
                detail=self._configuration_error,
            )
        missing = self._missing_configuration()
        if missing:
            return ProviderHealth(
                provider=self.name,
                available=False,
                authenticated=False,
                detail=f"Missing custom LLM configuration: {', '.join(missing)}",
            )
        return ProviderHealth(
            provider=self.name,
            available=True,
            authenticated=True,
            version="Responses" if self.wire_api == "responses" else "Chat Completions",
            detail="Configured; use the provider test to verify connectivity",
        )

    async def cancel(self) -> bool:
        task = self._active_task
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def generate_trade_intent(
        self, snapshot: MarketSnapshot, portfolio: PortfolioState
    ) -> ProviderResult:
        health = await self.health_check()
        if not health.available or not health.authenticated:
            raise ProviderUnavailable(health.detail)
        assert self.base_url is not None
        assert self.model is not None

        started = time.monotonic()
        input_payload = _decision_payload(snapshot, portfolio)
        prompt = _decision_prompt(snapshot, portfolio, include_schema=True)
        data_version = content_fingerprint(
            snapshot.model_dump(mode="json"),
            schema_version=MARKET_SNAPSHOT_SCHEMA_VERSION,
        )
        provider_version = f"openai-compatible-{self.wire_api}"

        def invocation_error(
            message: str,
            *,
            raw_output: str = "",
            usage: dict[str, Any] | None = None,
            model: str | None = None,
        ) -> ProviderInvocationError:
            return ProviderInvocationError(
                message,
                model=model or self.model,
                duration=timedelta(seconds=time.monotonic() - started),
                raw_output=raw_output,
                usage=usage or {},
                prompt_version=DECISION_PROMPT_VERSION,
                data_version=data_version,
                provider_version=provider_version,
                input_payload=input_payload,
                prompt=prompt,
            )
        if self.wire_api == "responses":
            endpoint = f"{self.base_url}/responses"
            request: dict[str, Any] = {
                "model": self.model,
                "input": prompt,
                "store": False,
            }
            if self.reasoning_effort:
                request["reasoning"] = {"effort": self.reasoning_effort}
        else:
            endpoint = f"{self.base_url}/chat/completions"
            request = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
            }
            if self.reasoning_effort:
                request["reasoning_effort"] = self.reasoning_effort

        headers = {
            name: value.get_secret_value() for name, value in self.extra_headers.items()
        }
        headers["Content-Type"] = "application/json"
        if self.require_api_key:
            assert self.api_key is not None
            headers["Authorization"] = f"Bearer {self.api_key.get_secret_value()}"

        try:
            async with self._semaphore:
                active = asyncio.current_task()
                self._active_task = active
                try:
                    async with httpx.AsyncClient(
                        timeout=self.timeout,
                        follow_redirects=False,
                        transport=self._transport,
                    ) as client:
                        response = await client.post(
                            endpoint,
                            headers=headers,
                            json=request,
                        )
                finally:
                    if self._active_task is active:
                        self._active_task = None
        except httpx.TimeoutException as exc:
            raise invocation_error(
                f"OpenAI-compatible endpoint timed out after {self.timeout:g}s"
            ) from exc
        except httpx.HTTPError as exc:
            raise invocation_error("OpenAI-compatible endpoint could not be reached") from exc

        if response.is_redirect:
            raise invocation_error("OpenAI-compatible endpoint redirects are not allowed")
        if response.status_code >= 400:
            raise invocation_error(
                f"OpenAI-compatible endpoint returned HTTP {response.status_code}"
            )
        if len(response.content) > MAX_OUTPUT_BYTES:
            raise invocation_error("OpenAI-compatible endpoint response exceeded the size limit")
        result_text = ""
        response_model: str | None = None
        usage: dict[str, Any] = {}
        try:
            envelope = response.json()
            if not isinstance(envelope, dict):
                raise TypeError
            if self.wire_api == "responses":
                result_text, response_model, usage = parse_responses_response(envelope)
            else:
                result_text, response_model, usage = parse_chat_completion(envelope)
            intent, rationale_truncated = _parse_intent(result_text)
        except (ProviderError, json.JSONDecodeError, TypeError, ValidationError) as exc:
            raise invocation_error(
                "OpenAI-compatible endpoint returned an invalid TradeIntent",
                raw_output=result_text,
                usage=usage,
                model=response_model,
            ) from exc

        if rationale_truncated:
            usage["rationale_truncated"] = True
        return ProviderResult(
            intent=intent,
            provider=self.name,
            model=response_model or self.model,
            duration=timedelta(seconds=time.monotonic() - started),
            raw_output=result_text,
            usage=usage,
            prompt_version=DECISION_PROMPT_VERSION,
            data_version=data_version,
            provider_version=provider_version,
            input_payload=input_payload,
            prompt=prompt,
            reasoning_effort=self.reasoning_effort,
        )
