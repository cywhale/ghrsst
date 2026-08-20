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
Capacity is on the critical path for that, not a footnote: at **295.7 GiB** free against a
403.3 GiB minimum (measured 2026-08-20), the block build cannot even be attempted.

## 1. Roadmap exception P5-EX-1

§14 P5-S7 says S8 is not authored until the S7 gate is green. Read literally, that stops
everything — because the gates keeping S7 red are the ones only production can settle:

| gate | why local work cannot move it |
|---|---|
| **G3** | p95 against the *current production* `TieredCube` baseline on VM24 |
| **G4** | 366-day p95 at production geometry (17999 × 36000) |
| **H7** | fd/RSS under sustained load on the production process, plus inode headroom |
| **G16** | metadata-scan wall time and snapshot-open cost on the production tree |
| **G18** | free disk on `/dev/sdc1` — **BLOCKED**, 295.7 GiB against a 403.3 GiB minimum |

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

**Measured 2026-08-20**, run `20260820T110036Z`, deploy SHA `96ee79c`, host `odb24` — the S8
`legacy_baseline` collection. `/dev/sdc1`, ext4, mounted at `/home/odbadmin/Data`.

| | |
|---|---|
| free now | **295.7 GiB** (317,551,714,304 B) |
| minimum required | **403.3 GiB** (`HARD_RESERVE` 250 + additional 153.3) |
| shortfall | **≈ 107.6 GiB** |
| recommended | 600 GiB — the minimum plus ~200 GiB growth margin, ≈ 70 days at the measured 2.74 GiB/day ingest |
| **verdict** | **G18 BLOCKED** |

Every figure above is **generated** by `ops/p5s8_evidence.py` from
`bench/results/p5s8_runs/p5s8_20260820T110036Z/`, never typed. The 2026-08-19 reading of
267 GiB is **historical** and must not be quoted as the current state.

**The +31.4 GiB since the aborted 08:00 run is operator-reported attribution**, not a
measurement: unrelated files under `~/Data` were moved by the operator, and the SRTM/terrain
directory remains. It is **not** GHRSST reclaim, **not** automated cleanup, and **not** a
sustainable capacity guarantee. The artifacts show that free space changed; they can never show
why. G18 is BLOCKED either way.

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

## Review round 4 — promises made executable

| finding | correction |
|---|---|
| `p5_shadow` claimed downstream ran against the shadow, while G4 called `127.0.0.1:8080`, H7 read production PM2 and `/proc`, and G16 walked `$G` | the profile is **narrowed to verification only**. Building an isolated shadow service is out of scope for a read-only step, so rather than leave the ambiguity it now claims nothing: **G4, H7's sustained half and both halves of G16 stay `NOT_ADJUDICATED`** under it |
| the artifact table listed files no block created | `ops/p5s8_report.py` is the single definition of what a run must produce; blocks write each one, and §8b **refuses to assemble** if any is missing, unparseable or incomplete. Nothing is written on refusal — a half-report is the most dangerous artifact of all, because every figure in it is real |
| the shadow had no filesystem gate of its own | `shadow-fs-gate`: existence, realpath containment, findmnt success, non-empty sources, and a device that is not production's |
| provisioning used hand-rolled `os.path` checks | replaced by **`SegmentedCubeStore`**, the canonical validator the read path already uses — which is what rejects absolute paths, `../` traversal and symlink escape |
| the identity preflight was not executable | now a tagged, fail-closed block, with a companion that records `preflight.json` including `not_applicable_with_reason` for every field the profile cannot answer |

Eleven mutations, serial from a git-clean tree: ten killed, one **true equivalent** verified by
re-running the whole suite under it. Two survivors were real gaps, both closed — the JSON parse
guard was only ever exercised through `preflight.json`, whose field checks caught the corruption
anyway (a corrupt `pm2_jlist.json` has no field checks); and the non-empty mount-source guard
only mattered when **one** source was empty, since two empty sources compare equal and trip the
device check for the wrong reason.

## Review round 5 — the sequence runs, end to end

| finding | correction |
|---|---|
| `preflight-record` read `ANCHOR_ROOT`, `WAL_ROOT`, `MANIFEST_ROOT`, `MANIFEST_GENERATION`, `FILESYSTEM_DEVICE`, `FILESYSTEM_FSTYPE`, `FREE_NOW_BYTES` and `INODES_FREE` from the environment, which nothing set | every field is now **derived from an artifact an earlier block wrote** — `capacity.json` from `statvfs`+`findmnt`, the P5 fields from `shadow_verification.json`, the WAL count from `wal_state.json`. Run in order the sequence aborted on blank fields; the only way it passed was to hand-write the artifact it exists to produce |
| `g16-scan` printed to stdout while prose asked for a manual `tee`, and the assembler required the file | the block writes and fsyncs its own artifact. Prose is not a producer |
| `shadow_verification.json` was unvalidated | exact fields, raw types, positive segment count and generation, non-empty WAL/anchor declarations, and `measurement_taken` exactly `False` |
| the preflight was checked for presence only | full 40-char lowercase hex `deploy_sha`, non-empty paths, non-negative raw-int capacity and inode counts, the exact label, a `run_id` matching the artifact directory, and `not_applicable` **only** where the profile permits it — with a reason |
| unit tests built `$ART` by hand | two end-to-end suites execute the **real blocks in the real order**, per profile, then assemble; missing, malformed, stale and empty producer outputs each prevent the report |

`$URL` also could not survive from `g4-probe` to `g4-sample`: separate blocks are separate
shells. The probe now writes a validated `g4_request.json` and the sampler reads it, so a
timing run cannot use a request the probe never checked.

Twelve mutations, serial from a git-clean tree: nine killed. Three are **true equivalents**,
verified by re-running the suite under each — a redundant guard catches the same input
(`g4-sample`'s missing-file check is backed by the reader failing; the empty-`findmnt` check by
the parser refusing a short record; the missing-input check by the field-completeness check).
One survivor was a real gap: the "neither derived nor excusable" branch is unreachable when an
input is missing, because the required-input check fires first — so it needed its own case, a
preflight field added with no derivation path, which must abort rather than be written blank.

## Review round 6 — evidence that proves which run it came from

| finding | correction |
|---|---|
| `$ART` could be reused, so an interrupted run's files satisfied the assembler exactly as fresh ones | `art-root` creates a **fresh** directory (`mkdir`, not `mkdir -p`) and writes `run.json`. Every producer stamps the `RUN_ID` — JSON records as a field, series as a `# run_id=…` header — and the assembler checks each against the marker and the directory name |
| `_check_nonempty()` accepted any one-line string as a series, so `lol` passed as thirty latency samples | real parsers: exact sample counts and finite non-negative seconds; numeric `ts npids fds rss` columns with a **stable** worker count; `root=… files=… dirs=… wall_s=…` with the scan root recorded. `_check_nonempty` survives only for the human-readable `df`/`findmnt` copies, which have no schema |
| `g4_request.json` was neither required nor validated | required for `legacy_baseline`, exact schema, run-bound, `span_days` and `probe_rows` pinned to 366, and its planned sample count **cross-checked against the series actually produced** |
| only the G4 probe validated its response | every sample is validated — JSON shape and exactly 366 rows — so a service degrading to a short, valid-looking 200 refuses the run instead of being timed. The sampler also re-checks the request's run binding before spending 30 requests against production |
| `json.load(open(...))` in `g4-sample` | context-managed, with a block-level guard asserting no fenced command opens a file outside a `with` |

Twelve mutations, serial from a git-clean tree: eleven killed. **Six survived the first pass and
five of those were real gaps** — each time a *different* check happened to catch the test's input,
leaving the guard under test unbound. Closing them needed inputs chosen so only the intended
check can fire: garbage with the right line count; a series shortened consistently in header and
body; three columns with a **stable** pid column (`1 2 3` / `4 5 6` trips worker-stability
instead); `capacity.json` and `wal_state.json` in the stale-artifact list, since neither has a
validator of its own; and the stale-request case tested at the sampler rather than only at the
assembler.

The one remaining survivor is a **true equivalent**: dropping the required-field check in the
metadata-scan parser leaves a `KeyError` on the following line, so the scan still fails closed —
by a worse message, not by a weaker guarantee.

## Review round 7 — no coercion, and every fact bound to the request behind it

| finding | correction |
|---|---|
| `int(float("2.5"))` is `2` — worker, file and directory counts were silently truncated into legal values | counts are parsed as **exact non-negative integer tokens**: digits only, no sign, decimal point, exponent, underscore, padding or boolean-like form. `wall_s` stays a float, because a duration legitimately is one — the rule is exactness where the value is a *count* |
| `g4_request.json` was checked for non-empty strings, so a record could keep `span_days: 366` while its dates, endpoint, coordinates or `append` said otherwise | canonical ISO dates, span arithmetic cross-checked against `span_days`, the endpoint pinned to the local GHRSST service, every required query parameter present, the URL's dates matching the record's, and `lon0`/`lat0`/`append` pinned **by value** — a different coordinate is a different measurement |
| the sampler would have spent thirty production requests on an edited record | it now calls **the same validator** the assembler uses, before the first request. One implementation, both sides |
| `pm2_jlist.json` was written and never read | validated for shape, the `ghrsst` app, `online` status and raw positive unique pids; and **cross-checked** against the H7 header's pid list and the sampled worker count, so a series describing a different set of processes is refused |

Fourteen mutations, serial from a git-clean tree: thirteen killed on the first pass. The
survivor was, again, a neighbouring check doing the work — every malformed date raised inside
`date.fromisoformat`, and the one compact form it accepts (`20260101`) was caught by the URL
cross-check. Binding the canonicity rule needed a date that parses, agrees with the URL, and is
still not canonical.

## Review round 8 — rows, roots and identity, each bound to its own evidence

| finding | correction |
|---|---|
| a G4 response was accepted if it was a list of length 366 — a list of 366 integers, or one repeating a single day, passed exactly as the real answer did | one shared `check_g4_response`, used by **both** the probe and every sample: each row an object with the required fields, canonical dates, and exactly the day set from `start` through `end` — no duplicates, no gaps, nothing outside the window |
| `check_g16_scan` required a non-empty `root`, so `root=/tmp/small-tree files=10` was a valid scan of the wrong tree | the capacity block records `data_root`, the preflight derives it, and the scan artifact's root must match it exactly |
| a 40-character SHA in the preflight proves the preflight holds a SHA | `identity-preflight` now writes `identity.json` — the verified commit, code root, interpreter, and the **path and sha256 of every module it imported** — and the assembler refuses a preflight naming a different (perfectly valid) SHA, a different code root, or a module resolved outside `$CODE_ROOT` |

Producer and validator share one schema: the runbook imports the validators rather than
restating the rules, so a check cannot pass in the document and fail in the code.

Twelve mutations, serial from a git-clean tree: nine killed, three **true equivalents** verified
by re-running the suite under each. All three are structural rather than accidental — at a fixed
length equal to the window, *duplicates*, *missing days* and *extra days* are the same condition
seen from three sides, so any one detects the others' inputs; and `identity.json` is loaded
before the required-artifact loop, so dropping it from the list still fails on the read. The
redundant checks are kept for their messages, not for a distinct guarantee.

Two stub corrections fell out, both the same lesson as earlier rounds: the `curl` stubs returned
`date: "d0"` placeholders, which were fine while only the row count was checked and become a
stub that cannot pass a real check the moment rows are validated.

## Review round 9 — the checkout, not just a commit; every module, not just one

| finding | correction |
|---|---|
| `git rev-parse --verify <sha>^{commit}` echoes **any existing commit** back, so comparing it to `DEPLOY_SHA` proved the commit exists — not that it is checked out. A valid ancestor passed, and so did a dirty tree | the identity block now requires `HEAD` to equal `DEPLOY_SHA` exactly **and** `git status --porcelain --untracked-files=all` to be empty, each failing closed with its own message. `identity.json` records `head_sha` and `worktree_clean`, and the assembler refuses a record that does not affirm both |
| `identity.json` fingerprinted only `store.block_manifest`, while the run imports and executes `ops.p5s8_report`, `ops.p5_roadmap`, `ops.quiescence`, `store.repair_wal` and `store.segmented_cube` — so an unrecorded module could change the **validator itself** without changing identity evidence | one per-profile inventory (`IDENTITY_MODULES` / `identity_modules()`), shared by producer and assembler. The identity block imports each module, records its resolved realpath and sha256, and assembly rehashes every one and refuses a record whose module set does not match the profile |

Eight mutations, serial from a git-clean tree: **all killed, no survivors** — including dropping
the HEAD check, dropping the dirty-tree gate, narrowing `--untracked-files`, and editing
`ops/p5s8_report.py` after identity was recorded.

Two fixture corrections were needed first, both consequences of the new gates rather than
defects in them: the test repositories gained a second commit (one commit cannot express "valid
ancestor that is not HEAD") and a `.gitignore` for byte-code caches, since importing from a
checked-out tree would otherwise dirty it and fail the clean-worktree gate for a reason having
nothing to do with the deployment.

## Round 10 — the endpoint the deployment actually serves

Staging `9557d4d` onto VM24 found the S8 contract pinned `http://127.0.0.1:8080/api/ghrsst`,
where nothing listens: `conf/start_app.sh` runs `gunicorn … -b 127.0.0.1:8035`. `g4-probe` could
not have run at all.

The pin behaved correctly — it refused rather than measuring the wrong target — but the value
was stale. Corrected to **`http://127.0.0.1:8035/api/ghrsst`**, and the URL is now built in one
place (`g4_url()`), which the probe imports and `check_g4_request` validates against. The probe
previously spelled its own URL out beside the pin, which is how the two came to disagree.

It stays pinned **by value**, deliberately: an arbitrary endpoint would need its own deployment
binding to mean anything, and this run targets the deployed service. A positive regression
asserts 8035 is accepted, a negative one asserts the stale 8080 is now refused, and a third
asserts no second endpoint survives anywhere in the contract.

## Round 11 — the service decides the window, not the clock

The first authorized VM24 execution (2026-08-20, `f6088b2`) **aborted at `g4-probe`**:
`end = date.today()` = 2026-08-20 while the service's `point_latest` was 2026-08-18, so the API
returned **364 rows** and the probe refused. The refusal was right; the window was wrong. How far
the point history reaches is a property of the deployment, not of the clock on whatever host runs
the runbook.

| change | |
|---|---|
| `HEALTHZ_URL` + `check_healthz()` | one shared read-only validator for the canonical service on 8035. Reads **`point_latest` explicitly** — a body carrying `latest`/`daily_latest` but not `point_latest` fails closed rather than falling back, because the daily staging window answers a different question |
| `g4_window(point_latest)` | `end = point_latest`, `start = end − 365`, span exactly 366 |
| probe | fetches `/healthz` first: HTTP 200, JSON object, canonical `point_latest`, strict `point_day_count` (and at least 366 point days, or the measurement cannot exist here) |
| `g4_request.json` | now records `healthz_url`, `point_latest` and `point_day_count` beside the window it produced |
| sampler | **re-reads `/healthz` live before the first timed sample** and refuses if `point_latest` has moved. An edited artifact cannot bypass it: the comparison is against the service, not against anything the file claims |
| assembler | refuses a request whose `end` is not the recorded `point_latest`, or whose window is not the 366-day span ending there |

The **366-day contract was not weakened** and the window was not shifted by hand — it is derived
from the authority that knows.

Nine mutations, serial from a git-clean tree: eight killed. One **true equivalent**, verified by
re-running both suites under it: `g4_window(x)` always returns `end == x`, so "end ≠ point_latest"
is subsumed by the window-equality check. It is kept for its clearer message, not for a distinct
guarantee.

The aborted run's artifacts are **retained as evidence** at
`/tmp/p5s8-art/p5s8_20260820T080028Z/` on VM24 (`run.json`, `identity.json`, `capacity.json`,
`df_h.txt`, `df_i.txt`, `findmnt.txt`, `wal_state.json`, `preflight.json`).

> **HISTORICAL — SUPERSEDED.** That aborted run read **264.4 GiB free**. It is *not* the current
> G18 evidence: the later successful run `20260820T110036Z` supersedes it, and the current
> figures live in §2 and in the generated blockers table below. The 264.4 figure is kept only as
> the record of what the 08:00 run measured before it refused at `g4-probe`.

## Round 12 — one service authority, and a health check that checks health

| finding | correction |
|---|---|
| `check_healthz()` read `point_latest` without ever looking at `status` — a body could carry a perfectly good window while the service reported itself `degraded` | `status` is required, must be raw `str`, and must equal exactly `"ok"`. Missing, wrongly-typed and non-`ok` are three distinct defects, each refused on its own and each asserted independently |
| `G4_ENDPOINT` and `HEALTHZ_URL` were spelled out separately — two authorities, and staging had already found one of them stale (8080 against a deployment serving 8035) while the other happened to be right | both derive from a single `SERVICE_BASE`. A regression rebinds the base and asserts **both** endpoints move; another asserts the host:port literal appears exactly once in the module |
| healthz used `isinstance`, against the repository rule that types are checked **before** any conversion (`block_manifest._exact`) | exact `type(x) is dict / str / int`, with subclass controls for each and a `bool` control for the count — `True` is an `int` subclass and would otherwise satisfy every numeric check |

`check_healthz()` was split so `check_g4_request` re-validates a recorded window through
`check_window_fields()` rather than **synthesising a `status`** the record never carried — a
fabricated field is the same laundering this module refuses everywhere else.

Eight mutations, serial from a git-clean tree: **all killed, no survivors.** One needed a
sharper control first — the body-type check was unbound, because `[]`, `"ok"`, `None` and `7`
are rejected by `isinstance` too; only a genuine `dict` subclass separates the exact-type rule
from the loose one.

Unchanged and still asserted: `point_latest` (not `latest`/`daily_latest`) determines the
366-day window; the sampler re-reads live `/healthz` before any timed sample; an edited request
artifact cannot bypass that check; and the 2026-08-20 / 364-row request remains a refusal.

## 4. Remaining blockers

<!-- generated:blockers:start -->
<!-- Generated by ops/p5s8_evidence.py from run 20260820T110036Z. Do not edit by hand. -->

| blocker | state |
|---|---|
| **G18 capacity** | **BLOCKED** — **295.7 GiB free** (measured 2026-08-20, run `20260820T110036Z`), 403.3 GiB minimum, 600 GiB recommended; shortfall ≈ 107.6 GiB |
| **G3 / G4** | `NOT_ADJUDICATED` — production thresholds; the S8 G4 reading is a `legacy_baseline` baseline that adjudicates nothing |
| **G7** | `PARTIAL` — the gate's arithmetic is proven; a real fold's temp disk is not measured |
| **H7** | inode headroom has evidence; **sustained load `NOT_ADJUDICATED`**, and the collected fd/RSS is `master_only` |
| **G16** | metadata scan has evidence; **snapshot-open `NOT_ADJUDICATED`** — no segmented manifest is deployed |
| **S = 90 / C = 30** | **provisional only**; nothing has selected it |
| **S8 execution** | `legacy_baseline` **COMPLETED** — executed 2026-08-20 at deploy SHA 96ee79c; artifacts assembled on VM24 and copied back; NO gate adjudicated by this run |
| **S9 / shadow execution** | **NOT_STARTED** — requires its own separate human approval |
| **S10** | not authorized; needs its own fresh written approval |

<!-- generated:blockers:end -->

## 5. The exact next approval needed

**IT capacity expansion (option A) to at least 403.3 GiB free — 600 GiB recommended — or an
approved staging-volume design meeting all six option-B obligations.**

That unblocks critical-path step 3. It is **not** implied by the P5-EX-1 exception, and it is
separate from the fresh VM24 read-only / shadow approval that step 4 needs before any S8 or S9
command runs against the host.
