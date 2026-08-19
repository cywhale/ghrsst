# P5-S6 — block-size / cadence sizing: RESULTS

Status: **HARNESS DELIVERED. NO PRODUCTION GATE ADJUDICATED.**
`[MUT-STG]` — local and synthetic only. No VM24, SSH, `GHRSST_*` store, cron, PM2 or production
path is touched anywhere in this step.

Branch `dev2026-p5-s6-o2-local`, based on the merged tip `7902b61`.

- Harness: [`../bench/bench_p5_sizing.py`](../bench/bench_p5_sizing.py)
- Tests: [`../tests/test_phase2_p5s6.py`](../tests/test_phase2_p5s6.py) — **28/28**, committed
  **failing first**; **11 guards mutation-verified**
- Artifacts: `../bench/results/p5s6_sizing_quick.json`, `p5s6_sizing_full.json`

## How to read every number here

| label | meaning |
|---|---|
| **MODEL** | arithmetic at production geometry (17999 × 36000, 3 × float32, legacy `time_chunk=90`, delta t1/s256/shard256). No I/O. May be wrong; is never *evidence*. |
| **PRODUCTION_GEOMETRY_FIXTURE** | measured on a store whose **chunk/shard geometry** is production's while its spatial extent is not. The *shape* of the cost is real; the magnitude is not. |
| **LOCAL** | measured on this machine, at fixture scale. |
| **DEFERRED** | needs VM24 read-only validation. Carries a reason and **no number**. |

`anchor_day = 2026-06-27` is the **committed historical fixture anchor**, used so the block grid
is reproducible across runs. It is not a statement about any live store.

**One day of one variable at production geometry is 2.6 GB; a 90-day 3-var block is ~700 GB.**
Nothing here materializes a slab: `assert_fixture_bounds()` refuses any fixture over 4 M cells,
and a test asserts that a 17999 × 36000 request raises.

## MODEL — reproduces the spec's §10 tables exactly

| S | C | segments / 366-d request | peak temp disk | write amp | delta span max |
|---|---|---|---|---|---|
| 30 | 30 | **13** (+delta) | 47.1 GB | 1.0× | 64 d |
| 45 | 45 | **9** (+delta) | 70.7 GB | 1.0× | 79 d |
| 90 | 90 | **5** (+delta) | 141.3 GB | 1.0× | 124 d |
| **90** | **30** | **5** (+delta) | 141.3 GB | **2.0×** | **64 d** |
| 90 | 45 | 5 (+delta) | 141.3 GB | 1.5× | 79 d |

Segment horizons (§10.5, +10 y): **S=90 → 41**, S=45 → 82, S=30 → 122.
File counts (§10.6): **119 286 files per 3-var block**; 1 325 / 2 651 / 3 978 files per day at
S=90 / 45 / 30.

Two model corrections the tests forced, both against the spec's own tables:

- `segments_per_request` is `ceil(days/S)`, **not** `+1` — the delta tier is the spec's
  "(+delta)" and is counted separately. My first version returned 14 / 10 / 6.
- the horizons use **`ceil`**, not `floor`: a partially-filled tail block is still a segment the
  snapshot opens. `floor` gave 40 at S=90/+10 y where the spec says 41, and the tail block is
  exactly that difference.

## LOCAL — snapshot-open and metadata scan

The question §10.6 asks is whether these degrade **super-linearly** with segment count. Measured
at the required 5 / 13 / 41 / 82 / 122 horizons:

| segments | snapshot open (ms) | **ms per segment** | walk files | walk (ms) |
|---|---|---|---|---|
| 5 | 2.00 | 0.400 | 55 | 2.20 |
| 13 | 5.34 | 0.411 | 143 | 5.53 |
| 41 | 17.00 | 0.415 | 451 | 17.56 |
| 82 | 34.54 | 0.421 | 902 | 33.58 |
| 122 | 52.39 | **0.429** | 1 342 | 49.93 |

**Per-segment cost is flat to within 7 % across a 24× range.** No super-linear term appears, so
nothing here argues for the §10.5 super-block-merge escape hatch. This is **LOCAL** on a warm
cache with tiny blocks; the production thresholds remain DEFERRED.

## PRODUCTION_GEOMETRY_FIXTURE — segment calls, against observed operations

Reported (from the day→segment map) versus **observed** (which stores `zarr.open_group` actually
opened), over a 90-day read:

| S | reported | observed |
|---|---|---|
| 30 | 3 | 3 |
| 45 | 2 | 2 |
| 90 | 1 | 1 |

The counter is compared to what the store saw, not to itself, and a test asserts the direction
the whole sizing question turns on — smaller S means **more** observed calls.

**This measurement had to be made falsifiable twice.** With a full read, `observed` equals
`reported` no matter where `observed` came from, so a mutation replacing the observation with
the tally **survived** — and so did one fabricating the path list from it. The fix is a case
where the two *must* differ: `open_only_first` reads a window landing inside one block while the
tally still says three, and the test asserts `observed = 1`, `reported = 3`. Both mutations now
fail. A counter that agrees with itself under every input the tests supply is not verified.

## LOCAL — tail rebuild and carry-forward

Rewrite evidence is a **sha256 content digest**, never mtime or size. A companion test moves
every mtime forward without changing a byte and asserts **0** files are counted as rewritten.

| S | C | carried-forward days | rewritten files | rebuild (ms) |
|---|---|---|---|---|
| 30 | 30 | 30 | 0 | 21.0 |
| 45 | 30 | 30 | 7 | 20.2 |
| 90 | 30 | 30 | 7 | 21.0 |

S=30/C=30 rewrites nothing because the block seals exactly at the cadence — there is no tail to
carry forward. That is the shape §10.2's `amplification = 1.0` describes, arrived at from the
disk rather than from the formula.

## DEFERRED — five metrics, no numbers

| metric | why it cannot be answered here |
|---|---|
| inode headroom | `df -i` on the production filesystem. A temp dir says nothing about VM24's inode table, and a full table is an outage with free bytes to spare. |
| production 366-d point latency | §10.4's 0.65 ms/call is a laptop projection against a 76–100 ms VM24 baseline. Measuring it at fixture extent produces a number that *looks* like the gate. |
| peak temp disk at production scale | 47/71/141 GB is arithmetic from 1.57 GB/day. Confirming it means building one real block — a VM24 mutation. |
| metadata scan on the production tree | dominated by the filesystem's directory implementation and cache state at 1.7–6.6 M files. Local timings do not extrapolate. |
| open fd / RSS under sustained load | H7's C=8/16 over 10 minutes is an S7 load gate against the real store; this harness runs no HTTP layer. |

## Gates — none adjudicated

| gate | verdict | why |
|---|---|---|
| **G3** | **NOT_ADJUDICATED** | the 366-day p95 gate is against the VM24 baseline; nothing at fixture extent can decide it |
| **H7** | **NOT_ADJUDICATED** | inode headroom and sustained-load fd/RSS are DEFERRED |
| **G16** | **NOT_ADJUDICATED** | scan/open *shapes* are measured; the production thresholds are not |

A test asserts the harness never emits a production PASS for G3 or H7, by walking the whole
report and failing on the string.

## Is S=90 / C=30 still viable?

**On the local and modelled evidence, yes — and nothing here can confirm it.** What the evidence
supports:

- it holds segments per 366-day request at the **minimum of the three sizes (5)**, which is the
  variable §10.4 says dominates read cost;
- it holds delta span at **64 days**, the same as S=30/C=30 — because span is driven by cadence,
  not block size (§10.1), measured across all three S values in test;
- its cost is **2.0× write amplification** (≈1 146 GB/year) and **141 GB** peak temp disk, versus
  ~2 000 GB for one monolith rebuild;
- snapshot-open per segment is flat, so the 41-segment +10 y horizon shows no scaling term.

What would overturn it: a production 366-day p95 outside the G3 budget, or inode headroom below
the +10 y projection. Both are DEFERRED, and both need VM24 read-only.

## Cannot be adjudicated without later VM24 RO validation

1. **G3** — production read latency at S=30/45/90.
2. **H7 inode headroom** — `df -i` against the ~6.6 M projection.
3. **G16 thresholds** — metadata scan and snapshot open on the real tree.
4. **Peak temp disk** — confirming 141 GB requires building a real block.

## Not delivered in this pass

- **O2 materialization strategy, source-map extension, disk gate, manifest/provenance/rollback
  lifecycle, API parity and snapshot-safety tests.** The instruction was to implement the sizing
  harness *first*; this document covers that and nothing beyond it.
- **S7 integration and any production deployment work** — explicitly out of scope.
