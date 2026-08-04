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
- Tests: [`../tests/test_phase2_p5s4.py`](../tests/test_phase2_p5s4.py) — **88/88 green**
- Full local suite: **544 tests OK** (17 skipped), up from 456.

**Review rounds 2 and 3** raised five findings and one; §8–§9 record what each changed.

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
read at minute zero is a *claim*. Under the lock the claim is re-checked against **identity, not
date membership** (§8.2): the live delta must have unique days, must **resolve to the same store**
the build read, and must still hold each folded day at the **same physical index**. Non-delta
sources are checked at their exact `YYYY/MM/DD` group, never at the root.

The map being re-verified is the segment's own `build_provenance.sources` — the copy that gets
published — which is exactly what P5-S3 round-4's provenance fields were added for. They were
introduced for auditability; they are now what this commit-time guard is built on.

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
   adversarially tested rather than unit-tested. Carried into that scope explicitly: the
   ambiguous state when the audit log fails to write **after** the manifest has committed
   (§9), and the recovery rule that the manifest is authoritative while the hold log is
   reconstructible from the generation archives.

## 6. Reproduce

```bash
dev2026/.venv/bin/python -m unittest dev2026.tests.test_phase2_p5s4
```

## 7. Mutation verification

Every guard was disabled in turn and the suite re-run; **all 50 fail**. One further mutation survives by construction and is discussed below the table.

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
| guard falls back to the plan's `source_map` | FAILED |
| a segment without provenance may publish | FAILED |
| guard reads the plan's map instead of the provenance | FAILED |
| delta `realpath` not compared | FAILED (2) |
| delta physical index not compared | FAILED |
| daily/hold checked at the root, not the day group | FAILED |
| lifecycle `apply` without the ingest lock allowed | FAILED |
| lifecycle `apply` does not take the lock | FAILED |
| a failed publish does not undo the rename | FAILED |
| `hold_move` logged before the manifest commits | FAILED |
| a missing `current_path` silently tolerated | FAILED |
| `repair_id` suffix not required (parser) | FAILED |
| intent suffix need not equal its own `seq` (parser) | FAILED |
| append does not enforce the suffix | FAILED (2) |
| replay compares only the fingerprint (parser) | FAILED |
| replay compares only the fingerprint (append gate) | FAILED |
| field types not checked | FAILED (4 + 1) |
| execute trusts the plan's map (no re-bind) | FAILED (3 + 1) |
| empty map treated as absent (the old `if top`) | FAILED (2) |
| non-dict `source_map` tolerated | FAILED |
| authority taken from a planning-time copy | FAILED (3 + 4) |
| plan/manifest segment identity unchecked | FAILED |
| writer coerces `payload` with `dict()` | FAILED |

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


## 8. Review round 2 — five findings

### 1. [Blocker] The guard read `plan["source_map"]`, not the segment's provenance

Two copies of the same map, and the guard checked the one that is **not** published. A plan
whose top-level copy disagreed with `build_provenance.sources` would pass a guard run against
the wrong map while the manifest recorded something else — and the provenance copy is the one an
auditor reads back.

`plan_publication` now derives the guard's map from `segment.build_provenance.sources`, refuses
when a supplied top-level copy disagrees (naming the differing days), and refuses a segment that
carries **no** provenance at all — there is nothing to re-verify, and a block published without
provenance cannot be shown to have read what it claims. A test passes a plan with `source_map`
removed entirely and asserts the guard still catches a retargeted delta.

### 2. [Blocker] Delta staleness compared dates, not identity

"Day D is in the live delta" says nothing about *which* delta or *where in it*. Two cases the
membership check could not see, both of which leave every date present:

- **the alias was retargeted.** The delta path is stable; swap replaces the store behind it. Same
  dates, different bytes. Now compared by `realpath` against the recorded `source_path`.
- **the day moved physical index.** `attrs['days']` is append order after a backfill (P4-S4 §2),
  so a rebuild can preserve the date set and reorder it — meaning the block read a different slot
  than it records. Now compared against the recorded `source_day_index`.

This is the second time P5-S3 round 4's provenance fields have turned out to be the thing that
makes a later check possible. They were added for auditability; they are now what the commit-time
guard is built on.

### 3. [Blocker] daily/hold sources were checked at the root

`os.path.exists(daily_root)` is true for years and says nothing about whether `YYYY/MM/DD` is
still there. `_source_group_path()` now resolves the exact group. The test deletes one day group
and asserts the root is still present, so it cannot pass for the old reason.

### 4. [Blocker] `advance_lifecycle(apply=True)` held no lock, and rename/publish could split

Two defects. It reads the live manifest, mutates `superseded[]` and publishes a new generation,
so a concurrent publication would silently drop one writer's changes; and a rename that succeeded
before a publish that failed left the manifest naming a `current_path` that no longer holds the
block — on disk and unreachable at once, since `current_path` is exactly what §9.3a
restore-before-rollback looks up.

`ingest_lock_path` is now **required** for `apply=True` and held across both the renames and the
publish. Every rename is **undone** if the publish raises, and `hold_move` is logged only after
the manifest commits, so the audit trail never claims a move that was rolled back. A
`current_path` that does not exist on entry is **refused, not repaired** — a crashed apply and a
deletion produce the same symptom and need different answers, and guessing between them is how a
rollback target quietly disappears.

Requiring the lock path is not the same as taking it, so that is tested separately: `flock`
conflicts across distinct open file descriptions even inside one process, so the test holds the
real lock and observes that the lifecycle waits and that the generation does not move.

### 5. [Blocker] WAL replay compared only the fingerprint; types and id monotonicity unchecked

- **Replay** now compares the **whole canonical payload**. Two commits agreeing on the
  fingerprint but disagreeing on the variables or the operator are two different claims about
  what happened; picking one silently is the ambiguity this log exists to remove. Enforced in
  both the append gate and the parser, because a hand-edited log never passes the gate.
- **Field types** are validated before any field is used. A checksum proves a record is
  unmodified, not that it was well-formed when written — `seq` as a float or `payload` as a list
  passes the checksum and then compares unequal to everything downstream, and in this file an
  unequal comparison means *authorized*.
- **`repair_id` monotonicity.** The spec calls `UUID + seq` "acceptable and recommended"; this
  implementation makes it **required**, because a recommendation cannot be checked. With
  `-<seq of the intent record>` mandatory, uniqueness follows from `seq` being gap-free and
  ordering follows from `seq` being monotonic. An opaque id cannot be ordered at all — UUIDs have
  no ordering — so "latest repair" would otherwise rest on nothing. `open_repair()` allocates
  conforming ids **in the same lock hold as the append**; allocating in one hold and appending in
  another leaves a window where a concurrent writer takes that seq.

  *This is a tightening beyond the spec's wording and is flagged as such.*

### Mutation survivors, again

Three mutations survived the first round-2 run and each was a test gap, not a code gap: the
lock-exclusion mutation had no test (requiring the path was tested; taking the lock was not), and
both `repair_id` suffix rules were tested only through the **append gate**, so removing them from
the **parser** changed nothing — a hand-edited log would have bypassed the whole rule. All three
now have tests and all three fail.


## 9. Review round 3 — one finding, and one deliberate deviation

### [High] The source map was *checked* at planning time, not *bound* at publication

`plan_publication` validated the two maps against each other and `execute_publication` then used
`plan["source_map"]` directly. A plan exists to be inspected, serialized and read back by a
human between those two calls — so a top-level `source_map` edited in that window sailed through
a guard that had already been satisfied, and the block would publish with one provenance while
having been guarded against another. Validating once is a check; re-deriving the authority at
the moment of commit is a binding, and only the second survives an edit in between.

`_validate_plan_source_map()` is now called from **both** entry points, and at publication the
authority is re-derived from the **manifest about to be published** — located by
`new_segment_id`, with a refusal if the plan's manifest does not contain exactly that segment
(a plan whose manifest does not describe the block it names is not a plan for that block).

`{}` is no longer waved through. `plan.get("source_map")` collapses "the field is absent" and
"the caller supplied an empty map" into the same falsy value, but an empty map is a positive
claim that the block read nothing — for a block with days, a claim to refuse rather than skip.
A module-level `_ABSENT` sentinel separates them: absent falls back to the segment's own map
(and is still fully guarded); empty, or any other mismatch, refuses.

Nine tests, including the workflow the binding exists for — plan → JSON file → reload → execute
— and one that edits the **manifest's own** provenance and asserts the guard verdict changes,
which is what proves the guard reads the segment being published rather than a planning-time
copy. Every refusal asserts `manifest.json` is byte-identical.

### [Non-blocking] The WAL writer laundered `payload`

`dict(payload)` accepts a list of pairs and writes a record that looks perfectly well-formed;
the reader would have no way to know the writer passed something else. The parser was strict
about types while the writer was not, which means the strictness only applied to files nobody
wrote through this function. `append()` now rejects a non-mapping payload outright.

**Deviation from the review's wording, flagged deliberately:** the check is `isinstance`, not
`type(payload) is dict`. An ordinary dict subclass canonicalizes identically, so rejecting it
would cost callers without closing anything; the hazard is a non-mapping *sequence*, and that is
what this blocks. Both cases are tested.

### [Non-blocking, carried to P5-S5] `_append_log` runs after the manifest commits

If the audit-log write fails after `bm.publish` succeeds, the manifest is updated and the hold
log has no record of it. This is deliberate ordering — logging *before* the commit would produce
the opposite and worse ambiguity, a trail claiming a move that was rolled back (§8.4) — but it
is a real ambiguous state and it is **not** resolved here. Added to §5 as P5-S5 crash-proof
scope: the recovery answer is that the manifest is authoritative and the log is reconstructible
from generation archives, but that has to be demonstrated, not asserted.

### A mutation that survives by construction

Making `staleness_guard` read `plan.get("source_map") or source_map` instead of the re-bound
`source_map` leaves the suite green — correctly. `_validate_plan_source_map` raises **before the
ingest lock is taken**, so by the time the guard runs the two are equal by construction and
nothing between them mutates the plan. That mutation is redundant code, not an untested guard,
and it is recorded here rather than listed above as if a test were holding it.