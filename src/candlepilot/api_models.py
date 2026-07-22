from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from candlepilot.application.scheduler import MAX_CANDIDATES_PER_CYCLE
from candlepilot.backtest.probe import MAX_SUGGESTED_TIMEOUT
from candlepilot.backtest.runner import MAX_BACKTEST_MODELS, MAX_BACKTEST_SYMBOLS
from candlepilot.config import MAX_CUSTOM_LLM_PROVIDERS


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(ApiModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class ProviderSelection(ApiModel):
    providers: list[str] = Field(min_length=1, max_length=1)


class ProviderConfig(ApiModel):
    name: str
    model: str | None = None
    reasoning_effort: str | None = None
    pricing: str | None = None
    auth_source: str | None = None


class ProviderTestRequest(ApiModel):
    name: str


class CustomProviderInput(ApiModel):
    id: str
    base_url: str
    model: str | None = None
    reasoning_effort: str | None = None
    wire_api: str = "chat-completions"
    require_api_key: bool = True
    pricing: str | None = None
    # None keeps the stored key, "" clears it, any other value replaces it — the
    # frontend never receives the current key, so it cannot send it back.
    api_key: str | None = None
    extra_headers: dict[str, str] | None = None


class CustomProvidersUpdate(ApiModel):
    providers: list[CustomProviderInput] = Field(max_length=MAX_CUSTOM_LLM_PROVIDERS)


class SettingsUpdate(ApiModel):
    # Only the keys the frontend actually changed are sent, so an untouched
    # secret is never echoed back as its own mask.
    values: dict[str, str] = Field(max_length=64)


class RunLimits(ApiModel):
    max_run_seconds: int | None = Field(default=None, gt=0, le=7 * 24 * 3600)
    max_run_cost_usd: float | None = Field(default=None, gt=0, le=10_000)


class EngineStartRequest(ApiModel):
    timeout_seconds: float | None = Field(default=None, gt=0, le=MAX_SUGGESTED_TIMEOUT)


class ClosePositionRequest(ApiModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]+USDT$")


class MarketAnalysisRequest(ApiModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]+USDT$")


class MarketAnalysisOutcomeBatchRequest(ApiModel):
    analysis_ids: list[Annotated[int, Field(gt=0)]] = Field(
        min_length=1, max_length=30
    )


class HistoryClearRequest(ApiModel):
    categories: list[str] = Field(min_length=1, max_length=16)


class CadenceSelection(ApiModel):
    cadences: list[str] = Field(min_length=1, max_length=1)


class AnalysisDecisionModeSelection(ApiModel):
    mode: Literal["off", "shadow"]


class CandidatesPerCycleSelection(ApiModel):
    candidates_per_cycle: int = Field(ge=1, le=MAX_CANDIDATES_PER_CYCLE)


class BacktestConfigInput(ApiModel):
    initial_equity: Annotated[Decimal, Field(gt=0)] = Decimal("10000")
    fee_rate: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.0005")
    slippage_fraction: Annotated[Decimal, Field(ge=0, le=1)] = Decimal("0.0005")


class BacktestRequest(ApiModel):
    symbols: list[str] = Field(min_length=1, max_length=MAX_BACKTEST_SYMBOLS)
    cadences: list[str] = Field(default=["5m"], min_length=1, max_length=5)
    start: datetime
    end: datetime
    providers: list[str] = Field(min_length=1, max_length=MAX_BACKTEST_MODELS)
    config: BacktestConfigInput = Field(default_factory=BacktestConfigInput)
    replay_live_run_id: int | None = Field(default=None, gt=0)
    # Set from a probe of these providers. None inherits the providers'
    # configured timeout when the run is created; that effective value is
    # then frozen on the run for reproducibility.
    timeout_seconds: float | None = Field(default=None, gt=0, le=MAX_SUGGESTED_TIMEOUT)

