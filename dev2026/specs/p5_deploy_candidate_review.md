# P5 — deploy candidate review package for the one-shot compaction

**This document does not approve anything.** It is the material a reviewer needs in order to
decide whether the commit it describes should become an approved deploy SHA, plus an explicit
statement of what is still blocked regardless of that decision.

Nothing here has been executed against VM24. No approval record exists. No production operation
has run. The candidate is a commit on a local branch and nothing more.

---

## 1. Why a new deploy candidate is needed at all

`ops/p5_oneshot_compaction.py` refuses to adjudicate a parity result unless the producer that
measured it is **the reviewed function** — resolved from `REVIEWED_PRODUCERS`, imported by the
path the registry names, and compared by object identity. The producers live in
`ops/p5_parity_producers.py`.

That file **postdates every approved commit**, and the operation now says so rather than
compensating. `operation_inventory_problem()` requires the producers' on-disk bytes to match a
git blob at a commit in `APPROVED_OPERATION_SHAS`; that set is **empty** in the shipped tree, so
**the operation refuses today** and the refusal names the reason.

An earlier revision admitted the producers on the strength of the identity artifact's own
fingerprint — a comparison against the file on disk. That establishes *the operator measured
what they shipped*; it does not establish *anyone reviewed it*, and treating the weaker check as
the stronger one is the thing this package exists to stop.

**No production run is possible until a commit containing the producers is approved.** That is
the decision this package is asking for, and §5 is what it would change.

---

## 2. What the reviewer is being asked to look at

| item | where |
|---|---|
| candidate commit | reported alongside this document; it is the commit that introduces this file |
| full diff | `git show --stat` and `git show` for the candidate |
| archive | `git archive` of the candidate tree, digest recorded in the package manifest |
| file list | §3 below |
| per-module digests | §4 below |
| how the approved identity must change | §5 below |

The archive and the digest manifest are regenerated from the commit; they are **derived**, not
authored, and any reviewer can recompute them.

---

## 3. Files the candidate adds or changes

New:

* `ops/p5_parity_producers.py` — the two reviewed parity producers.

Changed in the same line of work (already committed earlier on this branch):

* `ops/p5_oneshot_compaction.py` — the operation, the identity contract, the producer registry
  and derivation, the path binding, the approval contract, the quarantine state machine.
* `ops/p5_oneshot_exception.py` — the one-shot record, capability types, pre/post contracts,
  the finalization clearance.
* `ops/p5_readiness.py` — `ASSESSOR_MODULES` covers `ops/p5_oneshot_exception.py`.
* `ingest/publish_manifest.py` — the optional borrowed ingest lock.
* `tests/test_phase2_p5_oneshot_compaction.py`, `tests/test_phase2_p5_oneshot_exception.py`.
* `specs/p5_monolith_mount_barrier_requirement.md`, `specs/p5_deployment_readiness.md`.

---

## 4. Digests

The package manifest carries a SHA-256 for **every** module in two sets, computed from the
committed tree:

* the **deployment inventory** — `p5_readiness.INVENTORY_MODULES`, 23 modules, which is what
  `identity_problem()` compares against git at the approved SHA;
* the **operation modules** — the tooling that runs a compaction, including
  `ops/p5_parity_producers.py` and `ops/p5_oneshot_compaction.py`.

A reviewer recomputes them with `sha256sum` over the archive and compares. They are not repeated
inline here on purpose: a digest copied into prose is a digest that goes stale silently, and the
manifest is generated from the commit every time.

---

## 5. The two inventories, the runtime closure, and the approval that governs each

### The closure was one module; it is twenty-one

`OPERATION_INVENTORY` held `ops/p5_parity_producers.py` — the module the producer registry
pointed at, mistaken for the set of code a compaction runs. Measuring it by **executing a real
compaction in a clean process** gives twenty-one first-party modules: the operation and its
contracts, the provisioner and legacy-base builder it calls into, the whole ingest build and
publish path, the store layer, and the `p5_readiness` policy chain. Six are reached only through
lazy imports — `ingest/corrected_day.py` is imported partway through the build, and
`p5_readiness` arrives via `_policy()` and the canonical capacity floor. **Twenty of the
twenty-one were unchecked.**

`tests/test_phase2_p5_oneshot_compaction.py` re-measures it the same way and fails if the
declared list and the measured closure differ in either direction. Padding it would demand
approval for code that never runs and make the gate look stronger than it is; missing an entry
is what this finding was.

### The two inventories are NOT disjoint, and should not be

An earlier revision of this document claimed they were, and asserted it in a test. That was only
true while the operation inventory had one element. Measuring the real closure shows the two
share eleven modules — the whole store and ingest layer — because reading a store and publishing
a manifest is the same code whether the service does it or a compaction does.

| | question | authority |
|---|---|---|
| `p5_readiness.INVENTORY_MODULES` (23) | is the code the SERVICE runs reviewed? | `REVIEWED_DEPLOY_SHAS` |
| `p5_oneshot_compaction.OPERATION_INVENTORY` (21) | is the code the OPERATION runs reviewed? | the external operation approval record |

What separates them is **which approval authority governs**, not which files are in them. Ten
modules are operation-only — the operation, its contracts, the provisioner, the legacy-base
builder, the producers, and the policy chain — and a deploy approval does not cover those.

### The allow-list cannot live in the operation

`APPROVED_OPERATION_SHAS` was a tuple inside `ops/p5_oneshot_compaction.py`, and that module is
part of the closure it governs. The circle is concrete: edit the implementation, commit it as Y,
then commit Z adding X to the allow-list. Whether that works depends only on which modules the
gate happens to check — and the gate is in the file being edited. **An allow-list a change can
extend is an allow-list the change approves itself with.**

So approval comes from **outside**, exactly as it does for the assessor
(`p5_readiness.APPROVAL_RECORD_PATH`, and for the same reason): the operator holds a record on a
path outside the repository, pointed at by `P5_OPERATION_APPROVAL`, naming

* `approved_sha` — the commit whose bytes were reviewed;
* `approved_modules` — the closure that was reviewed, so **widening the inventory later
  invalidates the approval** rather than riding in on it;
* `approved_by`, `approved_utc`, and a `scope` of `p5_oneshot_compaction_operation`.

A record inside the repository is refused, and so is a symlink whose name sits outside but which
resolves inside — the tree may not choose what its own approval says.

`OPERATION_APPROVAL_DEFAULT` is `None`. **No record, no approval, and the operation refuses** —
which is the state a fresh checkout is in today.

### What `approved_modules` has to look like

The list is the only part of the record that says *what* was reviewed, and it arrives as whatever
JSON a person wrote. `approved_modules_problem()` checks it element by element **before** any set
arithmetic, because `set()` over a list containing a nested list or dict raises `TypeError`, and a
gate that crashes on a malformed approval has refused nothing — it has failed, which a caller
catching broadly could read as any other error.

| Rule | Why |
|---|---|
| exact `str` — not `isinstance` | `True` is an `int` and hashable; a `str` subclass can define its own `__eq__`/`__hash__`, and the comparison against the inventory would then run by rules the record supplies |
| canonical relative POSIX path | `./ops/x.py` and `ops/x.py` name the file git resolves either way, so one spelling reads as *both* missing and extra |
| no duplicates | sets swallow them, and a reader counting entries gets a different answer from the gate comparing sets |
| **set equality**, not subset | a *missing* module is code that runs unreviewed; an *extra* one means the inventory shrank without the approval being redone, or the approver was covering something else |

Missing, extra, duplicate and wrong-type each keep their own refusal wording, so the message names
which rule fired rather than reporting a generic mismatch.

### `approved_sha` is deliberately not required to be `HEAD`

Two commits are checked, and they answer different questions. **This is stated because it is a
real version rule, and an unstated one is worse than either answer.**

* **`deploy_sha`** (identity artifact, from `p5_readiness.REVIEWED_DEPLOY_SHAS`) **is** required to
  equal `HEAD`, with a clean worktree. That binds the *whole* checkout.
* **`approved_sha`** (this record) binds exactly the 21 paths in `OPERATION_INVENTORY`, byte for
  byte, and says nothing about any other file.

So the operation does **not** refuse when `HEAD != approved_sha`. It refuses when any module it
actually imports differs from the approved commit.

The reason to keep them separate is that the two reviews are of different things and land at
different times: an operation review reads the compaction path; a deployment review reads the
service. Requiring one commit to be both would mean reissuing the operation approval whenever an
unrelated spec, test, or service module changed — and an approval that must be reissued for
changes nobody reviewed is an approval that gets reissued without being read.

**Why a non-inventory file may be excluded without leaving a hole.** The two commits bind the same
bytes from opposite directions: `HEAD == deploy_sha` *plus a clean worktree* means the files on
disk **are** `deploy_sha`, and the inventory gate then requires those same files to equal
`approved_sha`. The commits may differ freely outside the inventory and must agree exactly inside
it. A non-inventory file is therefore never unchecked — it is checked by the deploy commit, which
is the authority for the code the service runs. What makes "outside the inventory" mean "does not
execute during a compaction" rather than "was left off a list" is
`tests/_closure_probe.py`: the inventory is measured by running the operation, not written down.

`TestWhatTheApprovedCommitDoesAndDoesNotBind` fixes this boundary in both directions — editing a
non-inventory spec does not invalidate the approval, editing `store/time_cube.py` does.

### Why the implementation cannot approve a changed implementation

Because `ops/p5_oneshot_compaction.py` is in `OPERATION_INVENTORY`, the gate compares its
on-disk bytes against the approved commit. A modified operation does not match any commit that
predates the modification, and it cannot name its own commit: a commit id is a hash of its
content, so no commit can contain its own id. A *later* commit naming an earlier one is a
changed implementation vouching for the version it replaced — which is precisely what an
approval is supposed to rule out. The only thing that can approve a change is a person, after
the change exists, recording that decision somewhere the change cannot reach.

### What approving this candidate requires

1. Write an operation approval record outside the repository naming this candidate's SHA and
   all 21 modules of the closure; point `P5_OPERATION_APPROVAL` at it.
2. Decide **separately** whether the candidate belongs in `REVIEWED_DEPLOY_SHAS`. That is about
   the code the service runs, and is not implied by (1).
3. Re-run `tests/test_phase2_p5_readiness.py`: the assessor measures its own runtime closure by
   executing the build, prune_swap and API paths.

`ops/p5_deploy_candidate.identity_update_plan()` computes all of this from the tree, and
`operation_modules()` is **derived from the gate's own inventory** rather than being a second
hand-written list that can disagree with what actually refuses.

## 5a. The 17 skipped tests, and what they are not

The full suite reports `OK (skipped=17)`. Every one of them has the **same** cause —
`GHRSST_ZARR_PATH` is unset, so the tests that compare against the real production zarr store
have nothing to compare against. None is skipped for being slow, flaky, or inconvenient, and
none is skipped conditionally on anything else.

| suite | count | reason |
|---|---|---|
| `test_parity.ParityTests` | 11 | `GHRSST_ZARR_PATH not set; skipping old-vs-new parity` |
| `test_store_access.RealStoreParityTests` | 5 | `GHRSST_ZARR_PATH not set; skipping real-store parity` |
| `test_phase2_s4.RealRegionalCubeTests` | 1 | `GHRSST_ZARR_PATH not set; skipping real-data regional cube parity` |

<details><summary>All 17 by name</summary>

`test_parity.ParityTests`: `test_bad_date_both_400`, `test_bad_field_both_400`,
`test_bbox_small`, `test_bbox_stride`, `test_divergence_date_less_cache_header`,
`test_divergence_over_max_days_413`, `test_divergence_range_cap_lifted`,
`test_point_default_latest`, `test_point_fixed_date`, `test_point_mode_truncate`,
`test_point_range_in_cap`.

`test_store_access.RealStoreParityTests`: `test_bbox_parity`,
`test_point_series_anomaly_key_parity`, `test_point_series_memory_bounded`,
`test_point_series_parity`, `test_points_batch_parity`.

`test_phase2_s4.RealRegionalCubeTests`: `test_real_regional_parity`.

</details>

**Nothing on a critical path is skipped.** Zero skips in
`test_phase2_p5_oneshot_compaction`, `test_phase2_p5_oneshot_exception`,
`test_phase2_p5_prod_provision`, `test_phase2_p5_legacy_base_contract`,
`test_phase2_p5_mount_barrier`, `test_phase2_p5_readiness` or
`test_phase2_p5_deploy_candidate` — so quarantine, producer identity, path binding, the
approval contract, the writer fence and the post-publish failure paths all run on every
invocation.

Two other conditional skips exist in the tree and did **not** fire here, recorded so their
absence is not mistaken for coverage: `psutil not installed` (`test_phase2_p5s3`) and
`root bypasses directory permissions` (`test_phase2_p5s4`, `test_phase2_p5s8_exec`). The second
would fire if the suite were ever run as root, which would silently drop permission-refusal
tests — a reason not to run it as root rather than a defect.

---

## 6. Status of the four open items

Each is stated as: what is true now, whether it blocks a VM24 execution review, what would
resolve it. **A green local test suite is not evidence about production**; every row below is
about the deployment, not about this repository.

### 6.1 Finalization (releasing the old daily/base space)

* **Now.** Not implemented, and deliberately so. `refuse_destructive_step()` fixes the contract
  a finalization must satisfy — a clearance bound to the published generation **and** a separate
  human approval **and** no quarantine outstanding — before anyone writes one. No code in this
  tree prunes, swaps, deletes or switches.
* **Execution blocker?** **No, for the compaction itself.** The compaction publishes and
  verifies; it never needed finalization. It IS a blocker for the goal that motivated all of
  this — the space is made *releasable*, not released.
* **Needs.** A separate reviewed operation, its own human approval, and a retention decision
  about what may be removed and what must be kept. The retention question is not a code
  question.

### 6.2 `backup_verified`

* **Now.** `NOT_ADJUDICATED`, always. Nothing available to this process can reach the
  hypervisor's backup catalogue, mount the image, or confirm a restore would succeed. The flag
  is a human attestation, bound to the record and reported as an attestation.
* **Execution blocker?** **No** — but only because the whole exception is a *human risk
  acceptance*, and this is the part of it that rests on a person's word rather than on evidence.
  It is a blocker for anyone reading `eligible: true` as "verified".
* **Needs.** Either an independent query against the backup system — which would make it
  adjudicable and is the real fix — or continued acceptance that this specific claim is
  unverified. No local test can change this.

### 6.3 Continuous writer fence

* **Now.** Three probes: before the scan, after the build, after the publish. Each is validated
  and the service sets are compared. The shipped default probe **refuses** rather than asserting
  a posture.
* **Execution blocker?** **Partially.** Probing is *sampling*: a service that stops and restarts
  entirely between two probes is invisible to all three. Nothing in this tree can close that —
  proving a posture held continuously needs something watching continuously.
* **Needs.** Either a supervisor that holds the services down for the window and can attest to
  the whole interval (a systemd unit mask, a maintenance mode, an operator watching), or
  continued acceptance recorded in `residual_writer_risk`. The current design takes the second
  and requires it to be written down.

### 6.4 Arbitrary-writer enforcement

* **Now.** Not enforceable. `flock` is advisory: the ingest lock excludes the ingest path
  because the ingest path takes it, and excludes nothing else. A shell, a stray script or a
  hand-restarted service obeys neither the lock nor the service posture. What exists is
  *detection* — the monolith is re-measured before the publish and the parity gates compare
  served bytes against their sources.
* **Execution blocker?** **Yes, in the sense that matters.** It is the reason this is a one-shot
  exception rather than a way of working, and the reason the read-only mount remains the
  stronger and preferred route. The operation runs only under an explicit
  `residual_writer_risk` acknowledgement that names the outcome as possibly unrecoverable.
* **Needs.** The read-only mount from
  [`p5_monolith_mount_barrier_requirement.md`](p5_monolith_mount_barrier_requirement.md), which
  is a storage decision this project cannot make. Everything else is compensation.

---

## 7. What a green local suite does and does not say

It says: the contracts refuse what they are written to refuse, on synthetic stores, on this
machine, at this commit. Mutation testing says the tests would notice if those refusals were
removed.

It does not say: that VM24 is in the state the preflight describes, that the fence held, that
the backup exists, that the deployed tree is this tree, or that any figure collected earlier is
still true. Every one of those is established on the host, in one window, by a collection that
expires in two hours — and none of it has been collected.
