import os
from pathlib import Path

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
