import json
import logging

from candlepilot.observability import JsonFormatter, OperationalMetrics, evaluate_alerts


def test_json_formatter_and_runtime_metrics_are_structured() -> None:
    record = logging.LogRecord(
        name="candlepilot.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="request_completed",
        args=(),
        exc_info=None,
    )
    record.structured = {"request_id": "abc", "status_code": 200}
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "request_completed"
    assert payload["request_id"] == "abc"
    assert payload["status_code"] == 200
    assert payload["timestamp"].endswith("+00:00")

    metrics = OperationalMetrics(latency_window=3)
    for status, duration in ((200, 10.0), (503, 30.0), (200, 20.0), (404, 5.0)):
        metrics.request_started()
        metrics.request_finished(status, duration)
    snapshot = metrics.snapshot()
    assert snapshot["requests_total"] == 4
    assert snapshot["errors_total"] == 1
    assert snapshot["error_rate"] == 0.25
    assert snapshot["in_flight"] == 0
    assert snapshot["average_duration_ms"] == 16.25
    assert snapshot["p95_duration_ms"] == 30.0
    assert snapshot["latency_sample_count"] == 3
    assert snapshot["status_counts"] == {"200": 2, "404": 1, "503": 1}


def test_alert_rules_require_minimum_volume_and_report_safety_failures() -> None:
    alerts = evaluate_alerts(
        {"requests_total": 100, "error_rate": 0.08},
        [
            {
                "provider": "codex-auth",
                "call_count": 10,
                "error_rate": 0.2,
                "p95_duration_ms": 45_000,
            },
            {
                "provider": "claude-code-auth",
                "call_count": 2,
                "error_rate": 1.0,
                "p95_duration_ms": 60_000,
            },
        ],
        emergency_locked=True,
        testnet_unprotected=("BTCUSDT",),
        user_stream_error="listen key expired",
    )
    identifiers = {alert["id"] for alert in alerts}
    assert identifiers == {
        "engine-emergency-lock",
        "testnet-unprotected-position",
        "user-stream-error",
        "runtime-error-rate",
        "provider-error-rate-codex-auth",
        "provider-latency-codex-auth",
    }
    assert "provider-error-rate-claude-code-auth" not in identifiers
    assert next(alert for alert in alerts if alert["id"] == "testnet-unprotected-position")[
        "severity"
    ] == "critical"
