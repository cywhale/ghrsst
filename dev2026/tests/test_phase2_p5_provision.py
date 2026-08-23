"""dev2026 — provisioning an ISOLATED P5 staging root, reversibly.

`p5_shadow` is `verification_only` by its own declaration: it proves a shadow is correctly
provisioned and takes no measurement. It cannot make a deployment ready, because it cannot
*create* anything — and VM24 has no `p5_blocks`, no `p5_repairs`, no anchor and no manifest for
it to verify. Calling that profile "deployment-ready" would be the same category error the whole
phase has been correcting: a check of one thing quoted as a verdict on another.

So something has to build the thing `p5_shadow` verifies. This module is that, under three
constraints that these tests exist to hold:

1. **Isolated.** It refuses to write inside a declared live root. Not "is discouraged from" —
   refuses, before creating anything.
2. **Reversible.** Everything it makes lives under one staging root, and `rollback()` removes
   exactly what was made and nothing else.
3. **No second truth.** Blocks come from `ingest.build_block`, the manifest from
   `store.block_manifest`, the WAL and anchor from `store.repair_wal`. A provisioner that
   wrote its own manifest would produce one the store might refuse.

Written before the module, and failing against an empty checkout.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
for p in (_DEV, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import p5_fixtures as fx  # noqa: E402
from ops import p5_provision as pv  # noqa: E402
from ops import p5_readiness as rd  # noqa: E402
from store import block_manifest as bm  # noqa: E402

ANCHOR = "2026-06-27"
S = 90
NY = NX = 16


class _ProvisionBase(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p5-provision-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.live_root = os.path.join(self.tmp, "live")          # stands in for $G
        self.stage_root = os.path.join(self.tmp, "stage")
        os.makedirs(self.live_root, exist_ok=True)
        s0, e0 = bm.block_bounds(ANCHOR, S, 0)
        self.s0, self.e0 = s0, e0
        self.days = fx.calendar_span(s0, e0)[:10]
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.days, ny=NY, nx=NX)

    def provision(self, **over):
        kw = dict(stage_root=self.stage_root, live_root=self.live_root,
                  delta_path=self.delta, start_day=self.s0, end_day=self.e0,
                  days=list(self.days), operator="tests",
                  at_utc="2026-08-21T13:00:00Z", grid={"ny": NY, "nx": NX})
        kw.update(over)
        return pv.provision(**kw)


class TestIsolationIsRefusedNotAdvised(_ProvisionBase):

    def test_a_stage_root_inside_the_live_root_is_refused(self):
        inside = os.path.join(self.live_root, "p5_stage")
        with self.assertRaises(pv.ProvisionRefused) as caught:
            self.provision(stage_root=inside)
        self.assertIn("live root", str(caught.exception).lower())

    def test_the_refusal_happens_before_anything_is_created(self):
        inside = os.path.join(self.live_root, "p5_stage")
        with self.assertRaises(pv.ProvisionRefused):
            self.provision(stage_root=inside)
        self.assertEqual(os.listdir(self.live_root), [],
                         "the live root was written to before the refusal")

    def test_a_stage_root_that_merely_starts_with_the_same_prefix_is_allowed(self):
        """`/data/ghrsst-stage` is not inside `/data/ghrsst`. A string prefix test would
        refuse it, and refusing a safe location teaches people to pass the override."""
        sibling = self.live_root + "-stage"
        out = self.provision(stage_root=sibling)
        self.assertTrue(os.path.isdir(out["manifest_root"]))

    def test_the_live_root_is_untouched_by_a_successful_provision(self):
        before = sorted(os.listdir(self.live_root))
        self.provision()
        self.assertEqual(sorted(os.listdir(self.live_root)), before)

    def test_an_existing_stage_root_is_refused_rather_than_merged_into(self):
        """Provisioning into a directory that already holds something makes rollback a guess."""
        os.makedirs(self.stage_root, exist_ok=True)
        with open(os.path.join(self.stage_root, "someone_elses_file"), "w") as fh:
            fh.write("x")
        with self.assertRaises(pv.ProvisionRefused):
            self.provision()

    def test_an_existing_EMPTY_stage_root_is_refused_too(self):
        """The dangerous half, and the one that was allowed.

        Only a NON-empty directory was refused, so an empty one was adopted -- and `rollback()`
        removes the whole root. That handed rollback the authority to delete a directory this
        run did not create, on the strength of it having been empty at the time. Empty is a
        statement about contents; ownership is a different statement."""
        os.makedirs(self.stage_root, exist_ok=True)
        with self.assertRaises(pv.ProvisionRefused) as caught:
            self.provision()
        self.assertIn("existing", str(caught.exception).lower())
        self.assertTrue(os.path.isdir(self.stage_root),
                        "the pre-existing directory was disturbed by a refused provision")

    def test_the_refusal_does_not_depend_on_the_directory_being_empty(self):
        """Both spellings refuse, and for the same stated reason."""
        os.makedirs(self.stage_root, exist_ok=True)
        with self.assertRaises(pv.ProvisionRefused) as empty:
            self.provision()
        shutil.rmtree(self.stage_root)
        os.makedirs(self.stage_root, exist_ok=True)
        with open(os.path.join(self.stage_root, "f"), "w") as fh:
            fh.write("x")
        with self.assertRaises(pv.ProvisionRefused) as nonempty:
            self.provision()
        self.assertEqual(str(empty.exception), str(nonempty.exception))

    def test_a_provisioned_root_records_a_real_lock_acquisition(self):
        """The provisioner takes the compaction lock in its own root and lets it go, so the
        readiness `locks` requirement rests on an acquisition rather than an assumption."""
        out = self.provision()
        probe = out["locks"]["acquire_probe"]
        self.assertIs(probe["acquired"], True)
        self.assertIs(probe["released"], True)
        status = os.path.join(out["stage_root"], "p5_compaction.lock.status")
        self.assertFalse(os.path.exists(status))


class TestWhatIsProvisioned(_ProvisionBase):

    def test_it_produces_the_roots_a_readiness_check_looks_for(self):
        out = self.provision()
        for key in ("manifest_root", "wal_root", "anchor_root", "block_path"):
            with self.subTest(key):
                self.assertTrue(os.path.exists(out[key]), key)

    def test_the_anchor_is_outside_the_manifest_root(self):
        """An anchor inside the domain it witnesses is lost with that domain."""
        out = self.provision()
        self.assertFalse(os.path.abspath(out["anchor_root"]).startswith(
            os.path.abspath(out["manifest_root"]) + os.sep))

    def test_the_result_passes_the_readiness_store_requirements(self):
        """The point of provisioning: the checks that were BLOCKED become READY."""
        import socket
        out = self.provision()
        target = {
            "label": rd.QUALIFYING_LABEL, "host": socket.gethostname(),
            # The provisioner stamps its probe with the run marker, so the target that quotes
            # it must declare the same run and sitting -- the binding working, not a bypass.
            "run_id": out["marker"], "collected_utc": out["provisioned_utc"],
            "root": out["stage_root"], "manifest_root": out["manifest_root"],
            "wal_root": out["wal_root"], "anchor_root": out["anchor_root"],
            "locks": out["locks"],
            "identity": {"reviewed_sha": "a" * 40, "head_sha": "a" * 40,
                         "worktree_clean": True, "modules": {"x": "y"}},
            "capacity": {"run_id": "r", "measured_utc": "t", "free_now_gib": 900.0,
                         "minimum_free_gib": 403.3},
        }
        by_id = {r["id"]: r for r in rd.assess(target)["requirements"]}
        for rid in ("manifest", "blocks", "wal_anchor", "locks"):
            with self.subTest(rid):
                self.assertEqual(by_id[rid]["verdict"], rd.READY, by_id[rid]["reason"])

    def test_the_manifest_is_the_one_the_store_would_accept(self):
        out = self.provision()
        live = bm.load_live(out["manifest_root"])
        bm.validate_manifest(live)
        self.assertEqual(live["generation"], 1)

    def test_it_writes_no_second_manifest_wal_or_provenance_format(self):
        with open(os.path.join(_DEV, "ops", "p5_provision.py")) as fh:
            source = fh.read()
        for forbidden in ("MANIFEST_FORMAT =", "def validate_manifest", "WAL_NAME =",
                          "jsonl.append", "def compute_checksum"):
            with self.subTest(forbidden):
                self.assertNotIn(forbidden, source)
        for required in ("block_manifest", "repair_wal", "build_block"):
            with self.subTest(required):
                self.assertIn(required, source)


class TestReversibility(_ProvisionBase):

    def test_rollback_removes_everything_it_made(self):
        out = self.provision()
        self.assertTrue(os.path.isdir(out["stage_root"]))
        pv.rollback(out)
        self.assertFalse(os.path.exists(out["stage_root"]))

    def test_rollback_refuses_a_root_inside_the_live_root(self):
        """The one call that deletes must be the most suspicious of its argument.

        The marker is planted inside the live root first, deliberately. Without it the marker
        check refuses the call and the test passes whether or not the live-root check exists --
        which is exactly what mutation testing found. The refusal has to be attributable to the
        guard under test, not to its neighbour."""
        out = self.provision()
        victim = os.path.join(self.live_root, "p5_stage")
        os.makedirs(victim, exist_ok=True)
        shutil.copy(os.path.join(out["stage_root"], pv.MARKER_NAME),
                    os.path.join(victim, pv.MARKER_NAME))
        request = dict(out, stage_root=victim)

        # Everything except the live-root check now says "go ahead".
        with self.assertRaises(pv.ProvisionRefused) as caught:
            pv.rollback(request)
        self.assertIn("live root", str(caught.exception).lower())
        self.assertTrue(os.path.isdir(victim), "a path inside the live root was deleted")

    def test_the_marker_alone_is_not_enough_to_authorize_a_delete(self):
        """Stated separately so the two guards cannot be confused for one."""
        out = self.provision()
        victim = os.path.join(self.live_root, "looks_provisioned")
        os.makedirs(victim, exist_ok=True)
        shutil.copy(os.path.join(out["stage_root"], pv.MARKER_NAME),
                    os.path.join(victim, pv.MARKER_NAME))
        with self.assertRaises(pv.ProvisionRefused):
            pv.rollback(dict(out, stage_root=victim))
        self.assertTrue(os.path.isdir(victim))

    def test_rollback_refuses_a_root_it_did_not_create(self):
        foreign = os.path.join(self.tmp, "foreign")
        os.makedirs(foreign, exist_ok=True)
        with self.assertRaises(pv.ProvisionRefused):
            pv.rollback({"stage_root": foreign, "live_root": self.live_root,
                         "marker": "not-the-one"})
        self.assertTrue(os.path.isdir(foreign))

    def test_the_marker_records_what_the_run_created(self):
        out = self.provision()
        self.assertTrue(os.path.isfile(os.path.join(out["stage_root"], pv.MARKER_NAME)))
        self.assertTrue(out["marker"])

    def test_rollback_is_idempotent(self):
        out = self.provision()
        pv.rollback(out)
        pv.rollback(out)          # already gone; must not raise


class TestItCreatesNoProductionState(_ProvisionBase):

    def test_provisioning_declares_it_is_staging_not_production(self):
        out = self.provision()
        self.assertEqual(out["label"], "P5_ISOLATED_STAGING")
        self.assertIn("STAGING", out["label"])

    def test_the_staging_label_cannot_settle_a_p5_compaction_requirement(self):
        """The trap this must not fall into: a provisioned staging root is not a deployment.
        Its label is non-qualifying, so a readiness report over it can never read READY."""
        self.assertIn(out_label := "P5_ISOLATED_STAGING", rd.NON_QUALIFYING_LABELS)
        import socket
        out = self.provision()
        report = rd.assess({"label": out_label, "host": socket.gethostname(),
                            "root": out["stage_root"], "manifest_root": out["manifest_root"],
                            "wal_root": out["wal_root"], "anchor_root": out["anchor_root"],
                            "locks": out["locks"]})
        self.assertIs(report["compaction_ready"], False)
        self.assertEqual(report["summary"][rd.READY], [])


if __name__ == "__main__":
    unittest.main()


class TestPartialStagingLeavesNoUnownedData(_ProvisionBase):
    """The marker was written LAST, so a failure anywhere before it left a populated staging
    root that `rollback()` would refuse to touch -- it carries no marker, so as far as rollback
    is concerned nobody made it. Unowned data, needing a human to delete a directory by hand
    from memory of which one was safe.

    Ownership has to be established before anything is created, and a failed provision has to
    clean up after itself or hand back something that can be.
    """

    STEPS = ("build_block", "publish", "declare_anchor_domain", "initialize_wal")

    def _break(self, step):
        """Make one provisioning step fail. Returns the restore callable.

        Deliberately NOT `addCleanup` + `doCleanups()`: `doCleanups` runs EVERY registered
        cleanup, including setUp's removal of the whole temp fixture, so a loop that used it
        destroyed its own fixture after the first iteration and the later cases failed for a
        reason that had nothing to do with the code."""

        def explode(*a, **kw):
            raise RuntimeError(f"injected failure in {step}")

        if step == "build_block":
            original, target, attr = pv.build_block, pv, "build_block"
        elif step == "publish":
            original, target, attr = pv.block_manifest.publish, pv.block_manifest, "publish"
        else:
            original, target, attr = getattr(pv.repair_wal, step), pv.repair_wal, step
        setattr(target, attr, explode)
        restore = lambda: setattr(target, attr, original)          # noqa: E731
        self.addCleanup(restore)
        return restore

    def test_a_failure_at_any_step_removes_the_staging_root(self):
        for step in self.STEPS:
            with self.subTest(step):
                restore = self._break(step)
                try:
                    with self.assertRaises(Exception) as caught:
                        self.provision()
                    self.assertIn(step, str(caught.exception))
                    self.assertFalse(os.path.exists(self.stage_root),
                                     f"a failure in {step} left the staging root behind")
                finally:
                    restore()

    def test_the_live_root_is_untouched_by_a_failed_provision(self):
        for step in self.STEPS:
            with self.subTest(step):
                before = sorted(os.listdir(self.live_root))
                restore = self._break(step)
                try:
                    with self.assertRaises(Exception):
                        self.provision()
                    self.assertEqual(sorted(os.listdir(self.live_root)), before)
                finally:
                    restore()

    def test_what_is_kept_for_diagnosis_is_owned_and_rollbackable(self):
        """Cleanup can be declined -- a failed build is worth looking at. What is left must
        still carry its marker, so a person can roll it back with the same call rather than a
        remembered path and a manual delete."""
        self._break("publish")
        with self.assertRaises(Exception):
            self.provision(cleanup_on_failure=False)
        self.assertTrue(os.path.isdir(self.stage_root))
        marker_path = os.path.join(self.stage_root, pv.MARKER_NAME)
        self.assertTrue(os.path.isfile(marker_path), "the kept root is unowned")

        with open(marker_path) as fh:
            marker = json.load(fh)
        self.assertIs(marker["complete"], False,
                      "a partial root must not look like a finished one")
        self.assertIn("publish", marker.get("failed_step", ""))
        pv.rollback(marker)
        self.assertFalse(os.path.exists(self.stage_root))

    def test_the_marker_exists_before_any_content_is_written(self):
        """The ownership stamp must precede the first thing worth owning."""
        seen = {}
        original = pv.build_block

        def note_then_build(*a, **kw):
            seen["marker_present"] = os.path.isfile(
                os.path.join(self.stage_root, pv.MARKER_NAME))
            return original(*a, **kw)

        self.addCleanup(setattr, pv, "build_block", original)
        pv.build_block = note_then_build
        self.provision()
        self.assertIs(seen["marker_present"], True,
                      "the block was built before the root was marked as ours")

    def test_a_completed_provision_marks_itself_complete(self):
        out = self.provision()
        with open(os.path.join(out["stage_root"], pv.MARKER_NAME)) as fh:
            marker = json.load(fh)
        self.assertIs(marker["complete"], True)
        self.assertIsNone(marker.get("failed_step"))

    def test_cleanup_failure_is_reported_rather_than_hidden(self):
        """If the rollback of a failed provision itself fails, the caller must be told the root
        is still there -- reporting only the build error would leave data nobody knows about."""
        self._break("publish")
        original_rollback = pv.rollback

        def refuse(*a, **kw):
            raise pv.ProvisionRefused("injected rollback failure")

        self.addCleanup(setattr, pv, "rollback", original_rollback)
        pv.rollback = refuse
        with self.assertRaises(pv.ProvisionCleanupError) as caught:
            self.provision()
        self.assertIn(self.stage_root, str(caught.exception))
        self.assertIsNotNone(caught.exception.__cause__)


class TestLockProbeFailureFailsTheProvision(_ProvisionBase):
    """The probe result was recorded and provisioning carried on. A root whose compaction lock
    cannot be taken is not a working staging root -- and it returned `complete: True`, so the
    marker asserted otherwise and `rollback()` was never reached."""

    def _break_lock(self):
        original = pv.compaction_lock.CompactionLock

        class Refuses:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                raise OSError(13, "Permission denied")

            def __exit__(self, *exc):
                return False

        self.addCleanup(setattr, pv.compaction_lock, "CompactionLock", original)
        pv.compaction_lock.CompactionLock = Refuses

    def test_a_probe_that_cannot_acquire_fails_the_provision(self):
        self._break_lock()
        with self.assertRaises(Exception) as caught:
            self.provision()
        self.assertIn("lock", str(caught.exception).lower())

    def test_it_never_returns_a_complete_root(self):
        self._break_lock()
        with self.assertRaises(Exception):
            self.provision()
        self.assertFalse(os.path.exists(self.stage_root),
                         "a root whose lock could not be taken survived as if it worked")

    def test_a_kept_failed_root_is_marked_incomplete(self):
        self._break_lock()
        with self.assertRaises(Exception):
            self.provision(cleanup_on_failure=False)
        with open(os.path.join(self.stage_root, pv.MARKER_NAME)) as fh:
            marker = json.load(fh)
        self.assertIs(marker["complete"], False)
        self.assertIn("lock", marker["failed_step"].lower())


class TestMarkerWriteFailuresAreInsideTheTransaction(_ProvisionBase):
    """The ownership stamp is itself a write, and writes fail. If the FIRST marker write fails
    the root exists and is unowned -- the very state the marker was introduced to prevent."""

    def _break_marker(self, on_call):
        original = pv._write_marker
        calls = {"n": 0}

        def maybe_explode(stage_root, payload):
            calls["n"] += 1
            if calls["n"] == on_call:
                raise OSError(28, "No space left on device")
            return original(stage_root, payload)

        self.addCleanup(setattr, pv, "_write_marker", original)
        pv._write_marker = maybe_explode
        return calls

    def test_a_failed_initial_marker_write_leaves_no_directory(self):
        self._break_marker(on_call=1)
        with self.assertRaises(Exception):
            self.provision()
        self.assertFalse(os.path.exists(self.stage_root),
                         "the root was created and left unowned when its stamp failed")

    def test_a_failed_partial_marker_write_still_cleans_up(self):
        """The second write is the one recording the failure. If it fails too, the root must
        still go -- reporting the first error and leaving the directory is the worst outcome."""
        original_build = pv.build_block

        def explode(*a, **kw):
            raise RuntimeError("injected failure in build_block")

        self.addCleanup(setattr, pv, "build_block", original_build)
        pv.build_block = explode
        self._break_marker(on_call=2)
        with self.assertRaises(Exception):
            self.provision()
        self.assertFalse(os.path.exists(self.stage_root))

    def test_the_live_root_is_untouched_by_either_marker_failure(self):
        """Restores explicitly rather than via `doCleanups()`, which runs EVERY cleanup --
        including setUp's removal of the temp fixture -- and so destroys its own fixture after
        the first iteration."""
        original_write, original_build = pv._write_marker, pv.build_block
        for call in (1, 2):
            with self.subTest(marker_write=call):
                before = sorted(os.listdir(self.live_root))
                try:
                    if call == 2:
                        def explode(*a, **kw):
                            raise RuntimeError("injected failure in build_block")
                        pv.build_block = explode
                    self._break_marker(on_call=call)
                    with self.assertRaises(Exception):
                        self.provision()
                    self.assertEqual(sorted(os.listdir(self.live_root)), before)
                finally:
                    pv._write_marker, pv.build_block = original_write, original_build


class TestRollbackOwnershipIsCanonical(_ProvisionBase):
    """The marker matched on one string, so a COPY of it authorized deleting whatever directory
    it was dropped into. A marker is a claim about one root, on one side of one live root, from
    one run -- and all three have to agree with the request."""

    def test_a_copied_marker_elsewhere_does_not_authorize_deletion(self):
        out = self.provision()
        victim = os.path.join(self.tmp, "someone_elses_dir")
        os.makedirs(victim, exist_ok=True)
        with open(os.path.join(victim, "precious.txt"), "w") as fh:
            fh.write("not ours")
        shutil.copy(os.path.join(out["stage_root"], pv.MARKER_NAME),
                    os.path.join(victim, pv.MARKER_NAME))

        with self.assertRaises(pv.ProvisionRefused) as caught:
            pv.rollback(dict(out, stage_root=victim))
        self.assertIn("stage_root", str(caught.exception))
        self.assertTrue(os.path.isfile(os.path.join(victim, "precious.txt")))

    def test_a_marker_naming_another_live_root_is_refused(self):
        out = self.provision()
        with open(os.path.join(out["stage_root"], pv.MARKER_NAME)) as fh:
            marker = json.load(fh)
        marker["live_root"] = os.path.join(self.tmp, "a_different_live_root")
        with open(os.path.join(out["stage_root"], pv.MARKER_NAME), "w") as fh:
            json.dump(marker, fh)
        with self.assertRaises(pv.ProvisionRefused) as caught:
            pv.rollback(out)
        self.assertIn("live_root", str(caught.exception))
        self.assertTrue(os.path.isdir(out["stage_root"]))

    def test_the_paths_are_compared_canonically_not_as_strings(self):
        """A request naming the same root through a symlink is the same root."""
        out = self.provision()
        link = os.path.join(self.tmp, "link_to_stage")
        os.symlink(out["stage_root"], link)
        pv.rollback(dict(out, stage_root=link))
        self.assertFalse(os.path.exists(out["stage_root"]))

    def test_each_identity_field_is_checked_on_its_own(self):
        out = self.provision()
        for field, wrong in (("marker", "P5_ISOLATED_STAGING:other:tests"),
                             ("live_root", os.path.join(self.tmp, "elsewhere"))):
            with self.subTest(field):
                with self.assertRaises(pv.ProvisionRefused) as caught:
                    pv.rollback(dict(out, **{field: wrong}))
                self.assertIn(field, str(caught.exception))
                self.assertTrue(os.path.isdir(out["stage_root"]))
