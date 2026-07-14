from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from candlepilot.api import create_app
from candlepilot.application.engine import TradingEngine
from candlepilot.config import Settings
from candlepilot.domain.models import ProviderHealth, TradeIntent, TradingMode
from candlepilot.market.scanner import MarketCandidateInput
from candlepilot.providers.base import LLMProvider, ProviderResult
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.storage.database import AuditRepository, Database


class ApiProvider(LLMProvider):
    name = "api-fixture"

    async def health_check(self):
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio):
        intent = TradeIntent.hold(snapshot.symbol, snapshot.cadence, "fixture")
        return ProviderResult(intent, self.name, None, timedelta(0), intent.model_dump_json(), {})


class ApiMarket:
    async def candidate_inputs(self):
        return [
            MarketCandidateInput(
                symbol="BTCUSDT",
                quote_volume_24h=Decimal("1000000"),
                bid=Decimal("99.9"),
                ask=Decimal("100.1"),
                volatility=Decimal("0.1"),
                trend_strength=Decimal("0.03"),
                listing_age_days=1000,
            )
        ]

    async def close(self):
        return None


def test_control_api_lifecycle(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        mode=TradingMode.PAPER,
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    application = create_app(
        settings=Settings(),
        database=database,
        market=market,  # type: ignore[arg-type]
        engine=engine,
    )
    with TestClient(application) as client:
        assert client.get("/api/status").json()["running"] is False
        assert client.post("/api/engine/start").status_code == 409
        assert client.post(
            "/api/providers/select", json={"name": "api-fixture"}
        ).status_code == 200
        assert client.post("/api/engine/start").json()["running"] is True
        universe = client.post("/api/universe/refresh").json()
        assert universe[0]["symbol"] == "BTCUSDT"
        stopped = client.post("/api/engine/emergency-stop").json()
        assert stopped["running"] is False
        assert stopped["emergency_locked"] is True


def test_unknown_provider_is_404(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        mode=TradingMode.PAPER,
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.post("/api/providers/select", json={"name": "missing"})
        assert response.status_code == 404
