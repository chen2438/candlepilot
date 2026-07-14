from __future__ import annotations

import asyncio
from decimal import Decimal

from candlepilot.domain.models import ExecutionReport, MarketSnapshot, OrderPlan, OrderType


class PaperExecutor:
    """Deterministic fill simulator for the production-data paper mode."""

    def __init__(self, *, slippage_fraction: Decimal = Decimal("0.0005")) -> None:
        self.slippage_fraction = slippage_fraction
        self._orders: dict[str, ExecutionReport] = {}
        self._lock = asyncio.Lock()

    async def execute(self, order: OrderPlan, snapshot: MarketSnapshot) -> ExecutionReport:
        async with self._lock:
            existing = self._orders.get(order.client_order_id)
            if existing is not None:
                return existing
            fill_price: Decimal | None
            status: str
            if order.order_type == OrderType.MARKET:
                reference = snapshot.ask if order.side == "BUY" else snapshot.bid
                direction = Decimal("1") if order.side == "BUY" else Decimal("-1")
                fill_price = reference * (Decimal("1") + direction * self.slippage_fraction)
                status = "FILLED"
            else:
                crosses = (order.side == "BUY" and order.price >= snapshot.ask) or (
                    order.side == "SELL" and order.price <= snapshot.bid
                )
                fill_price = order.price if crosses else None
                status = "FILLED" if crosses else "NEW"
            report = ExecutionReport(
                client_order_id=order.client_order_id,
                status=status,
                filled_quantity=order.quantity if fill_price is not None else Decimal("0"),
                average_price=fill_price,
                message="paper fill simulator",
            )
            self._orders[order.client_order_id] = report
            return report

    async def emergency_flatten(self) -> None:
        async with self._lock:
            for order_id, report in list(self._orders.items()):
                if report.status == "NEW":
                    self._orders[order_id] = report.model_copy(update={"status": "CANCELED"})

    @property
    def orders(self) -> tuple[ExecutionReport, ...]:
        return tuple(self._orders.values())

