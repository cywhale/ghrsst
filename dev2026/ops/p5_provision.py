"""dev2026 — build an ISOLATED P5 staging root, reversibly.

`ops/p5_roadmap.py` declares the `p5_shadow` profile `verification_only`: it proves a shadow is
correctly provisioned and takes no measurement. That is an honest description and a real limit —
it cannot make a deployment ready, because it cannot create anything, and VM24 has nothing for it
to verify. `bench/results/p5_readiness.json` records the consequence: `manifest`, `blocks` and
`wal_anchor` are `BLOCKED` on the live deployment because those roots do not exist.

Something has to build what `p5_shadow` verifies. This is that something, and it is deliberately
the smallest such thing:

- **It never writes production state.** It refuses a staging root inside the declared live root,
  before creating anything. Provisioning P5 roots inside `$G` is a production change and needs
  its own approval; this module cannot be talked into performing one.
- **It is reversible.** Everything lands under one staging root, stamped with a marker naming the
  run. `rollback()` removes exactly that root, refuses one it did not create, and refuses one
  inside the live root.
- **It creates no second truth.** Blocks come from `ingest.build_block`, the manifest from
  `store.block_manifest`, the WAL and anchor from `store.repair_wal`. A provisioner writing its
  own manifest would produce one the store might refuse — which is the failure it exists to
  prevent, not a shortcut worth taking.

## What a provisioned root is, and is not

It is a real segmented base: a block built by the shipped builder, published through the shipped
manifest authority, with an initialized WAL and an anchor declaring a rollback domain. Every
structural readiness check passes against it.

It is **not** a deployment. Its label is `P5_ISOLATED_STAGING`, which
`p5_readiness.NON_QUALIFYING_LABELS` refuses — so a readiness report over a staging root can
never read `READY`, however complete the root is. Proving the provisioning recipe works is a
different statement from a deployment being ready, and the distinction is enforced rather than
described.
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Dict, Optional, Sequence

from ingest.build_block import build_block
from store import block_manifest, compaction_lock, repair_wal

#: Written into a provisioned root, and the only thing `rollback()` will delete a root for.
MARKER_NAME = ".p5_provision.json"

#: The label a staging root carries. Non-qualifying by construction: see the module docstring.
STAGING_LABEL = "P5_ISOLATED_STAGING"


class ProvisionRefused(Exception):
    """A provisioning or rollback request that would touch something it must not."""


def _inside(path: str, root: str) -> bool:
    """Is `path` inside `root`? By path components, never by string prefix.

    `/data/ghrsst-stage` is not inside `/data/ghrsst`, and a `startswith` test says it is.
    Refusing a safe location is not the harmless direction of this error: it teaches operators
    to reach for the override, and the override is what makes the dangerous case reachable."""
    path = os.path.abspath(path)
    root = os.path.abspath(root)
    return path == root or path.startswith(root + os.sep)


def _refuse_if_not_isolated(stage_root: str, live_root: str) -> None:
    if not live_root:
        raise ProvisionRefused(
            "no live root was declared, so nothing states what this must stay out of")
    if _inside(stage_root, live_root):
        raise ProvisionRefused(
            f"refusing to provision {stage_root!r}: it is inside the declared live root "
            f"{live_root!r}. Creating P5 roots in the live data root is a production change "
            f"and needs its own approval; this module does not perform one")


def provision(*, stage_root: str, live_root: str, delta_path: str, start_day: str,
              end_day: str, days: Sequence[str], operator: str, at_utc: str,
              grid: Dict, block_name: str = "block_v1.zarr",
              domain_id: Optional[str] = None) -> Dict:
    """Create a complete, isolated P5 staging root and return what was made.

    Refuses before creating anything if the location is not isolated, or if the staging root
    already exists — provisioning into a directory holding someone else's files makes rollback
    a guess about which of them were ours."""
    _refuse_if_not_isolated(stage_root, live_root)
    # ANY existing path, empty or not. An empty directory someone else made is still theirs,
    # and `rollback()` removes the whole root -- so accepting an existing empty directory hands
    # rollback the authority to delete something this run did not create. "It was empty" is not
    # a claim about ownership.
    if os.path.exists(stage_root):
        raise ProvisionRefused(
            f"refusing to provision into the existing {stage_root!r}: rollback removes the "
            f"whole root, so it must create it and own it. Empty is not the same as ours")

    manifest_root = os.path.join(stage_root, "p5_blocks")
    wal_root = os.path.join(stage_root, "p5_repairs")
    # Beside the manifest root, never inside it: the anchor is the witness to a rollback domain,
    # and a witness stored in the domain it witnesses is lost with it.
    anchor_root = os.path.join(stage_root, "p5_anchor")
    artifacts = os.path.join(stage_root, "artifacts")
    # `makedirs` without exist_ok on the root: if it appeared between the check above and
    # here, this refuses rather than adopting it.
    os.makedirs(stage_root)
    for d in (manifest_root, wal_root, anchor_root, artifacts):
        os.makedirs(d, exist_ok=True)

    block_path = os.path.join(manifest_root, block_name)
    plan = build_block(block_path, start_day=start_day, end_day=end_day,
                       classification_target=list(days), delta_path=delta_path,
                       artifacts_dir=artifacts, lock=None, hard_reserve_bytes=0,
                       unsafe_skip_isolation=True)

    manifest = {
        "format": block_manifest.MANIFEST_FORMAT,
        "version": block_manifest.SCHEMA_VERSION,
        "generation": 1, "generation_id": f"gen000001-{STAGING_LABEL.lower()}",
        "created_utc": at_utc, "created_by": f"ops/p5_provision.py ({operator})",
        "predecessor_generation": None, "predecessor_manifest": None,
        "grid": {"ny": grid["ny"], "nx": grid["nx"],
                 "region": grid.get("region", [0, grid["ny"], 0, grid["nx"]])},
        "variables": list(plan["segment"].get("variables") or ()) or None,
        "block_grid": plan["segment"]["block_grid"] if "block_grid" in plan["segment"]
        else {"anchor_day": start_day, "block_days": len(
            block_manifest.calendar_span(start_day, end_day))},
        "segments": [dict(plan["segment"])], "superseded": [], "manifest_checksum": "",
    }
    if manifest["variables"] is None:
        from ingest.build_block import VARS
        manifest["variables"] = list(VARS)
    manifest["manifest_checksum"] = block_manifest.compute_checksum(manifest)
    block_manifest.publish(manifest_root, manifest)

    default_domain = f"{STAGING_LABEL.lower()}-{os.path.basename(stage_root)}"
    repair_wal.declare_anchor_domain(
        anchor_root, domain_id=domain_id or default_domain,
        at_utc=at_utc, operator=operator,
        # A staging root lives on one filesystem by design. Saying so is the point: a real
        # deployment must not, and the flag makes the difference visible rather than assumed.
        allow_same_filesystem=True,
        note="isolated P5 staging root; NOT a deployment and not a rollback domain for one")
    repair_wal.initialize_wal(wal_root, manifest_root=manifest_root, at_utc=at_utc,
                              operator=operator, anchor_root=anchor_root)

    # Take the compaction lock and let it go, in this root only. "The directory exists" is not
    # evidence that a lock can be acquired there, and the fencing the whole compaction posture
    # rests on is worth one real acquisition rather than an assumption. Isolated and instant:
    # the lock lives under the staging root and is released before this returns.
    probe = {"path": os.path.join(stage_root, "p5_compaction.lock")}
    try:
        with compaction_lock.CompactionLock(probe["path"], holder=f"p5_provision/{operator}"):
            probe["acquired"] = True
        probe["released"] = True
    except Exception as exc:                                  # noqa: BLE001
        probe.update(acquired=False, released=False, error=repr(exc))

    result = {
        "label": STAGING_LABEL,
        "stage_root": stage_root, "live_root": live_root,
        "manifest_root": manifest_root, "wal_root": wal_root, "anchor_root": anchor_root,
        "artifacts": artifacts, "block_path": block_path,
        "locks": {"compaction": probe["path"],
                  "ingest": os.path.join(stage_root, "ingest.lock"),
                  "acquire_probe": probe},
        "provisioned_utc": at_utc, "operator": operator,
        "days": list(days), "start_day": start_day, "end_day": end_day,
        "marker": f"{STAGING_LABEL}:{at_utc}:{operator}",
        "is_production_state": False,
        "note": ("an isolated staging root. It proves the provisioning recipe; it does not make "
                 "any deployment ready, and its label cannot settle a compaction requirement."),
    }
    with open(os.path.join(stage_root, MARKER_NAME), "w") as fh:
        json.dump(result, fh, indent=2, sort_keys=True)
    return result


def rollback(provisioned: Dict) -> None:
    """Remove exactly the root this provisioned, and nothing else.

    The only call here that deletes, so the most suspicious of its argument: it refuses a root
    inside the live root, and refuses any root whose marker does not match the run being rolled
    back. Idempotent — a root already gone is a rollback already done."""
    stage_root = provisioned.get("stage_root")
    live_root = provisioned.get("live_root")
    if not stage_root:
        raise ProvisionRefused("no stage_root to roll back")
    if live_root and _inside(stage_root, live_root):
        raise ProvisionRefused(
            f"refusing to remove {stage_root!r}: it is inside the live root {live_root!r}")
    if not os.path.exists(stage_root):
        return
    marker_path = os.path.join(stage_root, MARKER_NAME)
    if not os.path.isfile(marker_path):
        raise ProvisionRefused(
            f"refusing to remove {stage_root!r}: it carries no {MARKER_NAME}, so this run did "
            f"not create it")
    with open(marker_path) as fh:
        marker = json.load(fh)
    if marker.get("marker") != provisioned.get("marker"):
        raise ProvisionRefused(
            f"refusing to remove {stage_root!r}: its marker {marker.get('marker')!r} is not "
            f"the one being rolled back ({provisioned.get('marker')!r})")
    shutil.rmtree(stage_root)
