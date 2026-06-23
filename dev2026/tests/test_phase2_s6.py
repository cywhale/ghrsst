"""dev2026 — P2-S6: dual-write ingest (idempotency, recovery, coverage, observability).

daily store = source of truth; cube follows. Tests: idempotent upsert/sync, append-duplicate
rejection, sync_missing recovery (cube behind daily), coverage invariant, healthz cube fields +
route counts.

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_s6.py
"""
from __future__ import annotations

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
from store.zarr_paths import group_path  # noqa: E402
from ingest.build_timecube import append_day, build_timecube  # noqa: E402
from ingest.dual_write import check_coverage, read_daily_day, sync_day, sync_missing, upsert_day  # noqa: E402

NY, NX = 16, 20


def build_day(root, day, with_anomaly=True, bias=0.0):
    g = zarr.open_group(group_path(root, day), mode="w", zarr_format=3)
    g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,))
    g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,))
    g["lon"][:] = np.linspace(100.0, 130.0, NX).astype(np.float32)
    g["lat"][:] = np.linspace(0.0, 30.0, NY).astype(np.float32)
    sst = (10 + bias + 0.01 * np.arange(NY)[:, None] + 0.001 * np.arange(NX)[None, :]).astype(np.float32)
    g.create_array("sst", shape=(1, NY, NX), dtype="float32", chunks=(1, NY, NX))
    g["sst"][0] = sst
    g.create_array("sea_ice", shape=(1, NY, NX), dtype="float32", chunks=(1, NY, NX))
    g["sea_ice"][0] = np.zeros((NY, NX), np.float32)
    if with_anomaly:
        g.create_array("sst_anomaly", shape=(1, NY, NX), dtype="float32", chunks=(1, NY, NX))
        g["sst_anomaly"][0] = np.full((NY, NX), 0.3, np.float32)


class DualWriteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p2s6_")
        self.daily = os.path.join(self.tmp, "daily")
        self.days = [f"2024-10-{d:02d}" for d in range(1, 9)]   # 8 days
        for i, d in enumerate(self.days):
            build_day(self.daily, d, with_anomaly=True, bias=i)
        self.cube = os.path.join(self.tmp, "cube")
        # cube initially built from the first 6 days (behind by 2)
        build_timecube(self.daily, self.cube, spatial_chunk=8, time_chunk=4, days=self.days[:6])
        self.sa = StoreAccess(self.daily, day_ttl_seconds=0.0)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_append_duplicate_rejected(self):
        slab = {v: np.zeros((NY, NX), np.float32) for v in ("sst", "sst_anomaly", "sea_ice")}
        with self.assertRaises(ValueError):
            append_day(self.cube, self.days[0], slab)        # already present -> reject

    def test_sync_day_idempotent(self):
        before = TimeCubeStore(self.cube).day_count
        self.assertEqual(sync_day(self.daily, self.cube, self.days[6]), "append")   # new
        self.assertEqual(sync_day(self.daily, self.cube, self.days[6]), "overwrite")  # idempotent
        after = TimeCubeStore(self.cube).day_count
        self.assertEqual(after, before + 1)                  # no duplicate from the re-sync

    def test_sync_missing_recovery(self):
        # cube has 6 days, daily has 8 -> recovery appends the 2 missing
        synced = sync_missing(self.daily, self.cube)
        self.assertEqual(synced, self.days[6:8])
        cov = check_coverage(self.daily, self.cube)
        self.assertTrue(cov["ok"])
        self.assertTrue(cov["latest_in_sync"])
        # value parity for a recovered day
        tc = TimeCubeStore(self.cube)
        p1 = self.sa.point_series(115.0, 12.0, [self.days[7]], ["sst", "sst_anomaly"])
        self.assertEqual(tc.point_series(115.0, 12.0, [self.days[7]], ["sst", "sst_anomaly"]), p1)

    def test_sync_missing_rerunnable(self):
        sync_missing(self.daily, self.cube)
        self.assertEqual(sync_missing(self.daily, self.cube), [])   # nothing left, safe re-run

    def test_coverage_reports_behind(self):
        cov = check_coverage(self.daily, self.cube)
        self.assertFalse(cov["ok"])
        self.assertEqual(cov["missing_after_cube_latest"], self.days[6:8])
        self.assertEqual(cov["missing_within_cube_span"], [])
        self.assertEqual(cov["daily_latest"], self.days[7])
        self.assertEqual(cov["cube_latest"], self.days[5])

    def test_coverage_flags_out_of_order_gap(self):
        # build a cube missing a day WITHIN its span -> needs rebuild, not append
        cube2 = os.path.join(self.tmp, "cube2")
        build_timecube(self.daily, cube2, spatial_chunk=8, time_chunk=4,
                       days=self.days[:3] + self.days[4:6])     # skips days[3]
        cov = check_coverage(self.daily, cube2)
        self.assertIn(self.days[3], cov["missing_within_cube_span"])
        self.assertFalse(cov["ok"])


# ---- API healthz / route observability ----
_TMP = tempfile.mkdtemp(prefix="p2s6api_")
_DAILY = os.path.join(_TMP, "daily")
_DAYS = [f"2025-03-{d:02d}" for d in range(1, 9)]
for _i, _d in enumerate(_DAYS):
    build_day(_DAILY, _d, with_anomaly=True, bias=_i)
_CUBE = os.path.join(_TMP, "cube")
build_timecube(_DAILY, _CUBE, spatial_chunk=8, time_chunk=4)


class HealthzObservabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._prev = (os.environ.get("GHRSST_ZARR_PATH"), os.environ.get("GHRSST_TIMECUBE_PATH"))
        os.environ["GHRSST_ZARR_PATH"] = _DAILY
        os.environ["GHRSST_TIMECUBE_PATH"] = _CUBE
        from fastapi.testclient import TestClient
        from api.app import app
        cls.cm = TestClient(app)
        cls.client = cls.cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.cm.__exit__(None, None, None)
        for k, v in zip(("GHRSST_ZARR_PATH", "GHRSST_TIMECUBE_PATH"), cls._prev):
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

    def test_healthz_cube_fields(self):
        h = self.client.get("/healthz").json()
        self.assertTrue(h["cube_loaded"])
        self.assertEqual(h["cube_latest"], _DAYS[-1])
        self.assertEqual(h["cube_day_count"], len(_DAYS))
        self.assertTrue(h["cube_latest_in_sync"])

    def test_route_counts_increment(self):
        self.client.get("/api/ghrsst", params={
            "lon0": 115.0, "lat0": 12.0, "start": "2025-03-02", "end": "2025-03-06"})  # multi-day
        h = self.client.get("/healthz").json()
        self.assertGreaterEqual(h["route_counts"].get("cube", 0), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
