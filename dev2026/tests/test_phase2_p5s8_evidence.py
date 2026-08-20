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


if __name__ == "__main__":
    unittest.main()
