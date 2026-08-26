"""dev2026 — the one-shot compaction operation, end to end and in its failure modes.

This is the operation that makes the old daily/base space *releasable*. It does not release it,
and the first implementation removes nothing at all — a fact this suite asserts from several
directions, because "we did not mean to delete anything" is not the same as "nothing can".

The three properties the suite is built around:

* **The gates are enforced, not merely callable.** An earlier revision reported
  `post_compaction_enforcement() -> enforced_by: None` and was right to. Now the operation runs
  `post_compaction_problem()` over evidence it assembled from its own state, and a failure
  refuses the run and issues no clearance. `TestAFailedGateStopsEverythingDownstream` injects a
  failure into each gate in turn.
* **The fence spans the whole scan-to-publish window.** A writer count is an observation about
  an instant; the interval is minutes long. The ingest lock is taken before the scan and
  borrowed by the publish, and the writer fence is probed three times and compared.
* **Nothing destructive is reachable.** Not by the operation, and not by anything holding its
  clearance without a separate human approval.
"""
from __future__ import annotations

import ast
import datetime
import inspect
import json
import os
import shutil
import socket
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
for p in (_DEV, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import p5_fixtures as fx  # noqa: E402
from ingest import publish_manifest as pub  # noqa: E402
from ops import p5_legacy_base as lb  # noqa: E402
from ops import p5_oneshot_compaction as oc  # noqa: E402
from ops import p5_parity_producers as ppd  # noqa: E402
from ops import p5_oneshot_exception as ex  # noqa: E402
from ops import p5_prod_provision as prod  # noqa: E402
from ops import p5_readiness as rd  # noqa: E402
from ops import p5s8_evidence as evd  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store import compaction_lock as cl  # noqa: E402
from store import repair_wal as rw  # noqa: E402
# Two directories on different devices cannot be arranged portably inside a test,
# so the anchor's device is substituted rather than created.
from test_phase2_p5_prod_provision import dev_map  # noqa: E402

SHA = "d" * 40


def iso(offset_s=0):
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=offset_s)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


class _CompactionBase(unittest.TestCase):
    """A live root with a provisioned generation 1 over a WRITABLE monolith.

    Generation 1 is published by the real provisioning operation under a real one-shot
    authorization, so the starting state of every test here is a state the previous milestone
    can actually produce — not one assembled by hand for the convenience of this one."""

    RUN_ID = "20260826T170000Z"
    PROVISION_RUN_ID = "20260826T160000Z"
    TEST_FLOOR = 0.001

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p5-oneshot-compaction-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.live = os.path.join(self.tmp, "G")
        self.anchor = os.path.join(self.tmp, "anchor_domain")
        self.artifacts = os.path.join(self.tmp, "artifacts")
        for d in (self.live, self.anchor, self.artifacts):
            os.makedirs(d)
        rw.declare_anchor_domain(self.anchor, domain_id="p5-oneshot-compaction-tests",
                                 at_utc="2026-08-26T12:00:00Z", operator="tests",
                                 note="unit-test domain; NOT a real rollback domain")

        # The legacy base: the monolith the exception exists for.
        self.base_days = fx.days_from("2026-01-01", 6)
        self.monolith = os.path.join(self.live, "mur_timecube_s8_t90_sh128.zarr")
        fx.build_block(self.monolith, self.base_days, ny=8, nx=8)
        # The days a compaction actually folds. The block grid anchors at the day AFTER the
        # legacy base ends (§12 M0), so a block over the monolith's OWN days is refused --
        # what generation 2 folds is delta, and the space that becomes releasable is the daily
        # store for those days, not the monolith.
        # A block must span a WHOLE grid block, not just the days it materialises: the grid
        # is [anchor, anchor+90). The days actually present are the classification target; the
        # rest of the span is `unknown`.
        self.grid = fx.days_from("2026-01-07", 90)
        self.days = self.grid[:5]
        self.delta = os.path.join(self.live, "mur_timecube_s8_t90_sh128.delta.zarr")
        fx.build_block(self.delta, self.days, ny=8, nx=8)

        self.locks = prod.canonical_lock_paths(self.live)
        self.clock, self.ilock = self.locks["compaction"], self.locks["ingest"]
        self.manifest_root = os.path.join(self.live, prod.MANIFEST_DIRNAME)
        self.wal_root = os.path.join(self.live, prod.WAL_DIRNAME)

        self.addCleanup(setattr, evd, "MINIMUM_FREE_GIB", evd.MINIMUM_FREE_GIB)
        evd.MINIMUM_FREE_GIB = self.TEST_FLOOR

        self.devs = {self.anchor: 4242}
        self._provision_generation_one()
        self.probe_calls = []

    # ---------------------------------------------------------------- the starting state
    def _provision_generation_one(self):
        """Publish generation 1 through the real operation, under a real authorization."""
        os.chmod(self.monolith, 0o755)          # writable: the whole reason for the exception
        with dev_map({self.anchor: 4242}):
            prod.provision_production(
                live_root=self.live, anchor_root=self.anchor,
                approval={"scope": prod.PRODUCTION_SCOPE, "approved_by": "cywhale",
                          "approved_utc": "2026-08-26T12:00:00Z", "live_root": self.live,
                          "run_id": self.PROVISION_RUN_ID},
                monolith_path=self.monolith,
                compaction_lock_path=self.clock, ingest_lock_path=self.ilock,
                run_id=self.PROVISION_RUN_ID, operator="tests",
                at_utc="2026-08-26T12:00:00Z", min_free_gib=self.TEST_FLOOR,
                one_shot_exception=self._record(run_id=self.PROVISION_RUN_ID,
                                                operation=ex.OP_PROVISION),
                one_shot_pre_compaction=self._pre(run_id=self.PROVISION_RUN_ID))

    # ---------------------------------------------------------------- evidence builders
    def _fence(self, **over):
        f = {
            "method": "pm2 stop of the writer-producing services + crontab commented out",
            "services_stopped": [{"name": "mur-delta-append", "posture": "stopped"},
                                 {"name": "mur-daily-ingest", "posture": "disabled"}],
            "cron_disabled": "commented-out",
            "flock_is_advisory": True,
            "arbitrary_writer_policy": ("no shell writes for the window; the content ledger "
                                        "recheck detects one that happened anyway"),
            "observed_utc": iso(-30),
            "verified": True,
        }
        f.update(over)
        return f

    def _record(self, *, run_id=None, operation=None, **over):
        m = ex.measure_monolith(self.monolith)
        r = {
            "scope": ex.ONESHOT_SCOPE, "operation": operation or ex.OP_COMPACTION,
            "host": socket.gethostname(), "run_id": run_id or self.RUN_ID,
            "monolith_realpath": m["monolith_realpath"],
            "monolith_metadata_fingerprint": m["monolith_metadata_fingerprint"],
            "monolith_day_digest": m["monolith_day_digest"],
            "monolith_content_ledger": dict(m["monolith_content_ledger"]),
            "backup_id": "vm24-daily-20260826T0300Z",
            "backup_completed_utc": iso(-7200), "backup_verified": True,
            "backup_retention_ref": "retained 30 days; restore runbook OPS-114",
            "expires_utc": iso(3000), "approved_by": "cywhale", "approved_utc": iso(-600),
            "reason": ("one fold wanted before the storage team can move the monolith to its "
                       "own read-only mount; VM backups make it recoverable"),
            "residual_writer_risk": {
                "acknowledged": True,
                "unrecoverable_outcome_possible": True,
                "statement": ("an unfenced writer can change the monolith during the "
                              "scan-to-publish interval; the gates detect it afterwards and a "
                              "VM restore does not undo what was served in between"),
                "accepted_by": "cywhale",
            },
        }
        r.update(over)
        return r

    def _pre(self, *, run_id=None, **over):
        m = ex.measure_monolith(self.monolith)
        p = {
            "host": socket.gethostname(), "run_id": run_id or self.RUN_ID,
            "collected_utc": iso(-60),
            "capacity": {"free_now_gib": 1252.1,
                         "filesystem_dev": os.stat(self.live).st_dev,
                         "measured_utc": iso(-60)},
            "healthz": {"status": "ok", "latest": self.days[-1], "route": "/point"},
            "identity": {"reviewed_sha": SHA, "worktree_clean": True, "verified": True,
                         "approved": True,
                         "module_fingerprints": {"ops/p5_oneshot_compaction.py": "e" * 64}},
            "quiescence": {"pm2": "stopped", "cron": "disabled", "readers": 0,
                           "reader_policy": "readers allowed; the old generation stays served "
                                            "until a separate switch",
                           "verified": True},
            "monolith_writers": {"count": 0, "method": "lsof over the store"},
            "writer_fence": self._fence(),
            "locks": {"compaction": "free", "ingest": "free", "verified": True,
                      "canonical_paths": {"compaction": os.path.realpath(self.clock),
                                          "ingest": os.path.realpath(self.ilock)},
                      "acquisition_probe": {"compaction": "acquired_and_released",
                                            "ingest": "acquired_and_released"}},
            "monolith_state": {k: m[k] for k in ex.MONOLITH_STATE_FIELDS},
            "backup": {"backup_id": "vm24-daily-20260826T0300Z", "verified": True,
                       "method": "restore-catalogue listing", "verified_utc": iso(-3600)},
            "plan": {"source_map": {d: "delta" for d in self.days},
                     "destination_manifest": "gen000002", "days": list(self.days),
                     # The external paths this run may read. The approval names the live root;
                     # anything outside it has to be named by the approved preflight or the
                     # block could be folded from a store nobody approved.
                     "paths": {"monolith": os.path.realpath(self.monolith),
                               "delta_path": os.path.realpath(self.delta),
                               "daily_root": None}},
        }
        p.update(over)
        return p

    def _approval(self, **over):
        a = {"scope": oc.COMPACTION_SCOPE, "approved_by": "cywhale",
             "approved_utc": iso(-600), "live_root": self.live, "run_id": self.RUN_ID}
        a.update(over)
        return a

    def _identity(self, **over):
        """A real identity artifact: the fingerprints are the blobs git holds at the approved
        commit, so nothing here describes itself."""
        sha = rd.REVIEWED_DEPLOY_SHAS[-1]
        a = {
            "host": socket.gethostname(), "run_id": self.RUN_ID,
            "deploy_sha": sha, "head_sha": sha, "worktree_clean": True,
            # The deployment inventory at the approved commit, PLUS the operation's own
            # producer module -- which postdates that commit, so it is fingerprinted from disk
            # and checked against disk. See the limitation in `identity_problem`.
            "module_fingerprints": {
                **rd.expected_module_digests(sha),
                **{m: oc._file_digest(os.path.join(_DEV, m))
                   for m in oc.REVIEWED_PRODUCER_MODULES},
            },
            "collected_utc": iso(-30),
            "verified_by": "ops tooling on the deployment host",
        }
        a.update(over)
        return a

    def _producer_digest(self):
        """A digest belonging to a module the identity artifact actually fingerprinted.

        A real parity producer is reviewed code shipped with the deployment; here that is
        represented by an inventory module, so the binding is exercised for real rather than
        against a value invented for the test."""
        return self._identity()["module_fingerprints"]["store/source_provenance.py"]

    # ---------------------------------------------------------------- injected probes
    def _fence_probe(self):
        self.probe_calls.append(iso())
        return self._fence()

    def _parity(self, **over):
        """A producer returning a fully BOUND artifact, as a reviewed one would."""
        def probe(*, block_path, source_map, monolith_path, days, host, run_id, deploy_sha,
                  generation, manifest_checksum, source_map_digest):
            # A real probe reads each day from the store the source map names and compares it
            # point-wise against the block. The oracle differs per day, which is the whole
            # reason the map is passed in.
            oracles = sorted({r["source_path"] for r in source_map.values()})
            out = {
                "kind": oc.PARITY_POINT_SERIES,
                "host": host, "run_id": run_id, "deploy_sha": deploy_sha,
                "producer": "store/source_provenance.compare_days",
                "producer_digest": self._producer_digest(),
                "generation": generation, "manifest_checksum": manifest_checksum,
                "source_map_digest": source_map_digest,
                "executed_utc": iso(), "samples": len(days) * 8, "mismatches": 0,
                "provenance": (f"per-day window fingerprints read from {oracles} and compared "
                               f"point-wise against {block_path}"),
                # the shape the pre-publish gate reads
                "days_checked": len(days), "boundaries_included": True,
                "first_day": days[0], "last_day": days[-1],
            }
            out.update(over)
            return out
        return probe

    def _api_parity(self, **over):
        def probe(*, manifest_root, monolith_path, allowed_legacy_paths, host, run_id,
                  deploy_sha, generation, manifest_checksum, source_map_digest):
            out = {
                "kind": oc.PARITY_API,
                "host": host, "run_id": run_id, "deploy_sha": deploy_sha,
                "producer": "store/source_provenance.compare_days",
                "producer_digest": self._producer_digest(),
                "generation": generation, "manifest_checksum": manifest_checksum,
                "source_map_digest": source_map_digest,
                "executed_utc": iso(), "samples": 24, "mismatches": 0,
                "provenance": (f"the deployment reader over {manifest_root}, compared against "
                               f"the pre-publish baseline"),
                "routes_checked": 4,
            }
            out.update(over)
            return out
        return probe

    # ---------------------------------------------------------------- forcing real failures
    # The producers can no longer be faked -- `producer_identity` refuses anything that is not
    # the registered module -- so a failure is injected INTO the real producer instead. Each of
    # these makes the genuine code path report a genuine problem, which is a better test than
    # a stub returning the answer.
    def forced_api_mismatch(self):
        """Make every served/oracle comparison disagree."""
        return mock.patch.object(ppd, "_close", return_value=False)

    def forced_point_series_mismatch(self):
        """Make the comparison primitive report a difference for every day."""
        return mock.patch.object(ppd.sp, "compare_days",
                                 return_value=["forced difference"])

    def forced_api_exception(self):
        """Make the served reader itself blow up, inside the producer."""
        import store.segmented_cube as sc
        return mock.patch.object(sc, "SegmentedCubeStore",
                                 side_effect=RuntimeError("served reader exploded"))

    def _next_block_path(self):
        """A fresh path per call. `build_block` refuses to reuse one, correctly: a block path is
        written once and never reused, so a test that calls the operation twice must not ask it
        to break that."""
        self._block_seq = getattr(self, "_block_seq", 0) + 1
        return os.path.join(self.manifest_root, f"block_gen2_{self._block_seq}.zarr")

    # ---------------------------------------------------------------- the call
    def compact(self, **over):
        kw = dict(
            live_root=self.live, manifest_root=self.manifest_root, wal_root=self.wal_root,
            anchor_root=self.anchor, monolith_path=self.monolith,
            compaction_lock_path=self.clock, ingest_lock_path=self.ilock,
            approval=self._approval(), run_id=self.RUN_ID, operator="tests",
            at_utc=iso(), min_free_gib=self.TEST_FLOOR,
            start_day=self.grid[0], end_day=self.grid[-1],
            classification_target=list(self.days), delta_path=self.delta,
            artifacts_dir=self.artifacts, hard_reserve_bytes=1024,
            block_out_path=self._next_block_path(),
            writer_fence_probe=self._fence_probe,
            # The REAL reviewed producers. A fake is refused by `producer_identity`, which
            # is the whole point -- so the positive control has to be the real path.
            point_series_parity=ppd.point_series_parity,
            api_parity=ppd.api_parity,
        )
        if "identity_artifact" not in over:
            kw["identity_artifact"] = self._identity()
        if "one_shot_exception" not in over:
            kw["one_shot_exception"] = self._record()
        if "one_shot_pre_compaction" not in over:
            kw["one_shot_pre_compaction"] = self._pre()
        kw.update(over)
        with dev_map(self.devs):
            return oc.compact_one_shot(**kw)

    def manifest_generation(self):
        return int(bm.load_live(self.manifest_root)["generation"])


# =====================================================================================
class TestTheOperationIsBoundToItsOwnName(_CompactionBase):

    def test_the_scope_is_its_own(self):
        self.assertNotEqual(oc.COMPACTION_SCOPE, prod.PRODUCTION_SCOPE)

    def test_a_provisioning_approval_is_not_a_compaction_approval(self):
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(approval=self._approval(scope=prod.PRODUCTION_SCOPE))
        self.assertIn("not an approval to publish a compaction generation",
                      str(caught.exception))

    def test_a_provisioning_record_is_refused(self):
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(one_shot_exception=self._record(operation=ex.OP_PROVISION))
        self.assertIn("is not a compaction acceptance", str(caught.exception))

    def test_a_provisioning_capability_cannot_be_used_here(self):
        """The capability TYPE is part of the binding, not just its operation string."""
        cap, _ = ex.authorize_unsealed(
            record=self._record(operation=ex.OP_PROVISION, run_id=self.RUN_ID),
            host=socket.gethostname(), run_id=self.RUN_ID,
            monolith_path=self.monolith, pre=self._pre(),
            expected_dev=os.stat(self.live).st_dev)
        self.assertNotIsInstance(cap, ex.OneShotCompactionAuthorization)

    def test_the_two_mints_refuse_each_other_s_operation(self):
        with self.assertRaises(ex.OneShotExceptionRefused):
            ex.authorize_unsealed(record=self._record(), host=socket.gethostname(),
                                  run_id=self.RUN_ID, monolith_path=self.monolith,
                                  pre=self._pre(), operation=ex.OP_COMPACTION)
        with self.assertRaises(ex.OneShotExceptionRefused):
            ex.authorize_compaction(record=self._record(), host=socket.gethostname(),
                                    run_id=self.RUN_ID, monolith_path=self.monolith,
                                    pre=self._pre(), operation=ex.OP_PROVISION)

    def test_the_run_id_must_match_the_approval(self):
        with self.assertRaises(oc.OneShotCompactionRefused):
            self.compact(approval=self._approval(run_id="some-other-run"))


class TestTheHappyPathPublishesAndDeletesNothing(_CompactionBase):

    def test_it_publishes_the_next_generation(self):
        before = self.manifest_generation()
        result = self.compact()
        self.assertEqual(self.manifest_generation(), before + 1)
        self.assertEqual(result["generation"], before + 1)

    def test_it_deletes_nothing_and_says_so(self):
        result = self.compact()
        self.assertFalse(result["deleted_anything"])
        self.assertFalse(result["service_switched"])
        self.assertIn("NOTHING was deleted", result["note"])

    def test_the_monolith_and_its_days_are_untouched(self):
        """The point of the whole exercise: the old base is still there afterwards."""
        before = ex.measure_monolith(self.monolith)
        self.compact()
        after = ex.measure_monolith(self.monolith)
        self.assertEqual(before["monolith_day_digest"], after["monolith_day_digest"])
        self.assertEqual(before["monolith_content_ledger"]["ledger_digest"],
                         after["monolith_content_ledger"]["ledger_digest"])

    def test_nothing_deleted_is_derived_from_an_inventory_not_asserted(self):
        evidence = self.compact()["post_compaction"]["nothing_deleted"]
        self.assertEqual(evidence["derived_from"], "inventory")
        self.assertTrue(evidence["value"])
        self.assertIn("monolith", evidence["before"])
        self.assertIn("monolith", evidence["after"])
        self.assertIsNone(ex.nothing_deleted_problem(evidence))

    def test_the_capability_is_consumed(self):
        self.assertTrue(self.compact()["capability_consumed"])

    def test_it_issues_a_clearance_bound_to_what_it_published(self):
        result = self.compact()
        clearance = result["finalization_clearance"]
        live = bm.load_live(self.manifest_root)
        self.assertEqual(clearance["generation"], int(live["generation"]))
        self.assertEqual(clearance["manifest_checksum"], live["manifest_checksum"])
        self.assertIsNone(ex.clearance_problem(
            clearance, host=socket.gethostname(), run_id=self.RUN_ID,
            generation=int(live["generation"]),
            manifest_checksum=live["manifest_checksum"]))

    def test_the_clearance_still_does_not_authorize_deletion_on_its_own(self):
        clearance = self.compact()["finalization_clearance"]
        live = bm.load_live(self.manifest_root)
        with self.assertRaises(ex.FinalizationRefused) as caught:
            ex.refuse_destructive_step("prune", clearance, host=socket.gethostname(),
                                       run_id=self.RUN_ID,
                                       generation=int(live["generation"]),
                                       manifest_checksum=live["manifest_checksum"])
        self.assertIn("not a decision to remove anything", str(caught.exception))

    def test_the_post_evidence_is_bound_to_this_run_and_generation(self):
        evidence = self.compact()["post_compaction"]
        self.assertEqual(evidence["run_id"], self.RUN_ID)
        self.assertEqual(evidence["host"], socket.gethostname())
        self.assertEqual(evidence["manifest"]["generation"], self.manifest_generation())


class TestAFailedGateStopsEverythingDownstream(_CompactionBase):
    """Failure injection, one gate at a time. Enforcement means refusing, not reporting."""

    def _assert_refused_without_clearance(self, **over):
        gen_before = self.manifest_generation()
        # Each test cleans up its own quarantine, so the next assertion in the same test is not
        # answered by a leftover record from the previous one.
        self.addCleanup(lambda: os.path.exists(oc.quarantine_path(self.manifest_root))
                        and os.remove(oc.quarantine_path(self.manifest_root)))
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(**over)
        # Whatever happened, no clearance exists -- so nothing downstream can be unlocked.
        with self.assertRaises(ex.FinalizationRefused):
            ex.refuse_destructive_step("prune", None, host=socket.gethostname(),
                                       run_id=self.RUN_ID, generation=gen_before + 1,
                                       manifest_checksum="x", approval=True)
        # And the monolith is still whole.
        self.assertTrue(os.path.isdir(self.monolith))
        return str(caught.exception)

    def test_a_point_series_mismatch_refuses_before_publishing(self):
        gen = self.manifest_generation()
        with self.forced_point_series_mismatch():
            message = self._assert_refused_without_clearance()
        self.assertIn("mismatch(es) out of", message)
        self.assertEqual(self.manifest_generation(), gen,
                         "a parity failure must be caught while the block is unreferenced")

    def test_an_api_parity_mismatch_refuses_and_issues_no_clearance(self):
        with self.forced_api_mismatch():
            message = self._assert_refused_without_clearance()
        self.assertIn("mismatch(es) out of", message)

    def test_a_wal_that_is_not_bound_refuses_and_issues_no_clearance(self):
        """The gate, exercised through the operation.

        The helper is patched rather than the WAL sabotaged: every way of breaking a real WAL
        on disk trips a DIFFERENT and equally correct refusal first (a rolled-back anchor, an
        unreadable log), so a test that broke one would pass on whichever guard happened to
        fire. What this pins is that the operation calls the helper and refuses on its verdict.
        `TestTheWalEvidenceIsRead` covers the helper itself."""
        unbound = {"bound": False, "anchor_intact": True,
                   "reason": "bound to a previous manifest root"}
        with mock.patch.object(oc, "_wal_anchor_evidence",
                               return_value=unbound) as helper:
            message = self._assert_refused_without_clearance()
        self.assertTrue(helper.called, "the operation never read the WAL evidence at all")
        self.assertIn("WAL is not bound", message)

    def test_a_missing_wal_anchor_refuses_and_issues_no_clearance(self):
        no_anchor = {"bound": True, "anchor_intact": False}
        with mock.patch.object(oc, "_wal_anchor_evidence", return_value=no_anchor):
            message = self._assert_refused_without_clearance()
        self.assertIn("anchor is missing", message)

    def test_an_incomplete_source_map_refuses_before_publishing(self):
        gen = self.manifest_generation()
        with mock.patch.object(oc, "_source_map_complete", return_value=False):
            message = self._assert_refused_without_clearance()
        self.assertIn("source map is incomplete", message)
        self.assertEqual(self.manifest_generation(), gen)

    def test_a_base_delta_overlap_refuses_before_publishing(self):
        gen = self.manifest_generation()
        with mock.patch.object(oc, "_base_delta_disjoint", return_value=False):
            message = self._assert_refused_without_clearance()
        self.assertIn("not disjoint", message)
        self.assertEqual(self.manifest_generation(), gen)

    def test_a_capacity_failure_after_the_publish_refuses(self):
        """The real `capacity_after` failure mode.

        A *stale* one is not reachable through the operation: it stamps `measured_utc` itself
        at the moment of the call, precisely so a carried-over figure cannot masquerade as an
        'after' reading. What can happen is the measurement failing — the volume below the
        floor once the block has been written — and that must refuse and clear nothing."""
        real = prod.measure_capacity
        calls = {"n": 0}

        def failing(*a, **kw):
            calls["n"] += 1
            if calls["n"] > 1:
                raise prod.ProductionProvisionRefused("free space fell below the floor")
            return real(*a, **kw)

        with mock.patch.object(prod, "measure_capacity", side_effect=failing):
            with self.assertRaises(oc.GenerationQuarantined) as caught:
                self.compact()
        # It RAISED rather than returning a problem, and it still quarantined -- which is the
        # point: after the authority changed, an escaping exception would leave the generation
        # unclassified for the next run to build on.
        self.assertIn("ProductionProvisionRefused", str(caught.exception))
        self.assertIsNotNone(oc.read_quarantine(self.manifest_root))
        self.assertTrue(os.path.isdir(self.monolith))

    def test_the_gate_refuses_a_capacity_after_that_is_not_freshly_stamped(self):
        """And the gate itself still rejects one, for any other caller assembling evidence."""
        problem = ex.post_compaction_problem({
            "host": "h", "run_id": "r",
            "manifest": {"generation": 2, "checksum_ok": True, "checksum": "f" * 64},
            "wal_anchor": {"bound": True, "anchor_intact": True},
            "source_map_complete": True, "base_delta_disjoint": True,
            "source_map_digest": "9" * 64,
            "point_series_parity": {"days_checked": 3, "mismatches": 0,
                                    "boundaries_included": True},
            "api_parity": {"routes_checked": 2, "mismatches": 0},
            "capacity_after": {"free_now_gib": 1.0},
            "nothing_deleted": {"value": True, "derived_from": "inventory",
                                "before": {"m": {"bytes": 1, "files": 1}},
                                "after": {"m": {"bytes": 1, "files": 1}}},
        })
        self.assertIn("capacity_after", problem)

    def test_a_removal_detected_by_the_inventory_refuses(self):
        """The gate that would catch this operation deleting something despite itself."""
        real_inventory = oc.inventory
        calls = {"n": 0}

        def shrinking(paths):
            calls["n"] += 1
            out = real_inventory(paths)
            if calls["n"] > 1:                      # the AFTER inventory
                for role in out:
                    out[role] = {**out[role], "bytes": 0, "files": 0}
            return out

        with mock.patch.object(oc, "inventory", side_effect=shrinking):
            message = self._assert_refused_without_clearance()
        self.assertIn("data was removed", message)

    def test_a_post_gate_failure_leaves_the_old_data_untouched(self):
        """Why refusing after the publish is survivable: the base is still there."""
        before = ex.measure_monolith(self.monolith)
        with self.forced_api_mismatch():
            with self.assertRaises(oc.OneShotCompactionRefused):
                self.compact()
        after = ex.measure_monolith(self.monolith)
        self.assertEqual(before["monolith_content_ledger"]["ledger_digest"],
                         after["monolith_content_ledger"]["ledger_digest"])

    def test_a_post_gate_failure_says_what_state_it_left(self):
        with self.forced_api_mismatch():
            with self.assertRaises(oc.OneShotCompactionRefused) as caught:
                self.compact()
        message = str(caught.exception)
        self.assertIn("published, NOT verified, and now QUARANTINED", message)
        self.assertIn("nothing was deleted", message)
        self.assertIn("no service was switched", message)
        self.assertIn("is intact", message)


class TestTheWalEvidenceIsRead(_CompactionBase):
    """The helper itself, against real logs."""

    def test_a_healthy_wal_reads_as_bound_with_its_anchor(self):
        evidence = oc._wal_anchor_evidence(self.wal_root, manifest_root=self.manifest_root,
                                           anchor_root=self.anchor)
        self.assertTrue(evidence["bound"])
        self.assertTrue(evidence["anchor_intact"])

    def test_a_wal_that_has_never_been_initialized_reads_as_unbound(self):
        fresh_wal = os.path.join(self.tmp, "fresh_wal")
        fresh_anchor = os.path.join(self.tmp, "fresh_anchor")
        os.makedirs(fresh_wal)
        os.makedirs(fresh_anchor)
        rw.declare_anchor_domain(fresh_anchor, domain_id="fresh", at_utc=iso(),
                                 operator="tests", note="unit-test domain")
        evidence = oc._wal_anchor_evidence(fresh_wal, manifest_root=self.manifest_root,
                                           anchor_root=fresh_anchor)
        self.assertFalse(evidence["bound"])
        self.assertIn("never been initialized", evidence["reason"])

    def test_an_unreadable_manifest_reads_as_unbound_rather_than_raising(self):
        evidence = oc._wal_anchor_evidence(
            self.wal_root, manifest_root=os.path.join(self.tmp, "nowhere"),
            anchor_root=self.anchor)
        self.assertFalse(evidence["bound"])
        self.assertIn("could not be read", evidence["reason"])


class TestTheFenceHoldsForTheWholeWindow(_CompactionBase):
    """A one-time writer count is not a fence."""

    def test_the_fence_is_probed_before_the_scan_and_after_the_publish(self):
        self.compact()
        self.assertGreaterEqual(len(self.probe_calls), 3,
                                "the fence must be probed before the scan, after the build, "
                                "and after the publish")

    def test_no_probe_at_all_refuses(self):
        """The shipped default fails closed rather than inventing a posture."""
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(writer_fence_probe=oc.refusing_fence_probe)
        self.assertIn("no writer_fence_probe was supplied", str(caught.exception))

    def test_a_running_writer_service_refuses_before_anything_is_read(self):
        gen = self.manifest_generation()
        fence = self._fence(services_stopped=[{"name": "mur-delta-append",
                                               "posture": "online"}])
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(writer_fence_probe=lambda: fence)
        self.assertIn("still running", str(caught.exception))
        self.assertEqual(self.manifest_generation(), gen)

    def test_an_enabled_append_cron_refuses(self):
        fence = self._fence(cron_disabled="no append window right now")
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(writer_fence_probe=lambda: fence)
        self.assertIn("statement about the clock", str(caught.exception))

    def test_a_fence_that_does_not_admit_flock_is_advisory_refuses(self):
        fence = self._fence(flock_is_advisory=False)
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(writer_fence_probe=lambda: fence)
        self.assertIn("flock is advisory", str(caught.exception))

    def test_a_fence_with_no_arbitrary_writer_policy_refuses(self):
        fence = self._fence(arbitrary_writer_policy="   ")
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(writer_fence_probe=lambda: fence)
        self.assertIn("arbitrary writers", str(caught.exception))

    def test_a_service_up_again_after_the_scan_refuses(self):
        """Caught by the per-probe check, not by the comparison.

        Asserted as the message the per-probe check emits rather than a comparison message,
        because that is the one that actually fires: `writer_fence_problem` runs on every probe
        and an `online` writer is invalid on its own. A comparison branch for this case would
        be unreachable, which is why `_fence_moved` no longer has one."""
        postures = [self._fence(),
                    self._fence(services_stopped=[{"name": "mur-delta-append",
                                                   "posture": "online"},
                                                  {"name": "mur-daily-ingest",
                                                   "posture": "disabled"}])]
        calls = {"n": 0}

        def probe():
            calls["n"] += 1
            return postures[min(calls["n"] - 1, len(postures) - 1)]

        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(writer_fence_probe=probe)
        self.assertIn("a writer that is still running is the hazard", str(caught.exception))

    def test_a_service_dropping_out_of_the_report_refuses(self):
        postures = [self._fence(),
                    self._fence(services_stopped=[{"name": "mur-daily-ingest",
                                                   "posture": "disabled"}])]
        calls = {"n": 0}

        def probe():
            calls["n"] += 1
            return postures[min(calls["n"] - 1, len(postures) - 1)]

        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(writer_fence_probe=probe)
        self.assertIn("not accounted for after it", str(caught.exception))

    def test_the_sampling_limitation_is_stated_rather_than_papered_over(self):
        """A service that stops and restarts entirely between two probes is invisible to all
        of them. Nothing here closes that, and the code says so."""
        doc = " ".join((inspect.getdoc(oc._fence_moved) or "").split())
        self.assertIn("These are samples", doc)
        self.assertIn("needs something watching continuously", doc)

    def test_the_ingest_lock_is_held_across_the_scan_and_the_publish(self):
        """Recorded from the actual lock state, not read off a comment."""
        seen = {}
        real_build, real_execute = oc._build, oc._execute

        def probe_writer():
            try:
                lock = cl.CompactionLock(self.ilock, holder="competing-ingest")
                lock.acquire()
            except cl.CompactionLockBusy:
                return "blocked"
            lock.release()
            return "entered"

        def spy_build(**kw):
            seen["during_scan"] = probe_writer()
            return real_build(**kw)

        def spy_execute(candidate, **kw):
            seen["during_publish"] = probe_writer()
            return real_execute(candidate, **kw)

        with mock.patch.object(oc, "_build", side_effect=spy_build), \
                mock.patch.object(oc, "_execute", side_effect=spy_execute):
            self.compact()
        self.assertEqual(seen["during_scan"], "blocked",
                         "the expensive scan ran outside the ingest fence")
        self.assertEqual(seen["during_publish"], "blocked")

    def test_the_compaction_lock_is_held_across_everything(self):
        seen = {}
        real_execute = oc._execute

        def spy_execute(candidate, **kw):
            try:
                lock = cl.CompactionLock(self.clock, holder="competing-compactor")
                lock.acquire()
                lock.release()
                seen["outermost"] = False
            except cl.CompactionLockBusy:
                seen["outermost"] = True
            return real_execute(candidate, **kw)

        with mock.patch.object(oc, "_execute", side_effect=spy_execute):
            self.compact()
        self.assertTrue(seen["outermost"])

    def test_a_running_compaction_refuses(self):
        holder = cl.CompactionLock(self.clock, holder="another-compactor")
        holder.acquire()
        self.addCleanup(holder.release)
        with self.assertRaises((oc.OneShotCompactionRefused, cl.CompactionLockBusy)):
            self.compact()


class TestTheMonolithMustNotMoveUnderIt(_CompactionBase):

    def _mutate_a_chunk(self):
        metadata = {"zarr.json", ".zarray", ".zgroup", ".zattrs", ".zmetadata"}
        for dirpath, _dirs, files in os.walk(self.monolith):
            for name in sorted(files):
                if name in metadata or name.startswith("."):
                    continue
                with open(os.path.join(dirpath, name), "ab") as fh:
                    fh.write(b"\0")
                return
        raise AssertionError("no chunk to mutate")

    def test_a_store_that_changed_since_the_record_refuses(self):
        record, pre = self._record(), self._pre()
        self._mutate_a_chunk()
        with self.assertRaises((oc.OneShotCompactionRefused, ex.OneShotExceptionRefused)):
            self.compact(one_shot_exception=record, one_shot_pre_compaction=pre)

    def test_a_store_that_changes_during_the_build_refuses_before_publishing(self):
        gen = self.manifest_generation()
        real_build = oc._build

        def build_then_mutate(**kw):
            plan = real_build(**kw)
            self._mutate_a_chunk()
            return plan

        with mock.patch.object(oc, "_build", side_effect=build_then_mutate):
            with self.assertRaises(oc.OneShotCompactionRefused) as caught:
                self.compact()
        self.assertIn("changed during the build", str(caught.exception))
        self.assertEqual(self.manifest_generation(), gen)

    def test_the_measurement_is_taken_by_the_operation(self):
        params = set(inspect.signature(oc.compact_one_shot).parameters)
        for banned in ("one_shot_observed", "observed", "monolith_state", "measured"):
            with self.subTest(banned):
                self.assertNotIn(banned, params)


class TestStaleOrMalformedPreflightRefuses(_CompactionBase):

    def test_stale_preflight_refuses(self):
        with self.assertRaises((oc.OneShotCompactionRefused, ex.OneShotExceptionRefused)):
            self.compact(one_shot_pre_compaction=self._pre(collected_utc=iso(-4 * 3600)))

    def test_preflight_for_another_run_refuses(self):
        with self.assertRaises((oc.OneShotCompactionRefused, ex.OneShotExceptionRefused)):
            self.compact(one_shot_pre_compaction=self._pre(run_id="another-run"))

    def test_an_unapproved_deployment_refuses(self):
        pre = self._pre()
        pre["identity"]["approved"] = False
        with self.assertRaises((oc.OneShotCompactionRefused, ex.OneShotExceptionRefused)):
            self.compact(one_shot_pre_compaction=pre)

    def test_drifted_module_fingerprints_refuse(self):
        problem = ex.pre_compaction_problem(
            self._pre(), record=self._record(), host=socket.gethostname(),
            run_id=self.RUN_ID,
            expected_fingerprints={"ops/p5_oneshot_compaction.py": "9" * 64})
        self.assertIn("do not match the reviewed commit", problem)

    def test_a_healthz_with_no_route_refuses(self):
        pre = self._pre()
        del pre["healthz"]["route"]
        with self.assertRaises((oc.OneShotCompactionRefused, ex.OneShotExceptionRefused)):
            self.compact(one_shot_pre_compaction=pre)

    def test_an_unsafe_pm2_posture_refuses(self):
        pre = self._pre()
        pre["quiescence"]["pm2"] = "online"
        with self.assertRaises((oc.OneShotCompactionRefused, ex.OneShotExceptionRefused)):
            self.compact(one_shot_pre_compaction=pre)

    def test_a_lock_acquisition_that_was_never_demonstrated_refuses(self):
        pre = self._pre()
        pre["locks"]["acquisition_probe"] = {"compaction": "assumed", "ingest": "assumed"}
        with self.assertRaises((oc.OneShotCompactionRefused, ex.OneShotExceptionRefused)):
            self.compact(one_shot_pre_compaction=pre)

    def test_a_relative_lock_path_refuses(self):
        pre = self._pre()
        pre["locks"]["canonical_paths"]["ingest"] = "relative/ingest.lock"
        with self.assertRaises((oc.OneShotCompactionRefused, ex.OneShotExceptionRefused)):
            self.compact(one_shot_pre_compaction=pre)

    def test_a_mismatched_backup_refuses(self):
        pre = self._pre()
        pre["backup"]["backup_id"] = "some-other-backup"
        with self.assertRaises((oc.OneShotCompactionRefused, ex.OneShotExceptionRefused)):
            self.compact(one_shot_pre_compaction=pre)

    def test_capacity_from_another_device_refuses(self):
        pre = self._pre()
        pre["capacity"]["filesystem_dev"] = os.stat(self.live).st_dev + 9999
        with self.assertRaises((oc.OneShotCompactionRefused, ex.OneShotExceptionRefused)):
            self.compact(one_shot_pre_compaction=pre)

    def test_the_backup_verdict_stays_not_adjudicated(self):
        verdict = self.compact()["one_shot_exception"]["backup_verification"]
        self.assertEqual(verdict["verdict"], ex.NOT_ADJUDICATED)


class TestThereIsNoBypass(_CompactionBase):

    def test_the_operation_has_no_force_or_skip_parameter(self):
        params = set(inspect.signature(oc.compact_one_shot).parameters)
        for banned in ("force", "skip", "skip_seal", "allow_unsealed", "require_sealed",
                       "unsafe", "override", "unsafe_skip_provenance",
                       "unsafe_skip_source_recheck", "unsafe_skip_compaction_lock"):
            with self.subTest(banned):
                self.assertNotIn(banned, params)

    def test_it_never_passes_an_unsafe_flag_to_the_publisher(self):
        """`execute_publication` has three `unsafe_skip_*` parameters. This operation must not
        pass any of them, and by AST rather than by reading the call."""
        tree = ast.parse(inspect.getsource(oc))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg:
                        with self.subTest(kw=kw.arg):
                            self.assertFalse(kw.arg.startswith("unsafe_"))

    def test_no_bypass_is_read_from_the_environment(self):
        for node in ast.walk(ast.parse(inspect.getsource(oc))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                for banned in ("P5_FORCE", "P5_SKIP", "P5_ALLOW"):
                    self.assertNotIn(banned, node.value)

    def test_the_default_fence_probe_refuses_rather_than_asserting_a_posture(self):
        signature = inspect.signature(oc.compact_one_shot)
        self.assertIs(signature.parameters["writer_fence_probe"].default,
                      oc.refusing_fence_probe)
        with self.assertRaises(oc.OneShotCompactionRefused):
            oc.refusing_fence_probe()

    def test_the_parity_probes_are_required(self):
        signature = inspect.signature(oc.compact_one_shot)
        for name in ("point_series_parity", "api_parity"):
            with self.subTest(name):
                self.assertIs(signature.parameters[name].default, inspect.Parameter.empty)


class TestTheDocumentsRecordTheOperation(unittest.TestCase):

    def _flat(self, name):
        with open(os.path.join(_DEV, "specs", name)) as fh:
            return " ".join(fh.read().split())

    def test_the_module_says_it_deletes_nothing(self):
        doc = " ".join((inspect.getdoc(oc) or "").split())
        self.assertIn("It deletes nothing", doc)
        self.assertIn("It does not switch the service", doc)

    def test_the_module_states_what_the_fence_does_not_cover(self):
        doc = " ".join((inspect.getdoc(oc) or "").split())
        self.assertIn("flock` is advisory", doc)
        self.assertIn("detection, not prevention", doc)

    def test_the_runbook_describes_the_operation_and_its_limits(self):
        flat = self._flat("p5_monolith_mount_barrier_requirement.md")
        self.assertIn("one_shot_compaction", flat)
        self.assertIn("deletes nothing", flat)
        self.assertIn("finalization clearance", flat)

    def test_the_roadmap_places_the_operation_and_keeps_deletion_separate(self):
        flat = self._flat("p5_deployment_readiness.md")
        self.assertIn("one_shot_compaction", flat)
        self.assertIn("Releasing the old space is a separate operation, and it does not exist "
                      "yet", flat)
        self.assertIn("nothing in this tree can release it", flat)


class TestTheHelpersDirectly(unittest.TestCase):
    """The gate logic, exercised on its own.

    Every one of these was a mutation SURVIVOR: the end-to-end tests reach these functions only
    through a real build, where the inputs are always well-formed, and the failure-injection
    tests patch the whole function rather than feeding it bad data. So the bodies were never
    executed against anything they were written to refuse."""

    def _record(self, day, **over):
        r = {"source_kind": "delta", "source_path": "/g/delta.zarr", "source_day_index": 0}
        r.update(over)
        return r

    # ---------------------------------------------------------------- source map completeness
    def test_a_complete_map_passes(self):
        days = ["2026-01-07", "2026-01-08"]
        self.assertTrue(oc._source_map_complete({d: self._record(d) for d in days}, days))

    def test_a_missing_day_is_incomplete(self):
        days = ["2026-01-07", "2026-01-08"]
        self.assertFalse(oc._source_map_complete({days[0]: self._record(days[0])}, days))

    def test_an_empty_map_is_incomplete(self):
        self.assertFalse(oc._source_map_complete({}, ["2026-01-07"]))

    def test_a_day_with_no_source_kind_is_incomplete(self):
        days = ["2026-01-07"]
        for missing in ("source_kind", "source_path"):
            with self.subTest(missing):
                record = self._record(days[0])
                del record[missing]
                self.assertFalse(oc._source_map_complete({days[0]: record}, days))

    def test_a_day_with_no_source_index_is_incomplete(self):
        """`source_kind` alone cannot answer the audit question -- two builds can both say
        `delta` and have read different physical indices."""
        days = ["2026-01-07"]
        record = self._record(days[0])
        del record["source_day_index"]
        self.assertFalse(oc._source_map_complete({days[0]: record}, days))

    def test_a_day_whose_record_is_not_a_record_is_incomplete(self):
        self.assertFalse(oc._source_map_complete({"2026-01-07": "delta"}, ["2026-01-07"]))

    # ---------------------------------------------------------------- disjointness
    def _segment(self, sid, start, end, **over):
        s = {"segment_id": sid, "start_day": start, "end_day": end, "gaps": [],
             "day_list": None}
        s.update(over)
        return s

    def test_two_segments_that_do_not_overlap_are_disjoint(self):
        manifest = {"segments": [self._segment("a", "2026-01-01", "2026-01-05"),
                                 self._segment("b", "2026-01-06", "2026-01-10")]}
        self.assertTrue(oc._base_delta_disjoint(manifest))

    def test_two_segments_claiming_the_same_day_are_not_disjoint(self):
        manifest = {"segments": [self._segment("a", "2026-01-01", "2026-01-06"),
                                 self._segment("b", "2026-01-06", "2026-01-10")]}
        self.assertFalse(oc._base_delta_disjoint(manifest))

    def test_a_day_excluded_by_a_gap_does_not_count_as_claimed(self):
        """A gap is a day the segment does NOT serve, so it cannot collide with one that does."""
        manifest = {"segments": [self._segment("a", "2026-01-01", "2026-01-06",
                                               gaps=["2026-01-06"]),
                                 self._segment("b", "2026-01-06", "2026-01-10")]}
        self.assertTrue(oc._base_delta_disjoint(manifest))

    def test_an_explicit_day_list_is_used_when_present(self):
        manifest = {"segments": [self._segment("a", "2026-01-01", "2026-01-10",
                                               day_list=["2026-01-01"]),
                                 self._segment("b", "2026-01-02", "2026-01-10")]}
        self.assertTrue(oc._base_delta_disjoint(manifest))

    def test_one_segment_is_trivially_disjoint(self):
        manifest = {"segments": [self._segment("a", "2026-01-01", "2026-01-05")]}
        self.assertTrue(oc._base_delta_disjoint(manifest))

    # ---------------------------------------------------------------- inventory
    def test_the_inventory_reports_a_missing_path_as_absent(self):
        out = oc.inventory({"gone": "/definitely/not/here"})
        self.assertFalse(out["gone"]["present"])
        self.assertEqual(out["gone"]["bytes"], 0)

    def test_the_shrink_check_notices_fewer_bytes_and_fewer_files(self):
        before = {"m": {"bytes": 100, "files": 10}}
        self.assertTrue(oc._nothing_shrank(before, {"m": {"bytes": 100, "files": 10}}))
        self.assertTrue(oc._nothing_shrank(before, {"m": {"bytes": 200, "files": 20}}))
        self.assertFalse(oc._nothing_shrank(before, {"m": {"bytes": 99, "files": 10}}))
        self.assertFalse(oc._nothing_shrank(before, {"m": {"bytes": 100, "files": 9}}))

    def test_a_role_absent_from_the_after_inventory_counts_as_shrunk(self):
        self.assertFalse(oc._nothing_shrank({"m": {"bytes": 1, "files": 1}}, {}))


class TestThePostGatesAreBoundToOneAct(unittest.TestCase):
    """`expected` is what makes a passing result a passing result about THIS run."""

    def evidence(self, **over):
        e = {
            "host": "odb24", "run_id": "R1",
            "manifest": {"generation": 2, "checksum_ok": True, "checksum": "f" * 64},
            "wal_anchor": {"bound": True, "anchor_intact": True},
            "source_map_complete": True, "base_delta_disjoint": True,
            "source_map_digest": "9" * 64,
            "monolith_state": {"monolith_realpath": "/g/m.zarr",
                               "monolith_metadata_fingerprint": "a" * 64,
                               "monolith_day_digest": "b" * 64,
                               "monolith_content_ledger": {"ledger_digest": "c" * 64}},
            "point_series_parity": {"days_checked": 5, "mismatches": 0,
                                    "boundaries_included": True},
            "api_parity": {"routes_checked": 3, "mismatches": 0},
            "capacity_after": {"free_now_gib": 10.0, "measured_utc": iso()},
            "nothing_deleted": {"value": True, "derived_from": "inventory",
                                "before": {"m": {"bytes": 1, "files": 1}},
                                "after": {"m": {"bytes": 1, "files": 1}}},
        }
        e.update(over)
        return e

    def expected(self, **over):
        e = {"host": "odb24", "run_id": "R1", "generation": 2,
             "manifest_checksum": "f" * 64, "source_map_digest": "9" * 64,
             "monolith_state": self.evidence()["monolith_state"]}
        e.update(over)
        return e

    def test_matching_evidence_passes(self):
        self.assertIsNone(ex.post_compaction_problem(self.evidence(),
                                                     expected=self.expected()))

    def test_gates_from_another_host_run_generation_or_manifest_are_refused(self):
        for field, wrong in (("host", "elsewhere"), ("run_id", "R2"), ("generation", 3),
                             ("manifest_checksum", "0" * 64),
                             ("source_map_digest", "1" * 64)):
            with self.subTest(field):
                problem = ex.post_compaction_problem(
                    self.evidence(), expected=self.expected(**{field: wrong}))
                self.assertIn(field, problem)

    def test_gates_about_another_monolith_are_refused(self):
        other = dict(self.evidence()["monolith_state"], monolith_day_digest="9" * 64)
        problem = ex.post_compaction_problem(self.evidence(),
                                             expected=self.expected(monolith_state=other))
        self.assertIn("monolith_day_digest", problem)

    def test_evidence_missing_the_binding_fields_is_refused(self):
        for field in ("host", "run_id", "source_map_digest"):
            with self.subTest(field):
                evidence = self.evidence()
                del evidence[field]
                problem = ex.post_compaction_problem(evidence, expected=self.expected())
                self.assertIn(field, problem)

    def test_a_bad_manifest_checksum_is_refused(self):
        for value in (False, None, "yes"):
            with self.subTest(repr(value)):
                problem = ex.post_compaction_problem(
                    self.evidence(manifest={"generation": 2, "checksum_ok": value,
                                            "checksum": "f" * 64}))
                self.assertIn("does not hash to the checksum it carries", problem)

    def test_a_generation_that_is_not_a_generation_is_refused(self):
        for value in (0, -1, True, "2"):
            with self.subTest(repr(value)):
                problem = ex.post_compaction_problem(
                    self.evidence(manifest={"generation": value, "checksum_ok": True,
                                            "checksum": "f" * 64}))
                self.assertIn("not a generation", problem)

    def test_a_nothing_deleted_block_from_somewhere_other_than_an_inventory_is_refused(self):
        """A dict that LOOKS like evidence but was not derived from a measurement."""
        problem = ex.post_compaction_problem(self.evidence(nothing_deleted={
            "value": True, "derived_from": "the operation did not call any delete",
            "before": {"m": {"bytes": 1, "files": 1}},
            "after": {"m": {"bytes": 1, "files": 1}}}))
        self.assertIn("Only an inventory taken", problem)


class TestTheFenceIsCheckedBeforeAnythingIsRead(_CompactionBase):
    """The pre-scan probe, and what only it provides.

    A later probe catches a bad fence too, so a test asserting merely "it raised" passes with
    the early check deleted. What the early one adds is that the multi-minute build never
    starts — so that is what is asserted."""

    def test_a_bad_fence_refuses_before_the_build_runs(self):
        bad = self._fence(verified=False)
        with mock.patch.object(oc, "_build", side_effect=AssertionError("must not run")) \
                as build:
            with self.assertRaises(oc.OneShotCompactionRefused) as caught:
                self.compact(writer_fence_probe=lambda: bad)
        self.assertIn("writer fence", str(caught.exception))
        self.assertFalse(build.called,
                         "the build ran before the fence was checked; the pre-scan probe is "
                         "what stops a multi-minute scan starting behind a fence that is down")

    def test_a_bad_fence_refuses_before_the_capability_is_minted(self):
        bad = self._fence(cron_disabled="enabled")
        with mock.patch.object(ex, "authorize_compaction",
                               side_effect=AssertionError("must not run")) as mint:
            with self.assertRaises(oc.OneShotCompactionRefused):
                self.compact(writer_fence_probe=lambda: bad)
        self.assertFalse(mint.called)


class TestTheCapabilityClassIsTheBinding(unittest.TestCase):
    """Each subclass authorizes exactly one operation, and the class enforces it."""

    def _kw(self, **over):
        kw = dict(host="odb24", run_id="R1", realpath="/g/m.zarr",
                  expires_utc=iso(600), measured={}, backup_id="b", approved_by="cywhale")
        kw.update(over)
        return kw

    def test_the_base_class_is_abstract(self):
        with self.assertRaises(ex.UnsealedMonolithAuthorizationError) as caught:
            ex._Authorization(ex._MINT, operation=ex.OP_PROVISION, **self._kw())
        self.assertIn("abstract", str(caught.exception))

    def test_a_subclass_refuses_an_operation_that_is_not_its_own(self):
        for cls, wrong in ((ex.UnsealedMonolithAuthorization, ex.OP_COMPACTION),
                           (ex.OneShotCompactionAuthorization, ex.OP_PROVISION)):
            with self.subTest(cls.__name__):
                with self.assertRaises(ex.UnsealedMonolithAuthorizationError) as caught:
                    cls(ex._MINT, operation=wrong, **self._kw())
                self.assertIn("authorizes", str(caught.exception))

    def test_each_subclass_accepts_its_own(self):
        for cls in (ex.UnsealedMonolithAuthorization, ex.OneShotCompactionAuthorization):
            with self.subTest(cls.__name__):
                self.assertEqual(cls(ex._MINT, operation=cls.OPERATION,
                                     **self._kw()).operation, cls.OPERATION)

    def test_neither_is_a_subclass_of_the_other(self):
        """Siblings, so `isinstance` genuinely separates them."""
        self.assertFalse(issubclass(ex.OneShotCompactionAuthorization,
                                    ex.UnsealedMonolithAuthorization))
        self.assertFalse(issubclass(ex.UnsealedMonolithAuthorization,
                                    ex.OneShotCompactionAuthorization))


class TestTheBorrowedIngestFenceIsValidated(_CompactionBase):
    """`execute_publication` may borrow the caller's ingest lock. The borrow is checked.

    Without these, all three validations survived mutation: the happy path always passes a
    correct borrow, so the refusal branches were never executed."""

    def _plan(self):
        """A real block plan and its composed candidate, ready to publish.

        The build needs the compaction reservation held -- without it a delta prune or swap can
        retarget the delta path mid-build (§7.1b) -- so it is taken here and released before the
        publish, which is the part these tests are about."""
        build_lock = cl.CompactionLock(self.clock, holder="borrow-tests/build")
        build_lock.acquire()
        try:
            with dev_map(self.devs):
                plan = oc._build(
                    out_path=os.path.join(self.manifest_root, "block_borrow.zarr"),
                    start_day=self.grid[0], end_day=self.grid[-1],
                    classification_target=list(self.days), monolith_path=self.monolith,
                    delta_path=self.delta, daily_root=None, artifacts_dir=self.artifacts,
                    hard_reserve_bytes=1024, lock=build_lock,
                    wal_root=self.wal_root, anchor_root=self.anchor)
        finally:
            build_lock.release()
        return plan, oc._plan(plan, manifest_root=self.manifest_root)

    def test_a_borrowed_lock_on_another_path_is_refused(self):
        plan, candidate = self._plan()
        elsewhere = os.path.join(self.tmp, "some_other.lock")
        lock = cl.CompactionLock(elsewhere, holder="tests")
        lock.acquire()
        self.addCleanup(lock.release)
        with self.assertRaises(pub.PublishRefused) as caught:
            pub.execute_publication(candidate, ingest_lock_path=self.ilock, ingest_lock=lock,
                                    compaction_lock_path=None, delta_path=self.delta,
                                    unsafe_skip_compaction_lock=True,
                                    build_artifact_path=plan["provenance_path"])
        self.assertIn("leaves the real one free", str(caught.exception))

    def test_a_borrowed_lock_that_is_not_held_is_refused(self):
        plan, candidate = self._plan()
        lock = cl.CompactionLock(self.ilock, holder="tests")     # never acquired
        with self.assertRaises(pub.PublishRefused) as caught:
            pub.execute_publication(candidate, ingest_lock_path=self.ilock, ingest_lock=lock,
                                    compaction_lock_path=None, delta_path=self.delta,
                                    unsafe_skip_compaction_lock=True,
                                    build_artifact_path=plan["provenance_path"])
        self.assertIn("is not held", str(caught.exception))

    def test_a_borrowed_lock_is_still_held_afterwards(self):
        """Releasing another component's fence is how "held throughout" becomes a gap."""
        plan, candidate = self._plan()
        lock = cl.CompactionLock(self.ilock, holder="tests")
        lock.acquire()
        self.addCleanup(lock.release)
        out = pub.execute_publication(candidate, ingest_lock_path=self.ilock, ingest_lock=lock,
                                      compaction_lock_path=None, delta_path=self.delta,
                                      unsafe_skip_compaction_lock=True,
                                      build_artifact_path=plan["provenance_path"])
        self.assertTrue(out["published"])
        self.assertTrue(lock.held, "the publisher released a fence it had only borrowed")
        # And it is genuinely still exclusive.
        contender = cl.CompactionLock(self.ilock, holder="competing-ingest")
        with self.assertRaises(cl.CompactionLockBusy):
            contender.acquire()


class TestTheDeploymentIdentityIsRequired(_CompactionBase):
    """A production approval names a root and a run. It does not review the code.

    `expected_module_fingerprints` used to be accepted and never read: an optional parameter
    that did nothing, which is worse than an absent one because a reader passing it believes a
    check is happening. It is gone, and this is what replaced it."""

    def test_the_misleading_parameter_is_gone(self):
        params = set(inspect.signature(oc.compact_one_shot).parameters)
        self.assertNotIn("expected_module_fingerprints", params)
        self.assertIn("identity_artifact", params)

    def test_the_identity_artifact_is_required(self):
        self.assertIs(inspect.signature(oc.compact_one_shot)
                      .parameters["identity_artifact"].default, inspect.Parameter.empty)

    def test_a_complete_identity_passes(self):
        self.assertIsNone(oc.identity_problem(self._identity(),
                                              host=socket.gethostname(), run_id=self.RUN_ID))

    def test_every_field_is_required(self):
        for field in oc.IDENTITY_FIELDS:
            with self.subTest(field):
                artifact = self._identity()
                del artifact[field]
                problem = oc.identity_problem(artifact, host=socket.gethostname(),
                                              run_id=self.RUN_ID)
                self.assertTrue(problem)
                self.assertIn(field, problem)

    def test_an_unreviewed_deploy_sha_refuses(self):
        """The blocker in one line: approved root, approved run, unreviewed code."""
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(identity_artifact=self._identity(deploy_sha="a" * 40,
                                                          head_sha="a" * 40))
        self.assertIn("not in the approved deploy set", str(caught.exception))
        self.assertEqual(self.manifest_generation(), 1)

    def test_head_that_is_not_the_reviewed_commit_refuses(self):
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(identity_artifact=self._identity(head_sha="b" * 40))
        self.assertIn("not the reviewed commit", str(caught.exception))

    def test_a_dirty_checkout_refuses(self):
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(identity_artifact=self._identity(worktree_clean=False))
        self.assertIn("not clean", str(caught.exception))

    def test_a_drifted_module_fingerprint_refuses(self):
        """The check a matching SHA cannot make: what is on disk, not what git says."""
        prints = dict(self._identity()["module_fingerprints"])
        prints["store/segmented_cube.py"] = "9" * 64
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(identity_artifact=self._identity(module_fingerprints=prints))
        self.assertIn("do not match", str(caught.exception))

    def test_a_shrunken_inventory_refuses(self):
        """A subset makes every comparison pass on a subset."""
        prints = dict(self._identity()["module_fingerprints"])
        del prints["store/block_manifest.py"]
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(identity_artifact=self._identity(module_fingerprints=prints))
        self.assertIn("fingerprints no", str(caught.exception))

    def test_a_module_the_review_never_saw_refuses(self):
        prints = dict(self._identity()["module_fingerprints"])
        prints["ops/something_new.py"] = "c" * 64
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(identity_artifact=self._identity(module_fingerprints=prints))
        self.assertIn("the approved commit does not contain", str(caught.exception))

    def test_an_identity_from_another_run_or_host_refuses(self):
        for field, wrong in (("run_id", "another-run"), ("host", "elsewhere")):
            with self.subTest(field):
                with self.assertRaises(oc.OneShotCompactionRefused):
                    self.compact(identity_artifact=self._identity(**{field: wrong}))

    def test_a_stale_identity_refuses(self):
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(identity_artifact=self._identity(collected_utc=iso(-4 * 3600)))
        self.assertIn("past the", str(caught.exception))

    def test_identity_is_checked_before_any_lock_or_scan(self):
        """An operator with a dirty checkout should learn it before a multi-minute scan."""
        with mock.patch.object(oc, "_build", side_effect=AssertionError("must not run")) \
                as build:
            with self.assertRaises(oc.OneShotCompactionRefused):
                self.compact(identity_artifact=self._identity(worktree_clean=False))
        self.assertFalse(build.called)


class TestParityEvidenceMustBeExecutedAndBound(_CompactionBase):
    """A callable that returns `{"mismatches": 0}` measured nothing, and says nothing.

    The producer's identity is DERIVED from the callable and checked against a reviewed
    registry. An earlier revision read the digest out of the artifact and merely required it to
    be *some* approved module's — which a fake could satisfy by copying one, returning a fully
    green result while measuring nothing. That is the hole these close."""

    @staticmethod
    def _bare_fake(**kw):
        return {}

    # ---------------------------------------------------------------- the derivation
    def test_a_green_fake_callable_is_refused(self):
        """The negative test the review asked for, on the production entry point.

        The fake returns EVERY required field, reports zero mismatches, and claims the producer
        name and a legitimate fingerprint of an approved module. It is still refused, because
        what it claims about itself is not where the identity comes from."""
        legitimate = self._identity()["module_fingerprints"]["store/source_provenance.py"]

        def convincing_fake(**kw):
            return {
                "kind": oc.PARITY_POINT_SERIES,
                "host": kw["host"], "run_id": kw["run_id"], "deploy_sha": kw["deploy_sha"],
                "producer": "ops/p5_parity_producers.py:point_series_parity",
                "producer_digest": legitimate,
                "generation": kw["generation"],
                "manifest_checksum": kw["manifest_checksum"],
                "source_map_digest": kw["source_map_digest"],
                "executed_utc": iso(), "samples": 40, "mismatches": 0,
                "provenance": "compared point-wise against each day's own source",
                "days_checked": len(kw["days"]), "boundaries_included": True,
            }

        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(point_series_parity=convincing_fake)
        message = str(caught.exception)
        self.assertIn("is not the reviewed", message)
        self.assertEqual(self.manifest_generation(), 1, "a green fake reached the publish")

    def test_a_callable_defined_in_a_test_is_refused(self):
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(api_parity=self._bare_fake)
        self.assertIn("is not the reviewed", str(caught.exception))

    def test_a_genuine_but_unregistered_function_is_refused(self):
        """Being in an approved module is not being the approved producer."""
        from store import source_provenance as real_module
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(point_series_parity=real_module.artifact_checksum)
        self.assertIn("is not the reviewed", str(caught.exception))

    def test_a_producer_whose_module_is_not_fingerprinted_is_refused(self):
        prints = {k: v for k, v in self._identity()["module_fingerprints"].items()
                  if k not in oc.REVIEWED_PRODUCER_MODULES}
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(identity_artifact=self._identity(module_fingerprints=prints))
        self.assertIn("does not fingerprint", str(caught.exception))

    def test_a_producer_module_that_changed_on_disk_is_refused(self):
        prints = dict(self._identity()["module_fingerprints"])
        prints["ops/p5_parity_producers.py"] = "a" * 64
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(identity_artifact=self._identity(module_fingerprints=prints))
        self.assertIn("is not the one that would run", str(caught.exception))

    def test_the_derivation_is_refused_before_any_lock_or_scan(self):
        with mock.patch.object(oc, "_build", side_effect=AssertionError("must not run")) \
                as build:
            with self.assertRaises(oc.OneShotCompactionRefused):
                self.compact(api_parity=self._bare_fake)
        self.assertFalse(build.called)

    def test_the_real_producers_pass(self):
        """The positive control, through the actual reviewed producer path."""
        for kind, fn in ((oc.PARITY_POINT_SERIES, ppd.point_series_parity),
                         (oc.PARITY_API, ppd.api_parity)):
            with self.subTest(kind):
                identity, problem = oc.producer_identity(
                    fn, kind=kind,
                    module_fingerprints=self._identity()["module_fingerprints"])
                self.assertIsNone(problem)
                self.assertEqual(identity["module"], "ops/p5_parity_producers.py")

    def test_the_registry_names_a_real_importable_producer(self):
        """A registry entry pointing at nothing would refuse every run with a puzzling error."""
        for kind, (relpath, qualname) in oc.REVIEWED_PRODUCERS.items():
            with self.subTest(kind):
                self.assertTrue(os.path.isfile(os.path.join(_DEV, relpath)))
                self.assertTrue(callable(getattr(ppd, qualname, None)))


class TestTheParityArtifactGate(unittest.TestCase):
    """The artifact contract itself, fed directly.

    Reached through the operation only by the real producers now, and they cannot be made to
    emit a malformed artifact — so the shapes they would never produce are checked here."""

    PRODUCER = {"producer": "ops/p5_parity_producers.py:point_series_parity",
                "producer_digest": "b" * 64,
                "module": "ops/p5_parity_producers.py",
                "qualname": "point_series_parity"}

    def artifact(self, **over):
        a = {
            "kind": oc.PARITY_POINT_SERIES, "host": "odb24", "run_id": "R1",
            "deploy_sha": "d" * 40,
            "producer": self.PRODUCER["producer"],
            "producer_digest": self.PRODUCER["producer_digest"],
            "generation": 2, "manifest_checksum": "f" * 64, "source_map_digest": "9" * 64,
            "executed_utc": iso(), "samples": 40, "mismatches": 0,
            "provenance": "read per-day and compared point-wise",
        }
        a.update(over)
        return a

    def problem(self, **over):
        return oc.parity_artifact_problem(
            self.artifact(**over), kind=oc.PARITY_POINT_SERIES, host="odb24", run_id="R1",
            deploy_sha="d" * 40, generation=2, manifest_checksum="f" * 64,
            source_map_digest="9" * 64, producer=self.PRODUCER)

    def test_a_complete_artifact_passes(self):
        self.assertIsNone(self.problem())

    def test_every_field_is_required(self):
        for field in oc.PARITY_ARTIFACT_FIELDS:
            with self.subTest(field):
                a = self.artifact()
                del a[field]
                problem = oc.parity_artifact_problem(
                    a, kind=oc.PARITY_POINT_SERIES, host="odb24", run_id="R1",
                    deploy_sha="d" * 40, generation=2, manifest_checksum="f" * 64,
                    source_map_digest="9" * 64, producer=self.PRODUCER)
                self.assertTrue(problem)
                self.assertIn(field, problem)

    def test_an_artifact_of_the_wrong_kind_is_refused(self):
        self.assertIn("is not evidence for another", self.problem(kind=oc.PARITY_API))

    def test_an_artifact_bound_to_another_act_is_refused(self):
        for field, wrong in (("host", "elsewhere"), ("run_id", "R2"),
                             ("deploy_sha", "e" * 40), ("generation", 99),
                             ("manifest_checksum", "0" * 64),
                             ("source_map_digest", "1" * 64)):
            with self.subTest(field):
                problem = self.problem(**{field: wrong})
                self.assertIn("about another act", problem)

    def test_an_artifact_claiming_another_producer_is_refused(self):
        problem = self.problem(producer="store/source_provenance.py:compare_days")
        self.assertIn("a string it chose", problem)

    def test_an_artifact_claiming_another_digest_is_refused(self):
        problem = self.problem(producer_digest="c" * 64)
        self.assertIn("on disk hashes to", problem)

    def test_zero_samples_is_an_absent_check_not_a_passing_one(self):
        for value in (0, -1, True):
            with self.subTest(repr(value)):
                self.assertIn("is not a passing check", self.problem(samples=value))

    def test_a_nonzero_mismatch_count_is_refused(self):
        self.assertIn("mismatch(es) out of", self.problem(mismatches=3))

    def test_a_negative_or_bool_mismatch_count_is_refused(self):
        for value in (-1, True):
            with self.subTest(repr(value)):
                self.assertIn("not a count", self.problem(mismatches=value))

    def test_an_artifact_with_no_provenance_is_refused(self):
        self.assertIn("verification provenance", self.problem(provenance="   "))

    def test_a_stale_execution_is_refused(self):
        self.assertIn("past the", self.problem(executed_utc=iso(-4 * 3600)))

    def test_an_execution_in_the_future_is_refused(self):
        self.assertIn("in the future", self.problem(executed_utc=iso(3600)))

    def test_an_unstamped_execution_is_refused(self):
        self.assertIn("not a timestamp", self.problem(executed_utc="recently"))


class TestThePrePublishGate(unittest.TestCase):
    """The shapes `_pre_publish_problem` refuses, which the real producer will not emit."""

    def gates(self, **over):
        parity = {"days_checked": 5, "mismatches": 0, "boundaries_included": True}
        parity.update(over.pop("parity", {}))
        g = {"point_series_parity": parity, "source_map_complete": True,
             "base_delta_disjoint": True}
        g.update(over)
        return g

    def test_complete_gates_pass(self):
        self.assertIsNone(oc._pre_publish_problem(self.gates()))

    def test_parity_that_skipped_the_boundaries_is_refused(self):
        problem = oc._pre_publish_problem(self.gates(parity={"boundaries_included": False}))
        self.assertIn("first and last days", problem)

    def test_parity_over_zero_days_is_refused(self):
        for value in (0, -1, True):
            with self.subTest(repr(value)):
                problem = oc._pre_publish_problem(self.gates(parity={"days_checked": value}))
                self.assertIn("checks nothing", problem)

    def test_a_missing_parity_field_is_refused(self):
        for field in ("days_checked", "mismatches", "boundaries_included"):
            with self.subTest(field):
                parity = {"days_checked": 5, "mismatches": 0, "boundaries_included": True}
                del parity[field]
                g = {"point_series_parity": parity, "source_map_complete": True,
                     "base_delta_disjoint": True}
                self.assertIn(field, oc._pre_publish_problem(g))

    def test_a_parity_result_that_is_not_a_record_is_refused(self):
        g = self.gates()
        g["point_series_parity"] = "green"
        self.assertIn("not a record", oc._pre_publish_problem(g))


class TestAFailedGenerationIsQuarantined(_CompactionBase):
    """Blocker 3: a published-but-unverified generation must not be silently reusable.

    Raising alone would leave generation N+1 live and unclassified — the next run would read it,
    plan N+2 on top of it, and the failure would sit underneath a generation nobody questioned."""

    def _fail_post_gates(self, **over):
        with self.forced_api_mismatch():
            with self.assertRaises(oc.GenerationQuarantined) as caught:
                self.compact(**over)
        return caught.exception

    def test_a_post_gate_failure_writes_a_quarantine(self):
        self._fail_post_gates()
        record = oc.read_quarantine(self.manifest_root)
        self.assertIsNotNone(record)
        self.assertEqual(record["generation"], 2)
        self.assertEqual(record["run_id"], self.RUN_ID)
        self.assertFalse(record["service_switched"])
        self.assertTrue(record["nothing_deleted"])

    def test_the_quarantine_names_the_intact_predecessor_archive(self):
        self._fail_post_gates()
        record = oc.read_quarantine(self.manifest_root)
        self.assertEqual(record["predecessor_generation"], 1)
        archive = os.path.join(self.manifest_root, record["predecessor_archive"])
        self.assertTrue(os.path.isfile(archive),
                        "the predecessor archive is what a rollback restores; it must survive")

    def test_the_old_generation_remains_recoverable(self):
        """The archive is immutable (§9.3) and still parses as generation 1."""
        self._fail_post_gates()
        record = oc.read_quarantine(self.manifest_root)
        with open(os.path.join(self.manifest_root, record["predecessor_archive"])) as fh:
            archived = json.load(fh)
        self.assertEqual(int(archived["generation"]), 1)
        self.assertEqual(archived["manifest_checksum"],
                         bm.compute_checksum(archived))

    def test_the_next_compaction_refuses_over_an_unresolved_quarantine(self):
        self._fail_post_gates()
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(run_id=self.RUN_ID)
        self.assertIn("QUARANTINED", str(caught.exception))
        self.assertIn("until a person reconciles it", str(caught.exception))

    def test_finalization_refuses_over_an_unresolved_quarantine(self):
        """Even with a clearance and an approval: removing data underneath an unverified
        authority is the worst available order of operations."""
        self._fail_post_gates()
        clearance = ex.issue_finalization_clearance(
            host=socket.gethostname(), run_id=self.RUN_ID, generation=2,
            manifest_checksum="f" * 64, monolith_day_digest="d" * 64,
            source_map_digest="9" * 64, issued_by="tests", gates_passed=True)
        with self.assertRaises(ex.FinalizationRefused) as caught:
            ex.refuse_destructive_step("prune", clearance, host=socket.gethostname(),
                                       run_id=self.RUN_ID, generation=2,
                                       manifest_checksum="f" * 64, approval=True,
                                       manifest_root=self.manifest_root)
        self.assertIn("QUARANTINED", str(caught.exception))

    def test_no_clearance_is_issued(self):
        exc = self._fail_post_gates()
        self.assertIn("no finalization clearance", str(exc))

    def test_nothing_was_deleted(self):
        before = ex.measure_monolith(self.monolith)
        self._fail_post_gates()
        after = ex.measure_monolith(self.monolith)
        self.assertEqual(before["monolith_content_ledger"]["ledger_digest"],
                         after["monolith_content_ledger"]["ledger_digest"])

    def test_an_unreadable_quarantine_is_not_an_absent_one(self):
        with open(oc.quarantine_path(self.manifest_root), "w") as fh:
            fh.write("{not json")
        problem = oc.quarantine_problem(self.manifest_root)
        self.assertIn("could not be read", problem)

    def test_a_clean_root_has_no_quarantine(self):
        self.assertIsNone(oc.quarantine_problem(self.manifest_root))
        self.assertIsNone(oc.read_quarantine(self.manifest_root))

    def test_reconciling_by_removing_the_record_lets_work_resume(self):
        """The record is the gate; removing it is the operator's explicit act."""
        self._fail_post_gates()
        os.remove(oc.quarantine_path(self.manifest_root))
        self.assertIsNone(oc.quarantine_problem(self.manifest_root))

    def test_the_quarantine_says_how_to_reconcile(self):
        self._fail_post_gates()
        record = oc.read_quarantine(self.manifest_root)
        self.assertIn("execute_rollback", record["how_to_reconcile"])
        self.assertIn("Until it is removed", record["how_to_reconcile"])


class TestEveryPostGateReachesTheQuarantine(_CompactionBase):
    """Failure injection per gate, as the review asked. Each must quarantine, not just raise."""

    def _assert_quarantined(self, _ctx=None, **over):
        import contextlib
        with (_ctx or contextlib.nullcontext()):
            with self.assertRaises(oc.GenerationQuarantined):
                self.compact(**over)
        record = oc.read_quarantine(self.manifest_root)
        self.assertIsNotNone(record, "the generation was left unclassified")
        self.assertEqual(record["generation"], 2)
        # And the predecessor is still there to go back to.
        self.assertTrue(os.path.isfile(
            os.path.join(self.manifest_root, record["predecessor_archive"])))
        return record

    def test_api_parity_failure_quarantines(self):
        record = self._assert_quarantined(self.forced_api_mismatch())
        self.assertIn("mismatch", str(record["reasons"]))

    def test_a_wal_binding_failure_quarantines(self):
        record = self._assert_quarantined(mock.patch.object(
            oc, "_wal_anchor_evidence",
            return_value={"bound": False, "anchor_intact": True,
                          "reason": "bound to a previous manifest root"}))
        self.assertIn("WAL", str(record["reasons"]))

    def test_a_missing_wal_anchor_quarantines(self):
        record = self._assert_quarantined(mock.patch.object(
            oc, "_wal_anchor_evidence", return_value={"bound": True, "anchor_intact": False}))
        self.assertIn("anchor", str(record["reasons"]))

    def test_a_capacity_after_failure_quarantines(self):
        """A `capacity_after` the gate cannot read reaches the quarantine like any other.

        `free_now_gib` is made a bool rather than a bad timestamp: the operation stamps the
        time itself, so a stale stamp is unreachable through it — and `True` is an `int` that
        would compare against a floor without complaint, which is the case the raw-type check
        exists for."""
        real = prod.measure_capacity
        calls = {"n": 0}

        def bad_after(*a, **kw):
            calls["n"] += 1
            out = dict(real(*a, **kw))
            if calls["n"] > 1:
                out["free_now_gib"] = True
            return out

        record = self._assert_quarantined(
            mock.patch.object(prod, "measure_capacity", side_effect=bad_after))
        self.assertIn("capacity_after", str(record["reasons"]))

    def test_a_source_map_failure_quarantines(self):
        """Source-map completeness is a PRE-publish gate, so it cannot reach the quarantine —
        and that is the stronger outcome, not a gap: nothing is published at all.

        Asserted here rather than left implicit, because "it did not quarantine" and "it did not
        publish" look the same from outside if nobody checks which."""
        with mock.patch.object(oc, "_source_map_complete", return_value=False):
            with self.assertRaises(oc.OneShotCompactionRefused) as caught:
                self.compact()
        self.assertNotIsInstance(caught.exception, oc.GenerationQuarantined)
        self.assertIsNone(oc.read_quarantine(self.manifest_root))
        self.assertEqual(self.manifest_generation(), 1,
                         "a pre-publish gate must refuse before the authority changes")

    def test_a_post_source_map_failure_quarantines(self):
        """The post-gate copy of the same field, which CAN be reached after publication."""
        real = ex.post_compaction_problem

        def failing(evidence, **kw):
            evidence = dict(evidence, source_map_complete=False)
            return real(evidence, **kw)

        record = self._assert_quarantined(
            mock.patch.object(ex, "post_compaction_problem", side_effect=failing))
        self.assertIn("source map", str(record["reasons"]))


class TestTheModuleDocumentsWhatItActuallyDoes(unittest.TestCase):
    """The header is the first thing a reviewer reads, and it aged twice in this file."""

    def _doc(self):
        return " ".join((inspect.getdoc(oc) or "").split())

    def test_it_documents_the_identity_check_and_its_position(self):
        doc = self._doc()
        self.assertIn("verify the DEPLOYMENT IDENTITY", doc)
        self.assertIn("before any lock, before any scan", doc)
        self.assertIn("an approval is not enough", doc)

    def test_it_records_that_the_misleading_parameter_is_gone(self):
        doc = self._doc()
        self.assertIn("no `expected_module_fingerprints` parameter", doc)
        self.assertIn("worse than an absent one", doc)

    def test_it_documents_parity_as_bound_artifacts(self):
        doc = self._doc()
        self.assertIn("Parity evidence is executed and bound", doc)
        self.assertIn('returning `{"mismatches": 0}` is refused', doc)

    def test_it_documents_the_quarantine_and_why_it_is_not_a_rollback(self):
        doc = self._doc()
        self.assertIn("A failed generation is quarantined", doc)
        self.assertIn("Not an automatic rollback", doc)
        self.assertIn("nobody proceeds *without* deciding", doc)

    def test_it_still_says_it_deletes_nothing(self):
        doc = self._doc()
        self.assertIn("It deletes nothing", doc)
        self.assertIn("It does not switch the service", doc)


class TestARaisedExceptionAfterThePublishAlsoQuarantines(_CompactionBase):
    """Finding 1: a RETURNED bad artifact was covered; a RAISED exception was not.

    Everything after the authority changed runs inside one guard, because the failure modes are
    not symmetric: a gate that returns a problem was written to be handled, and a probe that
    throws was not. Both leave generation N+1 live, and only the first used to be recorded.

    Each test here injects a real exception from a different post-publish step and requires the
    same three things: the run raises `GenerationQuarantined`, a durable record names the
    generation and its predecessor archive, and the original exception survives as `__cause__`
    so whoever reads the traceback sees what actually broke."""

    def _assert_quarantined_from(self, patch, marker):
        with patch:
            with self.assertRaises(oc.GenerationQuarantined) as caught:
                self.compact()
        record = oc.read_quarantine(self.manifest_root)
        self.assertIsNotNone(record, "an exception escaped without classifying the generation")
        self.assertEqual(record["generation"], 2)
        self.assertEqual(record["predecessor_generation"], 1)
        self.assertTrue(os.path.isfile(
            os.path.join(self.manifest_root, record["predecessor_archive"])))
        self.assertIn(marker, str(record["reasons"]))
        self.assertIsNotNone(caught.exception.__cause__,
                             "the original exception was discarded")
        return caught.exception

    def test_a_raising_fence_probe_quarantines(self):
        """The third probe, after the publish."""
        calls = {"n": 0}
        fence = self._fence()

        def probe():
            calls["n"] += 1
            if calls["n"] >= 3:
                raise RuntimeError("pm2 is unreachable")
            return fence

        with self.assertRaises(oc.GenerationQuarantined) as caught:
            self.compact(writer_fence_probe=probe)
        record = oc.read_quarantine(self.manifest_root)
        self.assertIsNotNone(record)
        self.assertIn("pm2 is unreachable", str(record["reasons"]))
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)

    def test_a_raising_load_live_quarantines(self):
        """The manifest cannot be re-read after publishing it.

        Armed only AFTER the publish: `bm.load_live` is the same function `plan_publication`
        and `execute_publication` use, so patching it outright raised before anything was
        published — which is a different (and safe) outcome, and would have made this test pass
        while proving nothing about the post-publish guard."""
        real_load, real_execute = oc.bm.load_live, oc._execute
        armed = {"yes": False}

        def execute(candidate, **kw):
            out = real_execute(candidate, **kw)
            armed["yes"] = True
            return out

        def load_live(root):
            if armed["yes"]:
                raise RuntimeError("manifest unreadable")
            return real_load(root)

        with mock.patch.object(oc, "_execute", side_effect=execute):
            self._assert_quarantined_from(
                mock.patch.object(oc.bm, "load_live", side_effect=load_live),
                "manifest unreadable")

    def test_a_raising_api_parity_producer_quarantines(self):
        exc = self._assert_quarantined_from(self.forced_api_exception(),
                                            "served reader exploded")
        self.assertIn("QUARANTINED", str(exc))

    def test_a_raising_wal_evidence_quarantines(self):
        self._assert_quarantined_from(
            mock.patch.object(oc, "_wal_anchor_evidence",
                              side_effect=OSError("the WAL device went away")),
            "the WAL device went away")

    def test_a_raising_measure_capacity_on_the_after_call_quarantines(self):
        real = prod.measure_capacity
        calls = {"n": 0}

        def failing(*a, **kw):
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError("statvfs failed")
            return real(*a, **kw)

        self._assert_quarantined_from(
            mock.patch.object(prod, "measure_capacity", side_effect=failing),
            "statvfs failed")

    def test_a_raising_inventory_on_the_after_call_quarantines(self):
        real = oc.inventory
        calls = {"n": 0}

        def failing(paths):
            calls["n"] += 1
            if calls["n"] > 1:
                raise OSError("the store vanished mid-walk")
            return real(paths)

        self._assert_quarantined_from(
            mock.patch.object(oc, "inventory", side_effect=failing),
            "the store vanished mid-walk")

    def test_a_raising_post_gate_check_quarantines(self):
        self._assert_quarantined_from(
            mock.patch.object(ex, "post_compaction_problem",
                              side_effect=TypeError("the gate itself broke")),
            "the gate itself broke")

    def test_a_raising_clearance_issue_quarantines(self):
        """The very last step. Even here the generation must not be left unclassified."""
        self._assert_quarantined_from(
            mock.patch.object(ex, "issue_finalization_clearance",
                              side_effect=RuntimeError("clearance write failed")),
            "clearance write failed")

    def test_every_such_failure_still_stops_the_next_run(self):
        with self.forced_api_exception():
            with self.assertRaises(oc.GenerationQuarantined):
                self.compact()
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact()
        self.assertIn("QUARANTINED", str(caught.exception))

    def test_every_such_failure_still_stops_finalization(self):
        with self.forced_api_exception():
            with self.assertRaises(oc.GenerationQuarantined):
                self.compact()
        clearance = ex.issue_finalization_clearance(
            host=socket.gethostname(), run_id=self.RUN_ID, generation=2,
            manifest_checksum="f" * 64, monolith_day_digest="d" * 64,
            source_map_digest="9" * 64, issued_by="tests", gates_passed=True)
        with self.assertRaises(ex.FinalizationRefused) as caught:
            ex.refuse_destructive_step("prune", clearance, host=socket.gethostname(),
                                       run_id=self.RUN_ID, generation=2,
                                       manifest_checksum="f" * 64, approval=True,
                                       manifest_root=self.manifest_root)
        self.assertIn("QUARANTINED", str(caught.exception))

    def test_nothing_is_deleted_on_any_of_these_paths(self):
        before = ex.measure_monolith(self.monolith)
        with self.forced_api_exception():
            with self.assertRaises(oc.GenerationQuarantined):
                self.compact()
        after = ex.measure_monolith(self.monolith)
        self.assertEqual(before["monolith_content_ledger"]["ledger_digest"],
                         after["monolith_content_ledger"]["ledger_digest"])


class TestAQuarantineThatCannotBeWrittenFailsClosedAndSaysSo(_CompactionBase):
    """The worst state reachable: published, unverified, and no record on disk.

    It gets its own exception type because reporting it as an ordinary quarantine would claim a
    guarantee that does not exist — the next run would find a clean manifest root and proceed."""

    def _break_the_write(self):
        return mock.patch.object(oc, "quarantine_generation",
                                 side_effect=OSError("read-only filesystem"))

    def test_it_raises_the_typed_failure_not_a_quarantine(self):
        with self._break_the_write(), self.forced_api_mismatch():
            with self.assertRaises(oc.QuarantineWriteFailed) as caught:
                self.compact()
        self.assertNotIsInstance(caught.exception, oc.GenerationQuarantined)

    def test_it_names_the_published_but_unquarantined_generation(self):
        with self._break_the_write(), self.forced_api_mismatch():
            with self.assertRaises(oc.QuarantineWriteFailed) as caught:
                self.compact()
        self.assertEqual(caught.exception.generation, 2)
        self.assertEqual(caught.exception.manifest_root, self.manifest_root)
        self.assertEqual(caught.exception.predecessor_archive, "manifest.gen000001.json")

    def test_it_demands_manual_intervention_explicitly(self):
        with self._break_the_write(), self.forced_api_mismatch():
            with self.assertRaises(oc.QuarantineWriteFailed) as caught:
                self.compact()
        message = str(caught.exception)
        self.assertIn("MANUAL INTERVENTION REQUIRED", message)
        self.assertIn("PUBLISHED", message)
        self.assertIn("no record on disk", message)
        self.assertIn("read-only filesystem", message)

    def test_it_does_not_claim_a_normal_quarantine(self):
        with self._break_the_write(), self.forced_api_mismatch():
            with self.assertRaises(oc.QuarantineWriteFailed):
                self.compact()
        self.assertIsNone(oc.read_quarantine(self.manifest_root),
                          "a record exists, so this was not the fail-closed path at all")

    def test_it_also_covers_a_raised_failure_not_only_a_returned_one(self):
        with self._break_the_write(), self.forced_api_exception():
            with self.assertRaises(oc.QuarantineWriteFailed) as caught:
                self.compact()
        self.assertIn("MANUAL INTERVENTION REQUIRED", str(caught.exception))

    def test_it_still_deletes_nothing(self):
        before = ex.measure_monolith(self.monolith)
        with self._break_the_write(), self.forced_api_mismatch():
            with self.assertRaises(oc.QuarantineWriteFailed):
                self.compact()
        after = ex.measure_monolith(self.monolith)
        self.assertEqual(before["monolith_content_ledger"]["ledger_digest"],
                         after["monolith_content_ledger"]["ledger_digest"])


class TestTheApprovalDoesNotAuthorizeArbitraryPaths(_CompactionBase):
    """Finding 3: an approval names a live root, and used to authorize writing anywhere.

    Every one of these has a VALID approval for the real live root. What differs is where the
    operation was pointed, and each must refuse before anything is written."""

    def _elsewhere(self, name):
        path = os.path.join(self.tmp, name)
        os.makedirs(path, exist_ok=True)
        return path

    def _assert_refused_before_writing(self, **over):
        gen = self.manifest_generation()
        with mock.patch.object(oc, "_build", side_effect=AssertionError("must not run")) \
                as build:
            with self.assertRaises(oc.OneShotCompactionRefused) as caught:
                self.compact(**over)
        self.assertFalse(build.called, "a path was accepted far enough to start a build")
        self.assertEqual(self.manifest_generation(), gen)
        return str(caught.exception)

    def test_an_alternate_manifest_root_refuses(self):
        message = self._assert_refused_before_writing(
            manifest_root=self._elsewhere("someone_elses_blocks"))
        self.assertIn("is not the canonical", message)
        self.assertIn("computed, not chosen", message)

    def test_an_alternate_wal_root_refuses(self):
        message = self._assert_refused_before_writing(
            wal_root=self._elsewhere("someone_elses_wal"))
        self.assertIn("is not the canonical", message)

    def test_a_block_written_outside_the_manifest_root_refuses(self):
        message = self._assert_refused_before_writing(
            block_out_path=os.path.join(self.tmp, "loose_block.zarr"))
        self.assertIn("not inside the manifest root", message)

    def test_a_block_written_into_the_live_root_but_outside_the_roots_refuses(self):
        message = self._assert_refused_before_writing(
            block_out_path=os.path.join(self.live, "sneaky_block.zarr"))
        self.assertIn("not inside the manifest root", message)

    def test_a_relative_artifacts_dir_refuses(self):
        message = self._assert_refused_before_writing(artifacts_dir="artifacts")
        self.assertIn("must be an absolute path", message)

    def test_an_anchor_inside_the_live_root_refuses(self):
        message = self._assert_refused_before_writing(
            anchor_root=self._elsewhere(os.path.join("G", "anchor_inside")))
        self.assertIn("witnesses nothing", message)

    def test_an_anchor_on_the_same_device_refuses(self):
        """The existing anchor authority, reached through this operation."""
        other = self._elsewhere("undeclared_anchor")
        with self.assertRaises((oc.OneShotCompactionRefused,
                                prod.ProductionProvisionRefused)) as caught:
            self.compact(anchor_root=other)
        self.assertTrue(str(caught.exception))

    def test_an_unapproved_delta_path_refuses(self):
        rogue = os.path.join(self.tmp, "rogue_delta.zarr")
        fx.build_block(rogue, self.days, ny=8, nx=8)
        message = self._assert_refused_before_writing(delta_path=rogue)
        self.assertIn("is not the", message)
        self.assertIn("the approved preflight names", message)

    def test_an_unapproved_daily_root_refuses(self):
        rogue = self._elsewhere("rogue_daily")
        message = self._assert_refused_before_writing(daily_root=rogue)
        self.assertIn("names no daily_root", message)

    def test_an_unapproved_monolith_refuses(self):
        rogue = os.path.join(self.tmp, "rogue_monolith.zarr")
        fx.build_block(rogue, self.base_days, ny=8, nx=8)
        with self.assertRaises((oc.OneShotCompactionRefused,
                                ex.OneShotExceptionRefused)) as caught:
            self.compact(monolith_path=rogue)
        self.assertTrue(str(caught.exception))

    def test_a_symlinked_source_refuses(self):
        """A link can be re-pointed between the approval and the run."""
        link = os.path.join(self.tmp, "delta_link.zarr")
        os.symlink(self.delta, link)
        pre = self._pre()
        pre["plan"]["paths"]["delta_path"] = link
        message = self._assert_refused_before_writing(delta_path=link,
                                                      one_shot_pre_compaction=pre)
        self.assertIn("is a symlink to", message)

    def test_a_relative_source_path_refuses(self):
        pre = self._pre()
        pre["plan"]["paths"]["delta_path"] = "relative/delta.zarr"
        message = self._assert_refused_before_writing(delta_path="relative/delta.zarr",
                                                      one_shot_pre_compaction=pre)
        self.assertIn("is not absolute", message)

    def test_a_nonexistent_source_refuses(self):
        pre = self._pre()
        gone = os.path.join(self.tmp, "not_here.zarr")
        pre["plan"]["paths"]["delta_path"] = gone
        message = self._assert_refused_before_writing(delta_path=gone,
                                                      one_shot_pre_compaction=pre)
        self.assertIn("does not exist", message)

    def test_the_canonical_roots_are_computed_from_the_live_root(self):
        roots = oc.canonical_roots(self.live)
        self.assertEqual(roots["manifest_root"], self.manifest_root)
        self.assertEqual(roots["wal_root"], self.wal_root)

    def test_the_happy_path_still_uses_the_canonical_roots(self):
        """The positive control: the operation is not simply refusing everything."""
        result = self.compact()
        self.assertEqual(result["manifest_root"], self.manifest_root)


class TestTheCompactionApprovalIsValidatedField(_CompactionBase):
    """Finding 4: presence is not validity, and each guard must stand on its own.

    Every case here changes ONE field and leaves the rest correct, so a neighbouring check
    cannot be what refuses it."""

    def problem(self, **over):
        return oc.approval_problem(self._approval(**over), live_root=self.live,
                                   run_id=self.RUN_ID)

    def test_a_complete_approval_passes(self):
        self.assertIsNone(self.problem())

    def test_every_field_is_required(self):
        for field in oc.APPROVAL_FIELDS:
            with self.subTest(field):
                approval = self._approval()
                del approval[field]
                problem = oc.approval_problem(approval, live_root=self.live,
                                              run_id=self.RUN_ID)
                self.assertTrue(problem)
                self.assertIn(field, problem)

    def test_a_record_that_is_not_a_record_is_refused(self):
        for value in (None, True, "approved", 1, []):
            with self.subTest(repr(value)):
                problem = oc.approval_problem(value, live_root=self.live, run_id=self.RUN_ID)
                self.assertIn("not a record", problem)

    def test_a_non_string_approved_by_is_refused(self):
        """`True` is not a name, and truthiness would have accepted it."""
        for value in (True, 1, ["cywhale"], {"name": "cywhale"}, None):
            with self.subTest(repr(value)):
                problem = self.problem(approved_by=value)
                self.assertIn("rather than a string", problem)

    def test_a_blank_approved_by_is_refused(self):
        for value in ("", "   ", "\t"):
            with self.subTest(repr(value)):
                self.assertTrue(self.problem(approved_by=value))

    def test_a_one_character_approved_by_is_refused(self):
        self.assertIn("does not name anyone", self.problem(approved_by="x"))

    def test_a_non_string_approved_utc_is_refused(self):
        for value in (True, 1724668800, None, []):
            with self.subTest(repr(value)):
                self.assertIn("rather than a string", self.problem(approved_utc=value))

    def test_an_unparseable_approved_utc_is_refused(self):
        for value in ("soon", "2026-08-26", "yesterday", "2026-13-45T00:00:00Z"):
            with self.subTest(value):
                problem = self.problem(approved_utc=value)
                self.assertIn("not a timestamp", problem)

    def test_a_future_approved_utc_is_refused(self):
        problem = self.problem(approved_utc=iso(3600))
        self.assertIn("in the future", problem)

    def test_a_non_string_run_id_is_refused(self):
        for value in (True, 20260826, None):
            with self.subTest(repr(value)):
                self.assertIn("rather than a string", self.problem(run_id=value))

    def test_another_run_is_refused(self):
        self.assertIn("is for run", self.problem(run_id="some-other-run"))

    def test_another_live_root_is_refused(self):
        self.assertIn("names live_root", self.problem(live_root=self.tmp))

    def test_a_relative_live_root_is_refused(self):
        self.assertIn("not an absolute path", self.problem(live_root="G"))

    def test_the_wrong_scope_is_refused(self):
        problem = self.problem(scope=prod.PRODUCTION_SCOPE)
        self.assertIn("not an approval to publish", problem)

    def test_an_expiry_is_honoured_when_present(self):
        self.assertIn("expired at", self.problem(approved_utc=iso(-600),
                                                 expires_utc=iso(-1)))

    def test_an_expiry_before_the_approval_is_refused(self):
        problem = self.problem(approved_utc=iso(-60), expires_utc=iso(-600))
        self.assertIn("authorizes nothing", problem)

    def test_an_unparseable_expiry_is_refused(self):
        self.assertIn("not a timestamp", self.problem(expires_utc="never"))

    def test_a_live_expiry_passes(self):
        self.assertIsNone(self.problem(expires_utc=iso(3000)))

    def test_the_operation_refuses_a_malformed_approval_before_anything_else(self):
        with mock.patch.object(oc, "_build", side_effect=AssertionError("must not run")):
            with self.assertRaises(oc.OneShotCompactionRefused) as caught:
                self.compact(approval=self._approval(approved_by=True))
        self.assertIn("compaction approval", str(caught.exception))


class TestTheProducerDerivationEdges(_CompactionBase):
    """The branches the end-to-end path cannot reach, fed directly.

    Each of these was a mutation survivor: the operation only ever calls `producer_identity`
    with the two registered kinds and with callables that do have a module file, so the guards
    for everything else had never executed."""

    def _fingerprints(self):
        return self._identity()["module_fingerprints"]

    def test_a_callable_with_no_module_file_is_refused(self):
        """A function built from a code object — what a REPL or an `exec` produces."""
        import types
        source = compile("def f(**kw):\n    return {}", "<interactive>", "exec")
        nowhere = types.FunctionType(source.co_consts[0], {})
        identity, problem = oc.producer_identity(
            nowhere, kind=oc.PARITY_POINT_SERIES, module_fingerprints=self._fingerprints())
        self.assertIsNone(identity)
        self.assertIn("is not the reviewed", problem)

    def test_the_operation_refuses_such_a_callable_too(self):
        import types
        source = compile("def f(**kw):\n    return {}", "<interactive>", "exec")
        nowhere = types.FunctionType(source.co_consts[0], {})
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(api_parity=nowhere)
        self.assertIn("is not the reviewed", str(caught.exception))

    def test_an_unregistered_kind_is_refused(self):
        """`producer_identity` is public and its kind is an argument; a caller asking about a
        kind nothing is registered for must be told, not quietly given an identity."""
        identity, problem = oc.producer_identity(
            ppd.point_series_parity, kind="throughput_parity",
            module_fingerprints=self._fingerprints())
        self.assertIsNone(identity)
        self.assertIn("no reviewed producer is registered", problem)

    def test_a_callable_outside_the_tree_is_refused(self):
        """A function from an installed package. Every one of these converges on the same
        refusal now, and that is the improvement: the question is no longer "where did this
        come from" but "is this the reviewed function", which has one answer."""
        import json as stdlib_json
        identity, problem = oc.producer_identity(
            stdlib_json.dumps, kind=oc.PARITY_API,
            module_fingerprints=self._fingerprints())
        self.assertIsNone(identity)
        self.assertIn("is not the reviewed", problem)


class TestASingleServedReadFailureIsAMismatch(_CompactionBase):
    """A per-day read failure must COUNT, not be skipped.

    `continue` without recording is the shape that turns a broken read path into a clean parity
    report: every day fails, every day is skipped, and the artifact says zero mismatches out of
    the days that happened to work."""

    def test_a_raising_point_series_is_counted_as_a_mismatch(self):
        from store.segmented_cube import SegmentedCubeStore
        with mock.patch.object(SegmentedCubeStore, "point_series",
                               side_effect=RuntimeError("segment unreadable")):
            with self.assertRaises(oc.GenerationQuarantined) as caught:
                self.compact()
        record = oc.read_quarantine(self.manifest_root)
        self.assertIsNotNone(record)
        self.assertIn("mismatch(es) out of", str(record["reasons"]))
        self.assertIn("QUARANTINED", str(caught.exception))

    def test_the_artifact_records_what_went_wrong(self):
        from store.segmented_cube import SegmentedCubeStore
        with mock.patch.object(SegmentedCubeStore, "point_series",
                               side_effect=RuntimeError("segment unreadable")):
            artifact = ppd.api_parity(
                manifest_root=self.manifest_root, monolith_path=self.monolith,
                allowed_legacy_paths=[os.path.realpath(self.monolith)],
                host="h", run_id="r", deploy_sha="s", generation=1,
                manifest_checksum="c", source_map_digest="d")
        self.assertGreater(artifact["mismatches"], 0)
        self.assertGreater(artifact["samples"], 0)
        self.assertTrue(any("the served read raised" in d
                            for d in artifact["differences"]))

    def test_a_raising_oracle_read_is_counted_too(self):
        from store.time_cube import TimeCubeStore
        with mock.patch.object(TimeCubeStore, "point_series",
                               side_effect=RuntimeError("oracle unreadable")):
            artifact = ppd.api_parity(
                manifest_root=self.manifest_root, monolith_path=self.monolith,
                allowed_legacy_paths=[os.path.realpath(self.monolith)],
                host="h", run_id="r", deploy_sha="s", generation=1,
                manifest_checksum="c", source_map_digest="d")
        self.assertGreater(artifact["mismatches"], 0)
        self.assertTrue(any("the oracle read raised" in d
                            for d in artifact["differences"]))

    def test_a_clean_run_reports_zero_over_a_positive_sample_count(self):
        """The positive control: the counting is not simply always non-zero."""
        artifact = ppd.api_parity(
            manifest_root=self.manifest_root, monolith_path=self.monolith,
            allowed_legacy_paths=[os.path.realpath(self.monolith)],
            host="h", run_id="r", deploy_sha="s", generation=1,
            manifest_checksum="c", source_map_digest="d")
        self.assertEqual(artifact["mismatches"], 0)
        self.assertGreater(artifact["samples"], 0)

    def test_a_silently_omitted_day_is_counted_as_a_mismatch(self):
        """Distinct from a read that RAISES, and not covered by it.

        `point_series` omits absent days rather than fabricating them, which is correct — but a
        day the oracle holds and the served view quietly drops is a served view that lost data,
        and an empty result is not an error anywhere else in the read path. Nothing but this
        branch would notice."""
        from store.segmented_cube import SegmentedCubeStore
        with mock.patch.object(SegmentedCubeStore, "point_series", return_value=[]):
            artifact = ppd.api_parity(
                manifest_root=self.manifest_root, monolith_path=self.monolith,
                allowed_legacy_paths=[os.path.realpath(self.monolith)],
                host="h", run_id="r", deploy_sha="s", generation=1,
                manifest_checksum="c", source_map_digest="d")
        self.assertGreater(artifact["mismatches"], 0)
        self.assertTrue(any("returned nothing for a day the oracle holds" in d
                            for d in artifact["differences"]))

    def test_a_silently_omitted_day_quarantines_the_run(self):
        from store.segmented_cube import SegmentedCubeStore
        with mock.patch.object(SegmentedCubeStore, "point_series", return_value=[]):
            with self.assertRaises(oc.GenerationQuarantined):
                self.compact()
        self.assertIsNotNone(oc.read_quarantine(self.manifest_root))


class TestTheProducerCannotBeImpersonated(_CompactionBase):
    """`__module__` and `__qualname__` are WRITABLE. Deriving identity from them was forgeable.

        fake.__module__  = "ops.p5_parity_producers"
        fake.__qualname__ = "point_series_parity"

    was enough for a stub returning `{"mismatches": 0}` to obtain a valid identity carrying the
    real module's digest — `inspect.getmodule` consults `__module__` first, so it resolved the
    genuine module and everything downstream agreed the result came from reviewed code.

    The registry is now resolved first and the callable must BE that object."""

    def _fingerprints(self):
        return self._identity()["module_fingerprints"]

    @staticmethod
    def _green_artifact(**kw):
        return {
            "kind": oc.PARITY_POINT_SERIES,
            "host": kw["host"], "run_id": kw["run_id"], "deploy_sha": kw["deploy_sha"],
            "producer": "ops/p5_parity_producers.py:point_series_parity",
            "generation": kw["generation"], "manifest_checksum": kw["manifest_checksum"],
            "source_map_digest": kw["source_map_digest"],
            "executed_utc": iso(), "samples": 40, "mismatches": 0,
            "provenance": "compared point-wise against each day's own source",
            "days_checked": len(kw.get("days") or [1]), "boundaries_included": True,
        }

    # ---------------------------------------------------------------- 1. a plain green fake
    def test_a_fake_returning_a_complete_green_artifact_is_refused(self):
        def fake(**kw):
            return dict(self._green_artifact(**kw),
                        producer_digest=oc._file_digest(
                            os.path.join(_DEV, "ops/p5_parity_producers.py")))

        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(point_series_parity=fake)
        self.assertIn("is not the reviewed", str(caught.exception))
        self.assertEqual(self.manifest_generation(), 1)

    # ---------------------------------------------------------------- 2. the forgery
    def test_a_fake_forging_module_qualname_and_digest_is_refused(self):
        """The exact impersonation the previous revision accepted."""
        legitimate = self._fingerprints()["ops/p5_parity_producers.py"]

        def forged(**kw):
            return dict(self._green_artifact(**kw), producer_digest=legitimate)

        forged.__module__ = "ops.p5_parity_producers"
        forged.__qualname__ = "point_series_parity"

        identity, problem = oc.producer_identity(
            forged, kind=oc.PARITY_POINT_SERIES,
            module_fingerprints=self._fingerprints())
        self.assertIsNone(identity, "a forged __module__/__qualname__ obtained an identity")
        self.assertIn("writable attributes", problem)

        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(point_series_parity=forged)
        self.assertIn("is not the reviewed", str(caught.exception))
        self.assertEqual(self.manifest_generation(), 1)

    def test_the_forgery_is_refused_for_the_api_kind_too(self):
        def forged(**kw):
            return {"kind": oc.PARITY_API, "mismatches": 0, "samples": 4}
        forged.__module__ = "ops.p5_parity_producers"
        forged.__qualname__ = "api_parity"
        identity, problem = oc.producer_identity(
            forged, kind=oc.PARITY_API, module_fingerprints=self._fingerprints())
        self.assertIsNone(identity)
        self.assertIn("is not the reviewed", problem)

    def test_a_copy_of_the_real_function_is_still_refused(self):
        """Same code, same name, same module — a different object.

        `is`, not equality: a copy is not the function the review read, and accepting one would
        let a wrapper that filters the result stand in for the producer."""
        import types
        real = ppd.point_series_parity
        clone = types.FunctionType(real.__code__, real.__globals__, real.__name__,
                                   real.__defaults__, real.__closure__)
        clone.__module__ = real.__module__
        clone.__qualname__ = real.__qualname__
        identity, problem = oc.producer_identity(
            clone, kind=oc.PARITY_POINT_SERIES, module_fingerprints=self._fingerprints())
        self.assertIsNone(identity)
        self.assertIn("is not the reviewed", problem)

    def test_a_wrapper_around_the_real_function_is_refused(self):
        """The subtlest impersonation: it really does call the reviewed producer, then edits
        the result. `functools.wraps` copies `__module__` and `__qualname__` for free."""
        import functools

        @functools.wraps(ppd.point_series_parity)
        def wrapper(**kw):
            out = ppd.point_series_parity(**kw)
            out["mismatches"] = 0
            return out

        identity, problem = oc.producer_identity(
            wrapper, kind=oc.PARITY_POINT_SERIES,
            module_fingerprints=self._fingerprints())
        self.assertIsNone(identity)
        self.assertIn("is not the reviewed", problem)

    # ---------------------------------------------------------------- 3. the positive control
    def test_both_reviewed_producers_still_pass(self):
        for kind, fn in ((oc.PARITY_POINT_SERIES, ppd.point_series_parity),
                         (oc.PARITY_API, ppd.api_parity)):
            with self.subTest(kind):
                identity, problem = oc.producer_identity(
                    fn, kind=kind, module_fingerprints=self._fingerprints())
                self.assertIsNone(problem)
                self.assertEqual(identity["producer"],
                                 f"ops/p5_parity_producers.py:{kind}")

    def test_the_operation_completes_through_the_reviewed_producers(self):
        result = self.compact()
        self.assertEqual(result["generation"], 2)
        self.assertIsNotNone(result["finalization_clearance"])

    # ---------------------------------------------------------------- 4. digest disagreement
    def test_a_producer_digest_that_does_not_match_the_approved_identity_is_refused(self):
        """The module resolves, the object IS the reviewed one, and the file has changed."""
        prints = dict(self._fingerprints())
        prints["ops/p5_parity_producers.py"] = "0" * 64
        identity, problem = oc.producer_identity(
            ppd.point_series_parity, kind=oc.PARITY_POINT_SERIES,
            module_fingerprints=prints)
        self.assertIsNone(identity)
        self.assertIn("is not the one that would run", problem)

        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(identity_artifact=self._identity(module_fingerprints=prints))
        self.assertIn("is not the one that would run", str(caught.exception))
        self.assertEqual(self.manifest_generation(), 1)

    def test_an_identity_that_does_not_fingerprint_the_producer_module_is_refused(self):
        prints = {k: v for k, v in self._fingerprints().items()
                  if k != "ops/p5_parity_producers.py"}
        identity, problem = oc.producer_identity(
            ppd.api_parity, kind=oc.PARITY_API, module_fingerprints=prints)
        self.assertIsNone(identity)
        self.assertIn("does not fingerprint", problem)

    # ---------------------------------------------------------------- the registry itself
    def test_an_unresolvable_registry_entry_fails_closed(self):
        with mock.patch.dict(oc.REVIEWED_PRODUCERS,
                             {oc.PARITY_POINT_SERIES: ("ops/nothing_here.py", "nope")}):
            identity, problem = oc.producer_identity(
                ppd.point_series_parity, kind=oc.PARITY_POINT_SERIES,
                module_fingerprints=self._fingerprints())
        self.assertIsNone(identity)
        self.assertIn("could not be resolved", problem)

    def test_a_registry_entry_naming_a_missing_attribute_fails_closed(self):
        with mock.patch.dict(oc.REVIEWED_PRODUCERS,
                             {oc.PARITY_API: ("ops/p5_parity_producers.py", "not_a_function")}):
            identity, problem = oc.producer_identity(
                ppd.api_parity, kind=oc.PARITY_API,
                module_fingerprints=self._fingerprints())
        self.assertIsNone(identity)
        self.assertIn("could not be resolved", problem)

    def test_a_registry_entry_that_is_not_a_module_path_is_refused(self):
        with mock.patch.dict(oc.REVIEWED_PRODUCERS,
                             {oc.PARITY_API: ("ops/p5_parity_producers", "api_parity")}):
            with self.assertRaises(oc.OneShotCompactionRefused) as caught:
                oc.producer_identity(ppd.api_parity, kind=oc.PARITY_API,
                                     module_fingerprints=self._fingerprints())
        self.assertIn("not a module path", str(caught.exception))

    def test_the_identity_never_consults_what_the_callable_claims(self):
        """By AST over the CODE, not the source text.

        A text scan would trip on the docstring, which names the forgeable attributes in order
        to explain why they are not used. This walks the function body and asserts no attribute
        is read off the `fn` parameter at all."""
        tree = ast.parse(textwrap.dedent(inspect.getsource(oc.producer_identity)))
        reads = {node.attr for node in ast.walk(tree)
                 if isinstance(node, ast.Attribute)
                 and isinstance(node.value, ast.Name) and node.value.id == "fn"}
        self.assertEqual(reads, set(),
                         f"the derivation reads {sorted(reads)} off the callable it is meant "
                         f"to be checking")
        body = ast.parse(textwrap.dedent(inspect.getsource(oc.producer_identity)))
        called = {getattr(n.func, "attr", None) for n in ast.walk(body)
                  if isinstance(n, ast.Call)}
        self.assertNotIn("getmodule", called)

    def test_a_reviewed_module_with_no_file_fails_closed(self):
        """Guards the REVIEWED module, not the caller's callable.

        Reachable if the registry ever names something importable without a source file — a
        namespace package, a frozen or builtin module. There is then nothing to fingerprint,
        and proceeding would mean running a producer whose bytes nobody can check."""
        with mock.patch.object(ppd, "__file__", None):
            identity, problem = oc.producer_identity(
                ppd.api_parity, kind=oc.PARITY_API,
                module_fingerprints=self._fingerprints())
        self.assertIsNone(identity)
        self.assertIn("no file on disk to fingerprint", problem)

    def test_an_import_resolving_outside_the_registry_path_fails_closed(self):
        """`sys.path` shadowing: the name imports, but not from where the registry says.

        Another `ops/p5_parity_producers.py` earlier on the path would be imported instead, and
        the digest check would then compare the SHADOW against the fingerprint — which is the
        wrong file to be reassured about. The path the import actually resolved to has to be
        the path the registry named."""
        with mock.patch.object(ppd, "__file__", "/somewhere/else/p5_parity_producers.py"):
            identity, problem = oc.producer_identity(
                ppd.point_series_parity, kind=oc.PARITY_POINT_SERIES,
                module_fingerprints=self._fingerprints())
        self.assertIsNone(identity)
        self.assertIn("not where the registry says it lives", problem)

    def test_an_import_resolving_elsewhere_inside_the_tree_also_fails_closed(self):
        """Inside the tree but at the wrong path is still the wrong file."""
        with mock.patch.object(ppd, "__file__",
                               os.path.join(_DEV, "ops", "p5_readiness.py")):
            identity, problem = oc.producer_identity(
                ppd.api_parity, kind=oc.PARITY_API,
                module_fingerprints=self._fingerprints())
        self.assertIsNone(identity)
        self.assertIn("not where the registry says it lives", problem)
