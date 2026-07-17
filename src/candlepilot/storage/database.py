from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    and_,
    delete,
    event,
    func,
    insert,
    not_,
    select,
    text,
)
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from candlepilot.broker.user_stream import UserStreamEvent
from candlepilot.domain.models import ExecutionAttempt, ExecutionReport, RiskDecision, TradeIntent
from candlepilot.providers.base import ProviderResult
from candlepilot.providers.pricing import PROVIDER_IDS, ModelPricingCatalog


class Base(DeclarativeBase):
    pass


class InferenceRow(Base):
    __tablename__ = "inferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    cadence: Mapped[str] = mapped_column(String(8), index=True)
    intent_json: Mapped[str] = mapped_column(Text)
    raw_output: Mapped[str] = mapped_column(Text)
    usage_json: Mapped[str] = mapped_column(Text, default="{}")
    duration_ms: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class InferenceDetailRow(Base):
    __tablename__ = "inference_details"

    inference_id: Mapped[int] = mapped_column(
        ForeignKey("inferences.id", ondelete="CASCADE"), primary_key=True
    )
    input_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_text: Mapped[str | None] = mapped_column(Text, nullable=True)


class RiskRow(Base):
    __tablename__ = "risk_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    inference_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    accepted: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text)
    decision_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class ExecutionRow(Base):
    __tablename__ = "executions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    report_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class ExecutionAttemptRow(Base):
    __tablename__ = "execution_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    inference_id: Mapped[int] = mapped_column(
        ForeignKey("inferences.id", ondelete="CASCADE"), unique=True, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    client_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    attempt_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class RuntimeStateRow(Base):
    __tablename__ = "runtime_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )


class UserStreamEventRow(Base):
    __tablename__ = "user_stream_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    transaction_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    payload_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class AlertEventRow(Base):
    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    alert_id: Mapped[str] = mapped_column(String(96), index=True)
    transition: Mapped[str] = mapped_column(String(16), index=True)
    severity: Mapped[str] = mapped_column(String(16))
    source: Mapped[str] = mapped_column(String(64), index=True)
    title: Mapped[str] = mapped_column(Text)
    detail: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class SchemaMigrationRow(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (
        1,
        (
            "CREATE INDEX IF NOT EXISTS ix_user_stream_event_symbol_time "
            "ON user_stream_events (event_type, symbol, event_time)",
        ),
    ),
    (
        2,
        (
            "CREATE TABLE IF NOT EXISTS inference_details ("
            "inference_id INTEGER NOT NULL PRIMARY KEY, "
            "input_json TEXT, prompt_text TEXT, "
            "FOREIGN KEY(inference_id) REFERENCES inferences(id) ON DELETE CASCADE)",
        ),
    ),
    (
        3,
        (
            "CREATE TABLE IF NOT EXISTS execution_attempts ("
            "id INTEGER NOT NULL PRIMARY KEY, "
            "inference_id INTEGER NOT NULL, symbol VARCHAR(32) NOT NULL, "
            "client_order_id VARCHAR(64), status VARCHAR(32) NOT NULL, "
            "stage VARCHAR(32) NOT NULL, attempt_json TEXT NOT NULL, "
            "created_at DATETIME NOT NULL, "
            "FOREIGN KEY(inference_id) REFERENCES inferences(id) ON DELETE CASCADE)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_execution_attempts_inference_id "
            "ON execution_attempts (inference_id)",
            "CREATE INDEX IF NOT EXISTS ix_execution_attempts_symbol "
            "ON execution_attempts (symbol)",
            "CREATE INDEX IF NOT EXISTS ix_execution_attempts_client_order_id "
            "ON execution_attempts (client_order_id)",
            "CREATE INDEX IF NOT EXISTS ix_execution_attempts_status "
            "ON execution_attempts (status)",
            "CREATE INDEX IF NOT EXISTS ix_execution_attempts_created_at "
            "ON execution_attempts (created_at)",
        ),
    ),
    (
        4,
        # The old backtest is gone. Its stored results came from a payload that
        # never matched what live sends -- single timeframe, unprefixed, no
        # daily levels -- so keeping them would invite comparing them against
        # the rewrite's numbers as though the two measured the same thing.
        (
            "DROP TABLE IF EXISTS backtests",
            # The simulated account is gone too: testnet is the only account now.
            "DELETE FROM runtime_state WHERE key = 'paper_account'",
        ),
    ),
)
CURRENT_SCHEMA_VERSION = max(version for version, _ in MIGRATIONS)


class Database:
    def __init__(self, url: str) -> None:
        self.engine: AsyncEngine = create_async_engine(url)
        if url.startswith("sqlite"):
            event.listen(self.engine.sync_engine, "connect", self._configure_sqlite)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @staticmethod
    def _configure_sqlite(connection: Any, _: Any) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    async def initialize(self) -> None:
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await self._apply_migrations(connection)

    @staticmethod
    async def _apply_migrations(connection: AsyncConnection) -> None:
        current = await connection.scalar(
            select(SchemaMigrationRow.version).order_by(SchemaMigrationRow.version.desc()).limit(1)
        )
        for version, statements in MIGRATIONS:
            if current is not None and version <= current:
                continue
            for statement in statements:
                await connection.execute(text(statement))
            await connection.execute(
                insert(SchemaMigrationRow).values(
                    version=version,
                    applied_at=datetime.now(UTC),
                )
            )

    async def schema_version(self) -> int:
        async with self.sessions() as session:
            version = await session.scalar(
                select(SchemaMigrationRow.version)
                .order_by(SchemaMigrationRow.version.desc())
                .limit(1)
            )
        return int(version or 0)

    async def close(self) -> None:
        await self.engine.dispose()


#: Every value ``AuditRepository._decision_outcome`` can return.
#:
#: Kept beside it, and beside ``_outcome_filter`` which has to reproduce the
#: same classification in SQL: the three drift apart silently otherwise, and the
#: symptom would be a filter that quietly returns the wrong decisions.
DECISION_OUTCOMES = (
    "hold",
    "analysis_only",
    "rejected",
    "approved",
    "executed",
    "execution_failed",
)


class AuditRepository:
    # History tables safe to clear. Excludes runtime_state (paper account,
    # emergency lock) and schema_migrations so deletion never weakens recovery
    # or safety state.
    HISTORY_TABLES: dict[str, type[Base]] = {
        "inferences": InferenceRow,
        "risk_decisions": RiskRow,
        "executions": ExecutionRow,
        "user_events": UserStreamEventRow,
        "alerts": AlertEventRow,
    }

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def clear_history(self, categories: set[str]) -> dict[str, int]:
        counts: dict[str, int] = {}
        async with self.sessions.begin() as session:
            for category in categories:
                if category == "executions":
                    attempts = await session.execute(delete(ExecutionAttemptRow))
                    executions = await session.execute(delete(ExecutionRow))
                    counts[category] = int(attempts.rowcount or 0) + int(
                        executions.rowcount or 0
                    )
                    continue
                model = self.HISTORY_TABLES.get(category)
                if model is None:
                    continue
                result = await session.execute(delete(model))
                counts[category] = int(result.rowcount or 0)
        return counts

    async def record_inference(self, result: ProviderResult) -> int:
        usage = dict(result.usage)
        usage["_provenance"] = {
            "prompt_version": result.prompt_version,
            "data_version": result.data_version,
            "provider_version": result.provider_version,
            "reasoning_effort": result.reasoning_effort,
        }
        row = InferenceRow(
            provider=result.provider,
            model=result.model,
            symbol=result.intent.symbol,
            cadence=result.intent.cadence,
            intent_json=result.intent.model_dump_json(),
            raw_output=result.raw_output,
            usage_json=json.dumps(usage, separators=(",", ":")),
            duration_ms=result.duration.total_seconds() * 1000,
        )
        async with self.sessions.begin() as session:
            session.add(row)
            await session.flush()
            if result.input_payload is not None or result.prompt is not None:
                session.add(
                    InferenceDetailRow(
                        inference_id=row.id,
                        input_json=json.dumps(
                            result.input_payload, separators=(",", ":"), ensure_ascii=False
                        )
                        if result.input_payload is not None
                        else None,
                        prompt_text=result.prompt,
                    )
                )
        return row.id

    async def latest_inference_id(self) -> int:
        async with self.sessions() as session:
            latest = await session.scalar(
                select(InferenceRow.id).order_by(InferenceRow.id.desc()).limit(1)
            )
        return int(latest or 0)

    async def run_session_metrics(
        self,
        start_after_id: int,
        *,
        end_at_id: int | None = None,
        catalog: ModelPricingCatalog | None = None,
        provider_ids: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        query = select(InferenceRow).where(InferenceRow.id > start_after_id)
        if end_at_id is not None:
            query = query.where(InferenceRow.id <= end_at_id)
        query = query.order_by(InferenceRow.id.asc())
        async with self.sessions() as session:
            rows = (await session.scalars(query)).all()

        totals = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }
        error_count = 0
        duration_total_ms = 0.0
        cost_total = 0.0
        priced_call_count = 0
        for row in rows:
            duration_total_ms += row.duration_ms
            usage = json.loads(row.usage_json)
            input_tokens = int(usage.get("input_tokens") or 0)
            output_tokens = int(usage.get("output_tokens") or 0)
            cached_input_tokens = int(
                usage.get("cached_input_tokens") or usage.get("cache_read_input_tokens") or 0
            )
            totals["input_tokens"] += input_tokens
            totals["cached_input_tokens"] += cached_input_tokens
            totals["cache_creation_input_tokens"] += int(
                usage.get("cache_creation_input_tokens") or 0
            )
            totals["output_tokens"] += output_tokens
            totals["total_tokens"] += int(usage.get("total_tokens") or input_tokens + output_tokens)
            if "error" in usage:
                error_count += 1
            cost = self._inference_cost(row, usage, catalog, provider_ids)
            if cost is not None:
                priced_call_count += 1
                cost_total += cost

        call_count = len(rows)
        cost_complete = priced_call_count == call_count
        equivalent_cost_usd = cost_total if cost_complete else None
        return {
            "call_count": call_count,
            "error_count": error_count,
            **totals,
            "priced_call_count": priced_call_count,
            "cost_complete": cost_complete,
            "equivalent_cost_usd": equivalent_cost_usd,
            "average_duration_ms": duration_total_ms / call_count if call_count else 0.0,
            "average_tokens": totals["total_tokens"] / call_count if call_count else 0.0,
            "average_cost_usd": (
                equivalent_cost_usd / call_count
                if equivalent_cost_usd is not None and call_count
                else 0.0
                if not call_count
                else None
            ),
        }

    async def record_risk(
        self, symbol: str, decision: RiskDecision, *, inference_id: int | None = None
    ) -> int:
        row = RiskRow(
            inference_id=inference_id,
            symbol=symbol,
            accepted=int(decision.accepted),
            reason=decision.reason,
            decision_json=decision.model_dump_json(),
        )
        async with self.sessions.begin() as session:
            session.add(row)
        return row.id

    async def record_execution(self, symbol: str, report: ExecutionReport) -> int:
        row = ExecutionRow(
            client_order_id=report.client_order_id,
            symbol=symbol,
            status=report.status,
            report_json=report.model_dump_json(),
        )
        async with self.sessions.begin() as session:
            session.add(row)
        return row.id

    async def record_execution_attempt(
        self, symbol: str, attempt: ExecutionAttempt
    ) -> int:
        row = ExecutionAttemptRow(
            inference_id=attempt.inference_id,
            symbol=symbol,
            client_order_id=attempt.client_order_id,
            status=attempt.status,
            stage=attempt.stage,
            attempt_json=attempt.model_dump_json(),
        )
        async with self.sessions.begin() as session:
            session.add(row)
        return row.id

    async def recent_executions(
        self, limit: int = 100, *, status: str | None = None
    ) -> list[dict[str, Any]]:
        query = select(ExecutionRow)
        if status is not None:
            query = query.where(ExecutionRow.status == status)
        query = query.order_by(ExecutionRow.id.desc()).limit(limit)
        async with self.sessions() as session:
            rows = (await session.scalars(query)).all()
        return [
            {
                "id": row.id,
                "client_order_id": row.client_order_id,
                "symbol": row.symbol,
                "status": row.status,
                "report": json.loads(row.report_json),
                "created_at": self._utc(row.created_at),
            }
            for row in rows
        ]

    async def recent_risk_decisions(
        self, limit: int = 100, *, accepted: bool | None = None
    ) -> list[dict[str, Any]]:
        query = select(RiskRow)
        if accepted is not None:
            query = query.where(RiskRow.accepted == int(accepted))
        query = query.order_by(RiskRow.id.desc()).limit(limit)
        async with self.sessions() as session:
            rows = (await session.scalars(query)).all()
        return [
            {
                "id": row.id,
                "inference_id": row.inference_id,
                "symbol": row.symbol,
                "accepted": bool(row.accepted),
                "reason": row.reason,
                "decision": json.loads(row.decision_json),
                "created_at": self._utc(row.created_at),
            }
            for row in rows
        ]

    async def executions_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(ExecutionRow)
                    .where(
                        ExecutionRow.created_at >= start,
                        ExecutionRow.created_at < end,
                    )
                    .order_by(ExecutionRow.created_at.asc(), ExecutionRow.id.asc())
                )
            ).all()
        return [
            {
                "id": row.id,
                "client_order_id": row.client_order_id,
                "symbol": row.symbol,
                "status": row.status,
                "report": json.loads(row.report_json),
                "created_at": self._utc(row.created_at),
            }
            for row in rows
        ]

    async def risk_decisions_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(RiskRow)
                    .where(RiskRow.created_at >= start, RiskRow.created_at < end)
                    .order_by(RiskRow.created_at.asc(), RiskRow.id.asc())
                )
            ).all()
        return [
            {
                "id": row.id,
                "inference_id": row.inference_id,
                "symbol": row.symbol,
                "accepted": bool(row.accepted),
                "reason": row.reason,
                "created_at": self._utc(row.created_at),
            }
            for row in rows
        ]

    async def record_alert_event(self, event: dict[str, Any]) -> int:
        row = AlertEventRow(
            alert_id=str(event["id"]),
            transition=str(event["transition"]),
            severity=str(event.get("severity", "")),
            source=str(event.get("source", "")),
            title=str(event.get("title", "")),
            detail=str(event.get("detail", "")),
        )
        async with self.sessions.begin() as session:
            session.add(row)
        return row.id

    async def recent_alert_events(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(AlertEventRow).order_by(AlertEventRow.id.desc()).limit(limit)
                )
            ).all()
        return [
            {
                "id": row.id,
                "alert_id": row.alert_id,
                "transition": row.transition,
                "severity": row.severity,
                "source": row.source,
                "title": row.title,
                "detail": row.detail,
                "created_at": self._utc(row.created_at),
            }
            for row in rows
        ]

    async def inference_ids_between(self, start: datetime, end: datetime) -> set[int]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(InferenceRow.id).where(
                        InferenceRow.created_at >= start,
                        InferenceRow.created_at < end,
                    )
                )
            ).all()
        return set(rows)

    async def record_user_event(self, event: UserStreamEvent) -> int:
        row = UserStreamEventRow(
            event_type=event.event_type,
            symbol=event.symbol,
            event_time=event.event_time,
            transaction_time=event.transaction_time,
            payload_json=json.dumps(event.payload, separators=(",", ":")),
        )
        async with self.sessions.begin() as session:
            session.add(row)
        return row.id

    async def recent_user_events(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(UserStreamEventRow).order_by(UserStreamEventRow.id.desc()).limit(limit)
                )
            ).all()
        return [
            {
                "id": row.id,
                "event_type": row.event_type,
                "symbol": row.symbol,
                "event_time": self._utc(row.event_time),
                "transaction_time": self._utc(row.transaction_time)
                if row.transaction_time is not None
                else None,
                "payload": json.loads(row.payload_json),
            }
            for row in rows
        ]

    @staticmethod
    def _utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    async def set_runtime_state(self, key: str, value: str) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(RuntimeStateRow, key)
            if row is None:
                session.add(RuntimeStateRow(key=key, value=value))
            else:
                row.value = value

    async def get_runtime_state(self, key: str) -> str | None:
        async with self.sessions() as session:
            row = await session.get(RuntimeStateRow, key)
            return row.value if row is not None else None

    async def delete_runtime_state(self, key: str) -> None:
        async with self.sessions.begin() as session:
            row = await session.get(RuntimeStateRow, key)
            if row is not None:
                await session.delete(row)

    async def recent_intents(self, limit: int = 100) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(InferenceRow).order_by(InferenceRow.id.desc()).limit(limit)
                )
            ).all()
        results = []
        for row in rows:
            usage = json.loads(row.usage_json)
            results.append(
                {
                    "id": row.id,
                    "provider": row.provider,
                    "model": row.model,
                    "provenance": usage.get("_provenance", {}),
                    "intent": TradeIntent.model_validate_json(row.intent_json).model_dump(
                        mode="json"
                    ),
                    "duration_ms": row.duration_ms,
                    "created_at": row.created_at.replace(tzinfo=UTC)
                    if row.created_at.tzinfo is None
                    else row.created_at,
                }
            )
        return results

    @staticmethod
    def _outcome_filter(outcome: str) -> Any:
        """Rebuild ``_decision_outcome`` as SQL.

        The action lives in ``intent_json`` rather than a column, so HOLD is
        matched with json_extract. That is unindexed, but the id index still
        drives the ordering and the walk stops at ``limit``; the alternative --
        a denormalised action column -- would put the same fact in two places
        for a table this size to gain nothing.

        This must agree with ``_decision_outcome``; the two are pinned together
        by a test that filters on every outcome it can produce.
        """

        action = func.json_extract(InferenceRow.intent_json, "$.action")
        is_hold = action == "HOLD"
        succeeded = ExecutionAttemptRow.status == "SUCCEEDED"
        return {
            "hold": is_hold,
            "analysis_only": and_(not_(is_hold), RiskRow.id.is_(None)),
            "rejected": and_(not_(is_hold), RiskRow.id.is_not(None), ~RiskRow.accepted),
            "approved": and_(
                not_(is_hold),
                RiskRow.accepted,
                ExecutionAttemptRow.id.is_(None),
            ),
            "executed": and_(
                not_(is_hold), RiskRow.accepted, ExecutionAttemptRow.id.is_not(None), succeeded
            ),
            "execution_failed": and_(
                not_(is_hold),
                RiskRow.accepted,
                ExecutionAttemptRow.id.is_not(None),
                not_(succeeded),
            ),
        }[outcome]

    async def recent_decision_events(
        self,
        limit: int = 100,
        *,
        before_id: int | None = None,
        symbol: str | None = None,
        cadence: str | None = None,
        provider: str | None = None,
        outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        query = (
            select(InferenceRow, RiskRow, ExecutionAttemptRow)
            .outerjoin(RiskRow, RiskRow.inference_id == InferenceRow.id)
            .outerjoin(
                ExecutionAttemptRow,
                ExecutionAttemptRow.inference_id == InferenceRow.id,
            )
        )
        # Keyset paging on the primary key: ids are monotonic with no ties, so a
        # page cannot skip or repeat a row when new decisions land mid-read, the
        # way an OFFSET would.
        if before_id is not None:
            query = query.where(InferenceRow.id < before_id)
        if symbol is not None:
            query = query.where(InferenceRow.symbol == symbol)
        if cadence is not None:
            query = query.where(InferenceRow.cadence == cadence)
        if provider is not None:
            query = query.where(InferenceRow.provider == provider)
        if outcome is not None:
            query = query.where(self._outcome_filter(outcome))
        async with self.sessions() as session:
            rows = (
                await session.execute(query.order_by(InferenceRow.id.desc()).limit(limit))
            ).all()
        events = []
        for inference, risk, attempt in rows:
            usage = json.loads(inference.usage_json)
            intent = TradeIntent.model_validate_json(inference.intent_json)
            outcome = self._decision_outcome(intent, risk, attempt)
            events.append(
                {
                    "id": inference.id,
                    "provider": inference.provider,
                    "model": inference.model,
                    "provenance": usage.get("_provenance", {}),
                    "failover": {
                        "route_position": usage.get("route_position"),
                        "continues": bool(usage.get("failover_continues")),
                        "error": usage.get("error_message"),
                    }
                    if usage.get("failover_attempt")
                    else None,
                    "intent": intent.model_dump(mode="json"),
                    "duration_ms": inference.duration_ms,
                    "outcome": outcome,
                    "risk": {
                        "id": risk.id,
                        "accepted": bool(risk.accepted),
                        "reason": risk.reason,
                        "decision": json.loads(risk.decision_json),
                        "created_at": self._utc(risk.created_at),
                    }
                    if risk is not None
                    else None,
                    "execution": self._execution_attempt_dict(attempt),
                    "created_at": self._utc(inference.created_at),
                }
            )
        return events

    @staticmethod
    def _decision_outcome(
        intent: TradeIntent,
        risk: RiskRow | None,
        attempt: ExecutionAttemptRow | None,
    ) -> str:
        if intent.action.value == "HOLD":
            return "hold"
        if risk is None:
            return "analysis_only"
        if not risk.accepted:
            return "rejected"
        if attempt is None:
            return "approved"
        return "executed" if attempt.status == "SUCCEEDED" else "execution_failed"

    def _execution_attempt_dict(
        self, attempt: ExecutionAttemptRow | None
    ) -> dict[str, Any] | None:
        if attempt is None:
            return None
        payload = json.loads(attempt.attempt_json)
        return {
            "id": attempt.id,
            **payload,
            "created_at": self._utc(attempt.created_at),
        }

    @staticmethod
    def _inference_cost(
        inference: InferenceRow,
        usage: dict[str, Any],
        catalog: ModelPricingCatalog | None,
        provider_ids: Mapping[str, str] | None = None,
    ) -> float | None:
        cost = usage.get("cost_usd")
        provider_id = (provider_ids or PROVIDER_IDS).get(inference.provider)
        if cost is None and catalog is not None and provider_id is not None:
            cost = catalog.cost_usd(
                provider_id,
                inference.model,
                input_tokens=int(usage.get("input_tokens") or 0),
                cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
            )
        return float(cost) if cost is not None else None

    async def decision_detail(
        self,
        inference_id: int,
        *,
        catalog: ModelPricingCatalog | None = None,
        provider_ids: Mapping[str, str] | None = None,
    ) -> dict[str, Any] | None:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(
                        InferenceRow,
                        RiskRow,
                        InferenceDetailRow,
                        ExecutionAttemptRow,
                    )
                    .outerjoin(RiskRow, RiskRow.inference_id == InferenceRow.id)
                    .outerjoin(
                        InferenceDetailRow,
                        InferenceDetailRow.inference_id == InferenceRow.id,
                    )
                    .outerjoin(
                        ExecutionAttemptRow,
                        ExecutionAttemptRow.inference_id == InferenceRow.id,
                    )
                    .where(InferenceRow.id == inference_id)
                )
            ).one_or_none()
        if row is None:
            return None
        inference, risk, detail, attempt = row
        usage = json.loads(inference.usage_json)
        intent = TradeIntent.model_validate_json(inference.intent_json)
        audit_status = "unavailable"
        if detail is not None:
            audit_status = (
                "complete"
                if detail.input_json is not None and detail.prompt_text is not None
                else "partial"
            )
        return {
            "id": inference.id,
            "provider": inference.provider,
            "model": inference.model,
            "provenance": usage.get("_provenance", {}),
            "intent": intent.model_dump(mode="json"),
            "duration_ms": inference.duration_ms,
            "outcome": self._decision_outcome(intent, risk, attempt),
            "risk": {
                "id": risk.id,
                "accepted": bool(risk.accepted),
                "reason": risk.reason,
                "decision": json.loads(risk.decision_json),
                "created_at": self._utc(risk.created_at),
            }
            if risk is not None
            else None,
            "execution": self._execution_attempt_dict(attempt),
            "input": json.loads(detail.input_json)
            if detail is not None and detail.input_json is not None
            else None,
            "prompt": detail.prompt_text if detail is not None else None,
            "audit_status": audit_status,
            "raw_output": inference.raw_output,
            "usage": {key: value for key, value in usage.items() if key != "_provenance"},
            "equivalent_cost_usd": self._inference_cost(
                inference, usage, catalog, provider_ids
            ),
            "created_at": self._utc(inference.created_at),
        }

    async def provider_metrics(
        self,
        hours: int = 24,
        *,
        catalog: ModelPricingCatalog | None = None,
        provider_ids: Mapping[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(InferenceRow)
                    .where(InferenceRow.created_at >= cutoff)
                    .order_by(InferenceRow.created_at.asc(), InferenceRow.id.asc())
                )
            ).all()

        grouped: dict[str, list[InferenceRow]] = {}
        for row in rows:
            grouped.setdefault(row.provider, []).append(row)

        metrics = []
        for provider, provider_rows in grouped.items():
            durations = sorted(row.duration_ms for row in provider_rows)
            error_count = 0
            tokens_total = 0
            cost_total = 0.0
            cost_present = False
            provider_id = (provider_ids or PROVIDER_IDS).get(provider)
            for row in provider_rows:
                usage = json.loads(row.usage_json)
                if "error" in usage:
                    error_count += 1
                tokens_total += int(usage.get("total_tokens") or 0)
                cost = usage.get("cost_usd")
                if cost is None and catalog is not None and provider_id is not None:
                    cost = catalog.cost_usd(
                        provider_id,
                        row.model,
                        input_tokens=int(usage.get("input_tokens") or 0),
                        cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
                        output_tokens=int(usage.get("output_tokens") or 0),
                    )
                if cost is not None:
                    cost_present = True
                    cost_total += float(cost)
            model_counts = Counter(row.model or "unknown" for row in provider_rows)
            call_count = len(provider_rows)
            p95_index = max(0, math.ceil(call_count * 0.95) - 1)
            metrics.append(
                {
                    "provider": provider,
                    "call_count": call_count,
                    "error_count": error_count,
                    "error_rate": error_count / call_count,
                    "average_duration_ms": sum(durations) / call_count,
                    "p95_duration_ms": durations[p95_index],
                    "models": dict(sorted(model_counts.items())),
                    "tokens_total": tokens_total,
                    "cost_usd_total": cost_total if cost_present else None,
                    "last_call_at": self._utc(provider_rows[-1].created_at),
                }
            )
        return sorted(metrics, key=lambda item: item["provider"])

    async def intents_between(
        self,
        symbol: str,
        cadence: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(InferenceRow)
                    .where(
                        InferenceRow.symbol == symbol,
                        InferenceRow.cadence == cadence,
                        InferenceRow.created_at >= start,
                        InferenceRow.created_at < end,
                    )
                    .order_by(InferenceRow.created_at.asc(), InferenceRow.id.asc())
                )
            ).all()
        results = []
        for row in rows:
            usage = json.loads(row.usage_json)
            results.append(
                {
                    "id": row.id,
                    "provider": row.provider,
                    "model": row.model,
                    "provenance": usage.get("_provenance", {}),
                    "intent": TradeIntent.model_validate_json(row.intent_json),
                    "created_at": row.created_at.replace(tzinfo=UTC)
                    if row.created_at.tzinfo is None
                    else row.created_at,
                }
            )
        return results

