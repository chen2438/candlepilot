import asyncio
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from candlepilot.domain.models import MarketSnapshot, PortfolioState, TradeAction
from candlepilot.providers.cli import (
    ClaudeCodeAuthProvider,
    CodexAuthProvider,
    ProviderInvocationError,
    find_codex_executable,
    find_claude_executable,
    find_codex_model,
    parse_claude_usage,
    parse_codex_events,
    sanitized_subprocess_env,
    trade_intent_output_schema,
)
from candlepilot.providers import cli as cli_module


def _write_fake_cli(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def test_parse_codex_events_extracts_message_and_usage() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "t"}),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps({"a": 1})},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 11917,
                        "cached_input_tokens": 8960,
                        "output_tokens": 5,
                    },
                }
            ),
        ]
    )
    text, usage = parse_codex_events(stdout)
    assert text == json.dumps({"a": 1})
    assert usage == {
        "input_tokens": 11917,
        "cached_input_tokens": 8960,
        "output_tokens": 5,
        "total_tokens": 11922,
    }


def test_parse_codex_events_is_defensive_on_garbage() -> None:
    text, usage = parse_codex_events("not json\n\n")
    assert text is None
    assert usage == {}


def test_find_codex_model_reads_config(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5.6-sol"\nmodel_reasoning_effort = "medium"\n')
    assert find_codex_model(config) == "gpt-5.6-sol"


def test_find_codex_model_missing_config_returns_none(tmp_path: Path) -> None:
    assert find_codex_model(tmp_path / "absent.toml") is None


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


def test_codex_detection_prefers_current_app_binary(monkeypatch, tmp_path: Path) -> None:
    app_binary = _write_fake_cli(tmp_path / "app" / "codex", "exit 0\n")
    path_binary = _write_fake_cli(tmp_path / "path" / "codex", "exit 0\n")
    monkeypatch.setattr(cli_module, "CODEX_APP_BINARIES", (app_binary,))
    monkeypatch.setenv("PATH", str(path_binary.parent))
    assert find_codex_executable() == app_binary


def test_codex_detection_falls_back_to_path(monkeypatch, tmp_path: Path) -> None:
    executable = _write_fake_cli(tmp_path / "codex", "exit 0\n")
    monkeypatch.setattr(cli_module, "CODEX_APP_BINARIES", ())
    monkeypatch.setattr(cli_module, "USER_CLI_DIRECTORY", tmp_path / "user-bin")
    monkeypatch.setenv("PATH", str(tmp_path))
    detected = find_codex_executable()
    assert detected == executable.resolve()


def test_provider_detection_falls_back_to_user_cli_directory(
    monkeypatch, tmp_path: Path
) -> None:
    user_bin = tmp_path / ".local" / "bin"
    codex = _write_fake_cli(user_bin / "codex", "exit 0\n")
    claude = _write_fake_cli(user_bin / "claude", "exit 0\n")
    monkeypatch.setattr(cli_module, "CODEX_APP_BINARIES", ())
    monkeypatch.setattr(cli_module, "USER_CLI_DIRECTORY", user_bin)
    monkeypatch.setenv("PATH", "")
    assert find_codex_executable() == codex.resolve()
    assert find_claude_executable() == claude.resolve()


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
    jsonl = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps(intent)},
                }
            ),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 1200,
                        "cached_input_tokens": 800,
                        "output_tokens": 50,
                        "reasoning_output_tokens": 10,
                    },
                }
            ),
        ]
    )
    executable = _write_fake_cli(
        tmp_path / "codex", "cat <<'CODEXEOF'\n" + jsonl + "\nCODEXEOF\n"
    )
    config = tmp_path / "config.toml"
    config.write_text('model = "gpt-5.6-sol"\n')
    result = asyncio.run(
        CodexAuthProvider(executable=executable, config_path=config).generate_trade_intent(
            _market(), _portfolio()
        )
    )
    assert result.intent.action == TradeAction.OPEN_LONG
    assert result.intent.risk_fraction == Decimal("0.01")
    assert result.model == "gpt-5.6-sol"
    assert result.usage["input_tokens"] == 1200
    assert result.usage["cached_input_tokens"] == 800
    assert result.usage["total_tokens"] == 1250
    assert result.prompt_version == "trade-intent-v6"
    assert result.data_version is not None
    assert result.data_version.startswith("market-snapshot-v1:sha256:")
    assert result.input_payload is not None
    assert result.input_payload["market"]["symbol"] == "BTCUSDT"
    assert result.prompt is not None and '"symbol":"BTCUSDT"' in result.prompt


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
    assert result.input_payload is not None
    assert result.prompt is not None and '"portfolio"' in result.prompt


def test_claude_provider_truncates_only_oversized_rationale_and_marks_usage(
    tmp_path: Path,
) -> None:
    intent = _minimal_intent()
    intent["rationale"] = "r" * 1_200
    envelope = json.dumps({"result": json.dumps(intent), "duration_ms": 12})
    executable = _write_fake_cli(tmp_path / "claude", f"printf '%s\\n' '{envelope}'\n")

    result = asyncio.run(
        ClaudeCodeAuthProvider(executable=executable).generate_trade_intent(
            _market(), _portfolio()
        )
    )

    assert len(result.intent.rationale) == 1_000
    assert result.usage["rationale_truncated"] is True
    assert "r" * 1_200 in result.raw_output
    assert result.prompt is not None and "at most 800 characters" in result.prompt


def test_claude_validation_failure_preserves_complete_audit_context(
    tmp_path: Path,
) -> None:
    intent = _minimal_intent()
    intent["action"] = "NOT_AN_ACTION"
    envelope = json.dumps(
        {
            "result": json.dumps(intent),
            "duration_ms": 321,
            "usage": {"input_tokens": 25, "output_tokens": 10},
            "modelUsage": {"claude-test": {"outputTokens": 10}},
        }
    )
    executable = _write_fake_cli(tmp_path / "claude", f"printf '%s\\n' '{envelope}'\n")

    with pytest.raises(ProviderInvocationError) as caught:
        asyncio.run(
            ClaudeCodeAuthProvider(executable=executable).generate_trade_intent(
                _market(), _portfolio()
            )
        )

    error = caught.value
    assert error.model == "claude-test"
    assert error.duration.total_seconds() > 0
    assert error.raw_output == envelope + "\n"
    assert error.usage["input_tokens"] == 25
    assert error.prompt_version == "trade-intent-v6"
    assert error.data_version.startswith("market-snapshot-v1:sha256:")
    assert error.input_payload["market"]["symbol"] == "BTCUSDT"
    assert '"portfolio"' in error.prompt


def _minimal_intent() -> dict:
    return {
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


def test_codex_provider_passes_model_and_reasoning_effort(tmp_path: Path) -> None:
    jsonl = "\n".join(
        [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": json.dumps(_minimal_intent())},
                }
            ),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 1}}),
        ]
    )
    body = 'echo "$@" > "$(dirname "$0")/args.txt"\n' + "cat <<'CODEXEOF'\n" + jsonl + "\nCODEXEOF\n"
    executable = _write_fake_cli(tmp_path / "codex", body)
    result = asyncio.run(
        CodexAuthProvider(
            executable=executable, model="gpt-5.2-codex", reasoning_effort="high"
        ).generate_trade_intent(_market(), _portfolio())
    )
    args = (tmp_path / "args.txt").read_text()
    assert "-m gpt-5.2-codex" in args
    assert "model_reasoning_effort=high" in args
    # An explicit model is used for cost attribution without reading config.toml.
    assert result.model == "gpt-5.2-codex"
    assert result.reasoning_effort == "high"


def test_claude_provider_passes_model_and_effort(tmp_path: Path) -> None:
    envelope = json.dumps({"result": json.dumps(_minimal_intent()), "num_turns": 1})
    body = 'echo "$@" > "$(dirname "$0")/args.txt"\n' + f"printf '%s\\n' '{envelope}'\n"
    executable = _write_fake_cli(tmp_path / "claude", body)
    result = asyncio.run(
        ClaudeCodeAuthProvider(
            executable=executable, model="sonnet", reasoning_effort="xhigh"
        ).generate_trade_intent(_market(), _portfolio())
    )
    args = (tmp_path / "args.txt").read_text()
    assert "--model sonnet" in args
    assert "--effort xhigh" in args
    assert result.reasoning_effort == "xhigh"


def test_claude_provider_sends_prompt_on_stdin_with_schema(tmp_path: Path) -> None:
    # The prompt must arrive on stdin, never as a trailing arg: --disallowedTools
    # greedily eats the next positional token and would word-split the prompt into
    # bogus deny rules. Plan mode is avoided (it triggers ExitPlanMode/prose), and
    # the schema is embedded so Claude uses the exact TradeIntent field names.
    envelope = json.dumps({"result": json.dumps(_minimal_intent()), "num_turns": 1})
    body = (
        'echo "$@" > "$(dirname "$0")/args.txt"\n'
        'cat > "$(dirname "$0")/stdin.txt"\n'
        + f"printf '%s\\n' '{envelope}'\n"
    )
    executable = _write_fake_cli(tmp_path / "claude", body)
    asyncio.run(
        ClaudeCodeAuthProvider(executable=executable).generate_trade_intent(_market(), _portfolio())
    )
    args = (tmp_path / "args.txt").read_text()
    stdin = (tmp_path / "stdin.txt").read_text()
    assert "--permission-mode default" in args
    assert "plan" not in args
    assert '"portfolio"' not in args  # prompt is not passed as an argument
    assert '"portfolio"' in stdin  # ...it is on stdin
    assert '"additionalProperties":false' in stdin  # schema is embedded for Claude
    assert "confidence is the estimated strength of an executable edge" in stdin
    assert "HOLD must use leverage=1, risk_fraction=0" in stdin
    assert "Overbought or oversold readings alone" in stdin


def test_registry_builds_one_provider_per_custom_endpoint() -> None:
    from pydantic import SecretStr

    from candlepilot.config import CustomLlmProvider, Settings
    from candlepilot.providers.registry import ProviderRegistry

    registry = ProviderRegistry.from_settings(
        Settings(
            custom_llm_providers=(
                CustomLlmProvider(
                    id="groq",
                    base_url="https://api.groq.example/v1",
                    api_key=SecretStr("groq-key"),
                    model="llama-3.3-70b",
                    wire_api="responses",
                ),
                CustomLlmProvider(
                    id="local",
                    base_url="http://127.0.0.1:1234/v1",
                    model="qwen",
                    require_api_key=False,
                ),
            ),
        )
    )
    groq = registry.get("openai-compatible:groq")
    assert groq.model == "llama-3.3-70b"
    assert groq.wire_api == "responses"
    assert groq.base_url == "https://api.groq.example/v1"
    local = registry.get("openai-compatible:local")
    assert local.require_api_key is False
    assert local.base_url == "http://127.0.0.1:1234/v1"
    # Each endpoint is a distinct instance with its own name.
    assert groq is not local
    assert {"openai-compatible:groq", "openai-compatible:local"} <= set(registry.names)
    # There is no unsuffixed endpoint any more: every custom API is addressed by id.
    assert "openai-compatible" not in registry.names


def test_registry_from_settings_applies_model_and_effort() -> None:
    from candlepilot.config import Settings
    from candlepilot.providers.registry import ProviderRegistry

    registry = ProviderRegistry.from_settings(
        Settings(
            codex_model="gpt-5.2-codex",
            codex_reasoning_effort="high",
            claude_model="opus",
            claude_effort="xhigh",
        )
    )
    assert registry.get("codex-auth").model == "gpt-5.2-codex"
    assert registry.get("codex-auth").reasoning_effort == "high"
    assert registry.get("claude-code-auth").model == "opus"
    assert registry.get("claude-code-auth").reasoning_effort == "xhigh"


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
