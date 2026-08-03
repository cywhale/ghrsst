"""dev2026 — P5-S3: tail-block builder + compaction lock (tests FIRST).

Gates (design spec §14 P5-S3): **G7** temp disk ≈ one block, **G13** source authority and set
scope, **G14** calendar boundaries + classification, **G17** build isolation, **G18** disk gate
charges additional only, plus a peak-RSS bound proving no full slab is ever materialized.

The headline for this step is the **write-side guard**. Five review rounds of P5-S1 shared one
shape — validation reading a derived view rather than the thing itself — and every one was on
the read side, because that was the only side that existed. These tests exist to stop the same
class reappearing now that there is a writer.

Local/synthetic only: no VM24 path, no `GHRSST_*` store, nothing outside a temp dir.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np
import zarr

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import p5_fixtures as fx  # noqa: E402
from ingest.build_block import (  # noqa: E402
    BuildRefused, disk_precheck, inspect_daily_day, resolve_source_map,
)
from ingest.build_block import build_block as build_block_strict  # noqa: E402


def build_block(*a, **kw):
    """Test shim: waive the isolation requirement for the cases that are not about it.

    `build_block_strict` requires a held `CompactionLock` and a positive `hard_reserve_bytes`.
    Tests below that exercise sealing, sources, resume, memory etc. are not about isolation, so
    they waive it explicitly here — in ONE named place, rather than each silently passing
    `lock=None`. `TestIsolationIsMandatory` calls `build_block_strict` directly."""
    kw.setdefault("lock", None)
    kw.setdefault("hard_reserve_bytes", 0)
    kw.setdefault("unsafe_skip_isolation", True)
    return build_block_strict(*a, **kw)
from store import block_manifest as bm  # noqa: E402
from store.compaction_lock import (  # noqa: E402
    CompactionLock, CompactionLockBusy, CompactionLockError, refuse_if_compaction_running,
)

ANCHOR = "2026-06-27"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.s0, self.e0 = bm.block_bounds(ANCHOR, 90, 0)
        self.span = fx.calendar_span(self.s0, self.e0)

    def _delta(self, days, name="delta.zarr", **kw):
        p = os.path.join(self.tmp, name)
        fx.build_delta(p, days, **kw)
        return p

    def _daily(self, days, name="mur.zarr", **kw):
        p = os.path.join(self.tmp, name)
        fx.build_daily(p, days, **kw)
        return p

    def _out(self, name):
        return os.path.join(self.tmp, name)


# ============================================================ write-side guard
class TestWriteSideGuard(_Base):
    def test_plan_is_derived_from_an_inspection_of_what_was_written(self):
        """`build → inspect → fingerprint → plan`: the entry must describe the block on disk,
        not what the builder intended to write."""
        delta = self._delta(self.span[:30])
        plan = build_block(self._out("b0"), start_day=self.s0, end_day=self.e0,
                           classification_target=self.span[:30], delta_path=delta,
                           artifacts_dir=self.tmp)
        seg = plan["segment"]
        insp = bm.inspect_store_contract(plan["out_path"])
        self.assertEqual(seg["fingerprint"]["metadata"],
                         bm.metadata_fingerprint_from_inspection(insp))
        self.assertEqual(seg["layout"], bm.segment_layout_from_inspection(insp))
        self.assertEqual(seg["day_count"], len(insp.days))
        self.assertEqual(seg["fingerprint"]["day_digest"], bm.day_digest(insp.days))

    def test_a_block_violating_the_contract_yields_no_plan(self):
        """The write-side analogue of the S1 rounds: if what we wrote is malformed, the
        builder must not be able to mint an entry for it."""
        import ingest.build_block as bbmod

        delta = self._delta(self.span[:10])
        real_create = bbmod._create

        def bad_create(out_path, days, keep_vars, ny, nx, lon, lat):
            real_create(out_path, days, keep_vars, ny, nx, lon, lat)
            g = zarr.open_group(out_path, mode="a")
            g["lon"][:] = np.linspace(131.0, 100.0, nx, dtype=np.float32)  # descending axis

        bbmod._create = bad_create
        try:
            with self.assertRaises(BuildRefused) as cm:
                build_block(self._out("bad"), start_day=self.s0, end_day=self.e0,
                            classification_target=self.span[:10], delta_path=delta,
                            artifacts_dir=self.tmp)
            self.assertIn("store contract", str(cm.exception))
        finally:
            bbmod._create = real_create
        self.assertFalse(os.path.isfile(os.path.join(self.tmp, "p5_block_plan.json")),
                         "a plan must not be written for a contract-violating block")

    def test_plan_is_written_only_on_success(self):
        delta = self._delta(self.span[:5])
        with self.assertRaises(BuildRefused):
            build_block(self._out("b0"), start_day=self.s0, end_day=self.e0,
                        classification_target=self.span[:5] + ["2020-01-01"],
                        delta_path=delta, artifacts_dir=self.tmp)
        self.assertFalse(os.path.isfile(os.path.join(self.tmp, "p5_block_plan.json")))


# ============================================================ G13: the two sets
class TestSourceSets(_Base):
    def test_rebuild_source_set_is_the_whole_successor_view(self):
        """§5.5: `rebuild_source_set` = predecessor present ∪ newly classified present — NOT
        a delta of the previous fold. A new immutable block must contain every day it claims."""
        delta = self._delta(self.span[:60])
        v1 = build_block(self._out("v1"), start_day=self.s0, end_day=self.e0,
                         classification_target=self.span[:30], delta_path=delta)
        self.assertEqual(v1["rebuild_source_set"], self.span[:30])

        v2 = build_block(self._out("v2"), start_day=self.s0, end_day=self.e0,
                         classification_target=self.span[30:60],
                         predecessor_present=self.span[:30],
                         predecessor_path=v1["out_path"], delta_path=delta)
        self.assertEqual(v2["rebuild_source_set"], self.span[:60])
        self.assertEqual(v2["classification_target"], self.span[30:60])
        self.assertEqual(len(bm.inspect_store_contract(v2["out_path"]).days), 60)

    def test_unresolved_day_refuses_before_writing(self):
        delta = self._delta(self.span[:5])
        out = self._out("b0")
        with self.assertRaises(BuildRefused) as cm:
            build_block(out, start_day=self.s0, end_day=self.e0,
                        classification_target=self.span[:5],
                        predecessor_present=[self.span[40]],      # nobody has it
                        delta_path=delta)
        self.assertIn("no source", str(cm.exception))
        self.assertFalse(os.path.exists(out), "nothing may be written before the refusal")

    def test_unknown_days_are_never_given_a_source(self):
        delta = self._delta(self.span[:30])
        plan = build_block(self._out("v1"), start_day=self.s0, end_day=self.e0,
                           classification_target=self.span[:30], delta_path=delta)
        self.assertEqual(plan["segment"]["unknown"], sorted(self.span[30:]))
        self.assertEqual(set(plan["segment"]["build_provenance"]["sources"]),
                         set(self.span[:30]))
        self.assertFalse(plan["segment"]["sealed"])

    def test_source_authority_prefers_delta_over_the_predecessor_block(self):
        """F13b in miniature: a day repaired back into delta must beat the block's stale copy,
        or the correction is frozen out and lost at the next prune."""
        delta = self._delta(self.span[:10])
        v1 = build_block(self._out("v1"), start_day=self.s0, end_day=self.e0,
                         classification_target=self.span[:10], delta_path=delta)
        smap = resolve_source_map(self.span[:10], delta_path=delta,
                                  predecessor_path=v1["out_path"])
        self.assertTrue(all(s["source_kind"] == "delta" for s in smap.values()))

        smap2 = resolve_source_map(self.span[:10], delta_path=None,
                                   predecessor_path=v1["out_path"])
        self.assertTrue(all(s["source_kind"] == "block" for s in smap2.values()))

    def test_source_order_falls_through_to_daily_then_hold(self):
        daily = self._daily(self.span[:3])
        hold = self._daily(self.span[3:5], name="hold")
        smap = resolve_source_map(self.span[:5], daily_root=daily, hold_root=hold)
        self.assertEqual([smap[d]["source_kind"] for d in self.span[:5]],
                         ["daily"] * 3 + ["hold"] * 2)


# ============================================================ F13: v1→v2→v3 across a prune
class TestTailRebuildChain(_Base):
    def test_v1_v2_v3_with_the_delta_pruned_between_versions(self):
        """The case that fails if the predecessor block is not a source: after v1 publishes
        and delta is pruned of days A, the block is the ONLY routine local source for A."""
        delta_all = self._delta(self.span[:90])
        v1 = build_block(self._out("v1"), start_day=self.s0, end_day=self.e0,
                         classification_target=self.span[:30], delta_path=delta_all)

        pruned = self._delta(self.span[30:90], name="delta_pruned.zarr")   # A is gone
        v2 = build_block(self._out("v2"), start_day=self.s0, end_day=self.e0,
                         classification_target=self.span[30:60],
                         predecessor_present=self.span[:30],
                         predecessor_path=v1["out_path"], delta_path=pruned)
        srcs = v2["segment"]["build_provenance"]["sources"]
        self.assertEqual(srcs[self.span[0]], "block", "A must come from the predecessor")
        self.assertEqual(srcs[self.span[30]], "delta", "B must come from delta")

        pruned2 = self._delta(self.span[60:90], name="delta_pruned2.zarr")
        v3 = build_block(self._out("v3"), start_day=self.s0, end_day=self.e0,
                         classification_target=self.span[60:90],
                         predecessor_present=self.span[:60],
                         predecessor_path=v2["out_path"], delta_path=pruned2)
        self.assertEqual(len(v3["rebuild_source_set"]), 90)
        self.assertTrue(v3["segment"]["sealed"], "the window is complete -> seal")
        self.assertEqual(v3["segment"]["unknown"], [])
        self.assertEqual(len(bm.inspect_store_contract(v3["out_path"]).days), 90)

    def test_a_missing_predecessor_refuses(self):
        pruned = self._delta(self.span[30:60])
        with self.assertRaises(BuildRefused) as cm:
            build_block(self._out("v2"), start_day=self.s0, end_day=self.e0,
                        classification_target=self.span[30:60],
                        predecessor_present=self.span[:30],
                        predecessor_path=None, delta_path=pruned)
        self.assertIn("no source", str(cm.exception))


# ============================================================ §7.1 fold cutoff
class TestFoldCutoff(_Base):
    def test_a_day_inside_the_protected_window_is_refused(self):
        delta = self._delta(self.span[:90])
        latest = self.span[89]
        with self.assertRaises(BuildRefused) as cm:
            build_block(self._out("b0"), start_day=self.s0, end_day=self.e0,
                        classification_target=self.span[:90], delta_path=delta,
                        window_latest_day=latest, spatial_window_days=31)
        self.assertIn("protected", str(cm.exception))

    def test_days_older_than_the_window_are_allowed(self):
        delta = self._delta(self.span[:90])
        plan = build_block(self._out("b0"), start_day=self.s0, end_day=self.e0,
                           classification_target=self.span[:50], delta_path=delta,
                           window_latest_day=self.span[89], spatial_window_days=31)
        self.assertEqual(len(plan["rebuild_source_set"]), 50)


# ============================================================ G17: the compaction lock
class TestCompactionLock(_Base):
    def test_prune_is_refused_while_a_build_holds_the_lock(self):
        lp = os.path.join(self.tmp, "p5_compaction.lock")
        self.assertIsNone(refuse_if_compaction_running(lp, operation="prune_delta"))
        with CompactionLock(lp, holder="build/test",
                            status_path=lp.replace(".lock", ".status.json")):
            r = refuse_if_compaction_running(lp, operation="prune_delta")
            self.assertIsNotNone(r)
            self.assertEqual(r["reason"], "compaction_lock_held")
            self.assertEqual(r["holder"]["holder"], "build/test")
        self.assertIsNone(refuse_if_compaction_running(lp, operation="prune_delta"),
                          "releasing must let a prune proceed")

    def test_a_second_holder_is_refused_not_blocked(self):
        lp = os.path.join(self.tmp, "p5_compaction.lock")
        with CompactionLock(lp):
            with self.assertRaises(CompactionLockBusy):
                CompactionLock(lp).acquire()

    def test_publish_time_assertion_catches_a_replaced_lock_file(self):
        """`rm` + recreate gives a NEW inode another process can lock freely while we hold the
        orphaned one — the only two-writer state reachable here."""
        lp = os.path.join(self.tmp, "p5_compaction.lock")
        with CompactionLock(lp) as lock:
            lock.assert_still_held()
            os.remove(lp)
            with self.assertRaises(CompactionLockError) as cm:
                lock.assert_still_held()
            self.assertIn("deleted", str(cm.exception))
            open(lp, "w").close()                    # recreated: different inode
            with self.assertRaises(CompactionLockError) as cm:
                lock.assert_still_held()
            self.assertIn("replaced", str(cm.exception))

    def test_a_released_lock_cannot_publish(self):
        lock = CompactionLock(os.path.join(self.tmp, "p5_compaction.lock")).acquire()
        lock.release()
        with self.assertRaises(CompactionLockError):
            lock.assert_still_held()

    def test_build_refuses_to_publish_if_the_lock_was_lost(self):
        lp = os.path.join(self.tmp, "p5_compaction.lock")
        delta = self._delta(self.span[:5])
        lock = CompactionLock(lp).acquire()
        os.remove(lp)                                 # lost mid-build
        with self.assertRaises(CompactionLockError):
            build_block(self._out("b0"), start_day=self.s0, end_day=self.e0,
                        classification_target=self.span[:5], delta_path=delta, lock=lock)
        lock.release()


# ============================================================ G17: the gate in the callers
class TestPruneAndSwapHonourTheLock(_Base):
    """The gate is only real if the operations that retarget the delta actually consult it."""

    def test_prune_delta_refuses_while_a_build_holds_the_lock(self):
        from ingest.prune_delta import prune_delta

        lp = os.path.join(self.tmp, "p5_compaction.lock")
        delta = self._delta(self.span[:10])
        with CompactionLock(lp, holder="build/test",
                            status_path=lp.replace(".lock", ".status.json")):
            r = prune_delta(delta, self._out("staging"), self.span[:5],
                            compaction_lock_path=lp)
        self.assertEqual(r["status"], "refused")
        self.assertEqual(r["reason"], "compaction_lock_held")
        self.assertFalse(os.path.exists(self._out("staging")),
                         "a refused prune must not create a staging path")

    def test_swap_refuses_while_a_build_holds_the_lock(self):
        from ingest.swap_delta import execute_swap_plan

        lp = os.path.join(self.tmp, "p5_compaction.lock")
        with CompactionLock(lp):
            r = execute_swap_plan({"status": "ok"}, mode="s2",
                                  hold_dir=self.tmp, compaction_lock_path=lp)
        self.assertEqual(r["status"], "refused")
        self.assertEqual(r["reason"], "compaction_lock_held")

    def test_without_the_lock_path_behaviour_is_unchanged(self):
        """The gate is opt-in by path, so existing callers keep working."""
        from ingest.prune_delta import prune_delta

        delta = self._delta(self.span[:10])
        r = prune_delta(delta, self._out("staging2"), self.span[:10])
        self.assertNotEqual(r.get("reason"), "compaction_lock_held")


# ============================================================ G18: the disk gate
class TestDiskPrecheck(_Base):
    def test_pinned_existing_is_reported_but_never_charged(self):
        """The v3-sealing case: charging predecessor + superseded again would refuse the very
        fold that ends the cycle."""
        a = disk_precheck(self.tmp, block_bytes_estimate=100, hard_reserve_bytes=0,
                          pinned_existing_bytes=0)
        b = disk_precheck(self.tmp, block_bytes_estimate=100, hard_reserve_bytes=0,
                          pinned_existing_bytes=10 ** 12)
        self.assertEqual(a["additional_bytes"], b["additional_bytes"])
        self.assertEqual(a["projected_free_after_bytes"], b["projected_free_after_bytes"])
        self.assertEqual(b["pinned_existing_bytes"], 10 ** 12)

    def test_gate_refuses_below_the_reserve(self):
        d = disk_precheck(self.tmp, block_bytes_estimate=1, hard_reserve_bytes=10 ** 18)
        self.assertFalse(d["ok"])

    def test_build_refuses_when_the_precheck_fails(self):
        delta = self._delta(self.span[:5])
        with self.assertRaises(BuildRefused) as cm:
            build_block(self._out("b0"), start_day=self.s0, end_day=self.e0,
                        classification_target=self.span[:5], delta_path=delta,
                        hard_reserve_bytes=10 ** 18)
        self.assertIn("disk precheck", str(cm.exception))


# ============================================================ resume + artifacts
class TestResumeAndArtifacts(_Base):
    def test_resume_completes_after_an_interrupted_build(self):
        delta = self._delta(self.span[:20])
        out = self._out("b0")
        import ingest.build_block as bbmod

        real_mark, n = bbmod._mark, {"i": 0}

        def dying_mark(path, key):
            n["i"] += 1
            if n["i"] > 12:
                raise RuntimeError("simulated interruption")
            return real_mark(path, key)

        bbmod._mark = dying_mark
        try:
            with self.assertRaises(RuntimeError):
                build_block(out, start_day=self.s0, end_day=self.e0,
                            classification_target=self.span[:20], delta_path=delta,
                            artifacts_dir=self.tmp)
        finally:
            bbmod._mark = real_mark

        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "p5_block_build_error.json")))
        self.assertFalse(os.path.isfile(os.path.join(self.tmp, "p5_block_plan.json")))

        plan = build_block(out, start_day=self.s0, end_day=self.e0,
                           classification_target=self.span[:20], delta_path=delta,
                           artifacts_dir=self.tmp, resume=True)
        insp = bm.inspect_store_contract(out)
        self.assertEqual(list(insp.days), self.span[:20])
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, "p5_block_plan.json")))

        # values must be right BY DATE, not merely present
        src = zarr.open_group(delta, mode="r")
        got = zarr.open_group(out, mode="r")
        for t in (0, 7, 19):
            self.assertTrue(np.allclose(np.asarray(src["sst"][t, :4, :4]),
                                        np.asarray(got["sst"][t, :4, :4]), equal_nan=True))

    def test_refuses_to_build_over_an_existing_path_without_resume(self):
        delta = self._delta(self.span[:5])
        out = self._out("b0")
        build_block(out, start_day=self.s0, end_day=self.e0,
                    classification_target=self.span[:5], delta_path=delta)
        with self.assertRaises(BuildRefused) as cm:
            build_block(out, start_day=self.s0, end_day=self.e0,
                        classification_target=self.span[:5], delta_path=delta)
        self.assertIn("already exists", str(cm.exception))

    def test_progress_journal_is_fsynced_per_event(self):
        delta = self._delta(self.span[:5])
        build_block(self._out("b0"), start_day=self.s0, end_day=self.e0,
                    classification_target=self.span[:5], delta_path=delta,
                    artifacts_dir=self.tmp)
        with open(os.path.join(self.tmp, "p5_block_build_progress.jsonl")) as fh:
            events = [json.loads(x) for x in fh if x.strip()]
        kinds = [e["event"] for e in events]
        for expected in ("source_map", "disk_precheck", "build_start", "build_finalized",
                         "plan_written"):
            self.assertIn(expected, kinds)


# ============================================================ absent vars + memory
class TestSemanticsAndMemory(_Base):
    def test_absent_var_is_preserved_as_valid_false_and_all_nan(self):
        daily = self._daily(self.span[:6], absent_vars_on={self.span[2]: ("sea_ice",)})
        plan = build_block(self._out("b0"), start_day=self.s0, end_day=self.e0,
                           classification_target=self.span[:6], daily_root=daily)
        g = zarr.open_group(plan["out_path"], mode="r")
        self.assertFalse(dict(g.attrs["var_valid"])["sea_ice"][2])
        self.assertTrue(np.all(np.isnan(np.asarray(g["sea_ice"][2, :4, :4]))))
        self.assertTrue(dict(g.attrs["var_valid"])["sea_ice"][1])

    def test_peak_rss_does_not_scale_with_grid_area(self):
        """The P4-S6 OOM lesson: a full slab is ~2.6 GB per var-day at production geometry.
        Peak RSS must be bounded by the TILE, not by ny*nx."""
        try:
            import psutil
        except ImportError:                            # pragma: no cover
            self.skipTest("psutil not installed")
        proc = psutil.Process()

        def peak_for(n):
            delta = self._delta(self.span[:4], name=f"d{n}.zarr", ny=n, nx=n)
            before = proc.memory_info().rss
            build_block(self._out(f"b{n}"), start_day=self.s0, end_day=self.e0,
                        classification_target=self.span[:4], delta_path=delta, tile=64)
            return proc.memory_info().rss - before

        small, large = peak_for(128), peak_for(512)     # 16x the area
        self.assertLess(large, small + 200 * 1024 * 1024,
                        f"RSS grew {large - small} bytes for 16x the grid area — a full slab "
                        f"is being materialized somewhere")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ============================================================ finding 2: isolation is mandatory
class TestIsolationIsMandatory(_Base):
    """`lock` and `hard_reserve_bytes` used to default to `None` and `0`. The guards existed,
    but a caller who forgot them got no isolation and a disabled disk gate, silently."""

    def setUp(self):
        super().setUp()
        self.delta = self._delta(self.span[:10])
        self.lock_path = os.path.join(self.tmp, "p5_compaction.lock")

    def _kw(self, **over):
        kw = dict(start_day=self.s0, end_day=self.e0,
                  classification_target=self.span[:10], delta_path=self.delta,
                  artifacts_dir=self.tmp)
        kw.update(over)
        return kw

    def test_both_arguments_are_required_not_defaulted(self):
        with self.assertRaises(TypeError):
            build_block_strict(self._out("b"), **self._kw())
        with self.assertRaises(TypeError):
            build_block_strict(self._out("b"), lock=None, **self._kw())

    def test_no_lock_refuses_and_writes_nothing(self):
        out = self._out("b")
        with self.assertRaises(BuildRefused) as cm:
            build_block_strict(out, lock=None, hard_reserve_bytes=1 << 20, **self._kw())
        self.assertIn("CompactionLock", str(cm.exception))
        self.assertFalse(os.path.exists(out))
        self.assertFalse(os.path.isfile(os.path.join(self.tmp, "p5_block_plan.json")))

    def test_a_released_lock_is_not_a_held_lock(self):
        lock = CompactionLock(self.lock_path)
        lock.acquire()
        lock.release()
        with self.assertRaises(BuildRefused) as cm:
            build_block_strict(self._out("b"), lock=lock, hard_reserve_bytes=1 << 20,
                               **self._kw())
        self.assertIn("CompactionLock", str(cm.exception))

    def test_zero_or_negative_reserve_refuses(self):
        with CompactionLock(self.lock_path) as lock:
            for bad in (0, -1):
                out = self._out(f"b{bad}")
                with self.assertRaises(BuildRefused) as cm:
                    build_block_strict(out, lock=lock, hard_reserve_bytes=bad, **self._kw())
                self.assertIn("hard_reserve_bytes", str(cm.exception))
                self.assertFalse(os.path.exists(out))

    def test_the_lock_is_asserted_before_the_first_write_not_only_at_publish(self):
        """Deleting the lock file must be caught before any byte lands, not after hours."""
        with CompactionLock(self.lock_path) as lock:
            os.remove(self.lock_path)
            out = self._out("b")
            with self.assertRaises(CompactionLockError):
                build_block_strict(out, lock=lock, hard_reserve_bytes=1 << 20, **self._kw())
            self.assertFalse(os.path.exists(out), "no bytes may be written without the lock")

    def test_the_fully_guarded_path_still_builds(self):
        """The mandatory path is not merely refusing everything."""
        with CompactionLock(self.lock_path) as lock:
            plan = build_block_strict(self._out("ok"), lock=lock, hard_reserve_bytes=1 << 20,
                                      **self._kw())
        self.assertEqual(plan["segment"]["day_count"], 10)


# ============================================================ finding 1: handle caching
class TestSourceHandlesAreCached(_Base):
    """Re-opening the source group per tile was roughly `days x vars x tiles` opens — about
    2.7M for a 90-day block at production geometry, which would put compaction back into the
    hours it exists to avoid."""

    def test_open_count_is_per_source_not_per_tile(self):
        delta = self._delta(self.span[:6])
        plan = build_block(self._out("b0"), start_day=self.s0, end_day=self.e0,
                           classification_target=self.span[:6], delta_path=delta,
                           artifacts_dir=self.tmp, tile=4)
        opens = plan["source_group_opens"]
        self.assertEqual(opens, 1, f"one delta source must be opened once, got {opens}")

    def test_open_count_does_not_grow_when_the_tile_shrinks(self):
        """The regression this closes: opens scaled with the tile count."""
        counts = {}
        for tile in (4, 64):
            delta = self._delta(self.span[:6], name=f"d{tile}.zarr")
            plan = build_block(self._out(f"b{tile}"), start_day=self.s0, end_day=self.e0,
                               classification_target=self.span[:6], delta_path=delta,
                               artifacts_dir=self.tmp, tile=tile)
            counts[tile] = plan["source_group_opens"]
        self.assertEqual(counts[4], counts[64],
                         f"opens must not depend on tile size, got {counts}")

    def test_opens_at_production_geometry_stay_bounded(self):
        """The number the review cited: ~2.7M opens for one 90-day fold.

        A wall-clock ratio gate was tried here first and removed. At fixture scale (32x32,
        64 tiles vs 1) tile=4 is ~33x slower than tile=64 even with handles cached, because
        per-slice overhead dominates — so a time ratio cannot attribute a regression to opens
        and would have failed on correct code. The open count is exact, causal, and is what
        actually turns into hours. It is measured on the real builder and projected here."""
        delta = self._delta(self.span[:6])
        plan = build_block(self._out("b0"), start_day=self.s0, end_day=self.e0,
                           classification_target=self.span[:6], delta_path=delta,
                           artifacts_dir=self.tmp, tile=4)
        ny = nx = 32
        tiles = ((ny + 3) // 4) * ((nx + 3) // 4)
        self.assertEqual(tiles, 64)                    # the fixture really is multi-tile
        per_tile_regression = 6 * 3 * tiles            # days x vars x tiles, the old behaviour
        self.assertLess(plan["source_group_opens"], per_tile_regression / 100)

        # production geometry, one 90-day fold from a single delta source
        prod_tiles = ((17999 + 255) // 256) * ((36000 + 255) // 256)
        self.assertGreater(90 * 3 * prod_tiles, 2_000_000)   # what it WAS
        self.assertEqual(plan["source_group_opens"], 1)      # what it is: O(sources)

    def test_mixed_sources_open_once_each(self):
        delta = self._delta(self.span[3:6])
        daily = self._daily(self.span[:3])
        plan = build_block(self._out("b0"), start_day=self.s0, end_day=self.e0,
                           classification_target=self.span[:6], delta_path=delta,
                           daily_root=daily, artifacts_dir=self.tmp, tile=4)
        # one delta group + one group per daily day
        self.assertEqual(plan["source_group_opens"], 4)


# ============================================================ finding 3: source inspection
class TestSourceContractIsInspected(_Base):
    """An OUTPUT inspection proves the output is structurally valid. It says nothing about
    whether the INPUT was sound — a corrupt source is copied faithfully into a block that then
    inspects perfectly clean."""

    def _corrupt(self, path, fn):
        g = zarr.open_group(path, mode="a")
        fn(g)

    def test_a_delta_with_a_descending_axis_refuses(self):
        delta = self._delta(self.span[:6])
        g = zarr.open_group(delta, mode="a")
        nx = g["lon"].shape[0]
        g["lon"][:] = np.linspace(131.0, 100.0, nx, dtype=np.float32)
        out = self._out("b0")
        with self.assertRaises(BuildRefused) as cm:
            build_block(out, start_day=self.s0, end_day=self.e0,
                        classification_target=self.span[:6], delta_path=delta,
                        artifacts_dir=self.tmp)
        self.assertIn("source store violates its contract", str(cm.exception))
        self.assertFalse(os.path.exists(out))

    def test_a_daily_day_with_a_wrong_dtype_refuses(self):
        daily = self._daily(self.span[:3])
        y, m, d = self.span[0].split("-")
        dpath = os.path.join(daily, y, m, d)
        g = zarr.open_group(dpath, mode="a")
        ny, nx = g["sst"].shape[1:]
        del g["sst"]
        g.create_array("sst", shape=(1, ny, nx), dtype="float64")
        with self.assertRaises(BuildRefused) as cm:
            build_block(self._out("b0"), start_day=self.s0, end_day=self.e0,
                        classification_target=self.span[:3], daily_root=daily,
                        artifacts_dir=self.tmp)
        self.assertIn("float32", str(cm.exception))

    def test_a_daily_day_with_a_non_finite_axis_refuses(self):
        daily = self._daily(self.span[:3])
        y, m, d = self.span[0].split("-")
        g = zarr.open_group(os.path.join(daily, y, m, d), mode="a")
        lat = np.asarray(g["lat"][:])
        lat[0] = np.nan
        g["lat"][:] = lat
        with self.assertRaises(BuildRefused) as cm:
            build_block(self._out("b0"), start_day=self.s0, end_day=self.e0,
                        classification_target=self.span[:3], daily_root=daily,
                        artifacts_dir=self.tmp)
        self.assertIn("non-finite", str(cm.exception))

    def test_sources_on_different_grids_refuse(self):
        """A block has ONE axis pair. Two grids would interleave geographies day by day and
        the output guard, which sees only one axis, would pass it."""
        delta = self._delta(self.span[3:6])
        daily = self._daily(self.span[:3], ny=8, nx=9)
        with self.assertRaises(BuildRefused) as cm:
            build_block(self._out("b0"), start_day=self.s0, end_day=self.e0,
                        classification_target=self.span[:6], delta_path=delta,
                        daily_root=daily, artifacts_dir=self.tmp)
        self.assertIn("disagree on the grid", str(cm.exception))

    def test_inspect_daily_day_accepts_a_sound_day(self):
        daily = self._daily(self.span[:2])
        meta = inspect_daily_day(daily, self.span[0])
        self.assertIn("sst", meta["vars"])
        self.assertGreater(meta["ny"], 0)


# ============================================================ finding 4: netcdf undelivered
class TestNetcdfIsNotAdvertised(_Base):
    def test_source_order_does_not_list_an_unimplemented_kind(self):
        """`netcdf` was listed in `SOURCE_ORDER` with no resolver. Advertising a kind that can
        never resolve makes a missing FEATURE look like a missing DAY."""
        from ingest.build_block import SOURCE_ORDER
        self.assertNotIn("netcdf", SOURCE_ORDER)
        self.assertEqual(SOURCE_ORDER, ("delta", "block", "daily", "hold"))

    def test_a_day_only_netcdf_could_supply_refuses_loudly(self):
        delta = self._delta(self.span[:3])
        with self.assertRaises(BuildRefused) as cm:
            build_block(self._out("b0"), start_day=self.s0, end_day=self.e0,
                        classification_target=self.span[:4], delta_path=delta,
                        artifacts_dir=self.tmp)
        self.assertIn(self.span[3], str(cm.exception))
