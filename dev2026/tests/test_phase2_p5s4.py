"""dev2026 — P5-S4: atomic manifest publish, superseded lifecycle, rollback, repair WAL.

Gate (design spec §14 P5-S4): a publication either commits generation `N+1` atomically or
leaves `manifest.json` **byte-unchanged**; no superseded block is ever released while something
can still read it; rollback restores before it repoints; and the repair WAL fails closed on
every integrity defect rather than skipping a line.

The WAL is the load-bearing piece. It is the **only** authorizing evidence for dropping a
repaired day, so most of these tests are about what it refuses, not what it permits.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np
import zarr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import p5_fixtures as fx  # noqa: E402
from ingest import publish_manifest as pub  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store import repair_wal as rw  # noqa: E402
from store import source_provenance as sp  # noqa: E402
from store.compaction_lock import (  # noqa: E402
    CompactionLock, CompactionLockBusy, CompactionLockError,
)

def _execute(plan, **kw):
    """Test shim: waive the compaction-lock requirement for the cases that are not about it.

    `execute_publication` requires `compaction_lock_path`. Tests below that exercise staleness,
    binding, lifecycle etc. are not about compaction isolation, so they waive it explicitly
    here -- in ONE named place, rather than each silently passing `None`.
    `TestCompactionLockIsMandatory` calls `pub.execute_publication` directly."""
    kw.setdefault("compaction_lock_path", None)
    kw.setdefault("unsafe_skip_compaction_lock", True)
    kw.setdefault("build_artifact_path", None)
    kw.setdefault("unsafe_skip_provenance", True)
    return pub.execute_publication(plan, **kw)


ANCHOR = "2026-06-27"
S = 90
T0 = datetime(2027, 1, 1, tzinfo=timezone.utc)


def _seg(sid, path, start, end, day_count, *, gaps=(), unknown=(), precedence=0,
         day_digest="d", sealed=True, supersedes=None, provenance=None, block_path=None):
    """A segment entry.

    When `block_path` is given, every store-dependent field is derived from an inspection of
    that block -- the same way `build_block` derives them, and the same way publication now
    re-derives them. Hand-written placeholders would make the binding test pass or fail for
    reasons unrelated to what it is testing."""
    known = [d for d in fx.calendar_span(start, end) if d not in set(unknown)]
    layout = {"time_chunk": day_count, "spatial_chunk": 8, "shard": [day_count, 32, 32]}
    variables, metadata = list(fx.VARS), "m"
    if block_path:
        insp = bm.inspect_store_contract(block_path)
        layout = bm.segment_layout_from_inspection(insp)
        variables = sorted(insp.vars)
        metadata = bm.metadata_fingerprint_from_inspection(insp)
        day_digest = bm.day_digest(insp.days)
        day_count = len(insp.days)
    return {
        "segment_id": sid, "kind": "block", "path": path, "immutable": True,
        "boundary_kind": "calendar", "start_day": start, "end_day": end,
        "materialized_through": max(known) if known else start, "day_count": day_count,
        "gaps": list(gaps), "unknown": list(unknown), "day_list": None,
        "layout": layout, "variables": variables,
        "fingerprint": {"algo": "sha256", "metadata": metadata, "day_digest": day_digest},
        "precedence": precedence, "sealed": sealed, "supersedes": supersedes,
        **({"build_provenance": provenance} if provenance else {}),
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "manifest_root")
        os.makedirs(self.root, exist_ok=True)
        self.hold = os.path.join(self.tmp, "hold")
        self.lock = os.path.join(self.tmp, "ingest.lock")
        self.s0, self.e0 = bm.block_bounds(ANCHOR, S, 0)
        self.span = fx.calendar_span(self.s0, self.e0)

    @staticmethod
    def _read(path, mode="rb"):
        with open(path, mode) as fh:
            return fh.read()

    @staticmethod
    def _lines(path):
        with open(path) as fh:
            return fh.read().splitlines()

    @staticmethod
    def _write(path, text):
        with open(path, "w") as fh:
            fh.write(text)

    def _artifact_for(self, block_path, segment_id, smap, *, seed=20260805, out=None):
        """A genuine artifact for a fixture block, sampled from the block itself.

        These tests build blocks with the fixture rather than through `build_block`, so the
        block's own bytes stand in for the source's. Hand-written fingerprints would make the
        byte check pass or fail for reasons unrelated to it."""
        insp = bm.inspect_store_contract(block_path)
        g = zarr.open_group(block_path, mode="r")
        valid_map = {v: list(f) for v, f in insp.var_valid}
        days = {}
        for t_idx, day in enumerate(insp.days):
            i0, i1, j0, j1 = sp.sample_window(insp.ny, insp.nx, seed=seed, day_index=t_idx)
            tiles, valid = {}, {}
            for var, flags in valid_map.items():
                present = flags[t_idx] is True
                valid[var] = present
                tiles[var] = (np.asarray(g[var][t_idx, i0:i1, j0:j1]) if present else None)
            rec = smap[day]
            days[day] = {
                "source_kind": rec["source_kind"], "source_path": rec["source_path"],
                "source_day_index": rec["source_day_index"], "day_index": t_idx,
                "source_fingerprint": sp.window_fingerprint(
                    tiles, seed=seed, day_index=t_idx, var_valid=valid),
                "var_valid": valid,
            }
        doc = sp.build_artifact(segment_id=segment_id, block_path=block_path,
                                ny=insp.ny, nx=insp.nx, seed=seed, days=days)
        path = out or os.path.join(self.tmp, sp.ARTIFACT_NAME)
        sp.write_artifact(path, doc)
        return path, doc

    # ---- WAL helpers -----------------------------------------------------
    def _open_repair(self, day, root=None, **kw):
        """Allocate a conforming repair_id. Ids are `<opaque>-<intent seq>`, so tests must not
        invent them any more than production may."""
        rec = rw.open_repair(root or self.tmp, day=day, at_utc=kw.pop("at_utc", "t"),
                             operator="o", payload=kw.pop("payload", {}))
        return rec["repair_id"]

    def _commit(self, rid, day, fp, root=None, at_utc="t", **payload):
        return rw.append(root or self.tmp, record=rw.COMMITTED, repair_id=rid, day=day,
                         at_utc=at_utc, operator="o",
                         payload={"fingerprint": fp, **payload})

    # ---- manifest helpers ------------------------------------------------
    def _manifest(self, segments, generation=1, superseded=()):
        m = {
            "format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION,
            "generation": generation,
            "generation_id": f"gen{generation:06d}-x",
            "created_utc": "2027-01-01T00:00:00Z", "created_by": "p5s4-tests",
            "predecessor_generation": generation - 1 if generation > 1 else None,
            "predecessor_manifest": (bm.archive_name(generation - 1)
                                     if generation > 1 else None),
            "grid": {"ny": 32, "nx": 32, "region": [0, 32, 0, 32]},
            "variables": list(fx.VARS),
            "block_grid": {"anchor_day": ANCHOR, "block_days": S},
            "segments": list(segments), "superseded": list(superseded),
            "manifest_checksum": "",
        }
        m["manifest_checksum"] = bm.compute_checksum(m)
        return m

    def _block(self, name, days):
        path = os.path.join(self.root, name + ".zarr")
        fx.build_block(path, days)
        return path

    def _live_gen1(self, *, with_block=True):
        days = self.span[:30]
        path = self._block("b_v1", days) if with_block else None
        seg = _seg("b_v1", "b_v1.zarr", self.s0, self.e0, 30, unknown=self.span[30:],
                   sealed=False, day_digest=bm.day_digest(days), block_path=path)
        bm.publish(self.root, self._manifest([seg]))
        return seg, days

    def _smap(self, delta_path, days, *, kind="delta", root=None):
        """The shape `build_block` emits: kind + RESOLVED path + physical index, per day."""
        base = os.path.realpath(root or delta_path)
        return {d: {"source_kind": kind, "source_path": base, "source_day_index": i}
                for i, d in enumerate(days)}

    def _plan_for_v2(self, delta_path, days_v2, *, smap=None):
        """A `build_block`-shaped plan for a v2 that supersedes v1.

        The segment carries `build_provenance.sources`, because that -- not the plan's
        convenience copy -- is what the staleness guard re-verifies and what an auditor reads
        out of the published manifest."""
        path = self._block("b_v2", days_v2)
        smap = self._smap(delta_path, days_v2) if smap is None else smap
        seg = _seg("b_v2", "b_v2.zarr", self.s0, self.e0, len(days_v2),
                   unknown=self.span[len(days_v2):], sealed=False, supersedes="b_v1",
                   day_digest=bm.day_digest(days_v2), block_path=path,
                   provenance={"sources": smap, "materialized_repairs": {},
                               "source_map_digest": "sha256:test"})
        return {"segment": seg, "out_path": path, "source_map": smap,
                "predecessor_bytes": 1234}


# ==================================================================== WAL: serialization
class TestWalSerialization(_Base):
    def test_checksum_covers_the_record_with_the_field_emptied_not_absent(self):
        rec = {"seq": 1, "repair_id": "r-1", "day": "2026-08-11", "record": rw.INTENT,
               "at_utc": "t", "operator": "o", "payload": {}, "prev_checksum": None,
               "record_checksum": ""}
        c = rw.record_checksum(rec)
        rec["record_checksum"] = c
        self.assertEqual(rw.record_checksum(rec), c, "the digest must be stable once stored")
        # and it must actually cover the content
        other = dict(rec, day="2026-08-12")
        self.assertNotEqual(rw.record_checksum(other), c)

    def test_canonical_json_refuses_nan(self):
        """A checksum over a document another parser cannot read is not evidence."""
        with self.assertRaises(ValueError):
            rw.canonical({"payload": {"x": float("nan")}})

    def test_append_writes_one_fsynced_line_per_record(self):
        rid = self._open_repair(self.span[0], payload={"expected_vars": ["sst"]})
        self._commit(rid, self.span[0], "fp1")
        with open(os.path.join(self.tmp, rw.WAL_NAME)) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual([json.loads(x)["seq"] for x in lines], [1, 2])
        self.assertEqual(json.loads(lines[0])["prev_checksum"], None)
        self.assertEqual(json.loads(lines[1])["prev_checksum"],
                         json.loads(lines[0])["record_checksum"])

    def test_seq_is_allocated_under_the_lock_and_is_gap_free(self):
        for i in range(5):
            self._open_repair(self.span[i])
        st = rw.read_wal(self.tmp)
        self.assertEqual([r["seq"] for r in st.records], [1, 2, 3, 4, 5])
        self.assertEqual(st.last_seq, 5)


# ==================================================================== WAL: fail-closed parsing
class TestWalFailsClosed(_Base):
    def _wal(self):
        return os.path.join(self.tmp, rw.WAL_NAME)

    def _seed(self, n=2):
        self.rid = self._open_repair(self.span[0])
        if n > 1:
            self._commit(self.rid, self.span[0], "fp1")

    def test_a_torn_final_line_refuses_ALL_prune(self):
        """The expected crash artifact. The torn record's day is unknowable, so no day can be
        shown to be unaffected."""
        self._seed()
        with open(self._wal(), "a") as fh:
            fh.write('{"seq": 3, "repair_id": "r2"')          # no newline, truncated
        with self.assertRaises(rw.WalCorrupt) as cm:
            rw.read_wal(self.tmp)
        self.assertEqual(cm.exception.scope, "all")

    def test_a_COMPLETE_final_record_with_no_newline_is_still_torn(self):
        """The crash that lands exactly on the newline boundary.

        The record parses, its checksum verifies, its chain is intact -- everything except the
        terminating byte. Without the missing-newline check this is indistinguishable from a
        healthy log, and the earlier torn-line test does not catch it: that one is rejected by
        the JSON parser, so it passes even with the newline check removed."""
        self._seed()
        state = rw.read_wal(self.tmp)
        rec = {"seq": 3, "repair_id": "r2-3", "day": self.span[5], "record": rw.INTENT,
               "at_utc": "t", "operator": "o", "payload": {},
               "prev_checksum": state.last_checksum, "record_checksum": ""}
        rec["record_checksum"] = rw.record_checksum(rec)
        with open(self._wal(), "a") as fh:
            fh.write(rw.canonical(rec))                        # NO trailing newline
        # precondition: everything except the newline is valid
        self.assertEqual(rw.record_checksum(rec), rec["record_checksum"])
        with self.assertRaises(rw.WalCorrupt) as cm:
            rw.read_wal(self.tmp)
        self.assertIn("newline", str(cm.exception))

    def test_a_checksum_mismatch_refuses_ALL_prune(self):
        self._seed()
        lines = self._lines(self._wal())
        rec = json.loads(lines[0])
        rec["day"] = "2099-01-01"                              # tamper, keep the old checksum
        lines[0] = rw.canonical(rec)
        self._write(self._wal(), "\n".join(lines) + "\n")
        with self.assertRaises(rw.WalCorrupt) as cm:
            rw.read_wal(self.tmp)
        self.assertIn("record_checksum", str(cm.exception))

    def test_a_chain_break_refuses_ALL_prune(self):
        self._seed()
        lines = self._lines(self._wal())
        rec = json.loads(lines[1])
        rec["prev_checksum"] = "0" * 64
        rec["record_checksum"] = rw.record_checksum(rec)       # internally consistent!
        lines[1] = rw.canonical(rec)
        self._write(self._wal(), "\n".join(lines) + "\n")
        with self.assertRaises(rw.WalCorrupt) as cm:
            rw.read_wal(self.tmp)
        self.assertIn("chain", str(cm.exception).lower())

    def test_prev_checksum_null_after_seq_1_is_a_chain_break(self):
        self._seed()
        lines = self._lines(self._wal())
        rec = json.loads(lines[1])
        rec["prev_checksum"] = None
        rec["record_checksum"] = rw.record_checksum(rec)
        lines[1] = rw.canonical(rec)
        self._write(self._wal(), "\n".join(lines) + "\n")
        with self.assertRaises(rw.WalCorrupt):
            rw.read_wal(self.tmp)

    def test_a_seq_gap_refuses_ALL_prune(self):
        """A gap with an INTACT chain, so only the seq check can catch it.

        Dropping a middle record breaks `prev_checksum` too, and dropping the first breaks the
        `null`-at-seq-1 rule -- either way the chain check fires and the seq check is never
        exercised. This log is hand-built so seq jumps 1 -> 3 while every prev_checksum still
        points at the record that actually precedes it."""
        self._seed(n=1)
        state = rw.read_wal(self.tmp)
        rec = {"seq": 3, "repair_id": "r3-3", "day": self.span[5], "record": rw.INTENT,
               "at_utc": "t", "operator": "o", "payload": {},
               "prev_checksum": state.last_checksum, "record_checksum": ""}
        rec["record_checksum"] = rw.record_checksum(rec)
        with open(self._wal(), "a") as fh:
            fh.write(rw.canonical(rec) + "\n")
        with self.assertRaises(rw.WalCorrupt) as cm:
            rw.read_wal(self.tmp)
        msg = str(cm.exception)
        self.assertIn("seq gap", msg)
        self.assertNotIn("chain", msg.lower(),
                         "this fixture must be caught by the seq check, not the chain check")

    def test_an_unknown_field_is_rejected_not_ignored(self):
        self._seed(n=1)
        lines = self._lines(self._wal())
        rec = json.loads(lines[0])
        rec["surprise"] = 1
        rec["record_checksum"] = rw.record_checksum(rec)
        self._write(self._wal(), rw.canonical(rec) + "\n")
        with self.assertRaises(rw.WalCorrupt):
            rw.read_wal(self.tmp)

    def test_the_parser_never_skips_a_bad_line(self):
        """The behaviour that would silently authorize a prune: skip and carry on. A later
        valid record for a DIFFERENT day must not be usable while an earlier line is corrupt."""
        self._seed()
        r2 = self._open_repair(self.span[5])
        rw.append(self.tmp, record=rw.ABORTED, repair_id=r2, day=self.span[5],
                  at_utc="t", operator="o", payload={"reason": "did not land"})
        lines = self._lines(self._wal())
        lines[0] = lines[0].replace('"operator":"o"', '"operator":"tampered"')
        self._write(self._wal(), "\n".join(lines) + "\n")
        with self.assertRaises(rw.WalCorrupt):
            rw.read_wal(self.tmp)

    def test_a_writer_refuses_to_append_after_a_corrupt_tail(self):
        """Valid records must never be appended after corruption -- that buries it under
        legitimate-looking history and makes the administrative recovery ambiguous."""
        self._seed()
        with open(self._wal(), "a") as fh:
            fh.write('{"seq": 3, "trunc')
        before = self._read(self._wal())
        with self.assertRaises(rw.WalCorrupt):
            rw.append(self.tmp, record=rw.INTENT, repair_id=None, day=self.span[9],
                      at_utc="t", operator="o", payload={})
        self.assertEqual(self._read(self._wal()), before,
                         "a refused append must leave the WAL byte-unchanged")


# ==================================================================== WAL: state machine
class TestWalStateMachine(_Base):

    def test_an_open_intent_blocks_its_day_with_no_timeout(self):
        self._open_repair(self.span[0])
        st = rw.read_wal(self.tmp)
        self.assertIn(self.span[0], st.blocked_days)
        out = rw.prune_authorization(st, [self.span[0], self.span[1]])
        self.assertEqual(out["authorized"], [self.span[1]])
        self.assertIn("no timeout", out["refused"][self.span[0]])

    def test_an_identical_terminal_replay_is_a_no_op(self):
        """The expected retry after an uncertain fsync."""
        rid = self._open_repair(self.span[0])
        self._commit(rid, self.span[0], "fp1")
        self._commit(rid, self.span[0], "fp1", at_utc="t2")
        st = rw.read_wal(self.tmp)
        self.assertEqual(st.last_seq, 3, "the replay still gets its own seq")
        self.assertEqual(st.repairs[rid].state, rw.COMMITTED)
        self.assertNotIn(self.span[0], st.poisoned_days)

    def test_a_committed_then_aborted_conflict_poisons_that_day_only(self):
        rid = self._open_repair(self.span[0])
        self._commit(rid, self.span[0], "fp1")
        r2 = self._open_repair(self.span[5])
        with self.assertRaises(rw.WalError):
            rw.append(self.tmp, record=rw.ABORTED, repair_id=rid, day=self.span[0],
                      at_utc="t", operator="o", payload={"reason": "x"})
        st = rw.read_wal(self.tmp)
        self.assertEqual(st.repairs[r2].state, "open", "other repairs are unaffected")

    def test_a_second_commit_with_a_DIFFERENT_fingerprint_is_invalid(self):
        rid = self._open_repair(self.span[0])
        self._commit(rid, self.span[0], "fp1")
        with self.assertRaises(rw.WalError):
            self._commit(rid, self.span[0], "fp2")

    def test_a_terminal_without_an_intent_is_refused(self):
        with self.assertRaises(rw.WalError) as cm:
            rw.append(self.tmp, record=rw.COMMITTED, repair_id="ghost-1", day=self.span[0],
                      at_utc="t", operator="o", payload={"fingerprint": "fp"})
        self.assertIn("no matching repair_intent", str(cm.exception))

    def test_a_repair_id_cannot_be_reused_for_a_different_day(self):
        rid = self._open_repair(self.span[0])
        with self.assertRaises(rw.WalError):
            self._commit(rid, self.span[1], "fp")

    def test_an_aborted_repair_stops_blocking(self):
        rid = self._open_repair(self.span[0])
        rw.append(self.tmp, record=rw.ABORTED, repair_id=rid, day=self.span[0],
                  at_utc="t", operator="o", payload={"reason": "never landed"})
        st = rw.read_wal(self.tmp)
        self.assertNotIn(self.span[0], st.blocked_days)
        self.assertEqual(rw.prune_authorization(st, [self.span[0]])["authorized"],
                         [self.span[0]])


# ==================================================================== WAL: the prune gate (E1)
class TestPruneGateIsIdentityBased(_Base):
    def _committed(self, day, fp):
        rid = self._open_repair(day)
        self._commit(rid, day, fp)
        return rid

    def test_a_never_repaired_day_is_authorized(self):
        st = rw.read_wal(self.tmp)
        self.assertEqual(rw.prune_authorization(st, [self.span[0]])["authorized"],
                         [self.span[0]])

    def test_a_committed_repair_with_no_materialized_entry_is_refused(self):
        """Crash boundary 3: committed, corrective refold not published."""
        self._committed(self.span[0], "fp1")
        out = rw.prune_authorization(rw.read_wal(self.tmp), [self.span[0]])
        self.assertEqual(out["authorized"], [])
        self.assertIn("has not been folded into base", out["refused"][self.span[0]])

    def test_an_OLDER_materialized_repair_is_refused(self):
        """Crash boundary 4: a later repair supersedes what the block carries."""
        r1 = self._committed(self.span[0], "fp1")
        self._committed(self.span[0], "fp2")
        mr = {self.span[0]: {"repair_id": r1, "source_kind": "delta",
                             "source_fingerprint": "fp1"}}
        out = rw.prune_authorization(rw.read_wal(self.tmp), [self.span[0]],
                                     materialized_repairs=mr)
        self.assertEqual(out["authorized"], [])
        self.assertIn("supersedes", out["refused"][self.span[0]])

    def test_the_right_id_with_the_WRONG_fingerprint_is_refused(self):
        rid = self._committed(self.span[0], "fp1")
        mr = {self.span[0]: {"repair_id": rid, "source_kind": "delta",
                             "source_fingerprint": "WRONG"}}
        out = rw.prune_authorization(rw.read_wal(self.tmp), [self.span[0]],
                                     materialized_repairs=mr)
        self.assertEqual(out["authorized"], [])
        self.assertIn("fingerprint", out["refused"][self.span[0]])

    def test_matching_id_and_fingerprint_authorizes(self):
        rid = self._committed(self.span[0], "fp1")
        mr = {self.span[0]: {"repair_id": rid, "source_kind": "delta",
                             "source_fingerprint": "fp1"}}
        out = rw.prune_authorization(rw.read_wal(self.tmp), [self.span[0]],
                                     materialized_repairs=mr)
        self.assertEqual(out["authorized"], [self.span[0]])

    def test_a_LATER_block_carrying_the_wrong_id_is_refused_like_a_stale_one(self):
        """Round-6, stated behaviourally: ordering is not identity.

        Clock evidence would authorize this -- everything about the block postdates the repair.
        Only the id disagrees, and the id is what decides."""
        self._committed(self.span[0], "fp1")
        mr = {self.span[0]: {"repair_id": "r0-earlier-1", "source_kind": "delta",
                             "source_fingerprint": "fp1",
                             "built_at_utc": "2099-01-01T00:00:00Z",
                             "block_newer_than_repair": True}}
        out = rw.prune_authorization(rw.read_wal(self.tmp), [self.span[0]],
                                     materialized_repairs=mr)
        self.assertEqual(out["authorized"], [], "a build timestamp must authorize nothing")

    def test_the_verdict_is_invariant_under_every_timestamp_in_the_log(self):
        """Two logs identical except for `at_utc` must produce the same authorization. If any
        clock reading were consulted, this is where it would show."""
        def build(root, when):
            rec = rw.open_repair(root, day=self.span[0], at_utc=when, operator="o",
                                 payload={})
            rw.append(root, record=rw.COMMITTED, repair_id=rec["repair_id"],
                      day=self.span[0], at_utc=when, operator="o",
                      payload={"fingerprint": "fp1"})
            return rw.read_wal(root), rec["repair_id"]

        (early, rid_a) = build(os.path.join(self.tmp, "a"), "1999-01-01T00:00:00Z")
        (late, rid_b) = build(os.path.join(self.tmp, "b"), "2099-01-01T00:00:00Z")
        self.assertNotEqual(rid_a, rid_b)
        for state, rid in ((early, rid_a), (late, rid_b)):
            mr = {self.span[0]: {"repair_id": rid, "source_kind": "delta",
                                 "source_fingerprint": "fp1"}}
            self.assertEqual(
                rw.prune_authorization(state, [self.span[0]], materialized_repairs=mr),
                {"authorized": [self.span[0]], "refused": {}})
            self.assertEqual(
                rw.prune_authorization(state, [self.span[0]])["authorized"], [])


# ==================================================================== publication §7.4
class TestPublication(_Base):
    def setUp(self):
        super().setUp()
        self.v1_seg, self.v1_days = self._live_gen1()
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.span[:60])

    def test_a_plan_does_not_touch_the_live_manifest(self):
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]), now=T0)
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_publication_commits_generation_n_plus_1_and_archives_it(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        out = _execute(plan, ingest_lock_path=self.lock,
                                      delta_path=self.delta, now=T0)
        self.assertEqual(out["status"], "published")
        live = bm.load_live(self.root)
        self.assertEqual(live["generation"], 2)
        self.assertEqual(live["predecessor_generation"], 1)
        self.assertEqual([s["segment_id"] for s in live["segments"]], ["b_v2"])
        self.assertTrue(os.path.isfile(os.path.join(self.root, bm.archive_name(1))))
        self.assertTrue(os.path.isfile(os.path.join(self.root, bm.archive_name(2))))

    def test_the_superseded_block_moves_to_the_superseded_list_as_referenced(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
        sup = bm.load_live(self.root)["superseded"]
        self.assertEqual([e["segment_id"] for e in sup], ["b_v1"])
        self.assertEqual(sup[0]["status"], "referenced")
        self.assertEqual(sup[0]["superseded_at_generation"], 2)
        self.assertLess(sup[0]["release_after_utc"], sup[0]["hold_until_utc"])

    def test_a_delta_day_that_left_the_delta_aborts_stale(self):
        """The build ran for hours outside the lock. What it read then is a claim."""
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        pruned = os.path.join(self.tmp, "delta2.zarr")
        fx.build_delta(pruned, self.span[30:60])               # the first 30 days are gone
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=pruned,
                                      now=T0)
        self.assertEqual(out["status"], "aborted_stale")
        self.assertFalse(out["published"])
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_a_duplicated_delta_day_aborts_stale(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        g = zarr.open_group(self.delta, mode="a")
        g.attrs["days"] = list(g.attrs["days"]) + [self.span[0]]
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                      now=T0)
        self.assertEqual(out["status"], "aborted_stale")
        self.assertIn("duplicate", out["reason"])

    def test_a_manifest_that_moved_under_the_plan_aborts_stale(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        other = self._manifest([self.v1_seg], generation=2)
        bm.publish(self.root, other)                           # someone else published first
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                      now=T0)
        self.assertEqual(out["status"], "aborted_stale")
        self.assertIn("moved while this plan was held", out["reason"])

    def test_publishing_a_generation_whose_block_is_missing_is_refused(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        shutil.rmtree(os.path.join(self.root, "b_v2.zarr"))
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                      now=T0)
        self.assertEqual(out["status"], "refused")
        self.assertIn("fails closed", out["reason"])
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_superseding_a_segment_that_is_not_published_is_refused(self):
        plan_src = self._plan_for_v2(self.delta, self.span[:60])
        plan_src["segment"]["supersedes"] = "does_not_exist"
        with self.assertRaises(pub.PublishRefused) as cm:
            pub.plan_publication(self.root, plan_src, now=T0)
        self.assertIn("no such segment", str(cm.exception))

    def test_a_running_compaction_blocks_publication(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        clock = os.path.join(self.tmp, "p5_compaction.lock")
        with CompactionLock(clock):
            out = pub.execute_publication(plan, ingest_lock_path=self.lock,
                                          delta_path=self.delta,
                                          compaction_lock_path=clock,
                                          build_artifact_path=None,
                                          unsafe_skip_provenance=True, now=T0)
        self.assertEqual(out["reason"], "compaction_lock_held")
        self.assertFalse(out["published"])

    def test_the_compaction_lock_path_is_REQUIRED(self):
        """It used to be optional, so a caller who forgot it published without ever asking
        whether a build was running."""
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        with self.assertRaises(TypeError):
            pub.execute_publication(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                    build_artifact_path=None, now=T0)
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        with self.assertRaises(pub.PublishRefused) as cm:
            pub.execute_publication(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                    compaction_lock_path=None, build_artifact_path=None,
                                    unsafe_skip_provenance=True, now=T0)
        self.assertIn("compaction_lock_path is required", str(cm.exception))
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_an_idle_compaction_lock_does_not_block_publication(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        clock = os.path.join(self.tmp, "p5_compaction.lock")
        with CompactionLock(clock):
            pass                                            # created, then released
        out = pub.execute_publication(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                      compaction_lock_path=clock, build_artifact_path=None,
                                      unsafe_skip_provenance=True, now=T0)
        self.assertEqual(out["status"], "published")

    def test_the_compaction_lock_is_held_through_publication_not_merely_probed(self):
        """§7.1b: a probe-and-release leaves a window between the check and `bm.publish` in which
        a build can take the lock and begin rewriting the referenced block. The reservation is
        HELD across the critical section instead, so a build that tries to start mid-publish is
        refused.

        Proven by pinning publication inside the critical section -- holding the ingest lock it
        blocks on -- and showing the compaction lock is unavailable to a would-be build while it
        waits. A regression to probe-and-release makes the acquire below SUCCEED.

        No fixed sleep: the worker is given a bounded window to reach the reservation, and the
        lock must then still be busy a moment later. A sleep long enough to be safe on an idle
        machine is not long enough on a loaded one, and a test that fails on correct code is
        worse than no test."""
        import threading
        import time
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        clock = os.path.join(self.tmp, "p5_compaction.lock")
        result = {}
        started, done = threading.Event(), threading.Event()

        def busy() -> bool:
            try:
                CompactionLock(clock).acquire().release()
                return False
            except CompactionLockBusy:
                return True

        holder = open(self.lock, "w")
        fcntl.flock(holder, fcntl.LOCK_EX)          # pin the worker inside the critical section
        try:
            def worker():
                started.set()
                try:
                    result["out"] = pub.execute_publication(
                        plan, ingest_lock_path=self.lock, delta_path=self.delta,
                        compaction_lock_path=clock, build_artifact_path=None,
                        unsafe_skip_provenance=True, now=T0)
                except BaseException as exc:        # noqa: BLE001 -- surfaced to the test
                    result["exc"] = exc
                finally:
                    done.set()

            threading.Thread(target=worker, daemon=True).start()
            self.assertTrue(started.wait(5))

            deadline = time.monotonic() + 10.0
            while not busy() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(busy(), "the compaction lock was never taken as a reservation")
            # ...and it is HELD, not momentarily probed: still busy while the worker waits.
            time.sleep(0.2)
            self.assertTrue(busy(), "the reservation was released before the commit")
            self.assertFalse(done.is_set(), "precondition: the worker is still blocked")
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()

        self.assertTrue(done.wait(10), "worker did not finish after the ingest lock released")
        self.assertNotIn("exc", result, f"worker raised: {result.get('exc')!r}")
        self.assertEqual(result["out"]["status"], "published")

    def test_the_fence_is_re_asserted_immediately_before_the_commit(self):
        """§7.1b: `rm` + recreate gives a new inode another process can lock freely while we
        hold the orphaned one -- the only two-writer state reachable here. Holding the
        reservation is not enough; it must still be OURS at the commit point."""
        import threading
        import time
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        clock = os.path.join(self.tmp, "p5_compaction.lock")
        result = {}
        started, done = threading.Event(), threading.Event()
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))

        def busy() -> bool:
            try:
                CompactionLock(clock).acquire().release()
                return False
            except CompactionLockBusy:
                return True

        holder = open(self.lock, "w")
        fcntl.flock(holder, fcntl.LOCK_EX)
        try:
            def worker():
                started.set()
                try:
                    result["out"] = pub.execute_publication(
                        plan, ingest_lock_path=self.lock, delta_path=self.delta,
                        compaction_lock_path=clock, build_artifact_path=None,
                        unsafe_skip_provenance=True, now=T0)
                except BaseException as exc:        # noqa: BLE001
                    result["exc"] = exc
                finally:
                    done.set()

            threading.Thread(target=worker, daemon=True).start()
            self.assertTrue(started.wait(5))
            deadline = time.monotonic() + 10.0
            while not busy() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(busy(), "the reservation was never taken")
            os.remove(clock)                        # the worker now holds an orphaned inode
            open(clock, "w").close()
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()

        self.assertTrue(done.wait(10))
        self.assertIsInstance(result.get("exc"), CompactionLockError)
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before,
                         "nothing may be committed once the fence fails")

    def test_the_reservation_is_still_held_DURING_the_commit_itself(self):
        """The fence proves the lock was ours immediately before `bm.publish`; this proves it
        is still ours *inside* it. Releasing between the two reopens the very window the
        reservation closes -- narrow, but `bm.publish` writes an archive, fsyncs and renames, so
        it is not instantaneous.

        Probed from inside the commit rather than from another thread, so there is no timing to
        get wrong."""
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        clock = os.path.join(self.tmp, "p5_compaction.lock")
        observed = {}
        real_publish = bm.publish

        def probing_publish(*a, **kw):
            try:
                CompactionLock(clock).acquire().release()
                observed["busy_during_commit"] = False
            except CompactionLockBusy:
                observed["busy_during_commit"] = True
            return real_publish(*a, **kw)

        bm.publish = probing_publish
        try:
            out = pub.execute_publication(plan, ingest_lock_path=self.lock,
                                          delta_path=self.delta,
                                          compaction_lock_path=clock,
                                          build_artifact_path=None,
                                          unsafe_skip_provenance=True, now=T0)
        finally:
            bm.publish = real_publish
        self.assertEqual(out["status"], "published")
        self.assertTrue(observed.get("busy_during_commit"),
                        "a build could have taken the compaction lock during the commit")

    def test_the_generation_archive_is_never_rewritten(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
        with self.assertRaises(bm.ManifestError) as cm:
            bm.publish(self.root, plan["manifest"])
        self.assertIn("immutable", str(cm.exception))

    def test_publication_appends_an_audit_line(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                operator="tester", now=T0)
        with open(os.path.join(self.root, pub.MANIFEST_LOG)) as fh:
            entry = json.loads(fh.read().splitlines()[-1])
        self.assertEqual(entry["op"], "manifest_publish")
        self.assertEqual((entry["from_generation"], entry["to_generation"]), (1, 2))


# ==================================================================== lifecycle §8.5
class TestSupersededLifecycle(_Base):
    def _gen2(self):
        self._live_gen1()
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.span[:60])
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)

    def test_referenced_does_not_advance_before_release_after_utc(self):
        """The block is still an in-flight snapshot's target, the next fold's build input, and
        the rollback target. Releasing early reproduces the S8a fabricated null."""
        self._gen2()
        out = pub.advance_lifecycle(self.root, hold_root=self.hold, now=T0)
        self.assertEqual(out["transitions"], [])
        self.assertIn("release_after_utc", out["blocked"][0]["reason"])

    def test_referenced_advances_to_releasable_after_the_grace(self):
        self._gen2()
        later = T0 + timedelta(seconds=pub.DEFAULT_RELEASE_AFTER_S + 1)
        out = pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True,
                                    ingest_lock_path=self.lock)
        self.assertEqual(out["transitions"][0]["to"], "releasable")
        self.assertEqual(bm.load_live(self.root)["superseded"][0]["status"], "releasable")

    def test_releasable_advances_to_held_by_RENAME_and_the_bytes_survive(self):
        self._gen2()
        later = T0 + timedelta(seconds=pub.DEFAULT_RELEASE_AFTER_S + 1)
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True,
                              ingest_lock_path=self.lock)
        out = pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True,
                                    ingest_lock_path=self.lock)
        self.assertEqual(out["moved"][0]["segment_id"], "b_v1")
        entry = bm.load_live(self.root)["superseded"][0]
        self.assertEqual(entry["status"], "held")
        self.assertTrue(entry["current_path"].startswith(self.hold))
        self.assertFalse(os.path.exists(os.path.join(self.root, "b_v1.zarr")))
        insp = bm.inspect_store_contract(entry["current_path"])
        self.assertEqual(len(insp.days), 30, "a hold move is a rename; content is unaltered")

    def test_held_is_terminal_and_this_module_never_hard_deletes(self):
        self._gen2()
        later = T0 + timedelta(days=pub.DEFAULT_HOLD_DAYS + 1)
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True,
                              ingest_lock_path=self.lock)
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True,
                              ingest_lock_path=self.lock)
        out = pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True,
                                    ingest_lock_path=self.lock)
        self.assertEqual(out["transitions"], [])
        self.assertIn("ops-only", out["blocked"][0]["reason"])
        entry = bm.load_live(self.root)["superseded"][0]
        self.assertTrue(os.path.exists(entry["current_path"]),
                        "nothing in this module may hard-delete a held block")

    def test_a_block_still_in_segments_is_never_released(self):
        """Only reachable via a hand-edited manifest -- which is exactly when it matters."""
        self._live_gen1()
        seg = _seg("b_v1", "b_v1.zarr", self.s0, self.e0, 30, unknown=self.span[30:],
                   sealed=False, day_digest=bm.day_digest(self.span[:30]))
        sup = [{"segment_id": "b_v1", "path": "b_v1.zarr", "superseded_at_generation": 1,
                "release_after_utc": "2020-01-01T00:00:00Z",
                "hold_until_utc": "2020-01-15T00:00:00Z", "status": "referenced",
                "current_path": "b_v1.zarr", "bytes": 1,
                "fingerprint": {"algo": "sha256", "day_digest": "d"}}]
        bm.publish(self.root, self._manifest([seg], generation=2, superseded=sup))
        out = pub.advance_lifecycle(self.root, hold_root=self.hold, now=T0)
        self.assertEqual(out["transitions"], [])
        self.assertIn("still referenced", out["blocked"][0]["reason"])

    def test_apply_false_is_the_default_and_moves_nothing(self):
        self._gen2()
        later = T0 + timedelta(seconds=pub.DEFAULT_RELEASE_AFTER_S + 1)
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=later)
        self.assertEqual(bm.load_live(self.root)["superseded"][0]["status"], "referenced")

    def test_hold_moves_are_recorded_in_an_append_only_log(self):
        self._gen2()
        later = T0 + timedelta(seconds=pub.DEFAULT_RELEASE_AFTER_S + 1)
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True,
                              ingest_lock_path=self.lock)
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True,
                              ingest_lock_path=self.lock)
        with open(os.path.join(self.root, pub.HOLD_LOG)) as fh:
            entry = json.loads(fh.read().splitlines()[-1])
        self.assertEqual(entry["op"], "hold_move")
        self.assertEqual(entry["original_path"], "b_v1.zarr")


# ==================================================================== rollback §9.3 / §9.3a
class TestRollback(_Base):
    def _gen2(self):
        self._live_gen1()
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.span[:60])
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)

    def _to_hold(self):
        later = T0 + timedelta(seconds=pub.DEFAULT_RELEASE_AFTER_S + 1)
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True,
                              ingest_lock_path=self.lock)
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True,
                              ingest_lock_path=self.lock)

    def test_rollback_does_not_consume_the_archive(self):
        """`os.replace(archive, live)` would rename the archive away, destroying the record a
        second rollback, a forward-recovery or an audit needs."""
        self._gen2()
        arch1 = os.path.join(self.root, bm.archive_name(1))
        before = self._read(arch1)
        out = pub.execute_rollback(self.root, 1, hold_root=self.hold,
                                   ingest_lock_path=self.lock, now=T0)
        self.assertTrue(out["rolled_back"])
        self.assertEqual(bm.load_live(self.root)["generation"], 1)
        self.assertEqual(self._read(arch1), before)

    def test_rolling_forward_again_is_the_same_procedure(self):
        self._gen2()
        pub.execute_rollback(self.root, 1, hold_root=self.hold, ingest_lock_path=self.lock,
                             now=T0)
        pub.execute_rollback(self.root, 2, hold_root=self.hold, ingest_lock_path=self.lock,
                             now=T0)
        self.assertEqual(bm.load_live(self.root)["generation"], 2)

    def test_a_block_in_hold_is_RESTORED_before_the_pointer_moves(self):
        self._gen2()
        self._to_hold()
        self.assertFalse(os.path.exists(os.path.join(self.root, "b_v1.zarr")))
        out = pub.execute_rollback(self.root, 1, hold_root=self.hold,
                                   ingest_lock_path=self.lock, now=T0)
        self.assertTrue(out["rolled_back"])
        self.assertEqual([r["segment_id"] for r in out["restored"]], ["b_v1"])
        self.assertTrue(os.path.exists(os.path.join(self.root, "b_v1.zarr")),
                        "the block must be back at the path generation 1 names")
        self.assertEqual(bm.load_live(self.root)["generation"], 1)

    def test_restore_is_planned_before_it_is_applied(self):
        self._gen2()
        self._to_hold()
        out = pub.restore_before_rollback(self.root, 1, hold_root=self.hold, now=T0)
        self.assertEqual(out["status"], "planned")
        self.assertFalse(out["applied"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "b_v1.zarr")))

    def test_a_hard_deleted_block_makes_rollback_unavailable_and_refuses(self):
        self._gen2()
        self._to_hold()
        entry = bm.load_live(self.root)["superseded"][0]
        shutil.rmtree(entry["current_path"])                  # the ops-only act
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        out = pub.execute_rollback(self.root, 1, hold_root=self.hold,
                                   ingest_lock_path=self.lock, now=T0)
        self.assertEqual(out["status"], "refused")
        self.assertFalse(out["rolled_back"])
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before,
                         "the pointer must not move when a referenced block is gone")

    def test_a_restored_block_whose_day_set_differs_is_a_HARD_STOP(self):
        """Never restore-anyway: a block that does not match what generation N declares is a
        different block, and publishing the pointer at it would serve wrong data."""
        self._gen2()
        self._to_hold()
        entry = bm.load_live(self.root)["superseded"][0]
        shutil.rmtree(entry["current_path"])
        fx.build_block(entry["current_path"], self.span[:29])   # 29 days, not 30
        before_gen = bm.load_live(self.root)["generation"]
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        with self.assertRaises(pub.PublishRefused) as cm:
            pub.execute_rollback(self.root, 1, hold_root=self.hold,
                                 ingest_lock_path=self.lock, now=T0)
        self.assertIn("human triage", str(cm.exception))
        self.assertEqual(bm.load_live(self.root)["generation"], before_gen,
                         "the pointer must not move")
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)
        self.assertTrue(os.path.exists(entry["current_path"]),
                        "the mismatched block goes back to hold, not left half-restored")

    def test_rollback_appends_an_audit_line_in_both_directions(self):
        self._gen2()
        pub.execute_rollback(self.root, 1, hold_root=self.hold, ingest_lock_path=self.lock,
                             operator="tester", reason="probe failed", now=T0)
        with open(os.path.join(self.root, pub.MANIFEST_LOG)) as fh:
            entry = json.loads(fh.read().splitlines()[-1])
        self.assertEqual(entry["op"], "manifest_rollback")
        self.assertEqual((entry["from_generation"], entry["to_generation"]), (2, 1))
        self.assertEqual(entry["reason"], "probe failed")


# ==================================================================== WAL <-> publication
class TestPruneOutlookReporting(_Base):
    def test_an_untrustworthy_wal_authorizes_nothing(self):
        self._open_repair(self.span[0])
        with open(os.path.join(self.tmp, rw.WAL_NAME), "a") as fh:
            fh.write('{"seq": 2, "tr')
        out = pub.prune_outlook(self.root, [self.span[0], self.span[1]], wal_root=self.tmp)
        self.assertEqual(out["authorized"], [])
        self.assertEqual(out["reason"], "repair_wal_untrustworthy")

    def test_an_absent_wal_is_not_an_error(self):
        """A deployment that has never had a repair has no log, and that authorizes normally."""
        out = pub.prune_outlook(self.root, [self.span[0]], wal_root=self.tmp)
        self.assertEqual(out["authorized"], [self.span[0]])

    def test_publication_itself_never_requires_prune_authorization(self):
        """Publishing a block changes no served value: the manifest does not describe delta,
        and delta still wins for the folded days. A blocked day must not block a publish."""
        self._live_gen1()
        delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(delta, self.span[:60])
        self._open_repair(self.span[0])
        plan = pub.plan_publication(self.root, self._plan_for_v2(delta, self.span[:60]), now=T0)
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=delta,
                                      now=T0)
        self.assertEqual(out["status"], "published")
        self.assertIn("UNCHANGED", out["note"])


if __name__ == "__main__":
    unittest.main()


# ==================================================================== review round 2 findings
class TestGuardUsesThePublishedProvenance(_Base):
    """The staleness guard must re-verify the map the manifest will actually carry."""

    def setUp(self):
        super().setUp()
        self._live_gen1()
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.span[:60])

    def test_a_plan_whose_two_maps_disagree_is_refused(self):
        src = self._plan_for_v2(self.delta, self.span[:60])
        src["source_map"] = dict(src["source_map"])
        src["source_map"][self.span[0]] = dict(src["source_map"][self.span[0]],
                                               source_day_index=999)
        with self.assertRaises(pub.PublishRefused) as cm:
            pub.plan_publication(self.root, src, now=T0)
        self.assertIn("build_provenance.sources", str(cm.exception))

    def test_a_segment_without_provenance_cannot_be_published(self):
        src = self._plan_for_v2(self.delta, self.span[:60])
        src["segment"].pop("build_provenance")
        with self.assertRaises(pub.PublishRefused) as cm:
            pub.plan_publication(self.root, src, now=T0)
        self.assertIn("no build_provenance", str(cm.exception))

    def test_the_guard_reads_the_SEGMENT_map_not_the_plans_copy(self):
        """A plan omitting `source_map` entirely must still be fully guarded."""
        src = self._plan_for_v2(self.delta, self.span[:60])
        src.pop("source_map")
        plan = pub.plan_publication(self.root, src, now=T0)
        self.assertEqual(plan["source_map"],
                         src["segment"]["build_provenance"]["sources"])
        retargeted = os.path.join(self.tmp, "delta_v2.zarr")
        fx.build_delta(retargeted, self.span[:60])
        out = _execute(plan, ingest_lock_path=self.lock,
                                      delta_path=retargeted, now=T0)
        self.assertEqual(out["status"], "aborted_stale")


class TestStalenessChecksIdentityNotMembership(_Base):
    def setUp(self):
        super().setUp()
        self._live_gen1()
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.span[:60])

    def _plan(self, **kw):
        return pub.plan_publication(self.root,
                                    self._plan_for_v2(self.delta, self.span[:60], **kw), now=T0)

    def test_a_swap_behind_the_SAME_alias_aborts_stale(self):
        """Every date is still present. The store behind the alias is a different one, and a
        date-membership check cannot see that at all."""
        alias = os.path.join(self.tmp, "delta_alias.zarr")
        real1 = os.path.join(self.tmp, "delta_v1.zarr")
        fx.build_delta(real1, self.span[:60])
        os.symlink(real1, alias)
        plan = pub.plan_publication(self.root,
                                    self._plan_for_v2(alias, self.span[:60]), now=T0)
        real2 = os.path.join(self.tmp, "delta_v2.zarr")
        fx.build_delta(real2, self.span[:60])
        os.remove(alias)
        os.symlink(real2, alias)                               # the swap
        live = fx.read_days(alias)
        self.assertEqual(set(live), set(self.span[:60]), "precondition: same date set")
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=alias,
                                      now=T0)
        self.assertEqual(out["status"], "aborted_stale")
        self.assertIn("not the same bytes", out["reason"])

    def test_a_day_that_MOVED_physical_index_aborts_stale(self):
        """`attrs['days']` is append order after a backfill, so a rebuild can keep every date
        and move it. The block then records a slot it did not read."""
        plan = self._plan()
        g = zarr.open_group(self.delta, mode="a")
        days = list(g.attrs["days"])
        g.attrs["days"] = days[30:] + days[:30]                # same set, rotated
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                      now=T0)
        self.assertEqual(out["status"], "aborted_stale")
        self.assertIn("physical index", out["reason"])

    def test_a_daily_source_whose_DAY_GROUP_is_gone_aborts_stale(self):
        """The root exists for years; that proves nothing about YYYY/MM/DD."""
        daily = os.path.join(self.tmp, "mur.zarr")
        fx.build_daily(daily, self.span[:60])
        smap = {d: {"source_kind": "daily", "source_path": os.path.realpath(daily),
                    "source_day_index": 0} for d in self.span[:60]}
        plan = pub.plan_publication(
            self.root, self._plan_for_v2(self.delta, self.span[:60], smap=smap), now=T0)
        y, m, d = self.span[0].split("-")
        shutil.rmtree(os.path.join(daily, y, m, d))
        self.assertTrue(os.path.isdir(daily), "precondition: the ROOT is still there")
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                      now=T0)
        self.assertEqual(out["status"], "aborted_stale")
        self.assertIn("source group", out["reason"])

    def test_an_intact_daily_source_publishes(self):
        daily = os.path.join(self.tmp, "mur.zarr")
        fx.build_daily(daily, self.span[:60])
        smap = {d: {"source_kind": "daily", "source_path": os.path.realpath(daily),
                    "source_day_index": 0} for d in self.span[:60]}
        plan = pub.plan_publication(
            self.root, self._plan_for_v2(self.delta, self.span[:60], smap=smap), now=T0)
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                      now=T0)
        self.assertEqual(out["status"], "published")


class TestLifecycleIsolationAndAtomicity(_Base):
    def _gen2(self):
        self._live_gen1()
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.span[:60])
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
        self.later = T0 + timedelta(seconds=pub.DEFAULT_RELEASE_AFTER_S + 1)

    def test_apply_without_the_ingest_lock_is_refused(self):
        self._gen2()
        with self.assertRaises(pub.PublishRefused) as cm:
            pub.advance_lifecycle(self.root, hold_root=self.hold, now=self.later, apply=True)
        self.assertIn("ingest_lock_path", str(cm.exception))

    def test_a_failed_publish_UNDOES_the_rename(self):
        """Rename-then-publish must be atomic-or-undone: `current_path` is what §9.3a restore
        looks up, so a moved block the manifest still names at its old path is on disk and
        unreachable at the same time."""
        self._gen2()
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=self.later, apply=True,
                              ingest_lock_path=self.lock)                    # -> releasable
        src = os.path.join(self.root, "b_v1.zarr")
        self.assertTrue(os.path.exists(src))
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))

        real_publish = bm.publish
        bm.publish = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
        try:
            with self.assertRaises(RuntimeError):
                pub.advance_lifecycle(self.root, hold_root=self.hold, now=self.later,
                                      apply=True, ingest_lock_path=self.lock)
        finally:
            bm.publish = real_publish

        self.assertTrue(os.path.exists(src), "the rename must be undone")
        self.assertFalse(os.path.exists(os.path.join(self.hold, "b_v1.zarr")))
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)
        entry = bm.load_live(self.root)["superseded"][0]
        self.assertEqual(entry["current_path"], "b_v1.zarr")

    def test_no_hold_move_is_logged_when_the_publish_fails(self):
        """An audit trail that claims a move that was rolled back is worse than none."""
        self._gen2()
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=self.later, apply=True,
                              ingest_lock_path=self.lock)
        real_publish = bm.publish
        bm.publish = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full"))
        try:
            with self.assertRaises(RuntimeError):
                pub.advance_lifecycle(self.root, hold_root=self.hold, now=self.later,
                                      apply=True, ingest_lock_path=self.lock)
        finally:
            bm.publish = real_publish
        self.assertFalse(os.path.isfile(os.path.join(self.root, pub.HOLD_LOG)))

    def test_apply_is_mutually_exclusive_with_a_held_ingest_lock(self):
        """Requiring the lock path is not the same as taking the lock.

        `flock` conflicts across distinct open file descriptions even inside one process, so
        the test can hold the real lock and observe that the lifecycle genuinely waits."""
        import threading
        self._gen2()
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=self.later, apply=True,
                              ingest_lock_path=self.lock)                    # -> releasable
        gen_before = bm.load_live(self.root)["generation"]

        started, done, error = threading.Event(), threading.Event(), []

        def worker():
            started.set()
            try:
                pub.advance_lifecycle(self.root, hold_root=self.hold, now=self.later,
                                      apply=True, ingest_lock_path=self.lock)
            except BaseException as exc:                       # surfaced after the join
                error.append(exc)
            finally:
                done.set()

        holder = open(self.lock, "w")
        try:
            fcntl.flock(holder, fcntl.LOCK_EX)
            th = threading.Thread(target=worker, daemon=True)
            th.start()
            self.assertTrue(started.wait(5), "the worker never started")
            self.assertFalse(done.wait(0.5), "the lifecycle proceeded while the lock was held")
            self.assertEqual(bm.load_live(self.root)["generation"], gen_before)
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
        self.assertTrue(done.wait(20), "the lifecycle never completed after the lock released")
        self.assertFalse(error, f"worker raised: {error}")
        self.assertEqual(bm.load_live(self.root)["generation"], gen_before + 1)

    def test_a_current_path_that_does_not_exist_is_REFUSED_not_repaired(self):
        """The split state a crashed apply would leave looks identical to a deletion, and the
        two need different answers."""
        self._gen2()
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=self.later, apply=True,
                              ingest_lock_path=self.lock)
        shutil.rmtree(os.path.join(self.root, "b_v1.zarr"))
        with self.assertRaises(pub.PublishRefused) as cm:
            pub.advance_lifecycle(self.root, hold_root=self.hold, now=self.later, apply=True,
                                  ingest_lock_path=self.lock)
        msg = str(cm.exception)
        self.assertIn("instead of guessing", msg)
        self.assertIn("b_v1", msg)


class TestWalStrictnessRoundTwo(_Base):
    def test_a_hand_made_repair_id_without_the_seq_suffix_is_refused(self):
        with self.assertRaises(rw.WalError) as cm:
            rw.append(self.tmp, record=rw.INTENT, repair_id="r1", day=self.span[0],
                      at_utc="t", operator="o", payload={})
        self.assertIn("must end in", str(cm.exception))

    def _handwrite(self, rec):
        """Write a single checksum-valid record by hand -- the append gate is bypassed, so
        only the PARSER can catch what is wrong with it."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        rec = dict(rec, record_checksum="")
        rec["record_checksum"] = rw.record_checksum(rec)
        with open(os.path.join(tmp, rw.WAL_NAME), "w") as fh:
            fh.write(rw.canonical(rec) + "\n")
        return tmp

    def test_the_PARSER_refuses_a_suffixless_repair_id(self):
        """A hand-edited log never passes through the append gate, so the suffix rule has to
        hold in the parser too or the whole check is bypassable with a text editor."""
        root = self._handwrite({"seq": 1, "repair_id": "opaque", "day": self.span[0],
                                "record": rw.INTENT, "at_utc": "t", "operator": "o",
                                "payload": {}, "prev_checksum": None})
        with self.assertRaises(rw.WalCorrupt) as cm:
            rw.read_wal(root)
        self.assertIn("-<seq>", str(cm.exception))

    def test_the_PARSER_refuses_an_intent_whose_suffix_is_not_its_own_seq(self):
        root = self._handwrite({"seq": 1, "repair_id": "x-7", "day": self.span[0],
                                "record": rw.INTENT, "at_utc": "t", "operator": "o",
                                "payload": {}, "prev_checksum": None})
        with self.assertRaises(rw.WalCorrupt) as cm:
            rw.read_wal(root)
        self.assertIn("must be the intent's own seq", str(cm.exception))

    def test_a_suffix_that_is_not_the_intents_own_seq_is_refused(self):
        self._open_repair(self.span[0])
        with self.assertRaises(rw.WalError):
            rw.append(self.tmp, record=rw.INTENT, repair_id="x-99", day=self.span[1],
                      at_utc="t", operator="o", payload={})

    def test_open_repair_allocates_unique_monotonic_ids(self):
        ids = [self._open_repair(self.span[i]) for i in range(4)]
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual([rw.repair_id_seq(i) for i in ids], [1, 2, 3, 4])

    def test_a_replay_agreeing_on_the_fingerprint_but_not_the_REST_is_invalid(self):
        """Two commits that agree on the fingerprint and disagree on anything else are two
        different claims about what happened; picking one silently is the ambiguity this log
        exists to remove."""
        rid = self._open_repair(self.span[0])
        self._commit(rid, self.span[0], "fp1", vars=["sst"])
        with self.assertRaises(rw.WalError) as cm:
            self._commit(rid, self.span[0], "fp1", vars=["sst", "sea_ice"])
        self.assertIn("conflicting terminal", str(cm.exception))

    def test_a_byte_identical_replay_is_still_a_no_op(self):
        rid = self._open_repair(self.span[0])
        self._commit(rid, self.span[0], "fp1", vars=["sst"])
        self._commit(rid, self.span[0], "fp1", vars=["sst"])
        st = rw.read_wal(self.tmp)
        self.assertEqual(st.repairs[rid].state, rw.COMMITTED)
        self.assertNotIn(self.span[0], st.poisoned_days)

    def test_a_replay_differing_only_in_payload_poisons_the_day_when_hand_written(self):
        """The append gate refuses it; a hand-edited log must be caught by the PARSER too."""
        rid = self._open_repair(self.span[0])
        self._commit(rid, self.span[0], "fp1", vars=["sst"])
        st = rw.read_wal(self.tmp)
        rec = {"seq": 3, "repair_id": rid, "day": self.span[0], "record": rw.COMMITTED,
               "at_utc": "t", "operator": "o",
               "payload": {"fingerprint": "fp1", "vars": ["sst", "sea_ice"]},
               "prev_checksum": st.last_checksum, "record_checksum": ""}
        rec["record_checksum"] = rw.record_checksum(rec)
        with open(os.path.join(self.tmp, rw.WAL_NAME), "a") as fh:
            fh.write(rw.canonical(rec) + "\n")
        st = rw.read_wal(self.tmp)
        self.assertIn(self.span[0], st.poisoned_days)
        self.assertEqual(rw.prune_authorization(st, [self.span[0]])["authorized"], [])

    def test_wrongly_typed_fields_are_refused_even_with_a_valid_checksum(self):
        for field, bad in (("seq", 1.0), ("day", 20260627), ("payload", []),
                           ("operator", None), ("repair_id", 7)):
            with self.subTest(field=field):
                tmp = tempfile.mkdtemp()
                self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
                rec = {"seq": 1, "repair_id": "r-1", "day": self.span[0],
                       "record": rw.INTENT, "at_utc": "t", "operator": "o", "payload": {},
                       "prev_checksum": None, "record_checksum": ""}
                rec[field] = bad
                rec["record_checksum"] = rw.record_checksum(rec)   # internally consistent
                with open(os.path.join(tmp, rw.WAL_NAME), "w") as fh:
                    fh.write(rw.canonical(rec) + "\n")
                with self.assertRaises(rw.WalCorrupt):
                    rw.read_wal(tmp)


# ============================================== review round 3: the map is BOUND, not checked
class TestSourceMapIsReboundAtPublication(_Base):
    """A plan exists to be inspected, serialized and read back by a human. Validating its
    `source_map` once at planning time is a check; re-deriving the authority from the manifest
    about to be published is a binding. Only the second survives an edit in between."""

    def setUp(self):
        super().setUp()
        self._live_gen1()
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.span[:60])
        self.retargeted = os.path.join(self.tmp, "delta_v2.zarr")
        fx.build_delta(self.retargeted, self.span[:60])

    def _plan(self):
        return pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)

    def _publish(self, plan, **kw):
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        try:
            out = _execute(plan, ingest_lock_path=self.lock,
                                          delta_path=self.delta, now=T0, **kw)
        except pub.PublishRefused as exc:
            out = {"status": "refused", "published": False, "reason": str(exc)}
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before,
                         "a refusal must leave manifest.json byte-identical")
        return out

    def test_a_source_map_MUTATED_after_planning_is_refused(self):
        plan = self._plan()
        plan["source_map"] = dict(plan["source_map"])
        plan["source_map"][self.span[0]] = dict(plan["source_map"][self.span[0]],
                                                source_day_index=999)
        out = self._publish(plan)
        self.assertFalse(out["published"])
        self.assertIn("at publication", out["reason"])

    def test_a_source_map_EMPTIED_after_planning_is_refused(self):
        """`{}` is not a missing field. It is a positive claim that the block read nothing,
        and `if top:` used to skip straight past it."""
        plan = self._plan()
        plan["source_map"] = {}
        out = self._publish(plan)
        self.assertFalse(out["published"])
        self.assertIn("disagree", out["reason"])

    def test_a_source_map_DELETED_after_planning_falls_back_to_the_provenance(self):
        """An absent field has nothing to contradict, so the segment's own map is used -- and
        it must still be fully guarded, not waved through."""
        plan = self._plan()
        del plan["source_map"]
        out = _execute(plan, ingest_lock_path=self.lock,
                                      delta_path=self.retargeted, now=T0)
        self.assertEqual(out["status"], "aborted_stale")

    def test_a_deleted_source_map_still_publishes_when_the_delta_is_intact(self):
        plan = self._plan()
        del plan["source_map"]
        out = _execute(plan, ingest_lock_path=self.lock,
                                      delta_path=self.delta, now=T0)
        self.assertEqual(out["status"], "published")

    def test_an_empty_map_is_distinguished_from_an_absent_one(self):
        """The two must not resolve to the same verdict, which is exactly what `plan.get()`
        without a sentinel would do."""
        empty, absent = self._plan(), self._plan()
        empty["source_map"] = {}
        del absent["source_map"]
        self.assertFalse(self._publish(empty)["published"])
        self.assertEqual(_execute(absent, ingest_lock_path=self.lock,
                                                 delta_path=self.delta, now=T0)["status"],
                         "published")

    def test_a_non_dict_source_map_is_refused(self):
        plan = self._plan()
        plan["source_map"] = [[self.span[0], {}]]
        out = self._publish(plan)
        self.assertFalse(out["published"])
        self.assertIn("must be an object", out["reason"])

    def test_the_PUBLISHED_segment_provenance_is_the_authority(self):
        """Editing the manifest's own provenance must change what the guard checks -- proving
        the guard reads the segment being published, not a copy made at planning time."""
        plan = self._plan()
        seg = next(s for s in plan["manifest"]["segments"] if s["segment_id"] == "b_v2")
        seg["build_provenance"]["sources"] = {
            d: {"source_kind": "delta", "source_path": os.path.realpath(self.retargeted),
                "source_day_index": i} for i, d in enumerate(self.span[:60])}
        plan["manifest"]["manifest_checksum"] = ""      # a forger would recompute this
        plan["manifest"]["manifest_checksum"] = bm.compute_checksum(plan["manifest"])
        del plan["source_map"]
        out = self._publish(plan)
        self.assertEqual(out["status"], "aborted_stale")
        self.assertIn("not the same bytes", out["reason"])

    def test_a_plan_whose_manifest_lacks_its_own_segment_is_refused(self):
        plan = self._plan()
        plan["manifest"]["segments"] = [s for s in plan["manifest"]["segments"]
                                        if s["segment_id"] != "b_v2"]
        out = self._publish(plan)
        self.assertFalse(out["published"])
        self.assertIn("cannot be the plan for the block it claims", out["reason"])

    def test_a_plan_round_tripped_through_json_still_publishes(self):
        """The workflow the binding exists for: plan -> file -> human -> execute."""
        plan = self._plan()
        path = os.path.join(self.tmp, "plan.json")
        with open(path, "w") as fh:
            json.dump(plan, fh, indent=2, sort_keys=True)
        with open(path) as fh:
            reloaded = json.load(fh)
        out = _execute(reloaded, ingest_lock_path=self.lock,
                                      delta_path=self.delta, now=T0)
        self.assertEqual(out["status"], "published")


class TestWalWriterDoesNotLaunderPayload(_Base):
    def test_a_list_of_pairs_payload_is_refused_not_coerced(self):
        """`dict(payload)` accepts a list of pairs and writes a record that looks well-formed.
        The reader could never tell the writer had passed something else."""
        with self.assertRaises(rw.WalError) as cm:
            rw.append(self.tmp, record=rw.INTENT, repair_id=None, day=self.span[0],
                      at_utc="t", operator="o", payload=[("fingerprint", "fp1")])
        self.assertIn("NOT coerced", str(cm.exception))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, rw.WAL_NAME)))

    def test_a_dict_subclass_is_still_accepted(self):
        class D(dict):
            pass
        rw.append(self.tmp, record=rw.INTENT, repair_id=None, day=self.span[0],
                  at_utc="t", operator="o", payload=D(expected_vars=["sst"]))
        self.assertEqual(rw.read_wal(self.tmp).last_seq, 1)

    def test_a_FALSEY_dict_subclass_keeps_its_contents(self):
        """`payload or {}` would replace this with an empty payload -- writing a record with
        content the caller never asked for, which is worse than rejecting it."""
        class Falsey(dict):
            def __bool__(self):
                return False

        payload = Falsey(fingerprint="fp1", expected_vars=["sst"])
        self.assertFalse(payload, "precondition: it really is falsey")
        self.assertTrue(len(payload), "precondition: and really is non-empty")
        rw.append(self.tmp, record=rw.INTENT, repair_id=None, day=self.span[0],
                  at_utc="t", operator="o", payload=payload)
        self.assertEqual(rw.read_wal(self.tmp).records[0]["payload"],
                         {"fingerprint": "fp1", "expected_vars": ["sst"]})

    def test_a_falsey_subclass_replay_is_compared_on_CONTENT(self):
        """The replay comparison must see the real payload too, not an emptied one."""
        class Falsey(dict):
            def __bool__(self):
                return False

        rid = self._open_repair(self.span[0])
        self._commit(rid, self.span[0], "fp1", vars=["sst"])
        with self.assertRaises(rw.WalError):
            rw.append(self.tmp, record=rw.COMMITTED, repair_id=rid, day=self.span[0],
                      at_utc="t", operator="o",
                      payload=Falsey(fingerprint="fp1", vars=["sst", "sea_ice"]))

    def test_an_absent_payload_is_still_allowed(self):
        rw.append(self.tmp, record=rw.INTENT, repair_id=None, day=self.span[0],
                  at_utc="t", operator="o")
        self.assertEqual(rw.read_wal(self.tmp).records[0]["payload"], {})


# ============================================ review round 4: the map is VALIDATED and BOUND
class TestSourceMapIsStrictlyValidated(_Base):
    """Two copies agreeing proves they agree. It says nothing about whether either is
    well-formed -- and anyone who can edit a plan can edit both copies and recompute
    `manifest_checksum`, so a self-consistent map is still untrusted input."""

    def setUp(self):
        super().setUp()
        self._live_gen1()
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.span[:60])

    def _plan_src(self, mutate=None):
        src = self._plan_for_v2(self.delta, self.span[:60])
        if mutate:
            mutate(src["segment"]["build_provenance"]["sources"])
        src["source_map"] = src["segment"]["build_provenance"]["sources"]
        return src

    def _refused(self, mutate):
        with self.assertRaises(pub.PublishRefused) as cm:
            pub.plan_publication(self.root, self._plan_src(mutate), now=T0)
        return str(cm.exception)

    def test_an_unknown_source_kind_is_refused(self):
        """The one that matters most: an unrecognised kind falls through the guard's `else`
        branch and is treated as a non-delta source, skipping realpath and index entirely."""
        def m(s):
            s[self.span[0]] = dict(s[self.span[0]], source_kind="netcdf")
        msg = self._refused(m)
        self.assertIn("source_kind", msg)
        self.assertIn("skipping the realpath and index checks", msg)

    def test_a_string_index_is_refused_not_coerced(self):
        def m(s):
            s[self.span[0]] = dict(s[self.span[0]], source_day_index="0")
        self.assertIn("not something that merely converts", self._refused(m))

    def test_a_float_or_bool_index_is_refused(self):
        for bad in (0.0, True, -1):
            with self.subTest(bad=bad):
                def m(s, bad=bad):
                    s[self.span[0]] = dict(s[self.span[0]], source_day_index=bad)
                self.assertIn("source_day_index", self._refused(m))

    def test_a_relative_or_empty_source_path_is_refused(self):
        for bad in ("", "delta.zarr", None):
            with self.subTest(bad=bad):
                def m(s, bad=bad):
                    s[self.span[0]] = dict(s[self.span[0]], source_path=bad)
                self.assertIn("absolute path", self._refused(m))

    def test_an_extra_or_missing_record_field_is_refused(self):
        def extra(s):
            s[self.span[0]] = dict(s[self.span[0]], note="hand-edited")
        self.assertIn("unexpected", self._refused(extra))

        def missing(s):
            s[self.span[0]] = {"source_kind": "delta"}
        self.assertIn("missing", self._refused(missing))

    def test_a_map_that_does_not_cover_the_segments_present_days_is_refused(self):
        """A day missing from the map is a day the staleness guard never checks."""
        def drop(s):
            del s[self.span[0]]
        self.assertIn("never checks", self._refused(drop))

        def add(s):
            s["2099-01-01"] = {"source_kind": "delta",
                               "source_path": os.path.realpath(self.delta),
                               "source_day_index": 0}
        self.assertIn("does not cover", self._refused(add))

    def test_a_non_object_entry_is_refused(self):
        def m(s):
            s[self.span[0]] = ["delta", "/x", 0]
        self.assertIn("not an object", self._refused(m))

    def test_validation_also_runs_at_publication_not_only_at_planning(self):
        """A plan edited after planning must be re-validated, not merely re-compared."""
        plan = pub.plan_publication(self.root, self._plan_src(), now=T0)
        seg = next(s for s in plan["manifest"]["segments"] if s["segment_id"] == "b_v2")
        seg["build_provenance"]["sources"][self.span[0]]["source_kind"] = "netcdf"
        plan["manifest"]["manifest_checksum"] = ""
        plan["manifest"]["manifest_checksum"] = bm.compute_checksum(plan["manifest"])
        del plan["source_map"]
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        with self.assertRaises(pub.PublishRefused) as cm:
            _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                    now=T0)
        self.assertIn("source_kind", str(cm.exception))
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)


class TestProvenanceIsBoundToTheBlock(_Base):
    """These exercise the artifact's LOCATOR agreement and segment identity.

    They pass `recheck_sources=False`: the fixture builds the block and the delta independently,
    so the block is not actually derived from its declared source and a byte comparison against
    that source would (correctly) refuse. The end-to-end source-byte check, where the builder
    really does read the delta, is exercised in the P5-S5 suite."""
    def setUp(self):
        super().setUp()
        self._live_gen1()
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.span[:60])

    def test_a_map_describing_a_DIFFERENT_block_is_refused(self):
        """Everything before this is plan-internal. This opens the block being published and
        requires the map to cover exactly the days it actually contains -- so a forged map
        fails unless the forger also rebuilds the block."""
        src = self._plan_for_v2(self.delta, self.span[:60])
        # a segment that DECLARES 30 days, with a matching map, but whose block holds 60
        src["segment"]["day_count"] = 30
        src["segment"]["unknown"] = self.span[30:]
        src["segment"]["materialized_through"] = self.span[29]
        smap = self._smap(self.delta, self.span[:30])
        src["segment"]["build_provenance"]["sources"] = smap
        src["source_map"] = smap
        plan = pub.plan_publication(self.root, src, now=T0)      # internally consistent
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        with self.assertRaises(pub.PublishRefused) as cm:
            _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                    now=T0)
        self.assertIn("different block", str(cm.exception))
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_a_matching_map_publishes(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                      now=T0)
        self.assertEqual(out["status"], "published")

    def test_a_build_artifact_that_disagrees_on_a_LOCATOR_is_refused(self):
        """The builder's own record is the only evidence outside the manifest of what was
        read, so when it exists it wins."""
        src = self._plan_for_v2(self.delta, self.span[:60])
        path, doc = self._artifact_for(src["out_path"], "b_v2", src["source_map"])
        doc["days"][self.span[0]]["source_day_index"] = 41
        doc["artifact_checksum"] = sp.artifact_checksum(doc)
        sp.write_artifact(path, doc)
        plan = pub.plan_publication(self.root, src, now=T0)
        with self.assertRaises(pub.PublishRefused) as cm:
            _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                     build_artifact_path=path, unsafe_skip_provenance=False,
                     recheck_sources=False, now=T0)
        self.assertIn("source_day_index", str(cm.exception))

    def test_a_build_artifact_for_another_segment_is_refused(self):
        src = self._plan_for_v2(self.delta, self.span[:60])
        path, _ = self._artifact_for(src["out_path"], "someone_else", src["source_map"])
        plan = pub.plan_publication(self.root, src, now=T0)
        with self.assertRaises(pub.PublishRefused) as cm:
            _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                     build_artifact_path=path, unsafe_skip_provenance=False,
                     recheck_sources=False, now=T0)
        self.assertIn("describes segment", str(cm.exception))

    def test_an_agreeing_build_artifact_publishes(self):
        src = self._plan_for_v2(self.delta, self.span[:60])
        path, _ = self._artifact_for(src["out_path"], "b_v2", src["source_map"])
        plan = pub.plan_publication(self.root, src, now=T0)
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                       build_artifact_path=path, unsafe_skip_provenance=False,
                       recheck_sources=False, now=T0)
        self.assertEqual(out["status"], "published")
        self.assertTrue(out["provenance"]["verified"])
        self.assertEqual(out["provenance"]["days"], 60)


# ============================ review round 5: full block binding + kind-specific source checks
class TestSegmentEntryIsBoundToTheBlock(_Base):
    """A manifest that passes its own checksum but disagrees with the block is not caught until
    a snapshot build fails closed -- which is an outage (stale in-memory snapshot, unusable
    manifest on disk) rather than a refusal."""

    def setUp(self):
        super().setUp()
        self._live_gen1()
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.span[:60])

    def _edited(self, mutate):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        seg = next(s for s in plan["manifest"]["segments"] if s["segment_id"] == "b_v2")
        mutate(seg, plan["manifest"])
        plan["manifest"]["manifest_checksum"] = ""
        plan["manifest"]["manifest_checksum"] = bm.compute_checksum(plan["manifest"])
        bm.validate_manifest(plan["manifest"])     # precondition: still schema-legal
        del plan["source_map"]
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        try:
            out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
            reason = out["reason"]
            self.assertFalse(out["published"])
        except pub.PublishRefused as exc:
            reason = str(exc)
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before,
                         "the live manifest must be byte-identical after a refusal")
        return reason

    def test_an_edited_layout_is_refused(self):
        def m(seg, manifest):
            seg["layout"] = dict(seg["layout"], spatial_chunk=16)
        self.assertIn("layout", self._edited(m))

    def test_an_edited_variable_list_is_refused(self):
        """Two layers again. The segment list and the top-level list must BOTH be edited to
        stay schema-legal, and the top-level one is copied from the live manifest, so the
        re-composition catches it. The block-level `variables` check is defence in depth and
        is tested where it can be reached."""
        def m(seg, manifest):
            seg["variables"] = ["sst"]
            manifest["variables"] = ["sst"]
        # The composition itself rejects it: the top-level list is rebuilt from the live
        # manifest's segments, so an edited segment list makes generation N+1 uncomposable.
        self.assertIn("cannot be composed", self._edited(m))

        block = self._block("probe", self.span[:10])
        seg = _seg("probe", "probe.zarr", self.s0, self.e0, 10, unknown=self.span[10:],
                   sealed=False, block_path=block)
        seg["variables"] = ["sst"]
        with self.assertRaises(pub.PublishRefused) as cm:
            pub.bind_segment_to_block(seg, {d: {} for d in self.span[:10]}, block,
                                      {"ny": 32, "nx": 32, "region": [0, 32, 0, 32]},
                                      where="probe")
        self.assertIn("variables", str(cm.exception))

    def test_an_edited_metadata_fingerprint_is_refused(self):
        def m(seg, manifest):
            seg["fingerprint"] = dict(seg["fingerprint"], metadata="0" * 64)
        self.assertIn("fingerprint.metadata", self._edited(m))

    def test_an_edited_day_digest_is_refused(self):
        def m(seg, manifest):
            seg["fingerprint"] = dict(seg["fingerprint"], day_digest="0" * 64)
        self.assertIn("fingerprint.day_digest", self._edited(m))

    def test_day_count_is_bound_too_although_an_earlier_guard_usually_wins(self):
        """`day_count` cannot be edited in isolation and stay schema-legal -- changing it means
        changing `gaps`/`unknown`, which changes the declared present days, which the source-map
        coverage check catches first. So the publication path refuses either way; the binding
        arm itself is defence in depth and is tested where it can actually be reached."""
        block = self._block("probe", self.span[:10])
        seg = _seg("probe", "probe.zarr", self.s0, self.e0, 10, unknown=self.span[10:],
                   sealed=False, block_path=block)
        seg["day_count"] = 9
        with self.assertRaises(pub.PublishRefused) as cm:
            pub.bind_segment_to_block(seg, {d: {} for d in self.span[:10]}, block,
                                      {"ny": 32, "nx": 32, "region": [0, 32, 0, 32]},
                                      where="probe")
        self.assertIn("day_count", str(cm.exception))

        def m(seg, manifest):
            seg["day_count"] = 59
            seg["gaps"] = [self.span[0]]
        self.assertIn("never checks", self._edited(m))       # the earlier guard, still a refusal

    def test_a_grid_that_does_not_match_the_block_is_refused(self):
        """Two layers, and it is worth being explicit about which one fires.

        A grid edited in the plan is caught by the re-composition, because `grid` is copied
        from the live manifest and cannot legitimately change. The block-level grid check is
        for the case re-composition cannot see -- a live manifest whose grid genuinely does not
        match the block -- so it is tested directly."""
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        plan["manifest"]["grid"] = {"ny": 64, "nx": 64, "region": [0, 64, 0, 64]}
        plan["manifest"]["manifest_checksum"] = ""
        plan["manifest"]["manifest_checksum"] = bm.compute_checksum(plan["manifest"])
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
        self.assertEqual(out["status"], "refused")
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

        block = self._block("probe", self.span[:10])
        seg = _seg("probe", "probe.zarr", self.s0, self.e0, 10, unknown=self.span[10:],
                   sealed=False, block_path=block)
        with self.assertRaises(pub.PublishRefused) as cm:
            pub.bind_segment_to_block(seg, {d: {} for d in self.span[:10]}, block,
                                      {"ny": 64, "nx": 64, "region": [0, 64, 0, 64]},
                                      where="probe")
        self.assertIn("manifest grid declares", str(cm.exception))

    def test_a_carried_forward_entry_that_was_edited_is_refused(self):
        """Published blocks are immutable, so their entries must be too -- and re-inspecting
        every segment on every publish would be disk work for no new information."""
        src = self._plan_for_v2(self.delta, self.span[:60])
        src["segment"]["supersedes"] = None                    # keep b_v1 in `segments`
        src["segment"]["start_day"], src["segment"]["end_day"] = bm.block_bounds(ANCHOR, S, 1)
        s1, e1 = src["segment"]["start_day"], src["segment"]["end_day"]
        days = fx.calendar_span(s1, e1)[:60]
        shutil.rmtree(src["out_path"])
        fx.build_block(src["out_path"], days)
        src["segment"] = _seg("b_v2", "b_v2.zarr", s1, e1, 60, unknown=fx.calendar_span(s1, e1)[60:],
                              sealed=False, precedence=1, block_path=src["out_path"],
                              provenance={"sources": self._smap(self.delta, days),
                                          "materialized_repairs": {},
                                          "source_map_digest": "sha256:test"})
        src["source_map"] = src["segment"]["build_provenance"]["sources"]
        fx.build_delta(self.delta, days)
        plan = pub.plan_publication(self.root, src, now=T0)
        old = next(s for s in plan["manifest"]["segments"] if s["segment_id"] == "b_v1")
        old["variables"] = ["sst"]                             # edit the CARRIED entry
        plan["manifest"]["manifest_checksum"] = ""
        plan["manifest"]["manifest_checksum"] = bm.compute_checksum(plan["manifest"])
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
        self.assertEqual(out["status"], "refused")
        self.assertIn("live generation produces", out["reason"])
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)


class TestSourceKindIsCheckedOnItsOwnTerms(_Base):
    """One branch for every non-delta kind made `source_kind` almost decorative."""

    def setUp(self):
        super().setUp()
        self._live_gen1()
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.span[:60])
        self.daily = os.path.join(self.tmp, "mur.zarr")
        fx.build_daily(self.daily, self.span[:60])

    def _run(self, smap):
        plan = pub.plan_publication(
            self.root, self._plan_for_v2(self.delta, self.span[:60], smap=smap), now=T0)
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                      now=T0)
        if out.get("published"):
            return out
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)
        return out

    def test_a_block_source_pointing_at_a_daily_root_is_refused(self):
        smap = self._smap(self.delta, self.span[:60], kind="block", root=self.daily)
        out = self._run(smap)
        self.assertEqual(out["status"], "aborted_stale")
        self.assertIn("not a block published in the live manifest", out["reason"])

    def test_a_block_source_naming_an_UNPUBLISHED_block_is_refused(self):
        stray = os.path.join(self.tmp, "stray.zarr")
        fx.build_block(stray, self.span[:60])
        out = self._run(self._smap(self.delta, self.span[:60], kind="block", root=stray))
        self.assertEqual(out["status"], "aborted_stale")
        self.assertIn("not a block published", out["reason"])

    def test_a_block_source_with_the_WRONG_index_is_refused(self):
        published = os.path.join(self.root, "b_v1.zarr")
        smap = {d: {"source_kind": "block", "source_path": os.path.realpath(published),
                    "source_day_index": i + 7} for i, d in enumerate(self.span[:30])}
        plan = pub.plan_publication(
            self.root, self._plan_for_v2(self.delta, self.span[:30], smap=smap), now=T0)
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                      now=T0)
        self.assertEqual(out["status"], "aborted_stale")
        self.assertIn("holds this day at index", out["reason"])

    def test_a_block_source_that_IS_published_at_the_right_index_passes(self):
        published = os.path.join(self.root, "b_v1.zarr")
        smap = {d: {"source_kind": "block", "source_path": os.path.realpath(published),
                    "source_day_index": i} for i, d in enumerate(self.span[:30])}
        plan = pub.plan_publication(
            self.root, self._plan_for_v2(self.delta, self.span[:30], smap=smap), now=T0)
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                      now=T0)
        self.assertEqual(out["status"], "published")

    def test_a_daily_source_with_a_nonzero_index_is_refused(self):
        smap = {d: {"source_kind": "daily", "source_path": os.path.realpath(self.daily),
                    "source_day_index": i} for i, d in enumerate(self.span[:60])}
        out = self._run(smap)
        self.assertEqual(out["status"], "aborted_stale")
        self.assertIn("index 0 is the only meaningful value", out["reason"])

    def test_a_hold_source_with_index_zero_passes(self):
        hold = os.path.join(self.tmp, "hold_daily")
        fx.build_daily(hold, self.span[:60])
        smap = {d: {"source_kind": "hold", "source_path": os.path.realpath(hold),
                    "source_day_index": 0} for d in self.span[:60]}
        self.assertEqual(self._run(smap)["status"], "published")

    def test_a_block_source_refuses_when_no_resolver_is_supplied(self):
        """An unverifiable claim is not a weaker claim; it is no claim."""
        smap = {self.span[0]: {"source_kind": "block", "source_path": "/somewhere/b.zarr",
                               "source_day_index": 0}}
        out = pub.staleness_guard(smap, delta_path=None, live_days=[])
        self.assertEqual(out["status"], "aborted_stale")
        self.assertIn("cannot be checked", out["reason"])


# ============ review round 6: the manifest must be the one THIS live generation produces
class TestSegmentSetIsDerivedNotAccepted(_Base):
    """Round 5 compared the carried-forward entries that were still *present*, which says
    nothing about an entry that was REMOVED or one that was INJECTED. Enumerating what may
    change requires enumerating every way a document can be wrong; re-composing asks the one
    question with a definite answer."""

    def setUp(self):
        super().setUp()
        # a live generation with TWO segments, so "drop one" is expressible
        self.b0 = self._block("b_v1", self.span[:30])
        s1, e1 = bm.block_bounds(ANCHOR, S, 1)
        self.span1 = fx.calendar_span(s1, e1)
        self.other = self._block("b_other", self.span1[:20])
        seg0 = _seg("b_v1", "b_v1.zarr", self.s0, self.e0, 30, unknown=self.span[30:],
                    sealed=False, block_path=self.b0)
        seg1 = _seg("b_other", "b_other.zarr", s1, e1, 20, unknown=self.span1[20:],
                    sealed=False, precedence=1, block_path=self.other)
        bm.publish(self.root, self._manifest([seg0, seg1]))
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.span[:60])

    def _plan(self):
        return pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)

    def _refuse(self, plan):
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
        self.assertFalse(out["published"])
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before,
                         "the live manifest must be byte-identical after a refusal")
        return out["reason"]

    def _reseal(self, plan):
        plan["manifest"]["manifest_checksum"] = ""
        plan["manifest"]["manifest_checksum"] = bm.compute_checksum(plan["manifest"])
        bm.validate_manifest(plan["manifest"])       # precondition: still schema-legal
        return plan

    def test_removing_a_segment_that_is_NOT_the_superseded_one_is_refused(self):
        """The case the review named: history quietly dropped, checksum recomputed, schema
        still valid."""
        plan = self._plan()
        plan["manifest"]["segments"] = [s for s in plan["manifest"]["segments"]
                                        if s["segment_id"] != "b_other"]
        plan["manifest"]["variables"] = sorted(
            {v for s in plan["manifest"]["segments"] for v in s["variables"]})
        reason = self._refuse(self._reseal(plan))
        self.assertIn("removed: ['b_other']", reason)

    def test_injecting_an_extra_segment_is_refused(self):
        s2, e2 = bm.block_bounds(ANCHOR, S, 2)
        span2 = fx.calendar_span(s2, e2)
        extra_path = self._block("b_extra", span2[:10])
        plan = self._plan()
        plan["manifest"]["segments"].append(
            _seg("b_extra", "b_extra.zarr", s2, e2, 10, unknown=span2[10:], sealed=False,
                 precedence=2, block_path=extra_path))
        reason = self._refuse(self._reseal(plan))
        self.assertIn("injected: ['b_extra']", reason)

    def test_an_edited_superseded_list_is_refused(self):
        plan = self._plan()
        plan["manifest"]["superseded"] = []
        reason = self._refuse(self._reseal(plan))
        self.assertIn("superseded list", reason)

    def test_an_edited_generation_number_is_refused(self):
        plan = self._plan()
        plan["manifest"]["generation"] = 7
        plan["predecessor_generation"] = 1
        reason = self._refuse(self._reseal(plan))
        self.assertIn("differs from the one this live generation produces", reason)

    def test_an_edited_predecessor_pointer_is_refused(self):
        plan = self._plan()
        plan["manifest"]["predecessor_manifest"] = bm.archive_name(99)
        reason = self._refuse(self._reseal(plan))
        self.assertIn("predecessor_manifest", reason)

    def test_a_superseded_entry_pointed_at_a_different_block_is_refused(self):
        plan = self._plan()
        entry = next(e for e in plan["manifest"]["superseded"]
                     if e["segment_id"] == "b_v1")
        entry["current_path"] = "b_other.zarr"
        reason = self._refuse(self._reseal(plan))
        self.assertIn("differs", reason)

    def test_the_UNEDITED_plan_still_publishes_with_both_segments_intact(self):
        """The re-composition must not reject the legitimate case."""
        out = _execute(self._plan(), ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
        self.assertEqual(out["status"], "published")
        live = bm.load_live(self.root)
        self.assertEqual(sorted(s["segment_id"] for s in live["segments"]),
                         ["b_other", "b_v2"])
        self.assertEqual([e["segment_id"] for e in live["superseded"]], ["b_v1"])

    def test_created_by_is_the_only_value_that_crosses_from_the_plan(self):
        """Everything else is re-derived from the live manifest, regenerated at publication, or
        measured from disk, so editing it changes nothing about what gets committed."""
        plan = self._plan()
        plan["manifest"]["generation_id"] = "some-other-uuid"
        plan["manifest"]["created_utc"] = "2099-01-01T00:00:00Z"
        plan["manifest"]["created_by"] = "someone-else"
        entry = next(e for e in plan["manifest"]["superseded"] if e["segment_id"] == "b_v1")
        entry["bytes"] = 999
        out = _execute(self._reseal(plan), ingest_lock_path=self.lock, delta_path=self.delta,
                       now=T0)
        self.assertEqual(out["status"], "published")
        live = bm.load_live(self.root)
        self.assertEqual(live["created_by"], "someone-else")
        self.assertNotEqual(live["generation_id"], "some-other-uuid")
        self.assertEqual(live["created_utc"], "2027-01-01T00:00:00Z")
        self.assertNotEqual(live["superseded"][0]["bytes"], 999,
                            "bytes is measured from the block, not taken from the plan")
        self.assertGreater(live["superseded"][0]["bytes"], 0)


# ============ review round 7: lifecycle deadlines are COMPUTED at publication, never accepted
class TestLifecycleDeadlinesAreNotAcceptedFromThePlan(_Base):
    """`release_after_utc` and `hold_until_utc` are what keep a superseded block available to
    in-flight snapshots, to the next fold, and to rollback. A plan that could set them into the
    past would shorten or erase that window and let ops hard-delete the block early."""

    def setUp(self):
        super().setUp()
        self._live_gen1()
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.span[:60])

    def _plan(self, **kw):
        return pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0, **kw)

    def _entry(self):
        return next(e for e in bm.load_live(self.root)["superseded"]
                    if e["segment_id"] == "b_v1")

    def test_back_dated_deadlines_in_the_plan_do_not_reach_the_manifest(self):
        plan = self._plan()
        entry = next(e for e in plan["manifest"]["superseded"] if e["segment_id"] == "b_v1")
        entry["release_after_utc"] = "1999-01-01T00:00:00Z"
        entry["hold_until_utc"] = "1999-01-02T00:00:00Z"
        plan["manifest"]["manifest_checksum"] = ""
        plan["manifest"]["manifest_checksum"] = bm.compute_checksum(plan["manifest"])
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
        self.assertEqual(out["status"], "published")
        got = self._entry()
        self.assertEqual(got["release_after_utc"], "2027-01-02T00:00:00Z")
        self.assertEqual(got["hold_until_utc"], "2027-01-15T00:00:00Z")

    def test_a_back_dated_plan_does_not_release_the_block_early(self):
        """The consequence, not just the field: the lifecycle must still refuse to release."""
        plan = self._plan()
        entry = next(e for e in plan["manifest"]["superseded"] if e["segment_id"] == "b_v1")
        entry["release_after_utc"] = "1999-01-01T00:00:00Z"
        entry["hold_until_utc"] = "1999-01-02T00:00:00Z"
        plan["manifest"]["manifest_checksum"] = ""
        plan["manifest"]["manifest_checksum"] = bm.compute_checksum(plan["manifest"])
        _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
        out = pub.advance_lifecycle(self.root, hold_root=self.hold, now=T0)
        self.assertEqual(out["transitions"], [], "the block must still be protected")
        self.assertIn("release_after_utc", out["blocked"][0]["reason"])

    def test_the_deadlines_come_from_the_PUBLICATION_clock_not_the_planning_clock(self):
        """The window protects from the moment of publication. A plan reviewed for a day and
        then published must still get a full grace period -- which a lower-bound check on the
        plan's values would have refused outright."""
        plan = self._plan()
        later = T0 + timedelta(days=3)
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=later)
        self.assertEqual(out["status"], "published")
        got = self._entry()
        self.assertEqual(got["release_after_utc"], "2027-01-05T00:00:00Z")
        self.assertEqual(got["hold_until_utc"], "2027-01-18T00:00:00Z")
        self.assertGreater(got["hold_until_utc"], got["release_after_utc"])

    def test_the_configured_policy_is_what_is_applied(self):
        out = _execute(self._plan(), ingest_lock_path=self.lock, delta_path=self.delta,
                       now=T0, release_after_s=7200, hold_days=30)
        self.assertEqual(out["status"], "published")
        got = self._entry()
        self.assertEqual(got["release_after_utc"], "2027-01-01T02:00:00Z")
        self.assertEqual(got["hold_until_utc"], "2027-01-31T00:00:00Z")


class TestAuditReflectsWhatWasCommitted(_Base):
    """An audit trail claiming generation 999 while the manifest says 2 is worse than no trail,
    because it is believed."""

    def setUp(self):
        super().setUp()
        self._live_gen1()
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.span[:60])

    def _log(self):
        with open(os.path.join(self.root, pub.MANIFEST_LOG)) as fh:
            return json.loads(fh.read().splitlines()[-1])

    def test_edited_plan_metadata_does_not_reach_the_audit_log(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        plan["generation"] = 999                       # top-level plan metadata, not the manifest
        plan["superseded_now"] = "b_imaginary"
        plan["predecessor_generation"] = 1             # still matches live, so we get past the gate
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
        self.assertEqual(out["status"], "published")
        self.assertEqual(out["generation"], 2)
        self.assertEqual(out["superseded"], "b_v1")
        entry = self._log()
        self.assertEqual(entry["to_generation"], 2)
        self.assertEqual(entry["from_generation"], 1)
        self.assertEqual(entry["superseded"], "b_v1")
        self.assertEqual(bm.load_live(self.root)["generation"], 2)

    def test_the_log_matches_the_manifest_on_a_normal_publish(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
        live = bm.load_live(self.root)
        entry = self._log()
        self.assertEqual(entry["to_generation"], live["generation"])
        self.assertEqual(entry["new_segment"], live["segments"][0]["segment_id"])
        self.assertEqual(out["generation"], live["generation"])


# ================= review round 8: the retention POLICY itself, and measured predecessor bytes
class TestRetentionPolicyIsValidated(_Base):
    """Round 7 stopped the plan back-dating the deadlines by computing them from these two
    numbers. If the numbers can be negative or zero, the protection is back where it started,
    one level down."""

    def setUp(self):
        super().setUp()
        self._live_gen1()
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.span[:60])

    def _plan(self):
        return pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)

    def _refused(self, **policy):
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        with self.assertRaises(pub.PublishRefused) as cm:
            _execute(self._plan(), ingest_lock_path=self.lock, delta_path=self.delta, now=T0,
                     **policy)
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)
        return str(cm.exception)

    def test_a_negative_release_window_is_refused(self):
        self.assertIn("release_after_s must be >= 0", self._refused(release_after_s=-1))

    def test_a_zero_or_negative_hold_window_is_refused(self):
        for bad in (0, -1):
            with self.subTest(hold_days=bad):
                self.assertIn("hold_days must be > 0", self._refused(hold_days=bad))

    def test_a_hold_window_shorter_than_the_release_window_is_refused(self):
        """It reverses the lifecycle: eligible for hard delete before eligible to leave
        `referenced`."""
        msg = self._refused(release_after_s=5 * 86400, hold_days=1)
        self.assertIn("not longer than the release window", msg)

    def test_a_hold_window_EQUAL_to_the_release_window_is_refused(self):
        """At equality the block becomes releasable and hard-deletable at the same instant,
        which collapses the two stages into one."""
        msg = self._refused(release_after_s=86400, hold_days=1)
        self.assertIn("collapses the lifecycle", msg)

    def test_one_second_under_the_release_window_still_refuses(self):
        self.assertIn("collapses the lifecycle",
                      self._refused(release_after_s=86400 + 1, hold_days=1))

    def test_non_integer_policy_values_are_refused(self):
        for kw in ({"release_after_s": "3600"}, {"hold_days": 14.0},
                   {"hold_days": True}, {"release_after_s": None}):
            with self.subTest(**kw):
                self.assertIn("must be an int", self._refused(**kw))

    def test_a_zero_release_window_is_allowed_but_a_zero_hold_window_is_not(self):
        """`release_after_s=0` is a deliberate, legal choice (publish and release immediately);
        `hold_days=0` is not, because `held` is terminal."""
        out = _execute(self._plan(), ingest_lock_path=self.lock, delta_path=self.delta, now=T0,
                       release_after_s=0)
        self.assertEqual(out["status"], "published")
        entry = bm.load_live(self.root)["superseded"][0]
        self.assertEqual(entry["release_after_utc"], "2027-01-01T00:00:00Z")
        self.assertGreater(entry["hold_until_utc"], entry["release_after_utc"])

    def test_the_policy_is_checked_before_the_lock_is_taken(self):
        """A caller error should not stall the delta append while it is discovered."""
        import threading
        holder = open(self.lock, "w")
        try:
            fcntl.flock(holder, fcntl.LOCK_EX)
            done = threading.Event()

            def worker():
                try:
                    _execute(self._plan(), ingest_lock_path=self.lock, delta_path=self.delta,
                             now=T0, hold_days=0)
                except pub.PublishRefused:
                    pass
                finally:
                    done.set()

            threading.Thread(target=worker, daemon=True).start()
            self.assertTrue(done.wait(10),
                            "the policy check must refuse without waiting for the lock")
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()


class TestPredecessorBytesAreMeasured(_Base):
    def setUp(self):
        super().setUp()
        self._live_gen1()
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.span[:60])

    def _publish(self, plan_src=None):
        src = plan_src or self._plan_for_v2(self.delta, self.span[:60])
        plan = pub.plan_publication(self.root, src, now=T0)
        return _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)

    def test_bytes_reflect_the_block_on_disk_not_the_plans_claim(self):
        src = self._plan_for_v2(self.delta, self.span[:60])
        src["predecessor_bytes"] = 1                       # a wildly wrong claim
        self.assertEqual(self._publish(src)["status"], "published")
        actual = pub.measure_bytes(os.path.join(self.root, "b_v1.zarr"))
        self.assertEqual(bm.load_live(self.root)["superseded"][0]["bytes"], actual)
        self.assertGreater(actual, 1)

    def test_a_negative_claimed_size_never_reaches_the_manifest(self):
        src = self._plan_for_v2(self.delta, self.span[:60])
        plan = pub.plan_publication(self.root, src, now=T0)
        entry = next(e for e in plan["manifest"]["superseded"] if e["segment_id"] == "b_v1")
        entry["bytes"] = -(1 << 40)
        plan["manifest"]["manifest_checksum"] = ""
        plan["manifest"]["manifest_checksum"] = bm.compute_checksum(plan["manifest"])
        out = _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
        self.assertEqual(out["status"], "published")
        self.assertGreater(bm.load_live(self.root)["superseded"][0]["bytes"], 0)

    def test_compose_generation_refuses_a_negative_size_outright(self):
        """The fallback path sanitises; the composer refuses. A negative size feeds the §7.1c
        pinned_existing forecast and would report free space that does not exist."""
        live = bm.load_live(self.root)
        seg = _seg("b_v9", "b_v9.zarr", self.s0, self.e0, 30, unknown=self.span[30:],
                   sealed=False, supersedes="b_v1")
        with self.assertRaises(pub.PublishRefused) as cm:
            pub.compose_generation(live, seg, now=T0, predecessor_bytes=-5)
        self.assertIn("non-negative int", str(cm.exception))

    def test_publish_refuses_when_a_still_referenced_predecessor_block_is_gone(self):
        """The predecessor is still an active segment of live -- THIS publication is what moves
        it to hold -- so its block must be on disk here. If it is gone we refuse and leave the
        manifest byte-unchanged, rather than sizing a hold entry off the plan's unverified claim
        (round 9). An absent-but-still-referenced block is data loss, not a legitimate release."""
        src = self._plan_for_v2(self.delta, self.span[:60])
        plan = pub.plan_publication(self.root, src, now=T0)
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        shutil.rmtree(os.path.join(self.root, "b_v1.zarr"))    # nothing left to measure
        with self.assertRaises(pub.PublishRefused) as cm:
            _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
        self.assertIn("not present", str(cm.exception))
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_publish_refuses_when_the_predecessor_cannot_be_measured_in_full(self):
        """Distinct from "gone": the block is there but part of it cannot be read. A short
        count would size the hold entry and the §7.1c forecast below the real block, so the
        measurement error is translated into a refusal rather than a smaller number."""
        if os.geteuid() == 0:
            self.skipTest("root bypasses directory permissions")
        src = self._plan_for_v2(self.delta, self.span[:60])
        plan = pub.plan_publication(self.root, src, now=T0)
        inner = os.path.join(self.root, "b_v1.zarr", "sst")
        mode = os.stat(inner).st_mode
        os.chmod(inner, 0o000)
        self.addCleanup(os.chmod, inner, mode)
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        with self.assertRaises(pub.PublishRefused) as cm:
            _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
        self.assertIn("could not be measured in full", str(cm.exception))
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_a_positive_plan_claim_cannot_mask_a_missing_predecessor_block(self):
        """Even a plausible positive byte claim does not license publishing a hold entry for a
        predecessor whose block is gone: the claim is not evidence (round 9)."""
        src = self._plan_for_v2(self.delta, self.span[:60])
        plan = pub.plan_publication(self.root, src, now=T0)
        entry = next(e for e in plan["manifest"]["superseded"] if e["segment_id"] == "b_v1")
        entry["bytes"] = 4242
        plan["manifest"]["manifest_checksum"] = ""
        plan["manifest"]["manifest_checksum"] = bm.compute_checksum(plan["manifest"])
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        shutil.rmtree(os.path.join(self.root, "b_v1.zarr"))
        with self.assertRaises(pub.PublishRefused):
            _execute(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_measure_bytes_returns_None_for_a_missing_path(self):
        self.assertIsNone(pub.measure_bytes(os.path.join(self.tmp, "nope.zarr")))

    def test_measure_bytes_sums_the_whole_tree(self):
        path = self._block("probe", self.span[:5])
        total = pub.measure_bytes(path)
        by_hand = sum(os.lstat(os.path.join(d, f)).st_size
                      for d, _dirs, files in os.walk(path) for f in files)
        self.assertEqual(total, by_hand)
        self.assertGreater(total, 0)

    def test_measure_bytes_fails_closed_on_an_untraversable_SUBDIRECTORY(self):
        """`os.walk` swallows traversal errors unless given an `onerror` hook, so a permission
        error on a sub-group is skipped before `lstat` is ever reached -- a different path from
        the unreadable-member case below, and one that under-counts far more."""
        if os.geteuid() == 0:
            self.skipTest("root bypasses directory permissions")
        path = self._block("probe", self.span[:5])
        inner = os.path.join(path, "sst")
        self.assertTrue(os.path.isdir(inner), "precondition: the block has sub-groups")
        mode = os.stat(inner).st_mode
        os.chmod(inner, 0o000)
        self.addCleanup(os.chmod, inner, mode)
        with self.assertRaises(OSError):
            pub.measure_bytes(path)

    def test_recompose_refuses_a_predecessor_with_no_measured_size(self):
        """Reached only by calling `recompose` directly -- publication always measures first.
        It exists so a future caller that forgets gets a refusal rather than a plausible zero,
        and it is tested here rather than left as an untestable claim."""
        src = self._plan_for_v2(self.delta, self.span[:60])
        plan = pub.plan_publication(self.root, src, now=T0)
        live = bm.load_live(self.root)
        manifest, replaced, diff = pub.recompose(live, plan, now=T0, measured_bytes=None)
        self.assertIsNone(manifest)
        self.assertIn("no measured size was supplied", diff)
        manifest, replaced, diff = pub.recompose(live, plan, now=T0, measured_bytes=123)
        self.assertIsNone(diff)
        self.assertEqual(replaced["segment_id"], "b_v1")

    def test_measure_bytes_fails_closed_on_an_unreadable_member(self):
        """An I/O or permission error on a member must PROPAGATE, not be swallowed into a short
        count (round 9). A partial sum feeds the §7.1c forecast and the hold accounting a number
        smaller than the block, reporting free space that does not exist."""
        from unittest import mock
        path = self._block("probe", self.span[:5])

        def boom(_p, *_a, **_k):
            raise OSError(5, "Input/output error")            # EIO on a real member

        with mock.patch.object(pub.os, "lstat", side_effect=boom):
            with self.assertRaises(OSError):
                pub.measure_bytes(path)
