from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel


DECISION_PROMPT_VERSION = "trade-intent-v17"
MARKET_SNAPSHOT_SCHEMA_VERSION = "market-snapshot-v4"
BACKTEST_DATA_SCHEMA_VERSION = "backtest-candles-v2"
GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{7,40}$")


def repository_commit(
    repository_path: Path | None = None,
    *,
    executable: str | None = None,
) -> str | None:
    """Return the local Git HEAD for immutable run provenance, if available."""

    repository_path = repository_path or Path(__file__).resolve().parents[2]
    executable = executable or shutil.which("git")
    if executable is None:
        return None
    try:
        result = subprocess.run(
            [executable, "rev-parse", "--short=7", "HEAD"],
            cwd=repository_path,
            env={
                "PATH": os.environ.get("PATH", os.defpath),
                "LANG": "C",
                "LC_ALL": "C",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=2,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and GIT_COMMIT_PATTERN.fullmatch(commit) else None


APPLICATION_GIT_COMMIT = repository_commit()


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    raise TypeError(f"unsupported provenance value: {type(value).__name__}")


def content_fingerprint(value: Any, *, schema_version: str) -> str:
    canonical = json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    digest = hashlib.sha256(canonical).hexdigest()
    return f"{schema_version}:sha256:{digest}"
