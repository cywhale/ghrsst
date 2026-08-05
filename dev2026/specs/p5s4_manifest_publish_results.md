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
- Tests: [`../tests/test_phase2_p5s4.py`](../tests/test_phase2_p5s4.py) — **133/133 green**
- Full local suite: **589 tests OK** (17 skipped), up from 456.

**Review rounds 2–7** raised five, one, three, two, two and two findings; §8–§13 record what each changed.

**This step does not claim crash safety.** It claims that each individual operation either commits or leaves the live manifest byte-unchanged. Two states are known-ambiguous and are P5-S5's job, not this step's (§10.3).

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

Every guard was disabled in turn and the suite re-run; **all 84 fail**. One further mutation survives by construction and is discussed below the table.

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
| `source_kind` not restricted to `SOURCE_ORDER` | FAILED (2) |
| index accepted via `int()` coercion | FAILED (4) |
| `source_path` not required absolute | FAILED (3) |
| record field set not checked | FAILED |
| map need not cover the segment's present days | FAILED |
| provenance not bound to the block on disk | FAILED |
| block binding compares nothing | FAILED |
| build artifact not cross-checked | FAILED |
| build artifact segment identity unchecked | FAILED |
| validator not run at publication | FAILED (5) |
| `payload` falsey-subclass emptied | FAILED |
| segment fields not re-derived from the block | FAILED (5) |
| grid not compared to the block | FAILED |
| grid shape ignored | FAILED |
| carried-forward entries not compared | FAILED |
| block sources not resolved against published blocks | FAILED (3) |
| block source index not compared | FAILED |
| block source without a resolver allowed | FAILED |
| daily/hold index not pinned to 0 | FAILED |
| unpublished path accepted as a block source | FAILED |
| no re-composition at publication | FAILED (8 + 1) |
| re-composition compares only the segment ids | FAILED (5 + 1) |
| too many fields treated as free | FAILED (6) |
| uncomposable generation not reported | FAILED |
| compaction lock optional again | FAILED |
| compaction lock never consulted | FAILED |
| publish the plan's manifest instead of the recomposed one | FAILED (5) |
| deadlines carried over from the plan | FAILED (3 + 1) |
| deadlines computed from the planning clock | FAILED (2) |
| release/hold policy args ignored | FAILED |
| audit log reads the plan's generation | FAILED |
| audit log reads the plan's `superseded_now` | FAILED |
| result reports the plan's generation | FAILED |
| `generation_id`/`created_utc` taken from the plan | FAILED (40 + 13) |

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

## 10. Review round 4 — three findings

### 1. [High] The provenance map was compared, never validated

Rounds 2–3 made the two copies agree and bound the authority to the manifest being published.
Both are about *agreement*. Neither asks whether the map is **well-formed**, and neither reaches
outside the document — so anyone able to edit a plan could edit both copies, recompute
`manifest_checksum`, and hand the guard a self-consistent map that need not describe the actual
build.

`validate_source_map()` now checks each record on its own terms. The one that mattered most:
`source_kind` must be one of the resolver's own `SOURCE_ORDER` — an unrecognised kind fell
through `staleness_guard`'s `else` branch and was silently treated as a **non-delta** source,
skipping the realpath and index checks that a delta day requires. That is a bypass of the round-2
fix, reachable by writing `"netcdf"` into one entry.

Also: exactly the three fields `build_block` emits; `source_day_index` a **raw** non-negative
`int` (not `int(x)` — the coercion accepts `"3"`, `3.9` and `True`, so a string index would
compare unequal to the live index and the refusal would read as *drift* rather than as malformed
input); `source_path` a non-empty absolute path, since it is compared against a `realpath`; and
keys **exactly** the segment's declared present days, because a day missing from the map is a day
the guard never looks at. `SOURCE_ORDER` is imported from `build_block` rather than copied.

**Binding to the artifact, and the part that is still not bound.** `bind_provenance_to_block()`
opens the block about to be published and requires the map to cover exactly the days it actually
contains — so a forged map fails unless the forger also rebuilds the block. `verify_build_artifact()`
cross-checks the builder's own `p5_block_plan.json` when a path is supplied, and that record wins
on disagreement.

What is **still** self-asserted: that each day was read from the `source_path` and
`source_day_index` the map claims. Nothing on disk records that except the map itself, so the
binding is to *which block* and *which days*, not to *which bytes were read*. Closing it properly
needs the builder to emit a checksummed provenance artifact alongside the block, which is a P5-S5
item. Stating it rather than letting "bound to the artifact" read as more than it is.

### 2. [Medium] `payload or {}` emptied a falsey dict subclass

A `dict` subclass with a `__bool__` returning `False` but real contents was silently replaced by
`{}` — the same laundering the round-3 type check was added to prevent, and worse, because the
record would then be written with content the caller never asked for. Now
`{} if payload is None else dict(payload)`: only `None` means "no payload". Tested with a
subclass that is falsey and non-empty at once, both for the written record and for the replay
comparison.

### 3. [Medium] Audit-log failure after the manifest commits — NOT fixed here

Confirmed and deliberately deferred. If `_append_log` fails after `bm.publish` succeeds, the
manifest is committed, the caller sees an exception, and a naive retry hits "generation archive
already exists". The ordering is intentional — logging *before* the commit yields the worse
ambiguity of a trail claiming a move that was rolled back — but this step does **not** resolve
it, and the summary above no longer implies it does.

P5-S5 must define and test: the manifest is authoritative; the hold/manifest logs are
reconstructible from the generation archives; and a retry after a committed-but-unlogged publish
must be able to recognise "already committed" and complete idempotently rather than refuse on the
archive.

## 11. Review round 5 — two findings

### 1. [High] Only the day set was bound; the rest of the entry was not

`layout`, `variables`, `fingerprint.metadata` and `day_digest` could each be edited,
`manifest_checksum` recomputed, and the manifest **published successfully** — with the mismatch
surfacing later, when `SegmentedCubeStore` fails closed building a snapshot. That converts an
editable-document error into an outage: the service holds a stale in-memory snapshot and the
on-disk manifest is unusable. Refusing at publication keeps it a refusal.

`bind_segment_to_block()` now re-derives every store-dependent field from the block on disk
using **the same functions the reader uses** — `inspect_store_contract` →
`segment_layout_from_inspection` / `metadata_fingerprint_from_inspection` — and compares. This
is P5-S3's `build → inspect → fingerprint` discipline applied at the commit point instead of
only at build time.

**Grid:** shape is always compared; the sub-region only when the block declares one. An absent
`attrs["region"]` means the block *is* the full grid, and treating that as a mismatch would
refuse every block the builder writes without a region.

**Carried-forward segments** are not re-inspected. Round 5 compared their *entries* against the
live generation; round 6 replaced that with re-composition (§12.1), which subsumes it and also
covers the cases entry-comparison could not see.

*One arm is defence in depth and is tested as such:* `day_count` cannot be edited in isolation
and stay schema-legal — changing it means changing `gaps`/`unknown`, which changes the declared
present days, which the source-map coverage check catches first. The publication path refuses
either way; the binding arm is tested directly against `bind_segment_to_block()`, where it can
actually be reached, rather than through a test that would pass for the other guard's reason.

*This also required fixing the test fixtures:* segment entries now derive `layout`,
`variables`, `metadata` and `day_digest` from an inspection of the block, the way production
does. Hand-written placeholders would have made the binding tests pass or fail for reasons
unrelated to what they test.

### 2. [Medium] Every non-delta kind went through one branch

That made `source_kind` almost decorative. A `block` entry pointing at a daily root, or a
`daily` entry carrying an arbitrary index, passed a check that only asked whether *some* path
existed. Each kind now means something specific and is checked accordingly:

| kind | what must hold |
|---|---|
| `delta` | the live delta's realpath, and the day at the recorded index |
| `block` | a block **published in the live manifest**, holding that day at the recorded index |
| `daily` / `hold` | the exact `YYYY/MM/DD` group, index `0` |

`daily`/`hold` index is pinned to `0` because a daily group has shape `(1, ny, nx)` — any other
value indicates a map built by something that did not understand the source.

`_published_block_index()` resolves `{day: index}` lazily and only for paths the **live
generation** publishes: a `block` source is a claim to have read *published history*, and a
path that is a legal Zarr store but is not in the manifest is not that. **With no resolver
supplied, a `block` source refuses** — an unverifiable claim is not a weaker claim, it is no
claim.

### 3. [Medium, still deferred] Source bytes are still not proven

Unchanged and restated: without `build_artifact_path`, the source map remains a declaration
inside the manifest. If a source is modified in place after the build, nothing here detects it.
The round-2/4 checks bind the map to *which block*, *what is in it*, and *whether each recorded
source still means what its kind says* — none of that is a checksum over the bytes that were
read. P5-S5 owns the checksummed provenance artifact; **no claim of complete provenance safety
is made here.**

## 12. Review round 6 — two findings

### 1. [High] The segment **set** was accepted, not derived

Round 5 compared the carried-forward entries that were still **present**. That says nothing
about an entry that was **removed** or one that was **injected**: drop an unrelated segment from
the plan, recompute `manifest_checksum`, and `validate_manifest` still passes — publishing a
manifest that silently loses history. Injecting a legal extra segment is the mirror image.

The fix is not another comparison. Enumerating what may change requires enumerating every way a
document can be wrong, and this is the second round where that approach left a hole.
`compose_generation()` — the function `plan_publication` already used — is now called **again
under the ingest lock**, against the live manifest, and the result is compared to the plan's
manifest **whole-object**. The question stops being "which fields differ" and becomes *is this
the manifest this live generation and this segment produce?*

> Generation `N+1` is exactly: live, minus the one superseded segment, plus the one new segment.

Only freshly generated and clock-derived fields are taken from the plan — `generation_id`,
`created_utc`, `created_by`, and the new superseded entry's `release_after_utc` /
`hold_until_utc` / `bytes`. Everything else must be reproducible from the live manifest.
A mutation widening that free list fails, and so does one narrowing the comparison to segment
ids.

A plan that cannot be composed at all — an edited segment `variables` list makes the top-level
union inconsistent — is reported as such rather than escaping as a raw `ManifestError`.

Ten tests, including the one the review asked for (live has two segments; the plan drops the
one that is *not* superseded → refused, naming it), plus injection, an emptied `superseded`
list, an edited generation number, an edited predecessor pointer, and — the test that keeps the
guard honest — an unedited plan that must still publish with both segments intact, and one that
edits **every** free field at once and must still publish.

**This subsumes round 5's carried-entry check**, which has been removed rather than left beside
it. Two overlapping guards where one is strictly stronger is how the weaker one ends up being
the only one someone maintains.

*Check ordering shifted as a result, and the tests say so.* Edits to `grid`, `variables` and
`day_count` are now caught by re-composition before the block binding sees them, because those
fields are derived from the live manifest. Their block-binding arms are defence in depth and are
tested directly against `bind_segment_to_block()`, where they can actually be reached — rather
than through publication tests that would pass for a different guard's reason.

### 2. [Medium] The compaction lock was optional

`compaction_lock_path` had a default of `None`, so a caller who forgot it published without ever
asking whether a build was running — and a build holding the lock may still be writing the very
block being referenced. It is now a **required keyword with no default**, with
`unsafe_skip_compaction_lock=True` as the single greppable waiver, applied through one named
shim in the test module so no test silently passes `None`. Same pattern as `build_block`'s
`unsafe_skip_isolation`.

### 3. [Known residual, unchanged] Source bytes are still not proven

Restated once more without softening: the map is bound to *which block*, *what is in it*, and
*whether each recorded source still means what its kind says*. None of that is a checksum over
the bytes that were read, and an in-place modification of a source after the build is
undetectable here. P5-S5 owns the checksummed provenance artifact. **No claim of complete
provenance safety is made in S4.**

## 13. Review round 7 — two findings

### 1. [High] The lifecycle deadlines could be back-dated by the plan

`release_after_utc` and `hold_until_utc` were on the free list as "clock-derived", so a plan
could set them **into the past** and publish successfully. Those two fields are the whole of
what keeps a superseded block available to in-flight snapshots, to the next fold, and to
rollback — back-dating them shortens or erases that window and lets ops hard-delete the block
early. The §8.5 machinery was intact and its inputs were forgeable.

The review offered two options. I took the first, and rejected the second **because it would
have been wrong, not merely weaker**: validating `release_after_utc >= execute_now +
release_after_s` refuses every plan that sat in review for longer than the grace period, which
is exactly the workflow the plan/execute split exists to support. The window protects from the
moment of **publication**, not of planning, so computing it at publication is both stricter and
correct.

That required a structural change worth stating plainly: **`recompose()` now returns the
manifest, and that is what `bm.publish` writes.** The plan's manifest is never handed to
`publish` at all. Previously the code compared, then published the plan's document — so every
field not explicitly compared was a field that could survive an edit. Now a field this function
does not deliberately carry across *cannot* reach the manifest, whatever an editor did to it.

Exactly two values cross from the plan: `created_by` (who asked) and the superseded entry's
`bytes` (a measurement the builder made). Neither is clock-derived and neither protects
anything. `generation_id` and `created_utc` are regenerated at the commit point, so editing them
changes nothing — a mutation that accepts them instead fails 40 tests.

Four tests, including the consequence rather than only the field: after publishing a back-dated
plan, `advance_lifecycle` must still **refuse to release** the block. Plus one that publishes a
plan three days after it was made and asserts a *full* grace period from the publication clock —
the case a lower-bound check would have refused.

### 2. [Medium] The audit log and result read the plan's metadata

`plan["generation"]` and `plan["superseded_now"]` are top-level plan fields, outside the
manifest and outside everything the re-composition verifies. Editing them produced a
`p5_manifest.jsonl` line claiming generation 999 while the manifest said 2, or naming a segment
that was never superseded. **An audit trail that is wrong is worse than one that is missing,
because it is believed.**

Both the log line and the returned dict now read the manifest that was actually committed and
the `replaced` entry the re-composition produced. The result also gained a `superseded` field
for the same reason. Tested by editing both plan fields and asserting the log, the result and
the live manifest all agree on generation 2 and `b_v1`.

### 3. [Known residual, unchanged] Source bytes, and post-commit audit-log failure

Neither is addressed here and neither is claimed. The source map still cannot prove which bytes
were read; `_append_log` still runs after the manifest commits, so a failed log write leaves a
committed manifest with no trail and a retry that hits "generation archive already exists". Both
are P5-S5 scope (§10.3, §12.3).