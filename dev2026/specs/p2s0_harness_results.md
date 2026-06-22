# P2-S0 — harness reliability (fixtures + micro-cost) — results

> Gate before any E1/F/time-cube work (spec §6): **the benchmark harness must be proven
> reliable first.** This records that proof + two real findings it already surfaced.
> Tools: `dev2026/ingest/build_fixture.py` (Fixture A), `dev2026/bench/bench_timecube_microcost.py`,
> tests `dev2026/tests/test_phase2_s0.py` (9/9 pass).

## Harness proven
- `test_phase2_s0.py` **9/9**: `longest_contiguous` (contiguous / gap / month-boundary / single /
  empty); fixture meta (N contiguous, synthetic, **not promotion-eligible**); micro-cost
  cache-independent metrics **exact** — `chunk_count == days × nfields` (O(days)),
  `decompressed_bytes == chunk_count × containing-chunk bytes`, `read_amplification` consistent.
- Cache-independent metrics are derived from chunk geometry (deterministic, cache-free) — the
  signal that does NOT depend on warm/partial state (the P1 trap).

## Run 1 — Fixture A (synthetic, 365 contiguous days, 128×128, chunk 128)
```
chunk_count = 1095 (= 365 × 3 vars, O(days))   decompressed = 71.76 MB   read_amp = 16384×
latency p50 = 2245 ms (warm)   py_alloc_peak = 2.7 MB   rss 49.5→53.6 MB
promotion_gate_eligible = False  (synthetic — pre-gate/CI only)
```
→ Confirms the O(days) baseline and that synthetic runs are correctly **not** promotion artifacts.

## Run 2 — Fixture B selector on the REAL local store (largest contiguous real span)
```
largest contiguous real span = 96 days (2025-11-30 .. 2026-03-05)   chunk = [1,1024,1024] (prod)
chunk_count = 288 (= 96 × 3, O(days))   decompressed = 1207.96 MB   read_amp = 1048576×
latency p50 = 1166 ms (warm, 96 days)   rss 49.5→122.7 MB
promotion_gate_eligible = False
```

### Finding 1 — local CANNOT provide a real 365-day promotion gate (yet)
The local copy's **largest contiguous real span is only 96 days** (internal gaps; copy still
ongoing). Per spec §5.1/§7 the local gate is therefore **INCOMPLETE** → Phase-2 may run **VM24
exploratory shadow only**, **not cutover-eligible**, until a ≥365-day contiguous real gate exists
(richer local copy, or the real gate runs on VM24's full store). The harness labels this
automatically (`promotion_gate_eligible=False`, reason recorded in `meta`).

### Finding 2 — cache-independent smoking gun for the P1/VM24 failure
On the **production chunking (1024×1024)**, a single point's day read decompresses a full 4 MB
chunk → **96 days × 3 vars ⇒ ~1.2 GB decompressed, read_amplification ≈ 1.05M×**. This is the
**O(days) × large-spatial-chunk** cost, and it is **cache-independent** — exactly why VM24's
365-day series was ~14 s p95 regardless of caching. It quantifies the target for Tier-1/Tier-2:
- E1/F can only parallelize/relabel this O(days)×4 MB work (chunk_count stays 288/96-day).
- A time store must cut `chunk_count` to O(days/time_chunk) AND shrink per-point decompressed
  bytes (small spatial chunk) — the design's core hypothesis, now with a concrete baseline number.

## Status / next
- S0 harness reliable ✅ (tests + two real runs). **No E1/F/time-cube implemented yet** (per instruction).
- Next per §6: **P2-S1 (E1 manifest)** then **P2-S2 (F smarter-read)**, each benchmarked with this
  micro-cost + `loadtest.py`, gated on Fixture B where a real ≥365 span is available (else shadow-only).
- Carry Finding 1 to ops: a longer contiguous local copy (or VM24 real gate) is needed before any
  Phase-2 cutover-eligible promotion.
