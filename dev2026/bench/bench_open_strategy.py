"""dev2026 — per-day open strategy: does bypassing the 3 MB root consolidated
metadata make a *fresh* per-day open cheap enough to avoid a long-lived root handle?

Compares, for opening N day-groups and reading one point each:
  (1) PROD       xr.open_zarr(root, group=/Y/M/D, consolidated=None)   # re-parses 3MB root
  (2) ROOT-ONCE  zarr.open_group(root) once, then rg[day]             # fast but STALE to new days
  (3) DIRECT     zarr.open_group(<root>/Y/M/D) per day (no root meta) # fresh, no root handle
  (4) DIRECT+LRU direct open but cached per-day in a dict             # fresh discovery + warm reuse

Answers Codex review [High] #2: if (3)/(4) are close to (2), store_access needs NO
long-lived root handle — cache only coords + LRU of directly-opened day groups, and
discover days by filesystem scan. No consolidated-metadata staleness.

Run:
  export GHRSST_ZARR_PATH=$(pwd)/bak/data/mur.zarr
  dev2026/.venv/bin/python dev2026/bench/bench_open_strategy.py --days 60
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import numpy as np
import xarray as xr
import zarr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from store.zarr_paths import group_ref, list_existing_days, resolve_zarr_path  # noqa: E402


def idx(val, arr):
    j = int(np.searchsorted(arr, val))
    if j <= 0:
        return 0
    if j >= arr.size:
        return arr.size - 1
    return j if abs(arr[j] - val) < abs(arr[j - 1] - val) else j - 1


def t():
    return time.perf_counter()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--lon", type=float, default=119.30672)
    ap.add_argument("--lat", type=float, default=22.28274)
    args = ap.parse_args()

    root = resolve_zarr_path()
    days = list_existing_days(root)[-args.days :]
    print(f"store: {root}\ndays : {len(days)}  ({days[0]}..{days[-1]})\n")

    # resolve indices once from first day (coords immutable across dataset)
    g0 = zarr.open_group(os.path.join(root, *days[0].split("-")), mode="r")
    lon = np.asarray(g0["lon"][:])
    lat = np.asarray(g0["lat"][:])
    ii, jj = idx(args.lat, lat), idx(args.lon, lon)

    def read_sst_xr(ds):
        return float(ds["sst"].isel(lat=ii, lon=jj).compute().values.item())

    def read_sst_z(g):
        return float(g["sst"][0, ii, jj])

    # (1) PROD
    a = t()
    for d in days:
        with xr.open_zarr(root, group=group_ref(d), zarr_format=3, consolidated=None) as ds:
            read_sst_xr(ds)
    prod = t() - a

    # (2) ROOT-ONCE
    a = t()
    rg = zarr.open_group(root, mode="r")
    for d in days:
        read_sst_z(rg[d.replace("-", "/")])
    root_once = t() - a

    # (3) DIRECT per-day (no root handle)
    a = t()
    for d in days:
        g = zarr.open_group(os.path.join(root, *d.split("-")), mode="r")
        read_sst_z(g)
    direct = t() - a

    # (4) DIRECT + LRU (warm second pass simulates steady state)
    cache = {}
    def get_day(d):
        g = cache.get(d)
        if g is None:
            g = zarr.open_group(os.path.join(root, *d.split("-")), mode="r")
            cache[d] = g
        return g
    for d in days:  # warm
        read_sst_z(get_day(d))
    a = t()
    for d in days:
        read_sst_z(get_day(d))
    direct_lru = t() - a

    n = len(days)
    for name, tot in [("(1) PROD open_zarr+consolidated", prod),
                      ("(2) ROOT-ONCE (stale risk)", root_once),
                      ("(3) DIRECT per-day open", direct),
                      ("(4) DIRECT+LRU warm", direct_lru)]:
        print(f"  {name:34} {tot*1000:8.1f} ms  ({tot/n*1000:6.2f} ms/day)")
    print()
    print(f"  DIRECT vs PROD     : {prod/direct:5.1f}x faster")
    print(f"  DIRECT vs ROOT-ONCE: {direct/root_once:5.2f}x (>1 means direct is slower)")
    print(f"  DIRECT+LRU vs PROD : {prod/direct_lru:5.1f}x faster")


if __name__ == "__main__":
    main()
