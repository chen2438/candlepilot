from __future__ import annotations

import json
from datetime import UTC, datetime
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
        return [self._backtest_dict(row) for row in rows]

    @staticmethod
    def _backtest_dict(row: BacktestRow) -> dict[str, Any]:
        created_at = (
            row.created_at.replace(tzinfo=UTC)
            if row.created_at.tzinfo is None
            else row.created_at
        )
        return {
            "id": row.id,
            "symbol": row.symbol,
            "cadence": row.cadence,
            "result": json.loads(row.result_json),
            "created_at": created_at,
        }
