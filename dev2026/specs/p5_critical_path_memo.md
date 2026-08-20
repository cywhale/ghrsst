# P5 — roadmap exception, capacity unlock, and the critical path

**LOCAL-ONLY PLANNING DOCUMENT.** Nothing here was executed. No VM24 contact, no production
action, no cleanup, and **no capacity claim derived from any local synthetic measurement**. The
authority for every declaration below is `ops/p5_roadmap.py`; this document is checked against
it by `tests/test_phase2_p5_roadmap.py`, so the two cannot drift.

**The S7 gate remains `NOT MET`.** P5-S7's *local implementation* is signed off at `f85a96d`.
That is not a gate pass and is not recorded as one anywhere.

## The objective this serves

The goal is not to finish S7. It is to **complete safe compaction, materialize immutable blocks,
and then release only the old daily/hold data that the retention and recovery policy permits.**
Capacity is on the critical path for that, not a footnote: at 267 GiB free against a 403.3 GiB
minimum, the block build cannot even be attempted.

## 1. Roadmap exception P5-EX-1

§14 P5-S7 says S8 is not authored until the S7 gate is green. Read literally, that stops
everything — because the gates keeping S7 red are the ones only production can settle:

| gate | why local work cannot move it |
|---|---|
| **G3** | p95 against the *current production* `TieredCube` baseline on VM24 |
| **G4** | 366-day p95 at production geometry (17999 × 36000) |
| **H7** | fd/RSS under sustained load on the production process, plus inode headroom |
| **G16** | metadata-scan wall time and snapshot-open cost on the production tree |
| **G18** | free disk on `/dev/sdc1` — **BLOCKED**, 267 GiB against a 403.3 GiB minimum |

Waiting for those before authoring the read-only runbook that **collects** them is circular: the
runbook is the instrument, not the conclusion.

**The exception, in full:**

- The **S7 gate remains `NOT MET`**. This is not a pass, a partial pass, or a waiver.
- **S8 runbook authoring is allowed**, because G3/G4/H7/G16 are production-dependent and cannot
  be adjudicated by local synthetic tests.
- **S9 may later execute read-only / shadow evidence collection only**, and only after a
  **separate human approval** that this exception does not confer.
- It authorizes **no** prune, swap, compaction, repair, deletion, production write, or
  deployment.
- **S10 still requires a fresh written approval.** An S9 PASS does not confer it.
- It **expires** when G3/G4/H7/G16 are adjudicated from production evidence and G18 is
  unblocked. At that point S7 is re-adjudicated on its merits.

`ops/p5_roadmap.py::policy_permits()` states the policy in executable form and **fails closed**:
anything not on the permitted list answers `False`, so a mutating verb added later is refused by
default rather than by someone remembering to list it.

**It is a policy predicate, not a control.** Nothing calls it before an `ssh`, a prune or a
deployment, and it **does not prevent S9 or S10 from being executed manually**. What actually
stops those is the approval gate and the runbook's own NO-GO preflights; the predicate exists so
tests can check the written policy for self-consistency. `POLICY_PREDICATE_IS_ENFORCED` is
`False` in the module, and a test asserts no document claims otherwise.

## 2. Capacity unlock — the decision

**These figures are historical and provisional.** They come from the VM24 read-only run of
**2026-08-19** (`bench/results/p5s6_vm24_readonly.json`) — the newest evidence that exists, and
also a one-day snapshot of a volume that has been filling since. **A current read-only scan log
must be available before any go/no-go rests on them**, and when it is, every dependent artifact
is regenerated from that evidence rather than edited by hand.
`/dev/sdc1`, ext4, mounted at `/home/odbadmin/Data`, 6.0 T total, 96 % used, **267 GiB free**.
The gate needs **403.3 GiB** (`HARD_RESERVE` 250 + additional 153.3); **600 GiB** is the
recommended operational target — the minimum plus roughly 200 GiB of growth margin, which at the
measured 2.74 GiB/day combined ingest is about 70 days of headroom.

**`HARD_RESERVE` is not lowered to make G18 pass.** Lowering it changes the reported verdict
without changing a single byte of free space.

| | **A — expand the data volume** (preferred) | **B — external staging / compaction volume** |
|---|---|---|
| **Target** | `free_now ≥ 403.3 GiB`; **600 GiB recommended** | enough free space on the staging volume, measured |
| **Topology change** | none | a second filesystem enters the commit path |
| **Must prove** | `df` shows the free space, on the **same** filesystem as `$G` | everything in the list below |
| **Why this ranking** | nothing about the storage topology changes, so every path semantic the manifest authority relies on is already proven | the design's commit point is a same-filesystem rename, and each obligation below is a way that assumption can silently fail |

**Option B's proof obligations** — all of them, before it is viable:

1. **Required free space**, measured on the staging volume, not assumed.
2. **Mount persistence** across reboot (fstab or a systemd unit) and the **correct filesystem
   type**. `flock` is unreliable on NFS (§16-Q15), and the build isolation lock depends on it.
3. **Atomic publication semantics.** `os.replace` is atomic only *within* a filesystem. A
   cross-device rename is **not** a commit point and must not be treated as one.
4. **Rollback and backup coverage** for the staging volume — including whether a whole-VM
   snapshot covers it. This is the WAL anchor-domain question in another form.
5. **No unsafe cross-filesystem rename assumptions** anywhere in the build or publish path.
6. **The final block location stays under the manifest root**, so segment paths still resolve
   inside it and the containment guard still holds. (That guard already refused an
   out-of-root block during S7 testing — it works, and option B must not route around it.)

### What capacity does and does not unlock

**Expanding capacity enables compaction** — the block build and publication can be attempted.

**It does not by itself authorize deleting the 1267 daily holds**, or any delta backup. The hold
audit found **0.0 GiB currently reclaimable**, and expanding the real day sets made two entries
*more* blocked, not less: the two expired delta backups run to 2026-07-08 and 2026-07-26, past
base's 2026-06-26, so base cannot serve those days at all.

Deletion additionally requires:

- a **recovery/parity policy decision** by ops;
- **recovery evidence** of one of the three accepted kinds — verified value parity, a recorded
  external manifest, or an immutable generation with fingerprint provenance;
- and explicitly: **day-set membership is not sufficient.** A new block covering the same days
  proves coverage, never value parity. Deleting on membership alone is the failure mode the hold
  audit was rewritten three times to stop asserting.

## 3. Critical path

| # | step | actor | status |
|---|---|---|---|
| 1 | P5-S7 local implementation sign-off (`f85a96d`) — **not** a gate pass | Claude + Codex | **COMPLETE** |
| 2 | Roadmap exception + S8 runbook authoring, local only | Claude | **IN_PROGRESS** |
| 3 | IT capacity expansion, or an approved staging-volume design | IT / ops | **BLOCKED_ON_OPS** |
| 4 | Fresh VM24 read-only / shadow approval | human | **NOT_REQUESTED** |
| 5 | Execute S9 read-only evidence collection | Codex / ops | **NOT_STARTED** |
| 6 | Re-adjudicate G3 / G4 / H7 / G16 / G18 | Codex | **NOT_STARTED** |
| 7 | Fresh S10 approval, if gates and capacity pass | human | **NOT_STARTED** |
| 8 | First compaction, in the **existing quiesced posture** | Codex / ops | **NOT_STARTED** |
| 9 | Classify old daily/hold data under the retention/recovery policy | ops | **NOT_STARTED** |
| 10 | Record **actual** freed bytes with a fresh `df` | ops | **NOT_STARTED** |

**Step 8's posture is not negotiable and not paraphrasable:**

> **compaction lock** → ingest lock → staleness/prechecks → quiesce →
> build/publish/verify as applicable → **prune/swap only after verification** →
> restart/API verification

The **compaction lock comes first and is held across the whole critical section**, released only
after restart/API verification. The ingest lock fences ingest; only the compaction reservation
(§7.1b) fences a second compaction, so a build that takes the ingest lock first is unfenced
against exactly the writer that can invalidate it. Releasing it after the build would leave
publish, verify and prune — the half that mutates the served view — open to a second compactor.
Staleness and prechecks run **inside** both locks: checked before them, they describe a state
another writer may already have changed.

Even if H4 passes, the first production publication uses the existing quiesced posture. Dropping
quiescence is a separate, later, evidence-backed change (§16-Q5) — the S8a lesson is that
"should be safe by construction" is exactly the claim that failed last time.

**Step 9 never runs on membership.** A new block having the same day membership as a hold is not
a reason to delete the hold.

**Step 10 is `df`, not arithmetic.** Theoretical reclaim is not capacity evidence. The figure
that counts is measured free space after the fact.

## Review round 2 — what the first draft got wrong

| finding | correction |
|---|---|
| the runbook wrote its artifacts to `$G/logs/...` with `mkdir -p` and `tee` | a **write under the production data root**, in a document promising none, consuming the space G18 is blocked on. `$ART_ROOT` is now operator-supplied and verified to be outside `$G` **and** off the production device |
| it demanded `ANCHOR_ROOT`, WAL state and a manifest generation before *legacy* baselines | the host has **no P5 roots**, so the runbook was unrunnable there. Split into `legacy_baseline` and `p5_shadow`; pre-P5 fields are `not_applicable_with_reason`, never omitted |
| the posture started at the ingest lock | corrected to open with the **compaction lock**, held across the whole critical section. The ingest lock fences ingest; only the compaction reservation fences a second compactor |
| commands mixed local shell, `ssh`, VM24 paths and `localhost` calls | every block is marked **Run on: LOCAL** or **Run on: VM24** |
| G4 timed whatever came back | fails closed on any non-200, verifies the span is **exactly 366 days**, and checks the row count before sampling |
| `pgrep -f … \| head -1` | PM2 `jlist` via the shipped `ops.quiescence.worker_pids`, **aggregated across every worker** |
| `os.walk` swallowed `OSError` | `onerror=` raises, so a permission-denied subtree cannot silently shrink the tree |
| capacity figures read as current | labelled **historical/provisional, 2026-08-19**; a current scan must precede any go/no-go |
| `authorizes()` implied a control | renamed `policy_permits()`, documented as advisory; it does **not** prevent manual S9/S10 execution |

Thirteen isolated mutations — one per guard — were run serially from a git-clean tree and all
were killed. Two guards had to be corrected first: both matched **prose** rather than the
command, so one flagged the sentence describing the old defect and another survived deletion of
the check it was meant to protect.

## Review round 3 — the command blocks are executed, not read

Text guards prove a check is *written*; they cannot prove it *fires*. Round 3 replaced them with
a suite that extracts each `# block:` fragment, runs it under `bash` against stubbed `findmnt`,
`pm2`, `curl` and fake `/proc` trees, and asserts a **non-zero exit** when a failure is injected
— plus a good path, so an always-failing block cannot pass as a working guard. Nothing contacts
VM24.

| finding | correction |
|---|---|
| `$ART` was created in a LOCAL block and written from VM24 blocks | two different machines; every later `> "$ART/..."` would have failed. Artifacts are now created **on VM24** and copied back with `scp` (§8a) |
| blocks were not independently fail-closed | every tagged block starts `set -euo pipefail`, so a failing `python \| tee`, `pm2 jlist`, `findmnt`, `rsync` or scan cannot leave an accepted artifact |
| the G4 probe timed whatever returned | validates **status, JSON shape, row count and span** before the first sample, and discards the run on any failure — including a non-200 *during* sampling |
| H7 read the pid set once | PM2 `jlist` is refreshed **every sample** and the **pid set** is compared, not its size, so a replaced worker is caught even though the count is unchanged |
| `p5_shadow` rsync'd one delta | the shadow is **pre-provisioned by ops** and *verified*: manifest, every referenced block, WAL root, and an anchor that declares a rollback domain |

Fifteen mutations were run against the blocks. Twelve were killed outright; three are **true
equivalents**, verified rather than assumed by re-running the whole suite under each mutation
and confirming the block still refuses every bad input — a second guard catches the same case
(JSON shape is also caught by the row-shape assertion, a failing `pm2` by the empty-pid-set
check, a missing manifest by the unreadable-manifest handler).

Two mutations exposed real coverage gaps rather than equivalents, and both are now closed:
deleting the **anchor domain-declaration** check broke nothing, because the only anchor test
removed the whole directory; and deleting the **containment** check broke nothing, because every
fixture put the path on the production device as well. The second needed a bind-mount fixture —
a path inside `$G` reporting its own device — which is precisely the case containment exists to
catch and the device check cannot.

## 4. Remaining blockers

| blocker | state |
|---|---|
| **G18 capacity** | **BLOCKED** — 267 GiB free, 403.3 GiB minimum, 600 GiB recommended |
| **G3 / G4** | `NOT_ADJUDICATED` — production thresholds |
| **G7** | `PARTIAL` — the gate's arithmetic is proven; a real fold's temp disk is not measured |
| **H7 / G16** | production evidence incomplete |
| **S = 90 / C = 30** | **provisional only**; nothing has selected it |
| **S8 execution** | not approved |
| **S10** | not authorized; needs its own fresh written approval |

## 5. The exact next approval needed

**IT capacity expansion (option A) to at least 403.3 GiB free — 600 GiB recommended — or an
approved staging-volume design meeting all six option-B obligations.**

That unblocks critical-path step 3. It is **not** implied by the P5-EX-1 exception, and it is
separate from the fresh VM24 read-only / shadow approval that step 4 needs before any S8 or S9
command runs against the host.
