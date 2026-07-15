import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from candlepilot.application.engine import TradingEngine
from candlepilot.domain.models import (
    MarketSnapshot,
    PortfolioState,
    ProviderHealth,
    TradeAction,
    TradeIntent,
    TradingMode,
)
from candlepilot.market.scanner import MarketCandidateInput
from candlepilot.providers.base import LLMProvider, ProviderResult
from candlepilot.providers.cli import ProviderError
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.risk.engine import SymbolRules
from candlepilot.storage.database import AuditRepository, Database


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


class FakeMarket:
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


def test_engine_requires_provider_and_audits_paper_fill(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'engine.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        engine = TradingEngine(
            mode=TradingMode.PAPER,
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
            SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5")),
        )
        intents = await audit.recent_intents()
        await database.close()
        return candidates, outcome, intents

    candidates, outcome, intents = asyncio.run(scenario())
    assert candidates[0].symbol == "BTCUSDT"
    assert outcome.risk.accepted
    assert outcome.execution is not None and outcome.execution.status == "FILLED"
    assert intents[0]["intent"]["action"] == "OPEN_LONG"


def test_engine_cadence_selection_validates_and_locks_when_running(tmp_path: Path) -> None:
    from candlepilot.application.engine import SUPPORTED_CADENCES

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cadence-engine.db'}")
        await database.initialize()
        engine = TradingEngine(
            mode=TradingMode.PAPER,
            providers=ProviderRegistry([FakeProvider()]),
            audit=AuditRepository(database.sessions),
            market=FakeMarket(),  # type: ignore[arg-type]
        )
        default = engine.active_cadences
        engine.select_cadences(["15m", "1m"])  # unordered input
        normalized = engine.active_cadences

        errors = {}
        for label, cadences in (("invalid", ["30m"]), ("empty", [])):
            try:
                engine.select_cadences(cadences)
            except ValueError:
                errors[label] = True

        engine.select_provider("fake-auth")
        await engine.start()
        try:
            engine.select_cadences(["1m"])
            errors["locked"] = False
        except RuntimeError:
            errors["locked"] = True
        await database.close()
        return default, normalized, errors

    default, normalized, errors = asyncio.run(scenario())
    assert default == SUPPORTED_CADENCES
    assert normalized == ("1m", "15m")  # normalized to canonical order
    assert errors == {"invalid": True, "empty": True, "locked": True}


def test_testnet_mode_refuses_to_start_without_credentials(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'testnet.db'}")
        await database.initialize()
        engine = TradingEngine(
            mode=TradingMode.TESTNET,
            providers=ProviderRegistry([FakeProvider()]),
            audit=AuditRepository(database.sessions),
            market=FakeMarket(),  # type: ignore[arg-type]
        )
        engine.select_provider("fake-auth")
        try:
            await engine.start()
            raise AssertionError("testnet should require broker credentials")
        except RuntimeError as exc:
            assert "credentials" in str(exc)
        await database.close()

    asyncio.run(scenario())


def test_testnet_add_requests_protective_bracket_replacement(tmp_path: Path) -> None:
    from candlepilot.broker.binance_testnet import ReconciliationReport
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
            mode=TradingMode.TESTNET,
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
                symbol_sides={"BTCUSDT": "LONG"},
                symbol_quantities={"BTCUSDT": "1"},
            ),
            SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5")),
        )
        await database.close()
        return outcome, broker.replace_existing_protection

    outcome, replacement = asyncio.run(scenario())
    assert outcome.execution is not None
    assert replacement is True


def test_engine_fails_over_once_to_explicit_backup_provider(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'fallback.db'}")
        await database.initialize()
        engine = TradingEngine(
            mode=TradingMode.PAPER,
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
            SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5")),
        )
        await database.close()
        return outcome

    outcome = asyncio.run(scenario())
    assert outcome.provider == "fake-auth"
    assert outcome.execution is not None


def test_emergency_lock_persists_and_expires_at_next_utc_day(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'runtime-state.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        first = TradingEngine(
            mode=TradingMode.PAPER,
            providers=ProviderRegistry([FakeProvider()]),
            audit=audit,
            market=FakeMarket(),  # type: ignore[arg-type]
        )
        stopped_at = datetime(2026, 1, 1, 22, 30, tzinfo=UTC)
        await first.emergency_stop(now=stopped_at)

        restored = TradingEngine(
            mode=TradingMode.PAPER,
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
