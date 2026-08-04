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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from store import block_manifest as bm         # noqa: E402
from store import repair_wal as rw            # noqa: E402
from store.compaction_lock import refuse_if_compaction_running   # noqa: E402

#: §8.5 -- must exceed refresh TTL + max request duration + margin, AND the realistic
#: discover-a-defect window, because a superseded block is also the rollback target.
DEFAULT_RELEASE_AFTER_S = 24 * 3600
DEFAULT_HOLD_DAYS = 14                          # P4-S8 §7

HOLD_LOG = "p5_hold.jsonl"
MANIFEST_LOG = "p5_manifest.jsonl"


class PublishRefused(Exception):
    """Nothing was written. The live manifest is byte-unchanged."""


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
        superseded.append({
            "segment_id": replaced["segment_id"], "path": replaced["path"],
            "superseded_at_generation": gen,
            "release_after_utc": _iso(now + timedelta(seconds=release_after_s)),
            "hold_until_utc": _iso(now + timedelta(days=hold_days)),
            "status": "referenced",
            "current_path": replaced["path"],
            "bytes": int(plan.get("predecessor_bytes", 0)),
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

    # The guard's map is the segment's OWN provenance -- the copy that gets published and that
    # an auditor will later read -- not the plan's convenience copy. If the two disagree, the
    # plan is not describing the block it is about to publish, and a guard run against the
    # wrong map would pass while the published record says something else.
    provenance = dict((segment.get("build_provenance") or {}).get("sources") or {})
    if not provenance:
        raise PublishRefused(
            "the segment carries no build_provenance.sources, so there is nothing for the "
            "staleness guard to re-verify. A block published without provenance cannot be "
            "shown to have read what it claims (§5, P5-S3 round 4).")
    top = dict(plan.get("source_map") or {})
    if top and top != provenance:
        only_top = sorted(set(top) - set(provenance))
        only_prov = sorted(set(provenance) - set(top))
        differing = sorted(d for d in set(top) & set(provenance) if top[d] != provenance[d])
        raise PublishRefused(
            f"the plan's source_map and the segment's build_provenance.sources disagree "
            f"(only in plan: {only_top[:3]}, only in provenance: {only_prov[:3]}, differing: "
            f"{differing[:3]}). The staleness guard would be checking a map the published "
            f"manifest does not contain.")

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


# --------------------------------------------------------------------------- staleness guard
def staleness_guard(source_map: dict, *, delta_path: Optional[str],
                    live_days: Sequence[str]) -> Optional[dict]:
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

    Non-delta sources are checked at the **exact group**, not the root: a daily root exists for
    years, so `os.path.exists(root)` proves nothing about whether `YYYY/MM/DD` is still there.
    """
    live = list(live_days)
    if len(live) != len(set(live)):
        dups = sorted({d for d in live if live.count(d) > 1})
        return {"status": "aborted_stale", "reason": f"live delta has duplicate days: {dups}",
                "published": False}
    live_index = {d: i for i, d in enumerate(live)}
    live_real = os.path.realpath(delta_path) if delta_path else None

    absent, retargeted, moved, missing_src = [], [], [], []
    for day, rec in sorted(source_map.items()):
        kind = rec.get("source_kind")
        if kind == "delta":
            if delta_path is None or day not in live_index:
                absent.append(day)
            elif rec.get("source_path") != live_real:
                retargeted.append((day, rec.get("source_path"), live_real))
            elif int(rec.get("source_day_index", -1)) != live_index[day]:
                moved.append((day, rec.get("source_day_index"), live_index[day]))
        else:
            path = _source_group_path(kind, rec.get("source_path"), day)
            if not path or not os.path.isdir(path):
                missing_src.append((day, path))

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
    return None


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
def execute_publication(plan: dict, *, ingest_lock_path: str, delta_path: Optional[str] = None,
                        compaction_lock_path: Optional[str] = None,
                        operator: str = "", now: Optional[datetime] = None) -> dict:
    """§7.4 steps 1-4. The commit point is `os.replace`, inside `bm.publish`.

    Refuses without writing anything on: a running compaction, a missing referenced block, a
    staleness drift, or any validation failure. `manifest.json` is byte-unchanged on every
    refusal path -- the archive for `N+1` is written first and is *unreferenced* until the
    replace, which is exactly the §9 "safe" crash state.
    """
    now = now or _utcnow()
    root = plan["root"]

    if compaction_lock_path:
        # A build holding the lock may still be writing the very block we are about to
        # reference. Publication is not the operation that should race it.
        busy = refuse_if_compaction_running(compaction_lock_path, operation="manifest_publish")
        if busy:
            return {**busy, "published": False}

    os.makedirs(root, exist_ok=True)
    lk = open(ingest_lock_path, "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX)          # publish and the daily append are exclusive

        live = bm.load_live(root)
        if int(live["generation"]) != int(plan["predecessor_generation"]):
            return {"status": "aborted_stale", "published": False,
                    "reason": (f"the manifest moved while this plan was held: planned against "
                               f"generation {plan['predecessor_generation']}, live is "
                               f"{live['generation']}. Re-plan against the current generation.")}

        drift = staleness_guard(plan["source_map"], delta_path=delta_path,
                                live_days=_read_delta_days(delta_path))
        if drift:
            return drift

        # Every block the NEW generation references must exist before we point at it.
        missing = [s["path"] for s in plan["manifest"]["segments"]
                   if not os.path.exists(_seg_path(root, s))]
        if missing:
            return {"status": "refused", "published": False,
                    "reason": (f"generation {plan['generation']} references block(s) that are "
                               f"not present: {missing[:3]}. Publishing would produce a "
                               f"manifest whose snapshot build fails closed.")}

        archive = bm.publish(root, plan["manifest"])
        _append_log(root, MANIFEST_LOG, {
            "op": "manifest_publish", "at_utc": _iso(now), "operator": operator,
            "from_generation": plan["predecessor_generation"],
            "to_generation": plan["generation"],
            "new_segment": plan["new_segment_id"], "superseded": plan["superseded_now"]})
        return {"status": "published", "published": True, "generation": plan["generation"],
                "archive": archive, "root": root,
                "note": ("The served view is UNCHANGED at this instant: the manifest does not "
                         "describe delta, and delta still wins for the folded days (§4.0).")}
    finally:
        fcntl.flock(lk, fcntl.LOCK_UN)
        lk.close()


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
