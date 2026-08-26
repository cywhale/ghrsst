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
import stat
from typing import Dict, Optional

from store import block_manifest


class LegacyBaseRefused(Exception):
    """The monolith or the destination cannot support a generation-1 legacy base."""


#: The one segment id a generation-1 legacy base uses. Fixed so a later generation superseding
#: it names something stable rather than whatever the monolith directory happened to be called.
SEGMENT_ID = "legacy_base"

#: S -- the block size in calendar days for the grid this generation fixes.
#:
#: **Policy, not derived.** There is no canonical S anywhere in the code to derive it from:
#: the design carries `S = 90 / C = 30` as prose and the generated blockers table says in as
#: many words that it is "provisional only; nothing has selected it". So it is declared here,
#: once, named as policy, and read by everything that needs it -- rather than appearing as a
#: literal `90` in a manifest builder where it would look like a measurement.
#:
#: Changing it changes the calendar grid every future block is cut on. A test binds the
#: generated manifest to this constant so the two cannot drift.
BLOCK_DAYS_S = 90

#: And it is still provisional. Nothing in P5 has selected 90 over any other value; S6 was to
#: validate it and has not. Recorded so a reader of a generated manifest does not mistake the
#: number for a settled one.
BLOCK_DAYS_S_IS_PROVISIONAL = True


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
        # DERIVED, per design §12 M0: "gaps as calendar[start..end] - present". Writing `[]`
        # would claim a contiguous span the store may not have, and nothing downstream would
        # catch it -- the day digest is over the days that ARE there, so it matches either way.
        "gaps": sorted(set(block_manifest.calendar_span(insp.days[0], insp.days[-1]))
                       - set(insp.days)),
        "unknown": [], "day_list": None,
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
        # The barrier the fingerprints above do not provide. See
        # `block_manifest.content_ledger`: change detection over file metadata, not a
        # cryptographic seal, and the thing that makes an in-place value rewrite refuse.
        "content_seal": block_manifest.content_ledger(monolith_path),
    }
    if insp.var_valid_implicit:
        segment["var_valid_mode"] = "implicit_all_true"
    return segment


def check_monolith_sealed(monolith_path: str) -> Dict:
    """Refuse a monolith that is still writable. Returns what was observed.

    **A necessary barrier, not a sufficient one.** It reads the store root's permission bits:
    if the owner, group or world can write it, the store is not sealed and nothing downstream
    can assume it will hold still. What it cannot establish is the converse — a subdirectory
    could carry different bits, a process running as root ignores permissions entirely, and a
    filesystem remounted read-write later changes the answer without changing this record.

    The real barrier is an operational one: the monolith on a read-only mount, which is a
    deployment prerequisite and not something this process can arrange. This refuses the
    obviously-unsealed case and records the evidence for the case it cannot settle.

    `immutable: true` in the segment is a DECLARATION and is not treated as proof of anything;
    this is the check that stands behind it."""
    if not os.path.isdir(monolith_path):
        raise LegacyBaseRefused(
            f"monolith_path {monolith_path!r} is not an existing directory")
    st = os.stat(monolith_path)
    writable = bool(st.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    evidence = {
        "path": os.path.realpath(monolith_path),
        "mode": oct(stat.S_IMODE(st.st_mode)),
        "writable_bits": writable,
        "checked": "store root permission bits only",
        "not_established": ("subdirectory bits, a root process ignoring permissions, or a "
                            "later remount read-write. The barrier that would establish those "
                            "is a read-only mount, which is a deployment prerequisite"),
    }
    if writable:
        raise LegacyBaseRefused(
            f"{monolith_path!r} is writable (mode {evidence['mode']}); a legacy base must be "
            f"sealed before a manifest describes it. The content seal detects a store that "
            f"changed, and detecting it after the fact is not the same as it not changing")
    return evidence


def prepare_generation_one(*, monolith_path: str, manifest_root: str, at_utc: str,
                           operator: str, generation_id: Optional[str] = None,
                           require_sealed: bool = True) -> Dict:
    """Everything expensive, done OUTSIDE any lock the ingest path needs.

    `inspect_store_contract()` reads the store's metadata and `content_ledger()` walks every
    file in it. On a ~2 TB monolith the second is minutes of `stat` calls, and it was running
    inside the ingest lock — which on VM24 means the daily-append cron blocked behind a scan
    that has nothing to do with it. The ingest lock exists to exclude ingest from the moment
    the store's authority CHANGES, and that moment is the publish.

    So this prepares; `publish_prepared()` publishes. Between the two the monolith could in
    principle change, which is what `check_monolith_sealed()` and the final validation in
    `publish_prepared()` are for."""
    seal_evidence = check_monolith_sealed(monolith_path) if require_sealed else None
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
        # The provenance line says WHERE block_days came from, because the manifest schema has
        # no field for "this number is policy" and a bare 90 in `block_grid` reads like a
        # measurement of the store.
        "created_by": (f"ops/p5_legacy_base.py ({operator}); "
                       f"block_days={BLOCK_DAYS_S} is POLICY (BLOCK_DAYS_S), not derived from "
                       f"the store"
                       + (", and PROVISIONAL: nothing has selected it"
                          if BLOCK_DAYS_S_IS_PROVISIONAL else "")),
        "predecessor_generation": None, "predecessor_manifest": None,
        # From the inspection, not from a caller: the grid is a property of the store.
        "grid": {"ny": insp.ny, "nx": insp.nx, "region": [0, insp.ny, 0, insp.nx]},
        "variables": sorted(insp.vars),
        # §12 M0: the anchor is the day AFTER the audited end_day -- the calendar grid starts
        # where the legacy base stops, so the first future block abuts it instead of
        # overlapping it. `block_days` is policy (BLOCK_DAYS_S), not an observation.
        "block_grid": {"anchor_day": block_manifest.add_days(insp.days[-1], 1),
                       "block_days": BLOCK_DAYS_S},
        "segments": [segment], "superseded": [], "manifest_checksum": "",
    }
    manifest["manifest_checksum"] = block_manifest.compute_checksum(manifest)
    return {
        "manifest": manifest,
        "segment": segment,
        "manifest_root": manifest_root,
        "monolith_path": os.path.realpath(monolith_path),
        "seal_evidence": seal_evidence,
        "require_sealed": require_sealed,
        # What the cheap final validation re-checks. Deliberately NOT the content ledger: that
        # is the expensive walk this refactor exists to keep out of the ingest lock.
        "recheck": {
            "metadata": segment["fingerprint"]["metadata"],
            "day_digest": segment["fingerprint"]["day_digest"],
            "layout": segment["layout"],
        },
        "days": len(insp.days),
        "first_day": insp.days[0], "last_day": insp.days[-1],
        "variables": sorted(insp.vars),
        "var_valid_mode": segment.get("var_valid_mode", "explicit"),
        "gaps": list(segment["gaps"]),
        "block_grid": dict(manifest["block_grid"]),
        "block_days_is_policy": True,
        "block_days_is_provisional": BLOCK_DAYS_S_IS_PROVISIONAL,
        "content_seal": dict(segment["content_seal"]),
        "fingerprint": dict(segment["fingerprint"]),
        "generation": 1,
        "generation_id": manifest["generation_id"],
        "segment_path_relative": segment["path"],
        "requires_runtime_allow_list": os.path.realpath(monolith_path),
    }


def validate_prepared(prepared: Dict) -> None:
    """The final check, immediately before the publish. CHEAP by construction.

    Between `prepare_generation_one()` and here the monolith could in principle have changed,
    so something has to look again — but re-walking every file would put the expensive scan
    back inside the ingest lock, which is the whole defect being fixed. So this re-reads the
    store's METADATA, which is O(arrays) rather than O(files), and re-checks the seal barrier.

    What it therefore does not re-verify is the content ledger. That gap is closed by the
    monolith being sealed, not by this function, and `check_monolith_sealed()` is what refuses
    a store that is not."""
    monolith = prepared["monolith_path"]
    if prepared.get("require_sealed"):
        now = check_monolith_sealed(monolith)
        was = prepared.get("seal_evidence") or {}
        if now["mode"] != was.get("mode"):
            raise LegacyBaseRefused(
                f"{monolith!r} changed permissions between preparation ({was.get('mode')}) and "
                f"publish ({now['mode']}); a store that became writable while a manifest was "
                f"being prepared for it is not the store that was prepared")
    insp = block_manifest.inspect_store_contract(monolith)
    recheck = prepared["recheck"]
    now_meta = block_manifest.metadata_fingerprint_from_inspection(insp)
    if now_meta != recheck["metadata"]:
        raise LegacyBaseRefused(
            f"{monolith!r} metadata changed between preparation and publish "
            f"({recheck['metadata'][:12]}\u2026 -> {now_meta[:12]}\u2026); the manifest "
            f"describes a store that no longer exists in that shape")
    now_days = block_manifest.day_digest(insp.days)
    if now_days != recheck["day_digest"]:
        raise LegacyBaseRefused(
            f"{monolith!r} day set changed between preparation and publish; the manifest "
            f"describes a different day domain from the one on disk")
    now_layout = block_manifest.segment_layout_from_inspection(insp)
    if now_layout != recheck["layout"]:
        raise LegacyBaseRefused(
            f"{monolith!r} layout changed between preparation and publish")


def publish_prepared(prepared: Dict) -> Dict:
    """Validate once more, then publish. This is ALL that belongs inside the ingest lock.

    `block_manifest.publish()` validates the manifest and archives the generation, and refuses
    to rewrite an existing archive — so this cannot quietly replace a generation someone else
    made."""
    validate_prepared(prepared)
    block_manifest.publish(prepared["manifest_root"], prepared["manifest"])
    out = {k: v for k, v in prepared.items() if k not in ("manifest", "segment", "recheck")}
    out["note"] = ("Generation 1 over the existing monolith. The deployment must name "
                   "`requires_runtime_allow_list` in GHRSST_ALLOWED_LEGACY_PATHS before this "
                   "manifest can be served; the manifest does not authorize its own path.")
    return out


def build_generation_one(*, monolith_path: str, manifest_root: str, at_utc: str,
                         operator: str, generation_id: Optional[str] = None,
                         require_sealed: bool = True) -> Dict:
    """Prepare and publish in one call, for a caller holding no locks.

    `ops/p5_prod_provision.py` does NOT use this: it prepares outside the ingest lock and
    publishes inside, which is the whole point of the split."""
    prepared = prepare_generation_one(
        monolith_path=monolith_path, manifest_root=manifest_root, at_utc=at_utc,
        operator=operator, generation_id=generation_id, require_sealed=require_sealed)
    return publish_prepared(prepared)
