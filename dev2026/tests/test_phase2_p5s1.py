"""dev2026 — P5-S1: manifest schema + SegmentedCubeStore prototype (tests written FIRST).

Gate (design spec §14 P5-S1): **every malformed or stale manifest is rejected WITHOUT ever
exposing a partial snapshot**, plus **G14** (fixed calendar boundaries + three-way day
classification). Stop condition: a fail-closed path is found that can serve a partial view.

The tests are organised around the properties the spec makes normative:

  §4.0  the manifest describes the BASE only -- any delta field is a schema violation, and
        `SegmentedCubeStore` must never open the delta path
  §5.1  ordering, precedence, duplicate-day illegality, exact day_count, stale detection
  §5.2  fixed calendar block boundaries; a gap never shifts a boundary
  §5.3  present / confirmed_missing / unknown, and their invariants
  §5.4  seal condition; `sealed: true` with a non-empty `unknown` is invalid
  §5.6  `superseded[]` is cumulative
  §6.2  reads are GROUPED by segment -- never one open or one call per day
  §9.3  rollback copies the archive; the generation archive stays byte-unchanged

Local/synthetic only: no VM24 path, no GHRSST_* store, nothing outside a temp dir.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import p5_fixtures as fx  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store.segmented_cube import SegmentedCubeStore, SnapshotError  # noqa: E402

ANCHOR = "2026-06-27"
S = 90


def _seg(seg_id, path, start, end, day_count, *, precedence, gaps=(), unknown=(),
         sealed=True, kind="block", boundary_kind="calendar", vars_=fx.VARS,
         supersedes=None, day_digest=None, time_chunk=None, store_path=None):
    """Build a segment entry. When `store_path` is given, `layout` / `day_digest` /
    `fingerprint.metadata` are DERIVED FROM DISK -- the same way S3's builder will produce
    them -- so a test that then mutates the store or the manifest exercises a real mismatch."""
    seg = {
        "segment_id": seg_id, "kind": kind, "path": path, "immutable": True,
        "boundary_kind": boundary_kind,
        "start_day": start, "end_day": end,
        "materialized_through": end if not unknown else sorted(set(fx.calendar_span(start, end)) - set(unknown))[-1],
        "day_count": day_count, "gaps": list(gaps), "unknown": list(unknown), "day_list": None,
        "layout": {"time_chunk": time_chunk or min(90, day_count or 1),
                   "spatial_chunk": 8, "shard": [90, 128, 128]},
        "variables": list(vars_),
        "fingerprint": {"algo": "sha256", "metadata": "x" * 64, "day_digest": day_digest or ""},
        "precedence": precedence, "sealed": sealed, "supersedes": supersedes,
    }
    if store_path:
        seg["layout"] = bm.segment_layout(store_path)
        seg["fingerprint"] = {"algo": "sha256",
                              "metadata": bm.metadata_fingerprint(store_path),
                              "day_digest": bm.day_digest(fx.read_days(store_path))}
    return seg


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = fx.make_root(self.tmp)

    # ---- helpers -----------------------------------------------------------------
    def _block(self, name, start, end, **kw):
        days = kw.pop("days", None) or fx.calendar_span(start, end)
        path = os.path.join(self.root, name)
        fx.build_block(path, days, **kw)
        return path, days

    def _manifest(self, segments, generation=1, superseded=(), extra=None):
        m = {
            "format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION,
            "generation": generation,
            "generation_id": f"gen{generation:06d}-20270101T000000Z",
            "created_utc": "2027-01-01T00:00:00Z", "created_by": "p5s1-tests",
            "predecessor_generation": generation - 1 if generation > 1 else None,
            "predecessor_manifest": (f"manifest.gen{generation - 1:06d}.json"
                                     if generation > 1 else None),
            "grid": {"ny": 32, "nx": 32, "region": [0, 32, 0, 32]},
            "variables": list(fx.VARS),
            "block_grid": {"anchor_day": ANCHOR, "block_days": S},
            "segments": list(segments),
            "superseded": list(superseded),
            "manifest_checksum": "",
        }
        if extra:
            m.update(extra)
        m["manifest_checksum"] = bm.compute_checksum(m)
        return m

    def _publish(self, m):
        return bm.publish(self.root, m)


# ============================================================ calendar arithmetic (§5.2, G14)
class TestCalendarBlockGrid(_Base):
    def test_block_bounds_are_pure_arithmetic(self):
        self.assertEqual(bm.block_bounds(ANCHOR, 90, 0), ("2026-06-27", "2026-09-24"))
        self.assertEqual(bm.block_bounds(ANCHOR, 90, 1), ("2026-09-25", "2026-12-23"))
        self.assertEqual(bm.block_bounds(ANCHOR, 90, 2), ("2026-12-24", "2027-03-23"))
        for k in range(4):
            s, e = bm.block_bounds(ANCHOR, 90, k)
            self.assertEqual(len(fx.calendar_span(s, e)), 90)

    def test_a_gap_never_shifts_a_boundary(self):
        """G14: a missing day changes day_count, never the block's start/end."""
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        span = fx.calendar_span(s0, e0)
        missing = span[40]
        days = [d for d in span if d != missing]
        path, _ = self._block("b0", s0, e0, days=days)
        seg = _seg("b0", "b0", s0, e0, 89, gaps=[missing], precedence=1,
                   day_digest=bm.day_digest(days))
        m = self._manifest([seg])
        bm.validate_manifest(m)                       # 89 + 1 gap + 0 unknown == 90
        # the NEXT block still starts the day after e0
        self.assertEqual(bm.block_bounds(ANCHOR, 90, 1)[0],
                         fx.calendar_span(e0, e0)[0][:0] or bm.next_day(e0))

    def test_declared_bounds_must_match_the_grid(self):
        seg = _seg("bad", "bad", "2026-06-28", "2026-09-25", 90, precedence=1)
        with self.assertRaises(bm.ManifestError) as cm:
            bm.validate_manifest(self._manifest([seg]))
        self.assertIn("block grid", str(cm.exception).lower())


# ============================================================ three-way classification (§5.3/§5.4)
class TestDayClassification(_Base):
    def test_unsealed_block_with_unknown_days_is_valid(self):
        """The round-3 fix: a fixed 90-day window materialized in 30-day passes MUST be
        representable. 30 present + 0 missing + 60 unknown == 90."""
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        span = fx.calendar_span(s0, e0)
        seg = _seg("v1", "v1", s0, e0, 30, unknown=span[30:], sealed=False, precedence=1)
        bm.validate_manifest(self._manifest([seg]))
        self.assertEqual(seg["day_count"] + len(seg["gaps"]) + len(seg["unknown"]), S)

    def test_sealed_with_unknown_is_a_schema_violation(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        span = fx.calendar_span(s0, e0)
        seg = _seg("bad", "bad", s0, e0, 30, unknown=span[30:], sealed=True, precedence=1)
        with self.assertRaises(bm.ManifestError) as cm:
            bm.validate_manifest(self._manifest([seg]))
        self.assertIn("unknown", str(cm.exception).lower())

    def test_classification_must_partition_the_window(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        seg = _seg("bad", "bad", s0, e0, 30, sealed=False, unknown=[], precedence=1)
        with self.assertRaises(bm.ManifestError):
            bm.validate_manifest(self._manifest([seg]))   # 30 + 0 + 0 != 90

    def test_overlapping_classes_are_rejected(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        span = fx.calendar_span(s0, e0)
        seg = _seg("bad", "bad", s0, e0, 89, gaps=[span[5]], unknown=[span[5]],
                   sealed=False, precedence=1)
        with self.assertRaises(bm.ManifestError):
            bm.validate_manifest(self._manifest([seg]))


# ============================================================ schema guards (§4.0, §5.1)
class TestSchemaGuards(_Base):
    def _one_block(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        path, days = self._block("b0", s0, e0)
        return _seg("b0", "b0", s0, e0, 90, precedence=1, store_path=path)

    def test_round_trip(self):
        m = self._manifest([self._one_block()])
        bm.validate_manifest(m)
        p = self._publish(m)
        again = bm.load_live(self.root)
        self.assertEqual(again["generation"], m["generation"])
        self.assertEqual(again["manifest_checksum"], m["manifest_checksum"])
        self.assertTrue(os.path.isfile(p))

    def test_checksum_tamper_is_rejected(self):
        m = self._manifest([self._one_block()])
        m["created_by"] = "someone-else"               # checksum no longer matches
        with self.assertRaises(bm.ManifestError) as cm:
            bm.validate_manifest(m)
        self.assertIn("checksum", str(cm.exception).lower())

    def test_missing_required_field_is_rejected(self):
        for field in ("format", "version", "generation", "segments", "block_grid", "grid"):
            with self.subTest(field=field):
                m = self._manifest([self._one_block()])
                del m[field]
                with self.assertRaises(bm.ManifestError):
                    bm.validate_manifest(m)

    def test_unknown_top_level_field_is_rejected(self):
        m = self._manifest([self._one_block()], extra={"surprise": 1})
        with self.assertRaises(bm.ManifestError) as cm:
            bm.validate_manifest(m)
        self.assertIn("surprise", str(cm.exception))

    def test_any_delta_field_is_rejected(self):
        """§4.0: the manifest describes the base ONLY. Two authorities over the same days is
        the ambiguity this guard exists to prevent."""
        for extra in ({"delta": {"path": "../delta.zarr"}},
                      {"delta_path": "../delta.zarr"},
                      {"delta_cutoff_day": "2026-09-24"}):
            with self.subTest(extra=list(extra)[0]):
                m = self._manifest([self._one_block()], extra=extra)
                with self.assertRaises(bm.ManifestError) as cm:
                    bm.validate_manifest(m)
                self.assertIn("delta", str(cm.exception).lower())

    def test_segments_must_be_ordered_and_non_duplicating(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        s1, e1 = bm.block_bounds(ANCHOR, 90, 1)
        a = _seg("b0", "b0", s0, e0, 90, precedence=1)
        b = _seg("b1", "b1", s1, e1, 90, precedence=2)
        bm.validate_manifest(self._manifest([a, b]))
        with self.assertRaises(bm.ManifestError):
            bm.validate_manifest(self._manifest([b, a]))          # descending
        with self.assertRaises(bm.ManifestError):
            bm.validate_manifest(self._manifest([a, dict(a, segment_id="dup")]))


# ============================================================ superseded lifecycle (§5.6)
class TestSupersededLifecycle(_Base):
    def test_cumulative_entries_survive_generations(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        sup = [{"segment_id": "b0_v1", "path": "b0_v1", "superseded_at_generation": 2,
                "release_after_utc": "2027-02-01T00:00:00Z",
                "hold_until_utc": "2027-02-14T00:00:00Z",
                "status": "held", "current_path": "../hold/b0_v1", "bytes": 123,
                "fingerprint": {"algo": "sha256", "day_digest": "d"}}]
        seg = _seg("b0_v2", "b0_v2", s0, e0, 90, precedence=1, supersedes="b0_v1")
        m = self._manifest([seg], generation=4, superseded=sup)
        bm.validate_manifest(m)
        self.assertEqual(m["superseded"][0]["superseded_at_generation"], 2)
        self.assertLess(m["superseded"][0]["superseded_at_generation"], m["generation"])

    def test_superseded_entry_missing_a_required_field_is_rejected(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        sup = [{"segment_id": "b0_v1", "path": "b0_v1"}]
        seg = _seg("b0_v2", "b0_v2", s0, e0, 90, precedence=1)
        with self.assertRaises(bm.ManifestError):
            bm.validate_manifest(self._manifest([seg], generation=2, superseded=sup))


# ============================================================ snapshot build, fail-closed
class TestSnapshotFailsClosed(_Base):
    def _two_blocks(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        s1, e1 = bm.block_bounds(ANCHOR, 90, 1)
        p0, d0 = self._block("b0", s0, e0)
        p1, d1 = self._block("b1", s1, e1, seed=50)
        return [_seg("b0", "b0", s0, e0, 90, precedence=1, store_path=p0),
                _seg("b1", "b1", s1, e1, 90, precedence=2, store_path=p1)], d0 + d1

    def test_happy_path_snapshot(self):
        segs, all_days = self._two_blocks()
        self._publish(self._manifest(segs))
        store = SegmentedCubeStore(self.root)
        self.assertEqual(store.days, sorted(all_days))
        self.assertEqual(store.day_count, 180)
        self.assertEqual(store.latest, max(all_days))
        self.assertTrue(store.covers_days([all_days[0], all_days[-1]]))
        self.assertEqual(store.generation, 1)

    def test_stale_manifest_day_count_mismatch_fails_closed(self):
        """F11: the store on disk disagrees with the manifest -> refuse, keep the previous
        snapshot, never serve a partial view."""
        segs, _ = self._two_blocks()
        self._publish(self._manifest(segs))
        store = SegmentedCubeStore(self.root)
        good_days = list(store.days)

        fx.corrupt_days(os.path.join(self.root, "b1"), ["2026-09-25"])   # now disagrees
        with self.assertRaises(SnapshotError):
            store.refresh()
        self.assertEqual(store.days, good_days, "the previous snapshot must be retained")
        self.assertEqual(store.generation, 1)

    def test_stale_manifest_same_count_different_days_fails_closed(self):
        """The case a count check alone cannot catch: the store holds the RIGHT NUMBER of
        days but the WRONG days. Only the day-set comparison detects it, so this test is
        what keeps that comparison honest."""
        segs, _ = self._two_blocks()
        self._publish(self._manifest(segs))
        store = SegmentedCubeStore(self.root)
        good_days = list(store.days)

        b1 = os.path.join(self.root, "b1")
        shifted = [bm.add_days(d, 365) for d in fx.read_days(b1)]        # same count
        self.assertEqual(len(shifted), 90)
        fx.corrupt_days(b1, shifted)
        with self.assertRaises(SnapshotError) as cm:
            store.refresh()
        self.assertIn("day sets differ", str(cm.exception).lower())
        self.assertEqual(store.days, good_days)

    def test_wrong_day_digest_fails_closed(self):
        """The day set and count both match, but the manifest's `day_digest` was computed
        from a different set (a copy-paste between segments, or a tamper). Only the digest
        comparison catches this, so this test is what keeps that comparison honest."""
        segs, _ = self._two_blocks()
        segs[1]["fingerprint"]["day_digest"] = bm.day_digest(["1999-01-01"])
        m = self._manifest(segs)
        fx.write_json(os.path.join(self.root, "manifest.json"), m)
        with self.assertRaises(SnapshotError) as cm:
            SegmentedCubeStore(self.root)
        self.assertIn("day_digest", str(cm.exception))

    def test_missing_segment_path_fails_closed(self):
        segs, _ = self._two_blocks()
        self._publish(self._manifest(segs))
        store = SegmentedCubeStore(self.root)
        good = list(store.days)
        shutil.rmtree(os.path.join(self.root, "b1"))
        with self.assertRaises(SnapshotError):
            store.refresh()
        self.assertEqual(store.days, good)

    def test_first_build_on_a_bad_manifest_raises_and_serves_nothing(self):
        segs, _ = self._two_blocks()
        m = self._manifest(segs)
        m["manifest_checksum"] = "0" * 64
        fx.write_json(os.path.join(self.root, "manifest.json"), m)
        with self.assertRaises(SnapshotError):
            SegmentedCubeStore(self.root)

    def test_duplicate_start_day_is_rejected_by_schema_validation(self):
        """§5.1: two live segments may not share a start_day -- a superseding version
        replaces its predecessor, which moves to `superseded`."""
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        _p, d0 = self._block("b0", s0, e0)
        a = _seg("b0", "b0", s0, e0, 90, precedence=1, store_path=_p)
        b = dict(a, segment_id="b0b", path="b0b", precedence=2)
        with self.assertRaises(bm.ManifestError) as cm:
            bm.validate_manifest(self._manifest([a, b]))
        self.assertIn("duplicate start_day", str(cm.exception))

    def test_overlapping_days_at_equal_precedence_fail_closed_at_snapshot(self):
        """F12 / §5.1: no 'pick one' rule. Distinct start_days pass schema validation, so
        only the SNAPSHOT build can catch equal-precedence day collisions -- which is what
        this test exists to keep honest."""
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        span = fx.calendar_span(s0, e0)
        _pl, d0 = self._block("legacy", span[0], span[-1])
        _pb, d1 = self._block("b0", span[30], span[-1], seed=77)
        a = _seg("legacy", "legacy", span[0], span[-1], 90, precedence=1,
                 kind="legacy_base", boundary_kind="legacy", store_path=_pl)
        b = _seg("b0", "b0", span[30], span[-1], 60, precedence=1,     # EQUAL precedence
                 boundary_kind="legacy", store_path=_pb)
        m = self._manifest([a, b])
        bm.validate_manifest(m)                      # schema-valid: start_days differ
        fx.write_json(os.path.join(self.root, "manifest.json"), m)
        with self.assertRaises(SnapshotError) as cm:
            SegmentedCubeStore(self.root)
        msg = str(cm.exception).lower()
        self.assertIn("duplicate day", msg)
        self.assertIn("equal precedence", msg)

    def test_distinct_precedence_on_the_same_days_is_legal(self):
        """The counterpart: overlap is LEGAL and resolves silently when precedence differs
        (it is the compaction window). Without this the test above could be satisfied by
        rejecting all overlap."""
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        span = fx.calendar_span(s0, e0)
        _pl, d0 = self._block("legacy", span[0], span[-1])
        _pb, d1 = self._block("b0", span[30], span[-1], seed=77)
        a = _seg("legacy", "legacy", span[0], span[-1], 90, precedence=0,
                 kind="legacy_base", boundary_kind="legacy", store_path=_pl)
        b = _seg("b0", "b0", span[30], span[-1], 60, precedence=1,
                 boundary_kind="legacy", store_path=_pb)
        self._publish(self._manifest([a, b]))
        store = SegmentedCubeStore(self.root)
        self.assertEqual(store.day_count, 90)
        self.assertEqual(store.segment_id(store.resolve(span[40])[0]), "b0")


# ============================================================ precedence + reads (§5.1, §6)
class TestPrecedenceAndReads(_Base):
    def setUp(self):
        super().setUp()
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        self.span0 = fx.calendar_span(s0, e0)
        # overlapping pair: the newer block (higher precedence) wins on the shared days
        self.old_path, dold = self._block("legacy", self.span0[0], self.span0[-1], seed=1)
        self.new_path, dnew = self._block("b0_v2", self.span0[30], self.span0[-1], seed=500)
        self.segs = [
            _seg("legacy", "legacy", self.span0[0], self.span0[-1], 90, precedence=0,
                 kind="legacy_base", boundary_kind="legacy", store_path=self.old_path),
            _seg("b0_v2", "b0_v2", self.span0[30], self.span0[-1], 60, precedence=5,
                 boundary_kind="legacy", store_path=self.new_path),
        ]
        self._publish(self._manifest(self.segs))
        self.store = SegmentedCubeStore(self.root)

    def test_higher_precedence_wins_and_days_are_deduped(self):
        self.assertEqual(len(self.store.days), 90)
        self.assertEqual(self.store.days, sorted(set(self.span0)))
        seg_idx, _ = self.store.resolve(self.span0[40])
        self.assertEqual(self.store.segment_id(seg_idx), "b0_v2")
        seg_idx, _ = self.store.resolve(self.span0[10])
        self.assertEqual(self.store.segment_id(seg_idx), "legacy")

    def test_point_series_returns_the_winning_value_in_requested_order(self):
        req = [self.span0[40], self.span0[10], self.span0[80]]
        rows = self.store.point_series(105.0, 5.0, req, ["sst"])
        self.assertEqual([r["date"] for r in rows], req, "requested order preserved")
        direct_new = self.store.segment_store("b0_v2").point_series(
            105.0, 5.0, [self.span0[40]], ["sst"])[0]["sst"]
        self.assertAlmostEqual(rows[0]["sst"], direct_new, places=5)

    def test_reads_are_grouped_one_call_per_segment(self):
        """§6.2: never one open or one call per day."""
        calls = {"n": 0}
        for sid in ("legacy", "b0_v2"):
            st = self.store.segment_store(sid)
            orig = st.point_series

            def counting(*a, _orig=orig, **k):
                calls["n"] += 1
                return _orig(*a, **k)
            st.point_series = counting
        rows = self.store.point_series(105.0, 5.0, self.span0, ["sst"])
        self.assertEqual(len(rows), 90)
        self.assertEqual(calls["n"], 2, "one call per segment touched, not per day")

    def test_absent_days_are_omitted_not_fabricated(self):
        rows = self.store.point_series(105.0, 5.0, ["2020-01-01", self.span0[3]], ["sst"])
        self.assertEqual([r["date"] for r in rows], [self.span0[3]])


# ============================================================ base-only guarantee (§4.0)
class TestNeverTouchesDelta(_Base):
    def test_segmented_store_never_opens_the_delta_path(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        p0, d0 = self._block("b0", s0, e0)
        delta_days = fx.days_from("2026-09-25", 31)
        delta_path = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(delta_path, delta_days)                      # F3
        self._publish(self._manifest(
            [_seg("b0", "b0", s0, e0, 90, precedence=1, store_path=p0)]))

        opened = []
        real_open = __import__("zarr").open_group

        import zarr as _z
        def spy(path, *a, **k):
            opened.append(str(path))
            return real_open(path, *a, **k)
        _z.open_group = spy
        try:
            store = SegmentedCubeStore(self.root)
            store.point_series(105.0, 5.0, d0[:5], ["sst"])
        finally:
            _z.open_group = real_open

        self.assertTrue(opened)
        self.assertFalse([p for p in opened if "delta" in p],
                         f"segmented store must never open the delta path: {opened}")
        self.assertNotIn(delta_days[0], store.days)


# ============================================================ publish / rollback (§9.3)
class TestPublishAndRollback(_Base):
    def _gen(self, n, segs):
        return self._manifest(segs, generation=n)

    def setUp(self):
        super().setUp()
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        s1, e1 = bm.block_bounds(ANCHOR, 90, 1)
        p0, self.d0 = self._block("b0", s0, e0)
        p1, self.d1 = self._block("b1", s1, e1, seed=50)
        self.a = _seg("b0", "b0", s0, e0, 90, precedence=1, store_path=p0)
        self.b = _seg("b1", "b1", s1, e1, 90, precedence=2, store_path=p1)

    def test_publish_writes_an_immutable_archive_and_an_atomic_pointer(self):
        bm.publish(self.root, self._gen(1, [self.a]))
        bm.publish(self.root, self._gen(2, [self.a, self.b]))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "manifest.gen000001.json")))
        self.assertTrue(os.path.isfile(os.path.join(self.root, "manifest.gen000002.json")))
        self.assertEqual(bm.load_live(self.root)["generation"], 2)

    def test_rollback_preserves_the_generation_archive(self):
        """§9.3: `os.replace`-ing the archive would CONSUME it. Copy, verify, fsync, replace."""
        bm.publish(self.root, self._gen(1, [self.a]))
        bm.publish(self.root, self._gen(2, [self.a, self.b]))
        arch = os.path.join(self.root, "manifest.gen000001.json")
        with open(arch, "rb") as fh:
            before = fh.read()

        bm.rollback_to(self.root, 1)
        self.assertEqual(bm.load_live(self.root)["generation"], 1)
        self.assertTrue(os.path.isfile(arch), "the archive must still exist after rollback")
        with open(arch, "rb") as fh:
            self.assertEqual(fh.read(), before, "the archive must be byte-unchanged")

        store = SegmentedCubeStore(self.root)
        self.assertEqual(store.day_count, 90)         # generation 1's view

    def test_rollback_to_a_missing_generation_refuses(self):
        bm.publish(self.root, self._gen(1, [self.a]))
        with self.assertRaises(bm.ManifestError):
            bm.rollback_to(self.root, 7)
        self.assertEqual(bm.load_live(self.root)["generation"], 1)

    def test_rollback_verifies_the_archive_before_replacing(self):
        bm.publish(self.root, self._gen(1, [self.a]))
        bm.publish(self.root, self._gen(2, [self.a, self.b]))
        arch = os.path.join(self.root, "manifest.gen000001.json")
        with open(arch) as fh:
            m = json.load(fh)
        m["manifest_checksum"] = "0" * 64
        fx.write_json(arch, m)
        with self.assertRaises(bm.ManifestError):
            bm.rollback_to(self.root, 1)
        self.assertEqual(bm.load_live(self.root)["generation"], 2,
                         "a corrupt archive must not become the live manifest")

    def test_refresh_is_a_no_op_when_the_generation_is_unchanged(self):
        bm.publish(self.root, self._gen(1, [self.a]))
        store = SegmentedCubeStore(self.root)
        meta_before = store._meta
        self.assertFalse(store.refresh_if_changed())
        self.assertIs(store._meta, meta_before, "unchanged generation must not rebuild")
        bm.publish(self.root, self._gen(2, [self.a, self.b]))
        self.assertTrue(store.refresh_if_changed())
        self.assertEqual(store.day_count, 180)


# ============================================================ integrity guards (review round 1)
class TestIntegrityGuards(_Base):
    """Three ways a malformed manifest reached a SERVING snapshot in the first cut. Each of
    these is a reproduction of a reported defect, kept as a regression test."""

    def test_day_list_cannot_escape_the_calendar_window(self):
        """[High] `day_list` was taken verbatim, so a segment could declare a 2026 window and
        actually serve 2027 days -- splitting the block grid from real availability (G14)."""
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        span = fx.calendar_span(s0, e0)
        shifted = [bm.add_days(d, 365) for d in span]          # same count, wrong year
        path = os.path.join(self.root, "b0")
        fx.build_block(path, shifted)
        seg = _seg("b0", "b0", s0, e0, 90, precedence=1, store_path=path)
        seg["day_list"] = shifted
        with self.assertRaises(bm.ManifestError) as cm:
            bm.validate_manifest(self._manifest([seg]))
        self.assertIn("day_list", str(cm.exception))

    def test_day_list_must_equal_span_minus_gaps_and_unknown(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        span = fx.calendar_span(s0, e0)
        for bad, why in (
                (span[:89], "count"),                       # len != day_count
                (span[:89] + [span[0]], "duplicate"),       # not unique
                (span[1:] + [span[0]], None),               # equal as a set -> must PASS
        ):
            seg = _seg("b0", "b0", s0, e0, 90, precedence=1)
            seg["day_list"] = bad
            if why is None:
                bm.validate_manifest(self._manifest([seg]))
            else:
                with self.assertRaises(bm.ManifestError):
                    bm.validate_manifest(self._manifest([seg]))

    def test_day_list_present_days_cannot_exceed_materialized_through(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        span = fx.calendar_span(s0, e0)
        seg = _seg("b0", "b0", s0, e0, 30, unknown=span[30:], sealed=False, precedence=1)
        seg["day_list"] = span[:29] + [span[60]]            # a present day past the frontier
        with self.assertRaises(bm.ManifestError):
            bm.validate_manifest(self._manifest([seg]))

    def test_a_segment_path_may_not_point_at_the_delta(self):
        """[High] The §4.0 guard only checked KEY NAMES, so `path: "../delta.zarr"` on a
        segment innocently named `not-delta-by-key` was served as base."""
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        days = fx.calendar_span(s0, e0)
        delta_path = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(delta_path, days)
        seg = _seg("not-delta-by-key", "../delta.zarr", s0, e0, 90, precedence=1,
                   store_path=delta_path)
        m = self._manifest([seg])
        fx.write_json(os.path.join(self.root, "manifest.json"), m)
        with self.assertRaises(SnapshotError) as cm:
            SegmentedCubeStore(self.root)
        self.assertIn("outside", str(cm.exception).lower())

    def test_block_paths_must_stay_inside_the_manifest_root(self):
        for bad in ("../elsewhere.zarr", "/tmp/absolute.zarr"):
            with self.subTest(path=bad):
                s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
                seg = _seg("b", bad, s0, e0, 90, precedence=1)
                m = self._manifest([seg])
                fx.write_json(os.path.join(self.root, "manifest.json"), m)
                with self.assertRaises(SnapshotError):
                    SegmentedCubeStore(self.root)

    def test_legacy_base_outside_the_root_requires_an_explicit_allowlist(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        days = fx.calendar_span(s0, e0)
        legacy = os.path.join(self.tmp, "legacy_monolith.zarr")
        fx.build_block(legacy, days)
        seg = _seg("legacy", "../legacy_monolith.zarr", s0, e0, 90, precedence=0,
                   kind="legacy_base", boundary_kind="legacy", store_path=legacy)
        self._publish(self._manifest([seg]))
        with self.assertRaises(SnapshotError) as cm:
            SegmentedCubeStore(self.root)                       # no allowlist -> refuse
        self.assertIn("legacy", str(cm.exception).lower())
        store = SegmentedCubeStore(self.root, allowed_legacy_paths=[legacy])
        self.assertEqual(store.day_count, 90)

    def test_grid_mismatch_fails_closed(self):
        """[High] A block built 16x16 loaded happily under a manifest declaring 32x32, so two
        segments could map the same lon/lat to different physical cells."""
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        days = fx.calendar_span(s0, e0)
        path = os.path.join(self.root, "b0")
        fx.build_block(path, days, ny=16, nx=16)
        seg = _seg("b0", "b0", s0, e0, 90, precedence=1, store_path=path)
        m = self._manifest([seg])                     # manifest grid says 32x32
        fx.write_json(os.path.join(self.root, "manifest.json"), m)
        with self.assertRaises(SnapshotError) as cm:
            SegmentedCubeStore(self.root)
        self.assertIn("grid", str(cm.exception).lower())

    def test_segments_must_share_identical_lon_lat_axes(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        s1, e1 = bm.block_bounds(ANCHOR, 90, 1)
        p0 = os.path.join(self.root, "b0")
        p1 = os.path.join(self.root, "b1")
        fx.build_block(p0, fx.calendar_span(s0, e0))
        fx.build_block(p1, fx.calendar_span(s1, e1))
        import zarr as _z                              # shift b1's longitude axis
        g = _z.open_group(p1, mode="a")
        g["lon"][:] = np.asarray(g["lon"][:]) + 7.0
        segs = [_seg("b0", "b0", s0, e0, 90, precedence=1, store_path=p0),
                _seg("b1", "b1", s1, e1, 90, precedence=2, store_path=p1)]
        fx.write_json(os.path.join(self.root, "manifest.json"), self._manifest(segs))
        with self.assertRaises(SnapshotError) as cm:
            SegmentedCubeStore(self.root)
        self.assertIn("axes", str(cm.exception).lower())

    def test_layout_mismatch_fails_closed(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        path = os.path.join(self.root, "b0")
        fx.build_block(path, fx.calendar_span(s0, e0))
        seg = _seg("b0", "b0", s0, e0, 90, precedence=1, store_path=path)
        seg["layout"] = dict(seg["layout"], spatial_chunk=64)   # not what is on disk
        fx.write_json(os.path.join(self.root, "manifest.json"), self._manifest([seg]))
        with self.assertRaises(SnapshotError) as cm:
            SegmentedCubeStore(self.root)
        self.assertIn("layout", str(cm.exception).lower())

    def test_metadata_fingerprint_is_required_and_verified(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        path = os.path.join(self.root, "b0")
        fx.build_block(path, fx.calendar_span(s0, e0))
        seg = _seg("b0", "b0", s0, e0, 90, precedence=1, store_path=path)
        seg["fingerprint"]["metadata"] = ""
        with self.assertRaises(bm.ManifestError):
            bm.validate_manifest(self._manifest([seg]))
        seg["fingerprint"]["metadata"] = "0" * 64
        fx.write_json(os.path.join(self.root, "manifest.json"), self._manifest([seg]))
        with self.assertRaises(SnapshotError) as cm:
            SegmentedCubeStore(self.root)
        self.assertIn("metadata fingerprint", str(cm.exception).lower())

    def test_var_valid_length_must_match_day_count(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        path = os.path.join(self.root, "b0")
        fx.build_block(path, fx.calendar_span(s0, e0))
        import zarr as _z
        g = _z.open_group(path, mode="a")
        vv = {k: list(v) for k, v in dict(g.attrs["var_valid"]).items()}
        vv["sst"] = vv["sst"][:-1]                     # now 89 flags for 90 days
        g.attrs["var_valid"] = vv
        seg = _seg("b0", "b0", s0, e0, 90, precedence=1, store_path=path)
        fx.write_json(os.path.join(self.root, "manifest.json"), self._manifest([seg]))
        with self.assertRaises(SnapshotError) as cm:
            SegmentedCubeStore(self.root)
        self.assertIn("var_valid", str(cm.exception).lower())

    def test_base_segments_are_asserted_disjoint_from_the_delta(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        days = fx.calendar_span(s0, e0)
        p0 = os.path.join(self.root, "b0")
        fx.build_block(p0, days)
        self._publish(self._manifest(
            [_seg("b0", "b0", s0, e0, 90, precedence=1, store_path=p0)]))
        store = SegmentedCubeStore(self.root)
        delta_path = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(delta_path, fx.days_from("2026-09-25", 31))
        store.assert_disjoint_from(delta_path)                  # different -> fine
        with self.assertRaises(SnapshotError):
            store.assert_disjoint_from(p0)                      # same realpath -> refuse


# ============================================================ TieredCube drop-in (§6.1)
class TestTieredCubeComposition(_Base):
    """`SegmentedCubeStore` must be a drop-in for the BASE inside `TieredCube`, with
    `TieredCube` continuing to own the delta and the delta-wins merge (§4.0/§6.1)."""

    def setUp(self):
        super().setUp()
        from store.tiered_cube import TieredCube
        from store.time_cube import TimeCubeStore

        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        self.base_days = fx.calendar_span(s0, e0)
        p0, d0 = self._block("b0", s0, e0)
        self._publish(self._manifest(
            [_seg("b0", "b0", s0, e0, 90, precedence=1, store_path=p0)]))

        # delta overlaps the last 5 base days and extends 26 days beyond
        self.delta_days = self.base_days[-5:] + fx.days_from(bm.next_day(e0), 26)
        dpath = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(dpath, self.delta_days)
        self.segmented = SegmentedCubeStore(self.root)
        self.cube = TieredCube(self.segmented, TimeCubeStore(dpath))

    def test_union_availability_and_latest(self):
        self.assertEqual(self.cube.day_count, len(set(self.base_days) | set(self.delta_days)))
        self.assertEqual(self.cube.latest, max(self.delta_days))
        self.assertTrue(self.cube.covers_days([self.base_days[0], self.delta_days[-1]]))

    def test_delta_wins_on_overlap_and_order_is_preserved(self):
        overlap = self.base_days[-1]
        req = [self.base_days[0], overlap, self.delta_days[-1]]
        rows = self.cube.point_series(105.0, 5.0, req, ["sst"])
        self.assertEqual([r["date"] for r in rows], req)
        # the delta fixture offsets values by +1000, so precedence is observable
        self.assertGreater(rows[1]["sst"], 500.0, "delta must win on the overlapping day")
        self.assertLess(rows[0]["sst"], 500.0, "base-only day must come from the block")

    def test_crossing_range_has_no_missing_or_duplicate_days(self):
        req = self.base_days[-10:] + self.delta_days[5:]
        rows = self.cube.point_series(105.0, 5.0, req, ["sst"])
        dates = [r["date"] for r in rows]
        self.assertEqual(dates, req)
        self.assertEqual(len(dates), len(set(dates)), "no duplicate days")


if __name__ == "__main__":
    unittest.main(verbosity=2)
