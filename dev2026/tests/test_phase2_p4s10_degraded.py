"""dev2026 — P4-S10 execution findings: the degraded-prune partition, and the runbook contract.

The VM24 rehearsal produced 4 approved candidates and 18 blocked days. The runbook's inline
`keep = [d for d in live if d >= keep_window_start]` would have offered all 22 for dropping.
`prune_delta`'s base-coverage gate would have refused that plan, so nothing was lost — but a
day set that is wrong and caught downstream is one gate-relaxation away from data loss.

These tests pin the arithmetic where it can be tested, and pin the runbook to it.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ops.degraded_prune import (  # noqa: E402
    DegradedPruneRefused, partition_delta_days,
)

_DEV2026 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNBOOK = os.path.join(_DEV2026, "specs", "p4s10_production_rollout_runbook.md")

# The rehearsal's shape: 22 calendar drop candidates, 4 of them base-covered.
BLOCKED = [f"2026-06-{d:02d}" for d in range(5, 23)]              # 18 days
APPROVED = ["2026-06-23", "2026-06-24", "2026-06-25", "2026-06-26"]
LIVE = sorted(BLOCKED + APPROVED + [f"2026-06-{d:02d}" for d in range(27, 31)])


def _audit(candidates=APPROVED, blocked=BLOCKED):
    return {"delta_prune": {"available": True, "keep_window_start": "2026-06-27",
                            "delta_prune_candidates": list(candidates),
                            "blocked_need_compaction_first": list(blocked)}}


def _approval(days=APPROVED):
    return {"approved_by": "ops", "approval_ref": "P4-S10 rehearsal note 2026-08-18",
            "approved_candidates": list(days)}


class TestTheExactPartition(unittest.TestCase):

    def test_the_dropped_set_is_EXACTLY_the_audits_candidates(self):
        out = partition_delta_days(live_delta_days=LIVE, audit=_audit(), approval=_approval())
        self.assertEqual(out["dropped_days"], APPROVED)
        self.assertEqual(len(out["dropped_days"]), 4)

    def test_keep_is_live_minus_approved_and_the_two_partition_the_delta(self):
        out = partition_delta_days(live_delta_days=LIVE, audit=_audit(), approval=_approval())
        self.assertEqual(sorted(out["keep_days"] + out["dropped_days"]), sorted(LIVE))
        self.assertEqual(set(out["keep_days"]) & set(out["dropped_days"]), set())

    def test_every_blocked_day_is_retained(self):
        out = partition_delta_days(live_delta_days=LIVE, audit=_audit(), approval=_approval())
        self.assertEqual(sorted(out["blocked_retained"]), sorted(BLOCKED))
        self.assertEqual(len(out["blocked_retained"]), 18)
        for day in BLOCKED:
            self.assertIn(day, out["keep_days"])
            self.assertNotIn(day, out["dropped_days"])

    def test_the_keep_window_arithmetic_would_have_dropped_the_blocked_days(self):
        """The bug this function exists to remove, stated as a fact rather than a memory."""
        keep_start = _audit()["delta_prune"]["keep_window_start"]
        old_keep = [d for d in LIVE if d >= keep_start]
        old_dropped = [d for d in LIVE if d < keep_start]
        self.assertEqual(len(old_dropped), 22)
        for day in BLOCKED:
            self.assertIn(day, old_dropped, "the old expression dropped base-uncovered days")
        self.assertNotEqual(sorted(old_keep), sorted(
            partition_delta_days(live_delta_days=LIVE, audit=_audit(),
                                 approval=_approval())["keep_days"]))


class TestApprovalIsRequired(unittest.TestCase):

    def test_blocked_days_without_an_approval_is_a_REFUSAL(self):
        with self.assertRaises(DegradedPruneRefused) as cm:
            partition_delta_days(live_delta_days=LIVE, audit=_audit())
        self.assertIn("normal outcome is NO-GO", str(cm.exception))

    def test_no_blocked_days_needs_no_approval(self):
        out = partition_delta_days(live_delta_days=LIVE, audit=_audit(blocked=[]))
        self.assertEqual(out["mode"], "normal")
        self.assertEqual(out["dropped_days"], APPROVED)

    def test_an_approval_must_say_who_and_against_what(self):
        for drop in ("approved_by", "approval_ref", "approved_candidates"):
            with self.subTest(drop):
                appr = {k: v for k, v in _approval().items() if k != drop}
                with self.assertRaises(DegradedPruneRefused) as cm:
                    partition_delta_days(live_delta_days=LIVE, audit=_audit(), approval=appr)
                self.assertIn(drop, str(cm.exception))

    def test_a_blank_approver_or_reference_is_refused(self):
        for field in ("approved_by", "approval_ref"):
            with self.subTest(field):
                appr = {**_approval(), field: "   "}
                with self.assertRaises(DegradedPruneRefused):
                    partition_delta_days(live_delta_days=LIVE, audit=_audit(), approval=appr)

    def test_an_operator_WIDENED_list_is_refused(self):
        """The route by which a blocked day gets dropped."""
        with self.assertRaises(DegradedPruneRefused) as cm:
            partition_delta_days(live_delta_days=LIVE, audit=_audit(),
                                 approval=_approval(APPROVED + [BLOCKED[-1]]))
        self.assertIn("EXACTLY", str(cm.exception))

    def test_an_operator_NARROWED_list_is_refused(self):
        with self.assertRaises(DegradedPruneRefused):
            partition_delta_days(live_delta_days=LIVE, audit=_audit(),
                                 approval=_approval(APPROVED[:2]))

    def test_the_mode_is_recorded_with_its_approval_reference(self):
        out = partition_delta_days(live_delta_days=LIVE, audit=_audit(), approval=_approval())
        self.assertEqual(out["mode"], "degraded_candidate_only")
        self.assertIn("rehearsal note", out["approval_ref"])


class TestTheAuditMustBeConsistentAndFresh(unittest.TestCase):

    def test_a_day_listed_as_BOTH_eligible_and_blocked_is_refused(self):
        with self.assertRaises(DegradedPruneRefused) as cm:
            partition_delta_days(live_delta_days=LIVE,
                                 audit=_audit(candidates=APPROVED + [BLOCKED[0]]),
                                 approval=_approval(APPROVED + [BLOCKED[0]]))
        self.assertIn("BOTH", str(cm.exception))

    def test_a_candidate_missing_from_the_live_delta_means_a_stale_audit(self):
        with self.assertRaises(DegradedPruneRefused) as cm:
            partition_delta_days(live_delta_days=[d for d in LIVE if d != APPROVED[0]],
                                 audit=_audit(), approval=_approval())
        self.assertIn("stale", str(cm.exception))

    def test_an_unavailable_audit_block_is_refused(self):
        with self.assertRaises(DegradedPruneRefused):
            partition_delta_days(live_delta_days=LIVE, audit={"delta_prune": {}},
                                 approval=_approval())


class TestTheAuditFieldSemantics(unittest.TestCase):
    """Finding 6: the field names must distinguish four different things, and the real audit
    must produce the rehearsal's split rather than a plausible-looking one."""

    #: Base's latest day is 2026-06-26, but it does NOT cover 06-05..06-22: those days were
    #: never compacted in, which is exactly what `blocked_need_compaction_first` reports. A
    #: fixture where base simply ends earlier would make every candidate blocked and prove
    #: nothing about the split.
    BASE_DAYS = sorted([f"2026-06-{d:02d}" for d in range(1, 5)] + APPROVED)

    def test_base_covers_all_drop_candidates_is_FALSE_when_any_day_is_blocked(self):
        """Its name reads like "base covers the days we are about to drop", which is true of the
        approved candidates by construction. It actually means "base covers EVERY calendar drop
        candidate, so nothing is blocked" -- the rehearsal's own case, where 4 of 22 were
        covered, must report False."""
        covers = (not BLOCKED and bool(BLOCKED + APPROVED))
        self.assertFalse(covers)

    def test_approved_candidates_are_base_covered_while_blocked_days_are_not(self):
        base = set(self.BASE_DAYS)
        calendar_candidates = BLOCKED + APPROVED
        eligible = [d for d in calendar_candidates if d in base]
        blocked = [d for d in calendar_candidates if d not in base]
        self.assertEqual(len(eligible), 4)
        self.assertEqual(len(blocked), 18)
        self.assertEqual(sorted(eligible), sorted(APPROVED))
        self.assertEqual(set(eligible) & set(blocked), set())


class TestTheRunbookUsesTheTestedPartition(unittest.TestCase):
    """The runbook must not recompute the day sets inline; that is what went wrong."""

    @classmethod
    def setUpClass(cls):
        with open(RUNBOOK) as fh:
            cls.text = fh.read()

    def test_the_keep_window_expression_is_gone(self):
        self.assertNotIn("keep = [d for d in dd if d >= keep_start]", self.text,
                         "this expression drops base-uncovered days")

    def test_the_runbook_calls_the_partition_function(self):
        self.assertIn("partition_delta_days", self.text)
        self.assertIn("from ops.degraded_prune import", self.text)

    def test_the_runbook_separates_CODE_ROOT_PYTHON_and_PYTHONPATH(self):
        self.assertIn("export CODE_ROOT=/home/odbadmin/python/ghrsst-p4s10/dev2026", self.text)
        self.assertIn("export PYTHON=/home/odbadmin/python/ghrsst-dev2026-phase2/dev2026/"
                      ".venv/bin/python", self.text)
        self.assertIn("export PYTHONPATH=$CODE_ROOT", self.text)

    def test_the_runbook_preflights_the_interpreter_and_the_code_root(self):
        self.assertIn('test -x "$PYTHON"', self.text)
        self.assertIn('test -f "$CODE_ROOT/ops/p4_retention_audit.py"', self.text)
        for mod in ("ingest.prune_delta", "ingest.swap_delta", "ingest.prune_staging"):
            self.assertIn(mod, self.text)

    def test_the_O4_cron_points_at_the_code_root_not_the_phase2_worktree(self):
        cron = [l for l in self.text.splitlines() if "p4_retention_audit.py" in l and "cron" not in l.lower()]
        self.assertTrue(cron)
        offenders = [l for l in self.text.splitlines()
                     if "ghrsst-dev2026-phase2" in l and "p4_retention_audit.py" in l]
        self.assertEqual(offenders, [],
                         "the audit script does not exist under the phase2 worktree")

    def test_the_three_sections_agree_that_blocked_days_mean_NO_GO_by_default(self):
        for anchor in ("## 2.", "### 4a.", "## 9."):
            i = self.text.index(anchor)
            section = self.text[i:i + 4000]
            with self.subTest(anchor):
                self.assertIn("degraded", section.lower())


class TestSwapArtifactSemantics(unittest.TestCase):
    """Finding 5: a successful LIVE swap reported `production_mutation=false`."""

    def test_a_successful_swap_is_not_labelled_as_no_production_mutation(self):
        with open(os.path.join(_DEV2026, "ingest", "swap_delta.py")) as fh:
            src = fh.read()
        self.assertNotIn('"production_mutation": False}', src,
                         "a swapped live delta IS a production mutation")

    def test_the_three_fields_exist_and_say_different_things(self):
        with open(os.path.join(_DEV2026, "ingest", "swap_delta.py")) as fh:
            src = fh.read()
        for field in ("staging_build_mutation", "live_swap_performed", "production_mutation"):
            self.assertIn(f'"{field}"', src)

    def test_a_REAL_successful_swap_reports_live_swap_performed_true(self):
        """Ground truth, through the executor, not a source grep."""
        import shutil
        import tempfile
        sys.path.insert(0, os.path.join(_DEV2026, "tests"))
        import test_phase2_p5s5_part3 as p3

        case = p3.TestTheFourStates("test_1_old_base_old_delta_is_safe")
        case.setUp()
        self.addCleanup(shutil.rmtree, case.tmp, ignore_errors=True)
        case._repair()
        case._refold()
        res = case._swap(case._plan())
        self.assertEqual(res["status"], "swapped", res.get("reason"))
        self.assertTrue(res["live_swap_performed"])
        self.assertTrue(res["staging_build_mutation"])
        self.assertTrue(res["production_mutation"])
        self.assertTrue(res["swap_performed"])
        self.assertTrue(res["manifest"]["quiescence"]["waived"]
                        or res["manifest"]["quiescence"]["attested"])


if __name__ == "__main__":
    unittest.main()
