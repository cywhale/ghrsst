# P5 — storage requirement for the legacy monolith, and how to verify it

**Audience: whoever owns the VM24 volumes, and the operator who will run the verification.**
Nothing in this document is executed by any code in this repository, and nothing in it should be
run until the storage layout has actually been changed. It states what P5 needs, why the
cheaper-looking alternatives do not work, and the exact read-only commands that establish
whether the requirement has been met.

No VM24 action has been taken to produce this. The geometry in §2 is from the collection of
2026-08-25 and is a snapshot, not a live reading.

---

## 1. What is being asked, and why it is not a formality

P5's first generation makes the existing monolith the **served authority** — a `legacy_base`
segment in a published manifest. From that moment, a change to the monolith is a change to what
the API returns, with no build step in between.

Two things in the code look like they cover this, and neither does:

| mechanism | what it actually does | why it is not the barrier |
|---|---|---|
| `content_seal` (a ledger of path, size and mtime) | notices that the store changed | **after the fact**, and blind to a write that restores size and mtime — demonstrated by a test, not assumed |
| `check_monolith_sealed()` (permission bits on the store root) | refuses an obviously-writable store at build time | `chmod` is reversible by anything with write access to the parent, a process running as root ignores it, and it says nothing about subdirectories |

Both are documented as necessary-not-sufficient where they are defined. **The barrier that
actually holds is a read-only mount**, and no code in this project can create one. That is the
dependency this document exists to hand over.

Until it is met, the `monolith_barrier` readiness requirement stays unsatisfied and
`compaction_ready` stays `false`. That is the intended behaviour, not a defect to work around.

---

## 2. Current geometry (as collected 2026-08-25 — a snapshot)

```
$G                  /home/odbadmin/Data/ghrsst
mount               /dev/sdc1  ->  /home/odbadmin/Data   (ext4, rw)   st_dev 2081
free                1252.1 GiB of 7054.4 GiB

monolith            $G/mur_timecube_s8_t90_sh128.zarr           GHRSST_TIMECUBE_PATH
delta               $G/mur_timecube_s8_t90_sh128.delta.zarr     GHRSST_DELTACUBE_PATH
daily               $G/mur.zarr                                 GHRSST_ZARR_PATH
p5_blocks           $G/p5_blocks                                (does not exist yet)
p5_repairs          $G/p5_repairs                               (does not exist yet)
```

**Everything is on one mount.** That is the problem in one line: `$G` cannot be made read-only,
because the delta receives a daily append, the repair WAL must be writable, and the P5 roots do
not exist yet and will be created there. Freezing that mount would leave a deployment that can
neither ingest, nor repair, nor compact.

---

## 3. Required target geometry

1. The **monolith path resolves through a genuinely read-only mount** — the mount, not the
   directory permissions.
2. **`p5_blocks`, `p5_repairs` and `delta` remain writable, on a different mount.** The
   readiness contract refuses a record that places any of them on the monolith's mount, because
   a mount is read-only or it is not and the two claims cannot both hold.
3. The monolith path is **not a symlink**: `path` and `realpath` must be equal. A link can be
   re-pointed without the mount record changing, which would make "what is frozen" and "what is
   served" different questions.

### On a read-only bind mount

A bind mount is worth considering and **must not be assumed to work**. Two specific reasons:

* A plain `mount --bind` inherits the source's read-write flag. Making it read-only needs a
  second step (`mount -o remount,ro,bind …`), and on some kernel/systemd combinations the
  remount silently does not take. `findmnt` output is the only evidence that it did.
* **More importantly: a read-only bind mount does not make the underlying files read-only
  through their original path.** If the monolith stays inside `$G` and is *also* exposed
  read-only elsewhere, then writes through `$G/mur_timecube_s8_t90_sh128.zarr` still succeed —
  and `$G` has to stay writable for the delta. The barrier would exist only for readers who
  happen to use the new path, which is not a barrier.

So the shape that actually satisfies the requirement is the monolith **relocated out of `$G`**
onto its own read-only mount (its own volume, or a filesystem whose only writable exposure is
withdrawn). A bind mount may be part of how that is arranged, but the write-denial test in §4.7
is what decides, and it must be run against **every path by which the monolith is reachable**.

Nothing in this document claims a bind mount is sufficient. §4 is how that claim would be
earned.

---

## 4. Verification checklist

**Run only after IT confirms the layout has been changed.** Every command here is read-only
except §4.7, which attempts one write **to the monolith** in order to be denied, and creates
nothing if the barrier holds. Nothing writes under `$G`.

Substitute the real paths if the layout moved the monolith.

```bash
MONO=/path/to/mur_timecube_s8_t90_sh128.zarr     # the NEW monolith location
G=/home/odbadmin/Data/ghrsst
DELTA=$G/mur_timecube_s8_t90_sh128.delta.zarr
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
```

### 4.1 Mount facts for every path the contract names

```bash
for p in "$MONO" "$G" "$DELTA" "$G/p5_blocks" "$G/p5_repairs"; do
  printf '%s\n' "$p"
  findmnt -T "$p" -o SOURCE,TARGET,FSTYPE,OPTIONS --noheadings 2>&1 | sed 's/^/    /'
done
```

`p5_blocks` and `p5_repairs` do not exist yet; `findmnt -T` reports the mount of the nearest
existing ancestor, which is the answer the contract wants for them.

### 4.2 Device numbers

```bash
for p in "$MONO" "$G" "$DELTA"; do
  printf '%-60s st_dev=%s\n' "$p" "$(stat -c %d "$p")"
done
```

The monolith's `st_dev` must differ from `$G`'s. Equal device numbers mean one filesystem, and
therefore one read-only flag for both.

### 4.3 Realpath and symlink check

```bash
printf 'path     %s\nrealpath %s\n' "$MONO" "$(realpath "$MONO")"
test "$MONO" = "$(realpath "$MONO")" && echo "  not a symlink: OK" || echo "  SYMLINK: refused"
ls -ld "$MONO"
```

### 4.4 Read-only option token

```bash
OPTS=$(findmnt -T "$MONO" -o OPTIONS --noheadings | tr -d ' ')
echo "options: $OPTS"
echo "$OPTS" | tr ',' '\n' | grep -qx 'ro' && echo "  read-only: OK" || echo "  NOT read-only: refused"
```

Matched as a whole token, deliberately. `relatime` contains the letters `ro`, and a substring
test would report a read-write mount as frozen.

### 4.5 Writable-path separation

```bash
MONO_TGT=$(findmnt -T "$MONO" -o TARGET --noheadings | tr -d ' ')
for p in "$G/p5_blocks" "$G/p5_repairs" "$DELTA"; do
  T=$(findmnt -T "$p" -o TARGET --noheadings | tr -d ' ')
  O=$(findmnt -T "$p" -o OPTIONS --noheadings | tr -d ' ')
  status=OK
  [ "$T" = "$MONO_TGT" ] && status="SAME MOUNT AS MONOLITH: refused"
  echo "$O" | tr ',' '\n' | grep -qx 'ro' && status="READ-ONLY: refused"
  printf '%-46s target=%-28s %s\n' "$p" "$T" "$status"
done
```

### 4.6 Every path by which the monolith is reachable

If a bind mount is used, list the other paths to the same filesystem and repeat §4.4 and §4.7
for each. A read-only view does not constrain a read-write one.

```bash
findmnt -o SOURCE,TARGET,OPTIONS | grep -F "$(findmnt -T "$MONO" -o SOURCE --noheadings | tr -d ' ')"
```

### 4.7 Write-denial test, as the service user

The only step that attempts a write. It must **fail**.

```bash
sudo -u odbadmin touch "$MONO/.p5_write_denial_test" \
  && { echo "WROTE: the barrier does not hold"; sudo -u odbadmin rm -f "$MONO/.p5_write_denial_test"; } \
  || echo "write denied: OK"
```

Run it as the user the API and the ingest cron actually run as (`odbadmin` at the time of
writing — confirm before trusting the result). A denial for `root` proves nothing about the
service, and a denial caused by permission bits rather than the mount is not the barrier: §4.4
is what distinguishes them.

If the file *is* created, remove it and record that the requirement is not met. Do not adjust
permissions to make the test pass — that would be reintroducing exactly the mechanism §1 rules
out.

### 4.8 Nothing was written under `$G`

```bash
find "$G" -newermt "$(date -u -d '10 minutes ago' '+%Y-%m-%d %H:%M:%S')" 2>/dev/null | head
```

Empty is the expected result. The scheduled delta append writes under `$G` on its own cadence;
if it ran during the checklist, its log will say so and that is not this checklist's doing.

---

## 5. What the readiness contract does with the output

The verification above produces exactly the fields `monolith_barrier` adjudicates:

| checklist step | record field |
|---|---|
| §4.3 | `monolith_path`, `monolith_realpath` |
| §4.1 | `source`, `target`, `fstype`, `options` |
| §4.2 | `device` |
| §4.5 | `writable_paths[]` — `role`, `path`, `source`, `target`, `options` for `p5_blocks`, `p5_repairs`, `delta` |
| the run itself | `run_id`, `host`, `collected_utc` |

The record must be collected **in the same run** as the rest of the readiness evidence, on the
host it describes, and inside the **2-hour** freshness window — a mount can be remounted
read-write at any time, so an older record says what was true then.

`§4.6` and `§4.7` are not fields. They are what an operator must have satisfied before treating
the record as meaningful, and they belong in the run's notes.

---

## 6. Status while this is open — fail closed

* **No mount-barrier record exists**, so `monolith_barrier` is `NOT_ADJUDICATED`: missing
  evidence, not a barrier shown to be absent. It blocks readiness either way.
* **`compaction_ready` is `false`** and no local result claims otherwise.
* **`S = 90` / `C = 30` remain provisional.** The generated manifest names them as policy and as
  unselected; selecting them needs an explicit operator decision recorded in the roadmap, and
  there is none.
* **No VM24 evidence will be collected until the operator is ready to continue**, because every
  readiness figure expires two hours after collection and a collection nobody is ready to act on
  expires unused.

## 6a. The one-shot exception path, and what it does not buy

A human has accepted the risk of running **one** operation over a still-writable monolith,
because VM24 has full automated VM backups. That is a claim about **recoverability**, not about
likelihood, and nothing in the code treats it as the latter. It does not weaken anything in
§1–§5: the requirement above stands, and the read-only mount remains the preferred and stronger
route.

### It authorizes exactly the operation it names

The record names an `operation`, and there are two:

| operation | what it does | what it removes |
|---|---|---|
| `provision_generation_one` | publishes a `legacy_base` manifest over the monolith | nothing |
| `one_shot_compaction` | builds a block from the delta and publishes it as the next generation | **nothing** — it deletes nothing at all |

Vague aliases — `compaction`, `compact`, `build_blocks`, `fold` — are refused *by name* with a
pointer to the exact one. "Compaction" names several things, and an approval should say which
code path it authorizes.

The **destructive family** (`prune`, `swap`, `delete`, `switch`, `finalize`, …) is refused by
name too, and nothing implements any of it. Releasing the old space is a separate operation that
does not exist yet; when it is written it must call
`p5_oneshot_exception.refuse_destructive_step()`, which requires **both** a finalization
clearance and a separate human approval. The gates passing means the new generation is
trustworthy; it does not mean anyone decided the old data should go, and those are different
judgements.

### The executing code must be reviewed, and that is a separate question from approval

A production approval names a live root and a run. It says nothing about whether the code about
to publish a served authority was ever reviewed, and an approval for the right root running
unreviewed code is a real failure mode — the live checkout was once observed at HEAD plus five
uncommitted edits.

So `one_shot_compaction` **requires an identity artifact** and checks it before any lock is taken
or any scan begins:

| bound to | refused when |
|---|---|
| `deploy_sha` | it is not in `p5_readiness.REVIEWED_DEPLOY_SHAS` — a deployment cannot declare its own commit reviewed |
| `head_sha`, `worktree_clean` | HEAD is not that commit, or the tree carries uncommitted edits |
| `module_fingerprints` | any digest differs from the blob **git holds at that commit**, or the inventory is short by one module, or it contains one the commit does not |
| `host`, `run_id`, `collected_utc` | another machine, another run, or past the freshness window |

The comparison is exact in both directions and recomputed from the repository, not against a
list the artifact supplies. A matching commit id says what git holds; fingerprints say what is
on disk, and only the second is what executes.

### Parity evidence is executed and bound, not a callable that says yes

The parity producers stay injectable — they must read the deployment's own stores through the
deployment's own reader, and a parity check written inside the operation would be the operation
comparing its output against its own idea of the truth. What is **not** optional is what they
return.

A callable returning `{"mismatches": 0}` is refused. Each result must be an artifact carrying
host, run, `deploy_sha`, the **candidate** generation and manifest checksum, the source-map
digest, an execution timestamp, real sample and mismatch counts, and written verification
provenance — and its `producer_digest` must be the digest of a module the identity artifact
fingerprinted. Reviewed code has a digest git holds; a callable defined in a shell does not, and
cannot acquire one. Zero mismatches out of zero samples is refused as an absent check rather
than accepted as a passing one.

### A generation that fails its gates is QUARANTINED

Post-publish gates run after generation N+1 exists. If one fails, raising alone would leave that
generation live and unclassified: the next run reads the live manifest, plans N+2 on top of it,
and the failure now sits underneath a generation nobody questioned.

So the operation writes `.p5_generation_quarantine.json` into the manifest root — durably, and
**before** it raises — naming the generation, the run, the reasons, and the predecessor archive.
Every later publish, switch and finalization refuses while it exists, including a finalization
holding a valid clearance and a human approval: removing data underneath an unverified authority
is the worst available order of operations.

It is deliberately **not** an automatic rollback. Restoring the predecessor is itself an
authority change, and performing one in the failure path on the strength of a gate the run could
not satisfy is a second unreviewed change chasing the first. The predecessor archive is immutable
(§9.3) and intact, so a person can still roll back with `execute_rollback` — what the record
guarantees is that nobody proceeds *without* deciding. Reconciling means rolling back or
establishing by review that the generation is sound, and then removing the file.

### The gates are enforced now

`post_compaction_problem()` is no longer merely callable.
`ops/p5_oneshot_compaction.compact_one_shot()` assembles the evidence **from its own state** —
the generation it published, the manifest checksum it got back, the monolith it measured under
the locks, and a before/after inventory — and refuses the run and issues no clearance if any
gate fails. `post_compaction_enforcement()` reports `ENFORCED` and names it.

Two of the gates are checked **before** the publish, while the block is still unreferenced:
point-series parity against the per-day oracle (first and last days included) and source-map
completeness, plus base/delta disjointness on the *planned* manifest. A failure there costs a
rebuild rather than a rollback. Only the checks that need a published manifest — its checksum,
the WAL/anchor binding, API parity, fresh capacity — run afterwards.

`nothing_deleted` is **re-derived, never believed**. The gate takes the raw before/after
inventory the operation measured, recomputes the verdict from it, and refuses if the recomputed
answer and the recorded one disagree — so a hand-written `true` over an inventory showing a
shrunken store fails.

### It is a **record**, not a flag — and what opens the door is a capability

There is no `force`, no `skip_seal`, no `allow_unsealed`, no `require_sealed=False` and no
environment variable — a test asserts none exists. `require_sealed` was removed from
`ops/p5_legacy_base.py` outright: it had no caller that ever passed `False`, so it was a bypass
sitting unused with the door open.

What opens the unsealed path is an `UnsealedMonolithAuthorization`: an object a caller **cannot
construct**, cannot pickle or copy, and cannot use twice. It is minted only by
`authorize_unsealed()`, after the whole chain below passes, and it is **consumed by its single
use**. A truthy stand-in is refused at the door — that is the one place where accepting a
duck-typed value would make it a flag after all.

The record binds to:

| bound to | refused when |
|---|---|
| `scope` = one-shot compaction risk acceptance | it approves anything else |
| `operation` | it names anything but `provision_generation_one` — the compaction family by name |
| `host`, `run_id` | another machine, or another run — **one-shot means one run** |
| `monolith_realpath`, `monolith_metadata_fingerprint`, `monolith_day_digest`, `monolith_content_ledger` | the store changed after the risk was accepted |
| `backup_id`, `backup_completed_utc`, `backup_verified`, `backup_retention_ref` | unattested, or completed *after* the approval was signed |
| `expires_utc` | now is past it, it expires at or before approval, or it outlives the capacity reading it rests on |
| `approved_by`, `approved_utc`, `reason` | the reason is not written in words a reviewer can weigh |
| `residual_writer_risk` | the unfenced-writer residue was not explicitly accepted, in writing, as possibly unrecoverable |

### The measurement is taken, not accepted

Verification against the record alone is refused: a fresh measurement of the monolith is
required, or the record is only being checked against itself.

Nor is a self-consistent pair enough. The record and a supplied measurement agreeing proves only
that whoever assembled them was consistent — both could describe the store as it *was*. So there
is no `one_shot_observed` parameter anywhere: the **operation** measures the store itself, at the
point it acts, under both locks, and compares what it finds against the record *and* the
same-run preflight. A record naming a different store is refused however well it matches its own
evidence.

The consequence for reporting: `ops/p5_readiness.py` reads a collected JSON document, so every
measurement it sees is caller-supplied by construction. Its verdict is therefore **never**
`eligible` — a green record shows `eligible_pending_recomputation`. A report is not an
authorization.

### The writer fence: a count is not a fence

`monolith_writers.count == 0` says what was true at one instant. The scan runs for minutes, and
the append cron does not consult it. So the exception additionally requires a **writer fence**:
the writer-producing services stopped (`stopped` / `disabled` / `not-installed`) and the append
cron disabled (`disabled` / `commented-out` / `not-installed`) for the whole window. "No append
window right now" is a statement about the clock, and the clock moves during a long scan.

The compaction operation probes the fence **three times** — before the scan, after the build,
after the publish — validates each probe and compares the service sets. There is no default
probe that reports a posture: the shipped default **refuses**, because whether `pm2` stopped a
service is a fact about the deployment host that nothing in this process can observe, and a
plausible-looking default would make every local test pass while proving nothing.

**What probing does not establish.** These are samples. A service that stopped and restarted
entirely between two of them is invisible to all of them, and nothing here can close that —
proving a posture held continuously needs something watching continuously. The fence is a
precondition an operator arranges, not a guarantee this code provides, which is why the
content-ledger recheck and the parity gates sit behind it.

**So the residue is accepted explicitly or the operation stays ineligible.** The record carries
`residual_writer_risk`: a written acknowledgement, naming who accepted it, that an unfenced
writer can change the store during the interval and that the outcome may be **unrecoverable** —
the parity gates detect such a change afterwards, and a VM restore does not undo what was served
in between. `unrecoverable_outcome_possible: false` is refused rather than accepted, because it
records a belief the code does not share. This is a judgement in the approved record, not a
parameter, because it is a decision and not a setting.

`flock` is advisory and the fence must say so: the ingest lock excludes the ingest path because
the ingest path takes it, and it excludes nothing else. An arbitrary writer — a shell, a stray
script — obeys neither the lock nor the service posture. The record must state the policy that
covers that, because a fence that does not name what it fails to cover reads as stronger than it
is.

### The fence widens, because the store can move

On the sealed path the ingest lock covers the **publish alone**: the store cannot change during
the expensive scan, and blocking the daily-append cron behind a minutes-long walk was a real
defect that was fixed. Under an authorization the store **can** change, so the ingest lock is
taken **before** the expensive scan and held through the publish, with the compaction lock still
outermost. §7.1b ordering is preserved; its *scope* widens. **A one-time writer count is not a
fence** and is not treated as one — the count is required, and it is the lock that does the
excluding.

The cost is real and is the trade the exception buys: the daily-append cron blocks for the whole
walk.

Before publishing, the **full content ledger is re-verified** — the second expensive walk. It
*detects* a store that changed; it does not prevent one, and it cannot see a write landing during
its own traversal. That residual window belongs to the writable layout, not to the check, and it
is why the read-only mount is the stronger route.

### The backup is attested, not proven

`backup_verified: true` is a person's word. Nothing available to this process can reach the
hypervisor's backup catalogue, mount the image, or confirm a restore would succeed, so it is
reported as **NOT_ADJUDICATED** — the same treatment `immutable: true` gets in a segment.
The backup id and the verification provenance are bound to the record and to the run, so the run
is covered by the backup the risk was accepted against and not some other one; that binding is
checkable, and the restore working is not.

### It never becomes readiness

`monolith_barrier` keeps whatever verdict it earned, `compaction_ready` stays `false`, and the
exception is reported in its own field — `one_shot_compaction_exception`. An eligible exception
beside a `BLOCKED` barrier is the expected shape, not a contradiction: the two say *"a person
accepted this risk once"* and *"this deployment is safe to compact"*, which are different
claims.

### The surrounding checks are not relaxed

**Before**, each a validated result bound to this host, this run and a collection time, not a
field that merely exists: fresh capacity within the canonical window and bound to the device
this run writes to · current healthz status and served day · identity on a clean reviewed commit
· process/PM2/cron quiescence · **no process holds the monolith for writing** · both locks free
· fresh monolith fingerprints and content ledger matching the record · the attested backup ·
the exact source map and destination manifest plan.

**After** — for a compaction, once one exists — all of which must pass **before anything is
deleted or any service is switched**: manifest checksum and generation · WAL/anchor binding ·
source-map completeness · base/delta disjointness · point-series parity against the monolith
oracle **including the first and last days** · API response parity · a fresh capacity
measurement · and an explicit `nothing_deleted: true`. The ordering is the point — a parity
failure found after the old data is gone is a parity failure with nothing to go back to.

### Rollback limits

The acceptance rests on the backup, so what the backup cannot undo is part of it:

* restoring the VM rolls back the **whole machine**, not the P5 roots alone — every other
  service on it loses the same interval;
* delta days appended between the backup and the restore are lost, and the ingest cron will not
  re-fetch them on its own;
* any repair committed to the WAL in that interval is lost with it, and the anchor's
  high-water mark will refuse the restored log as rolled back — which is correct, and is manual
  recovery;
* requests already answered from a bad generation cannot be recalled;
* the restore is decided and started by a person, so the exposure is **detection time plus
  restore time**, not restore time alone.

## 7. Where this sits

```
>>> 1. IT mount-layout decision  <<<   this document
    2. fresh VM24 read-only collection
    3. candidate deployment review / approval
    4. provisioning approval
    5. provision generation 1 + WAL
    6. API switchover approval
    7. compaction approval
```

Step 1 gates every step after it. Nothing further is buildable locally until the layout question
is answered, which is why this is a requirement handed over rather than more implementation.
