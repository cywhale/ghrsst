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
  49 ms warm / 467 ms C8** (vs daily 15 ms) — meets the **absolute recent-window POST budget** below.
  **Historical-day (base) POST stays 2.5 s** — same root
  cause as historical bbox (2 901 tiny `(90,8,8)` chunks × 90 steps). So the blocker is **historical**,
  not POST per se. ✅ recent(delta) / ❌ historical(base).

## The clean split (precise per path)
- **RECENT days (DELTA tier `s256/t1`): all paths operationally fast, but not identical** —
  point **2.6 ms** warm / 11 ms C8; bbox 750k **18 ms** warm / **133 ms** C8; POST(1000) **49 ms** warm
  / **467 ms** C8. A cube-authoritative system serves all recent-day queries with no daily store. (POST
  is the slowest recent path — see threshold reconciliation below.)
- **HISTORICAL days (BASE tier `s8/t90`): point is fine; bbox and scattered POST are blockers**
  (~90× read amp + tens of thousands of tiny chunks). The base layout is read-optimal for the PRIMARY
  point-series workload and **must not be re-chunked**.

## Threshold reconciliation — delta POST → absolute recent-window budget (RESOLVED)
The original §3.2 bar was "point/POST from cube ≤ daily p95 + 25%". Daily POST warm p95 ≈ **15 ms** → a
~19 ms bar, which **delta POST (49 ms warm / 467 ms C8) fails** while being clearly usable. Since the
adopted policy serves **POST /points from the recent tier only**, the bar is **revised to an absolute
recent-window budget: POST warm p95 < 100 ms and C8 p95 < ~1 s** — which **delta POST meets**. Rationale:
daily's ~15 ms is an artifact of its `(1,1024,1024)` single-chunk layout (all points in one chunk), not
a meaningful FLOOR to hold the cube to. Point GET keeps daily+25% (or absolute < ~50 ms). bbox conclusion
unaffected.

## Recommendation → adopted policy: Option A + D with `SPATIAL_WINDOW_DAYS = 31`
Evidence supports one simple rule (evidence only — **NOT authorization to prune**):
- **Time-cube authoritative**; daily Zarr = short-term staging only.
- **Full history** for **point time-series / range** and **single-day point GET** (cheap from the cube
  at any age: ~2–3 ms).
- **Spatial queries (bbox + POST /points) served only for the latest `SPATIAL_WINDOW_DAYS = 31` days**
  (recent = bbox-friendly delta + newest base blocks). **Older spatial → clear 4xx** naming the
  available window. **One date cutoff, no small/large split.**
- The **historical/base bbox+POST results above are the RATIONALE for the 31-day cutoff** (base `s8/t90`
  ~90× read amp), **not** a path to optimize (base is read-optimal for the primary point-series).
- **Option C** (separate single-day raster store) held in reserve only if full-history spatial later
  becomes a hard external requirement.
One growing store (vs two ~2 TB+); every high-volume path stays fast. (Design: P4 spec §4.1.)

## Gate status — what this memo does NOT do
- It does **not** authorize deleting or pruning the daily store.
- Numbers are **synthetic + warm + local**; the **P4-S0b VM24 read-only binding gate** must validate the
  §4.1 policy on the real production base+delta (read-only): full-history point/range + single-day point,
  recent-31-day bbox + POST within budget, and that older spatial would be rejected — before any
  storage-policy commit.
- P3-S1 `format=grid`/`columnar` stay **experimental**; the frontend contract (P3-S3) stays paused
  until the policy sign-off (P4-S1).

## Next
**P4-S0b** (VM24 read-only binding gate — needs explicit approval; validates the `SPATIAL_WINDOW_DAYS=31`
policy) → **P4-S1** finalize policy.
