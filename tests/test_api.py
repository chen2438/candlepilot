import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from candlepilot.api import create_app
from candlepilot.application.engine import TradingEngine
from candlepilot.broker.binance_testnet import ReconciliationReport
from candlepilot.config import Settings
from candlepilot.domain.models import (
    MarketSnapshot,
    ProviderHealth,
    TradeIntent,
    TradingMode,
)
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
        step = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000}[
            interval
        ]
        start_ms = int(start.timestamp() * 1000)
        return [
            [start_ms + offset * step, "100", "101", "99", "100", "10"]
            for offset in range(min(2, max_candles))
        ]

    async def historical_funding_rates(self, symbol, start, end, *, max_events=10_000):
        return []


class LLMReplayMarket(ApiMarket):
    async def historical_klines(self, symbol, interval, start, end, *, max_candles=10_000):
        step = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000}[
            interval
        ]
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
        assert client.get("/api/health/live").json()["status"] == "alive"
        readiness = client.get("/api/health/ready")
        assert readiness.status_code == 200
        assert readiness.json()["checks"]["database"] == {
            "ready": True,
            "schema_version": 2,
            "expected_schema_version": 2,
        }
        runtime_metrics = client.get("/api/metrics/runtime")
        assert runtime_metrics.status_code == 200
        assert int(runtime_metrics.headers["X-Request-ID"], 16) >= 0
        assert runtime_metrics.json()["requests_total"] >= 2
        assert runtime_metrics.json()["in_flight"] == 1
        assert client.get("/api/alerts").json()["active_count"] == 0
        assert client.get("/api/status").json()["running"] is False
        assert client.get("/api/status").json()["market_stream"]["enabled"] is False
        assert client.get("/api/status").json()["user_stream"]["enabled"] is False
        assert client.get("/api/testnet/events").json() == []
        assert client.get("/api/decision-events").json() == []
        assert client.get("/api/decision-events?limit=0").status_code == 422
        assert client.get("/api/testnet/account-status").json()["enabled"] is False
        assert client.get("/api/metrics/providers").json() == {
            "window_hours": 24,
            "pricing_source": None,
            "providers": [],
        }
        assert client.get("/api/metrics/providers?hours=0").status_code == 422
        assert client.post("/api/engine/start").status_code == 409
        assert client.post(
            "/api/providers/select", json={"name": "api-fixture"}
        ).status_code == 200
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
                    {},
                )
            )
        )
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
    asyncio.run(database.close())


def test_default_provider_is_selected_from_settings(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'default-provider.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        mode=TradingMode.PAPER,
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


def test_application_wires_snapshot_age_into_risk_policy(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'risk-settings-api.db'}")
    market = ApiMarket()
    application = create_app(
        settings=Settings(max_snapshot_age_seconds=22),
        database=database,
        market=market,  # type: ignore[arg-type]
    )

    assert application.state.engine.risk.max_snapshot_age_seconds == 22
    asyncio.run(database.close())


def test_readiness_rejects_testnet_mode_without_broker(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'not-ready-api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        mode=TradingMode.TESTNET,
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.get("/api/health/ready")
        assert response.status_code == 503
        payload = response.json()
        assert payload["status"] == "not_ready"
        assert payload["checks"]["database"]["ready"] is True
        assert payload["checks"]["testnet_broker"] == {
            "ready": False,
            "required": True,
            "configured": False,
        }
    asyncio.run(database.close())


def test_testnet_account_status_is_sanitized_and_includes_reconciliation(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'testnet-account-api.db'}")
    market = ApiMarket()
    broker = ApiTestnetBroker()
    engine = TradingEngine(
        mode=TradingMode.TESTNET,
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
            "mode": "binance-testnet",
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
                "stop_loss": None,
                "take_profit": None,
                "protection_source": "exchange",
            }
        ]
        assert broker.account_calls == 1

        engine.testnet_reconciliation = ReconciliationReport(
            position_symbols=("BTCUSDT",),
            open_order_count=0,
            unprotected_symbols=("BTCUSDT",),
        )
        assert client.get("/api/account/positions").json()[0]["protection_source"] == "missing"
        assert broker.account_calls == 1
    asyncio.run(database.close())


def test_account_and_risk_query_endpoints(tmp_path: Path) -> None:
    from candlepilot.domain.models import OrderPlan, OrderType

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'account-api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        mode=TradingMode.PAPER,
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        # Empty account before any trading activity.
        assert client.get("/api/account/positions").json() == []
        assert client.get("/api/orders").json() == []
        assert client.get("/api/fills").json() == []
        assert client.get("/api/risk-events").json() == []
        portfolio = client.get("/api/account/portfolio").json()
        assert portfolio["mode"] == "paper-production-data"
        assert portfolio["source"] == "paper"
        assert portfolio["equity"] == "10000"
        assert portfolio["open_positions"] == 0

        # Seed one filled paper position directly through the executor.
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            cadence="1m",
            timestamp=datetime.now(UTC),
            mark_price="100",
            bid="99.9",
            ask="100.1",
            quote_volume_24h="1000000",
        )
        report = asyncio.run(
            engine.paper_executor.execute(
                OrderPlan(
                    client_order_id="cp-account-1",
                    symbol="BTCUSDT",
                    side="BUY",
                    quantity=Decimal("1"),
                    order_type=OrderType.MARKET,
                    stop_price=Decimal("95"),
                ),
                snapshot,
                leverage=3,
            )
        )
        asyncio.run(engine.audit.record_execution("BTCUSDT", report))

        positions = client.get("/api/account/positions").json()
        assert positions[0]["symbol"] == "BTCUSDT"
        assert positions[0]["side"] == "LONG"
        assert positions[0]["leverage"] == 3
        orders = client.get("/api/orders").json()
        assert orders[0]["client_order_id"] == "cp-account-1"
        assert client.get("/api/fills").json()[0]["status"] == "FILLED"
        assert client.get("/api/account/portfolio").json()["open_positions"] == 1
        assert client.get("/api/orders?limit=0").status_code == 422
    asyncio.run(database.close())


def test_alert_transitions_are_logged_and_persisted(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'alerts-api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        mode=TradingMode.PAPER,
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
        {"openai": {"models": {"gpt-5.6-sol": {"cost": {"input": 5, "output": 30, "cache_read": 0.5}}}}}
    )

    async def loader(_cache_dir):
        return catalog

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'pricing-api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        mode=TradingMode.PAPER,
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
        mode=TradingMode.PAPER,
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
            "/api/providers/config", json={"name": "api-fixture", "model": "", "reasoning_effort": ""}
        ).json()[0]
        assert cleared["model"] is None
        assert cleared["reasoning_effort"] is None

        assert client.post(
            "/api/providers/config", json={"name": "api-fixture", "reasoning_effort": "bogus"}
        ).status_code == 422
        assert client.post(
            "/api/providers/config", json={"name": "missing", "model": "x"}
        ).status_code == 404

        # Locked while the engine runs.
        client.post("/api/providers/select", json={"name": "api-fixture"})
        client.post("/api/engine/start")
        assert client.post(
            "/api/providers/config", json={"name": "api-fixture", "model": "y"}
        ).status_code == 409
    asyncio.run(database.close())


def test_provider_test_endpoint_reports_success_and_failure(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test-provider.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        mode=TradingMode.PAPER,
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
        mode=TradingMode.PAPER,
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(
        settings=Settings(data_dir=tmp_path), database=database, market=market, engine=engine  # type: ignore[arg-type]
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

        assert client.post(
            "/api/history/clear", json={"categories": ["bogus"]}
        ).status_code == 422
        assert client.post("/api/history/clear", json={"categories": []}).status_code == 422
    asyncio.run(database.close())


def test_cadence_selection_endpoint(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cadence-api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        mode=TradingMode.PAPER,
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
        mode=TradingMode.PAPER,
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
        assert run["result"]["provenance"]["data_version"].startswith(
            "backtest-candles-v1:sha256:"
        )

        listed = client.get("/api/backtests").json()
        assert listed[0]["id"] == run["id"]
        assert listed[0]["result"]["trade_count"] == 1
        assert "trades" not in listed[0]["result"]
        assert "equity_curve" not in listed[0]["result"]
        assert client.get(f"/api/backtests/{run['id']}").json() == run
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


def test_backtest_detail_returns_404_for_unknown_run(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'missing-backtest.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        mode=TradingMode.PAPER,
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    with TestClient(app) as client:
        assert client.get("/api/backtests/999").status_code == 404
    asyncio.run(database.close())


def test_fresh_llm_backtest_calls_provider_and_audits_decisions(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'fresh-replay.db'}")
    market = LLMReplayMarket()
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
            "/api/backtests/llm",
            json={
                "symbol": "BTCUSDT",
                "cadence": "5m",
                "provider": "api-fixture",
                "start": start.isoformat(),
                "end": (start + timedelta(hours=2)).isoformat(),
                "max_calls": 2,
            },
        )
        assert response.status_code == 201, response.text
        replay = response.json()["result"]["replay"]
        assert replay["source"] == "fresh_llm_calls"
        assert replay["decision_count"] == 2
        assert len(client.get("/api/signals").json()) == 2
    asyncio.run(database.close())


def test_portfolio_backtest_api_persists_aggregate_result(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'portfolio-api.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        mode=TradingMode.PAPER,
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    start = datetime(2026, 1, 1, tzinfo=UTC)
    candles = [
        {
            "timestamp": start.isoformat(),
            "open": "100",
            "high": "101",
            "low": "99",
            "close": "100",
            "volume": "10",
        }
    ]

    with TestClient(app) as client:
        response = client.post(
            "/api/backtests/portfolio",
            json={
                "legs": [
                    {"symbol": "BTCUSDT", "cadence": "5m", "candles": candles},
                    {"symbol": "ETHUSDT", "cadence": "5m", "candles": candles},
                ]
            },
        )
        assert response.status_code == 201, response.text
        run = response.json()
        assert run["symbol"] == "PORTFOLIO"
        assert run["result"]["allocation"] == "equal_weight_sleeves"
        assert set(run["result"]["per_symbol"]) == {"BTCUSDT", "ETHUSDT"}
    asyncio.run(database.close())
