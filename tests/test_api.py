import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from candlepilot.api import create_app
from candlepilot.application.engine import TradingEngine
from conftest import FakeTestnetBroker, StatefulTestnetBroker
from candlepilot.broker.binance_testnet import ProtectiveLevels, ReconciliationReport
from candlepilot.config import Settings
from candlepilot.domain.models import (
    ProviderHealth,
    TradeIntent,
)
from candlepilot.market.scanner import MarketCandidateInput
from candlepilot.providers.base import LLMProvider, ProviderResult
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.settings_file import read_env_file
from candlepilot.storage.database import (
    CURRENT_SCHEMA_VERSION,
    AuditRepository,
    Database,
)


class ApiProvider(LLMProvider):
    name = "api-fixture"

    async def health_check(self):
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio):
        intent = TradeIntent.hold(snapshot.symbol, snapshot.cadence, "fixture")
        return ProviderResult(intent, self.name, None, timedelta(0), intent.model_dump_json(), {})


class ConfigurableProvider(ApiProvider):
    name = "api-fixture"
    reasoning_effort_options = ("low", "medium", "high")


class BrokenProvider(LLMProvider):
    name = "broken-fixture"

    async def health_check(self):
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio):
        raise RuntimeError("model 'bogus' is not available")


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
        step = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000}[interval]
        start_ms = int(start.timestamp() * 1000)
        return [
            [start_ms + offset * step, "100", "101", "99", "100", "10"]
            for offset in range(min(2, max_candles))
        ]

    async def historical_funding_rates(self, symbol, start, end, *, max_events=10_000):
        return []


class LLMReplayMarket(ApiMarket):
    async def historical_klines(self, symbol, interval, start, end, *, max_candles=10_000):
        step = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000}[interval]
        start_ms = int(start.timestamp() * 1000)
        return [
            [
                start_ms + index * step,
                str(100 + index),
                str(102 + index),
                str(99 + index),
                str(101 + index),
                "10",
            ]
            for index in range(21)
        ]


class ApiTestnetBroker:
    def __init__(self) -> None:
        self.account_calls = 0
        self.level_calls = 0

    async def protective_levels(self):
        self.level_calls += 1
        return {
            "BTCUSDT": ProtectiveLevels(
                stop_loss=Decimal("58000"), take_profit=Decimal("63000")
            )
        }

    async def account(self):
        self.account_calls += 1
        # Mirrors the real /fapi/v3/account futures response, which has no
        # canTrade field; margin readiness is derived from availableBalance.
        return {
            "totalWalletBalance": "10000.5",
            "totalMarginBalance": "10025.5",
            "availableBalance": "9000",
            "totalUnrealizedProfit": "25",
            "totalInitialMargin": "1000",
            "positions": [
                {
                    "symbol": "BTCUSDT",
                    "positionAmt": "0.25",
                    "entryPrice": "60000",
                    "markPrice": "60100",
                    "unrealizedProfit": "25",
                    "leverage": "3",
                    "isolated": True,
                    "positionInitialMargin": "1000",
                },
                {"symbol": "ETHUSDT", "positionAmt": "0"},
            ],
        }


def test_control_api_lifecycle(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
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
        assert client.get("/api/health/live").json()["status"] == "alive"
        readiness = client.get("/api/health/ready")
        assert readiness.status_code == 200
        assert readiness.json()["checks"]["database"] == {
            "ready": True,
            "schema_version": CURRENT_SCHEMA_VERSION,
            "expected_schema_version": CURRENT_SCHEMA_VERSION,
        }
        assert "testnet_broker" not in readiness.json()["checks"]
        runtime_metrics = client.get("/api/metrics/runtime")
        assert runtime_metrics.status_code == 200
        assert int(runtime_metrics.headers["X-Request-ID"], 16) >= 0
        assert runtime_metrics.json()["requests_total"] >= 2
        assert runtime_metrics.json()["in_flight"] == 1
        assert client.get("/api/alerts").json()["active_count"] == 0
        assert client.get("/api/status").json()["running"] is False
        assert client.get("/api/status").json()["user_stream"]["enabled"] is False
        assert client.get("/api/testnet/events").json() == []
        assert client.get("/api/decision-events").json() == []
        assert client.get("/api/decision-events?limit=0").status_code == 422
        # The broker is no longer optional, so the account is always reachable.
        assert client.get("/api/testnet/account-status").json()["enabled"] is True
        assert client.get("/api/metrics/providers").json() == {
            "window_hours": 24,
            "pricing_source": None,
            "providers": [],
        }
        assert client.get("/api/metrics/run-session").json()["state"] == "none"
        assert client.get("/api/metrics/providers?hours=0").status_code == 422
        assert client.post("/api/engine/start").status_code == 409
        assert client.post("/api/providers/select", json={"name": "api-fixture"}).status_code == 200
        assert client.post("/api/engine/start").json()["running"] is True
        universe = client.post("/api/universe/refresh").json()
        assert universe[0]["symbol"] == "BTCUSDT"
        refreshed_at = client.get("/api/status").json()["universe_refreshed_at"]
        assert isinstance(refreshed_at, str) and refreshed_at.endswith("+00:00")
        asyncio.run(
            engine.audit.record_inference(
                ProviderResult(
                    TradeIntent.hold("BTCUSDT", "5m", "websocket fixture"),
                    "api-fixture",
                    "test-model",
                    timedelta(milliseconds=1),
                    "{}",
                    {
                        "input_tokens": 120,
                        "cached_input_tokens": 20,
                        "output_tokens": 30,
                        "total_tokens": 150,
                        "cost_usd": 0.004,
                    },
                )
            )
        )
        running_usage = client.get("/api/metrics/run-session").json()
        assert running_usage["state"] == "running"
        assert running_usage["call_count"] == 1
        assert running_usage["total_tokens"] == 150
        assert running_usage["equivalent_cost_usd"] == 0.004
        assert running_usage["average_duration_ms"] == 1
        assert running_usage["average_tokens"] == 150
        assert running_usage["average_cost_usd"] == 0.004
        with client.websocket_connect("/ws/events") as socket:
            event = socket.receive_json()
            assert event["type"] == "status"
            assert event["data"]["universe_refreshed_at"] == refreshed_at
            decision_event = socket.receive_json()
            assert decision_event["type"] == "decisions"
            assert decision_event["data"][0]["intent"]["rationale"] == "websocket fixture"
            assert decision_event["data"][0]["created_at"].endswith("+00:00")
        stopped = client.post("/api/engine/emergency-stop").json()
        assert stopped["running"] is False
        assert stopped["emergency_locked"] is True
        completed_usage = client.get("/api/metrics/run-session").json()
        assert completed_usage["state"] == "completed"
        assert completed_usage["total_tokens"] == 150
        assert completed_usage["average_duration_ms"] == 1
        assert completed_usage["average_tokens"] == 150
        assert completed_usage["average_cost_usd"] == 0.004

        # Inferences created after the stop boundary cannot change the last run.
        asyncio.run(
            engine.audit.record_inference(
                ProviderResult(
                    TradeIntent.hold("ETHUSDT", "5m", "outside session"),
                    "api-fixture",
                    "test-model",
                    timedelta(milliseconds=1),
                    "{}",
                    {"total_tokens": 999, "cost_usd": 1},
                )
            )
        )
        assert client.get("/api/metrics/run-session").json()["total_tokens"] == 150
    asyncio.run(database.close())


def test_default_provider_is_selected_from_settings(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'default-provider.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    application = create_app(
        settings=Settings(default_provider="api-fixture"),
        database=database,
        market=market,  # type: ignore[arg-type]
        engine=engine,
    )

    assert application.state.engine.selected_provider == "api-fixture"
    asyncio.run(database.close())


def test_custom_provider_status_never_returns_endpoint_or_key(tmp_path: Path) -> None:
    from candlepilot.config import CustomLlmProvider

    settings = Settings(
        custom_llm_providers=(
            CustomLlmProvider(
                id="private",
                base_url="https://private.example/v1",
                api_key=SecretStr("private-api-key"),
                model="vendor-model",
            ),
        )
    )
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'custom-provider.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry.from_settings(settings),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )

    async def no_pricing(_path):
        return None

    application = create_app(
        settings=settings,
        database=database,
        market=market,  # type: ignore[arg-type]
        engine=engine,
        pricing_loader=no_pricing,
    )
    with TestClient(application) as client:
        response = client.get("/api/providers")
        assert response.status_code == 200
        rendered = response.text
        custom = next(
            item for item in response.json() if item["provider"] == "openai-compatible:private"
        )
        assert custom["available"] is True
        assert custom["authenticated"] is True
        assert custom["model"] == "vendor-model"
        assert "private-api-key" not in rendered
        assert "private.example" not in rendered
    asyncio.run(database.close())


def test_application_wires_snapshot_age_into_risk_policy(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'risk-settings-api.db'}")
    market = ApiMarket()
    application = create_app(
        settings=Settings(
            max_snapshot_age_seconds=22,
            binance_testnet_api_key=SecretStr("k"),
            binance_testnet_api_secret=SecretStr("s"),
        ),
        database=database,
        market=market,  # type: ignore[arg-type]
    )

    assert application.state.engine.risk.max_snapshot_age_seconds == 22
    asyncio.run(database.close())


def test_testnet_account_status_is_sanitized_and_includes_reconciliation(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'testnet-account-api.db'}")
    market = ApiMarket()
    broker = ApiTestnetBroker()
    engine = TradingEngine(
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
        testnet_broker=broker,  # type: ignore[arg-type]
    )
    engine.testnet_reconciliation = ReconciliationReport(
        position_symbols=("BTCUSDT",),
        open_order_count=1,
        unprotected_symbols=(),
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.get("/api/testnet/account-status")
        assert response.status_code == 200
        status = response.json()
        assert status["enabled"] is True and status["active"] is True
        assert status["account"]["total_wallet_balance"] == "10000.5"
        # No canTrade field in the response, yet available margin (9000) marks it ready.
        assert status["account"]["can_trade"] is True
        assert set(status["account"]) == {
            "can_trade",
            "total_wallet_balance",
            "total_margin_balance",
            "available_balance",
            "total_unrealized_profit",
            "total_initial_margin",
        }
        assert status["positions"] == [
            {
                "symbol": "BTCUSDT",
                "position_amount": "0.25",
                "entry_price": "60000",
                "mark_price": "60100",
                "unrealized_profit": "25",
                "leverage": 3,
                "isolated": True,
            }
        ]
        assert status["reconciliation"] == {
            "position_symbols": ["BTCUSDT"],
            "open_order_count": 1,
            "unprotected_symbols": [],
        }

        portfolio = client.get("/api/account/portfolio").json()
        assert portfolio == {
            "source": "binance-testnet",
            "initial_equity": None,
            "cash": "10000.5",
            "equity": "10025.5",
            "available_balance": "9000",
            "daily_pnl": None,
            "unrealized_pnl": "25",
            "open_positions": 1,
            "margin_used": "1000",
        }
        positions = client.get("/api/account/positions").json()
        assert positions == [
            {
                "symbol": "BTCUSDT",
                "side": "LONG",
                "quantity": "0.25",
                "average_price": "60000",
                "mark_price": "60100",
                "leverage": 3,
                "unrealized_pnl": "25",
                "notional": "15025.00",
                "margin_used": "1000",
                # The live bracket triggers, not just the fact that one exists.
                "stop_loss": "58000",
                "take_profit": "63000",
                "protection_source": "exchange",
            }
        ]
        assert broker.account_calls == 1
        # The console refreshes several account panels together; the bracket read
        # is a signed request and must be memoized like the account itself.
        assert broker.level_calls == 1

        engine.testnet_reconciliation = ReconciliationReport(
            position_symbols=("BTCUSDT",),
            open_order_count=0,
            unprotected_symbols=("BTCUSDT",),
        )
        assert client.get("/api/account/positions").json()[0]["protection_source"] == "missing"
        assert broker.account_calls == 1
        assert broker.level_calls == 1
    asyncio.run(database.close())


def test_account_and_risk_query_endpoints(tmp_path: Path) -> None:
    from candlepilot.domain.models import ExecutionReport

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'account-api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=StatefulTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        # These are database-backed, so they are genuinely empty before any
        # trading. The account endpoints are not asserted here: they read
        # through a one-second memo, and priming it with an empty account would
        # hide the seeded position from the assertions below.
        assert client.get("/api/orders").json() == []
        assert client.get("/api/fills").json() == []
        assert client.get("/api/risk-events").json() == []

        # Seed one filled position on the exchange and its audited fill.
        engine.testnet_broker.positions["BTCUSDT"] = (  # type: ignore[attr-defined]
            "LONG",
            Decimal("1"),
            Decimal("100"),
        )
        asyncio.run(
            engine.audit.record_execution(
                "BTCUSDT",
                ExecutionReport(
                    client_order_id="cp-account-1",
                    status="FILLED",
                    filled_quantity="1",
                    average_price="100",
                ),
            )
        )

        positions = client.get("/api/account/positions").json()
        assert positions[0]["symbol"] == "BTCUSDT"
        assert positions[0]["side"] == "LONG"
        assert positions[0]["leverage"] == 3
        orders = client.get("/api/orders").json()
        assert orders[0]["client_order_id"] == "cp-account-1"
        assert client.get("/api/fills").json()[0]["status"] == "FILLED"
        portfolio = client.get("/api/account/portfolio").json()
        assert portfolio["source"] == "binance-testnet"
        assert portfolio["equity"] == "10000"
        assert portfolio["open_positions"] == 1
        assert client.get("/api/orders?limit=0").status_code == 422
    asyncio.run(database.close())


def test_alert_transitions_are_logged_and_persisted(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'alerts-api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    with TestClient(app) as client:
        # No alerts yet, so no history.
        assert client.get("/api/alerts").json()["active_count"] == 0
        assert client.get("/api/alerts/history").json()["events"] == []

        # Emergency lock fires a critical alert on the next evaluation.
        client.post("/api/engine/emergency-stop")
        fired = client.get("/api/alerts").json()
        assert any(a["id"] == "engine-emergency-lock" for a in fired["alerts"])
        assert [t["transition"] for t in fired["transitions"]] == ["fired"]

        # A steady-state re-poll emits no new transition.
        assert client.get("/api/alerts").json()["transitions"] == []

        # The fired transition is persisted to history.
        history = client.get("/api/alerts/history").json()["events"]
        assert history[0]["alert_id"] == "engine-emergency-lock"
        assert history[0]["transition"] == "fired"
        assert history[0]["severity"] == "critical"

        # Clearing the lock resolves the alert.
        client.post("/api/engine/clear-emergency-lock")
        resolved = client.get("/api/alerts").json()
        assert [t["transition"] for t in resolved["transitions"]] == ["resolved"]
        assert client.get("/api/alerts/history").json()["events"][0]["transition"] == "resolved"
        assert client.get("/api/alerts/history?limit=0").status_code == 422
    asyncio.run(database.close())


def test_provider_metrics_prices_codex_via_injected_catalog(tmp_path: Path) -> None:
    from candlepilot.providers.base import ProviderResult
    from candlepilot.providers.pricing import parse_models_dev

    catalog = parse_models_dev(
        {
            "openai": {
                "models": {"gpt-5.6-sol": {"cost": {"input": 5, "output": 30, "cache_read": 0.5}}}
            }
        }
    )

    async def loader(_cache_dir):
        return catalog

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'pricing-api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(
        database=database,
        market=market,  # type: ignore[arg-type]
        engine=engine,
        pricing_loader=loader,
    )
    intent = TradeIntent.hold("BTCUSDT", "5m", "seed")
    with TestClient(app) as client:
        asyncio.run(
            engine.audit.record_inference(
                ProviderResult(
                    intent=intent,
                    provider="codex-auth",
                    model="gpt-5.6-sol",
                    duration=timedelta(milliseconds=100),
                    raw_output=intent.model_dump_json(),
                    usage={
                        "input_tokens": 1000,
                        "cached_input_tokens": 400,
                        "output_tokens": 200,
                        "total_tokens": 1200,
                    },
                    input_payload={
                        "market": {"symbol": "BTCUSDT"},
                        "portfolio": {"equity": "10000"},
                    },
                    prompt="fixture prompt",
                )
            )
        )
        body = client.get("/api/metrics/providers").json()
        assert body["pricing_source"] == "models.dev"
        codex = body["providers"][0]
        assert codex["provider"] == "codex-auth"
        expected = 600 * 5e-6 + 400 * 5e-7 + 200 * 3e-5
        assert abs(float(codex["cost_usd_total"]) - expected) < 1e-9
        detail = client.get("/api/decision-events/1")
        assert detail.status_code == 200
        assert detail.json()["input"]["market"]["symbol"] == "BTCUSDT"
        assert detail.json()["prompt"] == "fixture prompt"
        assert detail.json()["usage"]["cached_input_tokens"] == 400
        assert abs(float(detail.json()["equivalent_cost_usd"]) - expected) < 1e-9
        assert client.get("/api/decision-events/999").status_code == 404
        assert client.get("/api/decision-events/0").status_code == 422
    asyncio.run(database.close())


def test_provider_config_sets_model_and_reasoning_effort(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'config-api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ConfigurableProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    with TestClient(app) as client:
        listed = client.get("/api/providers").json()[0]
        assert listed["model"] is None
        assert listed["reasoning_effort"] is None
        assert listed["reasoning_effort_options"] == ["low", "medium", "high"]

        updated = client.post(
            "/api/providers/config",
            json={"name": "api-fixture", "model": "gpt-x", "reasoning_effort": "high"},
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()[0]["model"] == "gpt-x"
        assert updated.json()[0]["reasoning_effort"] == "high"

        # Clearing sends empty strings back to null.
        cleared = client.post(
            "/api/providers/config",
            json={"name": "api-fixture", "model": "", "reasoning_effort": ""},
        ).json()[0]
        assert cleared["model"] is None
        assert cleared["reasoning_effort"] is None

        assert (
            client.post(
                "/api/providers/config", json={"name": "api-fixture", "reasoning_effort": "bogus"}
            ).status_code
            == 422
        )
        assert (
            client.post("/api/providers/config", json={"name": "missing", "model": "x"}).status_code
            == 404
        )

        # Locked while the engine runs.
        client.post("/api/providers/select", json={"name": "api-fixture"})
        client.post("/api/engine/start")
        assert (
            client.post(
                "/api/providers/config", json={"name": "api-fixture", "model": "y"}
            ).status_code
            == 409
        )
    asyncio.run(database.close())


def test_settings_endpoint_reads_masked_and_writes_env(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "# keep me\nCANDLEPILOT_PORT=8000\nBINANCE_TESTNET_API_KEY=super-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CANDLEPILOT_ENV_FILE", str(env_path))
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'settings.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    with TestClient(app) as client:
        payload = client.get("/api/settings").json()
        assert payload["path"] == str(env_path)
        fields = {f["key"]: f for s in payload["sections"] for f in s["fields"]}
        assert fields["CANDLEPILOT_PORT"]["value"] == "8000"
        # The secret is never returned in full, only a masked tail.
        assert fields["BINANCE_TESTNET_API_KEY"]["value"] is None
        assert fields["BINANCE_TESTNET_API_KEY"]["configured"] is True
        assert "super-secret" not in client.get("/api/settings").text

        saved = client.post(
            "/api/settings",
            json={"values": {"CANDLEPILOT_PORT": "9100", "CANDLEPILOT_CADENCES": "5m,15m"}},
        )
        assert saved.status_code == 200, saved.text
        text = env_path.read_text(encoding="utf-8")
        assert "# keep me" in text  # comments survive
        assert "CANDLEPILOT_PORT=9100" in text
        assert "CANDLEPILOT_CADENCES=5m,15m" in text
        assert "BINANCE_TESTNET_API_KEY=super-secret" in text  # untouched key kept

        # An empty value clears the setting: every parser treats "KEY=" as unset,
        # and keeping the key present matches the .env.example convention.
        cleared = client.post("/api/settings", json={"values": {"CANDLEPILOT_CADENCES": ""}})
        assert "CANDLEPILOT_CADENCES=\n" in env_path.read_text(encoding="utf-8")
        fields = {f["key"]: f for s in cleared.json()["sections"] for f in s["fields"]}
        assert fields["CANDLEPILOT_CADENCES"]["configured"] is False
        assert Settings.from_mapping(read_env_file(env_path)).cadences == ("5m", "15m", "30m")

        # Invalid values are rejected before the file is touched.
        before = env_path.read_text(encoding="utf-8")
        # These parse fine but would brick startup at engine/scheduler construction.
        for values in (
            {"CANDLEPILOT_CADENCES": "7m"},
            {"CANDLEPILOT_CANDIDATES_PER_CYCLE": "99"},
            {"CANDLEPILOT_HOST": "0.0.0.0"},
            {"CANDLEPILOT_MODE": "bogus"},
            {"CANDLEPILOT_PORT": "not-a-port"},
        ):
            assert client.post("/api/settings", json={"values": values}).status_code == 422, values
        assert env_path.read_text(encoding="utf-8") == before

        assert client.post(
            "/api/settings", json={"values": {"NOT_A_SETTING": "x"}}
        ).status_code == 422
        assert client.post(
            "/api/settings", json={"values": {"CANDLEPILOT_PORT": "1\n2"}}
        ).status_code == 422
    asyncio.run(database.close())


def test_custom_providers_editor_endpoint(tmp_path: Path, monkeypatch) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        'CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON=[{"id":"main","base_url":"https://a.example/v1",'
        '"api_key":"sk-existing","model":"m1","extra_headers":{"x-team":"desk"}}]\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("CANDLEPILOT_ENV_FILE", str(env_path))
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cp.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    with TestClient(app) as client:
        listed = client.get("/api/custom-providers")
        assert listed.status_code == 200, listed.text
        body = listed.json()
        assert body["max_providers"] == 8
        assert body["wire_apis"] == ["chat-completions", "responses"]
        entry = body["providers"][0]
        # Every field is editable in the clear except the key...
        assert entry["id"] == "main"
        assert entry["base_url"] == "https://a.example/v1"
        assert entry["model"] == "m1"
        # ...which is only ever reported as configured + masked.
        assert entry["api_key_configured"] is True
        assert "sk-existing" not in listed.text
        # Header values are secrets too: only names are exposed.
        assert entry["extra_header_names"] == ["x-team"]
        assert "desk" not in listed.text

        # Editing without resending the key keeps it, and keeps unsent headers.
        saved = client.post(
            "/api/custom-providers",
            json={"providers": [{"id": "main", "base_url": "https://b.example/v1", "model": "m2"}]},
        )
        assert saved.status_code == 200, saved.text
        stored = json.loads(read_env_file(env_path)["CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON"])
        assert stored[0]["api_key"] == "sk-existing"
        assert stored[0]["base_url"] == "https://b.example/v1"
        assert stored[0]["model"] == "m2"
        assert stored[0]["extra_headers"] == {"x-team": "desk"}

        # A supplied key replaces it; an empty string clears it.
        client.post(
            "/api/custom-providers",
            json={"providers": [{"id": "main", "base_url": "https://b.example/v1",
                                 "api_key": "sk-new"}]},
        )
        stored = json.loads(read_env_file(env_path)["CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON"])
        assert stored[0]["api_key"] == "sk-new"
        client.post(
            "/api/custom-providers",
            json={"providers": [{"id": "main", "base_url": "https://b.example/v1",
                                 "api_key": "", "require_api_key": False}]},
        )
        stored = json.loads(read_env_file(env_path)["CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON"])
        assert "api_key" not in stored[0]

        # Adding a second endpoint, then removing all of them.
        client.post(
            "/api/custom-providers",
            json={"providers": [
                {"id": "main", "base_url": "https://b.example/v1", "require_api_key": False},
                {"id": "local", "base_url": "http://127.0.0.1:1234/v1", "require_api_key": False},
            ]},
        )
        assert len(client.get("/api/custom-providers").json()["providers"]) == 2
        client.post("/api/custom-providers", json={"providers": []})
        assert client.get("/api/custom-providers").json()["providers"] == []
        assert read_env_file(env_path)["CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON"] == ""

        # Invalid definitions are rejected by the startup parser.
        for bad in (
            {"id": "BAD", "base_url": "https://x/v1"},
            {"id": "a", "base_url": "https://x/v1", "wire_api": "grpc"},
            {"id": "a", "base_url": "ftp://x/v1"},
        ):
            assert client.post(
                "/api/custom-providers", json={"providers": [bad]}
            ).status_code == 422, bad
    asyncio.run(database.close())


def test_restart_command_drops_dotenv_values_but_keeps_exports(monkeypatch) -> None:
    import candlepilot.api as api_module
    from candlepilot.api import restart_command

    # One value came from .env, one is genuinely exported in the shell.
    monkeypatch.setenv("CANDLEPILOT_FROM_DOTENV", "stale")
    monkeypatch.setenv("CANDLEPILOT_EXPORTED", "shell")
    monkeypatch.setattr(api_module, "DOTENV_INJECTED_KEYS", {"CANDLEPILOT_FROM_DOTENV"})
    monkeypatch.setattr(api_module.sys, "argv", ["/usr/bin/candlepilot", "serve"])

    argv, environment = restart_command()
    # Re-exec through the module so both launch styles come back the same way.
    assert argv[1:] == ["-m", "candlepilot.cli", "serve"]
    # The stale .env value must go, or the rewritten file would be ignored.
    assert "CANDLEPILOT_FROM_DOTENV" not in environment
    assert environment["CANDLEPILOT_EXPORTED"] == "shell"


def test_restart_is_refused_while_the_engine_runs(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'restart.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    with TestClient(app) as client:
        client.post("/api/providers/select", json={"name": "api-fixture"})
        client.post("/api/engine/start")
        # A restart would kill a live run, so it must be refused.
        refused = client.post("/api/restart")
        assert refused.status_code == 409
        assert "stop the engine" in refused.json()["detail"]
        client.post("/api/engine/stop")
    asyncio.run(database.close())


def test_run_limits_endpoint(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'run-limits.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    with TestClient(app) as client:
        status = client.get("/api/status").json()
        assert status["run_limits"] == {"max_run_seconds": None, "max_run_cost_usd": None}
        assert status["auto_stop_reason"] is None

        updated = client.post(
            "/api/run-limits", json={"max_run_seconds": 3600, "max_run_cost_usd": 2.5}
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["run_limits"] == {
            "max_run_seconds": 3600,
            "max_run_cost_usd": 2.5,
        }

        # Both limits are optional; null clears them back to unbounded.
        cleared = client.post(
            "/api/run-limits", json={"max_run_seconds": None, "max_run_cost_usd": None}
        ).json()
        assert cleared["run_limits"] == {"max_run_seconds": None, "max_run_cost_usd": None}

        assert client.post("/api/run-limits", json={"max_run_seconds": 0}).status_code == 422
        assert client.post("/api/run-limits", json={"max_run_cost_usd": -1}).status_code == 422

        # Locked while the engine runs.
        client.post("/api/providers/select", json={"name": "api-fixture"})
        client.post("/api/engine/start")
        assert client.post("/api/run-limits", json={"max_run_seconds": 60}).status_code == 409
    asyncio.run(database.close())


def test_provider_test_endpoint_reports_success_and_failure(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test-provider.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider(), BrokenProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    with TestClient(app) as client:
        ok = client.post("/api/providers/test", json={"name": "api-fixture"})
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["ok"] is True
        assert body["action"] == "HOLD"
        assert "duration_ms" in body

        broken = client.post("/api/providers/test", json={"name": "broken-fixture"}).json()
        assert broken["ok"] is False
        assert "bogus" in broken["detail"]

        assert client.post("/api/providers/test", json={"name": "missing"}).status_code == 404

        # The test call is not audited, so it leaves no inference/decision behind.
        assert client.get("/api/signals").json() == []

        # Locked while the engine runs.
        client.post("/api/providers/select", json={"name": "api-fixture"})
        client.post("/api/engine/start")
        assert client.post("/api/providers/test", json={"name": "api-fixture"}).status_code == 409
    asyncio.run(database.close())


def test_model_options_curated_from_catalog() -> None:
    from candlepilot.api import _model_options
    from candlepilot.providers.pricing import parse_models_dev

    catalog = parse_models_dev(
        {
            "openai": {
                "models": {
                    "gpt-5.6-sol": {"cost": {"input": 5, "output": 30}},
                    "gpt-5-codex": {"cost": {"input": 1, "output": 2}},
                    "gpt-4o": {"cost": {"input": 1, "output": 2}},
                }
            },
            "anthropic": {"models": {"claude-sonnet-5": {"cost": {"input": 3, "output": 15}}}},
        }
    )
    codex = _model_options("codex-auth", catalog, None)
    assert "gpt-5.6-sol" in codex and "gpt-5-codex" in codex
    assert "gpt-4o" not in codex  # filtered to the gpt-5 family

    claude = _model_options("claude-code-auth", catalog, None)
    assert claude[:4] == ["sonnet", "opus", "haiku", "fable"]  # aliases first
    assert "claude-sonnet-5" in claude

    # A current custom model is always included so it stays selectable.
    assert "gpt-9-custom" in _model_options("codex-auth", catalog, "gpt-9-custom")
    # Offline (no catalog): only curated aliases / current remain.
    assert _model_options("codex-auth", None, None) == []
    assert _model_options("claude-code-auth", None, "opus") == ["sonnet", "opus", "haiku", "fable"]


def test_history_clear_removes_selected_categories(tmp_path: Path) -> None:
    from candlepilot.providers.base import ProviderResult

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'history-api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(
        settings=Settings(data_dir=tmp_path),
        database=database,
        market=market,
        engine=engine,  # type: ignore[arg-type]
    )
    intent = TradeIntent.hold("BTCUSDT", "5m", "seed")
    with TestClient(app) as client:
        asyncio.run(
            engine.audit.record_inference(
                ProviderResult(
                    intent=intent,
                    provider="codex-auth",
                    model="m",
                    duration=timedelta(milliseconds=1),
                    raw_output=intent.model_dump_json(),
                    usage={},
                )
            )
        )
        assert len(client.get("/api/signals").json()) == 1

        response = client.post(
            "/api/history/clear", json={"categories": ["inferences", "market_cache"]}
        )
        assert response.status_code == 200, response.text
        cleared = response.json()["cleared"]
        assert cleared["inferences"] == 1
        assert "market_cache" in cleared
        assert client.get("/api/signals").json() == []

        assert client.post("/api/history/clear", json={"categories": ["bogus"]}).status_code == 422
        assert client.post("/api/history/clear", json={"categories": []}).status_code == 422
    asyncio.run(database.close())


def test_cadence_selection_endpoint(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cadence-api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    with TestClient(app) as client:
        status = client.get("/api/status").json()
        assert status["active_cadences"] == ["5m", "15m", "30m"]
        assert status["supported_cadences"] == ["5m", "15m", "30m"]

        updated = client.post("/api/cadences", json={"cadences": ["30m", "15m"]})
        assert updated.status_code == 200, updated.text
        assert updated.json()["active_cadences"] == ["15m", "30m"]  # canonical order

        assert client.post("/api/cadences", json={"cadences": ["1m"]}).status_code == 422
        assert client.post("/api/cadences", json={"cadences": []}).status_code == 422

        # Locked while the engine runs.
        client.post("/api/providers/select", json={"name": "api-fixture"})
        client.post("/api/engine/start")
        assert client.post("/api/cadences", json={"cadences": ["5m"]}).status_code == 409
    asyncio.run(database.close())


def test_candidates_per_cycle_endpoint(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'per-cycle-api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    with TestClient(app) as client:
        status = client.get("/api/status").json()
        assert status["candidates_per_cycle"] == 5
        assert status["max_candidates_per_cycle"] == 20

        updated = client.post("/api/candidates-per-cycle", json={"candidates_per_cycle": 8})
        assert updated.status_code == 200, updated.text
        assert updated.json()["candidates_per_cycle"] == 8

        # Out-of-range values are rejected by the request schema.
        assert (
            client.post("/api/candidates-per-cycle", json={"candidates_per_cycle": 0}).status_code
            == 422
        )
        assert (
            client.post("/api/candidates-per-cycle", json={"candidates_per_cycle": 21}).status_code
            == 422
        )

        # Locked while the engine runs.
        client.post("/api/providers/select", json={"name": "api-fixture"})
        client.post("/api/engine/start")
        assert (
            client.post("/api/candidates-per-cycle", json={"candidates_per_cycle": 3}).status_code
            == 409
        )
    asyncio.run(database.close())


def test_unknown_provider_is_404(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.post("/api/providers/select", json={"name": "missing"})
        assert response.status_code == 404
    asyncio.run(database.close())


def test_provider_route_api_exposes_order_and_locks_while_running(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'provider-route-api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    with TestClient(app) as client:
        selected = client.post(
            "/api/providers/select", json={"providers": ["api-fixture"]}
        )
        assert selected.status_code == 200
        assert selected.json()["provider_chain"] == ["api-fixture"]
        assert selected.json()["provider_routes"][0]["priority"] == 1
        assert selected.json()["active_provider"] is None
        assert client.post("/api/engine/start").json()["active_provider"] == "api-fixture"
        locked = client.post(
            "/api/providers/select", json={"providers": ["api-fixture"]}
        )
        assert locked.status_code == 409
    asyncio.run(database.close())


def test_backtest_detail_returns_404_for_unknown_run(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'missing-backtest.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    with TestClient(app) as client:
        assert client.get("/api/backtests/999").status_code == 404
    asyncio.run(database.close())


def test_decision_events_reject_filters_they_cannot_honour(tmp_path: Path) -> None:
    """An unknown filter value must not read as "no such decisions"."""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'filters.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        assert client.get("/api/decision-events?outcome=hold").status_code == 200
        assert client.get("/api/decision-events?outcome=rejceted").status_code == 422
        assert client.get("/api/decision-events?cadence=7m").status_code == 422
        assert client.get("/api/decision-events?before_id=0").status_code == 422
        assert client.get("/api/decision-events?limit=501").status_code == 422
    asyncio.run(database.close())


class BacktestMarket(ApiMarket):
    """History deep enough for the warm-up every decision needs."""

    async def exchange_info(self):
        from candlepilot.market.binance import ContractInfo
        from candlepilot.risk.engine import SymbolRules

        rules = SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01"))
        return {
            symbol: ContractInfo(symbol, datetime(2020, 1, 1, tzinfo=UTC), rules)
            for symbol in ("BTCUSDT", "ETHUSDT")
        }

    async def historical_klines(self, symbol, interval, start, end, *, max_candles=10_000):
        step = {"5m": 300_000, "15m": 900_000, "30m": 1_800_000, "1d": 86_400_000}[interval]
        start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        rows = []
        price = 100.0
        for index in range((end_ms - start_ms) // step):
            price *= 1.0005
            open_ms = start_ms + index * step
            rows.append(
                [open_ms, str(price), str(price * 1.004), str(price * 0.996), str(price), "500"]
            )
        return rows[:max_candles]


def _backtest_app(tmp_path: Path, name: str):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / name}")
    market = BacktestMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    return database, engine, app


def _window(hours: int = 1) -> dict[str, str]:
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    return {
        "start": (end - timedelta(hours=hours)).isoformat(),
        "end": end.isoformat(),
    }


def test_backtest_estimate_counts_calls_before_any_are_paid_for(tmp_path: Path) -> None:
    database, _engine, app = _backtest_app(tmp_path, "bt-estimate.db")

    with TestClient(app) as client:
        response = client.post(
            "/api/backtests/estimate",
            json={"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(2)},
        )
        assert response.status_code == 200
        body = response.json()
        # Two hours of 5m bars.
        assert body["decisions_per_model"] == 24
        assert body["total_calls"] == 24
        assert body["within_limit"] is True
    asyncio.run(database.close())


def test_backtest_refuses_a_window_that_could_not_finish(tmp_path: Path) -> None:
    """A three-day multi-symbol comparison is days of real calls, not seconds."""

    database, _engine, app = _backtest_app(tmp_path, "bt-limit.db")

    with TestClient(app) as client:
        end = datetime.now(UTC) - timedelta(hours=1)
        response = client.post(
            "/api/backtests",
            json={
                "symbols": ["BTCUSDT", "ETHUSDT"],
                "cadences": ["5m", "15m", "30m"],
                "providers": ["api-fixture"],
                "start": (end - timedelta(days=3)).isoformat(),
                "end": end.isoformat(),
            },
        )
        assert response.status_code == 422
        assert "calls per model" in response.json()["detail"]
    asyncio.run(database.close())


def test_backtest_is_refused_while_the_engine_runs(tmp_path: Path) -> None:
    """They share a provider, and each provider serialises its own calls."""

    database, engine, app = _backtest_app(tmp_path, "bt-busy.db")

    with TestClient(app) as client:
        client.post("/api/providers/select", json={"name": "api-fixture"})
        assert client.post("/api/engine/start").json()["running"] is True

        response = client.post(
            "/api/backtests",
            json={"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window()},
        )

        assert response.status_code == 409
        assert "stop the engine" in response.json()["detail"]
    asyncio.run(database.close())


def test_backtest_runs_in_the_background_and_reports_each_model(tmp_path: Path) -> None:
    database, _engine, app = _backtest_app(tmp_path, "bt-run.db")

    with TestClient(app) as client:
        created = client.post(
            "/api/backtests",
            json={"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window()},
        )
        # 202: a real window is hours of calls, so it cannot be a synchronous
        # request. The id is handed back immediately.
        assert created.status_code == 202
        run_id = created.json()["id"]
        assert created.json()["estimate"]["decisions_per_model"] == 12

        for _ in range(200):
            run = client.get(f"/api/backtests/{run_id}").json()
            if run["status"] != "running":
                break
            time.sleep(0.05)

        assert run["status"] == "completed", run.get("error")
        assert [model["provider"] for model in run["models"]] == ["api-fixture"]
        model = run["models"][0]
        assert model["decisions_done"] == model["decisions_total"] == 12
        assert model["progress"] == 1.0
        # The fixture provider only ever holds, so nothing traded.
        assert model["result"]["trade_count"] == 0
        assert client.get("/api/backtests").json()[0]["id"] == run_id
    asyncio.run(database.close())


def test_backtest_rejects_an_unknown_model_before_starting(tmp_path: Path) -> None:
    database, _engine, app = _backtest_app(tmp_path, "bt-unknown.db")

    with TestClient(app) as client:
        response = client.post(
            "/api/backtests",
            json={"symbols": ["BTCUSDT"], "providers": ["nope"], **_window()},
        )
        assert response.status_code == 404
    asyncio.run(database.close())


def _seed_captures(
    database, engine, symbol: str, times: list[datetime], *, version: str | None = None
) -> None:
    from candlepilot.provenance import MICROSTRUCTURE_SCHEMA_VERSION

    asyncio.run(database.initialize())
    asyncio.run(
        engine.audit.store_book_captures(
            [
                {
                    "symbol": symbol,
                    "captured_at": when,
                    "schema_version": version or MICROSTRUCTURE_SCHEMA_VERSION,
                    "payload": {
                        "bid": "99.9", "ask": "100.1", "mark_price": "100",
                        "index_price": "100", "funding_rate": "0.0001",
                        "depth": {"bids": [["99", "3"]], "asks": [["101", "1"]]},
                        "trade_imbalance": 0.25, "trade_seconds": 180.0,
                        "open_interest": "1234.5",
                    },
                }
                for when in times
            ]
        )
    )


def test_collector_records_without_touching_a_model(tmp_path: Path) -> None:
    database, engine, app = _backtest_app(tmp_path, "collector.db")

    with TestClient(app) as client:
        assert client.get("/api/collector").json()["running"] is False
        started = client.post("/api/collector/start", json={"symbols": ["BTCUSDT"]})
        assert started.status_code == 200
        assert started.json()["running"] is True
        assert started.json()["symbols"] == ["BTCUSDT"]
        # Starting twice is a mistake worth naming, not a silent restart.
        assert client.post("/api/collector/start", json={"symbols": ["ETHUSDT"]}).status_code == 409
        assert client.post("/api/collector/stop").json()["running"] is False
    asyncio.run(database.close())


def test_a_real_backtest_is_refused_when_the_book_was_not_recorded(tmp_path: Path) -> None:
    """Half a window with flow is two strategies averaged into one number."""

    database, engine, app = _backtest_app(tmp_path, "bt-partial.db")
    window = _window(1)
    start = datetime.fromisoformat(window["start"])

    # Only two of the twelve 5-minute instants the window needs.
    _seed_captures(database, engine, "BTCUSDT", [start, start + timedelta(minutes=5)])

    with TestClient(app) as client:
        response = client.post(
            "/api/backtests",
            json={
                "symbols": ["BTCUSDT"], "providers": ["api-fixture"],
                "use_recorded_book": True, **window,
            },
        )
        assert response.status_code == 409
        detail = response.json()["detail"]
        assert "decision instants" in detail
        assert "plain backtest" in detail
    asyncio.run(database.close())


def test_a_real_backtest_runs_when_every_instant_was_recorded(tmp_path: Path) -> None:
    database, engine, app = _backtest_app(tmp_path, "bt-real.db")
    window = _window(1)
    start = datetime.fromisoformat(window["start"])
    end = datetime.fromisoformat(window["end"])

    from candlepilot.market.collector import aligned_capture_times

    _seed_captures(database, engine, "BTCUSDT", aligned_capture_times(start, end))

    with TestClient(app) as client:
        created = client.post(
            "/api/backtests",
            json={
                "symbols": ["BTCUSDT"], "providers": ["api-fixture"],
                "use_recorded_book": True, **window,
            },
        )
        assert created.status_code == 202
        run_id = created.json()["id"]
        for _ in range(200):
            run = client.get(f"/api/backtests/{run_id}").json()
            if run["status"] != "running":
                break
            time.sleep(0.05)
        assert run["status"] == "completed", run.get("error")
        assert run["spec"]["use_recorded_book"] is True
    asyncio.run(database.close())


def test_captures_from_an_older_derivation_are_refused_not_replayed(tmp_path: Path) -> None:
    """The tape summary cannot be recomputed, so a formula change invalidates it."""

    database, engine, app = _backtest_app(tmp_path, "bt-stale.db")
    window = _window(1)
    start = datetime.fromisoformat(window["start"])
    end = datetime.fromisoformat(window["end"])

    from candlepilot.market.collector import aligned_capture_times

    _seed_captures(
        database, engine, "BTCUSDT", aligned_capture_times(start, end), version="microstructure-v0"
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/backtests",
            json={
                "symbols": ["BTCUSDT"], "providers": ["api-fixture"],
                "use_recorded_book": True, **window,
            },
        )
        assert response.status_code == 409
        assert "no longer mean the same thing" in response.json()["detail"]
    asyncio.run(database.close())


def test_a_plain_backtest_does_not_need_any_captures(tmp_path: Path) -> None:
    database, _engine, app = _backtest_app(tmp_path, "bt-plain.db")

    with TestClient(app) as client:
        created = client.post(
            "/api/backtests",
            json={"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)},
        )
        assert created.status_code == 202
    asyncio.run(database.close())


def test_a_running_backtest_reports_progress_over_the_api(tmp_path: Path) -> None:
    """The console polls this endpoint, so 0% here is 0% on screen.

    compare() was never given on_progress and the stored row was only written
    once the whole comparison returned, so a run that takes minutes showed 0%
    with a 0 denominator for its entire life and then jumped straight to 100%.
    """

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bt-progress.db'}")
    market = BacktestMarket()
    gate = asyncio.Event()
    seen: list[dict[str, object]] = []

    class Gated(ApiProvider):
        """Holds the run open at its second decision so the API can be read."""

        calls = 0

        async def generate_trade_intent(self, snapshot, portfolio):
            Gated.calls += 1
            if Gated.calls == 2:
                await gate.wait()
            return await super().generate_trade_intent(snapshot, portfolio)

    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([Gated()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        created = client.post(
            "/api/backtests",
            json={"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)},
        )
        assert created.status_code == 202

        # Wait for the run to reach the gate rather than sleeping a fixed span.
        for _ in range(200):
            model = client.get("/api/backtests").json()[0]["models"][0]
            if model["decisions_total"] and model["decisions_done"] >= 1:
                seen.append(model)
                break
            time.sleep(0.02)
        gate.set()

    assert seen, "the API never showed a decision while the run was in flight"
    mid = seen[0]
    # A denominator, and a numerator that is neither 0 nor already finished.
    assert mid["decisions_total"] == 12
    assert 0 < mid["progress"] < 1
    asyncio.run(database.close())
