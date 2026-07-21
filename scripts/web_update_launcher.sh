#!/usr/bin/env bash
set -Eeuo pipefail

# Root-owned, sudo-restricted entry point used by the authenticated web API.
# It accepts no arguments and can only start the dedicated update service.

[[ "$#" -eq 0 ]] || { echo "candlepilot web updater accepts no arguments" >&2; exit 64; }

UNIT="candlepilot-update.service"
if systemctl is-active --quiet "$UNIT"; then
  echo "a CandlePilot update is already running" >&2
  exit 75
fi

systemctl start --no-block "$UNIT"
echo "CandlePilot update queued"
