import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from candlepilot.application.engine import TradingEngine
from candlepilot.domain.models import (
    MarketSnapshot,
    OrderType,
    PortfolioState,
    PositionState,
    ProviderHealth,
    TradeAction,
    TradeIntent,
)
from candlepilot.market.scanner import MarketCandidateInput
from candlepilot.providers.base import LLMProvider, ProviderResult
from candlepilot.providers.cli import ProviderError, ProviderInvocationError
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.risk.engine import SymbolRules
from candlepilot.storage.database import AuditRepository, Database
from conftest import FakeTestnetBroker


class FakeProvider(LLMProvider):
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


class FailedProvider(LLMProvider):
    name = "failed-primary"

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio) -> ProviderResult:
        raise ProviderError("fixture failure")


class FlakyProvider(FakeProvider):
    name = "flaky-auth"

    def __init__(self) -> None:
        self.calls = 0

    async def generate_trade_intent(self, snapshot, portfolio) -> ProviderResult:
        self.calls += 1
        if self.calls == 1:
            raise ProviderError("temporary fixture failure")
        result = await super().generate_trade_intent(snapshot, portfolio)
        return ProviderResult(
            result.intent,
            self.name,
            result.model,
            result.duration,
            result.raw_output,
            result.usage,
        )


class UnavailableProvider(FailedProvider):
    name = "unavailable-auth"

    def __init__(self) -> None:
        self.calls = 0

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(
            provider=self.name,
            available=False,
            authenticated=False,
            detail="fixture unavailable",
        )

    async def generate_trade_intent(self, snapshot, portfolio) -> ProviderResult:
        self.calls += 1
        raise AssertionError("startup health failure should put this provider in cooldown")


class AuditedFailedProvider(LLMProvider):
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
            take_profit="104",
            rationale="marketable limit fixture",
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
        engine.select_provider("fake-auth")
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
        await database.close()
        return candidates, outcome, intents

    candidates, outcome, intents = asyncio.run(scenario())
    assert candidates[0].symbol == "BTCUSDT"
    assert outcome.risk.accepted
    assert outcome.execution is not None and outcome.execution.status == "FILLED"
    assert intents[0]["intent"]["action"] == "OPEN_LONG"


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
        engine.select_provider("fake-auth")
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
            providers=ProviderRegistry([FakeProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider("fake-auth")
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
    # but the size must come from the refreshed price: risking 100 over a 3.101
    # per-unit loss is 32.247, where the stale price would have sized 47.619.
    # The fill itself comes back from the exchange, so it is not ours to assert.
    assert orders[0].stop_price == Decimal("98.00")
    assert orders[0].quantity == Decimal("32.247")


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
        engine.select_provider("fake-auth")
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
        engine.select_provider("fake-auth")
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
        engine.select_provider("fake-auth")
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
    from candlepilot.application.engine import SUPPORTED_CADENCES

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
        engine.select_cadences(["30m", "15m"])  # unordered input
        normalized = engine.active_cadences

        errors = {}
        for label, cadences in (("invalid", ["1m"]), ("empty", [])):
            try:
                engine.select_cadences(cadences)
            except ValueError:
                errors[label] = True

        engine.select_provider("fake-auth")
        await engine.start()
        try:
            engine.select_cadences(["5m"])
            errors["locked"] = False
        except RuntimeError:
            errors["locked"] = True
        await database.close()
        return default, normalized, errors

    default, normalized, errors = asyncio.run(scenario())
    assert default == SUPPORTED_CADENCES
    assert normalized == ("15m", "30m")  # normalized to canonical order
    assert errors == {"invalid": True, "empty": True, "locked": True}


def test_evaluate_stop_reason_covers_duration_budget_and_route_exhaustion() -> None:
    from candlepilot.application.engine import ROUTE_EXHAUSTION_STOP_AFTER

    engine = TradingEngine.__new__(TradingEngine)  # pure check; no I/O needed
    now = datetime.now(UTC)
    engine.running = True
    engine.run_started_at = now - timedelta(seconds=100)
    engine.max_run_seconds = None
    engine.max_run_cost_usd = None
    engine.route_exhausted_since = None

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
    engine.route_exhausted_since = now - ROUTE_EXHAUSTION_STOP_AFTER + timedelta(seconds=1)
    assert engine.evaluate_stop_reason(now=now) is None  # still within grace
    engine.route_exhausted_since = now - ROUTE_EXHAUSTION_STOP_AFTER
    assert "every provider" in engine.evaluate_stop_reason(now=now)

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
        engine.select_provider("fake-auth")
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


def test_route_exhaustion_is_tracked_and_cleared(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'exhaust.db'}")
        await database.initialize()
        market = FakeMarket()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([FailedProvider(), FakeProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
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
        engine.select_provider("failed-primary")
        await engine.start()
        await engine.evaluate(snapshot, portfolio, rules)
        exhausted = engine.route_exhausted_since
        await engine.stop()

        # A working provider clears the exhaustion marker on the next success.
        engine.select_provider("fake-auth")
        await engine.start()
        engine.route_exhausted_since = datetime.now(UTC)
        await engine.evaluate(snapshot, portfolio, rules)
        cleared = engine.route_exhausted_since
        await database.close()
        return exhausted, cleared

    exhausted, cleared = asyncio.run(scenario())
    assert exhausted is not None
    assert cleared is None


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
        engine.select_provider("fake-auth")
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
        engine.select_provider("fake-auth")
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
                "entry succeeded but protection and rescue both failed",
                entry=ExecutionReport(
                    client_order_id=order.client_order_id,
                    status="FILLED",
                    filled_quantity="1",
                    average_price="100",
                ),
                rescue=None,
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
        engine.select_provider("fake-auth")
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
            events[0]["execution"],
        )
        await database.close()
        return result

    running, locked, flattened, execution = asyncio.run(scenario())
    assert running is False
    assert locked is True
    assert flattened is True
    assert execution["status"] == "FAILED"
    assert execution["stage"] == "RESCUE"


def test_engine_fails_over_once_to_explicit_backup_provider(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'fallback.db'}")
        await database.initialize()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([FailedProvider(), FakeProvider()]),
            audit=AuditRepository(database.sessions),
            market=FakeMarket(),  # type: ignore[arg-type]
        )
        engine.select_provider("failed-primary", "fake-auth")
        await engine.start()
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
        await database.close()
        return outcome

    outcome = asyncio.run(scenario())
    assert outcome.provider == "fake-auth"
    assert outcome.execution is not None


def test_ordered_provider_route_cools_down_and_recovers_primary(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'route-recovery.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        flaky = FlakyProvider()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([flaky, FakeProvider()]),
            audit=audit,
            market=FakeMarket(),  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["flaky-auth", "fake-auth"])
        await engine.start()
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            cadence="5m",
            timestamp=datetime.now(UTC),
            mark_price="100",
            bid="99.9",
            ask="100.1",
            quote_volume_24h="1000000",
        )
        portfolio = PortfolioState(equity="10000", available_balance="8000")
        rules = SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01"))

        first = await engine.evaluate(snapshot, portfolio, rules)
        first_status = engine.provider_route_status()
        second = await engine.evaluate(snapshot, portfolio, rules)
        calls_during_cooldown = flaky.calls
        engine._provider_route_states["flaky-auth"].cooldown_until = datetime.now(UTC) - timedelta(
            seconds=1
        )
        third = await engine.evaluate(snapshot, portfolio, rules)
        events = await audit.recent_decision_events(limit=10)
        failed_event = next(
            event
            for event in events
            if event["provider"] == "flaky-auth"
            and "provider attempt failed" in event["intent"]["rationale"]
        )
        failed_detail = await audit.decision_detail(failed_event["id"])
        await database.close()
        return (
            first,
            second,
            third,
            first_status,
            calls_during_cooldown,
            flaky.calls,
            failed_detail,
        )

    first, second, third, route_status, cooldown_calls, final_calls, failed_detail = (
        asyncio.run(scenario())
    )
    assert first.provider == "fake-auth"
    assert second.provider == "fake-auth"
    assert third.provider == "flaky-auth"
    assert route_status[0]["state"] == "cooldown"
    assert route_status[1]["state"] == "active"
    assert cooldown_calls == 1
    assert final_calls == 2
    assert failed_detail["usage"]["failover_attempt"] is True
    assert failed_detail["usage"]["failover_continues"] is True


def test_engine_starts_on_ready_fallback_when_primary_is_unavailable(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'startup-fallback.db'}")
        await database.initialize()
        unavailable = UnavailableProvider()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([unavailable, FakeProvider()]),
            audit=AuditRepository(database.sessions),
            market=FakeMarket(),  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["unavailable-auth", "fake-auth"])
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
        await database.close()
        return engine.active_provider, unavailable.calls, outcome.provider

    active, unavailable_calls, outcome_provider = asyncio.run(scenario())
    assert active == "fake-auth"
    assert unavailable_calls == 0
    assert outcome_provider == "fake-auth"


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
        )
        engine.select_provider("audited-failure")
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

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'testnet-portfolio.db'}")
        await database.initialize()
        engine = TradingEngine(
            providers=ProviderRegistry([FakeProvider()]),
            audit=AuditRepository(database.sessions),
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
    assert position.stop_loss == Decimal("104")
    # No take-profit leg on the exchange must read as absent, not as invented.
    assert position.take_profit is None
    # A flat symbol is not a position.
    assert "ETHUSDT" not in portfolio.positions
