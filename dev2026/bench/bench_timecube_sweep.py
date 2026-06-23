"""dev2026 — P2-S3: time-cube chunking sweep (Candidate A) vs daily baseline.

Proves the STRUCTURAL reduction the design predicted (spec §3/§4):
  chunk_count   O(days)        -> O(days/time_chunk)
  chunk bytes   1024^2 x 4     -> time_chunk x spatial x spatial x 4
For each (spatial_chunk, time_chunk, shard) it builds a cube from a daily source fixture and
records: chunk_count_per_query, decompressed_bytes, read_amplification, file_count (sharding
effect), build time, **append-one-day cost**, and measured point-series latency. Compared
against the daily-store baseline.

Run (build a daily fixture first):
  dev2026/.venv/bin/python dev2026/ingest/build_fixture.py --out /tmp/dailyfix --days 365 --ny 64 --nx 64 --chunk 64
  dev2026/.venv/bin/python dev2026/bench/bench_timecube_sweep.py --src /tmp/dailyfix \
    --out-dir /tmp/cubes --spatial 4,8,16,32 --out sweep.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import zarr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ingest.build_timecube import append_day, build_timecube  # noqa: E402
from store.store_access import StoreAccess  # noqa: E402
from store.time_cube import TimeCubeStore  # noqa: E402
from store.zarr_paths import group_path, list_existing_days  # noqa: E402
from bench_timecube_microcost import chunk_geometry  # noqa: E402

NF = 3  # sst, sst_anomaly, sea_ice


def _pcts(xs):
    a = np.asarray(xs) * 1000
    return {"p50": round(float(np.percentile(a, 50)), 1), "p95": round(float(np.percentile(a, 95)), 1)}


def daily_baseline(src, lon, lat, days):
    sa = StoreAccess(src)
    ii, jj, _, _ = sa.nearest_indices(lon, lat)
    ny, nx, cy, cx = chunk_geometry(src, days[0], "sst")
    chunk_bytes = min(cy, ny) * min(cx, nx) * 4
    T = len(days)
    chunk_count = T * NF
    decompressed = chunk_count * chunk_bytes
    lats = []
    for _ in range(5):
        t0 = time.perf_counter()
        sa.point_series(lon, lat, days, ["sst", "sst_anomaly", "sea_ice"])
        lats.append(time.perf_counter() - t0)
    return {"layout": "daily", "chunk": [1, cy, cx], "chunk_count_per_query": chunk_count,
            "decompressed_bytes": int(decompressed),
            "read_amplification": round(decompressed / (T * NF * 4), 1),
            "complexity": "O(days)", "latency_ms": _pcts(lats)}


def measure_cube(src, out, lon, lat, days, spatial, time_chunk, shard):
    t0 = time.perf_counter()
    meta = build_timecube(src, out, spatial_chunk=spatial, time_chunk=time_chunk, shard_spatial=shard)
    build_s = round(time.perf_counter() - t0, 2)

    tc = meta["chunk"][0]
    T = len(days)
    chunks_in_time = (T + tc - 1) // tc                 # ceil(T/time_chunk)
    chunk_count = chunks_in_time * NF
    inner_chunk_bytes = tc * spatial * spatial * 4      # one (time_chunk x s x s) chunk
    decompressed = chunk_count * inner_chunk_bytes
    read_amp = round(decompressed / (T * NF * 4), 1)

    store = TimeCubeStore(out)
    store.point_series(lon, lat, days[:5], ["sst"])     # warm
    lats = []
    for _ in range(5):
        a = time.perf_counter()
        store.point_series(lon, lat, days, ["sst", "sst_anomaly", "sea_ice"])
        lats.append(time.perf_counter() - a)

    # append-one-day cost (synthetic slab)
    ny, nx = meta["grid"]
    slab = {v: np.zeros((ny, nx), np.float32) for v in meta["vars"]}
    a = time.perf_counter()
    append_day(out, "2099-12-31", slab)
    append_s = round((time.perf_counter() - a) * 1000, 1)

    return {"layout": "time_cube", "spatial_chunk": spatial, "time_chunk": tc,
            "shard": meta["shards"], "chunk": meta["chunk"],
            "chunk_count_per_query": chunk_count, "decompressed_bytes": int(decompressed),
            "read_amplification": read_amp, "complexity": f"O(days/{tc})",
            "file_count": meta["file_count"], "disk_mb": round(meta["bytes_on_disk"] / 1e6, 1),
            "build_s": build_s, "append_one_day_ms": append_s, "latency_ms": _pcts(lats)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, help="daily-group source fixture")
    ap.add_argument("--out-dir", required=True, dest="out_dir")
    ap.add_argument("--lon", type=float, default=115.0)
    ap.add_argument("--lat", type=float, default=12.0)
    ap.add_argument("--spatial", default="4,8,16,32")
    ap.add_argument("--time-chunks", default="all,90", dest="time_chunks")
    ap.add_argument("--shard", type=int, default=64, help="spatial shard size (0=none)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    days = list_existing_days(args.src)
    os.makedirs(args.out_dir, exist_ok=True)
    base = daily_baseline(args.src, args.lon, args.lat, days)
    print(f"daily baseline: chunk_count={base['chunk_count_per_query']} "
          f"decompressed={base['decompressed_bytes']/1e6:.2f}MB amp={base['read_amplification']}x "
          f"p50={base['latency_ms']['p50']}ms p95={base['latency_ms']['p95']}ms")

    variants = []
    spatials = [int(s) for s in args.spatial.split(",")]
    tcs = [None if t == "all" else int(t) for t in args.time_chunks.split(",")]
    for tc in tcs:
        for s in spatials:
            for shard in ([None, args.shard] if args.shard else [None]):
                name = f"s{s}_t{'all' if tc is None else tc}_{'shard' if shard else 'noshard'}"
                out = os.path.join(args.out_dir, name)
                r = measure_cube(args.src, out, args.lon, args.lat, days, s, tc, shard)
                r["name"] = name
                variants.append(r)
                print(f"  {name:>22}: chunk_count={r['chunk_count_per_query']:>4} "
                      f"decompressed={r['decompressed_bytes']/1e6:6.3f}MB amp={r['read_amplification']:>8}x "
                      f"files={r['file_count']:>5} append={r['append_one_day_ms']:>7}ms "
                      f"p95={r['latency_ms']['p95']}ms")

    out = {"meta": {"src": args.src, "days": len(days), "point": [args.lon, args.lat],
                    "promotion_gate_eligible": False,
                    "note": "P2-S3 structural sweep on a fixture; not a promotion gate."},
           "daily_baseline": base, "variants": variants}
    if args.out:
        json.dump(out, open(args.out, "w"), indent=2)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
