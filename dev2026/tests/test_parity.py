"""dev2026 — P1-S3 parity: NEW api/app.py vs the REAL old ghrsst_app.py.

Runs BOTH apps via TestClient against the same real store and compares response
DATA/shape/error semantics for queries within the old app's constraints.

Contract: this compares PARSED JSON equality (json() == json()), i.e. data-shape /
value parity — NOT byte-for-byte response bytes. The new bbox path streams JSON, so
whitespace / chunk boundaries differ by design; the parsed array and fields match. Also
asserts the INTENTIONAL divergences (these are deliberate P1 improvements, not
regressions): cache headers, and the lifted point-range cap (old 31-day clamp ->
new 365-day cap with 413).

Skipped unless GHRSST_ZARR_PATH points at a real mur.zarr (the old app needs the
proper root store; it reads the path at import time).

Run:
  GHRSST_ZARR_PATH=$(pwd)/bak/data/mur.zarr \
    dev2026/.venv/bin/python dev2026/tests/test_parity.py
"""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))            # dev2026  -> api, store
sys.path.insert(0, os.path.join(HERE, "..", ".."))      # repo root -> ghrsst_app

POINT = {"lon0": 119.30672, "lat0": 22.28274}


@unittest.skipUnless(os.environ.get("GHRSST_ZARR_PATH"),
                     "GHRSST_ZARR_PATH not set; skipping old-vs-new parity")
class ParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import ghrsst_app                      # OLD app (reads path at import)
        from api.app import app as new_app     # NEW app
        from store.store_access import StoreAccess

        cls.old_cm = TestClient(ghrsst_app.app)
        cls.new_cm = TestClient(new_app)
        cls.old = cls.old_cm.__enter__()
        cls.new = cls.new_cm.__enter__()

        days = StoreAccess().existing_days()
        if len(days) < 12:
            raise unittest.SkipTest("need >=12 days for parity")
        cls.latest = days[-1]
        cls.mid = days[-10]                    # a fixed day strictly before latest
        cls.mid_plus4 = days[-6]               # 5-day window mid..mid+4 (<=31, in-cap)

    @classmethod
    def tearDownClass(cls):
        cls.old_cm.__exit__(None, None, None)
        cls.new_cm.__exit__(None, None, None)

    def _both(self, params):
        o = self.old.get("/api/ghrsst", params=params)
        n = self.new.get("/api/ghrsst", params=params)
        return o, n

    def _assert_data_equal(self, params):
        o, n = self._both(params)
        self.assertEqual(o.status_code, 200, f"old {o.status_code}: {o.text[:200]}")
        self.assertEqual(n.status_code, 200, f"new {n.status_code}: {n.text[:200]}")
        self.assertEqual(o.json(), n.json(), f"data mismatch for {params}")
        return o, n

    # ---------------- DATA PARITY (in-cap queries) ----------------
    def test_point_default_latest(self):
        self._assert_data_equal({**POINT, "append": "sst,sst_anomaly,sea_ice"})

    def test_point_fixed_date(self):
        self._assert_data_equal({**POINT, "start": self.mid, "append": "sst,sea_ice"})

    def test_point_range_in_cap(self):
        self._assert_data_equal({**POINT, "start": self.mid, "end": self.mid_plus4,
                                 "append": "sst,sst_anomaly"})

    def test_point_mode_truncate(self):
        self._assert_data_equal({**POINT, "start": self.mid, "append": "sst,sst_anomaly",
                                 "mode": "truncate"})

    def test_bbox_small(self):
        self._assert_data_equal({"lon0": 119.0, "lat0": 21.0, "lon1": 119.5, "lat1": 21.5,
                                 "start": self.mid, "append": "sst,sea_ice"})

    def test_bbox_stride(self):
        self._assert_data_equal({"lon0": 119.0, "lat0": 21.0, "lon1": 120.0, "lat1": 22.0,
                                 "start": self.mid, "append": "sst", "sample": 2})

    # ---------------- ERROR-SEMANTICS PARITY ----------------
    def test_bad_field_both_400(self):
        o, n = self._both({**POINT, "append": "bogus"})
        self.assertEqual(o.status_code, 400)
        self.assertEqual(n.status_code, 400)

    def test_bad_date_both_400(self):
        o, n = self._both({**POINT, "start": "2026/01/01"})
        self.assertEqual(o.status_code, 400)
        self.assertEqual(n.status_code, 400)

    # ---------------- INTENTIONAL DIVERGENCES (documented) ----------------
    def test_divergence_date_less_cache_header(self):
        # NEW fixes the default-latest 10d-stale bug: must be no-store.
        o, n = self._both({**POINT})
        self.assertEqual(n.headers.get("cache-control"), "no-store")
        # old app sets no Cache-Control itself (nginx did) -> divergence is intentional.
        self.assertEqual(o.json(), n.json())  # data still identical

    def test_divergence_range_cap_lifted(self):
        # 40-day range: OLD clamps to 31 days; NEW returns the full range (<365).
        params = {**POINT, "start": self.latest, "append": "sst"}
        # build a 40-day window ending at latest, starting 39 days earlier
        from datetime import datetime, timedelta
        d_end = datetime.strptime(self.latest, "%Y-%m-%d").date()
        d_start = (d_end - timedelta(days=39)).isoformat()
        params = {**POINT, "start": d_start, "end": self.latest, "append": "sst"}
        o, n = self._both(params)
        self.assertEqual(o.status_code, 200)
        self.assertEqual(n.status_code, 200)
        self.assertLessEqual(len(o.json()), 31)        # old clamp
        self.assertGreater(len(n.json()), 31)          # new lifted the cap
        self.assertLessEqual(len(n.json()), 40)

    def test_divergence_over_max_days_413(self):
        # > 365-day requested span: NEW returns 413; OLD clamps to 31 (200).
        o, n = self._both({**POINT, "start": "2024-01-01", "end": "2026-01-01", "append": "sst"})
        self.assertEqual(n.status_code, 413)
        self.assertIn(o.status_code, (200, 400))       # old does not 413 (clamps or empty)


if __name__ == "__main__":
    unittest.main(verbosity=2)
