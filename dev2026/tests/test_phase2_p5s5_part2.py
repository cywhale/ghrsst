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

import json

import numpy as np
import zarr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import p5_fixtures as fx  # noqa: E402
from ingest import corrected_day as cd  # noqa: E402
from ingest import publish_manifest as pub  # noqa: E402
from ingest.build_block import build_block  # noqa: E402
from ingest.prune_delta import prune_delta  # noqa: E402
from ingest.swap_delta import execute_swap_plan  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store import repair_wal as rw  # noqa: E402
from store import source_provenance as sp  # noqa: E402
from store.segmented_cube import SegmentedCubeStore  # noqa: E402
from store.tiered_cube import TieredCube  # noqa: E402
from store.time_cube import TimeCubeStore  # noqa: E402

def _refold_entry(*a, **kw):
    kw.setdefault("unsafe_allow_colocated_anchor", True)
    return cd.corrective_refold(*a, **kw)


def _prune(*a, **kw):
    kw.setdefault("unsafe_allow_colocated_anchor", True)
    return prune_delta(*a, **kw)


def _swap(*a, **kw):
    kw.setdefault("unsafe_allow_colocated_anchor", True)
    return execute_swap_plan(*a, **kw)


def _eligibility(*a, **kw):
    """Shim: this suite's WAL uses a co-located anchor (see `_Base.setUp`)."""
    kw.setdefault("unsafe_allow_colocated_anchor", True)
    return cd.prune_eligibility(*a, **kw)


def _wal_append(*a, **kw):
    """Shim: the co-located suite (see `_Base.setUp`) waives the external-anchor rule here."""
    kw.setdefault("unsafe_allow_colocated_anchor", True)
    return rw.append(*a, **kw)


def _identity(*a, **kw):
    kw.setdefault("unsafe_allow_colocated_anchor", True)
    return cd._verify_repair_identity(*a, **kw)


def _phase_a(*a, **kw):
    kw.setdefault("unsafe_allow_colocated_anchor", True)
    return cd.phase_a_base_only(*a, **kw)


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
        self.clock = os.path.join(self.tmp, "p5_compaction.lock")
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
        # The WAL is bound to this manifest authority before anything asks it a question.
        # Most of this suite is not about the anchor domain, so it waives the external-anchor
        # requirement in ONE named place. TestExternalAnchorWorkflow uses a real separate
        # domain end to end.
        rw.initialize_wal(self.wal, manifest_root=self.root, at_utc="t", operator="ops",
                          unsafe_allow_colocated_anchor=True)

    # ---- helpers ---------------------------------------------------------
    def _fold(self, name, *, delta_path=None, wal_root=None):
        return build_block(os.path.join(self.root, name + ".zarr"),
                           start_day=self.s0, end_day=self.e0,
                           classification_target=list(self.days),
                           delta_path=delta_path or self.delta,
                           artifacts_dir=self.artifacts, lock=None, hard_reserve_bytes=0,
                           unsafe_skip_isolation=True, wal_root=wal_root,
                           unsafe_allow_colocated_anchor=True)

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
                             payload={"expected_vars": list(fx.VARS)},
                             unsafe_allow_colocated_anchor=True)
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
        _wal_append(self.wal, record=rw.COMMITTED, repair_id=repair_id, day=day,
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
        out = _phase_a(self.repaired, manifest_root=self.root,
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
            _phase_a(self.repaired, manifest_root=self.root,
                                 delta_path=self.delta, wal_root=self.wal)
        self.assertIn("E2 parity failed", str(cm.exception))

    def test_the_manifest_must_resolve_to_the_CORRECTED_version(self):
        """A refold that published but did not take effect looks identical from outside."""
        self._repair_and_refold()
        with self.assertRaises(cd.CorrectedDayRefused) as cm:
            _phase_a(self.repaired, manifest_root=self.root,
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
            _phase_a(self.repaired, manifest_root=self.root,
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
            _identity(self.repaired, seg=seg, wal_root=self.wal,
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
            _identity(self.repaired, seg=seg, wal_root=self.wal,
                                       delta_path=self.delta)
        self.assertIn("has not been folded into base", str(cm.exception))

    def test_identity_refuses_a_STALE_repair_id(self):
        rid, _fp, _plan = self._repair_and_refold()
        rid2 = self._open_repair()
        self._apply_repair(bump=2.0)
        self._commit_repair(rid2)
        seg = dict(bm.load_live(self.root)["segments"][0])
        with self.assertRaises(cd.CorrectedDayRefused) as cm:
            _identity(self.repaired, seg=seg, wal_root=self.wal,
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
            _identity(self.repaired, seg=seg, wal_root=self.wal,
                                       delta_path=self.delta)
        self.assertIn("reads the repaired day from delta", str(cm.exception))

    def test_phase_A_refuses_once_the_day_has_already_left_delta(self):
        """Phase A is defined PRE-prune. Against a delta that no longer holds the day there is
        nothing to compare base with, and a comparison against nothing passes vacuously."""
        self._repair_and_refold()
        pruned = os.path.join(self.tmp, "delta_pruned.zarr")
        fx.build_delta(pruned, [d for d in self.days if d != self.repaired])
        with self.assertRaises(cd.CorrectedDayRefused) as cm:
            _phase_a(self.repaired, manifest_root=self.root,
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
        out = _eligibility([self.days[0]], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [self.days[0]])

    def test_an_OPEN_intent_blocks_its_day(self):
        self._open_repair()
        out = _eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [])
        self.assertIn("no timeout", out["refused"][self.repaired])

    def test_a_committed_repair_without_a_refold_is_refused(self):
        rid = self._open_repair()
        self._apply_repair()
        self._commit_repair(rid)
        out = _eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [])
        self.assertIn("has not been folded into base", out["refused"][self.repaired])

    def test_a_completed_refold_authorizes_the_day(self):
        self._repair_and_refold()
        out = _eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [self.repaired])
        self.assertEqual(out["refused"], {})

    def test_an_untrustworthy_WAL_refuses_every_day(self):
        self._repair_and_refold()
        with open(os.path.join(self.wal, rw.WAL_NAME), "a") as fh:
            fh.write('{"seq": 9, "tr')
        out = _eligibility(self.days, manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [])
        self.assertEqual(set(out["refused"]), set(self.days))
        self.assertEqual(out["wal"], "untrustworthy")

    def test_E1_passing_is_not_enough_without_phase_A(self):
        """The hard rule, isolated. E1 is satisfied by construction — the block records the
        latest committed id and the matching fingerprint — and Phase A still refuses because
        base does not serve the corrected value."""
        rid, fp, _ = self._repair_and_refold()
        state = rw.read_wal(self.wal, anchor_root=self.wal)
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

        out = _eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [])
        self.assertIn("Phase A did not pass", out["refused"][self.repaired])


# ==================================================================== prune_delta wiring
class TestPruneDeltaHonoursTheGate(_Base):
    def _keep(self):
        return [d for d in self.days if d != self.days[0]]

    def test_dropping_days_REQUIRES_wal_root_and_manifest_root(self):
        out = _prune(self.delta, os.path.join(self.tmp, "staging.zarr"), self._keep(),
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
        out = _prune(self.delta, os.path.join(self.tmp, "staging.zarr"), keep,
                          base_days=self.days, spatial_window_days=1,
                          wal_root=self.wal, manifest_root=self.root)
        self.assertEqual(out["status"], "refused")
        self.assertIn("not authorized to leave delta", out["reason"])
        self.assertEqual(out["corrected_day_refused"], [self.repaired])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "staging.zarr")))

    def test_an_open_intent_refuses_the_prune(self):
        self._open_repair()
        keep = [d for d in self.days if d != self.repaired]
        out = _prune(self.delta, os.path.join(self.tmp, "staging.zarr"), keep,
                          base_days=self.days, spatial_window_days=1,
                          wal_root=self.wal, manifest_root=self.root)
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["corrected_day_refused"], [self.repaired])

    def test_after_a_refold_the_prune_proceeds(self):
        """The ordering completes: repair -> refold -> publish -> Phase A -> prune."""
        self._repair_and_refold()
        keep = [d for d in self.days if d != self.repaired]
        out = _prune(self.delta, os.path.join(self.tmp, "staging.zarr"), keep,
                          base_days=self.days, spatial_window_days=1,
                          wal_root=self.wal, manifest_root=self.root)
        self.assertEqual(out["status"], "ok", out.get("reason"))
        self.assertEqual(out["dropped_days"], [self.repaired])

    def test_a_pure_reorder_needs_no_gate(self):
        """Nothing is dropped, so there is no corrected day to authorize."""
        out = _prune(self.delta, os.path.join(self.tmp, "staging.zarr"), self.days,
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
            _phase_a(self.repaired, manifest_root=self.root,
                                 delta_path=self.delta, wal_root=self.wal)

        v2 = self._refold()
        phase_a = _phase_a(self.repaired, manifest_root=self.root,
                                       delta_path=self.delta, wal_root=self.wal,
                                       expected_segment_id=v2["segment"]["segment_id"])
        self.assertEqual(phase_a["repair_id"], rid)
        self.assertEqual(phase_a["fingerprint"], fp)

        # value recorded from BASE, pre-prune
        store = SegmentedCubeStore(self.root)
        before = store.point_series(100.0, 0.0, [self.repaired], ["sst"])[0]

        # PHASE B — authorized only now
        keep = [d for d in self.days if d != self.repaired]
        plan = _prune(self.delta, os.path.join(self.tmp, "staging.zarr"), keep,
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
                           unsafe_skip_isolation=True, wal_root=self.wal,
                           unsafe_allow_colocated_anchor=True)
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
        out = _eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [],
                         "a block that attests nothing cannot authorize a prune")


if __name__ == "__main__":
    unittest.main()


# ============ review round 2: swap-time re-authorization, WAL identity, live-delta binding
class TestSwapTimeReauthorization(_Base):
    """The staleness guard compares DAY SETS. A repair committed between plan and swap does not
    change the day set at all — same day, same date — so the guard sees nothing. Re-running the
    gate under the ingest lock, before the path switch, is the only thing between "a newer
    correction landed" and "that correction was dropped"."""

    def _plan_dropping_repaired(self):
        self._repair_and_refold()
        keep = [d for d in self.days if d != self.repaired]
        plan = _prune(self.delta, os.path.join(self.tmp, "staging.zarr"), keep,
                           base_days=self.days, spatial_window_days=1,
                           wal_root=self.wal, manifest_root=self.root)
        self.assertEqual(plan["status"], "ok", plan.get("reason"))
        return plan

    def test_a_NEWER_repair_after_the_plan_aborts_the_swap(self):
        """The day set is identical, so the staleness guard passes. Only the re-run gate can
        see that base no longer carries the latest correction."""
        plan = self._plan_dropping_repaired()
        rid2 = self._open_repair()
        self._apply_repair(bump=11.0)
        self._commit_repair(rid2)
        before = sorted(bm.inspect_store_contract(self.delta).days)

        out = _swap(plan, mode="s2",
                                hold_dir=os.path.join(self.tmp, "hold"),
                                wal_root=self.wal, manifest_root=self.root,
                                compaction_lock_path=self.clock)
        self.assertEqual(out["status"], "aborted_stale")
        self.assertFalse(out["swap_performed"])
        self.assertIn(self.repaired, out["corrected_day_refused"])
        self.assertIn("day set is unchanged", out["reason"])
        self.assertEqual(sorted(bm.inspect_store_contract(self.delta).days), before,
                         "the live delta must be untouched")

    def test_the_day_set_really_is_unchanged_so_the_staleness_guard_cannot_see_it(self):
        """Precondition for the test above: if the repair changed the day set, the abort would
        prove nothing about the corrected-day gate."""
        self._plan_dropping_repaired()
        before = sorted(bm.inspect_store_contract(self.delta).days)
        rid2 = self._open_repair()
        self._apply_repair(bump=11.0)
        self._commit_repair(rid2)
        self.assertEqual(sorted(bm.inspect_store_contract(self.delta).days), before)

    def test_swapping_away_days_REQUIRES_the_roots(self):
        plan = self._plan_dropping_repaired()
        out = _swap(plan, mode="s2", hold_dir=os.path.join(self.tmp, "hold"),
                                compaction_lock_path=self.clock)
        self.assertEqual(out["status"], "refused")
        self.assertIn("REQUIRED to swap away days", out["reason"])
        self.assertFalse(out["swap_performed"])

    def test_an_unchanged_authorization_still_swaps(self):
        """The re-run gate must not refuse the legitimate case."""
        plan = self._plan_dropping_repaired()
        out = _swap(plan, mode="s2", hold_dir=os.path.join(self.tmp, "hold"),
                                wal_root=self.wal, manifest_root=self.root,
                                compaction_lock_path=self.clock)
        self.assertEqual(out["status"], "swapped", out.get("reason"))
        self.assertNotIn(self.repaired, bm.inspect_store_contract(self.delta).days)


class TestWalDeploymentIdentity(_Base):
    """An ABSENT log is not an EMPTY one. Read as "no repairs" it authorizes every day, and
    that failure is silent, total, and indistinguishable from a deployment that has genuinely
    had none."""

    def test_a_missing_WAL_authorizes_nothing(self):
        empty = os.path.join(self.tmp, "no_wal")
        os.makedirs(empty, exist_ok=True)
        out = _eligibility(self.days, manifest_root=self.root,
                                   delta_path=self.delta, wal_root=empty)
        self.assertEqual(out["authorized"], [])
        self.assertEqual(out["wal"], "untrustworthy")
        self.assertIn("no wal_initialized record", out["refused"][self.days[0]])

    def test_an_UNINITIALIZED_but_present_WAL_authorizes_nothing(self):
        root = os.path.join(self.tmp, "wal_unbound")
        os.makedirs(root, exist_ok=True)
        with open(os.path.join(root, rw.WAL_NAME), "w") as fh:
            fh.write("")
        out = _eligibility(self.days, manifest_root=self.root,
                                   delta_path=self.delta, wal_root=root)
        self.assertEqual(out["authorized"], [])

    def test_a_WAL_bound_to_ANOTHER_deployment_authorizes_nothing(self):
        other_root = os.path.join(self.tmp, "other_manifest")
        os.makedirs(other_root, exist_ok=True)
        shutil.copy(os.path.join(self.root, bm.LIVE_NAME),
                    os.path.join(other_root, bm.LIVE_NAME))
        other_wal = os.path.join(self.tmp, "other_wal")
        os.makedirs(other_wal, exist_ok=True)
        rw.initialize_wal(other_wal, manifest_root=other_root, at_utc="t", operator="ops",
                          unsafe_allow_colocated_anchor=True)
        out = _eligibility(self.days, manifest_root=self.root,
                                   delta_path=self.delta, wal_root=other_wal)
        self.assertEqual(out["authorized"], [])
        self.assertIn("different deployment", out["refused"][self.days[0]])

    def test_an_unbound_WAL_can_never_be_bound_retroactively(self):
        """Appending to an unbound log is allowed; what is refused is ever *trusting* it.
        `initialize_wal` will not bind a log that already carries records, so those records can
        never authorize anything -- the fail-closed property lives at authorization time."""
        root = os.path.join(self.tmp, "wal_unbound2")
        os.makedirs(root, exist_ok=True)
        rw.open_repair(root, day=self.repaired, at_utc="t", operator="ops",
                       unsafe_allow_colocated_anchor=True)
        with self.assertRaises(rw.WalError) as cm:
            rw.initialize_wal(root, manifest_root=self.root, at_utc="t", operator="ops",
                              unsafe_allow_colocated_anchor=True)
        self.assertIn("cannot be retroactively bound", str(cm.exception))
        out = _eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=root)
        self.assertEqual(out["authorized"], [])

    def test_initialization_is_idempotent_and_rebinding_is_refused(self):
        again = rw.initialize_wal(self.wal, manifest_root=self.root, at_utc="t",
                                  operator="ops", unsafe_allow_colocated_anchor=True)
        self.assertEqual(again["status"], "already_initialized")
        other = os.path.join(self.tmp, "other2")
        os.makedirs(other, exist_ok=True)
        shutil.copy(os.path.join(self.root, bm.LIVE_NAME), os.path.join(other, bm.LIVE_NAME))
        with self.assertRaises(rw.WalError) as cm:
            rw.initialize_wal(self.wal, manifest_root=other, at_utc="t", operator="ops",
                              unsafe_allow_colocated_anchor=True)
        self.assertIn("would transfer its authorizations", str(cm.exception))

    def test_the_authority_survives_publishing_a_new_generation(self):
        """`generation_id` changes on every publish and cannot be the binding; the grid, the
        block calendar and the resolved root do not."""
        before = rw.read_wal(self.wal, anchor_root=self.wal).authority
        self._repair_and_refold()
        rw.read_wal(self.wal, anchor_root=self.wal).assert_bound_to(self.root)
        self.assertEqual(rw.read_wal(self.wal, anchor_root=self.wal).authority, before)

    def test_a_wal_initialized_record_may_only_be_first(self):
        with self.assertRaises(rw.WalError):
            _wal_append(self.wal, record=rw.WAL_INITIALIZED, repair_id="wal-0", day="",
                      at_utc="t", operator="ops", payload={"authority": {}})


class TestTheGateChecksTheLiveDelta(_Base):
    def test_a_separate_source_delta_refuses_when_dropping_days(self):
        """The day whose correction is being authorized away lives in the delta that will be
        SWAPPED. Checking some other copy authorizes the wrong bytes."""
        other = os.path.join(self.tmp, "other_delta.zarr")
        fx.build_delta(other, self.days)
        keep = [d for d in self.days if d != self.repaired]
        out = _prune(self.delta, os.path.join(self.tmp, "staging.zarr"), keep,
                          base_days=self.days, spatial_window_days=1,
                          source_delta=other, wal_root=self.wal, manifest_root=self.root)
        self.assertEqual(out["status"], "refused")
        self.assertIn("differs from the live delta being swapped", out["reason"])


# ==================================================================== §7.8 orchestration, A→B→C
class TestCorrectiveRefoldOrchestration(_Base):
    """One entry point for the sequence, and the sequence proven through the REAL paths:
    §7.4 publication, a live delta swap, and a post-swap read."""

    def setUp(self):
        super().setUp()
        # the orchestrator publishes through the real §7.4 path, which re-composes generation
        # N+1 from the live manifest, so the live one must be a real published generation
        self.compaction_lock = os.path.join(self.tmp, "p5_compaction.lock")

    def _refold_via_entry_point(self, name="b_v2"):
        return _refold_entry(
            day=self.repaired, manifest_root=self.root, delta_path=self.delta,
            wal_root=self.wal, new_block_path=os.path.join(self.root, name + ".zarr"),
            artifacts_dir=self.artifacts, start_day=self.s0, end_day=self.e0,
            classification_target=list(self.days),
            predecessor_segment_id=self.v1_id, ingest_lock_path=self.lock,
            compaction_lock_path=self.compaction_lock, hard_reserve_bytes=1 << 20,
            unsafe_skip_isolation=True, operator="ops")

    def test_the_entry_point_runs_build_publish_and_phase_A_in_order(self):
        rid = self._open_repair()
        self._apply_repair()
        fp = self._commit_repair(rid)
        out = self._refold_via_entry_point()
        self.assertEqual(out["status"], "refolded")
        self.assertTrue(out["publication"]["published"])
        self.assertTrue(out["publication"]["provenance"]["verified"],
                        "the refold publishes through the real §7.4 provenance verification")
        self.assertEqual(out["phase_a"]["repair_id"], rid)
        self.assertEqual(out["phase_a"]["fingerprint"], fp)
        self.assertFalse(out["pruned"], "the entry point must not prune")

    def test_it_refuses_to_publish_a_refold_that_materialized_nothing(self):
        """A refold that supersedes the sealed version while carrying no correction is worse
        than no refold: it looks like the repair landed."""
        with self.assertRaises(cd.CorrectedDayRefused) as cm:
            self._refold_via_entry_point()          # no repair committed at all
        self.assertIn("did not materialize a repair", str(cm.exception))
        self.assertEqual(bm.load_live(self.root)["generation"], 1,
                         "nothing may publish")

    def test_a_running_compaction_blocks_the_refold_and_leaves_the_sealed_version(self):
        from store.compaction_lock import CompactionLock
        rid = self._open_repair()
        self._apply_repair()
        self._commit_repair(rid)
        with CompactionLock(self.compaction_lock):
            with self.assertRaises(cd.CorrectedDayRefused) as cm:
                self._refold_via_entry_point()
        self.assertIn("did not publish", str(cm.exception))
        self.assertEqual(bm.load_live(self.root)["generation"], 1)
        self.assertTrue(os.path.isdir(self.v1["out_path"]))

    def test_PHASE_A_then_B_then_C_through_the_real_swap(self):
        """The whole ordering, end to end, with no shortcuts:

        repair → refold (real §7.4 publication) → PHASE A base-only
              → PHASE B prune + real live delta swap
              → PHASE C the day is gone from the live delta and base still serves the
                correction, necessarily from the corrected block.
        """
        rid = self._open_repair()
        self._apply_repair()
        fp = self._commit_repair(rid)

        refold = self._refold_via_entry_point()
        self.assertEqual(refold["phase_a"]["fingerprint"], fp)
        corrected_id = refold["segment_id"]

        base_before = SegmentedCubeStore(self.root).point_series(
            100.0, 0.0, [self.repaired], ["sst"])[0]
        # Pre-prune, the public view answers from DELTA -- recorded to show the two agree only
        # because the refold worked, and that Phase C is not merely re-reading delta.
        public_before = TieredCube(SegmentedCubeStore(self.root),
                                   TimeCubeStore(self.delta)).point_series(
                                       100.0, 0.0, [self.repaired], ["sst"])[0]
        self.assertEqual(public_before["sst"], base_before["sst"])

        # PHASE B — plan, then the REAL swap with the gate re-run under the ingest lock
        keep = [d for d in self.days if d != self.repaired]
        plan = _prune(self.delta, os.path.join(self.tmp, "staging.zarr"), keep,
                           base_days=self.days, spatial_window_days=1,
                           wal_root=self.wal, manifest_root=self.root)
        self.assertEqual(plan["status"], "ok", plan.get("reason"))
        swap = _swap(plan, mode="s2", hold_dir=os.path.join(self.tmp, "hold"),
                                 wal_root=self.wal, manifest_root=self.root,
                                 compaction_lock_path=self.compaction_lock)
        self.assertEqual(swap["status"], "swapped", swap.get("reason"))

        # PHASE C — the LIVE delta no longer holds the day; the PUBLIC view still serves the
        # correction, and now necessarily from the corrected block.
        live_days = list(bm.inspect_store_contract(self.delta).days)
        self.assertNotIn(self.repaired, live_days)
        store = SegmentedCubeStore(self.root)
        seg_idx, _ = store.resolve(self.repaired)
        self.assertEqual(store.segment_id(seg_idx), corrected_id,
                         "the corrected version must be the one serving it")
        after = store.point_series(100.0, 0.0, [self.repaired], ["sst"])[0]
        self.assertEqual(after, base_before,
                         "the corrected value must survive the prune and the swap unchanged")

        # ...and through the PUBLIC tiered view, which is what a user actually gets. Pre-prune
        # this read proved nothing (delta-wins); post-prune it is the end-to-end assertion,
        # because delta no longer has the day to answer with.
        public = TieredCube(SegmentedCubeStore(self.root),
                            TimeCubeStore(self.delta)).point_series(
                                100.0, 0.0, [self.repaired], ["sst"])
        self.assertEqual(len(public), 1, "the public view must still serve the day")
        self.assertEqual(public[0], base_before,
                         "the public read must return the corrected value, from the block")

        # and Phase A is now correctly unavailable: it is a PRE-prune check
        with self.assertRaises(cd.CorrectedDayRefused):
            _phase_a(self.repaired, manifest_root=self.root,
                                 delta_path=self.delta, wal_root=self.wal)

    def test_the_sealed_predecessor_is_untouched_and_superseded(self):
        rid = self._open_repair()
        self._apply_repair()
        self._commit_repair(rid)
        before = bm.metadata_fingerprint_from_inspection(
            bm.inspect_store_contract(self.v1["out_path"]))
        out = self._refold_via_entry_point()
        live = bm.load_live(self.root)
        self.assertEqual([s["segment_id"] for s in live["segments"]], [out["segment_id"]])
        self.assertEqual([e["segment_id"] for e in live["superseded"]], [self.v1_id])
        self.assertEqual(
            bm.metadata_fingerprint_from_inspection(
                bm.inspect_store_contract(self.v1["out_path"])), before)


# ============ review round 3: one reservation, swap reservation, WAL freshness, public Phase C
class TestOneReservationSpansTheRefold(_Base):
    """Letting publication take its own lock would mean releasing between build and publish —
    and that gap is exactly when a concurrent build could start rewriting the block the refold
    is about to reference."""

    def setUp(self):
        super().setUp()
        self.compaction_lock = os.path.join(self.tmp, "p5_compaction.lock")
        rid = self._open_repair()
        self._apply_repair()
        self.fp = self._commit_repair(rid)
        self.rid = rid

    def test_the_happy_path_uses_NO_unsafe_waiver(self):
        """Real compaction reservation, real build isolation, real positive disk reserve, real
        provenance verification. If any of those only worked with a waiver, this fails."""
        out = _refold_entry(
            day=self.repaired, manifest_root=self.root, delta_path=self.delta,
            wal_root=self.wal, new_block_path=os.path.join(self.root, "b_v2.zarr"),
            artifacts_dir=self.artifacts, start_day=self.s0, end_day=self.e0,
            classification_target=list(self.days),
            predecessor_segment_id=self.v1_id, ingest_lock_path=self.lock,
            compaction_lock_path=self.compaction_lock, hard_reserve_bytes=1 << 20,
            operator="ops")
        self.assertEqual(out["status"], "refolded")
        self.assertTrue(out["publication"]["published"])
        self.assertTrue(out["publication"]["provenance"]["verified"])
        self.assertEqual(out["phase_a"]["repair_id"], self.rid)

    def test_publication_BORROWS_the_lock_and_does_not_release_it(self):
        """Asserted from inside: while publication runs, the reservation is unavailable to a
        third party, and it is still held when Phase A runs."""
        from store.compaction_lock import CompactionLock, CompactionLockBusy
        observed = {}
        real_publish = bm.publish

        def probing(*a, **kw):
            try:
                CompactionLock(self.compaction_lock).acquire().release()
                observed["busy_during_commit"] = False
            except CompactionLockBusy:
                observed["busy_during_commit"] = True
            return real_publish(*a, **kw)

        real_phase_a = cd.phase_a_base_only

        def probing_phase_a(*a, **kw):
            try:
                CompactionLock(self.compaction_lock).acquire().release()
                observed["busy_during_phase_a"] = False
            except CompactionLockBusy:
                observed["busy_during_phase_a"] = True
            return real_phase_a(*a, **kw)

        bm.publish, cd.phase_a_base_only = probing, probing_phase_a
        try:
            _refold_entry(
                day=self.repaired, manifest_root=self.root, delta_path=self.delta,
                wal_root=self.wal, new_block_path=os.path.join(self.root, "b_v2.zarr"),
                artifacts_dir=self.artifacts, start_day=self.s0, end_day=self.e0,
                classification_target=list(self.days),
                predecessor_segment_id=self.v1_id, ingest_lock_path=self.lock,
                compaction_lock_path=self.compaction_lock, hard_reserve_bytes=1 << 20)
        finally:
            bm.publish, cd.phase_a_base_only = real_publish, real_phase_a
        self.assertTrue(observed.get("busy_during_commit"),
                        "the reservation must be held through the commit")
        self.assertTrue(observed.get("busy_during_phase_a"),
                        "and still held through Phase A -- one reservation, no gap")

    def test_the_lock_fence_is_re_asserted_before_phase_A(self):
        """Holding the fd is not the same as still owning the reservation: `rm` + recreate gives
        a new inode another process can lock freely while we hold the orphaned one. The refold
        re-asserts between publish and Phase A, so a fence broken in that window refuses."""
        from store.compaction_lock import CompactionLockError
        real_publish = bm.publish

        def breaking(*a, **kw):
            out = real_publish(*a, **kw)
            os.remove(self.compaction_lock)         # orphan our inode, mid-sequence
            open(self.compaction_lock, "w").close()
            return out

        bm.publish = breaking
        try:
            with self.assertRaises(CompactionLockError):
                _refold_entry(
                    day=self.repaired, manifest_root=self.root, delta_path=self.delta,
                    wal_root=self.wal,
                    new_block_path=os.path.join(self.root, "b_v2.zarr"),
                    artifacts_dir=self.artifacts, start_day=self.s0, end_day=self.e0,
                    classification_target=list(self.days),
                    predecessor_segment_id=self.v1_id, ingest_lock_path=self.lock,
                    compaction_lock_path=self.compaction_lock, hard_reserve_bytes=1 << 20)
        finally:
            bm.publish = real_publish

    def test_publication_refuses_a_released_lock(self):
        from store.compaction_lock import CompactionLock
        lock = CompactionLock(self.compaction_lock).acquire()
        lock.release()
        with self.assertRaises(pub.PublishRefused) as cm:
            pub.execute_publication({"root": self.root, "manifest": {}, "new_segment_id": "x",
                                     "predecessor_generation": 1},
                                    ingest_lock_path=self.lock, compaction_lock=lock,
                                    compaction_lock_path=self.compaction_lock,
                                    build_artifact_path=None, unsafe_skip_provenance=True)
        self.assertIn("is not held", str(cm.exception))

    def test_a_borrowed_lock_must_be_the_CANONICAL_one(self):
        """Holding *some* lock satisfies "a lock is held" while the real reservation sits free
        for a build to take -- the guarantee reads as satisfied and protects nothing."""
        from store.compaction_lock import CompactionLock
        unrelated = os.path.join(self.tmp, "unrelated.lock")
        with CompactionLock(unrelated) as lock:
            with self.assertRaises(pub.PublishRefused) as cm:
                pub.execute_publication({"root": self.root, "manifest": {}, "new_segment_id": "x",
                                         "predecessor_generation": 1},
                                        ingest_lock_path=self.lock, compaction_lock=lock,
                                        compaction_lock_path=self.compaction_lock,
                                        build_artifact_path=None, unsafe_skip_provenance=True)
            self.assertIn("canonical reservation", str(cm.exception))

    def test_a_borrowed_lock_without_a_canonical_path_is_refused(self):
        from store.compaction_lock import CompactionLock
        with CompactionLock(self.compaction_lock) as lock:
            with self.assertRaises(pub.PublishRefused) as cm:
                pub.execute_publication({"root": self.root, "manifest": {}, "new_segment_id": "x",
                                         "predecessor_generation": 1},
                                        ingest_lock_path=self.lock, compaction_lock=lock,
                                        build_artifact_path=None, unsafe_skip_provenance=True)
            self.assertIn("nothing to bind the lock's identity to", str(cm.exception))

    def test_the_refold_refuses_an_unrelated_held_lock(self):
        """The reviewer's case: hold the canonical lock, pass a different one, and the refold
        published anyway."""
        from store.compaction_lock import CompactionLock
        unrelated = os.path.join(self.tmp, "unrelated.lock")
        with CompactionLock(self.compaction_lock), CompactionLock(unrelated) as other:
            with self.assertRaises(cd.CorrectedDayRefused) as cm:
                _refold_entry(
                    day=self.repaired, manifest_root=self.root, delta_path=self.delta,
                    wal_root=self.wal,
                    new_block_path=os.path.join(self.root, "b_v2.zarr"),
                    artifacts_dir=self.artifacts, start_day=self.s0, end_day=self.e0,
                    classification_target=list(self.days),
                    predecessor_segment_id=self.v1_id, ingest_lock_path=self.lock,
                    compaction_lock_path=self.compaction_lock, lock=other,
                    hard_reserve_bytes=1 << 20)
            self.assertIn("leaves the real one free", str(cm.exception))
        self.assertEqual(bm.load_live(self.root)["generation"], 1)


class TestSwapHoldsAReservation(_Base):
    def _plan(self):
        self._repair_and_refold()
        keep = [d for d in self.days if d != self.repaired]
        plan = _prune(self.delta, os.path.join(self.tmp, "staging.zarr"), keep,
                           base_days=self.days, spatial_window_days=1,
                           wal_root=self.wal, manifest_root=self.root)
        self.assertEqual(plan["status"], "ok", plan.get("reason"))
        return plan

    def test_the_lock_is_unavailable_DURING_the_path_switch(self):
        """A probe answers "was a build running a moment ago". Between that answer and the
        rename pair, a build can take the lock and start reading the delta whose path is about
        to move. Probed from inside the swap, so there is no timing to get wrong."""
        from store.compaction_lock import CompactionLock, CompactionLockBusy
        import ingest.swap_delta as sd
        clock = os.path.join(self.tmp, "p5_compaction.lock")
        plan = self._plan()
        observed = {}
        real_rename = sd.os.rename

        def probing(src, dst):
            if "busy_at_switch" not in observed:
                try:
                    CompactionLock(clock).acquire().release()
                    observed["busy_at_switch"] = False
                except CompactionLockBusy:
                    observed["busy_at_switch"] = True
            return real_rename(src, dst)

        sd.os.rename = probing
        try:
            out = _swap(plan, mode="s2",
                                    hold_dir=os.path.join(self.tmp, "hold"),
                                    wal_root=self.wal, manifest_root=self.root,
                                    compaction_lock_path=clock)
        finally:
            sd.os.rename = real_rename
        self.assertEqual(out["status"], "swapped", out.get("reason"))
        self.assertTrue(observed.get("busy_at_switch"),
                        "a build could have taken the lock at the moment of the switch")

    def test_a_running_build_refuses_the_swap_without_waiting(self):
        from store.compaction_lock import CompactionLock
        clock = os.path.join(self.tmp, "p5_compaction.lock")
        plan = self._plan()
        before = sorted(bm.inspect_store_contract(self.delta).days)
        with CompactionLock(clock, holder="a-build"):
            out = _swap(plan, mode="s2",
                                    hold_dir=os.path.join(self.tmp, "hold"),
                                    wal_root=self.wal, manifest_root=self.root,
                                    compaction_lock_path=clock)
        self.assertEqual(out["reason"], "compaction_lock_held")
        self.assertFalse(out["swap_performed"])
        self.assertEqual(sorted(bm.inspect_store_contract(self.delta).days), before)


class TestWalFreshnessAnchor(_Base):
    """A valid PREFIX of a log is still a valid log: checksums chain, `seq` is gap-free, the
    state machine is coherent. The days repaired in the lost tail simply read as never-repaired.
    Nothing inside a self-describing log can detect its own truncation."""

    def test_a_rolled_back_WAL_prefix_authorizes_nothing(self):
        self._repair_and_refold()
        wal_file = os.path.join(self.wal, rw.WAL_NAME)
        with open(wal_file) as fh:
            lines = fh.read().splitlines()
        self.assertGreater(len(lines), 2)
        with open(wal_file, "w") as fh:              # roll back to a valid earlier prefix
            fh.write("\n".join(lines[:2]) + "\n")
        rolled = rw.parse_wal(wal_file)
        self.assertIsNotNone(rolled.authority, "precondition: the prefix is still a VALID log")
        self.assertIsNone(rolled.latest_committed(self.repaired),
                          "precondition: the repair now reads as never having happened")

        out = _eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [])
        self.assertIn("must equal the head", out["refused"][self.repaired])

    def test_an_anchor_that_LAGS_the_head_is_refused(self):
        """`seq` present in the log is not enough: an anchor that lags leaves the tail
        unattested, which is indistinguishable from a rollback."""
        self._repair_and_refold()
        state = rw.read_wal(self.wal, anchor_root=self.wal)
        self.assertGreater(state.last_seq, 1)
        first = state.records[0]
        with open(os.path.join(self.wal, rw.ANCHOR_NAME), "w") as fh:
            fh.write(rw.canonical({"seq": 1, "record_checksum": first["record_checksum"],
                                   "at_utc": "t"}))
        out = _eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [])
        self.assertIn("must equal the head", out["refused"][self.repaired])

    def test_a_WHOLE_ROOT_rollback_is_caught_only_by_a_SEPARATE_anchor_domain(self):
        """A co-located sidecar cannot see a VM snapshot rollback: log and anchor come back
        together and the restored pair is perfectly self-consistent. This pins both halves of
        that -- the co-located anchor misses it, a separate rollback domain catches it -- so the
        limitation is documented by a test rather than only by prose."""
        anchor_root = os.path.join(self.tmp, "anchor_domain")
        os.makedirs(anchor_root, exist_ok=True)
        # both domains start in step, so the divergence below is the rollback and nothing else
        rw._write_anchor(anchor_root, rw.read_wal(self.wal, anchor_root=self.wal).records[-1])
        rw.read_wal(self.wal, anchor_root=anchor_root).assert_fresh()
        snapshot = os.path.join(self.tmp, "wal_snapshot")
        shutil.copytree(self.wal, snapshot)         # the "VM snapshot", log + sidecar together

        rid = self._open_repair()                   # advance both anchors
        self._apply_repair()
        self._commit_repair(rid)
        rw._write_anchor(anchor_root, rw.read_wal(self.wal, anchor_root=self.wal).records[-1])

        shutil.rmtree(self.wal)                     # roll the WHOLE root back
        shutil.copytree(snapshot, self.wal)

        co_located = rw.read_wal(self.wal, anchor_root=self.wal)
        co_located.assert_fresh()                   # the restored pair is self-consistent

        with self.assertRaises(rw.WalRolledBack) as cm:
            rw.read_wal(self.wal, anchor_root=anchor_root).assert_fresh()
        self.assertIn("must equal the head", str(cm.exception))

    def test_a_substituted_record_at_the_anchor_seq_is_refused(self):
        self._repair_and_refold()
        state = rw.read_wal(self.wal, anchor_root=self.wal)
        anchor = state.anchor
        self.assertIsNotNone(anchor)
        with open(os.path.join(self.wal, rw.ANCHOR_NAME), "w") as fh:
            fh.write(rw.canonical({"seq": anchor["seq"], "record_checksum": "0" * 64,
                                   "at_utc": anchor["at_utc"]}))
        out = _eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [])
        self.assertIn("does not contain the record the durable anchor names",
                      out["refused"][self.repaired])

    def test_records_with_NO_anchor_at_all_authorize_nothing(self):
        self._repair_and_refold()
        os.remove(os.path.join(self.wal, rw.ANCHOR_NAME))
        out = _eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [])
        self.assertIn("no durable anchor", out["refused"][self.repaired])

    def test_the_anchor_is_monotonic(self):
        self._repair_and_refold()                   # advance the log well past seq 1
        state = rw.read_wal(self.wal, anchor_root=self.wal)
        head = state.anchor["seq"]
        self.assertGreater(head, 1, "precondition: the anchor is past the initial record")
        rw._write_anchor(self.wal, {"seq": 1, "record_checksum": "x", "at_utc": "t"})
        self.assertEqual(rw.read_wal(self.wal, anchor_root=self.wal).anchor["seq"], head,
                         "a stale write must not un-anchor a log that has gone further")

    def test_a_healthy_log_passes_freshness(self):
        self._repair_and_refold()
        rw.read_wal(self.wal, anchor_root=self.wal).assert_fresh()
        out = _eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [self.repaired])


# ============================= review round 4: E2 for every day, and the swap lock is required
class TestE2RunsForEveryDroppedDay(_Base):
    """A day with no committed repair is not a day that was never edited — it is a day the WAL
    has no opinion about, and a repair applied straight to delta leaves exactly that trace.
    Skipping E2 there made the one case E2 is documented to catch — "a repair that bypassed the
    WAL" — the one case it could not see."""

    def test_a_bypass_WAL_repair_is_refused(self):
        """The reviewer's case: rewrite the delta day, write nothing to the WAL."""
        self._apply_repair(bump=17.0)               # no intent, no commit -- straight to delta
        state = rw.read_wal(self.wal, anchor_root=self.wal)
        self.assertIsNone(state.latest_committed(self.repaired),
                          "precondition: the WAL knows nothing about this day")
        out = _eligibility([self.repaired], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [])
        self.assertIn("no committed repair explains", out["refused"][self.repaired])

    def test_prune_delta_refuses_a_bypass_WAL_repair(self):
        self._apply_repair(bump=17.0)
        keep = [d for d in self.days if d != self.repaired]
        out = _prune(self.delta, os.path.join(self.tmp, "staging.zarr"), keep,
                          base_days=self.days, spatial_window_days=1,
                          wal_root=self.wal, manifest_root=self.root)
        self.assertEqual(out["status"], "refused")
        self.assertEqual(out["corrected_day_refused"], [self.repaired])

    def test_an_untouched_day_still_authorizes(self):
        """E2 on every day must not refuse the ordinary case."""
        out = _eligibility([self.days[0]], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [self.days[0]])

    def test_a_base_defect_on_an_unrepaired_day_is_refused(self):
        """The other half of what E2 exists for: base is wrong and no repair explains it."""
        live = bm.load_live(self.root)
        block = os.path.join(self.root, live["segments"][0]["path"])
        idx = list(bm.inspect_store_contract(block).days).index(self.days[0])
        slot = cd.date_slot(self.days[0])
        i0, i1, j0, j1 = sp.sample_window(32, 32, seed=sp.SAMPLE_SEED, day_index=slot)
        g = zarr.open_group(block, mode="a")
        g["sst"][idx, i0:i1, j0:j1] = np.asarray(g["sst"][idx, i0:i1, j0:j1]) + 4.0
        out = _eligibility([self.days[0]], manifest_root=self.root,
                                   delta_path=self.delta, wal_root=self.wal)
        self.assertEqual(out["authorized"], [])
        self.assertIn("base is defective", out["refused"][self.days[0]])


class TestSwapRequiresTheCompactionLock(_Base):
    def _plan(self):
        self._repair_and_refold()
        keep = [d for d in self.days if d != self.repaired]
        plan = _prune(self.delta, os.path.join(self.tmp, "staging.zarr"), keep,
                           base_days=self.days, spatial_window_days=1,
                           wal_root=self.wal, manifest_root=self.root)
        self.assertEqual(plan["status"], "ok", plan.get("reason"))
        return plan

    def test_omitting_the_lock_path_refuses(self):
        """It defaulted to None and only reserved when supplied, so a caller who omitted it
        swapped with no reservation at all -- while a build held the real lock and was reading
        that delta."""
        plan = self._plan()
        before = sorted(bm.inspect_store_contract(self.delta).days)
        out = _swap(plan, mode="s2", hold_dir=os.path.join(self.tmp, "hold"),
                                wal_root=self.wal, manifest_root=self.root)
        self.assertEqual(out["status"], "refused")
        self.assertIn("compaction_lock_path is required", out["reason"])
        self.assertFalse(out["swap_performed"])
        self.assertEqual(sorted(bm.inspect_store_contract(self.delta).days), before)

    def test_a_build_holding_the_lock_blocks_a_swap_that_omitted_it(self):
        """The reviewer's measurement: another process held the canonical lock and the swap
        succeeded anyway because the caller had not passed the path."""
        from store.compaction_lock import CompactionLock
        plan = self._plan()
        before = sorted(bm.inspect_store_contract(self.delta).days)
        with CompactionLock(self.clock, holder="a-build"):
            out = _swap(plan, mode="s2",
                                    hold_dir=os.path.join(self.tmp, "hold"),
                                    wal_root=self.wal, manifest_root=self.root)
        self.assertFalse(out["swap_performed"])
        self.assertEqual(sorted(bm.inspect_store_contract(self.delta).days), before)


# ==================== review round 5: the external anchor domain, through the REAL workflow
class TestExternalAnchorWorkflow(unittest.TestCase):
    """VM24 has taken two whole-VM snapshot rollbacks. A co-located anchor cannot see one — the
    rollback restores log and sidecar together and the pair is self-consistent — so the
    protection only exists if the **whole workflow** uses an anchor in a different rollback
    domain. Nothing here calls `_write_anchor()`: every anchor advance comes from the real
    initialize → intent → commit → refold → plan → swap path."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "manifest_root")
        self.wal = os.path.join(self.tmp, "wal")
        self.anchor = os.path.join(self.tmp, "anchor_domain")
        for d in (self.root, self.wal, self.anchor):
            os.makedirs(d, exist_ok=True)
        self.lock = os.path.join(self.tmp, "ingest.lock")
        self.clock = os.path.join(self.tmp, "p5_compaction.lock")
        self.artifacts = os.path.join(self.tmp, "artifacts")
        os.makedirs(self.artifacts, exist_ok=True)
        self.s0, self.e0 = bm.block_bounds(ANCHOR, S, 0)
        self.span = fx.calendar_span(self.s0, self.e0)
        self.days = self.span[:8]
        self.repaired = self.days[3]
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.days)

        self.v1 = build_block(os.path.join(self.root, "b_v1.zarr"),
                              start_day=self.s0, end_day=self.e0,
                              classification_target=list(self.days), delta_path=self.delta,
                              artifacts_dir=self.artifacts, lock=None, hard_reserve_bytes=0,
                              unsafe_skip_isolation=True)
        self.v1_id = self.v1["segment"]["segment_id"]
        self._publish(self.v1, generation=1)
        # A temp dir cannot be a different rollback domain, so the declaration says so out
        # loud: this suite proves the MECHANISM (the workflow uses the external anchor and
        # refuses when it disagrees), not that the anchor survives a VM snapshot. That
        # remains a deployment prerequisite -- see the results doc.
        rw.declare_anchor_domain(self.anchor, domain_id="p5s5-part2-test-domain",
                                 at_utc="t", operator="tests",
                                 allow_same_filesystem=True,
                                 note="unit-test domain; NOT a real rollback domain")
        rw.initialize_wal(self.wal, manifest_root=self.root, at_utc="t", operator="ops",
                          anchor_root=self.anchor)

    def _publish(self, plan, *, generation):
        seg = dict(plan["segment"])
        m = {"format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION,
             "generation": generation, "generation_id": f"gen{generation:06d}-x",
             "created_utc": "2027-01-01T00:00:00Z", "created_by": "p5s5p2-anchor",
             "predecessor_generation": generation - 1 if generation > 1 else None,
             "predecessor_manifest": (bm.archive_name(generation - 1)
                                      if generation > 1 else None),
             "grid": {"ny": 32, "nx": 32, "region": [0, 32, 0, 32]},
             "variables": list(fx.VARS),
             "block_grid": {"anchor_day": ANCHOR, "block_days": S},
             "segments": [seg], "superseded": [], "manifest_checksum": ""}
        m["manifest_checksum"] = bm.compute_checksum(m)
        bm.publish(self.root, m)

    def _repair(self):
        rec = rw.open_repair(self.wal, day=self.repaired, at_utc="t", operator="ops",
                             payload={}, anchor_root=self.anchor)
        idx = list(bm.inspect_store_contract(self.delta).days).index(self.repaired)
        g = zarr.open_group(self.delta, mode="a")
        g["sst"][idx] = np.asarray(g["sst"][idx]) + 21.0
        fp, _v, _t = cd.fingerprint_day(self.delta, self.repaired, physical_index=idx)
        _wal_append(self.wal, record=rw.COMMITTED, repair_id=rec["repair_id"],
                  day=self.repaired, at_utc="t", operator="ops",
                  payload={"fingerprint": fp}, anchor_root=self.anchor)
        return rec["repair_id"], fp

    def _refold(self):
        return cd.corrective_refold(
            day=self.repaired, manifest_root=self.root, delta_path=self.delta,
            wal_root=self.wal, anchor_root=self.anchor,
            new_block_path=os.path.join(self.root, "b_v2.zarr"),
            artifacts_dir=self.artifacts, start_day=self.s0, end_day=self.e0,
            classification_target=list(self.days), predecessor_segment_id=self.v1_id,
            ingest_lock_path=self.lock, compaction_lock_path=self.clock,
            hard_reserve_bytes=1 << 20, operator="ops")

    _stage_n = 0

    def _plan(self, **kw):
        keep = [d for d in self.days if d != self.repaired]
        kw.setdefault("anchor_root", self.anchor)
        type(self)._stage_n += 1
        staging = os.path.join(self.tmp, f"staging{type(self)._stage_n}.zarr")
        return prune_delta(self.delta, staging, keep,
                           base_days=self.days, spatial_window_days=1,
                           wal_root=self.wal, manifest_root=self.root, **kw)

    # ---- the workflow --------------------------------------------------
    def test_the_WHOLE_workflow_runs_on_the_external_anchor(self):
        rid, fp = self._repair()
        refold = self._refold()
        self.assertEqual(refold["phase_a"]["repair_id"], rid)
        self.assertEqual(refold["phase_a"]["fingerprint"], fp)

        plan = self._plan()
        self.assertEqual(plan["status"], "ok", plan.get("reason"))
        self.assertEqual(plan["anchor_root"], os.path.realpath(self.anchor))

        swap = execute_swap_plan(plan, mode="s2", hold_dir=os.path.join(self.tmp, "hold"),
                                 wal_root=self.wal, manifest_root=self.root,
                                 anchor_root=self.anchor, compaction_lock_path=self.clock)
        self.assertEqual(swap["status"], "swapped", swap.get("reason"))
        self.assertNotIn(self.repaired, bm.inspect_store_contract(self.delta).days)

        # the anchor advanced through real appends only -- no test wrote it
        state = rw.read_wal(self.wal, anchor_root=self.anchor)
        self.assertEqual(state.anchor["seq"], state.last_seq)
        self.assertGreater(state.last_seq, 2)
        self.assertFalse(os.path.isfile(os.path.join(self.wal, rw.ANCHOR_NAME)),
                         "no co-located sidecar should exist at all")

    def test_a_WAL_root_rollback_with_the_anchor_intact_refuses_plan_AND_swap(self):
        """The VM24 case, minus the VM: restore the WAL root from an earlier copy while the
        anchor — in another rollback domain — keeps the real head."""
        self._repair()
        self._refold()
        plan = self._plan()
        self.assertEqual(plan["status"], "ok", plan.get("reason"))

        snapshot = os.path.join(self.tmp, "wal_snapshot")
        shutil.copytree(self.wal, snapshot)
        rw.open_repair(self.wal, day=self.days[0], at_utc="t", operator="ops",
                       payload={}, anchor_root=self.anchor)      # advances both
        shutil.rmtree(self.wal)
        shutil.copytree(snapshot, self.wal)                      # roll the WAL root back

        again = self._plan()
        self.assertEqual(again["status"], "refused")
        self.assertIn("must equal the head", again["reason"])

        before = sorted(bm.inspect_store_contract(self.delta).days)
        swap = execute_swap_plan(plan, mode="s2", hold_dir=os.path.join(self.tmp, "hold"),
                                 wal_root=self.wal, manifest_root=self.root,
                                 anchor_root=self.anchor, compaction_lock_path=self.clock)
        self.assertEqual(swap["status"], "aborted_stale")
        self.assertFalse(swap["swap_performed"])
        self.assertEqual(sorted(bm.inspect_store_contract(self.delta).days), before)

    def test_the_REFOLD_refuses_to_attest_from_a_rolled_back_WAL(self):
        """The builder derives `materialized_repairs` from the WAL. Reading it without the
        external anchor would let a refold attest a repair from a log the prune gate will
        refuse -- evidence produced that nothing downstream can accept, and a superseded sealed
        version to show for it."""
        self._repair()
        snapshot = os.path.join(self.tmp, "wal_snap2")
        shutil.copytree(self.wal, snapshot)
        rw.open_repair(self.wal, day=self.days[0], at_utc="t", operator="ops",
                       payload={}, anchor_root=self.anchor)      # advances both
        shutil.rmtree(self.wal)
        shutil.copytree(snapshot, self.wal)                      # WAL root back, anchor ahead

        with self.assertRaises(rw.WalRolledBack) as cm:
            self._refold()
        self.assertIn("must equal the head", str(cm.exception))
        self.assertEqual(bm.load_live(self.root)["generation"], 1,
                         "nothing may publish on evidence the gate would refuse")

    def test_plan_and_swap_on_DIFFERENT_anchor_domains_is_refused(self):
        self._repair()
        self._refold()
        plan = self._plan()
        other = os.path.join(self.tmp, "other_anchor")
        os.makedirs(other, exist_ok=True)
        before = sorted(bm.inspect_store_contract(self.delta).days)
        swap = execute_swap_plan(plan, mode="s2", hold_dir=os.path.join(self.tmp, "hold"),
                                 wal_root=self.wal, manifest_root=self.root,
                                 anchor_root=other, compaction_lock_path=self.clock)
        self.assertEqual(swap["status"], "refused")
        self.assertIn("switching anchor domain between plan and swap", swap["reason"])
        self.assertEqual(sorted(bm.inspect_store_contract(self.delta).days), before)

    def test_a_swap_that_DROPS_the_anchor_domain_is_refused(self):
        self._repair()
        self._refold()
        plan = self._plan()
        swap = execute_swap_plan(plan, mode="s2", hold_dir=os.path.join(self.tmp, "hold"),
                                 wal_root=self.wal, manifest_root=self.root,
                                 compaction_lock_path=self.clock)
        self.assertEqual(swap["status"], "refused")
        self.assertIn("switching anchor domain", swap["reason"])

    # ---- fail-closed on the production path ----------------------------
    def test_omitting_the_anchor_root_fails_closed_everywhere(self):
        self._repair()
        for label, call in (
            ("initialize", lambda: rw.initialize_wal(
                os.path.join(self.tmp, "fresh_wal"), manifest_root=self.root,
                at_utc="t", operator="ops")),
            ("open_repair", lambda: rw.open_repair(
                self.wal, day=self.days[0], at_utc="t", operator="ops", payload={})),
        ):
            with self.subTest(label):
                os.makedirs(os.path.join(self.tmp, "fresh_wal"), exist_ok=True)
                with self.assertRaises(rw.WalNotInitialized) as cm:
                    call()
                self.assertIn("anchor_root is required", str(cm.exception))

        plan = self._plan(anchor_root=None)
        self.assertEqual(plan["status"], "refused")
        self.assertIn("anchor_root is required", plan["reason"])

    def test_an_anchor_root_that_IS_the_wal_root_is_refused(self):
        with self.assertRaises(rw.WalNotInitialized) as cm:
            rw.resolve_anchor_root(self.wal, self.wal)
        self.assertIn("certainly cannot detect a rollback", str(cm.exception))

    def test_a_WAL_bound_to_an_anchor_cannot_be_read_without_one(self):
        """Dropping the external anchor mid-workflow removes the protection the log was
        initialized with, so it is refused rather than silently downgraded."""
        state = rw.read_wal(self.wal, anchor_root=self.wal)
        with self.assertRaises(rw.WalNotInitialized) as cm:
            state.assert_bound_to(self.root, None)
        self.assertIn("dropping the external anchor mid-workflow", str(cm.exception))


# ================== review round 6: the terminal writer, and what "different domain" means
class TestTerminalWritersCannotBypassTheAnchor(unittest.TestCase):
    """A `repair_committed` written against a co-located anchor used to *succeed* while leaving
    the external anchor behind. Nothing was lost — but every later prune and refold then failed
    closed, and the deployment needed manual recovery. A write that reports success and strands
    the workflow is worse than one that refuses."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "manifest_root")
        self.wal = os.path.join(self.tmp, "wal")
        self.anchor = os.path.join(self.tmp, "anchor_domain")
        self.other = os.path.join(self.tmp, "other_domain")
        for d in (self.root, self.wal, self.anchor, self.other):
            os.makedirs(d, exist_ok=True)
        self.s0, self.e0 = bm.block_bounds(ANCHOR, S, 0)
        self.days = fx.calendar_span(self.s0, self.e0)[:4]
        self.day = self.days[1]
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.days)
        blk = build_block(os.path.join(self.root, "b_v1.zarr"), start_day=self.s0,
                          end_day=self.e0, classification_target=list(self.days),
                          delta_path=self.delta,
                          artifacts_dir=os.path.join(self.tmp, "art"),
                          lock=None, hard_reserve_bytes=0, unsafe_skip_isolation=True)
        seg = dict(blk["segment"])
        m = {"format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION, "generation": 1,
             "generation_id": "gen1-x", "created_utc": "2027-01-01T00:00:00Z",
             "created_by": "t", "predecessor_generation": None,
             "predecessor_manifest": None,
             "grid": {"ny": 32, "nx": 32, "region": [0, 32, 0, 32]},
             "variables": list(fx.VARS),
             "block_grid": {"anchor_day": ANCHOR, "block_days": S},
             "segments": [seg], "superseded": [], "manifest_checksum": ""}
        m["manifest_checksum"] = bm.compute_checksum(m)
        bm.publish(self.root, m)
        for d, name in ((self.anchor, "domain-A"), (self.other, "domain-B")):
            rw.declare_anchor_domain(d, domain_id=name, at_utc="t", operator="tests",
                                     allow_same_filesystem=True, note="unit-test domain")
        rw.initialize_wal(self.wal, manifest_root=self.root, at_utc="t", operator="ops",
                          anchor_root=self.anchor)
        self.rid = rw.open_repair(self.wal, day=self.day, at_utc="t", operator="ops",
                                  payload={}, anchor_root=self.anchor)["repair_id"]

    def _state(self):
        return rw.read_wal(self.wal, anchor_root=self.anchor)

    def test_a_terminal_write_with_NO_anchor_is_refused(self):
        with open(os.path.join(self.wal, rw.WAL_NAME), "rb") as fh:
            before_wal = fh.read()
        with open(os.path.join(self.anchor, rw.ANCHOR_NAME), "rb") as fh:
            before_anchor = fh.read()
        with self.assertRaises(rw.WalNotInitialized) as cm:
            rw.commit_repair(self.wal, repair_id=self.rid, day=self.day, fingerprint="fp",
                             at_utc="t", operator="ops")
        self.assertIn("anchor_root is required", str(cm.exception))
        with open(os.path.join(self.wal, rw.WAL_NAME), "rb") as fh:
            self.assertEqual(fh.read(), before_wal, "the WAL must be byte-identical")
        with open(os.path.join(self.anchor, rw.ANCHOR_NAME), "rb") as fh:
            self.assertEqual(fh.read(), before_anchor, "the anchor must be byte-identical")
        self.assertFalse(os.path.isfile(os.path.join(self.wal, rw.ANCHOR_NAME)),
                         "no co-located sidecar may appear")

    def test_a_terminal_write_against_ANOTHER_domain_is_refused(self):
        with open(os.path.join(self.wal, rw.WAL_NAME), "rb") as fh:
            before_wal = fh.read()
        with self.assertRaises(rw.WalNotInitialized) as cm:
            rw.commit_repair(self.wal, repair_id=self.rid, day=self.day, fingerprint="fp",
                             at_utc="t", operator="ops", anchor_root=self.other)
        # The canonical-path check fires first now; a different domain is also a different
        # path, so either half is a refusal and the stricter one wins.
        self.assertIn("bound to the anchor at", str(cm.exception))
        with open(os.path.join(self.wal, rw.WAL_NAME), "rb") as fh:
            self.assertEqual(fh.read(), before_wal)
        self.assertFalse(os.path.isfile(os.path.join(self.other, rw.ANCHOR_NAME)),
                         "the other domain's anchor must not be advanced at all")

    def test_the_refusal_is_BEFORE_the_write_so_nothing_is_half_advanced(self):
        head_before = self._state().last_seq
        for call in (
            lambda: rw.commit_repair(self.wal, repair_id=self.rid, day=self.day,
                                     fingerprint="fp", at_utc="t", operator="ops"),
            lambda: rw.abort_repair(self.wal, repair_id=self.rid, day=self.day,
                                    reason="x", at_utc="t", operator="ops",
                                    anchor_root=self.other),
        ):
            with self.assertRaises(rw.WalNotInitialized):
                call()
        state = self._state()
        self.assertEqual(state.last_seq, head_before)
        self.assertEqual(state.anchor["seq"], state.last_seq)

    def test_the_terminal_wrappers_work_on_the_canonical_domain(self):
        rw.commit_repair(self.wal, repair_id=self.rid, day=self.day, fingerprint="fp",
                         at_utc="t", operator="ops", anchor_root=self.anchor)
        state = self._state()
        self.assertEqual(state.repairs[self.rid].state, rw.COMMITTED)
        self.assertEqual(state.anchor["seq"], state.last_seq)

    def test_abort_closes_the_intent_on_the_canonical_domain(self):
        rw.abort_repair(self.wal, repair_id=self.rid, day=self.day, reason="did not land",
                        at_utc="t", operator="ops", anchor_root=self.anchor)
        state = self._state()
        self.assertEqual(state.repairs[self.rid].state, rw.ABORTED)
        self.assertNotIn(self.day, state.blocked_days)


class TestAnchorDomainIsDeclaredNotInferred(unittest.TestCase):
    """A different path is not a different filesystem, and a different filesystem is not a
    different rollback domain. Nothing in this process can tell them apart, so the domain is
    something an operator DECLARES and ops verify — not something the code infers."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.wal = os.path.join(self.tmp, "wal")
        self.anchor = os.path.join(self.tmp, "anchor")
        for d in (self.wal, self.anchor):
            os.makedirs(d, exist_ok=True)

    def test_an_undeclared_sibling_directory_is_refused(self):
        """The exact shape of the old test fixture: a sibling under one temp dir."""
        with self.assertRaises(rw.WalNotInitialized) as cm:
            rw.resolve_anchor_root(self.wal, self.anchor)
        msg = str(cm.exception)
        self.assertIn("declares no rollback domain", msg)
        self.assertIn("neither is a different filesystem", msg)

    def test_a_same_filesystem_domain_needs_an_explicit_acknowledgement(self):
        rw.declare_anchor_domain(self.anchor, domain_id="d", at_utc="t", operator="ops")
        with self.assertRaises(rw.WalNotInitialized) as cm:
            rw.resolve_anchor_root(self.wal, self.anchor)
        self.assertIn("same filesystem", str(cm.exception))

    def test_the_acknowledgement_is_recorded_in_the_artifact(self):
        """It belongs where an auditor reads the deployment, not at a call site."""
        rw.declare_anchor_domain(self.anchor, domain_id="d", at_utc="t", operator="ops",
                                 allow_same_filesystem=True, note="test")
        with open(os.path.join(self.anchor, rw.ANCHOR_DOMAIN_NAME)) as fh:
            doc = json.load(fh)
        self.assertTrue(doc["allow_same_filesystem"])
        self.assertEqual(doc["operator"], "ops")
        self.assertEqual(rw.resolve_anchor_root(self.wal, self.anchor),
                         os.path.realpath(self.anchor))

    def test_redeclaring_a_different_id_is_refused(self):
        rw.declare_anchor_domain(self.anchor, domain_id="d1", at_utc="t", operator="ops")
        self.assertEqual(
            rw.declare_anchor_domain(self.anchor, domain_id="d1", at_utc="t",
                                     operator="ops")["status"], "already_declared")
        with self.assertRaises(rw.WalError) as cm:
            rw.declare_anchor_domain(self.anchor, domain_id="d2", at_utc="t", operator="ops")
        self.assertIn("would silently move every log", str(cm.exception))

    def test_the_WAL_binds_to_the_domain_ID_not_the_path(self):
        """A path changes on every remount and proves nothing about durability."""
        rw.declare_anchor_domain(self.anchor, domain_id="d", at_utc="t", operator="ops",
                                 allow_same_filesystem=True)
        auth = rw.manifest_authority.__doc__
        self.assertIsNotNone(auth)
        self.assertEqual(rw.anchor_domain_id(self.anchor), "d")


# ================ review round 7: writer freshness, stat preflight, declaration strictness
class TestWriterRefusesAStaleWal(unittest.TestCase):
    """A rollback that leaves an older WAL beside an intact external anchor used to let the
    next writer re-use the missing seq and report success. Nothing was wrongly pruned — the
    gate still failed closed — but the rollback evidence was overwritten and recovery got
    harder. A writer that cannot be trusted to read the log must not be trusted to extend it."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "manifest_root")
        self.wal = os.path.join(self.tmp, "wal")
        self.anchor = os.path.join(self.tmp, "anchor")
        for d in (self.root, self.wal, self.anchor):
            os.makedirs(d, exist_ok=True)
        s0, e0 = bm.block_bounds(ANCHOR, S, 0)
        days = fx.calendar_span(s0, e0)[:3]
        delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(delta, days)
        blk = build_block(os.path.join(self.root, "b.zarr"), start_day=s0, end_day=e0,
                          classification_target=list(days), delta_path=delta,
                          artifacts_dir=os.path.join(self.tmp, "art"), lock=None,
                          hard_reserve_bytes=0, unsafe_skip_isolation=True)
        m = {"format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION, "generation": 1,
             "generation_id": "g1", "created_utc": "2027-01-01T00:00:00Z", "created_by": "t",
             "predecessor_generation": None, "predecessor_manifest": None,
             "grid": {"ny": 32, "nx": 32, "region": [0, 32, 0, 32]},
             "variables": list(fx.VARS),
             "block_grid": {"anchor_day": ANCHOR, "block_days": S},
             "segments": [dict(blk["segment"])], "superseded": [], "manifest_checksum": ""}
        m["manifest_checksum"] = bm.compute_checksum(m)
        bm.publish(self.root, m)
        rw.declare_anchor_domain(self.anchor, domain_id="d", at_utc="t", operator="ops",
                                 allow_same_filesystem=True, note="unit-test domain")
        rw.initialize_wal(self.wal, manifest_root=self.root, at_utc="t", operator="ops",
                          anchor_root=self.anchor)
        self.days = days
        self.rid = rw.open_repair(self.wal, day=days[0], at_utc="t", operator="ops",
                                  payload={}, anchor_root=self.anchor)["repair_id"]
        # snapshot the WAL root, advance it, then roll the WAL root back
        snap = os.path.join(self.tmp, "wal_snap")
        shutil.copytree(self.wal, snap)
        rw.commit_repair(self.wal, repair_id=self.rid, day=days[0], fingerprint="fp",
                         at_utc="t", operator="ops", anchor_root=self.anchor)
        shutil.rmtree(self.wal)
        shutil.copytree(snap, self.wal)

    def _bytes(self):
        with open(os.path.join(self.wal, rw.WAL_NAME), "rb") as fh:
            wal = fh.read()
        with open(os.path.join(self.anchor, rw.ANCHOR_NAME), "rb") as fh:
            anchor = fh.read()
        return wal, anchor

    def test_intent_commit_and_abort_all_refuse_on_a_rolled_back_log(self):
        before = self._bytes()
        for label, call in (
            ("intent", lambda: rw.open_repair(self.wal, day=self.days[1], at_utc="t",
                                              operator="ops", payload={},
                                              anchor_root=self.anchor)),
            ("commit", lambda: rw.commit_repair(self.wal, repair_id=self.rid,
                                                day=self.days[0], fingerprint="fp2",
                                                at_utc="t", operator="ops",
                                                anchor_root=self.anchor)),
            ("abort", lambda: rw.abort_repair(self.wal, repair_id=self.rid, day=self.days[0],
                                              reason="x", at_utc="t", operator="ops",
                                              anchor_root=self.anchor)),
        ):
            with self.subTest(label):
                with self.assertRaises(rw.WalRolledBack) as cm:
                    call()
                self.assertIn("must equal the head", str(cm.exception))
                self.assertEqual(self._bytes(), before,
                                 "WAL and external anchor must be byte-identical")
                self.assertFalse(os.path.isfile(os.path.join(self.wal, rw.ANCHOR_NAME)),
                                 "no co-located anchor may be created")

    def test_the_missing_seq_is_NOT_reused(self):
        """The precise defect: the replacement append reported success at the missing seq."""
        head = rw.parse_wal(os.path.join(self.wal, rw.WAL_NAME)).last_seq
        anchor_seq = rw.read_wal(self.wal, anchor_root=self.anchor).anchor["seq"]
        self.assertLess(head, anchor_seq, "precondition: the log is behind the anchor")
        with self.assertRaises(rw.WalRolledBack):
            rw.open_repair(self.wal, day=self.days[1], at_utc="t", operator="ops",
                           payload={}, anchor_root=self.anchor)
        self.assertEqual(rw.parse_wal(os.path.join(self.wal, rw.WAL_NAME)).last_seq, head)


class TestAnchorPreflightFailsClosed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.anchor = os.path.join(self.tmp, "anchor")
        os.makedirs(self.anchor, exist_ok=True)
        rw.declare_anchor_domain(self.anchor, domain_id="d", at_utc="t", operator="ops")

    def test_a_MISSING_wal_root_does_not_pass_the_filesystem_preflight(self):
        """`os.stat` failing is an unanswerable question, not a passing answer — and a missing
        WAL root is the ordinary state when initializing a new deployment."""
        missing = os.path.join(self.tmp, "not_created_yet")
        self.assertFalse(os.path.exists(missing))
        with self.assertRaises(rw.WalNotInitialized) as cm:
            rw.resolve_anchor_root(missing, self.anchor)
        self.assertIn("cannot compare filesystems", str(cm.exception))

    def test_a_missing_anchor_root_is_refused(self):
        with self.assertRaises(rw.WalNotInitialized) as cm:
            rw.resolve_anchor_root(self.tmp, os.path.join(self.tmp, "nope"))
        self.assertIn("not an existing directory", str(cm.exception))

    def test_initialize_wal_creates_the_wal_root_then_preflights(self):
        """It is the one entry point that legitimately meets a missing WAL root, so it creates
        it — and then the same-filesystem check still applies."""
        fresh = os.path.join(self.tmp, "fresh_wal")
        with self.assertRaises(rw.WalNotInitialized) as cm:
            rw.initialize_wal(fresh, manifest_root=self.tmp, at_utc="t", operator="ops",
                              anchor_root=self.anchor)
        self.assertIn("same filesystem", str(cm.exception))
        self.assertTrue(os.path.isdir(fresh), "the WAL root was created before the preflight")


class TestDeclarationSchemaIsStrict(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.anchor = os.path.join(self.tmp, "anchor")
        os.makedirs(self.anchor, exist_ok=True)

    def test_allow_same_filesystem_is_not_coerced(self):
        """`bool("false")` is True — a typo in a deployment script silently granting the
        waiver it was trying to withhold."""
        for bad in ("false", 0, 1, None, []):
            with self.subTest(value=bad):
                with self.assertRaises(rw.WalError) as cm:
                    rw.declare_anchor_domain(self.anchor, domain_id="d", at_utc="t",
                                             operator="ops", allow_same_filesystem=bad)
                self.assertIn("raw bool", str(cm.exception))
                self.assertFalse(os.path.isfile(
                    os.path.join(self.anchor, rw.ANCHOR_DOMAIN_NAME)))

    def test_a_declaration_with_the_wrong_field_set_or_type_is_refused(self):
        """Each case starts from a PRISTINE declaration: reading the file back would carry the
        previous iteration's damage forward, and every case would then pass for the first
        case's reason."""
        rw.declare_anchor_domain(self.anchor, domain_id="d", at_utc="t", operator="ops")
        path = os.path.join(self.anchor, rw.ANCHOR_DOMAIN_NAME)
        with open(path) as fh:
            pristine = fh.read()
        for label, mutate in (("missing field", lambda d: d.pop("operator")),
                              ("unknown field", lambda d: d.update({"extra": 1})),
                              ("truthy string flag",
                               lambda d: d.update({"allow_same_filesystem": "true"})),
                              ("non-string id", lambda d: d.update({"domain_id": 7})),
                              ("empty id", lambda d: d.update({"domain_id": ""}))):
            with self.subTest(label):
                doc = json.loads(pristine)
                mutate(doc)
                with open(path, "w") as fh:
                    json.dump(doc, fh)
                with self.assertRaises(rw.WalCorrupt):
                    rw.anchor_domain_id(self.anchor)

    def test_re_declaring_the_SAME_document_is_idempotent(self):
        kw = dict(domain_id="d", at_utc="t", operator="ops", note="n",
                  allow_same_filesystem=True)
        self.assertEqual(rw.declare_anchor_domain(self.anchor, **kw)["status"], "declared")
        self.assertEqual(rw.declare_anchor_domain(self.anchor, **kw)["status"],
                         "already_declared")

    def test_re_declaring_the_same_id_with_DIFFERENT_content_is_refused(self):
        """The old code returned `already_declared` and ignored the new fields, so the
        documented "re-declare with allow_same_filesystem=True" was a no-op that looked like
        it worked."""
        rw.declare_anchor_domain(self.anchor, domain_id="d", at_utc="t", operator="ops")
        with self.assertRaises(rw.WalError) as cm:
            rw.declare_anchor_domain(self.anchor, domain_id="d", at_utc="t", operator="ops",
                                     allow_same_filesystem=True)
        self.assertIn("immutable", str(cm.exception))
        self.assertIn("allow_same_filesystem", str(cm.exception))
        self.assertFalse(rw._anchor_domain_doc(self.anchor)["allow_same_filesystem"],
                         "the stored declaration must be unchanged")


class TestAuthorityBindsPathAndDomain(unittest.TestCase):
    """The docstring claimed "domain id, not path". It binds BOTH — which is the safer
    behaviour, so the claim was corrected rather than the code."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "manifest_root")
        os.makedirs(self.root, exist_ok=True)
        s0, e0 = bm.block_bounds(ANCHOR, S, 0)
        days = fx.calendar_span(s0, e0)[:2]
        delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(delta, days)
        blk = build_block(os.path.join(self.root, "b.zarr"), start_day=s0, end_day=e0,
                          classification_target=list(days), delta_path=delta,
                          artifacts_dir=os.path.join(self.tmp, "art"), lock=None,
                          hard_reserve_bytes=0, unsafe_skip_isolation=True)
        m = {"format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION, "generation": 1,
             "generation_id": "g1", "created_utc": "2027-01-01T00:00:00Z", "created_by": "t",
             "predecessor_generation": None, "predecessor_manifest": None,
             "grid": {"ny": 32, "nx": 32, "region": [0, 32, 0, 32]},
             "variables": list(fx.VARS),
             "block_grid": {"anchor_day": ANCHOR, "block_days": S},
             "segments": [dict(blk["segment"])], "superseded": [], "manifest_checksum": ""}
        m["manifest_checksum"] = bm.compute_checksum(m)
        bm.publish(self.root, m)

    def test_the_authority_records_BOTH_and_a_twin_domain_is_refused(self):
        wal = os.path.join(self.tmp, "wal")
        a = os.path.join(self.tmp, "anchor_a")
        b = os.path.join(self.tmp, "anchor_b")
        for d in (wal, a, b):
            os.makedirs(d, exist_ok=True)
        for d in (a, b):
            rw.declare_anchor_domain(d, domain_id="same-id", at_utc="t", operator="ops",
                                     allow_same_filesystem=True)
        rw.initialize_wal(wal, manifest_root=self.root, at_utc="t", operator="ops",
                          anchor_root=a)
        auth = rw.read_wal(wal, anchor_root=a).authority
        self.assertEqual(auth["anchor_domain_id"], "same-id")
        self.assertEqual(auth["anchor_root"], os.path.realpath(a))

        # a second root carrying the SAME declaration is still a different anchor
        with self.assertRaises(rw.WalNotInitialized) as cm:
            rw.read_wal(wal, anchor_root=b).assert_bound_to(self.root, os.path.realpath(b))
        self.assertIn("different deployment", str(cm.exception))


# ==================== review round 8: a twin anchor root with the SAME declared domain
class TestWriterIsBoundToTheCanonicalAnchorPath(unittest.TestCase):
    """The previous bypass, one level down. A second root can declare the same `domain_id` and
    carry a copy of the current sidecar; checking the id alone let a terminal write succeed
    there, advancing the WAL and the twin while the canonical anchor fell behind."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "manifest_root")
        self.wal = os.path.join(self.tmp, "wal")
        self.anchor = os.path.join(self.tmp, "anchor_domain")
        self.twin = os.path.join(self.tmp, "twin_domain")
        for d in (self.root, self.wal, self.anchor, self.twin):
            os.makedirs(d, exist_ok=True)
        s0, e0 = bm.block_bounds(ANCHOR, S, 0)
        self.days = fx.calendar_span(s0, e0)[:3]
        delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(delta, self.days)
        blk = build_block(os.path.join(self.root, "b.zarr"), start_day=s0, end_day=e0,
                          classification_target=list(self.days), delta_path=delta,
                          artifacts_dir=os.path.join(self.tmp, "art"), lock=None,
                          hard_reserve_bytes=0, unsafe_skip_isolation=True)
        m = {"format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION, "generation": 1,
             "generation_id": "g1", "created_utc": "2027-01-01T00:00:00Z", "created_by": "t",
             "predecessor_generation": None, "predecessor_manifest": None,
             "grid": {"ny": 32, "nx": 32, "region": [0, 32, 0, 32]},
             "variables": list(fx.VARS),
             "block_grid": {"anchor_day": ANCHOR, "block_days": S},
             "segments": [dict(blk["segment"])], "superseded": [], "manifest_checksum": ""}
        m["manifest_checksum"] = bm.compute_checksum(m)
        bm.publish(self.root, m)

        # TWO roots, the SAME declared domain id
        decl = dict(domain_id="same-domain", at_utc="t", operator="ops",
                    allow_same_filesystem=True, note="unit-test domain")
        rw.declare_anchor_domain(self.anchor, **decl)
        rw.declare_anchor_domain(self.twin, **decl)
        rw.initialize_wal(self.wal, manifest_root=self.root, at_utc="t", operator="ops",
                          anchor_root=self.anchor)
        self.rid = rw.open_repair(self.wal, day=self.days[0], at_utc="t", operator="ops",
                                  payload={}, anchor_root=self.anchor)["repair_id"]
        # the twin carries a COPY of the current sidecar, so the id check alone cannot tell
        shutil.copy(os.path.join(self.anchor, rw.ANCHOR_NAME),
                    os.path.join(self.twin, rw.ANCHOR_NAME))

    def _snapshot(self):
        out = {}
        for label, path in (("wal", os.path.join(self.wal, rw.WAL_NAME)),
                            ("anchor", os.path.join(self.anchor, rw.ANCHOR_NAME)),
                            ("twin", os.path.join(self.twin, rw.ANCHOR_NAME))):
            with open(path, "rb") as fh:
                out[label] = fh.read()
        return out

    def test_the_twin_really_is_indistinguishable_by_domain_id(self):
        """Precondition: without the path check there is nothing to tell them apart."""
        self.assertEqual(rw.anchor_domain_id(self.anchor), rw.anchor_domain_id(self.twin))
        self.assertEqual(self._snapshot()["anchor"], self._snapshot()["twin"])

    def test_intent_commit_and_abort_all_refuse_on_the_twin_path(self):
        before = self._snapshot()
        for label, call in (
            ("intent", lambda: rw.open_repair(self.wal, day=self.days[1], at_utc="t",
                                              operator="ops", payload={},
                                              anchor_root=self.twin)),
            ("commit", lambda: rw.commit_repair(self.wal, repair_id=self.rid,
                                                day=self.days[0], fingerprint="fp",
                                                at_utc="t", operator="ops",
                                                anchor_root=self.twin)),
            ("abort", lambda: rw.abort_repair(self.wal, repair_id=self.rid, day=self.days[0],
                                              reason="x", at_utc="t", operator="ops",
                                              anchor_root=self.twin)),
        ):
            with self.subTest(label):
                with self.assertRaises(rw.WalNotInitialized) as cm:
                    call()
                self.assertIn("still a different anchor", str(cm.exception))
                self.assertEqual(self._snapshot(), before,
                                 "WAL, canonical anchor and twin must all be byte-identical")

    def test_the_canonical_path_still_works(self):
        """The binding must not refuse the legitimate anchor."""
        rw.commit_repair(self.wal, repair_id=self.rid, day=self.days[0], fingerprint="fp",
                         at_utc="t", operator="ops", anchor_root=self.anchor)
        state = rw.read_wal(self.wal, anchor_root=self.anchor)
        self.assertEqual(state.repairs[self.rid].state, rw.COMMITTED)
        self.assertEqual(state.anchor["seq"], state.last_seq)

    def test_the_refusal_is_before_freshness_so_a_stale_twin_reports_the_path(self):
        """Ordering: the path mismatch is the caller's actual mistake, so it is the diagnosis
        they get -- not a freshness error about an anchor they never meant to use."""
        with open(os.path.join(self.twin, rw.ANCHOR_NAME), "w") as fh:
            fh.write(rw.canonical({"seq": 99, "record_checksum": "0" * 64, "at_utc": "t"}))
        with self.assertRaises(rw.WalNotInitialized) as cm:
            rw.commit_repair(self.wal, repair_id=self.rid, day=self.days[0], fingerprint="fp",
                             at_utc="t", operator="ops", anchor_root=self.twin)
        self.assertIn("still a different anchor", str(cm.exception))
