import os

import pytest


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Detach every test from the developer's own environment and ``.env``.

    ``cli.serve`` calls ``load_dotenv()`` with no argument, which reads the repo
    root ``.env`` into ``os.environ`` — so without this the suite silently
    depends on whatever the machine happens to have configured.
    """

    for key in list(os.environ):
        if key.startswith(("CANDLEPILOT_", "BINANCE_")):
            monkeypatch.delenv(key, raising=False)
    # load_dotenv() would otherwise fall back to the repo root .env.
    monkeypatch.setenv("CANDLEPILOT_ENV_FILE", str(tmp_path / "absent.env"))


@pytest.fixture(autouse=True)
def offline_pricing(monkeypatch):
    """Keep the test suite offline: the models.dev pricing loader never fetches.

    Tests that need a real catalog pass an explicit ``pricing_loader`` to
    ``create_app`` instead.
    """

    async def _no_catalog(_cache_dir):
        return None

    monkeypatch.setattr("candlepilot.api.load_pricing_catalog", _no_catalog)
