from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal

from candlepilot.backtest.engine import (
    BacktestConfig,
    BacktestEngine,
    BacktestResult,
    Candle,
    EquityPoint,
    ReplayIntent,
)


@dataclass(frozen=True, slots=True)
class PortfolioBacktestResult:
    initial_equity: Decimal
    final_equity: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    total_fees: Decimal
    total_funding: Decimal
    allocation: str
    per_symbol: dict[str, BacktestResult]
    equity_curve: tuple[EquityPoint, ...]


class PortfolioBacktestEngine:
    """Aggregate independent, equally funded symbol sleeves into one portfolio curve."""

    def __init__(self, config: BacktestConfig | None = None) -> None:
        self.config = config or BacktestConfig()

    def run(
        self,
        legs: dict[str, tuple[list[Candle], list[ReplayIntent]]],
    ) -> PortfolioBacktestResult:
        if len(legs) < 2:
            raise ValueError("portfolio backtest requires at least two symbols")
        if any(not candles for candles, _ in legs.values()):
            raise ValueError("every portfolio symbol requires candles")
        sleeve_equity = self.config.initial_equity / Decimal(len(legs))
        sleeve_config = replace(self.config, initial_equity=sleeve_equity)
        per_symbol = {
            symbol: BacktestEngine(sleeve_config).run(candles, decisions)
            for symbol, (candles, decisions) in sorted(legs.items())
        }
        curve = self._aggregate_curve(per_symbol, sleeve_equity)
        final_equity = sum(
            (result.final_equity for result in per_symbol.values()), Decimal("0")
        )
        return PortfolioBacktestResult(
            initial_equity=self.config.initial_equity,
            final_equity=final_equity,
            total_return=(final_equity / self.config.initial_equity) - Decimal("1"),
            max_drawdown=self._max_drawdown(curve),
            total_fees=sum(
                (result.total_fees for result in per_symbol.values()), Decimal("0")
            ),
            total_funding=sum(
                (result.total_funding for result in per_symbol.values()), Decimal("0")
            ),
            allocation="equal_weight_sleeves",
            per_symbol=per_symbol,
            equity_curve=tuple(curve),
        )

    @staticmethod
    def _aggregate_curve(
        per_symbol: dict[str, BacktestResult], sleeve_equity: Decimal
    ) -> list[EquityPoint]:
        timestamps = sorted(
            {
                point.timestamp
                for result in per_symbol.values()
                for point in result.equity_curve
            }
        )
        values = {symbol: sleeve_equity for symbol in per_symbol}
        points = {
            symbol: {point.timestamp: point.equity for point in result.equity_curve}
            for symbol, result in per_symbol.items()
        }
        curve: list[EquityPoint] = []
        for timestamp in timestamps:
            for symbol in per_symbol:
                values[symbol] = points[symbol].get(timestamp, values[symbol])
            curve.append(EquityPoint(timestamp, sum(values.values(), Decimal("0"))))
        return curve

    @staticmethod
    def _max_drawdown(curve: list[EquityPoint]) -> Decimal:
        peak = curve[0].equity
        drawdown = Decimal("0")
        for point in curve:
            peak = max(peak, point.equity)
            if peak > 0:
                drawdown = max(drawdown, (peak - point.equity) / peak)
        return drawdown
