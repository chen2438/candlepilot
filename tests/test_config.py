from candlepilot.config import DEFAULT_DATABASE_URL, Settings


def test_settings_use_concrete_database_default(monkeypatch) -> None:
    monkeypatch.delenv("CANDLEPILOT_DATABASE_URL", raising=False)
    assert Settings.from_env().database_url == DEFAULT_DATABASE_URL


def test_testnet_secrets_are_wrapped(monkeypatch) -> None:
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "key-value")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "secret-value")
    settings = Settings.from_env()
    assert settings.binance_testnet_api_key is not None
    assert settings.binance_testnet_api_key.get_secret_value() == "key-value"
    assert "secret-value" not in repr(settings)
