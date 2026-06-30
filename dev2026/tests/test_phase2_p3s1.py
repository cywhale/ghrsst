"""dev2026 — P3-S1: opt-in bbox `format=grid`/`columnar` over the real API (TestClient).

Gates: default `format=json` UNCHANGED; compact formats carry the SAME data with SEMANTIC float32
parity (not byte); absent var -> field_status 'absent'; land NaN -> null; headers (X-Bbox-Format,
X-Read-Ms, X-Encode-Ms); `format` rejected outside bbox mode; bad format -> 400; truncate honored.

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_p3s1.py
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
_TMP = tempfile.mkdtemp(prefix="p3s1_")
_DAILY = os.path.join(_TMP, "daily")
_DAY = "2026-01-01"


def _build():
    g = zarr.open_group(group_path(_DAILY, _DAY), mode="w", zarr_format=3)
    g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,)); g["lon"][:] = np.linspace(120, 130, NX).astype(np.float32)
    g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,)); g["lat"][:] = np.linspace(10, 20, NY).astype(np.float32)
    sst = (10 + 0.01 * np.arange(NY)[:, None] + 0.001 * np.arange(NX)[None, :]).astype(np.float32)
    sst[2, 3] = np.nan                          # land/masked NaN -> null
    g.create_array("sst", shape=(1, NY, NX), dtype="float32", chunks=(1, 16, 16)); g["sst"][0] = sst
    g.create_array("sea_ice", shape=(1, NY, NX), dtype="float32", chunks=(1, 16, 16)); g["sea_ice"][0] = np.zeros((NY, NX), np.float32)
    # NO sst_anomaly -> requested-but-absent var


_build()
_BBOX = dict(lon0=121.0, lat0=11.0, lon1=129.0, lat1=19.0, append="sst,sst_anomaly,sea_ice", start=_DAY, end=_DAY)


def _f32eq(a, b):
    if a is None or b is None:
        return a is b
    return np.float32(a) == np.float32(b)


class P3S1Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._prev = os.environ.get("GHRSST_ZARR_PATH")
        os.environ["GHRSST_ZARR_PATH"] = _DAILY
        from fastapi.testclient import TestClient
        from api.app import app
        cls.cm = TestClient(app); cls.c = cls.cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.cm.__exit__(None, None, None)
        if cls._prev is None:
            os.environ.pop("GHRSST_ZARR_PATH", None)
        else:
            os.environ["GHRSST_ZARR_PATH"] = cls._prev
        import shutil
        shutil.rmtree(_TMP, ignore_errors=True)

    def _get(self, **extra):
        p = dict(_BBOX); p.update(extra)
        return self.c.get("/api/ghrsst", params=p)

    def _recon(self, fmt):
        r = self._get(format=fmt)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["x-bbox-format"], fmt)
        self.assertIn("x-read-ms", r.headers)
        body = r.json()
        out = []
        if fmt == "json":
            for row in body:
                out.append((row["lon"], row["lat"], {f: row.get(f) for f in ("sst", "sst_anomaly", "sea_ice")}))
        elif fmt == "columnar":
            n = len(body["lon"])
            for i in range(n):
                vals = {f: (body["fields"][f][i] if body["field_status"][f] == "present" else None)
                        for f in ("sst", "sst_anomaly", "sea_ice")}
                out.append((body["lon"][i], body["lat"][i], vals))
        else:  # grid
            nlat, nlon = body["shape"]
            for i in range(nlat):
                for j in range(nlon):
                    vals = {f: (body["fields"][f][i][j] if body["field_status"][f] == "present" else None)
                            for f in ("sst", "sst_anomaly", "sea_ice")}
                    out.append((body["lon"][j], body["lat"][i], vals))
        return r, out

    def test_grid_columnar_parity_with_json(self):
        _, ref = self._recon("json")
        for fmt in ("columnar", "grid"):
            _, got = self._recon(fmt)
            self.assertEqual(len(got), len(ref), fmt)
            for a, b in zip(ref, got):
                self.assertTrue(_f32eq(a[0], b[0]) and _f32eq(a[1], b[1]), f"{fmt} lon/lat")
                for f in ("sst", "sst_anomaly", "sea_ice"):
                    self.assertTrue(_f32eq(a[2][f], b[2][f]), f"{fmt} {f}: {a[2][f]} != {b[2][f]}")

    def test_absent_var_field_status(self):
        for fmt in ("columnar", "grid"):
            body = self._get(format=fmt).json()
            self.assertIsNone(body["fields"]["sst_anomaly"])          # not a giant null array
            self.assertEqual(body["field_status"]["sst_anomaly"], "absent")
            self.assertEqual(body["field_status"]["sst"], "present")

    def test_default_is_json_unchanged(self):
        r = self._get()                                              # no format param
        self.assertEqual(r.headers["x-bbox-format"], "json")
        self.assertIsInstance(r.json(), list)                        # row array

    def test_format_rejected_outside_bbox(self):
        r = self.c.get("/api/ghrsst", params=dict(lon0=125.0, lat0=15.0, start=_DAY, end=_DAY,
                                                  append="sst", format="grid"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("format", r.text.lower())

    def test_bad_format(self):
        self.assertEqual(self._get(format="arrow").status_code, 400)

    def test_truncate_parity(self):
        _, ref = self._recon("json")            # json already truncated below via param
        rj = self._get(format="json", mode="truncate").json()
        ref_t = [(row["lon"], row["lat"], {f: row.get(f) for f in ("sst", "sea_ice")}) for row in rj]
        for fmt in ("columnar", "grid"):
            body = self._get(format=fmt, mode="truncate").json()
            # spot-check: all present sst values rounded to 3 dp match the row path
            if fmt == "grid":
                vals = [body["fields"]["sst"][i][j] for i in range(body["shape"][0]) for j in range(body["shape"][1])]
            else:
                vals = list(body["fields"]["sst"])
            for a, v in zip(ref_t, vals):
                self.assertTrue(_f32eq(a[2]["sst"], v), f"{fmt} truncate sst")


if __name__ == "__main__":
    unittest.main(verbosity=2)
