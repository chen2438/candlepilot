from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PositiveDecimal = Annotated[Decimal, Field(gt=0)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]
RATIONALE_MAX_LENGTH = 1_000

#: A decision cadence. Every layer -- env parsing, the engine, the API and the
#: stored models -- validates against this one definition, so the set cannot
#: drift between the parser that accepts a value and the code that rejects it.
Cadence = Literal["5m", "15m", "30m"]
#: The same set as a tuple, in the canonical order cadences are reported in.
SUPPORTED_CADENCES: tuple[str, ...] = get_args(Cadence)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class TradeAction(StrEnum):
    HOLD = "HOLD"
    OPEN_LONG = "OPEN_LONG"
    OPEN_SHORT = "OPEN_SHORT"
    ADD = "ADD"
    REDUCE = "REDUCE"
    CLOSE = "CLOSE"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class ProviderHealth(StrictModel):
    provider: str
    available: bool
    authenticated: bool
    executable: str | None = None
    version: str | None = None
    detail: str = ""
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class MarketSnapshot(StrictModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]+USDT$")
    cadence: Cadence
    timestamp: datetime
    mark_price: PositiveDecimal
    bid: PositiveDecimal
    ask: PositiveDecimal
    quote_volume_24h: NonNegativeDecimal
    funding_rate: Decimal = Decimal("0")
    features: dict[str, float] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_market(self) -> MarketSnapshot:
        if self.timestamp.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        if self.ask < self.bid:
            raise ValueError("ask cannot be below bid")
        return self


class TradeIntent(StrictModel):
    symbol: str = Field(pattern=r"^[A-Z0-9]+USDT$")
    cadence: Cadence
    action: TradeAction
    confidence: float = Field(ge=0, le=1)
    leverage: int = Field(ge=1, le=10)
    risk_fraction: Decimal = Field(ge=0, le=Decimal("0.02"))
    order_type: OrderType = OrderType.MARKET
    entry_price: Decimal | None = Field(default=None, gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    ttl_seconds: int = Field(default=60, ge=5, le=900)
    rationale: str = Field(min_length=1, max_length=RATIONALE_MAX_LENGTH)

    @field_validator("risk_fraction", mode="before")
    @classmethod
    def decimal_from_number(cls, value: object) -> Decimal:
        return Decimal(str(value))

    @model_validator(mode="after")
    def validate_order_semantics(self) -> TradeIntent:
        opening = self.action in {
            TradeAction.OPEN_LONG,
            TradeAction.OPEN_SHORT,
            TradeAction.ADD,
        }
        if opening and self.stop_loss is None:
            raise ValueError("opening and add intents require stop_loss")
        if self.order_type == OrderType.LIMIT and self.entry_price is None:
            raise ValueError("limit intents require entry_price")
        if self.action == TradeAction.HOLD and self.risk_fraction != 0:
            raise ValueError("HOLD must have zero risk_fraction")
        return self

    @classmethod
    def hold(
        cls,
        symbol: str,
        cadence: Cadence,
        reason: str,
    ) -> TradeIntent:
        return cls(
            symbol=symbol,
            cadence=cadence,
            action=TradeAction.HOLD,
            confidence=0,
            leverage=1,
            risk_fraction=Decimal("0"),
            rationale=reason[:RATIONALE_MAX_LENGTH],
        )


class PositionState(StrictModel):
    """An open position as the decision model sees it.

    ``entry_price`` and the protective levels are what make ADD/REDUCE/CLOSE
    answerable: without them the model is asked whether an invalidation was
    reached while having no idea where the invalidation sits.
    """

    side: Literal["LONG", "SHORT"]
    quantity: PositiveDecimal
    entry_price: PositiveDecimal
    unrealized_pnl: Decimal = Decimal("0")
    leverage: int = Field(default=1, ge=1)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)


class PortfolioState(StrictModel):
    equity: PositiveDecimal
    available_balance: NonNegativeDecimal
    daily_pnl: Decimal = Decimal("0")
    open_positions: int = Field(default=0, ge=0)
    margin_used: NonNegativeDecimal = Decimal("0")
    positions: dict[str, PositionState] = Field(default_factory=dict)
    pending_entry_symbols: tuple[str, ...] = ()


class RiskDecision(StrictModel):
    accepted: bool
    reason: str
    max_quantity: Decimal | None = Field(default=None, ge=0)
    evaluated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OrderPlan(StrictModel):
    client_order_id: str
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: PositiveDecimal
    order_type: OrderType
    price: Decimal | None = Field(default=None, gt=0)
    stop_price: Decimal | None = Field(default=None, gt=0)
    take_profit_price: Decimal | None = Field(default=None, gt=0)
    reduce_only: bool = False


class ExecutionReport(StrictModel):
    client_order_id: str
    status: Literal[
        "NEW",
        "PARTIALLY_FILLED",
        "FILLED",
        "CANCELED",
        "REJECTED",
        "EXPIRED",
        "EXPIRED_IN_MATCH",
    ]
    filled_quantity: NonNegativeDecimal = Decimal("0")
    average_price: Decimal | None = Field(default=None, gt=0)
    message: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class ExecutionAttempt(StrictModel):
    inference_id: int = Field(gt=0)
    client_order_id: str | None = None
    status: Literal["SUCCEEDED", "FAILED", "RESCUED", "UNKNOWN"]
    stage: Literal["ENTRY", "PROTECTION", "RESCUE", "COMPLETE"]
    message: str
    exchange_error_code: int | None = None
    entry_report: ExecutionReport | None = None
    rescue_report: ExecutionReport | None = None
    estimated_loss_usdt: Decimal | None = Field(default=None, ge=0)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
