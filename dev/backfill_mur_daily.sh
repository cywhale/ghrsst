#!/usr/bin/env bash
set -euo pipefail

# Backfill helper that reuses cron_mur_daily.sh for a contiguous date range.
if [[ $# -ne 2 ]]; then
  echo "Usage: $0 START_DATE END_DATE" >&2
  echo "Example: $0 2025-10-01 2025-10-27" >&2
  exit 2
fi

START_RAW="$1"
END_RAW="$2"

if ! START_TS=$(date -u -d "$START_RAW" +%s 2>/dev/null); then
  echo "Invalid START_DATE: $START_RAW (expected YYYY-MM-DD)" >&2
  exit 1
fi

if ! END_TS=$(date -u -d "$END_RAW" +%s 2>/dev/null); then
  echo "Invalid END_DATE: $END_RAW (expected YYYY-MM-DD)" >&2
  exit 1
fi

if (( START_TS > END_TS )); then
  echo "START_DATE must be <= END_DATE" >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNNER="${SCRIPT_DIR}/cron_mur_daily.sh"

if [[ ! -x "$RUNNER" ]]; then
  echo "Cannot execute cron script: $RUNNER" >&2
  exit 1
fi

CURRENT="$START_RAW"
while :; do
  echo "=== Backfill: $CURRENT ==="
  "$RUNNER" "$CURRENT"

  if [[ "$CURRENT" == "$END_RAW" ]]; then
    break
  fi
  CURRENT=$(date -u -d "$CURRENT + 1 day" +%F)
done

echo "Backfill complete: $START_RAW → $END_RAW"
