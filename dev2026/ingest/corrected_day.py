"""dev2026 — P5-S5 Part 2: the corrected-day lifecycle (design spec §7.5a, §7.8, §7.8a).

**The rule this module exists to enforce**, and the only sentence in it that matters:

> A repaired day may leave delta **only** when E1 repair-identity authorization and §7.8a
> Phase A base-only verification have **both** passed. Neither alone is sufficient, and no
> other evidence substitutes for either.

Why both, when either looks like enough:

* **E1 alone** proves the block *claims* to have materialized the right repair — a `repair_id`
  in `build_provenance.materialized_repairs` matching the WAL's latest commit. It is a
  statement in a document about another document. It does not read the block.
* **Phase A alone** proves the base *currently* serves a value equal to the repaired delta day.
  It cannot distinguish "the correction was folded" from "the two happen to agree", and on a
  sampled comparison "happen to agree" is not exotic — an all-NaN region agrees with anything
  else all-NaN.

So E1 is the deterministic identity and Phase A is the read that makes it about bytes.

**The trap §7.8a exists to close.** The round-5 spec draft said a corrective refold is proven
when "a point GET returns the corrected value". Pre-prune the repaired day is *still in delta*
and `TieredCube` is delta-wins, so a public GET returns **delta** — it would have passed whether
or not the refold worked. Every check here is therefore made through a **base-only**
`SegmentedCubeStore`, which by construction never sees delta (§4.0/§6.1).

**What is NOT here.** Nothing in this module prunes, publishes or writes to a store. It answers
"may this day be dropped", and `prune_delta` refuses when the answer is no.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from typing import Dict, List, Optional, Sequence

import numpy as np
import zarr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ingest.build_block import VARS               # noqa: E402  -- one canonical variable set
from store import block_manifest as bm            # noqa: E402
from store import repair_wal as rw                # noqa: E402
from store import source_provenance as sp         # noqa: E402
from store.segmented_cube import SegmentedCubeStore   # noqa: E402


class CorrectedDayRefused(Exception):
    """A corrected day is not eligible to leave delta. Nothing was pruned."""


# --------------------------------------------------------------------------- day-stable sample
def date_slot(day: str) -> int:
    """A sample-window selector derived from the DATE, not from a physical index.

    The block-provenance artifact keys its window on the block's `day_index`, which is correct
    there: it describes one block. A repair fingerprint has to be comparable across *different*
    stores — the repaired delta day at one index, the refolded block's copy at another — so a
    physical index would make the two sides sample different cells and disagree for a reason
    that is not a difference. The date is the one coordinate both stores agree on."""
    y, m, d = (int(x) for x in day.split("-"))
    return (date(y, m, d) - date(1970, 1, 1)).days


def fingerprint_day(store_path: str, day: str, *, physical_index: int) -> tuple:
    """`(fingerprint, var_valid)` for one day of any cube-shaped store, on the date-keyed window.

    Used by three callers that must agree: the repair tooling when it commits to the WAL, the
    corrective refold when it records what it materialized, and the prune gate when it checks.
    One function so they cannot drift."""
    insp = bm.inspect_store_contract(store_path)
    if physical_index < 0 or physical_index >= len(insp.days):
        raise CorrectedDayRefused(
            f"{store_path}: physical index {physical_index} is out of range for "
            f"{len(insp.days)} day(s)")
    if insp.days[physical_index] != day:
        raise CorrectedDayRefused(
            f"{store_path}: index {physical_index} holds {insp.days[physical_index]!r}, not "
            f"{day!r}. A fingerprint taken at the wrong slot describes the wrong day.")
    slot = date_slot(day)
    i0, i1, j0, j1 = sp.sample_window(insp.ny, insp.nx, seed=sp.SAMPLE_SEED, day_index=slot)
    valid_map = {v: list(f) for v, f in insp.var_valid}
    g = zarr.open_group(store_path, mode="r")
    tiles, valid = {}, {}
    for var in VARS:
        flags = valid_map.get(var, [])
        present = bool(flags) and physical_index < len(flags) and flags[physical_index] is True
        valid[var] = present
        tiles[var] = (np.asarray(g[var][physical_index, i0:i1, j0:j1]) if present else None)
    return sp.window_fingerprint(tiles, seed=sp.SAMPLE_SEED, day_index=slot,
                                 var_valid=valid), valid, tiles


def _day_index(store_path: str, day: str) -> Optional[int]:
    insp = bm.inspect_store_contract(store_path)
    try:
        return list(insp.days).index(day)
    except ValueError:
        return None


# --------------------------------------------------------------------------- E2
def e2_parity(day: str, *, delta_path: str, base_store_path: str) -> List[str]:
    """§7.5a E2 — seeded, point-wise, float32-semantic, NaN-aware, `var_valid`-aware.

    Compares the **delta** day against the **base** copy of the same day and returns the
    differences, empty when they agree. Never a full slab: one date-keyed window per store, the
    P4-S6 OOM lesson kept executable.

    E2 is *defence in depth, never sufficient alone* (§7.5a). A mismatch refuses regardless of
    E1 — it means either a repair that bypassed the WAL or a genuine base defect, and both need
    a human. A match authorizes nothing on its own."""
    d_idx = _day_index(delta_path, day)
    b_idx = _day_index(base_store_path, day)
    if d_idx is None:
        return [f"{day} is not present in the delta at {delta_path}"]
    if b_idx is None:
        return [f"{day} is not present in the base block at {base_store_path}"]
    _, d_valid, d_tiles = fingerprint_day(delta_path, day, physical_index=d_idx)
    _, b_valid, b_tiles = fingerprint_day(base_store_path, day, physical_index=b_idx)
    return sp.compare_days(d_tiles, b_tiles, seed=sp.SAMPLE_SEED, day_index=date_slot(day),
                           a_valid=d_valid, b_valid=b_valid)


# --------------------------------------------------------------------------- §7.8a Phase A
def phase_a_base_only(day: str, *, manifest_root: str, delta_path: str, wal_root: str,
                      anchor_root: Optional[str] = None,
                      expected_segment_id: Optional[str] = None,
                      allowed_legacy_paths: Optional[Sequence[str]] = None,
                      unsafe_allow_colocated_anchor: bool = False) -> dict:
    """§7.8a **Phase A** — the proof, taken pre-prune and BASE-ONLY.

    Four checks, in the spec's order. Each answers something the others cannot:

    1. **the manifest resolves the day to the corrected version** — not to the superseded one.
       A refold that published but did not take effect looks identical from the outside;
    2. **the base value equals the repaired delta day** (E2). This is the assertion that the
       correction was materialized rather than merely referenced;
    3. **provenance identity** — the covering segment's
       `build_provenance.materialized_repairs[D].repair_id` equals the WAL's
       `latest_committed(D)`, and the recorded fingerprint equals the WAL's committed one;
    4. **the day is still in delta.** Phase A is defined pre-prune; running it after the fact
       would compare base against nothing and pass vacuously.

    A public point GET is deliberately **not** consulted. Pre-prune it returns the delta value
    by delta-precedence, so it would pass whether or not the refold worked — the exact trap
    §7.8a was written to close. The `SegmentedCubeStore` never sees delta by construction
    (§4.0/§6.1), so a read through it is a base read.

    Raises `CorrectedDayRefused` on any failure. Returns the evidence record on success.
    """
    if _day_index(delta_path, day) is None:
        raise CorrectedDayRefused(
            f"{day} is not in the delta at {delta_path}. Phase A is a PRE-prune check: with "
            f"the day already gone there is nothing to compare base against, and a comparison "
            f"against nothing passes vacuously.")

    store = SegmentedCubeStore(manifest_root, allowed_legacy_paths=allowed_legacy_paths)
    try:
        seg_idx, _local = store.resolve(day)
    except Exception as exc:
        raise CorrectedDayRefused(
            f"the published manifest does not resolve {day} to any segment ({exc}); the "
            f"corrective refold has not taken effect") from exc
    segment_id = store.segment_id(seg_idx)
    if expected_segment_id is not None and segment_id != expected_segment_id:
        raise CorrectedDayRefused(
            f"the manifest resolves {day} to segment {segment_id!r}, not the corrected version "
            f"{expected_segment_id!r}. A refold that published but did not take effect looks "
            f"identical from the outside; this is the check that tells them apart.")

    live = bm.load_live(manifest_root)
    seg = next((s for s in live["segments"] if s["segment_id"] == segment_id), None)
    if seg is None:                                # cannot happen via resolve(); fail closed
        raise CorrectedDayRefused(f"segment {segment_id!r} is not in the live manifest")
    block_path = seg["path"] if os.path.isabs(seg["path"]) else os.path.join(manifest_root,
                                                                            seg["path"])

    diffs = e2_parity(day, delta_path=delta_path, base_store_path=block_path)
    if diffs:
        raise CorrectedDayRefused(
            f"E2 parity failed for {day}: base does not match the repaired delta day "
            f"({'; '.join(diffs[:3])}). Either the correction was not materialized, a repair "
            f"bypassed the WAL, or base is defective -- all three need a human.")

    identity = _verify_repair_identity(
        day, seg=seg, wal_root=wal_root, delta_path=delta_path, anchor_root=anchor_root,
        manifest_root=manifest_root,
        unsafe_allow_colocated_anchor=unsafe_allow_colocated_anchor)
    return {"day": day, "segment_id": segment_id, "block_path": block_path,
            "e2": "match", **identity, "phase": "A", "verified_pre_prune": True}


def _verify_repair_identity(day: str, *, seg: dict, wal_root: str, delta_path: str,
                            anchor_root: Optional[str] = None,
                            manifest_root: Optional[str] = None,
                            unsafe_allow_colocated_anchor: bool = False) -> dict:
    """§7.5a E1 against the covering segment. Identity, never time.

    Re-reads the WAL **through the same anchor domain** the rest of the workflow uses. Reading
    it without one here would let Phase A pass on a log the prune gate would refuse -- the
    protection present at one stage and absent at the next."""
    resolved = rw.resolve_anchor_root(
        wal_root, anchor_root, unsafe_allow_colocated_anchor=unsafe_allow_colocated_anchor)
    state = rw.read_wal(wal_root, anchor_root=resolved)   # WalCorrupt -> caller refuses
    if manifest_root is not None:
        state.assert_bound_to(
            manifest_root, None if (anchor_root is None and unsafe_allow_colocated_anchor)
            else resolved)
    state.assert_fresh()
    latest = state.latest_committed(day)
    if latest is None:
        return {"repair_id": None, "identity": "day was never repaired"}

    mr = ((seg.get("build_provenance") or {}).get("materialized_repairs") or {})
    entry = mr.get(day)
    if not entry:
        raise CorrectedDayRefused(
            f"repair {latest.repair_id} is committed for {day} but the covering segment "
            f"{seg['segment_id']!r} records no materialized repair for it. The correction has "
            f"not been folded into base (§7.8).")
    if entry.get("repair_id") != latest.repair_id:
        raise CorrectedDayRefused(
            f"segment {seg['segment_id']!r} materialized repair {entry.get('repair_id')!r} for "
            f"{day} but the latest committed repair is {latest.repair_id!r}; a later repair "
            f"supersedes what the block carries. Run a fresh corrective refold.")
    if entry.get("source_kind") != "delta":
        raise CorrectedDayRefused(
            f"segment {seg['segment_id']!r} records {day} as materialized from "
            f"{entry.get('source_kind')!r}; a corrective refold reads the repaired day from "
            f"delta by construction (§7.1a)")
    if entry.get("source_fingerprint") != latest.fingerprint:
        raise CorrectedDayRefused(
            f"segment {seg['segment_id']!r} names repair {latest.repair_id} for {day} but its "
            f"recorded fingerprint does not match the WAL's committed fingerprint. The id "
            f"agrees and the bytes do not, which is worse than a plain mismatch.")
    return {"repair_id": latest.repair_id, "identity": "matches latest committed",
            "fingerprint": latest.fingerprint}


# --------------------------------------------------------------------------- the prune gate
def prune_eligibility(days: Sequence[str], *, manifest_root: str, delta_path: str,
                      wal_root: str, anchor_root: Optional[str] = None,
                      allowed_legacy_paths: Optional[Sequence[str]] = None,
                      unsafe_allow_colocated_anchor: bool = False) -> dict:
    """May these delta days be dropped? `{"authorized": [...], "refused": {day: reason}}`.

    **E2 for every dropped day; E1 and Phase A additionally for every repaired one.** The WAL's
    silence about a day is not evidence that nothing happened to it — a repair applied straight
    to delta leaves exactly that silence — so an unrepaired day still has to match base (§7.5a
    E2). A day the WAL *does* know about needs E1 identity **and** a full Phase A pass on top,
    and a refusal from any of them is a refusal.

    Never raises for a refusal: a refused day stays in delta, where it is served correctly. Only
    a WAL that does not parse raises, and that refuses every day rather than some.
    """
    try:
        resolved = rw.resolve_anchor_root(
            wal_root, anchor_root,
            unsafe_allow_colocated_anchor=unsafe_allow_colocated_anchor)
        state = rw.read_wal(wal_root, anchor_root=resolved)
        # An ABSENT or unbound log is not an empty one. Read as "no repairs" it authorizes
        # every day, and that failure is silent, total, and indistinguishable from a healthy
        # deployment that has genuinely had none.
        state.assert_bound_to(
            manifest_root,
            None if (anchor_root is None and unsafe_allow_colocated_anchor) else resolved)
        state.assert_fresh()
    except (rw.WalCorrupt, rw.WalNotInitialized, rw.WalRolledBack) as exc:
        return {"authorized": [], "refused": {d: f"the repair WAL cannot be trusted ({exc})"
                                              for d in days},
                "wal": "untrustworthy"}

    # E1 first: it needs no disk reads beyond the WAL, and a day it refuses need not be probed.
    e1 = rw.prune_authorization(state, days,
                                materialized_repairs=_materialized_map(manifest_root, days))
    authorized, refused = [], dict(e1["refused"])
    for day in e1["authorized"]:
        repaired = state.latest_committed(day) is not None
        try:
            if repaired:
                # E1 identity AND the full Phase A, which includes E2.
                phase_a_base_only(
                    day, manifest_root=manifest_root, delta_path=delta_path,
                    wal_root=wal_root, anchor_root=anchor_root,
                    allowed_legacy_paths=allowed_legacy_paths,
                    unsafe_allow_colocated_anchor=unsafe_allow_colocated_anchor)
            else:
                # E2 runs for EVERY dropped day, not only for days the WAL knows about. A day
                # with no committed repair is not a day that was never edited -- it is a day the
                # WAL has no opinion about, and a repair applied straight to delta without a WAL
                # record leaves exactly that trace. Skipping E2 here made the one case E2 is
                # documented to catch -- "a repair that bypassed the WAL" -- the one case it
                # could not see.
                _e2_against_base(day, manifest_root=manifest_root, delta_path=delta_path,
                                 allowed_legacy_paths=allowed_legacy_paths)
        except (CorrectedDayRefused, rw.WalError) as exc:
            refused[day] = (f"§7.8a Phase A did not pass: {exc}" if repaired
                            else f"§7.5a E2 parity did not pass: {exc}")
            continue
        authorized.append(day)
    return {"authorized": sorted(authorized), "refused": refused, "wal": "valid"}


def _e2_against_base(day: str, *, manifest_root: str, delta_path: str,
                     allowed_legacy_paths=None) -> None:
    """E2 for a day the WAL has no committed repair for. Refuses on any mismatch.

    A mismatch here means base and delta disagree about a day nothing claims to have repaired:
    either a repair went straight to delta without a WAL record, or base is defective. §7.5a
    says both need a human, and both must stop the prune."""
    store = SegmentedCubeStore(manifest_root, allowed_legacy_paths=allowed_legacy_paths)
    try:
        seg_idx, _ = store.resolve(day)
    except Exception as exc:
        raise CorrectedDayRefused(
            f"the published manifest does not resolve {day} to any segment ({exc})") from exc
    segment_id = store.segment_id(seg_idx)
    live = bm.load_live(manifest_root)
    seg = next((s for s in live["segments"] if s["segment_id"] == segment_id), None)
    if seg is None:
        raise CorrectedDayRefused(f"segment {segment_id!r} is not in the live manifest")
    block_path = seg["path"] if os.path.isabs(seg["path"]) else os.path.join(manifest_root,
                                                                            seg["path"])
    diffs = e2_parity(day, delta_path=delta_path, base_store_path=block_path)
    if diffs:
        raise CorrectedDayRefused(
            f"base and delta disagree about {day}, which no committed repair explains "
            f"({'; '.join(diffs[:3])}). Either a repair bypassed the WAL or base is defective; "
            f"§7.5a says both need a human.")


def _materialized_map(manifest_root: str, days: Sequence[str]) -> dict:
    """`build_provenance.materialized_repairs` of whichever segment covers each day."""
    live = bm.load_live(manifest_root)
    out = {}
    for seg in live["segments"]:
        mr = ((seg.get("build_provenance") or {}).get("materialized_repairs") or {})
        for day in days:
            if day in mr:
                out[day] = mr[day]
    return out


# --------------------------------------------------------------------------- §7.8 orchestration
def corrective_refold(*, day: str, manifest_root: str, delta_path: str, wal_root: str,
                      new_block_path: str, artifacts_dir: str, start_day: str, end_day: str,
                      classification_target: Sequence[str],
                      predecessor_segment_id: str, ingest_lock_path: str,
                      compaction_lock_path: Optional[str],
                      hard_reserve_bytes: int, lock=None,
                      predecessor_path: Optional[str] = None,
                      predecessor_present: Sequence[str] = (),
                      operator: str = "", release_after_s: Optional[int] = None,
                      hold_days: Optional[int] = None, anchor_root: Optional[str] = None,
                      unsafe_skip_isolation: bool = False,
                      unsafe_skip_compaction_lock: bool = False,
                      unsafe_allow_colocated_anchor: bool = False) -> dict:
    """§7.8 — the whole corrective refold, in the one order that is safe.

        build a NEW version of the same calendar window (delta-first)
          → publish it through the real §7.4 path
          → §7.8a PHASE A: base-only verification + provenance identity   ← the proof
          → (the caller may only now prune; §7.5a enforces it independently)

    Every step already refuses on its own, and out-of-order use is already caught — Phase A
    refuses post-prune, the prune gate refuses without Phase A. This exists because *knowing*
    the order and *encoding* it are different things: a runbook that says "then verify" is a
    step someone can skip under pressure, and the failure is silent until a correction is gone.

    It deliberately does **not** prune. Publication and pruning are separated by §7.5's
    ordering, and a function that did both would make the gate its own caller — the one
    arrangement in which a gate can be waived by the thing it gates.

    **One compaction reservation spans the whole sequence.** The build, the publication and
    Phase A all run under the same held `CompactionLock`, acquired once here. Letting
    publication take its own would mean releasing between build and publish — and that gap is
    precisely when a concurrent build could start rewriting the block this refold is about to
    reference. `execute_publication` therefore *borrows* the lock rather than acquiring one.

    The sealed predecessor is never touched (§7.8.1): a new version at a new path supersedes
    it, and it keeps serving until this returns.
    """
    from ingest.build_block import build_block         # local: build_block imports this module
    from ingest import publish_manifest as pub
    from store.compaction_lock import CompactionLock

    # A caller-supplied lock is verified against the canonical path, never trusted as "some
    # held lock". Ignoring `compaction_lock_path` when a lock is passed let an unrelated lock
    # satisfy the reservation while the real one stayed free.
    if lock is not None and not unsafe_skip_isolation:
        if not compaction_lock_path:
            raise CorrectedDayRefused(
                "compaction_lock_path is required even when a held lock is passed: it is what "
                "the lock's identity is checked against")
        if os.path.realpath(getattr(lock, "path", "")) != os.path.realpath(
                compaction_lock_path):
            raise CorrectedDayRefused(
                f"the supplied lock is on {getattr(lock, 'path', None)!r} but the canonical "
                f"reservation is {compaction_lock_path!r}; holding an unrelated lock leaves "
                f"the real one free for a build to take")
    owned = None
    if lock is None and compaction_lock_path and not unsafe_skip_isolation:
        owned = CompactionLock(compaction_lock_path,
                               holder="p5-corrective-refold").acquire()
        lock = owned
    try:
        return _corrective_refold(
            day=day, manifest_root=manifest_root, delta_path=delta_path, wal_root=wal_root,
            new_block_path=new_block_path, artifacts_dir=artifacts_dir, start_day=start_day,
            end_day=end_day, classification_target=classification_target,
            predecessor_segment_id=predecessor_segment_id,
            ingest_lock_path=ingest_lock_path,
            compaction_lock_path=compaction_lock_path,
            hard_reserve_bytes=hard_reserve_bytes, lock=lock,
            predecessor_path=predecessor_path, predecessor_present=predecessor_present,
            operator=operator, release_after_s=release_after_s, hold_days=hold_days,
            unsafe_skip_isolation=unsafe_skip_isolation,
            unsafe_skip_compaction_lock=unsafe_skip_compaction_lock,
            anchor_root=anchor_root,
            unsafe_allow_colocated_anchor=unsafe_allow_colocated_anchor,
            build_block=build_block, pub=pub)
    finally:
        if owned is not None:
            owned.release()


def _corrective_refold(*, day, manifest_root, delta_path, wal_root, new_block_path,
                       artifacts_dir, start_day, end_day, classification_target,
                       predecessor_segment_id, ingest_lock_path, compaction_lock_path,
                       hard_reserve_bytes, lock, predecessor_path, predecessor_present,
                       operator, release_after_s, hold_days, unsafe_skip_isolation,
                       unsafe_skip_compaction_lock, anchor_root,
                       unsafe_allow_colocated_anchor, build_block, pub):
    plan = build_block(new_block_path, start_day=start_day, end_day=end_day,
                       classification_target=list(classification_target),
                       predecessor_present=list(predecessor_present),
                       predecessor_path=predecessor_path, delta_path=delta_path,
                       artifacts_dir=artifacts_dir, lock=lock,
                       hard_reserve_bytes=hard_reserve_bytes, wal_root=wal_root,
                       anchor_root=anchor_root,
                       unsafe_allow_colocated_anchor=unsafe_allow_colocated_anchor,
                       unsafe_skip_isolation=unsafe_skip_isolation)
    segment = dict(plan["segment"])
    segment["supersedes"] = predecessor_segment_id
    plan["segment"] = segment

    if day not in (segment.get("build_provenance") or {}).get("materialized_repairs", {}):
        raise CorrectedDayRefused(
            f"the refold did not materialize a repair for {day}: the builder attests only days "
            f"it read FROM DELTA with a committed repair, so either the repair is not committed "
            f"or the day did not resolve to delta. Publishing this would supersede the sealed "
            f"version with one that carries no correction.")

    kw = {}
    if release_after_s is not None:
        kw["release_after_s"] = release_after_s
    if hold_days is not None:
        kw["hold_days"] = hold_days
    # The SAME reservation, borrowed -- not a second acquire, and not a gap.
    if lock is not None:
        lock.assert_still_held()
        kw["compaction_lock"] = lock
    kw["compaction_lock_path"] = compaction_lock_path
    published = pub.execute_publication(
        pub.plan_publication(manifest_root, plan),
        ingest_lock_path=ingest_lock_path, delta_path=delta_path,
        build_artifact_path=plan["provenance_path"], operator=operator,
        unsafe_skip_compaction_lock=unsafe_skip_compaction_lock, **kw)
    if not published.get("published"):
        raise CorrectedDayRefused(
            f"the corrective refold did not publish ({published.get('reason')}); the sealed "
            f"version is untouched and still serving")

    if lock is not None:
        lock.assert_still_held()        # still ours across Phase A, not merely across publish
    phase_a = phase_a_base_only(
        day, manifest_root=manifest_root, delta_path=delta_path, wal_root=wal_root,
        anchor_root=anchor_root, expected_segment_id=segment["segment_id"],
        unsafe_allow_colocated_anchor=unsafe_allow_colocated_anchor)
    return {"status": "refolded", "day": day, "segment_id": segment["segment_id"],
            "block_path": plan["out_path"], "publication": published, "phase_a": phase_a,
            "pruned": False,
            "note": ("Phase A passed, so §7.5a will now authorize this day. Pruning is a "
                     "separate operation on purpose: a function that did both would make the "
                     "gate its own caller.")}
