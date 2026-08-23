"""dev2026 — may a first P5 compaction run against THIS deployment?

Everything built so far answers a different question. `ops/p5s8_evidence.py` measured the
legacy monolith on VM24; the P5 store layer is proven against `tempfile` trees. Neither says
whether the target has a manifest, a WAL, an anchor or a lock — and VM24, today, has none of
them: it runs the phase-2 tiered deployment, with no `p5_blocks`, no `p5_repairs`, no anchor
and no compaction lock.

So this module adjudicates a deployment, not a document. Every checker looks at the target that
was handed to it and reports what it found.

## Why the verdicts are shaped this way

`READY` / `BLOCKED` / `NOT_ADJUDICATED` / `NEEDS_HUMAN_APPROVAL` — and deliberately no `PASS`.
The failure this phase kept correcting is a measurement of one thing quoted as a verdict on
another: an S8 legacy baseline read as a P5 result, a synthetic fixture read as production
evidence, a figure from two runs ago read as current. `NON_QUALIFYING_LABELS` closes that at the
top: evidence carrying one of those labels cannot produce `READY` for any requirement, whatever
the checker found, and the reason names the label that stopped it.

`NEEDS_HUMAN_APPROVAL` is neither a pass nor a failure. It marks the things no amount of checking
can settle — switching the live service being the obvious one. It still blocks readiness.

## What this module is NOT

It builds nothing, changes nothing and asks for nothing. It re-implements no manifest, WAL or
provenance logic either: every structural check calls the shipped module the store itself calls,
so a manifest this accepts is one `SegmentedCubeStore` would accept.
"""
from __future__ import annotations

import os
from typing import Dict, List

from store import block_manifest, repair_wal

READY = "READY"
BLOCKED = "BLOCKED"
NOT_ADJUDICATED = "NOT_ADJUDICATED"
NEEDS_APPROVAL = "NEEDS_HUMAN_APPROVAL"

VERDICTS = (READY, BLOCKED, NOT_ADJUDICATED, NEEDS_APPROVAL)

#: Evidence labels that may never produce READY for a P5 compaction requirement. Each names a
#: real thing that was measured -- and a different thing from the one being adjudicated here.
NON_QUALIFYING_LABELS = (
    "LOCAL_SYNTHETIC",              # a tempfile tree is not a deployment
    "MODEL",                        # arithmetic is not a measurement
    "PRODUCTION_GEOMETRY_FIXTURE",  # the right shape, still a fixture
    "LEGACY_BASELINE",              # the monolith S8 measured has no P5 roots at all
    "DEFERRED",                     # explicitly not measured
    #: An isolated staging root built by `ops/p5_provision.py`. Structurally complete, and a
    #: deliberate non-deployment: proving the provisioning recipe works is a different claim
    #: from a deployment being ready, and the label is what keeps the two apart.
    "P5_ISOLATED_STAGING",
)

#: The label a target must carry for its checkers to be believed: evidence read from the real
#: deployment, read-only.
QUALIFYING_LABEL = "VM24_PRODUCTION_READ_ONLY"

#: Commits that have completed review and may be deployed. Declared HERE, where a reviewed
#: change is needed to alter it -- never taken from the target.
#:
#: `reviewed_sha == head_sha` compared the target against itself: both fields come from one
#: collected JSON, so anything that could write that file could declare its own commit
#: reviewed. An approved set is only worth anything if the thing being approved cannot edit it.
REVIEWED_DEPLOY_SHAS = (
    "3223fba37da5d292ea6acb58ccef0da2b5138d31",
)

#: Modules whose bytes are fingerprinted and re-verified against the approved commit.
INVENTORY_MODULES = (
    "store/block_manifest.py", "store/repair_wal.py", "store/compaction_lock.py",
    "ingest/build_block.py", "ingest/publish_manifest.py", "api/app.py",
)

#: How long a free-space reading may be believed. Free space is true of a moment: an ingest
#: window, a restart or an overnight pause can all move it, and none of them announce
#: themselves in the artifact. Short enough that a schedule tick invalidates the reading.
MAX_CAPACITY_AGE_S = 2 * 3600

#: How far a capacity reading may sit from the collection that carries it. They are supposed to
#: be the same act; a large gap means one of them was reused.
MAX_CAPACITY_SKEW_S = 300

#: Clocks are not synchronised to the second. A reading a little ahead of the assessor is
#: skew; one materially ahead cannot be aged at all, and an unaged reading never expires.
MAX_CLOCK_LEAD_S = 60

#: How far a lock acquisition probe may sit from the collection that carries it. Same reason as
#: the capacity skew: a probe from another sitting is evidence about another moment.
MAX_PROBE_SKEW_S = 900

_SHA256 = None


def _is_sha256(value) -> bool:
    """Exactly 64 lowercase hex characters. Shape is not identity, but the wrong shape is not
    a digest at all, and 'non-empty' was the only test it previously had to pass."""
    global _SHA256
    if _SHA256 is None:
        import re
        _SHA256 = re.compile(r"^[0-9a-f]{64}$")
    return isinstance(value, str) and bool(_SHA256.match(value))

REQUIREMENTS: List[Dict] = [
    {"id": "identity", "evidence_kind": "preflight",
     "title": "the reviewed commit, checked out clean and detached",
     "why": ("a compaction is run BY code. If the deployed tree is not exactly the reviewed "
             "commit, the review adjudicated something else.")},
    {"id": "manifest", "evidence_kind": "store",
     "title": "a live segmented manifest the store would accept",
     "why": ("the manifest is the authority for what is served. Without one there is no "
             "segmented base to compact into, and `SegmentedCubeStore` has nothing to open.")},
    {"id": "blocks", "evidence_kind": "store",
     "title": "every referenced block present and passing the strict contract",
     "why": ("a manifest referencing a block that is absent or malformed publishes a view "
             "nobody can read; the failure surfaces at request time, on the live service.")},
    {"id": "wal_anchor", "evidence_kind": "store",
     "title": ("an initialized P5 WAL, an anchor outside the domain, and a declared "
               "rollback domain"),
     "why": ("the WAL is how a half-finished compaction is recognised as half-finished. An "
             "anchor inside the domain it witnesses is lost with it, and an undeclared domain "
             "means no one has said what a rollback would restore.")},
    {"id": "locks", "evidence_kind": "store",
     "title": "canonical compaction and ingest lock paths, in the declared order",
     "why": ("compaction and ingest both mutate the served view. Two writers with no shared "
             "lock path are not excluded from each other at all.")},
    {"id": "api_parity", "evidence_kind": "service",
     "title": "the API serves the segmented base with values matching the current base",
     "why": ("routing and value parity can only be shown by a service actually serving the "
             "manifest -- which means switching the live deployment, or standing up an "
             "isolated second one.")},
    {"id": "capacity", "evidence_kind": "measurement",
     "title": "free space above the minimum, from a fresh measurement",
     "why": ("a block build needs the space before it starts. A figure with no run behind it "
             "is a number in a file.")},
    {"id": "failure_semantics", "evidence_kind": "proven_locally",
     "title": "fail-closed refusal, rollback and partial-staging behaviour",
     "why": ("the first compaction is the one most likely to be interrupted. What happens then "
             "has to be a property of the code, not a hope.")},
]

#: Requirements no inspection can settle, with the reason. They are reported as needing a
#: person rather than quietly failing -- and they still block readiness.
APPROVAL_REQUIRED = {
    "api_parity": ("proving this needs a service serving the manifest. On VM24 that is either a "
                   "live switchover or a second isolated service -- both are production changes "
                   "and neither is authorized by a read-only preflight."),
}


def _reason(**kw) -> str:
    return "; ".join(f"{k}={v}" for k, v in kw.items())


def expected_module_digests(sha: str) -> Dict[str, str]:
    """sha256 of each inventory module AT `sha`, read from git by the assessor.

    The target reports what it found on the host. This is what the approved commit actually
    contains. Comparing the two is the only version of the check that a target cannot satisfy
    by describing itself."""
    import hashlib
    import subprocess
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    prefix = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out = {}
    for rel in INVENTORY_MODULES:
        proc = subprocess.run(["git", "show", f"{sha}:{prefix}/{rel}"], cwd=repo,
                              capture_output=True)
        if proc.returncode == 0:
            out[rel] = hashlib.sha256(proc.stdout).hexdigest()
    return out


def _git_blob(sha: str, rel: str):
    """The bytes of one repository path at one commit, or None if it is not there."""
    import subprocess
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repo, prefix = os.path.dirname(here), os.path.basename(here)
    proc = subprocess.run(["git", "show", f"{sha}:{prefix}/{rel}"], cwd=repo,
                          capture_output=True)
    return proc.stdout if proc.returncode == 0 else None


def expected_suite_digest(suite: str, sha: str):
    """sha256 of a test module AT `sha`, recomputed by the assessor.

    A digest that matches nothing certifies nothing. This is what the approved commit actually
    contains, so a claim can be checked against the suite it names rather than believed."""
    import hashlib
    blob = _git_blob(sha, suite.replace(".", "/") + ".py")
    return hashlib.sha256(blob).hexdigest() if blob is not None else None


def _check_identity(t: Dict) -> tuple:
    ident = t.get("identity") or {}
    reviewed, head = ident.get("reviewed_sha"), ident.get("head_sha")
    if not reviewed or not head:
        return NOT_ADJUDICATED, "no identity preflight was collected from the target"
    if reviewed not in REVIEWED_DEPLOY_SHAS:
        return BLOCKED, (f"{reviewed!r} is not in the approved deploy set; a target cannot "
                         f"declare its own commit reviewed")
    if reviewed != head:
        return BLOCKED, f"HEAD {head!r} is not the reviewed commit {reviewed!r}"
    if ident.get("worktree_clean") is not True:
        return BLOCKED, "the checkout is not clean; the deployed tree is not the reviewed tree"
    reported = ident.get("modules")
    if not reported:
        return NOT_ADJUDICATED, ("no module inventory was fingerprinted, so a matching SHA "
                                 "proves only what git says, not what is on disk")
    expected = expected_module_digests(reviewed)
    if not expected:
        return NOT_ADJUDICATED, (f"could not recompute the module fingerprints for {reviewed} "
                                 f"here, so the reported ones cannot be checked against "
                                 f"anything")
    missing = sorted(set(expected) - set(reported))
    if missing:
        return BLOCKED, (f"the inventory does not cover {missing}; an absent module reads as "
                         f"clean and is the easiest thing to leave out")
    wrong = sorted(k for k, v in expected.items() if reported.get(k) != v)
    if wrong:
        return BLOCKED, (f"module fingerprint mismatch against {reviewed[:7]}: {wrong}. The "
                         f"deployed bytes are not the approved bytes")
    return READY, (f"clean checkout at {head[:7]}, {len(expected)} module fingerprints "
                   f"recomputed from the approved commit and matched")


def _live_manifest(manifest_root: str):
    """The live manifest, via the shipped reader. Returns (manifest, None) or (None, reason)."""
    if not manifest_root or not os.path.isdir(manifest_root):
        return None, f"no manifest root at {manifest_root!r}"
    live = os.path.join(manifest_root, block_manifest.LIVE_NAME)
    if not os.path.isfile(live):
        return None, f"no live manifest at {live}"
    import json
    try:
        with open(live) as fh:
            raw = json.load(fh)
    except Exception as exc:                                  # noqa: BLE001
        return None, f"the live manifest is unreadable: {exc!r}"
    try:
        return block_manifest.validate_manifest(raw), None
    except Exception as exc:                                  # noqa: BLE001
        return None, f"the live manifest is invalid: {exc!r}"


def _check_manifest(t: Dict) -> tuple:
    manifest, why = _live_manifest(t.get("manifest_root"))
    if manifest is None:
        return BLOCKED, why
    return READY, _reason(generation=manifest["generation"],
                          segments=len(manifest["segments"]),
                          id=manifest["generation_id"])


def _check_blocks(t: Dict) -> tuple:
    manifest, why = _live_manifest(t.get("manifest_root"))
    if manifest is None:
        return BLOCKED, f"cannot check blocks: {why}"
    root = t["manifest_root"]
    checked = []
    for seg in manifest["segments"]:
        path = seg["path"]
        if not os.path.isabs(path):
            path = os.path.join(root, path)
        if not os.path.exists(path):
            return BLOCKED, f"segment {seg['segment_id']!r} references a missing store: {path}"
        try:
            # The strict contract the store itself applies -- not a lighter copy of it.
            block_manifest.inspect_store_contract(path)
        except Exception as exc:                              # noqa: BLE001
            return BLOCKED, f"segment {seg['segment_id']!r} fails the store contract: {exc!r}"
        if not seg.get("build_provenance"):
            return BLOCKED, (f"segment {seg['segment_id']!r} carries no build_provenance, so "
                             f"nothing records which sources produced its bytes")
        checked.append(seg["segment_id"])
    return READY, _reason(segments_verified=len(checked), ids=",".join(checked))


def ignorable_days(state) -> set:
    """Days poisoned SOLELY by the log's own initialization record.

    `repair_wal._replay` classifies any non-INTENT record with no matching intent as a terminal
    orphan and poisons its `day`. The log's own `wal_initialized` record carries `day == ""`, so
    every freshly initialized WAL reports `poisoned_days == {""}` while being entirely healthy.

    The first version of this dropped every empty-string day, which is broader than the wart it
    works around: "no real day is empty today" is an observation about current data, not a
    property of the format, and a filter justified by an observation outlives it. So the days
    forgiven are computed from the records -- a day is ignorable only if every record carrying
    it is a `wal_initialized`.

    Reported upstream rather than fixed at the source: correcting the replay changes signed-off
    P5-S5 WAL semantics, which is not this phase's call to make."""
    from collections import defaultdict
    kinds = defaultdict(set)
    for rec in getattr(state, "records", ()) or ():
        kinds[rec.get("day")].add(rec.get("record"))
    return {day for day, seen in kinds.items() if seen == {"wal_initialized"}}


def _real_days(days, state) -> set:
    """The poisoned/blocked days that actually name something, given this log's records."""
    return set(days) - ignorable_days(state)


def _check_wal_anchor(t: Dict) -> tuple:
    wal_root, anchor_root = t.get("wal_root"), t.get("anchor_root")
    if not wal_root or not os.path.isdir(wal_root):
        return BLOCKED, f"no P5 WAL root at {wal_root!r}"
    if not anchor_root or not os.path.isdir(anchor_root):
        return BLOCKED, f"no anchor root at {anchor_root!r}"
    # An explicit `anchor_domain_id(...) is None` check stood here and was removed: mutation
    # testing showed it could be deleted with every test still passing, because
    # `resolve_anchor_root` already refuses an undeclared domain. Two copies of one rule is the
    # second truth this module exists to avoid -- and the copy is the one that goes stale.
    try:
        resolved = repair_wal.resolve_anchor_root(wal_root, anchor_root)
    except Exception as exc:                                  # noqa: BLE001
        return BLOCKED, (f"the anchor root is refused by the shipped resolver, which is the "
                         f"authority on rollback domains: {exc}")
    try:
        state = repair_wal.read_wal(wal_root, anchor_root=resolved)
    except Exception as exc:                                  # noqa: BLE001
        return BLOCKED, f"the WAL is not readable: {exc!r}"
    # `authority` is None until `wal_initialized` binds the log to a manifest. An empty
    # directory parses cleanly and reports nothing wrong -- reading that as a healthy WAL is
    # how "no WAL at all" would have been adjudicated READY.
    if state.authority is None:
        return BLOCKED, ("the WAL has never been initialized: no wal_initialized record binds "
                         "it to a manifest authority")
    bound = state.authority.get("authority") or state.authority
    blocked = _real_days(state.blocked_days, state)
    if blocked:
        return BLOCKED, f"{len(blocked)} day(s) have an OPEN repair intent: {sorted(blocked)}"
    poisoned = _real_days(state.poisoned_days, state)
    if poisoned:
        return BLOCKED, (f"{len(poisoned)} day(s) carry untrustworthy repair evidence: "
                         f"{sorted(poisoned)}")
    try:
        state.assert_fresh()
    except Exception as exc:                                  # noqa: BLE001
        return BLOCKED, f"the WAL is not fresh against its anchor: {exc!r}"
    return READY, _reason(anchor=resolved, domain=repair_wal.anchor_domain_id(resolved),
                          authority=repair_wal.authority_digest(bound)
                          if isinstance(bound, dict) else "recorded",
                          open_repairs=0)


def _check_locks(t: Dict) -> tuple:      # noqa: C901 -- one binding per branch, by design
    locks = t.get("locks") or {}
    missing = [k for k in ("compaction", "ingest") if not locks.get(k)]
    if missing:
        return BLOCKED, f"no canonical path declared for lock(s): {missing}"
    # Distinct STRINGS are not distinct locks. A symlink or a bind mount makes two paths one
    # file, and two writers each holding "their" lock are excluded from nothing.
    if os.path.realpath(locks["compaction"]) == os.path.realpath(locks["ingest"]):
        return BLOCKED, ("the compaction and ingest lock paths resolve to the same file; one "
                         "lock cannot express the ordering between two writers")
    for kind in ("compaction", "ingest"):
        parent = os.path.dirname(os.path.abspath(locks[kind])) or "."
        if not os.path.isdir(parent):
            return BLOCKED, f"the {kind} lock directory does not exist: {parent}"
        if not os.access(parent, os.W_OK | os.X_OK):
            return BLOCKED, (f"the {kind} lock directory is not writable: {parent}. A lock "
                             f"that cannot be created is a lock that is never held")
    # "The directory exists" is not evidence that a lock can be taken in it, and
    # `p5_compaction.lock` does not exist on the target at all. Only a controlled acquisition
    # says whether the fencing this whole posture rests on actually works.
    probe = locks.get("acquire_probe")
    if not probe:
        return NOT_ADJUDICATED, (f"no lock acquisition probe was recorded. The paths are "
                                 f"declared and distinct, but nothing has taken "
                                 f"{locks['compaction']!r} and released it, so acquirability "
                                 f"is assumed rather than shown")
    # An unbound probe claims some lock somewhere was taken once. The declared compaction lock
    # is the one the whole posture fences with, and a probe from another path, host, run or
    # sitting is evidence about a different event. Each binding is checked separately so no
    # missing field is covered by another field being present.
    for field in ("path", "run_id", "host", "at_utc", "actor"):
        if not probe.get(field):
            return (NOT_ADJUDICATED if field == "actor" else BLOCKED), (
                f"the lock probe declares no {field}, so it cannot be attributed to this "
                f"lock, this host and this collection")
    if os.path.realpath(probe["path"]) != os.path.realpath(locks["compaction"]):
        return BLOCKED, (f"the probe took {probe['path']!r}, which is a different file from "
                         f"the declared compaction lock {locks['compaction']!r}")
    if probe["run_id"] != t.get("run_id"):
        return BLOCKED, (f"the probe is from run {probe['run_id']!r}, not the collection's "
                         f"{t.get('run_id')!r}")
    if str(probe["host"]).split(".")[0] != str(t.get("host") or "").split(".")[0]:
        return BLOCKED, (f"the probe was taken on host {probe['host']!r}, not the target's "
                         f"{t.get('host')!r}")
    probed_at, collected = _parse_utc(probe["at_utc"]), _parse_utc(t.get("collected_utc"))
    if probed_at is None or collected is None:
        return NOT_ADJUDICATED, ("the probe or the collection carries no usable timestamp, so "
                                 "the probe cannot be placed in this collection")
    drift = abs((probed_at - collected).total_seconds())
    if drift > MAX_PROBE_SKEW_S:
        return BLOCKED, (f"the probe was taken {int(drift)}s from the collection that reports "
                         f"it; that is a different sitting, and lock state does not carry")
    if probe.get("acquired") is not True or probe.get("released") is not True:
        return BLOCKED, (f"the lock acquisition probe did not complete: {probe}. A lock that "
                         f"cannot be taken, or cannot be let go, fences nothing")
    return READY, _reason(compaction=locks["compaction"], ingest=locks["ingest"],
                          probe=f"acquired and released by {probe['actor']}",
                          order="compaction acquired before ingest")


def _parse_utc(text):
    """An ISO timestamp, or None. `Z` is not a timezone Python parses without help."""
    from datetime import datetime
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def capacity_expires_utc(t: Dict):
    """When this target's capacity reading stops being believable."""
    from datetime import timedelta
    measured = _parse_utc((t.get("capacity") or {}).get("measured_utc"))
    if measured is None:
        return None
    return (measured + timedelta(seconds=MAX_CAPACITY_AGE_S)).isoformat()


def _check_capacity(t: Dict, now=None) -> tuple:
    cap = t.get("capacity") or {}
    if not cap:
        return NOT_ADJUDICATED, "no capacity evidence was supplied"
    if not cap.get("run_id") or not cap.get("measured_utc"):
        return NOT_ADJUDICATED, ("the capacity figure carries no run_id/measured_utc, so it is "
                                 "a number in a file rather than a measurement")
    # Bound to the collection that carries it -- and the collection has to say who it is.
    # `if target_run and ...` skipped the binding when the field was absent, which made
    # omitting it safer than getting it wrong. An absent field must never be the easy path.
    target_run = t.get("run_id")
    if not target_run:
        return NOT_ADJUDICATED, ("the target declares no run_id, so the capacity reading cannot "
                                 "be bound to the collection that carries it")
    if cap["run_id"] != target_run:
        return NOT_ADJUDICATED, (f"the capacity reading is from run {cap['run_id']!r} while the "
                                 f"collection is run {target_run!r}: different moments")
    measured = _parse_utc(cap["measured_utc"])
    if measured is None:
        return NOT_ADJUDICATED, f"measured_utc {cap['measured_utc']!r} is not a timestamp"
    collected = _parse_utc(t.get("collected_utc"))
    if collected is None:
        return NOT_ADJUDICATED, ("the target declares no usable collected_utc, so the reading "
                                 "cannot be placed against the collection")
    skew = abs((measured - collected).total_seconds())
    if skew > MAX_CAPACITY_SKEW_S:
        return NOT_ADJUDICATED, (f"the capacity reading sits {int(skew)}s from the collection "
                                 f"that carries it; they are meant to be one act, so one of "
                                 f"them was reused")
    # Free space is true of a moment. An ingest window, a restart or an overnight pause moves
    # it, and none of them announce themselves in the artifact.
    if now is None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
    elif isinstance(now, str):
        now = _parse_utc(now)
    age = (now - measured).total_seconds()
    # A reading ahead of the clock is a clock problem or a fabricated one. Either way it cannot
    # be aged, and an unaged reading never expires -- so it would be believed forever.
    if age < -MAX_CLOCK_LEAD_S:
        return NOT_ADJUDICATED, (f"the capacity reading is {int(-age)}s in the future against "
                                 f"the assessing clock; a reading that cannot be aged cannot "
                                 f"expire")
    if age > MAX_CAPACITY_AGE_S:
        return NOT_ADJUDICATED, (f"the capacity reading has expired: measured "
                                 f"{cap['measured_utc']}, {int(age)}s ago, against a "
                                 f"{MAX_CAPACITY_AGE_S}s window. Re-collect before relying "
                                 f"on it")
    free, minimum = cap.get("free_now_gib"), cap.get("minimum_free_gib")
    if free is None or minimum is None:
        return NOT_ADJUDICATED, "the capacity evidence is missing free_now_gib/minimum_free_gib"
    if free < minimum:
        return BLOCKED, (f"free {free} GiB is below the {minimum} GiB minimum "
                         f"(run {cap['run_id']})")
    recommended = cap.get("recommended_free_gib")
    note = "" if recommended is None or free >= recommended else "; below the recommended target"
    return READY, f"free {free} GiB against a {minimum} GiB minimum (run {cap['run_id']}){note}"


def _check_failure_semantics(t: Dict) -> tuple:
    proven = t.get("failure_semantics_proven_by")
    if not proven:
        return NOT_ADJUDICATED, ("no suite was named as proving the refusal, rollback and "
                                 "partial-staging behaviour for this target")
    # A sentence is not evidence. Naming a suite says nothing about WHICH code it ran against
    # or whether that run happened, so the claim has to carry the commit and a digest of the
    # run that produced it.
    if not isinstance(proven, dict):
        return NOT_ADJUDICATED, (f"the claim {proven!r} is a sentence, not a bound result: it "
                                 f"names no commit and no run digest")
    sha, digest = proven.get("deploy_sha"), proven.get("digest")
    suite = proven.get("suite")
    if not sha or not digest or not suite:
        missing = [k for k in ("suite", "deploy_sha", "digest") if not proven.get(k)]
        return NOT_ADJUDICATED, (f"the claim is missing {missing}; without all three it cannot "
                                 f"be tied to a run of known code")
    if not _is_sha256(digest):
        return NOT_ADJUDICATED, (f"digest {digest!r} is not a SHA-256: 64 lowercase hex "
                                 f"characters. Any non-empty value used to pass here")
    if sha not in REVIEWED_DEPLOY_SHAS:
        return BLOCKED, (f"the behaviour was proven against {sha[:7]}, which is not in the "
                         f"approved deploy set")
    # The digest has to be OF something. Recomputed from the suite at the approved commit, so
    # a well-formed digest of nothing in particular is refused like any other mismatch.
    expected = expected_suite_digest(suite, sha)
    if expected is None:
        return BLOCKED, (f"{suite!r} is not present at {sha[:7]}, so no run of it could have "
                         f"proven anything about that commit")
    if digest != expected:
        return BLOCKED, (f"the digest does not match {suite} at {sha[:7]}: the claim is about "
                         f"a different file than the one it names")
    return READY, _reason(suite=suite, deploy_sha=sha[:7], digest=digest[:12])


#: Requirements whose verdict comes from stat-ing paths. They mean nothing off the host that
#: owns those paths -- see `_on_host`.
FILESYSTEM_BACKED = ("manifest", "blocks", "wal_anchor", "locks")


def _on_host(target: Dict) -> tuple:
    """Is this process running on the host the target describes?

    The first real assessment reported `locks` BLOCKED because "the compaction lock directory
    does not exist". It does exist -- on VM24. The check ran on the auditor's laptop and
    reported on the wrong machine, and every other filesystem verdict in that run was right
    only by coincidence: the P5 roots happened to be absent in both places.

    So filesystem-backed requirements are adjudicated only where their paths live."""
    import socket
    declared = target.get("host")
    if not declared:
        return False, "the target names no host, so its paths cannot be attributed to one"
    here = socket.gethostname().split(".")[0]
    if declared.split(".")[0] != here:
        return False, (f"collected on host {declared!r}, adjudicated on {here!r}: stat-ing "
                       f"these paths here would report a different machine")
    return True, ""


CHECKERS = {
    "identity": _check_identity,
    "manifest": _check_manifest,
    "blocks": _check_blocks,
    "wal_anchor": _check_wal_anchor,
    "locks": _check_locks,
    "capacity": _check_capacity,
    "failure_semantics": _check_failure_semantics,
}


def assess(target: Dict, now=None) -> Dict:
    """Adjudicate one deployment target and return a machine-readable report.

    `now` is injectable so freshness is testable, and defaults to the wall clock."""
    label = target.get("label") or "UNLABELLED"
    disqualified = label in NON_QUALIFYING_LABELS or label != QUALIFYING_LABEL
    on_host, host_reason = _on_host(target)

    results = []
    for req in REQUIREMENTS:
        rid = req["id"]
        if rid in APPROVAL_REQUIRED:
            verdict, reason = NEEDS_APPROVAL, APPROVAL_REQUIRED[rid]
        elif rid in FILESYSTEM_BACKED and not on_host:
            verdict, reason = NOT_ADJUDICATED, host_reason
        elif rid == "capacity":
            verdict, reason = _check_capacity(target, now=now)
        else:
            verdict, reason = CHECKERS[rid](target)
        if verdict == READY and disqualified:
            # The checker found the thing. It found it in evidence that is not of this
            # deployment, which is a different statement -- and the one that keeps being
            # collapsed. Report what stopped it, not merely that it stopped.
            verdict = NOT_ADJUDICATED
            reason = (f"evidence carries label {label!r}, which cannot settle a P5 compaction "
                      f"requirement; the checker's finding was: {reason}")
        results.append({"id": rid, "title": req["title"], "why": req["why"],
                        "evidence_kind": req["evidence_kind"],
                        "verdict": verdict, "reason": reason})

    summary = {v: [r["id"] for r in results if r["verdict"] == v] for v in VERDICTS}
    ready = all(r["verdict"] == READY for r in results)
    outstanding = summary[BLOCKED] + summary[NOT_ADJUDICATED] + summary[NEEDS_APPROVAL]
    return {
        "schema": "ghrsst.p5.readiness/1",
        "label": label,
        "adjudicated_on_host": on_host,
        "adjudicated_on_host_note": host_reason or "paths were stat-ed on the host that owns them",
        "target": {k: target.get(k) for k in
                   ("root", "manifest_root", "wal_root", "anchor_root", "locks")},
        "requirements": results,
        "summary": summary,
        "compaction_ready": ready,
        #: Stated, not implied: this report describes one collection. Past this timestamp the
        #: capacity reading it rests on must be taken again -- a schedule tick, a restart or an
        #: overnight pause all move free space without announcing it here.
        "capacity_expires_utc": capacity_expires_utc(target),
        "max_capacity_age_s": MAX_CAPACITY_AGE_S,
        "re_collect_before_acting": True,
        "before_first_compaction": outstanding,
        "next_safe_step": _next_step(summary),
    }


def _next_step(summary: Dict) -> str:
    """The smallest reversible thing to do next, chosen from what is outstanding."""
    if summary[BLOCKED]:
        return (f"resolve the blocked requirement(s) {summary[BLOCKED]} — provisioning an "
                f"isolated P5 staging root is the reversible way to do it, and touches no "
                f"live path")
    if summary[NOT_ADJUDICATED]:
        return (f"collect qualifying evidence for {summary[NOT_ADJUDICATED]} from the real "
                f"deployment, read-only")
    if summary[NEEDS_APPROVAL]:
        return (f"obtain explicit human approval for {summary[NEEDS_APPROVAL]}; nothing in a "
                f"read-only preflight confers it")
    return "every requirement is READY; a first compaction may be scheduled under its own approval"


#: Where an assessed run's collected target and generated report live.
RUNS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "bench", "results", "p5_readiness_runs")
ARTIFACT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "bench", "results", "p5_readiness.json")


def load_target(path: str) -> Dict:
    """A target collected on the host, read back verbatim. Nothing here edits a reading."""
    import json
    with open(path) as fh:
        return json.load(fh)


def main(argv=None) -> int:
    """Assess a collected target and write the machine-readable readiness artifact."""
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="path to a collected target JSON")
    ap.add_argument("--out", default=ARTIFACT)
    args = ap.parse_args(argv)

    target = load_target(args.target)
    report = assess(target)
    # Provenance travels with the verdict: a readiness report that does not say which
    # collection it adjudicated is the same kind of unanchored claim as a bare free-space
    # figure, and this phase has spent long enough on those.
    report["provenance"] = {k: target.get(k) for k in ("run_id", "host", "collected_utc")}
    report["provenance"]["target_artifact"] = os.path.relpath(
        args.target, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    report["observed"] = {
        "service": target.get("service"),
        "p5_roots_present": target.get("p5_roots_present"),
        "staging": target.get("staging"),
        "identity": {k: v for k, v in (target.get("identity") or {}).items()
                     if k != "modules"},
        "capacity": target.get("capacity"),
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    print(f"wrote {args.out}")
    for v in VERDICTS:
        ids = report["summary"][v]
        print(f"  {v:<22} {len(ids):>2}  {', '.join(ids) if ids else '-'}")
    print(f"  compaction_ready: {report['compaction_ready']}")
    print(f"  next: {report['next_safe_step']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
