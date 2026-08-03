# P5-S4 — atomic manifest publish + lifecycle + repair WAL: RESULTS

Status: **DONE for the parts below; §7 names what is explicitly NOT delivered.**
`[MUT-STG]`: staging/synthetic only. No VM24 path, no `GHRSST_*` store, nothing written outside
a temp dir. Nothing here hard-deletes anything.

Implements P5-S4 of [`p5_segmented_timecube_compaction_design.md`](p5_segmented_timecube_compaction_design.md) §14,
plus the §7.5a-1 repair WAL, which the reviewer correctly identified as a **lifecycle
prerequisite** rather than a follow-on: without it the corrected-day prune gate cannot exist,
and without that gate the lifecycle can retire a block that is the only evidence a correction
was folded.

- Repair WAL: [`../store/repair_wal.py`](../store/repair_wal.py)
- Publish / lifecycle / rollback: [`../ingest/publish_manifest.py`](../ingest/publish_manifest.py)
- Tests: [`../tests/test_phase2_p5s4.py`](../tests/test_phase2_p5s4.py) — **55/55 green**
- Full local suite: **511 tests OK** (17 skipped), up from 456.

## 1. The repair WAL (§7.5a-1) — identity, never time

The spec's round-5 draft made the prune gate a timestamp comparison: "the block was built after
`repaired_at_utc`". Round-6 replaced it, and the reason is the whole design of this file:

> Build time does not establish that the build **consumed** the corrected value. If the delta
> repair becomes visible *before* its log line is appended, a fold in that window reads the
> corrected value and records nothing — or worse, reads the **old** value while the
> later-written timestamp makes it look authorized. **Ordering is not identity.**

So `prune_authorization()` compares `repair_id`s and fingerprints and nothing else. That claim is
tested behaviourally, not by grepping the source: a materialized entry carrying
`built_at_utc: 2099-…` and `block_newer_than_repair: True` but the **wrong id** is refused, and
two logs identical except for every `at_utc` produce identical verdicts.

*(My first version of that test read the function's source text looking for the word
"timestamp". It failed on the docstring, which says timestamps are not consulted. A guard that
inspects prose rather than behaviour is the pattern this project has repeatedly had to remove;
it was replaced rather than reworded.)*

### What it refuses

| condition | scope | rationale |
|---|---|---|
| torn final line | **all prune** | the torn record's `day` is unknowable, so no day can be shown unaffected |
| `record_checksum` mismatch | **all prune** | a corrupt record's own `day` field cannot be believed either |
| `prev_checksum` chain break (wrong value, or `null` at `seq > 1`) | **all prune** | the chain is what makes the log verifiable end to end |
| `seq` gap | **all prune** | records are missing |
| unknown / missing field | **all prune** | a different schema version or a hand-edit; not the record we validated |
| conflicting terminals, or a terminal with no intent | **that day** | the rest of the chain is intact; refusing everything would be an overreaction |
| open `repair_intent` | **that day** | unknown state, refused, **no timeout and no automatic resolution** |

Fail-closed here costs disk, not correctness: the affected days stay in delta, where they are
served correctly. Only compaction pauses.

**The parser never skips a bad line.** Skipping is precisely the behaviour that would silently
authorize a prune, so it is tested directly: a corrupt *early* record must make a later, valid,
well-formed record for a *different* day unusable.

**Writers validate before they append**, with no write-only fast path, and a refused append
leaves the file **byte-unchanged** (asserted on bytes). Valid records are never appended after a
corrupt tail — that would bury the corruption under legitimate-looking history and make the
administrative `log_tail_repaired` recovery ambiguous about what was lost.

### The four §7.5a crash boundaries

All four are tested and all four refuse: intent written / repair never started; delta swapped
but not committed; committed but the corrective refold not published; a later repair superseding
what the block materialized.

## 2. Publication (§7.4) — the commit point is `os.replace`

`plan_publication` is **pure with respect to the live manifest** — it reads and returns
generation `N+1` in memory. `execute_publication` re-checks it under the **ingest lock** and
commits. Splitting them is what makes "validated-plan-only" mean something: a plan can be
inspected, diffed and refused by a human before anything touches disk.

**The staleness guard is the point of the lock.** A build runs for hours *outside* it; what it
read at minute zero is a *claim*. Under the lock the claim is re-checked: the live delta must
have unique days, and every day the block folded **from delta** must still be in the live delta.
Days folded from block/daily/hold are checked against their recorded `source_path` instead —
which is exactly what P5-S3 round-4's provenance map was added for, now load-bearing rather than
merely auditable.

Every refusal path leaves `manifest.json` **byte-identical**, asserted on bytes rather than on
generation number: stale delta, drifted manifest generation, a missing referenced block, a
running compaction.

**Publishing changes no served value.** The manifest does not describe delta (§4.0) and delta
still wins for the folded days, so publication is a pure base-side operation. A day blocked by
an open repair intent therefore does **not** block a publish — tested, because the tempting
mistake is to wire the prune gate into publication and quietly stall compaction.

## 3. The superseded lifecycle (§8.5) — the one deletion hazard

`referenced → releasable → held`. Nothing here hard-deletes; `held` is terminal and deletion is
an explicit ops act after `hold_until_utc`.

A superseded block has **three** live uses after it leaves `segments`, and the grace period
exists for all three, not just the first:

1. an in-flight snapshot still references it (release early → the S8a *silent fabricated null*);
2. the next tail fold reads its carried-forward days from it (§7.1a);
3. §9.3 needs it as the rollback target — so `release_after_utc` must exceed the realistic
   discover-a-defect window, not merely the refresh TTL.

`apply=False` is the default: the move-to-hold is the one action that touches bytes, so it does
not happen as a side effect of asking what would happen. Each applied transition publishes a new
manifest generation, so the lifecycle is as auditable as a fold. A block still named by the live
generation's `segments` is **never** released — only reachable via a hand-edited manifest, which
is exactly when it matters.

## 4. Rollback (§9.3, §9.3a) — restore, then repoint

`bm.rollback_to` copies the archive, verifies the copy, fsyncs, and `os.replace`s. It never
`os.replace`s the archive itself — that would *consume* the immutable record a second rollback,
a forward-recovery or an audit needs. Asserted on archive bytes before and after.

`restore_before_rollback` runs **first**, and the ordering is not stylistic: replacing
`manifest.json` first would publish a generation whose snapshot build immediately fails closed,
leaving the service pinned to a stale in-memory snapshot with **no valid manifest on disk**.

A restored block is verified against generation `N`'s declared `day_digest`. A mismatch is a
**hard stop** — the block goes back to hold, the pointer never moves, and a human triages. There
is no restore-anyway. If a referenced block was hard-deleted, rollback to that generation
reports itself unavailable rather than half-completing.

## 5. Deliberately NOT delivered here

1. **The sealed-block corrective refold (§7.8) and its three-phase verification (§7.8a).** The
   WAL now *detects* that a block carries a stale repair, and refuses. Nothing yet orders
   *repair → refold → publish → verify → only then prune*. F13d belongs to the next step.
2. **Wiring the E1 gate into `prune_delta`.** `prune_outlook()` reports the verdict; the prune
   executor does not yet consult it. Until it does, **delta prune must not be run against a
   corrected day** on the strength of this step.
3. **E2 sampled value parity.** E1 is the deterministic check and is delivered; E2 (seeded
   point-wise comparison of the delta day against the base-resolved day) is not. It is the only
   net that would catch a repair performed **without** a WAL intent — a process violation, but a
   possible one.
4. **Snapshot refresh on publication (§7.4 step 5) and the §7.6 post-publication probes.** This
   step commits the manifest; it does not yet refresh the serving snapshot or run the probes.
5. **Crash-injection and concurrency proof.** That is P5-S5 and is where these guarantees get
   adversarially tested rather than unit-tested.

## 6. Reproduce

```bash
dev2026/.venv/bin/python -m unittest dev2026.tests.test_phase2_p5s4
```

## 7. Mutation verification

Every guard was disabled in turn and the suite re-run; **all 27 fail**.

| guard disabled | result |
|---|---|
| parser skips a bad line instead of failing | FAILED (9) |
| torn final line accepted | FAILED |
| `record_checksum` not verified | FAILED (2) |
| `prev_checksum` chain not verified | FAILED |
| `seq` gap not detected | FAILED |
| unknown/missing fields tolerated | FAILED |
| open intent no longer blocks | FAILED |
| an older materialized repair accepted | FAILED (2) |
| fingerprint mismatch accepted | FAILED |
| a missing materialized entry accepted | FAILED (2) |
| writer appends after a corrupt tail | FAILED |
| conflicting terminal accepted | FAILED (2) |
| terminal without an intent accepted | FAILED |
| staleness guard disabled | FAILED (2) |
| duplicate delta days tolerated | FAILED |
| delta drift tolerated | FAILED |
| generation-moved check removed | FAILED |
| missing referenced block tolerated | FAILED |
| compaction lock ignored at publish | FAILED |
| `release_after_utc` grace ignored | FAILED |
| a still-referenced block may be released | FAILED |
| `held` is no longer terminal | FAILED |
| rollback skips restore-before-repoint | FAILED |
| restored-block `day_digest` not verified | FAILED |
| supersedes a segment that is not published | FAILED |
| `apply` defaults to True | FAILED |
| parser `_fail` becomes a no-op | FAILED (9) |

**Two mutations initially survived, and both were my tests passing for the wrong reason** — the
same failure mode the last three review rounds found, caught here by the mutation run rather
than by a reviewer:

- **torn final line.** My fixture appended `{"seq": 3, "repair_id": "r2"` — invalid JSON, so the
  *parser* rejected it and the missing-newline check was never exercised. The real crash lands
  on the newline boundary: a complete, checksum-valid, chain-valid record with no terminating
  byte. Added as its own test, with the validity of everything-but-the-newline asserted as a
  precondition.
- **`seq` gap.** Dropping the first record trips the `null`-at-`seq`-1 rule; dropping a middle
  one breaks `prev_checksum`. Either way the *chain* check fires first. The test now hand-builds
  a log where `seq` jumps 1 → 3 while every `prev_checksum` still points at the record that
  actually precedes it, and asserts the message names the seq gap and **not** the chain.
