# P2-S3 — Tier-2 time-cube (Candidate A) — result: STRUCTURAL WIN (proceed)

> After Tier-1 (E1, F) was benchmark-disproved, Tier-2 must prove a **structural reduction**
> of the O(days) × large-chunk work. It does. Tools: `ingest/build_timecube.py`,
> `store/time_cube.py`, `bench/bench_timecube_sweep.py`, `tests/test_phase2_s3.py` (5/5).
> NON-BINDING (synthetic fixture, warm; real-data ≥365 + cold = VM24 binding gate later).

## Parity — PASS (5/5)
`TimeCubeStore.point_series` == P1 `StoreAccess.point_series` (value + day order + field-not-in-cube
omit + subset + append-visibility). The time store is a correct drop-in for the time-series route.

## Sweep — 365-day synthetic fixture (64×64), point series, warm
Daily baseline: `chunk_count=1095` (=365×3, **O(days)**), decompressed 17.94 MB, read_amp 4096×,
**p50 983 ms / p95 1003 ms**.

| variant | chunk_count | decompressed | read_amp | p95 ms | files | append ms |
|---|---|---|---|---|---|---|
| daily baseline | 1095 | 17.94 MB | 4096× | 1003 | — | — |
| s4  t=all shard | 3 | 0.070 MB | 16× | 2.7 | 10 | 72 |
| s8  t=all shard | 3 | 0.280 MB | 64× | 2.8 | 10 | 32 |
| s16 t=all shard | 3 | 1.121 MB | 256× | 3.2 | 10 | 21 |
| s32 t=all shard | 3 | 4.485 MB | 1024× | 4.7 | 10 | 18 |
| s4  t=90 shard | 15 | 0.086 MB | 19.7× | 5.1 | 18 | 103 |
| **s8  t=90 shard** | **15** | **0.346 MB** | **78.9×** | **5.4** | **18** | **30** |
| s16 t=90 shard | 15 | 1.382 MB | 315× | 5.1 | 18 | 14 |
| s8  t=90 noshard | 15 | 0.346 MB | 78.9× | 3.1 | **648** | 51 |
| s4  t=90 noshard | 15 | 0.086 MB | 19.7× | 3.3 | **2568** | 194 |

## Findings
1. **Structural reduction achieved (cache-independent)**: `chunk_count` drops from **O(days) 1095
   → O(days/time_chunk) 3–15** (70–365×); decompressed/read_amp crash (4096× → 16–79× for small
   spatial chunks). Warm latency 1003 ms → **2–5 ms (~200–480×)**. This is the win Tier-1 could not
   get because it reduces *total work*, not just parallelism. On PROD (1024² daily chunk, ~1M×
   amp) the daily baseline is far worse, so the relative advantage is larger still.
2. **Sharding is essential and cheap**: small spatial chunks explode file count (s4 t=90 → 2568
   files) → **v3 sharding collapses it to 18** with only ~+2 ms p95 (shard index read). Keep
   sharding on for any small-chunk candidate.
3. **Append vs read trade (time_chunk)**: `time_chunk=all` gives the fewest read chunks (3) but at
   scale would rewrite the whole array per appended day; `time_chunk=90` keeps append to one slab
   (here 14–103 ms) while still tiny read chunk_count (15). For a **daily-appended** store, prefer a
   moderate time_chunk.
4. **Spatial chunk vs read_amp**: smaller spatial chunk → lower read_amp (better point reads). bbox
   stays on the daily store (hybrid routing), so a small spatial chunk does NOT hurt bbox.

## Recommended chunking (carry to P2-S4+; confirm on real data/append-at-scale)
**spatial = 8, time_chunk = 90, sharded** (`s8_t90_shard`): chunk_count 15, read_amp ~79×, p95
~5 ms, files 18, append ~30 ms — a balanced point of fast reads, cheap daily append, and low file
count. (`s4` lowers amp further but raises append; `s16` is fine too. Final pick must be confirmed
with the real-data/append-at-scale + cold-cache numbers on VM24.)

## Decision / next
- **Tier-2 time-cube is the structural fix** — adopt Candidate A. Tier-1 stays rejected.
- **Hybrid routing holds**: point/range/time-series → time-cube; bbox/single-day + POST/points →
  daily store (P1). bbox not regressed (untouched).
- Open design item for P2-S4: **per-day var presence on real data** (a day lacking `sst_anomaly`).
  The cube is dense; the access layer must reconcile (presence mask, or build-time fill + a
  per-(day,var) validity record) to keep exact old/P1 omit semantics. Synthetic fixtures here have
  all vars on all days, so this is deferred to P2-S4, not yet exercised on real data.
- Next: **P2-S4** time_store access layer is largely built (`TimeCubeStore` + parity) — finish the
  validity/absence handling; **P2-S5** hybrid router in store/api; **P2-S6** dual-write ingest
  (`append_day`); **P2-S7** full local gate; **P2-S8** VM24 binding.
- **Still NOT promotable**: synthetic + local <365 contiguous real days; the binding gate needs
  real ≥365 contiguous + cold cache on VM24.
