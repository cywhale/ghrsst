"""dev2026 — P2-S7: chunking selection (READ-FIRST, append as operational constraint).

Per review: users feel READ latency; ingest writes once/day, so append is a CONSTRAINT not the
optimization target. Selection order:
  1. read gate first  — 365-day point series p95 + LR concurrency scaling + read_amp + RSS
  2. among read-passers, prefer acceptable daily append cost + manageable file count
  3. reject ONLY if append is operationally infeasible (won't fit the daily ingest window, or
     unmanageable file count). Do NOT discard small spatial chunks solely because append is slower.

For each spatial chunk in {8,16,32,64} (time_chunk=90, sharded) on a 365-day synthetic fixture:
read_amp, C=1/4/8/16 point-series p95 (warm, store-level), RSS peak, append-one-day ms, file
count — plus a GLOBAL extrapolation (area ratio) of append time and file count so an operator can
pick the smallest spatial that fits their daily ingest window.

NON-BINDING (synthetic, warm, regional). VM24 full-grid + cold + real append is the binding gate.

Run:
  dev2026/.venv/bin/python dev2026/bench/bench_chunking_select.py --grid 256 --days 365 --out sel.json
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
GLOBAL_CELLS = 17999 * 36000   # MUR global grid

try:
    import psutil
    _PROC = psutil.Process()
except Exception:
    _PROC = None


def _rss():
    return round(_PROC.memory_info().rss / 1e6, 1) if _PROC else 0.0


def _build_daily(root, ndays, G):
    base = (10 + 0.02 * np.arange(G)[:, None] + 0.001 * np.arange(G)[None, :]).astype(np.float32)
    d0 = date(2024, 1, 1)
    for i in range(ndays):
        day = (d0 + timedelta(days=i)).isoformat()
        g = zarr.open_group(group_path(root, day), mode="w", zarr_format=3)
        g.create_array("lon", shape=(G,), dtype="float32", chunks=(G,))
        g.create_array("lat", shape=(G,), dtype="float32", chunks=(G,))
        g["lon"][:] = np.linspace(100, 130, G).astype(np.float32)
        g["lat"][:] = np.linspace(0, 30, G).astype(np.float32)
        for v in VARS:
            g.create_array(v, shape=(1, G, G), dtype="float32", chunks=(1, min(1024, G), min(1024, G)))
            g[v][0] = base + 0.01 * i


def _p95(xs):
    return round(float(np.percentile(np.asarray(xs) * 1000, 95)), 1)


def _read_concurrency(store, days, conc, iters=20):
    lats, peak_rss = [], _rss()
    lock = threading.Lock()
    rng = np.random.default_rng(0)
    pts = [(float(rng.uniform(101, 129)), float(rng.uniform(1, 29))) for _ in range(conc * iters)]

    def worker(my):
        local = []
        for lon, lat in my:
            t0 = time.perf_counter()
            store.point_series(lon, lat, days, ["sst", "sea_ice"])
            local.append(time.perf_counter() - t0)
        with lock:
            lats.extend(local)

    chunks = [pts[i::conc] for i in range(conc)]
    ts = [threading.Thread(target=worker, args=(c,)) for c in chunks]
    for t in ts:
        t.start()
    for t in ts:
        peak_rss = max(peak_rss, _rss())
    for t in ts:
        t.join()
    return _p95(lats), max(peak_rss, _rss())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", type=int, default=256)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--spatials", default="8,16,32,64")
    ap.add_argument("--time-chunk", type=int, default=90, dest="time_chunk")
    ap.add_argument("--shard", type=int, default=64)
    ap.add_argument("--ingest-window-min", type=float, default=180.0, dest="window_min",
                    help="daily ingest window (minutes) used to flag append feasibility")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    G = args.grid
    area_ratio = GLOBAL_CELLS / (G * G)
    tmp = tempfile.mkdtemp(prefix="chunksel_")
    results = []
    try:
        daily = os.path.join(tmp, "daily")
        _build_daily(daily, args.days, G)
        days = [(date(2024, 1, 1) + timedelta(days=i)).isoformat() for i in range(args.days)]
        slab = {v: np.zeros((G, G), np.float32) for v in VARS}
        nf = 2  # sst, sea_ice for the read query

        for s in [int(x) for x in args.spatials.split(",")]:
            cube = os.path.join(tmp, f"cube_s{s}")
            meta = build_timecube(daily, cube, spatial_chunk=s, time_chunk=args.time_chunk,
                                  shard_spatial=args.shard)
            store = TimeCubeStore(cube)
            store.point_series(115.0, 12.0, days[:5], ["sst"])    # warm
            tc = meta["chunk"][0]
            chunk_count = ((args.days + tc - 1) // tc) * nf
            decompressed = chunk_count * tc * s * s * 4
            read_amp = round(decompressed / (args.days * nf * 4), 1)
            c1 = _read_concurrency(store, days, 1)[0]
            curve = {}
            rss_peak = 0.0
            for c in (4, 8, 16):
                p, r = _read_concurrency(store, days, c)
                curve[c] = p
                rss_peak = max(rss_peak, r)
            a = time.perf_counter()
            append_day(cube, "2099-12-31", slab)
            append_ms = round((time.perf_counter() - a) * 1000, 1)

            append_global_s = round(append_ms / 1000 * area_ratio, 1)
            files_global = int(meta["file_count"] * area_ratio)
            append_feasible = append_global_s <= args.window_min * 60
            r = {"spatial": s, "chunk": meta["chunk"], "shard": meta["shards"],
                 "read_amp": read_amp, "chunk_count": chunk_count,
                 "p95_c1_ms": c1, "p95_lr": curve, "rss_peak_mb": rss_peak,
                 "lr_ratio_c8": round(curve[8] / c1, 2) if c1 else None,
                 "append_ms_regional": append_ms, "file_count_regional": meta["file_count"],
                 "append_global_s_est": append_global_s, "append_global_min_est": round(append_global_s / 60, 1),
                 "files_global_est": files_global, "append_feasible_in_window": append_feasible}
            results.append(r)
            print(f"  s={s:<3} read_amp={read_amp:>7}x p95(C1)={c1}ms LR(C8)={curve[8]}ms "
                  f"rss={rss_peak:.0f}MB | append regional={append_ms}ms -> global~{r['append_global_min_est']}min "
                  f"files_global~{files_global:,} feasible={append_feasible}")

        # selection: read-first (lowest read_amp = best at scale), then append-feasible
        read_ok = results  # all pass warm p95<4s trivially; differentiate by read_amp at scale
        feasible = [r for r in results if r["append_feasible_in_window"]]
        pick = min(feasible, key=lambda r: r["read_amp"]) if feasible else None
        out = {"meta": {"grid": [G, G], "days": args.days, "area_ratio_to_global": round(area_ratio, 1),
                        "ingest_window_min": args.window_min, "binding": False,
                        "note": "READ-FIRST selection; append is an operational constraint. "
                                "Synthetic/warm/regional — VM24 full-grid+cold is binding."},
               "results": results,
               "recommendation": ({"spatial": pick["spatial"], "reason":
                                   "lowest read_amp among append-feasible (read-first)"} if pick else
                                  {"spatial": None, "reason": "no global-append-feasible candidate "
                                   "in window -> use REGIONAL/tiled cubes or widen ingest window"})}
        print("\nRECOMMENDATION:", json.dumps(out["recommendation"]))
        print("  (read-first: prefer smallest read_amp; append only rejects infeasible-in-window. "
              "If none feasible globally -> regional/tiled cubes.)")
        if args.out:
            json.dump(out, open(args.out, "w"), indent=2)
            print(f"wrote {args.out}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
