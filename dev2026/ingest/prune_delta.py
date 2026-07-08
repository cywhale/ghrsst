"""dev2026 — P4-S6: safe delta pruning via REBUILD-THEN-SWAP (staging/shadow only for now).

The delta cube's ``attrs['days']`` is in PHYSICAL APPEND order (a backfill lands at the tail), and
``day_index[date]`` maps a date to its physical time slab. You therefore CANNOT prune a delta day by editing
``attrs['days']`` / ``var_valid`` in place or by truncating the time axis — that desyncs ``day_index`` from
the stored slabs and silently corrupts every later read (P4-S4 §2). The ONLY safe primitive is:

    build a FRESH delta containing exactly the kept days, written in CHRONOLOGICAL order, from a trusted
    source (daily staging preferred; else the existing delta read BY ``day_index``), validate it, and hand
    the caller an atomic-SWAP PLAN — the swap itself is a later phase.

This module implements that rebuild + validation and returns the plan. It performs **no swap, no production
mutation, no in-place edit** of the source delta/daily. It writes only the staging ``out_path`` the caller
supplies (which must differ from the live delta). Compatible with the P4-S5 audit output (consumes its
``keep_days`` / eligibility) and with the P4-S4 safety model. Production wiring + the atomic swap are P4-S7/S8/S9.

Spec: ``specs/p4_ingest_prune_retention_design.md`` (§2, §5). Results: ``specs/p4s6_delta_prune_results.md``.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Dict, List, Optional, Sequence

import numpy as np
import zarr

from ingest.dual_write import DELTA_SPATIAL_CHUNK, DELTA_SHARD_SPATIAL, append_to_delta, read_daily_day
from store.zarr_paths import group_exists


# --------------------------------------------------------------------------- calendar helpers (mirror P4-S5 §4.1)
def _d(s: str) -> date:
    return date.fromisoformat(s)


def _calendar_range(start: str, end: str) -> List[str]:
    d0, d1 = _d(start), _d(end)
    return [] if d0 > d1 else [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def recent_window_contiguous(delta_days: Sequence[str], spatial_window_days: int) -> dict:
    """Reproduce the P4-S5 §4.1 gate: the latest ``spatial_window_days`` CALENDAR days ending at
    max(delta_days) must all be present. Returns {contiguous, missing, window_start, window_end}."""
    dd = sorted(set(delta_days))
    if not dd:
        return {"contiguous": False, "missing": [], "window_start": None, "window_end": None,
                "reason": "no delta days"}
    end = dd[-1]
    start = (_d(end) - timedelta(days=spatial_window_days - 1)).isoformat()
    required = _calendar_range(start, end)
    present = set(dd)
    missing = [x for x in required if x not in present]
    return {"contiguous": not missing, "missing": missing, "window_start": start, "window_end": end}


# --------------------------------------------------------------------------- read-only source access
def _open_delta_meta(delta_path: str) -> dict:
    """Read-only snapshot of a delta cube's metadata (physical-order days + day_index + var_valid)."""
    g = zarr.open_group(delta_path, mode="r")                 # READ-ONLY
    days = list(g.attrs["days"])                              # physical/append order
    vars_ = list(g.attrs.get("vars", []))
    vv = {v: list(x) for v, x in dict(g.attrs.get("var_valid", {})).items()}
    a0 = g[vars_[0]] if vars_ else None
    ny = int(a0.shape[1]) if a0 is not None else 0
    nx = int(a0.shape[2]) if a0 is not None else 0
    return {
        "group": g,
        "days": days,
        "day_index": {d: i for i, d in enumerate(days)},      # date -> PHYSICAL slab index
        "vars": vars_,
        "var_valid": vv,
        "ny": ny, "nx": nx,
        "region": (tuple(g.attrs["region"]) if "region" in g.attrs else None),
    }


def _read_delta_day(meta: dict, day: str) -> Dict[str, Optional[np.ndarray]]:
    """Read one day's vars from an existing delta, addressed BY ``day_index[day]`` (its PHYSICAL slab) —
    never by position in any keep list. Absent var (var_valid False) -> None."""
    t = meta["day_index"][day]                                # physical index, robust to append order
    g = meta["group"]
    out: Dict[str, Optional[np.ndarray]] = {}
    for v in meta["vars"]:
        vv = meta["var_valid"].get(v, [True] * len(meta["days"]))
        present = (v in g) and bool(vv[t])
        out[v] = np.asarray(g[v][t, :, :]) if present else None
    return out


# --------------------------------------------------------------------------- staging writer (delta-source path)
def _append_prebuilt_day(out_path: str, day: str, data: Dict[str, Optional[np.ndarray]],
                         ny: int, nx: int, lon: np.ndarray, lat: np.ndarray, region: Optional[tuple],
                         spatial_chunk: int, shard_spatial: int) -> None:
    """Append one in-memory day into the staging delta with the SAME var-union convention as append_to_delta,
    so the two writers can be MIXED on the same output: a var first seen on a later day is created with a
    NaN backfill + ``var_valid False`` for the prior days. finalize ``attrs['days']`` LAST; absent var ->
    NaN slab + valid False. Used for the delta-source fallback (staging/shadow scale)."""
    present_today = [v for v, arr in data.items() if arr is not None]
    if not os.path.isdir(out_path):                           # CREATE (empty, time_chunk=1)
        cy, cx = min(spatial_chunk, ny), min(spatial_chunk, nx)
        sh_y = min((shard_spatial // cy) * cy or cy, ny)
        sh_x = min((shard_spatial // cx) * cx or cx, nx)
        g = zarr.open_group(out_path, mode="w", zarr_format=3)
        g.create_array("lon", shape=(nx,), dtype="float32", chunks=(nx,)); g["lon"][:] = lon.astype(np.float32)
        g.create_array("lat", shape=(ny,), dtype="float32", chunks=(ny,)); g["lat"][:] = lat.astype(np.float32)
        # create arrays only for vars PRESENT today (matches append_to_delta's create branch); absent-today
        # vars are added later when they first appear present, with a NaN backfill.
        for v in present_today:
            g.create_array(v, shape=(0, ny, nx), dtype="float32", chunks=(1, cy, cx),
                           shards=(1, sh_y, sh_x), fill_value=float("nan"))
        g.attrs["days"] = []
        g.attrs["vars"] = list(present_today)
        g.attrs["var_valid"] = {v: [] for v in present_today}
        if region:
            g.attrs["region"] = [int(x) for x in region]
        g.attrs["layout"] = "time_lat_lon"
    else:
        g = zarr.open_group(out_path, mode="a")
    days = list(g.attrs["days"]); t = len(days)
    delta_vars = list(g.attrs.get("vars", []))
    var_valid = {k: list(v) for k, v in dict(g.attrs.get("var_valid", {})).items()}
    for v in delta_vars:
        var_valid.setdefault(v, [True] * t)
    # derive chunk/shard for any NEW var from an existing array (else the create-branch params)
    if delta_vars:
        a0 = g[delta_vars[0]]
        cy, cx = int(a0.chunks[-2]), int(a0.chunks[-1])
        sh_y, sh_x = int(a0.shards[-2]), int(a0.shards[-1])
    else:
        cy, cx = min(spatial_chunk, ny), min(spatial_chunk, nx)
        sh_y = min((shard_spatial // cy) * cy or cy, ny); sh_x = min((shard_spatial // cx) * cx or cx, nx)
    for v in present_today:                                   # NEW var present today but not yet in the delta
        if v not in delta_vars:
            g.create_array(v, shape=(t, ny, nx), dtype="float32", chunks=(1, cy, cx),
                           shards=(1, sh_y, sh_x), fill_value=float("nan"))
            delta_vars.append(v)
            var_valid[v] = [False] * t                        # var was absent for every prior day
    for v in delta_vars:
        arr = g[v]
        arr.resize((t + 1, ny, nx))
        present = data.get(v) is not None
        arr[t, :, :] = np.asarray(data[v], dtype=np.float32) if present else np.float32(np.nan)
        var_valid.setdefault(v, [True] * t).append(bool(present))
    g.attrs["vars"] = list(delta_vars)
    g.attrs["var_valid"] = var_valid
    g.attrs["days"] = days + [day]                            # FINALIZE days LAST (never a half day)


# --------------------------------------------------------------------------- the helper
def prune_delta(delta_path: str, out_path: str, keep_days: Sequence[str], *,
                source_daily: Optional[str] = None, source_delta: Optional[str] = None,
                base_days: Optional[Sequence[str]] = None,
                spatial_window_days: int = 31,
                audit_recent_window: Optional[dict] = None,
                region: Optional[tuple] = None,
                spatial_chunk: int = DELTA_SPATIAL_CHUNK, shard_spatial: int = DELTA_SHARD_SPATIAL,
                sample_n: int = 32, seed: int = 0, workers: int = 2) -> dict:
    """Rebuild a delta containing exactly ``keep_days`` (chronological) into the staging ``out_path``, then
    return an atomic-SWAP PLAN. Never swaps, never mutates ``delta_path`` / the sources.

    Sourcing per kept day: ``source_delta`` defaults to ``delta_path``. Prefer ``source_daily`` when it has
    the day (tiled, production-scale via append_to_delta); else read the old delta BY ``day_index``.

    Fail-closed gates (any failure -> status='refused', NOTHING built/swapped):
    - ``keep_days`` non-empty and a subset of the original delta days;
    - **recent-window contiguity is ALWAYS decided by LOCAL recomputation** on the original delta (P4-S5
      §4.1). ``audit_recent_window`` (the P4-S5 ``recent_spatial_window`` dict) is OPTIONAL and can only
      *corroborate*: if it disagrees with the local recompute -> refuse (fail-closed). It can never
      force-pass a locally-detected gap. A hole -> refuse.
    - **base coverage is MANDATORY whenever anything is dropped**: if ``dropped_days`` is non-empty and
      ``base_days`` is None -> refuse. ``base_days=None`` is allowed ONLY for a pure rebuild/reorder
      (``dropped_days`` empty). When given, every dropped day must be covered by base.
    """
    source_delta = source_delta or delta_path
    if os.path.abspath(out_path) == os.path.abspath(delta_path):
        raise ValueError("out_path must differ from delta_path (never build over the live delta)")
    if os.path.exists(out_path):
        raise ValueError(f"out_path already exists (use a fresh staging path): {out_path}")

    orig = _open_delta_meta(delta_path)
    orig_days = set(orig["days"])
    keep_sorted = sorted(set(keep_days))
    dropped = sorted(orig_days - set(keep_sorted))

    def _refuse(reason, **extra):
        return {"status": "refused", "reason": reason, "delta_path": delta_path, "out_path": out_path,
                "keep_days": keep_sorted, "dropped_days": dropped, "production_mutation": False,
                "swap_performed": False, **extra}

    if not keep_sorted:
        return _refuse("keep_days is empty — refusing to build an empty delta")
    missing_keep = [d for d in keep_sorted if d not in orig_days]
    if missing_keep:
        return _refuse(f"keep_days contains days not in the source delta: {missing_keep}")

    # recent-window gate — LOCAL recompute always decides (never a caller override).
    rw = recent_window_contiguous(orig["days"], spatial_window_days)
    if audit_recent_window is not None:
        # audit may only corroborate; a mismatch is suspicious -> fail-closed.
        a_contig = audit_recent_window.get("recent_window_contiguous")
        a_missing = sorted(audit_recent_window.get("missing_in_window", []) or [])
        if a_contig != rw["contiguous"] or a_missing != sorted(rw["missing"]):
            return _refuse("audit recent-window disagrees with local recomputation — refusing (fail-closed)",
                           recent_window=rw, audit_recent_window=audit_recent_window)
    if not rw["contiguous"]:
        return _refuse("recent spatial window has holes — repair before pruning (P4-S4 §4.1)",
                       recent_window=rw)

    # keep-preserves-window gate — the kept set MUST retain the ENTIRE active recent spatial window
    # (rw.window_start..rw.window_end). Dropping a day inside the window would break bbox / POST /points for
    # that day after the P4-S8 swap, even if base covers it (base is bbox-hostile). Fail-closed (P4-S4 §5).
    required_window = _calendar_range(rw["window_start"], rw["window_end"])
    keep_set = set(keep_sorted)
    missing_from_keep_window = [d for d in required_window if d not in keep_set]
    if missing_from_keep_window:
        return _refuse("keep_days drops day(s) inside the active recent spatial window — would break the "
                       "spatial-window policy after swap; refuse (repair the keep-set to retain the window)",
                       recent_window=rw, missing_from_keep_window=missing_from_keep_window)

    # base-coverage gate — MANDATORY when dropping anything. base_days=None only for pure rebuild/reorder.
    if dropped:
        if base_days is None:
            return _refuse("base_days is REQUIRED when dropping days (mandatory base-coverage gate); "
                           "base_days=None is allowed only for a pure rebuild/reorder with no dropped days")
        uncovered = [d for d in dropped if d not in set(base_days)]
        if uncovered:
            return _refuse("dropped days not covered by base (compaction must run first): "
                           f"{uncovered}", base_uncovered=uncovered)

    # ---- rebuild new_delta chronologically ----
    daily_has = (lambda day: bool(source_daily) and group_exists(source_daily, day))
    lon = np.asarray(orig["group"]["lon"][:])
    lat = np.asarray(orig["group"]["lat"][:])
    src_used = set()
    for day in keep_sorted:                                   # CHRONOLOGICAL write order
        if daily_has(day):
            append_to_delta(source_daily, out_path, day, region=region,
                            spatial_chunk=spatial_chunk, shard_spatial=shard_spatial, workers=workers)
            src_used.add("daily")
        else:                                                 # fallback: read old delta BY day_index
            data = _read_delta_day(orig, day)
            _append_prebuilt_day(out_path, day, data, orig["ny"], orig["nx"], lon, lat,
                                 orig["region"], spatial_chunk, shard_spatial)
            src_used.add("delta")

    validation = _validate(out_path, keep_sorted, orig, source_daily, region, sample_n, seed,
                           spatial_window_days)
    status = "ok" if validation["all_ok"] else "invalid"
    return {
        "status": status,
        "delta_path": delta_path,
        "out_path": out_path,
        "source": ("+".join(sorted(src_used)) if src_used else None),
        "keep_days": keep_sorted,
        "dropped_days": dropped,
        "recent_window": rw,
        "validation": validation,
        "swap_plan": {
            "action": "atomic_swap",
            "from": out_path,
            "to": delta_path,
            "backup": delta_path + ".pre-prune",
            "performed": False,
            "note": ("P4-S6 returns the plan ONLY — no swap performed. A later phase (P4-S7/S8/S9) does the "
                     "atomic swap (keep <to>.pre-prune until /healthz confirms) then triggers cube refresh."),
        },
        "production_mutation": False,
        "swap_performed": False,
    }


# --------------------------------------------------------------------------- validation
def _validate(out_path: str, keep_sorted: List[str], orig: dict, source_daily: Optional[str],
              region: Optional[tuple], sample_n: int, seed: int, spatial_window_days: int) -> dict:
    """Validate the rebuilt delta: day set == keep_sorted (sorted, unique), latest == max(days); the rebuilt
    delta's own recent spatial window is contiguous (post-build invariant); var_valid preserved (incl.
    absent-var); sample parity per kept day (float32, NaN-aware) vs source."""
    ng = zarr.open_group(out_path, mode="r")                 # READ-ONLY
    new_days = list(ng.attrs["days"])
    new_index = {d: i for i, d in enumerate(new_days)}
    new_vv = {v: list(x) for v, x in dict(ng.attrs.get("var_valid", {})).items()}

    day_set_ok = (new_days == keep_sorted)                   # written chronologically -> already sorted
    unique = (len(new_days) == len(set(new_days)))
    is_sorted = (new_days == sorted(new_days))
    latest_ok = (max(new_days) == keep_sorted[-1]) if new_days else False
    new_rw = recent_window_contiguous(new_days, spatial_window_days)   # post-build window invariant
    recent_window_ok = new_rw["contiguous"]

    rng = np.random.default_rng(seed)
    var_valid_ok = True
    parity_ok = True
    parity_checked = 0
    for day in keep_sorted:
        # expected per-var presence from the SOURCE (daily if it has the day, else old delta)
        if source_daily and group_exists(source_daily, day):
            src = read_daily_day(source_daily, day, region)  # {v: ndarray|None}
        else:
            src = _read_delta_day(orig, day)
        t = new_index[day]
        for v, sval in src.items():
            present = sval is not None
            # var_valid parity (absent var must be recorded False)
            if v in new_vv:
                if bool(new_vv[v][t]) != present:
                    var_valid_ok = False
            elif present:
                var_valid_ok = False
            if not present:
                continue
            arr = np.asarray(sval, dtype=np.float32)
            ny, nx = arr.shape
            n = min(sample_n, ny * nx)
            flat = rng.choice(ny * nx, size=n, replace=False)
            ii, jj = np.divmod(flat, nx)
            got = np.asarray(ng[v][t, :, :], dtype=np.float32)[ii, jj]
            exp = arr[ii, jj]
            both_nan = np.isnan(got) & np.isnan(exp)
            eq = both_nan | (got == exp)
            parity_checked += int(n)
            if not bool(np.all(eq)):
                parity_ok = False
    all_ok = (day_set_ok and unique and is_sorted and latest_ok and recent_window_ok
              and var_valid_ok and parity_ok)
    return {
        "day_set_ok": day_set_ok, "unique": unique, "sorted_chronological": is_sorted,
        "latest": (max(new_days) if new_days else None), "latest_ok": latest_ok,
        "recent_window_ok": recent_window_ok, "recent_window_missing": new_rw["missing"],
        "var_valid_ok": var_valid_ok, "parity_ok": parity_ok, "parity_cells_checked": parity_checked,
        "new_day_count": len(new_days), "all_ok": all_ok,
    }
