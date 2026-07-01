"""dev2026 — P4-S3: chronological `latest` when delta days are APPEND-ORDER (out-of-order backfill).

Production 31-day backfill exposed: append_to_delta writes attrs['days'] in APPEND order, so after
backfilling older days AFTER recent ones, days[-1] is NOT the chronologically latest. `TimeCubeStore.latest`
(and cube_latest/delta_latest/spatial_window) must be chronological max(days); day_index must keep mapping
each day to its PHYSICAL stored time index (arrays NOT reordered).

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_p4s3order.py
"""
from __future__ import annotations

import datetime
import importlib
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
from store.tiered_cube import TieredCube  # noqa: E402
from store import spatial_policy  # noqa: E402
from ingest.build_timecube_bulk import build_timecube_bulk  # noqa: E402
from ingest.dual_write import append_to_delta, sync_missing  # noqa: E402

NY, NX = 32, 40
VARS = ("sst", "sst_anomaly", "sea_ice")


def _build(tmp):
    daily = os.path.join(tmp, "daily")
    days = [(datetime.date(2026, 6, 1) + datetime.timedelta(days=i)).isoformat() for i in range(6)]
    lon = np.linspace(120, 130, NX).astype(np.float32); lat = np.linspace(10, 20, NY).astype(np.float32)
    for i, d in enumerate(days):
        g = zarr.open_group(group_path(daily, d), mode="w", zarr_format=3)
        g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,)); g["lon"][:] = lon
        g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,)); g["lat"][:] = lat
        for v in VARS:
            fld = np.full((NY, NX), 285.0 + 0.01 * i, np.float32)   # each day a distinct value
            g.create_array(v, shape=(1, NY, NX), dtype="float32", chunks=(1, 16, 16)); g[v][0] = fld
    base = os.path.join(tmp, "base")
    build_timecube_bulk(daily, base, spatial_chunk=8, time_chunk=90, shard_spatial=16, read_block=16,
                        workers=2, end_day=days[3])                 # base d0..d3 (chronological)
    delta = os.path.join(tmp, "delta")
    for d in (days[4], days[0], days[2]):                          # APPEND OUT OF ORDER
        append_to_delta(daily, delta, d, spatial_chunk=16, shard_spatial=16)
    return daily, base, delta, days


class Ordering(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p4s3o_")
        self.daily, self.base, self.delta, self.days = _build(self.tmp)

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def test_latest_is_chronological_arrays_not_reordered(self):
        d = TimeCubeStore(self.delta)
        self.assertEqual(d.days, [self.days[4], self.days[0], self.days[2]])   # PHYSICAL order kept
        self.assertEqual(d._day_index, {self.days[4]: 0, self.days[0]: 1, self.days[2]: 2})
        self.assertEqual(d.latest, self.days[4])                              # chronological max, NOT days[-1]
        self.assertNotEqual(d.latest, d.days[-1])                            # days[-1] == d2 (the bug)

    def test_point_series_correct_per_physical_index(self):
        sa = StoreAccess(self.daily); d = TimeCubeStore(self.delta)
        for day in (self.days[0], self.days[2], self.days[4]):
            ref = sa.point_series(125.0, 15.0, [day], ["sst"])[0]["sst"]
            got = d.point_series(125.0, 15.0, [day], ["sst"])[0]["sst"]
            self.assertEqual(np.float32(got), np.float32(ref), day)          # right data despite append order

    def test_tiered_and_spatial_window_chronological(self):
        tier = TieredCube(TimeCubeStore(self.base), TimeCubeStore(self.delta))
        self.assertEqual(tier.latest, self.days[4])                          # max(base d3, delta d4)
        self.assertEqual(spatial_policy.spatial_window_bounds(tier.delta.days), (self.days[0], self.days[4]))

    def test_healthz_chronological(self):
        prev = {k: os.environ.get(k) for k in ("GHRSST_ZARR_PATH", "GHRSST_TIMECUBE_PATH", "GHRSST_DELTACUBE_PATH")}
        os.environ.update({"GHRSST_ZARR_PATH": self.daily, "GHRSST_TIMECUBE_PATH": self.base,
                           "GHRSST_DELTACUBE_PATH": self.delta})
        try:
            import api.app as appmod; importlib.reload(appmod)
            from fastapi.testclient import TestClient
            with TestClient(appmod.app) as c:
                h = c.get("/healthz").json()
                self.assertEqual(h["delta_latest"], self.days[4])            # chronological
                self.assertEqual(h["cube_latest"], self.days[4])
                self.assertEqual(h["spatial_window"], [self.days[0], self.days[4]])
        finally:
            for k, v in prev.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            import api.app as appmod; importlib.reload(appmod)

    def test_ingest_latest_chronological(self):
        # cube latest is d4 chronologically -> only d5 (> d4) is "after". With the old days[-1]==d2 bug,
        # sync_missing would wrongly also pick d3 (an in-span gap that needs a rebuild, not an append).
        self.assertEqual(sync_missing(self.daily, self.delta), [self.days[5]])


if __name__ == "__main__":
    unittest.main(verbosity=2)
