"""dev2026 — P5-S7: the artifact and the results document must agree with the harness.

Three P5 documents have already drifted from their artifacts: twice by hand-copying a timing
table that the code then changed, and once by leaving a whole duplicated section whose status
contradicted the corrected one. Every guard here exists because of one of those.

The projection compares **claims**, not labels: a report whose verdicts all still say the right
word while the numbers underneath moved is exactly the failure these tests exist to catch.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
for p in (_DEV, os.path.join(_DEV, "bench"), HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

ARTIFACT = os.path.join(_DEV, "bench", "results", "p5s7_local_gate.json")
DOC = os.path.join(_DEV, "specs", "p5s7_local_gate_results.md")

#: Wall-clock and resource readings vary run to run. Everything else must reproduce exactly.
VOLATILE = ("p50_ms", "p95_ms", "wall_s", "rss_mb", "point_p95_ms_segmented",
            "point_p95_ms_monolith_baseline", "point_p95_ratio", "range_p50_ms",
            "range_p95_ms", "open_fds", "threads", "free_now_bytes",
            "projected_free_after_bytes", "block_bytes_estimate", "additional_bytes",
            "local_observation", "measured", "samples", "growth", "pool_sizing",
            "baseline", "by_concurrency", "sustained")


def _artifact():
    with open(ARTIFACT) as fh:
        return json.load(fh)


def _doc():
    with open(DOC) as fh:
        return fh.read()


class TestTheArtifactIsSelfConsistent(unittest.TestCase):

    def setUp(self):
        self.a = _artifact()

    def test_every_gate_G1_to_G12_is_present_with_a_verdict_and_a_reason(self):
        for n in range(1, 13):
            g = f"G{n}"
            with self.subTest(g):
                self.assertIn(g, self.a["gates"])
                entry = self.a["gates"][g]
                self.assertIn(entry["verdict"],
                              ("PASS", "FAIL", "PARTIAL", "NOT_ADJUDICATED", "DEFERRED"))
                if entry["verdict"] != "PASS":
                    self.assertTrue(entry.get("reason", "").strip(),
                                    "a non-PASS gate must say why")

    def test_a_PASS_gate_names_the_tests_that_adjudicated_it_or_the_measurement(self):
        for g, entry in self.a["gates"].items():
            if entry["verdict"] != "PASS":
                continue
            with self.subTest(g):
                self.assertTrue(entry.get("adjudicated_by") or entry.get("measured"),
                                f"{g} claims PASS with neither tests nor a measurement")
                if entry.get("adjudicated_by"):
                    self.assertGreater(entry["tests_run"], 0, "PASS with zero tests run")
                    self.assertEqual(entry["failures"] + entry["errors"], 0)

    def test_the_s7_gate_flag_agrees_with_the_gate_verdicts(self):
        """`met` must be computed, not asserted: a green summary over a non-green table is
        precisely the shape of an unearned sign-off."""
        not_green = {g: v["verdict"] for g, v in self.a["gates"].items()
                     if v["verdict"] != "PASS"}
        self.assertEqual(self.a["gates_not_green"], not_green)
        self.assertEqual(self.a["s7_gate"]["met"], not not_green)

    def test_the_disk_gate_used_a_REAL_reserve_and_can_fail(self):
        d = self.a["measured"]["disk_gate"]
        self.assertGreater(d["hard_reserve_bytes"], 0, "a zeroed reserve gates nothing")
        self.assertTrue(d["pass_case_ok"])
        self.assertFalse(d["fail_case_ok"])
        self.assertTrue(d["gate_can_fail"])

    def test_G3_G4_are_not_promoted_from_the_local_observation(self):
        for g in ("G3", "G4"):
            with self.subTest(g):
                self.assertEqual(self.a["gates"][g]["verdict"], "NOT_ADJUDICATED")
                self.assertIn("local_observation", self.a["gates"][g])

    def test_the_scope_records_that_nothing_production_was_touched(self):
        scope = self.a["scope"].lower()
        for phrase in ("no vm24", "no cron", "no prune"):
            self.assertIn(phrase, scope)
        self.assertEqual(self.a["label"], "LOCAL_SYNTHETIC")

    def test_S_and_C_are_still_provisional(self):
        p = self.a["provisional_configuration"]
        self.assertEqual((p["S"], p["C"]), (90, 30))
        self.assertIn("provisional", p["status"].lower())
        self.assertIn("not a final selection", p["status"].lower())

    def test_the_366_day_case_really_used_366_days(self):
        """A "366-day p95" measured over fewer days is a different measurement wearing the
        same name."""
        self.assertEqual(self.a["measured"]["latency"]["range_days"], 366)
        self.assertGreaterEqual(self.a["fixture"]["base_days"], 366)

    def test_the_concurrency_axis_is_distinguished_from_the_fold_cadence(self):
        note = self.a["measured"]["concurrency"]["axis_note"].lower()
        self.assertIn("not the fold cadence", note)
        self.assertEqual(self.a["measured"]["concurrency"]["levels"], [1, 4, 8, 16])

    def test_no_request_errored_during_the_load_measurement(self):
        self.assertEqual(self.a["measured"]["concurrency"]["errors"], [])


class TestTheCommittedArtifactMatchesTheGenerator(unittest.TestCase):
    """Regenerating must reproduce every non-volatile CLAIM, not merely the labels."""

    def _claims(self, node, out=None, trail=""):
        out = {} if out is None else out
        if isinstance(node, dict):
            for k, v in node.items():
                if k in VOLATILE:
                    continue
                self._claims(v, out, f"{trail}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                self._claims(v, out, f"{trail}[{i}]")
        else:
            out[trail] = node
        return out

    def test_the_committed_artifact_reproduces(self):
        import bench_p5s7_local_gate as h
        import tempfile
        import shutil
        tmp = tempfile.mkdtemp(prefix="p5s7-verify-")
        try:
            fresh = h.build_report(h._build_fixture(tmp), tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        committed = self._claims(_artifact())
        regenerated = self._claims(fresh)
        self.assertEqual(committed, regenerated,
                         "the committed artifact differs from what the harness now "
                         "produces — regenerate it")


class TestTheDocumentAgreesWithTheArtifact(unittest.TestCase):

    def setUp(self):
        self.a, self.doc = _artifact(), _doc()

    def test_every_gate_verdict_in_the_doc_matches_the_artifact(self):
        """The doc's table is prose; the artifact is the result. They must not diverge."""
        # Scoped to the verdict table: the "still not adjudicated" table further down names
        # some of the same gates, and counting rows across both would conflate a duplicated
        # verdict with a legitimate cross-reference.
        start = self.doc.index("| gate | verdict | basis |")
        table = self.doc[start:self.doc.index("\n##", start)]
        for n in range(1, 13):
            g = f"G{n}"
            verdict = self.a["gates"][g]["verdict"]
            row = [ln for ln in table.split("\n") if ln.startswith(f"| **{g}**")]
            with self.subTest(g):
                self.assertEqual(len(row), 1, f"expected exactly one {g} row, got {len(row)}")
                self.assertIn(verdict, row[0],
                              f"doc says something other than {verdict} for {g}")

    def test_the_doc_states_the_s7_gate_is_not_met_while_the_artifact_says_so(self):
        if not self.a["s7_gate"]["met"]:
            flat = " ".join(self.doc.split())
            self.assertIn("THE S7 GATE IS NOT MET", flat)
            self.assertIn("S8 is therefore not authored", flat)
        else:
            self.assertNotIn("S7 GATE IS NOT MET", self.doc)

    def test_the_doc_carries_no_hand_typed_timings(self):
        """Two P5 docs drifted from their artifacts by hand-copying exactly this."""
        body = re.sub(r"§\d+(\.\d+)*[a-z]?", "", self.doc)
        body = body.replace("< 4 s", "").replace("4 s", "")     # the spec's own threshold
        stray = re.findall(r"\b\d+(?:\.\d+)?\s*(?:ms|milliseconds)\b", body)
        self.assertEqual(stray, [], f"hand-typed timings in the doc: {stray}")

    def test_the_doc_does_not_claim_a_production_PASS_for_a_local_result(self):
        flat = " ".join(self.doc.split())
        for banned in ("G3 PASS", "G4 PASS", "G16 PASS", "H7 PASS",
                       "production performance evidence", "VM24 capacity evidence"):
            self.assertNotIn(banned, flat.replace("not VM24 capacity or", "not X or")
                             .replace("production-performance evidence", "X"))

    def test_G18_is_still_reported_BLOCKED(self):
        flat = " ".join(self.doc.split())
        self.assertIn("G18", flat)
        self.assertIn("BLOCKED", flat)

    def test_no_heading_appears_twice(self):
        """A duplicated block is how a contradicted status survived review in S6."""
        for level in ("## ", "### "):
            seen = [ln for ln in self.doc.split("\n")
                    if ln.startswith(level) and not ln.startswith(level + "#")]
            dupes = sorted({h for h in seen if seen.count(h) > 1})
            with self.subTest(level=level.strip()):
                self.assertEqual(dupes, [], f"duplicated heading(s): {dupes}")

    def test_the_doc_records_the_provisional_configuration_without_selecting_it(self):
        flat = " ".join(self.doc.split())
        self.assertIn("provisional configuration", flat)
        self.assertNotIn("final selection of S", flat)
        self.assertNotIn("SELECTED", self.doc)


if __name__ == "__main__":
    unittest.main()
