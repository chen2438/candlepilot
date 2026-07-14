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
    assert replay_rows[0]["intent"].symbol == "BTCUSDT"
