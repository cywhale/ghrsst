"""dev2026 — P1-S1 acceptance tests for store_access.StoreAccess.

Covers (spec 01 P1-S1 acceptance):
  * parity with the old per-day xr.open_zarr read path (real store; skipped if
    GHRSST_ZARR_PATH unset) — point_series / points_batch / bbox.
  * new-day visibility after warmup (filesystem rescan via TTL).
  * thread-safety stress: concurrent point/batch/bbox with LRU eviction churn,
    results must equal the single-threaded reference, no exceptions.
  * batch contract: N in == N out, order preserved, index correct, dupes kept,
    fan-out limit enforced.
  * single-request memory bound (per-chunk release, not whole-series resident).

Run:
  dev2026/.venv/bin/python -m unittest -v dev2026.tests.test_store_access
  # or from dev2026/:  ../dev2026/.venv/bin/python -m unittest -v tests.test_store_access
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import tracemalloc
import unittest

import numpy as np
import zarr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from store.store_access import StoreAccess, _nearest_idx  # noqa: E402
from store.zarr_paths import group_path  # noqa: E402

NY, NX = 40, 50            # small grid
CY, CX = 16, 16            # small chunks -> multiple chunks across the grid


def _sst_value(i, j):
    # deterministic field with one NaN (land) cell
    if i == 5 and j == 7:
        return np.float32(np.nan)
    return np.float32(10.0 + 0.01 * i + 0.001 * j)


def build_synthetic_day(root, day):
    """Create root/YYYY/MM/DD as a zarr v3 group with lon/lat/sst/sea_ice."""
    lon = np.linspace(100.0, 130.0, NX).astype(np.float32)
    lat = np.linspace(0.0, 30.0, NY).astype(np.float32)
    g = zarr.open_group(group_path(root, day), mode="w", zarr_format=3)
    g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,))
    g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,))
    g.create_array("sst", shape=(1, NY, NX), dtype="float32", chunks=(1, CY, CX))
    g.create_array("sea_ice", shape=(1, NY, NX), dtype="float32", chunks=(1, CY, CX))
    g["lon"][:] = lon
    g["lat"][:] = lat
    sst = np.fromfunction(np.vectorize(_sst_value), (NY, NX), dtype=int).astype(np.float32)
    g["sst"][0, :, :] = sst
    g["sea_ice"][0, :, :] = np.zeros((NY, NX), np.float32)
    return lon, lat, sst


class SyntheticStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ghrsst_test_")
        self.days = ["2020-01-01", "2020-01-02", "2020-01-03"]
        for d in self.days:
            self.lon, self.lat, self.sst = build_synthetic_day(self.tmp, d)
        self.sa = StoreAccess(zarr_path=self.tmp, day_ttl_seconds=0.0, lru_max=2)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_point_series_values(self):
        lon_q, lat_q = 117.3, 12.4
        ii = _nearest_idx(lat_q, self.lat)
        jj = _nearest_idx(lon_q, self.lon)
        rows = self.sa.point_series(lon_q, lat_q, self.days, ["sst", "sea_ice"])
        self.assertEqual([r["date"] for r in rows], self.days)
        exp = self.sst[ii, jj]
        for r in rows:
            if np.isnan(exp):
                self.assertIsNone(r["sst"])
            else:
                self.assertAlmostEqual(r["sst"], float(exp), places=5)
            self.assertEqual(r["lon"], float(self.lon[jj]))
            self.assertEqual(r["lat"], float(self.lat[ii]))

    def test_point_series_skips_missing_day(self):
        rows = self.sa.point_series(118.0, 10.0, ["2020-01-02", "2019-12-31"], ["sst"])
        self.assertEqual([r["date"] for r in rows], ["2020-01-02"])

    def test_point_series_omits_absent_field(self):
        # synthetic days have NO sst_anomaly -> old point path omits the key
        rows = self.sa.point_series(118.0, 10.0, ["2020-01-01"], ["sst", "sst_anomaly"])
        self.assertIn("sst", rows[0])
        self.assertNotIn("sst_anomaly", rows[0])   # absent field omitted (old-parity)

    def test_batch_nulls_absent_field(self):
        rows = self.sa.points_batch([[118.0, 10.0]], "2020-01-01", ["sst", "sst_anomaly"])
        self.assertIn("sst_anomaly", rows[0])       # NEW endpoint: key present
        self.assertIsNone(rows[0]["sst_anomaly"])   # ...with null

    def test_bbox_nulls_absent_field(self):
        rows = [r for b in self.sa.bbox_batches(
            "2020-01-01", 110.0, 10.0, 112.0, 12.0, ["sst", "sst_anomaly"]) for r in b]
        self.assertIn("sst_anomaly", rows[0])       # matches old bbox: key present
        self.assertIsNone(rows[0]["sst_anomaly"])

    def test_bbox_point_limit(self):
        sa = StoreAccess(zarr_path=self.tmp, day_ttl_seconds=0.0, bbox_point_limit=10)
        with self.assertRaises(ValueError):
            list(sa.bbox_batches("2020-01-01", 100.0, 0.0, 130.0, 30.0, ["sst"]))  # 2000 pts

    def test_batch_order_index_dupes(self):
        pts = [[100.0, 0.0], [130.0, 30.0], [100.0, 0.0], [115.0, 12.0]]  # dup at 0 and 2
        rows = self.sa.points_batch(pts, "2020-01-01", ["sst"])
        self.assertEqual(len(rows), len(pts))               # N in == N out
        self.assertEqual([r["index"] for r in rows], [0, 1, 2, 3])  # order + index
        self.assertEqual(rows[0]["lon"], rows[2]["lon"])    # dup preserved
        self.assertEqual(rows[0]["lat"], rows[2]["lat"])
        self.assertEqual(rows[0]["sst"], rows[2]["sst"])

    def test_batch_matches_point_reads(self):
        pts = [[101.0, 1.0], [123.0, 7.0], [110.0, 25.0], [128.5, 29.0]]
        batch = self.sa.points_batch(pts, "2020-01-02", ["sst", "sea_ice"])
        for k, (lo, la) in enumerate(pts):
            single = self.sa.point_series(lo, la, ["2020-01-02"], ["sst", "sea_ice"])[0]
            self.assertEqual(batch[k]["sst"], single["sst"])
            self.assertEqual(batch[k]["sea_ice"], single["sea_ice"])
            self.assertEqual(batch[k]["lon"], single["lon"])

    def test_batch_fanout_limit(self):
        sa = StoreAccess(zarr_path=self.tmp, day_ttl_seconds=0.0, batch_chunk_fanout_max=1)
        # two points in clearly different 16x16 chunks
        pts = [[100.0, 0.0], [130.0, 30.0]]
        with self.assertRaises(ValueError):
            sa.points_batch(pts, "2020-01-01", ["sst"])

    def test_batch_points_max(self):
        sa = StoreAccess(zarr_path=self.tmp, day_ttl_seconds=0.0, points_batch_max=3)
        with self.assertRaises(ValueError):
            sa.points_batch([[100.0, 0.0]] * 4, "2020-01-01", ["sst"])

    def test_new_day_visibility(self):
        # warm: coords cached + LRU has an old day
        _ = self.sa.point_series(110.0, 10.0, ["2020-01-01"], ["sst"])
        self.assertFalse(self.sa.day_present("2020-01-09"))
        build_synthetic_day(self.tmp, "2020-01-09")          # new day appears
        self.assertTrue(self.sa.day_present("2020-01-09"))    # TTL=0 -> rescan sees it
        rows = self.sa.point_series(110.0, 10.0, ["2020-01-09"], ["sst"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2020-01-09")

    def test_bbox_batches_full_grid(self):
        batches = list(self.sa.bbox_batches(
            "2020-01-01", 100.0, 0.0, 130.0, 30.0, ["sst"], batch_rows=137))
        rows = [r for b in batches for r in b]
        self.assertEqual(len(rows), NY * NX)
        # spot-check a NaN cell (i=5, j=7) is null
        target = next(r for r in rows
                      if r["lon"] == float(self.lon[7]) and r["lat"] == float(self.lat[5]))
        self.assertIsNone(target["sst"])

    def test_thread_safety_stress(self):
        # lru_max=2 with 3 days forces eviction churn under concurrency
        pts = [[100.0 + k, float(k)] for k in range(8)]
        # single-threaded reference
        ref_series = {d: self.sa.point_series(110.0, 12.0, [d], ["sst", "sea_ice"])[0]
                      for d in self.days}
        ref_batch = {d: self.sa.points_batch(pts, d, ["sst"]) for d in self.days}

        errors = []
        mismatches = []

        def worker(n):
            try:
                for _ in range(40):
                    d = self.days[n % len(self.days)]
                    s = self.sa.point_series(110.0, 12.0, [d], ["sst", "sea_ice"])[0]
                    if s != ref_series[d]:
                        mismatches.append(("series", d, s))
                    b = self.sa.points_batch(pts, d, ["sst"])
                    if b != ref_batch[d]:
                        mismatches.append(("batch", d))
                    list(self.sa.bbox_batches(d, 100.0, 0.0, 130.0, 30.0, ["sst"]))
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [], f"exceptions under concurrency: {errors}")
        self.assertEqual(mismatches, [], f"value mismatches under concurrency: {mismatches[:5]}")


@unittest.skipUnless(os.environ.get("GHRSST_ZARR_PATH"),
                     "GHRSST_ZARR_PATH not set; skipping real-store parity")
class RealStoreParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import xarray as xr
        cls.xr = xr
        cls.sa = StoreAccess()
        cls.days = cls.sa.existing_days()
        if not cls.days:
            raise unittest.SkipTest("no days in store")

    def _ref_point(self, day, ii, jj, fields):
        ds = self.xr.open_zarr(self.sa.root, group="/" + day.replace("-", "/"),
                               zarr_format=3, consolidated=None)
        out = {}
        for f in fields:
            if f in ds:
                v = ds[f].isel(lat=ii, lon=jj).compute().values.item()
                out[f] = None if (v is None or np.isnan(v)) else float(v)
        ds.close()
        return out

    def test_point_series_parity(self):
        lon_q, lat_q = 119.30672, 22.28274
        days = self.days[-min(30, len(self.days)):]
        ii, jj, _, _ = self.sa.nearest_indices(lon_q, lat_q)
        fields = ["sst", "sea_ice"]
        rows = self.sa.point_series(lon_q, lat_q, days, fields)
        for r in rows:
            ref = self._ref_point(r["date"], ii, jj, fields)
            for f in fields:
                self.assertEqual(r.get(f), ref.get(f), f"{r['date']} {f}")

    def test_point_series_anomaly_key_parity(self):
        # absent sst_anomaly must be OMITTED (not null) to match old point path;
        # compare KEY PRESENCE per day, not just .get() values.
        lon_q, lat_q = 119.30672, 22.28274
        days = self.days[-min(30, len(self.days)):]
        ii, jj, _, _ = self.sa.nearest_indices(lon_q, lat_q)
        fields = ["sst", "sst_anomaly"]
        rows = self.sa.point_series(lon_q, lat_q, days, fields)
        checked_absent = 0
        for r in rows:
            ds = self.xr.open_zarr(self.sa.root, group="/" + r["date"].replace("-", "/"),
                                   zarr_format=3, consolidated=None)
            present = "sst_anomaly" in ds
            ds.close()
            self.assertEqual("sst_anomaly" in r, present, f"{r['date']} key mismatch")
            if not present:
                checked_absent += 1
        # informational: how many days actually exercised the absent branch
        print(f"  [anomaly_key_parity] {checked_absent}/{len(rows)} days had no sst_anomaly")

    def test_points_batch_parity(self):
        day = self.days[-1]
        pts = [[119.3, 22.3], [121.0, 23.5], [120.5, 24.0], [118.0, 21.0]]
        rows = self.sa.points_batch(pts, day, ["sst", "sea_ice"])
        self.assertEqual([r["index"] for r in rows], list(range(len(pts))))
        for r in rows:
            lo, la = pts[r["index"]]
            ii, jj, _, _ = self.sa.nearest_indices(lo, la)
            ref = self._ref_point(day, ii, jj, ["sst", "sea_ice"])
            self.assertEqual(r.get("sst"), ref.get("sst"))
            self.assertEqual(r.get("sea_ice"), ref.get("sea_ice"))

    def test_bbox_parity(self):
        day = self.days[-1]
        rows = [r for b in self.sa.bbox_batches(day, 119.0, 21.0, 119.5, 21.5, ["sst"])
                for r in b]
        # reference via xarray slice
        ds = self.xr.open_zarr(self.sa.root, group="/" + day.replace("-", "/"),
                               zarr_format=3, consolidated=None)
        lon = np.asarray(ds["lon"].values); lat = np.asarray(ds["lat"].values)
        j0, j1 = sorted((_nearest_idx(119.0, lon), _nearest_idx(119.5, lon)))
        i0, i1 = sorted((_nearest_idx(21.0, lat), _nearest_idx(21.5, lat)))
        ref = ds["sst"].isel(lat=slice(i0, i1 + 1), lon=slice(j0, j1 + 1)).compute().values[0]
        ds.close()
        self.assertEqual(len(rows), ref.size)
        # compare as sets of (lon,lat,val) to be order-independent
        got = {(round(r["lon"], 4), round(r["lat"], 4),
                None if r["sst"] is None else round(r["sst"], 4)) for r in rows}
        exp = set()
        for ri in range(ref.shape[0]):
            for cj in range(ref.shape[1]):
                v = ref[ri, cj]
                exp.add((round(float(lon[j0 + cj]), 4), round(float(lat[i0 + ri]), 4),
                         None if np.isnan(v) else round(float(v), 4)))
        self.assertEqual(got, exp)

    def test_point_series_memory_bounded(self):
        # 365-day (or all-available) point series must stay small (per-chunk release)
        lon_q, lat_q = 119.30672, 22.28274
        days = self.days[-min(365, len(self.days)):]
        tracemalloc.start()
        rows = self.sa.point_series(lon_q, lat_q, days, ["sst", "sea_ice"])
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peak_mb = peak / 1e6
        self.assertEqual(len(rows), len([d for d in days if self.sa.day_present(d)]))
        # generous ceiling: scalars only, must be far below a whole-chunk-per-day blowup
        self.assertLess(peak_mb, 50.0, f"point_series peak {peak_mb:.1f} MB too high")


if __name__ == "__main__":
    unittest.main(verbosity=2)
