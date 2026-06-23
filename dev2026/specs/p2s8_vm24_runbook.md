# P2-S8 — VM24 binding gate + cube ingest runbook

> **Ownership**: authored by Claude; **executed by Codex/ops on VM24** (SSH, full store, disk,
> cron/ingest). **VM24 is the binding decision point** for the time-cube. Claude does NOT run VM24.
> First candidate (P2-S7, NON-binding/synthetic): **`spatial=8 / time_chunk=90 / shard=128`** —
> confirm or tune here. Single source of truth for design: `phase2_timecube_design.md`. Replace every
> `<PLACEHOLDER>`. No promotion until this gate passes on real ≥365 contiguous + cold data.

---

## 0a. VM24 isolation rules (MUST hold for the whole gate)
- Work in a **separate checkout / git worktree**, e.g. `~/python/ghrsst-dev2026-phase2`. **Do NOT
  modify the production tree** (`~/python/ghrsst`) or its data.
- **Shadow port only.** Do NOT modify NGINX, system services, cron, or any unrelated process. Do NOT
  `kill` anything you did not start. Start the shadow app, run the gate, then stop only that shadow
  process.
- Build the cube to a path on a filesystem with spare capacity (see §1) — **never** under the daily
  store / production data dirs.
- Production daily store, MCP (8765), and the live API (8035) stay running and untouched throughout.

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

## 2. Build the cube — to a STAGING path (HOLDOUT), promoted only at the end of §6
**Never build `--out` directly onto an existing/production path.** `build_timecube` **refuses to
delete an existing `--out`** (`FileExistsError`) unless `--overwrite`. Build to a fresh staging path
on the **same filesystem** as the final `<CUBE>` (so the §6 rename is atomic), **holding out the
latest day** (`--exclude-latest`) for the §6 true-append test.
```bash
cd dev2026; P=.venv/bin/python
STAGE=<CUBE>.building.$(date +%s)         # fresh staging path; SAME filesystem as <CUBE>; never production
# --- full global cube (holdout) — if disk/append/files all within gates ---
$P ingest/build_timecube.py --src "$SRC" --out "$STAGE" \
   --spatial-chunk 8 --time-chunk 90 --shard-spatial 128 --exclude-latest
# --- OR tiled (holdout) — bounds append/disk per tile; build the regions that get point queries ---
$P ingest/build_timecube.py --src "$SRC" --out "$STAGE" \
   --spatial-chunk 8 --time-chunk 90 --shard-spatial 128 --exclude-latest --region i0,i1,j0,j1
find "$STAGE" -type f | wc -l ; du -sh "$STAGE"          # record file count + disk
HOLD=<the latest real day held out>                      # appended in §6 for a TRUE append measure
# IMPORTANT: $STAGE and the final <CUBE> MUST be on the SAME filesystem — mv/rename is atomic only
# within one filesystem. Do NOT rename here. Promotion happens in §6, AFTER the holdout day is
# appended and POST-append coverage passes. The whole gate (§3→§6) runs on $STAGE.
```
> NOTE: the current `TimeCubeStore`/`HybridRouter` load ONE cube. **Tiled (multi-cube) routing is a
> P2-S5 follow-up** (route a point to the tile covering it; daily fallback for uncovered). If tiling is
> chosen here, that router extension must land **before any cutover** (see §7).

## 3. PRE-append coverage (holdout cube is intentionally 1 day behind)
```bash
$P - <<PY
from ingest.dual_write import check_coverage
import json; cov = check_coverage("$SRC", "$STAGE"); print(json.dumps(cov, indent=2, default=str))
# PRE-append REQUIRE (do NOT require cov['ok']==True yet — the holdout day isn't appended):
#   missing_after_cube_latest == [HOLD]   (exactly the held-out day, nothing more)
#   missing_within_cube_span  == []       (no in-span gaps)
#   extra_cube_days           == []       (no orphan cube days)
#   days_sorted and days_unique           (well-formed time axis)
PY
```

## 4. Serve the cube-backed API (shadow; separate port; production untouched)
> Serve the **`$STAGE`** holdout cube — the whole gate (read + append) runs on it; promote to `<CUBE>`
> only at the end of §6.
```bash
GHRSST_ZARR_PATH="$SRC" GHRSST_TIMECUBE_PATH="$STAGE" GHRSST_RSS_CEILING_MB=<X> \
GHRSST_ZARR_WORKERS=<W> PYTHONPATH=. .venv/bin/gunicorn api.app:app \
  -w <GUNICORN_WORKERS> -k uvicorn.workers.UvicornWorker -b 127.0.0.1:<NEW_PORT> --timeout 180
curl -s http://127.0.0.1:<NEW_PORT>/healthz   # cube_loaded, cube_latest, cube_latest_in_sync, route_counts
```

## 5. Binding read gate — cold AND warm (loadtest)
> **Do NOT run a global `drop_caches`** — it evicts the whole machine's page cache and harms every
> other service on VM24. Use **targeted eviction of the cube files only**:
```bash
B=http://127.0.0.1:<NEW_PORT>
# COLD-ish: evict ONLY the staged cube's files (preferred). Requires vmtouch.
vmtouch -e "$STAGE"                       # targeted; safe for other projects
#   - if vmtouch is unavailable: mark the run as cache-state=best_effort (NOT strict cold), or
#     schedule strict-cold testing in a maintenance window (a global drop_caches is allowed ONLY
#     in an approved maintenance window, never during normal operation).
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

## 6. TRUE append (holdout) → POST-append coverage → atomic promote
The cube was built in §2 holding out `HOLD`; §3 (pre-append) and §5 (read gate) ran on `$STAGE`.
Now append `HOLD` into `$STAGE` (a `sync_day` for an in-cube day would be an OVERWRITE that
under-estimates cost — the holdout guarantees a TRUE append), require **post-append** coverage
`ok==true`, THEN promote `$STAGE` → `<CUBE>`.
```bash
# stop the §4 shadow app first if it locks the cube, then:
$P - <<PY
import time, json
from ingest.dual_write import sync_day, sync_missing, check_coverage
from store.time_cube import TimeCubeStore
b = TimeCubeStore("$STAGE").day_count
t = time.perf_counter(); res = sync_day("$SRC", "$STAGE", "$HOLD"); dt = time.perf_counter() - t
a = TimeCubeStore("$STAGE").day_count
print("sync_day:", res, "append_s", round(dt, 1), "day_count", b, "->", a)
assert res == "append" and a == b + 1, "NOT a true append — was HOLD already present? (rebuild holdout)"
# REQUIRE append_s (+ margin) <= INGEST_WINDOW
# idempotency: re-running the SAME day must be 'overwrite' with unchanged count
print("idempotent:", sync_day("$SRC", "$STAGE", "$HOLD"), "count", TimeCubeStore("$STAGE").day_count)
# POST-append coverage MUST now be fully ok
cov = check_coverage("$SRC", "$STAGE"); print(json.dumps(cov, default=str))
assert cov["ok"] is True, "post-append coverage not ok"
PY
# recovery drill (optional, re-runnable): if a day was missed, sync_missing appends it.
# Promote ONLY after the asserts pass (same filesystem -> atomic; refuse if final exists):
test ! -e <CUBE> && mv "$STAGE" <CUBE> && echo "promoted -> <CUBE>"
```
> **Alternative to the holdout**: instead of `--exclude-latest`, build the full cube and wait for the
> NEXT cron day (a day not yet in the cube), then time `sync_day` for it — also a true append. Either
> way the binding requirements are the same: `res=='append'`, `append_s` (+ margin) ≤ `INGEST_WINDOW`,
> POST-append `check_coverage.ok == true`. Recovery: a missed day is re-appended by `sync_missing`
> (re-runnable).

## Ordered gate sequence (summary)
1. §2 build holdout cube to `$STAGE` (`--exclude-latest`, same fs as `<CUBE>`).
2. §3 **pre-append** coverage: `missing_after_cube_latest==[HOLD]`, no in-span gaps, no extra, sorted/unique (cov.ok will be False — expected).
3. §5 read gate on `$STAGE` (cold via `vmtouch -e`, then warm).
4. §6 `sync_day(HOLD)` → assert `append` + count+1 + `append_s ≤ INGEST_WINDOW`.
5. §6 **post-append** coverage: now require `cov.ok == true`.
6. §6 promote `$STAGE` → `<CUBE>` (atomic, same fs).

## 7. Go / No-Go
**GO (proceed to cutover discussion)** iff ALL:
- disk precheck passed; built to `$STAGE` (holdout); **pre-append** coverage = only `[HOLD]` missing,
  no in-span/extra/order problems; **POST-append** `check_coverage.ok == true`; promoted `$STAGE`→`<CUBE>` (atomic, same fs).
- cold 365-day p95 < 4 s; LR C=4/8/16 bounded, zero timeout/OOM; RSS ≤ ceiling.
- **append measured as a TRUE append (holdout; `sync_day` returned `append`)** ≤ `INGEST_WINDOW`
  (with margin); file count ≤ `FILE_MAX`.
- routing confirmed (`route_counts` cube; `X-Store-Route: cube`).
- **If a TILED (multi-cube) cube was chosen: NOT cutover-eligible yet** — the multi-cube routing
  (P2-S5 follow-up) must be implemented and re-gated first. A single global cube can proceed.

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
