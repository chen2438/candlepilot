import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from candlepilot.backtest.engine import BacktestConfig, Candle
from candlepilot.backtest.runner import (
    BacktestDecision,
    BacktestSpec,
    BacktestRunner,
    ModelRun,
    compare,
    decision_times,
    estimate,
    validate,
)
from candlepilot.backtest.snapshots import INTERVAL_MILLISECONDS
from candlepilot.domain.models import ProviderHealth, TradeAction, TradeIntent
from candlepilot.market.features import DECISION_FEATURE_INTERVALS
from candlepilot.providers.base import LLMProvider, ProviderResult
from candlepilot.risk.engine import AggressiveRiskPolicy, SymbolRules

WINDOW_START = datetime(2026, 6, 1, tzinfo=UTC)
WINDOW_END = WINDOW_START + timedelta(hours=2)
RULES = SymbolRules(Decimal("0.001"), Decimal("0.001"), Decimal("5"), Decimal("0.01"))


def _series(interval: str, *, count: int = 400) -> list[Candle]:
    step = timedelta(milliseconds=INTERVAL_MILLISECONDS[interval])
    first = WINDOW_START - step * (count - 40)
    candles = []
    price = Decimal("100")
    for index in range(count):
        price *= Decimal("1.0005")
        candles.append(
            Candle(
                timestamp=first + step * index,
                open=price,
                high=price * Decimal("1.004"),
                low=price * Decimal("0.996"),
                close=price,
                volume=Decimal("500"),
            )
        )
    return candles


def _all_series() -> dict[str, list[Candle]]:
    series = {interval: _series(interval) for interval in DECISION_FEATURE_INTERVALS}
    series["1d"] = _series("1d", count=60)
    return series


def _spec(**changes) -> BacktestSpec:
    values = dict(
        symbols=("BTCUSDT",),
        cadences=("5m",),
        start=WINDOW_START,
        end=WINDOW_END,
        providers=("model-a",),
        config=BacktestConfig(slippage_fraction=Decimal("0"), fee_rate=Decimal("0")),
    )
    values.update(changes)
    return BacktestSpec(**values)  # type: ignore[arg-type]


class _Provider(LLMProvider):
    def __init__(self, name: str, action: TradeAction = TradeAction.OPEN_LONG) -> None:
        self.name = name
        self._action = action
        self.snapshots: list[object] = []

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio) -> ProviderResult:
        self.snapshots.append(snapshot)
        if self._action == TradeAction.HOLD or snapshot.symbol in portfolio.positions:
            intent = TradeIntent.hold(snapshot.symbol, snapshot.cadence, "fixture")
        else:
            intent = TradeIntent(
                symbol=snapshot.symbol,
                cadence=snapshot.cadence,
                action=TradeAction.OPEN_LONG,
                confidence=0.8,
                leverage=2,
                risk_fraction="0.01",
                stop_loss=snapshot.mark_price * Decimal("0.98"),
                take_profit=snapshot.mark_price * Decimal("1.04"),
                rationale="fixture",
            )
        return ProviderResult(intent, self.name, "m", timedelta(0), "{}", {})


def _runner(spec: BacktestSpec, risk: AggressiveRiskPolicy | None = None) -> BacktestRunner:
    return BacktestRunner(
        spec=spec,
        series={symbol: _all_series() for symbol in spec.symbols},
        rules={symbol: RULES for symbol in spec.symbols},
        # Snapshots are historical, so their age is measured against the
        # decision time, not the wall clock.
        risk=risk or AggressiveRiskPolicy(require_take_profit=True),
        provider_retry_delays=(0, 0),
    )


def test_estimate_counts_the_calls_before_any_are_paid_for() -> None:
    """A day of 5m bars is hours of real calls; the count has to come first."""

    spec = _spec(
        symbols=("BTCUSDT", "ETHUSDT"),
        end=WINDOW_START + timedelta(days=1),
        providers=("model-a", "model-b", "model-c"),
    )

    result = estimate(spec, seconds_per_call=24.0)

    # 288 five-minute bars in a day, two symbols.
    assert result.decisions_per_model == 576
    assert result.total_calls == 1728
    # Models run in parallel, so wall clock is one model's serial pass.
    assert result.estimated_seconds == pytest.approx(576 * 24.0)


def test_estimate_adds_each_cadence_rather_than_multiplying_the_window() -> None:
    one = estimate(_spec(cadences=("5m",)), seconds_per_call=1)
    all_cadences = estimate(
        _spec(cadences=("5m", "15m", "30m", "1h", "4h")), seconds_per_call=1
    )

    # Two hours: 24 five-minute bars, 8 fifteens, 4 thirties, 2 hours, no 4h close.
    assert one.decisions_per_model == 24
    assert all_cadences.decisions_per_model == 38


def test_specs_that_cannot_finish_are_refused() -> None:
    validate(_spec(end=WINDOW_START + timedelta(days=31)))
    with pytest.raises(ValueError, match="cannot exceed 31 days"):
        validate(_spec(end=WINDOW_START + timedelta(days=31, seconds=1)))
    with pytest.raises(ValueError, match="end after it starts"):
        validate(_spec(end=WINDOW_START))
    with pytest.raises(ValueError, match="compared against itself"):
        validate(_spec(providers=("model-a", "model-a")))
    with pytest.raises(ValueError, match="reach into the future"):
        validate(_spec(start=datetime.now(UTC), end=datetime.now(UTC) + timedelta(hours=1)))


def test_decisions_land_on_each_closed_bar_inside_the_window() -> None:
    times = decision_times(_spec(), "5m")

    assert times[0] == WINDOW_START + timedelta(minutes=5)
    assert times[-1] == WINDOW_END
    assert len(times) == 24


def test_four_hour_backtest_decisions_use_the_complete_closed_ladder() -> None:
    spec = _spec(
        cadences=("4h",),
        end=WINDOW_START + timedelta(hours=8),
    )
    provider = _Provider("model-a", TradeAction.HOLD)
    run = ModelRun("model-a")

    asyncio.run(_runner(spec).run(provider, run))

    assert run.decisions_done == run.decisions_total == 2
    assert [snapshot.cadence for snapshot in provider.snapshots] == ["4h", "4h"]
    assert all("4h_ema_spread" in snapshot.features for snapshot in provider.snapshots)


def test_the_run_uses_the_real_risk_policy_not_a_copy_of_it() -> None:
    """The old backtest sized positions itself and never called the policy.

    That skipped the daily-loss breaker, the position cap and tick alignment --
    so it scored a system nobody runs. A policy that vetoes everything must
    therefore produce no trades at all.
    """

    spec = _spec()
    vetoing = AggressiveRiskPolicy(max_positions=0, require_take_profit=True)

    result = asyncio.run(_runner(spec, vetoing).run(_Provider("model-a"), ModelRun("model-a")))

    assert result.trade_count == 0


def test_a_run_produces_trades_and_a_curve() -> None:
    spec = _spec()
    provider = _Provider("model-a")

    run = ModelRun("model-a")
    result = asyncio.run(_runner(spec).run(provider, run))

    assert run.decisions_done == run.decisions_total == 24
    assert result.trade_count >= 1
    assert result.equity_curve
    # Nothing is left open, so the final equity is fully realised.
    assert result.final_equity == result.equity_curve[-1].equity


def test_marks_use_only_five_minute_bars_closed_by_the_decision() -> None:
    runner = _runner(_spec())
    when = WINDOW_START + timedelta(minutes=5)
    candles = runner._series["BTCUSDT"]["5m"]
    expected = next(candle.close for candle in candles if candle.timestamp == WINDOW_START)
    future = next(
        candle.close for candle in candles if candle.timestamp == WINDOW_START + timedelta(minutes=5)
    )

    mark = runner._marks(when)["BTCUSDT"]

    assert mark == expected
    assert mark != future


def test_entry_bar_protection_is_settled_before_the_next_decision() -> None:
    spec = _spec(end=WINDOW_START + timedelta(minutes=15))
    runner = _runner(spec)
    candles = runner._series["BTCUSDT"]["5m"]
    index = next(
        i for i, candle in enumerate(candles) if candle.timestamp == WINDOW_START + timedelta(minutes=5)
    )
    entry_bar = candles[index]
    candles[index] = Candle(
        timestamp=entry_bar.timestamp,
        open=entry_bar.open,
        high=entry_bar.open * Decimal("2"),
        low=entry_bar.low,
        close=entry_bar.close,
        volume=entry_bar.volume,
    )

    result = asyncio.run(runner.run(_Provider("model-a"), ModelRun("model-a")))

    assert result.trades
    assert result.trades[0].exit_reason == "take_profit"
    assert result.trades[0].exit_time == WINDOW_START + timedelta(minutes=5)


def test_higher_cadences_cannot_duplicate_funding_settlement() -> None:
    base_spec = _spec(end=WINDOW_START + timedelta(minutes=35))
    multi_spec = _spec(
        end=WINDOW_START + timedelta(minutes=35),
        cadences=("5m", "15m", "30m"),
    )
    base_runner = _runner(base_spec)
    multi_runner = _runner(multi_spec)
    for runner in (base_runner, multi_runner):
        by_interval = runner._series["BTCUSDT"]
        base = by_interval["5m"]
        base_index = next(
            i
            for i, candle in enumerate(base)
            if candle.timestamp == WINDOW_START + timedelta(minutes=5)
        )
        candle = base[base_index]
        base[base_index] = Candle(
            candle.timestamp,
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.volume,
            Decimal("0.001"),
        )
        # These exaggerated rates must be irrelevant: settlement is driven by
        # the finest series once, not once per decision cadence.
        for interval in ("15m", "30m"):
            source = by_interval[interval]
            source_index = next(i for i, item in enumerate(source) if item.timestamp >= WINDOW_START)
            item = source[source_index]
            source[source_index] = Candle(
                item.timestamp,
                item.open,
                item.high,
                item.low,
                item.close,
                item.volume,
                Decimal("1"),
            )

    base = asyncio.run(base_runner.run(_Provider("a"), ModelRun("a")))
    multi = asyncio.run(multi_runner.run(_Provider("b"), ModelRun("b")))

    assert base.total_funding > 0
    assert multi.total_funding == base.total_funding


def test_unreached_reduce_only_limit_is_pending_and_blocks_another_order() -> None:
    class RestingLimit(_Provider):
        def __init__(self, name: str) -> None:
            super().__init__(name)
            self.calls = 0

        async def generate_trade_intent(self, snapshot, portfolio):
            self.calls += 1
            if self.calls == 1:
                return await super().generate_trade_intent(snapshot, portfolio)
            intent = TradeIntent(
                symbol=snapshot.symbol,
                cadence=snapshot.cadence,
                action=TradeAction.CLOSE,
                confidence=0.8,
                leverage=1,
                risk_fraction="0",
                order_type="LIMIT",
                entry_price=snapshot.mark_price * Decimal("2"),
                rationale="resting exit fixture",
            )
            return ProviderResult(intent, self.name, "m", timedelta(0), "{}", {})

    decisions: list[BacktestDecision] = []

    async def capture(_run: ModelRun, decision: BacktestDecision | None) -> None:
        if decision is not None:
            decisions.append(decision)

    result = asyncio.run(
        _runner(_spec(end=WINDOW_START + timedelta(minutes=20))).run(
            RestingLimit("limit"), ModelRun("limit"), on_progress=capture
        )
    )

    assert result.trade_count == 1  # The still-open position is closed at run end.
    assert decisions[0].outcome == "traded"
    assert decisions[1].outcome == "pending"
    assert decisions[1].fill is not None and decisions[1].fill["status"] == "NEW"
    assert all(item.outcome == "rejected" for item in decisions[2:])


def test_decision_fill_price_is_the_slipped_execution_price() -> None:
    spec = _spec(
        end=WINDOW_START + timedelta(minutes=10),
        config=BacktestConfig(slippage_fraction=Decimal("0.001"), fee_rate=Decimal("0")),
    )
    runner = _runner(spec)
    decisions: list[BacktestDecision] = []

    async def capture(_run: ModelRun, decision: BacktestDecision | None) -> None:
        if decision is not None:
            decisions.append(decision)

    asyncio.run(runner.run(_Provider("market"), ModelRun("market"), on_progress=capture))
    fill_candle = next(
        candle
        for candle in runner._series["BTCUSDT"]["5m"]
        if candle.timestamp == WINDOW_START + timedelta(minutes=5)
    )

    assert decisions[0].fill is not None
    assert Decimal(decisions[0].fill["price"]) == fill_candle.open * Decimal("1.001")


def test_a_transient_failure_is_retried_inside_the_same_decision() -> None:

    class Flaky(_Provider):
        def __init__(self) -> None:
            super().__init__("flaky")
            self.calls = 0

        async def generate_trade_intent(self, snapshot, portfolio):
            self.calls += 1
            if self.calls % 2 == 0:
                raise RuntimeError("provider blew up")
            return await super().generate_trade_intent(snapshot, portfolio)

    run = ModelRun("flaky")
    provider = Flaky()
    asyncio.run(_runner(_spec()).run(provider, run))

    assert run.calls_failed == 0
    assert run.decisions_done == 24
    assert provider.calls == 47


def test_models_are_compared_over_the_identical_window() -> None:
    """Ranking only means anything if every model saw the same bars."""

    spec = _spec(providers=("model-a", "model-b"))
    providers = {"model-a": _Provider("model-a"), "model-b": _Provider("model-b")}

    runs = asyncio.run(
        compare(
            spec=spec,
            runner_for=lambda _: _runner(spec),
            provider_for=lambda name: providers[name],
        )
    )

    assert [run.provider for run in runs] == ["model-a", "model-b"]
    assert all(run.result is not None and run.error is None for run in runs)
    seen_a = [s.timestamp for s in providers["model-a"].snapshots]
    seen_b = [s.timestamp for s in providers["model-b"].snapshots]
    assert seen_a == seen_b


def test_one_provider_failing_stops_the_comparison() -> None:
    class Slow(_Provider):
        def __init__(self) -> None:
            super().__init__("model-a")
            self.cancelled = False

        async def generate_trade_intent(self, snapshot, portfolio):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    class Broken(_Provider):
        async def generate_trade_intent(self, snapshot, portfolio):
            raise RuntimeError("model is down")

    spec = _spec(providers=("model-a", "broken"))
    slow = Slow()
    providers = {"model-a": slow, "broken": Broken("broken")}

    runs = asyncio.run(
        compare(
            spec=spec,
            runner_for=lambda _: _runner(spec),
            provider_for=lambda name: providers[name],
        )
    )

    bad = next(run for run in runs if run.provider == "broken")
    assert slow.cancelled
    assert bad.result is not None and bad.result.trade_count == 0
    assert bad.calls_failed == 1
    assert bad.decisions_done == 1
    assert bad.provider_failed
    assert bad.last_successful_at is None
    assert bad.result.equity_curve[-1].timestamp == spec.start


def test_progress_is_reported_while_the_run_is_still_running() -> None:
    """The console polls a stored copy, so in-memory counters are invisible.

    compare() was never handed on_progress, and its signature took none of the
    run's state anyway, so nothing was written until the whole comparison
    returned. An hour-long backtest sat at 0% and then jumped to 100%.
    """

    spec = _spec()
    seen: list[tuple[int, int]] = []

    async def record(run: ModelRun, decision: BacktestDecision | None) -> None:
        seen.append((run.decisions_done, run.decisions_total))

    run = ModelRun("model-a")
    asyncio.run(_runner(spec).run(_Provider("model-a"), run, on_progress=record))

    # The denominator has to arrive before the first call, or there is nothing
    # to show a percentage against.
    assert seen[0] == (0, run.decisions_total)
    assert run.decisions_total > 0
    # One report per decision, plus the opening one.
    assert len(seen) == run.decisions_total + 1
    assert seen[-1] == (run.decisions_total, run.decisions_total)
    # And it climbs rather than jumping at the end.
    assert [done for done, _ in seen] == list(range(run.decisions_total + 1))


def test_progress_accumulates_tokens_and_requires_complete_pricing() -> None:
    run = ModelRun("model-a")
    prices = iter((0.01, None))
    for price in prices:
        run.record_usage(
            {
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "output_tokens": 20,
                "total_tokens": 120,
            },
            price,
            250,
        )

    assert run.total_tokens == 240
    assert run.cached_input_tokens == 80
    assert run.usage_calls == 2
    assert run.priced_calls == 1
    assert run.equivalent_cost_usd is None
    assert run.usage_dict()["average_duration_ms"] == 250


def test_remaining_time_uses_observed_decision_throughput() -> None:
    run = ModelRun("model-a", decisions_done=3, decisions_total=12)

    run.update_timing(9.0)

    assert run.elapsed_seconds == 9.0
    assert run.remaining_seconds == pytest.approx(27.0)


def test_progress_reports_unrealized_return_without_closing_the_position() -> None:
    spec = _spec()
    seen = []

    async def record(run: ModelRun, decision: BacktestDecision | None) -> None:
        if run.decisions_done == 3 and run.live_result is not None:
            seen.append(run.live_result)

    result = asyncio.run(
        _runner(spec).run(_Provider("model-a"), ModelRun("model-a"), on_progress=record)
    )

    assert seen
    live = seen[0]
    assert live.trade_count == 0
    assert live.win_rate == 0
    assert live.unrealized_pnl != 0
    assert live.total_return == (live.equity / spec.config.initial_equity) - 1
    # The live snapshot did not manufacture a close merely to calculate it.
    assert result.trade_count == 1


def test_compare_reports_each_model_while_it_works() -> None:
    """on_progress must reach the runner, not just fire once per finished model."""

    spec = _spec(providers=("model-a", "model-b"))
    providers = {"model-a": _Provider("model-a"), "model-b": _Provider("model-b")}
    mid_run: list[str] = []

    async def record(run: ModelRun, decision: BacktestDecision | None) -> None:
        if run.result is None and 0 < run.decisions_done < run.decisions_total:
            mid_run.append(run.provider)

    runs = asyncio.run(
        compare(
            spec=spec,
            runner_for=lambda _: _runner(spec),
            provider_for=lambda name: providers[name],
            on_progress=record,
        )
    )

    assert sorted(set(mid_run)) == ["model-a", "model-b"]
    # The last report for each model carries the result the earlier ones lacked.
    assert all(run.result is not None for run in runs)


def test_a_decision_records_why_nothing_traded() -> None:
    """Zero trades has three different causes and the totals show one number.

    A model that held, a model the risk policy vetoed, and a model whose calls
    failed all report trade_count 0. Only the per-decision outcome tells them
    apart.
    """

    spec = _spec()
    seen: list[BacktestDecision] = []

    async def record(run: ModelRun, decision: BacktestDecision | None) -> None:
        if decision is not None:
            seen.append(decision)

    class Flaky(_Provider):
        calls = 0

        async def generate_trade_intent(self, snapshot, portfolio):
            Flaky.calls += 1
            if Flaky.calls <= 3:
                raise RuntimeError("endpoint timed out after 45s")
            return await super().generate_trade_intent(snapshot, portfolio)

    run = ModelRun("model-a")
    result = asyncio.run(_runner(spec).run(Flaky("model-a"), run, on_progress=record))

    assert len(seen) == 1
    assert run.decisions_done == 1
    assert run.provider_failed
    assert seen[0].outcome == "call_failed"
    assert "3 attempts" in seen[0].detail
    assert "timed out" in seen[0].detail
    assert len(seen[0].attempt_started_at) == 3
    assert all(started.tzinfo == UTC for started in seen[0].attempt_started_at)
    assert seen[0].attempt_started_at == sorted(seen[0].attempt_started_at)
    # The failed one carries no action: there was no intent to record.
    assert seen[0].action is None
    assert {item.outcome for item in seen} <= {
        "traded", "rejected", "hold", "no_snapshot", "call_failed",
    }
    # Every decision is anchored in time and instrument.
    assert all(item.symbol == "BTCUSDT" and item.cadence == "5m" for item in seen)
    assert result.equity_curve[-1].timestamp == spec.start


def test_a_traded_decision_records_the_fill_it_got() -> None:
    """"It opened long" is not reviewable without the price and the stop."""

    spec = _spec()
    seen: list[BacktestDecision] = []

    async def record(run: ModelRun, decision: BacktestDecision | None) -> None:
        if decision is not None:
            seen.append(decision)

    asyncio.run(_runner(spec).run(_Provider("model-a"), ModelRun("model-a"), on_progress=record))

    traded = [item for item in seen if item.outcome == "traded"]
    assert traded, "the buyer never traded"
    fill = traded[0].fill
    assert fill is not None
    assert set(fill) == {
        "status",
        "price",
        "quantity",
        "side",
        "leverage",
        "stop_loss",
        "take_profit",
    }
    assert fill["status"] == "FILLED"
    assert Decimal(fill["price"]) > 0
    assert traded[0].rationale and traded[0].confidence is not None
