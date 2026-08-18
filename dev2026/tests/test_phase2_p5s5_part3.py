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
import tempfile
import threading
import unittest

import numpy as np
import zarr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import p5_fixtures as fx  # noqa: E402
from ingest import corrected_day as cd  # noqa: E402
from ingest.build_block import build_block  # noqa: E402
from ingest.prune_delta import prune_delta  # noqa: E402
from ingest import swap_delta as sd  # noqa: E402
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
            "observed_inflight": 0, **kw}


def _counted(alive):
    """What a real hook returns: the count it measured, whatever it turns out to be."""
    return {"drained": alive == 0, "evidence": "harness: enumerated reader threads",
            "observed_inflight": alive}


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
        rw.initialize_wal(self.wal, manifest_root=self.root, at_utc="t", operator="ops",
                          unsafe_allow_colocated_anchor=True)

        self.stale_value = self._base_value(self.repaired)      # what gen 1 serves for it

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
                             payload={"expected_vars": list(fx.VARS)},
                             unsafe_allow_colocated_anchor=True)
        idx = list(bm.inspect_store_contract(self.live).days).index(self.repaired)
        g = zarr.open_group(self.live, mode="a")
        g["sst"][idx] = np.asarray(g["sst"][idx]) + 21.0
        fp, _v, _t = cd.fingerprint_day(self.live, self.repaired, physical_index=idx)
        rw.append(self.wal, record=rw.COMMITTED, repair_id=rec["repair_id"],
                  day=self.repaired, at_utc="t", operator="ops",
                  payload={"fingerprint": fp}, unsafe_allow_colocated_anchor=True)
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
            wal_root=self.wal, unsafe_allow_colocated_anchor=True,
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
                           wal_root=self.wal, manifest_root=self.root,
                           unsafe_allow_colocated_anchor=True)

    def _swap(self, plan, **kw):
        kw.setdefault("pre_swap_quiesce_fn", _attested)
        return execute_swap_plan(plan, mode="s1", hold_dir=self.hold,
                                 wal_root=self.wal, manifest_root=self.root,
                                 unsafe_allow_colocated_anchor=True,
                                 compaction_lock_path=self.clock, **kw)

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
                                                         "observed_inflight": 0})
        self.assertEqual(res["status"], "quiesce_failed")
        self.assertIn("drained=False", res["reason"])

    def test_a_truthy_string_does_not_launder_into_a_drain(self):
        """`bool("false")` is True — the shape that made an unset shell variable grant a waiver
        in Part 2 round 7. `drained` must be exactly `True`."""
        for bad in ("false", "true", 1, [1]):
            with self.subTest(repr(bad)):
                res = self._attempt(pre_swap_quiesce_fn=lambda b=bad: {
                    "drained": b, "evidence": "x", "observed_inflight": 0})
                self.assertEqual(res["status"], "quiesce_failed")
                self.assertIn("must be exactly True", res["reason"])

    def test_an_attestation_without_evidence_is_refused(self):
        for bad in ({"evidence": None}, {"evidence": ""}, {"evidence": "   "}, {"evidence": 7}):
            with self.subTest(repr(bad)):
                res = self._attempt(pre_swap_quiesce_fn=lambda b=bad: {
                    "drained": True, "observed_inflight": 0, **b})
                self.assertEqual(res["status"], "quiesce_failed")
                self.assertIn("no `evidence` string", res["reason"])

    def test_a_positive_in_flight_count_is_refused_even_when_drained_is_claimed(self):
        res = self._attempt(pre_swap_quiesce_fn=lambda: {
            "drained": True, "evidence": "pm2 stop returned 0", "observed_inflight": 2})
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
        full = {"drained": True, "evidence": "e", "observed_inflight": 0}
        for drop in ("drained", "evidence", "observed_inflight"):
            with self.subTest(drop):
                att = {k: v for k, v in full.items() if k != drop}
                res = self._attempt(pre_swap_quiesce_fn=lambda a=att: dict(a))
                self.assertEqual(res["status"], "quiesce_failed")
                self.assertIn(f"missing required field(s) {drop}", res["reason"])

    def test_a_negative_inflight_count_is_not_fewer_than_none(self):
        res = self._attempt(pre_swap_quiesce_fn=lambda: {
            "drained": True, "evidence": "e", "observed_inflight": -1})
        self.assertEqual(res["status"], "quiesce_failed")
        self.assertIn("not a measurement", res["reason"])

    def test_the_inflight_count_must_be_a_raw_int(self):
        """`isinstance(True, int)` is True, so a bool would otherwise read as a proven zero."""
        for bad in (False, True, "0", 0.0, None):
            with self.subTest(repr(bad)):
                res = self._attempt(pre_swap_quiesce_fn=lambda b=bad: {
                    "drained": True, "evidence": "e", "observed_inflight": b})
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

    def test_the_waiver_has_to_be_typed(self):
        """`unsafe_skip_quiescence` exists for suites that predate §7.9. It is named so it
        cannot be reached by omission, which is how the old default behaved."""
        res = execute_swap_plan(self._plan(), mode="s1", hold_dir=self.hold,
                                wal_root=self.wal, manifest_root=self.root,
                                unsafe_allow_colocated_anchor=True,
                                compaction_lock_path=self.clock,
                                unsafe_skip_quiescence=True)
        self.assertEqual(res["status"], "swapped", res.get("reason"))


class TestTheProductionRunbookMatchesTheContract(unittest.TestCase):
    """Review round 2, finding 1: the one production runbook was broken by this API change.

    Its `quiesce()` ended in a bare `return`, so under the new executor it would stop pm2, close
    the port, return None, and be refused — the swap never running while the app was down. A
    contract that only the tests satisfy is not a contract, so the runbook is checked here."""

    RUNBOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "specs", "p4s10_production_rollout_runbook.md")

    def setUp(self):
        with open(self.RUNBOOK) as fh:
            text = fh.read()
        start = text.index("def quiesce():")
        self.body = text[start:text.index("\ndef ", start + 1)]

    def test_the_runbook_hook_returns_an_attestation_not_a_bare_return(self):
        self.assertNotIn("; return\n", self.body,
                         "a bare `return` is exactly what the executor now refuses")
        self.assertIn("return att", self.body)

    def test_the_runbook_hook_supplies_every_required_core_field(self):
        """If a core field is ever added, this fails until the runbook is updated too — which is
        the drift that produced this finding."""
        for field in sd.QUIESCENCE_CORE_FIELDS:
            with self.subTest(field):
                self.assertIn(f'"{field}"', self.body)

    def test_the_attestation_the_runbook_builds_is_ACCEPTED_by_the_executor(self):
        """The shape itself, run through the real validator rather than eyeballed."""
        att = {"drained": True,
               "evidence": ("pm2 stop ghrsst -> exit 0; GET /healthz on :8035 refused; "
                            "pm2 jlist shows 0 online workers for ghrsst"),
               "observed_inflight": 0,
               "checked_utc": "2027-01-01T00:00:00+00:00"}
        self.assertIsNone(sd._quiescence_refusal(att))
        self.assertEqual(sd.normalize_quiescence(att)["extra_fields"], ["checked_utc"])

    def test_the_runbook_measures_the_worker_count_rather_than_inferring_it(self):
        """A closed port says new connections are refused; it does not say the worker finished
        the request it was already serving."""
        self.assertIn("pm2", self.body)
        self.assertIn("worker_pids()", self.body)


if __name__ == "__main__":
    unittest.main()
