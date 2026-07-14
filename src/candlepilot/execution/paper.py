from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from uuid import uuid4

from candlepilot.domain.models import (
    ExecutionReport,
    MarketSnapshot,
    OrderPlan,
    OrderType,
    PortfolioState,
)


@dataclass(slots=True)
class PaperPosition:
    side: str
    quantity: Decimal
    average_price: Decimal
    leverage: int
    stop_loss: Decimal | None
    take_profit: Decimal | None


class PaperExecutor:
    """Deterministic fill simulator for the production-data paper mode."""

    def __init__(
        self,
        *,
        initial_equity: Decimal = Decimal("10000"),
        slippage_fraction: Decimal = Decimal("0.0005"),
        fee_rate: Decimal = Decimal("0.0005"),
    ) -> None:
        self.initial_equity = initial_equity
        self.cash = initial_equity
        self.slippage_fraction = slippage_fraction
        self.fee_rate = fee_rate
        self._orders: dict[str, ExecutionReport] = {}
        self._pending_orders: dict[str, tuple[OrderPlan, int]] = {}
        self._positions: dict[str, PaperPosition] = {}
        self._marks: dict[str, Decimal] = {}
        self._lock = asyncio.Lock()

    async def execute(
        self, order: OrderPlan, snapshot: MarketSnapshot, *, leverage: int = 1
    ) -> ExecutionReport:
        async with self._lock:
            self._marks[snapshot.symbol] = snapshot.mark_price
            existing = self._orders.get(order.client_order_id)
            if existing is not None:
                return existing
            fill_price: Decimal | None
            status: str
            if order.order_type == OrderType.MARKET:
                fill_price = self._market_fill(order.side, snapshot)
                status = "FILLED"
            else:
                fill_price = self._limit_fill(order, snapshot)
                status = "FILLED" if fill_price is not None else "NEW"
            report = ExecutionReport(
                client_order_id=order.client_order_id,
                status=status,
                filled_quantity=order.quantity if fill_price is not None else Decimal("0"),
                average_price=fill_price,
                message="paper fill simulator",
            )
            self._orders[order.client_order_id] = report
            if fill_price is not None:
                self._apply_fill(order, fill_price, leverage)
            else:
                self._pending_orders[order.client_order_id] = (order, leverage)
            return report

    async def mark_to_market(self, snapshot: MarketSnapshot) -> tuple[ExecutionReport, ...]:
        async with self._lock:
            self._marks[snapshot.symbol] = snapshot.mark_price
            completed: list[ExecutionReport] = []
            for order_id, (order, leverage) in list(self._pending_orders.items()):
                if order.symbol != snapshot.symbol:
                    continue
                fill_price = self._limit_fill(order, snapshot)
                if fill_price is None:
                    continue
                report = self._orders[order_id].model_copy(
                    update={
                        "status": "FILLED",
                        "filled_quantity": order.quantity,
                        "average_price": fill_price,
                    }
                )
                self._orders[order_id] = report
                del self._pending_orders[order_id]
                self._apply_fill(order, fill_price, leverage)
                completed.append(report)

            position = self._positions.get(snapshot.symbol)
            if position is not None:
                reason = self._protective_trigger(position, snapshot.mark_price)
                if reason is not None:
                    side = "SELL" if position.side == "LONG" else "BUY"
                    order = OrderPlan(
                        client_order_id=f"cp-paper-{reason}-{uuid4().hex[:16]}",
                        symbol=snapshot.symbol,
                        side=side,
                        quantity=position.quantity,
                        order_type=OrderType.MARKET,
                        reduce_only=True,
                    )
                    fill_price = self._market_fill(side, snapshot)
                    report = ExecutionReport(
                        client_order_id=order.client_order_id,
                        status="FILLED",
                        filled_quantity=order.quantity,
                        average_price=fill_price,
                        message=f"paper {reason}",
                    )
                    self._orders[order.client_order_id] = report
                    self._apply_fill(order, fill_price, position.leverage)
                    completed.append(report)
            return tuple(completed)

    def _market_fill(self, side: str, snapshot: MarketSnapshot) -> Decimal:
        reference = snapshot.ask if side == "BUY" else snapshot.bid
        direction = Decimal("1") if side == "BUY" else Decimal("-1")
        return reference * (Decimal("1") + direction * self.slippage_fraction)

    @staticmethod
    def _limit_fill(order: OrderPlan, snapshot: MarketSnapshot) -> Decimal | None:
        crosses = (order.side == "BUY" and order.price >= snapshot.ask) or (
            order.side == "SELL" and order.price <= snapshot.bid
        )
        return order.price if crosses else None

    @staticmethod
    def _protective_trigger(position: PaperPosition, mark: Decimal) -> str | None:
        if position.side == "LONG":
            if position.stop_loss is not None and mark <= position.stop_loss:
                return "stop_loss"
            if position.take_profit is not None and mark >= position.take_profit:
                return "take_profit"
        else:
            if position.stop_loss is not None and mark >= position.stop_loss:
                return "stop_loss"
            if position.take_profit is not None and mark <= position.take_profit:
                return "take_profit"
        return None

    def _apply_fill(self, order: OrderPlan, price: Decimal, leverage: int) -> None:
        fee = order.quantity * price * self.fee_rate
        self.cash -= fee
        position = self._positions.get(order.symbol)
        if order.reduce_only:
            if position is None:
                return
            quantity = min(order.quantity, position.quantity)
            direction = Decimal("1") if position.side == "LONG" else Decimal("-1")
            self.cash += quantity * (price - position.average_price) * direction
            position.quantity -= quantity
            if position.quantity <= 0:
                del self._positions[order.symbol]
            return
        side = "LONG" if order.side == "BUY" else "SHORT"
        if position is None:
            self._positions[order.symbol] = PaperPosition(
                side,
                order.quantity,
                price,
                leverage,
                order.stop_price,
                order.take_profit_price,
            )
            return
        if position.side != side:
            raise RuntimeError("paper executor cannot cross an existing position")
        total = position.quantity + order.quantity
        position.average_price = (
            position.average_price * position.quantity + price * order.quantity
        ) / total
        position.quantity = total
        position.leverage = max(position.leverage, leverage)
        position.stop_loss = order.stop_price or position.stop_loss
        position.take_profit = order.take_profit_price or position.take_profit

    async def emergency_flatten(self) -> None:
        async with self._lock:
            for order_id, report in list(self._orders.items()):
                if report.status == "NEW":
                    self._orders[order_id] = report.model_copy(update={"status": "CANCELED"})
            self._pending_orders.clear()
            for symbol, position in list(self._positions.items()):
                mark = self._marks.get(symbol, position.average_price)
                direction = Decimal("1") if position.side == "LONG" else Decimal("-1")
                self.cash += position.quantity * (mark - position.average_price) * direction
                self.cash -= position.quantity * mark * self.fee_rate
            self._positions.clear()

    def portfolio_state(self) -> PortfolioState:
        unrealized = Decimal("0")
        margin_used = Decimal("0")
        for symbol, position in self._positions.items():
            mark = self._marks.get(symbol, position.average_price)
            direction = Decimal("1") if position.side == "LONG" else Decimal("-1")
            unrealized += position.quantity * (mark - position.average_price) * direction
            margin_used += position.quantity * mark / position.leverage
        equity = self.cash + unrealized
        return PortfolioState(
            equity=max(Decimal("0.00000001"), equity),
            available_balance=max(Decimal("0"), equity - margin_used),
            daily_pnl=equity - self.initial_equity,
            open_positions=len(self._positions),
            margin_used=margin_used,
            symbol_sides={symbol: position.side for symbol, position in self._positions.items()},
            symbol_quantities={
                symbol: position.quantity for symbol, position in self._positions.items()
            },
        )

    @property
    def orders(self) -> tuple[ExecutionReport, ...]:
        return tuple(self._orders.values())

    @property
    def position_symbols(self) -> tuple[str, ...]:
        return tuple(self._positions)
