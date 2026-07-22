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
from candlepilot.analysis.models import MarketAnalysis, market_analysis_output_schema
from candlepilot.analysis.outcomes import evaluate_outcome, next_complete_5m_start
from candlepilot.analysis.service import MarketAnalysisService
from candlepilot.domain.models import ProviderHealth, TradeIntent
from candlepilot.market.features import Kline
from candlepilot.providers.base import DecisionProvider, ProviderResult, StructuredOutputResult
from candlepilot.providers.registry import ProviderRegistry
from candlepilot.storage.database import AuditRepository, Database, MarketAnalysisRepository
from conftest import FakeTestnetBroker


def _analysis_payload(direction: str = "long") -> dict[str, object]:
    base: dict[str, object] = {
        "direction": direction,
        "summary": "15m structure is constructive, with 1h confirmation still required.",
        "anchor": {
            "timeframe": "15m",
            "time": "2026-07-22T10:00:00+00:00",
            "price": 100,
            "reason": "latest confirmed 15m structure bar",
        },
        "scenarios": [
            {
                "name": "continuation",
                "probability": 60,
                "trigger": "15m closes above 101",
                "expected_path": "price tests 103 then 106",
                "invalidation": "15m closes below 98",
            },
            {
                "name": "range",
                "probability": 40,
                "trigger": "price remains below 101",
                "expected_path": "rotation between 98 and 101",
                "invalidation": "close outside the range",
            },
        ],
        "range_plan": None,
        "entry_plan": {
            "entry": 101,
            "stop": 98,
            "target1": 104,
            "target2": 108,
            "stop_structure": "below the confirmed 15m swing low",
            "entry_trigger": "15m close and 5m retest above 101",
            "management": "At T1 reduce half; move stop toward breakeven if structure holds.",
        },
        "key_evidence": ["15m EMA alignment", "1h MACD histogram improving"],
        "missing_data_impact": ["news and event risk are unknown"],
    }
    return base


def test_analysis_contract_requires_explicit_directional_levels() -> None:
    analysis = MarketAnalysis.model_validate(_analysis_payload())
    assert analysis.reward_risk() == {"target1": 1.0, "target2": 7 / 3}

    invalid = _analysis_payload()
    invalid["entry_plan"] = None
    with pytest.raises(ValidationError, match="requires an entry plan"):
        MarketAnalysis.model_validate(invalid)


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


def test_neutral_analysis_requires_range_containing_anchor() -> None:
    payload = _analysis_payload("neutral")
    payload["entry_plan"] = None
    payload["range_plan"] = {"low": 98, "high": 102, "tactic": "wait at the edges"}
    assert MarketAnalysis.model_validate(payload).direction == "neutral"
    payload["range_plan"] = {"low": 90, "high": 95, "tactic": "invalid range"}
    with pytest.raises(ValidationError, match="contain the anchor"):
        MarketAnalysis.model_validate(payload)


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


def test_outcome_ignores_levels_before_entry_and_tracks_t1_then_breakeven() -> None:
    analysis = MarketAnalysis.model_validate(_analysis_payload())
    outcome = evaluate_outcome(
        analysis,
        [
            _bar(0, "97", "99", opened="98"),  # stop before entry: irrelevant
            _bar(5, "100.5", "102"),
            _bar(10, "103", "105", opened="104"),
            _bar(15, "100", "102"),
        ],
    )
    assert outcome.status == "breakeven_after_target1"
    assert outcome.entry_at == datetime(2026, 7, 22, 10, 5, tzinfo=UTC)
    assert outcome.target1_at == datetime(2026, 7, 22, 10, 10, tzinfo=UTC)


def test_outcome_marks_unknowable_intrabar_order_and_starts_after_completion_bar() -> None:
    analysis = MarketAnalysis.model_validate(_analysis_payload())
    outcome = evaluate_outcome(analysis, [_bar(0, "97", "102")])
    assert outcome.status == "ambiguous"
    assert next_complete_5m_start(datetime(2026, 7, 22, 10, 0, 1, tzinfo=UTC)) == datetime(
        2026, 7, 22, 10, 5, tzinfo=UTC
    )


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

    async def historical_klines(self, symbol, interval, start, end, *, max_candles=100_000):
        return []

    async def close(self):
        return None


def test_data_pack_uses_only_kansoku_timeframes_and_frozen_raw_bars() -> None:
    async def scenario():
        market = AnalysisMarket()
        result = await AnalysisDataPackBuilder(market).build(  # type: ignore[arg-type]
            "BTCUSDT", account=None, previous_analysis=None
        )
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
    app = create_app(database=database, market=market, engine=engine)  # type: ignore[arg-type]
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
