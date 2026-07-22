from pathlib import Path

import pytest

from candlepilot.settings_file import (
    ENV_FIELDS,
    describe_settings,
    mask_secret,
    read_env_file,
    write_env_file,
)


def test_mask_secret_never_reveals_the_whole_value() -> None:
    assert mask_secret("") == ""
    assert mask_secret("abc") == "…bc"  # short values expose only a tail
    assert mask_secret("sk-abcdef1234") == "sk-…1234"
    secret = "gsk_supersecretvalue"
    masked = mask_secret(secret)
    assert secret not in masked
    assert len(masked) < len(secret)


def test_write_env_file_preserves_comments_and_key_order(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# leading comment\n"
        "CANDLEPILOT_MODE=paper-production-data\n"
        "\n"
        "# provider section\n"
        "CANDLEPILOT_PORT=8000\n"
        "BINANCE_TESTNET_API_KEY=old-key\n",
        encoding="utf-8",
    )
    write_env_file(
        path,
        {
            "CANDLEPILOT_PORT": "9001",
            "BINANCE_TESTNET_API_KEY": "new-key",
            "CANDLEPILOT_CADENCES": "5m,15m",  # new key is appended
        },
    )
    text = path.read_text(encoding="utf-8")
    assert "# leading comment" in text
    assert "# provider section" in text
    assert "CANDLEPILOT_MODE=paper-production-data" in text
    assert "CANDLEPILOT_PORT=9001" in text
    assert "BINANCE_TESTNET_API_KEY=new-key" in text
    assert "old-key" not in text
    assert "CANDLEPILOT_CADENCES=5m,15m" in text
    # Untouched keys keep their original position.
    lines = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert lines[0] == "CANDLEPILOT_MODE=paper-production-data"
    assert lines[-1] == "CANDLEPILOT_CADENCES=5m,15m"
    assert read_env_file(path)["CANDLEPILOT_PORT"] == "9001"


def test_written_json_survives_the_real_startup_loader(tmp_path: Path, monkeypatch) -> None:
    # Regression: values must be written exactly as load_dotenv reads them back.
    # Quoting/escaping JSON here would hand {\"a\":1} to the next startup.
    import os

    from candlepilot.config import Settings, load_dotenv

    providers = '[{"id":"groq","base_url":"https://api.groq.example/v1","api_key":"gsk_x"}]'
    path = tmp_path / ".env"
    write_env_file(path, {"CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON": providers})
    assert path.stat().st_mode & 0o777 == 0o600
    assert read_env_file(path)["CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON"] == providers

    monkeypatch.delenv("CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON", raising=False)
    try:
        load_dotenv(path)
        settings = Settings.from_env()
        loaded = settings.custom_llm_providers
        assert [p.id for p in loaded] == ["groq"]
        assert loaded[0].api_key.get_secret_value() == "gsk_x"
        assert loaded[0].base_url == "https://api.groq.example/v1"
    finally:
        os.environ.pop("CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON", None)


def test_write_env_file_creates_missing_file(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    write_env_file(path, {"CANDLEPILOT_PORT": "8123"})
    assert read_env_file(path) == {"CANDLEPILOT_PORT": "8123"}


def test_read_env_file_tolerates_export_quotes_and_comments(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        '# comment\nexport CANDLEPILOT_MODE="backtest"\nCANDLEPILOT_HOST=\'127.0.0.1\'\njunk\n',
        encoding="utf-8",
    )
    values = read_env_file(path)
    assert values["CANDLEPILOT_MODE"] == "backtest"
    assert values["CANDLEPILOT_HOST"] == "127.0.0.1"
    assert "junk" not in values


def test_describe_settings_masks_secrets_and_exposes_plain_values(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    values = {
        "CANDLEPILOT_PORT": "8000",
        "BINANCE_TESTNET_API_KEY": "super-secret-key",
        "CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON": '[{"id":"groq","api_key":"gsk_secret"}]',
    }
    payload = describe_settings(path, values)
    fields = {
        field["key"]: field
        for section in payload["sections"]
        for field in section["fields"]
    }
    plain = fields["CANDLEPILOT_PORT"]
    assert plain["secret"] is False
    assert plain["value"] == "8000"

    secret = fields["BINANCE_TESTNET_API_KEY"]
    assert secret["secret"] is True
    assert secret["configured"] is True
    assert secret["value"] is None  # never returned in full
    assert "super-secret-key" not in str(payload)

    # Custom endpoints are edited through their own form, not this raw JSON key,
    # so the blob never reaches the generic settings payload at all.
    assert "CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON" not in fields
    assert "gsk_secret" not in str(payload)

    unset = fields["CANDLEPILOT_CLAUDE_MODEL"]
    assert unset["configured"] is False

    route = fields["CANDLEPILOT_PROVIDER_CHAIN"]
    assert route["placeholder"] == "codex"
    assert route["description"] == (
        "一次运行只能填写一个：本地规则填 local；实验规则填 local-structure、"
        "local-flow 或 local-structure-flow（正式只影子）；Codex 填 codex，"
        "Claude Code 填 claude-code，自定义端点填 custom:<id>；不得用逗号连接多个。"
    )
    breaker = fields["CANDLEPILOT_DAILY_LOSS_PERCENT"]
    assert breaker["kind"] == "number"
    session_ttl = fields["CANDLEPILOT_AUTH_SESSION_TTL_SECONDS"]
    assert session_ttl["kind"] == "int"
    assert session_ttl["placeholder"] == "604800"
    assert breaker["placeholder"] == "5"
    assert "0.1–50" in breaker["description"]


@pytest.mark.parametrize("key", sorted(ENV_FIELDS))
def test_every_described_field_is_a_real_env_key(key: str) -> None:
    assert key.startswith(("CANDLEPILOT_", "BINANCE_"))
