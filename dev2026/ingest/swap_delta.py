"""dev2026 — P4-S8a: delta swap executor (STAGING/SHADOW ONLY — production execution is P4-S9/S10, ops).

Executes the atomic-swap PLAN returned by `ingest.prune_delta.prune_delta` per the P4-S8 design spec
(`specs/p4s8_swap_compaction_design.md` §3): ingest lock → staleness guard (unique days + exact
keep∪dropped set) → same-filesystem precheck → swap (S1 symlink retarget or S2 double-rename) →
refresh → verify → rollback-on-verify-fail → move backup to hold + append-only manifest.

**Production safety verdict baked in (S8a proof test, `tests/test_phase2_p4s8a.py`):** a cached
`TimeCubeStore` reading through a retargeted path serves **old `day_index` × new bytes silently** (and a
shrunk target yields silent `None`, not an error). So **BOTH S1 and S2 require COMPLETE request quiescence
BEFORE the path switch** in production. The S10 posture plugs in via TWO hooks, both inside the lock:
``pre_swap_quiesce_fn`` (= "pm2 stop + verify port down", runs after the staleness guard and BEFORE any
rename — a reader alive across the switch, even during a graceful shutdown, can still mix old `_Meta`
with new bytes) and ``refresh_fn`` (= "pm2 start + healthz wait", runs after the swap and again
best-effort during rollback). Restart-AFTER-swap alone is rejected; TTL/admin refresh alone is rejected
(new-request visibility only; does not quiesce in-flight readers).
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Sequence

import zarr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from store import durable_jsonl  # noqa: E402
from store.compaction_lock import (  # noqa: E402
    CompactionLock, CompactionLockBusy,
)

DEFAULT_HOLD_DAYS = 14


class QuiescenceNotProven(RuntimeError):
    """The quiescence hook ran without proving the process was drained.

    Distinct from the hook *failing*: a hook that raises says "I tried to stop the app and
    could not". This says "you told me nothing" -- and silence is the state a forgotten
    `return` produces, so it must not read as success."""


QUIESCENCE_CORE_FIELDS = ("drained", "evidence", "observed_inflight", "attestation_id")
#: Validated and recorded when supplied. `checked_utc` was previously kept only as a NAME, so
#: the record said a timestamp had been passed without saying what it was (review round 3,
#: finding 4). Optional because the join key, not the clock, is what matches two records.
QUIESCENCE_OPTIONAL_FIELDS = ("checked_utc",)


def _quiescence_refusal(attestation) -> Optional[str]:
    """None when the attestation PROVES drain; otherwise why it does not.

    The hook must report what it checked, not merely finish. `pm2 stop` returning 0 is not
    evidence the port is closed and the workers are gone -- and "no reader was observed" is not
    "no reader exists", which is the assumption the (e2) analysis is not allowed to make.

    All three core fields are REQUIRED. `observed_inflight` was previously checked only when
    present and only against `> 0`, so `{"drained": True, "evidence": "trust me"}` passed, and
    so did a count of -1: the one load-bearing gate accepting the answer it never asked for.
    Extra keys are allowed as supplementary evidence, but only the core fields are validated,
    and only they are recorded (`normalize_quiescence`).

    `drained` must be a raw `True` and `observed_inflight` a raw `int`. Truthiness would accept
    the string ``"false"``, which is what a shell-scripted hook produces when it interpolates a
    variable it never set -- the same laundering that made `allow_same_filesystem` grant a
    waiver it was refusing (P5-S5 Part 2 round 7). `bool` is excluded explicitly because
    `isinstance(True, int)` is True in Python, so `observed_inflight=False` would otherwise read
    as a proven zero."""
    if attestation is None:
        return ("the quiescence hook returned no attestation. A hook that merely does not raise "
                "proves nothing: it cannot distinguish a drained process from one it never "
                "looked at")
    if not isinstance(attestation, dict):
        return f"the quiescence attestation must be a dict, got {type(attestation).__name__}"
    missing = [f for f in QUIESCENCE_CORE_FIELDS if f not in attestation]
    if missing:
        return (f"the quiescence attestation is missing required field(s) "
                f"{', '.join(missing)}. All of {', '.join(QUIESCENCE_CORE_FIELDS)} must be "
                f"supplied: an absent field is an unanswered question, not a passing answer")
    drained = attestation["drained"]
    if drained is not True:
        return (f"the quiescence attestation reports drained={drained!r}; it must be exactly "
                f"True. Readers are not proven gone")
    evidence = attestation["evidence"]
    if not isinstance(evidence, str) or not evidence.strip():
        return ("the quiescence attestation carries no `evidence` string. Record WHAT was "
                "checked (process stopped, port closed, in-flight count read), so a swap that "
                "later loses a day can be traced to what quiescence actually verified")
    inflight = attestation["observed_inflight"]
    if isinstance(inflight, bool) or not isinstance(inflight, int):
        return (f"observed_inflight must be a raw int, got "
                f"{type(inflight).__name__} ({inflight!r})")
    att_id = attestation["attestation_id"]
    if not isinstance(att_id, str) or not att_id.strip():
        return ("the quiescence attestation carries no `attestation_id` string. It is the join "
                "key: the ops-side evidence file and this executor's manifest both record it, "
                "so two records can be matched after a retry instead of guessed at by order")
    for opt in QUIESCENCE_OPTIONAL_FIELDS:
        if opt in attestation and (not isinstance(attestation[opt], str)
                                   or not attestation[opt].strip()):
            return f"the quiescence attestation's `{opt}` must be a non-empty string when given"
    if inflight != 0:
        if inflight < 0:
            return (f"observed_inflight is {inflight}: a negative count is not a measurement, "
                    f"and it must not read as 'fewer than none'")
        return (f"the quiescence attestation reports {inflight} in-flight request(s) still "
                f"alive. A reader alive across the swap is the (e2) combination itself")
    return None


def normalize_quiescence(attestation: dict) -> dict:
    """The validated core, and nothing else, for the audit record.

    The callback's own object is never stored: it is caller-controlled and may hold anything.
    Extra keys are acknowledged by NAME so the record shows what was supplied without the
    manifest inheriting arbitrary payload."""
    known = QUIESCENCE_CORE_FIELDS + QUIESCENCE_OPTIONAL_FIELDS
    extra = sorted(str(k) for k in attestation if k not in known)
    out = {f: attestation[f] for f in QUIESCENCE_CORE_FIELDS}
    out.update({f: attestation[f] for f in QUIESCENCE_OPTIONAL_FIELDS if f in attestation})
    if extra:
        out["extra_fields"] = extra
    return out


def _st_dev(path: str) -> int:
    """Device id for a path (factored out so tests can simulate a cross-device hold_dir/staging)."""
    return os.stat(path).st_dev


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _read_live_days(live_path: str) -> List[str]:
    g = zarr.open_group(live_path, mode="r")          # READ-ONLY; resolves a symlink transparently
    return list(g.attrs["days"])


def _default_verifier(live_path: str, keep_sorted: List[str]) -> dict:
    """Local post-swap verification (shadow-scale §4 subset): day set == keep (sorted, unique),
    latest == max(days). API-level probes (/healthz, bbox 200/400) are the S9 harness's job."""
    days = _read_live_days(live_path)
    ok_set = (days == keep_sorted)
    ok_unique = (len(days) == len(set(days)))
    ok_latest = bool(days) and (max(days) == keep_sorted[-1])
    return {"ok": ok_set and ok_unique and ok_latest,
            "day_set_ok": ok_set, "unique": ok_unique, "latest_ok": ok_latest,
            "live_day_count": len(days)}


def _atomic_retarget(symlink_path: str, new_target: str) -> None:
    """Atomically retarget a symlink: create tmp symlink, rename over (POSIX-atomic path switch).
    NOTE (proof test): atomic PATH switch only — NOT application metadata consistency."""
    tmp = symlink_path + ".swap-tmp"
    if os.path.lexists(tmp):
        os.remove(tmp)
    os.symlink(new_target, tmp)
    os.replace(tmp, symlink_path)


def execute_swap_plan(plan: dict, *, mode: str, hold_dir: str,
                      wal_root: Optional[str] = None, manifest_root: Optional[str] = None,
                      anchor_root: Optional[str] = None,
                      unsafe_allow_colocated_anchor: bool = False,
                      allowed_legacy_paths: Optional[Sequence[str]] = None,
                      corrected_day_gate: bool = True,
                      lock_path: Optional[str] = None,
                      refresh_fn: Optional[Callable[[], None]] = None,
                      pre_swap_quiesce_fn: Optional[Callable[[], dict]] = None,
                      unsafe_skip_quiescence: bool = False,
                      verifier: Optional[Callable[[str, List[str]], dict]] = None,
                      hold_days: int = DEFAULT_HOLD_DAYS,
                      compaction_lock_path: Optional[str] = None,
                      unsafe_skip_compaction_lock: bool = False,
                      operator: Optional[str] = None) -> dict:
    """Execute a prune_delta swap plan. Returns a result dict; never raises for policy refusals.

    mode: 's1' (live path is a SYMLINK; swap = atomic retarget; backup = old target dir, record-only) or
          's2' (double rename: live -> backup dir in hold, staging -> live; ms-scale ENOENT window —
          shadow/staging only per the design spec).
    pre_swap_quiesce_fn (S10 production posture, Codex review): called INSIDE the lock, AFTER the
    staleness guard + same-fs prechecks, BEFORE any path switch — production passes "pm2 stop + verify
    port down" here so quiescence and swap share one critical section (an external stop before this
    function would sit outside the lock AND deadlock on a nested flock). REQUIRED (§7.9): it must
    return an attestation ``{"drained": True, "evidence": "<what was checked>",
    "observed_inflight": 0}``; returning nothing, or reporting anything short of a proven drain, is
    refused. If it raises or cannot prove drain, NOTHING has been touched: the executor returns
    ``status="quiesce_failed"`` (safe state; the caller decides app recovery).
    Order: lock -> staleness -> prechecks -> quiesce -> swap -> refresh_fn -> verify.
    refresh_fn: runs AFTER the swap (tests: cube.refresh; production: "pm2 start + healthz wait") and is
    re-invoked best-effort during rollback. verifier: extra post-swap check, called
    (live_path, keep_sorted) -> {"ok": bool, ...}; the built-in local verifier always runs first.
    """
    # P5-S3 §7.1b: same gate as prune_delta. A swap retargets the delta path, and a block
    # build may be reading it right now; the wrong bytes would become permanent history.
    # §7.1b: a RESERVATION, not a probe. A probe answers "was a build running a moment ago";
    # between that answer and the `os.rename` pair below, a build can take the lock and begin
    # reading the very delta whose path we are about to switch -- the P4-S8a configuration with
    # a permanent consequence. The reservation is non-blocking (a running build makes us refuse
    # rather than wait) and is held, in compaction -> ingest order, across the swap AND any
    # rollback, released only once both are finished.
    # REQUIRED, not optional. It defaulted to None and only reserved when a path was supplied,
    # so a caller who simply omitted it swapped the delta path with no reservation at all --
    # while a build held the real lock and was reading that delta. The waiver is named so it
    # cannot be typed by accident.
    # P5-S5 Part 3 §7.9: quiescence is REQUIRED, and it must be PROVEN.
    # The (e2) hole -- a reader holding a pre-publish base while the delta it re-resolves is
    # already pruned -- is closed by the protocol, not by the snapshot mechanism
    # (`tiered_cube` module docstring). The protocol's load-bearing step is this one. It
    # defaulted to None and was skipped when omitted, so a caller who simply forgot swapped the
    # delta path with live readers still holding the old base: the exact combination the design
    # spec claims is unreachable. Absence of a hook is not evidence of an empty process.
    if pre_swap_quiesce_fn is None and not unsafe_skip_quiescence:
        return {"status": "refused", "swap_performed": False, "live_touched": False,
                "reason": ("pre_swap_quiesce_fn is required: the swap retargets the delta path, "
                           "and a reader alive across it can hold a pre-publish base together "
                           "with a post-prune delta, where folded days sit in neither tier "
                           "(§7.5, (e2)). Pass the quiescence hook, or unsafe_skip_quiescence="
                           "True in a test that is not exercising it.")}
    compaction_guard = None
    if not compaction_lock_path and not unsafe_skip_compaction_lock:
        return {"status": "refused", "swap_performed": False,
                "reason": ("compaction_lock_path is required: a swap retargets the delta path, "
                           "and a block build may be reading it right now (§7.1b). Pass the "
                           "lock path, or unsafe_skip_compaction_lock=True in a test that is "
                           "not exercising it.")}
    if compaction_lock_path:
        try:
            compaction_guard = CompactionLock(
                compaction_lock_path, holder="p5-execute-swap-plan").acquire()
        except CompactionLockBusy:
            return {"status": "refused", "reason": "compaction_lock_held",
                    "operation": "execute_swap_plan", "lock_path": compaction_lock_path,
                    "swap_performed": False,
                    "hint": ("a block build holds the compaction lock and may be reading this "
                             "delta; retargeting the path now would freeze wrong bytes into an "
                             "immutable block. Reschedule after the build completes.")}
    try:
        if mode not in ("s1", "s2"):
            raise ValueError(f"mode must be 's1' or 's2', got {mode!r}")
        if plan.get("status") != "ok":
            return {"status": "refused", "reason": f"plan status is {plan.get('status')!r}, not 'ok' — "
                                                   "only a validated prune_delta plan is executable"}
        sp = plan.get("swap_plan") or {}
        staging, live = sp.get("from"), sp.get("to")
        if not (staging and live and os.path.isdir(staging)):
            return {"status": "refused", "reason": "swap_plan.from/to missing or staging path not a directory"}
        keep_sorted = sorted(plan["keep_days"])
        dropped = sorted(plan.get("dropped_days", []))
        expected_live = set(keep_sorted) | set(dropped)

        if mode == "s1" and not os.path.islink(live):
            return {"status": "refused", "reason": "mode s1 requires the live path to be a symlink "
                                                   "(one-time ops migration; design spec §2)"}
        if mode == "s2" and os.path.islink(live):
            return {"status": "refused", "reason": "mode s2 on a symlink live path — use s1"}

        os.makedirs(hold_dir, exist_ok=True)
        lock_file = lock_path or (live + ".swap.lock")
        swap_id = _utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(3).hex()
        stamp = _utcnow().isoformat()

        lk = open(lock_file, "w")
        try:
            fcntl.flock(lk, fcntl.LOCK_EX)                # ingest lock: swap and append mutually exclusive

            # ---- staleness guard (§3 step 4): UNIQUE + EXACT set, under the lock ----
            live_days = _read_live_days(live)
            if len(live_days) != len(set(live_days)):
                dups = sorted({d for d in live_days if live_days.count(d) > 1})
                return {"status": "aborted_stale", "reason": f"live delta has duplicate days: {dups}",
                        "swap_performed": False}
            if set(live_days) != expected_live:
                new_days = sorted(set(live_days) - expected_live)
                gone = sorted(expected_live - set(live_days))
                return {"status": "aborted_stale",
                        "reason": "live delta changed since the plan was built — rebuild the plan",
                        "unexpected_new_days": new_days, "unexpected_missing_days": gone,
                        "swap_performed": False}

            # ---- §7.5a corrected-day re-authorization, UNDER THE LOCK, BEFORE the path switch.
            # The staleness guard above compares the live delta's DAY SET against the plan's. A
            # repair committed between plan and swap does not change the day set at all: the day is
            # still there, still the same date, and the guard sees nothing. Re-running the gate here
            # is the only thing standing between "a newer correction landed" and "that correction
            # was dropped". The plan's authorization is minutes or hours old; this one is current.
            if dropped and corrected_day_gate:
                if not (wal_root and manifest_root):
                    return {"status": "refused", "swap_performed": False,
                            "reason": ("wal_root and manifest_root are REQUIRED to swap away days: "
                                       "without them the §7.5a gate cannot be re-run under the "
                                       "lock, and the plan's authorization is not current")}
                try:
                    from ingest import corrected_day as _cd
                    # The plan recorded which anchor domain authorized it. Re-authorizing against a
                    # different one would mean the two stages rest on different evidence -- and the
                    # weaker of the two is the one that decides.
                    planned_anchor = plan.get("anchor_root")
                    here = (os.path.realpath(anchor_root) if anchor_root else None)
                    if planned_anchor != here:
                        return {"status": "refused", "swap_performed": False,
                                "reason": (f"the plan was authorized against anchor domain "
                                           f"{planned_anchor!r} but the swap is using {here!r}; "
                                           f"switching anchor domain between plan and swap means the "
                                           f"two stages rest on different evidence")}
                    gate = _cd.prune_eligibility(
                        dropped, manifest_root=manifest_root, delta_path=live, wal_root=wal_root,
                        anchor_root=anchor_root, allowed_legacy_paths=allowed_legacy_paths,
                        unsafe_allow_colocated_anchor=unsafe_allow_colocated_anchor)
                except Exception as exc:                  # unreadable WAL/manifest -> fail closed
                    return {"status": "refused", "swap_performed": False,
                            "reason": (f"the corrected-day gate could not be re-run under the lock "
                                       f"({exc}); refusing rather than switching the path on a "
                                       f"stale authorization")}
                if gate["refused"]:
                    first = sorted(gate["refused"])[0]
                    return {"status": "aborted_stale", "swap_performed": False,
                            "reason": (f"{len(gate['refused'])} day(s) are no longer authorized to "
                                       f"leave delta (first {first}: {gate['refused'][first]}). The "
                                       f"day set is unchanged, so the staleness guard cannot see "
                                       f"this -- a repair landed after the plan was built."),
                            "corrected_day_refused": sorted(gate["refused"])}

            # ---- same-filesystem prechecks (os.rename must not cross devices) ----
            live_parent = os.path.dirname(os.path.abspath(live)) or "."
            if _st_dev(staging) != _st_dev(live_parent):
                return {"status": "refused", "reason": "staging and live are on different filesystems "
                                                       "(os.rename would not be atomic)", "swap_performed": False}
            if mode == "s2" and _st_dev(hold_dir) != _st_dev(live_parent):
                # S2 renames live -> hold_dir/<backup>: a cross-device hold_dir would fail MID-swap
                # (after live was moved) — refuse cleanly BEFORE touching anything (Codex S8a-review #2).
                return {"status": "refused", "reason": "hold_dir is on a different filesystem than live "
                                                       "(s2 renames the old live into hold_dir)",
                        "swap_performed": False}

            # ---- pre-swap quiescence (INSIDE the lock, after staleness+prechecks, BEFORE any rename) ----
            quiescence = {"attested": False,
                          "waived": bool(unsafe_skip_quiescence and pre_swap_quiesce_fn is None)}
            if pre_swap_quiesce_fn is not None:
                attestation = None
                try:
                    attestation = pre_swap_quiesce_fn()
                    why = _quiescence_refusal(attestation)
                    if why is not None:
                        raise QuiescenceNotProven(why)
                    # Kept for the audit record: an `evidence` string that is mandatory at the
                    # gate and then discarded cannot answer "what did quiescence actually
                    # verify?" the day a swap is suspected of losing one.
                    quiescence = {"attested": True, "waived": False,
                                  **normalize_quiescence(attestation)}
                except Exception as exc:
                    # SAFE state: no file has been touched. The caller decides app recovery (it may have
                    # half-stopped the serving process) — that is why this is a distinct status.
                    # Carry the attempted id when there was one: a REFUSED attempt still wrote an
                    # ops-side evidence file, and matching them is the whole point of the key.
                    if isinstance(attestation, dict):
                        aid = attestation.get("attestation_id")
                        if isinstance(aid, str) and aid.strip():
                            quiescence = {**quiescence, "attempted_attestation_id": aid}
                    _manifest(hold_dir, {"swap_id": swap_id, "ts": stamp, "op": "swap_quiesce_failed",
                                         "mode": mode, "from": staging, "to": live, "operator": operator,
                                         "quiescence": quiescence,
                                         "error": f"{type(exc).__name__}: {exc}"})
                    return {"status": "quiesce_failed",
                            "reason": f"pre-swap quiescence failed: {type(exc).__name__}: {exc}",
                            "swap_id": swap_id, "swap_performed": False, "live_touched": False,
                            "quiescence": quiescence}

            # ---- swap ----
            if mode == "s1":
                old_target = os.path.realpath(live)
                _atomic_retarget(live, os.path.abspath(staging))
                backup = old_target                        # the old versioned dir IS the backup (record-only)
            else:
                backup = os.path.join(hold_dir, os.path.basename(live) + f".pre-prune-{swap_id}")
                os.rename(live, backup)                    # ← ms-scale ENOENT window starts
                os.rename(staging, live)                   # ← window ends

            # ---- refresh + verify: ANY exception here is treated as verify failure -> rollback under the
            # still-held lock. A raising refresh_fn must NOT leave a swapped live delta behind
            # (Codex S8a-review #1). ----
            failure_reason = None
            v: dict = {}
            try:
                if refresh_fn is not None:                 # step-7 mechanism (tests: cube.refresh)
                    refresh_fn()
                v = _default_verifier(live, keep_sorted)
                if v["ok"] and verifier is not None:
                    extra = verifier(live, keep_sorted)
                    v = {**v, "extra": extra, "ok": bool(v["ok"] and extra.get("ok"))}
                if not v["ok"]:
                    failure_reason = "post-swap verification failed"
            except Exception as exc:
                failure_reason = f"refresh/verify raised: {type(exc).__name__}: {exc}"
                v = {"ok": False, "exception": failure_reason}

            if failure_reason:
                # ---- rollback under the still-held lock (§5) ----
                try:
                    if mode == "s1":
                        _atomic_retarget(live, backup)
                    else:
                        os.rename(live, staging)           # put the new delta back at its staging path
                        os.rename(backup, live)            # restore the original live delta
                    try:
                        if refresh_fn is not None:
                            refresh_fn()                   # best-effort; the re-read below is authoritative
                    except Exception:
                        pass
                    restored = sorted(_read_live_days(live)) == sorted(expected_live)
                    _manifest(hold_dir, {"swap_id": swap_id, "ts": stamp, "op": "swap_rollback", "mode": mode,
                                         "quiescence": quiescence,
                                         "from": staging, "to": live, "backup": backup, "operator": operator,
                                         "reason": failure_reason, "verify": v, "restored_ok": restored})
                    return {"status": "rolled_back", "reason": failure_reason, "verify": v,
                            "restored_ok": restored, "swap_id": swap_id, "swap_performed": False,
                            "quiescence": quiescence}
                except Exception as rexc:
                    # rollback itself failed — ambiguous on-disk state; record loudly, human required.
                    _manifest(hold_dir, {"swap_id": swap_id, "ts": stamp, "op": "swap_rollback_failed",
                                         "mode": mode, "from": staging, "to": live, "backup": backup,
                                         "quiescence": quiescence,
                                         "operator": operator, "reason": failure_reason,
                                         "rollback_error": f"{type(rexc).__name__}: {rexc}"})
                    return {"status": "rollback_failed", "reason": failure_reason,
                            "rollback_error": f"{type(rexc).__name__}: {rexc}", "backup": backup,
                            "swap_id": swap_id, "swap_performed": True,
                            "quiescence": quiescence,
                            "note": "on-disk state ambiguous — manual recovery from backup required"}

            # ---- success: hold lifecycle + manifest (§7) ----
            hold_until = (_utcnow() + timedelta(days=hold_days)).isoformat()
            if mode == "s2":
                hold_entry = backup                        # already renamed into hold_dir
            else:
                hold_entry = backup                        # record-only: old target stays put until ops deletes
            record = {"swap_id": swap_id, "ts": stamp, "op": "delta_prune_swap", "mode": mode,
                      "from": staging, "to": live, "backup": hold_entry, "hold_until": hold_until,
                      "keep_day_count": len(keep_sorted), "keep_latest": keep_sorted[-1],
                      "dropped_days": dropped, "operator": operator, "verify": v,
                      "quiescence": quiescence,
                      "hard_delete": "ops-only after hold_until (never automated in S7/S8)"}
            _manifest(hold_dir, record)
            return {"status": "swapped", "swap_id": swap_id, "mode": mode, "backup": hold_entry,
                    "hold_until": hold_until, "verify": v, "manifest": record,
                    "quiescence": quiescence, "swap_performed": True,
                    "production_mutation": False}          # caller supplies paths; this phase = staging/shadow
        finally:
            fcntl.flock(lk, fcntl.LOCK_UN)
            lk.close()


    finally:
        if compaction_guard is not None:
            compaction_guard.release()

def _manifest(hold_dir: str, record: dict) -> None:
    """Append-only JSONL manifest (§7 convention, shared with P4-S7), durably.

    The file AND, on first create, the directory are fsync'd -- see `store.durable_jsonl`, the
    single implementation both this and the ops-side evidence file use. There used to be a
    second copy of this logic in the runbook's markdown, and that copy got the directory sync
    wrong, because a copy in a document cannot be tested (review round 4, finding 3)."""
    durable_jsonl.append(os.path.join(hold_dir, "manifest.jsonl"), record, dir_fd_path=hold_dir)
