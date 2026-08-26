"""dev2026 — the one-shot compaction: build a block, publish it, verify, delete NOTHING.

This is the operation `ops/p5_oneshot_exception.OP_COMPACTION` names. It exists to make the
space in the old daily/base store *releasable*; it does not release it, and the first
implementation deliberately removes nothing at all.

## What it does, in order

    flock(p5_compaction.lock, LOCK_EX)              outermost, held across everything
      -> writer-fence probe BEFORE anything is read
      -> flock(mur_delta.ingest.lock, LOCK_EX)      BEFORE the scan, not before the publish
           -> measure the monolith HERE and mint the single-use compaction capability
           -> inventory every path that must not shrink
           -> build_block(...)                      the expensive scan
           -> re-probe the fence; it must not have moved
           -> re-measure the monolith; it must not have moved either
           -> PRE-PUBLISH gates: point-series parity against the PER-DAY oracle
              (first and last days included), source-map completeness, and
              base/delta disjointness on the PLANNED manifest. A failure here
              refuses with NOTHING published.
           -> execute_publication (borrowing both locks)
      <- ingest lock released
      -> POST-PUBLISH gates: manifest checksum/generation, WAL and anchor binding,
         API parity, fresh capacity, and a second inventory
      -> post_compaction_problem(evidence, expected=<this run's own state>)
      -> a finalization clearance, if and only if every gate passed
    release

## What it does NOT do, and why that is the design rather than an omission

**It deletes nothing.** No prune, no swap, no daily removal, no `GHRSST_BLOCK_MANIFEST_DIR`
switch. Publishing a new generation is reversible in a way that removing the data it was built
from is not, and the two decisions are different: "the new generation is trustworthy" is
established by the gates below, while "the old data should go" is a person's judgement about
retention and recovery that no amount of parity checking settles.

So the gates gate a **clearance**, not a deletion. `p5_oneshot_exception.refuse_destructive_step`
is the contract a future finalization must satisfy, and it requires the clearance *and* a
separate human approval. Nothing in this tree implements a destructive step.

**It does not switch the service.** The served view is unchanged at the instant of publication —
the manifest does not describe delta, and delta still wins for the folded days (§4.0). Pointing
the service at the new generation is a separate production change with its own approval.

## The fence, and what it does not cover

`flock` is advisory. The ingest lock excludes the ingest path because the ingest path takes it;
it excludes nothing that does not. That is why the operation requires a writer fence —
writer-producing services stopped, append cron disabled — probed **three times**: before
anything is read, after the build, and after the publish. Each probe is validated and the
service sets are compared. A one-time writer count is an observation about an instant, and the
interval this protects is minutes long.

The shipped default probe **refuses**. Whether `pm2` stopped a service is a fact about the
deployment host that nothing in this process can observe, and a default returning a plausible
posture would make every local test pass while proving nothing about any deployment.

Two things the fence does not establish, stated rather than left to be inferred:

* **Probing is sampling.** A service that stopped and restarted entirely between two probes is
  invisible to all of them. Proving a posture held continuously needs something watching
  continuously, which is not available here.
* **An arbitrary writer obeys neither the lock nor the service posture** — a shell, a stray
  script, a service someone restarts by hand.

What catches both after the fact is the monolith re-measurement before the publish and the
parity gates: detection, not prevention. That is why the read-only mount remains the stronger
route and why this stays a one-shot exception rather than a way of working.
"""
from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional, Sequence

from ops import p5_oneshot_exception as ex
from ops import p5_prod_provision as prod
from store import block_manifest as bm
from store import compaction_lock
from store import repair_wal

#: The scope a production approval must declare for THIS operation. Distinct from the
#: provisioning scope: an approval for creating roots is not an approval for publishing a
#: compaction generation over them.
COMPACTION_SCOPE = "p5_oneshot_compaction"

#: Paths whose contents must not shrink across the run. The inventory over these is what
#: `nothing_deleted` is re-derived from -- see `p5_oneshot_exception.nothing_deleted_problem`.
PROTECTED_ROLES = ("monolith", "delta", "daily")


class OneShotCompactionRefused(Exception):
    """A precondition, a gate, or the fence did not hold. Nothing destructive ever ran."""


def _exact_str(value) -> bool:
    return type(value) is str and bool(value.strip())


# ---------------------------------------------------------------- the fence probe
def refusing_fence_probe(*_a, **_k):
    """The default writer-fence probe: it refuses.

    There is no way for this process to learn whether `pm2` stopped a service or whether the
    append cron is commented out — those live on the deployment host, and a probe that guessed
    would be fabricating the one piece of evidence the whole window rests on. So the shipped
    default fails closed, and the operator tooling supplies a real probe.

    This is deliberately NOT a `verified: true` stub. A default that returns something
    plausible is worse than one that refuses: it makes every local test pass while proving
    nothing about any deployment."""
    raise OneShotCompactionRefused(
        "no writer_fence_probe was supplied. This operation runs over a WRITABLE monolith, so "
        "the writer-producing services must be stopped and the append cron disabled for the "
        "whole scan-to-publish window -- and whether they are is a fact about the deployment "
        "host that nothing in this process can observe. Supply a probe that reports the real "
        "posture; there is no default that can honestly say the fence is up")


def inventory(paths: Dict[str, str]) -> Dict[str, Dict]:
    """Bytes and file count for each named path. The raw material for `nothing_deleted`.

    Cheap relative to the build (it is one more walk of stores already being walked), and it is
    the only way the claim "nothing was removed" can be re-derived by a checker rather than
    taken on the operation's word."""
    out: Dict[str, Dict] = {}
    for role, path in sorted(paths.items()):
        if path is None:
            continue
        total = 0
        files = 0
        real = os.path.realpath(path)
        if not os.path.exists(real):
            out[role] = {"path": real, "bytes": 0, "files": 0, "present": False}
            continue
        for dirpath, _dirs, names in os.walk(real):
            for name in names:
                try:
                    total += os.stat(os.path.join(dirpath, name)).st_size
                    files += 1
                except FileNotFoundError:              # vanished mid-walk: count it as gone
                    continue
        out[role] = {"path": real, "bytes": total, "files": files, "present": True}
    return out


def _fence_moved(before: Dict, after: Dict) -> Optional[str]:
    """Did the SET of fenced services shrink between two probes? The problem, or `None`.

    Only the set difference, deliberately. Earlier revisions of this function also re-checked
    for a service that had come back up and for a re-enabled cron — and both branches were
    unreachable, because `writer_fence_problem()` runs on every probe first and refuses any
    posture outside `SAFE_PM2_POSTURES` / `SAFE_CRON_POSTURES`. A branch that cannot fire is not
    a guard, and keeping one invites the belief that something is being checked. What comparison
    genuinely adds is the case each individual probe cannot see: a service that was fenced
    before the scan and is simply *absent* from the later report, which is valid-looking on its
    own and is a fence that stopped covering something.

    **What no amount of probing establishes.** These are samples — before the scan, after the
    build, after the publish. A service that stopped and restarted entirely between two samples
    is invisible to all of them, and nothing available here can close that: proving a posture
    held continuously needs something watching continuously. This is why the fence is a
    *precondition an operator arranges*, not a guarantee this code provides, and why the
    content-ledger recheck and the parity gates sit behind it."""
    was = {s["name"] for s in before.get("services_stopped", [])
           if type(s) is dict and "name" in s}
    now = {s["name"] for s in after.get("services_stopped", [])
           if type(s) is dict and "name" in s}
    dropped = sorted(was - now)
    if dropped:
        return (f"services {dropped} were fenced before the scan and are not accounted for "
                f"after it; a fence that stops being reported is not one that held")
    return None


# ---------------------------------------------------------------- the operation
def compact_one_shot(*, live_root: str, manifest_root: str, wal_root: str, anchor_root: str,
                     monolith_path: str, delta_path: Optional[str] = None,
                     daily_root: Optional[str] = None,
                     compaction_lock_path: str, ingest_lock_path: str,
                     approval: Dict, one_shot_exception: Dict, one_shot_pre_compaction: Dict,
                     run_id: str, operator: str, at_utc: str, min_free_gib: float,
                     start_day: str, end_day: str, classification_target: Sequence[str],
                     artifacts_dir: str, hard_reserve_bytes: int,
                     block_out_path: str,
                     writer_fence_probe: Callable = refusing_fence_probe,
                     point_series_parity: Callable,
                     api_parity: Callable,
                     expected_module_fingerprints: Optional[Dict] = None,
                     now=None) -> Dict:
    """Build one block from the monolith, publish it, verify it, and delete nothing.

    `point_series_parity` and `api_parity` are injected rather than implemented here. Both need
    to read the deployment's own stores through the deployment's own reader, and a parity check
    written inside this module would be this module comparing its own output against its own
    idea of the truth. Each must return a record; the gates below adjudicate it.

    There is no `force`, no `skip`, no `unsafe_*` parameter, and this operation never passes one
    to `execute_publication` either — a test asserts that by AST."""
    if not callable(point_series_parity) or not callable(api_parity):
        raise OneShotCompactionRefused(
            "point_series_parity and api_parity are required callables: the gates are only "
            "worth anything if something actually measures them")
    for name, value in (("run_id", run_id), ("operator", operator), ("at_utc", at_utc),
                        ("start_day", start_day), ("end_day", end_day)):
        if not _exact_str(value):
            raise OneShotCompactionRefused(f"{name} must be a non-empty string")

    # ---- approval, scoped to THIS operation ------------------------------------------------
    approval = _check_approval(approval, live_root=live_root, run_id=run_id)

    # ---- the record must name this operation, before anything is read ----------------------
    problem = ex.operation_problem(
        one_shot_exception.get("operation") if type(one_shot_exception) is dict else None)
    if problem:
        raise OneShotCompactionRefused(f"the one-shot exception record {problem}")
    if one_shot_exception.get("operation") != ex.OP_COMPACTION:
        raise OneShotCompactionRefused(
            f"the record authorizes {one_shot_exception.get('operation')!r}; this operation is "
            f"{ex.OP_COMPACTION!r}. A provisioning acceptance is not a compaction acceptance")

    locks = prod.check_lock_paths(live_root=live_root,
                                  compaction_lock_path=compaction_lock_path,
                                  ingest_lock_path=ingest_lock_path)
    capacity = prod.measure_capacity(live_root, min_free_gib=min_free_gib)

    busy = compaction_lock.refuse_if_compaction_running(
        locks["compaction"], operation="p5 one-shot compaction")
    if busy:
        raise OneShotCompactionRefused(
            f"a compaction is already running ({busy}); two compactors against one store is "
            f"the state the reservation exists to make impossible")

    protected = {"monolith": monolith_path, "delta": delta_path, "daily": daily_root}
    protected = {k: v for k, v in protected.items() if v}

    holder = f"p5_oneshot_compaction/{run_id}"
    result: Dict = {"deleted_anything": False, "service_switched": False}

    with compaction_lock.CompactionLock(locks["compaction"], holder=holder) as clock:
        # ---- the fence goes up BEFORE anything is read ------------------------------------
        fence_before = writer_fence_probe()
        problem = ex.writer_fence_problem(fence_before)
        if problem:
            raise OneShotCompactionRefused(f"the writer fence {problem}")

        with compaction_lock.CompactionLock(locks["ingest"],
                                            holder=f"{holder}/scan-to-publish") as ilock:
            clock.assert_still_held()

            # ---- measured HERE, by this code, under both locks ----------------------------
            capability, verdict = ex.authorize_compaction(
                record=one_shot_exception, host=_hostname(), run_id=run_id,
                monolith_path=monolith_path, pre=one_shot_pre_compaction,
                expected_dev=capacity["filesystem_dev"], now=now)
            # There is deliberately NO `isinstance(capability, OneShotCompactionAuthorization)`
            # check here. `authorize_compaction()` mints that exact class and nothing else, so
            # the branch could not fire -- and a guard that cannot fire is not a guard, it is a
            # reassurance. The type binding does real work where a capability arrives from
            # somewhere else: `prepare_generation_one()` checks it, because a caller hands one
            # in there. Here the operation mints its own.
            measured = capability.measured
            # SPENT here, at the act it authorizes. Minting without consuming would leave a
            # capability that is single-use in principle and never used in practice -- the
            # object would still be holdable after the run, which is the standing permission
            # the whole design refuses. `consume()` re-checks the store and the expiry.
            authorization_evidence = capability.consume(
                operation=ex.OP_COMPACTION, subject_realpath=monolith_path, now=now)
            inventory_before = inventory(protected)

            # ---- the expensive part, inside the fence ------------------------------------
            plan = _build(out_path=block_out_path, start_day=start_day, end_day=end_day,
                          classification_target=classification_target,
                          monolith_path=monolith_path, delta_path=delta_path,
                          daily_root=daily_root, artifacts_dir=artifacts_dir,
                          hard_reserve_bytes=hard_reserve_bytes, lock=clock,
                          wal_root=wal_root, anchor_root=anchor_root)
            clock.assert_still_held()
            ilock.assert_still_held()

            # ---- the fence must not have moved while we scanned --------------------------
            fence_mid = writer_fence_probe()
            problem = ex.writer_fence_problem(fence_mid) or _fence_moved(fence_before, fence_mid)
            if problem:
                raise OneShotCompactionRefused(
                    f"the writer fence did not hold across the scan: {problem}")

            # ---- the monolith must still be what was measured ----------------------------
            remeasured = ex.measure_monolith(monolith_path, now=now)
            drift = ex._state_mismatch(measured, remeasured,
                                       left_name="the measurement taken before the scan",
                                       right_name="the monolith after the build")
            if drift:
                raise OneShotCompactionRefused(
                    f"the monolith changed during the build: {drift}. Nothing was published")

            # ---- PRE-PUBLISH gates: a failure here publishes nothing ----------------------
            source_map = dict(plan.get("source_map") or {})
            # The oracle is PER DAY, not one store. The block grid starts where the legacy
            # base stops, so a compaction folds delta days -- and the truth for such a day is
            # the delta it was read from, not the monolith, which never held it. The source map
            # is what says which; handing the probe a single `monolith_path` would have made it
            # compare most days against a store that does not contain them.
            parity = point_series_parity(block_path=plan["out_path"],
                                         source_map=source_map,
                                         monolith_path=monolith_path,
                                         days=list(classification_target))
            # Planned, not published: `plan_publication` composes generation N+1 in memory and
            # writes nothing, so the candidate manifest can be checked for a day claimed twice
            # while the block is still unreferenced.
            candidate = _plan(plan, manifest_root=manifest_root)
            pre_gates = {
                "point_series_parity": parity,
                "source_map_complete": _source_map_complete(source_map, classification_target),
                "base_delta_disjoint": _base_delta_disjoint(candidate["manifest"]),
            }
            problem = _pre_publish_problem(pre_gates)
            if problem:
                raise OneShotCompactionRefused(
                    f"refusing to publish: {problem}. The block is built and unreferenced; "
                    f"nothing was published, nothing was deleted")

            # ---- the authority change, borrowing both locks -------------------------------
            published = _execute(candidate, locks=locks, clock=clock, ilock=ilock,
                                 delta_path=delta_path, operator=operator,
                                 artifacts_dir=artifacts_dir, block_plan=plan)
            if not published.get("published"):
                raise OneShotCompactionRefused(
                    f"publication did not commit: {published.get('reason') or published}")

        # ---- ingest lock released; still under the compaction lock ------------------------
        clock.assert_still_held()
        live = bm.load_live(manifest_root)
        fence_after = writer_fence_probe()
        problem = ex.writer_fence_problem(fence_after) or _fence_moved(fence_before, fence_after)
        if problem:
            raise OneShotCompactionRefused(
                f"the writer fence did not hold across the whole window: {problem}")

        evidence = {
            "host": _hostname(), "run_id": run_id,
            "manifest": {"generation": int(live["generation"]),
                         "checksum": live["manifest_checksum"],
                         "checksum_ok": bm.compute_checksum(live) == live["manifest_checksum"]},
            "wal_anchor": _wal_anchor_evidence(wal_root, manifest_root=manifest_root,
                                               anchor_root=anchor_root),
            "source_map_complete": pre_gates["source_map_complete"],
            "base_delta_disjoint": pre_gates["base_delta_disjoint"],
            "source_map_digest": _source_map_digest(source_map),
            "point_series_parity": parity,
            "api_parity": api_parity(manifest_root=manifest_root,
                                     monolith_path=monolith_path,
                                     allowed_legacy_paths=[os.path.realpath(monolith_path)]),
            # Stamped HERE. `measure_capacity` returns free space and a device and no time, so
            # a reading of it is indistinguishable from the preflight's -- which is exactly what
            # `capacity_after` must not be. The stamp is taken at the moment of the call.
            "capacity_after": {**prod.measure_capacity(live_root, min_free_gib=min_free_gib),
                               "measured_utc": ex._now(now).strftime("%Y-%m-%dT%H:%M:%SZ")},
            "monolith_state": remeasured,
            "nothing_deleted": {
                "value": None, "derived_from": "inventory",
                "before": inventory_before, "after": inventory(protected),
            },
        }
        # Re-derived from the operation's OWN inventory, never accepted as a claim. Recomputing
        # it here and letting `nothing_deleted_problem` recompute it again is not redundant:
        # this fills the field, that one refuses to believe it.
        evidence["nothing_deleted"]["value"] = _nothing_shrank(
            evidence["nothing_deleted"]["before"], evidence["nothing_deleted"]["after"])

        expected = {
            "host": _hostname(), "run_id": run_id,
            "generation": int(live["generation"]),
            "manifest_checksum": live["manifest_checksum"],
            "source_map_digest": _source_map_digest(source_map),
            "monolith_state": remeasured,
        }
        problem = ex.post_compaction_problem(evidence, expected=expected)
        result.update({
            "run_id": run_id, "operator": operator, "at_utc": at_utc,
            "generation": int(live["generation"]),
            "manifest_root": manifest_root, "block_path": plan["out_path"],
            "one_shot_exception": verdict,
            "capability_consumed": capability.consumed,
            "authorization": authorization_evidence,
            "fence": {"before": fence_before, "mid": fence_mid, "after": fence_after,
                      "held_over": "compaction lock outermost; ingest lock from before the "
                                   "scan through the publish; writer fence probed before, "
                                   "after the build, and after the publish"},
            "post_compaction": evidence,
            "post_compaction_problem": problem,
        })
        if problem:
            # Published but NOT cleared. The new generation exists and is unverified; the old
            # data is untouched, which is exactly why refusing here is survivable.
            raise OneShotCompactionRefused(
                f"the post-compaction gates did not pass: {problem}. Generation "
                f"{live['generation']} is published and NOT cleared: nothing was deleted, no "
                f"service was switched, and no finalization clearance was issued. Rolling the "
                f"generation back is an operator decision -- see execute_rollback")

        result["finalization_clearance"] = ex.issue_finalization_clearance(
            host=_hostname(), run_id=run_id, generation=int(live["generation"]),
            manifest_checksum=live["manifest_checksum"],
            monolith_day_digest=remeasured["monolith_day_digest"],
            source_map_digest=_source_map_digest(source_map),
            issued_by=f"ops/p5_oneshot_compaction.compact_one_shot ({operator})",
            gates_passed=True, now=now)
        result["note"] = (
            "Generation published and verified. NOTHING was deleted and no service was "
            "switched: this operation does neither, by design. The clearance records that the "
            "gates passed; releasing the old space is a separate operation that does not exist "
            "yet, and it needs this clearance AND a separate human approval.")
    return result


# ---------------------------------------------------------------- helpers
def _hostname() -> str:
    import socket
    return socket.gethostname()


def _check_approval(approval, *, live_root: str, run_id: str) -> Dict:
    """A production approval scoped to compaction. Reuses the provisioning shape, not its scope."""
    if type(approval) is not dict:
        raise OneShotCompactionRefused(
            f"approval is {type(approval).__name__}, not a record")
    if approval.get("scope") != COMPACTION_SCOPE:
        raise OneShotCompactionRefused(
            f"the approval declares scope {approval.get('scope')!r}; this needs "
            f"{COMPACTION_SCOPE!r}. An approval to create roots is not an approval to publish "
            f"a compaction generation over them")
    for field in ("approved_by", "approved_utc", "live_root", "run_id"):
        if field not in approval:
            raise OneShotCompactionRefused(f"the approval records no {field}")
    if os.path.realpath(approval["live_root"]) != os.path.realpath(live_root):
        raise OneShotCompactionRefused(
            f"the approval names live_root {approval['live_root']!r}, not {live_root!r}")
    if approval["run_id"] != run_id:
        raise OneShotCompactionRefused(
            f"the approval is for run {approval['run_id']!r}, not {run_id!r}")
    return approval


def _build(*, out_path, start_day, end_day, classification_target, monolith_path, delta_path,
           daily_root, artifacts_dir, hard_reserve_bytes, lock, wal_root, anchor_root) -> Dict:
    """The block build, through the existing authority. No `unsafe_*` argument is passed."""
    from ingest.build_block import build_block
    return build_block(
        out_path, start_day=start_day, end_day=end_day,
        classification_target=list(classification_target),
        predecessor_path=monolith_path, delta_path=delta_path, daily_root=daily_root,
        artifacts_dir=artifacts_dir, lock=lock, hard_reserve_bytes=hard_reserve_bytes,
        wal_root=wal_root, anchor_root=anchor_root)


def _plan(block_plan, *, manifest_root) -> Dict:
    """Compose generation N+1 in memory. Reads the live manifest; writes nothing."""
    from ingest import publish_manifest as pub
    return pub.plan_publication(manifest_root, block_plan)


def _execute(candidate, *, locks, clock, ilock, delta_path, operator, artifacts_dir,
             block_plan) -> Dict:
    """Commit generation N+1, borrowing BOTH locks this run already holds.

    Borrowing is not an optimisation. `execute_publication` takes the ingest lock with a
    blocking `flock` on a fresh descriptor, and flock locks are per open file description — so
    a caller holding the fence across the scan would deadlock against itself. The borrow is what
    lets "held from before the scan through the publish" be true rather than intended."""
    from ingest import publish_manifest as pub
    # Taken from the build's own report, not composed from a naming convention. `build_block`
    # writes the provenance artifact and returns where it put it; guessing the path is how a
    # publication ends up verifying an artifact belonging to a different build.
    artifact = block_plan.get("provenance_path")
    if not artifact:
        raise OneShotCompactionRefused(
            "the build produced no provenance artifact path. A block published without one "
            "carries only locator provenance, which cannot show that the bytes it holds are "
            "the bytes its sources held (§7.5a)")
    return pub.execute_publication(
        candidate,
        ingest_lock_path=locks["ingest"], ingest_lock=ilock,
        compaction_lock_path=locks["compaction"], compaction_lock=clock,
        delta_path=delta_path, build_artifact_path=artifact, operator=operator)


def _source_map_complete(source_map: Dict, days: Sequence[str]) -> bool:
    """Every day the block claims has a NAMED source, with the bytes it was read from.

    Not "the map is non-empty", and not "the day is a key". `source_map_record` exists because
    `source_kind` alone cannot answer the audit question -- two builds can both say `"delta"`
    for day D and have read different physical indices. So a record missing its resolved path
    or its index is a day whose provenance nobody can reconstruct, which is the same problem as
    a day with no record at all."""
    if not source_map:
        return False
    for day in days:
        record = source_map.get(day)
        if type(record) is not dict:
            return False
        if not record.get("source_kind") or not record.get("source_path"):
            return False
        if "source_day_index" not in record:
            return False
    return True


def _segment_days(segment: Dict) -> set:
    """The days a manifest segment claims to serve."""
    if segment.get("day_list"):
        return set(segment["day_list"])
    start, end = segment.get("start_day"), segment.get("end_day")
    if not start or not end:
        return set()
    return set(bm.calendar_span(start, end)) - set(segment.get("gaps") or [])


def _base_delta_disjoint(live_manifest: Dict) -> bool:
    """No day is claimed by two LIVE segments of the published manifest.

    Deliberately checked against the manifest rather than against the source map. The source
    map resolves every day to exactly one tier by construction -- the resolver breaks out of
    its loop on the first hit -- so checking it for overlap would be checking a property the
    data structure cannot violate, and would pass forever regardless of what was published.

    What CAN overlap is two segments in a manifest: a new block covering days the legacy base
    also holds, where the old segment was not superseded. That is a day whose served value
    depends on which segment answers first, which is a correctness question wearing the clothes
    of a performance one. Superseded entries are excluded because they are no longer served."""
    seen: Dict[str, str] = {}
    for segment in live_manifest.get("segments", []):
        for day in _segment_days(segment):
            if day in seen:
                return False
            seen[day] = segment.get("segment_id")
    return True


def _source_map_digest(source_map: Dict) -> str:
    from ingest.build_block import source_map_digest
    return source_map_digest(source_map)


def _nothing_shrank(before: Dict, after: Dict) -> bool:
    for role, was in sorted(before.items()):
        now = after.get(role)
        if type(now) is not dict:
            return False
        if now["bytes"] < was["bytes"] or now["files"] < was["files"]:
            return False
    return True


def _pre_publish_problem(gates: Dict) -> Optional[str]:
    """The gates that MUST be settled before the authority changes.

    Parity, completeness and disjointness are properties of the BUILT BLOCK, so they can all be
    established while the block is still unreferenced — and a failure then costs a rebuild
    rather than a rollback. Only the checks that need a published manifest (its checksum, the
    WAL binding, API parity) wait until afterwards."""
    parity = gates["point_series_parity"]
    if type(parity) is not dict:
        return "the point-series parity result is not a record"
    for field in ("days_checked", "mismatches", "boundaries_included"):
        if field not in parity:
            return f"the point-series parity result records no {field}"
    if parity["mismatches"] != 0:
        return (f"{parity['mismatches']} point-series mismatch(es) against the monolith oracle")
    if parity["boundaries_included"] is not True:
        return ("the parity check skipped the first and last days; the boundaries are where an "
                "off-by-one shows")
    if type(parity["days_checked"]) is bool or not isinstance(parity["days_checked"], int) \
            or parity["days_checked"] <= 0:
        return f"the parity check covered {parity['days_checked']!r} days, which checks nothing"
    if gates["source_map_complete"] is not True:
        return "the source map is incomplete; a day with no named source is unaccounted for"
    if gates["base_delta_disjoint"] is not True:
        return "base and delta are not disjoint; a day claimed by both is a served-value race"
    return None


def _wal_anchor_evidence(wal_root: str, *, manifest_root: str, anchor_root: str) -> Dict:
    """Is the WAL still bound to the authority that now exists, with its anchor intact?

    A published generation moves the manifest authority. A WAL still bound to the PREVIOUS one
    is a log whose repairs are about a store the service no longer serves — and `repair_wal`
    refuses to append to it, so the failure surfaces at the next repair rather than here. This
    reads both and compares them, so it surfaces now."""
    try:
        authority = repair_wal.manifest_authority(manifest_root, anchor_root)
    except Exception as exc:                                        # noqa: BLE001
        return {"bound": False, "anchor_intact": False,
                "reason": f"the manifest authority could not be read: {exc!r}"}
    try:
        state = repair_wal.read_wal(wal_root, anchor_root=anchor_root)
    except Exception as exc:                                        # noqa: BLE001
        return {"bound": False, "anchor_intact": False, "authority": authority,
                "reason": f"the WAL could not be read: {exc!r}"}
    if state.authority is None:
        return {"bound": False, "anchor_intact": state.anchor is not None,
                "authority": authority,
                "reason": ("the WAL has never been initialized, so nothing binds it to the "
                           "generation that was just published")}
    bound_to = state.authority.get("manifest_root") or state.authority.get("root")
    same_root = (bound_to is None
                 or os.path.realpath(bound_to) == os.path.realpath(manifest_root))
    return {
        "bound": bool(same_root),
        "anchor_intact": state.anchor is not None,
        "authority": authority,
        "wal_authority": dict(state.authority),
        "wal_last_seq": state.last_seq,
        "reason": None if same_root else (
            f"the WAL is bound to {bound_to!r}, not to the manifest root that was just "
            f"published ({manifest_root!r})"),
    }
