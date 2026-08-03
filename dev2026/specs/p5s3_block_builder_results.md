# P5-S3 — tail-block builder + compaction lock: RESULTS

Status: **DONE for the parts below; two sub-parts explicitly NOT delivered and named in §7.**
`[MUT-STG]`: staging/synthetic only. No VM24 path, no `GHRSST_*` store, nothing written
outside a temp dir. The builder returns a **plan**; it never publishes.

Implements P5-S3 of [`p5_segmented_timecube_compaction_design.md`](p5_segmented_timecube_compaction_design.md) §14.

- Builder: [`../ingest/build_block.py`](../ingest/build_block.py)
- Compaction lock: [`../store/compaction_lock.py`](../store/compaction_lock.py)
- Gate wired into [`../ingest/prune_delta.py`](../ingest/prune_delta.py) and
  [`../ingest/swap_delta.py`](../ingest/swap_delta.py)
- Tests: [`../tests/test_phase2_p5s3.py`](../tests/test_phase2_p5s3.py) — **28/28 green**
- Full local suite: **426 tests OK** (17 skipped), up from 398.

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

- **`O_CLOEXEC`**: the fd is not inherited by children, so "process death releases the lock"
  stays true (§7.1b-1). If a child inherited it, killing the supervisor would leave it held.
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

Every new guard was disabled in turn and the suite re-run; all eleven fail.

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
