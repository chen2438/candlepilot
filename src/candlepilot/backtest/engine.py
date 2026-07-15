from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from candlepilot.backtest.regime import UNKNOWN, RegimeClassifier
from candlepilot.domain.models import TradeAction, TradeIntent


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
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("candle prices must be positive")
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError("invalid OHLC range")


@dataclass(frozen=True, slots=True)
class ReplayIntent:
    decided_at: datetime
    intent: TradeIntent


@dataclass(frozen=True, slots=True)
class BacktestConfig:
    initial_equity: Decimal = Decimal("10000")
    fee_rate: Decimal = Decimal("0.0005")
    slippage_fraction: Decimal = Decimal("0.0005")
    max_risk_fraction: Decimal = Decimal("0.02")
    max_margin_fraction: Decimal = Decimal("0.60")


@dataclass(frozen=True, slots=True)
class BacktestTrade:
    side: str
    quantity: Decimal
    entry_time: datetime
    entry_price: Decimal
    exit_time: datetime
    exit_price: Decimal
    gross_pnl: Decimal
    fees: Decimal
    funding: Decimal
    net_pnl: Decimal
    exit_reason: str
    regime: str = UNKNOWN


@dataclass(frozen=True, slots=True)
class EquityPoint:
    timestamp: datetime
    equity: Decimal


@dataclass(frozen=True, slots=True)
class TradeGroupStats:
    trade_count: int
    win_rate: Decimal
    net_pnl: Decimal
    average_net_pnl: Decimal
    profit_factor: Decimal | None


@dataclass(frozen=True, slots=True)
class BacktestResult:
    initial_equity: Decimal
    final_equity: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    win_rate: Decimal
    profit_factor: Decimal | None
    sharpe_ratio: Decimal | None
    sortino_ratio: Decimal | None
    payoff_ratio: Decimal | None
    turnover: Decimal
    exposure_fraction: Decimal
    grouped_stats: dict[str, dict[str, TradeGroupStats]]
    total_fees: Decimal
    total_funding: Decimal
    trades: tuple[BacktestTrade, ...]
    equity_curve: tuple[EquityPoint, ...]


@dataclass(slots=True)
class _Position:
    side: str
    quantity: Decimal
    leverage: int
    entry_time: datetime
    entry_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal | None
    entry_fee: Decimal
    regime: str = UNKNOWN
    funding: Decimal = Decimal("0")


class BacktestEngine:
    """Single-symbol event engine with next-bar execution and conservative fills."""

    def __init__(
        self,
        config: BacktestConfig | None = None,
        regime_classifier: RegimeClassifier | None = None,
    ) -> None:
        self.config = config or BacktestConfig()
        self.regime_classifier = regime_classifier or RegimeClassifier()

    def run(self, candles: list[Candle], decisions: list[ReplayIntent]) -> BacktestResult:
        if not candles:
            raise ValueError("backtest requires at least one candle")
        ordered_candles = sorted(candles, key=lambda item: item.timestamp)
        if len({item.timestamp for item in ordered_candles}) != len(ordered_candles):
            raise ValueError("candle timestamps must be unique")
        regime_labels = self.regime_classifier.labels(
            [candle.close for candle in ordered_candles]
        )
        regime_by_time = {
            candle.timestamp: label
            for candle, label in zip(ordered_candles, regime_labels)
        }
        decision_map = {decision.decided_at: decision.intent for decision in decisions}
        if len(decision_map) != len(decisions):
            raise ValueError("only one replay intent per candle is supported")

        cash = self.config.initial_equity
        position: _Position | None = None
        pending: TradeIntent | None = None
        trades: list[BacktestTrade] = []
        curve: list[EquityPoint] = []

        for candle in ordered_candles:
            if pending is not None:
                cash, position, completed = self._execute_pending(
                    pending, candle, cash, position, regime_by_time.get(candle.timestamp, UNKNOWN)
                )
                if completed is not None:
                    trades.append(completed)

            if position is not None:
                funding = (
                    position.quantity
                    * candle.close
                    * candle.funding_rate
                    * (Decimal("-1") if position.side == "LONG" else Decimal("1"))
                )
                cash += funding
                position.funding += funding
                exit_price, exit_reason = self._intrabar_exit(position, candle)
                if exit_price is not None:
                    cash, trade = self._close(position, candle.timestamp, exit_price, cash, exit_reason)
                    trades.append(trade)
                    position = None

            unrealized = self._unrealized(position, candle.close)
            curve.append(EquityPoint(candle.timestamp, cash + unrealized))
            pending = decision_map.get(candle.timestamp)

        if position is not None:
            last = ordered_candles[-1]
            cash, trade = self._close(position, last.timestamp, last.close, cash, "end_of_data")
            trades.append(trade)
            curve[-1] = EquityPoint(last.timestamp, cash)

        return self._result(cash, trades, curve)

    def _execute_pending(
        self,
        intent: TradeIntent,
        candle: Candle,
        cash: Decimal,
        position: _Position | None,
        regime: str,
    ) -> tuple[Decimal, _Position | None, BacktestTrade | None]:
        if intent.action == TradeAction.HOLD:
            return cash, position, None
        if intent.action == TradeAction.CLOSE and position is not None:
            exit_price = self._slipped(candle.open, "SELL" if position.side == "LONG" else "BUY")
            cash, trade = self._close(position, candle.timestamp, exit_price, cash, "model_exit")
            return cash, None, trade
        if intent.action == TradeAction.REDUCE and position is not None:
            exit_price = self._slipped(
                candle.open, "SELL" if position.side == "LONG" else "BUY"
            )
            cash, trade = self._close_quantity(
                position,
                position.quantity / 2,
                candle.timestamp,
                exit_price,
                cash,
                "model_exit",
            )
            return cash, position, trade
        if intent.action == TradeAction.ADD and position is not None:
            return self._add(intent, candle, cash, position), position, None
        if position is not None or intent.action not in {
            TradeAction.OPEN_LONG,
            TradeAction.OPEN_SHORT,
        }:
            return cash, position, None
        stop = intent.stop_loss
        if stop is None:
            return cash, position, None
        side = "LONG" if intent.action == TradeAction.OPEN_LONG else "SHORT"
        entry = self._slipped(candle.open, "BUY" if side == "LONG" else "SELL")
        if (side == "LONG" and stop >= entry) or (side == "SHORT" and stop <= entry):
            return cash, position, None
        per_unit_loss = abs(entry - stop) + entry * self.config.slippage_fraction
        risk_budget = cash * min(intent.risk_fraction, self.config.max_risk_fraction)
        risk_quantity = risk_budget / per_unit_loss
        margin_quantity = (cash * self.config.max_margin_fraction * intent.leverage) / entry
        quantity = min(risk_quantity, margin_quantity)
        entry_fee = quantity * entry * self.config.fee_rate
        cash -= entry_fee
        return (
            cash,
            _Position(
                side=side,
                quantity=quantity,
                leverage=intent.leverage,
                entry_time=candle.timestamp,
                entry_price=entry,
                stop_loss=stop,
                take_profit=intent.take_profit,
                entry_fee=entry_fee,
                regime=regime,
            ),
            None,
        )

    def _add(
        self,
        intent: TradeIntent,
        candle: Candle,
        cash: Decimal,
        position: _Position,
    ) -> Decimal:
        stop = intent.stop_loss
        if stop is None:
            return cash
        entry_side = "BUY" if position.side == "LONG" else "SELL"
        entry = self._slipped(candle.open, entry_side)
        if (position.side == "LONG" and stop >= entry) or (
            position.side == "SHORT" and stop <= entry
        ):
            return cash
        equity = cash + self._unrealized(position, candle.open)
        per_unit_loss = abs(entry - stop) + entry * self.config.slippage_fraction
        risk_budget = equity * min(intent.risk_fraction, self.config.max_risk_fraction)
        risk_quantity = risk_budget / per_unit_loss
        existing_margin = position.quantity * candle.open / position.leverage
        remaining_margin = max(
            Decimal("0"), equity * self.config.max_margin_fraction - existing_margin
        )
        margin_quantity = remaining_margin * intent.leverage / entry
        quantity = min(risk_quantity, margin_quantity)
        if quantity <= 0:
            return cash
        entry_fee = quantity * entry * self.config.fee_rate
        total = position.quantity + quantity
        position.entry_price = (
            position.entry_price * position.quantity + entry * quantity
        ) / total
        position.quantity = total
        position.leverage = max(position.leverage, intent.leverage)
        position.stop_loss = stop
        if intent.take_profit is not None:
            position.take_profit = intent.take_profit
        position.entry_fee += entry_fee
        return cash - entry_fee

    def _intrabar_exit(self, position: _Position, candle: Candle) -> tuple[Decimal | None, str]:
        # When both stop and target are touched in one candle, use the stop. This is
        # intentionally conservative because sub-candle ordering is unavailable.
        if position.side == "LONG":
            if candle.low <= position.stop_loss:
                return self._slipped(position.stop_loss, "SELL"), "stop_loss"
            if position.take_profit is not None and candle.high >= position.take_profit:
                return self._slipped(position.take_profit, "SELL"), "take_profit"
        else:
            if candle.high >= position.stop_loss:
                return self._slipped(position.stop_loss, "BUY"), "stop_loss"
            if position.take_profit is not None and candle.low <= position.take_profit:
                return self._slipped(position.take_profit, "BUY"), "take_profit"
        return None, ""

    def _close(
        self,
        position: _Position,
        timestamp: datetime,
        exit_price: Decimal,
        cash: Decimal,
        reason: str,
    ) -> tuple[Decimal, BacktestTrade]:
        return self._close_quantity(
            position,
            position.quantity,
            timestamp,
            exit_price,
            cash,
            reason,
        )

    def _close_quantity(
        self,
        position: _Position,
        quantity: Decimal,
        timestamp: datetime,
        exit_price: Decimal,
        cash: Decimal,
        reason: str,
    ) -> tuple[Decimal, BacktestTrade]:
        quantity = min(quantity, position.quantity)
        fraction = quantity / position.quantity
        entry_fee = position.entry_fee * fraction
        funding = position.funding * fraction
        direction = Decimal("1") if position.side == "LONG" else Decimal("-1")
        gross = quantity * (exit_price - position.entry_price) * direction
        exit_fee = quantity * exit_price * self.config.fee_rate
        fees = entry_fee + exit_fee
        net = gross - exit_fee + funding
        cash += gross - exit_fee
        trade = BacktestTrade(
            side=position.side,
            quantity=quantity,
            entry_time=position.entry_time,
            entry_price=position.entry_price,
            exit_time=timestamp,
            exit_price=exit_price,
            gross_pnl=gross,
            fees=fees,
            funding=funding,
            net_pnl=net - entry_fee,
            exit_reason=reason,
            regime=position.regime,
        )
        position.quantity -= quantity
        position.entry_fee -= entry_fee
        position.funding -= funding
        return cash, trade

    def _slipped(self, price: Decimal, side: str) -> Decimal:
        direction = Decimal("1") if side == "BUY" else Decimal("-1")
        return price * (Decimal("1") + direction * self.config.slippage_fraction)

    @staticmethod
    def _unrealized(position: _Position | None, mark: Decimal) -> Decimal:
        if position is None:
            return Decimal("0")
        direction = Decimal("1") if position.side == "LONG" else Decimal("-1")
        return position.quantity * (mark - position.entry_price) * direction

    def _result(
        self,
        final_equity: Decimal,
        trades: list[BacktestTrade],
        curve: list[EquityPoint],
    ) -> BacktestResult:
        peak = curve[0].equity
        max_drawdown = Decimal("0")
        for point in curve:
            peak = max(peak, point.equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - point.equity) / peak)
        wins = [trade.net_pnl for trade in trades if trade.net_pnl > 0]
        losses = [-trade.net_pnl for trade in trades if trade.net_pnl < 0]
        win_rate = Decimal(len(wins)) / Decimal(len(trades)) if trades else Decimal("0")
        gross_profit = sum(wins, Decimal("0"))
        gross_loss = sum(losses, Decimal("0"))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
        payoff_ratio = (
            (gross_profit / Decimal(len(wins))) / (gross_loss / Decimal(len(losses)))
            if wins and losses
            else None
        )
        returns = [
            (current.equity / previous.equity) - Decimal("1")
            for previous, current in zip(curve, curve[1:])
            if previous.equity > 0
        ]
        periods_per_year = self._periods_per_year(curve)
        sharpe_ratio = self._sharpe(returns, periods_per_year)
        sortino_ratio = self._sortino(returns, periods_per_year)
        traded_notional = sum(
            (
                trade.quantity * trade.entry_price
                + trade.quantity * trade.exit_price
                for trade in trades
            ),
            Decimal("0"),
        )
        exposed_points = sum(
            any(trade.entry_time <= point.timestamp < trade.exit_time for trade in trades)
            for point in curve
        )
        return BacktestResult(
            initial_equity=self.config.initial_equity,
            final_equity=final_equity,
            total_return=(final_equity / self.config.initial_equity) - Decimal("1"),
            max_drawdown=max_drawdown,
            win_rate=win_rate,
            profit_factor=profit_factor,
            sharpe_ratio=sharpe_ratio,
            sortino_ratio=sortino_ratio,
            payoff_ratio=payoff_ratio,
            turnover=traded_notional / self.config.initial_equity,
            exposure_fraction=(
                Decimal(exposed_points) / Decimal(len(curve)) if curve else Decimal("0")
            ),
            grouped_stats={
                "side": self._group_trades(trades, "side"),
                "exit_reason": self._group_trades(trades, "exit_reason"),
                "regime": self._group_trades(trades, "regime"),
            },
            total_fees=sum((trade.fees for trade in trades), Decimal("0")),
            total_funding=sum((trade.funding for trade in trades), Decimal("0")),
            trades=tuple(trades),
            equity_curve=tuple(curve),
        )

    @staticmethod
    def _group_trades(
        trades: list[BacktestTrade], attribute: str
    ) -> dict[str, TradeGroupStats]:
        grouped: dict[str, list[BacktestTrade]] = {}
        for trade in trades:
            grouped.setdefault(str(getattr(trade, attribute)), []).append(trade)
        results: dict[str, TradeGroupStats] = {}
        for name, items in grouped.items():
            wins = [trade.net_pnl for trade in items if trade.net_pnl > 0]
            losses = [-trade.net_pnl for trade in items if trade.net_pnl < 0]
            gross_profit = sum(wins, Decimal("0"))
            gross_loss = sum(losses, Decimal("0"))
            net_pnl = sum((trade.net_pnl for trade in items), Decimal("0"))
            results[name] = TradeGroupStats(
                trade_count=len(items),
                win_rate=Decimal(len(wins)) / Decimal(len(items)),
                net_pnl=net_pnl,
                average_net_pnl=net_pnl / Decimal(len(items)),
                profit_factor=gross_profit / gross_loss if gross_loss else None,
            )
        return results

    @staticmethod
    def _periods_per_year(curve: list[EquityPoint]) -> Decimal | None:
        intervals = [
            Decimal(str((current.timestamp - previous.timestamp).total_seconds()))
            for previous, current in zip(curve, curve[1:])
            if current.timestamp > previous.timestamp
        ]
        if not intervals:
            return None
        intervals.sort()
        seconds = intervals[len(intervals) // 2]
        return Decimal(365 * 24 * 60 * 60) / seconds if seconds > 0 else None

    @staticmethod
    def _sharpe(returns: list[Decimal], periods_per_year: Decimal | None) -> Decimal | None:
        if len(returns) < 2 or periods_per_year is None:
            return None
        mean = sum(returns, Decimal("0")) / Decimal(len(returns))
        variance = sum(((item - mean) ** 2 for item in returns), Decimal("0")) / Decimal(
            len(returns) - 1
        )
        return mean / variance.sqrt() * periods_per_year.sqrt() if variance > 0 else None

    @staticmethod
    def _sortino(returns: list[Decimal], periods_per_year: Decimal | None) -> Decimal | None:
        if not returns or periods_per_year is None:
            return None
        mean = sum(returns, Decimal("0")) / Decimal(len(returns))
        downside = (
            sum((min(item, Decimal("0")) ** 2 for item in returns), Decimal("0"))
            / Decimal(len(returns))
        ).sqrt()
        return mean / downside * periods_per_year.sqrt() if downside > 0 else None
