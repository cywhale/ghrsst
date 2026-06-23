"""dev2026 — P2-S3: time-cube parity + structural sanity.

TimeCubeStore.point_series must equal P1 StoreAccess.point_series (value + order) on the
same data, and the cube must have time-major chunking (the structural change). Field not
present in the cube is omitted (P1 parity). Per-day var absence on real data is a P2-S4
item (synthetic fixtures here have all vars on all days).

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_s3.py
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


class TimeCubeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p2s3_")
        self.daily = os.path.join(self.tmp, "daily")
        self.days = [f"2024-07-{d:02d}" for d in range(1, 15)]   # 14 contiguous days
        for d in self.days:
            build_day(self.daily, d, with_anomaly=True)
        self.cube = os.path.join(self.tmp, "cube")
        self.meta = build_timecube(self.daily, self.cube, spatial_chunk=8,
                                   time_chunk=None, shard_spatial=16)
        self.sa = StoreAccess(self.daily, day_ttl_seconds=0.0)
        self.tc = TimeCubeStore(self.cube)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_parity_with_p1(self):
        for lon, lat in [(119.3, 22.3), (100.0, 0.0), (130.0, 30.0), (112.7, 9.1)]:
            p1 = self.sa.point_series(lon, lat, self.days, ["sst", "sst_anomaly", "sea_ice"])
            tc = self.tc.point_series(lon, lat, self.days, ["sst", "sst_anomaly", "sea_ice"])
            self.assertEqual(p1, tc, f"cube != P1 at ({lon},{lat})")

    def test_order_and_subset(self):
        sub = self.days[3:9]
        rows = self.tc.point_series(115.0, 12.0, sub, ["sst"])
        self.assertEqual([r["date"] for r in rows], sub)

    def test_time_major_chunk(self):
        # structural: the cube's sst array is time-major with one time chunk (=T) and small spatial
        arr = zarr.open_group(self.cube, mode="r")["sst"]
        self.assertEqual(arr.shape, (len(self.days), NY, NX))
        self.assertEqual(arr.chunks[0], len(self.days))      # time-contiguous
        self.assertLessEqual(arr.chunks[1], 8)               # small spatial
        self.assertIsNotNone(self.meta["shards"])            # sharded

    def test_field_not_in_cube_omitted(self):
        # cube built from daily-without-anomaly -> requesting anomaly omits the key
        daily2 = os.path.join(self.tmp, "daily2")
        for d in self.days[:3]:
            build_day(daily2, d, with_anomaly=False)
        cube2 = os.path.join(self.tmp, "cube2")
        build_timecube(daily2, cube2, spatial_chunk=8)
        tc2 = TimeCubeStore(cube2)
        rows = tc2.point_series(110.0, 10.0, self.days[:3], ["sst", "sst_anomaly"])
        self.assertIn("sst", rows[0])
        self.assertNotIn("sst_anomaly", rows[0])

    def test_overwrite_guard(self):
        out = os.path.join(self.tmp, "cube_guard")
        build_timecube(self.daily, out, spatial_chunk=8)              # first build ok
        with self.assertRaises(FileExistsError):
            build_timecube(self.daily, out, spatial_chunk=8)          # exists -> refuse (path safety)
        build_timecube(self.daily, out, spatial_chunk=8, overwrite=True)   # explicit overwrite ok

    def test_holdout_days_subset(self):
        out = os.path.join(self.tmp, "cube_holdout")
        build_timecube(self.daily, out, spatial_chunk=8, days=self.days[:-1])  # exclude latest
        self.assertEqual(TimeCubeStore(out).day_count, len(self.days) - 1)
        self.assertNotIn(self.days[-1], TimeCubeStore(out).days)

    def test_append_day_visible(self):
        ny, nx = self.meta["grid"]
        append_day(self.cube, "2024-07-15", {v: np.ones((ny, nx), np.float32)
                                             for v in self.meta["vars"]})
        tc = TimeCubeStore(self.cube)            # reopen to pick up new attrs
        rows = tc.point_series(115.0, 12.0, ["2024-07-15"], ["sst"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sst"], 1.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
