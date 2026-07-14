from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta

from candlepilot.domain.models import MarketSnapshot, PortfolioState, ProviderHealth, TradeIntent


@dataclass(frozen=True, slots=True)
class ProviderResult:
    intent: TradeIntent
    provider: str
    model: str | None
    duration: timedelta
    raw_output: str
    usage: dict[str, int | float | str]


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    subscription_auth: bool = True
    structured_output: bool = True
    tools_disabled: bool = True
    cancellable: bool = False
    max_concurrency: int = 1


class LLMProvider(ABC):
    name: str

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    async def cancel(self) -> bool:
        return False

    @abstractmethod
    async def health_check(self) -> ProviderHealth:
        raise NotImplementedError

    @abstractmethod
    async def generate_trade_intent(
        self,
        snapshot: MarketSnapshot,
        portfolio: PortfolioState,
    ) -> ProviderResult:
        raise NotImplementedError
