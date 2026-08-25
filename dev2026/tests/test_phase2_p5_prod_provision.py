"""dev2026 — provisioning the P5 roots IN PRODUCTION, as a separate operation.

`ops/p5_provision.py` cannot do this. It refuses any stage root inside the declared live root,
and it declares its anchor with `allow_same_filesystem=True` — right for a throwaway root on one
filesystem, invalid for a deployment whose anchor is supposed to survive that filesystem. So the
readiness gate that read its presence as "this deployment can provision production" was reading
the wrong thing: a tool that refuses the job is not the tool for the job.

The fix is a second operation, not a flag on the first. A bypass would make the isolation
guarantee optional, and an optional guarantee is not one — so these tests also hold
`p5_provision` to still refusing, and to having no way to be told not to.

What the production operation must refuse, each on its own, before writing anything:

* no explicit production-scoped approval naming this live root and this run;
* an anchor inside the live root, on the live root's device, undeclared, or carrying the
  same-filesystem waiver;
* P5 roots that already exist — neither adopted nor deleted;
* free space below the floor, or a device that moved between preflight and the lock;
* a compaction already running.

And what it must do: take the compaction lock outermost, hold the ingest lock only across the
publish, own its state with a run-bound marker, and on failure remove only what it created.

Written before the module, and failing against an empty checkout.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
for p in (_DEV, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import p5_fixtures as fx  # noqa: E402
from ops import p5_prod_provision as pp  # noqa: E402
from ops import p5_provision as pv  # noqa: E402
from ops import p5_readiness as rd  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store import compaction_lock as cl  # noqa: E402
from store import repair_wal as rw  # noqa: E402

ANCHOR = "2026-06-27"
S = 90
NY = NX = 16

#: The real production root. It must never be created, opened or written by this suite; a test
#: asserts it does not appear as a default anywhere and that the suite leaves it alone.
REAL_G = "/home/odbadmin/Data/ghrsst"


class _FakeDev:
    """A stat result whose `st_dev` is ours and whose every other field is the real one.

    Two directories on different devices cannot be created portably inside a test — `/dev/shm`
    does not exist on macOS — so the device is substituted rather than arranged. Only `st_dev`
    is touched, and only for paths the test names."""

    def __init__(self, real, dev):
        self._real, self.st_dev = real, dev

    def __getattr__(self, name):
        return getattr(self._real, name)


def dev_map(mapping):
    """Patch `os.stat` inside the module so named paths report the given `st_dev`."""
    real_stat = os.stat
    resolved = {os.path.realpath(k): v for k, v in mapping.items()}

    def fake(path, *a, **kw):
        st = real_stat(path, *a, **kw)
        try:
            key = os.path.realpath(path)
        except (TypeError, ValueError):
            return st
        return _FakeDev(st, resolved[key]) if key in resolved else st

    return mock.patch.object(pp.os, "stat", side_effect=fake)


class _ProdBase(unittest.TestCase):
    """A live root, an anchor on a declared separate domain, and a delta to seed from."""

    RUN_ID = "20260825T120000Z"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p5-prod-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.live = os.path.join(self.tmp, "G")
        self.anchor = os.path.join(self.tmp, "anchor_domain")
        os.makedirs(self.live)
        os.makedirs(self.anchor)
        rw.declare_anchor_domain(self.anchor, domain_id="p5-prod-tests-domain",
                                 at_utc="2026-08-25T12:00:00Z", operator="tests",
                                 note="unit-test domain; NOT a real rollback domain")
        self.s0, self.e0 = bm.block_bounds(ANCHOR, S, 0)
        self.days = fx.calendar_span(self.s0, self.e0)[:8]
        self.delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(self.delta, self.days, ny=NY, nx=NX)
        self.clock = os.path.join(self.tmp, "p5_compaction.lock")
        self.ilock = os.path.join(self.tmp, "mur_delta.ingest.lock")
        # The anchor must look like a different device; the fixture cannot make one.
        self.devs = {self.anchor: 4242}

    def approval(self, **over):
        a = {"scope": pp.PRODUCTION_SCOPE, "approved_by": "cywhale",
             "approved_utc": "2026-08-25T12:00:00Z", "live_root": self.live,
             "run_id": self.RUN_ID}
        a.update(over)
        return a

    def provision(self, **over):
        kw = dict(live_root=self.live, anchor_root=self.anchor, approval=self.approval(),
                  compaction_lock_path=self.clock, ingest_lock_path=self.ilock,
                  delta_path=self.delta, start_day=self.s0, end_day=self.e0,
                  days=list(self.days), grid={"ny": NY, "nx": NX}, run_id=self.RUN_ID,
                  operator="tests", at_utc="2026-08-25T12:00:00Z", min_free_gib=0.001)
        kw.update(over)
        with dev_map(self.devs):
            return pp.provision_production(**kw)


class TestTheIsolatedToolStaysIsolated(unittest.TestCase):
    """The first tool is not extended, and cannot be told to stop refusing."""

    def test_the_isolated_tool_still_refuses_a_stage_root_in_the_live_root(self):
        tmp = tempfile.mkdtemp(prefix="p5-iso-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        live = os.path.join(tmp, "live")
        os.makedirs(live)
        with self.assertRaises(pv.ProvisionRefused) as caught:
            pv.provision(stage_root=os.path.join(live, "p5_stage"), live_root=live,
                         delta_path=os.path.join(tmp, "d.zarr"), start_day="2026-06-27",
                         end_day="2026-06-30", days=[], operator="t", at_utc="t",
                         grid={"ny": 8, "nx": 8})
        self.assertIn("live root", str(caught.exception).lower())

    def test_the_isolated_tool_has_no_flag_that_disables_isolation(self):
        """A bypass would make the guarantee optional. Checked against the signature AND the
        source, because a flag read from the environment would not appear in either alone."""
        import inspect
        params = set(inspect.signature(pv.provision).parameters)
        for banned in ("allow_live_root", "force", "unsafe_allow_live_root", "production",
                       "allow_production", "skip_isolation"):
            self.assertNotIn(banned, params)
        src = inspect.getsource(pv)
        for banned in ("allow_live_root", "unsafe_allow_live_root", "P5_ALLOW_LIVE"):
            self.assertNotIn(banned, src)

    def test_the_isolated_tool_still_marks_its_output_as_not_production(self):
        self.assertEqual(pv.STAGING_LABEL, "P5_ISOLATED_STAGING")
        self.assertIn(pv.STAGING_LABEL, rd.NON_QUALIFYING_LABELS)

    def test_the_two_tools_are_different_modules_with_different_labels(self):
        self.assertNotEqual(pp.PRODUCTION_LABEL, pv.STAGING_LABEL)
        self.assertNotEqual(pp.__file__, pv.__file__)


class TestProductionScopeIsRequired(_ProdBase):
    """Writing into the live data root needs an approval that names this root and this run."""

    def test_it_succeeds_under_a_correct_production_approval(self):
        """The positive control: every refusal below must be about its own defect."""
        out = self.provision()
        self.assertEqual(out["label"], pp.PRODUCTION_LABEL)
        self.assertIs(out["is_production_state"], True)
        self.assertEqual(out["generation"], 1)
        self.assertTrue(os.path.isdir(out["manifest_root"]))
        self.assertTrue(os.path.isdir(out["wal_root"]))

    def test_no_approval_at_all_refuses(self):
        for value in (None, "", [], 0):
            with self.subTest(repr(value)):
                with self.assertRaises(pp.ProductionApprovalRefused):
                    self.provision(approval=value)

    def test_an_approval_for_a_different_scope_refuses(self):
        with self.assertRaises(pp.ProductionApprovalRefused) as caught:
            self.provision(approval=self.approval(scope="p5_readiness_assessor"))
        self.assertIn("scope", str(caught.exception))

    def test_an_approval_for_a_different_live_root_refuses(self):
        other = os.path.join(self.tmp, "other_G")
        os.makedirs(other)
        with self.assertRaises(pp.ProductionApprovalRefused) as caught:
            self.provision(approval=self.approval(live_root=other))
        self.assertIn("live root", str(caught.exception))

    def test_an_approval_for_a_different_run_refuses(self):
        """A re-usable approval is a standing permission to write into production."""
        with self.assertRaises(pp.ProductionApprovalRefused) as caught:
            self.provision(approval=self.approval(run_id="some-other-run"))
        self.assertIn("run", str(caught.exception))

    def test_an_approval_missing_an_audit_field_refuses(self):
        for field in ("approved_by", "approved_utc", "scope", "live_root", "run_id"):
            with self.subTest(field):
                a = self.approval()
                del a[field]
                with self.assertRaises(pp.ProductionApprovalRefused):
                    self.provision(approval=a)

    def test_a_refused_approval_creates_nothing(self):
        before = sorted(os.listdir(self.live))
        with self.assertRaises(pp.ProductionApprovalRefused):
            self.provision(approval=self.approval(scope="wrong"))
        self.assertEqual(sorted(os.listdir(self.live)), before)


class TestTheAnchorMustBeOnAnotherFailureDomain(_ProdBase):
    """The anchor is the only thing that can notice the live volume rolling back."""

    def test_an_anchor_on_the_live_device_refuses(self):
        """Isolated to the device check: the anchor is outside the live root, declared, and
        without the waiver — only its device is wrong."""
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            with dev_map({}):                       # no substitution: same real device
                pp.provision_production(
                    live_root=self.live, anchor_root=self.anchor, approval=self.approval(),
                    compaction_lock_path=self.clock, ingest_lock_path=self.ilock,
                    delta_path=self.delta, start_day=self.s0, end_day=self.e0,
                    days=list(self.days), grid={"ny": NY, "nx": NX}, run_id=self.RUN_ID,
                    operator="tests", at_utc="t", min_free_gib=0.001)
        self.assertIn("same device", str(caught.exception))

    def test_an_anchor_inside_the_live_root_refuses(self):
        inside = os.path.join(self.live, "anchor")
        os.makedirs(inside)
        rw.declare_anchor_domain(inside, domain_id="inside", at_utc="t", operator="t")
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision(anchor_root=inside)
        self.assertIn("inside the live root", str(caught.exception))

    def test_an_undeclared_anchor_refuses(self):
        bare = os.path.join(self.tmp, "bare_anchor")
        os.makedirs(bare)
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision(anchor_root=bare, **{})
        self.assertIn("declares no rollback domain", str(caught.exception))

    def test_a_missing_anchor_refuses(self):
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision(anchor_root=os.path.join(self.tmp, "nope"))
        self.assertIn("not an existing directory", str(caught.exception))

    def test_no_anchor_at_all_refuses(self):
        for value in ("", None):
            with self.subTest(repr(value)):
                with self.assertRaises(pp.ProductionProvisionRefused):
                    self.provision(anchor_root=value)

    def test_the_same_filesystem_waiver_is_refused_outright(self):
        """`resolve_anchor_root` accepts this waiver as auditable, and for a staging root it is.
        Production refuses it before any write: the waiver says the anchor shares the volume it
        is supposed to outlive."""
        waived = os.path.join(self.tmp, "waived_anchor")
        os.makedirs(waived)
        rw.declare_anchor_domain(waived, domain_id="waived", at_utc="t", operator="t",
                                 allow_same_filesystem=True)
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision(anchor_root=waived, **{})
        self.assertIn("allow_same_filesystem", str(caught.exception))

    def test_the_production_module_never_passes_the_waiver_itself(self):
        """By AST, not by text: the module explains in prose why the waiver is wrong for
        production, and a substring search cannot tell that sentence from a call making it."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(pp))
        offenders = [
            node.lineno for node in ast.walk(tree) if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "allow_same_filesystem"
            and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)]
        self.assertEqual(offenders, [],
                         "the production module passes the same-filesystem waiver")


class TestExistingRootsAreNeitherAdoptedNorDeleted(_ProdBase):

    def test_an_existing_manifest_root_refuses(self):
        os.makedirs(os.path.join(self.live, pp.MANIFEST_DIRNAME))
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision()
        self.assertIn(pp.MANIFEST_DIRNAME, str(caught.exception))

    def test_an_existing_wal_root_refuses(self):
        os.makedirs(os.path.join(self.live, pp.WAL_DIRNAME))
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision()
        self.assertIn(pp.WAL_DIRNAME, str(caught.exception))

    def test_the_refusal_leaves_the_existing_data_untouched(self):
        """Refusing is not a licence to clean up. The whole point is that this run did not
        create it and therefore has no authority over it."""
        root = os.path.join(self.live, pp.MANIFEST_DIRNAME)
        os.makedirs(root)
        witness = os.path.join(root, "someone_elses.json")
        with open(witness, "w") as fh:
            fh.write('{"not":"ours"}')
        with self.assertRaises(pp.ProductionProvisionRefused):
            self.provision()
        self.assertTrue(os.path.isfile(witness), "pre-existing production data was removed")
        with open(witness) as fh:
            self.assertEqual(json.load(fh), {"not": "ours"})

    def test_the_paths_are_computed_not_taken_from_the_caller(self):
        """Containment is only an argument if the path cannot be pointed elsewhere."""
        import inspect
        params = set(inspect.signature(pp.provision_production).parameters)
        self.assertNotIn("manifest_root", params)
        self.assertNotIn("wal_root", params)


class TestCapacityAndDeviceBinding(_ProdBase):

    def test_free_space_below_the_floor_refuses(self):
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision(min_free_gib=10 ** 9)
        self.assertIn("below the required", str(caught.exception))

    def test_the_reading_is_bound_to_the_device_holding_the_live_root(self):
        out = self.provision()
        cap = out["capacity_at_start"]
        self.assertEqual(cap["filesystem_dev"], os.stat(self.live).st_dev)
        self.assertEqual(cap["data_root"], os.path.realpath(self.live))

    def test_a_device_that_moves_between_preflight_and_the_lock_refuses(self):
        """The preflight is a fact about a moment. Taking the lock is a later moment, and a
        remount in between makes the reading about a different volume."""
        # Flipped when the compaction lock is taken, not after a count of `os.stat` calls:
        # `os.path.isdir` stats too, so counting made the drift land in an arbitrary place --
        # sometimes in the preflight itself, where both readings then agreed and nothing
        # refused. The lock is the boundary the check is about, so it is the trigger.
        real_stat = os.stat
        locked = {"yes": False}
        real_acquire = cl.CompactionLock.acquire

        def acquire_then_drift(self_):
            got = real_acquire(self_)
            locked["yes"] = True
            return got

        def drifting(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            try:
                which = os.path.realpath(path)
            except (TypeError, ValueError):
                return st
            if which == os.path.realpath(self.anchor):
                return _FakeDev(st, 4242)
            if locked["yes"] and which == os.path.realpath(self.live):
                return _FakeDev(st, st.st_dev + 1)
            return st

        with mock.patch.object(cl.CompactionLock, "acquire", acquire_then_drift), \
                mock.patch.object(pp.os, "stat", side_effect=drifting):
            with self.assertRaises(pp.ProductionProvisionRefused) as caught:
                pp.provision_production(
                    live_root=self.live, anchor_root=self.anchor, approval=self.approval(),
                    compaction_lock_path=self.clock, ingest_lock_path=self.ilock,
                    delta_path=self.delta, start_day=self.s0, end_day=self.e0,
                    days=list(self.days), grid={"ny": NY, "nx": NX}, run_id=self.RUN_ID,
                    operator="tests", at_utc="t", min_free_gib=0.001,
                    cleanup_on_failure=True)
        self.assertIn("moved device", str(caught.exception))


class TestLockOrdering(_ProdBase):
    """§7.1b: compaction lock outermost, ingest lock only across the publish."""

    def _held(self, path):
        return cl.refuse_if_compaction_running(path, operation="probe") is not None

    def test_the_compaction_lock_is_held_across_build_and_publish(self):
        seen = {}
        real_build, real_publish = pp.build_block, pp.block_manifest.publish

        def spy_build(*a, **kw):
            seen["build"] = (self._held(self.clock), self._held(self.ilock))
            return real_build(*a, **kw)

        def spy_publish(*a, **kw):
            seen["publish"] = (self._held(self.clock), self._held(self.ilock))
            return real_publish(*a, **kw)

        with mock.patch.object(pp, "build_block", side_effect=spy_build), \
                mock.patch.object(pp.block_manifest, "publish", side_effect=spy_publish):
            self.provision()

        self.assertEqual(seen["build"], (True, False),
                         "during the build the compaction lock must be held and the ingest "
                         "lock must not: a build runs for hours and must not exclude ingest")
        self.assertEqual(seen["publish"], (True, True),
                         "during the publish BOTH must be held")

    def test_both_locks_are_released_afterwards(self):
        self.provision()
        self.assertFalse(self._held(self.clock))
        self.assertFalse(self._held(self.ilock))

    def test_a_compaction_already_running_refuses(self):
        with cl.CompactionLock(self.clock, holder="someone-else"):
            with self.assertRaises(pp.ProductionProvisionRefused) as caught:
                self.provision()
        self.assertIn("compaction is running", str(caught.exception))

    def test_publishing_asserts_the_compaction_lock_is_still_held(self):
        """The fencing check the whole posture rests on — checked by ORDER, not by count.

        There is a second `assert_still_held()` after the WAL is initialized, so counting calls
        let the pre-publish one be deleted while the count stayed non-zero. What matters is that
        one happens BEFORE the publish: after it, the block is already the store's authority and
        the assertion has nothing left to protect."""
        real_publish = pp.block_manifest.publish
        real_assert = cl.CompactionLock.assert_still_held
        real_build = pp.build_block
        order = []

        def recording_assert(self_):
            order.append("assert")
            return real_assert(self_)

        def recording_build(*a, **kw):
            out = real_build(*a, **kw)
            order.append("build_end")
            return out

        def recording_publish(*a, **kw):
            order.append("publish")
            return real_publish(*a, **kw)

        with mock.patch.object(cl.CompactionLock, "assert_still_held", recording_assert), \
                mock.patch.object(pp, "build_block", side_effect=recording_build), \
                mock.patch.object(pp.block_manifest, "publish", side_effect=recording_publish):
            self.provision()
        self.assertIn("publish", order)
        # IMMEDIATELY before, not merely somewhere before. `build_block` asserts the lock
        # itself, so "an assert happened earlier" is satisfied by the build and says nothing
        # about the publish -- which is how deleting the pre-publish fence survived a round.
        idx = order.index("publish")
        self.assertGreater(idx, 0, f"publish came first (order: {order})")
        self.assertEqual(order[idx - 1], "assert",
                         f"the event before publishing was not a fence check (order: {order})")


class TestPartialFailureAndRecovery(_ProdBase):

    def test_a_failure_after_the_roots_exist_removes_only_what_it_created(self):
        pre = os.path.join(self.live, "pre_existing_production_file")
        with open(pre, "w") as fh:
            fh.write("not ours")
        boom = RuntimeError("injected: the build failed")
        with mock.patch.object(pp, "build_block", side_effect=boom):
            with self.assertRaises(RuntimeError):
                self.provision()
        self.assertFalse(os.path.exists(os.path.join(self.live, pp.MANIFEST_DIRNAME)))
        self.assertFalse(os.path.exists(os.path.join(self.live, pp.WAL_DIRNAME)))
        self.assertTrue(os.path.isfile(pre), "pre-existing production data was removed")
        self.assertTrue(os.path.isdir(self.live), "the live root itself was removed")

    def test_with_cleanup_disabled_the_partial_state_stays_and_is_marked(self):
        with mock.patch.object(pp, "build_block", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.provision(cleanup_on_failure=False)
        marker_path = os.path.join(self.live, pp.marker_name(self.RUN_ID))
        self.assertTrue(os.path.isfile(marker_path))
        with open(marker_path) as fh:
            marker = json.load(fh)
        self.assertIs(marker["complete"], False)
        self.assertIn("injected", marker["failed_step"])
        self.assertTrue(marker["created_paths"], "the marker records nothing as created")

    def test_the_recorded_partial_state_can_be_rolled_back_afterwards(self):
        with mock.patch.object(pp, "build_block", side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.provision(cleanup_on_failure=False)
        marker_path = os.path.join(self.live, pp.marker_name(self.RUN_ID))
        with open(marker_path) as fh:
            marker = json.load(fh)
        pp.rollback_production(marker)
        self.assertFalse(os.path.exists(os.path.join(self.live, pp.MANIFEST_DIRNAME)))
        self.assertFalse(os.path.exists(marker_path))
        self.assertTrue(os.path.isdir(self.live))

    def test_rollback_refuses_a_marker_that_is_not_the_one_on_disk(self):
        out = self.provision()
        forged = dict(out, marker="P5_PRODUCTION_PROVISION:someone-else")
        with self.assertRaises(pp.ProductionProvisionRefused):
            pp.rollback_production(forged)
        self.assertTrue(os.path.isdir(out["manifest_root"]), "a forged marker deleted data")

    def test_rollback_refuses_when_no_marker_exists(self):
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            pp.rollback_production({"live_root": self.live, "run_id": "never-ran",
                                    "marker": "x"})
        self.assertIn("did not create", str(caught.exception))

    def test_rollback_never_removes_the_live_root(self):
        out = self.provision()
        marker_path = os.path.join(self.live, pp.marker_name(self.RUN_ID))
        with open(marker_path) as fh:
            marker = json.load(fh)
        marker["created_paths"] = [self.live]
        with open(marker_path, "w") as fh:
            json.dump(marker, fh)
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            pp.rollback_production(marker)
        self.assertIn("live root", str(caught.exception))
        self.assertTrue(os.path.isdir(self.live))


class TestWhatItBuildsIsTheRealThing(_ProdBase):
    """No second manifest, WAL or provenance truth."""

    def test_the_manifest_is_readable_by_the_store_and_checksums(self):
        out = self.provision()
        live = bm.load_live(out["manifest_root"])
        self.assertEqual(live["generation"], 1)
        self.assertEqual(len(live["segments"]), 1)
        copy = dict(live)
        copy["manifest_checksum"] = ""
        self.assertEqual(bm.compute_checksum(copy), live["manifest_checksum"])

    def test_the_wal_parses_against_the_declared_anchor(self):
        out = self.provision()
        state = rw.read_wal(out["wal_root"], anchor_root=out["anchor_root"])
        state.assert_fresh()

    def test_the_block_satisfies_the_store_contract(self):
        out = self.provision()
        insp = bm.inspect_store_contract(out["block_path"])
        self.assertEqual(len(insp.days), len(self.days))

    def test_it_performs_no_compaction_family_operation(self):
        """Root provisioning and compaction are separate operations with different blast
        radii; the module must not quietly contain the second."""
        import inspect
        src = inspect.getsource(pp)
        for banned in ("prune_delta", "execute_swap_plan", "corrective_refold", "shutil.move"):
            self.assertNotIn(banned, src)


class TestTheReadinessContractNamesTheProductionTool(unittest.TestCase):
    """The gate must measure the tool that can do the job."""

    def test_the_deployment_gate_names_the_production_tool(self):
        self.assertEqual(rd.DEPLOYMENT_TOOLS, ("ops/p5_prod_provision.py",))

    def test_the_staging_tool_is_named_separately_and_not_gated_on(self):
        self.assertEqual(rd.STAGING_TOOLS, ("ops/p5_provision.py",))
        self.assertNotIn("ops/p5_provision.py", rd.DEPLOYMENT_TOOLS)
        self.assertEqual(set(rd.DEPLOYMENT_TOOLS) & set(rd.STAGING_TOOLS), set())

    def test_neither_tool_is_in_the_runtime_inventory(self):
        for rel in rd.DEPLOYMENT_TOOLS + rd.STAGING_TOOLS:
            with self.subTest(rel):
                self.assertNotIn(rel, rd.INVENTORY_MODULES)

    def test_the_current_candidate_no_longer_satisfies_the_tool_gate(self):
        """`708058e` carries only the isolated tool. Before this change the gate read that as
        production provisioning capability; it is not, and the gate must say so."""
        problem = rd.deployment_tools_problem(rd.CURRENT_DEPLOY_SHA)
        self.assertTrue(problem, "the candidate still looks sufficient for production")
        self.assertIn("ops/p5_prod_provision.py", problem)

    def test_no_approved_commit_yet_carries_the_production_tool(self):
        for sha in rd.REVIEWED_DEPLOY_SHAS:
            with self.subTest(sha[:7]):
                self.assertTrue(rd.deployment_tools_problem(sha))


class TestThisSuiteNeverTouchesProduction(unittest.TestCase):
    """A local suite that could write to `$G` would be the defect it is testing for."""

    def test_the_real_production_root_is_not_a_default_anywhere(self):
        import inspect
        for mod in (pp, pv):
            with self.subTest(mod.__name__):
                self.assertNotIn(REAL_G, inspect.getsource(mod))

    def test_every_required_path_is_keyword_only_with_no_default(self):
        """Nothing can be provisioned by calling the function with no arguments."""
        import inspect
        sig = inspect.signature(pp.provision_production)
        for name in ("live_root", "anchor_root", "approval", "run_id"):
            with self.subTest(name):
                param = sig.parameters[name]
                self.assertIs(param.default, inspect.Parameter.empty)
                self.assertEqual(param.kind, inspect.Parameter.KEYWORD_ONLY)

    def test_the_real_production_root_was_not_created_by_this_suite(self):
        self.assertFalse(os.path.exists(REAL_G),
                         f"{REAL_G} exists on this machine; this suite must never create it")


class TestTheDocumentsRecordTheCorrection(unittest.TestCase):
    """`708058e` must stop reading as sufficient, in prose as well as in the gate.

    The gate refusing while the document still says the candidate provisions production is the
    same split-brain this phase keeps correcting: a check and a claim disagreeing, with the
    claim being what a person reads."""

    def _doc(self):
        with open(os.path.join(_DEV, "specs", "p5_deployment_readiness.md")) as fh:
            return fh.read()

    def test_the_document_says_the_candidate_is_not_sufficient(self):
        """Tied to the declaration, not to a phrase.

        The wording appears in a heading too, so matching it anywhere passed while the sentence
        that names the SHA had been deleted. The assertion derives the sentence from
        `CURRENT_DEPLOY_SHA`, and only holds while the gate actually refuses it."""
        doc = self._doc()
        self.assertTrue(rd.deployment_tools_problem(rd.CURRENT_DEPLOY_SHA),
                        "the gate no longer refuses the candidate; this test is stale")
        self.assertIn(
            f"**`{rd.CURRENT_DEPLOY_SHA[:7]}` is therefore not sufficient for production "
            f"provisioning.**", doc,
            "the document does not state, for the declared candidate, that it is insufficient")
        self.assertIn("new reviewed deployment commit", doc)

    def test_the_document_explains_why_the_isolated_tool_cannot_do_it(self):
        doc = self._doc()
        self.assertIn("A tool that refuses the job is not the tool for the job", doc)
        self.assertIn("allow_same_filesystem=True", doc)

    def test_the_document_names_both_declarations(self):
        doc = self._doc()
        self.assertIn("`DEPLOYMENT_TOOLS`", doc)
        self.assertIn("`STAGING_TOOLS`", doc)
        self.assertIn("ops/p5_prod_provision.py", doc)

    def test_the_generated_blocker_row_agrees_with_the_gate(self):
        """Document and generator, tied to the same declaration rather than to each other."""
        from ops import p5s8_evidence as ev
        row = ev._provisioning_blocker()
        for tool in rd.DEPLOYMENT_TOOLS:
            with self.subTest(tool):
                self.assertIn(tool, row)
        self.assertTrue(rd.deployment_tools_problem(rd.CURRENT_DEPLOY_SHA))
        self.assertIn("no approved commit", row)
