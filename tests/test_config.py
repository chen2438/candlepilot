import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from candlepilot.config import DEFAULT_DATABASE_URL, Settings, load_dotenv
from candlepilot.auth import hash_password
from pydantic import SecretStr


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
    assert Settings.from_env().cadences == ("15m",)
    monkeypatch.setenv("CANDLEPILOT_CADENCES", "30m")
    assert Settings.from_env().cadences == ("30m",)


def test_the_parser_rejects_a_cadence_the_engine_would_refuse(monkeypatch) -> None:
    """The parser handed any string through and let the engine object later.

    A typo in .env was accepted here, survived Settings, and only surfaced as
    `unsupported cadences` when TradingEngine was constructed three layers
    away. The value is read here, so it is checked here.
    """

    for raw in ("1m", "garbage", "5m,1m"):
        monkeypatch.setenv("CANDLEPILOT_CADENCES", raw)
        with pytest.raises(ValueError, match="unsupported cadences"):
            Settings.from_env()


def test_cadences_reject_multiple_values(monkeypatch) -> None:
    monkeypatch.setenv("CANDLEPILOT_CADENCES", "4h,30m")
    with pytest.raises(ValueError, match="exactly one analysis cadence"):
        Settings.from_env()
    # Blank and whitespace-only both mean the 15m default.
    monkeypatch.setenv("CANDLEPILOT_CADENCES", "   ")
    assert Settings.from_env().cadences == ("15m",)


def test_trailing_stop_mode_defaults_to_shadow_and_rejects_unknown_values(
    monkeypatch,
) -> None:
    assert Settings.from_env().trailing_stop_mode == "shadow"
    monkeypatch.setenv("CANDLEPILOT_TRAILING_STOP_MODE", "LIVE")
    assert Settings.from_env().trailing_stop_mode == "live"
    monkeypatch.setenv("CANDLEPILOT_TRAILING_STOP_MODE", "automatic")
    with pytest.raises(ValueError, match="must be off, shadow, or live"):
        Settings.from_env()


def test_structure_gate_mode_defaults_to_shadow_and_rejects_unknown_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CANDLEPILOT_STRUCTURE_GATE_MODE", raising=False)
    assert Settings.from_env().structure_gate_mode == "shadow"
    monkeypatch.setenv("CANDLEPILOT_STRUCTURE_GATE_MODE", "ENFORCE")
    assert Settings.from_env().structure_gate_mode == "enforce"
    monkeypatch.setenv("CANDLEPILOT_STRUCTURE_GATE_MODE", "automatic")
    with pytest.raises(ValueError, match="must be off, shadow, or enforce"):
        Settings.from_env()


def test_custom_llm_providers_parse_from_json(monkeypatch) -> None:
    import json

    monkeypatch.setenv(
        "CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON",
        json.dumps(
            [
                {
                    "id": "groq",
                    "base_url": "https://api.groq.example/v1",
                    "api_key": "gk",
                    "model": "llama-3.3-70b",
                    "wire_api": "responses",
                    "extra_headers": {"x-team": "desk"},
                },
                {"id": "local", "base_url": "http://127.0.0.1:1234/v1", "require_api_key": False},
            ]
        ),
    )
    providers = Settings.from_env().custom_llm_providers
    assert [p.id for p in providers] == ["groq", "local"]
    assert providers[0].provider_name == "openai-compatible:groq"
    assert providers[0].wire_api == "responses"
    assert providers[0].api_key.get_secret_value() == "gk"
    assert providers[0].extra_headers["x-team"].get_secret_value() == "desk"
    assert providers[1].require_api_key is False
    assert providers[1].api_key is None
    # Secrets must not leak through repr.
    assert "gk" not in repr(providers)


def test_custom_llm_providers_reject_bad_definitions(monkeypatch) -> None:
    import json

    import pytest

    bad_cases = [
        '{"id": "a"}',  # not a list
        json.dumps([{"id": "groq"}]),  # missing base_url
        json.dumps([{"base_url": "https://x/v1"}]),  # missing id
        json.dumps([{"id": "Groq", "base_url": "https://x/v1"}]),  # uppercase id
        json.dumps([{"id": "a", "base_url": "https://x/v1", "typo": 1}]),  # unknown key
        json.dumps([{"id": "a", "base_url": "https://x/v1", "wire_api": "grpc"}]),
        json.dumps([{"id": "a", "base_url": "https://x/v1", "require_api_key": "yes"}]),
        json.dumps(
            [
                {"id": "dup", "base_url": "https://x/v1"},
                {"id": "dup", "base_url": "https://y/v1"},
            ]
        ),  # duplicate id
        json.dumps(
            [{"id": "a", "base_url": "https://x/v1", "extra_headers": {"Authorization": "x"}}]
        ),  # protected header
        "not json",
    ]
    for raw in bad_cases:
        monkeypatch.setenv("CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON", raw)
        with pytest.raises(ValueError):
            Settings.from_env()

    monkeypatch.setenv(
        "CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON",
        json.dumps([{"id": f"p{i}", "base_url": "https://x/v1"} for i in range(9)]),
    )
    with pytest.raises(ValueError, match="at most"):
        Settings.from_env()

    monkeypatch.delenv("CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON")
    assert Settings.from_env().custom_llm_providers == ()


def test_provider_chain_accepts_custom_endpoint_ids(monkeypatch) -> None:
    monkeypatch.setenv(
        "CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON",
        json.dumps(
            [
                {"id": "groq", "base_url": "https://groq.example/v1"},
                {"id": "local", "base_url": "http://127.0.0.1:1234/v1"},
            ]
        ),
    )
    monkeypatch.setenv("CANDLEPILOT_PROVIDER_CHAIN", "codex, custom:groq, custom:local")
    assert Settings.from_env().provider_chain == (
        "codex-auth",
        "openai-compatible:groq",
        "openai-compatible:local",
    )
    # Ids are matched case-insensitively.
    monkeypatch.setenv("CANDLEPILOT_PROVIDER_CHAIN", "custom:GROQ")
    assert Settings.from_env().provider_chain == ("openai-compatible:groq",)

    for bad in ("custom:bad_id", "custom:", "custom:-x", "custom:a b"):
        monkeypatch.setenv("CANDLEPILOT_PROVIDER_CHAIN", bad)
        with pytest.raises(ValueError):
            Settings.from_env()

    for internal_name in ("openai-compatible:groq", "codex-auth"):
        monkeypatch.setenv("CANDLEPILOT_PROVIDER_CHAIN", internal_name)
        with pytest.raises(ValueError, match=r"custom:<id>"):
            Settings.from_env()
    monkeypatch.delenv("CANDLEPILOT_PROVIDER_CHAIN")


def test_local_rule_is_a_first_class_provider_name(monkeypatch) -> None:
    monkeypatch.setenv("CANDLEPILOT_PROVIDER_CHAIN", "local, codex")

    settings = Settings.from_env()

    assert settings.provider_chain == ("local-rule", "codex-auth")


def test_run_limits_default_to_unbounded_and_read_env(monkeypatch) -> None:
    monkeypatch.delenv("CANDLEPILOT_MAX_RUN_SECONDS", raising=False)
    monkeypatch.delenv("CANDLEPILOT_MAX_RUN_COST_USD", raising=False)
    settings = Settings.from_env()
    assert settings.max_run_seconds is None
    assert settings.max_run_cost_usd is None

    monkeypatch.setenv("CANDLEPILOT_MAX_RUN_SECONDS", "7200")
    monkeypatch.setenv("CANDLEPILOT_MAX_RUN_COST_USD", "3.5")
    settings = Settings.from_env()
    assert settings.max_run_seconds == 7200
    assert settings.max_run_cost_usd == 3.5

    monkeypatch.setenv("CANDLEPILOT_MAX_RUN_SECONDS", "")
    assert Settings.from_env().max_run_seconds is None

    for bad in ("0", "-5", "abc"):
        monkeypatch.setenv("CANDLEPILOT_MAX_RUN_SECONDS", bad)
        with pytest.raises(ValueError, match="CANDLEPILOT_MAX_RUN_SECONDS"):
            Settings.from_env()
    for bad in ("0", "-5", "abc", "nan", "inf"):
        monkeypatch.setenv("CANDLEPILOT_MAX_RUN_SECONDS", "")
        monkeypatch.setenv("CANDLEPILOT_MAX_RUN_COST_USD", bad)
        with pytest.raises(ValueError, match="CANDLEPILOT_MAX_RUN_COST_USD"):
            Settings.from_env()


def test_candidates_per_cycle_default_and_env_override(monkeypatch) -> None:
    monkeypatch.delenv("CANDLEPILOT_CANDIDATES_PER_CYCLE", raising=False)
    assert Settings.from_env().candidates_per_cycle == 5
    monkeypatch.setenv("CANDLEPILOT_CANDIDATES_PER_CYCLE", "8")
    assert Settings.from_env().candidates_per_cycle == 8
    for bad in ("not-a-number", "0", "21"):
        monkeypatch.setenv("CANDLEPILOT_CANDIDATES_PER_CYCLE", bad)
        with pytest.raises(ValueError, match="CANDLEPILOT_CANDIDATES_PER_CYCLE"):
            Settings.from_env()


def test_port_and_inference_timeout_reject_invalid_values() -> None:
    for raw in ("not-a-port", "0", "65536"):
        with pytest.raises(ValueError, match="CANDLEPILOT_PORT"):
            Settings.from_mapping({"CANDLEPILOT_PORT": raw})
    for raw in ("slow", "0", "-1", "nan", "inf"):
        with pytest.raises(ValueError, match="CANDLEPILOT_LLM_TIMEOUT"):
            Settings.from_mapping({"CANDLEPILOT_LLM_TIMEOUT": raw})


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


def test_daily_loss_percent_defaults_converts_and_rejects_unsafe_values() -> None:
    assert Settings.from_mapping({}).daily_loss_fraction == Decimal("0.05")
    assert Settings.from_mapping(
        {"CANDLEPILOT_DAILY_LOSS_PERCENT": "   "}
    ).daily_loss_fraction == Decimal("0.05")
    assert Settings.from_mapping(
        {"CANDLEPILOT_DAILY_LOSS_PERCENT": "7.5"}
    ).daily_loss_fraction == Decimal("0.075")
    for raw in ("0", "0.09", "50.1", "nan", "inf", "not-a-number"):
        with pytest.raises(ValueError, match="CANDLEPILOT_DAILY_LOSS_PERCENT"):
            Settings.from_mapping({"CANDLEPILOT_DAILY_LOSS_PERCENT": raw})


def test_authentication_configuration_is_complete_or_rejected() -> None:
    password_hash = hash_password("correct horse battery staple")
    settings = Settings.from_mapping(
        {
            "CANDLEPILOT_AUTH_ENABLED": "true",
            "CANDLEPILOT_AUTH_USERNAME": "operator",
            "CANDLEPILOT_AUTH_PASSWORD_HASH": password_hash,
            "CANDLEPILOT_AUTH_SESSION_SECRET": "x" * 32,
            "CANDLEPILOT_AUTH_COOKIE_SECURE": "true",
            "CANDLEPILOT_AUTH_SESSION_TTL_SECONDS": "3600",
        }
    )
    assert settings.auth_enabled is True
    assert settings.auth_username == "operator"
    assert isinstance(settings.auth_password_hash, SecretStr)
    assert settings.auth_cookie_secure is True
    assert settings.auth_session_ttl_seconds == 3600

    incomplete = {
        "CANDLEPILOT_AUTH_ENABLED": "true",
        "CANDLEPILOT_AUTH_USERNAME": "operator",
        "CANDLEPILOT_AUTH_PASSWORD_HASH": password_hash,
    }
    with pytest.raises(ValueError, match="SESSION_SECRET"):
        Settings.from_mapping(incomplete)
    with pytest.raises(ValueError, match="supported scrypt"):
        Settings.from_mapping({**incomplete, "CANDLEPILOT_AUTH_SESSION_SECRET": "x" * 32, "CANDLEPILOT_AUTH_PASSWORD_HASH": "plain-text"})


def test_removed_default_provider_env_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("CANDLEPILOT_DEFAULT_PROVIDER", "codex")
    with pytest.raises(ValueError, match="CANDLEPILOT_PROVIDER_CHAIN"):
        Settings.from_env()


def test_provider_chain_accepts_current_names_and_preserves_order(monkeypatch) -> None:
    monkeypatch.setenv(
        "CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON",
        '[{"id":"main","base_url":"https://main.example/v1"}]',
    )
    monkeypatch.setenv(
        "CANDLEPILOT_PROVIDER_CHAIN", "codex,claude-code,custom:main"
    )
    assert Settings.from_env().provider_chain == (
        "codex-auth",
        "claude-code-auth",
        "openai-compatible:main",
    )


@pytest.mark.parametrize(
    "retired",
    (
        "local-rule",
        "codex-auth",
        "claude",
        "claude code",
        "claude-code-auth",
        "custom-api:main",
        "openai-compatible:main",
    ),
)
def test_provider_chain_rejects_retired_aliases(monkeypatch, retired) -> None:
    monkeypatch.setenv("CANDLEPILOT_PROVIDER_CHAIN", retired)
    with pytest.raises(ValueError, match="unsupported provider"):
        Settings.from_env()


def test_provider_chain_rejects_duplicates(monkeypatch) -> None:
    monkeypatch.setenv("CANDLEPILOT_PROVIDER_CHAIN", "codex,codex")
    with pytest.raises(ValueError, match="duplicates"):
        Settings.from_env()


def test_provider_references_must_exist_in_the_same_candidate() -> None:
    custom = '[{"id":"main","base_url":"https://main.example/v1"}]'

    valid = Settings.from_mapping(
        {
            "CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON": custom,
            "CANDLEPILOT_PROVIDER_CHAIN": "custom:main,codex",
        }
    )
    assert valid.provider_chain == ("openai-compatible:main", "codex-auth")

    with pytest.raises(ValueError) as error:
        Settings.from_mapping(
            {
                "CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON": custom,
                "CANDLEPILOT_PROVIDER_CHAIN": "custom:removed,codex",
            }
        )
    message = str(error.value)
    assert "CANDLEPILOT_PROVIDER_CHAIN" in message
    assert "openai-compatible:removed" in message


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


def test_legacy_single_endpoint_env_is_rejected(monkeypatch) -> None:
    # The flat CANDLEPILOT_CUSTOM_LLM_* configuration was removed. Ignoring it
    # would silently drop a provider the user still expects, so it must fail.
    monkeypatch.setenv("CANDLEPILOT_CUSTOM_LLM_BASE_URL", "https://llm.example/v1")
    with pytest.raises(ValueError, match="were removed"):
        Settings.from_env()
    monkeypatch.delenv("CANDLEPILOT_CUSTOM_LLM_BASE_URL")

    monkeypatch.setenv("CANDLEPILOT_CUSTOM_LLM_API_KEY", "k")
    with pytest.raises(ValueError, match="CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON"):
        Settings.from_env()

    # An empty legacy value is inert and must not block startup.
    monkeypatch.setenv("CANDLEPILOT_CUSTOM_LLM_API_KEY", "")
    assert Settings.from_env().custom_llm_providers == ()


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


def test_removed_runtime_mode_is_rejected_rather_than_ignored() -> None:
    """A stale CANDLEPILOT_MODE must not read as "still simulated".

    Every order now goes to the exchange. Silently ignoring the key would let
    someone keep believing they configured a simulated account.
    """

    with pytest.raises(ValueError, match="CANDLEPILOT_MODE was removed"):
        Settings.from_mapping({"CANDLEPILOT_MODE": "paper-production-data"})

    # An empty value is just leftover formatting, not a belief about the mode.
    assert Settings.from_mapping({"CANDLEPILOT_MODE": ""}) is not None
