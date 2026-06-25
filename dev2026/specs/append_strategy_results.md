# Append strategy — base+delta (production) + bulk_append_day (interim)

> Deployment found `sync_day`/`append_day` >80 min/day. Root cause (Codex): same RMW problem as the
> build — appending one day into a `time_chunk=90` shard read-modify-writes the whole 90-step shard,
> and it loads a full global day into memory. Read chunking (`s8/t90/shard=128`) is unchanged.
> Tools: `store/tiered_cube.py`, `ingest/dual_write.py` (append_to_delta / compact),
> `ingest/build_timecube_bulk.bulk_append_day`; tests `tests/test_phase2_append.py` (7/7). Full 111/111.

## Why the old append is slow (at scale)
The base inner chunk is `(time_chunk=90, 8, 8)`. Appending one day at time index `t` writes into the
inner chunk that spans all 90 time steps → Zarr **reads the existing chunk, inserts the slice,
re-writes it** (RMW). Globally there are ~30.4 M inner chunks (×3 vars); a mid-block append RMWs all
of them → **~700 GB read + ~700 GB write of decompress/recompress per day ≈ the observed >80 min.**

## Production design: base + delta + compaction (preferred)
- **BASE** cube: `time_chunk=90` (fast reads; built by the bulk builder).
- **DELTA** cube: **`time_chunk=1`** — each appended day is **its own fresh shard**, so the daily
  append writes one day's data with **NO read-modify-write** (`ingest.dual_write.append_to_delta`).
- **`TieredCube`** (`store/tiered_cube.py`) reads BASE + DELTA (delta for its recent days, base for the
  rest), drop-in for the HybridRouter; wired via `GHRSST_DELTACUBE_PATH`. Each cube keeps its own
  per-(day,var) validity → exact P1 omit/null semantics.
- **Compaction** (`ingest.dual_write.compact`): periodically (e.g. when the delta fills a 90-day block,
  or weekly) bulk-REBUILD the base through `end_day` (fast bulk builder) and reset the delta to days
  after `end_day`; daily store is the source of truth, so this is a clean re-derive (no in-place base
  mutation while serving). Returns staging paths; the caller swaps atomically.

### Cost — small grid is overhead-dominated; the win is at SCALE
| append | 256² | 512² (mid-block) | GLOBAL (extrapolated) |
|---|---|---|---|
| OLD (base t90, RMW) | 344 ms | 1320 ms | **~80 min** (≈ user's >80 min) |
| DELTA (t=1, no RMW) | 301 ms | 1143 ms | **~minutes** |
| ratio | 1.1× | 1.2× | **~10–100×** |
The RMW cost lives in the **read volume** of the partially-filled 90-day shards (~700 GB/day global).
At 512² that read is small in absolute terms so per-chunk codec/file overhead dominates and the
delta win looks modest (1.2×). At **global scale the RMW read is what caused >80 min**, and the delta
(`time_chunk=1`) eliminates it and writes `1/time_chunk` the data → minutes. (Binding number = VM24.)

## Interim band-aid: `bulk_append_day`
If a single-tier cube must be appended before base+delta is rolled out: `bulk_append_day` writes the
day **tile-by-tile** with bounded workers, **no full-global array in memory** (one tile per worker),
and **checkpoint/resume** per `(var,tile)`. It still RMWs the time-spanning shard (inherent to a
time_chunk>1 cube) — it only removes the memory blow-up and parallelizes/resumes it. Use only until
base+delta lands.

## Correctness (7/7) — all preserve exact P1 semantics
- `bulk_append_day` parity vs P1 + append/upsert + resumable (checkpoint cleared on completion).
- delta `append_to_delta` create-then-append; delta layout `time_chunk==1`.
- **TieredCube parity == P1** over base+delta day ranges (mixed var presence, land-NaN).
- **compaction parity == P1**; new base latest == `end_day`; delta reset to post-`end_day` days.
- app: `GHRSST_DELTACUBE_PATH` → `cube_kind=tiered`, multi-day range routes to cube and matches P1;
  `/healthz` exposes `cube_kind` / `delta_latest` / `delta_day_count`.

## Immediate action (ops) + runbook
- **Stop the current (RMW) append**; run the read gate on the completed **364-day holdout** base cube.
- Daily ingest uses **`append_to_delta`** (cheap); periodic **`compact`** folds delta → base.
- `p2s8_vm24_runbook.md` updated: build base with the bulk builder, serve base+delta tier, append via
  delta, compact periodically; the binding append number is measured on VM24.
