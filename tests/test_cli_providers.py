import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from candlepilot.domain.models import MarketSnapshot, PortfolioState, TradeAction
from candlepilot.providers.cli import (
    ClaudeCodeAuthProvider,
    CodexAuthProvider,
    find_codex_executable,
    sanitized_subprocess_env,
    trade_intent_output_schema,
)


def _write_fake_cli(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _market() -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTCUSDT",
        cadence="5m",
        timestamp=datetime.now(UTC),
        mark_price="100",
        bid="99.9",
        ask="100.1",
        quote_volume_24h="1000000",
    )


def _portfolio() -> PortfolioState:
    return PortfolioState(equity="10000", available_balance="8000")


def test_sensitive_environment_is_removed() -> None:
    clean = sanitized_subprocess_env(
        {
            "HOME": "/tmp/home",
            "PATH": "/usr/bin",
            "BINANCE_API_SECRET": "secret",
            "OPENAI_API_KEY": "secret",
            "ANTHROPIC_API_KEY": "secret",
            "UNRELATED_SECRET": "also-secret",
        }
    )
    assert clean["HOME"] == "/tmp/home"
    assert "BINANCE_API_SECRET" not in clean
    assert "OPENAI_API_KEY" not in clean
    assert "ANTHROPIC_API_KEY" not in clean
    assert "UNRELATED_SECRET" not in clean


def test_codex_output_schema_requires_every_property() -> None:
    schema = trade_intent_output_schema()
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False
    assert '"default"' not in json.dumps(schema)
    assert '"pattern"' not in json.dumps(schema)


def test_codex_detection_falls_back_to_path(monkeypatch, tmp_path: Path) -> None:
    executable = _write_fake_cli(tmp_path / "codex", "exit 0\n")
    monkeypatch.setenv("PATH", str(tmp_path))
    detected = find_codex_executable()
    assert detected == executable.resolve()


def test_codex_provider_parses_schema_output(tmp_path: Path) -> None:
    intent = {
        "symbol": "BTCUSDT",
        "cadence": "5m",
        "action": "OPEN_LONG",
        "confidence": 0.75,
        "leverage": 3,
        "risk_fraction": "0.01",
        "order_type": "MARKET",
        "entry_price": None,
        "stop_loss": "98",
        "take_profit": "104",
        "ttl_seconds": 60,
        "rationale": "trend confirmation",
    }
    executable = _write_fake_cli(
        tmp_path / "codex", f"printf '%s\\n' '{json.dumps(intent)}'\n"
    )
    result = asyncio.run(
        CodexAuthProvider(executable=executable).generate_trade_intent(_market(), _portfolio())
    )
    assert result.intent.action == TradeAction.OPEN_LONG
    assert result.intent.risk_fraction == Decimal("0.01")


def test_claude_provider_unwraps_result(tmp_path: Path) -> None:
    intent = {
        "symbol": "BTCUSDT",
        "cadence": "5m",
        "action": "HOLD",
        "confidence": 0,
        "leverage": 1,
        "risk_fraction": "0",
        "order_type": "MARKET",
        "entry_price": None,
        "stop_loss": None,
        "take_profit": None,
        "ttl_seconds": 60,
        "rationale": "no edge",
    }
    envelope = json.dumps({"result": json.dumps(intent), "duration_ms": 12, "num_turns": 1})
    executable = _write_fake_cli(tmp_path / "claude", f"printf '%s\\n' '{envelope}'\n")
    result = asyncio.run(
        ClaudeCodeAuthProvider(executable=executable).generate_trade_intent(_market(), _portfolio())
    )
    assert result.intent.action == TradeAction.HOLD
    assert result.usage["num_turns"] == 1
