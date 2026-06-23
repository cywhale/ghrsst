"""dev2026 — P2-S2: Candidate F benchmark (bounded-parallel read engine).

Drives FReadEngine (one GLOBAL bounded pool) at profiles {baseline,1,2,4,8} ×
concurrency {1,4,8,16}, recording the review-mandated hard-gate artifact:
  - scheduler_type, worker_count (=parallelism), threads_per_worker (=1)
  - process threads before/peak/after, in_flight_day_chunks peak (== max concurrent
    chunk reads globally; MUST stay <= parallelism regardless of request concurrency)
  - RSS before/peak/after
  - chunk_count / decompressed_bytes / read_amplification (expected STILL O(days))
  - C=1/4/8/16 latency p50/p95/p99 curve
Plus the pinned thread env (OMP/OPENBLAS/MKL/NUMEXPR) so BLAS/numexpr can't open hidden
threads. F is REJECTED if it only helps C=1 but RSS/threads grow with C or LR latency blows.

IMPORTANT: run with the thread env pinned, e.g.
  OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 \
  GHRSST_ZARR_PATH=... dev2026/.venv/bin/python dev2026/bench/bench_f_engine.py \
    --fixture-b --duration 6 --out f.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import threading
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from store.store_access import StoreAccess  # noqa: E402
from store.f_engine import FReadEngine  # noqa: E402
from bench_timecube_microcost import chunk_geometry, longest_contiguous  # noqa: E402

try:
    import psutil
    _PROC = psutil.Process()
except Exception:
    _PROC = None

THREAD_ENV = ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS")


def _threads():
    return _PROC.num_threads() if _PROC else threading.active_count()


def _rss():
    return round(_PROC.memory_info().rss / 1e6, 1) if _PROC else None


def _rand_pt():
    return (round(random.uniform(100.5, 129.5), 4), round(random.uniform(0.5, 29.5), 4))


def _pcts(xs):
    if not xs:
        return {"p50": None, "p95": None, "p99": None}
    a = np.asarray(xs) * 1000
    return {"p50": round(float(np.percentile(a, 50)), 1),
            "p95": round(float(np.percentile(a, 95)), 1),
            "p99": round(float(np.percentile(a, 99)), 1)}


def run_cell(read_fn, span_days, conc, duration, engine):
    """Run `conc` client threads doing read_fn over span_days for `duration`s; sample
    process threads + RSS peak. Returns metrics for this (profile, C) cell."""
    if engine is not None:
        engine.reset_inflight_peak()
    lats, stop = [], time.monotonic() + duration
    lat_lock = threading.Lock()
    peak = {"threads": _threads(), "rss": _rss() or 0}
    sampler_stop = threading.Event()

    def sampler():
        while not sampler_stop.is_set():
            peak["threads"] = max(peak["threads"], _threads())
            r = _rss()
            if r:
                peak["rss"] = max(peak["rss"], r)
            time.sleep(0.05)

    def worker():
        local = []
        while time.monotonic() < stop:
            lon, lat = _rand_pt()
            t0 = time.perf_counter()
            read_fn(lon, lat, span_days, ["sst", "sea_ice"])
            local.append(time.perf_counter() - t0)
        with lat_lock:
            lats.extend(local)

    t_before, r_before = _threads(), _rss()
    s = threading.Thread(target=sampler, daemon=True)
    s.start()
    ts = [threading.Thread(target=worker) for _ in range(conc)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    sampler_stop.set()
    s.join()
    return {"concurrency": conc, "requests": len(lats), "latency_ms": _pcts(lats),
            "threads_before": t_before, "threads_peak": peak["threads"], "threads_after": _threads(),
            "rss_mb_before": r_before, "rss_mb_peak": peak["rss"], "rss_mb_after": _rss(),
            "in_flight_chunks_peak": (engine.in_flight_peak if engine is not None else 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=None)
    ap.add_argument("--fixture-b", action="store_true")
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--profiles", default="baseline,1,2,4,8")
    ap.add_argument("--concurrency", default="1,4,8,16")
    ap.add_argument("--duration", type=float, default=6.0)
    ap.add_argument("--cache-state", default="unknown", choices=["cold", "warm", "unknown"])
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    store = args.store or os.environ.get("GHRSST_ZARR_PATH")
    if not store:
        raise SystemExit("set --store or GHRSST_ZARR_PATH")
    sa = StoreAccess(store)
    all_days = sa.existing_days()
    if args.fixture_b:
        s0, e0, runlen = longest_contiguous(all_days)
        span = [d for d in all_days if s0 <= d <= e0]
    else:
        span = all_days[-args.days:]
        s0, e0, runlen = longest_contiguous(span)
    existing = [d for d in span if sa.day_present(d)]
    nf = 2  # sst, sea_ice
    ny, nx, cy, cx = chunk_geometry(store, existing[0], "sst")
    chunk_count = len(existing) * nf
    decompressed = chunk_count * (min(cy, ny) * min(cx, nx)) * 4  # approx (point in interior chunk)

    env = {k: os.environ.get(k) for k in THREAD_ENV}
    env_pinned = all(env[k] == "1" for k in THREAD_ENV)

    profiles = [p.strip() for p in args.profiles.split(",")]
    concs = [int(c) for c in args.concurrency.split(",")]
    results = []
    for prof in profiles:
        if prof == "baseline":
            eng, read_fn = None, sa.point_series
            worker_count = 1
        else:
            eng = FReadEngine(store, parallelism=int(prof))
            read_fn, worker_count = eng.point_series, int(prof)
        # warm
        read_fn(119.3, 22.3, existing[: min(5, len(existing))], ["sst"])
        cells = [run_cell(read_fn, existing, c, args.duration, eng) for c in concs]
        if eng is not None:
            eng.shutdown()
        results.append({"profile": prof, "scheduler_type": "bounded_threadpool" if eng else "sequential",
                        "worker_count": worker_count, "threads_per_worker": 1, "cells": cells})
        for c in cells:
            print(f"  prof={prof:>8} C={c['concurrency']:>2}  p50={c['latency_ms']['p50']}ms "
                  f"p95={c['latency_ms']['p95']}ms  thr_peak={c['threads_peak']} "
                  f"inflight_peak={c['in_flight_chunks_peak']} rss_peak={c['rss_mb_peak']}MB")

    out = {"meta": {"store": store, "ts": time.time(), "cache_state": args.cache_state,
                    "span_days": len(existing), "longest_contiguous": runlen,
                    "promotion_gate_eligible": False,
                    "promotion_note": "F bench: architecture signal only; not promotable "
                                      "(local <365 contiguous real days). " +
                                      ("thread-env PINNED ok" if env_pinned else "WARNING: thread-env NOT pinned to 1"),
                    "thread_env": env, "thread_env_pinned": env_pinned,
                    "chunk": [1, cy, cx]},
           "cache_independent": {"chunk_count_per_query": chunk_count,
                                 "decompressed_bytes_per_query": int(decompressed),
                                 "read_amplification": round(decompressed / (len(existing) * nf * 4), 1),
                                 "complexity": "O(days) [F parallelizes but does NOT remove it]"},
           "profiles": results}
    if not env_pinned:
        print("WARNING: thread env not pinned to 1 -> BLAS/numexpr may add hidden threads. "
              "Re-run with OMP/OPENBLAS/MKL/NUMEXPR_NUM_THREADS=1.")
    if args.out:
        json.dump(out, open(args.out, "w"), indent=2)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
