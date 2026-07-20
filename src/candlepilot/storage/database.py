from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
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
    update,
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


class LiveRunRow(Base):
    __tablename__ = "live_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    status: Mapped[str] = mapped_column(String(16), default="running", index=True)
    config_json: Mapped[str] = mapped_column(Text, default="{}")
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class InferenceRow(Base):
    __tablename__ = "inferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    live_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("live_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
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
    inference_id: Mapped[int | None] = mapped_column(
        ForeignKey("inferences.id", ondelete="SET NULL"), nullable=True, index=True
    )
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


class BacktestRunRow(Base):
    __tablename__ = "backtest_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    spec_json: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class BacktestModelRunRow(Base):
    """One model's pass over a run's window.

    Split from the run so progress can be written per model while the others are
    still going: a comparison is only useful if you can watch it fill in.
    """

    __tablename__ = "backtest_model_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64))
    decisions_done: Mapped[int] = mapped_column(Integer, default=0)
    decisions_total: Mapped[int] = mapped_column(Integer, default=0)
    calls_failed: Mapped[int] = mapped_column(Integer, default=0)
    usage_json: Mapped[str] = mapped_column(Text, default="{}")
    progress_json: Mapped[str] = mapped_column(Text, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class BacktestDecisionRow(Base):
    """One decision a model made inside a backtest.

    The totals alone cannot tell a model that held all day from one the risk
    policy vetoed every time from one whose calls timed out: all three report
    zero trades. `outcome` is what separates them.
    """

    __tablename__ = "backtest_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("backtest_runs.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(64))
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    symbol: Mapped[str] = mapped_column(String(32))
    cadence: Mapped[str] = mapped_column(String(8))
    #: traded | pending | rejected | hold | no_snapshot | call_failed
    outcome: Mapped[str] = mapped_column(String(16))
    action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: The veto reason, the call's error -- whatever explains the outcome.
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    fill_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts_json: Mapped[str] = mapped_column(Text, default="[]")


class BookCaptureRow(Base):
    """One recorded order-book state.

    The only source of order flow for a past window: Binance keeps no history
    of the book, so anything not written down here is gone.
    """

    __tablename__ = "book_captures"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    schema_version: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text)


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


# Historical data and all pre-v12 databases have been retired.  Keep one empty
# baseline migration so the user's current v12 database advances without
# replaying upgrade logic, while fresh databases are created directly from the
# ORM metadata at the current shape.
MINIMUM_SUPPORTED_SCHEMA_VERSION = 12
MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = ((13, ()),)
CURRENT_SCHEMA_VERSION = 13


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
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
        if current is not None and current < MINIMUM_SUPPORTED_SCHEMA_VERSION:
            raise RuntimeError(
                "database schemas before v12 are no longer supported; clear the old "
                "database and start CandlePilot with a fresh schema"
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
    # History tables safe to clear. Excludes runtime_state (the emergency lock)
    # and schema_migrations so deletion never weakens recovery or safety state.
    HISTORY_TABLES: dict[str, type[Base]] = {
        "inferences": InferenceRow,
        "risk_decisions": RiskRow,
        "executions": ExecutionRow,
        "user_events": UserStreamEventRow,
        "alerts": AlertEventRow,
        # Model runs cascade from the run, so clearing the parent is enough.
        "backtests": BacktestRunRow,
        "book_captures": BookCaptureRow,
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
                if category == "inferences":
                    # Run headers are part of formal-decision history. Keeping
                    # empty headers after their decisions are cleared is both
                    # misleading and impossible to inspect from the UI.
                    await session.execute(delete(LiveRunRow))
        return counts

    async def create_live_run(self, config: Mapping[str, Any]) -> int:
        row = LiveRunRow(
            status="running",
            config_json=json.dumps(dict(config), separators=(",", ":")),
        )
        async with self.sessions.begin() as session:
            session.add(row)
            await session.flush()
        return row.id

    async def finish_live_run(
        self,
        run_id: int,
        *,
        status: str,
        stop_reason: str | None,
        ended_at: datetime | None = None,
    ) -> None:
        self._validate_live_run_status(status)
        async with self.sessions.begin() as session:
            await session.execute(
                update(LiveRunRow)
                .where(LiveRunRow.id == run_id, LiveRunRow.status == "running")
                .values(
                    status=status,
                    stop_reason=stop_reason,
                    ended_at=ended_at or datetime.now(UTC),
                )
            )

    async def recent_live_run_performance(
        self,
        limit: int = 100,
        *,
        current_positions: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            runs = (
                await session.scalars(
                    select(LiveRunRow).order_by(LiveRunRow.id.desc()).limit(limit)
                )
            ).all()
            run_ids = [row.id for row in runs]
            attempt_rows = (
                await session.execute(
                    select(
                        ExecutionAttemptRow.client_order_id,
                        InferenceRow.live_run_id,
                        InferenceRow.symbol,
                        InferenceRow.intent_json,
                    )
                    .join(InferenceRow, InferenceRow.id == ExecutionAttemptRow.inference_id)
                    .where(
                        InferenceRow.live_run_id.in_(run_ids),
                        ExecutionAttemptRow.client_order_id.is_not(None),
                    )
                )
            ).all() if run_ids else []

        order_contexts: dict[str, tuple[int, str, str | None]] = {}
        for client_order_id, run_id, symbol, intent_json in attempt_rows:
            if client_order_id is None or run_id is None:
                continue
            action = TradeIntent.model_validate_json(intent_json).action.value
            side = "BUY" if action == "OPEN_LONG" else "SELL" if action == "OPEN_SHORT" else None
            order_contexts[client_order_id] = (run_id, symbol, side)

        realized: defaultdict[int, Decimal] = defaultdict(Decimal)
        unrealized: defaultdict[int, Decimal] = defaultdict(Decimal)
        open_symbols: defaultdict[int, set[str]] = defaultdict(set)
        closed: Counter[int] = Counter()
        wins: Counter[int] = Counter()
        lots_by_symbol: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        lots_by_order: dict[str, dict[str, Any]] = {}
        fills = list(reversed(await self.recent_trade_fills(10_000)))
        for fill in fills:
            client_order_id = str(fill["client_order_id"])
            symbol = str(fill["symbol"])
            quantity = Decimal(str(fill["report"].get("filled_quantity", "0")))
            if quantity <= 0:
                continue
            if not fill["reduce_only"]:
                context = order_contexts.get(client_order_id)
                average_price = fill["report"].get("average_price")
                if context is None or average_price is None:
                    continue
                run_id, _, intent_side = context
                lot = {
                    "client_order_id": client_order_id,
                    "run_id": run_id,
                    "symbol": symbol,
                    "side": fill.get("side") or intent_side,
                    "entry_price": Decimal(str(average_price)),
                    "remaining": quantity,
                }
                lots_by_symbol[symbol].append(lot)
                lots_by_order[client_order_id] = lot
                continue

            if fill["realized_pnl"] is None:
                continue
            remaining_exit = quantity
            allocations: defaultdict[int, Decimal] = defaultdict(Decimal)
            related_id = fill.get("related_client_order_id")
            preferred = lots_by_order.get(str(related_id)) if related_id else None
            candidates = ([preferred] if preferred is not None else []) + [
                lot
                for lot in reversed(lots_by_symbol[symbol])
                if lot is not preferred and lot["remaining"] > 0
            ]
            for lot in candidates:
                if remaining_exit <= 0:
                    break
                consumed = min(remaining_exit, lot["remaining"])
                if consumed <= 0:
                    continue
                lot["remaining"] -= consumed
                remaining_exit -= consumed
                allocations[lot["run_id"]] += consumed
            if not allocations:
                context = order_contexts.get(client_order_id)
                if context is not None:
                    allocations[context[0]] = quantity
            allocated_quantity = sum(allocations.values(), Decimal("0"))
            if allocated_quantity <= 0:
                continue
            fill_pnl = Decimal(str(fill["realized_pnl"]))
            for run_id, allocated in allocations.items():
                allocated_pnl = fill_pnl * allocated / allocated_quantity
                realized[run_id] += allocated_pnl
                closed[run_id] += 1
                if allocated_pnl > 0:
                    wins[run_id] += 1

        for symbol, position in (current_positions or {}).items():
            active_lots = [lot for lot in lots_by_symbol[symbol] if lot["remaining"] > 0]
            if not active_lots:
                continue
            owners = {lot["run_id"] for lot in active_lots}
            for run_id in owners:
                open_symbols[run_id].add(symbol)
            if len(owners) == 1:
                unrealized[next(iter(owners))] += Decimal(
                    str(position.get("unrealized_pnl", "0"))
                )
                continue
            mark_price = Decimal(str(position.get("mark_price", "0")))
            for lot in active_lots:
                direction = Decimal("1") if lot["side"] == "BUY" else Decimal("-1")
                unrealized[lot["run_id"]] += (
                    (mark_price - lot["entry_price"]) * lot["remaining"] * direction
                )

        result = []
        for run in runs:
            realized_pnl = realized[run.id]
            unrealized_pnl = unrealized[run.id]
            total_pnl = realized_pnl + unrealized_pnl
            closed_trades = closed[run.id]
            result.append(
                {
                    "live_run_id": run.id,
                    "realized_pnl": str(realized_pnl),
                    "unrealized_pnl": str(unrealized_pnl),
                    "total_pnl": str(total_pnl),
                    "wins": wins[run.id],
                    "closed_trades": closed_trades,
                    "open_position_count": len(open_symbols[run.id]),
                    "win_rate": str(Decimal(wins[run.id]) / Decimal(closed_trades))
                    if closed_trades
                    else None,
                    "includes_unrealized": True,
                    "valued_at": self._utc(datetime.now(UTC)),
                }
            )
        return result

    async def interrupt_open_live_runs(self, *, ended_at: datetime | None = None) -> int:
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(LiveRunRow)
                .where(LiveRunRow.status == "running")
                .values(
                    status="interrupted",
                    stop_reason="process restarted before the run closed cleanly",
                    ended_at=ended_at or datetime.now(UTC),
                )
            )
        return int(result.rowcount or 0)

    async def fail_open_backtest_runs(self, *, ended_at: datetime | None = None) -> int:
        """Close backtests whose owning process disappeared before recording a result."""

        async with self.sessions.begin() as session:
            result = await session.execute(
                update(BacktestRunRow)
                .where(BacktestRunRow.status == "running")
                .values(
                    status="failed",
                    error="process restarted before the backtest closed cleanly",
                    ended_at=ended_at or datetime.now(UTC),
                )
            )
        return int(result.rowcount or 0)

    @staticmethod
    def _validate_live_run_status(status: str) -> None:
        if status not in {"stopped", "auto_stopped", "emergency_stopped", "interrupted"}:
            raise ValueError(f"unsupported live run status: {status}")

    async def record_inference(
        self, result: ProviderResult, *, live_run_id: int | None = None
    ) -> int:
        usage = dict(result.usage)
        usage["_provenance"] = {
            "prompt_version": result.prompt_version,
            "data_version": result.data_version,
            "provider_version": result.provider_version,
            "reasoning_effort": result.reasoning_effort,
        }
        row = InferenceRow(
            live_run_id=live_run_id,
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

    async def update_risk(
        self,
        inference_id: int,
        decision: RiskDecision,
        *,
        completed: bool,
    ) -> None:
        values: dict[str, Any] = {
            "accepted": int(decision.accepted),
            "reason": decision.reason,
            "decision_json": decision.model_dump_json(),
        }
        if completed:
            values["created_at"] = datetime.now(UTC)
        async with self.sessions.begin() as session:
            result = await session.execute(
                update(RiskRow)
                .where(RiskRow.inference_id == inference_id)
                .values(**values)
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"expected one risk decision for inference {inference_id}, "
                    f"updated {result.rowcount or 0}"
                )

    async def pending_local_entries(
        self, *, live_run_id: int | None = None
    ) -> list[dict[str, Any]]:
        conditions = [
            RiskRow.accepted == 1,
            func.json_extract(RiskRow.decision_json, "$.pending_entry") == 1,
        ]
        if live_run_id is not None:
            conditions.append(InferenceRow.live_run_id == live_run_id)
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(InferenceRow, RiskRow)
                    .join(RiskRow, RiskRow.inference_id == InferenceRow.id)
                    .where(*conditions)
                    .order_by(InferenceRow.id.asc())
                )
            ).all()
        return [
            {
                "inference_id": inference.id,
                "live_run_id": inference.live_run_id,
                "provider": inference.provider,
                "intent": TradeIntent.model_validate_json(inference.intent_json),
                "decision": RiskDecision.model_validate_json(risk.decision_json),
                "created_at": self._utc(inference.created_at),
            }
            for inference, risk in rows
        ]

    async def record_execution(self, symbol: str, report: ExecutionReport) -> int:
        row = ExecutionRow(
            client_order_id=report.client_order_id,
            symbol=symbol,
            status=report.status,
            report_json=report.model_dump_json(),
        )
        async with self.sessions.begin() as session:
            session.add(row)
            prior_events = (
                await session.scalars(
                    select(UserStreamEventRow)
                    .where(
                        UserStreamEventRow.event_type == "ORDER_TRADE_UPDATE",
                        func.json_extract(UserStreamEventRow.payload_json, "$.o.c")
                        == report.client_order_id,
                    )
                    .order_by(UserStreamEventRow.event_time.asc(), UserStreamEventRow.id.asc())
                )
            ).all()
            for prior_event in prior_events:
                self._apply_order_update(
                    row,
                    json.loads(prior_event.payload_json),
                    self._utc(prior_event.event_time),
                )
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

    async def recent_trade_fills(self, limit: int = 100) -> list[dict[str, Any]]:
        """Return one row per completed exchange trade, including bracket exits.

        Execution rows originate in CandlePilot's REST submission path, so they
        do not include exchange-triggered stop-loss or take-profit orders.  The
        persisted Binance user stream is the authoritative source for those
        fills.  Execution rows remain as a fallback for paper/test records and
        for the short interval before a matching stream event arrives.
        """

        event_query = (
            select(UserStreamEventRow)
            .where(
                UserStreamEventRow.event_type == "ORDER_TRADE_UPDATE",
                func.json_extract(UserStreamEventRow.payload_json, "$.o.X") == "FILLED",
                func.json_extract(UserStreamEventRow.payload_json, "$.o.x") == "TRADE",
            )
            .order_by(UserStreamEventRow.event_time.desc(), UserStreamEventRow.id.desc())
            # A historical database may contain both the live user-stream event
            # and its REST reconciliation copy. Fetch enough rows to discard
            # those semantic duplicates without shrinking the requested page.
            .limit(limit * 4)
        )
        execution_query = (
            select(ExecutionRow)
            .where(ExecutionRow.status == "FILLED")
            .order_by(ExecutionRow.created_at.desc(), ExecutionRow.id.desc())
            .limit(limit)
        )
        async with self.sessions() as session:
            event_rows = (await session.scalars(event_query)).all()
            execution_rows = (await session.scalars(execution_query)).all()
            unique_event_rows: dict[tuple[str, ...], UserStreamEventRow] = {}
            unkeyed_event_rows: list[UserStreamEventRow] = []
            for row in event_rows:
                payload = json.loads(row.payload_json)
                identity = self._terminal_fill_identity(payload)
                if identity is None:
                    unkeyed_event_rows.append(row)
                    continue
                existing = unique_event_rows.get(identity)
                if existing is None:
                    unique_event_rows[identity] = row
                    continue
                existing_payload = json.loads(existing.payload_json)
                if (
                    existing_payload.get("_source") == "rest_trade_reconciliation"
                    and payload.get("_source") != "rest_trade_reconciliation"
                ):
                    unique_event_rows[identity] = row
            event_rows = [*unique_event_rows.values(), *unkeyed_event_rows]
            event_client_ids = {
                str(json.loads(row.payload_json).get("o", {}).get("c", ""))
                for row in event_rows
            }
            client_ids = event_client_ids | {row.client_order_id for row in execution_rows}
            intent_actions: dict[str, str] = {}
            if client_ids:
                action_rows = (
                    await session.execute(
                        select(
                            ExecutionAttemptRow.client_order_id,
                            InferenceRow.intent_json,
                        )
                        .join(
                            InferenceRow,
                            InferenceRow.id == ExecutionAttemptRow.inference_id,
                        )
                        .where(ExecutionAttemptRow.client_order_id.in_(client_ids))
                        .order_by(ExecutionAttemptRow.id.desc())
                    )
                ).all()
                for client_order_id, intent_json in action_rows:
                    if client_order_id is not None and client_order_id not in intent_actions:
                        intent_actions[client_order_id] = (
                            TradeIntent.model_validate_json(intent_json).action.value
                        )
            fills: list[dict[str, Any]] = []
            for row in event_rows:
                payload = json.loads(row.payload_json)
                order = payload.get("o", {})
                client_order_id = str(order.get("c", ""))
                reduce_only = order.get("R") in {True, "true", "TRUE", 1, "1"}
                purpose, related_entry_id = self._trade_fill_identity(
                    client_order_id,
                    intent_action=intent_actions.get(client_order_id),
                    reduce_only=reduce_only,
                )
                if reduce_only and related_entry_id is None:
                    related_entry_id = await self._latest_entry_before(session, row)
                average_price = Decimal(str(order.get("ap", "0")))
                fills.append(
                    {
                        "id": row.id,
                        "source": (
                            "exchange_rest_reconciliation"
                            if payload.get("_source") == "rest_trade_reconciliation"
                            else "exchange_user_stream"
                        ),
                        "client_order_id": client_order_id,
                        "related_client_order_id": related_entry_id,
                        "symbol": row.symbol or str(order.get("s", "")),
                        "side": str(order.get("S", "")),
                        "purpose": purpose,
                        "reduce_only": reduce_only,
                        "realized_pnl": str(order.get("rp", "0")),
                        "status": "FILLED",
                        "report": {
                            "client_order_id": client_order_id,
                            "status": "FILLED",
                            "filled_quantity": str(order.get("z", "0")),
                            "average_price": str(average_price) if average_price > 0 else None,
                            "message": "Binance user stream trade fill",
                            "timestamp": self._utc(row.event_time),
                        },
                        "created_at": self._utc(row.event_time),
                    }
                )

        for row in execution_rows:
            if row.client_order_id in event_client_ids:
                continue
            report = json.loads(row.report_json)
            purpose, related_entry_id = self._trade_fill_identity(
                row.client_order_id,
                intent_action=intent_actions.get(row.client_order_id),
            )
            fills.append(
                {
                    "id": row.id,
                    "source": "execution_audit",
                    "client_order_id": row.client_order_id,
                    "related_client_order_id": related_entry_id,
                    "symbol": row.symbol,
                    "side": None,
                    "purpose": purpose,
                    "reduce_only": purpose != "entry",
                    "realized_pnl": None,
                    "status": row.status,
                    "report": report,
                    "created_at": self._utc(row.created_at),
                }
            )
        await self._enrich_trade_fill_financials(fills)
        fills.sort(key=lambda item: item["created_at"], reverse=True)
        return fills[:limit]

    @staticmethod
    def _terminal_fill_identity(payload: dict[str, Any]) -> tuple[str, ...] | None:
        """Identify one final exchange fill across live and REST ingestion paths."""

        order = payload.get("o", {})
        if order.get("x") != "TRADE" or order.get("X") != "FILLED":
            return None
        order_id = order.get("i")
        client_order_id = str(order.get("c", ""))
        if order_id is None and not client_order_id:
            return None
        try:
            cumulative_quantity = str(Decimal(str(order.get("z", "0"))).normalize())
        except ArithmeticError:  # pragma: no cover - malformed exchange payload fallback
            cumulative_quantity = str(order.get("z", ""))
        stable_order_id = (
            f"exchange:{order_id}" if order_id is not None else f"client:{client_order_id}"
        )
        return (
            str(order.get("s", "")),
            stable_order_id,
            "TRADE",
            "FILLED",
            cumulative_quantity,
        )

    async def _enrich_trade_fill_financials(self, fills: list[dict[str, Any]]) -> None:
        """Attach USDT notional and auditable realized return-on-margin values."""

        related_entry_ids: set[str] = set()
        for fill in fills:
            report = fill["report"]
            quantity = Decimal(str(report.get("filled_quantity", "0")))
            raw_average = report.get("average_price")
            average_price = Decimal(str(raw_average)) if raw_average is not None else None
            fill["notional_usdt"] = (
                str(abs(quantity * average_price))
                if quantity > 0 and average_price is not None and average_price > 0
                else None
            )
            fill["realized_pnl_margin_usdt"] = None
            fill["realized_return_percent"] = None
            related_entry_id = fill.get("related_client_order_id")
            if isinstance(related_entry_id, str) and related_entry_id:
                related_entry_ids.add(related_entry_id)

        if not related_entry_ids:
            return

        async with self.sessions() as session:
            entry_rows = (
                await session.scalars(
                    select(ExecutionRow).where(
                        ExecutionRow.client_order_id.in_(related_entry_ids)
                    )
                )
            ).all()
            context_rows = (
                await session.execute(
                    select(ExecutionAttemptRow, InferenceRow, InferenceDetailRow)
                    .join(
                        InferenceRow,
                        InferenceRow.id == ExecutionAttemptRow.inference_id,
                    )
                    .outerjoin(
                        InferenceDetailRow,
                        InferenceDetailRow.inference_id == InferenceRow.id,
                    )
                    .where(ExecutionAttemptRow.client_order_id.in_(related_entry_ids))
                    .order_by(ExecutionAttemptRow.id.desc())
                )
            ).all()

        entry_reports = {
            row.client_order_id: ExecutionReport.model_validate_json(row.report_json)
            for row in entry_rows
        }
        entry_contexts: dict[str, tuple[Decimal, int]] = {}
        for attempt, inference, detail in context_rows:
            client_order_id = attempt.client_order_id
            if client_order_id is None or client_order_id in entry_contexts:
                continue
            report = entry_reports.get(client_order_id)
            if report is None or report.average_price is None or report.filled_quantity <= 0:
                continue
            intent = TradeIntent.model_validate_json(inference.intent_json)
            entry_basis = report.average_price
            if intent.action.value == "ADD" and detail is not None and detail.input_json:
                payload = json.loads(detail.input_json)
                position = payload.get("portfolio", {}).get("positions", {}).get(inference.symbol)
                if isinstance(position, dict):
                    existing_quantity = Decimal(str(position.get("quantity", "0")))
                    existing_entry = Decimal(str(position.get("entry_price", "0")))
                    if existing_quantity > 0 and existing_entry > 0:
                        combined_quantity = existing_quantity + report.filled_quantity
                        entry_basis = (
                            (existing_entry * existing_quantity)
                            + (report.average_price * report.filled_quantity)
                        ) / combined_quantity
            entry_contexts[client_order_id] = (entry_basis, intent.leverage)

        for fill in fills:
            if not fill["reduce_only"] or fill["realized_pnl"] is None:
                continue
            context = entry_contexts.get(fill.get("related_client_order_id"))
            if context is None:
                continue
            entry_basis, leverage = context
            quantity = Decimal(str(fill["report"].get("filled_quantity", "0")))
            margin = abs(quantity * entry_basis) / leverage
            if margin <= 0:
                continue
            realized_pnl = Decimal(str(fill["realized_pnl"]))
            fill["realized_pnl_margin_usdt"] = str(margin)
            fill["realized_return_percent"] = str((realized_pnl / margin) * Decimal("100"))

    @staticmethod
    def _trade_fill_identity(
        client_order_id: str,
        *,
        intent_action: str | None = None,
        reduce_only: bool = False,
    ) -> tuple[str, str | None]:
        suffixes = {
            "-sl": "stop_loss",
            "-tp": "take_profit",
            "-rescue": "rescue_close",
        }
        for suffix, purpose in suffixes.items():
            if client_order_id.endswith(suffix):
                return purpose, client_order_id[: -len(suffix)]
        if client_order_id.startswith("cp-manual-"):
            return "manual_close", None
        if client_order_id.startswith("cp-kill-"):
            return "other_close", None
        if intent_action == "CLOSE":
            return "model_close", None
        if intent_action == "REDUCE":
            return "model_reduce", None
        if reduce_only:
            return "other_close", None
        return "entry", None

    async def _latest_entry_before(
        self, session: AsyncSession, exit_event: UserStreamEventRow
    ) -> str | None:
        candidates = (
            await session.scalars(
                select(UserStreamEventRow)
                .where(
                    UserStreamEventRow.event_type == "ORDER_TRADE_UPDATE",
                    UserStreamEventRow.symbol == exit_event.symbol,
                    UserStreamEventRow.event_time <= exit_event.event_time,
                    UserStreamEventRow.id != exit_event.id,
                )
                .order_by(UserStreamEventRow.event_time.desc(), UserStreamEventRow.id.desc())
                .limit(100)
            )
        ).all()
        for candidate in candidates:
            order = json.loads(candidate.payload_json).get("o", {})
            if order.get("X") != "FILLED" or order.get("x") != "TRADE":
                continue
            if order.get("R") in {True, "true", "TRUE", 1, "1"}:
                continue
            client_order_id = str(order.get("c", ""))
            purpose, _ = self._trade_fill_identity(client_order_id)
            if purpose == "entry":
                return client_order_id
        return None

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
        async with self.sessions.begin() as session:
            existing: UserStreamEventRow | None = None
            identity = self._terminal_fill_identity(event.payload)
            if identity is not None:
                order = event.payload.get("o", {})
                order_id = order.get("i")
                lookup = select(UserStreamEventRow).where(
                    UserStreamEventRow.event_type == event.event_type,
                    UserStreamEventRow.symbol == event.symbol,
                )
                if order_id is not None:
                    lookup = lookup.where(
                        func.json_extract(UserStreamEventRow.payload_json, "$.o.i") == order_id
                    )
                else:
                    lookup = lookup.where(
                        func.json_extract(UserStreamEventRow.payload_json, "$.o.c")
                        == str(order.get("c", ""))
                    )
                candidates = (
                    await session.scalars(lookup.order_by(UserStreamEventRow.id.desc()))
                ).all()
                existing = next(
                    (
                        candidate
                        for candidate in candidates
                        if self._terminal_fill_identity(json.loads(candidate.payload_json))
                        == identity
                    ),
                    None,
                )

            payload_json = json.dumps(event.payload, separators=(",", ":"))
            if existing is None:
                row = UserStreamEventRow(
                    event_type=event.event_type,
                    symbol=event.symbol,
                    event_time=event.event_time,
                    transaction_time=event.transaction_time,
                    payload_json=payload_json,
                )
                session.add(row)
            else:
                row = existing
                existing_payload = json.loads(row.payload_json)
                if (
                    existing_payload.get("_source") == "rest_trade_reconciliation"
                    and event.payload.get("_source") != "rest_trade_reconciliation"
                ):
                    row.event_time = event.event_time
                    row.transaction_time = event.transaction_time
                    row.payload_json = payload_json
            session.add(row)
            if event.event_type == "ORDER_TRADE_UPDATE":
                order = event.payload.get("o", {})
                client_order_id = order.get("c")
                if client_order_id:
                    execution = await session.scalar(
                        select(ExecutionRow).where(
                            ExecutionRow.client_order_id == str(client_order_id)
                        )
                    )
                    if execution is not None:
                        self._apply_order_update(execution, event.payload, event.event_time)
        return row.id

    @staticmethod
    def _apply_order_update(
        row: ExecutionRow, payload: dict[str, Any], event_time: datetime
    ) -> None:
        """Advance one REST execution report from a Binance order event."""

        order = payload.get("o", {})
        status = str(order.get("X", ""))
        supported = {
            "NEW",
            "PARTIALLY_FILLED",
            "FILLED",
            "CANCELED",
            "REJECTED",
            "EXPIRED",
            "EXPIRED_IN_MATCH",
        }
        if status not in supported:
            return
        terminal = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "EXPIRED_IN_MATCH"}
        if row.status in terminal and status != row.status:
            return
        if row.status == "PARTIALLY_FILLED" and status == "NEW":
            return

        previous = ExecutionReport.model_validate_json(row.report_json)
        incoming_filled = Decimal(str(order.get("z", previous.filled_quantity)))
        filled = max(previous.filled_quantity, incoming_filled)
        raw_average = Decimal(str(order.get("ap", "0")))
        average = (
            raw_average
            if raw_average > 0 and incoming_filled >= previous.filled_quantity
            else previous.average_price
        )
        updated = ExecutionReport(
            client_order_id=previous.client_order_id,
            status=status,
            filled_quantity=filled,
            average_price=average,
            message="Binance user stream order update",
            timestamp=event_time,
        )
        row.status = status
        row.report_json = updated.model_dump_json()

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

    async def store_book_captures(self, captures: list[dict[str, Any]]) -> int:
        """Write a boundary's captures, ignoring any already recorded.

        A restarted collector resumes on a boundary it may have already
        written; the unique index makes that a no-op rather than a duplicate.
        """

        if not captures:
            return 0
        written = 0
        async with self.sessions.begin() as session:
            for item in captures:
                existing = await session.scalar(
                    select(BookCaptureRow.id).where(
                        BookCaptureRow.symbol == item["symbol"],
                        BookCaptureRow.captured_at == item["captured_at"],
                    )
                )
                if existing is not None:
                    continue
                session.add(
                    BookCaptureRow(
                        symbol=item["symbol"],
                        captured_at=item["captured_at"],
                        schema_version=item["schema_version"],
                        payload_json=json.dumps(
                            item["payload"], separators=(",", ":"), default=str
                        ),
                    )
                )
                written += 1
        return written

    async def book_captures(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(BookCaptureRow)
                    .where(
                        BookCaptureRow.symbol == symbol,
                        BookCaptureRow.captured_at >= start,
                        BookCaptureRow.captured_at <= end,
                    )
                    .order_by(BookCaptureRow.captured_at)
                )
            ).all()
        return [
            {
                "symbol": row.symbol,
                "captured_at": self._utc(row.captured_at),
                "schema_version": row.schema_version,
                **json.loads(row.payload_json),
            }
            for row in rows
        ]

    async def book_capture_summary(self) -> list[dict[str, Any]]:
        """What has been recorded so far, per symbol."""

        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(
                        BookCaptureRow.symbol,
                        func.count(BookCaptureRow.id),
                        func.min(BookCaptureRow.captured_at),
                        func.max(BookCaptureRow.captured_at),
                    ).group_by(BookCaptureRow.symbol)
                )
            ).all()
        return [
            {
                "symbol": symbol,
                "capture_count": count,
                "first_capture_at": self._utc(first),
                "last_capture_at": self._utc(last),
            }
            for symbol, count, first, last in rows
        ]

    async def create_backtest_run(
        self, spec: dict[str, Any], providers: list[str]
    ) -> int:
        async with self.sessions.begin() as session:
            run = BacktestRunRow(
                spec_json=json.dumps(spec, separators=(",", ":"), default=str),
                status="running",
            )
            session.add(run)
            await session.flush()
            for provider in providers:
                session.add(BacktestModelRunRow(run_id=run.id, provider=provider))
            return run.id

    async def record_backtest_decisions(
        self, run_id: int, provider: str, rows: list[dict[str, Any]]
    ) -> None:
        """Append completed model decisions so running details stay current."""

        if not rows:
            return
        async with self.sessions.begin() as session:
            await session.execute(
                insert(BacktestDecisionRow),
                [{"run_id": run_id, "provider": provider, **row} for row in rows],
            )

    async def backtest_decisions(
        self,
        run_id: int,
        *,
        provider: str | None = None,
        after_id: int = 0,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], int]:
        """One model's decisions for a run, oldest first, plus the total count."""

        conditions = [BacktestDecisionRow.run_id == run_id]
        if provider is not None:
            conditions.append(BacktestDecisionRow.provider == provider)
        query = (
            select(BacktestDecisionRow)
            .where(*conditions, BacktestDecisionRow.id > after_id)
            .order_by(BacktestDecisionRow.id)
            .limit(limit + 1)
        )
        async with self.sessions() as session:
            rows = (await session.scalars(query)).all()
            total = await session.scalar(
                select(func.count()).select_from(BacktestDecisionRow).where(*conditions)
            )
        return [
            {
                "id": row.id,
                "provider": row.provider,
                "decided_at": self._utc(row.decided_at).isoformat(),
                "symbol": row.symbol,
                "cadence": row.cadence,
                "outcome": row.outcome,
                "action": row.action,
                "confidence": row.confidence,
                "rationale": row.rationale,
                "detail": row.detail,
                "fill": json.loads(row.fill_json) if row.fill_json else None,
                "attempt_started_at": json.loads(row.attempts_json or "[]"),
            }
            for row in rows
        ], int(total or 0)

    async def update_backtest_progress(
        self,
        run_id: int,
        provider: str,
        *,
        decisions_done: int,
        decisions_total: int,
        calls_failed: int,
        usage: dict[str, Any] | None = None,
        progress: dict[str, Any] | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(BacktestModelRunRow).where(
                    BacktestModelRunRow.run_id == run_id,
                    BacktestModelRunRow.provider == provider,
                )
            )
            if row is None:
                return
            row.decisions_done = decisions_done
            row.decisions_total = decisions_total
            row.calls_failed = calls_failed
            if usage is not None:
                row.usage_json = json.dumps(usage, separators=(",", ":"))
            if progress is not None:
                row.progress_json = json.dumps(
                    progress, separators=(",", ":"), default=str
                )
            if result is not None:
                row.result_json = json.dumps(result, separators=(",", ":"), default=str)
            if error is not None:
                row.error = error

    async def finish_backtest_run(
        self,
        run_id: int,
        *,
        status: str,
        error: str | None = None,
        effective_end: datetime | None = None,
    ) -> None:
        async with self.sessions.begin() as session:
            run = await session.get(BacktestRunRow, run_id)
            if run is None:
                return
            run.status = status
            run.error = error
            if effective_end is not None:
                spec = json.loads(run.spec_json)
                spec.setdefault("requested_end", spec["end"])
                spec["end"] = effective_end.isoformat()
                run.spec_json = json.dumps(spec, separators=(",", ":"))
            run.ended_at = datetime.now(UTC)

    async def backtest_run(self, run_id: int) -> dict[str, Any] | None:
        async with self.sessions() as session:
            run = await session.get(BacktestRunRow, run_id)
            if run is None:
                return None
            models = (
                await session.scalars(
                    select(BacktestModelRunRow)
                    .where(BacktestModelRunRow.run_id == run_id)
                    .order_by(BacktestModelRunRow.id)
                )
            ).all()
        return self._backtest_dict(run, list(models), detail=True)

    async def recent_backtest_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self.sessions() as session:
            runs = (
                await session.scalars(
                    select(BacktestRunRow).order_by(BacktestRunRow.id.desc()).limit(limit)
                )
            ).all()
            if not runs:
                return []
            models = (
                await session.scalars(
                    select(BacktestModelRunRow)
                    .where(BacktestModelRunRow.run_id.in_([run.id for run in runs]))
                    .order_by(BacktestModelRunRow.id)
                )
            ).all()
        by_run: dict[int, list[BacktestModelRunRow]] = {}
        for model in models:
            by_run.setdefault(model.run_id, []).append(model)
        return [
            self._backtest_dict(run, by_run.get(run.id, []), detail=False) for run in runs
        ]

    def _backtest_dict(
        self,
        run: BacktestRunRow,
        models: list[BacktestModelRunRow],
        *,
        detail: bool,
    ) -> dict[str, Any]:
        spec = json.loads(run.spec_json)
        provider_configs = spec.get("provider_configs", {})
        return {
            "id": run.id,
            "status": run.status,
            "error": run.error,
            "spec": spec,
            "created_at": self._utc(run.created_at),
            "ended_at": self._utc(run.ended_at) if run.ended_at is not None else None,
            "models": [
                self._model_run_dict(
                    model,
                    detail=detail,
                    config=provider_configs.get(model.provider, {}),
                )
                for model in models
            ],
        }

    @staticmethod
    def _model_run_dict(
        row: BacktestModelRunRow, *, detail: bool, config: dict[str, Any]
    ) -> dict[str, Any]:
        result = json.loads(row.result_json) if row.result_json else None
        runtime = json.loads(row.progress_json or "{}")
        if result is not None and not detail:
            # The list view only needs the headline numbers; trades and the
            # curve are megabytes on a three-day run.
            result = {
                key: value
                for key, value in result.items()
                if key not in ("trades", "equity_curve")
            }
        return {
            "provider": row.provider,
            "model": config.get("model"),
            "reasoning_effort": config.get("reasoning_effort"),
            "decisions_done": row.decisions_done,
            "decisions_total": row.decisions_total,
            "calls_failed": row.calls_failed,
            "usage": json.loads(row.usage_json or "{}"),
            "elapsed_seconds": runtime.get("elapsed_seconds", 0.0),
            "remaining_seconds": runtime.get("remaining_seconds"),
            "live_result": runtime.get("live_result"),
            "progress": (row.decisions_done / row.decisions_total)
            if row.decisions_total
            else 0.0,
            "result": result,
            "error": row.error,
        }

    async def recent_decision_events(
        self,
        limit: int = 100,
        *,
        before_id: int | None = None,
        run_limit: int | None = None,
        before_run_id: int | None = None,
        symbol: str | None = None,
        cadence: str | None = None,
        provider: str | None = None,
        outcome: str | None = None,
    ) -> list[dict[str, Any]]:
        conditions = []
        if symbol is not None:
            conditions.append(InferenceRow.symbol == symbol)
        if cadence is not None:
            conditions.append(InferenceRow.cadence == cadence)
        if provider is not None:
            conditions.append(InferenceRow.provider == provider)
        if outcome is not None:
            conditions.append(self._outcome_filter(outcome))
        query = (
            select(
                InferenceRow,
                RiskRow,
                ExecutionAttemptRow,
                ExecutionRow,
                LiveRunRow,
            )
            .outerjoin(RiskRow, RiskRow.inference_id == InferenceRow.id)
            .outerjoin(
                ExecutionAttemptRow,
                ExecutionAttemptRow.inference_id == InferenceRow.id,
            )
            .outerjoin(
                ExecutionRow,
                ExecutionRow.client_order_id == ExecutionAttemptRow.client_order_id,
            )
            .outerjoin(LiveRunRow, LiveRunRow.id == InferenceRow.live_run_id)
            .where(*conditions)
        )
        async with self.sessions() as session:
            if run_limit is not None:
                run_query = (
                    select(InferenceRow.live_run_id)
                    .outerjoin(RiskRow, RiskRow.inference_id == InferenceRow.id)
                    .outerjoin(
                        ExecutionAttemptRow,
                        ExecutionAttemptRow.inference_id == InferenceRow.id,
                    )
                    .where(InferenceRow.live_run_id.is_not(None), *conditions)
                )
                if before_run_id is not None:
                    run_query = run_query.where(
                        InferenceRow.live_run_id < before_run_id
                    )
                run_ids = tuple(
                    (
                        await session.scalars(
                            run_query
                            .distinct()
                            .order_by(InferenceRow.live_run_id.desc())
                            .limit(run_limit)
                        )
                    ).all()
                )
                rows = (
                    (
                        await session.execute(
                            query.where(InferenceRow.live_run_id.in_(run_ids)).order_by(
                                InferenceRow.live_run_id.desc(),
                                InferenceRow.id.desc(),
                            )
                        )
                    ).all()
                    if run_ids
                    else []
                )
            else:
                # Keyset paging on the primary key: ids are monotonic with no ties,
                # so a page cannot skip or repeat a row when new decisions land
                # mid-read, the way an OFFSET would.
                if before_id is not None:
                    query = query.where(InferenceRow.id < before_id)
                rows = (
                    await session.execute(
                        query.order_by(InferenceRow.id.desc()).limit(limit)
                    )
                ).all()
        events = []
        for inference, risk, attempt, execution, live_run in rows:
            usage = json.loads(inference.usage_json)
            intent = TradeIntent.model_validate_json(inference.intent_json)
            outcome = self._decision_outcome(intent, risk, attempt)
            events.append(
                {
                    "id": inference.id,
                    "live_run_id": inference.live_run_id,
                    "live_run": self._live_run_dict(live_run),
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
                    "decision_duration_ms": self._decision_duration_ms(
                        inference,
                        risk,
                        attempt,
                    ),
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
                    "execution": self._execution_attempt_dict(attempt, execution),
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

    def _decision_duration_ms(
        self,
        inference: InferenceRow,
        risk: RiskRow | None,
        attempt: ExecutionAttemptRow | None,
    ) -> float:
        completed_at = (
            attempt.created_at
            if attempt is not None
            else risk.created_at
            if risk is not None
            else inference.created_at
        )
        audit_ms = max(
            0.0,
            (self._utc(completed_at) - self._utc(inference.created_at)).total_seconds()
            * 1000,
        )
        return inference.duration_ms + audit_ms

    def _execution_attempt_dict(
        self,
        attempt: ExecutionAttemptRow | None,
        execution: ExecutionRow | None,
    ) -> dict[str, Any] | None:
        if attempt is None:
            return None
        payload = json.loads(attempt.attempt_json)
        if execution is not None:
            # The execution row is continuously reconciled by the Binance user
            # stream, while the attempt preserves the immediate REST result.
            # Use the reconciled report so delayed fill quantity/price reaches
            # decision details without rewriting immutable attempt history.
            payload["entry_report"] = json.loads(execution.report_json)
        return {
            "id": attempt.id,
            **payload,
            "created_at": self._utc(attempt.created_at),
        }

    def _live_run_dict(self, row: LiveRunRow | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "id": row.id,
            "status": row.status,
            "config": json.loads(row.config_json or "{}"),
            "stop_reason": row.stop_reason,
            "started_at": self._utc(row.started_at),
            "ended_at": self._utc(row.ended_at) if row.ended_at is not None else None,
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
                        ExecutionRow,
                        LiveRunRow,
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
                    .outerjoin(
                        ExecutionRow,
                        ExecutionRow.client_order_id
                        == ExecutionAttemptRow.client_order_id,
                    )
                    .outerjoin(LiveRunRow, LiveRunRow.id == InferenceRow.live_run_id)
                    .where(InferenceRow.id == inference_id)
                )
            ).one_or_none()
        if row is None:
            return None
        inference, risk, detail, attempt, execution, live_run = row
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
            "live_run_id": inference.live_run_id,
            "live_run": self._live_run_dict(live_run),
            "provider": inference.provider,
            "model": inference.model,
            "provenance": usage.get("_provenance", {}),
            "intent": intent.model_dump(mode="json"),
            "duration_ms": inference.duration_ms,
            "decision_duration_ms": self._decision_duration_ms(
                inference,
                risk,
                attempt,
            ),
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
            "execution": self._execution_attempt_dict(attempt, execution),
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
