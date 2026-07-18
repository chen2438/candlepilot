from __future__ import annotations

import json
import os
import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from pydantic import SecretStr

from candlepilot.domain.models import SUPPORTED_CADENCES



DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./candlepilot.db"
# Single source of truth for the .env location, shared with the frontend editor.
ENV_FILE_VARIABLE = "CANDLEPILOT_ENV_FILE"
# Keys this process took from .env rather than from a real environment variable.
# A restart must drop these before re-exec, otherwise the inherited old values
# would win over the rewritten .env (load_dotenv never overrides real vars).
DOTENV_INJECTED_KEYS: set[str] = set()
DEFAULT_PROVIDER_ALIASES = {
    "local": "local-rule",
    "local-rule": "local-rule",
    "codex": "codex-auth",
    "codex-auth": "codex-auth",
    "claude": "claude-code-auth",
    "claude code": "claude-code-auth",
    "claude-code": "claude-code-auth",
    "claude-code-auth": "claude-code-auth",
}
CUSTOM_LLM_WIRE_APIS = {"chat-completions", "responses"}
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
    """Parse ``CANDLEPILOT_CADENCES`` into a canonical, validated subset.

    This used to hand any string through and let the engine reject it at
    construction, three layers later. The value is a typo away from wrong and
    the parser is where it is read, so it is also where it is checked.
    """

    if not raw or not raw.strip():
        return SUPPORTED_CADENCES
    requested = {item.strip() for item in raw.split(",") if item.strip()}
    if not requested:
        return SUPPORTED_CADENCES
    unsupported = requested - set(SUPPORTED_CADENCES)
    if unsupported:
        raise ValueError(
            f"unsupported cadences: {', '.join(sorted(unsupported))}; "
            f"choose from {', '.join(SUPPORTED_CADENCES)}"
        )
    return tuple(cadence for cadence in SUPPORTED_CADENCES if cadence in requested)


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
    # Additional custom endpoints are addressed by id, e.g. "custom:groq".
    prefix, separator, identifier = alias.partition(":")
    if separator and prefix in {"custom", "custom-api", "openai-compatible"}:
        if not CUSTOM_PROVIDER_ID_PATTERN.fullmatch(identifier):
            raise ValueError(
                f"invalid custom LLM provider id in {raw!r}: "
                "expected [a-z0-9][a-z0-9-]* (max 31 chars)"
            )
        return f"{CUSTOM_PROVIDER_PREFIX}{identifier}"
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
    max_risk_fraction: Decimal = Decimal("0.02")
    max_positions: int = 8
    max_margin_fraction: Decimal = Decimal("0.60")
    daily_loss_fraction: Decimal = Decimal("0.08")
    inference_timeout_seconds: float = 45.0
    max_snapshot_age_seconds: int = 75
    cadences: tuple[str, ...] = SUPPORTED_CADENCES
    candidates_per_cycle: int = 5
    max_run_seconds: int | None = None
    max_run_cost_usd: float | None = None
    provider_chain: tuple[str, ...] = ()
    default_provider: str | None = None
    codex_model: str | None = None
    codex_reasoning_effort: str | None = None
    claude_model: str | None = None
    claude_effort: str | None = None
    custom_llm_providers: tuple[CustomLlmProvider, ...] = ()
    binance_testnet_api_key: SecretStr | None = None
    binance_testnet_api_secret: SecretStr | None = None

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
        settings = cls(
            database_url=get("CANDLEPILOT_DATABASE_URL", DEFAULT_DATABASE_URL),
            data_dir=Path(get("CANDLEPILOT_DATA_DIR", "data")),
            bind_host=get("CANDLEPILOT_HOST", "127.0.0.1"),
            bind_port=int(get("CANDLEPILOT_PORT", "8000")),
            inference_timeout_seconds=float(get("CANDLEPILOT_LLM_TIMEOUT", "45")),
            max_snapshot_age_seconds=_parse_snapshot_age(
                get("CANDLEPILOT_MAX_SNAPSHOT_AGE_SECONDS")
            ),
            cadences=_parse_cadences(get("CANDLEPILOT_CADENCES")),
            candidates_per_cycle=_parse_candidates_per_cycle(
                get("CANDLEPILOT_CANDIDATES_PER_CYCLE")
            ),
            max_run_seconds=_parse_positive_number(
                get("CANDLEPILOT_MAX_RUN_SECONDS"), int
            ),
            max_run_cost_usd=_parse_positive_number(
                get("CANDLEPILOT_MAX_RUN_COST_USD"), float
            ),
            provider_chain=_parse_provider_chain(get("CANDLEPILOT_PROVIDER_CHAIN")),
            default_provider=_parse_default_provider(
                get("CANDLEPILOT_DEFAULT_PROVIDER")
            ),
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
        )
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
    if settings.default_provider is not None and settings.default_provider not in known:
        problems.append(
            "CANDLEPILOT_DEFAULT_PROVIDER references unknown provider: "
            f"{settings.default_provider}"
        )
    if problems:
        raise ValueError(
            "; ".join(problems)
            + ". Update the Provider route and default before renaming or deleting "
            "a referenced Custom API id."
        )
