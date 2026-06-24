# Bulk time-cube builder — fixes the slow build (write-pattern, not read-chunking)

> Deployment hit a build that would take **20+ days**. Root cause (Codex): the per-day writer
> read-modify-writes the whole `time_chunk`-spanning shard every day. Fix: a **bulk shard-block
> builder**. Read chunking (`s8/t90/shard=128`) is unchanged — reads are still fast; only the WRITE
> strategy changed. Tool: `ingest/build_timecube_bulk.py`, tests `tests/test_phase2_bulk.py` (6/6).

## Root cause
`build_timecube` writes `arr[i, :, :] = day` one day at a time. A shard is `(time_chunk, shard,
shard)` = e.g. `(90,128,128)`, so writing a single time index `i` touches a shard that holds 90 time
steps → Zarr must **read the existing shard, insert the one slice, re-write the whole shard**. Over a
`time_chunk` block, each shard is rewritten ~`time_chunk` (90) times → ~90× write amplification.

## Fix — write one shard-block at a time
`build_timecube_bulk` writes by **time_block × spatial super-tile**:
- assemble a block `(time_chunk, read_block, read_block)` from the daily source days (read_block is a
  multiple of shard; default = the daily source chunk size so each daily chunk is decompressed once),
- write `arr[t0:t1, i0:i1, j0:j1] = block` **once** — the write covers complete shards → each shard
  written exactly once, **no partial-shard RMW**.
- **bounded parallelism** (default 4 workers) over disjoint `(var, time_block, tile)` units → disjoint
  shard files, safe.
- **checkpoint/resume** per unit in `<out>/_build_checkpoint.json` — rerun to continue after an
  interruption; completed units skip (writes are idempotent so a re-done unit is harmless).
- **`--latest-days N`**: build the most recent N days FIRST (router falls back to daily for older
  ranges); extend later by rerunning with a larger N. Also `--exclude-latest`/`--end-day` for the
  P2-S8 true-append holdout.
- memory ≈ `workers × time_chunk × read_block² × 4` bytes per var (tune `--read-block`/`--workers`).

## Measured speedup
| build | grid | days | per-day | bulk (workers=4) | speedup |
|---|---|---|---|---|---|
| full | 256² | 90 | **41.8 s** | **0.5 s** | **77.7×** |
| holdout (364d) | 256² | 364 | ~60 s | **2.3 s** | ~26× |

The speedup tracks the removed RMW factor (~`time_chunk`); at **global scale it turns the 20+ day
build into well under a day** (bulk is read-bound: read all source once + one write per shard).

## Correctness (6/6 tests) — identical output, just faster
- **layout matches** per-day (shape/chunks/shards identical).
- **parity**: `TimeCubeStore(bulk).point_series == TimeCubeStore(perday) == P1` (multiple points,
  mixed var presence, land-NaN).
- **validity mask** identical to the per-day build.
- **resume idempotent**: rerun on a complete cube → 0 new units, unchanged + parity.
- **resume after partial**: drop half the checkpoint → rerun re-does them, cube still correct.
- **`--latest-days`**: builds exactly the latest N days, parity on that window.

## Runbook
`p2s8_vm24_runbook.md` §2 now uses `build_timecube_bulk --workers 4` (resumable, `--latest-days` for
incremental availability, `--exclude-latest` for the holdout). Read chunking and all downstream gates
are unchanged. No promotion (synthetic); VM24 remains binding.
