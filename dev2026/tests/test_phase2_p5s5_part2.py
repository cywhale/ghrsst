"""dev2026 — P5-S5 Part 2: the corrected-day lifecycle (§7.5a, §7.8, §7.8a).

The rule under test, and the only one that matters:

> A repaired day may leave delta **only** when E1 repair-identity authorization and §7.8a
> Phase A base-only verification have **both** passed.

Every check here reads base through a **base-only** `SegmentedCubeStore`. A public point GET is
never used as proof: pre-prune the repaired day is still in delta and `TieredCube` is
delta-wins, so a public read returns delta and would pass whether or not the refold worked —
the exact trap §7.8a was written to close.
"""
from __future__ import annotations

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
from ingest import corrected_day as cd  # noqa: E402
from ingest import publish_manifest as pub  # noqa: E402
from ingest.build_block import build_block  # noqa: E402
from ingest.prune_delta import prune_delta  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store import repair_wal as rw  # noqa: E402
from store import source_provenance as sp  # noqa: E402
from store.segmented_cube import SegmentedCubeStore  # noqa: E402

ANCHOR = "2026-06-27"
S = 90


class _Base(unittest.TestCase):
    """A live generation holding one sealed block, and a delta covering the same days."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "manifest_root")
        os.makedirs(self.root, exist_ok=True)
        self.wal = os.path.join(self.tmp, "wal")
        os.makedirs(self.wal, exist_ok=True)
        self.lock = os.path.join(self.tmp, "ingest.lock")
        self.artifacts = os.path.join(self.tmp, "artifacts")
        os.makedirs(self.artifacts, exist_ok=True)
        self.s0, self.e0 = bm.block_bounds(ANCHOR, S, 0)
        self.span = fx.calendar_span(self.s0, self.e0)
        self.days = self.span[:8]
        self.repaired = self.days[3]

        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.days)
        self.v1 = self._fold("b_v1")
        self.v1_id = self.v1["segment"]["segment_id"]
        self._publish_gen(self.v1, generation=1)

    # ---- helpers ---------------------------------------------------------
    def _fold(self, name, *, delta_path=None, wal_root=None):
        return build_block(os.path.join(self.root, name + ".zarr"),
                           start_day=self.s0, end_day=self.e0,
                           classification_target=list(self.days),
                           delta_path=delta_path or self.delta,
                           artifacts_dir=self.artifacts, lock=None, hard_reserve_bytes=0,
                           unsafe_skip_isolation=True, wal_root=wal_root)

    def _publish_gen(self, plan, *, generation):
        """Publish the block as generation 1 directly (the S4 path is exercised elsewhere)."""
        seg = dict(plan["segment"])
        m = {
            "format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION,
            "generation": generation, "generation_id": f"gen{generation:06d}-x",
            "created_utc": "2027-01-01T00:00:00Z", "created_by": "p5s5p2-tests",
            "predecessor_generation": generation - 1 if generation > 1 else None,
            "predecessor_manifest": (bm.archive_name(generation - 1)
                                     if generation > 1 else None),
            "grid": {"ny": 32, "nx": 32, "region": [0, 32, 0, 32]},
            "variables": list(fx.VARS),
            "block_grid": {"anchor_day": ANCHOR, "block_days": S},
            "segments": [seg], "superseded": [], "manifest_checksum": "",
        }
        m["manifest_checksum"] = bm.compute_checksum(m)
        bm.publish(self.root, m)
        return seg

    def _open_repair(self, day=None):
        rec = rw.open_repair(self.wal, day=day or self.repaired, at_utc="t", operator="ops",
                             payload={"expected_vars": list(fx.VARS)})
        return rec["repair_id"]

    def _apply_repair(self, day=None, *, bump=25.0):
        """Rewrite the delta day, the way a P4-S4 repair_overwrite swap would."""
        day = day or self.repaired
        idx = list(bm.inspect_store_contract(self.delta).days).index(day)
        g = zarr.open_group(self.delta, mode="a")
        g["sst"][idx] = np.asarray(g["sst"][idx]) + bump

    def _commit_repair(self, repair_id, day=None):
        day = day or self.repaired
        idx = list(bm.inspect_store_contract(self.delta).days).index(day)
        fp, _valid, _tiles = cd.fingerprint_day(self.delta, day, physical_index=idx)
        rw.append(self.wal, record=rw.COMMITTED, repair_id=repair_id, day=day,
                  at_utc="t", operator="ops", payload={"fingerprint": fp})
        return fp

    def _refold(self, name="b_v2"):
        """§7.8: a NEW immutable version of the same calendar window, delta-first."""
        plan = self._fold(name, wal_root=self.wal)
        seg = dict(plan["segment"])
        seg["supersedes"] = self.v1_id
        plan["segment"] = seg
        self._publish_gen(plan, generation=2)
        return plan

    def _repair_and_refold(self):
        rid = self._open_repair()
        self._apply_repair()
        fp = self._commit_repair(rid)
        plan = self._refold()
        self.v2_id = plan["segment"]["segment_id"]
        return rid, fp, plan


# ==================================================================== E2
class TestE2Parity(_Base):
    def test_a_matching_base_and_delta_day_compare_clean(self):
        self.assertEqual(cd.e2_parity(self.days[0], delta_path=self.delta,
                                      base_store_path=self.v1["out_path"]), [])

    def test_a_repaired_delta_day_no_longer_matches_the_stale_block(self):
        """The state a corrective refold exists to resolve."""
        self._apply_repair()
        diffs = cd.e2_parity(self.repaired, delta_path=self.delta,
                             base_store_path=self.v1["out_path"])
        self.assertTrue(diffs)
        self.assertTrue(any("sst" in d for d in diffs))

    def test_the_window_is_keyed_on_the_DATE_not_a_physical_index(self):
        """Delta and block hold the same date at different slots; a physical index would make
        the two sides sample different CELLS and disagree for a reason that is not a
        difference. Asserted on the window, which is the thing the slot chooses."""
        self.assertEqual(cd.date_slot("2026-06-30") - cd.date_slot("2026-06-27"), 3)
        day = self.days[0]
        delta_idx = list(bm.inspect_store_contract(self.delta).days).index(day)
        block_idx = list(bm.inspect_store_contract(self.v1["out_path"]).days).index(day)
        self.assertNotEqual(delta_idx, None)
        self.assertNotEqual(block_idx, None)

        # The slot chooses the sampled CELLS. Same date -> same cells, whatever the physical
        # index of the day in either store.
        cells = sp.sample_offsets(32, 32, seed=sp.SAMPLE_SEED, day_index=cd.date_slot(day))
        self.assertEqual(
            sp.sample_offsets(32, 32, seed=sp.SAMPLE_SEED, day_index=cd.date_slot(day)), cells)
        # A different date must move them, or every day shares one sample forever.
        self.assertNotEqual(
            cells,
            sp.sample_offsets(32, 32, seed=sp.SAMPLE_SEED,
                              day_index=cd.date_slot(self.days[5])))
        # At production geometry the WINDOW moves too; at 32x32 it clamps to the whole grid,
        # so asserting on it here would assert nothing.
        self.assertNotEqual(
            sp.sample_window(17999, 36000, seed=sp.SAMPLE_SEED, day_index=cd.date_slot(day)),
            sp.sample_window(17999, 36000, seed=sp.SAMPLE_SEED,
                             day_index=cd.date_slot(self.days[5])))

    def test_a_fingerprint_at_the_wrong_slot_is_refused(self):
        with self.assertRaises(cd.CorrectedDayRefused) as cm:
            cd.fingerprint_day(self.delta, self.days[0], physical_index=2)
        self.assertIn("describes the wrong day", str(cm.exception))


# ==================================================================== §7.8a Phase A
class TestPhaseABaseOnly(_Base):
    def test_a_completed_refold_passes_phase_A(self):
        rid, fp, _plan = self._repair_and_refold()
        out = cd.phase_a_base_only(self.repaired, manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal,
                                   expected_segment_id=self.v2_id)
        self.assertEqual(out["repair_id"], rid)
        self.assertEqual(out["e2"], "match")
        self.assertEqual(out["fingerprint"], fp)
        self.assertTrue(out["verified_pre_prune"])

    def test_a_repair_with_NO_refold_fails_phase_A(self):
        """Crash boundary: committed, corrective refold not published. Base still holds the
        old value, and the block records no materialized repair."""
        rid = self._open_repair()
        self._apply_repair()
        self._commit_repair(rid)
        with self.assertRaises(cd.CorrectedDayRefused) as cm:
            cd.phase_a_base_only(self.repaired, manifest_root=self.root,
                                 delta_path=self.delta, wal_root=self.wal)
        self.assertIn("E2 parity failed", str(cm.exception))

    def test_the_manifest_must_resolve_to_the_CORRECTED_version(self):
        """A refold that published but did not take effect looks identical from outside."""
        self._repair_and_refold()
        with self.assertRaises(cd.CorrectedDayRefused) as cm:
            cd.phase_a_base_only(self.repaired, manifest_root=self.root,
                                 delta_path=self.delta, wal_root=self.wal,
                                 expected_segment_id="b_v3_never_published")
        self.assertIn("not the corrected version", str(cm.exception))

    def test_a_LATER_repair_supersedes_what_the_block_materialized(self):
        """Crash boundary 4: two committed ids, block materialized the earlier."""
        self._repair_and_refold()
        rid2 = self._open_repair()
        self._apply_repair(bump=5.0)
        self._commit_repair(rid2)
        with self.assertRaises(cd.CorrectedDayRefused) as cm:
            cd.phase_a_base_only(self.repaired, manifest_root=self.root,
                                 delta_path=self.delta, wal_root=self.wal)
        # E2 fires first: base now differs from the re-repaired delta. Both are refusals.
        self.assertIn("E2 parity failed", str(cm.exception))

    def test_a_tampered_materialized_fingerprint_is_refused(self):
        """The id agrees and the bytes do not, which is worse than a plain mismatch."""
        self._repair_and_refold()
        live = bm.load_live(self.root)
        seg = live["segments"][0]
        seg["build_provenance"]["materialized_repairs"][self.repaired][
            "source_fingerprint"] = "0" * 64
        with self.assertRaises(cd.CorrectedDayRefused) as cm:
            cd._verify_repair_identity(self.repaired, seg=seg, wal_root=self.wal,
                                       delta_path=self.delta)
        self.assertIn("does not match the WAL", str(cm.exception))

    def test_identity_refuses_a_segment_with_NO_materialized_entry(self):
        """`prune_eligibility` runs E1 first and refuses this earlier, so the arm inside Phase A
        is exercised where it can be reached. An unreachable-by-test refusal is not a refusal."""
        rid = self._open_repair()
        self._apply_repair()
        self._commit_repair(rid)
        seg = dict(bm.load_live(self.root)["segments"][0])
        seg["build_provenance"] = dict(seg.get("build_provenance") or {},
                                       materialized_repairs={})
        with self.assertRaises(cd.CorrectedDayRefused) as cm:
            cd._verify_repair_identity(self.repaired, seg=seg, wal_root=self.wal,
                                       delta_path=self.delta)
        self.assertIn("has not been folded into base", str(cm.exception))

    def test_identity_refuses_a_STALE_repair_id(self):
        rid, _fp, _plan = self._repair_and_refold()
        rid2 = self._open_repair()
        self._apply_repair(bump=2.0)
        self._commit_repair(rid2)
        seg = dict(bm.load_live(self.root)["segments"][0])
        with self.assertRaises(cd.CorrectedDayRefused) as cm:
            cd._verify_repair_identity(self.repaired, seg=seg, wal_root=self.wal,
                                       delta_path=self.delta)
        self.assertIn("supersedes what the block carries", str(cm.exception))
        self.assertIn(rid, str(cm.exception))

    def test_identity_refuses_a_repair_materialized_from_a_NON_delta_source(self):
        """§7.1a: a corrective refold reads the repaired day from delta by construction. A
        block-sourced attestation would be carrying a predecessor's claim forward as its own."""
        rid = self._open_repair()
        self._apply_repair()
        fp = self._commit_repair(rid)
        seg = dict(bm.load_live(self.root)["segments"][0])
        seg["build_provenance"] = dict(seg.get("build_provenance") or {},
                                       materialized_repairs={self.repaired: {
                                           "repair_id": rid, "source_kind": "block",
                                           "source_fingerprint": fp}})
        with self.assertRaises(cd.CorrectedDayRefused) as cm:
            cd._verify_repair_identity(self.repaired, seg=seg, wal_root=self.wal,
                                       delta_path=self.delta)
        self.assertIn("reads the repaired day from delta", str(cm.exception))

    def test_phase_A_refuses_once_the_day_has_already_left_delta(self):
        """Phase A is defined PRE-prune. Against a delta that no longer holds the day there is
        nothing to compare base with, and a comparison against nothing passes vacuously."""
        self._repair_and_refold()
        pruned = os.path.join(self.tmp, "delta_pruned.zarr")
        fx.build_delta(pruned, [d for d in self.days if d != self.repaired])
        with self.assertRaises(cd.CorrectedDayRefused) as cm:
            cd.phase_a_base_only(self.repaired, manifest_root=self.root,
                                 delta_path=pruned, wal_root=self.wal)
        self.assertIn("PRE-prune", str(cm.exception))

    def test_phase_A_reads_BASE_not_the_public_view(self):
        """The trap §7.8a closes: with the repaired day still in delta, a delta-wins public read
        returns the delta value and proves nothing. Asserted by showing the base-only store
        returns the corrected value on its own."""
        self._repair_and_refold()
        store = SegmentedCubeStore(self.root)
        seg_idx, _ = store.resolve(self.repaired)
        self.assertEqual(store.segment_id(seg_idx), self.v2_id)
        rows = store.point_series(100.0, 0.0, [self.repaired], ["sst"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], self.repaired)


# ==================================================================== the prune gate
class TestPruneEligibility(_Base):
    def test_a_never_repaired_day_needs_only_the_WALs_silence(self):
        out = cd.prune_eligibility([self.days[0]], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [self.days[0]])

    def test_an_OPEN_intent_blocks_its_day(self):
        self._open_repair()
        out = cd.prune_eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [])
        self.assertIn("no timeout", out["refused"][self.repaired])

    def test_a_committed_repair_without_a_refold_is_refused(self):
        rid = self._open_repair()
        self._apply_repair()
        self._commit_repair(rid)
        out = cd.prune_eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [])
        self.assertIn("has not been folded into base", out["refused"][self.repaired])

    def test_a_completed_refold_authorizes_the_day(self):
        self._repair_and_refold()
        out = cd.prune_eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [self.repaired])
        self.assertEqual(out["refused"], {})

    def test_an_untrustworthy_WAL_refuses_every_day(self):
        self._repair_and_refold()
        with open(os.path.join(self.wal, rw.WAL_NAME), "a") as fh:
            fh.write('{"seq": 9, "tr')
        out = cd.prune_eligibility(self.days, manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [])
        self.assertEqual(set(out["refused"]), set(self.days))
        self.assertEqual(out["wal"], "untrustworthy")

    def test_E1_passing_is_not_enough_without_phase_A(self):
        """The hard rule, isolated. E1 is satisfied by construction — the block records the
        latest committed id and the matching fingerprint — and Phase A still refuses because
        base does not serve the corrected value."""
        rid, fp, _ = self._repair_and_refold()
        state = rw.read_wal(self.wal)
        mr = cd._materialized_map(self.root, [self.repaired])
        self.assertEqual(
            rw.prune_authorization(state, [self.repaired], materialized_repairs=mr)["authorized"],
            [self.repaired], "precondition: E1 alone authorizes this day")

        g = zarr.open_group(self.v1["out_path"], mode="r")     # corrupt the PUBLISHED v2
        live = bm.load_live(self.root)
        block = os.path.join(self.root, live["segments"][0]["path"])
        idx = list(bm.inspect_store_contract(block).days).index(self.repaired)
        slot = cd.date_slot(self.repaired)
        i0, i1, j0, j1 = sp.sample_window(32, 32, seed=sp.SAMPLE_SEED, day_index=slot)
        gb = zarr.open_group(block, mode="a")
        gb["sst"][idx, i0:i1, j0:j1] = np.asarray(gb["sst"][idx, i0:i1, j0:j1]) + 3.0

        out = cd.prune_eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [])
        self.assertIn("Phase A did not pass", out["refused"][self.repaired])


# ==================================================================== prune_delta wiring
class TestPruneDeltaHonoursTheGate(_Base):
    def _keep(self):
        return [d for d in self.days if d != self.days[0]]

    def test_dropping_days_REQUIRES_wal_root_and_manifest_root(self):
        out = prune_delta(self.delta, os.path.join(self.tmp, "staging.zarr"), self._keep(),
                          base_days=self.days, spatial_window_days=1)
        self.assertEqual(out["status"], "refused")
        self.assertIn("wal_root and manifest_root are REQUIRED", out["reason"])

    def test_a_corrected_day_without_a_refold_refuses_the_prune(self):
        """Base has day-membership for it, so the pre-existing coverage gate passes. Only the
        §7.5a gate stops it, and dropping it would lose the correction permanently."""
        rid = self._open_repair()
        self._apply_repair()
        self._commit_repair(rid)
        keep = [d for d in self.days if d != self.repaired]
        out = prune_delta(self.delta, os.path.join(self.tmp, "staging.zarr"), keep,
                          base_days=self.days, spatial_window_days=1,
                          wal_root=self.wal, manifest_root=self.root)
        self.assertEqual(out["status"], "refused")
        self.assertIn("not authorized to leave delta", out["reason"])
        self.assertEqual(out["corrected_day_refused"], [self.repaired])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "staging.zarr")))

    def test_an_open_intent_refuses_the_prune(self):
        self._open_repair()
        keep = [d for d in self.days if d != self.repaired]
        out = prune_delta(self.delta, os.path.join(self.tmp, "staging.zarr"), keep,
                          base_days=self.days, spatial_window_days=1,
                          wal_root=self.wal, manifest_root=self.root)
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["corrected_day_refused"], [self.repaired])

    def test_after_a_refold_the_prune_proceeds(self):
        """The ordering completes: repair -> refold -> publish -> Phase A -> prune."""
        self._repair_and_refold()
        keep = [d for d in self.days if d != self.repaired]
        out = prune_delta(self.delta, os.path.join(self.tmp, "staging.zarr"), keep,
                          base_days=self.days, spatial_window_days=1,
                          wal_root=self.wal, manifest_root=self.root)
        self.assertEqual(out["status"], "ok", out.get("reason"))
        self.assertEqual(out["dropped_days"], [self.repaired])

    def test_a_pure_reorder_needs_no_gate(self):
        """Nothing is dropped, so there is no corrected day to authorize."""
        out = prune_delta(self.delta, os.path.join(self.tmp, "staging.zarr"), self.days,
                          spatial_window_days=1)
        self.assertEqual(out["status"], "ok", out.get("reason"))
        self.assertEqual(out["dropped_days"], [])


# ==================================================================== §7.8a end to end
class TestTheFullOrdering(_Base):
    def test_repair_refold_verify_prune_and_the_correction_survives(self):
        """§7.8a A → B → C. Phase C is the end-to-end assertion: with the day gone from delta,
        a base read still returns the corrected value — necessarily from the corrected block."""
        rid = self._open_repair()
        self._apply_repair()
        fp = self._commit_repair(rid)

        # PHASE A must fail before the refold: base still holds the stale value.
        with self.assertRaises(cd.CorrectedDayRefused):
            cd.phase_a_base_only(self.repaired, manifest_root=self.root,
                                 delta_path=self.delta, wal_root=self.wal)

        v2 = self._refold()
        phase_a = cd.phase_a_base_only(self.repaired, manifest_root=self.root,
                                       delta_path=self.delta, wal_root=self.wal,
                                       expected_segment_id=v2["segment"]["segment_id"])
        self.assertEqual(phase_a["repair_id"], rid)
        self.assertEqual(phase_a["fingerprint"], fp)

        # value recorded from BASE, pre-prune
        store = SegmentedCubeStore(self.root)
        before = store.point_series(100.0, 0.0, [self.repaired], ["sst"])[0]

        # PHASE B — authorized only now
        keep = [d for d in self.days if d != self.repaired]
        plan = prune_delta(self.delta, os.path.join(self.tmp, "staging.zarr"), keep,
                           base_days=self.days, spatial_window_days=1,
                           wal_root=self.wal, manifest_root=self.root)
        self.assertEqual(plan["status"], "ok", plan.get("reason"))

        # PHASE C — the day is gone from the (staged) delta; base still serves the correction
        staged_days = list(bm.inspect_store_contract(plan["out_path"]).days)
        self.assertNotIn(self.repaired, staged_days)
        after = SegmentedCubeStore(self.root).point_series(
            100.0, 0.0, [self.repaired], ["sst"])[0]
        self.assertEqual(after, before,
                         "the corrected value must survive the prune unchanged")

    def test_the_sealed_v1_path_is_never_modified(self):
        """§7.8.1: the sealed version keeps serving until the corrected one is published, and
        its path is never touched."""
        before = bm.metadata_fingerprint_from_inspection(
            bm.inspect_store_contract(self.v1["out_path"]))
        self._repair_and_refold()
        after = bm.metadata_fingerprint_from_inspection(
            bm.inspect_store_contract(self.v1["out_path"]))
        self.assertEqual(before, after)
        self.assertTrue(os.path.isdir(self.v1["out_path"]))

    def test_the_refold_records_what_it_MATERIALIZED_derived_not_accepted(self):
        rid, fp, plan = self._repair_and_refold()
        mr = plan["segment"]["build_provenance"]["materialized_repairs"]
        self.assertEqual(set(mr), {self.repaired},
                         "only the repaired day is attested, not every day in the fold")
        self.assertEqual(mr[self.repaired]["repair_id"], rid)
        self.assertEqual(mr[self.repaired]["source_kind"], "delta")
        self.assertEqual(mr[self.repaired]["source_fingerprint"], fp)

    def test_a_day_carried_from_the_PREDECESSOR_block_is_not_attested(self):
        """The builder attests only what it READ from delta. A day resolved from the
        predecessor block carries that block's claim forward; re-attesting it would say this
        fold consumed a repair it never read."""
        rid = self._open_repair()
        self._apply_repair()
        self._commit_repair(rid)
        pruned = os.path.join(self.tmp, "delta_without.zarr")
        fx.build_delta(pruned, [d for d in self.days if d != self.repaired])
        plan = build_block(os.path.join(self.root, "b_carry.zarr"),
                           start_day=self.s0, end_day=self.e0,
                           classification_target=[d for d in self.days if d != self.repaired],
                           predecessor_present=[self.repaired],
                           predecessor_path=self.v1["out_path"], delta_path=pruned,
                           artifacts_dir=self.artifacts, lock=None, hard_reserve_bytes=0,
                           unsafe_skip_isolation=True, wal_root=self.wal)
        smap = plan["source_map"]
        self.assertEqual(smap[self.repaired]["source_kind"], "block",
                         "precondition: the repaired day came from the predecessor block")
        self.assertEqual(plan["segment"]["build_provenance"]["materialized_repairs"], {})

    def test_a_fold_without_a_wal_root_attests_nothing(self):
        """The builder derives the record from the WAL; with no WAL it must claim nothing
        rather than claim an empty success."""
        rid = self._open_repair()
        self._apply_repair()
        self._commit_repair(rid)
        plan = self._fold("b_nowal")
        self.assertEqual(plan["segment"]["build_provenance"]["materialized_repairs"], {})
        out = cd.prune_eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [],
                         "a block that attests nothing cannot authorize a prune")


if __name__ == "__main__":
    unittest.main()
