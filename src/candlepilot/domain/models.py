from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


PositiveDecimal = Annotated[Decimal, Field(gt=0)]
NonNegativeDecimal = Annotated[Decimal, Field(ge=0)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class TradingMode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper-production-data"
    TESTNET = "binance-testnet"


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
    cadence: Literal["1m", "5m", "15m"]
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
    cadence: Literal["1m", "5m", "15m"]
    action: TradeAction
    confidence: float = Field(ge=0, le=1)
    leverage: int = Field(ge=1, le=10)
    risk_fraction: Decimal = Field(ge=0, le=Decimal("0.02"))
    order_type: OrderType = OrderType.MARKET
    entry_price: Decimal | None = Field(default=None, gt=0)
    stop_loss: Decimal | None = Field(default=None, gt=0)
    take_profit: Decimal | None = Field(default=None, gt=0)
    ttl_seconds: int = Field(default=60, ge=5, le=900)
    rationale: str = Field(min_length=1, max_length=500)

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
    def hold(cls, symbol: str, cadence: Literal["1m", "5m", "15m"], reason: str) -> TradeIntent:
        return cls(
            symbol=symbol,
            cadence=cadence,
            action=TradeAction.HOLD,
            confidence=0,
            leverage=1,
            risk_fraction=Decimal("0"),
            rationale=reason,
        )


class PortfolioState(StrictModel):
    equity: PositiveDecimal
    available_balance: NonNegativeDecimal
    daily_pnl: Decimal = Decimal("0")
    open_positions: int = Field(default=0, ge=0)
    margin_used: NonNegativeDecimal = Decimal("0")
    symbol_sides: dict[str, Literal["LONG", "SHORT"]] = Field(default_factory=dict)
    symbol_quantities: dict[str, PositiveDecimal] = Field(default_factory=dict)


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
    reduce_only: bool = False


class ExecutionReport(StrictModel):
    client_order_id: str
    status: Literal["NEW", "PARTIALLY_FILLED", "FILLED", "CANCELED", "REJECTED"]
    filled_quantity: NonNegativeDecimal = Decimal("0")
    average_price: Decimal | None = Field(default=None, gt=0)
    message: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
