from __future__ import annotations

from datetime import datetime
from math import isfinite
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


Price = Annotated[float, Field(gt=0)]
CHINESE_TEXT = (
    "Use Simplified Chinese for this user-facing text. Keep symbols, timeframes, "
    "numbers, and standard market abbreviations unchanged."
)


class AnalysisModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalysisAnchor(AnalysisModel):
    timeframe: Literal["5m", "15m", "1h"]
    time: datetime
    price: Price
    reason: str = Field(min_length=1, max_length=500, description=CHINESE_TEXT)

    @model_validator(mode="after")
    def require_timezone(self) -> AnalysisAnchor:
        if self.time.tzinfo is None or self.time.utcoffset() is None:
            raise ValueError("anchor time must include a timezone")
        return self


class AnalysisScenario(AnalysisModel):
    name: str = Field(min_length=1, max_length=80, description=CHINESE_TEXT)
    probability: float = Field(
        ge=0,
        le=100,
        description="Probability in percentage points from 0 to 100; use 45, not 0.45.",
    )
    trigger: str = Field(min_length=1, max_length=500, description=CHINESE_TEXT)
    expected_path: str = Field(min_length=1, max_length=800, description=CHINESE_TEXT)
    invalidation: str = Field(min_length=1, max_length=500, description=CHINESE_TEXT)


class RangePlan(AnalysisModel):
    low: Price = Field(
        description="For neutral analysis, the range must contain the anchor price."
    )
    high: Price = Field(
        description="For neutral analysis, the range must contain the anchor price."
    )
    tactic: str = Field(min_length=1, max_length=800, description=CHINESE_TEXT)

    @model_validator(mode="after")
    def ordered(self) -> RangePlan:
        if self.high <= self.low:
            raise ValueError("range high must be above range low")
        return self


class EntryPlan(AnalysisModel):
    entry: Price
    stop: Price
    target1: Price
    target2: Price
    stop_structure: str = Field(min_length=1, max_length=500, description=CHINESE_TEXT)
    entry_trigger: str = Field(min_length=1, max_length=500, description=CHINESE_TEXT)
    management: str = Field(min_length=1, max_length=800, description=CHINESE_TEXT)


class MarketAnalysis(AnalysisModel):
    direction: Literal["long", "short", "neutral"]
    summary: str = Field(min_length=1, max_length=1200, description=CHINESE_TEXT)
    anchor: AnalysisAnchor
    scenarios: list[AnalysisScenario] = Field(min_length=2, max_length=4)
    range_plan: RangePlan | None
    entry_plan: EntryPlan | None
    key_evidence: list[str] = Field(
        min_length=2,
        max_length=8,
        description=f"Every item: {CHINESE_TEXT}",
    )
    missing_data_impact: list[str] = Field(
        default_factory=list,
        max_length=8,
        description=f"Every item: {CHINESE_TEXT}",
    )

    @model_validator(mode="before")
    @classmethod
    def include_nearby_anchor_in_neutral_range(cls, value: Any) -> Any:
        """Repair a nearby neutral-range boundary without accepting detached ranges."""

        if not isinstance(value, dict) or value.get("direction") != "neutral":
            return value
        anchor = value.get("anchor")
        range_plan = value.get("range_plan")
        if not isinstance(anchor, dict) or not isinstance(range_plan, dict):
            return value
        numbers = (anchor.get("price"), range_plan.get("low"), range_plan.get("high"))
        if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in numbers):
            return value
        anchor_price, low, high = (float(item) for item in numbers)
        if not all(isfinite(item) and item > 0 for item in (anchor_price, low, high)):
            return value
        width = high - low
        if width <= 0 or low <= anchor_price <= high:
            return value
        distance = low - anchor_price if anchor_price < low else anchor_price - high
        if distance > width:
            return value
        normalized_range = {
            **range_plan,
            "low": min(low, anchor_price),
            "high": max(high, anchor_price),
        }
        return {**value, "range_plan": normalized_range}

    @field_validator("scenarios", mode="before")
    @classmethod
    def normalize_fractional_probabilities(cls, value: Any) -> Any:
        """Accept the common 0-1 convention only when the whole set totals about one."""

        if not isinstance(value, list) or not value:
            return value
        probabilities: list[float] = []
        for scenario in value:
            if not isinstance(scenario, dict):
                return value
            probability = scenario.get("probability")
            if isinstance(probability, bool) or not isinstance(probability, (int, float)):
                return value
            probabilities.append(float(probability))
        total = sum(probabilities)
        if not all(0 <= probability <= 1 for probability in probabilities):
            return value
        if not 0.9 <= total <= 1.1:
            return value
        normalized: list[dict[str, Any]] = []
        for scenario, probability in zip(value, probabilities, strict=True):
            normalized.append({**scenario, "probability": probability * 100})
        return normalized

    @model_validator(mode="after")
    def semantic_contract(self) -> MarketAnalysis:
        total = sum(item.probability for item in self.scenarios)
        if not 90 <= total <= 110:
            raise ValueError("scenario probabilities must total 100% within ±10%")
        if self.direction == "neutral":
            if self.entry_plan is not None:
                raise ValueError("neutral analysis cannot include an entry plan")
            if self.range_plan is None:
                raise ValueError("neutral analysis requires a numeric range plan")
            if not self.range_plan.low <= self.anchor.price <= self.range_plan.high:
                raise ValueError("neutral range must contain the anchor price")
            return self
        if self.entry_plan is None:
            raise ValueError("directional analysis requires an entry plan")
        plan = self.entry_plan
        risk = abs(plan.entry - plan.stop)
        if risk <= 0:
            raise ValueError("entry and stop must differ")
        if self.direction == "long":
            valid = plan.stop < plan.entry < plan.target1 < plan.target2
        else:
            valid = plan.stop > plan.entry > plan.target1 > plan.target2
        if not valid:
            raise ValueError("entry, stop and targets do not match the direction")
        return self

    def reward_risk(self) -> dict[str, float] | None:
        if self.entry_plan is None:
            return None
        plan = self.entry_plan
        risk = abs(plan.entry - plan.stop)
        return {
            "target1": abs(plan.target1 - plan.entry) / risk,
            "target2": abs(plan.target2 - plan.entry) / risk,
        }


def compact_validation_error(prefix: str, exc: ValidationError) -> str:
    """Return a bounded user-safe summary without model payloads or Pydantic URLs."""

    errors = exc.errors(include_url=False, include_input=False)
    messages: list[str] = []
    for error in errors:
        message = str(error.get("msg") or "invalid structured output")
        if message not in messages:
            messages.append(message)
    detail = "; ".join(messages[:3])
    if len(messages) > 3:
        detail += f"; and {len(messages) - 3} other error types"
    count = len(errors)
    noun = "error" if count == 1 else "errors"
    return f"{prefix} ({count} validation {noun}): {detail}"


def market_analysis_output_schema() -> dict[str, object]:
    """JSON Schema accepted by every external provider.

    Pydantic emits a couple of metadata keys Codex accepts; keeping one schema
    for CLI and HTTP providers prevents their contracts from drifting.
    """

    schema = MarketAnalysis.model_json_schema()

    def make_strict(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["required"] = list(properties)
                node["additionalProperties"] = False
            for value in node.values():
                make_strict(value)
        elif isinstance(node, list):
            for value in node:
                make_strict(value)

    make_strict(schema)
    return schema
