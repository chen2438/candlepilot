from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

MODELS_DEV_URL = "https://models.dev/api.json"
CACHE_FILENAME = "models-dev-v1.json"
DEFAULT_TTL = timedelta(hours=24)

# models.dev provider ids for the CLIs CandlePilot drives.
PROVIDER_IDS: dict[str, str] = {
    "codex-auth": "openai",
    "claude-code-auth": "anthropic",
}

Fetcher = Callable[[], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class TokenRates:
    """Per-token USD rates (models.dev publishes USD per 1M tokens)."""

    input: float
    output: float
    cache_read: float
    cache_write: float


@dataclass(frozen=True, slots=True)
class ModelPrice:
    standard: TokenRates
    threshold_tokens: int | None = None
    above: TokenRates | None = None

    def cost_usd(
        self,
        *,
        input_tokens: int,
        cached_input_tokens: int = 0,
        output_tokens: int,
        cache_write_tokens: int = 0,
    ) -> float:
        """Cost using the OpenAI convention where cached reads are a subset of input.

        Tokens are clamped so cached/cache-write can never exceed input and are
        never double-billed, mirroring how CodexBar folds cached usage.
        """

        total_input = max(0, input_tokens)
        cached = min(max(0, cached_input_tokens), total_input)
        remaining = total_input - cached
        cache_write = min(max(0, cache_write_tokens), remaining)
        non_cached = remaining - cache_write
        rates = self.standard
        if (
            self.threshold_tokens is not None
            and self.above is not None
            and total_input > self.threshold_tokens
        ):
            rates = self.above
        return (
            non_cached * rates.input
            + cached * rates.cache_read
            + cache_write * rates.cache_write
            + max(0, output_tokens) * rates.output
        )


@dataclass(frozen=True, slots=True)
class ModelPricingCatalog:
    prices: dict[tuple[str, str], ModelPrice]

    def get(self, provider_id: str, model: str) -> ModelPrice | None:
        return self.prices.get((provider_id, model))

    def cost_usd(
        self,
        provider_id: str,
        model: str | None,
        *,
        input_tokens: int,
        cached_input_tokens: int = 0,
        output_tokens: int,
        cache_write_tokens: int = 0,
    ) -> float | None:
        if not model:
            return None
        price = self.get(provider_id, model)
        if price is None:
            return None
        return price.cost_usd(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            cache_write_tokens=cache_write_tokens,
        )


def _rates(cost: dict[str, Any]) -> TokenRates:
    million = 1_000_000
    input_rate = float(cost["input"]) / million
    output_rate = float(cost["output"]) / million
    cache_read = float(cost.get("cache_read", cost["input"])) / million
    cache_write = float(cost.get("cache_write", cost["input"])) / million
    return TokenRates(input_rate, output_rate, cache_read, cache_write)


def parse_models_dev(payload: dict[str, Any]) -> ModelPricingCatalog:
    prices: dict[tuple[str, str], ModelPrice] = {}
    for provider_id, provider in payload.items():
        models = (provider or {}).get("models") or {}
        for model_id, model in models.items():
            cost = model.get("cost")
            if not isinstance(cost, dict) or "input" not in cost or "output" not in cost:
                continue
            threshold: int | None = None
            above: TokenRates | None = None
            over = cost.get("context_over_200k")
            if isinstance(over, dict) and "input" in over:
                above = _rates(over)
                threshold = 200_000
            tiers = cost.get("tiers")
            if isinstance(tiers, list):
                for tier in tiers:
                    size = ((tier or {}).get("tier") or {}).get("size")
                    if size and isinstance(tier, dict) and "input" in tier:
                        above = _rates(tier)
                        threshold = int(size)
                        break
            prices[(provider_id, model_id)] = ModelPrice(_rates(cost), threshold, above)
    return ModelPricingCatalog(prices)


async def _default_fetcher() -> dict[str, Any]:
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(MODELS_DEV_URL)
        response.raise_for_status()
        return response.json()


def _read_cache(cache_path: Path) -> tuple[datetime, dict[str, Any]] | None:
    if not cache_path.is_file():
        return None
    try:
        wrapper = json.loads(cache_path.read_text(encoding="utf-8"))
        fetched_at = datetime.fromisoformat(wrapper["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
        return fetched_at, wrapper["data"]
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        return None


def _write_cache(cache_dir: Path, payload: dict[str, Any], now: datetime) -> None:
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / CACHE_FILENAME).write_text(
        json.dumps({"fetched_at": now.isoformat(), "data": payload}, separators=(",", ":")),
        encoding="utf-8",
    )


async def load_catalog(
    cache_dir: Path,
    *,
    ttl: timedelta = DEFAULT_TTL,
    fetcher: Fetcher | None = None,
    now: datetime | None = None,
) -> ModelPricingCatalog | None:
    """Load a pricing catalog, refreshing the models.dev cache when stale.

    Returns the last valid cache if the refresh fails, and ``None`` only when
    there is neither a usable cache nor a successful fetch, so cost display
    degrades gracefully offline instead of raising.
    """

    now = now or datetime.now(UTC)
    fetcher = fetcher or _default_fetcher
    cache_path = cache_dir / CACHE_FILENAME
    cached = _read_cache(cache_path)
    if cached is not None and now - cached[0] < ttl:
        return parse_models_dev(cached[1])
    try:
        payload = await fetcher()
    except Exception:
        return parse_models_dev(cached[1]) if cached is not None else None
    _write_cache(cache_dir, payload, now)
    return parse_models_dev(payload)
