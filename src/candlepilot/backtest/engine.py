"""Fill simulation for historical replay.

The one rule here: everything the live path decides is decided by the live
code. This module owns only what an exchange owns -- whether a resting trigger
was touched, at what price, and what it cost. Sizing, leverage caps, the rolling
24-hour loss breaker and tick alignment all come from ``AggressiveRiskPolicy``, because
a backtest that re-implements them measures the re-implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from candlepilot.domain.models import (
    ExecutionReport,
    OrderPlan,
    PortfolioState,
    PositionState,
)


@dataclass(frozen=True, slots=True)
class Candle:
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    funding_rate: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            raise ValueError("candle timestamp must be timezone-aware")
        if self.high < self.low:
            raise ValueError("candle high cannot be below its low")


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_equity: Decimal = Decimal("10000")
    fee_rate: Decimal = Decimal("0.0005")
    slippage_fraction: Decimal = Decimal("0.0005")


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    symbol: str
    side: str
    quantity: Decimal
    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime
    exit_price: Decimal
    net_pnl: Decimal
    fees: Decimal
    funding: Decimal
    exit_reason: str


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: datetime
    equity: Decimal


@dataclass(slots=True)
class _Position:
    side: str
    quantity: Decimal
    entry_price: Decimal
    entry_time: datetime
    leverage: int
    stop_loss: Decimal
    take_profit: Decimal | None
    fees: Decimal = Decimal("0")
    funding: Decimal = Decimal("0")


@dataclass(slots=True)
class _PendingOrder:
    order: OrderPlan
    submitted_at: datetime
    leverage: int


@dataclass(frozen=True, slots=True)
class BacktestResult:
    initial_equity: Decimal
    final_equity: Decimal
    gross_price_pnl: Decimal
    net_pnl: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    win_rate: Decimal
    profit_factor: Decimal | None
    trade_count: int
    total_fees: Decimal
    total_funding: Decimal
    run_end_trade_count: int
    cancelled_pending_orders: int
    symbol_results: list[BacktestSymbolResult] = field(default_factory=list)
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[EquityPoint] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class BacktestSymbolResult:
    """One symbol's additive contribution to a shared portfolio result."""

    symbol: str
    gross_price_pnl: Decimal
    net_pnl: Decimal
    contribution_return: Decimal
    trade_count: int
    total_fees: Decimal
    total_funding: Decimal


class SimulatedExchange:
    """Books orders against historical candles for one symbol at a time.

    Entries fill on the candle after the decision -- the model reasoned on a
    closed bar, so filling inside it would be reading the future. Protective
    triggers are checked against the candle's own range, and when both the stop
    and the take profit sit inside one candle the stop wins: the bar does not
    say which came first, and assuming the good one is how a backtest flatters
    itself.
    """

    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()
        self.cash = self.config.initial_equity
        self._positions: dict[str, _Position] = {}
        self._pending: dict[str, _PendingOrder] = {}
        self.trades: list[BacktestTrade] = []
        self._equity_window: list[EquityPoint] = []

    def portfolio_state(
        self, marks: dict[str, Decimal], *, as_of: datetime | None = None
    ) -> PortfolioState:
        unrealized = Decimal("0")
        margin_used = Decimal("0")
        positions: dict[str, PositionState] = {}
        for symbol, position in self._positions.items():
            mark = marks.get(symbol, position.entry_price)
            direction = Decimal("1") if position.side == "LONG" else Decimal("-1")
            pnl = position.quantity * (mark - position.entry_price) * direction
            unrealized += pnl
            margin_used += position.quantity * mark / position.leverage
            positions[symbol] = PositionState(
                side=position.side,  # type: ignore[arg-type]
                quantity=position.quantity,
                entry_price=position.entry_price,
                unrealized_pnl=pnl,
                leverage=position.leverage,
                initial_margin=position.quantity * mark / position.leverage,
                stop_loss=position.stop_loss,
                take_profit=position.take_profit,
            )
        equity = self.cash + unrealized
        window_start_equity = self.config.initial_equity
        if as_of is not None:
            if as_of.tzinfo is None:
                raise ValueError("portfolio time must be timezone-aware")
            current_time = as_of.astimezone(UTC)
            if self._equity_window and current_time < self._equity_window[-1].timestamp:
                raise ValueError("portfolio time cannot move backwards")
            point = EquityPoint(current_time, equity)
            if self._equity_window and current_time == self._equity_window[-1].timestamp:
                self._equity_window[-1] = point
            else:
                self._equity_window.append(point)
            cutoff = current_time - timedelta(hours=24)
            while (
                len(self._equity_window) > 1
                and self._equity_window[1].timestamp <= cutoff
            ):
                self._equity_window.pop(0)
            window_start_equity = self._equity_window[0].equity
        elif self._equity_window:
            window_start_equity = self._equity_window[0].equity
        return PortfolioState(
            equity=max(Decimal("0.00000001"), equity),
            available_balance=max(Decimal("0"), equity - margin_used),
            pnl_24h=equity - window_start_equity,
            open_positions=len(self._positions),
            margin_used=margin_used,
            positions=positions,
        )

    def equity(self, marks: dict[str, Decimal]) -> Decimal:
        return self.portfolio_state(marks).equity

    def _slipped(self, price: Decimal, side: str) -> Decimal:
        drift = price * self.config.slippage_fraction
        return price + drift if side == "BUY" else price - drift

    def has_pending(self, symbol: str) -> bool:
        return symbol in self._pending

    def execute(self, order: OrderPlan, candle: Candle, *, leverage: int) -> ExecutionReport:
        """Fill marketable orders at the open; keep resting limits pending."""

        if order.order_type.value == "LIMIT":
            assert order.price is not None
            marketable = (order.side == "BUY" and order.price >= candle.open) or (
                order.side == "SELL" and order.price <= candle.open
            )
            if not marketable:
                self._pending[order.symbol] = _PendingOrder(
                    order=order, submitted_at=candle.timestamp, leverage=leverage
                )
                return ExecutionReport(
                    client_order_id=order.client_order_id,
                    status="NEW",
                    message="simulated resting limit",
                    timestamp=candle.timestamp,
                )

        return self._fill(order, candle.open, candle.timestamp, leverage=leverage)

    def _fill(
        self,
        order: OrderPlan,
        base_price: Decimal,
        when: datetime,
        *,
        leverage: int,
    ) -> ExecutionReport:
        fill = self._slipped(base_price, order.side)
        if order.order_type.value == "LIMIT":
            assert order.price is not None
            fill = min(fill, order.price) if order.side == "BUY" else max(fill, order.price)

        fee = fill * order.quantity * self.config.fee_rate
        self.cash -= fee
        if order.reduce_only:
            self._reduce(order, fill, when, fee)
        else:
            self._open_or_add(order, fill, when, leverage, fee)
        return ExecutionReport(
            client_order_id=order.client_order_id,
            status="FILLED",
            filled_quantity=order.quantity,
            average_price=fill,
            message="simulated fill",
            timestamp=when,
        )

    def _open_or_add(
        self,
        order: OrderPlan,
        fill: Decimal,
        when: datetime,
        leverage: int,
        fee: Decimal,
    ) -> None:
        side = "LONG" if order.side == "BUY" else "SHORT"
        existing = self._positions.get(order.symbol)
        if existing is None:
            assert order.stop_price is not None
            self._positions[order.symbol] = _Position(
                side=side,
                quantity=order.quantity,
                entry_price=fill,
                entry_time=when,
                leverage=leverage,
                stop_loss=order.stop_price,
                take_profit=order.take_profit_price,
                fees=fee,
            )
            return
        total = existing.quantity + order.quantity
        existing.entry_price = (
            existing.entry_price * existing.quantity + fill * order.quantity
        ) / total
        existing.quantity = total
        existing.leverage = leverage
        existing.fees += fee
        if order.stop_price is not None:
            existing.stop_loss = order.stop_price
        if order.take_profit_price is not None:
            existing.take_profit = order.take_profit_price

    def _reduce(self, order: OrderPlan, fill: Decimal, when: datetime, fee: Decimal) -> None:
        position = self._positions.get(order.symbol)
        if position is None:
            return
        quantity = min(order.quantity, position.quantity)
        self._book(order.symbol, position, quantity, fill, when, fee, "model_exit")

    def _book(
        self,
        symbol: str,
        position: _Position,
        quantity: Decimal,
        exit_price: Decimal,
        when: datetime,
        fee: Decimal,
        reason: str,
    ) -> None:
        share = quantity / position.quantity
        direction = Decimal("1") if position.side == "LONG" else Decimal("-1")
        gross = quantity * (exit_price - position.entry_price) * direction
        entry_fees = position.fees * share
        funding = position.funding * share
        self.cash += gross - funding
        self.trades.append(
            BacktestTrade(
                symbol=symbol,
                side=position.side,
                quantity=quantity,
                entry_time=position.entry_time,
                entry_price=position.entry_price,
                exit_time=when,
                exit_price=exit_price,
                net_pnl=gross - entry_fees - fee - funding,
                fees=entry_fees + fee,
                funding=funding,
                exit_reason=reason,
            )
        )
        position.quantity -= quantity
        position.fees -= entry_fees
        position.funding -= funding
        if position.quantity <= 0:
            del self._positions[symbol]

    def settle_candle(self, symbol: str, candle: Candle) -> None:
        """Charge funding and close the position if a trigger was touched."""

        position = self._positions.get(symbol)
        if position is not None:
            direction = Decimal("1") if position.side == "LONG" else Decimal("-1")
            position.funding += (
                position.quantity * candle.close * candle.funding_rate * direction
            )

            trigger, reason = self._touched(position, candle)
            if trigger is not None:
                exit_side = "SELL" if position.side == "LONG" else "BUY"
                fill = self._slipped(trigger, exit_side)
                fee = fill * position.quantity * self.config.fee_rate
                self.cash -= fee
                self._book(
                    symbol, position, position.quantity, fill, candle.timestamp, fee, reason
                )

        pending = self._pending.get(symbol)
        if pending is None or candle.timestamp < pending.submitted_at:
            return
        order = pending.order
        assert order.price is not None
        touched = candle.low <= order.price if order.side == "BUY" else candle.high >= order.price
        if not touched:
            return
        if order.reduce_only and symbol not in self._positions:
            del self._pending[symbol]
            return
        marketable_at_open = (order.side == "BUY" and order.price >= candle.open) or (
            order.side == "SELL" and order.price <= candle.open
        )
        base_price = candle.open if marketable_at_open else order.price
        del self._pending[symbol]
        self._fill(order, base_price, candle.timestamp, leverage=pending.leverage)

        # A resting entry and its target can both lie inside one OHLC bar, but
        # the bar cannot prove the target happened after the entry. Never grant
        # that ambiguous profit. A stop remains conservative and is applied.
        opened = self._positions.get(symbol)
        if opened is not None and not order.reduce_only:
            trigger, reason = self._touched(
                opened, candle, allow_target=marketable_at_open
            )
            if trigger is not None:
                exit_side = "SELL" if opened.side == "LONG" else "BUY"
                fill = self._slipped(trigger, exit_side)
                fee = fill * opened.quantity * self.config.fee_rate
                self.cash -= fee
                self._book(
                    symbol, opened, opened.quantity, fill, candle.timestamp, fee, reason
                )

    @staticmethod
    def _touched(
        position: _Position, candle: Candle, *, allow_target: bool = True
    ) -> tuple[Decimal | None, str]:
        stop, target = position.stop_loss, position.take_profit
        if position.side == "LONG":
            stopped = candle.low <= stop
            took = target is not None and candle.high >= target
        else:
            stopped = candle.high >= stop
            took = target is not None and candle.low <= target
        # A single candle cannot say which trigger came first, so the stop wins.
        # Preferring the take profit would quietly turn every ambiguous bar into
        # a winner and inflate every number downstream of it.
        if stopped:
            return stop, "stop_loss"
        if took and allow_target:
            return target, "take_profit"
        return None, ""

    def close_all(self, marks: dict[str, Decimal], when: datetime) -> int:
        """Flatten open positions and return how many pending orders were cancelled."""

        for symbol in list(self._positions):
            position = self._positions[symbol]
            mark = marks.get(symbol, position.entry_price)
            exit_side = "SELL" if position.side == "LONG" else "BUY"
            fill = self._slipped(mark, exit_side)
            fee = fill * position.quantity * self.config.fee_rate
            self.cash -= fee
            self._book(symbol, position, position.quantity, fill, when, fee, "run_end")
        cancelled_pending_orders = len(self._pending)
        self._pending.clear()
        return cancelled_pending_orders


def summarize(
    config: BacktestConfig,
    trades: list[BacktestTrade],
    curve: list[EquityPoint],
    *,
    cancelled_pending_orders: int = 0,
) -> BacktestResult:
    final = curve[-1].equity if curve else config.initial_equity
    gross_price_pnl = sum(
        (trade.net_pnl + trade.fees + trade.funding for trade in trades),
        Decimal("0"),
    )
    wins = [trade for trade in trades if trade.net_pnl > 0]
    gross_win = sum((trade.net_pnl for trade in wins), Decimal("0"))
    gross_loss = -sum(
        (trade.net_pnl for trade in trades if trade.net_pnl < 0), Decimal("0")
    )
    peak = config.initial_equity
    drawdown = Decimal("0")
    for point in curve:
        peak = max(peak, point.equity)
        if peak > 0:
            drawdown = max(drawdown, (peak - point.equity) / peak)
    symbol_results: list[BacktestSymbolResult] = []
    for symbol in sorted({trade.symbol for trade in trades}):
        symbol_trades = [trade for trade in trades if trade.symbol == symbol]
        symbol_net = sum((trade.net_pnl for trade in symbol_trades), Decimal("0"))
        symbol_fees = sum((trade.fees for trade in symbol_trades), Decimal("0"))
        symbol_funding = sum((trade.funding for trade in symbol_trades), Decimal("0"))
        symbol_results.append(
            BacktestSymbolResult(
                symbol=symbol,
                gross_price_pnl=symbol_net + symbol_fees + symbol_funding,
                net_pnl=symbol_net,
                contribution_return=(symbol_net / config.initial_equity)
                if config.initial_equity
                else Decimal("0"),
                trade_count=len(symbol_trades),
                total_fees=symbol_fees,
                total_funding=symbol_funding,
            )
        )
    return BacktestResult(
        initial_equity=config.initial_equity,
        final_equity=final,
        gross_price_pnl=gross_price_pnl,
        net_pnl=final - config.initial_equity,
        total_return=(final / config.initial_equity) - 1
        if config.initial_equity
        else Decimal("0"),
        max_drawdown=drawdown,
        win_rate=Decimal(len(wins)) / Decimal(len(trades)) if trades else Decimal("0"),
        # None, not zero: no losses is an undefined ratio, and zero would read
        # as the worst possible score for what may be a flawless run.
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
        trade_count=len(trades),
        total_fees=sum((trade.fees for trade in trades), Decimal("0")),
        total_funding=sum((trade.funding for trade in trades), Decimal("0")),
        run_end_trade_count=sum(trade.exit_reason == "run_end" for trade in trades),
        cancelled_pending_orders=cancelled_pending_orders,
        symbol_results=symbol_results,
        trades=trades,
        equity_curve=curve,
    )


def utc_now() -> datetime:
    return datetime.now(UTC)
