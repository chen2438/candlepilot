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

CODEX_APP_BINARIES = (Path("/Applications/ChatGPT.app/Contents/Resources/codex"),)
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
    return (
        user_binary.resolve()
        if user_binary.is_file() and os.access(user_binary, os.X_OK)
        else None
    )


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
        stdin=(
            asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL
        ),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin.encode() if stdin is not None else None),
            timeout=timeout,
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
        message = (
            err_text.strip() or out_text.strip() or f"exit code {process.returncode}"
        )
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
        " 该快照来自历史数据，不包含订单流字段：由于不存在可重建的历史订单簿，因此没有 "
        "book_imbalance、recent_trade_imbalance、basis_bps 或 open_interest。不得把字段缺失"
        "视为反对某种形态的证据，也不得在此要求订单流确认；只能使用 quote_volume_ratio 判断"
        "参与度。其他规则保持不变。"
    )


def _batch_flow_clause(snapshots: Sequence[MarketSnapshot]) -> str:
    if all(
        any(name in snapshot.features for name in FLOW_FEATURES)
        for snapshot in snapshots
    ):
        return ""
    return (
        " 部分输入快照来自历史数据，不包含订单流字段。仅对这些市场，不得把订单流缺失视为"
        "负面证据，并使用 quote_volume_ratio 判断参与度。"
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
            " 回复必须严格为一个符合以下 JSON Schema 的 JSON 对象，不得包含 Markdown 代码围栏"
            "或额外说明；只能使用 Schema 中的字段名："
            + json.dumps(trade_intent_output_schema(), separators=(",", ":"))
            + "."
        )
    return (
        f"提示词版本：{DECISION_PROMPT_VERSION}。"
        "你是一个日内期货系统中的决策组件。"
        "不得使用工具、文件、Shell 命令、网络搜索或任何外部上下文，只能分析下方提供的 JSON。"
        "严格返回一个符合 TradeIntent Schema 的对象。confidence 表示所提议非 HOLD 动作具备可执行交易优势的估计强度。"
        "对于 HOLD，它只表示残余机会强度，应低于 0.55。"
        "决策特征按周期提供，前缀为 5m/15m/30m/1h/4h。"
        "decision cadence 只决定何时复核这套完整周期阶梯。"
        "趋势由 ema_spread 和 ema_20/ema_50 组合"
        "表示。range_high_20/range_low_20 包含最新已收盘 K 线，prior_range_high_20/"
        "prior_range_low_20 不包含。breakout_above_20 和 breakdown_below_20 只描述最新一根"
        "收盘；可交易突破必须具备 breakout_hold_above_20 或 breakdown_hold_below_20，即最近"
        "两根已收盘 K 线都保持在 breakout_hold_high_20 或 breakout_hold_low_20 之外。"
        "last_swing_high/last_swing_low 只有在对应 confirmed 标志为 1 时才是可用枢轴，"
        "bars_since_swing_* 表示其年龄。last_bar_close_position 在 K 线最低点为 0、最高点为 1。"
        "range_high_50 和 range_low_50 提供更宽的背景，range_position_50 表示价格在该区间内的"
        "位置（最低点为 0、最高点为 1）。参与度由 quote_volume_ratio、book_imbalance 和 "
        "recent_trade_imbalance 表示；recent_trade_imbalance 只覆盖 recent_trade_seconds 秒的"
        "近期成交，窗口很短时应视为噪声而非可靠订单流。"
        "derivatives_context 包含可选的 Binance 已收盘 5m 持仓统计，并明确给出可用状态和缺失字段。"
        "缺失字段代表未知，不代表中性或零。"
        "ema20_distance_atr 表示价格偏离自身 20 周期均值的有符号 ATR 倍数。"
        "1d_previous_high/low/close 是紧邻当前时间之前一个已经收盘的 UTC 日线。"
        "1d_range_high_20 和 1d_range_low_20 是 20 日高低点，1d_range_position_20 表示当前"
        "标记价在其间的位置（最低点为 0、最高点为 1，突破日线区间时可超出 0..1）。"
        "只有 confidence 不低于 0.55、存在可辩护的失效价格，并且符合以下五种形态之一时，"
        "才能提交 OPEN_LONG、OPEN_SHORT 或 ADD："
        "(1) TREND_BREAKOUT：5m 与 15m/30m 至少一个方向一致，1h/4h 不能同时明显反向，且对应"
        "已确认的两根 K 线区间保持是入场触发；"
        "(2) TREND_CONTINUATION：满足相同的周期方向一致性并确认即时延续触发，但入场既不是新的"
        "区间突破，也不是回调或回踩；"
        "(3) BREAKOUT_RETEST：高周期趋势仍然完整，价格回踩此前已确认的突破位，随后企稳或重新站上，"
        "并伴随参与度恢复；"
        "(4) TREND_PULLBACK：高周期趋势仍然完整，价格回调至附近的 swing、range、EMA 或日线位，"
        "但不是已确认突破位的回踩，随后企稳或重新站上，并伴随参与度恢复；"
        "(5) REVERSAL：价格拒绝或重新站上某一价格位，同时得到动量或订单流确认；只有日线位的拒绝"
        "才值得完整仓位。"
        "每个 OPEN_LONG、OPEN_SHORT 或 ADD 的 setup_type 必须严格填写上述五个枚举之一并与实际"
        "形态匹配，其他动作的 setup_type 必须为 null。"
        "portfolio.positions 包含每个现有仓位的 entry_price、unrealized_pnl 和当前真实挂在交易所"
        "的保护价；其中 stop_loss 就是该仓位的失效价。portfolio.stop_loss_cooldown_until 将最近 "
        "90 分钟内发生净亏保护退出的标的映射到冷却截止时间；映射中的标的在截止前如要 OPEN 或 ADD，"
        "必须返回 HOLD。对于现有仓位，ADD 需要与新入场相同的信号汇合；失效位已触及或确认出现反向"
        "证据时返回 REDUCE 或 CLOSE，否则返回 HOLD。"
        "HOLD 必须使用 leverage=1、risk_fraction=0、order_type=MARKET，并将 entry_price、"
        "stop_loss 和 take_profit 设为 null。开仓和 ADD 必须同时提供止损、止盈，以及 "
        "decision_framework='structure-v1'、setup_type、anchor_timeframe、anchor_price、"
        "trigger_type、trigger_price、invalidation_type、invalidation_level 和 target_type。"
        "失效位必须引用输入中真实存在的 SWING、RANGE、EMA 或 DAILY_LEVEL；止损应放在该失效位之外，"
        "不得凭空使用固定百分比。target_type 必须说明止盈使用 SWING、RANGE、DAILY_LEVEL 还是 "
        "R_MULTIPLE。对于 ADD，必须评估加仓后的合并仓位，而非孤立评估新增数量；必须严格沿用现有"
        "仓位杠杆，因为杠杆配置作用于整个标的仓位。"
        "leverage 不得超过 10，risk_fraction 不得超过 0.01。确定性定量还会限制：全部未平仓止损"
        "风险合计不超过权益 4%，组合初始保证金不超过权益 80%，任一标的初始保证金不超过权益 10%；"
        "portfolio.positions 提供每个现有仓位的 initial_margin，开仓或 ADD 必须尊重该标的剩余容量。"
        f"rationale 使用简体中文，不超过 {RATIONALE_TARGET_LENGTH} 个字符。"
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
            " 回复必须严格为一个符合以下 JSON Schema 的 JSON 对象，不得包含 Markdown 代码围栏"
            "或额外说明："
            + json.dumps(trade_intent_batch_output_schema(), separators=(",", ":"))
            + "."
        )
    # Reuse the established policy text verbatim, replacing only the output contract and
    # serialized payload. This keeps single-decision tests/backtests behavior unchanged.
    template = _decision_prompt(snapshots[0], portfolio, include_schema=False)
    policy, _ = template.rsplit("\n", 1)
    policy = policy.replace(
        "严格返回一个符合 TradeIntent Schema 的对象。",
        "严格返回一个带有 intents 数组的对象；markets 中的每个市场必须对应一个 TradeIntent，"
        "顺序必须与 markets 相同。不得遗漏、重复或重排标的。",
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
            if item.get("type") == "agent_message" and isinstance(
                item.get("text"), str
            ):
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
            text = (
                text.removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )
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
            text = (
                text.removeprefix("```json")
                .removeprefix("```")
                .removesuffix("```")
                .strip()
            )
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
        raise ValueError(
            "batch intents must match market symbols and cadence in input order"
        )
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
        "input_tokens",
        "cached_input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
        "total_tokens",
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
            source
            for source in CODEX_AUTH_SOURCES
            if find_codex_executable(source) is not None
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
                    [str(self.executable), "login", "status"],
                    cwd=Path(directory),
                    timeout=8,
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
                    with tempfile.TemporaryDirectory(
                        prefix="candlepilot-analysis-"
                    ) as directory:
                        root = Path(directory)
                        schema_path = root / "analysis.schema.json"
                        schema_path.write_text(
                            json.dumps(output_schema, separators=(",", ":")),
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
                            argv += [
                                "-c",
                                f"model_reasoning_effort={self.reasoning_effort}",
                            ]
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
                    with tempfile.TemporaryDirectory(
                        prefix="candlepilot-codex-"
                    ) as directory:
                        root = Path(directory)
                        schema_path = root / "trade-intent.schema.json"
                        schema_path.write_text(
                            json.dumps(
                                trade_intent_output_schema(), separators=(",", ":")
                            ),
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
                            argv += [
                                "-c",
                                f"model_reasoning_effort={self.reasoning_effort}",
                            ]
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
        data_version = content_fingerprint(
            input_payload, schema_version=MARKET_SNAPSHOT_SCHEMA_VERSION
        )
        stdout = ""
        usage: dict[str, Any] = {}
        try:
            async with self._semaphore:
                active = asyncio.current_task()
                self._active_task = active
                try:
                    with tempfile.TemporaryDirectory(
                        prefix="candlepilot-codex-"
                    ) as directory:
                        root = Path(directory)
                        schema_path = root / "trade-intents.schema.json"
                        schema_path.write_text(
                            json.dumps(
                                trade_intent_batch_output_schema(),
                                separators=(",", ":"),
                            ),
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
                            argv += [
                                "-c",
                                f"model_reasoning_effort={self.reasoning_effort}",
                            ]
                        argv += ["--output-schema", str(schema_path), "-"]
                        stdout, _ = await _run_process(
                            argv, cwd=root, stdin=prompt, timeout=self.timeout
                        )
                finally:
                    if self._active_task is active:
                        self._active_task = None
            result_text, usage = parse_codex_events(stdout)
            if result_text is None:
                raise ValueError("Codex did not return an agent message")
            intents, truncated = _parse_intents(result_text, snapshots)
        except (
            ProviderError,
            ValueError,
            json.JSONDecodeError,
            ValidationError,
        ) as exc:
            if isinstance(exc, ProviderInvocationError):
                raise
            raise ProviderInvocationError(
                f"Codex returned an invalid batch: {exc}",
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
        if truncated:
            usage["rationale_truncated"] = True
        duration = timedelta(seconds=time.monotonic() - started)
        return _split_batch_results(
            intents=intents,
            provider=self.name,
            model=model,
            duration=duration,
            raw_output=result_text,
            usage=usage,
            prompt_version=DECISION_PROMPT_VERSION,
            data_version=data_version,
            provider_version=self._provider_version,
            input_payload=input_payload,
            prompt=prompt,
            reasoning_effort=self.reasoning_effort,
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
                    [str(self.executable), "auth", "status"],
                    cwd=Path(directory),
                    timeout=8,
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
                    with tempfile.TemporaryDirectory(
                        prefix="candlepilot-analysis-"
                    ) as directory:
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
                        stdout, _ = await _run_process(
                            argv,
                            stdin=full_prompt,
                            cwd=Path(directory),
                            timeout=self.timeout,
                        )
                finally:
                    if self._active_task is active:
                        self._active_task = None
        except ProviderError:
            raise
        try:
            envelope = json.loads(stdout)
            if not isinstance(envelope, dict) or not isinstance(
                envelope.get("result"), str
            ):
                raise TypeError
            model, usage = parse_claude_usage(envelope)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(
                "Claude Code returned an invalid analysis envelope"
            ) from exc
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
                    with tempfile.TemporaryDirectory(
                        prefix="candlepilot-claude-"
                    ) as directory:
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
        except (
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            ValidationError,
        ) as exc:
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
        data_version = content_fingerprint(
            input_payload, schema_version=MARKET_SNAPSHOT_SCHEMA_VERSION
        )
        stdout = ""
        usage: dict[str, Any] = {}
        model = self.model
        try:
            async with self._semaphore:
                active = asyncio.current_task()
                self._active_task = active
                try:
                    with tempfile.TemporaryDirectory(
                        prefix="candlepilot-claude-"
                    ) as directory:
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
                        stdout, _ = await _run_process(
                            argv,
                            stdin=prompt,
                            cwd=Path(directory),
                            timeout=self.timeout,
                        )
                finally:
                    if self._active_task is active:
                        self._active_task = None
            envelope = json.loads(stdout)
            if not isinstance(envelope, dict):
                raise TypeError("Claude Code response envelope must be an object")
            model, usage = parse_claude_usage(envelope)
            intents, truncated = _parse_intents(envelope["result"], snapshots)
        except (
            ProviderError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
            ValidationError,
        ) as exc:
            raise ProviderInvocationError(
                f"Claude Code returned an invalid batch: {exc}",
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
        if truncated:
            usage["rationale_truncated"] = True
        return _split_batch_results(
            intents=intents,
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
