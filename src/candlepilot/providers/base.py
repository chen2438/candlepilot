from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import timedelta
from collections.abc import Sequence
from typing import Any

from candlepilot.domain.models import MarketSnapshot, PortfolioState, ProviderHealth, TradeIntent


@dataclass(frozen=True, slots=True)
class ProviderResult:
    intent: TradeIntent
    provider: str
    model: str | None
    duration: timedelta
    raw_output: str
    usage: dict[str, Any]
    prompt_version: str | None = None
    data_version: str | None = None
    provider_version: str | None = None
    input_payload: dict[str, Any] | None = None
    prompt: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class StructuredOutputResult:
    provider: str
    model: str | None
    duration: timedelta
    raw_output: str
    usage: dict[str, Any]
    provider_version: str | None = None
    reasoning_effort: str | None = None


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    subscription_auth: bool = True
    structured_output: bool = True
    tools_disabled: bool = True
    cancellable: bool = False
    max_concurrency: int = 1
    external_inference: bool = True
    configurable_model: bool = True
    requires_backtest_probe: bool = True
    retryable: bool = True
    estimated_seconds_per_decision: float | None = None
    live_shadow_only: bool = False


class DecisionProvider(ABC):
    name: str
    model: str | None = None
    reasoning_effort: str | None = None
    reasoning_effort_options: tuple[str, ...] = ()
    #: Seconds one decision may take before it is abandoned. Every provider
    #: already had this; it is declared here because the probe and the backtest
    #: override it for the length of a run, and a protocol that does not admit
    #: the attribute makes that a reach into a private detail.
    timeout: float = 45

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities()

    async def cancel(self) -> bool:
        return False

    async def generate_structured_output(
        self,
        *,
        prompt: str,
        output_schema: dict[str, Any],
    ) -> StructuredOutputResult:
        """Run an advisory structured prompt without granting tools.

        Providers opt in explicitly.  The local rule strategy has no language
        model and therefore keeps the safe unsupported default.
        """

        raise NotImplementedError(f"{self.name} does not support advisory analysis")

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

    async def generate_trade_intents(
        self,
        snapshots: Sequence[MarketSnapshot],
        portfolio: PortfolioState,
    ) -> list[ProviderResult]:
        """Analyze one cadence as a batch.

        The compatibility implementation keeps deterministic/test providers working;
        external providers override this to make one physical model invocation.
        """
        return [
            await self.generate_trade_intent(snapshot, portfolio)
            for snapshot in snapshots
        ]
