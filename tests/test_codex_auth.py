import asyncio
from pathlib import Path

from candlepilot.codex_auth import CodexAuthManager, parse_device_auth_line, sanitize_rate_limits


def _write_fake_codex(path: Path) -> Path:
    path.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "login" ]; then\n'
        '  echo "Open https://auth.openai.com/codex/device"\n'
        '  echo "Enter ABCD-EFGH"\n'
        "  sleep 0.1\n"
        "  exit 0\n"
        "fi\n"
        'if [ "$1" = "logout" ]; then exit 0; fi\n'
        'if [ "$1" = "app-server" ]; then\n'
        '  read init; echo \'{"id":1,"result":{"serverInfo":{"name":"codex"}}}\'\n'
        '  read usage; echo \'{"id":2,"result":{"rateLimits":{"planType":"team","limitId":"codex","limitName":"Codex","primary":{"usedPercent":23,"windowDurationMins":43200,"resetsAt":1785542400}}}}\'\n'
        "  exit 0\n"
        "fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def test_device_auth_parser_accepts_only_safe_codex_links_and_codes() -> None:
    assert parse_device_auth_line(
        "\x1b[32mOpen https://auth.openai.com/codex/device and enter ABCD-EFGH\x1b[0m"
    ) == ("https://auth.openai.com/codex/device", "ABCD-EFGH")
    assert parse_device_auth_line(
        "https://attacker.example/device?token=secret ABCD-EFGH"
    ) == (None, "ABCD-EFGH")
    assert parse_device_auth_line(
        "Open https://auth.openai.com/codex/device?token=secret using this one-time code"
    ) == (None, None)


def test_codex_auth_manager_exposes_device_flow_without_raw_output(tmp_path: Path) -> None:
    async def exercise() -> None:
        manager = CodexAuthManager(executable=_write_fake_codex(tmp_path / "codex"))
        started = await manager.start_login()
        assert started["state"] == "starting"

        for _ in range(100):
            status = manager.status()
            if status["state"] == "succeeded":
                break
            await asyncio.sleep(0.01)
        assert status["verification_uri"] == "https://auth.openai.com/codex/device"
        assert status["user_code"] == "ABCD-EFGH"
        assert status["message"] == "Codex CLI 登录成功"
        assert set(status) == {
            "available",
            "state",
            "verification_uri",
            "user_code",
            "message",
            "started_at",
            "finished_at",
        }

        logged_out = await manager.logout()
        assert logged_out["state"] == "idle"
        assert logged_out["user_code"] is None
        await manager.close()

    asyncio.run(exercise())


def test_codex_auth_manager_can_cancel_before_process_starts(tmp_path: Path) -> None:
    async def exercise() -> None:
        manager = CodexAuthManager(executable=_write_fake_codex(tmp_path / "codex"))
        await manager.start_login()
        cancelled = await manager.cancel_login()
        assert cancelled["state"] == "cancelled"
        assert manager.active is False

    asyncio.run(exercise())


def test_codex_auth_manager_reads_only_sanitized_dynamic_rate_limit_windows(
    tmp_path: Path,
) -> None:
    async def exercise() -> None:
        manager = CodexAuthManager(executable=_write_fake_codex(tmp_path / "codex"))
        usage = await manager.rate_limits()
        assert usage["available"] is True
        assert usage["buckets"] == [
            {
                "limit_id": "codex",
                "limit_name": "Codex",
                "plan_type": "team",
                "windows": [
                    {
                        "kind": "primary",
                        "used_percent": 23,
                        "remaining_percent": 77,
                        "window_duration_minutes": 43200,
                        "resets_at": usage["buckets"][0]["windows"][0]["resets_at"],
                    }
                ],
            }
        ]
        assert set(usage) == {"available", "buckets", "checked_at", "message"}

    asyncio.run(exercise())


def test_rate_limit_sanitizer_does_not_invent_missing_windows() -> None:
    usage = sanitize_rate_limits(
        {
            "rateLimits": {
                "planType": "team",
                "primary": {"usedPercent": 10, "windowDurationMins": 43200},
                "secret": "must-not-cross-api-boundary",
            }
        }
    )
    assert len(usage["buckets"][0]["windows"]) == 1
    assert "secret" not in repr(usage)
