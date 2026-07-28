#!/usr/bin/env bash
# Append a daily Zarr group to the recent delta tier. Safe to schedule twice
# for the same UTC day: a finalized delta day is a no-op on the retry.
set -euo pipefail

DAY="${1:-$(date -u -d "yesterday" +%Y-%m-%d)}"
DEV=/home/odbadmin/python/ghrsst-dev2026-phase2
PM2=/home/odbadmin/.npm-global/bin/pm2
PM2_APP=ghrsst
G=/home/odbadmin/Data/ghrsst
LOCK=$G/mur_delta.ingest.lock
LOGDIR=$G/logs/delta_append
DELTA=$G/mur_timecube_s8_t90_sh128.delta.zarr
PY=$DEV/dev2026/.venv/bin/python
mkdir -p "$LOGDIR"
LOG="$LOGDIR/append_${DAY}.log"

delta_day_finalized() {
  "$PY" - "$DELTA" "$DAY" <<'PY'
import sys
import zarr

delta_path, day = sys.argv[1:]
g = zarr.open_group(delta_path, mode="r")
days = list(g.attrs.get("days", []))
if day not in days:
    raise SystemExit(1)

i = days.index(day)
vars_ = list(g.attrs.get("vars", []))
valid = dict(g.attrs.get("var_valid", {}))
for name in vars_:
    arr = g[name]
    if arr.ndim != 3 or arr.shape[0] <= i or len(valid.get(name, [])) <= i:
        raise SystemExit(1)

print(f"finalized day={day} index={i} vars={','.join(vars_)}")
PY
}

wait_healthz() {
  for _ in $(seq 1 45); do
    if curl -fsS --max-time 5 http://127.0.0.1:8035/healthz >/dev/null; then
      return 0
    fi
    sleep 2
  done
  return 1
}

{
  echo "START $(date -Is) day=$DAY"
  exec 9>"$LOCK"
  if ! flock -n 9; then
    echo "LOCK_BUSY $(date -Is) lock=$LOCK"
    exit 75
  fi

  # attrs.days is finalized last by append_to_delta, so membership means the
  # complete slab is visible. Missing variables are represented by var_valid.
  if finalized=$(delta_day_finalized 2>&1); then
    echo "SKIP_ALREADY_VALID $(date -Is) $finalized"
    exit 0
  fi

  cd "$DEV"
  "$PY" dev2026/ingest/append_delta_day.py \
    --daily "$G/mur.zarr" \
    --delta "$DELTA" \
    --day "$DAY" \
    --workers 4 \
    --read-block 1024
  echo "APPEND_DONE $(date -Is) day=$DAY"

  if [ -x "$PM2" ]; then
    echo "RELOAD_START $(date -Is) app=$PM2_APP"
    "$PM2" restart "$PM2_APP"
    if wait_healthz; then
      echo "RELOAD_DONE $(date -Is) app=$PM2_APP healthz=ok"
    else
      echo "RELOAD_FAILED $(date -Is) app=$PM2_APP healthz=unavailable"
      exit 3
    fi
  else
    echo "RELOAD_SKIP $(date -Is) reason=pm2_not_executable path=$PM2"
    exit 2
  fi
  echo "DONE $(date -Is) day=$DAY"
} >> "$LOG" 2>&1
