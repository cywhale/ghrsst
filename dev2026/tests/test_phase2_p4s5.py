"""dev2026 — P4-S5: read-only retention/prune-eligibility audit tool tests.

Proves the audit tool (`ops/p4_retention_audit.py`) is (a) correct on the P4-S4 §4.1 calendar/gap-aware
logic, (b) correct on append-order delta days (chronological max, never days[-1]), and (c) strictly
READ-ONLY — it mutates nothing and calls no mutating function.

Windows are deliberately SMALL (spatial_window_days=3–5) so tiny synthetic stores can exercise the
31-day-style logic without 31 days of data.

Run: dev2026/.venv/bin/python dev2026/tests/test_phase2_p4s5.py
"""
from __future__ import annotations

import contextlib
import datetime
import io
import os
import sys
import tempfile
import types
import unittest

import numpy as np
import zarr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "ops"))
from store.zarr_paths import group_path  # noqa: E402
from ingest.build_timecube_bulk import build_timecube_bulk  # noqa: E402
from ingest.dual_write import append_to_delta  # noqa: E402
import p4_retention_audit as audit  # noqa: E402  (the tool under test)

NY, NX = 16, 20
VARS = ("sst", "sst_anomaly", "sea_ice")


def _iso(y, m, d):
    return datetime.date(y, m, d).isoformat()


def _mk_daily(tmp, days):
    daily = os.path.join(tmp, "daily")
    lon = np.linspace(100, 130, NX).astype(np.float32)
    lat = np.linspace(0, 30, NY).astype(np.float32)
    for i, d in enumerate(days):
        g = zarr.open_group(group_path(daily, d), mode="w", zarr_format=3)
        g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,)); g["lon"][:] = lon
        g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,)); g["lat"][:] = lat
        for v in VARS:
            g.create_array(v, shape=(1, NY, NX), dtype="float32", chunks=(1, 8, 8))
            g[v][0] = (5 + 0.01 * i + np.zeros((NY, NX), np.float32))
    return daily


def _mk_base(tmp, daily, end_day):
    base = os.path.join(tmp, "base")
    build_timecube_bulk(daily, base, spatial_chunk=8, time_chunk=90, shard_spatial=8, read_block=8,
                        workers=2, end_day=end_day)
    return base


def _mk_delta(tmp, daily, append_order):
    """Build a delta by appending days IN THE GIVEN ORDER (to control physical append order)."""
    delta = os.path.join(tmp, "delta")
    for d in append_order:
        append_to_delta(daily, delta, d, spatial_chunk=8, shard_spatial=8)
    return delta


def _args(daily=None, base=None, delta=None, window=5, staging_buffer=2, delta_buffer=1,
          mode="conservative", measure_sizes=False, healthz_url=None):
    return types.SimpleNamespace(
        daily=daily, base=base, delta=delta, spatial_window_days=window,
        staging_buffer_days=staging_buffer, delta_buffer_days=delta_buffer, mode=mode,
        measure_sizes=measure_sizes, healthz_url=healthz_url, json_out=None, strict=False)


def _quiet_main(argv):
    """Run the CLI but swallow its (large) JSON stdout so CI output stays clean."""
    with contextlib.redirect_stdout(io.StringIO()):
        return audit.main(argv)


def _snapshot_mtimes(*paths):
    """Every file mtime+size under the given store dirs — to prove the tool changes nothing."""
    snap = {}
    for p in paths:
        if not p or not os.path.isdir(p):
            continue
        for root, _dirs, files in os.walk(p):
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
        self.tmp = tempfile.mkdtemp(prefix="p4s5_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestContiguousDelta(Base):
    """Scenario 1: sorted contiguous delta -> no append-order, recent window contiguous, prune-eligible."""

    def test_contiguous(self):
        days = [_iso(2026, 6, d) for d in range(1, 13)]          # 06-01..06-12
        daily = _mk_daily(self.tmp, days)
        base = _mk_base(self.tmp, daily, _iso(2026, 6, 4))       # base 06-01..06-04
        delta = _mk_delta(self.tmp, daily, days[4:])            # delta 06-05..06-12, in order
        rep = audit.build_report(_args(daily, base, delta, window=5))
        d = rep["stores"]["delta"]
        self.assertTrue(d["is_sorted_chronological"])
        self.assertFalse(d["append_order_differs"])
        self.assertEqual(d["latest_chronological"], _iso(2026, 6, 12))
        rw = rep["recent_spatial_window"]
        self.assertEqual((rw["window_start"], rw["window_end"]), (_iso(2026, 6, 8), _iso(2026, 6, 12)))
        self.assertTrue(rw["recent_window_contiguous"])
        self.assertEqual(rw["missing_in_window"], [])
        self.assertTrue(rw["prune_eligible"])
        self.assertEqual(rw["action"], "proceed")
        self.assertEqual(rep["audit_failures"], [])


class TestAppendOrderDelta(Base):
    """Scenario 2: recent days appended first, older days backfilled last -> physical append order differs
    from chronological, but ALL logic is driven by the chronological max (never days[-1])."""

    def test_append_order(self):
        days = [_iso(2026, 6, d) for d in range(1, 11)]          # 06-01..06-10
        daily = _mk_daily(self.tmp, days)
        base = _mk_base(self.tmp, daily, _iso(2026, 6, 2))
        # append 06-05..06-10 FIRST, then backfill 06-04,06-03,06-02,06-01 (out of order)
        order = days[4:] + list(reversed(days[:4]))
        delta = _mk_delta(self.tmp, daily, order)
        rep = audit.build_report(_args(daily, base, delta, window=5))
        d = rep["stores"]["delta"]
        # physical last is the backfilled OLD day; chronological latest is the true max
        self.assertEqual(d["latest_physical"], _iso(2026, 6, 1))
        self.assertEqual(d["latest_chronological"], _iso(2026, 6, 10))
        self.assertTrue(d["append_order_differs"])
        self.assertFalse(d["is_sorted_chronological"])
        # window MUST anchor on chronological max (06-10), not days[-1] (06-01)
        rw = rep["recent_spatial_window"]
        self.assertEqual(rw["window_end"], _iso(2026, 6, 10))
        self.assertEqual(rw["window_start"], _iso(2026, 6, 6))
        self.assertTrue(rw["recent_window_contiguous"])
        self.assertTrue(any("append" in w.lower() for w in rep["warnings"]))


class TestGappyRecentWindow(Base):
    """Scenario 3: a hole inside the recent window -> prune blocked, action repair_first, strict exit 2."""

    def test_gap_blocks_prune(self):
        days = [_iso(2026, 6, d) for d in range(1, 11)]          # 06-01..06-10
        daily = _mk_daily(self.tmp, days)
        base = _mk_base(self.tmp, daily, _iso(2026, 6, 2))
        # delta missing 06-08 (a hole inside the latest-5 window 06-06..06-10)
        keep = [d for d in days[4:] if d != _iso(2026, 6, 8)]
        delta = _mk_delta(self.tmp, daily, keep)
        args = _args(daily, base, delta, window=5)
        rep = audit.build_report(args)
        rw = rep["recent_spatial_window"]
        self.assertIn(_iso(2026, 6, 8), rw["missing_in_window"])
        self.assertFalse(rw["recent_window_contiguous"])
        self.assertFalse(rw["prune_eligible"])
        self.assertEqual(rw["action"], "repair_first")
        self.assertTrue(rep["audit_failures"])
        # --strict -> nonzero exit
        args.strict = True
        code = _quiet_main([
            "--daily", daily, "--base", base, "--delta", delta,
            "--spatial-window-days", "5", "--staging-buffer-days", "2", "--delta-buffer-days", "1",
            "--strict"])
        self.assertEqual(code, 2)
        # without --strict -> exit 0 even with the gap (report + warnings only)
        code0 = _quiet_main([
            "--daily", daily, "--base", base, "--delta", delta, "--spatial-window-days", "5"])
        self.assertEqual(code0, 0)

    def test_gap_suppresses_staging_candidates_and_manifest(self):
        # Recent-window hole MUST suppress ALL prune candidates + manifest (P4-S4 §4.1), even for daily
        # days that would otherwise be eligible (in base, outside the staging window).
        days = [_iso(2026, 6, d) for d in range(1, 13)]         # 06-01..06-12
        daily = _mk_daily(self.tmp, days)
        base = _mk_base(self.tmp, daily, _iso(2026, 6, 8))       # base covers 06-01..06-08
        # delta 06-04..06-12 but missing 06-10 -> hole inside the latest-5 window (06-08..06-12)
        appended = [d for d in days[3:] if d != _iso(2026, 6, 10)]
        delta = _mk_delta(self.tmp, daily, appended)
        rep = audit.build_report(_args(daily, base, delta, window=5, staging_buffer=1, delta_buffer=1))
        # precondition detected
        self.assertFalse(rep["recent_spatial_window"]["recent_window_contiguous"])
        self.assertIn(_iso(2026, 6, 10), rep["recent_spatial_window"]["missing_in_window"])
        # staging: candidates suppressed despite 06-01..06-03 being in-base & outside the staging window
        sk = rep["staging_keep"]
        self.assertEqual(sk["daily_prune_candidates"], [])
        self.assertEqual(sk["daily_prune_candidate_count"], 0)
        self.assertTrue(sk["blocked_by_recent_window_gap"])
        self.assertEqual(sk["action"], "repair_first")
        # manifest fully suppressed
        self.assertEqual(rep["manifest_preview"], [])
        # a warning names the suppression
        self.assertTrue(any("suppressed" in w for w in rep["warnings"]))

    def test_gap_suppresses_delta_candidates_even_when_base_covers(self):
        # Older delta days that base DOES cover would normally be delta_prune eligible; a recent-window hole
        # must still zero them (reviewer point 2).
        days = [_iso(2026, 6, d) for d in range(1, 13)]
        daily = _mk_daily(self.tmp, days)
        base = _mk_base(self.tmp, daily, _iso(2026, 6, 8))       # base covers all older delta days
        appended = [d for d in days[3:] if d != _iso(2026, 6, 10)]   # delta 06-04..06-12 minus 06-10
        delta = _mk_delta(self.tmp, daily, appended)
        rep = audit.build_report(_args(daily, base, delta, window=5, delta_buffer=1))
        dp = rep["delta_prune"]
        # drop candidates by calendar still computed (diagnostic), but ZERO eligible while the window has a hole
        self.assertTrue(dp["drop_candidates_by_calendar"])       # there ARE older days
        self.assertEqual(dp["delta_prune_candidates"], [])
        self.assertEqual(dp["delta_prune_candidate_count"], 0)
        self.assertTrue(dp["blocked_by_recent_window_gap"])
        self.assertEqual(dp["action"], "repair_first")


class TestConservativeBlocksUncompacted(Base):
    """Scenario 4: conservative mode never proposes pruning a daily day not yet in base, even when it is
    outside the recent staging window."""

    def test_conservative_keeps_uncompacted(self):
        days = [_iso(2026, 6, d) for d in range(1, 13)]          # 06-01..06-12
        daily = _mk_daily(self.tmp, days)
        base = _mk_base(self.tmp, daily, _iso(2026, 6, 3))       # base covers 06-01..06-03 only
        delta = _mk_delta(self.tmp, daily, days[3:])            # delta 06-04..06-12
        # window=3, staging_buffer=1 -> keep window = latest 4 calendar days = 06-09..06-12
        rep = audit.build_report(_args(daily, base, delta, window=3, staging_buffer=1, mode="conservative"))
        sk = rep["staging_keep"]
        cands = sk["daily_prune_candidates"]
        # days 06-04..06-08 are outside the recent window BUT not in base -> conservative keeps them
        for d in [_iso(2026, 6, x) for x in range(4, 9)]:
            self.assertNotIn(d, cands, f"{d} not in base must NOT be a conservative candidate")
        # only 06-01,06-02,06-03 (in base AND outside recent window) may be candidates
        self.assertEqual(set(cands), {_iso(2026, 6, 1), _iso(2026, 6, 2), _iso(2026, 6, 3)})
        # every candidate is covered by base
        for rec in rep["manifest_preview"]:
            self.assertTrue(rec["base_covers"])
            self.assertFalse(rec["redownload_required"])
            self.assertTrue(rec["dry_run"])


class TestAcceptedRiskFlagsRedownload(Base):
    """Scenario 5: accepted_risk reports candidates for days not in base, flagged redownload_required."""

    def test_accepted_risk_flags(self):
        days = [_iso(2026, 6, d) for d in range(1, 13)]
        daily = _mk_daily(self.tmp, days)
        base = _mk_base(self.tmp, daily, _iso(2026, 6, 3))       # base 06-01..06-03
        delta = _mk_delta(self.tmp, daily, days[3:])            # delta 06-04..06-12
        rep = audit.build_report(_args(daily, base, delta, window=3, staging_buffer=1, mode="accepted_risk"))
        sk = rep["staging_keep"]
        cands = set(sk["daily_prune_candidates"])
        # accepted_risk keeps ONLY the latest 4 calendar days (06-09..06-12); everything older is a candidate
        self.assertEqual(cands, {_iso(2026, 6, x) for x in range(1, 9)})
        # days 06-04..06-08 are candidates NOT in base -> redownload_required True
        by_day = {r["day"]: r for r in rep["manifest_preview"]}
        for x in range(4, 9):
            r = by_day[_iso(2026, 6, x)]
            self.assertFalse(r["base_covers"])
            self.assertTrue(r["redownload_required"])
        # days 06-01..06-03 ARE in base -> redownload not required even in accepted_risk
        for x in range(1, 4):
            r = by_day[_iso(2026, 6, x)]
            self.assertTrue(r["base_covers"])
            self.assertFalse(r["redownload_required"])


class TestDeltaPruneNeedsBaseCoverage(Base):
    """Scenario 6: delta drop candidates are only ELIGIBLE if base covers the day; un-compacted older delta
    days are blocked (compaction must run first)."""

    def test_delta_prune_base_gate(self):
        days = [_iso(2026, 6, d) for d in range(1, 13)]          # 06-01..06-12
        daily = _mk_daily(self.tmp, days)
        base = _mk_base(self.tmp, daily, _iso(2026, 6, 6))       # base 06-01..06-06
        delta = _mk_delta(self.tmp, daily, days[4:])            # delta 06-05..06-12
        # window=3, delta_buffer=1 -> keep window latest 4 days = 06-09..06-12; drop = 06-05..06-08
        rep = audit.build_report(_args(daily, base, delta, window=3, delta_buffer=1))
        dp = rep["delta_prune"]
        self.assertEqual(dp["drop_candidates_by_calendar"],
                         [_iso(2026, 6, x) for x in range(5, 9)])
        # base covers 06-05,06-06 -> eligible; 06-07,06-08 not in base -> blocked
        self.assertEqual(dp["delta_prune_candidates"], [_iso(2026, 6, 5), _iso(2026, 6, 6)])
        self.assertEqual(dp["blocked_need_compaction_first"], [_iso(2026, 6, 7), _iso(2026, 6, 8)])
        self.assertFalse(dp["base_covers_all_drop_candidates"])

    def test_delta_prune_all_covered(self):
        days = [_iso(2026, 6, d) for d in range(1, 13)]
        daily = _mk_daily(self.tmp, days)
        base = _mk_base(self.tmp, daily, _iso(2026, 6, 8))       # base 06-01..06-08 covers all drop cands
        delta = _mk_delta(self.tmp, daily, days[4:])            # delta 06-05..06-12
        rep = audit.build_report(_args(daily, base, delta, window=3, delta_buffer=1))
        dp = rep["delta_prune"]
        self.assertEqual(dp["delta_prune_candidates"], [_iso(2026, 6, x) for x in range(5, 9)])
        self.assertEqual(dp["blocked_need_compaction_first"], [])
        self.assertTrue(dp["base_covers_all_drop_candidates"])


class TestReadOnly(Base):
    """Scenario 7: the tool mutates nothing and imports/calls no mutating function."""

    def test_no_store_mutation(self):
        days = [_iso(2026, 6, d) for d in range(1, 11)]
        daily = _mk_daily(self.tmp, days)
        base = _mk_base(self.tmp, daily, _iso(2026, 6, 4))
        delta = _mk_delta(self.tmp, daily, days[4:])
        before = _snapshot_mtimes(daily, base, delta)
        self.assertTrue(before)                                  # sanity: we captured files
        # run the full report (incl. the heavy --measure-sizes read-only walk)
        audit.build_report(_args(daily, base, delta, window=5, measure_sizes=True))
        after = _snapshot_mtimes(daily, base, delta)
        self.assertEqual(before, after, "audit must not change any store file (mtime/size)")

    def test_source_has_no_mutating_calls(self):
        # Rigorous AST check (substring scan would false-positive on the docstring, which NAMES the
        # mutating functions it avoids). Verify: (a) no import of the mutating `ingest` package; (b) those
        # names are not in the module namespace (i.e. genuinely not imported); (c) every zarr.open_group
        # uses mode='r'; (d) no os.replace/rename/remove or rmtree calls.
        import ast
        with open(audit.__file__) as fh:
            tree = ast.parse(fh.read())
        # (a) no `import ingest` / `from ingest ...`
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertFalse((node.module or "").startswith("ingest"),
                                 "must not import from the mutating ingest package")
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertFalse(a.name.startswith("ingest"), "must not import ingest")
        # (b) mutating functions are NOT in the tool's namespace
        for name in ("append_to_delta", "compact", "build_timecube_bulk", "build_timecube", "upsert_day"):
            self.assertFalse(hasattr(audit, name), f"tool must not import {name}")
        # (c)/(d) inspect every call
        forbidden_attr = {"replace", "rename", "remove", "rmtree", "rmdir", "unlink", "mkdir", "makedirs"}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                self.assertNotIn(func.attr, forbidden_attr,
                                 f"tool must not call a mutating fs op: {func.attr}")
                if func.attr == "open_group":                # every zarr.open_group must be read-only
                    mode = next((kw.value for kw in node.keywords if kw.arg == "mode"), None)
                    self.assertTrue(isinstance(mode, ast.Constant) and mode.value == "r",
                                    "every zarr.open_group must use mode='r'")

    def test_json_out_is_only_write(self):
        # --json-out writes the report artifact ONLY (not any store). Verify it lands where asked.
        days = [_iso(2026, 6, d) for d in range(1, 8)]
        daily = _mk_daily(self.tmp, days)
        delta = _mk_delta(self.tmp, daily, days[2:])
        out = os.path.join(self.tmp, "report.json")
        code = _quiet_main(["--daily", daily, "--delta", delta, "--spatial-window-days", "3",
                           "--json-out", out])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(out))
        import json
        with open(out) as fh:
            rep = json.load(fh)
        self.assertEqual(rep["audit_version"], audit.AUDIT_VERSION)
        self.assertTrue(rep["read_only"])


class TestDeterminism(Base):
    """Acceptance: the logic core is a deterministic function of the day sets (disk/healthz excluded)."""

    def test_deterministic_core(self):
        days = [_iso(2026, 6, d) for d in range(1, 11)]
        daily = _mk_daily(self.tmp, days)
        base = _mk_base(self.tmp, daily, _iso(2026, 6, 3))
        delta = _mk_delta(self.tmp, daily, days[3:])
        r1 = audit.deterministic_core(audit.build_report(_args(daily, base, delta, window=5)))
        r2 = audit.deterministic_core(audit.build_report(_args(daily, base, delta, window=5)))
        import json
        self.assertEqual(json.dumps(r1, sort_keys=True), json.dumps(r2, sort_keys=True))


class TestMissingStoresGraceful(Base):
    """A missing base/delta must not crash — report present=false and keep going (VM24 read-only safety)."""

    def test_missing_paths(self):
        days = [_iso(2026, 6, d) for d in range(1, 6)]
        daily = _mk_daily(self.tmp, days)
        rep = audit.build_report(_args(daily, base="/no/such/base", delta="/no/such/delta", window=3))
        self.assertFalse(rep["stores"]["base"]["present"])
        self.assertFalse(rep["stores"]["delta"]["present"])
        self.assertFalse(rep["recent_spatial_window"]["available"])
        self.assertTrue(rep["stores"]["daily"]["present"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
