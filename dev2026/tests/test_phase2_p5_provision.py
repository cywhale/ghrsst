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
