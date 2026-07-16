import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from decimal import Decimal

from candlepilot.domain.models import ExecutionReport, RiskDecision, TradeIntent
from candlepilot.providers.base import ProviderResult
from candlepilot.storage.database import AuditRepository, Database


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
        await repository.record_risk(
            "BTCUSDT", RiskDecision(accepted=True, reason="within limits")
        )
        await repository.record_risk(
            "ETHUSDT", RiskDecision(accepted=False, reason="missing stop"), inference_id=7
        )
        orders = await repository.recent_executions()
        fills = await repository.recent_executions(status="FILLED")
        risk = await repository.recent_risk_decisions()
        rejections = await repository.recent_risk_decisions(accepted=False)
        await database.close()
        return orders, fills, risk, rejections

    orders, fills, risk, rejections = asyncio.run(scenario())
    assert [item["client_order_id"] for item in orders] == ["cp-2", "cp-1"]
    assert [item["status"] for item in fills] == ["FILLED"]
    assert fills[0]["report"]["average_price"] == "100"
    assert risk[0]["symbol"] == "ETHUSDT" and risk[0]["accepted"] is False
    assert len(rejections) == 1
    assert rejections[0]["reason"] == "missing stop"
    assert rejections[0]["inference_id"] == 7


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
            ProviderResult(opening, "codex-auth", "gpt-test", timedelta(milliseconds=80), "{}", {})
        )
        await repository.record_risk(
            "BTCUSDT",
            RiskDecision(accepted=False, reason="margin limit"),
            inference_id=opening_id,
        )
        hold = TradeIntent.hold("ETHUSDT", "1m", "no setup")
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
        "approved",
        "hold",
        "rejected",
    ]
    assert events[0]["risk"] is None
    assert events[1]["risk"]["decision"]["max_quantity"] == "2.5"
    assert events[2]["intent"]["symbol"] == "ETHUSDT"
    assert events[2]["risk"]["accepted"] is True
    assert events[3]["model"] == "gpt-test"
    assert events[3]["risk"]["reason"] == "margin limit"


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
        {"openai": {"models": {"gpt-5.6-sol": {"cost": {"input": 5, "output": 30, "cache_read": 0.5}}}}}
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


def test_clear_history_is_selective_and_preserves_runtime_state(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'clear.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "seed")
        await repository.record_inference(
            ProviderResult(
                intent=intent,
                provider="codex-auth",
                model="m",
                duration=timedelta(milliseconds=1),
                raw_output=intent.model_dump_json(),
                usage={},
            )
        )
        await repository.record_risk("BTCUSDT", RiskDecision(accepted=True, reason="ok"))
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
    assert len(risk) == 1  # not selected -> untouched
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

    assert asyncio.run(scenario()) == (2, 2)
