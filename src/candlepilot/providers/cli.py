from __future__ import annotations

import asyncio
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

from pydantic import ValidationError

from candlepilot.domain.models import (
    RATIONALE_MAX_LENGTH,
    MarketSnapshot,
    PortfolioState,
    ProviderHealth,
    TradeIntent,
)
from candlepilot.providers.base import DecisionProvider, ProviderCapabilities, ProviderResult
from candlepilot.provenance import (
    DECISION_PROMPT_VERSION,
    MARKET_SNAPSHOT_SCHEMA_VERSION,
    content_fingerprint,
)


CODEX_APP_BINARIES = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
)
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


def find_codex_executable() -> Path | None:
    for app_binary in CODEX_APP_BINARIES:
        if app_binary.is_file() and os.access(app_binary, os.X_OK):
            return app_binary
    candidate = shutil.which("codex")
    if candidate:
        return Path(candidate).resolve()
    user_binary = USER_CLI_DIRECTORY / "codex"
    return (
        user_binary.resolve()
        if user_binary.is_file() and os.access(user_binary, os.X_OK)
        else None
    )


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
    return {
        "market": snapshot.model_dump(mode="json"),
        "portfolio": portfolio.model_dump(mode="json"),
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
        "should normally be below 0.55. Do not inflate confidence or force a trade. "
        "Decision features are per interval, prefixed 5m/15m/30m/1h/4h. The 5m interval "
        "times entries, 15m/30m confirm the actionable move, and 1h/4h define the broader "
        "trend and structure regime; the decision cadence controls when this same complete "
        "ladder is reviewed, not which evidence may be ignored. Trend is ema_spread and "
        "the ema_20/ema_50 pair; structure is range_high_20, range_low_20, range_high_50 and "
        "range_low_50, with range_position_50 giving where price sits in the 50-bar range (0 "
        "at the low, 1 at the high); participation is quote_volume_ratio, book_imbalance and "
        "recent_trade_imbalance, the last of which covers only recent_trade_seconds of tape -- "
        "treat a short window as noise rather than flow. "
        "ema20_distance_atr is how far price has run from its own 20-bar mean in units of its "
        "own ATR, signed. This -- and only this -- is what extended means: about 2.5 or beyond "
        "in the direction you would trade is chasing a move that has already travelled. Do not "
        "read range_position_50 as extension: a live trend sits at its own range edge by "
        "definition, so an aligned trend at range_position_50 near 1 (or near 0 when short) is "
        "the trend working, not a reason to stand aside. "
        "Separately, 1d_range_high_20 and 1d_range_low_20 are the 20-day high and low, and "
        "1d_range_position_20 places the live mark between them (0 at the low, 1 at the high, "
        "and outside 0..1 when price has broken the daily range). These are the strongest "
        "reference levels in the payload -- resting orders cluster at them in a way they do "
        "not at an intraday extreme. They are levels, not a trend, and not a veto: price "
        "through a daily extreme with the trend aligned behind it is a breakout, which favours "
        "that direction rather than forbidding it; a daily level ahead of price, approached "
        "against the trend, is where a move is likely to stall. "
        "Judge only from these; if a setup you are considering needs "
        "evidence the payload does not carry, that setup is not established. "
        "Submit OPEN_LONG, OPEN_SHORT, or ADD only at confidence 0.55 or above with a "
        "defensible invalidation price and one of these setups: (1) trend entry: 5m and at "
        "least one of 15m/30m align, 1h/4h do not both clearly oppose the direction, "
        "volume/order flow does not materially contradict, and price is not chasing by "
        "ema20_distance_atr; alignment from 1h/4h strengthens the setup but does not replace "
        "the short-term trigger; a daily level immediately ahead argues "
        "for a nearer target, not for skipping the trade; (2) pullback: the higher-timeframe "
        "1h/4h trend remains intact and price stabilizes at or reclaims a nearby level with renewed "
        "participation, counting a daily level held as stronger evidence than an intraday one; "
        "(3) reversal: price rejects or reclaims a level plus momentum or flow confirmation, "
        "and a rejection at a daily level is the only reversal worth a full-size entry. "
        "Overbought or oversold readings alone are never reversal confirmation. "
        "portfolio.positions carries each open position's entry_price, unrealized_pnl and the "
        "protective levels currently live on the exchange; its stop_loss is that position's "
        "invalidation. For an existing position, ADD requires the same entry confluence; "
        "REDUCE or CLOSE when its invalidation is reached or opposing evidence is confirmed. "
        "Otherwise HOLD. "
        "HOLD must use leverage=1, risk_fraction=0, order_type=MARKET, and null entry_price, "
        "stop_loss, and take_profit. Opening and ADD decisions require both a stop loss and "
        "a take profit. "
        "Never exceed leverage 10 or risk 0.02. Hard sizing also limits total initial "
        "margin across the portfolio to 80% of equity and total initial margin for any "
        "single symbol to 10% of equity; portfolio.positions reports each existing "
        "position's initial_margin, and opening or ADD sizing must respect the remaining "
        "per-symbol capacity. "
        f"Keep rationale concise and at most {RATIONALE_TARGET_LENGTH} characters."
        + _flow_clause(snapshot)
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
    return TradeIntent.model_validate(data), rationale_truncated


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
    ) -> None:
        self.executable = executable or find_codex_executable()
        self.timeout = timeout
        self.config_path = config_path
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
        except (json.JSONDecodeError, ValidationError) as exc:
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
