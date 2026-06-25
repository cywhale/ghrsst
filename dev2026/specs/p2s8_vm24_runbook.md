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
**Use the BULK builder `build_timecube_bulk` — NOT the per-day `build_timecube`.** The per-day writer
writes one day at a time, which read-modify-writes the whole `time_chunk`-spanning shard every day
(~90× write amplification → multi-day global builds). The bulk builder writes one shard-block
(`time_chunk × shard × shard`) at a time = each shard written once (measured ~**78× faster** at 256²;
larger at global scale). It is **resumable** (checkpoint per var/time-block/tile) and bounded-parallel.

**Never build `--out` onto an existing/production path** — `build_timecube_bulk` refuses (`FileExistsError`)
unless `--overwrite`, or resumes an existing partial build (its `_build_checkpoint.json`). Build to a
fresh staging path on the **same filesystem** as the final `<CUBE>` (so the §6 rename is atomic),
**holding out the latest day** (`--exclude-latest`) for the §6 true-append test.
```bash
cd dev2026; P=.venv/bin/python
STAGE=<CUBE>.building.$(date +%s)         # fresh staging path; SAME filesystem as <CUBE>; never production
# --- full global cube (holdout), bounded parallel + resumable ---
$P ingest/build_timecube_bulk.py --src "$SRC" --out "$STAGE" \
   --spatial-chunk 8 --time-chunk 90 --shard-spatial 128 --workers 4 --exclude-latest
#   incremental availability: add --latest-days 90 to build the most recent 90 days FIRST
#   (the hybrid router falls back to daily for older ranges), then rerun with a larger N to extend.
#   resume after an interruption: rerun the SAME command — completed (var,time-block,tile) units skip.
#   memory ≈ workers × time_chunk × read_block² × 4 bytes (per var); lower --read-block/--workers if RAM-bound.
# --- OR tiled (holdout): build the regions that get point queries ---
$P ingest/build_timecube_bulk.py --src "$SRC" --out "$STAGE" \
   --spatial-chunk 8 --time-chunk 90 --shard-spatial 128 --workers 4 --exclude-latest --region i0,i1,j0,j1
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
GHRSST_ZARR_PATH="$SRC" GHRSST_TIMECUBE_PATH="$STAGE" GHRSST_DELTACUBE_PATH=<DELTA or unset> \
GHRSST_RSS_CEILING_MB=<X> GHRSST_ZARR_WORKERS=<W> PYTHONPATH=. .venv/bin/gunicorn api.app:app \
  -w <GUNICORN_WORKERS> -k uvicorn.workers.UvicornWorker -b 127.0.0.1:<NEW_PORT> --timeout 180
curl -s http://127.0.0.1:<NEW_PORT>/healthz   # cube_kind (timecube|tiered), cube_latest, delta_*, route_counts
# Production append uses a base+delta tier: set GHRSST_DELTACUBE_PATH to a time_chunk=1 delta cube
# (cheap daily appends; see §6). For the read gate alone, the base (STAGE) cube is enough.
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

## 6. Append + promote — Path A (PRODUCTION base+delta) or Path B (interim single-tier)
> **Production daily append is the DELTA cube (`time_chunk=1`), NOT `sync_day` into the base.**
> Appending into the `time_chunk=90` base read-modify-writes the whole 90-step shard (>80 min global,
> the deploy problem). The delta append writes one fresh shard = cheap (`append_strategy_results.md`).

### Path A — base + delta (RECOMMENDED; cheap daily append)
The §2 base (`--exclude-latest`) is the COMPLETE base for older days, so promote it directly; the
held-out latest day(s) live in the DELTA. The tier (base+delta) covers everything.
```bash
DELTA=<CUBE>.delta            # time_chunk=1 delta cube, APPEND-optimized layout (s256/shard256)
# promote the base (atomic; same fs; refuse if final exists):
test ! -e <CUBE> && mv "$STAGE" <CUBE> && echo "promoted base -> <CUBE>"
# DAILY APPEND = the production cron CLI (TILED/streaming; never materializes a full global array).
# Delta chunking is DECOUPLED from the base read-cube (base=s8 for reads; delta=s256 for cheap append
# -- s8 delta = ~10M tiny chunks/var = the >2h/day; s256 was 215x faster, no read penalty). This is
# the EXACT path cron runs -- do NOT hand-build the delta. Run once per held-out day:
for HOLD in <D D+1 ...>; do
  PYTHONPATH=. $P ingest/append_delta_day.py --daily "$SRC" --delta "$DELTA" --day "$HOLD" \
    --delta-spatial-chunk 256 --delta-shard-spatial 256 --workers 4   # defaults already s256/s256
done
# prints {op,append_s,tile_count,max_block_cells,grid_cells,rss_mb}
# REQUIRE: append_s (+ margin) <= INGEST_WINDOW (BINDING gate, now MINUTES not hours); AND peak RSS
#   bounded -- max_block_cells << grid_cells, RSS must NOT scale with the grid (>7.8 GB pattern gone).
$P - <<PY
from store.time_cube import TimeCubeStore
from store.tiered_cube import TieredCube
tc = TieredCube(TimeCubeStore("<CUBE>"), TimeCubeStore("$DELTA"))
assert tc.latest == "<daily latest>", "tier latest mismatch"
print("tier latest:", tc.latest, "days:", tc.day_count)      # tier must cover all daily days
PY
# serve base+delta: GHRSST_TIMECUBE_PATH=<CUBE> GHRSST_DELTACUBE_PATH=$DELTA (§4) -> /healthz cube_kind==tiered
# PERIODIC COMPACTION (cron; e.g. weekly or when the delta fills a 90-day block): fold delta -> base
$P - <<PY
from ingest.dual_write import compact
out = compact("$SRC", "<CUBE>", "$DELTA", end_day="<90-day-block-boundary day>",
              spatial_chunk=8, time_chunk=90, shard_spatial=128, workers=4)
print("compact ->", out)     # new_base / new_delta staging paths; swap both atomically, keep daily as truth
PY
```

### Path B — single-tier (INTERIM only; slow RMW daily append — migrate to Path A)
True-append into the base via the holdout; daily append here is the slow RMW path (`bulk_append_day`
at best). Use only until base+delta is rolled out.
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
1. §2 build holdout base to `$STAGE` with the **bulk builder** (`--exclude-latest`, same fs as `<CUBE>`).
2. §3 **pre-append** coverage on `$STAGE`: `missing_after_cube_latest==[HOLD]`, no in-span gaps, no extra, sorted/unique (cov.ok False — expected; the holdout day(s) live in the delta).
3. §5 read gate on `$STAGE` (cold via `vmtouch -e $STAGE`, then warm).
4. §6 **Path A (recommended)**: promote `$STAGE`→`<CUBE>`; `append_to_delta(HOLD)` → **binding append gate** `delta_append_s ≤ INGEST_WINDOW`; `TieredCube(base,delta)` covers all daily days; serve base+delta; periodic `compact`. **Path B (interim)**: `sync_day(HOLD)` into base + post-append coverage + promote (slow RMW append — migrate to A).
5. §7 go/no-go; §8 cutover (config-only rollback: unset cube/delta env).

## 7. Go / No-Go
**GO (proceed to cutover discussion)** iff ALL:
- disk precheck passed; built to `$STAGE` (holdout); **pre-append** coverage = only `[HOLD]` missing,
  no in-span/extra/order problems; **POST-append** `check_coverage.ok == true`; promoted `$STAGE`→`<CUBE>` (atomic, same fs).
- cold 365-day p95 < 4 s; LR C=4/8/16 bounded, zero timeout/OOM; RSS ≤ ceiling.
- **append gate**: Path A **`delta_append_s` ≤ `INGEST_WINDOW`** (with margin) and the `TieredCube`
  covers all daily days; compaction completes within budget. (Path B interim: `sync_day` returned
  `append` ≤ window.) file count ≤ `FILE_MAX` (base; delta is small).
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
- **Rollback**: unset `GHRSST_TIMECUBE_PATH` (and `GHRSST_DELTACUBE_PATH`) → router falls back to the
  daily store for everything (= P1 behaviour); restart; or NGINX upstream rollback per the P1 runbook.
  The daily store is untouched throughout, so rollback is config-only and instant.
- Keep the ingest cron (daily **`ingest/append_delta_day.py`** = tiled `append_to_delta`, memory-bounded
  + periodic **`compact`** + `check_coverage` alert) running and monitored before and after cutover.

## 9. Decision to record
Confirm the production chunking: **`s8/t90/shard=128`** if all gates pass, else the tuned variant
(shard=256 / s16-s32 / tiled) that passes. Write the chosen config + the binding numbers back here.
