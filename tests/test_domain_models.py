from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from candlepilot.domain.models import (
    RATIONALE_MAX_LENGTH,
    MarketSnapshot,
    TradeAction,
    TradeIntent,
)


def test_open_intent_requires_stop_loss() -> None:
    with pytest.raises(ValidationError, match="require stop_loss"):
        TradeIntent(
            symbol="BTCUSDT",
            cadence="5m",
            action=TradeAction.OPEN_LONG,
            confidence=0.8,
            leverage=5,
            risk_fraction="0.01",
            rationale="breakout",
        )


def test_hold_factory_is_safe() -> None:
    intent = TradeIntent.hold("ETHUSDT", "1m", "provider unavailable")
    assert intent.action == TradeAction.HOLD
    assert intent.risk_fraction == Decimal("0")


def test_trade_intent_allows_rationale_up_to_one_thousand_characters() -> None:
    intent = TradeIntent.hold("ETHUSDT", "5m", "x" * RATIONALE_MAX_LENGTH)
    assert len(intent.rationale) == 1_000


def test_hold_factory_bounds_oversized_error_reason() -> None:
    intent = TradeIntent.hold("ETHUSDT", "5m", "x" * (RATIONALE_MAX_LENGTH + 1))
    assert len(intent.rationale) == RATIONALE_MAX_LENGTH


def test_market_snapshot_rejects_crossed_quote() -> None:
    with pytest.raises(ValidationError, match="ask cannot be below bid"):
        MarketSnapshot(
            symbol="BTCUSDT",
            cadence="1m",
            timestamp=datetime.now(UTC),
            mark_price="100",
            bid="101",
            ask="100",
            quote_volume_24h="1000000",
        )
