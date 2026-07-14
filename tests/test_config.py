from candlepilot.config import DEFAULT_DATABASE_URL, Settings


def test_settings_use_concrete_database_default(monkeypatch) -> None:
    monkeypatch.delenv("CANDLEPILOT_DATABASE_URL", raising=False)
    assert Settings.from_env().database_url == DEFAULT_DATABASE_URL
