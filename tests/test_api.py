import asyncio
from datetime import UTC, datetime, timedelta
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

    async def historical_klines(self, symbol, interval, start, end, *, max_candles=10_000):
        step = {"1m": 60_000, "5m": 300_000, "15m": 900_000}[interval]
        start_ms = int(start.timestamp() * 1000)
        return [
            [start_ms + offset * step, "100", "101", "99", "100", "10"]
            for offset in range(min(2, max_candles))
        ]

    async def historical_funding_rates(self, symbol, start, end, *, max_events=10_000):
        return []


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
        assert client.get("/api/status").json()["market_stream"]["enabled"] is False
        assert client.post("/api/engine/start").status_code == 409
        assert client.post(
            "/api/providers/select", json={"name": "api-fixture"}
        ).status_code == 200
        assert client.post("/api/engine/start").json()["running"] is True
        universe = client.post("/api/universe/refresh").json()
        assert universe[0]["symbol"] == "BTCUSDT"
        refreshed_at = client.get("/api/status").json()["universe_refreshed_at"]
        assert isinstance(refreshed_at, str) and refreshed_at.endswith("+00:00")
        with client.websocket_connect("/ws/events") as socket:
            event = socket.receive_json()
            assert event["type"] == "status"
            assert event["data"]["universe_refreshed_at"] == refreshed_at
        stopped = client.post("/api/engine/emergency-stop").json()
        assert stopped["running"] is False
        assert stopped["emergency_locked"] is True
    asyncio.run(database.close())


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
    asyncio.run(database.close())


def test_backtest_run_is_persisted_and_listed(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'backtest-api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        mode=TradingMode.PAPER,
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    first = datetime(2026, 1, 1, tzinfo=UTC)
    payload = {
        "symbol": "BTCUSDT",
        "cadence": "5m",
        "candles": [
            {
                "timestamp": first.isoformat(),
                "open": "100",
                "high": "101",
                "low": "99",
                "close": "100",
                "volume": "10",
            },
            {
                "timestamp": (first + timedelta(minutes=5)).isoformat(),
                "open": "100",
                "high": "111",
                "low": "99",
                "close": "110",
                "volume": "12",
            },
        ],
        "decisions": [
            {
                "decided_at": first.isoformat(),
                "intent": {
                    "symbol": "BTCUSDT",
                    "cadence": "5m",
                    "action": "OPEN_LONG",
                    "confidence": 0.8,
                    "leverage": 2,
                    "risk_fraction": "0.01",
                    "stop_loss": "95",
                    "take_profit": "108",
                    "rationale": "fixture breakout",
                },
            }
        ],
    }

    with TestClient(app) as client:
        created = client.post("/api/backtests", json=payload)
        assert created.status_code == 201
        run = created.json()
        assert run["id"] == 1
        assert run["symbol"] == "BTCUSDT"
        assert len(run["result"]["trades"]) == 1
        assert run["result"]["total_return"] != "0"

        listed = client.get("/api/backtests").json()
        assert listed == [run]
    asyncio.run(database.close())


def test_cached_replay_rejects_range_without_decisions(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'replay-api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        mode=TradingMode.PAPER,
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(
        settings=Settings(data_dir=tmp_path),
        database=database,
        market=market,  # type: ignore[arg-type]
        engine=engine,
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)

    with TestClient(app) as client:
        response = client.post(
            "/api/backtests/replay",
            json={
                "symbol": "BTCUSDT",
                "cadence": "5m",
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=10)).isoformat(),
            },
        )
        assert response.status_code == 409, response.text
        assert "no cached LLM decisions" in response.json()["detail"]
    asyncio.run(database.close())
