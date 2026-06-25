"""dev2026 — bulk time-cube builder (fixes the slow per-day write pattern).

Problem: `build_timecube` writes ONE day at a time (`arr[i,:,:] = ...`). With a shard that spans
`time_chunk` steps (e.g. 90), each single-day write read-modify-writes the WHOLE shard → ~time_chunk×
write amplification → builds that take many days at global scale.

Fix: write by **time_block × spatial_shard block**. Assemble one block of shape
`(time_chunk, read_block, read_block)` from the daily source days, then write it ONCE — the write
covers complete shards, so each shard is written exactly once (no partial-shard RMW).

Features (per review):
  * bulk shard-block writes (time_block × spatial super-tile, super-tile = k×shard)
  * bounded parallelism (default 4 workers) over disjoint (var, time_block, tile) units
  * checkpoint/resume per (var, time_block, tile) in `<out>/_build_checkpoint.json`
  * `--latest-days N`: build only the most recent N days first (router falls back to daily for
    older ranges); extend later by rebuilding with a larger N (front-extension isn't a zarr append).

Produces the SAME cube layout as build_timecube (TimeCubeStore reads it identically): same chunk,
shards, fill=NaN, attrs (days, vars, var_valid, region, layout).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import numpy as np
import zarr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from store.zarr_paths import group_path, list_existing_days  # noqa: E402

VARS = ("sst", "sst_anomaly", "sea_ice")


def _ckpt_path(out):
    return os.path.join(out, "_build_checkpoint.json")


def build_timecube_bulk(src: str, out: str, spatial_chunk: int = 8, time_chunk: int = 90,
                        shard_spatial: int = 128, region: Optional[tuple] = None,
                        read_block: Optional[int] = None, workers: int = 4,
                        latest_days: Optional[int] = None, end_day: Optional[str] = None,
                        exclude_latest: bool = False, overwrite: bool = False,
                        resume: bool = True) -> dict:
    all_days = list_existing_days(src)
    if not all_days:
        raise SystemExit(f"no days under {src}")
    days = list(all_days)
    if end_day:                                  # holdout: build through end_day inclusive
        days = [d for d in days if d <= end_day]
    if exclude_latest and days:                  # holdout: drop the latest day for a true-append test
        days = days[:-1]
    if latest_days:
        days = days[-latest_days:]
    T = len(days)
    tc = min(time_chunk, T)

    g0 = zarr.open_group(group_path(src, days[0]), mode="r")
    lon_full = np.asarray(g0["lon"][:]); lat_full = np.asarray(g0["lat"][:])
    i0, i1, j0, j1 = region if region else (0, lat_full.size, 0, lon_full.size)
    lon = lon_full[j0:j1]; lat = lat_full[i0:i1]
    ny, nx = lat.size, lon.size
    cy = min(spatial_chunk, ny); cx = min(spatial_chunk, nx)
    sh_y = min((shard_spatial // cy) * cy or cy, ny)
    sh_x = min((shard_spatial // cx) * cx or cx, nx)
    shards = (tc, sh_y, sh_x)
    # read_block: multiple of shard (so writes cover whole shards), default = the daily source chunk
    # spatial size (read each daily chunk once) clamped to grid + made a shard multiple.
    daily_cy = int(g0["sst"].chunks[-2]) if "sst" in g0 else sh_y
    rb = read_block or daily_cy
    rb_y = max(sh_y, (rb // sh_y) * sh_y or sh_y); rb_y = min(rb_y, ((ny + sh_y - 1)//sh_y)*sh_y)
    rb_x = max(sh_x, (rb // sh_x) * sh_x or sh_x); rb_x = min(rb_x, ((nx + sh_x - 1)//sh_x)*sh_x)

    # per-(day,var) validity from filesystem presence (same as build_timecube)
    valid = {}
    for v in VARS:
        flags = [os.path.isdir(os.path.join(group_path(src, d), v)) for d in days]
        if any(flags):
            valid[v] = flags
    union_vars = list(valid.keys())

    fresh = True
    if os.path.exists(out):
        if resume and os.path.isfile(_ckpt_path(out)):
            fresh = False                                  # resume into existing partial build
        elif overwrite:
            import shutil; shutil.rmtree(out)
        else:
            raise FileExistsError(f"--out exists: {out} (use --overwrite, or resume a partial build)")

    if fresh:
        g = zarr.open_group(out, mode="w", zarr_format=3)
        g.create_array("lon", shape=(nx,), dtype="float32", chunks=(nx,)); g["lon"][:] = lon.astype(np.float32)
        g.create_array("lat", shape=(ny,), dtype="float32", chunks=(ny,)); g["lat"][:] = lat.astype(np.float32)
        for v in union_vars:
            g.create_array(v, shape=(T, ny, nx), dtype="float32", chunks=(tc, cy, cx),
                           shards=shards, fill_value=float("nan"))
        g.attrs["days"] = days; g.attrs["vars"] = union_vars
        g.attrs["var_valid"] = {v: [bool(x) for x in f] for v, f in valid.items()}
        g.attrs["region"] = [int(i0), int(i1), int(j0), int(j1)]; g.attrs["layout"] = "time_lat_lon"
        with open(_ckpt_path(out), "w") as fh:
            json.dump({"done": [], "T": T, "shards": list(shards)}, fh)
    g = zarr.open_group(out, mode="a")

    # work units: (var, time_block, tile_i, tile_j); checkpoint by key
    units = []
    for v in union_vars:
        for t0 in range(0, T, tc):
            for ti in range(0, ny, rb_y):
                for tj in range(0, nx, rb_x):
                    units.append((v, t0, ti, tj))
    with open(_ckpt_path(out)) as fh:
        done = set(json.load(fh)["done"])
    pending = [u for u in units if f"{u[0]}|{u[1]}|{u[2]}|{u[3]}" not in done]

    ck_lock = threading.Lock()
    arr_handles = {v: g[v] for v in union_vars}

    def process(unit):
        v, t0, ti, tj = unit
        t1 = min(t0 + tc, T); bi1 = min(ti + rb_y, ny); bj1 = min(tj + rb_x, nx)
        block = np.full((t1 - t0, bi1 - ti, bj1 - tj), np.nan, np.float32)
        for d, day in enumerate(days[t0:t1]):
            if not valid[v][t0 + d]:
                continue
            gd = zarr.open_group(group_path(src, day), mode="r")
            block[d] = np.asarray(gd[v][0, i0 + ti:i0 + bi1, j0 + tj:j0 + bj1])
        arr_handles[v][t0:t1, ti:bi1, tj:bj1] = block        # ONE shard-block write
        key = f"{v}|{t0}|{ti}|{tj}"
        with ck_lock:
            with open(_ckpt_path(out)) as fh:
                c = json.load(fh)
            c["done"].append(key)
            with open(_ckpt_path(out), "w") as fh:
                json.dump(c, fh)

    t_start = time.perf_counter()
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(process, pending))
    else:
        for u in pending:
            process(u)
    build_s = round(time.perf_counter() - t_start, 2)

    nfiles = sum(len(f) for _, _, f in os.walk(out))
    return {"out": out, "days": T, "grid": [ny, nx], "chunk": [tc, cy, cx], "shards": list(shards),
            "read_block": [rb_y, rb_x], "vars": union_vars, "workers": workers,
            "units_total": len(units), "units_done_this_run": len(pending),
            "file_count": nfiles, "build_s": build_s,
            "bytes_on_disk": sum(os.path.getsize(os.path.join(r, f))
                                 for r, _, fs in os.walk(out) for f in fs)}


def bulk_append_day(src: str, cube: str, day: str, workers: int = 4,
                    read_block: Optional[int] = None) -> dict:
    """Short-term append fix: write a day into the cube TILE-by-TILE with bounded workers and NO
    full-global array in memory (one tile per worker). Resumable per (var,tile) via a per-day
    checkpoint. Append (new day, resize +1) or upsert (existing day, write in place).

    NOTE: with a time_chunk>1 base cube this STILL read-modify-writes the time-spanning shard (that
    is inherent to writing one day into a 90-day shard) — it only removes the memory blow-up and
    parallelizes/resumes it. The production-cheap append is the base+delta+compaction design
    (store/tiered_cube.py); use this only as an interim until that lands."""
    g = zarr.open_group(cube, mode="a")
    days = list(g.attrs["days"])
    region = g.attrs.get("region")
    i0, j0 = (region[0], region[2]) if region else (0, 0)
    union_vars = list(g.attrs.get("vars", VARS))
    var_valid = {k: list(v) for k, v in dict(g.attrs.get("var_valid", {})).items()}
    ny, nx = g["sst"].shape[-2], g["sst"].shape[-1] if "sst" in g else g[union_vars[0]].shape[-2:]
    a0 = g[union_vars[0]]
    sh_y, sh_x = a0.shards[-2], a0.shards[-1]
    rb = read_block or max(sh_y, sh_x)
    rb_y = max(sh_y, (rb // sh_y) * sh_y or sh_y); rb_x = max(sh_x, (rb // sh_x) * sh_x or sh_x)

    appended = day not in days
    if appended:
        t = len(days)
        for v in union_vars:
            arr = g[v]; arr.resize((t + 1, arr.shape[1], arr.shape[2]))
    else:
        t = days.index(day)

    gd = zarr.open_group(group_path(src, day), mode="r")
    present = {v: (v in gd) for v in union_vars}
    ck = os.path.join(cube, f"_append_{day}.json")
    if os.path.isfile(ck):
        with open(ck) as fh:
            done = set(json.load(fh)["done"])
    else:
        done = set()
    ck_lock = threading.Lock()

    units = [(v, ti, tj) for v in union_vars
             for ti in range(0, ny, rb_y) for tj in range(0, nx, rb_x)
             if f"{v}|{ti}|{tj}" not in done]

    def process(u):
        v, ti, tj = u
        bi1 = min(ti + rb_y, ny); bj1 = min(tj + rb_x, nx)
        if present[v]:
            block = np.asarray(gd[v][0, i0 + ti:i0 + bi1, j0 + tj:j0 + bj1])
        else:
            block = np.full((bi1 - ti, bj1 - tj), np.nan, np.float32)
        g[v][t, ti:bi1, tj:bj1] = block          # tiled write (RMW of that shard only)
        with ck_lock:
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
            list(ex.map(process, units))
    else:
        for u in units:
            process(u)
    # finalize attrs (days + validity) and clear the per-day checkpoint
    if appended:
        g.attrs["days"] = days + [day]
        for v in union_vars:
            var_valid.setdefault(v, [True] * len(days)).append(bool(present[v]))
    else:
        for v in union_vars:
            if v in var_valid:
                var_valid[v][t] = bool(present[v])
    g.attrs["var_valid"] = var_valid
    if os.path.isfile(ck):
        os.remove(ck)
    return {"day": day, "op": "append" if appended else "upsert",
            "tiles": len(units), "append_s": round(time.perf_counter() - t0, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--spatial-chunk", type=int, default=8, dest="spatial_chunk")
    ap.add_argument("--time-chunk", type=int, default=90, dest="time_chunk")
    ap.add_argument("--shard-spatial", type=int, default=128, dest="shard_spatial")
    ap.add_argument("--read-block", type=int, default=None, dest="read_block",
                    help="spatial super-tile (multiple of shard; default = daily source chunk size)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--latest-days", type=int, default=None, dest="latest_days",
                    help="build only the most recent N days first (extend later by rebuilding larger N)")
    ap.add_argument("--end-day", default=None, dest="end_day",
                    help="build through this day inclusive (holdout for a TRUE append test)")
    ap.add_argument("--exclude-latest", action="store_true", dest="exclude_latest",
                    help="drop the latest day (holdout for a TRUE append test)")
    ap.add_argument("--region", default=None, help="i0,i1,j0,j1 lat/lon index slice")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--no-resume", action="store_true", dest="no_resume")
    args = ap.parse_args()
    region = tuple(int(x) for x in args.region.split(",")) if args.region else None
    meta = build_timecube_bulk(args.src, args.out, args.spatial_chunk, args.time_chunk,
                               args.shard_spatial, region=region, read_block=args.read_block,
                               workers=args.workers, latest_days=args.latest_days,
                               end_day=args.end_day, exclude_latest=args.exclude_latest,
                               overwrite=args.overwrite, resume=not args.no_resume)
    print(f"bulk build -> {args.out}")
    print(f"  days={meta['days']} chunk={meta['chunk']} shards={meta['shards']} read_block={meta['read_block']} "
          f"workers={meta['workers']} units={meta['units_done_this_run']}/{meta['units_total']} "
          f"files={meta['file_count']} disk={meta['bytes_on_disk']/1e6:.1f}MB build={meta['build_s']}s")


if __name__ == "__main__":
    main()
