"""dev2026 — P4-S0b: spatial-window policy helper (delta membership == spatial availability)."""
from __future__ import annotations

import os
import sys
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from store import spatial_policy  # noqa: E402


class SpatialPolicy(unittest.TestCase):
    DELTA = ["2026-06-01", "2026-06-02", "2026-06-03"]

    def test_membership_is_availability(self):
        self.assertTrue(spatial_policy.spatial_day_allowed("2026-06-02", self.DELTA))
        self.assertFalse(spatial_policy.spatial_day_allowed("2026-05-31", self.DELTA))   # older/base -> rejected
        self.assertFalse(spatial_policy.spatial_day_allowed("2026-07-01", self.DELTA))   # not present

    def test_window_bounds(self):
        self.assertEqual(spatial_policy.spatial_window_bounds(self.DELTA), ("2026-06-01", "2026-06-03"))
        self.assertIsNone(spatial_policy.spatial_window_bounds([]))

    def test_classify(self):
        c = spatial_policy.classify_days(["2026-05-31", "2026-06-02", "2026-06-03"], self.DELTA)
        self.assertEqual(c["served"], ["2026-06-02", "2026-06-03"])
        self.assertEqual(c["rejected"], ["2026-05-31"])

    def test_rejection_payload(self):
        p = spatial_policy.rejection_payload("2026-05-31", self.DELTA)
        self.assertEqual(p["requested_day"], "2026-05-31")
        self.assertEqual(p["available_spatial_window"], "2026-06-01..2026-06-03")
        self.assertIn("spatial_window_days", p)
        self.assertIn("hint", p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
