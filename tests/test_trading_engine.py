import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from candlepilot.application.engine import TradingEngine
from candlepilot.broker.user_stream import UserStreamEvent
from candlepilot.domain.models import (
    ExecutionReport,
    MarketSnapshot,
    OrderType,
    PortfolioState,
    PositionState,
    ProviderHealth,
    RiskDecision,
    TradeAction,
    TradeIntent,
)
from candlepilot.market.scanner import MarketCandidateInput
from candlepilot.providers.base import DecisionProvider, ProviderResult
from candlepilot.providers.cli import ProviderError, ProviderInvocationError
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.risk.engine import RiskEvaluation, SymbolRules
from candlepilot.storage.database import AuditRepository, Database
from conftest import FakeTestnetBroker


class FakeProvider(DecisionProvider):
    name = "fake-auth"

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio) -> ProviderResult:
        intent = TradeIntent(
            symbol=snapshot.symbol,
            cadence=snapshot.cadence,
            action=TradeAction.OPEN_LONG,
            confidence=0.8,
            leverage=3,
            risk_fraction="0.01",
            stop_loss=snapshot.mark_price * Decimal("0.98"),
            take_profit=snapshot.mark_price * Decimal("1.04"),
            rationale="fixture",
        )
        return ProviderResult(intent, self.name, "fixture", timedelta(milliseconds=1), "{}", {})


class FailedProvider(DecisionProvider):
    name = "failed-primary"

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio) -> ProviderResult:
        raise ProviderError("fixture failure")


class UnexpectedFailedProvider(FailedProvider):
    name = "unexpected-failure"

    async def generate_trade_intent(self, snapshot, portfolio) -> ProviderResult:
        raise ValueError("unexpected fixture failure")


class AuditedFailedProvider(DecisionProvider):
    name = "audited-failure"
    model = "fixture-model"

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio) -> ProviderResult:
        raise ProviderInvocationError(
            "fixture validation failure",
            model=self.model,
            duration=timedelta(milliseconds=432),
            raw_output='{"action":"INVALID"}',
            usage={"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
            prompt_version="trade-intent-v2",
            data_version="market-snapshot-v1:sha256:fixture",
            provider_version="fixture-provider-1",
            input_payload={"market": {"symbol": snapshot.symbol}, "portfolio": {}},
            prompt="fixture prompt",
        )


class MarketableLimitProvider(FakeProvider):
    async def generate_trade_intent(self, snapshot, portfolio) -> ProviderResult:
        intent = TradeIntent(
            symbol=snapshot.symbol,
            cadence=snapshot.cadence,
            action=TradeAction.OPEN_LONG,
            confidence=0.8,
            leverage=3,
            risk_fraction="0.01",
            order_type=OrderType.LIMIT,
            entry_price="101",
            stop_loss="98",
            take_profit="106",
            rationale="marketable limit fixture",
        )
        return ProviderResult(intent, self.name, "fixture", timedelta(0), "{}", {})


class RestingLimitProvider(FakeProvider):
    async def generate_trade_intent(self, snapshot, portfolio) -> ProviderResult:
        intent = TradeIntent(
            symbol=snapshot.symbol,
            cadence=snapshot.cadence,
            action=TradeAction.OPEN_LONG,
            confidence=0.8,
            leverage=3,
            risk_fraction="0.01",
            order_type=OrderType.LIMIT,
            entry_price="99",
            stop_loss="98",
            take_profit="102",
            ttl_seconds=60,
            rationale="resting limit fixture",
        )
        return ProviderResult(intent, self.name, "fixture", timedelta(0), "{}", {})


class RefreshSafeProvider(FakeProvider):
    async def generate_trade_intent(self, snapshot, portfolio) -> ProviderResult:
        intent = TradeIntent(
            symbol=snapshot.symbol,
            cadence=snapshot.cadence,
            action=TradeAction.OPEN_LONG,
            confidence=0.8,
            leverage=3,
            risk_fraction="0.01",
            stop_loss=snapshot.mark_price * Decimal("0.985"),
            take_profit=snapshot.mark_price * Decimal("1.06"),
            rationale="refresh-safe fixture",
        )
        return ProviderResult(intent, self.name, "fixture", timedelta(0), "{}", {})


class FakeMarket:
    def __init__(self, mark_price: Decimal = Decimal("100")) -> None:
        self.mark_price = mark_price
        self.snapshot_calls = 0

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

    async def market_snapshot(self, symbol, cadence):
        self.snapshot_calls += 1
        return MarketSnapshot(
            symbol=symbol,
            cadence=cadence,
            timestamp=datetime.now(UTC),
            mark_price=self.mark_price,
            bid=self.mark_price - Decimal("0.1"),
            ask=self.mark_price + Decimal("0.1"),
            quote_volume_24h="1000000",
        )


def test_engine_requires_provider_and_audits_paper_fill(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([FakeProvider()]),
            audit=audit,
            market=FakeMarket(),  # type: ignore[arg-type]
        )
        try:
            await engine.start()
            raise AssertionError("start should require a provider")
        except RuntimeError:
            pass
        engine.select_provider_chain(["fake-auth"])
        await engine.start()
        candidates = await engine.refresh_universe()
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            cadence="5m",
            timestamp=datetime.now(UTC),
            mark_price="100",
            bid="99.9",
            ask="100.1",
            quote_volume_24h="1000000",
        )
        outcome = await engine.evaluate(
            snapshot,
            PortfolioState(equity="10000", available_balance="8000"),
            SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")),
        )
        intents = await audit.recent_intents()
        await engine.stop()
        events = await audit.recent_decision_events()
        await database.close()
        return candidates, outcome, intents, events

    candidates, outcome, intents, events = asyncio.run(scenario())
    assert candidates[0].symbol == "BTCUSDT"
    assert outcome.risk.accepted
    assert outcome.execution is not None and outcome.execution.status == "FILLED"
    assert intents[0]["intent"]["action"] == "OPEN_LONG"
    assert events[0]["live_run_id"] is not None
    assert events[0]["live_run"]["status"] == "stopped"
    assert events[0]["live_run"]["stop_reason"] == "stopped by user"


def test_universe_excludes_symbols_not_tradable_on_the_execution_venue(
    tmp_path: Path,
) -> None:
    class ProductionMarket(FakeMarket):
        async def candidate_inputs(self):
            btc = (await super().candidate_inputs())[0]
            return [
                btc,
                MarketCandidateInput(
                    symbol="ALLOUSDT",
                    quote_volume_24h=Decimal("2000000"),
                    bid=Decimal("0.46"),
                    ask=Decimal("0.461"),
                    volatility=Decimal("0.2"),
                    trend_strength=Decimal("0.05"),
                    listing_age_days=100,
                ),
            ]

    class TestnetVenue(FakeTestnetBroker):
        async def tradable_symbols(self):
            return frozenset({"BTCUSDT"})

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'venue-universe.db'}")
        engine = TradingEngine(
            testnet_broker=TestnetVenue(),  # type: ignore[arg-type]
            providers=ProviderRegistry([FakeProvider()]),
            audit=AuditRepository(database.sessions),
            market=ProductionMarket(),  # type: ignore[arg-type]
        )
        candidates = await engine.refresh_universe()
        await database.close()
        return candidates, engine.venue_excluded_symbols

    candidates, excluded = asyncio.run(scenario())
    assert [candidate.symbol for candidate in candidates] == ["BTCUSDT"]
    assert excluded == ("ALLOUSDT",)


def test_engine_refreshes_market_and_rejects_crossed_price_before_execution(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'fresh-market.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        market = FakeMarket(mark_price=Decimal("105"))
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([FakeProvider()]),
            audit=audit,
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["fake-auth"])
        await engine.start()
        outcome = await engine.evaluate(
            MarketSnapshot(
                symbol="BTCUSDT",
                cadence="5m",
                timestamp=datetime.now(UTC) - timedelta(seconds=20),
                mark_price="100",
                bid="99.9",
                ask="100.1",
                quote_volume_24h="1000000",
            ),
            PortfolioState(equity="10000", available_balance="8000"),
            SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")),
        )
        risk_events = await audit.recent_risk_decisions()
        executions = await audit.recent_executions()
        await database.close()
        return outcome, market.snapshot_calls, risk_events, executions

    outcome, snapshot_calls, risk_events, executions = asyncio.run(scenario())
    assert snapshot_calls == 1
    assert not outcome.risk.accepted and outcome.execution is None
    assert "take profit" in outcome.risk.reason
    assert risk_events[0]["reason"] == outcome.risk.reason
    assert executions == []


def test_engine_executes_against_refreshed_market_after_slow_analysis(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'fresh-execution.db'}")
        await database.initialize()
        market = FakeMarket(mark_price=Decimal("101"))
        broker = FakeTestnetBroker()
        engine = TradingEngine(
            testnet_broker=broker,  # type: ignore[arg-type]
            providers=ProviderRegistry([RefreshSafeProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["fake-auth"])
        await engine.start()
        outcome = await engine.evaluate(
            MarketSnapshot(
                symbol="BTCUSDT",
                cadence="5m",
                timestamp=datetime.now(UTC) - timedelta(seconds=20),
                mark_price="100",
                bid="99.9",
                ask="100.1",
                quote_volume_24h="1000000",
            ),
            PortfolioState(equity="10000", available_balance="8000"),
            SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")),
        )
        await database.close()
        return outcome, market.snapshot_calls, broker.orders

    outcome, snapshot_calls, orders = asyncio.run(scenario())
    assert outcome.risk.accepted and outcome.execution is not None
    assert snapshot_calls == 1
    # The analysis snapshot said 100, the refreshed market says 101. The stop
    # stays where the model put it -- it reasoned off the analysis snapshot --
    # but the size must come from the refreshed price. The 10% per-symbol initial
    # margin cap is tighter here: 1000 USDT at 3x / 101 = 29.702 units.
    # The fill itself comes back from the exchange, so it is not ours to assert.
    assert orders[0].stop_price == Decimal("98.50")
    assert orders[0].quantity == Decimal("29.702")


def test_engine_rejects_when_refreshed_market_breaks_raw_reward_risk(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'refresh-ratio.db'}")
        await database.initialize()
        market = FakeMarket(mark_price=Decimal("101"))
        broker = FakeTestnetBroker()
        engine = TradingEngine(
            testnet_broker=broker,  # type: ignore[arg-type]
            providers=ProviderRegistry([FakeProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["fake-auth"])
        await engine.start()
        outcome = await engine.evaluate(
            MarketSnapshot(
                symbol="BTCUSDT",
                cadence="5m",
                timestamp=datetime.now(UTC) - timedelta(seconds=20),
                mark_price="100",
                bid="99.9",
                ask="100.1",
                quote_volume_24h="1000000",
            ),
            PortfolioState(equity="10000", available_balance="8000"),
            SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")),
        )
        await database.close()
        return outcome, market.snapshot_calls, broker.orders

    outcome, snapshot_calls, orders = asyncio.run(scenario())
    assert not outcome.risk.accepted and outcome.execution is None
    assert "pre-trade reward/risk ratio" in outcome.risk.reason
    assert snapshot_calls == 1
    assert orders == []


def test_engine_executes_and_audits_marketable_limit_after_refresh(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'marketable-limit.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([MarketableLimitProvider()]),
            audit=audit,
            market=FakeMarket(),  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["fake-auth"])
        await engine.start()
        outcome = await engine.evaluate(
            MarketSnapshot(
                symbol="BTCUSDT",
                cadence="5m",
                timestamp=datetime.now(UTC) - timedelta(seconds=20),
                mark_price="100",
                bid="99.9",
                ask="100.1",
                quote_volume_24h="1000000",
            ),
            PortfolioState(equity="10000", available_balance="8000"),
            SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")),
        )
        risk_events = await audit.recent_risk_decisions()
        executions = await audit.recent_executions()
        await database.close()
        return outcome, risk_events, executions

    outcome, risk_events, executions = asyncio.run(scenario())
    assert outcome.risk.accepted
    assert outcome.execution is not None and outcome.execution.status == "FILLED"
    assert "immediately marketable after refresh" in outcome.risk.reason
    assert risk_events[0]["reason"] == outcome.risk.reason
    assert len(executions) == 1


def test_engine_queues_resting_limit_then_rechecks_and_executes(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'pending-limit.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        broker = FakeTestnetBroker()
        market = FakeMarket()
        engine = TradingEngine(
            testnet_broker=broker,  # type: ignore[arg-type]
            providers=ProviderRegistry([RestingLimitProvider()]),
            audit=audit,
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["fake-auth"])
        await engine.start()
        rules = SymbolRules(
            Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")
        )
        engine.venue_contract_rules = {"BTCUSDT": rules}
        outcome = (
            await engine.evaluate_batch(
                [await market.market_snapshot("BTCUSDT", "5m")],
                PortfolioState(equity="10000", available_balance="8000"),
                {"BTCUSDT": rules},
            )
        )[0]
        queued = await audit.pending_local_entries(live_run_id=engine.live_run_id)
        orders_before_trigger = list(broker.orders)

        market.mark_price = Decimal("98.8")
        trigger_result = await engine.process_pending_entry("BTCUSDT")
        remaining = await audit.pending_local_entries(live_run_id=engine.live_run_id)
        await database.close()
        return outcome, queued, orders_before_trigger, trigger_result, remaining, broker.orders

    outcome, queued, orders_before, trigger_result, remaining, orders = asyncio.run(
        scenario()
    )
    assert outcome.risk.accepted and outcome.risk.pending_entry
    assert outcome.risk.pending_expires_at is not None
    assert len(queued) == 1
    assert orders_before == []
    assert trigger_result == "triggered"
    assert remaining == []
    assert len(orders) == 1


def test_engine_expires_pending_limit_without_submitting(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'expired-pending.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        broker = FakeTestnetBroker()
        market = FakeMarket()
        engine = TradingEngine(
            testnet_broker=broker,  # type: ignore[arg-type]
            providers=ProviderRegistry([RestingLimitProvider()]),
            audit=audit,
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["fake-auth"])
        await engine.start()
        rules = SymbolRules(
            Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")
        )
        engine.venue_contract_rules = {"BTCUSDT": rules}
        await engine.evaluate_batch(
            [await market.market_snapshot("BTCUSDT", "5m")],
            PortfolioState(equity="10000", available_balance="8000"),
            {"BTCUSDT": rules},
        )
        queued = (await audit.pending_local_entries(live_run_id=engine.live_run_id))[0]
        expired = queued["decision"].model_copy(
            update={"pending_expires_at": datetime.now(UTC) - timedelta(seconds=1)}
        )
        await audit.update_risk(queued["inference_id"], expired, completed=False)
        result = await engine.process_pending_entry("BTCUSDT")
        events = await audit.recent_decision_events()
        await database.close()
        return result, broker.orders, events

    result, orders, events = asyncio.run(scenario())
    assert result == "expired"
    assert orders == []
    assert events[0]["risk"]["accepted"] is False
    assert "expired after 60s" in events[0]["risk"]["reason"]


def test_engine_rejects_expired_analysis_before_market_refresh(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'expired-analysis.db'}")
        await database.initialize()
        market = FakeMarket()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([FakeProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["fake-auth"])
        await engine.start()
        outcome = await engine.evaluate(
            MarketSnapshot(
                symbol="BTCUSDT",
                cadence="5m",
                timestamp=datetime.now(UTC) - timedelta(seconds=76),
                mark_price="100",
                bid="99.9",
                ask="100.1",
                quote_volume_24h="1000000",
            ),
            PortfolioState(equity="10000", available_balance="8000"),
            SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")),
        )
        await database.close()
        return outcome, market.snapshot_calls

    outcome, snapshot_calls = asyncio.run(scenario())
    assert not outcome.risk.accepted
    assert "analysis snapshot expired" in outcome.risk.reason
    assert snapshot_calls == 0


def test_engine_audits_market_refresh_failure_without_execution(tmp_path: Path) -> None:
    class FailingMarket(FakeMarket):
        async def market_snapshot(self, symbol, cadence):
            self.snapshot_calls += 1
            raise TimeoutError("fixture")

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'refresh-failure.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        market = FailingMarket()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([FakeProvider()]),
            audit=audit,
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["fake-auth"])
        await engine.start()
        outcome = await engine.evaluate(
            MarketSnapshot(
                symbol="BTCUSDT",
                cadence="5m",
                timestamp=datetime.now(UTC),
                mark_price="100",
                bid="99.9",
                ask="100.1",
                quote_volume_24h="1000000",
            ),
            PortfolioState(equity="10000", available_balance="8000"),
            SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")),
        )
        risk_events = await audit.recent_risk_decisions()
        executions = await audit.recent_executions()
        await database.close()
        return outcome, risk_events, executions

    outcome, risk_events, executions = asyncio.run(scenario())
    assert outcome.risk.reason == "pre-trade refresh failed: TimeoutError"
    assert risk_events[0]["reason"] == outcome.risk.reason
    assert executions == []


def test_engine_cadence_selection_validates_and_locks_when_running(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cadence-engine.db'}")
        await database.initialize()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([FakeProvider()]),
            audit=AuditRepository(database.sessions),
            market=FakeMarket(),  # type: ignore[arg-type]
        )
        default = engine.active_cadences
        engine.select_cadences(["30m"])
        selected = engine.active_cadences

        errors = {}
        for label, cadences in (
            ("invalid", ["1m"]),
            ("empty", []),
            ("multiple", ["15m", "30m"]),
        ):
            try:
                engine.select_cadences(cadences)
            except ValueError:
                errors[label] = True

        engine.select_provider_chain(["fake-auth"])
        await engine.start()
        try:
            engine.select_cadences(["5m"])
            errors["locked"] = False
        except RuntimeError:
            errors["locked"] = True
        await database.close()
        return default, selected, errors

    default, selected, errors = asyncio.run(scenario())
    assert default == ("15m",)
    assert selected == ("30m",)
    assert errors == {"invalid": True, "empty": True, "multiple": True, "locked": True}


def test_evaluate_stop_reason_covers_duration_budget_and_route_failures() -> None:
    from candlepilot.providers.retry import DECISION_PROVIDER_MAX_ATTEMPTS

    engine = TradingEngine.__new__(TradingEngine)  # pure check; no I/O needed
    now = datetime.now(UTC)
    engine.running = True
    engine.run_started_at = now - timedelta(seconds=100)
    engine.max_run_seconds = None
    engine.max_run_cost_usd = None
    engine.route_failure_count = 0
    engine.rescue_count = 0

    assert engine.evaluate_stop_reason(now=now, run_cost_usd=5.0) is None

    engine.max_run_seconds = 120
    assert engine.evaluate_stop_reason(now=now) is None  # 100s < 120s
    engine.max_run_seconds = 100
    assert "duration limit" in engine.evaluate_stop_reason(now=now)

    engine.max_run_seconds = None
    engine.max_run_cost_usd = 2.0
    assert engine.evaluate_stop_reason(now=now, run_cost_usd=1.5) is None
    assert "cost budget" in engine.evaluate_stop_reason(now=now, run_cost_usd=2.0)
    # An unknown cost must never trigger a stop.
    assert engine.evaluate_stop_reason(now=now, run_cost_usd=None) is None

    engine.max_run_cost_usd = None
    engine.route_failure_count = DECISION_PROVIDER_MAX_ATTEMPTS - 1
    assert engine.evaluate_stop_reason(now=now) is None
    engine.route_failure_count = DECISION_PROVIDER_MAX_ATTEMPTS
    assert "every provider" in engine.evaluate_stop_reason(now=now)

    engine.route_failure_count = 0
    engine.rescue_count = 2
    assert engine.evaluate_stop_reason(now=now) is None
    engine.rescue_count = 3
    assert "累计紧急回补 3 次" in engine.evaluate_stop_reason(now=now)

    # A stopped engine never reports a reason.
    engine.running = False
    assert engine.evaluate_stop_reason(now=now) is None


def test_run_limits_validate_and_lock_while_running(tmp_path: Path) -> None:
    import pytest

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'limits.db'}")
        await database.initialize()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([FakeProvider()]),
            audit=AuditRepository(database.sessions),
            market=FakeMarket(),  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["fake-auth"])
        assert engine.max_run_seconds is None and engine.max_run_cost_usd is None

        with pytest.raises(ValueError):
            engine.select_run_limits(max_run_seconds=0, max_run_cost_usd=None)
        with pytest.raises(ValueError):
            engine.select_run_limits(max_run_seconds=None, max_run_cost_usd=0)

        engine.select_run_limits(max_run_seconds=600, max_run_cost_usd=1.5)
        assert engine.max_run_seconds == 600
        assert engine.max_run_cost_usd == 1.5

        await engine.start()
        with pytest.raises(RuntimeError):
            engine.select_run_limits(max_run_seconds=60, max_run_cost_usd=None)
        assert engine.max_run_seconds == 600
        await database.close()

    asyncio.run(scenario())


def test_route_failures_retry_three_times_and_success_clears_the_count(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'exhaust.db'}")
        await database.initialize()
        market = FakeMarket()
        retry_delays: list[float] = []

        async def capture_retry(delay: float) -> None:
            retry_delays.append(delay)

        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry(
                [FailedProvider(), UnexpectedFailedProvider(), FakeProvider()]
            ),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
            retry_sleep=capture_retry,
        )
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            cadence="5m",
            timestamp=datetime.now(UTC),
            mark_price="100",
            bid="99.9",
            ask="100.1",
            quote_volume_24h="1000000",
        )
        rules = SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01"))
        portfolio = PortfolioState(equity="10000", available_balance="8000")

        # Only the failing provider is routed: the route becomes exhausted.
        engine.select_provider_chain(["failed-primary"])
        await engine.start()
        await engine.evaluate(snapshot, portfolio, rules)
        exhausted = engine.route_failure_count
        await engine.stop()

        # A provider implementation bug is still a failed provider attempt. It
        # must not escape to the scheduler and repeat forever outside the route
        # threshold merely because it used the wrong exception class.
        engine.select_provider_chain(["unexpected-failure"])
        await engine.start()
        await engine.evaluate(snapshot, portfolio, rules)
        unexpected_exhausted = engine.route_failure_count
        await engine.stop()

        # A working provider clears the exhaustion marker on the next success.
        engine.select_provider_chain(["fake-auth"])
        await engine.start()
        engine.route_failure_count = 2
        await engine.evaluate(snapshot, portfolio, rules)
        cleared = engine.route_failure_count
        await database.close()
        return exhausted, unexpected_exhausted, cleared, retry_delays

    exhausted, unexpected_exhausted, cleared, retry_delays = asyncio.run(scenario())
    assert exhausted == 3
    assert unexpected_exhausted == 3
    assert cleared == 0
    assert retry_delays == [5.0, 15.0, 5.0, 15.0]


def test_next_retry_round_refreshes_market_and_portfolio_inputs(tmp_path: Path) -> None:
    class RefreshPortfolioBroker(FakeTestnetBroker):
        async def account(self):
            return {
                "totalMarginBalance": "9000",
                "availableBalance": "7000",
                "totalInitialMargin": "0",
                "positions": [],
            }

    class CapturingFlakyProvider(FakeProvider):
        name = "capturing-flaky"

        def __init__(self) -> None:
            self.inputs: list[tuple[Decimal, Decimal]] = []

        async def generate_trade_intent(self, snapshot, portfolio) -> ProviderResult:
            self.inputs.append((snapshot.mark_price, portfolio.equity))
            if len(self.inputs) == 1:
                raise ProviderError("retry with fresh inputs")
            result = await super().generate_trade_intent(snapshot, portfolio)
            return ProviderResult(
                result.intent,
                self.name,
                result.model,
                result.duration,
                result.raw_output,
                result.usage,
            )

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'fresh-retry.db'}")
        await database.initialize()
        market = FakeMarket(mark_price=Decimal("105"))
        provider = CapturingFlakyProvider()
        engine = TradingEngine(
            testnet_broker=RefreshPortfolioBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([provider]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
            provider_retry_delays=(0, 0),
        )
        engine.select_provider_chain([provider.name])
        await engine.start()
        outcome = await engine.evaluate(
            MarketSnapshot(
                symbol="BTCUSDT",
                cadence="5m",
                timestamp=datetime.now(UTC),
                mark_price="100",
                bid="99.9",
                ask="100.1",
                quote_volume_24h="1000000",
            ),
            PortfolioState(equity="10000", available_balance="8000"),
            SymbolRules(
                Decimal("0.001"),
                Decimal("0.001"),
                Decimal("5"),
                Decimal("0.01"),
            ),
        )
        await database.close()
        return outcome, provider.inputs, market.snapshot_calls

    outcome, inputs, snapshot_calls = asyncio.run(scenario())
    assert inputs == [
        (Decimal("100"), Decimal("10000")),
        (Decimal("105"), Decimal("9000")),
    ]
    assert snapshot_calls == 2  # retry input refresh, then pre-trade refresh
    assert outcome.execution is not None


def test_retry_refresh_failure_never_reuses_old_inputs_or_places_an_order(
    tmp_path: Path,
) -> None:
    class FailingRefreshMarket(FakeMarket):
        async def market_snapshot(self, symbol, cadence):
            self.snapshot_calls += 1
            raise TimeoutError("fresh snapshot unavailable")

    class CountingFailedProvider(FailedProvider):
        def __init__(self) -> None:
            self.calls = 0

        async def generate_trade_intent(self, snapshot, portfolio) -> ProviderResult:
            self.calls += 1
            return await super().generate_trade_intent(snapshot, portfolio)

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'retry-refresh-fail.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        market = FailingRefreshMarket()
        provider = CountingFailedProvider()
        broker = FakeTestnetBroker()
        engine = TradingEngine(
            testnet_broker=broker,  # type: ignore[arg-type]
            providers=ProviderRegistry([provider]),
            audit=audit,
            market=market,  # type: ignore[arg-type]
            provider_retry_delays=(0, 0),
        )
        engine.select_provider_chain([provider.name])
        await engine.start()
        try:
            await engine.evaluate(
                MarketSnapshot(
                    symbol="BTCUSDT",
                    cadence="5m",
                    timestamp=datetime.now(UTC),
                    mark_price="100",
                    bid="99.9",
                    ask="100.1",
                    quote_volume_24h="1000000",
                ),
                PortfolioState(equity="10000", available_balance="8000"),
                SymbolRules(
                    Decimal("0.001"),
                    Decimal("0.001"),
                    Decimal("5"),
                    Decimal("0.01"),
                ),
            )
        except RuntimeError as exc:
            error = str(exc)
        else:
            raise AssertionError("retry refresh failure must abort the decision")
        events = await audit.recent_intents()
        await database.close()
        return error, provider.calls, market.snapshot_calls, broker.orders, events

    error, provider_calls, snapshot_calls, orders, events = asyncio.run(scenario())
    assert error == "decision retry input refresh failed: TimeoutError"
    assert provider_calls == 1
    assert snapshot_calls == 1
    assert orders == []
    assert len(events) == 1


def test_testnet_add_requests_protective_bracket_replacement(tmp_path: Path) -> None:
    from candlepilot.broker.binance_testnet import ProtectiveLevels, ReconciliationReport
    from candlepilot.domain.models import ExecutionReport

    class AddProvider(FakeProvider):
        async def generate_trade_intent(self, snapshot, portfolio) -> ProviderResult:
            intent = TradeIntent(
                symbol=snapshot.symbol,
                cadence=snapshot.cadence,
                action=TradeAction.ADD,
                confidence=0.8,
                leverage=3,
                risk_fraction="0.01",
                stop_loss="98",
                take_profit="104",
                rationale="add fixture",
            )
            return ProviderResult(intent, self.name, None, timedelta(0), "{}", {})

    class CapturingBroker:
        replace_existing_protection: bool | None = None

        async def reconcile_account(self):
            return ReconciliationReport(("BTCUSDT",), 2, ())

        async def account(self):
            return {
                "totalMarginBalance": "10000",
                "availableBalance": "8000",
                "totalInitialMargin": "100",
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "1",
                        "entryPrice": "99",
                        "unrealizedProfit": "1",
                        "leverage": "5",
                    },
                ],
            }

        async def protective_levels(self):
            return {"BTCUSDT": ProtectiveLevels(stop_loss=Decimal("95"))}

        async def execute_with_stop(
            self, order, *, leverage, replace_existing_protection=False
        ):
            self.replace_existing_protection = replace_existing_protection
            return ExecutionReport(
                client_order_id=order.client_order_id,
                status="FILLED",
                filled_quantity=order.quantity,
                average_price=Decimal("100"),
            )

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'testnet-add.db'}")
        await database.initialize()
        broker = CapturingBroker()
        engine = TradingEngine(
            providers=ProviderRegistry([AddProvider()]),
            audit=AuditRepository(database.sessions),
            market=FakeMarket(),  # type: ignore[arg-type]
            testnet_broker=broker,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["fake-auth"])
        await engine.start()
        outcome = await engine.evaluate(
            MarketSnapshot(
                symbol="BTCUSDT",
                cadence="5m",
                timestamp=datetime.now(UTC),
                mark_price="100",
                bid="99.9",
                ask="100.1",
                quote_volume_24h="1000000",
            ),
            PortfolioState(
                equity="10000",
                available_balance="8000",
                open_positions=1,
                positions={
                    "BTCUSDT": PositionState(side="LONG", quantity="1", entry_price="99")
                },
            ),
            SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")),
        )
        await database.close()
        return outcome, broker.replace_existing_protection

    outcome, replacement = asyncio.run(scenario())
    assert outcome.execution is not None
    assert replacement is True


def test_start_refuses_pending_entry_orders(tmp_path: Path) -> None:
    import pytest

    from candlepilot.application.engine import AccountReconciliationError
    from candlepilot.broker.binance_testnet import ReconciliationReport

    class PendingEntryBroker:
        async def reconcile_account(self):
            return ReconciliationReport((), 1, (), ("ETHUSDT",))

    async def scenario() -> None:
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'pending-entry.db'}")
        await database.initialize()
        engine = TradingEngine(
            providers=ProviderRegistry([FakeProvider()]),
            audit=AuditRepository(database.sessions),
            market=FakeMarket(),  # type: ignore[arg-type]
            testnet_broker=PendingEntryBroker(),  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["fake-auth"])
        with pytest.raises(AccountReconciliationError, match="pending entry orders: ETHUSDT"):
            await engine.start()
        await database.close()

    asyncio.run(scenario())


def test_testnet_execution_failure_is_audited_with_rescue_loss(tmp_path: Path) -> None:
    from candlepilot.broker.binance_testnet import ProtectiveStopError, ReconciliationReport
    from candlepilot.domain.models import ExecutionReport

    class FailingProtectionBroker:
        async def reconcile_account(self):
            return ReconciliationReport((), 0, ())

        async def account(self):
            return {
                "totalMarginBalance": "10000",
                "availableBalance": "8000",
                "totalInitialMargin": "0",
                "positions": [],
            }

        async def protective_levels(self):
            return {}

        async def execute_with_stop(self, order, **_):
            entry = ExecutionReport(
                client_order_id=order.client_order_id,
                status="FILLED",
                filled_quantity="1",
                average_price="100",
            )
            rescue = ExecutionReport(
                client_order_id=f"{order.client_order_id}-rescue",
                status="FILLED",
                filled_quantity="1",
                average_price="98",
            )
            raise ProtectiveStopError(
                "entry succeeded but protective bracket failed; rescued",
                entry=entry,
                rescue=rescue,
                exchange_error_code=-4120,
                estimated_loss_usdt=Decimal("2"),
                failed_stage="PROTECTION",
                requires_emergency_lock=False,
            )

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'execution-failure.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        engine = TradingEngine(
            providers=ProviderRegistry([FakeProvider()]),
            audit=audit,
            market=FakeMarket(),  # type: ignore[arg-type]
            testnet_broker=FailingProtectionBroker(),  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["fake-auth"])
        await engine.start()
        outcome = await engine.evaluate(
            MarketSnapshot(
                symbol="BTCUSDT",
                cadence="5m",
                timestamp=datetime.now(UTC),
                mark_price="100",
                bid="99.9",
                ask="100.1",
                quote_volume_24h="1000000",
            ),
            PortfolioState(equity="10000", available_balance="8000"),
            SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")),
        )
        events = await audit.recent_decision_events()
        executions = await audit.recent_executions()
        await database.close()
        return outcome, events, executions

    outcome, events, executions = asyncio.run(scenario())
    assert outcome.risk.accepted is True
    assert outcome.execution is None
    assert events[0]["outcome"] == "execution_failed"
    assert events[0]["execution"]["status"] == "RESCUED"
    assert events[0]["execution"]["exchange_error_code"] == -4120
    assert events[0]["execution"]["estimated_loss_usdt"] == "2"
    assert {item["client_order_id"] for item in executions} == {
        events[0]["execution"]["client_order_id"],
        f"{events[0]['execution']['client_order_id']}-rescue",
    }


def test_terminal_exchange_rejection_is_not_audited_as_success(tmp_path: Path) -> None:
    class RejectingBroker(FakeTestnetBroker):
        async def execute_with_stop(self, order, **_):
            return ExecutionReport(
                client_order_id=order.client_order_id,
                status="REJECTED",
                message="fixture rejection",
            )

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'terminal-rejection.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        engine = TradingEngine(
            testnet_broker=RejectingBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([FakeProvider()]),
            audit=audit,
            market=FakeMarket(),  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["fake-auth"])
        await engine.start()
        outcome = await engine.evaluate(
            MarketSnapshot(
                symbol="BTCUSDT",
                cadence="5m",
                timestamp=datetime.now(UTC),
                mark_price="100",
                bid="99.9",
                ask="100.1",
                quote_volume_24h="1000000",
            ),
            PortfolioState(equity="10000", available_balance="8000"),
            SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")),
        )
        events = await audit.recent_decision_events()
        await database.close()
        return outcome, events

    outcome, events = asyncio.run(scenario())
    assert outcome.execution is not None and outcome.execution.status == "REJECTED"
    assert events[0]["outcome"] == "execution_failed"
    assert events[0]["execution"]["status"] == "FAILED"
    assert events[0]["execution"]["stage"] == "ENTRY"


def test_unrescued_protection_failure_emergency_locks_engine(tmp_path: Path) -> None:
    from candlepilot.broker.binance_testnet import ProtectiveStopError, ReconciliationReport
    from candlepilot.domain.models import ExecutionReport

    class UnrescuedProtectionBroker:
        flattened = False

        async def reconcile_account(self):
            return ReconciliationReport((), 0, ())

        async def account(self):
            return {
                "totalMarginBalance": "10000",
                "availableBalance": "8000",
                "totalInitialMargin": "0",
                "positions": [],
            }

        async def protective_levels(self):
            return {}

        async def execute_with_stop(self, order, **_):
            raise ProtectiveStopError(
                "entry succeeded but emergency rescue was incomplete",
                entry=ExecutionReport(
                    client_order_id=order.client_order_id,
                    status="FILLED",
                    filled_quantity="1",
                    average_price="100",
                ),
                rescue=ExecutionReport(
                    client_order_id=f"{order.client_order_id}-rescue",
                    status="PARTIALLY_FILLED",
                    filled_quantity="0.4",
                    average_price="99",
                ),
                exchange_error_code=-4120,
                estimated_loss_usdt=None,
                failed_stage="RESCUE",
                requires_emergency_lock=True,
            )

        async def emergency_flatten(self):
            self.flattened = True

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'unrescued-failure.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        broker = UnrescuedProtectionBroker()
        engine = TradingEngine(
            providers=ProviderRegistry([FakeProvider()]),
            audit=audit,
            market=FakeMarket(),  # type: ignore[arg-type]
            testnet_broker=broker,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["fake-auth"])
        await engine.start()
        await engine.evaluate(
            MarketSnapshot(
                symbol="BTCUSDT",
                cadence="5m",
                timestamp=datetime.now(UTC),
                mark_price="100",
                bid="99.9",
                ask="100.1",
                quote_volume_24h="1000000",
            ),
            PortfolioState(equity="10000", available_balance="8000"),
            SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")),
        )
        events = await audit.recent_decision_events()
        result = (
            engine.running,
            engine.emergency_locked,
            broker.flattened,
            engine.rescue_count,
            events[0]["execution"],
        )
        await database.close()
        return result

    running, locked, flattened, rescue_count, execution = asyncio.run(scenario())
    assert running is False
    assert locked is True
    assert flattened is True
    assert rescue_count == 0
    assert execution["status"] == "FAILED"
    assert execution["stage"] == "RESCUE"
    assert execution["rescue_report"]["status"] == "PARTIALLY_FILLED"


def test_batch_does_not_submit_after_internal_emergency_stop(tmp_path: Path) -> None:
    from candlepilot.broker.binance_testnet import ProtectiveStopError, ReconciliationReport

    class FirstOrderTriggersEmergency:
        def __init__(self) -> None:
            self.orders = []
            self.flattened = False

        async def reconcile_account(self):
            return ReconciliationReport((), 0, ())

        async def account(self):
            return {
                "totalMarginBalance": "10000",
                "availableBalance": "8000",
                "totalInitialMargin": "0",
                "positions": [],
            }

        async def protective_levels(self):
            return {}

        async def execute_with_stop(self, order, **_):
            self.orders.append(order.symbol)
            if len(self.orders) == 1:
                raise ProtectiveStopError(
                    "entry protection and rescue failed",
                    entry=ExecutionReport(
                        client_order_id=order.client_order_id,
                        status="FILLED",
                        filled_quantity=order.quantity,
                        average_price="100",
                    ),
                    rescue=None,
                    exchange_error_code=-4120,
                    estimated_loss_usdt=None,
                    failed_stage="RESCUE",
                    requires_emergency_lock=True,
                )
            raise AssertionError("no later batch order may be submitted after emergency stop")

        async def emergency_flatten(self):
            self.flattened = True

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'batch-emergency.db'}")
        await database.initialize()
        broker = FirstOrderTriggersEmergency()
        market = FakeMarket()
        engine = TradingEngine(
            providers=ProviderRegistry([FakeProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
            testnet_broker=broker,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["fake-auth"])
        await engine.start()
        rules = SymbolRules(
            Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")
        )
        snapshots = [
            await market.market_snapshot(symbol, "5m")
            for symbol in ("BTCUSDT", "ETHUSDT")
        ]
        outcomes = await engine.evaluate_batch(
            snapshots,
            PortfolioState(equity="10000", available_balance="8000"),
            {"BTCUSDT": rules, "ETHUSDT": rules},
        )
        result = broker.orders, broker.flattened, engine.emergency_locked, outcomes
        await database.close()
        return result

    orders, flattened, locked, outcomes = asyncio.run(scenario())
    assert orders == ["BTCUSDT"]
    assert flattened is True and locked is True
    assert len(outcomes) == 2
    assert outcomes[1].execution is None
    assert outcomes[1].risk.accepted is False
    assert "emergency lock" in outcomes[1].risk.reason


def test_engine_rejects_multiple_providers(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'single-provider.db'}")
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([FailedProvider(), FakeProvider()]),
        audit=AuditRepository(database.sessions),
        market=FakeMarket(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="exactly one provider"):
        engine.select_provider_chain(["failed-primary", "fake-auth"])


def test_engine_persists_failed_provider_audit_context(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'failure-audit.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([AuditedFailedProvider()]),
            audit=audit,
            market=FakeMarket(),  # type: ignore[arg-type]
            provider_retry_delays=(0, 0),
        )
        engine.select_provider_chain(["audited-failure"])
        await engine.start()
        outcome = await engine.evaluate(
            MarketSnapshot(
                symbol="BTCUSDT",
                cadence="5m",
                timestamp=datetime.now(UTC),
                mark_price="100",
                bid="99.9",
                ask="100.1",
                quote_volume_24h="1000000",
            ),
            PortfolioState(equity="10000", available_balance="8000"),
            SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")),
        )
        events = await audit.recent_intents()
        detail = await audit.decision_detail(events[0]["id"])
        await database.close()
        return outcome, detail

    outcome, detail = asyncio.run(scenario())
    assert outcome.intent.action == TradeAction.HOLD
    assert outcome.execution is None
    assert detail is not None
    assert detail["model"] == "fixture-model"
    assert detail["duration_ms"] == 432
    assert detail["raw_output"] == '{"action":"INVALID"}'
    assert detail["prompt"] == "fixture prompt"
    assert detail["audit_status"] == "complete"
    assert detail["usage"]["input_tokens"] == 20
    assert detail["usage"]["error_message"] == "fixture validation failure"
    assert detail["provenance"]["provider_version"] == "fixture-provider-1"


def test_emergency_lock_persists_and_expires_at_next_utc_day(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runtime-state.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        first = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([FakeProvider()]),
            audit=audit,
            market=FakeMarket(),  # type: ignore[arg-type]
        )
        stopped_at = datetime(2026, 1, 1, 22, 30, tzinfo=UTC)
        await first.emergency_stop(now=stopped_at)

        restored = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([FakeProvider()]),
            audit=audit,
            market=FakeMarket(),  # type: ignore[arg-type]
        )
        await restored.restore_runtime_state(now=stopped_at + timedelta(hours=1))
        before_midnight = (restored.emergency_locked, restored.emergency_locked_until)
        await restored.restore_runtime_state(now=stopped_at + timedelta(hours=2))
        after_midnight = (restored.emergency_locked, restored.emergency_locked_until)
        stored = await audit.get_runtime_state("emergency_locked_until")
        await database.close()
        return before_midnight, after_midnight, stored

    before_midnight, after_midnight, stored = asyncio.run(scenario())
    assert before_midnight == (True, datetime(2026, 1, 2, tzinfo=UTC))
    assert after_midnight == (False, None)
    assert stored is None


def test_emergency_stop_audits_flatten_executions(tmp_path: Path) -> None:
    from candlepilot.broker.binance_testnet import EmergencyExecution

    class ReportingBroker(FakeTestnetBroker):
        async def emergency_flatten(self):
            self.flattened = True
            return (
                EmergencyExecution(
                    symbol="BTCUSDT",
                    report=ExecutionReport(
                        client_order_id="cp-kill-audit",
                        status="FILLED",
                        filled_quantity=Decimal("1"),
                        average_price=Decimal("100"),
                    ),
                ),
            )

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'emergency-audit.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        engine = TradingEngine(
            testnet_broker=ReportingBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([FakeProvider()]),
            audit=audit,
            market=FakeMarket(),  # type: ignore[arg-type]
        )
        await engine.emergency_stop()
        executions = await audit.recent_executions()
        fills = await audit.recent_trade_fills()
        await database.close()
        return executions, fills

    executions, fills = asyncio.run(scenario())
    assert executions[0]["symbol"] == "BTCUSDT"
    assert executions[0]["client_order_id"] == "cp-kill-audit"
    assert fills[0]["purpose"] == "other_close"
    assert fills[0]["reduce_only"] is True


def test_emergency_stop_audits_successes_before_raising_cleanup_error(
    tmp_path: Path,
) -> None:
    import pytest

    from candlepilot.broker.binance_testnet import (
        EmergencyExecution,
        EmergencyFlattenError,
    )

    execution = EmergencyExecution(
        symbol="BTCUSDT",
        report=ExecutionReport(
            client_order_id="cp-kill-partial",
            status="FILLED",
            filled_quantity=Decimal("1"),
            average_price=Decimal("100"),
        ),
    )

    class PartiallyFailingBroker(FakeTestnetBroker):
        async def emergency_flatten(self):
            raise EmergencyFlattenError("one symbol failed", executions=(execution,))

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'emergency-partial.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        engine = TradingEngine(
            testnet_broker=PartiallyFailingBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([FakeProvider()]),
            audit=audit,
            market=FakeMarket(),  # type: ignore[arg-type]
        )
        with pytest.raises(EmergencyFlattenError, match="one symbol failed"):
            await engine.emergency_stop()
        rows = await audit.recent_executions()
        await database.close()
        return rows

    rows = asyncio.run(scenario())
    assert rows[0]["client_order_id"] == "cp-kill-partial"


def test_emergency_lock_clear_requires_a_flat_account_without_orders(tmp_path: Path) -> None:
    import pytest

    from candlepilot.broker.binance_testnet import (
        AccountReconciliationError,
        ReconciliationReport,
    )

    class ReconcilingBroker(FakeTestnetBroker):
        report = ReconciliationReport(("BTCUSDT",), 2, ("BTCUSDT",), ("ETHUSDT",))

        async def reconcile_account(self):
            return self.report

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'clear-lock.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        broker = ReconcilingBroker()
        engine = TradingEngine(
            testnet_broker=broker,  # type: ignore[arg-type]
            providers=ProviderRegistry([FakeProvider()]),
            audit=audit,
            market=FakeMarket(),  # type: ignore[arg-type]
        )
        await engine.emergency_stop()
        with pytest.raises(
            AccountReconciliationError,
            match="仍有持仓：BTCUSDT；仍有挂单：2",
        ):
            await engine.clear_emergency_lock()
        blocked = (
            engine.emergency_locked,
            await audit.get_runtime_state("emergency_locked_until"),
        )
        broker.report = ReconciliationReport((), 0, ())
        await engine.clear_emergency_lock()
        cleared = (
            engine.emergency_locked,
            await audit.get_runtime_state("emergency_locked_until"),
        )
        await database.close()
        return blocked, cleared

    blocked, cleared = asyncio.run(scenario())
    assert blocked[0] is True
    assert blocked[1] is not None
    assert cleared == (False, None)


def test_testnet_portfolio_carries_entry_price_and_live_bracket(tmp_path: Path) -> None:
    """The decision model cannot judge an invalidation it cannot see.

    ``current_portfolio`` is the only thing the prompt gets about open
    positions, so entry price, unrealized PnL and the exchange-side bracket
    have to survive the trip from the account payload into ``PositionState``.
    """

    from candlepilot.broker.binance_testnet import ProtectiveLevels, ReconciliationReport

    class PositionBroker:
        async def reconcile_account(self):
            return ReconciliationReport(("BTCUSDT",), 2, ())

        async def account(self):
            raise AssertionError("formal decisions must use the enriched account snapshot")

        async def account_snapshot(self):
            return {
                "totalMarginBalance": "10000",
                "availableBalance": "8000",
                "totalInitialMargin": "100",
                "positions": [
                    {
                        "symbol": "BTCUSDT",
                        "positionAmt": "-1.5",
                        "entryPrice": "101.25",
                        "unrealizedProfit": "-3.75",
                        "leverage": "5",
                    },
                    {"symbol": "ETHUSDT", "positionAmt": "0", "entryPrice": "0"},
                ],
            }

        async def protective_levels(self):
            return {"BTCUSDT": ProtectiveLevels(stop_loss=Decimal("104"))}

        async def income_24h(self):
            return Decimal("-10")

        async def pending_entry_symbols(self):
            return ("ETHUSDT",)

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'testnet-portfolio.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        stopped_at = datetime.now(UTC) - timedelta(minutes=5)
        await audit.record_user_event(
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                stopped_at,
                stopped_at,
                "SOLUSDT",
                {
                    "o": {
                        "c": "cp-prior-entry-sl",
                        "s": "SOLUSDT",
                        "x": "TRADE",
                        "X": "FILLED",
                    }
                },
            )
        )
        engine = TradingEngine(
            providers=ProviderRegistry([FakeProvider()]),
            audit=audit,
            market=FakeMarket(),  # type: ignore[arg-type]
            testnet_broker=PositionBroker(),  # type: ignore[arg-type]
        )
        portfolio = await engine.current_portfolio()
        await database.close()
        return portfolio

    portfolio = asyncio.run(scenario())
    assert portfolio.open_positions == 1
    position = portfolio.positions["BTCUSDT"]
    assert position.side == "SHORT"
    assert position.quantity == Decimal("1.5")
    assert position.entry_price == Decimal("101.25")
    assert position.unrealized_pnl == Decimal("-3.75")
    assert position.leverage == 5
    assert position.initial_margin == Decimal("30.375")
    assert position.stop_loss == Decimal("104")
    # No take-profit leg on the exchange must read as absent, not as invented.
    assert position.take_profit is None
    assert portfolio.pnl_24h == Decimal("-13.75")
    assert portfolio.pending_entry_symbols == ("ETHUSDT",)
    cooldown = portfolio.stop_loss_cooldown_until["SOLUSDT"]
    assert timedelta(minutes=84) < cooldown - datetime.now(UTC) < timedelta(minutes=86)
    # A flat symbol is not a position.
    assert "ETHUSDT" not in portfolio.positions


def test_testnet_portfolio_reports_missing_position_risk_entry_price(tmp_path: Path) -> None:
    import pytest

    from candlepilot.broker.binance_testnet import AccountReconciliationError

    class MissingPriceBroker(FakeTestnetBroker):
        async def account_snapshot(self):
            return {
                "totalMarginBalance": "10000",
                "availableBalance": "8000",
                "totalInitialMargin": "100",
                "positions": [{"symbol": "BTCUSDT", "positionAmt": "1"}],
            }

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'missing-price.db'}")
        await database.initialize()
        engine = TradingEngine(
            providers=ProviderRegistry([FakeProvider()]),
            audit=AuditRepository(database.sessions),
            market=FakeMarket(),  # type: ignore[arg-type]
            testnet_broker=MissingPriceBroker(),  # type: ignore[arg-type]
        )
        with pytest.raises(
            AccountReconciliationError,
            match="position risk response is missing entry price for BTCUSDT",
        ):
            await engine.current_portfolio()
        await database.close()

    asyncio.run(scenario())


def test_take_profit_reentry_windows_are_audit_only(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'tp-reentry.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        exited_at = datetime.now(UTC) - timedelta(minutes=5)
        await audit.record_user_event(
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                exited_at,
                exited_at,
                "BTCUSDT",
                {
                    "o": {
                        "c": "cp-entry-tp",
                        "s": "BTCUSDT",
                        "x": "TRADE",
                        "X": "FILLED",
                    }
                },
            )
        )
        engine = TradingEngine(
            providers=ProviderRegistry([FakeProvider()]),
            audit=audit,
            market=FakeMarket(),  # type: ignore[arg-type]
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        )
        intent = TradeIntent(
            symbol="BTCUSDT",
            cadence="5m",
            action=TradeAction.OPEN_LONG,
            confidence=0.8,
            leverage=2,
            risk_fraction="0.01",
            stop_loss="98",
            take_profit="104",
            rationale="reentry sample",
        )
        evaluation = await engine._with_take_profit_reentry_shadow(
            intent, RiskEvaluation(decision=RiskDecision(accepted=True, reason="ok"))
        )
        await database.close()
        return evaluation

    evaluation = asyncio.run(scenario())
    assessment = evaluation.decision.take_profit_reentry_assessment
    assert evaluation.decision.accepted is True
    assert assessment is not None
    assert assessment.mode == "shadow"
    assert assessment.would_block_minutes == (15, 30, 60)
