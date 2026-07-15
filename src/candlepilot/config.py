from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from pydantic import SecretStr

from candlepilot.domain.models import TradingMode


DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./candlepilot.db"


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
        return ("1m", "5m", "15m")
    parsed = tuple(item.strip() for item in raw.split(",") if item.strip())
    return parsed or ("1m", "5m", "15m")


def _parse_candidates_per_cycle(raw: str | None) -> int:
    if not raw:
        return 5
    try:
        return int(raw.strip())
    except ValueError:
        return 5


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
    cadences: tuple[str, ...] = ("1m", "5m", "15m")
    candidates_per_cycle: int = 5
    codex_model: str | None = None
    codex_reasoning_effort: str | None = None
    claude_model: str | None = None
    claude_effort: str | None = None
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
            cadences=_parse_cadences(os.getenv("CANDLEPILOT_CADENCES")),
            candidates_per_cycle=_parse_candidates_per_cycle(
                os.getenv("CANDLEPILOT_CANDIDATES_PER_CYCLE")
            ),
            codex_model=os.getenv("CANDLEPILOT_CODEX_MODEL") or None,
            codex_reasoning_effort=os.getenv("CANDLEPILOT_CODEX_REASONING_EFFORT") or None,
            claude_model=os.getenv("CANDLEPILOT_CLAUDE_MODEL") or None,
            claude_effort=os.getenv("CANDLEPILOT_CLAUDE_EFFORT") or None,
            binance_testnet_api_key=SecretStr(os.environ["BINANCE_TESTNET_API_KEY"])
            if os.getenv("BINANCE_TESTNET_API_KEY")
            else None,
            binance_testnet_api_secret=SecretStr(os.environ["BINANCE_TESTNET_API_SECRET"])
            if os.getenv("BINANCE_TESTNET_API_SECRET")
            else None,
        )
