from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from candlepilot.storage.models import MarketAnalysisOutcomeRow, MarketAnalysisRow


class MarketAnalysisRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def create(self, *, symbol: str, provider: str, prompt_version: str, data_version: str) -> int:
        row = MarketAnalysisRow(
            symbol=symbol,
            status="pending",
            provider=provider,
            prompt_version=prompt_version,
            data_version=data_version,
        )
        async with self.sessions.begin() as session:
            session.add(row)
            await session.flush()
        return row.id

    async def start(self, analysis_id: int, *, input_payload: Mapping[str, Any], prompt: str) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(MarketAnalysisRow)
                .where(MarketAnalysisRow.id == analysis_id)
                .values(
                    status="running",
                    input_json=json.dumps(input_payload, separators=(",", ":"), ensure_ascii=False),
                    prompt_text=prompt,
                )
            )

    async def succeed(
        self,
        analysis_id: int,
        *,
        result: Mapping[str, Any],
        raw_output: str,
        usage: Mapping[str, Any],
        model: str | None,
        reasoning_effort: str | None,
        duration_ms: float,
    ) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(MarketAnalysisRow)
                .where(MarketAnalysisRow.id == analysis_id)
                .values(
                    status="succeeded",
                    result_json=json.dumps(result, separators=(",", ":"), ensure_ascii=False),
                    raw_output=raw_output,
                    usage_json=json.dumps(dict(usage), separators=(",", ":")),
                    model=model,
                    reasoning_effort=reasoning_effort,
                    duration_ms=duration_ms,
                    error=None,
                    completed_at=datetime.now(UTC),
                )
            )

    async def fail(self, analysis_id: int, error: str, *, cancelled: bool = False) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(MarketAnalysisRow)
                .where(MarketAnalysisRow.id == analysis_id)
                .values(
                    status="cancelled" if cancelled else "failed",
                    error=error[:2000],
                    completed_at=datetime.now(UTC),
                )
            )

    async def fail_open(self) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                update(MarketAnalysisRow)
                .where(MarketAnalysisRow.status.in_(("pending", "running")))
                .values(
                    status="failed",
                    error="analysis interrupted because the service restarted",
                    completed_at=datetime.now(UTC),
                )
            )

    async def save_outcome(self, analysis_id: int, outcome: Mapping[str, Any]) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(MarketAnalysisOutcomeRow, analysis_id)
            values = json.dumps(outcome, separators=(",", ":"), ensure_ascii=False)
            if row is None:
                session.add(
                    MarketAnalysisOutcomeRow(
                        analysis_id=analysis_id,
                        outcome_json=values,
                        updated_at=datetime.now(UTC),
                    )
                )
            else:
                row.outcome_json = values
                row.updated_at = datetime.now(UTC)

    async def outcome(self, analysis_id: int) -> dict[str, Any] | None:
        async with self.sessions() as session:
            row = await session.get(MarketAnalysisOutcomeRow, analysis_id)
        if row is None:
            return None
        updated_at = row.updated_at.replace(tzinfo=UTC) if row.updated_at.tzinfo is None else row.updated_at
        return {
            "outcome": json.loads(row.outcome_json),
            "outcome_updated_at": updated_at,
        }

    async def attach_outcome(self, payload: dict[str, Any]) -> dict[str, Any]:
        stored = await self.outcome(int(payload["id"]))
        return {
            **payload,
            "outcome": stored["outcome"] if stored else None,
            "outcome_updated_at": stored["outcome_updated_at"] if stored else None,
        }

    async def get(self, analysis_id: int, *, include_audit: bool = False) -> dict[str, Any] | None:
        async with self.sessions() as session:
            row = await session.get(MarketAnalysisRow, analysis_id)
        if row is None:
            return None
        return await self.attach_outcome(self._as_dict(row, include_audit=include_audit))

    async def recent(self, *, limit: int = 30, symbol: str | None = None) -> list[dict[str, Any]]:
        statement = select(MarketAnalysisRow)
        if symbol is not None:
            statement = statement.where(MarketAnalysisRow.symbol == symbol)
        async with self.sessions() as session:
            rows = (
                await session.scalars(statement.order_by(MarketAnalysisRow.id.desc()).limit(limit))
            ).all()
        return [
            await self.attach_outcome(self._as_dict(row, include_audit=False))
            for row in rows
        ]

    async def latest_success(self, symbol: str) -> dict[str, Any] | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(MarketAnalysisRow)
                .where(
                    MarketAnalysisRow.symbol == symbol,
                    MarketAnalysisRow.status == "succeeded",
                )
                .order_by(MarketAnalysisRow.id.desc())
                .limit(1)
            )
        if row is None:
            return None
        return await self.attach_outcome(self._as_dict(row, include_audit=False))

    async def performance_records(self) -> list[dict[str, Any]]:
        statement = (
            select(MarketAnalysisRow, MarketAnalysisOutcomeRow)
            .outerjoin(
                MarketAnalysisOutcomeRow,
                MarketAnalysisOutcomeRow.analysis_id == MarketAnalysisRow.id,
            )
            .where(
                MarketAnalysisRow.status == "succeeded",
                MarketAnalysisRow.result_json.is_not(None),
            )
            .order_by(MarketAnalysisRow.id)
        )
        async with self.sessions() as session:
            rows = (await session.execute(statement)).all()
        return [
            {
                "id": analysis.id,
                "symbol": analysis.symbol,
                "result": json.loads(analysis.result_json or "null"),
                "outcome": json.loads(outcome.outcome_json) if outcome else None,
            }
            for analysis, outcome in rows
        ]

    @staticmethod
    def _as_dict(row: MarketAnalysisRow, *, include_audit: bool) -> dict[str, Any]:
        created_at = row.created_at.replace(tzinfo=UTC) if row.created_at.tzinfo is None else row.created_at
        completed_at = row.completed_at
        if completed_at is not None and completed_at.tzinfo is None:
            completed_at = completed_at.replace(tzinfo=UTC)
        payload: dict[str, Any] = {
            "id": row.id,
            "symbol": row.symbol,
            "status": row.status,
            "provider": row.provider,
            "model": row.model,
            "reasoning_effort": row.reasoning_effort,
            "prompt_version": row.prompt_version,
            "data_version": row.data_version,
            "result": json.loads(row.result_json) if row.result_json else None,
            "usage": json.loads(row.usage_json),
            "duration_ms": row.duration_ms,
            "error": row.error,
            "created_at": created_at,
            "completed_at": completed_at,
        }
        if include_audit:
            payload.update(
                {
                    "input": json.loads(row.input_json) if row.input_json else None,
                    "prompt": row.prompt_text,
                    "raw_output": row.raw_output,
                }
            )
        return payload

