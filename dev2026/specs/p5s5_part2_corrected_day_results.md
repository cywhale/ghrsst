# P5-S5 Part 2 — corrected-day lifecycle: RESULTS

Status: **DELIVERED.** Part 3 (crash / concurrency proof) remains NOT DELIVERED, and
**G10 / H4 / G17 are still NOT claimed.**
`[MUT-STG]`: staging/synthetic only. No VM24 path, no `GHRSST_*` store, no cron, nothing
written outside a temp dir.

Branch `dev2026-p5-s5-part2-repair-lifecycle`, stacked on `fd3a234` (Part 1).

- Lifecycle module: [`../ingest/corrected_day.py`](../ingest/corrected_day.py)
- Gate wired into [`../ingest/prune_delta.py`](../ingest/prune_delta.py)
- Builder derives `materialized_repairs`: [`../ingest/build_block.py`](../ingest/build_block.py)
- Tests: [`../tests/test_phase2_p5s5_part2.py`](../tests/test_phase2_p5s5_part2.py) — **87/87 green**
- Full local suite: **759 tests OK** (17 skipped), up from 672
- `-W error::ResourceWarning` over S4 + S5 Part 1 + Part 2: **303 OK**
- **68 guards mutation-verified**

## The rule

> A repaired day may leave delta **only** when E1 repair-identity authorization and §7.8a
> Phase A base-only verification have **both** passed.

Both, because either alone is a different kind of insufficient:

- **E1 alone** proves the block *claims* the right repair — a `repair_id` in
  `build_provenance.materialized_repairs` matching the WAL's latest commit. It is a statement in
  one document about another. It never reads the block.
- **Phase A alone** proves base *currently* serves a value equal to the repaired delta day. It
  cannot separate "the correction was folded" from "these happen to agree", and on a sampled
  comparison agreeing by accident is not exotic — an all-NaN region agrees with any other
  all-NaN region.

E1 is the deterministic identity; Phase A is the read that makes it about bytes.

## Gate status

| requirement | status | evidence |
|---|---|---|
| WAL authorization wired into `prune_delta` | **PASS** | `TestPruneDeltaHonoursTheGate` |
| `wal_root` + `manifest_root` required when dropping days | **PASS** | `test_dropping_days_REQUIRES_wal_root_and_manifest_root` |
| open intent / corrupt WAL / conflicting terminal → that day refuses | **PASS** | `TestPruneEligibility` |
| E2 gate: point-wise, float32, NaN-aware, never full-slab | **PASS** | `TestE2Parity`; window/offsets asserted at production geometry |
| §7.8 corrective refold: one orchestration entry point | **PASS** (round 2) | `TestCorrectiveRefoldOrchestration`, through the real §7.4 publication |
| §7.8 sealed path untouched, predecessor superseded | **PASS** | `test_the_sealed_predecessor_is_untouched_and_superseded` |
| re-authorization inside the swap lock, before the path switch | **PASS** (round 2) | `TestSwapTimeReauthorization` |
| WAL initialization identity bound to manifest authority | **PASS** (round 2) | `TestWalDeploymentIdentity` |
| the gate checks the LIVE delta being swapped | **PASS** (round 2) | `TestTheGateChecksTheLiveDelta` |
| one compaction reservation spans build → publish → Phase A | **PASS** (round 3) | `TestOneReservationSpansTheRefold`, including a no-waiver happy path |
| the swap holds a reservation, not a probe | **PASS** (round 3) | `TestSwapHoldsAReservation` |
| a rolled-back WAL prefix authorizes nothing | **PASS** (round 3) | `TestWalFreshnessAnchor` |
| Phase C through the **public** `TieredCube` | **PASS** (round 3) | `test_PHASE_A_then_B_then_C_through_the_real_swap` |
| `compaction_lock_path` required to swap | **PASS** (round 4) | `TestSwapRequiresTheCompactionLock` |
| a borrowed lock is bound to the canonical path | **PASS** (round 4) | `test_a_borrowed_lock_must_be_the_CANONICAL_one`, `test_the_refold_refuses_an_unrelated_held_lock` |
| the anchor must equal the WAL head | **PASS** (round 4) | `test_an_anchor_that_LAGS_the_head_is_refused` |
| E2 runs for **every** dropped day (bypass-WAL repair) | **PASS** (round 4) | `TestE2RunsForEveryDroppedDay` |
| the external-anchor **mechanism** runs through the whole workflow | **PASS** (round 5–6) | `TestExternalAnchorWorkflow` — initialize → intent → commit → refold → plan → swap, no `_write_anchor()` |
| **whole-VM snapshot rollback** | **PARTIAL — deployment prerequisite, NOT proven here** | see §"What the anchor does and does not prove" |
| terminal writers cannot bypass the external anchor | **PASS** (round 6) | `TestTerminalWritersCannotBypassTheAnchor` |
| the anchor domain is declared and auditable, not inferred | **PASS** (round 6) | `TestAnchorDomainIsDeclaredNotInferred` |
| an external `anchor_root` is **required** on the production path | **PASS** (round 5) | `test_omitting_the_anchor_root_fails_closed_everywhere` |
| the anchor domain is part of the WAL's deployment identity | **PASS** (round 5) | `test_a_WAL_bound_to_an_anchor_cannot_be_read_without_one` |
| plan and swap on different anchor domains → refuse | **PASS** (round 5) | `test_plan_and_swap_on_DIFFERENT_anchor_domains_is_refused` |
| §7.8a Phase A: base-only, manifest resolution, E2, provenance identity | **PASS** | `TestPhaseABaseOnly` |
| §7.8a ordering A → B → C end to end, through the real publication and swap | **PASS** (round 2) | `test_PHASE_A_then_B_then_C_through_the_real_swap` |
| E1 alone does not authorize | **PASS** | `test_E1_passing_is_not_enough_without_phase_A` |
| **Part 3 (crash/concurrency), (e2) fence, G10/H4/G17** | **NOT DELIVERED / NOT CLAIMED** | — |

## The trap §7.8a exists to close

The round-5 spec draft said a corrective refold is proven when "a point GET returns the
corrected value". **Pre-prune the repaired day is still in delta and `TieredCube` is delta-wins**,
so a public GET returns delta — it would have passed whether or not the refold worked.

Every check here therefore goes through a **base-only `SegmentedCubeStore`**, which by
construction never sees delta (§4.0/§6.1). `test_phase_A_reads_BASE_not_the_public_view` pins
that the base store resolves the day to the corrected version on its own.

Phase A also **refuses to run post-prune**: with the day already gone from delta there is
nothing to compare base against, and a comparison against nothing passes vacuously.

## Design decisions worth defending

**The sample window is keyed on the DATE, not a physical index.** The Part 1 artifact keys its
window on the block's `day_index`, which is right there — it describes one block. A repair
fingerprint has to be comparable across *different* stores: the repaired delta day at one index
and the refolded block's copy at another. A physical index would make the two sides sample
different cells and disagree for a reason that is not a difference. `date_slot()` is the one
coordinate both stores agree on, and one `fingerprint_day()` serves all three callers — repair
tooling, refold, prune gate — so they cannot drift.

**`materialized_repairs` is derived, never accepted.** The caller does not get to say which
correction a block carries. For each day the build read **from delta** that has a committed
repair, the builder records the WAL's latest `repair_id` and the fingerprint of the bytes it
actually read. A day resolved from the **predecessor block** is deliberately *not* attested — the
fold did not read that repair, so it must not say it did. Both are tested, and a fold with no
`wal_root` attests nothing rather than an empty success.

**The gate is on by default and fail-closed.** `prune_delta` requires `wal_root` and
`manifest_root` whenever it drops anything; `corrected_day_gate=False` is the single named
waiver, used by one shim each in the P4-S6 and P4-S8a suites, which predate the WAL and exercise
prune mechanics. The waiver is in one named place per suite rather than sprinkled through call
sites.

**E2 refuses regardless of E1, and runs for every dropped day.** A parity mismatch means either
a repair that bypassed the WAL or a genuine base defect, and both need a human (§7.5a). A parity
*match* authorizes nothing on its own. Running E2 only for days the WAL knew about (rounds 1–3)
made the one case E2 is documented to catch — a repair that bypassed the WAL — the one case it
could not see.

## Mutation verification

68 guards disabled in turn; **all 68 fail** — 18 / 13 / 13 / 8 / 10 / 6 across six rounds.

| guard disabled | result |
|---|---|
| Phase A skipped entirely | FAILED |
| E2 parity not consulted in Phase A | FAILED (3) |
| repair identity not verified | FAILED (2) |
| materialized fingerprint not compared | FAILED |
| missing materialized entry accepted | FAILED |
| stale `repair_id` accepted | FAILED |
| non-delta materialized source accepted | FAILED |
| Phase A runs post-prune (vacuous) | FAILED |
| expected segment not compared | FAILED |
| corrupt WAL authorizes days | FAILED |
| `prune_delta` ignores the gate | FAILED (2) |
| `prune_delta` does not require the roots | FAILED |
| the gate is off by default | FAILED (3) |
| builder attests block-sourced days too | FAILED |
| builder accepts a caller-supplied repair id | FAILED (5 + 2) |
| no re-authorization inside the swap lock | FAILED (2) |
| swap does not require the roots | FAILED |
| swap ignores the gate verdict | FAILED |
| swap re-auth checks the staging delta, not live | FAILED (2) |
| an unbound WAL is treated as empty | FAILED (3) |
| authority mismatch tolerated | FAILED |
| a missing `wal_initialized` record tolerated | FAILED (2) |
| rebinding an initialized WAL allowed | FAILED |
| gate uses `source_delta` instead of the live delta | FAILED |
| orchestrator publishes a refold that materialized nothing | FAILED |
| orchestrator skips Phase A | FAILED (2) |
| orchestrator ignores a failed publication | FAILED |

Four mutations survived the first run — the three identity arms and the block-sourced
attestation. Each was a **test gap where an earlier guard shadowed the check**: `prune_eligibility`
runs E1 before Phase A, so E1's refusal fired first and the arms inside `_verify_repair_identity`
were never reached. They are now tested against `_verify_repair_identity` directly, where they
can be reached. An unreachable-by-test refusal is not a refusal.

## Reproduce

```bash
dev2026/.venv/bin/python -m unittest dev2026.tests.test_phase2_p5s5_part2
```

## Residual risks

1. **E2 is a seeded sample** (§7.5a: defence in depth, never sufficient alone). A corruption
   confined to unsampled cells survives it. E1 is the deterministic gate and is enforced.
2. **The refold entry point does not prune, by design.** `corrective_refold()` runs
   build → publish → Phase A; the caller then runs `prune_delta` + `execute_swap_plan`
   separately. That separation is deliberate (a function doing both would make the gate its own
   caller), but it does mean the *whole* A→B→C sequence is assembled by the operator rather
   than by one call. The runbook for it is `test_PHASE_A_then_B_then_C_through_the_real_swap`.
3. **Part 3 is not delivered.** No crash-boundary injection, no concurrency proof, and the
   **(e2) composite base+delta fence still does not exist**. Until Part 3 decides, publication
   should keep the quiesced posture (§15), and no complete-old/complete-new claim is made.
4. **Post-commit audit-log failure** remains ambiguous (P5-S4 §12.3).


## Review round 2 — four findings

### 1. [High] The authorization was never re-run at the moment of the swap

`prune_delta` authorizes when the plan is built. `execute_swap_plan` switches the path minutes
or hours later, and its staleness guard compares **day sets** — a repair committed in between
does not change the day set at all: same day, same date, same count. The guard sees nothing,
and the swap drops a day whose newest correction base does not carry.

The gate now re-runs **inside the ingest lock, before the path switch**, against the **live**
delta. The test makes the point explicitly: a second test asserts the day set really is
unchanged, so the abort cannot be credited to the staleness guard.

`wal_root` and `manifest_root` are required to swap away days; `corrected_day_gate=False` is
the one named waiver, applied through a single shim in the P4-S8a suite.

### 2. [High] An absent WAL read as "never repaired"

A missing or uninitialized log parsed as empty, so every day looked never-repaired and every
prune was authorized. **The failure is silent, total, and indistinguishable from a deployment
that has genuinely had no repairs.**

A WAL now carries a `wal_initialized` first record binding it to a **manifest authority**:
resolved root, format, version, grid and block calendar. `generation_id` changes on every
publish and cannot be the binding; those five do not change for the life of a store. The gate
calls `assert_bound_to(manifest_root)` and refuses when the record is absent or names another
deployment. Initialization is idempotent for an identical binding and **refuses to rebind** a
log that already carries history, because that would transfer its authorizations to another
base.

*One deliberate narrowing.* I first refused **appends** to an unbound log as well. That is
stricter than the finding asks and buys nothing: `initialize_wal` already refuses to bind a log
that has records, so an unbound log's records can never authorize anything. Keeping the append
refusal would have churned the signed-off P5-S4 suite for a rule that changes no outcome. The
fail-closed property lives at authorization time, which is where it was asked for, and a test
pins the consequence.

### 3. [High] The gate checked `source_delta`, not the delta being swapped

`source_delta` may point at a different store. The day whose correction is being authorized away
lives in `delta_path` — the one that gets swapped — so checking another copy authorizes the
wrong bytes. The gate now uses `delta_path`, and a `source_delta` that differs from it **refuses
outright** when dropping days: there is no immutable binding between two arbitrary deltas, and
inventing one would be the substitution the gate exists to prevent.

### 4. Orchestration and integration evidence

`corrective_refold()` is now the single §7.8 entry point: build a new version delta-first →
publish through the **real §7.4 path** (provenance verification, locks, recomposition) → §7.8a
Phase A. It refuses to publish a refold that materialized no repair for the day, because a
refold that supersedes the sealed version while carrying no correction is worse than none — it
looks like the repair landed.

It deliberately **does not prune**. Publication and pruning are separated by §7.5's ordering, and
a function that did both would make the gate its own caller — the one arrangement in which a
gate can be waived by the thing it gates.

`test_PHASE_A_then_B_then_C_through_the_real_swap` runs the whole ordering with no shortcuts:
repair → orchestrated refold → Phase A → `prune_delta` → **real `execute_swap_plan`** → and then
asserts the live delta no longer holds the day, the manifest resolves it to the corrected
version, and a base read returns the same corrected value as before the swap. Phase A is then
asserted to be correctly unavailable, because it is a pre-prune check.

All 13 round-2 guards are mutation-verified, including "swap re-authorization checks the staging
delta instead of live" and "orchestrator skips Phase A".


## Review round 3 — four findings

### 1. [High] The refold released the reservation between build and publish

`execute_publication` acquired its own `CompactionLock`, so the refold had to drop its own —
and that gap is exactly when a concurrent build could start rewriting the block the refold is
about to reference. Publication now **borrows** a held lock: it verifies the lock is held,
re-asserts the fence, and does **not** release it, because it did not take it.

`corrective_refold()` acquires one reservation and holds it across build → publish → Phase A.
The proof is taken from inside: a probe wrapped around `bm.publish` and another around
`phase_a_base_only` both find the lock unavailable to a third party. The fence is re-asserted
between publish and Phase A, tested by `rm`-ing and recreating the lock file in that window —
holding the fd is not the same as still owning the reservation.

`test_the_happy_path_uses_NO_unsafe_waiver` runs the whole thing with a real reservation, real
build isolation, a real positive disk reserve and real provenance verification. If any of those
only worked behind a waiver, it fails.

### 2. [High] The swap probed the compaction lock instead of reserving it

A probe answers "was a build running a moment ago". Between that answer and the `os.rename`
pair, a build can take the lock and begin reading the delta whose path is about to move — the
P4-S8a configuration with a permanent consequence. `execute_swap_plan` now takes the reservation
**non-blocking** and holds it in compaction → ingest order across the swap **and any rollback**.

Tested by probing from inside the switch itself — wrapping `os.rename` — so there is no timing
to get wrong, plus a running-build case that refuses without waiting and leaves the live delta
byte-identical.

### 3. [High] A valid WAL *prefix* is still a valid WAL

Restore `p5_repairs.jsonl` from an older backup, or truncate it, and every integrity rule still
passes: checksums chain, `seq` is gap-free, the state machine is coherent. The days repaired in
the lost tail simply read as **never-repaired**, and the gate authorizes dropping them. Nothing
inside a self-describing log can detect its own truncation — the detection has to come from
outside it.

`p5_repairs.anchor.json` is a durable high-water mark, written inside the WAL lock **after** the
record is durable (the other order would name a record a crash could leave unwritten) and
monotonic, so a stale write cannot un-anchor a log that has gone further. `assert_fresh()`
requires the log to contain the exact record the anchor names.

*One arm removed rather than kept:* I first wrote separate "log is shorter than the anchor" and
"record at the anchor differs" checks. A shorter log has no record at that seq at all, so the
first could never fire — an unreachable arm reading as a guard. One check now covers both,
because they are the same fact.

### 4. Phase C now reads through the public view

Phase C previously asserted only a base-only read. It now also reads through a real
`TieredCube(SegmentedCubeStore, TimeCubeStore)` — what a user actually gets. The test records the
public value **pre-prune** as well, where it is served by delta, so the post-prune equality is
visibly not just re-reading delta: by then delta no longer has the day to answer with.

## Review round 4 — four findings, all reproduced by the reviewer with real runs

### 1. [High] `execute_swap_plan()` could omit the compaction lock entirely

It defaulted to `None` and only reserved when a path was supplied, so a caller who omitted it
swapped the delta path with **no reservation at all** — measured succeeding while another
process held the real lock. Now required, with `unsafe_skip_compaction_lock=True` as the single
named waiver (one shim in the P4-S8a suite). The Phase C test passes a real lock, so the
end-to-end path is the production-safe one.

### 2. [High] A borrowed lock was not bound to the canonical path

`corrective_refold()` accepted any held lock and then *ignored* `compaction_lock_path`. Holding
some unrelated lock satisfied "a lock is held" while the real reservation sat free for a build
to take — the guarantee read as satisfied and protected nothing. The reviewer demonstrated it:
hold the canonical lock, pass a different one, and the refold published.

Both `corrective_refold()` and `execute_publication()` now require `compaction_lock_path`
alongside a borrowed lock and compare `realpath`. Passing a lock with no canonical path is
refused: there would be nothing to bind identity to.

### 3. [High] The anchor was not a head fence, and cannot survive a snapshot rollback

Two separate defects.

**It only had to point at *some* record.** WAL head 3 with anchor 1 passed. It now must equal
the head: an anchor that lags leaves the tail unattested, which is indistinguishable from a
rollback, and that needs manual recovery rather than a heuristic.

**A co-located sidecar cannot detect a whole-root rollback**, and I claimed it could. A VM
snapshot restores log and anchor together and the restored pair is perfectly self-consistent —
the reviewer measured exactly that. `read_wal(..., anchor_root=...)` now places the anchor in a
different rollback domain, and the module docstring states plainly what co-location does prove
(single-file truncation or substitution) and what it does not. The gate status table records
this as **PARTIAL**, not PASS: it is a deployment property, not something the code can assert
about itself. A test pins **both halves** — the co-located anchor misses the rollback, the
separate domain catches it — so the limitation is documented by a test rather than by prose.

### 4. [High] E2 never ran for a day the WAL had no opinion about

`prune_eligibility` authorized any day with no committed repair without running E2 at all. So a
repair applied straight to delta, with nothing written to the WAL, was authorized — reproduced
by the reviewer. That contradicted this document's own claim that E2 catches a bypass-WAL
repair, which is the correction that matters most.

E2 now runs for **every** dropped day. A day with a committed repair additionally requires E1
identity and the full Phase A. A day with none still has to match base, and a mismatch refuses
with "no committed repair explains this" — covering both halves of what §7.5a says E2 is for: a
repair that bypassed the WAL, and a base defect.


## Review round 5 — one High, one Low

### [High] The external anchor existed but no workflow used it

`read_wal(..., anchor_root=...)` supported an external anchor from round 4, and **nothing else
did**. `initialize_wal`, `open_repair`, the builder, Phase A's identity check, the prune gate
and the swap-time re-authorization all fell back to a sidecar beside the log. The capability was
present and unreachable, and the separate-domain test proved it by calling the private
`_write_anchor()` directly — which is exactly the shape of a test that demonstrates a mechanism
rather than a behaviour.

That matters here more than usual: **VM24 has taken two whole-VM snapshot rollbacks**, which is
the scenario this defends against and the one a co-located sidecar cannot see.

`anchor_root` is now threaded through the whole path — initialize, intent, commit/abort, build /
refold, Phase A, prune plan, swap-time re-authorization — and:

- **it is required.** `resolve_anchor_root()` is the single place that decides, and it refuses a
  missing anchor *and* one that resolves to the WAL root itself. `unsafe_allow_colocated_anchor`
  is the one named waiver, used by a shim per legacy suite.
- **it is part of the WAL's deployment identity.** The `wal_initialized` record carries the
  resolved anchor root, so a log initialized against an external anchor cannot later be read
  without one, and vice versa. Without that, a deployment could initialize with the protection
  and prune without it — every stage self-consistent, and the protection absent from the stage
  that mattered.
- **the plan records the domain it was authorized against**, and the swap refuses if it differs.
  Two stages resting on different evidence means the weaker one decides.

`TestExternalAnchorWorkflow` runs the real sequence end to end and **never calls
`_write_anchor()`**: every anchor advance comes from a real append. It asserts no co-located
sidecar exists at all, then rolls the WAL root back with the anchor intact and requires **both**
the plan and the swap to refuse — plus the refold, which would otherwise attest a repair from a
log the gate will reject, producing evidence nothing can accept and a superseded sealed version
to show for it.

### [Low] A docstring that outlived its behaviour

`prune_eligibility` still said an unrepaired day "needs only the WAL's silence". Round 4 made E2
run for every dropped day, so that had been false since. Corrected: the WAL's silence about a
day is not evidence that nothing happened to it — a repair straight to delta leaves exactly that
silence.


## What the anchor does and does not prove

Three things are routinely collapsed into one, and only the third is the protection:

| | detects | does not detect |
|---|---|---|
| **a different path** | a `p5_repairs.jsonl` truncated or replaced on its own | anything that touches both directories |
| **a different filesystem** (`st_dev`) | the above | one VM snapshot restoring both mounts |
| **a different ROLLBACK DOMAIN** | a restore of the WAL's host | — |

**No check inside this process can establish the third.** A path on a snapshotted volume is
indistinguishable from one on an independent volume. So:

- the anchor root must carry a **declared** `p5_anchor_domain.json` — an auditable
  `domain_id` an operator sets deliberately — and the WAL binds to **that id**, not to a local
  path, which changes on every remount and says nothing about durability;
- `st_dev` equality is refused as a **minimum preflight**, with the explicit note that a
  *different* `st_dev` would still not prove a different backup domain. A genuinely independent
  volume that reports the host device can be accepted by re-declaring with
  `allow_same_filesystem=True` — recorded **in the artifact**, because it is a deployment claim
  being waived and belongs where an auditor reads the deployment, not at a call site;
- **the guarantee itself stays PARTIAL** until ops confirm the anchor lives outside the WAL
  host's snapshot scope: another machine, an object store, or a volume IT has stated is not
  restored with the VM.

**What the tests do prove:** the whole workflow uses the external anchor, refuses when it is
missing, undeclared, co-located, on the same filesystem without acknowledgement, from another
domain, or behind the log's head. My round-5 test rolled back a WAL subdirectory while sparing
a sibling **under the same `mkdtemp()`** — that demonstrates selective-rollback detection, and I
marked it PASS for whole-VM rollback, which it never showed. The row is PARTIAL again and the
suite's own domain declaration says `NOT a real rollback domain` in its `note`.

## Review round 6 — two findings

### 1. [High] "Different path" was marked as PASS for snapshot rollback

Covered above. The correction is in three places: the gate row, the module docstring, and the
test fixture's own declaration.

### 2. [Medium] Terminal writers could bypass the external anchor

`append()` accepted `anchor_root=None` and wrote a **co-located** sidecar — and that is the API
`repair_committed` / `repair_aborted` used. A terminal write therefore *succeeded* while leaving
the real external anchor behind: nothing lost, but every later prune and refold failed closed
and the deployment needed manual recovery. **A write that reports success and strands the
workflow is worse than one that refuses.**

- `append()` now goes through `resolve_anchor_root()` like every reader — no silent fallback;
- `commit_repair()` / `abort_repair()` are the terminal writers the workflow uses, so the anchor
  choice lives in one place rather than at every call site;
- the supplied anchor is checked against the domain the WAL is **bound** to **before a byte is
  written**, so a mismatched call leaves the WAL and both anchors byte-identical — asserted on
  bytes, and on the absence of any co-located sidecar;
- the end-to-end workflow test now uses `commit_repair()` rather than raw `append()`.
