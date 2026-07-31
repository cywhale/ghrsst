"""dev2026 — P5-S0: the BASELINE OF RECORD for the current TieredCube read path.

Gate **G3** ("point p95 within +25 % of the current TieredCube baseline") is meaningless
without a recorded, reproducible baseline measured on the same fixture the segmented store
will later be measured on. This harness produces it.

What it measures, on production chunk geometry (base `s8/t90/shard128`, delta `t1/s256`):

  * single historical day point (route: cube)
  * 366-day point range within the base
  * a base -> delta CROSSING range (the path VM24 measured at 219 ms vs 76-100 ms pure-base)
  * cache-independent `chunk_count` / `decompressed_bytes` / `read_amplification` for each,
    derived from geometry READ BACK FROM DISK
  * peak RSS and, for the crossing range, the delta-span sensitivity that H5 predicts

[RO] with respect to production: builds its own synthetic fixture in a temp dir. It never
opens `GHRSST_ZARR_PATH` / `GHRSST_TIMECUBE_PATH` / `GHRSST_DELTACUBE_PATH`, never reads a
VM24 path, and writes nothing outside `--workdir` and `--out`.

Run:
  dev2026/.venv/bin/python dev2026/bench/bench_p5_baseline.py \
      --out dev2026/bench/results/p5s0_baseline.json
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from datetime import date, timedelta

import numpy as np
import zarr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from p5_cost import point_column_cost, read_geometry, timeit_ms, tree_size  # noqa: E402
from store.tiered_cube import TieredCube  # noqa: E402
from store.time_cube import TimeCubeStore  # noqa: E402

try:
    import psutil
    _PROC = psutil.Process()
except Exception:                                     # pragma: no cover - psutil optional
    _PROC = None

VARS = ("sst", "sst_anomaly", "sea_ice")
BASE_SPATIAL, BASE_TIME, BASE_SHARD = 8, 90, 128
DELTA_SPATIAL = 256


def _rss_mb():
    return round(_PROC.memory_info().rss / 1e6, 1) if _PROC else None


def _days(start: str, n: int):
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def _synth(T, ny, nx, seed):
    """Smooth field + small noise: sst-like compressibility, not pure random noise
    (pure noise would understate compression and distort bytes-on-disk)."""
    rng = np.random.default_rng(seed)
    lat_g = np.linspace(0, 1, ny)[None, :, None]
    lon_g = np.linspace(0, 1, nx)[None, None, :]
    t_g = np.arange(T, dtype=np.float32)[:, None, None]
    base = 10.0 + 18.0 * lat_g + 2.0 * np.sin(6.28 * lon_g) + 0.01 * t_g
    return (base + rng.normal(0, 0.05, (T, ny, nx))).astype(np.float32)


def _build_base(path, days, ny, nx):
    g = zarr.open_group(path, mode="w", zarr_format=3)
    g.create_array("lon", shape=(nx,), dtype="float32", chunks=(nx,))
    g["lon"][:] = np.linspace(100.0, 160.0, nx, dtype=np.float32)
    g.create_array("lat", shape=(ny,), dtype="float32", chunks=(ny,))
    g["lat"][:] = np.linspace(0.0, 40.0, ny, dtype=np.float32)
    T = len(days)
    tc = min(BASE_TIME, T)
    for i, v in enumerate(VARS):
        g.create_array(v, shape=(T, ny, nx), dtype="float32",
                       chunks=(tc, BASE_SPATIAL, BASE_SPATIAL),
                       shards=(tc, BASE_SHARD, BASE_SHARD), fill_value=float("nan"))
        for t0 in range(0, T, tc):
            t1 = min(t0 + tc, T)
            g[v][t0:t1] = _synth(t1 - t0, ny, nx, seed=1000 + i * 17 + t0)
    g.attrs["days"] = list(days)
    g.attrs["vars"] = list(VARS)
    g.attrs["var_valid"] = {v: [True] * T for v in VARS}
    g.attrs["layout"] = "time_lat_lon"
    return path


def _build_delta(path, days, ny, nx):
    g = zarr.open_group(path, mode="w", zarr_format=3)
    g.create_array("lon", shape=(nx,), dtype="float32", chunks=(nx,))
    g["lon"][:] = np.linspace(100.0, 160.0, nx, dtype=np.float32)
    g.create_array("lat", shape=(ny,), dtype="float32", chunks=(ny,))
    g["lat"][:] = np.linspace(0.0, 40.0, ny, dtype=np.float32)
    T = len(days)
    cy, cx = min(DELTA_SPATIAL, ny), min(DELTA_SPATIAL, nx)
    for i, v in enumerate(VARS):
        g.create_array(v, shape=(T, ny, nx), dtype="float32", chunks=(1, cy, cx),
                       shards=(1, cy, cx), fill_value=float("nan"))
        for t in range(T):
            g[v][t] = _synth(1, ny, nx, seed=5000 + i * 31 + t)[0]
    g.attrs["days"] = list(days)
    g.attrs["vars"] = list(VARS)
    g.attrs["var_valid"] = {v: [True] * T for v in VARS}
    g.attrs["layout"] = "time_lat_lon"
    return path


def _analytic(path, t0, t1, ii, jj):
    """Sum the cache-independent cost across variables for the EXACT index window read.

    `t0`/`t1` are mandatory and must be the indices the request actually uses. Hardcoding
    `t0=0` would silently measure the HEAD of the array while the request reads its TAIL --
    and on a base whose length is not a multiple of the time chunk, the head and the tail
    have different chunk alignment and a different clipped final chunk, so the reported
    chunk_count/decompressed_bytes would be wrong in a way nothing else would catch."""
    total_chunks = 0
    total_bytes = 0
    for v in VARS:
        geom = read_geometry(path, v)
        c = point_column_cost(geom, t0=t0, t1=t1, ii=ii, jj=jj)
        total_chunks += c["chunk_count"]
        total_bytes += c["decompressed_bytes"]
    useful = (t1 - t0) * len(VARS) * 4
    return {"index_window": [int(t0), int(t1)],
            "days": int(t1 - t0),
            "chunk_count": total_chunks, "decompressed_bytes": total_bytes,
            "useful_bytes": useful,
            "read_amplification": round(total_bytes / useful, 1) if useful else None}


def run(workdir: str, *, ny: int, nx: int, base_days: int, delta_days: int,
        repeats: int, delta_spans) -> dict:
    if base_days < 366:
        raise SystemExit(f"--base-days must be >= 366 (got {base_days}); the 366-day\n"
                         "case must contain exactly 366 days, not min(366, base_days)")
    if os.path.exists(workdir):
        shutil.rmtree(workdir)
    os.makedirs(workdir, exist_ok=True)

    all_base = _days("2024-01-01", base_days)
    first_delta = (date.fromisoformat(all_base[-1]) + timedelta(days=1)).isoformat()
    all_delta = _days(first_delta, delta_days)

    base_path = _build_base(os.path.join(workdir, "base.zarr"), all_base, ny, nx)
    delta_path = _build_delta(os.path.join(workdir, "delta.zarr"), all_delta, ny, nx)

    base_geom = {v: read_geometry(base_path, v) for v in VARS}
    delta_geom = {v: read_geometry(delta_path, v) for v in VARS}
    assert tuple(base_geom["sst"]["chunks"]) == (min(BASE_TIME, base_days),
                                                 BASE_SPATIAL, BASE_SPATIAL), base_geom
    assert tuple(delta_geom["sst"]["chunks"])[0] == 1, delta_geom

    cube = TieredCube(TimeCubeStore(base_path), TimeCubeStore(delta_path))
    lon, lat = 130.0, 20.0
    ii, jj, _, _ = cube.base.nearest_indices(lon, lat)

    rss0 = _rss_mb()
    cases = {}

    # 1) single historical day -- index taken from the SAME lookup the request uses
    t_single = len(all_base) // 2
    d1 = [all_base[t_single]]
    cube.point_series(lon, lat, d1, VARS)             # warm
    cases["single_day_point"] = {
        "days": 1, "tier": "base",
        "latency": timeit_ms(lambda: cube.point_series(lon, lat, d1, VARS), repeats),
        "analytic": _analytic(base_path, t_single, t_single + 1, ii, jj)}

    # 2) EXACTLY 366 days, entirely inside base -- the TAIL of base, so the analytic window
    #    must be the tail too (see `_analytic`).
    assert base_days >= 366, f"base_days={base_days} cannot hold a 366-day range"
    d366 = all_base[-366:]
    assert len(d366) == 366, f"expected exactly 366 days, got {len(d366)}"
    cube.point_series(lon, lat, d366, VARS)
    rows = cube.point_series(lon, lat, d366, VARS)
    assert len(rows) == 366, f"366-day case returned {len(rows)} rows"
    cases["range_366d_base_only"] = {
        "days": 366, "tier": "base", "rows_returned": len(rows),
        "latency": timeit_ms(lambda: cube.point_series(lon, lat, d366, VARS), repeats),
        "analytic": _analytic(base_path, base_days - 366, base_days, ii, jj)}

    # 3) base -> delta crossing range, swept over delta span (H5). Always 366 days total.
    crossing = []
    for span in delta_spans:
        span = min(span, delta_days)
        n_base = 366 - span
        dcross = all_base[-n_base:] + all_delta[:span]
        assert len(dcross) == 366, f"crossing case has {len(dcross)} days, expected 366"
        cube.point_series(lon, lat, dcross, VARS)
        r = cube.point_series(lon, lat, dcross, VARS)
        assert len(r) == 366, f"crossing case returned {len(r)} rows"
        crossing.append({
            "delta_span_days": span, "total_days": len(dcross), "rows_returned": len(r),
            "latency": timeit_ms(lambda: cube.point_series(lon, lat, dcross, VARS), repeats),
            "analytic_base": _analytic(base_path, base_days - n_base, base_days, ii, jj),
            "analytic_delta": _analytic(delta_path, 0, span, ii, jj)})
    cases["range_366d_crossing"] = {"tier": "base+delta", "sweep": crossing}

    bfiles, bbytes = tree_size(base_path)
    dfiles, dbytes = tree_size(delta_path)
    return {
        "harness": "bench_p5_baseline.py",
        "step": "P5-S0",
        "role": "BASELINE OF RECORD for gate G3 (point p95 within +25 % of current TieredCube)",
        "provenance": ("COMMITTED harness, synthetic fixture on production chunk geometry, "
                       "local, warm. NOT a VM24 measurement and not binding performance."),
        "env": {"zarr": zarr.__version__, "python": sys.version.split()[0],
                "platform": platform.platform()},
        "fixture": {"grid": [ny, nx],
                    "base_days": base_days, "delta_days": delta_days,
                    "base_span": [all_base[0], all_base[-1]],
                    "delta_span": [all_delta[0], all_delta[-1]],
                    "base_geometry": base_geom, "delta_geometry": delta_geom,
                    "base_files": bfiles, "base_bytes": bbytes,
                    "delta_files": dfiles, "delta_bytes": dbytes},
        "point": [lon, lat], "grid_index": [int(ii), int(jj)],
        "cases": cases,
        "rss_mb": {"start": rss0, "end": _rss_mb()},
        "g3_reference_p95_ms": {
            "single_day_point": cases["single_day_point"]["latency"]["p95_ms"],
            "range_366d_base_only": cases["range_366d_base_only"]["latency"]["p95_ms"],
            "range_366d_crossing_min_delta": crossing[0]["latency"]["p95_ms"],
        },
    }


def main():
    ap = argparse.ArgumentParser(description="P5-S0 TieredCube baseline of record")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--ny", type=int, default=256)
    ap.add_argument("--nx", type=int, default=256)
    ap.add_argument("--base-days", type=int, default=450, dest="base_days",
                    help="must be >= 366 so the 366-day case is exactly 366 days")
    ap.add_argument("--delta-days", type=int, default=64, dest="delta_days")
    ap.add_argument("--delta-spans", default="31,45,64", dest="delta_spans")
    ap.add_argument("--repeats", type=int, default=15)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    import tempfile
    workdir = args.workdir or tempfile.mkdtemp(prefix="p5s0_base_")
    res = run(workdir, ny=args.ny, nx=args.nx, base_days=args.base_days,
              delta_days=args.delta_days, repeats=args.repeats,
              delta_spans=[int(x) for x in args.delta_spans.split(",")])
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as fh:
            json.dump(res, fh, indent=2, sort_keys=True)
        print(f"wrote {args.out}")
    c = res["cases"]
    print(f"single-day point   p50/p95 = {c['single_day_point']['latency']['p50_ms']}/"
          f"{c['single_day_point']['latency']['p95_ms']} ms  "
          f"chunks={c['single_day_point']['analytic']['chunk_count']}")
    print(f"366-day base range p50/p95 = {c['range_366d_base_only']['latency']['p50_ms']}/"
          f"{c['range_366d_base_only']['latency']['p95_ms']} ms  "
          f"chunks={c['range_366d_base_only']['analytic']['chunk_count']}  "
          f"read_amp={c['range_366d_base_only']['analytic']['read_amplification']}x")
    for row in c["range_366d_crossing"]["sweep"]:
        print(f"crossing delta_span={row['delta_span_days']:>3}d  "
              f"p50/p95 = {row['latency']['p50_ms']}/{row['latency']['p95_ms']} ms  "
              f"delta_chunks={row['analytic_delta']['chunk_count']}  "
              f"delta_decompressed={row['analytic_delta']['decompressed_bytes']/1e6:.1f} MB")


if __name__ == "__main__":
    main()
