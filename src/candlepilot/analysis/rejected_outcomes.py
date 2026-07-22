from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict

from candlepilot.domain.models import OrderType
from candlepilot.market.features import Kline
from candlepilot.analysis.outcomes import parse_closed_rows


class RejectedDecisionOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal[
        "waiting_entry",
        "active",
        "take_profit",
        "stop_loss",
        "expired_unfilled",
        "invalidated_before_entry",
        "target_before_entry",
        "ambiguous",
    ]
    direction: Literal["LONG", "SHORT"]
    order_type: OrderType
    entry_price: Decimal | None
    stop_loss: Decimal
    take_profit: Decimal
    price_source: Literal["pre_trade", "intent"]
    price_basis: Literal["mark_price", "contract_price"]
    bars_observed: int
    observation_started_at: datetime
    observed_until: datetime | None = None
    entry_at: datetime | None = None
    resolved_at: datetime | None = None
    detail: str


@dataclass(frozen=True, slots=True)
class RejectedDecisionPlan:
    direction: Literal["LONG", "SHORT"]
    order_type: OrderType
    entry_price: Decimal | None
    stop_loss: Decimal
    take_profit: Decimal
    price_source: Literal["pre_trade", "intent"]
    evaluated_at: datetime
    ttl_seconds: int

    def __post_init__(self) -> None:
        if self.evaluated_at.tzinfo is None:
            raise ValueError("evaluated_at must be timezone-aware")
        if self.stop_loss <= 0 or self.take_profit <= 0:
            raise ValueError("protective prices must be positive")
        if self.order_type == OrderType.LIMIT and self.entry_price is None:
            raise ValueError("limit counterfactual requires an entry price")
        if self.direction == "LONG" and self.stop_loss >= self.take_profit:
            raise ValueError("long counterfactual stop must be below take profit")
        if self.direction == "SHORT" and self.stop_loss <= self.take_profit:
            raise ValueError("short counterfactual stop must be above take profit")


class HistoricalMarkPriceSource(Protocol):
    async def historical_mark_price_klines(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        *,
        max_candles: int = 10_000,
    ) -> list[list[Any]]: ...


@dataclass(slots=True)
class _State:
    active: bool
    entry_at: datetime | None


def _next_complete_minute(value: datetime) -> datetime:
    value = value.astimezone(UTC)
    minute = value.replace(second=0, microsecond=0)
    return minute if value == minute else minute + timedelta(minutes=1)


def _next_five_minute_boundary(value: datetime) -> datetime:
    value = value.astimezone(UTC).replace(second=0, microsecond=0)
    remainder = value.minute % 5
    return value if remainder == 0 else value + timedelta(minutes=5 - remainder)


def _touches(bar: Kline, price: Decimal) -> bool:
    return bar.low <= price <= bar.high


def _outcome(
    plan: RejectedDecisionPlan,
    *,
    status: str,
    bars_observed: int,
    observation_started_at: datetime,
    price_basis: Literal["mark_price", "contract_price"],
    state: _State,
    detail: str,
    observed_until: datetime | None,
    resolved_at: datetime | None = None,
) -> RejectedDecisionOutcome:
    return RejectedDecisionOutcome(
        status=status,
        direction=plan.direction,
        order_type=plan.order_type,
        entry_price=plan.entry_price,
        stop_loss=plan.stop_loss,
        take_profit=plan.take_profit,
        price_source=plan.price_source,
        price_basis=price_basis,
        bars_observed=bars_observed,
        observation_started_at=observation_started_at,
        observed_until=observed_until,
        entry_at=state.entry_at,
        resolved_at=resolved_at,
        detail=detail,
    )


def evaluate_rejected_decision(
    plan: RejectedDecisionPlan,
    bars: list[Kline],
    *,
    observation_started_at: datetime,
    price_basis: Literal["mark_price", "contract_price"] = "mark_price",
    minute_refinements: dict[datetime, list[Kline]] | None = None,
    observation_end: datetime | None = None,
) -> RejectedDecisionOutcome:
    state = _State(
        active=plan.order_type == OrderType.MARKET,
        entry_at=plan.evaluated_at if plan.order_type == OrderType.MARKET else None,
    )
    expiry = plan.evaluated_at + timedelta(seconds=plan.ttl_seconds)
    refinements = minute_refinements or {}
    observed = 0
    observed_until: datetime | None = None
    for base_bar in bars:
        observed += 1
        evaluation_bars = refinements.get(base_bar.open_time, [base_bar])
        interval_label = "1 分钟" if base_bar.open_time in refinements else "5 分钟"
        for bar in evaluation_bars:
            bar_end = bar.open_time + timedelta(
                minutes=1 if interval_label == "1 分钟" else 5
            )
            if not state.active and bar_end > expiry:
                return _outcome(
                    plan,
                    status="expired_unfilled",
                    bars_observed=observed - 1,
                    observation_started_at=observation_started_at,
                    price_basis=price_basis,
                    state=state,
                    observed_until=observed_until,
                    resolved_at=expiry,
                    detail="限价决策在原 TTL 内没有形成可确认入场，反事实观察已结束",
                )
            observed_until = bar_end
            stop = _touches(bar, plan.stop_loss)
            target = _touches(bar, plan.take_profit)
            if not state.active:
                assert plan.entry_price is not None
                entry = _touches(bar, plan.entry_price)
                if entry and (stop or target):
                    return _outcome(
                        plan,
                        status="ambiguous",
                        bars_observed=observed,
                        observation_started_at=observation_started_at,
                        price_basis=price_basis,
                        state=state,
                        observed_until=observed_until,
                        resolved_at=bar.open_time,
                        detail=f"入场价与退出价位在同一根完整 {interval_label}标记价格 K 线内触及，无法确定顺序",
                    )
                if not entry and stop and target:
                    return _outcome(
                        plan,
                        status="ambiguous",
                        bars_observed=observed,
                        observation_started_at=observation_started_at,
                        price_basis=price_basis,
                        state=state,
                        observed_until=observed_until,
                        resolved_at=bar.open_time,
                        detail=f"入场前止损与止盈在同一根完整 {interval_label}标记价格 K 线内触及，无法确定顺序",
                    )
                if not entry and stop:
                    return _outcome(
                        plan,
                        status="invalidated_before_entry",
                        bars_observed=observed,
                        observation_started_at=observation_started_at,
                        price_basis=price_basis,
                        state=state,
                        observed_until=observed_until,
                        resolved_at=bar.open_time,
                        detail="限价决策尚未入场，价格先触及止损",
                    )
                if not entry and target:
                    return _outcome(
                        plan,
                        status="target_before_entry",
                        bars_observed=observed,
                        observation_started_at=observation_started_at,
                        price_basis=price_basis,
                        state=state,
                        observed_until=observed_until,
                        resolved_at=bar.open_time,
                        detail="限价决策尚未入场，价格先触及止盈",
                    )
                if entry:
                    state.active = True
                    state.entry_at = bar.open_time
                continue
            if stop and target:
                return _outcome(
                    plan,
                    status="ambiguous",
                    bars_observed=observed,
                    observation_started_at=observation_started_at,
                    price_basis=price_basis,
                    state=state,
                    observed_until=observed_until,
                    resolved_at=bar.open_time,
                    detail=f"止损与止盈在同一根完整 {interval_label}标记价格 K 线内触及，无法确定顺序",
                )
            if stop:
                return _outcome(
                    plan,
                    status="stop_loss",
                    bars_observed=observed,
                    observation_started_at=observation_started_at,
                    price_basis=price_basis,
                    state=state,
                    observed_until=observed_until,
                    resolved_at=bar.open_time,
                    detail="假设风控放行并入场，随后标记价格触及固定止损",
                )
            if target:
                return _outcome(
                    plan,
                    status="take_profit",
                    bars_observed=observed,
                    observation_started_at=observation_started_at,
                    price_basis=price_basis,
                    state=state,
                    observed_until=observed_until,
                    resolved_at=bar.open_time,
                    detail="假设风控放行并入场，随后标记价格触及固定止盈",
                )
    if not state.active and observation_end is not None and observation_end >= expiry:
        return _outcome(
            plan,
            status="expired_unfilled",
            bars_observed=observed,
            observation_started_at=observation_started_at,
            price_basis=price_basis,
            state=state,
            observed_until=observed_until,
            resolved_at=expiry,
            detail="限价决策在原 TTL 内没有形成可确认入场，反事实观察已结束",
        )
    return _outcome(
        plan,
        status="active" if state.active else "waiting_entry",
        bars_observed=observed,
        observation_started_at=observation_started_at,
        price_basis=price_basis,
        state=state,
        observed_until=observed_until,
        detail=(
            "假设风控放行并入场，尚未触及固定止损或止盈"
            if state.active
            else "限价决策尚未在原 TTL 内触及入场价"
        ),
    )


async def evaluate_rejected_decision_from_market(
    market: HistoricalMarkPriceSource,
    *,
    symbol: str,
    plan: RejectedDecisionPlan,
    end: datetime | None = None,
) -> RejectedDecisionOutcome:
    end = (end or datetime.now(UTC)).astimezone(UTC)
    start = _next_complete_minute(plan.evaluated_at)
    boundary = _next_five_minute_boundary(start)
    source = getattr(market, "historical_mark_price_klines", None)
    price_basis: Literal["mark_price", "contract_price"] = "mark_price"
    if source is None:
        source = getattr(market, "historical_klines")
        price_basis = "contract_price"

    prefix: list[Kline] = []
    if start < min(boundary, end):
        prefix = parse_closed_rows(
            await source(
                symbol,
                "1m",
                start,
                min(boundary, end),
                max_candles=5,
            ),
            now=end,
        )
    main: list[Kline] = []
    if boundary < end:
        main = parse_closed_rows(
            await source(
                symbol,
                "5m",
                boundary,
                end,
                max_candles=100_000,
            ),
            now=end,
        )
    bars = prefix + main
    refinements: dict[datetime, list[Kline]] = {
        bar.open_time: [bar] for bar in prefix
    }
    main_times = {bar.open_time for bar in main}
    while True:
        outcome = evaluate_rejected_decision(
            plan,
            bars,
            observation_started_at=start,
            price_basis=price_basis,
            minute_refinements=refinements,
            observation_end=end,
        )
        if (
            outcome.status != "ambiguous"
            or outcome.resolved_at is None
            or outcome.resolved_at not in main_times
            or outcome.resolved_at in refinements
        ):
            return outcome
        window = outcome.resolved_at
        minutes = parse_closed_rows(
            await source(
                symbol,
                "1m",
                window,
                window + timedelta(minutes=5),
                max_candles=5,
            ),
            now=end,
        )
        expected = [window + timedelta(minutes=index) for index in range(5)]
        if len(minutes) != 5 or [bar.open_time for bar in minutes] != expected:
            return outcome.model_copy(
                update={
                    "detail": f"{outcome.detail}；对应完整 1 分钟标记价格 K 线不足，无法细分"
                }
            )
        refinements[window] = minutes
