# P5 — the deployment-version composition, and the integration candidate

**Status: a candidate and a blocker. Nothing here is approved, and generating it approved
nothing.** `REVIEWED_DEPLOY_SHAS` and `CURRENT_DEPLOY_SHA` are unchanged; no operation approval
record exists.

Every figure below is computed from real git blobs by
`ops/p5_deploy_candidate.compatibility_matrix()` and asserted by
`TestTheCompatibilityMatrixIsComputedFromGit`, so a wrong claim in this document fails the
suite rather than sitting here being read.

## 1. The blocker

`CURRENT_DEPLOY_SHA` is `708058e`. It is a **valid deployment** — all 23 service modules are
present — and it **cannot host this operation**, because the operation's own code is not in
that tree.

| | `3223fba` | `708058e` (current) | candidate |
|---|---|---|---|
| service modules present | 23/23 | 23/23 | 23/23 |
| operation modules present | 15/21 | **16/21** | 21/21 |
| operation modules at candidate bytes | 11/21 | **11/21** | 21/21 |
| can host the operation | no | **no** | yes |

### The five absent at `708058e`

    ops/p5_legacy_base.py
    ops/p5_oneshot_compaction.py
    ops/p5_oneshot_exception.py
    ops/p5_parity_producers.py
    ops/p5_prod_provision.py

### The five present with different bytes

| module | `708058e` | candidate |
|---|---|---|
| `ops/p5_readiness.py` | `0dc1b4198dfce445…` | `6782fba677f49d03…` |
| `ops/p5s8_evidence.py` | `727383253417c387…` | `166adcd5562486b8…` |
| `ingest/publish_manifest.py` | `666ca1eeb974625f…` | `41a7631538664688…` |
| `store/block_manifest.py` | `f458627be33c60dc…` | `c84f6a1aeda4e388…` |
| `store/segmented_cube.py` | `1154ce161c1e516b…` | `0b15305d7d556e13…` |

Absent and differing are reported **separately** because they are different failures: one is
"never reviewed here", the other is "reviewed at other bytes", and they send a reviewer to
different places.

## 2. The version model, stated so it can be executed

Three bindings must hold **at once**, each checked by a different mechanism:

| binding | checked by | governs |
|---|---|---|
| `HEAD == deploy_sha`, worktree clean | `identity_problem` | the whole checkout |
| 23 service modules match `deploy_sha` | `identity_problem` | what the service runs |
| 21 operation modules match `approved_sha` | `operation_inventory_problem` | what the compaction runs |

`deploy_sha` and `approved_sha` **need not be the same commit** — that boundary is deliberate
and is documented in `p5_deploy_candidate_review.md` §5. But a clean checkout of `deploy_sha`
*is what is on disk*, and the on-disk bytes are what the operation gate compares against
`approved_sha`. So the two commits may differ freely **outside** the 21 operation modules and
must agree **byte-for-byte inside** them.

**Therefore `708058e` + an operation approval at the candidate is refused**, and the refusal is
correct rather than a gap to widen. Proven in
`TestTheDeploymentAndTheOperationMustBeTheSameTree`, which materialises each commit with
`git archive` and runs the real gate against it:

* a clean checkout of `708058e` has a **valid identity artifact** — `identity_problem` returns
  `None`;
* the same checkout is **refused by the operation gate**: *"the identity artifact does not
  fingerprint the operation module `ops/p5_oneshot_compaction.py`; the code about to run is
  code nobody recorded"*;
* an artifact that *claims* the missing modules instead is refused with *"is not on disk"*;
* a tree carrying `708058e`'s `store/segmented_cube.py` — what a partial upgrade leaves behind
  — is refused with *"does not match the reviewed bytes"*.

Both entries of `REVIEWED_DEPLOY_SHAS` are covered, so promoting the older one is not a route
around this either.

## 3. The integration candidate

**One commit carrying the service code and the operation code together.** The operation code is
not parked in a separate commit that `HEAD` would never check out — the candidate is a direct
descendant of `708058e`, and it is the tree that would actually run. (No commit count is written
here: it moves with every commit, and a number that drifts is worse than no number. Ancestry is
asserted by `test_the_candidate_is_a_descendant_of_the_deploy_commit`.)

**No architectural change was needed and none was made.** The existing identity/HEAD contract
already supports this: when `deploy_sha == approved_sha == the candidate`, all three bindings
above are satisfied by construction. The separation of the two approvals remains available for
the case where the operation is reviewed at a different time from the service; it simply
requires the two commits to agree on the 21 modules, which `708058e` and the candidate do not.

### Changed files vs `708058e`

The **exact, complete** list is generated, not transcribed: `changed_files(CURRENT_DEPLOY_SHA,
candidate)` is carried in the package manifest as `changed_vs_current_deploy`, split by git's
own status letter. A total written down here could not be right anyway — this document is
itself part of the diff it describes, so any commit that updates the number changes it.

What is stable, and what a reviewer needs, is which **code modules** moved. Those are listed
below and asserted against git by `TestTheIntegrationCandidateDocumentIsTrue`.

**Added — the operation's own code, absent from the deploy commit entirely**

    ops/p5_legacy_base.py            ops/p5_oneshot_exception.py    ops/p5_prod_provision.py
    ops/p5_oneshot_compaction.py     ops/p5_parity_producers.py

plus the operator tooling (`ops/p5_deploy_candidate.py`), its tests, and two specs.

**Modified — code**

    api/app.py                   ops/p5s8_evidence.py         store/segmented_cube.py
    ingest/publish_manifest.py   ops/p5_readiness.py          store/block_manifest.py

plus their tests and two specs.

**Deleted — none.** A candidate that removed a module the service imports would be a very
different review, and a test asserts the empty deletion list so nobody has to notice its
absence.

## 4. The two inventories

23 service + 21 operation, sharing 11. **They are not disjoint**, and the overlap is expected:
the service and a compaction genuinely run the same store and ingest code. What separates the
inventories is *which approval authority governs each*, not which files are in them.

| | count | |
|---|---|---|
| shared | 11 | `ingest/build_block.py`, `ingest/corrected_day.py`, `ingest/publish_manifest.py`, `store/__init__.py`, `store/block_manifest.py`, `store/compaction_lock.py`, `store/durable_jsonl.py`, `store/repair_wal.py`, `store/segmented_cube.py`, `store/source_provenance.py`, `store/time_cube.py` |
| service-only | 12 | `api/__init__.py`, `api/app.py`, `ingest/build_timecube.py`, `ingest/dual_write.py`, `ingest/prune_delta.py`, `ingest/swap_delta.py`, `store/bbox_encode.py`, `store/hybrid_router.py`, `store/spatial_policy.py`, `store/store_access.py`, `store/tiered_cube.py`, `store/zarr_paths.py` |
| operation-only | 10 | `ops/p5_legacy_base.py`, `ops/p5_oneshot_compaction.py`, `ops/p5_oneshot_exception.py`, `ops/p5_parity_producers.py`, `ops/p5_prod_provision.py`, `ops/p5_readiness.py`, `ops/p5_roadmap.py`, `ops/p5s8_evidence.py`, `ops/p5s8_report.py`, `ops/quiescence.py` |

## 5. What the changes bear on

`CAPABILITY_MODULES` maps a module to the capabilities it participates in. This says which
questions the candidate **raises**; only a person who reads the diff can answer them.

| capability | changed modules that touch it |
|---|---|
| **API parity** | `api/app.py`, `store/block_manifest.py`, `store/segmented_cube.py` |
| **generation-1** | `ingest/publish_manifest.py`, `ops/p5_legacy_base.py`, `ops/p5_prod_provision.py`, `store/block_manifest.py` |
| **provisioning** | `ops/p5_prod_provision.py`, `ops/p5_readiness.py` |
| **one-shot compaction** | `ingest/publish_manifest.py`, `ops/p5_legacy_base.py`, `ops/p5_oneshot_compaction.py`, `ops/p5_oneshot_exception.py`, `ops/p5_parity_producers.py`, `store/block_manifest.py`, `store/segmented_cube.py` |

`api/app.py` changing is the one a reviewer should read first: it is service-only, so the
operation approval does not cover it, and it is on the API-parity path the compaction's
post-publish gate asserts against.

## 5a. Service-side risks found in review

Three properties of the service changes that are correct as written but constrain how the
candidate may be deployed. None of them is a reason to weaken a gate; all three are things an
operator has to know **before** acting, not after.

### The manifest schema is not backward compatible — this constrains approval ORDER

`block_manifest._require_keys` rejects unknown segment fields, deliberately. `content_seal` is
new in the candidate, so once a generation carrying one is published:

| | reads a sealed manifest | reports |
|---|---|---|
| the candidate | yes — verifies the seal | `degraded` if it fails |
| `708058e` | **no** — `ManifestError: unknown field(s) ['content_seal']` | `"status": "ok"` |

The older commit leaves `cube = None` and serves **daily-only**, and it has no `degraded`
reporting, so its `/healthz` still says `ok` while the base has left the served view — exactly
the failure the candidate's healthz change exists to make visible.

**So rolling the service back below the candidate is safe only while no published generation
carries a seal.** A manifest *without* the field is read by both commits, which is why
deploying the candidate before any seal exists is safe, and why the service deployment must
land **before** the operation runs. Pinned by
`TestASealedManifestCannotBeReadByTheOlderDeployment`, which runs the old validator from a real
checkout rather than asserting this from a string.

Exposure is bounded: `ops/p5_legacy_base.py` is the only module that writes a seal, no service
path does, and it is itself gated on the operation approval.

### The seal keys on mtime, so a restore invalidates it

`content_ledger` hashes path, size and `st_mtime_ns`. Anything that rewrites metadata without
changing content — **a VM-level backup restore**, an `rsync` without `-t`, a `cp -r`, a
filesystem migration — produces a seal mismatch, and the service then refuses the base and
serves daily-only.

This interacts directly with the recoverability story: VM-level backup is the stated
compensation for operating on a writable monolith, and exercising that compensation breaks the
seal. The failure is loud and fail-closed rather than silent, which is the right direction, but
**a restore requires re-provisioning the generation, not just restarting the service.** The
specs document mtime as a limit on detection strength (false negatives); this is the opposite
direction — a false positive after a legitimate restore — and was not written down.

### The seal scan: measured, and a regression it exposed

Measured read-only on VM24 against the real monolith. The measurement was taken at `f3750cf`,
an earlier commit on this same line; `store/block_manifest.py` — which contains
`content_ledger`, the function measured — is **byte-identical** at `f3750cf` and at the current
candidate (`c84f6a1aeda4e388…`), so the numbers carry over unchanged. What changed after the
measurement is how OFTEN that scan runs, not what it costs:

| | |
|---|---|
| files / bytes | **1,012,962** / **2,238,016,046,417** (2.24 TB) |
| device | `/dev/sdc1` → `/home/odbadmin/Data`, ext4, **`rw,relatime`** |
| cold-like / warm-like wall | **13.167 s** / **13.077 s** |
| CPU | 10.14 s user + 3.03 s sys |
| peak RSS | 21,220 KiB |

**Startup cost is fine**: 13 s against gunicorn `--timeout 180` is ~7%, both workers pay it
concurrently, and no cron restarts the service.

**The measurement exposed a regression that startup cost alone would have hidden.** The seal
check lives in `SegmentedCubeStore._build_meta()`, and the lifespan TTL loop called
`cube.refresh()` — the *unconditional* rebuild. On the production 300 s TTL that is a
1,012,962-file, 13-second walk **every five minutes, per worker, forever**: roughly 9% of a core
burned to re-derive an answer that had not changed. At `708058e` the same loop was cheap,
because `_build_meta()` had no seal check — so this was introduced by the candidate.

Fixed by calling `cube.maybe_refresh(ttl)`, which is what `refresh_if_changed()` was written for
(*"Reopening every segment on each TTL tick would be pure waste"*). The delta tier still
refreshes every tick, so a newly appended day appears exactly as before.

What that gives up, and why it is acceptable, is stated in
`p5_deployment_readiness.md` — **the seal is not continuous protection against an arbitrary
writer**, and it never was.

## 6. The three approvals this needs

All three are separate, none is implied by another, and **none was created**.

1. **Service deployment approval** — add the candidate to `p5_readiness.REVIEWED_DEPLOY_SHAS`,
   and decide `CURRENT_DEPLOY_SHA` *separately*. Governs the 23 modules the service imports.
   *Not done here, and not automated: a tool that added its own SHA to the approved set would
   be the self-approval circle in another costume. A test asserts the candidate is absent from
   both.*
2. **Operation approval** — an external record at `P5_OPERATION_APPROVAL` naming `approved_sha`
   and all 21 operation modules as an exact set. Governs the code the compaction runs.
   *Not done here. `OPERATION_APPROVAL_DEFAULT` is `None`, so a fresh checkout refuses.*
3. **Assessor approval** — `p5_readiness.APPROVAL_RECORD_PATH`. Governs whether the readiness
   assessor's own verdicts may be relied on.

Approving (1) does not approve (2). A commit can reasonably be reviewed as a service deployment
and not as an operation host, and the two reviews read different code.

### How the candidate is promoted, without approving itself

**A commit cannot contain its own id, so no candidate can list itself in
`REVIEWED_DEPLOY_SHAS`.** This is not a new problem and it does not need a new mechanism — the
project already solved it once, and the promotion of `708058e` is the worked example:

| step | commit | what it did |
|---|---|---|
| propose | `af2b249` | named the revision in `PROPOSED_DEPLOY_SHAS` — *"approved by nothing"* |
| promote | `5e6b076` | moved it into `REVIEWED_DEPLOY_SHAS` |

At `708058e` itself, `REVIEWED_DEPLOY_SHAS` contained only `3223fba`. **The deployment commit
never listed itself; a later commit listed it.** `PROPOSED_DEPLOY_SHAS` is `()` today, and its
own comment says the mechanism is kept because "the next candidate will need it".

So for this candidate:

1. **Propose** — a child commit adds the candidate SHA to `PROPOSED_DEPLOY_SHAS`. `_check_identity`
   reads only `REVIEWED_DEPLOY_SHAS`, so a proposed revision can be measured, argued about and
   rejected without ever being deployable.
2. **Promote** — a further commit (call it **P**) moves it into `REVIEWED_DEPLOY_SHAS`. **P is the
   record of the approval.** It is a human decision, made after reading this candidate, recorded
   in a commit the candidate cannot reach.
3. `CURRENT_DEPLOY_SHA` is **a separate edit and a separate decision**, in P or later. "Was this
   reviewed" is historical and does not expire; "which should be deployed now" is a current
   recommendation and moves. Collapsing them is what made retaining `3223fba` look like
   endorsing it.

**Why this composes.** `ops/p5_readiness.py` is **operation-only** — it is not one of the 23
service modules. So P changes no service module, and the 11 shared modules are byte-identical
between the candidate and P. The three bindings of §2 then hold with:

    deploy_sha    = the candidate      (the service checkout: HEAD == it, worktree clean)
    approved_sha  = P                  (the operation checkout, which is what reads
                                        REVIEWED_DEPLOY_SHAS and sees the candidate in it)

That is the §2 separation being used for the reason it exists, not worked around: P differs from
the candidate **only outside the 21 operation modules'** service-facing set — specifically in
`ops/p5_readiness.py`, which is itself an operation module, so the operation approval must name
**P**, not the candidate. Nothing is self-approving: P approves the candidate, and P's own
operation approval is an external record a person writes.

**Nothing here has been done.** `PROPOSED_DEPLOY_SHAS` is still `()`, `REVIEWED_DEPLOY_SHAS`
still holds two commits, neither of which is the candidate, and `CURRENT_DEPLOY_SHA` is still
`708058e`. A test asserts the candidate is absent from all of them.

### The order is not free

§5a fixes it: **(1) before (2), and the service deployment before the operation runs.** A
generation carrying a `content_seal` cannot be read by `708058e`, so publishing one while the
service still runs the old commit takes the base out of the served view under a healthy-looking
`/healthz`. Deploying the candidate first is safe in both directions, because an unsealed
manifest is readable by either commit.

`deploy_sha` and `approved_sha` should both be **this candidate**. They may differ in principle
(§2), but only if the two commits agree byte-for-byte on all 21 operation modules — and any
commit that satisfies that while differing elsewhere would need its own service review anyway,
so separating them buys nothing here and costs a second review.

## 7. What a green local suite says

That these gates refuse the right compositions **on this machine**. It is not a statement about
VM24, and it is not compaction readiness. No VM24 contact, no writes under `$G`, no approval
record, no push, no deployment, no provisioning or compaction was performed in producing this.
