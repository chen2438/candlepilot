from __future__ import annotations

from typing import TYPE_CHECKING

from candlepilot.domain.models import ProviderHealth
from candlepilot.providers.base import DecisionProvider
from candlepilot.providers.cli import ClaudeCodeAuthProvider, CodexAuthProvider
from candlepilot.providers.local import LocalRuleProvider
from candlepilot.providers.openai_compatible import OpenAICompatibleProvider

if TYPE_CHECKING:
    from candlepilot.config import Settings


class ProviderRegistry:
    def __init__(self, providers: list[DecisionProvider] | None = None) -> None:
        configured = providers or [
            LocalRuleProvider(),
            LocalRuleProvider("structure"),
            LocalRuleProvider("flow"),
            LocalRuleProvider("structure-flow"),
            CodexAuthProvider(),
            ClaudeCodeAuthProvider(),
        ]
        self._providers = {provider.name: provider for provider in configured}

    @classmethod
    def from_settings(cls, settings: Settings) -> ProviderRegistry:
        providers: list[DecisionProvider] = [
            LocalRuleProvider(),
            LocalRuleProvider("structure"),
            LocalRuleProvider("flow"),
            LocalRuleProvider("structure-flow"),
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

    def get(self, name: str) -> DecisionProvider:
        try:
            return self._providers[name]
        except KeyError as exc:
            raise KeyError(f"unknown LLM provider: {name}") from exc

    async def health(self) -> list[ProviderHealth]:
        return [await provider.health_check() for provider in self._providers.values()]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._providers)
