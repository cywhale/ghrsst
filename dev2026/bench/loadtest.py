"""dev2026 — P1-S4 HTTP load harness (scenarios SB / LR / BBOX / OL).

Drives a RUNNING new-app instance (uvicorn/gunicorn) over real HTTP and records
p50/p95/p99 latency, timeout/error/shed(503) counts, throughput, and a /healthz
time series (executor queue depth + worker RSS). Machine-readable JSON output so a
local run and a VM24 run can be diffed.

  * SB   sustained burst: 80% point (random 1-60d range) + 20% batch points (16 pts)
  * LR   concurrent long-range: 365-day point series at C in {4,8,16}
  * BBOX large single-day bbox (memory hotspot)
  * OL   overload: concurrency >> capacity -> expect fast 503 + Retry-After, recovery

  >>> LOCAL RESULTS ARE NON-BINDING: harness validation + relative/mechanics evidence
  >>> only. The authoritative G1'/G6/G7 gate is this same tool run on VM24 against the
  >>> full store with the spec params (SB C=32/T=120s, LR C=4/8/16, BBOX ~POINT_LIMIT).

No hardcoded production host. --base (or GHRSST_LOADTEST_BASE); defaults to the
local dev port 127.0.0.1:8036.

Run (start the app first, then):
  GHRSST_ZARR_PATH=$(pwd)/bak/data/mur.zarr dev2026/.venv/bin/uvicorn api.app:app --port 8036 &
  dev2026/.venv/bin/python dev2026/bench/loadtest.py --scenario all --duration 15 --out /tmp/lt.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone

import httpx
import numpy as np


def _pcts(xs):
    if not xs:
        return {"p50": None, "p95": None, "p99": None, "max": None}
    a = np.asarray(xs) * 1000.0  # ms
    return {"p50": round(float(np.percentile(a, 50)), 1),
            "p95": round(float(np.percentile(a, 95)), 1),
            "p99": round(float(np.percentile(a, 99)), 1),
            "max": round(float(a.max()), 1)}


class Stats:
    def __init__(self):
        self.lat = []          # successful latencies (s)
        self.ok = 0
        self.shed = 0          # 503
        self.errors = 0        # other non-2xx
        self.timeouts = 0

    def record(self, dt, status):
        if status == 503:
            self.shed += 1
        elif 200 <= status < 300:
            self.ok += 1
            self.lat.append(dt)
        else:
            self.errors += 1

    def summary(self, wall):
        total = self.ok + self.shed + self.errors + self.timeouts
        return {"requests": total, "ok": self.ok, "shed_503": self.shed,
                "errors": self.errors, "timeouts": self.timeouts,
                "throughput_rps": round(total / wall, 1) if wall else None,
                "latency_ms": _pcts(self.lat)}


class Bounds:
    def __init__(self, earliest, latest, dense_days=300):
        self.earliest = earliest
        self.latest = latest
        self._e = datetime.strptime(earliest, "%Y-%m-%d").date()
        self._l = datetime.strptime(latest, "%Y-%m-%d").date()
        # sample within the DENSE recent window [latest-dense_days, latest] so a
        # sparse local store (big gaps before the recent contiguous region) doesn't
        # produce spurious 400s. On a contiguous full store this is just the tail.
        self._ds = max(self._e, self._l - timedelta(days=dense_days - 1))
        self.span = (self._l - self._ds).days

    def random_day(self):
        return (self._ds + timedelta(days=random.randint(0, self.span))).isoformat()

    def random_range(self, max_len=60):
        n = random.randint(1, max_len)
        off = random.randint(0, max(0, self.span - n))
        s = self._ds + timedelta(days=off)
        e = s + timedelta(days=n - 1)
        return s.isoformat(), e.isoformat()

    def last_n(self, n):
        s = max(self._e, self._l - timedelta(days=n - 1))
        return s.isoformat(), self.latest


def _rand_pt():
    return (round(random.uniform(-179, 179), 4), round(random.uniform(-80, 80), 4))


# ---- request generators (return (method, path, params, json_body)) -------
def gen_sb(b: Bounds, bbox_deg):
    if random.random() < 0.8:
        lon, lat = _rand_pt()
        s, e = b.random_range(60)
        return ("GET", "/api/ghrsst", {"lon0": lon, "lat0": lat, "start": s, "end": e, "append": "sst"}, None)
    pts = [list(_rand_pt()) for _ in range(16)]
    return ("POST", "/api/ghrsst/points", None, {"date": b.random_day(), "points": pts, "append": "sst"})


def gen_lr(b: Bounds, bbox_deg):
    lon, lat = _rand_pt()
    s, e = b.last_n(365)
    return ("GET", "/api/ghrsst", {"lon0": lon, "lat0": lat, "start": s, "end": e, "append": "sst,sea_ice"}, None)


def gen_bbox(b: Bounds, bbox_deg):
    lon0 = round(random.uniform(-170, 170 - bbox_deg), 3)
    lat0 = round(random.uniform(-70, 70 - bbox_deg), 3)
    return ("GET", "/api/ghrsst", {"lon0": lon0, "lat0": lat0, "lon1": lon0 + bbox_deg,
            "lat1": lat0 + bbox_deg, "start": b.latest, "append": "sst"}, None)


GENERATORS = {"SB": gen_sb, "LR": gen_lr, "BBOX": gen_bbox, "OL": gen_lr}


async def _sampler(base, stop_t, series, interval=1.0):
    # dedicated client so heavy load on the worker pool can't starve sampling
    async with httpx.AsyncClient(base_url=base, timeout=5) as sc:
        while time.monotonic() < stop_t:
            try:
                h = (await sc.get("/healthz")).json()
                series.append({"t": round(time.monotonic(), 2),
                               "queue_depth": h.get("executor_queue_depth"),
                               "rss_mb": h.get("rss_mb")})
            except Exception:
                pass
            await asyncio.sleep(interval)


async def _worker(client, stop_t, gen, b, bbox_deg, stats, req_timeout):
    while time.monotonic() < stop_t:
        method, path, params, body = gen(b, bbox_deg)
        t0 = time.perf_counter()
        try:
            if method == "GET":
                r = await client.get(path, params=params, timeout=req_timeout)
            else:
                r = await client.post(path, json=body, timeout=req_timeout)
            _ = r.content  # ensure full body read (captures streaming completion)
            stats.record(time.perf_counter() - t0, r.status_code)
        except httpx.TimeoutException:
            stats.timeouts += 1
        except Exception:
            stats.errors += 1


async def run_scenario(base, name, concurrency, duration, bbox_deg, req_timeout, dense_days):
    gen = GENERATORS[name]
    stats = Stats()
    series = []
    limits = httpx.Limits(max_connections=concurrency + 8, max_keepalive_connections=concurrency + 8)
    async with httpx.AsyncClient(base_url=base, limits=limits) as client:
        b0 = (await client.get("/healthz", timeout=10)).json()
        b = Bounds(b0["earliest"], b0["latest"], dense_days)
        stop_t = time.monotonic() + duration
        wall0 = time.monotonic()
        tasks = [asyncio.create_task(_worker(client, stop_t, gen, b, bbox_deg, stats, req_timeout))
                 for _ in range(concurrency)]
        sampler = asyncio.create_task(_sampler(base, stop_t, series))
        await asyncio.gather(*tasks)
        await sampler
        wall = time.monotonic() - wall0
        recovery_ms = None
        if name == "OL":  # probe recovery latency after the storm
            await asyncio.sleep(1.0)
            lon, lat = _rand_pt()
            t0 = time.perf_counter()
            rr = await client.get("/api/ghrsst", params={"lon0": lon, "lat0": lat, "append": "sst"},
                                  timeout=req_timeout)
            if rr.status_code < 500:
                recovery_ms = round((time.perf_counter() - t0) * 1000, 1)
    qd = [s["queue_depth"] for s in series if s.get("queue_depth") is not None]
    rss = [s["rss_mb"] for s in series if s.get("rss_mb") is not None]
    res = {"scenario": name, "concurrency": concurrency, "duration_s": duration,
           **stats.summary(wall),
           "queue_depth_max": max(qd) if qd else None,
           "rss_mb_max": max(rss) if rss else None,
           "rss_mb_last": rss[-1] if rss else None,
           "recovery_ms": recovery_ms,
           "healthz_series": series}
    return res


SCENARIO_CONC = {"SB": [32], "LR": [4, 8, 16], "BBOX": [4], "OL": [64]}


async def main_async(args):
    scenarios = (["SB", "LR", "BBOX", "OL"] if args.scenario == "all"
                 else [args.scenario.upper()])
    results = []
    for name in scenarios:
        concs = [args.concurrency] if args.concurrency else SCENARIO_CONC[name]
        for c in concs:
            print(f"  running {name} @ C={c} for {args.duration}s ...", flush=True)
            results.append(await run_scenario(args.base, name, c, args.duration,
                                              args.bbox_deg, args.req_timeout, args.dense_days))
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("GHRSST_LOADTEST_BASE", "http://127.0.0.1:8036"),
                    help="base URL of a RUNNING new app (no hardcoded prod host)")
    ap.add_argument("--scenario", default="all", help="SB|LR|BBOX|OL|all")
    ap.add_argument("--duration", type=int, default=15, help="seconds per (scenario,C)")
    ap.add_argument("--concurrency", type=int, default=None, help="override scenario default C")
    ap.add_argument("--bbox-deg", type=float, default=5.0, dest="bbox_deg",
                    help="bbox edge in degrees for BBOX/OL (crank toward POINT_LIMIT on VM24)")
    ap.add_argument("--req-timeout", type=float, default=60.0, dest="req_timeout")
    ap.add_argument("--dense-days", type=int, default=300, dest="dense_days",
                    help="sample days within the last N days (avoids 400s on a sparse local store)")
    ap.add_argument("--out", default=None, help="write machine-readable JSON here")
    args = ap.parse_args()

    print("=" * 78)
    print(f"P1-S4 load harness -> {args.base}")
    print(">>> NON-BINDING if run locally: harness validation + relative/mechanics only.")
    print(">>> AUTHORITATIVE G1'/G6/G7 gate = this tool on VM24 + full store + spec params.")
    print("=" * 78)

    results = asyncio.run(main_async(args))

    # console summary (drop the verbose series)
    print("\n%-5s %4s %7s %6s %6s %6s %7s %7s %7s %8s %8s %9s" %
          ("scen", "C", "req", "ok", "503", "err", "p50ms", "p95ms", "p99ms", "rssMax", "qDepth", "recovMs"))
    for r in results:
        L = r["latency_ms"]
        print("%-5s %4s %7d %6d %6d %6d %7s %7s %7s %8s %8s %9s" % (
            r["scenario"], r["concurrency"], r["requests"], r["ok"], r["shed_503"],
            r["errors"] + r["timeouts"], L["p50"], L["p95"], L["p99"],
            r["rss_mb_max"], r["queue_depth_max"], r["recovery_ms"]))

    out = {"meta": {"base": args.base, "ts": time.time(),
                    "iso": datetime.now(timezone.utc).isoformat(),
                    "duration_s": args.duration, "bbox_deg": args.bbox_deg,
                    "binding": False,
                    "note": "LOCAL run is non-binding; VM24 full-store run is the release gate."},
           "results": results}
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(out, fh, indent=2)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
