"""dev2026 — P4 POST-PRUNE point-availability regression (VM24 production, 2026-07-28).

Reproduces the production regression the first real daily-staging prune exposed: after the daily store
was pruned to a 38-day staging window, **full-history point/range queries started returning 400** even
though base+delta still hold the history.

    GET /api/ghrsst?lat0=32&lon0=-69&start=2025-01-01&end=2025-12-31  ->  400
    {"detail":"Data not exist for requested period; available range is 2026-06-21/2026-07-28."}

Root cause (three compounding defects, all on the DAILY store being treated as the availability
authority):
  1. `api/app.py` point branch clamps the requested range to `store.bounds()` (daily) — a 2025 request
     is clamped to `s=<daily earliest>` while `e` stays in 2025, so `s > e` and the range is EMPTY;
  2. the same branch filters `existing = [d for d in wanted if store.day_present(d)]` (daily only);
  3. `HybridRouter.route_point()` ALSO pre-filters by `daily.day_present` and requires `len(existing)>1`,
     so a historical single-day point can never reach the cube.

Contract under test (P4): point GET (single-day AND range, <=366 days) = FULL HISTORY from base+delta;
bbox + POST /points stay bound to the recent spatial window (delta membership). Daily is recent staging
only — never the full-history availability authority.

Fixture mirrors the post-prune production shape (no VM24 needed):
    daily  38 days  2026-06-21..2026-07-28   (pruned staging)
    base   38 days  2026-05-20..2026-06-26   (older history; ends before daily's tail)
    delta  36 days  2026-06-23..2026-07-28   (recent, overlaps base 06-23..06-26)
    union  70 days  2026-05-20..2026-07-28   > daily
    base-only (history NOT in daily): 2026-05-20..2026-06-20 (32 days)

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_p4_point_availability.py
"""
from __future__ import annotations

import datetime
import importlib
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np
import zarr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from store.zarr_paths import group_path  # noqa: E402
from ingest.build_timecube_bulk import build_timecube_bulk  # noqa: E402
from ingest.dual_write import append_to_delta  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

_ENV_KEYS = ("GHRSST_ZARR_PATH", "GHRSST_TIMECUBE_PATH", "GHRSST_DELTACUBE_PATH",
             "GHRSST_SPATIAL_WINDOW_ENFORCE")

NY, NX = 24, 30
VARS = ("sst", "sst_anomaly", "sea_ice")

LATEST = "2026-07-28"
DAILY_START = "2026-06-21"      # pruned staging window start (38 days)
BASE_START = "2026-05-20"       # oldest history (base)
BASE_END = "2026-06-26"         # base cutoff (as on VM24: base ends before daily tail)
DELTA_START = "2026-06-23"      # delta window start (36 days)

HIST_DAY = "2026-06-01"         # in base, NOT in daily  -> the regression case
HIST_RANGE = ("2026-05-25", "2026-06-10")   # fully base-only
CROSS_RANGE = ("2026-06-20", "2026-06-25")  # base -> delta boundary crossing


def _days(a: str, b: str):
    d0 = datetime.date.fromisoformat(a)
    d1 = datetime.date.fromisoformat(b)
    return [(d0 + datetime.timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def _val(day: str, ii: int, jj: int) -> float:
    """Deterministic, day-specific field value so parity is meaningful per date."""
    ordinal = datetime.date.fromisoformat(day).toordinal() % 1000
    return float(ordinal) + 0.01 * ii + 0.001 * jj


def _build_day(root: str, day: str, with_anomaly: bool = True):
    lon = np.linspace(100.0, 130.0, NX).astype(np.float32)
    lat = np.linspace(0.0, 30.0, NY).astype(np.float32)
    g = zarr.open_group(group_path(root, day), mode="w", zarr_format=3)
    g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,)); g["lon"][:] = lon
    g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,)); g["lat"][:] = lat
    # real MUR daily groups carry a 1-D time coord (see the P4-S6 data-var filter)
    g.create_array("time", shape=(1,), dtype="int64", chunks=(1,))
    g["time"][:] = [datetime.date.fromisoformat(day).toordinal()]
    fld = np.fromfunction(lambda i, j: _val(day, 0, 0) + 0.01 * i + 0.001 * j, (NY, NX))
    for v in VARS:
        if v == "sst_anomaly" and not with_anomaly:
            continue
        g.create_array(v, shape=(1, NY, NX), dtype="float32", chunks=(1, 8, 8))
        g[v][0] = fld.astype(np.float32)
    return g


def _build_post_prune_stores(tmp: str) -> dict:
    """Build the post-prune production shape once: full history in base+delta, daily pruned to 38 days."""
    full = os.path.join(tmp, "daily_full")
    all_days = _days(BASE_START, LATEST)
    for d in all_days:
        _build_day(full, d)
    base = os.path.join(tmp, "base")
    build_timecube_bulk(full, base, spatial_chunk=8, time_chunk=90, shard_spatial=8,
                        read_block=8, workers=2, end_day=BASE_END)
    delta = os.path.join(tmp, "delta")
    for d in _days(DELTA_START, LATEST):
        append_to_delta(full, delta, d, spatial_chunk=8, shard_spatial=8)
    daily = os.path.join(tmp, "daily")                        # the PRUNED live staging store
    daily_days = _days(DAILY_START, LATEST)
    for d in daily_days:
        shutil.copytree(group_path(full, d), group_path(daily, d))
    shutil.rmtree(full, ignore_errors=True)                   # only base+delta+pruned daily remain
    return {"base": base, "delta": delta, "daily": daily, "daily_days": daily_days,
            "all_days": all_days}


class PostPrunePointAvailability(unittest.TestCase):
    """Post-prune production shape: daily pruned to 38 days; base+delta hold the history.

    `Cfg` snapshots env at import, so the module is RELOADED after the env is set — otherwise
    GHRSST_SPATIAL_WINDOW_ENFORCE stays at its import-time default and the spatial assertions below
    would silently test an unenforced gate (project convention, cf. tests/test_phase2_p4s3gate.py)."""

    @classmethod
    def setUpClass(cls):
        cls._prev = {k: os.environ.get(k) for k in _ENV_KEYS}
        cls.tmp = tempfile.mkdtemp(prefix="p4pointavail_")
        st = _build_post_prune_stores(cls.tmp)
        cls.base, cls.delta, cls.daily = st["base"], st["delta"], st["daily"]
        cls.daily_days, cls.all_days = st["daily_days"], st["all_days"]
        os.environ.update({"GHRSST_ZARR_PATH": cls.daily, "GHRSST_TIMECUBE_PATH": cls.base,
                           "GHRSST_DELTACUBE_PATH": cls.delta,
                           "GHRSST_SPATIAL_WINDOW_ENFORCE": "1"})   # production posture
        import api.app as appmod
        cls.appmod = importlib.reload(appmod)                 # re-read Cfg with the env above
        cls._cm = TestClient(cls.appmod.app)
        cls.client = cls._cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._cm.__exit__(None, None, None)
        for k, v in cls._prev.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        import api.app as appmod
        importlib.reload(appmod)                              # restore for other test modules
        shutil.rmtree(cls.tmp, ignore_errors=True)

    # -- sanity: the fixture really is post-prune shaped, and the gate is really ON ----
    def test_fixture_shape(self):
        self.assertEqual(len(self.daily_days), 38)
        self.assertNotIn(HIST_DAY, self.daily_days)          # the regression precondition
        bd = list(zarr.open_group(self.base, mode="r").attrs["days"])
        dd = list(zarr.open_group(self.delta, mode="r").attrs["days"])
        self.assertIn(HIST_DAY, bd)
        self.assertEqual(len(dd), 36)
        self.assertGreater(len(set(bd) | set(dd)), len(self.daily_days))   # union > daily
        self.assertTrue(self.client.get("/healthz").json()["spatial_window_enforce"])

    def test_historical_single_day_point(self):
        # daily lacks 2026-06-01; base has it -> MUST be 200 from the cube (was 400 in production)
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": HIST_DAY, "end": HIST_DAY, "append": "sst"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get("X-Store-Route"), "cube")
        body = r.json()
        self.assertEqual([row["date"] for row in body], [HIST_DAY])
        # value parity against a direct cube read
        from store.time_cube import TimeCubeStore
        exp = TimeCubeStore(self.base).point_series(110.0, 12.0, [HIST_DAY], ["sst"])[0]["sst"]
        self.assertAlmostEqual(body[0]["sst"], exp, places=5)

    def test_historical_range_point(self):
        s, e = HIST_RANGE
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": s, "end": e, "append": "sst"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get("X-Store-Route"), "cube")
        dates = [row["date"] for row in r.json()]
        self.assertEqual(dates, _days(s, e))                  # every day present, in order

    def test_base_to_delta_crossing_range(self):
        s, e = CROSS_RANGE
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": s, "end": e, "append": "sst,sst_anomaly"})
        self.assertEqual(r.status_code, 200, r.text)
        rows = r.json()
        self.assertEqual([row["date"] for row in rows], _days(s, e))   # ordered, no gaps
        from store.time_cube import TimeCubeStore
        from store.tiered_cube import TieredCube
        cube = TieredCube(TimeCubeStore(self.base), TimeCubeStore(self.delta))
        for row in rows:
            exp = cube.point_series(110.0, 12.0, [row["date"]], ["sst"])[0]["sst"]
            self.assertAlmostEqual(row["sst"], exp, places=5, msg=row["date"])

    def test_long_historical_range_spanning_full_union(self):
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": BASE_START, "end": LATEST, "append": "sst"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([row["date"] for row in r.json()], self.all_days)   # all 70 days

    def test_latest_single_day_point(self):
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": LATEST, "end": LATEST, "append": "sst"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([row["date"] for row in r.json()], [LATEST])
        # cube (delta) covers the latest day -> cube is authoritative; values must match daily exactly
        self.assertEqual(r.headers.get("X-Store-Route"), "cube")
        from store.store_access import StoreAccess
        exp = StoreAccess(self.daily).point_series(110.0, 12.0, [LATEST], ["sst"])[0]["sst"]
        self.assertAlmostEqual(r.json()[0]["sst"], exp, places=5)

    def test_default_latest_point_still_correct(self):
        r = self.client.get("/api/ghrsst", params={"lon0": 110.0, "lat0": 12.0})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual([row["date"] for row in body], [LATEST])
        self.assertEqual(r.headers["cache-control"], "no-store")

    def test_range_partially_before_history_clamps_not_errors(self):
        # start well before the union start: clamp to the union, do NOT 400
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": "2026-05-01", "end": "2026-05-25", "append": "sst"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([row["date"] for row in r.json()], _days(BASE_START, "2026-05-25"))

    def test_fully_out_of_range_still_400_with_full_range_text(self):
        # genuinely unavailable period -> 400, but the message must advertise the FULL point range
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": "2024-01-01", "end": "2024-01-05"})
        self.assertEqual(r.status_code, 400)
        detail = r.json()["detail"]
        self.assertIn(BASE_START, detail)     # full-history start, NOT the daily staging start
        self.assertIn(LATEST, detail)
        self.assertNotIn(DAILY_START, detail)

    def test_max_days_cap_unchanged(self):
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": "2024-01-01", "end": "2026-07-28"})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(r.json()["detail"]["max_days"], 366)

    def test_cache_control_semantics_unchanged(self):
        s, e = HIST_RANGE                                     # ends before latest -> cacheable
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": s, "end": e})
        self.assertIn("max-age", r.headers["cache-control"])
        r2 = self.client.get("/api/ghrsst", params={          # touches latest -> no-store
            "lon0": 110.0, "lat0": 12.0, "start": "2026-07-01", "end": LATEST})
        self.assertEqual(r2.headers["cache-control"], "no-store")


    # ---- (5)(6)(7) the spatial contract must NOT widen: bbox/POST stay delta-window bound ----

    def test_old_bbox_rejected(self):
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "lon1": 111.0, "lat1": 13.0,
            "start": HIST_DAY, "end": HIST_DAY, "append": "sst"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_bbox_in_daily_but_outside_delta_rejected(self):
        # 2026-06-21 IS in daily staging but NOT in the delta window -> spatial gate must still reject
        self.assertIn(DAILY_START, self.daily_days)
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "lon1": 111.0, "lat1": 13.0,
            "start": DAILY_START, "end": DAILY_START, "append": "sst"})
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("available_spatial_window", str(r.json()["detail"]))

    def test_recent_bbox_ok(self):
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "lon1": 112.0, "lat1": 14.0,
            "start": LATEST, "end": LATEST, "append": "sst"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreater(len(r.json()), 0)

    def test_old_post_points_rejected(self):
        r = self.client.post("/api/ghrsst/points", json={
            "date": HIST_DAY, "points": [[110.0, 12.0]], "append": "sst"})
        self.assertEqual(r.status_code, 400, r.text)

    def test_recent_post_points_ok(self):
        r = self.client.post("/api/ghrsst/points", json={
            "date": LATEST, "points": [[110.0, 12.0], [111.0, 13.0]], "append": "sst"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(r.json()), 2)


    # ---- (8) healthz advertises PUBLIC point availability; daily staging exposed separately ----

    def test_healthz_fields(self):
        hz = self.client.get("/healthz").json()
        # public point availability = base+delta+daily union
        self.assertEqual(hz["earliest"], BASE_START)
        self.assertEqual(hz["latest"], LATEST)
        # daily staging surfaced explicitly (was invisible -> clients read 38 days as "all we have")
        self.assertEqual(hz["daily_earliest"], DAILY_START)
        self.assertEqual(hz["daily_latest"], LATEST)
        self.assertEqual(hz["daily_day_count"], 38)
        # cube/delta observability preserved
        self.assertEqual(hz["cube_kind"], "tiered")
        self.assertEqual(hz["cube_latest"], LATEST)
        self.assertEqual(hz["cube_day_count"], 70)
        self.assertEqual(hz["delta_day_count"], 36)
        self.assertTrue(hz["cube_latest_in_sync"])
        # point_* mirrors the public availability explicitly
        self.assertEqual(hz["point_earliest"], BASE_START)
        self.assertEqual(hz["point_latest"], LATEST)
        self.assertEqual(hz["point_day_count"], 70)


class MixedRouteNoTruncation(unittest.TestCase):
    """A range that NEITHER tier covers alone must be merged, never truncated.

    Real trigger: the daily cron has written today's group but the delta append has not run yet, so
    the newest day is daily-only while the history is cube-only. Routing the whole series to one
    store would silently drop the other tier's days — a worse failure than the 400 this fix removes.
    """

    @classmethod
    def setUpClass(cls):
        cls._prev = {k: os.environ.get(k) for k in _ENV_KEYS}
        cls.tmp = tempfile.mkdtemp(prefix="p4pointavail_mixed_")
        cls.days = _days("2026-07-01", "2026-07-07")
        full = os.path.join(cls.tmp, "daily_full")
        for d in cls.days:
            _build_day(full, d)
        cls.base = os.path.join(cls.tmp, "base")                  # cube: 07-01..07-05
        build_timecube_bulk(full, cls.base, spatial_chunk=8, time_chunk=90, shard_spatial=8,
                            read_block=8, workers=2, end_day="2026-07-05")
        cls.daily = os.path.join(cls.tmp, "daily")                # daily: 07-04..07-07 (07-06/07 cube-less)
        for d in _days("2026-07-04", "2026-07-07"):
            shutil.copytree(group_path(full, d), group_path(cls.daily, d))
        shutil.rmtree(full, ignore_errors=True)
        os.environ.update({"GHRSST_ZARR_PATH": cls.daily, "GHRSST_TIMECUBE_PATH": cls.base,
                           "GHRSST_SPATIAL_WINDOW_ENFORCE": "1"})
        os.environ.pop("GHRSST_DELTACUBE_PATH", None)
        import api.app as appmod
        cls.appmod = importlib.reload(appmod)
        cls._cm = TestClient(cls.appmod.app)
        cls.client = cls._cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._cm.__exit__(None, None, None)
        for k, v in cls._prev.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        import api.app as appmod
        importlib.reload(appmod)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_span_neither_tier_covers_is_merged(self):
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": "2026-07-01", "end": "2026-07-07",
            "append": "sst"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers.get("X-Store-Route"), "mixed")
        rows = r.json()
        self.assertEqual([x["date"] for x in rows], self.days)     # ALL 7 days, none dropped
        # per-day values come from whichever tier holds the day and must match that tier exactly
        from store.time_cube import TimeCubeStore
        from store.store_access import StoreAccess
        cube, daily = TimeCubeStore(self.base), StoreAccess(self.daily)
        for row in rows:
            src = cube if row["date"] <= "2026-07-05" else daily
            exp = src.point_series(110.0, 12.0, [row["date"]], ["sst"])[0]["sst"]
            self.assertAlmostEqual(row["sst"], exp, places=5, msg=row["date"])

    def test_cube_only_and_daily_only_subranges_route_purely(self):
        r_cube = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": "2026-07-01", "end": "2026-07-03"})
        self.assertEqual(r_cube.headers.get("X-Store-Route"), "cube")     # cube-only history
        r_daily = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": "2026-07-06", "end": "2026-07-07"})
        self.assertEqual(r_daily.headers.get("X-Store-Route"), "daily")   # daily-only new days
        self.assertEqual(r_daily.status_code, 200)

    def test_healthz_union_covers_both_tiers(self):
        hz = self.client.get("/healthz").json()
        self.assertEqual(hz["earliest"], "2026-07-01")   # cube start
        self.assertEqual(hz["latest"], "2026-07-07")     # daily-only newest day
        self.assertEqual(hz["point_day_count"], 7)
        self.assertEqual(hz["daily_day_count"], 4)
        self.assertEqual(hz["cube_day_count"], 5)


class NoCubeTransition(unittest.TestCase):
    """(9) with NO cube configured the app must behave exactly like P1 (daily-only)."""

    @classmethod
    def setUpClass(cls):
        cls._prev = {k: os.environ.get(k) for k in _ENV_KEYS}
        cls.tmp = tempfile.mkdtemp(prefix="p4pointavail_nocube_")
        cls.days = _days("2026-07-01", "2026-07-05")
        cls.daily = os.path.join(cls.tmp, "daily")
        for d in cls.days:
            _build_day(cls.daily, d)
        os.environ["GHRSST_ZARR_PATH"] = cls.daily
        os.environ.pop("GHRSST_TIMECUBE_PATH", None)
        os.environ.pop("GHRSST_DELTACUBE_PATH", None)
        os.environ["GHRSST_SPATIAL_WINDOW_ENFORCE"] = "1"    # no delta -> gate is a no-op
        import api.app as appmod
        cls.appmod = importlib.reload(appmod)
        cls._cm = TestClient(cls.appmod.app)
        cls.client = cls._cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls._cm.__exit__(None, None, None)
        for k, v in cls._prev.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        import api.app as appmod
        importlib.reload(appmod)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_daily_only_point_and_healthz(self):
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": "2026-07-02", "end": "2026-07-04"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([x["date"] for x in r.json()], _days("2026-07-02", "2026-07-04"))
        self.assertEqual(r.headers.get("X-Store-Route"), "daily")
        hz = self.client.get("/healthz").json()
        self.assertFalse(hz["cube_loaded"])
        self.assertEqual(hz["earliest"], "2026-07-01")        # daily IS the availability authority
        self.assertEqual(hz["latest"], "2026-07-05")
        self.assertEqual(hz["daily_day_count"], 5)
        self.assertEqual(hz["point_day_count"], 5)

    def test_daily_only_bbox_and_out_of_range(self):
        r = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "lon1": 112.0, "lat1": 14.0,
            "start": "2026-07-03", "end": "2026-07-03"})
        self.assertEqual(r.status_code, 200, r.text)         # no delta -> gate no-op
        r2 = self.client.get("/api/ghrsst", params={
            "lon0": 110.0, "lat0": 12.0, "start": "2020-01-01", "end": "2020-01-02"})
        self.assertEqual(r2.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
