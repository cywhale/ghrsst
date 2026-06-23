# P2-S7 — chunking selection (READ-FIRST) — result

> Per review: users feel READ latency; ingest writes once/day → **append is an operational
> CONSTRAINT, not the optimization target.** Select chunking read-first, then reject only the
> append-infeasible. `s8` is NOT pre-discarded. Tool: `bench/bench_chunking_select.py`.
> NON-BINDING (synthetic, warm, regional; extrapolated to global). VM24 cold + full-grid + real
> append is the binding gate.

## Selection sweep — 365-day fixture, 256² grid, time_chunk=90, sharded(64), warm
| spatial | read_amp | p95 C1 | LR C8 | RSS peak | append regional | append global est | files global est | feasible (3 h) |
|---|---|---|---|---|---|---|---|---|
| **8** | **78.9×** | 4.5 ms | 37.8 ms | 168 MB | 406 ms | **~67 min** | ~2.45 M | **yes** |
| 16 | 315.6× | 4.6 ms | 38.8 ms | 281 MB | 131 ms | ~21.5 min | ~2.45 M | yes |
| 32 | 1262.5× | 4.7 ms | 39.1 ms | 329 MB | 55.5 ms | ~9.1 min | ~2.45 M | yes |
| 64 | 5049.9× | 4.3 ms | 32.8 ms | 338 MB | 37.6 ms | ~6.2 min | ~2.45 M | yes |

(global append/files extrapolated by area ratio GLOBAL_CELLS/256² ≈ 9890×; ingest window 180 min.)

## Findings (read-first)
1. **Read gate: all candidates pass absolutely** — warm p95 ~4–5 ms (C1) and ~33–39 ms (C8), far
   under the 4 s target; RSS bounded (168–338 MB). The cube crushes the absolute read gate.
   - Note on the LR *multiplier*: C8/C1 ≈ 8× looks like it "fails" the <3× rule, but that rule was
     framed for the daily store where the baseline B was **seconds**. Here B ≈ 4.5 **ms**, so the
     multiplier is inflated by a tiny baseline while the **absolute** p95 (38 ms) is excellent. For
     the cube, judge reads by absolute latency + `read_amp`, not the daily-era multiplier.
2. **`read_amp` is the real read discriminator (cache-independent)** and strongly favors small
   spatial: **s8 = 79× vs s64 = 5050×** — s8 reads ~64× less data per query, the decisive advantage
   at COLD cache / scale (where the VM24 failure lived). Warm latency hides this; read_amp does not.
3. **Append is feasible for ALL candidates in a 3 h window** (s8 ~67 min global est). Per the review
   rule, append does NOT reject s8.
4. **File count is controlled by SHARD size, not inner chunk** — all spatials give ~2.45 M global
   files at shard=64 (a shard packs the small inner chunks). So **shard size is an independent lever**:
   enlarge it (e.g. 128–256) to cut global file count without changing read_amp/read latency.

## Recommendation
- **Read-first pick: `spatial = 8` (inner chunk), time_chunk = 90, sharded** — lowest `read_amp`
  among append-feasible candidates. This **corrects P2-S6's "retire s8"**: s8 is read-optimal and its
  global append (~67 min est) fits a typical multi-hour daily ingest window.
- **Tune shard size separately for file count** (e.g. 128–256) to bring global files well under ~2.4 M
  without touching read performance.
- **If the real VM24 ingest window is tight** (e.g. <30 min) or ~2.4 M files is unacceptable even
  after shard tuning, step up to s16/s32 (append 22/9 min, read_amp still 316/1263× — far better than
  daily's ~1 M×), or use **regional/tiled cubes** (bounded append, hybrid router already falls back to
  daily for uncovered areas).

## Shard-size sweep (review item 2 — shard is NOT a free lever)
File count is controlled by **shard** size, not inner chunk — but a larger shard packs more inner
chunks per shard FILE, so a daily append's read-modify-write rewrites a bigger shard, and shard size
can affect read/metadata/cache behaviour. So it must be **measured**, not assumed. `bench/
bench_shard_sweep.py` sweeps shard ∈ {64,128,256} with `spatial=8, time_chunk=90` fixed (365-day
fixture, 256², warm):
| shard | files regional | files global est | read p95 C1 | C8 | p99 C8 | RSS | append p50 | append global est |
|---|---|---|---|---|---|---|---|---|
| 64 | 248 | ~2.45 M | 4.6 ms | 38.9 | 41.3 | 166 MB | 420 ms | ~69 min |
| **128** | 68 | **~672 K** | 4.7 ms | 37.5 | 39.7 | 180 MB | 450 ms | ~74 min |
| 256 | 23 | ~227 K | 4.8 ms | 38.2 | 40.2 | 299 MB | 547 ms | ~90 min |

**Shard IS a real tradeoff (measured, not free):** read p95/p99 ~unchanged (read_amp is set by inner
chunk=8, not shard) → read-neutral; larger shard → far fewer files (2.45 M→227 K) BUT more expensive
append (69→90 min, bigger read-modify-write) AND higher RSS (166→299 MB). **`shard=128` is the
operational balance** (10× fewer files than 64 ≈ 672 K, append ~74 min within a 3 h window, RSS 180 MB).

**Updated chunking recommendation: `spatial=8 / time_chunk=90 / shard=128`.** Final shard confirmed on
VM24 vs the real ingest window + file-count tolerance (shard=256 if files dominate and ~90 min append
fits; shard=64 if append/RSS must be minimal and ~2.4 M files acceptable).

## Caveats / binding gate
- **RSS from `bench_chunking_select.py` / `bench_shard_sweep.py` is ILLUSTRATIVE only** (store-level
  threads, warm, synthetic). The **binding RSS comes from the P2-S7 HTTP gate** (`/healthz` sampling
  under load) and ultimately VM24.
- Warm + synthetic + extrapolated. The append extrapolation assumes ~linear scaling with area and
  doesn't fully model shard read-modify-write at global scale. **VM24 is binding**: real ≥365
  contiguous + cold-cache read p95/p99 + LR concurrency + RSS, AND a real full-grid (or per-tile)
  append measurement against the actual daily ingest window + file-count tolerance.
- No promotion claim. The final production chunking is decided on VM24, with s8 as the read-first
  starting candidate (shard size tuned for files), s16/s32 or regional as the append/ops fallback.

## Next
P2-S7 full local gate on the chosen chunking via the HTTP `loadtest.py` scenarios (cube-backed API,
confirming `X-Store-Route: cube` + read p95/p99 + RSS + backpressure) → then P2-S8 VM24 binding.
