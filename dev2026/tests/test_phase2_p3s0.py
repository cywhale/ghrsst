"""dev2026 — P3-S0: the prototype row/columnar/grid encoders must be SEMANTICALLY equivalent.

S0 ships no API change, but the bench encoders are the basis for the S1 `format=grid`/`columnar`
encoders, so lock their semantics now: all three reconstruct the SAME (lon,lat,value) data with the
SAME null-for-NaN / absent semantics as the current row path; absent var -> field_status 'absent'
(not a giant null array); present-but-NaN cell -> null inside the array.

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_p3s0.py
"""
from __future__ import annotations

import os
import sys
import unittest

import numpy as np
import orjson

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from bench.bench_bbox_wire import _enc_columnar, _enc_grid, _enc_row, VARS  # noqa: E402


def _cols(present=VARS):
    rng = np.random.default_rng(1)
    base = {}
    for k, v in enumerate(VARS):
        if v not in present:
            continue
        a = (5 + k + 0.01 * np.arange(4)[:, None] + 0.001 * np.arange(5)[None, :]).astype(np.float32)
        a[1, 2] = np.nan                       # land/masked NaN -> null
        base[v] = a
    return base


def _recon_row(rows):
    return [(r["lon"], r["lat"], {f: r.get(f) for f in VARS}) for r in rows]


def _recon_columnar(payload):
    o = orjson.loads(payload)
    n = len(o["lon"])
    out = []
    for i in range(n):
        vals = {f: (o["fields"][f][i] if o["field_status"][f] == "present" else None) for f in VARS}
        out.append((o["lon"][i], o["lat"][i], vals))
    return out


def _recon_grid(payload):
    o = orjson.loads(payload)
    nlat, nlon = o["shape"]
    out = []
    for i in range(nlat):
        for j in range(nlon):
            vals = {f: (o["fields"][f][i][j] if o["field_status"][f] == "present" else None) for f in VARS}
            out.append((o["lon"][j], o["lat"][i], vals))
    return out


class P3S0EncoderParity(unittest.TestCase):
    def setUp(self):
        self.lons = np.linspace(120, 121, 5).astype(np.float32)
        self.lats = np.linspace(20, 21, 4).astype(np.float32)
        self.day = "2026-01-01"

    def _check(self, cols):
        rows = _enc_row(self.lons, self.lats, cols, self.day)
        ref = _recon_row(rows)
        col = _recon_columnar(_enc_columnar(self.lons, self.lats, cols, self.day))
        grid = _recon_grid(_enc_grid(self.lons, self.lats, cols, self.day))
        self.assertEqual(len(ref), self.lons.size * self.lats.size)
        # same ordering, same lon/lat, same per-field values incl. null-for-NaN/absent
        def same(x, y):
            # semantic (not byte) equality: same float32 datum, or both null. The row path emits
            # float(np.float32) (long decimal) while orjson numpy emits the shortest round-trip; both
            # are the SAME float32 value (spec §6: default JSON unchanged, new formats semantic-equal).
            if x is None or y is None:
                return x is y
            return np.float32(x) == np.float32(y)

        for a, b, c in zip(ref, col, grid):
            self.assertTrue(same(a[0], b[0]) and same(a[0], c[0]), "lon")
            self.assertTrue(same(a[1], b[1]) and same(a[1], c[1]), "lat")
            for f in VARS:
                self.assertTrue(same(a[2][f], b[2][f]), f"columnar {f}")
                self.assertTrue(same(a[2][f], c[2][f]), f"grid {f}")

    def test_all_vars_present_with_nan(self):
        self._check(_cols())

    def test_absent_var_field_status(self):
        cols = _cols(present=("sst", "sea_ice"))            # sst_anomaly absent
        # absent var -> compact: fields[var] is null + field_status 'absent' (NOT a 2-D null array)
        for enc in (_enc_columnar, _enc_grid):
            o = orjson.loads(enc(self.lons, self.lats, cols, self.day))
            self.assertIsNone(o["fields"]["sst_anomaly"])
            self.assertEqual(o["field_status"]["sst_anomaly"], "absent")
            self.assertEqual(o["field_status"]["sst"], "present")
        # and reconstruction still matches the row path (absent -> null per point)
        self._check(cols)


if __name__ == "__main__":
    unittest.main(verbosity=2)
