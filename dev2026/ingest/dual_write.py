"""dev2026 — P2-S6: dual-write ingest (daily store = source of truth; cube follows).

The daily-group store is authoritative. The time-cube is a derived, append-only mirror used
only for the multi-day point/range route (P2-S5). This module keeps the cube in lock-step:

  sync_day(daily, cube, day)   -> read `day` from the daily store, upsert into the cube
  upsert_day(cube, day, data)  -> idempotent write (append if new, overwrite-in-place if exists)
  sync_missing(daily, cube)    -> RECOVERY: append every daily day after the cube's latest that
                                  the cube lacks (e.g. a cube append that failed after the daily
                                  write) — just rerun this; it re-derives from the daily store.
  check_coverage(daily, cube)  -> coverage invariant (daily latest == cube latest, no gaps).

Idempotency: appending an existing day via build_timecube.append_day RAISES (no duplicates);
sync_day/upsert_day are the safe re-runnable path. Out-of-order backfill (a missing day BEFORE
the cube's latest) is not an append — it requires a cube rebuild (flagged by check_coverage).
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional

import numpy as np
import zarr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ingest.build_timecube import VARS, append_day  # noqa: E402
from store.zarr_paths import group_path, list_existing_days  # noqa: E402


def _cube_region(cube_path: str):
    g = zarr.open_group(cube_path, mode="r")
    region = g.attrs.get("region")
    return tuple(region) if region else None


def read_daily_day(daily_path: str, day: str, region=None) -> Dict[str, Optional[np.ndarray]]:
    """Read a day's vars from the daily store (source of truth). Absent var -> None.
    Respects the cube's spatial region (i0,i1,j0,j1)."""
    dpath = group_path(daily_path, day)
    if not os.path.isdir(dpath):
        raise ValueError(f"day {day} not in daily store {daily_path}")
    gd = zarr.open_group(dpath, mode="r")
    if region:
        i0, i1, j0, j1 = region
    else:
        i0, i1, j0, j1 = 0, gd["sst"].shape[-2], 0, gd["sst"].shape[-1]
    out: Dict[str, Optional[np.ndarray]] = {}
    for v in VARS:
        out[v] = np.asarray(gd[v][0, i0:i1, j0:j1]) if (v in gd) else None
    return out


def upsert_day(cube_path: str, day: str, data: dict) -> str:
    """Idempotent: if `day` exists in the cube, overwrite that time slab in place (+ refresh
    validity); else append. Returns 'append' or 'overwrite'."""
    g = zarr.open_group(cube_path, mode="a")
    days = list(g.attrs["days"])
    if day not in days:
        append_day(cube_path, day, data)
        return "append"
    t = days.index(day)
    var_valid = {k: list(v) for k, v in dict(g.attrs.get("var_valid", {})).items()}
    for v in g.attrs.get("vars", VARS):
        arr = g[v]
        present = v in data and data[v] is not None
        if present:
            arr[t, :, :] = np.asarray(data[v], dtype=np.float32)
        else:
            arr[t, :, :] = np.float32(np.nan)
        if v in var_valid:
            var_valid[v][t] = bool(present)
    g.attrs["var_valid"] = var_valid
    return "overwrite"


def sync_day(daily_path: str, cube_path: str, day: str) -> str:
    """Dual-write follow: read `day` from the daily store and upsert into the cube."""
    region = _cube_region(cube_path)
    return upsert_day(cube_path, day, read_daily_day(daily_path, day, region))


def sync_missing(daily_path: str, cube_path: str) -> List[str]:
    """RECOVERY: append every daily day AFTER the cube's latest that the cube lacks, in order.
    Re-runnable. Returns the days synced. (Gaps BEFORE cube latest need a rebuild — see
    check_coverage.)"""
    g = zarr.open_group(cube_path, mode="r")
    cube_days = set(g.attrs["days"])
    cube_latest = g.attrs["days"][-1] if g.attrs["days"] else ""
    todo = [d for d in list_existing_days(daily_path) if d > cube_latest and d not in cube_days]
    for d in todo:
        sync_day(daily_path, cube_path, d)
    return todo


def check_coverage(daily_path: str, cube_path: str) -> dict:
    """Coverage invariant: daily latest == cube latest, and the cube covers all daily days it is
    expected to (>= cube earliest). Reports gaps that need append (sync_missing) vs rebuild."""
    g = zarr.open_group(cube_path, mode="r")
    cube_days = list(g.attrs["days"])
    cube_set = set(cube_days)
    daily = list_existing_days(daily_path)
    daily_latest = daily[-1] if daily else None
    cube_latest = cube_days[-1] if cube_days else None
    cube_earliest = cube_days[0] if cube_days else None
    # daily days strictly WITHIN the cube's existing span [earliest, latest] that are missing
    # (these need a REBUILD); days AFTER cube_latest are just not-yet-appended (sync_missing).
    in_span_missing = [d for d in daily
                       if cube_earliest and cube_latest and cube_earliest <= d <= cube_latest
                       and d not in cube_set]
    after_latest = [d for d in daily if cube_latest and d > cube_latest]
    # cube days that are NOT in the daily store (daily is source of truth -> these are
    # orphans needing a rebuild); and structural problems in the cube's time axis.
    daily_set = set(daily)
    extra_cube_days = [d for d in cube_days if d not in daily_set]
    days_sorted = all(cube_days[i] < cube_days[i + 1] for i in range(len(cube_days) - 1))
    days_unique = len(cube_days) == len(cube_set)
    structural_ok = days_sorted and days_unique and not extra_cube_days
    return {
        "cube_loaded": True,
        "daily_latest": daily_latest, "cube_latest": cube_latest,
        "daily_day_count": len(daily), "cube_day_count": len(cube_days),
        "latest_in_sync": (daily_latest == cube_latest),
        "missing_after_cube_latest": after_latest,         # fix via sync_missing (append)
        "missing_within_cube_span": in_span_missing,       # needs REBUILD (out-of-order)
        "extra_cube_days": extra_cube_days,                # cube has days daily lacks -> REBUILD
        "days_sorted": days_sorted,                        # time axis strictly ascending?
        "days_unique": days_unique,                        # no duplicate days?
        "structural_ok": structural_ok,                    # cube time-axis is well-formed
        "ok": (daily_latest == cube_latest) and not in_span_missing and structural_ok,
    }
