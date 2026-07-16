from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from pydantic import SecretStr

from candlepilot.domain.models import TradingMode


DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./candlepilot.db"
DEFAULT_PROVIDER_ALIASES = {
    "codex": "codex-auth",
    "codex-auth": "codex-auth",
    "claude": "claude-code-auth",
    "claude code": "claude-code-auth",
    "claude-code": "claude-code-auth",
    "claude-code-auth": "claude-code-auth",
    "custom": "openai-compatible",
    "custom-api": "openai-compatible",
    "openai-compatible": "openai-compatible",
}
CUSTOM_LLM_WIRE_APIS = {"chat-completions", "responses"}
PROTECTED_CUSTOM_HEADER_NAMES = {
    "authorization",
    "content-length",
    "content-type",
    "host",
}
HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def load_dotenv(path: Path | None = None) -> None:
    """Populate ``os.environ`` from a ``.env`` file without overriding real vars.

    Parses simple ``KEY=VALUE`` lines (``export`` prefix, ``#`` comments and
    surrounding quotes are tolerated). Existing environment variables always win,
    so an explicit ``export`` in the shell overrides the file. A missing file is
    a silent no-op.
    """

    path = path or Path(".env")
    if not path.is_file():
        return
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
        if key and key not in os.environ:
            os.environ[key] = value


def _parse_cadences(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ("5m", "15m", "30m")
    parsed = tuple(item.strip() for item in raw.split(",") if item.strip())
    return parsed or ("5m", "15m", "30m")


def _parse_positive_number[T: (int, float)](raw: str | None, cast: type[T]) -> T | None:
    """Parse an optional positive run limit; blank or invalid means unbounded."""

    if not raw or not raw.strip():
        return None
    try:
        value = cast(raw.strip())
    except ValueError:
        return None
    return value if value > 0 else None


def _parse_candidates_per_cycle(raw: str | None) -> int:
    if not raw:
        return 5
    try:
        return int(raw.strip())
    except ValueError:
        return 5


def _parse_snapshot_age(raw: str | None) -> int:
    if not raw:
        return 75
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError("CANDLEPILOT_MAX_SNAPSHOT_AGE_SECONDS must be an integer") from exc
    if value <= 0:
        raise ValueError("CANDLEPILOT_MAX_SNAPSHOT_AGE_SECONDS must be positive")
    return value


def _parse_default_provider(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    alias = raw.strip().lower()
    try:
        return DEFAULT_PROVIDER_ALIASES[alias]
    except KeyError as exc:
        choices = ", ".join(DEFAULT_PROVIDER_ALIASES)
        raise ValueError(
            f"unsupported CANDLEPILOT_DEFAULT_PROVIDER: {raw!r}; choose one of {choices}"
        ) from exc


def _parse_provider_chain(raw: str | None) -> tuple[str, ...]:
    if not raw or not raw.strip():
        return ()
    providers: list[str] = []
    for item in raw.split(","):
        provider = _parse_default_provider(item)
        if provider is None:
            continue
        if provider in providers:
            raise ValueError("CANDLEPILOT_PROVIDER_CHAIN cannot contain duplicates")
        providers.append(provider)
    if not providers:
        raise ValueError("CANDLEPILOT_PROVIDER_CHAIN must contain at least one provider")
    return tuple(providers)


def _parse_custom_llm_wire_api(raw: str | None) -> str:
    value = (raw or "chat-completions").strip().lower()
    if value not in CUSTOM_LLM_WIRE_APIS:
        choices = ", ".join(sorted(CUSTOM_LLM_WIRE_APIS))
        raise ValueError(f"unsupported CANDLEPILOT_CUSTOM_LLM_WIRE_API: choose {choices}")
    return value


def _parse_boolean(raw: str | None, *, name: str, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be true or false")


def _parse_custom_llm_headers(raw: str | None) -> dict[str, SecretStr]:
    if raw is None or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("CANDLEPILOT_CUSTOM_LLM_EXTRA_HEADERS_JSON must be JSON") from exc
    if not isinstance(parsed, dict) or len(parsed) > 16:
        raise ValueError("custom LLM extra headers must be a JSON object with at most 16 entries")
    headers: dict[str, SecretStr] = {}
    for name, value in parsed.items():
        if (
            not isinstance(name, str)
            or not HEADER_NAME_PATTERN.fullmatch(name)
            or name.lower() in PROTECTED_CUSTOM_HEADER_NAMES
        ):
            raise ValueError("custom LLM extra header name is invalid or protected")
        if not isinstance(value, str) or not value or "\r" in value or "\n" in value:
            raise ValueError("custom LLM extra header value must be a non-empty single line")
        headers[name] = SecretStr(value)
    return headers


@dataclass(frozen=True, slots=True)
class Settings:
    mode: TradingMode = TradingMode.PAPER
    database_url: str = DEFAULT_DATABASE_URL
    data_dir: Path = Path("data")
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    max_leverage: int = 10
    max_risk_fraction: Decimal = Decimal("0.02")
    max_positions: int = 8
    max_margin_fraction: Decimal = Decimal("0.60")
    daily_loss_fraction: Decimal = Decimal("0.08")
    inference_timeout_seconds: float = 45.0
    max_snapshot_age_seconds: int = 75
    cadences: tuple[str, ...] = ("5m", "15m", "30m")
    candidates_per_cycle: int = 5
    max_run_seconds: int | None = None
    max_run_cost_usd: float | None = None
    provider_chain: tuple[str, ...] = ()
    default_provider: str | None = None
    codex_model: str | None = None
    codex_reasoning_effort: str | None = None
    claude_model: str | None = None
    claude_effort: str | None = None
    custom_llm_base_url: str | None = None
    custom_llm_api_key: SecretStr | None = None
    custom_llm_model: str | None = None
    custom_llm_reasoning_effort: str | None = None
    custom_llm_wire_api: str = "chat-completions"
    custom_llm_require_api_key: bool = True
    custom_llm_extra_headers: dict[str, SecretStr] | None = None
    binance_testnet_api_key: SecretStr | None = None
    binance_testnet_api_secret: SecretStr | None = None

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            mode=TradingMode(os.getenv("CANDLEPILOT_MODE", TradingMode.PAPER.value)),
            database_url=os.getenv("CANDLEPILOT_DATABASE_URL", DEFAULT_DATABASE_URL),
            data_dir=Path(os.getenv("CANDLEPILOT_DATA_DIR", "data")),
            bind_host=os.getenv("CANDLEPILOT_HOST", "127.0.0.1"),
            bind_port=int(os.getenv("CANDLEPILOT_PORT", "8000")),
            inference_timeout_seconds=float(os.getenv("CANDLEPILOT_LLM_TIMEOUT", "45")),
            max_snapshot_age_seconds=_parse_snapshot_age(
                os.getenv("CANDLEPILOT_MAX_SNAPSHOT_AGE_SECONDS")
            ),
            cadences=_parse_cadences(os.getenv("CANDLEPILOT_CADENCES")),
            candidates_per_cycle=_parse_candidates_per_cycle(
                os.getenv("CANDLEPILOT_CANDIDATES_PER_CYCLE")
            ),
            max_run_seconds=_parse_positive_number(
                os.getenv("CANDLEPILOT_MAX_RUN_SECONDS"), int
            ),
            max_run_cost_usd=_parse_positive_number(
                os.getenv("CANDLEPILOT_MAX_RUN_COST_USD"), float
            ),
            provider_chain=_parse_provider_chain(os.getenv("CANDLEPILOT_PROVIDER_CHAIN")),
            default_provider=_parse_default_provider(
                os.getenv("CANDLEPILOT_DEFAULT_PROVIDER")
            ),
            codex_model=os.getenv("CANDLEPILOT_CODEX_MODEL") or None,
            codex_reasoning_effort=os.getenv("CANDLEPILOT_CODEX_REASONING_EFFORT") or None,
            claude_model=os.getenv("CANDLEPILOT_CLAUDE_MODEL") or None,
            claude_effort=os.getenv("CANDLEPILOT_CLAUDE_EFFORT") or None,
            custom_llm_base_url=os.getenv("CANDLEPILOT_CUSTOM_LLM_BASE_URL") or None,
            custom_llm_api_key=SecretStr(os.environ["CANDLEPILOT_CUSTOM_LLM_API_KEY"])
            if os.getenv("CANDLEPILOT_CUSTOM_LLM_API_KEY")
            else None,
            custom_llm_model=os.getenv("CANDLEPILOT_CUSTOM_LLM_MODEL") or None,
            custom_llm_reasoning_effort=os.getenv(
                "CANDLEPILOT_CUSTOM_LLM_REASONING_EFFORT"
            )
            or None,
            custom_llm_wire_api=_parse_custom_llm_wire_api(
                os.getenv("CANDLEPILOT_CUSTOM_LLM_WIRE_API")
            ),
            custom_llm_require_api_key=_parse_boolean(
                os.getenv("CANDLEPILOT_CUSTOM_LLM_REQUIRE_API_KEY"),
                name="CANDLEPILOT_CUSTOM_LLM_REQUIRE_API_KEY",
                default=True,
            ),
            custom_llm_extra_headers=_parse_custom_llm_headers(
                os.getenv("CANDLEPILOT_CUSTOM_LLM_EXTRA_HEADERS_JSON")
            ),
            binance_testnet_api_key=SecretStr(os.environ["BINANCE_TESTNET_API_KEY"])
            if os.getenv("BINANCE_TESTNET_API_KEY")
            else None,
            binance_testnet_api_secret=SecretStr(os.environ["BINANCE_TESTNET_API_SECRET"])
            if os.getenv("BINANCE_TESTNET_API_SECRET")
            else None,
        )
