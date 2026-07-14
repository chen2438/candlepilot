from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile
import time
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from candlepilot.domain.models import MarketSnapshot, PortfolioState, ProviderHealth, TradeIntent
from candlepilot.providers.base import LLMProvider, ProviderCapabilities, ProviderResult


CODEX_APP_BINARY = Path("/Applications/Codex.app/Contents/Resources/codex")
MAX_OUTPUT_BYTES = 1_000_000
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
    if CODEX_APP_BINARY.is_file() and os.access(CODEX_APP_BINARY, os.X_OK):
        return CODEX_APP_BINARY
    candidate = shutil.which("codex")
    return Path(candidate).resolve() if candidate else None


def find_claude_executable() -> Path | None:
    candidate = shutil.which("claude")
    return Path(candidate).resolve() if candidate else None


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


def _decision_prompt(snapshot: MarketSnapshot, portfolio: PortfolioState) -> str:
    payload = {
        "market": snapshot.model_dump(mode="json"),
        "portfolio": portfolio.model_dump(mode="json"),
    }
    return (
        "You are the decision component of a testnet-only intraday futures system. "
        "Do not use tools, files, shell commands, web search, or external context. "
        "Analyze only the JSON supplied below. Return exactly one object matching the "
        "provided TradeIntent schema. Use HOLD when evidence is weak or data is unsuitable. "
        "Opening and ADD decisions require a stop loss. Never exceed leverage 10 or risk 0.02.\n"
        + json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    )


def _parse_intent(value: str | dict[str, Any]) -> TradeIntent:
    data: Any = value
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(text)
    return TradeIntent.model_validate(data)


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


class CodexAuthProvider(LLMProvider):
    name = "codex-auth"

    def __init__(self, *, executable: Path | None = None, timeout: float = 45) -> None:
        self.executable = executable or find_codex_executable()
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(1)
        self._active_task: asyncio.Task[Any] | None = None

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
                detail="Codex App binary and codex CLI were not found",
            )
        try:
            with tempfile.TemporaryDirectory(prefix="candlepilot-health-") as directory:
                version, _ = await _run_process(
                    [str(self.executable), "--version"], cwd=Path(directory), timeout=5
                )
                auth, _ = await _run_process(
                    [str(self.executable), "login", "status"], cwd=Path(directory), timeout=8
                )
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
        self._active_task = asyncio.current_task()
        try:
            async with self._semaphore:
                with tempfile.TemporaryDirectory(prefix="candlepilot-codex-") as directory:
                    root = Path(directory)
                    schema_path = root / "trade-intent.schema.json"
                    schema_path.write_text(
                        json.dumps(trade_intent_output_schema(), separators=(",", ":")),
                        encoding="utf-8",
                    )
                    stdout, _ = await _run_process(
                        [
                            str(self.executable),
                            "exec",
                            "--ephemeral",
                            "--ignore-user-config",
                            "--ignore-rules",
                            "--sandbox",
                            "read-only",
                            "--skip-git-repo-check",
                            "--output-schema",
                            str(schema_path),
                            "-",
                        ],
                        cwd=root,
                        stdin=_decision_prompt(snapshot, portfolio),
                        timeout=self.timeout,
                    )
        finally:
            self._active_task = None
        try:
            intent = _parse_intent(stdout)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ProviderError(f"Codex returned an invalid TradeIntent: {exc}") from exc
        return ProviderResult(
            intent=intent,
            provider=self.name,
            model=None,
            duration=timedelta(seconds=time.monotonic() - started),
            raw_output=stdout,
            usage={},
        )


class ClaudeCodeAuthProvider(LLMProvider):
    name = "claude-code-auth"

    def __init__(self, *, executable: Path | None = None, timeout: float = 45) -> None:
        self.executable = executable or find_claude_executable()
        self.timeout = timeout
        self._semaphore = asyncio.Semaphore(1)
        self._active_task: asyncio.Task[Any] | None = None

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
                detail="The independent claude CLI was not found in PATH",
            )
        try:
            with tempfile.TemporaryDirectory(prefix="candlepilot-health-") as directory:
                version, _ = await _run_process(
                    [str(self.executable), "--version"], cwd=Path(directory), timeout=5
                )
                auth, _ = await _run_process(
                    [str(self.executable), "auth", "status"], cwd=Path(directory), timeout=8
                )
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
        self._active_task = asyncio.current_task()
        try:
            async with self._semaphore:
                with tempfile.TemporaryDirectory(prefix="candlepilot-claude-") as directory:
                    stdout, _ = await _run_process(
                        [
                            str(self.executable),
                            "-p",
                            "--output-format",
                            "json",
                            "--permission-mode",
                            "plan",
                            "--max-turns",
                            "1",
                            "--disallowedTools",
                            "Bash,Read,Edit,Write,WebFetch,WebSearch,Task,NotebookEdit",
                            _decision_prompt(snapshot, portfolio),
                        ],
                        cwd=Path(directory),
                        timeout=self.timeout,
                    )
        finally:
            self._active_task = None
        try:
            envelope = json.loads(stdout)
            intent = _parse_intent(envelope["result"])
        except (KeyError, TypeError, json.JSONDecodeError, ValidationError) as exc:
            raise ProviderError(f"Claude Code returned an invalid TradeIntent: {exc}") from exc
        usage = {
            key: envelope[key]
            for key in ("duration_ms", "duration_api_ms", "num_turns", "total_cost_usd")
            if key in envelope
        }
        return ProviderResult(
            intent=intent,
            provider=self.name,
            model=envelope.get("model"),
            duration=timedelta(seconds=time.monotonic() - started),
            raw_output=stdout,
            usage=usage,
        )
