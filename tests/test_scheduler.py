import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from candlepilot.application.engine import TradingEngine
from candlepilot.application.scheduler import TradingScheduler
from candlepilot.domain.models import MarketSnapshot, ProviderHealth, TradeIntent
from candlepilot.market.binance import ContractInfo
from candlepilot.market.scanner import MarketCandidateInput
from candlepilot.providers.base import DecisionProvider, ProviderResult
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.risk.engine import SymbolRules
from candlepilot.storage.database import AuditRepository, Database
from conftest import FakeTestnetBroker, StatefulTestnetBroker


class HoldProvider(DecisionProvider):
    name = "hold"

    async def health_check(self):
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio):
        intent = TradeIntent.hold(snapshot.symbol, snapshot.cadence, "no edge")
        return ProviderResult(intent, self.name, None, timedelta(0), intent.model_dump_json(), {})


class ConflictingProvider(DecisionProvider):
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
            # Every entry is bracketed on the exchange, so a take profit is not
            # optional the way it was for the simulated executor.
            take_profit="104" if long else "96",
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
                SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")),
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


class FakeTestnetFeed:
    def __init__(self):
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


def test_scheduler_runs_ranked_candidate_cycle(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'scheduler.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["hold"])
        await engine.start()
        scheduler = TradingScheduler(engine, market, candidates_per_cycle=5)  # type: ignore[arg-type]
        outcomes = await scheduler.run_cycle("5m")
        await database.close()
        return outcomes

    outcomes = asyncio.run(scenario())
    assert len(outcomes) == 1
    assert outcomes[0].intent.action.value == "HOLD"


def test_scheduler_run_once_uses_account_feed_without_starting_timers(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'run-once.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["hold"])
        await engine.start()
        feed = FakeTestnetFeed()
        scheduler = TradingScheduler(
            engine,
            market,  # type: ignore[arg-type]
            testnet_feed=feed,  # type: ignore[arg-type]
        )
        outcomes = await scheduler.run_once("15m")
        names = [task.get_name() for task in scheduler._tasks]
        await database.close()
        return outcomes, feed.started, feed.stopped, names

    outcomes, started, stopped, names = asyncio.run(scenario())
    assert len(outcomes) == 1
    assert started is True
    assert stopped is True
    assert names == []


def test_scheduler_stop_cancels_an_inflight_single_cycle(tmp_path: Path) -> None:
    async def scenario():
        started = asyncio.Event()

        class GatedProvider(HoldProvider):
            async def generate_trade_intents(self, snapshots, portfolio):
                started.set()
                await asyncio.Event().wait()
                return []

        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cancel-once.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([GatedProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["hold"])
        await engine.start()
        scheduler = TradingScheduler(engine, market)  # type: ignore[arg-type]
        task = asyncio.create_task(scheduler.run_once("15m"))
        await started.wait()
        await scheduler.stop()
        cancelled = task.cancelled()
        await database.close()
        return cancelled, engine.running

    cancelled, engine_running = asyncio.run(scenario())
    assert cancelled is True
    # The caller chooses graceful versus emergency engine shutdown after the
    # in-flight inference has been cancelled.
    assert engine_running is True


def test_scheduler_submits_one_batch_for_all_cycle_symbols(tmp_path: Path) -> None:
    async def scenario():
        class TwoSymbolMarket(SchedulerMarket):
            async def candidate_inputs(self):
                first = (await super().candidate_inputs())[0]
                return [
                    first,
                    MarketCandidateInput(
                        "ETHUSDT", Decimal("900000"), Decimal("199.9"),
                        Decimal("200.1"), Decimal("0.1"), Decimal("0.03"), 1000,
                    ),
                ]

            async def exchange_info(self):
                rules = SymbolRules(
                    Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")
                )
                listed_at = datetime(2020, 1, 1, tzinfo=UTC)
                return {
                    "BTCUSDT": ContractInfo("BTCUSDT", listed_at, rules),
                    "ETHUSDT": ContractInfo("ETHUSDT", listed_at, rules),
                }

        class BatchProvider(HoldProvider):
            batch_calls = 0

            async def generate_trade_intents(self, snapshots, portfolio):
                self.batch_calls += 1
                return [
                    await super().generate_trade_intent(snapshot, portfolio)
                    for snapshot in snapshots
                ]

        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'batch-cycle.db'}")
        await database.initialize()
        market = TwoSymbolMarket()
        provider = BatchProvider()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([provider]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["hold"])
        await engine.start()
        outcomes = await TradingScheduler(
            engine, market, candidates_per_cycle=2  # type: ignore[arg-type]
        ).run_cycle("5m")
        await database.close()
        return provider.batch_calls, outcomes

    calls, outcomes = asyncio.run(scenario())
    assert calls == 1
    assert sorted(outcome.intent.symbol for outcome in outcomes) == ["BTCUSDT", "ETHUSDT"]


def test_scheduler_only_runs_selected_cadences(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'cadences.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["hold"])
        engine.select_cadences(["1h"])
        await engine.start()
        scheduler = TradingScheduler(engine, market)  # type: ignore[arg-type]
        scheduler.start()
        names = sorted(task.get_name() for task in scheduler._tasks)
        await scheduler.stop()
        await database.close()
        return names

    names = asyncio.run(scenario())
    assert names == [
        "candlepilot-1h",
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
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["hold"])
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
                        SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")),
                    )
                    for symbol in symbols
                }

        market = MultiSymbolMarket()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["hold"])
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

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'held-symbol.db'}")
        await database.initialize()

        class HeldSymbolMarket(SchedulerMarket):
            async def exchange_info(self):
                rules = SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01"))
                return {
                    symbol: ContractInfo(
                        symbol,
                        datetime(2020, 1, 1, tzinfo=UTC),
                        rules,
                    )
                    for symbol in ("BTCUSDT", "ETHUSDT")
                }

        market = HeldSymbolMarket()
        # ETHUSDT is already held on the exchange but is not a candidate.
        engine = TradingEngine(
            testnet_broker=StatefulTestnetBroker(  # type: ignore[arg-type]
                {"ETHUSDT": ("LONG", Decimal("1"), Decimal("100"))}
            ),
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["hold"])
        await engine.start()
        scheduler = TradingScheduler(engine, market, candidates_per_cycle=1)  # type: ignore[arg-type]
        outcomes = await scheduler.run_cycle("5m")
        await database.close()
        return [outcome.intent.symbol for outcome in outcomes]

    assert asyncio.run(scenario()) == ["BTCUSDT", "ETHUSDT"]


def test_scheduler_uses_testnet_filters_instead_of_production_filters(tmp_path: Path) -> None:
    async def scenario():
        class VenueBroker(FakeTestnetBroker):
            async def tradable_contract_rules(self):
                return {
                    "BTCUSDT": SymbolRules(
                        Decimal("0.1"), Decimal("0.2"), Decimal("100"), Decimal("0.5")
                    )
                }

        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'venue-rules.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            testnet_broker=VenueBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["hold"])
        await engine.start()
        observed: list[SymbolRules] = []
        evaluate_batch = engine.evaluate_batch

        async def capture(snapshots, portfolio, rules_by_symbol):
            observed.extend(rules_by_symbol.values())
            return await evaluate_batch(snapshots, portfolio, rules_by_symbol)

        engine.evaluate_batch = capture  # type: ignore[method-assign]
        scheduler = TradingScheduler(engine, market)  # type: ignore[arg-type]
        await scheduler.run_cycle("5m")
        await database.close()
        return observed

    observed = asyncio.run(scenario())
    assert len(observed) == 1
    assert observed[0].quantity_step == Decimal("0.1")
    assert observed[0].tick_size == Decimal("0.5")


def test_scheduler_reports_a_held_symbol_missing_testnet_rules(tmp_path: Path) -> None:
    async def scenario():
        class VenueBroker(StatefulTestnetBroker):
            async def tradable_contract_rules(self):
                return {
                    "BTCUSDT": SymbolRules(
                        Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01")
                    )
                }

        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'missing-held-rules.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            testnet_broker=VenueBroker(
                {"ETHUSDT": ("LONG", Decimal("1"), Decimal("100"))}
            ),  # type: ignore[arg-type]
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["hold"])
        await engine.start()
        scheduler = TradingScheduler(engine, market)  # type: ignore[arg-type]
        try:
            await scheduler.run_cycle("5m")
        except RuntimeError as exc:
            message = str(exc)
        else:
            message = None
        await database.close()
        return message

    assert asyncio.run(scenario()) == (
        "testnet contract rules are unavailable for held or selected symbol ETHUSDT"
    )


def test_guard_stops_the_run_when_a_limit_is_reached(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'guard.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["hold"])
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


def test_three_rescues_auto_stop_one_live_run_and_report_the_reason(tmp_path: Path) -> None:
    from candlepilot.broker.binance_testnet import ProtectiveStopError
    from candlepilot.domain.models import ExecutionReport

    class RescueBroker(FakeTestnetBroker):
        async def execute_with_stop(self, order, **_):
            entry = ExecutionReport(
                client_order_id=order.client_order_id,
                status="FILLED",
                filled_quantity=order.quantity,
                average_price="100",
            )
            rescue = ExecutionReport(
                client_order_id=f"{order.client_order_id}-rescue",
                status="FILLED",
                filled_quantity=order.quantity,
                average_price="99",
            )
            raise ProtectiveStopError(
                "entry succeeded but protective bracket failed; rescued",
                entry=entry,
                rescue=rescue,
                exchange_error_code=-4130,
                estimated_loss_usdt=order.quantity,
                failed_stage="PROTECTION",
                requires_emergency_lock=False,
            )

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'rescue-limit.db'}")
        await database.initialize()
        audit = AuditRepository(database.sessions)
        market = SchedulerMarket()
        engine = TradingEngine(
            testnet_broker=RescueBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([ConflictingProvider()]),
            audit=audit,
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["conflicting"])
        await engine.start()
        scheduler = TradingScheduler(engine, market)  # type: ignore[arg-type]
        cycle_sizes = []
        for _ in range(3):
            cycle_sizes.append(len(await scheduler.run_cycle("5m")))
        if scheduler._auto_stop_task is not None:
            await scheduler._auto_stop_task
        events = await audit.recent_decision_events()
        stopped = (
            engine.running,
            engine.rescue_count,
            engine.auto_stop_reason,
            events[0]["live_run"],
            len(await scheduler.run_cycle("5m")),
        )
        await engine.start()
        reset_count = engine.rescue_count
        await engine.stop()
        await database.close()
        return cycle_sizes, stopped, reset_count

    cycle_sizes, stopped, reset_count = asyncio.run(scenario())
    running, count, reason, live_run, fourth_cycle_size = stopped
    assert cycle_sizes == [1, 1, 1]
    assert running is False
    assert count == 3
    assert reason == "本次运行累计紧急回补 3 次，达到安全上限 3 次"
    assert live_run["status"] == "auto_stopped"
    assert live_run["stop_reason"] == reason
    assert live_run["config"]["rescue_limit"] == 3
    assert fourth_cycle_size == 0
    assert reset_count == 0


def test_guard_emergency_stops_when_the_user_feed_dies(tmp_path: Path) -> None:
    class DeadFeed:
        running = False

        def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'dead-feed.db'}")
        await database.initialize()
        market = SchedulerMarket()
        broker = FakeTestnetBroker()
        engine = TradingEngine(
            testnet_broker=broker,  # type: ignore[arg-type]
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["hold"])
        await engine.start()
        scheduler = TradingScheduler(
            engine,
            market,  # type: ignore[arg-type]
            guard_interval_seconds=0.01,
            testnet_feed=DeadFeed(),  # type: ignore[arg-type]
        )
        scheduler.start()
        for _ in range(200):
            if not engine.running:
                break
            await asyncio.sleep(0.01)
        if scheduler._auto_stop_task is not None:
            await scheduler._auto_stop_task
        result = (
            engine.running,
            engine.emergency_locked,
            engine.auto_stop_reason,
            broker.flattened,
        )
        await database.close()
        return result

    running, locked, reason, flattened = asyncio.run(scenario())
    assert running is False and locked is True and flattened is True
    assert reason is not None and "user stream stopped" in reason


def test_automatic_emergency_stop_cancels_decisions_before_flattening(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'emergency-order.db'}")
        await database.initialize()
        provider_started = asyncio.Event()
        provider_cancelled = asyncio.Event()

        class GatedProvider(HoldProvider):
            async def generate_trade_intent(self, snapshot, portfolio):
                provider_started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    provider_cancelled.set()
                    raise

        class OrderingBroker(FakeTestnetBroker):
            flattened_after_cancellation = False

            async def emergency_flatten(self):
                self.flattened_after_cancellation = provider_cancelled.is_set()
                await super().emergency_flatten()

        market = SchedulerMarket()
        broker = OrderingBroker()
        engine = TradingEngine(
            testnet_broker=broker,  # type: ignore[arg-type]
            providers=ProviderRegistry([GatedProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["hold"])
        await engine.start()
        scheduler = TradingScheduler(engine, market)  # type: ignore[arg-type]
        cycle = asyncio.create_task(scheduler.run_cycle("5m"))
        scheduler._tasks = [cycle]
        await asyncio.wait_for(provider_started.wait(), timeout=1)

        scheduler.request_emergency_stop("test safety trigger")
        assert scheduler._auto_stop_task is not None
        await scheduler._auto_stop_task
        result = broker.flattened_after_cancellation, cycle.cancelled(), engine.emergency_locked
        await database.close()
        return result

    flattened_after_cancellation, cycle_cancelled, emergency_locked = asyncio.run(scenario())
    assert flattened_after_cancellation is True
    assert cycle_cancelled is True
    assert emergency_locked is True


def test_automatic_graceful_stop_cancels_decisions_before_finishing_run(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'graceful-order.db'}")
        await database.initialize()
        provider_started = asyncio.Event()
        provider_cancelled = asyncio.Event()

        class GatedProvider(HoldProvider):
            async def generate_trade_intent(self, snapshot, portfolio):
                provider_started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    provider_cancelled.set()
                    raise

        market = SchedulerMarket()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([GatedProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["hold"])
        await engine.start()
        scheduler = TradingScheduler(engine, market)  # type: ignore[arg-type]
        cycle = asyncio.create_task(scheduler.run_cycle("5m"))
        scheduler._tasks = [cycle]
        await asyncio.wait_for(provider_started.wait(), timeout=1)

        scheduler.request_auto_stop("test graceful trigger")
        assert scheduler._auto_stop_task is not None
        await scheduler._auto_stop_task
        result = provider_cancelled.is_set(), cycle.cancelled(), engine.running
        await database.close()
        return result

    provider_cancelled, cycle_cancelled, running = asyncio.run(scenario())
    assert provider_cancelled is True
    assert cycle_cancelled is True
    assert running is False


def test_guard_only_loads_cost_when_a_budget_is_set(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'guard-cost.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["hold"])
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
        # The opposing-entry rule reads open positions back out of the account,
        # so the first fill has to be visible to the second cadence.
        engine = TradingEngine(
            testnet_broker=StatefulTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([ConflictingProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["conflicting"])
        await engine.start()
        await engine.refresh_universe()
        scheduler = TradingScheduler(engine, market)  # type: ignore[arg-type]
        cycles = await asyncio.gather(
            scheduler.run_cycle("5m"), scheduler.run_cycle("15m")
        )
        portfolio = await engine.current_portfolio()
        await database.close()
        return cycles, portfolio

    cycles, portfolio = asyncio.run(scenario())
    outcomes = [outcome for cycle in cycles for outcome in cycle]
    assert sum(outcome.execution is not None for outcome in outcomes) == 1
    assert sum(outcome.risk.accepted for outcome in outcomes) == 1
    assert portfolio.open_positions == 1


def test_scheduler_refreshes_universe_periodically(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'universe-scheduler.db'}")
        await database.initialize()
        market = SchedulerMarket()
        engine = TradingEngine(
            testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
            providers=ProviderRegistry([HoldProvider()]),
            audit=AuditRepository(database.sessions),
            market=market,  # type: ignore[arg-type]
        )
        engine.select_provider_chain(["hold"])
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
