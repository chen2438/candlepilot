"""Shared decision-level Provider retry policy.

Live decisions and backtests must agree on what a failed decision means. A
single route pass is one attempt; transient failures are retried inside the
same decision instead of waiting for the next candle boundary.
"""

from __future__ import annotations


DECISION_PROVIDER_MAX_ATTEMPTS = 3
DECISION_PROVIDER_RETRY_DELAYS = (5.0, 15.0)


def validate_retry_delays(delays: tuple[float, ...]) -> tuple[float, ...]:
    """Require one non-negative delay between every pair of attempts."""

    expected = DECISION_PROVIDER_MAX_ATTEMPTS - 1
    if len(delays) != expected:
        raise ValueError(f"provider retry delays must contain exactly {expected} values")
    if any(delay < 0 for delay in delays):
        raise ValueError("provider retry delays cannot be negative")
    return delays
