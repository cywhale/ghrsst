"""dev2026 — build the P5 generation-1 manifest whose base IS the existing monolith.

A P5 deployment has to start from a manifest that serves exactly what the deployment already
serves. Building generation 1 from the delta would not: `api/app.py` treats a configured
manifest as *the* base and does not fall back to the monolith beside it, so a manifest holding
only the delta tail would drop the rest of the history from the served view under a `/healthz`
that still answered. The schema has the right shape for this — a `legacy_base` segment, with
`boundary_kind: "legacy"` — and this module builds one.

## Everything is derived, nothing is asserted

The day domain, the variables, the `var_valid` semantics, the grid, the layout and both
fingerprints all come from `block_manifest.inspect_store_contract()` reading the monolith on
disk. Nothing is passed in and copied through: a manifest that says what a caller believed is
not a description of a store.

## The path is relative, and the authorization is not in the manifest

`SegmentedCubeStore` refuses an absolute segment path outright, so the segment records the
monolith **relative** to the manifest root — `../mur_timecube_...zarr` for the production
layout, where the monolith is a sibling of `p5_blocks`. Resolving outside the block root is
then allowed only when the DEPLOYMENT names that path in `GHRSST_ALLOWED_LEGACY_PATHS`
(`api.app.resolve_allowed_legacy_paths`). The manifest never authorizes its own external path:
this module writes the reference, and the deployment decides whether it may be followed.

## What this is not

It publishes into a manifest root you give it. It does not choose that root, does not create
P5 roots, does not touch a WAL, and has nothing to do with `ops/p5_prod_provision.py` or with
VM24. Publishing a generation into a live deployment is a production change with its own
approval; this builds the generation.
"""
from __future__ import annotations

import os
from typing import Dict, Optional

from store import block_manifest


class LegacyBaseRefused(Exception):
    """The monolith or the destination cannot support a generation-1 legacy base."""


#: The one segment id a generation-1 legacy base uses. Fixed so a later generation superseding
#: it names something stable rather than whatever the monolith directory happened to be called.
SEGMENT_ID = "legacy_base"


def legacy_base_segment(monolith_path: str, manifest_root: str) -> Dict:
    """The `legacy_base` segment describing `monolith_path`, derived from disk.

    `var_valid_mode` follows what the store actually carries. A monolith predating the P5
    contract has no per-day validity arrays, and the schema allows `implicit_all_true` for
    exactly that case — on a `legacy_base` segment and nowhere else. Claiming `explicit` over a
    store that has none would be a manifest describing a store that does not exist; claiming
    `implicit_all_true` over one that DOES carry them would discard real absence information
    and read every missing variable as present."""
    # `inspect_store_contract` is the day-domain authority and refuses a store with no days,
    # so there is deliberately no second check for it here: a branch that cannot be reached is
    # not a guard, and keeping one invites the belief that something is being checked.
    insp = block_manifest.inspect_store_contract(monolith_path)
    relative = os.path.relpath(os.path.realpath(monolith_path),
                               os.path.realpath(manifest_root))
    if os.path.isabs(relative):
        raise LegacyBaseRefused(
            f"cannot express {monolith_path!r} relative to {manifest_root!r}; "
            f"SegmentedCubeStore refuses an absolute segment path")
    segment = {
        "segment_id": SEGMENT_ID,
        "kind": "legacy_base",
        "path": relative,
        "immutable": True,
        # `legacy`, not `calendar`: the monolith predates the block grid and its bounds are
        # whatever it holds. Declaring `calendar` would invite the block-grid check to compare
        # it against a 90-day span it was never built to.
        "boundary_kind": "legacy",
        "start_day": insp.days[0],
        "end_day": insp.days[-1],
        "materialized_through": insp.days[-1],
        "day_count": len(insp.days),
        "gaps": [], "unknown": [], "day_list": None,
        "layout": block_manifest.segment_layout_from_inspection(insp),
        "variables": sorted(insp.vars),
        "fingerprint": {
            "algo": "sha256",
            "metadata": block_manifest.metadata_fingerprint_from_inspection(insp),
            "day_digest": block_manifest.day_digest(insp.days),
        },
        "precedence": 0,
        "sealed": True,
        "supersedes": None,
    }
    if insp.var_valid_implicit:
        segment["var_valid_mode"] = "implicit_all_true"
    return segment


def build_generation_one(*, monolith_path: str, manifest_root: str, at_utc: str,
                         operator: str, generation_id: Optional[str] = None) -> Dict:
    """Build and publish generation 1 over `monolith_path`. Returns what was published.

    Published through `block_manifest.publish()`, which validates and archives it — there is no
    second manifest format here and no second validator. `publish()` refuses to rewrite an
    existing generation archive, so this cannot quietly replace a generation someone else
    made."""
    if not os.path.isdir(monolith_path):
        raise LegacyBaseRefused(f"monolith_path {monolith_path!r} is not an existing directory")
    if not os.path.isdir(manifest_root):
        raise LegacyBaseRefused(
            f"manifest_root {manifest_root!r} is not an existing directory; this builds a "
            f"generation, it does not create the root that holds it")
    for name, value in (("at_utc", at_utc), ("operator", operator)):
        if type(value) is not str or not value.strip():
            raise LegacyBaseRefused(f"{name} must be a non-empty string")

    segment = legacy_base_segment(monolith_path, manifest_root)
    insp = block_manifest.inspect_store_contract(monolith_path)
    manifest = {
        "format": block_manifest.MANIFEST_FORMAT,
        "version": block_manifest.SCHEMA_VERSION,
        "generation": 1,
        "generation_id": generation_id or f"gen000001-{SEGMENT_ID}",
        "created_utc": at_utc,
        "created_by": f"ops/p5_legacy_base.py ({operator})",
        "predecessor_generation": None, "predecessor_manifest": None,
        # From the inspection, not from a caller: the grid is a property of the store.
        "grid": {"ny": insp.ny, "nx": insp.nx, "region": [0, insp.ny, 0, insp.nx]},
        "variables": sorted(insp.vars),
        "block_grid": {"anchor_day": insp.days[0], "block_days": 90},
        "segments": [segment], "superseded": [], "manifest_checksum": "",
    }
    manifest["manifest_checksum"] = block_manifest.compute_checksum(manifest)
    block_manifest.publish(manifest_root, manifest)
    return {
        "manifest_root": manifest_root,
        "monolith_path": os.path.realpath(monolith_path),
        "segment_path_relative": segment["path"],
        "generation": 1,
        "generation_id": manifest["generation_id"],
        "days": len(insp.days),
        "first_day": insp.days[0], "last_day": insp.days[-1],
        "variables": sorted(insp.vars),
        "var_valid_mode": segment.get("var_valid_mode", "explicit"),
        "fingerprint": dict(segment["fingerprint"]),
        "requires_runtime_allow_list": os.path.realpath(monolith_path),
        "note": ("Generation 1 over the existing monolith. The deployment must name "
                 "`requires_runtime_allow_list` in GHRSST_ALLOWED_LEGACY_PATHS before this "
                 "manifest can be served; the manifest does not authorize its own path."),
    }
