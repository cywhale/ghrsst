# P5-S6 — block-size / cadence sizing: RESULTS

Status: **MODEL DELIVERED. THE THREE "MEASUREMENTS" DO NOT USE THE O2 READ PATH AND ARE
WITHDRAWN AS EVIDENCE.** No production gate adjudicated.

> **Review round 2 correction — read this before any table below.** Three measurements went
> around the code they claimed to measure, and the fourth measured arithmetic:
>
> | claimed | actually measured | now |
> |---|---|---|
> | segment calls on the grouped read path | stores *this harness* opened in a loop — no manifest, no `SegmentedCubeStore`, no `point_series()` | **not production-path evidence** |
> | O2 snapshot-open cost | raw `zarr.open_group()` — no manifest validation, segment inspection, metadata binding or snapshot build | **raw group-open cost, relabelled** |
> | H5 crossing latency vs delta span | nothing; the table is volume arithmetic | **DEFERRED** |
> | tail rebuild carry-forward | `v2` rebuilt from fixtures, never read from the predecessor; `carried_forward_days` is a computed number | **DEFERRED** |
>
> `open_only_first` is an artificial restriction of the harness's own loop, not a real request,
> so it does not rescue the first row. Rebuilding these against `SegmentedCubeStore` /
> `TieredCube` — the path `bench_p5_segmented_read.py` already exercises — is the next step and
> is **not done here**.
`[MUT-STG]` — local and synthetic only. No VM24, SSH, `GHRSST_*` store, cron, PM2 or production
path is touched anywhere in this step.

Branch `dev2026-p5-s6-o2-local`, based on the merged tip `7902b61`.

- Harness: [`../bench/bench_p5_sizing.py`](../bench/bench_p5_sizing.py)
- Tests: [`../tests/test_phase2_p5s6.py`](../tests/test_phase2_p5s6.py) — **33/33**, committed
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

## Snapshot construction — the REAL constructor (item 2)

`measure_snapshot_construction()` times `SegmentedCubeStore(manifest_dir)`: manifest validation,
segment inspection, metadata binding and snapshot build — what a generation change and a process
start actually pay. p50/p95 over repeated constructions, fixture geometry and the warm-cache
assumption are recorded with every row. **LOCAL_SYNTHETIC; adjudicates no gate.**

Blocks now carry **30 days each**, recorded in the artifact as `days_per_segment` and
`total_days`. One day per segment made the 5/13/41/82/122 horizons into one-day blocks, which
understates exactly the day-metadata and inspection cost this measurement exists to capture.

**The timings are NOT reproduced here.** An earlier revision hand-copied them and they drifted
from the artifact within one round — the same artifact-drift failure this document has already
hit twice. The authoritative values are
`bench/results/p5s6_sizing_full.json` → `measured.snapshot_construction`, and a test asserts this
section carries no hand-typed millisecond table.

What the shape shows, which does not drift: per-segment construction is **flat** across
5 → 122 segments, while the **absolute** cost at the S=30 / +10 y horizon is an order of
magnitude above the raw `open_group` figure for the same segment count. That gap is everything
`SegmentedCubeStore` does around a bare open — validation, inspection, binding, snapshot build —
and it is exactly what the earlier conflation hid. **LOCAL_SYNTHETIC at 16×16 spatial extent —
not a production number, and it adjudicates nothing.**

The raw-`open_group` **measurement** is retained alongside it — in the artifact, under
`measured.snapshot_open`, still labelled as raw cost — because the two were conflated once and
the difference is most of the work. No table is reproduced in this document: see the section
below for why.

## RAW GROUP-OPEN COST (not O2 snapshot-open) — retained in the artifact, for contrast

The question §10.6 asks is whether these degrade **super-linearly** with segment count. Measured
at the required 5 / 13 / 41 / 82 / 122 horizons:

**Timings are NOT reproduced here either.** The hand-typed table that used to sit above had
already drifted from the artifact — the same failure as the construction section, one section
over, because the previous round fixed the instance instead of the class. The authoritative
values are `bench/results/p5s6_sizing_full.json` → `measured.snapshot_open`, and the guard now
covers **every** measured-timing section rather than one of them.

What these numbers are: raw `zarr.open_group()` cost. They exclude manifest validation, segment
inspection, metadata binding and snapshot construction — most of what `SegmentedCubeStore`
actually does on a generation change, which is why the construction measurement exists beside
them. The shape, which does not drift: per-segment raw cost is **flat** across the 5 → 122
range, so no super-linear term appears and nothing here argues for the §10.5 super-block-merge
escape hatch. **LOCAL** on a warm cache with tiny blocks; production thresholds remain DEFERRED.

## Grouped read — the REAL path (item 1, partial)

`measure_grouped_read()` now builds a **published manifest**, constructs
`SegmentedCubeStore(manifest_dir)`, and calls **`SegmentedCubeStore.point_series()`**. The
observation is taken by patching `open_group` in **`store.time_cube`** — the boundary the
implementation actually resolves it through, since `segmented_cube` delegates rather than
importing zarr. Patching a name the implementation never uses would observe nothing and pass.

**The measurement produced a correction, not a number.** A warm read observes **zero**
`open_group` calls — and that is right: P5-S2 §6.2 guarantees the read path opens nothing,
serving from metadata captured at snapshot time. So `open_group` **cannot** count segments a
read touches, and my first version hid this behind an `or len(expected)` fallback that
substituted the *predicted* count whenever the observation was empty. Detaching the spy
entirely still passed. The fallback is gone; zero is reported as zero.

Consequently:

- **expected segments for a request** — derived from the manifest and the request, and asserted
  to grow with the request width. **LOCAL_SYNTHETIC.**
- **observed segments touched by a read** — **DEFERRED**: it needs a chunk-level observable (a
  counting store around each segment), which is not built here.
- **raw-cell canary** (deliberately not called "parity") — a sentinel is written **directly into the block's raw cells via zarr**,
  bypassing the read path, and then read back **through `point_series()`**. Comparing
  `point_series()` to `point_series()` proves only determinism: if it returned the wrong cell,
  both sides return the same wrong cell. A **negative control** (`_negative_control=True`)
  compares against a value that was never written and must report a mismatch, because
  `raw_cell_canary_ok: True` proves nothing unless `False` is reachable — a mutation disabling
  the comparison outright survived until that control existed.
  **Scope: ONE probe day, ONE variable, ONE cell.** It shows the measured request serves real
  bytes; it is **not** parity over all returned rows or all variables, and the field names say
  so. **LOCAL_SYNTHETIC.**

Both measurements are now **in the generated artifacts**, not only in this prose: they were
described here and absent from the JSON, so the consistency tests could not see the omission.

## Segment calls — NOT the grouped read path (withdrawn, superseded above)

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

## Tail rebuild — DEFERRED, and therefore no numbers

No carry-forward was measured. `v2` is rebuilt from fixtures, never read from the predecessor
block, and there is no source map — so `carried_forward_days` was arithmetic wearing the label
of read evidence. The earlier table of rebuild wall times has been **removed rather than
relabelled**: a millisecond figure sitting under a `DEFERRED` heading reads as performance
evidence no matter what the heading says.

A probe (`_tail_rebuild_probe`) still runs the fixture work, because the guard it exercises is
worth keeping under test: rewrite evidence is a **sha256 content digest**, and a companion test
moves every mtime forward without changing a byte and asserts **0** files are counted as
rewritten. The probe's timings are not published.

Real carry-forward needs the O2 materialization strategy, which does not exist yet.

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

**The modelled case is unchanged and still favours it; the measured case is now withdrawn, so
this section rests on MODEL alone.** No configuration is chosen in this document. What the evidence
supports:

- it holds segments per 366-day request at the **minimum of the three sizes (5)**, which is the
  variable §10.4 says dominates read cost;
- it holds delta span at **64 days**, the same as S=30/C=30 — because span is driven by cadence,
  not block size (§10.1), measured across all three S values in test;
- its cost is **2.0× write amplification** (≈1 146 GB/year) and **141 GB** peak temp disk, versus
  ~2 000 GB for one monolith rebuild;
- raw group-open per segment is flat, which is *suggestive* for the 41-segment +10 y horizon
  and is **not** an O2 snapshot-open result.

What would overturn it: a production 366-day p95 outside the G3 budget, or inode headroom below
the +10 y projection. Both are DEFERRED, and both need VM24 read-only.

## Cannot be adjudicated without later VM24 RO validation

1. **G3** — production read latency at S=30/45/90.
2. **H7 inode headroom** — `df -i` against the ~6.6 M projection.
3. **G16 thresholds** — metadata scan and snapshot open on the real tree.
4. **Peak temp disk** — confirming 141 GB requires building a real block.

## H5 — crossing latency through a real `TieredCube` (item 3, DELIVERED)

`measure_h5_span()` builds a published base **and** a delta from **one oracle**: the delta days
are the same days the base tail holds, then stamped with a distinct per-day value so precedence
is observable. Two independently generated fixtures would make any disagreement meaningless —
it would not be a bug, just two different datasets.

Each span measures **base-only**, **crossing** and **delta-only** requests through
`TieredCube.point_series()`, recording p50/p95, requested vs returned day counts, and
`day_source` per day. Spans: **31 / 45 / 64 / 90 / 124** in the full run; the first two in
`quick`.

Asserted, not assumed:

- the crossing request really spans **both** tiers (`day_source` contains base *and* delta) —
  otherwise "crossing" is a base read with a longer name;
- **delta wins** for days both tiers hold, checked on the **timed rows themselves**. Base holds
  the same days, so a timed loop reading base alone returns the right *count* — a mutation doing
  exactly that survived until the values were checked;
- a **negative control** makes the precedence check fail on demand, so `ok: True` is falsifiable.

Timings are in the artifact under `H5.by_span`, not reproduced here. **LOCAL_SYNTHETIC: it does
not adjudicate G3, which is a VM24 measurement.**

## Provisional design target

**S=90 / C=30**, an operator decision recorded here, held as `DEFAULT_S` / `DEFAULT_C`
**configuration** in the harness and never hardcoded at a call site. Production adjudication
still waits on VM24's G3 / H7 / G16.

## Item 4 — NOT STARTED

**O2 tail carry-forward.** One finding that changes its shape: the carry-forward contract
**already exists** in S3's `build_block` — `predecessor_path`, `predecessor_present`,
`resolve_source_map()` with authority order delta → block → daily → hold, delta-wins for late
corrections, `rebuild_source_set = predecessor_present ∪ newly_present`, and a refusal for any
day whose source cannot be resolved *before* anything is written. So O2 is **exercising and
measuring that path**, not building a second source-map or provenance truth beside it.

Not started here, and `measure_tail_rebuild` remains DEFERRED and publishes no numbers.

## VM24 — read-only measurement TAKEN

**Artifact: `bench/results/p5s6_vm24_readonly.json`, provenance `VM24_PRODUCTION_READ_ONLY`.**
Kept in its own file: no VM24 number appears in the LOCAL_SYNTHETIC artifacts and none of the
synthetic numbers appear here. Host stable throughout (up 5 days, load 0.10) — no measurement
was marked BLOCKED.

Read-only only: `df`, `df -i`, `free`, `findmnt`, `/healthz`, `pm2 jlist`, `/proc/<pid>` and
`du -sh`, plus **21 serial point-range GETs** at concurrency 1, 0.3 s apart. No prune, swap,
compaction, repair, hold deletion, cron/PM2/systemd change, or write of any kind.

### The model was close — checked against the real stores for the first time

| | measured | modelled (§10.3) |
|---|---|---|
| delta GB/day | 1.12 | 1.09 |
| base GB/day | 1.62 | 1.57 |

Both within ~3 %. The §10.3 volume table, and the peak-temp-disk figures derived from it, rest on
numbers that now have production evidence behind them.

### The H5 finding, and it inverts a natural assumption

**Delta-only is the slowest window**, despite returning *fewer* days than base-only:

| window | days returned | p50 | p95 |
|---|---|---|---|
| base-only | 51 | fastest | |
| crossing | 31 | middle | |
| delta-only | 41 | **slowest** | |

(Values in the artifact; not reproduced here, per the timing-drift guard.) Delta is
**t1/s256** — one chunk per day — so a 41-day delta read touches ~41 chunks per variable, while
base's `(90,8,8)` chunking serves 51 days from far fewer. This is §10.1's claim that **cadence,
not block size, governs the read cost of the delta tail** — measured on production rather than
modelled.

It is a direct argument for the **small C** in the provisional S=90 / C=30 target: C drives the
delta span, and the delta span is what this measurement shows dominating read latency.

### Gates, from VM24 evidence

| gate | verdict | why |
|---|---|---|
| **G3** | **NOT_ADJUDICATED** | 1-concurrency samples at one coordinate over 3 windows. They inform G3; they do not decide it. |
| **H7 inode headroom** | **PASS_EVIDENCE_AVAILABLE** | 225,490,340 free inodes against a ~4.9 M +10 y projection — ~46× headroom. The adjudication is the reviewer's. |
| **H7 fd/RSS** | **NOT_ADJUDICATED** | 22 fds, 78 MB RSS at rest; H7 asks for C=8/16 under sustained load, which is not a read-only measurement. |
| **G16** | **NOT_ADJUDICATED** | metadata scan / snapshot-open on the production tree were not measured; walking a 5.4 T store is not a low-rate probe. |

### Capacity gate (§7.1c / G18) — **BLOCKED**

**The earlier claim in this document that "141 GB fits with margin" was wrong, and is
withdrawn.** It compared the block size alone against free space. The gate is
`projected_free_after ≥ HARD_RESERVE`, and `additional` is never the block alone:

| term | value | source |
|---|---|---|
| `free_now` | 267 GB | `df` avail, measured |
| `new_block` (S=90 × 1.62 GB/day) | 145.8 GB | measured base rate |
| `temp` / checkpoint / artifacts | 2 GB | estimate |
| `growth_during_build` (2.74 GB/day × 2 d) | 5.5 GB | measured ingest rate, **assumed** duration |
| **`additional`** | **153.3 GB** | |
| **`projected_free_after`** | **113.7 GB** | `free_now − additional` |
| `HARD_RESERVE` | **250 GB** | §7.1c / P4-S4 §6 |

**113.7 GB < 250 GB → BLOCKED.** Shortfall **136.3 GB**; the build would need **403.3 GB** free.

It stays blocked with `temp` and `growth` set to **zero** — the block alone breaches the
reserve — so the verdict does not depend on how the soft terms are estimated, and the assumed
2-day build duration does not change it either.

`HARD_RESERVE` is **not** adjusted here. Lowering a reserve to make a gate pass is an ops policy
decision raised separately, never an implementation detail; a test pins it at 250 GB. The
arithmetic is cross-checked against the shipped `build_block.disk_precheck` on identical inputs,
so this is production's formula rather than a second one.

What remains true: a 2 TB monolith rebuild is far further out of reach than one 90-day block,
which is the constraint O2 exists to remove. But **O2 cannot run on VM24 at today's free
space**, and no local result changes that.

### Hold / lifecycle audit — **AUDIT ONLY, NO DELETION PERFORMED**

Read-only: `hold/manifest.jsonl` plus `du -sb`. Nothing was deleted, moved or modified, and this
classification **authorizes nothing**.

| class | what | size | releasable? |
|---|---|---|---|
| `releasable_after_approval` | 1267 daily staging holds, `2023-01-01`..`2026-06-20` | ~2192 GiB | yes, **with approval** |
| `releasable_after_approval` | 2 expired delta backups (07-09, 07-28) | ~96 GiB | yes, **with approval** |
| `referenced` | newest delta backup (08-18), `hold_until` 2026-09-01 | ~59 GiB | **no** |
| `inconsistent_or_unknown` | 2 delta copies **not in the hold manifest** | ~41 GiB | **not determinable** |

The daily holds qualify because base is **gapless across 2023-01-01..2026-06-26** (1273 days,
read from the store's own attributes), so every held day is served by base for point/range; and
the newest held day, `2026-06-20`, falls **before** the enforced spatial window starts
(`2026-06-27`), so none is needed for bbox.

`blocked_unique_copy` and `repair_or_refold_pending` are both **empty**, and the reason is
recorded rather than assumed: the P5 roots (`p5_blocks`, `p5_repairs`, `p5_anchor`,
`base_blocks`) are **absent on this host**, so no repair WAL or Phase A state exists to consult.
Empty by construction, not by inspection — a distinction that matters if P5 is ever deployed
there.

The two untracked delta copies are deliberately **excluded** from the reclaimable total. They
are on disk with no hold record, which is exactly what a manual cleanup leaves behind; releasing
them is an ops decision, not this audit's.

### Does it clear G18?

| | GiB |
|---|---|
| `currently_reclaimable` | **2288.0** |
| `free_now` | 267.0 |
| `free_after_hypothetical_reclaim` | 2555.0 |
| `required_free_before_build` (250 + 153.3) | 403.3 |
| `remaining_shortfall` | **0.0** |

**Disk expansion is not required** — roughly 17× what the 136.3 GiB shortfall needs. But
theoretical reclaim is not reclaimed capacity: **G18 remains BLOCKED** until a human approves
the release and it actually happens.

One measurement note, because it nearly produced a wrong number: `du --block-size=1G` per
directory summed to 2534 GiB against a 2347 GiB total, since it rounds each of 1267 entries up.
Every figure here is `du -sb` bytes, and the parts summing to the measured whole is the check
that caught it.

## Not delivered in this pass

- **O2 materialization strategy, source-map extension, disk gate, manifest/provenance/rollback
  lifecycle, API parity and snapshot-safety tests.** The instruction was to implement the sizing
  harness *first*; this document covers that and nothing beyond it.
- **S7 integration and any production deployment work** — explicitly out of scope.
