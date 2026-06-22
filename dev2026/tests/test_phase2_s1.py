"""dev2026 — P2-S1: Candidate E1 (manifest sidecar) parity + manifest correctness.

E1 must return EXACTLY the same point_series as P1 StoreAccess (it only changes the
per-day read path, not the data). Also: manifest covers all existing days; absent fields
are omitted (old/P1 parity); new-day visibility via rescan.

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_s1.py
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
from store.e1_manifest import E1ManifestStore  # noqa: E402
from store.zarr_paths import group_path  # noqa: E402

NY, NX, CY, CX = 32, 40, 8, 8


def build_day(root, day, with_anomaly=True):
    lon = np.linspace(100.0, 130.0, NX).astype(np.float32)
    lat = np.linspace(0.0, 30.0, NY).astype(np.float32)
    g = zarr.open_group(group_path(root, day), mode="w", zarr_format=3)
    g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,))
    g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,))
    g["lon"][:] = lon
    g["lat"][:] = lat
    sst = (10 + 0.01 * np.arange(NY)[:, None] + 0.001 * np.arange(NX)[None, :]).astype(np.float32)
    sst[3, 5] = np.nan
    g.create_array("sst", shape=(1, NY, NX), dtype="float32", chunks=(1, CY, CX))
    g["sst"][0] = sst
    g.create_array("sea_ice", shape=(1, NY, NX), dtype="float32", chunks=(1, CY, CX))
    g["sea_ice"][0] = np.zeros((NY, NX), np.float32)
    if with_anomaly:
        g.create_array("sst_anomaly", shape=(1, NY, NX), dtype="float32", chunks=(1, CY, CX))
        g["sst_anomaly"][0] = (0.1 * np.arange(NY)[:, None] * np.ones((1, NX))).astype(np.float32)


class E1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p2s1_")
        # day 2 deliberately has NO sst_anomaly -> exercise absent-field omit parity
        self.days = ["2024-05-01", "2024-05-02", "2024-05-03"]
        for i, d in enumerate(self.days):
            build_day(self.tmp, d, with_anomaly=(i != 1))
        self.sa = StoreAccess(self.tmp, day_ttl_seconds=0.0)
        self.e1 = E1ManifestStore(self.tmp)
        self.e1.sa.day_ttl_seconds = 0.0
        self.e1.build_manifest()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_manifest_covers_days(self):
        st = self.e1.manifest_stats()
        self.assertEqual(st["days"], 3)
        self.assertEqual(st["var_coverage"]["sst"], 3)
        self.assertEqual(st["var_coverage"]["sst_anomaly"], 2)   # day 2 has none

    def test_parity_with_p1(self):
        for lon, lat in [(119.3, 22.3), (100.0, 0.0), (130.0, 30.0), (113.4, 12.7)]:
            p1 = self.sa.point_series(lon, lat, self.days, ["sst", "sst_anomaly", "sea_ice"])
            e1 = self.e1.point_series(lon, lat, self.days, ["sst", "sst_anomaly", "sea_ice"])
            self.assertEqual(p1, e1, f"E1 != P1 at ({lon},{lat})")

    def test_absent_field_omitted(self):
        rows = self.e1.point_series(110.0, 10.0, ["2024-05-02"], ["sst", "sst_anomaly"])
        self.assertIn("sst", rows[0])
        self.assertNotIn("sst_anomaly", rows[0])   # day 2 lacks it -> omit (parity)

    def test_new_day_visibility(self):
        rows = self.e1.point_series(110.0, 10.0, ["2024-05-09"], ["sst"])
        self.assertEqual(rows, [])                 # not present yet
        build_day(self.tmp, "2024-05-09")
        rows = self.e1.point_series(110.0, 10.0, ["2024-05-09"], ["sst"])
        self.assertEqual(len(rows), 1)             # rescan picks it up


if __name__ == "__main__":
    unittest.main(verbosity=2)
