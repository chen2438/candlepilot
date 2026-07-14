from __future__ import annotations

import json
import math
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import DateTime, Float, Integer, String, Text, event, insert, select, text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from candlepilot.broker.user_stream import UserStreamEvent
from candlepilot.domain.models import ExecutionReport, RiskDecision, TradeIntent
from candlepilot.providers.base import ProviderResult


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


class BacktestRow(Base):
    __tablename__ = "backtests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    cadence: Mapped[str] = mapped_column(String(8), index=True)
    result_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class RuntimeStateRow(Base):
    __tablename__ = "runtime_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), onupdate=lambda: datetime.now(UTC)
    )


class UserStreamEventRow(Base):
    __tablename__ = "user_stream_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    event_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    transaction_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
            select(SchemaMigrationRow.version)
            .order_by(SchemaMigrationRow.version.desc())
            .limit(1)
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


class AuditRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def record_inference(self, result: ProviderResult) -> int:
        usage = dict(result.usage)
        usage["_provenance"] = {
            "prompt_version": result.prompt_version,
            "data_version": result.data_version,
            "provider_version": result.provider_version,
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
        return row.id

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

    async def executions_between(
        self, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
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

    async def risk_decisions_between(
        self, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
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
                    select(UserStreamEventRow)
                    .order_by(UserStreamEventRow.id.desc())
                    .limit(limit)
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

    async def save_paper_state(self, state: dict[str, Any]) -> None:
        await self.set_runtime_state(
            "paper_account", json.dumps(state, separators=(",", ":"))
        )

    async def load_paper_state(self) -> dict[str, Any] | None:
        value = await self.get_runtime_state("paper_account")
        return json.loads(value) if value is not None else None

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
            results.append({
                "id": row.id,
                "provider": row.provider,
                "model": row.model,
                "provenance": usage.get("_provenance", {}),
                "intent": TradeIntent.model_validate_json(row.intent_json).model_dump(mode="json"),
                "duration_ms": row.duration_ms,
                "created_at": row.created_at.replace(tzinfo=UTC)
                if row.created_at.tzinfo is None
                else row.created_at,
            })
        return results

    async def provider_metrics(self, hours: int = 24) -> list[dict[str, Any]]:
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
            error_count = sum(
                1 for row in provider_rows if "error" in json.loads(row.usage_json)
            )
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
            results.append({
                "id": row.id,
                "provider": row.provider,
                "model": row.model,
                "provenance": usage.get("_provenance", {}),
                "intent": TradeIntent.model_validate_json(row.intent_json),
                "created_at": row.created_at.replace(tzinfo=UTC)
                if row.created_at.tzinfo is None
                else row.created_at,
            })
        return results

    async def record_backtest(
        self, symbol: str, cadence: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        row = BacktestRow(
            symbol=symbol,
            cadence=cadence,
            result_json=json.dumps(result, separators=(",", ":")),
        )
        async with self.sessions.begin() as session:
            session.add(row)
        return self._backtest_dict(row)

    async def recent_backtests(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(BacktestRow).order_by(BacktestRow.id.desc()).limit(limit)
                )
            ).all()
        return [self._backtest_dict(row, detail=False) for row in rows]

    async def backtest(self, backtest_id: int) -> dict[str, Any] | None:
        async with self.sessions() as session:
            row = await session.get(BacktestRow, backtest_id)
        return self._backtest_dict(row, detail=True) if row is not None else None

    @staticmethod
    def _backtest_dict(row: BacktestRow, *, detail: bool = True) -> dict[str, Any]:
        created_at = (
            row.created_at.replace(tzinfo=UTC)
            if row.created_at.tzinfo is None
            else row.created_at
        )
        result = json.loads(row.result_json)
        if not detail:
            result = dict(result)
            result["trade_count"] = len(result.pop("trades", []))
            result.pop("equity_curve", None)
            per_symbol = result.get("per_symbol")
            if isinstance(per_symbol, dict):
                for sleeve in per_symbol.values():
                    if isinstance(sleeve, dict):
                        sleeve["trade_count"] = len(sleeve.pop("trades", []))
                        sleeve.pop("equity_curve", None)
        return {
            "id": row.id,
            "symbol": row.symbol,
            "cadence": row.cadence,
            "result": result,
            "created_at": created_at,
        }
