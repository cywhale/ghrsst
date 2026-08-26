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
from ops import p5_oneshot_exception as ex  # noqa: E402
from ops import p5_prod_provision as prod  # noqa: E402
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
                     "destination_manifest": "gen000002", "days": list(self.days)},
        }
        p.update(over)
        return p

    def _approval(self, **over):
        a = {"scope": oc.COMPACTION_SCOPE, "approved_by": "cywhale",
             "approved_utc": iso(-600), "live_root": self.live, "run_id": self.RUN_ID}
        a.update(over)
        return a

    # ---------------------------------------------------------------- injected probes
    def _fence_probe(self):
        self.probe_calls.append(iso())
        return self._fence()

    def _parity(self, **over):
        def probe(*, block_path, source_map, monolith_path, days):
            # A real probe reads each day from the store the source map names and compares it
            # point-wise against the block. The oracle differs per day, which is the whole
            # reason the map is passed in.
            oracles = sorted({r["source_path"] for r in source_map.values()})
            out = {"days_checked": len(days), "mismatches": 0, "boundaries_included": True,
                   "first_day": days[0], "last_day": days[-1],
                   "block_path": block_path, "oracles": oracles}
            out.update(over)
            return out
        return probe

    def _api_parity(self, **over):
        def probe(*, manifest_root, monolith_path, allowed_legacy_paths):
            out = {"routes_checked": 4, "mismatches": 0, "manifest_root": manifest_root}
            out.update(over)
            return out
        return probe

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
            block_out_path=os.path.join(self.manifest_root, "block_gen2.zarr"),
            writer_fence_probe=self._fence_probe,
            point_series_parity=self._parity(),
            api_parity=self._api_parity(),
        )
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
        message = self._assert_refused_without_clearance(
            point_series_parity=self._parity(mismatches=3))
        self.assertIn("point-series mismatch", message)
        self.assertEqual(self.manifest_generation(), gen,
                         "a parity failure must be caught while the block is unreferenced")

    def test_parity_that_skipped_the_boundaries_refuses_before_publishing(self):
        gen = self.manifest_generation()
        message = self._assert_refused_without_clearance(
            point_series_parity=self._parity(boundaries_included=False))
        self.assertIn("first and last days", message)
        self.assertEqual(self.manifest_generation(), gen)

    def test_parity_over_zero_days_refuses_before_publishing(self):
        gen = self.manifest_generation()
        self._assert_refused_without_clearance(
            point_series_parity=self._parity(days_checked=0))
        self.assertEqual(self.manifest_generation(), gen)

    def test_an_api_parity_mismatch_refuses_and_issues_no_clearance(self):
        message = self._assert_refused_without_clearance(
            api_parity=self._api_parity(mismatches=2))
        self.assertIn("API response mismatch", message)

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
            with self.assertRaises(prod.ProductionProvisionRefused):
                self.compact()
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
        with self.assertRaises(oc.OneShotCompactionRefused):
            self.compact(api_parity=self._api_parity(mismatches=1))
        after = ex.measure_monolith(self.monolith)
        self.assertEqual(before["monolith_content_ledger"]["ledger_digest"],
                         after["monolith_content_ledger"]["ledger_digest"])

    def test_a_post_gate_failure_says_what_state_it_left(self):
        with self.assertRaises(oc.OneShotCompactionRefused) as caught:
            self.compact(api_parity=self._api_parity(mismatches=1))
        message = str(caught.exception)
        self.assertIn("published and NOT cleared", message)
        self.assertIn("nothing was deleted", message)
        self.assertIn("no service was switched", message)


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
