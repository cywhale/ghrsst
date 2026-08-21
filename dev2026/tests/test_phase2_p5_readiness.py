"""dev2026 — P5 compaction DEPLOYMENT READINESS, adjudicated from a real target.

The question this answers is narrow and operational: *may a first P5 compaction be run against
this deployment?* Everything shipped so far answers a different one. `p5s8` measured a legacy
monolith; the whole P5 store layer is tested against `tempfile` trees. Neither says a thing
about whether VM24 has a manifest, a WAL, an anchor or a lock — because VM24 has none of them.

So the contract here is a refusal engine, not a checklist:

- A requirement is `READY` only when a checker looked at the actual target and found the thing.
- Evidence carrying a non-qualifying label — `LOCAL_SYNTHETIC`, the legacy S8 baseline, a model
  — can never produce `READY`. It reports `NOT_ADJUDICATED` and says which label stopped it.
  This is the specific mistake the phase has been correcting for weeks: a measurement of one
  thing quoted as a verdict on another.
- Anything needing a person is `NEEDS_HUMAN_APPROVAL`, which is not a failure and not a pass.

These tests are written before the module and fail against an empty checkout.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
for p in (_DEV, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import p5_fixtures as fx  # noqa: E402
from ingest.build_block import build_block  # noqa: E402
from ops import p5_readiness as rd  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store import repair_wal as rw  # noqa: E402

ANCHOR = "2026-06-27"
S = 90
NY = NX = 16


def _fresh_capacity(free_gib=1263.8, run_id="20260821T000000Z"):
    """A capacity reading shaped like the generated evidence, not a constant in this file."""
    return {
        "label": "VM24_PRODUCTION_READ_ONLY",
        "run_id": run_id,
        "measured_utc": "2026-08-21T00:00:00.000000+00:00",
        "free_now_gib": free_gib,
        "minimum_free_gib": 403.3,
        "recommended_free_gib": 600.0,
        "hard_reserve_gib": 250,
    }


class _TargetBase(unittest.TestCase):
    """A real, isolated P5 root: block, manifest, WAL, anchor on a separate root."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p5-readiness-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = os.path.join(self.tmp, "data")
        self.manifest_root = os.path.join(self.root, "p5_blocks")
        self.wal_root = os.path.join(self.root, "p5_repairs")
        # Deliberately OUTSIDE the data root: the anchor is the rollback-domain witness, and an
        # anchor sharing the domain it witnesses witnesses nothing.
        self.anchor_root = os.path.join(self.tmp, "anchor")
        for d in (self.manifest_root, self.wal_root, self.anchor_root):
            os.makedirs(d, exist_ok=True)

        s0, e0 = bm.block_bounds(ANCHOR, S, 0)
        span = fx.calendar_span(s0, e0)
        self.days = span[:12]
        delta = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(delta, self.days, ny=NY, nx=NX)
        self.block_path = os.path.join(self.manifest_root, "block_v1.zarr")
        plan = build_block(self.block_path, start_day=s0, end_day=e0,
                           classification_target=list(self.days), delta_path=delta,
                           artifacts_dir=os.path.join(self.tmp, "artifacts"),
                           lock=None, hard_reserve_bytes=0, unsafe_skip_isolation=True)
        m = {"format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION,
             "generation": 1, "generation_id": "gen000001-readiness",
             "created_utc": "2026-08-21T00:00:00Z", "created_by": "p5-readiness-tests",
             "predecessor_generation": None, "predecessor_manifest": None,
             "grid": {"ny": NY, "nx": NX, "region": [0, NY, 0, NX]},
             "variables": list(fx.VARS),
             "block_grid": {"anchor_day": ANCHOR, "block_days": S},
             "segments": [dict(plan["segment"])], "superseded": [], "manifest_checksum": ""}
        m["manifest_checksum"] = bm.compute_checksum(m)
        bm.publish(self.manifest_root, m)

        rw.declare_anchor_domain(self.anchor_root, domain_id="p5-readiness-test",
                                 at_utc="2026-08-21T00:00:00Z", operator="tests",
                                 allow_same_filesystem=True)
        rw.initialize_wal(self.wal_root, manifest_root=self.manifest_root,
                          at_utc="2026-08-21T00:00:01Z", operator="tests",
                          anchor_root=self.anchor_root)

    def target(self, **over):
        base = {
            "label": "VM24_PRODUCTION_READ_ONLY",
            # The fixture's paths are on THIS machine, so this is the host that owns them.
            # Filesystem-backed checks are adjudicated only where their paths live.
            "host": socket.gethostname(),
            "root": self.root,
            "manifest_root": self.manifest_root,
            "wal_root": self.wal_root,
            "anchor_root": self.anchor_root,
            "identity": {"reviewed_sha": "a" * 40, "head_sha": "a" * 40,
                         "worktree_clean": True,
                         "modules": {"store/block_manifest.py": "0" * 64}},
            "capacity": _fresh_capacity(),
            "locks": {"compaction": os.path.join(self.root, "p5_compaction.lock"),
                      "ingest": os.path.join(self.root, "ingest.lock")},
            "api_parity": {"checked": True, "route": "segmented", "mismatches": 0,
                           "samples": 32},
        }
        base.update(over)
        return base


class TestTheVerdictVocabulary(unittest.TestCase):

    def test_the_four_verdicts_exist_and_are_distinct(self):
        self.assertEqual(len(set(rd.VERDICTS)), 4)
        for v in (rd.READY, rd.BLOCKED, rd.NOT_ADJUDICATED, rd.NEEDS_APPROVAL):
            with self.subTest(v):
                self.assertIn(v, rd.VERDICTS)

    def test_there_is_no_pass_verdict(self):
        """`PASS` is the word an S8 baseline or a synthetic run would be mislabelled with."""
        for v in rd.VERDICTS:
            with self.subTest(v):
                self.assertNotIn("PASS", v)

    def test_every_requirement_declares_why_it_blocks_compaction(self):
        self.assertTrue(rd.REQUIREMENTS)
        for req in rd.REQUIREMENTS:
            with self.subTest(req["id"]):
                self.assertTrue(req["title"])
                self.assertTrue(req["why"])
                self.assertIn("evidence_kind", req)

    def test_the_requirements_cover_the_declared_contract(self):
        ids = {r["id"] for r in rd.REQUIREMENTS}
        for needed in ("identity", "manifest", "blocks", "wal_anchor", "locks",
                       "api_parity", "capacity", "failure_semantics"):
            with self.subTest(needed):
                self.assertIn(needed, ids)


class TestAWellFormedTargetIsAdjudicated(_TargetBase):

    def test_a_complete_isolated_target_reaches_ready_on_the_store_requirements(self):
        report = rd.assess(self.target())
        by_id = {r["id"]: r for r in report["requirements"]}
        for rid in ("manifest", "blocks", "wal_anchor", "capacity"):
            with self.subTest(rid):
                self.assertEqual(by_id[rid]["verdict"], rd.READY, by_id[rid]["reason"])

    def test_the_report_is_machine_readable_and_round_trips(self):
        report = rd.assess(self.target())
        self.assertEqual(json.loads(json.dumps(report, sort_keys=True)), report)
        self.assertEqual(sorted(report["summary"]), sorted(rd.VERDICTS))
        listed = sum(len(v) for v in report["summary"].values())
        self.assertEqual(listed, len(report["requirements"]))

    def test_readiness_requires_every_requirement_to_be_ready(self):
        report = rd.assess(self.target())
        expected = all(r["verdict"] == rd.READY for r in report["requirements"])
        self.assertIs(report["compaction_ready"], expected)

    def test_the_report_answers_what_must_happen_before_the_first_compaction(self):
        report = rd.assess(self.target())
        self.assertTrue(report["next_safe_step"])
        self.assertTrue(report["before_first_compaction"])


class TestNonQualifyingEvidenceCanNeverReachReady(_TargetBase):
    """The mistake this whole phase has been correcting, in its final form: a measurement of
    one thing quoted as a verdict on another."""

    def test_a_local_synthetic_target_is_not_adjudicated(self):
        report = rd.assess(self.target(label="LOCAL_SYNTHETIC"))
        self.assertIs(report["compaction_ready"], False)
        for req in report["requirements"]:
            with self.subTest(req["id"]):
                self.assertNotEqual(req["verdict"], rd.READY)

    def test_the_reason_names_the_label_that_stopped_it(self):
        report = rd.assess(self.target(label="LOCAL_SYNTHETIC"))
        by_id = {r["id"]: r for r in report["requirements"]}
        self.assertIn("LOCAL_SYNTHETIC", by_id["manifest"]["reason"])

    def test_the_legacy_s8_baseline_is_not_a_p5_compaction_pass(self):
        report = rd.assess(self.target(label="LEGACY_BASELINE"))
        self.assertIs(report["compaction_ready"], False)
        self.assertEqual(report["summary"][rd.READY], [])

    def test_every_non_qualifying_label_is_refused(self):
        for label in rd.NON_QUALIFYING_LABELS:
            with self.subTest(label):
                report = rd.assess(self.target(label=label))
                self.assertIs(report["compaction_ready"], False)

    def test_a_qualifying_label_is_still_adjudicated_on_the_evidence(self):
        """The control: refusing bad labels must not mean refusing everything."""
        report = rd.assess(self.target())
        self.assertTrue(report["summary"][rd.READY])


class TestTheStoreRequirementsInspectTheTarget(_TargetBase):

    def test_a_missing_manifest_root_is_blocked_not_absent(self):
        report = rd.assess(self.target(manifest_root=os.path.join(self.tmp, "nope")))
        by_id = {r["id"]: r for r in report["requirements"]}
        self.assertEqual(by_id["manifest"]["verdict"], rd.BLOCKED)
        self.assertIn("nope", by_id["manifest"]["reason"])

    def test_a_manifest_with_no_p5_roots_at_all_is_blocked(self):
        """The state VM24 is actually in: a tiered deployment and nothing P5."""
        bare = os.path.join(self.tmp, "bare")
        os.makedirs(bare, exist_ok=True)
        report = rd.assess(self.target(root=bare, manifest_root=os.path.join(bare, "p5_blocks"),
                                       wal_root=os.path.join(bare, "p5_repairs"),
                                       anchor_root=os.path.join(bare, "anchor")))
        by_id = {r["id"]: r for r in report["requirements"]}
        for rid in ("manifest", "blocks", "wal_anchor"):
            with self.subTest(rid):
                self.assertEqual(by_id[rid]["verdict"], rd.BLOCKED)
        self.assertIs(report["compaction_ready"], False)

    def test_a_referenced_block_that_is_missing_is_blocked(self):
        shutil.rmtree(self.block_path)
        by_id = {r["id"]: r for r in rd.assess(self.target())["requirements"]}
        self.assertEqual(by_id["blocks"]["verdict"], rd.BLOCKED)

    def test_an_uninitialized_wal_is_blocked(self):
        shutil.rmtree(self.wal_root)
        os.makedirs(self.wal_root, exist_ok=True)
        by_id = {r["id"]: r for r in rd.assess(self.target())["requirements"]}
        self.assertEqual(by_id["wal_anchor"]["verdict"], rd.BLOCKED)

    def test_an_undeclared_anchor_domain_is_blocked_by_the_shipped_resolver(self):
        """Deliberately not by a check of our own.

        There WAS one here -- `anchor_domain_id(...) is None` -- and mutation testing deleted it
        with every test still green, because `resolve_anchor_root` refuses an undeclared domain
        first. The duplicate was removed rather than the test strengthened: two copies of one
        rule is the second truth this module exists to avoid, and the copy is what goes stale.
        This asserts the refusal arrives, and that it arrives from the authority."""
        os.remove(os.path.join(self.anchor_root, rw.ANCHOR_DOMAIN_NAME))
        by_id = {r["id"]: r for r in rd.assess(self.target())["requirements"]}
        reason = by_id["wal_anchor"]["reason"]
        self.assertEqual(by_id["wal_anchor"]["verdict"], rd.BLOCKED)
        self.assertIn("rollback domain", reason.lower())
        self.assertIn("shipped resolver", reason)

    def test_a_segment_without_build_provenance_is_blocked(self):
        """`build_provenance` is OPTIONAL in the manifest schema, so a segment can be perfectly
        valid and still record nothing about which sources produced its bytes. For a compaction
        that is the difference between a block and an unattributable pile of numbers -- and no
        test covered it until mutation testing deleted the check and everything stayed green."""
        live_path = os.path.join(self.manifest_root, bm.LIVE_NAME)
        with open(live_path) as fh:
            live = json.load(fh)
        self.assertIn("build_provenance", live["segments"][0],
                      "the fixture never had provenance; the test would prove nothing")
        live["segments"][0].pop("build_provenance")
        live["manifest_checksum"] = ""
        live["manifest_checksum"] = bm.compute_checksum(live)
        bm.validate_manifest(live)          # still a VALID manifest -- that is the point
        with open(live_path, "w") as fh:
            json.dump(live, fh)

        by_id = {r["id"]: r for r in rd.assess(self.target())["requirements"]}
        self.assertEqual(by_id["manifest"]["verdict"], rd.READY,
                         "the manifest itself is valid; only the block check should object")
        self.assertEqual(by_id["blocks"]["verdict"], rd.BLOCKED)
        self.assertIn("build_provenance", by_id["blocks"]["reason"])

    def test_the_module_keeps_no_copy_of_the_anchor_domain_rule(self):
        with open(os.path.join(_DEV, "ops", "p5_readiness.py")) as fh:
            source = fh.read()
        self.assertNotIn("anchor_domain_id(anchor_root) is None", source)

    def test_the_initialization_record_poisons_the_empty_day(self):
        """A wart in shipped `repair_wal`, pinned here rather than papered over.

        `_replay` treats any non-INTENT record without a matching intent as a terminal orphan
        and poisons its `day`. The log's own `wal_initialized` record carries `day == ""`, so a
        freshly initialized, entirely healthy WAL reports `poisoned_days == {""}`. No real day
        is named and the prune guard is keyed by real day, so nothing downstream is harmed --
        but any health check reading "poisoned_days is non-empty" as unhealthy would call every
        correct WAL broken, which is exactly what happened here.

        Not fixed in this phase: correcting the replay changes signed-off P5-S5 WAL semantics.
        If it IS corrected, this test fails and `_real_days` can be deleted."""
        state = rw.read_wal(self.wal_root, anchor_root=self.anchor_root)
        self.assertEqual(set(state.poisoned_days), {""},
                         "the replay wart changed; revisit ops/p5_readiness._real_days")
        self.assertEqual({d for d in state.poisoned_days if d}, set(),
                         "a REAL day is poisoned, which is a genuine problem")

    def test_a_genuinely_poisoned_real_day_is_still_blocked(self):
        """The filter must not swallow the thing it is filtering around."""
        verdict, reason = rd._check_wal_anchor(self.target())
        self.assertEqual(verdict, rd.READY, reason)
        self.assertEqual(rd._real_days({"", "2026-06-27"}), {"2026-06-27"})
        self.assertEqual(rd._real_days({""}), set())

    def test_the_checkers_use_the_shipped_modules_rather_than_a_second_truth(self):
        """A readiness module that re-implements manifest validation would pass a manifest the
        store would refuse. It must ask the same code the store asks."""
        with open(os.path.join(_DEV, "ops", "p5_readiness.py")) as fh:
            source = fh.read()
        for module in ("block_manifest", "repair_wal"):
            with self.subTest(module):
                self.assertIn(module, source)
        for forbidden in ("MANIFEST_FORMAT =", "def validate_manifest", "WAL_NAME ="):
            with self.subTest(forbidden):
                self.assertNotIn(forbidden, source)


class TestIdentityAndCapacity(_TargetBase):

    def test_a_sha_mismatch_is_blocked(self):
        by_id = {r["id"]: r for r in rd.assess(self.target(
            identity={"reviewed_sha": "a" * 40, "head_sha": "b" * 40,
                      "worktree_clean": True, "modules": {}}))["requirements"]}
        self.assertEqual(by_id["identity"]["verdict"], rd.BLOCKED)

    def test_a_dirty_worktree_is_blocked(self):
        by_id = {r["id"]: r for r in rd.assess(self.target(
            identity={"reviewed_sha": "a" * 40, "head_sha": "a" * 40,
                      "worktree_clean": False, "modules": {}}))["requirements"]}
        self.assertEqual(by_id["identity"]["verdict"], rd.BLOCKED)

    def test_capacity_below_the_minimum_is_blocked(self):
        by_id = {r["id"]: r for r in rd.assess(self.target(
            capacity=_fresh_capacity(free_gib=295.7)))["requirements"]}
        self.assertEqual(by_id["capacity"]["verdict"], rd.BLOCKED)
        self.assertIn("403.3", by_id["capacity"]["reason"])

    def test_capacity_without_a_run_id_is_not_adjudicated(self):
        """A figure with no run behind it is a number in a file, not a measurement."""
        cap = _fresh_capacity()
        cap.pop("run_id")
        by_id = {r["id"]: r for r in rd.assess(self.target(capacity=cap))["requirements"]}
        self.assertEqual(by_id["capacity"]["verdict"], rd.NOT_ADJUDICATED)

    def test_the_module_hardcodes_no_capacity_figure(self):
        """295.7 was the reading two runs ago. Nothing here may carry one."""
        with open(os.path.join(_DEV, "ops", "p5_readiness.py")) as fh:
            source = fh.read()
        for stale in ("295.7", "267", "264.4", "1263.8"):
            with self.subTest(stale):
                self.assertNotIn(stale, source)


class TestAFilesystemCheckOnlyCountsOnItsOwnHost(_TargetBase):
    """The first real assessment reported `locks` BLOCKED because "the compaction lock
    directory does not exist" -- and it does exist, on VM24. The check ran on the auditor's
    laptop and reported on the wrong machine.

    Every other filesystem verdict in that run was right by coincidence: the P5 roots are
    absent on VM24 AND absent locally, so `os.path.isdir` gave the correct answer for the wrong
    reason. A checker that can be right by accident will eventually be wrong by accident.

    So a target names the host it was collected from, and filesystem-backed requirements are
    `NOT_ADJUDICATED` anywhere else. The only way to a READY is to run the assessment on the
    host the target describes."""

    FS_BACKED = ("manifest", "blocks", "wal_anchor", "locks")

    def test_a_target_from_another_host_is_not_adjudicated_on_filesystem_facts(self):
        report = rd.assess(self.target(host="some-other-host"))
        by_id = {r["id"]: r for r in report["requirements"]}
        for rid in self.FS_BACKED:
            with self.subTest(rid):
                self.assertEqual(by_id[rid]["verdict"], rd.NOT_ADJUDICATED)
                self.assertIn("some-other-host", by_id[rid]["reason"])

    def test_the_reason_says_the_check_would_have_read_the_wrong_machine(self):
        by_id = {r["id"]: r for r in rd.assess(self.target(host="elsewhere"))["requirements"]}
        self.assertIn("host", by_id["locks"]["reason"].lower())

    def test_host_free_requirements_are_still_adjudicated_off_host(self):
        """Identity and capacity are facts the collection carries, not paths to stat."""
        by_id = {r["id"]: r for r in rd.assess(self.target(host="elsewhere"))["requirements"]}
        self.assertEqual(by_id["capacity"]["verdict"], rd.READY)
        self.assertEqual(by_id["identity"]["verdict"], rd.READY)

    def test_a_target_with_no_host_is_not_adjudicated_either(self):
        by_id = {r["id"]: r for r in rd.assess(self.target(host=None))["requirements"]}
        self.assertEqual(by_id["manifest"]["verdict"], rd.NOT_ADJUDICATED)

    def test_the_matching_host_control_still_reaches_ready(self):
        """Guarding the guard: this must not become a blanket refusal."""
        by_id = {r["id"]: r for r in rd.assess(self.target())["requirements"]}
        for rid in ("manifest", "blocks", "wal_anchor"):
            with self.subTest(rid):
                self.assertEqual(by_id[rid]["verdict"], rd.READY, by_id[rid]["reason"])


class TestWhatNeedsAPerson(_TargetBase):

    def test_live_deployment_switchover_needs_approval_and_is_never_ready(self):
        report = rd.assess(self.target())
        by_id = {r["id"]: r for r in report["requirements"]}
        self.assertEqual(by_id["api_parity"]["verdict"], rd.NEEDS_APPROVAL)

    def test_approval_items_are_listed_separately_from_blockers(self):
        report = rd.assess(self.target())
        self.assertTrue(report["summary"][rd.NEEDS_APPROVAL])
        overlap = set(report["summary"][rd.NEEDS_APPROVAL]) & set(report["summary"][rd.BLOCKED])
        self.assertEqual(overlap, set())

    def test_needing_approval_still_blocks_readiness(self):
        report = rd.assess(self.target())
        self.assertIs(report["compaction_ready"], False)


class TestTheCommittedRunAndItsDocument(unittest.TestCase):
    """The artifact is the authority; the document is checked against it.

    Every drift this phase corrected had the same shape -- a figure written into prose and left
    behind when the measurement moved. So the verdicts in the results document are read back out
    of the committed artifact rather than trusted."""

    ARTIFACT = os.path.join(_DEV, "bench", "results", "p5_readiness.json")
    DOC = os.path.join(_DEV, "specs", "p5_deployment_readiness.md")

    def setUp(self):
        with open(self.ARTIFACT) as fh:
            self.report = json.load(fh)
        with open(self.DOC) as fh:
            self.doc = fh.read()
        self.flat = " ".join(self.doc.split())

    def test_the_artifact_was_generated_on_the_host_it_describes(self):
        self.assertIs(self.report["adjudicated_on_host"], True)
        self.assertEqual(self.report["provenance"]["host"], "odb24")
        self.assertTrue(self.report["provenance"]["run_id"])

    def test_the_committed_run_is_not_ready_and_says_so(self):
        self.assertIs(self.report["compaction_ready"], False)
        self.assertIn("compaction_ready = false", self.flat)

    def test_every_verdict_in_the_document_matches_the_artifact(self):
        for req in self.report["requirements"]:
            with self.subTest(req["id"]):
                self.assertIn(f"`{req['id']}`", self.flat,
                              "the document does not mention this requirement")
                row_start = self.flat.index(f"| `{req['id']}` |")
                row = self.flat[row_start:row_start + 220]
                self.assertIn(req["verdict"], row,
                              f"the document reports a different verdict for {req['id']}")

    def test_the_counts_in_the_opening_line_match(self):
        s = self.report["summary"]
        self.assertIn(f"{len(s[rd.BLOCKED])} requirements are `BLOCKED`", self.flat)
        self.assertIn(f"{len(s[rd.READY])} are `READY`", self.flat)

    def test_the_document_records_no_gate_as_passed(self):
        for claim in ("G18 PASS", "S7 gate PASS", "compaction PASS", "gate passed"):
            with self.subTest(claim):
                self.assertNotIn(claim.lower(), self.flat.lower())
        self.assertIn("NOT MET", self.doc)

    def test_the_capacity_figure_is_read_from_the_artifact_not_typed(self):
        free = self.report["observed"]["capacity"]["free_now_gib"]
        self.assertIn(f"{free} GiB", self.flat)

    def test_the_document_states_the_unreviewed_tool_caveat(self):
        """The report was produced by a module that is not in the reviewed commit. Saying so is
        the difference between evidence and a claim about evidence."""
        self.assertIn("has not been reviewed", self.flat)


if __name__ == "__main__":
    unittest.main()
