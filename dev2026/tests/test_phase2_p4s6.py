"""dev2026 — P4-S6: safe delta pruning (rebuild-then-swap) tests. STAGING/SHADOW ONLY.

Proves `ingest.prune_delta.prune_delta`:
- rebuilds a delta from a chosen keep-set into a FRESH staging path (never over the live delta);
- handles APPEND-ORDER delta days (recent then older backfill) -> new delta is CHRONOLOGICAL;
- reads old-delta days BY `day_index` (physical mapping preserved: values match by DATE);
- refuses on a recent-window gap and on missing base coverage of dropped days;
- validates day set / uniqueness / latest=max / var_valid (incl. absent var) / sample parity;
- mutates NOTHING (source delta + daily unchanged) and performs NO swap.

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_p4s6.py
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
from ingest.prune_delta import prune_delta  # noqa: E402

NY, NX = 16, 20
VARS = ("sst", "sst_anomaly", "sea_ice")


def _iso(y, m, d):
    return datetime.date(y, m, d).isoformat()


def _mk_daily(tmp, days, absent=None):
    """absent: optional {day: (var,...)} of vars to OMIT for that day (absent-var case)."""
    absent = absent or {}
    daily = os.path.join(tmp, "daily")
    lon = np.linspace(100, 130, NX).astype(np.float32)
    lat = np.linspace(0, 30, NY).astype(np.float32)
    for i, d in enumerate(days):
        g = zarr.open_group(group_path(daily, d), mode="w", zarr_format=3)
        g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,)); g["lon"][:] = lon
        g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,)); g["lat"][:] = lat
        for v in VARS:
            if v in absent.get(d, ()):                       # omit this var for this day
                continue
            # a per-(day,var) distinctive field so parity is meaningful and date-specific
            fld = (10 * i + hash(v) % 7 + 0.001 * np.arange(NY)[:, None] + 0.0001 * np.arange(NX)[None, :])
            g.create_array(v, shape=(1, NY, NX), dtype="float32", chunks=(1, 8, 8))
            g[v][0] = fld.astype(np.float32)
    return daily


def _mk_base(tmp, daily, end_day):
    base = os.path.join(tmp, "base")
    build_timecube_bulk(daily, base, spatial_chunk=8, time_chunk=90, shard_spatial=8, read_block=8,
                        workers=2, end_day=end_day)
    return base


def _mk_delta(tmp, daily, append_order, name="delta"):
    delta = os.path.join(tmp, name)
    for d in append_order:
        append_to_delta(daily, delta, d, spatial_chunk=8, shard_spatial=8)
    return delta


def _days_attr(path):
    return list(zarr.open_group(path, mode="r").attrs["days"])


def _var_valid(path):
    return {v: list(x) for v, x in dict(zarr.open_group(path, mode="r").attrs.get("var_valid", {})).items()}


def _slab(path, day, var):
    g = zarr.open_group(path, mode="r")
    days = list(g.attrs["days"]); t = days.index(day)
    return np.asarray(g[var][t, :, :], dtype=np.float32)


def _snapshot(path):
    snap = {}
    for root, _dirs, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            try:
                st = os.stat(fp)
                snap[fp] = (st.st_mtime_ns, st.st_size)
            except OSError:
                pass
    return snap


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p4s6_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def out(self, name="new_delta"):
        return os.path.join(self.tmp, name)


class TestAppendOrderRebuild(Base):
    """Append-order delta (recent days then older backfill) -> prune rebuilds a CHRONOLOGICAL delta, and
    each kept day's values match the ORIGINAL by DATE (physical mapping preserved)."""

    def test_append_order_rebuild_from_delta(self):
        days = [_iso(2026, 6, d) for d in range(1, 11)]          # 06-01..06-10
        daily = _mk_daily(self.tmp, days)
        # delta append order: 06-05..06-10 first, then backfill 06-04..06-01 (out of order)
        order = days[4:] + list(reversed(days[:4]))
        delta = _mk_delta(self.tmp, daily, order)
        self.assertNotEqual(_days_attr(delta), sorted(_days_attr(delta)))   # physically append-order
        self.assertEqual(_days_attr(delta)[-1], _iso(2026, 6, 1))           # tail is the backfilled OLD day

        keep = days[3:]                                          # keep 06-04..06-10 (drop 06-01..06-03)
        # source ONLY from old delta (no daily) -> exercises read-by-day_index
        plan = prune_delta(delta, self.out(), keep, source_delta=delta, base_days=days,
                           spatial_window_days=5)
        self.assertEqual(plan["status"], "ok")
        self.assertEqual(plan["source"], "delta")

        new_days = _days_attr(self.out())
        self.assertEqual(new_days, sorted(keep))               # CHRONOLOGICAL + exactly the kept days
        self.assertEqual(len(new_days), len(set(new_days)))    # unique
        self.assertEqual(max(new_days), new_days[-1])          # latest == max == last (now chronological)
        # physical mapping preserved: each kept day's slab equals the ORIGINAL delta slab for that DATE
        for d in keep:
            for v in VARS:
                np.testing.assert_array_equal(_slab(self.out(), d, v), _slab(delta, d, v))

    def test_rebuild_from_daily_source(self):
        days = [_iso(2026, 6, d) for d in range(1, 11)]
        daily = _mk_daily(self.tmp, days)
        order = days[4:] + list(reversed(days[:4]))
        delta = _mk_delta(self.tmp, daily, order)
        keep = days[3:]
        plan = prune_delta(delta, self.out(), keep, source_daily=daily, base_days=days,
                           spatial_window_days=5)
        self.assertEqual(plan["status"], "ok")
        self.assertIn("daily", plan["source"])
        self.assertEqual(_days_attr(self.out()), sorted(keep))
        self.assertTrue(plan["validation"]["parity_ok"])
        self.assertTrue(plan["validation"]["all_ok"])


class TestRecentWindowGate(Base):
    """A recent-window hole must PREVENT the prune (status refused, nothing built)."""

    def test_gap_refuses(self):
        days = [_iso(2026, 6, d) for d in range(1, 11)]
        daily = _mk_daily(self.tmp, days)
        # delta missing 06-08 -> hole inside the latest-5 window (06-06..06-10)
        appended = [d for d in days if d != _iso(2026, 6, 8)]
        delta = _mk_delta(self.tmp, daily, appended)
        keep = [d for d in days[3:] if d != _iso(2026, 6, 8)]
        plan = prune_delta(delta, self.out(), keep, source_delta=delta, spatial_window_days=5)
        self.assertEqual(plan["status"], "refused")
        self.assertIn("recent spatial window", plan["reason"])
        self.assertFalse(os.path.exists(self.out()))           # nothing was built
        self.assertFalse(plan["swap_performed"])


class TestBaseCoverageGate(Base):
    """A dropped day not covered by base must PREVENT the prune (compaction must run first)."""

    def test_uncovered_drop_refuses(self):
        days = [_iso(2026, 6, d) for d in range(1, 11)]
        daily = _mk_daily(self.tmp, days)
        delta = _mk_delta(self.tmp, daily, days)               # delta 06-01..06-10 contiguous
        base = _mk_base(self.tmp, daily, _iso(2026, 6, 2))     # base covers 06-01..06-02 ONLY
        keep = days[3:]                                        # drop 06-01,06-02,06-03
        base_days = _days_attr(base)
        # 06-03 is dropped but NOT in base -> refuse
        plan = prune_delta(delta, self.out(), keep, source_delta=delta, base_days=base_days,
                           spatial_window_days=5)
        self.assertEqual(plan["status"], "refused")
        self.assertIn(_iso(2026, 6, 3), plan.get("base_uncovered", []))
        self.assertFalse(os.path.exists(self.out()))

    def test_covered_drop_ok(self):
        days = [_iso(2026, 6, d) for d in range(1, 11)]
        daily = _mk_daily(self.tmp, days)
        delta = _mk_delta(self.tmp, daily, days)
        base = _mk_base(self.tmp, daily, _iso(2026, 6, 5))     # base covers 06-01..06-05 (all dropped)
        keep = days[3:]                                        # drop 06-01,06-02,06-03 (all in base)
        plan = prune_delta(delta, self.out(), keep, source_delta=delta, base_days=_days_attr(base),
                           spatial_window_days=5)
        self.assertEqual(plan["status"], "ok")


class TestVarValidPreserved(Base):
    """var_valid preserved, including a day where a var is ABSENT (must stay valid False + NaN slab)."""

    def test_absent_var_preserved(self):
        days = [_iso(2026, 6, d) for d in range(1, 9)]         # 06-01..06-08
        absent = {_iso(2026, 6, 6): ("sea_ice",)}              # 06-06 has no sea_ice
        daily = _mk_daily(self.tmp, days, absent=absent)
        delta = _mk_delta(self.tmp, daily, days)
        keep = days[2:]                                        # keep 06-03..06-08 (incl. the absent-var day)
        plan = prune_delta(delta, self.out(), keep, source_delta=delta, base_days=days,
                           spatial_window_days=4)
        self.assertEqual(plan["status"], "ok")
        self.assertTrue(plan["validation"]["var_valid_ok"])
        # the absent var for 06-06 is valid False in the new delta
        new_vv = _var_valid(self.out())
        t = _days_attr(self.out()).index(_iso(2026, 6, 6))
        self.assertFalse(new_vv["sea_ice"][t])
        # and present vars for that day are valid True
        self.assertTrue(new_vv["sst"][t])
        # the absent slab is all-NaN
        self.assertTrue(np.all(np.isnan(_slab(self.out(), _iso(2026, 6, 6), "sea_ice"))))


class TestNoMutationNoSwap(Base):
    """Source delta + daily are byte-identical after prune; no swap is performed."""

    def test_sources_unchanged(self):
        days = [_iso(2026, 6, d) for d in range(1, 11)]
        daily = _mk_daily(self.tmp, days)
        delta = _mk_delta(self.tmp, daily, days)
        before_delta = _snapshot(delta)
        before_daily = _snapshot(daily)
        plan = prune_delta(delta, self.out(), days[3:], source_daily=daily, source_delta=delta,
                           base_days=days, spatial_window_days=5)
        self.assertEqual(plan["status"], "ok")
        self.assertEqual(_snapshot(delta), before_delta, "source delta must be unchanged")
        self.assertEqual(_snapshot(daily), before_daily, "source daily must be unchanged")
        # swap plan present but NOT performed; live delta path untouched
        self.assertFalse(plan["swap_plan"]["performed"])
        self.assertFalse(plan["production_mutation"])
        self.assertEqual(plan["swap_plan"]["to"], delta)

    def test_refuses_out_equals_delta(self):
        days = [_iso(2026, 6, d) for d in range(1, 6)]
        daily = _mk_daily(self.tmp, days)
        delta = _mk_delta(self.tmp, daily, days)
        with self.assertRaises(ValueError):
            prune_delta(delta, delta, days, source_delta=delta, spatial_window_days=3)

    def test_refuses_existing_out_path(self):
        days = [_iso(2026, 6, d) for d in range(1, 6)]
        daily = _mk_daily(self.tmp, days)
        delta = _mk_delta(self.tmp, daily, days)
        out = self.out()
        os.makedirs(out)                                       # pre-existing -> must refuse to clobber
        with self.assertRaises(ValueError):
            prune_delta(delta, out, days, source_delta=delta, spatial_window_days=3)


class TestAuditCrossCheck(Base):
    """`audit_recent_window` may only CORROBORATE local recomputation — never force-pass a local gap."""

    def test_audit_claiming_ok_cannot_bypass_local_gap(self):
        # Gappy delta; caller passes an audit dict falsely claiming the window is contiguous. LOCAL recompute
        # detects the gap -> the mismatch is fail-closed: refuse, build nothing, no swap plan.
        days = [_iso(2026, 6, d) for d in range(1, 11)]
        daily = _mk_daily(self.tmp, days)
        appended = [d for d in days if d != _iso(2026, 6, 8)]  # hole at 06-08 inside latest-5 window
        delta = _mk_delta(self.tmp, daily, appended)
        keep = [d for d in days[3:] if d != _iso(2026, 6, 8)]
        forged = {"recent_window_contiguous": True, "missing_in_window": []}   # a lie
        plan = prune_delta(delta, self.out(), keep, source_delta=delta, base_days=days,
                           spatial_window_days=5, audit_recent_window=forged)
        self.assertEqual(plan["status"], "refused")
        self.assertFalse(os.path.exists(self.out()))           # nothing built
        self.assertNotIn("swap_plan", plan)                    # no swap plan emitted on refusal

    def test_matching_audit_proceeds(self):
        days = [_iso(2026, 6, d) for d in range(1, 11)]
        daily = _mk_daily(self.tmp, days)
        delta = _mk_delta(self.tmp, daily, days)               # contiguous
        keep = days[3:]
        audit = {"recent_window_contiguous": True, "missing_in_window": []}    # matches local recompute
        plan = prune_delta(delta, self.out(), keep, source_delta=delta, base_days=days,
                           spatial_window_days=5, audit_recent_window=audit)
        self.assertEqual(plan["status"], "ok")
        self.assertEqual(_days_attr(self.out()), sorted(keep))


class TestBaseCoverageMandatory(Base):
    """base_days=None is allowed ONLY for a pure rebuild/reorder (no dropped days)."""

    def test_dropped_with_base_none_refuses(self):
        days = [_iso(2026, 6, d) for d in range(1, 11)]
        daily = _mk_daily(self.tmp, days)
        delta = _mk_delta(self.tmp, daily, days)
        plan = prune_delta(delta, self.out(), days[3:], source_delta=delta, base_days=None,
                           spatial_window_days=5)
        self.assertEqual(plan["status"], "refused")
        self.assertIn("base_days is REQUIRED", plan["reason"])
        self.assertFalse(os.path.exists(self.out()))
        self.assertNotIn("swap_plan", plan)

    def test_pure_rebuild_base_none_ok(self):
        # keep ALL days (dropped empty) -> base_days=None is fine; append-order delta is repaired to
        # chronological (a pure rebuild/reorder).
        days = [_iso(2026, 6, d) for d in range(1, 9)]
        daily = _mk_daily(self.tmp, days)
        order = days[4:] + list(reversed(days[:4]))            # append-order (out of order)
        delta = _mk_delta(self.tmp, daily, order)
        self.assertNotEqual(_days_attr(delta), sorted(_days_attr(delta)))
        plan = prune_delta(delta, self.out(), days, source_delta=delta, base_days=None,
                           spatial_window_days=4)
        self.assertEqual(plan["status"], "ok")
        self.assertEqual(plan["dropped_days"], [])
        self.assertEqual(_days_attr(self.out()), sorted(days))  # reordered to chronological
        # values still match by date
        for d in days:
            np.testing.assert_array_equal(_slab(self.out(), d, "sst"), _slab(delta, d, "sst"))


class TestKeepPreservesActiveWindow(Base):
    """The kept set MUST retain the ENTIRE active recent spatial window — dropping a day INSIDE it is
    refused even if base covers it (would break bbox/POST after swap)."""

    def test_drop_day_inside_window_refuses(self):
        days = [_iso(2026, 6, d) for d in range(1, 11)]         # 06-01..06-10; window=5 -> active 06-06..06-10
        daily = _mk_daily(self.tmp, days)
        delta = _mk_delta(self.tmp, daily, days)               # contiguous
        keep = [d for d in days if d != _iso(2026, 6, 8)]      # drop 06-08 (INSIDE the active window)
        plan = prune_delta(delta, self.out(), keep, source_delta=delta, base_days=days,
                           spatial_window_days=5)
        self.assertEqual(plan["status"], "refused")
        self.assertIn(_iso(2026, 6, 8), plan.get("missing_from_keep_window", []))
        self.assertFalse(os.path.exists(self.out()))           # nothing built
        self.assertNotIn("swap_plan", plan)

    def test_drop_latest_window_day_refuses(self):
        days = [_iso(2026, 6, d) for d in range(1, 11)]
        daily = _mk_daily(self.tmp, days)
        delta = _mk_delta(self.tmp, daily, days)
        keep = [d for d in days if d != _iso(2026, 6, 10)]     # drop the LATEST window day
        plan = prune_delta(delta, self.out(), keep, source_delta=delta, base_days=days,
                           spatial_window_days=5)
        self.assertEqual(plan["status"], "refused")
        self.assertIn(_iso(2026, 6, 10), plan.get("missing_from_keep_window", []))
        self.assertFalse(os.path.exists(self.out()))
        self.assertNotIn("swap_plan", plan)

    def test_drop_only_older_than_window_ok(self):
        days = [_iso(2026, 6, d) for d in range(1, 11)]
        daily = _mk_daily(self.tmp, days)
        delta = _mk_delta(self.tmp, daily, days)
        keep = days[5:]                                        # keep 06-06..06-10 = exactly the active window
        plan = prune_delta(delta, self.out(), keep, source_delta=delta, base_days=days,
                           spatial_window_days=5)
        self.assertEqual(plan["status"], "ok")
        self.assertEqual(_days_attr(self.out()), sorted(keep))
        self.assertTrue(plan["validation"]["recent_window_ok"])   # post-build window invariant holds
        self.assertTrue(plan["validation"]["all_ok"])


class TestMixedSourceVarFirstAppears(Base):
    """Mixed source: some days from daily, some from delta fallback; a var first appears in a FALLBACK
    delta day -> it is created with a NaN backfill + var_valid False for the earlier days."""

    def test_var_first_appears_in_fallback_day(self):
        days = [_iso(2026, 6, d) for d in range(1, 7)]         # 06-01..06-06
        absent = {d: ("sea_ice",) for d in days[:4]}          # sea_ice ABSENT 06-01..06-04, present 06-05..06
        daily = _mk_daily(self.tmp, days, absent=absent)
        delta = _mk_delta(self.tmp, daily, days)              # delta has sea_ice appearing at 06-05
        # force 06-05,06-06 to fall back to delta by removing their daily groups
        import shutil
        for d in (_iso(2026, 6, 5), _iso(2026, 6, 6)):
            shutil.rmtree(group_path(daily, d))
        keep = days[1:]                                        # keep 06-02..06-06 (drop 06-01)
        plan = prune_delta(delta, self.out(), keep, source_daily=daily, source_delta=delta,
                           base_days=days, spatial_window_days=4)
        self.assertEqual(plan["status"], "ok")
        self.assertEqual(plan["source"], "daily+delta")        # genuinely mixed
        self.assertTrue(plan["validation"]["all_ok"])
        self.assertTrue(plan["validation"]["var_valid_ok"])
        # sea_ice: absent (False) for 06-02..06-04, present (True) for 06-05,06-06
        new_vv = _var_valid(self.out())
        new_days = _days_attr(self.out())
        self.assertEqual(new_days, sorted(keep))
        expected = [d in (_iso(2026, 6, 5), _iso(2026, 6, 6)) for d in new_days]
        self.assertEqual(new_vv["sea_ice"], expected)
        # early absent sea_ice slabs are all-NaN; a present one matches the source delta
        self.assertTrue(np.all(np.isnan(_slab(self.out(), _iso(2026, 6, 2), "sea_ice"))))
        np.testing.assert_array_equal(_slab(self.out(), _iso(2026, 6, 5), "sea_ice"),
                                      _slab(delta, _iso(2026, 6, 5), "sea_ice"))


class TestBulkEngineArtifacts(Base):
    """S9 field-failure hardening: progress/error artifacts + plan-written-only-on-success + resume."""

    def _fixture(self):
        days = [_iso(2026, 6, d) for d in range(1, 11)]
        daily = _mk_daily(self.tmp, days)
        order = days[4:] + list(reversed(days[:4]))            # append-order source delta
        delta = _mk_delta(self.tmp, daily, order)
        return days, daily, delta

    def test_progress_and_plan_artifacts(self):
        days, daily, delta = self._fixture()
        art = os.path.join(self.tmp, "art"); os.makedirs(art)
        plan = prune_delta(delta, self.out(), days[3:], source_daily=daily, base_days=days,
                           spatial_window_days=5, artifacts_dir=art)
        self.assertEqual(plan["status"], "ok")
        self.assertEqual(plan["engine"], "bulk")
        # progress journal: gates -> build_start -> var_done... -> finalized -> validation start/end -> plan
        with open(os.path.join(art, "prune_delta_progress.jsonl")) as fh:
            events = [json.loads(x)["event"] for x in fh]
        for expected in ("gates_passed", "build_start", "var_done", "build_finalized",
                         "validation_start", "validation_day", "validation_end", "plan_written"):
            self.assertIn(expected, events, f"missing journal event {expected}")
        # var_done records carry day/source/target_index/elapsed/status
        with open(os.path.join(art, "prune_delta_progress.jsonl")) as fh:
            vd = [json.loads(x) for x in fh if json.loads(x)["event"] == "var_done"]
        self.assertTrue(all({"day", "var", "source", "target_index", "elapsed_s", "status"} <= set(r)
                            for r in vd))
        # plan JSON written by the tool itself, matching the return value's keep set
        with open(os.path.join(art, "prune_plan.json")) as fh:
            disk_plan = json.load(fh)
        self.assertEqual(disk_plan["keep_days"], plan["keep_days"])
        self.assertFalse(os.path.exists(os.path.join(art, "prune_delta_error.json")))

    def test_refusal_writes_no_plan(self):
        days, daily, delta = self._fixture()
        art = os.path.join(self.tmp, "art"); os.makedirs(art)
        plan = prune_delta(delta, self.out(), days[3:], source_daily=daily, base_days=None,   # refused
                           spatial_window_days=5, artifacts_dir=art)
        self.assertEqual(plan["status"], "refused")
        self.assertFalse(os.path.exists(os.path.join(art, "prune_plan.json")))
        with open(os.path.join(art, "prune_delta_progress.jsonl")) as fh:
            events = [json.loads(x)["event"] for x in fh]
        self.assertIn("refused", events)

    def test_failure_writes_error_artifact_and_resume_completes(self):
        # Inject a mid-build exception: fail the Nth tile write. The error artifact + progress journal must
        # suffice to determine state; resume=True then completes the build and validates green.
        from unittest import mock
        import ingest.prune_delta as pdmod
        days, daily, delta = self._fixture()
        art = os.path.join(self.tmp, "art"); os.makedirs(art)
        out = self.out()
        calls = {"n": 0}
        orig_bulk = pdmod._bulk_build

        def sabotaged_bulk(*a, **kw):
            # wrap the journal to blow up after the 3rd var_done (mid-build, deterministic)
            journal = kw["journal"]
            real_event = journal.event
            def flaky_event(**ev):
                real_event(**ev)
                if ev.get("event") == "var_done":
                    calls["n"] += 1
                    if calls["n"] == 3:
                        raise RuntimeError("injected mid-build failure")
            journal.event = flaky_event
            return orig_bulk(*a, **kw)

        with mock.patch.object(pdmod, "_bulk_build", side_effect=sabotaged_bulk):
            res = prune_delta(delta, out, days[3:], source_daily=daily, base_days=days,
                              spatial_window_days=5, artifacts_dir=art, workers=1)
        self.assertEqual(res["status"], "error")
        self.assertIn("injected", res["reason"])
        # error artifact: traceback + enough context; progress shows exactly which var-days completed
        with open(os.path.join(art, "prune_delta_error.json")) as fh:
            err = json.load(fh)
        self.assertIn("injected mid-build failure", err["error"])
        self.assertIn("Traceback", err["traceback"])
        with open(os.path.join(art, "prune_delta_progress.jsonl")) as fh:
            recs = [json.loads(x) for x in fh]
        done_vars = [(r["day"], r["var"]) for r in recs if r["event"] == "var_done"]
        self.assertEqual(len(done_vars), 3)                     # frontier is exact
        self.assertEqual(recs[-1]["event"], "error")
        self.assertFalse(os.path.exists(os.path.join(art, "prune_plan.json")))   # no plan on failure
        # ---- resume: same out_path + resume=True completes from the checkpoint ----
        art2 = os.path.join(self.tmp, "art2"); os.makedirs(art2)
        res2 = prune_delta(delta, out, days[3:], source_daily=daily, base_days=days,
                           spatial_window_days=5, artifacts_dir=art2, resume=True, workers=1)
        self.assertEqual(res2["status"], "ok")
        self.assertTrue(res2["validation"]["all_ok"])
        self.assertEqual(_days_attr(out), sorted(days[3:]))
        for d in days[3:]:                                      # values correct by DATE after resume
            np.testing.assert_array_equal(_slab(out, d, "sst"), _slab(delta, d, "sst"))
        self.assertTrue(os.path.exists(os.path.join(art2, "prune_plan.json")))

    def test_perday_engine_still_works(self):
        days, daily, delta = self._fixture()
        plan = prune_delta(delta, self.out(), days[3:], source_delta=delta, base_days=days,
                           spatial_window_days=5, engine="perday")
        self.assertEqual(plan["status"], "ok")
        self.assertEqual(plan["engine"], "perday")
        self.assertEqual(_days_attr(self.out()), sorted(days[3:]))

    def test_bulk_no_worker_resize_or_attrs(self):
        # invariant probe: mid-build (before finalize) the staging arrays already have FULL final shape
        # and NO days attr — workers never resize, metadata lands last. Checked via the journal ordering:
        # build_finalized comes strictly after the last var_done.
        days, daily, delta = self._fixture()
        art = os.path.join(self.tmp, "art"); os.makedirs(art)
        prune_delta(delta, self.out(), days[3:], source_daily=daily, base_days=days,
                    spatial_window_days=5, artifacts_dir=art)
        with open(os.path.join(art, "prune_delta_progress.jsonl")) as fh:
            events = [json.loads(x)["event"] for x in fh]
        self.assertGreater(events.index("build_finalized"), max(i for i, e in enumerate(events)
                                                                if e == "var_done"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
