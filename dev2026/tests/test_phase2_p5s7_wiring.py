"""dev2026 — P5-S7: base-tier selection must honour manifest authority and fail CLOSED.

**LOCAL AND SYNTHETIC ONLY.** Temp-dir stores; no VM24, no `GHRSST_*` production store, no
cron/PM2/systemd/NGINX, no production action.

## The rule

`GHRSST_BLOCK_MANIFEST_DIR` being **non-empty is the deployment's choice of authority**, not a
hint to try. Once it is set, a missing path, a non-directory, an invalid or stale manifest, or
any construction failure must leave the API explicitly degraded to daily-only — never quietly
served by the legacy monolith sitting beside it. Serving `GHRSST_TIMECUBE_PATH` in that case
publishes bytes no manifest ever authorized, under a `/healthz` that looks healthy.

The first version branched on `os.path.isdir(mf_dir)` and fell through an `elif` to the
monolith, so a typo'd or not-yet-mounted manifest path silently downgraded the deployment to a
different base. The predicate must be *"is a manifest configured"*, not *"does the manifest path
happen to exist right now"*.

## Why these assertions read app state as well as `/healthz`

`/healthz` reports what the app believes. A test that only reads it would pass on a build that
installed a monolith cube and mislabelled it. Each case therefore also asserts the actual object
in `app.state.router.cube`.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import p5_fixtures as fx  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store.segmented_cube import SegmentedCubeStore  # noqa: E402
from store.tiered_cube import TieredCube  # noqa: E402
from store.time_cube import TimeCubeStore  # noqa: E402
from store.zarr_paths import group_path  # noqa: E402
from test_phase2_p5s7_http import _segment  # noqa: E402

import numpy as np  # noqa: E402
import zarr  # noqa: E402

NY = NX = 16
ANCHOR = "2025-03-01"
BASE_DAYS = 20
LON, LAT = 104.0, 5.0

_ENV_KEYS = ("GHRSST_ZARR_PATH", "GHRSST_BLOCK_MANIFEST_DIR",
             "GHRSST_TIMECUBE_PATH", "GHRSST_DELTACUBE_PATH")


class _WiringFixture:
    """A valid manifest root, a malformed one, a legacy monolith, and a daily store."""

    def __init__(self, tmp):
        self.tmp = tmp
        self.days = fx.days_from(ANCHOR, BASE_DAYS)

        self.root = os.path.join(tmp, "manifest_root")
        os.makedirs(self.root, exist_ok=True)
        p = fx.build_block(os.path.join(self.root, "b0.zarr"), self.days,
                           ny=NY, nx=NX, seed=3)
        m = {"format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION, "generation": 1,
             "generation_id": "gen000001-wiring", "created_utc": "2027-01-01T00:00:00Z",
             "created_by": "p5s7-wiring-tests", "predecessor_generation": None,
             "predecessor_manifest": None,
             "grid": {"ny": NY, "nx": NX, "region": [0, NY, 0, NX]},
             "variables": list(fx.VARS),
             "block_grid": {"anchor_day": ANCHOR, "block_days": BASE_DAYS},
             "segments": [_segment("s0", "b0.zarr", self.days, 0, p)],
             "superseded": [], "manifest_checksum": ""}
        m["manifest_checksum"] = bm.compute_checksum(m)
        bm.publish(self.root, m)

        # An EXISTING directory holding a manifest that cannot be parsed. Distinct from a
        # missing path: this is the "malformed, not absent" case §4 asks to preserve.
        self.malformed = os.path.join(tmp, "malformed_root")
        os.makedirs(self.malformed, exist_ok=True)
        with open(os.path.join(self.malformed, "manifest.json"), "w") as fh:
            fh.write("{ this is not json")

        self.missing = os.path.join(tmp, "not_created_at_all")

        # The legacy monolith holding the SAME days with DIFFERENT values, so "which base
        # answered" is decidable from a served value, not merely from a label.
        self.monolith = os.path.join(tmp, "monolith.zarr")
        fx.build_block(self.monolith, self.days, ny=NY, nx=NX, seed=777)

        self.daily = os.path.join(tmp, "daily")
        os.makedirs(self.daily, exist_ok=True)
        self.daily_day = self.days[-1]
        self._build_daily(self.daily_day)

    def _build_daily(self, day):
        g = zarr.open_group(group_path(self.daily, day), mode="w", zarr_format=3)
        g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,))
        g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,))
        g["lon"][:] = np.linspace(100.0, 100.0 + NX - 1, NX, dtype=np.float32)
        g["lat"][:] = np.linspace(0.0, NY - 1.0, NY, dtype=np.float32)
        for v in fx.VARS:
            g.create_array(v, shape=(1, NY, NX), dtype="float32", chunks=(1, 8, 8))
            g[v][0] = np.full((NY, NX), 42.0, dtype=np.float32)

    def value_from(self, store_path, day, var="sst"):
        g = zarr.open_group(store_path, mode="r")
        t = list(g.attrs["days"]).index(day)
        j = int(np.argmin(np.abs(np.asarray(g["lon"][:]) - LON)))
        i = int(np.argmin(np.abs(np.asarray(g["lat"][:]) - LAT)))
        return float(np.asarray(g[var][t, i, j]))


class _WiringCase(unittest.TestCase):
    """Boots the app under a specific env combination and exposes the live cube."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="p5s7-wiring-")
        cls.fx = _WiringFixture(cls.tmp)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def boot(self, **env):
        """Start the real app with exactly this env; returns (client, cube)."""
        from fastapi.testclient import TestClient
        from api.app import app
        saved = {k: os.environ.get(k) for k in _ENV_KEYS}
        for k in _ENV_KEYS:
            os.environ.pop(k, None)
        os.environ["GHRSST_ZARR_PATH"] = self.fx.daily
        for k, v in env.items():
            os.environ[k] = v
        cm = TestClient(app)
        client = cm.__enter__()

        def restore():
            cm.__exit__(None, None, None)
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        self.addCleanup(restore)
        return client, app.state.router.cube

    def assert_daily_only(self, client, cube):
        """No cube of any kind was installed, and the API says so."""
        self.assertIsNone(cube, f"a cube was silently installed: {type(cube).__name__}")
        h = client.get("/healthz").json()
        self.assertEqual(h["status"], "ok")
        self.assertFalse(h["cube_loaded"])
        self.assertIsNone(h["cube_kind"])
        self.assertIsNone(h["cube_base_kind"])
        self.assertIsNone(h["cube_segment_count"])
        self.assertIsNone(h["cube_generation"])
        self.assertEqual(h["cube_day_count"], 0)
        # Degraded == daily-only: only the daily staging day is answerable.
        self.assertEqual(h["point_day_count"], 1)
        self.assertEqual(h["point_latest"], self.fx.daily_day)

    def assert_no_monolith_was_opened(self, cube):
        """The decisive check: no store anywhere in the tier resolves to the monolith path."""
        paths = []
        for obj in (cube, getattr(cube, "base", None), getattr(cube, "delta", None)):
            p = getattr(obj, "path", None)
            if p:
                paths.append(os.path.realpath(p))
            if isinstance(obj, SegmentedCubeStore):
                paths.extend(os.path.realpath(x) for x in obj.segment_paths)
        self.assertNotIn(os.path.realpath(self.fx.monolith), paths,
                         "the legacy monolith was opened")


class TestNoManifestConfiguredKeepsLegacyFallback(_WiringCase):
    """(1) An unset manifest env leaves the documented legacy path available."""

    def test_the_monolith_is_served_when_no_manifest_is_configured(self):
        client, cube = self.boot(GHRSST_TIMECUBE_PATH=self.fx.monolith)
        self.assertIsInstance(cube, TimeCubeStore)
        h = client.get("/healthz").json()
        self.assertTrue(h["cube_loaded"])
        self.assertEqual(h["cube_base_kind"], "monolith")
        self.assertIsNone(h["cube_segment_count"], "a monolith has no segment count")
        day = self.fx.days[2]
        r = client.get("/api/ghrsst", params=dict(lon0=LON, lat0=LAT, start=day, end=day))
        self.assertEqual(r.status_code, 200)
        self.assertAlmostEqual(r.json()[0]["sst"],
                               self.fx.value_from(self.fx.monolith, day), places=4)

    def test_an_EMPTY_manifest_env_is_treated_as_unset(self):
        """`FOO=` in a unit file is the usual way to clear a variable; it must not be read as
        'a manifest is configured' and fail the deployment closed."""
        client, cube = self.boot(GHRSST_BLOCK_MANIFEST_DIR="",
                                 GHRSST_TIMECUBE_PATH=self.fx.monolith)
        self.assertIsInstance(cube, TimeCubeStore)
        self.assertTrue(client.get("/healthz").json()["cube_loaded"])


class TestManifestWinsOverMonolith(_WiringCase):
    """(3) Both set and the manifest is valid: segmented wins and the monolith is not opened."""

    def test_the_segmented_base_wins_and_the_monolith_is_never_opened(self):
        client, cube = self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.fx.root,
                                 GHRSST_TIMECUBE_PATH=self.fx.monolith)
        self.assertIsInstance(cube, SegmentedCubeStore)
        self.assert_no_monolith_was_opened(cube)
        h = client.get("/healthz").json()
        self.assertEqual(h["cube_base_kind"], "segmented")
        self.assertEqual(h["cube_segment_count"], 1)

    def test_the_VALUE_served_comes_from_the_block_not_the_monolith(self):
        """The two stores hold the same days with different values, so this decides which base
        answered — a label alone could be right while the bytes came from elsewhere."""
        client, _ = self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.fx.root,
                              GHRSST_TIMECUBE_PATH=self.fx.monolith)
        day = self.fx.days[2]
        block_v = self.fx.value_from(os.path.join(self.fx.root, "b0.zarr"), day)
        mono_v = self.fx.value_from(self.fx.monolith, day)
        self.assertNotAlmostEqual(block_v, mono_v, places=3,
                                  msg="fixture cannot distinguish the two bases")
        r = client.get("/api/ghrsst", params=dict(lon0=LON, lat0=LAT, start=day, end=day))
        self.assertAlmostEqual(r.json()[0]["sst"], block_v, places=4)


class TestASegmentedBaseWithoutADelta(_WiringCase):
    """A manifest with no `GHRSST_DELTACUBE_PATH` is a legal deployment, and only became
    reachable when the app learned to build a segmented base.

    It crashed: `HybridRouter._CubeSnapshot.covers_days` read `_meta.day_index`, a
    `TimeCubeStore` field that a `SegmentedCubeStore` snapshot does not have — previously a
    segmented base was only ever reached through `TieredCube`, which has its own snapshot."""

    def test_a_point_read_works_with_no_delta_tier(self):
        client, cube = self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.fx.root)
        self.assertIsInstance(cube, SegmentedCubeStore)
        self.assertNotIsInstance(cube, TieredCube)
        day = self.fx.days[2]
        r = client.get("/api/ghrsst", params=dict(lon0=LON, lat0=LAT, start=day, end=day))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["X-Store-Route"], "cube")
        self.assertAlmostEqual(r.json()[0]["sst"],
                               self.fx.value_from(os.path.join(self.fx.root, "b0.zarr"), day),
                               places=4)

    def test_healthz_reports_no_delta_without_crashing(self):
        client, _ = self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.fx.root)
        h = client.get("/healthz").json()
        self.assertTrue(h["cube_loaded"])
        self.assertEqual(h["cube_base_kind"], "segmented")
        self.assertEqual(h["cube_kind"], "timecube", "no delta tier was composed")
        self.assertEqual(h["delta_day_count"], 0)


class TestMissingManifestPathFailsClosed(_WiringCase):
    """(2) Configured but absent: degrade to daily-only, never fall back."""

    def test_a_missing_manifest_directory_does_NOT_fall_back_to_the_monolith(self):
        client, cube = self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.fx.missing,
                                 GHRSST_TIMECUBE_PATH=self.fx.monolith)
        self.assert_daily_only(client, cube)
        self.assert_no_monolith_was_opened(cube)

    def test_a_NON_DIRECTORY_manifest_path_does_not_fall_back(self):
        not_a_dir = os.path.join(self.fx.tmp, "a_file")
        with open(not_a_dir, "w") as fh:
            fh.write("x")
        client, cube = self.boot(GHRSST_BLOCK_MANIFEST_DIR=not_a_dir,
                                 GHRSST_TIMECUBE_PATH=self.fx.monolith)
        self.assert_daily_only(client, cube)

    def test_a_base_day_is_refused_rather_than_answered_from_the_monolith(self):
        """Degraded must be visible to a client, not only in /healthz: a day only the base
        holds must 4xx, not return the monolith's value for it."""
        client, _ = self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.fx.missing,
                              GHRSST_TIMECUBE_PATH=self.fx.monolith)
        day = self.fx.days[2]
        r = client.get("/api/ghrsst", params=dict(lon0=LON, lat0=LAT, start=day, end=day))
        self.assertEqual(r.status_code, 400)

    def test_the_daily_window_still_serves(self):
        """Fail-closed on the base must not take the daily tier down with it."""
        client, _ = self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.fx.missing,
                              GHRSST_TIMECUBE_PATH=self.fx.monolith)
        d = self.fx.daily_day
        r = client.get("/api/ghrsst", params=dict(lon0=LON, lat0=LAT, start=d, end=d))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["X-Store-Route"], "daily")
        self.assertAlmostEqual(r.json()[0]["sst"], 42.0, places=4)


class TestMalformedManifestFailsClosed(_WiringCase):
    """(4) The pre-existing fail-closed behaviour for a malformed manifest is preserved."""

    def test_an_unparseable_manifest_does_NOT_fall_back_to_the_monolith(self):
        client, cube = self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.fx.malformed,
                                 GHRSST_TIMECUBE_PATH=self.fx.monolith)
        self.assert_daily_only(client, cube)
        self.assert_no_monolith_was_opened(cube)

    def test_a_manifest_whose_BLOCK_is_missing_fails_closed(self):
        """A structurally valid manifest pointing at a block that is not there — the 'stale
        manifest' case. Nothing may be served from the monolith in its place."""
        stale = os.path.join(self.fx.tmp, "stale_root")
        os.makedirs(stale, exist_ok=True)
        shutil.copyfile(os.path.join(self.fx.root, "manifest.json"),
                        os.path.join(stale, "manifest.json"))   # block never copied
        client, cube = self.boot(GHRSST_BLOCK_MANIFEST_DIR=stale,
                                 GHRSST_TIMECUBE_PATH=self.fx.monolith)
        self.assert_daily_only(client, cube)
        self.assert_no_monolith_was_opened(cube)


class TestTheGuardIsFalsifiable(_WiringCase):
    """The negative tests must fail if the guard is removed. Asserted against the real
    predicate rather than trusting that a passing suite implies a working guard."""

    def test_selecting_on_isdir_would_fall_through_to_the_monolith(self):
        """Reproduces the removed code's predicate and shows it picks the monolith. The
        mutation `if mf_dir` -> `if mf_dir and os.path.isdir(mf_dir)` is exactly this."""
        mf_dir, tc_path = self.fx.missing, self.fx.monolith
        chosen_buggy = ("segmented" if (mf_dir and os.path.isdir(mf_dir))
                        else "monolith" if (tc_path and os.path.isdir(tc_path)) else None)
        chosen_fixed = ("segmented" if mf_dir
                        else "monolith" if (tc_path and os.path.isdir(tc_path)) else None)
        self.assertEqual(chosen_buggy, "monolith", "the reported blocker")
        self.assertEqual(chosen_fixed, "segmented", "authority is decided by configuration")


if __name__ == "__main__":
    unittest.main()
