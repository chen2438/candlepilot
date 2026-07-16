from __future__ import annotations

from typing import TYPE_CHECKING

from candlepilot.domain.models import ProviderHealth
from candlepilot.providers.base import LLMProvider
from candlepilot.providers.cli import ClaudeCodeAuthProvider, CodexAuthProvider
from candlepilot.providers.openai_compatible import OpenAICompatibleProvider

if TYPE_CHECKING:
    from candlepilot.config import Settings


class ProviderRegistry:
    def __init__(self, providers: list[LLMProvider] | None = None) -> None:
        configured = providers or [CodexAuthProvider(), ClaudeCodeAuthProvider()]
        self._providers = {provider.name: provider for provider in configured}

    @classmethod
    def from_settings(cls, settings: Settings) -> ProviderRegistry:
        providers: list[LLMProvider] = [
            CodexAuthProvider(
                timeout=settings.inference_timeout_seconds,
                model=settings.codex_model,
                reasoning_effort=settings.codex_reasoning_effort,
            ),
            ClaudeCodeAuthProvider(
                timeout=settings.inference_timeout_seconds,
                model=settings.claude_model,
                reasoning_effort=settings.claude_effort,
            ),
            # The unsuffixed endpoint from the flat CANDLEPILOT_CUSTOM_LLM_* vars is
            # always present, so an unconfigured install still reports it as such.
            OpenAICompatibleProvider(
                timeout=settings.inference_timeout_seconds,
                base_url=settings.custom_llm_base_url,
                api_key=settings.custom_llm_api_key,
                model=settings.custom_llm_model,
                reasoning_effort=settings.custom_llm_reasoning_effort,
                wire_api=settings.custom_llm_wire_api,
                require_api_key=settings.custom_llm_require_api_key,
                extra_headers=settings.custom_llm_extra_headers,
            ),
        ]
        providers.extend(
            OpenAICompatibleProvider(
                timeout=settings.inference_timeout_seconds,
                name=custom.provider_name,
                base_url=custom.base_url,
                api_key=custom.api_key,
                model=custom.model,
                reasoning_effort=custom.reasoning_effort,
                wire_api=custom.wire_api,
                require_api_key=custom.require_api_key,
                extra_headers=custom.extra_headers,
            )
            for custom in settings.custom_llm_providers
        )
        return cls(providers)

    def get(self, name: str) -> LLMProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise KeyError(f"unknown LLM provider: {name}") from exc

    async def health(self) -> list[ProviderHealth]:
        return [await provider.health_check() for provider in self._providers.values()]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._providers)
