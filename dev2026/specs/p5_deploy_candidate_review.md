# P5 — deploy candidate review package for the one-shot compaction

**This document does not approve anything.** It is the material a reviewer needs in order to
decide whether the commit it describes should become an approved deploy SHA, plus an explicit
statement of what is still blocked regardless of that decision.

Nothing here has been executed against VM24. No approval record exists. No production operation
has run. The candidate is a commit on a local branch and nothing more.

---

## 1. Why a new deploy candidate is needed at all

`ops/p5_oneshot_compaction.py` refuses to adjudicate a parity result unless the producer that
measured it is **the reviewed function** — resolved from `REVIEWED_PRODUCERS`, imported by path,
and compared by object identity. The producers live in `ops/p5_parity_producers.py`.

That file **postdates every commit in `p5_readiness.REVIEWED_DEPLOY_SHAS`**. So today:

* `identity_problem()` admits it as an *operation-side* module — one of
  `REVIEWED_PRODUCER_MODULES` — fingerprinted from disk rather than against a git blob at an
  approved commit;
* which means the strongest statement the identity check can make about the producer is *"the
  file on disk matches what the artifact recorded"*, not *"this is code a review approved"*.

**Until a deploy SHA containing the producers is reviewed and added to
`REVIEWED_DEPLOY_SHAS`, no production run is possible.** That is the point of this package.

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

## 5. How the approved identity must be updated

If, and only if, the reviewer approves the candidate:

1. Add the candidate SHA to `p5_readiness.REVIEWED_DEPLOY_SHAS`.
2. Decide whether `ops/p5_parity_producers.py` should join
   `p5_readiness.INVENTORY_MODULES`. **Recommended: yes.** Once it is in the inventory,
   `identity_problem()` compares it against the git blob at the approved SHA like every other
   deployed module, and the weaker on-disk-only path in `REVIEWED_PRODUCER_MODULES` stops being
   the thing that admits it.
3. If it joins the inventory, remove it from `REVIEWED_PRODUCER_MODULES` — leaving it in both
   would mean the `extra` allowance in `identity_problem()` still covers it, which is exactly
   the weaker path this step exists to close.
4. Re-run the closure and inventory tests in `tests/test_phase2_p5_readiness.py`; the assessor
   measures its own runtime closure, so a module added to the inventory that the readiness path
   does not import will be reported.
5. Only then is an operator identity artifact collectable that satisfies
   `identity_problem()` with the producers covered by git rather than by disk.

**Step 2 is a judgement, not a formality.** Putting the producers in the deployment inventory
says they ship with the service. If the intent is that they are operator tooling that never runs
on the deployment, the honest alternative is a separate approved-tooling SHA set with its own
review — and that does not exist today.

---

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
