"""Read, describe and rewrite the local ``.env`` so the frontend can edit it.

The frontend only ever *writes* secrets: existing values are returned masked, so a
key never leaves the backend in full. Saving rewrites ``.env`` in place, keeping
comments and key order, and never applies to the running process — every change
takes effect on the next start.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path



@dataclass(frozen=True, slots=True)
class EnvField:
    key: str
    label: str
    kind: str = "text"  # text | int | number | bool | enum | json | secret
    options: tuple[str, ...] = ()
    placeholder: str = ""
    description: str = ""

    @property
    def secret(self) -> bool:
        return self.kind == "secret"


@dataclass(frozen=True, slots=True)
class EnvSection:
    title: str
    fields: tuple[EnvField, ...] = field(default_factory=tuple)


ENV_SECTIONS: tuple[EnvSection, ...] = (
    EnvSection(
        "运行模式与服务",
        (
            EnvField("CANDLEPILOT_HOST", "绑定地址", placeholder="127.0.0.1",
                     description="只允许 localhost。"),
            EnvField("CANDLEPILOT_PORT", "端口", "int", placeholder="8000"),
            EnvField("CANDLEPILOT_DATABASE_URL", "数据库", placeholder="sqlite+aiosqlite:///./candlepilot.db"),
            EnvField("CANDLEPILOT_DATA_DIR", "数据目录", placeholder="./data"),
        ),
    ),
    EnvSection(
        "决策与运行",
        (
            EnvField(
                "CANDLEPILOT_CADENCES",
                "分析周期",
                placeholder="15m",
                description="只能填写一个：5m、15m、30m、1h 或 4h。",
            ),
            EnvField(
                "CANDLEPILOT_CANDIDATES_PER_CYCLE",
                "每周期候选标的数",
                "int",
                placeholder="5",
            ),
            EnvField("CANDLEPILOT_LLM_TIMEOUT", "LLM 超时（秒）", "number", placeholder="45"),
            EnvField("CANDLEPILOT_MAX_SNAPSHOT_AGE_SECONDS", "快照最大年龄（秒）", "int", placeholder="75"),
            EnvField("CANDLEPILOT_MAX_RUN_SECONDS", "运行时长上限（秒）", "int",
                     description="留空=不限。"),
            EnvField("CANDLEPILOT_MAX_RUN_COST_USD", "运行预算（USD 等效）", "number",
                     description="留空=不限。"),
            EnvField(
                "CANDLEPILOT_TRAILING_STOP_MODE",
                "移动止损模式",
                "enum",
                ("off", "shadow", "live"),
                description="shadow 并行记录五组参数；live 只执行 2R 激活、回撤 1R。",
            ),
            EnvField(
                "CANDLEPILOT_STRUCTURE_GATE_MODE",
                "结构入场门槛",
                "enum",
                ("off", "shadow", "enforce"),
                description="shadow 只记录新结构规则是否通过；enforce 才会拒绝不合格入场。",
            ),
        ),
    ),
    EnvSection(
        "Provider 路由",
        (
            EnvField(
                "CANDLEPILOT_PROVIDER_CHAIN",
                "主备顺序",
                placeholder="codex, claude-code, custom:groq",
                description=(
                    "按顺序用逗号分隔：本地规则填 local，Codex 填 codex，"
                    "Claude Code 填 claude-code，自定义端点填 custom:<id>。"
                ),
            ),
            EnvField("CANDLEPILOT_CODEX_MODEL", "Codex 模型"),
            EnvField("CANDLEPILOT_CODEX_REASONING_EFFORT", "Codex 推理强度", "enum",
                     ("", "minimal", "low", "medium", "high")),
            EnvField("CANDLEPILOT_CLAUDE_MODEL", "Claude 模型"),
            EnvField("CANDLEPILOT_CLAUDE_EFFORT", "Claude 强度", "enum",
                     ("", "low", "medium", "high", "xhigh", "max")),
        ),
    ),
    EnvSection(
        "币安测试网",
        (
            EnvField("BINANCE_TESTNET_API_KEY", "API Key", "secret"),
            EnvField("BINANCE_TESTNET_API_SECRET", "API Secret", "secret"),
        ),
    ),
)

ENV_FIELDS: dict[str, EnvField] = {
    field_spec.key: field_spec
    for section in ENV_SECTIONS
    for field_spec in section.fields
}

# The providers array embeds api_key values, so it is masked like a secret even
# though it is edited as JSON.
CUSTOM_PROVIDERS_ENV = "CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON"
MASKED_JSON_KEYS = frozenset({CUSTOM_PROVIDERS_ENV})


def mask_secret(value: str) -> str:
    """Return a recognisable but non-recoverable preview of a secret."""

    if not value:
        return ""
    if len(value) <= 6:
        return "…" + value[-2:]
    return f"{value[:3]}…{value[-4:]}"


def read_env_file(path: Path) -> dict[str, str]:
    """Parse ``.env`` into a mapping, tolerating comments, quotes and ``export``."""

    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _format_line(key: str, value: str) -> str:
    """Render ``KEY=value`` exactly as the loaders read it back.

    Values are written raw and never escaped: both this module's reader and
    ``config.load_dotenv`` only strip *surrounding* quotes and do no unescaping,
    so quoting a JSON value would feed ``{\\"a\\":1}`` to the next startup.
    Leading/trailing whitespace is dropped because the readers strip it anyway.
    """

    return f"{key}={value.strip()}"


def write_env_file(path: Path, updates: Mapping[str, str]) -> None:
    """Apply ``updates`` to ``.env`` in place, keeping comments and key order.

    Writes atomically with owner-only permissions because the file holds secrets.
    """

    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(updates)
    rewritten: list[str] = []
    for raw in lines:
        stripped = raw.strip()
        candidate = stripped[len("export ") :].lstrip() if stripped.startswith("export ") else stripped
        key = candidate.partition("=")[0].strip()
        if stripped and not stripped.startswith("#") and "=" in candidate and key in remaining:
            rewritten.append(_format_line(key, remaining.pop(key)))
        else:
            rewritten.append(raw)
    for key, value in remaining.items():
        rewritten.append(_format_line(key, value))

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=".env.", suffix=".tmp", delete=False
    )
    try:
        with handle as stream:
            stream.write("\n".join(rewritten).rstrip("\n") + "\n")
        os.chmod(handle.name, 0o600)
        os.replace(handle.name, path)
    except BaseException:
        Path(handle.name).unlink(missing_ok=True)
        raise


def describe_settings(path: Path, values: Mapping[str, str]) -> dict[str, object]:
    """Build the frontend payload: metadata plus masked-or-plain current values."""

    sections = []
    for section in ENV_SECTIONS:
        fields = []
        for spec in section.fields:
            raw = values.get(spec.key, "")
            masked = spec.secret or spec.key in MASKED_JSON_KEYS
            fields.append(
                {
                    "key": spec.key,
                    "label": spec.label,
                    "kind": spec.kind,
                    "options": list(spec.options),
                    "placeholder": spec.placeholder,
                    "description": spec.description,
                    "secret": masked,
                    "configured": bool(raw),
                    "value": None if masked else raw,
                    "masked": mask_secret(raw) if masked else None,
                }
            )
        sections.append({"title": section.title, "fields": fields})
    return {"path": str(path), "sections": sections}
