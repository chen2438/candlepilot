import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from candlepilot.domain.models import TradeIntent
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
