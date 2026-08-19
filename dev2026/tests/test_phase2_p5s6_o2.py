"""dev2026 — P5-S6 / O2: tail carry-forward through the REAL S3 materialization path.

**LOCAL AND SYNTHETIC ONLY.** No VM24, no `GHRSST_*` store, no cron/PM2/systemd, no production
data. Every fixture is a `tempfile` tree.

## Why this suite builds almost no new machinery

The carry-forward contract already exists in `ingest.build_block`: `predecessor_path`,
`predecessor_present`, `resolve_source_map()` with authority order delta → block → daily → hold,
`rebuild_source_set = predecessor_present ∪ newly_present`, tiled fill, and a refusal for any
day whose source cannot be resolved *before* a byte is written.

So O2 is **exercising and measuring that path**, not writing a second source-map or provenance
truth beside it. Where these tests find a gap, the gap is reported — they do not paper over it
with a parallel implementation.

## The oracle discipline

The successor's carried-forward days are compared against the **predecessor block's own bytes**,
read back from the published store. Comparing a successor to a freshly generated fixture would
compare two datasets, not a copy to its source.
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import p5_fixtures as fx  # noqa: E402
from ingest.build_block import (  # noqa: E402
    BuildRefused, build_block, resolve_source_map,
)
from store import block_manifest as bm  # noqa: E402

ANCHOR = "2026-06-27"
S = 90
LON, LAT = 100.0, 0.0


class _O2Base(unittest.TestCase):
    """A published predecessor block, plus a delta holding newly aged days."""

    NY = NX = 24

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p5s6-o2-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "manifest_root")
        self.artifacts = os.path.join(self.tmp, "artifacts")
        for d in (self.root, self.artifacts):
            os.makedirs(d, exist_ok=True)
        self.s0, self.e0 = bm.block_bounds(ANCHOR, S, 0)
        self.span = fx.calendar_span(self.s0, self.e0)
        self.pred_days = self.span[:20]          # already folded into the predecessor
        self.new_days = self.span[20:30]         # newly aged out of the protected window

        # TWO delta generations, because that is the real lifecycle. `delta_v1` holds the days
        # the predecessor was folded from; once that block is published those days are pruned
        # from the delta, leaving `delta_v2` -- the live delta the successor build sees. Days
        # that survive only in the predecessor are exactly the carry-forward days. A fixture
        # whose delta still held them would resolve them to delta (correctly!) and would never
        # exercise carry-forward at all.
        self.delta_v1 = os.path.join(self.tmp, "delta_v1.zarr")
        fx.build_delta(self.delta_v1, self.pred_days, ny=self.NY, nx=self.NX)
        self.delta = os.path.join(self.tmp, "delta_v2.zarr")
        fx.build_delta(self.delta, self.new_days, ny=self.NY, nx=self.NX, seed=1500)

        self.pred_path = os.path.join(self.root, "block_v1.zarr")
        self.pred_plan = build_block(
            self.pred_path, start_day=self.s0, end_day=self.e0,
            classification_target=list(self.pred_days), delta_path=self.delta_v1,
            artifacts_dir=self.artifacts, lock=None, hard_reserve_bytes=0,
            unsafe_skip_isolation=True)
        self._publish(self.pred_plan["segment"])

    def _publish(self, segment, generation=1):
        m = {"format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION,
             "generation": generation, "generation_id": f"gen{generation:06d}-o2",
             "created_utc": "2027-01-01T00:00:00Z", "created_by": "p5s6-o2-tests",
             "predecessor_generation": generation - 1 if generation > 1 else None,
             "predecessor_manifest": bm.archive_name(generation - 1) if generation > 1 else None,
             "grid": {"ny": self.NY, "nx": self.NX, "region": [0, self.NY, 0, self.NX]},
             "variables": list(fx.VARS),
             "block_grid": {"anchor_day": ANCHOR, "block_days": S},
             "segments": [dict(segment)], "superseded": [], "manifest_checksum": ""}
        m["manifest_checksum"] = bm.compute_checksum(m)
        return bm.publish(self.root, m)

    def _successor(self, *, name="block_v2.zarr", classification=None,
                   confirmed_missing=(), delta_path=None, predecessor_path=None,
                   predecessor_present=None, artifacts=True):
        return build_block(
            os.path.join(self.root, name), start_day=self.s0, end_day=self.e0,
            classification_target=list(self.new_days if classification is None
                                       else classification),
            predecessor_present=list(self.pred_days if predecessor_present is None
                                     else predecessor_present),
            # `False` means "explicitly no predecessor", distinct from "not overridden".
            predecessor_path=(self.pred_path if predecessor_path is None
                              else (predecessor_path or None)),
            delta_path=self.delta if delta_path is None else delta_path,
            confirmed_missing=list(confirmed_missing),
            artifacts_dir=self.artifacts if artifacts else None,
            lock=None, hard_reserve_bytes=0, unsafe_skip_isolation=True)

    # ---- oracle helpers -------------------------------------------------
    def _cell(self, path, day, var="sst"):
        g = zarr.open_group(path, mode="r")
        idx = list(g.attrs["days"]).index(day)
        return np.asarray(g[var][idx])

    def _readmit_to_delta(self, day, value):
        """A late correction: a day already folded into the predecessor is re-ingested into the
        live delta with new values. Rebuilds `delta_v2` so the day is genuinely in it."""
        days = sorted(set(self.new_days) | {day})
        shutil.rmtree(self.delta, ignore_errors=True)
        fx.build_delta(self.delta, days, ny=self.NY, nx=self.NX, seed=1500)
        g = zarr.open_group(self.delta, mode="a")
        idx = list(g.attrs["days"]).index(day)
        g["sst"][idx] = np.full(np.asarray(g["sst"][idx]).shape, value, dtype="float32")
        return day


class TestOrdinaryCarryForward(_O2Base):

    def test_the_successor_physically_contains_every_declared_present_day(self):
        plan = self._successor()
        seg = plan["segment"]
        g = zarr.open_group(plan["out_path"], mode="r")
        on_disk = list(g.attrs["days"])
        expected = sorted(set(self.pred_days) | set(self.new_days))
        self.assertEqual(on_disk, expected)
        self.assertEqual(seg["day_count"], len(expected))

    def test_carried_forward_days_come_from_the_PREDECESSOR_in_the_source_map(self):
        plan = self._successor()
        smap = plan["source_map"]
        for day in self.pred_days:
            with self.subTest(day):
                self.assertEqual(smap[day]["source_kind"], "block")
                self.assertEqual(os.path.realpath(smap[day]["source_path"]),
                                 os.path.realpath(self.pred_path))

    def test_newly_aged_days_come_from_DELTA(self):
        plan = self._successor()
        for day in self.new_days:
            with self.subTest(day):
                self.assertEqual(plan["source_map"][day]["source_kind"], "delta")

    def test_carried_forward_BYTES_match_the_predecessor_block(self):
        """Parity is anchored twice, because the predecessor is itself built by the code under
        test: a systematic day-index fault would corrupt block and oracle identically and the
        comparison would pass for the wrong reason. So this also pins the predecessor to
        `delta_v1` -- bytes the build code did not produce -- and proves the days are mutually
        distinguishable, without which no index test can detect anything."""
        plan = self._successor()
        probes = (self.pred_days[0], self.pred_days[len(self.pred_days) // 2],
                  self.pred_days[-1])

        for day in probes:                       # (1) the predecessor is a faithful copy...
            with self.subTest(anchor=day):
                np.testing.assert_array_equal(self._cell(self.pred_path, day),
                                              self._cell(self.delta_v1, day))
        for day in probes:                       # (2) ...and the successor carried it forward
            with self.subTest(carried=day):
                np.testing.assert_array_equal(self._cell(plan["out_path"], day),
                                              self._cell(self.pred_path, day))

        first, last = self._cell(plan["out_path"], probes[0]), self._cell(plan["out_path"],
                                                                         probes[-1])
        self.assertFalse(np.array_equal(first, last),
                         "fixture days are indistinguishable; parity here proves nothing")

    def test_the_predecessor_is_a_REAL_published_block(self):
        live = bm.load_live(self.root)
        self.assertEqual(live["segments"][0]["path"], os.path.basename(self.pred_path))
        self.assertTrue(os.path.isdir(self.pred_path))


class TestLateCorrectionOverridesPredecessor(_O2Base):

    def test_a_day_in_BOTH_tiers_resolves_to_delta(self):
        day = self._readmit_to_delta(self.pred_days[3], -777.0)
        plan = self._successor(classification=list(self.new_days) + [day])
        self.assertEqual(plan["source_map"][day]["source_kind"], "delta")

    def test_the_corrected_VALUE_lands_in_the_successor(self):
        day = self._readmit_to_delta(self.pred_days[3], -777.0)
        plan = self._successor(classification=list(self.new_days) + [day])
        got = self._cell(plan["out_path"], day)
        self.assertTrue(np.allclose(got, -777.0),
                        "the successor kept the predecessor's stale copy")
        self.assertFalse(np.allclose(self._cell(self.pred_path, day), -777.0),
                         "the predecessor must still hold the OLD value, or this proves nothing")


class TestConfirmedMissingAndBackfill(_O2Base):

    def test_a_confirmed_missing_day_is_recorded_as_a_gap_and_is_NOT_present(self):
        missing = self.new_days[2]
        plan = self._successor(
            classification=[d for d in self.new_days if d != missing],
            confirmed_missing=[missing])
        self.assertIn(missing, plan["segment"]["gaps"])
        g = zarr.open_group(plan["out_path"], mode="r")
        self.assertNotIn(missing, list(g.attrs["days"]))
        self.assertNotIn(missing, plan["source_map"])

    def test_the_same_day_BACKFILLED_later_becomes_present(self):
        missing = self.new_days[2]
        self._successor(name="block_gap.zarr",
                        classification=[d for d in self.new_days if d != missing],
                        confirmed_missing=[missing])
        later = self._successor(name="block_backfilled.zarr")     # no longer confirmed missing
        self.assertNotIn(missing, later["segment"]["gaps"])
        g = zarr.open_group(later["out_path"], mode="r")
        self.assertIn(missing, list(g.attrs["days"]))
        self.assertEqual(later["source_map"][missing]["source_kind"], "delta")


class TestSourceMapCompleteness(_O2Base):

    def test_every_present_day_has_a_source_map_entry(self):
        plan = self._successor()
        g = zarr.open_group(plan["out_path"], mode="r")
        for day in list(g.attrs["days"]):
            with self.subTest(day):
                self.assertIn(day, plan["source_map"])
                rec = plan["source_map"][day]
                for field in ("source_kind", "source_path", "source_day_index"):
                    self.assertIn(field, rec)

    def test_the_source_map_digest_is_recorded_in_the_segment(self):
        plan = self._successor()
        prov = plan["segment"]["build_provenance"]
        self.assertTrue(prov["source_map_digest"])
        self.assertEqual(sorted(prov["sources"]), sorted(plan["source_map"]))


class TestRefusalsBeforeAnyByteIsWritten(_O2Base):

    def _assert_nothing_written(self, path):
        self.assertFalse(os.path.exists(path), f"{path} was created despite the refusal")

    def test_a_missing_source_is_refused(self):
        """The carried days are pruned from the delta and no predecessor is supplied, so they
        have nowhere to come from. This must refuse, not silently emit a short block."""
        out = os.path.join(self.root, "block_nosrc.zarr")
        with self.assertRaises(BuildRefused) as cm:
            self._successor(name="block_nosrc.zarr", predecessor_path=False)
        self.assertIn("no source", str(cm.exception).lower())
        self._assert_nothing_written(out)

    def test_a_wrong_predecessor_PATH_is_refused(self):
        out = os.path.join(self.root, "block_wrongpath.zarr")
        with self.assertRaises(BuildRefused):
            self._successor(name="block_wrongpath.zarr",
                            predecessor_path=os.path.join(self.tmp, "does_not_exist.zarr"))
        self._assert_nothing_written(out)

    def test_a_day_outside_the_block_window_is_refused(self):
        out = os.path.join(self.root, "block_stray.zarr")
        stray = bm.add_days(self.e0, 5)
        with self.assertRaises(BuildRefused) as cm:
            self._successor(name="block_stray.zarr",
                            classification=list(self.new_days) + [stray])
        self.assertIn("outside the block window", str(cm.exception))
        self._assert_nothing_written(out)

    def test_the_source_day_INDEX_is_read_from_the_source_not_assumed(self):
        """`resolve_source_map` records the day's physical index in its own store; a successor
        that assumed positional alignment would read the wrong slab."""
        smap = resolve_source_map(list(self.pred_days), delta_path=None,
                                  predecessor_path=self.pred_path)
        g = zarr.open_group(self.pred_path, mode="r")
        actual = {d: i for i, d in enumerate(g.attrs["days"])}
        for day in self.pred_days:
            with self.subTest(day):
                self.assertEqual(smap[day]["source_day_index"], actual[day])


class TestTiledReadsNoFullSlab(_O2Base):

    def test_the_source_group_is_opened_a_bounded_number_of_times(self):
        """O(sources), not O(days): the S3 handle cache is what makes a 90-day fold viable."""
        plan = self._successor()
        self.assertLessEqual(plan["source_group_opens"], 4,
                             "the source group was reopened per day")

    def test_the_successor_is_chunked_not_one_slab(self):
        plan = self._successor()
        g = zarr.open_group(plan["out_path"], mode="r")
        chunks = g["sst"].chunks
        shape = g["sst"].shape
        self.assertLess(chunks[1], shape[1], "spatial dimension is not chunked")
        self.assertLess(chunks[2], shape[2], "spatial dimension is not chunked")


if __name__ == "__main__":
    unittest.main()
