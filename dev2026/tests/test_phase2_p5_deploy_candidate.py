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


def _read_text(path):
    """Read and CLOSE. `open(p).read()` leaves the handle to the garbage collector, which
    under `-W error::ResourceWarning` surfaces as an "Exception ignored" during finalization
    rather than as a test failure -- visible only to someone reading stderr."""
    with open(path) as fh:
        return fh.read()


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

    def test_a_modified_file_refuses_the_package(self):
        """Mocks what `manifest()` ACTUALLY calls.

        This test used to patch `worktree_is_clean()`, which `manifest()` never calls -- it
        reads `dirty_paths()` and `ignored_modules()` directly, because it needs the individual
        lists for the message. So the mock did nothing and the refusal came from the working
        tree really being dirty. It passed for two rounds and failed the moment the tree was
        clean, which is the only honest signal it ever gave."""
        with mock.patch.object(dc, "dirty_paths", return_value=["dev2026/store/time_cube.py"]), \
                mock.patch.object(dc, "ignored_modules", return_value=[]):
            with self.assertRaises(dc.DeployCandidateError) as caught:
                dc.manifest("HEAD")
        self.assertIn("describes a tree that exists nowhere", str(caught.exception))
        self.assertIn("store/time_cube.py", str(caught.exception))

    def test_an_ignored_module_refuses_the_package_on_its_own(self):
        """The other half, isolated: `dirty_paths()` is empty, so only the ignored-module
        finding can be the reason. An ignored `.py` is invisible to `status` at any untracked
        mode, sits on disk, and is imported at run time."""
        with mock.patch.object(dc, "dirty_paths", return_value=[]), \
                mock.patch.object(dc, "ignored_modules",
                                  return_value=["dev2026/ops/sneaky.py"]):
            with self.assertRaises(dc.DeployCandidateError) as caught:
                dc.manifest("HEAD")
        self.assertIn("IGNORED python modules", str(caught.exception))

    def test_a_clean_tree_is_accepted(self):
        """The positive control, so the two refusals above are not refusing everything."""
        with mock.patch.object(dc, "dirty_paths", return_value=[]), \
                mock.patch.object(dc, "ignored_modules", return_value=[]):
            self.assertFalse(dc.manifest("HEAD")["approved"])

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
    """Recomputed from the two inventories, not remembered.

    The plan changed shape when the operation-SHA mechanism replaced the disk-only path, and
    these were rewritten with it rather than patched to keep passing: the old ones asserted
    that the plan warns about leaving a module in both inventories, which is no longer the
    outstanding question."""

    def test_it_names_the_operation_inventory(self):
        plan = dc.identity_update_plan("HEAD")
        self.assertIn("ops/p5_parity_producers.py", plan["operation_inventory"])

    def test_it_reports_the_deploy_set_and_the_operation_approval_separately(self):
        plan = dc.identity_update_plan("HEAD")
        self.assertEqual(tuple(plan["approved_deploy_shas"]), rd.REVIEWED_DEPLOY_SHAS)
        self.assertIn("operation_approval", plan)
        self.assertEqual(plan["operation_approval"]["scope"], oc.OPERATION_APPROVAL_SCOPE)

    def test_it_separates_shared_modules_from_operation_only_ones(self):
        """They are NOT disjoint, and reporting them as such was the earlier error. The service
        and a compaction share the whole store and ingest layer."""
        plan = dc.identity_update_plan("HEAD")
        self.assertIn("store/block_manifest.py", plan["shared_with_service_inventory"])
        self.assertIn("ops/p5_oneshot_compaction.py", plan["operation_only_modules"])
        self.assertTrue(any("the deploy approval does not cover them" in s
                            for s in plan["steps"]))

    def test_it_reports_that_no_approval_record_is_configured(self):
        plan = dc.identity_update_plan("HEAD")
        self.assertFalse(plan["operation_approval"]["configured"])
        self.assertTrue(any("no usable approval record today" in s for s in plan["steps"]))

    def test_it_requires_an_external_approval_record_before_anything_else(self):
        plan = dc.identity_update_plan("HEAD")
        self.assertIn("OPERATION APPROVAL RECORD outside the repository", plan["steps"][0])
        self.assertIn("can approve a change to itself", plan["steps"][0])


class TestThePackageCoversWhatTheReviewerNeeds(unittest.TestCase):

    def test_it_covers_the_whole_deployment_inventory(self):
        doc = dc.manifest("HEAD", require_clean=False)
        self.assertEqual(set(doc["deployment_inventory_digests"]),
                         set(rd.INVENTORY_MODULES))

    def test_it_covers_every_operation_module(self):
        doc = dc.manifest("HEAD", require_clean=False)
        self.assertEqual(set(doc["operation_module_digests"]), set(dc.operation_modules()))

    def test_the_operation_modules_include_the_producers_and_the_compaction(self):
        for required in ("ops/p5_parity_producers.py", "ops/p5_oneshot_compaction.py",
                         "ops/p5_oneshot_exception.py"):
            with self.subTest(required):
                self.assertIn(required, dc.operation_modules())

    def test_the_whole_runtime_closure_is_covered(self):
        """Derived from the gate, so the package cannot report a smaller set than what runs."""
        for module in oc.OPERATION_INVENTORY:
            with self.subTest(module):
                self.assertIn(module, dc.operation_modules())

    def test_the_package_list_is_not_a_second_hand_written_list(self):
        source = inspect.getsource(dc.operation_modules)
        self.assertIn("oc.OPERATION_INVENTORY", source)

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
        self.assertIn("postdates every approved commit", flat)
        self.assertIn("the operation refuses today", flat)
        self.assertIn("No production run is possible until a commit containing the producers "
                      "is approved", flat)

    def test_it_names_the_weaker_check_it_replaced(self):
        flat = self._flat()
        self.assertIn("the operator measured what they shipped", flat)
        self.assertIn("it does not establish *anyone reviewed it*", flat)

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


class TestTheCleanlinessCheckSeesEverything(unittest.TestCase):
    """`--porcelain` alone was not enough, in two separate ways.

    Default `--untracked-files=normal` collapses an untracked DIRECTORY to one entry and never
    names the files inside it. And no untracked mode lists IGNORED files at all — a module in
    `.gitignore` sits on disk, gets imported at run time, and appears in no listing the package
    produces, while every digest in the manifest still matches.

    That second case is not hypothetical here: `producer_identity()` imports a module by path
    and hashes the file on disk, so an ignored module at a registry path would pass the on-disk
    check and be absent from the reviewed commit."""

    def test_it_asks_for_untracked_files_all(self):
        source = inspect.getsource(dc.dirty_paths)
        self.assertIn("--untracked-files=all", source)

    def test_it_asks_for_ignored_files_too(self):
        source = inspect.getsource(dc.ignored_modules)
        self.assertIn("--ignored", source)

    def test_a_file_inside_an_untracked_directory_is_named(self):
        """The `-uall` case: `normal` would report the directory and not the file."""
        scratch = os.path.join(_DEV, "ops", "_pkg_probe_dir")
        module = os.path.join(scratch, "runtime_thing.py")
        os.makedirs(scratch, exist_ok=True)
        try:
            with open(module, "w") as fh:
                fh.write("VALUE = 1\n")
            dirty = dc.dirty_paths()
            self.assertIn("dev2026/ops/_pkg_probe_dir/runtime_thing.py", dirty,
                          "an untracked directory was collapsed; the file inside is unnamed")
            self.assertFalse(dc.worktree_is_clean())
        finally:
            os.remove(module)
            os.rmdir(scratch)

    def test_an_ignored_runtime_module_makes_the_package_refuse(self):
        """The case the review asked for.

        A module git is told to ignore, at a path the operation could import. `status` at any
        untracked mode reports nothing; the package must still refuse."""
        gitignore = os.path.join(os.path.dirname(_DEV), ".gitignore")
        module = os.path.join(_DEV, "ops", "_ignored_runtime_probe.py")
        had_gitignore = os.path.isfile(gitignore)
        original = _read_text(gitignore) if had_gitignore else None
        try:
            with open(gitignore, "a") as fh:
                fh.write("\ndev2026/ops/_ignored_runtime_probe.py\n")
            with open(module, "w") as fh:
                fh.write("def point_series_parity(**kw):\n    return {'mismatches': 0}\n")

            # `status` at the strictest untracked mode does not see it. That is the hazard.
            self.assertNotIn("dev2026/ops/_ignored_runtime_probe.py", dc.dirty_paths())

            self.assertIn("dev2026/ops/_ignored_runtime_probe.py", dc.ignored_modules())
            self.assertFalse(dc.worktree_is_clean())
            with self.assertRaises(dc.DeployCandidateError) as caught:
                dc.manifest("HEAD")
            self.assertIn("IGNORED python modules", str(caught.exception))
            self.assertIn("_ignored_runtime_probe.py", str(caught.exception))
        finally:
            if os.path.isfile(module):
                os.remove(module)
            if original is None:
                if os.path.isfile(gitignore):
                    os.remove(gitignore)
            else:
                with open(gitignore, "w") as fh:
                    fh.write(original)

    def test_an_ignored_non_python_file_does_not_trip_it(self):
        """Isolating: build output and caches are ignored on purpose and are not modules."""
        gitignore = os.path.join(os.path.dirname(_DEV), ".gitignore")
        blob = os.path.join(_DEV, "ops", "_ignored_probe.log")
        had = os.path.isfile(gitignore)
        original = _read_text(gitignore) if had else None
        try:
            with open(gitignore, "a") as fh:
                fh.write("\ndev2026/ops/_ignored_probe.log\n")
            with open(blob, "w") as fh:
                fh.write("noise\n")
            self.assertEqual(dc.ignored_modules(), [],
                             "an ignored log was treated as a runtime module")
        finally:
            if os.path.isfile(blob):
                os.remove(blob)
            if original is None:
                if os.path.isfile(gitignore):
                    os.remove(gitignore)
            else:
                with open(gitignore, "w") as fh:
                    fh.write(original)

    def test_a_clean_tree_passes(self):
        """The positive control, and it also proves the suite cleans up after itself."""
        self.assertEqual(dc.ignored_modules(), [])


class TestTheIdentityPlanReflectsTheOperationSha(unittest.TestCase):
    """Item 4's mechanism, as the plan reports it."""

    def test_it_asks_for_an_external_record_first(self):
        plan = dc.identity_update_plan("HEAD")
        self.assertIn("outside the repository", plan["steps"][0])

    def test_it_says_why_the_allow_list_cannot_live_in_the_operation(self):
        plan = dc.identity_update_plan("HEAD")
        self.assertIn("can approve a change to itself", plan["steps"][0])

    def test_it_keeps_the_service_decision_separate(self):
        plan = dc.identity_update_plan("HEAD")
        self.assertTrue(any("decide SEPARATELY" in s for s in plan["steps"]))
        self.assertTrue(any("re-approving tooling on every service change" in s
                            for s in plan["steps"]))

    def test_it_reports_both_inventories_and_what_they_share(self):
        plan = dc.identity_update_plan("HEAD")
        self.assertGreater(len(plan["shared_with_service_inventory"]), 5)
        self.assertIn("ops/p5_parity_producers.py", plan["operation_inventory"])
        self.assertEqual(plan["operation_inventory_size"], len(oc.OPERATION_INVENTORY))
        self.assertGreater(plan["service_inventory_size"], 20)

    def test_it_reports_whether_the_candidate_actually_contains_the_modules(self):
        plan = dc.identity_update_plan("HEAD")
        present = plan["operation_modules_present_at_candidate"]
        self.assertEqual(set(present), set(oc.OPERATION_INVENTORY))

    def test_it_warns_when_the_candidate_lacks_an_operation_module(self):
        old = rd.REVIEWED_DEPLOY_SHAS[0]
        plan = dc.identity_update_plan(old)
        self.assertTrue(any("is not present at" in s for s in plan["steps"]))
        self.assertFalse(plan["operation_modules_present_at_candidate"]
                         ["ops/p5_parity_producers.py"])

    def test_the_document_states_the_chosen_boundary(self):
        flat = " ".join(_read_text(
            os.path.join(_DEV, "specs", "p5_deploy_candidate_review.md")).split())
        self.assertIn("The closure was one module; it is twenty-one", flat)
        self.assertIn("is the code the SERVICE runs reviewed?", flat)
        self.assertIn("is the code the OPERATION runs reviewed?", flat)
        self.assertIn("The two inventories are NOT disjoint", flat)
        self.assertIn("The allow-list cannot live in the operation", flat)

    def test_the_document_explains_the_self_approval_problem(self):
        flat = " ".join(_read_text(
            os.path.join(_DEV, "specs", "p5_deploy_candidate_review.md")).split())
        self.assertIn("Why the implementation cannot approve a changed implementation", flat)
        self.assertIn("no commit can contain its own id", flat)
        self.assertIn("a changed implementation vouching for the version it replaced", flat)
        self.assertIn("An allow-list a change can extend is an allow-list the change approves "
                      "itself with", flat)

    def test_the_document_corrects_the_earlier_disjointness_claim(self):
        flat = " ".join(_read_text(
            os.path.join(_DEV, "specs", "p5_deploy_candidate_review.md")).split())
        self.assertIn("An earlier revision of this document claimed they were", flat)


class TestTheCleanlinessConjunctionInIsolation(unittest.TestCase):
    """`worktree_is_clean()` is an AND of two independent findings, tested as one.

    The end-to-end ignored-module test asserted `worktree_is_clean()` is False while the working
    tree ALSO had modified files — so it passed on the `dirty_paths()` half and would have
    passed with the `ignored_modules()` half deleted. A mutation proved exactly that. These
    stub each half so the other cannot be the reason.
    """

    def test_ignored_modules_alone_make_the_tree_unclean(self):
        with mock.patch.object(dc, "dirty_paths", return_value=[]), \
                mock.patch.object(dc, "ignored_modules",
                                  return_value=["dev2026/ops/sneaky.py"]):
            self.assertFalse(dc.worktree_is_clean())

    def test_dirty_paths_alone_make_the_tree_unclean(self):
        with mock.patch.object(dc, "dirty_paths", return_value=["dev2026/ops/edited.py"]), \
                mock.patch.object(dc, "ignored_modules", return_value=[]):
            self.assertFalse(dc.worktree_is_clean())

    def test_neither_means_clean(self):
        with mock.patch.object(dc, "dirty_paths", return_value=[]), \
                mock.patch.object(dc, "ignored_modules", return_value=[]):
            self.assertTrue(dc.worktree_is_clean())

    def test_the_manifest_refuses_on_ignored_modules_alone(self):
        """The other masked guard: `if dirty or ignored` passed on `dirty` every time."""
        with mock.patch.object(dc, "dirty_paths", return_value=[]), \
                mock.patch.object(dc, "ignored_modules",
                                  return_value=["dev2026/ops/sneaky.py"]):
            with self.assertRaises(dc.DeployCandidateError) as caught:
                dc.manifest("HEAD")
        message = str(caught.exception)
        self.assertIn("IGNORED python modules", message)
        self.assertIn("sneaky.py", message)
        self.assertNotIn("Untracked or modified", message,
                         "the refusal named the wrong finding")

    def test_the_manifest_refuses_on_dirty_paths_alone(self):
        with mock.patch.object(dc, "dirty_paths", return_value=["dev2026/ops/edited.py"]), \
                mock.patch.object(dc, "ignored_modules", return_value=[]):
            with self.assertRaises(dc.DeployCandidateError) as caught:
                dc.manifest("HEAD")
        message = str(caught.exception)
        self.assertIn("Untracked or modified", message)
        self.assertNotIn("IGNORED python modules", message)

    def test_a_clean_tree_produces_a_manifest(self):
        """The positive control for the conjunction, independent of this checkout's state."""
        with mock.patch.object(dc, "dirty_paths", return_value=[]), \
                mock.patch.object(dc, "ignored_modules", return_value=[]):
            doc = dc.manifest("HEAD")
        self.assertFalse(doc["approved"])


# =====================================================================================
class TestTheCompatibilityMatrixIsComputedFromGit(unittest.TestCase):
    """The deployment-version composition question, answered from blobs rather than prose.

    A reviewer asked which commits can host this operation. That is not a matter of opinion:
    either a commit contains the 21 operation modules at the bytes the approval names, or it
    does not, and `git cat-file` settles it. These tests assert the matrix reports what git
    actually holds, so a wrong claim in a document cannot survive the suite."""

    DEPLOY = "708058e15b37a02e1d7aa5a66b9978c6e13bd446"

    def matrix(self):
        return dc.compatibility_matrix("HEAD")

    def test_the_current_deploy_commit_cannot_host_the_operation(self):
        row = self.matrix()["baselines"][self.DEPLOY]
        self.assertFalse(row["can_host_the_operation"])
        self.assertIn("absent", row["why_not"])

    def test_it_names_the_five_absent_operation_modules(self):
        row = self.matrix()["baselines"][self.DEPLOY]
        self.assertEqual(sorted(row["operation_modules_absent"]), sorted([
            "ops/p5_legacy_base.py", "ops/p5_oneshot_compaction.py",
            "ops/p5_oneshot_exception.py", "ops/p5_parity_producers.py",
            "ops/p5_prod_provision.py"]))

    def test_absent_and_differing_are_reported_separately(self):
        """Two different failures: never reviewed here, versus reviewed at other bytes.
        Collapsing them would tell a reviewer the wrong thing to go and read."""
        row = self.matrix()["baselines"][self.DEPLOY]
        self.assertEqual(sorted(row["operation_modules_differing"]), sorted([
            "ingest/publish_manifest.py", "ops/p5_readiness.py", "ops/p5s8_evidence.py",
            "store/block_manifest.py", "store/segmented_cube.py"]))
        self.assertFalse(set(row["operation_modules_absent"])
                         & set(row["operation_modules_differing"]))

    def test_the_three_operation_module_groups_partition_the_inventory(self):
        row = self.matrix()["baselines"][self.DEPLOY]
        total = (len(row["operation_modules_absent"]) + len(row["operation_modules_differing"])
                 + len(row["operation_modules_identical"]))
        self.assertEqual(total, len(oc.OPERATION_INVENTORY))

    def test_the_deploy_commit_carries_every_service_module(self):
        """It is a valid deployment. It just is not one this operation can run inside."""
        row = self.matrix()["baselines"][self.DEPLOY]
        self.assertEqual(row["service_modules_absent"], [])

    def test_every_reviewed_deploy_sha_is_reported(self):
        matrix = self.matrix()
        self.assertEqual(sorted(matrix["baselines"]), sorted(rd.REVIEWED_DEPLOY_SHAS))
        for sha, row in matrix["baselines"].items():
            with self.subTest(sha[:7]):
                self.assertFalse(row["can_host_the_operation"])

    def test_the_candidate_does_host_the_operation(self):
        matrix = self.matrix()
        self.assertTrue(matrix["candidate_hosts_the_operation"])

    def test_the_inventories_overlap_and_the_matrix_says_so(self):
        matrix = self.matrix()
        self.assertEqual(matrix["service_inventory_size"], 23)
        self.assertEqual(matrix["operation_inventory_size"], 21)
        self.assertEqual(len(matrix["shared_modules"]), 11)
        self.assertEqual(len(matrix["service_only_modules"]), 12)
        self.assertEqual(len(matrix["operation_only_modules"]), 10)
        self.assertEqual(
            len(matrix["shared_modules"]) + len(matrix["operation_only_modules"]),
            matrix["operation_inventory_size"])

    def test_it_reports_which_capabilities_the_changes_touch(self):
        touched = self.matrix()["capabilities_touched_vs_baselines"]
        self.assertEqual(sorted(touched), ["api_parity", "generation_one",
                                           "one_shot_compaction", "provisioning"])
        self.assertIn("api/app.py", touched["api_parity"])
        self.assertIn("ops/p5_oneshot_compaction.py", touched["one_shot_compaction"])

    def test_it_names_all_three_approvals_and_grants_none(self):
        matrix = self.matrix()
        blob = " ".join(matrix["approvals_required"])
        self.assertIn("SERVICE DEPLOYMENT APPROVAL", blob)
        self.assertIn("OPERATION APPROVAL", blob)
        self.assertIn("ASSESSOR APPROVAL", blob)
        self.assertFalse(matrix["candidate_is_approved"])
        self.assertNotEqual(matrix["candidate"], matrix["current_deploy_sha"])

    def test_generating_the_matrix_changes_no_approval_state(self):
        before = (tuple(rd.REVIEWED_DEPLOY_SHAS), rd.CURRENT_DEPLOY_SHA,
                  oc.OPERATION_APPROVAL_PATH)
        self.matrix()
        self.assertEqual(before, (tuple(rd.REVIEWED_DEPLOY_SHAS), rd.CURRENT_DEPLOY_SHA,
                                  oc.OPERATION_APPROVAL_PATH))

    def test_the_matrix_agrees_with_git_cat_file(self):
        """Recomputed independently, by a different code path, from the same commits."""
        row = self.matrix()["baselines"][self.DEPLOY]
        for rel in row["operation_modules_absent"]:
            with self.subTest(rel):
                proc = subprocess.run(
                    ("git", "-C", os.path.dirname(_DEV), "cat-file", "blob",
                     f"{self.DEPLOY}:dev2026/{rel}"), capture_output=True)
                self.assertNotEqual(proc.returncode, 0, f"{rel} exists at {self.DEPLOY}")
        head = subprocess.run(("git", "-C", os.path.dirname(_DEV), "rev-parse", "HEAD"),
                              capture_output=True, text=True, check=True).stdout.strip()
        for rel in row["operation_modules_differing"]:
            with self.subTest(rel):
                digests = [hashlib.sha256(subprocess.run(
                    ("git", "-C", os.path.dirname(_DEV), "cat-file", "blob",
                     f"{sha}:dev2026/{rel}"), capture_output=True,
                    check=True).stdout).hexdigest() for sha in (self.DEPLOY, head)]
                self.assertNotEqual(digests[0], digests[1])


# =====================================================================================
class TestTheChangedFileListDistinguishesAdditions(unittest.TestCase):
    """`--name-status`, not `--name-only`. Five operation modules are ADDED relative to the
    current deploy commit, and a flat list of names cannot say that -- which is exactly the
    fact a reviewer of this composition needs first."""

    DEPLOY = "708058e15b37a02e1d7aa5a66b9978c6e13bd446"

    def test_the_operation_modules_are_reported_as_additions(self):
        rows = dc.changed_files(self.DEPLOY, "HEAD")
        for rel in ("ops/p5_oneshot_compaction.py", "ops/p5_oneshot_exception.py",
                    "ops/p5_parity_producers.py", "ops/p5_legacy_base.py",
                    "ops/p5_prod_provision.py"):
            with self.subTest(rel):
                self.assertIn(rel, rows["added"])
                self.assertNotIn(rel, rows["modified"])

    def test_the_shared_store_modules_are_reported_as_modifications(self):
        rows = dc.changed_files(self.DEPLOY, "HEAD")
        for rel in ("store/segmented_cube.py", "store/block_manifest.py",
                    "ingest/publish_manifest.py"):
            with self.subTest(rel):
                self.assertIn(rel, rows["modified"])

    def test_nothing_is_deleted(self):
        """A deployment candidate that removes a module the service imports would be a very
        different review. Asserting it here means nobody has to notice its absence."""
        self.assertEqual(dc.changed_files(self.DEPLOY, "HEAD")["deleted"], [])

    def test_the_total_is_the_sum_of_the_groups(self):
        rows = dc.changed_files(self.DEPLOY, "HEAD")
        self.assertEqual(rows["total"], len(rows["added"]) + len(rows["modified"])
                         + len(rows["deleted"]) + len(rows["other"]))
        self.assertEqual(rows["base"], self.DEPLOY)

    def test_the_package_carries_both(self):
        with mock.patch.object(dc, "dirty_paths", return_value=[]), \
                mock.patch.object(dc, "ignored_modules", return_value=[]):
            doc = dc.manifest("HEAD")
        self.assertIn("compatibility_matrix", doc)
        self.assertIn("changed_vs_current_deploy", doc)
        self.assertEqual(doc["changed_vs_current_deploy"]["base"], rd.CURRENT_DEPLOY_SHA)
        self.assertEqual(len(doc["deployment_inventory_digests"]), 23)
        self.assertFalse(doc["approved"])


# =====================================================================================
class TestTheIntegrationCandidateDocumentIsTrue(unittest.TestCase):
    """A document nobody checks becomes a document that is wrong.

    `p5_integration_candidate.md` states counts and module names that are all derivable from
    git. These tests re-derive them, so the spec fails the suite rather than quietly ageing
    into a false claim about which commits can host the operation."""

    DOC = os.path.join(_DEV, "specs", "p5_integration_candidate.md")

    def doc(self):
        """Whitespace-collapsed. The document is hard-wrapped, so a phrase that reads as one
        sentence can be split across a newline; asserting on the raw text would make these
        tests depend on where the wrap happens to fall."""
        return " ".join(_read_text(self.DOC).split())

    def test_it_exists_and_says_nothing_is_approved(self):
        text = self.doc()
        self.assertIn("Nothing here is approved", text)
        self.assertIn("no operation approval record exists", text)

    def test_the_five_absent_modules_are_named_and_really_absent(self):
        text = self.doc()
        matrix = dc.compatibility_matrix("HEAD")
        absent = matrix["baselines"][rd.CURRENT_DEPLOY_SHA]["operation_modules_absent"]
        self.assertEqual(len(absent), 5)
        for rel in absent:
            with self.subTest(rel):
                self.assertIn(rel, text)

    def test_the_inventory_counts_in_the_document_match_the_code(self):
        text = self.doc()
        matrix = dc.compatibility_matrix("HEAD")
        self.assertEqual(matrix["service_inventory_size"], 23)
        self.assertEqual(matrix["operation_inventory_size"], 21)
        self.assertIn("23 service + 21 operation, sharing 11", text)
        self.assertEqual(len(matrix["shared_modules"]), 11)

    def test_it_states_all_three_approvals(self):
        text = self.doc()
        for phrase in ("Service deployment approval", "Operation approval",
                       "Assessor approval"):
            with self.subTest(phrase):
                self.assertIn(phrase, text)

    def test_it_does_not_claim_readiness(self):
        text = self.doc().lower()
        self.assertIn("not compaction readiness", text)
        self.assertNotIn("ready to deploy", text)

    def test_it_does_not_transcribe_a_total_it_cannot_keep_true(self):
        """The document is part of the diff it describes, so any commit that updates a written
        total changes it. The complete list is generated into the package manifest instead, and
        the document says so rather than carrying a number that is stale on arrival."""
        text = self.doc()
        self.assertIn("changed_vs_current_deploy", text)
        self.assertIn("itself part of the diff it describes", text)

    def test_every_changed_code_module_the_document_lists_really_changed(self):
        """The stable claim, checked against git: each module named in the document is in the
        diff, on the side the document puts it."""
        rows = dc.changed_files(rd.CURRENT_DEPLOY_SHA, "HEAD")
        text = self.doc()
        for rel in ("ops/p5_oneshot_compaction.py", "ops/p5_parity_producers.py",
                    "ops/p5_legacy_base.py", "ops/p5_oneshot_exception.py",
                    "ops/p5_prod_provision.py"):
            with self.subTest("added", rel=rel):
                self.assertIn(rel, text)
                self.assertIn(rel, rows["added"])
        for rel in ("api/app.py", "ingest/publish_manifest.py", "ops/p5_readiness.py",
                    "ops/p5s8_evidence.py", "store/block_manifest.py",
                    "store/segmented_cube.py"):
            with self.subTest("modified", rel=rel):
                self.assertIn(rel, text)
                self.assertIn(rel, rows["modified"])

    def test_the_document_and_git_agree_that_nothing_is_deleted(self):
        self.assertEqual(dc.changed_files(rd.CURRENT_DEPLOY_SHA, "HEAD")["deleted"], [])
        self.assertIn("Deleted — none", self.doc())


# =====================================================================================
class TestEachHalfOfTheHostingRuleIsReachedOnItsOwn(unittest.TestCase):
    """`can_host_the_operation` is a conjunction, and `708058e` fails BOTH halves.

    That made the field untestable against the current deploy commit: absent and differing are
    each non-empty there, so dropping either half still reports `False` and every assertion
    still passed. Mutation testing found it — two mutants survived that weakened the rule to
    one half. These are the commit pairs that separate them, chosen because each exhibits
    exactly one failure mode, so the half under test is the only thing that can be the reason.

    The same shape recurs below: a field whose two possible values were never both exercised is
    a field the suite was not really checking."""

    C_3223FBA = "3223fba37da5d292ea6acb58ccef0da2b5138d31"
    C_708058E = "708058e15b37a02e1d7aa5a66b9978c6e13bd446"
    C_2AB737F = "2ab737f10fa73d2adca103ff7f2dafc86a29b294"

    def row(self, candidate, baseline):
        matrix = dc.compatibility_matrix(candidate, baselines=(baseline,))
        return list(matrix["baselines"].values())[0]

    def test_absent_modules_alone_prevent_hosting(self):
        """`3223fba` -> `708058e`: six operation modules are absent and NONE of the present
        ones differ. Only the `not absent` half can produce the refusal."""
        row = self.row(self.C_708058E, self.C_3223FBA)
        self.assertEqual(row["operation_modules_differing"], [])
        self.assertEqual(len(row["operation_modules_absent"]), 6)
        self.assertFalse(row["can_host_the_operation"])

    def test_differing_modules_alone_prevent_hosting(self):
        """`2ab737f` -> HEAD: every operation module is present and one differs. Only the
        `not differs` half can produce the refusal."""
        row = self.row("HEAD", self.C_2AB737F)
        self.assertEqual(row["operation_modules_absent"], [])
        self.assertEqual(row["operation_modules_differing"],
                         ["ops/p5_oneshot_compaction.py"])
        self.assertFalse(row["can_host_the_operation"])

    def test_a_commit_identical_on_every_operation_module_can_host(self):
        """The positive control for the conjunction: neither half fires."""
        row = self.row("HEAD", "HEAD")
        self.assertEqual(row["operation_modules_absent"], [])
        self.assertEqual(row["operation_modules_differing"], [])
        self.assertTrue(row["can_host_the_operation"])
        self.assertIsNone(row["why_not"])

    def test_an_approved_candidate_is_reported_as_approved(self):
        """The other value of `candidate_is_approved`. Every earlier assertion used a candidate
        that is NOT approved, so a hardcoded `False` passed them all — and a package that
        always says 'not approved' would be useless the day one is."""
        self.assertTrue(dc.compatibility_matrix(self.C_708058E)["candidate_is_approved"])
        self.assertFalse(dc.compatibility_matrix("HEAD")["candidate_is_approved"])

    def test_deletions_are_classified_as_deletions(self):
        """No commit in this history deletes a dev-tree file, so `deleted` was always empty and
        misfiling a deletion as `other` changed nothing observable. Reversing the diff produces
        genuine `D` statuses from git and exercises the branch."""
        rows = dc.changed_files("HEAD", self.C_708058E)
        self.assertEqual(rows["other"], [])
        self.assertEqual(rows["added"], [])
        self.assertIn("ops/p5_oneshot_compaction.py", rows["deleted"])
        # Derived, not hardcoded: reversing a diff turns every addition into a deletion, so the
        # two must correspond exactly. A literal count here would go stale on the next commit —
        # which is how this test first failed, one commit after it was written.
        forward = dc.changed_files(self.C_708058E, "HEAD")
        self.assertEqual(rows["deleted"], forward["added"])
        self.assertEqual(rows["modified"], forward["modified"])
        self.assertEqual(rows["total"], len(rows["deleted"]) + len(rows["modified"]))
