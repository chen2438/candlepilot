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
        return cls(
            [
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
                OpenAICompatibleProvider(
                    timeout=settings.inference_timeout_seconds,
                    base_url=settings.custom_llm_base_url,
                    api_key=settings.custom_llm_api_key,
                    model=settings.custom_llm_model,
                    reasoning_effort=settings.custom_llm_reasoning_effort,
                ),
            ]
        )

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
