#!/usr/bin/env bash
set -Eeuo pipefail

# Root-owned entry point used by the authenticated web API. The unprivileged
# application can only queue one of the fixed actions validated again by the
# root worker; it never invokes a privilege-escalation tool.

REQUEST_FILE="/run/candlepilot-update/request"
ACTION_FILE="/run/candlepilot-update/action"
action=""
case "$#:${1:-}" in
  0:) action="update" ;;
  1:--refresh-backups) action="refresh-backups" ;;
  1:--delete-stale-backups) action="delete-stale-backups" ;;
  1:--clear-logs) action="clear-logs" ;;
  2:--delete-backup)
    [[ "$2" =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{7,64}$ ]] || {
      echo "invalid CandlePilot backup id" >&2
      exit 64
    }
    action=$'delete-backup\t'"$2"
    ;;
  *) echo "unsupported CandlePilot maintenance action" >&2; exit 64 ;;
esac

[[ -d "${REQUEST_FILE%/*}" ]] || {
  echo "the CandlePilot update request directory is unavailable" >&2
  exit 69
}

if ! (set -o noclobber; printf '%s\n' "$action" >"$ACTION_FILE") 2>/dev/null; then
  echo "a CandlePilot maintenance action is already queued" >&2
  exit 75
fi
if ! (set -o noclobber; : >"$REQUEST_FILE") 2>/dev/null; then
  rm -f -- "$ACTION_FILE"
  echo "a CandlePilot maintenance action is already queued" >&2
  exit 75
fi
echo "CandlePilot maintenance action queued"
