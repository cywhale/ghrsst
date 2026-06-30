"""dev2026 — P4-S0 (local/shadow FEASIBILITY): daily store vs time-cube for every daily-routed query.

Compares the three paths that route to the daily store today — single-day BBOX, single-day POINT,
POST /points — across DAILY vs BASE-cube (historical day) vs DELTA-cube (recent day). PARITY FIRST
(values + absent/NaN semantics), then perf: cache-independent chunk_count/decompressed/read_amp
(analytic), warm p95 latency, concurrency C=4/C=8, peak RSS. BBOX also reports json vs compact
(grid≈raster candidate) bytes.

LOCAL/SHADOW ONLY. Builds a synthetic topology in a tempdir (a FULL 90-day base block so the (90,8,8)
chunk is representative). Touches NO VM24 / production data. This is FEASIBILITY EVIDENCE — it does NOT
authorize pruning the daily store (that needs the P4-S0b VM24 read-only binding gate + approval).

Run: dev2026/.venv/bin/python dev2026/bench/bench_p4s0_daily_vs_cube.py [--date YYYYMMDD] [--grid 1024]
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import psutil
import zarr

HERE = os.path.dirname(__file__)
DEV = os.path.join(HERE, "..")
sys.path.insert(0, DEV)
from store.zarr_paths import group_path  # noqa: E402
from store.store_access import StoreAccess  # noqa: E402
from store.time_cube import TimeCubeStore  # noqa: E402
from store.cube_singleday_proto import (cube_bbox_arrays, cube_point, cube_points_batch,  # noqa: E402
                                        chunk_cost)
from store.bbox_encode import encode_grid  # noqa: E402
from ingest.build_timecube_bulk import build_timecube_bulk  # noqa: E402
from ingest.dual_write import append_to_delta  # noqa: E402

VARS = ("sst", "sst_anomaly", "sea_ice")
_PROC = psutil.Process()


class Peak:
    def __enter__(self):
        self.run = True; self.peak = _PROC.memory_info().rss; self.base = self.peak
        self.th = threading.Thread(target=self._p, daemon=True); self.th.start(); return self
    def _p(self):
        while self.run:
            self.peak = max(self.peak, _PROC.memory_info().rss); time.sleep(0.004)
    def __exit__(self, *a):
        self.run = False; self.th.join()
    @property
    def mb(self):
        return round((self.peak - self.base) / 1e6, 1)


def _field(t, G):
    f = (5 + 0.01 * (t % 13) + 0.001 * np.arange(G)[:, None] + 0.0005 * np.arange(G)[None, :]).astype(np.float32)
    return f


def _build(tmp, G, ndays):
    daily = os.path.join(tmp, "daily")
    days = [(datetime.date(2026, 1, 1) + datetime.timedelta(days=i)).isoformat() for i in range(ndays)]
    rng = np.random.default_rng(0)
    lon = np.linspace(100, 140, G).astype(np.float32); lat = np.linspace(0, 40, G).astype(np.float32)
    nanmask = rng.random((G, G)) < 0.05
    for i, d in enumerate(days):
        g = zarr.open_group(group_path(daily, d), mode="w", zarr_format=3)
        g.create_array("lon", shape=(G,), dtype="float32", chunks=(G,)); g["lon"][:] = lon
        g.create_array("lat", shape=(G,), dtype="float32", chunks=(G,)); g["lat"][:] = lat
        for v in VARS:
            fld = _field(i, G).copy(); fld[nanmask] = np.nan
            g.create_array(v, shape=(1, G, G), dtype="float32", chunks=(1, 1024, 1024)); g[v][0] = fld
    base = os.path.join(tmp, "base")
    build_timecube_bulk(daily, base, spatial_chunk=8, time_chunk=90, shard_spatial=128,
                        workers=4, end_day=days[-2])               # full 90-block over days[:-1]
    delta = os.path.join(tmp, "delta")
    append_to_delta(daily, delta, days[-1])                        # recent day -> delta (s256/t1)
    return daily, base, delta, days


def _p95(fn, n):
    ts = []
    for _ in range(n):
        t = time.perf_counter(); fn(); ts.append((time.perf_counter() - t) * 1000)
    ts.sort()
    return round(ts[min(len(ts) - 1, int(math.ceil(0.95 * len(ts)) - 1))], 1), round(ts[0], 1)


def _conc(fn, C, waves=2):
    lat = []
    with Peak() as pk:
        with ThreadPoolExecutor(max_workers=C) as ex:
            def task():
                t = time.perf_counter(); fn(); return (time.perf_counter() - t) * 1000
            for _ in range(waves):
                lat.extend(ex.map(lambda _i: task(), range(C)))
    lat.sort()
    return {"p95_ms": round(lat[int(math.ceil(0.95 * len(lat)) - 1)], 1), "rss_mb": pk.mb}


def _vals_bbox(cols):
    return {f: np.nan_to_num(np.asarray(cols[f], np.float32), nan=-9e9) for f in cols}


def _parity_bbox(d_cols, c_cols):
    if set(d_cols) != set(c_cols):
        return False
    for f in d_cols:
        a = _vals_bbox(d_cols)[f]; b = _vals_bbox(c_cols)[f]
        if a.shape != b.shape or not np.array_equal(a, b):
            return False
    return True


def _parity_rows(a, b):
    if len(a) != len(b):
        return False
    for ra, rb in zip(a, b):
        if set(ra) != set(rb):                          # same keys (absent-omit / null parity)
            return False
        for k in ra:
            va, vb = ra[k], rb[k]
            if isinstance(va, float) and isinstance(vb, float):
                if np.float32(va) != np.float32(vb) and not (math.isnan(va) and math.isnan(vb)):
                    return False
            elif va != vb:
                return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="unknown"); ap.add_argument("--grid", type=int, default=1024)
    args = ap.parse_args()
    G = args.grid
    tmp = tempfile.mkdtemp(prefix="p4s0_")
    daily_p, base_p, delta_p, days = _build(tmp, G, 91)
    sa = StoreAccess(daily_p); base = TimeCubeStore(base_p); delta = TimeCubeStore(delta_p)
    d_hist = days[45]; d_recent = days[90]
    fields = list(VARS)
    SIZES = [("small50k", 224, 224), ("med250k", 500, 500), ("large750k", 866, 866)]
    # central bbox windows by lon/lat for each size
    lon, lat = sa._coords()

    def win(ni, nj):
        i0 = (lat.size - ni) // 2; j0 = (lon.size - nj) // 2
        return float(lon[j0]), float(lat[i0]), float(lon[j0 + nj - 1]), float(lat[i0 + ni - 1])

    rng = np.random.default_rng(7)
    batch_pts = [[float(lon[rng.integers(0, lon.size)]), float(lat[rng.integers(0, lat.size)])] for _ in range(1000)]
    rep = {"date_tag": args.date, "grid": G, "hist_day": d_hist, "recent_day": d_recent,
           "chunks": {"daily": [1, 1024, 1024], "base": list(base._array("sst").chunks),
                      "delta": list(delta._array("sst").chunks)},
           "note": "LOCAL/SHADOW feasibility only — does NOT authorize pruning daily (needs P4-S0b VM24 binding gate).",
           "bbox": [], "point": {}, "batch": {}}
    print(f"# P4-S0 daily vs cube — grid {G}², hist={d_hist}(base) recent={d_recent}(delta)")
    print(f"chunks daily{rep['chunks']['daily']} base{rep['chunks']['base']} delta{rep['chunks']['delta']}\n")

    # ---------- BBOX ----------
    print("## bbox  | source | size | parity | read_amp | chunk_count | warm p95 ms | C8 p95/RSS | json MB | grid MB")
    for label, ni, nj in SIZES:
        lo0, la0, lo1, la1 = win(ni, nj)
        d_lons, d_lats, d_cols = sa.bbox_arrays(d_hist, lo0, la0, lo1, la1, fields)
        for src, tc, day in (("daily", None, d_hist), ("base", base, d_hist), ("delta", delta, d_recent)):
            if src == "daily":
                run = lambda: sa.bbox_arrays(day, lo0, la0, lo1, la1, fields)
                lons, lats, cols = sa.bbox_arrays(day, lo0, la0, lo1, la1, fields)
                ch = chunk_cost((1, 1024, 1024), ni, nj, len(cols))
            else:
                run = lambda tc=tc, day=day: cube_bbox_arrays(tc, day, lo0, la0, lo1, la1, fields)
                lons, lats, cols = cube_bbox_arrays(tc, day, lo0, la0, lo1, la1, fields)
                ch = chunk_cost(tc._array("sst").chunks, ni, nj, len(cols))
            par = _parity_bbox(d_cols, cols) if src in ("daily", "base") else None  # delta=recent day, no daily-hist ref
            n = 3 if (src == "base" and ni >= 800) else 5
            wp95, _ = _p95(run, n)
            c8 = _conc(run, 8, waves=1)
            json_b = len(StoreAccess.bbox_rows_window(lons, lats, cols, fields, day, 0, lons.size * lats.size))
            import orjson
            json_mb = len(orjson.dumps(StoreAccess.bbox_rows_window(lons, lats, cols, fields, day, 0, lons.size * lats.size))) / 1e6
            grid_mb = len(encode_grid(lons, lats, cols, fields, day)) / 1e6
            row = {"source": src, "size": label, "points": ni * nj, "parity": par, **ch,
                   "warm_p95_ms": wp95, "c8_p95_ms": c8["p95_ms"], "c8_rss_mb": c8["rss_mb"],
                   "json_mb": round(json_mb, 1), "grid_mb": round(grid_mb, 1)}
            rep["bbox"].append(row)
            print(f"   | {src:5} | {label:9} | {str(par):5} | {ch['read_amp']:>8} | {ch['chunk_count']:>7} | "
                  f"{wp95:>8} | {c8['p95_ms']}/{c8['rss_mb']} | {json_mb:.1f} | {grid_mb:.1f}")

    # ---------- POINT (single day) ----------
    print("\n## point (single day) | source | parity | read_amp | warm p95 ms | C8 p95/RSS")
    plon, plat = float(lon[lon.size // 2]), float(lat[lat.size // 2])
    d_pt = sa.point_series(plon, plat, [d_hist], fields)
    for src, tc, day, ref in (("daily", None, d_hist, d_pt), ("base", base, d_hist, d_pt),
                              ("delta", delta, d_recent, None)):
        if src == "daily":
            run = lambda: sa.point_series(plon, plat, [day], fields); got = sa.point_series(plon, plat, [day], fields)
            ch = chunk_cost((1, 1024, 1024), 1, 1, len(fields))
        else:
            run = lambda tc=tc, day=day: cube_point(tc, day, plon, plat, fields); got = cube_point(tc, day, plon, plat, fields)
            ch = chunk_cost(tc._array("sst").chunks, 1, 1, len(fields))
        par = _parity_rows(ref, got) if ref is not None else None
        wp95, _ = _p95(run, 9); c8 = _conc(run, 8, waves=3)
        rep["point"][src] = {"parity": par, **ch, "warm_p95_ms": wp95, "c8_p95_ms": c8["p95_ms"], "c8_rss_mb": c8["rss_mb"]}
        print(f"   | {src:5} | {str(par):5} | {ch['read_amp']:>8} | {wp95:>8} | {c8['p95_ms']}/{c8['rss_mb']}")

    # ---------- POST points (batch, single day) ----------
    print("\n## POST points (1000, single day) | source | parity | chunk_count | warm p95 ms | C8 p95/RSS")
    ijs = [( _idx(plat_v, lat), _idx(plon_v, lon)) for plon_v, plat_v in batch_pts]
    d_b = sa.points_batch(batch_pts, d_hist, fields)
    for src, tc, day, ref in (("daily", None, d_hist, d_b), ("base", base, d_hist, d_b),
                              ("delta", delta, d_recent, None)):
        if src == "daily":
            run = lambda: sa.points_batch(batch_pts, day, fields); got = sa.points_batch(batch_pts, day, fields)
            ch = chunk_cost((1, 1024, 1024), 1, 1, len(fields), access="batch", ij_list=ijs)
        else:
            run = lambda tc=tc, day=day: cube_points_batch(tc, day, batch_pts, fields); got = cube_points_batch(tc, day, batch_pts, fields)
            ch = chunk_cost(tc._array("sst").chunks, 1, 1, len(fields), access="batch", ij_list=ijs)
        par = _parity_rows(ref, got) if ref is not None else None
        wp95, _ = _p95(run, 5); c8 = _conc(run, 8, waves=1)
        rep["batch"][src] = {"parity": par, **ch, "warm_p95_ms": wp95, "c8_p95_ms": c8["p95_ms"], "c8_rss_mb": c8["rss_mb"]}
        print(f"   | {src:5} | {str(par):5} | {ch['chunk_count']:>7} | {wp95:>8} | {c8['p95_ms']}/{c8['rss_mb']}")

    res = os.path.join(HERE, "results"); os.makedirs(res, exist_ok=True)
    art = os.path.join(res, f"p4s0_daily_vs_cube_{args.date}.json")
    with open(art, "w") as fh:
        json.dump(rep, fh, indent=2)
    print(f"\nartifact -> {os.path.relpath(art, DEV)}  (COLD not measured locally; read_amp is the cache-independent signal)")
    import shutil; shutil.rmtree(tmp, ignore_errors=True)


def _idx(v, arr):
    return int(np.clip(np.searchsorted(arr, v), 0, arr.size - 1))


if __name__ == "__main__":
    main()
