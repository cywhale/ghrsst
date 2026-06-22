"""dev2026 — P2-S0 harness reliability tests.

Proves the fixture builder + micro-cost bench are trustworthy BEFORE any E1/F/time-cube
work: (1) longest_contiguous is correct; (2) build_fixture produces N contiguous days with
correct meta; (3) micro-cost's cache-independent metrics are exact (chunk_count = days*nf,
decompressed = chunk_count * containing-chunk bytes) and synthetic fixtures are never
promotion-eligible.

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_s0.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "bench"))
from bench_timecube_microcost import longest_contiguous  # noqa: E402

PY = sys.executable
INGEST = os.path.join(HERE, "..", "ingest", "build_fixture.py")
MICRO = os.path.join(HERE, "..", "bench", "bench_timecube_microcost.py")


class LongestContiguousTests(unittest.TestCase):
    def test_all_contiguous(self):
        self.assertEqual(longest_contiguous(["2024-01-01", "2024-01-02", "2024-01-03"]),
                         ("2024-01-01", "2024-01-03", 3))

    def test_with_gap(self):
        days = ["2024-01-01", "2024-01-02", "2024-01-05", "2024-01-06", "2024-01-07"]
        self.assertEqual(longest_contiguous(days), ("2024-01-05", "2024-01-07", 3))

    def test_month_boundary(self):
        self.assertEqual(longest_contiguous(["2024-01-31", "2024-02-01"]),
                         ("2024-01-31", "2024-02-01", 2))

    def test_single_and_empty(self):
        self.assertEqual(longest_contiguous(["2024-01-01"]), ("2024-01-01", "2024-01-01", 1))
        self.assertEqual(longest_contiguous([]), (None, None, 0))


class FixtureAndMicrocostTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="p2s0_")
        cls.store = os.path.join(cls.tmp, "fix")
        cls.ny = cls.nx = 16
        cls.chunk = 8                 # -> 2x2 chunk grid; point in chunk (0,0) => 8x8 extent
        cls.days = 6
        r = subprocess.run([PY, INGEST, "--out", cls.store, "--days", str(cls.days),
                            "--ny", str(cls.ny), "--nx", str(cls.nx), "--chunk", str(cls.chunk),
                            "--start", "2024-03-01"], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        cls.out = os.path.join(cls.tmp, "micro.json")
        r2 = subprocess.run([PY, MICRO, "--store", cls.store, "--lon", "100", "--lat", "0",
                             "--days", str(cls.days), "--out", cls.out],
                            capture_output=True, text=True)
        assert r2.returncode == 0, r2.stderr
        with open(cls.out) as fh:
            cls.res = json.load(fh)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_fixture_meta(self):
        with open(os.path.join(self.store, "fixture_meta.json")) as fh:
            meta = json.load(fh)
        self.assertEqual(meta["days"], self.days)
        self.assertTrue(meta["contiguous"])
        self.assertFalse(meta["promotion_gate_eligible"])
        self.assertEqual(meta["composition"], {"real": 0, "synthetic": self.days})

    def test_chunk_count_is_O_days(self):
        ci = self.res["cache_independent"]
        nf = len(self.res["meta"]["fields"])           # 3 vars
        self.assertEqual(nf, 3)
        self.assertEqual(ci["chunk_count_per_query"], self.days * nf)   # O(days)

    def test_decompressed_bytes_exact(self):
        ci = self.res["cache_independent"]
        # point at lon=100,lat=0 -> nearest idx (0,0) -> chunk (0,0) extent = chunk x chunk
        ext = self.chunk * self.chunk * 4
        self.assertEqual(ci["decompressed_bytes_per_query"], ci["chunk_count_per_query"] * ext)
        self.assertEqual(ci["useful_bytes"], self.days * 3 * 4)
        self.assertEqual(ci["read_amplification"],
                         round(ci["decompressed_bytes_per_query"] / ci["useful_bytes"], 1))

    def test_not_promotion_eligible_synthetic(self):
        self.assertFalse(self.res["meta"]["promotion_gate_eligible"])
        self.assertEqual(self.res["meta"]["longest_contiguous"]["len"], self.days)

    def test_measured_fields_present(self):
        me = self.res["measured"]
        self.assertIn("p50", me["latency_ms"])
        self.assertGreaterEqual(me["rows_returned"], 1)
        self.assertIsNotNone(me["py_alloc_peak_mb"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
