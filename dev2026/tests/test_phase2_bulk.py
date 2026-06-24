"""dev2026 — bulk time-cube builder: parity, resume idempotency, latest-days.

The bulk builder (time_block × shard-block writes) must produce a cube IDENTICAL to the per-day
build (and to P1), be safely re-runnable (checkpoint/resume), and support --latest-days.

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_bulk.py
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
from store.zarr_paths import group_path  # noqa: E402
from ingest.build_timecube import build_timecube  # noqa: E402
from ingest.build_timecube_bulk import build_timecube_bulk  # noqa: E402

NY, NX = 40, 56


def build_day(root, day, with_anomaly=True, bias=0.0):
    g = zarr.open_group(group_path(root, day), mode="w", zarr_format=3)
    g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,)); g["lon"][:] = np.linspace(100, 130, NX).astype(np.float32)
    g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,)); g["lat"][:] = np.linspace(0, 30, NY).astype(np.float32)
    sst = (10 + bias + 0.01 * np.arange(NY)[:, None] + 0.001 * np.arange(NX)[None, :]).astype(np.float32)
    sst[4, 6] = np.nan
    g.create_array("sst", shape=(1, NY, NX), dtype="float32", chunks=(1, 16, 16)); g["sst"][0] = sst
    g.create_array("sea_ice", shape=(1, NY, NX), dtype="float32", chunks=(1, 16, 16)); g["sea_ice"][0] = np.zeros((NY, NX), np.float32)
    if with_anomaly:
        g.create_array("sst_anomaly", shape=(1, NY, NX), dtype="float32", chunks=(1, 16, 16))
        g["sst_anomaly"][0] = np.full((NY, NX), 0.2 + bias * 0.01, np.float32)


class BulkBuildTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="p2bulk_")
        cls.daily = os.path.join(cls.tmp, "daily")
        # 100 days spans >1 time-block (tc=90) -> exercises multi-block + partial last block
        cls.days = [f"2024-{(i//28)+1:02d}-{(i%28)+1:02d}" for i in range(100)]
        cls.days = sorted(set(cls.days))[:100]
        for i, d in enumerate(cls.days):
            build_day(cls.daily, d, with_anomaly=(i % 4 != 0), bias=i)   # mixed presence
        cls.perday = os.path.join(cls.tmp, "perday")
        build_timecube(cls.daily, cls.perday, spatial_chunk=8, time_chunk=90, shard_spatial=32)
        cls.bulk = os.path.join(cls.tmp, "bulk")
        cls.meta = build_timecube_bulk(cls.daily, cls.bulk, spatial_chunk=8, time_chunk=90,
                                       shard_spatial=32, read_block=32, workers=4)
        cls.sa = StoreAccess(cls.daily, day_ttl_seconds=0.0)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_bulk_layout_matches(self):
        a_pd = zarr.open_group(self.perday, mode="r")["sst"]
        a_bk = zarr.open_group(self.bulk, mode="r")["sst"]
        self.assertEqual(a_pd.shape, a_bk.shape)
        self.assertEqual(a_pd.chunks, a_bk.chunks)
        self.assertEqual(tuple(a_pd.shards or ()), tuple(a_bk.shards or ()))

    def test_parity_bulk_perday_p1(self):
        bk = TimeCubeStore(self.bulk); pd = TimeCubeStore(self.perday)
        for lon, lat in [(119.3, 22.3), (100.0, 0.0), (130.0, 30.0), (112.7, 9.1)]:
            p1 = self.sa.point_series(lon, lat, self.days, ["sst", "sst_anomaly", "sea_ice"])
            self.assertEqual(bk.point_series(lon, lat, self.days, ["sst", "sst_anomaly", "sea_ice"]), p1)
            self.assertEqual(pd.point_series(lon, lat, self.days, ["sst", "sst_anomaly", "sea_ice"]), p1)

    def test_validity_mask_matches(self):
        self.assertEqual(zarr.open_group(self.bulk, mode="r").attrs["var_valid"],
                         zarr.open_group(self.perday, mode="r").attrs["var_valid"])

    def test_resume_idempotent(self):
        # re-run on the completed bulk cube -> 0 new units, cube unchanged, parity holds
        meta2 = build_timecube_bulk(self.daily, self.bulk, spatial_chunk=8, time_chunk=90,
                                    shard_spatial=32, read_block=32, workers=4)
        self.assertEqual(meta2["units_done_this_run"], 0)
        bk = TimeCubeStore(self.bulk)
        self.assertEqual(bk.point_series(119.3, 22.3, self.days, ["sst"]),
                         self.sa.point_series(119.3, 22.3, self.days, ["sst"]))

    def test_resume_after_partial(self):
        # simulate an interrupted build: drop some completed units from the checkpoint, re-run
        ck = os.path.join(self.bulk, "_build_checkpoint.json")
        with open(ck) as fh:
            c = json.load(fh)
        full = list(c["done"])
        c["done"] = full[: len(full) // 2]            # pretend half were not done
        with open(ck, "w") as fh:
            json.dump(c, fh)
        meta3 = build_timecube_bulk(self.daily, self.bulk, spatial_chunk=8, time_chunk=90,
                                    shard_spatial=32, read_block=32, workers=4)
        self.assertGreater(meta3["units_done_this_run"], 0)   # re-did the dropped half
        bk = TimeCubeStore(self.bulk)                          # still correct (idempotent writes)
        self.assertEqual(bk.point_series(112.7, 9.1, self.days, ["sst", "sst_anomaly"]),
                         self.sa.point_series(112.7, 9.1, self.days, ["sst", "sst_anomaly"]))

    def test_latest_days(self):
        out = os.path.join(self.tmp, "latest")
        build_timecube_bulk(self.daily, out, spatial_chunk=8, time_chunk=90, shard_spatial=32,
                            read_block=32, workers=2, latest_days=30)
        tc = TimeCubeStore(out)
        self.assertEqual(tc.day_count, 30)
        self.assertEqual(tc.days, self.days[-30:])
        # parity on the latest-30 window
        self.assertEqual(tc.point_series(119.3, 22.3, self.days[-30:], ["sst"]),
                         self.sa.point_series(119.3, 22.3, self.days[-30:], ["sst"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
