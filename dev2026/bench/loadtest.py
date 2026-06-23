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
    """Event-based so we can compute time-windowed metrics (G2' stability gate)."""
    def __init__(self):
        self.events = []       # (t_rel_s, dt_s_or_None, kind) kind in ok|shed|err|timeout
        self.route_counts = {}  # X-Store-Route tally (confirms multi-day -> cube under load)

    def record(self, t_rel, dt, status):
        if status == 503:
            self.events.append((t_rel, None, "shed"))
        elif 200 <= status < 300:
            self.events.append((t_rel, dt, "ok"))
        else:
            self.events.append((t_rel, None, "err"))

    def timeout(self, t_rel):
        self.events.append((t_rel, None, "timeout"))

    @staticmethod
    def _bucket_metrics(evs, window_s):
        lat = [dt for _, dt, k in evs if k == "ok" and dt is not None]
        n = len(evs)
        return {"n": n,
                "ok": sum(k == "ok" for *_, k in evs),
                "shed_503": sum(k == "shed" for *_, k in evs),
                "errors": sum(k == "err" for *_, k in evs),
                "timeouts": sum(k == "timeout" for *_, k in evs),
                "rps": round(n / window_s, 1),
                "latency_ms": _pcts(lat)}

    def summary(self, wall):
        ok = sum(k == "ok" for *_, k in self.events)
        shed = sum(k == "shed" for *_, k in self.events)
        err = sum(k == "err" for *_, k in self.events)
        to = sum(k == "timeout" for *_, k in self.events)
        lat = [dt for _, dt, k in self.events if k == "ok" and dt is not None]
        total = len(self.events)
        return {"requests": total, "ok": ok, "shed_503": shed, "errors": err, "timeouts": to,
                "throughput_rps": round(total / wall, 1) if wall else None,
                "latency_ms": _pcts(lat), "route_counts": dict(self.route_counts)}

    def windowed(self, window_s, warmup_s):
        """10s (default) rolling buckets + first-vs-last stability summary for G2'."""
        buckets = {}
        max_t = 0.0
        for t_rel, dt, kind in self.events:
            buckets.setdefault(int(t_rel // window_s), []).append((t_rel, dt, kind))
            max_t = max(max_t, t_rel)
        wins = []
        for b in sorted(buckets):
            m = self._bucket_metrics(buckets[b], window_s)
            m["bucket"] = b
            m["t0_s"] = b * window_s
            # a trailing bucket the run ended inside is "partial": its rps is not
            # comparable (covers < window_s), so exclude it from the stability gate.
            m["partial"] = (b * window_s + window_s) > (max_t + 1e-3)
            wins.append(m)
        # stability: first vs last FULL bucket after warmup that has ok samples
        post = [w for w in wins if w["t0_s"] >= warmup_s and w["ok"] > 0
                and not w["partial"] and w["latency_ms"]["p95"] is not None]
        stab = None
        if len(post) >= 2:
            f, l = post[0], post[-1]
            fp, lp = f["latency_ms"]["p95"], l["latency_ms"]["p95"]
            stab = {"first_bucket_t0_s": f["t0_s"], "last_bucket_t0_s": l["t0_s"],
                    "first_p95_ms": fp, "last_p95_ms": lp,
                    "p95_ratio": round(lp / fp, 2) if fp else None,
                    "tail_drift_ok": (lp <= 1.2 * fp) if (fp and lp) else None,
                    "first_rps": f["rps"], "last_rps": l["rps"],
                    "rps_within_10pct": (abs(l["rps"] - f["rps"]) <= 0.1 * f["rps"]) if f["rps"] else None}
        return {"window_s": window_s, "warmup_s": warmup_s, "windows": wins, "stability": stab}


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


# ---- request generators: (b, opts) -> (method, path, params, json_body) ---
# opts has .bbox_deg and .sb_range_max (max day-span for SB point requests; set 1
# for short single-day SB to measure the G2 latency SLO; >1 for range-heavy SB
# whose gate is G2' STABILITY, not an absolute p99).
def gen_sb(b: Bounds, opts):
    if random.random() < 0.8:
        lon, lat = _rand_pt()
        s, e = b.random_range(opts.sb_range_max)
        return ("GET", "/api/ghrsst", {"lon0": lon, "lat0": lat, "start": s, "end": e, "append": "sst"}, None)
    pts = [list(_rand_pt()) for _ in range(16)]
    return ("POST", "/api/ghrsst/points", None, {"date": b.random_day(), "points": pts, "append": "sst"})


def gen_lr(b: Bounds, opts):
    lon, lat = _rand_pt()
    s, e = b.last_n(365)
    return ("GET", "/api/ghrsst", {"lon0": lon, "lat0": lat, "start": s, "end": e, "append": "sst,sea_ice"}, None)


def gen_bbox(b: Bounds, opts):
    d = opts.bbox_deg
    lon0 = round(random.uniform(-170, 170 - d), 3)
    lat0 = round(random.uniform(-70, 70 - d), 3)
    return ("GET", "/api/ghrsst", {"lon0": lon0, "lat0": lat0, "lon1": lon0 + d,
            "lat1": lat0 + d, "start": b.latest, "append": "sst"}, None)


GENERATORS = {"SB": gen_sb, "LR": gen_lr, "BBOX": gen_bbox, "OL": gen_lr}


async def _sampler(base, stop_t, series, interval=1.0):
    # dedicated client so heavy load on the worker pool can't starve sampling
    async with httpx.AsyncClient(base_url=base, timeout=5) as sc:
        while time.monotonic() < stop_t:
            try:
                h = (await sc.get("/healthz")).json()
                series.append({"t": round(time.monotonic(), 2),
                               "pid": h.get("pid"),                       # worker identity
                               "executor_limit": h.get("executor_limit"),
                               "queue_depth": h.get("executor_queue_depth"),
                               "rss_mb": h.get("rss_mb")})
            except Exception:
                pass
            await asyncio.sleep(interval)


async def _worker(client, stop_t, start_perf, gen, b, opts, stats, req_timeout):
    while time.monotonic() < stop_t:
        method, path, params, body = gen(b, opts)
        t0 = time.perf_counter()
        try:
            if method == "GET":
                r = await client.get(path, params=params, timeout=req_timeout)
            else:
                r = await client.post(path, json=body, timeout=req_timeout)
            _ = r.content  # ensure full body read (captures streaming completion)
            now = time.perf_counter()
            stats.record(now - start_perf, now - t0, r.status_code)  # (t_rel, latency, status)
            route = r.headers.get("x-store-route")
            if route:
                stats.route_counts[route] = stats.route_counts.get(route, 0) + 1
        except httpx.TimeoutException:
            stats.timeout(time.perf_counter() - start_perf)
        except Exception:
            stats.events.append((time.perf_counter() - start_perf, None, "err"))


async def run_scenario(base, name, concurrency, duration, bbox_deg, req_timeout, dense_days,
                       sb_range_max, window_s, warmup_s):
    from types import SimpleNamespace
    gen = GENERATORS[name]
    opts = SimpleNamespace(bbox_deg=bbox_deg, sb_range_max=sb_range_max)
    stats = Stats()
    series = []
    limits = httpx.Limits(max_connections=concurrency + 8, max_keepalive_connections=concurrency + 8)
    async with httpx.AsyncClient(base_url=base, limits=limits) as client:
        b0 = (await client.get("/healthz", timeout=10)).json()
        b = Bounds(b0["earliest"], b0["latest"], dense_days)
        stop_t = time.monotonic() + duration
        wall0 = time.monotonic()
        start_perf = time.perf_counter()
        tasks = [asyncio.create_task(_worker(client, stop_t, start_perf, gen, b, opts, stats, req_timeout))
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
    # per-pid summary: /healthz is a SINGLE-worker view; on gunicorn -w N samples
    # land on different workers. Group by pid so RSS/queue are interpreted per worker
    # (NOT summed — total RSS must be collected externally over SSH, e.g. ps).
    by_pid = {}
    for s in series:
        pid = s.get("pid")
        if pid is None:
            continue
        d = by_pid.setdefault(str(pid), {"samples": 0, "rss_mb_max": None,
                                         "queue_depth_max": None, "executor_limit": s.get("executor_limit")})
        d["samples"] += 1
        if s.get("rss_mb") is not None:
            d["rss_mb_max"] = s["rss_mb"] if d["rss_mb_max"] is None else max(d["rss_mb_max"], s["rss_mb"])
        if s.get("queue_depth") is not None:
            d["queue_depth_max"] = s["queue_depth"] if d["queue_depth_max"] is None else max(d["queue_depth_max"], s["queue_depth"])
    res = {"scenario": name, "concurrency": concurrency, "duration_s": duration,
           **stats.summary(wall),
           "windowed": stats.windowed(window_s, warmup_s),   # G2' stability source
           "queue_depth_max": max(qd) if qd else None,
           "rss_mb_max": max(rss) if rss else None,     # max across sampled workers (NOT a total)
           "rss_mb_last": rss[-1] if rss else None,
           "workers_sampled": len(by_pid),
           "by_pid": by_pid,
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
                                              args.bbox_deg, args.req_timeout, args.dense_days,
                                              args.sb_range_max, args.window_s, args.warmup_s))
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
    ap.add_argument("--sb-range-max", type=int, default=60, dest="sb_range_max",
                    help="SB point-request max day span. Use 1 for short single-day SB (G2 "
                         "latency SLO); >1 for range-heavy SB (gate = G2' stability, not p99).")
    ap.add_argument("--req-timeout", type=float, default=60.0, dest="req_timeout")
    ap.add_argument("--window-s", type=int, default=10, dest="window_s", help="metrics bucket size (s)")
    ap.add_argument("--warmup-s", type=int, default=10, dest="warmup_s",
                    help="exclude buckets before this from the first-vs-last stability summary")
    ap.add_argument("--dense-days", type=int, default=300, dest="dense_days",
                    help="sample days within the last N days (avoids 400s on a sparse local store)")
    ap.add_argument("--run-kind", choices=["local", "vm24"], default="local", dest="run_kind",
                    help="local = non-binding harness validation; vm24 = authoritative gate (binding=true)")
    ap.add_argument("--binding", dest="binding", action="store_true", default=None,
                    help="force meta.binding=true (overrides --run-kind)")
    ap.add_argument("--out", default=None, help="write machine-readable JSON here")
    args = ap.parse_args()

    binding = args.binding if args.binding is not None else (args.run_kind == "vm24")
    print("=" * 78)
    print(f"P1-S4 load harness -> {args.base}  [run-kind={args.run_kind} binding={binding}]")
    if binding:
        print(">>> BINDING run: this output is treated as an authoritative G1'/G6/G7 gate.")
    else:
        print(">>> NON-BINDING: harness validation + relative/mechanics only.")
        print(">>> AUTHORITATIVE gate = this tool with --run-kind vm24 on VM24 + full store.")
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
                    "run_kind": args.run_kind, "binding": binding,
                    "note": ("BINDING VM24 gate run." if binding else
                             "LOCAL run is non-binding; rerun with --run-kind vm24 on VM24 as the gate.")},
           "results": results}
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(out, fh, indent=2)
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
