"""dev2026 — point time-series critical-path benchmark.

Goal: empirically locate where the time goes when serving a single-point query
over many days, which is ghrsst's PRIMARY query pattern (currently capped at 31
days). We faithfully replicate the production point path in ghrsst_app.read_ghrsst
and break the cost into:

  (A) per-day group open  (xr.open_zarr(root, group=/Y/M/D, consolidated=None))
  (B) coordinate read     (lon: 36000, lat: 17999)
  (C) point value read    (isel + .compute  -> 1 spatial chunk decompress)

Then we measure an OPTIMIZED path (open root group ONCE via zarr, reuse coords)
to quantify how much of the cost is redundant metadata re-parsing vs unavoidable
chunk decompression.

Run:
  export GHRSST_ZARR_PATH=$(pwd)/bak/data/mur.zarr
  dev2026/.venv/bin/python dev2026/bench/bench_pointseries.py --lon 119.30672 --lat 22.28274 --days 150
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time

import numpy as np
import xarray as xr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from store.zarr_paths import (  # noqa: E402
    group_ref,
    list_existing_days,
    resolve_zarr_path,
)


def idx_from_coord(val: float, arr: np.ndarray) -> int:
    """Mirror ghrsst_app._idx_from_coord (nearest)."""
    j = int(np.searchsorted(arr, val))
    if j == 0:
        return 0
    if j >= arr.size:
        return arr.size - 1
    return j if abs(arr[j] - val) < abs(arr[j - 1] - val) else j - 1


def open_group_app(root: str, day: str):
    """Mirror ghrsst_app._open_group exactly."""
    return xr.open_zarr(root, group=group_ref(day), zarr_format=3, consolidated=None)


def t() -> float:
    return time.perf_counter()


def fmt(seconds: float) -> str:
    return f"{seconds * 1000:8.2f} ms"


def measure_metadata_parse(root: str):
    """Cost of just loading+parsing the consolidated root zarr.json."""
    zjson = os.path.join(root, "zarr.json")
    size = os.path.getsize(zjson)
    samples = []
    for _ in range(5):
        a = t()
        with open(zjson, "rb") as fh:
            json.loads(fh.read())
        samples.append(t() - a)
    return size, min(samples), statistics.median(samples)


def chunk_info(root: str, day: str, field: str = "sst"):
    meta = os.path.join(root, *day.split("-"), field, "zarr.json")
    with open(meta) as fh:
        d = json.load(fh)
    shape = d["shape"]
    cshape = d["chunk_grid"]["configuration"]["chunk_shape"]
    dtype = d.get("data_type", "float32")
    bytes_per = 4 if "32" in str(dtype) else 8
    chunk_uncompressed = int(np.prod(cshape)) * bytes_per
    return shape, cshape, chunk_uncompressed


def production_path(root: str, days, lon0: float, lat0: float, fields):
    """Replicate read_ghrsst point path: coords from first existing day, then
    per-day open + isel.compute. Returns (total, breakdown dict, rows)."""
    breakdown = {"open": 0.0, "coords": 0.0, "point": 0.0, "n_opens": 0}

    # _load_coords_from_any: opens first existing day to get lon/lat
    a = t()
    with open_group_app(root, days[0]) as ds0:
        lon = np.asarray(ds0["lon"].values)
        lat = np.asarray(ds0["lat"].values)
    breakdown["coords"] += t() - a
    breakdown["n_opens"] += 1

    lo = float(min(max(lon0, lon.min()), lon.max()))
    la = float(min(max(lat0, lat.min()), lat.max()))
    jj = idx_from_coord(lo, lon)
    ii = idx_from_coord(la, lat)

    rows = []
    for day in days:
        a = t()
        ds = open_group_app(root, day)
        breakdown["open"] += t() - a
        breakdown["n_opens"] += 1
        a = t()
        row = {"date": day}
        for f in fields:
            if f in ds:
                v = ds[f].isel(lat=ii, lon=jj).compute().values.item()
                row[f] = None if (v is None or np.isnan(v)) else float(v)
        breakdown["point"] += t() - a
        ds.close()
        rows.append(row)

    total = breakdown["open"] + breakdown["coords"] + breakdown["point"]
    return total, breakdown, rows


def optimized_path(root: str, days, lon0: float, lat0: float, fields):
    """Open the root group ONCE (parse consolidated metadata once), reuse coords
    loaded once, then index each day's subgroup arrays directly via zarr."""
    import zarr

    breakdown = {"open_root": 0.0, "coords": 0.0, "point": 0.0}

    a = t()
    rg = zarr.open_group(root, mode="r")
    breakdown["open_root"] += t() - a

    # coords: read once from first day
    a = t()
    g0 = rg[days[0].replace("-", "/")]
    lon = np.asarray(g0["lon"][:])
    lat = np.asarray(g0["lat"][:])
    breakdown["coords"] += t() - a

    lo = float(min(max(lon0, lon.min()), lon.max()))
    la = float(min(max(lat0, lat.min()), lat.max()))
    jj = idx_from_coord(lo, lon)
    ii = idx_from_coord(la, lat)

    rows = []
    for day in days:
        a = t()
        g = rg[day.replace("-", "/")]
        row = {"date": day}
        for f in fields:
            if f in g:
                v = float(g[f][0, ii, jj]) if g[f].ndim == 3 else float(g[f][ii, jj])
                row[f] = None if np.isnan(v) else v
        breakdown["point"] += t() - a
        rows.append(row)

    total = breakdown["open_root"] + breakdown["coords"] + breakdown["point"]
    return total, breakdown, rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lon", type=float, default=119.30672)
    ap.add_argument("--lat", type=float, default=22.28274)
    ap.add_argument("--days", type=int, default=150, help="cap number of days")
    ap.add_argument("--fields", default="sst,sea_ice")
    args = ap.parse_args()

    root = resolve_zarr_path()
    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    all_days = list_existing_days(root)
    if not all_days:
        raise SystemExit(f"No /YYYY/MM/DD groups under {root}")
    days = all_days[-args.days :] if args.days else all_days

    print(f"store         : {root}")
    print(f"existing days : {len(all_days)}  (using last {len(days)}: {days[0]} .. {days[-1]})")
    print(f"point         : lon={args.lon} lat={args.lat}  fields={fields}")
    print()

    # metadata parse cost
    size, mn, med = measure_metadata_parse(root)
    print(f"[root zarr.json] size={size/1e6:.2f} MB  parse: min={fmt(mn)} median={fmt(med)}")
    shape, cshape, cbytes = chunk_info(root, days[0])
    print(f"[sst array] shape={shape} chunk={cshape} -> {cbytes/1e6:.2f} MB uncompressed / point-read")
    print()

    # production-faithful path
    p_total, p_bd, p_rows = production_path(root, days, args.lon, args.lat, fields)
    print("=== PRODUCTION path (per-day open + per-request coords) ===")
    print(f"  group opens     : {p_bd['n_opens']}")
    print(f"  coords read     : {fmt(p_bd['coords'])}")
    print(f"  per-day open    : {fmt(p_bd['open'])}   ({fmt(p_bd['open']/len(days))}/day)")
    print(f"  point isel/comp : {fmt(p_bd['point'])}   ({fmt(p_bd['point']/len(days))}/day)")
    print(f"  TOTAL           : {fmt(p_total)}   ({fmt(p_total/len(days))}/day)")
    print()

    # optimized path
    o_total, o_bd, o_rows = optimized_path(root, days, args.lon, args.lat, fields)
    print("=== OPTIMIZED path (open root once, reuse coords, direct chunk index) ===")
    print(f"  open root       : {fmt(o_bd['open_root'])}")
    print(f"  coords read     : {fmt(o_bd['coords'])}")
    print(f"  point read      : {fmt(o_bd['point'])}   ({fmt(o_bd['point']/len(days))}/day)")
    print(f"  TOTAL           : {fmt(o_total)}   ({fmt(o_total/len(days))}/day)")
    print()

    speedup = p_total / o_total if o_total else float("nan")
    print(f"=== SUMMARY ===  optimized is {speedup:.1f}x faster on {len(days)} days")
    print(f"  extrapolated 365-day series: production ~{p_total/len(days)*365:.2f}s  optimized ~{o_total/len(days)*365:.2f}s")

    # parity check: same values?
    mism = 0
    for pr, orow in zip(p_rows, o_rows):
        for f in fields:
            pv, ov = pr.get(f), orow.get(f)
            if pv is None and ov is None:
                continue
            if pv is None or ov is None or abs(pv - ov) > 1e-4:
                mism += 1
    print(f"  parity (prod vs optimized values): {'OK' if mism == 0 else f'{mism} MISMATCH'}")


if __name__ == "__main__":
    main()
