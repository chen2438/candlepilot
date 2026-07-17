import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from decimal import Decimal
from sqlalchemy import text

from candlepilot.broker.user_stream import UserStreamEvent
from candlepilot.domain.models import (
    ExecutionAttempt,
    ExecutionReport,
    RiskDecision,
    TradeAction,
    TradeIntent,
)
from candlepilot.providers.base import ProviderResult
from candlepilot.providers.pricing import parse_models_dev
from candlepilot.storage.database import (
    CURRENT_SCHEMA_VERSION,
    DECISION_OUTCOMES,
    AuditRepository,
    Database,
)


def test_inference_audit_round_trip(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "test")
        identifier = await repository.record_inference(
            ProviderResult(
                intent=intent,
                provider="codex-auth",
                model="test-model",
                duration=timedelta(milliseconds=123),
                raw_output=intent.model_dump_json(),
                usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                prompt_version="trade-intent-v1",
                data_version="market-snapshot-v1:sha256:test",
                provider_version="codex-cli 1.0",
                input_payload={"market": {"symbol": "BTCUSDT"}, "portfolio": {"equity": "1"}},
                prompt="fixture prompt",
            )
        )
        rows = await repository.recent_intents()
        detail = await repository.decision_detail(identifier)
        replay_rows = await repository.intents_between(
            "BTCUSDT",
            "5m",
            datetime.now(UTC) - timedelta(minutes=1),
            datetime.now(UTC) + timedelta(minutes=1),
        )
        await database.close()
        return identifier, rows, replay_rows, detail

    identifier, rows, replay_rows, detail = asyncio.run(scenario())
    assert identifier == 1
    assert rows[0]["provider"] == "codex-auth"
    assert rows[0]["intent"]["symbol"] == "BTCUSDT"
    assert rows[0]["duration_ms"] == 123
    assert rows[0]["provenance"]["prompt_version"] == "trade-intent-v1"
    assert replay_rows[0]["model"] == "test-model"
    assert replay_rows[0]["intent"].symbol == "BTCUSDT"
    assert detail is not None
    assert detail["input"]["market"]["symbol"] == "BTCUSDT"
    assert detail["prompt"] == "fixture prompt"
    assert detail["audit_status"] == "complete"
    assert '"symbol":"BTCUSDT"' in detail["raw_output"]
    assert detail["usage"]["input_tokens"] == 10


def test_inference_audit_distinguishes_partial_and_unavailable_details(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-status.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "test")
        partial_id = await repository.record_inference(
            ProviderResult(
                intent,
                "claude-code-auth",
                None,
                timedelta(0),
                "error",
                {"error": "ProviderError"},
                input_payload={"market": {"symbol": "BTCUSDT"}},
            )
        )
        unavailable_id = await repository.record_inference(
            ProviderResult(
                intent,
                "claude-code-auth",
                None,
                timedelta(0),
                "legacy",
                {},
            )
        )
        partial = await repository.decision_detail(partial_id)
        unavailable = await repository.decision_detail(unavailable_id)
        await database.close()
        return partial, unavailable

    partial, unavailable = asyncio.run(scenario())
    assert partial is not None and partial["audit_status"] == "partial"
    assert unavailable is not None and unavailable["audit_status"] == "unavailable"


def test_execution_and_risk_queries_filter_and_order(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'queries.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        await repository.record_execution(
            "BTCUSDT",
            ExecutionReport(client_order_id="cp-1", status="FILLED", average_price=Decimal("100")),
        )
        await repository.record_execution(
            "ETHUSDT",
            ExecutionReport(client_order_id="cp-2", status="NEW"),
        )
        intent = TradeIntent.hold("ETHUSDT", "5m", "risk link")
        inference_id = await repository.record_inference(
            ProviderResult(
                intent=intent,
                provider="codex-auth",
                model="m",
                duration=timedelta(milliseconds=1),
                raw_output=intent.model_dump_json(),
                usage={},
            )
        )
        await repository.record_risk("BTCUSDT", RiskDecision(accepted=True, reason="within limits"))
        await repository.record_risk(
            "ETHUSDT",
            RiskDecision(accepted=False, reason="missing stop"),
            inference_id=inference_id,
        )
        orders = await repository.recent_executions()
        fills = await repository.recent_executions(status="FILLED")
        risk = await repository.recent_risk_decisions()
        rejections = await repository.recent_risk_decisions(accepted=False)
        await database.close()
        return orders, fills, risk, rejections, inference_id

    orders, fills, risk, rejections, inference_id = asyncio.run(scenario())
    assert [item["client_order_id"] for item in orders] == ["cp-2", "cp-1"]
    assert [item["status"] for item in fills] == ["FILLED"]
    assert fills[0]["report"]["average_price"] == "100"
    assert risk[0]["symbol"] == "ETHUSDT" and risk[0]["accepted"] is False
    assert len(rejections) == 1
    assert rejections[0]["reason"] == "missing stop"
    assert rejections[0]["inference_id"] == inference_id


def test_order_events_advance_the_execution_audit_to_final_status(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'execution-updates.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        now = datetime.now(UTC)
        await repository.record_execution(
            "BTCUSDT", ExecutionReport(client_order_id="cp-live", status="NEW")
        )
        await repository.record_user_event(
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now,
                now,
                "BTCUSDT",
                {"o": {"c": "cp-live", "X": "PARTIALLY_FILLED", "z": "0.4", "ap": "99"}},
            )
        )
        await repository.record_user_event(
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now + timedelta(seconds=1),
                now + timedelta(seconds=1),
                "BTCUSDT",
                {"o": {"c": "cp-live", "X": "FILLED", "z": "1", "ap": "100"}},
            )
        )
        await repository.record_user_event(
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now + timedelta(seconds=2),
                now + timedelta(seconds=2),
                "BTCUSDT",
                {"o": {"c": "cp-live", "X": "NEW", "z": "0", "ap": "0"}},
            )
        )
        result = (await repository.recent_executions())[0]
        await database.close()
        return result

    result = asyncio.run(scenario())
    assert result["status"] == "FILLED"
    assert result["report"]["filled_quantity"] == "1"
    assert result["report"]["average_price"] == "100"


def test_execution_record_catches_up_when_the_user_event_arrives_first(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'execution-race.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        now = datetime.now(UTC)
        await repository.record_user_event(
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now,
                now,
                "ETHUSDT",
                {"o": {"c": "cp-race", "X": "CANCELED", "z": "0", "ap": "0"}},
            )
        )
        await repository.record_execution(
            "ETHUSDT", ExecutionReport(client_order_id="cp-race", status="NEW")
        )
        result = (await repository.recent_executions())[0]
        await database.close()
        return result

    result = asyncio.run(scenario())
    assert result["status"] == "CANCELED"
    assert result["report"]["timestamp"] is not None


def test_decision_events_join_inference_and_risk_outcomes(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'decision-events.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        opening = TradeIntent(
            symbol="BTCUSDT",
            cadence="5m",
            action="OPEN_LONG",
            confidence=0.8,
            leverage=3,
            risk_fraction="0.01",
            stop_loss="95",
            take_profit="110",
            rationale="trend",
        )
        opening_id = await repository.record_inference(
            ProviderResult(
                opening,
                "codex-auth",
                "gpt-test",
                timedelta(milliseconds=80),
                "{}",
                {},
                reasoning_effort="high",
            )
        )
        await repository.record_risk(
            "BTCUSDT",
            RiskDecision(accepted=False, reason="margin limit"),
            inference_id=opening_id,
        )
        hold = TradeIntent.hold("ETHUSDT", "5m", "no setup")
        hold_id = await repository.record_inference(
            ProviderResult(hold, "claude-code-auth", None, timedelta(milliseconds=20), "{}", {})
        )
        await repository.record_risk(
            "ETHUSDT",
            RiskDecision(accepted=True, reason="hold: no order required"),
            inference_id=hold_id,
        )
        approved_id = await repository.record_inference(
            ProviderResult(
                opening.model_copy(update={"symbol": "SOLUSDT"}),
                "codex-auth",
                "gpt-test",
                timedelta(milliseconds=60),
                "{}",
                {},
            )
        )
        await repository.record_risk(
            "SOLUSDT",
            RiskDecision(accepted=True, reason="within limits", max_quantity="2.5"),
            inference_id=approved_id,
        )
        await repository.record_execution_attempt(
            "SOLUSDT",
            ExecutionAttempt(
                inference_id=approved_id,
                client_order_id="cp-sol",
                status="SUCCEEDED",
                stage="COMPLETE",
                message="completed",
            ),
        )
        failed_id = await repository.record_inference(
            ProviderResult(
                opening.model_copy(update={"symbol": "DOGEUSDT"}),
                "codex-auth",
                "gpt-test",
                timedelta(milliseconds=55),
                "{}",
                {},
            )
        )
        await repository.record_risk(
            "DOGEUSDT",
            RiskDecision(accepted=True, reason="within limits", max_quantity="100"),
            inference_id=failed_id,
        )
        await repository.record_execution_attempt(
            "DOGEUSDT",
            ExecutionAttempt(
                inference_id=failed_id,
                client_order_id="cp-doge",
                status="RESCUED",
                stage="PROTECTION",
                message="protective bracket failed; rescued",
                exchange_error_code=-4120,
                estimated_loss_usdt="1.25",
            ),
        )
        await repository.record_inference(
            ProviderResult(
                opening.model_copy(update={"symbol": "XRPUSDT"}),
                "codex-auth",
                "gpt-test",
                timedelta(milliseconds=40),
                "{}",
                {},
            )
        )
        events = await repository.recent_decision_events()
        await database.close()
        return events

    events = asyncio.run(scenario())
    assert [event["outcome"] for event in events] == [
        "analysis_only",
        "execution_failed",
        "executed",
        "hold",
        "rejected",
    ]
    assert events[0]["risk"] is None
    assert events[1]["execution"]["status"] == "RESCUED"
    assert events[1]["execution"]["estimated_loss_usdt"] == "1.25"
    assert events[2]["risk"]["decision"]["max_quantity"] == "2.5"
    assert events[2]["execution"]["status"] == "SUCCEEDED"
    assert events[3]["intent"]["symbol"] == "ETHUSDT"
    assert events[3]["risk"]["accepted"] is True
    assert events[4]["model"] == "gpt-test"
    assert events[4]["provenance"]["reasoning_effort"] == "high"
    assert events[4]["risk"]["reason"] == "margin limit"


def test_provider_metrics_aggregate_latency_errors_and_models(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'provider-metrics.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "test")
        for model, duration_ms, usage in (
            ("model-a", 100, {}),
            ("model-a", 300, {"error": "ProviderError"}),
            ("model-b", 200, {}),
        ):
            await repository.record_inference(
                ProviderResult(
                    intent=intent,
                    provider="codex-auth",
                    model=model,
                    duration=timedelta(milliseconds=duration_ms),
                    raw_output="{}",
                    usage=usage,
                )
            )
        metrics = await repository.provider_metrics(24)
        await database.close()
        return metrics

    metrics = asyncio.run(scenario())
    assert len(metrics) == 1
    assert metrics[0]["provider"] == "codex-auth"
    assert metrics[0]["call_count"] == 3
    assert metrics[0]["error_count"] == 1
    assert metrics[0]["error_rate"] == 1 / 3
    assert metrics[0]["average_duration_ms"] == 200
    assert metrics[0]["p95_duration_ms"] == 300
    assert metrics[0]["models"] == {"model-a": 2, "model-b": 1}
    assert metrics[0]["last_call_at"].tzinfo is UTC


def test_provider_metrics_aggregate_tokens_and_equivalent_cost(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'metrics.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "test")

        def result(provider: str, model: str | None, usage: dict) -> ProviderResult:
            return ProviderResult(
                intent=intent,
                provider=provider,
                model=model,
                duration=timedelta(milliseconds=100),
                raw_output=intent.model_dump_json(),
                usage=usage,
            )

        # Claude: tokens + equivalent USD cost.
        await repository.record_inference(
            result("claude-code-auth", "claude-sonnet-5", {"total_tokens": 100, "cost_usd": 0.05})
        )
        await repository.record_inference(
            result("claude-code-auth", "claude-sonnet-5", {"total_tokens": 40, "cost_usd": 0.02})
        )
        # Codex: tokens only, no cost data (subscription).
        await repository.record_inference(
            result("codex-auth", "gpt-5.6-sol", {"total_tokens": 6903})
        )
        metrics = await repository.provider_metrics(24)
        await database.close()
        return {item["provider"]: item for item in metrics}

    metrics = asyncio.run(scenario())
    claude = metrics["claude-code-auth"]
    codex = metrics["codex-auth"]
    assert claude["tokens_total"] == 140
    assert abs(claude["cost_usd_total"] - 0.07) < 1e-9
    assert claude["models"] == {"claude-sonnet-5": 2}
    assert codex["tokens_total"] == 6903
    assert codex["cost_usd_total"] is None  # no equivalent cost for Codex
    assert codex["models"] == {"gpt-5.6-sol": 1}


def test_provider_metrics_price_codex_via_catalog(tmp_path: Path) -> None:
    from candlepilot.providers.pricing import parse_models_dev

    catalog = parse_models_dev(
        {
            "openai": {
                "models": {"gpt-5.6-sol": {"cost": {"input": 5, "output": 30, "cache_read": 0.5}}}
            }
        }
    )

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'codexcost.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "test")
        await repository.record_inference(
            ProviderResult(
                intent=intent,
                provider="codex-auth",
                model="gpt-5.6-sol",
                duration=timedelta(milliseconds=100),
                raw_output=intent.model_dump_json(),
                usage={
                    "input_tokens": 1000,
                    "cached_input_tokens": 400,
                    "output_tokens": 200,
                    "total_tokens": 1200,
                },
            )
        )
        metrics = await repository.provider_metrics(24, catalog=catalog)
        await database.close()
        return metrics[0]

    codex = asyncio.run(scenario())
    # 600 non-cached input * 5e-6 + 400 cached * 5e-7 + 200 output * 3e-5
    expected = 600 * 5e-6 + 400 * 5e-7 + 200 * 3e-5
    assert abs(codex["cost_usd_total"] - expected) < 1e-12
    assert codex["tokens_total"] == 1200


def test_run_session_metrics_respect_id_boundaries_and_complete_cost(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'run-session.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "test")

        async def record(usage: dict) -> int:
            return await repository.record_inference(
                ProviderResult(
                    intent=intent,
                    provider="claude-code-auth",
                    model="claude-test",
                    duration=timedelta(milliseconds=100),
                    raw_output="{}",
                    usage=usage,
                )
            )

        await record({"total_tokens": 999, "cost_usd": 1})
        start_id = await repository.latest_inference_id()
        await record(
            {
                "input_tokens": 100,
                "cache_read_input_tokens": 40,
                "cache_creation_input_tokens": 10,
                "output_tokens": 20,
                "total_tokens": 120,
                "cost_usd": 0.01,
            }
        )
        end_id = await repository.latest_inference_id()
        await record({"input_tokens": 50, "output_tokens": 5, "error": "fixture"})
        completed = await repository.run_session_metrics(start_id, end_at_id=end_id)
        running = await repository.run_session_metrics(start_id)
        await database.close()
        return completed, running

    completed, running = asyncio.run(scenario())
    assert completed == {
        "call_count": 1,
        "error_count": 0,
        "input_tokens": 100,
        "cached_input_tokens": 40,
        "cache_creation_input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 120,
        "priced_call_count": 1,
        "cost_complete": True,
        "equivalent_cost_usd": 0.01,
        "average_duration_ms": 100.0,
        "average_tokens": 120.0,
        "average_cost_usd": 0.01,
    }
    assert running["call_count"] == 2
    assert running["error_count"] == 1
    assert running["total_tokens"] == 175
    assert running["cost_complete"] is False
    assert running["equivalent_cost_usd"] is None
    assert running["average_duration_ms"] == 100.0
    assert running["average_tokens"] == 87.5
    assert running["average_cost_usd"] is None


def test_clear_history_is_selective_and_preserves_runtime_state(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'clear.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "seed")
        inference_id = await repository.record_inference(
            ProviderResult(
                intent=intent,
                provider="codex-auth",
                model="m",
                duration=timedelta(milliseconds=1),
                raw_output=intent.model_dump_json(),
                usage={},
            )
        )
        await repository.record_risk(
            "BTCUSDT",
            RiskDecision(accepted=True, reason="ok"),
            inference_id=inference_id,
        )
        await repository.set_runtime_state("paper_account", "keep-me")

        counts = await repository.clear_history({"inferences"})
        remaining_inferences = await repository.recent_intents()
        remaining_risk = await repository.recent_risk_decisions()
        preserved = await repository.get_runtime_state("paper_account")
        await database.close()
        return counts, remaining_inferences, remaining_risk, preserved

    counts, inferences, risk, preserved = asyncio.run(scenario())
    assert counts == {"inferences": 1}
    assert inferences == []
    assert len(risk) == 1  # not selected -> retained, but no dangling identity
    assert risk[0]["inference_id"] is None
    assert preserved == "keep-me"  # runtime_state (paper account) never cleared


def test_database_migrations_are_versioned_and_idempotent(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'migrations.db'}")
        await database.initialize()
        first = await database.schema_version()
        await database.initialize()
        second = await database.schema_version()
        await database.close()
        return first, second

    # Derived, not hardcoded: the point is that re-running initialize is a
    # no-op, not that the schema happens to be at some particular version.
    assert asyncio.run(scenario()) == (CURRENT_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION)


def test_risk_foreign_key_migration_nulls_existing_orphans(tmp_path: Path) -> None:
    path = tmp_path / "risk-v7.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at DATETIME);
        INSERT INTO schema_migrations VALUES (7, '2026-07-17 00:00:00');
        CREATE TABLE inferences (id INTEGER PRIMARY KEY);
        INSERT INTO inferences VALUES (1);
        CREATE TABLE risk_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inference_id INTEGER,
            symbol VARCHAR(32) NOT NULL,
            accepted INTEGER NOT NULL,
            reason TEXT NOT NULL,
            decision_json TEXT NOT NULL,
            created_at DATETIME NOT NULL
        );
        INSERT INTO risk_decisions VALUES
            (1, 1, 'BTCUSDT', 1, 'linked', '{}', '2026-07-17 00:00:00'),
            (2, 99, 'ETHUSDT', 0, 'orphan', '{}', '2026-07-17 00:00:00');
        """
    )
    connection.close()

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{path}")
        async with database.engine.begin() as migration_connection:
            await database._apply_migrations(migration_connection)
            rows = (
                await migration_connection.execute(
                    text("SELECT id, inference_id FROM risk_decisions ORDER BY id")
                )
            ).all()
        await database.close()
        return rows

    assert asyncio.run(scenario()) == [(1, 1), (2, None)]


async def _seed_decisions(repository: AuditRepository) -> None:
    """One decision of every outcome the classifier can produce."""

    async def infer(symbol: str, cadence: str, provider: str, intent: TradeIntent) -> int:
        return await repository.record_inference(
            ProviderResult(
                intent=intent,
                provider=provider,
                model="m",
                duration=timedelta(milliseconds=1),
                raw_output=intent.model_dump_json(),
                usage={},
            )
        )

    def opening(symbol: str, cadence: str) -> TradeIntent:
        return TradeIntent(
            symbol=symbol,
            cadence=cadence,
            action=TradeAction.OPEN_LONG,
            confidence=0.8,
            leverage=2,
            risk_fraction="0.01",
            stop_loss="90",
            take_profit="110",
            rationale="seed",
        )

    # hold
    await infer("BTCUSDT", "5m", "codex-auth", TradeIntent.hold("BTCUSDT", "5m", "no setup"))
    # analysis_only: an intent that never reached the risk policy.
    await infer("ETHUSDT", "5m", "codex-auth", opening("ETHUSDT", "5m"))
    # rejected
    rejected = await infer("BTCUSDT", "15m", "claude-code-auth", opening("BTCUSDT", "15m"))
    await repository.record_risk(
        "BTCUSDT", RiskDecision(accepted=False, reason="no"), inference_id=rejected
    )
    # approved: risk passed, nothing executed.
    approved = await infer("BTCUSDT", "30m", "codex-auth", opening("BTCUSDT", "30m"))
    await repository.record_risk(
        "BTCUSDT", RiskDecision(accepted=True, reason="ok"), inference_id=approved
    )
    # executed
    executed = await infer("SOLUSDT", "5m", "codex-auth", opening("SOLUSDT", "5m"))
    await repository.record_risk(
        "SOLUSDT", RiskDecision(accepted=True, reason="ok"), inference_id=executed
    )
    await repository.record_execution_attempt(
        "SOLUSDT",
        ExecutionAttempt(
            inference_id=executed, status="SUCCEEDED", stage="COMPLETE", message="filled"
        ),
    )
    # execution_failed
    failed = await infer("SOLUSDT", "15m", "codex-auth", opening("SOLUSDT", "15m"))
    await repository.record_risk(
        "SOLUSDT", RiskDecision(accepted=True, reason="ok"), inference_id=failed
    )
    await repository.record_execution_attempt(
        "SOLUSDT",
        ExecutionAttempt(
            inference_id=failed, status="FAILED", stage="ENTRY", message="rejected"
        ),
    )


def test_outcome_filter_agrees_with_the_python_classifier(tmp_path: Path) -> None:
    """The SQL filter reproduces _decision_outcome and must not drift from it.

    A filter that classifies differently from the column the console displays
    would answer "show me every rejection" with the wrong set, and nothing in
    the response would admit it.
    """

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'outcomes.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        await _seed_decisions(repository)
        everything = await repository.recent_decision_events(500)
        by_outcome = {
            outcome: await repository.recent_decision_events(500, outcome=outcome)
            for outcome in DECISION_OUTCOMES
        }
        await database.close()
        return everything, by_outcome

    everything, by_outcome = asyncio.run(scenario())

    # The seed covers every outcome, so this test fails if a new one is added
    # without a filter branch to match.
    assert {event["outcome"] for event in everything} == set(DECISION_OUTCOMES)
    for outcome in DECISION_OUTCOMES:
        expected = [event["id"] for event in everything if event["outcome"] == outcome]
        assert [event["id"] for event in by_outcome[outcome]] == expected, outcome
        assert expected, outcome


def test_decision_events_page_backwards_without_skipping_or_repeating(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'paging.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        await _seed_decisions(repository)

        pages: list[list[int]] = []
        cursor: int | None = None
        while True:
            page = await repository.recent_decision_events(2, before_id=cursor)
            if not page:
                break
            pages.append([event["id"] for event in page])
            cursor = page[-1]["id"]
        everything = await repository.recent_decision_events(500)
        await database.close()
        return pages, [event["id"] for event in everything]

    pages, everything = asyncio.run(scenario())

    walked = [identifier for page in pages for identifier in page]
    assert walked == everything
    assert len(walked) == len(set(walked))


def test_paging_and_filtering_compose(tmp_path: Path) -> None:
    """A cursor must stay inside the filtered result, not the whole table."""

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'compose.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        await _seed_decisions(repository)
        first = await repository.recent_decision_events(1, symbol="SOLUSDT")
        second = await repository.recent_decision_events(
            1, symbol="SOLUSDT", before_id=first[0]["id"]
        )
        both = await repository.recent_decision_events(500, symbol="SOLUSDT")
        await database.close()
        return first, second, both

    first, second, both = asyncio.run(scenario())

    assert [event["id"] for event in first + second] == [event["id"] for event in both]
    assert all(event["intent"]["symbol"] == "SOLUSDT" for event in both)


def test_custom_endpoint_is_priced_once_it_declares_who_bills_it(tmp_path: Path) -> None:
    """Custom API calls showed no cost at all, whatever the model.

    The provider name is "openai-compatible:<id>", which no fixed map contained,
    so the catalog was never consulted and every custom endpoint reported "--"
    forever. That also left the run budget guard inert, since it stops on an
    equivalent cost it could never compute.
    """

    catalog = parse_models_dev(
        {"xai": {"models": {"grok-4.5": {"cost": {"input": 2, "output": 6, "cache_read": 0.5}}}}}
    )

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'custom-cost.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "seed")
        await repository.record_inference(
            ProviderResult(
                intent=intent,
                provider="openai-compatible:grok",
                model="grok-4.5",
                duration=timedelta(seconds=28),
                raw_output=intent.model_dump_json(),
                usage={"input_tokens": 1_000_000, "output_tokens": 1_000_000, "total_tokens": 2_000_000},
            )
        )
        declared = await repository.provider_metrics(
            catalog=catalog, provider_ids={"openai-compatible:grok": "xai"}
        )
        undeclared = await repository.provider_metrics(catalog=catalog)
        await database.close()
        return declared, undeclared

    declared, undeclared = asyncio.run(scenario())

    # 1M input at $2/M plus 1M output at $6/M.
    assert declared[0]["cost_usd_total"] == 8.0
    # Without a declared vendor the cost stays unknown rather than guessed.
    assert undeclared[0]["cost_usd_total"] is None
