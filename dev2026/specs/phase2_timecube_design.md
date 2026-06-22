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

## 4. Candidate data structures — each with a decision gate
Pick by benchmark, not intuition. Build a small **regional subset** (e.g. a few-degree box ×
the available days) first so candidates iterate in seconds, then validate the winner at scale.

| # | candidate | hypothesis | decide by |
|---|---|---|---|
| A | **time-cube** `(time,lat,lon)`, time-contiguous chunk + small spatial chunk + **v3 sharding** | best point-series locality | H1 metrics + append (H2) + files (H3) |
| B | **rechunked time-major Zarr** (moderate `(time, lat, lon)` chunks, no sharding) | simpler; maybe enough | same; compare file count vs A |
| C | **per-variable time-stack** (one array per var, time-major) | isolates sst vs anomaly vs ice | append/read per var |
| D | **spatial-tile + time-chunk hybrid** (coarse spatial tiles, long time) | balance bbox-adjacent reuse | bbox impact + point-series |
| E | **index sidecar / manifest** over existing daily groups (precomputed offsets; no re-store) | avoid data conversion entirely | does it actually cut O(T) open cost? likely NO (still per-day reads) — cheap to disprove first |

**Decision rules**:
- Run **E first** (cheapest): if a manifest/offset index alone meets the gate, no re-store needed. VM24 evidence suggests it won't (the cost is per-day reads, not just opens), but disprove it cheaply.
- Among A–D, pick the one that **passes §5 point-series gate with the lowest append cost + file count**, and **does not regress bbox** (if a candidate also serves bbox).
- **bbox protection**: if the chosen time store hurts bbox vs the daily store, **keep bbox on the daily store** (hybrid §2). The spec must state the final routing per pattern with benchmark numbers.

## 5. LOCAL GATE (must pass BEFORE any VM24 work)
> The VM24 No-go proved local functional/small benchmarks are insufficient. **Local gate is
> not the production gate, but it must prove the architecture direction works** under
> realistic critical-path load. If local fails, do NOT push to VM24.

Thresholds (per the Phase-2 task requirement), measured via `bench/loadtest.py` (+ the
micro-cost bench), machine-readable JSON:
- **365-day point series, C=1**: **p95 < 4 s** (target p50 < 2.5 s).
- **365-day LR C=4**: p95 < **2× baseline**; **C=8** < **3× baseline**; **C=16**: no timeout, no OOM, may 503-shed, served p95 < **4× baseline**.
- **730-day point series**: report; should remain bounded (target similar multiples of its own C=1 baseline).
- **RSS not monotonically growing**; within ceiling.
- Workloads required: single-point 365-day & 730-day; LR C=1/4/8/16; SB-short; SB range-heavy; BBOX near limit; overload/backpressure.
- Artifacts: same JSON schema as `loadtest.py` (windowed stability incl.), so local and VM24 runs diff directly.

> Caveat baked in: the local store is **partial / warm**. To reduce the local↔VM24 gap, the
> local gate run MUST (a) use the largest contiguous local span available, (b) **drop OS page
> cache before the run where possible** (or report cache state), and (c) report `chunk_count_
> per_query` + `decompressed_bytes` from the micro-bench — these are cache-independent and are
> the real architectural signal. A pass on cache-independent metrics + warm latency is the
> minimum bar to spend VM24 time.

## 6. Per-step plan — every step ships code + benchmark + gate
| step | deliverable | benchmark + gate (local) |
|---|---|---|
| **P2-S1** | candidate-E manifest/offset probe over daily store | micro-bench: does it cut O(T)? gate: meet §5 365-day or **reject E** |
| **P2-S2** | `dev2026/ingest/` time-store converter (regional subset first), v3 sharding | conversion correctness (parity vs daily) + files/day (H3) |
| **P2-S3** | `bench/bench_timecube_microcost.py` (chunk_count / decompressed_bytes / read_ampl / py_alloc / rss per query) | produce the H1 table for each chunking candidate; pick chunking by gate |
| **P2-S4** | `store/time_store.py` access layer (point_series/range via time store) | **parity** vs P1 `point_series` (exact values) + thread-safety stress (reuse P1 patterns) |
| **P2-S5** | hybrid router in `store`/`api` (route by pattern) | routing unit tests + **full parity across all routes** vs old/P1 |
| **P2-S6** | daily-append into the time store wired into the ingest path | append micro-bench (H2): cost/day, no rewrite of existing, new-day visibility |
| **P2-S7** | full local load gate (`loadtest.py` all scenarios incl 365/730-day) | **§5 LOCAL GATE — must pass to proceed** |
| **P2-S8** | VM24 shadow + binding gate (Codex/ops) + cutover discussion | runbook (extend `p1s5_s6_runbook.md`); VM24 binding only after S7 |

## 7. Promotion checklist (gate to advance; no skipping)
1. code complete (the step's module)
2. data conversion complete (where applicable) + correctness parity vs daily store
3. local functional parity pass (values identical to old/P1 for the routed pattern)
4. **local critical-path gate pass** (§5 latency/concurrency + micro-cost cache-independent metrics)
5. local load/RSS gate pass (`loadtest.py` all scenarios, JSON artifact)
6. **only then** → VM24 shadow deployment + binding gate (Codex/ops)
7. **only after VM24 binding pass** → NGINX cutover discussion

## 8. Hybrid routing — correctness & ingest contract
- Router maps (has bbox? / #days / single-day?) → store; documented + unit-tested; default safe (unknown → daily store).
- **Dual-write ingest**: the daily pipeline keeps writing daily groups AND appends to the time store; both must stay consistent (a parity check in CI/local). Storage roughly doubles for the time-served variables — accepted; quantify in S2.
- live-store tolerance + new-day visibility carried over from P1 (filesystem rescan / append visibility test).

## 9. Risks & mitigations
| risk | mitigation |
|---|---|
| storage doubling (two stores) | only duplicate time-served vars; quantify in S2; revisit if prohibitive |
| small-chunk file explosion | Zarr v3 sharding (candidate A); measure files/day (H3) |
| time_chunk vs append trade-off | H2 append bench drives the choice |
| read amplification from large chunks | H1 read_ampl gated; tune spatial chunk |
| local↔VM24 gap recurs | §5 cache-independent metrics + drop-cache + largest-span discipline |
| time store helps point but hurts bbox | hybrid routing keeps bbox on daily store (§2/§4) |

## 10. Open questions for reviewer
1. 730-day a hard target now, or just "bounded / report"? (sets `time_chunk` & gate.)
2. Storage budget on VM24 for the duplicated time store (drives keep-all-vars vs sst-only).
3. Is candidate-E (manifest-only, no re-store) worth a first attempt, or skip straight to a time-cube given the VM24 evidence?
4. Does `POST /points` ever need multi-day batch? (would pull it toward the time store.)
