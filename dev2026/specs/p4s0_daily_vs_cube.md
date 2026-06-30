# P4-S0 — daily store vs time-cube: feasibility evidence + decision memo

**LOCAL/SHADOW FEASIBILITY ONLY. This does NOT authorize pruning the daily store** — that requires the
**P4-S0b VM24 read-only binding gate** (§3.3 of the P4 spec) on real production data + explicit
approval. Synthetic 1024² topology, full 90-day base block; no VM24/production data touched.

Tools: `bench/bench_p4s0_daily_vs_cube.py`, prototype reads `store/cube_singleday_proto.py`, parity
`tests/test_phase2_p4s0.py` (3/3). Artifact `bench/results/p4s0_daily_vs_cube_<UTCDATE>.json`. Chunk
shapes: **daily `(1,1024,1024)` · base `(90,8,8)` · delta `(1,256,256)`**. Cold not measured locally;
**`read_amp`/`chunk_count` are the cache-independent discriminators** (P2 method); warm p95 + C8 are
empirical (warm).

## Parity first (correctness) — PASS
All cube single-day reads reproduce the daily store's values **and** semantics (point → omit absent
key; bbox/batch → null absent; land NaN → null; `points_batch` `index` key) — `test_phase2_p4s0.py`
3/3 and the bench parity column (bbox/point/batch on base = `True`; delta cells are recent-day, no
daily-historical reference). A perf number is only meaningful because parity holds.

## Perf — daily vs base (historical day) vs delta (recent day)
### single-day BBOX
| size | source | read_amp | chunk_count | warm p95 | C8 p95 / RSS |
|---|---|---|---|---|---|
| small 50k | daily | 21× | 3 | 7 ms | 20 ms / 21 MB |
| | **base** | **90×** | **2 352** | **335 ms** | **2 592 ms** / 14 MB |
| | delta | 1.3× | 3 | 4 ms | 30 ms |
| med 250k | daily | 4× | 3 | 7 ms | 21 ms |
| | **base** | **91×** | **11 907** | **790 ms** | **6 892 ms** |
| | delta | 1.0× | 12 | 5 ms | 32 ms |
| large 750k | daily | 1.4× | 3 | 8 ms | 23 ms |
| | **base** | **91×** | **35 643** | **3 212 ms** | **26 186 ms** |
| | delta | 1.4× | 48 | 17 ms | 121 ms |

### single-day POINT
| source | read_amp | warm p95 | C8 p95 |
|---|---|---|---|
| daily | 1 048 576× | 9.4 ms | 20 ms |
| base | 5 760× | **2.7 ms** | 13 ms |
| delta | 65 536× | 2.5 ms | 10 ms |

### POST /points (1000 scattered, single day)
| source | chunk_count | warm p95 | C8 p95 |
|---|---|---|---|
| daily | 3 | **14 ms** | 319 ms |
| base | 2 901 | **2 434 ms** | 11 916 ms |
| delta | 48 | 2 483 ms | 9 717 ms |

## Findings (vs the §3.2 provisional thresholds)
- **single-day POINT from cube: PASS, even better than daily.** Daily decompresses a 4 MB `(1,1024,1024)`
  chunk for one cell (read_amp ~1 M×); the cube's small spatial chunks make point reads ~2–3 ms. Cube is
  the better store for points. ✅
- **recent-day (DELTA) bbox: PASS.** `(1,256,256)` → read_amp ~1×, warm ≤ 17 ms, C8 ≤ 121 ms at 750k —
  comfortably under the delta-bbox bar. ✅
- **historical-day (BASE) bbox: FAIL (hard blocker).** `(90,8,8)` gives ~90× read amp AND tens of
  thousands of tiny chunks → warm 0.3–3.2 s and **C8 up to 26 s** at 750k. This is the structural cost
  the P4 spec predicted; **re-chunking the base is off the table** (it's read-optimal for the primary
  point-series workload). ❌
- **scattered POST /points from cube: FAIL.** Random points hit many small chunks (base 2 901, delta 48)
  → ~2.4 s vs daily's 14 ms (one big chunk holds everything). Clustered/local batches would be far
  cheaper, but the worst-case external batch is a blocker from the cube. ❌ (clustered batch: re-measure
  if needed.)

## Recommendation (evidence only — NOT authorization to prune)
A **time-cube-authoritative** architecture is feasible for the **dominant workloads**: long
point/range (already cube) **and** single-day point, **and** recent-day bbox (delta). The blockers are
**historical bbox** and **scattered POST /points**, both because the base cube's read-optimal
`s8/t90` layout is hostile to single-day spatial scatter.

→ Lean **Option A (cube authoritative + daily short-term staging)** **combined with constraining the
two blockers** (Option D for those paths):
- serve point/range + single-day point + **recent** bbox from the cube;
- **constrain/deprecate historical large bbox** (cap size, or document "recent-only", or WMS for
  spatial); **constrain scattered POST /points** to local/clustered or small N;
- only add a **separate single-day raster store (Option C)** if historical bbox / scattered batch are
  later judged hard requirements that A∪D can't satisfy.

This prioritizes storage sustainability (one growing store, not two ~2 TB+) while keeping every
high-volume path fast.

## Gate status — what this memo does NOT do
- It does **not** authorize deleting or pruning the daily store.
- Numbers are **synthetic + warm + local**; the **P4-S0b VM24 read-only binding gate** must rerun this
  matrix on the real production base+delta (read-only) and clear §3.2 before any storage-policy commit.
- P3-S1 `format=grid`/`columnar` stay **experimental**; the frontend contract (P3-S3) stays paused
  until the storage decision (P4-S1) is made.

## Next
**P4-S0b** (VM24 read-only binding gate — needs explicit approval) → **P4-S1** storage-policy decision.
