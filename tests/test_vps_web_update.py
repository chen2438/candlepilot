import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_launcher_rejects_unsupported_actions_before_calling_systemd() -> None:
    result = subprocess.run(
        [str(ROOT / "scripts/web_update_launcher.sh"), "unexpected"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 64
    assert "unsupported CandlePilot maintenance action" in result.stderr


def test_launcher_rejects_backup_path_instead_of_an_id() -> None:
    result = subprocess.run(
        [
            str(ROOT / "scripts/web_update_launcher.sh"),
            "--delete-backup",
            "../../var/backups/candlepilot",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 64
    assert "invalid CandlePilot backup id" in result.stderr


def test_root_worker_never_executes_the_application_users_installer() -> None:
    worker = (ROOT / "scripts/web_update_worker.sh").read_text(encoding="utf-8")
    installer = (ROOT / "scripts/install_vps.sh").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts/web_update_launcher.sh").read_text(encoding="utf-8")

    assert '/usr/local/libexec/candlepilot-install-vps' in worker
    assert 'bash "$APP_DIR/scripts/install_vps.sh"' not in worker
    assert "HEAD:scripts/install_vps.sh" in installer
    assert "candlepilot-update.path" in installer
    assert "/run/candlepilot-update/request" in launcher
    assert "--delete-backup" in launcher
    assert "--delete-stale-backups" in launcher
    assert "--clear-logs" in launcher
    assert "latest backup is protected" in worker
    assert 'target.resolve(strict=True).parent != root' in worker
    assert "len(backups) <= 1" in worker
    assert 'write_backup_status "running" "delete_all"' in worker
    assert "sudo" not in launcher
    assert "systemctl" not in launcher
    assert "NoNewPrivileges=true" in installer
    assert "LogNamespace=candlepilot" in installer
    assert "journalctl --namespace=candlepilot --vacuum-time=1s" in worker
    assert "journalctl --vacuum-time=1s" not in worker


def test_root_worker_bulk_delete_keeps_latest_and_ignores_unrecognized_entries(
    tmp_path: Path,
) -> None:
    worker = (ROOT / "scripts/web_update_worker.sh").read_text(encoding="utf-8")
    marker = 'delete_stale_backups() {\n  python3 - "$BACKUP_ROOT" <<\'PY\'\n'
    program = worker.split(marker, 1)[1].split("\nPY\n}", 1)[0]
    latest = tmp_path / "20260722T100000Z-aaaaaaaa"
    stale_one = tmp_path / "20260721T100000Z-bbbbbbbb"
    stale_two = tmp_path / "20260720T100000Z-cccccccc"
    unrelated = tmp_path / "notes"
    for directory in (latest, stale_one, stale_two, unrelated):
        directory.mkdir()
        (directory / "payload").write_text("backup", encoding="utf-8")

    result = subprocess.run(
        ["python3", "-", str(tmp_path)],
        input=program,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) >= 0
    assert latest.is_dir()
    assert not stale_one.exists()
    assert not stale_two.exists()
    assert unrelated.is_dir()


def test_installer_backs_up_the_database_selected_in_env() -> None:
    installer_path = ROOT / "scripts/install_vps.sh"
    installer = installer_path.read_text(encoding="utf-8")

    syntax = subprocess.run(
        ["bash", "-n", str(installer_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert syntax.returncode == 0, syntax.stderr
    update_body = installer.split("update_existing_installation() {", 1)[1].split(
        '[[ "${EUID}" -eq 0 ]]', 1
    )[0]
    assert 'values.get(\n    "CANDLEPILOT_DATABASE_URL"' in update_body
    assert 'sqlite3 "$installed_database_path"' in update_body
    assert '"$backup_dir/database.sqlite3" "$installed_database_path"' in update_body
    assert '"$APP_DIR/candlepilot.db"' not in update_body
    assert 'target == ":memory:" or target.startswith("file:")' in update_body


def test_uninstaller_removes_the_privileged_update_surface() -> None:
    uninstaller = (ROOT / "scripts/uninstall_vps.sh").read_text(encoding="utf-8")

    for path in (
        "/etc/systemd/system/candlepilot-update.service",
        "/etc/systemd/system/candlepilot-update.path",
        "/etc/systemd/system/candlepilot.service.d",
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


def test_uninstaller_removes_the_validated_configured_backup_root() -> None:
    uninstaller = (ROOT / "scripts/uninstall_vps.sh").read_text(encoding="utf-8")

    assert "BACKUP_ROOT=\"${CANDLEPILOT_UPDATE_BACKUP_ROOT:-}\"" in uninstaller
    assert "UPDATE_CONFIG=/etc/candlepilot/web-update.conf" in uninstaller
    assert 'BACKUP_ROOT="${BACKUP_ROOT:-/var/backups/candlepilot}"' in uninstaller
    assert 'LEXICAL_BACKUP_ROOT="$(realpath -ms -- "$BACKUP_ROOT")"' in uninstaller
    assert "/var/backups)" in uninstaller
    assert 'rm -rf -- "$BACKUP_ROOT"' in uninstaller
