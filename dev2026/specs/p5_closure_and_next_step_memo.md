# P5 closure and next-step decision memo

> **Status of this document.** Committed with the P4-S10 execution findings (review round 2,
> finding 7: a decision memo left untracked is a decision nobody can cite). It changes no code,
> no gate and no status; it exists to make the remaining decisions explicit before anyone
> reaches for a deployment.
>
> **Branch wording (finding 6).** P5-S5 Part 3 is the **signed-off tip** of
> `dev2026-p5-s5-part3-crash-proof`. PR #35 is open and still requires a human merge, as do
> #32 → #33 → #34 ahead of it. Nothing in this document should be read as saying that work has
> landed on `main`.
>
> **No VM24, production, cron, `GHRSST_*` store or deployment file has been touched.**

## 1. What is on the branch, and how it hangs together

`dev2026-p5-s5-part3-crash-proof` @ `8359482`, pushed; local `HEAD` and
`origin/dev2026-p5-s5-part3-crash-proof` are the same commit. Verified ancestry:

```
origin/dev2026-p5-s4-manifest-publish      -> ancestor  ✓
origin/dev2026-p5-s5-proof        (Part 1) -> ancestor  ✓
origin/dev2026-p5-s5-part2-repair-lifecycle-> ancestor  ✓
```

Nothing was rebased, squashed or amended: all nine Part 3 commits sit on top of the signed-off
Part 2 tip. PR **#35**, based on `dev2026-p5-s5-part2-repair-lifecycle`, stacked under #34 → #33
→ #32.

### Delivered evidence

| gate | verdict | evidence |
|---|---|---|
| **H4** — stable-old across a generation change | **PASS** | (a) value-asserted, (b) shrinking day set, (e) concurrent refresh, every observation wholly one generation |
| **G17** — build isolation + fencing | **PASS** | killed holder releases the `flock`; `SIGSTOP`ped holder still blocks and refuses without waiting; replaced lock inode makes the holder refuse to publish, with a companion test showing the replacement really does admit a second holder |
| §9 crash boundaries (d) | **PASS** | 8 boundaries, real §7.4 publication in a child killed with `os._exit(9)`, asserted on disk state |
| committed-but-unlogged + retry/recovery | **PASS** | `reconcile_publication()`; retry proves no double-publish, no double-hold, predecessor preserved |
| `(e2)` mechanism | **PASS** | dangerous pair reproduced, then shown to require an in-flight refresh across the swap |
| **G10** | **PARTIAL** | §2 below |

Gate numbers: crash suite **50/50**, Part 3 suite **53/53**, full local suite **876 OK** (17
skipped), `-W error::ResourceWarning` **439 OK**, **45 guards mutation-verified**.

`store/tiered_cube.py` and `store/segmented_cube.py` are **unchanged**, and this memo claims no
additional gate PASS.

## 2. Explicitly PARTIAL — four items, none of them closed

**(1) G10 — retained-snapshot deletion gap.** G10 says *"never a fabricated `None`"* without
qualification. A block the live manifest still references, deleted while a snapshot is open,
makes reads through that snapshot return `None`: Zarr opens lazily per read, so retaining the
snapshot *object* is not retaining the *data*. The snapshot **build** fails closed as (c)
requires; this arrives by a route (c) does not cover. Prevented by §8.5's lifecycle, not by the
read path. **Stop condition:** if ops ever hard-deletes inside `release_after_utc`, or a process
is seen serving `None` for a day the manifest lists, this clause is breached and the read path
must pin segment handles for a snapshot's life.

**(2) `(e2)` production closure.** The executor can check that a quiescence attestation is
present, well-formed and internally consistent. It cannot check that it is **true**. A hook that
reports `drained: True` without looking is accepted by construction. Closure needs ops evidence
from a real run.

**(3) Whole-VM snapshot rollback.** The WAL freshness anchor detects a different path and a
different filesystem, and fails closed when it cannot answer. It cannot by itself prove an
independent **rollback domain**. VM24 has had two whole-VM rollbacks; an anchor restored
alongside the WAL it checks proves nothing. Needs an ops-supplied anchor root outside the VM's
snapshot scope.

**(4) External approval record for the deployment SHA.** The runbook gate accepts only a full
immutable commit SHA. *Which* SHA is the **reviewed** one cannot come from the runbook: a commit
that records SHA *X* changes `HEAD` to something other than *X*, so the document cannot hold its
own pin. It must come from the sign-off note or a signed tag.

## 3. Explicitly NOT DELIVERED

- **`build_block` crash injection** — §9's first two rows (during build; after build, before
  validation). These are resume/discard cases owned by the builder's progress journal, and no
  crash was injected into them.
- **The composite base+delta fence implementation** — deliberately not built. The round-1 verdict
  was that the protocol closes `(e2)` when quiescence holds, so the fence was not needed; that
  verdict stands, and the fence remains unwritten.

## 4. Why P5 must not be deployed together with the upcoming VM24 delta prune

Not a scheduling preference — the two changes interact in a way that would destroy the evidence
either one needs.

**They both take the same lock, for opposite reasons.** A delta prune/swap holds the compaction
reservation and requires full request quiescence; a P5 publication takes the ingest lock and
(now) requires a proven drain. Running them in one window means a failure in either leaves an
on-disk state whose cause is ambiguous, and §9.4's whole posture is that an ambiguous state must
stop and wait for a human.

**The prune is the riskier half and it is already understood.** The delta prune has its own
signed-off executor, its own rollback, and a runbook that has been rehearsed. P5 publication has
none of that operational history. Shipping them together means the first real exercise of P5's
crash and recovery paths happens in the same window as a mutation that removes days from delta —
and if the served view is wrong afterwards, there is no way to attribute it.

**Three of the four PARTIALs are exactly the ones a prune would exercise first.** `(e2)`
production closure, whole-VM rollback and the G10 deletion gap are all about what happens when
something is removed while a reader is alive. A delta prune is the removal. Deploying P5 in the
same window makes its unproven operational assumptions load-bearing on day one.

**And VM24 is currently unstable.** Both the anchor-domain prerequisite and the quiescence
evidence require a machine whose behaviour can be trusted enough to interpret the result.

**Recommendation:** land the delta prune alone, on the existing signed-off P4 path, and let it
produce the quiescence evidence and the anchor-domain answer that P5's PARTIALs are waiting on.
P5 deploys afterwards, as its own change, with those two questions already answered.

## 5. Two separate follow-ups

### Option A — docs/spec-only decision on the remaining `(e2)` production closure

**Not VM24-dependent. Can start immediately. No code.**

Decide and record, in the design spec, which of these the project adopts:

1. **Accept the operational guarantee.** `(e2)` is closed by the quiescence protocol; the
   executor's job is to refuse without a well-formed attestation, which it does. The residual
   risk is a hook that lies, and that is an ops responsibility with an audit trail behind it.
2. **Require independent evidence.** The attestation must be corroborated by something the
   executor did not receive from the hook — a port probe it runs itself, or a reader-count read
   from a source the hook does not control. Larger, and it turns the executor into a monitor.
3. **Build the composite fence after all**, making `(e2)` a code-enforced invariant and demoting
   quiescence to defence in depth. The most expensive, and it reopens signed-off P5-S2.

Deliverable: a spec section and a recorded decision. My reading is that **(1)** matches what the
evidence supports and what the reviewer has already accepted twice; **(3)** should stay available
but unexercised unless a real incident argues for it.

### Option B — P5-S6 sizing / production-calendar validation

**VM24-DEPENDENT. BLOCKED until VM24 is explicitly approved, and not started now.**

P5-S6 measures H3/H5/H7 at S = 30/45/90 and C = 30/45/90 **on the production calendar anchor**,
because P5-S0 established that decompressed bytes are anchor- and offset-sensitive and a
synthetic sweep does not settle the real geometry. That makes it inherently a
production-calendar exercise.

Entry conditions, all of which must hold before it starts:

- VM24 is explicitly approved for read access and is stable;
- the delta prune has landed and settled;
- the anchor-domain question (PARTIAL 3) has an ops answer;
- the work is `[RO]` on the real store — measurement only, no mutation.

Until every one of those is true, P5-S6 is **not started**.

## 6. Test evidence for this memo

Commands run locally for this memo, verbatim, from `~/proj/ghrsst`:

```
dev2026/.venv/bin/python -m unittest dev2026.tests.test_phase2_p5s5_part3_crash
    -> Ran 50 tests, OK

dev2026/.venv/bin/python -m unittest dev2026.tests.test_phase2_p5s5_part3
    -> Ran 53 tests, OK

PYTHONWARNINGS=error::ResourceWarning dev2026/.venv/bin/python -m unittest \
  dev2026.tests.test_phase2_p5s4 dev2026.tests.test_phase2_p5s5 \
  dev2026.tests.test_phase2_p5s5_part2 dev2026.tests.test_phase2_p5s5_part3 \
  dev2026.tests.test_phase2_p5s5_part3_crash dev2026.tests.test_phase2_p4s8a
    -> Ran 439 tests, OK
```

The **876-test** full-suite figure quoted above and in the results doc is from the round-9 gate
run at this commit; it was **not** re-run for this memo, and is labelled as such rather than
presented as fresh evidence.
