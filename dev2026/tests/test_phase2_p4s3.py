"""dev2026 — P4-S3: cube metadata refresh/reopen — new delta days visible WITHOUT a process restart.

Reproduces the P4-S0b VM24 finding (a cron-appended delta day is invisible to the running process until
reload) and proves the fix: `TimeCubeStore.refresh()` / `TieredCube.refresh()` re-read metadata
(concurrency-safe, read-only), and the app's TTL background loop picks up new days automatically. Refresh
only surfaces validated days (append_to_delta finalizes attrs['days'] last).

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_p4s3.py
"""
from __future__ import annotations

import datetime
import os
import sys
import tempfile
import time
import unittest

import numpy as np
import zarr

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from store.zarr_paths import group_path  # noqa: E402
from store.time_cube import TimeCubeStore  # noqa: E402
from store.tiered_cube import TieredCube  # noqa: E402
from ingest.build_timecube_bulk import build_timecube_bulk  # noqa: E402
from ingest.dual_write import append_to_delta  # noqa: E402

NY, NX = 40, 48
VARS = ("sst", "sst_anomaly", "sea_ice")


def _build(tmp, ndays=6):
    daily = os.path.join(tmp, "daily")
    days = [(datetime.date(2026, 6, 20) + datetime.timedelta(days=i)).isoformat() for i in range(ndays)]
    lon = np.linspace(100, 130, NX).astype(np.float32); lat = np.linspace(0, 30, NY).astype(np.float32)
    for i, d in enumerate(days):
        g = zarr.open_group(group_path(daily, d), mode="w", zarr_format=3)
        g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,)); g["lon"][:] = lon
        g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,)); g["lat"][:] = lat
        for v in VARS:
            fld = (5 + 0.01 * i + 0.001 * np.arange(NY)[:, None]).repeat(NX, 1)[:, :NX].astype(np.float32)
            g.create_array(v, shape=(1, NY, NX), dtype="float32", chunks=(1, 16, 16)); g[v][0] = fld
    base = os.path.join(tmp, "base")
    build_timecube_bulk(daily, base, spatial_chunk=8, time_chunk=90, shard_spatial=16, read_block=16,
                        workers=2, end_day=days[3])
    delta = os.path.join(tmp, "delta")
    append_to_delta(daily, delta, days[4], spatial_chunk=16, shard_spatial=16)   # delta has days[4]
    return daily, base, delta, days


class StoreRefresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p4s3_")
        self.daily, self.base, self.delta, self.days = _build(self.tmp)

    def tearDown(self):
        import shutil; shutil.rmtree(self.tmp, ignore_errors=True)

    def test_new_delta_day_visible_after_refresh(self):
        cube = TieredCube(TimeCubeStore(self.base), TimeCubeStore(self.delta))
        self.assertEqual(cube.latest, self.days[4])
        self.assertFalse(cube.covers_days([self.days[5]]))          # not appended yet

        append_to_delta(self.daily, self.delta, self.days[5], spatial_chunk=16, shard_spatial=16)
        # STILL stale on the SAME object (reproduces the bug)
        self.assertEqual(cube.latest, self.days[4])
        self.assertFalse(cube.covers_days([self.days[5]]))

        cube.refresh()                                              # the fix
        self.assertEqual(cube.latest, self.days[5])
        self.assertTrue(cube.covers_days([self.days[5]]))
        rows = cube.point_series(115.0, 12.0, [self.days[5]], ["sst"])   # and it SERVES the new day
        self.assertEqual([r["date"] for r in rows], [self.days[5]])

    def test_maybe_refresh_ttl_guard(self):
        cube = TieredCube(TimeCubeStore(self.base), TimeCubeStore(self.delta))
        append_to_delta(self.daily, self.delta, self.days[5], spatial_chunk=16, shard_spatial=16)
        self.assertFalse(cube.maybe_refresh(0))                     # 0 -> disabled, no refresh
        self.assertEqual(cube.latest, self.days[4])
        cube.maybe_refresh(10_000)                                  # not elapsed -> no refresh
        self.assertEqual(cube.latest, self.days[4])
        cube.delta._last_refresh -= 10_000                          # force elapsed
        self.assertTrue(cube.maybe_refresh(1))
        self.assertEqual(cube.latest, self.days[5])

    def test_refresh_only_surfaces_validated_days(self):
        # append_to_delta writes attrs['days'] LAST -> a store reading attrs never sees a half-day.
        d = TimeCubeStore(self.delta)
        before = list(d.days)
        append_to_delta(self.daily, self.delta, self.days[5], spatial_chunk=16, shard_spatial=16)
        d.refresh()
        self.assertEqual(d.days, before + [self.days[5]])           # exactly one fully-appended new day


# ---- app background TTL refresh (integration) ----
class AppRefresh(unittest.TestCase):
    def test_background_refresh_picks_up_new_delta_day(self):
        tmp = tempfile.mkdtemp(prefix="p4s3app_")
        daily, base, delta, days = _build(tmp)
        prev = {k: os.environ.get(k) for k in ("GHRSST_ZARR_PATH", "GHRSST_TIMECUBE_PATH",
                                               "GHRSST_DELTACUBE_PATH", "GHRSST_CUBE_REFRESH_TTL_SECONDS")}
        os.environ.update({"GHRSST_ZARR_PATH": daily, "GHRSST_TIMECUBE_PATH": base,
                           "GHRSST_DELTACUBE_PATH": delta, "GHRSST_CUBE_REFRESH_TTL_SECONDS": "1"})
        try:
            from fastapi.testclient import TestClient
            import importlib, api.app as appmod
            importlib.reload(appmod)                                # pick up the env-driven Cfg
            with TestClient(appmod.app) as c:
                h0 = c.get("/healthz").json()
                self.assertEqual(h0["delta_latest"], days[4])
                self.assertEqual(h0["cube_refresh_ttl_s"], 1)
                append_to_delta(daily, delta, days[5], spatial_chunk=16, shard_spatial=16)  # cron-like append
                deadline = time.time() + 6
                latest = h0["delta_latest"]
                while time.time() < deadline and latest != days[5]:
                    time.sleep(0.5); latest = c.get("/healthz").json()["delta_latest"]
                self.assertEqual(latest, days[5])                  # visible WITHOUT restart
        finally:
            for k, v in prev.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
            import importlib, api.app as appmod; importlib.reload(appmod)
            import shutil; shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
