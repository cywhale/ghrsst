# P5-S5 Part 2 — corrected-day lifecycle: RESULTS

Status: **DELIVERED.** Part 3 (crash / concurrency proof) remains NOT DELIVERED, and
**G10 / H4 / G17 are still NOT claimed.**
`[MUT-STG]`: staging/synthetic only. No VM24 path, no `GHRSST_*` store, no cron, nothing
written outside a temp dir.

Branch `dev2026-p5-s5-part2-repair-lifecycle`, stacked on `fd3a234` (Part 1).

- Lifecycle module: [`../ingest/corrected_day.py`](../ingest/corrected_day.py)
- Gate wired into [`../ingest/prune_delta.py`](../ingest/prune_delta.py)
- Builder derives `materialized_repairs`: [`../ingest/build_block.py`](../ingest/build_block.py)
- Tests: [`../tests/test_phase2_p5s5_part2.py`](../tests/test_phase2_p5s5_part2.py) — **59/59 green**
- Full local suite: **731 tests OK** (17 skipped), up from 672
- `-W error::ResourceWarning` over S4 + S5 Part 1 + Part 2: **275 OK**
- **44 guards mutation-verified**

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

**E2 refuses regardless of E1.** A parity mismatch means either a repair that bypassed the WAL or
a genuine base defect, and both need a human (§7.5a). A parity *match* authorizes nothing on its
own.

## Mutation verification

44 guards disabled in turn; **all 44 fail** — 18 in round 1, 13 in round 2, 13 in round 3.

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