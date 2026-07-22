from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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
    client_order_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
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
    ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


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


class LiveDecisionSnapshotRow(Base):
    """Exact inputs used by one formal decision cycle.

    This is deliberately independent from inference audit rows: provider
    retries and history presentation must not decide whether replay data
    survives or how many copies of a decision input exist.
    """

    __tablename__ = "live_decision_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    live_run_id: Mapped[int] = mapped_column(
        ForeignKey("live_runs.id", ondelete="CASCADE"), index=True
    )
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    cadence: Mapped[str] = mapped_column(String(8), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    market_json: Mapped[str] = mapped_column(Text)
    portfolio_json: Mapped[str] = mapped_column(Text)
    rules_json: Mapped[str] = mapped_column(Text)


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


class TrailingStopEventRow(Base):
    __tablename__ = "trailing_stop_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24), index=True)
    event_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class PartialTakeProfitEventRow(Base):
    __tablename__ = "partial_take_profit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    event_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )


class MarketAnalysisRow(Base):
    __tablename__ = "market_analyses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    reasoning_effort: Mapped[str | None] = mapped_column(String(32), nullable=True)
    prompt_version: Mapped[str] = mapped_column(String(64))
    data_version: Mapped[str] = mapped_column(String(64))
    input_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage_json: Mapped[str] = mapped_column(Text, default="{}")
    duration_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), index=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class MarketAnalysisOutcomeRow(Base):
    __tablename__ = "market_analysis_outcomes"

    analysis_id: Mapped[int] = mapped_column(
        ForeignKey("market_analyses.id", ondelete="CASCADE"), primary_key=True
    )
    outcome_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class RejectedDecisionOutcomeRow(Base):
    __tablename__ = "rejected_decision_outcomes"

    inference_id: Mapped[int] = mapped_column(
        ForeignKey("inferences.id", ondelete="CASCADE"), primary_key=True
    )
    outcome_json: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SchemaMigrationRow(Base):
    __tablename__ = "schema_migrations"

    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# Historical data and all pre-v12 databases have been retired.  Keep one empty
# baseline migration so the user's current v12 database advances without
# replaying upgrade logic, while fresh databases are created directly from the
# ORM metadata at the current shape.
MINIMUM_SUPPORTED_SCHEMA_VERSION = 12
CURRENT_SCHEMA_VERSION = 19
MIGRATIONS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (13, ()),
    (
        14,
        (
            "CREATE TABLE IF NOT EXISTS trailing_stop_events ("
            "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
            "symbol VARCHAR(32) NOT NULL, mode VARCHAR(16) NOT NULL, "
            "status VARCHAR(24) NOT NULL, event_json TEXT NOT NULL, "
            "created_at DATETIME NOT NULL)",
            "CREATE INDEX IF NOT EXISTS ix_trailing_stop_events_symbol "
            "ON trailing_stop_events (symbol)",
            "CREATE INDEX IF NOT EXISTS ix_trailing_stop_events_status "
            "ON trailing_stop_events (status)",
            "CREATE INDEX IF NOT EXISTS ix_trailing_stop_events_created_at "
            "ON trailing_stop_events (created_at)",
        ),
    ),
    (
        15,
        (
            "CREATE TABLE IF NOT EXISTS live_decision_snapshots ("
            "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
            "live_run_id INTEGER NOT NULL REFERENCES live_runs(id) ON DELETE CASCADE, "
            "batch_id VARCHAR(36) NOT NULL, "
            "symbol VARCHAR(32) NOT NULL, cadence VARCHAR(8) NOT NULL, "
            "captured_at DATETIME NOT NULL, market_json TEXT NOT NULL, "
            "portfolio_json TEXT NOT NULL, rules_json TEXT NOT NULL)",
            "CREATE INDEX IF NOT EXISTS ix_live_decision_snapshots_live_run_id "
            "ON live_decision_snapshots (live_run_id)",
            "CREATE INDEX IF NOT EXISTS ix_live_decision_snapshots_batch_id "
            "ON live_decision_snapshots (batch_id)",
            "CREATE INDEX IF NOT EXISTS ix_live_decision_snapshots_symbol "
            "ON live_decision_snapshots (symbol)",
            "CREATE INDEX IF NOT EXISTS ix_live_decision_snapshots_cadence "
            "ON live_decision_snapshots (cadence)",
            "CREATE INDEX IF NOT EXISTS ix_live_decision_snapshots_captured_at "
            "ON live_decision_snapshots (captured_at)",
        ),
    ),
    (
        16,
        (
            "CREATE TABLE IF NOT EXISTS partial_take_profit_events ("
            "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
            "symbol VARCHAR(32) NOT NULL, status VARCHAR(32) NOT NULL, "
            "event_json TEXT NOT NULL, created_at DATETIME NOT NULL)",
            "CREATE INDEX IF NOT EXISTS ix_partial_take_profit_events_symbol "
            "ON partial_take_profit_events (symbol)",
            "CREATE INDEX IF NOT EXISTS ix_partial_take_profit_events_status "
            "ON partial_take_profit_events (status)",
            "CREATE INDEX IF NOT EXISTS ix_partial_take_profit_events_created_at "
            "ON partial_take_profit_events (created_at)",
        ),
    ),
    (
        17,
        (
            "CREATE TABLE IF NOT EXISTS market_analyses ("
            "id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT, "
            "symbol VARCHAR(32) NOT NULL, status VARCHAR(16) NOT NULL, "
            "provider VARCHAR(64) NOT NULL, model VARCHAR(128), "
            "reasoning_effort VARCHAR(32), prompt_version VARCHAR(64) NOT NULL, "
            "data_version VARCHAR(64) NOT NULL, input_json TEXT, prompt_text TEXT, "
            "result_json TEXT, raw_output TEXT, usage_json TEXT NOT NULL DEFAULT '{}', "
            "duration_ms FLOAT, error TEXT, created_at DATETIME NOT NULL, completed_at DATETIME)",
            "CREATE INDEX IF NOT EXISTS ix_market_analyses_symbol ON market_analyses (symbol)",
            "CREATE INDEX IF NOT EXISTS ix_market_analyses_status ON market_analyses (status)",
            "CREATE INDEX IF NOT EXISTS ix_market_analyses_provider ON market_analyses (provider)",
            "CREATE INDEX IF NOT EXISTS ix_market_analyses_created_at ON market_analyses (created_at)",
        ),
    ),
    (
        18,
        (
            "CREATE TABLE IF NOT EXISTS market_analysis_outcomes ("
            "analysis_id INTEGER NOT NULL PRIMARY KEY REFERENCES market_analyses(id) ON DELETE CASCADE, "
            "outcome_json TEXT NOT NULL, updated_at DATETIME NOT NULL)",
        ),
    ),
    (
        19,
        (
            "CREATE TABLE IF NOT EXISTS rejected_decision_outcomes ("
            "inference_id INTEGER NOT NULL PRIMARY KEY REFERENCES inferences(id) ON DELETE CASCADE, "
            "outcome_json TEXT NOT NULL, updated_at DATETIME NOT NULL)",
        ),
    ),
)
