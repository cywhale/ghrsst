"""dev2026 — P2-S2: Candidate F parity + bounded-pool guardrails.

F must return EXACTLY the same point_series as P1 (it only parallelizes the reads), and
its global pool must keep concurrent chunk reads bounded by `parallelism` regardless of
how many client threads call it.

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_s2.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest

import numpy as np
import zarr

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from store.store_access import StoreAccess  # noqa: E402
from store.f_engine import FReadEngine  # noqa: E402
from store.zarr_paths import group_path  # noqa: E402

NY, NX, CY, CX = 32, 40, 8, 8


def build_day(root, day, with_anomaly=True):
    g = zarr.open_group(group_path(root, day), mode="w", zarr_format=3)
    g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,))
    g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,))
    g["lon"][:] = np.linspace(100.0, 130.0, NX).astype(np.float32)
    g["lat"][:] = np.linspace(0.0, 30.0, NY).astype(np.float32)
    sst = (10 + 0.01 * np.arange(NY)[:, None] + 0.001 * np.arange(NX)[None, :]).astype(np.float32)
    sst[3, 5] = np.nan
    g.create_array("sst", shape=(1, NY, NX), dtype="float32", chunks=(1, CY, CX))
    g["sst"][0] = sst
    g.create_array("sea_ice", shape=(1, NY, NX), dtype="float32", chunks=(1, CY, CX))
    g["sea_ice"][0] = np.zeros((NY, NX), np.float32)
    if with_anomaly:
        g.create_array("sst_anomaly", shape=(1, NY, NX), dtype="float32", chunks=(1, CY, CX))
        g["sst_anomaly"][0] = (0.1 * np.arange(NY)[:, None] * np.ones((1, NX))).astype(np.float32)


class FTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p2s2_")
        self.days = [f"2024-06-{d:02d}" for d in range(1, 13)]   # 12 contiguous days
        for i, d in enumerate(self.days):
            build_day(self.tmp, d, with_anomaly=(i != 4))         # one day lacks anomaly
        self.sa = StoreAccess(self.tmp, day_ttl_seconds=0.0)
        self.f = FReadEngine(self.tmp, parallelism=4)
        self.f.sa.day_ttl_seconds = 0.0

    def tearDown(self):
        self.f.shutdown()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parity_with_p1(self):
        for lon, lat in [(119.3, 22.3), (100.0, 0.0), (130.0, 30.0), (113.4, 12.7)]:
            p1 = self.sa.point_series(lon, lat, self.days, ["sst", "sst_anomaly", "sea_ice"])
            f = self.f.point_series(lon, lat, self.days, ["sst", "sst_anomaly", "sea_ice"])
            self.assertEqual(p1, f, f"F != P1 at ({lon},{lat})")

    def test_absent_field_omitted(self):
        rows = self.f.point_series(110.0, 10.0, [self.days[4]], ["sst", "sst_anomaly"])
        self.assertIn("sst", rows[0])
        self.assertNotIn("sst_anomaly", rows[0])

    def test_order_preserved(self):
        rows = self.f.point_series(119.3, 22.3, self.days, ["sst"])
        self.assertEqual([r["date"] for r in rows], self.days)

    def test_inflight_bounded_by_parallelism(self):
        # many client threads hammering the SAME engine must not exceed parallelism in-flight
        self.f.reset_inflight_peak()
        errs = []

        def worker():
            try:
                for _ in range(10):
                    self.f.point_series(115.0, 12.0, self.days, ["sst", "sea_ice"])
            except Exception as e:  # noqa: BLE001
                errs.append(repr(e))

        ts = [threading.Thread(target=worker) for _ in range(8)]   # 8 clients, parallelism=4
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errs, [])
        self.assertLessEqual(self.f.in_flight_peak, 4)             # GLOBAL pool bound holds
        self.assertGreaterEqual(self.f.in_flight_peak, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
