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

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import numpy as np
import zarr

try:
    import psutil
    _PROC = psutil.Process()
except Exception:
    _PROC = None


def _rss_mb():
    return round(_PROC.memory_info().rss / 1e6, 1) if _PROC else None

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ingest.build_timecube import VARS, append_day, build_timecube  # noqa: E402
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


# ---------------------------------------------------------------------------
# Base + delta + compaction (production cheap-append design; store/tiered_cube.py)
# ---------------------------------------------------------------------------
def append_to_delta(daily_path: str, delta_path: str, day: str, *, spatial_chunk: int = 8,
                    shard_spatial: int = 128, region: Optional[tuple] = None,
                    workers: int = 4, read_block: Optional[int] = None) -> dict:
    """TILED/STREAMING append of one day into the DELTA cube (time_chunk=1 -> fresh shard, no RMW).

    Production daily-ingest path (used by VM24 + cron). Reads/writes ONE spatial tile at a time so it
    NEVER materializes a full global array (peak block = read_block² per worker, not ny×nx). Creates
    the delta if missing (time_chunk=1, spatial_chunk=8, shard_spatial=128, same region/coords/vars/
    var_valid semantics). Bounded workers; checkpoint/resume per (day,var,tile) in
    `<delta>/_delta_append_<day>.json` (safe to rerun after interruption). Preserves P1 semantics:
    absent var -> valid False (omit); present NaN -> null; values match the daily store.

    Returns {op: create|append|overwrite, append_s, tile_count, max_block_cells, rss_mb}.
    """
    gd = zarr.open_group(group_path(daily_path, day), mode="r")
    lon_full = np.asarray(gd["lon"][:]); lat_full = np.asarray(gd["lat"][:])
    i0, i1, j0, j1 = region if region else (0, lat_full.size, 0, lon_full.size)
    ny, nx = i1 - i0, j1 - j0
    cy, cx = min(spatial_chunk, ny), min(spatial_chunk, nx)
    sh_y = min((shard_spatial // cy) * cy or cy, ny)
    sh_x = min((shard_spatial // cx) * cx or cx, nx)
    daily_cy = int(gd["sst"].chunks[-2]) if "sst" in gd else sh_y
    rb = read_block or daily_cy
    rb_y = min(max(sh_y, (rb // sh_y) * sh_y or sh_y), ((ny + sh_y - 1) // sh_y) * sh_y)
    rb_x = min(max(sh_x, (rb // sh_x) * sh_x or sh_x), ((nx + sh_x - 1) // sh_x) * sh_x)
    day_vars = [v for v in VARS if v in gd]

    if not os.path.isdir(delta_path):           # CREATE (empty, time_chunk=1), then unified append
        g = zarr.open_group(delta_path, mode="w", zarr_format=3)
        g.create_array("lon", shape=(nx,), dtype="float32", chunks=(nx,)); g["lon"][:] = lon_full[j0:j1].astype(np.float32)
        g.create_array("lat", shape=(ny,), dtype="float32", chunks=(ny,)); g["lat"][:] = lat_full[i0:i1].astype(np.float32)
        for v in day_vars:
            g.create_array(v, shape=(0, ny, nx), dtype="float32", chunks=(1, cy, cx),
                           shards=(1, sh_y, sh_x), fill_value=float("nan"))
        g.attrs["days"] = []; g.attrs["vars"] = list(day_vars)
        g.attrs["var_valid"] = {v: [] for v in day_vars}
        g.attrs["region"] = [int(i0), int(i1), int(j0), int(j1)]; g.attrs["layout"] = "time_lat_lon"
        created = True
    else:
        g = zarr.open_group(delta_path, mode="a"); created = False

    days = list(g.attrs["days"]); Tcur = len(days)
    delta_vars = list(g.attrs.get("vars", day_vars))
    var_valid = {k: list(val) for k, val in dict(g.attrs.get("var_valid", {})).items()}
    for v in delta_vars:
        var_valid.setdefault(v, [True] * Tcur)
    # add any var present today but missing from the delta (NaN-filled + validity FALSE for prior days)
    for v in day_vars:
        if v not in delta_vars:
            g.create_array(v, shape=(Tcur, ny, nx), dtype="float32", chunks=(1, cy, cx),
                           shards=(1, sh_y, sh_x), fill_value=float("nan"))
            delta_vars.append(v)
            var_valid[v] = [False] * Tcur        # var was absent for every existing day

    overwrite = day in days
    t = days.index(day) if overwrite else Tcur
    op = "overwrite" if overwrite else ("create" if created else "append")
    if not overwrite:
        for v in delta_vars:
            arr = g[v]; arr.resize((t + 1, arr.shape[1], arr.shape[2]))

    ck = os.path.join(delta_path, f"_delta_append_{day}.json")
    if os.path.isfile(ck):
        with open(ck) as fh:
            done = set(json.load(fh)["done"])
    else:
        done = set()
    units = [(v, ti, tj) for v in delta_vars
             for ti in range(0, ny, rb_y) for tj in range(0, nx, rb_x)
             if f"{v}|{ti}|{tj}" not in done]
    lock = threading.Lock(); max_cells = [0]

    def proc(u):
        v, ti, tj = u
        bi1 = min(ti + rb_y, ny); bj1 = min(tj + rb_x, nx)
        if v in day_vars:                        # read ONLY this tile from the daily store
            block = np.asarray(gd[v][0, i0 + ti:i0 + bi1, j0 + tj:j0 + bj1])
        else:
            block = np.full((bi1 - ti, bj1 - tj), np.nan, np.float32)
        g[v][t, ti:bi1, tj:bj1] = block          # write ONLY this tile (no full-global array)
        with lock:
            max_cells[0] = max(max_cells[0], block.size)
            if os.path.isfile(ck):
                with open(ck) as fh:
                    d = json.load(fh)["done"]
            else:
                d = []
            d.append(f"{v}|{ti}|{tj}")
            with open(ck, "w") as fh:
                json.dump({"done": d}, fh)

    t0 = time.perf_counter()
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(proc, units))
    else:
        for u in units:
            proc(u)

    for v in delta_vars:                         # validity + days, finalize, clear checkpoint
        present = v in day_vars
        if overwrite:
            var_valid[v][t] = bool(present)
        else:
            var_valid.setdefault(v, [True] * Tcur).append(bool(present))
    g.attrs["vars"] = delta_vars
    g.attrs["var_valid"] = var_valid
    if not overwrite:
        g.attrs["days"] = days + [day]
    if os.path.isfile(ck):
        os.remove(ck)
    return {"op": op, "append_s": round(time.perf_counter() - t0, 2),
            "tile_count": len(units), "max_block_cells": max_cells[0],
            "grid_cells": ny * nx, "rss_mb": _rss_mb()}


def compact(daily_path: str, base_path: str, delta_path: str, end_day: str, *,
            spatial_chunk: int = 8, time_chunk: int = 90, shard_spatial: int = 128,
            region: Optional[tuple] = None, workers: int = 4) -> dict:
    """Fold the delta into the base: bulk-REBUILD the base through `end_day` (fast bulk builder),
    and rebuild the delta with only days AFTER `end_day`. Returns staging paths; the caller swaps
    base<-new_base and delta<-new_delta atomically (same filesystem). The daily store is the source
    of truth, so this is a clean re-derive — no in-place base mutation while serving."""
    from ingest.build_timecube_bulk import build_timecube_bulk
    new_base = f"{base_path}.compact"
    build_timecube_bulk(daily_path, new_base, spatial_chunk=spatial_chunk, time_chunk=time_chunk,
                        shard_spatial=shard_spatial, region=region, workers=workers,
                        end_day=end_day, overwrite=True)
    later = [d for d in list_existing_days(daily_path) if d > end_day]
    new_delta = None
    if later:
        new_delta = f"{delta_path}.compact"
        if os.path.exists(new_delta):
            import shutil; shutil.rmtree(new_delta)
        for d in later:                          # tiled append (no full-global array)
            append_to_delta(daily_path, new_delta, d, spatial_chunk=spatial_chunk,
                            shard_spatial=shard_spatial, region=region, workers=workers)
    return {"new_base": new_base, "new_delta": new_delta, "compacted_through": end_day,
            "delta_days_after": later}


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
