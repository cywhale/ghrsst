"""dev2026 — P2-S6: append-at-scale benchmark for the time-cube.

P2-S3's ~30 ms append was a 64×64 synthetic grid — not extrapolatable. This measures the
cost of appending ONE day (resize + write one (ny,nx) slab) at larger regional grids with
`time_chunk=90` + sharding, the recommended config. Reports append p50/p95 per grid, with/
without sharding, plus cube file_count and build time.

NON-BINDING (synthetic, regional). Global/full-grid append on VM24 is the binding measurement.

Run:
  dev2026/.venv/bin/python dev2026/bench/bench_append_scale.py --grids 128,256,512 --days 100 --out append.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import date, timedelta

import numpy as np
import zarr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ingest.build_timecube import append_day, build_timecube  # noqa: E402
from store.zarr_paths import group_path  # noqa: E402

VARS = ("sst", "sst_anomaly", "sea_ice")


def _build_daily(root, ndays, ny, nx):
    lon = np.linspace(100, 130, nx).astype(np.float32)
    lat = np.linspace(0, 30, ny).astype(np.float32)
    base = (10 + 0.02 * np.arange(ny)[:, None] + 0.001 * np.arange(nx)[None, :]).astype(np.float32)
    d0 = date(2024, 1, 1)
    days = []
    for i in range(ndays):
        day = (d0 + timedelta(days=i)).isoformat()
        days.append(day)
        g = zarr.open_group(group_path(root, day), mode="w", zarr_format=3)
        g.create_array("lon", shape=(nx,), dtype="float32", chunks=(nx,))
        g.create_array("lat", shape=(ny,), dtype="float32", chunks=(ny,))
        g["lon"][:] = lon
        g["lat"][:] = lat
        for v in VARS:
            g.create_array(v, shape=(1, ny, nx), dtype="float32", chunks=(1, min(1024, ny), min(1024, nx)))
            g[v][0] = base + 0.01 * i + np.float32(0.0 if v == "sst" else 0.1)
    return days


def _pcts(xs):
    a = np.asarray(xs) * 1000
    return {"p50": round(float(np.percentile(a, 50)), 1), "p95": round(float(np.percentile(a, 95)), 1),
            "min": round(float(a.min()), 1), "max": round(float(a.max()), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grids", default="128,256,512")
    ap.add_argument("--days", type=int, default=100)
    ap.add_argument("--spatial-chunk", type=int, default=8, dest="spatial_chunk")
    ap.add_argument("--time-chunk", type=int, default=90, dest="time_chunk")
    ap.add_argument("--shard", type=int, default=64)
    ap.add_argument("--appends", type=int, default=6)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    grids = [int(g) for g in args.grids.split(",")]
    results = []
    tmp = tempfile.mkdtemp(prefix="appendscale_")
    try:
        for G in grids:
            daily = os.path.join(tmp, f"daily_{G}")
            days = _build_daily(daily, args.days, G, G)
            slab = {v: np.zeros((G, G), np.float32) for v in VARS}
            for shard in ([None, args.shard] if args.shard else [None]):
                cube = os.path.join(tmp, f"cube_{G}_{'shard' if shard else 'no'}")
                t0 = time.perf_counter()
                meta = build_timecube(daily, cube, spatial_chunk=args.spatial_chunk,
                                      time_chunk=args.time_chunk, shard_spatial=shard)
                build_s = round(time.perf_counter() - t0, 2)
                d0 = date(2024, 1, 1) + timedelta(days=args.days)
                appends = []
                for k in range(args.appends):
                    day = (d0 + timedelta(days=k)).isoformat()
                    a = time.perf_counter()
                    append_day(cube, day, slab)
                    appends.append(time.perf_counter() - a)
                r = {"grid": [G, G], "shard": meta["shards"], "chunk": meta["chunk"],
                     "days_built": args.days, "build_s": build_s,
                     "file_count_after_build": meta["file_count"],
                     "append_ms": _pcts(appends)}
                results.append(r)
                print(f"  grid={G}x{G:<5} shard={'yes' if shard else 'no':<3} chunk={meta['chunk']} "
                      f"files={meta['file_count']:>6} build={build_s:>5}s "
                      f"append p50={r['append_ms']['p50']}ms p95={r['append_ms']['p95']}ms")
        out = {"meta": {"time_chunk": args.time_chunk, "spatial_chunk": args.spatial_chunk,
                        "appends_per_cube": args.appends, "binding": False,
                        "note": "synthetic/regional append-at-scale; VM24 full-grid is binding."},
               "results": results}
        if args.out:
            json.dump(out, open(args.out, "w"), indent=2)
            print(f"wrote {args.out}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
