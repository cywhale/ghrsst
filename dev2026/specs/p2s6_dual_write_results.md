# P2-S6 — dual-write ingest + append-at-scale — result (with a design-relevant finding)

> Daily store = source of truth; the time-cube is a derived append-only mirror kept in lock-step.
> Tools: `ingest/dual_write.py`, `bench/bench_append_scale.py`, `tests/test_phase2_s6.py` (8/8),
> `/healthz` cube fields, `loadtest.py` route tally. NON-BINDING (synthetic/regional).

## Dual-write / recovery / coverage (all tested, 8/8)
- **`sync_day(daily, cube, day)`** — read the day from the daily store, **upsert** into the cube
  (idempotent: append if new, overwrite-in-place if present). Daily stays authoritative.
- **Idempotency**: `build_timecube.append_day` **rejects a duplicate day** (raises with a recovery
  hint). `upsert_day`/`sync_day` are the safe re-runnable path.
- **Recovery**: **`sync_missing(daily, cube)`** appends every daily day after the cube's latest that
  the cube lacks (e.g. a cube append that failed after the daily write) — just rerun it.
- **Coverage invariant**: **`check_coverage`** reports `daily_latest == cube_latest`, distinguishing
  `missing_after_cube_latest` (fix via `sync_missing`/append) from `missing_within_cube_span` (needs a
  **rebuild**, out-of-order). (Found & fixed a bug here: post-latest days were mis-flagged as in-span.)
- **Observability**: `/healthz` now returns `cube_loaded`, `cube_latest`, `cube_day_count`,
  `cube_latest_in_sync`, `route_counts`. `loadtest.py` tallies `X-Store-Route` per scenario (the load
  gate can confirm multi-day requests actually route to the cube).
- minor: added `TimeCubeStore.covers_days()`; router uses it instead of `_day_index`.

## Append-at-scale (time_chunk=90, spatial_chunk=8)
| grid | shard | files | build s | append p50 | p95 |
|---|---|---|---|---|---|
| 128² | no | 776 | 25.5 | 290 ms | 306 |
| 128² | **yes** | 20 | 11.6 | **119 ms** | 128 |
| 256² | no | 3080 | 87.9 | 960 ms | 1047 |
| 256² | **yes** | 56 | 37.4 | **405 ms** | 406 |
| 512² | no | 12296 | 334.7 | 3680 ms | 3764 |
| 512² | **yes** | 200 | 152.0 | **1638 ms** | 1653 |

### Findings
1. **Append cost scales with grid AREA** (it rewrites the current time-chunk's spatial chunks/shards
   for the new time-slice): ~4× per 2× edge (128→256→512 ≈ 290→960→3680 ms unsharded).
2. **Sharding ~halves append** (119 vs 290, 405 vs 960, 1638 vs 3680) AND cuts file count 40–60×
   (12296 → 200). Keep sharding ON — it helps both files and append.
3. **⚠ DESIGN-RELEVANT: the read-optimal `s8` chunking is append-infeasible GLOBALLY.** Global MUR
   (17999×36000 ≈ 648M cells) is ~2470× the 512² area; extrapolating 1638 ms × 2470 ⇒ **~hours per
   daily append** at spatial=8, plus ~10M tiny chunks/time-slice. **The sweep's read-optimal `s8` is
   NOT a viable global production chunking.** The production config is a **balance** of read (P2-S3
   sweep) and append (this bench).

## Revised chunking guidance (supersedes the P2-S3 `s8` starting point)
- **`spatial=8` is read-optimal but append-infeasible for a GLOBAL cube.** Options to balance:
  - **(a) larger spatial chunk** (e.g. 32–64): far fewer chunks/time-slice ⇒ cheaper append, while
    still a large point-read win vs daily's 1024² (read_amp ~256–1024× vs daily ~1M×). Quantify on VM24.
  - **(b) regional cubes** — build cubes only for regions where point queries occur (hybrid router
    already falls back to daily for uncovered areas). Bounds append cost to the region.
  - **(c)** revisit `time_chunk` (more frequent fresh chunks reduce read-modify-write shards).
- **Final production chunking is NOT decided here** — it needs the VM24 full-grid (or careful
  per-tile) append + read numbers. `s8` is explicitly retired as the global recommendation.

## Status / next
- Dual-write, recovery, coverage, observability: done + tested. Append-at-scale: measured + the
  global-append constraint surfaced (the point of doing this locally before VM24).
- **Still no promotion** — synthetic/regional. Open for P2-S7/S8: pick a balanced chunking (likely
  larger spatial chunk and/or regional), then run the full local gate + the VM24 binding gate
  (real ≥365 contiguous + cold + real append-at-scale).
