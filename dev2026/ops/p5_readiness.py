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


def _check_identity(t: Dict) -> tuple:
    ident = t.get("identity") or {}
    reviewed, head = ident.get("reviewed_sha"), ident.get("head_sha")
    if not reviewed or not head:
        return NOT_ADJUDICATED, "no identity preflight was collected from the target"
    if reviewed != head:
        return BLOCKED, f"HEAD {head!r} is not the reviewed commit {reviewed!r}"
    if ident.get("worktree_clean") is not True:
        return BLOCKED, "the checkout is not clean; the deployed tree is not the reviewed tree"
    if not ident.get("modules"):
        return NOT_ADJUDICATED, ("no module inventory was fingerprinted, so a matching SHA "
                                 "proves only what git says, not what is on disk")
    return READY, f"clean checkout at {head}, {len(ident['modules'])} modules fingerprinted"


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


def _real_days(days) -> set:
    """Day sets from the WAL, minus the empty placeholder.

    `repair_wal._replay` classifies any non-INTENT record with no matching intent as a terminal
    orphan and poisons its `day`. The log's own `wal_initialized` record carries `day == ""`,
    so EVERY freshly initialized WAL reports `poisoned_days == {""}`. It harms nothing there --
    no real day is named, and the guard at the prune site is keyed by real day -- but a
    health check reading "poisoned_days is non-empty" as unhealthy calls every correct WAL
    broken. Reported upstream rather than fixed here: changing the replay would change
    signed-off P5-S5 WAL semantics, which is not this phase's call to make.

    `test_the_initialization_record_poisons_the_empty_day` pins the behaviour, so if the
    replay is ever corrected this filter is flagged as no longer needed."""
    return {d for d in days if d}


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
    blocked = _real_days(state.blocked_days)
    if blocked:
        return BLOCKED, f"{len(blocked)} day(s) have an OPEN repair intent: {sorted(blocked)}"
    poisoned = _real_days(state.poisoned_days)
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


def _check_locks(t: Dict) -> tuple:
    locks = t.get("locks") or {}
    missing = [k for k in ("compaction", "ingest") if not locks.get(k)]
    if missing:
        return BLOCKED, f"no canonical path declared for lock(s): {missing}"
    same = os.path.abspath(locks["compaction"]) == os.path.abspath(locks["ingest"])
    if same:
        return BLOCKED, ("the compaction and ingest locks are the same path; one lock cannot "
                         "express the ordering between two writers")
    # Existence is NOT required: the lock files are created on acquisition. What must be true is
    # that both paths are declared, distinct, and their directories are writable by the operator
    # -- a lock that cannot be created is a lock that is never held.
    for kind in ("compaction", "ingest"):
        parent = os.path.dirname(os.path.abspath(locks[kind])) or "."
        if not os.path.isdir(parent):
            return BLOCKED, f"the {kind} lock directory does not exist: {parent}"
    return READY, _reason(compaction=locks["compaction"], ingest=locks["ingest"],
                          order="compaction acquired before ingest")


def _check_capacity(t: Dict) -> tuple:
    cap = t.get("capacity") or {}
    if not cap:
        return NOT_ADJUDICATED, "no capacity evidence was supplied"
    if not cap.get("run_id") or not cap.get("measured_utc"):
        return NOT_ADJUDICATED, ("the capacity figure carries no run_id/measured_utc, so it is "
                                 "a number in a file rather than a measurement")
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
    return READY, f"proven by {proven}"


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


def assess(target: Dict) -> Dict:
    """Adjudicate one deployment target and return a machine-readable report."""
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
