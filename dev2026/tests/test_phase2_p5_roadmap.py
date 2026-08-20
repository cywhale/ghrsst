"""dev2026 — P5 roadmap exception, capacity unlock, critical path: the guards.

**LOCAL-ONLY PLANNING ARTEFACTS.** Nothing here contacts VM24 or performs a production action.

The declarations live in `ops/p5_roadmap.py` and the documents are checked against them. A
roadmap that exists only as prose drifts the moment one section is corrected and another is not
— P5-S6 shipped two contradictory status sections for exactly that reason, and every
artifact-drift guard passed because they all read the first copy.

The sharpest guard here is the one that refuses to let the exception grow: `authorizes()` must
say **no** to every mutating verb, and it must fail closed, so a verb added to the codebase
later is denied by default rather than by someone remembering to list it.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
for p in (_DEV, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from ops import p5_roadmap as rm  # noqa: E402

MEMO = os.path.join(_DEV, "specs", "p5_critical_path_memo.md")
RUNBOOK = os.path.join(_DEV, "specs", "p5s8_vm24_shadow_runbook.md")
SPEC = os.path.join(_DEV, "specs", "p5_segmented_timecube_compaction_design.md")
S7_DOC = os.path.join(_DEV, "specs", "p5s7_local_gate_results.md")
VM24 = os.path.join(_DEV, "bench", "results", "p5s6_vm24_readonly.json")


def _read(path):
    with open(path) as fh:
        return fh.read()


class TestTheExceptionAuthorizesNothingMutating(unittest.TestCase):

    def test_every_forbidden_action_is_refused(self):
        for action in rm.FORBIDDEN_UNDER_EXCEPTION:
            with self.subTest(action):
                self.assertFalse(rm.authorizes(action))

    def test_it_fails_CLOSED_on_an_action_nobody_listed(self):
        """The list of forbidden verbs will always be incomplete. Refusing by default is what
        makes that survivable; an allow-by-default check would authorize the next verb someone
        invents."""
        for invented in ("rm_rf", "truncate", "reindex", "restore", "", "PRUNE", "prune "):
            with self.subTest(invented):
                self.assertFalse(rm.authorizes(invented))

    def test_only_authoring_and_local_work_are_permitted(self):
        self.assertEqual(set(rm.PERMITTED_UNDER_EXCEPTION),
                         {"s8_runbook_authoring", "local_tests", "local_documents"})
        for allowed in rm.PERMITTED_UNDER_EXCEPTION:
            self.assertTrue(rm.authorizes(allowed))

    def test_the_exception_never_marks_S7_pass(self):
        self.assertFalse(rm.s7_is_pass())
        self.assertEqual(rm.S7_GATE["verdict"], "NOT_MET")
        self.assertTrue(rm.ROADMAP_EXCEPTION["does_not_mark_s7_pass"])
        self.assertEqual(rm.ROADMAP_EXCEPTION["s7_gate_state"], "NOT_MET")

    def test_S9_and_S10_each_need_their_own_approval(self):
        self.assertIn("separate human approval", rm.ROADMAP_EXCEPTION["s9_execution"])
        self.assertIn("read-only", rm.ROADMAP_EXCEPTION["s9_execution"])
        self.assertIn("fresh written approval", rm.ROADMAP_EXCEPTION["s10_rollout"])
        self.assertIn("s9_execution", rm.FORBIDDEN_UNDER_EXCEPTION)
        self.assertIn("s10_rollout", rm.FORBIDDEN_UNDER_EXCEPTION)

    def test_the_exception_declares_when_it_expires(self):
        """An exception with no expiry becomes the rule."""
        self.assertTrue(rm.ROADMAP_EXCEPTION["expires_when"].strip())
        for g in ("G3", "G4", "H7", "G16", "G18"):
            self.assertIn(g, rm.ROADMAP_EXCEPTION["expires_when"] + " "
                          + " ".join(rm.PRODUCTION_DEPENDENT_GATES))


class TestCapacityIsNotQuietlyRelaxed(unittest.TestCase):

    def test_HARD_RESERVE_is_fixed_and_matches_the_measured_evidence(self):
        self.assertTrue(rm.HARD_RESERVE_IS_FIXED)
        self.assertEqual(rm.CAPACITY["hard_reserve_gib"], 250)

    def test_the_capacity_numbers_come_from_the_VM24_artifact(self):
        """Typed figures drift from the measurement they claim to quote."""
        with open(VM24) as fh:
            a = json.load(fh)
        gate = a["capacity_gate"]
        self.assertEqual(rm.CAPACITY["free_now_gib"], gate["free_now_gb"])
        self.assertEqual(rm.CAPACITY["minimum_free_gib"], gate["free_now_required_gb"])
        self.assertEqual(rm.CAPACITY["hard_reserve_gib"], gate["hard_reserve_gb"])
        self.assertEqual(rm.CAPACITY["shortfall_gib"], gate["shortfall_gb"])
        self.assertEqual(rm.CAPACITY["verdict"], gate["verdict"])
        self.assertEqual(rm.CAPACITY["recommended_free_gib"],
                         a["hold_lifecycle_audit"]["capacity"]["recommended_free_gib"])
        self.assertEqual(rm.CAPACITY["currently_reclaimable_gib"],
                         a["hold_lifecycle_audit"]["capacity"]["currently_reclaimable_gib"])

    def test_G18_is_still_BLOCKED_because_free_is_below_the_minimum(self):
        self.assertLess(rm.CAPACITY["free_now_gib"], rm.CAPACITY["minimum_free_gib"])
        self.assertEqual(rm.CAPACITY["verdict"], "BLOCKED")

    def test_option_A_is_preferred_and_option_B_carries_every_obligation(self):
        by = {o["option"]: o for o in rm.CAPACITY_OPTIONS}
        self.assertTrue(by["A"]["preferred"])
        self.assertFalse(by["B"]["preferred"])
        blob = " ".join(by["B"]["proof_obligations"]).lower()
        for required in ("free space", "mount persistence", "filesystem",
                         "atomic", "rollback", "backup", "rename", "manifest"):
            with self.subTest(required):
                self.assertIn(required, blob)

    def test_capacity_enables_compaction_but_authorizes_no_deletion(self):
        scope = rm.CAPACITY_SCOPE
        self.assertIn("compaction", scope["enables"])
        self.assertIn("1267", scope["does_not_authorize"])
        joined = " ".join(scope["deletion_additionally_requires"]).lower()
        self.assertIn("recovery", joined)
        self.assertIn("membership is not sufficient", joined.replace("  ", " "))

    def test_no_local_synthetic_number_is_used_as_capacity_evidence(self):
        self.assertEqual(rm.CAPACITY["label"], "VM24_PRODUCTION_READ_ONLY")
        self.assertNotIn("LOCAL", rm.CAPACITY["label"])


class TestTheCriticalPath(unittest.TestCase):

    def test_the_ten_steps_are_present_in_order(self):
        self.assertEqual([s["step"] for s in rm.CRITICAL_PATH], list(range(1, 11)))

    def test_only_the_signed_off_step_is_complete(self):
        done = [s for s in rm.CRITICAL_PATH if s["status"] == "COMPLETE"]
        self.assertEqual([s["step"] for s in done], [1])
        self.assertIn("NOT an S7 gate pass", done[0]["note"])

    def test_capacity_blocks_the_path_at_step_3(self):
        step3 = rm.CRITICAL_PATH[2]
        self.assertEqual(step3["status"], "BLOCKED_ON_OPS")
        self.assertEqual(step3["gate"], "G18")
        self.assertEqual(rm.report()["next_approval_required"]["blocking_step"], 3)

    def test_the_compaction_posture_is_recorded_in_full(self):
        """Step 8 is the one that touches production. Paraphrasing it is how prune-before-verify
        gets introduced. The ORDER is asserted in detail by the S8 runbook suite; this checks
        the memo's step 8 still quotes the full posture rather than a shortened version."""
        p = rm.COMPACTION_POSTURE
        self.assertEqual(p[0], "compaction lock")
        self.assertLess(p.index("build/publish/verify as applicable"),
                        p.index("prune/swap only after verification"))
        self.assertEqual(p[-1], "restart/API verification")
        memo = " ".join(_read(MEMO).split())
        for stage in p:
            with self.subTest(stage):
                self.assertIn(stage, memo)

    def test_deletion_never_runs_on_day_membership_alone(self):
        step9 = rm.CRITICAL_PATH[8]
        self.assertIn("verified", step9["note"])
        self.assertIn("Never delete solely", step9["note"])

    def test_freed_bytes_must_be_measured_not_computed(self):
        step10 = rm.CRITICAL_PATH[9]
        text = step10["name"] + " " + step10["note"]
        self.assertIn("df", text)
        self.assertIn("not capacity evidence", text)


class TestTheS8RunbookCoversItsContract(unittest.TestCase):

    def setUp(self):
        self.doc = _read(RUNBOOK)
        self.flat = " ".join(self.doc.split())

    def test_every_required_section_is_covered(self):
        needles = {
            "G3_G4_latency": "G4 — span latency",
            "H7_inodes": "inode headroom",
            "G16_metadata": "metadata scan, fail closed",
            "G18_capacity": "Capacity preflight (G18)",
            "identity_preflight": "Identity preflight",
            "abort_conditions": "Abort conditions",
            "artifacts": "Artifacts and provenance",
            "no_write": "read-only boundary",
        }
        self.assertEqual(set(needles), set(rm.S8_REQUIRED_SECTIONS),
                         "the runbook's section contract changed; update both sides")
        for key, needle in needles.items():
            with self.subTest(key):
                self.assertIn(needle.lower(), self.flat.lower())

    def test_every_required_preflight_field_is_listed(self):
        for field in rm.S8_PREFLIGHT_FIELDS:
            with self.subTest(field):
                self.assertIn(field, self.doc)

    def test_the_runbook_states_it_has_not_been_executed(self):
        self.assertIn("AUTHORING ONLY", self.doc)
        self.assertIn("NOTHING IN THIS DOCUMENT HAS BEEN EXECUTED", self.doc)
        self.assertIn("separate human approval", self.flat)

    def test_the_runbook_promises_no_writes_and_names_the_shadow_exception(self):
        self.assertIn("**no** prune, swap, compaction, repair, hold deletion", self.flat)
        self.assertIn("not the production data volume", self.flat.replace("*", ""))
        # The shadow is now VERIFIED, not created: ops pre-provisions it, so the runbook's
        # complete write set is `$ART`. That is a stronger claim than the earlier draft's, and
        # it has to be stated rather than left implicit.
        plain0 = self.flat.replace("*", "")
        self.assertIn("pre-provisioned by ops", plain0)
        self.assertIn("nothing else writes anywhere", plain0.lower())
        # Markdown-insensitive: the doc emphasises the negation ("**not** the production
        # data volume"), so a literal match on the plain phrase silently fails.
        plain = self.flat.replace("*", "")
        self.assertIn("not the production data volume", plain)

    def test_the_runbook_does_not_claim_it_settles_G3_or_the_S7_gate(self):
        self.assertIn("No S8 result marks the S7 gate PASS", self.flat)
        self.assertIn("G3", self.flat)
        self.assertIn("NOT_ADJUDICATED", self.flat)

    def test_HARD_RESERVE_is_not_adjusted_by_the_runbook(self):
        self.assertIn("is not adjusted here", self.flat)
        self.assertNotIn("lower HARD_RESERVE", self.flat)

    #: The abort conditions that must exist by NAME. A row count is not a contract: deleting
    #: the "would write under $G" row still left eight rows and the count-based check passed,
    #: which is how the most important condition in the table could go missing unnoticed.
    REQUIRED_ABORTS = (
        ("write under `$G`", "the read-only boundary itself"),
        ("healthz", "service health"),
        ("load average", "the metadata scan's impact"),
        ("free space falls below", "capacity headroom during the run"),
        ("anchor domain", "provenance of everything collected"),
        ("repair intent", "an open WAL intent is an ops decision, never a step here"),
        ("unstable or unreachable", "the BLOCKED posture the 2026-08-19 run used"),
        ("production device", "a shadow root must not eat the free space G18 is blocked on"),
    )

    def test_the_runbook_carries_abort_conditions_with_reasons(self):
        table = self.doc[self.doc.index("## 7. Abort conditions"):self.doc.index("## 8.")]
        rows = [ln for ln in table.split("\n") if ln.startswith("| ") and "---" not in ln]
        for row in rows[1:]:
            with self.subTest(row[:50]):
                self.assertEqual(row.count("|"), 3, "every condition must carry a reason")

    def test_every_required_abort_condition_is_present_by_name(self):
        table = " ".join(
            self.doc[self.doc.index("## 7. Abort conditions"):self.doc.index("## 8.")].split())
        for needle, why in self.REQUIRED_ABORTS:
            with self.subTest(needle):
                self.assertIn(needle, table, f"missing abort condition covering {why}")


class TestTheDocumentsAgreeWithTheDeclarations(unittest.TestCase):

    def test_the_memo_matches_the_capacity_numbers(self):
        memo = " ".join(_read(MEMO).split())
        def fmt(x):
            """267.0 is written "267 GiB" in prose; compare the value, not its repr."""
            return str(int(x)) if float(x) == int(x) else str(x)
        for value in (fmt(rm.CAPACITY["free_now_gib"]), fmt(rm.CAPACITY["minimum_free_gib"]),
                      fmt(rm.CAPACITY["recommended_free_gib"]),
                      fmt(rm.CAPACITY["hard_reserve_gib"])):
            with self.subTest(value):
                self.assertIn(value, memo)

    def test_the_memo_and_the_spec_both_say_the_S7_gate_is_not_met(self):
        for path in (MEMO, SPEC, RUNBOOK):
            with self.subTest(os.path.basename(path)):
                flat = " ".join(_read(path).split())
                self.assertIn("NOT MET", flat.upper())

    #: Words that turn a "S7 gate ... PASS" span into a DENIAL of the claim.
    NEGATIONS = ("no ", "not ", "never", "cannot", "does not", "n't")

    def _affirmative_s7_pass(self, flat):
        """Occurrences of S7-gate-PASS that are not negated.

        A plain substring check flagged "No S8 result marks the S7 gate PASS" -- a sentence
        asserting the opposite of what the guard hunts for. A guard that fires on the sentence
        stating the rule is worse than none: the fix is to delete the sentence, which is
        precisely backwards."""
        out = []
        for m in re.finditer(r"S7[^.]{0,60}?PASS", flat):
            window = flat[max(0, m.start() - 60):m.end()].lower()
            if not any(n in window for n in self.NEGATIONS):
                out.append(flat[max(0, m.start() - 60):m.end() + 20])
        return out

    def test_no_document_records_an_S7_gate_pass(self):
        for path in (MEMO, SPEC, RUNBOOK, S7_DOC):
            with self.subTest(doc=os.path.basename(path)):
                self.assertEqual(self._affirmative_s7_pass(" ".join(_read(path).split())), [])

    def test_that_guard_still_catches_an_actual_claim(self):
        """Without this, the negation carve-out could swallow the real thing."""
        self.assertTrue(self._affirmative_s7_pass("the S7 gate is now PASS"))
        self.assertEqual(self._affirmative_s7_pass("No S8 result marks the S7 gate PASS"), [])

    def test_the_spec_carries_the_exception_and_its_limits(self):
        spec = " ".join(_read(SPEC).split())
        self.assertIn("P5-EX-1", spec)
        self.assertIn("S8 runbook authoring is allowed", spec)
        for limit in ("no** prune", "S10 still requires", "expires"):
            with self.subTest(limit):
                self.assertIn(limit, spec)

    def test_the_memo_records_the_next_approval_and_does_not_assume_it(self):
        memo = " ".join(_read(MEMO).split())
        self.assertIn("The exact next approval needed", memo)
        self.assertIn("not** implied by the P5-EX-1 exception", memo)

    def test_no_heading_appears_twice_in_any_new_document(self):
        """The duplicated-block failure that let two contradictory statuses coexist in S6."""
        for path in (MEMO, RUNBOOK):
            for level in ("## ", "### "):
                seen = [ln for ln in _read(path).split("\n")
                        if ln.startswith(level) and not ln.startswith(level + "#")]
                dupes = sorted({h for h in seen if seen.count(h) > 1})
                with self.subTest(doc=os.path.basename(path), level=level.strip()):
                    self.assertEqual(dupes, [], f"duplicated heading(s): {dupes}")

    def test_the_planning_documents_carry_no_local_capacity_claim(self):
        """A local synthetic figure quoted beside a capacity number gets read as one."""
        memo = " ".join(_read(MEMO).split())
        self.assertIn("no capacity claim derived from any local synthetic measurement", memo)
        self.assertNotIn("LOCAL_SYNTHETIC capacity", memo)


class TestTheReportIsSelfConsistent(unittest.TestCase):

    def setUp(self):
        self.r = rm.report()

    def test_the_report_is_labelled_planning_only(self):
        self.assertEqual(self.r["label"], "LOCAL_ONLY_PLANNING_DOCUMENT")
        for phrase in ("No VM24 contact", "no production action", "no cleanup"):
            self.assertIn(phrase.lower(), self.r["scope"].lower())

    def test_the_report_reproduces_the_declarations(self):
        self.assertEqual(self.r["s7_gate"]["verdict"], "NOT_MET")
        self.assertEqual(len(self.r["critical_path"]), 10)
        self.assertEqual(self.r["capacity"]["verdict"], "BLOCKED")
        self.assertEqual(sorted(self.r["s8_preflight_fields"]),
                         sorted(rm.S8_PREFLIGHT_FIELDS))

    def test_the_committed_artifact_reproduces_the_declarations(self):
        """The third artefact. A JSON left stale beside a corrected module is the drift this
        project has already shipped twice."""
        with open(os.path.join(_DEV, "bench", "results", "p5_critical_path.json")) as fh:
            committed = json.load(fh)
        self.assertEqual(committed, json.loads(json.dumps(rm.report(), sort_keys=True)),
                         "the committed roadmap artifact differs from ops/p5_roadmap.py — "
                         "regenerate it with `python -m ops.p5_roadmap`")

    def test_the_report_is_JSON_serialisable(self):
        """It is written to an artifact; a non-serialisable field fails at publish time."""
        json.dumps(self.r, sort_keys=True)


if __name__ == "__main__":
    unittest.main()
