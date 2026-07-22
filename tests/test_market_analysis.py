import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from candlepilot.api import create_app
from candlepilot.application.engine import TradingEngine
from candlepilot.analysis.datapack import AnalysisDataPackBuilder
from candlepilot.analysis.decision import (
    AnalysisAssistedDecision,
    AnalysisDecisionBridge,
    analysis_assisted_output_schema,
    build_analysis_assisted_prompt,
)
from candlepilot.analysis.models import MarketAnalysis, market_analysis_output_schema
from candlepilot.analysis.outcomes import (
    evaluate_outcome,
    evaluate_outcome_from_market,
    next_complete_5m_start,
)
from candlepilot.analysis.performance import calculate_analysis_performance
from candlepilot.analysis.prompt import PROMPT_VERSION, build_analysis_prompt
from candlepilot.analysis.scheduler import (
    MarketAnalysisScheduler,
    next_analysis_boundary,
)
from candlepilot.analysis.service import MarketAnalysisService
from candlepilot.domain.models import MarketSnapshot, PortfolioState, ProviderHealth, TradeIntent
from candlepilot.market.features import Kline
from candlepilot.market.scanner import Candidate
from candlepilot.providers.base import DecisionProvider, ProviderResult, StructuredOutputResult
from candlepilot.providers.cli import ProviderInvocationError
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.storage.database import AuditRepository, Database, MarketAnalysisRepository
from conftest import FakeTestnetBroker


def _analysis_payload(direction: str = "long") -> dict[str, object]:
    base: dict[str, object] = {
        "direction": direction,
        "summary": "15 分钟结构偏强，但仍需 1 小时周期确认。",
        "anchor": {
            "timeframe": "15m",
            "time": "2026-07-22T10:00:00+00:00",
            "price": 100,
            "reason": "最新确认的 15 分钟结构 K 线",
        },
        "scenarios": [
            {
                "name": "延续上涨",
                "probability": 60,
                "trigger": "15 分钟收盘站上 101",
                "expected_path": "价格先测试 103，再测试 106",
                "invalidation": "15 分钟收盘跌破 98",
            },
            {
                "name": "区间整理",
                "probability": 40,
                "trigger": "价格持续处于 101 下方",
                "expected_path": "价格在 98 至 101 之间轮动",
                "invalidation": "收盘离开区间",
            },
        ],
        "range_plan": None,
        "entry_plan": {
            "entry": 101,
            "stop": 98,
            "target1": 104,
            "target2": 108,
            "stop_structure": "确认的 15 分钟摆动低点下方",
            "entry_trigger": "15 分钟收盘站上 101，随后 5 分钟回踩确认",
            "management": "T1 减仓约一半；结构保持时将剩余仓位止损移向保本价。",
        },
        "key_evidence": ["15 分钟 EMA 排列一致", "1 小时 MACD 柱状图改善"],
        "missing_data_impact": ["新闻与事件风险未知"],
    }
    return base


def test_analysis_scheduler_aligns_to_utc_quarter_hour() -> None:
    assert next_analysis_boundary(
        datetime(2026, 7, 22, 10, 7, 31, tzinfo=UTC)
    ) == datetime(2026, 7, 22, 10, 15, tzinfo=UTC)
    assert next_analysis_boundary(
        datetime(2026, 7, 22, 10, 45, tzinfo=UTC)
    ) == datetime(2026, 7, 22, 11, 0, tzinfo=UTC)


def test_analysis_scheduler_records_an_independent_round() -> None:
    async def scenario() -> dict[str, object]:
        now = datetime(2026, 7, 22, 10, 7, tzinfo=UTC)

        async def run_round() -> dict[str, object]:
            return {
                "status": "completed",
                "candidates": ["BTCUSDT"],
                "queued": [{"id": 1, "symbol": "BTCUSDT"}],
                "skipped": [],
            }

        scheduler = MarketAnalysisScheduler(run_round, clock=lambda: now)
        await scheduler.run_now()
        status = scheduler.status()
        await scheduler.close()
        return status

    status = asyncio.run(scenario())
    assert status["round_running"] is False
    assert status["last_started_at"] == datetime(2026, 7, 22, 10, 7, tzinfo=UTC)
    assert status["last_finished_at"] == datetime(2026, 7, 22, 10, 7, tzinfo=UTC)
    assert status["last_result"]["queued"] == [{"id": 1, "symbol": "BTCUSDT"}]


def test_stopping_analysis_schedule_keeps_the_current_round_alive() -> None:
    async def scenario() -> tuple[dict[str, object], dict[str, object]]:
        started = asyncio.Event()
        release = asyncio.Event()
        now = datetime(2026, 7, 22, 10, 7, tzinfo=UTC)

        async def run_round() -> dict[str, object]:
            started.set()
            await release.wait()
            return {"status": "completed", "candidates": [], "queued": [], "skipped": []}

        scheduler = MarketAnalysisScheduler(run_round, clock=lambda: now)
        scheduler.start()
        running = asyncio.create_task(scheduler.run_now())
        await started.wait()
        await scheduler.stop()
        stopped_status = scheduler.status()
        release.set()
        await running
        finished_status = scheduler.status()
        await scheduler.close()
        return stopped_status, finished_status

    stopped_status, finished_status = asyncio.run(scenario())
    assert stopped_status["enabled"] is False
    assert stopped_status["round_running"] is True
    assert finished_status["round_running"] is False
    assert finished_status["last_result"]["status"] == "completed"


def test_analysis_contract_requires_explicit_directional_levels() -> None:
    analysis = MarketAnalysis.model_validate(_analysis_payload())
    assert analysis.reward_risk() == {"target1": 1.0, "target2": 7 / 3}

    invalid = _analysis_payload()
    invalid["entry_plan"] = None
    with pytest.raises(ValidationError, match="requires an entry plan"):
        MarketAnalysis.model_validate(invalid)


def test_analysis_performance_compares_fixed_notional_and_fixed_risk() -> None:
    def record(direction: str, outcome: str) -> dict[str, object]:
        if direction == "long":
            plan = {"entry": 100, "stop": 90, "target1": 110, "target2": 120}
        else:
            plan = {"entry": 100, "stop": 110, "target1": 90, "target2": 80}
        return {
            "result": {"direction": direction, "entry_plan": plan},
            "outcome": {"status": outcome},
        }

    performance = calculate_analysis_performance(
        [
            record("long", "stopped"),
            record("long", "target2"),
            record("short", "breakeven_after_target1"),
            record("long", "ambiguous"),
            record("short", "active"),
            {"result": {"direction": "neutral"}, "outcome": {"status": "neutral_observation"}},
        ],
        fixed_notional_usdt=100,
        fixed_risk_usdt=10,
    )

    assert performance["directional_analyses"] == 5
    assert performance["settled_trades"] == 3
    assert performance["open_trades"] == 1
    assert performance["ambiguous_results"] == 1
    assert performance["wins"] == 2
    assert performance["losses"] == 1
    assert performance["fixed_notional"]["total_pnl_usdt"] == pytest.approx(10)
    assert performance["fixed_notional"]["win_rate_percent"] == pytest.approx(200 / 3)
    assert performance["fixed_risk"]["total_pnl_usdt"] == pytest.approx(10)
    assert performance["fixed_risk"]["total_r"] == pytest.approx(1)
    assert performance["fixed_risk"]["win_rate_percent"] == pytest.approx(200 / 3)


def test_analysis_contract_keeps_low_reward_plan_for_downstream_risk() -> None:
    payload = _analysis_payload()
    payload["entry_plan"]["target1"] = 103  # type: ignore[index]

    analysis = MarketAnalysis.model_validate(payload)

    assert analysis.reward_risk() == {"target1": 2 / 3, "target2": 7 / 3}


def test_analysis_contract_normalizes_fractional_scenario_probabilities() -> None:
    payload = _analysis_payload()
    payload["scenarios"][0]["probability"] = 0.6  # type: ignore[index]
    payload["scenarios"][1]["probability"] = 0.4  # type: ignore[index]

    analysis = MarketAnalysis.model_validate(payload)

    assert [item.probability for item in analysis.scenarios] == [60, 40]


def test_analysis_contract_does_not_rescale_invalid_fractional_totals() -> None:
    payload = _analysis_payload()
    payload["scenarios"][0]["probability"] = 0.2  # type: ignore[index]
    payload["scenarios"][1]["probability"] = 0.2  # type: ignore[index]

    with pytest.raises(ValidationError, match="must total 100%"):
        MarketAnalysis.model_validate(payload)


def test_analysis_output_schema_requires_every_object_property() -> None:
    schema = market_analysis_output_schema()

    def assert_strict(node: object) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                assert node.get("required") == list(properties)
                assert node.get("additionalProperties") is False
            assert "default" not in node
            for value in node.values():
                assert_strict(value)
        elif isinstance(node, list):
            for value in node:
                assert_strict(value)

    assert_strict(schema)
    assert "missing_data_impact" in schema["required"]
    assert "Simplified Chinese" in schema["properties"]["summary"]["description"]


def test_analysis_prompt_requires_chinese_user_facing_text() -> None:
    prompt = build_analysis_prompt({"symbol": "BTCUSDT"})

    assert PROMPT_VERSION == "market-analysis-v4"
    assert "Write every user-facing natural-language value in Simplified Chinese" in prompt
    assert "Keep JSON keys, enum values" in prompt
    assert "Previous analysis may be in another language" in prompt
    assert "benchmark snapshots describe broad BTC/ETH market regime" in prompt
    assert "Do not use universal ratio thresholds" in prompt


def test_assisted_analysis_maps_t1_to_fixed_one_times_intent_and_keeps_t2_shadow() -> None:
    decision = AnalysisAssistedDecision.model_validate(
        {
            "symbol": "BTCUSDT",
            "analysis": _analysis_payload(),
            "execution": {
                "confidence": 0.8,
                "order_type": "LIMIT",
                "ttl_seconds": 120,
                "setup_type": "BREAKOUT_RETEST",
                "trigger_type": "RECLAIM",
                "invalidation_type": "SWING",
                "invalidation_level": 98,
                "target_type": "SWING",
            },
        }
    )
    snapshot = MarketSnapshot(
        symbol="BTCUSDT",
        cadence="15m",
        timestamp=datetime.now(UTC),
        mark_price="100",
        bid="99.9",
        ask="100.1",
        quote_volume_24h="1000000",
    )
    result = AnalysisDecisionBridge._provider_result(
        decision=decision,
        snapshot=snapshot,
        portfolio=PortfolioState(equity="10000", available_balance="8000"),
        provider="fixture",
        model="fixture-model",
        duration=timedelta(seconds=1),
        raw_output="{}",
        usage={"analysis_decision_mode": "shadow"},
        provider_version="fixture-1",
        reasoning_effort="medium",
        input_payload={},
        prompt="fixture",
    )

    assert result.intent.action.value == "OPEN_LONG"
    assert result.intent.leverage == 1
    assert result.intent.take_profit == Decimal("104")
    assert result.usage["shadow_target2"] == 108
    assert result.usage["assisted_execution_ready"] is True
    assert result.prompt_version == "analysis-assisted-decision-v5"


def test_assisted_schema_is_strict_and_prompt_declares_shadow_boundaries() -> None:
    schema = analysis_assisted_output_schema()

    def assert_strict(node: object) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                assert node.get("required") == list(properties)
                assert node.get("additionalProperties") is False
            assert "default" not in node
            for value in node.values():
                assert_strict(value)
        elif isinstance(node, list):
            for value in node:
                assert_strict(value)

    assert_strict(schema)
    prompt = build_analysis_assisted_prompt([{"symbol": "BTCUSDT"}])
    assert "T1 is the fixed formal take-profit field" in prompt
    assert "T2 is retained only for shadow outcome comparison" in prompt
    assert "fixes assisted decisions at 1x leverage" in prompt
    assert "use 45, 30 and 25, never 0.45, 0.30 and 0.25" in prompt
    assert "range_plan must contain anchor.price" in prompt
    assert "A LIMIT requires ttl_seconds from 5 to 900" in prompt
    assert "CandlePilot will safely HOLD that symbol" in prompt


def test_assisted_directional_market_allows_null_ttl() -> None:
    payload = {
        "symbol": "BTCUSDT",
        "analysis": _analysis_payload(),
        "execution": {
            "confidence": 0.8,
            "order_type": "MARKET",
            "ttl_seconds": None,
            "setup_type": "TREND_CONTINUATION",
            "trigger_type": "MARKET_CONFIRMED",
            "invalidation_type": "SWING",
            "invalidation_level": 98,
            "target_type": "SWING",
        },
    }

    assert AnalysisAssistedDecision.model_validate(payload).has_complete_execution_hints()


def test_assisted_neutral_ignores_accidental_execution_hints() -> None:
    analysis = _analysis_payload("neutral")
    analysis["entry_plan"] = None
    analysis["range_plan"] = {"low": 98, "high": 102, "tactic": "保持观望"}
    decision = AnalysisAssistedDecision.model_validate(
        {
            "symbol": "BTCUSDT",
            "analysis": analysis,
            "execution": {
                "confidence": 0.2,
                "order_type": "MARKET",
                "ttl_seconds": None,
                "setup_type": "TREND_CONTINUATION",
                "trigger_type": "MARKET_CONFIRMED",
                "invalidation_type": "SWING",
                "invalidation_level": 98,
                "target_type": "SWING",
            },
        }
    )

    assert not decision.has_complete_execution_hints()


def test_assisted_directional_incomplete_hints_safely_hold() -> None:
    decision = AnalysisAssistedDecision.model_validate(
        {
            "symbol": "BTCUSDT",
            "analysis": _analysis_payload(),
            "execution": {
                "confidence": 0.8,
                "order_type": "LIMIT",
                "trigger_type": "RECLAIM",
                "invalidation_type": "SWING",
                "invalidation_level": 98,
                "target_type": "SWING",
            },
        }
    )
    snapshot = MarketSnapshot(
        symbol="BTCUSDT",
        cadence="15m",
        timestamp=datetime.now(UTC),
        mark_price="100",
        bid="99.9",
        ask="100.1",
        quote_volume_24h="1000000",
    )

    result = AnalysisDecisionBridge._provider_result(
        decision=decision,
        snapshot=snapshot,
        portfolio=PortfolioState(equity="10000", available_balance="8000"),
        provider="fixture",
        model="fixture-model",
        duration=timedelta(seconds=1),
        raw_output="{}",
        usage={},
        provider_version="fixture-1",
        reasoning_effort="medium",
        input_payload={},
        prompt="fixture",
    )

    assert result.intent.action.value == "HOLD"
    assert "不猜测交易参数" in result.intent.rationale
    assert result.usage["assisted_execution_ready"] is False


def test_neutral_analysis_requires_range_containing_anchor() -> None:
    payload = _analysis_payload("neutral")
    payload["entry_plan"] = None
    payload["range_plan"] = {"low": 98, "high": 102, "tactic": "等待区间边缘确认"}
    assert MarketAnalysis.model_validate(payload).direction == "neutral"
    payload["range_plan"] = {"low": 90, "high": 94, "tactic": "无效区间"}
    with pytest.raises(ValidationError, match="contain the anchor"):
        MarketAnalysis.model_validate(payload)


def test_neutral_analysis_expands_nearby_range_boundary_to_anchor() -> None:
    payload = _analysis_payload("neutral")
    payload["entry_plan"] = None
    payload["range_plan"] = {"low": 98, "high": 99, "tactic": "等待区间边缘确认"}

    analysis = MarketAnalysis.model_validate(payload)

    assert analysis.range_plan is not None
    assert analysis.range_plan.low == 98
    assert analysis.range_plan.high == 100


def _bar(minutes: int, low: str, high: str, *, opened: str = "101") -> Kline:
    return Kline(
        open_time=datetime(2026, 7, 22, 10, minutes, tzinfo=UTC),
        open=Decimal(opened),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(opened),
        volume=Decimal("1"),
        quote_volume=Decimal("100"),
    )


def test_outcome_tracks_t1_then_breakeven_after_entry() -> None:
    analysis = MarketAnalysis.model_validate(_analysis_payload())
    outcome = evaluate_outcome(
        analysis,
        [
            _bar(0, "100.5", "102"),
            _bar(5, "103", "105", opened="104"),
            _bar(10, "100", "102"),
        ],
    )
    assert outcome.status == "breakeven_after_target1"
    assert outcome.entry_at == datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    assert outcome.target1_at == datetime(2026, 7, 22, 10, 5, tzinfo=UTC)
    assert outcome.detail == "T1 部分止盈后，剩余仓位回到入场价"


def test_outcome_records_stop_before_entry() -> None:
    analysis = MarketAnalysis.model_validate(_analysis_payload())

    outcome = evaluate_outcome(analysis, [_bar(0, "97", "99", opened="98")])

    assert outcome.status == "stopped_before_entry"
    assert outcome.entry_at is None
    assert outcome.resolved_at == datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    assert outcome.detail == "计划尚未入场，价格已先触及结构止损"


def test_outcome_records_target1_before_entry() -> None:
    analysis = MarketAnalysis.model_validate(_analysis_payload())

    outcome = evaluate_outcome(analysis, [_bar(0, "103", "105", opened="104")])

    assert outcome.status == "target1_before_entry"
    assert outcome.entry_at is None
    assert outcome.resolved_at == datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    assert outcome.detail == "计划尚未入场，价格已先触及 T1"


def test_outcome_marks_unknowable_intrabar_order_and_starts_after_completion_bar() -> None:
    analysis = MarketAnalysis.model_validate(_analysis_payload())
    outcome = evaluate_outcome(analysis, [_bar(0, "97", "102")])
    assert outcome.status == "ambiguous"
    assert "无法确定先后顺序" in outcome.detail
    assert next_complete_5m_start(datetime(2026, 7, 22, 10, 0, 1, tzinfo=UTC)) == datetime(
        2026, 7, 22, 10, 5, tzinfo=UTC
    )


def test_outcome_uses_complete_minute_bars_to_resolve_five_minute_order() -> None:
    analysis = MarketAnalysis.model_validate(_analysis_payload())
    window = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    outcome = evaluate_outcome(
        analysis,
        [_bar(0, "97", "102")],
        minute_refinements={
            window: [
                _bar(0, "100.5", "102"),
                _bar(1, "97", "99.5", opened="99"),
                _bar(2, "99", "100", opened="99.5"),
                _bar(3, "99", "100", opened="99.5"),
                _bar(4, "99", "100", opened="99.5"),
            ]
        },
    )

    assert outcome.status == "stopped"
    assert outcome.entry_at == window
    assert outcome.resolved_at == window + timedelta(minutes=1)
    assert outcome.bars_observed == 1
    assert "已使用完整 1 分钟 K 线细分" in outcome.detail


def test_outcome_uses_minutes_to_identify_stop_before_entry() -> None:
    analysis = MarketAnalysis.model_validate(_analysis_payload())
    window = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    outcome = evaluate_outcome(
        analysis,
        [_bar(0, "97", "102")],
        minute_refinements={
            window: [
                _bar(0, "97", "99", opened="98"),
                _bar(1, "100.5", "102"),
                _bar(2, "99", "100", opened="99.5"),
                _bar(3, "99", "100", opened="99.5"),
                _bar(4, "99", "100", opened="99.5"),
            ]
        },
    )

    assert outcome.status == "stopped_before_entry"
    assert outcome.entry_at is None
    assert outcome.resolved_at == window
    assert "已使用完整 1 分钟 K 线细分" in outcome.detail


def test_outcome_keeps_ambiguity_when_conflict_remains_inside_one_minute() -> None:
    analysis = MarketAnalysis.model_validate(_analysis_payload())
    window = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    outcome = evaluate_outcome(
        analysis,
        [_bar(0, "97", "102")],
        minute_refinements={
            window: [
                _bar(0, "97", "102"),
                _bar(1, "99", "100"),
                _bar(2, "99", "100"),
                _bar(3, "99", "100"),
                _bar(4, "99", "100"),
            ]
        },
    )

    assert outcome.status == "ambiguous"
    assert outcome.resolved_at == window
    assert "同一根完整 1 分钟 K 线" in outcome.detail


def _outcome_row(
    opened: datetime,
    interval_minutes: int,
    low: str,
    high: str,
    *,
    price: str = "101",
) -> list[object]:
    return [
        int(opened.timestamp() * 1000),
        price,
        high,
        low,
        price,
        "1",
        int((opened + timedelta(minutes=interval_minutes)).timestamp() * 1000) - 1,
        "100",
    ]


class OutcomeMarket:
    def __init__(self, *, complete_minutes: bool = True) -> None:
        self.calls: list[tuple[str, datetime, datetime, int]] = []
        self.complete_minutes = complete_minutes

    async def historical_klines(
        self, symbol, interval, start, end, *, max_candles=10_000
    ):
        self.calls.append((interval, start, end, max_candles))
        if interval == "5m":
            return [_outcome_row(start, 5, "97", "102")]
        rows = [
            _outcome_row(start, 1, "100.5", "102"),
            _outcome_row(start + timedelta(minutes=1), 1, "97", "99.5", price="99"),
            _outcome_row(start + timedelta(minutes=2), 1, "99", "100", price="99.5"),
            _outcome_row(start + timedelta(minutes=3), 1, "99", "100", price="99.5"),
            _outcome_row(start + timedelta(minutes=4), 1, "99", "100", price="99.5"),
        ]
        return rows if self.complete_minutes else rows[:-1]


def test_market_outcome_fetches_minutes_only_for_ambiguous_window() -> None:
    async def scenario():
        market = OutcomeMarket()
        outcome = await evaluate_outcome_from_market(
            market,
            symbol="BTCUSDT",
            analysis=MarketAnalysis.model_validate(_analysis_payload()),
            completed_at=datetime(2026, 7, 22, 9, 58, tzinfo=UTC),
            end=datetime(2026, 7, 22, 10, 10, tzinfo=UTC),
        )
        return market, outcome

    market, outcome = asyncio.run(scenario())
    assert outcome.status == "stopped"
    assert [(call[0], call[3]) for call in market.calls] == [("5m", 100_000), ("1m", 5)]
    assert market.calls[1][1] == datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    assert market.calls[1][2] == datetime(2026, 7, 22, 10, 5, tzinfo=UTC)


def test_market_outcome_does_not_guess_with_incomplete_minute_window() -> None:
    async def scenario():
        market = OutcomeMarket(complete_minutes=False)
        return await evaluate_outcome_from_market(
            market,
            symbol="BTCUSDT",
            analysis=MarketAnalysis.model_validate(_analysis_payload()),
            completed_at=datetime(2026, 7, 22, 9, 58, tzinfo=UTC),
            end=datetime(2026, 7, 22, 10, 10, tzinfo=UTC),
        )

    outcome = asyncio.run(scenario())
    assert outcome.status == "ambiguous"
    assert "完整 1 分钟 K 线不足" in outcome.detail


def _rows(interval_minutes: int, count: int) -> list[list[object]]:
    end = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=interval_minutes)
    start = end - timedelta(minutes=interval_minutes * (count - 1))
    rows: list[list[object]] = []
    for index in range(count):
        opened = start + timedelta(minutes=interval_minutes * index)
        price = 100 + index / 10
        rows.append(
            [
                int(opened.timestamp() * 1000),
                str(price),
                str(price + 1),
                str(price - 1),
                str(price + 0.2),
                "10",
                int((opened + timedelta(minutes=interval_minutes)).timestamp() * 1000) - 1,
                "1000",
            ]
        )
    return rows


class AnalysisMarket:
    def __init__(self) -> None:
        self.kline_calls: list[tuple[str, int]] = []

    async def klines(self, symbol: str, interval: str, limit: int):
        self.kline_calls.append((interval, limit))
        minutes = {"5m": 5, "15m": 15, "1h": 60}[interval]
        return _rows(minutes, limit)

    async def book_ticker(self, symbol: str):
        return {"bidPrice": "100", "askPrice": "100.1"}

    async def premium_index(self, symbol: str):
        return {
            "markPrice": "100.05",
            "indexPrice": "100",
            "lastFundingRate": "0.0001",
            "nextFundingTime": str(int((datetime.now(UTC) + timedelta(hours=1)).timestamp() * 1000)),
        }

    async def depth(self, symbol: str, limit: int):
        return {"bids": [["100", "2"]], "asks": [["101", "1"]]}

    async def open_interest(self, symbol: str):
        return {"openInterest": "500"}

    async def agg_trades(self, symbol: str, limit: int):
        return [{"p": "100", "q": "1", "m": False}, {"p": "101", "q": "1", "m": True}]

    async def ticker_24h(self, symbol: str):
        return {"priceChangePercent": "2", "quoteVolume": "1000000"}

    async def derivatives_positioning(self, symbol: str):
        return {
            "open_interest_change_5m": 0.02,
            "global_long_short_ratio": 1.1,
            "global_long_short_ratio_change_5m": -0.03,
            "top_long_short_position_ratio": 1.2,
            "top_long_short_position_ratio_change_5m": 0.01,
            "taker_buy_sell_ratio": 1.05,
            "taker_buy_sell_ratio_change_5m": 0.04,
        }

    async def historical_klines(self, symbol, interval, start, end, *, max_candles=100_000):
        return []

    async def close(self):
        return None


class AnalysisOptions:
    async def context(self, symbol):
        return {
            "source": "fixture",
            "available": True,
            "direct": {"underlying": symbol.removesuffix("USDT"), "available": True},
            "benchmark_underlyings": ["BTC", "ETH"],
            "snapshots": {},
        }


def test_data_pack_uses_only_kansoku_timeframes_and_frozen_raw_bars() -> None:
    async def scenario():
        market = AnalysisMarket()
        result = await AnalysisDataPackBuilder(  # type: ignore[arg-type]
            market,
            options=AnalysisOptions(),
        ).build("BTCUSDT", account=None, previous_analysis=None)
        return market, result

    market, result = asyncio.run(scenario())
    assert market.kline_calls == [("5m", 150), ("15m", 500), ("1h", 150)]
    assert set(result["timeframes"]) == {"5m", "15m", "1h"}
    assert all(len(frame["bars"]) == 60 for frame in result["timeframes"].values())
    assert result["timeframes"]["5m"]["summary"]["emas"] == [
        pytest.approx({"period": 9, "last": 114.7}),
        pytest.approx({"period": 21, "last": 114.1}),
        pytest.approx({"period": 55, "last": 112.4}),
    ]
    assert "news" in result["unavailable_inputs"]
    assert "options_levels" not in result["unavailable_inputs"]
    assert result["options_context"]["source"] == "fixture"
    positioning = result["derivatives"]["positioning_statistics_5m"]
    assert positioning["availability"] == "complete"
    assert positioning["missing_fields"] == []
    assert positioning["values"]["open_interest_change_5m"] == 0.02


class StaticBuilder:
    async def build(self, symbol, *, account, previous_analysis):
        return {"data_version": "test", "symbol": symbol}


class AnalysisProvider(DecisionProvider):
    name = "analysis-fixture"
    model = "fixture-model"
    reasoning_effort = "medium"

    async def health_check(self):
        return ProviderHealth(provider=self.name, available=True, authenticated=True)

    async def generate_trade_intent(self, snapshot, portfolio):
        intent = TradeIntent.hold(snapshot.symbol, snapshot.cadence, "fixture")
        return ProviderResult(
            intent=intent,
            provider=self.name,
            model=self.model,
            duration=timedelta(0),
            raw_output=intent.model_dump_json(),
            usage={},
        )

    async def generate_structured_output(self, *, prompt, output_schema):
        return StructuredOutputResult(
            provider=self.name,
            model=self.model,
            duration=timedelta(milliseconds=12),
            raw_output=__import__("json").dumps(_analysis_payload()),
            usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            reasoning_effort=self.reasoning_effort,
        )


class SlowAnalysisProvider(AnalysisProvider):
    async def generate_structured_output(self, *, prompt, output_schema):
        await asyncio.sleep(60)
        return await super().generate_structured_output(
            prompt=prompt, output_schema=output_schema
        )


class AssistedAnalysisProvider(AnalysisProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def generate_structured_output(self, *, prompt, output_schema):
        self.calls += 1
        decisions = []
        for symbol in ("BTCUSDT", "ETHUSDT"):
            analysis = _analysis_payload()
            analysis["scenarios"][0]["probability"] = 0.6  # type: ignore[index]
            analysis["scenarios"][1]["probability"] = 0.4  # type: ignore[index]
            decisions.append(
                {
                    "symbol": symbol,
                    "analysis": analysis,
                    "execution": {
                        "confidence": 0.8,
                        "order_type": "LIMIT",
                        "ttl_seconds": 120,
                        "setup_type": "BREAKOUT_RETEST",
                        "trigger_type": "RECLAIM",
                        "invalidation_type": "SWING",
                        "invalidation_level": 98,
                        "target_type": "SWING",
                    },
                }
            )
        return StructuredOutputResult(
            provider=self.name,
            model=self.model,
            duration=timedelta(milliseconds=20),
            raw_output=__import__("json").dumps({"decisions": decisions}),
            usage={"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
            reasoning_effort=self.reasoning_effort,
        )


class InvalidAssistedAnalysisProvider(AssistedAnalysisProvider):
    async def generate_structured_output(self, *, prompt, output_schema):
        response = await super().generate_structured_output(
            prompt=prompt, output_schema=output_schema
        )
        payload = __import__("json").loads(response.raw_output)
        for decision in payload["decisions"]:
            decision["analysis"]["scenarios"][0]["probability"] = 0.2
            decision["analysis"]["scenarios"][1]["probability"] = 0.2
        return StructuredOutputResult(
            provider=response.provider,
            model=response.model,
            duration=response.duration,
            raw_output=__import__("json").dumps(payload),
            usage=response.usage,
            provider_version=response.provider_version,
            reasoning_effort=response.reasoning_effort,
        )


def test_analysis_service_persists_frozen_input_and_validated_result(tmp_path: Path) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'analysis.db'}")
        await database.initialize()
        repository = MarketAnalysisRepository(database.sessions)

        async def account():
            return None

        service = MarketAnalysisService(
            builder=StaticBuilder(),  # type: ignore[arg-type]
            repository=repository,
            account_loader=account,
        )
        provider = AnalysisProvider()
        identifier = await service.create(symbol="BTCUSDT", provider=provider)  # type: ignore[arg-type]
        await service.run(identifier, symbol="BTCUSDT", provider=provider)  # type: ignore[arg-type]
        row = await repository.get(identifier, include_audit=True)
        await database.close()
        return row

    row = asyncio.run(scenario())
    assert row is not None
    assert row["status"] == "succeeded"
    assert row["result"]["reward_risk"]["target1"] == 1
    assert row["input"] == {"data_version": "test", "symbol": "BTCUSDT"}
    assert row["usage"]["total_tokens"] == 30
    assert row["prompt_version"] == "market-analysis-v4"
    assert row["result"]["summary"] == "15 分钟结构偏强，但仍需 1 小时周期确认。"


def test_assisted_bridge_uses_one_batch_call_and_persists_split_shadow_rows(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'assisted.db'}")
        await database.initialize()
        repository = MarketAnalysisRepository(database.sessions)
        bridge = AnalysisDecisionBridge(
            builder=StaticBuilder(),  # type: ignore[arg-type]
            repository=repository,
        )
        provider = AssistedAnalysisProvider()
        snapshots = [
            MarketSnapshot(
                symbol=symbol,
                cadence="15m",
                timestamp=datetime.now(UTC),
                mark_price="100",
                bid="99.9",
                ask="100.1",
                quote_volume_24h="1000000",
            )
            for symbol in ("BTCUSDT", "ETHUSDT")
        ]
        results = await bridge.generate(
            provider=provider,
            snapshots=snapshots,
            portfolio=PortfolioState(equity="10000", available_balance="8000"),
            persist=True,
        )
        rows = await repository.recent(limit=10)
        await database.close()
        return provider.calls, results, rows

    calls, results, rows = asyncio.run(scenario())
    assert calls == 1
    assert [item.intent.symbol for item in results] == ["BTCUSDT", "ETHUSDT"]
    assert all(item.intent.leverage == 1 for item in results)
    assert all(item.intent.take_profit == Decimal("104") for item in results)
    assert {row["usage"]["batch_index"] for row in rows} == {1, 2}
    assert all(row["usage"]["analysis_decision_mode"] == "shadow" for row in rows)
    assert all(row["usage"]["shadow_target2"] == 108 for row in rows)
    assert sum(row["usage"]["total_tokens"] for row in rows) == 18


def test_assisted_bridge_keeps_raw_audit_but_exposes_compact_validation_error(
    tmp_path: Path,
) -> None:
    async def scenario():
        database = Database(f"sqlite+aiosqlite:///{tmp_path / 'invalid-assisted.db'}")
        await database.initialize()
        repository = MarketAnalysisRepository(database.sessions)
        bridge = AnalysisDecisionBridge(
            builder=StaticBuilder(),  # type: ignore[arg-type]
            repository=repository,
        )
        snapshots = [
            MarketSnapshot(
                symbol=symbol,
                cadence="15m",
                timestamp=datetime.now(UTC),
                mark_price="100",
                bid="99.9",
                ask="100.1",
                quote_volume_24h="1000000",
            )
            for symbol in ("BTCUSDT", "ETHUSDT")
        ]
        with pytest.raises(ProviderInvocationError) as caught:
            await bridge.generate(
                provider=InvalidAssistedAnalysisProvider(),
                snapshots=snapshots,
                portfolio=PortfolioState(equity="10000", available_balance="8000"),
                persist=False,
            )
        await database.close()
        return caught.value

    error = asyncio.run(scenario())
    assert "2 validation errors" in str(error)
    assert "scenario probabilities must total 100%" in str(error)
    assert "input_value" not in str(error)
    assert "errors.pydantic.dev" not in str(error)
    assert error.raw_output.startswith('{"decisions"')


def test_market_analysis_api_runs_selected_provider_and_returns_audit(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'analysis-api.db'}")
    market = AnalysisMarket()
    provider = AnalysisProvider()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([provider]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    engine.select_provider_chain([provider.name])
    engine.candidates = [
        Candidate(
            symbol=symbol,
            score=Decimal(str(1 - index / 10)),
            volume_rank=index + 1,
            spread_bps=Decimal("1"),
            volatility=Decimal("0.02"),
            trend_strength=Decimal("0.01"),
        )
        for index, symbol in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT"))
    ]
    app = create_app(
        database=database,
        market=market,
        engine=engine,
        options_context_provider=AnalysisOptions(),
    )  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.post("/api/market-analyses", json={"symbol": "BTCUSDT"})
        assert response.status_code == 202
        identifier = response.json()["id"]
        for _ in range(50):
            detail = client.get(f"/api/market-analyses/{identifier}").json()
            if detail["status"] not in {"pending", "running"}:
                break
            __import__("time").sleep(0.01)
        assert detail["status"] == "succeeded", detail
        assert detail["result"]["direction"] == "long"
        assert set(detail["input"]["timeframes"]) == {"5m", "15m", "1h"}
        history = client.get("/api/market-analyses").json()
        assert history[0]["id"] == identifier
        assert "input" not in history[0]
        outcome = client.post(f"/api/market-analyses/{identifier}/outcome")
        assert outcome.status_code == 200
        assert outcome.json()["outcome"]["status"] == "waiting_entry"
        batch_outcomes = client.post(
            "/api/market-analyses/outcomes",
            json={"analysis_ids": [identifier, identifier, 999_999]},
        )
        assert batch_outcomes.status_code == 200
        assert batch_outcomes.json()["updated_ids"] == [identifier]
        assert batch_outcomes.json()["errors"] == [
            {
                "id": 999_999,
                "status_code": 404,
                "detail": "market analysis not found",
            }
        ]
        repository = MarketAnalysisRepository(database.sessions)
        assert client.portal is not None
        client.portal.call(
            repository.save_outcome,
            identifier,
            {
                "status": "target2",
                "bars_observed": 4,
                "entry_at": "2026-07-22T10:05:00Z",
                "target1_at": "2026-07-22T10:10:00Z",
                "resolved_at": "2026-07-22T10:15:00Z",
                "detail": "T1 部分止盈后，剩余仓位触及 T2",
            },
        )
        performance = client.get(
            "/api/market-analyses/performance",
            params={"fixed_notional_usdt": 100, "fixed_risk_usdt": 10},
        )
        assert performance.status_code == 200
        assert performance.json()["settled_trades"] == 1
        assert performance.json()["wins"] == 1
        assert performance.json()["fixed_notional"]["total_pnl_usdt"] == pytest.approx(
            500 / 101
        )
        assert performance.json()["fixed_risk"]["total_pnl_usdt"] == pytest.approx(
            50 / 3
        )
        assert client.get(
            "/api/market-analyses/performance?fixed_notional_usdt=0"
        ).status_code == 422
        refreshed_history = client.get("/api/market-analyses").json()
        assert refreshed_history[0]["outcome"]["status"] == "target2"

        selection = client.post(
            "/api/candidates-per-cycle", json={"candidates_per_cycle": 2}
        )
        assert selection.status_code == 200
        batch = client.post("/api/market-analyses/batch")
        assert batch.status_code == 202
        queued = batch.json()["analyses"]
        assert [item["symbol"] for item in queued] == ["BTCUSDT", "ETHUSDT"]
        for item in queued:
            for _ in range(50):
                detail = client.get(f"/api/market-analyses/{item['id']}").json()
                if detail["status"] not in {"pending", "running"}:
                    break
                __import__("time").sleep(0.01)
            assert detail["status"] == "succeeded", detail


def test_automatic_analysis_skips_unresolved_and_allows_ambiguous(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'analysis-schedule.db'}")
    market = AnalysisMarket()
    provider = AnalysisProvider()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([provider]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    engine.select_provider_chain([provider.name])
    engine.candidates = [
        Candidate(
            symbol=symbol,
            score=Decimal(str(1 - index / 10)),
            volume_rank=index + 1,
            spread_bps=Decimal("1"),
            volatility=Decimal("0.02"),
            trend_strength=Decimal("0.01"),
        )
        for index, symbol in enumerate(("BTCUSDT", "ETHUSDT"))
    ]
    app = create_app(
        database=database,
        market=market,
        engine=engine,
        options_context_provider=AnalysisOptions(),
    )  # type: ignore[arg-type]
    with TestClient(app) as client:
        created = client.post(
            "/api/market-analyses", json={"symbol": "BTCUSDT"}
        ).json()
        identifier = created["id"]
        for _ in range(50):
            detail = client.get(f"/api/market-analyses/{identifier}").json()
            if detail["status"] == "succeeded":
                break
            __import__("time").sleep(0.01)
        assert client.post(
            f"/api/market-analyses/{identifier}/outcome"
        ).json()["outcome"]["status"] == "waiting_entry"

        started = client.post("/api/market-analyses/schedule/start")
        assert started.status_code == 200
        assert started.json()["enabled"] is True
        assert started.json()["interval_minutes"] == 15
        assert started.json()["next_run_at"] is not None

        assert client.portal is not None
        client.portal.call(app.state.analysis_scheduler.run_now)
        status = client.get("/api/market-analyses/schedule").json()
        assert status["last_result"]["status"] == "completed"
        assert [item["symbol"] for item in status["last_result"]["queued"]] == [
            "ETHUSDT"
        ]
        assert status["last_result"]["skipped"] == [
            {
                "symbol": "BTCUSDT",
                "analysis_id": identifier,
                "outcome": "waiting_entry",
                "reason": "最近一份方向计划尚未了结",
            }
        ]
        history = client.get("/api/market-analyses?limit=30").json()
        assert [item["symbol"] for item in history[:2]] == ["ETHUSDT", "BTCUSDT"]

        repository = MarketAnalysisRepository(database.sessions)
        client.portal.call(
            repository.save_outcome,
            identifier,
            {
                "status": "ambiguous",
                "bars_observed": 3,
                "entry_at": "2026-07-22T10:05:00Z",
                "target1_at": None,
                "resolved_at": "2026-07-22T10:15:00Z",
                "detail": "同一根 K 线内无法确定计划价位触发顺序",
            },
        )
        client.portal.call(app.state.analysis_scheduler.run_now)
        second_round = client.get("/api/market-analyses/schedule").json()[
            "last_result"
        ]
        assert [item["symbol"] for item in second_round["queued"]] == ["BTCUSDT"]
        assert [item["symbol"] for item in second_round["skipped"]] == ["ETHUSDT"]

        stopped = client.post("/api/market-analyses/schedule/stop")
        assert stopped.status_code == 200
        assert stopped.json()["enabled"] is False
        assert stopped.json()["next_run_at"] is None


def test_cancelling_one_batch_item_cancels_the_whole_queue(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'analysis-cancel.db'}")
    market = AnalysisMarket()
    provider = SlowAnalysisProvider()
    engine = TradingEngine(
        testnet_broker=FakeTestnetBroker(),  # type: ignore[arg-type]
        providers=ProviderRegistry([provider]),
        audit=AuditRepository(database.sessions),
        market=market,  # type: ignore[arg-type]
    )
    engine.select_provider_chain([provider.name])
    engine.candidates = [
        Candidate(
            symbol=symbol,
            score=Decimal("1"),
            volume_rank=index + 1,
            spread_bps=Decimal("1"),
            volatility=Decimal("0.02"),
            trend_strength=Decimal("0.01"),
        )
        for index, symbol in enumerate(("BTCUSDT", "ETHUSDT"))
    ]
    app = create_app(
        database=database,
        market=market,
        engine=engine,
        options_context_provider=AnalysisOptions(),
    )  # type: ignore[arg-type]
    with TestClient(app) as client:
        queued = client.post("/api/market-analyses/batch").json()["analyses"]
        cancellation = client.post(
            f"/api/market-analyses/{queued[1]['id']}/cancel"
        )
        assert cancellation.status_code == 200
        assert [
            client.get(f"/api/market-analyses/{item['id']}").json()["status"]
            for item in queued
        ] == ["cancelled", "cancelled"]
