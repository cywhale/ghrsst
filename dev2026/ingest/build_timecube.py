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
                   compressor: str = "zstd") -> dict:
    """Build a time-cube from daily-group store `src` into `out`. Returns meta dict.
    time_chunk=None -> all days in one time chunk. shard_spatial=None -> no sharding."""
    days = list_existing_days(src)
    if not days:
        raise SystemExit(f"no days under {src}")
    g0 = zarr.open_group(group_path(src, days[0]), mode="r")
    lon = np.asarray(g0["lon"][:])
    lat = np.asarray(g0["lat"][:])
    ny, nx = lat.size, lon.size
    T = len(days)
    tc = T if time_chunk is None else min(time_chunk, T)
    cy = min(spatial_chunk, ny)
    cx = min(spatial_chunk, nx)
    present_vars = [v for v in VARS if v in g0]

    if os.path.exists(out):
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
        # shard dims must be multiples of chunk dims; clamp to grid
        sy = (sy // cy) * cy or cy
        sx = (sx // cx) * cx or cx
        shards = (tc, sy, sx)

    arrs = {}
    for v in present_vars:
        kw = dict(shape=(T, ny, nx), dtype="float32", chunks=(tc, cy, cx))
        if shards:
            kw["shards"] = shards
        arrs[v] = g.create_array(v, **kw)

    # fill time-major: read each day's spatial field, place at time index i
    for i, day in enumerate(days):
        gd = zarr.open_group(group_path(src, day), mode="r")
        for v in present_vars:
            arrs[v][i, :, :] = np.asarray(gd[v][0, :, :])

    g.attrs["days"] = days
    g.attrs["vars"] = present_vars
    g.attrs["layout"] = "time_lat_lon"

    nfiles = _count_files(out)
    meta = {"out": out, "src": src, "days": T, "grid": [ny, nx],
            "chunk": [tc, cy, cx], "shards": list(shards) if shards else None,
            "vars": present_vars, "file_count": nfiles,
            "bytes_on_disk": sum(os.path.getsize(os.path.join(r, f))
                                 for r, _, fs in os.walk(out) for f in fs)}
    with open(os.path.join(out, "timecube_meta.json"), "w") as fh:
        json.dump(meta, fh, indent=2)
    return meta


def append_day(cube_path: str, day_iso: str, data: dict) -> None:
    """Append ONE day to an existing cube: resize each var's time dim +1 and write the
    new (ny,nx) slab; update attrs['days']. Cost depends on time_chunk (the slab lands in
    the current time-chunk -> only that chunk's spatial chunks are (re)written)."""
    g = zarr.open_group(cube_path, mode="a")
    days = list(g.attrs["days"])
    t = len(days)
    for v in g.attrs.get("vars", VARS):
        if v not in data:
            continue
        arr = g[v]
        arr.resize((t + 1, arr.shape[1], arr.shape[2]))
        arr[t, :, :] = np.asarray(data[v], dtype=np.float32)
    g.attrs["days"] = days + [day_iso]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="daily-group fixture root")
    ap.add_argument("--out", required=True)
    ap.add_argument("--spatial-chunk", type=int, default=8, dest="spatial_chunk")
    ap.add_argument("--time-chunk", type=int, default=None, dest="time_chunk")
    ap.add_argument("--shard-spatial", type=int, default=None, dest="shard_spatial")
    args = ap.parse_args()
    meta = build_timecube(args.src, args.out, args.spatial_chunk, args.time_chunk, args.shard_spatial)
    print(f"time-cube -> {args.out}")
    print(f"  days={meta['days']} chunk={meta['chunk']} shards={meta['shards']} "
          f"files={meta['file_count']} disk={meta['bytes_on_disk']/1e6:.1f}MB")


if __name__ == "__main__":
    main()
