"""dev2026 — bbox response-serialization MEMORY probe.

Codex review [High] #2: the real memory risk is transient chunks + response
serialization, not resident coords. This quantifies the serialization peak for a
large single-day bbox, comparing the current row-by-row dict approach (mirrors
ghrsst_app.read_ghrsst:405-411) against a vectorized columnar build.

Uses tracemalloc (Python-object peak — the relevant signal for JSON building).

Run:
  export GHRSST_ZARR_PATH=$(pwd)/bak/data/mur.zarr
  dev2026/.venv/bin/python dev2026/bench/bench_bbox_memory.py --deg 10
"""
from __future__ import annotations

import argparse
import os
import resource
import sys
import tracemalloc

import numpy as np
import orjson
import xarray as xr


def _maxrss_mb():
    """Process peak RSS. ru_maxrss is bytes on macOS, KiB on Linux."""
    ru = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return ru / 1e6 if sys.platform == "darwin" else ru / 1e3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from store.zarr_paths import group_ref, list_existing_days, resolve_zarr_path  # noqa: E402


def idx(val, arr):
    j = int(np.searchsorted(arr, val))
    if j <= 0:
        return 0
    if j >= arr.size:
        return arr.size - 1
    return j if abs(arr[j] - val) < abs(arr[j - 1] - val) else j - 1


def peak_mb(fn):
    tracemalloc.start()
    out = fn()
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return peak / 1e6, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deg", type=float, default=10.0, help="bbox edge in degrees")
    ap.add_argument("--fields", default="sst,sea_ice")
    args = ap.parse_args()

    root = resolve_zarr_path()
    day = list_existing_days(root)[-1]
    fields = [f.strip() for f in args.fields.split(",") if f.strip()]

    ds = xr.open_zarr(root, group=group_ref(day), zarr_format=3, consolidated=None)
    lon = np.asarray(ds["lon"].values)
    lat = np.asarray(ds["lat"].values)
    lo0, lo1 = 119.0, 119.0 + args.deg
    la0, la1 = 10.0, 10.0 + args.deg
    j0, j1 = sorted((idx(lo0, lon), idx(lo1, lon)))
    i0, i1 = sorted((idx(la0, lat), idx(la1, lat)))
    sub = ds[fields].isel(lat=slice(i0, i1 + 1), lon=slice(j0, j1 + 1)).compute()
    lats = sub["lat"].values.astype(float)
    lons = sub["lon"].values.astype(float)
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    flat = {"lon": lon_grid.ravel(), "lat": lat_grid.ravel()}
    for f in fields:
        flat[f] = sub[f].values.astype(np.float32).reshape(-1)
    n = flat["lon"].size
    print(f"store: {root}\nday  : {day}  bbox {args.deg}deg  points={n:,}  fields={fields}\n")

    # ---- Path A: current row-by-row dict building + orjson (mirrors app) ----
    def path_a():
        rows = []
        for k in range(n):
            row = {"lon": float(flat["lon"][k]), "lat": float(flat["lat"][k]), "date": day}
            for f in fields:
                v = flat[f][k]
                row[f] = None if np.isnan(v) else float(v)
            rows.append(row)
        return orjson.dumps(rows)

    # ---- Path B: vectorized columnar -> list of dicts via zip (less temp) ---
    def path_b():
        cols = {"lon": flat["lon"].tolist(), "lat": flat["lat"].tolist()}
        for f in fields:
            arr = flat[f]
            cols[f] = [None if np.isnan(x) else float(x) for x in arr]
        rows = [dict(zip(("lon", "lat", *fields, "date"),
                         (cols["lon"][k], cols["lat"][k], *(cols[f][k] for f in fields), day)))
                for k in range(n)]
        return orjson.dumps(rows)

    # ---- Path C: STREAMING in batches (peak = one batch, not all rows) -----
    BATCH = 50_000
    def path_c():
        total = 0  # simulate a StreamingResponse: encode batch, emit bytes, drop it
        for start in range(0, n, BATCH):
            end = min(start + BATCH, n)
            rows = []
            for k in range(start, end):
                row = {"lon": float(flat["lon"][k]), "lat": float(flat["lat"][k]), "date": day}
                for f in fields:
                    v = flat[f][k]
                    row[f] = None if np.isnan(v) else float(v)
                rows.append(row)
            chunk = orjson.dumps(rows)
            total += len(chunk)   # in real code: yield chunk; here we only keep the count
            del rows, chunk
        return total

    rss0 = _maxrss_mb()
    a_mb, a_out = peak_mb(path_a)
    b_mb, b_out = peak_mb(path_b)
    c_mb, c_bytes = peak_mb(path_c)
    rss1 = _maxrss_mb()

    print(f"[A] row-by-row dict + orjson (current app style): peak Python alloc = {a_mb:8.1f} MB  "
          f"(payload {len(a_out)/1e6:.1f} MB)")
    print(f"[B] 'vectorized' columnar lists + orjson         : peak Python alloc = {b_mb:8.1f} MB  "
          f"(payload {len(b_out)/1e6:.1f} MB)  <- NOT a fix")
    print(f"[C] STREAMING in {BATCH:,}-row batches           : peak Python alloc = {c_mb:8.1f} MB  "
          f"(~{len(b_out)/1e6:.0f} MB streamed, never resident)")
    print(f"\n    process peak RSS (ru_maxrss): {rss0:.0f} -> {rss1:.0f} MB "
          f"(includes native numpy/zarr/orjson buffers; tracemalloc above is Python-only)")
    print(f"    CAVEAT: ru_maxrss is a CUMULATIVE high-water mark after running A, B, then C in ONE")
    print(f"            process — it is ILLUSTRATIVE ('naive paths push RSS high'), NOT a per-path")
    print(f"            RSS comparison and it cannot isolate C's improvement.")
    print(f"    NOTE: tracemalloc proves the Python-object blowup; the AUTHORITATIVE G6 gate is")
    print(f"          HTTP-level worker RSS in scenario-BBOX/-LR (spec P1-S4), not this in-proc number.")
    print(f"\n=> a single {n:,}-point bbox via the current style transiently allocates ~{a_mb:.0f} MB.")
    print(f"   Columnar lists (B) do NOT help ({b_mb:.0f} MB). Only STREAMING (C) bounds it to "
          f"~{c_mb:.0f} MB (one batch).")
    print(f"   Under concurrency A/B multiply per in-flight request -> G6 RSS ceiling, bounded")
    print(f"   executor (P1-S1/S2), and bbox MUST stream (P1-S2 / P2-B), not just 'vectorize'.")
    ds.close()


if __name__ == "__main__":
    main()
