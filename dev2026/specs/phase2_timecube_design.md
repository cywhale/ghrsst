# Phase-2 design — time-optimized store + hybrid routing (dev2026)

> Status: **design spec for independent review** (not implementation). Trigger:
> [`vm24_p1_binding_results.md`](vm24_p1_binding_results.md) (VM24 No-go: 365-day point
> series p95 14 s vs 4 s target). Builds on P1 (`store/store_access.py`, `api/app.py`,
> `bench/loadtest.py`). Production untouched throughout; no NGINX cutover until a VM24
> binding gate passes — and that only after the **local gate** below passes.

---

## 0. Problem restated (from VM24 evidence)
- Root failure = **time fan-out**: a `T`-day point series costs **O(T)** day-group opens +
  O(T) per-day chunk decompress. P1 removed the per-request metadata re-parse but cannot
  remove the O(T) fan-out → 14 s p95 at T=365 on full store.
- **Not** failing: bbox / single-day grid, batch single-day, RSS (≤184 MB/worker).
- ⇒ Fix the time axis only; **do not re-architect what already works.**

## 1. Goal / non-goals
**Goal**: 365-day (and 730-day) point/time-series fast enough to pass the local gate (§5)
then the VM24 binding gate, via a **time-optimized store** for the time axis.

**Non-goals**: re-architecting bbox/single-day grid or batch single-day (P1/old store stays);
touching production; any NGINX/cutover work before gates pass; a 5th execution mode, etc.

## 2. Initial architecture assumption — HYBRID ROUTING (evidence-backed)
P1 proved bbox/single-day are fine; only time fan-out failed. So **do not force all query
patterns through a new store**:

| query pattern | store (initial) | rationale |
|---|---|---|
| point/range **time-series** (multi-day) | **new time-optimized store** | the only failing path |
| bbox / single-day grid | **existing daily-group store** (P1 path) | not a bottleneck; already streams fine |
| `POST /points` single-day batch | **daily-group store** (P1 path) | single-day; benchmark must prove a time store helps before moving it |
| single-day point | **daily-group store** (or either) | trivial; route to whichever benchmarks better |

A **router** in the access/app layer classifies each request and dispatches. Routing must be
explicit and unit-tested. If a candidate time store later benchmarks better for single-day
batch too, move it **only on benchmark evidence** (§4 decision gates), not intuition.

## 3. Critical path as TESTABLE hypotheses (estimate AND measure)
Each hypothesis must be (a) estimated up front, (b) measured by a committed micro-benchmark
(`bench/bench_timecube_microcost.py`, JSON out), and (c) gated.

**H1 — complexity**: a time store changes a `T`-day point series from
`O(T) group-opens + O(T) chunk-decompress` → `O(ceil(T / time_chunk)) chunk reads`.
For T=365 with `time_chunk≈365`: **1–2 chunk reads** vs **365** group opens.

**Per-query metrics to estimate + measure (point series, T∈{365,730}, single point):**
| metric | meaning | gate intent |
|---|---|---|
| chunk_count_per_query | # inner chunks decompressed | should be O(T/time_chunk), single-digit for T=365 |
| decompressed_bytes_per_query | bytes inflated to answer the query | watch **read amplification** (see below) |
| read_amplification | decompressed_bytes / useful_bytes (T×4×nfields) | smaller spatial chunk ↓ amplification; trade vs file count |
| py_alloc_peak | tracemalloc peak | bounded, not O(T) Python objects |
| rss_peak | process RSS under load | ≤ ceiling, no monotonic growth |
| p50/p95/p99 latency | end-to-end | meet §5 gate |
| concurrency_degradation | p95(C) / p95(C=1) curve | sub-linear; meet §5 LR multipliers |

> Example amplification: time_chunk=365, spatial 8×8, f32 → one chunk = 365·8·8·4 ≈ 93 KB to
> return 365·4 = 1460 B (point) ⇒ ~64× amplification but **one** decompress (~ms). 32×32 ⇒
> ~1000× — likely too wasteful. The benchmark picks the spatial chunk; do not assume.

**H2 — daily append cost**: appending one day must NOT rewrite existing data and must stay
cheap; `time_chunk` size trades append cost vs read (small time_chunk → cheap append, more
read chunks; large → opposite). Measured by an append micro-benchmark.

**H3 — global file count**: small spatial chunks explode file count for a global daily store
→ **Zarr v3 sharding** packs many inner chunks per shard. Measure files/day and total.

**H4 — no-rewrite ceiling (E1/F)**: `chunk_count_per_query` is the discriminator. A time store
(Tier 2) makes it **O(T/time_chunk)** (single-digit for T=365); E1/F leave it **O(T)** (≈365)
and can only change *wall time* (parallelism) or *open overhead* (manifest). So the H1 table is
run for E1/F too, but their gate is purely empirical — do they hit §5 *despite* O(T) reads? If
not, the cache-independent `chunk_count`/`decompressed_bytes` evidence explains why and justifies
the rewrite.

## 4. Candidate data structures — risk-ordered, each with a decision gate
**Best-solution principle (review-driven): try the lowest-operational-risk approach that can
pass the §5 local gate before committing to a data rewrite.** A no-rewrite win avoids dual-write
ingest, storage doubling, and conversion/backfill risk — so it is *preferred if it passes*, even
if a time-cube is faster. Evaluate strictly in this order; stop at the first that passes the gate.

Build the §5 contiguous fixture first so every candidate iterates against the SAME full-time data.

### Tier 1 — NO data rewrite (lowest operational risk; evaluate first)
| # | candidate | hypothesis | decide by |
|---|---|---|---|
| **E1** | **manifest / index sidecar** over daily groups (precomputed day→path/offset map; no re-store) | cuts per-day *open* overhead only | H1 metrics; **likely rejected** — P1 already opens directly, the cost is per-day *reads* not opens. Cheap to disprove. |
| **F** | **existing daily store + smarter read engine** (no rewrite): xarray/dask **virtual concat** over daily groups; **bounded-concurrency parallel per-day reads**; zarr v3 async/concurrent reads where available; consolidated-metadata / metadata caching; optional **kerchunk/reference** virtual dataset | **parallelizes** O(days), does not *remove* it | full H1 metrics (§3) incl. **concurrency curve + RSS** |

**F ceiling (state honestly, design the bench to catch it):** F's best case is `O(days)` reads done
in parallel. Whether that meets the gate depends on **CPU-bound vs I/O-bound**:
- If VM24's 14 s was dominated by **decompress (CPU)** → parallel reads across cores can cut C=1
  wall time a lot (e.g. 14 s / 8 ≈ 1.75 s) and F may pass C=1.
- But under **LR concurrency** the cores are already busy with other requests, so per-request
  parallelism collapses → F likely **fails the LR multipliers** even if it passes C=1.
- And parallel in-flight day-chunks **multiply RSS** (was not a P1 blocker because reads were
  sequential) → F's RSS gate must be watched.
- If I/O-bound (cold-disk seeks) → parallelism helps less; reference/kerchunk reduces open
  overhead but not the O(days) byte reads.
**F concurrency must be coordinated with the API executor/backpressure (not a second unbounded
pool):** any Dask/xarray/zarr parallelism F uses **shares one global thread/worker budget** with
the P1 `BoundedExecutor` (else `-w gunicorn × per-request dask threads × concurrent requests`
oversubscribes cores and explodes RSS/threads). The F benchmark MUST report:
- scheduler workers/threads (dask pool size), **in-flight day-chunks per request**, and **total
  process threads** under each C.

**F decision gate:** adopt F (skip Tier 2) **only if** it hits the §5 local gate **on the
real-data fixture (B)** with **bounded concurrency** — i.e. LR multipliers + RSS bounded +
cache-independent metrics + **no core oversubscription / no thread or RSS growth**. **Reject F if**
it only passes C=1, fails LR/RSS, passes *only* by oversubscribing local cores, or shows thread/RSS
growth — then proceed to Tier 2. A synthetic-only (Fixture A) pass does not authorize adoption.

### Tier 2 — data rewrite (structural fix; only if Tier 1 fails the gate)
| # | candidate | hypothesis | decide by |
|---|---|---|---|
| A | **time-cube** `(time,lat,lon)`, time-contiguous chunk + small spatial chunk + **v3 sharding** | removes O(days): point series → O(T/time_chunk) chunk reads | H1 + append (H2) + files (H3) |
| B | **rechunked time-major Zarr** (moderate `(time,lat,lon)` chunks, no sharding) | simpler; maybe enough | same; file count vs A |
| C | **per-variable time-stack** (one array per var, time-major) | isolates sst/anomaly/ice | append/read per var |
| D | **spatial-tile + time-chunk hybrid** (coarse spatial tiles, long time) | balance bbox reuse | bbox impact + point series |

**Tier-2 decision rules**:
- Pick the candidate that **passes §5 with the lowest append cost (H2) + file count (H3)** and **does not regress bbox**.
- **bbox protection / hybrid routing**: if the chosen time store hurts bbox vs the daily store, **keep bbox on the daily store** (§2). Final spec states routing per pattern with benchmark numbers.
- Expectation (review + VM24 evidence): the structural fix is most likely **A** (time-cube), but this is decided by benchmark, not assumed.

## 5. LOCAL GATE (must pass BEFORE any VM24 work)
> The VM24 No-go proved local functional/small benchmarks are insufficient. **Local gate is
> not the production gate, but it must prove the architecture direction works** under
> realistic critical-path load. If local fails, do NOT push to VM24.

### 5.1 Required fixtures — TWO kinds (synthetic ≠ promotion gate)
The VM24 No-go happened because the local P1 "365-day" run used a **warm, ~260-day partial**
store. Phase-2 forbids relying on synthetic/partial data for promotion. **Two fixtures**:

- **Fixture A — synthetic/regional (pre-gate, CI, candidate comparison)**: a small lat/lon box ×
  **≥365 (pref 730) contiguous days**, daily-group format, real days where available + **synthesized
  realistic-compressibility fill** for the rest (`dev2026/ingest/build_fixture.py`). Use for fast
  repeatable architecture-timing and apples-to-apples candidate comparison.
  **⚠ Synthetic data can hide real compression / chunk / decompression behavior** — so a pass on
  Fixture A **alone CANNOT authorize the VM24 binding gate** (it would repeat the P1 local→VM24
  optimism trap).

- **Fixture B — real-data (promotion gate)**: benchmark on the **largest REAL contiguous regional
  span available** locally (no synthetic fill). **Promotion to VM24 requires a real-data (Fixture B)
  artifact.**
  - If local has **≥365 contiguous real days** → Fixture B is the real local gate; a pass authorizes
    the VM24 binding gate.
  - If local **cannot provide 365 contiguous real days** → the local gate is labelled **INCOMPLETE**;
    this permits only **VM24 exploratory shadow** runs, **NOT cutover eligibility**. Cutover stays
    blocked until a real-data (≥365 contiguous) gate passes (on a richer local copy or on VM24 itself).

- Both fixtures: record day count + contiguity + real-vs-synthetic composition + cache state in the
  artifact `meta`. For Tier-2, convert the SAME fixture to the time store (apples-to-apples).
- **A local pass is INVALID** if it used < 365 contiguous days, a warm partial store, or Fixture A
  only (for promotion).

### 5.2 Thresholds
Measured via `bench/loadtest.py` (+ micro-cost bench), machine-readable JSON; **365-day = HARD
gate, 730-day = REPORT target** (per orchestrator decision):
- **365-day point series, C=1 (HARD)**: **p95 < 4 s** (target p50 < 2.5 s).
- **365-day LR C=4**: p95 < **2× baseline**; **C=8** < **3× baseline**; **C=16**: no timeout, no OOM, may 503-shed, served p95 < **4× baseline**.
- **730-day point series (REPORT)**: report C=1 + LR; bounded vs its own C=1 baseline (not a hard fail).
- **RSS not monotonically growing**; within ceiling (watch F especially — parallel reads inflate RSS).
- Workloads required: single-point 365-day & 730-day; LR C=1/4/8/16; SB-short; SB range-heavy; BBOX near limit; overload/backpressure.
- Artifacts: same JSON schema as `loadtest.py` (windowed stability incl.) + the micro-cost JSON; `meta` records fixture span/contiguity + cache state.

### 5.3 Cache discipline
- **Drop OS page cache before each gate run** where possible (`vmtouch -e` / `echo 3 >
  /proc/sys/vm/drop_caches` on Linux; document if not possible on the dev Mac) **or report cache
  state**; report both cold and warm where feasible.
- Always report the **cache-independent** signals (`chunk_count_per_query`, `decompressed_bytes`)
  from the micro-bench — these are the real architectural discriminator and don't depend on
  cache warmth. A pass on cache-independent metrics + cold-ish latency is the minimum bar to
  spend VM24 time.

## 6. Per-step plan — every step ships code + benchmark + gate
Tier-1 (no rewrite) is evaluated FIRST; Tier-2 (time store) only starts if Tier-1 fails the gate.
| step | deliverable | benchmark + gate (local) |
|---|---|---|
| **P2-S0a** | `dev2026/ingest/build_fixture.py` — **synthetic/regional** fixture (Fixture A): ≥365 (pref 730) contiguous days, real+synth fill + `bench/bench_timecube_microcost.py` (chunk_count / decompressed_bytes / read_ampl / py_alloc / rss per query) | pre-gate/CI; ≥365 contiguous (meta); micro-bench = O(days) daily-store baseline. **Synthetic pass ≠ promotion.** |
| **P2-S0b** | **real-data** benchmark fixture (Fixture B): largest REAL contiguous regional span available (no synth fill) | the **promotion gate** input; if <365 contiguous real days → local gate **INCOMPLETE** (shadow-only, no cutover eligibility) |
| **P2-S1 (Tier 1)** | **E1** manifest/offset sidecar over daily groups | micro-bench: does it cut O(T)? **gate: meet §5 365-day or reject E1** |
| **P2-S2 (Tier 1)** | **F** smarter read engine (dask/xarray virtual concat, **bounded** parallel per-day reads coordinated with the API executor, zarr async, optional kerchunk) over the daily store | full §5 gate on **Fixture B** incl. **LR multipliers + RSS + thread/in-flight-chunk report**; **adopt F only if it passes with bounded concurrency (no oversubscription/RSS/thread growth) → skip S3–S6.** Else → Tier 2. |
| **P2-S3 (Tier 2)** | time-store converter (subset first), v3 sharding; produce H1 table per chunking candidate (A–D) | pick chunking by gate (read_ampl/latency/append/files) |
| **P2-S4 (Tier 2)** | `store/time_store.py` access layer (point_series/range via time store) | **parity** vs P1 `point_series` (exact values) + thread-safety stress (reuse P1 patterns) |
| **P2-S5 (Tier 2)** | hybrid router in `store`/`api` (route by pattern) | routing unit tests + **full parity across all routes** vs old/P1 |
| **P2-S6 (Tier 2)** | daily-append (dual-write) into the time store in the ingest path | append micro-bench (H2): cost/day, no rewrite of existing, new-day visibility |
| **P2-S7** | full local load gate (`loadtest.py` all scenarios incl 365/730-day) on the adopted approach (F or time store) | **§5 LOCAL GATE — must pass to proceed** |
| **P2-S8** | VM24 shadow + binding gate (Codex/ops) + cutover discussion | extend `p1s5_s6_runbook.md`; VM24 binding only after S7 |

## 7. Promotion checklist (gate to advance; no skipping)
1. code complete (the step's module)
2. data conversion complete (where applicable) + correctness parity vs daily store
3. local functional parity pass (values identical to old/P1 for the routed pattern)
4. **local critical-path gate pass** (§5 latency/concurrency + micro-cost cache-independent metrics)
5. local load/RSS gate pass (`loadtest.py` all scenarios, JSON artifact)
6. **real-data (Fixture B) artifact pass** — synthetic-only (Fixture A) does NOT satisfy this. If local lacks ≥365 contiguous real days, gate = INCOMPLETE → VM24 **exploratory shadow only**, not cutover-eligible.
7. **only then** → VM24 shadow deployment + binding gate (Codex/ops)
8. **only after VM24 binding pass** → NGINX cutover discussion

## 8. Hybrid routing — correctness & ingest contract
- Router maps (has bbox? / #days / single-day?) → store; documented + unit-tested; default safe (unknown → daily store). `POST /points` stays **single-day** on the daily store (no multi-day in Phase-2 scope).
- **Dual-write ingest applies ONLY if Tier-2 (a time store) is adopted** — F (Tier-1) needs no second store, no dual-write, no extra storage (its main appeal).
- If Tier-2: the daily pipeline keeps writing daily groups AND appends to the time store; both stay consistent (parity check in CI/local). **Keep all three time-series vars `sst`, `sst_anomaly`, `sea_ice`** in the time store (dropping vars would break append/parity); storage ~doubles for those vars — accepted initially, quantify in S3, phase by benchmark only if VM24 capacity is tight.
- live-store tolerance + new-day visibility carried over from P1 (filesystem rescan / append visibility test).

## 9. Risks & mitigations
| risk | mitigation |
|---|---|
| storage doubling (two stores, Tier-2 only) | only duplicate time-served vars; quantify in S3 (Tier-2 conversion); revisit if prohibitive |
| small-chunk file explosion | Zarr v3 sharding (candidate A); measure files/day (H3) |
| time_chunk vs append trade-off | H2 append bench drives the choice |
| read amplification from large chunks | H1 read_ampl gated; tune spatial chunk |
| local↔VM24 gap recurs | §5 cache-independent metrics + drop-cache + largest-span discipline |
| time store helps point but hurts bbox | hybrid routing keeps bbox on daily store (§2/§4) |

## 10. Open questions — RESOLVED (orchestrator, 2026-06)
1. **730-day** → **report target, not a hard gate**; **365-day is the hard gate**. (§5.2)
2. **Storage** → keep **`sst`, `sst_anomaly`, `sea_ice`** (3 vars) in the time store initially (dropping breaks append parity); if capacity tight, phase by benchmark. (§8)
3. **Candidate E first?** → **Yes, but expanded to E1 (manifest) + F (dask/xarray/zarr smarter-read)** as the Tier-1 no-rewrite ladder, evaluated before any time-cube. (§4 Tier 1, §6 S1–S2)
4. **`POST /points` multi-day?** → **No — out of Phase-2 scope; stays single-day** on the daily store. (§2, §8)

5. **Tier-1→Tier-2 ladder + F adopt-if-passes** → confirmed correct. **F adopt only if it passes
   with bounded concurrency AND on the real-data Fixture B** (not synthetic). (§4 F gate, §6 S2)
6. **Synthesized-fill fixture** → **acceptable as PRE-gate / CI only (Fixture A); NOT the final
   local promotion gate**. Promotion needs the real-data Fixture B; synthetic-only ⇒ shadow-only,
   no cutover eligibility. (§5.1, §7)
