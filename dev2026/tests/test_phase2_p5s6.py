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
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "bench"))

import bench_p5_sizing as sizing  # noqa: E402

_DEV2026 = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LABELS = ("MODEL", "PRODUCTION_GEOMETRY_FIXTURE", "LOCAL", "LOCAL_SYNTHETIC",
          "DEFERRED")


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
        """The full report measures all five; `quick` measures the first two by design, and the
        constant is what the full run walks."""
        self.assertEqual(sorted(sizing.H5_DELTA_SPANS), [31, 45, 64, 90, 124])
        report = sizing.build_report(quick=True)
        self.assertTrue(set(report["H5"]["spans_measured"]) <= set(sizing.H5_DELTA_SPANS))

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

    #: ONLY wall-clock. Everything else -- including every `observed_*` count and the walked
    #: `files` total -- is deterministic given the fixture, so it is a claim and must match.
    #:
    #: The first version excluded all of them as "run-dependent", which meant an artifact whose
    #: observed_open_paths or observed_segment_calls had been edited by hand still passed the
    #: test that was introduced to stop exactly that. Excluding an observation from the
    #: comparison is indistinguishable from not observing it.
    VOLATILE = ("wall_ms", "ms_per_segment", "ms_per_1k_files", "read_ms",
                "p50_ms", "p95_ms")

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
                # H5 was DEFERRED because it was not measured. It is measured now, through a
                # real TieredCube, so the withdrawal no longer applies -- and the assertion is
                # replaced rather than deleted, so the artifact still has to say which.
                self.assertEqual(a["H5"]["label"], "LOCAL_SYNTHETIC")
                self.assertTrue(a["H5"]["measured"])
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
        # No hand-copied construction timings: they drifted from the artifact within one round.
        construction = doc[doc.index("## Snapshot construction"):doc.index("## RAW GROUP-OPEN")]
        self.assertNotIn("construction p50 (ms)", construction)
        self.assertIn("NOT reproduced here", construction)
        # The two sections that report measured timings TODAY. This is an explicit list, not a
        # claim of exhaustiveness: a third timing section added later would not be covered, and
        # saying "every measured-timing section" would then be false. If one is added, add it
        # here -- the registry is the contract.
        #
        # NOT "does it match the current artifact": a STALE copied number would pass that, and
        # stale is exactly what happened. No figure -- decimal OR integer with a unit -- may
        # appear in these sections; the integer form was a way past the first version.
        timing_sections = {
            "snapshot construction": ("## Snapshot construction", "## RAW GROUP-OPEN"),
            "raw group-open": ("## RAW GROUP-OPEN", "## Grouped read"),
        }
        for name, (begin, finish) in timing_sections.items():
            with self.subTest(name):
                body = doc[doc.index(begin):doc.index(finish)]
                # §10.5 / §10.6 are spec references, not measurements.
                body = re.sub(r"§\d+(\.\d+)*[a-z]?", "", body)
                stray = (re.findall(r"\d+\.\d+", body)
                         + re.findall(r"\b\d+\s*(?:ms|s)\b", body))
                self.assertEqual(stray, [],
                                 f"hand-typed timings in the {name} section: {stray}")
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


class TestGroupedReadUsesTheRealPath(unittest.TestCase):
    """Item 1. The measurement must go through `SegmentedCubeStore.point_series()` against a
    real published manifest -- not a loop over `zarr.open_group()` standing in for it."""

    def test_it_builds_a_real_manifest_and_a_real_store(self):
        out = sizing.measure_grouped_read(segments=4, total_days=40, request_days=10)
        self.assertEqual(out["label"], "LOCAL_SYNTHETIC")
        self.assertTrue(out["used_manifest"])
        self.assertEqual(out["read_path"], "SegmentedCubeStore.point_series")
        self.assertGreater(out["returned_days"], 0)
        # §6.2: a warm read opens NOTHING. Asserting the opposite was my error, not the
        # code's -- the read serves from metadata captured at snapshot time.
        self.assertEqual(out["observed_open_calls"], 0,
                         "P5-S2 §6.2: the read path must not open a group")
        self.assertTrue(out["opens_during_read_must_be_zero"])

    def test_the_observation_comes_from_the_IMPLEMENTATIONS_module_boundary(self):
        """`segmented_cube` delegates to `TimeCubeStore`, so that is the boundary the read
        actually resolves `open_group` through. Patching a name the implementation never uses
        would observe nothing and pass."""
        out = sizing.measure_grouped_read(segments=4, total_days=40, request_days=10)
        self.assertEqual(out["spy_target"], "store.time_cube.zarr.open_group")

    def test_the_narrow_request_is_EXPECTED_to_need_fewer_segments_than_the_manifest_has(self):
        """From the manifest and the request, independent of the read. The matching OBSERVED
        count needs a chunk-level observable and is DEFERRED -- see below."""
        out = sizing.measure_grouped_read(segments=4, total_days=40, request_days=5,
                                          request_within_one_segment=True)
        self.assertEqual(out["manifest_segments"], 4)
        self.assertLess(out["expected_segments_for_request"], out["manifest_segments"])
        self.assertGreaterEqual(out["expected_segments_for_request"], 1)

    def test_a_wider_request_needs_more_segments_than_a_narrow_one(self):
        narrow = sizing.measure_grouped_read(segments=4, total_days=40, request_days=5,
                                             request_within_one_segment=True)
        wide = sizing.measure_grouped_read(segments=4, total_days=40, request_days=40)
        self.assertGreater(wide["expected_segments_for_request"],
                           narrow["expected_segments_for_request"])

    def test_counting_segments_a_READ_touches_is_marked_DEFERRED(self):
        """Honest about what is not measured: `open_group` cannot answer it, because §6.2 says
        it is never called during a read."""
        out = sizing.measure_grouped_read(segments=4, total_days=40, request_days=10)
        self.assertEqual(out["segments_touched_observable"]["label"], "DEFERRED")
        self.assertIn("chunk-level observable",
                      out["segments_touched_observable"]["reason"])

    def test_the_canary_states_its_LIMITED_scope(self):
        """One day, one variable, one cell. Calling it "parity" invited the reading that every
        returned row and variable had been checked; none of that is true."""
        out = sizing.measure_grouped_read(segments=4, total_days=40, request_days=10)
        scope = out["raw_cell_canary_scope"]
        self.assertIn("ONE probe day", scope)
        self.assertIn("NOT parity", scope)

    def test_values_match_a_COMMON_oracle_not_a_second_independent_store(self):
        """Parity against the bytes the fixture wrote, so two stores disagreeing cannot be
        reported as agreement."""
        out = sizing.measure_grouped_read(segments=4, total_days=40, request_days=10)
        self.assertTrue(out["raw_cell_canary_ok"], out["raw_cell_canary_mismatches"])
        self.assertIn("sentinel written directly", out["raw_cell_canary_oracle"])
        self.assertEqual(out["raw_cell_canary_mismatches"], [])
        self.assertIn("overwritten after the measured read", out["raw_cell_canary_note"])

    def test_the_parity_check_CAN_fail(self):
        """`parity_ok: True` proves nothing unless False is reachable. Disabling the comparison
        outright also yields True, and a mutation doing exactly that survived until this test
        existed."""
        bad = sizing.measure_grouped_read(segments=4, total_days=40, request_days=10,
                                          _negative_control=True)
        self.assertFalse(bad["raw_cell_canary_ok"])
        self.assertTrue(bad["raw_cell_canary_mismatches"])
        entry = bad["raw_cell_canary_mismatches"][0]
        self.assertIn("expected_raw_cell", entry)
        self.assertNotAlmostEqual(entry["got"], entry["expected_raw_cell"], places=3)


class TestTheRealMeasurementsReachTheArtifacts(unittest.TestCase):
    """They were in the results doc and not in the JSON, so nothing could detect the omission."""

    def test_both_artifacts_carry_the_grouped_read_and_the_constructor(self):
        for stamp in ("quick", "full"):
            with self.subTest(stamp):
                path = os.path.join(_DEV2026, "bench", "results", f"p5s6_sizing_{stamp}.json")
                with open(path) as fh:
                    art = json.load(fh)
                grouped = art["measured"]["grouped_read"]
                self.assertTrue(grouped)
                for row in grouped:
                    self.assertEqual(row["read_path"], "SegmentedCubeStore.point_series")
                    self.assertEqual(row["spy_target"], "store.time_cube.zarr.open_group")
                    self.assertTrue(row["raw_cell_canary_ok"])
                # the artifact must hold at least one genuinely CROSS-segment request
                spans = [r["expected_segments_for_request"] for r in grouped]
                self.assertGreaterEqual(max(spans), 2,
                                        "no persisted grouped-read row crosses a segment "
                                        "boundary, so the artifact holds no cross-segment "
                                        "evidence")
                self.assertGreaterEqual(max(spans), 4,
                                        "at least one row should span every segment")
                construction = art["measured"]["snapshot_construction"]
                self.assertTrue(construction)
                for row in construction:
                    self.assertEqual(row["measures"],
                                     "SegmentedCubeStore(manifest_dir) construction")
                    self.assertGreater(row["p50_ms"], 0.0)


class TestSnapshotOpenIsTheRealConstruction(unittest.TestCase):
    """Item 2. `SegmentedCubeStore(manifest_dir)` -- validation, inspection, binding, snapshot
    build -- not raw group-open."""

    def test_it_measures_the_store_constructor(self):
        out = sizing.measure_snapshot_construction(segments=5)
        self.assertEqual(out["label"], "LOCAL_SYNTHETIC")
        self.assertEqual(out["measures"], "SegmentedCubeStore(manifest_dir) construction")
        for phase in ("manifest_validation", "segment_inspection", "snapshot_build"):
            self.assertIn(phase, out["includes"])

    def test_segments_carry_a_REPRESENTATIVE_day_count(self):
        """One day per segment made the horizons into one-day blocks, understating the
        day-metadata and inspection cost this measurement exists to capture."""
        out = sizing.measure_snapshot_construction(segments=5, days_per_segment=30)
        geom = out["fixture_geometry"]
        self.assertEqual(geom["days_per_segment"], 30)
        self.assertEqual(geom["total_days"], 5 * 30)
        self.assertGreater(geom["total_days"], geom["segments"])

    def test_it_reports_p50_and_p95_and_the_fixture_geometry(self):
        out = sizing.measure_snapshot_construction(segments=5)
        self.assertGreater(out["p50_ms"], 0.0)
        self.assertGreaterEqual(out["p95_ms"], out["p50_ms"])
        self.assertIn("ny", out["fixture_geometry"])
        self.assertIn("warm", out["cache_assumption"])

    def test_construction_costs_more_than_a_bare_group_open(self):
        """The distinction the earlier round got wrong, asserted rather than described."""
        real = sizing.measure_snapshot_construction(segments=5)
        raw = sizing.measure_snapshot_open(segments=5)
        self.assertGreater(real["p50_ms"], raw["wall_ms"] / 10.0,
                           "construction should not be cheaper than a fraction of raw opens")
        self.assertIn("NOT O2 snapshot-open cost", raw["measures"])


class TestH5CrossingLatency(unittest.TestCase):
    """H5: crossing-range latency versus delta span, through a REAL `TieredCube`.

    Base and delta are built from ONE authoritative oracle, so a value disagreement is a real
    disagreement rather than two fixtures that were never the same data. Every number is
    LOCAL_SYNTHETIC and adjudicates nothing."""

    def test_the_spans_are_the_required_ones(self):
        out = sizing.measure_h5_latency(quick=True)
        self.assertEqual(out["label"], "LOCAL_SYNTHETIC")
        self.assertEqual(sorted(out["spans_measured"]), sorted(sizing.H5_DELTA_SPANS)[:2])

    def test_it_uses_a_real_TieredCube_not_a_loop(self):
        row = sizing.measure_h5_span(delta_span=31)
        self.assertEqual(row["read_path"], "TieredCube.point_series")
        self.assertTrue(row["used_base_and_delta"])

    def test_each_span_covers_base_only_crossing_and_delta_only(self):
        row = sizing.measure_h5_span(delta_span=31)
        for kind in ("base_only", "crossing", "delta_only"):
            with self.subTest(kind):
                case = row["requests"][kind]
                self.assertGreater(case["returned_days"], 0)
                self.assertEqual(case["returned_days"], case["requested_days"])
                self.assertGreater(case["p50_ms"], 0.0)
                self.assertGreaterEqual(case["p95_ms"], case["p50_ms"])
                # the TIMED rows, not a separate check, must show delta winning
                self.assertTrue(case["timed_rows_served_delta_values"],
                                "the timed read did not serve delta's values; it may not have "
                                "gone through the TieredCube at all")
        self.assertGreater(row["requests"]["delta_only"]["delta_owned_days_in_response"], 0)

    def test_the_crossing_request_really_spans_both_tiers(self):
        """Otherwise "crossing" is just a base read with a longer name."""
        row = sizing.measure_h5_span(delta_span=31)
        sources = row["requests"]["crossing"]["day_sources"]
        self.assertIn("base", sources.values())
        self.assertIn("delta", sources.values())

    def test_delta_wins_for_a_day_both_tiers_hold(self):
        """The composition rule, asserted on values from the ONE oracle."""
        row = sizing.measure_h5_span(delta_span=31)
        self.assertTrue(row["delta_precedence_ok"])
        self.assertEqual(row["delta_precedence_mismatches"], [])

    def test_the_precedence_check_CAN_fail(self):
        bad = sizing.measure_h5_span(delta_span=31, _negative_control=True)
        self.assertFalse(bad["delta_precedence_ok"])
        self.assertTrue(bad["delta_precedence_mismatches"])

    def test_the_fixture_and_cache_assumptions_are_recorded(self):
        row = sizing.measure_h5_span(delta_span=31)
        self.assertIn("ny", row["fixture_geometry"])
        self.assertIn("warm", row["cache_assumption"])
        self.assertEqual(row["delta_span_days"], 31)

    def test_H5_reaches_the_artifacts_and_adjudicates_nothing(self):
        for stamp in ("quick", "full"):
            with self.subTest(stamp):
                path = os.path.join(_DEV2026, "bench", "results", f"p5s6_sizing_{stamp}.json")
                with open(path) as fh:
                    art = json.load(fh)
                h5 = art["H5"]
                self.assertEqual(h5["label"], "LOCAL_SYNTHETIC")
                self.assertTrue(h5["measured"])
                self.assertEqual(art["gates"]["G3"]["verdict"], "NOT_ADJUDICATED")


class TestCapacityGate(unittest.TestCase):
    """§7.1c / G18. The conclusion I published -- "141 GB fits with margin" -- was wrong: it
    counted the block and nothing else, and compared it to free space rather than to
    HARD_RESERVE."""

    #: measured on VM24, 2026-08-19
    FREE_GB = 267.0
    BASE_GB_DAY = 1.62
    INGEST_GB_DAY = 2.74          # base 1.62 + delta 1.12, both measured

    def _gate(self, **kw):
        return sizing.capacity_gate(free_now_gb=self.FREE_GB,
                                    base_gb_per_day=self.BASE_GB_DAY,
                                    ingest_gb_per_day=self.INGEST_GB_DAY, **kw)

    def test_additional_is_more_than_the_block(self):
        g = self._gate()
        self.assertGreater(g["additional_gb"], g["new_block_gb"],
                           "additional must include temp/checkpoint and growth")
        self.assertGreater(g["growth_during_build_gb"], 0)

    def test_the_gate_is_BLOCKED_at_the_measured_free_space(self):
        g = self._gate()
        self.assertEqual(g["verdict"], "BLOCKED")
        self.assertFalse(g["gate_ok"])
        self.assertLess(g["projected_free_after_gb"], g["hard_reserve_gb"])
        self.assertGreater(g["shortfall_gb"], 0)

    def test_it_is_BLOCKED_even_under_the_most_FAVOURABLE_assumptions(self):
        """No zeroing of temp or growth rescues it -- the block alone already breaches the
        reserve. So the verdict does not depend on how the soft terms are estimated."""
        g = self._gate(build_days=0.0, temp_gb=0.0)
        self.assertEqual(g["verdict"], "BLOCKED")
        self.assertLess(g["projected_free_after_gb"], g["hard_reserve_gb"])

    def test_the_reserve_is_not_lowered_to_make_it_pass(self):
        self.assertEqual(sizing.HARD_RESERVE_GB, 250)
        self.assertIn("ops policy decision", self._gate()["note"])

    def test_it_reports_what_free_space_WOULD_be_needed(self):
        g = self._gate()
        self.assertAlmostEqual(g["free_now_required_gb"],
                               g["hard_reserve_gb"] + g["additional_gb"], places=1)
        self.assertGreater(g["free_now_required_gb"], self.FREE_GB)

    def test_the_arithmetic_agrees_with_the_SHIPPED_precheck(self):
        """Not "looks similar": the same inputs through `build_block.disk_precheck`, whose
        formula is the one production uses."""
        from unittest import mock
        from ingest import build_block as bb

        g = self._gate()
        free_bytes = int(self.FREE_GB * (1 << 30))

        class FakeStat:
            f_bavail = free_bytes
            f_frsize = 1

        with mock.patch("os.statvfs", return_value=FakeStat()):
            shipped = bb.disk_precheck(
                ".", block_bytes_estimate=int(g["new_block_gb"] * (1 << 30)),
                temp_bytes=int(g["temp_checkpoint_artifacts_gb"] * (1 << 30)),
                growth_bytes=int(g["growth_during_build_gb"] * (1 << 30)),
                hard_reserve_bytes=int(g["hard_reserve_gb"] * (1 << 30)))
        self.assertFalse(shipped["ok"], "the shipped precheck must also refuse")
        self.assertAlmostEqual(shipped["projected_free_after_bytes"] / (1 << 30),
                               g["projected_free_after_gb"], places=0)

    def test_a_machine_with_enough_room_PASSES(self):
        """Otherwise "always BLOCKED" would satisfy every test above."""
        g = sizing.capacity_gate(free_now_gb=500.0, base_gb_per_day=self.BASE_GB_DAY,
                                 ingest_gb_per_day=self.INGEST_GB_DAY)
        self.assertEqual(g["verdict"], "PASS")
        self.assertTrue(g["gate_ok"])
        self.assertEqual(g["shortfall_gb"], 0.0)


PARITY_EV = {"kind": "value_parity", "variables": ["sst", "sst_anomaly", "sea_ice"],
             "var_valid_checked": True, "nan_semantics_checked": True,
             "verification_provenance": "sha256 per-day digest vs base, run 2026-08-19",
             "day_range": ["2023-01-01", "2026-06-20"]}
MANIFEST_EV = {"kind": "external_manifest", "source": "https://podaac.jpl.nasa.gov/...",
               "day_coverage": ["2023-01-01", "2026-08-17"],
               "download_procedure": "documented in ops runbook X",
               "verification_method": "md5 from the provider manifest"}


class TestHoldLifecycleClassification(unittest.TestCase):
    """Round 2. The first version authorized a release from membership + spatial window alone.
    Membership is not parity, and "bbox does not need it" is not "nothing needs it"."""

    NOW = "2026-08-19T07:12:51+00:00"
    WINDOW = ("2026-06-27", "2026-08-17")
    BASE = {f"2026-06-{d:02d}" for d in range(1, 27)} | {"2026-01-01"}
    EXPIRED = {"day": "2026-01-01", "hold_until": "2026-08-13T00:00:00+00:00"}

    def _c(self, entry=None, **kw):
        kw.setdefault("base_days", self.BASE)
        kw.setdefault("spatial_window", self.WINDOW)
        kw.setdefault("p5_roots_present", True)
        return sizing.classify_hold_entry(entry=entry or dict(self.EXPIRED),
                                          now_utc=self.NOW, **kw)

    # ---- the defect this round exists to fix -------------------------------
    def test_being_outside_the_spatial_window_ALONE_never_authorizes(self):
        out = self._c()
        self.assertTrue(out["evidence"]["bbox_spatially_unneeded"])
        self.assertTrue(out["evidence"]["base_membership_covered"])
        self.assertNotEqual(out["klass"], "releasable_after_approval")
        self.assertFalse(out["evidence"]["release_authorized"])

    def test_membership_without_parity_or_recovery_is_recovery_policy_pending(self):
        out = self._c()
        self.assertEqual(out["klass"], "recovery_policy_pending")
        self.assertIn("none is sufficient", out["reason"])
        self.assertIn("not whether these BYTES can be recovered", out["reason"])
        self.assertFalse(out["evidence"]["recovery_content_verified"])
        self.assertIsNotNone(out["evidence"]["recovery_evidence_rejected_because"])

    def test_verified_parity_authorizes(self):  # noqa: D401
        out = self._c(recovery_evidence=PARITY_EV)
        self.assertEqual(out["klass"], "releasable_after_approval")
        self.assertTrue(out["evidence"]["release_authorized"])
        self.assertIn("REQUIRES HUMAN APPROVAL", out["reason"])

    def test_a_verified_recovery_source_authorizes(self):
        out = self._c(recovery_evidence=MANIFEST_EV)
        self.assertEqual(out["klass"], "releasable_after_approval")

    # ---- pre-P5 semantics --------------------------------------------------
    def test_absent_P5_roots_are_UNCHECKED_not_clear(self):
        out = self._c(p5_roots_present=False, recovery_evidence=MANIFEST_EV)
        self.assertEqual(out["klass"], "repair_state_unknown_pre_p5")
        self.assertIn("UNCHECKED, not clear", out["reason"])
        self.assertNotEqual(out["klass"], "releasable_after_approval")

    def test_a_pending_repair_blocks_release(self):
        out = self._c(wal_repairs_pending={"2026-01-01"}, recovery_evidence=MANIFEST_EV)
        self.assertEqual(out["klass"], "repair_state_unknown_pre_p5")

    # ---- reference / uniqueness -------------------------------------------
    def test_a_day_base_does_NOT_cover_is_a_unique_copy(self):
        out = self._c({"day": "2026-06-30", "hold_until": "2026-01-01T00:00:00+00:00"},
                      recovery_evidence=MANIFEST_EV)
        self.assertEqual(out["klass"], "blocked_unique_copy")

    def test_a_rollback_referenced_backup_is_not_releasable(self):
        out = self._c(referenced_by_rollback=True, recovery_evidence=MANIFEST_EV)
        self.assertEqual(out["klass"], "referenced")
        self.assertIn("rollback", out["reason"])

    def test_an_entry_on_the_active_serving_path_is_not_releasable(self):
        out = self._c(in_active_serving_path=True, recovery_evidence=MANIFEST_EV)
        self.assertEqual(out["klass"], "referenced")

    def test_an_unexpired_hold_is_referenced(self):
        out = self._c({"day": "2026-01-01", "hold_until": "2026-09-01T00:00:00+00:00"},
                      recovery_evidence=MANIFEST_EV)
        self.assertEqual(out["klass"], "referenced")

    def test_an_untracked_path_is_NEVER_releasable(self):
        out = self._c(tracked_in_manifest=False, recovery_evidence=PARITY_EV)
        self.assertEqual(out["klass"], "inconsistent_or_unknown")

    def test_missing_metadata_fails_closed(self):
        for entry in ({"day": "2026-01-01"}, {"hold_until": "2026-08-13T00:00:00+00:00"}):
            with self.subTest(sorted(entry)):
                self.assertEqual(self._c(entry, recovery_evidence=MANIFEST_EV)["klass"],
                                 "inconsistent_or_unknown")

    def test_no_objection_can_be_overridden_by_recovery_evidence(self):
        """Every blocker, each with parity AND recovery supplied: none may reach releasable."""
        cases = [
            ("unique copy", {"entry": {"day": "2026-06-30",
                                       "hold_until": "2026-01-01T00:00:00+00:00"}}),
            ("rollback", {"referenced_by_rollback": True}),
            ("serving", {"in_active_serving_path": True}),
            ("unexpired", {"entry": {"day": "2026-01-01",
                                     "hold_until": "2026-09-01T00:00:00+00:00"}}),
            ("untracked", {"tracked_in_manifest": False}),
            ("pre-p5", {"p5_roots_present": False}),
        ]
        for name, kw in cases:
            with self.subTest(name):
                out = self._c(recovery_evidence=PARITY_EV, **kw)
                self.assertNotEqual(out["klass"], "releasable_after_approval")
                self.assertFalse(out["evidence"]["release_authorized"])


class TestRecoveryEvidenceContract(unittest.TestCase):
    """Day-set coverage and supersession are deliberately not recovery proof."""

    def test_no_evidence_is_rejected(self):
        self.assertIsNotNone(sizing.validate_recovery_evidence(None))

    def test_a_bare_boolean_cannot_stand_in(self):
        for bad in (True, 1, "yes", [], "value_parity"):
            with self.subTest(repr(bad)):
                self.assertIsNotNone(sizing.validate_recovery_evidence(bad))

    def test_an_unknown_kind_is_rejected(self):
        self.assertIsNotNone(sizing.validate_recovery_evidence(
            {"kind": "day_set_coverage", "source": "x"}))

    def test_every_required_field_is_required(self):
        for drop in ("variables", "var_valid_checked", "nan_semantics_checked",
                     "verification_provenance", "day_range"):
            with self.subTest(drop):
                ev = {k: v for k, v in PARITY_EV.items() if k != drop}
                self.assertIsNotNone(sizing.validate_recovery_evidence(ev))

    def test_flags_must_be_exactly_True(self):
        for bad in ("true", 1, "false"):
            with self.subTest(repr(bad)):
                self.assertIsNotNone(sizing.validate_recovery_evidence(
                    {**PARITY_EV, "var_valid_checked": bad}))

    def test_valid_evidence_of_each_kind_passes(self):
        self.assertIsNone(sizing.validate_recovery_evidence(PARITY_EV))
        self.assertIsNone(sizing.validate_recovery_evidence(MANIFEST_EV))


class TestAuditEntryPointMatchesTheArtifact(unittest.TestCase):
    """The artifact must be GENERATED by the committed classifier, not assembled beside it."""

    def _evidence(self):
        with open(os.path.join(_DEV2026, "bench", "results",
                               "p5s6_vm24_hold_evidence.json")) as fh:
            return json.load(fh)

    def _run(self):
        """The evidence AS STORED. No expansion here: the helper used to widen the day range
        before calling, which meant the test corrected the evidence and then checked the audit.
        The audit expands and verifies it now."""
        return sizing.audit_hold_lifecycle_from_evidence(self._evidence())

    def test_the_artifact_matches_a_fresh_run_of_the_audit(self):
        fresh = self._run()
        with open(os.path.join(_DEV2026, "bench", "results",
                               "p5s6_vm24_readonly.json")) as fh:
            stored = json.load(fh)["hold_lifecycle_audit"]
        self.assertEqual([e["klass"] for e in stored["entries"]],
                         [e["klass"] for e in fresh["entries"]])
        self.assertEqual(stored["currently_reclaimable_gib"],
                         fresh["currently_reclaimable_gib"])

    def test_the_production_evidence_authorizes_NOTHING(self):
        fresh = self._run()
        self.assertEqual(fresh["currently_reclaimable_gib"], 0.0)
        for e in fresh["entries"]:
            self.assertNotEqual(e["klass"], "releasable_after_approval")
            self.assertFalse(e["evidence"]["release_authorized"])

    def test_the_audit_is_marked_audit_only_and_no_deletion(self):
        fresh = self._run()
        self.assertTrue(fresh["audit_only"])
        self.assertFalse(fresh["deletion_performed"])

    def test_incomplete_evidence_fails_closed(self):
        for drop in ("measured_utc", "base_day_range", "spatial_window", "p5_roots_present",
                     "entries", "now_utc", "host"):
            with self.subTest(drop):
                ev = self._evidence()
                ev.pop(drop)
                out = sizing.audit_hold_lifecycle_from_evidence(ev)
                self.assertEqual(out["verdict"], "FAIL_CLOSED")
                self.assertIn(drop, out["reason"])

    def test_every_raw_bool_refuses_a_COERCIBLE_value(self):
        """`bool("false")` is True. Verified against the real evidence file, per field."""
        for field in ("p5_roots_present",):
            for bad in ("false", "true", 0, 1, "", None, []):
                with self.subTest(f"{field}={bad!r}"):
                    ev = self._evidence()
                    ev[field] = bad
                    out = sizing.audit_hold_lifecycle_from_evidence(ev)
                    self.assertEqual(out["verdict"], "FAIL_CLOSED")
                    self.assertIn("raw bool", out["reason"])
        for field in ("tracked_in_manifest", "referenced_by_rollback",
                      "in_active_serving_path"):
            for bad in ("false", "true", 1, 0):
                with self.subTest(f"entry.{field}={bad!r}"):
                    ev = self._evidence()
                    ev["entries"][0][field] = bad
                    out = sizing.audit_hold_lifecycle_from_evidence(ev)
                    self.assertEqual(out["verdict"], "FAIL_CLOSED")
                    self.assertIn("raw bool", out["reason"])

    def test_a_string_false_cannot_bypass_the_pre_P5_block(self):
        """The reviewer's reproduction: p5_roots_present='false' produced
        release_authorized=True."""
        ev = self._evidence()
        ev["p5_roots_present"] = "false"
        out = sizing.audit_hold_lifecycle_from_evidence(ev)
        self.assertEqual(out["verdict"], "FAIL_CLOSED")
        self.assertNotIn("entries", out)

    def test_a_day_range_whose_arithmetic_disagrees_fails_closed(self):
        for bad, why in (({"start": "2023-01-01", "end": "2026-06-26", "day_count": 2},
                          "day_count"),
                         ({"start": "2026-06-26", "end": "2023-01-01", "day_count": 1273},
                          "precedes"),
                         ({"start": "2023-1-1", "end": "2026-06-26", "day_count": 1273},
                          "canonical"),
                         ({"start": "2023-01-01", "end": "2026-06-26", "day_count": 1273,
                           "gaps": 5}, "gaps")):
            with self.subTest(why):
                ev = self._evidence()
                ev["base_day_range"] = bad
                out = sizing.audit_hold_lifecycle_from_evidence(ev)
                self.assertEqual(out["verdict"], "FAIL_CLOSED")

    def test_the_day_sets_are_EXPANDED_not_two_endpoints(self):
        """1267 days, not 2. The endpoint form silently made membership a two-day check."""
        fresh = self._run()
        self.assertEqual(fresh["entries"][0]["day_count"], 1267)
        self.assertGreater(sum(e["day_count"] for e in fresh["entries"]), 1400)

    def test_the_big_entries_report_MORE_than_one_blocker(self):
        """Reporting only the first blocker reads as "fix this and it releases"."""
        fresh = self._run()
        daily = fresh["entries"][0]
        self.assertEqual(daily["klass"], "repair_state_unknown_pre_p5")
        self.assertTrue(daily["also_blocking"])
        self.assertTrue(any("recovery evidence" in b for b in daily["also_blocking"]))

    def test_supplying_valid_evidence_WOULD_authorize_on_a_p5_host(self):
        """Otherwise "always blocked" would satisfy every assertion above."""
        out = sizing.classify_hold_entry(
            entry={"day": "2023-01-01", "hold_until": "2026-08-13T00:00:00+00:00"},
            now_utc="2026-08-19T07:40:00+00:00", base_days={"2023-01-01"},
            spatial_window=("2026-06-27", "2026-08-17"),
            recovery_evidence=MANIFEST_EV, p5_roots_present=True)
        self.assertEqual(out["klass"], "releasable_after_approval")
        self.assertTrue(out["evidence"]["release_authorized"])


if __name__ == "__main__":
    unittest.main()
