"""dev2026 — P4-S7: daily-staging prune + validation manifest tests. STAGING/SHADOW ONLY.

Proves `ingest.prune_staging.prune_daily_staging`:
- dry-run by DEFAULT (computes + validates + emits dry-run manifest, moves nothing);
- real run MOVES eligible day groups to hold (never deletes), manifest per §7 conventions;
- global recent-window gate refuses everything on a hole;
- conservative mode never prunes a day not covered by base;
- per-day validation (coverage tier delta-else-base, var_valid agreement, sample parity) skips bad days;
- accepted_risk + uncovered day requires the explicit allow_redownload_only flag;
- cross-device hold_dir refused; untouched days byte-identical.

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_p4s7.py
"""
from __future__ import annotations

import datetime
import json
import os
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
from ingest.prune_staging import prune_daily_staging  # noqa: E402

NY, NX = 16, 20
VARS = ("sst", "sst_anomaly", "sea_ice")


def _iso(d):
    return datetime.date(2026, 6, d).isoformat()


def _mk_daily(tmp, days, absent=None):
    absent = absent or {}
    daily = os.path.join(tmp, "daily")
    lon = np.linspace(100, 130, NX).astype(np.float32)
    lat = np.linspace(0, 30, NY).astype(np.float32)
    for i, d in enumerate(days):
        g = zarr.open_group(group_path(daily, d), mode="w", zarr_format=3)
        g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,)); g["lon"][:] = lon
        g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,)); g["lat"][:] = lat
        for v in VARS:
            if v in absent.get(d, ()):
                continue
            g.create_array(v, shape=(1, NY, NX), dtype="float32", chunks=(1, 8, 8))
            g[v][0] = np.full((NY, NX), 10.0 * i + (hash(v) % 5), np.float32)
    return daily


def _build(tmp, n=12, base_thru=8, delta_from=5, absent=None):
    days = [_iso(d) for d in range(1, n + 1)]
    daily = _mk_daily(tmp, days, absent=absent)
    base = os.path.join(tmp, "base")
    build_timecube_bulk(daily, base, spatial_chunk=8, time_chunk=90, shard_spatial=8, read_block=8,
                        workers=2, end_day=_iso(base_thru))
    delta = os.path.join(tmp, "delta")
    for d in days[delta_from - 1:]:
        append_to_delta(daily, delta, d, spatial_chunk=8, shard_spatial=8)
    return days, daily, base, delta


def _existing_days(daily):
    from store.zarr_paths import list_existing_days
    return list_existing_days(daily)


def _snap(path):
    out = {}
    for root, _d, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            st = os.stat(fp)
            out[fp] = (st.st_mtime_ns, st.st_size)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p4s7_")
        self.hold = os.path.join(self.tmp, "hold")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_prune(self, daily, base, delta, **kw):
        kw.setdefault("spatial_window_days", 3)
        kw.setdefault("staging_buffer_days", 1)        # keep window = latest 4 calendar days
        return prune_daily_staging(daily, self.hold, delta_path=delta, base_path=base, **kw)


class TestDryRunDefault(Base):
    def test_dry_run_moves_nothing(self):
        days, daily, base, delta = _build(self.tmp)    # keep 06-09..06-12; candidates 06-01..06-08
        before = _snap(daily)
        res = self.run_prune(daily, base, delta)       # dry_run defaults to True
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["dry_run"])
        self.assertEqual(res["candidates"], days[:8])
        self.assertEqual(res["pruned"], days[:8])      # would-be pruned, all validated
        self.assertEqual(res["skipped"], [])
        self.assertEqual(_existing_days(daily), days)  # NOTHING moved
        self.assertEqual(_snap(daily), before)         # NOTHING touched
        # dry-run manifest goes to its OWN file (never pollutes the real manifest)
        self.assertTrue(res["manifest_path"].endswith("manifest.dryrun.jsonl"))
        self.assertFalse(os.path.exists(os.path.join(self.hold, "manifest.jsonl")))
        with open(res["manifest_path"]) as fh:
            recs = [json.loads(x) for x in fh]
        self.assertEqual(len(recs), 8)
        self.assertTrue(all(r["dry_run"] for r in recs))
        # tier attribution: 06-05..06-08 validated vs delta (preferred), 06-01..06-04 vs base
        by_day = {r["day"]: r for r in recs}
        for d in days[4:8]:
            self.assertEqual(by_day[d]["validated_against"], "delta")
        for d in days[:4]:
            self.assertEqual(by_day[d]["validated_against"], "base")
        self.assertFalse(res["daily_staging_mutation"])                    # dry-run mutates nothing


class TestRealRunConservative(Base):
    def test_move_to_hold(self):
        days, daily, base, delta = _build(self.tmp)
        res = self.run_prune(daily, base, delta, dry_run=False, operator="p4s7-test")
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["pruned"], days[:8])
        self.assertEqual(_existing_days(daily), days[8:])                  # kept: 06-09..06-12
        # every pruned day group MOVED (not deleted) into hold and still readable
        for rec in res["records"]:
            self.assertTrue(os.path.isdir(rec["hold_path"]))
            g = zarr.open_group(rec["hold_path"], mode="r")
            self.assertIn("sst", g)
            self.assertIn("ops-only", rec["hard_delete"])
            self.assertFalse(rec["dry_run"])
            self.assertIsNotNone(rec["hold_until"])
            self.assertTrue(rec["base_covers"])
            self.assertFalse(rec["redownload_required"])
        # real manifest (shared §7 file), one line per pruned day
        with open(os.path.join(self.hold, "manifest.jsonl")) as fh:
            self.assertEqual(len([1 for _ in fh]), 8)
        # result-field semantics (Codex S7-review #4)
        self.assertTrue(res["daily_staging_mutation"])                     # a real run mutates staging
        self.assertFalse(res["hard_delete"])                               # hold-only, never delete
        self.assertFalse(res["production_mutation"])                       # no VM24/live path touched

    def test_conservative_keeps_uncompacted(self):
        # base thru 06-06 -> 06-07/06-08 are old but NOT in base -> conservative keeps them
        days, daily, base, delta = _build(self.tmp, base_thru=6)
        res = self.run_prune(daily, base, delta, dry_run=False)
        self.assertEqual(res["candidates"], days[:6])                      # 06-07,06-08 not candidates
        self.assertIn(_iso(7), _existing_days(daily))
        self.assertIn(_iso(8), _existing_days(daily))


class TestGlobalWindowGate(Base):
    def test_recent_window_hole_refuses_all(self):
        days = [_iso(d) for d in range(1, 13)]
        daily = _mk_daily(self.tmp, days)
        base = os.path.join(self.tmp, "base")
        build_timecube_bulk(daily, base, spatial_chunk=8, time_chunk=90, shard_spatial=8,
                            read_block=8, workers=2, end_day=_iso(8))
        delta = os.path.join(self.tmp, "delta")
        for d in days[4:]:
            if d != _iso(11):                          # hole inside the latest-3 window (06-10..06-12)
                append_to_delta(daily, delta, d, spatial_chunk=8, shard_spatial=8)
        res = self.run_prune(daily, base, delta, dry_run=False)
        self.assertEqual(res["status"], "refused")
        self.assertIn("holes", res["reason"])
        self.assertEqual(_existing_days(daily), days)                      # nothing moved


class TestPerDayValidation(Base):
    def test_parity_failure_skips_day(self):
        days, daily, base, delta = _build(self.tmp)
        # tamper daily 06-02 AFTER the cubes were built -> parity vs base fails for that day only
        g = zarr.open_group(group_path(daily, _iso(2)), mode="a")
        g["sst"][0] = np.full((NY, NX), -777.0, np.float32)
        res = self.run_prune(daily, base, delta, dry_run=False)
        self.assertEqual(res["status"], "ok")
        self.assertNotIn(_iso(2), res["pruned"])
        self.assertEqual([s["day"] for s in res["skipped"]], [_iso(2)])
        self.assertIn("parity", res["skipped"][0]["reason"])
        self.assertIn(_iso(2), _existing_days(daily))                      # bad day NOT pruned
        self.assertNotIn(_iso(1), _existing_days(daily))                   # good days still pruned

    def test_absent_var_day_prunes(self):
        days, daily, base, delta = _build(self.tmp, absent={_iso(3): ("sea_ice",)})
        res = self.run_prune(daily, base, delta, dry_run=False)
        self.assertIn(_iso(3), res["pruned"])                              # absent-var agreement holds


class TestAcceptedRiskUncovered(Base):
    def _build_uncovered(self):
        # base thru 06-04, delta 06-07.. -> 06-05/06-06 have NO cube coverage
        return _build(self.tmp, base_thru=4, delta_from=7)

    def test_uncovered_skipped_without_flag(self):
        days, daily, base, delta = self._build_uncovered()
        res = self.run_prune(daily, base, delta, mode="accepted_risk", dry_run=False)
        skipped_days = [s["day"] for s in res["skipped"]]
        self.assertEqual(skipped_days, [_iso(5), _iso(6)])
        self.assertIn("allow_redownload_only", res["skipped"][0]["reason"])
        self.assertIn(_iso(5), _existing_days(daily))

    def test_uncovered_pruned_with_explicit_flag(self):
        days, daily, base, delta = self._build_uncovered()
        res = self.run_prune(daily, base, delta, mode="accepted_risk", dry_run=False,
                             allow_redownload_only=True)
        self.assertIn(_iso(5), res["pruned"])
        rec = {r["day"]: r for r in res["records"]}[_iso(5)]
        self.assertTrue(rec["redownload_required"])
        self.assertIsNone(rec["validated_against"])                        # no local parity possible
        self.assertIsNone(rec["validation"]["ok"])

    def test_conservative_never_touches_uncovered(self):
        days, daily, base, delta = self._build_uncovered()
        res = self.run_prune(daily, base, delta, mode="conservative", dry_run=False)
        self.assertNotIn(_iso(5), res["candidates"])                       # kept by the keep-set itself
        self.assertNotIn(_iso(6), res["candidates"])
        self.assertIn(_iso(5), _existing_days(daily))


class TestCrashSafeManifest(Base):
    """Codex S7-review #1: each moved day's manifest line is written+fsync'd IMMEDIATELY after its
    rename — a failure mid-run never leaves a moved day without a manifest record."""

    def test_failure_after_first_move(self):
        from unittest import mock
        import ingest.prune_staging as ps
        days, daily, base, delta = _build(self.tmp)
        calls = {"n": 0}
        real_move = ps._move.__wrapped__ if hasattr(ps._move, "__wrapped__") else os.rename

        def flaky(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_move(src, dst)             # first day moves for real
            raise OSError("disk vanished")             # every later move fails

        with mock.patch.object(ps, "_move", side_effect=flaky):
            res = self.run_prune(daily, base, delta, dry_run=False)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["pruned"], [_iso(1)])     # only the first candidate moved
        # the moved day is in hold AND its manifest line exists (written before the failures)
        rec = res["records"][0]
        self.assertTrue(os.path.isdir(rec["hold_path"]))
        self.assertEqual(rec["status"], "moved")
        self.assertEqual(rec["source_path"], group_path(daily, _iso(1)))
        with open(os.path.join(self.hold, "manifest.jsonl")) as fh:
            lines = [json.loads(x) for x in fh]
        self.assertEqual([m["day"] for m in lines], [_iso(1)])
        self.assertEqual(lines[0]["status"], "moved")
        # later days: reported as skipped with the move error, still present in daily
        skipped_days = [s["day"] for s in res["skipped"]]
        self.assertEqual(skipped_days, days[1:8])
        self.assertTrue(all("move failed" in s["reason"] for s in res["skipped"]))
        for d in days[1:]:
            self.assertTrue(os.path.isdir(group_path(daily, d)))
        # invariant: no day vanished
        for d in days:
            in_daily = os.path.isdir(group_path(daily, d))
            in_hold = (d == _iso(1)) and os.path.isdir(rec["hold_path"])
            self.assertTrue(in_daily or in_hold, f"{d} vanished")


class TestExtraVarProtection(Base):
    """Codex S7-review #2: a daily day carrying a data var the covering tier lacks must be SKIPPED —
    pruning it would lose daily-only data."""

    def test_time_coord_array_does_not_trip_extra_var_gate(self):
        # Real VM24 daily groups carry a 1-D `time` array. It is a COORDINATE, not a data var — it must
        # NOT be flagged as "daily-only data" (which would skip EVERY day). Same data-var filter as
        # prune_delta's bulk engine (name + 3-D shape).
        days, daily, base, delta = _build(self.tmp)
        for d in days:                                          # add `time` to every daily group
            g = zarr.open_group(group_path(daily, d), mode="a")
            g.create_array("time", shape=(1,), dtype="int64", chunks=(1,)); g["time"][:] = [0]
        res = self.run_prune(daily, base, delta, dry_run=False)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["pruned"], days[:8])               # nothing skipped because of `time`
        self.assertEqual(res["skipped"], [])

    def test_extra_daily_var_skips_day(self):
        days, daily, base, delta = _build(self.tmp)
        # add a data var to 06-02 AFTER the cubes were built (tier lacks it)
        g = zarr.open_group(group_path(daily, _iso(2)), mode="a")
        g.create_array("chlorophyll", shape=(1, NY, NX), dtype="float32", chunks=(1, 8, 8))
        g["chlorophyll"][0] = np.full((NY, NX), 1.5, np.float32)
        res = self.run_prune(daily, base, delta, dry_run=False)
        self.assertNotIn(_iso(2), res["pruned"])
        sk = {s["day"]: s for s in res["skipped"]}
        self.assertIn(_iso(2), sk)
        self.assertIn("chlorophyll", sk[_iso(2)]["reason"])
        self.assertIn(_iso(2), _existing_days(daily))                      # data preserved
        self.assertNotIn(_iso(1), _existing_days(daily))                   # other days still pruned


class TestHoldDstPrecheck(Base):
    """Codex S7-review #3: an already-existing hold destination skips that day (never rename-over)."""

    def test_existing_dst_skips(self):
        from unittest import mock
        import ingest.prune_staging as ps
        days, daily, base, delta = _build(self.tmp)
        fixed_id = "FIXEDRUNID"
        os.makedirs(self.hold, exist_ok=True)
        collide = os.path.join(self.hold, f"daily.day-{_iso(1)}.pre-prune-{fixed_id}")
        os.makedirs(collide)                           # pre-existing destination for day 1
        with mock.patch.object(ps, "_run_id", return_value=fixed_id):
            res = self.run_prune(daily, base, delta, dry_run=False)
        sk = {s["day"]: s for s in res["skipped"]}
        self.assertIn(_iso(1), sk)
        self.assertIn("already exists", sk[_iso(1)]["reason"])
        self.assertIn(_iso(1), _existing_days(daily))                      # day 1 untouched
        self.assertIn(_iso(2), res["pruned"])                              # others proceed


class TestSafety(Base):
    def test_cross_device_hold_refused(self):
        from unittest import mock
        import ingest.prune_staging as ps
        days, daily, base, delta = _build(self.tmp)
        hold_abs = os.path.abspath(self.hold)
        real = ps._st_dev
        fake = lambda p: 99999 if os.path.abspath(p) == hold_abs else real(p)   # noqa: E731
        with mock.patch.object(ps, "_st_dev", side_effect=fake):
            res = self.run_prune(daily, base, delta, dry_run=False)
        self.assertEqual(res["status"], "refused")
        self.assertIn("filesystem", res["reason"])
        self.assertEqual(_existing_days(daily), days)

    def test_untouched_days_byte_identical(self):
        days, daily, base, delta = _build(self.tmp)
        kept_paths = [group_path(daily, d) for d in days[8:]]
        before = {p: _snap(p) for p in kept_paths}
        self.run_prune(daily, base, delta, dry_run=False)
        for p in kept_paths:
            self.assertEqual(_snap(p), before[p])

    def test_no_hard_delete_anywhere(self):
        # every candidate ends up either still in daily or readable under hold — never gone
        days, daily, base, delta = _build(self.tmp)
        res = self.run_prune(daily, base, delta, dry_run=False)
        hold_paths = {r["day"]: r["hold_path"] for r in res["records"]}
        for d in days:
            in_daily = os.path.isdir(group_path(daily, d))
            in_hold = d in hold_paths and os.path.isdir(hold_paths[d])
            self.assertTrue(in_daily or in_hold, f"{d} vanished")


if __name__ == "__main__":
    unittest.main(verbosity=2)
