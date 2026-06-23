# P2-S2 — Candidate F (bounded-parallel read engine) — result: REJECT for gate → Tier-2

> Tier-1 no-rewrite candidate (spec §4). Goal: does bounded parallel reading of the daily
> store's per-day chunks pass the local gate without a rewrite? **No.** F is safe and a strict
> single-request improvement, but it does not remove the O(days) total-work limit and fails the
> LR concurrency multiplier. Tools: `store/f_engine.py`, `bench/bench_f_engine.py`,
> `tests/test_phase2_s2.py` (4/4). Thread env pinned (OMP/OPENBLAS/MKL/NUMEXPR=1).

## Resource hard-gates — PASS (implementation is safe)
- **ONE global bounded pool**, shared across all requests (no per-request scheduler/pool, no
  unbounded Dask). `in_flight_day_chunks_peak == parallelism` exactly (1/2/4/8) **regardless of
  request concurrency** → max concurrent chunk reads globally is bounded.
- **RSS bounded** (~210–299 MB) and **does NOT grow with concurrency** (bounded by parallelism,
  not C).
- thread env pinned; the engine pool is fixed at `parallelism`. (Note: `threads_peak` in the
  artifact grows with C because the BENCH's load generator uses C client threads — that is not
  the engine oversubscribing; the bounded metric is `in_flight_chunks_peak == parallelism`.)
- parity 4/4: F == P1 exactly (incl. absent-field omit, day order).

## Latency curve — 96-day span, warm (full table in f_real.json)
**The Phase-2 LR gate is p95-based**, so p95 is primary below (p50 in parentheses for context):
| profile | C=1 p95 (p50) | C=4 | C=8 | C=16 | in_flight | rss_peak |
|---|---|---|---|---|---|---|
| baseline (P1 seq) | 562 (491) | 657 (546) | 733 (694) | 1340 (1287) | 1 | 288 |
| F P=1 | 420 (380) | 1464 (1339) | 2957 (2867) | 6177 (5563) | 1 | 288 |
| F P=2 | 230 (205) | 829 (766) | 1654 (1554) | 3193 (3039) | 2 | 213 |
| F P=4 | 130 (115) | 452 (420) | 915 (845) | 1755 (1717) | 4 | 260 |
| **F P=8** | **75 (72)** | **272 (256)** | **528 (508)** | **1040 (1014)** | 8 | 299 |

**F P=8 p95 multipliers vs its own C=1 (B = 75 ms p95)** — gate: C=4 < 2·B, C=8 < 3·B:
| C | p95 ms | ratio vs B | gate | verdict |
|---|---|---|---|---|
| 4 | 272 | **3.6×** | < 2× | **FAIL** |
| 8 | 528 | **7.0×** | < 3× | **FAIL** |
| 16 | 1040 | 13.8× | shed allowed, p95<4× | FAIL |

## Verdict: REJECT for the gate (proceed to Tier-2 time-cube)
1. **Fails the LR p95 multiplier vs its own C=1 baseline** (table above): C=4 = 3.6× B (gate <2×),
   C=8 = 7.0× B (gate <3×). The single-request parallelism collapses under concurrency because the
   global pool is shared — exactly the predicted failure mode. (p50 shows the same shape.)
2. **The bounded global pool is itself a throughput bottleneck.** F P=1 under load (C=16 = 5.6 s) is *worse* than P1, because one shared pool serializes concurrent requests, whereas P1 gets free per-request parallelism. F only beats P1 when P is large — but `gunicorn_workers × parallelism ≤ cores` caps P, so this does not scale.
3. **Total work unchanged (cache-independent).** `chunk_count`/`decompressed_bytes` are identical to P1 (O(days) × 4 MB chunk). F redistributes latency; the system **throughput ceiling = total decompress work / cores is the same**. Under sustained concurrency F and P1 converge.
4. **Cannot be certified at the real gate.** Local largest contiguous real span = 96 days (warm); F's 365-day / cold-cache behaviour is untestable here → not promotion-eligible regardless (spec §5.1/§7).

## What F *is* good for (optional, not gate-passing)
F is a **safe, strict improvement for single-point / low-concurrency** queries (6.8× at C=1) with
bounded RSS/threads. It MAY be kept as an optional engine for the single-query path or for shadow
exploration — but it is **not** the structural fix and **does not authorize cutover**.

> **Follow-up if F is ever kept as an optional path (review [Low], not a blocker since F is
> rejected):** `point_series` submits ALL `days` futures to the pool at once, so while *in-flight*
> chunk reads are bounded by `parallelism`, the *pending-future* count is `C × days` under
> concurrency. A kept F would need a **queue/admission bound** (cap submitted futures, or gate via
> the API `BoundedExecutor` permit before submitting), not only the in-flight bound.

## Conclusion — Tier-1 exhausted
Both Tier-1 no-rewrite candidates are disproved with benchmarks: **E1** (no latency change) and
**F** (fails LR concurrency; same total-work ceiling). The only way to cut the O(days) × large-
chunk total work is to **reduce reads and per-point bytes** → **Tier-2 time-cube** (P2-S3+): cut
`chunk_count` to O(days/time_chunk) AND shrink per-point decompressed bytes (small spatial chunk
+ v3 sharding). That is the next step.
