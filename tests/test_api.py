import asyncio
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from candlepilot.api import create_app
from candlepilot.application.engine import TradingEngine
from candlepilot.application.scheduler import CADENCE_SECONDS
from candlepilot.backtest.probe import PROBE_CEILING_SECONDS, PROBE_DECISIONS
from conftest import FakeTestnetBroker, StatefulTestnetBroker
from candlepilot.broker.binance_testnet import ProtectiveLevels, ReconciliationReport
from candlepilot.config import Settings
from candlepilot.domain.models import (
    MarketSnapshot,
    ProviderHealth,
    TradeIntent,
)
from candlepilot.market.scanner import MarketCandidateInput
from candlepilot.providers.base import DecisionProvider, ProviderResult
from candlepilot.providers.local import LocalRuleProvider
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.settings_file import read_env_file
from candlepilot.storage.database import (
    CURRENT_SCHEMA_VERSION,
    AuditRepository,
    Database,
)


class ApiProvider(DecisionProvider):
    name = "api-fixture"

    async def health_check(self):
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio):
        intent = TradeIntent.hold(snapshot.symbol, snapshot.cadence, "fixture")
        return ProviderResult(intent, self.name, None, timedelta(0), intent.model_dump_json(), {})


class ConfigurableProvider(ApiProvider):
    name = "api-fixture"
    reasoning_effort_options = ("low", "medium", "high")


class CustomApiProvider(ApiProvider):
    name = "openai-compatible:main"


class BrokenProvider(DecisionProvider):
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

    async def market_snapshot(self, symbol, cadence):
        return MarketSnapshot(
            symbol=symbol,
            cadence=cadence,
            timestamp=datetime.now(UTC),
            mark_price="100",
            bid="99.9",
            ask="100.1",
            quote_volume_24h="1000000",
        )

    async def historical_klines(self, symbol, interval, start, end, *, max_candles=10_000):
        step = {
            "1m": 60_000,
            "5m": 300_000,
            "15m": 900_000,
            "30m": 1_800_000,
            "1h": 3_600_000,
            "4h": 14_400_000,
        }[interval]
        start_ms = int(start.timestamp() * 1000)
        return [
            [start_ms + offset * step, "100", "101", "99", "100", "10"]
            for offset in range(min(2, max_candles))
        ]

    async def historical_funding_rates(self, symbol, start, end, *, max_events=10_000):
        return []


class LLMReplayMarket(ApiMarket):
    async def historical_klines(self, symbol, interval, start, end, *, max_candles=10_000):
        step = {
            "1m": 60_000,
            "5m": 300_000,
            "15m": 900_000,
            "30m": 1_800_000,
            "1h": 3_600_000,
            "4h": 14_400_000,
        }[interval]
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
        self.position_risk_calls = 0
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
        # Mirrors the real /fapi/v3/account futures response: it has neither
        # canTrade nor a usable mark/entry price for the position table.
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
                    "unrealizedProfit": "25",
                    "leverage": "3",
                    "isolated": True,
                    "positionInitialMargin": "1000",
                },
                {"symbol": "ETHUSDT", "positionAmt": "0"},
            ],
        }

    async def position_risk(self):
        self.position_risk_calls += 1
        return [
            {
                "symbol": "BTCUSDT",
                "positionAmt": "0.25",
                "entryPrice": "60000",
                "markPrice": "60100",
                "unRealizedProfit": "25",
                "leverage": "3",
            }
        ]


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
        status = client.get("/api/status").json()
        assert status["running"] is False
        assert status["user_stream"]["enabled"] is False
        assert status["route_failure_count"] == 0
        assert status["route_failure_limit"] == 3
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
        assert client.post("/api/providers/select", json={"providers": ["api-fixture"]}).status_code == 200
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


def test_live_start_runs_three_real_decisions_and_freezes_user_timeout(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'live-probe.db'}")
    market = ApiMarket()

    class MeasuredProvider(ApiProvider):
        def __init__(self) -> None:
            self.calls = 0
            self.observed_timeouts: list[float] = []

        async def generate_trade_intent(self, snapshot, portfolio):
            self.calls += 1
            self.observed_timeouts.append(self.timeout)
            await asyncio.sleep(0.001)
            return await super().generate_trade_intent(snapshot, portfolio)

    provider = MeasuredProvider()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([provider]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
        cadences=("15m",),
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        client.post("/api/providers/select", json={"providers": ["api-fixture"]})
        response = client.post(
            "/api/engine/start", json={"timeout_seconds": 7}
        )
        assert response.status_code == 200, response.text
        status = response.json()
        assert provider.calls == 3
        assert provider.observed_timeouts == [7, 7, 7]
        assert status["decision_timeout_seconds"] == 7
        assert status["startup_probe"]["decisions_per_provider"] == 3
        assert status["startup_probe"]["running"] is False
        assert status["startup_probe"]["completed_decisions"] == 3
        assert status["startup_probe"]["active_decision"] is None
        assert status["startup_probe"]["analysis_symbol_count"] == 1
        client.post("/api/engine/stop")
        assert provider.timeout == 45
    asyncio.run(database.close())


def test_live_startup_probe_publishes_each_decision_progress(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'live-probe-progress.db'}")
    market = ApiMarket()
    second_call_started = threading.Event()
    release_second_call = threading.Event()

    class GatedProvider(ApiProvider):
        calls = 0

        async def generate_trade_intent(self, snapshot, portfolio):
            GatedProvider.calls += 1
            if GatedProvider.calls == 2:
                second_call_started.set()
                await asyncio.to_thread(release_second_call.wait)
            return await super().generate_trade_intent(snapshot, portfolio)

    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([GatedProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
        cadences=("15m",),
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=1) as pool:
        client.post("/api/providers/select", json={"providers": ["api-fixture"]})
        future = pool.submit(client.post, "/api/engine/start")
        try:
            assert second_call_started.wait(timeout=2)
            progress = client.get("/api/status").json()["startup_probe"]
            assert progress["running"] is True
            assert progress["active_decision"] == 2
            assert progress["completed_decisions"] == 1
            assert len(progress["durations_seconds"]["api-fixture"]) == 1
            assert progress["probe_symbol"] == "BTCUSDT"
            assert progress["probe_cadence"] == "15m"
        finally:
            release_second_call.set()
        response = future.result(timeout=2)
        assert response.status_code == 200, response.text
        assert response.json()["startup_probe"]["completed_decisions"] == 3
        client.post("/api/engine/stop")
    asyncio.run(database.close())


def test_live_startup_probe_cancels_sibling_providers_after_failure(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'live-probe-cancel.db'}")
    market = ApiMarket()
    hanging_started = threading.Event()
    hanging_cancelled = threading.Event()

    class Failing(ApiProvider):
        name = "failing-startup"

        async def generate_trade_intent(self, snapshot, portfolio):
            await asyncio.to_thread(hanging_started.wait)
            raise RuntimeError("startup fixture failed")

    class Hanging(ApiProvider):
        name = "hanging-startup"

        async def generate_trade_intent(self, snapshot, portfolio):
            hanging_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                hanging_cancelled.set()
                raise

    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([Failing(), Hanging()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
        cadences=("15m",),
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        client.post(
            "/api/providers/select",
            json={"providers": ["failing-startup", "hanging-startup"]},
        )
        response = client.post("/api/engine/start")
        assert response.status_code == 409
        assert "startup fixture failed" in response.json()["detail"]
        assert hanging_cancelled.is_set()
        assert engine.providers.get("failing-startup").timeout == 45
        assert engine.providers.get("hanging-startup").timeout == 45
    asyncio.run(database.close())


def test_live_start_rejects_a_probe_that_cannot_fit_the_selected_cadence(
    tmp_path: Path, monkeypatch
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'live-capacity.db'}")
    market = ApiMarket()

    class SlowProvider(ApiProvider):
        async def generate_trade_intent(self, snapshot, portfolio):
            await asyncio.sleep(0.02)
            return await super().generate_trade_intent(snapshot, portfolio)

    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([SlowProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
        cadences=("5m",),
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    monkeypatch.setitem(CADENCE_SECONDS, "5m", 0.01)

    with TestClient(app) as client:
        client.post("/api/providers/select", json={"providers": ["api-fixture"]})
        response = client.post(
            "/api/engine/start", json={"timeout_seconds": 1}
        )
        assert response.status_code == 422
        assert "Reduce analysis symbols" in response.json()["detail"]
        assert client.get("/api/status").json()["running"] is False
    asyncio.run(database.close())


def test_provider_chain_is_selected_from_settings(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'default-provider.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    application = create_app(
        settings=Settings(provider_chain=("api-fixture",)),
        database=database,
        market=market,  # type: ignore[arg-type]
        engine=engine,
    )

    assert application.state.engine.provider_chain == ("api-fixture",)
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
            "pending_entry_symbols": [],
        }

        portfolio = client.get("/api/account/portfolio").json()
        assert portfolio == {
            "source": "binance-testnet",
            "initial_equity": None,
            "cash": "10000.5",
            "equity": "10025.5",
            "available_balance": "9000",
            "daily_pnl": "25",
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
        assert broker.position_risk_calls == 1
        # The frontend refreshes several account panels together; the bracket read
        # is a signed request and must be memoized like the account itself.
        assert broker.level_calls == 1

        engine.testnet_reconciliation = ReconciliationReport(
            position_symbols=("BTCUSDT",),
            open_order_count=0,
            unprotected_symbols=("BTCUSDT",),
        )
        assert client.get("/api/account/positions").json()[0]["protection_source"] == "missing"
        assert broker.account_calls == 1
        assert broker.position_risk_calls == 1
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


def test_manual_position_close_requires_stopped_engine_and_is_audited(
    tmp_path: Path,
) -> None:
    from candlepilot.broker.user_stream import UserStreamEvent
    from candlepilot.domain.models import ExecutionReport

    class ManualCloseBroker(FakeTestnetBroker):
        def __init__(self) -> None:
            super().__init__()
            self.closed_symbols: list[str] = []
            self.fill_queries = 0

        async def close_position_market(self, symbol: str) -> ExecutionReport:
            self.closed_symbols.append(symbol)
            return ExecutionReport(
                client_order_id="cp-manual-test",
                status="FILLED",
                filled_quantity="0.25",
                average_price="60100",
                message="manual market close from account frontend",
            )

        async def completed_order_fill_event(
            self, symbol: str, client_order_id: str
        ) -> UserStreamEvent:
            self.fill_queries += 1
            now = datetime.now(UTC)
            return UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now,
                now,
                symbol,
                {
                    "_source": "rest_trade_reconciliation",
                    "o": {
                        "c": client_order_id,
                        "s": symbol,
                        "S": "SELL",
                        "x": "TRADE",
                        "X": "FILLED",
                        "z": "0.25",
                        "ap": "60100",
                        "R": True,
                        "rp": "25",
                    },
                },
            )

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'manual-close-api.db'}")
    broker = ManualCloseBroker()
    engine = TradingEngine(
        testnet_broker=broker,  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=ApiMarket(),  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=ApiMarket(), engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        engine.running = True
        blocked = client.post(
            "/api/account/positions/close", json={"symbol": "BTCUSDT"}
        )
        assert blocked.status_code == 409
        assert broker.closed_symbols == []

        engine.running = False
        response = client.post(
            "/api/account/positions/close", json={"symbol": "BTCUSDT"}
        )
        assert response.status_code == 200
        assert response.json()["status"] == "FILLED"
        assert response.json()["filled_quantity"] == "0.25"
        assert broker.closed_symbols == ["BTCUSDT"]
        orders = client.get("/api/orders").json()
        assert orders[0]["client_order_id"] == "cp-manual-test"
        assert orders[0]["symbol"] == "BTCUSDT"
        fills = client.get("/api/fills").json()
        assert fills[0]["source"] == "exchange_rest_reconciliation"
        assert fills[0]["side"] == "SELL"
        assert fills[0]["report"]["average_price"] == "60100"
        assert fills[0]["realized_pnl"] == "25"
        assert broker.fill_queries == 1
        assert client.get("/api/fills").json()[0]["side"] == "SELL"
        assert broker.fill_queries == 1

        assert (
            client.post("/api/account/positions/close", json={"symbol": "btc"}).status_code
            == 422
        )
    asyncio.run(database.close())


def test_missing_manual_fill_reconciliation_uses_retry_backoff(tmp_path: Path) -> None:
    from candlepilot.domain.models import ExecutionReport

    class MissingFillBroker(FakeTestnetBroker):
        def __init__(self) -> None:
            super().__init__()
            self.fill_queries = 0

        async def completed_order_fill_event(self, _symbol: str, _client_order_id: str):
            self.fill_queries += 1
            return None

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'manual-fill-backoff.db'}")
    broker = MissingFillBroker()
    engine = TradingEngine(
        testnet_broker=broker,  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=ApiMarket(),  # type: ignore[arg-type]
    )
    asyncio.run(database.initialize())
    asyncio.run(
        engine.audit.record_execution(
            "BTCUSDT",
            ExecutionReport(
                client_order_id="cp-manual-missing",
                status="FILLED",
                filled_quantity="0.1",
            ),
        )
    )
    app = create_app(database=database, market=ApiMarket(), engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        first = client.get("/api/fills").json()
        second = client.get("/api/fills").json()
        assert first[0]["source"] == "execution_audit"
        assert second[0]["source"] == "execution_audit"
        assert broker.fill_queries == 1
    asyncio.run(database.close())


def test_flat_entry_reconciles_protective_exit_but_open_position_is_skipped(
    tmp_path: Path,
) -> None:
    from candlepilot.broker.user_stream import UserStreamEvent
    from candlepilot.domain.models import ExecutionReport

    class ExitFillBroker(StatefulTestnetBroker):
        def __init__(self) -> None:
            super().__init__({"BTCUSDT": ("LONG", Decimal("0.1"), Decimal("65000"))})
            self.exit_queries: list[tuple[str, str]] = []

        async def completed_exit_fill_event(
            self, symbol: str, entry_client_order_id: str
        ) -> UserStreamEvent:
            self.exit_queries.append((symbol, entry_client_order_id))
            now = datetime.now(UTC)
            return UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now,
                now,
                symbol,
                {
                    "_source": "rest_trade_reconciliation",
                    "o": {
                        "c": f"{entry_client_order_id}-sl",
                        "s": symbol,
                        "S": "SELL",
                        "x": "TRADE",
                        "X": "FILLED",
                        "z": "1000",
                        "ap": "0.00194",
                        "R": True,
                        "rp": "-25",
                    },
                },
            )

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'offline-exit-fill.db'}")
    broker = ExitFillBroker()
    engine = TradingEngine(
        testnet_broker=broker,  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=ApiMarket(),  # type: ignore[arg-type]
    )
    asyncio.run(database.initialize())
    for symbol, client_order_id, quantity in (
        ("AKEUSDT", "cp-ake-entry", "1000"),
        ("BTCUSDT", "cp-btc-entry", "0.1"),
    ):
        asyncio.run(
            engine.audit.record_execution(
                symbol,
                ExecutionReport(
                    client_order_id=client_order_id,
                    status="FILLED",
                    filled_quantity=quantity,
                ),
            )
        )
    app = create_app(database=database, market=ApiMarket(), engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        fills = client.get("/api/fills").json()
        exit_fill = next(fill for fill in fills if fill["purpose"] == "stop_loss")
        assert exit_fill["client_order_id"] == "cp-ake-entry-sl"
        assert exit_fill["related_client_order_id"] == "cp-ake-entry"
        assert exit_fill["realized_pnl"] == "-25"
        assert broker.exit_queries == [("AKEUSDT", "cp-ake-entry")]
        assert client.get("/api/fills").status_code == 200
        assert broker.exit_queries == [("AKEUSDT", "cp-ake-entry")]
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
        assert listed["timeout_seconds"] == 45
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
        assert (
            client.post(
                "/api/providers/config",
                json={"name": "api-fixture", "pricing": "openai"},
            ).status_code
            == 422
        )

        # Locked while the engine runs.
        client.post("/api/providers/select", json={"providers": ["api-fixture"]})
        client.post("/api/engine/start")
        assert (
            client.post(
                "/api/providers/config", json={"name": "api-fixture", "model": "y"}
            ).status_code
            == 409
        )
    asyncio.run(database.close())


def test_custom_provider_config_switches_runtime_pricing_catalog(tmp_path: Path) -> None:
    from candlepilot.config import CustomLlmProvider
    from candlepilot.providers.pricing import parse_models_dev

    catalog = parse_models_dev(
        {
            "openai": {
                "models": {"gpt-5.6-sol": {"cost": {"input": 1, "output": 2}}}
            },
            "xai": {
                "models": {"grok-model": {"cost": {"input": 3, "output": 4}}}
            },
        }
    )

    async def loader(_cache_dir):
        return catalog

    settings = Settings(
        custom_llm_providers=(
            CustomLlmProvider(
                id="main",
                base_url="https://main.example/v1",
                pricing="openai",
            ),
        )
    )
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runtime-pricing.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([CustomApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(
        settings=settings,
        database=database,
        market=market,  # type: ignore[arg-type]
        engine=engine,
        pricing_loader=loader,
    )

    with TestClient(app) as client:
        listed = client.get("/api/providers").json()[0]
        assert listed["pricing"] == "openai"
        assert listed["pricing_options"] == ["openai", "xai"]
        assert "gpt-5.6-sol" in listed["model_options"]

        switched = client.post(
            "/api/providers/config",
            json={
                "name": "openai-compatible:main",
                "model": "grok-model",
                "pricing": "xai",
            },
        )
        assert switched.status_code == 200, switched.text
        configured = switched.json()[0]
        assert configured["pricing"] == "xai"
        assert configured["model"] == "grok-model"
        assert "grok-model" in configured["model_options"]
        assert "gpt-5.6-sol" not in configured["model_options"]

        intent = TradeIntent.hold("BTCUSDT", "5m", "runtime pricing")
        asyncio.run(
            engine.audit.record_inference(
                ProviderResult(
                    intent=intent,
                    provider="openai-compatible:main",
                    model="grok-model",
                    duration=timedelta(milliseconds=10),
                    raw_output=intent.model_dump_json(),
                    usage={
                        "input_tokens": 1000,
                        "output_tokens": 1000,
                        "total_tokens": 2000,
                    },
                )
            )
        )
        metrics = client.get("/api/metrics/providers").json()["providers"][0]
        assert abs(float(metrics["cost_usd_total"]) - 0.007) < 1e-9

        cleared = client.post(
            "/api/providers/config",
            json={
                "name": "openai-compatible:main",
                "model": "grok-model",
                "pricing": "",
            },
        ).json()[0]
        assert cleared["pricing"] is None
        assert cleared["model_options"] == ["grok-model"]
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
        assert Settings.from_mapping(read_env_file(env_path)).cadences == (
            "5m", "15m", "30m", "1h", "4h"
        )

        # Invalid values are rejected before the file is touched.
        before = env_path.read_text(encoding="utf-8")
        # These parse fine but would brick startup at engine/scheduler construction.
        for values in (
            {"CANDLEPILOT_CADENCES": "7m"},
            {"CANDLEPILOT_CANDIDATES_PER_CYCLE": "99"},
            {"CANDLEPILOT_HOST": "0.0.0.0"},
            {"CANDLEPILOT_MODE": "bogus"},
            {"CANDLEPILOT_PORT": "not-a-port"},
            {"CANDLEPILOT_PROVIDER_CHAIN": "custom:missing,codex"},
            {"CANDLEPILOT_DEFAULT_PROVIDER": "custom:missing"},
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
        # The list stays safe to render before the user asks to reveal a key.
        assert entry["id"] == "main"
        assert entry["base_url"] == "https://a.example/v1"
        assert entry["model"] == "m1"
        # The ordinary list reports only configured + masked state.
        assert entry["api_key_configured"] is True
        assert "sk-existing" not in listed.text
        # Header values are secrets too: only names are exposed.
        assert entry["extra_header_names"] == ["x-team"]
        assert "desk" not in listed.text

        # An explicit reveal request returns only this provider's key and must
        # not be cached by the browser or an intermediary.
        revealed = client.get("/api/custom-providers/main/api-key")
        assert revealed.status_code == 200
        assert revealed.json() == {"api_key": "sk-existing"}
        assert revealed.headers["cache-control"] == "no-store"
        assert revealed.headers["pragma"] == "no-cache"
        assert client.get("/api/custom-providers/missing/api-key").status_code == 404

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
        assert client.get("/api/custom-providers/main/api-key").status_code == 404

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


def test_custom_provider_id_change_requires_route_update(
    tmp_path: Path, monkeypatch
) -> None:
    env_path = tmp_path / ".env"
    original = (
        'CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON=[{"id":"main","base_url":"https://a.example/v1"}]\n'
        "CANDLEPILOT_PROVIDER_CHAIN=custom:main\n"
    )
    env_path.write_text(original, encoding="utf-8")
    monkeypatch.setenv("CANDLEPILOT_ENV_FILE", str(env_path))
    settings = Settings.from_mapping(read_env_file(env_path))
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'referenced-custom.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([CustomApiProvider(), ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(
        settings=settings,
        database=database,
        market=market,  # type: ignore[arg-type]
        engine=engine,
    )

    with TestClient(app) as client:
        renamed = client.post(
            "/api/custom-providers",
            json={"providers": [{"id": "renamed", "base_url": "https://a.example/v1"}]},
        )
        assert renamed.status_code == 422
        assert "CANDLEPILOT_PROVIDER_CHAIN" in renamed.json()["detail"]
        assert env_path.read_text(encoding="utf-8") == original
    asyncio.run(database.close())


def test_startup_rejects_unknown_provider_references(
    tmp_path: Path,
) -> None:
    database, engine, _app = _backtest_app(tmp_path, "stale-route.db")

    with pytest.raises(ValueError) as error:
        create_app(
            settings=Settings(
                provider_chain=("missing-custom", "api-fixture"),
            ),
            database=database,
            market=BacktestMarket(),  # type: ignore[arg-type]
            engine=engine,
        )
    message = str(error.value)
    assert "missing-custom" in message
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
        client.post("/api/providers/select", json={"providers": ["api-fixture"]})
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
        assert status["rescue_count"] == 0
        assert status["rescue_limit"] == 3

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
        client.post("/api/providers/select", json={"providers": ["api-fixture"]})
        client.post("/api/engine/start")
        assert client.post("/api/run-limits", json={"max_run_seconds": 60}).status_code == 409
    asyncio.run(database.close())


def test_provider_test_endpoint_reports_success_and_failure(tmp_path: Path) -> None:
    class UsageProvider(ApiProvider):
        async def generate_trade_intent(self, snapshot, portfolio):
            result = await super().generate_trade_intent(snapshot, portfolio)
            return replace(
                result,
                model="test-model",
                usage={
                    "input_tokens": 120,
                    "cached_input_tokens": 20,
                    "output_tokens": 30,
                    "total_tokens": 150,
                    "cost_usd": 0.004,
                },
            )

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test-provider.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([UsageProvider(), BrokenProvider()]),
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
        assert body["usage"] == {
            "tokens_reported": True,
            "input_tokens": 120,
            "cached_input_tokens": 20,
            "cache_creation_input_tokens": 0,
            "output_tokens": 30,
            "total_tokens": 150,
            "equivalent_cost_usd": 0.004,
        }

        broken = client.post("/api/providers/test", json={"name": "broken-fixture"}).json()
        assert broken["ok"] is False
        assert "bogus" in broken["detail"]

        assert client.post("/api/providers/test", json={"name": "missing"}).status_code == 404

        # The test call is not audited, so it leaves no inference/decision behind.
        assert client.get("/api/signals").json() == []

        # Locked while the engine runs.
        client.post("/api/providers/select", json={"providers": ["api-fixture"]})
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

        engine.running = True
        blocked = client.post(
            "/api/history/clear", json={"categories": ["inferences"]}
        )
        assert blocked.status_code == 409
        assert "formal decision engine" in blocked.json()["detail"]
        assert len(client.get("/api/signals").json()) == 1
        engine.running = False

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


def test_caller_supplied_decision_evaluation_is_not_exposed(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'no-manual-evaluate.db'}")
    market = ApiMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([ApiProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        response = client.post(
            "/api/decisions/evaluate",
            json={"snapshot": {}, "portfolio": {}, "rules": {}},
        )
        # A built frontend mounts StaticFiles and answers an unknown POST with
        # 405; a clean backend-only checkout has no mount and answers 404. The
        # security contract is that FastAPI publishes no executable API route.
        assert response.status_code in {404, 405}
        assert "/api/decisions/evaluate" not in client.get("/openapi.json").json()["paths"]
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
        assert status["active_cadences"] == ["5m", "15m", "30m", "1h", "4h"]
        assert status["supported_cadences"] == ["5m", "15m", "30m", "1h", "4h"]

        updated = client.post("/api/cadences", json={"cadences": ["4h", "1h"]})
        assert updated.status_code == 200, updated.text
        assert updated.json()["active_cadences"] == ["1h", "4h"]  # canonical order

        assert client.post("/api/cadences", json={"cadences": ["1m"]}).status_code == 422
        assert client.post("/api/cadences", json={"cadences": []}).status_code == 422

        # Locked while the engine runs.
        client.post("/api/providers/select", json={"providers": ["api-fixture"]})
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
        client.post("/api/providers/select", json={"providers": ["api-fixture"]})
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
        response = client.post("/api/providers/select", json={"providers": ["missing"]})
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
        retired = client.post(
            "/api/providers/select", json={"name": "api-fixture", "backup": None}
        )
        assert retired.status_code == 422
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
        step = {
            "5m": 300_000,
            "15m": 900_000,
            "30m": 1_800_000,
            "1h": 3_600_000,
            "4h": 14_400_000,
            "1d": 86_400_000,
        }[interval]
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


def _await_run(client: TestClient, run_id: int = 1) -> dict[str, object]:
    """Poll until the background run leaves `running`, rather than sleeping."""

    for _ in range(400):
        body = client.get(f"/api/backtests/{run_id}").json()
        if body["status"] != "running":
            return body
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never finished")


def _window(hours: int = 1) -> dict[str, str]:
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    return {
        "start": (end - timedelta(hours=hours)).isoformat(),
        "end": end.isoformat(),
    }


def _complete_probe(client: TestClient, payload: dict[str, object]) -> dict[str, object]:
    started = client.post("/api/backtests/probe", json=payload)
    assert started.status_code == 202, started.text
    for _ in range(500):
        body = client.get("/api/backtests/probe").json()
        if not body["running"] and body["providers"]:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("probe never finished")
    assert all(item["done"] for item in body["providers"]), body
    assert all(item["failures"] == 0 for item in body["providers"]), body
    return body


def _start_backtest(client: TestClient, payload: dict[str, object]):
    _complete_probe(client, payload)
    return client.post("/api/backtests", json=payload)


def test_backtest_estimate_counts_calls_before_any_are_paid_for(tmp_path: Path) -> None:
    database, _engine, app = _backtest_app(tmp_path, "bt-estimate.db")

    with TestClient(app) as client:
        payload = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(2)}
        _complete_probe(client, payload)
        response = client.post(
            "/api/backtests/estimate",
            json=payload,
        )
        assert response.status_code == 200
        body = response.json()
        # Two hours of 5m bars.
        assert body["decisions_per_model"] == 24
        assert body["total_calls"] == 24
        assert body["slowest_provider"] == "api-fixture"
        assert body["latency_source"] == "probe_slowest_average"
        assert body["within_limit"] is True
    asyncio.run(database.close())


def test_participating_providers_probe_in_parallel_and_stale_data_is_rejected(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bt-probe-estimate.db'}")
    market = BacktestMarket()
    first_calls: set[str] = set()
    both_started = asyncio.Event()

    async def rendezvous(name: str) -> None:
        first_calls.add(name)
        if len(first_calls) == 2:
            both_started.set()
        await asyncio.wait_for(both_started.wait(), timeout=1)

    class Fast(ApiProvider):
        name = "fast-fixture"

        async def generate_trade_intent(self, snapshot, portfolio):
            await rendezvous(self.name)
            return await super().generate_trade_intent(snapshot, portfolio)

    class Slow(ApiProvider):
        name = "slow-fixture"

        async def generate_trade_intent(self, snapshot, portfolio):
            await rendezvous(self.name)
            return await super().generate_trade_intent(snapshot, portfolio)

    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([Fast(), Slow()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    window = _window(2)
    payload = {
        "symbols": ["BTCUSDT"],
        "providers": ["fast-fixture", "slow-fixture"],
        **window,
    }

    with TestClient(app) as client:
        _complete_probe(client, payload)
        estimate_response = client.post("/api/backtests/estimate", json=payload)
        assert estimate_response.status_code == 200
        estimate_body = estimate_response.json()
        assert estimate_body["slowest_provider"] in payload["providers"]
        assert estimate_body["seconds_per_call"] >= 0
        assert estimate_body["total_calls"] == 48
        assert first_calls == {"fast-fixture", "slow-fixture"}

        stale = {
            **payload,
            "start": (datetime.fromisoformat(window["start"]) + timedelta(minutes=5)).isoformat(),
        }
        stale_response = client.post("/api/backtests/estimate", json=stale)
        assert stale_response.status_code == 422
        assert "no matching probe" in stale_response.json()["detail"]
    asyncio.run(database.close())


def test_estimate_rejects_a_probe_with_any_failed_call(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bt-probe-failed.db'}")
    market = BacktestMarket()

    class Flaky(ApiProvider):
        calls = 0

        async def generate_trade_intent(self, snapshot, portfolio):
            Flaky.calls += 1
            if Flaky.calls == 1:
                raise RuntimeError("transient failure")
            return await super().generate_trade_intent(snapshot, portfolio)

    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([Flaky()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    payload = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window()}

    with TestClient(app) as client:
        assert client.post("/api/backtests/probe", json=payload).status_code == 202
        for _ in range(500):
            status = client.get("/api/backtests/probe").json()
            if not status["running"]:
                break
            time.sleep(0.02)
        response = client.post("/api/backtests/estimate", json=payload)
        assert response.status_code == 422
        assert "does not have 5 successful calls" in response.json()["detail"]
    asyncio.run(database.close())


def test_backtest_requires_a_probe_for_the_current_settings(tmp_path: Path) -> None:
    """An unrelated historical average must not authorize a paid run."""

    database, _engine, app = _backtest_app(tmp_path, "bt-limit.db")

    with TestClient(app) as client:
        response = client.post(
            "/api/backtests",
            json={"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window()},
        )
        assert response.status_code == 422
        assert "fresh 5-decision probe" in response.json()["detail"]
    asyncio.run(database.close())


def test_local_rule_backtest_estimates_and_starts_without_a_probe(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bt-local-rule.db'}")
    market = BacktestMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([LocalRuleProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    payload = {"symbols": ["BTCUSDT"], "providers": ["local-rule"], **_window()}

    with TestClient(app) as client:
        probe = client.post("/api/backtests/probe", json=payload)
        assert probe.status_code == 422
        assert "do not require a probe" in probe.json()["detail"]

        estimate_response = client.post("/api/backtests/estimate", json=payload)
        assert estimate_response.status_code == 200
        estimate_body = estimate_response.json()
        assert estimate_body["latency_source"] == "local_deterministic"
        assert estimate_body["slowest_provider"] == "local-rule"

        created = client.post("/api/backtests", json=payload)
        assert created.status_code == 202
        run = _await_run(client)
        assert run["status"] == "completed"
        assert run["spec"]["timeout_source"] == "not_applicable"
        assert run["models"][0]["usage"]["total_tokens"] == 0
        assert run["models"][0]["usage"]["equivalent_cost_usd"] == 0
    asyncio.run(database.close())


def test_backtest_is_refused_while_the_engine_runs(tmp_path: Path) -> None:
    """They share a provider, and each provider serialises its own calls."""

    database, engine, app = _backtest_app(tmp_path, "bt-busy.db")

    with TestClient(app) as client:
        client.post("/api/providers/select", json={"providers": ["api-fixture"]})
        assert client.post("/api/engine/start").json()["running"] is True

        response = client.post(
            "/api/backtests",
            json={"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window()},
        )

        assert response.status_code == 409
        assert "stop the engine" in response.json()["detail"]
    asyncio.run(database.close())


def test_backtest_runs_in_the_background_and_reports_each_model(tmp_path: Path) -> None:
    database, engine, app = _backtest_app(tmp_path, "bt-run.db")
    provider = engine.providers.get("api-fixture")
    provider.model = "fixture-model"
    provider.reasoning_effort = "high"

    with TestClient(app) as client:
        payload = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window()}
        created = _start_backtest(client, payload)
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
        assert model["model"] == "fixture-model"
        assert model["reasoning_effort"] == "high"
        assert run["spec"]["provider_configs"]["api-fixture"] == {
            "model": "fixture-model",
            "reasoning_effort": "high",
        }
        assert model["decisions_done"] == model["decisions_total"] == 12
        assert model["progress"] == 1.0
        # The fixture provider only ever holds, so nothing traded.
        assert model["result"]["trade_count"] == 0
        assert client.get("/api/backtests").json()[0]["id"] == run_id
    asyncio.run(database.close())


def test_running_backtest_exposes_each_completed_decision_immediately(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bt-live-decisions.db'}")
    market = BacktestMarket()

    class SlowProvider(ApiProvider):
        async def generate_trade_intent(self, snapshot, portfolio):
            await asyncio.sleep(0.15)
            return await super().generate_trade_intent(snapshot, portfolio)

    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([SlowProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        payload = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window()}
        created = _start_backtest(client, payload)
        assert created.status_code == 202
        run_id = created.json()["id"]
        for _ in range(100):
            decisions = client.get(f"/api/backtests/{run_id}/decisions").json()
            run = client.get(f"/api/backtests/{run_id}").json()
            if decisions:
                break
            time.sleep(0.03)
        else:
            raise AssertionError("the first completed decision was not exposed")

        assert run["status"] == "running"
        assert len(decisions) >= 1
        client.post(f"/api/backtests/{run_id}/cancel")

    asyncio.run(database.close())


def test_app_shutdown_cancels_background_work_before_closing_resources(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'shutdown-work.db'}")
    market = BacktestMarket()

    class NeverReturns(ApiProvider):
        async def generate_trade_intent(self, snapshot, portfolio):
            if self.timeout == PROBE_CEILING_SECONDS:
                return await super().generate_trade_intent(snapshot, portfolio)
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([NeverReturns()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        assert client.post(
            "/api/collector/start", json={"symbols": ["BTCUSDT"]}
        ).status_code == 200
        payload = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)}
        created = _start_backtest(client, payload)
        assert created.status_code == 202
        run_id = created.json()["id"]
        for _ in range(200):
            model = client.get(f"/api/backtests/{run_id}").json()["models"][0]
            if model["decisions_total"] > 0:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("backtest never reached its model call")

    async def state_after_shutdown():
        run = await engine.audit.backtest_run(run_id)
        await database.close()
        return run

    run = asyncio.run(state_after_shutdown())
    assert app.state.collector.running is False
    assert engine.running is False
    assert run is not None and run["status"] == "cancelled"


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
        payload = {
            "symbols": ["BTCUSDT"], "providers": ["api-fixture"],
            "use_recorded_book": True, **window,
        }
        created = _start_backtest(client, payload)
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
        payload = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)}
        created = _start_backtest(client, payload)
        assert created.status_code == 202
    asyncio.run(database.close())


def test_a_running_backtest_reports_progress_over_the_api(tmp_path: Path) -> None:
    """The frontend polls this endpoint, so 0% here is 0% on screen.

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

        run_calls = 0

        async def generate_trade_intent(self, snapshot, portfolio):
            if self.timeout != PROBE_CEILING_SECONDS:
                Gated.run_calls += 1
            if Gated.run_calls == 2:
                await gate.wait()
            result = await super().generate_trade_intent(snapshot, portfolio)
            return ProviderResult(
                result.intent,
                result.provider,
                "fixture-model",
                timedelta(milliseconds=250),
                result.raw_output,
                {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120,
                 "cost_usd": 0.0025},
            )

    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([Gated()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        payload = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)}
        created = _start_backtest(client, payload)
        assert created.status_code == 202

        # Wait for the run to reach the gate rather than sleeping a fixed span.
        for _ in range(200):
            model = client.get("/api/backtests").json()[0]["models"][0]
            if model["decisions_total"] and model["decisions_done"] >= 1:
                seen.append(model)
                break
            time.sleep(0.02)
        assert client.post("/api/engine/start").status_code == 409
        assert client.post(
            "/api/backtests",
            json={"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)},
        ).status_code == 409
        assert client.post(
            "/api/backtests/probe",
            json={"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)},
        ).status_code == 409
        assert client.post(
            "/api/providers/test", json={"name": "api-fixture"}
        ).status_code == 409
        assert client.post(
            "/api/providers/config",
            json={"name": "api-fixture", "model": None, "reasoning_effort": None},
        ).status_code == 409
        gate.set()

    assert seen, "the API never showed a decision while the run was in flight"
    mid = seen[0]
    # A denominator, and a numerator that is neither 0 nor already finished.
    assert mid["decisions_total"] == 12
    assert 0 < mid["progress"] < 1
    assert mid["usage"]["total_tokens"] == 120
    assert mid["usage"]["equivalent_cost_usd"] == 0.0025
    assert mid["usage"]["average_duration_ms"] == 250
    assert mid["elapsed_seconds"] > 0
    assert mid["remaining_seconds"] > 0
    assert mid["live_result"] is not None
    assert set(mid["live_result"]) == {
        "equity",
        "unrealized_pnl",
        "total_return",
        "max_drawdown",
        "win_rate",
        "trade_count",
    }
    assert mid["live_result"]["trade_count"] == 0
    asyncio.run(database.close())


def test_a_provider_failure_stops_and_truncates_the_backtest(
    tmp_path: Path, monkeypatch
) -> None:
    """Three failed attempts stop at the last decision the Provider completed."""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bt-unreliable.db'}")
    market = BacktestMarket()
    monkeypatch.setattr(
        "candlepilot.backtest.runner.DECISION_PROVIDER_RETRY_DELAYS", (0, 0)
    )

    class Timeouts(ApiProvider):
        run_calls = 0

        async def generate_trade_intent(self, snapshot, portfolio):
            if self.timeout != PROBE_CEILING_SECONDS:
                Timeouts.run_calls += 1
            if Timeouts.run_calls in {2, 3, 4}:
                raise RuntimeError("endpoint timed out after 45s")
            return await super().generate_trade_intent(snapshot, portfolio)

    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([Timeouts()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    window = _window(1)

    with TestClient(app) as client:
        payload = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **window}
        assert _start_backtest(client, payload).status_code == 202
        run = _await_run(client)

    assert run["status"] == "failed"
    assert "became unavailable after 3 attempts" in run["error"]
    assert run["spec"]["requested_end"] == window["end"]
    expected_end = datetime.fromisoformat(window["start"]) + timedelta(minutes=5)
    assert datetime.fromisoformat(run["spec"]["end"]) == expected_end
    assert run["models"][0]["result"] is not None
    assert run["models"][0]["decisions_done"] == 2
    assert run["models"][0]["calls_failed"] == 1
    assert datetime.fromisoformat(
        run["models"][0]["result"]["equity_curve"][-1]["timestamp"]
    ) == expected_end
    asyncio.run(database.close())


def test_a_clean_run_is_still_completed(tmp_path: Path) -> None:
    """The flag must not fire on the runs it is meant to leave alone."""

    database, _engine, app = _backtest_app(tmp_path, "bt-clean.db")

    with TestClient(app) as client:
        payload = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)}
        assert _start_backtest(client, payload).status_code == 202
        run = _await_run(client)

    assert run["status"] == "completed"
    assert run["error"] is None
    asyncio.run(database.close())


def test_the_run_timeout_reaches_the_provider_and_is_handed_back(tmp_path: Path) -> None:
    """The registry's providers are shared with the live engine.

    An override that leaked would silently re-time every later live inference.
    """

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bt-timeout.db'}")
    market = BacktestMarket()
    seen: list[float] = []

    class Recording(ApiProvider):
        async def generate_trade_intent(self, snapshot, portfolio):
            if self.timeout != PROBE_CEILING_SECONDS:
                seen.append(self.timeout)
            return await super().generate_trade_intent(snapshot, portfolio)

    provider = Recording()
    provider.timeout = 45
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([provider]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        payload = {
            "symbols": ["BTCUSDT"],
            "providers": ["api-fixture"],
            "timeout_seconds": 90,
            **_window(1),
        }
        assert _start_backtest(client, payload).status_code == 202
        _await_run(client)
        # Recorded on the run: a failure count means nothing if nothing says
        # which timeout produced it.
        spec = client.get("/api/backtests/1").json()["spec"]
        assert spec["timeout_seconds"] == 90
        assert spec["timeout_source"] == "explicit"

    assert seen and set(seen) == {90.0}
    assert provider.timeout == 45
    asyncio.run(database.close())


def test_the_configured_timeout_is_frozen_on_the_run(tmp_path: Path) -> None:
    """An inherited timeout must be visible and stable, not discovered on failure."""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bt-inherited-timeout.db'}")
    market = BacktestMarket()
    seen: list[float] = []

    class Recording(ApiProvider):
        async def generate_trade_intent(self, snapshot, portfolio):
            if self.timeout != PROBE_CEILING_SECONDS:
                seen.append(self.timeout)
            return await super().generate_trade_intent(snapshot, portfolio)

    provider = Recording()
    provider.timeout = 60
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([provider]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        payload = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)}
        assert _start_backtest(client, payload).status_code == 202
        _await_run(client)
        spec = client.get("/api/backtests/1").json()["spec"]
        assert spec["timeout_seconds"] == 60
        assert spec["timeout_source"] == "provider_config"

    assert seen and set(seen) == {60.0}
    assert provider.timeout == 60
    asyncio.run(database.close())


def test_the_probe_times_real_calls_and_suggests_a_timeout(tmp_path: Path) -> None:
    """Guessing the timeout cost a run 5 of its 12 decisions.

    The probe is the only way to learn what the endpoint actually needs, so it
    has to reach the endpoint and report what it saw.
    """

    database, _engine, app = _backtest_app(tmp_path, "bt-probe.db")

    with TestClient(app) as client:
        started = client.post(
            "/api/backtests/probe",
            json={"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)},
        )
        assert started.status_code == 202
        assert started.json()["decisions"] == PROBE_DECISIONS

        for _ in range(200):
            body = client.get("/api/backtests/probe").json()
            if not body["running"] and body["providers"]:
                break
            time.sleep(0.02)

    assert body["running"] is False
    probe = body["providers"][0]
    assert probe["provider"] == "api-fixture"
    assert len(probe["calls"]) == PROBE_DECISIONS
    assert all(call["ok"] for call in probe["calls"])
    assert probe["failures"] == 0
    assert probe["suggested_timeout_seconds"] is not None
    asyncio.run(database.close())


def test_probing_is_refused_while_the_engine_runs(tmp_path: Path) -> None:
    """It borrows the same provider the live loop is queueing on."""

    database, engine, app = _backtest_app(tmp_path, "bt-probe-busy.db")
    engine.running = True

    with TestClient(app) as client:
        response = client.post(
            "/api/backtests/probe",
            json={"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)},
        )
        assert response.status_code == 409
    asyncio.run(database.close())


def test_every_backtest_decision_is_readable_afterwards(tmp_path: Path) -> None:
    """A run reported totals only, so "0 trades" had no explanation.

    Held, vetoed and failed all produce the same zero; the stored decisions are
    what separate them.
    """

    database, _engine, app = _backtest_app(tmp_path, "bt-decisions.db")

    with TestClient(app) as client:
        payload = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)}
        assert _start_backtest(client, payload).status_code == 202
        run = _await_run(client)
        decisions = client.get("/api/backtests/1/decisions").json()

    # One per decision the run counted -- including the tail, which the final
    # batch has to flush.
    assert len(decisions) == run["models"][0]["decisions_total"] == 12
    first = decisions[0]
    assert first["symbol"] == "BTCUSDT"
    assert first["cadence"] == "5m"
    assert len(first["attempt_started_at"]) == 1
    assert datetime.fromisoformat(first["attempt_started_at"][0]).tzinfo is not None
    # The api fixture always holds, so the reason for zero trades is legible.
    assert {item["outcome"] for item in decisions} == {"hold"}
    assert first["rationale"] == "fixture"
    assert first["provider"] == "api-fixture"
    assert first["decided_at"]
    # In decision order, not insertion race order.
    stamps = [item["decided_at"] for item in decisions]
    assert stamps == sorted(stamps)
    asyncio.run(database.close())


def test_decisions_are_filtered_to_one_model(tmp_path: Path) -> None:
    database, _engine, app = _backtest_app(tmp_path, "bt-decisions-filter.db")

    with TestClient(app) as client:
        payload = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)}
        _start_backtest(client, payload)
        _await_run(client)
        mine = client.get("/api/backtests/1/decisions?provider=api-fixture").json()
        other = client.get("/api/backtests/1/decisions?provider=nobody").json()
        missing = client.get("/api/backtests/999/decisions")

    assert len(mine) == 12
    assert other == []
    assert missing.status_code == 404
    asyncio.run(database.close())


def test_clearing_backtests_takes_the_decisions_with_them(tmp_path: Path) -> None:
    """Decisions cascade from the run; a stale orphan would outlive its parent."""

    database, _engine, app = _backtest_app(tmp_path, "bt-decisions-clear.db")

    with TestClient(app) as client:
        payload = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)}
        _start_backtest(client, payload)
        _await_run(client)
        assert len(client.get("/api/backtests/1/decisions").json()) == 12

        cleared = client.post("/api/history/clear", json={"categories": ["backtests"]})
        assert cleared.status_code == 200
        assert client.get("/api/backtests").json() == []
        assert client.get("/api/backtests/1/decisions").status_code == 404
    asyncio.run(database.close())


def test_a_running_probe_shows_each_call_as_it_lands(tmp_path: Path) -> None:
    """The probe published nothing until all five calls were done.

    At PROBE_CEILING_SECONDS that is fifteen minutes of a spinner that looks the
    same as a hang -- against the one thing whose slowness is the whole point.
    """

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'probe-live.db'}")
    market = BacktestMarket()
    gate = asyncio.Event()

    class Gated(ApiProvider):
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
    mid: dict[str, object] = {}

    with TestClient(app) as client:
        client.post(
            "/api/backtests/probe",
            json={"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)},
        )
        # Held at call 2, so exactly one call has landed and one is waiting.
        for _ in range(300):
            body = client.get("/api/backtests/probe").json()
            if body["providers"] and body["providers"][0]["calls"]:
                mid = body["providers"][0]
                break
            time.sleep(0.02)
        assert client.post("/api/engine/start").status_code == 409
        assert client.post(
            "/api/backtests",
            json={"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)},
        ).status_code == 409
        assert client.post(
            "/api/providers/test", json={"name": "api-fixture"}
        ).status_code == 409
        blocked_clear = client.post(
            "/api/history/clear", json={"categories": ["backtests"]}
        )
        assert blocked_clear.status_code == 409
        assert "backtest or probe" in blocked_clear.json()["detail"]
        gate.set()

    assert mid, "the probe published nothing while it was running"
    assert len(mid["calls"]) == 1
    assert mid["done"] is False
    # The elapsed clock is the only thing that moves while an endpoint thinks.
    assert mid["in_flight_seconds"] is not None
    asyncio.run(database.close())


def test_a_probe_can_be_abandoned(tmp_path: Path) -> None:
    """Five calls at the ceiling is fifteen minutes; there has to be a way out."""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'probe-cancel.db'}")
    market = BacktestMarket()
    forever = asyncio.Event()

    class Hangs(ApiProvider):
        async def generate_trade_intent(self, snapshot, portfolio):
            await forever.wait()
            raise AssertionError("unreachable")

    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([Hangs()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]

    with TestClient(app) as client:
        assert client.post("/api/backtests/probe/cancel").status_code == 409
        client.post(
            "/api/backtests/probe",
            json={"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)},
        )
        # Wait for a call to actually be in flight. Cancelling on `running`
        # alone lands during the history load instead, which is a different
        # path -- and the one this test used to pass on while the real one was
        # broken.
        for _ in range(300):
            body = client.get("/api/backtests/probe").json()
            if body["providers"] and body["providers"][0]["in_flight_seconds"] is not None:
                break
            time.sleep(0.02)
        else:
            raise AssertionError("no call ever went in flight")
        assert client.post("/api/backtests/probe/cancel").json() == {"cancelled": True}
        for _ in range(300):
            if not client.get("/api/backtests/probe").json()["running"]:
                break
            time.sleep(0.02)
        body = client.get("/api/backtests/probe").json()

    assert body["running"] is False
    assert body["providers"][0]["error"] == "cancelled"
    assert body["providers"][0]["done"] is True
    asyncio.run(database.close())
