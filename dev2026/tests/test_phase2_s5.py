"""dev2026 — P2-S5: hybrid router routing decisions + per-route parity.

Store level: HybridRouter routes multi-day point/range to the cube, single-day/bbox/batch to
the daily P1 store; every route must return identical values (routing is perf-only).
API level: the GET point path routes via the router and exposes X-Store-Route; routed output
matches the P1/daily result.

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_s5.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

import numpy as np
import zarr

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from store.store_access import StoreAccess  # noqa: E402
from store.time_cube import TimeCubeStore  # noqa: E402
from store.hybrid_router import HybridRouter  # noqa: E402
from store.zarr_paths import group_path  # noqa: E402
from ingest.build_timecube import build_timecube  # noqa: E402

NY, NX = 24, 32


def build_day(root, day, with_anomaly=True):
    g = zarr.open_group(group_path(root, day), mode="w", zarr_format=3)
    g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,))
    g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,))
    g["lon"][:] = np.linspace(100.0, 130.0, NX).astype(np.float32)
    g["lat"][:] = np.linspace(0.0, 30.0, NY).astype(np.float32)
    sst = (10 + 0.01 * np.arange(NY)[:, None] + 0.001 * np.arange(NX)[None, :]).astype(np.float32)
    sst[2, 3] = np.nan
    g.create_array("sst", shape=(1, NY, NX), dtype="float32", chunks=(1, NY, NX))
    g["sst"][0] = sst
    g.create_array("sea_ice", shape=(1, NY, NX), dtype="float32", chunks=(1, NY, NX))
    g["sea_ice"][0] = np.zeros((NY, NX), np.float32)
    if with_anomaly:
        g.create_array("sst_anomaly", shape=(1, NY, NX), dtype="float32", chunks=(1, NY, NX))
        g["sst_anomaly"][0] = (0.1 * np.arange(NY)[:, None] * np.ones((1, NX))).astype(np.float32)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p2s5_")
        self.daily = os.path.join(self.tmp, "daily")
        self.days = [f"2024-09-{d:02d}" for d in range(1, 11)]   # 10 days
        # mixed presence to exercise absent-field through the cube route
        self.present = [i % 3 != 0 for i in range(len(self.days))]
        for i, d in enumerate(self.days):
            build_day(self.daily, d, with_anomaly=self.present[i])
        self.cube = os.path.join(self.tmp, "cube")
        build_timecube(self.daily, self.cube, spatial_chunk=8, time_chunk=4, shard_spatial=16)
        self.sa = StoreAccess(self.daily, day_ttl_seconds=0.0)
        self.router = HybridRouter(self.sa, TimeCubeStore(self.cube))
        self.nocube = HybridRouter(self.sa, None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- routing decisions ----
    # P4 CONTRACT CHANGE: single-day point now routes to the CUBE when the cube covers that day.
    # Pre-P4 it was pinned to daily (daily was full-history, so it was a free choice). After the P4
    # daily-staging prune daily holds only a recent window, so pinning single-day to daily made
    # historical single-day points unserveable (production 400s). Cube single-day is also faster
    # (P4-S0b VM24: 4.5 ms cube vs 14.7 ms daily). Values are identical either way — asserted here
    # and in test_parity_single_day_via_cube below.
    def test_route_default_latest_single_day(self):
        self.assertEqual(self.router.route_point([self.days[-1]]), "cube")

    def test_route_fixed_single_day(self):
        self.assertEqual(self.router.route_point([self.days[3]]), "cube")

    def test_route_single_day_not_in_cube_falls_back_to_daily(self):
        # a day the cube lacks must still route to daily (transition safety: fresh ingest)
        partial = os.path.join(self.tmp, "partial_cube_single")
        build_timecube(self.daily, partial, spatial_chunk=8, time_chunk=4, days=self.days[:5])
        rp = HybridRouter(self.sa, TimeCubeStore(partial))
        self.assertEqual(rp.route_point([self.days[2]]), "cube")      # covered
        self.assertEqual(rp.route_point([self.days[7]]), "daily")     # not in cube -> daily

    def test_route_multi_day_range(self):
        self.assertEqual(self.router.route_point(self.days[2:8]), "cube")

    def test_route_no_cube_is_daily(self):
        self.assertEqual(self.nocube.route_point(self.days[2:8]), "daily")

    def test_route_uncovered_falls_back_to_daily(self):
        # cube covering only days[:7]; a range incl. day 8 (in daily, NOT in cube) -> daily
        partial = os.path.join(self.tmp, "partial_cube")
        build_timecube(self.daily, partial, spatial_chunk=8, time_chunk=4, days=self.days[:7])
        rp = HybridRouter(self.sa, TimeCubeStore(partial))
        self.assertEqual(rp.route_point(self.days[2:6]), "cube")      # all covered
        self.assertEqual(rp.route_point(self.days[5:9]), "daily")     # 7,8 not in cube -> fallback

    # ---- per-route parity (routed == P1) ----
    def test_parity_multi_day_via_cube(self):
        for lon, lat in [(119.3, 22.3), (100.0, 0.0), (104.5, 7.0)]:
            p1 = self.sa.point_series(lon, lat, self.days, ["sst", "sst_anomaly", "sea_ice"])
            routed = self.router.point_series(lon, lat, self.days, ["sst", "sst_anomaly", "sea_ice"])
            self.assertEqual(routed, p1, f"routed multi-day != P1 at ({lon},{lat})")

    def test_parity_single_day_via_cube(self):
        # P4: single-day routes to the cube — the routed result must still equal the P1 daily read
        # EXACTLY (authoritative semantics unchanged by the routing switch).
        for day in (self.days[2], self.days[3], self.days[-1]):
            r = self.router.point_series(115.0, 12.0, [day], ["sst", "sst_anomaly", "sea_ice"])
            self.assertEqual(r, self.sa.point_series(115.0, 12.0, [day],
                                                     ["sst", "sst_anomaly", "sea_ice"]), day)

    def test_parity_batch_and_bbox_route_daily(self):
        pts = [[110.0, 10.0], [120.0, 20.0]]
        self.assertEqual(self.router.points_batch(pts, self.days[0], ["sst"]),
                         self.sa.points_batch(pts, self.days[0], ["sst"]))
        rb = [r for b in self.router.bbox_batches(self.days[0], 110, 10, 112, 12, ["sst"]) for r in b]
        db = [r for b in self.sa.bbox_batches(self.days[0], 110, 10, 112, 12, ["sst"]) for r in b]
        self.assertEqual(rb, db)

    def test_absent_field_preserved_through_cube_route(self):
        # multi-day routes to cube; absent-on-some-days anomaly must still omit per P1
        p1 = self.sa.point_series(115.0, 12.0, self.days, ["sst", "sst_anomaly"])
        routed = self.router.point_series(115.0, 12.0, self.days, ["sst", "sst_anomaly"])
        self.assertEqual(routed, p1)
        self.assertEqual([("sst_anomaly" in r) for r in routed], self.present)


# build daily + cube and point the app at both BEFORE constructing the client
_TMP = tempfile.mkdtemp(prefix="p2s5api_")
_DAILY = os.path.join(_TMP, "daily")
_DAYS = [f"2025-02-{d:02d}" for d in range(1, 11)]
for _d in _DAYS:
    build_day(_DAILY, _d, with_anomaly=True)
_CUBE = os.path.join(_TMP, "cube")
build_timecube(_DAILY, _CUBE, spatial_chunk=8, time_chunk=4, shard_spatial=16)


class ApiRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._prev = (os.environ.get("GHRSST_ZARR_PATH"), os.environ.get("GHRSST_TIMECUBE_PATH"))
        os.environ["GHRSST_ZARR_PATH"] = _DAILY
        os.environ["GHRSST_TIMECUBE_PATH"] = _CUBE
        from fastapi.testclient import TestClient
        from api.app import app
        cls.cm = TestClient(app)
        cls.client = cls.cm.__enter__()
        cls.sa = StoreAccess(_DAILY)

    @classmethod
    def tearDownClass(cls):
        cls.cm.__exit__(None, None, None)
        for k, v in zip(("GHRSST_ZARR_PATH", "GHRSST_TIMECUBE_PATH"), cls._prev):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

    # P4: single-day / default-latest route to the CUBE when it covers the day (see RouterTests).
    def test_default_latest_routes_cube(self):
        r = self.client.get("/api/ghrsst", params={"lon0": 115.0, "lat0": 12.0})
        self.assertEqual(r.headers["x-store-route"], "cube")

    def test_single_day_routes_cube_with_same_values(self):
        p = {"lon0": 115.0, "lat0": 12.0, "start": "2025-02-03", "append": "sst,sea_ice"}
        r = self.client.get("/api/ghrsst", params=p)
        self.assertEqual(r.headers["x-store-route"], "cube")
        self.assertEqual(r.status_code, 200)
        # authoritative semantics preserved: identical to a direct daily read of the same day
        from store.store_access import StoreAccess as _SA
        exp = _SA(_DAILY).point_series(115.0, 12.0, ["2025-02-03"], ["sst", "sea_ice"])
        self.assertEqual(r.json(), exp)

    def test_multi_day_routes_cube_and_matches(self):
        r = self.client.get("/api/ghrsst", params={
            "lon0": 119.3, "lat0": 22.3, "start": "2025-02-02", "end": "2025-02-06",
            "append": "sst,sst_anomaly,sea_ice"})
        self.assertEqual(r.headers["x-store-route"], "cube")           # routed to time-cube
        days = ["2025-02-02", "2025-02-03", "2025-02-04", "2025-02-05", "2025-02-06"]
        p1 = self.sa.point_series(119.3, 22.3, days, ["sst", "sst_anomaly", "sea_ice"])
        # API returns date as ISO strings; normalize p1 dates (already ISO) and compare values
        got = r.json()
        self.assertEqual([row["date"] for row in got], days)
        for a, b in zip(got, p1):
            self.assertEqual(a.get("sst"), b.get("sst"))
            self.assertEqual(a.get("sst_anomaly"), b.get("sst_anomaly"))

    def test_bbox_and_post_still_work(self):
        rb = self.client.get("/api/ghrsst", params={
            "lon0": 110, "lat0": 10, "lon1": 112, "lat1": 12, "start": "2025-02-03"})
        self.assertEqual(rb.status_code, 200)
        rp = self.client.post("/api/ghrsst/points",
                              json={"date": "2025-02-03", "points": [[119.3, 22.3]], "append": "sst"})
        self.assertEqual(rp.status_code, 200)
        self.assertEqual(rp.json()[0]["index"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
