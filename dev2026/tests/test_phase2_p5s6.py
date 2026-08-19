"""dev2026 — P5-S6 sizing harness: the model, and what is actually measured.

## The labelling rule this suite enforces

Every number the harness emits carries one of four labels, and the tests check the label as
carefully as the value:

- **`MODEL`** — arithmetic at production geometry (17999 × 36000, 3 float32 vars). No I/O, no
  slab, nothing measured. A model may be *wrong* but it is never *evidence*.
- **`PRODUCTION_GEOMETRY_FIXTURE`** — measured on a synthetic store whose **chunk/shard geometry
  matches production** while its spatial extent does not. The shape of the cost is real; the
  magnitude is not production's.
- **`LOCAL`** — measured here, on this machine, at fixture scale.
- **`DEFERRED`** — cannot be established without VM24 read-only validation. Emitted with a
  reason, never with a number that looks like a result.

A metric that cannot be measured locally is `DEFERRED`. It is never an invented PASS, and
`p5s6_sizing_results.md` may not mark a production G3/H7 PASS from anything in here.

## Why the tests come first

Written before the harness, and committed failing, so that "the harness agrees with itself" is
never the thing being checked. Counters are asserted against **observed operations** — the
`CountingLocalStore` key set — rather than against the harness's own tallies, because a counter
that only ever compares to itself measures nothing.
"""
from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "bench"))

import bench_p5_sizing as sizing  # noqa: E402

_DEV2026 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LABELS = ("MODEL", "PRODUCTION_GEOMETRY_FIXTURE", "LOCAL", "DEFERRED")


class TestProductionGeometryConstants(unittest.TestCase):
    """The constants come from the spec, not from a fixture that happened to be handy."""

    def test_the_geometry_is_the_production_one(self):
        self.assertEqual(sizing.NY, 17999)
        self.assertEqual(sizing.NX, 36000)
        self.assertEqual(sizing.VARS, ("sst", "sst_anomaly", "sea_ice"))
        self.assertEqual(sizing.LEGACY_TIME_CHUNK, 90)
        self.assertEqual(sizing.DELTA_GEOMETRY, {"time_chunk": 1, "spatial_chunk": 256,
                                                 "shard_spatial": 256})

    def test_the_anchor_is_labelled_as_a_historical_fixture_anchor(self):
        """2026-06-27 is the committed fixture anchor. It is NOT current VM24 evidence, and the
        harness must say so wherever it appears."""
        self.assertEqual(sizing.FIXTURE_ANCHOR_DAY, "2026-06-27")
        note = sizing.FIXTURE_ANCHOR_NOTE.lower()
        self.assertIn("fixture", note)
        self.assertNotIn("current", note)
        for word in ("vm24 evidence", "production evidence"):
            self.assertNotIn(word, note)


class TestTheModelIsArithmeticNotMeasurement(unittest.TestCase):

    def test_delta_span_is_driven_by_cadence_not_block_size(self):
        """§10.1: delta_span_max = SPATIAL_WINDOW + C + DELTA_BUFFER, for any S."""
        for c, expected in ((30, 64), (45, 79), (90, 124)):
            with self.subTest(c=c):
                spans = {sizing.model_delta_span(s=s, c=c)["delta_span_max"]
                         for s in (30, 45, 90)}
                self.assertEqual(spans, {expected})

    def test_write_amplification_matches_the_spec_table(self):
        """§10.2: amplification = (S/C + 1) / 2."""
        for s, c, amp in ((30, 30, 1.0), (45, 45, 1.0), (90, 90, 1.0),
                          (90, 30, 2.0), (90, 45, 1.5)):
            with self.subTest(f"S={s} C={c}"):
                self.assertAlmostEqual(
                    sizing.model_write_amplification(s=s, c=c)["amplification"], amp, places=6)

    def test_segments_per_366_day_request_matches_the_spec_table(self):
        for s, segs in ((30, 13), (45, 9), (90, 5)):
            with self.subTest(s=s):
                self.assertEqual(sizing.model_segments_per_request(s=s, days=366)["segments"],
                                 segs)

    def test_the_segment_count_horizons_match_the_spec(self):
        """§10.5, at the horizons S6 is required to report: 5 / 13 / 41 / 82 / 122."""
        horizons = sizing.model_segment_horizons()
        self.assertEqual(horizons["S=90"]["+10y"], 41)
        self.assertEqual(horizons["S=45"]["+10y"], 82)
        self.assertEqual(horizons["S=30"]["+10y"], 122)
        self.assertEqual(sorted(sizing.SNAPSHOT_HORIZONS), [5, 13, 41, 82, 122])

    def test_the_file_count_model_matches_the_spec_arithmetic(self):
        """§10.6: 141 × 282 shard files per variable for a 90-day block."""
        out = sizing.model_file_counts(s=90)
        self.assertEqual(out["shards_per_var"], 141 * 282)
        self.assertEqual(out["files_per_block"], 3 * 141 * 282)
        self.assertAlmostEqual(out["files_per_day"], 3 * 141 * 282 / 90, places=1)

    def test_peak_temp_disk_matches_the_spec_table(self):
        for s, gb in ((30, 47), (45, 71), (90, 141)):
            with self.subTest(s=s):
                self.assertAlmostEqual(sizing.model_peak_temp_disk_gb(s=s), gb, delta=1.0)

    def test_every_model_result_is_labelled_MODEL(self):
        for fn, kw in ((sizing.model_delta_span, {"s": 90, "c": 30}),
                       (sizing.model_write_amplification, {"s": 90, "c": 30}),
                       (sizing.model_segments_per_request, {"s": 90, "days": 366}),
                       (sizing.model_file_counts, {"s": 90})):
            with self.subTest(fn.__name__):
                self.assertEqual(fn(**kw)["label"], "MODEL")

    def test_no_model_function_touches_the_filesystem(self):
        """A "model" that reads the disk is a measurement wearing the wrong label."""
        import io
        real_open = open
        opened = []

        def spy(*a, **k):
            opened.append(a[0] if a else None)
            return real_open(*a, **k)

        import builtins
        builtins.open = spy
        try:
            sizing.model_all()
        finally:
            builtins.open = real_open
        self.assertEqual(opened, [], f"a MODEL function opened {opened[:3]}")
        del io


class TestNoFullSlabIsEverMaterialized(unittest.TestCase):
    """The one hard prohibition: 17999 × 36000 × 4 bytes is 2.6 GB per day per variable."""

    def test_the_harness_declares_and_enforces_a_slab_ceiling(self):
        self.assertLess(sizing.MAX_FIXTURE_CELLS, 17999 * 36000 // 100)

    def test_a_fixture_request_above_the_ceiling_is_refused(self):
        with self.assertRaises(sizing.FixtureTooLarge):
            sizing.assert_fixture_bounds(ny=17999, nx=36000, days=1)

    def test_the_production_geometry_fixture_stays_under_it(self):
        geom = sizing.production_geometry_fixture_shape()
        sizing.assert_fixture_bounds(**geom)
        self.assertLessEqual(geom["ny"] * geom["nx"] * geom["days"], sizing.MAX_FIXTURE_CELLS)


class TestCountersAreCheckedAgainstObservedOperations(unittest.TestCase):
    """A counter compared only to itself measures nothing."""

    def test_the_segment_call_count_matches_the_OBSERVED_chunk_keys(self):
        out = sizing.measure_segment_calls(s=30, days=90)
        self.assertEqual(out["label"], "PRODUCTION_GEOMETRY_FIXTURE")
        self.assertEqual(out["reported_segment_calls"], out["observed_segment_calls"],
                         "the harness's tally must equal what the store actually saw")
        self.assertGreater(out["observed_chunk_keys"], 0)

    def test_the_observation_is_a_LIST_OF_PATHS_not_a_number_the_harness_chose(self):
        """`reported == observed` passes if the observation is quietly replaced by the tally.
        The paths are what the store actually opened, and the harness cannot invent them."""
        out = sizing.measure_segment_calls(s=30, days=90)
        paths = out["observed_open_paths"]
        self.assertEqual(len(paths), out["observed_segment_calls"])
        self.assertEqual(len(set(paths)), len(paths), "each segment counted once")
        for name in paths:
            self.assertTrue(name.startswith("block_") and name.endswith(".zarr"), name)
        self.assertEqual(sorted(paths),
                         sorted(f"block_{i:03d}.zarr" for i in range(3)))

    def test_a_read_inside_ONE_block_observes_one_open_not_the_block_count(self):
        """The case that makes the observation falsifiable. With a full read, observed always
        equals reported no matter where `observed` came from -- so a mutation replacing the
        observation with the tally survived. Here they MUST differ."""
        out = sizing.measure_segment_calls(s=30, days=90, open_only_first=True)
        self.assertEqual(out["reported_segment_calls"], 3)
        self.assertEqual(out["observed_segment_calls"], 1,
                         "only one block was read; the tally says three")
        self.assertEqual(out["observed_open_paths"], ["block_000.zarr"])
        self.assertLess(out["observed_segment_calls"], out["reported_segment_calls"])

    def test_more_segments_means_more_observed_calls(self):
        """The direction the whole sizing question turns on, measured rather than modelled."""
        small_s = sizing.measure_segment_calls(s=30, days=90)
        large_s = sizing.measure_segment_calls(s=90, days=90)
        self.assertGreater(small_s["observed_segment_calls"],
                           large_s["observed_segment_calls"])

    def test_snapshot_open_cost_reports_observed_syscalls_not_a_guess(self):
        out = sizing.measure_snapshot_open(segments=5)
        self.assertEqual(out["label"], "LOCAL")
        self.assertGreater(out["observed_metadata_reads"], 0)
        self.assertGreater(out["wall_ms"], 0.0)


class TestRewriteEvidenceIsContentNotMtime(unittest.TestCase):
    """§P5-S0's rule: mtime and size do not prove a rewrite."""

    def test_the_rebuild_probe_uses_content_digests(self):
        out = sizing._tail_rebuild_probe(s=30, c=30)
        self.assertEqual(out["rewrite_evidence"], "sha256-content-digest")
        self.assertIn("carried_forward_days", out)
        self.assertIn("rewritten_files", out)

    def test_a_file_touched_but_unchanged_is_NOT_counted_as_rewritten(self):
        out = sizing._tail_rebuild_probe(s=30, c=30, touch_without_changing=True)
        self.assertEqual(out["rewritten_files_touched_only"], 0,
                         "mtime moved and content did not; that is not a rewrite")


class TestDeferredIsNeverAPass(unittest.TestCase):

    def test_a_DEFERRED_tail_rebuild_publishes_no_numbers(self):
        """A `rebuild_wall_ms` in a DEFERRED row reads as performance evidence."""
        out = sizing.measure_tail_rebuild(s=30, c=30)
        self.assertEqual(out["label"], "DEFERRED")
        for banned in ("rebuild_wall_ms", "rewritten_files", "carried_forward_days",
                       "days_in_new_tail", "rewritten_files_touched_only",
                       "modelled_amplification"):
            self.assertNotIn(banned, out)
        banned = ("rebuild_wall_ms", "rewritten_files", "rewritten_files_touched_only",
                  "carried_forward_days", "days_in_new_tail", "modelled_amplification")
        for stamp in ("quick", "full"):
            path = os.path.join(_DEV2026, "bench", "results", f"p5s6_sizing_{stamp}.json")
            with open(path) as fh:
                rows = json.load(fh)["measured"]["tail_rebuild"]
            self.assertTrue(rows)
            for row in rows:
                for key in banned:              # the whole set, on every row, in both artifacts
                    with self.subTest(stamp=stamp, key=key):
                        self.assertNotIn(key, row)

    def test_production_scale_metrics_are_marked_DEFERRED_with_a_reason(self):
        deferred = sizing.deferred_metrics()
        self.assertTrue(deferred)
        for name, entry in deferred.items():
            with self.subTest(name):
                self.assertEqual(entry["label"], "DEFERRED")
                self.assertTrue(entry["reason"].strip())
                self.assertNotIn("value", entry, "a DEFERRED metric must carry no number")

    def test_inode_headroom_is_DEFERRED_because_it_needs_the_production_filesystem(self):
        self.assertIn("inode_headroom", sizing.deferred_metrics())

    def test_the_harness_never_emits_a_production_gate_PASS(self):
        report = sizing.build_report(quick=True)
        blob = json.dumps(report).lower()
        for claim in ('"g3": "pass"', '"h7": "pass"', "production_pass"):
            self.assertNotIn(claim, blob)
        self.assertEqual(report["gates"]["G3"]["verdict"], "NOT_ADJUDICATED")
        self.assertEqual(report["gates"]["H7"]["verdict"], "NOT_ADJUDICATED")


class TestTheReportIsWellFormed(unittest.TestCase):

    def test_every_entry_carries_one_of_the_four_labels(self):
        report = sizing.build_report(quick=True)
        found = []

        def walk(node):
            if isinstance(node, dict):
                if "label" in node:
                    found.append(node["label"])
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(report)
        self.assertTrue(found)
        for label in found:
            self.assertIn(label, LABELS)

    def test_the_report_records_the_anchor_as_a_fixture_anchor(self):
        report = sizing.build_report(quick=True)
        self.assertEqual(report["fixture_anchor_day"], "2026-06-27")
        self.assertIn("fixture", report["fixture_anchor_note"].lower())

    def test_the_S_and_C_grids_are_the_required_ones(self):
        report = sizing.build_report(quick=True)
        self.assertEqual(sorted(report["grid"]["S"]), [30, 45, 90])
        self.assertEqual(sorted(report["grid"]["C"]), [30, 45, 90])

    def test_H5_covers_the_required_delta_spans(self):
        report = sizing.build_report(quick=True)
        self.assertEqual(sorted(report["H5"]["delta_spans"]), [31, 45, 64, 90, 124])

    def test_H3_covers_the_required_windows(self):
        report = sizing.build_report(quick=True)
        for window in ("1_day", "366_day", "legacy_block_delta_crossing"):
            self.assertIn(window, report["H3"])


class TestCommittedArtifactsMatchTheGenerator(unittest.TestCase):
    """Round-3 finding. The code and the results doc were corrected; the committed JSON still
    said `H5.label = MODEL` and `tail_rebuild.label = LOCAL`, so three artefacts of the same
    round disagreed. That is the artifact drift P5 has hit repeatedly, and it is checked here
    rather than remembered."""

    RESULTS = os.path.join(_DEV2026, "bench", "results")

    def _artifact(self, stamp):
        path = os.path.join(self.RESULTS, f"p5s6_sizing_{stamp}.json")
        self.assertTrue(os.path.isfile(path), f"missing artifact {path}")
        with open(path) as fh:
            return json.load(fh)

    def _labels(self, node, out=None):
        """Every (path, label) pair, so a drifted label is located and not just counted."""
        out = {} if out is None else out

        def walk(n, trail):
            if isinstance(n, dict):
                if "label" in n:
                    out[trail] = n["label"]
                for k, v in n.items():
                    walk(v, f"{trail}.{k}")
            elif isinstance(n, list):
                for i, v in enumerate(n):
                    walk(v, f"{trail}[{i}]")

        walk(node, "")
        return out

    #: Keys whose values are wall-clock or otherwise run-dependent. Everything else is a CLAIM
    #: and must match the generator exactly.
    VOLATILE = ("wall_ms", "ms_per_segment", "ms_per_1k_files", "files", "observed_chunk_keys",
                "observed_metadata_reads", "observed_open_paths", "observed_segment_calls",
                "reported_segment_calls")

    def _claims(self, node, out=None, trail=""):
        """Every non-volatile leaf, keyed by path: the report's claims minus its timings."""
        out = {} if out is None else out
        if isinstance(node, dict):
            for k, v in node.items():
                if k in self.VOLATILE:
                    continue
                self._claims(v, out, f"{trail}.{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                self._claims(v, out, f"{trail}[{i}]")
        else:
            out[trail] = node
        return out

    def test_the_committed_artifacts_match_the_generators_CLAIMS_not_just_labels(self):
        """Labels alone left `reason`, `measures` and `not_measured` free to drift: the
        generator could be reworded and a stale artifact would still pass. Everything except
        run-dependent timings is compared."""
        for stamp, quick in (("quick", True), ("full", False)):
            with self.subTest(stamp):
                fresh = self._claims(sizing.build_report(quick=quick))
                stale = self._claims(self._artifact(stamp))
                self.assertEqual(stale, fresh,
                                 f"the committed {stamp} artifact's claims differ from what the "
                                 f"harness now produces — regenerate it")

    def test_the_committed_artifacts_have_the_generators_labels(self):
        """BOTH artifacts. Checking only `quick` left every label unique to the full grid --
        S=45, the 41/82/122 horizons -- free to drift unnoticed."""
        for stamp, quick in (("quick", True), ("full", False)):
            with self.subTest(stamp):
                fresh = sizing.build_report(quick=quick)
                self.assertEqual(self._labels(self._artifact(stamp)), self._labels(fresh),
                                 f"the committed {stamp} artifact's labels differ from what the "
                                 f"harness now produces — regenerate it")

    def test_both_artifacts_carry_the_WITHDRAWALS(self):
        for stamp in ("quick", "full"):
            with self.subTest(stamp):
                a = self._artifact(stamp)
                self.assertEqual(a["H5"]["label"], "DEFERRED")
                self.assertIn("reason", a["H5"])
                for row in a["measured"]["tail_rebuild"]:
                    self.assertEqual(row["label"], "DEFERRED")
                    self.assertIn("materialization strategy", row["reason"])
                for row in a["measured"]["segment_calls"]:
                    self.assertIn("NOT the grouped read path", row["measures"])
                for row in a["measured"]["snapshot_open"]:
                    self.assertIn("NOT O2 snapshot-open cost", row["measures"])
                    self.assertIn("SegmentedCubeStore", row["not_measured"])

    def test_the_results_doc_agrees_with_the_artifacts(self):
        """The third artefact. Round 2 corrected the code and the doc and left the JSON stale;
        nothing stops the reverse next time unless the doc is checked as well."""
        doc_path = os.path.join(_DEV2026, "specs", "p5s6_sizing_results.md")
        with open(doc_path) as fh:
            doc = fh.read()
        art = self._artifact("full")
        self.assertIn("H5", doc)
        for gate in ("G3", "H7", "G16"):
            self.assertEqual(art["gates"][gate]["verdict"], "NOT_ADJUDICATED")
            self.assertIn("NOT_ADJUDICATED", doc)
        # the withdrawals must be visible in the prose, not only in the JSON
        for phrase in ("WITHDRAWN AS EVIDENCE", "raw group-open", "DEFERRED"):
            self.assertIn(phrase, doc)
        # the doc must carry the artifact's OWN withdrawal wording, not a paraphrase that can
        # drift away from it
        flat = " ".join(doc.split())
        self.assertIn("materialization strategy", flat)
        self.assertIn("materialization strategy",
                      " ".join(art["measured"]["tail_rebuild"][0]["reason"].split()))
        self.assertIn("NOT O2 snapshot-open cost", art["measured"]["snapshot_open"][0]["measures"])
        self.assertIn("not O2 snapshot-open", flat)
        # and the pre-withdrawal headings must be gone
        for stale in ("## LOCAL — tail rebuild and carry-forward",
                      "## LOCAL — snapshot-open and metadata scan",
                      "## PRODUCTION_GEOMETRY_FIXTURE — segment calls"):
            self.assertNotIn(stale, doc, f"stale heading survives: {stale}")
        # the doc must not choose a configuration, and must say so
        self.assertIn("No configuration is chosen", doc)
        self.assertNotIn("SELECTED", doc)
        # a DEFERRED section may not carry a wall-clock number
        tail = doc[doc.index("## Tail rebuild"):doc.index("## DEFERRED — five metrics")]
        self.assertNotIn("rebuild (ms)", tail)
        # normalized: the doc hard-wraps, so a contiguous match is a test about line breaks
        self.assertIn("removed rather than relabelled", " ".join(tail.split()))

    def test_no_artifact_claims_a_production_gate_PASS(self):
        for stamp in ("quick", "full"):
            with self.subTest(stamp):
                a = self._artifact(stamp)
                for gate in ("G3", "H7", "G16"):
                    self.assertEqual(a["gates"][gate]["verdict"], "NOT_ADJUDICATED")


if __name__ == "__main__":
    unittest.main()
