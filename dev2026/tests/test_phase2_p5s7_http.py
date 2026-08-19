"""dev2026 — P5-S7: the whole API surface over a SEGMENTED store, through real HTTP.

**LOCAL AND SYNTHETIC ONLY (`LOCAL_SYNTHETIC`).** Every store is a `tempfile` tree. No VM24, no
SSH, no `GHRSST_*` production store, no cron/PM2/systemd/NGINX, no prune/swap/compaction/repair
/hold cleanup, no production deletion. Nothing here is VM24 capacity or production-performance
evidence.

## Through the API, not around it

Every assertion goes through `fastapi.testclient.TestClient` — real routing, real dependency
wiring, real response headers and status codes. Calling `HybridRouter` or `TieredCube` directly
would test the layer *under* the contract and would not notice, for example, that the app cannot
construct a segmented base at all (which is exactly what the first test here found).

## The oracle is independent of the read path

Expected values are read straight out of the block and delta Zarr arrays with `zarr.open_group`,
indexed by the day's own position in that store's `attrs["days"]`. The API's answer is never
compared against another API call, against `SegmentedCubeStore`, or against `TieredCube` — those
are the things under test. `TestNegativeControl` corrupts the oracle and asserts the parity
tests actually fail, because a parity test that cannot fail is not a parity test.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import tempfile
import shutil
import unittest

import numpy as np
import zarr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import p5_fixtures as fx  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store.zarr_paths import group_path  # noqa: E402

NY, NX = 24, 24
#: The fixture grid: lon = 100..100+NX-1, lat = 0..NY-1 (see `p5_fixtures.build_block`).
LON, LAT = 104.0, 5.0
LON_I, LAT_I = 4, 5

ANCHOR = "2025-01-01"
BLOCK_DAYS = 30
N_BLOCKS = 3
DELTA_DAYS = 10


def _lonlat_index(store_path, lon, lat):
    """Nearest index, computed from the store's OWN coordinate arrays."""
    g = zarr.open_group(store_path, mode="r")
    j = int(np.argmin(np.abs(np.asarray(g["lon"][:]) - lon)))
    i = int(np.argmin(np.abs(np.asarray(g["lat"][:]) - lat)))
    return i, j


def _segment(seg_id, rel, days, precedence, store_path):
    """A manifest segment, with layout/fingerprint read from the block ITSELF.

    Hand-authoring these was the first thing tried and the manifest validator rejected it —
    correctly. A segment whose fingerprint is typed rather than computed describes a block that
    may not exist."""
    return {"segment_id": seg_id, "kind": "block", "path": rel, "immutable": True,
            "boundary_kind": "legacy", "start_day": days[0], "end_day": days[-1],
            "materialized_through": days[-1], "day_count": len(days),
            "gaps": [], "unknown": [], "day_list": None,
            "layout": bm.segment_layout(store_path), "variables": sorted(fx.VARS),
            "fingerprint": {"algo": "sha256",
                            "metadata": bm.metadata_fingerprint(store_path),
                            "day_digest": bm.day_digest(fx.read_days(store_path))},
            "precedence": precedence, "sealed": True, "supersedes": None}


class S7Fixture:
    """A published segmented base + a delta + a daily staging store, all synthetic."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.root = os.path.join(tmp, "manifest_root")
        self.daily = os.path.join(tmp, "daily")
        os.makedirs(self.root, exist_ok=True)
        os.makedirs(self.daily, exist_ok=True)

        self.base_days = fx.days_from(ANCHOR, BLOCK_DAYS * N_BLOCKS)
        self.delta_days = fx.days_from(
            bm.add_days(self.base_days[-1], 1), DELTA_DAYS)

        self.segments, self.block_paths = [], {}
        for k in range(N_BLOCKS):
            days = self.base_days[k * BLOCK_DAYS:(k + 1) * BLOCK_DAYS]
            name = f"block_{k}.zarr"
            p = fx.build_block(os.path.join(self.root, name), days,
                               ny=NY, nx=NX, seed=k * 7)
            self.block_paths[name] = p
            self.segments.append(_segment(f"s{k}", name, days, k, p))
        self.delta_path = os.path.join(tmp, "delta.zarr")
        fx.build_delta(self.delta_path, self.delta_days, ny=NY, nx=NX, seed=555)

        # A daily staging store for the recent window (bbox / POST are daily-served).
        for d in self.delta_days[-3:]:
            self._build_daily_day(d)
        self.generation = 0
        self.publish(self.segments)

    def _build_daily_day(self, day):
        g = zarr.open_group(group_path(self.daily, day), mode="w", zarr_format=3)
        g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,))
        g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,))
        g["lon"][:] = np.linspace(100.0, 100.0 + NX - 1, NX, dtype=np.float32)
        g["lat"][:] = np.linspace(0.0, NY - 1.0, NY, dtype=np.float32)
        for v in fx.VARS:
            g.create_array(v, shape=(1, NY, NX), dtype="float32", chunks=(1, 8, 8))
            g[v][0] = self._delta_cell_plane(day, v)

    def _delta_cell_plane(self, day, var):
        g = zarr.open_group(self.delta_path, mode="r")
        return np.asarray(g[var][list(g.attrs["days"]).index(day)])

    def manifest(self, segments, generation):
        m = {"format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION,
             "generation": generation, "generation_id": f"gen{generation:06d}-s7",
             "created_utc": "2027-01-01T00:00:00Z", "created_by": "p5s7-tests",
             "predecessor_generation": generation - 1 if generation > 1 else None,
             "predecessor_manifest": (bm.archive_name(generation - 1)
                                      if generation > 1 else None),
             "grid": {"ny": NY, "nx": NX, "region": [0, NY, 0, NX]},
             "variables": list(fx.VARS),
             "block_grid": {"anchor_day": ANCHOR, "block_days": BLOCK_DAYS},
             "segments": [dict(s) for s in segments], "superseded": [],
             "manifest_checksum": ""}
        m["manifest_checksum"] = bm.compute_checksum(m)
        return m

    def publish(self, segments):
        self.generation += 1
        return bm.publish(self.root, self.manifest(segments, self.generation))

    # ---- the independent oracle ----------------------------------------
    def expect(self, day, var="sst", lon=LON, lat=LAT):
        """Read the value straight out of the block or delta that holds `day`.

        Delta wins, matching the serving authority. Nothing here consults
        `SegmentedCubeStore`, `TieredCube` or the API."""
        if day in self.delta_days:
            path = self.delta_path
        else:
            k = self.base_days.index(day) // BLOCK_DAYS
            path = self.block_paths[f"block_{k}.zarr"]
        g = zarr.open_group(path, mode="r")
        t = list(g.attrs["days"]).index(day)
        i, j = _lonlat_index(path, lon, lat)
        return float(np.asarray(g[var][t, i, j]))


class _AppCase(unittest.TestCase):
    """Boots the real app over the fixture via TestClient."""

    ENV = {}

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="p5s7-")
        cls.fx = S7Fixture(cls.tmp)
        cls._saved = {}
        env = {"GHRSST_ZARR_PATH": cls.fx.daily,
               "GHRSST_BLOCK_MANIFEST_DIR": cls.fx.root,
               "GHRSST_DELTACUBE_PATH": cls.fx.delta_path}
        env.update(cls.ENV)
        for k, v in env.items():
            cls._saved[k] = os.environ.get(k)
            os.environ[k] = v
        from fastapi.testclient import TestClient
        from api.app import app
        cls._client_cm = TestClient(app)
        cls.client = cls._client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        try:
            cls._client_cm.__exit__(None, None, None)
        finally:
            for k, v in cls._saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, **params):
        return self.client.get("/api/ghrsst", params=params)


class TestTheAppCanServeASegmentedStore(_AppCase):
    """G1/G12 precondition. The app wires `GHRSST_TIMECUBE_PATH` -> `TimeCubeStore` only, so
    before S7 there was no way to point it at a manifest-driven base at all: every "segmented
    API" claim would have been measured against a monolith or against no cube."""

    def test_healthz_reports_a_segmented_tiered_cube(self):
        h = self.client.get("/healthz").json()
        self.assertEqual(h["status"], "ok")
        self.assertTrue(h["cube_loaded"], "no cube was constructed from the manifest dir")
        self.assertEqual(h["cube_kind"], "tiered")
        self.assertEqual(h["cube_base_kind"], "segmented")
        self.assertEqual(h["cube_segment_count"], N_BLOCKS)
        self.assertEqual(h["cube_generation"], self.fx.generation)

    def test_the_cube_covers_every_base_and_delta_day(self):
        h = self.client.get("/healthz").json()
        self.assertEqual(h["cube_day_count"], BLOCK_DAYS * N_BLOCKS + DELTA_DAYS)
        self.assertEqual(h["point_latest"], self.fx.delta_days[-1])
        self.assertEqual(h["point_earliest"], self.fx.base_days[0])


class TestPointAndRangeThroughHTTP(_AppCase):
    """G1 semantic equality and G2 day integrity, over the wire."""

    def test_point_GET_matches_the_raw_block_cell(self):
        for day in (self.fx.base_days[0], self.fx.base_days[45], self.fx.base_days[-1]):
            with self.subTest(day):
                r = self.get(lon0=LON, lat0=LAT, start=day, end=day)
                self.assertEqual(r.status_code, 200)
                rows = r.json()
                self.assertEqual(len(rows), 1)
                self.assertAlmostEqual(rows[0]["sst"], self.fx.expect(day), places=4)

    def test_a_delta_day_is_served_from_the_delta(self):
        day = self.fx.delta_days[-1]
        r = self.get(lon0=LON, lat0=LAT, start=day, end=day)
        self.assertAlmostEqual(r.json()[0]["sst"], self.fx.expect(day), places=4)

    def test_a_range_CROSSING_every_segment_and_the_delta(self):
        s, e = self.fx.base_days[0], self.fx.delta_days[-1]
        r = self.get(lon0=LON, lat0=LAT, start=s, end=e)
        self.assertEqual(r.status_code, 200)
        rows = r.json()
        want = self.fx.base_days + self.fx.delta_days
        self.assertEqual([row["date"] for row in rows], want, "G2: day set/order")
        self.assertEqual(len(set(row["date"] for row in rows)), len(want), "G2: duplicates")
        for row in rows:
            with self.subTest(row["date"]):
                self.assertAlmostEqual(row["sst"], self.fx.expect(row["date"]), places=4)

    def test_the_route_header_is_cube_for_point_and_range(self):
        """G12/Q4: segmented storage stays `cube`; segments are never exposed publicly."""
        for s, e in ((self.fx.base_days[0], self.fx.base_days[0]),
                     (self.fx.base_days[0], self.fx.delta_days[-1])):
            with self.subTest(start=s, end=e):
                r = self.get(lon0=LON, lat0=LAT, start=s, end=e)
                self.assertEqual(r.headers["X-Store-Route"], "cube")

    def test_MAX_DAYS_is_enforced_with_413(self):
        """G12. The cap is on the REQUESTED span, before clamping to availability."""
        s = "2020-01-01"
        e = self.fx.delta_days[-1]
        r = self.get(lon0=LON, lat0=LAT, start=s, end=e)
        self.assertEqual(r.status_code, 413)
        body = r.json()["detail"]
        self.assertEqual(body["max_days"], 366)
        self.assertGreater(body["requested_days"], 366)


class TestSpatialContractThroughHTTP(_AppCase):
    """G11 — bbox/POST answers are unchanged and no block is ever spatially served."""

    def test_bbox_returns_the_daily_plane_values(self):
        day = self.fx.delta_days[-1]
        r = self.client.get("/api/ghrsst", params={
            "lon0": LON, "lat0": LAT, "lon1": LON + 2, "lat1": LAT + 2,
            "start": day, "end": day})
        self.assertEqual(r.status_code, 200)
        rows = json.loads(r.content)
        self.assertTrue(rows)
        got = {(round(p["lon"], 3), round(p["lat"], 3)): p.get("sst") for p in rows}
        self.assertAlmostEqual(got[(round(LON, 3), round(LAT, 3))],
                               self.fx.expect(day), places=4)

    def test_POST_points_matches_the_oracle(self):
        day = self.fx.delta_days[-1]
        r = self.client.post("/api/ghrsst/points", json={
            "date": day, "points": [[LON, LAT]]})
        self.assertEqual(r.status_code, 200)
        rows = r.json()
        self.assertAlmostEqual(rows[0]["sst"], self.fx.expect(day), places=4)

    def test_a_base_only_day_is_not_spatially_served_from_a_block(self):
        """A block is a point/range tier only. bbox for a day older than the daily window
        must not silently start reading block planes."""
        old = self.fx.base_days[0]
        r = self.client.get("/api/ghrsst", params={
            "lon0": LON, "lat0": LAT, "lon1": LON + 2, "lat1": LAT + 2,
            "start": old, "end": old})
        self.assertIn(r.status_code, (400, 404))


class TestNegativeControl(_AppCase):
    """A parity test that cannot fail is not a parity test."""

    def test_parity_fails_when_the_ORACLE_is_perturbed(self):
        day = self.fx.base_days[5]
        real = self.fx.expect(day)
        r = self.get(lon0=LON, lat0=LAT, start=day, end=day)
        self.assertAlmostEqual(r.json()[0]["sst"], real, places=4)
        with self.assertRaises(AssertionError):
            self.assertAlmostEqual(r.json()[0]["sst"], real + 1.0, places=4)

    def test_the_oracle_reads_a_DIFFERENT_value_per_day(self):
        """If every day held the same value, every parity assertion above would pass on a
        store that returned the wrong day."""
        vals = [self.fx.expect(d) for d in self.fx.base_days[:5]]
        self.assertEqual(len(set(vals)), len(vals), "fixture days are indistinguishable")


if __name__ == "__main__":
    unittest.main()
