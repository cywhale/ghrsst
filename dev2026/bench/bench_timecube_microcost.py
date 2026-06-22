"""dev2026 — P2-S0: per-query micro-cost benchmark (critical-path discriminator).

Reports the H1/H4 metrics (spec phase2_timecube_design.md §3) for a single-point
TIME-SERIES query against a daily-group store — the O(days) baseline that Tier-1
(E1/F) and Tier-2 (time-cube) candidates must beat:

  CACHE-INDEPENDENT (the real architectural signal; derived from chunk geometry):
    - chunk_count_per_query        : # chunks decompressed  (daily store => O(days))
    - decompressed_bytes_per_query : bytes inflated to answer the query
    - useful_bytes                 : days * nfields * 4
    - read_amplification           : decompressed / useful
  MEASURED:
    - latency p50/p95/p99 (repeated), py_alloc peak (tracemalloc), rss delta/peak (psutil)

`meta` records fixture contiguity/composition (reads fixture_meta.json if present),
the longest CONTIGUOUS run, chunk shape, cache_state, and whether the run is
promotion-gate-eligible (real-data Fixture B with >=365 contiguous days). JSON out
uses fields aligned with bench/loadtest.py conventions for cross-run diffing.

Run:
  GHRSST_ZARR_PATH=/path/to/store \
    dev2026/.venv/bin/python dev2026/bench/bench_timecube_microcost.py \
      --lon 119.3 --lat 22.3 --days 365 --out micro.json
  # Fixture B (real store, largest contiguous real span):
  ... bench_timecube_microcost.py --fixture-b --cache-state cold --out micro_b.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tracemalloc
from datetime import date, datetime, timedelta

import numpy as np
import zarr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from store.store_access import StoreAccess  # noqa: E402
from store.zarr_paths import group_path, resolve_zarr_path  # noqa: E402

try:
    import psutil
    _PROC = psutil.Process()
except Exception:
    _PROC = None


def _rss_mb():
    if _PROC is None:
        return None
    return round(_PROC.memory_info().rss / 1e6, 1)


def _iso(d):
    return d.isoformat()


def longest_contiguous(days):
    """Longest run of consecutive calendar days in a sorted ISO list -> (start,end,len)."""
    if not days:
        return (None, None, 0)
    best = (days[0], days[0], 1)
    run_start, run_len = days[0], 1
    prev = datetime.strptime(days[0], "%Y-%m-%d").date()
    for s in days[1:]:
        cur = datetime.strptime(s, "%Y-%m-%d").date()
        if (cur - prev).days == 1:
            run_len += 1
        else:
            run_start, run_len = s, 1
        if run_len > best[2]:
            best = (run_start, s, run_len)
        prev = cur
    return best


def chunk_geometry(store, day, field):
    """(ny, nx, cy, cx) for a day's field array (spatial chunk shape)."""
    g = zarr.open_group(group_path(store, day), mode="r")
    arr = g[field]
    shp, ch = arr.shape, arr.chunks
    ny, nx = (shp[-2], shp[-1])
    cy, cx = (ch[-2], ch[-1])
    return ny, nx, cy, cx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=None, help="store path (or GHRSST_ZARR_PATH)")
    ap.add_argument("--lon", type=float, default=119.30672)
    ap.add_argument("--lat", type=float, default=22.28274)
    ap.add_argument("--days", type=int, default=365, help="use last-N contiguous days ending at latest")
    ap.add_argument("--fields", default="sst,sst_anomaly,sea_ice")
    ap.add_argument("--fixture-b", action="store_true",
                    help="use the largest CONTIGUOUS REAL span in the store (promotion-gate input)")
    ap.add_argument("--cache-state", default="unknown", choices=["cold", "warm", "unknown"])
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--engine", default="baseline", choices=["baseline", "e1"],
                    help="read engine to TIME: baseline=P1 StoreAccess, e1=manifest sidecar "
                         "(cache-independent metrics are identical — geometry-based)")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    store = args.store or resolve_zarr_path()
    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    sa = StoreAccess(store)
    # the engine whose latency we measure (cache-independent metrics use sa/geometry either way)
    if args.engine == "e1":
        from store.e1_manifest import E1ManifestStore
        engine = E1ManifestStore(store)
        engine.build_manifest()
        read_series = engine.point_series
    else:
        read_series = sa.point_series
    all_days = sa.existing_days()
    if not all_days:
        raise SystemExit(f"no day groups under {store}")

    # choose the day span
    if args.fixture_b:
        s0, e0, runlen = longest_contiguous(all_days)
        span_days = [d for d in all_days if s0 <= d <= e0]
    else:
        span_days = all_days[-args.days:]
        s0, e0, runlen = longest_contiguous(span_days)

    existing = [d for d in span_days if sa.day_present(d)]
    fields_present = [f for f in fields if f in zarr.open_group(group_path(store, existing[0]), mode="r")]
    nf = len(fields_present)
    ii, jj, glon, glat = sa.nearest_indices(args.lon, args.lat)

    # ---- cache-independent metrics from chunk geometry (uniform grid across days) ----
    ny, nx, cy, cx = chunk_geometry(store, existing[0], fields_present[0])
    ci, cj = ii // cy, jj // cx
    ext_y = min(cy, ny - ci * cy)
    ext_x = min(cx, nx - cj * cx)
    chunk_bytes = 1 * ext_y * ext_x * 4               # f32 chunk containing the point
    n = len(existing)
    chunk_count = n * nf                               # daily store: 1 chunk/field/day => O(days)
    decompressed_bytes = chunk_count * chunk_bytes
    useful_bytes = n * nf * 4
    read_amp = round(decompressed_bytes / useful_bytes, 1) if useful_bytes else None

    # ---- measured: latency (repeats), py_alloc, rss ----
    lats = []
    rss0 = _rss_mb()
    tracemalloc.start()
    for _ in range(args.repeats):
        t0 = time.perf_counter()
        rows = read_series(args.lon, args.lat, existing, fields_present)
        lats.append(time.perf_counter() - t0)
    py_peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    rss1 = _rss_mb()
    a = np.asarray(lats) * 1000

    # fixture composition (from fixture_meta.json if present, else assume real store)
    fm_path = os.path.join(store, "fixture_meta.json")
    if os.path.isfile(fm_path):
        fixture_meta = json.load(open(fm_path))
    else:
        fixture_meta = {"fixture_kind": "real_store", "composition": {"real": n, "synthetic": 0}}
    real_days = fixture_meta.get("composition", {}).get("real", 0)
    promotion_eligible = bool(args.fixture_b and fixture_meta.get("fixture_kind") == "real_store"
                              and runlen >= 365)

    out = {
        "meta": {
            "store": store, "ts": time.time(), "engine": args.engine,
            "point": {"lon": args.lon, "lat": args.lat, "ii": ii, "jj": jj,
                      "grid_lon": glon, "grid_lat": glat},
            "span_requested_days": len(span_days), "span_existing_days": n,
            "longest_contiguous": {"start": s0, "end": e0, "len": runlen},
            "fields": fields_present, "chunk": [1, cy, cx], "grid": [ny, nx],
            "cache_state": args.cache_state,
            "fixture": fixture_meta,
            "is_fixture_b": bool(args.fixture_b),
            "promotion_gate_eligible": promotion_eligible,
            "promotion_note": ("real-data >=365 contiguous: eligible" if promotion_eligible else
                               "NOT a promotion artifact (synthetic, or <365 contiguous real days, "
                               "or not --fixture-b) -> shadow-only, no cutover (spec §5.1/§7)"),
            "rss_available": _PROC is not None,
        },
        "cache_independent": {
            "chunk_count_per_query": chunk_count,
            "decompressed_bytes_per_query": int(decompressed_bytes),
            "useful_bytes": int(useful_bytes),
            "read_amplification": read_amp,
            "complexity": "O(days) [daily-group baseline]",
        },
        "measured": {
            "latency_ms": {"p50": round(float(np.percentile(a, 50)), 1),
                           "p95": round(float(np.percentile(a, 95)), 1),
                           "p99": round(float(np.percentile(a, 99)), 1),
                           "min": round(float(a.min()), 1)},
            "py_alloc_peak_mb": round(py_peak / 1e6, 1),
            "rss_mb_before": rss0, "rss_mb_after": rss1,
            "repeats": args.repeats, "rows_returned": len(rows),
        },
    }

    ci_ = out["cache_independent"]; me = out["measured"]; mt = out["meta"]
    print(f"store: {store}")
    print(f"span: {n} existing / {len(span_days)} requested; longest contiguous = {runlen} "
          f"({s0}..{e0}); fields={fields_present} chunk={mt['chunk']}")
    print(f"[cache-independent] chunk_count={ci_['chunk_count_per_query']} ({ci_['complexity']})  "
          f"decompressed={ci_['decompressed_bytes_per_query']/1e6:.2f} MB  "
          f"read_amp={ci_['read_amplification']}x")
    print(f"[measured] latency p50={me['latency_ms']['p50']}ms p95={me['latency_ms']['p95']}ms  "
          f"py_alloc_peak={me['py_alloc_peak_mb']}MB  rss {me['rss_mb_before']}->{me['rss_mb_after']}MB  "
          f"cache={mt['cache_state']}")
    print(f"promotion_gate_eligible={mt['promotion_gate_eligible']}  ({mt['promotion_note']})")
    if args.out:
        json.dump(out, open(args.out, "w"), indent=2)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
