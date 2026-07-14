import asyncio
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from candlepilot.domain.models import MarketSnapshot, PortfolioState, TradeAction
from candlepilot.providers.cli import (
    ClaudeCodeAuthProvider,
    CodexAuthProvider,
    find_codex_executable,
    parse_claude_usage,
    parse_codex_stderr,
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
            "USER": "trader",
            "LOGNAME": "trader",
            "BINANCE_API_SECRET": "secret",
            "OPENAI_API_KEY": "secret",
            "ANTHROPIC_API_KEY": "secret",
            "UNRELATED_SECRET": "also-secret",
        }
    )
    assert clean["HOME"] == "/tmp/home"
    # USER/LOGNAME are non-secret and required for the macOS Keychain lookup
    # that Claude Code uses to confirm its login.
    assert clean["USER"] == "trader"
    assert clean["LOGNAME"] == "trader"
    assert "BINANCE_API_SECRET" not in clean
    assert "OPENAI_API_KEY" not in clean
    assert "ANTHROPIC_API_KEY" not in clean
    assert "UNRELATED_SECRET" not in clean


def test_parse_codex_stderr_extracts_model_and_tokens() -> None:
    stderr = (
        "workdir: /tmp\n"
        "model: gpt-5.6-sol\n"
        "provider: openai\n"
        "codex\nok\n"
        "tokens used\n6,903\n"
    )
    model, usage = parse_codex_stderr(stderr)
    assert model == "gpt-5.6-sol"
    assert usage == {"total_tokens": 6903}


def test_parse_codex_stderr_is_defensive_when_absent() -> None:
    model, usage = parse_codex_stderr("no telemetry here")
    assert model is None
    assert usage == {}


def test_parse_claude_usage_sums_tokens_and_reads_cost_and_model() -> None:
    envelope = {
        "result": "{}",
        "model": None,
        "total_cost_usd": 0.0732009,
        "duration_ms": 4200,
        "num_turns": 1,
        "usage": {
            "input_tokens": 2,
            "output_tokens": 44,
            "cache_read_input_tokens": 18053,
            "cache_creation_input_tokens": 11088,
        },
        "modelUsage": {
            "claude-haiku-4-5-20251001": {"outputTokens": 14},
            "claude-sonnet-5": {"outputTokens": 44},
        },
    }
    model, usage = parse_claude_usage(envelope)
    assert model == "claude-sonnet-5"  # dominant model by output tokens
    assert usage["input_tokens"] == 2
    assert usage["output_tokens"] == 44
    assert usage["total_tokens"] == 2 + 44 + 18053 + 11088
    assert usage["cost_usd"] == 0.0732009
    assert usage["num_turns"] == 1


def test_parse_claude_usage_tolerates_missing_usage() -> None:
    model, usage = parse_claude_usage({"result": "{}"})
    assert model is None
    assert usage["total_tokens"] == 0
    assert "cost_usd" not in usage


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
    assert result.prompt_version == "trade-intent-v1"
    assert result.data_version is not None
    assert result.data_version.startswith("market-snapshot-v1:sha256:")


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


def test_cli_providers_declare_subscription_capabilities(tmp_path: Path) -> None:
    executable = _write_fake_cli(tmp_path / "provider", "exit 0\n")

    for provider in (
        CodexAuthProvider(executable=executable),
        ClaudeCodeAuthProvider(executable=executable),
    ):
        assert provider.capabilities.subscription_auth
        assert provider.capabilities.structured_output
        assert provider.capabilities.tools_disabled
        assert provider.capabilities.cancellable
        assert provider.capabilities.max_concurrency == 1


def test_cancel_terminates_active_cli_process(tmp_path: Path) -> None:
    executable = _write_fake_cli(tmp_path / "codex", "sleep 30\n")

    async def scenario():
        provider = CodexAuthProvider(executable=executable, timeout=40)
        task = asyncio.create_task(provider.generate_trade_intent(_market(), _portfolio()))
        await asyncio.sleep(0.1)
        started = time.monotonic()
        cancelled = await provider.cancel()
        return cancelled, task.cancelled(), time.monotonic() - started

    cancelled, task_cancelled, duration = asyncio.run(scenario())
    assert cancelled
    assert task_cancelled
    assert duration < 3
