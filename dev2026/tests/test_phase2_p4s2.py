"""dev2026 — P4-S2: opt-in CoverageJSON-lite bbox format (`format=coveragejson` / `format=raster`).

Gates (spec p4s2_raster_format_design.md): valid CoverageJSON Coverage(Grid) envelope; ranges are flat
row-major NdArray with null nodata; y axis descends (N->S); whole-field absent OMITS ranges.<var> +
field_status='absent'; SEMANTIC float32 parity vs the row `format=json` (incl. null-vs-absent); default
`format=json` unchanged; `raster` is an alias.

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_p4s2.py
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
from store.zarr_paths import group_path  # noqa: E402

NY, NX = 40, 50
_TMP = tempfile.mkdtemp(prefix="p4s2_")
_DAILY = os.path.join(_TMP, "daily")
_DAY = "2026-06-29"
FIELDS = ("sst", "sst_anomaly", "sea_ice")


def _build():
    g = zarr.open_group(group_path(_DAILY, _DAY), mode="w", zarr_format=3)
    g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,)); g["lon"][:] = np.linspace(120, 130, NX).astype(np.float32)
    g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,)); g["lat"][:] = np.linspace(10, 20, NY).astype(np.float32)
    sst = (285 + 0.01 * np.arange(NY)[:, None] + 0.001 * np.arange(NX)[None, :]).astype(np.float32)
    sst[20, 25] = np.nan                                              # land, INSIDE the bbox window
    g.create_array("sst", shape=(1, NY, NX), dtype="float32", chunks=(1, 16, 16)); g["sst"][0] = sst
    g.create_array("sea_ice", shape=(1, NY, NX), dtype="float32", chunks=(1, 16, 16)); g["sea_ice"][0] = np.zeros((NY, NX), np.float32)
    # NO sst_anomaly -> requested-but-absent


_build()
_BBOX = dict(lon0=121.0, lat0=11.0, lon1=129.0, lat1=19.0, append="sst,sst_anomaly,sea_ice", start=_DAY, end=_DAY)


def _f32eq(a, b):
    if a is None or b is None:
        return a is b
    return np.float32(a) == np.float32(b)


def _axis_vals(ax):
    n = ax["num"]
    if n == 1:
        return [ax["start"]]
    step = (ax["stop"] - ax["start"]) / (n - 1)
    return [ax["start"] + i * step for i in range(n)]


class P4S2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._prev = {k: os.environ.get(k) for k in ("GHRSST_ZARR_PATH", "GHRSST_ENABLE_COVERAGEJSON")}
        os.environ["GHRSST_ZARR_PATH"] = _DAILY
        os.environ["GHRSST_ENABLE_COVERAGEJSON"] = "1"       # P4-S2 flag ON for these tests
        from fastapi.testclient import TestClient
        import importlib, api.app as appmod
        importlib.reload(appmod)                             # pick up the env-driven Cfg
        cls.appmod = appmod
        cls.cm = TestClient(appmod.app); cls.c = cls.cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.cm.__exit__(None, None, None)
        for k, v in cls._prev.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        import importlib, api.app as appmod; importlib.reload(appmod)
        import shutil; shutil.rmtree(_TMP, ignore_errors=True)

    def _get(self, **extra):
        p = dict(_BBOX); p.update(extra)
        return self.c.get("/api/ghrsst", params=p)

    def test_structure(self):
        r = self._get(format="coveragejson")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["x-bbox-format"], "coveragejson")
        b = r.json()
        self.assertEqual(b["type"], "Coverage")
        self.assertEqual(b["profile"], "ghrsst-raster-json-1")
        self.assertEqual(b["domain"]["domainType"], "Grid")
        ax = b["domain"]["axes"]
        self.assertEqual({"x", "y", "t"}, set(ax))
        self.assertGreater(ax["y"]["start"], ax["y"]["stop"])              # N->S descending
        self.assertTrue(ax["t"]["values"][0].endswith("T09:00:00Z"))
        crs = b["domain"]["referencing"][0]["system"]["id"]
        self.assertEqual(crs, "http://www.opengis.net/def/crs/OGC/1.3/CRS84")
        nlon, nlat = ax["x"]["num"], ax["y"]["num"]
        sst = b["ranges"]["sst"]
        self.assertEqual(sst["axisNames"], ["t", "y", "x"])
        self.assertEqual(sst["shape"], [1, nlat, nlon])
        self.assertEqual(len(sst["values"]), nlat * nlon)                 # flat row-major
        self.assertIn(None, sst["values"])                               # land NaN -> null
        self.assertNotIn("sst_anomaly", b["ranges"])                     # absent -> omitted (never null)
        self.assertEqual(b["ghrsst:field_status"]["sst_anomaly"], "absent")
        self.assertEqual(b["ghrsst:field_status"]["sst"], "present")

    def test_parity_with_json_incl_null_vs_absent(self):
        # row json bbox order: k = ri*nlon + cj, ri = lat index ASCENDING (0=south), cj = lon index.
        # CoverageJSON: iy = 0 is NORTH (y descends), so ri = nlat-1-iy; ix = cj. Compare by index.
        rows = self._get(format="json").json()
        b = self._get(format="coveragejson").json()
        ax = b["domain"]["axes"]; nlon, nlat = ax["x"]["num"], ax["y"]["num"]
        self.assertEqual(len(rows), nlat * nlon)
        seen_null = False
        for iy in range(nlat):
            ri = nlat - 1 - iy
            for ix in range(nlon):
                row = rows[ri * nlon + ix]
                for f in FIELDS:
                    ref = row.get(f)                                    # row json: absent OR NaN -> null
                    if b["ghrsst:field_status"].get(f) == "present":
                        got = b["ranges"][f]["values"][iy * nlon + ix]
                    else:
                        got = None                                     # absent -> null (matches row json)
                    self.assertTrue(_f32eq(ref, got), f"{f} iy={iy} ix={ix}: {ref} != {got}")
                    seen_null = seen_null or (got is None and f == "sst")
        self.assertTrue(seen_null, "expected the land-NaN cell to appear as null in both")

    def test_raster_alias_canonical_header(self):
        r = self._get(format="raster")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-bbox-format"], "coveragejson")   # canonicalised (Codex #3)
        self.assertEqual(r.json()["profile"], "ghrsst-raster-json-1")

    def test_default_json_unchanged(self):
        r = self._get()
        self.assertEqual(r.headers["x-bbox-format"], "json")
        self.assertIsInstance(r.json(), list)

    def test_legacy_grid_columnar_not_public(self):
        for f in ("grid", "columnar"):
            self.assertEqual(self._get(format=f).status_code, 400, f)   # retired, not public

    def test_disabled_returns_400(self):
        # with the flag OFF, coveragejson/raster are rejected (deploy-safety until P4-S3)
        os.environ["GHRSST_ENABLE_COVERAGEJSON"] = "0"
        try:
            import importlib, api.app as appmod; importlib.reload(appmod)
            from fastapi.testclient import TestClient
            with TestClient(appmod.app) as c:
                for f in ("coveragejson", "raster"):
                    self.assertEqual(c.get("/api/ghrsst", params=dict(_BBOX, format=f)).status_code, 400, f)
                self.assertEqual(c.get("/api/ghrsst", params=dict(_BBOX)).status_code, 200)   # json still ok
        finally:
            os.environ["GHRSST_ENABLE_COVERAGEJSON"] = "1"
            import importlib, api.app as appmod; importlib.reload(appmod)


if __name__ == "__main__":
    unittest.main(verbosity=2)
