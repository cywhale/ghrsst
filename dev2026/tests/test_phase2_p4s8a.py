"""dev2026 — P4-S8a: swap executor + the symlink cached-handle PROOF TEST. STAGING/SHADOW ONLY.

The proof test (TestCachedHandleProof) pins the empirically-observed zarr behavior that decides S1's
production eligibility (design spec §2 / §9-Q1):

  VERDICT: pre-refresh reads through a retargeted path are **SILENTLY WRONG** —
  old `_Meta.day_index` × NEW target bytes (same-shape target), and silent `None` (shrunk target,
  missing chunk -> fill -> "absent"), never an error. Therefore S1 (and S2, same path-reuse window)
  is NOT production-safe without request quiescence (PM2 restart under lock / serving-disable).

Executor tests cover: S1/S2 happy paths, staleness abort (new day + duplicate days), verify-fail
rollback, refusals, hold/manifest lines. No production paths anywhere.

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_p4s8a.py
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
from store.time_cube import TimeCubeStore  # noqa: E402
from ingest.dual_write import append_to_delta  # noqa: E402
from ingest.prune_delta import prune_delta  # noqa: E402
from ingest.swap_delta import execute_swap_plan  # noqa: E402

NY, NX = 16, 20
VARS = ("sst", "sst_anomaly", "sea_ice")


def _iso(y, m, d):
    return datetime.date(y, m, d).isoformat()


def _mk_daily(tmp, days, base_val=100.0, name="daily"):
    daily = os.path.join(tmp, name)
    lon = np.linspace(100, 130, NX).astype(np.float32)
    lat = np.linspace(0, 30, NY).astype(np.float32)
    for i, d in enumerate(days):
        g = zarr.open_group(group_path(daily, d), mode="w", zarr_format=3)
        g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,)); g["lon"][:] = lon
        g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,)); g["lat"][:] = lat
        for v in VARS:
            g.create_array(v, shape=(1, NY, NX), dtype="float32", chunks=(1, 8, 8))
            g[v][0] = np.full((NY, NX), base_val + i, np.float32)
    return daily


def _mk_delta(tmp, daily, days, name="delta"):
    delta = os.path.join(tmp, name)
    for d in days:
        append_to_delta(daily, delta, d, spatial_chunk=8, shard_spatial=8)
    return delta


def _days_attr(path):
    return list(zarr.open_group(path, mode="r").attrs["days"])


def _manifest_lines(hold_dir):
    p = os.path.join(hold_dir, "manifest.jsonl")
    if not os.path.isfile(p):
        return []
    with open(p) as fh:
        return [json.loads(x) for x in fh if x.strip()]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p4s8a_")
        self.hold = os.path.join(self.tmp, "hold")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestCachedHandleProof(Base):
    """THE S1 eligibility gate (§2). Pins the observed zarr/TimeCubeStore behavior across a symlink
    retarget: silent old-meta x new-bytes mixing — NOT stable-old, NOT an error."""

    def _two_deltas(self):
        days = [_iso(2026, 6, d) for d in range(1, 5)]
        dA = _mk_daily(self.tmp, days, base_val=100.0, name="dailyA")   # A: day i -> 100+i
        dB = _mk_daily(self.tmp, days, base_val=500.0, name="dailyB")   # B: same days -> 500+i
        deltaA = _mk_delta(self.tmp, dA, days, name="deltaA")
        deltaB = _mk_delta(self.tmp, dB, days, name="deltaB")
        return days, deltaA, deltaB

    def test_pre_refresh_read_is_silent_mixing(self):
        days, deltaA, deltaB = self._two_deltas()
        link = os.path.join(self.tmp, "delta_live")
        os.symlink(deltaA, link)
        store = TimeCubeStore(link)
        pre = store.point_series(115.0, 12.0, [days[1]], ["sst"])[0]["sst"]
        self.assertEqual(pre, 101.0)                                    # A's value for day1
        # atomic retarget A -> B (path switch IS atomic)
        tmp = link + ".swap-tmp"; os.symlink(deltaB, tmp); os.replace(tmp, link)
        # PRE-REFRESH read: the cached store returns B's BYTES against A's day_index — silent mixing.
        mid = store.point_series(115.0, 12.0, [days[1]], ["sst"])[0]["sst"]
        self.assertEqual(mid, 501.0,
                         "expected the documented SILENT-MIXING behavior (new bytes through old meta); "
                         "if this fails, zarr's path-resolution semantics changed — re-run the S1 verdict")
        self.assertNotEqual(mid, pre)                                   # ...and it is NOT stable-old
        # POST-REFRESH: wholly-new, correct
        store.refresh()
        post = store.point_series(115.0, 12.0, [days[1]], ["sst"])[0]["sst"]
        self.assertEqual(post, 501.0)
        self.assertEqual(store.latest, max(days))

    def test_shrunk_target_pre_refresh_is_silent_none(self):
        # Retarget to a SMALLER delta: reading a day index beyond the new target does not raise — the
        # missing chunk resolves to fill (NaN) -> API semantics turn it into None. Fully silent.
        days, deltaA, _ = self._two_deltas()
        dB = os.path.join(self.tmp, "dailyB")
        deltaC = _mk_delta(self.tmp, dB, days[:2], name="deltaC")       # only 2 days
        link = os.path.join(self.tmp, "delta_live2")
        os.symlink(deltaA, link)
        store = TimeCubeStore(link)
        self.assertEqual(store.point_series(115.0, 12.0, [days[3]], ["sst"])[0]["sst"], 103.0)
        tmp = link + ".swap-tmp"; os.symlink(deltaC, tmp); os.replace(tmp, link)
        row = store.point_series(115.0, 12.0, [days[3]], ["sst"])[0]    # t=3 beyond C's 2 slabs
        self.assertIsNone(row["sst"],
                          "expected the documented silent-None behavior for a shrunk target")
        # conclusion encoded: neither outcome is 'stable-old or error' -> S1 fails the §2 eligibility
        # bar; production swap REQUIRES quiescence (restart / serving-disable). See results doc.


class _Fixture(Base):
    """daily(8 days) -> delta(8 days) -> plan dropping the 3 oldest (window=4, buffer=1)."""

    def build(self, live_name="delta", symlink=False):
        self.days = [_iso(2026, 6, d) for d in range(1, 9)]
        self.daily = _mk_daily(self.tmp, self.days)
        real = _mk_delta(self.tmp, self.daily, self.days, name=live_name)
        if symlink:
            self.live = os.path.join(self.tmp, "delta_live")
            os.symlink(real, self.live)
        else:
            self.live = real
        self.keep = self.days[3:]                     # drop 06-01..06-03
        self.staging = os.path.join(self.tmp, "new_delta")
        plan = prune_delta(self.live, self.staging, self.keep, source_daily=self.daily,
                           base_days=self.days, spatial_window_days=4)
        assert plan["status"] == "ok", plan
        return plan


class TestS2Executor(_Fixture):
    def test_happy_path(self):
        plan = self.build()
        store = TimeCubeStore(self.live)              # a live reader; refresh_fn simulates step 7
        res = execute_swap_plan(plan, mode="s2", hold_dir=self.hold, refresh_fn=store.refresh,
                                operator="p4s8a-test")
        self.assertEqual(res["status"], "swapped")
        self.assertTrue(res["swap_performed"])
        self.assertEqual(_days_attr(self.live), sorted(self.keep))     # live now = pruned delta
        self.assertEqual(store.days, sorted(self.keep))                # reader refreshed
        # backup moved into hold, still readable, contains the ORIGINAL 8 days
        self.assertTrue(res["backup"].startswith(self.hold))
        self.assertEqual(sorted(_days_attr(res["backup"])), self.days)
        # manifest line
        lines = _manifest_lines(self.hold)
        self.assertEqual(len(lines), 1)
        m = lines[0]
        self.assertEqual(m["op"], "delta_prune_swap")
        self.assertEqual(m["dropped_days"], self.days[:3])
        self.assertEqual(m["keep_latest"], self.days[-1])
        self.assertIn("hold_until", m)
        self.assertIn("ops-only", m["hard_delete"])

    def test_s2_refuses_symlink_live(self):
        plan = self.build(symlink=True)
        res = execute_swap_plan(plan, mode="s2", hold_dir=self.hold)
        self.assertEqual(res["status"], "refused")


class TestS1Executor(_Fixture):
    def test_happy_path_symlink(self):
        plan = self.build(symlink=True)
        old_target = os.path.realpath(self.live)
        store = TimeCubeStore(self.live)
        res = execute_swap_plan(plan, mode="s1", hold_dir=self.hold, refresh_fn=store.refresh)
        self.assertEqual(res["status"], "swapped")
        self.assertEqual(os.path.realpath(self.live), os.path.realpath(self.staging))
        self.assertEqual(_days_attr(self.live), sorted(self.keep))
        self.assertEqual(store.days, sorted(self.keep))
        # backup = old target dir, record-only, untouched in place
        self.assertEqual(res["backup"], old_target)
        self.assertTrue(os.path.isdir(old_target))
        self.assertEqual(sorted(_days_attr(old_target)), self.days)

    def test_s1_refuses_non_symlink_live(self):
        plan = self.build(symlink=False)
        res = execute_swap_plan(plan, mode="s1", hold_dir=self.hold)
        self.assertEqual(res["status"], "refused")


class TestStalenessGuard(_Fixture):
    def test_new_day_after_plan_aborts(self):
        plan = self.build()
        extra = _iso(2026, 6, 9)
        d2 = _mk_daily(self.tmp, [extra], base_val=200.0, name="daily_extra")
        append_to_delta(d2, self.live, extra, spatial_chunk=8, shard_spatial=8)   # cron beat us to it
        before = _days_attr(self.live)
        res = execute_swap_plan(plan, mode="s2", hold_dir=self.hold)
        self.assertEqual(res["status"], "aborted_stale")
        self.assertEqual(res["unexpected_new_days"], [extra])
        self.assertFalse(res["swap_performed"])
        self.assertEqual(_days_attr(self.live), before)                 # live untouched
        self.assertTrue(os.path.isdir(self.staging))                    # staging kept for rebuild

    def test_duplicate_live_day_aborts(self):
        plan = self.build()
        g = zarr.open_group(self.live, mode="a")                        # forge a duplicate (test-only)
        g.attrs["days"] = list(g.attrs["days"]) + [self.days[0]]
        res = execute_swap_plan(plan, mode="s2", hold_dir=self.hold)
        self.assertEqual(res["status"], "aborted_stale")
        self.assertIn("duplicate", res["reason"])
        self.assertFalse(res["swap_performed"])


class TestVerifyFailRollback(_Fixture):
    def test_rollback_restores_original(self):
        plan = self.build()
        store = TimeCubeStore(self.live)
        bad = lambda live, keep: {"ok": False, "why": "injected failure"}   # noqa: E731
        res = execute_swap_plan(plan, mode="s2", hold_dir=self.hold, refresh_fn=store.refresh,
                                verifier=bad)
        self.assertEqual(res["status"], "rolled_back")
        self.assertTrue(res["restored_ok"])
        self.assertFalse(res["swap_performed"])
        self.assertEqual(sorted(_days_attr(self.live)), self.days)      # original live restored
        self.assertEqual(store.days, self.days)                         # reader re-refreshed to original
        self.assertTrue(os.path.isdir(self.staging))                    # new delta back at staging path
        self.assertEqual(_days_attr(self.staging), sorted(self.keep))
        lines = _manifest_lines(self.hold)                              # rollback audited
        self.assertEqual([m["op"] for m in lines], ["swap_rollback"])

    def test_rollback_s1(self):
        plan = self.build(symlink=True)
        old_target = os.path.realpath(self.live)
        bad = lambda live, keep: {"ok": False}                          # noqa: E731
        res = execute_swap_plan(plan, mode="s1", hold_dir=self.hold, verifier=bad)
        self.assertEqual(res["status"], "rolled_back")
        self.assertEqual(os.path.realpath(self.live), old_target)       # symlink retargeted back
        self.assertEqual(sorted(_days_attr(self.live)), self.days)


class TestRefreshExceptionRollback(_Fixture):
    """A raising refresh_fn must NOT leave a swapped live delta behind (Codex S8a-review #1):
    it is treated as a verify failure -> rollback under the still-held lock + manifest."""

    def test_s2_refresh_raises_rolls_back(self):
        plan = self.build()
        def boom():                                    # raises on EVERY call, incl. during rollback —
            raise RuntimeError("refresh blew up")      # the executor's authoritative re-read still decides
        res = execute_swap_plan(plan, mode="s2", hold_dir=self.hold, refresh_fn=boom)
        self.assertEqual(res["status"], "rolled_back")
        self.assertIn("refresh/verify raised", res["reason"])
        self.assertTrue(res["restored_ok"])
        self.assertFalse(res["swap_performed"])
        self.assertEqual(sorted(_days_attr(self.live)), self.days)      # original live restored
        self.assertTrue(os.path.isdir(self.staging))                    # staging preserved for retry
        self.assertEqual(_days_attr(self.staging), sorted(self.keep))
        lines = _manifest_lines(self.hold)                              # rollback audited with the reason
        self.assertEqual([m["op"] for m in lines], ["swap_rollback"])
        self.assertIn("refresh/verify raised", lines[0]["reason"])

    def test_s1_refresh_raises_retargets_back(self):
        plan = self.build(symlink=True)
        old_target = os.path.realpath(self.live)
        def boom():
            raise RuntimeError("refresh blew up")
        res = execute_swap_plan(plan, mode="s1", hold_dir=self.hold, refresh_fn=boom)
        self.assertEqual(res["status"], "rolled_back")
        self.assertEqual(os.path.realpath(self.live), old_target)       # symlink retargeted back
        self.assertEqual(sorted(_days_attr(self.live)), self.days)
        self.assertTrue(res["restored_ok"])


class TestHoldDirFilesystemPrecheck(_Fixture):
    """S2 renames live -> hold_dir/<backup>; a cross-device hold_dir must be refused BEFORE any swap
    (Codex S8a-review #2). Simulated by patching the factored _st_dev helper."""

    def test_cross_device_hold_dir_refuses(self):
        from unittest import mock
        import ingest.swap_delta as sd
        plan = self.build()
        hold_abs = os.path.abspath(self.hold)
        real_st_dev = sd._st_dev
        fake = lambda p: 99999 if os.path.abspath(p) == hold_abs else real_st_dev(p)   # noqa: E731
        before = _days_attr(self.live)
        with mock.patch.object(sd, "_st_dev", side_effect=fake):
            res = execute_swap_plan(plan, mode="s2", hold_dir=self.hold)
        self.assertEqual(res["status"], "refused")
        self.assertIn("hold_dir", res["reason"])
        self.assertFalse(res["swap_performed"])
        self.assertEqual(_days_attr(self.live), before)                 # nothing touched
        self.assertTrue(os.path.isdir(self.staging))

    def test_s1_ignores_hold_dir_device(self):
        # S1 never renames into hold_dir (record-only backup) -> a cross-device hold_dir is fine.
        from unittest import mock
        import ingest.swap_delta as sd
        plan = self.build(symlink=True)
        hold_abs = os.path.abspath(self.hold)
        real_st_dev = sd._st_dev
        fake = lambda p: 99999 if os.path.abspath(p) == hold_abs else real_st_dev(p)   # noqa: E731
        with mock.patch.object(sd, "_st_dev", side_effect=fake):
            res = execute_swap_plan(plan, mode="s1", hold_dir=self.hold)
        self.assertEqual(res["status"], "swapped")


class TestRefusals(_Fixture):
    def test_refuses_non_ok_plan(self):
        self.build()
        res = execute_swap_plan({"status": "refused", "reason": "x"}, mode="s2", hold_dir=self.hold)
        self.assertEqual(res["status"], "refused")

    def test_refuses_bad_mode(self):
        plan = self.build()
        with self.assertRaises(ValueError):
            execute_swap_plan(plan, mode="s3", hold_dir=self.hold)


if __name__ == "__main__":
    unittest.main(verbosity=2)
