"""dev2026 — P2-S7: shard-size sweep for the selected spatial=8 / time_chunk=90 candidate.

Shard size controls file count, but it is NOT a free lever (review): a larger shard packs more
inner chunks per shard FILE, so a daily append's read-modify-write rewrites a bigger shard, and
shard size can affect read/metadata/cache behaviour. So MEASURE read p95/p99, append, file count,
and RSS across shard ∈ {64,128,256} with spatial=8, time_chunk=90 fixed.

NON-BINDING (synthetic, warm, regional). The binding RSS/latency come from the P2-S7 HTTP gate
(/healthz sampling) and ultimately VM24.

Run:
  dev2026/.venv/bin/python dev2026/bench/bench_shard_sweep.py --grid 256 --days 365 --shards 64,128,256 --out shardsweep.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from datetime import date, timedelta

import numpy as np
import zarr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ingest.build_timecube import append_day, build_timecube  # noqa: E402
from store.time_cube import TimeCubeStore  # noqa: E402
from store.zarr_paths import group_path  # noqa: E402

VARS = ("sst", "sst_anomaly", "sea_ice")
GLOBAL_CELLS = 17999 * 36000
try:
    import psutil
    _PROC = psutil.Process()
except Exception:
    _PROC = None


def _rss():
    return round(_PROC.memory_info().rss / 1e6, 1) if _PROC else 0.0


def _build_daily(root, ndays, G):
    base = (10 + 0.02 * np.arange(G)[:, None] + 0.001 * np.arange(G)[None, :]).astype(np.float32)
    for i in range(ndays):
        day = (date(2024, 1, 1) + timedelta(days=i)).isoformat()
        g = zarr.open_group(group_path(root, day), mode="w", zarr_format=3)
        g.create_array("lon", shape=(G,), dtype="float32", chunks=(G,)); g["lon"][:] = np.linspace(100, 130, G).astype(np.float32)
        g.create_array("lat", shape=(G,), dtype="float32", chunks=(G,)); g["lat"][:] = np.linspace(0, 30, G).astype(np.float32)
        for v in VARS:
            g.create_array(v, shape=(1, G, G), dtype="float32", chunks=(1, min(1024, G), min(1024, G)))
            g[v][0] = base + 0.01 * i


def _pcts(xs):
    a = np.asarray(xs) * 1000
    return {"p50": round(float(np.percentile(a, 50)), 1), "p95": round(float(np.percentile(a, 95)), 1),
            "p99": round(float(np.percentile(a, 99)), 1)}


def _read(store, days, conc, iters=30):
    """Concurrency read with a PROPER background RSS sampler (peak across the whole run)."""
    lats, lock = [], threading.Lock()
    peak = {"rss": _rss()}
    stop = threading.Event()
    rng = np.random.default_rng(1)
    pts = [(float(rng.uniform(101, 129)), float(rng.uniform(1, 29))) for _ in range(conc * iters)]

    def sampler():
        while not stop.is_set():
            peak["rss"] = max(peak["rss"], _rss())
            time.sleep(0.02)

    def worker(my):
        loc = []
        for lon, lat in my:
            t0 = time.perf_counter()
            store.point_series(lon, lat, days, ["sst", "sea_ice"])
            loc.append(time.perf_counter() - t0)
        with lock:
            lats.extend(loc)

    s = threading.Thread(target=sampler, daemon=True); s.start()
    ts = [threading.Thread(target=worker, args=(pts[i::conc],)) for i in range(conc)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    stop.set(); s.join()
    return _pcts(lats), peak["rss"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", type=int, default=256)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--spatial", type=int, default=8)
    ap.add_argument("--time-chunk", type=int, default=90, dest="time_chunk")
    ap.add_argument("--shards", default="64,128,256")
    ap.add_argument("--appends", type=int, default=5)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    G = args.grid
    area_ratio = GLOBAL_CELLS / (G * G)
    tmp = tempfile.mkdtemp(prefix="shardsweep_")
    results = []
    try:
        daily = os.path.join(tmp, "daily")
        _build_daily(daily, args.days, G)
        days = [(date(2024, 1, 1) + timedelta(days=i)).isoformat() for i in range(args.days)]
        slab = {v: np.zeros((G, G), np.float32) for v in VARS}
        for shard in [int(x) for x in args.shards.split(",")]:
            sh = min(shard, G)
            cube = os.path.join(tmp, f"cube_sh{sh}")
            meta = build_timecube(daily, cube, spatial_chunk=args.spatial,
                                  time_chunk=args.time_chunk, shard_spatial=sh)
            store = TimeCubeStore(cube)
            store.point_series(115.0, 12.0, days[:5], ["sst"])     # warm
            c1, rss1 = _read(store, days, 1)
            c8, rss8 = _read(store, days, 8)
            appends = []
            for k in range(args.appends):
                d = (date(2024, 1, 1) + timedelta(days=args.days + k)).isoformat()
                a = time.perf_counter()
                append_day(cube, d, slab)
                appends.append((time.perf_counter() - a) * 1000)
            r = {"shard_spatial": sh, "shards": meta["shards"],
                 "file_count_regional": meta["file_count"],
                 "files_global_est": int(meta["file_count"] * area_ratio),
                 "read_c1": c1, "read_c8": c8, "rss_peak_mb": max(rss1, rss8),
                 "append_ms": {"p50": round(float(np.percentile(appends, 50)), 1),
                               "p95": round(float(np.percentile(appends, 95)), 1)},
                 "append_global_min_est": round(float(np.percentile(appends, 50)) / 1000 * area_ratio / 60, 1)}
            results.append(r)
            print(f"  shard={sh:<4} files_reg={meta['file_count']:>5} files_global~{r['files_global_est']:,} "
                  f"read p95 C1={c1['p95']}ms C8={c8['p95']}ms p99(C8)={c8['p99']}ms rss={r['rss_peak_mb']:.0f}MB "
                  f"append p50={r['append_ms']['p50']}ms ->global~{r['append_global_min_est']}min")
        out = {"meta": {"grid": [G, G], "days": args.days, "spatial": args.spatial,
                        "time_chunk": args.time_chunk, "area_ratio_to_global": round(area_ratio, 1),
                        "binding": False, "note": "shard sweep; synthetic/warm/regional. RSS here is "
                        "illustrative — binding RSS comes from the HTTP gate /healthz + VM24."},
               "results": results}
        if args.out:
            json.dump(out, open(args.out, "w"), indent=2)
            print(f"wrote {args.out}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
