# P5-S3 — tail-block builder + compaction lock: RESULTS

Status: **DONE for the parts below; two sub-parts explicitly NOT delivered and named in §7.**
`[MUT-STG]`: staging/synthetic only. No VM24 path, no `GHRSST_*` store, nothing written
outside a temp dir. The builder returns a **plan**; it never publishes.

Implements P5-S3 of [`p5_segmented_timecube_compaction_design.md`](p5_segmented_timecube_compaction_design.md) §14.

- Builder: [`../ingest/build_block.py`](../ingest/build_block.py)
- Compaction lock: [`../store/compaction_lock.py`](../store/compaction_lock.py)
- Gate wired into [`../ingest/prune_delta.py`](../ingest/prune_delta.py) and
  [`../ingest/swap_delta.py`](../ingest/swap_delta.py)
- Tests: [`../tests/test_phase2_p5s3.py`](../tests/test_phase2_p5s3.py) — **51/51 green**
- Full local suite: **449 tests OK** (17 skipped), up from 398.

**Review rounds 2 and 3** raised four and three findings; §11 and §12 record what each changed. Two of them were
defects I had introduced without noticing — the per-tile group open, and two safety gates that
were technically present but opt-in.

## 1. The write-side guard — the thing this step exists to get right

Five review rounds of P5-S1 shared one shape: **validation reading a derived view instead of
the thing itself**, so a malformed store was laundered into a well-formed one. Every one was on
the read side, because that was the only side that existed. I said at the time I expected the
same class to reappear on the write side, and that S3 would close it by construction rather
than by review. That is what this is:

> **`build → inspect → fingerprint → plan` is the only path.** The builder never computes a
> segment entry from what it *intended* to write. It re-opens what it *actually wrote*, runs
> the same strict `inspect_store_contract()` a reader runs, and derives `layout`, `day_digest`
> and `fingerprint.metadata` **from that inspection**.

So a manifest entry can only ever describe a store that passed the reader's own validator, and
a block that violates the contract produces **no plan at all** — tested by corrupting the block
mid-build (a descending `lon` axis) and requiring `BuildRefused` plus the absence of
`p5_block_plan.json`.

## 2. The two day sets (§5.5) — G13

| set | contents | source required |
|---|---|---|
| `classification_target` | the newly aged days this fold materializes | resolved as part of classification |
| `rebuild_source_set` | **every `present` day of the successor** = predecessor present ∪ newly classified present | **yes, one entry each** |
| `unknown` | the rest of the fixed calendar window | **never** |

Source authority per day, resolved fresh at every build: **delta → published block → daily →
hold → NetCDF** — the same order the read path applies, so a fold materializes what the API is
serving today. A day repaired back into delta after a prune therefore beats the predecessor
block's stale copy instead of being frozen out.

**F13 end to end**: v1 (days A from delta) → prune A from delta → v2 (A from the **block**, B
from delta) → prune B → v3 (A+B from the block, C from delta) → seals at 90 days with
`unknown: []`. The provenance records `block` for carried-forward days and `delta` for new ones.

## 3. Refusals, all before anything is written

- an unresolved `rebuild_source_set` day → refuse, and `out_path` does not exist afterwards;
- a day inside the protected `SPATIAL_WINDOW_DAYS` window → refuse (§7.1);
- an existing `out_path` without a resumable checkpoint → refuse (a block path is written once);
- the disk precheck failing → refuse.

## 4. The compaction lock (§7.1b) — G17

A dedicated `flock` on `p5_compaction.lock`, held for the whole build, separate from the ingest
lock so the twice-daily delta append is unaffected. `prune_delta` and `execute_swap_plan` now
take `compaction_lock_path=` and **refuse non-blocking** (`reason: compaction_lock_held`) rather
than wait — a prune blocked for hours reads as a hang, and the operator needs to reschedule.

Two details that matter more than the lock itself:

- **`O_CLOEXEC`**: the fd is closed across `exec`, so a spawned program cannot outlive the
  supervisor still holding the lock. Stated precisely, because the flag is narrower than it
  looks: it does **not** stop a plain `fork` from inheriting the descriptor. "Process death
  releases the lock" (§7.1b-1) therefore holds **because the builder is single-process and
  forks nothing**, with `O_CLOEXEC` covering the `exec` case — not because of the flag alone.
  A future fork-based worker pool must close the fd in the child or move the lock to a
  supervisor-only fd; this is recorded in the module docstring next to the flag.
- **the publish-time assertion checks the fd *and the inode***. `rm` + recreate gives a new
  inode another process can lock freely while we hold the orphaned one — the only two-writer
  state reachable here. Both the deleted and the replaced cases are tested.

The status JSON is **observability only**; nothing branches on it.

## 5. The disk gate (§7.1c) — G18

Charges **additional allocation only** — staging block + temp + measured growth.
`pinned_existing` (predecessor and superseded blocks) is reported for the lifecycle forecast and
**never subtracted twice**, because `free_now` already excludes it. A test asserts that passing
a terabyte of `pinned_existing` changes neither `additional_bytes` nor
`projected_free_after_bytes` — the v3-sealing case that a double-charging gate would refuse.

## 6. Memory and resume

- **Tiled reads.** Nothing materializes a full slab. The gate compares peak RSS at 128² and
  512² (16× the area) and requires growth under 200 MB; a full-slab implementation would fail
  it. This is the P4-S6 OOM lesson made executable.
- **Resume.** An interrupted build leaves an fsync'd per-`(day,var)` checkpoint and an error
  JSON, and no plan. Resuming completes it, and the values are asserted correct **by date**,
  not merely present.
- **Artifacts.** Progress JSONL fsync'd per event; `p5_block_plan.json` written **only** on
  success.

## 7. Deliberately NOT delivered here

Naming these rather than letting the step look complete:

0. **The NetCDF source fallback (§7.1a, deep recovery).** It was listed in `SOURCE_ORDER` with
   no resolver and no reader behind it, so a day only NetCDF could have supplied looked like a
   missing *day* rather than a missing *feature*. It is now **removed from `SOURCE_ORDER`**;
   such a day refuses loudly, naming the date. Recovery scope, not S3.

1. **The repair WAL (§7.5a-1) and the identity-based prune gate (E1/E2/E3).** The builder emits
   `build_provenance.materialized_repairs` as an **empty map** — the structure is present, the
   WAL that would populate it is not. Until it exists, the §7.5a prune authorization cannot be
   enforced, so **delta prune must not be run against a corrected day** on the strength of this
   step.
2. **The corrective refold of a sealed block (§7.8) and its three-phase verification (§7.8a).**
   `build_block` can rebuild any window, including a sealed one, but nothing yet orders
   *repair → refold → publish → verify → only then prune*.

Both belong to a follow-on step. F13c (`confirmed_missing` promotion), F14 (unsealed→sealed
transition) and F16b (fencing split-brain) are partially exercised here and complete there.

## 8. Risk register

| risk | status after S3 |
|---|---|
| **R1 / R1a** | unchanged from S2 (R1 `OPEN` pending S5/G10; R1a `CLOSED`) |
| **R4** outage + bulk backfill | the compaction-ordering half now has a builder that refuses to fold inside the protected window; the full F18 path still needs the S3 follow-on |

## 9. Reproduce

```bash
dev2026/.venv/bin/python -m unittest dev2026.tests.test_phase2_p5s3
dev2026/.venv/bin/python -m unittest discover -s dev2026/tests -p "test_*.py"
```

## 10. Mutation verification

Every new guard was disabled in turn and the suite re-run; all twenty-five fail. A twentieth mutation survived by design and is discussed below the table.

| guard disabled | result |
|---|---|
| write-side: derive the entry from intent instead of the inspection | FAILED (12 errors) |
| `rebuild_source_set` reduced to newly-aged only | FAILED (3 + 1) |
| source order: block before delta | FAILED |
| fold cutoff (protected window) | FAILED |
| unresolved day refuses | FAILED (2) |
| disk gate charges `pinned_existing` | FAILED |
| refuse to reuse a block path | FAILED |
| lock inode assertion | FAILED |
| publish requires the lock held | FAILED |
| `prune_delta` compaction gate | FAILED |
| `execute_swap_plan` compaction gate | FAILED |
| handle cache disabled (opens per tile) | FAILED |
| isolation requirement removed | FAILED |
| positive-reserve check removed | FAILED |
| pre-write lock assertion removed | FAILED |
| source contract inspection skipped | FAILED |
| daily-day inspection skipped | FAILED |
| grid-uniformity check removed | FAILED |
| `netcdf` re-advertised in `SOURCE_ORDER` | FAILED |
| grid identity reduced to `(ny, nx)` | FAILED (3) |
| grid identity ignores dtype | FAILED |
| uniform-grid comparison removed | FAILED (2 + 1) |
| inspection open not counted | FAILED (5) |
| day-index opens its own handle again | FAILED |
| daily inspection opens a second handle | FAILED |

**One mutation SURVIVED, and it should have.** Relaxing `has_var` from `flag is True` to
`bool(flag)` leaves the suite green. That is not a coverage hole: `inspect_store_contract`
(`block_manifest.py:549`) rejects any `var_valid` entry that is not an exact `bool`, so nothing
non-bool can reach `has_var` in the first place, and the two expressions are provably equivalent
there. The strict form stays as defence-in-depth against a future path that skips the
inspection — but it is **not** a gated guard, and is recorded here as such rather than listed
above as if a test were holding it.

## 11. Review round 2 — what the four findings changed

### 1. [High] The source group was re-opened on every tile

`_read_tile` and `_source_has_var` each called `zarr.open_group()`. At production geometry
(17999x36000, 256-tiles, 90 days, 3 vars) that is **~2.7M opens for one fold** — enough to put
compaction back into the hours it exists to avoid. This was mine and the tests did not see it,
because a 32x32 fixture with one tile per var opens exactly as often either way.

`_SourceReader` now opens each distinct source **once** and holds the handle; `opens` is
exposed on the plan and in the progress journal, so it is a gated quantity rather than an
assumption. Gates: opens are `1` for a single delta source regardless of tile size, `4` for
delta + three daily days, and **do not change when the tile shrinks** — the exact scaling that
regressed.

**A wall-clock gate was tried here and removed, deliberately.** At fixture scale tile=4 is
~33x slower than tile=64 *with the cache in place*, because per-slice overhead dominates when
the grid is 32x32. A time-ratio gate would therefore have failed on correct code while proving
nothing about opens. The open count is exact, causal, and is the quantity that becomes hours;
the test states this rather than leaving a silently weaker gate in place.

### 2. [High] The lock and the disk reserve were opt-in

`lock=None` and `hard_reserve_bytes=0` were defaults. Both guards existed and both were tested
— but a caller who simply *forgot* them got a build with no isolation and a disabled disk gate,
and nothing said so. A guard you can omit by accident is not a guard.

Both are now **required keyword arguments with no default**. `lock` must be a *currently held*
`CompactionLock` and `hard_reserve_bytes` must be `> 0`; either failure refuses before a byte is
written. The lock is additionally asserted held **before the first write**, not only before
publish — losing it after three hours of building is worth catching, but catching it at minute
zero is better.

`unsafe_skip_isolation=True` is the single, greppable, deliberately ugly waiver, used by the
tests that are not about isolation (via one named shim at the top of the test module, so no test
silently passes `lock=None`). `TestIsolationIsMandatory` calls the strict entry point directly,
including a test that the fully guarded path still *builds* — a mandatory gate that refuses
everything would also pass every negative test.

### 3. [High] Source stores were never strictly inspected

The write-side guard inspects the block it wrote. As the reviewer put it, that proves the
**output** is structurally legal; it proves nothing about whether the **input** was semantically
sound. A source with a corrupt axis or a wrong dtype gets copied faithfully into a block that
then inspects perfectly clean — the S1 laundering pattern, relocated.

`resolve_source_map` results are now inspected before anything is read: `inspect_store_contract`
for delta and predecessor-block sources, and a new `inspect_daily_day` for daily/hold groups
(shape `(1,ny,nx)`, `float32`, axes 1-D, finite and strictly increasing). `has_var` reads
`var_valid` **from the inspection** and requires `flag is True` rather than truthiness — the
same strictness the read side arrived at, though see §10 for why that last part is redundant
rather than load-bearing.

One thing the finding did not name, which fell out of doing it: nothing checked that the sources
**agree on a grid**. A block has one `lon`/`lat` pair, so a mixed-grid source set would write
each source's tiles into the same axes and interleave two geographies day by day — and the
output guard, which sees only one axis pair, would pass it. `assert_uniform_grid()` now refuses.

### 4. [Medium] NetCDF was advertised but not implemented

Removed from `SOURCE_ORDER` and declared undelivered in §7.0. Implementing it was the other
option; declaring it is the honest one for this step, and the refusal now names the missing day
instead of implying the source was consulted.


## 12. Review round 3 — what the three findings changed

### 1. [High] `assert_uniform_grid` compared size, not identity

I wrote the grid check as `(ny, nx)`, which is the version of the check that catches the case
I happened to have a fixture for. Two sources can both be 18000x36000 and be shifted half a
cell apart, or use 0..360 where the other uses -180..180 — same shape, same dtype, same cell
count, different Earth. Those are precisely the cases that produce plausible-looking wrong data
instead of an error, so a size comparison guards the easy half and leaves the dangerous half
open.

The comparison is now over the **S1 grid identity** — `lon`/`lat` value digest, shape and dtype
— reached through one shared `grid_identity()` helper. This matters beyond tidiness: the daily
path and the cube path are inspected by different code, so if they did not compute the digest
the same way, every legitimate mixed-source fold would look like a grid mismatch. `inspect_daily_day`
therefore routes its axes through `block_manifest._axis`, the same validator the cube inspection
uses, rather than reimplementing the rules and the digest convention beside it.

Four tests: same-size-shifted-axes, same-size-different-longitude-convention, the existing
different-size case, and — the one that keeps the guard honest — a **matching** delta+daily fold
that must still succeed. The refusal names the differing fields (`lon_digest`, …) and prints both
grids, because "sources disagree on the grid" without saying how is not actionable at 3am.

### 2. [Medium] `source_group_opens` under-counted real opens

The reviewer was right, and the real number was worse than the finding said. Both
`inspect_store_contract` and `inspect_daily_day` opened the group themselves, *and*
`resolve_source_map`'s day-index read opened the delta a third time — none of them counted.
A performance gate reading that number was under-stating the cost it exists to bound.

Two changes. The day-index read and the daily inspection now **reuse the reader's handle**
instead of opening their own. `inspect_store_contract` keeps S1's path-only signature, so its
open is real and is now **counted explicitly** rather than hidden.

The important part is the test, which does not trust the instrumentation at all: it patches
`zarr.open_group` on the zarr module — the one both `build_block` and `block_manifest` resolve
through — records every open of a source path, and asserts the reported count **equals** the
observed count. That test is what found the third delta open; the finding as written would have
left it there. Reported counts are now 2 for a lone delta (inspection + handle) and 5 for
delta + three daily days, and disabling any of the three sharing/counting fixes fails it.

### 3. [Low] The `O_CLOEXEC` comment claimed more than the flag delivers

`O_CLOEXEC` closes the fd across `exec`. It does **not** stop a plain `fork` from inheriting
the descriptor and the flock reference. Nothing here forks, so no current path is affected, but
the comment said "a child process must NOT inherit this fd", which is the kind of statement
someone later builds a fork-based worker pool on top of.

Reworded in `compaction_lock.py` and in §4 above: "process death releases the lock" (§7.1b-1)
holds **because the builder is single-process and forks nothing**, with `O_CLOEXEC` covering the
`exec` case — not because of the flag alone. The requirement a future worker pool would inherit
(close the fd in the child, or move the lock to a supervisor-only fd) is recorded next to the
flag rather than in this document, where it would not be read.