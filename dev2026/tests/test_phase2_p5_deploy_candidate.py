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
