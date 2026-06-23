# P2-S8 — VM24 binding gate + cube ingest runbook

> **Ownership**: authored by Claude; **executed by Codex/ops on VM24** (SSH, full store, disk,
> cron/ingest). **VM24 is the binding decision point** for the time-cube. Claude does NOT run VM24.
> First candidate (P2-S7, NON-binding/synthetic): **`spatial=8 / time_chunk=90 / shard=128`** —
> confirm or tune here. Single source of truth for design: `phase2_timecube_design.md`. Replace every
> `<PLACEHOLDER>`. No promotion until this gate passes on real ≥365 contiguous + cold data.

---

## 0. Operational gates (fill in the accepted values BEFORE running)
| gate | symbol | accepted value (ops sets) | default to assume |
|---|---|---|---|
| daily ingest window | `INGEST_WINDOW` | `<e.g. 3h>` | 3 h |
| cube file-count tolerance | `FILE_MAX` | `<filesystem/ops limit>` | ≤ 1.0 M files |
| per-worker RSS ceiling | `GHRSST_RSS_CEILING_MB` | `RAM ÷ -w − headroom` | (P1-S5 value) |
| read p95 (365-day) | — | < 4 s cold (target p50 < 2.5 s) | — |
| LR concurrency | — | C=4/8/16 bounded, **zero timeout/OOM** | — |
| append per day | — | **fits `INGEST_WINDOW`** with margin | — |

Estimates for `s8/t90/shard=128` (from P2-S7 extrapolation; VERIFY on VM24, do not trust blindly):
- **global cube disk** ≈ same order as the daily store's size for the 3 time-served vars over the
  span (the cube duplicates `sst`, `sst_anomaly`, `sea_ice`). For 365 days expect **hundreds of GB**.
- **global file count (shard=128)** ≈ `(17999/128)×(36000/128)×ceil(days/90)×3` ≈ **~0.6 M files** for
  365 days. If > `FILE_MAX`: enlarge shard (256 → ~0.23 M; costs more append/RSS — P2-S7 sweep) or tile.
- **global append/day** ≈ tens of minutes (P2-S7 est ~74 min). If > `INGEST_WINDOW`: enlarge inner
  chunk (s16/s32) or tile.

---

## 1. Disk-space precheck (MUST pass before build)
```bash
SRC=<abs path to full mur.zarr>
# estimate cube size = du of the 3 time-served vars across the span (rough upper bound)
EST_GB=$(du -sBG "$SRC" | awk '{print $1}' | tr -dc 0-9)   # daily store size (upper bound for cube)
FREE_GB=$(df -BG --output=avail <cube target fs> | tail -1 | tr -dc 0-9)
echo "estimated cube <= ${EST_GB} GB ; free ${FREE_GB} GB"
# REQUIRE free >= 1.2 * EST_GB ; abort if not (do NOT fill the disk feeding production)
test "$FREE_GB" -ge $(( EST_GB * 12 / 10 )) || { echo "INSUFFICIENT DISK — abort"; exit 1; }
```

## 2. Build the cube (full OR tiled)
```bash
cd dev2026; P=.venv/bin/python
# --- full global cube (if disk/append/files all within gates) ---
$P ingest/build_timecube.py --src "$SRC" --out <CUBE> \
   --spatial-chunk 8 --time-chunk 90 --shard-spatial 128
# --- OR tiled (bounds append + disk per tile; build the regions that get point queries) ---
$P ingest/build_timecube.py --src "$SRC" --out <CUBE_TILE_k> \
   --spatial-chunk 8 --time-chunk 90 --shard-spatial 128 --region i0,i1,j0,j1
# record file count + disk:
find <CUBE> -type f | wc -l ; du -sh <CUBE>
```
> NOTE: the current `TimeCubeStore`/`HybridRouter` load ONE cube. **Tiled (multi-cube) routing is a
> P2-S5 follow-up** (route a point to the tile covering it; daily fallback for uncovered). If tiling is
> chosen here, that router extension must land before cutover.

## 3. Coverage / structural check (after build, and after each append)
```bash
$P - <<PY
from ingest.dual_write import check_coverage
import json; print(json.dumps(check_coverage("$SRC", "<CUBE>"), indent=2, default=str))
PY
# REQUIRE: ok == true (latest_in_sync, no missing_within_cube_span, no extra_cube_days,
#          days_sorted, days_unique)
```

## 4. Serve the cube-backed API (shadow; separate port; production untouched)
```bash
GHRSST_ZARR_PATH="$SRC" GHRSST_TIMECUBE_PATH=<CUBE> GHRSST_RSS_CEILING_MB=<X> \
GHRSST_ZARR_WORKERS=<W> PYTHONPATH=. .venv/bin/gunicorn api.app:app \
  -w <GUNICORN_WORKERS> -k uvicorn.workers.UvicornWorker -b 127.0.0.1:<NEW_PORT> --timeout 180
curl -s http://127.0.0.1:<NEW_PORT>/healthz   # cube_loaded, cube_latest, cube_latest_in_sync, route_counts
```

## 5. Binding read gate — cold AND warm (loadtest)
```bash
B=http://127.0.0.1:<NEW_PORT>
# COLD: drop OS page cache first (Linux), then run immediately
sync; echo 3 | sudo tee /proc/sys/vm/drop_caches >/dev/null   # or vmtouch -e <CUBE>
$P bench/loadtest.py --base $B --run-kind vm24 --scenario LR --duration 120 --dense-days 365 \
   --cache-state cold --out vm24_cube_lr_cold.json
$P bench/loadtest.py --base $B --run-kind vm24 --scenario SB --sb-range-max 60 --concurrency 32 \
   --duration 120 --dense-days 365 --cache-state cold --out vm24_cube_sb_cold.json
# WARM: rerun without dropping caches
$P bench/loadtest.py --base $B --run-kind vm24 --scenario LR --duration 120 --dense-days 365 \
   --cache-state warm --out vm24_cube_lr_warm.json
```
- **Confirm routing**: each artifact's `route_counts` shows multi-day → **cube** (and `X-Store-Route:
  cube`); `/healthz` RSS sampled during the run is the **authoritative RSS** (sum by worker pid via
  `ps` for total, as P1-S5).
- **Pass**: 365-day p95 < 4 s **cold**; LR C=4/8/16 bounded, zero timeout/OOM, RSS ≤
  `GHRSST_RSS_CEILING_MB`; SB stable (G2′-style).

## 6. Append/upsert measurement (binding — fits the ingest window?)
```bash
# measure a REAL daily append from the daily store into the cube (read-from-daily + upsert)
$P - <<PY
import time; from ingest.dual_write import sync_day, check_coverage
t=time.perf_counter(); sync_day("$SRC", "<CUBE>", "<NEW_DAY_ISO>")
print("append_s", round(time.perf_counter()-t,1))
import json; print(json.dumps(check_coverage("$SRC","<CUBE>"), default=str))
PY
# REQUIRE: append_s (+ margin) <= INGEST_WINDOW ; coverage ok == true
# idempotency check: re-run sync_day for the SAME day -> 'overwrite', day_count unchanged
# recovery check: simulate a missed day, then ingest.dual_write.sync_missing -> appends it
```

## 7. Go / No-Go
**GO (proceed to cutover discussion)** iff ALL:
- disk precheck passed; cube built; `check_coverage.ok == true`.
- cold 365-day p95 < 4 s; LR C=4/8/16 bounded, zero timeout/OOM; RSS ≤ ceiling.
- append/day ≤ `INGEST_WINDOW` (with margin); file count ≤ `FILE_MAX`.
- routing confirmed (`route_counts` cube; `X-Store-Route: cube`).

**NO-GO / do not cut over (and the fix)**:
- read p95 fails cold → larger time/spatial issue; re-tune (should not happen given P2-S7, but verify).
- append > window → **enlarge inner chunk (s16/s32)** or **tile**; re-run gate.
- file count > `FILE_MAX` → **enlarge shard (256)** (re-check append/RSS) or **tile**; re-run.
- RSS > ceiling → reduce `GHRSST_ZARR_WORKERS` / shard size; re-run.
- coverage not ok / dual-write can't keep up in the cron window → **do not cut over**; stay on the
  daily store (hybrid router already falls back), fix ingest first.

## 8. Cutover (only AFTER a binding GO)
- The cube is **additive**: enabling it is just setting `GHRSST_TIMECUBE_PATH` on the new app; the
  hybrid router sends only multi-day point/range to the cube, everything else to the daily store. No
  NGINX/data change beyond the P1 cutover path (`p1s5_s6_runbook.md`) if the new app isn't already live.
- **Rollback**: unset `GHRSST_TIMECUBE_PATH` (router → daily for everything = P1 behaviour) and
  restart; or NGINX upstream rollback per the P1 runbook. The daily store is untouched throughout, so
  rollback is config-only and instant.
- Keep the dual-write cron (daily `sync_day` + a `sync_missing` safety pass + `check_coverage` alert)
  running and monitored before and after cutover.

## 9. Decision to record
Confirm the production chunking: **`s8/t90/shard=128`** if all gates pass, else the tuned variant
(shard=256 / s16-s32 / tiled) that passes. Write the chosen config + the binding numbers back here.
