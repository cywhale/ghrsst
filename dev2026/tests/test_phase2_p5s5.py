"""dev2026 — P5-S5: source-BYTE provenance (spec §5, §7.5a E2, §7.8a).

Gate: a block may be published only when the builder's **checksummed provenance artifact**
shows that the block's bytes are the bytes its sources held. Locator provenance — which store,
which slot — is necessary and is not that, and must never authorize a prune on its own.

Everything here goes through the real `build_block` → real artifact → real `execute_publication`
path. A hand-written fingerprint would make these pass or fail for reasons unrelated to what
they test.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np
import zarr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import p5_fixtures as fx  # noqa: E402
from ingest import publish_manifest as pub  # noqa: E402
from ingest.build_block import build_block  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store import source_provenance as sp  # noqa: E402

ANCHOR = "2026-06-27"
S = 90


def _build(out_path, *, days, delta_path, artifacts_dir, start, end, **kw):
    return build_block(out_path, start_day=start, end_day=end,
                       classification_target=list(days), delta_path=delta_path,
                       artifacts_dir=artifacts_dir, lock=None, hard_reserve_bytes=0,
                       unsafe_skip_isolation=True, **kw)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "manifest_root")
        os.makedirs(self.root, exist_ok=True)
        self.lock = os.path.join(self.tmp, "ingest.lock")
        self.s0, self.e0 = bm.block_bounds(ANCHOR, S, 0)
        self.span = fx.calendar_span(self.s0, self.e0)
        self.days = self.span[:12]
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.days)
        self.artifacts = os.path.join(self.tmp, "artifacts")
        os.makedirs(self.artifacts, exist_ok=True)

    @staticmethod
    def _read(path, mode="rb"):
        with open(path, mode) as fh:
            return fh.read()

    def _artifact_path(self):
        return os.path.join(self.artifacts, sp.ARTIFACT_NAME)

    def _fold(self, name="b_v1"):
        """A real fold: builder reads the delta, writes the block, emits the artifact."""
        return _build(os.path.join(self.root, name + ".zarr"), days=self.days,
                      delta_path=self.delta, artifacts_dir=self.artifacts,
                      start=self.s0, end=self.e0)

    def _manifest_gen0(self):
        """A live generation 1 holding an unrelated seed block, so the fold publishes as an
        addition. `validate_manifest` requires a non-empty `segments`, and an empty live
        manifest is not a state the system ever reaches."""
        s1, e1 = bm.block_bounds(ANCHOR, S, 1)
        span1 = fx.calendar_span(s1, e1)
        seed_path = os.path.join(self.root, "b_seed.zarr")
        fx.build_block(seed_path, span1[:5])
        insp = bm.inspect_store_contract(seed_path)
        seed = {
            "segment_id": "b_seed", "kind": "block", "path": "b_seed.zarr",
            "immutable": True, "boundary_kind": "calendar",
            "start_day": s1, "end_day": e1, "materialized_through": span1[4],
            "day_count": 5, "gaps": [], "unknown": list(span1[5:]), "day_list": None,
            "layout": bm.segment_layout_from_inspection(insp),
            "variables": sorted(insp.vars),
            "fingerprint": {"algo": "sha256",
                            "metadata": bm.metadata_fingerprint_from_inspection(insp),
                            "day_digest": bm.day_digest(insp.days)},
            "precedence": 1, "sealed": False, "supersedes": None,
        }
        m = {
            "format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION,
            "generation": 1, "generation_id": "gen000001-x",
            "created_utc": "2027-01-01T00:00:00Z", "created_by": "p5s5-tests",
            "predecessor_generation": None, "predecessor_manifest": None,
            "grid": {"ny": 32, "nx": 32, "region": [0, 32, 0, 32]},
            "variables": list(fx.VARS),
            "block_grid": {"anchor_day": ANCHOR, "block_days": S},
            "segments": [seed], "superseded": [], "manifest_checksum": "",
        }
        m["manifest_checksum"] = bm.compute_checksum(m)
        bm.publish(self.root, m)

    def _publish(self, plan, **kw):
        kw.setdefault("compaction_lock_path", None)
        kw.setdefault("unsafe_skip_compaction_lock", True)
        kw.setdefault("build_artifact_path", self._artifact_path())
        kw.setdefault("delta_path", self.delta)
        return pub.execute_publication(
            pub.plan_publication(self.root, plan, now=None),
            ingest_lock_path=self.lock, **kw)


# ==================================================================== the primitive
class TestFingerprintSemantics(_Base):
    """§7.5a E2: point-wise, float32-semantic, NaN-aware, `var_valid`-aware."""

    def test_it_is_point_wise_and_never_materializes_a_slab(self):
        """The window is one tile, and the compared cells are individual points inside it --
        the P4-S6 OOM lesson, kept executable."""
        i0, i1, j0, j1 = sp.sample_window(17999, 36000, seed=1, day_index=0)
        self.assertLessEqual(i1 - i0, sp.SAMPLE_WINDOW)
        self.assertLessEqual(j1 - j0, sp.SAMPLE_WINDOW)
        offs = sp.sample_offsets(i1 - i0, j1 - j0, seed=1, day_index=0)
        self.assertEqual(len(offs), sp.SAMPLE_POINTS)
        self.assertEqual(len(set(offs)), len(offs), "sampled points must be distinct")

    def test_the_window_moves_with_the_day(self):
        """A fixed window would leave the same region of every day unexamined forever."""
        seen = {sp.sample_window(4096, 4096, seed=3, day_index=d) for d in range(12)}
        self.assertGreater(len(seen), 1)

    def test_it_is_deterministic_for_the_same_seed_and_day(self):
        a = sp.sample_window(4096, 4096, seed=3, day_index=5)
        b = sp.sample_window(4096, 4096, seed=3, day_index=5)
        self.assertEqual(a, b)
        self.assertNotEqual(a, sp.sample_window(4096, 4096, seed=4, day_index=5))

    def _fp(self, arr, **kw):
        return sp.window_fingerprint({"sst": arr}, seed=1, day_index=0,
                                     var_valid=kw.pop("valid", {"sst": True}))

    def test_NaN_compares_equal_to_NaN_and_not_to_a_value(self):
        """Land is NaN. A naive `==` reports every land cell as a difference; a naive
        `nan == nan` check reports every land cell as a match."""
        a = np.full((8, 8), np.nan, dtype=np.float32)
        b = np.full((8, 8), np.nan, dtype=np.float32)
        self.assertEqual(self._fp(a), self._fp(b))
        c = b.copy()
        c[0, 0] = 1.0
        self.assertNotEqual(self._fp(a), self._fp(c))

    def test_absent_is_distinct_from_present_but_NaN(self):
        """P1 omit semantics vs land. Collapsing them is the gap<->present confusion E2 exists
        to catch."""
        nan_tile = np.full((8, 8), np.nan, dtype=np.float32)
        absent = sp.window_fingerprint({"sst": None}, seed=1, day_index=0,
                                       var_valid={"sst": False})
        present = sp.window_fingerprint({"sst": nan_tile}, seed=1, day_index=0,
                                        var_valid={"sst": True})
        self.assertNotEqual(absent, present)

    def test_var_valid_alone_changes_the_fingerprint(self):
        arr = np.zeros((8, 8), dtype=np.float32)
        a = sp.window_fingerprint({"sst": arr}, seed=1, day_index=0, var_valid={"sst": True})
        b = sp.window_fingerprint({"sst": arr}, seed=1, day_index=0, var_valid={"sst": False})
        self.assertNotEqual(a, b)

    def test_float32_semantics_not_float64(self):
        """A float64 promotion of a float32 store differs from a float64 read of another in
        the low bits; comparing in float64 would report a difference that is not one."""
        base = np.array([[np.float32(0.1)]], dtype=np.float32)
        same = np.array([[np.float64(np.float32(0.1))]], dtype=np.float64)
        self.assertEqual(self._fp(base), self._fp(same))

    def test_compare_days_names_the_variable_and_the_cell(self):
        a = np.zeros((8, 8), dtype=np.float32)
        b = a.copy()
        offs = sp.sample_offsets(8, 8, seed=1, day_index=0)
        b[offs[0]] = 5.0
        diffs = sp.compare_days({"sst": a}, {"sst": b}, seed=1, day_index=0,
                                a_valid={"sst": True}, b_valid={"sst": True})
        self.assertTrue(diffs)
        self.assertIn("sst", diffs[0])

    def test_compare_days_reports_var_valid_disagreement(self):
        a = np.zeros((8, 8), dtype=np.float32)
        diffs = sp.compare_days({"sst": a}, {"sst": a}, seed=1, day_index=0,
                                a_valid={"sst": True}, b_valid={"sst": False})
        self.assertTrue(any("var_valid" in d for d in diffs))

    def test_identical_days_compare_clean(self):
        a = np.arange(64, dtype=np.float32).reshape(8, 8)
        self.assertEqual(sp.compare_days({"sst": a}, {"sst": a.copy()}, seed=1, day_index=0,
                                         a_valid={"sst": True}, b_valid={"sst": True}), [])


# ==================================================================== the artifact
class TestArtifactIntegrity(_Base):
    def test_the_builder_emits_a_checksummed_artifact(self):
        plan = self._fold()
        path = self._artifact_path()
        self.assertTrue(os.path.isfile(path))
        doc = sp.load_artifact(path, expected_vars=fx.VARS)
        self.assertEqual(doc["segment_id"], plan["segment"]["segment_id"])
        self.assertEqual(set(doc["days"]), set(self.days))
        for day, rec in doc["days"].items():
            self.assertEqual(rec["source_kind"], "delta")
            self.assertEqual(rec["source_path"], os.path.realpath(self.delta))
            self.assertTrue(rec["source_fingerprint"])

    def test_a_tampered_artifact_is_refused(self):
        self._fold()
        path = self._artifact_path()
        doc = json.loads(self._read(path).decode())
        doc["days"][self.days[0]]["source_fingerprint"] = "0" * 64
        with open(path, "w") as fh:
            json.dump(doc, fh)
        with self.assertRaises(sp.ProvenanceError) as cm:
            sp.load_artifact(path, expected_vars=fx.VARS)
        self.assertIn("artifact_checksum", str(cm.exception))

    def test_a_truncated_artifact_is_refused(self):
        self._fold()
        path = self._artifact_path()
        raw = self._read(path)
        with open(path, "wb") as fh:
            fh.write(raw[: len(raw) // 2])
        with self.assertRaises(sp.ProvenanceError):
            sp.load_artifact(path, expected_vars=fx.VARS)

    def test_a_missing_artifact_is_refused(self):
        with self.assertRaises(sp.ProvenanceError) as cm:
            sp.load_artifact(os.path.join(self.tmp, "nope.json"), expected_vars=fx.VARS)
        self.assertIn("no provenance artifact", str(cm.exception))

    def test_an_incomplete_day_record_is_refused(self):
        self._fold()
        path = self._artifact_path()
        doc = json.loads(self._read(path).decode())
        del doc["days"][self.days[0]]["source_fingerprint"]
        doc["artifact_checksum"] = sp.artifact_checksum(doc)   # a forger would reseal
        with open(path, "w") as fh:
            json.dump(doc, fh)
        with self.assertRaises(sp.ProvenanceError) as cm:
            sp.load_artifact(path, expected_vars=fx.VARS)
        self.assertIn("wrong field set", str(cm.exception))

    def test_an_unknown_extra_field_is_refused(self):
        self._fold()
        path = self._artifact_path()
        doc = json.loads(self._read(path).decode())
        doc["days"][self.days[0]]["note"] = "hand-edited"
        doc["artifact_checksum"] = sp.artifact_checksum(doc)
        with open(path, "w") as fh:
            json.dump(doc, fh)
        with self.assertRaises(sp.ProvenanceError):
            sp.load_artifact(path, expected_vars=fx.VARS)

    def test_a_wrong_format_or_version_is_refused(self):
        self._fold()
        path = self._artifact_path()
        for field, bad in (("format", "something.else"), ("version", 99)):
            doc = json.loads(self._read(path).decode())
            doc[field] = bad
            doc["artifact_checksum"] = sp.artifact_checksum(doc)
            with open(path, "w") as fh:
                json.dump(doc, fh)
            with self.assertRaises(sp.ProvenanceError) as cm:
                sp.load_artifact(path, expected_vars=fx.VARS)
            self.assertIn("format/version", str(cm.exception))


# ==================================================================== publication verification
class TestPublicationVerifiesSourceBytes(_Base):
    def setUp(self):
        super().setUp()
        self._manifest_gen0()
        self.plan = self._fold()

    def _seg_plan(self):
        return {"segment": self.plan["segment"], "out_path": self.plan["out_path"],
                "source_map": self.plan["source_map"]}

    def test_a_real_fold_publishes_and_the_sources_are_rechecked(self):
        """End to end: the builder read the delta, and at publication the same windows are
        re-read from the delta and must still agree."""
        out = self._publish(self._seg_plan())
        self.assertEqual(out["status"], "published")
        self.assertTrue(out["provenance"]["verified"])
        self.assertEqual(out["provenance"]["days"], len(self.days))
        self.assertEqual(out["provenance"]["sources_rechecked"], len(self.days),
                         "every source was still present, so every one must be rechecked")

    def test_the_artifact_is_REQUIRED(self):
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        with self.assertRaises(pub.PublishRefused) as cm:
            pub.execute_publication(
                pub.plan_publication(self.root, self._seg_plan(), now=None),
                ingest_lock_path=self.lock, delta_path=self.delta,
                compaction_lock_path=None, unsafe_skip_compaction_lock=True,
                build_artifact_path=None)
        self.assertIn("build_artifact_path is required", str(cm.exception))
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_a_BLOCK_altered_after_the_build_is_refused(self):
        """The check no locator can make. Every path, index and day is untouched; only the
        bytes changed."""
        g = zarr.open_group(self.plan["out_path"], mode="a")
        i0, i1, j0, j1 = sp.sample_window(32, 32, seed=20260805, day_index=0)
        patch = np.asarray(g["sst"][0, i0:i1, j0:j1]).copy()
        patch += 10.0
        g["sst"][0, i0:i1, j0:j1] = patch
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        with self.assertRaises(pub.PublishRefused) as cm:
            self._publish(self._seg_plan())
        self.assertIn("do not match the fingerprint", str(cm.exception))
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_a_SOURCE_modified_in_place_after_the_build_is_refused(self):
        """The case locator provenance is structurally blind to: same store, same index, same
        day -- different bytes."""
        g = zarr.open_group(self.delta, mode="a")
        i0, i1, j0, j1 = sp.sample_window(32, 32, seed=20260805, day_index=0)
        patch = np.asarray(g["sst"][0, i0:i1, j0:j1]).copy() + 7.0
        g["sst"][0, i0:i1, j0:j1] = patch
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        with self.assertRaises(pub.PublishRefused) as cm:
            self._publish(self._seg_plan())
        self.assertIn("no longer holds the bytes", str(cm.exception))
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_an_artifact_covering_different_days_is_refused(self):
        path = self._artifact_path()
        doc = json.loads(self._read(path).decode())
        doc["days"].pop(self.days[-1])
        doc["artifact_checksum"] = sp.artifact_checksum(doc)
        with open(path, "w") as fh:
            json.dump(doc, fh)
        with self.assertRaises(pub.PublishRefused) as cm:
            self._publish(self._seg_plan())
        self.assertIn("different days", str(cm.exception))

    def test_a_GONE_source_aborts_at_the_staleness_guard(self):
        """Publication runs the cheap locator check before the expensive byte check, so a
        vanished delta is caught there first. Asserted as what actually happens rather than as
        what the provenance layer would have said."""
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        shutil.rmtree(self.delta)
        out = self._publish(self._seg_plan())
        self.assertEqual(out["status"], "aborted_stale")
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_verify_build_artifact_REFUSES_a_gone_source_directly(self):
        """Not being able to check is not the same as checking. The staleness guard shadows
        this path in publication, so it is exercised where it can be reached -- otherwise the
        refusal would be an untestable claim."""
        from ingest.build_block import _SourceReader
        shutil.rmtree(self.delta)
        with self.assertRaises(pub.PublishRefused) as cm:
            pub.verify_build_artifact(
                self.plan["source_map"], self._artifact_path(),
                segment_id=self.plan["segment"]["segment_id"],
                block_path=self.plan["out_path"], where="probe",
                source_reader=_SourceReader())
        self.assertIn("gone or unreadable", str(cm.exception))

    def test_every_source_is_rechecked_none_are_silently_skipped(self):
        """The count is asserted against the day count, so a source quietly not re-read shows
        up as a number rather than as a pass."""
        out = self._publish(self._seg_plan())
        self.assertEqual(out["provenance"]["sources_rechecked"], len(self.days))


# ==================================================================== the distinction itself
class TestLocatorIsNotByteProvenance(_Base):
    def test_a_locator_perfect_artifact_still_fails_the_byte_check(self):
        """The whole point of this step, stated as a test: every locator field agrees, the
        manifest agrees with itself, and the bytes are still wrong."""
        self._manifest_gen0()
        plan = self._fold()
        path = self._artifact_path()
        doc = sp.load_artifact(path, expected_vars=fx.VARS)

        # locator side: identical to the manifest's map, by construction
        for day, rec in doc["days"].items():
            self.assertEqual(
                {k: rec[k] for k in ("source_kind", "source_path", "source_day_index")},
                plan["source_map"][day])

        g = zarr.open_group(plan["out_path"], mode="a")
        i0, i1, j0, j1 = sp.sample_window(32, 32, seed=20260805, day_index=1)
        g["sst"][1, i0:i1, j0:j1] = np.asarray(g["sst"][1, i0:i1, j0:j1]) + 1.0
        with self.assertRaises(pub.PublishRefused) as cm:
            self._publish({"segment": plan["segment"], "out_path": plan["out_path"],
                           "source_map": plan["source_map"]})
        self.assertIn("do not match the fingerprint", str(cm.exception))

    def test_the_fingerprint_comes_from_the_SOURCE_not_from_what_was_written(self):
        """The difference between provenance and self-certification.

        If the builder fingerprinted its own output, the artifact would say "the block contains
        what the block contains" -- true of any block, including one written wrongly. Here the
        fill writes corrupted tiles while the fingerprint pass reads the source cleanly, so the
        artifact records the SOURCE bytes and publication catches the bad write. An
        output-fingerprinting builder publishes it happily."""
        import ingest.build_block as bbmod
        self._manifest_gen0()
        real = bbmod._SourceReader.read_tile
        budget = {"n": len(self.days) * len(fx.VARS)}          # one tile per (day, var) at 32x32

        def corrupting(self_reader, source, var, i0, i1, j0, j1):
            out = real(self_reader, source, var, i0, i1, j0, j1)
            if out is not None and budget["n"] > 0:
                budget["n"] -= 1
                return np.asarray(out) + 5.0                   # the FILL gets bad bytes
            return out                                         # the fingerprint pass does not

        bbmod._SourceReader.read_tile = corrupting
        try:
            plan = self._fold()
        finally:
            bbmod._SourceReader.read_tile = real
        self.assertEqual(budget["n"], 0, "precondition: the fill really was corrupted")

        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        with self.assertRaises(pub.PublishRefused) as cm:
            self._publish({"segment": plan["segment"], "out_path": plan["out_path"],
                           "source_map": plan["source_map"]})
        self.assertIn("do not match the fingerprint", str(cm.exception))
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_the_artifact_segment_id_must_match_the_published_segment(self):
        """Covered in the P5-S4 suite too; asserted here so the S5 suite alone is sufficient."""
        self._manifest_gen0()
        plan = self._fold()
        path = self._artifact_path()
        doc = json.loads(self._read(path).decode())
        doc["segment_id"] = "someone_else"
        doc["artifact_checksum"] = sp.artifact_checksum(doc)
        with open(path, "w") as fh:
            json.dump(doc, fh)
        with self.assertRaises(pub.PublishRefused) as cm:
            self._publish({"segment": plan["segment"], "out_path": plan["out_path"],
                           "source_map": plan["source_map"]})
        self.assertIn("describes segment", str(cm.exception))

    def test_the_module_documents_that_a_sample_is_not_a_proof(self):
        """E2 is probabilistic and defence in depth; E1's repair identity is the deterministic
        authorization. Asserted on the returned report, not on prose: the report never claims
        more than 'verified'."""
        self._manifest_gen0()
        plan = self._fold()
        out = self._publish({"segment": plan["segment"], "out_path": plan["out_path"],
                             "source_map": plan["source_map"]})
        self.assertEqual(set(out["provenance"]),
                         {"verified", "days", "sources_rechecked", "source_recheck"})
        self.assertEqual(out["provenance"]["source_recheck"], "performed")


if __name__ == "__main__":
    unittest.main()


# ============ review round 2: var_valid transitions, and the artifact choosing its own sample
class TestSourceVarValidTransitionsAreCaught(_Base):
    """A variable that was **absent** at build time and has since been **backfilled to
    present** changes nothing about the sampled cells of the variables that were already
    there — so a value-only comparison cannot see it. It is also exactly the gap↔present
    transition a corrective refold exists for, which made it the one case the check most
    needed to catch and structurally could not."""

    def setUp(self):
        super().setUp()
        self._manifest_gen0()

    def _delta_missing_sea_ice(self):
        path = os.path.join(self.tmp, "delta_partial.zarr")
        fx.build_delta(path, self.days, absent_vars=["sea_ice"])
        return path

    def test_a_delta_variable_backfilled_from_ABSENT_to_present_is_refused(self):
        delta = self._delta_missing_sea_ice()
        plan = _build(os.path.join(self.root, "b_v1.zarr"), days=self.days,
                      delta_path=delta, artifacts_dir=self.artifacts,
                      start=self.s0, end=self.e0)
        art = sp.load_artifact(self._artifact_path(), expected_vars=fx.VARS)
        self.assertFalse(art["days"][self.days[0]]["var_valid"].get("sea_ice", False),
                         "precondition: the build recorded sea_ice as absent")

        # backfill: the source now HAS the variable the build never read
        full = os.path.join(self.tmp, "delta_full.zarr")
        fx.build_delta(full, self.days)
        shutil.rmtree(delta)
        os.rename(full, delta)

        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        with self.assertRaises(pub.PublishRefused) as cm:
            self._publish({"segment": plan["segment"], "out_path": plan["out_path"],
                           "source_map": plan["source_map"]}, delta_path=delta)
        msg = str(cm.exception)
        self.assertIn("no longer has the same variables", msg)
        self.assertIn("sea_ice", msg)
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_a_delta_variable_that_DISAPPEARED_is_also_refused(self):
        """The other direction: present at build, absent now."""
        plan = self._fold()
        stripped = os.path.join(self.tmp, "delta_stripped.zarr")
        fx.build_delta(stripped, self.days, absent_vars=["sea_ice"])
        shutil.rmtree(self.delta)
        os.rename(stripped, self.delta)
        with self.assertRaises(pub.PublishRefused) as cm:
            self._publish({"segment": plan["segment"], "out_path": plan["out_path"],
                           "source_map": plan["source_map"]})
        self.assertIn("no longer has the same variables", str(cm.exception))

    def test_a_DAILY_source_variable_backfilled_is_refused(self):
        """Same hazard on the other source kind, which resolves `var_valid` by a different
        code path (`inspect_daily` vs the cube inspection)."""
        daily = os.path.join(self.tmp, "mur.zarr")
        fx.build_daily(daily, self.days,
                       absent_vars_on={d: ["sea_ice"] for d in self.days})
        plan = build_block(os.path.join(self.root, "b_v1.zarr"), start_day=self.s0,
                           end_day=self.e0, classification_target=list(self.days),
                           daily_root=daily, artifacts_dir=self.artifacts,
                           lock=None, hard_reserve_bytes=0, unsafe_skip_isolation=True)
        art = sp.load_artifact(self._artifact_path(), expected_vars=fx.VARS)
        self.assertFalse(art["days"][self.days[0]]["var_valid"].get("sea_ice", False))

        shutil.rmtree(daily)
        fx.build_daily(daily, self.days)                       # backfilled
        with self.assertRaises(pub.PublishRefused) as cm:
            self._publish({"segment": plan["segment"], "out_path": plan["out_path"],
                           "source_map": plan["source_map"]}, delta_path=None)
        self.assertIn("no longer has the same variables", str(cm.exception))

    def test_an_unchanged_partial_source_still_publishes(self):
        """The guard must not reject a legitimately partial source that has not moved."""
        delta = self._delta_missing_sea_ice()
        plan = _build(os.path.join(self.root, "b_v1.zarr"), days=self.days,
                      delta_path=delta, artifacts_dir=self.artifacts,
                      start=self.s0, end=self.e0)
        out = self._publish({"segment": plan["segment"], "out_path": plan["out_path"],
                             "source_map": plan["source_map"]}, delta_path=delta)
        self.assertEqual(out["status"], "published")
        self.assertEqual(out["provenance"]["sources_rechecked"], len(self.days))


class TestTheArtifactCannotChooseItsOwnSample(_Base):
    """A verifier that takes its strictness from the document it is verifying has none."""

    def setUp(self):
        super().setUp()
        self._manifest_gen0()
        self.plan = self._fold()
        self._pristine = self._read(self._artifact_path()).decode()

    def _reseal(self, mutate):
        """Always mutate a PRISTINE copy. Reading back the file each time would accumulate the
        previous iteration's mutation and make a subTest pass or fail for the wrong reason."""
        path = self._artifact_path()
        doc = json.loads(self._pristine)
        mutate(doc)
        doc["artifact_checksum"] = sp.artifact_checksum(doc)   # a forger reseals
        with open(path, "w") as fh:
            json.dump(doc, fh)
        return path

    def _seg_plan(self):
        return {"segment": self.plan["segment"], "out_path": self.plan["out_path"],
                "source_map": self.plan["source_map"]}

    def test_a_shrunken_grid_is_refused(self):
        """The attack the review named: declare a tiny grid, get a tiny sample."""
        self._reseal(lambda d: d["grid"].update({"ny": 4, "nx": 4}))
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        with self.assertRaises(pub.PublishRefused) as cm:
            self._publish(self._seg_plan())
        self.assertIn("grid", str(cm.exception))
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_a_different_seed_is_refused(self):
        """Choosing the seed chooses which cells are compared -- so it is policy, not payload."""
        path = self._reseal(lambda d: d["sample"].update({"seed": 999}))
        with self.assertRaises(sp.ProvenanceError) as cm:
            sp.load_artifact(path, expected_vars=fx.VARS)
        self.assertIn("policy", str(cm.exception))

    def test_weakened_sample_parameters_are_refused(self):
        for field, bad in (("points", 1), ("window", 2), ("algo", "md5")):
            with self.subTest(field=field):
                path = self._reseal(lambda d, f=field, b=bad: d["sample"].update({f: b}))
                with self.assertRaises(sp.ProvenanceError):
                    sp.load_artifact(path, expected_vars=fx.VARS)

    def test_non_integer_grid_or_sample_values_are_refused(self):
        for mutate in (lambda d: d["grid"].update({"ny": "32"}),
                       lambda d: d["grid"].update({"nx": 32.0}),
                       lambda d: d["sample"].update({"points": True}),
                       lambda d: d["grid"].update({"ny": 0})):
            path = self._reseal(mutate)
            with self.assertRaises(sp.ProvenanceError):
                sp.load_artifact(path, expected_vars=fx.VARS)

    def test_a_malformed_grid_or_sample_object_is_refused(self):
        for mutate in (lambda d: d.update({"grid": [32, 32]}),
                       lambda d: d.update({"sample": "sha256"}),
                       lambda d: d["grid"].pop("nx"),
                       lambda d: d["sample"].update({"surprise": 1})):
            path = self._reseal(mutate)
            with self.assertRaises(sp.ProvenanceError):
                sp.load_artifact(path, expected_vars=fx.VARS)

    def test_a_non_bool_var_valid_flag_is_refused(self):
        """`var_valid` decides whether a variable is read from the source at all, so
        truthiness would let `1`, `"false"` and `[]` each mean something."""
        for bad in (1, "true", [], None):
            path = self._reseal(
                lambda d, b=bad: d["days"][self.days[0]]["var_valid"].update({"sst": b}))
            with self.assertRaises(sp.ProvenanceError) as cm:
                sp.load_artifact(path, expected_vars=fx.VARS)
            self.assertIn("raw bool", str(cm.exception))

    def test_an_empty_var_valid_is_refused(self):
        path = self._reseal(lambda d: d["days"][self.days[0]].update({"var_valid": {}}))
        with self.assertRaises(sp.ProvenanceError):
            sp.load_artifact(path, expected_vars=fx.VARS)

    def test_a_block_whose_geometry_diverges_from_the_fill_yields_no_artifact(self):
        """The fill samples against the probe's geometry; the artifact is written from the
        block's inspection. If they diverged, every fingerprint would describe cells a verifier
        does not read — so this drives them apart and pins what actually happens.

        The write-side store-contract guard refuses first, before a plan or an artifact exists.
        I had added a second explicit check here and removed it: it could never fire, and
        unreachable code that reads as a guard is worse than no guard."""
        import ingest.build_block as bbmod
        real_create = bbmod._create

        def smaller(out_path, days, keep_vars, ny, nx, lon, lat):
            return real_create(out_path, days, keep_vars, ny // 2, nx // 2,
                               lon[: nx // 2], lat[: ny // 2])

        bbmod._create = smaller
        try:
            with self.assertRaises(Exception) as cm:
                _build(os.path.join(self.tmp, "diverged.zarr"), days=self.days,
                       delta_path=self.delta, artifacts_dir=self.artifacts,
                       start=self.s0, end=self.e0)
        finally:
            bbmod._create = real_create
        self.assertIn("does not satisfy the store contract", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "diverged.zarr",
                                                     sp.ARTIFACT_NAME)))

    def test_the_builder_records_the_policy_seed_and_the_blocks_real_grid(self):
        doc = sp.load_artifact(self._artifact_path(), expected_vars=fx.VARS)
        insp = bm.inspect_store_contract(self.plan["out_path"])
        self.assertEqual(doc["sample"]["seed"], sp.SAMPLE_SEED)
        self.assertEqual((doc["grid"]["ny"], doc["grid"]["nx"]), (insp.ny, insp.nx))


# ================= review round 3: bind the index to the DATE, and close the schema holes
class TestDayIndexIsBoundToTheDate(_Base):
    """An index inside the array bounds still selects a slot. If that slot's sampled cells
    happen to agree -- an all-NaN region, a repeated value, a short block -- the fingerprint
    matches and the artifact has attested day D against another day's bytes."""

    def setUp(self):
        super().setUp()
        self._manifest_gen0()
        self.plan = self._fold()
        self._pristine = self._read(self._artifact_path()).decode()

    def _seg_plan(self):
        return {"segment": self.plan["segment"], "out_path": self.plan["out_path"],
                "source_map": self.plan["source_map"]}

    def _repoint(self, day, new_index):
        doc = json.loads(self._pristine)
        rec = doc["days"][day]
        rec["day_index"] = new_index
        # re-fingerprint against the SLOT the artifact now points at, so the byte check would
        # pass and only the date binding can refuse
        insp = bm.inspect_store_contract(self.plan["out_path"])
        g = zarr.open_group(self.plan["out_path"], mode="r")
        i0, i1, j0, j1 = sp.sample_window(insp.ny, insp.nx, seed=sp.SAMPLE_SEED,
                                          day_index=new_index)
        valid_map = {v: list(f) for v, f in insp.var_valid}
        tiles, valid = {}, {}
        for var in fx.VARS:
            flags = valid_map.get(var, [])
            present = bool(flags) and new_index < len(flags) and flags[new_index] is True
            valid[var] = present
            tiles[var] = (np.asarray(g[var][new_index, i0:i1, j0:j1]) if present else None)
        rec["source_fingerprint"] = sp.window_fingerprint(
            tiles, seed=sp.SAMPLE_SEED, day_index=new_index, var_valid=valid)
        rec["var_valid"] = valid
        doc["artifact_checksum"] = sp.artifact_checksum(doc)
        with open(self._artifact_path(), "w") as fh:
            json.dump(doc, fh)

    def test_a_day_pointed_at_another_slot_is_refused(self):
        """The artifact is internally perfect: the fingerprint matches the slot it names. Only
        the date binding can tell that the slot is the wrong day."""
        self._repoint(self.days[0], 5)
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        with self.assertRaises(pub.PublishRefused) as cm:
            self._publish(self._seg_plan())
        msg = str(cm.exception)
        self.assertIn("holds", msg)
        self.assertIn(self.days[5], msg)
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_an_out_of_range_day_index_is_refused(self):
        doc = json.loads(self._pristine)
        doc["days"][self.days[0]]["day_index"] = 999
        doc["artifact_checksum"] = sp.artifact_checksum(doc)
        with open(self._artifact_path(), "w") as fh:
            json.dump(doc, fh)
        with self.assertRaises(pub.PublishRefused) as cm:
            self._publish(self._seg_plan())
        self.assertIn("out of range", str(cm.exception))

    def test_the_unmodified_artifact_still_publishes(self):
        """The binding must not reject correct indices."""
        self.assertEqual(self._publish(self._seg_plan())["status"], "published")


class TestArtifactSchemaHoles(_Base):
    def setUp(self):
        super().setUp()
        self._manifest_gen0()
        self.plan = self._fold()
        self._pristine = self._read(self._artifact_path()).decode()

    def _reseal(self, mutate):
        doc = json.loads(self._pristine)
        mutate(doc)
        doc["artifact_checksum"] = sp.artifact_checksum(doc)
        path = self._artifact_path()
        with open(path, "w") as fh:
            json.dump(doc, fh)
        return path

    def _seg_plan(self):
        return {"segment": self.plan["segment"], "out_path": self.plan["out_path"],
                "source_map": self.plan["source_map"]}

    def test_var_valid_must_cover_exactly_the_canonical_variables(self):
        """A missing key was read as False and an unknown key ignored, so a partial map bought
        a partial check."""
        path = self._reseal(lambda d: d["days"][self.days[0]]["var_valid"].pop("sea_ice"))
        with self.assertRaises(sp.ProvenanceError) as cm:
            sp.load_artifact(path, expected_vars=fx.VARS)
        self.assertIn("missing=['sea_ice']", str(cm.exception))

        path = self._reseal(
            lambda d: d["days"][self.days[0]]["var_valid"].update({"surprise": True}))
        with self.assertRaises(sp.ProvenanceError) as cm:
            sp.load_artifact(path, expected_vars=fx.VARS)
        self.assertIn("unexpected=['surprise']", str(cm.exception))

    def test_the_expected_variable_set_is_a_required_argument(self):
        """It cannot be inferred from the document, so the caller has to state it -- and being
        forced to state it is what stops the check being skipped."""
        with self.assertRaises(TypeError):
            sp.load_artifact(self._artifact_path())

    def test_a_renamed_block_path_is_refused(self):
        """The field is part of the audit record, so an unchecked one is a wrong audit record
        rather than a harmless label."""
        self._reseal(lambda d: d.update({"block_path": "some_other_block.zarr"}))
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        with self.assertRaises(pub.PublishRefused) as cm:
            self._publish(self._seg_plan())
        self.assertIn("names block", str(cm.exception))
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_a_coerced_version_is_refused(self):
        for bad in ("1", True, 1.0):
            with self.subTest(version=bad):
                path = self._reseal(lambda d, b=bad: d.update({"version": b}))
                with self.assertRaises(sp.ProvenanceError) as cm:
                    sp.load_artifact(path, expected_vars=fx.VARS)
                self.assertIn("must be an int", str(cm.exception))

    def test_a_coerced_source_day_index_is_refused(self):
        for bad in ("0", True, 2.0, -1):
            with self.subTest(idx=bad):
                path = self._reseal(
                    lambda d, b=bad: d["days"][self.days[0]].update({"source_day_index": b}))
                with self.assertRaises(sp.ProvenanceError):
                    sp.load_artifact(path, expected_vars=fx.VARS)

    def test_build_artifact_cannot_mint_an_artifact_with_a_foreign_seed(self):
        """The seed parameter is gone. An API that can produce only-invalid output is a trap:
        the failure would surface at publication, far from the call that caused it."""
        import inspect
        params = inspect.signature(sp.build_artifact).parameters
        self.assertNotIn("seed", params)
        doc = sp.build_artifact(segment_id="x", block_path="/tmp/x.zarr", ny=32, nx=32,
                                days={"2026-06-27": {
                                    "source_kind": "delta", "source_path": "/tmp/d.zarr",
                                    "source_day_index": 0, "day_index": 0,
                                    "source_fingerprint": "f",
                                    "var_valid": {v: True for v in fx.VARS}}})
        self.assertEqual(doc["sample"]["seed"], sp.SAMPLE_SEED)


# ================= review round 4: verification must hold AT the commit, not before the locks
class TestProvenanceIsVerifiedInsideTheCriticalSection(_Base):
    """Verification used to run before either lock was taken, so it verified a state that could
    then change. Between that check and the commit, the staging block or a source could be
    modified, and the only thing left in the way was `staleness_guard` — which compares
    LOCATORS and cannot see bytes."""

    def setUp(self):
        super().setUp()
        self._manifest_gen0()
        self.plan = self._fold()

    def _seg_plan(self):
        return {"segment": self.plan["segment"], "out_path": self.plan["out_path"],
                "source_map": self.plan["source_map"]}

    def _corrupt(self, path, t_idx, delta_amount=9.0):
        g = zarr.open_group(path, mode="a")
        insp = bm.inspect_store_contract(path) if path != self.delta else None
        ny = nx = 32
        i0, i1, j0, j1 = sp.sample_window(ny, nx, seed=sp.SAMPLE_SEED, day_index=t_idx)
        g["sst"][t_idx, i0:i1, j0:j1] = (
            np.asarray(g["sst"][t_idx, i0:i1, j0:j1]) + delta_amount)

    def _publish_with_interleaved(self, mutate):
        """Run `mutate` after the locks are held and after the staleness guard, but before the
        commit — by hooking `bind_segment_to_block`, which sits immediately before the byte
        verification inside the critical section. Deterministic: no threads, no sleeps."""
        real = pub.bind_segment_to_block
        fired = {"n": 0}

        def hooked(*a, **kw):
            out = real(*a, **kw)
            if fired["n"] == 0:
                fired["n"] = 1
                mutate()
            return out

        pub.bind_segment_to_block = hooked
        try:
            return self._publish(self._seg_plan()), fired
        finally:
            pub.bind_segment_to_block = real

    def test_a_BLOCK_corrupted_after_the_locks_are_held_is_still_refused(self):
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        with self.assertRaises(pub.PublishRefused) as cm:
            self._publish_with_interleaved(
                lambda: self._corrupt(self.plan["out_path"], 2))
        self.assertIn("do not match the fingerprint", str(cm.exception))
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before,
                         "nothing may commit once the bytes have moved")

    def test_a_SOURCE_corrupted_after_the_locks_are_held_is_still_refused(self):
        """The staleness guard has already passed by this point — every locator still agrees,
        and only the byte check can see the change."""
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        with self.assertRaises(pub.PublishRefused) as cm:
            self._publish_with_interleaved(lambda: self._corrupt(self.delta, 4))
        self.assertIn("no longer holds the bytes", str(cm.exception))
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_the_hook_really_fires_inside_the_critical_section(self):
        """Precondition for the two tests above: if the hook never ran, they would pass for
        the wrong reason."""
        out, fired = self._publish_with_interleaved(lambda: None)
        self.assertEqual(fired["n"], 1)
        self.assertEqual(out["status"], "published")

    def test_there_is_no_pre_lock_verification_left_to_shadow_it(self):
        """A second, earlier copy would be a cheap fail-fast that no mutation could kill while
        the in-lock one exists — a guard in appearance only. Proven behaviourally: with the
        artifact deleted after planning but before publication, the refusal must come from
        inside the critical section, which means the locks were taken first."""
        seen = {"locked": False}
        real = pub.bind_segment_to_block

        def hooked(*a, **kw):
            seen["locked"] = True
            return real(*a, **kw)

        pub.bind_segment_to_block = hooked
        try:
            os.remove(self._artifact_path())
            with self.assertRaises(pub.PublishRefused) as cm:
                self._publish(self._seg_plan())
        finally:
            pub.bind_segment_to_block = real
        self.assertIn("no provenance artifact", str(cm.exception))
        self.assertTrue(seen["locked"],
                        "the artifact was checked before the locks were taken")


class TestTheSourceRecheckWaiverIsLabelled(_Base):
    """`recheck_sources=False` disabled the `source changed -> refuse` guarantee while the
    result still said `verified: True`. A bypass that reports success is worse than no bypass."""

    def setUp(self):
        super().setUp()
        self._manifest_gen0()
        self.plan = self._fold()

    def _seg_plan(self):
        return {"segment": self.plan["segment"], "out_path": self.plan["out_path"],
                "source_map": self.plan["source_map"]}

    def test_the_waiver_is_named_unsafe(self):
        import inspect
        params = inspect.signature(pub.execute_publication).parameters
        self.assertIn("unsafe_skip_source_recheck", params)
        self.assertNotIn("recheck_sources", params)

    def test_a_waived_publication_does_not_report_verified(self):
        g = zarr.open_group(self.delta, mode="a")
        i0, i1, j0, j1 = sp.sample_window(32, 32, seed=sp.SAMPLE_SEED, day_index=0)
        g["sst"][0, i0:i1, j0:j1] = np.asarray(g["sst"][0, i0:i1, j0:j1]) + 8.0
        out = self._publish(self._seg_plan(), unsafe_skip_source_recheck=True)
        self.assertEqual(out["status"], "published")
        self.assertFalse(out["provenance"]["verified"])
        self.assertEqual(out["provenance"]["source_recheck"], "waived")
        self.assertEqual(out["provenance"]["sources_rechecked"], 0)
        self.assertIn("does NOT hold", out["provenance"]["reason"])

    def test_the_same_change_WITHOUT_the_waiver_refuses(self):
        """The waiver is what makes the difference, not the fixture."""
        g = zarr.open_group(self.delta, mode="a")
        i0, i1, j0, j1 = sp.sample_window(32, 32, seed=sp.SAMPLE_SEED, day_index=0)
        g["sst"][0, i0:i1, j0:j1] = np.asarray(g["sst"][0, i0:i1, j0:j1]) + 8.0
        with self.assertRaises(pub.PublishRefused):
            self._publish(self._seg_plan())

    #: every outcome reports exactly these keys; a waived one adds `reason` and drops nothing
    SHAPE = {"verified", "days", "sources_rechecked", "source_recheck"}

    def _assert_waived(self, report, *, expect_days):
        """A consumer that has to branch on the waiver type to know which fields exist is
        reading the report to find out how to read the report. The counts are 0 when nothing
        was checked -- the honest value, not an absent one."""
        self.assertEqual(set(report), self.SHAPE | {"reason"})
        self.assertFalse(report["verified"])
        self.assertEqual(report["source_recheck"], "waived")
        self.assertEqual(report["days"], expect_days)
        self.assertEqual(report["sources_rechecked"], 0)
        self.assertTrue(report["reason"])

    def test_a_full_verification_reports_the_canonical_shape(self):
        report = self._publish(self._seg_plan())["provenance"]
        self.assertEqual(set(report), self.SHAPE, "a verified outcome carries no `reason`")
        self.assertTrue(report["verified"])
        self.assertEqual(report["days"], len(self.days))
        self.assertEqual(report["sources_rechecked"], len(self.days))

    def test_a_waived_source_recheck_keeps_the_shape(self):
        report = self._publish(self._seg_plan(),
                               unsafe_skip_source_recheck=True)["provenance"]
        self._assert_waived(report, expect_days=len(self.days))
        self.assertIn("source changed", report["reason"])

    def test_skipping_provenance_entirely_keeps_the_shape_too(self):
        """`days` and `sources_rechecked` were missing here, so an audit consumer had to
        branch on which waiver was used before it could read the report."""
        report = self._publish(self._seg_plan(), build_artifact_path=None,
                               unsafe_skip_provenance=True)["provenance"]
        self._assert_waived(report, expect_days=0)
        self.assertIn("no provenance artifact was verified", report["reason"])

