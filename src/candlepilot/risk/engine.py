from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from uuid import uuid4

from candlepilot.domain.models import (
    MarketSnapshot,
    OrderPlan,
    OrderType,
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
    tick_size: Decimal
    max_quantity: Decimal | None = None
    market_quantity_step: Decimal | None = None
    market_min_quantity: Decimal | None = None
    market_max_quantity: Decimal | None = None

    def quantity_limits(
        self, order_type: OrderType
    ) -> tuple[Decimal, Decimal, Decimal | None]:
        if order_type == OrderType.MARKET:
            step = self.market_quantity_step or self.quantity_step
            minimum = (
                self.market_min_quantity
                if self.market_min_quantity is not None
                else self.min_quantity
            )
            maximum = (
                self.market_max_quantity
                if self.market_max_quantity is not None
                else self.max_quantity
            )
        else:
            step = self.quantity_step
            minimum = self.min_quantity
            maximum = self.max_quantity
        return step, minimum, maximum if maximum is not None and maximum > 0 else None


@dataclass(frozen=True, slots=True)
class RiskEvaluation:
    decision: RiskDecision
    order: OrderPlan | None = None


def _round_down(value: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        raise ValueError("quantity step must be positive")
    return (value / step).to_integral_value(rounding=ROUND_DOWN) * step


def _to_tick(value: Decimal, tick: Decimal, *, rounding: str) -> Decimal:
    """Snap a model-proposed price onto the exchange price grid.

    The model is given no tick size, so its prices are free-form and would be
    rejected by PRICE_FILTER. Callers round protective levels *away* from the
    entry, so snapping can never tighten a level through the price it was
    validated against.
    """

    if tick <= 0:
        raise ValueError("tick size must be positive")
    return (value / tick).to_integral_value(rounding=rounding) * tick


class AggressiveRiskPolicy:
    """Hard portfolio limits applied after an LLM proposes a trade."""

    def __init__(
        self,
        *,
        max_leverage: int = 10,
        max_risk_fraction: Decimal = Decimal("0.01"),
        max_portfolio_risk_fraction: Decimal = Decimal("0.04"),
        max_margin_fraction: Decimal = Decimal("0.80"),
        max_symbol_margin_fraction: Decimal = Decimal("0.10"),
        daily_loss_fraction: Decimal = Decimal("0.05"),
        minimum_reward_risk_ratio: Decimal = Decimal("1.3"),
        fee_fraction: Decimal = Decimal("0.0005"),
        slippage_fraction: Decimal = Decimal("0.001"),
        max_snapshot_age_seconds: int = 75,
        require_take_profit: bool = False,
    ) -> None:
        if max_snapshot_age_seconds <= 0:
            raise ValueError("max_snapshot_age_seconds must be positive")
        if max_risk_fraction < 0:
            raise ValueError("max_risk_fraction cannot be negative")
        if max_portfolio_risk_fraction < max_risk_fraction:
            raise ValueError(
                "max_portfolio_risk_fraction cannot be below max_risk_fraction"
            )
        if minimum_reward_risk_ratio <= 0:
            raise ValueError("minimum_reward_risk_ratio must be positive")
        if fee_fraction < 0 or slippage_fraction < 0:
            raise ValueError("fee and slippage fractions cannot be negative")
        self.max_leverage = max_leverage
        self.max_risk_fraction = max_risk_fraction
        self.max_portfolio_risk_fraction = max_portfolio_risk_fraction
        self.max_margin_fraction = max_margin_fraction
        self.max_symbol_margin_fraction = max_symbol_margin_fraction
        self.daily_loss_fraction = daily_loss_fraction
        self.minimum_reward_risk_ratio = minimum_reward_risk_ratio
        self.fee_fraction = fee_fraction
        self.slippage_fraction = slippage_fraction
        self.max_snapshot_age_seconds = max_snapshot_age_seconds
        self.require_take_profit = require_take_profit

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
        if intent.action == TradeAction.HOLD:
            return RiskEvaluation(RiskDecision(accepted=True, reason="hold: no order required"))

        existing = portfolio.positions.get(intent.symbol)
        if intent.action in {TradeAction.REDUCE, TradeAction.CLOSE}:
            if existing is None:
                return self._reject("cannot reduce or close a missing position")
            return self._close_order(intent, existing.side, existing.quantity, rules)

        age = (now - snapshot.timestamp).total_seconds()
        if age < -2 or age > self.max_snapshot_age_seconds:
            return self._reject("market snapshot is stale")

        window_start_equity = portfolio.equity - portfolio.pnl_24h
        if (
            window_start_equity > 0
            and portfolio.pnl_24h
            <= -(window_start_equity * self.daily_loss_fraction)
        ):
            return self._reject("24-hour loss circuit breaker is active")
        if intent.leverage > self.max_leverage:
            return self._reject("requested leverage exceeds the hard limit")
        if intent.risk_fraction > self.max_risk_fraction:
            return self._reject("requested risk exceeds the hard limit")

        existing_side = existing.side if existing is not None else None
        opening = intent.action in {TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT, TradeAction.ADD}
        if opening and intent.symbol in portfolio.pending_entry_symbols:
            return self._reject("a pending entry order already exists for this symbol")
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

        # A market entry fills at the book, so only a limit price reaches the
        # exchange and needs the grid; snapping it toward our side never turns a
        # marketable price into a worse fill.
        entry_rounding = ROUND_DOWN if requested_side == "LONG" else ROUND_UP
        if intent.order_type == OrderType.LIMIT and intent.entry_price is not None:
            entry = _to_tick(intent.entry_price, rules.tick_size, rounding=entry_rounding)
            if entry <= 0:
                return self._reject("entry price rounds to zero at the exchange tick size")
        else:
            entry = snapshot.mark_price
        stop = intent.stop_loss
        if stop is None:
            return self._reject("opening intent has no stop loss")
        stop = _to_tick(stop, rules.tick_size, rounding=entry_rounding)
        if stop <= 0:
            return self._reject("stop loss rounds to zero at the exchange tick size")
        if requested_side == "LONG" and stop >= entry:
            return self._reject("long stop loss must be below entry")
        if requested_side == "SHORT" and stop <= entry:
            return self._reject("short stop loss must be above entry")
        preserved_tighter_stop = False
        if intent.action == TradeAction.ADD:
            assert existing is not None
            if existing.stop_loss is None:
                return self._reject("cannot add while the existing position has no stop loss")
            tighter_stop = (
                max(stop, existing.stop_loss)
                if requested_side == "LONG"
                else min(stop, existing.stop_loss)
            )
            preserved_tighter_stop = tighter_stop != stop
            stop = tighter_stop
            if requested_side == "LONG" and stop >= entry:
                return self._reject(
                    "cannot add below an existing tightened long stop"
                )
            if requested_side == "SHORT" and stop <= entry:
                return self._reject(
                    "cannot add above an existing tightened short stop"
                )
        if requested_side == "LONG" and snapshot.mark_price <= stop:
            return self._reject("latest market price has crossed the long stop loss")
        if requested_side == "SHORT" and snapshot.mark_price >= stop:
            return self._reject("latest market price has crossed the short stop loss")

        take_profit = intent.take_profit
        if self.require_take_profit and take_profit is None:
            return self._reject("opening intent has no take profit")
        if take_profit is not None:
            take_profit = _to_tick(
                take_profit,
                rules.tick_size,
                rounding=ROUND_UP if requested_side == "LONG" else ROUND_DOWN,
            )
            if take_profit <= 0:
                return self._reject("take profit rounds to zero at the exchange tick size")
            if requested_side == "LONG" and take_profit <= entry:
                return self._reject("long take profit must be above entry")
            if requested_side == "SHORT" and take_profit >= entry:
                return self._reject("short take profit must be below entry")
            if requested_side == "LONG" and snapshot.mark_price >= take_profit:
                return self._reject("latest market price has crossed the long take profit")
            if requested_side == "SHORT" and snapshot.mark_price <= take_profit:
                return self._reject("latest market price has crossed the short take profit")

        immediately_marketable = intent.order_type == OrderType.LIMIT and (
            (requested_side == "LONG" and snapshot.ask <= entry)
            or (requested_side == "SHORT" and snapshot.bid >= entry)
        )
        pending_entry = intent.order_type == OrderType.LIMIT and not immediately_marketable

        effective_entry = self._effective_entry(
            requested_side,
            entry,
            snapshot,
            order_type=intent.order_type,
        )
        per_unit_loss = self._effective_loss_per_unit(
            requested_side,
            effective_entry,
            stop,
        )
        requested_risk_budget = portfolio.equity * min(
            intent.risk_fraction, self.max_risk_fraction
        )
        risk_budget = requested_risk_budget
        existing_symbol_risk = Decimal("0")
        if intent.action == TradeAction.ADD:
            assert existing is not None
            existing_symbol_risk = existing.quantity * self._effective_loss_per_unit(
                existing.side,
                existing.entry_price,
                stop,
            )
            remaining_hard_risk = max(
                Decimal("0"),
                (portfolio.equity * self.max_risk_fraction) - existing_symbol_risk,
            )
            risk_budget = min(requested_risk_budget, remaining_hard_risk)
            if risk_budget <= 0:
                return self._reject("existing position exhausts the symbol risk limit")

        try:
            portfolio_risk = self._portfolio_stop_risk(
                portfolio,
                replacing_symbol=intent.symbol if intent.action == TradeAction.ADD else None,
            )
        except ValueError as exc:
            return self._reject(str(exc))
        remaining_portfolio_risk = max(
            Decimal("0"),
            (portfolio.equity * self.max_portfolio_risk_fraction)
            - portfolio_risk
            - existing_symbol_risk,
        )
        risk_budget = min(risk_budget, remaining_portfolio_risk)
        if risk_budget <= 0:
            return self._reject("portfolio stop risk limit is exhausted")
        risk_quantity = risk_budget / per_unit_loss
        remaining_margin = max(
            Decimal("0"),
            (portfolio.equity * self.max_margin_fraction) - portfolio.margin_used,
        )
        existing_symbol_margin = (
            existing.initial_margin if existing is not None else Decimal("0")
        )
        remaining_symbol_margin = max(
            Decimal("0"),
            (portfolio.equity * self.max_symbol_margin_fraction)
            - existing_symbol_margin,
        )
        margin_price = (
            max(entry, snapshot.ask if requested_side == "LONG" else snapshot.bid)
            if intent.order_type == OrderType.LIMIT
            else entry
        )
        margin_quantity = (
            min(
                remaining_margin,
                remaining_symbol_margin,
                portfolio.available_balance,
            )
            * intent.leverage
        ) / margin_price
        quantity_step, min_quantity, max_quantity = rules.quantity_limits(intent.order_type)
        uncapped_quantity = min(risk_quantity, margin_quantity)
        exchange_capped = max_quantity is not None and uncapped_quantity > max_quantity
        quantity = _round_down(
            min(uncapped_quantity, max_quantity)
            if max_quantity is not None
            else uncapped_quantity,
            quantity_step,
        )
        if quantity < min_quantity:
            return self._reject("risk-sized quantity is below the exchange minimum")
        if quantity * entry < rules.min_notional:
            return self._reject("risk-sized notional is below the exchange minimum")

        reward_risk_entry = entry
        if intent.action == TradeAction.ADD:
            assert existing is not None
            combined_quantity = existing.quantity + quantity
            reward_risk_entry = (
                (existing.entry_price * existing.quantity)
                + (entry * quantity)
            ) / combined_quantity
        if take_profit is not None:
            reward_risk_ratio = self._raw_reward_risk_ratio(
                requested_side,
                reward_risk_entry,
                stop,
                take_profit,
            )
            if reward_risk_ratio <= self.minimum_reward_risk_ratio:
                return RiskEvaluation(
                    RiskDecision(
                        accepted=False,
                        reason=(
                            "pre-trade reward/risk ratio "
                            f"{reward_risk_ratio:.4f}:1 must be greater than "
                            f"{self.minimum_reward_risk_ratio}:1"
                        ),
                        pre_trade_entry_price=reward_risk_entry,
                        pre_trade_reward_risk_ratio=reward_risk_ratio,
                    )
                )

        order = OrderPlan(
            client_order_id=f"cp-{uuid4().hex[:24]}",
            symbol=intent.symbol,
            side="BUY" if requested_side == "LONG" else "SELL",
            quantity=quantity,
            order_type=intent.order_type,
            price=entry if intent.order_type == OrderType.LIMIT else None,
            stop_price=stop,
            take_profit_price=take_profit,
            reduce_only=False,
        )
        accepted_reasons = ["accepted within hard risk limits"]
        if exchange_capped:
            quantity_filter = (
                "MARKET_LOT_SIZE"
                if intent.order_type == OrderType.MARKET
                else "LOT_SIZE"
            )
            accepted_reasons.append(
                f"quantity capped at exchange {quantity_filter} maxQty {max_quantity}"
            )
        if immediately_marketable:
            accepted_reasons.append("limit entry is immediately marketable after refresh")
        if pending_entry:
            accepted_reasons.append("resting limit intent queued locally until trigger")
        if preserved_tighter_stop:
            accepted_reasons.append("existing tighter stop preserved for the merged position")
        return RiskEvaluation(
            decision=RiskDecision(
                accepted=True,
                reason="; ".join(accepted_reasons),
                max_quantity=quantity,
                pre_trade_entry_price=reward_risk_entry,
                pre_trade_reward_risk_ratio=reward_risk_ratio
                if take_profit is not None
                else None,
                pending_entry=pending_entry,
            ),
            order=order,
        )

    def _effective_entry(
        self,
        side: str,
        entry: Decimal,
        snapshot: MarketSnapshot,
        *,
        order_type: OrderType,
    ) -> Decimal:
        if order_type == OrderType.LIMIT:
            return entry
        book_price = snapshot.ask if side == "LONG" else snapshot.bid
        slip = book_price * self.slippage_fraction
        return book_price + slip if side == "LONG" else book_price - slip

    def _effective_exit(self, side: str, price: Decimal) -> Decimal:
        slip = price * self.slippage_fraction
        return price - slip if side == "LONG" else price + slip

    def _effective_loss_per_unit(
        self,
        side: str,
        entry: Decimal,
        stop: Decimal,
    ) -> Decimal:
        direction = Decimal("1") if side == "LONG" else Decimal("-1")
        exit_price = self._effective_exit(side, stop)
        price_loss = max(Decimal("0"), (entry - exit_price) * direction)
        fees = (entry + exit_price) * self.fee_fraction
        return price_loss + fees

    @staticmethod
    def _raw_reward_risk_ratio(
        side: str, entry: Decimal, stop: Decimal, take_profit: Decimal
    ) -> Decimal:
        direction = Decimal("1") if side == "LONG" else Decimal("-1")
        reward = max(Decimal("0"), (take_profit - entry) * direction)
        risk = max(Decimal("0"), (entry - stop) * direction)
        return reward / risk if risk > 0 else Decimal("0")

    def _portfolio_stop_risk(
        self,
        portfolio: PortfolioState,
        *,
        replacing_symbol: str | None,
    ) -> Decimal:
        total = Decimal("0")
        for symbol, position in portfolio.positions.items():
            if symbol == replacing_symbol:
                continue
            if position.stop_loss is None:
                raise ValueError(
                    f"cannot measure portfolio stop risk: {symbol} has no stop loss"
                )
            total += position.quantity * self._effective_loss_per_unit(
                position.side,
                position.entry_price,
                position.stop_loss,
            )
        return total

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
        quantity_step, min_quantity, max_quantity = rules.quantity_limits(
            intent.order_type
        )
        quantity = _round_down(position_quantity, quantity_step)
        if intent.action == TradeAction.REDUCE:
            quantity = _round_down(quantity / 2, quantity_step)
        if quantity <= 0 or quantity < min_quantity:
            return AggressiveRiskPolicy._reject(
                "reduce-only quantity is below the exchange minimum"
            )
        if max_quantity is not None and quantity > max_quantity:
            quantity_filter = (
                "MARKET_LOT_SIZE"
                if intent.order_type == OrderType.MARKET
                else "LOT_SIZE"
            )
            return AggressiveRiskPolicy._reject(
                f"reduce-only quantity exceeds exchange {quantity_filter} maxQty"
            )
        side = "SELL" if existing_side == "LONG" else "BUY"
        price = intent.entry_price
        if intent.order_type == OrderType.LIMIT and price is not None:
            price = _to_tick(
                price,
                rules.tick_size,
                rounding=ROUND_UP if side == "SELL" else ROUND_DOWN,
            )
            if price <= 0:
                return AggressiveRiskPolicy._reject(
                    "exit price rounds to zero at the exchange tick size"
                )
        order = OrderPlan(
            client_order_id=f"cp-{uuid4().hex[:24]}",
            symbol=intent.symbol,
            side=side,
            quantity=quantity,
            order_type=intent.order_type,
            price=price,
            reduce_only=True,
        )
        return RiskEvaluation(
            RiskDecision(accepted=True, reason="reduce-only exit accepted"), order=order
        )
