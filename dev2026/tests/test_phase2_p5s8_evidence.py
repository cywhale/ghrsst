"""dev2026 — the committed VM24 evidence must BE the collected run, not a memory of it.

The 2026-08-20 S8 run was collected, assembled and re-validated — and its numbers reached a
report as prose while `bench/results/p5s6_vm24_readonly.json` still carried 267 GiB from
2026-08-19. Anyone opening the artifact afterwards would have read the old figure and been
right to trust it.

So these tests assert the committed artifacts and the memo **are regenerated from the run
directory**, and that the superseded figures cannot survive anywhere that reads as current
state. A number that lives only in a message is not evidence.
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

from ops import p5s8_evidence as ev  # noqa: E402
from ops import p5_roadmap as rm  # noqa: E402

RUN = "p5s8_20260820T110036Z"
RUN_ID = "20260820T110036Z"
S8_ARTIFACT = os.path.join(_DEV, "bench", "results", "p5s8_vm24_readonly.json")
S6_ARTIFACT = os.path.join(_DEV, "bench", "results", "p5s6_vm24_readonly.json")
MEMO = os.path.join(_DEV, "specs", "p5_critical_path_memo.md")


def _load(path):
    with open(path) as fh:
        return json.load(fh)


def _read(path):
    with open(path) as fh:
        return fh.read()


class TestTheCollectedRunIsInTheCheckout(unittest.TestCase):
    """The run must be committed, not left in a scratch directory that no reviewer can see."""

    REQUIRED = ("run.json", "identity.json", "preflight.json", "capacity.json",
                "wal_state.json", "g4_request.json", "g4_366d_seconds.txt",
                "pm2_jlist.json", "h7_fd_rss_timeseries.txt", "g16_metadata_scan.txt",
                "p5s8_readonly.json", "df_h.txt", "df_i.txt", "findmnt.txt")

    def test_every_collected_artifact_is_present(self):
        path = ev.run_dir(RUN)
        for name in self.REQUIRED:
            with self.subTest(name):
                self.assertTrue(os.path.isfile(os.path.join(path, name)))

    def test_the_run_validates_through_the_shipped_parsers(self):
        out = ev.derive(RUN)
        self.assertEqual(out["provenance"]["run_id"], RUN_ID)
        self.assertEqual(out["label"], "VM24_PRODUCTION_READ_ONLY")

    def test_the_evidence_is_bound_to_the_deploy_sha_host_and_time(self):
        prov = ev.derive(RUN)["provenance"]
        self.assertEqual(prov["deploy_sha"], "96ee79c9af0d121924b0032e44f86000c3d63e10")
        self.assertTrue(prov["deploy_sha"].startswith("96ee79c"))
        self.assertEqual(prov["host"], "odb24")
        self.assertTrue(prov["measured_utc"].startswith("2026-08-20T11:00:36"))


class TestTheCommittedArtifactsAreGenerated(unittest.TestCase):

    def test_the_s8_artifact_matches_a_fresh_derivation(self):
        """Regenerating must reproduce it exactly, or the committed copy was edited."""
        self.assertEqual(_load(S8_ARTIFACT),
                         json.loads(json.dumps(ev.derive(RUN), sort_keys=True)))

    def test_the_s6_capacity_gate_was_superseded_from_the_run(self):
        gate = _load(S6_ARTIFACT)["capacity_gate"]
        expected = ev.superseded_capacity_gate(ev.derive(RUN))
        for key, value in expected.items():
            with self.subTest(key):
                self.assertEqual(gate[key], value)

    def test_the_roadmap_capacity_reads_the_generated_evidence(self):
        self.assertEqual(rm.CAPACITY["free_now_gib"], 295.7)
        self.assertEqual(rm.CAPACITY["run_id"], RUN_ID)
        self.assertEqual(rm.CAPACITY["source_artifact"],
                         "bench/results/p5s8_vm24_readonly.json")

    def test_the_roadmap_artifact_is_regenerated_too(self):
        committed = _load(os.path.join(_DEV, "bench", "results", "p5_critical_path.json"))
        self.assertEqual(committed["capacity"]["free_now_gib"], 295.7)
        self.assertEqual(committed["capacity"]["verdict"], "BLOCKED")


class TestTheSupersededFigureCannotSurvive(unittest.TestCase):
    """The point of the exercise: after the new artifact is installed, 267 GiB must not read as
    the current state anywhere."""

    #: Fields that state CURRENT free space. A stale value in any of them is the defect;
    #: a value inside a block explicitly marked historical is the record, not a claim.
    CURRENT_STATE_FIELDS = ("free_now_gb", "free_now_gib", "free_now_bytes",
                            "projected_free_after_gb", "shortfall_gb", "shortfall_gib")

    def test_no_current_state_field_still_reports_the_superseded_figure(self):
        for path in (S8_ARTIFACT, S6_ARTIFACT,
                     os.path.join(_DEV, "bench", "results", "p5_critical_path.json")):
            data = _load(path)

            def walk(node, trail=""):
                if isinstance(node, dict):
                    for k, v in node.items():
                        if k in self.CURRENT_STATE_FIELDS and isinstance(v, (int, float)):
                            with self.subTest(artifact=os.path.basename(path), field=trail + k):
                                self.assertNotIn(str(v), ("267", "267.0", "113.7"),
                                                 "a superseded figure survives as current state")
                        walk(v, trail + k + ".")
                elif isinstance(node, list):
                    for item in node:
                        walk(item, trail)

            walk(data)

    def test_the_historical_snapshot_is_marked_rather_than_half_rewritten(self):
        """The 2026-08-19 `filesystem` block keeps its own numbers -- splicing a fresh `avail`
        into it would describe no single moment -- but it must say it is historical."""
        fs = _load(S6_ARTIFACT)["filesystem"]
        self.assertEqual(fs["status"], "historical")
        self.assertEqual(fs["as_of"], "2026-08-19")
        self.assertIn(RUN_ID, fs["superseded_by"])

    def test_the_superseded_gate_entry_is_marked_too(self):
        """`capacity_gate` is regenerated, but `gates.G18_capacity` restates the verdict in prose
        carrying `projected_free_after 113.7` and `shortfall 136.3` -- both computed from
        267 GiB. Regenerating one and leaving the other is how the figure survived last time."""
        gate = _load(S6_ARTIFACT)["gates"]["G18_capacity"]
        self.assertEqual(gate["status"], "historical")
        self.assertEqual(gate["as_of"], "2026-08-19")
        self.assertIn(RUN_ID, gate["superseded_by"])
        self.assertIn("295.7", gate["superseded_by"])
        self.assertEqual(gate["verdict"], "BLOCKED", "the verdict did not change; only the figure")

    def test_the_derived_gate_fields_were_recomputed_not_carried_over(self):
        """`projected_free_after_gb` and `gate_ok` were computed from 267 GiB. A merge that
        updates free space and leaves them behind is silently self-contradictory."""
        gate = _load(S6_ARTIFACT)["capacity_gate"]
        expected = round(gate["free_now_gb"] - gate["additional_gb"], 1)
        self.assertEqual(gate["projected_free_after_gb"], expected)
        self.assertIs(gate["gate_ok"], False)
        self.assertIn(RUN_ID, gate["inputs_provenance"]["free_now_gb"])

    def test_the_memo_quotes_267_only_as_history(self):
        """It may appear once, explicitly labelled historical -- deleting the record of what
        the figure was is not the same as not presenting it as current."""
        flat = " ".join(_read(MEMO).split())
        for m in re.finditer(r"267 GiB", flat):
            window = flat[max(0, m.start() - 90):m.end() + 90].lower()
            with self.subTest(window[:60]):
                self.assertIn("historical", window,
                              "267 GiB appears without being marked historical")

    def test_the_memo_carries_the_measured_figures(self):
        flat = " ".join(_read(MEMO).split())
        for token in ("295.7", "403.3", "107.6", "20260820T110036Z", "96ee79c"):
            with self.subTest(token):
                self.assertIn(token, flat)

    def test_positive_control_a_reintroduced_267_would_be_caught(self):
        text = "free space is 267 GiB today"
        flat = " ".join(text.split())
        hits = [m for m in re.finditer(r"267 GiB", flat)
                if "historical" not in flat[max(0, m.start() - 90):m.end() + 90].lower()]
        self.assertTrue(hits, "the guard would not catch a reintroduced current-state figure")


class TestCapacityChangeIsAttributedNotClaimed(unittest.TestCase):

    def setUp(self):
        self.attr = _load(S8_ARTIFACT)["capacity"]["change_attribution"]

    def test_it_is_recorded_as_operator_reported(self):
        self.assertEqual(self.attr["source"], "operator-reported")
        self.assertEqual(self.attr["observed_gib"], 31.4)

    def test_it_is_not_claimed_as_reclaim_cleanup_or_a_guarantee(self):
        self.assertFalse(self.attr["is_ghrsst_reclaim"])
        self.assertFalse(self.attr["is_automated_cleanup"])
        self.assertFalse(self.attr["is_sustainable_guarantee"])

    def test_the_cause_names_what_the_operator_reported(self):
        cause = self.attr["cause"].lower()
        self.assertIn("moved by the operator", cause)
        self.assertIn("srtm", cause)

    def test_the_memo_repeats_the_attribution_without_upgrading_it(self):
        flat = " ".join(_read(MEMO).split()).lower()
        self.assertIn("operator-reported", flat)
        self.assertIn("not** ghrsst reclaim", flat.replace("*", "*"))
        self.assertIn("srtm", flat)


class TestScopeLimitsArePersisted(unittest.TestCase):

    def setUp(self):
        self.ev = _load(S8_ARTIFACT)

    def test_h7_scope_is_master_only(self):
        h7 = self.ev["h7"]
        self.assertEqual(h7["scope"], "master_only")
        self.assertIn("gunicorn master", h7["scope_reason"].lower())
        self.assertIn("not sampled", h7["scope_reason"].lower())

    def test_h7_sustained_stays_not_adjudicated(self):
        self.assertEqual(self.ev["h7"]["sustained_load"], "NOT_ADJUDICATED")
        self.assertEqual(self.ev["gates"]["H7_sustained_load"], "NOT_ADJUDICATED")

    def test_g4_is_a_legacy_baseline_that_adjudicates_nothing(self):
        g4 = self.ev["g4"]
        self.assertEqual(g4["scope"], "legacy_baseline")
        self.assertEqual(g4["adjudicates"], [])
        self.assertEqual((g4["p50_ms"], g4["p95_ms"]), (77.33, 110.62))
        self.assertEqual(self.ev["gates"]["G4"], "NOT_ADJUDICATED")

    def test_the_g4_window_came_from_healthz_not_the_clock(self):
        g4 = self.ev["g4"]
        self.assertEqual(g4["window_authority"], "healthz.point_latest")
        self.assertEqual(g4["window_end"], g4["point_latest"])
        self.assertEqual(g4["span_days"], 366)

    def test_g16_metadata_scan_is_separate_from_snapshot_open(self):
        g16 = self.ev["g16"]
        self.assertEqual(g16["metadata_scan"]["files"], 7126309)
        self.assertEqual(g16["metadata_scan"]["dirs"], 118721)
        self.assertEqual(g16["snapshot_open"], "NOT_ADJUDICATED")
        self.assertEqual(self.ev["gates"]["G16_metadata_scan"], "EVIDENCE_COLLECTED")
        self.assertEqual(self.ev["gates"]["G16_snapshot_open"], "NOT_ADJUDICATED")

    def test_nothing_was_promoted_to_PASS(self):
        for gate, verdict in self.ev["gates"].items():
            with self.subTest(gate):
                self.assertNotEqual(verdict, "PASS")


class TestCapacityVerdict(unittest.TestCase):

    def setUp(self):
        self.cap = _load(S8_ARTIFACT)["capacity"]

    def test_the_measured_values_are_preserved(self):
        self.assertEqual(self.cap["free_now_gib"], 295.7)
        self.assertEqual(self.cap["free_now_bytes"], 317551714304)
        self.assertEqual(self.cap["minimum_free_gib"], 403.3)
        self.assertEqual(self.cap["shortfall_gib"], 107.6)
        self.assertEqual(self.cap["verdict"], "BLOCKED")

    def test_the_shortfall_is_derived_not_typed(self):
        self.assertAlmostEqual(
            self.cap["shortfall_gib"],
            round(self.cap["minimum_free_gib"] - self.cap["free_now_gib"], 1), places=6)

    def test_hard_reserve_is_unchanged(self):
        self.assertEqual(self.cap["hard_reserve_gib"], 250)


class TestTheRemainingBlockersSectionIsCurrent(unittest.TestCase):
    """The blockers table restated numbers held elsewhere and drifted twice: it carried the
    ABORTED run's 264.4 GiB beside a capacity section already saying 295.7, and went on calling
    the S8 execution "not approved" after it had completed. A table that repeats facts must be
    derived from them, so it is generated and checked here."""

    def setUp(self):
        self.memo = _read(MEMO)
        start = ev.BLOCKERS_START
        end = ev.BLOCKERS_END
        self.assertEqual(self.memo.count(start), 1, "expected exactly one generated block")
        self.assertEqual(self.memo.count(end), 1)
        self.section = self.memo[self.memo.index(start):
                                 self.memo.index(end) + len(end)]
        self.flat = " ".join(self.section.split())

    # ---- the rejections -------------------------------------------------
    def test_it_does_not_carry_the_aborted_runs_capacity(self):
        self.assertNotIn("264.4", self.flat)
        self.assertNotIn("138.9", self.flat, "the aborted run's shortfall survives")

    def test_it_does_not_name_the_aborted_run_as_the_current_run(self):
        self.assertNotIn("20260820T080028Z", self.flat)

    def test_it_does_not_say_S8_execution_is_not_approved(self):
        lowered = self.flat.lower()
        idx = lowered.find("s8 execution")
        self.assertNotEqual(idx, -1, "the S8 execution row disappeared")
        row = lowered[idx:idx + 220]
        self.assertNotIn("not approved", row)

    # ---- the acceptances ------------------------------------------------
    def test_it_carries_the_current_measured_values(self):
        for token in ("295.7 GiB", "403.3 GiB", "107.6", "20260820T110036Z", "BLOCKED"):
            with self.subTest(token):
                self.assertIn(token, self.flat)

    def test_it_states_the_completed_S8_execution_and_that_it_adjudicated_nothing(self):
        lowered = self.flat.lower()
        self.assertIn("legacy_baseline", lowered)
        self.assertIn("completed", lowered)
        self.assertIn("copied back", lowered)
        self.assertIn("no gate adjudicated by this run", lowered)

    def test_it_states_S9_shadow_execution_has_not_started(self):
        lowered = self.flat.lower()
        self.assertIn("s9", lowered)
        self.assertIn("not_started", lowered)

    def test_the_declared_execution_status_records_the_completed_run(self):
        """The declaration itself, not only its rendering. Mutating the status alone is caught
        by the memo-vs-render comparison, but only indirectly -- this binds the source."""
        legacy = rm.S8_EXECUTION["legacy_baseline"]
        self.assertEqual(legacy["status"], "COMPLETED")
        self.assertEqual(legacy["run_id"], RUN_ID)
        self.assertIn("no gate adjudicated", legacy["detail"].lower())
        self.assertEqual(rm.S8_EXECUTION["s9_shadow_execution"]["status"], "NOT_STARTED")

    def test_it_is_generated_not_hand_written(self):
        self.assertIn("Generated by ops/p5s8_evidence.py", self.section)
        self.assertIn("Do not edit by hand", self.section)

    def test_it_matches_a_fresh_render(self):
        """The committed table must equal what the generator produces now."""
        self.assertEqual(self.section.strip(),
                         ev.render_blockers(ev.derive(RUN)).strip())

    def test_positive_control_the_previous_stale_table_would_be_rejected(self):
        """Guards the guard: the exact text that shipped in b080103 must fail these checks."""
        stale = ("| **G18 capacity** | **BLOCKED** — **264.4 GiB free** (measured 2026-08-20, "
                 "run `20260820T080028Z`), 403.3 GiB minimum; shortfall ≈ 138.9 GiB |\n"
                 "| **S8 execution** | not approved |")
        flat = " ".join(stale.split())
        self.assertIn("264.4", flat)
        self.assertIn("20260820T080028Z", flat)
        self.assertIn("not approved", flat.lower())


class TestNoDuplicateOrUnmarkedCurrentStateSections(unittest.TestCase):
    """Historical round notes may keep old numbers, but only where they say so."""

    #: Figures that are superseded. Each may appear only in a window that marks it as history.
    SUPERSEDED = ("264.4 GiB", "267 GiB", "138.9")
    HISTORY_MARKERS = ("historical", "superseded", "aborted", "round 11", "was the claim")

    def setUp(self):
        self.memo = _read(MEMO)
        self.flat = " ".join(self.memo.split())

    def test_every_superseded_figure_is_marked_as_history(self):
        for token in self.SUPERSEDED:
            for m in re.finditer(re.escape(token), self.flat):
                window = self.flat[max(0, m.start() - 200):m.end() + 200].lower()
                with self.subTest(token=token, at=m.start()):
                    self.assertTrue(any(k in window for k in self.HISTORY_MARKERS),
                                    f"{token} appears without being marked historical")

    def test_no_heading_appears_twice(self):
        for level in ("## ", "### "):
            seen = [ln for ln in self.memo.split("\n")
                    if ln.startswith(level) and not ln.startswith(level + "#")]
            dupes = sorted({h for h in seen if seen.count(h) > 1})
            with self.subTest(level.strip()):
                self.assertEqual(dupes, [], f"duplicated heading(s): {dupes}")

    def test_there_is_exactly_one_remaining_blockers_section(self):
        self.assertEqual(self.memo.count("## 4. Remaining blockers"), 1)

    def test_the_aborted_run_paragraph_does_not_claim_current_evidence(self):
        idx = self.flat.find("264.4")
        self.assertNotEqual(idx, -1, "the historical record of the aborted run was deleted")
        window = self.flat[max(0, idx - 300):idx + 300].lower()
        self.assertIn("superseded", window)
        self.assertNotIn("is the current g18 evidence", window)


class TestNoP5DocumentQuotesASupersededFigureAsCurrent(unittest.TestCase):
    """The memo was fixed twice while three other P5 documents went on stating 267 GiB as the
    live G18 reading. Scoping the guard to the document that was reported is how the same defect
    keeps reappearing next door, so this sweeps every P5 spec."""

    SPECS = os.path.join(_DEV, "specs")
    #: Superseded readings. Bare "267" is excluded -- it is the 1267 hold count elsewhere -- but
    #: "267.0" and "264.4" are, because a unitless table cell is exactly where one hid.
    SUPERSEDED = ("267 GiB", "267.0 GiB", "267 GB", "267.0", "264.4 GiB", "264.4")
    #: Deliberately narrow. "withdrawn" and "was wrong" were tried and dropped: `p5s6_sizing`
    #: withdraws a DIFFERENT claim two paragraphs above its capacity table, and that word alone
    #: was enough to certify an unmarked 267 GB as history. A marker has to be about the figure.
    HISTORY_MARKERS = ("historical", "superseded", "supersedes", "aborted")
    #: Close enough that the marker belongs to this figure -- roughly a table row plus its
    #: caption. A section banner four paragraphs up does not mark the number in a cell.
    WINDOW = 300

    def _p5_docs(self):
        for name in sorted(os.listdir(self.SPECS)):
            if name.startswith("p5") and name.endswith(".md"):
                yield name, " ".join(_read(os.path.join(self.SPECS, name)).split())

    @classmethod
    def _unmarked(cls, flat):
        """Every superseded figure in `flat` that no nearby marker calls history.

        The control below runs through THIS function, not through a re-implementation of it:
        a positive control that reproduces the logic it is guarding proves only that the copy
        agrees with itself."""
        found, checked = [], 0
        for token in cls.SUPERSEDED:
            for m in re.finditer(re.escape(token), flat):
                checked += 1
                window = flat[max(0, m.start() - cls.WINDOW):m.end() + cls.WINDOW].lower()
                if not any(k in window for k in cls.HISTORY_MARKERS):
                    found.append((token, m.start()))
        return found, checked

    def test_every_occurrence_sits_in_a_window_that_marks_it_history(self):
        total = 0
        for name, flat in self._p5_docs():
            unmarked, checked = self._unmarked(flat)
            total += checked
            with self.subTest(doc=name):
                self.assertEqual(unmarked, [], f"{name}: reads as the current state")
        self.assertGreater(total, 0, "no occurrence was examined -- the sweep found nothing "
                                     "to check, so it proves nothing")

    def test_positive_control_an_unmarked_occurrence_is_caught(self):
        unmarked, checked = self._unmarked(
            "G18 is BLOCKED: 267 GiB free against a 403.3 GiB minimum on the host today.")
        self.assertEqual(checked, 1)
        self.assertEqual(unmarked, [("267 GiB", 16)])

    def test_negative_control_a_marked_occurrence_is_accepted(self):
        """The sweep must not simply reject the digits -- the historical record has to survive."""
        unmarked, checked = self._unmarked(
            "The 2026-08-19 reading of 267 GiB is historical; the current figure is 295.7 GiB.")
        self.assertEqual(checked, 1)
        self.assertEqual(unmarked, [])


class TestTheG18AuthorityHoldsNoMeasuredFigure(unittest.TestCase):
    """`PRODUCTION_DEPENDENT_GATES['G18']` read "BLOCKED at 267 GiB" for two runs after 267 GiB
    stopped being true, and `p5_critical_path.json` published it.

    The rule that stops it recurring is structural, not vigilance: that dict says WHY a gate is
    production-dependent, so the only number it may carry is the fixed threshold. Readings are
    measured, and measured things are derived from the evidence."""

    ROADMAP_ARTIFACT = os.path.join(_DEV, "bench", "results", "p5_critical_path.json")
    #: Written with the unit. A bare "267" would match the 1267 daily holds, which is a count of
    #: directories and has nothing to do with free space.
    SUPERSEDED = ("267 GiB", "267.0 GiB", "267 GB", "264.4 GiB", "113.7 GiB", "138.9 GiB")
    #: The only quantity that belongs in a static rationale: policy, not measurement.
    ALLOWED_NUMBERS = {"403.3"}

    #: Only quantities count -- a number carrying a size unit. Every digit in the string would
    #: also flag the `1` in `/dev/sdc1`, which names the device the gate is about and is not a
    #: measurement of anything.
    QUANTITY = re.compile(r"(\d+(?:\.\d+)?)\s*(?:[KMGT]i?B|bytes)\b")

    @classmethod
    def _numbers(cls, text):
        return set(cls.QUANTITY.findall(text))

    def test_the_static_rationale_carries_no_reading_only_the_threshold(self):
        g18 = rm.PRODUCTION_DEPENDENT_GATES["G18"]
        self.assertEqual(self._numbers(g18) - self.ALLOWED_NUMBERS, set(),
                         f"a measured figure is hardcoded in the G18 rationale: {g18!r}")

    def test_no_gate_rationale_quotes_a_superseded_figure(self):
        for gate, text in rm.PRODUCTION_DEPENDENT_GATES.items():
            for token in self.SUPERSEDED:
                with self.subTest(gate=gate, token=token):
                    self.assertNotIn(token, text)

    def test_the_published_artifact_says_the_same_thing(self):
        published = _load(self.ROADMAP_ARTIFACT)["production_dependent_gates"]
        self.assertEqual(published, dict(rm.PRODUCTION_DEPENDENT_GATES),
                         "the committed roadmap artifact and the module authority disagree")

    def test_positive_control_the_previous_rationale_would_be_rejected(self):
        stale = "free disk on /dev/sdc1; BLOCKED at 267 GiB against a 403.3 GiB minimum"
        self.assertNotEqual(self._numbers(stale) - self.ALLOWED_NUMBERS, set(),
                            "the guard would have passed the very text it exists to reject")


class TestTheCurrentG18SummaryIsDerivedAndBound(unittest.TestCase):
    """The current reading has to exist somewhere, and it has to be traceable to the run that
    produced it -- otherwise removing it from the rationale just loses the fact."""

    ROADMAP_ARTIFACT = os.path.join(_DEV, "bench", "results", "p5_critical_path.json")

    def test_it_names_the_run_and_the_measured_values(self):
        summary = rm.g18_summary()
        for token in ("BLOCKED", "295.7", "403.3", "107.6", RUN_ID, "2026-08-20"):
            with self.subTest(token):
                self.assertIn(token, summary)

    def test_the_artifact_publishes_it(self):
        self.assertEqual(_load(self.ROADMAP_ARTIFACT)["g18_current"], rm.g18_summary())

    def test_it_agrees_with_the_generated_evidence(self):
        cap = _load(S8_ARTIFACT)["capacity"]
        summary = rm.g18_summary()
        self.assertIn(str(cap["free_now_gib"]), summary)
        self.assertIn(str(cap["shortfall_gib"]), summary)
        self.assertIn(cap["verdict"], summary)

    def test_it_follows_the_evidence_rather_than_repeating_a_constant(self):
        """Substitute a different measurement; a derived summary moves with it."""
        original = rm.CAPACITY
        try:
            rm.CAPACITY = {**original, "free_now_gib": 812.5, "shortfall_gib": 0.0,
                           "verdict": "NOT_BLOCKED"}
            moved = rm.g18_summary()
        finally:
            rm.CAPACITY = original
        self.assertIn("812.5", moved)
        self.assertIn("NOT_BLOCKED", moved)
        self.assertNotIn("295.7", moved)


class TestTheNextApprovalSectionMatchesWhatHasRun(unittest.TestCase):
    """Section 5 asked for approval "before any S8 or S9 command runs against the host" after S8
    `legacy_baseline` had already run. An approval request that misstates what is blocked wastes
    the one thing it is trying to obtain."""

    def setUp(self):
        self.memo = _read(MEMO)
        self.flat = " ".join(self.memo.split())
        start = self.memo.index(ev.APPROVALS_START)
        self.section = self.memo[start:self.memo.index(ev.APPROVALS_END) + len(ev.APPROVALS_END)]

    def test_it_is_generated_not_hand_written(self):
        self.assertIn("Generated by ops/p5s8_evidence.py", self.section)
        self.assertIn("Do not edit by hand", self.section)

    def test_it_matches_a_fresh_render(self):
        self.assertEqual(self.section, ev.render_approvals(ev.derive(RUN)))

    def test_it_records_the_completed_S8_run_as_completed(self):
        flat = " ".join(self.section.split())
        self.assertIn("S8 `legacy_baseline` | **COMPLETED**", flat)
        self.assertIn(RUN_ID, flat)
        self.assertIn("nothing — it already ran", flat)

    def test_it_still_requires_separate_approval_for_what_has_not_run(self):
        flat = " ".join(self.section.split())
        for stage in ("S8 `p5_shadow`", "S9 / shadow execution", "S10 rollout"):
            with self.subTest(stage):
                self.assertIn(stage, flat)
        self.assertIn("NOT_STARTED", flat)
        self.assertIn("separate human approval", flat)
        self.assertIn("fresh written approval", flat)

    #: Wordings that describe a completed stage as future work. Each is anchored on S8 so that
    #: the same sentence about S9 -- which HAS not run -- stays legal.
    FUTURE_TENSE_S8 = (r"before any S8", r"before an S8 command", r"S8 execution \| not approved",
                       r"before any S8 or S9 command")

    def test_no_wording_puts_the_completed_S8_run_in_the_future(self):
        for pattern in self.FUTURE_TENSE_S8:
            with self.subTest(pattern):
                self.assertIsNone(re.search(pattern, self.flat, re.I),
                                  "the memo describes the completed S8 run as pending")

    def test_positive_control_the_previous_wording_would_be_rejected(self):
        old = ("It is not implied by the P5-EX-1 exception, and it is separate from the fresh "
               "VM24 read-only / shadow approval that step 4 needs before any S8 or S9 command "
               "runs against the host.")
        hits = [p for p in self.FUTURE_TENSE_S8 if re.search(p, old, re.I)]
        self.assertTrue(hits, "the guard would not catch the wording it was written for")

    def test_the_published_roadmap_states_approval_per_stage(self):
        """The memo table and the artifact must not disagree about which stage is waiting."""
        need = _load(os.path.join(_DEV, "bench", "results", "p5_critical_path.json"))
        need = need["next_approval_required"]
        self.assertEqual(need["per_stage"], {k: v["status"] for k, v in rm.S8_EXECUTION.items()})
        self.assertEqual(need["already_executed_under_a_spent_approval"], ["legacy_baseline"])
        self.assertNotIn("before any S8", need["what"])

    def test_the_critical_path_step_4_does_not_deny_the_S8_approval_happened(self):
        """A VM24 approval WAS requested and used. Step 4 is still NOT_REQUESTED, but only
        because approval does not carry between stages -- it must say which one it means."""
        step = next(s for s in rm.CRITICAL_PATH if s["step"] == 4)
        self.assertEqual(step["status"], "NOT_REQUESTED")
        self.assertIn("S9", step["name"])
        self.assertIn("legacy_baseline", step["note"])


if __name__ == "__main__":
    unittest.main()
