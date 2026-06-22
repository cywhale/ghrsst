"""dev2026 — concurrency / event-loop blocking benchmark.

ghrsst_app.read_ghrsst is `async def` but calls synchronous xr.open_zarr(...).compute()
with NO threadpool offload. Under concurrent point requests (the map platform fires
one HTTP request per point), each request blocks the event loop, so a single uvicorn
worker serializes them. gunicorn runs only -w 2 workers in production, so effective
concurrency is ~2 regardless of how many points the client requests.

This bench simulates N concurrent single-point/single-day "requests" three ways:
  (1) SERIAL                         - baseline sum of costs
  (2) asyncio.gather, blocking body  - what the current async endpoint does
  (3) asyncio.gather + to_thread     - the proposed fix (offload sync work)

Run:
  export GHRSST_ZARR_PATH=$(pwd)/bak/data/mur.zarr
  dev2026/.venv/bin/python dev2026/bench/bench_concurrency.py --n 16
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

import numpy as np
import xarray as xr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from store.zarr_paths import group_ref, list_existing_days, resolve_zarr_path  # noqa: E402


def idx_from_coord(val, arr):
    j = int(np.searchsorted(arr, val))
    if j == 0:
        return 0
    if j >= arr.size:
        return arr.size - 1
    return j if abs(arr[j] - val) < abs(arr[j - 1] - val) else j - 1


def one_request(root, day, lon0, lat0, fields):
    """A faithful single-point/single-day request: open group (re-parse metadata),
    read coords, read point. Synchronous, exactly like the production body."""
    with xr.open_zarr(root, group=group_ref(day), zarr_format=3, consolidated=None) as ds:
        lon = np.asarray(ds["lon"].values)
        lat = np.asarray(ds["lat"].values)
        jj = idx_from_coord(float(lon0), lon)
        ii = idx_from_coord(float(lat0), lat)
        out = {}
        for f in fields:
            if f in ds:
                v = ds[f].isel(lat=ii, lon=jj).compute().values.item()
                out[f] = None if np.isnan(v) else float(v)
        return out


def t():
    return time.perf_counter()


async def run_blocking(reqs):
    """Each coroutine runs the sync body inline -> blocks the loop (current behavior)."""
    async def h(args):
        return one_request(*args)
    return await asyncio.gather(*(h(a) for a in reqs))


async def run_offloaded(reqs):
    """Offload the sync body to the default thread pool (proposed fix)."""
    return await asyncio.gather(*(asyncio.to_thread(one_request, *a) for a in reqs))


def one_request_cached(rg, day, ii, jj, fields):
    """Cached body: root group already open, coords already resolved to ii/jj.
    Only the per-point chunk read remains (blosc/zstd decompress releases the GIL)."""
    g = rg[day.replace("-", "/")]
    out = {}
    for f in fields:
        if f in g:
            v = float(g[f][0, ii, jj]) if g[f].ndim == 3 else float(g[f][ii, jj])
            out[f] = None if np.isnan(v) else v
    return out


async def run_cached_blocking(rg, day, pts, fields):
    async def h(ii, jj):
        return one_request_cached(rg, day, ii, jj, fields)
    return await asyncio.gather(*(h(ii, jj) for ii, jj in pts))


async def run_cached_offloaded(rg, day, pts, fields):
    return await asyncio.gather(
        *(asyncio.to_thread(one_request_cached, rg, day, ii, jj, fields) for ii, jj in pts)
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=16, help="concurrent requests")
    ap.add_argument("--fields", default="sst")
    args = ap.parse_args()

    root = resolve_zarr_path()
    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    days = list_existing_days(root)
    if not days:
        raise SystemExit("no days")
    day = days[-1]

    # scatter N distinct points so chunk caches don't trivially overlap
    rng = np.linspace(0, 1, args.n)
    reqs = [(root, day, 118.0 + 4 * r, 20.0 + 4 * r, fields) for r in rng]

    print(f"store: {root}")
    print(f"day  : {day}   concurrent requests: {args.n}   fields: {fields}")
    print()

    # warm once (load codecs etc.)
    one_request(*reqs[0])

    a = t()
    for r in reqs:
        one_request(*r)
    serial = t() - a

    a = t()
    asyncio.run(run_blocking(list(reqs)))
    blocking = t() - a

    a = t()
    asyncio.run(run_offloaded(list(reqs)))
    offloaded = t() - a

    print(f"  SERIAL (sum)              : {serial*1000:8.1f} ms")
    print(f"  asyncio.gather BLOCKING   : {blocking*1000:8.1f} ms   (current async endpoint)")
    print(f"  asyncio.gather TO_THREAD  : {offloaded*1000:8.1f} ms   (proposed offload fix)")
    print()
    print(f"  blocking vs serial : {blocking/serial:4.2f}x  (~1.0 => no concurrency; event loop serialized)")
    print(f"  offload speedup    : {serial/offloaded:4.2f}x  vs serial on this machine's cores")
    print(f"  -> offload alone does NOT help: open_zarr metadata parse is GIL-bound Python.")
    print()

    # --- cached body: root opened once, coords resolved once; only chunk reads remain ---
    import zarr
    rg = zarr.open_group(root, mode="r")
    g0 = rg[day.replace("-", "/")]
    lon = np.asarray(g0["lon"][:])
    lat = np.asarray(g0["lat"][:])
    pts = [(idx_from_coord(lat0, lat), idx_from_coord(lon0, lon))
           for (_, _, lon0, lat0, _) in reqs]

    one_request_cached(rg, day, *pts[0], fields)  # warm
    a = t()
    for ii, jj in pts:
        one_request_cached(rg, day, ii, jj, fields)
    c_serial = t() - a
    a = t()
    asyncio.run(run_cached_blocking(rg, day, pts, fields))
    c_block = t() - a
    a = t()
    asyncio.run(run_cached_offloaded(rg, day, pts, fields))
    c_off = t() - a

    print("=== CACHED body (root open once + coords cached) — the proposed steady state ===")
    print(f"  CACHED serial (sum)       : {c_serial*1000:8.1f} ms   ({c_serial/args.n*1000:.2f} ms/point)")
    print(f"  CACHED gather BLOCKING    : {c_block*1000:8.1f} ms")
    print(f"  CACHED gather TO_THREAD   : {c_off*1000:8.1f} ms")
    print(f"  cached offload speedup    : {c_serial/c_off:4.2f}x  (chunk decompress releases GIL => real parallelism)")
    print(f"  full win vs current       : {serial/c_off:4.2f}x  (current serial {serial*1000:.0f}ms -> cached+offload {c_off*1000:.0f}ms)")


if __name__ == "__main__":
    main()
