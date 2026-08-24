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
_APPROVED = rd.REVIEWED_DEPLOY_SHAS[0]

_ASSESSOR_REPO = None


def approved_assessor_repo():
    """A real git repository holding THIS assessor's modules, committed and approved.

    Not a stub: `assessor_is_approved()` runs its whole comparison against it, so the fixtures
    exercise the check positively rather than switching it off. Built once -- copying four
    modules and running `git init` per test would dominate the suite."""
    global _ASSESSOR_REPO
    if _ASSESSOR_REPO is None:
        import subprocess
        root = tempfile.mkdtemp(prefix="p5-approved-assessor-")
        for rel in rd.ASSESSOR_MODULES:
            dst = os.path.join(root, "dev2026", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy(os.path.join(_DEV, rel), dst)
        for args in (("init", "-q"), ("config", "user.email", "t@example.invalid"),
                     ("config", "user.name", "t"), ("add", "-A"),
                     ("commit", "-qm", "approved assessor")):
            subprocess.run(("git",) + args, cwd=root, check=True, capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root,
                              capture_output=True, text=True).stdout.strip()
        _ASSESSOR_REPO = (root, head)
    return _ASSESSOR_REPO


def build_probe_workspace(case):
    """Everything a complete `assess()` needs, built OUTSIDE the process that measures.

    A real published manifest and block, a real WAL and anchor, a real evidence repository with
    a committed executed-result artifact, and an approved assessor build. Written to a config
    file the probe reads, so the measuring interpreter imports nothing but the readiness path.
    Building a manifest and a WAL here imports `ingest/build_block.py` and friends; counting
    those would pad the inventory with modules the assessor never runs."""
    import hashlib
    import subprocess

    tmp = tempfile.mkdtemp(prefix="p5-closure-ws-")
    case.addCleanup(shutil.rmtree, tmp, ignore_errors=True)

    root = os.path.join(tmp, "data")
    manifest_root = os.path.join(root, "p5_blocks")
    wal_root = os.path.join(root, "p5_repairs")
    anchor_root = os.path.join(tmp, "anchor")
    for d in (manifest_root, wal_root, anchor_root):
        os.makedirs(d, exist_ok=True)
    s0, e0 = bm.block_bounds(ANCHOR, S, 0)
    days = fx.calendar_span(s0, e0)[:8]
    delta = os.path.join(tmp, "delta.zarr")
    fx.build_delta(delta, days, ny=NY, nx=NX)
    plan = build_block(os.path.join(manifest_root, "block_v1.zarr"), start_day=s0, end_day=e0,
                       classification_target=list(days), delta_path=delta,
                       artifacts_dir=os.path.join(tmp, "artifacts"), lock=None,
                       hard_reserve_bytes=0, unsafe_skip_isolation=True)
    m = {"format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION, "generation": 1,
         "generation_id": "gen000001-closure", "created_utc": "2026-08-23T00:00:00Z",
         "created_by": "closure-probe", "predecessor_generation": None,
         "predecessor_manifest": None,
         "grid": {"ny": NY, "nx": NX, "region": [0, NY, 0, NX]},
         "variables": list(fx.VARS), "block_grid": {"anchor_day": ANCHOR, "block_days": S},
         "segments": [dict(plan["segment"])], "superseded": [], "manifest_checksum": ""}
    m["manifest_checksum"] = bm.compute_checksum(m)
    bm.publish(manifest_root, m)
    rw.declare_anchor_domain(anchor_root, domain_id="closure-probe",
                             at_utc="2026-08-23T00:00:00Z", operator="tests",
                             allow_same_filesystem=True)
    rw.initialize_wal(wal_root, manifest_root=manifest_root, at_utc="2026-08-23T00:00:01Z",
                      operator="tests", anchor_root=anchor_root)

    repo = os.path.join(tmp, "repo")
    dev = os.path.join(repo, "dev2026")
    execs = os.path.join(dev, rd.EXECUTIONS_REL)
    os.makedirs(execs)
    suite_rel = "tests/test_closure_probe_suite.py"
    suite_path = os.path.join(dev, suite_rel)
    os.makedirs(os.path.dirname(suite_path), exist_ok=True)
    with open(suite_path, "w") as fh:
        fh.write("# the suite whose run is recorded\n")
    with open(suite_path, "rb") as fh:
        suite_digest = hashlib.sha256(fh.read()).hexdigest()

    def git(*a):
        subprocess.run(("git",) + a, cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.email", "t@example.invalid")
    git("config", "user.name", "t")
    git("add", "-A")
    git("commit", "-qm", "the code under test")
    deploy_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                capture_output=True, text=True).stdout.strip()
    with open(os.path.join(execs, "run.json"), "w") as fh:
        json.dump({"suite": "tests.test_closure_probe_suite", "deploy_sha": deploy_sha,
                   "command": ".venv/bin/python -m unittest tests.test_closure_probe_suite",
                   "exit_code": 0, "tests_run": 17, "ran_utc": "2026-08-23T10:00:00Z",
                   "source_digest": suite_digest}, fh)
    git("add", "-A")
    git("commit", "-qm", "record the run")
    evidence_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                  capture_output=True, text=True).stdout.strip()

    assessor_root, assessor_head = approved_assessor_repo()
    now = _now_iso()
    target = {
        "label": rd.QUALIFYING_LABEL, "host": socket.gethostname(),
        "run_id": "CLOSURE_RUN", "collected_utc": now,
        "root": root, "manifest_root": manifest_root, "wal_root": wal_root,
        "anchor_root": anchor_root,
        "identity": {"reviewed_sha": deploy_sha, "head_sha": deploy_sha,
                     "worktree_clean": True, "modules": {suite_rel: suite_digest}},
        "capacity": {"label": rd.QUALIFYING_LABEL, "run_id": "CLOSURE_RUN",
                     "measured_utc": now, "free_now_gib": 1.0,
                     "minimum_free_gib": 0.001, "recommended_free_gib": 0.001,
                     "hard_reserve_gib": 250, "data_root": root,
                     "filesystem_dev": os.stat(root).st_dev},
        "locks": {"compaction": os.path.join(root, "p5_compaction.lock"),
                  "ingest": os.path.join(root, "ingest.lock"),
                  "acquire_probe": {"acquired": True, "released": True,
                                    "path": os.path.join(root, "p5_compaction.lock"),
                                    "run_id": "CLOSURE_RUN", "host": socket.gethostname(),
                                    "at_utc": now, "actor": "closure-probe"}},
        "failure_semantics_proven_by": "run.json",
        "failure_semantics_evidence_commit": evidence_sha,
    }
    cfg = {
        "dev": _DEV,
        "overrides": {
            "REPO_ROOT": repo, "DEV_PREFIX": "dev2026",
            "REVIEWED_DEPLOY_SHAS": [deploy_sha, evidence_sha],
            "FAILURE_SEMANTICS_SUITES": ["tests.test_closure_probe_suite"],
            "INVENTORY_MODULES": [suite_rel],
            "ASSESSOR_REPO_ROOT": assessor_root,
            "APPROVED_ASSESSOR_SHAS": [assessor_head],
        },
        "target": target,
        "canonical_minimum_gib": 0.001,
    }
    cfg_path = os.path.join(tmp, "probe_config.json")
    with open(cfg_path, "w") as fh:
        json.dump(cfg, fh)
    return cfg_path


class _ApprovedAssessorMixin:
    """Point the assessor's self-check at an approved build of its own modules."""

    def _approve_assessor(self):
        root, head = approved_assessor_repo()
        for name, value in (("ASSESSOR_REPO_ROOT", root),
                            ("APPROVED_ASSESSOR_SHAS", (head,))):
            self.addCleanup(setattr, rd, name, getattr(rd, name))
            setattr(rd, name, value)
        ok, why = rd.assessor_is_approved()
        assert ok, f"the fixture's own assessor approval failed: {why}"


def _now_iso():
    """A just-taken timestamp. The fixture stands for a collection that happened now, because a
    capacity reading is only believable inside its freshness window."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


FIXTURE_RUN = "FIXTURE_RUN"


#: The fixture's threshold. The assessor RE-MEASURES free space with `statvfs`, so a fixture
#: carrying production's 403.3 GiB would be BLOCKED by whatever this machine's temp filesystem
#: happens to have -- testing the disk rather than the code. Tests about the threshold set
#: their own, relative to the live reading.
FIXTURE_MINIMUM_GIB = 0.001


def _fresh_capacity(free_gib=1263.8, run_id=FIXTURE_RUN, measured_utc=None,
                    data_root=None, filesystem_dev=None,
                    minimum_free_gib=FIXTURE_MINIMUM_GIB):
    """A capacity reading shaped like the generated evidence, not a constant in this file."""
    extra = {}
    if data_root is not None:
        extra["data_root"] = data_root
    if filesystem_dev is not None:
        extra["filesystem_dev"] = filesystem_dev
    return {
        **extra,
        "label": "VM24_PRODUCTION_READ_ONLY",
        "run_id": run_id,
        "measured_utc": measured_utc or _now_iso(),
        "free_now_gib": free_gib,
        "minimum_free_gib": minimum_free_gib,
        "recommended_free_gib": minimum_free_gib,
        "hard_reserve_gib": 250,
    }


class _TargetBase(_ApprovedAssessorMixin, unittest.TestCase):
    """A real, isolated P5 root: block, manifest, WAL, anchor on a separate root."""

    def _set_canonical(self, minimum_gib):
        """Move the CANONICAL G18 threshold, restoring it afterwards.

        The threshold is policy now, so a fixture cannot lower it by writing a field. Tests
        that need a different bar move the policy -- which is also how "the gate follows the
        capacity model" is demonstrated rather than asserted."""
        from ops import p5s8_evidence as ev
        self.addCleanup(setattr, ev, "MINIMUM_FREE_GIB", ev.MINIMUM_FREE_GIB)
        ev.MINIMUM_FREE_GIB = minimum_gib

    def setUp(self):
        self._approve_assessor()
        self.collected_utc = _now_iso()
        # Production's 403.3 GiB against a temp filesystem would test this machine's disk
        # rather than the code, so the fixtures run against a bar they genuinely clear.
        self._set_canonical(FIXTURE_MINIMUM_GIB)
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
            # An APPROVED sha with the fingerprints the assessor recomputes for it, so the
            # well-formed fixture is one that genuinely satisfies the identity gate.
            "identity": {"reviewed_sha": _APPROVED, "head_sha": _APPROVED,
                         "worktree_clean": True,
                         "modules": rd.expected_module_digests(_APPROVED)},
            "run_id": FIXTURE_RUN,
            "collected_utc": self.collected_utc,
            # Bound to the device holding the target root: a free-space reading is a fact
            # about a device, and one from an unrelated volume proves nothing about this one.
            "capacity": _fresh_capacity(measured_utc=self.collected_utc,
                                        data_root=self.root,
                                        filesystem_dev=os.stat(self.root).st_dev),
            "locks": {"compaction": os.path.join(self.root, "p5_compaction.lock"),
                      "ingest": os.path.join(self.root, "ingest.lock"),
                      # Fully bound: path, run, host, time and actor. A probe missing any of
                      # them is refused, so a fixture carrying a bare one would silently make
                      # every other test in this file assert against a NOT_ADJUDICATED lock.
                      "acquire_probe": {
                          "acquired": True, "released": True,
                          "path": os.path.join(self.root, "p5_compaction.lock"),
                          "run_id": FIXTURE_RUN, "host": socket.gethostname(),
                          "at_utc": self.collected_utc, "actor": "tests"}},
            "api_parity": {"checked": True, "route": "segmented", "mismatches": 0,
                           "samples": 32},
        }
        base.update(over)
        return base


class TestAnUnapprovedAssessorDemotesRealFindings(_TargetBase):
    """The demotion branch on a target that would otherwise pass.

    `TestTheAssessorIsItselfBound` uses a bare target, so every requirement is refused by its
    own checker before the demotion is reached -- deleting the branch survived a mutation round
    against it. Here the fixture is complete and several requirements DO read READY with an
    approved assessor, so the branch is the only thing that can change them."""

    def test_the_same_target_flips_when_the_assessor_loses_approval(self):
        approved = rd.assess(self.target())
        was_ready = {r["id"] for r in approved["requirements"] if r["verdict"] == rd.READY}
        self.assertTrue(was_ready, "nothing was READY; the demotion would be unobservable")

        rd.APPROVED_ASSESSOR_SHAS = ()
        demoted = {r["id"]: r for r in rd.assess(self.target())["requirements"]}
        for rid in sorted(was_ready):
            with self.subTest(rid):
                self.assertEqual(demoted[rid]["verdict"], rd.NOT_ADJUDICATED)
                self.assertIn("assessor is not approved", demoted[rid]["reason"])

    def test_the_demoted_reason_keeps_what_the_checker_actually_found(self):
        """Demotion must not erase the finding: it says the finding cannot authorize, not that
        there was nothing to find."""
        approved = {r["id"]: r for r in rd.assess(self.target())["requirements"]}
        ready = next(r for r in approved.values() if r["verdict"] == rd.READY)
        rd.APPROVED_ASSESSOR_SHAS = ()
        demoted = {r["id"]: r for r in rd.assess(self.target())["requirements"]}[ready["id"]]
        self.assertIn("The check itself found", demoted["reason"])

    def test_readiness_is_false_and_the_report_says_why(self):
        rd.APPROVED_ASSESSOR_SHAS = ()
        report = rd.assess(self.target())
        self.assertIs(report["compaction_ready"], False)
        self.assertIs(report["assessor_approved"], False)
        self.assertEqual(report["summary"][rd.READY], [])


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
        state = rw.read_wal(self.wal_root, anchor_root=self.anchor_root)
        self.assertEqual(rd._real_days({"", "2026-06-27"}, state), {"2026-06-27"})
        self.assertEqual(rd._real_days({""}, state), set())

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
        st = os.statvfs(self.root)
        live = st.f_bavail * st.f_frsize / 1024 ** 3
        minimum = round(live + 1000.0, 1)
        self._set_canonical(minimum)
        by_id = {r["id"]: r for r in rd.assess(self.target(
            capacity=_fresh_capacity(measured_utc=self.collected_utc, data_root=self.root,
                                     filesystem_dev=os.stat(self.root).st_dev,
                                     minimum_free_gib=minimum)))["requirements"]}
        self.assertEqual(by_id["capacity"]["verdict"], rd.BLOCKED)
        self.assertIn(str(minimum), by_id["capacity"]["reason"])

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
        """Identity is a fact the collection carries; a SHA and a set of digests mean the same
        thing anywhere. Capacity is not on this list: free space is a fact about a device, and
        `st_dev` is meaningless on another machine -- so it moved to the host-bound side."""
        by_id = {r["id"]: r for r in rd.assess(self.target(host="elsewhere"))["requirements"]}
        self.assertEqual(by_id["identity"]["verdict"], rd.READY)
        self.assertEqual(by_id["capacity"]["verdict"], rd.NOT_ADJUDICATED)

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


class TestIdentityIsBoundToAnApprovedShaNotAClaimedOne(_TargetBase):
    """`reviewed_sha == head_sha` compares the target against itself.

    Both fields come from the same collected JSON, so anything that can write that file can
    declare its own commit reviewed. The approved set has to live where review governs it --
    in this module, changed only by a reviewed change -- and the module fingerprints have to be
    recomputed by the assessor from the approved commit, not accepted as reported."""

    def test_a_sha_outside_the_approved_set_is_blocked(self):
        ident = {"reviewed_sha": "c" * 40, "head_sha": "c" * 40,
                 "worktree_clean": True, "modules": {}}
        by_id = {r["id"]: r for r in rd.assess(self.target(identity=ident))["requirements"]}
        self.assertEqual(by_id["identity"]["verdict"], rd.BLOCKED)
        self.assertIn("approved", by_id["identity"]["reason"].lower())

    def test_the_approved_set_is_declared_in_the_module_not_the_target(self):
        self.assertTrue(rd.REVIEWED_DEPLOY_SHAS)
        for sha in rd.REVIEWED_DEPLOY_SHAS:
            with self.subTest(sha):
                self.assertEqual(len(sha), 40)

    def test_reported_module_hashes_are_recomputed_from_the_approved_commit(self):
        """A target reporting the right SHA and wrong bytes must not pass."""
        approved = rd.REVIEWED_DEPLOY_SHAS[0]
        expected = rd.expected_module_digests(approved)
        self.assertTrue(expected, "no digests could be recomputed; the check would be vacuous")
        tampered = {k: "0" * 64 for k in expected}
        ident = {"reviewed_sha": approved, "head_sha": approved,
                 "worktree_clean": True, "modules": tampered}
        by_id = {r["id"]: r for r in rd.assess(self.target(identity=ident))["requirements"]}
        self.assertEqual(by_id["identity"]["verdict"], rd.BLOCKED)
        self.assertIn("fingerprint", by_id["identity"]["reason"].lower())

    def test_a_faithful_inventory_at_the_approved_sha_is_ready(self):
        approved = rd.REVIEWED_DEPLOY_SHAS[0]
        ident = {"reviewed_sha": approved, "head_sha": approved,
                 "worktree_clean": True, "modules": rd.expected_module_digests(approved)}
        by_id = {r["id"]: r for r in rd.assess(self.target(identity=ident))["requirements"]}
        self.assertEqual(by_id["identity"]["verdict"], rd.READY, by_id["identity"]["reason"])

    def test_a_missing_module_is_blocked_not_skipped(self):
        approved = rd.REVIEWED_DEPLOY_SHAS[0]
        expected = dict(rd.expected_module_digests(approved))
        expected.pop(sorted(expected)[0])
        ident = {"reviewed_sha": approved, "head_sha": approved,
                 "worktree_clean": True, "modules": expected}
        by_id = {r["id"]: r for r in rd.assess(self.target(identity=ident))["requirements"]}
        self.assertEqual(by_id["identity"]["verdict"], rd.BLOCKED)


class TestCapacityEvidenceMustBeFreshAndBound(_TargetBase):
    """A free-space reading is true of a moment. Carrying one across a schedule tick, a restart
    or an overnight pause is the same error as quoting a superseded figure -- it just takes
    longer to become wrong."""

    NOW = "2026-08-21T13:10:00Z"

    def _cap(self, **over):
        cap = _fresh_capacity(data_root=self.root, filesystem_dev=os.stat(self.root).st_dev)
        cap.update(measured_utc="2026-08-21T13:04:22Z", run_id="R1")
        cap.update(over)
        return cap

    def _assess(self, *, now=NOW, **over):
        t = self.target(run_id="R1", collected_utc="2026-08-21T13:04:22Z", **over)
        return {r["id"]: r for r in rd.assess(t, now=now)["requirements"]}

    def test_a_fresh_bound_reading_is_ready(self):
        by_id = self._assess(capacity=self._cap())
        self.assertEqual(by_id["capacity"]["verdict"], rd.READY, by_id["capacity"]["reason"])

    def test_a_reading_older_than_the_window_is_not_adjudicated(self):
        by_id = self._assess(capacity=self._cap(), now="2026-08-22T09:00:00Z")
        self.assertEqual(by_id["capacity"]["verdict"], rd.NOT_ADJUDICATED)
        self.assertIn("expired", by_id["capacity"]["reason"].lower())

    def test_a_reading_from_a_different_run_is_not_adjudicated(self):
        by_id = self._assess(capacity=self._cap(run_id="SOME_OTHER_RUN"))
        self.assertEqual(by_id["capacity"]["verdict"], rd.NOT_ADJUDICATED)
        self.assertIn("run", by_id["capacity"]["reason"].lower())

    def test_a_reading_taken_far_from_the_collection_is_not_adjudicated(self):
        """Chosen to be FRESH but distant: 12:00Z is 70 minutes before `now`, inside the 2h
        expiry window, and 64 minutes from the 13:04Z collection, outside the skew window. An
        older timestamp would expire first and the skew check would never run -- which is
        exactly how deleting it survived a mutation round."""
        by_id = self._assess(capacity=self._cap(measured_utc="2026-08-21T12:00:00Z"))
        self.assertEqual(by_id["capacity"]["verdict"], rd.NOT_ADJUDICATED)
        reason = by_id["capacity"]["reason"]
        self.assertIn("from the collection that carries it", reason)
        self.assertNotIn("expired", reason, "the expiry check fired, not the skew check")

    def test_the_report_states_when_the_reading_expires(self):
        t = self.target(run_id="R1", collected_utc="2026-08-21T13:04:22Z",
                        capacity=self._cap())
        report = rd.assess(t, now=self.NOW)
        self.assertIn("capacity_expires_utc", report)
        self.assertTrue(report["capacity_expires_utc"])
        self.assertIn("MAX_CAPACITY_AGE_S", str(rd.MAX_CAPACITY_AGE_S)
                      and "MAX_CAPACITY_AGE_S")

    def test_the_window_is_short_enough_to_be_meaningful(self):
        """Long enough for one working session, short enough that a schedule tick invalidates
        it. A day-long window would let a reading survive every ingest window there is."""
        self.assertLessEqual(rd.MAX_CAPACITY_AGE_S, 3 * 3600)


class TestLocksAreNotAssumedAcquirable(_TargetBase):

    def test_an_absent_compaction_lock_with_no_probe_is_not_adjudicated(self):
        """`$G/p5_compaction.lock` does not exist on VM24. "The directory exists" is not
        evidence that a lock can be taken in it.

        The base fixture carries a successful probe, so it is removed here -- otherwise this
        would be testing the fixture rather than the checker."""
        locks = {k: v for k, v in self.target()["locks"].items() if k != "acquire_probe"}
        by_id = {r["id"]: r for r in rd.assess(self.target(locks=locks))["requirements"]}
        self.assertEqual(by_id["locks"]["verdict"], rd.NOT_ADJUDICATED)
        self.assertIn("probe", by_id["locks"]["reason"].lower())

    def test_a_recorded_successful_probe_is_ready(self):
        by_id = {r["id"]: r for r in rd.assess(self.target())["requirements"]}
        self.assertEqual(by_id["locks"]["verdict"], rd.READY, by_id["locks"]["reason"])

    def test_a_failed_probe_is_blocked(self):
        probe = dict(self.target()["locks"]["acquire_probe"],
                     acquired=False, released=False, error="Permission denied")
        locks = dict(self.target()["locks"], acquire_probe=probe)
        by_id = {r["id"]: r for r in rd.assess(self.target(locks=locks))["requirements"]}
        self.assertEqual(by_id["locks"]["verdict"], rd.BLOCKED)

    def test_two_paths_resolving_to_the_same_file_are_blocked(self):
        """Distinct strings are not distinct locks. A symlink makes them the same file, and two
        writers holding one lock each are excluded from nothing."""
        real = os.path.join(self.root, "one.lock")
        with open(real, "w") as fh:
            fh.write("")
        alias = os.path.join(self.root, "alias.lock")
        os.symlink(real, alias)
        locks = {"compaction": real, "ingest": alias,
                 "acquire_probe": dict(self.target()["locks"]["acquire_probe"], path=real)}
        by_id = {r["id"]: r for r in rd.assess(self.target(locks=locks))["requirements"]}
        self.assertEqual(by_id["locks"]["verdict"], rd.BLOCKED)
        self.assertIn("same file", by_id["locks"]["reason"].lower())

    def test_an_unwritable_lock_directory_is_blocked(self):
        ro = os.path.join(self.tmp, "readonly")
        os.makedirs(ro, exist_ok=True)
        os.chmod(ro, 0o500)
        self.addCleanup(os.chmod, ro, 0o700)
        locks = {"compaction": os.path.join(ro, "p5_compaction.lock"),
                 "ingest": os.path.join(self.root, "ingest.lock"),
                 "acquire_probe": dict(self.target()["locks"]["acquire_probe"],
                                       path=os.path.join(ro, "p5_compaction.lock"))}
        by_id = {r["id"]: r for r in rd.assess(self.target(locks=locks))["requirements"]}
        self.assertEqual(by_id["locks"]["verdict"], rd.BLOCKED)


class TestOnlyTheInitializationRecordMayBeIgnored(_TargetBase):
    """`_real_days` dropped EVERY empty-string day. That is broader than the wart it works
    around, and "no real day is affected today" is an observation, not a guarantee."""

    def test_the_initialization_record_is_the_only_thing_forgiven(self):
        state = rw.read_wal(self.wal_root, anchor_root=self.anchor_root)
        self.assertEqual(rd.ignorable_days(state), {""})

    def test_an_empty_day_from_anything_else_is_not_forgiven(self):
        """Fabricate a state whose empty day is not attributable to initialization."""
        class _S:
            records = ({"record": "repair_committed", "day": "", "repair_id": "x", "seq": 2},)
        self.assertEqual(rd.ignorable_days(_S()), set())

    def test_a_poisoned_real_day_still_blocks(self):
        class _S:
            records = ({"record": "wal_initialized", "day": "", "repair_id": "", "seq": 1},)
        self.assertEqual(rd.ignorable_days(_S()), {""})
        self.assertNotIn("2026-06-27", rd.ignorable_days(_S()))


class TestTheStoredCollectionDeclaresItsOwnExpiry(unittest.TestCase):
    """A readiness artifact is true of a moment. The last several rounds were all one defect --
    a figure outliving its measurement -- so this one states when it stops applying, and the
    document's timestamp is COMPUTED from the artifact rather than typed beside it."""

    TARGET = os.path.join(_DEV, "bench", "results", "p5_readiness_runs",
                          "20260821T130320Z", "target_live.json")
    DOC = os.path.join(_DEV, "specs", "p5_deployment_readiness.md")

    def setUp(self):
        self.target = rd.load_target(self.TARGET)
        with open(self.DOC) as fh:
            self.flat = " ".join(fh.read().split())

    def test_the_document_states_the_computed_expiry(self):
        expires = rd.capacity_expires_utc(self.target)
        self.assertTrue(expires)
        self.assertIn(expires, self.flat,
                      "the document's expiry is not the one the artifact implies")

    def test_the_document_requires_re_collection_before_acting(self):
        for phrase in ("Re-collect", "expires", "not evidence that authorizes a compaction"):
            with self.subTest(phrase):
                self.assertIn(phrase, self.flat)

    def test_the_stored_reading_cannot_be_lifted_into_another_run(self):
        """Run binding, exercised on the real collection rather than a fixture."""
        borrowed = json.loads(json.dumps(self.target))
        borrowed["run_id"] = "A_LATER_RUN"
        by_id = {r["id"]: r for r in rd.assess(borrowed)["requirements"]}
        self.assertEqual(by_id["capacity"]["verdict"], rd.NOT_ADJUDICATED)
        self.assertIn("different moments", by_id["capacity"]["reason"])

    def test_the_stored_reading_expires_against_a_later_clock(self):
        by_id = {r["id"]: r for r in rd.assess(self.target,
                                               now="2026-08-22T09:00:00Z")["requirements"]}
        self.assertEqual(by_id["capacity"]["verdict"], rd.NOT_ADJUDICATED)
        self.assertIn("expired", by_id["capacity"]["reason"])

    def test_the_stored_collection_is_not_ready_under_the_current_rules(self):
        report = rd.assess(self.target, now="2026-08-22T09:00:00Z")
        self.assertIs(report["compaction_ready"], False)
        self.assertEqual(report["summary"][rd.READY], [])


class TestTheLockProbeMustBeAboutTheDeclaredLock(_TargetBase):
    """A probe is a claim that *this* lock was taken, on *this* host, during *this* collection.

    Unbound, it is a claim that some lock somewhere was taken once. The declared compaction
    lock is the one the compaction posture fences with; a successful probe of anything else is
    evidence about a different file."""

    def _probe(self, **over):
        p = {"acquired": True, "released": True,
             "path": os.path.join(self.root, "p5_compaction.lock"),
             "run_id": FIXTURE_RUN, "host": socket.gethostname(),
             "at_utc": self.collected_utc, "actor": "ops/p5_provision.py"}
        p.update(over)
        return p

    def _locks(self, **over):
        return {"compaction": os.path.join(self.root, "p5_compaction.lock"),
                "ingest": os.path.join(self.root, "ingest.lock"),
                "acquire_probe": self._probe(**over)}

    def _verdict(self, **over):
        by_id = {r["id"]: r for r in
                 rd.assess(self.target(locks=self._locks(**over)))["requirements"]}
        return by_id["locks"]["verdict"], by_id["locks"]["reason"]

    def test_a_fully_bound_probe_is_ready(self):
        verdict, reason = self._verdict()
        self.assertEqual(verdict, rd.READY, reason)

    def test_a_probe_of_another_path_is_blocked(self):
        other = os.path.join(self.root, "some_other.lock")
        verdict, reason = self._verdict(path=other)
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("different", reason.lower())

    def test_a_probe_bound_by_realpath_not_by_string(self):
        """A symlink to the declared lock IS the declared lock; a different file is not."""
        real = os.path.join(self.root, "p5_compaction.lock")
        with open(real, "w") as fh:
            fh.write("")
        alias = os.path.join(self.root, "alias_to_compaction.lock")
        os.symlink(real, alias)
        verdict, reason = self._verdict(path=alias)
        self.assertEqual(verdict, rd.READY, reason)

    def test_a_probe_from_another_run_is_blocked(self):
        verdict, reason = self._verdict(run_id="A_DIFFERENT_RUN")
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("run", reason.lower())

    def test_a_probe_from_another_host_is_blocked(self):
        verdict, reason = self._verdict(host="somewhere-else")
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("host", reason.lower())

    def test_a_probe_taken_long_before_the_collection_is_blocked(self):
        verdict, reason = self._verdict(at_utc="2026-08-21T09:00:00Z")
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("collection", reason.lower())

    def test_a_probe_naming_no_actor_is_not_adjudicated(self):
        verdict, reason = self._verdict(actor="")
        self.assertEqual(verdict, rd.NOT_ADJUDICATED)
        self.assertIn("actor", reason.lower())

    def test_each_missing_binding_field_is_refused_on_its_own(self):
        """One at a time, so no field's absence is covered by another's presence."""
        for field in ("path", "run_id", "host", "at_utc", "actor"):
            with self.subTest(field):
                probe = self._probe()
                probe.pop(field)
                by_id = {r["id"]: r for r in rd.assess(self.target(
                    locks={"compaction": os.path.join(self.root, "p5_compaction.lock"),
                           "ingest": os.path.join(self.root, "ingest.lock"),
                           "acquire_probe": probe}))["requirements"]}
                self.assertNotEqual(by_id["locks"]["verdict"], rd.READY,
                                    f"a probe with no {field} still read READY")
                self.assertIn(field, by_id["locks"]["reason"])


class TestCapacityRequiresItsCollectionFields(_TargetBase):
    """`if target_run and ...` skipped the binding when `run_id` was absent, so omitting a field
    was safer than getting it wrong. An absent field must never be the easy path."""

    def _verdict(self, **over):
        by_id = {r["id"]: r for r in rd.assess(self.target(**over))["requirements"]}
        return by_id["capacity"]["verdict"], by_id["capacity"]["reason"]

    def test_a_target_without_run_id_is_not_adjudicated(self):
        t = self.target()
        t.pop("run_id")
        by_id = {r["id"]: r for r in rd.assess(t)["requirements"]}
        self.assertEqual(by_id["capacity"]["verdict"], rd.NOT_ADJUDICATED)
        self.assertIn("run_id", by_id["capacity"]["reason"])

    def test_a_target_without_collected_utc_is_not_adjudicated(self):
        t = self.target()
        t.pop("collected_utc")
        by_id = {r["id"]: r for r in rd.assess(t)["requirements"]}
        self.assertEqual(by_id["capacity"]["verdict"], rd.NOT_ADJUDICATED)
        self.assertIn("collected_utc", by_id["capacity"]["reason"])

    def test_a_measurement_from_the_future_is_not_adjudicated(self):
        """A reading ahead of the clock is a clock problem or a fabricated one. Either way it
        cannot be aged, and an unaged reading never expires."""
        future = "2026-08-21T14:00:00Z"
        t = self.target(run_id="R", collected_utc=future,
                        capacity=_fresh_capacity(run_id="R", measured_utc=future,
                                                 data_root=self.root,
                                                 filesystem_dev=os.stat(self.root).st_dev))
        by_id = {r["id"]: r for r in rd.assess(t, now="2026-08-21T13:00:00Z")["requirements"]}
        self.assertEqual(by_id["capacity"]["verdict"], rd.NOT_ADJUDICATED)
        self.assertIn("future", by_id["capacity"]["reason"].lower())

    def test_the_two_hour_expiry_still_applies(self):
        """Guarding the guard: the future check must not have replaced the expiry."""
        t = self.target(run_id="R", collected_utc="2026-08-21T13:00:00Z",
                        capacity=_fresh_capacity(
                            run_id="R", measured_utc="2026-08-21T13:00:00Z",
                            data_root=self.root,
                            filesystem_dev=os.stat(self.root).st_dev))
        by_id = {r["id"]: r for r in rd.assess(t, now="2026-08-21T16:00:00Z")["requirements"]}
        self.assertEqual(by_id["capacity"]["verdict"], rd.NOT_ADJUDICATED)
        self.assertIn("expired", by_id["capacity"]["reason"])

    def test_a_reading_a_few_seconds_ahead_is_tolerated(self):
        """Clocks are not synchronised to the second; the guard is against a real gap."""
        t = self.target(run_id="R", collected_utc="2026-08-21T13:00:00Z",
                        capacity=_fresh_capacity(
                            run_id="R", measured_utc="2026-08-21T13:00:02Z",
                            data_root=self.root,
                            filesystem_dev=os.stat(self.root).st_dev))
        by_id = {r["id"]: r for r in rd.assess(t, now="2026-08-21T13:00:00Z")["requirements"]}
        self.assertEqual(by_id["capacity"]["verdict"], rd.READY, by_id["capacity"]["reason"])


class TestTheFreshnessPolicyIsDocumentedAndEnforced(unittest.TestCase):
    """The window is a number in code and a rule for a person. Both have to say the same thing,
    and the document's figure is read back out of the module rather than typed beside it."""

    DOC = os.path.join(_DEV, "specs", "p5_deployment_readiness.md")

    def setUp(self):
        with open(self.DOC) as fh:
            self.flat = " ".join(fh.read().split())

    def test_the_documented_window_is_the_one_the_code_enforces(self):
        hours = rd.MAX_CAPACITY_AGE_S / 3600
        self.assertEqual(hours, int(hours), "the window is no longer a whole number of hours")
        self.assertIn(f"valid for **{int(hours)} hours from `measured_utc`**", self.flat)
        self.assertIn("MAX_CAPACITY_AGE_S", self.flat)

    def test_the_policy_names_what_invalidates_a_collection_early(self):
        for event in ("append", "restart", "mount", "write under"):
            with self.subTest(event):
                self.assertIn(event, self.flat)

    def test_the_policy_requires_one_short_window_for_everything(self):
        self.assertIn("in one short window", self.flat)
        for item in ("capacity", "healthz", "locks", "identity", "manifest", "WAL/anchor",
                     "quiescence"):
            with self.subTest(item):
                self.assertIn(item, self.flat)

    def test_the_document_lists_the_enforced_bindings(self):
        for enforced in ("must equal the collection", "within 300s", "ahead of the assessing "
                         "clock", "bound to the declared lock by realpath"):
            with self.subTest(enforced):
                self.assertIn(enforced, self.flat)

    def test_the_window_is_shorter_than_the_shortest_ingest_gap(self):
        """05:05, 07:30, 09:40, 18:05, 20:30 local. The shortest gap is 2h05m, so a window
        longer than that could hide a schedule tick -- the event that invalidates a reading."""
        windows = [(5, 5), (7, 30), (9, 40), (18, 5), (20, 30)]
        minutes = [h * 60 + m for h, m in windows]
        gaps = [b - a for a, b in zip(minutes, minutes[1:])]
        gaps.append(24 * 60 - minutes[-1] + minutes[0])
        self.assertLessEqual(rd.MAX_CAPACITY_AGE_S, min(gaps) * 60,
                             "a reading could survive an ingest window")


class TestBlocksUseTheStoresOwnContainmentRules(_TargetBase):
    """`_check_blocks` joined the segment path to the root and asked whether it existed. A
    `../` or a symlink pointing out of the block root satisfies that and is exactly what
    `SegmentedCubeStore._resolve_segment_path` exists to refuse: a block segment may not point
    at the delta, at another store, or through a symlink.

    Two path authorities disagreeing is worse than one being strict: the readiness check would
    certify a manifest the deployment then refuses to open."""

    def _repoint(self, raw_path):
        live_path = os.path.join(self.manifest_root, bm.LIVE_NAME)
        with open(live_path) as fh:
            live = json.load(fh)
        live["segments"][0]["path"] = raw_path
        live["manifest_checksum"] = ""
        live["manifest_checksum"] = bm.compute_checksum(live)
        with open(live_path, "w") as fh:
            json.dump(live, fh)
        return {r["id"]: r for r in rd.assess(self.target())["requirements"]}

    def test_a_dotdot_escape_is_blocked(self):
        outside = os.path.join(self.tmp, "outside_block.zarr")
        shutil.copytree(self.block_path, outside)
        by_id = self._repoint(os.path.join("..", "..", "outside_block.zarr"))
        self.assertEqual(by_id["blocks"]["verdict"], rd.BLOCKED)
        self.assertIn("outside", by_id["blocks"]["reason"].lower())

    def test_a_symlink_escape_is_blocked(self):
        outside = os.path.join(self.tmp, "sym_target.zarr")
        shutil.copytree(self.block_path, outside)
        link = os.path.join(self.manifest_root, "looks_inside.zarr")
        os.symlink(outside, link)
        by_id = self._repoint("looks_inside.zarr")
        self.assertEqual(by_id["blocks"]["verdict"], rd.BLOCKED)
        self.assertIn("outside", by_id["blocks"]["reason"].lower())

    def test_an_absolute_segment_path_is_blocked(self):
        by_id = self._repoint(self.block_path)
        self.assertEqual(by_id["blocks"]["verdict"], rd.BLOCKED)
        self.assertIn("absolute", by_id["blocks"]["reason"].lower())

    def test_a_contained_segment_is_still_ready(self):
        """Guarding the guard: containment must not become a blanket refusal."""
        by_id = {r["id"]: r for r in rd.assess(self.target())["requirements"]}
        self.assertEqual(by_id["blocks"]["verdict"], rd.READY, by_id["blocks"]["reason"])

    def test_the_check_delegates_to_the_store_rather_than_restating_it(self):
        with open(os.path.join(_DEV, "ops", "p5_readiness.py")) as fh:
            source = fh.read()
        self.assertIn("SegmentedCubeStore", source)
        for restated in ("def _within", "startswith(parent", "os.path.isabs(raw)"):
            with self.subTest(restated):
                self.assertNotIn(restated, source)


class TestCapacityIsBoundToTheFilesystemUnderTest(_TargetBase):
    """A free-space reading is a fact about a device. Nothing tied it to the device holding
    `$G`, so a reading from a roomy unrelated volume would have produced READY while the volume
    that matters was full."""

    def _cap_for_root(self, **over):
        cap = _fresh_capacity(measured_utc=self.collected_utc)
        cap.update(data_root=self.root, filesystem_dev=os.stat(self.root).st_dev)
        cap.update(over)
        return cap

    def _verdict(self, **over):
        by_id = {r["id"]: r for r in rd.assess(self.target(**over))["requirements"]}
        return by_id["capacity"]["verdict"], by_id["capacity"]["reason"]

    def test_a_reading_of_the_targets_own_device_is_ready(self):
        verdict, reason = self._verdict(capacity=self._cap_for_root())
        self.assertEqual(verdict, rd.READY, reason)

    def test_a_reading_from_another_device_is_blocked(self):
        verdict, reason = self._verdict(
            capacity=self._cap_for_root(filesystem_dev=os.stat(self.root).st_dev + 1))
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("device", reason.lower())

    def test_a_reading_of_another_path_is_blocked(self):
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere, exist_ok=True)
        verdict, reason = self._verdict(capacity=self._cap_for_root(data_root=elsewhere))
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("data_root", reason.lower())

    def test_a_reading_with_no_device_recorded_is_not_adjudicated(self):
        cap = self._cap_for_root()
        cap.pop("filesystem_dev")
        verdict, reason = self._verdict(capacity=cap)
        self.assertEqual(verdict, rd.NOT_ADJUDICATED)
        self.assertIn("filesystem_dev", reason)

    def test_the_device_binding_is_not_adjudicated_off_host(self):
        """`st_dev` is meaningless on another machine; refusing to compare it is the honest
        answer, not comparing it anyway."""
        by_id = {r["id"]: r for r in rd.assess(
            self.target(host="somewhere-else", capacity=self._cap_for_root()))["requirements"]}
        self.assertEqual(by_id["capacity"]["verdict"], rd.NOT_ADJUDICATED)


class TestTheExecutedResultMustBeCommittedAndTracked(_ApprovedAssessorMixin,
                                                    unittest.TestCase):
    """`load_execution` read whatever JSON the path pointed at. A path is not a provenance:
    `/tmp/whatever.json`, `../../anything.json` and a symlink out of the committed directory
    all satisfied it, so the evidence could be written by the same hand that wrote the claim.

    The artifact has to be a reviewable object: inside the one committed directory, and read
    FROM GIT at the approved commit rather than from the working tree."""

    def setUp(self):
        import subprocess
        self._approve_assessor()
        self.tmp = tempfile.mkdtemp(prefix="p5-exec-repo-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        self.execs = os.path.join(self.repo, "dev2026", rd.EXECUTIONS_REL)
        os.makedirs(self.execs)
        self.suite_rel = "tests/test_thing.py"
        suite_path = os.path.join(self.repo, "dev2026", self.suite_rel)
        os.makedirs(os.path.dirname(suite_path), exist_ok=True)
        with open(suite_path, "w") as fh:
            fh.write("# a suite\n")
        self.artifact_rel = "run_ok.json"
        self._write_artifact(self.artifact_rel)

        def git(*a):
            subprocess.run(("git",) + a, cwd=self.repo, check=True, capture_output=True)

        git("init", "-q")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        git("add", "-A")
        git("commit", "-qm", "committed evidence")
        self.sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                                  capture_output=True, text=True).stdout.strip()

        for name, value in (("REPO_ROOT", self.repo), ("DEV_PREFIX", "dev2026"),
                            ("REVIEWED_DEPLOY_SHAS", (self.sha,))):
            self.addCleanup(setattr, rd, name, getattr(rd, name))
            setattr(rd, name, value)

    def _write_artifact(self, rel, **over):
        import hashlib
        with open(os.path.join(self.repo, "dev2026", self.suite_rel), "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        run = {"suite": "tests.test_thing", "deploy_sha": "PLACEHOLDER",
               "command": ".venv/bin/python -m unittest tests.test_thing",
               "exit_code": 0, "tests_run": 12, "ran_utc": "2026-08-23T09:00:00Z",
               "source_digest": digest}
        run.update(over)
        path = os.path.join(self.execs, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            json.dump(run, fh)
        return path

    # ---------------------------------------------------------------- path policy
    def test_an_absolute_path_is_refused(self):
        with self.assertRaises(rd.ExecutionRefused) as caught:
            rd.resolve_execution_path("/tmp/anything.json")
        self.assertIn("absolute", str(caught.exception).lower())

    def test_a_dotdot_traversal_is_refused(self):
        """Escaping, so containment would catch it too -- the isolated case is below."""
        with self.assertRaises(rd.ExecutionRefused) as caught:
            rd.resolve_execution_path("../../etc/passwd.json")
        self.assertIn("..", str(caught.exception))

    def test_a_dotdot_that_resolves_back_INSIDE_is_still_refused(self):
        """The isolated fixture for this rule.

        `sub/../run_ok.json` lands on a real file in the committed directory, so containment
        accepts it and only the `..` rule can object -- which is why deleting that rule survived
        a mutation round against the escaping case alone.

        It is refused because a claim carrying `..` is not a canonical name for a committed
        object: two spellings of one artifact make the recorded evidence ambiguous, and the
        traversal it permits depends on what the intermediate components happen to be."""
        os.makedirs(os.path.join(self.execs, "sub"), exist_ok=True)
        inside = f"sub/../{self.artifact_rel}"
        self.assertTrue(os.path.isfile(os.path.join(self.execs, inside)),
                        "the fixture does not resolve to a real file; it proves nothing")
        with self.assertRaises(rd.ExecutionRefused) as caught:
            rd.resolve_execution_path(inside)
        self.assertIn("..", str(caught.exception))

    def test_a_symlink_escaping_the_committed_directory_is_refused(self):
        outside = os.path.join(self.tmp, "outside.json")
        with open(outside, "w") as fh:
            json.dump({}, fh)
        os.symlink(outside, os.path.join(self.execs, "looks_committed.json"))
        with self.assertRaises(rd.ExecutionRefused) as caught:
            rd.resolve_execution_path("looks_committed.json")
        self.assertIn("outside", str(caught.exception).lower())

    def test_a_path_inside_the_committed_directory_resolves(self):
        """Positive control for the path policy alone."""
        resolved = rd.resolve_execution_path(self.artifact_rel)
        self.assertEqual(os.path.realpath(resolved),
                         os.path.realpath(os.path.join(self.execs, self.artifact_rel)))

    # ---------------------------------------------------------------- git tracking
    def test_an_untracked_file_in_the_right_place_is_refused(self):
        """The dangerous case: correctly located, correctly shaped, and never committed."""
        self._write_artifact("never_committed.json", deploy_sha=self.sha)
        with self.assertRaises(rd.ExecutionRefused) as caught:
            rd.load_execution("never_committed.json", self.sha)
        self.assertIn("not tracked", str(caught.exception).lower())

    def test_the_working_tree_copy_cannot_override_the_committed_one(self):
        """Read from git, so editing the file on disk changes nothing."""
        self._write_artifact(self.artifact_rel, deploy_sha=self.sha, tests_run=999999)
        run = rd.load_execution(self.artifact_rel, self.sha)
        self.assertEqual(run["tests_run"], 12,
                         "the assessor read the working tree instead of the commit")

    def test_a_tracked_artifact_loads(self):
        run = rd.load_execution(self.artifact_rel, self.sha)
        self.assertEqual(run["suite"], "tests.test_thing")

    # ---------------------------------------------------------------- end to end
    def _target(self, claim):
        return {"label": rd.QUALIFYING_LABEL, "host": "nowhere",
                "failure_semantics_proven_by": claim,
                "failure_semantics_evidence_commit": getattr(self, "evidence_commit", self.sha)}

    def test_each_path_defect_is_refused_end_to_end(self):
        for claim in ("/tmp/x.json", "../x.json", "looks_committed.json"):
            with self.subTest(claim):
                by_id = {r["id"]: r for r in
                         rd.assess(self._target(claim))["requirements"]}
                self.assertEqual(by_id["failure_semantics"]["verdict"], rd.NOT_ADJUDICATED)


class TestTheLegacyAllowListIsAssessorPolicy(_TargetBase):
    """`allowed_legacy_paths` came from the target, so a manifest could ship the permission that
    lets its own segment escape the block root. An allow-list taken from the thing it governs
    is not an allow-list."""

    def test_a_target_supplied_allow_list_is_ignored(self):
        outside = os.path.join(self.tmp, "outside_block.zarr")
        shutil.copytree(self.block_path, outside)
        live_path = os.path.join(self.manifest_root, bm.LIVE_NAME)
        with open(live_path) as fh:
            live = json.load(fh)
        live["segments"][0]["kind"] = "legacy_base"
        live["segments"][0]["path"] = os.path.join("..", "..", "outside_block.zarr")
        live["manifest_checksum"] = ""
        live["manifest_checksum"] = bm.compute_checksum(live)
        with open(live_path, "w") as fh:
            json.dump(live, fh)

        by_id = {r["id"]: r for r in rd.assess(
            self.target(allowed_legacy_paths=[outside]))["requirements"]}
        self.assertEqual(by_id["blocks"]["verdict"], rd.BLOCKED,
                         "the target talked the assessor into allowing its own escape")

    def test_the_assessor_default_allows_nothing(self):
        self.assertEqual(tuple(rd.ALLOWED_LEGACY_PATHS), ())

    def test_the_policy_is_what_reaches_the_store(self):
        """Positive control: configured on the assessor, the allowance takes effect."""
        seen = {}
        from store import segmented_cube
        original = segmented_cube.SegmentedCubeStore

        class Recording(original):
            def __init__(self, path, allowed_legacy_paths=None):
                seen["allowed"] = list(allowed_legacy_paths or [])
                super().__init__(path, allowed_legacy_paths=allowed_legacy_paths)

        self.addCleanup(setattr, segmented_cube, "SegmentedCubeStore", original)
        segmented_cube.SegmentedCubeStore = Recording
        self.addCleanup(setattr, rd, "ALLOWED_LEGACY_PATHS", rd.ALLOWED_LEGACY_PATHS)
        rd.ALLOWED_LEGACY_PATHS = ("/configured/by/the/assessor",)
        rd.assess(self.target(allowed_legacy_paths=["/from/the/target"]))
        self.assertEqual(seen["allowed"], ["/configured/by/the/assessor"])


class TestCapacityIsRemeasuredNotRead(_TargetBase):
    """The artifact's `free_now_gib` decided G18. An artifact is a record of what someone saw;
    the gate is about what is there NOW, on this device, and the assessor is standing on it.

    So the assessor re-runs `statvfs` itself. The collected figure stays in the record as
    provenance -- it says what the collection observed -- but it does not decide anything."""

    def _live_free_gib(self):
        st = os.statvfs(self.root)
        return st.f_bavail * st.f_frsize / 1024 ** 3

    def _cap(self, **over):
        cap = _fresh_capacity(measured_utc=self.collected_utc, data_root=self.root,
                              filesystem_dev=os.stat(self.root).st_dev)
        cap.update(over)
        return cap

    def _verdict(self, **over):
        by_id = {r["id"]: r for r in rd.assess(self.target(**over))["requirements"]}
        return by_id["capacity"]["verdict"], by_id["capacity"]["reason"]

    def test_a_fabricated_free_space_figure_does_not_pass_the_gate(self):
        """500 GiB claimed against a canonical bar just above what the device actually has. The
        claim is generous, the device is not, and the device is what the build runs on."""
        minimum = round(self._live_free_gib() + 1000.0, 1)
        self._set_canonical(minimum)
        verdict, reason = self._verdict(
            capacity=self._cap(free_now_gib=500.0, minimum_free_gib=minimum))
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("statvfs", reason.lower())

    def test_the_verdict_quotes_the_remeasured_figure_not_the_claimed_one(self):
        minimum = round(self._live_free_gib() + 1000.0, 1)
        self._set_canonical(minimum)
        _, reason = self._verdict(
            capacity=self._cap(free_now_gib=500.0, minimum_free_gib=minimum))
        self.assertNotIn("500.0 GiB free", reason)

    def test_real_free_space_above_the_minimum_is_ready(self):
        """Positive control, with the device binding intact -- a low threshold this temp
        filesystem genuinely clears."""
        verdict, reason = self._verdict(capacity=self._cap())
        self.assertEqual(verdict, rd.READY, reason)
        self.assertIn("statvfs", reason.lower())

    def test_the_device_binding_still_applies_when_remeasuring(self):
        """Re-measuring must not have replaced the binding: the assessor has to be told WHICH
        path to statvfs, and that path has to be the target's own root."""
        elsewhere = os.path.join(self.tmp, "elsewhere")
        os.makedirs(elsewhere, exist_ok=True)
        verdict, reason = self._verdict(capacity=self._cap(data_root=elsewhere))
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("data_root", reason.lower())

    def test_the_collected_figure_is_kept_as_provenance(self):
        verdict, reason = self._verdict(capacity=self._cap(free_now_gib=500.0))
        self.assertEqual(verdict, rd.READY, reason)
        self.assertIn("collected", reason.lower())

    def test_an_unstatable_root_is_not_adjudicated(self):
        gone = os.path.join(self.tmp, "no_such_root")
        verdict, reason = self._verdict(root=gone, capacity=self._cap(data_root=gone))
        self.assertEqual(verdict, rd.NOT_ADJUDICATED)


class TestTheG18ThresholdIsCanonicalNotCollected(_TargetBase):
    """The assessor re-measured free space and then compared it against the threshold the
    TARGET supplied. Half the gate was hardened and the other half left as a field anyone could
    write: lower `minimum_free_gib` and a device with nothing on it passes.

    The threshold is policy. It comes from the capacity model in `ops/p5s8_evidence.py`, where
    `MINIMUM_FREE_GIB = HARD_RESERVE + additional`, and the collected figure is checked against
    it rather than believed."""

    def _live_free_gib(self):
        st = os.statvfs(self.root)
        return st.f_bavail * st.f_frsize / 1024 ** 3

    def _cap(self, **over):
        cap = _fresh_capacity(measured_utc=self.collected_utc, data_root=self.root,
                              filesystem_dev=os.stat(self.root).st_dev)
        cap.update(over)
        return cap

    def _verdict(self, **over):
        by_id = {r["id"]: r for r in rd.assess(self.target(**over))["requirements"]}
        return by_id["capacity"]["verdict"], by_id["capacity"]["reason"]

    def test_a_lowered_threshold_in_the_artifact_does_not_open_the_gate(self):
        """The reported reproduction: statvfs clears the target's tiny minimum and falls far
        short of the canonical one. The canonical one decides."""
        self._set_canonical(self._live_free_gib() + 1000.0)
        verdict, reason = self._verdict(capacity=self._cap(minimum_free_gib=0.001))
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("canonical", reason.lower())

    def test_an_artifact_minimum_disagreeing_with_policy_is_blocked(self):
        verdict, reason = self._verdict(capacity=self._cap(minimum_free_gib=1.5))
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("minimum_free_gib", reason)

    def test_a_missing_artifact_minimum_is_not_adjudicated(self):
        cap = self._cap()
        cap.pop("minimum_free_gib")
        verdict, reason = self._verdict(capacity=cap)
        self.assertEqual(verdict, rd.NOT_ADJUDICATED)
        self.assertIn("minimum_free_gib", reason)

    def test_an_agreeing_artifact_above_the_canonical_threshold_is_ready(self):
        """Positive control: policy and provenance agree, and the device clears it."""
        verdict, reason = self._verdict(capacity=self._cap())
        self.assertEqual(verdict, rd.READY, reason)

    def test_the_gate_and_the_report_move_with_the_canonical_model(self):
        """No second hardcoded threshold anywhere: raise the policy and the verdict flips."""
        raised = self._live_free_gib() + 1000.0
        self._set_canonical(raised)
        verdict, reason = self._verdict(capacity=self._cap(minimum_free_gib=raised))
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn(str(raised), reason)

    def test_the_canonical_value_is_the_capacity_models_own(self):
        """Read through the fixture's patch AND against the model's own derivation, so the
        readiness module is shown to have no threshold of its own either way."""
        from ops import p5s8_evidence as ev
        self.assertEqual(rd.canonical_minimum_free_gib(), ev.MINIMUM_FREE_GIB)
        self._set_canonical(round(ev.HARD_RESERVE_GIB
                                  + ev.capacity_model()["additional_gb"], 1))
        self.assertEqual(rd.canonical_minimum_free_gib(), 403.3)

    def test_the_readiness_module_hardcodes_no_threshold(self):
        with open(os.path.join(_DEV, "ops", "p5_readiness.py")) as fh:
            source = fh.read()
        for stale in ("403.3", "250", "600.0"):
            with self.subTest(stale):
                self.assertNotIn(stale, source)


class TestTheEvidenceCommitIsDeclaredAndApproved(_ApprovedAssessorMixin, unittest.TestCase):
    """`load_execution(proven, REVIEWED_DEPLOY_SHAS[0])` assumed the artifact lived at the FIRST
    approved commit. A result recording a run of commit N is committed AFTER commit N, so in
    production it lives at some later commit -- and the peek would simply not find it.

    The target names the commit the evidence is read from, that commit must itself be approved,
    and the artifact's own `deploy_sha` is checked separately: one says where the record is, the
    other says what it is about."""

    def setUp(self):
        import subprocess
        self._approve_assessor()
        self.tmp = tempfile.mkdtemp(prefix="p5-evidence-repo-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = os.path.join(self.tmp, "repo")
        self.dev = os.path.join(self.repo, "dev2026")
        self.execs = os.path.join(self.dev, rd.EXECUTIONS_REL)
        os.makedirs(self.execs)
        self.suite_rel = "tests/test_thing.py"
        suite_path = os.path.join(self.dev, self.suite_rel)
        os.makedirs(os.path.dirname(suite_path), exist_ok=True)
        with open(suite_path, "w") as fh:
            fh.write("# the suite whose run is being recorded\n")

        def git(*a):
            subprocess.run(("git",) + a, cwd=self.repo, check=True, capture_output=True)
        self.git = git
        git("init", "-q")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        git("add", "-A")
        git("commit", "-qm", "the code under test")
        self.deploy_sha = self._head()

        for name in ("REPO_ROOT", "DEV_PREFIX", "REVIEWED_DEPLOY_SHAS",
                     "FAILURE_SEMANTICS_SUITES"):
            self.addCleanup(setattr, rd, name, getattr(rd, name))
        rd.REPO_ROOT, rd.DEV_PREFIX = self.repo, "dev2026"
        # The fixture's suite is authorized HERE so these tests isolate their own subjects --
        # the allow-list has its own class, which narrows it deliberately.
        rd.FAILURE_SEMANTICS_SUITES = ("tests.test_thing",)

    def _head(self):
        import subprocess
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                              capture_output=True, text=True).stdout.strip()

    def _record_the_run(self, **over):
        """Commit the result AFTER the commit it is about -- the production ordering."""
        import hashlib
        with open(os.path.join(self.dev, self.suite_rel), "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        run = {"suite": "tests.test_thing", "deploy_sha": self.deploy_sha,
               "command": ".venv/bin/python -m unittest tests.test_thing",
               "exit_code": 0, "tests_run": 31, "ran_utc": "2026-08-23T10:00:00Z",
               "source_digest": digest}
        run.update(over)
        with open(os.path.join(self.execs, "run.json"), "w") as fh:
            json.dump(run, fh)
        self.git("add", "-A")
        self.git("commit", "-qm", "record the run")
        return self._head()

    def _target(self, **over):
        t = {"label": rd.QUALIFYING_LABEL, "host": "nowhere",
             "failure_semantics_proven_by": "run.json"}
        t.update(over)
        return t

    def _verdict(self, **over):
        by_id = {r["id"]: r for r in rd.assess(self._target(**over))["requirements"]}
        return by_id["failure_semantics"]["verdict"], by_id["failure_semantics"]["reason"]

    def test_the_production_ordering_is_ready(self):
        """The artifact exists ONLY at a later approved commit, never at the first one."""
        evidence_sha = self._record_the_run()
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
        self.assertIsNone(rd._git_blob(self.deploy_sha, f"{rd.EXECUTIONS_REL}/run.json"),
                          "the artifact exists at the first approved commit; ordering untested")
        verdict, reason = self._verdict(failure_semantics_evidence_commit=evidence_sha)
        self.assertEqual(verdict, rd.READY, reason)

    def test_no_declared_evidence_commit_is_not_adjudicated(self):
        evidence_sha = self._record_the_run()
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
        verdict, reason = self._verdict()
        self.assertEqual(verdict, rd.NOT_ADJUDICATED)
        self.assertIn("failure_semantics_evidence_commit", reason)

    def test_an_unapproved_evidence_commit_is_blocked(self):
        evidence_sha = self._record_the_run()
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha,)      # the evidence commit is NOT approved
        verdict, reason = self._verdict(failure_semantics_evidence_commit=evidence_sha)
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("approved", reason.lower())

    def test_an_artifact_naming_an_unapproved_deploy_sha_is_blocked(self):
        """Where the record lives and what it is about are separate claims."""
        evidence_sha = self._record_the_run(deploy_sha="d" * 40)
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
        verdict, reason = self._verdict(failure_semantics_evidence_commit=evidence_sha)
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("approved deploy set", reason)

    def test_the_digest_is_checked_against_the_deploy_commit_not_the_evidence_commit(self):
        """The suite must DIFFER between the two commits, or the test proves nothing.

        Recording the run and changing the suite in separate commits leaves the suite identical
        at the deploy and evidence commits, so comparing against either gives the same answer --
        which is how swapping one for the other survived a mutation round. Here the suite is
        changed IN the evidence commit: the digest is of the suite as it was when the run
        happened, so it matches at the deploy commit and would not at the evidence commit."""
        import hashlib
        with open(os.path.join(self.dev, self.suite_rel), "rb") as fh:
            at_deploy = hashlib.sha256(fh.read()).hexdigest()
        with open(os.path.join(self.dev, self.suite_rel), "a") as fh:
            fh.write("# changed in the same commit that records the run\n")
        with open(os.path.join(self.dev, self.suite_rel), "rb") as fh:
            at_evidence = hashlib.sha256(fh.read()).hexdigest()
        self.assertNotEqual(at_deploy, at_evidence, "the two commits carry the same suite")

        evidence_sha = self._record_the_run(source_digest=at_deploy)
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
        verdict, reason = self._verdict(failure_semantics_evidence_commit=evidence_sha)
        self.assertEqual(verdict, rd.READY, reason)

    def test_a_digest_matching_only_the_evidence_commit_is_blocked(self):
        """The other direction: a run claiming a suite it never ran."""
        import hashlib
        with open(os.path.join(self.dev, self.suite_rel), "a") as fh:
            fh.write("# changed in the same commit that records the run\n")
        with open(os.path.join(self.dev, self.suite_rel), "rb") as fh:
            at_evidence = hashlib.sha256(fh.read()).hexdigest()
        evidence_sha = self._record_the_run(source_digest=at_evidence)
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
        verdict, reason = self._verdict(failure_semantics_evidence_commit=evidence_sha)
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("different file", reason)

    def test_a_digest_of_a_different_file_is_still_blocked(self):
        evidence_sha = self._record_the_run(source_digest="a" * 64)
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
        verdict, reason = self._verdict(failure_semantics_evidence_commit=evidence_sha)
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("different file", reason)

    def test_an_untracked_artifact_at_the_evidence_commit_is_refused(self):
        evidence_sha = self._record_the_run()
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
        with open(os.path.join(self.execs, "uncommitted.json"), "w") as fh:
            json.dump({"suite": "x"}, fh)
        verdict, reason = self._verdict(failure_semantics_proven_by="uncommitted.json",
                                        failure_semantics_evidence_commit=evidence_sha)
        self.assertEqual(verdict, rd.NOT_ADJUDICATED)
        self.assertIn("not tracked", reason.lower())

    def test_a_working_tree_edit_does_not_change_the_verdict(self):
        evidence_sha = self._record_the_run()
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
        with open(os.path.join(self.execs, "run.json"), "w") as fh:
            json.dump({"suite": "tests.test_thing", "deploy_sha": self.deploy_sha,
                       "command": "x", "exit_code": 1, "tests_run": 0,
                       "ran_utc": "no", "source_digest": "b" * 64}, fh)
        verdict, reason = self._verdict(failure_semantics_evidence_commit=evidence_sha)
        self.assertEqual(verdict, rd.READY, reason)

    def test_path_defects_are_still_refused(self):
        evidence_sha = self._record_the_run()
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
        outside = os.path.join(self.tmp, "outside.json")
        with open(outside, "w") as fh:
            json.dump({}, fh)
        os.symlink(outside, os.path.join(self.execs, "escape.json"))
        for claim in ("/tmp/x.json", "../x.json", "sub/../run.json", "escape.json"):
            with self.subTest(claim):
                verdict, _ = self._verdict(failure_semantics_proven_by=claim,
                                           failure_semantics_evidence_commit=evidence_sha)
                self.assertEqual(verdict, rd.NOT_ADJUDICATED)

    def test_there_is_no_test_only_evidence_commit_override(self):
        """`EVIDENCE_COMMIT` let a test read from anywhere, which would have hidden exactly the
        production path this class exists to exercise."""
        self.assertFalse(hasattr(rd, "EVIDENCE_COMMIT"))


class TestTheAssessorIsItselfBound(unittest.TestCase):
    """Every guard so far pointed outward. The assessor read its own G18 policy from the
    WORKING TREE -- `ops/p5s8_evidence.py` as it happens to be on disk -- so editing one line
    of local policy moved the bar the assessor then enforced. The tool checking whether the
    deployment is approved code was not itself approved code.

    An assessor is trustworthy only at a commit someone approved, with a worktree matching it.
    Below that bar nothing it produces may authorize anything."""

    def setUp(self):
        for name in ("APPROVED_ASSESSOR_SHAS", "REPO_ROOT", "DEV_PREFIX"):
            self.addCleanup(setattr, rd, name, getattr(rd, name))

    def test_the_assessor_inventory_covers_itself_and_its_policy(self):
        for rel in ("ops/p5_readiness.py", "ops/p5s8_evidence.py"):
            with self.subTest(rel):
                self.assertIn(rel, rd.ASSESSOR_MODULES)

    def test_the_inventory_covers_every_g18_policy_dependency(self):
        """`MINIMUM_FREE_GIB` is derived through the roadmap and the report module; a policy
        dependency left out of the inventory is a place to change the bar unobserved."""
        for rel in ("ops/p5_roadmap.py", "ops/p5s8_report.py"):
            with self.subTest(rel):
                self.assertIn(rel, rd.ASSESSOR_MODULES)

    def test_the_deployment_inventory_covers_them_too(self):
        for rel in ("ops/p5_readiness.py", "ops/p5s8_evidence.py"):
            with self.subTest(rel):
                self.assertIn(rel, rd.INVENTORY_MODULES)

    def test_an_empty_approved_set_says_so_specifically(self):
        """"No assessor build is approved" and "you are on the wrong commit" are different
        situations for whoever reads this, and the membership test alone reports the second for
        both -- which is why deleting the empty-set branch survived a mutation round."""
        rd.APPROVED_ASSESSOR_SHAS = ()
        ok, reason = rd.assessor_is_approved()
        self.assertFalse(ok)
        self.assertIn("APPROVED_ASSESSOR_SHAS is empty", reason)

    def test_a_wrong_commit_says_which_commit(self):
        """The other half of the same distinction, so neither message stands in for the other."""
        root, head = approved_assessor_repo()
        rd.ASSESSOR_REPO_ROOT = root
        rd.APPROVED_ASSESSOR_SHAS = ("b" * 40,)
        ok, reason = rd.assessor_is_approved()
        self.assertFalse(ok)
        self.assertIn(head[:7], reason)
        self.assertNotIn("is empty", reason)

    def test_this_branch_is_not_an_approved_assessor(self):
        """The honest state today: `3efcfd5` has not been reviewed."""
        self.assertEqual(tuple(rd.APPROVED_ASSESSOR_SHAS), ())

    def test_a_locally_edited_policy_is_refused_rather_than_used(self):
        """The reported defect, end to end: change the threshold on disk and the assessor must
        refuse, not enforce the new number."""
        import subprocess
        repo = tempfile.mkdtemp(prefix="p5-assessor-repo-")
        self.addCleanup(shutil.rmtree, repo, ignore_errors=True)
        dev = os.path.join(repo, "dev2026", "ops")
        os.makedirs(dev)
        for rel in rd.ASSESSOR_MODULES:
            path = os.path.join(repo, "dev2026", rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write("MINIMUM_FREE_GIB = 403.3\n")

        def git(*a):
            subprocess.run(("git",) + a, cwd=repo, check=True, capture_output=True)
        git("init", "-q")
        git("config", "user.email", "t@example.invalid")
        git("config", "user.name", "t")
        git("add", "-A")
        git("commit", "-qm", "approved assessor")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                              capture_output=True, text=True).stdout.strip()
        rd.REPO_ROOT, rd.DEV_PREFIX = repo, "dev2026"
        rd.APPROVED_ASSESSOR_SHAS = (head,)

        ok, reason = rd.assessor_is_approved()
        self.assertTrue(ok, reason)                       # positive control: clean and approved

        policy = os.path.join(repo, "dev2026", "ops", "p5s8_evidence.py")
        with open(policy, "w") as fh:
            fh.write("MINIMUM_FREE_GIB = 0.001\n")        # the bar, lowered on disk
        ok, reason = rd.assessor_is_approved()
        self.assertFalse(ok, "a locally edited policy was accepted")
        self.assertIn("p5s8_evidence.py", reason)

    def test_an_unapproved_assessor_demotes_every_requirement(self):
        rd.APPROVED_ASSESSOR_SHAS = ()
        report = rd.assess({"label": rd.QUALIFYING_LABEL, "host": "nowhere"})
        self.assertIs(report["compaction_ready"], False)
        self.assertEqual(report["summary"][rd.READY], [])
        self.assertIs(report["assessor_approved"], False)
        self.assertTrue(report["assessor_note"])

    def test_the_demotion_reason_names_the_assessor_not_the_target(self):
        """Reported on the report, because a target with nothing in it is refused by its own
        checkers first -- the demotion only rewrites reasons that would otherwise be READY."""
        rd.APPROVED_ASSESSOR_SHAS = ()
        report = rd.assess({"label": rd.QUALIFYING_LABEL, "host": "nowhere"})
        self.assertIn("assessor", report["assessor_note"].lower())
        self.assertIn("approved", report["assessor_note"].lower())


class TestTheFailureSemanticsSuiteIsAuthorized(TestTheEvidenceCommitIsDeclaredAndApproved):
    """`suite` was any name, as long as the command mentioned it and the run was green. A
    trivial suite that asserts `True` passes with exit 0 and a positive test count, so the
    evidence said "some tests ran and did not fail" -- about nothing in particular."""

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, rd, "FAILURE_SEMANTICS_SUITES",
                        rd.FAILURE_SEMANTICS_SUITES)

    def test_the_allow_list_is_declared_in_approved_code(self):
        self.assertTrue(rd.FAILURE_SEMANTICS_SUITES)
        for name in rd.FAILURE_SEMANTICS_SUITES:
            with self.subTest(name):
                self.assertTrue(name.startswith("tests."))

    def test_an_unauthorized_suite_is_blocked(self):
        """Everything else about this artifact is correct: it is committed at an approved
        evidence commit, its digest matches the suite at the deploy commit, its path is clean
        and its run was green. Only the allow-list can refuse it."""
        rd.FAILURE_SEMANTICS_SUITES = ("tests.test_the_only_authorized_one",)
        evidence_sha = self._record_the_run()
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
        verdict, reason = self._verdict(failure_semantics_evidence_commit=evidence_sha)
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("authorized", reason.lower())
        self.assertIn("tests.test_thing", reason)

    def test_an_authorized_suite_with_the_same_evidence_is_ready(self):
        """The paired positive control: identical artifact, allow-list widened by one name."""
        rd.FAILURE_SEMANTICS_SUITES = ("tests.test_thing",)
        evidence_sha = self._record_the_run()
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
        verdict, reason = self._verdict(failure_semantics_evidence_commit=evidence_sha)
        self.assertEqual(verdict, rd.READY, reason)

    def test_the_digest_check_still_applies_to_an_authorized_suite(self):
        rd.FAILURE_SEMANTICS_SUITES = ("tests.test_thing",)
        evidence_sha = self._record_the_run(source_digest="a" * 64)
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
        verdict, reason = self._verdict(failure_semantics_evidence_commit=evidence_sha)
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("different file", reason)


class TestTheCommitSchemaFailsClosed(TestTheEvidenceCommitIsDeclaredAndApproved):
    """A commit id is 40 lowercase hex characters. Anything else -- an int, a bool, a short
    SHA, a branch name, an empty string -- must be refused as malformed, not passed to git and
    not raised as a TypeError out of a slice."""

    MALFORMED = (1, True, False, 0, "", "main", "HEAD", "3efcfd5",
                 "3EFCFD5A" * 5, "z" * 40, None, ["a" * 40], 40.0)

    def test_a_malformed_evidence_commit_is_refused_without_raising(self):
        evidence_sha = self._record_the_run()
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
        for bad in self.MALFORMED:
            with self.subTest(repr(bad)):
                verdict, reason = self._verdict(failure_semantics_evidence_commit=bad)
                self.assertIn(verdict, (rd.NOT_ADJUDICATED, rd.BLOCKED))
                self.assertNotIn("Traceback", reason)

    def test_a_malformed_evidence_commit_says_what_shape_is_required(self):
        evidence_sha = self._record_the_run()
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
        _, reason = self._verdict(failure_semantics_evidence_commit="3efcfd5")
        self.assertIn("40", reason)

    def test_a_malformed_deploy_sha_in_the_artifact_is_refused(self):
        for bad in ("3efcfd5", "", "main", 12345, True):
            with self.subTest(repr(bad)):
                evidence_sha = self._record_the_run(deploy_sha=bad)
                rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
                verdict, reason = self._verdict(
                    failure_semantics_evidence_commit=evidence_sha)
                self.assertIn(verdict, (rd.NOT_ADJUDICATED, rd.BLOCKED))
                self.assertNotIn("Traceback", reason)

    def test_a_well_formed_pair_is_still_ready(self):
        evidence_sha = self._record_the_run()
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
        verdict, reason = self._verdict(failure_semantics_evidence_commit=evidence_sha)
        self.assertEqual(verdict, rd.READY, reason)


class TestTheEvidenceCommitDescendsFromTheDeployCommit(TestTheEvidenceCommitIsDeclaredAndApproved):
    """A result about commit N has to be recorded in history that contains N. An approved
    commit on an unrelated line is approved -- and says nothing about N."""

    def test_an_unrelated_approved_commit_is_blocked(self):
        """The unrelated commit CARRIES the artifact, and the same suite bytes.

        An orphan branch without them is refused for being untracked, which is a different
        objection and would have let the descendancy rule be deleted unnoticed. Here tracking
        passes, the digest matches the suite at the deploy commit, the deploy sha is approved
        and the evidence commit is approved -- and it is on a line of history that never
        contained the commit it claims to be about."""
        import subprocess
        evidence_sha = self._record_the_run()
        main = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=self.repo,
                              capture_output=True, text=True).stdout.strip()
        with open(os.path.join(self.dev, self.suite_rel)) as fh:
            suite_bytes = fh.read()
        with open(os.path.join(self.execs, "run.json")) as fh:
            artifact = fh.read()

        self.git("checkout", "-q", "--orphan", "sideline")
        subprocess.run(["git", "rm", "-rqf", "."], cwd=self.repo, capture_output=True)
        for rel, body in ((self.suite_rel, suite_bytes),
                          (os.path.join(rd.EXECUTIONS_REL, "run.json"), artifact)):
            path = os.path.join(self.dev, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as fh:
                fh.write(body)
        self.git("add", "-A")
        self.git("commit", "-qm", "same content, unrelated history")
        unrelated = self._head()
        self.git("checkout", "-q", "-f", main)

        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha, unrelated)
        self.assertIsNotNone(rd._git_blob(unrelated, f"{rd.EXECUTIONS_REL}/run.json"),
                             "the artifact is absent on the unrelated line; tracking would "
                             "refuse it first and the descendancy rule would go untested")
        verdict, reason = self._verdict(failure_semantics_evidence_commit=unrelated)
        self.assertEqual(verdict, rd.BLOCKED)
        self.assertIn("descend", reason.lower())

    def test_the_descendant_positive_control_still_holds(self):
        evidence_sha = self._record_the_run()
        rd.REVIEWED_DEPLOY_SHAS = (self.deploy_sha, evidence_sha)
        self.assertIsNone(rd._git_blob(self.deploy_sha, f"{rd.EXECUTIONS_REL}/run.json"),
                          "the artifact exists at the deploy commit; ordering untested")
        verdict, reason = self._verdict(failure_semantics_evidence_commit=evidence_sha)
        self.assertEqual(verdict, rd.READY, reason)

    def test_the_deploy_commit_may_be_its_own_evidence_commit(self):
        """A commit is its own descendant; refusing that would be an off-by-one."""
        self.assertTrue(rd.commit_descends_from(self.deploy_sha, self.deploy_sha))


class TestTheAssessorRuntimeTreeIsClean(unittest.TestCase):
    """Comparing four policy files left the rest of the runtime unguarded.

    `_check_blocks` imports `store/segmented_cube.py` and the verdict comes straight out of it,
    yet it was not in the inventory: an approved HEAD with matching policy modules could be
    running an edited containment rule. An inventory that lists what someone remembered is a
    different thing from one covering what actually executes."""

    def setUp(self):
        import subprocess
        self.repo = tempfile.mkdtemp(prefix="p5-runtime-tree-")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        for rel in rd.ASSESSOR_MODULES:
            dst = os.path.join(self.repo, "dev2026", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy(os.path.join(_DEV, rel), dst)
        for args in (("init", "-q"), ("config", "user.email", "t@example.invalid"),
                     ("config", "user.name", "t"), ("add", "-A"),
                     ("commit", "-qm", "approved assessor runtime")):
            subprocess.run(("git",) + args, cwd=self.repo, check=True, capture_output=True)
        self.head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                                   capture_output=True, text=True).stdout.strip()
        for name in ("ASSESSOR_REPO_ROOT", "DEV_PREFIX", "APPROVED_ASSESSOR_SHAS"):
            self.addCleanup(setattr, rd, name, getattr(rd, name))
        rd.ASSESSOR_REPO_ROOT, rd.DEV_PREFIX = self.repo, "dev2026"
        rd.APPROVED_ASSESSOR_SHAS = (self.head,)

    def _path(self, rel):
        return os.path.join(self.repo, "dev2026", rel)

    # ---------------------------------------------------------------- inventory coverage
    #: Runs the REAL readiness path in a fresh interpreter and reports what it imported.
    #:
    #: The previous probe hand-imported the modules it expected to find, which proves only that
    #: those modules exist. A first-party import added inside `_check_wal_anchor` or any other
    #: checker would never have loaded, and the coverage test would have passed while the
    #: inventory silently missed it.
    #:
    #: This drives `assess()` and reports the REASONS the checkers produced, so the closure is
    #: what execution pulled in and the evidence that it executed is the checkers' own output.
    _CLOSURE_PROBE = """
import json, os, sys
cfg = json.load(open(sys.argv[1]))
DEV = cfg["dev"]
sys.path.insert(0, DEV)
from ops import p5_readiness as rd
for key, value in cfg["overrides"].items():
    setattr(rd, key, tuple(value) if isinstance(value, list) else value)
# The canonical G18 bar is policy the assessor reads through `ops.p5s8_evidence`; a temp
# filesystem does not hold 403.3 GiB, so the probe moves the POLICY rather than letting the
# target claim a lower one -- which is the point of that threshold being canonical.
from ops import p5s8_evidence as _ev
_ev.MINIMUM_FREE_GIB = cfg["canonical_minimum_gib"]
target = cfg["target"]
report = rd.assess(target)
verdicts = {r["id"]: r["verdict"] for r in report["requirements"]}
reasons = {r["id"]: r["reason"] for r in report["requirements"]}
# The unapproved branch too: it renders different reasons and may import differently.
rd.APPROVED_ASSESSOR_SHAS = ()
rd.assess(target)
out = set()
for mod in list(sys.modules.values()):
    f = getattr(mod, "__file__", None)
    if not f:
        continue
    f = os.path.abspath(f)
    if f.startswith(DEV + os.sep) and "/tests/" not in f and "/.venv/" not in f \\
            and f.endswith(".py"):
        out.add(os.path.relpath(f, DEV))
print(json.dumps({"closure": sorted(out), "verdicts": verdicts, "reasons": reasons,
                  "schema": report["schema"]}))
"""

    def _run_closure_probe(self):
        """Build a complete target elsewhere, then measure one clean `assess()` run."""
        import subprocess
        cfg_path = build_probe_workspace(self)
        proc = subprocess.run([sys.executable, "-c", self._CLOSURE_PROBE, cfg_path],
                              capture_output=True, text=True, cwd=_DEV)
        self.assertEqual(proc.returncode, 0, proc.stderr[-3000:])
        return json.loads(proc.stdout)

    #: Text only the real checker produces. A probe that hand-imported modules and reported
    #: invented verdicts would satisfy a verdict-only assertion -- these it cannot fabricate
    #: without running the checkers, which is the thing being evidenced.
    CHECKER_SIGNATURES = {
        "capacity": "statvfs reports",
        "blocks": "segments_verified=",
        "wal_anchor": "anchor=",
        "locks": "acquired and released by",
        "identity": "module fingerprints recomputed",
        "failure_semantics": "tests_run=",
        "manifest": "generation=",
    }

    def test_the_probe_actually_reaches_every_checker(self):
        """Without this, the coverage test could pass on a run where nothing executed.

        Each requirement must have been adjudicated on its merits AND its reason must carry
        text only that checker emits -- the evidence that the code ran, rather than a claim
        that it did."""
        result = self._run_closure_probe()
        self.assertEqual(result["schema"], "ghrsst.p5.readiness/1")
        for rid, signature in self.CHECKER_SIGNATURES.items():
            with self.subTest(rid):
                self.assertEqual(result["verdicts"].get(rid), rd.READY,
                                 f"{rid} did not run its success path")
                self.assertIn(signature, result["reasons"].get(rid, ""),
                              f"{rid}'s reason does not look like that checker's own output")

    def test_the_inventory_covers_every_module_the_readiness_path_imports(self):
        """Measured by execution, not by listing.

        A hand-maintained inventory drifts the moment an import is added, and the drift is
        invisible until someone edits the module nobody listed -- which is how
        `store/segmented_cube.py` came to decide verdicts from outside the inventory."""
        loaded = set(self._run_closure_probe()["closure"])
        self.assertIn("store/segmented_cube.py", loaded,
                      "the probe did not reach _check_blocks; it would prove nothing")
        missing = sorted(loaded - set(rd.ASSESSOR_MODULES))
        self.assertEqual(missing, [],
                         f"these execute on the readiness path and are not in the inventory: "
                         f"{missing}")

    def test_the_inventory_lists_nothing_that_does_not_execute(self):
        """The other direction: padding the inventory would make coverage pass emptily."""
        loaded = set(self._run_closure_probe()["closure"])
        self.assertEqual(sorted(set(rd.ASSESSOR_MODULES) - loaded), [])

    def test_the_named_runtime_modules_are_listed(self):
        for rel in ("ops/p5_readiness.py", "ops/p5s8_evidence.py", "ops/p5_roadmap.py",
                    "ops/p5s8_report.py", "store/segmented_cube.py"):
            with self.subTest(rel):
                self.assertIn(rel, rd.ASSESSOR_MODULES)

    # ---------------------------------------------------------------- the four cases
    def test_a_clean_approved_tree_is_approved(self):
        """Positive control for all three refusals below."""
        ok, reason = rd.assessor_is_approved()
        self.assertTrue(ok, reason)

    def test_editing_a_module_that_was_already_inventoried_is_refused(self):
        with open(self._path("ops/p5s8_evidence.py"), "a") as fh:
            fh.write("\n# edited policy\n")
        ok, reason = rd.assessor_is_approved()
        self.assertFalse(ok)
        self.assertIn("p5s8_evidence.py", reason)

    def test_editing_segmented_cube_is_refused(self):
        """The module the old inventory omitted, and the one `_check_blocks` gets its verdict
        from. Under the previous inventory this edit changed the containment rule and the
        assessor still called itself approved."""
        with open(self._path("store/segmented_cube.py"), "a") as fh:
            fh.write("\n# edited containment\n")
        ok, reason = rd.assessor_is_approved()
        self.assertFalse(ok)
        self.assertIn("segmented_cube.py", reason)

    def test_an_untracked_runtime_file_is_refused(self):
        """A new file shadowing or extending the runtime is not covered by comparing the
        blobs of files that were already there."""
        with open(self._path("store/sitecustomize_hack.py"), "w") as fh:
            fh.write("# not tracked at the approved commit\n")
        ok, reason = rd.assessor_is_approved()
        self.assertFalse(ok)
        self.assertIn("untracked", reason.lower())

    def test_a_dirty_tracked_file_outside_the_inventory_is_refused(self):
        """The clean-tree requirement, isolated: this file is tracked, is NOT in the inventory,
        and the blob comparison therefore cannot object to it."""
        import subprocess
        extra = self._path("ops/unrelated_helper.py")
        with open(extra, "w") as fh:
            fh.write("# committed, not inventoried\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "extra"], cwd=self.repo, check=True,
                       capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                              capture_output=True, text=True).stdout.strip()
        rd.APPROVED_ASSESSOR_SHAS = (head,)
        self.assertNotIn("ops/unrelated_helper.py", rd.ASSESSOR_MODULES)
        ok, _ = rd.assessor_is_approved()
        self.assertTrue(ok, "the fixture is not clean before the edit")

        with open(extra, "a") as fh:
            fh.write("# edited after approval\n")
        ok, reason = rd.assessor_is_approved()
        self.assertFalse(ok, "a dirty tracked file outside the inventory was accepted")
        self.assertIn("unrelated_helper.py", reason)

    def test_a_deleted_tracked_file_is_refused(self):
        os.remove(self._path("ops/p5_roadmap.py"))
        ok, reason = rd.assessor_is_approved()
        self.assertFalse(ok)
        self.assertIn("p5_roadmap.py", reason)

    def test_a_module_hidden_from_status_by_assume_unchanged_is_refused(self):
        """The per-module comparison earns its place here.

        With HEAD approved and the tree clean, every TRACKED file matches by definition -- so
        the blob check looks redundant, and deleting it survived a mutation round. It is not
        redundant against the cases `git status` cannot see: `--assume-unchanged` tells git to
        stop noticing local edits to a tracked file, and the worktree then reports clean while
        running different code."""
        import subprocess
        rel = "store/segmented_cube.py"
        subprocess.run(["git", "update-index", "--assume-unchanged", f"dev2026/{rel}"],
                       cwd=self.repo, check=True, capture_output=True)
        with open(self._path(rel), "a") as fh:
            fh.write("\n# edited behind git's back\n")
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                cwd=self.repo, capture_output=True, text=True).stdout.strip()
        self.assertEqual(status, "", "the fixture is visible to status; it proves nothing")

        ok, reason = rd.assessor_is_approved()
        self.assertFalse(ok, "an edit hidden from status was accepted")
        self.assertIn("segmented_cube.py", reason)
        self.assertIn("differs", reason)

    def test_an_ignored_module_absent_from_the_approved_commit_is_refused(self):
        """The other case status cannot see: a runtime module that is `.gitignore`d, so it is
        neither tracked nor reported as untracked -- present on disk, absent from the commit,
        and imported all the same."""
        import subprocess
        rel = "ops/quiescence.py"
        subprocess.run(["git", "rm", "--cached", "-q", f"dev2026/{rel}"],
                       cwd=self.repo, check=True, capture_output=True)
        with open(os.path.join(self.repo, ".gitignore"), "w") as fh:
            fh.write(f"dev2026/{rel}\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-qm", "ignore a runtime module"], cwd=self.repo,
                       check=True, capture_output=True)
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                              capture_output=True, text=True).stdout.strip()
        rd.APPROVED_ASSESSOR_SHAS = (head,)
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                                cwd=self.repo, capture_output=True, text=True).stdout.strip()
        self.assertEqual(status, "", "the fixture is visible to status; it proves nothing")
        self.assertTrue(os.path.isfile(self._path(rel)), "the module is not on disk")

        ok, reason = rd.assessor_is_approved()
        self.assertFalse(ok, "a runtime module absent from the approved commit was accepted")
        self.assertIn("quiescence.py", reason)
        self.assertIn("not present", reason)

    def test_the_clean_tree_check_names_what_is_dirty(self):
        with open(self._path("untracked_note.txt"), "w") as fh:
            fh.write("x\n")
        _, reason = rd.assessor_is_approved()
        self.assertIn("untracked_note.txt", reason)
