from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from pydantic import SecretStr

from candlepilot.domain.models import TradingMode


DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./candlepilot.db"


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
