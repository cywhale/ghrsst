"""dev2026 — P4-S8a: delta swap executor (STAGING/SHADOW ONLY — production execution is P4-S9/S10, ops).

Executes the atomic-swap PLAN returned by `ingest.prune_delta.prune_delta` per the P4-S8 design spec
(`specs/p4s8_swap_compaction_design.md` §3): ingest lock → staleness guard (unique days + exact
keep∪dropped set) → same-filesystem precheck → swap (S1 symlink retarget or S2 double-rename) →
refresh → verify → rollback-on-verify-fail → move backup to hold + append-only manifest.

**Production safety verdict baked in (S8a proof test, `tests/test_phase2_p4s8a.py`):** a cached
`TimeCubeStore` reading through a retargeted path serves **old `day_index` × new bytes silently** (and a
shrunk target yields silent `None`, not an error). So **BOTH S1 and S2 are STAGING/SHADOW/REHEARSAL ONLY.**
Production MUST NOT reuse this executor without an **external request-quiescence wrapper** — PM2
restart-under-lock (available on VM24 today) or a serving-disable/drain mechanism (future design) — plugged
in via `refresh_fn`. An admin-refresh endpoint alone is NOT sufficient (new-request visibility only; does
not quiesce in-flight readers).
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Sequence

import zarr

DEFAULT_HOLD_DAYS = 14


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
                      lock_path: Optional[str] = None,
                      refresh_fn: Optional[Callable[[], None]] = None,
                      verifier: Optional[Callable[[str, List[str]], dict]] = None,
                      hold_days: int = DEFAULT_HOLD_DAYS,
                      operator: Optional[str] = None) -> dict:
    """Execute a prune_delta swap plan. Returns a result dict; never raises for policy refusals.

    mode: 's1' (live path is a SYMLINK; swap = atomic retarget; backup = old target dir, record-only) or
          's2' (double rename: live -> backup dir in hold, staging -> live; ms-scale ENOENT window —
          shadow/staging only per the design spec).
    refresh_fn: the step-7 mechanism (tests: cube.refresh; production: quiescence mechanism — see module
    docstring). verifier: extra post-swap check, called (live_path, keep_sorted) -> {"ok": bool, ...};
    the built-in local verifier always runs first.
    """
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
                                     "from": staging, "to": live, "backup": backup, "operator": operator,
                                     "reason": failure_reason, "verify": v, "restored_ok": restored})
                return {"status": "rolled_back", "reason": failure_reason, "verify": v,
                        "restored_ok": restored, "swap_id": swap_id, "swap_performed": False}
            except Exception as rexc:
                # rollback itself failed — ambiguous on-disk state; record loudly, human required.
                _manifest(hold_dir, {"swap_id": swap_id, "ts": stamp, "op": "swap_rollback_failed",
                                     "mode": mode, "from": staging, "to": live, "backup": backup,
                                     "operator": operator, "reason": failure_reason,
                                     "rollback_error": f"{type(rexc).__name__}: {rexc}"})
                return {"status": "rollback_failed", "reason": failure_reason,
                        "rollback_error": f"{type(rexc).__name__}: {rexc}", "backup": backup,
                        "swap_id": swap_id, "swap_performed": True,
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
                  "hard_delete": "ops-only after hold_until (never automated in S7/S8)"}
        _manifest(hold_dir, record)
        return {"status": "swapped", "swap_id": swap_id, "mode": mode, "backup": hold_entry,
                "hold_until": hold_until, "verify": v, "manifest": record, "swap_performed": True,
                "production_mutation": False}          # caller supplies paths; this phase = staging/shadow
    finally:
        fcntl.flock(lk, fcntl.LOCK_UN)
        lk.close()


def _manifest(hold_dir: str, record: dict) -> None:
    """Append-only JSONL manifest (§7 convention, shared with P4-S7)."""
    with open(os.path.join(hold_dir, "manifest.jsonl"), "a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
