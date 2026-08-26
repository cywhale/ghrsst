"""dev2026 — the one-shot compaction risk exception.

A human accepted the risk of running ONE compaction without the read-only mount barrier,
because VM24 has full automated VM backups. That is a claim about recoverability, not about
likelihood, and this suite holds the code to treating it as the former.

What the exception must never become: a flag. `force`, `skip_seal`, `allow_unsealed`,
`require_sealed=False` and an environment variable are all bypasses whose blast radius is
"whatever the caller felt like", reusable by the next caller who finds one, and unauditable
afterwards. So the exception is a record bound to one host, one run, one monolith at one
measured state, one verified backup and one expiry — and every one of those is a refusal.

And what it must never do: make `monolith_barrier` READY, make `compaction_ready` true, or
authorize a second run. Those are separate assertions here because they are separate claims,
and the whole reason the exception is reported in its own field is that folding it into
readiness would lose the distinction.
"""
from __future__ import annotations

import ast
import datetime
import inspect
import os
import socket
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
for p in (_DEV, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from ops import p5_oneshot_exception as ex  # noqa: E402
from ops import p5_prod_provision as pp  # noqa: E402
from ops import p5_readiness as rd  # noqa: E402
# The production fixture (tempdir data root, anchor domain, sealed monolith, canonical locks,
# faked device numbers) already exists there; reproducing it here would be a second copy to
# keep in step with the operation.
from test_phase2_p5_prod_provision import _ProdBase  # noqa: E402

HOST = "odb24"
RUN_ID = "20260826T140000Z"
MONO = "/home/odbadmin/Data/ghrsst/mur_timecube_s8_t90_sh128.zarr"
META = "a" * 64
DAYS = "b" * 64
LEDGER = {"algo": "sha256-path-size-mtime", "ledger_digest": "c" * 64,
          "file_count": 120000, "total_bytes": 2 * 1024 ** 4}


def iso(offset_s=0):
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=offset_s)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def record(**over):
    r = {
        "scope": ex.ONESHOT_SCOPE, "host": HOST, "run_id": RUN_ID,
        "monolith_realpath": MONO,
        "monolith_metadata_fingerprint": META, "monolith_day_digest": DAYS,
        "monolith_content_ledger": dict(LEDGER),
        "backup_id": "vm24-daily-20260826T0300Z", "backup_completed_utc": iso(-7200),
        "backup_verified": True,
        "backup_retention_ref": "retained 30 days; restore runbook OPS-114",
        "expires_utc": iso(6 * 3600), "approved_by": "cywhale", "approved_utc": iso(-600),
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
        "capacity": {"free_now_gib": 1252.1, "filesystem_dev": 2081, "measured_utc": iso()},
        "healthz": {"status": "ok", "latest": "2026-08-25"},
        "identity": {"reviewed_sha": "d" * 40, "worktree_clean": True},
        "quiescence": {"pm2": "online", "cron": "no append window", "readers": 0},
        "monolith_writers": {"count": 0, "method": "lsof over the store"},
        "locks": {"compaction": "free", "ingest": "free"},
        "monolith_state": observed(),
        "backup": {"backup_id": "vm24-daily-20260826T0300Z", "verified": True},
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
                                    observed=observed(**(obs or {})), **kw)

    # ---------------------------------------------------------------- accepted
    def test_a_complete_bound_record_is_accepted(self):
        """The positive control: every refusal below must be about its own defect."""
        self.assertIsNone(self.problem())

    # ---------------------------------------------------------------- scope and shape
    def test_a_record_for_another_scope_is_refused(self):
        problem = self.problem({"scope": "p5_production_provisioning"})
        self.assertIn("scope", problem)

    def test_every_field_is_required(self):
        for field in ex.EXCEPTION_FIELDS:
            with self.subTest(field):
                rec = record()
                del rec[field]
                problem = ex.exception_problem(rec, host=HOST, run_id=RUN_ID,
                                               observed=observed())
                self.assertTrue(problem)
                self.assertIn(field, problem)

    def test_a_record_that_is_not_a_record_is_refused(self):
        for value in (None, True, "yes", 1, []):
            with self.subTest(repr(value)):
                problem = ex.exception_problem(value, host=HOST, run_id=RUN_ID,
                                               observed=observed())
                self.assertTrue(problem)
                self.assertIn("not a flag", problem)

    def test_a_reason_that_is_not_a_reason_is_refused(self):
        for value in ("ok", "because", "do it"):
            with self.subTest(value):
                problem = self.problem({"reason": value})
                self.assertIn("not a reason", problem)

    # ---------------------------------------------------------------- bound to host and run
    def test_another_host_is_refused(self):
        problem = self.problem(host="somewhere-else")
        self.assertIn("not accepted for another", problem)

    def test_another_run_is_refused(self):
        """"One-shot" is the whole contract: a record that outlives its run is standing
        permission."""
        problem = self.problem(run_id="a-later-run")
        self.assertIn("ONE run is what one-shot means", problem)

    # ---------------------------------------------------------------- bound to the monolith
    def test_a_changed_realpath_is_refused(self):
        problem = self.problem(obs={"monolith_realpath": "/somewhere/else.zarr"})
        self.assertIn("realpath", problem)

    def test_a_changed_metadata_fingerprint_is_refused(self):
        problem = self.problem(obs={"monolith_metadata_fingerprint": "9" * 64})
        self.assertIn("metadata fingerprint", problem)

    def test_a_changed_day_digest_is_refused(self):
        problem = self.problem(obs={"monolith_day_digest": "9" * 64})
        self.assertIn("day digest", problem)

    def test_a_changed_content_ledger_is_refused(self):
        problem = self.problem(obs={"monolith_content_ledger":
                                    dict(LEDGER, ledger_digest="9" * 64)})
        self.assertIn("content ledger has since changed", problem)

    def test_no_fresh_measurement_at_all_is_refused(self):
        """A record verified against nothing is verified against itself."""
        for value in (None, {}, "measured"):
            with self.subTest(repr(value)):
                problem = ex.exception_problem(record(), host=HOST, run_id=RUN_ID,
                                               observed=value)
                self.assertIn("no fresh measurement", problem)

    def test_fingerprints_that_are_not_digests_are_refused(self):
        for field in ("monolith_metadata_fingerprint", "monolith_day_digest"):
            with self.subTest(field):
                self.assertIn("SHA-256", self.problem({field: "not-a-digest"}))

    # ---------------------------------------------------------------- bound to a backup
    def test_an_unverified_backup_is_refused(self):
        for value in (False, None, "yes", 1):
            with self.subTest(repr(value)):
                problem = self.problem({"backup_verified": value})
                self.assertIn("backup nobody confirmed", problem)

    def test_a_backup_completing_after_the_approval_is_refused(self):
        """The person accepting the risk must have been looking at a backup that existed."""
        problem = self.problem({"backup_completed_utc": iso(600),
                                "approved_utc": iso(-600)})
        self.assertIn("after the approval", problem)

    def test_a_missing_retention_reference_is_refused(self):
        problem = self.problem({"backup_retention_ref": "   "})
        self.assertIn("backup_retention_ref", problem)

    # ---------------------------------------------------------------- and it expires
    def test_an_expired_record_is_refused(self):
        problem = self.problem({"expires_utc": iso(-60)})
        self.assertIn("expired", problem)

    def test_a_record_expiring_before_it_was_approved_is_refused(self):
        problem = self.problem({"approved_utc": iso(-60), "expires_utc": iso(-120)})
        self.assertIn("authorizes nothing", problem)

    def test_expiry_is_evaluated_against_the_supplied_clock(self):
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=12)
        problem = ex.exception_problem(record(), host=HOST, run_id=RUN_ID,
                                       observed=observed(), now=later)
        self.assertIn("expired", problem)


class TestThereIsNoGenericBypass(unittest.TestCase):
    """The exception is a record. Nothing may also accept a switch."""

    def test_the_production_operation_has_no_bypass_parameter(self):
        params = set(inspect.signature(pp.provision_production).parameters)
        for banned in ("force", "skip_seal", "allow_unsealed", "require_sealed",
                       "unsafe", "override"):
            with self.subTest(banned):
                self.assertNotIn(banned, params)

    def test_no_bypass_is_read_from_the_environment(self):
        """By AST: the modules name these words in prose to say they are absent, and a
        substring scan cannot tell that sentence from code doing it."""
        for module in (pp, ex, rd):
            tree = ast.parse(inspect.getsource(module))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    with self.subTest(module=module.__name__, const=node.value[:40]):
                        for banned in ("P5_FORCE", "P5_SKIP_SEAL", "P5_ALLOW_UNSEALED"):
                            self.assertNotIn(banned, node.value)

    def test_the_builder_is_never_called_with_the_seal_check_disabled(self):
        """`require_sealed` exists on the builder and defaults to True; the production
        operation must never pass it at all."""
        tree = ast.parse(inspect.getsource(pp))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    with self.subTest(kw=kw.arg):
                        self.assertNotEqual(kw.arg, "require_sealed")

    def test_an_ineligible_exception_is_ineligible_in_the_verdict(self):
        """Supplying a broken record is not "proceed with a note".

        This is the verdict alone. Whether the OPERATION acts on the verdict is a separate
        question with a separate test -- `TestTheOperationEnforcesTheException` below -- because
        a module that computes `eligible: false` and a caller that ignores it both pass this
        one."""
        verdict = ex.evaluate(record={}, host=HOST, run_id=RUN_ID, observed=observed(),
                              pre=pre())
        self.assertFalse(verdict["eligible"])
        self.assertIn("record", verdict["problems"])


class TestTheOperationEnforcesTheException(_ProdBase):
    """The provisioner must REFUSE on an ineligible record, not log it and continue.

    Asserting on `ex.evaluate()` proves the verdict is computed. It does not prove anything
    reads it: a `provision_production` that evaluated the record and then carried on regardless
    would satisfy every test in the module above. So these drive the real operation, against a
    real monolith, and check the filesystem afterwards."""

    def _bound(self, **over):
        """A record bound to what this fixture actually is: this host, this run, this store."""
        bound = {"host": socket.gethostname(), "run_id": self.RUN_ID,
                 "monolith_realpath": os.path.realpath(self.monolith)}
        bound.update(over)
        return record(**bound)

    def _observed(self, **over):
        bound = {"monolith_realpath": os.path.realpath(self.monolith)}
        bound.update(over)
        return observed(**bound)

    def _pre(self):
        return pre(monolith_state=self._observed())

    def test_an_ineligible_exception_refuses_the_whole_operation(self):
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision(one_shot_exception=self._bound(backup_verified=False),
                           one_shot_observed=self._observed(),
                           one_shot_pre_compaction=self._pre())
        self.assertIn("one-shot compaction exception", str(caught.exception))
        for root in self.roots():
            self.assertFalse(os.path.exists(root), f"{root} was created despite the refusal")

    def test_a_record_with_no_fresh_measurement_refuses_the_whole_operation(self):
        with self.assertRaises(pp.ProductionProvisionRefused):
            self.provision(one_shot_exception=self._bound(), one_shot_observed=None,
                           one_shot_pre_compaction=self._pre())
        for root in self.roots():
            self.assertFalse(os.path.exists(root))

    def test_a_record_for_a_different_store_refuses_even_when_self_consistent(self):
        """The hazard the `subject_realpath` binding closes.

        Record and measurement agree with each other perfectly -- and describe a store this run
        is not touching. Nothing inside the pair can detect that; only the operation, which
        knows which path it is about to publish over, can."""
        elsewhere = os.path.join(self.tmp, "some_other_monolith.zarr")
        with self.assertRaises(pp.ProductionProvisionRefused) as caught:
            self.provision(one_shot_exception=self._bound(monolith_realpath=elsewhere),
                           one_shot_observed=self._observed(monolith_realpath=elsewhere),
                           one_shot_pre_compaction=self._pre())
        self.assertIn("this run acts on", str(caught.exception))
        for root in self.roots():
            self.assertFalse(os.path.exists(root))

    def test_an_eligible_exception_lets_the_operation_proceed(self):
        """The other half: a gate that refuses everything is not a gate, it is an outage."""
        result = self.provision(one_shot_exception=self._bound(),
                                one_shot_observed=self._observed(),
                                one_shot_pre_compaction=self._pre())
        self.assertTrue(result["one_shot_exception"]["eligible"])
        for root in self.roots():
            self.assertTrue(os.path.isdir(root))

    def test_proceeding_under_the_exception_still_reports_no_readiness(self):
        """It provisioned. That is not the same as the deployment being ready to compact."""
        result = self.provision(one_shot_exception=self._bound(),
                                one_shot_observed=self._observed(),
                                one_shot_pre_compaction=self._pre())
        verdict = result["one_shot_exception"]
        self.assertNotIn("compaction_ready", verdict)
        self.assertIn("does not make monolith_barrier READY", verdict["note"])
        self.assertIn("does not make compaction_ready true", verdict["note"])
        self.assertIn("does not authorize any later run", verdict["note"])
        self.assertIn("one compaction", verdict["authorizes"])


class TestItNeverBecomesReadiness(unittest.TestCase):
    """Eligibility and readiness are different claims, reported in different fields."""

    def target(self, **over):
        t = {"label": rd.QUALIFYING_LABEL, "host": HOST, "run_id": RUN_ID,
             "one_shot_exception": record(), "monolith_state": observed(),
             "pre_compaction": pre()}
        t.update(over)
        return t

    def test_an_eligible_exception_leaves_monolith_barrier_unchanged(self):
        report = rd.assess(self.target())
        by_id = {r["id"]: r for r in report["requirements"]}
        self.assertNotEqual(by_id["monolith_barrier"]["verdict"], rd.READY)
        self.assertEqual(report["one_shot_compaction_exception"]["monolith_barrier_verdict"],
                         by_id["monolith_barrier"]["verdict"])

    def test_an_eligible_exception_does_not_make_the_deployment_ready(self):
        report = rd.assess(self.target())
        self.assertTrue(report["one_shot_compaction_exception"]["supplied"])
        self.assertFalse(report["compaction_ready"])

    def test_the_two_are_separate_fields(self):
        report = rd.assess(self.target())
        self.assertIn("compaction_ready", report)
        self.assertIn("one_shot_compaction_exception", report)
        self.assertIsNot(report["compaction_ready"],
                         report["one_shot_compaction_exception"]["eligible"])

    def test_an_absent_exception_is_the_normal_state_not_a_failure(self):
        t = self.target()
        del t["one_shot_exception"]
        field = rd.assess(t)["one_shot_compaction_exception"]
        self.assertFalse(field["supplied"])
        self.assertFalse(field["eligible"])
        self.assertIn("normal state", field["note"])

    def test_the_verdict_says_what_it_authorizes_and_what_it_does_not(self):
        verdict = ex.evaluate(record=record(), host=HOST, run_id=RUN_ID,
                              observed=observed(), pre=pre())
        self.assertTrue(verdict["eligible"])
        self.assertIn("one compaction", verdict["authorizes"])
        self.assertIn("not evidence that the risk is small", verdict["note"])
        self.assertIn("does not authorize any later run", verdict["note"])

    def test_it_never_authorizes_a_later_run(self):
        verdict = ex.evaluate(record=record(), host=HOST, run_id="the-next-run",
                              observed=observed(), pre=pre())
        self.assertFalse(verdict["eligible"])


class TestThePreCompactionContract(unittest.TestCase):

    def test_complete_fresh_evidence_passes(self):
        self.assertIsNone(ex.pre_compaction_problem(pre()))

    def test_every_field_is_required(self):
        for field in ex.PRE_COMPACTION_FIELDS:
            with self.subTest(field):
                ev = pre()
                del ev[field]
                problem = ex.pre_compaction_problem(ev)
                self.assertTrue(problem)
                self.assertIn(field, problem)

    def test_an_empty_answer_is_not_a_passing_one(self):
        for field in ex.PRE_COMPACTION_FIELDS:
            with self.subTest(field):
                problem = ex.pre_compaction_problem(pre(**{field: None}))
                self.assertTrue(problem)

    def test_a_process_writing_the_monolith_refuses(self):
        problem = ex.pre_compaction_problem(pre(monolith_writers={"count": 1, "method": "lsof"}))
        self.assertIn("open for writing", problem)

    def test_a_writer_count_that_is_not_a_count_refuses(self):
        for value in (True, "0", None, 1.5):
            with self.subTest(repr(value)):
                problem = ex.pre_compaction_problem(
                    pre(monolith_writers={"count": value, "method": "lsof"}))
                self.assertTrue(problem)

    def test_a_plan_without_a_source_map_refuses(self):
        for field in ("source_map", "destination_manifest", "days"):
            with self.subTest(field):
                plan = {k: v for k, v in pre()["plan"].items() if k != field}
                problem = ex.pre_compaction_problem(pre(plan=plan))
                self.assertIn(field, problem)

    def test_absent_pre_evidence_makes_the_exception_ineligible(self):
        verdict = ex.evaluate(record=record(), host=HOST, run_id=RUN_ID,
                              observed=observed(), pre=None)
        self.assertFalse(verdict["eligible"])
        self.assertIn("pre_compaction", verdict["problems"])


class TestThePostCompactionGates(unittest.TestCase):

    def test_complete_gates_pass(self):
        self.assertIsNone(ex.post_compaction_problem(post()))

    def test_every_gate_is_required(self):
        for field in ex.POST_COMPACTION_FIELDS:
            with self.subTest(field):
                ev = post()
                del ev[field]
                problem = ex.post_compaction_problem(ev)
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
        self.assertIn("boundaries", problem)

    def test_parity_that_checked_nothing_refuses(self):
        for value in (0, -1, True, "12"):
            with self.subTest(repr(value)):
                problem = ex.post_compaction_problem(post(point_series_parity={
                    "days_checked": value, "mismatches": 0, "boundaries_included": True}))
                self.assertTrue(problem)

    def test_a_base_and_delta_that_overlap_refuse(self):
        problem = ex.post_compaction_problem(post(base_delta_disjoint=False))
        self.assertIn("claimed by both", problem)

    def test_an_incomplete_source_map_refuses(self):
        problem = ex.post_compaction_problem(post(source_map_complete=False))
        self.assertIn("unaccounted for", problem)


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
        """The `subject_realpath` binding, in the words an operator would read."""
        flat = self._flat()
        self.assertIn("Nor is a self-consistent pair enough", flat)
        self.assertIn("both could describe a store this run is not touching", flat)
        self.assertIn("the **operation** supplies the realpath it is about to publish over",
                      flat)


    def test_it_lists_the_pre_and_post_checks(self):
        flat = self._flat()
        self.assertIn("no process holds the monolith for writing", flat)
        self.assertIn("before anything is deleted or any service is switched", flat)
        self.assertIn("`nothing_deleted: true`", flat)
        self.assertIn("nothing to go back to", flat)

    #: One distinctive phrase per limit the module states. Explicit rather than derived from
    #: the sentences: a derivation that has to guess where each one ends is a test about string
    #: slicing, and it broke on the first sentence containing a comma.
    ROLLBACK_PHRASES = (
        "whole machine",
        "delta days appended between the backup and the restore are lost",
        "repair committed to the wal in that interval is lost",
        "answered from a bad generation cannot be recalled",
        "decided and started by a person",
    )

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
    """The critical path is where someone looks to see whether a step can be skipped.

    An exception recorded only in the requirement document is one a reader of the roadmap will
    not find, and the roadmap is the thing that says step 1 blocks everything after it."""

    def _flat(self):
        with open(os.path.join(_DEV, "specs", "p5_deployment_readiness.md")) as fh:
            return " ".join(fh.read().split())

    def test_the_roadmap_scopes_the_detour_to_the_mount_step_alone(self):
        flat = self._flat()
        self.assertIn("a detour around step 1, and only step 1", flat)
        self.assertIn("Steps 2–6 are untouched", flat)

    def test_the_roadmap_says_it_is_a_record_not_a_flag(self):
        flat = self._flat()
        self.assertIn("It is a **record**, not a flag", flat)
        self.assertIn("one host, one run, one monolith at one measured state", flat)

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
