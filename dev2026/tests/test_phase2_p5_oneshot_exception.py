"""dev2026 — the one-shot exception: a bound capability, not a flag.

A human accepted the risk of running ONE operation over a still-writable monolith, because VM24
has full automated VM backups. That is a claim about recoverability, not about likelihood, and
this suite holds the code to treating it as the former.

What the exception must never become: a flag. `force`, `skip_seal`, `allow_unsealed`,
`require_sealed=False` and an environment variable are all bypasses whose blast radius is
"whatever the caller felt like", reusable by the next caller who finds one, and unauditable
afterwards. So the exception is a record bound to one host, one run, one operation, one monolith
at one measured state, one attested backup and one expiry — and every one of those is a refusal.

What it must never do: make `monolith_barrier` READY, make `compaction_ready` true, authorize a
second run, or authorize an operation nobody implemented. Those are separate assertions here
because they are separate claims.

Three things this suite exists to keep true, each of which was FALSE in an earlier revision and
was caught by review rather than by a test:

* **The exception must actually work.** `prepare_generation_one()` refused a writable monolith
  regardless, so an eligible record authorized nothing. `TestAWritableMonolithNeedsTheCapability`
  drives the real operation against a genuinely writable store.
* **Provisioning is not compaction.** The record names an operation, and the compaction family
  is refused by name — no entry point exists, so nothing would run the post-compaction gates.
* **The measurement must be taken by the code that acts.** There is no `one_shot_observed`
  parameter any more; `TestTheMeasurementIsTakenNotAccepted` changes the store behind a
  self-consistent record and requires a refusal.
"""
from __future__ import annotations

import ast
import datetime
import inspect
import os
import socket
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
for p in (_DEV, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from ops import p5_oneshot_exception as ex  # noqa: E402
from ops import p5_legacy_base as lb  # noqa: E402
from ops import p5_prod_provision as pp  # noqa: E402
from ops import p5_readiness as rd  # noqa: E402
from store import compaction_lock as cl  # noqa: E402
# The production fixture (tempdir data root, anchor domain, sealed monolith, canonical locks,
# faked device numbers) already exists there; reproducing it here would be a second copy to
# keep in step with the operation.
from test_phase2_p5_prod_provision import _ProdBase  # noqa: E402

HOST = "odb24"
RUN_ID = "20260826T140000Z"
MONO = "/home/odbadmin/Data/ghrsst/mur_timecube_s8_t90_sh128.zarr"
META = "a" * 64
DAYS = "b" * 64
SHA = "d" * 40
LEDGER = {"algo": "sha256-path-size-mtime", "ledger_digest": "c" * 64,
          "file_count": 120000, "total_bytes": 2 * 1024 ** 4}


def iso(offset_s=0):
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=offset_s)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def record(**over):
    r = {
        "scope": ex.ONESHOT_SCOPE, "operation": ex.OP_PROVISION,
        "host": HOST, "run_id": RUN_ID,
        "monolith_realpath": MONO,
        "monolith_metadata_fingerprint": META, "monolith_day_digest": DAYS,
        "monolith_content_ledger": dict(LEDGER),
        "backup_id": "vm24-daily-20260826T0300Z", "backup_completed_utc": iso(-7200),
        "backup_verified": True,
        "backup_retention_ref": "retained 30 days; restore runbook OPS-114",
        # Inside the capacity window below, because an exception may not outlive the reading
        # it rests on.
        "expires_utc": iso(3000), "approved_by": "cywhale", "approved_utc": iso(-600),
        "reason": ("one fold wanted before the storage team can move the monolith to its own "
                   "read-only mount; VM-level backups make it recoverable"),
    }
    r.update(over)
    return r


def observed(**over):
    o = {"monolith_realpath": MONO, "monolith_metadata_fingerprint": META,
         "monolith_day_digest": DAYS, "monolith_content_ledger": dict(LEDGER)}
    o.update(over)
    return o


def pre(**over):
    p = {
        "host": HOST, "run_id": RUN_ID, "collected_utc": iso(-60),
        "capacity": {"free_now_gib": 1252.1, "filesystem_dev": 2081, "measured_utc": iso(-60)},
        "healthz": {"status": "ok", "latest": "2026-08-25"},
        "identity": {"reviewed_sha": SHA, "worktree_clean": True, "verified": True},
        "quiescence": {"pm2": "online", "cron": "no append window", "readers": 0,
                       "verified": True},
        "monolith_writers": {"count": 0, "method": "lsof over the store"},
        "locks": {"compaction": "free", "ingest": "free", "verified": True},
        "monolith_state": observed(),
        "backup": {"backup_id": "vm24-daily-20260826T0300Z", "verified": True,
                   "method": "restore-catalogue listing + checksum of the manifest",
                   "verified_utc": iso(-3600)},
        "plan": {"source_map": {"2026-01-01": "delta"}, "destination_manifest": "gen000002",
                 "days": ["2026-01-01"]},
    }
    p.update(over)
    return p


def post(**over):
    p = {
        "manifest": {"generation": 2, "checksum_ok": True},
        "wal_anchor": {"bound": True, "anchor_intact": True},
        "source_map_complete": True,
        "base_delta_disjoint": True,
        "point_series_parity": {"days_checked": 12, "mismatches": 0,
                                "boundaries_included": True},
        "api_parity": {"routes_checked": 6, "mismatches": 0},
        "capacity_after": {"free_now_gib": 1100.4, "measured_utc": iso()},
        "nothing_deleted": True,
    }
    p.update(over)
    return p


class TestTheRecordBindsToOneThing(unittest.TestCase):

    def problem(self, rec=None, obs=None, **kw):
        return ex.exception_problem(record(**(rec or {})), host=kw.pop("host", HOST),
                                    run_id=kw.pop("run_id", RUN_ID),
                                    operation=kw.pop("operation", ex.OP_PROVISION),
                                    observed=observed(**(obs or {})), **kw)

    def test_a_complete_bound_record_is_accepted(self):
        """The positive control: every refusal below must be about its own defect."""
        self.assertIsNone(self.problem())

    def test_a_record_for_another_scope_is_refused(self):
        self.assertIn("scope", self.problem({"scope": "p5_production_provisioning"}))

    def test_every_field_is_required(self):
        for field in ex.EXCEPTION_FIELDS:
            with self.subTest(field):
                rec = record()
                del rec[field]
                problem = ex.exception_problem(rec, host=HOST, run_id=RUN_ID,
                                               operation=ex.OP_PROVISION, observed=observed())
                self.assertTrue(problem)
                self.assertIn(field, problem)

    def test_a_record_that_is_not_a_record_is_refused(self):
        for value in (None, True, "yes", 1, []):
            with self.subTest(repr(value)):
                problem = ex.exception_problem(value, host=HOST, run_id=RUN_ID,
                                               operation=ex.OP_PROVISION, observed=observed())
                self.assertTrue(problem)
                self.assertIn("not a flag", problem)

    def test_a_reason_that_is_not_a_reason_is_refused(self):
        for value in ("ok", "because", "do it"):
            with self.subTest(value):
                self.assertIn("not a reason", self.problem({"reason": value}))

    def test_another_host_is_refused(self):
        self.assertIn("not accepted for another", self.problem(host="somewhere-else"))

    def test_another_run_is_refused(self):
        """"One-shot" is the whole contract: a record that outlives its run is standing
        permission."""
        self.assertIn("ONE run is what one-shot means", self.problem(run_id="a-later-run"))

    def test_a_changed_realpath_is_refused(self):
        self.assertIn("monolith_realpath",
                      self.problem(obs={"monolith_realpath": "/somewhere/else.zarr"}))

    def test_a_changed_metadata_fingerprint_is_refused(self):
        self.assertIn("monolith_metadata_fingerprint",
                      self.problem(obs={"monolith_metadata_fingerprint": "9" * 64}))

    def test_a_changed_day_digest_is_refused(self):
        self.assertIn("monolith_day_digest", self.problem(obs={"monolith_day_digest": "9" * 64}))

    def test_a_changed_content_ledger_is_refused(self):
        problem = self.problem(obs={"monolith_content_ledger": {**LEDGER,
                                                               "ledger_digest": "9" * 64}})
        self.assertIn("content ledger", problem)

    def test_no_fresh_measurement_at_all_is_refused(self):
        for value in (None, {}, "measured"):
            with self.subTest(repr(value)):
                problem = ex.exception_problem(record(), host=HOST, run_id=RUN_ID,
                                               operation=ex.OP_PROVISION, observed=value)
                self.assertIn("verified against itself", problem)

    def test_a_record_for_a_different_store_is_refused_by_the_subject_binding(self):
        problem = self.problem(subject_realpath="/elsewhere/mur.zarr")
        self.assertIn("this run acts on", problem)

    def test_an_unverified_backup_is_refused(self):
        for value in (False, None, "yes", 1):
            with self.subTest(repr(value)):
                self.assertTrue(self.problem({"backup_verified": value}))

    def test_a_backup_completing_after_the_approval_is_refused(self):
        problem = self.problem({"backup_completed_utc": iso(60)})
        self.assertIn("backup completing after the approval", problem)

    def test_an_expired_record_is_refused(self):
        self.assertIn("expired at", self.problem({"expires_utc": iso(-1),
                                                  "approved_utc": iso(-600)}))

    def test_a_record_expiring_before_it_was_approved_is_refused(self):
        problem = self.problem({"approved_utc": iso(-60), "expires_utc": iso(-600)})
        self.assertTrue(problem)


class TestItAuthorizesOneOperationAndCompactionIsNotOne(unittest.TestCase):
    """Provisioning publishes a manifest. It prunes, swaps, deletes and switches nothing.

    An earlier revision called the record a "compaction risk acceptance" and handed it to an
    operation that provisions — so the post-compaction gates were described as protecting a
    lifecycle that no code in this tree runs."""

    def test_only_provisioning_is_implemented(self):
        self.assertEqual(ex.IMPLEMENTED_OPERATIONS, (ex.OP_PROVISION,))

    def test_the_compaction_family_is_refused_by_name(self):
        for op in ex.COMPACTION_FAMILY:
            with self.subTest(op):
                problem = ex.operation_problem(op)
                self.assertIn("NO one-shot entry point", problem)
                self.assertIn("post-compaction gates", problem)

    def test_a_record_naming_compaction_is_refused(self):
        problem = ex.exception_problem(record(operation="compaction"), host=HOST,
                                       run_id=RUN_ID, operation="compaction",
                                       observed=observed())
        self.assertIn("NO one-shot entry point", problem)

    def test_a_record_for_one_operation_does_not_authorize_another(self):
        problem = ex.exception_problem(record(operation=ex.OP_PROVISION), host=HOST,
                                       run_id=RUN_ID, operation="compaction",
                                       observed=observed())
        self.assertTrue(problem)

    def test_an_unimplemented_operation_is_refused(self):
        self.assertIn("nothing implements", ex.operation_problem("make_it_fast"))

    def test_the_verdict_never_authorizes_compaction(self):
        verdict = ex.evaluate(record=record(), host=HOST, run_id=RUN_ID,
                              observed=observed(), pre=pre())
        self.assertFalse(verdict["compaction_authorized"])
        self.assertIn("NO one-shot entry point", verdict["compaction_note"])

    def test_the_provisioner_refuses_a_compaction_record_before_creating_anything(self):
        """Fail-fast: the refusal must land before any root exists, not after."""
        problem = ex.operation_problem("prune")
        self.assertTrue(problem)


class TestTheGatesThatNothingEnforces(unittest.TestCase):
    """A gate that exists as a function and is invoked by nothing constrains nothing."""

    def test_the_enforcement_report_says_no_operation_runs_them(self):
        report = ex.post_compaction_enforcement()
        self.assertEqual(report["verdict"], ex.NOT_ADJUDICATED)
        self.assertIsNone(report["enforced_by"])
        self.assertIn("invoked by nothing", report["why"])

    def test_it_names_what_would_have_to_exist_first(self):
        report = ex.post_compaction_enforcement()
        self.assertIn("prune, swap, delete or switch", report["before_this_can_change"])

    def test_the_verdict_carries_that_report(self):
        verdict = ex.evaluate(record=record(), host=HOST, run_id=RUN_ID,
                              observed=observed(), pre=pre())
        self.assertEqual(verdict["post_compaction_enforcement"]["verdict"], ex.NOT_ADJUDICATED)

    def test_no_operation_in_the_tree_can_prune_swap_delete_or_switch_under_it(self):
        """By AST over the provisioner: it must not call the prune/swap executors at all."""
        tree = ast.parse(inspect.getsource(pp))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = getattr(fn, "attr", None) or getattr(fn, "id", None)
                if name:
                    called.add(name)
        for banned in ("prune_delta", "execute_swap_plan", "rmtree_store", "switch_manifest"):
            with self.subTest(banned):
                self.assertNotIn(banned, called)


class TestThereIsNoGenericBypass(unittest.TestCase):
    """The exception is a capability. Nothing may also accept a switch."""

    def test_the_production_operation_has_no_bypass_parameter(self):
        params = set(inspect.signature(pp.provision_production).parameters)
        for banned in ("force", "skip_seal", "allow_unsealed", "require_sealed",
                       "unsafe", "override"):
            with self.subTest(banned):
                self.assertNotIn(banned, params)

    def test_the_operation_has_no_caller_supplied_measurement_parameter(self):
        """`one_shot_observed` was the hole: evidence assembled by the caller, trusted by the
        operation. It is gone, and its absence is the assertion."""
        params = set(inspect.signature(pp.provision_production).parameters)
        for banned in ("one_shot_observed", "observed", "monolith_state"):
            with self.subTest(banned):
                self.assertNotIn(banned, params)

    def test_the_builder_no_longer_carries_require_sealed_at_all(self):
        """It had no caller that ever passed `False` — a bypass sitting unused with the door
        open. Removing it is strictly better than adding a capability beside it."""
        for fn in (lb.prepare_generation_one, lb.build_generation_one):
            with self.subTest(fn.__name__):
                self.assertNotIn("require_sealed", inspect.signature(fn).parameters)

    def test_the_one_call_builder_does_not_offer_the_unsealed_path(self):
        """It cannot take the ingest lock on the caller's behalf, so offering the unsealed
        path there would offer it without its fence."""
        self.assertNotIn("unsealed_authorization",
                         inspect.signature(lb.build_generation_one).parameters)

    def test_no_bypass_is_read_from_the_environment(self):
        """By AST: the modules name these words in prose to say they are absent, and a
        substring scan cannot tell that sentence from code doing it."""
        for module in (pp, ex, rd, lb):
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    with self.subTest(module=module.__name__, const=node.value[:40]):
                        for banned in ("P5_FORCE", "P5_SKIP_SEAL", "P5_ALLOW_UNSEALED"):
                            self.assertNotIn(banned, node.value)


class TestTheCapabilityCannotBeForged(unittest.TestCase):
    """`UnsealedMonolithAuthorization` is the only thing that opens the unsealed path."""

    def test_it_cannot_be_constructed(self):
        with self.assertRaises(ex.UnsealedMonolithAuthorizationError) as caught:
            ex.UnsealedMonolithAuthorization(
                object(), host=HOST, run_id=RUN_ID, operation=ex.OP_PROVISION,
                realpath=MONO, expires_utc=iso(600), measured=observed(),
                backup_id="b", approved_by="cywhale")
        self.assertIn("cannot be constructed", str(caught.exception))

    def test_a_truthy_value_is_not_a_capability(self):
        """The one place where accepting a duck-typed stand-in would make it a flag."""
        for impostor in (True, 1, "authorized", {"authorized": True}, object()):
            with self.subTest(repr(impostor)[:30]):
                with self.assertRaises(lb.LegacyBaseRefused) as caught:
                    lb.prepare_generation_one(
                        monolith_path=_DEV, manifest_root=_DEV, at_utc=iso(),
                        operator="tests", unsealed_authorization=impostor)
                self.assertIn("not an UnsealedMonolithAuthorization", str(caught.exception))

    def _mint(self):
        return ex.UnsealedMonolithAuthorization(
            ex._MINT, host=HOST, run_id=RUN_ID, operation=ex.OP_PROVISION,
            realpath=os.path.realpath(_DEV), expires_utc=iso(600), measured=observed(),
            backup_id="b", approved_by="cywhale")

    def test_it_cannot_be_serialized(self):
        """One that survives the process that earned it is a standing permission."""
        import copy
        import pickle
        cap = self._mint()
        with self.assertRaises(ex.UnsealedMonolithAuthorizationError):
            pickle.dumps(cap)
        with self.assertRaises(ex.UnsealedMonolithAuthorizationError):
            copy.deepcopy(cap)

    def test_it_is_consumed_by_its_single_use(self):
        cap = self._mint()
        self.assertFalse(cap.consumed)
        cap.consume(operation=ex.OP_PROVISION, subject_realpath=_DEV)
        self.assertTrue(cap.consumed)
        with self.assertRaises(ex.UnsealedMonolithAuthorizationError) as caught:
            cap.consume(operation=ex.OP_PROVISION, subject_realpath=_DEV)
        self.assertIn("already been used", str(caught.exception))

    def test_it_refuses_another_store(self):
        with self.assertRaises(ex.UnsealedMonolithAuthorizationError):
            self._mint().consume(operation=ex.OP_PROVISION, subject_realpath=HERE)

    def test_it_refuses_another_operation(self):
        with self.assertRaises(ex.UnsealedMonolithAuthorizationError):
            self._mint().consume(operation="compaction", subject_realpath=_DEV)

    def test_it_refuses_after_expiry(self):
        cap = ex.UnsealedMonolithAuthorization(
            ex._MINT, host=HOST, run_id=RUN_ID, operation=ex.OP_PROVISION,
            realpath=os.path.realpath(_DEV), expires_utc=iso(-1), measured=observed(),
            backup_id="b", approved_by="cywhale")
        with self.assertRaises(ex.UnsealedMonolithAuthorizationError) as caught:
            cap.consume(operation=ex.OP_PROVISION, subject_realpath=_DEV)
        self.assertIn("expired", str(caught.exception))

    def test_matches_is_how_host_and_run_are_checked(self):
        """`consume()` deliberately does not take them: the spending code does not know the
        run, and a capability asked to vouch for its own run vouches for itself."""
        cap = self._mint()
        self.assertTrue(cap.matches(host=HOST, run_id=RUN_ID))
        self.assertFalse(cap.matches(host="other", run_id=RUN_ID))
        self.assertFalse(cap.matches(host=HOST, run_id="other"))
        self.assertNotIn("run_id", inspect.signature(cap.consume).parameters)


class TestItNeverBecomesReadiness(unittest.TestCase):
    """Eligibility and readiness are different claims, reported in different fields."""

    def test_a_report_can_never_be_an_authorization(self):
        """The assessor reads a JSON document, so every measurement in it is caller-supplied.
        `eligible` must stay false however green the document is."""
        verdict = ex.evaluate(record=record(), host=HOST, run_id=RUN_ID,
                              observed=observed(), pre=pre())
        self.assertFalse(verdict["eligible"])
        self.assertTrue(verdict["eligible_pending_recomputation"])
        self.assertIn("measurement", verdict["problems"])
        self.assertEqual(verdict["measurement_source"], "supplied by the caller")

    def test_a_broken_record_is_not_merely_pending(self):
        verdict = ex.evaluate(record=record(host="elsewhere"), host=HOST, run_id=RUN_ID,
                              observed=observed(), pre=pre())
        self.assertFalse(verdict["eligible_pending_recomputation"])

    def test_the_two_are_separate_fields(self):
        report = rd.assess({"label": "x"})
        self.assertIn("one_shot_compaction_exception", report)
        self.assertIn("compaction_ready", report)
        self.assertFalse(report["one_shot_compaction_exception"]["supplied"])

    def test_an_eligible_exception_does_not_make_the_deployment_ready(self):
        report = rd.assess({"label": "x", "host": HOST, "run_id": RUN_ID,
                            "one_shot_exception": record(),
                            "monolith_state": observed(), "pre_compaction": pre()})
        self.assertFalse(report["compaction_ready"])

    def test_an_eligible_exception_leaves_monolith_barrier_unchanged(self):
        report = rd.assess({"label": "x", "host": HOST, "run_id": RUN_ID,
                            "one_shot_exception": record(),
                            "monolith_state": observed(), "pre_compaction": pre()})
        barrier = next(r for r in report["requirements"] if r["id"] == "monolith_barrier")
        self.assertIn(barrier["verdict"], (rd.BLOCKED, rd.NOT_ADJUDICATED))
        self.assertEqual(report["one_shot_compaction_exception"]["monolith_barrier_verdict"],
                         barrier["verdict"])

    def test_absent_is_the_normal_state_and_not_a_failure(self):
        verdict = rd.assess({"label": "x"})["one_shot_compaction_exception"]
        self.assertIn("normal state", verdict["note"])


class TestThePreCompactionContract(unittest.TestCase):
    """Field presence was the old bar and it was too low."""

    def problem(self, **over):
        return ex.pre_compaction_problem(pre(**over), record=record(), host=HOST,
                                         run_id=RUN_ID)

    def test_complete_evidence_passes(self):
        self.assertIsNone(self.problem())

    def test_every_field_is_required(self):
        for field in ex.PRE_COMPACTION_FIELDS:
            with self.subTest(field):
                evidence = pre()
                del evidence[field]
                problem = ex.pre_compaction_problem(evidence, record=record(), host=HOST,
                                                    run_id=RUN_ID)
                self.assertTrue(problem)
                self.assertIn(field, problem)

    def test_preflight_from_another_host_or_run_is_refused(self):
        self.assertIn("another machine", self.problem(host="elsewhere"))
        self.assertIn("another run", self.problem(run_id="other"))

    def test_stale_preflight_is_refused(self):
        self.assertIn("past the", self.problem(collected_utc=iso(-3 * 3600)))

    def test_preflight_from_the_future_is_refused(self):
        self.assertIn("future", self.problem(collected_utc=iso(3600)))

    # ---------------------------------------------------------------- capacity
    def test_a_stale_capacity_reading_is_refused(self):
        problem = self.problem(capacity={"free_now_gib": 1252.1, "filesystem_dev": 2081,
                                         "measured_utc": iso(-3 * 3600)})
        self.assertIn("past the", problem)

    def test_the_freshness_window_is_the_canonical_one(self):
        """Not a copy. A second constant with the same name is a second policy."""
        max_age, _ = ex._policy()
        self.assertIs(max_age, rd.MAX_CAPACITY_AGE_S)

    def test_a_capacity_reading_from_another_device_is_refused(self):
        problem = ex.pre_compaction_problem(pre(), record=record(), host=HOST, run_id=RUN_ID,
                                            expected_dev=999)
        self.assertIn("another volume", problem)

    def test_a_bool_free_space_is_not_a_number(self):
        """`True` is an `int` and would compare against a floor without complaint."""
        problem = self.problem(capacity={"free_now_gib": True, "filesystem_dev": 2081,
                                         "measured_utc": iso(-60)})
        self.assertIn("not a number", problem)

    def test_a_bool_device_number_is_not_a_device(self):
        problem = self.problem(capacity={"free_now_gib": 1.0, "filesystem_dev": True,
                                         "measured_utc": iso(-60)})
        self.assertIn("not a device number", problem)

    def test_the_exception_may_not_outlive_the_capacity_reading(self):
        """Six hours of exception over two hours of measurement authorizes the last four on
        nothing."""
        problem = ex.pre_compaction_problem(
            pre(), record=record(expires_utc=iso(6 * 3600)), host=HOST, run_id=RUN_ID)
        self.assertIn("stops being current", problem)

    # ---------------------------------------------------------------- validated results
    def test_healthz_must_say_the_service_is_serving(self):
        self.assertIn("not serving normally",
                      self.problem(healthz={"status": "degraded", "latest": "2026-08-25"}))

    def test_healthz_latest_must_be_a_day(self):
        self.assertIn("not a day",
                      self.problem(healthz={"status": "ok", "latest": "recently"}))

    def test_an_unvalidated_block_is_a_note_not_a_result(self):
        for field, block in (("identity", {"reviewed_sha": SHA, "worktree_clean": True}),
                             ("locks", {"compaction": "free", "ingest": "free"}),
                             ("quiescence", {"pm2": "online", "cron": "-", "readers": 0})):
            with self.subTest(field):
                problem = self.problem(**{field: block})
                self.assertIn("not a result", problem)

    def test_a_dirty_worktree_is_refused(self):
        problem = self.problem(identity={"reviewed_sha": SHA, "worktree_clean": False,
                                         "verified": True})
        self.assertIn("not the commit that was reviewed", problem)

    def test_a_reviewed_sha_that_is_not_a_commit_is_refused(self):
        problem = self.problem(identity={"reviewed_sha": "HEAD", "worktree_clean": True,
                                         "verified": True})
        self.assertIn("not a commit id", problem)

    def test_a_held_lock_is_refused(self):
        for name in ("compaction", "ingest"):
            with self.subTest(name):
                locks = {"compaction": "free", "ingest": "free", "verified": True}
                locks[name] = "held"
                self.assertIn(name, self.problem(locks=locks))

    def test_a_process_writing_the_monolith_refuses(self):
        for value in (1, 7):
            with self.subTest(value):
                problem = self.problem(monolith_writers={"count": value, "method": "lsof"})
                self.assertIn("the hazard, not the risk", problem)

    def test_a_writer_count_from_an_unnamed_method_is_refused(self):
        self.assertIn("nobody can reproduce", self.problem(monolith_writers={"count": 0}))

    def test_a_bool_writer_count_is_not_a_count(self):
        problem = self.problem(monolith_writers={"count": False, "method": "lsof"})
        self.assertIn("not a count", problem)

    # ---------------------------------------------------------------- backup provenance
    def test_a_backup_that_is_not_the_records_backup_is_refused(self):
        problem = self.problem(backup={"backup_id": "some-other-backup", "verified": True,
                                       "method": "catalogue", "verified_utc": iso(-3600)})
        self.assertIn("different backup", problem)

    def test_a_backup_verified_before_it_finished_is_refused(self):
        problem = ex.pre_compaction_problem(
            pre(backup={"backup_id": "vm24-daily-20260826T0300Z", "verified": True,
                        "method": "catalogue", "verified_utc": iso(-9000)}),
            record=record(), host=HOST, run_id=RUN_ID)
        self.assertIn("before it finished", problem)

    def test_a_backup_with_no_named_method_is_refused(self):
        problem = self.problem(backup={"backup_id": "vm24-daily-20260826T0300Z",
                                       "verified": True, "method": "  ",
                                       "verified_utc": iso(-3600)})
        self.assertTrue(problem)

    def test_the_preflight_monolith_state_must_match_the_record(self):
        problem = self.problem(monolith_state=observed(monolith_day_digest="9" * 64))
        self.assertIn("monolith_day_digest", problem)

    def test_a_plan_without_a_source_map_refuses(self):
        for field in ("source_map", "destination_manifest", "days"):
            with self.subTest(field):
                plan = {k: v for k, v in pre()["plan"].items() if k != field}
                self.assertIn(field, self.problem(plan=plan))

    def test_absent_pre_evidence_makes_the_exception_ineligible(self):
        verdict = ex.evaluate(record=record(), host=HOST, run_id=RUN_ID,
                              observed=observed(), pre=None)
        self.assertIn("pre_compaction", verdict["problems"])


class TestTheBackupIsAttestedNotProven(unittest.TestCase):
    """`backup_verified: true` is a person's word, and is reported as such."""

    def test_it_is_always_not_adjudicated(self):
        verdict = ex.backup_verification(record(), pre())
        self.assertEqual(verdict["verdict"], ex.NOT_ADJUDICATED)
        self.assertTrue(verdict["attested"])

    def test_it_says_why_no_local_check_can_settle_it(self):
        why = ex.backup_verification(record(), pre())["why"]
        self.assertIn("hypervisor's backup catalogue", why)
        self.assertIn("not proof", why)

    def test_it_states_the_consequence_of_the_attestation_being_wrong(self):
        verdict = ex.backup_verification(record(), pre())
        self.assertIn("recoverability the whole exception rests on is not there",
                      verdict["consequence"])

    def test_the_verdict_carries_it(self):
        verdict = ex.evaluate(record=record(), host=HOST, run_id=RUN_ID,
                              observed=observed(), pre=pre())
        self.assertEqual(verdict["backup_verification"]["verdict"], ex.NOT_ADJUDICATED)


class TestThePostCompactionGates(unittest.TestCase):

    def test_complete_gates_pass(self):
        self.assertIsNone(ex.post_compaction_problem(post()))

    def test_every_gate_is_required(self):
        for field in ex.POST_COMPACTION_FIELDS:
            with self.subTest(field):
                evidence = post()
                del evidence[field]
                problem = ex.post_compaction_problem(evidence)
                self.assertTrue(problem)
                self.assertIn(field, problem)

    def test_deleting_anything_before_the_gates_pass_refuses(self):
        """`None` is caught by the "an unanswered check is not a passing one" rule, which is a
        different (and correct) refusal, so it is asserted as a refusal rather than as this
        message."""
        self.assertTrue(ex.post_compaction_problem(post(nothing_deleted=None)))
        for value in (False, "later", 0, 1):
            with self.subTest(repr(value)):
                problem = ex.post_compaction_problem(post(nothing_deleted=value))
                self.assertIn("may be removed until every gate", problem)

    def test_any_point_series_mismatch_refuses(self):
        problem = ex.post_compaction_problem(post(point_series_parity={
            "days_checked": 12, "mismatches": 1, "boundaries_included": True}))
        self.assertIn("mismatch", problem)

    def test_parity_that_skipped_the_boundaries_refuses(self):
        problem = ex.post_compaction_problem(post(point_series_parity={
            "days_checked": 12, "mismatches": 0, "boundaries_included": False}))
        self.assertIn("first and last days", problem)

    def test_parity_that_checked_nothing_refuses(self):
        for value in (0, -1, True):
            with self.subTest(repr(value)):
                problem = ex.post_compaction_problem(post(point_series_parity={
                    "days_checked": value, "mismatches": 0, "boundaries_included": True}))
                self.assertIn("checks nothing", problem)

    def test_an_api_parity_mismatch_refuses(self):
        problem = ex.post_compaction_problem(post(api_parity={"routes_checked": 6,
                                                              "mismatches": 2}))
        self.assertIn("API response mismatch", problem)

    def test_a_base_and_delta_that_overlap_refuse(self):
        self.assertIn("claimed by both",
                      ex.post_compaction_problem(post(base_delta_disjoint=False)))

    def test_an_incomplete_source_map_refuses(self):
        self.assertIn("unaccounted for",
                      ex.post_compaction_problem(post(source_map_complete=False)))


class TestTheRollbackLimitsAreStated(unittest.TestCase):
    """The acceptance rests on the backup, so what the backup cannot undo is part of it."""

    def test_the_limits_name_what_a_restore_costs(self):
        limits = " ".join(ex.rollback_limits())
        for phrase in ("WHOLE machine", "Delta days appended", "repair committed to the WAL",
                       "cannot be recalled", "decided and started by a person"):
            with self.subTest(phrase):
                self.assertIn(phrase, limits)

    def test_the_module_does_not_treat_the_backup_as_lowering_the_risk(self):
        doc = " ".join((inspect.getdoc(ex) or "").split())
        self.assertIn("makes the failure recoverable, not that it makes it unlikely", doc)
        self.assertIn("The stronger route is still the preferred one", doc)
        self.assertIn("nothing here treats its existence as a reason to relax any other check",
                      doc)


# =====================================================================================
#  Against the real operation, on a real store.
# =====================================================================================
class _OneShotBase(_ProdBase):
    """The production fixture, plus a record bound to what the fixture actually is."""

    def unseal(self):
        """Make the monolith genuinely writable — the state the exception exists for."""
        os.chmod(self.monolith, 0o755)

    def measured(self):
        return ex.measure_monolith(self.monolith)

    def mutate_store(self):
        """Write into the monolith without changing its day set or array metadata.

        The subtlest change there is: a value rewritten in place. The metadata fingerprint and
        the day digest are both blind to it, so only the content ledger sees it — which is the
        whole reason the ledger exists.

        It must be a CHUNK, not `zarr.json`. Appending to the metadata document produces a
        store that cannot be opened at all, which is a different failure and would let this
        test pass on an exception raised by the JSON parser rather than by the ledger."""
        metadata = {"zarr.json", ".zarray", ".zgroup", ".zattrs", ".zmetadata"}
        for dirpath, _dirs, files in os.walk(self.monolith):
            for name in sorted(files):
                if name in metadata or name.startswith("."):
                    continue
                victim = os.path.join(dirpath, name)
                with open(victim, "ab") as fh:
                    fh.write(b"\0")
                return victim
        raise AssertionError("no chunk file found in the monolith to mutate")

    def bound_record(self, **over):
        m = self.measured()
        rec = {
            "scope": ex.ONESHOT_SCOPE, "operation": ex.OP_PROVISION,
            "host": socket.gethostname(), "run_id": self.RUN_ID,
            "monolith_realpath": m["monolith_realpath"],
            "monolith_metadata_fingerprint": m["monolith_metadata_fingerprint"],
            "monolith_day_digest": m["monolith_day_digest"],
            "monolith_content_ledger": dict(m["monolith_content_ledger"]),
            "backup_id": "vm24-daily-20260826T0300Z",
            "backup_completed_utc": iso(-7200), "backup_verified": True,
            "backup_retention_ref": "retained 30 days; restore runbook OPS-114",
            "expires_utc": iso(3000), "approved_by": "cywhale", "approved_utc": iso(-600),
            "reason": ("one generation-1 publish wanted before the storage team can move the "
                       "monolith to its own read-only mount; VM backups make it recoverable"),
        }
        rec.update(over)
        return rec

    def bound_pre(self, **over):
        m = self.measured()
        evidence = {
            "host": socket.gethostname(), "run_id": self.RUN_ID, "collected_utc": iso(-60),
            "capacity": {"free_now_gib": 1252.1,
                         "filesystem_dev": os.stat(self.live).st_dev,
                         "measured_utc": iso(-60)},
            "healthz": {"status": "ok", "latest": "2026-01-06"},
            "identity": {"reviewed_sha": SHA, "worktree_clean": True, "verified": True},
            "quiescence": {"pm2": "online", "cron": "no append window", "readers": 0,
                           "verified": True},
            "monolith_writers": {"count": 0, "method": "lsof over the store"},
            "locks": {"compaction": "free", "ingest": "free", "verified": True},
            "monolith_state": {k: m[k] for k in ex.MONOLITH_STATE_FIELDS},
            "backup": {"backup_id": "vm24-daily-20260826T0300Z", "verified": True,
                       "method": "restore-catalogue listing", "verified_utc": iso(-3600)},
            "plan": {"source_map": {"legacy": "monolith"},
                     "destination_manifest": "gen000001", "days": list(self.mono_days)},
        }
        evidence.update(over)
        return evidence

    def oneshot(self, **over):
        """Provision under a bound exception, defaulting only what was not supplied.

        Built LAZILY on purpose. `bound_record()` and `bound_pre()` measure the store, so
        evaluating them eagerly would have the fixture call `measure_monolith` even when the
        caller supplied both — which silently broke a test that spies on that call."""
        kw = dict(over)
        if "one_shot_exception" not in kw:
            kw["one_shot_exception"] = self.bound_record()
        if "one_shot_pre_compaction" not in kw:
            kw["one_shot_pre_compaction"] = self.bound_pre()
        return self.provision(**kw)


class TestAWritableMonolithNeedsTheCapability(_OneShotBase):
    """The blocker that made the whole exception decorative.

    `prepare_generation_one()` called `check_monolith_sealed()` unconditionally, so a writable
    monolith was refused no matter how good the record was. Every test in this class would have
    failed against that revision — and none of the record-level tests above would have."""

    def test_a_writable_monolith_is_refused_with_no_exception(self):
        """The normal path, unchanged: no record, no publish over a writable store."""
        self.unseal()
        with self.assertRaises(lb.LegacyBaseRefused) as caught:
            self.provision()
        self.assertIn("is writable", str(caught.exception))
        for root in self.roots():
            self.assertFalse(os.path.exists(root))

    def test_an_eligible_bound_exception_reaches_the_operation(self):
        """The positive control the earlier revision could not have passed."""
        self.unseal()
        result = self.oneshot()
        self.assertTrue(result["one_shot_exception"]["eligible"])
        self.assertTrue(result["manifest_published"])
        self.assertEqual(result["generation"], 1)
        for root in self.roots():
            self.assertTrue(os.path.isdir(root))

    def test_the_capability_is_consumed_by_the_run(self):
        self.unseal()
        self.assertTrue(self.oneshot()["one_shot_exception"]["capability_consumed"])

    def test_an_invalid_exception_still_refuses_a_writable_monolith(self):
        self.unseal()
        with self.assertRaises((pp.ProductionProvisionRefused, ex.OneShotExceptionRefused)):
            self.oneshot(one_shot_exception=self.bound_record(backup_verified=False))
        for root in self.roots():
            self.assertFalse(os.path.exists(root))

    def test_an_exception_for_another_run_refuses(self):
        self.unseal()
        with self.assertRaises((pp.ProductionProvisionRefused, ex.OneShotExceptionRefused)):
            self.oneshot(one_shot_exception=self.bound_record(run_id="some-other-run"))

    def test_an_expired_exception_refuses(self):
        self.unseal()
        with self.assertRaises((pp.ProductionProvisionRefused, ex.OneShotExceptionRefused)):
            self.oneshot(one_shot_exception=self.bound_record(expires_utc=iso(-1)))

    def test_an_exception_naming_compaction_refuses_before_anything_is_created(self):
        self.unseal()
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.oneshot(one_shot_exception=self.bound_record(operation="compaction"))
        self.assertIn("NO one-shot entry point", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(self.live, pp.marker_name(self.RUN_ID))))

    def test_missing_pre_compaction_evidence_refuses(self):
        self.unseal()
        with self.assertRaises((pp.ProductionProvisionRefused, ex.OneShotExceptionRefused)):
            self.oneshot(one_shot_pre_compaction=None)

    def test_stale_capacity_evidence_refuses(self):
        self.unseal()
        stale = self.bound_pre()
        stale["capacity"]["measured_utc"] = iso(-3 * 3600)
        with self.assertRaises((pp.ProductionProvisionRefused, ex.OneShotExceptionRefused)):
            self.oneshot(one_shot_pre_compaction=stale)

    def test_capacity_evidence_from_another_device_refuses(self):
        """The operation binds `expected_dev` from the live root it is about to write into."""
        self.unseal()
        wrong = self.bound_pre()
        wrong["capacity"]["filesystem_dev"] = os.stat(self.live).st_dev + 7777
        with self.assertRaises((pp.ProductionProvisionRefused, ex.OneShotExceptionRefused)):
            self.oneshot(one_shot_pre_compaction=wrong)

    def test_a_mismatched_backup_refuses(self):
        self.unseal()
        wrong = self.bound_pre()
        wrong["backup"]["backup_id"] = "a-different-backup"
        with self.assertRaises((pp.ProductionProvisionRefused, ex.OneShotExceptionRefused)):
            self.oneshot(one_shot_pre_compaction=wrong)

    def test_a_writer_holding_the_monolith_refuses(self):
        self.unseal()
        busy = self.bound_pre()
        busy["monolith_writers"] = {"count": 1, "method": "lsof over the store"}
        with self.assertRaises((pp.ProductionProvisionRefused, ex.OneShotExceptionRefused)):
            self.oneshot(one_shot_pre_compaction=busy)


class TestTheMeasurementIsTakenNotAccepted(_OneShotBase):
    """Blocker 3: the operation measures the store itself, at the point it acts.

    The record and the preflight can be made to agree with each other by anyone assembling
    both. Only a measurement taken by the operation can disagree with them."""

    def test_the_authorizer_takes_no_measurement_argument(self):
        params = set(inspect.signature(ex.authorize_unsealed).parameters)
        for banned in ("observed", "measured", "monolith_state"):
            with self.subTest(banned):
                self.assertNotIn(banned, params)

    def test_a_store_that_changed_under_a_self_consistent_record_refuses(self):
        """Record and preflight agree perfectly — and describe the store as it WAS."""
        self.unseal()
        stale_record = self.bound_record()
        stale_pre = self.bound_pre()
        # Now change the store. Both documents still agree with each other, and both are now
        # wrong; nothing inside the pair can tell.
        self.mutate_store()
        with self.assertRaises((pp.ProductionProvisionRefused, ex.OneShotExceptionRefused)) \
                as caught:
            self.oneshot(one_shot_exception=stale_record, one_shot_pre_compaction=stale_pre)
        self.assertIn("content ledger", str(caught.exception))
        for root in self.roots():
            self.assertFalse(os.path.exists(root))

    def test_a_preflight_disagreeing_with_the_measurement_refuses(self):
        self.unseal()
        drifted = self.bound_pre()
        drifted["monolith_state"]["monolith_day_digest"] = "9" * 64
        with self.assertRaises((pp.ProductionProvisionRefused, ex.OneShotExceptionRefused)):
            self.oneshot(one_shot_pre_compaction=drifted)

    def test_the_verdict_records_where_the_measurement_came_from(self):
        self.unseal()
        verdict = self.oneshot()["one_shot_exception"]
        self.assertEqual(verdict["measurement_source"], "recomputed at the execution point")

    def test_a_value_rewritten_between_prepare_and_publish_refuses(self):
        """The ledger re-verification. Metadata and the day set are unchanged by an in-place
        rewrite, so only the content ledger sees it."""
        self.unseal()
        real_publish = lb.publish_prepared
        touched = {}

        def rewrite_then_publish(prepared):
            if not touched:
                touched["yes"] = True
                self.mutate_store()          # same days, same metadata, different bytes
            return real_publish(prepared)

        with mock.patch.object(lb, "publish_prepared", side_effect=rewrite_then_publish):
            with self.assertRaises(lb.LegacyBaseRefused) as caught:
                self.oneshot()
        self.assertIn("changed between preparation and publish", str(caught.exception))


class TestTheFenceCoversScanToPublish(_OneShotBase):
    """Blocker 4: a one-time writer count is not a fence.

    The monolith is writable, so the expensive scan is exposed for its whole duration. The
    ingest lock is therefore taken BEFORE the prepare and held through the publish. These
    tests record the actual event order rather than reading the comment that claims it."""

    def _competing_writer(self):
        """Can an ingest-shaped writer take the canonical ingest lock right now?"""
        try:
            lock = cl.CompactionLock(self.ilock, holder="competing-ingest")
            lock.acquire()
        except cl.CompactionLockBusy:
            return "blocked"
        lock.release()
        return "entered"

    def _trace(self, **over):
        """Run a provision, probing the ingest lock at the scan and at the publish."""
        events = []
        real_prepare, real_publish = lb.prepare_generation_one, lb.publish_prepared

        def spy_prepare(**kw):
            events.append(("during_scan", self._competing_writer()))
            return real_prepare(**kw)

        def spy_publish(prepared):
            events.append(("during_publish", self._competing_writer()))
            return real_publish(prepared)

        with mock.patch.object(lb, "prepare_generation_one", side_effect=spy_prepare), \
                mock.patch.object(lb, "publish_prepared", side_effect=spy_publish):
            self.provision(**over)
        return dict(events)

    def test_a_writer_cannot_enter_during_the_scan_or_the_publish(self):
        self.unseal()
        seen = self._trace(one_shot_exception=self.bound_record(),
                           one_shot_pre_compaction=self.bound_pre())
        self.assertEqual(seen["during_scan"], "blocked",
                         "the expensive scan ran outside the ingest fence on a WRITABLE store")
        self.assertEqual(seen["during_publish"], "blocked")

    def test_the_sealed_path_still_fences_only_the_publish(self):
        """Unchanged, and deliberately so: a sealed store cannot move during the scan, and
        blocking the daily-append cron behind a minutes-long walk is the defect that was fixed
        two rounds ago. The two paths differ because the stores differ."""
        seen = self._trace()
        self.assertEqual(seen["during_scan"], "entered")
        self.assertEqual(seen["during_publish"], "blocked")

    def test_the_run_reports_which_fence_it_used(self):
        self.unseal()
        result = self.oneshot()
        self.assertIn("from BEFORE the expensive prepare THROUGH the publish",
                      result["locks"]["order"])

    def test_the_sealed_run_reports_the_narrow_fence(self):
        result = self.provision()
        self.assertIn("PUBLISH ONLY", result["locks"]["order"])

    def test_the_compaction_lock_stays_outermost_on_both_paths(self):
        for unsealed in (False, True):
            with self.subTest(unsealed=unsealed):
                self.setUp()
                if unsealed:
                    self.unseal()
                    result = self.oneshot()
                else:
                    result = self.provision()
                self.assertIn("compaction lock outermost", result["locks"]["order"])

    def test_losing_the_compaction_lock_refuses_before_the_expensive_scan(self):
        """The re-assertion INSIDE the fence, and what only it provides.

        A second `assert_still_held()` already guards the publish, so losing the compaction
        lock could never publish regardless. What the earlier one adds is that the refusal
        lands *before* the multi-minute ledger walk rather than after it — so the assertion
        this test pins is "the store was not scanned", not "the run failed". Without it the run
        fails too, just several minutes later, and a test that only asserted the raise would
        have passed on either."""
        self.unseal()
        # Built BEFORE the spy is installed: the fixture helpers measure the store themselves,
        # so constructing them inside the patch would make the spy report a call this test
        # made rather than one the operation made.
        rec, evidence = self.bound_record(), self.bound_pre()
        real_acquire = cl.CompactionLock.acquire

        def usurp_on_ingest(lock_self):
            out = real_acquire(lock_self)
            if os.path.realpath(lock_self.path) == os.path.realpath(self.ilock):
                self._usurp_compaction_lock()
            return out

        with mock.patch.object(cl.CompactionLock, "acquire", usurp_on_ingest), \
                mock.patch.object(ex, "measure_monolith",
                                  wraps=ex.measure_monolith) as measured:
            with self.assertRaises(cl.CompactionLockError):
                self.oneshot(one_shot_exception=rec, one_shot_pre_compaction=evidence)
        self.assertFalse(measured.called,
                         "the monolith was scanned after the compaction lock had been lost; "
                         "the re-assertion inside the fence did not run before the scan")

    def _usurp_compaction_lock(self):
        """Replace the compaction lock file while this run holds it.

        The one two-writer state reachable here: another process can take a lock on the fresh
        inode while this run holds one on the orphaned old inode."""
        spare = self.clock + ".usurped"
        with open(spare, "w"):
            pass
        os.replace(spare, self.clock)

    def test_losing_the_compaction_lock_after_the_scan_refuses_to_publish(self):
        """The re-assertion between the scan and the publish.

        The earlier one cannot cover this window, so tripping the lock here is what shows the
        second assertion does anything.

        The assertion pinned is that `publish_prepared` was never CALLED. "No manifest on disk
        afterwards" looked like the stronger check and is worthless here: the failure path
        rolls the roots back, so the manifest is gone either way — a version with the guard
        deleted passed that assertion happily."""
        self.unseal()
        rec, evidence = self.bound_record(), self.bound_pre()
        real_prepare, real_publish = lb.prepare_generation_one, lb.publish_prepared

        def prepare_then_usurp(**kw):
            prepared = real_prepare(**kw)
            self._usurp_compaction_lock()
            return prepared

        with mock.patch.object(lb, "prepare_generation_one", side_effect=prepare_then_usurp), \
                mock.patch.object(lb, "publish_prepared",
                                  side_effect=real_publish) as published:
            with self.assertRaises(cl.CompactionLockError):
                self.oneshot(one_shot_exception=rec, one_shot_pre_compaction=evidence)
        self.assertFalse(published.called,
                         "the authority was changed after the compaction lock had been lost")

    def test_a_held_ingest_lock_stops_the_run_rather_than_waiting(self):
        """Non-blocking by design: the operation refuses rather than queueing behind ingest."""
        self.unseal()
        holder = cl.CompactionLock(self.ilock, holder="ingest-cron")
        holder.acquire()
        self.addCleanup(holder.release)
        with self.assertRaises(cl.CompactionLockBusy):
            self.oneshot()


class TestTheSealedPathIsUnchanged(_OneShotBase):
    """Everything above must leave the normal route exactly as it was."""

    def test_sealed_provisioning_still_works_without_any_record(self):
        result = self.provision()
        self.assertTrue(result["manifest_published"])
        self.assertIsNone(result["one_shot_exception"])

    def test_the_sealed_run_records_seal_evidence_and_no_authorization(self):
        prepared = lb.prepare_generation_one(
            monolith_path=self.monolith,
            manifest_root=self._fresh_manifest_root(),
            at_utc="2026-08-25T12:00:00Z", operator="tests")
        self.assertIsNotNone(prepared["seal_evidence"])
        self.assertIsNone(prepared["unsealed_authorization"])

    def test_a_prepared_generation_with_neither_is_refused(self):
        """Belt and braces: a record carrying neither describes a store nothing checked."""
        prepared = lb.prepare_generation_one(
            monolith_path=self.monolith,
            manifest_root=self._fresh_manifest_root(),
            at_utc="2026-08-25T12:00:00Z", operator="tests")
        prepared["seal_evidence"] = None
        with self.assertRaises(lb.LegacyBaseRefused) as caught:
            lb.validate_prepared(prepared)
        self.assertIn("nothing checked", str(caught.exception))

    def _fresh_manifest_root(self):
        root = os.path.join(self.tmp, "manifest_root_for_prepare")
        if not os.path.isdir(root):
            os.makedirs(root)
        return root


class TestTheRunbookRecordsTheExceptionPath(unittest.TestCase):
    """The operator reads the runbook, not the module docstrings."""

    def _flat(self):
        path = os.path.join(_DEV, "specs", "p5_monolith_mount_barrier_requirement.md")
        with open(path) as fh:
            return " ".join(fh.read().split())

    def test_it_says_the_backup_is_about_recoverability_not_likelihood(self):
        flat = self._flat()
        self.assertIn("claim about **recoverability**, not about likelihood", flat)
        self.assertIn("does not weaken anything", flat)

    def test_it_says_there_is_no_flag(self):
        flat = self._flat()
        self.assertIn("a **record**, not a flag", flat)
        for banned in ("`force`", "`skip_seal`", "`allow_unsealed`", "`require_sealed=False`"):
            with self.subTest(banned):
                self.assertIn(f"no {banned}", flat)

    def test_it_names_everything_the_record_binds_to(self):
        flat = self._flat()
        for field in ex.EXCEPTION_FIELDS:
            with self.subTest(field):
                self.assertIn(field, flat)

    def test_it_says_the_exception_never_becomes_readiness(self):
        flat = self._flat()
        self.assertIn("It never becomes readiness", flat)
        self.assertIn("`compaction_ready` stays `false`", flat)
        self.assertIn("expected shape, not a contradiction", flat)

    def test_it_says_a_self_consistent_pair_is_not_enough(self):
        flat = self._flat()
        self.assertIn("Nor is a self-consistent pair enough", flat)
        self.assertIn("the **operation** measures the store itself", flat)

    def test_it_says_provisioning_is_not_compaction(self):
        flat = self._flat()
        self.assertIn("authorizes provisioning, not compaction", flat)
        self.assertIn("prunes, swaps, deletes and switches nothing", flat)

    def test_it_records_that_no_operation_enforces_the_post_gates(self):
        flat = self._flat()
        self.assertIn("no operation enforces them", flat)

    def test_it_describes_the_widened_fence(self):
        flat = self._flat()
        self.assertIn("ingest lock is taken **before** the expensive scan", flat)
        self.assertIn("A one-time writer count is not a fence", flat)

    def test_it_says_the_backup_attestation_is_not_adjudicated(self):
        flat = self._flat()
        self.assertIn("NOT_ADJUDICATED", flat)

    ROLLBACK_PHRASES = (
        "whole machine",
        "delta days appended between the backup and the restore are lost",
        "repair committed to the wal in that interval",
        "answered from a bad generation cannot be recalled",
        "decided and started by a person",
    )

    def test_it_lists_the_pre_and_post_checks(self):
        flat = self._flat()
        for phrase in ("no process holds the monolith for writing",
                       "source-map completeness", "point-series parity",
                       "before anything is deleted or any service is switched"):
            with self.subTest(phrase):
                self.assertIn(phrase, flat)

    def test_it_records_the_rollback_limits_the_module_states(self):
        """Document and code, tied to each other rather than written twice."""
        flat = self._flat().lower()
        self.assertEqual(len(ex.rollback_limits()), len(self.ROLLBACK_PHRASES),
                         "a rollback limit was added or removed in the module and this list "
                         "did not move with it")
        for phrase in self.ROLLBACK_PHRASES:
            with self.subTest(phrase):
                self.assertIn(phrase, flat)
                self.assertTrue(any(phrase.split()[0] in limit.lower()
                                    for limit in ex.rollback_limits()),
                                "the runbook states a limit the module does not")


class TestTheRoadmapShowsTheExceptionPath(unittest.TestCase):
    """The critical path is where someone looks to see whether a step can be skipped."""

    def _flat(self):
        with open(os.path.join(_DEV, "specs", "p5_deployment_readiness.md")) as fh:
            return " ".join(fh.read().split())

    def test_the_roadmap_scopes_the_detour_to_the_mount_step_alone(self):
        flat = self._flat()
        self.assertIn("a detour around step 1, and only step 1", flat)
        self.assertIn("Steps 2–4 and 6 are untouched", flat)

    def test_the_roadmap_says_it_is_a_record_not_a_flag(self):
        flat = self._flat()
        self.assertIn("It is a **record**, not a flag", flat)
        self.assertIn("one host, one run, one operation, one monolith at one measured state",
                      flat)

    def test_the_roadmap_says_it_never_becomes_readiness(self):
        flat = self._flat()
        self.assertIn("never makes `monolith_barrier` READY and never makes `compaction_ready` "
                      "true", flat)

    def test_the_roadmap_states_the_rollback_limits(self):
        flat = self._flat()
        self.assertIn("recoverable**, not unlikely", flat)
        self.assertIn("rolls back the whole machine", flat)
        self.assertIn("loses delta days and WAL repairs", flat)
        self.assertIn("cannot recall answers already served", flat)

    def test_the_roadmap_keeps_the_mount_as_the_preferred_route(self):
        flat = self._flat()
        self.assertIn("**The read-only mount remains the preferred route**", flat)
        self.assertIn("step 1 stays on the critical path for every run after this one", flat)

    def test_the_roadmap_says_step_7_is_not_reached_by_the_exception(self):
        """It buys step 5, not step 7. The compaction entry point does not exist."""
        flat = self._flat()
        self.assertIn("does not reach step 7", flat)
