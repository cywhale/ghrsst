# P4 — storage policy & bbox strategy (can the system be time-cube authoritative?)

Status: **DRAFT — round 3: policy adopted (P4-S0 accepted)** (Claude authored; Codex reviewed 3×).
P4-S0 local feasibility is accepted; the adopted policy is **Option A + D with
`SPATIAL_WINDOW_DAYS = 31`** (§4.1), pending P4-S1 sign-off + the P4-S0b VM24 read-only binding gate.
The P3-S1 compact-bbox work is prototype/evidence ([`p3s1_bbox_format.md`](p3s1_bbox_format.md)); the
**frontend contract (P3-S3) waits for the policy sign-off.**

> Round-3 patch: **single spatial rule** — bbox + POST /points served only for the latest 31 days,
> older → clear 4xx (§4.1); **no small/large historical split; no WMS as an API substitute**; **POST bar
> revised to an absolute recent-window budget** (warm p95 < 100 ms, C8 < ~1 s) since POST is delta-only
> (§3.2); historical base results kept as the **rationale for the cutoff**, not a path to optimize; §8
> P4-S0b validates this policy on prod read-only.

> Round-2 patches folded: (1) **VM24 read-only binding gate** required before any pruning — local
> P4-S0 is feasibility only (§3.3, §8 P4-S0b); (2) gate **expanded to all daily-routed queries**
> (bbox + single-day point + POST points) + parity, both tiers (§3.1); (3) **compaction disk reality**
> — full-rebuild needs ~2 TB free (not available) → block-level/rolling/defer alternatives (§6.1);
> (4) **daily-pruning safety** — prune manifest + parity + NetCDF-redownload recovery + rollback window
> + corruption plan (§6.2); (5) **provisional P4-S0 thresholds** (§3.2); (6) raster **versioning +
> grid clarity** — format_version / nodata / axis_order / cell_ref (§5).

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

## 3. Benchmark gate — ALL daily-routed queries: daily store vs time-cube — **P4-S0 (local) + binding (VM24)**
> Key question: **served from the cube, is performance acceptable enough to demote the daily store to
> short-term staging?** Today **three** query types route to the daily store and ALL must clear
> cube/delta parity + perf before daily can be demoted (Codex review #2):
> **(a) single-day bbox, (b) single-day point, (c) POST /points (batch).**

### 3.1 Scope (Codex #2) — every daily-routed path, both tiers
Sources to compare: **daily store · base-cube day (historical) · delta-cube day (recent)**.
Queries: single-day **bbox**, single-day **point**, **POST /points** batch. For each, on BOTH a
**latest delta day** and a **historical base day**:
- **perf:** latency, **chunk_count / decompressed_bytes / read_amp** (cache-independent P2
  discriminators), RSS, encode.
- **parity (correctness, not just speed):** value equality + **duplicate / ordering / index / null
  semantics** match the daily path (the cube's per-(day,var) `var_valid` must reproduce
  absent-vs-NaN exactly). A perf win is void without parity.
Dimensions: **size** small/medium/large (≈ 50k / 250k / 750k pts for bbox), **cache** cold
(`vmtouch -e` / fresh process) / warm, **concurrency** 1 / C=4 / C=8. Wire: `format=json` + raster
candidate (reuse the P3-S1 harness). Prototype cube read paths (single-day bbox = pick time index →
spatial slice; single-day point/batch from the cube) are local/shadow only.

### 3.2 Provisional pass/fail thresholds (Codex #5 — not left open)
Bound the decision up front (tune with orchestrator in §9):
| metric | provisional bar |
|---|---|
Spatial reads (bbox + POST /points) are served ONLY from the recent window (§4.1), so their bars are
**absolute recent-window budgets**, not relative-to-daily:
| metric | bar |
|---|---|
| **recent** single-day **bbox** warm p95 (≤ medium) | **< ~200 ms**; C8 bounded |
| **recent** **POST /points** warm p95 | **< 100 ms**; **C8 p95 < ~1 s** |
| full-history single-day **point** GET | **≤ daily-path p95 + 25 %** (or absolute < ~50 ms) |
| **RSS** under C=8 | **≤ `GHRSST_RSS_CEILING_MB`** (prod 4096), no OOM, no unbounded growth |
| **concurrency** C=4 / C=8 | bounded degradation, zero 5xx/OOM |
POST is an absolute budget (not daily+25%) because it is served from the delta/recent tier only; daily's
~15 ms is an artifact of its single-`(1,1024,1024)`-chunk layout, not a meaningful floor. **Historical
(base) bbox / scattered POST are NOT a threshold to pass** — their ~90× read amp is precisely the
**rationale for the 31-day spatial cutoff (§4.1)**; the base layout is read-optimal for the primary
point-series and is **not** a path we intend to optimize.

### 3.3 Binding gate (Codex #1) — local/shadow is feasibility ONLY
**Local/shadow P4-S0 proves feasibility; it CANNOT authorize deleting or pruning the daily store.**
Before any storage-policy commit (§4) or pruning (§6), a **VM24 READ-ONLY binding gate** must rerun
the §3.1 matrix against the **real production base+delta cube** (read-only, no writes, no pruning, no
cron/deploy changes) and clear §3.2. Only the binding gate can authorize demoting the daily store.

Output `specs/p4s0_daily_vs_cube.md` + `bench/results/p4s0_*.json`; per-query, per-tier verdict:
**acceptable? (yes / yes-for-delta-only / no)** → feeds §4.

## 4. Storage policy — ADOPTED: Option A + D with a single spatial-window rule — **P4-S1 finalizes**
Options considered (P4-S0 evidence in `p4s0_daily_vs_cube.md`): **A** time-cube authoritative + daily
short-term staging (1 store, sustainable); **B** daily authoritative + rolling cube (still ~2 stores —
doesn't fix disk); **C** cube + separate single-day raster store (adds a store); **D** cube for
point/range, spatial reads constrained. P4-S0 showed recent/delta spatial is cheap while historical/base
spatial is a structural blocker, so:

### 4.1 Adopted policy (pending P4-S1 sign-off) — **`SPATIAL_WINDOW_DAYS = 31`**
- **Time-cube authoritative** (Option A); daily Zarr = short-term staging only (§6.2 retention window).
- **Spatial queries = bbox + POST /points.** Both are supported **only for the latest
  `SPATIAL_WINDOW_DAYS = 31` days** (served from the cube; recent days are in the bbox-friendly delta and
  the newest base blocks). **Older spatial queries return a clear 4xx** naming the available spatial
  window (e.g. "spatial queries limited to the latest 31 days; available window <start>..<end>").
- **Single rule, no exceptions:** do **not** split historical bbox into small/large cases — the API rule
  stays one date cutoff for all spatial requests. (Rationale: base `s8/t90` read amplification, §2/§3.2.)
- **Full-history is retained** for **point time-series / range** and **single-day point GET** (cheap
  from the cube at any age; P4-S0: point ≈ 2–3 ms).
- **Option C** (separate raster store) is held in reserve **only if** full-history spatial later becomes
  a hard external requirement that the 31-day window can't satisfy. **B** is rejected (doesn't fix disk).

This is the smallest sustainable footprint (one growing store) while every high-volume path stays fast.

## 5. Compact bbox wire format → RASTER-style (redefine; supersedes the prototype `grid`) — **P4-S2**
Only finalized once the serving store is chosen (§4). The xarray-like `lon[]`/`lat[]`+2-D `grid` is not
intuitive for the frontend/API contract; use a **raster** contract:
```json
{
  "format": "raster",
  "format_version": 1,
  "date": "2026-01-01",
  "crs": "EPSG:4326",
  "axis_order": "lon,lat",
  "bbox_requested": [lon0, lat0, lon1, lat1],
  "bbox_actual":    [x0, y0, x1, y1],
  "nx": <int>, "ny": <int>,
  "x0": <float>, "y0": <float>, "dx": <float>, "dy": <float>,
  "cell_ref": "center",
  "scan": "row-major", "order": "north-to-south,west-to-east",
  "index_formula": "k = iy * nx + ix",
  "nodata": null,
  "fields": { "sst": [ ... nx*ny ... ], "sea_ice": [ ... ] },
  "field_status": { "sst": "present", "sst_anomaly": "absent" }
}
```
Rules / clarity (Codex #6):
- **`format_version`** (int) — bump on any breaking change to this contract; clients must check it.
- **`crs`** `EPSG:4326`; **`axis_order`** `lon,lat` (x=lon, y=lat) — explicit so x/y are unambiguous.
- **`cell_ref`** = `center` (x0/y0/dx/dy describe **cell centers**, matching MUR grid-point semantics;
  stated so clients don't mis-place by half a cell). `bbox_actual` is the snapped cell-center extent.
- **`nodata`: `null`** — JSON has no NaN; masked/missing cells are JSON `null`. Fields are **flat
  row-major arrays of length `nx*ny`**; **missing cells stay `null`, never skipped** (positional
  integrity); **whole-field absent → `fields[var]=null` + `field_status="absent"`** (no giant null array).
- Default **scan** row-major, **order** north→south, west→east, **`index_formula` `k = iy*nx + ix`** —
  all explicit in-payload.
**`format=json` default stays unchanged.** Parity stays **semantic float32** (not byte). The
`format=grid`/`columnar` prototypes are retired or kept clearly-experimental once `raster` lands.

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
### 6.1 Compaction under the CURRENT disk reality (Codex #3)
The P3 `compact()` = **full staged base rebuild**, which needs **~2 TB+ free** for the new base before
the atomic swap. **After the full cube rebuild that headroom does NOT exist** → **full-rebuild
compaction is currently a BLOCKER, not a routine op.** Design alternatives, gated on measured free disk:
- **block-level compaction:** fold ONE completed 90-day time-block at a time into base in place /
  block-by-block, so peak extra space ≈ one block, not a full copy.
- **rolling time-block compaction:** compact oldest-completed block, free its delta span, repeat —
  bounded working set.
- **external/temp storage:** stage the new base on a separate volume, then move — only if a second
  ~2 TB volume is actually available (it is not today).
- **explicit "no safe compaction yet" blocker:** if none of the above fits the free-disk margin, the
  policy is **defer compaction** and let the delta grow (with a delta-size/▾free-disk alarm) until disk
  is provisioned. Compaction must **never** run below a hard free-disk safety margin.
Decision recorded with the binding gate; do not assume full-rebuild compaction is available.

### 6.2 Daily-pruning safety (Codex #4) — prune is irreversible-ish; gate hard
Before pruning ANY daily day (only after the §3.3 VM24 binding gate authorizes demotion):
- **prune manifest:** an append-only record of every pruned day (date, cube/delta location proving
  coverage, checksum/sample digest, timestamp, operator) — so "what was removed and was it safe" is auditable.
- **pre-prune parity:** for each candidate day, **coverage + `var_valid` + sample parity** (N random
  points/cells equal between daily and cube/delta) must pass; fail → do not prune that day.
- **raw NetCDF / redownload recovery:** the source MUR NetCDF is re-downloadable → the canonical
  recovery path is **re-download → re-append to delta**, NOT "restore the daily Zarr". Document the
  redownload window/availability; pruning is only safe where redownload is possible.
- **rollback window:** keep pruned days in a **trash/hold** (or the staging retention window) for a
  defined period before hard delete, so a discovered cube defect is recoverable without redownload.
- **corruption recovery plan:** if the cube is later found corrupt for a pruned span → re-derive from
  re-downloaded NetCDF (primary) or trash/hold (secondary, within the window); the prune manifest
  identifies exactly which spans need re-derivation.
- **daily staging retention window:** keep daily Zarr for **7 / 14 / 30 days** (open §9) for
  re-derivation/spot-checks before the above prune path applies to older days.

## 7. Ops boundary (hard)
**Do NOT touch VM24 production, cron, the daily store, base cube, delta cube, deployment scripts, or
NGINX in this P4 work.** The production cube is working and must not be disturbed. This phase is
**design/spec + local/shadow benchmarks only** until explicitly approved. (Mirrors the standing rule;
restated because P4 reasons about production data that must stay untouched.)

## 8. Step plan
- **P4-S0 (local/shadow — allowed now): FEASIBILITY only.** All-daily-routed-query benchmark
  (bbox + single-day point + POST points) daily vs base-cube vs delta-cube, with parity + the §3.2
  thresholds; cube prototype read paths. Output evidence. **Cannot authorize any pruning.**
- **P4-S0b (VM24 READ-ONLY binding gate — needs explicit approval):** validate the §4.1 policy against
  **real production base+delta** (read-only; no writes/pruning/cron/deploy). Confirm on prod:
  **full-history point/range** ✓, **full-history single-day point GET** ✓, **recent-31-day bbox**
  within budget, **recent-31-day POST /points** within the absolute budget (§3.2), and that
  **older-than-31-day bbox/POST would be rejected** by the policy once implemented. **Only this can
  authorize demoting the daily store** (Codex #1).
- **P4-S1** — finalize the §4.1 policy (A + D + `SPATIAL_WINDOW_DAYS=31`; POST absolute budget) from
  local + binding evidence — orchestrator/Codex sign-off.
- **P4-S2** — raster wire-format spec finalized (served for recent-31-day spatial only).
- **P4-S3** — ingest-policy + compaction-mode + daily-pruning-safety spec (§6).
- **then** P3-S3 frontend contract resumes (recent-31-day spatial + the 4xx window contract).

## 9. Open questions (for orchestrator / Codex)
**Decided (this patch):** policy = Option A + D with `SPATIAL_WINDOW_DAYS = 31` (spatial = bbox + POST,
recent-31-day only, older → 4xx; full history for point/range + single-day point); POST bar is an
absolute recent-window budget, not daily+25%; the single 31-day cutoff has no small/large split; WMS is
not an API-facing substitute. (Old Q "is bbox a hard requirement / thresholds" resolved.)

**Still open:**
1. **`SPATIAL_WINDOW_DAYS` value** — confirm **31** (vs 14 / 30) and align with the staging retention
   window (Q2). Should the two be the same number?
2. **Daily staging retention window:** 7 / 14 / 30 days (≥ `SPATIAL_WINDOW_DAYS`, since recent spatial
   is served from the cube's recent tier regardless)?
3. **CRS / cell_ref** for raster — EPSG:4326, `axis_order=lon,lat`, `cell_ref=center` confirmed?
4. Confirm **re-chunking the base cube is off the table** (would hurt the primary point-series).
5. If Option A: is **removing daily entirely** acceptable eventually, or always keep a rolling staging
   window?
6. **Compaction feasibility (§6.1):** is a second ~2 TB volume provisionable for staged rebuilds, or do
   we commit to block-level/rolling compaction (or "defer compaction" with a delta-growth alarm)?
