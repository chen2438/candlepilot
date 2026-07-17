import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from candlepilot.backtest.engine import BacktestConfig, Candle
from candlepilot.backtest.probe import (
    PROBE_CEILING_SECONDS,
    PROBE_DECISIONS,
    ProbeCall,
    ProviderProbe,
    probe_instants,
    probe_provider,
)
from candlepilot.backtest.runner import BacktestSpec
from candlepilot.backtest.snapshots import HistoricalSnapshotBuilder
from candlepilot.domain.models import PortfolioState, ProviderHealth, TradeIntent
from candlepilot.providers.base import LLMProvider, ProviderResult

START = datetime(2026, 6, 1, tzinfo=UTC)


def _candles(interval_minutes: int, count: int) -> list[Candle]:
    return [
        Candle(
            timestamp=START - timedelta(minutes=interval_minutes * (count - index)),
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=Decimal("10"),
        )
        for index in range(count)
    ]


def _series() -> dict[str, list[Candle]]:
    return {
        "5m": _candles(5, 400),
        "15m": _candles(15, 200),
        "30m": _candles(30, 200),
        "1d": _candles(60 * 24, 40),
    }


def _spec(**overrides) -> BacktestSpec:
    return BacktestSpec(
        symbols=("BTCUSDT",),
        cadences=("5m",),
        start=START,
        end=START + timedelta(hours=1),
        providers=("model-a",),
        config=BacktestConfig(),
        **overrides,
    )


class _Provider(LLMProvider):
    def __init__(self, name: str = "model-a", *, fail: int = 0) -> None:
        self.name = name
        self._fail = fail
        self.calls = 0
        self.seen_timeouts: list[float] = []

    async def health_check(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio) -> ProviderResult:
        self.calls += 1
        self.seen_timeouts.append(self.timeout)
        if self.calls <= self._fail:
            raise RuntimeError(f"endpoint timed out after {self.timeout:g}s")
        intent = TradeIntent.hold(snapshot.symbol, snapshot.cadence, "probe")
        return ProviderResult(intent, self.name, None, timedelta(0), "{}", {})


def _run(provider: LLMProvider, spec: BacktestSpec | None = None) -> ProviderProbe:
    spec = spec or _spec()
    return asyncio.run(
        probe_provider(
            provider,
            spec=spec,
            builder=HistoricalSnapshotBuilder(_series()),
            symbol="BTCUSDT",
            portfolio=PortfolioState(equity=Decimal("10000"), available_balance=Decimal("10000")),
        )
    )


def test_the_probe_sends_the_payload_the_run_will_send() -> None:
    """Timing a different prompt would measure a different thing."""

    spec = _spec()
    assert probe_instants(spec) == sorted(probe_instants(spec))
    assert len(probe_instants(spec)) == PROBE_DECISIONS


def test_the_probe_outlives_the_timeout_it_exists_to_question() -> None:
    """Probing at the configured timeout would only reproduce the timeouts.

    An endpoint that needs 70s reports "timed out after 45s" at the default and
    teaches nothing; the probe has to be allowed to watch it finish.
    """

    provider = _Provider()
    provider.timeout = 45

    result = _run(provider)

    assert provider.seen_timeouts == [PROBE_CEILING_SECONDS] * PROBE_DECISIONS
    # ...and the provider is handed back exactly as it was found.
    assert provider.timeout == 45
    assert len(result.calls) == PROBE_DECISIONS
    assert all(call.ok for call in result.calls)


def test_the_ceiling_is_restored_even_when_a_call_explodes() -> None:
    provider = _Provider(fail=PROBE_DECISIONS)
    provider.timeout = 45

    result = _run(provider)

    assert provider.timeout == 45
    assert result.failures == PROBE_DECISIONS


def test_a_suggestion_leaves_room_over_the_slowest_call() -> None:
    probe = ProviderProbe(
        provider="model-a",
        calls=[
            ProbeCall(seconds=30.0, ok=True),
            ProbeCall(seconds=40.0, ok=True),
            ProbeCall(seconds=20.0, ok=True),
        ],
    )

    assert probe.slowest_ok_seconds == 40.0
    # 40 * 1.5: three samples cannot describe a tail, so the suggestion is a
    # starting point above the worst seen rather than a computed bound.
    assert probe.suggested_timeout_seconds == 60


def test_an_endpoint_that_never_answers_gets_no_suggestion() -> None:
    """The answer is not a bigger number: it is that this endpoint cannot serve."""

    probe = ProviderProbe(
        provider="model-a",
        calls=[ProbeCall(seconds=180.0, ok=False, error="timed out") for _ in range(3)],
    )

    assert probe.slowest_ok_seconds is None
    assert probe.suggested_timeout_seconds is None
    assert probe.failures == 3


def test_a_failed_call_is_a_result_not_the_end_of_the_probe() -> None:
    """Two good samples and one failure still says more than no samples."""

    result = _run(_Provider(fail=1))

    assert len(result.calls) == PROBE_DECISIONS
    assert result.failures == 1
    assert result.suggested_timeout_seconds is not None
