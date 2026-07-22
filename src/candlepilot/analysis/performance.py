from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any


PRICED_OUTCOME_STATUSES = frozenset(
    {"stopped", "target2", "breakeven_after_target1"}
)
RESOLVED_PLAN_WIN_STATUSES = frozenset(
    {"target1_before_entry", "target2", "breakeven_after_target1"}
)
RESOLVED_PLAN_LOSS_STATUSES = frozenset({"stopped_before_entry", "stopped"})
UNENTERED_PLAN_STATUSES = frozenset(
    {"target1_before_entry", "stopped_before_entry"}
)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _trade_multiples(
    record: Mapping[str, Any], *, include_unentered: bool = False
) -> tuple[Decimal, Decimal] | None:
    result = record.get("result")
    outcome = record.get("outcome")
    if not isinstance(result, Mapping) or not isinstance(outcome, Mapping):
        return None
    if result.get("direction") not in {"long", "short"}:
        return None
    status = str(outcome.get("status"))
    priced_statuses = (
        PRICED_OUTCOME_STATUSES | UNENTERED_PLAN_STATUSES
        if include_unentered
        else PRICED_OUTCOME_STATUSES
    )
    if status not in priced_statuses:
        return None
    plan = result.get("entry_plan")
    if not isinstance(plan, Mapping):
        return None

    if status in UNENTERED_PLAN_STATUSES:
        completion_entry = outcome.get("completion_entry_price")
        if completion_entry is None:
            return None
        entry = _decimal(completion_entry)
    else:
        entry = _decimal(plan["entry"])
    stop = _decimal(plan["stop"])
    target1 = _decimal(plan["target1"])
    target2 = _decimal(plan["target2"])
    if status in {"stopped", "stopped_before_entry"}:
        weighted_exit = stop
    elif status == "target1_before_entry":
        weighted_exit = target1
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
    all_plan_returns: list[Decimal] = []
    all_plan_r_multiples: list[Decimal] = []
    directional_analyses = 0
    ambiguous_results = 0
    open_trades = 0
    resolved_plan_wins = 0
    resolved_plan_losses = 0
    resolved_unentered_plans = 0

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
            outcome_status = outcome.get("status")
            if outcome_status == "ambiguous":
                ambiguous_results += 1
            elif outcome_status in {"active", "target1_partial"}:
                open_trades += 1
            if outcome_status in RESOLVED_PLAN_WIN_STATUSES:
                resolved_plan_wins += 1
            elif outcome_status in RESOLVED_PLAN_LOSS_STATUSES:
                resolved_plan_losses += 1
            if outcome_status in UNENTERED_PLAN_STATUSES:
                resolved_unentered_plans += 1
        multiples = _trade_multiples(record)
        if multiples is not None:
            return_fraction, r_multiple = multiples
            returns.append(return_fraction)
            r_multiples.append(r_multiple)
        all_plan_multiples = _trade_multiples(record, include_unentered=True)
        if all_plan_multiples is not None:
            return_fraction, r_multiple = all_plan_multiples
            all_plan_returns.append(return_fraction)
            all_plan_r_multiples.append(r_multiple)

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
    all_plan_total_return = sum(all_plan_returns, Decimal(0))
    all_plan_total_r = sum(all_plan_r_multiples, Decimal(0))
    resolved_plans = resolved_plan_wins + resolved_plan_losses
    resolved_plan_win_rate = (
        Decimal(resolved_plan_wins) / Decimal(resolved_plans) * Decimal(100)
        if resolved_plans
        else None
    )

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
        "all_plans": {
            "resolved_plans": resolved_plans,
            "entered_plans": resolved_plans - resolved_unentered_plans,
            "unentered_plans": resolved_unentered_plans,
            "wins": resolved_plan_wins,
            "losses": resolved_plan_losses,
            "win_rate_percent": number(resolved_plan_win_rate),
            "priced_plans": len(all_plan_returns),
            "fixed_notional_total_pnl_usdt": number(all_plan_total_return * notional),
            "fixed_notional_average_return_percent": number(
                all_plan_total_return / Decimal(len(all_plan_returns)) * Decimal(100)
                if all_plan_returns
                else None
            ),
            "fixed_risk_total_pnl_usdt": number(all_plan_total_r * risk),
            "fixed_risk_total_r": number(all_plan_total_r),
            "fixed_risk_average_r": number(
                all_plan_total_r / Decimal(len(all_plan_r_multiples))
                if all_plan_r_multiples
                else None
            ),
        },
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
