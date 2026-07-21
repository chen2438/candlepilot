import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_launcher_rejects_arguments_before_calling_systemd() -> None:
    result = subprocess.run(
        [str(ROOT / "scripts/web_update_launcher.sh"), "unexpected"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 64
    assert "accepts no arguments" in result.stderr


def test_root_worker_never_executes_the_application_users_installer() -> None:
    worker = (ROOT / "scripts/web_update_worker.sh").read_text(encoding="utf-8")
    installer = (ROOT / "scripts/install_vps.sh").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts/web_update_launcher.sh").read_text(encoding="utf-8")

    assert '/usr/local/libexec/candlepilot-install-vps' in worker
    assert 'bash "$APP_DIR/scripts/install_vps.sh"' not in worker
    assert "HEAD:scripts/install_vps.sh" in installer
    assert "candlepilot-update.path" in installer
    assert "/run/candlepilot-update/request" in launcher
    assert "sudo" not in launcher
    assert "systemctl" not in launcher
    assert "NoNewPrivileges=true" in installer


def test_uninstaller_removes_the_privileged_update_surface() -> None:
    uninstaller = (ROOT / "scripts/uninstall_vps.sh").read_text(encoding="utf-8")

    for path in (
        "/etc/systemd/system/candlepilot-update.service",
        "/etc/systemd/system/candlepilot-update.path",
        "/etc/tmpfiles.d/candlepilot-update.conf",
        "/etc/sudoers.d/candlepilot-web-update",
        "/usr/local/sbin/candlepilot-web-update",
        "/usr/local/libexec/candlepilot-web-update-worker",
        "/usr/local/libexec/candlepilot-install-vps",
        "/var/lib/candlepilot",
        "/var/log/candlepilot-update.log",
        "/run/candlepilot-update",
    ):
        assert path in uninstaller
