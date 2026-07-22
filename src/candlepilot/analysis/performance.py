from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any


PRICED_OUTCOME_STATUSES = frozenset(
    {"stopped", "target2", "breakeven_after_target1"}
)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _trade_multiples(record: Mapping[str, Any]) -> tuple[Decimal, Decimal] | None:
    result = record.get("result")
    outcome = record.get("outcome")
    if not isinstance(result, Mapping) or not isinstance(outcome, Mapping):
        return None
    if result.get("direction") not in {"long", "short"}:
        return None
    if outcome.get("status") not in PRICED_OUTCOME_STATUSES:
        return None
    plan = result.get("entry_plan")
    if not isinstance(plan, Mapping):
        return None

    entry = _decimal(plan["entry"])
    stop = _decimal(plan["stop"])
    target1 = _decimal(plan["target1"])
    target2 = _decimal(plan["target2"])
    status = str(outcome["status"])
    if status == "stopped":
        weighted_exit = stop
    elif status == "target2":
        weighted_exit = (target1 + target2) / Decimal(2)
    else:
        weighted_exit = (target1 + entry) / Decimal(2)

    direction = Decimal(1) if result["direction"] == "long" else Decimal(-1)
    price_pnl = direction * (weighted_exit - entry)
    stop_distance = abs(entry - stop)
    if entry <= 0 or stop_distance <= 0:
        return None
    return price_pnl / entry, price_pnl / stop_distance


def calculate_analysis_performance(
    records: Iterable[Mapping[str, Any]],
    *,
    fixed_notional_usdt: float,
    fixed_risk_usdt: float,
) -> dict[str, Any]:
    notional = _decimal(fixed_notional_usdt)
    risk = _decimal(fixed_risk_usdt)
    returns: list[Decimal] = []
    r_multiples: list[Decimal] = []
    directional_analyses = 0
    ambiguous_results = 0
    open_trades = 0

    for record in records:
        result = record.get("result")
        outcome = record.get("outcome")
        if not isinstance(result, Mapping) or result.get("direction") not in {
            "long",
            "short",
        }:
            continue
        directional_analyses += 1
        if isinstance(outcome, Mapping):
            if outcome.get("status") == "ambiguous":
                ambiguous_results += 1
            elif outcome.get("status") in {"active", "target1_partial"}:
                open_trades += 1
        multiples = _trade_multiples(record)
        if multiples is not None:
            return_fraction, r_multiple = multiples
            returns.append(return_fraction)
            r_multiples.append(r_multiple)

    settled_trades = len(returns)
    wins = sum(value > 0 for value in returns)
    losses = sum(value < 0 for value in returns)
    breakevens = settled_trades - wins - losses
    win_rate = (
        Decimal(wins) / Decimal(settled_trades) * Decimal(100)
        if settled_trades
        else None
    )
    total_return = sum(returns, Decimal(0))
    total_r = sum(r_multiples, Decimal(0))

    def number(value: Decimal | None) -> float | None:
        return None if value is None else float(value)

    return {
        "directional_analyses": directional_analyses,
        "settled_trades": settled_trades,
        "open_trades": open_trades,
        "ambiguous_results": ambiguous_results,
        "wins": wins,
        "losses": losses,
        "breakevens": breakevens,
        "fixed_notional": {
            "amount_per_trade_usdt": number(notional),
            "total_pnl_usdt": number(total_return * notional),
            "average_return_percent": number(
                total_return / Decimal(settled_trades) * Decimal(100)
                if settled_trades
                else None
            ),
            "win_rate_percent": number(win_rate),
        },
        "fixed_risk": {
            "risk_per_trade_usdt": number(risk),
            "total_pnl_usdt": number(total_r * risk),
            "total_r": number(total_r),
            "average_r": number(
                total_r / Decimal(settled_trades) if settled_trades else None
            ),
            "win_rate_percent": number(win_rate),
        },
        "costs_included": False,
    }
