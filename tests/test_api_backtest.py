import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import update

from candlepilot.api import create_app
from candlepilot.application.engine import TradingEngine
from candlepilot.backtest.probe import PROBE_CEILING_SECONDS, PROBE_DECISIONS
from candlepilot.config import Settings
from conftest import FakeTestnetBroker
from candlepilot.domain.models import MarketSnapshot, PortfolioState
from candlepilot.providers.base import ProviderResult
from candlepilot.providers.local import LocalRuleProvider
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.storage.database import (
    AuditRepository,
    Database,
    LiveRunRow,
)

from api_fixtures import ApiMarket, ApiProvider

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
    app = create_app(
        settings=Settings(data_dir=tmp_path / "data"),
        database=database,
        market=market,
        engine=engine,
    )  # type: ignore[arg-type]
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


def test_backtest_estimate_rejects_invalid_time_windows(tmp_path: Path) -> None:
    database, _engine, app = _backtest_app(tmp_path, "bt-invalid-window.db")
    start = datetime.now(UTC).replace(minute=1, second=0, microsecond=0) - timedelta(hours=2)

    with TestClient(app) as client:
        base = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"]}
        naive = client.post(
            "/api/backtests/estimate",
            json={
                **base,
                "start": start.replace(tzinfo=None).isoformat(),
                "end": (start + timedelta(minutes=10)).replace(tzinfo=None).isoformat(),
            },
        )
        assert naive.status_code == 422
        assert "must include a timezone" in naive.json()["detail"]

        no_close = client.post(
            "/api/backtests/estimate",
            json={
                **base,
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=3)).isoformat(),
            },
        )
        assert no_close.status_code == 422
        assert "contains no closed decision bar" in no_close.json()["detail"]

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


def test_local_rule_variants_backtest_without_a_probe(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'bt-local-rule.db'}")
    market = BacktestMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry(
            [
                LocalRuleProvider(),
                LocalRuleProvider("structure"),
                LocalRuleProvider("flow"),
                LocalRuleProvider("structure-flow"),
            ]
        ),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    provider_names = [
        "local-rule",
        "local-structure-shadow",
        "local-flow-shadow",
        "local-structure-flow-shadow",
    ]
    payload = {"symbols": ["BTCUSDT"], "providers": provider_names, **_window()}

    with TestClient(app) as client:
        probe = client.post("/api/backtests/probe", json=payload)
        assert probe.status_code == 422
        assert "do not require a probe" in probe.json()["detail"]

        estimate_response = client.post("/api/backtests/estimate", json=payload)
        assert estimate_response.status_code == 200
        estimate_body = estimate_response.json()
        assert estimate_body["latency_source"] == "local_deterministic"
        assert estimate_body["slowest_provider"] in provider_names

        created = client.post("/api/backtests", json=payload)
        assert created.status_code == 202
        run = _await_run(client)
        assert run["status"] == "completed"
        assert run["spec"]["timeout_source"] == "not_applicable"
        assert {model["provider"] for model in run["models"]} == set(provider_names)
        assert all(model["usage"]["total_tokens"] == 0 for model in run["models"])
        assert all(
            model["usage"]["equivalent_cost_usd"] == 0 for model in run["models"]
        )
    asyncio.run(database.close())


def test_backtest_is_refused_while_the_engine_runs(tmp_path: Path) -> None:
    """They share a provider, and each provider serialises its own calls."""

    database, engine, app = _backtest_app(tmp_path, "bt-busy.db")

    with TestClient(app) as client:
        client.post("/api/providers/select", json={"providers": ["api-fixture"]})
        assert client.post("/api/engine/probe").status_code == 200
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
            decisions = client.get(f"/api/backtests/{run_id}/decisions").json()["items"]
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
    assert engine.running is False
    assert run is not None and run["status"] == "cancelled"


def test_cancel_during_history_load_records_cancelled_terminal_state(tmp_path: Path) -> None:
    history_started = threading.Event()

    class SlowHistoryMarket(BacktestMarket):
        async def historical_klines(self, *args, **kwargs):
            history_started.set()
            await asyncio.Event().wait()
            raise AssertionError("cancelled history request resumed")

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cancel-history.db'}")
    market = SlowHistoryMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([LocalRuleProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    app = create_app(
        settings=Settings(data_dir=tmp_path / "slow-history-data"),
        database=database,
        market=market,
        engine=engine,
    )  # type: ignore[arg-type]

    with TestClient(app) as client:
        created = client.post(
            "/api/backtests",
            json={"symbols": ["BTCUSDT"], "providers": ["local-rule"], **_window()},
        )
        assert created.status_code == 202, created.text
        run_id = created.json()["id"]
        assert history_started.wait(timeout=1)
        refused_restart = client.post("/api/restart")
        assert refused_restart.status_code == 409
        assert "provider probe or backtest" in refused_restart.json()["detail"]
        assert client.post(f"/api/backtests/{run_id}/cancel").status_code == 200
        run = _await_run(client, run_id)

    assert run["status"] == "cancelled"
    assert run["ended_at"] is not None
    asyncio.run(database.close())


def test_startup_fails_a_backtest_left_running_by_previous_process(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'stale-backtest.db'}")
    market = BacktestMarket()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([LocalRuleProvider()]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )

    async def seed() -> int:
        await database.initialize()
        return await engine.audit.create_backtest_run(
            {"symbols": ["BTCUSDT"], **_window()}, ["local-rule"]
        )

    run_id = asyncio.run(seed())
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
    with TestClient(app) as client:
        run = client.get(f"/api/backtests/{run_id}").json()

    assert run["status"] == "failed"
    assert run["ended_at"] is not None
    assert run["error"] == "process restarted before the backtest closed cleanly"
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


def test_manual_book_collection_and_recorded_book_requests_are_removed(
    tmp_path: Path,
) -> None:
    database, _engine, app = _backtest_app(tmp_path, "bt-no-collector.db")

    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]
        assert not any(path.startswith("/api/collector") for path in paths)
        response = client.post(
            "/api/backtests",
            json={
                "symbols": ["BTCUSDT"],
                "providers": ["api-fixture"],
                "use_recorded_book": True,
                **_window(1),
            },
        )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"
    asyncio.run(database.close())


def test_a_plain_backtest_does_not_need_any_captures(tmp_path: Path) -> None:
    database, _engine, app = _backtest_app(tmp_path, "bt-plain.db")

    with TestClient(app) as client:
        payload = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)}
        created = _start_backtest(client, payload)
        assert created.status_code == 202
    asyncio.run(database.close())


def test_formal_run_replay_uses_exact_snapshot_count_and_starting_account(
    tmp_path: Path,
) -> None:
    database, engine, app = _backtest_app(tmp_path, "bt-formal-replay.db")
    window = _window(1)
    start = datetime.fromisoformat(window["start"])
    end = datetime.fromisoformat(window["end"])
    initial = PortfolioState(equity="1234", available_balance="1234")
    snapshot = MarketSnapshot(
        symbol="BTCUSDT",
        cadence="5m",
        timestamp=start + timedelta(minutes=5),
        mark_price="100",
        bid="99.9",
        ask="100.1",
        quote_volume_24h="1000000",
        features={"5m_ema_20": 99.0},
    )

    async def seed() -> int:
        await database.initialize()
        run_id = await engine.audit.create_live_run(
            {"initial_portfolio": initial.model_dump(mode="json")}
        )
        async with database.sessions.begin() as session:
            await session.execute(
                update(LiveRunRow).where(LiveRunRow.id == run_id).values(started_at=start)
            )
        await engine.audit.record_live_decision_snapshots(
            run_id,
            [
                {
                    "batch_id": "00000000-0000-0000-0000-000000000001",
                    "symbol": "BTCUSDT",
                    "cadence": "5m",
                    "captured_at": snapshot.timestamp,
                    "market": snapshot.model_dump(mode="json"),
                    "portfolio": initial.model_dump(mode="json"),
                    "rules": {
                        "quantity_step": "0.001",
                        "min_quantity": "0.001",
                        "min_notional": "5",
                        "tick_size": "0.01",
                        "max_quantity": None,
                        "market_quantity_step": None,
                        "market_min_quantity": None,
                        "market_max_quantity": None,
                    },
                }
            ],
        )
        await engine.audit.finish_live_run(
            run_id, status="stopped", stop_reason="fixture", ended_at=end
        )
        return run_id

    live_run_id = asyncio.run(seed())
    payload = {
        "symbols": ["ETHUSDT"],
        "providers": ["api-fixture"],
        "replay_live_run_id": live_run_id,
        **window,
    }
    with TestClient(app) as client:
        formal_runs = client.get("/api/backtests/formal-runs").json()
        assert formal_runs[0]["snapshot_count"] == 1
        assert formal_runs[0]["symbols"] == ["BTCUSDT"]
        _complete_probe(client, payload)
        estimate_response = client.post("/api/backtests/estimate", json=payload)
        assert estimate_response.status_code == 200
        assert estimate_response.json()["decisions_per_model"] == 1
        created = client.post("/api/backtests", json=payload)
        assert created.status_code == 202, created.text
        run = _await_run(client, created.json()["id"])
        assert run["status"] == "completed", run.get("error")
        assert run["spec"]["symbols"] == ["BTCUSDT"]
        assert run["spec"]["replay_live_run_id"] == live_run_id
        assert run["models"][0]["result"]["initial_equity"] == "1234"
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


def test_an_unexpected_model_error_marks_the_backtest_failed(
    tmp_path: Path, monkeypatch
) -> None:
    database, _engine, app = _backtest_app(tmp_path, "bt-model-error.db")

    async def crash(*args, **kwargs):
        raise RuntimeError("risk calculation crashed")

    monkeypatch.setattr("candlepilot.api.BacktestRunner.run", crash)

    with TestClient(app) as client:
        payload = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)}
        assert _start_backtest(client, payload).status_code == 202
        run = _await_run(client)

    assert run["status"] == "failed"
    assert run["error"] == "api-fixture: risk calculation crashed"
    assert run["models"][0]["error"] == "risk calculation crashed"
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
        page = client.get("/api/backtests/1/decisions").json()
        decisions = page["items"]
        first_page = client.get("/api/backtests/1/decisions?limit=5").json()
        second_page = client.get(
            "/api/backtests/1/decisions",
            params={"limit": 5, "after_id": first_page["next_after_id"]},
        ).json()
        third_page = client.get(
            "/api/backtests/1/decisions",
            params={"limit": 5, "after_id": second_page["next_after_id"]},
        ).json()
        assert client.get("/api/backtests/1/decisions?limit=501").status_code == 422
        assert client.get("/api/backtests/1/decisions?after_id=-1").status_code == 422

    # One per decision the run counted -- including the tail, which the final
    # batch has to flush.
    assert len(decisions) == run["models"][0]["decisions_total"] == 12
    assert page["total"] == 12
    assert page["has_more"] is False
    assert page["next_after_id"] is None
    paged = first_page["items"] + second_page["items"] + third_page["items"]
    assert [item["id"] for item in paged] == [item["id"] for item in decisions]
    assert [first_page["has_more"], second_page["has_more"], third_page["has_more"]] == [
        True,
        True,
        False,
    ]
    assert third_page["next_after_id"] is None
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

    assert len(mine["items"]) == 12
    assert mine["total"] == 12
    assert other == {"items": [], "total": 0, "has_more": False, "next_after_id": None}
    assert missing.status_code == 404
    asyncio.run(database.close())


def test_clearing_backtests_takes_the_decisions_with_them(tmp_path: Path) -> None:
    """Decisions cascade from the run; a stale orphan would outlive its parent."""

    database, _engine, app = _backtest_app(tmp_path, "bt-decisions-clear.db")

    with TestClient(app) as client:
        payload = {"symbols": ["BTCUSDT"], "providers": ["api-fixture"], **_window(1)}
        _start_backtest(client, payload)
        _await_run(client)
        assert len(client.get("/api/backtests/1/decisions").json()["items"]) == 12

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
