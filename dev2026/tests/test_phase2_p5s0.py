"""dev2026 — P5-S0 harness reliability tests (written BEFORE the harness, local-first §13).

P5-S0 is the step that turns the design spec's scratch-probe numbers into *committed*
evidence. Before any of those numbers may be believed, the harness itself must be proven:

  (1) THE COUNTERS REALLY COUNT — a content rewrite is detected even when size and mtime
      are unchanged; and the analytic, cache-independent `chunk_count` equals the chunk
      keys a real zarr read actually touches (observed through a counting LocalStore).
  (2) THE FIXTURE REALLY HAS THE DECLARED GEOMETRY — chunk/shard shapes are read back
      FROM DISK, never trusted from the build parameters, and a mismatch raises.
  (3) THE GATE CAN FAIL — a fabricated result that does not reproduce H1/H2 must make the
      gate evaluator return FAIL. A gate that cannot fail is not a gate.

Only after those hold do the H1/H2/H3/H8 measurements below mean anything.

Run: dev2026/.venv/bin/python -m unittest dev2026.tests.test_phase2_p5s0
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

import numpy as np
import zarr

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, "..", "bench"))

from p5_cost import (  # noqa: E402
    CountingLocalStore,
    assert_geometry,
    changed_files,
    evaluate_s0_gate,
    observed_data_keys,
    point_column_cost,
    read_geometry,
    snapshot_tree,
)
import bench_p5_rmw  # noqa: E402


NY = NX = 256
CH = (90, 8, 8)
SH = (90, 128, 128)


def _mk(path, T, chunks=CH, shards=SH, ny=NY, nx=NX, store=None):
    target = store if store is not None else path
    g = zarr.open_group(target, mode="w", zarr_format=3)
    g.create_array("sst", shape=(T, ny, nx), dtype="float32",
                   chunks=chunks, shards=shards, fill_value=float("nan"))
    return g


def _fill(g, T, ny=NY, nx=NX, seed=0):
    rng = np.random.default_rng(seed)
    g["sst"][:] = (rng.random((T, ny, nx), dtype=np.float32) * 30.0)


# --------------------------------------------------------------------------- (1) counters
class TestCountersReallyCount(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_same_size_same_mtime_rewrite_is_detected(self):
        """The decisive counter test: a size-preserving, mtime-preserving rewrite MUST be
        detected. An (mtime, size) snapshot would silently miss it and report 0 bytes
        rewritten -- which would fabricate a passing H1/H2 result."""
        p = os.path.join(self.d, "f.bin")
        with open(p, "wb") as fh:
            fh.write(b"A" * 4096)
        st = os.stat(p)
        before = snapshot_tree(self.d)
        with open(p, "wb") as fh:
            fh.write(b"B" * 4096)                       # identical size, different content
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))  # identical mtime
        after = snapshot_tree(self.d)
        self.assertEqual(os.stat(p).st_size, 4096)
        self.assertEqual(os.stat(p).st_mtime_ns, st.st_mtime_ns)
        n, nbytes = changed_files(before, after)
        self.assertEqual(n, 1, "content rewrite must be detected despite identical mtime+size")
        self.assertEqual(nbytes, 4096)

    def test_reset_keeps_derived_read_only_store_attributed(self):
        """`open_group(store, mode='r')` derives a store via `with_read_only`. If `reset`
        rebinds instead of clearing, the derived store keeps appending to the orphaned
        list and every observed count silently reads 0."""
        path = os.path.join(self.d, "shared")
        st = CountingLocalStore(path)
        g = _mk(path, 90, store=st)
        _fill(g, 90)
        handle = zarr.open_group(st, mode="r")        # derived store shares the list
        st.reset()
        np.asarray(handle["sst"][0:90, 1, 1])
        self.assertGreater(st.read_count, 0, "reads through the derived store must count")
        self.assertGreater(len(observed_data_keys(st)), 0)

    def test_untouched_tree_reports_no_change(self):
        p = os.path.join(self.d, "f.bin")
        with open(p, "wb") as fh:
            fh.write(b"A" * 1024)
        before = snapshot_tree(self.d)
        after = snapshot_tree(self.d)
        self.assertEqual(changed_files(before, after), (0, 0))

    def test_only_changed_files_are_counted(self):
        for name, payload in (("a", b"a" * 100), ("b", b"b" * 200), ("c", b"c" * 300)):
            with open(os.path.join(self.d, name), "wb") as fh:
                fh.write(payload)
        before = snapshot_tree(self.d)
        with open(os.path.join(self.d, "b"), "wb") as fh:
            fh.write(b"z" * 200)
        n, nbytes = changed_files(before, snapshot_tree(self.d))
        self.assertEqual((n, nbytes), (1, 200))

    def test_new_file_counts_as_changed(self):
        before = snapshot_tree(self.d)
        with open(os.path.join(self.d, "new"), "wb") as fh:
            fh.write(b"x" * 77)
        self.assertEqual(changed_files(before, snapshot_tree(self.d)), (1, 77))

    def test_analytic_chunk_count_equals_observed_chunk_keys(self):
        """`chunk_count` is analytic (cache-independent, P2 method). Here it is validated
        against the chunk keys a REAL read touches, via a counting LocalStore."""
        path = os.path.join(self.d, "obs")
        st = CountingLocalStore(path)
        g = _mk(path, 180, store=st)
        _fill(g, 180)
        geom = read_geometry(path, "sst")
        cost = point_column_cost(geom, t0=0, t1=180, ii=33, jj=17)
        st.reset()
        vals = np.asarray(zarr.open_group(st, mode="r")["sst"][0:180, 33, 17])
        self.assertEqual(vals.size, 180)
        observed = observed_data_keys(st)
        self.assertEqual(cost["chunk_count"], len(observed),
                         f"analytic {cost['chunk_count']} != observed {len(observed)}: {sorted(observed)}")
        self.assertEqual(cost["chunk_count"], 2)        # 180 days / time_chunk 90

    def test_analytic_tail_window_on_non_aligned_array_matches_observed(self):
        """The offset/clipping trap: a request reads the TAIL of base, not the head, and a
        base whose length is NOT a multiple of the time chunk has a clipped final chunk.
        Measuring `t0=0` while the request reads the tail would report the wrong
        chunk_count and nothing else would catch it. Validated against observed chunk keys
        at several non-aligned offsets."""
        path = os.path.join(self.d, "nonaligned")
        T = 205                                          # 205 = 2*90 + 25 -> clipped tail
        st = CountingLocalStore(path)
        g = _mk(path, T, store=st)
        _fill(g, T)
        geom = read_geometry(path, "sst")
        self.assertEqual(geom["shape"][0], T)
        handle = zarr.open_group(st, mode="r")
        for t0, t1 in ((T - 100, T), (T - 1, T), (89, 91), (0, T), (100, 190)):
            with self.subTest(window=(t0, t1)):
                cost = point_column_cost(geom, t0=t0, t1=t1, ii=5, jj=7)
                st.reset()
                vals = np.asarray(handle["sst"][t0:t1, 5, 7])
                self.assertEqual(vals.size, t1 - t0)
                self.assertEqual(cost["chunk_count"], len(observed_data_keys(st)),
                                 f"window {(t0, t1)}: analytic {cost['chunk_count']} != "
                                 f"observed {len(observed_data_keys(st))}")
        # the clipped final chunk really is shorter than the others
        tail = point_column_cost(geom, t0=180, t1=T, ii=5, jj=7)
        head = point_column_cost(geom, t0=0, t1=25, ii=5, jj=7)
        self.assertEqual(tail["chunk_count"], head["chunk_count"])
        self.assertLess(tail["decompressed_bytes"], head["decompressed_bytes"],
                        "the clipped tail chunk must decompress fewer bytes than a full one")

    def test_analytic_cost_matches_hand_computed(self):
        geom = {"shape": (360, NY, NX), "chunks": (90, 8, 8), "shards": SH}
        cost = point_column_cost(geom, t0=0, t1=360, ii=0, jj=0)
        self.assertEqual(cost["chunk_count"], 4)
        self.assertEqual(cost["decompressed_bytes"], 4 * 90 * 8 * 8 * 4)
        self.assertEqual(cost["useful_bytes"], 360 * 4)
        # a 30-day block layout: same days, more chunks, ~same decompressed bytes
        geom30 = {"shape": (360, NY, NX), "chunks": (30, 8, 8), "shards": (30, 128, 128)}
        c30 = point_column_cost(geom30, t0=0, t1=360, ii=0, jj=0)
        self.assertEqual(c30["chunk_count"], 12)
        self.assertEqual(c30["decompressed_bytes"], cost["decompressed_bytes"])


# --------------------------------------------------------------------------- (2) geometry
class TestFixtureGeometryIsSelfValidating(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_geometry_is_read_back_from_disk(self):
        path = os.path.join(self.d, "g")
        _fill(_mk(path, 90), 90)
        geom = read_geometry(path, "sst")               # fresh open, not build params
        self.assertEqual(tuple(geom["chunks"]), CH)
        self.assertEqual(tuple(geom["shards"]), SH)
        self.assertEqual(tuple(geom["shape"]), (90, NY, NX))

    def test_declared_geometry_mismatch_raises(self):
        """The harness must never take its own build parameters on trust."""
        path = os.path.join(self.d, "g")
        _fill(_mk(path, 90), 90)
        assert_geometry(path, "sst", chunks=CH, shards=SH)          # truth: passes
        with self.assertRaises(AssertionError):
            assert_geometry(path, "sst", chunks=(45, 8, 8), shards=SH)
        with self.assertRaises(AssertionError):
            assert_geometry(path, "sst", chunks=CH, shards=(90, 64, 64))


# --------------------------------------------------------------------------- (3) H1/H2/H3/H8
class TestHypothesisMeasurements(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_h1_single_day_overwrite_rewrites_the_whole_block(self):
        r = bench_p5_rmw.measure_h1(self.d, ny=NY, nx=NX, time_chunk=90, shard=128)
        self.assertEqual(r["data_shard_files_rewritten"], r["data_shard_files_total"],
                         "every DATA shard spanning the time block must be rewritten")
        self.assertGreater(r["amplification_vs_average_stored_day"], 45.0,
                           "H1 predicts ~time_chunk-fold amplification")
        self.assertIsNotNone(r["amplification_vs_uncompressed_day"])
        self.assertTrue(r["h1_confirmed"])

    def test_h1_separates_data_shards_from_metadata(self):
        """A 4x4-shard block has 16 DATA shards. Counting `zarr.json` with them would
        report 17 and quietly overstate the shard count."""
        r = bench_p5_rmw.measure_h1(self.d, ny=256, nx=256, time_chunk=90, shard=128)
        self.assertEqual(r["data_shard_files_total"], 4)          # 256/128 = 2 -> 2x2
        self.assertEqual(r["data_shard_files_rewritten"], 4)
        self.assertNotIn("shard_files_rewritten", r, "the ambiguous field must be gone")
        self.assertGreater(r["data_bytes_rewritten"], 0)

    def test_h2_partial_tail_append_rewrites_the_partial_shard(self):
        r = bench_p5_rmw.measure_h2(self.d, ny=NY, nx=NX, tail_lengths=(10, 30),
                                    time_chunk=90, shard=128)
        for row in r["rows"]:
            self.assertGreater(row["amplification_vs_average_stored_day"],
                               row["tail_len"] * 0.5,
                               "appending into a partial shard rewrites the whole shard")
            # the resize touches zarr.json; it must be reported, but separately
            self.assertGreaterEqual(row["metadata_files_changed"], 1)
            self.assertEqual(row["data_shard_files_rewritten"], row["data_shard_files_total"])
        self.assertTrue(r["h2_confirmed"])
        amps = [row["amplification_vs_average_stored_day"] for row in r["rows"]]
        self.assertLess(amps[0], amps[1], "amplification must grow with tail length")

    def test_h2_reports_both_amplification_bases(self):
        """`stored_bytes/L` is the average COMPRESSED stored bytes per day, not logical
        uncompressed bytes per day. Both are reported, and they are not the same number."""
        r = bench_p5_rmw.measure_h2(self.d, ny=NY, nx=NX, tail_lengths=(30,),
                                    time_chunk=90, shard=128)
        row = r["rows"][0]
        self.assertEqual(row["logical_uncompressed_bytes_per_day"], NY * NX * 4)
        self.assertNotEqual(row["average_stored_bytes_per_day"],
                            row["logical_uncompressed_bytes_per_day"])
        self.assertGreater(row["amplification_vs_average_stored_day"], 0)
        self.assertGreater(row["amplification_vs_uncompressed_day"], 0)

    def test_h3_constant_time_chunk_preserves_chunk_count_and_bytes(self):
        """H3 as stated: at a CONSTANT inner time chunk (segments >= 90 d), neither
        cache-independent counter may move."""
        r = bench_p5_rmw.measure_h3(self.d, ny=NY, nx=NX, total_days=180, segment_counts=(1, 2))
        ctc = r["constant_time_chunk"]
        rows = {row["segments"]: row for row in ctc["rows"]}
        self.assertTrue(ctc["time_chunk_constant"])
        self.assertEqual(rows[1]["chunk_count"], rows[2]["chunk_count"],
                         "segmentation must NOT change the cache-independent chunk count")
        self.assertEqual(rows[1]["decompressed_bytes"], rows[2]["decompressed_bytes"])
        self.assertGreater(rows[1]["store_reads"], 0,
                           "the observed counter must actually count inside the harness "
                           "path, not only in isolation")
        self.assertGreaterEqual(rows[2]["store_reads"], rows[1]["store_reads"],
                                "more segments => at least as many store-level reads")
        self.assertTrue(r["h3_supported"])

    def test_h3_smaller_blocks_raise_chunk_count_at_constant_bytes(self):
        """The §10.4 sizing trade, and the distinction an earlier harness version got
        wrong: a sub-90-day block shrinks the inner time chunk, so chunk_count RISES while
        decompressed_bytes stays put. That is not an H3 violation."""
        r = bench_p5_rmw.measure_h3(self.d, ny=NY, nx=NX, total_days=180,
                                    segment_counts=(1, 2), block_size_segments=(2, 6))
        sweep = {row["segments"]: row for row in r["block_size_sweep"]["rows"]}
        self.assertEqual(sweep[2]["time_chunk"], 90)
        self.assertEqual(sweep[6]["time_chunk"], 30)
        self.assertGreater(sweep[6]["chunk_count"], sweep[2]["chunk_count"])
        self.assertEqual(sweep[6]["decompressed_bytes"], sweep[2]["decompressed_bytes"])
        self.assertTrue(r["block_size_sweep"]["decompressed_bytes_invariant"])
        self.assertTrue(r["h3_supported"], "a shrinking time chunk must NOT fail H3")

    def test_h8_rectilinear_is_measured_not_assumed(self):
        r = bench_p5_rmw.measure_h8(self.d, ny=NY, nx=NX)
        self.assertIn(r["rect_chunks_with_sharding"]["supported"], (True, False))
        if r["rect_chunks_with_sharding"]["supported"] is False:
            self.assertTrue(r["rect_chunks_with_sharding"]["error"])
        self.assertIn("verdict", r)


# --------------------------------------------------------------------------- (4) artifacts + gate
class TestArtifactsAndGate(unittest.TestCase):
    def test_artifact_carries_provenance_and_geometry(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        r = bench_p5_rmw.measure_h1(d, ny=NY, nx=NX, time_chunk=90, shard=128)
        for key in ("geometry", "data_shard_files_total", "data_shard_files_rewritten",
                    "metadata_files_changed", "data_bytes_rewritten",
                    "average_stored_bytes_per_day", "logical_uncompressed_bytes_per_day",
                    "amplification_vs_average_stored_day", "amplification_vs_uncompressed_day"):
            self.assertIn(key, r)
        self.assertEqual(tuple(r["geometry"]["chunks"]), CH)

    def test_gate_fails_when_h1_is_not_reproduced(self):
        """A fabricated low-amplification H1 must FAIL the gate -- proving the gate bites."""
        good = {"h1": {"h1_confirmed": True, "amplification_vs_average_stored_day": 90.0},
                "h2": {"h2_confirmed": True}, "h3": {"h3_supported": True}}
        self.assertEqual(evaluate_s0_gate(good)["verdict"], "PASS")

        bad = {"h1": {"h1_confirmed": False, "amplification_vs_average_stored_day": 1.02},
               "h2": {"h2_confirmed": True}, "h3": {"h3_supported": True}}
        out = evaluate_s0_gate(bad)
        self.assertEqual(out["verdict"], "FAIL")
        self.assertIn("H1", " ".join(out["failures"]))

    def test_gate_fails_when_h2_is_not_reproduced(self):
        bad = {"h1": {"h1_confirmed": True, "amplification_vs_average_stored_day": 90.0},
               "h2": {"h2_confirmed": False}, "h3": {"h3_supported": True}}
        out = evaluate_s0_gate(bad)
        self.assertEqual(out["verdict"], "FAIL")
        self.assertIn("H2", " ".join(out["failures"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
