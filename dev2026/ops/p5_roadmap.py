"""dev2026 — P5 roadmap exception, capacity-unlock options and critical path.

The authority for all three lives **here**, not in prose. A roadmap that exists only as a
document drifts the moment one section is corrected and another is not — P5-S6 shipped two
contradictory status sections for exactly that reason, and every artifact-drift guard passed
because they all read the first copy. The documents are generated from and checked against these
declarations.

## What this module is for

P5-S7's gate is **NOT MET** and cannot be met locally: G3, G4, H7 and G16 carry production
thresholds, and G18 is blocked on disk. The spec says S8 is not authored until S7 is green, so a
literal reading stops all work — including the read-only runbook whose entire purpose is to
collect the very production evidence those gates need. `ROADMAP_EXCEPTION` records a narrow way
out of that deadlock and, just as importantly, records everything it does **not** unlock.

`authorizes()` is the enforcement point: it answers `False` for every mutating action, so the
exception cannot be cited for a prune, a swap, a deletion or a deployment.
"""
from __future__ import annotations

from typing import Dict, List

#: P5-S7's own verdict. Never "PASS" — the exception below routes around the deadlock, it does
#: not adjudicate the gates.
S7_GATE = {
    "verdict": "NOT_MET",
    "green": ["G1", "G2", "G5", "G6", "G8", "G9", "G10", "G11", "G12"],
    "not_green": {"G3": "NOT_ADJUDICATED", "G4": "NOT_ADJUDICATED", "G7": "PARTIAL"},
    "local_implementation": "SIGNED_OFF",
    "note": ("local implementation and wiring are signed off at f85a96d; that is not a gate "
             "pass and does not authorize S8 execution, only S8 authoring under the "
             "exception below."),
}

#: Gates that only production evidence can settle. This list is the exception's justification:
#: no local synthetic test can move any of them, so waiting for them before AUTHORING the
#: runbook that collects them is circular.
PRODUCTION_DEPENDENT_GATES = {
    "G3": "point p95 vs the CURRENT production TieredCube baseline on VM24",
    "G4": "366-day p95 at production geometry (17999 x 36000)",
    "H7": "fd/RSS under sustained load on the production process, plus inode headroom",
    "G16": "metadata-scan wall time and snapshot-open cost on the production tree",
    "G18": "free disk on /dev/sdc1; BLOCKED at 267 GiB against a 403.3 GiB minimum",
}

#: Actions the exception explicitly does NOT authorize. `authorizes()` refuses every one.
FORBIDDEN_UNDER_EXCEPTION = (
    "prune", "swap", "compaction", "repair", "hold_delete", "deletion",
    "production_write", "deployment", "cron_change", "pm2_change", "systemd_change",
    "nginx_change", "config_change", "s9_execution", "s10_rollout",
)

#: The only things it does authorize.
PERMITTED_UNDER_EXCEPTION = ("s8_runbook_authoring", "local_tests", "local_documents")

ROADMAP_EXCEPTION = {
    "id": "P5-EX-1",
    "applies_to": "P5-S8 authoring only",
    "s7_gate_state": "NOT_MET",
    "rationale": ("S14 P5-S7 says S8 is not authored until the S7 gate is green. G3, G4, H7 and "
                  "G16 are production-dependent and G18 is blocked on disk, so no local "
                  "synthetic test can turn them green. Waiting for them before authoring the "
                  "read-only runbook that COLLECTS them is circular: the runbook is the "
                  "instrument, not the conclusion."),
    "authorizes": list(PERMITTED_UNDER_EXCEPTION),
    "does_not_authorize": list(FORBIDDEN_UNDER_EXCEPTION),
    "s8_execution": "requires a separate human approval; authoring alone confers nothing",
    "s9_execution": ("read-only / shadow evidence collection only, and only after a separate "
                     "human approval that is not implied by this exception"),
    "s10_rollout": "requires its own fresh written approval; an S9 PASS does not confer it",
    "does_not_mark_s7_pass": True,
    "expires_when": ("G3/G4/H7/G16 are adjudicated from production evidence and G18 is "
                     "unblocked; at that point S7 is re-adjudicated on its merits."),
}

# --------------------------------------------------------------------------- capacity
#: From the VM24 read-only measurement (2026-08-19), `bench/results/p5s6_vm24_readonly.json`.
CAPACITY = {
    "label": "VM24_PRODUCTION_READ_ONLY",
    #: The newest evidence that exists, and also a snapshot of a filling volume taken on one
    #: day. Both are true, so it is used AND labelled. Refreshing it means a new read-only scan,
    #: not an edit here.
    "status": "historical/provisional",
    "as_of": "2026-08-19",
    "must_refresh_before_use": True,
    "refresh_requirement": ("a current VM24 read-only scan log must be available before these "
                            "figures are relied on for a go/no-go; when it is, regenerate every "
                            "dependent artifact from that evidence rather than editing these "
                            "values by hand."),
    "measured_utc": "2026-08-19T07:40:00Z",
    "device": "/dev/sdc1", "fstype": "ext4", "mount": "/home/odbadmin/Data",
    "free_now_gib": 267.0,
    "hard_reserve_gib": 250,
    "additional_gib": 153.3,
    "minimum_free_gib": 403.3,
    "recommended_free_gib": 600.0,
    "shortfall_gib": 136.3,
    "currently_reclaimable_gib": 0.0,
    "verdict": "BLOCKED",
    "recommended_basis": ("the 403.3 GiB minimum plus ~200 GiB growth margin; at the measured "
                          "2.74 GiB/day combined ingest that is roughly 70 days of headroom."),
}

#: HARD_RESERVE is not a tuning knob. Lowering it to make G18 pass changes the reported verdict
#: without changing a single byte of free space.
HARD_RESERVE_IS_FIXED = True

CAPACITY_OPTIONS = [
    {
        "option": "A", "name": "Expand the data volume", "preferred": True,
        "requirement_gib": CAPACITY["minimum_free_gib"],
        "recommended_gib": CAPACITY["recommended_free_gib"],
        "proof_obligations": [
            "df on the expanded volume shows free_now >= 403.3 GiB (600 GiB recommended)",
            "the expansion is on the SAME filesystem that holds $G, so publication stays a "
            "same-filesystem os.replace",
        ],
        "why_preferred": ("nothing about the storage topology changes, so every path semantic "
                          "the manifest authority relies on is already proven."),
    },
    {
        "option": "B", "name": "External staging / compaction volume", "preferred": False,
        "proof_obligations": [
            "required free space on the staging volume, measured not assumed",
            "mount persistence across reboot (fstab/systemd unit) and the correct filesystem "
            "type; flock is unreliable on NFS (S16-Q15)",
            "atomic publication semantics: os.replace is atomic only WITHIN a filesystem, so a "
            "cross-device rename is not a commit point and must not be treated as one",
            "rollback and backup coverage for the staging volume, including whether a whole-VM "
            "snapshot covers it -- the WAL anchor domain question in another form",
            "no unsafe cross-filesystem rename assumptions anywhere in the build or publish "
            "path",
            "the FINAL block location remains under the manifest root, so segment paths still "
            "resolve inside it and the containment guard still holds",
        ],
        "why_not_preferred": ("it introduces a second filesystem into a design whose commit "
                              "point is a same-filesystem rename. Every obligation above is a "
                              "way that assumption can silently fail."),
    },
]

#: The distinction that has to survive into the runbook: space unblocks the BUILD, and nothing
#: else. Reclaiming the holds is a separate decision with a separate evidence bar.
CAPACITY_SCOPE = {
    "enables": "compaction: the block build and publication can be attempted",
    "does_not_authorize": ("deletion of the 1267 daily holds, or of any delta backup. The "
                           "hold audit found 0.0 GiB currently reclaimable, and expanding the "
                           "real day sets made two entries MORE blocked, not less."),
    "deletion_additionally_requires": [
        "a recovery/parity policy decision by ops",
        "recovery evidence of one of the three accepted kinds (verified value parity, a "
        "recorded external manifest, or an immutable generation with fingerprint provenance)",
        "day-set membership is NOT sufficient: a new block covering the same days proves "
        "coverage, never value parity",
    ],
}

# --------------------------------------------------------------------------- critical path
CRITICAL_PATH: List[Dict] = [
    {"step": 1, "name": "P5-S7 local implementation sign-off",
     "status": "COMPLETE", "actor": "Claude + Codex", "gate": None,
     "note": "signed off at f85a96d; NOT an S7 gate pass"},
    {"step": 2, "name": "Roadmap exception + S8 runbook authoring",
     "status": "IN_PROGRESS", "actor": "Claude (local only)", "gate": None,
     "note": "this phase; authoring contacts no host"},
    {"step": 3, "name": "IT capacity expansion, or an approved staging-volume design",
     "status": "BLOCKED_ON_OPS", "actor": "IT / ops", "gate": "G18",
     "note": "option A or option B; HARD_RESERVE is not lowered either way"},
    {"step": 4, "name": "Fresh VM24 read-only / shadow approval",
     "status": "NOT_REQUESTED", "actor": "human", "gate": None,
     "note": "not implied by the roadmap exception"},
    {"step": 5, "name": "Execute S9 read-only evidence collection",
     "status": "NOT_STARTED", "actor": "Codex / ops", "gate": None,
     "note": "read-only throughout; shadow copies only"},
    {"step": 6, "name": "Re-adjudicate G3/G4/H7/G16/G18",
     "status": "NOT_STARTED", "actor": "Codex", "gate": "G3,G4,H7,G16,G18",
     "note": "on production evidence; this is where the exception expires"},
    {"step": 7, "name": "Fresh S10 approval, if gates and capacity pass",
     "status": "NOT_STARTED", "actor": "human", "gate": None,
     "note": "an S9 PASS does not confer it"},
    {"step": 8, "name": "First compaction in the EXISTING quiesced posture",
     "status": "NOT_STARTED", "actor": "Codex / ops", "gate": None,
     "note": ("ingest lock -> PM2 stop/quiesce -> build -> publish -> verify -> prune/swap "
              "ONLY after verification -> PM2 start -> API verification")},
    {"step": 9, "name": "Classify old daily/hold data under the retention/recovery policy",
     "status": "NOT_STARTED", "actor": "ops", "gate": None,
     "note": ("only after the new block is published AND verified. Never delete solely "
              "because the new block has the same day membership.")},
    {"step": 10, "name": "Record actual freed bytes with a fresh df",
     "status": "NOT_STARTED", "actor": "ops", "gate": None,
     "note": "theoretical reclaim is not capacity evidence"},
]

#: The compaction posture, spelled out so step 8 cannot be paraphrased into something shorter.
#:
#: Corrected in review round 2: it now opens with the **compaction lock**, not the ingest lock.
#: The ingest lock fences ingest; only the compaction reservation (§7.1b) fences a second
#: compaction, and a build that takes the ingest lock first is unfenced against exactly the
#: writer that can invalidate it. Staleness/precheck runs INSIDE both locks -- checked before
#: them, it describes a state another writer may already have changed.
COMPACTION_POSTURE = ("compaction lock", "ingest lock", "staleness/prechecks", "quiesce",
                      "build/publish/verify as applicable",
                      "prune/swap only after verification", "restart/API verification")

#: The lock is not a step that completes; it is a scope. Releasing it after "build" would leave
#: publish, verify and prune unfenced -- which is the window §7.1b exists to close.
COMPACTION_LOCK_SCOPE = {
    "held_from": "compaction lock acquisition, before the ingest lock",
    "held_until": "after restart/API verification completes",
    "held_across_critical_section": True,
    "why": ("a compaction reservation released early fences the cheap half of the operation "
            "and leaves publish, verify and prune -- the half that mutates the served view -- "
            "open to a second compactor."),
}

# --------------------------------------------------------------------------- S8 contract
#: Sections the S8 runbook must carry. The test suite checks the document against THIS list, so
#: a section quietly dropped from the runbook fails rather than going unnoticed.
S8_REQUIRED_SECTIONS = {
    "G3_G4_latency": "production latency measurement method for G3 and G4",
    "H7_inodes": "inode headroom plus sustained fd/RSS/thread evidence",
    "G16_metadata": "metadata-scan and snapshot-open evidence",
    "G18_capacity": "capacity preflight",
    "identity_preflight": "WAL / anchor / deployment identity preflight",
    "abort_conditions": "explicit abort conditions",
    "artifacts": "artifact paths and provenance",
    "no_write": "no-write guarantees",
}

#: Preflight fields every S8 run must record. A run missing any of them is not evidence.
S8_PREFLIGHT_FIELDS = (
    "run_id", "host", "measured_utc", "deploy_sha", "code_root", "python", "pythonpath",
    "data_root",
    "anchor_root", "anchor_domain_id", "wal_root", "wal_repairs_pending",
    "manifest_root", "manifest_generation", "filesystem_device", "filesystem_fstype",
    "free_now_bytes", "inodes_free", "label",
)


#: Two profiles, because the host as it stands has NO P5 roots (`p5_roots_present: false` in
#: the recorded evidence). Demanding ANCHOR_ROOT, WAL state and a manifest generation before
#: collecting *legacy* G4/G16 baselines makes the runbook unrunnable on the only host it has.
S8_PROFILES = {
    "legacy_baseline": {
        "requires_p5_roots": False,
        "scope": "measurement",
        "purpose": ("baseline evidence from the CURRENT monolith deployment: G4 span timing, "
                    "G16 metadata scan, G18 capacity, H7 inode headroom."),
        "required_fields": ("run_id", "host", "measured_utc", "deploy_sha", "code_root",
                            "python", "pythonpath", "data_root", "filesystem_device",
                            "filesystem_fstype", "free_now_bytes", "inodes_free", "label"),
        #: Recorded as `not_applicable_with_reason`, never omitted. An absent field reads as
        #: clean; "does not apply, because ..." reads as what it is.
        "not_applicable": {
            "anchor_root": "no P5 repair WAL is deployed, so there is no anchor to resolve",
            "anchor_domain_id": "follows anchor_root",
            "wal_root": "p5_repairs does not exist on the host",
            "wal_repairs_pending": ("no WAL, so neither a count nor zero is meaningful; zero "
                                    "here would assert a clean WAL that does not exist"),
            "manifest_root": "p5_blocks does not exist on the host",
            "manifest_generation": "no manifest is published, so there is no generation",
        },
        "may_adjudicate": [],
        "may_not_adjudicate": ["G3", "G16_snapshot_open", "H7_sustained_load"],
        "may_not_adjudicate_reason": ("no segmented base is deployed, so there is no segmented "
                                      "arm for G3 and nothing to snapshot-open for G16; H7's "
                                      "sustained half needs load this profile must not generate "
                                      "against production."),
    },
    "p5_shadow": {
        "requires_p5_roots": True,
        #: VERIFICATION ONLY, and narrowed deliberately. The previous version said "everything
        #: downstream runs against $SHADOW_ROOT" while G4 still called 127.0.0.1:8080, H7 still
        #: read production PM2 and /proc, and G16 still walked $G -- so the claim was false and
        #: the measurements were production ones wearing a shadow label. Isolating them needs a
        #: real second service, which a read-only step does not build. Rather than leave the
        #: ambiguity, the profile now claims only what it does: it PROVES a shadow is correctly
        #: provisioned, and adjudicates nothing.
        "scope": "verification_only",
        "purpose": ("verify that a pre-provisioned P5 shadow is complete and safely placed: "
                    "manifest, referenced blocks, WAL root, and an anchor declaring a rollback "
                    "domain. It takes NO measurement."),
        "required_fields": tuple(S8_PREFLIGHT_FIELDS),
        "not_applicable": {},
        "may_adjudicate": [],
        "may_not_adjudicate": ["G3_production_baseline", "G4", "H7_sustained_load",
                               "G16_metadata_scan", "G16_snapshot_open"],
        "may_not_adjudicate_reason": ("this profile runs no measurement. A shadow host is not "
                                      "the production service, and nothing here redirects the "
                                      "latency, fd/RSS or metadata-scan collection away from "
                                      "production -- so none of those gates may be claimed "
                                      "from it. They stay NOT_ADJUDICATED until an isolated "
                                      "shadow service exists to measure."),
    },
}

#: `policy_permits` is a POLICY predicate, not a control. Nothing calls it before an ssh, a
#: prune or a deployment, and saying otherwise would misrepresent what stops an operator.
POLICY_PREDICATE_IS_ENFORCED = False
POLICY_PREDICATE_NOTE = ("advisory only: not wired into any execution wrapper. It answers what "
                         "the roadmap exception permits; it cannot stop a command anyone "
                         "chooses to type. The controls that actually stop things are the "
                         "approval gates and the runbook's own NO-GO preflights.")


def policy_permits(action: str) -> bool:
    """Does the roadmap exception PERMIT `action`? Advisory; enforces nothing at runtime.

    This **does not** prevent S9 or S10 from being executed manually, and nothing calls it on
    the path to an ssh, a prune or a deployment. It is the written policy in executable form,
    so tests can check the policy for self-consistency -- not a control.

    Fails closed: anything not explicitly permitted answers False, so a mutating verb added
    later is refused by default rather than by someone remembering to list it."""
    return action in PERMITTED_UNDER_EXCEPTION


def authorizes(action: str) -> bool:
    """Deprecated alias for `policy_permits`. The old name implied a control that does not
    exist -- nothing consults it before an operation runs."""
    return policy_permits(action)


def s7_is_pass() -> bool:
    """Never true here. Present so a test can assert the shape rather than grep for a word."""
    return S7_GATE["verdict"] == "PASS"


def report() -> dict:
    """The machine-readable roadmap. Documents are checked against this."""
    return {
        "label": "LOCAL_ONLY_PLANNING_DOCUMENT",
        "scope": ("planning and authoring only. No VM24 contact, no production action, no "
                  "cleanup, and no capacity claim derived from local synthetic measurement."),
        "s7_gate": dict(S7_GATE),
        "roadmap_exception": dict(ROADMAP_EXCEPTION),
        "production_dependent_gates": dict(PRODUCTION_DEPENDENT_GATES),
        "capacity": dict(CAPACITY),
        "hard_reserve_is_fixed": HARD_RESERVE_IS_FIXED,
        "capacity_options": [dict(o) for o in CAPACITY_OPTIONS],
        "capacity_scope": dict(CAPACITY_SCOPE),
        "critical_path": [dict(s) for s in CRITICAL_PATH],
        "compaction_posture": list(COMPACTION_POSTURE),
        "s8_required_sections": dict(S8_REQUIRED_SECTIONS),
        "s8_preflight_fields": list(S8_PREFLIGHT_FIELDS),
        "s8_profiles": {k: {kk: (dict(vv) if isinstance(vv, dict) else
                                 (list(vv) if isinstance(vv, tuple) else vv))
                            for kk, vv in v.items()} for k, v in S8_PROFILES.items()},
        "compaction_lock_scope": dict(COMPACTION_LOCK_SCOPE),
        "policy_predicate": {"enforced": POLICY_PREDICATE_IS_ENFORCED,
                             "note": POLICY_PREDICATE_NOTE},
        "next_approval_required": {
            "what": "IT capacity expansion (option A) or an approved staging-volume design "
                    "(option B), then a fresh VM24 read-only / shadow approval",
            "blocking_step": 3,
            "not_implied_by": "the P5-EX-1 roadmap exception",
        },
    }


def main(argv=None) -> int:
    """Write the roadmap artifact. Carries no measurement of its own — every number in it is
    quoted from the VM24 read-only artifact, and a test asserts they still match."""
    import argparse
    import json
    import os

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "bench", "results"))
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "p5_critical_path.json")
    with open(path, "w") as fh:
        json.dump(report(), fh, indent=2, sort_keys=True)
    print(f"wrote {path}")
    r = report()
    print(f"  S7 gate: {r['s7_gate']['verdict']}   capacity: {r['capacity']['verdict']}")
    print(f"  next approval blocks step {r['next_approval_required']['blocking_step']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
