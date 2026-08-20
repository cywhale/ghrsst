# P5-S6 — block-size / cadence sizing: RESULTS

Status: **MODEL DELIVERED. THE FOUR ROUND-2 WITHDRAWALS ARE NOW DISCHARGED** — each measurement
was rebuilt against the code it claims to measure. Still **fixture extent, local and synthetic**:
no production gate is adjudicated, and nothing here is capacity evidence.

> **Review round 2 correction — kept as the record of what was wrong, and what closed it.**
> Three measurements went around the code they claimed to measure, and the fourth measured
> arithmetic:
>
> | claimed | actually measured, at round 2 | now |
> |---|---|---|
> | segment calls on the grouped read path | stores *this harness* opened in a loop — no manifest, no `SegmentedCubeStore`, no `point_series()` | **discharged** — real `point_series()` against a published manifest, spied in the implementation's own namespace |
> | O2 snapshot-open cost | raw `zarr.open_group()` — no manifest validation, segment inspection, metadata binding or snapshot build | **discharged** — real constructor; the raw figure is retained separately and labelled as raw |
> | H5 crossing latency vs delta span | nothing; the table is volume arithmetic | **discharged** — real `TieredCube` |
> | tail rebuild carry-forward | `v2` rebuilt from fixtures, never read from the predecessor; `carried_forward_days` is a computed number | **discharged (O2)** — a published predecessor read through `build_block`'s source map; the count comes out of that map |
>
> `open_only_first` was an artificial restriction of the harness's own loop, not a real request,
> so it never rescued the first row; what rescued it was a request that lands inside one segment,
> where `observed` and `reported` **must** differ.
`[MUT-STG]` — local and synthetic only. No VM24, SSH, `GHRSST_*` store, cron, PM2 or production
path is touched anywhere in this step.

Branch `dev2026-p5-s6-o2-local`, based on the merged tip `7902b61`.

- Harness: [`../bench/bench_p5_sizing.py`](../bench/bench_p5_sizing.py)
- Tests: [`../tests/test_phase2_p5s6.py`](../tests/test_phase2_p5s6.py) — **113/113**, committed
  **failing first**; and [`../tests/test_phase2_p5s6_o2.py`](../tests/test_phase2_p5s6_o2.py) —
  **17/17**, the O2 carry-forward path. **23 guards mutation-verified**, each mutation run
  serially from a git-clean tree.
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

## Tail rebuild — LOCAL_SYNTHETIC, through the real carry-forward path

**This withdrawal is discharged.** The previous revision said carry-forward could not be
measured because `v2` was rebuilt from fixtures, never read from the predecessor, and had no
source map — `carried_forward_days` was arithmetic wearing the label of read evidence. O2 closed
that: `measure_tail_rebuild()` now builds and **publishes** a predecessor block, prunes its days
out of the delta, and builds the successor through `ingest.build_block` with
`predecessor_path` / `predecessor_present`. `carried_forward_days` is counted out of the
returned source map.

The O2 work needed almost no new code, which is the main finding. `build_block` already
implemented the contract — resolution order delta → block → daily → hold, so a post-prune
repair beats the block's stale copy; `rebuild_source_set = predecessor_present ∪ newly_present`;
and a refusal for any unresolved day *before* a byte is written. O2 exercises that path instead
of building a second source-map truth beside it.

The row publishes numbers **only if the returned plan proves the fold happened**: every
predecessor day resolved to `block`, every newly aged day to `delta`, every present day has a
source-map entry, provenance was written, and the predecessor's digest is unchanged. Any failure
degrades the row to `DEFERRED` with no numbers. This is not decorative — the first run of the
rewired function returned `DEFERRED` because the harness had not passed `artifacts_dir` and no
provenance was written.

A block is folded at fill = C, 2C, … until it seals at S, and each position amplifies
differently, so every position is measured rather than just the first (S=90, C=30, fixture
extent):

| fill (days already in the block) | carried forward | newly aged | days out | single-fold ratio |
|---|---|---|---|---|
| 30 | 30 | 30 | 60 | 2.0 |
| 60 | 60 | 30 | 90 | 3.0 |
| 90 | — | — | — | `DEFERRED`: sealed, nothing ages in |

Measuring only the first position would have hidden a second problem: there
`carried_forward_days` numerically equals `min(C, S)`, so a harness that computed it
arithmetically would be indistinguishable from one that counted it out of the source map. At
fill=60 the two separate, and a test asserts they do.

The refused positions are refused on purpose. With `S == C`, or at fill = S, the block is
already full and there is no fold. An earlier draft returned `write_amplification: 30.0` for
that case, because a `max(1, ...)` guard turned a division by an empty count into a
publishable-looking number.

**The measured ratio is one fold, not the model.** `write_amplification` here is
`days_in_new_tail / newly_aged_days` at a single fill level — the artifact rows carry
`amplification_scope: single-fold` for exactly this reason. §10.2's `(S/C+1)/2` is a different
quantity: the **lifecycle mean** over every fold from empty to sealed. At S=90/C=30 both are
**2.0** — a coincidence of this configuration, not agreement — and they diverge
at S=45/C=30 (measured 3.0, model 1.25). A test asserts that divergence, so this section cannot
quietly become a claim that the measurement corroborates the model. It does not.

Rewrite evidence remains a **sha256 content digest**: `_tail_rebuild_probe` still runs, and a
companion test moves every mtime forward without changing a byte and asserts **0** files count
as rewritten. Timings live in the artifacts, not in this prose, and remain fixture-extent —
not production geometry, and not a capacity result.

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

> **The `free_now` figure below is the 2026-08-19 VM24 reading and is HISTORICAL.** The
> current measurement is **295.7 GiB free (2026-08-20, run `20260820T110036Z`)** —
> `bench/results/p5s8_vm24_readonly.json` and `specs/p5_critical_path_memo.md` §2 are the
> authority. The G18 verdict is BLOCKED under either reading; only the arithmetic moves.

**The earlier claim in this document that "141 GB fits with margin" was wrong, and is
withdrawn.** It compared the block size alone against free space. The gate is
`projected_free_after ≥ HARD_RESERVE`, and `additional` is never the block alone:

| term | value | source |
|---|---|---|
| `free_now` | 267 GB — **2026-08-19, historical** | `df` avail, measured; superseded by run `20260820T110036Z` (295.7 GiB) |
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

### Hold / lifecycle audit — **AUDIT ONLY, NO DELETION PERFORMED** (revision 4)

> **Revision 3 had two bypasses of its own, both now closed.** Its audit read day sets as
> two-element lists, so `set()` produced a **two-day set where 1267 days were meant** — and the
> test helper expanded the ranges *before* calling, so the test corrected the evidence and then
> checked the audit. It also used `bool()` on four raw flags, so `p5_roots_present="false"`
> coerced to `True` and bypassed the pre-P5 block entirely. Ranges now carry a declared
> `day_count` and are expanded **and verified by the audit**; every raw flag requires
> `type(x) is bool`.
>
> Expanding the real day sets made two entries **more** blocked, not less: the expired delta
> backups run to `2026-07-08` and `2026-07-26`, past base's `2026-06-26`, so base cannot serve
> those days at all — `blocked_unique_copy`. The endpoint form hid that.
>
> **Revision 2 is withdrawn too.** It kept the two expired delta backups releasable on **day-set
> coverage** and **supersession** — the same category error as revision 1, one level down.
> Coverage says which *dates* exist elsewhere; it says nothing about whether *these bytes* can
> be recovered. **Currently reclaimable drops from ~96 GiB to 0.0 GiB.**

Generated by the committed `audit_hold_lifecycle_from_evidence()` from
`bench/results/p5s6_vm24_hold_evidence.json`, so the artifact and the classifier cannot
disagree — revision 2's verdicts were assembled by hand beside the classifier, and a test now
fails if the two diverge.

#### What counts as recovery evidence

Exactly three kinds, each with required fields and exact type checks
(`RECOVERY_EVIDENCE_KINDS`): **verified value parity** (variables, `var_valid`, NaN semantics,
verification provenance), a **recorded external manifest** (source, coverage, download
procedure, verification method), or an **immutable generation** with fingerprint and provenance
supporting rollback.

Day-set coverage, supersession, `hold_until` expiry and "not the rollback target" are **not** on
that list. They are necessary; none is sufficient. `bool(x)` cannot stand in for any of them,
and a test supplies `True`, `1`, `"yes"` and `"value_parity"` to prove it.

#### Result

| entry | days | size | class | authorized? |
|---|---|---|---|---|
| daily holds | 1267 | ~2192 GiB | `repair_state_unknown_pre_p5` **+ no recovery evidence** | no |
| expired delta backup 07-09 | 40 | ~41 GiB | `blocked_unique_copy` | no |
| expired delta backup 07-28 | 52 | ~55 GiB | `blocked_unique_copy` | no |
| newest delta backup 08-18 | 52 | ~59 GiB | `referenced` (rollback target) | no |
| 2 untracked delta copies | 52 / 21 | ~41 GiB | `inconsistent_or_unknown` | no |

Each entry carries `also_blocking`, so the artifact never reads as "fix this one thing and it
releases" — for the large entries, two independent conditions are unmet.

**Pre-P5 semantics:** the P5 roots are absent, so repair state is **unchecked, not clear**, and
it blocks authorization outright. Accepting a pre-P5 release is an **operator policy**, never a
classifier default.

#### Capacity — G18 remains BLOCKED

> **The `free_now` figure below is the 2026-08-19 VM24 reading and is HISTORICAL.** The
> current measurement is **295.7 GiB free (2026-08-20, run `20260820T110036Z`)** —
> `bench/results/p5s8_vm24_readonly.json` and `specs/p5_critical_path_memo.md` §2 are the
> authority. The G18 verdict is BLOCKED under either reading; only the arithmetic moves.

| | GiB |
|---|---|
| `currently_reclaimable` | **0.0** |
| withheld pending operator policy | 2388.2 |
| `free_now` — **2026-08-19, historical**; superseded by run `20260820T110036Z` (295.7) | 267.0 |
| **`minimum_free`** (HARD_RESERVE 250 + additional 153.3) | **403.3** |
| **`recommended_free`** (a **target**, not a solution — nothing added, nothing reclaimed) | **600.0** |
| shortfall against minimum | 136.3 |

**40 GiB was the arithmetic gap in revision 2, not a practical expansion size, and it no longer
applies at all** — with nothing safely reclaimable the shortfall is the full **136.3 GiB**.
`recommended_free` adds ~200 GiB of explicit margin: at the measured 2.74 GiB/day combined
ingest that is ~70 days of headroom and room for a second block before the next capacity
decision.

`HARD_RESERVE` stays 250 GiB. Nothing was deleted, and only a real deletion followed by a fresh
`df` can move G18.

#### The operator decisions required

For **any** of the withheld 2388.2 GiB, one of:

1. verified base/hold **value parity** — variables, `var_valid`, NaN semantics, with digest
   provenance;
2. a recorded **external recovery manifest** — source, coverage, download procedure,
   verification method;
3. an **immutable generation** with fingerprint/provenance that supports recovery.

Plus, separately, a policy position on **releasing anything on a pre-P5 host**, where repair
state cannot be checked at all.

Otherwise: **expand the data volume** — minimum 403.3 GiB free, recommended 600 GiB.

### Superseded historical audit (revisions 1–3) — **DO NOT USE AS CURRENT STATE**

> Revisions 1–3 of the hold audit reached conclusions about what could be released and how much
> capacity that would recover. **All of them were withdrawn.** None of their figures is
> reproduced here — not even to contradict, because a number in a document gets quoted whatever
> the sentence around it says, and the previous version of this pointer proved that by restating
> the very figures it claimed to omit.
>
> The current state is the **revision 4** section above. The withdrawn reasoning, and why each
> revision was wrong, is in the `revision_note` fields of
> `bench/results/p5s6_vm24_readonly.json` and in commits `1ff4fd5`, `848d4bb`, `3f228ca`,
> `dc3197e`.

## Not delivered in this pass

- **O2 tail carry-forward** is now exercised and measured — see the single authoritative status
  in *Tail rebuild — LOCAL_SYNTHETIC*, and nowhere else in this document. It is
  **LOCAL_SYNTHETIC only**, at fixture extent, and a row publishes numbers **only when the
  provenance and source-map proof succeeds**; otherwise it degrades to `DEFERRED` with none.
- **Not capacity evidence.** The disk gate exists in `build_block`, but the tail measurement
  deliberately passes `hard_reserve_bytes=0` so a temp-dir fixture can run at all. Nothing in
  this document speaks to VM24's free space, which is separately **BLOCKED**.
- **Still not delivered:** API parity, snapshot-safety tests, and the
  manifest/provenance/rollback lifecycle at **production geometry**.
- **S7 integration and any production deployment work** — explicitly out of scope. Nothing here
  has been deployed, and no VM24 or production action was taken in this pass.
