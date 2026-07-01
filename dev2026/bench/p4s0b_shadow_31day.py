"""dev2026 — P4-S0b SHADOW 31-day retention harness (SHADOW/STAGING ONLY — NOT the read-only gate).

The read-only P4-S0b gate (p4s0b_readonly_gate.py) can only validate PER-DAY delta performance because
production delta currently holds ~2 days. This tool exercises the FULL `SPATIAL_WINDOW_DAYS`-day
retention policy by building a **shadow** delta (a fresh delta at a staging path) and validating that:
  * all N recent days are in the delta and serve bbox + POST within the §3.2 absolute budgets;
  * a day OUTSIDE the window (not in the delta) is REJECTED by the spatial policy (delta membership).

**SHADOW/STAGING ONLY.** Defaults to a throwaway tempdir. It WRITES a delta, so it must NEVER point at
the production delta — pass `--delta-out` to an approved staging path only. It is explicitly **not part
of the read-only binding gate**; Codex/ops decide separately whether to run a variant on VM24 as an
approved staging action. Never mutate production from this dev session.

Run (local synthetic, default):
  dev2026/.venv/bin/python dev2026/bench/p4s0b_shadow_31day.py --window 31
Run (shadow delta from a real read-only daily source, writing to an APPROVED staging path):
  ... --daily-src <ro daily> --delta-out <STAGING path, NOT production> --window 31
"""
from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import zarr

HERE = os.path.dirname(__file__)
DEV = os.path.join(HERE, "..")
sys.path.insert(0, DEV)
from store.zarr_paths import group_path, list_existing_days  # noqa: E402
from store.store_access import StoreAccess  # noqa: E402
from store.time_cube import TimeCubeStore  # noqa: E402
from store.cube_singleday_proto import cube_bbox_arrays, cube_points_batch, chunk_cost  # noqa: E402
from store import spatial_policy  # noqa: E402
from ingest.dual_write import append_to_delta  # noqa: E402  (writes SHADOW delta only)

VARS = ("sst", "sst_anomaly", "sea_ice")
BUDGET = {"bbox_warm_p95_ms": 200.0, "post_warm_p95_ms": 100.0, "post_c8_p95_ms": 1000.0}
# Refuse the EXACT known production store basenames only (Codex #2). Staging under /Data/ghrsst (the 5 TB
# disk) is ALLOWED, e.g. /home/odbadmin/Data/ghrsst/staging/p4s0b_shadow_31day.zarr — just not the prod stores.
_PROD_BASENAMES = {"mur.zarr", "mur_timecube_s8_t90_sh128.zarr", "mur_timecube_s8_t90_sh128.delta.zarr"}


def _p95(fn, n):
    ts = sorted(((lambda t0=time.perf_counter(): (fn(), (time.perf_counter() - t0) * 1000)[1])()) for _ in range(n))
    return round(ts[int(math.ceil(0.95 * len(ts)) - 1)], 1)


def _c8(fn, C=8):
    """Real concurrent p95 over C threads (Codex #1 — not sequential repeats)."""
    with ThreadPoolExecutor(max_workers=C) as ex:
        lat = list(ex.map(lambda _i: (lambda t0=time.perf_counter(): (fn(), (time.perf_counter() - t0) * 1000)[1])(), range(C)))
    lat.sort()
    return round(lat[int(math.ceil(0.95 * len(lat)) - 1)], 1)


def _guard_not_production(path):
    base = os.path.basename(os.path.normpath(path))
    if base in _PROD_BASENAMES:
        raise SystemExit(f"REFUSING: --delta-out basename '{base}' is a known PRODUCTION store. Use a "
                         f"staging path, e.g. /home/odbadmin/Data/ghrsst/staging/p4s0b_shadow_31day.zarr")


def _build_synth_daily(daily, G, days):
    rng = np.random.default_rng(0)
    lon = np.linspace(100, 140, G).astype(np.float32); lat = np.linspace(0, 40, G).astype(np.float32)
    nan = rng.random((G, G)) < 0.05
    for i, d in enumerate(days):
        g = zarr.open_group(group_path(daily, d), mode="w", zarr_format=3)
        g.create_array("lon", shape=(G,), dtype="float32", chunks=(G,)); g["lon"][:] = lon
        g.create_array("lat", shape=(G,), dtype="float32", chunks=(G,)); g["lat"][:] = lat
        for v in VARS:
            fld = (5 + 0.01 * i + 0.001 * np.arange(G)[:, None]).repeat(G, 1)[:, :G].astype(np.float32)
            fld = fld + 0.0005 * np.arange(G)[None, :]; fld[nan] = np.nan
            g.create_array(v, shape=(1, G, G), dtype="float32", chunks=(1, 1024, 1024)); g[v][0] = fld


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", type=int, default=spatial_policy.SPATIAL_WINDOW_DAYS)
    ap.add_argument("--grid", type=int, default=1024)
    ap.add_argument("--daily-src", default=None, help="real READ-ONLY daily source (else synthetic)")
    ap.add_argument("--delta-out", default=None, help="SHADOW/STAGING delta path (never production)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    N = args.window
    print(f"# P4-S0b SHADOW {N}-day retention harness — SHADOW/STAGING ONLY, not the read-only gate\n")

    tmp = tempfile.mkdtemp(prefix="p4s0b_shadow_")
    delta_out = args.delta_out or os.path.join(tmp, "shadow_delta")
    _guard_not_production(delta_out)
    if os.path.isdir(delta_out):
        raise SystemExit(f"REFUSING: --delta-out '{delta_out}' already exists; use a fresh staging path.")

    if args.daily_src:
        daily = args.daily_src                          # read-only source
        days = list_existing_days(daily)[-N:]
        if len(days) < N:
            print(f"WARNING: source has only {len(days)} days (< window {N})")
    else:
        daily = os.path.join(tmp, "daily")
        days = [(datetime.date(2026, 1, 1) + datetime.timedelta(days=i)).isoformat() for i in range(N)]
        _build_synth_daily(daily, args.grid, days)

    # build the SHADOW delta (writes only to delta_out) by appending the N recent days
    t0 = time.perf_counter()
    for d in days:
        append_to_delta(daily, delta_out, d)            # tiled append, delta-optimized layout
    build_s = round(time.perf_counter() - t0, 1)
    delta = TimeCubeStore(delta_out)
    sa = StoreAccess(daily)
    lon, lat = sa._coords()
    ni = min(500, lat.size); nj = min(500, lon.size)
    i0 = (lat.size - ni) // 2; j0 = (lon.size - nj) // 2
    b = (float(lon[j0]), float(lat[i0]), float(lon[j0 + nj - 1]), float(lat[i0 + ni - 1]))  # ~250k @ prod
    rng = np.random.default_rng(7)
    pts = [[float(lon[rng.integers(0, lon.size)]), float(lat[rng.integers(0, lat.size)])] for _ in range(1000)]

    rep = {"gate": "P4-S0b SHADOW retention", "shadow_only": True, "window": N,
           "delta_days": list(delta.days), "delta_build_s": build_s, "per_day": [], "policy": {}}
    print(f"shadow delta built: {delta.day_count} days in {build_s}s at {delta_out}")
    print(f"{'day':12} | bbox warm/C8 ms | read_amp | POST warm/C8 ms")
    for d in delta.days:
        run_bb = lambda d=d: cube_bbox_arrays(delta, d, *b, VARS)
        lons, lats, cols = run_bb()
        ch = chunk_cost(delta._array("sst").chunks, lats.size, lons.size, len(cols))
        bw = _p95(run_bb, 4); bc = _c8(run_bb)
        run_po = lambda d=d: cube_points_batch(delta, d, pts, VARS)
        pw = _p95(run_po, 4); pc = _c8(run_po)
        rep["per_day"].append({"day": d, "bbox_warm_p95_ms": bw, "bbox_c8_p95_ms": bc,
                               "read_amp": ch["read_amp"], "post_warm_p95_ms": pw, "post_c8_p95_ms": pc})
        print(f"{d:12} | {bw}/{bc} | {ch['read_amp']} | {pw}/{pc}")

    # policy: a day OUTSIDE the window (not in delta) must be rejected
    outside = (datetime.date.fromisoformat(delta.days[0]) - datetime.timedelta(days=1)).isoformat()
    rep["policy"] = {"outside_day": outside,
                     "allowed": spatial_policy.spatial_day_allowed(outside, delta.days),
                     "reject_payload": spatial_policy.rejection_payload(outside, delta.days),
                     "window_bounds": spatial_policy.spatial_window_bounds(delta.days)}
    bbox_ok = all(x["bbox_warm_p95_ms"] < BUDGET["bbox_warm_p95_ms"] for x in rep["per_day"])
    post_ok = all(x["post_warm_p95_ms"] < BUDGET["post_warm_p95_ms"] and x["post_c8_p95_ms"] < BUDGET["post_c8_p95_ms"] for x in rep["per_day"])
    rep["pass_fail"] = {"all_{}_days_bbox_within_budget".format(N): bbox_ok,
                        "all_{}_days_post_within_budget".format(N): post_ok,
                        "outside_day_rejected": not rep["policy"]["allowed"],
                        "OVERALL_full_window": bool(bbox_ok and post_ok and not rep["policy"]["allowed"])}
    print("\n" + json.dumps(rep["pass_fail"], indent=2))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(rep, fh, indent=2)
        print(f"artifact -> {args.out}")
    if not args.delta_out:                              # only auto-clean the throwaway tempdir
        import shutil; shutil.rmtree(tmp, ignore_errors=True)
    else:
        print(f"NOTE: shadow delta left at {delta_out} (staging) — clean up per ops policy.")


if __name__ == "__main__":
    main()
