"""dev2026 — P4-S3 endpoint wiring: spatial-window (delta-only) enforcement on bbox + POST /points.

Adopted policy (spec §4.1): spatial queries (bbox + POST /points) are served ONLY for days in the delta
window; older -> clear 4xx. Point time-series / range and single-day POINT GET keep full history (NOT
gated). Enforcement is behind GHRSST_SPATIAL_WINDOW_ENFORCE (default off = current behaviour).

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_p4s3gate.py
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
from ingest.build_timecube_bulk import build_timecube_bulk  # noqa: E402
from ingest.dual_write import append_to_delta  # noqa: E402

NY, NX = 40, 50
VARS = ("sst", "sst_anomaly", "sea_ice")


def _build(tmp):
    daily = os.path.join(tmp, "daily")
    days = [(datetime.date(2026, 6, 20) + datetime.timedelta(days=i)).isoformat() for i in range(6)]
    lon = np.linspace(120, 130, NX).astype(np.float32); lat = np.linspace(10, 20, NY).astype(np.float32)
    for i, d in enumerate(days):
        g = zarr.open_group(group_path(daily, d), mode="w", zarr_format=3)
        g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,)); g["lon"][:] = lon
        g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,)); g["lat"][:] = lat
        for v in VARS:
            fld = (285 + 0.01 * i + 0.001 * np.arange(NY)[:, None]).repeat(NX, 1)[:, :NX].astype(np.float32)
            g.create_array(v, shape=(1, NY, NX), dtype="float32", chunks=(1, 16, 16)); g[v][0] = fld
    base = os.path.join(tmp, "base")
    build_timecube_bulk(daily, base, spatial_chunk=8, time_chunk=90, shard_spatial=16, read_block=16,
                        workers=2, end_day=days[3])
    delta = os.path.join(tmp, "delta")                       # delta window = the 2 most-recent days
    append_to_delta(daily, delta, days[4], spatial_chunk=16, shard_spatial=16)
    append_to_delta(daily, delta, days[5], spatial_chunk=16, shard_spatial=16)
    return daily, base, delta, days


def _client(enforce, coveragejson="0", with_delta=True):
    tmp = tempfile.mkdtemp(prefix="p4s3g_")
    daily, base, delta, days = _build(tmp)
    os.environ.update({"GHRSST_ZARR_PATH": daily, "GHRSST_TIMECUBE_PATH": base,
                       "GHRSST_SPATIAL_WINDOW_ENFORCE": enforce, "GHRSST_ENABLE_COVERAGEJSON": coveragejson})
    if with_delta:
        os.environ["GHRSST_DELTACUBE_PATH"] = delta
    else:
        os.environ.pop("GHRSST_DELTACUBE_PATH", None)   # no delta tier -> gate no-op (transition safety)
    import api.app as appmod
    importlib.reload(appmod)
    from fastapi.testclient import TestClient
    return TestClient(appmod.app), days, tmp


class SpatialGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._prev = {k: os.environ.get(k) for k in
                     ("GHRSST_ZARR_PATH", "GHRSST_TIMECUBE_PATH", "GHRSST_DELTACUBE_PATH",
                      "GHRSST_SPATIAL_WINDOW_ENFORCE", "GHRSST_ENABLE_COVERAGEJSON")}

    @classmethod
    def tearDownClass(cls):
        for k, v in cls._prev.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        import api.app as appmod; importlib.reload(appmod)

    def _bbox(self, c, day, fmt=None):
        p = dict(lon0=121, lat0=11, lon1=129, lat1=19, append="sst,sea_ice", start=day, end=day)
        if fmt:
            p["format"] = fmt
        return c.get("/api/ghrsst", params=p)

    def _post(self, c, day):
        return c.post("/api/ghrsst/points", json={"date": day, "points": [[125.0, 15.0]], "append": "sst"})

    def test_enforced_window(self):
        c, days, tmp = _client("1")
        try:
            with c:
                recent, older = days[5], days[1]                 # recent in delta; older in daily+base only
                self.assertEqual(self._bbox(c, recent).status_code, 200)        # bbox delta day -> ok
                r = self._bbox(c, older)
                self.assertEqual(r.status_code, 400)                            # bbox older -> 4xx
                self.assertIn("available_spatial_window", r.text)
                self.assertEqual(self._post(c, recent).status_code, 200)        # POST delta day -> ok
                self.assertEqual(self._post(c, older).status_code, 400)         # POST older -> 4xx
                # NOT gated: single-day POINT GET + multi-day range keep full history
                self.assertEqual(c.get("/api/ghrsst", params=dict(lon0=125, lat0=15, start=older, end=older,
                                                                  append="sst")).status_code, 200)
                self.assertEqual(c.get("/api/ghrsst", params=dict(lon0=125, lat0=15, start=days[0], end=days[5],
                                                                  append="sst")).status_code, 200)
                h = c.get("/healthz").json()
                self.assertTrue(h["spatial_window_enforce"])
                self.assertEqual(h["spatial_window"], [days[4], days[5]])
        finally:
            import shutil; shutil.rmtree(tmp, ignore_errors=True)

    def test_coveragejson_respects_the_gate(self):
        # DEPLOYMENT-RISK PATH: both flags ON. The compact format must NOT bypass the window gate.
        c, days, tmp = _client("1", coveragejson="1")
        try:
            with c:
                recent, older = days[5], days[1]
                r = self._bbox(c, recent, "coveragejson")
                self.assertEqual(r.status_code, 200)                            # delta day -> served
                self.assertEqual(r.json()["type"], "Coverage")
                self.assertEqual(r.headers["x-bbox-format"], "coveragejson")
                r2 = self._bbox(c, older, "coveragejson")
                self.assertEqual(r2.status_code, 400)                           # older -> gated, not served
                self.assertIn("available_spatial_window", r2.text)
                # raster alias is gated too, and canonicalises the header when served
                self.assertEqual(self._bbox(c, older, "raster").status_code, 400)
                r4 = self._bbox(c, recent, "raster")
                self.assertEqual(r4.status_code, 200)
                self.assertEqual(r4.headers["x-bbox-format"], "coveragejson")
        finally:
            import shutil; shutil.rmtree(tmp, ignore_errors=True)

    def test_transition_safety_no_delta_tier(self):
        # enforcement ON but NO delta tier loaded -> gate is a no-op (current behaviour preserved)
        c, days, tmp = _client("1", coveragejson="1", with_delta=False)
        try:
            with c:
                self.assertEqual(self._bbox(c, days[1]).status_code, 200)       # any day still served
                self.assertEqual(self._post(c, days[1]).status_code, 200)
                h = c.get("/healthz").json()
                self.assertTrue(h["spatial_window_enforce"])
                self.assertIsNone(h["spatial_window"])                          # no delta -> no window
        finally:
            import shutil; shutil.rmtree(tmp, ignore_errors=True)

    def test_disabled_default_serves_any_day(self):
        c, days, tmp = _client("0")
        try:
            with c:
                self.assertEqual(self._bbox(c, days[1]).status_code, 200)       # older bbox ok when off
                self.assertEqual(self._post(c, days[1]).status_code, 200)
                self.assertFalse(c.get("/healthz").json()["spatial_window_enforce"])
        finally:
            import shutil; shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
