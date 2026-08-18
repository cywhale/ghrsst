# P5-S5 Part 3 — the `(e2)` cross-tier question: RESULTS

Status: **PARTIAL.** `(e2)` **protocol design and mechanism are proven**; **production closure
is PARTIAL**, pending ops evidence that the deployed quiescence hook attests a drain it actually
verified. The crash-boundary matrix is **NOT YET DELIVERED**, and **G10 / H4 / G17 are still NOT
claimed.**
`[MUT-STG]`: staging/synthetic only. No VM24 path, no `GHRSST_*` store, no cron, nothing
written outside a temp dir.

Branch `dev2026-p5-s5-part3-crash-proof`, stacked on `3c37193` (Part 2, signed off).

- Harness: [`../tests/test_phase2_p5s5_part3.py`](../tests/test_phase2_p5s5_part3.py) — **53/53 green**
- Changed: [`../ingest/swap_delta.py`](../ingest/swap_delta.py) — §7.9 quiescence is required, must prove itself, and is recorded
- Changed: [`p4s10_production_rollout_runbook.md`](p4s10_production_rollout_runbook.md) — §1 env + preflight, Step 4a, Step 4b
- New: [`../ops/quiescence.py`](../ops/quiescence.py) — the drain measurement, importable and therefore testable
- New: [`../store/durable_jsonl.py`](../store/durable_jsonl.py) — one durable append-only writer, shared by the executor manifest and the ops evidence file
- **Unchanged: [`../store/tiered_cube.py`](../store/tiered_cube.py).** No signed-off P5-S2 behaviour was touched — not even the docstring, which still records R1 as "adjudicated at S5/G10". Amending it to point at this verdict is a one-line follow-up **after** sign-off, not something to slip in alongside the evidence.
- Full local suite: **826 tests OK** (17 skipped), up from 773
- `-W error::ResourceWarning` over S4 + S5 Parts 1–3 + P4-S8a: **389 OK**
- **29 guards mutation-verified**

## The question

`TieredCube.snapshot()` guarantees one stable snapshot **per tier**, not one instant across
both (P5-S2, risk R1). Its docstring names the single pair that loses data — a **pre-publish
base with a post-prune delta** — and asserts it cannot reach a reader, because §7.5 orders
publish → verify → prune and the prune's path switch runs under request quiescence.

The instruction for this round was not to build a fence and not to invoke §14, but to find out
whether the dangerous combination can cross the real production protocol. It was answered
against the real `publish_manifest`, `prune_delta` and `execute_swap_plan` paths, with every
interleaving forced by `threading.Event`. There is no `sleep` in the suite: a failure is a
failure, not a slow machine.

## The four states

| base | delta | verdict | test |
|---|---|---|---|
| old | old | **safe** — delta serves the day | `test_1_old_base_old_delta_is_safe` |
| new | old | **safe** — delta-wins, and delta holds the corrected copy | `test_2_new_base_old_delta_is_safe_and_delta_wins` |
| new | pruned | **safe** — base serves the folded day | `test_3_new_base_pruned_delta_is_safe` |
| **old** | **pruned** | **the only dangerous combination** | `test_4_old_base_pruned_delta_loses_data_in_BOTH_shapes` |

The danger has **two shapes**, and they fail differently, so both are tested:

- a day folded into base for the **first** time sits in neither tier — `point_series` omits
  days nobody has, so the row **vanishes** from the response;
- a day that was **corrected** in delta is still present in the old base at its **stale
  value** — the read succeeds and is silently wrong. This is the worse of the two, and it is
  specific to the corrected-day lifecycle Part 2 built: nothing in the response says the value
  is the one the repair existed to replace.

`test_4` measures both against the value the day was corrected *away from*, rather than
asserting that a bad thing "would" happen.

## Is it reachable? Yes — and that is the point

`TieredCube.refresh()` refreshes base, then delta. Both reads are individually correct, and the
composite lock makes them atomic against other **snapshots** — but not against the
**filesystem**. A swap landing between the two reads hands the reader exactly the losing pair.

`test_the_dangerous_pair_IS_reachable_when_a_refresh_spans_the_swap` parks a real
`cube.refresh()` at that boundary, runs the real refold → publish → prune → swap, then releases
it. The reader emerges holding generation 1 with a pruned delta: the folded day is gone and the
repaired day reads at its stale value.

So the invariant is **not** provided by the snapshot mechanism, exactly as the P5-S2 docstring
says. It rests entirely on **nothing being in flight across the swap**. That is what makes
quiescence load-bearing rather than operational hygiene — and it is why the fix this round is in
the swap protocol, not in the read path.

## The reviewer's four points

| # | requirement | status | evidence |
|---|---|---|---|
| 1 | after publish, before prune, an in-flight request still reads old base + old delta | **PASS** | `test_after_publish_before_prune_an_in_flight_reader_is_unaffected` |
| 2 | quiescence must make that request finish or be terminated **before** the swap | **PASS** | `test_quiescence_refuses_the_swap_while_that_reader_is_still_in_flight` |
| 3 | a request starting after the swap gets new base + pruned delta | **PASS** | `test_a_reader_that_starts_after_the_swap_sees_the_new_pair` |
| 4 | no request ever returns old base + pruned delta | **PASS** | `test_no_read_across_the_whole_protocol_ever_returns_the_losing_pair` |

Point 4 is a standing assertion, not a spot check: a reader loops for the whole
publish → prune → swap sequence, **every** snapshot it takes is recorded, and all of them are
checked afterwards — membership *and* value. It refreshes through the composite `refresh()`
only, which is the single refresh path the serving app actually has
([`../api/app.py`](../api/app.py) `_refresh_loop`); if a second, tier-local refresh path is ever
added to the serving layer, this proof lapses and `(e2)` reopens.

Point 2 is the one that decides the verdict. With a reader parked mid-refresh, the quiescence
hook counts it, cannot attest a drain, and the swap **refuses with the live delta untouched**.
The reader then completes on new base + old delta — safe — and the swap succeeds on the retry.

## Verdict: mechanism proven; production closure still PARTIAL

The dangerous pair is unreachable **when the process is genuinely stopped across the swap**, so
no refresh is in flight to be caught mid-way. That is a real result: no composite
generation/epoch fence is needed for it, and `tiered_cube.py` is unchanged.

It is **not** the same as `(e2)` being closed, and this document does not say it is. What the
executor can check is that the attestation is **present, well-formed and internally consistent**.
It cannot check that the attestation is **true** — a hook that reports `drained: True` without
looking is accepted, by construction, because there is nothing on this side of the boundary that
could contradict it. The closure therefore rests on an operational fact, and an operational fact
needs operational evidence:

| | status |
|---|---|
| the dangerous pair exists and is reachable without quiescence | **PROVEN** (`test_the_dangerous_pair_IS_reachable_when_a_refresh_spans_the_swap`) |
| safety comes from process quiescence, not from `TieredSnapshot` | **PROVEN** |
| the swap refuses to proceed without a proven drain | **PROVEN** (§7.9, mutation-verified) |
| the attestation is durable and auditable after the fact | **PROVEN** (fsync'd per record; on every post-quiesce outcome) |
| the deployed runbook can execute against the current contract | **PROVEN for arguments and modules** (its own argument set builds a plan and runs a swap on a real external anchor; its preflight blocks are executed, not read) |
| the runbook is pinned to a reviewed implementation | **PARTIAL — the gate now accepts only a full immutable SHA; which SHA is *reviewed* comes from an external approval record, not from this document** |
| the deployed hook attests a drain it actually verified | **PARTIAL — deployment prerequisite** |

The last row is the one that keeps this PARTIAL. The runbook hook has been updated to measure
the worker count and record what it checked (below), but it has not been run: VM24 is off
limits for this work, so no real execution of it exists to point at.

Nothing here requires changing `TieredCube` or building the composite fence.

## Review round 2 — the production runbook, and a schema that was not enforced

**The one production runbook was broken by this API change.** `p4s10_production_rollout_runbook`
`quiesce()` ended in a bare `return`, so under the new executor it would stop pm2, close the
port, return `None`, be refused, and leave the swap unrun with the app down. A contract that
only the tests satisfy is not a contract. The hook now enumerates PM2 workers — a closed port
says new connections are refused, not that the worker finished the request it was already
serving — returns a real attestation, and writes an ops-side `quiescence_evidence.jsonl` before
the swap so it can be compared with what the executor recorded.
`TestTheProductionRunbookMatchesTheContract` reads the runbook and fails if it drifts again,
including if a core field is ever added.

**`observed_inflight` was documented as required and was not.** It was checked only when
present, and only against `> 0`, so both of these were accepted by the one load-bearing gate:

```
{"drained": True, "evidence": "trust me"}
{"drained": True, "evidence": "trust me", "observed_inflight": -1}
```

All three core fields are now required; `observed_inflight` must be a **raw `int`** equal to
`0`. `bool` is excluded explicitly, because `isinstance(True, int)` is True in Python and
`observed_inflight=False` would otherwise read as a proven zero. A negative count is refused as
"not a measurement" rather than treated as fewer than none. Extra keys remain allowed as
supplementary evidence and are **not** validated.

**The attestation was validated and then discarded.** Requiring `evidence` and throwing it away
made this document's own claim — that a swap suspected of losing a day can be traced to what
quiescence verified — untrue. The validated core is now recorded in `hold_dir/manifest.jsonl` on
the successful swap **and** on a rollback, and returned as `res["quiescence"]`. Only the fixed
validated fields are stored; extra keys are acknowledged by **name** so the record shows what
was supplied without the manifest inheriting an arbitrary caller object. A waived swap records
`{"attested": false, "waived": true}` rather than nothing, so the audit distinguishes "proven"
from "not asked".

## Review round 3 — the runbook could still not run, and the drain check was wrong

**1. The runbook was still unexecutable.** Fixing `quiesce()` was not enough: the `prune_delta`
call passed no `wal_root` / `manifest_root` / `anchor_root`, so it would fail closed the moment
it dropped a day, and the `execute_swap_plan` call passed no `compaction_lock_path`, so it was
refused *before `quiesce()` ever ran*. My previous test extracted only the `quiesce()` body and
could not see any of it. `TestTheProductionRunbookMatchesTheContract` now reads the **argument
names out of the runbook** and calls the real `prune_delta` and `execute_swap_plan` with exactly
that set: an argument the runbook forgets is an argument the test does not pass. The §1 env
block gained the four P5 roots and a preflight that checks them, including the `st_dev`
comparison between the WAL and the anchor.

**2. "0 online workers" was not a drain.** A worker leaves PM2's `online` set the moment it
starts stopping, while the OS process is still finishing the request it had already accepted —
precisely the state `(e2)` must exclude. The check now captures **every** worker pid *before*
the stop and proves each one is **gone** afterwards via `kill(pid, 0)`, then checks for draining
statuses, an open port, and any newly listed pid.

That logic now lives in [`../ops/quiescence.py`](../ops/quiescence.py), **imported** by the
runbook rather than inlined in it. This is the actual lesson of findings 1 and 2: the wrong
liveness check and the missing executor arguments were both invisible because they were written
in markdown, and a runbook cannot be tested. `TestQuiescenceAttestation` covers the surviving
pid, the `stopping` status, a worker that came back, an open port, and `PermissionError` — which
counts the process as **alive**, since EPERM means it exists and belongs to someone else.

**3. "Durable" was claimed of a buffered write.** `_manifest()` did `write` + `close`, leaving
the record in the page cache: a VM that died during the swap would lose exactly the evidence
that the swap was the last thing to happen. It now flushes and `fsync`s per record, and `fsync`s
the directory when the file is first created, since an unsynced dirent can lose the whole file.
The runbook's ops-side evidence file does the same.

**4. The two records could not be matched.** The ops file carried `checked_utc`, but
`normalize_quiescence()` kept extra fields only by **name**, discarding the value — and the ops
record had no `swap_id`. After a retry the only way to pair them was ordering. There is now a
required `attestation_id` join key, generated by the hook, recorded on **both** sides; a refused
attempt records it as `attempted_attestation_id`, because a refused attempt has already written
its ops-side file. `checked_utc` is validated and its **value** recorded.

**5. The rollback result did not carry what the docs promised.** The rollback *manifest* had
`quiescence`; the `rolled_back` and `rollback_failed` **return values** did not, while the
runbook and this document told operators to read `res["quiescence"]`. Every post-quiesce
outcome now returns it.

## Review round 4 — the deployment pin, the anchor posture, and one writer

**1. The pinned deployment branch did not contain this code.** The runbook still said
`dev2026-p4-s8-swap-design`, whose tip predates the WAL, the current executor contract and
`ops/quiescence.py`: a worktree checked out from it fails on import. The pin is now the
signed-off tip of the Part 3 branch, with `3c37193` (Part 2) as the minimum ancestor, plus a
**capability preflight** — because a commit pin cannot be written inside the commit it names,
so the modules and `execute_swap_plan`'s signature are checked **directly** rather than inferred
from a hash. The test asserts the pinned commit exists and is an ancestor of `HEAD`, and that
every module and symbol the preflight imports actually resolves.

**2. The default anchor configuration contradicted the anchor's purpose.** `ANCHOR_ROOT` was
`$G/p5_anchor` — same filesystem as the WAL, same rollback domain — and the preflight only
printed a warning. The freshness anchor exists to survive a rollback of the WAL's own host;
VM24 has had two whole-VM rollbacks, and an anchor restored alongside the WAL it checks proves
nothing. `ANCHOR_ROOT` is now a **required operator-supplied path** outside `$G`, and a shared
filesystem is a **NO-GO with a non-zero exit**, not a warning. The test **executes the
runbook's own preflight block** against a co-located anchor and asserts it stops the run —
reading the text would not have shown whether it exits.

The integration test now runs on a **real declared external anchor** with no
`unsafe_allow_colocated_anchor` anywhere in its path, threaded through `initialize_wal`,
`open_repair`, the commit append, the refold, the plan and the swap, and it asserts no
co-located sidecar exists afterwards. The anchor is still on the same filesystem as the WAL —
unavoidable under `mkdtemp`, and waived by the *declaration* rather than the unsafe flag — so a
separate rollback **domain** remains a deployment prerequisite, unchanged.

**3. The ops-side evidence file still hand-rolled durability.** It had the file `fsync` and not
the first-create directory `fsync`, so a machine dying between the evidence write and the
executor's first manifest record could come back with the record durable and its dirent gone.
Both writers now call [`../store/durable_jsonl.py`](../store/durable_jsonl.py). This is the same
lesson as round 3 and it is worth stating once more: **the copy that lived in markdown was the
copy that was wrong**, because a document cannot be tested. The runbook now calls
`qs.write_evidence()`, and a test fails if `os.fsync` reappears anywhere in the runbook text.

## Review round 5 — three checks that did not check

**1. An undeclared anchor domain passed the preflight.** The line ran
`print(rw.anchor_domain_id(...))`, and `anchor_domain_id()` returns `None` for an undeclared
root — so Python exited 0 and the `|| NO-GO` branch never ran. The check accepted exactly the
configuration it was written to reject; only the plan, later, would have failed closed. It now
`raise SystemExit(...)` on a falsy domain.

Testing it needed care: the **same-filesystem arm fires first** on a single-volume test host, so
a test that merely ran the block would have passed without ever reaching the domain check. The
test puts a stub `stat` on `PATH` that reports the two roots as different devices, which is the
only way to reach the later check — and it asserts on the *domain* message specifically, so it
cannot pass on the earlier refusal. There is a matching positive case with a declared domain, so
the negative is not passing for an unrelated reason.

**2. The "pin" was still a movable branch.** An ancestor check plus a branch name is not a
statement about which implementation ran. The gate now demands an **exact operator-supplied
`$DEPLOY_SHA`** that must equal the worktree `HEAD`. The runbook records, in the document
itself, that it is **not fully pinned** until a reviewed SHA is named — a docs-only follow-up
commit after Part 3 sign-off can name it, since that commit is not the one being pinned.

Two mutations here were instructive:

- Removing `: "${DEPLOY_SHA:?...}"` **survived**, and should have: with it gone the next line
  still refuses an unset variable. It is a diagnostic, not a guard, and it is not counted as one.
- The **ancestor floor was unreachable**. Checked against `HEAD`, it sat *after* the line that
  forces `HEAD == $DEPLOY_SHA`, so no input could ever trip it. It now runs against
  `$DEPLOY_SHA` and **before** that comparison, and a pre-Part-2 SHA is refused with its own
  message. A check that reads as a guard without being one is worse than no check, because it
  is counted as protection.

**3. The path boundary contradicted the anchor.** "All paths under `$G`, anything else aborts"
could not coexist with an `ANCHOR_ROOT` that must sit outside `$G`. The boundary now names that
single exception explicitly — an ops-approved, locally-mounted path in an independent durability
domain, read and attested rather than written — while every path a prune or swap **mutates**
stays under `$G`.

## Review round 6 — the pin accepted every movable ref, and the fix was self-contradictory

**`$DEPLOY_SHA` was not an exact SHA.** The gate compared `git rev-parse "$DEPLOY_SHA"` to
`HEAD`, and `rev-parse` resolves `HEAD`, a branch, a tag and an abbreviation alike — so every
movable ref passed the check written to reject them. My own test used `DEPLOY_SHA=HEAD` as its
**success** case, which is as clear a statement as possible that the test agreed with the bug.

The gate now canonicalizes with `--verify` and requires the operator's **original input** to
equal the canonical form, so only a full 40-character commit SHA survives.

**The proposed fix for pinning could not work.** The previous note said a docs-only follow-up
commit would record the reviewed SHA in this runbook. It cannot: a commit that records SHA *X*
**changes `HEAD` to something other than *X***, so the pin it just wrote can never satisfy
`HEAD == $DEPLOY_SHA`. The document cannot hold its own pin. The reviewed SHA now comes from an
**external approval record** — the Part 3 sign-off note, or a signed tag — and the runbook says
so.

**Ordering.** The ancestor floor ran first and answered "predates P5-S5 Part 2" for a SHA that
does not exist at all — a true refusal for the wrong reason, which sends a wrong pin to be
debugged in the wrong place. Existence and form are established first, then ancestry, then the
`HEAD` comparison.

**What the mutations forced me to fix in the tests.** Three of them survived the first run: the
five checks are deliberately redundant, so any one of them refuses most bad input, and a test
that asserted only the **exit code** proved nothing about any individual check. Each case now
asserts **which** guard fired, by its message:

| input | must be refused by |
|---|---|
| `HEAD`, a branch name, an abbreviation | canonical-form equality |
| a 40-char SHA that is not a commit | `--verify` |
| the full SHA of a pre-Part-2 commit | the ancestor floor |
| the full SHA of another real commit | the `HEAD` comparison |

One mutation still survives and is **not counted**: rewriting the final comparison as
`[ "$(git rev-parse HEAD)" = "$(git rev-parse "$DEPLOY_SHA")" ]`. After canonicalization
`$DEPLOY_SHA` is already a full SHA, so `rev-parse` returns it unchanged — the rewrite is
equivalent, not a weakened guard. The literal form is kept because it does not depend on an
upstream check to be correct.

## §7.9 — quiescence is required, and must prove itself

`pre_swap_quiesce_fn` defaulted to `None` and was **silently skipped when omitted**. A caller
who simply forgot swapped the delta path with readers alive — the exact combination the design
spec claims is unreachable, reachable by leaving out an argument. Same shape as the
`compaction_lock_path` default fixed in Part 2, and fixed the same way: **required, with a named
waiver** (`unsafe_skip_quiescence`) that has to be typed on purpose.

Presence alone is not enough either. A hook that merely does not raise cannot distinguish a
drained process from one it never looked at, and **"no reader was observed" is not "no reader
exists"** — precisely the assumption the `(e2)` analysis is not allowed to make. The hook must
now return an attestation:

```python
{"drained": True, "evidence": "pm2 stop ghrsst-api; :8080 closed; 0 worker pids",
 "observed_inflight": 0}
```

Refused: no attestation (the forgotten `return` — and the old success-by-silence), a non-dict,
`drained` anything but a raw `True`, a missing or blank `evidence` string, and any
`observed_inflight > 0`. `drained` is checked with `is True` rather than for truthiness because
`bool("false")` is `True`, which is what a shell hook produces when it interpolates a variable
it never set — the same laundering that made `allow_same_filesystem` grant the waiver it was
refusing in Part 2 round 7.

Every refusal is asserted to leave the live delta byte-for-byte as it was.

## Mutation verification

Thirty-four guards, each disabled in turn; **all thirty-four fail** — five from round 1, six
from round 2, seven from round 3, five from round 4, six from round 5, and five from round 6.

Two mutations survived across rounds 5 and 6 and are **not** counted, because neither changes
behaviour: removing `: "${DEPLOY_SHA:?...}"` (the next line still refuses an unset variable) and
rewriting the final SHA comparison to re-`rev-parse` an already-canonical value. Both are
diagnostics or equivalent formulations. Counting them would have inflated the number with lines
that protect nothing — which is the same error as counting an unreachable check.

One round-3 mutation **survived the first run**: removing the per-record `fsync` left the test
green, because it asserted only that *something* had been fsync'd and the directory sync alone
satisfied that. The test now identifies the object by **inode** and asserts both the record file
and the directory. A guard whose test passes on a neighbouring guard's behaviour is not
verified, and this is the third time in P5-S5 that shape has appeared.

| guard disabled | result |
|---|---|
| quiescence hook not required | FAILED |
| a missing attestation accepted | FAILED |
| `drained` truthiness instead of raw `True` | FAILED (4) |
| `evidence` not required | FAILED (4) |
| in-flight readers ignored | FAILED |
| every core field required (round 2) | FAILED (4) |
| `observed_inflight` must be a raw int (round 2) | FAILED (5) |
| a negative `observed_inflight` refused (round 2) | FAILED |
| the attestation recorded on success (round 2) | FAILED (3) |
| only validated fields recorded, never the callback object (round 2) | FAILED |
| the runbook hook reverted to a bare `return` (round 2) | FAILED (5) |
| `attestation_id` join key not required (round 3) | FAILED (4) |
| pid liveness filtered to PM2 `online` (round 3) | FAILED |
| `EPERM` read as "process gone" (round 3) | FAILED |
| the manifest record not fsync'd (round 3) | FAILED |
| the rollback result omits `quiescence` (round 3) | FAILED |
| the runbook drops the compaction reservation (round 3) | FAILED (2) |
| the runbook drops the P5 roots from the plan (round 3) | FAILED (4) |
| the deployment pin names a code-less branch (round 4) | FAILED |
| a co-located anchor warns instead of stopping (round 4) | FAILED |
| the runbook hand-rolls persistence again (round 4) | FAILED |
| the shared writer drops the directory `fsync` (round 4) | FAILED |
| the shared writer drops the file `fsync` (round 4) | FAILED |
| the anchor-domain check only prints (round 5) | FAILED |
| the exact-SHA comparison removed (round 5) | FAILED |
| the ancestor floor removed (round 5) | FAILED |
| the ancestor floor checked against `HEAD`, unreachable (round 5) | FAILED |
| the anchor exception dropped from the path boundary (round 5) | FAILED |
| the outstanding-pin note removed (round 5) | FAILED |
| canonical-form equality removed (round 6) | FAILED (3) |
| `--verify` dropped, a non-commit SHA accepted (round 6) | FAILED (4) |
| the ancestor floor removed (round 6, after reorder) | FAILED |
| the `HEAD` equality removed (round 6) | FAILED |
| the self-referential docs pin proposed again (round 6) | FAILED |

The harness's own thread cleanup is verified the same way: a forced failure in the looping-reader
test leaves **zero** stray thread tracebacks, because a parked reader that outlives a failed
assertion reads a directory `tearDown` is deleting and reports as a lurid unrelated traceback
instead of the assertion that actually failed. That was a real leak in the first draft of this
suite, found when a refused plan skipped the join.

## NOT delivered in this round

- **Crash-boundary injection** — kill points between publish, prune, swap and WAL append, and
  the retry / committed-but-unlogged case. This is the rest of Part 3.
- **Snapshot cases (a) (b) (c) (e)** from the design spec's crash matrix.
- **Whole-VM snapshot rollback** — still **PARTIAL — deployment prerequisite** (Part 2).
- **G10 / H4 / G17** — not claimed.

## What would reopen `(e2)`

Stated plainly, because the verdict is conditional:

1. a second refresh path in the serving layer that refreshes **one** tier;
2. a swap performed without quiescence (now refused, waiver aside);
3. a quiescence hook that attests a drain it did not verify — the code can check that the
   attestation is *present and well-formed*, never that it is *true*. That is an operational
   responsibility, it is why `evidence` is mandatory and now durable, and it is the reason
   production closure is recorded above as PARTIAL rather than done.
