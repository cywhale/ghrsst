# P4-S0 — daily store vs time-cube: feasibility evidence + decision memo

**LOCAL/SHADOW FEASIBILITY ONLY. This does NOT authorize pruning the daily store** — that requires the
**P4-S0b VM24 read-only binding gate** (§3.3 of the P4 spec) on real production data + explicit
approval. Synthetic 1024² topology, full 90-day base block; no VM24/production data touched.

Tools: `bench/bench_p4s0_daily_vs_cube.py`, prototype reads `store/cube_singleday_proto.py`, parity
`tests/test_phase2_p4s0.py` (3/3). Artifact `bench/results/p4s0_daily_vs_cube_<UTCDATE>.json`. Chunk
shapes: **daily `(1,1024,1024)` · base `(90,8,8)` · delta `(1,256,256)`**. Cold not measured locally;
**`read_amp`/`chunk_count` are the cache-independent discriminators** (P2 method); warm p95 + C8 are
empirical (warm).

## Parity first (correctness) — PASS (every source vs its own daily day)
All cube single-day reads reproduce the daily store's values **and** semantics (point → omit absent
key; bbox/batch → null absent; land NaN → null; `points_batch` `index` key). The bench now compares
**each source against the daily read of that source's day** (base vs daily-historical, delta vs
daily-recent) → **parity `True` for bbox/point/POST on BOTH base and delta** (Codex P4-S0 #Medium);
`test_phase2_p4s0.py` 3/3. A perf number is only meaningful because parity holds.

## Perf — daily vs base (historical day) vs delta (recent day)
### single-day BBOX
| size | source | read_amp | chunk_count | warm p95 | C8 p95 / RSS |
|---|---|---|---|---|---|
| small 50k | daily | 21× | 3 | 8 ms | 21 ms |
| | **base** | **90×** | **2 352** | **365 ms** | **2 767 ms** |
| | delta | 1.3× | 3 | 5 ms | 37 ms |
| med 250k | daily | 4× | 3 | 8 ms | 22 ms |
| | **base** | **91×** | **11 907** | **845 ms** | **7 522 ms** |
| | delta | 1.0× | 12 | 5 ms | 34 ms |
| large 750k | daily | 1.4× | 3 | 8 ms | 22 ms |
| | **base** | **91×** | **35 643** | **3 343 ms** | **27 658 ms** |
| | delta | 1.4× | 48 | 18 ms | 133 ms |

### single-day POINT
| source | read_amp | warm p95 | C8 p95 |
|---|---|---|---|
| daily | 1 048 576× | 7.4 ms | 19 ms |
| base | 5 760× | **2.9 ms** | 13 ms |
| delta | 65 536× | 2.6 ms | 11 ms |

### POST /points (1000 scattered, single day) — chunk-GROUPED reads (Codex #High)
| source | chunk_count | warm p95 | C8 p95 |
|---|---|---|---|
| daily | 3 | **15 ms** | 334 ms |
| base | 2 901 | **2 484 ms** | 12 197 ms |
| delta | 48 | **49 ms** | 467 ms |
> The first cut read each point individually and overstated the cube (delta showed 2.5 s for only 48
> chunks). With chunk-grouped reads (mirroring daily: group by chunk → read each distinct chunk's
> minimal block once per field) **delta POST drops ~50× to 49 ms**; base stays slow (its 2 901 tiny
> chunks must each decompress 90 time steps — the same read-amp wall as historical bbox).

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
- **scattered POST /points: DELTA usable / BASE FAIL.** With grouped reads, **recent-day (delta) POST is
  49 ms warm / 467 ms C8** (vs daily 15 ms) — operationally fine but **exceeds the strict daily+25% bar**
  (see threshold reconciliation → P4-S1 product decision). **Historical-day (base) POST stays 2.5 s** —
  same root cause as historical bbox (2 901 tiny `(90,8,8)` chunks × 90 steps). So the blocker is
  **historical**, not POST per se. ⚠️ delta (usable, bar TBD) / ❌ base.

## The clean split (precise per path)
- **RECENT days (DELTA tier `s256/t1`): all paths operationally fast, but not identical** —
  point **2.6 ms** warm / 11 ms C8; bbox 750k **18 ms** warm / **133 ms** C8; POST(1000) **49 ms** warm
  / **467 ms** C8. A cube-authoritative system serves all recent-day queries with no daily store. (POST
  is the slowest recent path — see threshold reconciliation below.)
- **HISTORICAL days (BASE tier `s8/t90`): point is fine; bbox and scattered POST are blockers**
  (~90× read amp + tens of thousands of tiny chunks). The base layout is read-optimal for the PRIMARY
  point-series workload and **must not be re-chunked**.

## Threshold reconciliation — delta POST vs the §3.2 provisional bar
The P4 spec §3.2 sets "point/POST from cube ≤ daily p95 + 25%". Daily POST warm p95 ≈ **15 ms**, so the
strict bar is ~19 ms; **delta POST at 49 ms warm / 467 ms C8 FAILS that bar** — while being clearly
usable in absolute terms. Two readings, flagged as a **product decision for P4-S1**:
- **(mark)** delta POST is **"usable but exceeds daily+25%"**; keep the strict relative bar and accept
  this as a known, documented gap; OR
- **(revise, recommended)** replace the relative POST bar with an **absolute budget** — e.g. **POST warm
  p95 < 100 ms and C8 p95 bounded (< ~1 s)** — which delta meets. Rationale: daily's ~15 ms is an
  artifact of its `(1,1024,1024)` layout where all points fall in ONE chunk; that is not a meaningful
  performance FLOOR to hold the cube to. An absolute interactive budget is the sounder product bar.
This does not affect the bbox conclusion; it only concerns the recent-day POST bar. **P4-S1 decides.**

## Recommendation (evidence only — NOT authorization to prune)
Lean **Option A (cube authoritative + daily short-term staging) + Option D for the historical-spatial
blockers**:
- serve point/range + single-day point (any day) + **all recent/delta-day** bbox & POST from the cube;
- **constrain/deprecate large HISTORICAL bbox & scattered HISTORICAL POST** (cap size / "recent-only" /
  WMS for spatial), unless an external requirement justifies a **separate single-day raster store
  (Option C)** for historical spatial reads;
- this keeps one growing store (vs two ~2 TB+) while every high-volume path stays fast.

## Gate status — what this memo does NOT do
- It does **not** authorize deleting or pruning the daily store.
- Numbers are **synthetic + warm + local**; the **P4-S0b VM24 read-only binding gate** must rerun this
  matrix on the real production base+delta (read-only) and clear §3.2 before any storage-policy commit.
- P3-S1 `format=grid`/`columnar` stay **experimental**; the frontend contract (P3-S3) stays paused
  until the storage decision (P4-S1) is made.

## Next
**P4-S0b** (VM24 read-only binding gate — needs explicit approval) → **P4-S1** storage-policy decision.
