"""dev2026 — P5-S4: atomic manifest publish, superseded lifecycle, rollback, repair WAL.

Gate (design spec §14 P5-S4): a publication either commits generation `N+1` atomically or
leaves `manifest.json` **byte-unchanged**; no superseded block is ever released while something
can still read it; rollback restores before it repoints; and the repair WAL fails closed on
every integrity defect rather than skipping a line.

The WAL is the load-bearing piece. It is the **only** authorizing evidence for dropping a
repaired day, so most of these tests are about what it refuses, not what it permits.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

import zarr

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import p5_fixtures as fx  # noqa: E402
from ingest import publish_manifest as pub  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store import repair_wal as rw  # noqa: E402

ANCHOR = "2026-06-27"
S = 90
T0 = datetime(2027, 1, 1, tzinfo=timezone.utc)


def _seg(sid, path, start, end, day_count, *, gaps=(), unknown=(), precedence=0,
         day_digest="d", sealed=True, supersedes=None, provenance=None):
    known = [d for d in fx.calendar_span(start, end) if d not in set(unknown)]
    return {
        "segment_id": sid, "kind": "block", "path": path, "immutable": True,
        "boundary_kind": "calendar", "start_day": start, "end_day": end,
        "materialized_through": max(known) if known else start, "day_count": day_count,
        "gaps": list(gaps), "unknown": list(unknown), "day_list": None,
        "layout": {"time_chunk": day_count, "spatial_chunk": 8,
                   "shard": [day_count, 32, 32]},
        "variables": list(fx.VARS),
        "fingerprint": {"algo": "sha256", "metadata": "m", "day_digest": day_digest},
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
        if with_block:
            self._block("b_v1", days)
        seg = _seg("b_v1", "b_v1.zarr", self.s0, self.e0, 30, unknown=self.span[30:],
                   sealed=False, day_digest=bm.day_digest(days))
        bm.publish(self.root, self._manifest([seg]))
        return seg, days

    def _plan_for_v2(self, delta_path, days_v2):
        """A `build_block`-shaped plan for a v2 that supersedes v1."""
        path = self._block("b_v2", days_v2)
        seg = _seg("b_v2", "b_v2.zarr", self.s0, self.e0, len(days_v2),
                   unknown=self.span[len(days_v2):], sealed=False, supersedes="b_v1",
                   day_digest=bm.day_digest(days_v2))
        smap = {d: {"source_kind": "delta", "source_path": os.path.realpath(delta_path),
                    "source_day_index": i} for i, d in enumerate(days_v2)}
        return {"segment": seg, "out_path": path, "source_map": smap,
                "predecessor_bytes": 1234}


# ==================================================================== WAL: serialization
class TestWalSerialization(_Base):
    def test_checksum_covers_the_record_with_the_field_emptied_not_absent(self):
        rec = {"seq": 1, "repair_id": "r", "day": "2026-08-11", "record": rw.INTENT,
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
        rw.append(self.tmp, record=rw.INTENT, repair_id="r1", day=self.span[0],
                  at_utc="t", operator="o", payload={"expected_vars": ["sst"]})
        rw.append(self.tmp, record=rw.COMMITTED, repair_id="r1", day=self.span[0],
                  at_utc="t", operator="o", payload={"fingerprint": "fp1"})
        with open(os.path.join(self.tmp, rw.WAL_NAME)) as fh:
            lines = fh.read().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual([json.loads(x)["seq"] for x in lines], [1, 2])
        self.assertEqual(json.loads(lines[0])["prev_checksum"], None)
        self.assertEqual(json.loads(lines[1])["prev_checksum"],
                         json.loads(lines[0])["record_checksum"])

    def test_seq_is_allocated_under_the_lock_and_is_gap_free(self):
        for i in range(5):
            rw.append(self.tmp, record=rw.INTENT, repair_id=f"r{i}", day=self.span[i],
                      at_utc="t", operator="o", payload={})
        st = rw.read_wal(self.tmp)
        self.assertEqual([r["seq"] for r in st.records], [1, 2, 3, 4, 5])
        self.assertEqual(st.last_seq, 5)


# ==================================================================== WAL: fail-closed parsing
class TestWalFailsClosed(_Base):
    def _wal(self):
        return os.path.join(self.tmp, rw.WAL_NAME)

    def _seed(self, n=2):
        rw.append(self.tmp, record=rw.INTENT, repair_id="r1", day=self.span[0],
                  at_utc="t", operator="o", payload={})
        if n > 1:
            rw.append(self.tmp, record=rw.COMMITTED, repair_id="r1", day=self.span[0],
                      at_utc="t", operator="o", payload={"fingerprint": "fp1"})

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
        rec = {"seq": 3, "repair_id": "r2", "day": self.span[5], "record": rw.INTENT,
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
        rec = {"seq": 3, "repair_id": "r3", "day": self.span[5], "record": rw.INTENT,
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
        rw.append(self.tmp, record=rw.INTENT, repair_id="r2", day=self.span[5],
                  at_utc="t", operator="o", payload={})
        rw.append(self.tmp, record=rw.ABORTED, repair_id="r2", day=self.span[5],
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
            rw.append(self.tmp, record=rw.INTENT, repair_id="r9", day=self.span[9],
                      at_utc="t", operator="o", payload={})
        self.assertEqual(self._read(self._wal()), before,
                         "a refused append must leave the WAL byte-unchanged")


# ==================================================================== WAL: state machine
class TestWalStateMachine(_Base):
    def _intent(self, rid, day):
        rw.append(self.tmp, record=rw.INTENT, repair_id=rid, day=day, at_utc="t",
                  operator="o", payload={})

    def test_an_open_intent_blocks_its_day_with_no_timeout(self):
        self._intent("r1", self.span[0])
        st = rw.read_wal(self.tmp)
        self.assertIn(self.span[0], st.blocked_days)
        out = rw.prune_authorization(st, [self.span[0], self.span[1]])
        self.assertEqual(out["authorized"], [self.span[1]])
        self.assertIn("no timeout", out["refused"][self.span[0]])

    def test_an_identical_terminal_replay_is_a_no_op(self):
        """The expected retry after an uncertain fsync."""
        self._intent("r1", self.span[0])
        rw.append(self.tmp, record=rw.COMMITTED, repair_id="r1", day=self.span[0],
                  at_utc="t", operator="o", payload={"fingerprint": "fp1"})
        rw.append(self.tmp, record=rw.COMMITTED, repair_id="r1", day=self.span[0],
                  at_utc="t2", operator="o", payload={"fingerprint": "fp1"})
        st = rw.read_wal(self.tmp)
        self.assertEqual(st.last_seq, 3, "the replay still gets its own seq")
        self.assertEqual(st.repairs["r1"].state, rw.COMMITTED)
        self.assertNotIn(self.span[0], st.poisoned_days)

    def test_a_committed_then_aborted_conflict_poisons_that_day_only(self):
        self._intent("r1", self.span[0])
        rw.append(self.tmp, record=rw.COMMITTED, repair_id="r1", day=self.span[0],
                  at_utc="t", operator="o", payload={"fingerprint": "fp1"})
        self._intent("r2", self.span[5])
        with self.assertRaises(rw.WalError):
            rw.append(self.tmp, record=rw.ABORTED, repair_id="r1", day=self.span[0],
                      at_utc="t", operator="o", payload={"reason": "x"})
        st = rw.read_wal(self.tmp)
        self.assertEqual(st.repairs["r2"].state, "open", "other repairs are unaffected")

    def test_a_second_commit_with_a_DIFFERENT_fingerprint_is_invalid(self):
        self._intent("r1", self.span[0])
        rw.append(self.tmp, record=rw.COMMITTED, repair_id="r1", day=self.span[0],
                  at_utc="t", operator="o", payload={"fingerprint": "fp1"})
        with self.assertRaises(rw.WalError):
            rw.append(self.tmp, record=rw.COMMITTED, repair_id="r1", day=self.span[0],
                      at_utc="t", operator="o", payload={"fingerprint": "fp2"})

    def test_a_terminal_without_an_intent_is_refused(self):
        with self.assertRaises(rw.WalError) as cm:
            rw.append(self.tmp, record=rw.COMMITTED, repair_id="ghost", day=self.span[0],
                      at_utc="t", operator="o", payload={"fingerprint": "fp"})
        self.assertIn("no matching repair_intent", str(cm.exception))

    def test_a_repair_id_cannot_be_reused_for_a_different_day(self):
        self._intent("r1", self.span[0])
        with self.assertRaises(rw.WalError):
            rw.append(self.tmp, record=rw.COMMITTED, repair_id="r1", day=self.span[1],
                      at_utc="t", operator="o", payload={"fingerprint": "fp"})

    def test_an_aborted_repair_stops_blocking(self):
        self._intent("r1", self.span[0])
        rw.append(self.tmp, record=rw.ABORTED, repair_id="r1", day=self.span[0],
                  at_utc="t", operator="o", payload={"reason": "never landed"})
        st = rw.read_wal(self.tmp)
        self.assertNotIn(self.span[0], st.blocked_days)
        self.assertEqual(rw.prune_authorization(st, [self.span[0]])["authorized"],
                         [self.span[0]])


# ==================================================================== WAL: the prune gate (E1)
class TestPruneGateIsIdentityBased(_Base):
    def _committed(self, rid, day, fp):
        rw.append(self.tmp, record=rw.INTENT, repair_id=rid, day=day, at_utc="t",
                  operator="o", payload={})
        rw.append(self.tmp, record=rw.COMMITTED, repair_id=rid, day=day, at_utc="t",
                  operator="o", payload={"fingerprint": fp})

    def test_a_never_repaired_day_is_authorized(self):
        st = rw.read_wal(self.tmp)
        self.assertEqual(rw.prune_authorization(st, [self.span[0]])["authorized"],
                         [self.span[0]])

    def test_a_committed_repair_with_no_materialized_entry_is_refused(self):
        """Crash boundary 3: committed, corrective refold not published."""
        self._committed("r1", self.span[0], "fp1")
        out = rw.prune_authorization(rw.read_wal(self.tmp), [self.span[0]])
        self.assertEqual(out["authorized"], [])
        self.assertIn("has not been folded into base", out["refused"][self.span[0]])

    def test_an_OLDER_materialized_repair_is_refused(self):
        """Crash boundary 4: a later repair supersedes what the block carries."""
        self._committed("r1", self.span[0], "fp1")
        self._committed("r2", self.span[0], "fp2")
        mr = {self.span[0]: {"repair_id": "r1", "source_kind": "delta",
                             "source_fingerprint": "fp1"}}
        out = rw.prune_authorization(rw.read_wal(self.tmp), [self.span[0]],
                                     materialized_repairs=mr)
        self.assertEqual(out["authorized"], [])
        self.assertIn("supersedes", out["refused"][self.span[0]])

    def test_the_right_id_with_the_WRONG_fingerprint_is_refused(self):
        self._committed("r1", self.span[0], "fp1")
        mr = {self.span[0]: {"repair_id": "r1", "source_kind": "delta",
                             "source_fingerprint": "WRONG"}}
        out = rw.prune_authorization(rw.read_wal(self.tmp), [self.span[0]],
                                     materialized_repairs=mr)
        self.assertEqual(out["authorized"], [])
        self.assertIn("fingerprint", out["refused"][self.span[0]])

    def test_matching_id_and_fingerprint_authorizes(self):
        self._committed("r1", self.span[0], "fp1")
        mr = {self.span[0]: {"repair_id": "r1", "source_kind": "delta",
                             "source_fingerprint": "fp1"}}
        out = rw.prune_authorization(rw.read_wal(self.tmp), [self.span[0]],
                                     materialized_repairs=mr)
        self.assertEqual(out["authorized"], [self.span[0]])

    def test_a_LATER_block_carrying_the_wrong_id_is_refused_like_a_stale_one(self):
        """Round-6, stated behaviourally: ordering is not identity.

        Clock evidence would authorize this -- everything about the block postdates the repair.
        Only the id disagrees, and the id is what decides."""
        self._committed("r1", self.span[0], "fp1")
        mr = {self.span[0]: {"repair_id": "r0-earlier", "source_kind": "delta",
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
            rw.append(root, record=rw.INTENT, repair_id="r1", day=self.span[0],
                      at_utc=when, operator="o", payload={})
            rw.append(root, record=rw.COMMITTED, repair_id="r1", day=self.span[0],
                      at_utc=when, operator="o", payload={"fingerprint": "fp1"})
            return rw.read_wal(root)

        early = build(os.path.join(self.tmp, "a"), "1999-01-01T00:00:00Z")
        late = build(os.path.join(self.tmp, "b"), "2099-01-01T00:00:00Z")
        mr = {self.span[0]: {"repair_id": "r1", "source_kind": "delta",
                             "source_fingerprint": "fp1"}}
        for state in (early, late):
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
        out = pub.execute_publication(plan, ingest_lock_path=self.lock,
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
        pub.execute_publication(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
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
        out = pub.execute_publication(plan, ingest_lock_path=self.lock, delta_path=pruned,
                                      now=T0)
        self.assertEqual(out["status"], "aborted_stale")
        self.assertFalse(out["published"])
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)

    def test_a_duplicated_delta_day_aborts_stale(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        g = zarr.open_group(self.delta, mode="a")
        g.attrs["days"] = list(g.attrs["days"]) + [self.span[0]]
        out = pub.execute_publication(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                      now=T0)
        self.assertEqual(out["status"], "aborted_stale")
        self.assertIn("duplicate", out["reason"])

    def test_a_manifest_that_moved_under_the_plan_aborts_stale(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        other = self._manifest([self.v1_seg], generation=2)
        bm.publish(self.root, other)                           # someone else published first
        out = pub.execute_publication(plan, ingest_lock_path=self.lock, delta_path=self.delta,
                                      now=T0)
        self.assertEqual(out["status"], "aborted_stale")
        self.assertIn("moved while this plan was held", out["reason"])

    def test_publishing_a_generation_whose_block_is_missing_is_refused(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        shutil.rmtree(os.path.join(self.root, "b_v2.zarr"))
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        out = pub.execute_publication(plan, ingest_lock_path=self.lock, delta_path=self.delta,
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
        from store.compaction_lock import CompactionLock
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        clock = os.path.join(self.tmp, "p5_compaction.lock")
        with CompactionLock(clock):
            out = pub.execute_publication(plan, ingest_lock_path=self.lock,
                                          delta_path=self.delta,
                                          compaction_lock_path=clock, now=T0)
        self.assertEqual(out["reason"], "compaction_lock_held")
        self.assertFalse(out["published"])

    def test_the_generation_archive_is_never_rewritten(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        pub.execute_publication(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)
        with self.assertRaises(bm.ManifestError) as cm:
            bm.publish(self.root, plan["manifest"])
        self.assertIn("immutable", str(cm.exception))

    def test_publication_appends_an_audit_line(self):
        plan = pub.plan_publication(self.root, self._plan_for_v2(self.delta, self.span[:60]),
                                    now=T0)
        pub.execute_publication(plan, ingest_lock_path=self.lock, delta_path=self.delta,
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
        pub.execute_publication(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)

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
        out = pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True)
        self.assertEqual(out["transitions"][0]["to"], "releasable")
        self.assertEqual(bm.load_live(self.root)["superseded"][0]["status"], "releasable")

    def test_releasable_advances_to_held_by_RENAME_and_the_bytes_survive(self):
        self._gen2()
        later = T0 + timedelta(seconds=pub.DEFAULT_RELEASE_AFTER_S + 1)
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True)
        out = pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True)
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
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True)
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True)
        out = pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True)
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
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True)
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True)
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
        pub.execute_publication(plan, ingest_lock_path=self.lock, delta_path=self.delta, now=T0)

    def _to_hold(self):
        later = T0 + timedelta(seconds=pub.DEFAULT_RELEASE_AFTER_S + 1)
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True)
        pub.advance_lifecycle(self.root, hold_root=self.hold, now=later, apply=True)

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
        rw.append(self.tmp, record=rw.INTENT, repair_id="r1", day=self.span[0],
                  at_utc="t", operator="o", payload={})
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
        rw.append(self.tmp, record=rw.INTENT, repair_id="r1", day=self.span[0],
                  at_utc="t", operator="o", payload={})
        plan = pub.plan_publication(self.root, self._plan_for_v2(delta, self.span[:60]), now=T0)
        out = pub.execute_publication(plan, ingest_lock_path=self.lock, delta_path=delta,
                                      now=T0)
        self.assertEqual(out["status"], "published")
        self.assertIn("UNCHANGED", out["note"])


if __name__ == "__main__":
    unittest.main()
