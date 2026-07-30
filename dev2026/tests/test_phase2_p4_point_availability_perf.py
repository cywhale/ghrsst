"""dev2026 — P4 point-availability LOCAL performance gates (synthetic, post-prune shape).

Companion to `test_phase2_p4_point_availability.py` (correctness). Here we check the post-fix routing
does not just *work* but stays fast on the path it now uses — the cube — for full-history point/range,
and that the recent paths (range / bbox / POST) are not regressed.

Scope honesty: this is a SYNTHETIC local gate. The grid is small, so absolute latencies are far below
production; what it does pin faithfully is the *chunk geometry* the cube read actually pays —
`(time_chunk=90, spatial 8x8)`, identical to VM24 — plus the real routing + serialization path through
the API. It deliberately does NOT claim VM24 binding performance; that is measured by Codex/ops after
deployment (cf. the P4-S0b binding gate).

Bars (from the fix contract):
  * historical 366-day point range  -> served by the CUBE, p95 < 4 s
  * historical single-day point     -> served by the CUBE, no gross regression (p95 < 1 s)
  * recent range / bbox / POST      -> not regressed (generous absolute bounds; values asserted 200)

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_p4_point_availability_perf.py
"""
from __future__ import annotations

import datetime
import importlib
import os
import shutil
import sys
import tempfile
import time
import unittest

import numpy as np
import zarr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from store.zarr_paths import group_path  # noqa: E402
from ingest.build_timecube_bulk import build_timecube_bulk  # noqa: E402
from ingest.dual_write import append_to_delta  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

_ENV_KEYS = ("GHRSST_ZARR_PATH", "GHRSST_TIMECUBE_PATH", "GHRSST_DELTACUBE_PATH",
             "GHRSST_SPATIAL_WINDOW_ENFORCE")

NY = NX = 32
HISTORY_DAYS = 400              # > 366 so a full-cap range is exercised
DAILY_KEEP = 38                 # post-prune staging window
DELTA_KEEP = 36
REPEATS = 5

RANGE_366_P95_S = 4.0           # contract bar
SINGLE_DAY_P95_S = 1.0          # gross-regression guard
RECENT_P95_S = 2.0              # gross-regression guard for recent paths


def _p95(xs):
    xs = sorted(xs)
    return xs[int(0.95 * (len(xs) - 1))]


def _timeit(fn, repeats=REPEATS):
    fn()                                     # warm
    ts = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        r = fn()
        ts.append(time.perf_counter() - t0)
    return r, _p95(ts)


class PointAvailabilityPerf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._prev = {k: os.environ.get(k) for k in _ENV_KEYS}
        cls.tmp = tempfile.mkdtemp(prefix="p4perf_")
        d0 = datetime.date(2025, 1, 1)
        cls.days = [(d0 + datetime.timedelta(days=i)).isoformat() for i in range(HISTORY_DAYS)]
        full = os.path.join(cls.tmp, "daily_full")
        lon = np.linspace(100.0, 130.0, NX).astype(np.float32)
        lat = np.linspace(0.0, 30.0, NY).astype(np.float32)
        for i, d in enumerate(cls.days):
            g = zarr.open_group(group_path(full, d), mode="w", zarr_format=3)
            g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,)); g["lon"][:] = lon
            g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,)); g["lat"][:] = lat
            for v in ("sst", "sst_anomaly", "sea_ice"):
                g.create_array(v, shape=(1, NY, NX), dtype="float32", chunks=(1, 16, 16))
                g[v][0] = np.full((NY, NX), float(i), np.float32)
        # base cube with the PRODUCTION chunk geometry (t90 / s8 / shard128)
        cls.base = os.path.join(cls.tmp, "base")
        base_end = cls.days[HISTORY_DAYS - DAILY_KEEP + 5]        # base ends before the daily tail
        build_timecube_bulk(full, cls.base, spatial_chunk=8, time_chunk=90, shard_spatial=128,
                            read_block=32, workers=4, end_day=base_end)
        cls.delta = os.path.join(cls.tmp, "delta")
        for d in cls.days[-DELTA_KEEP:]:
            append_to_delta(full, cls.delta, d, spatial_chunk=256, shard_spatial=256)
        cls.daily = os.path.join(cls.tmp, "daily")                # pruned staging
        for d in cls.days[-DAILY_KEEP:]:
            shutil.copytree(group_path(full, d), group_path(cls.daily, d))
        shutil.rmtree(full, ignore_errors=True)

        os.environ.update({"GHRSST_ZARR_PATH": cls.daily, "GHRSST_TIMECUBE_PATH": cls.base,
                           "GHRSST_DELTACUBE_PATH": cls.delta,
                           "GHRSST_SPATIAL_WINDOW_ENFORCE": "1"})
        import api.app as appmod
        cls.appmod = importlib.reload(appmod)
        cls._cm = TestClient(cls.appmod.app)
        cls.client = cls._cm.__enter__()
        cls.report = []

    @classmethod
    def tearDownClass(cls):
        cls._cm.__exit__(None, None, None)
        for k, v in cls._prev.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        import api.app as appmod
        importlib.reload(appmod)
        shutil.rmtree(cls.tmp, ignore_errors=True)
        print("\n--- P4 point-availability local perf (synthetic 32x32, cube chunks (90,8,8)) ---")
        for line in cls.report:
            print("   " + line)

    def _get(self, **params):
        return self.client.get("/api/ghrsst", params=params)

    def test_historical_366_day_range_uses_cube_under_bar(self):
        s, e = self.days[0], self.days[365]                      # exactly 366 days, all cube-only
        r, p95 = _timeit(lambda: self._get(lon0=110.0, lat0=12.0, start=s, end=e,
                                           append="sst,sst_anomaly"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get("X-Store-Route"), "cube")
        self.assertEqual(len(r.json()), 366)
        self.report.append(f"historical 366-day range : p95 {p95*1000:7.1f} ms  (bar {RANGE_366_P95_S}s) route=cube")
        self.assertLess(p95, RANGE_366_P95_S)

    def test_historical_single_day_uses_cube_no_regression(self):
        day = self.days[100]                                     # deep history: cube-only
        r, p95 = _timeit(lambda: self._get(lon0=110.0, lat0=12.0, start=day, end=day, append="sst"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get("X-Store-Route"), "cube")
        self.report.append(f"historical single-day    : p95 {p95*1000:7.1f} ms  (bar {SINGLE_DAY_P95_S}s) route=cube")
        self.assertLess(p95, SINGLE_DAY_P95_S)

    def test_recent_paths_not_regressed(self):
        latest = self.days[-1]
        recent_start = self.days[-28]
        r1, p1 = _timeit(lambda: self._get(lon0=110.0, lat0=12.0, start=recent_start, end=latest,
                                           append="sst"))
        self.assertEqual(r1.status_code, 200, r1.text)
        self.report.append(f"recent 28-day range      : p95 {p1*1000:7.1f} ms  route={r1.headers.get('X-Store-Route')}")
        self.assertLess(p1, RECENT_P95_S)

        r2, p2 = _timeit(lambda: self._get(lon0=110.0, lat0=12.0, lon1=115.0, lat1=17.0,
                                           start=latest, end=latest, append="sst"))
        self.assertEqual(r2.status_code, 200, r2.text)
        self.report.append(f"recent bbox              : p95 {p2*1000:7.1f} ms")
        self.assertLess(p2, RECENT_P95_S)

        body = {"date": latest, "points": [[110.0, 12.0], [111.0, 13.0], [112.0, 14.0]],
                "append": "sst"}
        r3, p3 = _timeit(lambda: self.client.post("/api/ghrsst/points", json=body))
        self.assertEqual(r3.status_code, 200, r3.text)
        self.report.append(f"recent POST /points      : p95 {p3*1000:7.1f} ms")
        self.assertLess(p3, RECENT_P95_S)

    def test_single_day_cube_vs_daily_comparable(self):
        """The routing switch (single-day daily->cube) must not make single-day slower in kind.
        Both are measured directly on the stores to exclude HTTP overhead."""
        from store.time_cube import TimeCubeStore
        from store.store_access import StoreAccess
        day = self.days[-1]                                      # present in BOTH daily and delta
        cube = TimeCubeStore(self.delta)
        daily = StoreAccess(self.daily)
        _, p_cube = _timeit(lambda: cube.point_series(110.0, 12.0, [day], ["sst"]), repeats=9)
        _, p_daily = _timeit(lambda: daily.point_series(110.0, 12.0, [day], ["sst"]), repeats=9)
        self.report.append(f"single-day cube vs daily : cube {p_cube*1000:.2f} ms | daily {p_daily*1000:.2f} ms")
        # generous: cube must not be an order of magnitude worse (VM24 measured cube FASTER: 4.5 vs 14.7 ms)
        self.assertLess(p_cube, max(p_daily * 10.0, 0.5))


if __name__ == "__main__":
    unittest.main(verbosity=2)
