from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from candlepilot.providers.cli import sanitized_subprocess_env


WEB_UPDATE_HELPER = Path("/usr/local/sbin/candlepilot-web-update")
WEB_UPDATE_STATUS_FILE = Path("/var/lib/candlepilot/update-status.json")
WEB_BACKUP_MANIFEST_FILE = Path("/var/lib/candlepilot/backups.json")
WEB_BACKUP_STATUS_FILE = Path("/var/lib/candlepilot/backup-status.json")
WEB_LOG_STATUS_FILE = Path("/var/lib/candlepilot/log-status.json")
WEB_UPDATE_REPOSITORY = Path(__file__).resolve().parents[2]
WEB_UPDATE_PHASES = {"idle", "running", "completed", "failed"}
WEB_BACKUP_ACTIONS = {"refresh", "delete", "delete_all"}
WEB_UPDATE_BRANCH_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
WEB_UPDATE_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40,64}$")
WEB_BACKUP_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{7,64}$")


def read_web_update_status(
    *,
    helper_path: Path | None = None,
    status_path: Path | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Read the root updater's deliberately small, world-readable status file."""

    helper_path = helper_path or WEB_UPDATE_HELPER
    status_path = status_path or WEB_UPDATE_STATUS_FILE
    platform = platform or sys.platform
    supported = (
        platform.startswith("linux")
        and helper_path.is_file()
        and os.access(helper_path, os.X_OK)
    )
    payload: dict[str, Any] = {
        "supported": supported,
        "phase": "idle",
        "message": (
            "尚未执行网页更新"
            if supported
            else "网页更新仅在通过 VPS 安装器部署更新助手后可用"
        ),
        "started_at": None,
        "finished_at": None,
        "from_commit": None,
        "current_commit": None,
        "backup": None,
    }
    if not supported or not status_path.is_file():
        return payload
    try:
        stored = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {**payload, "phase": "failed", "message": "无法读取更新状态"}
    if not isinstance(stored, dict) or stored.get("phase") not in WEB_UPDATE_PHASES:
        return {**payload, "phase": "failed", "message": "更新状态格式无效"}
    for key in (
        "message",
        "started_at",
        "finished_at",
        "from_commit",
        "current_commit",
        "backup",
    ):
        value = stored.get(key)
        if value is not None and not isinstance(value, str):
            return {**payload, "phase": "failed", "message": "更新状态格式无效"}
        if isinstance(value, str) and len(value) > 1000:
            return {**payload, "phase": "failed", "message": "更新状态字段过长"}
    return {
        **payload,
        **{key: stored.get(key) for key in payload if key != "supported"},
    }


def read_web_backup_inventory(
    *,
    helper_path: Path | None = None,
    manifest_path: Path | None = None,
    status_path: Path | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Read the root worker's sanitized backup manifest and action status."""

    helper_path = helper_path or WEB_UPDATE_HELPER
    manifest_path = manifest_path or WEB_BACKUP_MANIFEST_FILE
    status_path = status_path or WEB_BACKUP_STATUS_FILE
    platform = platform or sys.platform
    supported = (
        platform.startswith("linux")
        and helper_path.is_file()
        and os.access(helper_path, os.X_OK)
    )
    payload: dict[str, Any] = {
        "supported": supported,
        "generated_at": None,
        "backups": [],
        "status": {
            "phase": "idle",
            "action": None,
            "message": (
                "尚未执行备份维护"
                if supported
                else "备份管理仅在通过 VPS 安装器部署维护助手后可用"
            ),
            "started_at": None,
            "finished_at": None,
            "backup_id": None,
            "reclaimed_bytes": None,
        },
    }
    if not supported:
        return payload
    manifest_invalid = False
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            generated_at = manifest.get("generated_at")
            stored_backups = manifest.get("backups")
            if not isinstance(generated_at, str) or len(generated_at) > 64:
                raise ValueError
            if not isinstance(stored_backups, list) or len(stored_backups) > 1000:
                raise ValueError
            backups: list[dict[str, Any]] = []
            seen: set[str] = set()
            for item in stored_backups:
                if not isinstance(item, dict):
                    raise ValueError
                backup_id = item.get("id")
                created_at = item.get("created_at")
                source_commit = item.get("source_commit")
                size_bytes = item.get("size_bytes")
                protected = item.get("protected")
                if (
                    not isinstance(backup_id, str)
                    or not WEB_BACKUP_ID_PATTERN.fullmatch(backup_id)
                    or backup_id in seen
                    or not isinstance(created_at, str)
                    or len(created_at) > 64
                    or (
                        source_commit is not None
                        and (
                            not isinstance(source_commit, str)
                            or not re.fullmatch(r"[0-9a-f]{7,64}", source_commit)
                        )
                    )
                    or isinstance(size_bytes, bool)
                    or not isinstance(size_bytes, int)
                    or not 0 <= size_bytes <= 2**63 - 1
                    or not isinstance(protected, bool)
                ):
                    raise ValueError
                seen.add(backup_id)
                backups.append(
                    {
                        "id": backup_id,
                        "created_at": created_at,
                        "source_commit": source_commit,
                        "size_bytes": size_bytes,
                        "protected": protected,
                    }
                )
            if backups != sorted(backups, key=lambda item: item["id"], reverse=True):
                raise ValueError
            if backups and (
                not backups[0]["protected"]
                or any(item["protected"] for item in backups[1:])
            ):
                raise ValueError
            payload["generated_at"] = generated_at
            payload["backups"] = backups
        except (OSError, ValueError, TypeError, AttributeError):
            manifest_invalid = True
            payload["status"] = {
                **payload["status"],
                "phase": "failed",
                "message": "备份清单格式无效，请刷新清单",
            }
    if status_path.is_file() and not manifest_invalid:
        try:
            stored_status = json.loads(status_path.read_text(encoding="utf-8"))
            phase = stored_status.get("phase")
            action = stored_status.get("action")
            message = stored_status.get("message")
            if phase not in WEB_UPDATE_PHASES or action not in WEB_BACKUP_ACTIONS:
                raise ValueError
            if not isinstance(message, str) or len(message) > 1000:
                raise ValueError
            status: dict[str, Any] = {
                "phase": phase,
                "action": action,
                "message": message,
            }
            for key in ("started_at", "finished_at", "backup_id"):
                value = stored_status.get(key)
                if value is not None and (not isinstance(value, str) or len(value) > 128):
                    raise ValueError
                status[key] = value
            if status["backup_id"] is not None and not WEB_BACKUP_ID_PATTERN.fullmatch(
                status["backup_id"]
            ):
                raise ValueError
            reclaimed = stored_status.get("reclaimed_bytes")
            if reclaimed is not None and (
                isinstance(reclaimed, bool)
                or not isinstance(reclaimed, int)
                or not 0 <= reclaimed <= 2**63 - 1
            ):
                raise ValueError
            status["reclaimed_bytes"] = reclaimed
            payload["status"] = status
        except (OSError, ValueError, TypeError, AttributeError):
            payload["status"] = {
                **payload["status"],
                "phase": "failed",
                "message": "无法读取备份维护状态",
            }
    return payload


def read_web_log_status(
    *,
    helper_path: Path | None = None,
    status_path: Path | None = None,
    platform: str | None = None,
) -> dict[str, Any]:
    """Read only the sanitized result of root-owned log maintenance."""

    helper_path = helper_path or WEB_UPDATE_HELPER
    status_path = status_path or WEB_LOG_STATUS_FILE
    platform = platform or sys.platform
    supported = (
        platform.startswith("linux")
        and helper_path.is_file()
        and os.access(helper_path, os.X_OK)
    )
    payload: dict[str, Any] = {
        "supported": supported,
        "phase": "idle",
        "message": (
            "尚未清理 CandlePilot 日志"
            if supported
            else "日志管理仅在通过 VPS 安装器部署维护助手后可用"
        ),
        "started_at": None,
        "finished_at": None,
        "before_bytes": None,
        "after_bytes": None,
    }
    if not supported or not status_path.is_file():
        return payload
    try:
        stored = json.loads(status_path.read_text(encoding="utf-8"))
        if not isinstance(stored, dict) or stored.get("phase") not in WEB_UPDATE_PHASES:
            raise ValueError
        message = stored.get("message")
        if not isinstance(message, str) or len(message) > 1000:
            raise ValueError
        result = {**payload, "phase": stored["phase"], "message": message}
        for key in ("started_at", "finished_at"):
            value = stored.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 128):
                raise ValueError
            result[key] = value
        for key in ("before_bytes", "after_bytes"):
            value = stored.get(key)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= 2**63 - 1
            ):
                raise ValueError
            result[key] = value
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return {**payload, "phase": "failed", "message": "无法读取日志维护状态"}


async def queue_web_maintenance(*arguments: str) -> None:
    """Queue one narrowly defined action through the unprivileged launcher."""

    try:
        process = await asyncio.create_subprocess_exec(
            str(WEB_UPDATE_HELPER),
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise RuntimeError("maintenance helper acknowledgement timed out") from exc
    except OSError as exc:
        raise RuntimeError(
            f"could not start the maintenance helper: {type(exc).__name__}"
        ) from exc
    if process.returncode != 0:
        detail = (stderr or stdout).decode("utf-8", errors="replace").strip()
        raise RuntimeError((detail or "maintenance helper refused the request")[-500:])


class WebUpdateCheckError(RuntimeError):
    pass


async def _run_web_update_git(
    *args: str, repository_path: Path
) -> tuple[int, str]:
    executable = shutil.which("git")
    if executable is None:
        raise WebUpdateCheckError("Git 不可用，无法检查更新")
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            *args,
            cwd=repository_path,
            env=sanitized_subprocess_env(),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=20)
    except TimeoutError as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()
        raise WebUpdateCheckError("检查更新超时，请稍后重试") from exc
    except OSError as exc:
        raise WebUpdateCheckError("无法执行 Git 更新检查") from exc
    return process.returncode, stdout.decode("utf-8", errors="replace").strip()


async def check_web_update(
    repository_path: Path | None = None,
) -> dict[str, Any]:
    repository_path = repository_path or WEB_UPDATE_REPOSITORY

    async def git(*args: str) -> str:
        returncode, output = await _run_web_update_git(
            *args, repository_path=repository_path
        )
        if returncode != 0:
            raise WebUpdateCheckError("Git 仓库状态不允许检查更新")
        return output

    branch = await git("branch", "--show-current")
    if not WEB_UPDATE_BRANCH_PATTERN.fullmatch(branch):
        raise WebUpdateCheckError("当前 Git 分支无效，无法检查更新")
    current_commit = await git("rev-parse", "HEAD")
    if not WEB_UPDATE_COMMIT_PATTERN.fullmatch(current_commit):
        raise WebUpdateCheckError("当前 Git 提交无效，无法检查更新")
    origin = await git("remote", "get-url", "origin")
    parsed_origin = urlsplit(origin)
    if (
        parsed_origin.scheme != "https"
        or parsed_origin.hostname != "github.com"
        or parsed_origin.username is not None
        or parsed_origin.password is not None
        or parsed_origin.query
        or parsed_origin.fragment
    ):
        raise WebUpdateCheckError("只允许检查无内嵌凭据的 GitHub HTTPS origin")
    await git("fetch", "--quiet", "--no-tags", "origin", branch)
    latest_commit = await git("rev-parse", "FETCH_HEAD")
    if not WEB_UPDATE_COMMIT_PATTERN.fullmatch(latest_commit):
        raise WebUpdateCheckError("远端 Git 提交无效，无法检查更新")
    update_available = latest_commit != current_commit
    if update_available:
        returncode, _ = await _run_web_update_git(
            "merge-base",
            "--is-ancestor",
            current_commit,
            latest_commit,
            repository_path=repository_path,
        )
        if returncode != 0:
            raise WebUpdateCheckError("远端版本不是当前版本的快进更新，已拒绝安装")
    return {
        "supported": True,
        "checked_at": datetime.now(UTC),
        "branch": branch,
        "current_commit": current_commit,
        "latest_commit": latest_commit,
        "update_available": update_available,
        "message": "发现可安装的新版本" if update_available else "当前已是最新版本",
    }
