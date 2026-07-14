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
