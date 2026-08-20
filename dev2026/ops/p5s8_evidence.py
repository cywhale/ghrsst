"""dev2026 — derive the committed VM24 evidence FROM a collected S8 run, never by hand.

The 2026-08-20 run was collected, assembled and re-validated, and then its numbers reached the
report as prose while `bench/results/p5s6_vm24_readonly.json` still carried the 2026-08-19
figure. A measurement that exists only in a message is not evidence: the next reader opens the
artifact and sees 267 GiB.

So the committed evidence is **generated from the run directory**. Every field here traces to a
file the runbook wrote and the existing parsers accept; nothing is typed in. `main()` rewrites
the dependent artifacts, and `tests/test_phase2_p5s8_evidence.py` asserts the committed copies
match what this module produces — so a stale figure fails rather than lingering.

## What the numbers do and do not mean

`free_now` rose 31.4 GiB between the aborted 08:00 run and the 11:00 one. That is **operator
attribution**, recorded as such: unrelated files under `~/Data` were moved by the operator, and
the SRTM/terrain directory remains. It is **not** GHRSST reclaim, not automated cleanup, and not
a sustainable capacity guarantee — G18 stays BLOCKED either way.
"""
from __future__ import annotations

import json
import os
from typing import Dict

from ops.p5s8_report import (ArtifactError, assemble, check_g16_scan, check_g4_request,
                             check_g4_series, check_h7_series, check_pm2_roster,
                             check_preflight_values, _run_id_from)

#: Where collected runs live in the repository.
RUNS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "bench", "results", "p5s8_runs")

#: The run this evidence is derived from.
CURRENT_RUN = "p5s8_20260820T110036Z"

#: G18's threshold, unchanged: HARD_RESERVE 250 + additional 153.3.
MINIMUM_FREE_GIB = 403.3
RECOMMENDED_FREE_GIB = 600.0
HARD_RESERVE_GIB = 250

GIB = 1024 ** 3

#: Why free space moved between the two runs of 2026-08-20. Operator-reported, and labelled as
#: such: nothing in the collected artifacts can attribute a capacity change to a cause.
CAPACITY_CHANGE_ATTRIBUTION = {
    "observed_gib": 31.4,
    "from_run": "p5s8_20260820T080028Z (aborted at g4-probe)",
    "to_run": CURRENT_RUN,
    "source": "operator-reported",
    "cause": ("unrelated files under ~/Data were moved by the operator; the SRTM/terrain "
              "directory remains"),
    "is_ghrsst_reclaim": False,
    "is_automated_cleanup": False,
    "is_sustainable_guarantee": False,
    "note": ("recorded as attribution, not as a measurement: the artifacts show free space "
             "changed, never why. G18 remains BLOCKED either way."),
}


def _load(run_dir: str, name: str):
    with open(os.path.join(run_dir, name)) as fh:
        return json.load(fh)


def _text(run_dir: str, name: str) -> str:
    with open(os.path.join(run_dir, name)) as fh:
        return fh.read()


def run_dir(run: str = CURRENT_RUN) -> str:
    path = os.path.join(RUNS_DIR, run)
    if not os.path.isdir(path):
        raise ArtifactError(f"no collected run at {path}")
    return path


def derive(run: str = CURRENT_RUN) -> dict:
    """Validate a collected run through the shipped parsers and derive the evidence record.

    Identity's module fingerprints are **not** re-hashed here: they name absolute paths on the
    host that produced them, and re-hashing off-host would fail for a reason that has nothing to
    do with the evidence. Assembly already ran that check on VM24, where it means something.
    """
    path = run_dir(run)
    run_id = _run_id_from(path)

    marker = _load(path, "run.json")
    if marker.get("run_id") != run_id:
        raise ArtifactError(f"run.json says {marker.get('run_id')!r}, directory says {run_id!r}")

    preflight = _load(path, "preflight.json")
    check_preflight_values(preflight, "legacy_baseline", run_id=run_id)
    identity = _load(path, "identity.json")
    if identity["deploy_sha"] != identity["head_sha"] or identity["worktree_clean"] is not True:
        raise ArtifactError("identity.json does not affirm a clean checkout at the deploy SHA")

    capacity = _load(path, "capacity.json")
    request = check_g4_request(_load(path, "g4_request.json"), run_id=run_id)
    pids = check_pm2_roster(_load(path, "pm2_jlist.json"), run_id=run_id)
    g4 = check_g4_series(os.path.join(path, "g4_366d_seconds.txt"), "g4_366d_seconds.txt",
                         run_id=run_id, expected=request["g4_samples"])
    h7 = check_h7_series(os.path.join(path, "h7_fd_rss_timeseries.txt"),
                         "h7_fd_rss_timeseries.txt", run_id=run_id, expected_pids=pids)
    g16 = check_g16_scan(os.path.join(path, "g16_metadata_scan.txt"), "g16_metadata_scan.txt",
                         run_id=run_id)

    free_bytes = int(capacity["free_now_bytes"])
    free_gib = round(free_bytes / GIB, 1)
    seconds = [float(x) for x in _text(path, "g4_366d_seconds.txt").split("\n")[1:] if x.strip()]
    ms = sorted(v * 1000 for v in seconds)

    def pct(q):
        k = max(0, min(len(ms) - 1, int(round((len(ms) - 1) * q))))
        return round(ms[k], 2)

    return {
        "label": preflight["label"],
        "provenance": {
            "run_id": run_id,
            "host": preflight["host"],
            "measured_utc": preflight["measured_utc"],
            "deploy_sha": identity["deploy_sha"],
            "profile": "legacy_baseline",
            "artifact_dir": f"bench/results/p5s8_runs/{run}",
            "derived_by": "ops/p5s8_evidence.py -- never typed by hand",
        },
        "capacity": {
            "data_root": capacity["data_root"],
            "filesystem_device": capacity["filesystem_device"],
            "filesystem_fstype": capacity["filesystem_fstype"],
            "free_now_bytes": free_bytes,
            "free_now_gib": free_gib,
            "inodes_free": int(capacity["inodes_free"]),
            "hard_reserve_gib": HARD_RESERVE_GIB,
            "minimum_free_gib": MINIMUM_FREE_GIB,
            "recommended_free_gib": RECOMMENDED_FREE_GIB,
            "shortfall_gib": round(MINIMUM_FREE_GIB - free_gib, 1),
            "verdict": "BLOCKED" if free_gib < MINIMUM_FREE_GIB else "NOT_BLOCKED",
            "change_attribution": dict(CAPACITY_CHANGE_ATTRIBUTION),
        },
        "g4": {
            "scope": "legacy_baseline",
            "window_start": request["start"], "window_end": request["end"],
            "span_days": request["span_days"],
            "window_authority": "healthz.point_latest",
            "point_latest": request["point_latest"],
            "samples": g4["samples"], "concurrency": 1,
            "p50_ms": pct(0.50), "p95_ms": pct(0.95),
            "adjudicates": [],
            "note": ("a legacy_baseline latency reading at concurrency 1. It adjudicates no "
                     "production segmented or compaction gate: there is no segmented arm "
                     "deployed to compare against."),
        },
        "h7": {
            "scope": "master_only",
            "scope_reason": ("the pid PM2 reports for the app is the GUNICORN MASTER; its "
                             "worker children (-w 2) were not sampled, so these figures are "
                             "the master's footprint and not the serving processes'."),
            "sampled_pids": sorted(pids),
            "samples": h7["samples"], "workers_sampled": h7["workers"],
            "inodes_free": int(capacity["inodes_free"]),
            "sustained_load": "NOT_ADJUDICATED",
            "sustained_load_reason": ("no sustained load was generated: this runbook is "
                                      "read-only and must not load production."),
        },
        "g16": {
            "metadata_scan": {"files": g16["files"], "dirs": g16["dirs"],
                              "root": "/home/odbadmin/Data/ghrsst"},
            "snapshot_open": "NOT_ADJUDICATED",
            "snapshot_open_reason": ("no segmented manifest is deployed on VM24, so "
                                     "SegmentedCubeStore construction cannot be timed. Kept "
                                     "SEPARATE from the metadata-scan evidence above."),
        },
        "gates": {
            "G3": "NOT_ADJUDICATED", "G4": "NOT_ADJUDICATED",
            "G16_metadata_scan": "EVIDENCE_COLLECTED",
            "G16_snapshot_open": "NOT_ADJUDICATED",
            "H7_inode_headroom": "EVIDENCE_COLLECTED",
            "H7_sustained_load": "NOT_ADJUDICATED",
            "G18": "BLOCKED" if free_gib < MINIMUM_FREE_GIB else "NOT_BLOCKED",
        },
        "assembled_on_host": True,
    }


def superseded_capacity_gate(evidence: dict) -> dict:
    """The capacity block for `p5s6_vm24_readonly.json`, replacing the 2026-08-19 figure."""
    cap = evidence["capacity"]
    return {
        "label": evidence["label"],
        "free_now_gb": cap["free_now_gib"],
        "free_now_required_gb": cap["minimum_free_gib"],
        "hard_reserve_gb": cap["hard_reserve_gib"],
        "shortfall_gb": cap["shortfall_gib"],
        "verdict": cap["verdict"],
        "measured_utc": evidence["provenance"]["measured_utc"],
        "run_id": evidence["provenance"]["run_id"],
        "deploy_sha": evidence["provenance"]["deploy_sha"],
        "supersedes": ("the 2026-08-19 reading of 267.0 GiB; that figure is historical and "
                       "must not be quoted as the current state"),
        "source_artifact": evidence["provenance"]["artifact_dir"],
        "change_attribution": cap["change_attribution"],
    }


BLOCKERS_START = "<!-- generated:blockers:start -->"
BLOCKERS_END = "<!-- generated:blockers:end -->"


def render_blockers(evidence: dict) -> str:
    """The `Remaining blockers` table, GENERATED.

    It was hand-maintained, and drifted twice: it carried the aborted run's 264.4 GiB beside a
    capacity section that already said 295.7, and went on calling the S8 execution "not
    approved" after it had completed. A table that restates numbers held elsewhere has to be
    derived from them."""
    from ops.p5_roadmap import S8_EXECUTION

    cap = evidence["capacity"]
    run = evidence["provenance"]["run_id"]
    legacy = S8_EXECUTION["legacy_baseline"]
    rows = [
        ("**G18 capacity**",
         f"**BLOCKED** — **{cap['free_now_gib']} GiB free** (measured "
         f"{evidence['provenance']['measured_utc'][:10]}, run `{run}`), "
         f"{cap['minimum_free_gib']} GiB minimum, {int(cap['recommended_free_gib'])} GiB "
         f"recommended; shortfall ≈ {cap['shortfall_gib']} GiB"),
        ("**G3 / G4**", "`NOT_ADJUDICATED` — production thresholds; the S8 G4 reading is a "
                        "`legacy_baseline` baseline that adjudicates nothing"),
        ("**G7**", "`PARTIAL` — the gate's arithmetic is proven; a real fold's temp disk is "
                   "not measured"),
        ("**H7**", "inode headroom has evidence; **sustained load `NOT_ADJUDICATED`**, and the "
                   "collected fd/RSS is `master_only`"),
        ("**G16**", "metadata scan has evidence; **snapshot-open `NOT_ADJUDICATED`** — no "
                    "segmented manifest is deployed"),
        ("**S = 90 / C = 30**", "**provisional only**; nothing has selected it"),
        ("**S8 execution**",
         f"`legacy_baseline` **{legacy['status']}** — {legacy['detail']}"),
        ("**S9 / shadow execution**",
         f"**{S8_EXECUTION['s9_shadow_execution']['status']}** — "
         f"{S8_EXECUTION['s9_shadow_execution']['detail']}"),
        ("**S10**", "not authorized; needs its own fresh written approval"),
    ]
    body = "\n".join(f"| {a} | {b} |" for a, b in rows)
    return (f"{BLOCKERS_START}\n"
            f"<!-- Generated by ops/p5s8_evidence.py from run {run}. Do not edit by hand. -->\n\n"
            f"| blocker | state |\n|---|---|\n{body}\n\n{BLOCKERS_END}")


APPROVALS_START = "<!-- generated:approvals:start -->"
APPROVALS_END = "<!-- generated:approvals:end -->"

#: What each stage still needs. Held beside the STATUS in `S8_EXECUTION` rather than inside it,
#: so the requirement text cannot be mistaken for a status of its own.
APPROVAL_NEEDS = {
    "legacy_baseline": ("**nothing — it already ran.** Do not describe it as pending or as "
                        "needing approval before it can run"),
    "p5_shadow": ("a fresh VM24 read-only approval; the completed `legacy_baseline` run "
                  "confers none"),
    "s9_shadow_execution": ("its own separate human approval — implied neither by P5-EX-1 nor "
                            "by the completed S8 run"),
    "s10_rollout": "its own fresh written approval; an S9 PASS does not confer it",
}


def render_approvals(evidence: dict) -> str:
    """Section 5's body, GENERATED from the execution state.

    Hand-written, it said approval was needed "before any S8 or S9 command runs against the
    host" — after S8 `legacy_baseline` had already run. Asking for an approval that was already
    given and acted on misstates what is actually blocked, so the stage table is derived from
    `S8_EXECUTION` and the threshold from the measured capacity."""
    from ops.p5_roadmap import CAPACITY, ROADMAP_EXCEPTION, S8_EXECUTION

    cap = evidence["capacity"]
    legacy = S8_EXECUTION["legacy_baseline"]
    rows = [
        (f"S8 `legacy_baseline`", f"**{legacy['status']}** — run `{legacy['run_id']}`, deploy "
                                  f"SHA `{evidence['provenance']['deploy_sha'][:7]}`",
         APPROVAL_NEEDS["legacy_baseline"]),
        ("S8 `p5_shadow`", f"**{S8_EXECUTION['p5_shadow']['status']}** — "
                           f"{S8_EXECUTION['p5_shadow']['detail']}",
         APPROVAL_NEEDS["p5_shadow"]),
        ("S9 / shadow execution", f"**{S8_EXECUTION['s9_shadow_execution']['status']}**",
         APPROVAL_NEEDS["s9_shadow_execution"]),
        ("S10 rollout", "**not authorized**", APPROVAL_NEEDS["s10_rollout"]),
    ]
    table = "\n".join(f"| {a} | {b} | {c} |" for a, b, c in rows)
    return (
        f"{APPROVALS_START}\n"
        f"<!-- Generated by ops/p5s8_evidence.py from run "
        f"{evidence['provenance']['run_id']}. Do not edit by hand. -->\n\n"
        f"**IT capacity expansion (option A) to at least {cap['minimum_free_gib']} GiB free — "
        f"{int(cap['recommended_free_gib'])} GiB recommended — or an approved staging-volume "
        f"design meeting all six option-B obligations.**\n\n"
        f"That unblocks critical-path step 3. It is **not** implied by the P5-EX-1 exception, "
        f"which authorizes {', '.join(ROADMAP_EXCEPTION['authorizes'])} and nothing else.\n\n"
        f"Approval is a per-stage question, and the stages are not in the same place:\n\n"
        f"| stage | state | what it still needs |\n|---|---|---|\n{table}\n\n"
        f"So the outstanding asks are **capacity** (above) and, separately, **a fresh VM24 "
        f"read-only / shadow approval for the work that has not run**. Neither is an approval "
        f"for S8 `legacy_baseline`, which is done.\n\n{APPROVALS_END}")


def _mark_superseded_gate(s6: dict, evidence: dict) -> None:
    """Mark the S6 artifact's G18 gate entry historical.

    `capacity_gate` is regenerated, but `gates.G18_capacity` restated the same verdict in prose
    carrying `projected_free_after 113.7` and `shortfall 136.3` — both computed from the
    2026-08-19 267 GiB. Left unmarked they read as the current adjudication."""
    gate = s6.get("gates", {}).get("G18_capacity")
    if not isinstance(gate, dict):
        return
    gate["as_of"] = "2026-08-19"
    gate["status"] = "historical"
    gate["superseded_by"] = (
        f"run {evidence['provenance']['run_id']} -- free {evidence['capacity']['free_now_gib']} "
        f"GiB, shortfall {evidence['capacity']['shortfall_gib']} GiB; see capacity_gate in this "
        f"file and bench/results/p5s8_vm24_readonly.json. The figures in this entry's `reason` "
        f"and `audit_note` are the 2026-08-19 arithmetic and are NOT the current state. The "
        f"verdict is unchanged.")


def supersede_s6(s6: dict, evidence: dict) -> dict:
    """Apply every supersession the S6 artifact needs, and RETURN it.

    A pure function on purpose. While this was inline in `main()`, deleting a whole
    supersession step and regenerating produced a file that still passed every check --
    because `main()` rewrites the artifact from the copy it just read, so a step that ADDS
    keys looks like a no-op once its keys are in the committed file. The generator was
    preserving its own output and being credited with producing it.

    Because it is pure and idempotent, a test can strip the marks off the committed artifact,
    re-apply this, and demand the committed file back exactly. A dropped step then fails
    instead of being inherited."""
    s6 = json.loads(json.dumps(s6))
    run_id = evidence["provenance"]["run_id"]
    free_gib = evidence["capacity"]["free_now_gib"]

    # Merging is not enough: the old block carried DERIVED fields -- projected_free_after_gb,
    # gate_ok, and a provenance string naming 2026-08-19 -- all computed from 267 GiB. A merge
    # leaves those behind, silently inconsistent with the free space beside them.
    old_gate = s6.get("capacity_gate", {})
    additional = float(old_gate.get("additional_gb", 153.3))
    projected = round(free_gib - additional, 1)
    s6["capacity_gate"] = {
        **old_gate, **superseded_capacity_gate(evidence),
        "projected_free_after_gb": projected,
        "gate_ok": projected >= evidence["capacity"]["hard_reserve_gib"],
        "inputs_provenance": {
            **old_gate.get("inputs_provenance", {}),
            "free_now_gb": (f"statvfs on {evidence['capacity']['filesystem_device']}, VM24 "
                            f"{evidence['provenance']['measured_utc'][:10]}, run {run_id}"),
        },
    }
    # The `filesystem` block is the 2026-08-19 snapshot -- size, used, avail, inode counts all
    # from that day. Splicing a fresh `avail` into it would produce a half-updated record that
    # is true of no single moment, so it is MARKED historical instead and points at the run
    # that supersedes it.
    fs = s6.get("filesystem")
    if isinstance(fs, dict):
        fs["as_of"] = "2026-08-19"
        fs["status"] = "historical"
        fs["superseded_by"] = (f"run {run_id} -- see bench/results/p5s8_vm24_readonly.json "
                               f"for current free space")

    _mark_superseded_gate(s6, evidence)

    # The hold audit quotes free space as current state too.
    hold = s6.get("hold_lifecycle_audit", {}).get("capacity")
    if isinstance(hold, dict):
        hold["free_now_gib"] = free_gib
        hold["free_now_source"] = (f"superseded by run {run_id}; the 2026-08-19 figure is "
                                   f"historical")
    s6["capacity_gate_superseded_note"] = (
        f"capacity_gate was regenerated from the collected S8 run {run_id}; the rest of this "
        f"file remains the 2026-08-19 read-only measurement.")
    return s6


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=CURRENT_RUN)
    args = ap.parse_args(argv)

    evidence = derive(args.run)
    dev = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    results = os.path.join(dev, "bench", "results")

    out = os.path.join(results, "p5s8_vm24_readonly.json")
    with open(out, "w") as fh:
        json.dump(evidence, fh, indent=2, sort_keys=True)
    print(f"wrote {out}")

    s6_path = os.path.join(results, "p5s6_vm24_readonly.json")
    with open(s6_path) as fh:
        s6 = supersede_s6(json.load(fh), evidence)
    with open(s6_path, "w") as fh:
        json.dump(s6, fh, indent=2, sort_keys=True)
    print(f"updated {s6_path}")

    memo_path = os.path.join(dev, "specs", "p5_critical_path_memo.md")
    with open(memo_path) as fh:
        memo = fh.read()
    for what, start, end, render in (
            ("blockers table", BLOCKERS_START, BLOCKERS_END, render_blockers),
            ("next-approval section", APPROVALS_START, APPROVALS_END, render_approvals)):
        if start in memo and end in memo:
            head, tail = memo[:memo.index(start)], memo[memo.index(end) + len(end):]
            memo = head + render(evidence) + tail
            print(f"regenerated the {what} in {memo_path}")
        else:
            print(f"NOTE: {memo_path} has no markers for the {what}; not rewritten")
    with open(memo_path, "w") as fh:
        fh.write(memo)

    cap = evidence["capacity"]
    print(f"  free_now {cap['free_now_gib']} GiB | minimum {cap['minimum_free_gib']} GiB | "
          f"shortfall {cap['shortfall_gib']} GiB | G18 {cap['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
