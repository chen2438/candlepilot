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


class ConflictingProvider(LLMProvider):
    name = "conflicting"

    async def health_check(self):
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio):
        long = snapshot.cadence == "5m"
        intent = TradeIntent(
            symbol=snapshot.symbol,
            cadence=snapshot.cadence,
            action="OPEN_LONG" if long else "OPEN_SHORT",
            confidence=0.9,
            leverage=2,
            risk_fraction="0.01",
            stop_loss="98" if long else "102",
            rationale="conflict fixture",
        )
        await asyncio.sleep(0.01)
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


class FakePaperFeed:
    def __init__(self):
        self.started: list[list[str]] = []
        self.stopped = False

    async def start(self, symbols):
        self.started.append(symbols)

    async def stop(self):
        self.stopped = True


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


def test_scheduler_only_runs_selected_cadences(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cadences.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            mode=TradingMode.PAPER,
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider("hold")
        engine.select_cadences(["30m", "15m"])  # order-insensitive input
        await engine.start()
        scheduler = TradingScheduler(engine, market)  # type: ignore[arg-type]
        scheduler.start()
        names = sorted(task.get_name() for task in scheduler._tasks)
        await scheduler.stop()
        await database.close()
        return names

    names = asyncio.run(scenario())
    assert names == [
        "candlepilot-15m",
        "candlepilot-30m",
        "candlepilot-guard",
        "candlepilot-universe",
    ]


def test_scheduler_candidates_per_cycle_validates_and_locks_when_running(
    tmp_path: Path,
) -> None:
    import pytest

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'per-cycle.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            mode=TradingMode.PAPER,
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider("hold")
        scheduler = TradingScheduler(engine, market)  # type: ignore[arg-type]

        # invalid bounds are rejected
        with pytest.raises(ValueError):
            scheduler.select_candidates_per_cycle(0)
        with pytest.raises(ValueError):
            scheduler.select_candidates_per_cycle(21)

        scheduler.select_candidates_per_cycle(3)
        assert scheduler.candidates_per_cycle == 3

        # locked once the engine is running
        await engine.start()
        with pytest.raises(RuntimeError):
            scheduler.select_candidates_per_cycle(7)
        assert scheduler.candidates_per_cycle == 3

        await database.close()

    asyncio.run(scenario())


def test_scheduler_limits_cycle_to_candidates_per_cycle(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'per-cycle-limit.db'}")
        await database.initialize()

        symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"]

        class MultiSymbolMarket(SchedulerMarket):
            async def candidate_inputs(self):
                self.candidate_calls += 1
                return [
                    MarketCandidateInput(
                        symbol,
                        Decimal("1000000"),
                        Decimal("99.9"),
                        Decimal("100.1"),
                        Decimal("0.1"),
                        Decimal("0.03"),
                        1000,
                    )
                    for symbol in symbols
                ]

            async def exchange_info(self):
                return {
                    symbol: ContractInfo(
                        symbol,
                        datetime(2020, 1, 1, tzinfo=UTC),
                        SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5")),
                    )
                    for symbol in symbols
                }

        market = MultiSymbolMarket()
        engine = TradingEngine(
            mode=TradingMode.PAPER,
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider("hold")
        await engine.start()
        scheduler = TradingScheduler(engine, market, candidates_per_cycle=2)  # type: ignore[arg-type]
        outcomes = await scheduler.run_cycle("5m")
        await database.close()
        return outcomes

    outcomes = asyncio.run(scenario())
    assert len(outcomes) == 2


def test_scheduler_always_analyzes_open_positions_outside_candidate_limit(
    tmp_path: Path,
) -> None:
    from candlepilot.domain.models import OrderPlan, OrderType

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'held-symbol.db'}")
        await database.initialize()

        class HeldSymbolMarket(SchedulerMarket):
            async def exchange_info(self):
                rules = SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"))
                return {
                    symbol: ContractInfo(
                        symbol,
                        datetime(2020, 1, 1, tzinfo=UTC),
                        rules,
                    )
                    for symbol in ("BTCUSDT", "ETHUSDT")
                }

        market = HeldSymbolMarket()
        engine = TradingEngine(
            mode=TradingMode.PAPER,
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        await engine.paper_executor.execute(
            OrderPlan(
                client_order_id="held-eth",
                symbol="ETHUSDT",
                side="BUY",
                quantity=Decimal("1"),
                order_type=OrderType.MARKET,
                stop_price=Decimal("90"),
            ),
            await market.market_snapshot("ETHUSDT", "5m"),
        )
        engine.select_provider("hold")
        await engine.start()
        scheduler = TradingScheduler(engine, market, candidates_per_cycle=1)  # type: ignore[arg-type]
        outcomes = await scheduler.run_cycle("5m")
        await database.close()
        return [outcome.intent.symbol for outcome in outcomes]

    assert asyncio.run(scenario()) == ["BTCUSDT", "ETHUSDT"]


def test_guard_stops_the_run_when_a_limit_is_reached(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'guard.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            mode=TradingMode.PAPER,
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider("hold")
        # Already past the duration limit the moment the guard first ticks.
        engine.select_run_limits(max_run_seconds=1, max_run_cost_usd=None)
        await engine.start()
        engine.run_started_at = datetime.now(UTC) - timedelta(seconds=30)
        scheduler = TradingScheduler(
            engine,  # type: ignore[arg-type]
            market,  # type: ignore[arg-type]
            guard_interval_seconds=0.01,
        )
        scheduler.start()
        for _ in range(200):
            if not engine.running:
                break
            await asyncio.sleep(0.01)
        if scheduler._auto_stop_task is not None:
            await scheduler._auto_stop_task
        running, reason, tasks = engine.running, engine.auto_stop_reason, scheduler._tasks
        await database.close()
        return running, reason, tasks

    running, reason, tasks = asyncio.run(scenario())
    assert running is False
    assert reason is not None and "duration limit" in reason
    assert tasks == []  # the scheduler tore its own tasks down


def test_guard_only_loads_cost_when_a_budget_is_set(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'guard-cost.db'}")
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
        calls = {"count": 0}

        async def loader():
            calls["count"] += 1
            return 99.0

        scheduler = TradingScheduler(
            engine,  # type: ignore[arg-type]
            market,  # type: ignore[arg-type]
            guard_interval_seconds=0.01,
            run_cost_loader=loader,
        )
        # No budget configured: the cost loader must not be called at all.
        scheduler.start()
        await asyncio.sleep(0.05)
        await scheduler.stop()
        without_budget = calls["count"]
        assert engine.running is True

        # With a budget, the loader drives the stop.
        await engine.stop()
        engine.select_run_limits(max_run_seconds=None, max_run_cost_usd=1.0)
        await engine.start()
        scheduler.start()
        for _ in range(200):
            if not engine.running:
                break
            await asyncio.sleep(0.01)
        if scheduler._auto_stop_task is not None:
            await scheduler._auto_stop_task
        reason = engine.auto_stop_reason
        await database.close()
        return without_budget, calls["count"], reason

    without_budget, with_budget, reason = asyncio.run(scenario())
    assert without_budget == 0
    assert with_budget > 0
    assert reason is not None and "cost budget" in reason


def test_concurrent_cadences_cannot_open_opposing_positions(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'conflict.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            mode=TradingMode.PAPER,
            providers=ProviderRegistry([ConflictingProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider("conflicting")
        await engine.start()
        await engine.refresh_universe()
        scheduler = TradingScheduler(engine, market)  # type: ignore[arg-type]
        cycles = await asyncio.gather(
            scheduler.run_cycle("5m"), scheduler.run_cycle("15m")
        )
        portfolio = engine.paper_executor.portfolio_state()
        await database.close()
        return cycles, portfolio

    cycles, portfolio = asyncio.run(scenario())
    outcomes = [outcome for cycle in cycles for outcome in cycle]
    assert sum(outcome.execution is not None for outcome in outcomes) == 1
    assert sum(outcome.risk.accepted for outcome in outcomes) == 1
    assert portfolio.open_positions == 1


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
    assert portfolio.positions["BTCUSDT"].side == "LONG"
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


def test_market_feed_tracks_candidates_and_open_positions(tmp_path: Path) -> None:
    from candlepilot.domain.models import OrderPlan, OrderType

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'feed-sync.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            mode=TradingMode.PAPER,
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        await engine.paper_executor.execute(
            OrderPlan(
                client_order_id="eth-position",
                symbol="ETHUSDT",
                side="BUY",
                quantity=Decimal("1"),
                order_type=OrderType.MARKET,
                stop_price=Decimal("90"),
            ),
            MarketSnapshot(
                symbol="ETHUSDT",
                cadence="1m",
                timestamp=datetime.now(UTC),
                mark_price="100",
                bid="99.9",
                ask="100.1",
                quote_volume_24h="1000000",
            ),
        )
        await engine.refresh_universe()
        feed = FakePaperFeed()
        scheduler = TradingScheduler(engine, market, paper_feed=feed)  # type: ignore[arg-type]
        await scheduler.sync_market_feed()
        await scheduler.stop()
        await database.close()
        return feed

    feed = asyncio.run(scenario())
    assert feed.started == [["BTCUSDT", "ETHUSDT"]]
    assert feed.stopped
