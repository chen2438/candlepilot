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


class AlertNotifier:
    """Local alert notification channel with fire/resolve de-duplication.

    Given the currently active alerts on each evaluation, it emits a transition
    event only when an alert first fires or clears, so repeated polling never
    re-notifies for a steady-state alert. Transitions are written to the JSON
    structured log (which external log shippers can forward) and returned for
    audit persistence. No data leaves the host.
    """

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._active: dict[str, dict[str, str]] = {}
        self._logger = logger or logging.getLogger("candlepilot.alerts")

    def diff(self, alerts: list[dict[str, str]]) -> list[dict[str, str]]:
        current = {alert["id"]: alert for alert in alerts}
        events: list[dict[str, str]] = []
        for alert_id, alert in current.items():
            if alert_id not in self._active:
                events.append({**alert, "transition": "fired"})
        for alert_id, alert in self._active.items():
            if alert_id not in current:
                events.append({**alert, "transition": "resolved"})
        self._active = current
        return events

    def emit(self, events: list[dict[str, str]]) -> None:
        for event in events:
            level = (
                logging.ERROR
                if event["transition"] == "fired" and event.get("severity") == "critical"
                else logging.WARNING
            )
            self._logger.log(
                level,
                f"alert_{event['transition']}",
                extra={"structured": {"alert": event}},
            )

    @property
    def active_ids(self) -> tuple[str, ...]:
        return tuple(self._active)


def evaluate_alerts(
    runtime: dict[str, Any],
    provider_metrics: list[dict[str, Any]],
    *,
    emergency_locked: bool = False,
    testnet_unprotected: tuple[str, ...] = (),
    user_stream_error: str | None = None,
    testnet_broker_missing: bool = False,
) -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []

    def add(identifier: str, severity: str, source: str, title: str, detail: str) -> None:
        alerts.append(
            {
                "id": identifier,
                "severity": severity,
                "source": source,
                "title": title,
                "detail": detail,
            }
        )

    if emergency_locked:
        add("engine-emergency-lock", "critical", "engine", "交易引擎处于紧急锁定", "需要人工核查账户与触发原因")
    if testnet_broker_missing:
        add("testnet-broker-missing", "critical", "testnet", "测试网 Broker 未配置", "测试网模式无法安全启动")
    if testnet_unprotected:
        add(
            "testnet-unprotected-position",
            "critical",
            "testnet",
            "测试网存在无保护仓位",
            ", ".join(testnet_unprotected),
        )
    if user_stream_error:
        add("user-stream-error", "warning", "testnet", "测试网用户流异常", user_stream_error)
    if runtime["requests_total"] >= 20 and runtime["error_rate"] >= 0.05:
        add(
            "runtime-error-rate",
            "critical" if runtime["error_rate"] >= 0.20 else "warning",
            "api",
            "API 错误率偏高",
            f"当前错误率 {runtime['error_rate']:.1%}，共 {runtime['requests_total']} 次请求",
        )
    for metric in provider_metrics:
        provider = str(metric["provider"])
        if metric["call_count"] >= 5 and metric["error_rate"] >= 0.10:
            add(
                f"provider-error-rate-{provider}",
                "critical" if metric["error_rate"] >= 0.30 else "warning",
                provider,
                "模型调用错误率偏高",
                f"24 小时错误率 {metric['error_rate']:.1%}，共 {metric['call_count']} 次调用",
            )
        if metric["call_count"] >= 5 and metric["p95_duration_ms"] >= 30_000:
            add(
                f"provider-latency-{provider}",
                "warning",
                provider,
                "模型 P95 延迟偏高",
                f"24 小时 P95 延迟 {metric['p95_duration_ms'] / 1_000:.1f} 秒",
            )
    return alerts
