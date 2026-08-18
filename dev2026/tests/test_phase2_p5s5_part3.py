"""dev2026 — P5-S5 Part 3: is the `(e2)` cross-tier combination reachable? (§7.5, §7.9)

`TieredCube.snapshot()` guarantees one stable snapshot **per tier**, not one instant across
both (P5-S2, risk R1). Its own docstring names the single pair that would lose data — a
**pre-publish base with a post-prune delta** — and claims it cannot reach a reader, because
§7.5 orders publish → verify → prune and the prune's path switch runs under request
quiescence.

That claim is what this suite tests, through the real `publish_manifest`, `prune_delta` and
`execute_swap_plan` paths. Nothing here is proven by a `sleep`: every interleaving is forced
with `threading.Event`, so a failure is a failure and not a slow machine.

## The four states

| base | delta | verdict |
|---|---|---|
| old | old | safe — delta serves the day |
| new | old | safe — delta-wins, and delta is the corrected copy |
| new | pruned | safe — base serves the folded day |
| **old** | **pruned** | **the only dangerous combination** |

The danger has two distinct shapes here, and both are tested, because they fail differently:

- a day folded into base for the FIRST time is in neither tier → the row **vanishes**;
- a day that was **corrected** in delta is still in the old base, at its **stale value** → the
  read succeeds and is silently wrong, which is the worse of the two.

## What the reproduction shows

The dangerous pair is reachable — `TieredCube.refresh()` reads base, then delta, and those two
reads are not atomic with respect to the filesystem. If a swap lands between them the reader
ends up holding exactly the losing pair. `test_the_dangerous_pair_IS_reachable_...` forces that
window and asserts both failure shapes, so the protocol is never described as closing a
hypothetical.

What makes it unreachable in production is that the process is **stopped** across the swap, so
no refresh is in flight to be caught mid-way. That makes quiescence load-bearing, which is why
§7.9 makes it required and makes it prove itself: a hook that merely does not raise cannot
distinguish a drained process from one it never looked at.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import subprocess
import tempfile
import re
import threading
import unittest
from unittest import mock

import numpy as np
import zarr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import p5_fixtures as fx  # noqa: E402
from ingest import corrected_day as cd  # noqa: E402
from ingest.build_block import build_block  # noqa: E402
from ingest.prune_delta import prune_delta  # noqa: E402
from ingest import swap_delta as sd  # noqa: E402
from ops import quiescence as qs  # noqa: E402
from ingest.swap_delta import execute_swap_plan  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store import repair_wal as rw  # noqa: E402
from store.segmented_cube import SegmentedCubeStore  # noqa: E402
from store.tiered_cube import TieredCube  # noqa: E402
from store.time_cube import TimeCubeStore  # noqa: E402

ANCHOR = "2026-06-27"
S = 90
LON, LAT = 100.0, 0.0


def _attested(**kw):
    """A quiescence attestation for the cases that are not about the attestation itself."""
    return {"drained": True, "evidence": "harness: reader threads joined",
            "observed_inflight": 0, "attestation_id": "att-fixed-for-tests", **kw}


def _counted(alive):
    """What a real hook returns: the count it measured, whatever it turns out to be."""
    return {"drained": alive == 0, "evidence": "harness: enumerated reader threads",
            "observed_inflight": alive, "attestation_id": "att-counted"}


class _PausableBase(SegmentedCubeStore):
    """A base tier that can be stopped INSIDE `TieredCube.refresh()`.

    `refresh()` refreshes base and then delta. Both are real, ordinary calls; the only thing
    this subclass adds is a place for the test to hold the thread between them. The window it
    exposes is the code's, not the test's — remove the pause and the same two reads still happen
    in the same order, just too quickly to land a swap in between by hand."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.paused_at_refresh = threading.Event()      # set when base is done, delta not started
        self.resume = threading.Event()                 # test opens the gate
        self.arm = False

    def refresh(self):
        super().refresh()
        if self.arm:
            self.arm = False
            self.paused_at_refresh.set()
            self.resume.wait(timeout=30)


class _Base(unittest.TestCase):
    """Generation 1 base + a delta reached through a SYMLINK, which is how the swap is done in
    production (mode s1: atomic retarget). Two days are set up to be lost in different ways.
    """

    #: subclasses set this to use a real separate anchor root threaded through every call,
    #: instead of the `unsafe_allow_colocated_anchor` waiver.
    EXTERNAL_ANCHOR = False

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "manifest_root")
        self.wal = os.path.join(self.tmp, "wal")
        self.artifacts = os.path.join(self.tmp, "artifacts")
        self.hold = os.path.join(self.tmp, "hold")
        for d in (self.root, self.wal, self.artifacts, self.hold):
            os.makedirs(d, exist_ok=True)
        self.lock = os.path.join(self.tmp, "ingest.lock")
        self.clock = os.path.join(self.tmp, "p5_compaction.lock")

        self.s0, self.e0 = bm.block_bounds(ANCHOR, S, 0)
        self.days = fx.calendar_span(self.s0, self.e0)[:6]
        self.repaired = self.days[0]        # corrected in delta; STALE in the gen-1 base
        self.folded = self.days[2]          # absent from the gen-1 base entirely
        # The newest days stay: dropping inside the active recent spatial window is refused by
        # policy (P4), and `days[-1]` is never a prune candidate.
        self.keep_days = [d for d in self.days if d not in (self.repaired, self.folded)]
        self.keep = self.keep_days[0]

        # the live delta is a symlink, exactly as on the serving host
        self.delta_v1 = os.path.join(self.tmp, "delta_v1.zarr")
        fx.build_delta(self.delta_v1, self.days)
        self.live = os.path.join(self.tmp, "delta_live.zarr")
        os.symlink(self.delta_v1, self.live)

        # generation 1 folds only the first two days: `folded` is delta-only at this point
        v1 = build_block(os.path.join(self.root, "b_v1.zarr"),
                         start_day=self.s0, end_day=self.e0,
                         classification_target=[d for d in self.days if d != self.folded],
                         delta_path=self.live, artifacts_dir=self.artifacts, lock=None,
                         hard_reserve_bytes=0, unsafe_skip_isolation=True)
        self.v1_id = v1["segment"]["segment_id"]
        self._publish(v1["segment"], generation=1)
        if self.EXTERNAL_ANCHOR:
            # A real anchor root, declared, and threaded through plan and swap. It is on the
            # same filesystem as the WAL -- unavoidable under `mkdtemp` -- so the declaration
            # waives THAT check explicitly. A separate rollback DOMAIN remains a deployment
            # prerequisite; see the results doc. What this proves is the wiring: no
            # `unsafe_allow_colocated_anchor` anywhere in this class's path.
            self.anchor = os.path.join(self.tmp, "anchor_domain")
            os.makedirs(self.anchor, exist_ok=True)
            rw.declare_anchor_domain(self.anchor, domain_id="p5s5p3-runbook-domain",
                                     at_utc="t", operator="tests",
                                     allow_same_filesystem=True,
                                     note="unit-test domain; NOT a real rollback domain")
        else:
            self.anchor = None
        rw.initialize_wal(self.wal, manifest_root=self.root, at_utc="t", operator="ops",
                          **self._anchor_kw())

        self.stale_value = self._base_value(self.repaired)      # what gen 1 serves for it

    def _anchor_kw(self):
        """The external anchor when there is one; the named waiver when there is not."""
        if self.EXTERNAL_ANCHOR:
            return {"anchor_root": self.anchor}
        return {"unsafe_allow_colocated_anchor": True}

    # ---- fixture helpers -------------------------------------------------
    def _publish(self, segment, *, generation):
        m = {"format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION,
             "generation": generation, "generation_id": f"gen{generation:06d}-x",
             "created_utc": "2027-01-01T00:00:00Z", "created_by": "p5s5p3-tests",
             "predecessor_generation": generation - 1 if generation > 1 else None,
             "predecessor_manifest": (bm.archive_name(generation - 1)
                                      if generation > 1 else None),
             "grid": {"ny": 32, "nx": 32, "region": [0, 32, 0, 32]},
             "variables": list(fx.VARS),
             "block_grid": {"anchor_day": ANCHOR, "block_days": S},
             "segments": [dict(segment)], "superseded": [], "manifest_checksum": ""}
        m["manifest_checksum"] = bm.compute_checksum(m)
        bm.publish(self.root, m)

    def _base_value(self, day):
        rows = SegmentedCubeStore(self.root).point_series(LON, LAT, [day], ["sst"])
        return rows[0]["sst"] if rows else None

    def _repair(self):
        """Correct the day in delta and commit it to the WAL, as a repair_overwrite would."""
        rec = rw.open_repair(self.wal, day=self.repaired, at_utc="t", operator="ops",
                             payload={"expected_vars": list(fx.VARS)}, **self._anchor_kw())
        idx = list(bm.inspect_store_contract(self.live).days).index(self.repaired)
        g = zarr.open_group(self.live, mode="a")
        g["sst"][idx] = np.asarray(g["sst"][idx]) + 21.0
        fp, _v, _t = cd.fingerprint_day(self.live, self.repaired, physical_index=idx)
        rw.append(self.wal, record=rw.COMMITTED, repair_id=rec["repair_id"],
                  day=self.repaired, at_utc="t", operator="ops",
                  payload={"fingerprint": fp}, **self._anchor_kw())
        self.corrected_value = self._delta_value(self.repaired)
        return rec["repair_id"]

    def _delta_value(self, day):
        rows = TimeCubeStore(self.live).point_series(LON, LAT, [day], ["sst"])
        return rows[0]["sst"] if rows else None

    def _refold(self):
        """Generation 2, through the real §7.8 orchestration: carries the correction AND folds
        the delta-only day."""
        return cd.corrective_refold(
            day=self.repaired, manifest_root=self.root, delta_path=self.live,
            wal_root=self.wal, **self._anchor_kw(),
            new_block_path=os.path.join(self.root, "b_v2.zarr"),
            artifacts_dir=self.artifacts, start_day=self.s0, end_day=self.e0,
            classification_target=list(self.days), predecessor_segment_id=self.v1_id,
            ingest_lock_path=self.lock, compaction_lock_path=self.clock,
            hard_reserve_bytes=1 << 20, operator="ops")

    _stage_n = 0

    def _plan(self):
        type(self)._stage_n += 1
        staging = os.path.join(self.tmp, f"staging{type(self)._stage_n}.zarr")
        return prune_delta(self.live, staging, list(self.keep_days),
                           base_days=self.days, spatial_window_days=1,
                           wal_root=self.wal, manifest_root=self.root, **self._anchor_kw())

    def _swap(self, plan, **kw):
        kw.setdefault("pre_swap_quiesce_fn", _attested)
        return execute_swap_plan(plan, mode="s1", hold_dir=self.hold,
                                 wal_root=self.wal, manifest_root=self.root,
                                 compaction_lock_path=self.clock,
                                 **self._anchor_kw(), **kw)

    def _cube(self, base=None):
        return TieredCube(base or SegmentedCubeStore(self.root), TimeCubeStore(self.live))

    def _row(self, snap, day):
        rows = snap.point_series(LON, LAT, [day], ["sst"])
        return rows[0] if rows else None


class TestTheFourStates(_Base):
    """Each cell of the matrix, reached through the real publish/prune/swap paths."""

    def test_1_old_base_old_delta_is_safe(self):
        self._repair()
        snap = self._cube().snapshot()
        self.assertEqual(snap.day_source(self.repaired), "delta")
        self.assertEqual(snap.day_source(self.folded), "delta")
        self.assertAlmostEqual(self._row(snap, self.repaired)["sst"], self.corrected_value, 4)

    def test_2_new_base_old_delta_is_safe_and_delta_wins(self):
        self._repair()
        cube = self._cube()
        self._refold()                      # publishes generation 2
        cube.base.refresh()                 # base advances; delta has not been pruned yet
        snap = cube.snapshot()
        self.assertEqual(snap.day_source(self.repaired), "delta")
        self.assertAlmostEqual(self._row(snap, self.repaired)["sst"], self.corrected_value, 4)

    def test_3_new_base_pruned_delta_is_safe(self):
        self._repair()
        self._refold()
        swap = self._swap(self._plan())
        self.assertEqual(swap["status"], "swapped", swap.get("reason"))
        snap = self._cube().snapshot()      # a fresh process, as after "pm2 start"
        self.assertEqual(snap.day_source(self.repaired), "base")
        self.assertEqual(snap.day_source(self.folded), "base")
        self.assertAlmostEqual(self._row(snap, self.repaired)["sst"], self.corrected_value, 4)

    def test_4_old_base_pruned_delta_loses_data_in_BOTH_shapes(self):
        """The combination the whole protocol exists to prevent — constructed here directly, so
        the cost is measured rather than asserted."""
        self._repair()
        stale_cube = self._cube()           # holds generation 1
        self._refold()
        self.assertEqual(self._swap(self._plan())["status"], "swapped")
        stale_cube.delta.refresh()          # delta advances alone: the losing pair
        snap = stale_cube.snapshot()

        # shape 1: the corrected day reads successfully, at the value it was corrected AWAY from
        row = self._row(snap, self.repaired)
        self.assertIsNotNone(row, "the stale base still answers — that is what makes it silent")
        self.assertAlmostEqual(row["sst"], self.stale_value, 4)
        self.assertNotAlmostEqual(row["sst"], self.corrected_value, 4)

        # shape 2: the newly folded day is in neither tier and disappears from the response
        self.assertIsNone(snap.day_source(self.folded))
        self.assertIsNone(self._row(snap, self.folded))


class TestReachability(_Base):
    """Can a request holding the old base survive into the post-prune world?"""

    def test_after_publish_before_prune_an_in_flight_reader_is_unaffected(self):
        """Reviewer point 1. Publication alone changes nothing for a reader already holding a
        snapshot: it keeps its own base metadata and the delta is still whole."""
        self._repair()
        snap = self._cube().snapshot()      # captured BEFORE the publication
        self._refold()
        self.assertEqual(snap.day_source(self.repaired), "delta")
        self.assertAlmostEqual(self._row(snap, self.repaired)["sst"], self.corrected_value, 4)
        self.assertEqual(snap.day_source(self.folded), "delta")

    def test_the_dangerous_pair_IS_reachable_when_a_refresh_spans_the_swap(self):
        """The reproduction. `TieredCube.refresh()` reads base, then delta; the two reads are not
        atomic against the filesystem. A swap landing between them hands the reader the losing
        pair — so the invariant genuinely rests on nothing being in flight."""
        self._repair()
        base = _PausableBase(self.root)
        cube = TieredCube(base, TimeCubeStore(self.live))
        base.arm = True
        reader = threading.Thread(target=cube.refresh, name="refresh-loop")
        reader.start()
        # Cleanups, not just the happy path: a parked thread that outlives a FAILED assertion
        # keeps reading a directory tearDown is deleting, which reports as a lurid unrelated
        # traceback instead of the assertion that actually failed. LIFO, so release then join.
        self.addCleanup(reader.join, 30)
        self.addCleanup(base.resume.set)
        self.assertTrue(base.paused_at_refresh.wait(timeout=30),
                        "the refresh never reached the base/delta boundary")

        self._refold()                                       # publish generation 2
        # the violation: swap while that refresh is parked mid-way
        self.assertEqual(self._swap(self._plan())["status"], "swapped")

        base.resume.set()
        reader.join(timeout=30)
        self.assertFalse(reader.is_alive())

        snap = cube.snapshot()
        self.assertIsNone(snap.day_source(self.folded), "the folded day should be lost here")
        self.assertAlmostEqual(self._row(snap, self.repaired)["sst"], self.stale_value, 4)

    def test_quiescence_refuses_the_swap_while_that_reader_is_still_in_flight(self):
        """Reviewer point 2. The same interleaving, with the production posture in place: the
        hook counts the in-flight reader, cannot attest a drain, and the swap refuses with the
        live delta untouched. Then the reader completes — safely, on the old pair — and the swap
        succeeds."""
        self._repair()
        base = _PausableBase(self.root)
        cube = TieredCube(base, TimeCubeStore(self.live))
        base.arm = True
        reader = threading.Thread(target=cube.refresh, name="refresh-loop")
        reader.start()
        self.addCleanup(reader.join, 30)
        self.addCleanup(base.resume.set)
        self.assertTrue(base.paused_at_refresh.wait(timeout=30))
        self._refold()

        def quiesce_counts_readers():
            return _counted(1 if reader.is_alive() else 0)

        before = sorted(bm.inspect_store_contract(self.live).days)
        refused = self._swap(self._plan(), pre_swap_quiesce_fn=quiesce_counts_readers)
        self.assertEqual(refused["status"], "quiesce_failed", refused.get("reason"))
        self.assertFalse(refused["swap_performed"])
        self.assertIn("Readers are not proven gone", refused["reason"])
        self.assertEqual(sorted(bm.inspect_store_contract(self.live).days), before,
                         "a refused swap must leave the live delta exactly as it was")

        base.resume.set()
        reader.join(timeout=30)
        snap = cube.snapshot()                       # completed on new base + OLD delta: safe
        self.assertEqual(snap.day_source(self.repaired), "delta")
        self.assertAlmostEqual(self._row(snap, self.repaired)["sst"], self.corrected_value, 4)

        done = self._swap(self._plan(), pre_swap_quiesce_fn=quiesce_counts_readers)
        self.assertEqual(done["status"], "swapped", done.get("reason"))

    def test_a_reader_that_starts_after_the_swap_sees_the_new_pair(self):
        """Reviewer point 3. The post-restart request: new base, pruned delta, correct value."""
        self._repair()
        self._refold()
        self.assertEqual(self._swap(self._plan())["status"], "swapped")
        snap = self._cube().snapshot()
        self.assertEqual(snap.day_source(self.repaired), "base")
        self.assertAlmostEqual(self._row(snap, self.repaired)["sst"], self.corrected_value, 4)

    def test_no_read_across_the_whole_protocol_ever_returns_the_losing_pair(self):
        """Reviewer point 4, as a standing assertion rather than a spot check: a reader loops
        for the whole publish → prune → swap sequence, and EVERY snapshot it takes is recorded
        and checked. The loop refreshes through the composite `refresh()` only — the single
        refresh path the serving app actually has (`api/app.py` `_refresh_loop`)."""
        self._repair()
        cube = self._cube()
        seen = []
        stop = threading.Event()
        started = threading.Event()

        def loop():
            while not stop.is_set():
                cube.refresh()
                snap = cube.snapshot()
                seen.append((snap.day_source(self.repaired),
                             self._row(snap, self.repaired),
                             snap.day_source(self.folded)))
                started.set()

        reader = threading.Thread(target=loop, name="reader")
        reader.start()
        self.addCleanup(reader.join, 30)
        self.addCleanup(stop.set)
        self.assertTrue(started.wait(timeout=30))
        self._refold()

        def quiesce_joins_the_reader():
            stop.set()
            reader.join(timeout=30)
            return _counted(1 if reader.is_alive() else 0)

        self.assertEqual(self._swap(self._plan(),
                                    pre_swap_quiesce_fn=quiesce_joins_the_reader)["status"],
                         "swapped")
        stop.set()
        reader.join(timeout=30)

        self.assertGreater(len(seen), 0)
        for src, row, folded_src in seen:
            self.assertIsNotNone(row, "a day vanished from a read taken during the protocol")
            if src == "base":
                # only legal once base carries the correction; never at the stale value
                self.assertAlmostEqual(row["sst"], self.corrected_value, 4)
            else:
                self.assertAlmostEqual(row["sst"], self.corrected_value, 4)
            self.assertIsNotNone(folded_src, "the folded day vanished mid-protocol")


class TestQuiescenceIsFailClosed(_Base):
    """§7.9: quiescence must be present AND must prove itself. "No reader was observed" is not
    "no reader exists" — and that is the assumption the (e2) analysis is not allowed to make."""

    def setUp(self):
        super().setUp()
        self._repair()
        self._refold()

    def _attempt(self, **kw):
        before = sorted(bm.inspect_store_contract(self.live).days)
        res = execute_swap_plan(self._plan(), mode="s1", hold_dir=self.hold,
                                wal_root=self.wal, manifest_root=self.root,
                                unsafe_allow_colocated_anchor=True,
                                compaction_lock_path=self.clock, **kw)
        self.assertEqual(sorted(bm.inspect_store_contract(self.live).days), before,
                         "nothing may be touched when quiescence is not proven")
        self.assertFalse(res.get("swap_performed"))
        return res

    def test_omitting_the_hook_entirely_is_refused(self):
        res = self._attempt()
        self.assertEqual(res["status"], "refused")
        self.assertIn("pre_swap_quiesce_fn is required", res["reason"])
        self.assertFalse(res["live_touched"])

    def test_a_hook_that_returns_nothing_is_refused(self):
        """The forgotten `return` — and the old behaviour, where not raising meant success."""
        res = self._attempt(pre_swap_quiesce_fn=lambda: None)
        self.assertEqual(res["status"], "quiesce_failed")
        self.assertIn("no attestation", res["reason"])

    def test_a_hook_that_reports_not_drained_is_refused(self):
        res = self._attempt(pre_swap_quiesce_fn=lambda: {"drained": False, "evidence": "x",
                                                         "observed_inflight": 0,
                                                         "attestation_id": "a"})
        self.assertEqual(res["status"], "quiesce_failed")
        self.assertIn("drained=False", res["reason"])

    def test_a_truthy_string_does_not_launder_into_a_drain(self):
        """`bool("false")` is True — the shape that made an unset shell variable grant a waiver
        in Part 2 round 7. `drained` must be exactly `True`."""
        for bad in ("false", "true", 1, [1]):
            with self.subTest(repr(bad)):
                res = self._attempt(pre_swap_quiesce_fn=lambda b=bad: {
                    "drained": b, "evidence": "x", "observed_inflight": 0, "attestation_id": "a"})
                self.assertEqual(res["status"], "quiesce_failed")
                self.assertIn("must be exactly True", res["reason"])

    def test_an_attestation_without_evidence_is_refused(self):
        for bad in ({"evidence": None}, {"evidence": ""}, {"evidence": "   "}, {"evidence": 7}):
            with self.subTest(repr(bad)):
                res = self._attempt(pre_swap_quiesce_fn=lambda b=bad: {
                    "drained": True, "observed_inflight": 0, "attestation_id": "a", **b})
                self.assertEqual(res["status"], "quiesce_failed")
                self.assertIn("no `evidence` string", res["reason"])

    def test_a_positive_in_flight_count_is_refused_even_when_drained_is_claimed(self):
        res = self._attempt(pre_swap_quiesce_fn=lambda: {
            "drained": True, "evidence": "pm2 stop returned 0", "observed_inflight": 2,
            "attestation_id": "a"})
        self.assertIn("2 in-flight", res["reason"])
        self.assertEqual(res["status"], "quiesce_failed")
        self.assertIn("2 in-flight", res["reason"])

    def test_observed_inflight_is_REQUIRED_not_merely_checked_when_present(self):
        """Review round 2, finding 2. `{"drained": True, "evidence": "trust me"}` was accepted:
        the one load-bearing gate taking an answer it never asked for."""
        res = self._attempt(pre_swap_quiesce_fn=lambda: {"drained": True,
                                                         "evidence": "trust me"})
        self.assertEqual(res["status"], "quiesce_failed")
        self.assertIn("missing required field(s) observed_inflight", res["reason"])

    def test_every_core_field_is_required(self):
        full = {"drained": True, "evidence": "e", "observed_inflight": 0,
                "attestation_id": "a"}
        for drop in sd.QUIESCENCE_CORE_FIELDS:
            with self.subTest(drop):
                att = {k: v for k, v in full.items() if k != drop}
                res = self._attempt(pre_swap_quiesce_fn=lambda a=att: dict(a))
                self.assertEqual(res["status"], "quiesce_failed")
                self.assertIn(f"missing required field(s) {drop}", res["reason"])

    def test_a_negative_inflight_count_is_not_fewer_than_none(self):
        res = self._attempt(pre_swap_quiesce_fn=lambda: {
            "drained": True, "evidence": "e", "observed_inflight": -1, "attestation_id": "a"})
        self.assertEqual(res["status"], "quiesce_failed")
        self.assertIn("not a measurement", res["reason"])

    def test_the_inflight_count_must_be_a_raw_int(self):
        """`isinstance(True, int)` is True, so a bool would otherwise read as a proven zero."""
        for bad in (False, True, "0", 0.0, None):
            with self.subTest(repr(bad)):
                res = self._attempt(pre_swap_quiesce_fn=lambda b=bad: {
                    "drained": True, "evidence": "e", "observed_inflight": b, "attestation_id": "a"})
                self.assertEqual(res["status"], "quiesce_failed")
                self.assertIn("must be a raw int", res["reason"])

    def test_a_hook_that_raises_still_reports_the_safe_state(self):
        def boom():
            raise RuntimeError("pm2 stop failed / port still serving")
        res = self._attempt(pre_swap_quiesce_fn=boom)
        self.assertEqual(res["status"], "quiesce_failed")
        self.assertFalse(res["live_touched"])

    def test_a_proven_drain_swaps(self):
        res = execute_swap_plan(self._plan(), mode="s1", hold_dir=self.hold,
                                wal_root=self.wal, manifest_root=self.root,
                                unsafe_allow_colocated_anchor=True,
                                compaction_lock_path=self.clock,
                                pre_swap_quiesce_fn=_attested)
        self.assertEqual(res["status"], "swapped", res.get("reason"))

    def test_the_attestation_is_RECORDED_on_a_successful_swap(self):
        """Review round 2, finding 3. `evidence` is mandatory at the gate and was then thrown
        away, so the audit trail could not answer what quiescence actually verified."""
        res = execute_swap_plan(self._plan(), mode="s1", hold_dir=self.hold,
                                wal_root=self.wal, manifest_root=self.root,
                                unsafe_allow_colocated_anchor=True,
                                compaction_lock_path=self.clock,
                                pre_swap_quiesce_fn=lambda: _attested(
                                    evidence="pm2 stop ghrsst; :8035 closed; 0 worker pids"))
        self.assertEqual(res["status"], "swapped", res.get("reason"))
        for where in (res["quiescence"], res["manifest"]["quiescence"]):
            self.assertTrue(where["attested"])
            self.assertFalse(where["waived"])
            self.assertEqual(where["observed_inflight"], 0)
            self.assertIn(":8035 closed", where["evidence"])

        with open(os.path.join(self.hold, "manifest.jsonl")) as fh:
            lines = [json.loads(x) for x in fh if x.strip()]
        swap_rec = [r for r in lines if r["op"] == "delta_prune_swap"][-1]
        self.assertIn(":8035 closed", swap_rec["quiescence"]["evidence"])

    def test_only_validated_fields_are_recorded_never_the_callback_object(self):
        """The callback's object is caller-controlled. Extra keys are acknowledged by NAME so
        the record shows what was supplied without inheriting arbitrary payload."""
        res = execute_swap_plan(self._plan(), mode="s1", hold_dir=self.hold,
                                wal_root=self.wal, manifest_root=self.root,
                                unsafe_allow_colocated_anchor=True,
                                compaction_lock_path=self.clock,
                                pre_swap_quiesce_fn=lambda: _attested(
                                    pm2_dump=object(), pids=[1, 2]))
        self.assertEqual(res["status"], "swapped", res.get("reason"))
        q = res["quiescence"]
        self.assertEqual(q["extra_fields"], ["pids", "pm2_dump"])
        self.assertNotIn("pids", q)
        self.assertNotIn("pm2_dump", q)
        json.dumps(res["manifest"])                 # the record must stay serializable

    def test_a_waived_swap_is_recorded_as_waived(self):
        res = execute_swap_plan(self._plan(), mode="s1", hold_dir=self.hold,
                                wal_root=self.wal, manifest_root=self.root,
                                unsafe_allow_colocated_anchor=True,
                                compaction_lock_path=self.clock,
                                unsafe_skip_quiescence=True)
        self.assertEqual(res["status"], "swapped", res.get("reason"))
        self.assertEqual(res["quiescence"], {"attested": False, "waived": True})

    def test_a_rollback_records_the_attestation_too(self):
        """A swap that rolls back is exactly when the question gets asked."""
        def bad_verifier(live, keep):
            return {"ok": False, "why": "forced"}
        res = execute_swap_plan(self._plan(), mode="s1", hold_dir=self.hold,
                                wal_root=self.wal, manifest_root=self.root,
                                unsafe_allow_colocated_anchor=True,
                                compaction_lock_path=self.clock,
                                pre_swap_quiesce_fn=lambda: _attested(evidence="rollback case"),
                                verifier=bad_verifier)
        self.assertEqual(res["status"], "rolled_back", res.get("reason"))
        with open(os.path.join(self.hold, "manifest.jsonl")) as fh:
            lines = [json.loads(x) for x in fh if x.strip()]
        rb = [r for r in lines if r["op"] == "swap_rollback"][-1]
        self.assertEqual(rb["quiescence"]["evidence"], "rollback case")

    def test_EVERY_post_quiesce_outcome_returns_the_attestation(self):
        """Review round 3, finding 5: the rollback manifest carried it, the rollback RESULT did
        not, while the runbook and results doc told operators to read `res["quiescence"]`."""
        def bad_verifier(live, keep):
            return {"ok": False, "why": "forced"}
        res = execute_swap_plan(self._plan(), mode="s1", hold_dir=self.hold,
                                wal_root=self.wal, manifest_root=self.root,
                                unsafe_allow_colocated_anchor=True,
                                compaction_lock_path=self.clock,
                                pre_swap_quiesce_fn=lambda: _attested(
                                    attestation_id="att-rollback"),
                                verifier=bad_verifier)
        self.assertEqual(res["status"], "rolled_back", res.get("reason"))
        self.assertEqual(res["quiescence"]["attestation_id"], "att-rollback")

    def test_a_REFUSED_attempt_still_names_the_id_it_tried(self):
        """A refused attempt already wrote an ops-side evidence file. Without the id on this
        side, matching them after a retry is guesswork by ordering."""
        res = self._attempt(pre_swap_quiesce_fn=lambda: {
            "drained": True, "evidence": "e", "observed_inflight": 3,
            "attestation_id": "att-refused"})
        self.assertEqual(res["status"], "quiesce_failed")
        self.assertEqual(res["quiescence"]["attempted_attestation_id"], "att-refused")
        with open(os.path.join(self.hold, "manifest.jsonl")) as fh:
            rec = [json.loads(x) for x in fh if x.strip()][-1]
        self.assertEqual(rec["op"], "swap_quiesce_failed")
        self.assertEqual(rec["quiescence"]["attempted_attestation_id"], "att-refused")

    def test_the_join_key_is_required_and_is_carried_to_the_record(self):
        for bad in (None, "", "   ", 7):
            with self.subTest(repr(bad)):
                res = self._attempt(pre_swap_quiesce_fn=lambda b=bad: {
                    "drained": True, "evidence": "e", "observed_inflight": 0,
                    "attestation_id": b})
                self.assertEqual(res["status"], "quiesce_failed")
                self.assertIn("attestation_id", res["reason"])

    def test_the_audit_record_is_fsynced_not_merely_written(self):
        """Review round 3, finding 3: "durable" was claimed of a buffered write. A VM that dies
        during the swap would lose exactly the evidence that the swap was the last thing to
        happen."""
        seen = []
        real_fsync, real_fstat = os.fsync, os.fstat

        def spy(fd):                      # identify the object by INODE, not by fd number
            try:
                seen.append(real_fstat(fd).st_ino)
            except OSError:
                pass
            return real_fsync(fd)

        with mock.patch("os.fsync", side_effect=spy):
            res = execute_swap_plan(self._plan(), mode="s1", hold_dir=self.hold,
                                    wal_root=self.wal, manifest_root=self.root,
                                    unsafe_allow_colocated_anchor=True,
                                    compaction_lock_path=self.clock,
                                    pre_swap_quiesce_fn=_attested)
        self.assertEqual(res["status"], "swapped", res.get("reason"))
        # The FILE, specifically. Asserting "something was fsync'd" passes on the directory
        # sync alone, which is how the first version of this test survived the mutation that
        # removed the record sync entirely.
        manifest = os.path.join(self.hold, "manifest.jsonl")
        self.assertIn(os.stat(manifest).st_ino, seen,
                      "the manifest RECORD was never fsync'd — only the directory")
        self.assertIn(os.stat(self.hold).st_ino, seen,
                      "the directory was never fsync'd on first creation")

    def test_the_waiver_has_to_be_typed(self):
        """`unsafe_skip_quiescence` exists for suites that predate §7.9. It is named so it
        cannot be reached by omission, which is how the old default behaved."""
        res = execute_swap_plan(self._plan(), mode="s1", hold_dir=self.hold,
                                wal_root=self.wal, manifest_root=self.root,
                                unsafe_allow_colocated_anchor=True,
                                compaction_lock_path=self.clock,
                                unsafe_skip_quiescence=True)
        self.assertEqual(res["status"], "swapped", res.get("reason"))


class TestQuiescenceAttestation(unittest.TestCase):
    """`ops.quiescence` — the drain measurement itself, which used to live in runbook markdown
    where nothing could test it. Review round 3, finding 2: counting PM2 `online` workers
    answers a question about PM2's bookkeeping, not about whether anyone is still reading the
    delta. A worker leaves `online` the moment it starts stopping."""

    OK = dict(app="ghrsst", attestation_id="att-1", checked_utc="2027-01-01T00:00:00+00:00")

    def test_a_pre_stop_pid_that_still_EXISTS_refuses_even_when_pm2_forgot_it(self):
        """The exact state the old check missed: PM2 no longer lists the worker as online — it
        lists nothing at all — while the OS process is still finishing its request."""
        with self.assertRaises(qs.NotQuiesced) as cm:
            qs.attest_drain(before_pids=[4242], jlist_after=[], port_open=False,
                            alive=lambda pid: True, **self.OK)
        self.assertIn("still exist after the stop", str(cm.exception))
        self.assertIn("4242", str(cm.exception))

    def test_a_stopping_worker_is_not_a_settled_stop(self):
        jl = [{"name": "ghrsst", "pid": 0, "pm2_env": {"status": "stopping"}}]
        with self.assertRaises(qs.NotQuiesced) as cm:
            qs.attest_drain(before_pids=[], jlist_after=jl, port_open=False,
                            alive=lambda pid: False, **self.OK)
        self.assertIn("stopping", str(cm.exception))

    def test_a_worker_that_came_BACK_refuses(self):
        """A fresh worker can open the delta before the path switch."""
        jl = [{"name": "ghrsst", "pid": 999, "pm2_env": {"status": "stopped"}}]
        with self.assertRaises(qs.NotQuiesced) as cm:
            qs.attest_drain(before_pids=[], jlist_after=jl, port_open=False,
                            alive=lambda pid: False, **self.OK)
        self.assertIn("999", str(cm.exception))

    def test_an_open_port_refuses(self):
        with self.assertRaises(qs.NotQuiesced) as cm:
            qs.attest_drain(before_pids=[], jlist_after=[], port_open=True,
                            alive=lambda pid: False, **self.OK)
        self.assertIn("still accepts connections", str(cm.exception))

    def test_a_missing_join_key_refuses(self):
        with self.assertRaises(qs.NotQuiesced):
            qs.attest_drain(app="ghrsst", before_pids=[], jlist_after=[], port_open=False,
                            attestation_id="", checked_utc="t", alive=lambda pid: False)

    def test_worker_pids_are_NOT_filtered_to_online(self):
        """The pids that matter most are the ones that just left that set."""
        jl = [{"name": "ghrsst", "pid": 11, "pm2_env": {"status": "online"}},
              {"name": "ghrsst", "pid": 12, "pm2_env": {"status": "stopping"}},
              {"name": "ghrsst", "pid": 13, "pm2_env": {"status": "stopped"}},
              {"name": "other", "pid": 14, "pm2_env": {"status": "online"}}]
        self.assertEqual(qs.worker_pids(jl, "ghrsst"), [11, 12, 13])

    def test_a_permission_error_counts_the_process_as_ALIVE(self):
        """EPERM means the process exists and belongs to someone else. Reading that as 'gone'
        would turn the one case we cannot inspect into a pass."""
        def denied(pid):
            raise PermissionError()
        self.assertTrue(qs.pid_alive.__wrapped__(1) if hasattr(qs.pid_alive, "__wrapped__")
                        else True)
        with mock.patch("os.kill", side_effect=PermissionError()):
            self.assertTrue(qs.pid_alive(4242))
        with mock.patch("os.kill", side_effect=ProcessLookupError()):
            self.assertFalse(qs.pid_alive(4242))

    def test_a_proven_drain_is_ACCEPTED_by_the_executor_validator(self):
        """The two halves must agree: the module builds what the executor requires."""
        att = qs.attest_drain(before_pids=[4242, 4243], jlist_after=[], port_open=False,
                              alive=lambda pid: False, **self.OK)
        self.assertIsNone(sd._quiescence_refusal(att))
        self.assertEqual(att["observed_inflight"], 0)
        self.assertIn("verified gone via kill(pid, 0)", att["evidence"])


class TestTheProductionRunbookMatchesTheContract(_Base):
    EXTERNAL_ANCHOR = True                       # review round 4, finding 2: no waiver here
    """Review round 3, finding 1: the runbook's `prune_delta` call omitted the P5 roots and its
    `execute_swap_plan` call omitted the compaction reservation, so it would have refused before
    `quiesce()` ever ran. The previous test extracted only the `quiesce()` body and could not
    see it.

    So this reads the ARGUMENT NAMES out of the runbook and calls the real functions with
    exactly that set. An argument the runbook forgets is an argument the test does not pass."""

    RUNBOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "specs", "p4s10_production_rollout_runbook.md")

    @classmethod
    def setUpClass(cls):
        with open(cls.RUNBOOK) as fh:
            cls.text = fh.read()

    def _kwargs_of(self, call):
        """The keyword names the runbook actually passes to `call`."""
        i = self.text.index(call + "(")
        depth, j = 0, i + len(call)
        while True:
            if self.text[j] == "(":
                depth += 1
            elif self.text[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        return set(re.findall(r"(?:^|[,(\s])([a-z_]+)\s*=", self.text[i:j + 1]))

    def test_the_runbook_prune_call_passes_the_roots_the_gate_requires(self):
        names = self._kwargs_of("plan = prune_delta")
        for required in ("wal_root", "manifest_root", "anchor_root"):
            with self.subTest(required):
                self.assertIn(required, names,
                              "without it the plan fails closed as soon as a day is dropped")

    def test_the_runbook_swap_call_passes_the_reservation_and_the_roots(self):
        names = self._kwargs_of("res = execute_swap_plan")
        for required in ("compaction_lock_path", "wal_root", "manifest_root", "anchor_root",
                         "pre_swap_quiesce_fn"):
            with self.subTest(required):
                self.assertIn(required, names)

    def test_the_runbooks_ARGUMENT_SET_actually_produces_a_plan_and_a_swap(self):
        """The integration check the text tests could not make: build a real plan and run a real
        swap passing ONLY the keywords the runbook passes."""
        self._repair()
        self._refold()
        pnames = self._kwargs_of("plan = prune_delta")
        # Argument NAMES come from the runbook; values are the fixture's, since production
        # window sizes mean nothing at a six-day fixture scale. What is under test is whether
        # the runbook passes enough arguments, not what it passes for the tuning knobs.
        # `anchor_root` is a REAL separate root here (EXTERNAL_ANCHOR), not the waiver: a test
        # that waives the anchor cannot show the production configuration executes.
        supply = {"wal_root": self.wal, "manifest_root": self.root, "anchor_root": self.anchor}
        plan = prune_delta(self.live, os.path.join(self.tmp, "runbook_staging.zarr"),
                           list(self.keep_days), base_days=self.days, spatial_window_days=1,
                           **{k: v for k, v in supply.items() if k in pnames})
        self.assertEqual(plan["status"], "ok", plan.get("reason"))

        snames = self._kwargs_of("res = execute_swap_plan")
        skw = {"compaction_lock_path": self.clock, "wal_root": self.wal,
               "manifest_root": self.root, "anchor_root": self.anchor,
               "pre_swap_quiesce_fn": _attested}
        res = execute_swap_plan(plan, mode="s1", hold_dir=self.hold,
                                **{k: v for k, v in skw.items() if k in snames})
        self.assertEqual(res["status"], "swapped", res.get("reason"))
        self.assertEqual(plan["anchor_root"], os.path.realpath(self.anchor))
        # nothing in this path may have leaned on the waiver
        state = rw.read_wal(self.wal, anchor_root=self.anchor)
        self.assertEqual(state.anchor["seq"], state.last_seq)
        self.assertFalse(os.path.isfile(os.path.join(self.wal, rw.ANCHOR_NAME)),
                         "a co-located sidecar would mean the external anchor was bypassed")

    def test_the_runbook_captures_pids_BEFORE_the_stop(self):
        """Capturing them afterwards measures nothing: the survivors are already unlisted."""
        body = self.text[self.text.index("def quiesce():"):
                         self.text.index("\ndef ", self.text.index("def quiesce():") + 1)]
        self.assertIn("qs.worker_pids(pm2_jlist(), APP)", body)
        self.assertIn("qs.attest_drain(", body)
        self.assertLess(body.index("qs.worker_pids"), body.index('"pm2", "stop"'),
                        "the pid capture must precede the stop")
        self.assertNotIn("; return\n", body, "a bare `return` is what the executor refuses")

    def test_the_deployment_pin_names_a_tree_that_HAS_this_code(self):
        """Review round 4, finding 1: the runbook pinned `dev2026-p4-s8-swap-design`, whose tip
        predates the WAL, the current executor contract and `ops/quiescence.py`. A worktree
        checked out from it fails on import."""
        self.assertNotIn("branch `dev2026-p4-s8-swap-design` tip", self.text,
                         "that branch cannot run this runbook")
        pin = re.search(r"git merge-base --is-ancestor ([0-9a-f]{7,40}) HEAD", self.text)
        self.assertIsNotNone(pin, "the runbook must pin a minimum ancestor commit")
        repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        exists = subprocess.run(["git", "cat-file", "-e", pin.group(1) + "^{commit}"],
                                cwd=repo, capture_output=True)
        self.assertEqual(exists.returncode, 0, f"pinned commit {pin.group(1)} does not exist")
        anc = subprocess.run(["git", "merge-base", "--is-ancestor", pin.group(1), "HEAD"],
                             cwd=repo, capture_output=True)
        self.assertEqual(anc.returncode, 0,
                         f"{pin.group(1)} is not an ancestor of HEAD — the pin is wrong")

    def test_the_capability_preflight_names_modules_that_actually_exist(self):
        """The pin cannot name the commit it lives in, so the runbook checks the code directly.
        This asserts those checks are real: every module and symbol it imports must resolve."""
        block = self.text[self.text.index("**Capability preflight"):]
        block = block[block.index("```bash") + 7:]
        block = block[:block.index("```")]
        for mod, syms in re.findall(r"from ([\w.]+) import ([^\n#]+)", block):
            with self.subTest(mod):
                imported = __import__(mod, fromlist=["_"])
                for sym in [x.strip() for x in syms.split(",") if x.strip()]:
                    self.assertTrue(hasattr(imported, sym), f"{mod}.{sym} is missing")
        self.assertIn("ops.quiescence", block)
        self.assertIn("QUIESCENCE_CORE_FIELDS", block)

    def test_the_anchor_preflight_is_a_NO_GO_not_a_warning(self):
        """Review round 4, finding 2: a shared filesystem only printed a WARNING and continued.
        The runbook's own shell is executed here, not read: same-filesystem must EXIT NON-ZERO.
        """
        block = self.text[self.text.index("**Preflight (every check must pass"):]
        block = block[block.index("```bash") + 7:]
        block = block[:block.index("```")]
        env = {**os.environ, "PY": sys.executable,
               "MANIFEST_ROOT": self.root, "WAL_ROOT": self.wal,
               "ANCHOR_ROOT": os.path.join(self.tmp, "same_fs_anchor")}
        os.makedirs(env["ANCHOR_ROOT"], exist_ok=True)      # same mkdtemp => same st_dev
        # `stat -c` is GNU; on this host use the BSD spelling so the check runs at all.
        if sys.platform == "darwin":
            block = block.replace("stat -c %d", "stat -f %d")
        res = subprocess.run(["bash", "-c", block], env=env, capture_output=True, text=True)
        self.assertNotEqual(res.returncode, 0,
                            "a co-located anchor must STOP the run, not warn and continue")
        self.assertIn("NO-GO", res.stdout + res.stderr)
        self.assertIn("independent domain", res.stdout + res.stderr)

    def test_the_runbook_does_not_hand_roll_persistence(self):
        """Finding 3: the evidence file's durability logic lived in markdown and got the
        directory fsync wrong. It must call the shared, tested writer."""
        self.assertIn("qs.write_evidence(", self.text)
        self.assertNotIn('os.fsync(fh.fileno())', self.text,
                         "durability belongs in store.durable_jsonl, not in this document")

    def test_the_runbook_delegates_to_the_TESTED_module(self):
        """If the hook is ever inlined back into markdown, this fails: unimportable logic is
        untestable logic, which is how findings 1 and 2 both got here."""
        self.assertIn("from ops import quiescence as qs", self.text)


if __name__ == "__main__":
    unittest.main()
