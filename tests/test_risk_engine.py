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


def _structured_snapshot() -> MarketSnapshot:
    return _snapshot().model_copy(
        update={
            "features": {
                "5m_atr_14": 2.0,
                "5m_ema20_distance_atr": 0.5,
                "5m_ema_spread": 0.01,
                "15m_ema_spread": 0.02,
                "30m_ema_spread": -0.01,
                "5m_prior_range_low_20": 98.0,
            }
        }
    )


def _structured_intent() -> TradeIntent:
    return _intent().model_copy(
        update={
            "decision_framework": "structure-v1",
            "setup_type": "TREND_PULLBACK",
            "anchor_timeframe": "5m",
            "anchor_price": Decimal("100"),
            "trigger_type": "MARKET_CONFIRMED",
            "trigger_price": Decimal("99.5"),
            "invalidation_type": "RANGE",
            "invalidation_level": Decimal("98"),
            "target_type": "RANGE",
        }
    )


def _breakout_intent() -> TradeIntent:
    return _structured_intent().model_copy(
        update={
            "setup_type": "TREND_BREAKOUT",
            "trigger_type": "BREAKOUT",
            "trigger_price": Decimal("99.5"),
        }
    )


def _intent(action: TradeAction = TradeAction.OPEN_LONG) -> TradeIntent:
    return TradeIntent(
        symbol="BTCUSDT",
        cadence="5m",
        action=action,
        confidence=0.8,
        leverage=5,
        risk_fraction="0.01",
        stop_loss="98" if action != TradeAction.OPEN_SHORT else "102",
        take_profit="104" if action != TradeAction.OPEN_SHORT else "96",
        rationale="test signal",
    )


def _portfolio(**changes) -> PortfolioState:
    values = {
        "equity": "10000",
        "available_balance": "8000",
        "pnl_24h": "0",
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


def test_structure_gate_shadow_records_failure_without_blocking_the_order() -> None:
    result = AggressiveRiskPolicy(
        max_symbol_margin_fraction=Decimal("1"),
        structure_gate_mode="shadow",
    ).evaluate(_intent(), _snapshot(), _portfolio(), RULES)

    assert result.decision.accepted
    assert result.decision.structure_assessment is not None
    assert not result.decision.structure_assessment.passed
    assert result.decision.structure_assessment.checks[0].key == "metadata"


def test_structure_gate_enforce_rejects_missing_plan_metadata() -> None:
    result = AggressiveRiskPolicy(structure_gate_mode="enforce").evaluate(
        _intent(), _snapshot(), _portfolio(), RULES
    )

    assert not result.decision.accepted
    assert result.order is None
    assert result.decision.reason == "structure entry gate failed: metadata"


def test_structure_gate_enforce_accepts_a_grounded_plan() -> None:
    result = AggressiveRiskPolicy(
        max_symbol_margin_fraction=Decimal("1"),
        structure_gate_mode="enforce",
    ).evaluate(_structured_intent(), _structured_snapshot(), _portfolio(), RULES)

    assert result.decision.accepted
    assert result.order is not None
    assert result.decision.structure_assessment is not None
    assert result.decision.structure_assessment.passed
    assert {check.key for check in result.decision.structure_assessment.checks} == {
        "metadata",
        "anchor",
        "extension",
        "alignment",
        "trigger",
        "invalidation",
    }


def test_unconfirmed_breakout_is_rejected_even_in_shadow_mode() -> None:
    result = AggressiveRiskPolicy(
        structure_gate_mode="shadow",
    ).evaluate(_breakout_intent(), _structured_snapshot(), _portfolio(), RULES)

    assert not result.decision.accepted
    assert result.order is None
    assert result.decision.reason == (
        "breakout requires two closed bars beyond the pre-break boundary"
    )
    assert result.decision.structure_assessment is not None


def test_confirmed_two_bar_breakout_passes_the_hard_gate() -> None:
    snapshot = _structured_snapshot().model_copy(
        update={
            "features": {
                **_structured_snapshot().features,
                "5m_breakout_hold_above_20": 1.0,
                "5m_breakout_hold_high_20": 99.5,
            }
        }
    )
    result = AggressiveRiskPolicy(
        max_symbol_margin_fraction=Decimal("1"),
        structure_gate_mode="shadow",
    ).evaluate(_breakout_intent(), snapshot, _portfolio(), RULES)

    assert result.decision.accepted
    assert result.order is not None


def test_stop_loss_reentry_cooldown_rejects_opening_until_expiry() -> None:
    now = datetime.now(UTC)
    result = AggressiveRiskPolicy().evaluate(
        _intent(),
        _snapshot(),
        _portfolio(stop_loss_cooldown_until={"BTCUSDT": now + timedelta(minutes=30)}),
        RULES,
        now=now,
    )

    assert not result.decision.accepted
    assert result.order is None
    assert result.decision.reason.startswith(
        "stop-loss re-entry cooldown is active until "
    )


def test_expired_stop_loss_reentry_cooldown_does_not_reject() -> None:
    now = datetime.now(UTC)
    result = AggressiveRiskPolicy(
        max_symbol_margin_fraction=Decimal("1"),
    ).evaluate(
        _intent(),
        _snapshot(),
        _portfolio(stop_loss_cooldown_until={"BTCUSDT": now - timedelta(seconds=1)}),
        RULES,
        now=now,
    )

    assert result.decision.accepted


def test_single_symbol_initial_margin_is_capped_at_ten_percent_of_equity() -> None:
    intent = _intent().model_copy(update={"stop_loss": Decimal("99.95")})
    result = AggressiveRiskPolicy().evaluate(intent, _snapshot(), _portfolio(), RULES)
    assert result.decision.accepted
    assert result.order is not None
    assert result.order.quantity == Decimal("50.000")
    assert result.order.quantity * Decimal("100") / 5 == Decimal("1000")


def test_portfolio_initial_margin_is_capped_at_eighty_percent_of_equity() -> None:
    intent = _intent().model_copy(update={"stop_loss": Decimal("99.95")})
    result = AggressiveRiskPolicy(max_symbol_margin_fraction=Decimal("1")).evaluate(
        intent,
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
    assert result.order.quantity == Decimal("41.716")
    assert result.order.stop_price == Decimal("98")
    assert result.order.take_profit_price == Decimal("104")


def test_market_entry_is_capped_by_market_lot_size_maximum() -> None:
    rules = SymbolRules(
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
        tick_size=Decimal("0.01"),
        max_quantity=Decimal("1000"),
        market_quantity_step=Decimal("0.1"),
        market_min_quantity=Decimal("0.1"),
        market_max_quantity=Decimal("10.05"),
    )

    result = AggressiveRiskPolicy(max_symbol_margin_fraction=Decimal("1")).evaluate(
        _intent(), _snapshot(), _portfolio(), rules
    )

    assert result.decision.accepted
    assert result.order is not None and result.order.quantity == Decimal("10.0")
    assert "quantity capped at exchange MARKET_LOT_SIZE maxQty 10.05" in (
        result.decision.reason
    )


def test_kaito_sized_above_testnet_market_max_is_reduced_before_submission() -> None:
    rules = SymbolRules(
        quantity_step=Decimal("0.1"),
        min_quantity=Decimal("0.1"),
        min_notional=Decimal("5"),
        tick_size=Decimal("0.0001"),
        max_quantity=Decimal("1000000"),
        market_quantity_step=Decimal("0.1"),
        market_min_quantity=Decimal("0.1"),
        market_max_quantity=Decimal("500"),
    )
    snapshot = _snapshot().model_copy(
        update={
            "symbol": "KAITOUSDT",
            "mark_price": Decimal("0.9576"),
            "bid": Decimal("0.9575"),
            "ask": Decimal("0.9577"),
        }
    )
    intent = _intent().model_copy(
        update={
            "symbol": "KAITOUSDT",
            "leverage": 5,
            "risk_fraction": Decimal("0.008"),
            "stop_loss": Decimal("0.9350"),
            "take_profit": Decimal("1.0050"),
        }
    )

    result = AggressiveRiskPolicy().evaluate(intent, snapshot, _portfolio(), rules)

    assert result.decision.accepted
    assert result.order is not None and result.order.quantity == Decimal("500")
    assert "MARKET_LOT_SIZE maxQty 500" in result.decision.reason


def test_marketable_limit_entry_uses_lot_size_maximum() -> None:
    rules = SymbolRules(
        quantity_step=Decimal("0.01"),
        min_quantity=Decimal("0.01"),
        min_notional=Decimal("5"),
        tick_size=Decimal("0.01"),
        max_quantity=Decimal("7.5"),
        market_quantity_step=Decimal("0.1"),
        market_min_quantity=Decimal("0.1"),
        market_max_quantity=Decimal("2"),
    )
    intent = _intent().model_copy(
        update={
            "order_type": OrderType.LIMIT,
            "entry_price": Decimal("101"),
            "take_profit": Decimal("106"),
        }
    )

    result = AggressiveRiskPolicy(max_symbol_margin_fraction=Decimal("1")).evaluate(
        intent, _snapshot(), _portfolio(), rules
    )

    assert result.decision.accepted
    assert result.order is not None and result.order.quantity == Decimal("7.5")
    assert "quantity capped at exchange LOT_SIZE maxQty 7.5" in result.decision.reason


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
    intent = _intent(TradeAction.ADD)
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
    policy = AggressiveRiskPolicy(max_symbol_margin_fraction=Decimal("1"))
    existing_risk = Decimal("25") * policy._effective_loss_per_unit(
        "LONG", Decimal("100"), Decimal("98")
    )
    new_entry = policy._effective_entry(
        "LONG", Decimal("100"), _snapshot(), order_type=OrderType.MARKET
    )
    new_risk = result.order.quantity * policy._effective_loss_per_unit(
        "LONG", new_entry, Decimal("98")
    )
    assert existing_risk + new_risk <= Decimal("100")
    assert existing_risk + new_risk > Decimal("99.99")


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
        _intent(TradeAction.ADD).model_copy(update={"stop_loss": Decimal("99.8")}),
        _snapshot(),
        portfolio,
        RULES,
    )

    assert result.decision.accepted
    assert result.order is not None
    assert result.order.quantity == Decimal("5.000")
    assert Decimal("900") + result.order.quantity * Decimal("100") / 5 == Decimal(
        "1000"
    )


def test_add_preserves_an_existing_tighter_stop() -> None:
    portfolio = _portfolio(
        open_positions=1,
        positions=_position("LONG", "5", stop_loss="99", take_profit="104"),
    )
    result = AggressiveRiskPolicy(max_symbol_margin_fraction=Decimal("1")).evaluate(
        _intent(TradeAction.ADD).model_copy(
            update={"stop_loss": Decimal("98"), "take_profit": Decimal("106")}
        ),
        _snapshot(),
        portfolio,
        RULES,
    )

    assert result.decision.accepted
    assert result.order is not None and result.order.stop_price == Decimal("99")
    assert "existing tighter stop preserved" in result.decision.reason


def test_add_cannot_enter_below_a_profitable_trailing_stop() -> None:
    portfolio = _portfolio(
        open_positions=1,
        positions=_position("LONG", "5", stop_loss="101", take_profit="106"),
    )
    result = AggressiveRiskPolicy().evaluate(
        _intent(TradeAction.ADD), _snapshot(), portfolio, RULES
    )

    assert not result.decision.accepted
    assert "existing tightened long stop" in result.decision.reason


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
        update={
            "order_type": OrderType.LIMIT,
            "entry_price": Decimal("101"),
            "take_profit": Decimal("106"),
        }
    )
    result = AggressiveRiskPolicy().evaluate(
        marketable, _snapshot(), _portfolio(), RULES
    )

    assert result.decision.accepted and result.order is not None
    assert "immediately marketable after refresh" in result.decision.reason
    assert result.order.price == Decimal("101")


def test_queues_a_resting_opening_limit_without_submitting_it() -> None:
    resting = _intent().model_copy(
        update={"order_type": OrderType.LIMIT, "entry_price": Decimal("99")}
    )

    result = AggressiveRiskPolicy().evaluate(resting, _snapshot(), _portfolio(), RULES)

    assert result.decision.accepted
    assert result.decision.pending_entry
    assert result.order is not None
    assert "queued locally until trigger" in result.decision.reason


def test_marketable_short_limit_uses_fresh_bid_for_margin_sizing() -> None:
    intent = _intent(TradeAction.OPEN_SHORT).model_copy(
        update={
            "order_type": OrderType.LIMIT,
            "entry_price": Decimal("99"),
            "take_profit": Decimal("94"),
        }
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


def test_24h_loss_circuit_breaker() -> None:
    result = AggressiveRiskPolicy().evaluate(
        _intent(), _snapshot(), _portfolio(equity="9500", pnl_24h="-500"), RULES
    )
    assert not result.decision.accepted
    assert "circuit breaker" in result.decision.reason


def test_24h_loss_circuit_breaker_never_blocks_a_close() -> None:
    result = AggressiveRiskPolicy().evaluate(
        _intent(TradeAction.CLOSE),
        _snapshot(age_seconds=300),
        _portfolio(
            equity="9500",
            pnl_24h="-500",
            open_positions=1,
            positions=_position("LONG", "1"),
        ),
        RULES,
    )

    assert result.decision.accepted
    assert result.order is not None and result.order.reduce_only


def test_rejects_raw_reward_risk_at_the_strict_threshold() -> None:
    intent = _intent().model_copy(update={"take_profit": Decimal("102.3")})

    result = AggressiveRiskPolicy().evaluate(intent, _snapshot(), _portfolio(), RULES)

    assert not result.decision.accepted
    assert result.decision.pre_trade_entry_price == Decimal("100")
    assert result.decision.pre_trade_reward_risk_ratio == Decimal("1.15")
    assert (
        result.decision.reason
        == "pre-trade reward/risk ratio 1.1500:1 must be greater than 1.15:1"
    )


def test_raw_reward_risk_above_threshold_ignores_fees_and_slippage() -> None:
    intent = _intent().model_copy(update={"take_profit": Decimal("102.32")})

    result = AggressiveRiskPolicy().evaluate(intent, _snapshot(), _portfolio(), RULES)

    assert result.decision.accepted
    assert result.decision.pre_trade_entry_price == Decimal("100")
    assert result.decision.pre_trade_reward_risk_ratio == Decimal("1.16")


def test_rejects_raw_reward_risk_below_the_threshold() -> None:
    intent = _intent().model_copy(update={"take_profit": Decimal("102")})

    result = AggressiveRiskPolicy().evaluate(intent, _snapshot(), _portfolio(), RULES)

    assert not result.decision.accepted
    assert "pre-trade reward/risk ratio 1.0000:1" in result.decision.reason


def test_portfolio_stop_risk_caps_new_exposure_at_four_percent() -> None:
    positions = {
        "ETHUSDT": PositionState(
            side="LONG",
            quantity="150",
            entry_price="100",
            stop_loss="98",
            take_profit="104",
        )
    }
    portfolio = _portfolio(open_positions=1, positions=positions)
    policy = AggressiveRiskPolicy(max_symbol_margin_fraction=Decimal("1"))

    result = policy.evaluate(_intent(), _snapshot(), portfolio, RULES)

    assert result.decision.accepted and result.order is not None
    existing_risk = policy._portfolio_stop_risk(portfolio, replacing_symbol=None)
    entry = policy._effective_entry(
        "LONG", Decimal("100"), _snapshot(), order_type=OrderType.MARKET
    )
    new_risk = result.order.quantity * policy._effective_loss_per_unit(
        "LONG", entry, Decimal("98")
    )
    assert existing_risk + new_risk <= Decimal("400")
    assert existing_risk + new_risk > Decimal("399.99")


def test_missing_existing_stop_rejects_new_portfolio_risk() -> None:
    positions = {
        "ETHUSDT": PositionState(
            side="LONG",
            quantity="1",
            entry_price="100",
            take_profit="104",
        )
    }

    result = AggressiveRiskPolicy().evaluate(
        _intent(),
        _snapshot(),
        _portfolio(open_positions=1, positions=positions),
        RULES,
    )

    assert not result.decision.accepted
    assert "has no stop loss" in result.decision.reason


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
        _portfolio(
            open_positions=1,
            positions=_position("LONG", stop_loss="98", take_profit="104"),
        ),
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


def test_market_close_uses_market_lot_size_and_rejects_above_its_maximum() -> None:
    rules = SymbolRules(
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("5"),
        tick_size=Decimal("0.01"),
        max_quantity=Decimal("100"),
        market_quantity_step=Decimal("0.1"),
        market_min_quantity=Decimal("0.1"),
        market_max_quantity=Decimal("1"),
    )
    portfolio = _portfolio(
        open_positions=1,
        positions=_position("LONG", "1.24"),
    )

    result = AggressiveRiskPolicy().evaluate(
        _intent(TradeAction.CLOSE), _snapshot(), portfolio, rules
    )

    assert not result.decision.accepted
    assert result.order is None
    assert "exceeds exchange MARKET_LOT_SIZE maxQty" in result.decision.reason


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
        update={
            "order_type": OrderType.LIMIT,
            "entry_price": Decimal("100.7"),
            "take_profit": Decimal("105"),
        }
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
