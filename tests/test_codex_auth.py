import asyncio
from pathlib import Path

from candlepilot.codex_auth import CodexAuthManager, parse_device_auth_line


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
