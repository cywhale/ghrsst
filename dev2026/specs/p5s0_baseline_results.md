# P5-S0 — current compaction/read baseline + cost model: RESULTS

Status: **DONE — gate PASS. [RO] with respect to production.** No VM24 path was opened, no
`GHRSST_*` store was read, no production file was touched, and no P5 storage code
(`SegmentedCubeStore`, `build_block`) exists yet. Everything here runs on synthetic fixtures in
temp directories.

Implements P5-S0 of [`p5_segmented_timecube_compaction_design.md`](p5_segmented_timecube_compaction_design.md) §14.

- Primitives: [`../bench/p5_cost.py`](../bench/p5_cost.py)
- Hypothesis harness: [`../bench/bench_p5_rmw.py`](../bench/bench_p5_rmw.py) → [`../bench/results/p5s0_rmw.json`](../bench/results/p5s0_rmw.json)
- Baseline of record: [`../bench/bench_p5_baseline.py`](../bench/bench_p5_baseline.py) → [`../bench/results/p5s0_baseline.json`](../bench/results/p5s0_baseline.json)
- Self-tests: [`../tests/test_phase2_p5s0.py`](../tests/test_phase2_p5s0.py) — **17/17 green**

## 0. Why this step existed

The design spec's structural numbers were tagged **[PROBE]**: produced by throwaway scratch
scripts, never committed, never reviewed. §14 P5-S0 made re-deriving them with a **committed,
self-tested** harness a precondition for every later step. The stop condition was explicit:
**stop if the harness cannot reproduce H1/H2, or if fixture geometry / counters cannot validate
themselves.**

Neither stop condition fired. Both counter defects found along the way were found *by the
self-tests*, before any measurement was believed — which is the entire point of writing them
first.

## 1. The harness had to prove itself first

Two counter families, kept deliberately distinct:

| family | what it is | how it is validated |
|---|---|---|
| **cache-independent (analytic)** — `chunk_count`, `decompressed_bytes`, `read_amplification` | derived from chunk geometry (P2 method); does not move with page cache or CPU | validated against the chunk keys a **real** zarr read touches, observed through a counting `LocalStore`; and against hand-computed values |
| **observed** — files rewritten, bytes rewritten, store reads | measured from the filesystem and the store | rewrites detected by **content digest** |

### 1.1 Two counter defects the self-tests caught

**(a) `(mtime, size)` snapshots are not sufficient.** A shard rewrite can preserve both. A
snapshot keyed on them would report *0 bytes rewritten* and **fabricate a passing H1/H2
result** — the exact false green this phase cannot afford.
`test_same_size_same_mtime_rewrite_is_detected` writes 4096 bytes, rewrites them with
different content, restores the original `mtime` by `os.utime`, and requires detection.
`snapshot_tree` is therefore content-addressed (sha256).

**(b) The observed counter silently reported zero.** `zarr.open_group(store, mode="r")` calls
`with_read_only`, which builds a **fresh** `type(self)` — a brand-new counter. Then `reset()`
*rebound* the list rather than clearing it, orphaning the list shared with the derived store.
Both faults produce the same symptom: every observed count reads 0, and any assertion of the
form "observed ≥ something" passes trivially. Fixed by sharing the list across
`with_read_only` and clearing **in place**; pinned by
`test_reset_keeps_derived_read_only_store_attributed` and by requiring `store_reads > 0`
inside the H3 harness path itself.

### 1.2 Fixture geometry is read back from disk

`read_geometry` / `assert_geometry` always re-open the array and compare `shape`/`chunks`/
`shards` **on disk** against what was declared; a mismatch raises. The harness never takes its
own build parameters on trust.

### 1.3 The gate can fail

`evaluate_s0_gate` is fed a fabricated low-amplification H1 result and a fabricated H2 failure
in `TestArtifactsAndGate`, and must return `FAIL` naming the hypothesis. **A gate that cannot
fail certifies nothing.**

## 2. H1 — one-day write inside a full block  ✅ REPRODUCED

Geometry read back from disk: `chunks=(90,8,8)`, `shards=(90,128,128)`, 512×512, 90 days.

| quantity | measured |
|---|---|
| block on disk | 16 shards, 83.68 MB |
| shards rewritten by a single-day write | **16 / 16 (100 %)** |
| bytes rewritten | **83.68 MB** |
| **amplification vs one day's logical bytes** | **90.0×** |
| wall time | 576 ms |

Matches the scratch probe exactly (90.0×) and matches the Zarr v3 sharding spec: updating a
subset of inner chunks requires re-emitting the whole shard, and the byte-range partial-write
optimization needs **fixed-size (uncompressed)** inner chunks, which we do not have.

**Consequence: candidate A stays rejected**, on committed evidence.

## 3. H2 — appending into a partial tail shard  ✅ REPRODUCED

Each row builds an `L`-day block that declares the **full** 90-day time block (the
in-place-growth configuration), then appends day `L+1`.

| tail length | block on disk | bytes rewritten | amplification | shard files rewritten |
|---|---|---|---|---|
| 1 d | 1.21 MB | 2.29 MB | 1.9× | 17 |
| 15 d | 14.29 MB | 15.41 MB | 16.2× | 17 |
| 30 d | 28.58 MB | 29.15 MB | **30.6×** | 17 |
| 60 d | 56.84 MB | 57.33 MB | 60.5× | 17 |
| 89 d | 83.77 MB | 82.79 MB | 88.0× | 17 |

Amplification tracks the tail length exactly — the append rewrites the whole partial shard
every time. Growing a 90-day block one day at a time therefore costs `Σ(1..90)` day-writes
= **45.5×** the block's final size.

> **Note on scope.** This is the cost of the *rejected* in-place-growth alternative. A
> **published** unsealed block is created at its materialized size (§7.2), so the steady-state
> cost is the §10.2 model `(S/C + 1)/2`, not 45.5×.

## 4. H3 — segmentation cost  ✅ SUPPORTED, and **refined by the harness**

The spec's H3 said segmentation changes *neither* `chunk_count` *nor* `decompressed_bytes`.
The committed harness showed that statement is **imprecise**, and separated two regimes that an
earlier draft of this harness conflated (producing a spurious FAIL).

**(a) Constant inner time chunk** — every segment holds ≥ 90 days, 360 days total:

| segments | days/segment | time chunk | chunk_count | decompressed | store reads | p50 | p95 |
|---|---|---|---|---|---|---|---|
| 1 | 360 | 90 | 4 | 92 160 B | 9 | 1.98 ms | 2.0 ms |
| 2 | 180 | 90 | 4 | 92 160 B | 10 | 2.42 ms | 2.6 ms |
| 4 | 90 | 90 | 4 | 92 160 B | 12 | 3.92 ms | 4.3 ms |

Both cache-independent counters are **exactly invariant**; only store calls and latency grow.
Marginal cost ≈ **+0.65 ms per extra segment** at this grid.

**(b) Shrinking inner time chunk** — sub-90-day blocks, same 360 days:

| block size | time chunk | chunk_count | decompressed | files | p50 |
|---|---|---|---|---|---|
| 90 d | 90 | 4 | 92 160 B | 72 | 3.95 ms |
| 45 d | 45 | 8 | 92 160 B | 144 | 7.87 ms |
| 30 d | 30 | 12 | 92 160 B | 216 | 11.82 ms |

`chunk_count` **rises** while `decompressed_bytes` stays **exactly constant**.

> **Refined H3 (recommended spec wording):** *segmentation never changes
> `decompressed_bytes`; `chunk_count` is additionally invariant while the inner time chunk is
> unchanged. A sub-90-day block shrinks the time chunk, raising `chunk_count` at constant
> decompressed bytes — the §10.4 sizing trade, not an H3 violation.*

This **strengthens** §10.4 rather than contradicting it: §10.4 already modelled decompressed
bytes as ~invariant with only call count changing. It is a spec-wording refinement for a later
editorial pass, not an architecture change — flagged for Codex, not acted on here.

## 5. H8 — Zarr v3 native append  ✅ REPRODUCED (rectilinear rejected)

zarr-python **3.2.1**.

- **Rectilinear *chunks* + sharding: rejected by the library** —
  `ValueError: Rectilinear chunks with sharding is not supported. Use rectilinear shards
  instead: chunks=(inner_size, ...), shards=[[shard_sizes], ...]`
- **Rectilinear *shards*: supported, and unusable for the base read layout.** The inner time
  chunk is uniform array-wide, so mixing 90-day history shards with 1-day tail shards forces
  inner time chunk = 1 everywhere:

| layout | build | files | bytes | 120-day series p50 | 1-day p50 | 1-day append |
|---|---|---|---|---|---|---|
| regular `(90,8,8)/(90,128,128)` | **0.90 s** | 34 | 112.3 MB | **1.24 ms** | 0.98 ms | — |
| rectilinear shards, inner `(1,8,8)` | 34.95 s | 514 | 139.8 MB | **23.73 ms** | 1.07 ms | 373 ms, 1.16 MB |

**19.2× point-series read regression**, 39× slower build, 15× more files, 25 % more bytes — in
exchange for a cheap append that the delta tier already provides. A point-series regression is
a veto (P4-S4 §6.1).

**Consequence: candidate E stays rejected for the base read layout**, with §16-Q7's re-test at
zarr-python 3.3 unchanged.

## 6. Baseline of record for gate G3

Current `TieredCube` (base `s8/t90/shard128` + delta `t1/s256`), synthetic fixture on
production chunk geometry, 256×256 grid, 360 base days + 64 delta days, 3 variables, warm.

| case | days | p50 | **p95** | chunk_count | read_amp |
|---|---|---|---|---|---|
| single historical day point | 1 | 2.28 ms | **2.43 ms** | 3 | — |
| 366-day range, base only | 360 | 5.64 ms | **6.00 ms** | 12 | 64× |
| crossing range, delta span 31 d | 366 | 37.5 ms | **39.1 ms** | 93 delta chunks | — |
| crossing range, delta span 45 d | 366 | 51.2 ms | **56.3 ms** | 135 delta chunks | — |
| crossing range, delta span 64 d | 366 | 69.5 ms | **70.2 ms** | 192 delta chunks | — |

**These three p95 values are the G3 reference** (`g3_reference_p95_ms` in the artifact). The
segmented store must land within **+25 %** of them on the same fixture.

### 6.1 H5 is visible in the baseline

Delta-span sensitivity is monotonic and steep: **37.5 → 51.2 → 69.5 ms** as the delta span
grows 31 → 45 → 64 days, with delta decompressed bytes rising **24.4 → 35.4 → 50.3 MB** against
a pure-base 366-day read of **5.6 ms**. A delta day costs ~262 kB decompressed per variable
versus ~256 B amortized in base.

This is committed local corroboration of the production observation (VM24 v0.5.0: pure-base
366-day **76 ms** vs base→delta crossing **219 ms** at 36 delta days), and it is the measured
basis for §10.1's rule that **cadence `C`, not block size `S`, governs delta span** — and hence
for preferring a smaller `C`.

**Caveat:** synthetic, warm, laptop, 256×256. Not VM24 performance and not binding. It is a
*relative* baseline for G3 on an identical fixture.

## 7. Gate

```json
{ "verdict": "PASS", "failures": [], "checked": ["H1", "H2", "H3"] }
```

| stop condition (§14 P5-S0) | outcome |
|---|---|
| harness cannot reproduce H1 | **not triggered** — 90.0×, 16/16 shards |
| harness cannot reproduce H2 | **not triggered** — amplification tracks tail length; 45.5× projected |
| fixture geometry cannot validate itself | **not triggered** — read back from disk, mismatch raises |
| counters cannot validate themselves | **not triggered** — analytic == observed chunk keys; two counter defects found and fixed *before* any result was believed |

## 8. What this does and does not authorize

- ✅ The spec's **[PROBE]** numbers for H1/H2/H3/H8 are now **committed, self-tested evidence**.
  §3.A (candidate A) and §3.E (candidate E) rejections stand on it.
- ✅ The **G3 baseline of record** exists and is reproducible.
- ✅ **H5** has local corroboration.
- ❌ It authorizes **no** production change, **no** VM24 contact, and **no** P5 storage
  implementation. `SegmentedCubeStore` and `build_block` remain unwritten — those are P5-S1/S2
  and P5-S3.
- ❌ It does **not** settle sizing. `S`/`C` remain the §10.8 provisional recommendation; **P5-S6**
  is the adjudicating benchmark, and it must measure on production-scale geometry rather than
  extrapolating the +0.65 ms/segment figure recorded here.

## 9. Reproduce

```bash
dev2026/.venv/bin/python -m unittest dev2026.tests.test_phase2_p5s0
dev2026/.venv/bin/python dev2026/bench/bench_p5_rmw.py      --out dev2026/bench/results/p5s0_rmw.json
dev2026/.venv/bin/python dev2026/bench/bench_p5_baseline.py --out dev2026/bench/results/p5s0_baseline.json
```

`bench_p5_rmw.py` exits non-zero when the gate fails.

## 10. For Codex

1. **Spec wording refinement (§2 H3, editorial):** adopt the refined H3 statement in §4 above.
   Evidence attached; no architecture impact.
2. **Artifacts committed:** `p5s0_rmw.json`, `p5s0_baseline.json` — both carry `provenance`,
   `env` (zarr/python/platform) and the geometry read back from disk.
3. **Next:** P5-S1 (manifest schema + segmented-store prototype), which is the first step that
   writes P5 storage code.
