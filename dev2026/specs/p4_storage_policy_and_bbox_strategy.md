# P4 — storage policy & bbox strategy (can the system be time-cube authoritative?)

Status: **DRAFT — round 3: policy adopted (P4-S0 accepted)** (Claude authored; Codex reviewed 3×).
P4-S0 local feasibility is accepted; the adopted policy is **Option A + D with
`SPATIAL_WINDOW_DAYS = 31`** (§4.1), pending P4-S1 sign-off + the P4-S0b VM24 read-only binding gate.
The P3-S1 compact-bbox work is prototype/evidence ([`p3s1_bbox_format.md`](p3s1_bbox_format.md)); the
**frontend contract (P3-S3) waits for the policy sign-off.**

> Round-3 patch: **single spatial rule** — bbox + POST /points served only for the latest 31 days,
> older → clear 4xx (§4.1); **no small/large historical split; no WMS as an API substitute**; **POST bar
> revised to an absolute recent-window budget** (warm p95 < 100 ms, C8 < ~1 s) since POST is delta-only
> (§3.2); historical base results kept as the **rationale for the cutoff**, not a path to optimize.
> Round-4 patch: spatial window is tied to the **bbox-friendly recent tier (delta `t1/s256`), NOT
> "newest base blocks"** (a newest base block is still `t90/s8` = bbox-hostile) (§4.1); **delta
> retention invariant** (keep ≥31 days; compaction folds only days older than the window into base,
> §6.1); daily is **no longer full-history authoritative** — ingest target flow + staging retention
> aligned to 31 (§6); P4-S0b caveat: prod delta has only ~2 days so it can't validate full 31-day
> retention — use staging/shadow or an explicitly-approved backfill, never mutate prod under the
> read-only gate (§8).

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
| **recent** single-day **bbox** warm p95 (≤ medium) | **< ~200 ms**; C8 bounded  · VM24: 24 ms / 178 ms C8 ✅ |
| **recent** **POST /points** warm p95 | **< 100 ms** ✅ VM24 ~88 ms; **C8 p95 < ~1 s** ⚠ VM24 ~1.2–1.3 s (see below) |
| full-history single-day **point** GET | **≤ daily-path p95 + 25 %** (or absolute < ~50 ms) · VM24: 4.5 ms ✅ |
| **RSS** under C=8 | **≤ `GHRSST_RSS_CEILING_MB`** (prod 4096), no OOM, no unbounded growth |
| **concurrency** C=4 / C=8 | bounded degradation, zero 5xx/OOM |
POST is an absolute budget (not daily+25%) because it is served from the delta/recent tier only; daily's
~15 ms is an artifact of its single-`(1,1024,1024)`-chunk layout, not a meaningful floor. **Historical
(base) bbox / scattered POST are NOT a threshold to pass** — their ~90× read amp is precisely the
**rationale for the 31-day spatial cutoff (§4.1)**; the base layout is read-optimal for the primary
point-series and is **not** a path we intend to optimize.

> **POST C8 budget — DECISION PENDING (P4-S0b VM24, `p4s0b_vm24_results.md`).** Delta POST warm p95 is
> ~88 ms (well inside 100 ms) but **C8 p95 ~1.2–1.3 s exceeds the ~1 s bar** → `OVERALL_per_day_delta`
> failed on that alone. P4-S1 must **either optimize delta POST at C=8, or relax the POST C8 budget to
> ~1.5 s** (recommended — POST /points at 8-way concurrency is a heavier, less-common call; 1.3 s is
> operationally reasonable and warm p95 is fine). bbox / point / range all passed on real prod data.

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
- **Spatial queries = bbox + POST /points.** Both are supported **only for days present in the
  bbox-friendly recent tier (the delta cube, `t1/s256`)** — currently the latest
  `SPATIAL_WINDOW_DAYS = 31` days (Codex round-4). **NOT** served from base blocks: even the *newest*
  base block is `t90/s8` and carries the same ~90× read amplification (§2), so "recent base blocks" are
  bbox-hostile just like historical ones. **Older spatial queries return a clear 4xx** naming the
  available spatial window (e.g. "spatial queries limited to the latest 31 days; available window
  <start>..<end>").
- **Delta retention invariant (spatial-serving guarantee):**
  - the delta must **retain at least `SPATIAL_WINDOW_DAYS` (31) days** of the bbox-friendly recent tier;
  - **compaction may fold ONLY days older than the spatial window into base**; it must **not** remove the
    latest 31 days from delta **unless an equivalent bbox-friendly recent tier replaces them**;
  - so the spatial window and the delta's bbox-friendly span are the SAME thing — bbox/POST availability
    == delta membership.
- **Single rule, no exceptions:** do **not** split historical bbox into small/large cases — the API rule
  stays one date cutoff for all spatial requests. (Rationale: base `s8/t90` read amplification, §2/§3.2.)
- **Full-history is retained** for **point time-series / range** and **single-day point GET** (cheap
  from the cube at any age — base `t90/s8` is read-optimal for point series; P4-S0: point ≈ 2–3 ms).
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
store**; **daily Zarr is NO LONGER full-history authoritative once P4 is adopted** — it exists only as a
bounded staging/re-derivation buffer.

**Target daily flow (Codex round-4):**
`NetCDF download → append to delta → validate → (optional) daily staging write → prune daily staging
after retention`. The delta (bbox-friendly recent tier) + base (point-series history) are authoritative;
daily is transient.
- **download** NetCDF (MUR) → **append to delta** via the tiled `append_to_delta` (P3 append-opt layout).
- **validation:** coverage check + per-day/var presence + sanity (range/NaN-fraction) before commit.
- **retry / resume:** checkpointed (existing per-(day,var,tile) checkpoint); idempotent re-run.
- **duplicate / overwrite policy:** idempotent upsert (re-append same day = overwrite, count stable).
- **latest metadata:** delta `days`/`latest` advance atomically; `/healthz` reflects tier latest —
  **but the SERVING process must reload to see it (§6.3).**
- **rollback:** keep the prior delta (and a base snapshot pointer) until the new day validates; revert
  by pointer swap.
### 6.1 Compaction under the CURRENT disk reality (Codex #3) + spatial-window constraint
**Compaction may fold ONLY days older than `SPATIAL_WINDOW_DAYS` (31) into base** — the delta must always
keep the latest 31 days as the bbox-friendly recent tier (§4.1 retention invariant); folding a recent day
into base would silently break bbox/POST for that day (base is `t90/s8`, bbox-hostile). So compaction
lags the window by ≥31 days.
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
- **daily staging retention window:** initial retention is **31 days, aligned with
  `SPATIAL_WINDOW_DAYS`** (keep daily Zarr for the latest 31 days for re-derivation/spot-checks, prune
  older). It **may be extended** later, but **must not be shorter than `SPATIAL_WINDOW_DAYS` unless
  NetCDF redownload is explicitly accepted as the recovery path** — so any spatially-served (delta) day
  is also re-derivable from staging (or from redownload if the window is deliberately shortened).

### 6.3 Serving-side metadata reload after delta append (P4-S0b finding — REQUIRED for authoritative mode)
P4-S0b found the running API's cube metadata is **stale after a delta append**: the API opens
`TieredCube`/`TimeCubeStore` ONCE at lifespan and caches `delta.days`/`latest`, so a newly-appended delta
day is **invisible until the process reloads** (VM24: disk delta had `2026-06-29` while `/healthz`
`delta_latest = 2026-06-28`; a range ending `06-29` fell back to **daily**). Harmless today (daily is
still authoritative), but a **blocker for time-cube-authoritative mode** — with daily demoted, an
un-reloaded API would mis-serve / 4xx the newest day. **P4-S3 must include a reload strategy:**
- **short-term (ops):** after a successful delta append, **restart the `ghrsst` PM2 process**
  (`pm2 restart ghrsst`); simplest, already in the deploy runbook.
- **better (code):** a **metadata refresh / TTL-based reopen** of `TieredCube`/`TimeCubeStore` (re-read
  `delta.attrs['days']`/`latest` on a short TTL or on an explicit refresh signal) so new delta days
  become visible **without a restart**. Must stay read-only + concurrency-safe (the stores hold a lock).
Either way the daily-append cron and the reload must be ordered: **append → validate → reload** before
the new day is advertised as cube-served.

## 7. Ops boundary (hard)
**Do NOT touch VM24 production, cron, the daily store, base cube, delta cube, deployment scripts, or
NGINX in this P4 work.** The production cube is working and must not be disturbed. This phase is
**design/spec + local/shadow benchmarks only** until explicitly approved. (Mirrors the standing rule;
restated because P4 reasons about production data that must stay untouched.)

## 8. Step plan
- **P4-S0 (local/shadow — allowed now): FEASIBILITY only.** All-daily-routed-query benchmark
  (bbox + single-day point + POST points) daily vs base-cube vs delta-cube, with parity + the §3.2
  thresholds; cube prototype read paths. Output evidence. **Cannot authorize any pruning.**
- **P4-S0b (VM24 READ-ONLY binding gate) — RAN 2026-07-01 (Codex/ops), PARTIAL PASS.** Results:
  `p4s0b_vm24_results.md`. Full-history point/range ✓ (26 ms), single-day point ✓ (4.5 ms), delta bbox ✓
  (24 ms / 178 ms C8), delta POST warm ✓ (~88 ms) + parity ✓ + policy-reject ✓, but **delta POST C8
  ~1.2–1.3 s > ~1 s → `OVERALL_per_day_delta = false`** (POST C8 only). Surfaced two findings: harness
  fan-out bug (fixed) and **stale delta metadata after append** (§6.3).
  - **Caveat (Codex round-4):** production delta held only **~3 days** (2026-06-27..29) at run time, so
    P4-S0b validated **per-day delta performance** but **NOT the full 31-day retention policy** —
    there aren't 31 delta days to serve yet.
  - **To test the full 31-day window** without mutating production: use a **staging/shadow delta** (build
    31 recent days into a shadow delta and benchmark it) OR an **explicitly-approved prep step** that
    backfills recent days into delta. **Do NOT backfill/mutate production delta under the read-only
    gate** — that is a separate, explicitly-approved ingest action, not part of P4-S0b.
  - **Only P4-S0b (+ the retention state) can authorize demoting the daily store** (Codex #1).
- **P4-S1** — finalize the §4.1 policy (A + D + `SPATIAL_WINDOW_DAYS=31`; POST absolute budget) from
  local + binding evidence — orchestrator/Codex sign-off.
- **P4-S2** — raster wire-format spec finalized (served for recent-31-day spatial only).
- **P4-S3** — ingest-policy + compaction-mode + daily-pruning-safety spec (§6).
- **then** P3-S3 frontend contract resumes (recent-31-day spatial + the 4xx window contract).

## 9. Open questions (for orchestrator / Codex)
**Decided:** policy = Option A + D with **`SPATIAL_WINDOW_DAYS = 31`** (spatial = bbox + POST, served
from the delta tier / recent-31-day only, older → 4xx; full history for point/range + single-day point);
POST bar is an absolute recent-window budget, not daily+25%; single 31-day cutoff, no small/large split;
WMS is not an API-facing substitute. **`SPATIAL_WINDOW_DAYS = 31` is the adopted default** (not an open
14/30 choice) unless product later reopens it. **Daily staging retention = 31 days, aligned with
`SPATIAL_WINDOW_DAYS`** (§6.2): may be **extended** later, but **must not be shorter unless NetCDF
redownload is explicitly accepted as the recovery path**.

**Still open:**
1. **Delta POST C8 budget (P4-S0b, §3.2):** VM24 delta POST C8 ~1.2–1.3 s > ~1 s. **Relax C8 budget to
   ~1.5 s** (recommended) **or optimize** delta POST at C=8? — P4-S1 decides. (warm p95 ~88 ms is fine.)
2. **CRS / cell_ref** for raster — EPSG:4326, `axis_order=lon,lat`, `cell_ref=center` confirmed?
3. Confirm **re-chunking the base cube is off the table** (would hurt the primary point-series).
4. If Option A: is **removing daily staging entirely** eventually acceptable (relying on NetCDF
   redownload for recovery), or always keep the rolling 31-day staging window?
5. **Compaction feasibility (§6.1):** is a second ~2 TB volume provisionable for staged rebuilds, or do
   we commit to block-level/rolling compaction (or "defer compaction" with a delta-growth alarm)?
6. **Reload strategy (§6.3):** short-term PM2 restart-after-append, or ship the TTL/refresh code path in
   P4-S3? (Required before authoritative mode — a stale API mis-serves the newest delta day.)
