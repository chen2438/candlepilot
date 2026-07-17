import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from candlepilot.backtest.engine import BacktestConfig, Candle
from candlepilot.backtest.runner import (
    MAX_FAILURE_RATE,
    BacktestDecision,
    BacktestSpec,
    BacktestRunner,
    ModelRun,
    compare,
    decision_times,
    estimate,
    unreliable_models,
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


def test_estimate_adds_a_cadence_rather_than_multiplying_the_window() -> None:
    one = estimate(_spec(cadences=("5m",)), seconds_per_call=1)
    three = estimate(_spec(cadences=("5m", "15m", "30m")), seconds_per_call=1)

    # Two hours: 24 five-minute bars, 8 fifteens, 4 thirties.
    assert one.decisions_per_model == 24
    assert three.decisions_per_model == 36


def test_specs_that_cannot_finish_are_refused() -> None:
    with pytest.raises(ValueError, match="cannot exceed 3 days"):
        validate(_spec(end=WINDOW_START + timedelta(days=4)))
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


def test_a_failing_call_does_not_end_the_run() -> None:
    """One bad provider call must cost one decision, not the whole comparison."""

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
    asyncio.run(_runner(_spec()).run(Flaky(), run))

    assert run.calls_failed == 12
    assert run.decisions_done == 24


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


def test_one_model_failing_still_reports_the_others() -> None:
    class Broken(_Provider):
        async def generate_trade_intent(self, snapshot, portfolio):
            raise RuntimeError("model is down")

    spec = _spec(providers=("model-a", "broken"))
    providers = {"model-a": _Provider("model-a"), "broken": Broken("broken")}

    runs = asyncio.run(
        compare(
            spec=spec,
            runner_for=lambda _: _runner(spec),
            provider_for=lambda name: providers[name],
        )
    )

    good = next(run for run in runs if run.provider == "model-a")
    bad = next(run for run in runs if run.provider == "broken")
    assert good.result is not None
    # Every call failed, so the run completes with nothing traded rather than
    # taking the comparison down with it.
    assert bad.result is not None and bad.result.trade_count == 0
    assert bad.calls_failed == 24


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


def test_a_run_that_lost_too_many_decisions_is_not_reliable() -> None:
    """A failed call is not a HOLD -- it is a bar the model never saw.

    Reporting such a run as completed, next to a model that lost none, invites
    comparing two different windows as though they were one.
    """

    clean = ModelRun("model-a", decisions_done=100, calls_failed=0)
    edge = ModelRun("model-b", decisions_done=100, calls_failed=10)
    degraded = ModelRun("model-c", decisions_done=100, calls_failed=11)

    assert clean.reliable
    # Exactly at the limit still counts, so the constant reads as "at most".
    assert edge.failure_rate == MAX_FAILURE_RATE
    assert edge.reliable
    assert not degraded.reliable


def test_only_models_that_produced_a_result_are_judged() -> None:
    """A model that raised has an error already; it is not 'unreliable' too."""

    crashed = ModelRun("model-a", decisions_done=10, calls_failed=10, error="died")
    assert unreliable_models([crashed]) == []


def test_one_degraded_model_poisons_the_comparison() -> None:
    spec = _spec(providers=("model-a", "model-b"))

    class Broken(_Provider):
        async def generate_trade_intent(self, snapshot, portfolio):
            raise RuntimeError("timed out")

    providers = {"model-a": _Provider("model-a"), "model-b": Broken("model-b")}
    runs = asyncio.run(
        compare(
            spec=spec,
            runner_for=lambda _: _runner(spec),
            provider_for=lambda name: providers[name],
        )
    )

    degraded = unreliable_models(runs)
    assert [run.provider for run in degraded] == ["model-b"]
    # The clean model still reports: the run is flagged, not discarded.
    assert next(run for run in runs if run.provider == "model-a").result is not None


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
            if Flaky.calls == 1:
                raise RuntimeError("endpoint timed out after 45s")
            return await super().generate_trade_intent(snapshot, portfolio)

    run = ModelRun("model-a")
    asyncio.run(_runner(spec).run(Flaky("model-a"), run, on_progress=record))

    # One record per decision, no matter how the decision ended.
    assert len(seen) == run.decisions_total
    assert seen[0].outcome == "call_failed"
    assert "timed out" in seen[0].detail
    # The failed one carries no action: there was no intent to record.
    assert seen[0].action is None
    assert {item.outcome for item in seen} <= {
        "traded", "rejected", "hold", "no_snapshot", "call_failed",
    }
    # Every decision is anchored in time and instrument.
    assert all(item.symbol == "BTCUSDT" and item.cadence == "5m" for item in seen)


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
    assert set(fill) == {"price", "quantity", "side", "leverage", "stop_loss", "take_profit"}
    assert Decimal(fill["price"]) > 0
    assert traded[0].rationale and traded[0].confidence is not None
