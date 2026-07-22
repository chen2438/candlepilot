import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from candlepilot.analysis.rejected_outcomes import (
    RejectedDecisionPlan,
    evaluate_rejected_decision,
    evaluate_rejected_decision_from_market,
)
from candlepilot.domain.models import OrderType
from candlepilot.market.features import Kline


START = datetime(2026, 7, 22, 10, 1, tzinfo=UTC)


def _plan(*, order_type: OrderType = OrderType.MARKET) -> RejectedDecisionPlan:
    return RejectedDecisionPlan(
        direction="LONG",
        order_type=order_type,
        entry_price=Decimal("100"),
        stop_loss=Decimal("98"),
        take_profit=Decimal("104"),
        price_source="pre_trade",
        evaluated_at=START,
        ttl_seconds=120,
    )


def _bar(opened: datetime, low: str, high: str) -> Kline:
    return Kline(
        open_time=opened,
        open=Decimal("100"),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal("100"),
        volume=Decimal("0"),
        quote_volume=Decimal("0"),
    )


def test_market_counterfactual_records_take_profit() -> None:
    observed = datetime(2026, 7, 22, 10, 2, tzinfo=UTC)
    outcome = evaluate_rejected_decision(
        _plan(),
        [_bar(observed, "99", "104")],
        observation_started_at=observed,
        minute_refinements={observed: [_bar(observed, "99", "104")]},
    )

    assert outcome.status == "take_profit"
    assert outcome.entry_at == START
    assert outcome.resolved_at == observed
    assert outcome.price_basis == "mark_price"


def test_counterfactual_never_guesses_when_stop_and_target_share_a_minute() -> None:
    observed = datetime(2026, 7, 22, 10, 2, tzinfo=UTC)
    outcome = evaluate_rejected_decision(
        _plan(),
        [_bar(observed, "98", "104")],
        observation_started_at=observed,
        minute_refinements={observed: [_bar(observed, "98", "104")]},
    )

    assert outcome.status == "ambiguous"
    assert "1 分钟" in outcome.detail


def test_limit_counterfactual_expires_without_a_confirmed_entry() -> None:
    observed = datetime(2026, 7, 22, 10, 2, tzinfo=UTC)
    outcome = evaluate_rejected_decision(
        _plan(order_type=OrderType.LIMIT),
        [
            _bar(observed, "101", "102"),
            _bar(observed + timedelta(minutes=1), "101", "102"),
        ],
        observation_started_at=observed,
        minute_refinements={
            observed: [_bar(observed, "101", "102")],
            observed + timedelta(minutes=1): [
                _bar(observed + timedelta(minutes=1), "101", "102")
            ],
        },
    )

    assert outcome.status == "expired_unfilled"
    assert outcome.entry_at is None


def _row(opened: datetime, minutes: int, low: str, high: str) -> list[object]:
    return [
        int(opened.timestamp() * 1000),
        "100",
        high,
        low,
        "100",
        "0",
        int((opened + timedelta(minutes=minutes)).timestamp() * 1000) - 1,
        "0",
    ]


class MarkOutcomeMarket:
    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime, datetime]] = []

    async def historical_mark_price_klines(
        self, symbol, interval, start, end, *, max_candles=10_000
    ):
        self.calls.append((interval, start, end))
        if interval == "5m":
            return [_row(start, 5, "98", "104")]
        if end - start == timedelta(minutes=5):
            return [
                _row(start + timedelta(minutes=index), 1, "99", "101")
                if index < 2
                else _row(start + timedelta(minutes=index), 1, "103", "104")
                for index in range(5)
            ]
        return [_row(start, 1, "99", "101") for _ in range(3)]


def test_market_counterfactual_uses_mark_prices_and_refines_ambiguous_5m() -> None:
    async def scenario():
        market = MarkOutcomeMarket()
        outcome = await evaluate_rejected_decision_from_market(
            market,
            symbol="BTCUSDT",
            plan=_plan(),
            end=datetime(2026, 7, 22, 10, 10, tzinfo=UTC),
        )
        return market, outcome

    market, outcome = asyncio.run(scenario())

    assert outcome.status == "take_profit"
    assert outcome.price_basis == "mark_price"
    assert [call[0] for call in market.calls] == ["1m", "5m", "1m"]
