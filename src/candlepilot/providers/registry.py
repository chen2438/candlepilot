from __future__ import annotations

from candlepilot.domain.models import ProviderHealth
from candlepilot.providers.base import LLMProvider
from candlepilot.providers.cli import ClaudeCodeAuthProvider, CodexAuthProvider


class ProviderRegistry:
    def __init__(self, providers: list[LLMProvider] | None = None) -> None:
        configured = providers or [CodexAuthProvider(), ClaudeCodeAuthProvider()]
        self._providers = {provider.name: provider for provider in configured}

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

