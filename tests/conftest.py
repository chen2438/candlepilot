import pytest


@pytest.fixture(autouse=True)
def offline_pricing(monkeypatch):
    """Keep the test suite offline: the models.dev pricing loader never fetches.

    Tests that need a real catalog pass an explicit ``pricing_loader`` to
    ``create_app`` instead.
    """

    async def _no_catalog(_cache_dir):
        return None

    monkeypatch.setattr("candlepilot.api.load_pricing_catalog", _no_catalog)
