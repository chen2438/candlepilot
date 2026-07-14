import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from candlepilot.application.engine import TradingEngine
from candlepilot.application.scheduler import TradingScheduler
from candlepilot.domain.models import MarketSnapshot, ProviderHealth, TradeIntent, TradingMode
from candlepilot.market.binance import ContractInfo
from candlepilot.market.scanner import MarketCandidateInput
from candlepilot.providers.base import LLMProvider, ProviderResult
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.risk.engine import SymbolRules
from candlepilot.storage.database import AuditRepository, Database


class HoldProvider(LLMProvider):
    name = "hold"

    async def health_check(self):
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio):
        intent = TradeIntent.hold(snapshot.symbol, snapshot.cadence, "no edge")
        return ProviderResult(intent, self.name, None, timedelta(0), intent.model_dump_json(), {})


class SchedulerMarket:
    def __init__(self):
        self.candidate_calls = 0
        self.mark_price = Decimal("100")

    async def candidate_inputs(self):
        self.candidate_calls += 1
        return [
            MarketCandidateInput(
                "BTCUSDT",
                Decimal("1000000"),
                Decimal("99.9"),
                Decimal("100.1"),
                Decimal("0.1"),
                Decimal("0.03"),
                1000,
            )
        ]

    async def exchange_info(self):
        return {
            "BTCUSDT": ContractInfo(
                "BTCUSDT",
                datetime(2020, 1, 1, tzinfo=UTC),
                SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5")),
            )
        }

    async def market_snapshot(self, symbol, cadence):
        return MarketSnapshot(
            symbol=symbol,
            cadence=cadence,
            timestamp=datetime.now(UTC),
            mark_price=self.mark_price,
            bid=self.mark_price - Decimal("0.1"),
            ask=self.mark_price + Decimal("0.1"),
            quote_volume_24h="1000000",
        )


def test_scheduler_runs_ranked_candidate_cycle(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'scheduler.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            mode=TradingMode.PAPER,
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider("hold")
        await engine.start()
        scheduler = TradingScheduler(engine, market, candidates_per_cycle=5)  # type: ignore[arg-type]
        outcomes = await scheduler.run_cycle("5m")
        await database.close()
        return outcomes

    outcomes = asyncio.run(scenario())
    assert len(outcomes) == 1
    assert outcomes[0].intent.action.value == "HOLD"


def test_paper_account_tracks_open_position() -> None:
    from candlepilot.domain.models import OrderPlan, OrderType
    from candlepilot.execution.paper import PaperExecutor

    async def scenario():
        executor = PaperExecutor()
        snapshot = await SchedulerMarket().market_snapshot("BTCUSDT", "5m")
        await executor.execute(
            OrderPlan(
                client_order_id="paper-1",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("1"),
                order_type=OrderType.MARKET,
                stop_price=Decimal("98"),
            ),
            snapshot,
            leverage=5,
        )
        return executor.portfolio_state()

    portfolio = asyncio.run(scenario())
    assert portfolio.open_positions == 1
    assert portfolio.symbol_sides["BTCUSDT"] == "LONG"
    assert portfolio.margin_used > 0


def test_scheduler_refreshes_universe_periodically(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'universe-scheduler.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            mode=TradingMode.PAPER,
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider("hold")
        await engine.start()
        scheduler = TradingScheduler(
            engine,
            market,  # type: ignore[arg-type]
            universe_refresh_seconds=0.01,
        )
        scheduler.start()
        await asyncio.sleep(0.035)
        await scheduler.stop()
        await database.close()
        return market.candidate_calls, scheduler

    calls, scheduler = asyncio.run(scenario())
    assert calls >= 2
    assert scheduler.universe_last_error is None
    assert scheduler._tasks == []


def test_scheduler_marks_and_stops_paper_position(tmp_path: Path) -> None:
    from candlepilot.domain.models import OrderPlan, OrderType

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'paper-mark.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            mode=TradingMode.PAPER,
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider("hold")
        await engine.start()
        await engine.paper_executor.execute(
            OrderPlan(
                client_order_id="scheduled-entry",
                symbol="BTCUSDT",
                side="BUY",
                quantity=Decimal("1"),
                order_type=OrderType.MARKET,
                stop_price=Decimal("98"),
            ),
            await market.market_snapshot("BTCUSDT", "5m"),
        )
        market.mark_price = Decimal("97")
        scheduler = TradingScheduler(engine, market)  # type: ignore[arg-type]
        await scheduler.run_cycle("5m")
        orders = engine.paper_executor.orders
        portfolio = engine.paper_executor.portfolio_state()
        await database.close()
        return orders, portfolio

    orders, portfolio = asyncio.run(scenario())
    assert any(report.message == "paper stop_loss" for report in orders)
    assert portfolio.open_positions == 0
