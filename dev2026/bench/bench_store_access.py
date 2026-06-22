"""dev2026 — P1-S1 store-layer speedup check (function level, pre-HTTP).

Confirms StoreAccess.point_series / points_batch are >=8x faster than the old
per-day xr.open_zarr path (spec P1-S1 acceptance), with parity already proven in
tests/test_store_access.py. Full HTTP-level / RSS / scenario load tests are P1-S4
(after the P1-S2 API exists).

Run:
  export GHRSST_ZARR_PATH=$(pwd)/bak/data/mur.zarr
  dev2026/.venv/bin/python dev2026/bench/bench_store_access.py --days 150
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
import xarray as xr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from store.store_access import StoreAccess, _nearest_idx  # noqa: E402
from store.zarr_paths import group_ref, list_existing_days, resolve_zarr_path  # noqa: E402


def t():
    return time.perf_counter()


def old_point_series(root, days, lon0, lat0, fields):
    """Faithful old per-day open path (mirrors ghrsst_app point loop)."""
    with xr.open_zarr(root, group=group_ref(days[0]), zarr_format=3, consolidated=None) as ds0:
        lon = np.asarray(ds0["lon"].values)
        lat = np.asarray(ds0["lat"].values)
    jj = _nearest_idx(float(lon0), lon)
    ii = _nearest_idx(float(lat0), lat)
    rows = []
    for d in days:
        with xr.open_zarr(root, group=group_ref(d), zarr_format=3, consolidated=None) as ds:
            row = {"date": d}
            for f in fields:
                if f in ds:
                    v = ds[f].isel(lat=ii, lon=jj).compute().values.item()
                    row[f] = None if np.isnan(v) else float(v)
            rows.append(row)
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=150)
    ap.add_argument("--lon", type=float, default=119.30672)
    ap.add_argument("--lat", type=float, default=22.28274)
    args = ap.parse_args()
    root = resolve_zarr_path()
    days = list_existing_days(root)[-args.days:]
    fields = ["sst", "sea_ice"]
    print(f"store: {root}\ndays : {len(days)} ({days[0]}..{days[-1]})\n")

    a = t()
    old = old_point_series(root, days, args.lon, args.lat, fields)
    old_s = t() - a

    sa = StoreAccess(zarr_path=root)
    sa.point_series(args.lon, args.lat, days[:1], fields)  # warm coords
    a = t()
    new = sa.point_series(args.lon, args.lat, days, fields)
    new_s = t() - a

    print(f"  OLD per-day open path : {old_s*1000:8.1f} ms ({old_s/len(days)*1000:.2f} ms/day)")
    print(f"  NEW StoreAccess       : {new_s*1000:8.1f} ms ({new_s/len(days)*1000:.2f} ms/day)")
    print(f"  speedup               : {old_s/new_s:5.1f}x   (P1-S1 target >= 8x)")
    print(f"  extrapolated 365-day  : old ~{old_s/len(days)*365:.2f}s  new ~{new_s/len(days)*365:.2f}s")

    # batch timing: N points one day, vs N old single-point reads
    npts = 64
    rng = np.linspace(0, 1, npts)
    pts = [[118.0 + 4 * r, 20.0 + 4 * r] for r in rng]
    day = days[-1]
    a = t()
    for lo, la in pts:
        old_point_series(root, [day], lo, la, ["sst"])
    old_b = t() - a
    a = t()
    sa.points_batch(pts, day, ["sst"])
    new_b = t() - a
    print(f"\n  {npts} points / 1 day:")
    print(f"  OLD (N separate opens): {old_b*1000:8.1f} ms")
    print(f"  NEW points_batch      : {new_b*1000:8.1f} ms   ({old_b/new_b:5.1f}x)")


if __name__ == "__main__":
    main()
