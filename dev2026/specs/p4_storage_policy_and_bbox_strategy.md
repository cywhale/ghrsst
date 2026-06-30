# P4 — storage policy & bbox strategy (can the system be time-cube authoritative?)

Status: **DRAFT for Codex review** (Claude authored; Codex reviews; Claude implements after sign-off).
Supersedes the "go straight to a bbox frontend contract" plan. The P3-S1 compact-bbox work is now
prototype/evidence ([`p3s1_bbox_format.md`](p3s1_bbox_format.md)); the **frontend contract (P3-S3)
waits for the decision in this spec.**

## 0. New context (VM24 time-cube rebuild + storage review)
- VM24 base time-cube now covers **2023-01-01 .. 2026-06-26**; delta **2026-06-27 .. 2026-06-28**;
  production API serves point/range from the **base+delta tier**.
- The **daily Zarr store is still present and also large**. Daily store **and** time-cube are each
  **~2 TB+** and **both grow every day**. Keeping two full authoritative stores long-term is not
  sustainable on the current disk.
- **Decision point:** can the system become **time-cube authoritative**, with the daily Zarr kept only
  as **short-term staging** (or removed long-term)?
- **Bbox is the hinge.** Today bbox is served from the **daily store** (hybrid router: bbox /
  single-day / POST points → daily). If the daily store is downgraded/removed, bbox must either be
  served from the **time-cube** or be **constrained/deprecated**.
- Product reality: the **internal map platform mostly needs long point time-series** (cube's strength);
  **spatial map rendering is via WMS**. **Bbox is mainly an external/API feature** — useful, but maybe
  not worth keeping a second full data store alive for.

## 1. Goal / non-goals
**Goal:** decide the long-term storage architecture **prioritizing storage sustainability**, and
determine whether **bbox is a blocker or non-blocker** for dropping the daily store as the long-term
authoritative store — backed by a benchmark gate (bbox from daily vs from cube), a redefined raster
wire format, and an ingest policy for the authoritative mode.
**Non-goals:**
- Not implementing the migration or touching VM24 (design + local/shadow benchmarks only; §7).
- Not finalizing the frontend contract (P3-S3 is gated on this decision).
- Not preserving bbox **at any cost** — sustainability wins ties.
- Not re-chunking the base cube for bbox: `s8/t90` is read-optimal for the **primary** point-series
  workload (P2-S3/S7) and must not be sacrificed.

## 2. The technical crux — why bbox-from-cube is not free
Single-day bbox = read `arr[t_day, i0:i1, j0:j1]` for ONE time index.
- **Daily store** chunk `(1, 1024, 1024)`: reads a few big single-time chunks → cheap, ~no time
  amplification. (This is why bbox lives on the daily store today.)
- **Base cube** chunk `(time_chunk=90, 8, 8)`: to read one day it must decompress the **whole 90-step**
  inner chunk per 8×8 spatial tile → **~90× read amplification** + many tiny chunks. Time-major layout
  is hostile to single-day spatial extraction.
- **Delta cube** chunk `(1, 256, 256)` (P3 append-optimized): one time index, large spatial chunks →
  **bbox-friendly**. So bbox on a **recent (delta) day is cheap from the cube**, but bbox on a
  **historical (base) day is read-amp-heavy**.
Hypotheses for the gate (§3): **H-A** base-cube single-day bbox is ≫ slower / heavier than daily (read
amp ≈ time_chunk); **H-B** delta-cube single-day bbox ≈ daily; **H-C** the cost scales with bbox size
and concurrency (RSS). These DECIDE whether daily can be dropped.

## 3. Benchmark gate — bbox: daily store vs time-cube (the deciding evidence) — **P4-S0**
> Key question: **served from the cube, is bbox performance acceptable enough to remove the daily store
> as a long-term authoritative store?** Local/shadow only; build a small local daily + base + delta for
> a few days; add a *prototype* cube single-day-bbox read path (pick time index → spatial slice).

Matrix (each cell: latency, **chunk_count / decompressed_bytes / read_amp** (cache-independent, the P2
discriminators), RSS, encode):
- **source:** daily store · base-cube day · delta-cube day
- **size:** small / medium / large (≈ 50k / 250k / 750k pts)
- **cache:** cold (`vmtouch -e` or fresh process) / warm
- **concurrency:** 1 / N (RSS + latency degradation)
- **wire:** current `format=json` + compact raster candidate (reuse the P3-S1 harness)
Output `specs/p4s0_bbox_daily_vs_cube.md` + `bench/results/p4s0_*.json`, with a per-size verdict:
**bbox-from-cube acceptable? (yes / yes-for-delta-only / no).** This feeds §4 option choice.

## 4. Storage policy options (decide AFTER the §3 gate) — **P4-S1**
| opt | architecture | long-term disk | bbox | point/range | notes |
|---|---|---|---|---|---|
| **A** | **time-cube authoritative + daily = short-term staging (7/14/30d), then pruned** | **1 store (cube)** | from cube (delta cheap; base read-amp) | native (cube) | smallest sustainable; bbox quality bounded by §3 |
| B | daily authoritative + rolling/trimmed time-cube | ~2 stores still | from daily | trimmed cube limits long series | doesn't solve disk if daily keeps full history |
| C | time-cube + **separate single-day bbox/raster store** | cube + smaller raster store | fast (purpose-built) | native (cube) | adds a 3rd store; only if bbox is a hard requirement AND §3 says cube-bbox is unacceptable |
| **D** | **constrain/deprecate bbox; WMS for spatial; cube for point/range** | **1 store (cube)** | limited/none (small bbox or none) | native (cube) | smallest + simplest; drops/limits external bbox API |

**Recommendation lean (to confirm with §3 evidence):** **A** if cube-bbox is acceptable at the sizes
that matter (esp. recent/delta days); **D** if base-cube bbox is unacceptable and bbox isn't worth a
second store; **C** only if bbox is a hard external requirement that A/D can't satisfy. **B** is
unlikely (doesn't fix disk). Prioritize sustainability over preserving bbox.

## 5. Compact bbox wire format → RASTER-style (redefine; supersedes the prototype `grid`) — **P4-S2**
Only finalized once the serving store is chosen (§4). The xarray-like `lon[]`/`lat[]`+2-D `grid` is not
intuitive for the frontend/API contract; use a **raster** contract:
```json
{
  "date": "2026-01-01",
  "format": "raster",
  "bbox_requested": [lon0, lat0, lon1, lat1],
  "bbox_actual":    [x0, y0, x1, y1],
  "nx": <int>, "ny": <int>,
  "x0": <float>, "y0": <float>, "dx": <float>, "dy": <float>,
  "crs": "EPSG:4326",
  "scan": "row-major", "order": "north-to-south,west-to-east",
  "index_formula": "k = iy * nx + ix",
  "fields": { "sst": [ ... nx*ny ... ], "sea_ice": [ ... ] },
  "field_status": { "sst": "present", "sst_anomaly": "absent" }
}
```
Rules: fields are **flat row-major arrays of length `nx*ny`**; **missing cells stay `null`, never
skipped** (positional integrity); **whole-field absent → `fields[var]=null` + `field_status="absent"`**
(no giant null array). Default scan **row-major, north→south, west→east, `k = iy*nx + ix`**. **`format=json`
default stays unchanged.** Parity stays **semantic float32** (not byte). (`format=grid`/`columnar`
prototypes may be retired or kept clearly-experimental.)

## 6. Ingest policy for time-cube-authoritative mode (Option A) — **P4-S3**
What "authoritative" requires: **the cube (+delta) must reproduce every served query without the daily
store**; daily staging exists only for a bounded retention window + re-derivation safety.
- **download** NetCDF (MUR) → **append to delta** via the tiled `append_to_delta` (P3 append-opt layout).
- **validation:** coverage check + per-day/var presence + sanity (range/NaN-fraction) before commit.
- **retry / resume:** checkpointed (existing per-(day,var,tile) checkpoint); idempotent re-run.
- **duplicate / overwrite policy:** idempotent upsert (re-append same day = overwrite, count stable).
- **latest metadata:** delta `days`/`latest` advance atomically; `/healthz` reflects tier latest.
- **rollback:** keep the prior delta (and a base snapshot pointer) until the new day validates; revert
  by pointer swap.
- **daily staging retention window:** keep daily Zarr for **7 / 14 / 30 days** (open question §8) for
  re-derivation / spot-checks; prune older daily groups after the cube is validated for those days.
- **periodic delta→base compaction:** `compact()` folds full blocks into base; gate on a **free-disk
  requirement** (need headroom for the staged new base before atomic swap) — never compact below a
  safety margin.

## 7. Ops boundary (hard)
**Do NOT touch VM24 production, cron, the daily store, base cube, delta cube, deployment scripts, or
NGINX in this P4 work.** The production cube is working and must not be disturbed. This phase is
**design/spec + local/shadow benchmarks only** until explicitly approved. (Mirrors the standing rule;
restated because P4 reasons about production data that must stay untouched.)

## 8. Step plan
- **P4-S0** — bbox daily-vs-cube benchmark gate (local/shadow; cube single-day-bbox prototype read
  path; the matrix in §3) → deciding evidence. *(local/shadow benchmark — allowed now.)*
- **P4-S1** — storage-policy decision (A/B/C/D) from the evidence — orchestrator/Codex sign-off.
- **P4-S2** — raster wire-format spec finalized for the chosen serving store (§5).
- **P4-S3** — ingest-policy spec for the chosen mode (§6).
- **then** P3-S3 frontend contract resumes **iff** bbox is preserved (Option A/C).

## 9. Open questions (for orchestrator / Codex)
1. **Is bbox a hard external requirement**, or acceptable to constrain/deprecate (Option D), given
   internal maps use WMS + point series?
2. **Daily staging retention window:** 7 / 14 / 30 days?
3. **Acceptable bbox-from-cube budget** (the §3 pass bar) — e.g. recent-day (delta) bbox must be ≤ Xms;
   historical-day (base) bbox may be slow/limited?
4. **CRS** for raster — EPSG:4326 (lon/lat) confirmed?
5. Confirm **re-chunking the base cube is off the table** (would hurt the primary point-series).
6. If Option A: is **removing daily entirely** acceptable eventually, or always keep a rolling staging
   window?
