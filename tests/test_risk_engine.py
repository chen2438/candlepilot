from datetime import UTC, datetime, timedelta
from decimal import Decimal

from candlepilot.domain.models import (
    MarketSnapshot,
    OrderType,
    PortfolioState,
    PositionState,
    TradeAction,
    TradeIntent,
)
from candlepilot.risk.engine import AggressiveRiskPolicy, SymbolRules


RULES = SymbolRules(
    quantity_step=Decimal("0.001"),
    min_quantity=Decimal("0.001"),
    min_notional=Decimal("5"),
    tick_size=Decimal("0.01"),
)


def _snapshot(*, age_seconds: int = 0) -> MarketSnapshot:
    return MarketSnapshot(
        symbol="BTCUSDT",
        cadence="5m",
        timestamp=datetime.now(UTC) - timedelta(seconds=age_seconds),
        mark_price="100",
        bid="99.9",
        ask="100.1",
        quote_volume_24h="1000000",
    )


def _intent(action: TradeAction = TradeAction.OPEN_LONG) -> TradeIntent:
    return TradeIntent(
        symbol="BTCUSDT",
        cadence="5m",
        action=action,
        confidence=0.8,
        leverage=5,
        risk_fraction="0.02",
        stop_loss="98" if action != TradeAction.OPEN_SHORT else "102",
        take_profit="104" if action != TradeAction.OPEN_SHORT else "96",
        rationale="test signal",
    )


def _portfolio(**changes) -> PortfolioState:
    values = {
        "equity": "10000",
        "available_balance": "8000",
        "daily_pnl": "0",
        "open_positions": 0,
        "margin_used": "0",
    }
    values.update(changes)
    return PortfolioState(**values)


def _position(side: str, quantity: str = "1", **changes) -> dict[str, PositionState]:
    return {
        "BTCUSDT": PositionState(
            side=side, quantity=quantity, entry_price="100", **changes
        )
    }


def test_single_symbol_initial_margin_is_capped_at_ten_percent_of_equity() -> None:
    result = AggressiveRiskPolicy().evaluate(_intent(), _snapshot(), _portfolio(), RULES)
    assert result.decision.accepted
    assert result.order is not None
    assert result.order.quantity == Decimal("50.000")
    assert result.order.quantity * Decimal("100") / 5 == Decimal("1000")


def test_portfolio_initial_margin_is_capped_at_eighty_percent_of_equity() -> None:
    result = AggressiveRiskPolicy(max_symbol_margin_fraction=Decimal("1")).evaluate(
        _intent(),
        _snapshot(),
        _portfolio(margin_used="7000", available_balance="3000"),
        RULES,
    )

    assert result.decision.accepted
    assert result.order is not None
    assert result.order.quantity == Decimal("50.000")
    assert Decimal("7000") + result.order.quantity * Decimal("100") / 5 == Decimal(
        "8000"
    )


def test_sizes_position_from_stop_distance_and_rounds_down() -> None:
    result = AggressiveRiskPolicy(max_symbol_margin_fraction=Decimal("1")).evaluate(
        _intent(), _snapshot(), _portfolio(), RULES
    )
    assert result.decision.accepted
    assert result.order is not None
    assert result.order.quantity == Decimal("95.238")
    assert result.order.stop_price == Decimal("98")
    assert result.order.take_profit_price == Decimal("104")


def test_testnet_policy_requires_take_profit_on_open() -> None:
    intent = _intent().model_copy(update={"take_profit": None})
    policy = AggressiveRiskPolicy(require_take_profit=True)
    result = policy.evaluate(intent, _snapshot(), _portfolio(), RULES)
    assert not result.decision.accepted
    assert "take profit" in result.decision.reason
    # The same intent is accepted when a take profit is not mandated.
    assert AggressiveRiskPolicy().evaluate(intent, _snapshot(), _portfolio(), RULES).decision.accepted


def test_rejects_an_entry_when_the_symbol_already_has_a_pending_order() -> None:
    result = AggressiveRiskPolicy().evaluate(
        _intent(),
        _snapshot(),
        _portfolio(pending_entry_symbols=("BTCUSDT",)),
        RULES,
    )

    assert not result.decision.accepted
    assert result.order is None
    assert "pending entry order" in result.decision.reason


def test_another_symbols_pending_entry_does_not_block_a_new_position() -> None:
    result = AggressiveRiskPolicy().evaluate(
        _intent(),
        _snapshot(),
        _portfolio(pending_entry_symbols=("ETHUSDT",)),
        RULES,
    )

    assert result.decision.accepted
    assert result.order is not None


def test_reduce_rejects_when_half_the_position_rounds_below_minimum() -> None:
    portfolio = _portfolio(
        open_positions=1,
        positions=_position("LONG", "0.001"),
    )

    result = AggressiveRiskPolicy().evaluate(
        _intent(TradeAction.REDUCE), _snapshot(), portfolio, RULES
    )

    assert not result.decision.accepted
    assert result.order is None
    assert "below the exchange minimum" in result.decision.reason


def test_close_rejects_a_dust_position_below_exchange_minimum() -> None:
    portfolio = _portfolio(
        open_positions=1,
        positions=_position("LONG", "0.0009"),
    )

    result = AggressiveRiskPolicy().evaluate(
        _intent(TradeAction.CLOSE), _snapshot(), portfolio, RULES
    )

    assert not result.decision.accepted
    assert result.order is None
    assert "below the exchange minimum" in result.decision.reason


def test_add_subtracts_existing_position_risk_from_the_hard_limit() -> None:
    intent = _intent(TradeAction.ADD)
    portfolio = _portfolio(
        open_positions=1,
        margin_used="1904.76",
        positions=_position(
            "LONG", "95.239", stop_loss="98", take_profit="104"
        ),
    )

    result = AggressiveRiskPolicy(max_symbol_margin_fraction=Decimal("1")).evaluate(
        intent, _snapshot(), portfolio, RULES
    )

    assert not result.decision.accepted
    assert result.order is None
    assert "exhausts the symbol risk limit" in result.decision.reason


def test_add_uses_only_the_remaining_combined_risk_budget() -> None:
    intent = _intent(TradeAction.ADD).model_copy(update={"risk_fraction": Decimal("0.02")})
    portfolio = _portfolio(
        open_positions=1,
        margin_used="500",
        positions=_position("LONG", "25", stop_loss="98", take_profit="104"),
    )

    result = AggressiveRiskPolicy(max_symbol_margin_fraction=Decimal("1")).evaluate(
        intent, _snapshot(), portfolio, RULES
    )

    assert result.decision.accepted
    assert result.order is not None
    existing_risk = Decimal("25") * (Decimal("2") + Decimal("0.1"))
    new_risk = result.order.quantity * (Decimal("2") + Decimal("0.1"))
    assert existing_risk + new_risk <= Decimal("200")
    assert existing_risk + new_risk > Decimal("199.99")


def test_add_uses_only_remaining_single_symbol_margin_capacity() -> None:
    portfolio = _portfolio(
        open_positions=1,
        margin_used="900",
        positions=_position(
            "LONG",
            "45",
            leverage=5,
            initial_margin="900",
            stop_loss="98",
            take_profit="104",
        ),
    )

    result = AggressiveRiskPolicy().evaluate(
        _intent(TradeAction.ADD), _snapshot(), portfolio, RULES
    )

    assert result.decision.accepted
    assert result.order is not None
    assert result.order.quantity == Decimal("5.000")
    assert Decimal("900") + result.order.quantity * Decimal("100") / 5 == Decimal(
        "1000"
    )


def test_rejects_take_profit_on_wrong_side_of_entry() -> None:
    long_bad = _intent().model_copy(update={"take_profit": Decimal("99")})  # below entry
    long_result = AggressiveRiskPolicy().evaluate(long_bad, _snapshot(), _portfolio(), RULES)
    assert not long_result.decision.accepted
    assert "long take profit must be above entry" in long_result.decision.reason

    short_bad = _intent(TradeAction.OPEN_SHORT).model_copy(update={"take_profit": Decimal("101")})
    short_result = AggressiveRiskPolicy().evaluate(short_bad, _snapshot(), _portfolio(), RULES)
    assert not short_result.decision.accepted
    assert "short take profit must be below entry" in short_result.decision.reason


def test_rejects_stale_market_data() -> None:
    result = AggressiveRiskPolicy().evaluate(
        _intent(), _snapshot(age_seconds=76), _portfolio(), RULES
    )
    assert not result.decision.accepted
    assert "stale" in result.decision.reason


def test_stale_hold_is_accepted_without_an_order() -> None:
    result = AggressiveRiskPolicy().evaluate(
        TradeIntent.hold("BTCUSDT", "5m", "no setup"),
        _snapshot(age_seconds=300),
        _portfolio(),
        RULES,
    )

    assert result.decision.accepted
    assert result.order is None


def test_market_order_uses_latest_mark_instead_of_suggested_entry() -> None:
    intent = _intent().model_copy(update={"entry_price": Decimal("90")})

    result = AggressiveRiskPolicy().evaluate(intent, _snapshot(), _portfolio(), RULES)

    assert result.decision.accepted
    assert result.order is not None and result.order.price is None


def test_rejects_crossed_protection_after_refresh() -> None:
    crossed_take_profit = _intent().model_copy(
        update={"order_type": OrderType.LIMIT, "entry_price": Decimal("99")}
    )
    moved = _snapshot().model_copy(
        update={
            "mark_price": Decimal("105"),
            "bid": Decimal("104.9"),
            "ask": Decimal("105.1"),
        }
    )
    crossed = AggressiveRiskPolicy().evaluate(
        crossed_take_profit, moved, _portfolio(), RULES
    )
    assert not crossed.decision.accepted
    assert "crossed the long take profit" in crossed.decision.reason



def test_allows_and_marks_immediately_marketable_limit_after_refresh() -> None:
    marketable = _intent().model_copy(
        update={"order_type": OrderType.LIMIT, "entry_price": Decimal("101")}
    )
    result = AggressiveRiskPolicy().evaluate(
        marketable, _snapshot(), _portfolio(), RULES
    )

    assert result.decision.accepted and result.order is not None
    assert "immediately marketable after refresh" in result.decision.reason
    assert result.order.price == Decimal("101")


def test_rejects_a_resting_opening_limit_that_cannot_be_atomically_protected() -> None:
    resting = _intent().model_copy(
        update={"order_type": OrderType.LIMIT, "entry_price": Decimal("99")}
    )

    result = AggressiveRiskPolicy().evaluate(resting, _snapshot(), _portfolio(), RULES)

    assert not result.decision.accepted
    assert result.order is None
    assert "cannot be attached atomically" in result.decision.reason


def test_marketable_short_limit_uses_fresh_bid_for_margin_sizing() -> None:
    intent = _intent(TradeAction.OPEN_SHORT).model_copy(
        update={"order_type": OrderType.LIMIT, "entry_price": Decimal("99")}
    )

    result = AggressiveRiskPolicy().evaluate(
        intent,
        _snapshot(),
        _portfolio(available_balance="10"),
        RULES,
    )

    assert result.decision.accepted and result.order is not None
    assert result.order.quantity == Decimal("0.500")
    assert "immediately marketable after refresh" in result.decision.reason


def test_snapshot_age_must_be_positive() -> None:
    import pytest

    with pytest.raises(ValueError, match="must be positive"):
        AggressiveRiskPolicy(max_snapshot_age_seconds=0)


def test_daily_loss_circuit_breaker() -> None:
    result = AggressiveRiskPolicy().evaluate(
        _intent(), _snapshot(), _portfolio(equity="9200", daily_pnl="-800"), RULES
    )
    assert not result.decision.accepted
    assert "circuit breaker" in result.decision.reason


def test_opposing_position_must_close_first() -> None:
    result = AggressiveRiskPolicy().evaluate(
        _intent(TradeAction.OPEN_LONG),
        _snapshot(),
        _portfolio(open_positions=1, positions=_position("SHORT")),
        RULES,
    )
    assert not result.decision.accepted
    assert "closed" in result.decision.reason


def test_same_side_open_requires_explicit_add() -> None:
    result = AggressiveRiskPolicy().evaluate(
        _intent(TradeAction.OPEN_LONG),
        _snapshot(),
        _portfolio(open_positions=1, positions=_position("LONG")),
        RULES,
    )
    assert not result.decision.accepted
    assert "explicit ADD" in result.decision.reason


def test_add_uses_existing_position_direction() -> None:
    result = AggressiveRiskPolicy().evaluate(
        _intent(TradeAction.ADD),
        _snapshot(),
        _portfolio(open_positions=1, positions=_position("LONG")),
        RULES,
    )
    assert result.decision.accepted
    assert result.order is not None and result.order.side == "BUY"


def test_close_is_always_reduce_only() -> None:
    result = AggressiveRiskPolicy().evaluate(
        _intent(TradeAction.CLOSE),
        _snapshot(),
        _portfolio(open_positions=1, positions=_position("LONG", "1.2345")),
        RULES,
    )
    assert result.decision.accepted
    assert result.order is not None and result.order.reduce_only
    assert result.order.side == "SELL"
    assert result.order.quantity == Decimal("1.234")


def test_protective_prices_snap_to_the_tick_grid_away_from_entry() -> None:
    """PRICE_FILTER rejects off-grid prices, and the model is told no tick size.

    Rounding has to move each level away from the entry: pulling a stop toward
    entry could snap it through the price it was just validated against, and a
    rejected bracket leaves a filled entry unprotected.
    """

    rules = SymbolRules(
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
        tick_size=Decimal("0.5"),
    )
    long_intent = _intent().model_copy(
        update={"stop_loss": Decimal("98.4"), "take_profit": Decimal("104.2")}
    )
    long_result = AggressiveRiskPolicy().evaluate(long_intent, _snapshot(), _portfolio(), rules)
    assert long_result.decision.accepted and long_result.order is not None
    assert long_result.order.stop_price == Decimal("98.0")
    assert long_result.order.take_profit_price == Decimal("104.5")

    short_intent = _intent(TradeAction.OPEN_SHORT).model_copy(
        update={"stop_loss": Decimal("101.6"), "take_profit": Decimal("95.8")}
    )
    short_result = AggressiveRiskPolicy().evaluate(short_intent, _snapshot(), _portfolio(), rules)
    assert short_result.decision.accepted and short_result.order is not None
    assert short_result.order.stop_price == Decimal("102.0")
    assert short_result.order.take_profit_price == Decimal("95.5")


def test_limit_entry_snaps_toward_our_own_side_of_the_book() -> None:
    rules = SymbolRules(
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
        tick_size=Decimal("0.5"),
    )
    intent = _intent().model_copy(
        update={"order_type": OrderType.LIMIT, "entry_price": Decimal("100.7")}
    )

    result = AggressiveRiskPolicy().evaluate(intent, _snapshot(), _portfolio(), rules)

    assert result.decision.accepted and result.order is not None
    # A long never bids up to reach the grid.
    assert result.order.price == Decimal("100.5")


def test_stop_that_snaps_to_zero_is_rejected_rather_than_sent() -> None:
    """A tick coarser than the stop distance rounds the stop off the bottom.

    Zero is not a price the exchange will take, and it is certainly not the
    invalidation the model asked for, so the trade is refused rather than sent
    with a nonsense bracket.
    """

    coarse = SymbolRules(
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
        tick_size=Decimal("200"),
    )
    result = AggressiveRiskPolicy().evaluate(_intent(), _snapshot(), _portfolio(), coarse)

    assert not result.decision.accepted
    assert "rounds to zero" in result.decision.reason
