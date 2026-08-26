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
* lock paths that are not the canonical production ones;
* P5 roots that already exist — neither adopted nor deleted;
* free space below the CANONICAL G18 floor, a caller-supplied floor that is not that figure, or
  a device that moved between the preflight and the lock;
* a compaction already running.

And what it must do: take the compaction lock across the creation, own its state with a
run-bound marker that is never behind the filesystem, and clean up CHECKED — keeping the marker
and raising whenever anything it created is still there.

It publishes no manifest and initializes no WAL. `TestItPublishesNothing` records why.

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
import numpy as np  # noqa: E402
from ops import p5_prod_provision as pp  # noqa: E402
from ops import p5_legacy_base as lb  # noqa: E402
from ops import p5s8_evidence as ev  # noqa: E402
from ops import p5_provision as pv  # noqa: E402
from ops import p5_readiness as rd  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store import compaction_lock as cl  # noqa: E402
from store import repair_wal as rw  # noqa: E402
from store.segmented_cube import SegmentedCubeStore  # noqa: E402

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
    """A live root, an anchor on a declared separate domain, and a lowered canonical floor.

    The floor is moved by patching the POLICY source, never by passing a different number: the
    module refuses any `min_free_gib` that is not exactly the canonical figure, so a test that
    wanted a lower bar has to move the policy the same way `p5_readiness` reads it."""

    RUN_ID = "20260825T120000Z"
    TEST_FLOOR = 0.001

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
        # The legacy base generation 1 is published over: a pre-existing monolith beside the
        # roots, exactly as production has it.
        self.monolith = os.path.join(self.live, "mur_timecube_s8_t90_sh128.zarr")
        self.mono_days = fx.days_from("2026-01-01", 6)
        fx.build_block(self.monolith, self.mono_days, ny=8, nx=8)
        self.seal_monolith()
        self.locks = pp.canonical_lock_paths(self.live)
        self.clock = self.locks["compaction"]
        self.ilock = self.locks["ingest"]
        self.devs = {self.anchor: 4242}
        self.addCleanup(setattr, ev, "MINIMUM_FREE_GIB", ev.MINIMUM_FREE_GIB)
        ev.MINIMUM_FREE_GIB = self.TEST_FLOOR

    def seal_monolith(self):
        """Make the monolith read-only, as a production legacy base must be.

        Registered after the tempdir cleanup so it runs BEFORE it: `rmtree` cannot remove a
        read-only directory."""
        os.chmod(self.monolith, 0o555)
        self.addCleanup(self.unseal_monolith)

    def unseal_monolith(self):
        try:
            os.chmod(self.monolith, 0o755)
        except OSError:
            pass

    def approval(self, **over):
        a = {"scope": pp.PRODUCTION_SCOPE, "approved_by": "cywhale",
             "approved_utc": "2026-08-25T12:00:00Z", "live_root": self.live,
             "run_id": self.RUN_ID}
        a.update(over)
        return a

    def kwargs(self, **over):
        kw = dict(live_root=self.live, anchor_root=self.anchor, approval=self.approval(),
                  monolith_path=self.monolith,
                  compaction_lock_path=self.clock, ingest_lock_path=self.ilock,
                  run_id=self.RUN_ID, operator="tests", at_utc="2026-08-25T12:00:00Z",
                  min_free_gib=self.TEST_FLOOR)
        kw.update(over)
        return kw

    def provision(self, **over):
        with dev_map(self.devs):
            return pp.provision_production(**self.kwargs(**over))

    def marker(self):
        with open(os.path.join(self.live, pp.marker_name(self.RUN_ID))) as fh:
            return json.load(fh)

    def roots(self):
        return [os.path.join(self.live, pp.MANIFEST_DIRNAME),
                os.path.join(self.live, pp.WAL_DIRNAME)]


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


class TestItPublishesGenerationOneAndBindsTheWal(_ProdBase):
    """Roots, generation 1 over the monolith, and a WAL bound to it — in that order.

    An earlier version published a block built from the delta, which would have dropped the
    monolith's history from the served view; and a version after that published nothing, which
    left a manifest root nothing could bind a WAL to. The valid shape is a `legacy_base`
    generation 1, and `initialize_wal()` can only follow it because it binds through
    `load_live()`.

    What this still does NOT do is point the service at the result. `GHRSST_BLOCK_MANIFEST_DIR`
    is a separate production change."""

    def test_it_creates_the_two_roots(self):
        out = self.provision()
        self.assertEqual(sorted(out["created_paths"]), sorted(self.roots()))
        for path in self.roots():
            with self.subTest(path):
                self.assertTrue(os.path.isdir(path))

    def test_it_publishes_a_legacy_base_generation_one(self):
        out = self.provision()
        self.assertIs(out["manifest_published"], True)
        self.assertEqual(out["generation"], 1)
        live = bm.load_live(out["manifest_root"])
        self.assertEqual(live["generation"], 1)
        self.assertEqual(len(live["segments"]), 1)
        self.assertEqual(live["segments"][0]["kind"], "legacy_base")
        self.assertEqual(live["segments"][0]["boundary_kind"], "legacy")

    def test_the_generation_covers_the_whole_monolith(self):
        out = self.provision()
        insp = bm.inspect_store_contract(self.monolith)
        seg = bm.load_live(out["manifest_root"])["segments"][0]
        self.assertEqual(seg["start_day"], insp.days[0])
        self.assertEqual(seg["end_day"], insp.days[-1])
        self.assertEqual(seg["day_count"], len(insp.days))

    def test_it_initializes_the_wal_against_the_published_manifest(self):
        out = self.provision()
        self.assertIs(out["wal_initialized"], True)
        with dev_map(self.devs):
            state = rw.read_wal(out["wal_root"], anchor_root=out["anchor_root"])
            state.assert_fresh()
        self.assertIsNotNone(state.authority)
        self.assertEqual(os.path.realpath(state.authority["manifest_root"]),
                         os.path.realpath(out["manifest_root"]))

    def test_it_does_not_switch_the_service_configuration(self):
        """Pointing the service at these roots is a different production change."""
        before = os.environ.get("GHRSST_BLOCK_MANIFEST_DIR")
        out = self.provision()
        self.assertEqual(os.environ.get("GHRSST_BLOCK_MANIFEST_DIR"), before)
        # By AST: the note deliberately NAMES the setting to say it is not switched, and a
        # substring scan cannot tell that sentence from an assignment doing it.
        import ast
        import inspect as _i
        tree = ast.parse(_i.getsource(pp))
        writes = [n for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr in ("putenv", "setdefault")]
        self.assertEqual(writes, [])
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
                self.assertNotEqual(node.value.attr, "environ",
                                    "the operation writes to the environment")
        self.assertIn("GHRSST_ALLOWED_LEGACY_PATHS", out["note"])

    def test_the_published_manifest_needs_the_runtime_allow_list_to_be_served(self):
        """The manifest names the monolith; it does not authorize it."""
        out = self.provision()
        with self.assertRaises(Exception):
            SegmentedCubeStore(out["manifest_root"])
        store = SegmentedCubeStore(out["manifest_root"],
                                   allowed_legacy_paths=[self.monolith])
        self.assertEqual(set(store.days), set(self.mono_days))

    def test_a_missing_monolith_refuses_before_anything_is_created(self):
        before = sorted(os.listdir(self.live))
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision(monolith_path=os.path.join(self.live, "not_there.zarr"))
        self.assertIn("not an existing directory", str(caught.exception))
        self.assertEqual(sorted(os.listdir(self.live)), before)

    def test_a_monolith_inside_the_block_root_cannot_arise(self):
        """Where that refusal actually lives, rather than a second guard that cannot fire.

        For the monolith to be inside the block root, the block root must exist — it has to be
        an existing directory — and an existing block root is already refused. So the operation
        carries no separate containment check for it, and this pins the reason."""
        inside = os.path.join(self.live, pp.MANIFEST_DIRNAME, "nested_store.zarr")
        os.makedirs(inside)
        self.addCleanup(shutil.rmtree, os.path.join(self.live, pp.MANIFEST_DIRNAME),
                        ignore_errors=True)
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision(monolith_path=inside)
        self.assertIn("already exists", str(caught.exception))
        import inspect as _i
        self.assertNotIn("inside the block root being created", _i.getsource(pp),
                         "an unreachable containment guard was added back")


class TestProductionScopeIsRequired(_ProdBase):
    """Writing into the live data root needs an approval that names this root and this run."""

    def test_it_succeeds_under_a_correct_production_approval(self):
        out = self.provision()
        self.assertEqual(out["label"], pp.PRODUCTION_LABEL)
        self.assertIs(out["is_production_state"], True)
        self.assertIs(out["complete"], True)

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


class TestTheCapacityFloorIsCanonical(_ProdBase):
    """A floor the caller can lower is not a floor."""

    def test_the_floor_comes_from_the_same_policy_source_readiness_reads(self):
        self.assertEqual(pp.canonical_minimum_free_gib(), ev.MINIMUM_FREE_GIB)
        self.assertEqual(pp.canonical_minimum_free_gib(), rd.canonical_minimum_free_gib())

    def test_a_lower_floor_is_refused(self):
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision(min_free_gib=self.TEST_FLOOR / 10)
        self.assertIn("canonical", str(caught.exception))

    def test_a_higher_floor_is_refused_too(self):
        """Not only lowering: any altered finite threshold means the request is operating under
        a policy that is not the one in force."""
        with self.assertRaises(pp.ProductionProvisionRefused):
            self.provision(min_free_gib=self.TEST_FLOOR * 10)

    def test_True_is_refused_even_when_the_canonical_floor_is_one(self):
        """The one case where the equality check alone is not enough.

        `float(True) == 1.0`, so with a canonical floor of 1 GiB the equality comparison accepts
        `True` and the bool guard is the only thing refusing it. Every other fixture in this
        class uses a floor of 0.001, where equality happens to catch it — which is how deleting
        the bool guard survived a mutation round."""
        ev.MINIMUM_FREE_GIB = 1.0
        self.assertEqual(float(True), pp.canonical_minimum_free_gib())
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision(min_free_gib=True)
        self.assertIn("bool", str(caught.exception))

    def test_a_non_threshold_is_diagnosed_as_one_not_as_a_wrong_number(self):
        """NaN and the infinities are refused by the equality check too, so the refusal proves
        nothing about this branch. What it changes is the diagnosis, and the two send an
        operator to different places: "not a threshold" is a malformed request, "not the
        canonical floor" is a request under the wrong policy."""
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(repr(value)):
                with self.assertRaises(pp.ProductionProvisionRefused) as caught:
                    self.provision(min_free_gib=value)
                self.assertIn("not a threshold", str(caught.exception))
                self.assertNotIn("canonical G18 floor", str(caught.exception))

    def test_every_non_threshold_shape_is_refused(self):
        cases = {"True": True, "False": False, "zero": 0, "negative": -1.0,
                 "NaN": float("nan"), "+inf": float("inf"), "-inf": float("-inf"),
                 "string": "0.001", "None": None, "list": [0.001], "complex": 1 + 0j}
        for name, value in cases.items():
            with self.subTest(name):
                with self.assertRaises(pp.ProductionProvisionRefused):
                    self.provision(min_free_gib=value)

    def test_free_space_below_the_canonical_floor_refuses(self):
        ev.MINIMUM_FREE_GIB = 10 ** 9
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision(min_free_gib=10 ** 9)
        self.assertIn("below the canonical G18 floor", str(caught.exception))

    def test_the_reading_is_bound_to_the_device_holding_the_live_root(self):
        out = self.provision()
        cap = out["capacity_at_start"]
        self.assertEqual(cap["filesystem_dev"], os.stat(self.live).st_dev)
        self.assertEqual(cap["data_root"], os.path.realpath(self.live))
        self.assertEqual(cap["min_free_gib"], pp.canonical_minimum_free_gib())

    def test_the_device_is_re_measured_under_the_lock(self):
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
                pp.provision_production(**self.kwargs())
        self.assertIn("moved device", str(caught.exception))

    def test_free_space_is_re_measured_under_the_lock(self):
        """The preflight is a fact about a moment; the lock is a later one."""
        real_statvfs = os.statvfs
        locked = {"yes": False}
        real_acquire = cl.CompactionLock.acquire

        class _Full:
            def __init__(self, real):
                self._real, self.f_bavail = real, 0

            def __getattr__(self, name):
                return getattr(self._real, name)

        def acquire_then_fill(self_):
            got = real_acquire(self_)
            locked["yes"] = True
            return got

        def filling(path, *a, **kw):
            st = real_statvfs(path, *a, **kw)
            return _Full(st) if locked["yes"] else st

        with dev_map(self.devs), \
                mock.patch.object(cl.CompactionLock, "acquire", acquire_then_fill), \
                mock.patch.object(pp.os, "statvfs", side_effect=filling):
            with self.assertRaises(pp.ProductionProvisionRefused) as caught:
                pp.provision_production(**self.kwargs())
        self.assertIn("under the lock", str(caught.exception))


class TestTheLockPathsAreCanonical(_ProdBase):
    """The locks are parameters so an operator states them and can be refused — not so they
    can be moved."""

    def test_the_canonical_paths_are_derived_from_the_live_root(self):
        self.assertEqual(pp.canonical_lock_paths(self.live), {
            "compaction": os.path.join(self.live, pp.COMPACTION_LOCK_NAME),
            "ingest": os.path.join(self.live, pp.INGEST_LOCK_NAME)})

    def test_a_wrong_compaction_lock_path_refuses_before_writing(self):
        elsewhere = os.path.join(self.tmp, "somewhere_else.lock")
        before = sorted(os.listdir(self.live))
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision(compaction_lock_path=elsewhere)
        self.assertIn("canonical production lock", str(caught.exception))
        self.assertEqual(sorted(os.listdir(self.live)), before,
                         "a wrong lock path still wrote something")

    def test_a_wrong_ingest_lock_path_refuses(self):
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision(ingest_lock_path=os.path.join(self.tmp, "nope.lock"))
        self.assertIn("canonical production lock", str(caught.exception))

    def test_a_symlink_alias_to_the_canonical_lock_is_accepted(self):
        """Compared by realpath: an alias locks the same inode and excludes the same writers."""
        open(self.clock, "a").close()
        alias = os.path.join(self.tmp, "alias_compaction.lock")
        os.symlink(self.clock, alias)
        out = self.provision(compaction_lock_path=alias)
        self.assertIs(out["complete"], True)

    def test_a_symlink_pointing_AWAY_is_refused(self):
        """String equality would accept it, and it locks a different inode."""
        other = os.path.join(self.tmp, "other.lock")
        open(other, "a").close()
        os.symlink(other, self.clock + ".alias")
        with self.assertRaises(pp.ProductionProvisionRefused):
            self.provision(compaction_lock_path=self.clock + ".alias")

    def test_a_missing_lock_path_refuses(self):
        for value in ("", None):
            with self.subTest(repr(value)):
                with self.assertRaises(pp.ProductionProvisionRefused):
                    self.provision(compaction_lock_path=value)

    def test_the_ingest_lock_is_taken_ONLY_across_the_publish(self):
        """§7.1b: the compaction lock is outermost and held throughout; the ingest lock is
        taken for the publish and released before the WAL is initialized. Checked by ORDER,
        because "both were acquired at some point" says nothing about the scope."""
        real_acquire = cl.CompactionLock.acquire
        real_release = cl.CompactionLock.release
        real_publish = pp.p5_legacy_base.publish_prepared
        real_wal = pp.repair_wal.initialize_wal
        order = []

        def which(self_):
            p = os.path.realpath(self_.path)
            return ("compaction" if p == os.path.realpath(self.clock)
                    else "ingest" if p == os.path.realpath(self.ilock) else "other")

        def rec_acquire(self_):
            order.append(f"acquire:{which(self_)}")
            return real_acquire(self_)

        def rec_release(self_):
            if self_.held:
                order.append(f"release:{which(self_)}")
            return real_release(self_)

        def rec_publish(*a, **kw):
            order.append("publish")
            return real_publish(*a, **kw)

        def rec_wal(*a, **kw):
            order.append("wal_init")
            return real_wal(*a, **kw)

        with mock.patch.object(cl.CompactionLock, "acquire", rec_acquire), \
                mock.patch.object(cl.CompactionLock, "release", rec_release), \
                mock.patch.object(pp.p5_legacy_base, "publish_prepared",
                                  side_effect=rec_publish), \
                mock.patch.object(pp.repair_wal, "initialize_wal", side_effect=rec_wal):
            self.provision()

        self.assertEqual(order[0], "acquire:compaction", f"order: {order}")
        self.assertEqual(order[-1], "release:compaction", f"order: {order}")
        i_acq, i_rel = order.index("acquire:ingest"), order.index("release:ingest")
        pub, wal = order.index("publish"), order.index("wal_init")
        self.assertLess(i_acq, pub, f"the ingest lock was taken after the publish: {order}")
        self.assertLess(pub, i_rel, f"the ingest lock was released before the publish: {order}")
        self.assertLess(i_rel, wal,
                        f"the WAL was initialized while the ingest lock was held: {order}")

    def test_a_compaction_already_running_refuses(self):
        with cl.CompactionLock(self.clock, holder="someone-else"):
            with self.assertRaises(pp.ProductionProvisionRefused) as caught:
                self.provision()
        self.assertIn("compaction is running", str(caught.exception))

    def test_the_lock_is_released_afterwards(self):
        self.provision()
        self.assertIsNone(cl.refuse_if_compaction_running(self.clock, operation="probe"))


class TestTheAnchorMustBeOnAnotherFailureDomain(_ProdBase):
    """The anchor is the only thing that can notice the live volume rolling back."""

    def test_an_anchor_on_the_live_device_refuses(self):
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            with dev_map({}):
                pp.provision_production(**self.kwargs())
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
            self.provision(anchor_root=bare)
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
        waived = os.path.join(self.tmp, "waived_anchor")
        os.makedirs(waived)
        rw.declare_anchor_domain(waived, domain_id="waived", at_utc="t", operator="t",
                                 allow_same_filesystem=True)
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision(anchor_root=waived)
        self.assertIn("allow_same_filesystem", str(caught.exception))

    def test_the_production_module_never_passes_the_waiver_itself(self):
        """By AST, not by text: the module explains in prose why the waiver is wrong, and a
        substring search cannot tell that sentence from a call making it."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(pp))
        offenders = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                     for kw in n.keywords if kw.arg == "allow_same_filesystem"
                     and not (isinstance(kw.value, ast.Constant) and kw.value.value is False)]
        self.assertEqual(offenders, [])


class TestExistingRootsAreNeitherAdoptedNorDeleted(_ProdBase):

    def test_an_existing_manifest_root_refuses_AS_PRE_EXISTING(self):
        """The diagnosis matters, not only the refusal.

        Without the preflight the run still stops -- `makedirs` without `exist_ok` raises and
        the racing-creator branch catches it -- but it reports a root that "appeared while the
        lock was held". That sends an operator to look for a concurrent writer that does not
        exist. "It was already there before this run started" is a different investigation, and
        it is the true one."""
        os.makedirs(os.path.join(self.live, pp.MANIFEST_DIRNAME))
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision()
        message = str(caught.exception)
        self.assertIn(pp.MANIFEST_DIRNAME, message)
        self.assertIn("already exists", message)
        self.assertIn("will not adopt it", " ".join(message.split()))
        self.assertNotIn("appeared while the lock was held", message)

    def test_the_pre_existing_refusal_happens_before_the_lock_is_taken(self):
        """It is a preflight, and a preflight that runs after the lock is a different check."""
        os.makedirs(os.path.join(self.live, pp.WAL_DIRNAME))
        acquired = []
        real_acquire = cl.CompactionLock.acquire

        def recording(self_):
            acquired.append(self_.path)
            return real_acquire(self_)

        with mock.patch.object(cl.CompactionLock, "acquire", recording):
            with self.assertRaises(pp.ProductionProvisionRefused):
                self.provision()
        self.assertEqual(acquired, [], "the lock was taken before the existing-root preflight")

    def test_an_existing_wal_root_refuses(self):
        os.makedirs(os.path.join(self.live, pp.WAL_DIRNAME))
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision()
        self.assertIn(pp.WAL_DIRNAME, str(caught.exception))
        self.assertIn("already exists", str(caught.exception))

    def test_the_refusal_leaves_the_existing_data_untouched(self):
        root = os.path.join(self.live, pp.MANIFEST_DIRNAME)
        os.makedirs(root)
        witness = os.path.join(root, "someone_elses.json")
        with open(witness, "w") as fh:
            fh.write('{"not":"ours"}')
        with self.assertRaises(pp.ProductionProvisionRefused):
            self.provision()
        self.assertTrue(os.path.isfile(witness), "pre-existing production data was removed")

    def test_the_root_paths_are_computed_not_taken_from_the_caller(self):
        import inspect
        params = set(inspect.signature(pp.provision_production).parameters)
        self.assertNotIn("manifest_root", params)
        self.assertNotIn("wal_root", params)


class TestTheMarkerIsNeverBehindTheFilesystem(_ProdBase):
    """`created_paths: []` while roots exist is the one state that must not occur.

    The completion write used to sit OUTSIDE the recovery path: a failure there left both roots
    on disk under the initial stamp, which said nothing had been created — a marker that
    authorized removing nothing while two directories existed."""

    def test_the_marker_records_each_root_as_it_is_created(self):
        """Watching the marker WRITES, not `makedirs`: the lock also creates a directory, so
        counting makedirs calls watches the wrong event."""
        payloads = []
        real_write = pp._write_marker

        def watching(path, payload):
            payloads.append(sorted(payload.get("created_paths") or []))
            return real_write(path, payload)

        with mock.patch.object(pp, "_write_marker", side_effect=watching):
            self.provision()
        self.assertEqual(payloads[0], [], "the ownership stamp claimed paths before any existed")
        self.assertEqual(payloads[1], [self.roots()[0]])
        self.assertEqual(payloads[2], sorted(self.roots()))
        self.assertEqual(payloads[-1], sorted(self.roots()))

    def test_a_failure_writing_the_completion_marker_does_not_leave_roots_silently(self):
        """The blocker, injected exactly where it was: the last write."""
        calls = {"n": 0}
        real_write = pp._write_marker

        def fail_on_last(path, payload):
            calls["n"] += 1
            if payload.get("complete") is True:
                raise OSError("injected: the completion marker could not be written")
            return real_write(path, payload)

        with mock.patch.object(pp, "_write_marker", side_effect=fail_on_last):
            with self.assertRaises(OSError):
                self.provision()
        for path in self.roots():
            with self.subTest(path):
                self.assertFalse(os.path.exists(path),
                                 "a root remained after the completion write failed")
        self.assertFalse(os.path.exists(os.path.join(self.live, pp.marker_name(self.RUN_ID))))

    def test_if_that_cleanup_also_fails_the_marker_lists_every_created_path(self):
        """The other acceptable outcome: a typed unrecoverable state whose marker is accurate.
        What must never happen is a marker claiming nothing was created."""
        real_write = pp._write_marker

        def fail_on_complete(path, payload):
            if payload.get("complete") is True:
                raise OSError("injected: completion marker failed")
            return real_write(path, payload)

        with mock.patch.object(pp, "_write_marker", side_effect=fail_on_complete), \
                mock.patch.object(pp.shutil, "rmtree",
                                  side_effect=OSError("injected: cannot remove")):
            with self.assertRaises(pp.ProductionCleanupError) as caught:
                self.provision()
        self.assertEqual(sorted(caught.exception.unrestored), sorted(self.roots()))
        self.assertEqual(os.path.realpath(caught.exception.live_root),
                         os.path.realpath(self.live))
        marker = self.marker()
        self.assertEqual(sorted(marker["created_paths"]), sorted(self.roots()),
                         "the marker claims paths that do not match what was created")
        self.assertIs(marker["complete"], False)
        self.assertNotEqual(marker["created_paths"], [])

    def test_a_marker_write_failure_before_any_root_leaves_nothing(self):
        with mock.patch.object(pp, "_write_marker",
                               side_effect=OSError("injected: first marker failed")):
            with self.assertRaises(OSError):
                self.provision()
        for path in self.roots():
            with self.subTest(path):
                self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.isdir(self.monolith), "pre-existing data was removed")


class TestCleanupIsCheckedNotBestEffort(_ProdBase):
    """`ignore_errors=True` reported success for a directory it had not removed."""

    def _fail_build(self):
        return mock.patch.object(pp.os, "makedirs",
                                 side_effect=[None, OSError("injected: second root failed")])

    def test_a_failure_after_one_root_removes_it_and_leaves_nothing(self):
        first, second = self.roots()
        real_makedirs = pp.os.makedirs
        calls = {"n": 0}

        def one_then_fail(path, *a, **kw):
            # Keyed on the ROOT paths: `CompactionLock.acquire` calls makedirs too, so a call
            # counter fails on the lock directory and the roots are never reached.
            if os.path.basename(path) == pp.WAL_DIRNAME:
                calls["n"] += 1
                raise OSError("injected: the second root could not be created")
            return real_makedirs(path, *a, **kw)

        with mock.patch.object(pp.os, "makedirs", side_effect=one_then_fail):
            with self.assertRaises(OSError):
                self.provision()
        self.assertEqual(calls["n"], 1)
        self.assertFalse(os.path.exists(first))
        self.assertFalse(os.path.exists(second))
        self.assertFalse(os.path.exists(os.path.join(self.live, pp.marker_name(self.RUN_ID))))

    def test_a_cleanup_that_cannot_delete_raises_and_keeps_the_marker(self):
        self.provision()
        marker = self.marker()
        with mock.patch.object(pp.shutil, "rmtree",
                               side_effect=OSError("injected: cannot remove")):
            with self.assertRaises(pp.ProductionCleanupError) as caught:
                pp.rollback_production(marker)
        self.assertEqual(sorted(caught.exception.unrestored), sorted(self.roots()))
        self.assertTrue(os.path.isfile(os.path.join(self.live, pp.marker_name(self.RUN_ID))),
                        "the ownership marker was removed while paths remained")

    def test_the_cleanup_error_chains_the_original_failure(self):
        boom = RuntimeError("injected: the original provisioning failure")
        real_makedirs = pp.os.makedirs
        calls = {"n": 0}

        def one_then_fail(path, *a, **kw):
            if os.path.basename(path) == pp.WAL_DIRNAME:
                calls["n"] += 1
                raise boom
            return real_makedirs(path, *a, **kw)

        with mock.patch.object(pp.os, "makedirs", side_effect=one_then_fail), \
                mock.patch.object(pp.shutil, "rmtree",
                                  side_effect=OSError("injected: cannot remove")):
            with self.assertRaises(pp.ProductionCleanupError) as caught:
                self.provision()
        self.assertIs(caught.exception.__cause__, boom)
        self.assertEqual(caught.exception.unrestored, [self.roots()[0]])

    def test_every_recorded_path_is_attempted_even_when_one_fails(self):
        """One failure must not skip the rest: a partially attempted cleanup leaves more behind
        than it reports."""
        attempted = []
        real_rmtree = shutil.rmtree

        def selective(path, *a, **kw):
            attempted.append(path)
            if path.endswith(pp.WAL_DIRNAME):
                raise OSError("injected: this one cannot be removed")
            return real_rmtree(path, *a, **kw)

        out = self.provision()
        marker = self.marker()
        with mock.patch.object(pp.shutil, "rmtree", side_effect=selective):
            with self.assertRaises(pp.ProductionCleanupError) as caught:
                pp.rollback_production(marker)
        self.assertEqual(sorted(attempted), sorted(out["created_paths"]))
        self.assertEqual(caught.exception.unrestored, [os.path.join(self.live,
                                                                    pp.WAL_DIRNAME)])
        self.assertFalse(os.path.exists(os.path.join(self.live, pp.MANIFEST_DIRNAME)))

    def test_the_marker_is_removed_only_after_every_path_is_verified_absent(self):
        self.provision()
        marker = self.marker()
        pp.rollback_production(marker)
        self.assertFalse(os.path.exists(os.path.join(self.live, pp.marker_name(self.RUN_ID))))
        for path in self.roots():
            with self.subTest(path):
                self.assertFalse(os.path.exists(path))

    def test_a_cleanup_reporting_success_while_the_path_remains_is_caught(self):
        """`rmtree` returning without raising is not evidence the directory is gone; the check
        is what `os.path.exists` says afterwards."""
        self.provision()
        marker = self.marker()
        with mock.patch.object(pp.shutil, "rmtree", side_effect=lambda *a, **kw: None):
            with self.assertRaises(pp.ProductionCleanupError) as caught:
                pp.rollback_production(marker)
        self.assertEqual(sorted(caught.exception.unrestored), sorted(self.roots()))

    def test_discard_is_checked_too(self):
        """The path taken when the marker itself cannot be written."""
        first = self.roots()[0]
        os.makedirs(first)
        marker_path = os.path.join(self.live, pp.marker_name(self.RUN_ID))
        with open(marker_path, "w") as fh:
            fh.write("{}")
        with mock.patch.object(pp.shutil, "rmtree",
                               side_effect=OSError("injected: cannot remove")):
            with self.assertRaises(pp.ProductionCleanupError) as caught:
                pp._discard([first], marker_path, live_root=self.live)
        self.assertEqual(caught.exception.unrestored, [first])
        self.assertTrue(os.path.isfile(marker_path),
                        "_discard removed the marker while a created path remained")

    def test_discard_removes_the_marker_only_when_everything_is_gone(self):
        first = self.roots()[0]
        os.makedirs(first)
        marker_path = os.path.join(self.live, pp.marker_name(self.RUN_ID))
        with open(marker_path, "w") as fh:
            fh.write("{}")
        pp._discard([first], marker_path, live_root=self.live)
        self.assertFalse(os.path.exists(first))
        self.assertFalse(os.path.exists(marker_path))


class TestRollbackOwnership(_ProdBase):

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
        self.provision()
        marker_path = os.path.join(self.live, pp.marker_name(self.RUN_ID))
        marker = self.marker()
        marker["created_paths"] = [self.live]
        with open(marker_path, "w") as fh:
            json.dump(marker, fh)
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            pp.rollback_production(marker)
        self.assertIn("live root", str(caught.exception))
        self.assertTrue(os.path.isdir(self.live))

    def test_rollback_refuses_a_recorded_path_outside_the_live_root(self):
        self.provision()
        outside = os.path.join(self.tmp, "not_ours")
        os.makedirs(outside)
        marker_path = os.path.join(self.live, pp.marker_name(self.RUN_ID))
        marker = self.marker()
        marker["created_paths"] = [outside]
        with open(marker_path, "w") as fh:
            json.dump(marker, fh)
        with self.assertRaises(pp.ProductionProvisionRefused):
            pp.rollback_production(marker)
        self.assertTrue(os.path.isdir(outside))


class TestTheReadinessContractNamesTheProductionTool(unittest.TestCase):
    """The gate must measure the tool that can do the job."""

    def test_the_deployment_gate_names_the_production_tool(self):
        self.assertEqual(rd.DEPLOYMENT_TOOLS, ("ops/p5_prod_provision.py",))

    def test_the_staging_tool_is_named_separately_and_not_gated_on(self):
        self.assertEqual(rd.STAGING_TOOLS, ("ops/p5_provision.py",))
        self.assertEqual(set(rd.DEPLOYMENT_TOOLS) & set(rd.STAGING_TOOLS), set())

    def test_neither_tool_is_in_the_runtime_inventory(self):
        for rel in rd.DEPLOYMENT_TOOLS + rd.STAGING_TOOLS:
            with self.subTest(rel):
                self.assertNotIn(rel, rd.INVENTORY_MODULES)

    def test_the_current_candidate_no_longer_satisfies_the_tool_gate(self):
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
        import inspect
        sig = inspect.signature(pp.provision_production)
        for name in ("live_root", "anchor_root", "approval", "run_id", "min_free_gib",
                     "compaction_lock_path", "ingest_lock_path"):
            with self.subTest(name):
                param = sig.parameters[name]
                self.assertIs(param.default, inspect.Parameter.empty)
                self.assertEqual(param.kind, inspect.Parameter.KEYWORD_ONLY)

    def test_the_real_production_root_was_not_created_by_this_suite(self):
        self.assertFalse(os.path.exists(REAL_G),
                         f"{REAL_G} exists on this machine; this suite must never create it")


class TestTheDocumentsRecordTheCorrection(unittest.TestCase):
    """`708058e` must stop reading as sufficient, and the narrowed scope must be written down."""

    def _doc(self):
        with open(os.path.join(_DEV, "specs", "p5_deployment_readiness.md")) as fh:
            return fh.read()

    def test_the_document_says_the_candidate_is_not_sufficient(self):
        doc = self._doc()
        self.assertTrue(rd.deployment_tools_problem(rd.CURRENT_DEPLOY_SHA),
                        "the gate no longer refuses the candidate; this test is stale")
        self.assertIn(
            f"**`{rd.CURRENT_DEPLOY_SHA[:7]}` is therefore not sufficient for production "
            f"provisioning.**", doc)
        self.assertIn("new reviewed deployment commit", doc)

    def test_the_document_explains_why_the_isolated_tool_cannot_do_it(self):
        doc = self._doc()
        self.assertIn("A tool that refuses the job is not the tool for the job", doc)
        self.assertIn("allow_same_filesystem=True", doc)

    def test_the_document_records_what_the_operation_does_and_why(self):
        """Both rejected shapes belong on the page: "publishes generation 1" reads as an
        arbitrary choice unless the two that do not work are recorded beside it."""
        doc = self._doc()
        self.assertIn("legacy_base", doc)
        self.assertIn("1 273", doc, "the document does not quantify what a delta-only "
                                    "generation 1 would drop from the served view")
        self.assertIn("initialize_wal", doc)
        self.assertIn("does NOT switch `GHRSST_BLOCK_MANIFEST_DIR`",
                      " ".join(doc.split()))

    def test_the_document_records_the_immutability_finding(self):
        flat = " ".join(self._doc().split())
        self.assertIn("The monolith is not sealed by the fingerprints alone", flat)
        self.assertIn("a value rewritten in place inside an existing day", flat)
        self.assertIn("change detection, not a cryptographic seal", flat)

    def test_the_document_records_the_lock_scope_correction(self):
        """The defect and its fix both belong on the page: "prepare then publish" reads as an
        arbitrary structure unless the reason is recorded beside it."""
        flat = " ".join(self._doc().split())
        self.assertIn("The preparation used to run inside the ingest lock.", flat)
        self.assertIn("the daily-append cron is blocked behind a scan", flat)
        self.assertIn("Only the second is inside the lock", flat)

    def test_the_document_records_the_seal_precondition(self):
        flat = " ".join(self._doc().split())
        self.assertIn("The monolith must be sealed BEFORE it becomes an authority", flat)
        self.assertIn("necessary, not sufficient", flat)
        self.assertIn("production readiness stays blocked on it", flat)

    def test_the_document_records_the_block_grid_policy(self):
        flat = " ".join(self._doc().split())
        self.assertIn("BLOCK_DAYS_S", flat)
        self.assertIn("provisional only; nothing has selected it", flat)
        self.assertIn("the day after the audited `end_day`", flat)

    def test_the_document_says_running_it_against_production_needs_approval(self):
        flat = " ".join(self._doc().split())
        self.assertIn("only ever run in a local synthetic fixture", flat)
        self.assertIn("separate production operation requiring explicit approval", flat)

    def test_the_document_separates_the_two_allow_lists(self):
        """Runtime authorization and assessor configuration answer different questions, and a
        document that names one `allow-list` teaches an operator to conflate them."""
        flat = " ".join(self._doc().split())
        self.assertIn("GHRSST_ALLOWED_LEGACY_PATHS", flat)
        self.assertIn("p5_readiness.ALLOWED_LEGACY_PATHS", flat)
        # In context. "neither implies the other" appears elsewhere on the page too, so the
        # bare phrase was satisfied while the sentence distinguishing the two allow-lists had
        # been changed to say they are the same.
        self.assertIn('may this manifest claim that store" — and neither implies the other',
                      flat)

    def test_the_document_records_that_the_runtime_blocker_is_closed(self):
        doc = self._doc()
        # Whitespace-normalised: markdown wraps, and a claim split across two lines is still
        # the claim. Matching the raw text made the test depend on where the paragraph broke.
        flat = " ".join(doc.split())
        self.assertIn("That blocker is closed", flat)
        self.assertIn("no fallback to the monolith", flat)
        self.assertIn("The manifest never authorizes its own external path", flat)

    def test_the_document_keeps_publishing_a_generation_out_of_scope(self):
        doc = self._doc()
        self.assertIn("creates no roots, touches no WAL", doc)
        self.assertIn("production change with its own approval, and none has been requested",
                      doc)

    def test_the_document_says_empty_roots_move_nothing(self):
        doc = self._doc()
        self.assertIn("Empty roots move nothing", doc)
        self.assertIn("not progress toward `compaction_ready`", doc)

    def test_the_document_says_a_new_deployment_commit_and_approval_are_needed(self):
        doc = self._doc()
        self.assertIn("new reviewed deployment commit", doc)
        self.assertIn("new assessor approval record", doc)
        self.assertIn("never run against a real\nproduction root", doc)

    def test_the_document_records_the_recovery_protocol(self):
        doc = self._doc()
        self.assertIn("Interruption: recoverable, not atomic", doc)
        self.assertIn("planned_paths", doc)
        self.assertIn("Operator recovery", doc)

    def test_the_generated_blocker_row_agrees_with_the_gate(self):
        from ops import p5s8_evidence as ev_
        row = ev_._provisioning_blocker()
        for tool in rd.DEPLOYMENT_TOOLS:
            with self.subTest(tool):
                self.assertIn(tool, row)
        self.assertIn("no approved commit", row)


class TestASurvivingRecordNamesEveryPathThatMayExist(_ProdBase):
    """The gap a marker written AFTER the fact cannot close.

    The protocol used to learn what existed by recording each root once it was created. Between
    `os.makedirs(A)` and the write that records A, the marker says nothing was created while A
    is on disk — and if the process stops there, nothing on disk names A.

    So the set is PLANNED before anything exists. It is knowable in advance: two fixed names
    under one root. `created_paths` still grows as they really appear, and the difference
    between the two lists is exactly the window."""

    def test_the_ownership_stamp_declares_the_planned_paths_before_anything_exists(self):
        payloads = []
        real_write = pp._write_marker

        def watching(path, payload):
            # Existence is captured AT THE MOMENT of the write. Checking it after the run
            # would ask whether the roots exist now, which they do -- a different question.
            payloads.append((dict(payload),
                             {p: os.path.exists(p) for p in (payload.get("planned_paths") or ())}))
            return real_write(path, payload)

        with mock.patch.object(pp, "_write_marker", side_effect=watching):
            self.provision()
        first, existed = payloads[0]
        self.assertEqual(sorted(first["planned_paths"]), sorted(self.roots()))
        self.assertEqual(first["created_paths"], [],
                         "the first stamp claimed something was created")
        self.assertEqual(sorted(existed), sorted(self.roots()))
        for path, was_there in existed.items():
            with self.subTest(path):
                self.assertFalse(was_there,
                                 "a planned path already existed when it was declared")

    def test_an_incremental_marker_write_failure_still_cleans_up(self):
        """The exact gap: the write that would have recorded the first root fails."""
        real_write = pp._write_marker
        state = {"n": 0}

        def fail_on_first_update(path, payload):
            state["n"] += 1
            if state["n"] == 2:                       # the stamp is 1; this is the first update
                raise OSError("injected: the incremental marker update failed")
            return real_write(path, payload)

        with mock.patch.object(pp, "_write_marker", side_effect=fail_on_first_update):
            with self.assertRaises(OSError):
                self.provision()
        for path in self.roots():
            with self.subTest(path):
                self.assertFalse(os.path.exists(path))

    def test_an_incremental_marker_failure_AND_a_cleanup_failure_leaves_a_complete_record(self):
        """Both halves broken at once, which is where the old protocol lost information.

        The recovery marker on disk was written before the first root existed, so what makes it
        complete is `planned_paths`, not anything learned afterwards."""
        real_write = pp._write_marker
        state = {"n": 0}

        def fail_updates(path, payload):
            state["n"] += 1
            if state["n"] >= 2:
                raise OSError("injected: every marker update after the stamp fails")
            return real_write(path, payload)

        with mock.patch.object(pp, "_write_marker", side_effect=fail_updates), \
                mock.patch.object(pp.shutil, "rmtree",
                                  side_effect=OSError("injected: cannot remove")):
            with self.assertRaises(pp.ProductionCleanupError) as caught:
                self.provision()
        err = caught.exception
        self.assertEqual(os.path.realpath(err.live_root), os.path.realpath(self.live))
        self.assertIsNotNone(err.__cause__)
        marker = self.marker()
        # The record that survived predates the first root, and still names it.
        self.assertEqual(sorted(marker["planned_paths"]), sorted(self.roots()))
        on_disk = [p for p in self.roots() if os.path.exists(p)]
        named = pp.owned_paths(marker)
        for path in on_disk:
            with self.subTest(path):
                self.assertIn(path, named,
                              "a path exists that the surviving record does not name")

    def test_owned_paths_covers_a_root_created_but_never_recorded(self):
        """The crash window, as the record a crash would leave behind."""
        marker = {"planned_paths": self.roots(), "created_paths": [], "contested_paths": []}
        self.assertEqual(sorted(pp.owned_paths(marker)), sorted(self.roots()))

    def test_owned_paths_excludes_a_contested_path(self):
        """A root that appeared under the lock was not created by this run, so removing it
        would delete data this run has no authority over."""
        first, second = self.roots()
        marker = {"planned_paths": [first, second], "created_paths": [],
                  "contested_paths": [first]}
        self.assertEqual(pp.owned_paths(marker), [second])

    def test_a_root_appearing_under_the_lock_is_refused_and_recorded_as_contested(self):
        real_makedirs = pp.os.makedirs

        def racing(path, *a, **kw):
            if os.path.basename(path) == pp.MANIFEST_DIRNAME and not os.path.exists(path):
                real_makedirs(path)                   # a racing creator wins
            return real_makedirs(path, *a, **kw)

        with mock.patch.object(pp.os, "makedirs", side_effect=racing):
            with self.assertRaises(pp.ProductionProvisionRefused) as caught:
                self.provision()
        self.assertIn("appeared while the lock was held", str(caught.exception))
        self.assertTrue(os.path.isdir(self.roots()[0]),
                        "a contested path was removed by this run")


class TestTheCrashWindowIsRecoverableNotAtomic(_ProdBase):
    """Every boundary between creating a root and recording ownership, as a recovery case.

    None of this makes provisioning atomic. What it makes it is RECOVERABLE: whatever write did
    not happen, a marker that predates every root names every root, so one `rollback_production`
    call finishes the job. These are the non-crash equivalents — the on-disk states a crash
    would leave, constructed directly and then recovered."""

    def _crashed_marker(self, *, created):
        """The marker as it would stand after a crash at a given point."""
        marker_path = os.path.join(self.live, pp.marker_name(self.RUN_ID))
        record = {"label": pp.PRODUCTION_LABEL,
                  "marker": f"{pp.PRODUCTION_LABEL}:{self.RUN_ID}:t:tests",
                  "run_id": self.RUN_ID, "live_root": os.path.realpath(self.live),
                  "planned_paths": self.roots(), "created_paths": list(created),
                  "contested_paths": [], "complete": False, "failed_step": None}
        pp._write_marker(marker_path, record)
        return record

    def test_crash_after_the_stamp_before_any_root(self):
        record = self._crashed_marker(created=[])
        pp.rollback_production(record)
        self.assertFalse(os.path.exists(os.path.join(self.live, pp.marker_name(self.RUN_ID))))
        for path in self.roots():
            with self.subTest(path):
                self.assertFalse(os.path.exists(path))
        # The monolith is pre-existing production data and is never this run's to remove.
        self.assertTrue(os.path.isdir(self.monolith))

    def test_crash_after_the_first_root_before_it_was_recorded(self):
        """The window the planned list exists for."""
        first = self.roots()[0]
        os.makedirs(first)
        record = self._crashed_marker(created=[])       # the update never happened
        pp.rollback_production(record)
        self.assertFalse(os.path.exists(first), "the unrecorded root survived recovery")
        self.assertFalse(os.path.exists(os.path.join(self.live, pp.marker_name(self.RUN_ID))))

    def test_crash_after_the_second_root_before_it_was_recorded(self):
        first, second = self.roots()
        os.makedirs(first)
        os.makedirs(second)
        record = self._crashed_marker(created=[first])  # only the first was recorded
        pp.rollback_production(record)
        for path in (first, second):
            with self.subTest(path):
                self.assertFalse(os.path.exists(path))

    def test_crash_after_both_roots_were_recorded(self):
        first, second = self.roots()
        os.makedirs(first)
        os.makedirs(second)
        record = self._crashed_marker(created=[first, second])
        pp.rollback_production(record)
        for path in (first, second):
            with self.subTest(path):
                self.assertFalse(os.path.exists(path))

    def test_recovery_never_touches_anything_else_in_the_live_root(self):
        witness = os.path.join(self.live, "pre_existing_production_file")
        with open(witness, "w") as fh:
            fh.write("not ours")
        os.makedirs(self.roots()[0])
        record = self._crashed_marker(created=[])
        pp.rollback_production(record)
        self.assertTrue(os.path.isfile(witness))
        self.assertTrue(os.path.isdir(self.live))

    def test_the_marker_write_is_durable_against_a_lost_rename(self):
        """The protocol rests on the marker being there after a crash, so the rename is fsynced
        as well as the contents. Checked by observing the directory fsync rather than by
        trusting that it happens."""
        synced = []
        real_open, real_fsync = os.open, os.fsync
        fds = {}

        def watching_open(path, flags, *a, **kw):
            fd = real_open(path, flags, *a, **kw)
            if os.path.isdir(path):
                fds[fd] = path
            return fd

        def watching_fsync(fd):
            if fd in fds:
                synced.append(fds[fd])
            return real_fsync(fd)

        with mock.patch.object(pp.os, "open", side_effect=watching_open), \
                mock.patch.object(pp.os, "fsync", side_effect=watching_fsync):
            self.provision()
        self.assertTrue(synced, "no directory was fsynced, so a marker rename can be lost")
        self.assertIn(os.path.realpath(self.live),
                      [os.path.realpath(p) for p in synced])

    def test_the_module_does_not_claim_provisioning_is_atomic(self):
        import inspect
        doc = inspect.getdoc(pp) or ""
        self.assertNotIn("atomic provisioning", doc.lower().replace("not atomic", ""))
        self.assertIn("crash", doc.lower())


class TestFailureAtEveryNewStep(_ProdBase):
    """Publish, WAL init and the completion write each fail, and each leaves a clean outcome.

    Two acceptable outcomes and no third: everything this run created is removed and verified
    absent, or it remains under an accurate marker with a typed `ProductionCleanupError`. What
    must never happen is a root on disk that nothing names."""

    def assert_clean_or_owned(self, error=None):
        roots_present = [p for p in self.roots() if os.path.exists(p)]
        marker_path = os.path.join(self.live, pp.marker_name(self.RUN_ID))
        if not roots_present:
            self.assertFalse(os.path.exists(marker_path),
                             "everything was removed but the marker was kept")
            return
        self.assertTrue(os.path.isfile(marker_path),
                        f"{roots_present} remain and no marker names them")
        named = pp.owned_paths(self.marker())
        for path in roots_present:
            self.assertIn(path, named, "a surviving root is not named by the marker")
        self.assertIsInstance(error, pp.ProductionCleanupError)
        self.assertEqual(sorted(error.unrestored), sorted(roots_present))

    def test_a_failure_publishing_the_manifest_leaves_nothing(self):
        with mock.patch.object(pp.p5_legacy_base, "publish_prepared",
                               side_effect=RuntimeError("injected: publish failed")):
            with self.assertRaises(RuntimeError):
                self.provision()
        self.assert_clean_or_owned()
        self.assertTrue(os.path.isdir(self.monolith), "the monolith was removed")

    def test_a_failure_initializing_the_wal_leaves_nothing(self):
        with mock.patch.object(pp.repair_wal, "initialize_wal",
                               side_effect=RuntimeError("injected: wal init failed")):
            with self.assertRaises(RuntimeError):
                self.provision()
        self.assert_clean_or_owned()

    def test_a_failure_at_the_completion_marker_leaves_nothing(self):
        real_write = pp._write_marker

        def fail_on_complete(path, payload):
            if payload.get("complete") is True:
                raise OSError("injected: completion marker failed")
            return real_write(path, payload)

        with mock.patch.object(pp, "_write_marker", side_effect=fail_on_complete):
            with self.assertRaises(OSError):
                self.provision()
        self.assert_clean_or_owned()

    def test_a_publish_failure_whose_cleanup_also_fails_is_typed_and_accurate(self):
        boom = RuntimeError("injected: publish failed")
        with mock.patch.object(pp.p5_legacy_base, "publish_prepared", side_effect=boom), \
                mock.patch.object(pp.shutil, "rmtree",
                                  side_effect=OSError("injected: cannot remove")):
            with self.assertRaises(pp.ProductionCleanupError) as caught:
                self.provision()
        self.assert_clean_or_owned(caught.exception)
        self.assertIs(caught.exception.__cause__, boom)

    def test_a_wal_failure_whose_cleanup_also_fails_is_typed_and_accurate(self):
        boom = RuntimeError("injected: wal init failed")
        with mock.patch.object(pp.repair_wal, "initialize_wal", side_effect=boom), \
                mock.patch.object(pp.shutil, "rmtree",
                                  side_effect=OSError("injected: cannot remove")):
            with self.assertRaises(pp.ProductionCleanupError) as caught:
                self.provision()
        self.assert_clean_or_owned(caught.exception)
        self.assertIs(caught.exception.__cause__, boom)

    def test_a_published_manifest_is_removed_with_its_root(self):
        """A failure after the publish must not leave a manifest behind: the block root is
        removed whole, and the manifest lives inside it."""
        with mock.patch.object(pp.repair_wal, "initialize_wal",
                               side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.provision()
        self.assertFalse(os.path.exists(
            os.path.join(self.live, pp.MANIFEST_DIRNAME, "manifest.json")))

    def test_a_second_run_after_a_clean_failure_can_start_again(self):
        """The point of removing everything: the next attempt is not blocked by leftovers."""
        with mock.patch.object(pp.repair_wal, "initialize_wal",
                               side_effect=RuntimeError("injected")):
            with self.assertRaises(RuntimeError):
                self.provision()
        out = self.provision()
        self.assertIs(out["complete"], True)
        self.assertIs(out["wal_initialized"], True)


class TestTheFenceBeforePublishing(_ProdBase):
    """`assert_still_held()` immediately before the publish, checked by order.

    This was lost when the publishing class was rewritten, and a mutation deleting the fence
    then survived. Order, not count: the roots and the WAL init also assert the lock, so "an
    assert happened at some point" is satisfied by them and says nothing about the publish —
    after which the manifest is already the store's authority and the fence has nothing left
    to protect."""

    def test_a_fence_check_immediately_precedes_the_publish(self):
        real_assert = cl.CompactionLock.assert_still_held
        real_publish = pp.p5_legacy_base.publish_prepared
        order = []

        def rec_assert(self_):
            order.append("assert")
            return real_assert(self_)

        def rec_publish(*a, **kw):
            order.append("publish")
            return real_publish(*a, **kw)

        with mock.patch.object(cl.CompactionLock, "assert_still_held", rec_assert), \
                mock.patch.object(pp.p5_legacy_base, "publish_prepared",
                                  side_effect=rec_publish):
            self.provision()
        self.assertIn("publish", order)
        idx = order.index("publish")
        self.assertGreater(idx, 0, f"publish came first (order: {order})")
        self.assertEqual(order[idx - 1], "assert",
                         f"the event before publishing was not a fence check: {order}")

    def test_a_fence_check_also_precedes_the_wal_initialization(self):
        """The publish is recorded too, deliberately.

        Without it the sequence is just asserts and the WAL, so the PUBLISH's fence sits
        immediately before `wal` in the recording and deleting the WAL's own fence changes
        nothing visible. With the publish in the sequence, the event before `wal` must be an
        assert that comes after it."""
        real_assert = cl.CompactionLock.assert_still_held
        real_wal = pp.repair_wal.initialize_wal
        real_publish = pp.p5_legacy_base.publish_prepared
        order = []

        def rec_assert(self_):
            order.append("assert")
            return real_assert(self_)

        def rec_publish(*a, **kw):
            order.append("publish")
            return real_publish(*a, **kw)

        def rec_wal(*a, **kw):
            order.append("wal")
            return real_wal(*a, **kw)

        with mock.patch.object(cl.CompactionLock, "assert_still_held", rec_assert), \
                mock.patch.object(pp.p5_legacy_base, "publish_prepared",
                                  side_effect=rec_publish), \
                mock.patch.object(pp.repair_wal, "initialize_wal", side_effect=rec_wal):
            self.provision()
        idx = order.index("wal")
        self.assertEqual(order[idx - 1], "assert", f"order: {order}")
        self.assertGreater(idx - 1, order.index("publish"),
                           f"the fence before the WAL is the publish's, not its own: {order}")


class TestThePartialSealIsRefused(_ProdBase):
    """A seal missing a field is not a seal, and must not be treated as one."""

    def test_a_seal_missing_any_required_field_is_refused(self):
        out = self.provision()
        full = dict(bm.load_live(out["manifest_root"])["segments"][0]["content_seal"])
        for field in ("algo", "ledger_digest", "file_count", "total_bytes"):
            with self.subTest(field):
                partial = {k: v for k, v in full.items() if k != field}
                problem = bm.verify_content_seal(self.monolith, partial)
                self.assertTrue(problem, f"a seal with no {field} was accepted")
                self.assertIn(field, problem)

    def test_a_seal_that_is_not_an_object_is_refused(self):
        for value in (None, "x", [], 3):
            with self.subTest(repr(value)):
                self.assertTrue(bm.verify_content_seal(self.monolith, value))

    def test_a_complete_seal_still_verifies(self):
        """Positive control: the refusals above are not a blanket one."""
        out = self.provision()
        full = bm.load_live(out["manifest_root"])["segments"][0]["content_seal"]
        self.assertIsNone(bm.verify_content_seal(self.monolith, full))


class TestTheExpensiveScanIsOutsideTheIngestLock(_ProdBase):
    """The ingest lock covers the authority change, not the audit that precedes it.

    `content_ledger()` walks every file in the monolith and `inspect_store_contract()` reads
    its metadata. Both ran INSIDE the ingest lock, which on VM24 means the daily-append cron
    blocks behind a scan of ~2 TB that has nothing to do with it. Instrumentation showed all
    four events — two inspections, the ledger, and the publish — with the ingest lock held.

    Order is not enough to prove this: what matters is whether the lock was HELD at the moment
    each ran, so each wrapper asks."""

    def _held(self, path):
        return cl.refuse_if_compaction_running(path, operation="probe") is not None

    def _observe(self):
        events = []
        real = {"insp": bm.inspect_store_contract, "led": bm.content_ledger,
                "pub": bm.publish}

        def wrap(name, fn):
            def inner(*a, **kw):
                events.append((name, self._held(self.ilock), self._held(self.clock)))
                return fn(*a, **kw)
            return inner

        with mock.patch.object(bm, "inspect_store_contract",
                               side_effect=wrap("inspect", real["insp"])), \
                mock.patch.object(bm, "content_ledger",
                                  side_effect=wrap("ledger", real["led"])), \
                mock.patch.object(bm, "publish", side_effect=wrap("publish", real["pub"])):
            self.provision()
        return events

    def test_the_content_ledger_runs_without_the_ingest_lock(self):
        for name, ingest_held, _c in self._observe():
            if name == "ledger":
                self.assertFalse(ingest_held,
                                 "the whole-store file walk ran inside the ingest lock")

    def test_the_store_inspection_runs_without_the_ingest_lock(self):
        """Except the final cheap re-read immediately before the publish, which is the point of
        splitting prepare from publish: metadata only, O(arrays) not O(files)."""
        events = self._observe()
        pub = [i for i, e in enumerate(events) if e[0] == "publish"][0]
        for i, (name, ingest_held, _c) in enumerate(events):
            if name == "inspect" and i < pub - 1:
                with self.subTest(index=i):
                    self.assertFalse(ingest_held,
                                     "a store inspection ran inside the ingest lock")

    def test_the_publish_runs_WITH_the_ingest_lock(self):
        events = self._observe()
        published = [e for e in events if e[0] == "publish"]
        self.assertEqual(len(published), 1)
        self.assertTrue(published[0][1], "the publish ran without the ingest lock")

    def test_the_compaction_lock_is_held_for_all_of_it(self):
        for name, _i, compaction_held in self._observe():
            with self.subTest(name):
                self.assertTrue(compaction_held,
                                f"{name} ran without the compaction lock")

    def test_the_final_validation_precedes_the_publish_inside_the_lock(self):
        """Something must look again between preparing and publishing, or the manifest could
        describe a store that changed in between."""
        order = []
        real_validate = pp.p5_legacy_base.validate_prepared
        real_publish = bm.publish

        def rec_validate(prepared):
            order.append(("validate", self._held(self.ilock)))
            return real_validate(prepared)

        def rec_publish(*a, **kw):
            order.append(("publish", self._held(self.ilock)))
            return real_publish(*a, **kw)

        with mock.patch.object(pp.p5_legacy_base, "validate_prepared",
                               side_effect=rec_validate), \
                mock.patch.object(bm, "publish", side_effect=rec_publish):
            self.provision()
        self.assertEqual([n for n, _ in order], ["validate", "publish"])
        self.assertTrue(all(held for _, held in order),
                        "the final validation must be inside the lock, with the publish")

    def test_a_monolith_that_changes_between_prepare_and_publish_refuses(self):
        """The window the split opens, closed by the final validation."""
        import zarr
        real_prepare = pp.p5_legacy_base.prepare_generation_one

        def prepare_then_change(**kw):
            prepared = real_prepare(**kw)
            self.unseal_monolith()
            # A WELL-FORMED change: the lon axis moves, so the store still satisfies the
            # contract and `inspect_store_contract` still succeeds -- but its metadata
            # fingerprint differs. Resizing the day arrays instead leaves `var_valid` one flag
            # short, and the inspection refuses with a different error before the drift
            # comparison is reached.
            g = zarr.open_group(self.monolith, mode="a")
            g["lon"][:] = np.asarray(g["lon"][:]) + 0.5
            # Re-sealed at the SAME mode, so the seal check passes and the drift comparison is
            # what refuses. Leaving it writable would be caught by `check_monolith_sealed`
            # first, and this test would pass without exercising the branch it is about.
            os.chmod(self.monolith, 0o555)
            return prepared

        with mock.patch.object(pp.p5_legacy_base, "prepare_generation_one",
                               side_effect=prepare_then_change):
            with self.assertRaises(pp.p5_legacy_base.LegacyBaseRefused) as caught:
                self.provision()
        self.assertIn("between preparation and publish", str(caught.exception))
        for path in self.roots():
            with self.subTest(path):
                self.assertFalse(os.path.exists(path), "a root survived the refusal")


class TestTheMonolithMustBeSealedFirst(_ProdBase):
    """`immutable: true` is a declaration. This is the check that stands behind it."""

    def test_a_writable_monolith_is_refused(self):
        self.unseal_monolith()
        with self.assertRaises(pp.p5_legacy_base.LegacyBaseRefused) as caught:
            self.provision()
        self.assertIn("is writable", str(caught.exception))

    def test_the_refusal_happens_before_the_roots_survive(self):
        self.unseal_monolith()
        with self.assertRaises(pp.p5_legacy_base.LegacyBaseRefused):
            self.provision()
        for path in self.roots():
            with self.subTest(path):
                self.assertFalse(os.path.exists(path))

    def test_the_seal_evidence_is_recorded_with_what_it_does_not_establish(self):
        out = self.provision()
        evidence = out["legacy_base"]["seal_evidence"]
        self.assertIs(evidence["writable_bits"], False)
        self.assertIn("store root permission bits only", evidence["checked"])
        self.assertIn("read-only mount", evidence["not_established"])

    def test_becoming_writable_between_prepare_and_publish_refuses(self):
        real_prepare = pp.p5_legacy_base.prepare_generation_one

        def prepare_then_unseal(**kw):
            prepared = real_prepare(**kw)
            self.unseal_monolith()
            return prepared

        with mock.patch.object(pp.p5_legacy_base, "prepare_generation_one",
                               side_effect=prepare_then_unseal):
            with self.assertRaises(pp.p5_legacy_base.LegacyBaseRefused) as caught:
                self.provision()
        self.assertIn("is writable", str(caught.exception))

    def test_a_DIFFERENT_read_only_mode_between_prepare_and_publish_refuses(self):
        """The mode comparison, isolated.

        Becoming writable is caught by the seal check first, so it says nothing about this
        branch. A store that went 0o555 -> 0o500 is still sealed, and is still not the store
        that was prepared."""
        real_prepare = pp.p5_legacy_base.prepare_generation_one

        def prepare_then_chmod(**kw):
            prepared = real_prepare(**kw)
            os.chmod(self.monolith, 0o500)
            return prepared

        with mock.patch.object(pp.p5_legacy_base, "prepare_generation_one",
                               side_effect=prepare_then_chmod):
            with self.assertRaises(pp.p5_legacy_base.LegacyBaseRefused) as caught:
                self.provision()
        self.assertIn("changed permissions", str(caught.exception))

    def test_the_seal_check_is_documented_as_necessary_not_sufficient(self):
        import inspect as _i
        doc = _i.getdoc(pp.p5_legacy_base.check_monolith_sealed) or ""
        self.assertIn("necessary barrier, not a sufficient one", doc)
        self.assertIn("root ignores permissions", doc)
        self.assertIn("read-only mount", doc)

    def test_the_content_seal_cannot_see_a_restored_mtime_and_size(self):
        """The documented limitation, demonstrated rather than asserted.

        The ledger hashes path, size and mtime. A write that restores all three is invisible to
        it — which is why the seal is called change detection and why the read-only barrier is
        the thing that actually matters."""
        import zarr
        out = self.provision()
        seal = bm.load_live(out["manifest_root"])["segments"][0]["content_seal"]
        self.assertIsNone(bm.verify_content_seal(self.monolith, seal))
        self.unseal_monolith()
        target = None
        for dirpath, _d, files in os.walk(self.monolith):
            for name in files:
                full = os.path.join(dirpath, name)
                if os.path.getsize(full) > 0:
                    target = full
                    break
            if target:
                break
        self.assertIsNotNone(target)
        st = os.stat(target)
        with open(target, "r+b") as fh:
            data = bytearray(fh.read())
            data[0] = (data[0] + 1) % 256          # same length, different bytes
            fh.seek(0)
            fh.write(bytes(data))
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual(os.stat(target).st_size, st.st_size)
        self.assertIsNone(bm.verify_content_seal(self.monolith, seal),
                          "the ledger detected a restored-metadata write; the documented "
                          "limitation no longer holds and the docs should change")
        del zarr


class TestDaySetDriftIsCaughtOnItsOwn(_ProdBase):
    """The day-digest branch of the final validation, isolated.

    `metadata_fingerprint` deliberately excludes the day set — but it includes array shape and
    `var_valid` length, so ADDING a day trips the metadata branch and says nothing about this
    one. RENAMING a day keeps the shape, the flag count and the flag contents identical, so the
    metadata fingerprint is unchanged and only `day_digest` differs."""

    def test_a_renamed_day_between_prepare_and_publish_refuses(self):
        import zarr
        real_prepare = pp.p5_legacy_base.prepare_generation_one
        observed = {}

        def prepare_then_rename(**kw):
            prepared = real_prepare(**kw)
            observed["before"] = prepared["recheck"]["metadata"]
            self.unseal_monolith()
            g = zarr.open_group(self.monolith, mode="a")
            renamed = list(self.mono_days)
            renamed[-1] = "2026-12-31"                 # same count, different day set
            g.attrs["days"] = renamed
            os.chmod(self.monolith, 0o555)
            observed["after"] = bm.metadata_fingerprint_from_inspection(
                bm.inspect_store_contract(self.monolith))
            return prepared

        with mock.patch.object(pp.p5_legacy_base, "prepare_generation_one",
                               side_effect=prepare_then_rename):
            with self.assertRaises(pp.p5_legacy_base.LegacyBaseRefused) as caught:
                self.provision()
        self.assertEqual(observed["before"], observed["after"],
                         "the metadata fingerprint moved too; this does not isolate the "
                         "day-digest branch")
        self.assertIn("day set changed between preparation and publish",
                      str(caught.exception))

    def test_that_refusal_leaves_no_roots(self):
        real_prepare = pp.p5_legacy_base.prepare_generation_one

        def rename(**kw):
            import zarr as z
            prepared = real_prepare(**kw)
            self.unseal_monolith()
            g = z.open_group(self.monolith, mode="a")
            renamed = list(self.mono_days)
            renamed[0] = "2025-01-01"
            g.attrs["days"] = renamed
            os.chmod(self.monolith, 0o555)
            return prepared

        with mock.patch.object(pp.p5_legacy_base, "prepare_generation_one", side_effect=rename):
            with self.assertRaises(pp.p5_legacy_base.LegacyBaseRefused):
                self.provision()
        for path in self.roots():
            with self.subTest(path):
                self.assertFalse(os.path.exists(path))
