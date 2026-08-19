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


class TestDuplicatesAreRefusedNotDeduplicated(unittest.TestCase):
    """Finding 1. `dict.fromkeys()` silently collapsed repeats, laundering a malformed store
    into a legal-looking day set."""

    def test_a_duplicated_live_delta_day_is_refused(self):
        with self.assertRaises(DegradedPruneRefused) as cm:
            partition_delta_days(live_delta_days=LIVE + [LIVE[0]], audit=_audit(),
                                 approval=_approval())
        self.assertIn("more than once", str(cm.exception))
        self.assertIn(LIVE[0], str(cm.exception))

    def test_a_duplicated_approval_entry_is_refused(self):
        with self.assertRaises(DegradedPruneRefused) as cm:
            partition_delta_days(live_delta_days=LIVE, audit=_audit(),
                                 approval=_approval(APPROVED + [APPROVED[0]]))
        self.assertIn("more than once", str(cm.exception))

    def test_a_duplicated_audit_candidate_is_refused_with_NO_approval_involved(self):
        """Reached without an approval at all, so the "approval must equal the audit" check
        cannot answer first. That shadowing is why the guard survived its first mutation."""
        with self.assertRaises(DegradedPruneRefused) as cm:
            partition_delta_days(live_delta_days=LIVE,
                                 audit=_audit(candidates=APPROVED + [APPROVED[0]], blocked=[]))
        self.assertIn("candidate day(s)", str(cm.exception))
        self.assertIn("more than once", str(cm.exception))

    def test_a_duplicated_BLOCKED_day_is_refused_too(self):
        with self.assertRaises(DegradedPruneRefused) as cm:
            partition_delta_days(live_delta_days=LIVE,
                                 audit=_audit(blocked=BLOCKED + [BLOCKED[0]]),
                                 approval=_approval())
        self.assertIn("blocked day(s)", str(cm.exception))

    def test_a_duplicate_is_not_quietly_absorbed_into_a_success(self):
        """The shape the old code produced: a duplicated day, and a clean-looking result."""
        with self.assertRaises(DegradedPruneRefused):
            partition_delta_days(live_delta_days=LIVE + [APPROVED[0]], audit=_audit(),
                                 approval=_approval())


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
    """Finding 6, re-done against the PRODUCTION function.

    The first version of this class computed the split itself and asserted on its own
    arithmetic, so `p4_retention_audit.delta_prune()` could have been broken outright and these
    tests would still have passed. Every assertion below now comes from calling it."""

    #: Base's latest day is 2026-06-26, but it does NOT cover 06-05..06-22: those days were
    #: never compacted in, which is what `blocked_need_compaction_first` reports. A fixture
    #: where base simply ends earlier would make every candidate blocked and prove nothing.
    BASE_DAYS = sorted([f"2026-06-{d:02d}" for d in range(1, 5)] + APPROVED)

    def _audit_out(self, base_days=None, delta_days=None):
        from ops import p4_retention_audit as audit
        # window_days + delta_buffer - 1 back from the newest delta day => keep_start 2026-06-27
        # the store-info shape `_dayset` actually reads
        return audit.delta_prune(
            {"present": True, "physical_days": list(base_days or self.BASE_DAYS)},
            {"present": True, "physical_days": list(delta_days or LIVE)},
            window_days=3, delta_buffer=1)

    def test_the_production_audit_reproduces_the_rehearsals_4_of_22_split(self):
        out = self._audit_out()
        self.assertTrue(out["available"], out.get("reason"))
        self.assertEqual(out["keep_window_start"], "2026-06-27")
        self.assertEqual(len(out["drop_candidates_by_calendar"]), 22)
        self.assertEqual(sorted(out["delta_prune_candidates"]), sorted(APPROVED))
        self.assertEqual(out["delta_prune_candidate_count"], 4)
        self.assertEqual(sorted(out["blocked_need_compaction_first"]), sorted(BLOCKED))
        self.assertEqual(len(out["blocked_need_compaction_first"]), 18)

    def test_base_covers_all_drop_candidates_is_FALSE_in_that_split(self):
        """Its name reads like "base covers the days we are about to drop", which is true of
        `delta_prune_candidates` by construction. It means "base covers EVERY calendar drop
        candidate, so nothing is blocked" -- False here, with 4 of 22 covered."""
        self.assertFalse(self._audit_out()["base_covers_all_drop_candidates"])

    def test_it_is_TRUE_only_when_nothing_is_blocked(self):
        """The companion: otherwise "always False" would also pass."""
        full_base = sorted(set(self.BASE_DAYS) | set(BLOCKED))
        out = self._audit_out(base_days=full_base)
        self.assertTrue(out["base_covers_all_drop_candidates"])
        self.assertEqual(out["blocked_need_compaction_first"], [])
        self.assertEqual(len(out["delta_prune_candidates"]), 22)

    def test_the_two_sets_partition_the_calendar_candidates(self):
        out = self._audit_out()
        self.assertEqual(
            sorted(out["delta_prune_candidates"] + out["blocked_need_compaction_first"]),
            sorted(out["drop_candidates_by_calendar"]))
        self.assertEqual(set(out["delta_prune_candidates"])
                         & set(out["blocked_need_compaction_first"]), set())

    def test_the_audits_own_output_feeds_the_partition_end_to_end(self):
        """The two halves must agree: what the audit emits is what the partition consumes."""
        out = self._audit_out()
        part = partition_delta_days(
            live_delta_days=LIVE, audit={"delta_prune": out},
            approval=_approval(out["delta_prune_candidates"]))
        self.assertEqual(sorted(part["dropped_days"]), sorted(APPROVED))
        self.assertEqual(len(part["blocked_retained"]), 18)

    def test_there_is_no_field_implying_an_approval_the_audit_never_saw(self):
        """Finding 4: `base_uncovered_approved_candidates` was always `[]` and its name implied
        an approval existed. Approval happens at the prune stage, not in the audit."""
        self.assertNotIn("base_uncovered_approved_candidates", self._audit_out())
        with open(os.path.join(_DEV2026, "ops", "p4_retention_audit.py")) as fh:
            self.assertNotIn("base_uncovered_approved_candidates", fh.read())

    def test_the_four_calendar_and_coverage_sets_are_named_distinctly(self):
        out = self._audit_out()
        for field in ("drop_candidates_by_calendar", "delta_prune_candidates",
                      "blocked_need_compaction_first", "base_covers_all_drop_candidates"):
            self.assertIn(field, out)


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

    def test_the_three_sections_all_state_the_NO_GO_DEFAULT_and_the_approval(self):
        """Checking for the word "degraded" would stay green with the policy deleted. Each
        section must carry BOTH halves: blocked days are NO-GO by default, and degraded mode
        needs a recorded approval."""
        for anchor in ("## 2.", "### 4a.", "## 9."):
            i = self.text.index(anchor)
            section = self.text[i:i + 5000].lower()
            with self.subTest(anchor):
                self.assertIn("no-go", section, "the default outcome must be stated here")
                self.assertIn("approval", section, "the exception must be stated here")
                self.assertIn("blocked", section)

    def test_the_runbooks_own_policy_code_REFUSES_without_an_approval(self):
        """Executable, not textual: the §4a snippet's policy is exercised against the
        rehearsal's audit shape. A runbook whose prose is right and whose code is wrong is the
        failure this whole round is about."""
        i = self.text.index("from ops.degraded_prune import partition_delta_days")
        block = self.text[i:self.text.index("base_days", i)]
        self.assertIn("APPROVAL = None", block, "the default must be no approval")
        self.assertIn("partition_delta_days(live_delta_days=", block)
        # the default path, run for real
        with self.assertRaises(DegradedPruneRefused):
            partition_delta_days(live_delta_days=LIVE, audit=_audit(), approval=None)

    def test_the_runbook_records_the_partition_for_the_run(self):
        self.assertIn("degraded_partition.json", self.text)


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
        for field in ("staging_build_present", "live_swap_performed", "production_mutation"):
            self.assertIn(f'"{field}"', src)
        self.assertNotIn("staging_build_mutation", src,
                         "this executor swaps a staging store in; it does not build one")

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
        self.assertTrue(res["staging_build_present"])
        self.assertNotIn("staging_build_mutation", res)
        self.assertTrue(res["production_mutation"])
        self.assertTrue(res["swap_performed"])
        self.assertTrue(res["manifest"]["quiescence"]["waived"]
                        or res["manifest"]["quiescence"]["attested"])


if __name__ == "__main__":
    unittest.main()
