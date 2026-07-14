from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter, deque
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        structured = getattr(record, "structured", None)
        if isinstance(structured, dict):
            payload.update(structured)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def configure_structured_logging(level: int = logging.INFO) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


class OperationalMetrics:
    def __init__(self, *, latency_window: int = 1_000) -> None:
        self.started_at = time.monotonic()
        self.requests_total = 0
        self.errors_total = 0
        self.in_flight = 0
        self.total_duration_ms = 0.0
        self.status_counts: Counter[int] = Counter()
        self.durations_ms: deque[float] = deque(maxlen=latency_window)

    def request_started(self) -> None:
        self.in_flight += 1

    def request_finished(self, status_code: int, duration_ms: float) -> None:
        self.in_flight = max(0, self.in_flight - 1)
        self.requests_total += 1
        self.status_counts[status_code] += 1
        if status_code >= 500:
            self.errors_total += 1
        self.total_duration_ms += duration_ms
        self.durations_ms.append(duration_ms)

    def snapshot(self) -> dict[str, Any]:
        durations = sorted(self.durations_ms)
        p95_index = max(0, math.ceil(len(durations) * 0.95) - 1)
        return {
            "uptime_seconds": time.monotonic() - self.started_at,
            "requests_total": self.requests_total,
            "errors_total": self.errors_total,
            "error_rate": self.errors_total / self.requests_total
            if self.requests_total
            else 0.0,
            "in_flight": self.in_flight,
            "average_duration_ms": self.total_duration_ms / self.requests_total
            if self.requests_total
            else 0.0,
            "p95_duration_ms": durations[p95_index] if durations else 0.0,
            "status_counts": {str(code): count for code, count in sorted(self.status_counts.items())},
            "latency_sample_count": len(durations),
        }
