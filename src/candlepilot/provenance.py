from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel


DECISION_PROMPT_VERSION = "trade-intent-v16"
MARKET_SNAPSHOT_SCHEMA_VERSION = "market-snapshot-v3"
BACKTEST_DATA_SCHEMA_VERSION = "backtest-candles-v1"
#: Version of the microstructure derivation recorded by the collector.
#:
#: The trade tape is summarised at capture time rather than stored -- it is 97%
#: of the volume -- so a recorded imbalance cannot be recomputed later. Bump
#: this whenever FeaturePipeline.microstructure changes what those numbers mean,
#: and the real backtest will refuse the stale captures instead of quietly
#: mixing two definitions into one result.
MICROSTRUCTURE_SCHEMA_VERSION = "microstructure-v1"


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
