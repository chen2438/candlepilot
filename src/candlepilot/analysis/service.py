from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from pydantic import ValidationError

from candlepilot.analysis.datapack import AnalysisDataPackBuilder, DATA_VERSION
from candlepilot.analysis.models import MarketAnalysis, market_analysis_output_schema
from candlepilot.analysis.prompt import PROMPT_VERSION, build_analysis_prompt
from candlepilot.providers.base import DecisionProvider
from candlepilot.storage.database import MarketAnalysisRepository


AccountLoader = Callable[[], Awaitable[Mapping[str, Any] | None]]


class MarketAnalysisService:
    def __init__(
        self,
        *,
        builder: AnalysisDataPackBuilder,
        repository: MarketAnalysisRepository,
        account_loader: AccountLoader,
    ) -> None:
        self.builder = builder
        self.repository = repository
        self.account_loader = account_loader

    async def create(self, *, symbol: str, provider: DecisionProvider) -> int:
        return await self.repository.create(
            symbol=symbol,
            provider=provider.name,
            prompt_version=PROMPT_VERSION,
            data_version=DATA_VERSION,
        )

    async def run(self, analysis_id: int, *, symbol: str, provider: DecisionProvider) -> None:
        try:
            try:
                account = await self.account_loader()
            except Exception:
                account = None
            previous = await self.repository.latest_success(symbol)
            previous_result = (
                {
                    "created_at": previous["created_at"].isoformat(),
                    "result": previous["result"],
                }
                if previous
                else None
            )
            data_pack = await self.builder.build(
                symbol,
                account=account,
                previous_analysis=previous_result,
            )
            prompt = build_analysis_prompt(data_pack)
            await self.repository.start(analysis_id, input_payload=data_pack, prompt=prompt)
            response = await provider.generate_structured_output(
                prompt=prompt,
                output_schema=market_analysis_output_schema(),
            )
            parsed = json.loads(response.raw_output)
            analysis = MarketAnalysis.model_validate(parsed)
            result = analysis.model_dump(mode="json")
            result["reward_risk"] = analysis.reward_risk()
            await self.repository.succeed(
                analysis_id,
                result=result,
                raw_output=response.raw_output,
                usage=response.usage,
                model=response.model,
                reasoning_effort=response.reasoning_effort,
                duration_ms=response.duration.total_seconds() * 1000,
            )
        except asyncio.CancelledError:
            await self.repository.fail(
                analysis_id, "analysis cancelled by user", cancelled=True
            )
            raise
        except (json.JSONDecodeError, ValidationError) as exc:
            await self.repository.fail(
                analysis_id, f"provider returned invalid analysis: {exc}"
            )
        except Exception as exc:
            await self.repository.fail(analysis_id, str(exc))
