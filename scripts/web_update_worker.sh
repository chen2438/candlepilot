#!/usr/bin/env bash
set -Eeuo pipefail

CONFIG_FILE="/etc/candlepilot/web-update.conf"
STATUS_DIR="/var/lib/candlepilot"
STATUS_FILE="$STATUS_DIR/update-status.json"
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

exec 9>/run/candlepilot-update.lock
if ! flock -n 9; then
  exit 75
fi

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
exit "$exit_code"
