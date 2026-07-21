"""Time a few real decisions before committing to a backtest.

A backtest is hundreds of model calls behind a fixed timeout, and the timeout
that suits one endpoint strands another. Guessing it cost a run 5 of its 12
decisions to 45s timeouts and still reported a tidy 0% return, so the number is
measured against the endpoint that will serve the run, on the payload it will
be sent.
"""

from __future__ import annotations

import math
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from candlepilot.backtest.runner import BacktestSpec, decision_times
from candlepilot.backtest.snapshots import HistoricalSnapshotBuilder
from candlepilot.domain.models import MarketSnapshot, PortfolioState
from candlepilot.providers.base import DecisionProvider

#: Calls per provider. Five samples are still cheap next to a full backtest,
#: while making one unusually fast or slow answer less likely to describe the
#: endpoint by itself.
PROBE_DECISIONS = 5

#: The ceiling a probe call is given, in seconds.
#:
#: Deliberately far above any sane timeout: probing at the configured one would
#: only reproduce the timeouts it is meant to diagnose, and report "45s" for an
#: endpoint that actually needs 70. The point is to observe how long the model
#: really takes, so the ceiling only exists to stop a hung call forever.
PROBE_CEILING_SECONDS = 180.0

#: Multiplier applied to the slowest observed call to suggest a timeout.
#:
#: Five samples cannot describe a tail, so the suggestion is a starting point
#: with room above the worst seen, not a computed safe bound.
TIMEOUT_HEADROOM = 1.5

MIN_SUGGESTED_TIMEOUT = 10
MAX_SUGGESTED_TIMEOUT = 600


@dataclass(frozen=True, slots=True)
class ProbeCall:
    """One timed decision."""

    seconds: float
    ok: bool
    error: str | None = None


@dataclass
class ProviderProbe:
    """What one endpoint did with `PROBE_DECISIONS` real decisions.

    Filled in as the calls land rather than returned at the end. A probe waits
    on the very thing it is measuring, and at `PROBE_CEILING_SECONDS` a silent
    one can run for fifteen minutes before it admits to anything -- long enough to
    look identical to a hang.
    """

    provider: str
    calls: list[ProbeCall] = field(default_factory=list)
    error: str | None = None
    #: `time.monotonic()` when the in-flight call started, if one is in flight.
    started_at: float | None = None
    done: bool = False

    @property
    def in_flight_seconds(self) -> float | None:
        """How long the current call has been waiting, for a live readout."""

        if self.started_at is None:
            return None
        return time.monotonic() - self.started_at

    @property
    def slowest_ok_seconds(self) -> float | None:
        durations = [call.seconds for call in self.calls if call.ok]
        return max(durations) if durations else None

    @property
    def average_ok_seconds(self) -> float | None:
        """Mean latency of this endpoint's successful probe decisions."""

        durations = [call.seconds for call in self.calls if call.ok]
        return sum(durations) / len(durations) if durations else None

    @property
    def failures(self) -> int:
        return sum(1 for call in self.calls if not call.ok)

    @property
    def suggested_timeout_seconds(self) -> int | None:
        """Headroom over the slowest success, or nothing to suggest.

        With every call failing there is no latency to reason from: the answer
        is not a bigger number, it is that this endpoint cannot serve the run.
        """

        slowest = self.slowest_ok_seconds
        if slowest is None:
            return None
        suggested = math.ceil(slowest * TIMEOUT_HEADROOM)
        return max(MIN_SUGGESTED_TIMEOUT, min(MAX_SUGGESTED_TIMEOUT, suggested))


def slowest_probe(
    probes: Mapping[str, ProviderProbe], providers: Sequence[str]
) -> tuple[str, float]:
    """The participating provider with the slowest mean decision latency."""

    provider = max(
        providers,
        key=lambda name: probes[name].average_ok_seconds or 0.0,
    )
    seconds = probes[provider].average_ok_seconds
    if seconds is None:
        raise ValueError(f"{provider} has no successful probe call")
    return provider, seconds


def probe_instants(spec: BacktestSpec, count: int = PROBE_DECISIONS) -> list[datetime]:
    """Five real decision payloads from the start of the window.

    The probe sends what the run will send, so it reads its snapshots off the
    same schedule rather than inventing an instant of its own. A short, slow-
    cadence window can contain fewer than five distinct decisions; repeat its
    available payloads so the latency sample still has the promised size.
    """

    times = sorted(decision_times(spec, spec.cadences[0]))
    if not times:
        return []
    return [times[index % len(times)] for index in range(count)]


async def probe_provider(
    provider: DecisionProvider,
    *,
    spec: BacktestSpec,
    builder: HistoricalSnapshotBuilder,
    symbol: str,
    portfolio: PortfolioState,
    ceiling: float = PROBE_CEILING_SECONDS,
    into: ProviderProbe | None = None,
    snapshots: Sequence[MarketSnapshot] | None = None,
) -> ProviderProbe:
    """Time `PROBE_DECISIONS` real calls against `provider`.

    The provider's own timeout is raised to `ceiling` for the duration and put
    back afterwards, so a probe can outlive the setting it exists to question.

    Pass `into` to have each call appended to an object the caller already
    published: the point of a probe is watching an endpoint answer, and a
    reader that only sees the verdict cannot tell a slow call from a dead one.
    """

    result = into if into is not None else ProviderProbe(provider=provider.name)
    instants = probe_instants(spec)
    replay_inputs = list(snapshots or ())
    if not replay_inputs and not instants:
        result.error = "the window holds no decision instants to probe"
        result.done = True
        return result

    previous = provider.timeout
    provider.timeout = ceiling
    try:
        for index in range(PROBE_DECISIONS):
            if replay_inputs:
                snapshot = replay_inputs[index % len(replay_inputs)]
            else:
                when = instants[index]
                try:
                    snapshot = builder.build(symbol, spec.cadences[0], when)
                except ValueError as exc:
                    result.error = f"no snapshot at {when.isoformat()}: {exc}"[:200]
                    return result
            started = time.monotonic()
            result.started_at = started
            try:
                await provider.generate_trade_intent(snapshot, portfolio)
            except Exception as exc:  # noqa: BLE001 - a failed probe is a result
                result.calls.append(
                    ProbeCall(
                        seconds=time.monotonic() - started,
                        ok=False,
                        error=str(exc)[:200],
                    )
                )
                continue
            finally:
                result.started_at = None
            result.calls.append(ProbeCall(seconds=time.monotonic() - started, ok=True))
    finally:
        provider.timeout = previous
        result.started_at = None
        result.done = True
    return result
