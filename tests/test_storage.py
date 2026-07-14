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
                usage={"input_tokens": 10},
                prompt_version="trade-intent-v1",
                data_version="market-snapshot-v1:sha256:test",
                provider_version="codex-cli 1.0",
            )
        )
        rows = await repository.recent_intents()
        replay_rows = await repository.intents_between(
            "BTCUSDT",
            "5m",
            datetime.now(UTC) - timedelta(minutes=1),
            datetime.now(UTC) + timedelta(minutes=1),
        )
        await database.close()
        return identifier, rows, replay_rows

    identifier, rows, replay_rows = asyncio.run(scenario())
    assert identifier == 1
    assert rows[0]["provider"] == "codex-auth"
    assert rows[0]["intent"]["symbol"] == "BTCUSDT"
    assert rows[0]["duration_ms"] == 123
    assert rows[0]["provenance"]["prompt_version"] == "trade-intent-v1"
    assert replay_rows[0]["model"] == "test-model"
    assert replay_rows[0]["intent"].symbol == "BTCUSDT"


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


def test_database_migrations_are_versioned_and_idempotent(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'migrations.db'}")
        await database.initialize()
        first = await database.schema_version()
        await database.initialize()
        second = await database.schema_version()
        await database.close()
        return first, second

    assert asyncio.run(scenario()) == (1, 1)
