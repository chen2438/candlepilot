from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from candlepilot.domain.models import MarketSnapshot, TradeAction, TradeIntent


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
