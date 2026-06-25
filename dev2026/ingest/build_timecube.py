"""dev2026 — P2-S3: daily-group store -> time-cube (time, lat, lon) converter (Candidate A).

Converts a regional daily-group fixture into a time-major Zarr v3 store so a point
time-series reads O(days/time_chunk) chunks of (time_chunk × small_spatial × small_spatial)
instead of O(days) chunks of (1024×1024). Optional **v3 sharding** packs many small spatial
chunks into one shard file to avoid file-count explosion.

Layout:
  group/
    lon (nx,)  lat (ny,)            coords
    attrs["days"] = [ISO dates...]  time index (order == time axis)
    sst / sst_anomaly / sea_ice  (T, ny, nx)  chunk=(time_chunk, cy, cx) [+ shards]

Callable `build_timecube(...)` for sweeps; CLI for one-offs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

import numpy as np
import zarr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from store.zarr_paths import group_path, list_existing_days  # noqa: E402

VARS = ("sst", "sst_anomaly", "sea_ice")


def _count_files(path: str) -> int:
    return sum(len(files) for _, _, files in os.walk(path))


def build_timecube(src: str, out: str, spatial_chunk: int = 8,
                   time_chunk: Optional[int] = None, shard_spatial: Optional[int] = None,
                   region: Optional[tuple] = None, days: Optional[list] = None,
                   overwrite: bool = False, compressor: str = "zstd") -> dict:
    """Build a time-cube from daily-group store `src` into `out`. Returns meta dict.
    time_chunk=None -> all days in one time chunk. shard_spatial=None -> no sharding.
    region=(i0,i1,j0,j1) -> spatial subset (lat slice i0:i1, lon slice j0:j1); None = whole
    grid. Subsetting is required to build a REGIONAL cube from the global production store
    (reading whole global fields is infeasible); global/tiled conversion is P2-S6 ingest work.
    days -> explicit ordered day list (subset); None = all existing days under src."""
    days = list(days) if days is not None else list_existing_days(src)
    if not days:
        raise SystemExit(f"no days under {src}")
    g0 = zarr.open_group(group_path(src, days[0]), mode="r")
    lon_full = np.asarray(g0["lon"][:])
    lat_full = np.asarray(g0["lat"][:])
    if region:
        i0, i1, j0, j1 = region
    else:
        i0, i1, j0, j1 = 0, lat_full.size, 0, lon_full.size
    lon = lon_full[j0:j1]
    lat = lat_full[i0:i1]
    ny, nx = lat.size, lon.size
    T = len(days)
    tc = T if time_chunk is None else min(time_chunk, T)
    cy = min(spatial_chunk, ny)
    cx = min(spatial_chunk, nx)

    # UNION of vars across ALL days (not just day 0) + per-(day,var) validity mask.
    # Presence is a cheap filesystem check (var dir under the day group). A var absent on
    # a day -> NaN in the cube AND valid=False, so the access layer can OMIT it for that day
    # (P1 parity), distinct from a present-but-NaN land cell (valid=True -> null).
    valid: Dict[str, list] = {}
    for v in VARS:
        flags = [os.path.isdir(os.path.join(group_path(src, day), v)) for day in days]
        if any(flags):
            valid[v] = flags
    union_vars = list(valid.keys())

    if os.path.exists(out):
        if not overwrite:
            raise FileExistsError(
                f"--out already exists: {out}. Refusing to delete (path-safety). Build to a fresh "
                f"staging path and rename, or pass overwrite=True / --overwrite if you are certain.")
        import shutil
        shutil.rmtree(out)
    g = zarr.open_group(out, mode="w", zarr_format=3)
    g.create_array("lon", shape=(nx,), dtype="float32", chunks=(nx,))
    g.create_array("lat", shape=(ny,), dtype="float32", chunks=(ny,))
    g["lon"][:] = lon.astype(np.float32)
    g["lat"][:] = lat.astype(np.float32)

    shards = None
    if shard_spatial:
        sy = min(max(shard_spatial, cy), ny)
        sx = min(max(shard_spatial, cx), nx)
        sy = (sy // cy) * cy or cy
        sx = (sx // cx) * cx or cx
        shards = (tc, sy, sx)

    arrs = {}
    for v in union_vars:
        kw = dict(shape=(T, ny, nx), dtype="float32", chunks=(tc, cy, cx),
                  fill_value=float("nan"))      # absent days stay NaN
        if shards:
            kw["shards"] = shards
        arrs[v] = g.create_array(v, **kw)

    # fill time-major; skip (leave NaN fill for) days where the var is absent
    for i, day in enumerate(days):
        gd = zarr.open_group(group_path(src, day), mode="r")
        for v in union_vars:
            if valid[v][i]:
                arrs[v][i, :, :] = np.asarray(gd[v][0, i0:i1, j0:j1])

    g.attrs["days"] = days
    g.attrs["vars"] = union_vars
    g.attrs["var_valid"] = {v: [bool(x) for x in flags] for v, flags in valid.items()}
    g.attrs["layout"] = "time_lat_lon"
    g.attrs["region"] = [int(i0), int(i1), int(j0), int(j1)]

    nfiles = _count_files(out)
    meta = {"out": out, "src": src, "days": T, "grid": [ny, nx],
            "chunk": [tc, cy, cx], "shards": list(shards) if shards else None,
            "vars": union_vars, "file_count": nfiles,
            "bytes_on_disk": sum(os.path.getsize(os.path.join(r, f))
                                 for r, _, fs in os.walk(out) for f in fs)}
    with open(os.path.join(out, "timecube_meta.json"), "w") as fh:
        json.dump(meta, fh, indent=2)
    return meta


def append_day(cube_path: str, day_iso: str, data: dict) -> None:
    """Append ONE day to an existing cube: resize EACH cube var's time dim +1; write the
    slab where the var is present (else leave NaN fill) and update the validity mask, so a
    day missing a var is omitted by the reader (P1 parity). Cost depends on time_chunk (the
    slab lands in the current time-chunk -> only that chunk's spatial chunks are (re)written)."""
    g = zarr.open_group(cube_path, mode="a")
    days = list(g.attrs["days"])
    if day_iso in days:                       # idempotency: never duplicate a day
        raise ValueError(
            f"day {day_iso} already in cube (would duplicate). For re-sync use "
            f"dual_write.upsert_day(); for recovery use dual_write.sync_missing().")
    t = len(days)
    var_valid = {k: list(v) for k, v in dict(g.attrs.get("var_valid", {})).items()}
    for v in g.attrs.get("vars", VARS):
        arr = g[v]
        arr.resize((t + 1, arr.shape[1], arr.shape[2]))
        present = v in data and data[v] is not None
        if present:
            arr[t, :, :] = np.asarray(data[v], dtype=np.float32)
        # else: resized region stays NaN fill
        var_valid.setdefault(v, [True] * t).append(bool(present))
    g.attrs["days"] = days + [day_iso]
    g.attrs["var_valid"] = var_valid


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="daily-group fixture root")
    ap.add_argument("--out", required=True)
    ap.add_argument("--spatial-chunk", type=int, default=8, dest="spatial_chunk")
    ap.add_argument("--time-chunk", type=int, default=None, dest="time_chunk")
    ap.add_argument("--shard-spatial", type=int, default=None, dest="shard_spatial")
    ap.add_argument("--region", default=None, help="i0,i1,j0,j1 lat/lon index slice (tiled build)")
    ap.add_argument("--end-day", default=None, dest="end_day",
                    help="build through this day inclusive (holdout: leaves later days for a TRUE append test)")
    ap.add_argument("--exclude-latest", action="store_true", dest="exclude_latest",
                    help="drop the latest existing day (holdout for a true append measurement)")
    ap.add_argument("--overwrite", action="store_true",
                    help="allow deleting an existing --out (default: refuse, path-safety)")
    args = ap.parse_args()
    region = tuple(int(x) for x in args.region.split(",")) if args.region else None
    days = list_existing_days(args.src)
    if args.end_day:
        days = [d for d in days if d <= args.end_day]
    if args.exclude_latest and days:
        days = days[:-1]
    meta = build_timecube(args.src, args.out, args.spatial_chunk, args.time_chunk,
                          args.shard_spatial, region=region, days=days, overwrite=args.overwrite)
    print(f"  built {len(days)} days (holdout: end_day={args.end_day} exclude_latest={args.exclude_latest})")
    print(f"time-cube -> {args.out}")
    print(f"  days={meta['days']} chunk={meta['chunk']} shards={meta['shards']} "
          f"files={meta['file_count']} disk={meta['bytes_on_disk']/1e6:.1f}MB")


if __name__ == "__main__":
    main()
