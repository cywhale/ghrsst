"""dev2026 — P4-S0: prototype single-day CUBE reads must be PARITY-exact vs the DAILY store.

Parity first (the benchmark's perf numbers are meaningless without it): single-day bbox / point /
POST points read from a base-cube or delta-cube must reproduce the daily store's values AND its
absent/NaN semantics (point→omit absent key; bbox/batch→null absent; land NaN→null). var_valid drives
absent on the cube.

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_p4s0.py
"""
from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest

import numpy as np
import zarr

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from store.zarr_paths import group_path  # noqa: E402
from store.store_access import StoreAccess  # noqa: E402
from store.time_cube import TimeCubeStore  # noqa: E402
from store.cube_singleday_proto import cube_bbox_arrays, cube_point, cube_points_batch  # noqa: E402
from ingest.build_timecube_bulk import build_timecube_bulk  # noqa: E402
from ingest.dual_write import append_to_delta  # noqa: E402

NY, NX = 48, 56
VARS = ("sst", "sst_anomaly", "sea_ice")


def _f32(a, b):
    if a is None or b is None:
        return a is b
    if isinstance(a, float) or isinstance(b, float):
        if isinstance(a, float) and math.isnan(a):
            return isinstance(b, float) and math.isnan(b)
        return np.float32(a) == np.float32(b)
    return a == b                                       # date str / index int -> plain equality


def _rows_eq(a, b):
    if len(a) != len(b):
        return False
    for ra, rb in zip(a, b):
        if set(ra) != set(rb):
            return False
        if not all(_f32(ra[k], rb[k]) for k in ra):
            return False
    return True


class P4S0Parity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="p4s0t_")
        daily = os.path.join(cls.tmp, "daily")
        import datetime
        cls.days = [(datetime.date(2026, 1, 1) + datetime.timedelta(days=i)).isoformat() for i in range(6)]
        rng = np.random.default_rng(0)
        lon = np.linspace(100, 110, NX).astype(np.float32); lat = np.linspace(0, 10, NY).astype(np.float32)
        nan = rng.random((NY, NX)) < 0.1
        for i, d in enumerate(cls.days):
            g = zarr.open_group(group_path(daily, d), mode="w", zarr_format=3)
            g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,)); g["lon"][:] = lon
            g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,)); g["lat"][:] = lat
            present = VARS if i != 2 else ("sst", "sea_ice")     # day index 2: sst_anomaly ABSENT
            for v in present:
                fld = (5 + 0.01 * i + 0.001 * np.arange(NY)[:, None]).repeat(NX, 1)[:, :NX].astype(np.float32)
                fld = fld + 0.0005 * np.arange(NX)[None, :]
                fld[nan] = np.nan
                g.create_array(v, shape=(1, NY, NX), dtype="float32", chunks=(1, 16, 16)); g[v][0] = fld
        cls.daily = daily
        cls.sa = StoreAccess(daily)
        base = os.path.join(cls.tmp, "base")
        build_timecube_bulk(daily, base, spatial_chunk=8, time_chunk=4, shard_spatial=16,
                            read_block=16, workers=2, end_day=cls.days[4])
        delta = os.path.join(cls.tmp, "delta")
        append_to_delta(daily, delta, cls.days[5], spatial_chunk=16, shard_spatial=16)
        cls.base = TimeCubeStore(base); cls.delta = TimeCubeStore(delta)

    @classmethod
    def tearDownClass(cls):
        import shutil; shutil.rmtree(cls.tmp, ignore_errors=True)

    def _bbox_eq(self, day, tc):
        b = (101.0, 1.0, 109.0, 9.0)
        dl, da, dc = self.sa.bbox_arrays(day, *b, VARS)
        cl, ca, cc = cube_bbox_arrays(tc, day, *b, VARS)
        self.assertEqual(set(dc), set(cc))
        for f in dc:
            self.assertTrue(np.array_equal(np.nan_to_num(dc[f], nan=-9e9), np.nan_to_num(cc[f], nan=-9e9)), f)

    def test_bbox_parity_base_and_delta(self):
        self._bbox_eq(self.days[1], self.base)        # historical (base)
        self._bbox_eq(self.days[5], self.delta)        # recent (delta)

    def test_point_parity_omit_absent(self):
        for day, tc in ((self.days[1], self.base), (self.days[5], self.delta), (self.days[2], self.base)):
            d = self.sa.point_series(104.0, 5.0, [day], VARS)
            c = cube_point(tc, day, 104.0, 5.0, VARS)
            self.assertTrue(_rows_eq(d, c), f"{day}: {d} != {c}")
        # day index 2 has sst_anomaly ABSENT -> both omit the key
        self.assertNotIn("sst_anomaly", cube_point(self.base, self.days[2], 104.0, 5.0, VARS)[0])

    def test_batch_parity_null_absent(self):
        pts = [[101.5, 1.5], [104.0, 5.0], [108.0, 8.0]]
        for day, tc in ((self.days[1], self.base), (self.days[5], self.delta), (self.days[2], self.base)):
            d = self.sa.points_batch(pts, day, VARS)
            c = cube_points_batch(tc, day, pts, VARS)
            self.assertTrue(_rows_eq(d, c), f"{day}: {d} != {c}")
        # absent var -> null key PRESENT (points_batch semantics), not omitted
        self.assertIsNone(cube_points_batch(self.base, self.days[2], pts, VARS)[0]["sst_anomaly"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
