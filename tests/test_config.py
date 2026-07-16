import os
from pathlib import Path

import pytest

from candlepilot.config import DEFAULT_DATABASE_URL, Settings, load_dotenv


def test_settings_use_concrete_database_default(monkeypatch) -> None:
    monkeypatch.delenv("CANDLEPILOT_DATABASE_URL", raising=False)
    assert Settings.from_env().database_url == DEFAULT_DATABASE_URL


def test_load_dotenv_sets_vars_but_real_env_wins(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# a comment\n"
        "CANDLEPILOT_DOTENV_A=fromfile\n"
        'export CANDLEPILOT_DOTENV_B="quoted"\n'
        "CANDLEPILOT_DOTENV_C=already-set\n"
    )
    monkeypatch.delenv("CANDLEPILOT_DOTENV_A", raising=False)
    monkeypatch.delenv("CANDLEPILOT_DOTENV_B", raising=False)
    monkeypatch.setenv("CANDLEPILOT_DOTENV_C", "fromenv")  # existing wins
    try:
        load_dotenv(env_file)
        assert os.environ["CANDLEPILOT_DOTENV_A"] == "fromfile"
        assert os.environ["CANDLEPILOT_DOTENV_B"] == "quoted"  # export + quotes stripped
        assert os.environ["CANDLEPILOT_DOTENV_C"] == "fromenv"  # not overridden
    finally:
        os.environ.pop("CANDLEPILOT_DOTENV_A", None)
        os.environ.pop("CANDLEPILOT_DOTENV_B", None)


def test_load_dotenv_missing_file_is_noop(tmp_path: Path) -> None:
    load_dotenv(tmp_path / "absent.env")  # must not raise


def test_cadences_default_and_env_override(monkeypatch) -> None:
    monkeypatch.delenv("CANDLEPILOT_CADENCES", raising=False)
    assert Settings.from_env().cadences == ("5m", "15m", "30m")
    monkeypatch.setenv("CANDLEPILOT_CADENCES", "15m, 30m")
    assert Settings.from_env().cadences == ("15m", "30m")


def test_candidates_per_cycle_default_and_env_override(monkeypatch) -> None:
    monkeypatch.delenv("CANDLEPILOT_CANDIDATES_PER_CYCLE", raising=False)
    assert Settings.from_env().candidates_per_cycle == 5
    monkeypatch.setenv("CANDLEPILOT_CANDIDATES_PER_CYCLE", "8")
    assert Settings.from_env().candidates_per_cycle == 8
    monkeypatch.setenv("CANDLEPILOT_CANDIDATES_PER_CYCLE", "not-a-number")
    assert Settings.from_env().candidates_per_cycle == 5


def test_snapshot_age_default_override_and_validation(monkeypatch) -> None:
    monkeypatch.delenv("CANDLEPILOT_MAX_SNAPSHOT_AGE_SECONDS", raising=False)
    assert Settings.from_env().max_snapshot_age_seconds == 75
    monkeypatch.setenv("CANDLEPILOT_MAX_SNAPSHOT_AGE_SECONDS", "20")
    assert Settings.from_env().max_snapshot_age_seconds == 20
    monkeypatch.setenv("CANDLEPILOT_MAX_SNAPSHOT_AGE_SECONDS", "0")
    with pytest.raises(ValueError, match="must be positive"):
        Settings.from_env()
    monkeypatch.setenv("CANDLEPILOT_MAX_SNAPSHOT_AGE_SECONDS", "slow")
    with pytest.raises(ValueError, match="must be an integer"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("codex", "codex-auth"),
        ("codex-auth", "codex-auth"),
        ("claude", "claude-code-auth"),
        ("Claude Code", "claude-code-auth"),
        ("claude-code-auth", "claude-code-auth"),
        ("custom", "openai-compatible"),
        ("custom-api", "openai-compatible"),
        ("openai-compatible", "openai-compatible"),
    ],
)
def test_default_provider_aliases(monkeypatch, configured, expected) -> None:
    monkeypatch.setenv("CANDLEPILOT_DEFAULT_PROVIDER", configured)
    assert Settings.from_env().default_provider == expected


def test_default_provider_is_optional(monkeypatch) -> None:
    monkeypatch.delenv("CANDLEPILOT_DEFAULT_PROVIDER", raising=False)
    assert Settings.from_env().default_provider is None


def test_default_provider_rejects_unknown_value(monkeypatch) -> None:
    monkeypatch.setenv("CANDLEPILOT_DEFAULT_PROVIDER", "openai-api")
    with pytest.raises(ValueError, match="unsupported CANDLEPILOT_DEFAULT_PROVIDER"):
        Settings.from_env()


def test_from_env_reads_loaded_dotenv(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("CANDLEPILOT_PORT=9001\nCANDLEPILOT_CODEX_MODEL=gpt-x\n")
    monkeypatch.delenv("CANDLEPILOT_PORT", raising=False)
    monkeypatch.delenv("CANDLEPILOT_CODEX_MODEL", raising=False)
    try:
        load_dotenv(env_file)
        settings = Settings.from_env()
        assert settings.bind_port == 9001
        assert settings.codex_model == "gpt-x"
    finally:
        os.environ.pop("CANDLEPILOT_PORT", None)
        os.environ.pop("CANDLEPILOT_CODEX_MODEL", None)


def test_testnet_secrets_are_wrapped(monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "key-value")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "secret-value")
    settings = Settings.from_env()
    assert settings.binance_testnet_api_key is not None
    assert settings.binance_testnet_api_key.get_secret_value() == "key-value"
    assert "secret-value" not in repr(settings)


def test_custom_llm_settings_wrap_key_and_read_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("CANDLEPILOT_CUSTOM_LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("CANDLEPILOT_CUSTOM_LLM_API_KEY", "custom-secret")
    monkeypatch.setenv("CANDLEPILOT_CUSTOM_LLM_MODEL", "vendor-model")
    monkeypatch.setenv("CANDLEPILOT_CUSTOM_LLM_REASONING_EFFORT", "high")
    monkeypatch.setenv("CANDLEPILOT_CUSTOM_LLM_WIRE_API", "responses")
    monkeypatch.setenv("CANDLEPILOT_CUSTOM_LLM_REQUIRE_API_KEY", "false")
    monkeypatch.setenv(
        "CANDLEPILOT_CUSTOM_LLM_EXTRA_HEADERS_JSON",
        '{"x-openai-actor-authorization":"header-secret"}',
    )
    settings = Settings.from_env()
    assert settings.custom_llm_base_url == "https://llm.example/v1"
    assert settings.custom_llm_api_key is not None
    assert settings.custom_llm_api_key.get_secret_value() == "custom-secret"
    assert settings.custom_llm_model == "vendor-model"
    assert settings.custom_llm_reasoning_effort == "high"
    assert settings.custom_llm_wire_api == "responses"
    assert settings.custom_llm_require_api_key is False
    assert settings.custom_llm_extra_headers is not None
    assert (
        settings.custom_llm_extra_headers["x-openai-actor-authorization"].get_secret_value()
        == "header-secret"
    )
    assert "custom-secret" not in repr(settings)
    assert "header-secret" not in repr(settings)


@pytest.mark.parametrize("value", ["completions", "response", ""])
def test_custom_llm_settings_reject_invalid_wire_api(monkeypatch, value: str) -> None:
    monkeypatch.setenv("CANDLEPILOT_CUSTOM_LLM_WIRE_API", value)
    if value:
        with pytest.raises(ValueError, match="unsupported.*WIRE_API"):
            Settings.from_env()
    else:
        assert Settings.from_env().custom_llm_wire_api == "chat-completions"


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        "[]",
        '{"Authorization":"secret"}',
        '{"x-provider":"line\\nbreak"}',
    ],
)
def test_custom_llm_settings_reject_unsafe_extra_headers(monkeypatch, value: str) -> None:
    monkeypatch.setenv("CANDLEPILOT_CUSTOM_LLM_EXTRA_HEADERS_JSON", value)
    with pytest.raises(ValueError, match="(?i)headers?"):
        Settings.from_env()
