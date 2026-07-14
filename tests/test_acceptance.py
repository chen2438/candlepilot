import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from candlepilot.application.acceptance import evaluate_acceptance
from candlepilot.domain.models import ExecutionReport, RiskDecision, TradeIntent
from candlepilot.providers.base import ProviderResult
from candlepilot.storage.database import AuditRepository, Database

START = datetime(2026, 1, 1, tzinfo=UTC)


def _execution(cid: str, symbol: str, offset_hours: float) -> dict:
    return {
        "client_order_id": cid,
        "symbol": symbol,
        "status": "FILLED",
        "created_at": START + timedelta(hours=offset_hours),
    }


def _risk(symbol: str, offset_hours: float, *, inference_id: int | None, accepted: bool = True) -> dict:
    return {
        "symbol": symbol,
        "accepted": accepted,
        "inference_id": inference_id,
        "created_at": START + timedelta(hours=offset_hours),
    }


def _checks(report) -> dict[str, bool]:
    return {check.name: check.passed for check in report.checks}


def test_clean_soak_run_passes() -> None:
    report = evaluate_acceptance(
        executions=[_execution("cp-1", "BTCUSDT", 0), _execution("cp-2", "ETHUSDT", 25)],
        risk_decisions=[
            _risk("BTCUSDT", 0, inference_id=1),
            _risk("ETHUSDT", 25, inference_id=2),
        ],
        inference_ids={1, 2},
        reconciliation={"unprotected_symbols": []},
    )
    assert report.passed is True
    assert report.observed_hours == 25.0
    assert all(_checks(report).values())


def test_insufficient_runtime_fails_without_false_pass() -> None:
    report = evaluate_acceptance(
        executions=[_execution("cp-1", "BTCUSDT", 0), _execution("cp-2", "BTCUSDT", 5)],
        risk_decisions=[_risk("BTCUSDT", 0, inference_id=1)],
        inference_ids={1},
        reconciliation={"unprotected_symbols": []},
    )
    assert report.passed is False
    assert _checks(report)["continuous_runtime"] is False


def test_empty_audit_trail_never_passes() -> None:
    report = evaluate_acceptance(
        executions=[],
        risk_decisions=[],
        inference_ids=set(),
        reconciliation=None,
    )
    assert report.passed is False
    assert _checks(report)["continuous_runtime"] is False
    assert _checks(report)["positions_reconciled"] is False


def test_duplicate_client_order_id_fails() -> None:
    report = evaluate_acceptance(
        executions=[_execution("cp-dup", "BTCUSDT", 0), _execution("cp-dup", "BTCUSDT", 25)],
        risk_decisions=[_risk("BTCUSDT", 0, inference_id=1)],
        inference_ids={1},
        reconciliation={"unprotected_symbols": []},
    )
    assert _checks(report)["unique_client_order_ids"] is False
    assert report.passed is False


def test_missing_reconciliation_fails() -> None:
    report = evaluate_acceptance(
        executions=[_execution("cp-1", "BTCUSDT", 0), _execution("cp-2", "BTCUSDT", 25)],
        risk_decisions=[_risk("BTCUSDT", 0, inference_id=1)],
        inference_ids={1},
        reconciliation=None,
    )
    assert _checks(report)["positions_reconciled"] is False


def test_unprotected_positions_fail_reconciliation() -> None:
    report = evaluate_acceptance(
        executions=[_execution("cp-1", "BTCUSDT", 0), _execution("cp-2", "BTCUSDT", 25)],
        risk_decisions=[_risk("BTCUSDT", 0, inference_id=1)],
        inference_ids={1},
        reconciliation={"unprotected_symbols": ["BTCUSDT"]},
    )
    assert _checks(report)["positions_reconciled"] is False


def test_untraceable_execution_fails() -> None:
    report = evaluate_acceptance(
        executions=[_execution("cp-1", "BTCUSDT", 0), _execution("cp-2", "SOLUSDT", 25)],
        risk_decisions=[_risk("BTCUSDT", 0, inference_id=1)],
        inference_ids={1},
        reconciliation={"unprotected_symbols": []},
    )
    assert _checks(report)["trade_traceability"] is False
    assert report.passed is False


def test_risk_decision_referencing_missing_inference_fails() -> None:
    report = evaluate_acceptance(
        executions=[_execution("cp-1", "BTCUSDT", 0), _execution("cp-2", "BTCUSDT", 25)],
        risk_decisions=[_risk("BTCUSDT", 0, inference_id=99)],
        inference_ids={1},
        reconciliation={"unprotected_symbols": []},
    )
    assert _checks(report)["trade_traceability"] is False


def test_windowed_audit_queries_feed_the_evaluator(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'acceptance.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "soak")
        inference_id = await repository.record_inference(
            ProviderResult(
                intent=intent,
                provider="codex-auth",
                model="test-model",
                duration=timedelta(milliseconds=10),
                raw_output=intent.model_dump_json(),
                usage={},
            )
        )
        await repository.record_risk(
            "BTCUSDT",
            RiskDecision(accepted=True, reason="within limits", max_quantity=Decimal("1")),
            inference_id=inference_id,
        )
        await repository.record_execution(
            "BTCUSDT",
            ExecutionReport(client_order_id="cp-soak-1", status="FILLED", average_price=Decimal("100")),
        )
        start = datetime.now(UTC) - timedelta(hours=1)
        end = datetime.now(UTC) + timedelta(hours=1)
        executions = await repository.executions_between(start, end)
        risk = await repository.risk_decisions_between(start, end)
        ids = await repository.inference_ids_between(start, end)
        await database.close()
        return executions, risk, ids

    executions, risk, ids = asyncio.run(scenario())
    assert [item["client_order_id"] for item in executions] == ["cp-soak-1"]
    assert risk[0]["inference_id"] in ids
    # A real one-hour window is far short of 24h, so the run must not pass.
    report = evaluate_acceptance(
        executions=executions,
        risk_decisions=risk,
        inference_ids=ids,
        reconciliation={"unprotected_symbols": []},
    )
    assert _checks(report)["unique_client_order_ids"] is True
    assert _checks(report)["trade_traceability"] is True
    assert _checks(report)["continuous_runtime"] is False
    assert report.passed is False
