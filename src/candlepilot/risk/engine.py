from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN
from uuid import uuid4

from candlepilot.domain.models import (
    MarketSnapshot,
    OrderPlan,
    PortfolioState,
    RiskDecision,
    TradeAction,
    TradeIntent,
)


@dataclass(frozen=True, slots=True)
class SymbolRules:
    quantity_step: Decimal
    min_quantity: Decimal
    min_notional: Decimal


@dataclass(frozen=True, slots=True)
class RiskEvaluation:
    decision: RiskDecision
    order: OrderPlan | None = None


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("quantity step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


class AggressiveRiskPolicy:
    """Hard portfolio limits applied after an LLM proposes a trade."""

    def __init__(
        self,
        *,
        max_leverage: int = 10,
        max_risk_fraction: Decimal = Decimal("0.02"),
        max_positions: int = 8,
        max_margin_fraction: Decimal = Decimal("0.60"),
        daily_loss_fraction: Decimal = Decimal("0.08"),
        slippage_fraction: Decimal = Decimal("0.001"),
        max_snapshot_age_seconds: int = 15,
    ) -> None:
        self.max_leverage = max_leverage
        self.max_risk_fraction = max_risk_fraction
        self.max_positions = max_positions
        self.max_margin_fraction = max_margin_fraction
        self.daily_loss_fraction = daily_loss_fraction
        self.slippage_fraction = slippage_fraction
        self.max_snapshot_age_seconds = max_snapshot_age_seconds

    def evaluate(
        self,
        intent: TradeIntent,
        snapshot: MarketSnapshot,
        portfolio: PortfolioState,
        rules: SymbolRules,
        *,
        now: datetime | None = None,
    ) -> RiskEvaluation:
        now = now or datetime.now(UTC)
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        if intent.symbol != snapshot.symbol or intent.cadence != snapshot.cadence:
            return self._reject("intent does not match its market snapshot")
        age = (now - snapshot.timestamp).total_seconds()
        if age < -2 or age > self.max_snapshot_age_seconds:
            return self._reject("market snapshot is stale")
        if intent.action == TradeAction.HOLD:
            return RiskEvaluation(RiskDecision(accepted=True, reason="hold: no order required"))

        start_equity = portfolio.equity - portfolio.daily_pnl
        if start_equity > 0 and portfolio.daily_pnl <= -(start_equity * self.daily_loss_fraction):
            return self._reject("daily loss circuit breaker is active")
        if intent.leverage > self.max_leverage:
            return self._reject("requested leverage exceeds the hard limit")
        if intent.risk_fraction > self.max_risk_fraction:
            return self._reject("requested risk exceeds the hard limit")

        existing_side = portfolio.symbol_sides.get(intent.symbol)
        opening = intent.action in {TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT, TradeAction.ADD}
        if opening and existing_side is None and portfolio.open_positions >= self.max_positions:
            return self._reject("maximum open position count reached")
        requested_side = (
            existing_side
            if intent.action == TradeAction.ADD
            else "LONG"
            if intent.action == TradeAction.OPEN_LONG
            else "SHORT"
        )
        if intent.action == TradeAction.ADD and existing_side is None:
            return self._reject("cannot add without an existing position")
        if intent.action == TradeAction.OPEN_LONG and existing_side == "LONG":
            return self._reject("existing long position requires an explicit ADD intent")
        if intent.action == TradeAction.OPEN_SHORT and existing_side == "SHORT":
            return self._reject("existing short position requires an explicit ADD intent")
        if intent.action == TradeAction.OPEN_LONG and existing_side == "SHORT":
            return self._reject("opposing position must be closed before opening long")
        if intent.action == TradeAction.OPEN_SHORT and existing_side == "LONG":
            return self._reject("opposing position must be closed before opening short")

        if intent.action in {TradeAction.REDUCE, TradeAction.CLOSE}:
            if existing_side is None:
                return self._reject("cannot reduce or close a missing position")
            position_quantity = portfolio.symbol_quantities.get(intent.symbol)
            if position_quantity is None:
                return self._reject("position quantity is unavailable for reduce-only exit")
            return self._close_order(intent, existing_side, position_quantity, rules)

        entry = intent.entry_price or snapshot.mark_price
        stop = intent.stop_loss
        if stop is None:
            return self._reject("opening intent has no stop loss")
        if requested_side == "LONG" and stop >= entry:
            return self._reject("long stop loss must be below entry")
        if requested_side == "SHORT" and stop <= entry:
            return self._reject("short stop loss must be above entry")

        per_unit_loss = abs(entry - stop) + (entry * self.slippage_fraction)
        risk_budget = portfolio.equity * min(intent.risk_fraction, self.max_risk_fraction)
        risk_quantity = risk_budget / per_unit_loss
        remaining_margin = max(
            Decimal("0"),
            (portfolio.equity * self.max_margin_fraction) - portfolio.margin_used,
        )
        margin_quantity = (min(remaining_margin, portfolio.available_balance) * intent.leverage) / entry
        quantity = _round_down(min(risk_quantity, margin_quantity), rules.quantity_step)
        if quantity < rules.min_quantity:
            return self._reject("risk-sized quantity is below the exchange minimum")
        if quantity * entry < rules.min_notional:
            return self._reject("risk-sized notional is below the exchange minimum")

        order = OrderPlan(
            client_order_id=f"cp-{uuid4().hex[:24]}",
            symbol=intent.symbol,
            side="BUY" if requested_side == "LONG" else "SELL",
            quantity=quantity,
            order_type=intent.order_type,
            price=intent.entry_price,
            stop_price=stop,
            take_profit_price=intent.take_profit,
            reduce_only=False,
        )
        return RiskEvaluation(
            decision=RiskDecision(
                accepted=True,
                reason="accepted within hard risk limits",
                max_quantity=quantity,
            ),
            order=order,
        )

    @staticmethod
    def _reject(reason: str) -> RiskEvaluation:
        return RiskEvaluation(RiskDecision(accepted=False, reason=reason))

    @staticmethod
    def _close_order(
        intent: TradeIntent,
        existing_side: str,
        position_quantity: Decimal,
        rules: SymbolRules,
    ) -> RiskEvaluation:
        quantity = _round_down(position_quantity, rules.quantity_step)
        if intent.action == TradeAction.REDUCE:
            quantity = _round_down(quantity / 2, rules.quantity_step)
        order = OrderPlan(
            client_order_id=f"cp-{uuid4().hex[:24]}",
            symbol=intent.symbol,
            side="SELL" if existing_side == "LONG" else "BUY",
            quantity=quantity,
            order_type=intent.order_type,
            price=intent.entry_price,
            reduce_only=True,
        )
        return RiskEvaluation(
            RiskDecision(accepted=True, reason="reduce-only exit accepted"), order=order
        )
