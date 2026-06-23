"""dev2026 — P2-S4: time-cube real-data variable absence (the [Medium] gate item).

The cube is dense, so a NaN can mean EITHER 'land (present -> null)' OR 'var absent that day
(-> omit)'. P2-S4 stores a per-(day,var) validity mask so TimeCubeStore reproduces P1's
EXACT per-day semantics. We use P1 StoreAccess as the oracle over a MIXED fixture where some
days lack sst_anomaly AND the queried point can land on a NaN cell — one parity assertion
then covers both distinctions.

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_s4.py
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
LAND = (2, 3)   # a cell that is NaN in sst AND (when present) in sst_anomaly


def build_day(root, day, with_anomaly):
    g = zarr.open_group(group_path(root, day), mode="w", zarr_format=3)
    g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,))
    g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,))
    lon = np.linspace(100.0, 130.0, NX).astype(np.float32)
    lat = np.linspace(0.0, 30.0, NY).astype(np.float32)
    g["lon"][:] = lon
    g["lat"][:] = lat
    sst = (10 + 0.01 * np.arange(NY)[:, None] + 0.001 * np.arange(NX)[None, :]).astype(np.float32)
    sst[LAND] = np.nan
    g.create_array("sst", shape=(1, NY, NX), dtype="float32", chunks=(1, NY, NX))
    g["sst"][0] = sst
    g.create_array("sea_ice", shape=(1, NY, NX), dtype="float32", chunks=(1, NY, NX))
    g["sea_ice"][0] = np.zeros((NY, NX), np.float32)
    if with_anomaly:
        an = (0.1 * np.arange(NY)[:, None] * np.ones((1, NX))).astype(np.float32)
        an[LAND] = np.nan                       # present-but-NaN land in anomaly too
        g.create_array("sst_anomaly", shape=(1, NY, NX), dtype="float32", chunks=(1, NY, NX))
        g["sst_anomaly"][0] = an
    return lon, lat


class VarAbsenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p2s4_")
        self.daily = os.path.join(self.tmp, "daily")
        self.days = [f"2024-08-{d:02d}" for d in range(1, 8)]   # 7 days
        # mixed: anomaly present on days 0,2,4,6; absent on 1,3,5
        self.present = [i % 2 == 0 for i in range(len(self.days))]
        for i, d in enumerate(self.days):
            self.lon, self.lat = build_day(self.daily, d, with_anomaly=self.present[i])
        self.cube = os.path.join(self.tmp, "cube")
        build_timecube(self.daily, self.cube, spatial_chunk=8, time_chunk=4, shard_spatial=16)
        self.sa = StoreAccess(self.daily, day_ttl_seconds=0.0)
        self.tc = TimeCubeStore(self.cube)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_validity_mask_built(self):
        self.assertEqual(self.tc.var_valid["sst_anomaly"], self.present)
        self.assertEqual(self.tc.var_valid["sst"], [True] * len(self.days))

    def test_parity_ocean_point(self):
        # P1 is the oracle: omits anomaly on absent days, value/null on present days
        p1 = self.sa.point_series(115.0, 12.0, self.days, ["sst", "sst_anomaly", "sea_ice"])
        tc = self.tc.point_series(115.0, 12.0, self.days, ["sst", "sst_anomaly", "sea_ice"])
        self.assertEqual(p1, tc)
        # explicit: absent days omit the key, present days have it
        for row, present in zip(tc, self.present):
            self.assertEqual("sst_anomaly" in row, present)

    def test_parity_land_point_absent_vs_null(self):
        # query the LAND cell -> present days: anomaly null (key present); absent days: omit
        lon_q, lat_q = float(self.lon[LAND[1]]), float(self.lat[LAND[0]])
        p1 = self.sa.point_series(lon_q, lat_q, self.days, ["sst", "sst_anomaly"])
        tc = self.tc.point_series(lon_q, lat_q, self.days, ["sst", "sst_anomaly"])
        self.assertEqual(p1, tc)
        for row, present in zip(tc, self.present):
            self.assertIsNone(row["sst"])                       # land -> sst null always
            if present:
                self.assertIn("sst_anomaly", row)
                self.assertIsNone(row["sst_anomaly"])           # present-but-NaN -> null
            else:
                self.assertNotIn("sst_anomaly", row)            # absent -> omit

    def test_append_missing_var_omitted(self):
        ny, nx = NY, NX
        # append a day WITHOUT anomaly -> reader must omit it
        append_day(self.cube, "2024-08-08",
                   {"sst": np.full((ny, nx), 5.0, np.float32),
                    "sea_ice": np.zeros((ny, nx), np.float32)})   # no sst_anomaly
        tc = TimeCubeStore(self.cube)
        rows = tc.point_series(115.0, 12.0, ["2024-08-08"], ["sst", "sst_anomaly"])
        self.assertEqual(rows[0]["sst"], 5.0)
        self.assertNotIn("sst_anomaly", rows[0])
        # and a present day still has it
        append_day(self.cube, "2024-08-09",
                   {"sst": np.full((ny, nx), 6.0, np.float32),
                    "sst_anomaly": np.full((ny, nx), 0.5, np.float32),
                    "sea_ice": np.zeros((ny, nx), np.float32)})
        tc = TimeCubeStore(self.cube)
        rows = tc.point_series(115.0, 12.0, ["2024-08-09"], ["sst", "sst_anomaly"])
        self.assertEqual(rows[0]["sst_anomaly"], 0.5)


@unittest.skipUnless(os.environ.get("GHRSST_ZARR_PATH"),
                     "GHRSST_ZARR_PATH not set; skipping real-data regional cube parity")
class RealRegionalCubeTests(unittest.TestCase):
    """Build a REGIONAL cube from the real store over a short span and parity-check vs P1 on
    real values/chunks (local real days are all-present, so this covers real geometry/values;
    mixed-absence is covered synthetically above)."""
    @classmethod
    def setUpClass(cls):
        from store.store_access import _nearest_idx
        cls.store = os.environ["GHRSST_ZARR_PATH"]
        cls.sa = StoreAccess(cls.store)
        all_days = cls.sa.existing_days()
        if len(all_days) < 20:
            raise unittest.SkipTest("need >=20 days")
        cls.days = all_days[-20:]
        lon, lat = cls.sa._coords()
        j0, j1 = _nearest_idx(118.0, lon), _nearest_idx(121.0, lon) + 1
        i0, i1 = _nearest_idx(21.0, lat), _nearest_idx(24.0, lat) + 1
        cls.tmp = tempfile.mkdtemp(prefix="p2s4real_")
        cls.cube = os.path.join(cls.tmp, "cube")
        build_timecube(cls.store, cls.cube, spatial_chunk=8, time_chunk=90,
                       shard_spatial=64, region=(i0, i1, j0, j1), days=cls.days)
        cls.tc = TimeCubeStore(cls.cube)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_real_regional_parity(self):
        fields = ["sst", "sst_anomaly", "sea_ice"]
        for lon, lat in [(119.3, 22.3), (120.0, 23.0), (118.5, 21.5)]:
            p1 = self.sa.point_series(lon, lat, self.days, fields)
            tc = self.tc.point_series(lon, lat, self.days, fields)
            self.assertEqual(p1, tc, f"real cube != P1 at ({lon},{lat})")


if __name__ == "__main__":
    unittest.main(verbosity=2)
