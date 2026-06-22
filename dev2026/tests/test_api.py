"""dev2026 — P1-S2 API tests (FastAPI TestClient over a synthetic store).

Covers: GET point (default-latest no-store), GET range (+413 over MAX_DAYS, +fixed
cache), absent-field omit on point / null on bbox+points, bbox StreamingResponse is
a valid JSON array of the right length, POST /points contract (order/index/dupes/
null/413/empty), bad field 400, healthz, and the BoundedExecutor backpressure
primitive (overload -> Overloaded).

Self-contained: builds a tiny synthetic Zarr store and points GHRSST_ZARR_PATH at it.
Run: dev2026/.venv/bin/python dev2026/tests/test_api.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import unittest

import numpy as np
import zarr

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from store.zarr_paths import group_path  # noqa: E402

NY, NX = 40, 50
CY, CX = 16, 16


def build_day(root, day, with_anomaly=False):
    lon = np.linspace(100.0, 130.0, NX).astype(np.float32)
    lat = np.linspace(0.0, 30.0, NY).astype(np.float32)
    g = zarr.open_group(group_path(root, day), mode="w", zarr_format=3)
    g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,))
    g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,))
    g.create_array("sst", shape=(1, NY, NX), dtype="float32", chunks=(1, CY, CX))
    g.create_array("sea_ice", shape=(1, NY, NX), dtype="float32", chunks=(1, CY, CX))
    g["lon"][:] = lon
    g["lat"][:] = lat
    sst = (10.0 + 0.01 * np.arange(NY)[:, None] + 0.001 * np.arange(NX)[None, :]).astype(np.float32)
    sst[5, 7] = np.nan  # land
    g["sst"][0] = sst
    g["sea_ice"][0] = np.zeros((NY, NX), np.float32)
    if with_anomaly:
        g.create_array("sst_anomaly", shape=(1, NY, NX), dtype="float32", chunks=(1, CY, CX))
        g["sst_anomaly"][0] = np.zeros((NY, NX), np.float32)


DAYS = ["2025-01-01", "2025-01-02", "2025-01-03"]   # latest = 2025-01-03

# app import only needs tunables (defaults fine); the store path is read at lifespan
# (setUpClass), NOT import — so no module-level env mutation (keeps cross-module
# test isolation: see tearDownClass which restores the original GHRSST_ZARR_PATH).
from fastapi.testclient import TestClient  # noqa: E402
from api.app import app, BoundedExecutor, Overloaded  # noqa: E402


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._prev_env = os.environ.get("GHRSST_ZARR_PATH")   # save to restore later
        cls._tmp = tempfile.mkdtemp(prefix="ghrsst_api_")
        for d in DAYS:
            build_day(cls._tmp, d, with_anomaly=False)        # synthetic days: NO sst_anomaly
        os.environ["GHRSST_ZARR_PATH"] = cls._tmp
        cls.client_cm = TestClient(app)
        cls.client = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)
        if cls._prev_env is None:                             # restore env for other modules
            os.environ.pop("GHRSST_ZARR_PATH", None)
        else:
            os.environ["GHRSST_ZARR_PATH"] = cls._prev_env
        import shutil
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def test_point_default_latest_no_store(self):
        r = self.client.get("/api/ghrsst", params={"lon0": 110.0, "lat0": 12.0})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["date"], "2025-01-03")          # latest
        self.assertEqual(r.headers["cache-control"], "no-store")  # date-less -> no-store

    def test_point_range_fixed_cache(self):
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": "2025-01-01", "end": "2025-01-02"})
        self.assertEqual(r.status_code, 200)
        dates = [row["date"] for row in r.json()]
        self.assertEqual(dates, ["2025-01-01", "2025-01-02"])
        # end (01-02) < latest (01-03) -> immutable -> long cache
        self.assertIn("max-age", r.headers["cache-control"])

    def test_point_range_touching_latest_no_store(self):
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": "2025-01-01", "end": "2025-01-03"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["cache-control"], "no-store")  # touches latest

    def test_point_range_413(self):
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": "2024-01-01", "end": "2025-06-01"})
        self.assertEqual(r.status_code, 413)
        detail = r.json()["detail"]
        self.assertEqual(detail["max_days"], 365)
        self.assertGreater(detail["requested_days"], 365)

    def test_point_omits_absent_field(self):
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "append": "sst,sst_anomaly"})
        row = r.json()[0]
        self.assertIn("sst", row)
        self.assertNotIn("sst_anomaly", row)   # absent -> omitted (old point parity)

    def test_bbox_streaming_valid_json(self):
        r = self.client.get("/api/ghrsst", params={
            "lon0": 100.0, "lat0": 0.0, "lon1": 130.0, "lat1": 30.0,
            "start": "2025-01-01"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("application/json"))
        body = json.loads(r.content)          # must be a valid JSON array
        self.assertEqual(len(body), NY * NX)
        self.assertEqual(r.headers["x-served-rows"], str(NY * NX))
        nan_cell = next(x for x in body if abs(x["lon"] - float(np.linspace(100, 130, NX)[7])) < 1e-4
                        and abs(x["lat"] - float(np.linspace(0, 30, NY)[5])) < 1e-4)
        self.assertIsNone(nan_cell["sst"])

    def test_bbox_absent_field_null(self):
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 10.0, "lon1": 112.0, "lat1": 12.0,
            "start": "2025-01-01", "append": "sst,sst_anomaly"})
        body = json.loads(r.content)
        self.assertIn("sst_anomaly", body[0])           # bbox: key present
        self.assertIsNone(body[0]["sst_anomaly"])       # ...null

    def test_bbox_stride(self):
        r = self.client.get("/api/ghrsst", params={
            "lon0": 100.0, "lat0": 0.0, "lon1": 130.0, "lat1": 30.0,
            "start": "2025-01-01", "sample": 2})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-stride"], "2")
        self.assertEqual(len(json.loads(r.content)), ((NY - 1)//2 + 1) * ((NX - 1)//2 + 1))

    def test_sample_rejected_in_point_mode(self):
        r = self.client.get("/api/ghrsst", params={"lon0": 110.0, "lat0": 12.0, "sample": 2})
        self.assertEqual(r.status_code, 400)

    def test_post_points_contract(self):
        pts = [[100.0, 0.0], [130.0, 30.0], [100.0, 0.0], [115.0, 12.0]]  # dup at 0,2
        r = self.client.post("/api/ghrsst/points", json={
            "date": "2025-01-02", "points": pts, "append": "sst,sst_anomaly"})
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(len(body), len(pts))                       # N in == N out
        self.assertEqual([x["index"] for x in body], [0, 1, 2, 3])  # order + index
        self.assertEqual(body[0]["sst"], body[2]["sst"])            # dup preserved
        self.assertIn("sst_anomaly", body[0])                       # batch: key present
        self.assertIsNone(body[0]["sst_anomaly"])                   # ...null
        self.assertEqual(r.headers["cache-control"], "no-store")    # batch never cached

    def test_post_points_413(self):
        pts = [[110.0, 10.0]] * 1001
        r = self.client.post("/api/ghrsst/points", json={"date": "2025-01-02", "points": pts})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json()["detail"]["max_points"], 1000)

    def test_post_points_empty_400(self):
        r = self.client.post("/api/ghrsst/points", json={"date": "2025-01-02", "points": []})
        self.assertEqual(r.status_code, 400)

    def test_bad_field_400(self):
        r = self.client.get("/api/ghrsst", params={"lon0": 110.0, "lat0": 12.0, "append": "foo"})
        self.assertEqual(r.status_code, 400)

    def test_healthz(self):
        r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["latest"], "2025-01-03")

    def test_bbox_releases_permit_on_success(self):
        # [High] fix: the bbox stream holds ONE admission permit for its whole
        # lifecycle and releases it in finally -> no permit leak after completion.
        bex = app.state.bex
        self.assertEqual(bex.queue_depth(), 0)
        r = self.client.get("/api/ghrsst", params={
            "lon0": 100.0, "lat0": 0.0, "lon1": 130.0, "lat1": 30.0, "start": "2025-01-01"})
        self.assertEqual(r.status_code, 200)
        json.loads(r.content)                       # fully consume the stream
        self.assertEqual(bex.queue_depth(), 0)      # permit released

    def test_bbox_releases_permit_on_error(self):
        # error after acquire (point-limit ValueError -> 400) must still release.
        bex = app.state.bex
        store = app.state.store
        old = store.bbox_point_limit
        store.bbox_point_limit = 5                  # force bbox_arrays ValueError
        try:
            r = self.client.get("/api/ghrsst", params={
                "lon0": 100.0, "lat0": 0.0, "lon1": 130.0, "lat1": 30.0, "start": "2025-01-01"})
            self.assertEqual(r.status_code, 400)
        finally:
            store.bbox_point_limit = old
        self.assertEqual(bex.queue_depth(), 0)      # permit released on error path


class BoundedExecutorTests(unittest.TestCase):
    def test_overload_sheds(self):
        async def scenario():
            bex = BoundedExecutor(workers=1, queue_max=0, wait_ms=20)  # limit=1
            started = threading.Event()
            release = threading.Event()

            def block():
                started.set()
                release.wait(2.0)

            task1 = asyncio.create_task(bex.run(block))
            await asyncio.sleep(0.05)               # let task1 admit + start
            self.assertTrue(started.is_set())
            with self.assertRaises(Overloaded):     # no slot within wait_ms -> shed
                await bex.run(lambda: 1)
            release.set()
            await task1
            bex.shutdown()

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main(verbosity=2)
