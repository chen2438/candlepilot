#!/usr/bin/env bash
set -Eeuo pipefail

# Root-owned entry point used by the authenticated web API. The unprivileged
# application can only create the fixed request watched by systemd; it never
# invokes a privilege-escalation tool or chooses which root command will run.

[[ "$#" -eq 0 ]] || { echo "candlepilot web updater accepts no arguments" >&2; exit 64; }

REQUEST_FILE="/run/candlepilot-update/request"
[[ -d "${REQUEST_FILE%/*}" ]] || {
  echo "the CandlePilot update request directory is unavailable" >&2
  exit 69
}

if ! (set -o noclobber; : >"$REQUEST_FILE") 2>/dev/null; then
  echo "a CandlePilot update is already queued" >&2
  exit 75
fi
echo "CandlePilot update queued"
