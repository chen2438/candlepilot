from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from decimal import Decimal
from typing import Any, Literal, Sequence
from uuid import uuid4

from pydantic import Field, ValidationError

from candlepilot.analysis.datapack import AnalysisDataPackBuilder, DATA_VERSION
from candlepilot.analysis.models import AnalysisModel, MarketAnalysis, compact_validation_error
from candlepilot.domain.models import (
    MarketSnapshot,
    OrderType,
    PortfolioState,
    TradeAction,
    TradeIntent,
)
from candlepilot.providers.base import DecisionProvider, ProviderResult
from candlepilot.providers.cli import ProviderError, ProviderInvocationError
from candlepilot.storage.database import MarketAnalysisRepository


PROMPT_VERSION = "analysis-assisted-decision-v5"
MINIMUM_CONFIDENCE = 0.55


class ExecutionHints(AnalysisModel):
    confidence: float = Field(default=0, ge=0, le=1)
    order_type: Literal["MARKET", "LIMIT"] | None = None
    ttl_seconds: int | None = Field(default=None, ge=5, le=900)
    setup_type: (
        Literal[
            "TREND_BREAKOUT",
            "TREND_CONTINUATION",
            "BREAKOUT_RETEST",
            "TREND_PULLBACK",
            "REVERSAL",
        ]
        | None
    ) = None
    trigger_type: (
        Literal["MARKET_CONFIRMED", "BREAKOUT", "RECLAIM", "REJECTION"] | None
    ) = None
    invalidation_type: Literal["SWING", "RANGE", "EMA", "DAILY_LEVEL"] | None = None
    invalidation_level: float | None = Field(default=None, gt=0)
    target_type: Literal["SWING", "RANGE", "DAILY_LEVEL", "R_MULTIPLE"] | None = None


class AnalysisAssistedDecision(AnalysisModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]+USDT$")
    analysis: MarketAnalysis
    execution: ExecutionHints = Field(default_factory=ExecutionHints)

    def has_complete_execution_hints(self) -> bool:
        if self.analysis.direction == "neutral":
            return False
        required = (
            self.execution.order_type,
            self.execution.setup_type,
            self.execution.trigger_type,
            self.execution.invalidation_type,
            self.execution.invalidation_level,
            self.execution.target_type,
        )
        if any(value is None for value in required):
            return False
        return self.execution.order_type != "LIMIT" or self.execution.ttl_seconds is not None


class AnalysisAssistedBatch(AnalysisModel):
    decisions: list[AnalysisAssistedDecision] = Field(min_length=1)


def analysis_assisted_output_schema() -> dict[str, Any]:
    schema = AnalysisAssistedBatch.model_json_schema()

    def make_strict(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            node.pop("pattern", None)
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties)
                node["additionalProperties"] = False
            for value in node.values():
                make_strict(value)
        elif isinstance(node, list):
            for value in node:
                make_strict(value)

    make_strict(schema)
    return schema


def build_analysis_assisted_prompt(data_packs: Sequence[dict[str, Any]]) -> str:
    payload = json.dumps(list(data_packs), separators=(",", ":"), ensure_ascii=False)
    return f"""You are CandlePilot's analysis-assisted decision model in SHADOW mode.
Use only DATA_PACKS below. Do not use tools, files, network access, remembered prices, or unsupported facts.

For every data pack, return exactly one decision in the same order and with the same symbol.
1. Reproduce the independent market-study method: 1h trend context, 15m structure and anchor, 5m timing; use the supplied Kansoku-style price, volume, derivatives, benchmark, account and previous-analysis data.
2. Produce the complete MarketAnalysis contract. User-facing natural language must be Simplified Chinese. Keep JSON keys, enum values, symbols, timeframes, numbers and abbreviations unchanged. Scenario probability values are percentage points from 0 to 100 and must total about 100; use 45, 30 and 25, never 0.45, 0.30 and 0.25.
3. Neutral analysis has no entry plan and all execution hints except confidence are null. Its numeric range_plan must contain anchor.price; use the current structure surrounding the anchor, not a detached future target range. If you accidentally populate neutral execution hints, CandlePilot ignores them and remains HOLD.
4. Directional analysis has explicit entry, structure-based stop, T1 and T2. T1 must be at least 1R. It must also populate order_type, setup_type, trigger_type, invalidation_type, invalidation_level and target_type. A LIMIT requires ttl_seconds from 5 to 900; a MARKET should use null ttl_seconds. If any required execution hint is unavailable, return null for it: CandlePilot will safely HOLD that symbol rather than invent the value or fail the other symbols. The application, not you, decides whether the plan passes the current hard minimum reward/risk.
5. T1 is the fixed formal take-profit field. T2 is retained only for shadow outcome comparison and must never replace T1.
6. Use MARKET only when the entry trigger is already confirmed by completed supplied 5m bars. Otherwise use LIMIT and set a 5-900 second TTL. Never infer an intrabar confirmation from an unfinished candle.
7. Set confidence to the estimated strength of the executable directional edge in the supplied snapshot, not a probability of profit. Choose structural setup, trigger, invalidation and target enum values that match the written plan.
8. Do not choose leverage, position size, or risk limits. CandlePilot deterministically fixes assisted decisions at 1x leverage and applies all existing hard-risk checks after your output.
9. Options context is supporting evidence only: distinguish direct underlying options from BTC/ETH market benchmarks; never treat open interest as signed positioning, a large-OI strike as proven support/resistance, or put/call and IV as standalone direction.
10. Missing inputs are unknown, not benign. Evidence and anchor time must be tied to supplied data, and the anchor time must include a timezone. If direct options are unavailable but benchmarks exist, state that distinction.

Return only the required JSON object.

DATA_PACKS:
{payload}
"""


class AnalysisDecisionBridge:
    def __init__(
        self,
        *,
        builder: AnalysisDataPackBuilder,
        repository: MarketAnalysisRepository,
    ) -> None:
        self.builder = builder
        self.repository = repository

    async def generate(
        self,
        *,
        provider: DecisionProvider,
        snapshots: Sequence[MarketSnapshot],
        portfolio: PortfolioState,
        persist: bool,
    ) -> list[ProviderResult]:
        if not (
            provider.capabilities.external_inference
            and provider.capabilities.structured_output
        ):
            raise ProviderError(
                "analysis-assisted decisions require an external structured-output provider"
            )
        account = self._account_payload(portfolio)
        previous_rows = await asyncio.gather(
            *(self.repository.latest_success(item.symbol) for item in snapshots)
        )
        previous = [
            {
                "created_at": row["created_at"].isoformat(),
                "result": row["result"],
            }
            if row
            else None
            for row in previous_rows
        ]
        data_packs = await asyncio.gather(
            *(
                self.builder.build(
                    snapshot.symbol,
                    account=account,
                    previous_analysis=prior,
                )
                for snapshot, prior in zip(snapshots, previous, strict=True)
            )
        )
        prompt = build_analysis_assisted_prompt(data_packs)
        analysis_ids: list[int] = []
        if persist:
            analysis_ids = await asyncio.gather(
                *(
                    self.repository.create(
                        symbol=snapshot.symbol,
                        provider=provider.name,
                        prompt_version=PROMPT_VERSION,
                        data_version=DATA_VERSION,
                    )
                    for snapshot in snapshots
                )
            )
            await asyncio.gather(
                *(
                    self.repository.start(
                        analysis_id,
                        input_payload={"data_packs": data_packs},
                        prompt=prompt,
                    )
                    for analysis_id in analysis_ids
                )
            )
        response = None
        try:
            response = await provider.generate_structured_output(
                prompt=prompt,
                output_schema=analysis_assisted_output_schema(),
            )
            batch = AnalysisAssistedBatch.model_validate_json(response.raw_output)
            expected = [item.symbol for item in snapshots]
            actual = [item.symbol for item in batch.decisions]
            if actual != expected:
                raise ProviderError("analysis-assisted decisions do not match input order")
            usage_rows = self._split_usage(response.usage, len(batch.decisions))
            results = [
                self._provider_result(
                    decision=decision,
                    snapshot=snapshot,
                    portfolio=portfolio,
                    provider=provider.name,
                    model=response.model,
                    duration=response.duration,
                    raw_output=response.raw_output,
                    usage=usage,
                    provider_version=response.provider_version,
                    reasoning_effort=response.reasoning_effort,
                    input_payload={"data_packs": data_packs},
                    prompt=prompt,
                )
                for decision, snapshot, usage in zip(
                    batch.decisions, snapshots, usage_rows, strict=True
                )
            ]
            if persist:
                await asyncio.gather(
                    *(
                        self.repository.succeed(
                            analysis_id,
                            result={
                                **decision.analysis.model_dump(mode="json"),
                                "reward_risk": decision.analysis.reward_risk(),
                            },
                            raw_output=response.raw_output,
                            usage={
                                **usage,
                                "shadow_target2": (
                                    decision.analysis.entry_plan.target2
                                    if decision.analysis.entry_plan is not None
                                    else None
                                ),
                            },
                            model=response.model,
                            reasoning_effort=response.reasoning_effort,
                            duration_ms=response.duration.total_seconds() * 1000,
                        )
                        for analysis_id, decision, usage in zip(
                            analysis_ids, batch.decisions, usage_rows, strict=True
                        )
                    )
                )
            return results
        except asyncio.CancelledError:
            if persist:
                await asyncio.gather(
                    *(
                        self.repository.fail(
                            analysis_id,
                            "analysis-assisted decision cancelled by user",
                            cancelled=True,
                        )
                        for analysis_id in analysis_ids
                    )
                )
            raise
        except Exception as exc:
            error = (
                compact_validation_error("provider returned invalid assisted analysis", exc)
                if isinstance(exc, ValidationError)
                else str(exc)
            )
            if persist:
                await asyncio.gather(
                    *(self.repository.fail(analysis_id, error) for analysis_id in analysis_ids)
                )
            if response is not None and isinstance(exc, ValidationError):
                raise ProviderInvocationError(
                    error,
                    model=response.model,
                    duration=response.duration,
                    raw_output=response.raw_output,
                    usage=response.usage,
                    prompt_version=PROMPT_VERSION,
                    data_version=DATA_VERSION,
                    provider_version=response.provider_version,
                    input_payload={"data_packs": data_packs},
                    prompt=prompt,
                ) from exc
            if isinstance(exc, ProviderError):
                raise
            if isinstance(exc, (json.JSONDecodeError, ValidationError)):
                raise ProviderError(error) from exc
            raise

    @staticmethod
    def _account_payload(portfolio: PortfolioState) -> dict[str, Any]:
        positions = []
        for symbol, position in portfolio.positions.items():
            quantity = position.quantity if position.side == "LONG" else -position.quantity
            positions.append(
                {
                    "symbol": symbol,
                    "positionAmt": str(quantity),
                    "entryPrice": str(position.entry_price),
                    "unrealizedProfit": str(position.unrealized_pnl),
                    "leverage": position.leverage,
                }
            )
        return {
            "totalWalletBalance": str(portfolio.equity),
            "availableBalance": str(portfolio.available_balance),
            "positions": positions,
        }

    @staticmethod
    def _split_usage(usage: dict[str, Any], size: int) -> list[dict[str, Any]]:
        call_id = str(uuid4())
        split_keys = {
            "input_tokens",
            "cached_input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
            "total_tokens",
        }
        rows: list[dict[str, Any]] = []
        for index in range(size):
            allocated = dict(usage)
            for key in split_keys:
                if key in usage:
                    total = int(usage.get(key) or 0)
                    allocated[key] = total // size + (1 if index < total % size else 0)
            if usage.get("cost_usd") is not None:
                allocated["cost_usd"] = float(usage["cost_usd"]) / size
            allocated.update(
                batch_size=size,
                batch_index=index + 1,
                batch_shared_call=True,
                physical_call_id=call_id,
                analysis_decision_mode="shadow",
            )
            rows.append(allocated)
        return rows

    @classmethod
    def _provider_result(
        cls,
        *,
        decision: AnalysisAssistedDecision,
        snapshot: MarketSnapshot,
        portfolio: PortfolioState,
        provider: str,
        model: str | None,
        duration: timedelta,
        raw_output: str,
        usage: dict[str, Any],
        provider_version: str | None,
        reasoning_effort: str | None,
        input_payload: dict[str, Any],
        prompt: str,
    ) -> ProviderResult:
        analysis = decision.analysis
        plan = analysis.entry_plan
        execution_ready = decision.has_complete_execution_hints()
        usage = {
            **usage,
            "shadow_target2": plan.target2 if plan is not None else None,
            "assisted_execution_ready": (
                execution_ready if analysis.direction != "neutral" else None
            ),
        }
        position = portfolio.positions.get(snapshot.symbol)
        if analysis.direction == "neutral":
            intent = TradeIntent.hold(snapshot.symbol, snapshot.cadence, analysis.summary)
        elif position is not None:
            intended_side = "LONG" if analysis.direction == "long" else "SHORT"
            if position.side == intended_side:
                intent = TradeIntent.hold(
                    snapshot.symbol,
                    snapshot.cadence,
                    f"同向持仓已存在；本轮不自动加仓。{analysis.summary}",
                )
            else:
                intent = TradeIntent(
                    symbol=snapshot.symbol,
                    cadence=snapshot.cadence,
                    action=TradeAction.CLOSE,
                    confidence=decision.execution.confidence,
                    leverage=1,
                    risk_fraction=Decimal("0"),
                    order_type=OrderType.MARKET,
                    rationale=f"AI 分析方向与现有持仓相反，仅平仓且不在同轮反手。{analysis.summary}",
                )
        elif not execution_ready:
            intent = TradeIntent.hold(
                snapshot.symbol,
                snapshot.cadence,
                f"方向分析缺少完整执行提示，不猜测交易参数；保持观望。{analysis.summary}",
            )
        elif decision.execution.confidence < MINIMUM_CONFIDENCE:
            intent = TradeIntent.hold(
                snapshot.symbol,
                snapshot.cadence,
                f"方向计划置信度低于 {MINIMUM_CONFIDENCE:.0%}，保持观望。{analysis.summary}",
            )
        else:
            assert plan is not None
            hints = decision.execution
            assert hints.order_type is not None
            intent = TradeIntent(
                symbol=snapshot.symbol,
                cadence=snapshot.cadence,
                action=(
                    TradeAction.OPEN_LONG
                    if analysis.direction == "long"
                    else TradeAction.OPEN_SHORT
                ),
                confidence=hints.confidence,
                leverage=1,
                risk_fraction=Decimal("0.01"),
                order_type=OrderType(hints.order_type),
                entry_price=Decimal(str(plan.entry)),
                stop_loss=Decimal(str(plan.stop)),
                take_profit=Decimal(str(plan.target1)),
                ttl_seconds=hints.ttl_seconds or 60,
                decision_framework="structure-v1",
                setup_type=hints.setup_type,
                anchor_timeframe=analysis.anchor.timeframe,
                anchor_price=Decimal(str(analysis.anchor.price)),
                trigger_type=hints.trigger_type,
                trigger_price=Decimal(str(plan.entry)),
                invalidation_type=hints.invalidation_type,
                invalidation_level=Decimal(str(hints.invalidation_level)),
                target_type=hints.target_type,
                rationale=analysis.summary,
            )
        return ProviderResult(
            intent=intent,
            provider=provider,
            model=model,
            duration=duration,
            raw_output=raw_output,
            usage=usage,
            prompt_version=PROMPT_VERSION,
            data_version=DATA_VERSION,
            provider_version=provider_version,
            input_payload=input_payload,
            prompt=prompt,
            reasoning_effort=reasoning_effort,
        )
