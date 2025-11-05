#!/usr/bin/env bash
set -euo pipefail

# --------- EDIT THESE PATHS ONCE ----------
PY_BIN="$HOME/.pyenv/versions/py314/bin/python"       # Python 3.14 venv (Zarr v3)
PDS_BIN="$HOME/.pyenv/shims/podaac-data-downloader"   # podaac-data-downloader CLI
WRITER="$HOME/python/ghrsst/dev/mur2zarr_v3.py"       # your writer (v3, no consolidation)
TESTER="$HOME/python/ghrsst/tests/test_compare_mur_point.py"  # verifier

SRC_DIR="$HOME/Data/ghrsst/src"                       # where .nc lands (kept for a few days)
ZARR_ROOT="$HOME/Data/ghrsst/mur.zarr"                # Zarr v3 root (groups=/YYYY/MM/DD)
INDEX_JSON="$HOME/Data/ghrsst/latest.json"                # only earliest/latest stored here
RETENTION_DAYS=30                                     # auto-remove .nc older than this
LOG_DIR="$HOME/tmp"
# -------------------------------------------

mkdir -p "$SRC_DIR" "$LOG_DIR"
LOG="$LOG_DIR/cron_mur_daily.log"

# avoid overlaps
LOCK_F="$LOG_DIR/.cron_mur_daily.lock"
exec 9>"$LOCK_F"
if ! flock -n 9; then
  echo "$(date -Is) [SKIP] another run in progress" | tee -a "$LOG"
  exit 0
fi

# target date (UTC day for the MUR granule)
if [[ $# -lt 1 ]]; then
  echo "Usage: $0 YYYY-MM-DD" | tee -a "$LOG"; exit 2
fi
DATE_UTC="$1"                # e.g., 2025-11-01
Y=$(date -u -d "$DATE_UTC" +%Y)
M=$(date -u -d "$DATE_UTC" +%m)
D=$(date -u -d "$DATE_UTC" +%d)
COMPACT="${Y}${M}${D}"

FNAME="${COMPACT}090000-JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.nc"
NC_PATH="${SRC_DIR}/${FNAME}"
GROUP_PATH="${ZARR_ROOT}/${Y}/${M}/${D}"

echo "$(date -Is) [INFO] target=${DATE_UTC}  nc=${NC_PATH}" | tee -a "$LOG"

# 0) If Zarr group already exists, nothing to do.
if [[ -d "$GROUP_PATH" ]]; then
  echo "$(date -Is) [OK] Zarr group exists: ${GROUP_PATH} -> EXIT" | tee -a "$LOG"
  exit 0
fi

# 1) Download (only if needed). PO.DAAC sometimes returns extra days; we filter by exact filename.
if [[ -s "$NC_PATH" ]]; then
  echo "$(date -Is) [OK] NetCDF exists (skip download): ${NC_PATH}" | tee -a "$LOG"
else
  echo "$(date -Is) [INFO] downloading via PO.DAAC..." | tee -a "$LOG"

  # ultra-tight window to avoid adjacent-day files (09:00:00Z ± 1s)
  START="${DATE_UTC}T09:00:00Z"
  END="${DATE_UTC}T09:00:01Z"

  "$PDS_BIN" -c MUR-JPL-L4-GLOB-v4.1 -d "$SRC_DIR" \
    --start-date "$START" --end-date "$END" -e .nc | tee -a "$LOG" || true

  # If downloader still left multiple files (or different minutes), keep only the exact FNAME.
  if compgen -G "${SRC_DIR}/${COMPACT}*JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.nc" > /dev/null; then
    # move exact file into place if present
    if [[ -f "${SRC_DIR}/${FNAME}" ]]; then
      : # expected case
    else
      # try to rename the most plausible one (pick strictly the '090000' if present)
      cand=$(ls -1 ${SRC_DIR}/${COMPACT}*JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.nc | grep "${COMPACT}090000" || true)
      if [[ -n "$cand" ]]; then
        mv -f "$cand" "$NC_PATH"
      fi
    fi
    # remove any other same-day granules (rare)
    for f in ${SRC_DIR}/${COMPACT}*JPL-L4_GHRSST-SSTfnd-MUR-GLOB-v02.0-fv04.1.nc; do
      [[ "$f" == "$NC_PATH" ]] || rm -f "$f"
    done
  fi

  if [[ ! -s "$NC_PATH" ]]; then
    echo "$(date -Is) [ERR] expected file not found after download: ${NC_PATH}" | tee -a "$LOG"
    exit 1
  fi
fi

# 2) Convert to Zarr v3 subgroup (/YYYY/MM/DD)
echo "$(date -Is) [INFO] converting -> Zarr /${Y}/${M}/${D}" | tee -a "$LOG"
"$PY_BIN" "$WRITER" "$NC_PATH" "$ZARR_ROOT" 2>&1 | tee -a "$LOG"

if [[ ! -d "$GROUP_PATH" ]]; then
  echo "$(date -Is) [ERR] Zarr group missing after write: ${GROUP_PATH}" | tee -a "$LOG"
  exit 1
fi
echo "$(date -Is) [OK] Zarr write done: ${GROUP_PATH}" | tee -a "$LOG"

# 3) Verify (only final PASS/FAIL line in main log; full transcript per day)
TEST_OUT="$LOG_DIR/test_${COMPACT}.log"
echo "$(date -Is) [INFO] verifying values vs NetCDF ..." | tee -a "$LOG"
set +e
"$PY_BIN" "$TESTER" --nc "$NC_PATH" --zarr "$ZARR_ROOT" --date "${Y}-${M}-${D}" --n 50 >"$TEST_OUT" 2>&1
TEST_RC=$?
set -e
if [[ $TEST_RC -eq 0 ]] && grep -q "PASS: All Zarr vs NetCDF values match within acceptable tolerances" "$TEST_OUT"; then
  echo "$(date -Is) [VERIFY] PASS: All Zarr vs NetCDF values match within acceptable tolerances" | tee -a "$LOG"
else
  FAIL_LINE=$(grep -m1 -E "FAIL|Error|ERROR|Traceback" "$TEST_OUT" || true)
  [[ -z "$FAIL_LINE" ]] && FAIL_LINE="FAIL (see $TEST_OUT)"
  echo "$(date -Is) [VERIFY] $FAIL_LINE" | tee -a "$LOG"
fi

# 4) Update minimal index (only earliest/latest)
export INDEX_JSON DATE_UTC

"$PY_BIN" - <<'PYCODE'
import json, os
idx_path = os.environ["INDEX_JSON"]
day      = os.environ["DATE_UTC"]

# keep only earliest/latest (no huge dates list)
obj = {"earliest": day, "latest": day}
if os.path.isfile(idx_path):
    try:
        with open(idx_path,"r",encoding="utf-8") as f:
            cur = json.load(f)
            e = cur.get("earliest", day)
            l = cur.get("latest", day)
            if e > day: e = day
            if l < day: l = day
            obj = {"earliest": e, "latest": l}
    except Exception:
        pass

tmp = idx_path + ".tmp"
with open(tmp,"w",encoding="utf-8") as f: json.dump(obj, f, ensure_ascii=False)
os.replace(tmp, idx_path)
print(f"index: earliest={obj['earliest']} latest={obj['latest']}")
PYCODE

# 5) Housekeeping: remove old NetCDFs
echo "$(date -Is) [INFO] purge .nc older than ${RETENTION_DAYS} days" | tee -a "$LOG"
find "$SRC_DIR" -type f -name '*.nc' -mtime +"$RETENTION_DAYS" -print -delete \
  | sed 's/^/  purged: /' | tee -a "$LOG" || true

echo "$(date -Is) [DONE] ${DATE_UTC}" | tee -a "$LOG"
