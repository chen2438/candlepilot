from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from candlepilot.domain.models import DEFAULT_DECISION_CADENCE, SUPPORTED_CADENCES
from candlepilot.auth import validate_password_hash



DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./candlepilot.db"
# Single source of truth for the .env location, shared with the frontend editor.
ENV_FILE_VARIABLE = "CANDLEPILOT_ENV_FILE"
# Keys this process took from .env rather than from a real environment variable.
# A restart must drop these before re-exec, otherwise the inherited old values
# would win over the rewritten .env (load_dotenv never overrides real vars).
DOTENV_INJECTED_KEYS: set[str] = set()
DEFAULT_PROVIDER_ALIASES = {
    "local": "local-rule",
    "local-structure": "local-structure-shadow",
    "local-flow": "local-flow-shadow",
    "local-structure-flow": "local-structure-flow-shadow",
    "codex": "codex-auth",
    "claude-code": "claude-code-auth",
}
CUSTOM_LLM_WIRE_APIS = {"chat-completions", "responses"}
TRAILING_STOP_MODES = {"off", "shadow", "live"}
STRUCTURE_GATE_MODES = {"off", "shadow", "enforce"}
CUSTOM_PROVIDER_PREFIX = "openai-compatible:"
CUSTOM_PROVIDER_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
MAX_CUSTOM_LLM_PROVIDERS = 8
CUSTOM_LLM_PROVIDER_KEYS = {
    "id",
    "base_url",
    "api_key",
    "model",
    "reasoning_effort",
    "wire_api",
    "require_api_key",
    "extra_headers",
    "pricing",
}
PROTECTED_CUSTOM_HEADER_NAMES = {
    "authorization",
    "content-length",
    "content-type",
    "host",
}
HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")


def _parse_database_url(raw: str | None) -> str:
    value = (raw or DEFAULT_DATABASE_URL).strip()
    try:
        url = make_url(value)
    except ArgumentError as exc:
        raise ValueError("database URL is invalid") from exc
    if url.drivername != "sqlite+aiosqlite":
        raise ValueError("database URL must use sqlite+aiosqlite")
    if not url.database:
        raise ValueError("database URL must name a SQLite database")
    if url.database == ":memory:" or url.database.startswith("file:"):
        raise ValueError("database URL must name a file-backed SQLite path")
    return value


def load_dotenv(path: Path | None = None) -> None:
    """Populate ``os.environ`` from a ``.env`` file without overriding real vars.

    Parses simple ``KEY=VALUE`` lines (``export`` prefix, ``#`` comments and
    surrounding quotes are tolerated). Existing environment variables always win,
    so an explicit ``export`` in the shell overrides the file. A missing file is
    a silent no-op.
    """

    path = path or Path(os.environ.get(ENV_FILE_VARIABLE, ".env"))
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
            DOTENV_INJECTED_KEYS.add(key)


def _parse_cadences(raw: str | None) -> tuple[str, ...]:
    """Parse the one live decision cadence from ``CANDLEPILOT_CADENCES``.

    This used to hand any string through and let the engine reject it at
    construction, three layers later. The value is a typo away from wrong and
    the parser is where it is read, so it is also where it is checked.
    """

    if not raw or not raw.strip():
        return (DEFAULT_DECISION_CADENCE,)
    requested = {item.strip() for item in raw.split(",") if item.strip()}
    if not requested:
        return (DEFAULT_DECISION_CADENCE,)
    unsupported = requested - set(SUPPORTED_CADENCES)
    if unsupported:
        raise ValueError(
            f"unsupported cadences: {', '.join(sorted(unsupported))}; "
            f"choose from {', '.join(SUPPORTED_CADENCES)}"
        )
    if len(requested) != 1:
        raise ValueError("exactly one analysis cadence must be selected")
    return tuple(cadence for cadence in SUPPORTED_CADENCES if cadence in requested)


def _parse_positive_number[T: (int, float)](
    raw: str | None, cast: type[T], *, name: str
) -> T | None:
    """Parse an optional positive run limit; only a blank value is unbounded."""

    if not raw or not raw.strip():
        return None
    try:
        value = cast(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def _parse_bind_port(raw: str | None) -> int:
    try:
        value = int((raw or "").strip())
    except ValueError as exc:
        raise ValueError("CANDLEPILOT_PORT must be an integer") from exc
    if not 1 <= value <= 65535:
        raise ValueError("CANDLEPILOT_PORT must be between 1 and 65535")
    return value


def _parse_inference_timeout(raw: str | None) -> float:
    try:
        value = float((raw or "").strip())
    except ValueError as exc:
        raise ValueError("CANDLEPILOT_LLM_TIMEOUT must be a number") from exc
    if not math.isfinite(value):
        raise ValueError("CANDLEPILOT_LLM_TIMEOUT must be finite")
    if value <= 0:
        raise ValueError("CANDLEPILOT_LLM_TIMEOUT must be positive")
    return value


def _parse_candidates_per_cycle(raw: str | None) -> int:
    if not raw or not raw.strip():
        return 5
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError("CANDLEPILOT_CANDIDATES_PER_CYCLE must be an integer") from exc
    if not 1 <= value <= 20:
        raise ValueError("CANDLEPILOT_CANDIDATES_PER_CYCLE must be between 1 and 20")
    return value


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


def _parse_daily_loss_percent(raw: str | None) -> Decimal:
    value = (raw or "").strip() or "5"
    try:
        percent = Decimal(value)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("CANDLEPILOT_DAILY_LOSS_PERCENT must be a number") from exc
    if not percent.is_finite():
        raise ValueError("CANDLEPILOT_DAILY_LOSS_PERCENT must be finite")
    if not Decimal("0.1") <= percent <= Decimal("50"):
        raise ValueError("CANDLEPILOT_DAILY_LOSS_PERCENT must be between 0.1 and 50")
    return percent / Decimal("100")


def _parse_provider_name(raw: str | None) -> str | None:
    if not raw or not raw.strip():
        return None
    alias = raw.strip().lower()
    # Additional custom endpoints are addressed by id, e.g. "custom:groq".
    prefix, separator, identifier = alias.partition(":")
    if separator and prefix == "custom":
        if not CUSTOM_PROVIDER_ID_PATTERN.fullmatch(identifier):
            raise ValueError(
                f"invalid custom LLM provider id in {raw!r}: "
                "expected [a-z0-9][a-z0-9-]* (max 31 chars)"
            )
        return f"{CUSTOM_PROVIDER_PREFIX}{identifier}"
    try:
        return DEFAULT_PROVIDER_ALIASES[alias]
    except KeyError as exc:
        choices = ", ".join((*DEFAULT_PROVIDER_ALIASES, "custom:<id>"))
        raise ValueError(
            f"unsupported provider in CANDLEPILOT_PROVIDER_CHAIN: {raw!r}; "
            f"choose one of {choices}"
        ) from exc


def _parse_provider_chain(raw: str | None) -> tuple[str, ...]:
    if not raw or not raw.strip():
        return ()
    providers: list[str] = []
    for item in raw.split(","):
        provider = _parse_provider_name(item)
        if provider is None:
            continue
        if provider in providers:
            raise ValueError("CANDLEPILOT_PROVIDER_CHAIN cannot contain duplicates")
        providers.append(provider)
    if len(providers) != 1:
        raise ValueError("CANDLEPILOT_PROVIDER_CHAIN must contain exactly one provider")
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


def _parse_trailing_stop_mode(raw: str | None) -> str:
    value = (raw or "shadow").strip().lower()
    if value not in TRAILING_STOP_MODES:
        raise ValueError(
            "CANDLEPILOT_TRAILING_STOP_MODE must be off, shadow, or live"
        )
    return value


def _parse_structure_gate_mode(raw: str | None) -> str:
    value = (raw or "shadow").strip().lower()
    if value not in STRUCTURE_GATE_MODES:
        raise ValueError(
            "CANDLEPILOT_STRUCTURE_GATE_MODE must be off, shadow, or enforce"
        )
    return value


def _validate_custom_llm_headers(parsed: object) -> dict[str, SecretStr]:
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
class CustomLlmProvider:
    """One additional OpenAI-compatible endpoint declared in the JSON list."""

    id: str
    base_url: str
    api_key: SecretStr | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    wire_api: str = "chat-completions"
    require_api_key: bool = True
    extra_headers: dict[str, SecretStr] | None = None
    #: models.dev provider id this endpoint bills as, e.g. ``xai``.
    #:
    #: It cannot be inferred. The same model is resold by many providers at
    #: different rates -- grok-4.5 is listed under a dozen, and not all charge
    #: what xAI charges -- and an OpenAI-compatible endpoint is exactly the
    #: aggregator case, so neither the model name nor the base URL identifies
    #: whose price applies. Left unset, cost stays unknown rather than guessed.
    pricing: str | None = None

    @property
    def provider_name(self) -> str:
        return f"{CUSTOM_PROVIDER_PREFIX}{self.id}"


LEGACY_CUSTOM_LLM_ENV = (
    "CANDLEPILOT_CUSTOM_LLM_BASE_URL",
    "CANDLEPILOT_CUSTOM_LLM_API_KEY",
    "CANDLEPILOT_CUSTOM_LLM_MODEL",
    "CANDLEPILOT_CUSTOM_LLM_REASONING_EFFORT",
    "CANDLEPILOT_CUSTOM_LLM_WIRE_API",
    "CANDLEPILOT_CUSTOM_LLM_REQUIRE_API_KEY",
    "CANDLEPILOT_CUSTOM_LLM_EXTRA_HEADERS_JSON",
)
REMOVED_PROVIDER_ENV = "CANDLEPILOT_DEFAULT_PROVIDER"


def _reject_removed_mode_env(env: Mapping[str, str]) -> None:
    """Fail loudly on the removed runtime mode.

    Binance testnet is the only mode now. Ignoring a stale CANDLEPILOT_MODE
    would let someone keep believing they are on a simulated account when every
    order goes to the exchange, which is the one misreading worth an error.
    """

    if not env.get("CANDLEPILOT_MODE", "").strip():
        return
    raise ValueError(
        "CANDLEPILOT_MODE was removed: the simulated and backtest run modes are "
        "gone and Binance testnet is the only account traded. Delete the line "
        "from .env. Backtests are now an on-demand analysis, not a run mode."
    )


def _reject_legacy_custom_llm_env(env: Mapping[str, str]) -> None:
    """Fail loudly on the removed single-endpoint variables.

    Ignoring them would silently drop a configured provider, so point at the
    replacement instead of starting with a Custom API the user still expects.
    """

    present = sorted(key for key in LEGACY_CUSTOM_LLM_ENV if env.get(key, "").strip())
    if not present:
        return
    raise ValueError(
        "these single-endpoint Custom API variables were removed: "
        + ", ".join(present)
        + ". Define every endpoint in CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON instead, e.g. "
        '[{"id":"main","base_url":"https://api.example/v1","api_key":"...","model":"..."}]'
    )


def _reject_removed_provider_env(env: Mapping[str, str]) -> None:
    if not env.get(REMOVED_PROVIDER_ENV, "").strip():
        return
    raise ValueError(
        "CANDLEPILOT_DEFAULT_PROVIDER was removed: define the complete ordered "
        "route in CANDLEPILOT_PROVIDER_CHAIN instead"
    )


def _parse_custom_llm_providers(raw: str | None) -> tuple[CustomLlmProvider, ...]:
    """Parse the custom endpoints from ``CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON``.

    This is the only source of Custom API providers. The flat
    ``CANDLEPILOT_CUSTOM_LLM_*`` vars it replaced are rejected by
    :func:`_reject_legacy_custom_llm_env`, and no unsuffixed
    ``openai-compatible`` provider survives them, so an endpoint that is not
    listed here does not exist. Each entry needs a unique ``id``, which becomes
    the provider ``openai-compatible:<id>``.
    """

    if raw is None or not raw.strip():
        return ()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON must be JSON") from exc
    if not isinstance(parsed, list):
        raise ValueError("CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON must be a JSON array")
    if len(parsed) > MAX_CUSTOM_LLM_PROVIDERS:
        raise ValueError(
            f"at most {MAX_CUSTOM_LLM_PROVIDERS} custom LLM providers may be configured"
        )
    providers: list[CustomLlmProvider] = []
    seen: set[str] = set()
    for entry in parsed:
        if not isinstance(entry, dict):
            raise ValueError("each custom LLM provider must be a JSON object")
        unknown = set(entry) - CUSTOM_LLM_PROVIDER_KEYS
        if unknown:
            raise ValueError(
                f"unknown custom LLM provider keys: {', '.join(sorted(unknown))}"
            )
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not CUSTOM_PROVIDER_ID_PATTERN.fullmatch(identifier):
            raise ValueError(
                "custom LLM provider id must match [a-z0-9][a-z0-9-]* (max 31 chars)"
            )
        if identifier in seen:
            raise ValueError(f"duplicate custom LLM provider id: {identifier}")
        seen.add(identifier)
        base_url = entry.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError(f"custom LLM provider {identifier} requires base_url")
        api_key = entry.get("api_key")
        if api_key is not None and (not isinstance(api_key, str) or not api_key):
            raise ValueError(f"custom LLM provider {identifier} api_key must be a string")
        for field in ("model", "reasoning_effort", "pricing"):
            value = entry.get(field)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"custom LLM provider {identifier} {field} must be a string")
        require_api_key = entry.get("require_api_key", True)
        if not isinstance(require_api_key, bool):
            raise ValueError(
                f"custom LLM provider {identifier} require_api_key must be true or false"
            )
        wire_api = entry.get("wire_api")
        if wire_api is not None and not isinstance(wire_api, str):
            raise ValueError(f"custom LLM provider {identifier} wire_api must be a string")
        headers = entry.get("extra_headers")
        providers.append(
            CustomLlmProvider(
                id=identifier,
                base_url=base_url.strip(),
                api_key=SecretStr(api_key) if api_key else None,
                model=entry.get("model") or None,
                reasoning_effort=entry.get("reasoning_effort") or None,
                wire_api=_parse_custom_llm_wire_api(wire_api),
                require_api_key=require_api_key,
                extra_headers=_validate_custom_llm_headers(headers) if headers else None,
                pricing=(entry.get("pricing") or "").strip() or None,
            )
        )
    return tuple(providers)


@dataclass(frozen=True, slots=True)
class Settings:
    database_url: str = DEFAULT_DATABASE_URL
    data_dir: Path = Path("data")
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    max_leverage: int = 10
    max_risk_fraction: Decimal = Decimal("0.01")
    max_portfolio_risk_fraction: Decimal = Decimal("0.04")
    max_margin_fraction: Decimal = Decimal("0.80")
    max_symbol_margin_fraction: Decimal = Decimal("0.10")
    daily_loss_fraction: Decimal = Decimal("0.05")
    minimum_reward_risk_ratio: Decimal = Decimal("1.15")
    inference_timeout_seconds: float = 45.0
    max_snapshot_age_seconds: int = 75
    cadences: tuple[str, ...] = (DEFAULT_DECISION_CADENCE,)
    candidates_per_cycle: int = 5
    trailing_stop_mode: str = "shadow"
    structure_gate_mode: str = "shadow"
    max_run_seconds: int | None = None
    max_run_cost_usd: float | None = None
    provider_chain: tuple[str, ...] = ()
    codex_model: str | None = None
    codex_reasoning_effort: str | None = None
    claude_model: str | None = None
    claude_effort: str | None = None
    custom_llm_providers: tuple[CustomLlmProvider, ...] = ()
    binance_testnet_api_key: SecretStr | None = None
    binance_testnet_api_secret: SecretStr | None = None
    auth_enabled: bool = False
    auth_username: str | None = None
    auth_password_hash: SecretStr | None = None
    auth_session_secret: SecretStr | None = None
    auth_session_ttl_seconds: int = 7 * 24 * 60 * 60
    auth_cookie_secure: bool = False

    @classmethod
    def from_env(cls) -> Settings:
        return cls.from_mapping(os.environ)

    @classmethod
    def from_mapping(cls, env: Mapping[str, str]) -> Settings:
        """Build settings from any env-shaped mapping.

        Keeping this pure lets a candidate ``.env`` be validated with exactly the
        same parsers as startup, without mutating ``os.environ`` underneath a
        concurrent LLM subprocess call.
        """

        def get(key: str, default: str | None = None) -> str | None:
            value = env.get(key, default)
            return value if value is not None else None

        _reject_removed_mode_env(env)
        _reject_legacy_custom_llm_env(env)
        _reject_removed_provider_env(env)
        settings = cls(
            database_url=_parse_database_url(get("CANDLEPILOT_DATABASE_URL")),
            data_dir=Path(get("CANDLEPILOT_DATA_DIR", "data")),
            bind_host=get("CANDLEPILOT_HOST", "127.0.0.1"),
            bind_port=_parse_bind_port(get("CANDLEPILOT_PORT", "8000")),
            inference_timeout_seconds=_parse_inference_timeout(
                get("CANDLEPILOT_LLM_TIMEOUT", "45")
            ),
            max_snapshot_age_seconds=_parse_snapshot_age(
                get("CANDLEPILOT_MAX_SNAPSHOT_AGE_SECONDS")
            ),
            daily_loss_fraction=_parse_daily_loss_percent(
                get("CANDLEPILOT_DAILY_LOSS_PERCENT")
            ),
            cadences=_parse_cadences(get("CANDLEPILOT_CADENCES")),
            candidates_per_cycle=_parse_candidates_per_cycle(
                get("CANDLEPILOT_CANDIDATES_PER_CYCLE")
            ),
            trailing_stop_mode=_parse_trailing_stop_mode(
                get("CANDLEPILOT_TRAILING_STOP_MODE")
            ),
            structure_gate_mode=_parse_structure_gate_mode(
                get("CANDLEPILOT_STRUCTURE_GATE_MODE")
            ),
            max_run_seconds=_parse_positive_number(
                get("CANDLEPILOT_MAX_RUN_SECONDS"),
                int,
                name="CANDLEPILOT_MAX_RUN_SECONDS",
            ),
            max_run_cost_usd=_parse_positive_number(
                get("CANDLEPILOT_MAX_RUN_COST_USD"),
                float,
                name="CANDLEPILOT_MAX_RUN_COST_USD",
            ),
            provider_chain=_parse_provider_chain(get("CANDLEPILOT_PROVIDER_CHAIN")),
            codex_model=get("CANDLEPILOT_CODEX_MODEL") or None,
            codex_reasoning_effort=get("CANDLEPILOT_CODEX_REASONING_EFFORT") or None,
            claude_model=get("CANDLEPILOT_CLAUDE_MODEL") or None,
            claude_effort=get("CANDLEPILOT_CLAUDE_EFFORT") or None,
            custom_llm_providers=_parse_custom_llm_providers(
                get("CANDLEPILOT_CUSTOM_LLM_PROVIDERS_JSON")
            ),
            binance_testnet_api_key=SecretStr(get("BINANCE_TESTNET_API_KEY") or "")
            if get("BINANCE_TESTNET_API_KEY")
            else None,
            binance_testnet_api_secret=SecretStr(get("BINANCE_TESTNET_API_SECRET") or "")
            if get("BINANCE_TESTNET_API_SECRET")
            else None,
            auth_enabled=_parse_boolean(
                get("CANDLEPILOT_AUTH_ENABLED"),
                name="CANDLEPILOT_AUTH_ENABLED",
                default=False,
            ),
            auth_username=(get("CANDLEPILOT_AUTH_USERNAME") or "").strip() or None,
            auth_password_hash=SecretStr(get("CANDLEPILOT_AUTH_PASSWORD_HASH") or "")
            if get("CANDLEPILOT_AUTH_PASSWORD_HASH")
            else None,
            auth_session_secret=SecretStr(get("CANDLEPILOT_AUTH_SESSION_SECRET") or "")
            if get("CANDLEPILOT_AUTH_SESSION_SECRET")
            else None,
            auth_session_ttl_seconds=int(get("CANDLEPILOT_AUTH_SESSION_TTL_SECONDS", "604800")),
            auth_cookie_secure=_parse_boolean(
                get("CANDLEPILOT_AUTH_COOKIE_SECURE"),
                name="CANDLEPILOT_AUTH_COOKIE_SECURE",
                default=False,
            ),
        )
        if settings.auth_enabled:
            if not settings.auth_username or not re.fullmatch(r"[A-Za-z0-9_.@-]{3,64}", settings.auth_username):
                raise ValueError("CANDLEPILOT_AUTH_USERNAME must contain 3-64 safe characters")
            if settings.auth_password_hash is None:
                raise ValueError("CANDLEPILOT_AUTH_PASSWORD_HASH is required when authentication is enabled")
            validate_password_hash(settings.auth_password_hash.get_secret_value())
            if (
                settings.auth_session_secret is None
                or len(settings.auth_session_secret.get_secret_value()) < 32
            ):
                raise ValueError("CANDLEPILOT_AUTH_SESSION_SECRET must contain at least 32 characters")
            if not 300 <= settings.auth_session_ttl_seconds <= 7 * 24 * 60 * 60:
                raise ValueError("CANDLEPILOT_AUTH_SESSION_TTL_SECONDS must be between 300 and 604800")
        validate_provider_references(settings)
        return settings


def validate_provider_references(
    settings: Settings,
    available_provider_names: Collection[str] | None = None,
) -> None:
    """Reject routes that reference providers absent from the same config.

    The optional names let ``create_app`` validate a deliberately injected
    provider registry in tests or embeddings. Normal configuration parsing uses
    the two built-in CLI providers plus every Custom API id in that candidate.
    """

    if available_provider_names is None:
        known = set(DEFAULT_PROVIDER_ALIASES.values())
        known.update(provider.provider_name for provider in settings.custom_llm_providers)
    else:
        known = set(available_provider_names)

    problems: list[str] = []
    missing_route = tuple(
        provider for provider in settings.provider_chain if provider not in known
    )
    if missing_route:
        problems.append(
            "CANDLEPILOT_PROVIDER_CHAIN references unknown provider(s): "
            f"{', '.join(missing_route)}"
        )
    if problems:
        raise ValueError(
            "; ".join(problems)
            + ". Update the complete Provider route before renaming or deleting "
            "a referenced Custom API id."
        )
