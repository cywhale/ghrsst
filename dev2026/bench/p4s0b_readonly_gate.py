"""dev2026 — P4-S0b VM24 READ-ONLY binding gate (Claude authors; Codex/ops EXECUTE on VM24).

Validates the adopted §4.1 policy against the REAL production base+delta cube, **strictly read-only**:
opens every store with mode='r', never appends/compacts/prunes, never touches cron/NGINX/deploy. The
only HTTP it issues are GET (point/range, /healthz) and POST /api/ghrsst/points (a READ query, not a
mutation). Refuses to import any write path.

Validates:
  A. full-history point/range from base+delta (cube)         [+ optional API X-Store-Route cross-check]
  B. single-day point GET (cube vs daily parity + latency)
  C. bbox + POST /points on EXISTING delta days (cube prototype reads, read-only) — latency / read_amp /
     RSS / parity vs the daily store for the same day
  D. policy dry-run: delta days are SERVED, a non-delta (older/base) day would be REJECTED (spatial_policy)
  E. /healthz + RSS + current API POST /points read-only smoke (DAILY-served today; the cube POST is
     validated at store-prototype level in C, and policy enforcement is P4-S3)
against the §3.2 ABSOLUTE budgets (bbox warm p95 < 200 ms; POST warm p95 < 100 ms & C8 < ~1 s; point
≤ daily+25% or < 50 ms).

CAVEAT (Codex round-4): production delta currently holds only ~2 days, so this validates PER-DAY delta
performance, NOT the full 31-day retention policy. Use bench/p4s0b_shadow_31day.py (shadow/staging only)
to exercise the full 31-day window — never via this read-only gate.

Run (on VM24, read-only):
  dev2026/.venv/bin/python dev2026/bench/p4s0b_readonly_gate.py \
    --daily /home/odbadmin/Data/ghrsst/mur.zarr \
    --base  /home/odbadmin/Data/ghrsst/mur_timecube_s8_t90_sh128.zarr \
    --delta /home/odbadmin/Data/ghrsst/mur_timecube_s8_t90_sh128.delta.zarr \
    --api-url http://127.0.0.1:8035 --out /tmp/p4s0b_result.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import psutil

HERE = os.path.dirname(__file__)
DEV = os.path.join(HERE, "..")
sys.path.insert(0, DEV)
from store.store_access import StoreAccess  # noqa: E402  (opens mode='r')
from store.time_cube import TimeCubeStore  # noqa: E402  (opens mode='r')
from store.tiered_cube import TieredCube  # noqa: E402
from store.cube_singleday_proto import cube_bbox_arrays, cube_point, cube_points_batch, chunk_cost  # noqa: E402
from store import spatial_policy  # noqa: E402
# NOTE: intentionally NOT importing dual_write / build_timecube_bulk (no write path in a read-only gate).

_PROC = psutil.Process()
VARS = ("sst", "sst_anomaly", "sea_ice")
BUDGET = {"bbox_warm_p95_ms": 200.0, "post_warm_p95_ms": 100.0, "post_c8_p95_ms": 1000.0,
          "point_abs_ms": 50.0}


def _p95(fn, n):
    ts = []
    for _ in range(n):
        t = time.perf_counter(); fn(); ts.append((time.perf_counter() - t) * 1000)
    ts.sort()
    return round(ts[int(math.ceil(0.95 * len(ts)) - 1)], 1)


def _c8(fn, C=8):
    lat = []
    with ThreadPoolExecutor(max_workers=C) as ex:
        lat = list(ex.map(lambda _i: (lambda t0=time.perf_counter(): (fn(), (time.perf_counter() - t0) * 1000)[1])(), range(C)))
    lat.sort()
    return round(lat[int(math.ceil(0.95 * len(lat)) - 1)], 1)


def _http_get(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, dict(r.headers), r.read()


def _http_post(url, obj, timeout=30):
    body = json.dumps(obj).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, dict(r.headers), r.read()


def _bbox_vals(cols):
    return {f: np.nan_to_num(np.asarray(cols[f], np.float32), nan=-9e9) for f in cols}


def _parity_bbox(a, b):
    if set(a) != set(b):
        return False
    va, vb = _bbox_vals(a), _bbox_vals(b)
    return all(va[f].shape == vb[f].shape and np.array_equal(va[f], vb[f]) for f in va)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", required=True); ap.add_argument("--base", required=True)
    ap.add_argument("--delta", required=True); ap.add_argument("--api-url", default=None)
    ap.add_argument("--out", default=None)
    ap.add_argument("--range-days", type=int, default=365)
    args = ap.parse_args()

    sa = StoreAccess(args.daily)                       # mode='r'
    base = TimeCubeStore(args.base); delta = TimeCubeStore(args.delta)   # mode='r'
    tier = TieredCube(base, delta)
    delta_days = list(delta.days)
    win = spatial_policy.spatial_window_bounds(delta_days)
    rep = {"gate": "P4-S0b VM24 READ-ONLY", "read_only": True,
           "caveat": f"prod delta has {len(delta_days)} day(s) -> PER-DAY delta perf only, NOT full "
                     f"{spatial_policy.SPATIAL_WINDOW_DAYS}-day retention (use shadow harness for that)",
           "delta_days": delta_days, "spatial_window": win,
           "base_span": [base.days[0], base.days[-1]] if base.days else None,
           "budgets": BUDGET, "A_point_range": {}, "B_single_point": {}, "C_bbox": [], "C_post": [],
           "D_policy_dryrun": {}, "E_healthz": {}, "pass_fail": {}}
    print(f"# P4-S0b READ-ONLY gate — delta {len(delta_days)}d {win}, base {rep['base_span']}")
    print(f"# {rep['caveat']}\n")

    lon, lat = sa._coords()
    plon, plat = float(lon[lon.size // 2]), float(lat[lat.size // 2])
    latest = delta.latest or (base.latest)

    # ---- A. full-history point/range (cube) ----
    hist_start = base.days[0] if base.days else latest
    days_all = [d for d in tier_days(base, delta)]
    rng_days = days_all[-args.range_days:] if len(days_all) > args.range_days else days_all
    run_pr = lambda: tier.point_series(plon, plat, rng_days, VARS)
    rows = run_pr()
    # sampled parity (Codex #5): compare cube vs daily point values on a few days across the range
    sample_days = rng_days[::max(1, len(rng_days) // 5)][:5]
    a_par = _rows_eq(sa.point_series(plon, plat, sample_days, VARS),
                     tier.point_series(plon, plat, sample_days, VARS))
    rep["A_point_range"] = {"span": [rng_days[0], rng_days[-1]], "n_days": len(rows),
                            "warm_p95_ms": _p95(run_pr, 5),
                            "sampled_days": sample_days, "sampled_parity": a_par}
    if args.api_url:
        try:
            st, hdr, _ = _http_get(f"{args.api_url}/api/ghrsst?lon0={plon}&lat0={plat}"
                                   f"&start={rng_days[0]}&end={rng_days[-1]}&append=sst", timeout=60)
            rep["A_point_range"]["api_status"] = st
            rep["A_point_range"]["api_x_store_route"] = hdr.get("X-Store-Route") or hdr.get("x-store-route")
        except Exception as e:  # noqa: BLE001
            rep["A_point_range"]["api_error"] = str(e)[:200]

    # ---- B. single-day point GET (cube vs daily parity) ----
    d0 = latest
    ref = sa.point_series(plon, plat, [d0], VARS)
    got = cube_point(delta if d0 in delta._day_index else base, d0, plon, plat, VARS)
    rep["B_single_point"] = {"day": d0, "parity": _rows_eq(ref, got),
                             "cube_warm_p95_ms": _p95(lambda: cube_point(delta if d0 in delta._day_index else base, d0, plon, plat, VARS), 9),
                             "daily_warm_p95_ms": _p95(lambda: sa.point_series(plon, plat, [d0], VARS), 9)}

    # ---- C. bbox + POST on EXISTING delta days (read-only cube prototype) ----
    b = _central_bbox(lon, lat, 500, 500)              # ~250k pts
    rng = np.random.default_rng(7)
    pts = [[float(lon[rng.integers(0, lon.size)]), float(lat[rng.integers(0, lat.size)])] for _ in range(1000)]
    for day in delta_days:
        run_bb = lambda day=day: cube_bbox_arrays(delta, day, *b, VARS)
        lons, lats, cols = run_bb()
        ref_cols = sa.bbox_arrays(day, *b, VARS)[2] if sa.day_present(day) else None
        rep["C_bbox"].append({"day": day, "points": lons.size * lats.size,
                              "parity": (_parity_bbox(ref_cols, cols) if ref_cols is not None else None),
                              **chunk_cost(delta._array("sst").chunks, lats.size, lons.size, len(cols)),
                              "warm_p95_ms": _p95(run_bb, 5), "c8_p95_ms": _c8(run_bb)})
        run_po = lambda day=day: cube_points_batch(delta, day, pts, VARS)
        gp = run_po()
        refp = sa.points_batch(pts, day, VARS) if sa.day_present(day) else None
        rep["C_post"].append({"day": day, "n_points": len(gp),
                              "parity": (_rows_eq(refp, gp) if refp is not None else None),
                              "warm_p95_ms": _p95(run_po, 5), "c8_p95_ms": _c8(run_po)})

    # ---- D. policy dry-run (enforcement not yet implemented) ----
    older = base.days[0] if base.days else None        # a historical/base day, not in delta
    sample = [older] + delta_days[:1] if older else delta_days[:1]
    cls = spatial_policy.classify_days(sample, delta_days)
    rep["D_policy_dryrun"] = {"sample_days": sample, **cls,
                              "older_day_rejected": (older is not None and older in cls["rejected"]),
                              "reject_example": (spatial_policy.rejection_payload(older, delta_days) if older else None),
                              "note": "enforcement not yet implemented; validates the DECISION logic (delta membership)"}

    # ---- E. healthz / RSS + current API POST /points read-only smoke ----
    if args.api_url:
        try:
            st, _, body = _http_get(f"{args.api_url}/healthz", timeout=20)
            rep["E_healthz"] = {"status": st, **{k: json.loads(body).get(k) for k in
                                ("cube_kind", "cube_latest", "cube_day_count", "delta_latest", "delta_day_count", "rss_mb")}}
        except Exception as e:  # noqa: BLE001
            rep["E_healthz"] = {"error": str(e)[:200]}
        try:  # current API POST /points is DAILY-served today (policy wiring is P4-S3) — read-only smoke
            st, _, resp = _http_post(f"{args.api_url}/api/ghrsst/points",
                                     {"date": d0, "points": pts[:20], "append": "sst,sst_anomaly,sea_ice"})
            rep["E_api_post_points"] = {"status": st, "n_rows": (len(json.loads(resp)) if st == 200 else None),
                                        "note": "current API POST /points is daily-served today (cube POST validated at "
                                                "store-prototype level in C); policy enforcement is P4-S3"}
        except Exception as e:  # noqa: BLE001
            rep["E_api_post_points"] = {"error": str(e)[:200]}
    rep["E_healthz"]["harness_process_rss_mb"] = round(_PROC.memory_info().rss / 1e6, 1)

    # ---- pass/fail vs absolute budgets ----
    bbox_ok = all(x["warm_p95_ms"] < BUDGET["bbox_warm_p95_ms"] for x in rep["C_bbox"]) if rep["C_bbox"] else None
    post_ok = all(x["warm_p95_ms"] < BUDGET["post_warm_p95_ms"] and x["c8_p95_ms"] < BUDGET["post_c8_p95_ms"]
                  for x in rep["C_post"]) if rep["C_post"] else None
    point_ok = rep["B_single_point"]["cube_warm_p95_ms"] < BUDGET["point_abs_ms"]
    parity_ok = bool(rep["A_point_range"].get("sampled_parity") and
                     rep["B_single_point"]["parity"] and
                     all(x["parity"] in (True, None) for x in rep["C_bbox"]) and
                     all(x["parity"] in (True, None) for x in rep["C_post"]))
    policy_ok = bool(rep["D_policy_dryrun"]["older_day_rejected"])
    perf_ok = bool(bbox_ok and post_ok and point_ok and parity_ok)     # budgets + parity
    rep["pass_fail"] = {"bbox_delta_within_budget": bbox_ok, "post_delta_within_budget": post_ok,
                        "single_point_within_budget": point_ok, "parity_ok": parity_ok,
                        "PERF_overall_per_day_delta": perf_ok,
                        "POLICY_rejects_older": policy_ok,                # spatial-policy decision logic
                        "OVERALL_per_day_delta": bool(perf_ok and policy_ok),  # perf AND policy
                        "NOTE": "per-day delta only; full 31-day retention NOT validated here"}
    print(json.dumps({k: rep[k] for k in ("delta_days", "spatial_window", "A_point_range",
                                          "B_single_point", "pass_fail")}, indent=2, default=str))
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(rep, fh, indent=2, default=str)
        print(f"\nfull artifact -> {args.out}")


def tier_days(base, delta):
    seen = []
    for d in list(base.days) + [d for d in delta.days if d not in set(base.days)]:
        seen.append(d)
    return sorted(set(seen))


def _central_bbox(lon, lat, ni, nj):
    ni = min(ni, lat.size); nj = min(nj, lon.size)
    i0 = (lat.size - ni) // 2; j0 = (lon.size - nj) // 2
    return float(lon[j0]), float(lat[i0]), float(lon[j0 + nj - 1]), float(lat[i0 + ni - 1])


def _rows_eq(a, b):
    if a is None or b is None or len(a) != len(b):
        return a is b or (a is not None and b is not None and len(a) == len(b) == 0)
    for ra, rb in zip(a, b):
        if set(ra) != set(rb):
            return False
        for k in ra:
            va, vb = ra[k], rb[k]
            if isinstance(va, float) or isinstance(vb, float):
                if va is None or vb is None:
                    if va is not vb:
                        return False
                elif np.float32(va) != np.float32(vb) and not (isinstance(va, float) and math.isnan(va) and isinstance(vb, float) and math.isnan(vb)):
                    return False
            elif va != vb:
                return False
    return True


if __name__ == "__main__":
    main()
