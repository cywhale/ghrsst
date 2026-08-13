"""dev2026 — P5-S4: atomic manifest publication, superseded lifecycle, rollback.

Three things live here, and they are one step because each is only safe in the presence of the
others.

**Publication (§7.4)** turns a validated block plan into manifest generation `N+1`. It runs
under the **ingest lock**, re-reads the live delta, and re-checks that the plan's recorded
sources still hold — the P4-S8a `aborted_stale` shape. A build runs for hours outside the lock;
what it read at minute zero is a *claim*, and the claim is re-verified at the commit point or it
is not published.

**The superseded lifecycle (§8.5)** is where the one deletion hazard lives. A block that leaves
`segments` is still referenced by in-flight snapshots, is still the next fold's build input, and
is still the rollback target. `referenced → releasable → held` exists so that none of those
three uses can be pulled out from under. Nothing here hard-deletes; the terminal state is `held`
and deletion is an explicit ops act after `hold_until_utc`.

**Rollback (§9.3, §9.3a)** never `os.replace`s an archive — that would *consume* the immutable
record a second rollback or an audit needs. And it verifies that every block generation `N`
references is actually present **before** the pointer moves: replacing `manifest.json` first
would publish a generation whose snapshot build immediately fails closed, leaving the service
pinned to a stale in-memory snapshot with no valid manifest on disk.

**What this step does NOT do.** It does not prune delta and does not delete anything. The prune
gate lives in `store/repair_wal.py` and is consulted here only to *report* which folded days a
subsequent prune could touch — publication itself never needs that authorization, because
publishing a block changes no served value (delta still wins for the folded days, §4.0/§8.3).
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

import numpy as np
import zarr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest.build_block import SOURCE_ORDER, VARS   # noqa: E402  -- one list, not a copy
from store import block_manifest as bm         # noqa: E402
from store import repair_wal as rw            # noqa: E402
from store import source_provenance as sp     # noqa: E402
from store.compaction_lock import CompactionLock, CompactionLockBusy   # noqa: E402

#: §8.5 -- must exceed refresh TTL + max request duration + margin, AND the realistic
#: discover-a-defect window, because a superseded block is also the rollback target.
DEFAULT_RELEASE_AFTER_S = 24 * 3600
DEFAULT_HOLD_DAYS = 14                          # P4-S8 §7

HOLD_LOG = "p5_hold.jsonl"
MANIFEST_LOG = "p5_manifest.jsonl"


class PublishRefused(Exception):
    """Nothing was written. The live manifest is byte-unchanged."""


#: `created_by` (who asked for the publication) is now the ONLY value that crosses from the
#: plan. The superseded entry's `bytes` used to as well; it is measured from the block on disk,
#: and there is no longer any fallback to the plan's claim -- a predecessor that cannot be
#: measured is a refusal, not a number to guess at.
#:
#: Fields ignored when comparing, because publication REGENERATES them: a fresh
#: `generation_id`, `created_utc` at the moment of commit, and the lifecycle deadlines, which
#: are computed from the publication clock under policy and never accepted from the document.
_REGENERATED_TOP = ("generation_id", "created_utc", "manifest_checksum")
_REGENERATED_SUPERSEDED = ("release_after_utc", "hold_until_utc", "bytes")


#: Distinguishes "the caller did not supply a source_map" from "the caller supplied an EMPTY
#: one". `plan.get("source_map")` collapses those two into the same falsy value, and an empty
#: map is not a missing field -- it is a positive claim that the block read nothing, which for
#: a block with days is a claim that must be refused rather than skipped over.
_ABSENT = object()


#: The record shape `build_block.source_map_record()` emits. Exactly these keys.
_SRC_FIELDS = ("source_kind", "source_path", "source_day_index")


def validate_source_map(sources, segment: dict, *, where: str) -> dict:
    """Strict validation of `build_provenance.sources` against the segment that carries it.

    Comparing two copies of a map only proves they agree; it says nothing about whether either
    is *well-formed*, and a self-consistent map is still trusted input. Anyone able to edit a
    plan can edit both copies and recompute `manifest_checksum`, so the map must be validated
    on its own terms:

    * exactly the three fields `build_block` writes, no more;
    * `source_kind` from the resolver's own `SOURCE_ORDER` -- otherwise an unknown kind falls
      through `staleness_guard`'s `else` branch and is silently treated as a non-delta source,
      skipping the realpath and index checks entirely;
    * `source_day_index` a **raw** non-negative `int`. Not `int(x)`: the coercion accepts
      `"3"`, `3.9` and `True`, so a string index would compare unequal to the live index and
      the refusal would look like drift rather than like malformed input;
    * `source_path` a non-empty absolute path, since it is compared against a `realpath`;
    * keys **exactly** the segment's declared present days -- a map missing a day leaves that
      day unguarded, and an extra day guards something the block does not contain.
    """
    if not isinstance(sources, dict):
        raise PublishRefused(f"{where}: build_provenance.sources must be an object, got "
                             f"{type(sources).__name__}")
    if not sources:
        raise PublishRefused(
            f"{where}: the segment carries no build_provenance.sources, so there is nothing "
            f"for the staleness guard to re-verify. A block published without provenance "
            f"cannot be shown to have read what it claims (§5, P5-S3 round 4).")

    for day, rec in sorted(sources.items()):
        if not isinstance(day, str):
            raise PublishRefused(f"{where}: source map key {day!r} is not a string")
        if not isinstance(rec, dict):
            raise PublishRefused(f"{where}: source map entry for {day} is not an object")
        missing = [k for k in _SRC_FIELDS if k not in rec]
        extra = [k for k in rec if k not in _SRC_FIELDS]
        if missing or extra:
            raise PublishRefused(
                f"{where}: source map entry for {day} has the wrong field set "
                f"(missing={missing}, unexpected={extra})")
        if rec["source_kind"] not in SOURCE_ORDER:
            raise PublishRefused(
                f"{where}: source map entry for {day} has source_kind "
                f"{rec['source_kind']!r}, not one of {list(SOURCE_ORDER)}. An unrecognised "
                f"kind would be guarded as a non-delta source, skipping the realpath and "
                f"index checks a delta day requires.")
        idx = rec["source_day_index"]
        if isinstance(idx, bool) or not isinstance(idx, int) or idx < 0:
            raise PublishRefused(
                f"{where}: source map entry for {day} has source_day_index {idx!r} "
                f"({type(idx).__name__}); it must be a non-negative int, not something that "
                f"merely converts to one")
        path = rec["source_path"]
        if not isinstance(path, str) or not path or not os.path.isabs(path):
            raise PublishRefused(
                f"{where}: source map entry for {day} has source_path {path!r}; it must be a "
                f"non-empty absolute path, because it is compared against a realpath")

    declared = set(bm.declared_present_days(segment))
    if set(sources) != declared:
        only_map = sorted(set(sources) - declared)
        only_seg = sorted(declared - set(sources))
        raise PublishRefused(
            f"{where}: the source map does not cover the segment's present days exactly "
            f"(in map only: {only_map[:3]}, in segment only: {only_seg[:3]}). A day missing "
            f"from the map is a day the staleness guard never checks.")
    return dict(sources)


def bind_segment_to_block(segment: dict, sources: dict, block_path: str, grid: dict, *,
                          where: str) -> None:
    """Re-derive the segment's every store-dependent field from the block ON DISK and compare.

    Everything before this is plan-internal: two maps agreeing, and a map that is well-formed.
    Neither reaches outside the document. This opens the block about to be published and
    rebuilds what the manifest claims about it, using the **same functions the reader uses** --
    `inspect_store_contract` → `segment_layout_from_inspection` /
    `metadata_fingerprint_from_inspection`, which is P5-S3's `build → inspect → fingerprint`
    discipline applied at the commit point rather than only at build time.

    Checking the day set alone was not enough. `layout`, `variables`, `fingerprint.metadata`
    and `day_digest` could each be edited, `manifest_checksum` recomputed, and the manifest
    published successfully -- with the mismatch surfacing only later, when `SegmentedCubeStore`
    fails closed building a snapshot. That turns an editable-document error into an outage: the
    service holds a stale in-memory snapshot and the on-disk manifest is unusable. Refusing
    here keeps it a refusal.

    **What this still does not establish.** That each day was read from the `source_path` and
    `source_day_index` the map claims. Nothing on disk records that except the map itself, so
    the binding is to *which block* and *what is in it*, not to *which bytes were read*. See
    §10.
    """
    insp = bm.inspect_store_contract(block_path)
    actual = set(insp.days)
    if set(sources) != actual:
        only_map = sorted(set(sources) - actual)
        only_block = sorted(actual - set(sources))
        raise PublishRefused(
            f"{where}: the source map does not match the days actually present in "
            f"{block_path} (in map only: {only_map[:3]}, in block only: {only_block[:3]}). "
            f"The map describes a different block than the one being published.")

    expected = {
        "day_count": len(insp.days),
        "layout": bm.segment_layout_from_inspection(insp),
        "variables": sorted(insp.vars),
        "fingerprint.metadata": bm.metadata_fingerprint_from_inspection(insp),
        "fingerprint.day_digest": bm.day_digest(insp.days),
    }
    declared = {
        "day_count": segment["day_count"],
        "layout": segment["layout"],
        "variables": sorted(segment["variables"]),
        "fingerprint.metadata": segment["fingerprint"].get("metadata"),
        "fingerprint.day_digest": segment["fingerprint"].get("day_digest"),
    }
    wrong = sorted(k for k in expected if declared[k] != expected[k])
    if wrong:
        detail = "; ".join(f"{k}: manifest {declared[k]!r} vs block {expected[k]!r}"
                           for k in wrong[:3])
        raise PublishRefused(
            f"{where}: the segment entry does not describe {block_path} ({detail}). A manifest "
            f"that passes its own checksum but disagrees with the block is not caught until a "
            f"snapshot build fails closed, which is an outage rather than a refusal.")

    if not _grid_matches(insp, grid):
        raise PublishRefused(
            f"{where}: {block_path} is {insp.ny}x{insp.nx} region {list(insp.region)}, but the "
            f"manifest grid declares {grid.get('ny')}x{grid.get('nx')} region "
            f"{grid.get('region')}")


def _grid_matches(insp, grid: dict) -> bool:
    """Shape always; sub-region only when the block declares one.

    An absent `attrs["region"]` means the block IS the full grid -- it is not a mismatch, and
    treating it as one would refuse every block the builder writes without a region."""
    if int(grid.get("ny", -1)) != int(insp.ny) or int(grid.get("nx", -1)) != int(insp.nx):
        return False
    if not insp.region:
        return True
    return list(grid.get("region") or []) == list(insp.region)


def verify_build_artifact(sources: dict, artifact_path: str, *, segment_id: str,
                          block_path: str, where: str,
                          source_reader=None) -> dict:
    """Verify the builder's checksummed provenance artifact — locator AND bytes.

    Four things must hold, and they are four because each catches something the others cannot:

    1. **the artifact itself is intact** — `load_artifact` checks format, field completeness and
       `artifact_checksum`. A truncated or edited artifact is refused, not partly believed;
    2. **it describes this block** — `segment_id`, and a day set exactly matching the manifest's
       source map;
    3. **its locator records agree with the manifest's** — the same `source_kind`, resolved
       `source_path` and `source_day_index` per day. This is the P5-S4 check, still necessary
       and still not sufficient;
    4. **the bytes in the block match the fingerprint the builder recorded from the source.**
       This is the new one. Everything before it is a document agreeing with another document;
       this reads the block's own cells and compares them to what the build says came out of
       the source. A block whose contents were altered after the build, or an artifact minted
       for a different build, fails here and nowhere else.

    When `source_reader` is supplied, a fifth check runs: re-read the same window from the
    **source** and require the same fingerprint. That is what detects a source modified in
    place after the build — the case the locator map is structurally blind to.

    A source that is **gone or unreadable is a refusal, not a skip.** The publication path
    already requires every recorded source to still resolve (the §7.4 staleness guard), so a
    vanished source is not the ordinary case it might look like; making it a soft skip would
    add a branch nothing can reach through publication and turn "we could not check" into "we
    checked".

    **What this does not establish.** The comparison is a seeded sample (§7.5a E2), so a
    corruption confined to unsampled cells survives it. E2 is defence in depth; E1's repair
    identity is the deterministic authorization. Recorded here so the artifact is not read as
    more than it is.
    """
    doc = sp.load_artifact(artifact_path, expected_vars=VARS)
    if doc["segment_id"] != segment_id:
        raise PublishRefused(
            f"{where}: provenance artifact {artifact_path} describes segment "
            f"{doc['segment_id']!r}, not {segment_id!r}")
    want_base = os.path.basename(os.path.abspath(block_path).rstrip("/"))
    if doc["block_path"] != want_base:
        raise PublishRefused(
            f"{where}: provenance artifact names block {doc['block_path']!r} but the segment "
            f"being published is {want_base!r}. The field is part of the audit record, so an "
            f"unchecked one is a wrong audit record rather than a harmless label.")

    recorded = doc["days"]
    if set(recorded) != set(sources):
        only_art = sorted(set(recorded) - set(sources))
        only_map = sorted(set(sources) - set(recorded))
        raise PublishRefused(
            f"{where}: the provenance artifact covers different days than the manifest's "
            f"source map (only in artifact: {only_art[:3]}, only in manifest: "
            f"{only_map[:3]})")

    for day in sorted(sources):
        want, got = sources[day], recorded[day]
        for field in ("source_kind", "source_path", "source_day_index"):
            if got[field] != want[field]:
                raise PublishRefused(
                    f"{where}: provenance artifact and manifest disagree for {day} on "
                    f"{field}: {got[field]!r} vs {want[field]!r}. The builder's own record is "
                    f"the only evidence outside the manifest of what was read.")

    # Grid and seed come from AUTHORITY, not from the artifact. `load_artifact` has already
    # pinned the sample parameters to policy; the grid is taken from an inspection of the block
    # itself and the artifact's declaration is then checked against it. Reading `ny`/`nx` out of
    # the document would let an artifact declare a 4x4 grid and be verified against a sample of
    # its own choosing.
    insp = bm.inspect_store_contract(block_path)
    ny, nx = int(insp.ny), int(insp.nx)
    seed = sp.SAMPLE_SEED
    if (int(doc["grid"]["ny"]), int(doc["grid"]["nx"])) != (ny, nx):
        raise PublishRefused(
            f"{where}: the provenance artifact declares a {doc['grid']['ny']}x"
            f"{doc['grid']['nx']} grid but {block_path} is {ny}x{nx}. The sample is drawn "
            f"against the block's real geometry, so an artifact describing another one is "
            f"describing another block.")
    g = zarr.open_group(block_path, mode="r")
    block_valid = {v: list(flags) for v, flags in insp.var_valid}

    for day in sorted(recorded):
        rec = recorded[day]
        t_idx = int(rec["day_index"])
        # Bind the index to the DATE, not merely to the array bounds. A `day_index` inside
        # range still selects a slot, and if that slot's sampled cells happen to agree -- an
        # all-NaN region, a repeated value, a short block -- the fingerprint matches and the
        # artifact has silently attested day D against another day's bytes.
        if t_idx >= len(insp.days) or insp.days[t_idx] != day:
            actual = insp.days[t_idx] if t_idx < len(insp.days) else "out of range"
            raise PublishRefused(
                f"{where}: the artifact records day_index {t_idx} for {day}, but the block "
                f"holds {actual!r} at that index. An index that resolves to a different date "
                f"attests the wrong day's bytes.")
        i0, i1, j0, j1 = sp.sample_window(ny, nx, seed=seed, day_index=t_idx)
        tiles, valid = {}, {}
        for var in VARS:                        # the canonical domain, matching the builder
            flags = block_valid.get(var, [])
            present = bool(flags) and t_idx < len(flags) and flags[t_idx] is True
            valid[var] = present
            tiles[var] = (np.asarray(g[var][t_idx, i0:i1, j0:j1]) if present else None)
        actual = sp.window_fingerprint(tiles, seed=seed, day_index=t_idx, var_valid=valid)
        if actual != rec["source_fingerprint"]:
            raise PublishRefused(
                f"{where}: the block's bytes for {day} do not match the fingerprint the build "
                f"recorded from its source (block {actual[:12]}… vs artifact "
                f"{rec['source_fingerprint'][:12]}…). Either the block was altered after the "
                f"build or the artifact belongs to a different one; both are refusals.")

    checked_sources = 0
    if source_reader is not None:
        for day in sorted(recorded):
            rec = recorded[day]
            src = {"source_kind": rec["source_kind"], "source_path": rec["source_path"],
                   "source_day_index": rec["source_day_index"], "day": day}
            got = _read_source_window(source_reader, src, day, rec, ny, nx, seed)
            if got is None:
                raise PublishRefused(
                    f"{where}: the source {rec['source_path']} recorded for {day} is gone or "
                    f"unreadable, so the bytes the build claims to have read cannot be "
                    f"re-checked. Not being able to check is not the same as checking.")
            tiles, source_valid = got

            # Compare the source's OWN availability against what the build recorded, before
            # anything is read for value. A variable backfilled from absent to present changes
            # nothing about the sampled cells of the variables that were already there, so a
            # value-only comparison cannot see it.
            recorded_valid = {v: bool(rec["var_valid"].get(v, False)) for v in VARS}
            if source_valid != recorded_valid:
                changed = sorted(v for v in VARS if source_valid[v] != recorded_valid[v])
                raise PublishRefused(
                    f"{where}: source {rec['source_path']} for {day} no longer has the same "
                    f"variables the build read: {', '.join(f'{v} {recorded_valid[v]} -> ' + str(source_valid[v]) for v in changed)}. "
                    f"An absent variable backfilled to present is a source change, and the "
                    f"one a value comparison alone cannot see.")

            actual = sp.window_fingerprint(tiles, seed=seed, day_index=int(rec["day_index"]),
                                           var_valid=source_valid)
            checked_sources += 1
            if actual != rec["source_fingerprint"]:
                raise PublishRefused(
                    f"{where}: source {rec['source_path']} for {day} no longer holds the bytes "
                    f"the build read from it (now {actual[:12]}… vs recorded "
                    f"{rec['source_fingerprint'][:12]}…). An in-place source modification is "
                    f"invisible to every locator check; this is the one that sees it.")
    return {"days": len(recorded), "sources_rechecked": checked_sources}


def _read_source_window(reader, src: dict, day: str, rec: dict, ny: int, nx: int, seed: int):
    """The sample window from the SOURCE, plus the source's OWN `var_valid`.

    Returns `(tiles, source_valid)` or `None` when the source cannot be read; the caller turns
    `None` into a refusal — this function reports, it does not decide.

    **`var_valid` is read from the source, never from the artifact.** The earlier version used
    the artifact's flags to decide whether to read a variable at all, so a variable that was
    *absent* at build time and has since been **backfilled to present** was skipped: the loop
    set `tiles[var] = None` without ever touching the source, the fingerprints matched, and a
    real source change passed verification. That is precisely the gap↔present transition a
    corrective refold is about, so the one case the check most needed to catch was the one it
    structurally could not see."""
    path = rec["source_path"]
    kind = rec["source_kind"]
    probe = _source_group_path(kind, path, day)
    if not probe or not os.path.isdir(probe):
        return None
    try:
        if kind in ("daily", "hold"):
            reader.inspect_daily(path, day)
        else:
            reader.inspect_cube(path)
    except Exception:                            # unreadable now: treated as gone, not as proof
        return None
    i0, i1, j0, j1 = sp.sample_window(ny, nx, seed=seed, day_index=int(rec["day_index"]))
    tiles, source_valid = {}, {}
    for var in VARS:
        try:
            present = bool(reader.has_var(src, var))
        except Exception:
            return None
        source_valid[var] = present
        if not present:
            tiles[var] = None
            continue
        try:
            tiles[var] = reader.read_tile(src, var, i0, i1, j0, j1)
        except Exception:
            return None
    return tiles, source_valid


def _segment_provenance(segment: dict) -> dict:
    return (segment.get("build_provenance") or {}).get("sources")


def _validate_plan_source_map(segment: dict, supplied, *, where: str) -> dict:
    """Bind the guard's map to the segment's OWN provenance, and return it.

    The authority is `build_provenance.sources` -- the copy that gets published and that an
    auditor reads back -- never the plan's convenience copy. Checking this once, at planning
    time, is not enough: a plan is designed to be inspected, serialized and re-loaded by a
    human between planning and execution, so a top-level `source_map` edited in that window
    would sail through a guard that had already been satisfied. **Both entry points call
    this**, and execution re-derives the authority from the manifest it is about to publish.
    """
    provenance = validate_source_map(_segment_provenance(segment) or {}, segment, where=where)
    if supplied is _ABSENT:
        return provenance                       # nothing to contradict; the segment is used
    if not isinstance(supplied, dict):
        raise PublishRefused(
            f"{where}: source_map must be an object, got {type(supplied).__name__}")
    if supplied != provenance:
        only_top = sorted(set(supplied) - set(provenance))
        only_prov = sorted(set(provenance) - set(supplied))
        differing = sorted(d for d in set(supplied) & set(provenance)
                           if supplied[d] != provenance[d])
        raise PublishRefused(
            f"{where}: source_map and the segment's build_provenance.sources disagree "
            f"({len(supplied)} vs {len(provenance)} day(s); only in source_map: "
            f"{only_top[:3]}, only in provenance: {only_prov[:3]}, differing: "
            f"{differing[:3]}). The staleness guard would be checking a map the published "
            f"manifest does not contain.")
    return provenance


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _append_log(root: str, name: str, entry: dict) -> None:
    """Append-only audit trail, fsync'd per line. Observability plus forensics -- but unlike
    the repair WAL, nothing branches on it, so it is not checksum-chained."""
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, name)
    existed = os.path.isfile(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o644)
    try:
        os.write(fd, (json.dumps(entry, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    if not existed:
        bm._fsync_dir(root)


# --------------------------------------------------------------------------- the plan
def plan_publication(root: str, plan: dict, *, release_after_s: int = DEFAULT_RELEASE_AFTER_S,
                     hold_days: int = DEFAULT_HOLD_DAYS, now: Optional[datetime] = None,
                     created_by: str = "p5-compaction") -> dict:
    """Build generation `N+1` **in memory** from the live manifest and a `build_block` plan.

    Pure with respect to the live manifest: it reads, it does not write. The result is handed
    to `execute_publication`, which re-checks it under the lock. Separating them is what makes
    "validated-plan-only" mean something -- a plan can be inspected, diffed and refused by a
    human before anything commits.
    """
    now = now or _utcnow()
    live = bm.load_live(root)
    bm.validate_manifest(live)
    segment = dict(plan["segment"])
    candidate, replaced = compose_generation(
        live, segment, now=now, release_after_s=release_after_s, hold_days=hold_days,
        predecessor_bytes=int(plan.get("predecessor_bytes", 0)), created_by=created_by)
    gen = candidate["generation"]

    provenance = _validate_plan_source_map(segment, plan.get("source_map", _ABSENT),
                                           where="the build plan")

    return {"status": "planned", "root": root, "generation": gen,
            "predecessor_generation": int(live["generation"]),
            "manifest": candidate,
            "new_segment_id": segment["segment_id"],
            "superseded_now": None if replaced is None else replaced["segment_id"],
            "source_map": provenance,
            "block_path": plan.get("out_path"),
            "note": ("A plan. Nothing is committed until execute_publication re-checks it "
                     "under the ingest lock."),
    }


def validate_retention_policy(release_after_s, hold_days) -> tuple:
    """The deadlines are only as good as the policy they are computed from.

    Round 7 stopped the plan from back-dating `release_after_utc` / `hold_until_utc` by
    computing them at publication -- from these two numbers. If the numbers themselves can be
    negative or zero, the protection is back where it started, just one level down:
    `release_after_s=-1, hold_days=0` produces deadlines already in the past, and the lifecycle
    releases and holds the block on its next run.

    `hold_days` must be strictly positive: `held` is the terminal state, and a hold window that
    has already expired means ops may hard-delete immediately. And the hold window must not be
    *shorter* than the release window -- `release_after_utc > hold_until_utc` would order the
    lifecycle backwards, making a block eligible for hard delete before it is even eligible to
    leave `referenced`.
    """
    if isinstance(release_after_s, bool) or not isinstance(release_after_s, int):
        raise PublishRefused(f"release_after_s must be an int, got {release_after_s!r}")
    if isinstance(hold_days, bool) or not isinstance(hold_days, int):
        raise PublishRefused(f"hold_days must be an int, got {hold_days!r}")
    if release_after_s < 0:
        raise PublishRefused(
            f"release_after_s must be >= 0, got {release_after_s}: a negative grace period "
            f"produces a release_after_utc already in the past, which releases a superseded "
            f"block that in-flight snapshots, the next fold and rollback all still need "
            f"(§8.5)")
    if hold_days <= 0:
        raise PublishRefused(
            f"hold_days must be > 0, got {hold_days}: `held` is the terminal state, and a hold "
            f"window that has already expired means ops may hard-delete the block immediately")
    if hold_days * 86400 <= release_after_s:
        raise PublishRefused(
            f"the hold window ({hold_days}d) is not longer than the release window "
            f"({release_after_s}s), which collapses the lifecycle: at equality the block "
            f"becomes releasable and hard-deletable at the same instant, and below it the "
            f"order reverses -- eligible for hard delete before eligible to leave "
            f"`referenced`. The two stages exist to be distinct.")
    return int(release_after_s), int(hold_days)


def measure_bytes(path: str) -> Optional[int]:
    """On-disk size of a block, by walking it. `None` when it is not there; **raises `OSError`
    on a member it cannot read** rather than returning a short count.

    Called **before the ingest lock is taken**, deliberately. A production block is ~94 GB
    across a large number of files (§10.6), so the walk costs seconds; doing it under the lock
    would stall the twice-daily delta append for exactly as long, which is a worse outcome than
    a slightly stale measurement of a store that is immutable anyway.

    It fails closed. An earlier version swallowed every `OSError` -- a vanished file, a
    permission error, an I/O fault -- and returned the partial sum, which silently under-counts
    the block. That number feeds the §7.1c `pinned_existing` capacity forecast and the
    superseded-block hold accounting, so an under-count reports free space that does not exist
    and records a hold entry sized smaller than the block it is protecting. `os.walk` also
    swallows traversal errors unless given an `onerror` hook, so a permission error on a
    sub-group would otherwise be skipped before `lstat` is ever reached. A block that cannot be
    measured in full is not measured: the error propagates and the caller refuses rather than
    publish against a guess."""
    if not os.path.isdir(path):
        return None

    def _fail(err: OSError) -> None:            # os.walk ignores traversal errors by default
        raise err

    total = 0
    for dirpath, _dirnames, filenames in os.walk(path, onerror=_fail):
        for name in filenames:
            total += os.lstat(os.path.join(dirpath, name)).st_size
    return total


def compose_generation(live: dict, segment: dict, *, now: datetime,
                       release_after_s: int = DEFAULT_RELEASE_AFTER_S,
                       hold_days: int = DEFAULT_HOLD_DAYS, predecessor_bytes: int = 0,
                       created_by: str = "p5-compaction"):
    """Generation `N+1` = live, minus exactly the superseded segment, plus exactly the new one.

    Written once and called from **both** planning and publication. At publication it is
    re-run against the live manifest under the lock and the result is compared, whole-object,
    against the plan's manifest -- so the question is never "which fields did someone change",
    which requires enumerating the ways a document can be wrong, but "is this the manifest this
    live generation and this segment produce". Enumerating is how the round-5 fix still left
    a hole: it compared the entries that *remained*, and said nothing about an entry that was
    removed or one that was injected.
    """
    release_after_s, hold_days = validate_retention_policy(release_after_s, hold_days)
    if isinstance(predecessor_bytes, bool) or not isinstance(predecessor_bytes, int) \
            or predecessor_bytes < 0:
        raise PublishRefused(
            f"predecessor_bytes must be a non-negative int, got {predecessor_bytes!r}; it "
            f"feeds the §7.1c pinned_existing forecast and the lifecycle audit, and a negative "
            f"size makes both report free space that does not exist")
    gen = int(live["generation"]) + 1
    segments = [dict(s) for s in live["segments"]]
    superseded = [dict(s) for s in live["superseded"]]      # CUMULATIVE (§5.6), never reset

    replaced = None
    if segment.get("supersedes"):
        matches = [s for s in segments if s["segment_id"] == segment["supersedes"]]
        if not matches:
            raise PublishRefused(
                f"the new segment declares supersedes={segment['supersedes']!r} but no such "
                f"segment is in generation {live['generation']}; publishing would drop a "
                f"reference to a block nothing else names")
        replaced = matches[0]
        segments = [s for s in segments if s["segment_id"] != replaced["segment_id"]]

    if any(s["segment_id"] == segment["segment_id"] for s in segments):
        raise PublishRefused(f"segment_id {segment['segment_id']!r} is already published; "
                             f"block paths are written once and never reused")
    segments.append(segment)
    segments.sort(key=lambda s: (s["start_day"], s["segment_id"]))

    if replaced is not None:
        # `validate_retention_policy` has already established hold > release, so the computed
        # deadlines cannot invert. An extra check here would be unreachable code that no
        # mutation can kill -- which reads as a guard while being none.
        release_at = now + timedelta(seconds=release_after_s)
        hold_until = now + timedelta(days=hold_days)
        superseded.append({
            "segment_id": replaced["segment_id"], "path": replaced["path"],
            "superseded_at_generation": gen,
            "release_after_utc": _iso(release_at),
            "hold_until_utc": _iso(hold_until),
            "status": "referenced",
            "current_path": replaced["path"],
            "bytes": int(predecessor_bytes),
            "fingerprint": {"algo": "sha256",
                            "day_digest": replaced["fingerprint"]["day_digest"]},
        })

    candidate = {
        "format": live["format"], "version": live["version"],
        "generation": gen, "generation_id": str(uuid.uuid4()),
        "created_utc": _iso(now), "created_by": created_by,
        "predecessor_generation": int(live["generation"]),
        "predecessor_manifest": bm.archive_name(int(live["generation"])),
        "grid": live["grid"], "variables": live["variables"], "block_grid": live["block_grid"],
        "segments": segments, "superseded": superseded, "manifest_checksum": "",
    }
    candidate["manifest_checksum"] = bm.compute_checksum(candidate)
    bm.validate_manifest(candidate)
    return candidate, replaced




# --------------------------------------------------------------------------- staleness guard
def staleness_guard(source_map: dict, *, delta_path: Optional[str],
                    live_days: Sequence[str],
                    block_day_index=None) -> Optional[dict]:
    """§7.4 step 1, run **under the ingest lock**. Returns a refusal dict, or None.

    The build ran for hours outside the lock. What it read then is a claim; this is where the
    claim is re-checked against the world as it is at the commit point.

    **Date membership is not identity.** A day being present in "the delta" says nothing about
    *which* delta, or *where in it*. The provenance map records `source_path` (resolved) and
    `source_day_index` precisely so both can be re-checked, and all three must still agree:

    * the live delta must **resolve to the same store** the build read. The delta path is a
      stable alias that swap retargets, so a swap that replaces the store behind the same alias
      leaves every date present while every byte may differ;
    * the day must still sit at the **same physical index**. Delta `attrs['days']` is append
      order after a backfill (P4-S4 §2), so a rebuild can preserve the date set and move the
      dates -- and an index that moved means the build read a different slot than the one the
      manifest will claim it read;
    * the live delta must have **unique** days, or the delta is in a state no downstream gate
      can reason about (the P4-S8a shape).

    **Each kind is checked on its own terms.** Putting every non-delta kind through one branch
    made `source_kind` almost decorative: a `block` entry pointed at a daily root, or a
    `daily` entry carrying an arbitrary index, passed a check that only asked whether *some*
    path existed. What each kind means is different, so what must be true of it is different:

    * `delta` — the live delta's realpath, and the day at the recorded index;
    * `block` — a block **published in the live manifest**, holding that day at the recorded
      index. `block_day_index(path)` resolves `{day: index}` for a published block; if the
      caller supplies no resolver, a `block` source **refuses**, because an unverifiable claim
      is not a weaker claim, it is no claim;
    * `daily` / `hold` — the exact `YYYY/MM/DD` group, with index `0`. A daily group has shape
      `(1, ny, nx)`, so `0` is the only index that means anything and any other value indicates
      a map built by something that did not understand the source.

    Checking the exact group rather than the root matters on its own: a daily root exists for
    years, so `os.path.exists(root)` proves nothing about the day.
    """
    live = list(live_days)
    if len(live) != len(set(live)):
        dups = sorted({d for d in live if live.count(d) > 1})
        return {"status": "aborted_stale", "reason": f"live delta has duplicate days: {dups}",
                "published": False}
    live_index = {d: i for i, d in enumerate(live)}
    live_real = os.path.realpath(delta_path) if delta_path else None

    absent, retargeted, moved, missing_src, bad_kind = [], [], [], [], []
    for day, rec in sorted(source_map.items()):
        kind, path = rec["source_kind"], rec["source_path"]
        idx = rec["source_day_index"]
        if kind == "delta":
            if delta_path is None or day not in live_index:
                absent.append(day)
            elif path != live_real:
                retargeted.append((day, path, live_real))
            elif idx != live_index[day]:
                moved.append((day, idx, live_index[day]))
        elif kind == "block":
            if block_day_index is None:
                bad_kind.append((day, f"source_kind 'block' at {path}, but no published-block "
                                      f"resolver was supplied, so the claim cannot be checked"))
                continue
            index = block_day_index(path)
            if index is None:
                bad_kind.append((day, f"{path} is not a block published in the live manifest"))
            elif day not in index:
                bad_kind.append((day, f"published block {path} does not contain this day"))
            elif idx != index[day]:
                bad_kind.append((day, f"published block {path} holds this day at index "
                                      f"{index[day]}, not the recorded {idx}"))
        else:                                   # daily | hold -- validated to be one of these
            group = _source_group_path(kind, path, day)
            if not group or not os.path.isdir(group):
                missing_src.append((day, group))
            elif idx != 0:
                bad_kind.append((day, f"a {kind} group has shape (1, ny, nx), so index 0 is "
                                      f"the only meaningful value; the map records {idx}"))

    def refuse(reason, days):
        return {"status": "aborted_stale", "published": False, "reason": reason,
                "days": days[:10]}

    if absent:
        return refuse(
            f"{len(absent)} day(s) folded from delta are no longer in the live delta (first "
            f"{absent[0]}); the delta was swapped underneath the build, so the block may hold "
            f"a value that is no longer the serving authority", absent)
    if retargeted:
        d, was, now = retargeted[0]
        return refuse(
            f"the live delta resolves to {now} but the block read {was} ({len(retargeted)} "
            f"day(s), first {d}). Every date is still present, but they are not the same "
            f"bytes: the alias was retargeted by a swap while the build ran",
            [x[0] for x in retargeted])
    if moved:
        d, was, now = moved[0]
        return refuse(
            f"{len(moved)} day(s) moved physical index in the live delta (first {d}: index "
            f"{was} -> {now}). `attrs['days']` is append order after a backfill, so the delta "
            f"was rebuilt and the block read a different slot than it records",
            [x[0] for x in moved])
    if missing_src:
        return refuse(
            f"{len(missing_src)} recorded non-delta source group(s) no longer exist (first "
            f"{missing_src[0][0]} at {missing_src[0][1]}); the provenance the block records "
            f"cannot be re-verified", [d for d, _ in missing_src])
    if bad_kind:
        return refuse(
            f"{len(bad_kind)} source record(s) do not match what their source_kind means "
            f"(first {bad_kind[0][0]}: {bad_kind[0][1]})", [d for d, _ in bad_kind])
    return None


def _published_block_index(root: str, live: dict):
    """`realpath -> {day: index}` for blocks the LIVE generation publishes, resolved lazily.

    Lazily because most folds reference one predecessor block and opening every published
    segment to answer a question about one of them is disk work for nothing. Restricted to the
    live generation because a `block` source is a claim to have read *published history*; a
    path that is a legal Zarr store but is not in the manifest is not that."""
    allowed = {os.path.realpath(_seg_path(root, s)): s for s in live["segments"]}
    cache: Dict[str, Optional[dict]] = {}

    def resolve(path: Optional[str]) -> Optional[dict]:
        if not path:
            return None
        real = os.path.realpath(path)
        if real not in cache:
            if real not in allowed:
                cache[real] = None
            else:
                insp = bm.inspect_store_contract(real)
                cache[real] = {d: i for i, d in enumerate(insp.days)}
        return cache[real]

    return resolve


def _source_group_path(kind: str, root: Optional[str], day: str) -> Optional[str]:
    """The path that must still exist for `day` -- the GROUP, never merely the root."""
    if not root:
        return None
    if kind in ("daily", "hold"):
        y, m, d = day.split("-")
        return os.path.join(root, y, m, d)
    return root


def _read_delta_days(delta_path: Optional[str]) -> List[str]:
    if not delta_path or not os.path.isdir(delta_path):
        return []
    import zarr
    return list(zarr.open_group(delta_path, mode="r").attrs.get("days", []))


# --------------------------------------------------------------------------- execute
def recompose(live: dict, plan: dict, *, now: datetime,
              release_after_s: int = DEFAULT_RELEASE_AFTER_S,
              hold_days: int = DEFAULT_HOLD_DAYS,
              measured_bytes: Optional[int] = None):
    """Re-derive generation `N+1` from the LIVE manifest, and say whether the plan matches.

    Returns `(manifest, replaced, difference)`. **The returned manifest is what gets
    published** — the plan's is never handed to `bm.publish`. That is the difference between
    checking a document and producing one: a field this function does not deliberately carry
    across simply cannot survive from the plan into the published manifest, whatever an editor
    did to it.

    Round 5 compared the carried-forward entries that were still *present*, which says nothing
    about an entry **removed** or one **injected**. Enumerating what may change requires
    enumerating every way a document can be wrong; re-composing asks the only question with a
    definite answer — *is this the manifest this live generation and this segment produce?*

    **The lifecycle deadlines are computed here, from the publication clock, and never
    accepted.** They were previously carried over from the plan as "clock-derived", which let a
    plan set `release_after_utc` and `hold_until_utc` into the past — shortening or erasing the
    window that keeps a superseded block available to in-flight snapshots, to the next fold,
    and to rollback, and letting ops hard-delete it early. Validating a lower bound instead
    would refuse every plan reviewed for longer than the grace period; computing them at
    publication is both stricter and correct, because the window protects from the moment of
    publication, not from the moment of planning.
    """
    segment = next((s for s in plan["manifest"]["segments"]
                    if s["segment_id"] == plan["new_segment_id"]), None)
    if segment is None:
        return None, None, (f"the plan's manifest does not contain segment "
                            f"{plan['new_segment_id']!r}")
    if segment.get("supersedes") and measured_bytes is None:
        # There used to be a fallback here that took the plan's claimed byte count when the
        # block could not be measured. It is gone: an unmeasured predecessor is now refused
        # upstream, so the fallback was unreachable, and an unreachable branch that substitutes
        # an unverified number reads as a guard while being none. This refusal replaces it, so
        # a future caller that forgets to measure gets a refusal rather than a plausible zero.
        return None, None, (
            f"segment {plan['new_segment_id']!r} supersedes "
            f"{segment['supersedes']!r} but no measured size was supplied for it; the "
            f"superseded entry's `bytes` is measured from the block on disk and never taken "
            f"from the plan")
    try:
        expected, replaced = compose_generation(
            live, segment, now=now, release_after_s=release_after_s, hold_days=hold_days,
            predecessor_bytes=measured_bytes or 0,
            created_by=plan["manifest"].get("created_by", "p5-compaction"))
    except (PublishRefused, bm.ManifestError) as exc:
        # The live manifest plus this segment does not compose into a valid generation at all,
        # which is a stronger statement than "differs" and is reported as the difference.
        return None, None, (
            f"generation {int(live['generation']) + 1} cannot be composed from the live "
            f"manifest and segment {plan['new_segment_id']!r}: {exc}")

    if _comparable(expected) == _comparable(plan["manifest"]):
        return expected, replaced, None

    got, want = plan["manifest"], expected
    got_ids = [s["segment_id"] for s in got["segments"]]
    want_ids = [s["segment_id"] for s in want["segments"]]
    if got_ids != want_ids:
        return None, None, (
            f"the plan publishes segments {got_ids} but this live generation plus "
            f"{plan['new_segment_id']!r} yields {want_ids} (removed: "
            f"{sorted(set(want_ids) - set(got_ids))}, injected: "
            f"{sorted(set(got_ids) - set(want_ids))})")
    got_sup = [(e["segment_id"], e["superseded_at_generation"]) for e in got["superseded"]]
    want_sup = [(e["segment_id"], e["superseded_at_generation"]) for e in want["superseded"]]
    if got_sup != want_sup:
        return None, None, (f"the plan's superseded list is {got_sup}, expected {want_sup}")
    a, b = _comparable(got), _comparable(want)
    differing = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
    return None, None, (f"the plan's manifest differs from the one this live generation "
                        f"produces (fields: {differing})")


def _comparable(manifest: dict) -> dict:
    """The manifest with regenerated fields blanked, for comparison only."""
    out = {k: v for k, v in manifest.items() if k not in _REGENERATED_TOP}
    out["superseded"] = [{k: v for k, v in e.items() if k not in _REGENERATED_SUPERSEDED}
                         for e in manifest.get("superseded", [])]
    return out


def execute_publication(plan: dict, *, ingest_lock_path: str,
                        compaction_lock_path: Optional[str] = None,
                        compaction_lock: Optional[CompactionLock] = None,
                        delta_path: Optional[str] = None,
                        build_artifact_path: Optional[str],
                        release_after_s: int = DEFAULT_RELEASE_AFTER_S,
                        hold_days: int = DEFAULT_HOLD_DAYS,
                        operator: str = "", now: Optional[datetime] = None,
                        unsafe_skip_compaction_lock: bool = False,
                        unsafe_skip_source_recheck: bool = False,
                        unsafe_skip_provenance: bool = False) -> dict:
    """§7.4 steps 1-4. The commit point is `os.replace`, inside `bm.publish`.

    Refuses without writing anything on: a running compaction, a missing referenced block, a
    staleness drift, or any validation failure. `manifest.json` is byte-unchanged on every
    refusal path -- the archive for `N+1` is written first and is *unreferenced* until the
    replace, which is exactly the §9 "safe" crash state.
    """
    # The caller-contract half of the reservation, checked FIRST: it is about the arguments,
    # not about the plan, and discovering it after a plan walk would report the wrong problem.
    compaction_guard: Optional[CompactionLock] = None
    borrowed = compaction_lock is not None
    if borrowed:
        # A borrowed lock must be bound to the CANONICAL path, not merely be some held lock.
        # Without the binding, a caller holding an unrelated lock file satisfies "a lock is
        # held" while the real reservation sits free for a build to take -- the guarantee
        # reads as satisfied and protects nothing.
        if not compaction_lock_path:
            raise PublishRefused(
                "a borrowed compaction_lock must be accompanied by compaction_lock_path: "
                "without it there is nothing to bind the lock's identity to, and any held "
                "lock would do")
        if os.path.realpath(getattr(compaction_lock, "path", "")) != \
                os.path.realpath(compaction_lock_path):
            raise PublishRefused(
                f"the borrowed compaction_lock is on {getattr(compaction_lock, 'path', None)!r} "
                f"but the canonical reservation is {compaction_lock_path!r}. Holding some "
                f"other lock leaves the real one free for a build to take.")
        if not getattr(compaction_lock, "held", False):
            raise PublishRefused(
                "the compaction_lock passed in is not held. Publication borrows a caller's "
                "reservation; a released one is not a reservation.")
        compaction_lock.assert_still_held()
        compaction_guard = compaction_lock

    now = now or _utcnow()
    root = plan["root"]

    # Re-bind the guard's map to the manifest ABOUT TO BE PUBLISHED, not to whatever the plan
    # carries now. Between planning and here a plan may have been written to disk, reviewed and
    # read back; re-validating is the difference between a check and a binding.
    published = [s for s in plan["manifest"]["segments"]
                 if s["segment_id"] == plan["new_segment_id"]]
    if len(published) != 1:
        raise PublishRefused(
            f"the plan's manifest does not contain exactly one segment "
            f"{plan['new_segment_id']!r} ({len(published)} found); it cannot be the plan for "
            f"the block it claims")
    source_map = _validate_plan_source_map(published[0], plan.get("source_map", _ABSENT),
                                           where="at publication")
    if not build_artifact_path and not unsafe_skip_provenance:
        raise PublishRefused(
            "build_artifact_path is required: without the builder's checksummed provenance "
            "artifact the manifest carries only locator provenance, which cannot show that "
            "the block's bytes are the bytes its sources held (§7.5a). Pass the artifact "
            "path, or unsafe_skip_provenance=True in a test that is not exercising it.")

    # Fail closed on the policy BEFORE anything else: a bad grace period is a caller error, and
    # finding it after the lock is taken would stall the delta append for no reason.
    release_after_s, hold_days = validate_retention_policy(release_after_s, hold_days)

    # Measure the predecessor OUTSIDE the lock -- see `measure_bytes`. The path comes from the
    # live manifest's own segment entry, not from the plan, so a plan cannot point the
    # measurement at some other block. This read is unlocked and could be stale; that is
    # harmless, because the re-composition under the lock refuses outright if the generation
    # moved, and the block itself is immutable.
    measured = None
    supersedes = published[0].get("supersedes")
    if supersedes:
        try:
            prior_live = bm.load_live(root)
        except bm.ManifestError:
            prior_live = {"segments": []}
        prior = next((s for s in prior_live["segments"]
                      if s["segment_id"] == supersedes), None)
        # `prior is None` (the predecessor is not an active segment of live at all) is left to
        # `compose_generation`, which refuses it under the lock. But a predecessor that IS still
        # in live is one THIS publication moves to `superseded`/hold -- so its block must be on
        # disk and fully measurable right here. If it is gone or unreadable we refuse; we do NOT
        # fall back to the plan's claimed byte count. Accepting the claim would record a hold
        # entry (with real hold/release deadlines) for a block that is not there, size the
        # §7.1c forecast off a number nothing verified, and mask data loss as a block that was
        # "already released". `measure_bytes` returns None for an absent block and raises for
        # one it cannot read in full; both are refusals here, before the lock, manifest
        # byte-unchanged.
        if prior is not None:
            seg_path = _seg_path(root, prior)
            try:
                measured = measure_bytes(seg_path)
            except OSError as exc:
                raise PublishRefused(
                    f"the superseded block {supersedes!r} at {seg_path} could not be measured "
                    f"in full ({exc}); refusing rather than publishing a hold entry sized from "
                    f"the plan's unverified claim") from exc
            if measured is None:
                raise PublishRefused(
                    f"the superseded block {supersedes!r} is not present at {seg_path}, yet it "
                    f"is still an active segment of the live manifest that this publication "
                    f"supersedes; refusing rather than recording a hold entry for a block that "
                    f"is already gone -- its absence is data loss, not a legitimate release")

    # `compaction_lock_path` is a REQUIRED keyword with no default. It used to be optional, so
    # a caller who simply forgot it published without ever asking whether a build was running
    # -- and a build holding the lock may still be writing the very block we are about to
    # reference. The waiver is named so it cannot be typed by accident, and is greppable.
    #
    # We do not merely PROBE the lock and release it: a probe leaves a window between the check
    # and `bm.publish` in which a build could take the lock and begin rewriting the referenced
    # block (§7.1b). We take the lock as a RESERVATION and hold it across the whole critical
    # section -- non-blocking, so a build that already holds it makes us refuse rather than wait
    # -- and re-assert the fd/inode fence immediately before the commit.
    # A caller that already HOLDS the compaction lock passes it in. Re-acquiring would
    # deadlock against itself -- `flock` conflicts across distinct open file descriptions even
    # inside one process -- and, worse, a caller that released and let us re-acquire would have
    # opened exactly the window the reservation exists to close. So an existing lock is
    # verified and borrowed: we assert it is still held and we do NOT release it, because we
    # did not take it.
    if borrowed:
        pass
    elif not compaction_lock_path:
        if not unsafe_skip_compaction_lock:
            raise PublishRefused(
                "compaction_lock_path is required: publication must not race a build that is "
                "still writing the block it references (§7.1b). Pass the lock path, a held "
                "compaction_lock, or unsafe_skip_compaction_lock=True in a test that is not "
                "exercising it.")
    else:
        try:
            compaction_guard = CompactionLock(
                compaction_lock_path, holder="p5-manifest-publish").acquire()
        except CompactionLockBusy:
            return {"status": "refused", "published": False,
                    "reason": "compaction_lock_held", "operation": "manifest_publish",
                    "lock_path": compaction_lock_path,
                    "hint": ("a block build holds the compaction lock and may still be writing "
                             "the block this manifest would reference; publishing now could "
                             "freeze a half-written block into history. Reschedule after the "
                             "build completes.")}

    try:
        os.makedirs(root, exist_ok=True)
        lk = open(ingest_lock_path, "w")
        try:
            fcntl.flock(lk, fcntl.LOCK_EX)      # publish and the daily append are exclusive

            live = bm.load_live(root)
            if int(live["generation"]) != int(plan["predecessor_generation"]):
                return {"status": "aborted_stale", "published": False,
                        "reason": (f"the manifest moved while this plan was held: planned "
                                   f"against generation {plan['predecessor_generation']}, live "
                                   f"is {live['generation']}. Re-plan against the current "
                                   f"generation.")}

            # The manifest must be the one THIS live generation produces -- not merely one whose
            # remaining entries agree with it. What comes back is what gets published.
            manifest, replaced, diff = recompose(
                live, plan, now=now, release_after_s=release_after_s, hold_days=hold_days,
                measured_bytes=measured)
            if diff:
                return {"status": "refused", "published": False,
                        "reason": (f"{diff}. Generation N+1 is exactly: live, minus the one "
                                   f"superseded segment, plus the one new segment.")}
            new_segment = next(s for s in manifest["segments"]
                               if s["segment_id"] == plan["new_segment_id"])

            drift = staleness_guard(source_map, delta_path=delta_path,
                                    live_days=_read_delta_days(delta_path),
                                    block_day_index=_published_block_index(root, live))
            if drift:
                return drift

            # Every block the NEW generation references must exist before we point at it.
            missing = [s["path"] for s in manifest["segments"]
                       if not os.path.exists(_seg_path(root, s))]
            if missing:
                return {"status": "refused", "published": False,
                        "reason": (f"generation {plan['generation']} references block(s) that "
                                   f"are not present: {missing[:3]}. Publishing would produce a "
                                   f"manifest whose snapshot build fails closed.")}

            # ...and the entry must describe THAT block, not merely be internally consistent.
            bind_segment_to_block(new_segment, source_map, _seg_path(root, new_segment),
                                  manifest["grid"], where="at publication")

            # The byte provenance is verified HERE, inside the critical section, and nowhere
            # else. It used to run before either lock was taken -- which verified a state that
            # could then change: between that check and the commit, the staging block or a
            # source could be modified, and the only thing standing between them and
            # publication was `staleness_guard`, which compares LOCATORS and cannot see bytes.
            #
            # There is deliberately no earlier copy. A pre-lock check would be a cheap
            # fail-fast, but no mutation could kill it while this one exists, so it would read
            # as a guard while being none. The cost is bounded and known: one sample window per
            # (day, var) from the block and from each source -- tens of megabytes for a 90-day
            # fold, seconds under the lock. `measure_bytes` was kept OUT of the lock because it
            # walks ~94 GB; this is three orders of magnitude smaller and, unlike a size
            # measurement, is a correctness gate that has to hold at the commit point.
            if build_artifact_path:
                try:
                    provenance_report = verify_build_artifact(
                        source_map, build_artifact_path,
                        segment_id=plan["new_segment_id"],
                        block_path=_seg_path(root, new_segment), where="at publication",
                        source_reader=None if unsafe_skip_source_recheck else _source_reader())
                except sp.ProvenanceError as exc:
                    raise PublishRefused(f"at publication: {exc}") from exc
                if unsafe_skip_source_recheck:
                    # The block was checked; the sources were not. Saying `verified` here would
                    # claim the guarantee whose whole point is that a source can change.
                    provenance_report.update(
                        {"verified": False, "source_recheck": "waived",
                         "reason": "unsafe_skip_source_recheck=True: the block's bytes were "
                                   "checked against the artifact, but no source was re-read, "
                                   "so `source changed -> refuse` does NOT hold for this "
                                   "publication"})
                else:
                    provenance_report.update({"verified": True,
                                              "source_recheck": "performed"})
            else:
                # The SAME key set as every other outcome. A consumer branching on the waiver
                # type to know which fields exist would be reading the report to find out how
                # to read the report; the counts are 0 because nothing was checked, which is
                # the honest value rather than an absent one.
                provenance_report = {"verified": False, "days": 0, "sources_rechecked": 0,
                                     "source_recheck": "waived",
                                     "reason": "unsafe_skip_provenance=True: no provenance "
                                               "artifact was verified, so neither `block bytes "
                                               "match the artifact` nor `source changed -> "
                                               "refuse` holds for this publication"}

            # Hold the reservation right up to the commit, and prove it is still ours: a build
            # that rm+recreated the lock file could otherwise lock a fresh inode while we hold
            # the orphaned one. `assert_still_held` is a no-op for the waived-lock test path.
            if compaction_guard is not None:
                compaction_guard.assert_still_held()

            archive = bm.publish(root, manifest)
            # Everything logged and returned is read off the manifest that was actually
            # committed and the recomposition that produced it. Reading `plan["generation"]` or
            # `plan["superseded_now"]` would let an edited plan describe a publication that did
            # not happen -- an audit trail claiming generation 999 while the manifest says 2 is
            # worse than no trail, because it is believed.
            superseded_now = None if replaced is None else replaced["segment_id"]
            _append_log(root, MANIFEST_LOG, {
                "op": "manifest_publish", "at_utc": _iso(now), "operator": operator,
                "from_generation": int(live["generation"]),
                "to_generation": int(manifest["generation"]),
                "new_segment": new_segment["segment_id"], "superseded": superseded_now})
            return {"status": "published", "published": True,
                    "generation": int(manifest["generation"]),
                    "superseded": superseded_now, "archive": archive, "root": root,
                    "provenance": provenance_report,
                    "note": ("The served view is UNCHANGED at this instant: the manifest does "
                             "not describe delta, and delta still wins for the folded days "
                             "(§4.0).")}
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)
            lk.close()
    finally:
        # Release the compaction reservation only AFTER the ingest lock is dropped and the
        # commit is done -- the whole point is that no build can start while the block is being
        # referenced. A BORROWED lock is never released here: its owner is still using it, and
        # releasing another component's reservation is how a "held throughout" guarantee turns
        # into a gap nobody sees.
        if compaction_guard is not None and not borrowed:
            compaction_guard.release()


def _source_reader():
    """A fresh `build_block._SourceReader`, imported lazily to keep the module import cheap."""
    from ingest.build_block import _SourceReader
    return _SourceReader()


def _seg_path(root: str, seg: dict) -> str:
    p = seg["path"]
    return p if os.path.isabs(p) else os.path.join(root, p)


# --------------------------------------------------------------------------- lifecycle §8.5
def advance_lifecycle(root: str, *, hold_root: str, now: Optional[datetime] = None,
                      operator: str = "", apply: bool = False,
                      ingest_lock_path: Optional[str] = None) -> dict:
    """`referenced` → `releasable` → `held`. Never deletes.

    A superseded block has three live uses after it leaves `segments`: an in-flight snapshot
    still references it, the next tail fold reads its carried-forward days from it, and §9.3
    needs it as the rollback target. `release_after_utc` is what keeps all three safe, so the
    transition is time-gated and the terminal state is `held`, not `gone`.

    `apply=False` (the default) reports the transitions without performing them. Moving a block
    is the one action here that touches bytes on disk, so it does not happen by accident.

    **`apply=True` requires the ingest lock**, and holds it across both the renames and the
    manifest publish. Without it two hazards are open at once: this reads the live manifest,
    mutates `superseded[]` and publishes a new generation, so a concurrent publication would
    make one of the two writers' changes vanish; and the rename would race a publication that
    is checking whether a referenced block exists.

    **The rename and the publish are made atomic-or-undone.** A rename that succeeds followed
    by a publish that fails would leave the manifest naming a `current_path` that no longer
    holds the block -- and `current_path` is exactly what §9.3a restore-before-rollback looks
    up, so the block would still be on disk and still be unreachable. Every rename is therefore
    undone if the publish raises, and a divergence found on entry is **refused**, not silently
    repaired: a `current_path` that does not exist may equally mean someone deleted the block,
    and guessing between the two is how a rollback target quietly disappears.
    """
    if apply and not ingest_lock_path:
        raise PublishRefused(
            "advance_lifecycle(apply=True) requires ingest_lock_path: it publishes a manifest "
            "generation and moves blocks, and both must be exclusive with the daily append "
            "and with compaction's publication (§8.6)")
    if not apply:
        return _advance_lifecycle(root, hold_root=hold_root, now=now, operator=operator,
                                  apply=False)
    lk = open(ingest_lock_path, "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX)
        return _advance_lifecycle(root, hold_root=hold_root, now=now, operator=operator,
                                  apply=True)
    finally:
        fcntl.flock(lk, fcntl.LOCK_UN)
        lk.close()


def _advance_lifecycle(root: str, *, hold_root: str, now: Optional[datetime] = None,
                       operator: str = "", apply: bool = False) -> dict:
    now = now or _utcnow()
    live = bm.load_live(root)
    bm.validate_manifest(live)
    referenced_ids = {s["segment_id"] for s in live["segments"]}

    transitions, blocked = [], []
    entries = [dict(e) for e in live["superseded"]]

    # A `current_path` the manifest names but that is not on disk is the split state a crashed
    # apply would leave. Refuse rather than repair: the same symptom is produced by a deletion,
    # and a wrong guess retires a rollback target.
    for e in entries:
        cp = _seg_path(root, {"path": e["current_path"]})
        if not os.path.exists(cp):
            raise PublishRefused(
                f"superseded block {e['segment_id']!r} is recorded at {cp} but nothing is "
                f"there. Either an apply crashed between the rename and the publish, or the "
                f"block was deleted. These need different answers, so this refuses instead of "
                f"guessing: look for it under {hold_root} and correct the manifest, or accept "
                f"that rollback past generation {e['superseded_at_generation']} is gone.")
    for e in entries:
        if e["segment_id"] in referenced_ids:
            # Cannot happen through plan_publication, but a hand-edited manifest could do it,
            # and releasing a block the live generation still names is the S8a fabricated-null.
            blocked.append({"segment_id": e["segment_id"],
                            "reason": "still referenced by the live generation's segments"})
            continue
        if e["status"] == "referenced":
            if now >= _parse_iso(e["release_after_utc"]):
                transitions.append({"segment_id": e["segment_id"], "from": "referenced",
                                    "to": "releasable"})
            else:
                blocked.append({"segment_id": e["segment_id"],
                                "reason": f"release_after_utc {e['release_after_utc']} "
                                          f"not reached"})
        elif e["status"] == "releasable":
            transitions.append({"segment_id": e["segment_id"], "from": "releasable",
                                "to": "held"})
        elif e["status"] == "held":
            hard = _parse_iso(e["hold_until_utc"])
            blocked.append({"segment_id": e["segment_id"],
                            "reason": ("terminal state: hard delete is an ops-only manual act "
                                       f"after {e['hold_until_utc']}"
                                       + ("" if now >= hard else " (not yet reached)"))})

    if not apply:
        return {"status": "planned", "transitions": transitions, "blocked": blocked,
                "applied": False}

    moved, done = [], []
    try:
        for tr in transitions:
            e = next(x for x in entries if x["segment_id"] == tr["segment_id"])
            if tr["to"] == "releasable":
                e["status"] = "releasable"
                continue
            src = _seg_path(root, {"path": e["current_path"]})
            os.makedirs(hold_root, exist_ok=True)
            dst = os.path.join(hold_root, os.path.basename(src.rstrip("/")))
            if os.path.exists(dst):
                raise PublishRefused(f"hold path already occupied: {dst}")
            os.rename(src, dst)                 # same filesystem: content is never altered
            done.append((src, dst))
            e["status"] = "held"
            e["current_path"] = dst
            moved.append({"segment_id": e["segment_id"], "from": src, "to": dst})

        gen = int(live["generation"]) + 1
        candidate = dict(live)
        candidate["generation"] = gen
        candidate["generation_id"] = str(uuid.uuid4())
        candidate["created_utc"] = _iso(now)
        candidate["predecessor_generation"] = int(live["generation"])
        candidate["predecessor_manifest"] = bm.archive_name(int(live["generation"]))
        candidate["superseded"] = entries
        candidate["manifest_checksum"] = ""
        candidate["manifest_checksum"] = bm.compute_checksum(candidate)
        bm.validate_manifest(candidate)
        bm.publish(root, candidate)             # <- the commit point for the whole transition
    except BaseException:
        # Undo the renames. The manifest was not updated, so leaving the blocks in hold would
        # make every one of them unreachable through the path the manifest still names.
        for src, dst in reversed(done):
            if os.path.exists(dst) and not os.path.exists(src):
                os.rename(dst, src)
        raise

    # Logged only after the manifest commits, so the audit trail never claims a move that was
    # rolled back.
    for m in moved:
        _append_log(root, HOLD_LOG, {"op": "hold_move", "at_utc": _iso(now),
                                     "operator": operator, "segment_id": m["segment_id"],
                                     "original_path": next(
                                         e["path"] for e in entries
                                         if e["segment_id"] == m["segment_id"]),
                                     "from": m["from"], "to": m["to"]})
    return {"status": "applied", "transitions": transitions, "blocked": blocked,
            "moved": moved, "applied": True, "generation": gen}


# --------------------------------------------------------------------------- rollback §9.3a
def restore_before_rollback(root: str, generation: int, *, hold_root: str,
                            operator: str = "", now: Optional[datetime] = None,
                            apply: bool = False) -> dict:
    """§9.3a. Locate and restore blocks that generation `N` names but that are in hold.

    Order is mandatory and the reason is specific: replacing `manifest.json` first would
    publish a generation whose snapshot build immediately fails closed, leaving the service
    pinned to a stale in-memory snapshot with **no valid manifest on disk**. So: restore, verify
    the restored block, and only then move the pointer.

    A restored block is verified -- readable, `attrs["days"]` matching generation `N`'s declared
    day set, `day_digest` matching. A mismatch is a hard stop requiring human triage; there is
    no "restore anyway".
    """
    now = now or _utcnow()
    arch = os.path.join(root, bm.archive_name(generation))
    if not os.path.isfile(arch):
        raise PublishRefused(f"no archive for generation {generation}: {arch}")
    with open(arch) as fh:
        target = json.load(fh)
    bm.validate_manifest(target)
    live = bm.load_live(root)
    by_id = {e["segment_id"]: e for e in live["superseded"]}

    needed, unavailable = [], []
    for seg in target["segments"]:
        path = _seg_path(root, seg)
        if os.path.exists(path):
            continue
        entry = by_id.get(seg["segment_id"])
        if entry is None or not os.path.exists(entry.get("current_path", "")):
            unavailable.append({
                "segment_id": seg["segment_id"], "expected_path": path,
                "reason": ("not present and not locatable in hold -- if it was hard-deleted, "
                           "rollback to this generation is no longer available and recovery "
                           "is the P4-S4 §10 rebuild ladder")})
            continue
        needed.append({"segment_id": seg["segment_id"], "from": entry["current_path"],
                       "to": path, "declared_days": seg["day_count"],
                       "day_digest": seg["fingerprint"]["day_digest"]})

    if unavailable:
        return {"status": "refused", "restored": [], "unavailable": unavailable,
                "rollback_available": False}
    if not apply:
        return {"status": "planned", "to_restore": needed, "unavailable": [],
                "rollback_available": True, "applied": False}

    restored = []
    for item in needed:
        os.rename(item["from"], item["to"])      # same-filesystem rename: bytes unchanged
        insp = bm.inspect_store_contract(item["to"])
        if bm.day_digest(insp.days) != item["day_digest"]:
            os.rename(item["to"], item["from"])  # put it back; do not leave a half state
            raise PublishRefused(
                f"restored block {item['segment_id']} does not match generation {generation}'s "
                f"declared day set (day_digest mismatch). Hard stop -- this needs human "
                f"triage, never a restore-anyway.")
        restored.append(item)
        _append_log(root, HOLD_LOG, {"op": "hold_restore", "at_utc": _iso(now),
                                     "operator": operator, "segment_id": item["segment_id"],
                                     "from": item["from"], "to": item["to"],
                                     "reason": f"rollback to generation {generation}"})
    return {"status": "restored", "restored": restored, "unavailable": [],
            "rollback_available": True, "applied": True}


def execute_rollback(root: str, generation: int, *, hold_root: str, ingest_lock_path: str,
                     operator: str = "", reason: str = "",
                     now: Optional[datetime] = None) -> dict:
    """§9.3 with the §9.3a precondition enforced, not assumed.

    `bm.rollback_to` does the copy → verify → fsync → `os.replace`, leaving the archive present
    and unmodified. This wrapper is what guarantees the blocks are there first."""
    now = now or _utcnow()
    lk = open(ingest_lock_path, "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX)
        pre = restore_before_rollback(root, generation, hold_root=hold_root,
                                      operator=operator, now=now, apply=True)
        if not pre["rollback_available"]:
            return {"status": "refused", "rolled_back": False,
                    "reason": "a block generation %d references is unavailable" % generation,
                    "unavailable": pre["unavailable"]}
        from_gen = int(bm.load_live(root)["generation"])
        arch = bm.rollback_to(root, generation)
        _append_log(root, MANIFEST_LOG, {
            "op": "manifest_rollback", "at_utc": _iso(now), "operator": operator,
            "from_generation": from_gen, "to_generation": generation, "reason": reason,
            "restored_blocks": [r["segment_id"] for r in pre["restored"]]})
        return {"status": "rolled_back", "rolled_back": True, "generation": generation,
                "archive": arch, "restored": pre["restored"]}
    finally:
        fcntl.flock(lk, fcntl.LOCK_UN)
        lk.close()


# --------------------------------------------------------------------------- WAL reporting
def prune_outlook(root: str, days: Sequence[str], *, wal_root: str,
                  materialized_repairs: Optional[dict] = None) -> dict:
    """Report which of `days` a subsequent prune could touch. **Reporting only.**

    Publication never needs this: publishing a block changes no served value, because the
    manifest does not describe delta and delta still wins for the folded days. It is here so an
    operator can see, at publish time, that a corrected day is still blocked -- rather than
    discovering it when the prune refuses.

    A whole-log integrity failure is returned as a refusal, not raised: the caller is reporting,
    and an unreadable WAL means "no day is authorized", which is an answer."""
    try:
        state = rw.read_wal(wal_root)
    except rw.WalCorrupt as exc:
        return {**rw.refuse_all_reason(exc), "authorized": [], "refused":
                {d: "the repair WAL does not validate; no day can be authorized" for d in days}}
    return rw.prune_authorization(state, days, materialized_repairs=materialized_repairs)
