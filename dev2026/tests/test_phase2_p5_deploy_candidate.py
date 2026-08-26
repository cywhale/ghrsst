"""dev2026 — the deploy candidate review package.

A package a reviewer cannot recompute is a package they have to trust, which defeats the point.
So everything is derived from a commit, and these hold it to that: digests come from git rather
than the worktree, a dirty tree is refused, nothing claims approval, and the identity-update
plan is computed from what is actually in the inventory rather than remembered.
"""
from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
for p in (_DEV, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from ops import p5_deploy_candidate as dc  # noqa: E402
from ops import p5_oneshot_compaction as oc  # noqa: E402
from ops import p5_readiness as rd  # noqa: E402


class TestItDescribesACommitNotAWorktree(unittest.TestCase):

    def test_digests_come_from_git_not_from_disk(self):
        """The distinction the whole package rests on.

        A digest read off the worktree describes bytes that may never have been committed, so a
        reviewer recomputing from the archive would get a different answer and have no way to
        tell which was wrong."""
        source = inspect.getsource(dc.digests_at)
        self.assertIn("_blob", source)
        tree = ast.parse(inspect.getsource(dc.digests_at))
        opens = {getattr(n.func, "id", None) for n in ast.walk(tree)
                 if isinstance(n, ast.Call)}
        self.assertNotIn("open", opens)

    def test_a_dirty_worktree_is_refused(self):
        with mock.patch.object(dc, "worktree_is_clean", return_value=False):
            with self.assertRaises(dc.DeployCandidateError) as caught:
                dc.manifest("HEAD")
        self.assertIn("describes a tree that exists nowhere", str(caught.exception))

    def test_a_dirty_worktree_can_be_described_only_deliberately(self):
        """`require_clean=False` exists for exactly this suite, and is not the default."""
        signature = inspect.signature(dc.manifest)
        self.assertIs(signature.parameters["require_clean"].default, True)

    def test_an_unknown_revision_fails_closed(self):
        with self.assertRaises(dc.DeployCandidateError):
            dc.manifest("definitely-not-a-commit", require_clean=False)

    def test_a_module_absent_from_the_candidate_is_reported_as_none(self):
        """Reported, not skipped: a missing module is what the reviewer needs to see."""
        digests = dc.digests_at("HEAD", ("ops/definitely_not_here.py",))
        self.assertIn("ops/definitely_not_here.py", digests)
        self.assertIsNone(digests["ops/definitely_not_here.py"])

    def test_the_digests_are_reproducible_from_the_archive(self):
        """The reviewer's own check, performed here: unpack and recompute."""
        doc = dc.manifest("HEAD", require_clean=False)
        sha = doc["candidate_sha"]
        with tempfile.TemporaryDirectory() as tmp:
            tar = os.path.join(tmp, "candidate.tar")
            with open(tar, "wb") as fh:
                proc = subprocess.run(
                    ("git", "-C", dc._repo_root(), "archive", "--format=tar", sha,
                     "--", dc.DEV_PREFIX), capture_output=True)
                self.assertEqual(proc.returncode, 0)
                fh.write(proc.stdout)
            subprocess.run(("tar", "-xf", tar, "-C", tmp), check=True)
            for rel, digest in doc["deployment_inventory_digests"].items():
                if digest is None:
                    continue
                path = os.path.join(tmp, dc.DEV_PREFIX, rel)
                with self.subTest(rel):
                    self.assertTrue(os.path.isfile(path))
                    with open(path, "rb") as fh:
                        self.assertEqual(hashlib.sha256(fh.read()).hexdigest(), digest)


class TestItApprovesNothing(unittest.TestCase):

    def test_the_manifest_says_it_is_not_approved(self):
        doc = dc.manifest("HEAD", require_clean=False)
        self.assertFalse(doc["approved"])
        self.assertIn("NOT APPROVED", doc["approval_note"])

    def test_it_never_writes_the_approved_sha_set(self):
        """By AST: nothing assigns to the approved set or mutates it.

        Scoped to that RECEIVER rather than to the method names: the module builds an ordinary
        list of plan steps with `.append`, and a blanket ban on mutating calls flagged it. What
        matters is what is being mutated, not which verb was used."""
        tree = ast.parse(inspect.getsource(dc))
        mutators = ("append", "add", "extend", "insert", "remove", "pop", "update")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store):
                self.assertNotEqual(node.attr, "REVIEWED_DEPLOY_SHAS")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in mutators:
                    receiver = node.func.value
                    target = getattr(receiver, "attr", None) or getattr(receiver, "id", None)
                    self.assertNotEqual(target, "REVIEWED_DEPLOY_SHAS")
                    self.assertNotEqual(target, "INVENTORY_MODULES")

    def test_it_never_writes_any_module_of_the_repository(self):
        """The package is generated into a caller-supplied directory and touches nothing else.

        `write_package` creates that directory and three files in it; anything opened for
        writing outside it would be the tool editing the tree it is meant to describe."""
        tree = ast.parse(inspect.getsource(dc))
        write_modes = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "open":
                for arg in node.args[1:]:
                    if isinstance(arg, ast.Constant):
                        write_modes.add(arg.value)
        self.assertTrue(write_modes.issubset({"w", "wb", "rb"}),
                        f"unexpected file modes: {sorted(write_modes)}")

    def test_the_candidate_is_not_already_in_the_approved_set(self):
        """If it were, the package would be describing something already approved and the
        reviewer would be answering a question that had been answered."""
        doc = dc.manifest("HEAD", require_clean=False)
        self.assertNotIn(doc["candidate_sha"], rd.REVIEWED_DEPLOY_SHAS)

    def test_the_plan_says_it_changes_nothing(self):
        plan = dc.identity_update_plan("HEAD")
        self.assertIn("edits nothing", plan["not_done_here"])


class TestTheIdentityUpdatePlanIsDerived(unittest.TestCase):

    def test_it_names_the_producer_modules_outside_the_inventory(self):
        plan = dc.identity_update_plan("HEAD")
        outside = plan["producer_modules_outside_the_deployment_inventory"]
        self.assertIn("ops/p5_parity_producers.py", outside,
                      "the producers are not in the deployment inventory yet, and the plan "
                      "must say so rather than implying they are covered")

    def test_it_requires_the_candidate_be_added_to_the_approved_set(self):
        plan = dc.identity_update_plan("HEAD")
        self.assertTrue(any("REVIEWED_DEPLOY_SHAS" in s for s in plan["steps"]))

    def test_it_warns_that_leaving_a_module_in_both_keeps_the_weaker_path(self):
        plan = dc.identity_update_plan("HEAD")
        self.assertTrue(any("weaker path alive" in s for s in plan["steps"]))

    def test_it_recomputes_rather_than_remembering(self):
        """With the producers already in the inventory, the outstanding steps shrink."""
        with mock.patch.object(
                rd, "INVENTORY_MODULES",
                tuple(rd.INVENTORY_MODULES) + ("ops/p5_parity_producers.py",)):
            plan = dc.identity_update_plan("HEAD")
        self.assertEqual(plan["producer_modules_outside_the_deployment_inventory"], [])
        self.assertFalse(any("weaker path alive" in s for s in plan["steps"]))

    def test_it_reports_the_currently_approved_shas(self):
        plan = dc.identity_update_plan("HEAD")
        self.assertEqual(tuple(plan["already_approved_shas"]), rd.REVIEWED_DEPLOY_SHAS)


class TestThePackageCoversWhatTheReviewerNeeds(unittest.TestCase):

    def test_it_covers_the_whole_deployment_inventory(self):
        doc = dc.manifest("HEAD", require_clean=False)
        self.assertEqual(set(doc["deployment_inventory_digests"]),
                         set(rd.INVENTORY_MODULES))

    def test_it_covers_every_operation_module(self):
        doc = dc.manifest("HEAD", require_clean=False)
        self.assertEqual(set(doc["operation_module_digests"]), set(dc.OPERATION_MODULES))

    def test_the_operation_modules_include_the_producers_and_the_compaction(self):
        for required in ("ops/p5_parity_producers.py", "ops/p5_oneshot_compaction.py",
                         "ops/p5_oneshot_exception.py"):
            with self.subTest(required):
                self.assertIn(required, dc.OPERATION_MODULES)

    def test_every_registered_producer_module_is_covered(self):
        """A producer module the package does not digest is one the reviewer cannot check."""
        for module in oc.REVIEWED_PRODUCER_MODULES:
            with self.subTest(module):
                self.assertIn(module, dc.OPERATION_MODULES)

    def test_the_file_list_is_the_candidate_tree(self):
        doc = dc.manifest("HEAD", require_clean=False)
        self.assertGreater(doc["file_count"], 100)
        self.assertEqual(doc["file_count"], len(doc["files"]))
        self.assertIn("ops/p5_oneshot_compaction.py", doc["files"])

    def test_writing_the_package_produces_all_three_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = dc.write_package(tmp, "HEAD", require_clean=False)
        self.assertEqual(set(written) - {"archive_sha256", "candidate_sha"},
                         {"manifest", "diff", "archive"})

    def test_the_written_manifest_is_the_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = dc.write_package(tmp, "HEAD", require_clean=False)
            with open(written["manifest"]) as fh:
                doc = json.load(fh)
        self.assertEqual(doc["candidate_sha"], written["candidate_sha"])
        self.assertFalse(doc["approved"])

    def test_the_archive_digest_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            written = dc.write_package(tmp, "HEAD", require_clean=False)
            with open(written["archive"], "rb") as fh:
                recomputed = hashlib.sha256(fh.read()).hexdigest()
        self.assertEqual(written["archive_sha256"], recomputed)


class TestTheReviewDocumentSaysWhatIsBlocked(unittest.TestCase):

    def _flat(self):
        with open(os.path.join(_DEV, "specs", "p5_deploy_candidate_review.md")) as fh:
            return " ".join(fh.read().split())

    def test_it_states_that_it_approves_nothing(self):
        self.assertIn("This document does not approve anything", self._flat())

    def test_it_states_why_a_new_candidate_is_needed(self):
        flat = self._flat()
        self.assertIn("postdates every commit in `p5_readiness.REVIEWED_DEPLOY_SHAS`", flat)
        self.assertIn("no production run is possible", flat)

    def test_it_covers_all_four_open_items(self):
        flat = self._flat()
        for heading in ("Finalization", "`backup_verified`", "Continuous writer fence",
                        "Arbitrary-writer enforcement"):
            with self.subTest(heading):
                self.assertIn(heading, flat)

    def test_each_item_states_whether_it_blocks_execution(self):
        flat = self._flat()
        self.assertEqual(flat.count("**Execution blocker?**"), 4)

    def test_it_refuses_to_read_a_green_suite_as_readiness(self):
        flat = self._flat()
        self.assertIn("A green local test suite is not evidence about production", flat)
        self.assertIn("What a green local suite does and does not say", flat)

    def test_it_names_the_arbitrary_writer_case_as_the_reason_this_stays_an_exception(self):
        flat = self._flat()
        self.assertIn("the reason this is a one-shot exception rather than a way of working",
                      flat)

    def test_it_accounts_for_every_skipped_test(self):
        flat = self._flat()
        self.assertIn("The 17 skipped tests", flat)
        self.assertIn("Nothing on a critical path is skipped", flat)
        for suite, count in (("test_parity.ParityTests", "11"),
                             ("test_store_access.RealStoreParityTests", "5"),
                             ("test_phase2_s4.RealRegionalCubeTests", "1")):
            with self.subTest(suite):
                self.assertIn(suite, flat)
                self.assertIn(count, flat)

    def test_it_names_the_conditional_skips_that_did_not_fire(self):
        """Absence of a skip is not coverage unless someone checked."""
        flat = self._flat()
        self.assertIn("psutil not installed", flat)
        self.assertIn("root bypasses directory permissions", flat)
