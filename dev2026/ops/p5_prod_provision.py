"""dev2026 — P5 PRODUCTION root provisioning.

`ops/p5_provision.py` cannot do this, and must not be made to. It refuses any stage root inside
the declared live root, and it declares its anchor with `allow_same_filesystem=True` — correct
for a throwaway root on one filesystem, invalid for a deployment whose anchor is supposed to
survive the loss of the volume it witnesses. Adding a bypass flag there would make the
isolation guarantee optional, and an optional guarantee is not one. So production provisioning
is a separate operation with its own scope, its own approval and its own refusals.

## What this does

Creates, under the live data root, exactly two P5 roots — `p5_blocks` (manifest authority) and
`p5_repairs` (repair WAL) — seeds generation 1 from the delta, and initializes the WAL against
an anchor the operator supplies. That is all. It never prunes, swaps, compacts, repairs or
deletes production data, and it holds no opinion about whether a compaction should follow.
Provisioning roots and running a compaction are different operations with different blast
radii, and the runbook sequences them separately.

## What it refuses, before writing anything

Every one of these is a refusal rather than a warning, and none is waivable by a flag:

* no explicit production approval naming this live root and this run;
* a live root that is missing, or not a directory;
* P5 roots that already exist — it will not adopt them, and it will not delete them;
* an anchor inside the live root, on the live root's device, undeclared, or declared with
  `allow_same_filesystem=True`;
* free space below the operator's floor, measured fresh, bound to the device actually holding
  the live root;
* a compaction already running.

## What it does not promise

A process crash between two writes is not covered. The marker is written first so the state is
always identifiably this run's, and `rollback_production()` removes only what this run created —
never the live root, never pre-existing data. Nothing here makes provisioning atomic against
power loss or SIGKILL; recovering from that is a person reading a marker that says
`complete: false`.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
from typing import Dict, Optional, Sequence

from ingest.build_block import build_block
from store import block_manifest
from store import compaction_lock
from store import repair_wal

#: The label a production-provisioned root carries. Distinct from the staging label so a
#: readiness target cannot present one as the other.
PRODUCTION_LABEL = "P5_PRODUCTION_PROVISION"

#: The scope an approval must declare. A record approving something else — an assessor build,
#: a shadow run — is not an approval for writing into the live data root.
PRODUCTION_SCOPE = "p5_production_provisioning"

#: The ONLY two directory names this creates, and it computes them itself. Taking them from the
#: caller would make "which path" a parameter, and the whole containment argument rests on it
#: not being one.
MANIFEST_DIRNAME = "p5_blocks"
WAL_DIRNAME = "p5_repairs"

#: Ownership marker, in the live root, named for the run. Two runs cannot collide, and a
#: rollback names the run it is rolling back.
MARKER_PREFIX = ".p5_prod_provision"


class ProductionProvisionRefused(Exception):
    """A production provisioning request that does not meet its preconditions."""


class ProductionApprovalRefused(ProductionProvisionRefused):
    """The approval is absent, malformed, or is not for this root and this run."""


class ProductionCleanupError(Exception):
    """Provisioning failed AND the partial state could not be removed. Both are reported."""


def _inside(path: str, root: str) -> bool:
    """Is `path` inside `root`? By path components, never by string prefix.

    `/data/ghrsst-stage` is not inside `/data/ghrsst`, and a `startswith` test says it is."""
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def _exact_str(value) -> bool:
    """A non-empty `str`, by exact type. `True` is an `int`; neither is a name."""
    return type(value) is str and bool(value.strip())


def marker_name(run_id: str) -> str:
    return f"{MARKER_PREFIX}_{run_id}.json"


# ---------------------------------------------------------------- preconditions
def check_production_approval(approval, *, live_root: str, run_id: str) -> Dict:
    """The approval, validated. Returns it; raises `ProductionApprovalRefused`.

    Writing into the live data root is the one thing this module does that cannot be undone by
    deleting a directory — the manifest it publishes becomes the store's authority. So the
    approval is checked field by field, against THIS root and THIS run, rather than for mere
    presence. An approval for a different root is an approval for a different act."""
    if type(approval) is not dict:
        raise ProductionApprovalRefused(
            f"no production approval was supplied (got {type(approval).__name__}); writing P5 "
            f"roots into the live data root is a production change and needs one")
    for field in ("scope", "approved_by", "approved_utc", "live_root", "run_id"):
        if field not in approval:
            raise ProductionApprovalRefused(
                f"the production approval records no {field}; an approval missing that names "
                f"nothing checkable")
        if not _exact_str(approval[field]):
            raise ProductionApprovalRefused(
                f"the production approval's {field} is {approval[field]!r}, which is not a "
                f"non-empty string")
    if approval["scope"] != PRODUCTION_SCOPE:
        raise ProductionApprovalRefused(
            f"the approval declares scope {approval['scope']!r}; this operation needs "
            f"{PRODUCTION_SCOPE!r}. An approval for one act is not an approval for another")
    if os.path.realpath(approval["live_root"]) != os.path.realpath(live_root):
        raise ProductionApprovalRefused(
            f"the approval is for live root {approval['live_root']!r}, not {live_root!r}. The "
            f"root being written to is the thing being approved")
    if approval["run_id"] != run_id:
        raise ProductionApprovalRefused(
            f"the approval is for run {approval['run_id']!r}, not {run_id!r}. A re-usable "
            f"approval is a standing permission to write into production")
    return dict(approval)


def check_anchor_domain(anchor_root: str, live_root: str) -> Dict:
    """The anchor, validated against the live root. Returns what was verified.

    `resolve_anchor_root()` enforces most of this at WAL time, and accepts an
    `allow_same_filesystem=True` declaration as an auditable waiver. That waiver is correct for
    a throwaway staging root and wrong here: the anchor is the only thing that can notice the
    live volume being rolled back underneath the WAL, and one that shares the volume is
    restored with it. So production refuses the waiver outright, before any write, rather than
    discovering it inside `initialize_wal`."""
    if not _exact_str(anchor_root):
        raise ProductionProvisionRefused(
            "no anchor_root was supplied. Production provisioning will not choose one: the "
            "anchor is a statement about storage nobody in this process can verify")
    if not os.path.isdir(anchor_root):
        raise ProductionProvisionRefused(
            f"anchor_root {anchor_root!r} is not an existing directory. It must be storage ops "
            f"have already provisioned, not something this run creates for itself")
    if _inside(anchor_root, live_root):
        raise ProductionProvisionRefused(
            f"anchor_root {anchor_root!r} is inside the live root {live_root!r}. A witness "
            f"stored in the domain it witnesses is lost with it")
    doc = repair_wal._anchor_domain_doc(os.path.realpath(anchor_root))
    if doc is None:
        raise ProductionProvisionRefused(
            f"anchor_root {anchor_root!r} declares no rollback domain. Run "
            f"declare_anchor_domain() against storage ops have confirmed is outside this "
            f"host's snapshot scope")
    if doc.get("allow_same_filesystem") is not False:
        raise ProductionProvisionRefused(
            f"anchor_root {anchor_root!r} is declared with allow_same_filesystem="
            f"{doc.get('allow_same_filesystem')!r}. That waiver is for an isolated staging "
            f"root; a deployment anchor must be on storage that survives the live volume")
    try:
        anchor_dev = os.stat(anchor_root).st_dev
        live_dev = os.stat(live_root).st_dev
    except OSError as exc:
        raise ProductionProvisionRefused(
            f"cannot compare the anchor and live devices ({exc}). An unanswerable question is "
            f"not a passing answer") from exc
    if anchor_dev == live_dev:
        raise ProductionProvisionRefused(
            f"anchor_root {anchor_root!r} is on the same device as the live root "
            f"({anchor_dev}). A different st_dev still would not prove a different backup "
            f"domain, but the same one rules it out")
    return {"anchor_root": os.path.realpath(anchor_root),
            "domain_id": doc.get("domain_id"), "anchor_dev": anchor_dev,
            "live_dev": live_dev, "allow_same_filesystem": False}


def measure_capacity(live_root: str, *, min_free_gib: float) -> Dict:
    """Free space and device, measured NOW against the root about to be written.

    Bound to the device actually holding `live_root`, not to a figure someone passed in: a
    roomy unrelated volume produced a passing number while the volume that mattered was full."""
    if type(min_free_gib) is bool or not isinstance(min_free_gib, (int, float)):
        raise ProductionProvisionRefused(
            f"min_free_gib must be a number, got {type(min_free_gib).__name__}")
    st = os.statvfs(live_root)
    free_gib = st.f_bavail * st.f_frsize / (1024 ** 3)
    binding = {"data_root": os.path.realpath(live_root),
               "filesystem_dev": os.stat(live_root).st_dev,
               "free_now_gib": round(free_gib, 1),
               "min_free_gib": float(min_free_gib),
               "inodes_free": st.f_favail}
    if free_gib < min_free_gib:
        raise ProductionProvisionRefused(
            f"free space on {live_root!r} is {free_gib:.1f} GiB, below the required "
            f"{float(min_free_gib):.1f} GiB. A build that runs out part-way leaves exactly the "
            f"partial state this refuses to create")
    return binding


def _refuse_existing_roots(manifest_root: str, wal_root: str) -> None:
    """Never adopt, never delete. Both are the same refusal from opposite directions."""
    for path in (manifest_root, wal_root):
        if os.path.exists(path):
            raise ProductionProvisionRefused(
                f"refusing to provision: {path!r} already exists. This operation will not "
                f"adopt it — rollback removes what this run created, and adopting would hand "
                f"that authority over data it did not create — and it will not delete it, "
                f"because deleting production state is not a provisioning step")


# ---------------------------------------------------------------- the operation
def provision_production(*, live_root: str, anchor_root: str, approval: Dict,
                         compaction_lock_path: str, ingest_lock_path: str,
                         delta_path: str, start_day: str, end_day: str,
                         days: Sequence[str], grid: Dict, run_id: str, operator: str,
                         at_utc: str, min_free_gib: float,
                         block_name: str = "block_v1.zarr",
                         domain_id: Optional[str] = None,
                         cleanup_on_failure: bool = True) -> Dict:
    """Create the P5 roots under `live_root` and seed generation 1. Returns what was made.

    Lock ordering follows the runbook's normative sequence: the compaction lock is taken FIRST
    and held across the whole operation, and the ingest lock is taken only for the publish
    critical section and released before the compaction lock. Publishing is the moment the
    store's authority changes, and it is the only moment ingest must be excluded from."""
    approval = check_production_approval(approval, live_root=live_root, run_id=run_id)
    if not os.path.isdir(live_root):
        raise ProductionProvisionRefused(
            f"live_root {live_root!r} is not an existing directory; production provisioning "
            f"does not create the data root it writes into")
    for name, value in (("run_id", run_id), ("operator", operator), ("at_utc", at_utc)):
        if not _exact_str(value):
            raise ProductionProvisionRefused(f"{name} must be a non-empty string")

    # Computed here, never taken from the caller: containment is only an argument if the path
    # cannot be pointed elsewhere.
    manifest_root = os.path.join(live_root, MANIFEST_DIRNAME)
    wal_root = os.path.join(live_root, WAL_DIRNAME)
    for path in (manifest_root, wal_root):
        if not _inside(path, live_root):
            raise ProductionProvisionRefused(
                f"{path!r} did not resolve inside {live_root!r}; refusing rather than writing "
                f"somewhere the containment check does not cover")
    _refuse_existing_roots(manifest_root, wal_root)
    anchor = check_anchor_domain(anchor_root, live_root)
    capacity = measure_capacity(live_root, min_free_gib=min_free_gib)

    busy = compaction_lock.refuse_if_compaction_running(
        compaction_lock_path, operation="p5 production provisioning")
    if busy:
        raise ProductionProvisionRefused(
            f"a compaction is running ({busy}); provisioning the roots underneath it would "
            f"change the authority it is building against")

    marker_path = os.path.join(live_root, marker_name(run_id))
    marker = f"{PRODUCTION_LABEL}:{run_id}:{at_utc}:{operator}"
    stamp = {"label": PRODUCTION_LABEL, "marker": marker, "run_id": run_id,
             "live_root": os.path.realpath(live_root), "manifest_root": manifest_root,
             "wal_root": wal_root, "anchor_root": anchor["anchor_root"],
             "provisioned_utc": at_utc, "operator": operator,
             "approved_by": approval["approved_by"], "approved_utc": approval["approved_utc"],
             "capacity_at_start": capacity, "anchor_verified": anchor,
             "created_paths": [], "complete": False, "failed_step": None}

    # Ownership before the first thing worth owning. The marker is itself a write and can fail;
    # if it does, nothing has been created and there is nothing to own.
    _write_marker(marker_path, stamp)
    created: list = []
    try:
        result = _provision_production_contents(
            live_root=live_root, manifest_root=manifest_root, wal_root=wal_root,
            anchor=anchor, capacity=capacity, delta_path=delta_path, start_day=start_day,
            end_day=end_day, days=days, grid=grid, run_id=run_id, operator=operator,
            at_utc=at_utc, block_name=block_name, domain_id=domain_id,
            compaction_lock_path=compaction_lock_path, ingest_lock_path=ingest_lock_path,
            approval=approval, marker=marker, created=created)
    except BaseException as failure:
        partial = {**stamp, "created_paths": list(created), "failed_step": repr(failure)}
        try:
            _write_marker(marker_path, partial)
        except Exception:                                     # noqa: BLE001
            _discard(created, marker_path)
            raise failure
        if not cleanup_on_failure:
            raise
        try:
            rollback_production(partial)
        except Exception as cleanup_failure:
            raise ProductionCleanupError(
                f"production provisioning failed ({failure!r}) and the partial state could not "
                f"be removed ({cleanup_failure!r}). It is marked as this run's and incomplete: "
                f"roll it back once the cause is understood. Nothing pre-existing was touched."
            ) from failure
        raise
    result["created_paths"] = list(created)
    _write_marker(marker_path, {**stamp, "created_paths": list(created), "complete": True,
                                "generation": result.get("generation")})
    result["marker_path"] = marker_path
    return result


def _provision_production_contents(*, live_root, manifest_root, wal_root, anchor, capacity,
                                   delta_path, start_day, end_day, days, grid, run_id,
                                   operator, at_utc, block_name, domain_id,
                                   compaction_lock_path, ingest_lock_path, approval, marker,
                                   created) -> Dict:
    """Everything after ownership is established. Any failure here is the caller's to clean up.

    Runbook §7.1b ordering:

        flock(p5_compaction.lock, LOCK_EX)  ->  create roots  ->  build
           ->  acquire ingest lock  ->  assert compaction lock still held
           ->  publish  ->  release ingest lock  ->  initialize WAL
           ->  release compaction lock
    """
    holder = f"p5_prod_provision/{run_id}"
    with compaction_lock.CompactionLock(compaction_lock_path, holder=holder) as clock:
        # Re-measured INSIDE the lock. The device could have been remounted between the
        # preflight and here, and a binding taken before the lock is a binding to a different
        # moment than the one being written in.
        now_dev = os.stat(live_root).st_dev
        if now_dev != capacity["filesystem_dev"]:
            raise ProductionProvisionRefused(
                f"the live root moved device between preflight ({capacity['filesystem_dev']}) "
                f"and the lock ({now_dev}); the capacity reading is about a different volume")

        for path in (manifest_root, wal_root):
            os.makedirs(path)                       # no exist_ok: refuse a racing creator
            created.append(path)

        artifacts = os.path.join(manifest_root, "artifacts")
        os.makedirs(artifacts, exist_ok=True)
        block_path = os.path.join(manifest_root, block_name)
        plan = build_block(block_path, start_day=start_day, end_day=end_day,
                           classification_target=list(days), delta_path=delta_path,
                           artifacts_dir=artifacts, lock=clock,
                           hard_reserve_bytes=int(capacity["min_free_gib"] * (1024 ** 3)))

        manifest = _seed_manifest(plan, grid=grid, at_utc=at_utc, operator=operator,
                                  run_id=run_id, start_day=start_day, end_day=end_day)
        # The publish critical section, and the only part ingest must be excluded from.
        with compaction_lock.CompactionLock(ingest_lock_path,
                                            holder=f"{holder}/publish") as ingest:
            clock.assert_still_held()
            block_manifest.publish(manifest_root, manifest)
            ingest.release()

        # After the publish, still under the compaction lock: the WAL binds to the manifest
        # authority that now exists.
        repair_wal.initialize_wal(wal_root, manifest_root=manifest_root, at_utc=at_utc,
                                  operator=operator, anchor_root=anchor["anchor_root"])
        clock.assert_still_held()

    return {
        "label": PRODUCTION_LABEL, "is_production_state": True,
        "run_id": run_id, "live_root": os.path.realpath(live_root),
        "manifest_root": manifest_root, "wal_root": wal_root,
        "anchor_root": anchor["anchor_root"], "anchor_domain_id": anchor["domain_id"],
        "block_path": block_path, "artifacts": artifacts,
        "generation": manifest["generation"],
        "locks": {"compaction": compaction_lock_path, "ingest": ingest_lock_path,
                  "order": "compaction outermost; ingest held only across publish"},
        "capacity_at_start": capacity, "anchor_verified": anchor,
        "approved_by": approval["approved_by"], "approved_utc": approval["approved_utc"],
        "provisioned_utc": at_utc, "operator": operator, "marker": marker,
        "days": list(days), "start_day": start_day, "end_day": end_day,
        "complete": True, "failed_step": None,
        "note": ("P5 roots and generation 1 only. This performs no compaction, prune, swap, "
                 "repair or deletion, and makes no claim that one is safe to run."),
    }


def _seed_manifest(plan: Dict, *, grid: Dict, at_utc: str, operator: str, run_id: str,
                   start_day: str, end_day: str) -> Dict:
    """Generation 1, built through `store.block_manifest` — never a second manifest format."""
    segment = plan["segment"]
    manifest = {
        "format": block_manifest.MANIFEST_FORMAT,
        "version": block_manifest.SCHEMA_VERSION,
        "generation": 1, "generation_id": f"gen000001-{run_id}",
        "created_utc": at_utc,
        "created_by": f"ops/p5_prod_provision.py ({operator})",
        "predecessor_generation": None, "predecessor_manifest": None,
        "grid": {"ny": grid["ny"], "nx": grid["nx"],
                 "region": grid.get("region", [0, grid["ny"], 0, grid["nx"]])},
        "variables": list(segment.get("variables") or ()) or None,
        "block_grid": segment.get("block_grid") or {
            "anchor_day": start_day,
            "block_days": len(block_manifest.calendar_span(start_day, end_day))},
        "segments": [dict(segment)], "superseded": [], "manifest_checksum": "",
    }
    if manifest["variables"] is None:
        from ingest.build_block import VARS
        manifest["variables"] = list(VARS)
    manifest["manifest_checksum"] = block_manifest.compute_checksum(manifest)
    return manifest


def _write_marker(path: str, payload: Dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _discard(created: Sequence[str], marker_path: str) -> None:
    """Best effort removal of exactly what was created, when the marker itself cannot be kept."""
    for path in reversed(list(created)):
        shutil.rmtree(path, ignore_errors=True)
    try:
        os.remove(marker_path)
    except OSError:
        pass


def rollback_production(provisioned: Dict) -> None:
    """Remove exactly what this run created under the live root, and nothing else.

    The only call here that deletes, so the most suspicious of its argument. It removes the
    paths the marker RECORDS having created — not the live root, not `p5_blocks` because it is
    called `p5_blocks`, and never a path this run did not make. A run that created nothing
    removes nothing."""
    live_root = provisioned.get("live_root")
    run_id = provisioned.get("run_id")
    if not (_exact_str(live_root) and _exact_str(run_id)):
        raise ProductionProvisionRefused(
            "rollback needs the live_root and run_id the marker was written for")
    marker_path = os.path.join(live_root, marker_name(run_id))
    if not os.path.isfile(marker_path):
        raise ProductionProvisionRefused(
            f"refusing to remove anything under {live_root!r}: there is no "
            f"{marker_name(run_id)}, so this run did not create what is there")
    with open(marker_path) as fh:
        recorded = json.load(fh)
    if recorded.get("marker") != provisioned.get("marker"):
        raise ProductionProvisionRefused(
            f"refusing to roll back: the marker on disk ({recorded.get('marker')!r}) is not "
            f"the one being rolled back ({provisioned.get('marker')!r})")
    if os.path.realpath(recorded.get("live_root") or "") != os.path.realpath(live_root):
        raise ProductionProvisionRefused(
            f"refusing to roll back: the marker records live_root "
            f"{recorded.get('live_root')!r}, not {live_root!r}")
    for path in reversed(list(recorded.get("created_paths") or [])):
        if not _inside(path, live_root):
            raise ProductionProvisionRefused(
                f"refusing to remove {path!r}: the marker records it as created but it is not "
                f"inside {live_root!r}")
        if os.path.realpath(path) == os.path.realpath(live_root):
            raise ProductionProvisionRefused(
                f"refusing to remove the live root itself ({path!r})")
        shutil.rmtree(path, ignore_errors=True)
    os.remove(marker_path)
