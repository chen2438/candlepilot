import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from candlepilot.broker.user_stream import UserStreamEvent
from candlepilot.domain.models import (
    ExecutionAttempt,
    ExecutionReport,
    RiskDecision,
    StructureAssessment,
    StructureCheck,
    TradeAction,
    TradeIntent,
    MarketSnapshot,
    PortfolioState,
)
from candlepilot.providers.base import ProviderResult
from candlepilot.providers.pricing import parse_models_dev
from candlepilot.runtime_lock import ServiceInstanceLock
from candlepilot.storage.database import (
    CURRENT_SCHEMA_VERSION,
    DECISION_OUTCOMES,
    AuditRepository,
    Database,
    UserStreamEventRow,
)


def test_inference_audit_round_trip(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "test")
        identifier = await repository.record_inference(
            ProviderResult(
                intent=intent,
                provider="codex-auth",
                model="test-model",
                duration=timedelta(milliseconds=123),
                raw_output=intent.model_dump_json(),
                usage={"input_tokens": 10, "output_tokens": 2, "total_tokens": 12},
                prompt_version="trade-intent-v1",
                data_version="market-snapshot-v1:sha256:test",
                provider_version="codex-cli 1.0",
                input_payload={
                    "market": {"symbol": "BTCUSDT"},
                    "portfolio": {"equity": "1"},
                },
                prompt="fixture prompt",
            )
        )
        rows = await repository.recent_intents()
        detail = await repository.decision_detail(identifier)
        replay_rows = await repository.intents_between(
            "BTCUSDT",
            "5m",
            datetime.now(UTC) - timedelta(minutes=1),
            datetime.now(UTC) + timedelta(minutes=1),
        )
        await database.close()
        return identifier, rows, replay_rows, detail

    identifier, rows, replay_rows, detail = asyncio.run(scenario())
    assert identifier == 1
    assert rows[0]["provider"] == "codex-auth"
    assert rows[0]["intent"]["symbol"] == "BTCUSDT"
    assert rows[0]["duration_ms"] == 123
    assert rows[0]["provenance"]["prompt_version"] == "trade-intent-v1"
    assert replay_rows[0]["model"] == "test-model"
    assert replay_rows[0]["intent"].symbol == "BTCUSDT"
    assert detail is not None
    assert detail["decision_duration_ms"] >= 123
    assert detail["input"]["market"]["symbol"] == "BTCUSDT"
    assert detail["prompt"] == "fixture prompt"
    assert detail["audit_status"] == "complete"
    assert '"symbol":"BTCUSDT"' in detail["raw_output"]
    assert detail["usage"]["input_tokens"] == 10


def test_recent_stop_loss_times_returns_latest_candlepilot_fill(tmp_path: Path) -> None:
    async def scenario() -> dict[str, datetime]:
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'stops.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        now = datetime.now(UTC)
        for minutes, client_id, symbol in (
            (1, "cp-first-sl", "BTCUSDT"),
            (2, "manual-stop", "ETHUSDT"),
            (3, "cp-latest-sl", "BTCUSDT"),
        ):
            event_time = now + timedelta(minutes=minutes)
            await repository.record_user_event(
                UserStreamEvent(
                    "ORDER_TRADE_UPDATE",
                    event_time,
                    event_time,
                    symbol,
                    {
                        "o": {
                            "c": client_id,
                            "s": symbol,
                            "x": "TRADE",
                            "X": "FILLED",
                        }
                    },
                )
            )
        result = await repository.recent_stop_loss_times(now)
        await database.close()
        return result

    result = asyncio.run(scenario())

    assert set(result) == {"BTCUSDT"}
    assert result["BTCUSDT"].tzinfo is not None


def test_live_decision_snapshots_are_independent_replay_inputs(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'replay.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        portfolio = PortfolioState(equity="10000", available_balance="10000")
        run_id = await repository.create_live_run(
            {"initial_portfolio": portfolio.model_dump(mode="json")}
        )
        snapshot = MarketSnapshot(
            symbol="BTCUSDT",
            cadence="5m",
            timestamp=datetime.now(UTC),
            mark_price="100",
            bid="99.9",
            ask="100.1",
            quote_volume_24h="1",
            features={"5m_ema_20": 99.0},
        )
        await repository.record_live_decision_snapshots(
            run_id,
            [
                {
                    "symbol": snapshot.symbol,
                    "batch_id": "00000000-0000-0000-0000-000000000001",
                    "cadence": snapshot.cadence,
                    "captured_at": snapshot.timestamp,
                    "market": snapshot.model_dump(mode="json"),
                    "portfolio": portfolio.model_dump(mode="json"),
                    "rules": {
                        "quantity_step": "0.001",
                        "min_quantity": "0.001",
                        "min_notional": "5",
                        "tick_size": "0.01",
                    },
                }
            ],
        )
        rows = await repository.live_decision_snapshots(run_id)
        runs = await repository.replayable_live_runs()
        await database.close()
        return rows, runs

    rows, runs = asyncio.run(scenario())
    assert rows[0]["market"]["features"]["5m_ema_20"] == 99.0
    assert rows[0]["portfolio"]["equity"] == "10000"
    assert runs[0]["snapshot_count"] == 1
    assert runs[0]["symbols"] == ["BTCUSDT"]


def test_live_runs_group_inferences_and_record_terminal_reason(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'live-runs.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        run_id = await repository.create_live_run(
            {"provider_chain": ["codex-auth"], "cadences": ["15m"]}
        )
        intent = TradeIntent.hold("BTCUSDT", "15m", "grouped")
        inference_id = await repository.record_inference(
            ProviderResult(
                intent=intent,
                provider="codex-auth",
                model="test-model",
                duration=timedelta(milliseconds=20),
                raw_output=intent.model_dump_json(),
                usage={},
            ),
            live_run_id=run_id,
        )
        await repository.finish_live_run(
            run_id,
            status="auto_stopped",
            stop_reason="run duration limit reached (60s)",
        )
        events = await repository.recent_decision_events()
        performance = await repository.recent_live_run_performance()

        stale_id = await repository.create_live_run({"provider_chain": ["slow"]})
        interrupted = await repository.interrupt_open_live_runs()
        stale_intent = TradeIntent.hold("ETHUSDT", "15m", "legacy")
        legacy_id = await repository.record_inference(
            ProviderResult(
                intent=stale_intent,
                provider="local-trend-v1",
                model="trend-v1",
                duration=timedelta(),
                raw_output=stale_intent.model_dump_json(),
                usage={},
            )
        )
        legacy = await repository.decision_detail(legacy_id)
        await database.close()
        return run_id, inference_id, events, performance, stale_id, interrupted, legacy

    run_id, inference_id, events, performance, stale_id, interrupted, legacy = (
        asyncio.run(scenario())
    )
    assert events[0]["id"] == inference_id
    assert events[0]["live_run_id"] == run_id
    assert events[0]["live_run"]["status"] == "auto_stopped"
    assert events[0]["live_run"]["stop_reason"] == "run duration limit reached (60s)"
    assert events[0]["live_run"]["config"]["cadences"] == ["15m"]
    assert performance[0]["total_pnl"] == "0"
    assert performance[0]["open_position_count"] == 0
    assert performance[0]["win_rate"] is None
    assert stale_id > run_id
    assert interrupted == 1
    assert legacy is not None
    assert legacy["live_run_id"] is None
    assert legacy["live_run"] is None


def test_service_instance_lock_rejects_a_second_owner(tmp_path: Path) -> None:
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'owned.db'}"
    first = ServiceInstanceLock(database_url)
    second = ServiceInstanceLock(database_url)

    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="another CandlePilot service"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_inference_audit_distinguishes_partial_and_unavailable_details(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'audit-status.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "test")
        partial_id = await repository.record_inference(
            ProviderResult(
                intent,
                "claude-code-auth",
                None,
                timedelta(0),
                "error",
                {"error": "ProviderError"},
                input_payload={"market": {"symbol": "BTCUSDT"}},
            )
        )
        unavailable_id = await repository.record_inference(
            ProviderResult(
                intent,
                "claude-code-auth",
                None,
                timedelta(0),
                "legacy",
                {},
            )
        )
        partial = await repository.decision_detail(partial_id)
        unavailable = await repository.decision_detail(unavailable_id)
        await database.close()
        return partial, unavailable

    partial, unavailable = asyncio.run(scenario())
    assert partial is not None and partial["audit_status"] == "partial"
    assert unavailable is not None and unavailable["audit_status"] == "unavailable"


def test_execution_and_risk_queries_filter_and_order(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'queries.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        await repository.record_execution(
            "BTCUSDT",
            ExecutionReport(
                client_order_id="cp-1", status="FILLED", average_price=Decimal("100")
            ),
        )
        await repository.record_execution(
            "ETHUSDT",
            ExecutionReport(client_order_id="cp-2", status="NEW"),
        )
        intent = TradeIntent.hold("ETHUSDT", "5m", "risk link")
        inference_id = await repository.record_inference(
            ProviderResult(
                intent=intent,
                provider="codex-auth",
                model="m",
                duration=timedelta(milliseconds=1),
                raw_output=intent.model_dump_json(),
                usage={},
            )
        )
        await repository.record_risk(
            "BTCUSDT", RiskDecision(accepted=True, reason="within limits")
        )
        await repository.record_risk(
            "ETHUSDT",
            RiskDecision(accepted=False, reason="missing stop"),
            inference_id=inference_id,
        )
        orders = await repository.recent_executions()
        fills = await repository.recent_executions(status="FILLED")
        risk = await repository.recent_risk_decisions()
        rejections = await repository.recent_risk_decisions(accepted=False)
        structure_summary = await repository.structure_gate_summary()
        await database.close()
        return orders, fills, risk, rejections, inference_id, structure_summary

    orders, fills, risk, rejections, inference_id, structure_summary = asyncio.run(
        scenario()
    )
    assert [item["client_order_id"] for item in orders] == ["cp-2", "cp-1"]
    assert [item["status"] for item in fills] == ["FILLED"]
    assert fills[0]["report"]["average_price"] == "100"
    assert risk[0]["symbol"] == "ETHUSDT" and risk[0]["accepted"] is False
    assert len(rejections) == 1
    assert rejections[0]["reason"] == "missing stop"
    assert rejections[0]["inference_id"] == inference_id
    assert structure_summary["sample_size"] == 0
    assert structure_summary["pass_rate"] is None


def test_structure_gate_summary_aggregates_embedded_shadow_checks(
    tmp_path: Path,
) -> None:
    async def scenario() -> dict[str, object]:
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'structure-summary.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        for passed in (True, False):
            await repository.record_risk(
                "BTCUSDT",
                RiskDecision(
                    accepted=True,
                    reason="within limits",
                    structure_assessment=StructureAssessment(
                        mode="shadow",
                        passed=passed,
                        checks=(
                            StructureCheck(
                                key="metadata", passed=True, detail="complete"
                            ),
                            StructureCheck(
                                key="extension", passed=passed, detail="checked"
                            ),
                        ),
                    ),
                ),
            )
        summary = await repository.structure_gate_summary()
        await database.close()
        return summary

    summary = asyncio.run(scenario())
    assert summary["sample_size"] == 2
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["pass_rate"] == 0.5
    assert summary["checks"] == [
        {"key": "metadata", "evaluated": 2, "passed": 2, "pass_rate": 1.0},
        {"key": "extension", "evaluated": 2, "passed": 1, "pass_rate": 0.5},
    ]


def test_order_events_advance_the_execution_audit_to_final_status(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'execution-updates.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        now = datetime.now(UTC)
        await repository.record_execution(
            "BTCUSDT", ExecutionReport(client_order_id="cp-live", status="NEW")
        )
        await repository.record_user_event(
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now,
                now,
                "BTCUSDT",
                {
                    "o": {
                        "c": "cp-live",
                        "X": "PARTIALLY_FILLED",
                        "z": "0.4",
                        "ap": "99",
                    }
                },
            )
        )
        await repository.record_user_event(
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now + timedelta(seconds=1),
                now + timedelta(seconds=1),
                "BTCUSDT",
                {"o": {"c": "cp-live", "X": "FILLED", "z": "1", "ap": "100"}},
            )
        )
        await repository.record_user_event(
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now + timedelta(seconds=2),
                now + timedelta(seconds=2),
                "BTCUSDT",
                {"o": {"c": "cp-live", "X": "NEW", "z": "0", "ap": "0"}},
            )
        )
        result = (await repository.recent_executions())[0]
        await database.close()
        return result

    result = asyncio.run(scenario())
    assert result["status"] == "FILLED"
    assert result["report"]["filled_quantity"] == "1"
    assert result["report"]["average_price"] == "100"


def test_execution_record_catches_up_when_the_user_event_arrives_first(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'execution-race.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        now = datetime.now(UTC)
        await repository.record_user_event(
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now,
                now,
                "ETHUSDT",
                {"o": {"c": "cp-race", "X": "CANCELED", "z": "0", "ap": "0"}},
            )
        )
        await repository.record_execution(
            "ETHUSDT", ExecutionReport(client_order_id="cp-race", status="NEW")
        )
        result = (await repository.recent_executions())[0]
        await database.close()
        return result

    result = asyncio.run(scenario())
    assert result["status"] == "CANCELED"
    assert result["report"]["timestamp"] is not None


def test_trade_fills_include_protective_and_manual_exits_without_duplicate_entries(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'trade-fills.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        run_id = await repository.create_live_run({"provider_chain": ["local-rule"]})
        now = datetime.now(UTC)
        for client_order_id, quantity, average_price in (
            ("cp-entry", "283", "0.11188"),
            ("cp-entry-2", "303", "0.10948"),
        ):
            intent = TradeIntent(
                symbol="BANKUSDT",
                cadence="5m",
                action=TradeAction.OPEN_LONG,
                confidence=0.8,
                leverage=3,
                risk_fraction="0.01",
                stop_loss="0.10",
                take_profit="0.12",
                rationale="entry",
            )
            inference_id = await repository.record_inference(
                ProviderResult(intent, "local-rule", "trend-v1", timedelta(), "{}", {}),
                live_run_id=run_id,
            )
            await repository.record_execution(
                "BANKUSDT",
                ExecutionReport(
                    client_order_id=client_order_id,
                    status="FILLED",
                    filled_quantity=quantity,
                    average_price=average_price,
                ),
            )
            await repository.record_execution_attempt(
                "BANKUSDT",
                ExecutionAttempt(
                    inference_id=inference_id,
                    client_order_id=client_order_id,
                    status="SUCCEEDED",
                    stage="COMPLETE",
                    message="filled",
                ),
            )
        events = [
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now,
                now,
                "BANKUSDT",
                {
                    "o": {
                        "c": "cp-entry",
                        "s": "BANKUSDT",
                        "S": "BUY",
                        "x": "TRADE",
                        "X": "FILLED",
                        "z": "283",
                        "ap": "0.11188",
                        "R": False,
                        "rp": "0",
                    }
                },
            ),
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now + timedelta(minutes=1),
                now + timedelta(minutes=1),
                "BANKUSDT",
                {
                    "o": {
                        "c": "cp-entry-sl",
                        "s": "BANKUSDT",
                        "S": "SELL",
                        "x": "TRADE",
                        "X": "FILLED",
                        "z": "283",
                        "ap": "0.10716",
                        "R": True,
                        "rp": "-1.33576",
                    }
                },
            ),
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now + timedelta(minutes=2),
                now + timedelta(minutes=2),
                "BANKUSDT",
                {
                    "o": {
                        "c": "cp-entry-2",
                        "s": "BANKUSDT",
                        "S": "BUY",
                        "x": "TRADE",
                        "X": "FILLED",
                        "z": "303",
                        "ap": "0.10948",
                        "R": False,
                        "rp": "0",
                    }
                },
            ),
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now + timedelta(minutes=3),
                now + timedelta(minutes=3),
                "BANKUSDT",
                {
                    "o": {
                        "c": "cp-manual-123",
                        "s": "BANKUSDT",
                        "S": "SELL",
                        "x": "TRADE",
                        "X": "FILLED",
                        "z": "303",
                        "ap": "0.11000",
                        "R": True,
                        "rp": "0.15756",
                    }
                },
            ),
        ]
        for event in events[:3]:
            await repository.record_user_event(event)
        await repository.finish_live_run(
            run_id,
            status="stopped",
            stop_reason="done",
            ended_at=now + timedelta(minutes=2, seconds=30),
        )
        # A manual close belongs to the run that opened the position even when
        # the operator closes it after that run has stopped.
        await repository.record_user_event(events[3])
        result = await repository.recent_trade_fills()
        performance = await repository.recent_live_run_performance()
        await database.close()
        return result, performance

    fills, performance = asyncio.run(scenario())
    assert [item["client_order_id"] for item in fills] == [
        "cp-manual-123",
        "cp-entry-2",
        "cp-entry-sl",
        "cp-entry",
    ]
    assert fills[0]["purpose"] == "manual_close"
    assert fills[0]["related_client_order_id"] == "cp-entry-2"
    assert fills[0]["realized_pnl"] == "0.15756"
    assert Decimal(fills[0]["notional_usdt"]) == Decimal("33.33")
    assert Decimal(fills[0]["realized_pnl_margin_usdt"]) == (
        Decimal("303") * Decimal("0.10948") / Decimal("3")
    )
    assert Decimal(fills[0]["realized_return_percent"]) == (
        Decimal("0.15756")
        / (Decimal("303") * Decimal("0.10948") / Decimal("3"))
        * Decimal("100")
    )
    assert fills[2]["purpose"] == "stop_loss"
    assert fills[2]["related_client_order_id"] == "cp-entry"
    assert fills[2]["side"] == "SELL"
    assert fills[2]["realized_pnl"] == "-1.33576"
    assert Decimal(fills[2]["notional_usdt"]) == Decimal("30.32628")
    assert Decimal(fills[2]["realized_return_percent"]) < 0
    assert sum(item["client_order_id"] == "cp-entry" for item in fills) == 1
    assert performance[0]["closed_trades"] == 2
    assert performance[0]["wins"] == 1
    assert performance[0]["win_rate"] == "0.5"
    assert Decimal(performance[0]["realized_pnl"]) == Decimal("-1.17820")
    assert Decimal(performance[0]["unrealized_pnl"]) == 0
    assert Decimal(performance[0]["total_pnl"]) == Decimal("-1.17820")


def test_trade_fills_deduplicate_live_and_rest_reconciliation(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'duplicate-fills.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        now = datetime.now(UTC)
        live_payload = {
            "o": {
                "c": "cp-entry-sl",
                "i": 12345,
                "s": "SOLUSDT",
                "S": "SELL",
                "x": "TRADE",
                "X": "FILLED",
                "z": "12.86",
                "ap": "75.9",
                "R": True,
                "rp": "-9.1306",
            }
        }
        live_id = await repository.record_user_event(
            UserStreamEvent("ORDER_TRADE_UPDATE", now, now, "SOLUSDT", live_payload)
        )
        reconciled_payload = {
            "_source": "rest_trade_reconciliation",
            "o": {**live_payload["o"], "z": "12.860"},
        }
        reconciled_id = await repository.record_user_event(
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now - timedelta(milliseconds=1),
                now - timedelta(milliseconds=1),
                "SOLUSDT",
                reconciled_payload,
            )
        )
        # Simulate a database created before semantic persistence dedup existed.
        async with database.sessions.begin() as session:
            session.add(
                UserStreamEventRow(
                    event_type="ORDER_TRADE_UPDATE",
                    symbol="SOLUSDT",
                    event_time=now - timedelta(milliseconds=1),
                    transaction_time=now - timedelta(milliseconds=1),
                    payload_json=json.dumps(reconciled_payload, separators=(",", ":")),
                )
            )
        fills = await repository.recent_trade_fills()
        events = await repository.recent_user_events()
        await database.close()
        return live_id, reconciled_id, fills, events

    live_id, reconciled_id, fills, events = asyncio.run(scenario())
    assert reconciled_id == live_id
    assert len(events) == 2
    assert len(fills) == 1
    assert fills[0]["source"] == "exchange_user_stream"
    assert fills[0]["client_order_id"] == "cp-entry-sl"


def test_live_run_performance_revalues_partial_manual_close_after_stop(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'partial-close-pnl.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        run_id = await repository.create_live_run({"provider_chain": ["local-rule"]})
        intent = TradeIntent(
            symbol="BTCUSDT",
            cadence="5m",
            action=TradeAction.OPEN_LONG,
            confidence=0.8,
            leverage=2,
            risk_fraction="0.01",
            stop_loss="90",
            take_profit="120",
            rationale="entry",
        )
        inference_id = await repository.record_inference(
            ProviderResult(intent, "local-rule", "trend-v1", timedelta(), "{}", {}),
            live_run_id=run_id,
        )
        await repository.record_execution(
            "BTCUSDT",
            ExecutionReport(
                client_order_id="cp-partial-entry",
                status="FILLED",
                filled_quantity="10",
                average_price="100",
            ),
        )
        await repository.record_execution_attempt(
            "BTCUSDT",
            ExecutionAttempt(
                inference_id=inference_id,
                client_order_id="cp-partial-entry",
                status="SUCCEEDED",
                stage="COMPLETE",
                message="filled",
            ),
        )
        now = datetime.now(UTC)
        await repository.record_user_event(
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now,
                now,
                "BTCUSDT",
                {
                    "o": {
                        "c": "cp-partial-entry",
                        "s": "BTCUSDT",
                        "S": "BUY",
                        "x": "TRADE",
                        "X": "FILLED",
                        "z": "10",
                        "ap": "100",
                        "R": False,
                        "rp": "0",
                    }
                },
            )
        )
        await repository.finish_live_run(
            run_id,
            status="stopped",
            stop_reason="stopped by user",
            ended_at=now + timedelta(seconds=1),
        )
        await repository.record_user_event(
            UserStreamEvent(
                "ORDER_TRADE_UPDATE",
                now + timedelta(seconds=2),
                now + timedelta(seconds=2),
                "BTCUSDT",
                {
                    "o": {
                        "c": "cp-manual-partial",
                        "s": "BTCUSDT",
                        "S": "SELL",
                        "x": "TRADE",
                        "X": "FILLED",
                        "z": "4",
                        "ap": "110",
                        "R": True,
                        "rp": "40",
                    }
                },
            )
        )
        performance = await repository.recent_live_run_performance(
            current_positions={"BTCUSDT": {"mark_price": "105", "unrealized_pnl": "30"}}
        )
        await database.close()
        return performance[0]

    performance = asyncio.run(scenario())
    assert performance["realized_pnl"] == "40"
    assert performance["unrealized_pnl"] == "30"
    assert performance["total_pnl"] == "70"
    assert performance["open_position_count"] == 1
    assert performance["wins"] == 1
    assert performance["closed_trades"] == 1
    assert performance["win_rate"] == "1"


def test_live_run_performance_attributes_merged_position_by_entry_lot(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'merged-run-pnl.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)

        async def record_entry(run_id: int, client_order_id: str) -> None:
            intent = TradeIntent(
                symbol="BTCUSDT",
                cadence="5m",
                action=TradeAction.OPEN_LONG,
                confidence=0.8,
                leverage=2,
                risk_fraction="0.01",
                stop_loss="90",
                take_profit="140",
                rationale="entry",
            )
            inference_id = await repository.record_inference(
                ProviderResult(intent, "local-rule", "trend-v1", timedelta(), "{}", {}),
                live_run_id=run_id,
            )
            await repository.record_execution_attempt(
                "BTCUSDT",
                ExecutionAttempt(
                    inference_id=inference_id,
                    client_order_id=client_order_id,
                    status="SUCCEEDED",
                    stage="COMPLETE",
                    message="filled",
                ),
            )

        first_run = await repository.create_live_run({"run": 1})
        await record_entry(first_run, "cp-first-entry")
        await repository.finish_live_run(
            first_run, status="stopped", stop_reason="fixture"
        )
        second_run = await repository.create_live_run({"run": 2})
        await record_entry(second_run, "cp-second-entry")

        async def fills(_limit):
            return [
                {
                    "client_order_id": "cp-manual-partial",
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "reduce_only": True,
                    "realized_pnl": "20",
                    "report": {"filled_quantity": "1", "average_price": "130"},
                },
                {
                    "client_order_id": "cp-second-entry",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "reduce_only": False,
                    "realized_pnl": "0",
                    "report": {"filled_quantity": "1", "average_price": "120"},
                },
                {
                    "client_order_id": "cp-first-entry",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "reduce_only": False,
                    "realized_pnl": "0",
                    "report": {"filled_quantity": "1", "average_price": "100"},
                },
            ]

        repository.recent_trade_fills = fills  # type: ignore[method-assign]
        performance = await repository.recent_live_run_performance(
            current_positions={
                "BTCUSDT": {"mark_price": "130", "unrealized_pnl": "20"}
            }
        )
        await database.close()
        return {
            item["live_run_id"]: item for item in performance
        }, first_run, second_run

    performance, first_run, second_run = asyncio.run(scenario())
    first = performance[first_run]
    second = performance[second_run]
    assert first["realized_pnl"] == "15.0"
    assert first["unrealized_pnl"] == "15.0"
    assert first["total_pnl"] == "30.0"
    assert second["realized_pnl"] == "5.0"
    assert second["unrealized_pnl"] == "5.0"
    assert second["total_pnl"] == "10.0"


def test_model_close_fill_uses_linked_intent_instead_of_order_id_shape(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'model-close-fill.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        run_id = await repository.create_live_run({"provider_chain": ["model"]})
        entry = TradeIntent(
            symbol="BTCUSDT",
            cadence="15m",
            action=TradeAction.OPEN_LONG,
            confidence=0.8,
            leverage=2,
            risk_fraction="0.01",
            stop_loss="63000",
            take_profit="67000",
            rationale="entry",
        )
        close = TradeIntent(
            symbol="BTCUSDT",
            cadence="15m",
            action=TradeAction.CLOSE,
            confidence=0.7,
            leverage=1,
            risk_fraction="0",
            rationale="model close",
        )
        entry_inference = await repository.record_inference(
            ProviderResult(entry, "model", "fixture", timedelta(), "{}", {}),
            live_run_id=run_id,
        )
        close_inference = await repository.record_inference(
            ProviderResult(close, "model", "fixture", timedelta(), "{}", {}),
            live_run_id=run_id,
        )
        await repository.record_execution(
            "BTCUSDT",
            ExecutionReport(
                client_order_id="cp-generic-entry",
                status="FILLED",
                filled_quantity="0.01",
                average_price="65000",
            ),
        )
        for inference_id, client_order_id in (
            (entry_inference, "cp-generic-entry"),
            (close_inference, "cp-generic-close"),
        ):
            await repository.record_execution_attempt(
                "BTCUSDT",
                ExecutionAttempt(
                    inference_id=inference_id,
                    client_order_id=client_order_id,
                    status="SUCCEEDED",
                    stage="COMPLETE",
                    message="filled",
                ),
            )
        now = datetime.now(UTC)
        for client_order_id, side, reduce_only, realized_pnl in (
            ("cp-generic-entry", "BUY", False, "0"),
            ("cp-generic-close", "SELL", True, "-5.35935"),
        ):
            await repository.record_user_event(
                UserStreamEvent(
                    "ORDER_TRADE_UPDATE",
                    now,
                    now,
                    "BTCUSDT",
                    {
                        "o": {
                            "c": client_order_id,
                            "s": "BTCUSDT",
                            "S": side,
                            "x": "TRADE",
                            "X": "FILLED",
                            "z": "0.01",
                            "ap": "64385.7" if reduce_only else "65000",
                            "R": reduce_only,
                            "rp": realized_pnl,
                        }
                    },
                )
            )
        fills = await repository.recent_trade_fills()
        await database.close()
        return next(
            fill for fill in fills if fill["client_order_id"] == "cp-generic-close"
        )

    fill = asyncio.run(scenario())
    assert fill["purpose"] == "model_close"
    assert fill["reduce_only"] is True
    assert fill["related_client_order_id"] == "cp-generic-entry"
    assert fill["realized_pnl"] == "-5.35935"
    assert fill["realized_return_percent"] is not None
    assert Decimal(fill["realized_return_percent"]) < 0
    assert AuditRepository._trade_fill_identity(
        "cp-generic-reduce", intent_action="REDUCE"
    ) == (
        "model_reduce",
        None,
    )
    assert AuditRepository._trade_fill_identity("external-order", reduce_only=True) == (
        "other_close",
        None,
    )


def test_trade_fill_realized_return_uses_combined_add_entry_margin(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'add-fill-return.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        now = datetime.now(UTC)
        intent = TradeIntent(
            symbol="BTCUSDT",
            cadence="5m",
            action=TradeAction.ADD,
            confidence=0.8,
            leverage=5,
            risk_fraction="0.01",
            stop_loss="8",
            take_profit="14",
            rationale="add",
        )
        inference_id = await repository.record_inference(
            ProviderResult(
                intent,
                "local-rule",
                "trend-v1",
                timedelta(),
                "{}",
                {},
                input_payload={
                    "portfolio": {
                        "positions": {
                            "BTCUSDT": {"quantity": "100", "entry_price": "10"}
                        }
                    }
                },
            )
        )
        await repository.record_execution(
            "BTCUSDT",
            ExecutionReport(
                client_order_id="cp-add",
                status="FILLED",
                filled_quantity="50",
                average_price="12",
            ),
        )
        await repository.record_execution_attempt(
            "BTCUSDT",
            ExecutionAttempt(
                inference_id=inference_id,
                client_order_id="cp-add",
                status="SUCCEEDED",
                stage="COMPLETE",
                message="filled",
            ),
        )
        for (
            client_order_id,
            side,
            quantity,
            average_price,
            realized_pnl,
            reduce_only,
        ) in (
            ("cp-add", "BUY", "50", "12", "0", False),
            ("cp-add-tp", "SELL", "150", "14", "500", True),
        ):
            await repository.record_user_event(
                UserStreamEvent(
                    "ORDER_TRADE_UPDATE",
                    now,
                    now,
                    "BTCUSDT",
                    {
                        "o": {
                            "c": client_order_id,
                            "s": "BTCUSDT",
                            "S": side,
                            "x": "TRADE",
                            "X": "FILLED",
                            "z": quantity,
                            "ap": average_price,
                            "R": reduce_only,
                            "rp": realized_pnl,
                        }
                    },
                )
            )
        result = await repository.recent_trade_fills()
        await database.close()
        return next(item for item in result if item["purpose"] == "take_profit")

    fill = asyncio.run(scenario())
    combined_entry = (
        (Decimal("100") * Decimal("10")) + (Decimal("50") * Decimal("12"))
    ) / Decimal("150")
    expected_margin = Decimal("150") * combined_entry / Decimal("5")
    assert Decimal(fill["notional_usdt"]) == Decimal("2100")
    assert Decimal(fill["realized_pnl_margin_usdt"]) == expected_margin
    assert Decimal(fill["realized_return_percent"]) == (
        Decimal("500") / expected_margin * Decimal("100")
    )


def test_decision_events_join_inference_and_risk_outcomes(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'decision-events.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        opening = TradeIntent(
            symbol="BTCUSDT",
            cadence="5m",
            action="OPEN_LONG",
            confidence=0.8,
            leverage=3,
            risk_fraction="0.01",
            stop_loss="95",
            take_profit="110",
            rationale="trend",
        )
        opening_id = await repository.record_inference(
            ProviderResult(
                opening,
                "codex-auth",
                "gpt-test",
                timedelta(milliseconds=80),
                "{}",
                {},
                reasoning_effort="high",
            )
        )
        await repository.record_risk(
            "BTCUSDT",
            RiskDecision(accepted=False, reason="margin limit"),
            inference_id=opening_id,
        )
        hold = TradeIntent.hold("ETHUSDT", "5m", "no setup")
        hold_id = await repository.record_inference(
            ProviderResult(
                hold, "claude-code-auth", None, timedelta(milliseconds=20), "{}", {}
            )
        )
        await repository.record_risk(
            "ETHUSDT",
            RiskDecision(accepted=True, reason="hold: no order required"),
            inference_id=hold_id,
        )
        approved_id = await repository.record_inference(
            ProviderResult(
                opening.model_copy(update={"symbol": "SOLUSDT"}),
                "codex-auth",
                "gpt-test",
                timedelta(milliseconds=60),
                "{}",
                {},
            )
        )
        await repository.record_risk(
            "SOLUSDT",
            RiskDecision(
                accepted=True,
                reason="within limits",
                max_quantity="2.5",
                pre_trade_entry_price="100.1",
                pre_trade_reward_risk_ratio="1.55",
            ),
            inference_id=approved_id,
        )
        await repository.record_execution(
            "SOLUSDT",
            ExecutionReport(
                client_order_id="cp-sol",
                status="FILLED",
                filled_quantity="2.5",
                average_price="100.25",
            ),
        )
        await repository.record_execution_attempt(
            "SOLUSDT",
            ExecutionAttempt(
                inference_id=approved_id,
                client_order_id="cp-sol",
                status="SUCCEEDED",
                stage="COMPLETE",
                message="completed",
            ),
        )
        failed_id = await repository.record_inference(
            ProviderResult(
                opening.model_copy(update={"symbol": "DOGEUSDT"}),
                "codex-auth",
                "gpt-test",
                timedelta(milliseconds=55),
                "{}",
                {},
            )
        )
        await repository.record_risk(
            "DOGEUSDT",
            RiskDecision(accepted=True, reason="within limits", max_quantity="100"),
            inference_id=failed_id,
        )
        await repository.record_execution_attempt(
            "DOGEUSDT",
            ExecutionAttempt(
                inference_id=failed_id,
                client_order_id="cp-doge",
                status="RESCUED",
                stage="PROTECTION",
                message="protective bracket failed; rescued",
                exchange_error_code=-4120,
                estimated_loss_usdt="1.25",
            ),
        )
        await repository.record_inference(
            ProviderResult(
                opening.model_copy(update={"symbol": "XRPUSDT"}),
                "codex-auth",
                "gpt-test",
                timedelta(milliseconds=40),
                "{}",
                {},
            )
        )
        events = await repository.recent_decision_events()
        await database.close()
        return events

    events = asyncio.run(scenario())
    assert [event["outcome"] for event in events] == [
        "analysis_only",
        "execution_failed",
        "executed",
        "hold",
        "rejected",
    ]
    assert events[0]["risk"] is None
    assert events[1]["execution"]["status"] == "RESCUED"
    assert events[1]["execution"]["estimated_loss_usdt"] == "1.25"
    assert events[2]["risk"]["decision"]["max_quantity"] == "2.5"
    assert events[2]["risk"]["decision"]["pre_trade_entry_price"] == "100.1"
    assert events[2]["risk"]["decision"]["pre_trade_reward_risk_ratio"] == "1.55"
    assert events[2]["execution"]["status"] == "SUCCEEDED"
    assert events[2]["execution"]["entry_report"]["filled_quantity"] == "2.5"
    assert events[2]["execution"]["entry_report"]["average_price"] == "100.25"
    assert events[3]["intent"]["symbol"] == "ETHUSDT"
    assert events[3]["risk"]["accepted"] is True
    assert events[4]["model"] == "gpt-test"
    assert events[4]["provenance"]["reasoning_effort"] == "high"
    assert events[4]["risk"]["reason"] == "margin limit"


def test_provider_metrics_aggregate_latency_errors_and_models(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'provider-metrics.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "test")
        for model, duration_ms, usage in (
            ("model-a", 100, {}),
            ("model-a", 300, {"error": "ProviderError"}),
            ("model-b", 200, {}),
        ):
            await repository.record_inference(
                ProviderResult(
                    intent=intent,
                    provider="codex-auth",
                    model=model,
                    duration=timedelta(milliseconds=duration_ms),
                    raw_output="{}",
                    usage=usage,
                )
            )
        metrics = await repository.provider_metrics(24)
        await database.close()
        return metrics

    metrics = asyncio.run(scenario())
    assert len(metrics) == 1
    assert metrics[0]["provider"] == "codex-auth"
    assert metrics[0]["call_count"] == 3
    assert metrics[0]["error_count"] == 1
    assert metrics[0]["error_rate"] == 1 / 3
    assert metrics[0]["average_duration_ms"] == 200
    assert metrics[0]["p95_duration_ms"] == 300
    assert metrics[0]["models"] == {"model-a": 2, "model-b": 1}
    assert metrics[0]["last_call_at"].tzinfo is UTC


def test_batch_audit_rows_count_as_one_physical_provider_call(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'batch-metrics.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        for symbol, index, tokens, cost in (
            ("BTCUSDT", 1, 60, 0.06),
            ("ETHUSDT", 2, 40, 0.04),
        ):
            await repository.record_inference(
                ProviderResult(
                    intent=TradeIntent.hold(symbol, "5m", "test"),
                    provider="codex-auth",
                    model="gpt-test",
                    duration=timedelta(milliseconds=250),
                    raw_output="{}",
                    usage={
                        "physical_call_id": "shared-call",
                        "batch_shared_call": True,
                        "batch_size": 2,
                        "batch_index": index,
                        "total_tokens": tokens,
                        "cost_usd": cost,
                    },
                )
            )
        session = await repository.run_session_metrics(0)
        provider = (await repository.provider_metrics(24))[0]
        await database.close()
        return session, provider

    session, provider = asyncio.run(scenario())
    assert session["call_count"] == 1
    assert session["priced_call_count"] == 1
    assert session["total_tokens"] == 100
    assert session["equivalent_cost_usd"] == pytest.approx(0.1)
    assert session["average_duration_ms"] == 250
    assert provider["call_count"] == 1
    assert provider["models"] == {"gpt-test": 1}
    assert provider["tokens_total"] == 100
    assert provider["cost_usd_total"] == pytest.approx(0.1)
    assert provider["average_duration_ms"] == 250


def test_provider_metrics_aggregate_tokens_and_equivalent_cost(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'metrics.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "test")

        def result(provider: str, model: str | None, usage: dict) -> ProviderResult:
            return ProviderResult(
                intent=intent,
                provider=provider,
                model=model,
                duration=timedelta(milliseconds=100),
                raw_output=intent.model_dump_json(),
                usage=usage,
            )

        # Claude: tokens + equivalent USD cost.
        await repository.record_inference(
            result(
                "claude-code-auth",
                "claude-sonnet-5",
                {"total_tokens": 100, "cost_usd": 0.05},
            )
        )
        await repository.record_inference(
            result(
                "claude-code-auth",
                "claude-sonnet-5",
                {"total_tokens": 40, "cost_usd": 0.02},
            )
        )
        # Codex: tokens only, no cost data (subscription).
        await repository.record_inference(
            result("codex-auth", "gpt-5.6-sol", {"total_tokens": 6903})
        )
        metrics = await repository.provider_metrics(24)
        await database.close()
        return {item["provider"]: item for item in metrics}

    metrics = asyncio.run(scenario())
    claude = metrics["claude-code-auth"]
    codex = metrics["codex-auth"]
    assert claude["tokens_total"] == 140
    assert abs(claude["cost_usd_total"] - 0.07) < 1e-9
    assert claude["models"] == {"claude-sonnet-5": 2}
    assert codex["tokens_total"] == 6903
    assert codex["cost_usd_total"] is None  # no equivalent cost for Codex
    assert codex["models"] == {"gpt-5.6-sol": 1}


def test_provider_metrics_price_codex_via_catalog(tmp_path: Path) -> None:
    from candlepilot.providers.pricing import parse_models_dev

    catalog = parse_models_dev(
        {
            "openai": {
                "models": {
                    "gpt-5.6-sol": {
                        "cost": {"input": 5, "output": 30, "cache_read": 0.5}
                    }
                }
            }
        }
    )

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'codexcost.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "test")
        await repository.record_inference(
            ProviderResult(
                intent=intent,
                provider="codex-auth",
                model="gpt-5.6-sol",
                duration=timedelta(milliseconds=100),
                raw_output=intent.model_dump_json(),
                usage={
                    "input_tokens": 1000,
                    "cached_input_tokens": 400,
                    "output_tokens": 200,
                    "total_tokens": 1200,
                },
            )
        )
        metrics = await repository.provider_metrics(24, catalog=catalog)
        await database.close()
        return metrics[0]

    codex = asyncio.run(scenario())
    # 600 non-cached input * 5e-6 + 400 cached * 5e-7 + 200 output * 3e-5
    expected = 600 * 5e-6 + 400 * 5e-7 + 200 * 3e-5
    assert abs(codex["cost_usd_total"] - expected) < 1e-12
    assert codex["tokens_total"] == 1200


def test_run_session_metrics_respect_id_boundaries_and_complete_cost(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'run-session.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "test")

        async def record(usage: dict) -> int:
            return await repository.record_inference(
                ProviderResult(
                    intent=intent,
                    provider="claude-code-auth",
                    model="claude-test",
                    duration=timedelta(milliseconds=100),
                    raw_output="{}",
                    usage=usage,
                )
            )

        await record({"total_tokens": 999, "cost_usd": 1})
        start_id = await repository.latest_inference_id()
        await record(
            {
                "input_tokens": 100,
                "cache_read_input_tokens": 40,
                "cache_creation_input_tokens": 10,
                "output_tokens": 20,
                "total_tokens": 120,
                "cost_usd": 0.01,
            }
        )
        end_id = await repository.latest_inference_id()
        await record({"input_tokens": 50, "output_tokens": 5, "error": "fixture"})
        completed = await repository.run_session_metrics(start_id, end_at_id=end_id)
        running = await repository.run_session_metrics(start_id)
        await database.close()
        return completed, running

    completed, running = asyncio.run(scenario())
    assert completed == {
        "call_count": 1,
        "error_count": 0,
        "input_tokens": 100,
        "cached_input_tokens": 40,
        "cache_creation_input_tokens": 10,
        "output_tokens": 20,
        "total_tokens": 120,
        "priced_call_count": 1,
        "cost_complete": True,
        "equivalent_cost_usd": 0.01,
        "average_duration_ms": 100.0,
        "average_tokens": 120.0,
        "average_cost_usd": 0.01,
    }
    assert running["call_count"] == 2
    assert running["error_count"] == 1
    assert running["total_tokens"] == 175
    assert running["cost_complete"] is False
    assert running["equivalent_cost_usd"] is None
    assert running["average_duration_ms"] == 100.0
    assert running["average_tokens"] == 87.5
    assert running["average_cost_usd"] is None


def test_clear_history_is_selective_and_preserves_runtime_state(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'clear.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "seed")
        inference_id = await repository.record_inference(
            ProviderResult(
                intent=intent,
                provider="codex-auth",
                model="m",
                duration=timedelta(milliseconds=1),
                raw_output=intent.model_dump_json(),
                usage={},
            )
        )
        await repository.record_risk(
            "BTCUSDT",
            RiskDecision(accepted=True, reason="ok"),
            inference_id=inference_id,
        )
        await repository.set_runtime_state("paper_account", "keep-me")

        counts = await repository.clear_history({"inferences"})
        remaining_inferences = await repository.recent_intents()
        remaining_risk = await repository.recent_risk_decisions()
        preserved = await repository.get_runtime_state("paper_account")
        await database.close()
        return counts, remaining_inferences, remaining_risk, preserved

    counts, inferences, risk, preserved = asyncio.run(scenario())
    assert counts == {"inferences": 1}
    assert inferences == []
    assert len(risk) == 1  # not selected -> retained, but no dangling identity
    assert risk[0]["inference_id"] is None
    assert preserved == "keep-me"  # runtime_state (paper account) never cleared


def test_trailing_stop_events_are_queryable_and_clearable(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'trailing-events.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        await repository.record_trailing_stop_event(
            "BTCUSDT",
            "shadow",
            "shadow",
            {"candidate_stop": "102", "original_stop": "98"},
        )
        before = await repository.recent_trailing_stop_events()
        counts = await repository.clear_history({"trailing_stops"})
        after = await repository.recent_trailing_stop_events()
        await database.close()
        return before, counts, after

    before, counts, after = asyncio.run(scenario())
    assert before[0]["event"]["candidate_stop"] == "102"
    assert counts == {"trailing_stops": 1}
    assert after == []


def test_database_migrations_are_versioned_and_idempotent(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'migrations.db'}")
        await database.initialize()
        first = await database.schema_version()
        await database.initialize()
        second = await database.schema_version()
        await database.close()
        return first, second

    # Derived, not hardcoded: the point is that re-running initialize is a
    # no-op, not that the schema happens to be at some particular version.
    assert asyncio.run(scenario()) == (CURRENT_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION)


def test_current_database_baseline_advances_without_legacy_migrations(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schema-v12.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at DATETIME);
        INSERT INTO schema_migrations VALUES (12, '2026-07-18 00:00:00');
        """
    )
    connection.close()

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{path}")
        await database.initialize()
        version = await database.schema_version()
        await database.close()
        return version

    assert asyncio.run(scenario()) == CURRENT_SCHEMA_VERSION


def test_prebaseline_database_is_rejected_instead_of_guessed_forward(
    tmp_path: Path,
) -> None:
    path = tmp_path / "schema-v11.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at DATETIME);
        INSERT INTO schema_migrations VALUES (11, '2026-07-18 00:00:00');
        """
    )
    connection.close()

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{path}")
        try:
            with pytest.raises(RuntimeError, match="before v12"):
                await database.initialize()
        finally:
            await database.close()

    asyncio.run(scenario())


async def _seed_decisions(repository: AuditRepository) -> None:
    """One decision of every outcome the classifier can produce."""

    async def infer(
        symbol: str, cadence: str, provider: str, intent: TradeIntent
    ) -> int:
        return await repository.record_inference(
            ProviderResult(
                intent=intent,
                provider=provider,
                model="m",
                duration=timedelta(milliseconds=1),
                raw_output=intent.model_dump_json(),
                usage={},
            )
        )

    def opening(symbol: str, cadence: str) -> TradeIntent:
        return TradeIntent(
            symbol=symbol,
            cadence=cadence,
            action=TradeAction.OPEN_LONG,
            confidence=0.8,
            leverage=2,
            risk_fraction="0.01",
            stop_loss="90",
            take_profit="110",
            rationale="seed",
        )

    # hold
    await infer(
        "BTCUSDT", "5m", "codex-auth", TradeIntent.hold("BTCUSDT", "5m", "no setup")
    )
    # analysis_only: an intent that never reached the risk policy.
    await infer("ETHUSDT", "5m", "codex-auth", opening("ETHUSDT", "5m"))
    # rejected
    rejected = await infer(
        "BTCUSDT", "15m", "claude-code-auth", opening("BTCUSDT", "15m")
    )
    await repository.record_risk(
        "BTCUSDT", RiskDecision(accepted=False, reason="no"), inference_id=rejected
    )
    # approved: risk passed, nothing executed.
    approved = await infer("BTCUSDT", "30m", "codex-auth", opening("BTCUSDT", "30m"))
    await repository.record_risk(
        "BTCUSDT", RiskDecision(accepted=True, reason="ok"), inference_id=approved
    )
    # executed
    executed = await infer("SOLUSDT", "5m", "codex-auth", opening("SOLUSDT", "5m"))
    await repository.record_risk(
        "SOLUSDT", RiskDecision(accepted=True, reason="ok"), inference_id=executed
    )
    await repository.record_execution_attempt(
        "SOLUSDT",
        ExecutionAttempt(
            inference_id=executed,
            status="SUCCEEDED",
            stage="COMPLETE",
            message="filled",
        ),
    )
    # execution_failed
    failed = await infer("SOLUSDT", "15m", "codex-auth", opening("SOLUSDT", "15m"))
    await repository.record_risk(
        "SOLUSDT", RiskDecision(accepted=True, reason="ok"), inference_id=failed
    )
    await repository.record_execution_attempt(
        "SOLUSDT",
        ExecutionAttempt(
            inference_id=failed, status="FAILED", stage="ENTRY", message="rejected"
        ),
    )


def test_outcome_filter_agrees_with_the_python_classifier(tmp_path: Path) -> None:
    """The SQL filter reproduces _decision_outcome and must not drift from it.

    A filter that classifies differently from the column the frontend displays
    would answer "show me every rejection" with the wrong set, and nothing in
    the response would admit it.
    """

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'outcomes.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        await _seed_decisions(repository)
        everything = await repository.recent_decision_events(500)
        by_outcome = {
            outcome: await repository.recent_decision_events(500, outcome=outcome)
            for outcome in DECISION_OUTCOMES
        }
        await database.close()
        return everything, by_outcome

    everything, by_outcome = asyncio.run(scenario())

    # The seed covers every outcome, so this test fails if a new one is added
    # without a filter branch to match.
    assert {event["outcome"] for event in everything} == set(DECISION_OUTCOMES)
    for outcome in DECISION_OUTCOMES:
        expected = [event["id"] for event in everything if event["outcome"] == outcome]
        assert [event["id"] for event in by_outcome[outcome]] == expected, outcome
        assert expected, outcome


def test_decision_events_page_backwards_without_skipping_or_repeating(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'paging.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        await _seed_decisions(repository)

        pages: list[list[int]] = []
        cursor: int | None = None
        while True:
            page = await repository.recent_decision_events(2, before_id=cursor)
            if not page:
                break
            pages.append([event["id"] for event in page])
            cursor = page[-1]["id"]
        everything = await repository.recent_decision_events(500)
        await database.close()
        return pages, [event["id"] for event in everything]

    pages, everything = asyncio.run(scenario())

    walked = [identifier for page in pages for identifier in page]
    assert walked == everything
    assert len(walked) == len(set(walked))


def test_decision_events_page_by_complete_live_runs(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'run-paging.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        for run_number in range(1, 13):
            run_id = await repository.create_live_run({"run_number": run_number})
            for symbol in ("BTCUSDT", "ETHUSDT"):
                intent = TradeIntent.hold(symbol, "15m", f"run {run_number}")
                await repository.record_inference(
                    ProviderResult(
                        intent=intent,
                        provider="local-rule",
                        model="trend-v1",
                        duration=timedelta(milliseconds=1),
                        raw_output=intent.model_dump_json(),
                        usage={},
                    ),
                    live_run_id=run_id,
                )
            await repository.finish_live_run(
                run_id,
                status="stopped",
                stop_reason="fixture",
            )

        first = await repository.recent_decision_events(run_limit=10)
        first_run_ids = [event["live_run_id"] for event in first]
        second = await repository.recent_decision_events(
            run_limit=10,
            before_run_id=min(first_run_ids),
        )
        filtered = await repository.recent_decision_events(
            run_limit=10,
            symbol="BTCUSDT",
        )
        await database.close()
        return first, second, filtered

    first, second, filtered = asyncio.run(scenario())
    assert [event["live_run_id"] for event in first] == [
        run_id for run_id in range(12, 2, -1) for _ in range(2)
    ]
    assert [event["live_run_id"] for event in second] == [2, 2, 1, 1]
    assert [event["live_run_id"] for event in filtered] == list(range(12, 2, -1))
    assert all(event["intent"]["symbol"] == "BTCUSDT" for event in filtered)


def test_paging_and_filtering_compose(tmp_path: Path) -> None:
    """A cursor must stay inside the filtered result, not the whole table."""

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'compose.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        await _seed_decisions(repository)
        first = await repository.recent_decision_events(1, symbol="SOLUSDT")
        second = await repository.recent_decision_events(
            1, symbol="SOLUSDT", before_id=first[0]["id"]
        )
        both = await repository.recent_decision_events(500, symbol="SOLUSDT")
        await database.close()
        return first, second, both

    first, second, both = asyncio.run(scenario())

    assert [event["id"] for event in first + second] == [event["id"] for event in both]
    assert all(event["intent"]["symbol"] == "SOLUSDT" for event in both)


def test_custom_endpoint_is_priced_once_it_declares_who_bills_it(
    tmp_path: Path,
) -> None:
    """Custom API calls showed no cost at all, whatever the model.

    The provider name is "openai-compatible:<id>", which no fixed map contained,
    so the catalog was never consulted and every custom endpoint reported "--"
    forever. That also left the run budget guard inert, since it stops on an
    equivalent cost it could never compute.
    """

    catalog = parse_models_dev(
        {
            "xai": {
                "models": {
                    "grok-4.5": {"cost": {"input": 2, "output": 6, "cache_read": 0.5}}
                }
            }
        }
    )

    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'custom-cost.db'}")
        await database.initialize()
        repository = AuditRepository(database.sessions)
        intent = TradeIntent.hold("BTCUSDT", "5m", "seed")
        await repository.record_inference(
            ProviderResult(
                intent=intent,
                provider="openai-compatible:grok",
                model="grok-4.5",
                duration=timedelta(seconds=28),
                raw_output=intent.model_dump_json(),
                usage={
                    "input_tokens": 1_000_000,
                    "output_tokens": 1_000_000,
                    "total_tokens": 2_000_000,
                },
            )
        )
        declared = await repository.provider_metrics(
            catalog=catalog, provider_ids={"openai-compatible:grok": "xai"}
        )
        undeclared = await repository.provider_metrics(catalog=catalog)
        await database.close()
        return declared, undeclared

    declared, undeclared = asyncio.run(scenario())

    # 1M input at $2/M plus 1M output at $6/M.
    assert declared[0]["cost_usd_total"] == 8.0
    # Without a declared vendor the cost stays unknown rather than guessed.
    assert undeclared[0]["cost_usd_total"] is None
