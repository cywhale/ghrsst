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

import contextlib
import io
import json
import os
import re
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
for p in (_DEV, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from ops import p5s8_evidence as ev  # noqa: E402
from ops import p5_roadmap as rm  # noqa: E402
from ops.p5s8_report import ArtifactError  # noqa: E402

#: Sentinel for "delete this key", so a test can distinguish an absent field from a `None` one.
_MISSING = object()

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

    #: Keys the supersession ADDS. Stripping them is how the test below distinguishes a mark
    #: the generator produced this run from one it inherited from its own previous output.
    ADDED_BY_SUPERSESSION = ("as_of", "status", "superseded_by")

    def test_the_marks_are_produced_by_the_generator_not_inherited_from_itself(self):
        """Deleting a whole supersession step and regenerating used to pass.

        `main()` rewrites the artifact from the copy it just read, so a step that adds keys
        looks like a no-op once its keys are in the committed file -- the generator preserves
        its own output and gets credited with producing it. Here the marks are stripped first,
        so anything the code no longer applies goes missing instead of being carried."""
        committed = _load(S6_ARTIFACT)
        stripped = json.loads(json.dumps(committed))
        for block in (stripped["filesystem"], stripped["gates"]["G18_capacity"]):
            for key in self.ADDED_BY_SUPERSESSION:
                block.pop(key, None)
        stripped.pop("capacity_gate_superseded_note", None)
        stripped["hold_lifecycle_audit"]["capacity"].pop("free_now_source", None)

        self.assertNotEqual(stripped, committed, "nothing was stripped -- the test is vacuous")
        self.assertEqual(ev.supersede_s6(stripped, ev.derive(RUN)), committed,
                         "re-applying the supersession does not reproduce the committed file")

    def test_the_supersession_does_not_mutate_its_input(self):
        """It returns a new record; a caller reading the old one must still see the old one."""
        before = _load(S6_ARTIFACT)
        snapshot = json.loads(json.dumps(before))
        ev.supersede_s6(before, ev.derive(RUN))
        self.assertEqual(before, snapshot)

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


class TestTheCapacityModelIsCanonicalNotInherited(unittest.TestCase):
    """`supersede_s6` read `additional_gb` back out of the artifact it was regenerating.

    Editing it to 0 there produced `projected_free_after_gb = 295.7` and `gate_ok = True` on the
    next run: a G18 pass manufactured by editing a file, with free space unchanged. The artifact
    was an input to its own regeneration. Every model term now comes from `CAPACITY_MODEL`, and
    a surviving copy that disagrees is refused rather than corrected in silence."""

    def setUp(self):
        self.s6 = _load(S6_ARTIFACT)
        self.evidence = ev.derive(RUN)

    def _with(self, **fields):
        s6 = json.loads(json.dumps(self.s6))
        for key, value in fields.items():
            if value is _MISSING:
                s6["capacity_gate"].pop(key, None)
            else:
                s6["capacity_gate"][key] = value
        return s6

    def _refusal(self, **fields):
        with self.assertRaises(ArtifactError) as caught:
            ev.supersede_s6(self._with(**fields), self.evidence)
        return str(caught.exception)

    # ---------------------------------------------------------------- positive control
    def test_the_canonical_model_is_accepted_and_recomputed(self):
        gate = ev.supersede_s6(self.s6, self.evidence)["capacity_gate"]
        canonical = ev.capacity_model()
        for key, want in canonical.items():
            with self.subTest(key):
                self.assertEqual(gate[key], want)
        self.assertEqual(gate["additional_gb"], 153.3)
        self.assertEqual(gate["projected_free_after_gb"], 142.4)
        self.assertIs(gate["gate_ok"], False)
        self.assertEqual(gate["verdict"], "BLOCKED")

    def test_the_model_terms_are_derived_from_their_inputs(self):
        """145.8 is S x the base rate and 5.5 is the ingest rate x the build days -- neither is
        a number typed beside the thing it is supposed to follow from."""
        m, canonical = ev.CAPACITY_MODEL, ev.capacity_model()
        self.assertEqual(canonical["new_block_gb"], round(m["s"] * m["base_gb_per_day"], 1))
        self.assertEqual(canonical["growth_during_build_gb"],
                         round(m["ingest_gb_per_day"] * m["build_days_assumed"], 1))
        self.assertEqual(canonical["additional_gb"],
                         round(canonical["new_block_gb"]
                               + canonical["temp_checkpoint_artifacts_gb"]
                               + canonical["growth_during_build_gb"], 1))

    def test_the_threshold_is_the_reserve_plus_the_model_and_is_unchanged(self):
        self.assertEqual(ev.HARD_RESERVE_GIB, 250)
        self.assertEqual(ev.MINIMUM_FREE_GIB, 403.3)
        self.assertEqual(ev.MINIMUM_FREE_GIB,
                         round(ev.HARD_RESERVE_GIB + ev.capacity_model()["additional_gb"], 1))

    # ---------------------------------------------------------------- negative controls
    def test_additional_gb_zero_is_refused(self):
        """The reported reproduction, in both spellings a hand edit produces."""
        self.assertIn("additional_gb", self._refusal(additional_gb=0))
        reason = self._refusal(additional_gb=0.0)
        self.assertIn("additional_gb", reason)
        self.assertIn("153.3", reason)

    def test_a_negative_additional_gb_is_refused(self):
        reason = self._refusal(additional_gb=-153.3)
        self.assertIn("-153.3", reason)
        self.assertIn("canonical model", reason)

    def test_a_string_additional_gb_is_refused_without_coercion(self):
        reason = self._refusal(additional_gb="153.3")
        self.assertIn("str", reason)
        self.assertIn("No coercion", reason)

    def test_a_bool_additional_gb_is_refused_as_a_bool(self):
        """`True` is an `int` in Python, so a numeric check alone would let it through.

        The reason has to name that, not just report a type difference: mutation testing showed
        that deleting the dedicated bool guard survived a check that only asserted "bool" was
        somewhere in the message, because the generic type mismatch happens to print it too."""
        reason = self._refusal(additional_gb=True)
        self.assertIn("is a bool", reason)
        self.assertIn("bool is an int in Python", reason)

    def test_the_canonical_value_is_used_even_if_the_validator_is_bypassed(self):
        """Defence in depth, tested separately on purpose.

        With the validator stubbed out, an edited artifact must STILL not move the arithmetic.
        Restoring the old `float(old_gate.get("additional_gb", 153.3))` survived every other
        test in this class, because the validator refused the doctored input before that line
        ran. The guarantee then rests entirely on one check, and narrowing it silently restores
        the defect. The value used is canonical whether or not anything objected."""
        original = ev._check_capacity_model
        try:
            ev._check_capacity_model = lambda *a, **k: None
            gate = ev.supersede_s6(self._with(additional_gb=0.0),
                                   self.evidence)["capacity_gate"]
        finally:
            ev._check_capacity_model = original
        self.assertEqual(gate["additional_gb"], 153.3)
        self.assertEqual(gate["projected_free_after_gb"], 142.4)
        self.assertIs(gate["gate_ok"], False)

    def test_an_int_additional_gb_is_refused_as_the_wrong_raw_type(self):
        reason = self._refusal(additional_gb=153)
        self.assertIn("int", reason)
        self.assertIn("float", reason)

    def test_a_missing_additional_gb_is_refused_not_defaulted(self):
        """The old code fell back to a literal 153.3, so deleting the field looked like health."""
        reason = self._refusal(additional_gb=_MISSING)
        self.assertIn("missing", reason)
        self.assertIn("default", reason)

    def test_each_component_term_is_checked_too(self):
        """`additional` alone is not the model: the terms it is built from survive as well."""
        for key, bad in (("new_block_gb", 0.0), ("temp_checkpoint_artifacts_gb", 0.0),
                         ("growth_during_build_gb", 0.0), ("build_days_assumed", 0.0),
                         ("s", 30), ("formula", "projected_free_after = free_now")):
            with self.subTest(key):
                self.assertIn(key, self._refusal(**{key: bad}))
        for key in ("new_block_gb", "temp_checkpoint_artifacts_gb", "growth_during_build_gb",
                    "build_days_assumed", "s", "formula"):
            with self.subTest(missing=key):
                self.assertIn("missing", self._refusal(**{key: _MISSING}))

    def test_gate_ok_cannot_become_true_at_the_measured_free_space(self):
        """The whole point. Every edit that made `gate_ok` True is refused, and the one path
        that could still write it -- an evidence record with a lowered threshold -- is refused
        by `_check_thresholds` rather than believed."""
        for fields in ({"additional_gb": 0.0}, {"additional_gb": 0}, {"additional_gb": -153.3},
                       {"new_block_gb": 0.0}, {"growth_during_build_gb": 0.0},
                       {"temp_checkpoint_artifacts_gb": 0.0}, {"additional_gb": _MISSING}):
            with self.subTest(**{k: repr(v) for k, v in fields.items()}):
                with self.assertRaises(ArtifactError):
                    ev.supersede_s6(self._with(**fields), self.evidence)

        for key, bad in (("hard_reserve_gib", 100), ("minimum_free_gib", 200.0)):
            doctored = json.loads(json.dumps(self.evidence))
            doctored["capacity"][key] = bad
            with self.subTest(threshold=key):
                with self.assertRaises(ArtifactError) as caught:
                    ev.supersede_s6(self.s6, doctored)
                self.assertIn(key, str(caught.exception))

        gate = ev.supersede_s6(self.s6, self.evidence)["capacity_gate"]
        self.assertIs(gate["gate_ok"], False, "295.7 GiB is below the 403.3 GiB minimum")

    def test_a_threshold_that_drifts_from_the_model_is_refused(self):
        """The reserve and the minimum are two statements of one fact: `projected >= reserve`
        and `free_now >= minimum` must agree. Desynchronise them -- the way hardcoding the
        minimum back to a literal would -- and no artifact is written."""
        original = ev.MINIMUM_FREE_GIB
        doctored = json.loads(json.dumps(self.evidence))
        doctored["capacity"]["minimum_free_gib"] = 200.0
        try:
            ev.MINIMUM_FREE_GIB = 200.0
            with self.assertRaises(ArtifactError) as caught:
                ev.supersede_s6(self.s6, doctored)
            self.assertIn("contradicts", str(caught.exception))
        finally:
            ev.MINIMUM_FREE_GIB = original

    def test_the_option_a_proof_text_follows_the_authoritative_values(self):
        """The proof obligation said "free_now >= 403.3 GiB (600 GiB recommended)" in prose
        while `requirement_gib` beside it was derived. A number in a sentence ages exactly like
        a number in a field and is harder to spot, so both come from the same reading."""
        altered = {**rm.CAPACITY, "minimum_free_gib": 512.5, "recommended_free_gib": 900.0}
        options = rm._capacity_options(altered)
        option_a = options[0]
        self.assertEqual(option_a["option"], "A")
        self.assertEqual(option_a["requirement_gib"], 512.5)
        self.assertEqual(option_a["recommended_gib"], 900.0)

        proof = " ".join(option_a["proof_obligations"])
        self.assertIn("512.5", proof)
        self.assertIn("900", proof)
        for stale in ("403.3", "600"):
            with self.subTest(stale):
                self.assertNotIn(stale, proof,
                                 "the proof text kept a threshold the reading no longer has")

    def test_the_committed_proof_text_matches_the_committed_reading(self):
        option_a = rm.CAPACITY_OPTIONS[0]
        proof = " ".join(option_a["proof_obligations"])
        self.assertIn(str(rm.CAPACITY["minimum_free_gib"]), proof)
        self.assertIn(str(int(rm.CAPACITY["recommended_free_gib"])), proof)
        self.assertEqual(option_a["requirement_gib"], ev.MINIMUM_FREE_GIB)

    def test_the_gate_rationale_carries_no_quantity_at_all(self):
        """Not even the threshold. It was kept as "policy", but it is DERIVED from the capacity
        model now, so a model change ages it exactly like a reading."""
        g18 = rm.PRODUCTION_DEPENDENT_GATES["G18"]
        self.assertEqual(TestTheG18AuthorityHoldsNoMeasuredFigure._numbers(g18), set())
        self.assertNotIn("403.3", g18)

    def test_the_committed_artifact_carries_the_canonical_model(self):
        gate = self.s6["capacity_gate"]
        for key, want in ev.capacity_model().items():
            with self.subTest(key):
                self.assertEqual(gate[key], want)
        self.assertEqual(self.evidence["capacity"]["model"], ev.capacity_model())
        self.assertEqual(rm.CAPACITY["additional_gib"], ev.capacity_model()["additional_gb"])


class TestTheRenderedReportIsBoundToTheEvidenceRun(unittest.TestCase):
    """Both renderers state a run's outcome with figures from the evidence and a status from
    `S8_EXECUTION`. Nothing tied the two together, so regenerating from a NEW run while the
    declaration still named the old one produced one coherent-looking table built from two."""

    def setUp(self):
        self.evidence = ev.derive(RUN)

    def _evidence_for_run(self, run_id):
        other = json.loads(json.dumps(self.evidence))
        other["provenance"]["run_id"] = run_id
        return other

    def test_valid_control_the_matching_run_renders(self):
        self.assertEqual(ev.execution_bound_to(self.evidence)["legacy_baseline"]["run_id"],
                         RUN_ID)
        for render in (ev.render_blockers, ev.render_approvals):
            with self.subTest(render.__name__):
                self.assertIn(RUN_ID, render(self.evidence))

    def test_a_later_run_against_the_old_declaration_is_refused(self):
        newer = self._evidence_for_run("20260901T090000Z")
        for render in (ev.render_blockers, ev.render_approvals, ev.execution_bound_to):
            with self.subTest(render.__name__):
                with self.assertRaises(ArtifactError) as caught:
                    render(newer)
                reason = str(caught.exception)
                self.assertIn(RUN_ID, reason)
                self.assertIn("20260901T090000Z", reason)

    def test_a_declaration_with_no_run_id_is_refused(self):
        original = dict(rm.S8_EXECUTION["legacy_baseline"])
        try:
            rm.S8_EXECUTION["legacy_baseline"] = {k: v for k, v in original.items()
                                                  if k != "run_id"}
            with self.assertRaises(ArtifactError):
                ev.execution_bound_to(self.evidence)
        finally:
            rm.S8_EXECUTION["legacy_baseline"] = original

    def test_an_uncompleted_declaration_is_not_bound_to_a_run(self):
        """A profile that has not run has no run to agree with; only COMPLETED is checked."""
        original = dict(rm.S8_EXECUTION["legacy_baseline"])
        try:
            rm.S8_EXECUTION["legacy_baseline"] = {**original, "status": "NOT_STARTED"}
            ev.execution_bound_to(self._evidence_for_run("20260901T090000Z"))
        finally:
            rm.S8_EXECUTION["legacy_baseline"] = original


class TestRegenerationUpdatesEveryDependentOutput(unittest.TestCase):
    """One command must leave every output describing the SAME measurement.

    It did not. `python -m ops.p5s8_evidence` wrote the S8 evidence, the S6 supersession and the
    memo; `p5_critical_path.json` was written only by `ops.p5_roadmap`, so a new evidence run
    updated three outputs and left the fourth quoting the previous one. Asserting the committed
    295.7 could never have caught that -- every output already said 295.7. So this builds an
    ALTERNATE run with different free space, runs the real entry point into a temporary
    checkout, and demands all four move together."""

    #: Above the 403.3 GiB minimum on purpose. A pipeline that only ever sees a blocked reading
    #: cannot show whether "BLOCKED" is derived or written down -- and it was written down.
    ALT_RUN_ID = "20260901T090000Z"
    ALT_FREE_BYTES = 420 * (1024 ** 3)
    ALT_FREE_GIB = 420.0
    #: Below the minimum, and different from the committed figure.
    BLOCKED_RUN_ID = "20260902T090000Z"
    BLOCKED_FREE_BYTES = int(320.0 * (1024 ** 3))
    BLOCKED_FREE_GIB = 320.0

    def _checkout(self, run_id, free_bytes):
        """A temporary repository root holding an alternate run, ready to regenerate into."""
        root = tempfile.mkdtemp(prefix="p5s8-regen-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        results = os.path.join(root, "bench", "results")
        os.makedirs(os.path.join(root, "specs"))
        os.makedirs(results)
        for name in ("p5s8_vm24_readonly.json", "p5s6_vm24_readonly.json",
                     "p5_critical_path.json"):
            shutil.copy(os.path.join(_DEV, "bench", "results", name),
                        os.path.join(results, name))
        shutil.copy(MEMO, os.path.join(root, "specs", "p5_critical_path_memo.md"))

        # The run directory is the real collected one with its stamps rewritten, so every
        # shipped parser still has to accept it -- a hand-built stub would prove nothing.
        src = os.path.join(_DEV, "bench", "results", "p5s8_runs", RUN)
        alt = os.path.join(results, "p5s8_runs", f"p5s8_{run_id}")
        shutil.copytree(src, alt)
        for name in sorted(os.listdir(alt)):
            path = os.path.join(alt, name)
            with open(path) as fh:
                text = fh.read()
            text = text.replace(RUN_ID, run_id)
            if name == "capacity.json":
                blob = json.loads(text)
                blob["free_now_bytes"] = free_bytes
                text = json.dumps(blob, indent=2, sort_keys=True)
            with open(path, "w") as fh:
                fh.write(text)
        return root, f"p5s8_{run_id}"

    def _regenerate(self, run_id, free_bytes):
        """Run the real entry point against the alternate run, then read back all four outputs.

        `S8_EXECUTION` is repointed at the alternate run because `execution_bound_to` refuses a
        COMPLETED declaration naming a different run -- that guard working, not a workaround."""
        root, run = self._checkout(run_id, free_bytes)
        original_exec = dict(rm.S8_EXECUTION["legacy_baseline"])
        original_capacity = rm.CAPACITY
        original_options = rm.CAPACITY_OPTIONS
        try:
            rm.S8_EXECUTION["legacy_baseline"] = {**original_exec, "run_id": run_id}
            with contextlib.redirect_stdout(io.StringIO()):
                rc = ev.main(["--run", run, "--root", root])
        finally:
            rm.S8_EXECUTION["legacy_baseline"] = original_exec
            rm.CAPACITY = original_capacity
            rm.CAPACITY_OPTIONS = original_options
        self.assertEqual(rc, 0)
        results = os.path.join(root, "bench", "results")
        return {
            "root": root,
            "s8": _load(os.path.join(results, "p5s8_vm24_readonly.json")),
            "s6": _load(os.path.join(results, "p5s6_vm24_readonly.json")),
            "roadmap": _load(os.path.join(results, "p5_critical_path.json")),
            "memo": _read(os.path.join(root, "specs", "p5_critical_path_memo.md")),
        }

    def test_the_entry_point_writes_every_declared_output(self):
        out = self._regenerate(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        for rel in ev.REGENERATED_OUTPUTS:
            with self.subTest(rel):
                self.assertTrue(os.path.exists(os.path.join(out["root"], rel)))
        self.assertIn("bench/results/p5_critical_path.json", ev.REGENERATED_OUTPUTS)

    def test_all_four_outputs_carry_the_new_measurement(self):
        """The regression itself: the roadmap artifact must move with the other three."""
        out = self._regenerate(self.ALT_RUN_ID, self.ALT_FREE_BYTES)

        self.assertEqual(out["s8"]["capacity"]["free_now_gib"], self.ALT_FREE_GIB)
        self.assertEqual(out["s8"]["provenance"]["run_id"], self.ALT_RUN_ID)
        self.assertEqual(out["s6"]["capacity_gate"]["free_now_gb"], self.ALT_FREE_GIB)
        self.assertEqual(out["s6"]["capacity_gate"]["run_id"], self.ALT_RUN_ID)

        roadmap = out["roadmap"]
        self.assertEqual(roadmap["capacity"]["free_now_gib"], self.ALT_FREE_GIB,
                         "p5_critical_path.json still quotes the previous measurement")
        self.assertEqual(roadmap["capacity"]["run_id"], self.ALT_RUN_ID)
        self.assertIn(str(self.ALT_FREE_GIB), roadmap["g18_current"])
        self.assertIn(self.ALT_RUN_ID, roadmap["g18_current"])

        flat = " ".join(out["memo"].split())
        self.assertIn(str(self.ALT_FREE_GIB), flat)
        self.assertIn(self.ALT_RUN_ID, flat)

        for name, blob in (("s8", out["s8"]), ("roadmap", roadmap)):
            with self.subTest(stale=name):
                self.assertNotIn("295.7", json.dumps(blob),
                                 "the committed measurement survived a regeneration")

    def test_the_verdict_is_derived_and_agrees_across_every_output(self):
        """420 GiB is above the minimum, so the verdict must flip everywhere at once. A literal
        verdict written beside a derived figure shows up here and nowhere else."""
        out = self._regenerate(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        self.assertEqual(out["s8"]["capacity"]["verdict"], "NOT_BLOCKED")
        self.assertEqual(out["s8"]["gates"]["G18"], "NOT_BLOCKED")
        self.assertEqual(out["s6"]["capacity_gate"]["verdict"], "NOT_BLOCKED")
        self.assertIs(out["s6"]["capacity_gate"]["gate_ok"], True)
        self.assertEqual(out["roadmap"]["capacity"]["verdict"], "NOT_BLOCKED")
        self.assertTrue(out["roadmap"]["g18_current"].startswith("NOT_BLOCKED"))

        blockers = out["memo"][out["memo"].index(ev.BLOCKERS_START):
                               out["memo"].index(ev.BLOCKERS_END)]
        row = next(ln for ln in blockers.split("\n") if "G18 capacity" in ln)
        self.assertIn("NOT_BLOCKED", row)
        self.assertNotIn("**BLOCKED**", row, "the verdict is a literal, not the measurement")

    def test_a_different_blocked_reading_also_propagates(self):
        """Not only the flip: a different figure on the same side of the threshold moves too."""
        out = self._regenerate(self.BLOCKED_RUN_ID, self.BLOCKED_FREE_BYTES)
        shortfall = round(403.3 - self.BLOCKED_FREE_GIB, 1)
        self.assertEqual(out["s8"]["capacity"]["verdict"], "BLOCKED")
        self.assertEqual(out["s8"]["capacity"]["shortfall_gib"], shortfall)
        self.assertEqual(out["roadmap"]["capacity"]["free_now_gib"], self.BLOCKED_FREE_GIB)
        self.assertEqual(out["roadmap"]["capacity"]["shortfall_gib"], shortfall)
        self.assertIn(str(shortfall), out["roadmap"]["g18_current"])
        self.assertIn(str(self.BLOCKED_FREE_GIB), " ".join(out["memo"].split()))

    def test_the_threshold_and_reserve_never_move_with_the_reading(self):
        """Whatever free space says, HARD_RESERVE and the minimum are policy."""
        for run_id, free_bytes in ((self.ALT_RUN_ID, self.ALT_FREE_BYTES),
                                   (self.BLOCKED_RUN_ID, self.BLOCKED_FREE_BYTES)):
            out = self._regenerate(run_id, free_bytes)
            with self.subTest(run_id):
                for blob in (out["s8"]["capacity"], out["roadmap"]["capacity"]):
                    self.assertEqual(blob["hard_reserve_gib"], 250)
                    self.assertEqual(blob["minimum_free_gib"], 403.3)
                self.assertEqual(out["s6"]["capacity_gate"]["hard_reserve_gb"], 250)

    def test_the_roadmap_reads_the_file_just_written_not_its_import_time_copy(self):
        """`refresh()` is the reason the roadmap artifact is not one measurement behind."""
        root, run = self._checkout(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        results = os.path.join(root, "bench", "results")
        original_exec = dict(rm.S8_EXECUTION["legacy_baseline"])
        original_capacity, original_options = rm.CAPACITY, rm.CAPACITY_OPTIONS
        try:
            rm.S8_EXECUTION["legacy_baseline"] = {**original_exec, "run_id": self.ALT_RUN_ID}
            with contextlib.redirect_stdout(io.StringIO()):
                ev.main(["--run", run, "--root", root])
            # After the entry point, the module state reflects the run just regenerated.
            self.assertEqual(rm.CAPACITY["free_now_gib"], self.ALT_FREE_GIB)
            self.assertEqual(rm.CAPACITY_OPTIONS[0]["requirement_gib"], 403.3)
            # And refreshing back against the real checkout restores the committed reading,
            # so the refresh follows the evidence rather than latching.
            self.assertEqual(
                rm.refresh(os.path.join(_DEV, "bench", "results"))["free_now_gib"], 295.7)
        finally:
            rm.S8_EXECUTION["legacy_baseline"] = original_exec
            rm.CAPACITY, rm.CAPACITY_OPTIONS = original_capacity, original_options
        self.assertEqual(_load(os.path.join(results, "p5_critical_path.json"))
                         ["capacity"]["free_now_gib"], self.ALT_FREE_GIB)

    def _snapshot(self, root):
        """The exact bytes of every regenerated output."""
        out = {}
        for rel in ev.REGENERATED_OUTPUTS:
            path = os.path.join(root, rel)
            with open(path, "rb") as fh:
                out[rel] = fh.read()
        return out

    def _regeneration_must_not_touch_anything(self, root, run, break_writer):
        """Inject a roadmap-writer failure and demand every original survives it byte for byte.

        Asserting only that an exception is raised is not enough: the outputs were written one
        at a time, so a failure at the last step left three files carrying the new measurement
        and the fourth carrying the old one. The error reported that; it did not undo it."""
        before = self._snapshot(root)
        original_exec = dict(rm.S8_EXECUTION["legacy_baseline"])
        original_main = rm.main
        original_capacity, original_options = rm.CAPACITY, rm.CAPACITY_OPTIONS
        try:
            rm.S8_EXECUTION["legacy_baseline"] = {**original_exec, "run_id": self.ALT_RUN_ID}
            rm.main = break_writer
            with self.assertRaises(ArtifactError) as caught:
                with contextlib.redirect_stdout(io.StringIO()):
                    ev.main(["--run", run, "--root", root])
        finally:
            rm.main = original_main
            rm.S8_EXECUTION["legacy_baseline"] = original_exec
            rm.CAPACITY, rm.CAPACITY_OPTIONS = original_capacity, original_options

        after = self._snapshot(root)
        for rel in ev.REGENERATED_OUTPUTS:
            with self.subTest(rel):
                self.assertEqual(after[rel], before[rel],
                                 f"{rel} was modified by a regeneration that failed")
        self.assertIn("nothing was published", str(caught.exception))
        return str(caught.exception)

    def test_a_failing_roadmap_writer_leaves_all_four_originals_untouched(self):
        """The writer reports failure. Nothing may have moved."""
        root, run = self._checkout(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        reason = self._regeneration_must_not_touch_anything(
            root, run, lambda argv=None: 3)
        self.assertIn("3", reason)

    def test_a_raising_roadmap_writer_leaves_all_four_originals_untouched(self):
        """And when it raises rather than returning, the same must hold."""
        def explode(argv=None):
            raise RuntimeError("disk went away")

        root, run = self._checkout(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        before = self._snapshot(root)
        original_exec = dict(rm.S8_EXECUTION["legacy_baseline"])
        original_main = rm.main
        original_capacity, original_options = rm.CAPACITY, rm.CAPACITY_OPTIONS
        try:
            rm.S8_EXECUTION["legacy_baseline"] = {**original_exec, "run_id": self.ALT_RUN_ID}
            rm.main = explode
            with self.assertRaises(RuntimeError):
                with contextlib.redirect_stdout(io.StringIO()):
                    ev.main(["--run", run, "--root", root])
        finally:
            rm.main = original_main
            rm.S8_EXECUTION["legacy_baseline"] = original_exec
            rm.CAPACITY, rm.CAPACITY_OPTIONS = original_capacity, original_options
        self.assertEqual(self._snapshot(root), before)

    def test_a_writer_that_reports_success_without_writing_leaves_everything_untouched(self):
        """A writer can return 0 and produce nothing -- a wrong path, a skipped branch, a stub.
        The other three outputs would then be consistent with each other and with nothing."""
        root, run = self._checkout(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        reason = self._regeneration_must_not_touch_anything(
            root, run, lambda argv=None: 0)
        self.assertIn("p5_critical_path.json", reason)
        self.assertIn("did not write", reason)

    def test_a_failure_leaves_the_roadmap_module_reading_what_is_on_disk(self):
        """Staging hands the roadmap writer a scratch copy of the new evidence. If the run then
        fails, the module must not be left describing a measurement that was never published."""
        root, run = self._checkout(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        original_exec = dict(rm.S8_EXECUTION["legacy_baseline"])
        original_main = rm.main
        original_capacity, original_options = rm.CAPACITY, rm.CAPACITY_OPTIONS
        try:
            rm.S8_EXECUTION["legacy_baseline"] = {**original_exec, "run_id": self.ALT_RUN_ID}
            rm.main = lambda argv=None: 3
            with self.assertRaises(ArtifactError):
                with contextlib.redirect_stdout(io.StringIO()):
                    ev.main(["--run", run, "--root", root])
            self.assertEqual(rm.CAPACITY["free_now_gib"], 295.7,
                             "the module kept the unpublished staged measurement")
        finally:
            rm.main = original_main
            rm.S8_EXECUTION["legacy_baseline"] = original_exec
            rm.CAPACITY, rm.CAPACITY_OPTIONS = original_capacity, original_options

    def test_staging_produces_every_output_before_any_is_written(self):
        """`stage()` returns the full contents of all four and writes none of them."""
        root, run = self._checkout(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        before = self._snapshot(root)
        original_exec = dict(rm.S8_EXECUTION["legacy_baseline"])
        original_capacity, original_options = rm.CAPACITY, rm.CAPACITY_OPTIONS
        try:
            rm.S8_EXECUTION["legacy_baseline"] = {**original_exec, "run_id": self.ALT_RUN_ID}
            with contextlib.redirect_stdout(io.StringIO()):
                staged = ev.stage(run, root)
        finally:
            rm.S8_EXECUTION["legacy_baseline"] = original_exec
            rm.CAPACITY, rm.CAPACITY_OPTIONS = original_capacity, original_options
        self.assertEqual(sorted(staged), sorted(ev.REGENERATED_OUTPUTS))
        self.assertEqual(self._snapshot(root), before, "stage() wrote to the real tree")
        self.assertEqual(json.loads(staged[ev.S8_REL])["capacity"]["free_now_gib"],
                         self.ALT_FREE_GIB)

    def test_publish_restores_the_originals_when_a_write_fails(self):
        """The narrow window staging cannot close: a failure part-way through installing the
        set. Every file already replaced goes back to the bytes it had."""
        root, run = self._checkout(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        before = self._snapshot(root)
        staged = {rel: "REPLACED\n" for rel in ev.REGENERATED_OUTPUTS}
        original_write = ev._atomic_write
        calls = []

        def fail_on_the_third(path, text):
            calls.append(path)
            if len(calls) == 3:
                raise OSError("no space left on device")
            original_write(path, text)

        try:
            ev._atomic_write = fail_on_the_third
            with self.assertRaises(OSError):
                ev.publish(staged, root)
        finally:
            ev._atomic_write = original_write
        self.assertEqual(self._snapshot(root), before,
                         "a partly-installed set was left behind")

    def test_a_publish_failure_advances_neither_the_files_nor_the_module(self):
        """Injected through the real `ev.main()`, not by calling `publish()` directly.

        `publish()` sat outside the recovery path, so a rollback restored the four files while
        the roadmap module went on reporting the staged, unpublished measurement: disk saying
        one thing and the process another, which is worse than either being wrong alone.
        Calling `publish()` directly could never have shown it -- the module is advanced by
        `stage()`, one call earlier.

        Every assertion runs BEFORE anything is restored. Snapshotting module state and putting
        it back in `finally` is precisely how this hid: the test would have repaired the defect
        it was meant to detect."""
        root, run = self._checkout(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        results = os.path.join(root, "bench", "results")
        before_files = self._snapshot(root)
        before_capacity = json.loads(json.dumps(rm.CAPACITY))
        before_options = json.loads(json.dumps(rm.CAPACITY_OPTIONS))

        original_exec = dict(rm.S8_EXECUTION["legacy_baseline"])
        original_write = ev._atomic_write
        saved_capacity, saved_options = rm.CAPACITY, rm.CAPACITY_OPTIONS
        calls = []

        def fail_part_way_through(path, text):
            calls.append(path)
            if len(calls) == 3:            # two files already replaced
                raise OSError("no space left on device")
            original_write(path, text)

        try:
            rm.S8_EXECUTION["legacy_baseline"] = {**original_exec, "run_id": self.ALT_RUN_ID}
            ev._atomic_write = fail_part_way_through
            with self.assertRaises(OSError):
                with contextlib.redirect_stdout(io.StringIO()):
                    ev.main(["--run", run, "--root", root])
            self.assertGreaterEqual(len(calls), 3, "the injected failure never fired")

            # 1. the files
            after = self._snapshot(root)
            for rel in ev.REGENERATED_OUTPUTS:
                with self.subTest(rel):
                    self.assertEqual(after[rel], before_files[rel],
                                     f"{rel} survived a failed publish in its new form")

            # 2. the module, against both its pre-run state and what is on disk NOW
            self.assertEqual(rm.CAPACITY, before_capacity,
                             "the module kept the staged measurement after a rollback")
            self.assertEqual(rm.CAPACITY_OPTIONS, before_options)
            self.assertEqual(rm.CAPACITY, rm._measured_capacity(results),
                             "the module disagrees with the evidence on disk")

            # 3. nothing of the staged run survives in module state
            blob = json.dumps({"capacity": rm.CAPACITY, "options": rm.CAPACITY_OPTIONS})
            self.assertNotIn(self.ALT_RUN_ID, blob)
            self.assertNotIn(str(self.ALT_FREE_GIB), blob)
            self.assertEqual(rm.CAPACITY["free_now_gib"], 295.7)
            self.assertEqual(rm.CAPACITY["run_id"], RUN_ID)
        finally:
            ev._atomic_write = original_write
            rm.S8_EXECUTION["legacy_baseline"] = original_exec
            rm.CAPACITY, rm.CAPACITY_OPTIONS = saved_capacity, saved_options

    def test_a_staging_failure_also_leaves_the_module_matching_disk(self):
        """The other half of the same recovery path, asserted the same way.

        The failure is injected AFTER the real roadmap writer has run -- its output is removed
        from under it -- because that is the only staging failure that actually advances the
        module. Stubbing the writer out instead makes the test pass whether or not the resync
        exists: nothing ever moved the module, so nothing needs putting back. Mutation testing
        showed exactly that, and this is the version that fails when the resync is deleted."""
        root, run = self._checkout(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        results = os.path.join(root, "bench", "results")
        before_files = self._snapshot(root)
        original_exec = dict(rm.S8_EXECUTION["legacy_baseline"])
        original_main = rm.main
        saved_capacity, saved_options = rm.CAPACITY, rm.CAPACITY_OPTIONS

        def write_then_vanish(argv=None):
            rc = original_main(argv)               # a real run: CAPACITY moves to the staged read
            out = argv[argv.index("--out") + 1]
            os.remove(os.path.join(out, "p5_critical_path.json"))
            return rc

        try:
            rm.S8_EXECUTION["legacy_baseline"] = {**original_exec, "run_id": self.ALT_RUN_ID}
            rm.main = write_then_vanish
            with self.assertRaises(ArtifactError) as caught:
                with contextlib.redirect_stdout(io.StringIO()):
                    ev.main(["--run", run, "--root", root])
            self.assertIn("nothing was published", str(caught.exception))
            self.assertEqual(self._snapshot(root), before_files)
            self.assertEqual(rm.CAPACITY, rm._measured_capacity(results),
                             "the module kept the staged measurement after a staging failure")
            self.assertEqual(rm.CAPACITY["free_now_gib"], 295.7)
            self.assertNotIn(self.ALT_RUN_ID, json.dumps(rm.CAPACITY))
        finally:
            rm.main = original_main
            rm.S8_EXECUTION["legacy_baseline"] = original_exec
            rm.CAPACITY, rm.CAPACITY_OPTIONS = saved_capacity, saved_options

    def test_the_resync_targets_the_root_being_regenerated(self):
        """Not the module's default root.

        Every other failure test uses a temp checkout copied from this one, so both trees hold
        the same reading and resyncing to either produces the same answer -- swapping `root` for
        `DEV_ROOT` survived mutation testing on that alone. Here the temp root's own baseline
        evidence is set to a DIFFERENT measurement, so the two roots disagree and only the
        correct one satisfies "matches the evidence on disk"."""
        root, run = self._checkout(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        results = os.path.join(root, "bench", "results")
        baseline_path = os.path.join(results, os.path.basename(ev.S8_REL))
        baseline = _load(baseline_path)
        baseline["capacity"]["free_now_gib"] = 310.0
        baseline["capacity"]["free_now_bytes"] = int(310.0 * (1024 ** 3))
        baseline["capacity"]["shortfall_gib"] = round(403.3 - 310.0, 1)
        with open(baseline_path, "w") as fh:
            json.dump(baseline, fh, indent=2, sort_keys=True)

        before_files = self._snapshot(root)
        original_exec = dict(rm.S8_EXECUTION["legacy_baseline"])
        original_write = ev._atomic_write
        saved_capacity, saved_options = rm.CAPACITY, rm.CAPACITY_OPTIONS
        calls = []

        def fail_part_way_through(path, text):
            calls.append(path)
            if len(calls) == 3:
                raise OSError("no space left on device")
            original_write(path, text)

        try:
            rm.S8_EXECUTION["legacy_baseline"] = {**original_exec, "run_id": self.ALT_RUN_ID}
            ev._atomic_write = fail_part_way_through
            with self.assertRaises(OSError):
                with contextlib.redirect_stdout(io.StringIO()):
                    ev.main(["--run", run, "--root", root])
            self.assertEqual(self._snapshot(root), before_files)
            self.assertEqual(rm.CAPACITY["free_now_gib"], 310.0,
                             "the module was resynced from the wrong tree")
            self.assertEqual(rm.CAPACITY, rm._measured_capacity(results))
        finally:
            ev._atomic_write = original_write
            rm.S8_EXECUTION["legacy_baseline"] = original_exec
            rm.CAPACITY, rm.CAPACITY_OPTIONS = saved_capacity, saved_options

    def test_a_successful_run_does_leave_the_module_on_the_new_measurement(self):
        """The negative control. Resyncing on failure must not mean resyncing away a success."""
        root, run = self._checkout(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        original_exec = dict(rm.S8_EXECUTION["legacy_baseline"])
        saved_capacity, saved_options = rm.CAPACITY, rm.CAPACITY_OPTIONS
        try:
            rm.S8_EXECUTION["legacy_baseline"] = {**original_exec, "run_id": self.ALT_RUN_ID}
            with contextlib.redirect_stdout(io.StringIO()):
                ev.main(["--run", run, "--root", root])
            self.assertEqual(rm.CAPACITY["free_now_gib"], self.ALT_FREE_GIB)
            self.assertEqual(rm.CAPACITY["run_id"], self.ALT_RUN_ID)
            self.assertEqual(rm.CAPACITY,
                             rm._measured_capacity(os.path.join(root, "bench", "results")))
        finally:
            rm.S8_EXECUTION["legacy_baseline"] = original_exec
            rm.CAPACITY, rm.CAPACITY_OPTIONS = saved_capacity, saved_options

    def test_the_crash_boundary_stays_documented_and_unclaimed(self):
        """`publish()` restores originals on a failed write, which is easy to read as full
        multi-file atomicity. It is not: a crash between two `os.replace` calls leaves the set
        partial. Pinned so a later edit cannot quietly upgrade the claim."""
        doc = ev.publish.__doc__
        self.assertIn("PARTIAL", doc)
        self.assertIn("os.replace", doc)
        self.assertIn("journal", doc)
        self.assertNotIn("atomic across", doc)

    def test_the_rollback_is_documented_as_an_attempt_not_a_guarantee(self):
        """It said "restoring every original", which the rollback cannot promise -- it writes,
        so it fails the same ways the publish did. ENOSPC does not stand aside for it."""
        doc = ev.publish.__doc__
        self.assertIn("ATTEMPTING", doc)
        self.assertIn("not a guarantee", doc)
        self.assertIn("manual repair", doc)
        self.assertNotIn("restoring every original", doc)

    def _publish_with_writes_failing_from(self, root, first_failure):
        """Make `_atomic_write` fail from the Nth call onward -- rollback writes included."""
        original_write = ev._atomic_write
        calls = []

        def fail_from_then_on(path, text):
            calls.append(path)
            if len(calls) >= first_failure:
                raise OSError(28, "No space left on device")
            original_write(path, text)

        self.addCleanup(setattr, ev, "_atomic_write", original_write)
        ev._atomic_write = fail_from_then_on
        return calls

    def test_a_rollback_that_also_fails_reports_exactly_what_was_left_behind(self):
        """The rollback was assumed to succeed. It writes, so `ENOSPC` takes it down too, and
        then the set is MIXED -- some files new, some old -- which is the one outcome a person
        has to be told about. Fail closed: name the files, and keep the original as the cause."""
        root, _run = self._checkout(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        before = self._snapshot(root)
        staged = {rel: f"NEW CONTENT for {rel}\n" for rel in ev.REGENERATED_OUTPUTS}
        rels = list(staged)

        self._publish_with_writes_failing_from(root, first_failure=3)
        with self.assertRaises(ev.PublishRollbackError) as caught:
            ev.publish(staged, root)
        error = caught.exception

        # The first two landed; the rollback could restore neither.
        self.assertEqual(list(error.unrestored), sorted(rels[:2]))
        self.assertEqual(error.root, root)
        self.assertIsInstance(error.__cause__, OSError,
                              "the failure that started it was not preserved")
        self.assertEqual(error.__cause__.errno, 28)
        for word in ("MIXED", "manual repair"):
            with self.subTest(word):
                self.assertIn(word, str(error))

        # And the report is true of the disk: those files really do carry the new content.
        after = self._snapshot(root)
        for rel in rels[:2]:
            with self.subTest(unrestored=rel):
                self.assertEqual(after[rel].decode(), staged[rel])
        for rel in rels[2:]:
            with self.subTest(untouched=rel):
                self.assertEqual(after[rel], before[rel])

    def test_one_file_that_cannot_be_restored_does_not_abandon_the_others(self):
        """A report naming some of the damage is worse than one naming all of it."""
        root, _run = self._checkout(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        before = self._snapshot(root)
        staged = {rel: f"NEW CONTENT for {rel}\n" for rel in ev.REGENERATED_OUTPUTS}
        rels = list(staged)
        original_write = ev._atomic_write
        calls = []

        def fail_on_publish_three_and_the_first_restore(path, text):
            calls.append(path)
            failing = len(calls) == 3 or path.endswith(rels[0])
            if failing and len(calls) >= 3:
                raise OSError(28, "No space left on device")
            original_write(path, text)

        self.addCleanup(setattr, ev, "_atomic_write", original_write)
        ev._atomic_write = fail_on_publish_three_and_the_first_restore
        with self.assertRaises(ev.PublishRollbackError) as caught:
            ev.publish(staged, root)

        self.assertEqual(list(caught.exception.unrestored), [rels[0]])
        after = self._snapshot(root)
        self.assertEqual(after[rels[1]], before[rels[1]],
                         "the second file was abandoned after the first failed to restore")

    def test_a_failed_resync_is_reported_rather_than_swallowed(self):
        """Swallowing it left the caller believing module state had been recovered. The
        original failure stays as `__cause__`; this only adds what recovery could not do."""
        root, run = self._checkout(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        original_exec = dict(rm.S8_EXECUTION["legacy_baseline"])
        original_refresh = rm.refresh
        original_main = rm.main
        saved_capacity, saved_options = rm.CAPACITY, rm.CAPACITY_OPTIONS

        def refresh_is_broken(results_dir=None):
            raise OSError(5, "Input/output error")

        try:
            rm.S8_EXECUTION["legacy_baseline"] = {**original_exec, "run_id": self.ALT_RUN_ID}
            rm.main = lambda argv=None: 3
            rm.refresh = refresh_is_broken
            with self.assertRaises(ev.RoadmapResyncError) as caught:
                with contextlib.redirect_stdout(io.StringIO()):
                    ev.main(["--run", run, "--root", root])
        finally:
            rm.refresh = original_refresh
            rm.main = original_main
            rm.S8_EXECUTION["legacy_baseline"] = original_exec
            rm.CAPACITY, rm.CAPACITY_OPTIONS = saved_capacity, saved_options

        error = caught.exception
        self.assertIsInstance(error.__cause__, ArtifactError,
                              "the original regeneration failure was replaced, not preserved")
        self.assertIn("nothing was published", str(error.__cause__))
        self.assertIn("could not be", str(error))
        self.assertIn("do not trust it", str(error))

    def test_a_successful_resync_does_not_raise_the_recovery_error(self):
        """The control: recovery working must stay indistinguishable from the plain failure."""
        root, run = self._checkout(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        original_exec = dict(rm.S8_EXECUTION["legacy_baseline"])
        original_main = rm.main
        saved_capacity, saved_options = rm.CAPACITY, rm.CAPACITY_OPTIONS
        try:
            rm.S8_EXECUTION["legacy_baseline"] = {**original_exec, "run_id": self.ALT_RUN_ID}
            rm.main = lambda argv=None: 3
            with self.assertRaises(ArtifactError) as caught:
                with contextlib.redirect_stdout(io.StringIO()):
                    ev.main(["--run", run, "--root", root])
            self.assertNotIsInstance(caught.exception, ev.RoadmapResyncError)
        finally:
            rm.main = original_main
            rm.S8_EXECUTION["legacy_baseline"] = original_exec
            rm.CAPACITY, rm.CAPACITY_OPTIONS = saved_capacity, saved_options

    def test_refresh_rebuilds_everything_derived_from_the_reading(self):
        """Checked by identity, deliberately.

        `CAPACITY_OPTIONS` quotes the THRESHOLD, which is policy and does not move with free
        space. So a by-value assertion passes whether or not it was rebuilt -- refreshing
        `CAPACITY` alone survived mutation testing for exactly that reason. The invariant is
        that nothing derived from a reading survives into the next one; the day an option
        quotes the reading itself, a value check would notice one release too late."""
        before_capacity, before_options = rm.CAPACITY, rm.CAPACITY_OPTIONS
        try:
            rm.refresh(os.path.join(_DEV, "bench", "results"))
            self.assertIsNot(rm.CAPACITY, before_capacity)
            self.assertIsNot(rm.CAPACITY_OPTIONS, before_options,
                             "a structure derived from the reading was not rebuilt")
            self.assertEqual(rm.CAPACITY_OPTIONS, rm._capacity_options(rm.CAPACITY))
        finally:
            rm.CAPACITY, rm.CAPACITY_OPTIONS = before_capacity, before_options

    def test_the_committed_checkout_is_untouched_by_these_regenerations(self):
        """Every regeneration above targets a temp root; the real artifacts must not move."""
        before = _load(S8_ARTIFACT)
        self._regenerate(self.ALT_RUN_ID, self.ALT_FREE_BYTES)
        self.assertEqual(_load(S8_ARTIFACT), before)
        self.assertEqual(before["capacity"]["free_now_gib"], 295.7)
        self.assertEqual(before["capacity"]["verdict"], "BLOCKED")


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
    #: None. The threshold was allowed here as policy, but it is derived from the capacity
    #: model, so it ages like any other figure -- a static rationale carries no quantity.
    ALLOWED_NUMBERS = set()

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

    def section(self):
        """The generated block -- looked up HERE and not in `setUp`.

        In setUp, a memo with the sentinels removed killed the whole class in setUp, including
        the wording guard below, which then reported an error about a missing marker rather
        than the stale sentence it exists to catch. A guard that cannot run is not a guard."""
        self.assertIn(ev.APPROVALS_START, self.memo, "the generated section is gone")
        start = self.memo.index(ev.APPROVALS_START)
        return self.memo[start:self.memo.index(ev.APPROVALS_END) + len(ev.APPROVALS_END)]

    def test_it_is_generated_not_hand_written(self):
        self.assertIn("Generated by ops/p5s8_evidence.py", self.section())
        self.assertIn("Do not edit by hand", self.section())

    def test_it_matches_a_fresh_render(self):
        self.assertEqual(self.section(), ev.render_approvals(ev.derive(RUN)))

    def test_it_records_the_completed_S8_run_as_completed(self):
        flat = " ".join(self.section().split())
        self.assertIn("S8 `legacy_baseline` | **COMPLETED**", flat)
        self.assertIn(RUN_ID, flat)
        self.assertIn("nothing — it already ran", flat)

    def test_it_still_requires_separate_approval_for_what_has_not_run(self):
        flat = " ".join(self.section().split())
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


class TestTheProvisioningBlockerIsGenerated(unittest.TestCase):
    """The blockers table must carry provisioning, derived rather than restated.

    Everything else in this table was hand-maintained once and drifted; the rule since is that
    a row repeating a fact held elsewhere is generated from that fact. The provisioning row's
    source of truth is the assessor's `DEPLOYMENT_TOOLS` and its approved/proposed deploy sets,
    so emptying or satisfying the declaration changes the row without anyone editing prose."""

    def test_the_row_names_the_declared_tool_and_the_requirement_that_enforces_it(self):
        row = ev._provisioning_blocker()
        self.assertIn("ops/p5_provision.py", row)
        self.assertIn("deployment_tools", row)

    def test_the_row_splits_the_approved_set_by_who_carries_the_tool(self):
        """Being approved and being equipped are different, and the row must not blur them now
        that the approved set contains one commit of each kind."""
        from ops import p5_readiness as rd
        row = ev._provisioning_blocker()
        for sha in rd.REVIEWED_DEPLOY_SHAS:
            with self.subTest(sha[:7]):
                self.assertIn(sha[:7], row)
        self.assertIn("carrying `ops/p5_provision.py`: `708058e`", row)
        self.assertIn("not carrying it: `3223fba`", row)
        self.assertIn("BLOCKS", row)

    def test_the_row_names_the_recommended_commit_without_authorizing_a_deployment(self):
        row = ev._provisioning_blocker()
        self.assertIn("Recommended for a new deployment: `708058e`", row)
        self.assertIn("not authorized by this document", row)

    def test_the_row_follows_the_approved_set_rather_than_naming_commits(self):
        """Take the equipped commit out of the approved set and the row goes back to refusing
        outright. A literal would keep saying one is available."""
        from ops import p5_readiness as rd
        self.addCleanup(setattr, rd, "REVIEWED_DEPLOY_SHAS", rd.REVIEWED_DEPLOY_SHAS)
        rd.REVIEWED_DEPLOY_SHAS = ("3223fba37da5d292ea6acb58ccef0da2b5138d31",)
        row = ev._provisioning_blocker()
        self.assertIn("**BLOCKED**", row)
        self.assertIn("no approved commit", row)

    def test_the_row_follows_the_declaration_rather_than_repeating_it(self):
        """Change what is declared and the row changes. A literal would not."""
        from ops import p5_readiness as rd
        self.addCleanup(setattr, rd, "DEPLOYMENT_TOOLS", rd.DEPLOYMENT_TOOLS)
        rd.DEPLOYMENT_TOOLS = ("ops/some_other_tool.py",)
        row = ev._provisioning_blocker()
        self.assertIn("ops/some_other_tool.py", row)
        self.assertNotIn("ops/p5_provision.py", row)

    def test_an_empty_declaration_does_not_read_as_satisfied(self):
        from ops import p5_readiness as rd
        self.addCleanup(setattr, rd, "DEPLOYMENT_TOOLS", rd.DEPLOYMENT_TOOLS)
        rd.DEPLOYMENT_TOOLS = ()
        self.assertIn("nothing declared", ev._provisioning_blocker())

    def test_the_generated_memo_carries_the_row(self):
        """End to end: the row reaches the published artifact, not just the renderer."""
        with open(os.path.join(_DEV, "specs", "p5_critical_path_memo.md")) as fh:
            memo = fh.read()
        self.assertIn("Provisioning tool", memo)
        self.assertIn("deployment_tools", memo)
