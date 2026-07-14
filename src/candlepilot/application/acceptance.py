from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

REQUIRED_RUNTIME_HOURS = 24.0


@dataclass(frozen=True, slots=True)
class AcceptanceCheck:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True, slots=True)
class AcceptanceReport:
    passed: bool
    required_hours: float
    observed_hours: float
    window_start: datetime | None
    window_end: datetime | None
    execution_count: int
    inference_count: int
    risk_decision_count: int
    checks: tuple[AcceptanceCheck, ...]


def evaluate_acceptance(
    *,
    executions: Sequence[Mapping[str, Any]],
    risk_decisions: Sequence[Mapping[str, Any]],
    inference_ids: Iterable[int],
    reconciliation: Mapping[str, Any] | None,
    required_hours: float = REQUIRED_RUNTIME_HOURS,
) -> AcceptanceReport:
    """Judge a testnet soak run against auditable release invariants.

    The evaluator is deliberately conservative: it only reports ``passed`` when
    every invariant is positively confirmed. Missing evidence (too little
    audited runtime, no reconciliation snapshot) fails the run rather than
    passing it silently, so the tool can never falsely mark acceptance.
    """

    inference_id_set = set(inference_ids)
    checks: list[AcceptanceCheck] = []

    timestamps = [record["created_at"] for record in executions]
    timestamps += [record["created_at"] for record in risk_decisions]
    if timestamps:
        window_start: datetime | None = min(timestamps)
        window_end: datetime | None = max(timestamps)
        observed_hours = (window_end - window_start).total_seconds() / 3600
    else:
        window_start = window_end = None
        observed_hours = 0.0
    checks.append(
        AcceptanceCheck(
            "continuous_runtime",
            observed_hours >= required_hours,
            f"observed {observed_hours:.2f}h of audited activity; "
            f"requires >= {required_hours:.2f}h",
        )
    )

    order_ids = [record["client_order_id"] for record in executions]
    duplicates = sorted(cid for cid, count in Counter(order_ids).items() if count > 1)
    checks.append(
        AcceptanceCheck(
            "unique_client_order_ids",
            not duplicates,
            "no duplicate client order ids"
            if not duplicates
            else f"duplicate client order ids: {', '.join(duplicates)}",
        )
    )

    if reconciliation is None:
        checks.append(
            AcceptanceCheck(
                "positions_reconciled",
                False,
                "no testnet reconciliation snapshot; cannot confirm positions are reconciled",
            )
        )
    else:
        unprotected = tuple(reconciliation.get("unprotected_symbols", ()))
        checks.append(
            AcceptanceCheck(
                "positions_reconciled",
                not unprotected,
                "all testnet positions reconciled with protective stops"
                if not unprotected
                else f"unreconciled or unprotected symbols: {', '.join(unprotected)}",
            )
        )

    dangling = sorted(
        {
            str(record["inference_id"])
            for record in risk_decisions
            if record.get("inference_id") is not None
            and record["inference_id"] not in inference_id_set
        }
    )
    traced_symbols = {
        record["symbol"]
        for record in risk_decisions
        if record.get("accepted") and record.get("inference_id") in inference_id_set
    }
    untraced = sorted({record["symbol"] for record in executions} - traced_symbols)
    if not dangling and not untraced:
        trace_detail = "every execution traces to an audited model output and accepted risk decision"
    else:
        parts: list[str] = []
        if dangling:
            parts.append(f"risk decisions referencing missing inferences: {', '.join(dangling)}")
        if untraced:
            parts.append(f"executions without a traced model and risk decision: {', '.join(untraced)}")
        trace_detail = "; ".join(parts)
    checks.append(
        AcceptanceCheck("trade_traceability", not dangling and not untraced, trace_detail)
    )

    return AcceptanceReport(
        passed=all(check.passed for check in checks),
        required_hours=required_hours,
        observed_hours=observed_hours,
        window_start=window_start,
        window_end=window_end,
        execution_count=len(executions),
        inference_count=len(inference_id_set),
        risk_decision_count=len(risk_decisions),
        checks=tuple(checks),
    )
