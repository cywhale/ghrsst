"""dev2026 — P5-S8: artifact contract, shadow safety gate, canonical manifest validation.

**LOCAL-ONLY. THE RUNBOOK IS NOT EXECUTED AGAINST VM24.** Every block runs in a `tempfile`
sandbox against stubbed `findmnt`/`pm2`/`curl`. No host is contacted.

Round 3 made the blocks executable. This round makes the **promises** executable too:

- the artifact table listed files no block created, so a run could "succeed" and produce almost
  none of its own evidence;
- the shadow had no filesystem safety gate of its own, only the prose of §0's;
- shadow provisioning was checked by hand-rolled `os.path` tests instead of the canonical P5
  validation, so an absolute path, a `../` escape or a symlink out of the root would have been
  accepted by the runbook and refused later by the code;
- the identity preflight was not a tagged block, so nothing proved it fails closed.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
for p in (_DEV, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import p5_fixtures as fx  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from test_phase2_p5s8_exec import BLOCKS, _stub, VENV_PY  # noqa: E402
from test_phase2_p5s7_http import _segment  # noqa: E402

RUNBOOK = os.path.join(_DEV, "specs", "p5s8_vm24_shadow_runbook.md")
NY = NX = 16


def _touch(path):
    with open(path, "w"):
        pass


class _Sandbox(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p5s8-art-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        self.G = os.path.join(self.tmp, "data", "ghrsst")
        self.art = os.path.join(self.tmp, "external", "p5s8_TEST")
        for d in (self.bin, self.G, self.art):
            os.makedirs(d, exist_ok=True)
        self.env = dict(os.environ)
        self.env.update({
            "PATH": self.bin + os.pathsep + os.environ["PATH"],
            "G": self.G, "PYTHON": VENV_PY, "CODE_ROOT": _DEV,
            "ART": self.art, "ART_ROOT": os.path.dirname(self.art), "RUN_ID": "TEST",
            "PROFILE": "legacy_baseline",
        })
        _stub(self.bin, "findmnt", f'if [[ "$*" == *"{self.G}"* ]]; then echo /dev/prod; '
                                   f'else echo /dev/other; fi')

    def run_block(self, tag, extra_env=None):
        self.assertIn(tag, BLOCKS, f"the runbook has no block tagged {tag}")
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run(["bash", "-c", BLOCKS[tag]], env=env, capture_output=True,
                              text=True, cwd=self.tmp)

    def shadow(self, *, segment_path=None, wal=True, anchor=True, symlink_escape=False):
        """A shadow whose manifest is built by the REAL publisher."""
        root = os.path.join(self.tmp, "shadow")
        mroot = os.path.join(root, "p5_blocks")
        os.makedirs(mroot, exist_ok=True)
        days = fx.days_from("2025-01-01", 10)
        blk = fx.build_block(os.path.join(mroot, "b0.zarr"), days, ny=NY, nx=NX, seed=1)
        rel = segment_path if segment_path is not None else "b0.zarr"
        if symlink_escape:
            outside = os.path.join(self.tmp, "outside_block")
            shutil.copytree(blk, outside, dirs_exist_ok=True)
            link = os.path.join(mroot, "link.zarr")
            if not os.path.islink(link):
                os.symlink(outside, link)
            rel = "link.zarr"
        seg = _segment("s0", rel, days, 0, blk)
        m = {"format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION, "generation": 1,
             "generation_id": "gen000001-shadow", "created_utc": "2027-01-01T00:00:00Z",
             "created_by": "p5s8-artifact-tests", "predecessor_generation": None,
             "predecessor_manifest": None,
             "grid": {"ny": NY, "nx": NX, "region": [0, NY, 0, NX]},
             "variables": list(fx.VARS),
             "block_grid": {"anchor_day": "2025-01-01", "block_days": 10},
             "segments": [seg], "superseded": [], "manifest_checksum": ""}
        m["manifest_checksum"] = bm.compute_checksum(m)
        try:
            bm.publish(mroot, m)
        except bm.ManifestError:
            # A deliberately invalid segment path still has to land on disk for the runbook to
            # be the thing that refuses it -- otherwise the test proves only that `publish`
            # validates, which is a different guarantee.
            with open(os.path.join(mroot, "manifest.json"), "w") as fh:
                json.dump(m, fh)
        if wal:
            os.makedirs(os.path.join(root, "p5_repairs"), exist_ok=True)
        anchor_dir = os.path.join(self.tmp, "anchor")
        if anchor:
            os.makedirs(anchor_dir, exist_ok=True)
            with open(os.path.join(anchor_dir, "domain.json"), "w") as fh:
                json.dump({"domain_id": "independent-1"}, fh)
        return root, anchor_dir


# ------------------------------------------------------------------ 1. p5_shadow scope
class TestTheShadowProfileClaimsOnlyWhatItMeasures(unittest.TestCase):
    """[High] The runbook said "everything downstream runs against $SHADOW_ROOT" while G4 still
    called `127.0.0.1:8080`, H7 still read production PM2 and `/proc`, and G16 still walked
    `$G`. Building an isolated shadow service is out of scope for a read-only step, so the
    profile is narrowed to what it actually does: **verification only**."""

    def setUp(self):
        from ops import p5_roadmap as rm
        self.rm = rm
        with open(RUNBOOK) as fh:
            self.doc = fh.read()

    def test_the_profile_declares_itself_verification_only(self):
        p5 = self.rm.S8_PROFILES["p5_shadow"]
        self.assertEqual(p5["scope"], "verification_only")
        self.assertEqual(p5["may_adjudicate"], [])

    def test_the_measurements_it_cannot_take_are_named_NOT_ADJUDICATED(self):
        p5 = self.rm.S8_PROFILES["p5_shadow"]
        for gate in ("G4", "H7_sustained_load", "G16_metadata_scan", "G16_snapshot_open"):
            with self.subTest(gate):
                self.assertIn(gate, p5["may_not_adjudicate"])

    def test_the_runbook_does_not_claim_downstream_runs_against_the_shadow(self):
        """The phrase may survive only inside the note recording that it WAS the claim.

        A flat `assertNotIn` flagged the correction paragraph itself — the third time a guard
        in this project has fired on the sentence documenting the fix. The distinction that
        matters is a live claim versus a quoted history, so the check is scoped to that."""
        flat = " ".join(self.doc.split()).lower()
        live = [m for m in range(len(flat))
                if flat.startswith("everything downstream runs", m)
                and "earlier draft said" not in flat[max(0, m - 120):m]]
        self.assertEqual(live, [], "the runbook still makes the claim as if it were true")
        self.assertIn("verification only", flat)

    def test_that_guard_still_catches_the_claim_made_live(self):
        saved, self.doc = self.doc, "Everything downstream runs against $SHADOW_ROOT."
        try:
            with self.assertRaises(AssertionError):
                self.test_the_runbook_does_not_claim_downstream_runs_against_the_shadow()
        finally:
            self.doc = saved

    #: Blocks that TAKE a measurement. Verification and record-keeping blocks may reference
    #: the shadow -- that is their job; a measurement may not, because nothing isolates it.
    MEASUREMENT_BLOCKS = ("g4-probe", "g4-sample", "h7-sample", "g16-scan")

    def test_no_measurement_block_is_redirected_at_the_shadow(self):
        """An allow-list of blocks that may mention SHADOW_ROOT needed updating every time a
        record-keeping block was added, which is maintenance, not a guard. The invariant is
        about measurements: none of them may claim shadow isolation it does not have."""
        for tag in self.MEASUREMENT_BLOCKS:
            with self.subTest(tag):
                self.assertIn(tag, BLOCKS)
                self.assertNotIn("SHADOW_ROOT", BLOCKS[tag],
                                 "a measurement block references the shadow while still "
                                 "reading production")

    def test_positive_control_a_redirected_measurement_would_be_caught(self):
        self.assertIn("SHADOW_ROOT", 'curl "$SHADOW_ROOT/x"')


# ------------------------------------------------------------------ 2. artifact contract
class TestTheArtifactContractIsHonoured(_Sandbox):
    """[High] The table promised `preflight.json`, `pm2_jlist.json`,
    `h7_fd_rss_timeseries.txt`, `g16_metadata_scan.txt` and `p5s8_readonly.json`; the blocks
    produced almost none of them. A run could 'succeed' and leave no evidence."""

    def _write_all(self, *, skip=(), corrupt=None):
        from ops import p5s8_report as rep
        for name in rep.required_artifacts("legacy_baseline"):
            if name in skip:
                continue
            path = os.path.join(self.art, name)
            if name == corrupt:
                with open(path, "w") as fh:
                    fh.write("{ not json")
                continue
            if name.endswith(".json"):
                with open(path, "w") as fh:
                    json.dump(rep.EXAMPLE_STUBS[name], fh)
            else:
                # Real series, not "sample 1": the parsers replaced the size check, so a
                # placeholder body would make every test here a test of the placeholder.
                bodies = {
                    "g4_366d_seconds.txt": "# run_id=TEST samples=1 validated_rows=366\n0.01\n",
                    "h7_fd_rss_timeseries.txt": "# run_id=TEST pids=[1] samples=1\n1 1 3 2048\n",  # pid set matches the roster stub
                    "g16_metadata_scan.txt": ("# run_id=TEST\nroot=/stub files=2 dirs=1 "
                                              "wall_s=0.5\n"),
                }
                with open(path, "w") as fh:
                    fh.write(bodies.get(name, "sample 1\n"))
        return self.art

    def test_the_generator_lists_the_same_artifacts_the_document_promises(self):
        from ops import p5s8_report as rep
        with open(RUNBOOK) as fh:
            doc = fh.read()
        for name in rep.required_artifacts("legacy_baseline"):
            with self.subTest(name):
                self.assertIn(name, doc, "the generator produces an artifact the doc omits")

    def test_a_complete_run_assembles_a_report(self):
        from ops import p5s8_report as rep
        self._write_all()
        out = rep.assemble(self.art, profile="legacy_baseline")
        self.assertEqual(out["profile"], "legacy_baseline")
        self.assertEqual(out["label"], "VM24_PRODUCTION_READ_ONLY")
        self.assertIn("artifacts", out)

    def test_a_MISSING_artifact_fails_closed(self):
        from ops import p5s8_report as rep
        self._write_all(skip=("df_h.txt",))
        with self.assertRaises(rep.ArtifactError) as cm:
            rep.assemble(self.art, profile="legacy_baseline")
        self.assertIn("df_h.txt", str(cm.exception))

    def test_a_MALFORMED_artifact_fails_closed(self):
        from ops import p5s8_report as rep
        self._write_all(corrupt="preflight.json")
        with self.assertRaises(rep.ArtifactError):
            rep.assemble(self.art, profile="legacy_baseline")

    def test_a_malformed_artifact_with_NO_field_checks_still_fails_closed(self):
        """Mutation testing found this: corrupting `preflight.json` is caught by its field
        checks even if JSON parsing is neutered, so the parse guard was never actually tested.
        `pm2_jlist.json` has no field checks — only the parser stands between a corrupt roster
        and an accepted report."""
        from ops import p5s8_report as rep
        self._write_all(corrupt="pm2_jlist.json")
        with self.assertRaises(rep.ArtifactError) as cm:
            rep.assemble(self.art, profile="legacy_baseline")
        self.assertIn("pm2_jlist.json", str(cm.exception))

    def test_a_preflight_missing_a_required_FIELD_fails_closed(self):
        from ops import p5s8_report as rep
        self._write_all()
        path = os.path.join(self.art, "preflight.json")
        with open(path) as fh:
            data = json.load(fh)
        data.pop("deploy_sha")
        with open(path, "w") as fh:
            json.dump(data, fh)
        with self.assertRaises(rep.ArtifactError) as cm:
            rep.assemble(self.art, profile="legacy_baseline")
        self.assertIn("deploy_sha", str(cm.exception))

    def test_a_not_applicable_field_satisfies_the_contract_with_a_reason(self):
        """`legacy_baseline` has no WAL. The field must be present as
        `not_applicable_with_reason`, and a bare null without a reason must NOT pass."""
        from ops import p5s8_report as rep
        self._write_all()
        path = os.path.join(self.art, "wal_state.json")
        with open(path, "w") as fh:
            json.dump({"wal_repairs_pending": None}, fh)      # no reason
        with self.assertRaises(rep.ArtifactError):
            rep.assemble(self.art, profile="legacy_baseline")

    def test_the_assemble_block_writes_the_report(self):
        self._write_all()
        r = self.run_block("assemble")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(os.path.isfile(os.path.join(self.art, "p5s8_readonly.json")))

    def test_the_assemble_block_fails_closed_on_a_missing_artifact(self):
        self._write_all(skip=("g16_metadata_scan.txt",))
        r = self.run_block("assemble")
        self.assertNotEqual(r.returncode, 0, "an incomplete run assembled a report")
        self.assertFalse(os.path.isfile(os.path.join(self.art, "p5s8_readonly.json")),
                         "a report was written despite the run being incomplete")


# ------------------------------------------------------------------ 3/4. value validation
class TestPreflightValuesAreValidatedNotJustPresent(_Sandbox):
    """Presence is not validity. A preflight carrying `deploy_sha: "x"` and
    `free_now_bytes: -1` has every required key and describes nothing."""

    def _pre(self, **over):
        from ops import p5s8_report as rep
        data = dict(rep.EXAMPLE_STUBS["preflight.json"])
        data.update({"run_id": "TEST", "deploy_sha": "a" * 40,
                     "code_root": "/opt/code", "python": "/opt/py", "pythonpath": "/opt/code",
                     "host": "h", "measured_utc": "2027-01-01T00:00:00Z",
                     "filesystem_device": "/dev/x", "filesystem_fstype": "ext4",
                     "free_now_bytes": 1, "inodes_free": 1,
                     "label": "VM24_PRODUCTION_READ_ONLY"})
        data.update(over)
        return data

    def _assert_refused(self, **over):
        from ops import p5s8_report as rep
        with self.assertRaises(rep.ArtifactError):
            rep.check_preflight_values(self._pre(**over), "legacy_baseline", run_id="TEST")

    def test_a_valid_preflight_passes(self):
        from ops import p5s8_report as rep
        rep.check_preflight_values(self._pre(), "legacy_baseline", run_id="TEST")

    def test_an_abbreviated_or_movable_deploy_sha_is_refused(self):
        for bad in ("HEAD", "a" * 7, "A" * 40, "z" * 40, "", None, 12345):
            with self.subTest(bad):
                self._assert_refused(deploy_sha=bad)

    def test_an_empty_path_field_is_refused(self):
        for field in ("code_root", "python", "pythonpath", "filesystem_device"):
            with self.subTest(field):
                self._assert_refused(**{field: ""})
                self._assert_refused(**{field: "   "})

    def test_a_negative_or_non_integer_capacity_value_is_refused(self):
        for bad in (-1, "267", 1.5, True, None):
            with self.subTest(bad):
                self._assert_refused(free_now_bytes=bad)
                self._assert_refused(inodes_free=bad)

    def test_a_wrong_label_is_refused(self):
        self._assert_refused(label="LOCAL_SYNTHETIC")

    def test_a_run_id_from_another_run_is_refused(self):
        self._assert_refused(run_id="OTHER")

    def test_a_not_applicable_reason_where_it_is_NOT_permitted_is_refused(self):
        """`legacy_baseline` may excuse the P5 fields and nothing else. Excusing
        `filesystem_device` would hide a capacity check that never ran."""
        self._assert_refused(
            filesystem_device="not_applicable_with_reason: could not be bothered")

    def test_a_permitted_not_applicable_still_needs_a_REASON(self):
        self._assert_refused(anchor_root="not_applicable_with_reason:")
        self._assert_refused(anchor_root=None)


class TestTheShadowVerificationRecordIsValidated(_Sandbox):
    """Item 3: `{}` or a partial record must not pass for `p5_shadow`."""

    def _rec(self, **over):
        data = {"segments": 2, "generation": 1, "wal_root": "/s/p5_repairs",
                "anchor_root": "/anchor", "anchor_domain_id": "independent",
                "measurement_taken": False}
        data.update(over)
        return data

    def _assert_refused(self, **over):
        from ops import p5s8_report as rep
        with self.assertRaises(rep.ArtifactError):
            rep.check_shadow_verification(self._rec(**over))

    def test_a_complete_record_passes(self):
        from ops import p5s8_report as rep
        rep.check_shadow_verification(self._rec())

    def test_an_empty_record_is_refused(self):
        from ops import p5s8_report as rep
        with self.assertRaises(rep.ArtifactError):
            rep.check_shadow_verification({})

    def test_every_required_field_is_required(self):
        for field in ("segments", "generation", "wal_root", "anchor_root",
                      "anchor_domain_id", "measurement_taken"):
            with self.subTest(field):
                rec = self._rec()
                rec.pop(field)
                from ops import p5s8_report as rep
                with self.assertRaises(rep.ArtifactError):
                    rep.check_shadow_verification(rec)

    def test_a_zero_or_negative_segment_count_is_refused(self):
        for bad in (0, -1, "2", 2.0, True, None):
            with self.subTest(bad):
                self._assert_refused(segments=bad)

    def test_an_invalid_generation_is_refused(self):
        for bad in (0, -3, "1", None, True):
            with self.subTest(bad):
                self._assert_refused(generation=bad)

    def test_an_undeclared_anchor_domain_is_refused(self):
        for bad in ("", "   ", None, 7):
            with self.subTest(bad):
                self._assert_refused(anchor_domain_id=bad)

    def test_measurement_taken_must_be_exactly_False(self):
        for bad in (True, "false", 0, None):
            with self.subTest(bad):
                self._assert_refused(measurement_taken=bad)


# ------------------------------------------------------------------ 3. shadow fs gate
class TestTheShadowFilesystemGate(_Sandbox):

    def test_a_good_shadow_passes(self):
        root, _ = self.shadow()
        r = self.run_block("shadow-fs-gate", extra_env={"SHADOW_ROOT": root})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_missing_shadow_root_aborts(self):
        r = self.run_block("shadow-fs-gate",
                           extra_env={"SHADOW_ROOT": os.path.join(self.tmp, "nope")})
        self.assertNotEqual(r.returncode, 0)

    def test_a_shadow_under_the_data_root_aborts(self):
        inside = os.path.join(self.G, "shadow")
        os.makedirs(inside, exist_ok=True)
        _stub(self.bin, "findmnt",
              f'if [[ "$*" == *"{inside}"* ]]; then echo /dev/bind; '
              f'elif [[ "$*" == *"{self.G}"* ]]; then echo /dev/prod; else echo /dev/other; fi')
        r = self.run_block("shadow-fs-gate", extra_env={"SHADOW_ROOT": inside})
        self.assertNotEqual(r.returncode, 0, "a shadow inside $G was accepted")

    def test_a_SYMLINK_into_the_data_root_aborts(self):
        link = os.path.join(self.tmp, "sneaky")
        target = os.path.join(self.G, "hidden")
        os.makedirs(target, exist_ok=True)
        if not os.path.islink(link):
            os.symlink(target, link)
        r = self.run_block("shadow-fs-gate", extra_env={"SHADOW_ROOT": link})
        self.assertNotEqual(r.returncode, 0, "a symlink into $G was accepted")

    def test_ONE_empty_findmnt_source_aborts(self):
        """The asymmetric case. With both sources empty they compare equal and the device check
        fires — for the wrong reason, but safely. With only one empty they DIFFER, and without
        the non-empty guard the gate passes on an unreadable mount table."""
        root, _ = self.shadow()
        _stub(self.bin, "findmnt",
              f'if [[ "$*" == *"{self.G}"* ]]; then echo ""; else echo /dev/other; fi')
        r = self.run_block("shadow-fs-gate", extra_env={"SHADOW_ROOT": root})
        self.assertNotEqual(r.returncode, 0,
                            "an unreadable production mount source was accepted")

    def test_the_production_device_aborts(self):
        root, _ = self.shadow()
        _stub(self.bin, "findmnt", "echo /dev/prod")
        r = self.run_block("shadow-fs-gate", extra_env={"SHADOW_ROOT": root})
        self.assertNotEqual(r.returncode, 0, "a shadow on the production device was accepted")

    def test_a_failing_findmnt_aborts(self):
        root, _ = self.shadow()
        _stub(self.bin, "findmnt", "exit 4")
        r = self.run_block("shadow-fs-gate", extra_env={"SHADOW_ROOT": root})
        self.assertNotEqual(r.returncode, 0, "a failed findmnt was treated as a pass")

    def test_an_empty_findmnt_source_aborts(self):
        root, _ = self.shadow()
        _stub(self.bin, "findmnt", "echo ''")
        r = self.run_block("shadow-fs-gate", extra_env={"SHADOW_ROOT": root})
        self.assertNotEqual(r.returncode, 0, "two empty sources compared equal and passed")


# ------------------------------------------------------------------ 4. canonical validation
class TestShadowVerifyUsesTheCanonicalValidation(_Sandbox):
    """Hand-rolled `os.path` checks are a second implementation of a rule P5 already owns.
    `SegmentedCubeStore` rejects absolute paths, `../` escapes and symlinks out of the root."""

    def test_the_block_uses_the_shipped_store(self):
        self.assertIn("SegmentedCubeStore", BLOCKS["shadow-verify"])

    def test_a_valid_shadow_passes(self):
        root, anchor = self.shadow()
        r = self.run_block("shadow-verify",
                           extra_env={"SHADOW_ROOT": root, "ANCHOR_ROOT": anchor})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_an_ABSOLUTE_segment_path_is_refused(self):
        root, anchor = self.shadow(segment_path="/etc")
        r = self.run_block("shadow-verify",
                           extra_env={"SHADOW_ROOT": root, "ANCHOR_ROOT": anchor})
        self.assertNotEqual(r.returncode, 0, "an absolute segment path was accepted")

    def test_a_TRAVERSAL_segment_path_is_refused(self):
        root, anchor = self.shadow(segment_path="../../outside.zarr")
        r = self.run_block("shadow-verify",
                           extra_env={"SHADOW_ROOT": root, "ANCHOR_ROOT": anchor})
        self.assertNotEqual(r.returncode, 0, "a ../ traversal was accepted")

    def test_a_SYMLINK_escape_is_refused(self):
        root, anchor = self.shadow(symlink_escape=True)
        r = self.run_block("shadow-verify",
                           extra_env={"SHADOW_ROOT": root, "ANCHOR_ROOT": anchor})
        self.assertNotEqual(r.returncode, 0, "a symlinked block outside the root was accepted")

    def test_a_missing_WAL_is_refused(self):
        root, anchor = self.shadow(wal=False)
        r = self.run_block("shadow-verify",
                           extra_env={"SHADOW_ROOT": root, "ANCHOR_ROOT": anchor})
        self.assertNotEqual(r.returncode, 0)


# ------------------------------------------------------------------ 5. identity preflight
class TestIdentityPreflightIsAnExecutableBlock(_Sandbox):

    def _repo(self, sha_ok=True):
        repo = os.path.join(self.tmp, "codetree")
        code = os.path.join(repo, "dev2026")
        os.makedirs(code, exist_ok=True)
        subprocess.run(["git", "init", "-q", repo], check=True)
        # Everything is committed BEFORE the SHA is read: the identity block now requires a
        # clean worktree, so files copied in afterwards would leave it dirty and the test would
        # fail for a reason unrelated to what it checks.
        with open(os.path.join(repo, ".gitignore"), "w") as fh:
            fh.write("__pycache__/\n*.pyc\n")
        _touch(os.path.join(repo, "f"))
        for pkg in ("store", "ops"):
            shutil.copytree(os.path.join(_DEV, pkg), os.path.join(code, pkg),
                            dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__"))
        # ops.p5_roadmap reads the committed capacity evidence at import time -- the whole
        # point of round 13 -- so a code tree without it is not a usable deployment.
        os.makedirs(os.path.join(code, "bench", "results"), exist_ok=True)
        shutil.copy(os.path.join(_DEV, "bench", "results", "p5s8_vm24_readonly.json"),
                    os.path.join(code, "bench", "results", "p5s8_vm24_readonly.json"))
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True, capture_output=True)
        subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "x"], check=True, capture_output=True)
        sha = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        return code, (sha if sha_ok else "HEAD")

    def test_it_is_a_tagged_block(self):
        self.assertIn("identity-preflight", BLOCKS)
        self.assertIn("set -euo pipefail", BLOCKS["identity-preflight"])

    def test_a_missing_code_root_aborts(self):
        r = self.run_block("identity-preflight", extra_env={
            "CODE_ROOT": os.path.join(self.tmp, "nope"), "DEPLOY_SHA": "0" * 40})
        self.assertNotEqual(r.returncode, 0)

    def test_a_movable_ref_as_DEPLOY_SHA_aborts(self):
        code, _ = self._repo()
        r = self.run_block("identity-preflight",
                           extra_env={"CODE_ROOT": code, "DEPLOY_SHA": "HEAD"})
        self.assertNotEqual(r.returncode, 0, "a movable ref passed as an exact pin")

    def test_an_exact_sha_passes(self):
        code, sha = self._repo()
        r = self.run_block("identity-preflight",
                           extra_env={"CODE_ROOT": code, "DEPLOY_SHA": sha})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
