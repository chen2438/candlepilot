import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from candlepilot.api import pricing_provider_ids
from candlepilot.config import Settings
from candlepilot.providers.pricing import (
    load_catalog,
    parse_models_dev,
)

# Mirrors the real models.dev shape (openai/gpt-5.6-sol with a 272k context tier).
PAYLOAD = {
    "openai": {
        "models": {
            "gpt-5.6-sol": {
                "cost": {
                    "input": 5,
                    "output": 30,
                    "cache_read": 0.5,
                    "cache_write": 6.25,
                    "tiers": [
                        {
                            "input": 10,
                            "output": 45,
                            "cache_read": 1,
                            "cache_write": 12.5,
                            "tier": {"type": "context", "size": 272000},
                        }
                    ],
                    "context_over_200k": {"input": 10, "output": 45, "cache_read": 1},
                },
            },
            "mini-nocache": {"cost": {"input": 1, "output": 2}},
        }
    },
    "anthropic": {
        "models": {"claude-sonnet-5": {"cost": {"input": 3, "output": 15, "cache_read": 0.3}}}
    },
    "junk": {"models": {"no-cost": {"name": "no pricing"}}},
}


def test_parse_and_standard_cost_with_cached_subset() -> None:
    catalog = parse_models_dev(PAYLOAD)
    # 1000 input (400 cached), 200 output. non_cached=600.
    cost = catalog.cost_usd(
        "openai", "gpt-5.6-sol", input_tokens=1000, cached_input_tokens=400, output_tokens=200
    )
    assert cost == 600 * 5e-6 + 400 * 5e-7 + 200 * 3e-5


def test_long_context_tier_switches_above_threshold() -> None:
    catalog = parse_models_dev(PAYLOAD)
    cost = catalog.cost_usd(
        "openai", "gpt-5.6-sol", input_tokens=300_000, cached_input_tokens=0, output_tokens=100
    )
    # 300k > 272k -> above-tier rates: input 1e-5, output 4.5e-5.
    assert cost == 300_000 * 1e-5 + 100 * 4.5e-5


def test_missing_cache_read_falls_back_to_input_rate() -> None:
    catalog = parse_models_dev(PAYLOAD)
    cost = catalog.cost_usd(
        "openai", "mini-nocache", input_tokens=100, cached_input_tokens=50, output_tokens=10
    )
    # cache_read absent -> cached billed at input rate 1e-6.
    assert cost == 50 * 1e-6 + 50 * 1e-6 + 10 * 2e-6


def test_unknown_model_and_missing_cost_yield_none() -> None:
    catalog = parse_models_dev(PAYLOAD)
    assert catalog.cost_usd("openai", "does-not-exist", input_tokens=10, output_tokens=1) is None
    assert catalog.cost_usd("openai", None, input_tokens=10, output_tokens=1) is None
    assert catalog.get("junk", "no-cost") is None  # entries without cost are skipped


def test_catalog_cache_refresh_and_offline_fallback(tmp_path: Path) -> None:
    async def scenario():
        calls = {"n": 0}

        async def fetch():
            calls["n"] += 1
            return PAYLOAD

        async def boom():
            raise RuntimeError("network down")

        now = datetime(2026, 7, 15, tzinfo=UTC)
        # First call fetches and caches.
        first = await load_catalog(tmp_path, fetcher=fetch, now=now)
        # Within TTL: served from cache, no new fetch.
        await load_catalog(tmp_path, fetcher=fetch, now=now + timedelta(hours=1))
        fresh_calls = calls["n"]
        # After TTL: refetches.
        await load_catalog(tmp_path, fetcher=fetch, now=now + timedelta(hours=25))
        stale_calls = calls["n"]
        # Stale cache + failing fetch: falls back to the last good cache.
        fallback = await load_catalog(tmp_path, fetcher=boom, now=now + timedelta(hours=50))
        # No cache at all + failing fetch: None.
        empty = await load_catalog(tmp_path / "empty", fetcher=boom, now=now)
        return first, fresh_calls, stale_calls, fallback, empty

    first, fresh_calls, stale_calls, fallback, empty = asyncio.run(scenario())
    assert first is not None
    assert fresh_calls == 1  # second load reused the cache
    assert stale_calls == 2  # third load refetched after TTL
    assert fallback is not None and fallback.get("openai", "gpt-5.6-sol") is not None
    assert empty is None


def test_pricing_provider_ids_only_include_custom_endpoints_that_declared_one() -> None:
    """A custom endpoint's price cannot be inferred, only declared.

    The same model is resold by many models.dev providers at rates that
    genuinely differ, and an OpenAI-compatible endpoint is exactly the
    aggregator case, so an undeclared endpoint must stay unpriced rather than
    be charged at some other vendor's rate.
    """

    settings = Settings.from_mapping(
        {
            "CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON": json.dumps(
                [
                    {"id": "grok", "base_url": "https://api.x.ai/v1", "pricing": "xai"},
                    {"id": "mystery", "base_url": "https://example.test/v1"},
                ]
            )
        }
    )

    identifiers = pricing_provider_ids(settings)

    assert identifiers["openai-compatible:grok"] == "xai"
    assert "openai-compatible:mystery" not in identifiers
    # The CLIs keep their fixed mapping.
    assert identifiers["codex-auth"] == "openai"
    assert identifiers["claude-code-auth"] == "anthropic"
