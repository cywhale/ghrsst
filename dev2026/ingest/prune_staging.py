"""dev2026 — P4-S7: daily-staging prune + validation manifest (MOVE-TO-HOLD, never hard delete).

Implements P4-S4 §4/§4.1 + the S8-design §7 hold/manifest conventions (same `hold_dir` + append-only
`manifest.jsonl` the S8a swap executor writes). **Dry-run is the default**: it computes candidates, runs
every per-day validation, and emits the would-be manifest records to `manifest.dryrun.jsonl` — without
moving anything. A real run MOVES each eligible day group into the hold area (`os.rename`, same
filesystem, precheck enforced) and appends a `dry_run:false` manifest line with `hold_until`.
**Hard delete is out of scope — ops-only, after `hold_until`, never automated here.**

Fail-closed gates:
- GLOBAL: the delta's recent spatial window must be contiguous (local recompute, P4-S4 §4.1) — a hole
  refuses the entire run, no candidates, nothing moved.
- Keep-set (calendar, never an entry slice): keep = latest `spatial_window_days + staging_buffer_days`
  calendar days (anchored on max(daily latest, delta latest)); `conservative` (default) additionally
  keeps EVERY day not yet covered by base.
- PER-DAY (skip, not prune, on any failure): the day must be covered by a cube tier — **delta preferred,
  else base** (steady-state candidates are in delta; the first full-history prune's candidates are in
  base only) — with `var_valid` agreement and float32 NaN-aware sample parity vs that tier. A day with NO
  cube coverage is skipped unless `mode='accepted_risk'` AND the explicit `allow_redownload_only=True`
  (the P4-S4 "NetCDF redownload explicitly accepted" acceptance), and is then manifest-marked
  `redownload_required: true`.
"""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

import numpy as np
import zarr

from ingest.prune_delta import recent_window_contiguous
from store.zarr_paths import group_path, list_existing_days

DEFAULT_HOLD_DAYS = 14


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _st_dev(path: str) -> int:
    """Factored for tests (cross-device simulation), mirroring swap_delta."""
    return os.stat(path).st_dev


def _move(src: str, dst: str) -> None:
    """Same-filesystem atomic move (factored so tests can inject per-day move failures)."""
    os.rename(src, dst)


def _run_id() -> str:
    """Factored so tests can pin the run id (e.g. to pre-create a colliding hold destination)."""
    return _utcnow().strftime("%Y%m%dT%H%M%SZ") + "-" + os.urandom(3).hex()


def _open_cube(path: Optional[str]) -> Optional[dict]:
    if not path or not os.path.isdir(path):
        return None
    g = zarr.open_group(path, mode="r")               # READ-ONLY
    days = list(g.attrs["days"])
    return {"group": g, "days": days, "day_index": {d: i for i, d in enumerate(days)},
            "vars": list(g.attrs.get("vars", [])),
            "var_valid": {v: list(x) for v, x in dict(g.attrs.get("var_valid", {})).items()},
            "region": (tuple(g.attrs["region"]) if "region" in g.attrs else None)}


def _validate_day_vs_tier(daily_path: str, day: str, tier: dict, tier_name: str,
                          sample_n: int, rng) -> dict:
    """Per-day gate: var_valid agreement + float32 NaN-aware sample parity daily vs the covering tier.
    Reads are BY day_index (append-order safe) and point-wise (cheap even on the t90/s8 base)."""
    t = tier["day_index"][day]
    gd = zarr.open_group(group_path(daily_path, day), mode="r")       # READ-ONLY
    # the daily day must not carry a DATA var the tier lacks — pruning would lose daily-only data
    # (Codex S7-review #2). Coordinate/helper arrays (lon/lat/time, or anything not 3-D (t,y,x)) are
    # NOT data vars — the real VM24 daily groups carry a 1-D `time` array which must not trip this
    # gate (same data-var filter as prune_delta's bulk engine).
    daily_vars = {k for k in gd.array_keys()
                  if k not in ("lon", "lat", "time") and len(gd[k].shape) == 3}
    extra = sorted(daily_vars - set(tier["vars"]))
    if extra:
        return {"ok": False, "reason": f"daily has var(s) not represented in the {tier_name} tier: "
                                       f"{extra} — pruning would lose daily-only data", "tier": tier_name}
    i0, j0 = (tier["region"][0], tier["region"][2]) if tier["region"] else (0, 0)
    checked = 0
    for v in tier["vars"]:
        daily_present = v in gd
        vv = tier["var_valid"].get(v)
        tier_valid = bool(vv[t]) if vv is not None else True
        if daily_present != tier_valid:               # absent-var semantics must agree
            return {"ok": False, "reason": f"var_valid mismatch for {v}: daily_present={daily_present} "
                                           f"tier_valid={tier_valid}", "tier": tier_name}
        if not daily_present:
            continue
        arr_t = tier["group"][v]
        ny, nx = int(arr_t.shape[1]), int(arr_t.shape[2])
        n = min(sample_n, ny * nx)
        flat = rng.choice(ny * nx, size=n, replace=False)
        ii, jj = np.divmod(flat, nx)
        for a, b in zip(ii.tolist(), jj.tolist()):
            got = np.float32(arr_t[t, a, b])
            exp = np.float32(gd[v][0, i0 + a, j0 + b])
            if not (got == exp or (np.isnan(got) and np.isnan(exp))):
                return {"ok": False, "reason": f"sample parity failed for {v} at ({a},{b}): "
                                               f"tier={got} daily={exp}", "tier": tier_name}
            checked += 1
    return {"ok": True, "tier": tier_name, "cells_checked": checked}


def prune_daily_staging(daily_path: str, hold_dir: str, *,
                        delta_path: str, base_path: Optional[str] = None,
                        spatial_window_days: int = 31, staging_buffer_days: int = 7,
                        mode: str = "conservative", dry_run: bool = True,
                        allow_redownload_only: bool = False,
                        hold_days: int = DEFAULT_HOLD_DAYS,
                        sample_n: int = 32, seed: int = 0,
                        lock_path: Optional[str] = None,
                        operator: Optional[str] = None) -> dict:
    """Prune old daily-staging day groups by MOVING them into the hold area. Dry-run by default.

    Returns {status, candidates, pruned, skipped, kept_count, manifest_path, ...}; refusals return
    status='refused' with nothing moved. Per-day validation failures SKIP that day (recorded), they do
    not abort the run — except the global recent-window gate, which refuses everything.
    """
    if mode not in ("conservative", "accepted_risk"):
        raise ValueError(f"unknown mode: {mode}")
    daily_days = list_existing_days(daily_path)
    if not daily_days:
        return {"status": "refused", "reason": "daily store empty or missing", "moved": []}
    delta = _open_cube(delta_path)
    if delta is None:
        return {"status": "refused", "reason": "delta cube missing — cannot verify the spatial window",
                "moved": []}
    base = _open_cube(base_path)

    # ---- GLOBAL gate: recent spatial window contiguous (P4-S4 §4.1; local recompute) ----
    rw = recent_window_contiguous(delta["days"], spatial_window_days)
    if not rw["contiguous"]:
        return {"status": "refused", "reason": "recent spatial window has holes — repair before pruning",
                "recent_window": rw, "moved": []}

    # ---- keep-set (calendar range, never an entry slice) ----
    base_set = set(base["days"]) if base else set()
    delta_set = set(delta["days"])
    anchor = max(max(daily_days), max(delta["days"]))
    keep_start = (datetime.fromisoformat(anchor).date()
                  - timedelta(days=spatial_window_days + staging_buffer_days - 1)).isoformat()
    in_recent = {d for d in daily_days if d >= keep_start}
    if mode == "conservative":
        keep = in_recent | {d for d in daily_days if d not in base_set}
    else:
        keep = set(in_recent)
    candidates = sorted(d for d in daily_days if d not in keep)

    os.makedirs(hold_dir, exist_ok=True)
    if not dry_run and _st_dev(hold_dir) != _st_dev(daily_path):
        return {"status": "refused", "reason": "hold_dir is on a different filesystem than the daily "
                                               "store (move must be an atomic rename)", "moved": []}

    run_id = _run_id()
    stamp = _utcnow().isoformat()
    hold_until = (_utcnow() + timedelta(days=hold_days)).isoformat()
    rng = np.random.default_rng(seed)
    daily_base = os.path.basename(os.path.abspath(daily_path))
    manifest_path = os.path.join(hold_dir, "manifest.dryrun.jsonl" if dry_run else "manifest.jsonl")

    lock_file = lock_path or (os.path.abspath(daily_path) + ".prune.lock")
    lk = open(lock_file, "w")
    pruned, skipped, records = [], [], []
    # Real run: the manifest is opened BEFORE the loop and each moved day's line is written + flushed +
    # fsync'd IMMEDIATELY after its rename — a crash mid-run can never leave a moved day without a
    # manifest record (Codex S7-review #1). Dry-run batches to its own file after the loop.
    mf = None if dry_run else open(manifest_path, "a")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX)                # shared ingest-lock convention (S8 §3/§7)
        for day in candidates:
            # ---- per-day coverage + validation: delta preferred, else base ----
            if day in delta_set:
                tier, tier_name = delta, "delta"
            elif day in base_set:
                tier, tier_name = base, "base"
            else:
                if mode == "accepted_risk" and allow_redownload_only:
                    tier, tier_name = None, None       # explicit redownload-only acceptance
                else:
                    skipped.append({"day": day, "reason": "no cube coverage (not in delta or base); "
                                    "redownload-only prune requires mode=accepted_risk AND "
                                    "allow_redownload_only=True"})
                    continue
            if tier is not None:
                val = _validate_day_vs_tier(daily_path, day, tier, tier_name, sample_n, rng)
                if not val["ok"]:
                    skipped.append({"day": day, "reason": val["reason"], "tier": tier_name})
                    continue
            else:
                val = {"ok": None, "tier": None, "note": "no local parity possible (redownload-only)"}

            src = group_path(daily_path, day)
            rec = {"day": day, "action": "prune_daily_staging", "op": "staging_prune",
                   "run_id": run_id, "ts": stamp, "mode": mode, "dry_run": dry_run,
                   "operator": operator, "source_path": src,
                   "base_covers": day in base_set, "delta_present": day in delta_set,
                   "in_recent_window": day >= keep_start,
                   "redownload_required": (day not in base_set and day not in delta_set),
                   "validated_against": tier_name, "validation": val,
                   "hold_until": (None if dry_run else hold_until)}
            if dry_run:
                records.append(rec)
                pruned.append(day)                     # would-be pruned
                continue
            # ---- MOVE-TO-HOLD (never delete): §7 naming, atomic same-fs rename ----
            dst = os.path.join(hold_dir, f"{daily_base}.day-{day}.pre-prune-{run_id}")
            if os.path.exists(dst):                    # never rely on rename-over semantics (review #3)
                skipped.append({"day": day, "reason": f"hold destination already exists: {dst}"})
                continue
            try:
                _move(src, dst)
            except OSError as exc:                     # a failing move skips THIS day; earlier moved
                skipped.append({"day": day,            # days already have their manifest lines
                                "reason": f"move failed: {type(exc).__name__}: {exc}"})
                continue
            rec["hold_path"] = dst
            rec["status"] = "moved"
            rec["hard_delete"] = "ops-only after hold_until (never automated in S7/S8)"
            mf.write(json.dumps(rec, sort_keys=True) + "\n")
            mf.flush()
            os.fsync(mf.fileno())                      # crash-safe: line durable before the next move
            records.append(rec)
            pruned.append(day)

        if dry_run:                                    # batch write is fine — nothing was mutated
            with open(manifest_path, "a") as fh:
                for rec in records:
                    fh.write(json.dumps(rec, sort_keys=True) + "\n")
    finally:
        if mf is not None:
            mf.close()
        fcntl.flock(lk, fcntl.LOCK_UN)
        lk.close()

    return {"status": "ok", "dry_run": dry_run, "run_id": run_id, "mode": mode,
            "keep_window_start": keep_start, "kept_count": len(keep),
            "candidates": candidates, "pruned": pruned, "skipped": skipped,
            "manifest_path": manifest_path, "records": records,
            "hold_until": (None if dry_run else hold_until),
            "recent_window": rw,
            "daily_staging_mutation": (not dry_run),   # a real run DOES mutate daily staging (move-to-hold)
            "hard_delete": False,                      # never — hold-only; hard delete is ops-only later
            "production_mutation": False}              # meaning: no VM24/live production path is touched
                                                       # by this phase (staging/shadow paths only)
