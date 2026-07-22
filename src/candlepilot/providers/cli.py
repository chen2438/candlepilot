from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import signal
import tempfile
import time
import tomllib
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from candlepilot.domain.models import (
    RATIONALE_MAX_LENGTH,
    MarketSnapshot,
    PortfolioState,
    ProviderHealth,
    TradeAction,
    TradeIntent,
)
from candlepilot.market.features import DERIVATIVES_POSITIONING_FEATURES
from candlepilot.providers.base import (
    DecisionProvider,
    ProviderCapabilities,
    ProviderResult,
    StructuredOutputResult,
)
from candlepilot.provenance import (
    DECISION_PROMPT_VERSION,
    MARKET_SNAPSHOT_SCHEMA_VERSION,
    content_fingerprint,
)


CODEX_APP_BINARIES = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
)
CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"
CODEX_AUTH_SOURCE_APP = "chatgpt-app"
CODEX_AUTH_SOURCE_CLI = "codex-cli"
CODEX_AUTH_SOURCES = (CODEX_AUTH_SOURCE_APP, CODEX_AUTH_SOURCE_CLI)
USER_CLI_DIRECTORY = Path.home() / ".local" / "bin"
MAX_OUTPUT_BYTES = 1_000_000
RATIONALE_TARGET_LENGTH = 800
SENSITIVE_ENV_PREFIXES = (
    "BINANCE_",
    "OPENAI_",
    "ANTHROPIC_",
    "CODEX_API_KEY",
    "AWS_",
    "GOOGLE_",
)
ENV_ALLOWLIST = {
    "HOME",
    "PATH",
    "SHELL",
    # USER and LOGNAME carry only the (non-secret) username, but the macOS
    # Keychain lookup that Claude Code uses to read its OAuth login fails
    # without them, reporting the CLI as logged out even when it is not.
    "USER",
    "LOGNAME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
}


class ProviderError(RuntimeError):
    pass


class ProviderInvocationError(ProviderError):
    """Provider failure carrying safe, local-only inference audit context."""

    def __init__(
        self,
        message: str,
        *,
        model: str | None,
        duration: timedelta,
        raw_output: str,
        usage: dict[str, Any],
        prompt_version: str,
        data_version: str,
        provider_version: str | None,
        input_payload: dict[str, Any],
        prompt: str,
    ) -> None:
        super().__init__(message)
        self.model = model
        self.duration = duration
        self.raw_output = raw_output
        self.usage = usage
        self.prompt_version = prompt_version
        self.data_version = data_version
        self.provider_version = provider_version
        self.input_payload = input_payload
        self.prompt = prompt


class ProviderUnavailable(ProviderError):
    pass


class ProviderTimeout(ProviderError):
    pass


def sanitized_subprocess_env(source: dict[str, str] | None = None) -> dict[str, str]:
    values = source if source is not None else dict(os.environ)
    clean = {
        key: value
        for key, value in values.items()
        if key in ENV_ALLOWLIST and not key.startswith(SENSITIVE_ENV_PREFIXES)
    }
    clean.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin")
    clean.setdefault("HOME", str(Path.home()))
    clean["NO_COLOR"] = "1"
    return clean


def find_codex_app_executable() -> Path | None:
    for app_binary in CODEX_APP_BINARIES:
        if app_binary.is_file() and os.access(app_binary, os.X_OK):
            return app_binary
    return None


def find_codex_cli_executable() -> Path | None:
    candidate = shutil.which("codex")
    if candidate:
        resolved = Path(candidate).resolve()
        app_paths = {path.resolve() for path in CODEX_APP_BINARIES if path.exists()}
        if resolved not in app_paths:
            return resolved
    user_binary = USER_CLI_DIRECTORY / "codex"
    return (
        user_binary.resolve()
        if user_binary.is_file() and os.access(user_binary, os.X_OK)
        else None
    )


def find_codex_executable(source: str | None = None) -> Path | None:
    if source == CODEX_AUTH_SOURCE_APP:
        return find_codex_app_executable()
    if source == CODEX_AUTH_SOURCE_CLI:
        return find_codex_cli_executable()
    if source is not None:
        raise ValueError(f"unknown Codex auth source: {source}")
    return find_codex_app_executable() or find_codex_cli_executable()


def find_codex_account_email(auth_path: Path | None = None) -> str | None:
    """Read only the email claim from Codex's local ID token.

    The raw token and all other authentication fields remain local. Missing,
    malformed, or non-email claims are treated as unavailable identity data.
    """

    path = auth_path or CODEX_AUTH_PATH
    try:
        auth = json.loads(path.read_text(encoding="utf-8"))
        token = auth["tokens"]["id_token"]
        encoded_payload = token.split(".")[1]
        padding = "=" * (-len(encoded_payload) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded_payload + padding))
    except (OSError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        return None
    email = payload.get("email")
    return email if isinstance(email, str) and "@" in email else None


def find_claude_executable() -> Path | None:
    candidate = shutil.which("claude")
    if candidate:
        return Path(candidate).resolve()
    user_binary = USER_CLI_DIRECTORY / "claude"
    return user_binary.resolve() if user_binary.is_file() and os.access(user_binary, os.X_OK) else None


async def _run_process(
    argv: Sequence[str],
    *,
    cwd: Path,
    stdin: str | None = None,
    timeout: float,
) -> tuple[str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=sanitized_subprocess_env(),
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin.encode() if stdin is not None else None), timeout=timeout
        )
    except (TimeoutError, asyncio.CancelledError) as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=2)
        except (ProcessLookupError, TimeoutError):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if isinstance(exc, TimeoutError):
            raise ProviderTimeout(f"provider timed out after {timeout:g}s") from exc
        raise

    stdout = stdout[:MAX_OUTPUT_BYTES]
    stderr = stderr[:MAX_OUTPUT_BYTES]
    out_text = stdout.decode("utf-8", errors="replace")
    err_text = stderr.decode("utf-8", errors="replace")
    if process.returncode != 0:
        message = err_text.strip() or out_text.strip() or f"exit code {process.returncode}"
        raise ProviderError(message[-2000:])
    return out_text, err_text


def _decision_payload(
    snapshot: MarketSnapshot, portfolio: PortfolioState
) -> dict[str, Any]:
    market = snapshot.model_dump(mode="json")
    market["derivatives_context"] = _derivatives_context(market["features"])
    market["features"] = _without_derivatives_context(market["features"])
    return {
        "market": market,
        "portfolio": portfolio.model_dump(mode="json"),
    }


def _batch_decision_payload(
    snapshots: Sequence[MarketSnapshot], portfolio: PortfolioState
) -> dict[str, Any]:
    markets = []
    for snapshot in snapshots:
        market = snapshot.model_dump(mode="json")
        market["derivatives_context"] = _derivatives_context(market["features"])
        market["features"] = _without_derivatives_context(market["features"])
        markets.append(market)
    return {
        "markets": markets,
        "portfolio": portfolio.model_dump(mode="json"),
    }


def _without_derivatives_context(features: dict[str, float]) -> dict[str, float]:
    return {
        name: value
        for name, value in features.items()
        if name not in DERIVATIVES_POSITIONING_FEATURES
    }


def _derivatives_context(features: dict[str, float]) -> dict[str, Any]:
    values = {
        name: features[name]
        for name in DERIVATIVES_POSITIONING_FEATURES
        if name in features
    }
    missing = [
        name for name in DERIVATIVES_POSITIONING_FEATURES if name not in features
    ]
    return {
        "source": "Binance public Futures Data API",
        "interval": "5m closed statistics",
        "availability": (
            "complete" if not missing else "partial" if values else "unavailable"
        ),
        "values": values,
        "missing_fields": missing,
        "interpretation_limits": [
            "ratios describe account or position crowding, not trader identity",
            "open interest is unsigned and does not identify long or short direction",
            "taker buy/sell ratio is supporting flow evidence, not a standalone signal",
        ],
    }


#: Order-flow fields a live snapshot carries and a historical one cannot.
FLOW_FEATURES = ("book_imbalance", "recent_trade_imbalance")


def _flow_clause(snapshot: MarketSnapshot) -> str:
    """Say so when the payload has no order flow, derived from the payload.

    Read off the snapshot rather than passed in as a mode: a flag could claim
    flow exists when the fields do not, and the model is told elsewhere that a
    setup needing absent evidence is not established -- which, left uncorrected,
    would make every historical decision a HOLD.
    """

    if any(name in snapshot.features for name in FLOW_FEATURES):
        return ""
    return (
        " This snapshot is historical and carries no order-flow fields: there is no "
        "book_imbalance, recent_trade_imbalance, basis_bps or open_interest, because no "
        "historical order book exists to reconstruct them. Do not treat their absence as "
        "evidence against a setup and do not require flow confirmation here; judge "
        "participation from quote_volume_ratio alone. Everything else applies unchanged."
    )


def _batch_flow_clause(snapshots: Sequence[MarketSnapshot]) -> str:
    if all(any(name in snapshot.features for name in FLOW_FEATURES) for snapshot in snapshots):
        return ""
    return (
        " Some supplied snapshots are historical and carry no order-flow fields. For those "
        "markets only, do not treat missing flow as negative evidence and judge participation "
        "from quote_volume_ratio."
    )


def _decision_prompt(
    snapshot: MarketSnapshot,
    portfolio: PortfolioState,
    *,
    include_schema: bool = False,
) -> str:
    payload = _decision_payload(snapshot, portfolio)
    # Codex is constrained by --output-schema; providers without a structured-output
    # flag (Claude) must be given the schema inline or they invent field names.
    schema_clause = ""
    if include_schema:
        schema_clause = (
            " The reply must be exactly one JSON object (no markdown fences, no prose) "
            "conforming to this JSON Schema, using these field names and no others: "
            + json.dumps(trade_intent_output_schema(), separators=(",", ":"))
            + "."
        )
    return (
        f"Prompt version: {DECISION_PROMPT_VERSION}. "
        "You are the decision component of a testnet-only intraday futures system. "
        "Do not use tools, files, shell commands, web search, or external context. "
        "Analyze only the JSON supplied below. Return exactly one object matching the "
        "provided TradeIntent schema. confidence is the estimated strength of an executable "
        "edge for the proposed non-HOLD action; it is not profit probability and never "
        "bypasses hard risk controls. For HOLD it is only residual opportunity strength and "
        "should normally be below 0.55. Do not inflate confidence, force a trade, or request "
        "more risk merely because confidence is higher. "
        "Decision features are per interval, prefixed 5m/15m/30m/1h/4h. The 5m interval "
        "times entries, 15m/30m confirm the actionable move, and 1h/4h define the broader "
        "trend and structure regime; the decision cadence controls when this same complete "
        "ladder is reviewed, not which evidence may be ignored. Trend is ema_spread and "
        "the ema_20/ema_50 pair. range_high_20/range_low_20 include the latest closed bar, "
        "while prior_range_high_20/prior_range_low_20 exclude it. breakout_above_20 and "
        "breakdown_below_20 describe a single latest close; a tradeable breakout instead "
        "requires breakout_hold_above_20 or breakdown_hold_below_20, meaning the last two "
        "closed bars both held beyond breakout_hold_high_20 or breakout_hold_low_20. "
        "last_swing_high/last_swing_low are usable pivots only when "
        "their matching confirmed flag is 1, and bars_since_swing_* states their age. "
        "last_bar_close_position is 0 at the bar low and 1 at the bar high. range_high_50 and "
        "range_low_50 are broader context, with range_position_50 giving where price sits in "
        "that range (0 at the low, 1 at the high); participation is quote_volume_ratio, book_imbalance and "
        "recent_trade_imbalance, the last of which covers only recent_trade_seconds of tape -- "
        "treat a short window as noise rather than flow. "
        "derivatives_context contains optional closed 5m Binance positioning statistics with "
        "an explicit availability state and missing-field list. Interpret price, OI change, "
        "account/position long-short ratios and taker buy/sell ratio together and against the "
        "multi-timeframe price structure. Do not apply universal ratio thresholds, infer trader "
        "identity, treat unsigned OI as directional, or let any one field become a standalone "
        "signal. A missing field is unknown rather than neutral or zero. "
        "ema20_distance_atr is how far price has run from its own 20-bar mean in units of its "
        "own ATR, signed. This -- and only this -- is what extended means: about 2.5 or beyond "
        "in the direction you would trade is chasing a move that has already travelled. Do not "
        "read range_position_50 as extension: a live trend sits at its own range edge by "
        "definition, so an aligned trend at range_position_50 near 1 (or near 0 when short) is "
        "the trend working, not a reason to stand aside. "
        "Separately, 1d_previous_high/low/close are the immediately preceding closed UTC day. "
        "1d_range_high_20 and 1d_range_low_20 are the 20-day high and low, and "
        "1d_range_position_20 places the live mark between them (0 at the low, 1 at the high, "
        "and outside 0..1 when price has broken the daily range). These are the strongest "
        "reference levels in the payload -- resting orders cluster at them in a way they do "
        "not at an intraday extreme. They are levels, not a trend, and not a veto: price "
        "through a daily extreme with the trend aligned behind it is a breakout, which favours "
        "that direction rather than forbidding it; a daily level ahead of price, approached "
        "against the trend, is where a move is likely to stall. "
        "Never submit TREND_BREAKOUT or a BREAKOUT trigger when the matching two-bar hold flag "
        "is 0. A future breakout must be HOLD until both bars close; do not anticipate it with "
        "a LIMIT. Never call an unconfirmed fallback range edge a swing. A MARKET entry that "
        "depends on a future reclaim must be HOLD; use a LIMIT only when its explicit price "
        "represents another still-pending trigger. Judge only from these; if a setup you are considering needs "
        "evidence the payload does not carry, that setup is not established. "
        "Submit OPEN_LONG, OPEN_SHORT, or ADD only at confidence 0.55 or above with a "
        "defensible invalidation price and exactly one of these five setups: "
        "(1) TREND_BREAKOUT: 5m and at least one of 15m/30m align, 1h/4h do not both clearly "
        "oppose the direction, and the matching confirmed two-bar range hold is the entry trigger; "
        "(2) TREND_CONTINUATION: the same timeframe alignment holds and an immediate continuation "
        "trigger is confirmed, but entry is neither a new range breakout nor a pullback/retest; "
        "(3) BREAKOUT_RETEST: the higher-timeframe trend remains intact and price retests a "
        "previously confirmed breakout level, then stabilizes or reclaims it with renewed "
        "participation; (4) TREND_PULLBACK: the higher-timeframe trend remains intact and price "
        "pulls back to a nearby swing, range, EMA, or daily level other than a confirmed breakout "
        "retest, then stabilizes or reclaims it with renewed participation; (5) REVERSAL: price "
        "rejects or reclaims a level plus momentum or flow confirmation, "
        "and a rejection at a daily level is the only reversal worth a full-size entry. "
        "For all five setups, volume/order flow must not materially contradict and price must not "
        "be chasing by ema20_distance_atr. Alignment from 1h/4h strengthens a trend setup but does "
        "not replace the short-term trigger; a daily level immediately ahead argues for a nearer "
        "target, not for skipping the trade. A daily level held during a pullback is stronger "
        "evidence than an intraday one. Overbought or oversold readings alone are never reversal "
        "confirmation. setup_type must be exactly the matching one of these five values for every "
        "OPEN_LONG, OPEN_SHORT, or ADD decision and must be null for other actions. "
        "portfolio.positions carries each open position's entry_price, unrealized_pnl and the "
        "protective levels currently live on the exchange; its stop_loss is that position's "
        "invalidation. portfolio.stop_loss_cooldown_until maps symbols with a net-loss "
        "protective exit in the last 90 minutes to their expiry time; OPEN or ADD on one "
        "of those symbols must HOLD "
        "until expiry. For an existing position, ADD requires the same entry confluence; "
        "REDUCE or CLOSE when its invalidation is reached or opposing evidence is confirmed. "
        "Otherwise HOLD. "
        "HOLD must use leverage=1, risk_fraction=0, order_type=MARKET, and null entry_price, "
        "stop_loss, and take_profit. Opening and ADD decisions require both a stop loss and "
        "a take profit plus decision_framework='structure-v1', setup_type, anchor_timeframe, "
        "anchor_price, trigger_type, trigger_price, invalidation_type, invalidation_level, "
        "and target_type. The invalidation level must name a real SWING, RANGE, EMA, or "
        "DAILY_LEVEL from the payload; place the stop beyond it rather than inventing a bare "
        "percentage. target_type must say whether the take profit uses a SWING, RANGE, "
        "DAILY_LEVEL, or R_MULTIPLE. For ADD, judge the combined position after the add, not the new "
        "quantity in isolation, and use exactly the existing position's leverage because leverage "
        "configuration applies to the whole symbol position. "
        "Never exceed leverage 10 or risk 0.01. Hard sizing also limits aggregate open stop "
        "risk to 4% of equity, total initial margin across the portfolio to 80% of equity, "
        "and total initial margin for any "
        "single symbol to 10% of equity; portfolio.positions reports each existing "
        "position's initial_margin, and opening or ADD sizing must respect the remaining "
        "per-symbol capacity. "
        f"Keep rationale concise and at most {RATIONALE_TARGET_LENGTH} characters."
        + _flow_clause(snapshot)
        + schema_clause
        + "\n"
        + json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    )


def _batch_decision_prompt(
    snapshots: Sequence[MarketSnapshot],
    portfolio: PortfolioState,
    *,
    include_schema: bool = False,
) -> str:
    if not snapshots:
        raise ValueError("a decision batch cannot be empty")
    payload = _batch_decision_payload(snapshots, portfolio)
    schema_clause = ""
    if include_schema:
        schema_clause = (
            " The reply must be exactly one JSON object (no markdown fences, no prose) "
            "conforming to this JSON Schema: "
            + json.dumps(trade_intent_batch_output_schema(), separators=(",", ":"))
            + "."
        )
    # Reuse the established policy text verbatim, replacing only the output contract and
    # serialized payload. This keeps single-decision tests/backtests behavior unchanged.
    template = _decision_prompt(snapshots[0], portfolio, include_schema=False)
    policy, _ = template.rsplit("\n", 1)
    policy = policy.replace(
        "Return exactly one object matching the provided TradeIntent schema.",
        "Return exactly one object with an intents array containing one TradeIntent for every "
        "market, in the same order as markets. Do not omit, duplicate, or reorder symbols.",
    )
    single_flow = _flow_clause(snapshots[0])
    if single_flow and policy.endswith(single_flow):
        policy = policy[: -len(single_flow)]
    return (
        policy
        + _batch_flow_clause(snapshots)
        + schema_clause
        + "\n"
        + json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    )


CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"


def find_codex_model(config_path: Path | None = None) -> str | None:
    """Read the configured Codex model for cost attribution.

    Codex's JSONL event stream does not name the model, so fall back to the
    user's ``~/.codex/config.toml``. Missing or malformed config yields ``None``
    (cost then stays unknown) rather than raising.
    """

    path = config_path or CODEX_CONFIG_PATH
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    model = data.get("model")
    return model if isinstance(model, str) and model else None


def parse_codex_events(stdout: str) -> tuple[str | None, dict[str, Any]]:
    """Extract the final agent message and token usage from codex ``--json`` output.

    Codex emits JSONL events: the schema-conforming answer arrives as the text of
    an ``agent_message`` ``item.completed`` event, and per-turn token counts as a
    ``turn.completed`` ``usage`` object (where cached reads are a subset of input,
    per the OpenAI convention). Unparseable lines are skipped defensively.
    """

    result_text: str | None = None
    usage: dict[str, Any] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                result_text = item["text"]
        elif event_type == "turn.completed":
            raw = event.get("usage") or {}
            input_tokens = int(raw.get("input_tokens") or 0)
            cached_input_tokens = int(raw.get("cached_input_tokens") or 0)
            output_tokens = int(raw.get("output_tokens") or 0)
            usage = {
                "input_tokens": input_tokens,
                "cached_input_tokens": cached_input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
    return result_text, usage


def parse_claude_usage(envelope: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Extract token counts, equivalent cost and model from a Claude envelope.

    The Claude Code CLI reports full token usage and a ``total_cost_usd`` figure
    (the equivalent API cost; subscription plans are not billed per call). The
    top-level ``model`` is often null, so fall back to the dominant model in the
    ``modelUsage`` breakdown.
    """

    raw = envelope.get("usage") or {}
    input_tokens = int(raw.get("input_tokens") or 0)
    output_tokens = int(raw.get("output_tokens") or 0)
    cache_read = int(raw.get("cache_read_input_tokens") or 0)
    cache_creation = int(raw.get("cache_creation_input_tokens") or 0)
    usage: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_creation,
        "total_tokens": input_tokens + output_tokens + cache_read + cache_creation,
    }
    for key in ("duration_ms", "duration_api_ms", "num_turns"):
        if key in envelope:
            usage[key] = envelope[key]
    cost = envelope.get("total_cost_usd")
    if cost is not None:
        usage["cost_usd"] = float(cost)
    model = envelope.get("model")
    if not model:
        model_usage = envelope.get("modelUsage")
        if isinstance(model_usage, dict) and model_usage:
            model = max(
                model_usage.items(),
                key=lambda item: (item[1] or {}).get("outputTokens", 0),
            )[0]
    return model, usage


def _parse_intent(value: str | dict[str, Any]) -> tuple[TradeIntent, bool]:
    data: Any = value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(text)
    rationale_truncated = False
    if isinstance(data, dict):
        rationale = data.get("rationale")
        if isinstance(rationale, str) and len(rationale) > RATIONALE_MAX_LENGTH:
            data = dict(data)
            data["rationale"] = rationale[:RATIONALE_MAX_LENGTH]
            rationale_truncated = True
    intent = TradeIntent.model_validate(data)
    if (
        intent.action
        in {TradeAction.OPEN_LONG, TradeAction.OPEN_SHORT, TradeAction.ADD}
        and intent.setup_type is None
    ):
        raise ValueError("opening and add intents require setup_type")
    return intent, rationale_truncated


def _parse_intents(
    value: str | dict[str, Any], snapshots: Sequence[MarketSnapshot]
) -> tuple[list[TradeIntent], bool]:
    data: Any = value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get("intents"), list):
        raise ValueError("batch response must contain an intents array")
    intents: list[TradeIntent] = []
    truncated = False
    for item in data["intents"]:
        intent, item_truncated = _parse_intent(item)
        intents.append(intent)
        truncated = truncated or item_truncated
    expected = [(item.symbol, item.cadence) for item in snapshots]
    actual = [(item.symbol, item.cadence) for item in intents]
    if actual != expected:
        raise ValueError("batch intents must match market symbols and cadence in input order")
    return intents, truncated


def trade_intent_output_schema() -> dict[str, Any]:
    """Return a strict-output schema accepted by Codex/OpenAI structured output."""
    schema = TradeIntent.model_json_schema()

    def remove_defaults(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            node.pop("pattern", None)
            for value in node.values():
                remove_defaults(value)
        elif isinstance(node, list):
            for value in node:
                remove_defaults(value)

    remove_defaults(schema)
    schema["required"] = list(schema.get("properties", {}))
    schema["additionalProperties"] = False
    return schema


def trade_intent_batch_output_schema() -> dict[str, Any]:
    intent_schema = trade_intent_output_schema()
    definitions = intent_schema.pop("$defs", {})
    return {
        "$defs": definitions,
        "type": "object",
        "properties": {
            "intents": {
                "type": "array",
                "items": intent_schema,
                "minItems": 1,
            }
        },
        "required": ["intents"],
        "additionalProperties": False,
    }


def _split_batch_results(
    *,
    intents: Sequence[TradeIntent],
    provider: str,
    model: str | None,
    duration: timedelta,
    raw_output: str,
    usage: dict[str, Any],
    prompt_version: str | None,
    data_version: str | None,
    provider_version: str | None,
    input_payload: dict[str, Any],
    prompt: str,
    reasoning_effort: str | None,
) -> list[ProviderResult]:
    size = len(intents)
    physical_call_id = str(uuid4())
    split_keys = {
        "input_tokens", "cached_input_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "output_tokens", "total_tokens",
    }
    results: list[ProviderResult] = []
    for index, intent in enumerate(intents):
        allocated = dict(usage)
        for key in split_keys:
            if key in usage:
                total = int(usage.get(key) or 0)
                allocated[key] = total // size + (1 if index < total % size else 0)
        if usage.get("cost_usd") is not None:
            allocated["cost_usd"] = float(usage["cost_usd"]) / size
        allocated.update(
            batch_size=size,
            batch_index=index + 1,
            batch_shared_call=True,
            physical_call_id=physical_call_id,
        )
        results.append(
            ProviderResult(
                intent=intent,
                provider=provider,
                model=model,
                duration=duration,
                raw_output=raw_output,
                usage=allocated,
                prompt_version=prompt_version,
                data_version=data_version,
                provider_version=provider_version,
                input_payload=input_payload,
                prompt=prompt,
                reasoning_effort=reasoning_effort,
            )
        )
    return results


class CodexAuthProvider(DecisionProvider):
    name = "codex-auth"
    reasoning_effort_options = ("minimal", "low", "medium", "high")

    def __init__(
        self,
        *,
        executable: Path | None = None,
        timeout: float = 45,
        config_path: Path | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        auth_source: str | None = None,
    ) -> None:
        if auth_source is not None and auth_source not in CODEX_AUTH_SOURCES:
            raise ValueError(f"unknown Codex auth source: {auth_source}")
        if auth_source is None:
            app_executable = find_codex_app_executable()
            auth_source = (
                CODEX_AUTH_SOURCE_APP
                if executable is None and app_executable is not None
                else CODEX_AUTH_SOURCE_CLI
            )
        self.auth_source = auth_source
        self.executable = executable or find_codex_executable(auth_source)
        self.timeout = timeout
        self.config_path = config_path
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._semaphore = asyncio.Semaphore(1)
        self._active_task: asyncio.Task[Any] | None = None
        self._provider_version: str | None = None

    @property
    def auth_source_options(self) -> tuple[str, ...]:
        return tuple(
            source for source in CODEX_AUTH_SOURCES if find_codex_executable(source) is not None
        )

    def set_auth_source(self, source: str) -> None:
        if source not in CODEX_AUTH_SOURCES:
            raise ValueError(f"unknown Codex auth source: {source}")
        executable = find_codex_executable(source)
        if executable is None:
            raise ValueError(f"Codex auth source is unavailable: {source}")
        self.auth_source = source
        self.executable = executable
        self._provider_version = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(cancellable=True)

    async def cancel(self) -> bool:
        task = self._active_task
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def health_check(self) -> ProviderHealth:
        if self.executable is None:
            return ProviderHealth(
                provider=self.name,
                available=False,
                authenticated=False,
                auth_source=self.auth_source,
                detail="ChatGPT/Codex App binary and codex CLI were not found",
            )
        try:
            with tempfile.TemporaryDirectory(prefix="candlepilot-health-") as directory:
                version, _ = await _run_process(
                    [str(self.executable), "--version"], cwd=Path(directory), timeout=5
                )
                auth, _ = await _run_process(
                    [str(self.executable), "login", "status"], cwd=Path(directory), timeout=8
                )
            self._provider_version = version.strip()
            return ProviderHealth(
                provider=self.name,
                available=True,
                authenticated=True,
                executable=str(self.executable),
                version=version.strip(),
                auth_source=self.auth_source,
                account_email=find_codex_account_email(),
                detail=auth.strip(),
            )
        except ProviderError as exc:
            return ProviderHealth(
                provider=self.name,
                available=True,
                authenticated=False,
                executable=str(self.executable),
                auth_source=self.auth_source,
                detail=str(exc),
            )

    async def generate_structured_output(
        self, *, prompt: str, output_schema: dict[str, Any]
    ) -> StructuredOutputResult:
        if self.executable is None:
            raise ProviderUnavailable("Codex executable was not found")
        started = time.monotonic()
        model = self.model or find_codex_model(self.config_path)
        stdout = ""
        try:
            async with self._semaphore:
                active = asyncio.current_task()
                self._active_task = active
                try:
                    with tempfile.TemporaryDirectory(prefix="candlepilot-analysis-") as directory:
                        root = Path(directory)
                        schema_path = root / "analysis.schema.json"
                        schema_path.write_text(
                            json.dumps(output_schema, separators=(",", ":")), encoding="utf-8"
                        )
                        argv = [
                            str(self.executable), "exec", "--ephemeral", "--ignore-user-config",
                            "--ignore-rules", "--sandbox", "read-only", "--skip-git-repo-check",
                            "--json",
                        ]
                        if self.model:
                            argv += ["-m", self.model]
                        if self.reasoning_effort:
                            argv += ["-c", f"model_reasoning_effort={self.reasoning_effort}"]
                        argv += ["--output-schema", str(schema_path), "-"]
                        stdout, _ = await _run_process(
                            argv, cwd=root, stdin=prompt, timeout=self.timeout
                        )
                finally:
                    if self._active_task is active:
                        self._active_task = None
        except ProviderError:
            raise
        result_text, usage = parse_codex_events(stdout)
        if result_text is None:
            raise ProviderError("Codex did not return an advisory analysis")
        return StructuredOutputResult(
            provider=self.name,
            model=model,
            duration=timedelta(seconds=time.monotonic() - started),
            raw_output=result_text,
            usage=usage,
            provider_version=self._provider_version,
            reasoning_effort=self.reasoning_effort,
        )

    async def generate_trade_intent(
        self, snapshot: MarketSnapshot, portfolio: PortfolioState
    ) -> ProviderResult:
        if self.executable is None:
            raise ProviderUnavailable("Codex executable was not found")
        started = time.monotonic()
        input_payload = _decision_payload(snapshot, portfolio)
        prompt = _decision_prompt(snapshot, portfolio)
        model = self.model or find_codex_model(self.config_path)
        data_version = content_fingerprint(
            snapshot.model_dump(mode="json"),
            schema_version=MARKET_SNAPSHOT_SCHEMA_VERSION,
        )
        try:
            async with self._semaphore:
                active = asyncio.current_task()
                self._active_task = active
                try:
                    with tempfile.TemporaryDirectory(prefix="candlepilot-codex-") as directory:
                        root = Path(directory)
                        schema_path = root / "trade-intent.schema.json"
                        schema_path.write_text(
                            json.dumps(trade_intent_output_schema(), separators=(",", ":")),
                            encoding="utf-8",
                        )
                        argv = [
                            str(self.executable),
                            "exec",
                            "--ephemeral",
                            "--ignore-user-config",
                            "--ignore-rules",
                            "--sandbox",
                            "read-only",
                            "--skip-git-repo-check",
                            "--json",
                        ]
                        if self.model:
                            argv += ["-m", self.model]
                        if self.reasoning_effort:
                            argv += ["-c", f"model_reasoning_effort={self.reasoning_effort}"]
                        argv += ["--output-schema", str(schema_path), "-"]
                        stdout, _ = await _run_process(
                            argv,
                            cwd=root,
                            stdin=prompt,
                            timeout=self.timeout,
                        )
                finally:
                    if self._active_task is active:
                        self._active_task = None
        except ProviderError as exc:
            raise ProviderInvocationError(
                str(exc),
                model=model,
                duration=timedelta(seconds=time.monotonic() - started),
                raw_output="",
                usage={},
                prompt_version=DECISION_PROMPT_VERSION,
                data_version=data_version,
                provider_version=self._provider_version,
                input_payload=input_payload,
                prompt=prompt,
            ) from exc
        result_text, usage = parse_codex_events(stdout)
        if result_text is None:
            raise ProviderInvocationError(
                "Codex did not return an agent message",
                model=model,
                duration=timedelta(seconds=time.monotonic() - started),
                raw_output=stdout,
                usage=usage,
                prompt_version=DECISION_PROMPT_VERSION,
                data_version=data_version,
                provider_version=self._provider_version,
                input_payload=input_payload,
                prompt=prompt,
            )
        try:
            intent, rationale_truncated = _parse_intent(result_text)
        except (ValueError, json.JSONDecodeError, ValidationError) as exc:
            raise ProviderInvocationError(
                f"Codex returned an invalid TradeIntent: {exc}",
                model=model,
                duration=timedelta(seconds=time.monotonic() - started),
                raw_output=result_text,
                usage=usage,
                prompt_version=DECISION_PROMPT_VERSION,
                data_version=data_version,
                provider_version=self._provider_version,
                input_payload=input_payload,
                prompt=prompt,
            ) from exc
        if rationale_truncated:
            usage["rationale_truncated"] = True
        return ProviderResult(
            intent=intent,
            provider=self.name,
            model=model,
            duration=timedelta(seconds=time.monotonic() - started),
            raw_output=result_text,
            usage=usage,
            prompt_version=DECISION_PROMPT_VERSION,
            data_version=data_version,
            provider_version=self._provider_version,
            input_payload=input_payload,
            prompt=prompt,
            reasoning_effort=self.reasoning_effort,
        )

    async def generate_trade_intents(
        self, snapshots: Sequence[MarketSnapshot], portfolio: PortfolioState
    ) -> list[ProviderResult]:
        if len(snapshots) == 1:
            return [await self.generate_trade_intent(snapshots[0], portfolio)]
        if self.executable is None:
            raise ProviderUnavailable("Codex executable was not found")
        started = time.monotonic()
        input_payload = _batch_decision_payload(snapshots, portfolio)
        prompt = _batch_decision_prompt(snapshots, portfolio)
        model = self.model or find_codex_model(self.config_path)
        data_version = content_fingerprint(input_payload, schema_version=MARKET_SNAPSHOT_SCHEMA_VERSION)
        stdout = ""
        usage: dict[str, Any] = {}
        try:
            async with self._semaphore:
                active = asyncio.current_task()
                self._active_task = active
                try:
                    with tempfile.TemporaryDirectory(prefix="candlepilot-codex-") as directory:
                        root = Path(directory)
                        schema_path = root / "trade-intents.schema.json"
                        schema_path.write_text(
                            json.dumps(trade_intent_batch_output_schema(), separators=(",", ":")),
                            encoding="utf-8",
                        )
                        argv = [str(self.executable), "exec", "--ephemeral", "--ignore-user-config",
                                "--ignore-rules", "--sandbox", "read-only", "--skip-git-repo-check", "--json"]
                        if self.model:
                            argv += ["-m", self.model]
                        if self.reasoning_effort:
                            argv += ["-c", f"model_reasoning_effort={self.reasoning_effort}"]
                        argv += ["--output-schema", str(schema_path), "-"]
                        stdout, _ = await _run_process(argv, cwd=root, stdin=prompt, timeout=self.timeout)
                finally:
                    if self._active_task is active:
                        self._active_task = None
            result_text, usage = parse_codex_events(stdout)
            if result_text is None:
                raise ValueError("Codex did not return an agent message")
            intents, truncated = _parse_intents(result_text, snapshots)
        except (ProviderError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            if isinstance(exc, ProviderInvocationError):
                raise
            raise ProviderInvocationError(
                f"Codex returned an invalid batch: {exc}", model=model,
                duration=timedelta(seconds=time.monotonic() - started), raw_output=stdout,
                usage=usage, prompt_version=DECISION_PROMPT_VERSION, data_version=data_version,
                provider_version=self._provider_version, input_payload=input_payload, prompt=prompt,
            ) from exc
        if truncated:
            usage["rationale_truncated"] = True
        duration = timedelta(seconds=time.monotonic() - started)
        return _split_batch_results(
            intents=intents, provider=self.name, model=model, duration=duration,
            raw_output=result_text, usage=usage, prompt_version=DECISION_PROMPT_VERSION,
            data_version=data_version, provider_version=self._provider_version,
            input_payload=input_payload, prompt=prompt, reasoning_effort=self.reasoning_effort,
        )


class ClaudeCodeAuthProvider(DecisionProvider):
    name = "claude-code-auth"
    reasoning_effort_options = ("low", "medium", "high", "xhigh", "max")

    def __init__(
        self,
        *,
        executable: Path | None = None,
        timeout: float = 45,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.executable = executable or find_claude_executable()
        self.timeout = timeout
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._semaphore = asyncio.Semaphore(1)
        self._active_task: asyncio.Task[Any] | None = None
        self._provider_version: str | None = None

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(cancellable=True)

    async def cancel(self) -> bool:
        task = self._active_task
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return True

    async def health_check(self) -> ProviderHealth:
        if self.executable is None:
            return ProviderHealth(
                provider=self.name,
                available=False,
                authenticated=False,
                detail="The independent claude CLI was not found in PATH or ~/.local/bin",
            )
        try:
            with tempfile.TemporaryDirectory(prefix="candlepilot-health-") as directory:
                version, _ = await _run_process(
                    [str(self.executable), "--version"], cwd=Path(directory), timeout=5
                )
                auth, _ = await _run_process(
                    [str(self.executable), "auth", "status"], cwd=Path(directory), timeout=8
                )
            self._provider_version = version.strip()
            return ProviderHealth(
                provider=self.name,
                available=True,
                authenticated=True,
                executable=str(self.executable),
                version=version.strip(),
                detail=auth.strip(),
            )
        except ProviderError as exc:
            return ProviderHealth(
                provider=self.name,
                available=True,
                authenticated=False,
                executable=str(self.executable),
                detail=str(exc),
            )

    async def generate_structured_output(
        self, *, prompt: str, output_schema: dict[str, Any]
    ) -> StructuredOutputResult:
        if self.executable is None:
            raise ProviderUnavailable("Claude Code CLI was not found")
        started = time.monotonic()
        full_prompt = (
            f"{prompt}\n\nReturn only JSON matching this schema:\n"
            f"{json.dumps(output_schema, separators=(',', ':'), ensure_ascii=False)}"
        )
        stdout = ""
        try:
            async with self._semaphore:
                active = asyncio.current_task()
                self._active_task = active
                try:
                    with tempfile.TemporaryDirectory(prefix="candlepilot-analysis-") as directory:
                        argv = [
                            str(self.executable), "-p", "--output-format", "json",
                            "--permission-mode", "default", "--max-turns", "4",
                            "--disallowedTools",
                            "Bash,Read,Edit,Write,WebFetch,WebSearch,Task,NotebookEdit",
                        ]
                        if self.model:
                            argv += ["--model", self.model]
                        if self.reasoning_effort:
                            argv += ["--effort", self.reasoning_effort]
                        stdout, _ = await _run_process(
                            argv, stdin=full_prompt, cwd=Path(directory), timeout=self.timeout
                        )
                finally:
                    if self._active_task is active:
                        self._active_task = None
        except ProviderError:
            raise
        try:
            envelope = json.loads(stdout)
            if not isinstance(envelope, dict) or not isinstance(envelope.get("result"), str):
                raise TypeError
            model, usage = parse_claude_usage(envelope)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError("Claude Code returned an invalid analysis envelope") from exc
        return StructuredOutputResult(
            provider=self.name,
            model=model or self.model,
            duration=timedelta(seconds=time.monotonic() - started),
            raw_output=envelope["result"],
            usage=usage,
            provider_version=self._provider_version,
            reasoning_effort=self.reasoning_effort,
        )

    async def generate_trade_intent(
        self, snapshot: MarketSnapshot, portfolio: PortfolioState
    ) -> ProviderResult:
        if self.executable is None:
            raise ProviderUnavailable("Claude Code CLI was not found")
        started = time.monotonic()
        input_payload = _decision_payload(snapshot, portfolio)
        prompt = _decision_prompt(snapshot, portfolio, include_schema=True)
        data_version = content_fingerprint(
            snapshot.model_dump(mode="json"),
            schema_version=MARKET_SNAPSHOT_SCHEMA_VERSION,
        )
        try:
            async with self._semaphore:
                active = asyncio.current_task()
                self._active_task = active
                try:
                    with tempfile.TemporaryDirectory(prefix="candlepilot-claude-") as directory:
                        # Not plan mode: plan mode makes Claude call ExitPlanMode (or
                        # explain the plan workflow) instead of answering, which burns
                        # the single turn and yields error_max_turns. A small turn
                        # budget tolerates a stray disallowed-tool attempt while the
                        # empty cwd, tool blocklist and sanitized env keep it inert.
                        argv = [
                            str(self.executable),
                            "-p",
                            "--output-format",
                            "json",
                            "--permission-mode",
                            "default",
                            "--max-turns",
                            "4",
                            "--disallowedTools",
                            "Bash,Read,Edit,Write,WebFetch,WebSearch,Task,NotebookEdit",
                        ]
                        if self.model:
                            argv += ["--model", self.model]
                        if self.reasoning_effort:
                            argv += ["--effort", self.reasoning_effort]
                        # Prompt goes on stdin, not as a trailing arg: --disallowedTools
                        # greedily consumes the following positional token, so an
                        # arg-passed prompt gets word-split into bogus "deny rules"
                        # whenever no --model/--effort flag separates them.
                        stdout, _ = await _run_process(
                            argv,
                            stdin=prompt,
                            cwd=Path(directory),
                            timeout=self.timeout,
                        )
                finally:
                    if self._active_task is active:
                        self._active_task = None
        except ProviderError as exc:
            raise ProviderInvocationError(
                str(exc),
                model=self.model,
                duration=timedelta(seconds=time.monotonic() - started),
                raw_output="",
                usage={},
                prompt_version=DECISION_PROMPT_VERSION,
                data_version=data_version,
                provider_version=self._provider_version,
                input_payload=input_payload,
                prompt=prompt,
            ) from exc
        model = self.model
        usage: dict[str, Any] = {}
        try:
            envelope = json.loads(stdout)
            if not isinstance(envelope, dict):
                raise TypeError("Claude Code response envelope must be an object")
            model, usage = parse_claude_usage(envelope)
            intent, rationale_truncated = _parse_intent(envelope["result"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            raise ProviderInvocationError(
                f"Claude Code returned an invalid TradeIntent: {exc}",
                model=model,
                duration=timedelta(seconds=time.monotonic() - started),
                raw_output=stdout,
                usage=usage,
                prompt_version=DECISION_PROMPT_VERSION,
                data_version=data_version,
                provider_version=self._provider_version,
                input_payload=input_payload,
                prompt=prompt,
            ) from exc
        if rationale_truncated:
            usage["rationale_truncated"] = True
        return ProviderResult(
            intent=intent,
            provider=self.name,
            model=model,
            duration=timedelta(seconds=time.monotonic() - started),
            raw_output=stdout,
            usage=usage,
            prompt_version=DECISION_PROMPT_VERSION,
            data_version=data_version,
            provider_version=self._provider_version,
            input_payload=input_payload,
            prompt=prompt,
            reasoning_effort=self.reasoning_effort,
        )

    async def generate_trade_intents(
        self, snapshots: Sequence[MarketSnapshot], portfolio: PortfolioState
    ) -> list[ProviderResult]:
        if len(snapshots) == 1:
            return [await self.generate_trade_intent(snapshots[0], portfolio)]
        if self.executable is None:
            raise ProviderUnavailable("Claude Code CLI was not found")
        started = time.monotonic()
        input_payload = _batch_decision_payload(snapshots, portfolio)
        prompt = _batch_decision_prompt(snapshots, portfolio, include_schema=True)
        data_version = content_fingerprint(input_payload, schema_version=MARKET_SNAPSHOT_SCHEMA_VERSION)
        stdout = ""
        usage: dict[str, Any] = {}
        model = self.model
        try:
            async with self._semaphore:
                active = asyncio.current_task()
                self._active_task = active
                try:
                    with tempfile.TemporaryDirectory(prefix="candlepilot-claude-") as directory:
                        argv = [str(self.executable), "-p", "--output-format", "json",
                                "--permission-mode", "default", "--max-turns", "4",
                                "--disallowedTools",
                                "Bash,Read,Edit,Write,WebFetch,WebSearch,Task,NotebookEdit"]
                        if self.model:
                            argv += ["--model", self.model]
                        if self.reasoning_effort:
                            argv += ["--effort", self.reasoning_effort]
                        stdout, _ = await _run_process(
                            argv, stdin=prompt, cwd=Path(directory), timeout=self.timeout
                        )
                finally:
                    if self._active_task is active:
                        self._active_task = None
            envelope = json.loads(stdout)
            if not isinstance(envelope, dict):
                raise TypeError("Claude Code response envelope must be an object")
            model, usage = parse_claude_usage(envelope)
            intents, truncated = _parse_intents(envelope["result"], snapshots)
        except (ProviderError, KeyError, TypeError, ValueError, json.JSONDecodeError, ValidationError) as exc:
            raise ProviderInvocationError(
                f"Claude Code returned an invalid batch: {exc}", model=model,
                duration=timedelta(seconds=time.monotonic() - started), raw_output=stdout,
                usage=usage, prompt_version=DECISION_PROMPT_VERSION, data_version=data_version,
                provider_version=self._provider_version, input_payload=input_payload, prompt=prompt,
            ) from exc
        if truncated:
            usage["rationale_truncated"] = True
        return _split_batch_results(
            intents=intents, provider=self.name, model=model,
            duration=timedelta(seconds=time.monotonic() - started), raw_output=stdout, usage=usage,
            prompt_version=DECISION_PROMPT_VERSION, data_version=data_version,
            provider_version=self._provider_version, input_payload=input_payload, prompt=prompt,
            reasoning_effort=self.reasoning_effort,
        )
