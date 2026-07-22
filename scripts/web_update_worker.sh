#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG_FILE="/etc/candlepilot/web-update.conf"
STATUS_DIR="/var/lib/candlepilot"
STATUS_FILE="$STATUS_DIR/update-status.json"
BACKUP_MANIFEST_FILE="$STATUS_DIR/backups.json"
BACKUP_STATUS_FILE="$STATUS_DIR/backup-status.json"
LOG_FILE="/var/log/candlepilot-update.log"

[[ -r "$CONFIG_FILE" ]] || { echo "missing $CONFIG_FILE" >&2; exit 1; }
# shellcheck disable=SC1090
source "$CONFIG_FILE"

install -d -m 0755 "$STATUS_DIR"
touch "$LOG_FILE"
chmod 0600 "$LOG_FILE"

write_status() {
  local phase="$1" message="$2" started_at="$3" finished_at="${4:-}"
  local from_commit="${5:-}" current_commit="${6:-}" backup="${7:-}"
  python3 - "$STATUS_FILE" "$phase" "$message" "$started_at" "$finished_at" \
    "$from_commit" "$current_commit" "$backup" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "phase": sys.argv[2],
    "message": sys.argv[3],
    "started_at": sys.argv[4] or None,
    "finished_at": sys.argv[5] or None,
    "from_commit": sys.argv[6] or None,
    "current_commit": sys.argv[7] or None,
    "backup": sys.argv[8] or None,
}
temporary = path.with_suffix(".tmp")
temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
os.chmod(temporary, 0o644)
os.replace(temporary, path)
PY
}

refresh_backup_manifest() {
  python3 - "$BACKUP_ROOT" "$BACKUP_MANIFEST_FILE" <<'PY'
import json
import os
import re
import stat
import sys
from datetime import UTC, datetime
from pathlib import Path

root = Path(sys.argv[1])
manifest = Path(sys.argv[2])
name_pattern = re.compile(r"^(\d{8}T\d{6}Z)-([0-9a-f]{7,64})$")
commit_pattern = re.compile(r"^[0-9a-f]{7,64}$")

def allocated_size(path: Path) -> int:
    total = 0
    for directory, names, files in os.walk(path, followlinks=False):
        names[:] = [name for name in names if not (Path(directory) / name).is_symlink()]
        for name in files:
            candidate = Path(directory) / name
            try:
                info = candidate.lstat()
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                total += info.st_blocks * 512
    return total

backups = []
if root.is_dir() and not root.is_symlink():
    for candidate in root.iterdir():
        match = name_pattern.fullmatch(candidate.name)
        if not match or not candidate.is_dir() or candidate.is_symlink():
            continue
        source_commit = None
        try:
            stored_commit = (candidate / "source-commit").read_text(encoding="utf-8").strip()
            if commit_pattern.fullmatch(stored_commit):
                source_commit = stored_commit
        except OSError:
            pass
        created = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        backups.append({
            "id": candidate.name,
            "created_at": created.isoformat().replace("+00:00", "Z"),
            "source_commit": source_commit,
            "size_bytes": allocated_size(candidate),
            "protected": False,
        })
backups.sort(key=lambda item: item["id"], reverse=True)
if backups:
    backups[0]["protected"] = True
payload = {
    "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    "backups": backups,
}
manifest.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
temporary = manifest.with_suffix(".tmp")
temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
os.chmod(temporary, 0o644)
os.replace(temporary, manifest)
PY
}

write_backup_status() {
  local phase="$1" action="$2" message="$3" started_at="$4"
  local finished_at="${5:-}" backup_id="${6:-}" reclaimed_bytes="${7:-}"
  python3 - "$BACKUP_STATUS_FILE" "$phase" "$action" "$message" "$started_at" \
    "$finished_at" "$backup_id" "$reclaimed_bytes" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "phase": sys.argv[2],
    "action": sys.argv[3],
    "message": sys.argv[4],
    "started_at": sys.argv[5] or None,
    "finished_at": sys.argv[6] or None,
    "backup_id": sys.argv[7] or None,
    "reclaimed_bytes": int(sys.argv[8]) if sys.argv[8] else None,
}
temporary = path.with_suffix(".tmp")
temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
os.chmod(temporary, 0o644)
os.replace(temporary, path)
PY
}

delete_backup() {
  python3 - "$BACKUP_ROOT" "$1" <<'PY'
import os
import re
import shutil
import stat
import sys
from pathlib import Path

raw_root = Path(sys.argv[1])
if raw_root.is_symlink():
    raise SystemExit("unsafe backup root")
root = raw_root.resolve(strict=True)
backup_id = sys.argv[2]
pattern = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{7,64}$")
if not pattern.fullmatch(backup_id):
    raise SystemExit("invalid backup id")
if str(root) == "/":
    raise SystemExit("unsafe backup root")
backups = sorted(
    candidate.name
    for candidate in root.iterdir()
    if pattern.fullmatch(candidate.name) and candidate.is_dir() and not candidate.is_symlink()
)
if backup_id not in backups:
    raise SystemExit("backup does not exist")
if len(backups) <= 1 or backup_id == backups[-1]:
    raise SystemExit("latest backup is protected")
target = root / backup_id
if target.resolve(strict=True).parent != root or target.is_symlink():
    raise SystemExit("unsafe backup target")
allocated = 0
for directory, names, files in os.walk(target, followlinks=False):
    names[:] = [name for name in names if not (Path(directory) / name).is_symlink()]
    for name in files:
        info = (Path(directory) / name).lstat()
        if stat.S_ISREG(info.st_mode):
            allocated += info.st_blocks * 512
shutil.rmtree(target)
print(allocated)
PY
}

if [[ "${1:-}" == "--refresh-manifest" ]]; then
  [[ "$#" -eq 1 ]] || exit 64
  refresh_backup_manifest
  exit 0
fi
[[ "$#" -eq 0 ]] || exit 64

exec 9>/run/candlepilot-update.lock
if ! flock -n 9; then
  exit 75
fi

action_line="$(cat /run/candlepilot-update/action 2>/dev/null || printf 'update')"
rm -f -- /run/candlepilot-update/action
action="${action_line%%$'\t'*}"
backup_id=""
if [[ "$action_line" == *$'\t'* ]]; then
  backup_id="${action_line#*$'\t'}"
fi

if [[ "$action" == "refresh-backups" ]]; then
  started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  write_backup_status "running" "refresh" "正在刷新备份清单" "$started_at"
  if refresh_backup_manifest >>"$LOG_FILE" 2>&1; then
    finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    write_backup_status "completed" "refresh" "备份清单已刷新" "$started_at" "$finished_at"
    exit 0
  fi
  finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  write_backup_status "failed" "refresh" "备份清单刷新失败，请检查更新服务日志" "$started_at" "$finished_at"
  exit 1
fi

if [[ "$action" == "delete-backup" ]]; then
  started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  write_backup_status "running" "delete" "正在删除过时备份" "$started_at" "" "$backup_id"
  set +e
  reclaimed_bytes="$(delete_backup "$backup_id" 2>>"$LOG_FILE")"
  exit_code=$?
  set -e
  if (( exit_code == 0 )) && refresh_backup_manifest >>"$LOG_FILE" 2>&1; then
    finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
    write_backup_status "completed" "delete" "过时备份已删除" "$started_at" "$finished_at" "$backup_id" "$reclaimed_bytes"
    exit 0
  fi
  finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  write_backup_status "failed" "delete" "备份删除失败，请检查更新服务日志" "$started_at" "$finished_at" "$backup_id"
  exit 1
fi

[[ "$action" == "update" ]] || exit 64

started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
from_commit="$(runuser -u "$APP_USER" -- git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || true)"
write_status "running" "正在检查并安装更新" "$started_at" "" "$from_commit" "$from_commit"

# Give the API response time to reach Nginx before the installer stops the app.
sleep 2
set +e
env \
  CANDLEPILOT_APP_USER="$APP_USER" \
  CANDLEPILOT_APP_DIR="$APP_DIR" \
  CANDLEPILOT_REPO_URL="$REPO_URL" \
  CANDLEPILOT_BRANCH="$BRANCH" \
  CANDLEPILOT_UPDATE_BACKUP_ROOT="$BACKUP_ROOT" \
  CANDLEPILOT_UPDATE_CONFIRM=UPDATE \
  /usr/local/libexec/candlepilot-install-vps >"$LOG_FILE" 2>&1
exit_code=$?
set -e

finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
current_commit="$(runuser -u "$APP_USER" -- git -C "$APP_DIR" rev-parse HEAD 2>/dev/null || true)"
backup="$(sed -n -e 's/^Backup: //p' -e 's/^Backup retained at //p' "$LOG_FILE" | tail -n 1)"
if (( exit_code == 0 )); then
  if [[ "$from_commit" == "$current_commit" ]]; then
    message="已经是最新版本"
  else
    message="更新完成，服务已通过健康检查"
  fi
  write_status "completed" "$message" "$started_at" "$finished_at" \
    "$from_commit" "$current_commit" "$backup"
else
  write_status "failed" "更新失败；若已修改文件则安装器已尝试回滚，请检查更新服务日志" \
    "$started_at" "$finished_at" "$from_commit" "$current_commit" "$backup"
fi
refresh_backup_manifest >>"$LOG_FILE" 2>&1 || true
exit "$exit_code"
