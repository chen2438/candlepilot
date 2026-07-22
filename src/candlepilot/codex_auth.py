from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from candlepilot.providers.cli import find_codex_cli_executable, sanitized_subprocess_env


DEVICE_AUTH_TIMEOUT_SECONDS = 20 * 60
COMMAND_TIMEOUT_SECONDS = 15
RATE_LIMIT_TIMEOUT_SECONDS = 15
_ANSI_ESCAPE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
_URL_PATTERN = re.compile(r"https://[^\s<>]+")
_CODE_PATTERN = re.compile(r"(?<![A-Z0-9])([A-Z0-9]{4,8}-[A-Z0-9]{4,8})(?![A-Z0-9])")
_NON_DEVICE_CODES = {"DEVICE-CODE", "OPENAI-CODEX", "TIME-CODE"}


class CodexAuthError(RuntimeError):
    pass


def _bounded_text(value: Any, *, maximum: int = 80) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:maximum] if value else None


def _rate_limit_window(kind: str, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    used = value.get("usedPercent")
    if not isinstance(used, int) or isinstance(used, bool):
        return None
    duration = value.get("windowDurationMins")
    resets_at = value.get("resetsAt")
    return {
        "kind": kind,
        "used_percent": max(0, min(100, used)),
        "remaining_percent": 100 - max(0, min(100, used)),
        "window_duration_minutes": duration
        if isinstance(duration, int) and not isinstance(duration, bool) and 0 < duration <= 2_628_000
        else None,
        "resets_at": datetime.fromtimestamp(resets_at, UTC)
        if isinstance(resets_at, int) and not isinstance(resets_at, bool) and 0 < resets_at <= 32_503_680_000
        else None,
    }


def sanitize_rate_limits(payload: Any) -> dict[str, Any]:
    """Return the small display contract, never the app-server's raw response."""
    if not isinstance(payload, dict):
        raise CodexAuthError("Codex CLI 返回了无法识别的额度信息")
    multi = payload.get("rateLimitsByLimitId")
    snapshots: list[tuple[str | None, Any]]
    if isinstance(multi, dict) and multi:
        snapshots = [(str(key), value) for key, value in multi.items()]
    else:
        snapshots = [(None, payload.get("rateLimits"))]
    buckets = []
    for fallback_id, snapshot in snapshots:
        if not isinstance(snapshot, dict):
            continue
        windows = [
            window
            for window in (
                _rate_limit_window("primary", snapshot.get("primary")),
                _rate_limit_window("secondary", snapshot.get("secondary")),
            )
            if window is not None
        ]
        if not windows:
            continue
        buckets.append(
            {
                "limit_id": _bounded_text(snapshot.get("limitId")) or _bounded_text(fallback_id),
                "limit_name": _bounded_text(snapshot.get("limitName")),
                "plan_type": _bounded_text(snapshot.get("planType"), maximum=32),
                "windows": windows,
            }
        )
    if not buckets:
        raise CodexAuthError("Codex CLI 未返回可展示的额度窗口")
    return {
        "available": True,
        "buckets": buckets,
        "checked_at": datetime.now(UTC),
        "message": "Codex 额度已刷新",
    }


def _safe_device_url(text: str) -> str | None:
    for match in _URL_PATTERN.findall(text):
        candidate = match.rstrip(".,;:)'\"]}")
        parsed = urlsplit(candidate)
        hostname = (parsed.hostname or "").lower()
        if (
            parsed.scheme == "https"
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
            and (
                hostname == "openai.com"
                or hostname.endswith(".openai.com")
                or hostname == "chatgpt.com"
                or hostname.endswith(".chatgpt.com")
            )
        ):
            return candidate
    return None


def parse_device_auth_line(line: str) -> tuple[str | None, str | None]:
    clean = _ANSI_ESCAPE.sub("", line).strip()
    url = _safe_device_url(clean)
    codes = [
        match.group(1)
        for match in _CODE_PATTERN.finditer(clean.upper())
        if match.group(1) not in _NON_DEVICE_CODES
    ]
    return url, codes[-1] if codes else None


class CodexAuthManager:
    def __init__(
        self,
        *,
        executable: Path | None = None,
        login_timeout: float = DEVICE_AUTH_TIMEOUT_SECONDS,
    ) -> None:
        self._configured_executable = executable
        self._login_timeout = login_timeout
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._state = "idle"
        self._verification_uri: str | None = None
        self._user_code: str | None = None
        self._message = "尚未启动 Codex CLI 登录"
        self._started_at: datetime | None = None
        self._finished_at: datetime | None = None

    def _executable(self) -> Path | None:
        return self._configured_executable or find_codex_cli_executable()

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        return {
            "available": self._executable() is not None,
            "state": self._state,
            "verification_uri": self._verification_uri,
            "user_code": self._user_code,
            "message": self._message,
            "started_at": self._started_at,
            "finished_at": self._finished_at,
        }

    async def start_login(self) -> dict[str, Any]:
        async with self._lock:
            if self.active:
                raise CodexAuthError("Codex CLI 登录已经在进行中")
            executable = self._executable()
            if executable is None:
                raise CodexAuthError("未检测到独立 Codex CLI")
            self._state = "starting"
            self._verification_uri = None
            self._user_code = None
            self._message = "正在向 Codex 请求设备码…"
            self._started_at = datetime.now(UTC)
            self._finished_at = None
            self._task = asyncio.create_task(self._run_login(executable))
            return self.status()

    async def cancel_login(self) -> dict[str, Any]:
        async with self._lock:
            task = self._task
            if task is None or task.done():
                return self.status()
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._state in {"starting", "pending"}:
            # A task cancelled before its coroutine gets its first timeslice does
            # not enter _run_login's exception handler.
            self._state = "cancelled"
            self._message = "已取消 Codex CLI 登录"
            self._finished_at = datetime.now(UTC)
        return self.status()

    async def logout(self) -> dict[str, Any]:
        async with self._lock:
            if self.active:
                raise CodexAuthError("请先取消正在进行的 Codex CLI 登录")
            executable = self._executable()
            if executable is None:
                raise CodexAuthError("未检测到独立 Codex CLI")
            with tempfile.TemporaryDirectory(prefix="candlepilot-codex-auth-") as directory:
                process = await asyncio.create_subprocess_exec(
                    str(executable),
                    "logout",
                    cwd=directory,
                    env=sanitized_subprocess_env(),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    start_new_session=True,
                )
                try:
                    await asyncio.wait_for(process.wait(), timeout=COMMAND_TIMEOUT_SECONDS)
                except TimeoutError as exc:
                    await self._terminate(process)
                    raise CodexAuthError("Codex CLI 登出超时") from exc
            if process.returncode != 0:
                raise CodexAuthError("Codex CLI 登出失败，请稍后重试")
            self._state = "idle"
            self._verification_uri = None
            self._user_code = None
            self._message = "已登出 Codex CLI"
            self._started_at = None
            self._finished_at = datetime.now(UTC)
            return self.status()

    async def rate_limits(self) -> dict[str, Any]:
        """Read account limits through Codex app-server without exposing credentials."""
        async with self._lock:
            if self.active:
                return self._unavailable_limits("Codex CLI 登录进行中，完成后再查询额度")
            executable = self._executable()
            if executable is None:
                return self._unavailable_limits("未检测到独立 Codex CLI")
            with tempfile.TemporaryDirectory(prefix="candlepilot-codex-usage-") as directory:
                try:
                    process = await asyncio.create_subprocess_exec(
                        str(executable),
                        "app-server",
                        "--stdio",
                        cwd=directory,
                        env=sanitized_subprocess_env(),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=True,
                    )
                except OSError:
                    return self._unavailable_limits("无法启动 Codex CLI 额度查询")
                try:
                    result = await asyncio.wait_for(
                        self._read_rate_limits(process), timeout=RATE_LIMIT_TIMEOUT_SECONDS
                    )
                    return sanitize_rate_limits(result)
                except TimeoutError:
                    return self._unavailable_limits("Codex CLI 额度查询超时")
                except CodexAuthError as exc:
                    return self._unavailable_limits(str(exc))
                except (OSError, json.JSONDecodeError):
                    return self._unavailable_limits("Codex CLI 额度查询不可用")
                finally:
                    await self._terminate(process)

    @staticmethod
    async def _read_rate_limits(process: asyncio.subprocess.Process) -> Any:
        if process.stdin is None or process.stdout is None:
            raise CodexAuthError("Codex CLI 额度查询不可用")
        requests = (
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "candlepilot",
                        "title": "CandlePilot",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
            },
            {"id": 2, "method": "account/rateLimits/read", "params": None},
        )
        for request in requests:
            process.stdin.write((json.dumps(request, separators=(",", ":")) + "\n").encode())
            await process.stdin.drain()
            while line := await process.stdout.readline():
                response = json.loads(line)
                if not isinstance(response, dict):
                    raise CodexAuthError("Codex CLI 返回了无法识别的额度信息")
                if response.get("id") != request["id"]:
                    continue
                if "error" in response:
                    raise CodexAuthError("Codex CLI 当前无法提供额度信息")
                if request["id"] == 2:
                    return response.get("result")
                break
            else:
                raise CodexAuthError("Codex CLI 提前结束额度查询")
        raise CodexAuthError("Codex CLI 未返回额度信息")

    @staticmethod
    def _unavailable_limits(message: str) -> dict[str, Any]:
        return {
            "available": False,
            "buckets": [],
            "checked_at": datetime.now(UTC),
            "message": message,
        }

    async def close(self) -> None:
        await self.cancel_login()

    async def _run_login(self, executable: Path) -> None:
        try:
            with tempfile.TemporaryDirectory(prefix="candlepilot-codex-auth-") as directory:
                process = await asyncio.create_subprocess_exec(
                    str(executable),
                    "login",
                    "--device-auth",
                    cwd=directory,
                    env=sanitized_subprocess_env(),
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                self._process = process
                readers = [
                    asyncio.create_task(self._read_stream(process.stdout)),
                    asyncio.create_task(self._read_stream(process.stderr)),
                ]
                try:
                    await asyncio.wait_for(process.wait(), timeout=self._login_timeout)
                    await asyncio.gather(*readers)
                except (TimeoutError, asyncio.CancelledError):
                    await self._terminate(process)
                    for reader in readers:
                        reader.cancel()
                    await asyncio.gather(*readers, return_exceptions=True)
                    raise
                if process.returncode == 0:
                    self._state = "succeeded"
                    self._message = "Codex CLI 登录成功"
                else:
                    self._state = "failed"
                    self._message = "Codex CLI 登录失败，请重新发起登录"
        except TimeoutError:
            self._state = "failed"
            self._message = "设备码已超时，请重新发起登录"
        except asyncio.CancelledError:
            self._state = "cancelled"
            self._message = "已取消 Codex CLI 登录"
            raise
        except OSError:
            self._state = "failed"
            self._message = "无法启动 Codex CLI 登录"
        finally:
            self._process = None
            self._finished_at = datetime.now(UTC)

    async def _read_stream(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        while line := await stream.readline():
            url, code = parse_device_auth_line(line.decode("utf-8", errors="replace"))
            if url is not None:
                self._verification_uri = url
            if code is not None:
                self._user_code = code
            if self._verification_uri and self._user_code:
                self._state = "pending"
                self._message = "请在授权页面输入一次性代码"

    @staticmethod
    async def _terminate(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            await asyncio.wait_for(process.wait(), timeout=2)
        except (PermissionError, ProcessLookupError, TimeoutError):
            try:
                process.kill()
                await process.wait()
            except (PermissionError, ProcessLookupError):
                return
