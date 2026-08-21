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

#: `HARD_RESERVE` is policy, not a tuning knob, and is never lowered to make G18 pass.
HARD_RESERVE_GIB = 250

#: The G18 capacity model: the ONE source for every term in the gate's arithmetic.
#:
#: These terms used to be read back out of `p5s6_vm24_readonly.json` and trusted. That made the
#: artifact an input to its own regeneration: editing `additional_gb` to 0 there produced
#: `projected_free_after_gb = 295.7` and `gate_ok = True` on the next run -- a G18 pass created
#: by editing a file, with free space unchanged. Nothing downstream may read a model term from
#: an artifact; `capacity_model()` computes them all from here, and `_check_capacity_model()`
#: refuses any artifact whose surviving copy disagrees.
CAPACITY_MODEL = {
    "s": 90,
    "base_gb_per_day": 1.62,
    "temp_checkpoint_artifacts_gb": 2.0,
    "ingest_gb_per_day": 2.74,
    "build_days_assumed": 2.0,
    "formula": "projected_free_after = free_now - (new_block + temp + growth)",
}


def capacity_model() -> Dict:
    """Every model term the G18 arithmetic needs, derived from `CAPACITY_MODEL`."""
    m = CAPACITY_MODEL
    new_block = round(m["s"] * m["base_gb_per_day"], 1)
    growth = round(m["ingest_gb_per_day"] * m["build_days_assumed"], 1)
    temp = float(m["temp_checkpoint_artifacts_gb"])
    return {
        "s": m["s"],
        "new_block_gb": new_block,
        "temp_checkpoint_artifacts_gb": temp,
        "growth_during_build_gb": growth,
        "build_days_assumed": float(m["build_days_assumed"]),
        "additional_gb": round(new_block + temp + growth, 1),
        "formula": m["formula"],
    }


#: G18's threshold. DERIVED -- `HARD_RESERVE` plus the model's `additional` -- so the threshold
#: and the arithmetic behind it cannot drift apart. It comes to 403.3 GiB, unchanged.
MINIMUM_FREE_GIB = round(HARD_RESERVE_GIB + capacity_model()["additional_gb"], 1)
RECOMMENDED_FREE_GIB = 600.0

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


def run_dir(run: str = CURRENT_RUN, root=None) -> str:
    runs = RUNS_DIR if root is None else os.path.join(root, "bench", "results", "p5s8_runs")
    path = os.path.join(runs, run)
    if not os.path.isdir(path):
        raise ArtifactError(f"no collected run at {path}")
    return path


def derive(run: str = CURRENT_RUN, root=None) -> dict:
    """Validate a collected run through the shipped parsers and derive the evidence record.

    Identity's module fingerprints are **not** re-hashed here: they name absolute paths on the
    host that produced them, and re-hashing off-host would fail for a reason that has nothing to
    do with the evidence. Assembly already ran that check on VM24, where it means something.
    """
    path = run_dir(run, root=root)
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
            #: Published so downstream readers take the model from the evidence instead of
            #: keeping their own copy. `ops/p5_roadmap.py` held a second 153.3.
            "model": capacity_model(),
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


def execution_bound_to(evidence: dict):
    """`S8_EXECUTION` as of this evidence, refusing a declaration about a different run.

    Both renderers state a run's outcome using figures from `evidence` and a status from
    `S8_EXECUTION`. Nothing tied the two together: regenerating from a NEW run while the
    declaration still named the old one produced a table that reads as one coherent report and
    is two, with the reader given no way to see the seam."""
    from ops.p5_roadmap import S8_EXECUTION

    legacy = S8_EXECUTION["legacy_baseline"]
    run_id = evidence["provenance"]["run_id"]
    if legacy["status"] == "COMPLETED" and legacy.get("run_id") != run_id:
        raise ArtifactError(
            f"S8_EXECUTION['legacy_baseline'] declares run {legacy.get('run_id')!r} COMPLETED, "
            f"but this evidence is run {run_id!r}. Update the declaration to the run being "
            f"reported -- a status and a measurement from different runs must not be rendered "
            f"as one result")
    return S8_EXECUTION


def render_blockers(evidence: dict) -> str:
    """The `Remaining blockers` table, GENERATED.

    It was hand-maintained, and drifted twice: it carried the aborted run's 264.4 GiB beside a
    capacity section that already said 295.7, and went on calling the S8 execution "not
    approved" after it had completed. A table that restates numbers held elsewhere has to be
    derived from them."""
    S8_EXECUTION = execution_bound_to(evidence)

    cap = evidence["capacity"]
    run = evidence["provenance"]["run_id"]
    legacy = S8_EXECUTION["legacy_baseline"]
    rows = [
        # The verdict comes from the evidence, not from a literal beside the figure. Written
        # out, it would keep saying BLOCKED against a free-space number that had risen past
        # the minimum -- the same defect as a measured figure hardcoded in prose.
        ("**G18 capacity**",
         f"**{cap['verdict']}** — **{cap['free_now_gib']} GiB free** (measured "
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
    # NOT `CAPACITY`: the module constant is bound at import, and this renders BEFORE the
    # roadmap refreshes. Every figure below comes from the evidence being reported.
    from ops.p5_roadmap import ROADMAP_EXCEPTION

    S8_EXECUTION = execution_bound_to(evidence)
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


def _check_capacity_model(old_gate: dict, canonical: Dict) -> None:
    """Refuse an artifact whose surviving model terms disagree with the canonical model.

    Recomputing alone is not enough. A silent overwrite turns an edited artifact into a correct
    one and says nothing, so the edit is never noticed; the point is that a tampered or stale
    input is LOUD. Raw types are checked before values, per the repository rule: `True` is an
    `int` in Python and `"153.3"` compares unequal for the wrong reason."""
    for key, want in canonical.items():
        if key not in old_gate:
            raise ArtifactError(
                f"capacity_gate is missing {key!r}; the canonical model says {want!r}. A model "
                f"term cannot be absent and then supplied by a default -- see CAPACITY_MODEL")
        got = old_gate[key]
        if type(got) is bool:
            raise ArtifactError(
                f"capacity_gate[{key!r}] is a bool ({got!r}); a model term must be "
                f"{type(want).__name__}, and bool is an int in Python")
        if type(got) is not type(want):
            raise ArtifactError(
                f"capacity_gate[{key!r}] is {type(got).__name__} ({got!r}); the canonical model "
                f"says {type(want).__name__} ({want!r}). No coercion is applied")
        if got != want:
            raise ArtifactError(
                f"capacity_gate[{key!r}] is {got!r}; the canonical model says {want!r}. The "
                f"artifact is stale or was edited -- change CAPACITY_MODEL, never the artifact")


def _check_thresholds(evidence: dict) -> None:
    """The evidence's own thresholds must be the canonical ones, not whatever it was handed."""
    cap = evidence["capacity"]
    for key, want in (("hard_reserve_gib", HARD_RESERVE_GIB),
                      ("minimum_free_gib", MINIMUM_FREE_GIB)):
        got = cap.get(key)
        if type(got) is bool or type(got) is not type(want) or got != want:
            raise ArtifactError(
                f"evidence capacity {key!r} is {got!r}; the canonical value is {want!r}. "
                f"HARD_RESERVE and the G18 threshold are policy and are never adjusted here")


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
    canonical = capacity_model()
    _check_thresholds(evidence)
    _check_capacity_model(old_gate, canonical)

    # Every model term is written from `canonical`, AFTER the check above -- so the surviving
    # copy is both verified and replaced, and `free_now` is the only measured input.
    additional = canonical["additional_gb"]
    projected = round(free_gib - additional, 1)
    gate_ok = projected >= HARD_RESERVE_GIB
    # free_now >= hard_reserve + additional is the same statement as projected >= hard_reserve.
    # If the two ever disagree, the arithmetic has been reached around, and no artifact is
    # written: a G18 pass is a measurement, never a rearrangement.
    if gate_ok is not (free_gib >= MINIMUM_FREE_GIB):
        raise ArtifactError(
            f"gate_ok {gate_ok} contradicts free_now {free_gib} GiB against the "
            f"{MINIMUM_FREE_GIB} GiB minimum")
    if gate_ok and evidence["capacity"]["verdict"] != "NOT_BLOCKED":
        raise ArtifactError(
            f"gate_ok is True while the evidence verdict is "
            f"{evidence['capacity']['verdict']!r}")
    s6["capacity_gate"] = {
        **old_gate, **superseded_capacity_gate(evidence), **canonical,
        "projected_free_after_gb": projected,
        "gate_ok": gate_ok,
        "inputs_provenance": {
            **old_gate.get("inputs_provenance", {}),
            "free_now_gb": (f"statvfs on {evidence['capacity']['filesystem_device']}, VM24 "
                            f"{evidence['provenance']['measured_utc'][:10]}, run {run_id}"),
            "capacity_model": ("recomputed from ops/p5s8_evidence.py::CAPACITY_MODEL and "
                               "checked against the surviving copy; never read from an "
                               "artifact"),
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


#: Every output a regeneration must leave consistent. Named here so the entry point cannot
#: quietly cover three of them: `p5_critical_path.json` was written only by `ops.p5_roadmap`,
#: so a new evidence run updated the other three and left it describing the previous one.
REGENERATED_OUTPUTS = (
    "bench/results/p5s8_vm24_readonly.json",
    "bench/results/p5s6_vm24_readonly.json",
    "specs/p5_critical_path_memo.md",
    "bench/results/p5_critical_path.json",
)

#: The repository root this module writes into, unless a caller names another.
DEV_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


S8_REL = "bench/results/p5s8_vm24_readonly.json"
S6_REL = "bench/results/p5s6_vm24_readonly.json"
MEMO_REL = "specs/p5_critical_path_memo.md"
ROADMAP_REL = "bench/results/p5_critical_path.json"


def stage(run: str, root: str) -> Dict[str, str]:
    """Produce the FULL contents of every regenerated output, writing none of them.

    The outputs were written one at a time as they were produced, with the roadmap artifact
    last. A failure in the roadmap writer therefore left three files carrying the new
    measurement and the fourth carrying the old one -- the exact inconsistency the entry point
    exists to prevent, reached by the error path instead of by forgetting a command. Raising
    afterwards reports the damage; it does not undo it.

    So nothing is published until everything has been generated and checked. The roadmap step
    needs the new evidence ON DISK to read, so it gets a throwaway directory of its own rather
    than the real one."""
    import contextlib
    import io
    import shutil
    import tempfile
    from ops import p5_roadmap

    staged: Dict[str, str] = {}
    evidence = derive(run, root=root)
    staged[S8_REL] = json.dumps(evidence, indent=2, sort_keys=True)

    with open(os.path.join(root, S6_REL)) as fh:
        staged[S6_REL] = json.dumps(supersede_s6(json.load(fh), evidence),
                                    indent=2, sort_keys=True)

    with open(os.path.join(root, MEMO_REL)) as fh:
        memo = fh.read()
    for what, begin, finish, render in (
            ("blockers table", BLOCKERS_START, BLOCKERS_END, render_blockers),
            ("next-approval section", APPROVALS_START, APPROVALS_END, render_approvals)):
        if begin in memo and finish in memo:
            head, tail = memo[:memo.index(begin)], memo[memo.index(finish) + len(finish):]
            memo = head + render(evidence) + tail
        else:
            print(f"NOTE: {MEMO_REL} has no markers for the {what}; not rewritten")
    staged[MEMO_REL] = memo

    # The roadmap writer reads the evidence file, so it needs one. It gets the STAGED evidence
    # in a scratch tree: the real artifact must not change until every step has succeeded.
    scratch = tempfile.mkdtemp(prefix="p5s8-stage-")
    try:
        scratch_results = os.path.join(scratch, "bench", "results")
        os.makedirs(scratch_results)
        with open(os.path.join(scratch_results, os.path.basename(S8_REL)), "w") as fh:
            fh.write(staged[S8_REL])
        # Its progress lines name the scratch directory, which is not where anything lands.
        with contextlib.redirect_stdout(io.StringIO()):
            rc = p5_roadmap.main(["--out", scratch_results])
        if rc != 0:
            raise ArtifactError(f"the roadmap writer failed with {rc}; nothing was published")
        produced = os.path.join(scratch_results, os.path.basename(ROADMAP_REL))
        if not os.path.exists(produced):
            raise ArtifactError(f"the roadmap writer did not write {ROADMAP_REL}; "
                                f"nothing was published")
        with open(produced) as fh:
            staged[ROADMAP_REL] = fh.read()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    missing = [rel for rel in REGENERATED_OUTPUTS if rel not in staged]
    if missing:
        raise ArtifactError(f"staging did not produce {missing}; nothing was published")
    return staged


def _atomic_write(path: str, text: str) -> None:
    """Replace one file's contents in a single step, so no reader sees a half-written file."""
    tmp = f"{path}.regen-{os.getpid()}"
    with open(tmp, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def publish(staged: Dict[str, str], root: str) -> None:
    """Install the staged outputs, restoring every original if any write fails.

    Generation cannot leave a partial state any more -- `stage()` finishes before this runs.
    This narrows what is left: each file is replaced in one step, and a failure part-way
    through the set puts back the bytes that were there before."""
    originals = {}
    for rel in staged:
        path = os.path.join(root, rel)
        if os.path.exists(path):
            with open(path) as fh:
                originals[rel] = fh.read()
        else:
            originals[rel] = None

    written = []
    try:
        for rel, text in staged.items():
            _atomic_write(os.path.join(root, rel), text)
            written.append(rel)
    except Exception:
        for rel in written:
            path = os.path.join(root, rel)
            if originals[rel] is None:
                os.remove(path)
            else:
                _atomic_write(path, originals[rel])
        raise


def main(argv=None) -> int:
    """THE regeneration entry point. Writes every output in `REGENERATED_OUTPUTS`, or none.

    The roadmap artifact is produced by the roadmap's own writer, not by a second copy of its
    logic -- `ops.p5_roadmap` remains the authority for its contents. This is only the thing
    that makes sure every output quoting one measurement is written together."""
    import argparse
    from ops import p5_roadmap
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default=CURRENT_RUN)
    ap.add_argument("--root", default=DEV_ROOT,
                    help="repository root to regenerate into; the default is this checkout")
    args = ap.parse_args(argv)
    root = args.root

    try:
        staged = stage(args.run, root)
    except Exception:
        # Staging touched nothing, but the roadmap module read the scratch evidence on its way
        # through. Point it back at what is actually on disk.
        p5_roadmap.refresh(os.path.join(root, "bench", "results"))
        raise
    publish(staged, root)

    for rel in REGENERATED_OUTPUTS:
        print(f"wrote {os.path.join(root, rel)}")
    cap = json.loads(staged[S8_REL])["capacity"]
    print(f"  free_now {cap['free_now_gib']} GiB | minimum {cap['minimum_free_gib']} GiB | "
          f"shortfall {cap['shortfall_gib']} GiB | G18 {cap['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
