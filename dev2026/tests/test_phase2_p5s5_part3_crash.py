"""dev2026 — P5-S5 Part 3: every §9 crash boundary, injected (§9, §9.3, §9.3a, §9.4).

## How the crashes are made

Each case runs the **real** §7.8 corrective refold — which runs the real §7.4
`execute_publication`, which commits at the real `os.replace` — in a **child process** that is
killed with `os._exit(9)` at a named boundary. `os._exit` runs no cleanup, flushes no buffer and
unwinds no `finally`, so what the parent then finds on disk is what a power cut would have left:
only what was actually `fsync`'d.

There is no `sleep` and no race. The boundary is chosen by name, reached deterministically, and
the process is gone the instant it arrives.

The parent asserts on **ground truth** — the bytes and directory entries under the manifest root
— never on anything the dead child reported, since a crashed process's report is exactly what is
not available in production either.

## What each case must establish

For every boundary the test states, and asserts:

1. **what may be durable** — which files can exist afterwards;
2. **what is served** — which generation a fresh reader resolves;
3. **the verdict** — `clean`, `committed_unlogged`, `orphan_archive`, or `fail_closed`;
4. **what a retry does** — and, for the two repairable verdicts, that repairing twice is not
   worse than repairing once.

An ambiguous state must never resolve itself into success. That is the property most of these
tests exist to hold onto.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ingest import publish_manifest as pub  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store.segmented_cube import SegmentedCubeStore  # noqa: E402

_TESTS = os.path.dirname(os.path.abspath(__file__))
_DEV2026 = os.path.dirname(_TESTS)

#: Boundaries, in the order the publication reaches them. The names are the §9 rows.
BEFORE_PUBLISH = "before_publish"                     # nothing written
AFTER_ARCHIVE = "after_archive"                       # gen archive fsync'd, pointer untouched
AFTER_TMP = "after_tmp"                               # temp pointer written, not yet replaced
AFTER_REPLACE = "after_replace"                       # committed; dir fsync + log not done
AFTER_PUBLISH_BEFORE_LOG = "after_publish_before_log"  # committed, unlogged
AFTER_LOG = "after_log"                               # complete: durability evidence
ROLLBACK_AFTER_TMP = "rollback_after_tmp"             # §9.3 step 3 done, step 4 not
ROLLBACK_AFTER_REPLACE = "rollback_after_replace"     # §9.3 step 4 done, step 6 not
BOUNDARIES = (BEFORE_PUBLISH, AFTER_ARCHIVE, AFTER_TMP, AFTER_REPLACE,
              AFTER_PUBLISH_BEFORE_LOG, AFTER_LOG,
              ROLLBACK_AFTER_TMP, ROLLBACK_AFTER_REPLACE)

CRASH_EXIT = 9


# ----------------------------------------------------------------- the child process
def _child_main() -> int:
    """Run a real refold in this process and die at the named boundary.

    Patching happens HERE, in a throwaway process, so nothing in the shipped code knows about
    crash injection. The fixture is P5-S5 Part 3's own `_Base`, so the publication under test is
    the same one every other Part 3 test exercises."""
    tmp, point = sys.argv[2], sys.argv[3]

    # `_Base.setUp` builds its fixture in `tempfile.mkdtemp()`; pin that to the directory the
    # parent handed us so the parent can inspect the wreckage.
    tempfile.mkdtemp = lambda *a, **k: tmp                       # type: ignore[assignment]

    import test_phase2_p5s5_part3 as p3

    case = p3.TestTheFourStates("test_1_old_base_old_delta_is_safe")
    case.setUp()
    # `_Base` publishes generation 1 with `bm.publish` directly, which writes no audit line --
    # so without this the baseline is itself `committed_unlogged` and every verdict below is
    # measured from a broken starting point. Generation 1 is logged here, as a normal
    # publication would have logged it.
    pub._append_log(case.root, pub.MANIFEST_LOG, {
        "op": "manifest_publish", "at_utc": "2027-01-01T00:00:00Z", "operator": "fixture",
        "from_generation": None, "to_generation": 1,
        "new_segment": case.v1_id, "superseded": None})
    case._repair()

    def die():
        sys.stdout.flush()
        os._exit(CRASH_EXIT)

    if point == BEFORE_PUBLISH:
        real_publish = bm.publish

        def publish(root, manifest):
            die()
        bm.publish = publish
    elif point in (AFTER_ARCHIVE, AFTER_TMP):
        real_write = bm._write_fsync
        needle = "manifest.gen" if point == AFTER_ARCHIVE else ".tmp-"

        def write(path, text):
            real_write(path, text)
            if needle in os.path.basename(path):
                die()
        bm._write_fsync = write
    elif point == AFTER_REPLACE:
        real_replace = os.replace

        def replace(src, dst, **kw):
            real_replace(src, dst, **kw)
            if os.path.basename(str(dst)) == bm.LIVE_NAME:
                die()                                  # committed; no dir fsync, no log
        os.replace = replace
    elif point == AFTER_PUBLISH_BEFORE_LOG:
        real_log = pub._append_log

        def log(root, name, entry):
            if name == pub.MANIFEST_LOG:
                die()                                  # committed, unlogged
            real_log(root, name, entry)
        pub._append_log = log
    elif point == AFTER_LOG:
        real_log = pub._append_log

        def log(root, name, entry):
            real_log(root, name, entry)
            if name == pub.MANIFEST_LOG:
                die()                    # the line has RETURNED from fsync -- is it durable?
        pub._append_log = log
    elif point in (ROLLBACK_AFTER_TMP, ROLLBACK_AFTER_REPLACE):
        pass                                           # patched after the refold succeeds
    else:
        raise SystemExit(f"unknown crash point {point!r}")

    try:
        case._refold()
    except BaseException as exc:                       # a refusal is a legitimate outcome
        print(f"child finished without crashing: {type(exc).__name__}: {exc}")
        return 0

    if point in (ROLLBACK_AFTER_TMP, ROLLBACK_AFTER_REPLACE):
        # generation 2 is live and logged; now crash inside the §9.3 rollback to generation 1
        if point == ROLLBACK_AFTER_TMP:
            real_copy = bm.shutil.copyfile

            def copyfile(src, dst, **kw):
                real_copy(src, dst, **kw)
                die()                                  # temp written, replace not reached
            bm.shutil.copyfile = copyfile
        else:
            real_replace = os.replace

            def replace(src, dst, **kw):
                real_replace(src, dst, **kw)
                if os.path.basename(str(dst)) == bm.LIVE_NAME:
                    die()                              # rolled back, unlogged
            os.replace = replace
        pub.execute_rollback(case.root, 1, hold_root=os.path.join(case.tmp, "hold"),
                             ingest_lock_path=case.lock, operator="ops", reason="crash test")
    print("child finished without crashing")
    return 0


def _crash_at(point: str) -> str:
    """Run a publication in a child that dies at `point`. Returns the fixture root."""
    tmp = tempfile.mkdtemp(prefix=f"p5s5crash-{point}-")
    proc = subprocess.run(
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0, {_TESTS!r}); "
         f"import test_phase2_p5s5_part3_crash as m; raise SystemExit(m._child_main())",
         "--child", tmp, point],
        cwd=_DEV2026, capture_output=True, text=True)
    if proc.returncode != CRASH_EXIT:
        raise AssertionError(
            f"child did not crash at {point!r} (rc={proc.returncode}).\n"
            f"stdout: {proc.stdout[-2000:]}\nstderr: {proc.stderr[-2000:]}")
    return tmp


# ----------------------------------------------------------------- parent-side inspection
class _CrashCase(unittest.TestCase):
    """Ground truth only: what is on disk, and what a fresh reader resolves from it."""

    POINT = ""

    @classmethod
    def setUpClass(cls):
        if not cls.POINT:
            raise unittest.SkipTest("base class")
        cls.crashed = _crash_at(cls.POINT)          # the crash runs ONCE: it is expensive

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "crashed", None):
            shutil.rmtree(cls.crashed, ignore_errors=True)

    def setUp(self):
        """Every test gets its OWN copy of the wreckage.

        Sharing one directory across the class let the repair tests mutate the state the verdict
        tests then measured, so two of them passed for the previous test's reason. Recovery
        tests mutate by definition; the fixture has to be per-test."""
        self.tmp = tempfile.mkdtemp(prefix="p5s5crash-copy-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        shutil.copytree(self.crashed, self.tmp, dirs_exist_ok=True, symlinks=True)

    # ---- helpers -----------------------------------------------------
    def _apply(self, **kw):
        """Applying always goes through the ingest lock, as production must."""
        return pub.reconcile_publication(
            self.root, operator="ops", apply=True,
            ingest_lock_path=os.path.join(self.tmp, "ingest.lock"), **kw)

    @property
    def root(self):
        return os.path.join(self.tmp, "manifest_root")

    def live_generation(self):
        return int(bm.load_live(self.root)["generation"])

    def archives(self):
        return sorted(int(n[len("manifest.gen"):-len(".json")])
                      for n in os.listdir(self.root)
                      if n.startswith("manifest.gen") and n.endswith(".json"))

    def temp_pointers(self):
        return sorted(n for n in os.listdir(self.root) if n.startswith("." + bm.LIVE_NAME))

    def log_generations(self):
        path = os.path.join(self.root, pub.MANIFEST_LOG)
        if not os.path.isfile(path):
            return []
        with open(path) as fh:
            return [json.loads(l)["to_generation"] for l in fh if l.strip()]

    def served_generation(self):
        """What a process starting now would serve — the only definition that matters."""
        return SegmentedCubeStore(self.root).generation

    def assert_live_manifest_is_whole(self):
        """The pointer is never torn: it parses, checksums, and names exactly one generation."""
        live = bm.load_live(self.root)
        bm.validate_manifest(live)
        self.assertEqual(live["manifest_checksum"], bm.compute_checksum(
            {**live, "manifest_checksum": ""}))


class TestCrashBeforePublish(_CrashCase):
    """§9 "after validation, before the generation file is written": nothing was written."""
    POINT = BEFORE_PUBLISH

    def test_nothing_was_written_and_generation_1_still_serves(self):
        self.assertEqual(self.archives(), [1])
        self.assertEqual(self.temp_pointers(), [])
        self.assert_live_manifest_is_whole()
        self.assertEqual(self.live_generation(), 1)
        self.assertEqual(self.served_generation(), 1)

    def test_the_verdict_is_clean_and_a_retry_is_an_ordinary_publication(self):
        self.assertEqual(pub.reconcile_publication(self.root)["verdict"], pub.RECOVERY_CLEAN)


class TestCrashAfterArchiveBeforePointer(_CrashCase):
    """§9 "after `manifest.gen<N+1>.json` write, before `os.replace`": the new generation file
    exists but is **unreferenced**."""
    POINT = AFTER_ARCHIVE

    def test_the_archive_is_durable_and_the_pointer_is_untouched(self):
        self.assertEqual(self.archives(), [1, 2], "the archive should have been fsync'd")
        self.assert_live_manifest_is_whole()
        self.assertEqual(self.live_generation(), 1)
        self.assertEqual(self.served_generation(), 1,
                         "an unreferenced archive must change nothing that is served")

    def test_the_verdict_is_orphan_archive_and_naming_it_is_not_acting_on_it(self):
        r = pub.reconcile_publication(self.root)
        self.assertEqual(r["verdict"], pub.RECOVERY_ORPHAN_ARCHIVE)
        self.assertEqual(r["orphan_generations"], [2])
        self.assertFalse(r["applied"], "classification must not mutate anything")
        self.assertEqual(self.live_generation(), 1)


class TestCrashAfterTempPointerBeforeReplace(_CrashCase):
    """The temp pointer is written but the atomic rename has not happened."""
    POINT = AFTER_TMP

    def test_the_temp_pointer_is_not_the_live_one(self):
        self.assertEqual(self.archives(), [1, 2])
        self.assertTrue(self.temp_pointers(), "the temp pointer should be on disk")
        self.assert_live_manifest_is_whole()
        self.assertEqual(self.live_generation(), 1)
        self.assertEqual(self.served_generation(), 1)

    def test_a_stray_temp_pointer_does_not_confuse_the_reader_or_the_reconciler(self):
        """`.manifest.json.tmp-2` is not a generation archive and must not be read as one."""
        self.assertEqual(pub.reconcile_publication(self.root)["verdict"],
                         pub.RECOVERY_ORPHAN_ARCHIVE)
        self.assertEqual(self.served_generation(), 1)


class TestCrashAfterReplace(_CrashCase):
    """The commit point has passed: generation 2 is live even though the directory was never
    fsync'd and the log line was never written."""
    POINT = AFTER_REPLACE

    def test_the_publication_is_committed_and_served(self):
        self.assert_live_manifest_is_whole()
        self.assertEqual(self.live_generation(), 2)
        self.assertEqual(self.served_generation(), 2,
                         "`os.replace` is the commit point; after it the new view is the truth")
        self.assertEqual(self.archives(), [1, 2])

    def test_the_predecessor_archive_survives_the_commit(self):
        """Rollback needs it, and `os.replace` on the archive would have consumed it."""
        with open(os.path.join(self.root, bm.archive_name(1))) as fh:
            self.assertEqual(int(json.load(fh)["generation"]), 1)


class TestCommittedButUnlogged(_CrashCase):
    """The state the whole authority rule exists for: the manifest says the publication
    happened, the audit log does not."""
    POINT = AFTER_PUBLISH_BEFORE_LOG

    def test_the_manifest_says_it_happened_and_the_log_does_not(self):
        self.assertEqual(self.live_generation(), 2)
        self.assertEqual(self.served_generation(), 2)
        self.assertEqual(self.archives(), [1, 2])
        self.assertNotIn(2, self.log_generations(),
                         "this case is only meaningful if the log really is missing it")

    def test_the_verdict_is_committed_unlogged(self):
        r = pub.reconcile_publication(self.root)
        self.assertEqual(r["verdict"], pub.RECOVERY_COMMITTED_UNLOGGED)
        self.assertFalse(r["applied"])

    def test_repair_reconstructs_the_line_and_says_so(self):
        r = self._apply()
        self.assertTrue(r["applied"])
        with open(os.path.join(self.root, pub.MANIFEST_LOG)) as fh:
            lines = [json.loads(l) for l in fh if l.strip()]
        rec = [l for l in lines if l["to_generation"] == 2]
        self.assertEqual(len(rec), 1)
        self.assertTrue(rec[0]["reconstructed"],
                        "an inference must never be recorded as an observation")
        self.assertIn("already committed", rec[0]["evidence"])
        self.assertEqual(self.live_generation(), 2, "repairing the LOG must not touch the state")

    def test_repairing_twice_does_not_double_log(self):
        self._apply()
        self._apply()
        self._apply()
        self.assertEqual(self.log_generations().count(2), 1)
        self.assertEqual(pub.reconcile_publication(self.root)["verdict"], pub.RECOVERY_CLEAN)


class TestOrphanArchiveRetrySafety(_CrashCase):
    """Completing an interrupted publication, and proving the retry cannot double anything."""
    POINT = AFTER_ARCHIVE

    def test_apply_completes_the_publication_without_consuming_the_archive(self):
        r = self._apply(confirm_orphan_generation=2)
        self.assertTrue(r["applied"])
        self.assertEqual(self.live_generation(), 2)
        self.assertEqual(self.served_generation(), 2)
        # §9.3's rule, applied forwards: copy the archive, never rename it away.
        self.assertEqual(self.archives(), [1, 2])
        self.assertEqual(pub.reconcile_publication(self.root)["verdict"], pub.RECOVERY_CLEAN)

    def test_the_predecessor_is_not_lost(self):
        self._apply(confirm_orphan_generation=2)
        live = bm.load_live(self.root)
        self.assertEqual(live["predecessor_generation"], 1)
        with open(os.path.join(self.root, bm.archive_name(1))) as fh:
            self.assertEqual(int(json.load(fh)["generation"]), 1)

    def test_completing_twice_is_not_worse_than_completing_once(self):
        self._apply(confirm_orphan_generation=2)
        before = self._read(os.path.join(self.root, bm.LIVE_NAME))
        self._apply(confirm_orphan_generation=2)
        self._apply(confirm_orphan_generation=2)
        self.assertEqual(self._read(os.path.join(self.root, bm.LIVE_NAME)), before)
        self.assertEqual(self.log_generations().count(2), 1)

    def test_republishing_the_same_generation_is_REFUSED_not_repeated(self):
        """The archive is immutable. A retry that rewrote it would erase the record of what was
        actually published, which is the one thing recovery reads."""
        self._apply(confirm_orphan_generation=2)
        live = bm.load_live(self.root)
        with self.assertRaises(bm.ManifestError) as cm:
            bm.publish(self.root, live)
        self.assertIn("already exists", str(cm.exception))
        self.assertEqual(self.archives(), [1, 2])

    def _read(self, path):
        with open(path, "rb") as fh:
            return fh.read()


class TestAmbiguousStatesFailClosed(_CrashCase):
    """§9.4. Every state that is not provably one of the two repairable ones. Enumerated, so
    that "we did not think of it" cannot come out as success."""
    POINT = AFTER_ARCHIVE

    def _verdict(self):
        return self._apply(confirm_orphan_generation=2)

    def test_a_log_claiming_a_generation_that_never_happened(self):
        """The direction the authority rule forbids: the log must not create a publication."""
        pub._append_log(self.root, pub.MANIFEST_LOG, {
            "op": "manifest_publish", "at_utc": "t", "operator": "x",
            "from_generation": 1, "to_generation": 7, "new_segment": "b", "superseded": None})
        r = self._verdict()
        self.assertEqual(r["verdict"], pub.RECOVERY_FAIL_CLOSED)
        self.assertIn("does not authorize", " ".join(r["reasons"]))
        self.assertFalse(r["applied"])
        self.assertEqual(self.live_generation(), 1, "nothing may be published on the log's word")

    def test_an_unparsable_log_tail(self):
        with open(os.path.join(self.root, pub.MANIFEST_LOG), "a") as fh:
            fh.write('{"op": "manifest_pub')            # a torn final write
        r = self._verdict()
        self.assertEqual(r["verdict"], pub.RECOVERY_FAIL_CLOSED)
        self.assertIn("unparsable", " ".join(r["reasons"]))
        self.assertFalse(r["applied"])

    def test_a_live_generation_with_no_archive(self):
        os.remove(os.path.join(self.root, bm.archive_name(1)))
        r = self._verdict()
        self.assertEqual(r["verdict"], pub.RECOVERY_FAIL_CLOSED)
        self.assertIn("no archive", " ".join(r["reasons"]))

    def test_an_unreadable_live_manifest(self):
        with open(os.path.join(self.root, bm.LIVE_NAME), "w") as fh:
            fh.write("{ this is not json")
        r = self._verdict()
        self.assertEqual(r["verdict"], pub.RECOVERY_FAIL_CLOSED)
        self.assertIn("unusable", " ".join(r["reasons"]))

    def test_a_live_manifest_whose_checksum_does_not_match(self):
        live = bm.load_live(self.root)
        live["created_by"] = "tampered"                 # checksum now stale
        with open(os.path.join(self.root, bm.LIVE_NAME), "w") as fh:
            json.dump(live, fh)
        r = self._verdict()
        self.assertEqual(r["verdict"], pub.RECOVERY_FAIL_CLOSED)

    def test_an_orphan_that_would_SKIP_a_generation(self):
        """Two generations were archived and neither committed; completing the newer would jump
        the live pointer over the older one.

        `validate_manifest` already refuses `predecessor >= generation`, so the only way to
        reach this arm is a genuinely higher orphan -- which is also the only way it happens in
        production: two interrupted publications in a row."""
        with open(os.path.join(self.root, bm.archive_name(2))) as fh:
            m = json.load(fh)
        m["generation"] = 3
        m["generation_id"] = "gen000003-x"
        m["predecessor_generation"] = 2
        m["predecessor_manifest"] = bm.archive_name(2)
        m["manifest_checksum"] = bm.compute_checksum({**m, "manifest_checksum": ""})
        with open(os.path.join(self.root, bm.archive_name(3)), "w") as fh:
            json.dump(m, fh)
        r = self._verdict()
        self.assertEqual(r["verdict"], pub.RECOVERY_FAIL_CLOSED)
        self.assertIn("skip a generation", " ".join(r["reasons"]))
        self.assertEqual(self.live_generation(), 1)

    def test_an_orphan_whose_block_is_not_on_disk(self):
        """Publishing it would make every snapshot build fail closed -- the §9.3a hazard, in the
        forward direction."""
        with open(os.path.join(self.root, bm.archive_name(2))) as fh:
            seg = json.load(fh)["segments"][-1]
        shutil.rmtree(os.path.join(self.root, os.path.basename(seg["path"])))
        r = self._verdict()
        self.assertEqual(r["verdict"], pub.RECOVERY_FAIL_CLOSED)
        self.assertIn("not on disk", " ".join(r["reasons"]))
        self.assertEqual(self.live_generation(), 1)

    def test_an_orphan_archive_that_does_not_validate(self):
        with open(os.path.join(self.root, bm.archive_name(2)), "w") as fh:
            fh.write("{}")
        r = self._verdict()
        self.assertEqual(r["verdict"], pub.RECOVERY_FAIL_CLOSED)
        self.assertEqual(self.live_generation(), 1)


class TestTheAuditLineIsReallyDurable(_CrashCase):
    """The durability claim, with crash evidence behind it rather than an `fsync` call read in
    the source. The process is killed the instant `_append_log` returns."""
    POINT = AFTER_LOG

    def test_the_line_survives_a_kill_immediately_after_the_write_returns(self):
        self.assertIn(2, self.log_generations(),
                      "the audit line was not durable: `fsync` returned and the record is gone")
        self.assertEqual(self.live_generation(), 2)
        self.assertEqual(pub.reconcile_publication(self.root)["verdict"], pub.RECOVERY_CLEAN)


class TestCrashDuringRollbackBeforeReplace(_CrashCase):
    """§9.3 steps 1–3 done, step 4 not: a verified temp file exists and the pointer is
    untouched."""
    POINT = ROLLBACK_AFTER_TMP

    def test_generation_2_is_still_live_and_the_archive_is_intact(self):
        self.assert_live_manifest_is_whole()
        self.assertEqual(self.live_generation(), 2)
        self.assertEqual(self.served_generation(), 2)
        self.assertEqual(self.archives(), [1, 2],
                         "the rollback must never consume the archive it copies")
        self.assertEqual(pub.reconcile_publication(self.root)["verdict"], pub.RECOVERY_CLEAN)

    def test_a_stray_rollback_temp_is_not_mistaken_for_a_generation(self):
        strays = [n for n in os.listdir(self.root) if "rollback-tmp" in n]
        self.assertTrue(strays, "the temp copy should be on disk at this boundary")
        self.assertEqual(self.archives(), [1, 2])


class TestCrashDuringRollbackAfterReplace(_CrashCase):
    """§9.3 step 4 done, step 6 (the log line) not: the rollback committed."""
    POINT = ROLLBACK_AFTER_REPLACE

    def test_generation_1_is_live_again_and_both_archives_survive(self):
        self.assert_live_manifest_is_whole()
        self.assertEqual(self.live_generation(), 1)
        self.assertEqual(self.served_generation(), 1)
        self.assertEqual(self.archives(), [1, 2],
                         "rolling back must leave generation 2 archived for a roll-forward")

    def test_a_DELIBERATE_rollback_is_not_mistaken_for_an_interrupted_publication(self):
        """The distinction that matters, and one this reconciler originally got wrong.

        Generation 2 IS ahead of live — but because someone rolled back on purpose, not because
        a publication was interrupted. The log separates the two: here it records generation 2
        as published. Treating it as an orphan and completing it would **silently undo the
        operator's rollback**, which is the reconciler acting on an intention it does not have.
        """
        r = self._apply(confirm_orphan_generation=2)
        self.assertEqual(r["verdict"], pub.RECOVERY_CLEAN)
        self.assertEqual(r.get("rolled_back_generations"), [2])
        self.assertEqual(self.live_generation(), 1, "it must not roll itself forward")
        self.assertEqual(self.served_generation(), 1)


class TestLifecycleRetryDoesNotDoubleHold(unittest.TestCase):
    """A crash between the hold move and its log line must not become a second move on retry."""

    def setUp(self):
        self.tmp = _crash_at(AFTER_LOG)                 # a complete generation-2 publication
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "manifest_root")
        self.hold = os.path.join(self.tmp, "hold_root")
        os.makedirs(self.hold, exist_ok=True)
        self.lock = os.path.join(self.tmp, "ingest.lock")

    def _hold_log(self):
        path = os.path.join(self.root, pub.HOLD_LOG)
        if not os.path.isfile(path):
            return []
        with open(path) as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def _advance(self):
        far = pub._parse_iso("2099-01-01T00:00:00Z")
        return pub.advance_lifecycle(self.root, hold_root=self.hold, now=far, operator="ops",
                                     apply=True, ingest_lock_path=self.lock)

    def test_the_lifecycle_reaches_a_FIXED_POINT_and_stays_there(self):
        """`referenced` → `releasable` → `held` is deliberately two steps, so the first call
        promotes and a later one moves. What must not happen is a THIRD move: a crashed-and-
        retried lifecycle must not hold the same block twice, or move a block that is already
        in hold."""
        self._advance()                                  # referenced -> releasable
        self._advance()                                  # releasable -> held
        moved = [e for e in self._hold_log() if e["op"] == "hold_move"]
        self.assertTrue(moved, "the fixture must actually move something, or this proves nothing")

        for _ in range(3):                               # the retries
            self._advance()
        again = [e for e in self._hold_log() if e["op"] == "hold_move"]
        self.assertEqual(len(again), len(moved),
                         "a retry moved a block that was already held")
        for entry in moved:
            self.assertTrue(os.path.exists(entry["to"]), "the held block must still be there")
            self.assertFalse(os.path.exists(entry["from"]),
                             "a hold move is a rename, not a copy")


class TestReconcilerTakesTheIngestLock(_CrashCase):
    """Review round 8, findings 1 and 2. Classification is a statement about a moment; acting on
    a moment that has passed is how a reconciler overwrites something newer than itself."""
    POINT = AFTER_ARCHIVE

    @property
    def lock(self):
        return os.path.join(self.tmp, "ingest.lock")

    def test_applying_without_a_lock_path_is_refused(self):
        r = pub.reconcile_publication(self.root, operator="ops", apply=True)
        self.assertEqual(r["verdict"], pub.RECOVERY_FAIL_CLOSED)
        self.assertFalse(r["applied"])
        self.assertIn("ingest_lock_path is required", " ".join(r["reasons"]))
        self.assertEqual(self.live_generation(), 1, "and it must not have acted")

    def test_completing_an_orphan_requires_naming_the_generation(self):
        r = self._apply()                                # no confirmation
        self.assertEqual(r["verdict"], pub.RECOVERY_ORPHAN_ARCHIVE)
        self.assertFalse(r["applied"])
        self.assertIn("confirm_orphan_generation=2", " ".join(r["reasons"]))
        self.assertEqual(self.live_generation(), 1)

    def test_confirming_the_WRONG_generation_is_refused(self):
        r = self._apply(confirm_orphan_generation=7)
        self.assertFalse(r["applied"])
        self.assertIn("which is not it", " ".join(r["reasons"]))
        self.assertEqual(self.live_generation(), 1)

    def test_a_STALE_observation_does_not_act_on_a_state_that_moved(self):
        """The race, made deterministic by sequencing rather than by timing.

        The operator classifies (orphan, generation 2), someone else completes that publication,
        and only then does the operator apply. Inside the lock the verdict is re-derived and is
        no longer an orphan, so the confirmation refers to a state that no longer exists and
        nothing is done twice."""
        observed = pub.reconcile_publication(self.root)
        self.assertEqual(observed["verdict"], pub.RECOVERY_ORPHAN_ARCHIVE)

        self._apply(confirm_orphan_generation=2)         # the other operator gets there first
        self.assertEqual(self.live_generation(), 2)
        log_before = list(self.log_generations())

        late = self._apply(confirm_orphan_generation=2)  # the stale apply lands afterwards
        self.assertEqual(late["verdict"], pub.RECOVERY_CLEAN)
        self.assertFalse(late["applied"])
        self.assertEqual(self.log_generations(), log_before,
                         "the stale apply must not have written a second line")

    def test_a_concurrent_holder_of_the_ingest_lock_blocks_the_apply(self):
        """Ground truth, not instrumentation: while another process holds the ingest lock, the
        apply does not complete AND the state does not change. If the lock were not taken, the
        child would finish immediately and generation 2 would be live."""
        holder = subprocess.Popen(
            [sys.executable, "-c",
             "import fcntl, sys\n"
             "lk = open(sys.argv[1], 'w')\n"
             "fcntl.flock(lk, fcntl.LOCK_EX)\n"
             "print('held', flush=True)\n"
             "sys.stdin.readline()\n", self.lock],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            applier = subprocess.Popen(
                [sys.executable, "-c",
                 f"import sys; sys.path.insert(0, {_DEV2026!r}); "
                 "from ingest import publish_manifest as pub; "
                 "print(pub.reconcile_publication(sys.argv[1], operator='ops', apply=True, "
                 "ingest_lock_path=sys.argv[2], confirm_orphan_generation=2)['applied'])",
                 self.root, self.lock],
                stdout=subprocess.PIPE, text=True)
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    applier.communicate(timeout=5)
                self.assertEqual(self.live_generation(), 1,
                                 "it must not have applied while another process held the lock")
            finally:
                applier.kill()
                applier.communicate()
        finally:
            holder.kill()
            holder.communicate()

    def test_a_MISSING_root_remains_absent(self):
        """Review round 9. `apply` used to `os.makedirs(root)` before taking the lock, so a
        mistyped path left an empty directory behind and only then failed closed -- a write
        outside the very lock the apply path promises to work inside."""
        missing = os.path.join(self.tmp, "no_such_root")
        r = pub.reconcile_publication(missing, operator="ops", apply=True,
                                      ingest_lock_path=self.lock,
                                      confirm_orphan_generation=2)
        self.assertEqual(r["verdict"], pub.RECOVERY_FAIL_CLOSED)
        self.assertFalse(r["applied"])
        self.assertIn("does not create one", " ".join(r["reasons"]))
        self.assertFalse(os.path.exists(missing),
                         "recovery reconciles a deployment; it must not establish one")

    def test_a_missing_root_is_also_absent_after_a_report_only_call(self):
        missing = os.path.join(self.tmp, "no_such_root_ro")
        r = pub.reconcile_publication(missing)
        self.assertEqual(r["verdict"], pub.RECOVERY_FAIL_CLOSED)
        self.assertFalse(os.path.exists(missing))

    def test_and_it_DOES_apply_once_the_lock_is_free(self):
        """The companion the blocking test needs: otherwise 'it did not apply' would also pass
        for a reconciler that never applies at all."""
        self.assertTrue(self._apply(confirm_orphan_generation=2)["applied"])
        self.assertEqual(self.live_generation(), 2)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        raise SystemExit(_child_main())
    unittest.main()


# ============================================================ G10 / H4: snapshot cases (a)(b)(c)(e)
class _SnapshotBase(unittest.TestCase):
    """A base store over two real published generations, built once per test.

    These are the §P5-S5 (a)(b)(c)(e) cases. They read through the **base-only**
    `SegmentedCubeStore`, because H4 is a claim about the base's immutable versioned blocks, not
    about the delta-wins public view."""

    def setUp(self):
        import p5_fixtures as fx
        from ingest.build_block import build_block
        self.fx = fx
        self.tmp = tempfile.mkdtemp(prefix="p5s5snap-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "manifest_root")
        self.artifacts = os.path.join(self.tmp, "artifacts")
        for d in (self.root, self.artifacts):
            os.makedirs(d, exist_ok=True)
        self.s0, self.e0 = bm.block_bounds("2026-06-27", 90, 0)
        self.days = fx.calendar_span(self.s0, self.e0)[:6]
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.days)

        self.v1 = self._fold("b_v1", self.days)
        self._publish(self.v1["segment"], generation=1)

    def _fold(self, name, days):
        from ingest.build_block import build_block
        return build_block(os.path.join(self.root, name + ".zarr"),
                           start_day=self.s0, end_day=self.e0,
                           classification_target=list(days), delta_path=self.delta,
                           artifacts_dir=self.artifacts, lock=None, hard_reserve_bytes=0,
                           unsafe_skip_isolation=True)

    def _publish(self, segment, *, generation, supersedes=None):
        prev = None
        if generation > 1:
            prev = bm.load_live(self.root)
        segs = [dict(segment)]
        superseded = []
        if prev is not None:
            keep = [s for s in prev["segments"] if s["segment_id"] != supersedes]
            segs = keep + segs
            if supersedes:
                old = [s for s in prev["segments"] if s["segment_id"] == supersedes][0]
                superseded = [{
                    "segment_id": old["segment_id"], "path": old["path"],
                    "superseded_at_generation": generation,
                    "release_after_utc": "2027-01-08T00:00:00Z",
                    "hold_until_utc": "2027-01-22T00:00:00Z",
                    "status": "referenced", "current_path": old["path"],
                    "bytes": 1, "fingerprint": old["fingerprint"]}]
        m = {"format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION,
             "generation": generation, "generation_id": f"gen{generation:06d}-x",
             "created_utc": "2027-01-01T00:00:00Z", "created_by": "p5s5p3-crash-tests",
             "predecessor_generation": generation - 1 if generation > 1 else None,
             "predecessor_manifest": bm.archive_name(generation - 1) if generation > 1 else None,
             "grid": {"ny": 32, "nx": 32, "region": [0, 32, 0, 32]},
             "variables": list(self.fx.VARS),
             "block_grid": {"anchor_day": "2026-06-27", "block_days": 90},
             "segments": segs, "superseded": superseded, "manifest_checksum": ""}
        m["manifest_checksum"] = bm.compute_checksum(m)
        bm.publish(self.root, m)
        return m

    def _value(self, store, snap, day):
        rows = store.point_series_from(snap, 100.0, 0.0, [day], ["sst"])
        return rows[0]["sst"] if rows else None


class TestCaseA_StableOldAcrossAPublish(_SnapshotBase):
    """(a) A reader holding generation N reads **stable-old, value-asserted** while N+1 is
    published underneath it — not merely "no exception"."""

    def test_a_held_snapshot_keeps_its_VALUES_across_a_publish(self):
        store = SegmentedCubeStore(self.root)
        snap = store._meta
        before = [self._value(store, snap, d) for d in self.days]
        self.assertTrue(any(v is not None for v in before), "the fixture must serve values")

        # generation 2 rewrites the same days with different bytes, and supersedes v1
        import zarr, numpy as np
        v2 = self._fold("b_v2", self.days)
        g = zarr.open_group(os.path.join(self.root, "b_v2.zarr"), mode="a")
        g["sst"][:] = np.asarray(g["sst"][:]) + 33.0
        self._publish(v2["segment"], generation=2, supersedes=self.v1["segment"]["segment_id"])

        after = [self._value(store, snap, d) for d in self.days]
        self.assertEqual(before, after,
                         "the held snapshot must read the SAME bytes it opened, not the new ones")
        self.assertEqual(store.generation, 1, "and it must still call itself generation 1")

        store.refresh()
        refreshed = [self._value(store, store._meta, d) for d in self.days]
        self.assertNotEqual(before, refreshed, "after refresh it must see the new generation")
        self.assertEqual(store.generation, 2)


class TestCaseB_ShrinkingDaySet(_SnapshotBase):
    """(b) The S8a silent-`None` variant: generation N+1 SHRINKS the day set. The held snapshot
    must keep answering for the days it had."""

    def test_a_held_snapshot_still_answers_for_days_the_new_generation_dropped(self):
        store = SegmentedCubeStore(self.root)
        snap = store._meta
        dropped = self.days[-2:]
        before = [self._value(store, snap, d) for d in dropped]
        self.assertTrue(all(v is not None for v in before))

        v2 = self._fold("b_v2_short", self.days[:-2])
        self._publish(v2["segment"], generation=2,
                      supersedes=self.v1["segment"]["segment_id"])

        after = [self._value(store, snap, d) for d in dropped]
        self.assertEqual(before, after, "a shrinking publication must not fabricate None")
        for d in dropped:
            self.assertIn(d, snap.days)

        store.refresh()
        for d in dropped:
            self.assertNotIn(d, store.days, "after refresh the days are honestly gone")
        rows = store.point_series(100.0, 0.0, dropped, ["sst"])
        self.assertEqual(rows, [], "absent days are omitted, never returned as null values")


class TestCaseC_SupersededBlockDeletedTooEarly(_SnapshotBase):
    """(c) A block the live manifest still references is deleted. The snapshot build must FAIL
    CLOSED — never build a partial view, never fabricate `None`."""

    def test_the_snapshot_build_fails_closed(self):
        from store.segmented_cube import SnapshotError
        shutil.rmtree(os.path.join(self.root, "b_v1.zarr"))
        with self.assertRaises(SnapshotError):
            SegmentedCubeStore(self.root)

    def test_the_refresh_is_refused_and_the_old_snapshot_is_RETAINED(self):
        """§5.1: the previous snapshot object is kept and the refresh raises, so the store does
        not degrade to a manifest-less state."""
        from store.segmented_cube import SnapshotError
        store = SegmentedCubeStore(self.root)
        held = store._meta
        shutil.rmtree(os.path.join(self.root, "b_v1.zarr"))
        with self.assertRaises(SnapshotError):
            store.refresh()
        self.assertIs(store._meta, held, "the failed refresh must not have swapped the snapshot")
        self.assertEqual(store.generation, 1)

    def test_KNOWN_GAP_reads_through_a_retained_snapshot_go_silently_None(self):
        """Not a pass. Recorded because it is true.

        Retaining the snapshot OBJECT is not the same as retaining the data: the block's bytes
        are gone from disk, and Zarr opens lazily per read, so an already-open snapshot answers
        `None` for days it previously served — the fabricated-`None` outcome (c) exists to
        forbid, arriving by a route (c) does not cover.

        Nothing in the read path closes this. What prevents it is §8.5's lifecycle: a superseded
        block is `referenced` until `release_after_utc` and only moves to hold after that, and
        hard deletion is ops-only after `hold_until`. So the guarantee is operational, exactly
        like `(e2)`'s — and it is recorded as a residual risk rather than claimed as a gate."""
        store = SegmentedCubeStore(self.root)
        before = [self._value(store, store._meta, d) for d in self.days]
        self.assertTrue(all(v is not None for v in before))
        shutil.rmtree(os.path.join(self.root, "b_v1.zarr"))
        after = [self._value(store, store._meta, d) for d in self.days]
        self.assertEqual(after, [None] * len(self.days),
                         "if this ever stops being None, the read path gained a guard and this "
                         "test should become a PASS rather than a recorded gap")


class TestCaseE_ConcurrentRefreshUnderLoad(_SnapshotBase):
    """(e) Concurrent refresh + reads: no torn snapshot. Every observation must be wholly one
    generation."""

    def test_no_reader_ever_observes_a_MIX_of_generations(self):
        import threading
        import zarr, numpy as np
        v2 = self._fold("b_v2", self.days)
        g = zarr.open_group(os.path.join(self.root, "b_v2.zarr"), mode="a")
        g["sst"][:] = np.asarray(g["sst"][:]) + 33.0
        self._publish(v2["segment"], generation=2,
                      supersedes=self.v1["segment"]["segment_id"])

        store = SegmentedCubeStore(self.root)          # opens at generation 2
        gen2 = [self._value(store, store._meta, d) for d in self.days]

        # roll the live pointer back and forth under a reader loop
        seen, stop = [], threading.Event()

        def reader():
            while not stop.is_set():
                m = store._meta                        # ONE capture, as a request does
                seen.append((m.generation,
                             tuple(self._value(store, m, d) for d in self.days)))

        def flipper():
            for _ in range(40):
                bm.rollback_to(self.root, 1)
                store.refresh()
                bm.rollback_to(self.root, 2)
                store.refresh()

        r = threading.Thread(target=reader)
        r.start()
        self.addCleanup(r.join, 30)
        self.addCleanup(stop.set)
        flipper()
        stop.set()
        r.join(30)

        self.assertGreater(len(seen), 0)
        gen1 = None
        for generation, values in seen:
            if generation == 2:
                self.assertEqual(list(values), gen2, "generation 2 must read generation-2 bytes")
            else:
                self.assertEqual(generation, 1)
                if gen1 is None:
                    gen1 = values
                self.assertEqual(values, gen1, "every generation-1 observation must agree")
        self.assertIsNotNone(gen1, "the flipper never actually rolled back — nothing was tested")
        self.assertNotEqual(list(gen1), gen2)


# ============================================================ G17: build isolation + fencing
_HOLDER = """
import os, sys
sys.path.insert(0, %r)
from store.compaction_lock import CompactionLock
lock = CompactionLock(sys.argv[1], holder="p5s5-part3-holder").acquire()
print("acquired", flush=True)
sys.stdin.readline()          # park until the parent closes our stdin
""" % _DEV2026


class TestG17BuildIsolationFencing(unittest.TestCase):
    """(f) F16 / F16b — the cases P5-S5 Part 1 recorded as PARTIAL: a **killed** holder, a
    **paused** holder, and a **replaced lock file**.

    Synchronisation is by pipe: the child prints `acquired` and the parent blocks on that line.
    A blocking read is deterministic; a `sleep` would be a guess."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p5s5g17-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lock = os.path.join(self.tmp, "p5_compaction.lock")

    def _holder(self):
        """A child holding the lock, with a cleanup that actually finishes it.

        `proc.kill()` alone leaves a zombie and two unclosed pipes -- which is a
        `ResourceWarning` under the project's gate, and a real leak besides. A `SIGSTOP`ped
        child also ignores `SIGKILL` until it is continued, so the cleanup must `SIGCONT` first
        or the `wait()` never returns."""
        proc = subprocess.Popen([sys.executable, "-c", _HOLDER, self.lock],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.addCleanup(self._reap, proc)
        self.assertEqual(proc.stdout.readline().strip(), "acquired")
        return proc

    @staticmethod
    def _reap(proc):
        import signal
        if proc.poll() is None:
            try:
                proc.send_signal(signal.SIGCONT)     # a stopped child cannot be killed
            except (ProcessLookupError, OSError):
                pass
            proc.kill()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()

    def _busy(self):
        from store.compaction_lock import refuse_if_compaction_running
        return refuse_if_compaction_running(self.lock, operation="test")

    def test_a_live_holder_blocks_non_blocking(self):
        self._holder()
        refusal = self._busy()
        self.assertIsNotNone(refusal, "a running build must refuse the operation")
        self.assertEqual(refusal["reason"], "compaction_lock_held")

    def test_a_KILLED_holder_releases_the_lock(self):
        """F16: the kernel drops the `flock` when the process dies, however it dies. No stale
        lock file needs cleaning up, which is the reason a `flock` was chosen over a lockfile."""
        proc = self._holder()
        self.assertIsNotNone(self._busy())
        proc.kill()
        proc.wait(timeout=30)
        self.assertIsNone(self._busy(), "a dead holder must not block anyone")
        from store.compaction_lock import CompactionLock
        with CompactionLock(self.lock, holder="after-kill"):
            pass

    def test_a_PAUSED_holder_still_blocks(self):
        """F16b: `SIGSTOP` is the case a liveness heuristic gets wrong. The process is not
        running and will answer nothing, but it still holds the lock and may still be mid-write,
        so the operation must refuse — and must refuse WITHOUT waiting for it."""
        import signal
        proc = self._holder()
        proc.send_signal(signal.SIGSTOP)                 # `_reap` continues it before killing
        refusal = self._busy()
        self.assertIsNotNone(refusal, "a paused holder still holds the lock")
        self.assertEqual(refusal["reason"], "compaction_lock_held")

    def test_a_paused_holder_RESUMES_and_still_owns_its_lock(self):
        import signal
        proc = self._holder()
        proc.send_signal(signal.SIGSTOP)
        proc.send_signal(signal.SIGCONT)
        self.assertIsNotNone(self._busy(), "resuming must not have lost the reservation")

    def test_a_REPLACED_lock_file_makes_the_holder_refuse_to_publish(self):
        """F16b: `rm` + recreate produces a new inode. Our `flock` is on the orphaned one, so a
        second process can hold the new inode while we still believe we are exclusive. The
        publish-time inode assertion is what catches it."""
        from store.compaction_lock import CompactionLock, CompactionLockError
        guard = CompactionLock(self.lock, holder="builder").acquire()
        self.addCleanup(guard.release)
        guard.assert_still_held()                        # sanity: intact
        os.remove(self.lock)
        with open(self.lock, "w"):
            pass                                          # a fresh inode at the same path
        with self.assertRaises(CompactionLockError) as cm:
            guard.assert_still_held()
        self.assertIn("replaced while held", str(cm.exception))

    def test_the_replacement_really_does_let_a_SECOND_holder_in(self):
        """The reason the inode check matters: without it this is two writers."""
        from store.compaction_lock import CompactionLock
        guard = CompactionLock(self.lock, holder="builder").acquire()
        self.addCleanup(guard.release)
        os.remove(self.lock)
        with open(self.lock, "w"):
            pass
        second = CompactionLock(self.lock, holder="intruder").acquire()
        self.addCleanup(second.release)
        self.assertTrue(second.held, "two processes now hold 'the' compaction lock")
