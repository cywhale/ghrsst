"""dev2026 — bbox single-day cost (secondary query pattern).

Mirrors the BBOX branch of read_ghrsst: open chosen day, isel a lat/lon slice,
.compute(), ravel to rows. Reports cost vs region size so we know how heavy the
secondary pattern is and whether chunk alignment matters.

Run:
  export GHRSST_ZARR_PATH=$(pwd)/bak/data/mur.zarr
  dev2026/.venv/bin/python dev2026/bench/bench_bbox.py
"""
from __future__ import annotations

import os
import sys
import time

import numpy as np
import xarray as xr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from store.zarr_paths import group_ref, list_existing_days, resolve_zarr_path  # noqa: E402


def idx(val, arr):
    j = int(np.searchsorted(arr, val))
    if j == 0:
        return 0
    if j >= arr.size:
        return arr.size - 1
    return j if abs(arr[j] - val) < abs(arr[j - 1] - val) else j - 1


def t():
    return time.perf_counter()


def main():
    root = resolve_zarr_path()
    day = list_existing_days(root)[-1]
    fields = ["sst"]
    print(f"store: {root}\nday  : {day}\n")

    a = t()
    ds = xr.open_zarr(root, group=group_ref(day), zarr_format=3, consolidated=None)
    open_ms = (t() - a) * 1000
    lon = np.asarray(ds["lon"].values)
    lat = np.asarray(ds["lat"].values)
    print(f"open+coords: {open_ms:.1f} ms")

    for deg in (1.0, 5.0, 10.0):
        lo0, lo1 = 119.0, 119.0 + deg
        la0, la1 = 21.0, 21.0 + deg
        j0, j1 = sorted((idx(lo0, lon), idx(lo1, lon)))
        i0, i1 = sorted((idx(la0, lat), idx(la1, lat)))
        nj, ni = j1 - j0 + 1, i1 - i0 + 1
        a = t()
        sub = ds[fields].isel(lat=slice(i0, i1 + 1), lon=slice(j0, j1 + 1)).compute()
        _ = sub["sst"].values
        comp_ms = (t() - a) * 1000
        npts = nj * ni
        print(f"  bbox {deg:>4}deg  {ni}x{nj}={npts:>9,} pts  compute={comp_ms:8.1f} ms  ({comp_ms/npts*1e6:.2f} us/pt)")
    ds.close()


if __name__ == "__main__":
    main()
