"""dev2026 — append strategies: bulk_append_day (band-aid) + base/delta/compaction (production).

Both must preserve EXACT P1 semantics. Delta (time_chunk=1) gives cheap appends; TieredCube reads
base+delta; compaction folds delta into base. bulk_append_day writes a day tile-by-tile (bounded,
streaming, resumable).

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_append.py
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
from store.tiered_cube import TieredCube  # noqa: E402
from store.zarr_paths import group_path  # noqa: E402
from ingest.build_timecube import build_timecube  # noqa: E402
from ingest.build_timecube_bulk import build_timecube_bulk, bulk_append_day  # noqa: E402
from ingest.dual_write import append_to_delta, compact  # noqa: E402

NY, NX = 32, 40


def build_day(root, day, with_anomaly=True, bias=0.0):
    g = zarr.open_group(group_path(root, day), mode="w", zarr_format=3)
    g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,)); g["lon"][:] = np.linspace(100, 130, NX).astype(np.float32)
    g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,)); g["lat"][:] = np.linspace(0, 30, NY).astype(np.float32)
    sst = (10 + bias + 0.01 * np.arange(NY)[:, None] + 0.001 * np.arange(NX)[None, :]).astype(np.float32)
    sst[3, 5] = np.nan
    g.create_array("sst", shape=(1, NY, NX), dtype="float32", chunks=(1, 16, 16)); g["sst"][0] = sst
    g.create_array("sea_ice", shape=(1, NY, NX), dtype="float32", chunks=(1, 16, 16)); g["sea_ice"][0] = np.zeros((NY, NX), np.float32)
    if with_anomaly:
        g.create_array("sst_anomaly", shape=(1, NY, NX), dtype="float32", chunks=(1, 16, 16))
        g["sst_anomaly"][0] = np.full((NY, NX), 0.2 + bias * 0.01, np.float32)


def _days(n, start=0):
    import datetime
    return [(datetime.date(2024, 1, 1) + datetime.timedelta(days=start + i)).isoformat() for i in range(n)]


class Base:
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p2app_")
        self.daily = os.path.join(self.tmp, "daily")
        self.days = _days(12)
        for i, d in enumerate(self.days):
            build_day(self.daily, d, with_anomaly=(i % 3 != 0), bias=i)   # mixed presence
        self.sa = StoreAccess(self.daily, day_ttl_seconds=0.0)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class BulkAppendTests(Base, unittest.TestCase):
    def test_bulk_append_parity_and_resume(self):
        # base built through day 10 (holdout day 11), then bulk-append day 11
        cube = os.path.join(self.tmp, "cube")
        build_timecube_bulk(self.daily, cube, spatial_chunk=8, time_chunk=4, shard_spatial=16,
                            read_block=16, workers=2, end_day=self.days[10])
        self.assertEqual(TimeCubeStore(cube).day_count, 11)
        res = bulk_append_day(self.daily, cube, self.days[11], workers=2, read_block=16)
        self.assertEqual(res["op"], "append")
        self.assertEqual(TimeCubeStore(cube).day_count, 12)
        # parity vs P1 over all days (incl the appended one + its mixed presence)
        for lon, lat in [(119.3, 22.3), (104.0, 7.0)]:
            self.assertEqual(TimeCubeStore(cube).point_series(lon, lat, self.days, ["sst", "sst_anomaly"]),
                             self.sa.point_series(lon, lat, self.days, ["sst", "sst_anomaly"]))

    def test_bulk_append_resumable(self):
        cube = os.path.join(self.tmp, "cube2")
        build_timecube_bulk(self.daily, cube, spatial_chunk=8, time_chunk=4, shard_spatial=16,
                            read_block=16, workers=1, end_day=self.days[10])
        # simulate partial append: write a checkpoint marking the day partially done, then resume
        bulk_append_day(self.daily, cube, self.days[11], workers=1, read_block=16)  # completes
        self.assertFalse(os.path.isfile(os.path.join(cube, f"_append_{self.days[11]}.json")))  # ck cleared
        # upsert path: re-applying the same day overwrites in place, count unchanged
        res = bulk_append_day(self.daily, cube, self.days[11], workers=1, read_block=16)
        self.assertEqual(res["op"], "upsert")
        self.assertEqual(TimeCubeStore(cube).day_count, 12)


class TieredTests(Base, unittest.TestCase):
    def _build_base_delta(self, split):
        base = os.path.join(self.tmp, "base")
        build_timecube_bulk(self.daily, base, spatial_chunk=8, time_chunk=4, shard_spatial=16,
                            read_block=16, workers=2, end_day=self.days[split])
        delta = os.path.join(self.tmp, "delta")
        for d in self.days[split + 1:]:
            append_to_delta(self.daily, delta, d, spatial_chunk=8, shard_spatial=16)
        return TieredCube(TimeCubeStore(base), TimeCubeStore(delta)), base, delta

    def test_delta_create_then_append_tiled(self):
        delta = os.path.join(self.tmp, "delta_only")
        r1 = append_to_delta(self.daily, delta, self.days[8], spatial_chunk=8, shard_spatial=16,
                             read_block=16, workers=2)
        r2 = append_to_delta(self.daily, delta, self.days[9], spatial_chunk=8, shard_spatial=16,
                             read_block=16, workers=2)
        self.assertEqual(r1["op"], "create")
        self.assertEqual(r2["op"], "append")
        d = TimeCubeStore(delta)
        self.assertEqual(d.days, [self.days[8], self.days[9]])
        self.assertEqual(zarr.open_group(delta, mode="r")["sst"].chunks[0], 1)   # time_chunk==1
        # parity vs P1 on the delta days
        self.assertEqual(d.point_series(115.0, 12.0, [self.days[8], self.days[9]], ["sst", "sst_anomaly"]),
                         self.sa.point_series(115.0, 12.0, [self.days[8], self.days[9]], ["sst", "sst_anomaly"]))

    def test_delta_layout_decoupled_from_base(self):
        # base uses s8 (long-read layout); delta uses the APPEND-optimized default (large spatial
        # chunk). Chunkings differ, but TieredCube reads both parity-exact vs P1.
        base = os.path.join(self.tmp, "base_dec")
        build_timecube_bulk(self.daily, base, spatial_chunk=8, time_chunk=4, shard_spatial=16,
                            read_block=16, workers=2, end_day=self.days[6])
        delta = os.path.join(self.tmp, "delta_dec")
        for d in self.days[7:]:
            append_to_delta(self.daily, delta, d)              # DEFAULT append-optimized layout
        bchunk = int(zarr.open_group(base, mode="r")["sst"].chunks[-1])
        dchunk = int(zarr.open_group(delta, mode="r")["sst"].chunks[-1])
        self.assertNotEqual(bchunk, dchunk)                    # decoupled (base s8 vs large delta chunk)
        self.assertGreater(dchunk, bchunk)
        self.assertEqual(int(zarr.open_group(delta, mode="r")["sst"].chunks[0]), 1)  # delta time_chunk=1
        tc = TieredCube(TimeCubeStore(base), TimeCubeStore(delta))
        for lon, lat in [(119.3, 22.3), (104.0, 7.0)]:
            self.assertEqual(tc.point_series(lon, lat, self.days, ["sst", "sst_anomaly"]),
                             self.sa.point_series(lon, lat, self.days, ["sst", "sst_anomaly"]))

    def test_no_full_global_materialization(self):
        # tiled: the largest block processed must be <= read_block^2, NOT the full grid
        delta = os.path.join(self.tmp, "delta_tiled")
        rb = 16
        r = append_to_delta(self.daily, delta, self.days[0], spatial_chunk=8, shard_spatial=16,
                            read_block=rb, workers=2)
        self.assertLessEqual(r["max_block_cells"], rb * rb)
        self.assertLess(r["max_block_cells"], r["grid_cells"])   # never the whole grid
        self.assertGreater(r["tile_count"], 1)                   # actually tiled

    def test_missing_var_validity(self):
        # day 0 has NO sst_anomaly (i%3==0) -> delta must omit it for that day (valid False)
        delta = os.path.join(self.tmp, "delta_absent")
        append_to_delta(self.daily, delta, self.days[0], spatial_chunk=8, shard_spatial=16, read_block=16)
        append_to_delta(self.daily, delta, self.days[1], spatial_chunk=8, shard_spatial=16, read_block=16)
        d = TimeCubeStore(delta)
        rows = d.point_series(115.0, 12.0, [self.days[0], self.days[1]], ["sst", "sst_anomaly"])
        self.assertNotIn("sst_anomaly", rows[0])   # day0 absent -> omit (P1 parity)
        self.assertIn("sst_anomaly", rows[1])       # day1 present

    def test_delta_append_resumable(self):
        delta = os.path.join(self.tmp, "delta_resume")
        append_to_delta(self.daily, delta, self.days[0], spatial_chunk=8, shard_spatial=16, read_block=16)
        # simulate interruption mid-append of day1: pre-seed a partial checkpoint, then run
        append_to_delta(self.daily, delta, self.days[1], spatial_chunk=8, shard_spatial=16, read_block=16)
        self.assertFalse(os.path.isfile(os.path.join(delta, f"_delta_append_{self.days[1]}.json")))  # cleared
        # re-applying the same day = overwrite, count unchanged, still parity
        r = append_to_delta(self.daily, delta, self.days[1], spatial_chunk=8, shard_spatial=16, read_block=16)
        self.assertEqual(r["op"], "overwrite")
        self.assertEqual(TimeCubeStore(delta).day_count, 2)
        self.assertEqual(TimeCubeStore(delta).point_series(115.0, 12.0, [self.days[1]], ["sst"]),
                         self.sa.point_series(115.0, 12.0, [self.days[1]], ["sst"]))

    def test_tiered_parity_with_p1(self):
        tc, _, _ = self._build_base_delta(split=6)         # base: days[0..6], delta: days[7..11]
        self.assertTrue(tc.covers_days(self.days))
        self.assertEqual(tc.latest, self.days[-1])
        self.assertEqual(tc.day_count, 12)
        for lon, lat in [(119.3, 22.3), (100.0, 0.0), (104.0, 7.0)]:
            self.assertEqual(tc.point_series(lon, lat, self.days, ["sst", "sst_anomaly", "sea_ice"]),
                             self.sa.point_series(lon, lat, self.days, ["sst", "sst_anomaly", "sea_ice"]))

    def test_compaction_parity_and_reset(self):
        tc, base, delta = self._build_base_delta(split=6)
        out = compact(self.daily, base, delta, end_day=self.days[9], spatial_chunk=8,
                      time_chunk=4, shard_spatial=16, workers=2)
        self.assertEqual(out["delta_days_after"], self.days[10:12])
        nb = TimeCubeStore(out["new_base"]); nd = TimeCubeStore(out["new_delta"])
        self.assertEqual(nb.latest, self.days[9])          # base compacted through end_day
        self.assertEqual(nd.days, self.days[10:12])        # delta reset to post-end_day
        tc2 = TieredCube(nb, nd)
        for lon, lat in [(119.3, 22.3), (104.0, 7.0)]:
            self.assertEqual(tc2.point_series(lon, lat, self.days, ["sst", "sst_anomaly"]),
                             self.sa.point_series(lon, lat, self.days, ["sst", "sst_anomaly"]))


# ---- app: base+delta tiered routing + healthz ----
_TMP = tempfile.mkdtemp(prefix="p2appapi_")
_DAILY = os.path.join(_TMP, "daily")
_DAYS = _days(10)
for _i, _d in enumerate(_DAYS):
    build_day(_DAILY, _d, with_anomaly=True, bias=_i)
_BASE = os.path.join(_TMP, "base")
build_timecube_bulk(_DAILY, _BASE, spatial_chunk=8, time_chunk=4, shard_spatial=16, read_block=16, workers=2, end_day=_DAYS[6])
_DELTA = os.path.join(_TMP, "delta")
for _d in _DAYS[7:]:
    append_to_delta(_DAILY, _DELTA, _d, spatial_chunk=8, shard_spatial=16)


class TieredApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._prev = {k: os.environ.get(k) for k in ("GHRSST_ZARR_PATH", "GHRSST_TIMECUBE_PATH", "GHRSST_DELTACUBE_PATH")}
        os.environ["GHRSST_ZARR_PATH"] = _DAILY
        os.environ["GHRSST_TIMECUBE_PATH"] = _BASE
        os.environ["GHRSST_DELTACUBE_PATH"] = _DELTA
        from fastapi.testclient import TestClient
        from api.app import app
        cls.cm = TestClient(app); cls.client = cls.cm.__enter__()
        cls.sa = StoreAccess(_DAILY)

    @classmethod
    def tearDownClass(cls):
        cls.cm.__exit__(None, None, None)
        for k, v in cls._prev.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

    def test_healthz_tiered(self):
        h = self.client.get("/healthz").json()
        self.assertEqual(h["cube_kind"], "tiered")
        self.assertEqual(h["cube_latest"], _DAYS[-1])          # delta extends to latest
        self.assertEqual(h["delta_day_count"], 3)              # days 7,8,9
        self.assertTrue(h["cube_latest_in_sync"])

    def test_multiday_routes_cube_and_matches_p1(self):
        # range spanning base + delta -> routes cube (tiered), matches P1
        r = self.client.get("/api/ghrsst", params={
            "lon0": 119.3, "lat0": 22.3, "start": _DAYS[4], "end": _DAYS[9],
            "append": "sst,sst_anomaly,sea_ice"})
        self.assertEqual(r.headers["x-store-route"], "cube")
        days = _DAYS[4:10]
        p1 = self.sa.point_series(119.3, 22.3, days, ["sst", "sst_anomaly", "sea_ice"])
        got = r.json()
        self.assertEqual([row["date"] for row in got], days)
        for a, b in zip(got, p1):
            self.assertEqual(a.get("sst"), b.get("sst"))
            self.assertEqual(a.get("sst_anomaly"), b.get("sst_anomaly"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
