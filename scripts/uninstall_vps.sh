#!/usr/bin/env bash
set -euo pipefail

# Remove a CandlePilot installation created by install_vps.sh without removing
# shared operating-system packages such as Nginx, Python, Node.js, or Git.

APP_USER="${CANDLEPILOT_APP_USER:-candlepilot}"
APP_DIR="${CANDLEPILOT_APP_DIR:-/opt/candlepilot}"
REMOVE_APP_USER="${CANDLEPILOT_REMOVE_APP_USER:-}"
CONFIRMATION="${CANDLEPILOT_UNINSTALL_CONFIRM:-}"
DRY_RUN=false

fail() {
  echo "CandlePilot uninstaller: $*" >&2
  exit 1
}

usage() {
  cat <<'EOF'
Usage: uninstall_vps.sh [--dry-run] [--yes]

  --dry-run  Show what would be removed without changing the VPS.
  --yes      Skip the final REMOVE confirmation.

Environment:
  CANDLEPILOT_APP_USER           Application user (default: candlepilot)
  CANDLEPILOT_APP_DIR            Installation directory (default: /opt/candlepilot)
  CANDLEPILOT_REMOVE_APP_USER    true/false; otherwise ask when the user exists
  CANDLEPILOT_UNINSTALL_CONFIRM  Set to REMOVE to skip the final confirmation
EOF
}

while (( $# > 0 )); do
  case "$1" in
    --dry-run) DRY_RUN=true ;;
    --yes) CONFIRMATION="REMOVE" ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
  shift
done

[[ "${EUID}" -eq 0 ]] || fail "run as root (for example: sudo bash scripts/uninstall_vps.sh)"
[[ "$APP_USER" =~ ^[a-z_][a-z0-9_-]*[$]?$ ]] || fail "CANDLEPILOT_APP_USER is invalid"
[[ "$APP_DIR" == /* ]] || fail "CANDLEPILOT_APP_DIR must be an absolute path"
APP_DIR="$(realpath -m -- "$APP_DIR")"
case "$APP_DIR" in
  /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
    fail "refusing unsafe CANDLEPILOT_APP_DIR: $APP_DIR"
    ;;
esac
[[ "${APP_DIR#/}" == */* ]] || fail "CANDLEPILOT_APP_DIR must be at least two path components deep"

if [[ -z "$REMOVE_APP_USER" ]] && id "$APP_USER" >/dev/null 2>&1 && [[ "$DRY_RUN" == false ]]; then
  read -r -p "Remove Linux user '$APP_USER' and its home directory (including Codex login state)? [y/N]: " answer </dev/tty
  case "$answer" in
    y|Y|yes|YES) REMOVE_APP_USER=true ;;
    *) REMOVE_APP_USER=false ;;
  esac
fi
REMOVE_APP_USER="${REMOVE_APP_USER:-false}"
[[ "$REMOVE_APP_USER" == true || "$REMOVE_APP_USER" == false ]] \
  || fail "CANDLEPILOT_REMOVE_APP_USER must be true or false"

cat <<EOF
CandlePilot uninstall targets:
  systemd services: /etc/systemd/system/candlepilot.service
                    /etc/systemd/system/candlepilot.service.d/logging.conf
                    /etc/systemd/system/candlepilot-update.service
                    /etc/systemd/system/candlepilot-update.path
  update helper:    /usr/local/sbin/candlepilot-web-update
                    /usr/local/libexec/candlepilot-web-update-worker
                    /usr/local/libexec/candlepilot-install-vps
  update request:   /etc/tmpfiles.d/candlepilot-update.conf
                    /run/candlepilot-update
  Nginx site:      /etc/nginx/sites-available/candlepilot
  TLS/config:      /etc/candlepilot
  update state/log: /var/lib/candlepilot, /var/log/candlepilot-update.log
  application:     $APP_DIR
  Linux user:      $APP_USER ($([[ "$REMOVE_APP_USER" == true ]] && echo remove || echo preserve))

Shared packages and firewall rules will be preserved.
EOF

if [[ "$DRY_RUN" == true ]]; then
  echo "Dry run complete; no changes were made."
  exit 0
fi

if [[ "$CONFIRMATION" != "REMOVE" ]]; then
  read -r -p "Type REMOVE to permanently delete these CandlePilot files: " CONFIRMATION </dev/tty
fi
[[ "$CONFIRMATION" == "REMOVE" ]] || fail "confirmation did not match; nothing was removed"

for unit in candlepilot-update.path candlepilot-update.service candlepilot.service; do
  if systemctl list-unit-files "$unit" --no-legend 2>/dev/null | grep -q candlepilot; then
    systemctl disable --now "$unit"
  fi
done
rm -f -- \
  /etc/systemd/system/candlepilot.service \
  /etc/systemd/system/candlepilot-update.service \
  /etc/systemd/system/candlepilot-update.path \
  /etc/tmpfiles.d/candlepilot-update.conf
rm -rf -- /etc/systemd/system/candlepilot.service.d
systemctl daemon-reload
systemctl reset-failed candlepilot.service candlepilot-update.service 2>/dev/null || true

rm -f -- /etc/nginx/sites-enabled/candlepilot /etc/nginx/sites-available/candlepilot
if command -v nginx >/dev/null 2>&1; then
  nginx -t
  if systemctl is-active --quiet nginx; then
    systemctl reload nginx
  fi
fi

rm -f -- \
  /etc/sudoers.d/candlepilot-web-update \
  /usr/local/sbin/candlepilot-web-update \
  /usr/local/libexec/candlepilot-web-update-worker \
  /usr/local/libexec/candlepilot-install-vps \
  /var/log/candlepilot-update.log
rm -rf -- /etc/candlepilot /var/lib/candlepilot
rm -rf -- /run/candlepilot-update
if [[ -e "$APP_DIR" || -L "$APP_DIR" ]]; then
  rm -rf -- "$APP_DIR"
fi

if [[ "$REMOVE_APP_USER" == true ]] && id "$APP_USER" >/dev/null 2>&1; then
  userdel --remove "$APP_USER"
fi

echo "CandlePilot has been uninstalled. Shared packages and firewall rules were not changed."
