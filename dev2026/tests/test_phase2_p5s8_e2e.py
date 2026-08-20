"""dev2026 — P5-S8: the whole producer sequence runs, end to end, per profile.

**LOCAL-ONLY. THE RUNBOOK IS NOT EXECUTED AGAINST VM24.** Every block runs in a `tempfile`
sandbox with stubbed `findmnt`, `df`, `pm2`, `curl` and a throwaway git repo. No host is
contacted; no production command is issued.

## Why unit tests were not enough

The artifact tests build a complete `$ART` with `_write_all()` and assert the assembler's
behaviour. That proves the *assembler*, not the *runbook* — and the two had drifted:
`preflight-record` read `ANCHOR_ROOT`, `WAL_ROOT`, `MANIFEST_ROOT`, `MANIFEST_GENERATION`,
`FILESYSTEM_DEVICE`, `FILESYSTEM_FSTYPE`, `FREE_NOW_BYTES` and `INODES_FREE` straight from the
environment, and nothing in the runbook ever set them. Run in order, the sequence aborted on
blank fields; the only way it passed was to hand-write the artifact it was supposed to produce.

So these tests execute the **real blocks in the real order** and let the assembler judge what
comes out. Each producer's output is then removed, corrupted or made stale in turn, and the
report must not be created.
"""
from __future__ import annotations

import json
import os
import shutil
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

NY = NX = 16

#: The producer sequence, per profile, in the order an operator runs it. The order is part of
#: the contract: `preflight-record` derives fields from artifacts earlier blocks wrote, so a
#: sequence that runs it first cannot populate them.
SEQUENCE = {
    "legacy_baseline": ["art-root", "identity-preflight", "capacity", "wal-record",
                        "preflight-record", "g4-probe", "g4-sample", "h7-sample",
                        "g16-scan"],
    # `capacity` runs under both profiles: a record of WHERE the run happened is context every
    # profile needs, and `df`/`findmnt` are reads.
    "p5_shadow": ["art-root", "identity-preflight", "capacity", "shadow-fs-gate",
                  "shadow-verify", "wal-record", "preflight-record"],
}


class _E2E(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p5s8-e2e-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        self.G = os.path.join(self.tmp, "data", "ghrsst")
        self.ext = os.path.join(self.tmp, "external")
        for d in (self.bin, self.G, self.ext, os.path.join(self.G, "sub")):
            os.makedirs(d, exist_ok=True)
        with open(os.path.join(self.G, "sub", "f.bin"), "w") as fh:
            fh.write("x")

        self.code, self.sha = self._repo()
        self.shadow_root, self.anchor = self._shadow()
        self._stubs()

        self.env = dict(os.environ)
        self.env.update({
            "PATH": self.bin + os.pathsep + os.environ["PATH"],
            "G": self.G, "PYTHON": VENV_PY, "CODE_ROOT": self.code,
            "PYTHONPATH": self.code, "ART_ROOT": self.ext,
            "DEPLOY_SHA": self.sha, "RUN_ID": "E2ETEST",
            "PROC_ROOT": os.path.join(self.tmp, "proc"),
            "SAMPLES": "2", "INTERVAL": "0", "G4_SAMPLES": "2", "G4_SLEEP": "0",
            "SHADOW_ROOT": self.shadow_root, "ANCHOR_ROOT": self.anchor,
        })
        self.art = os.path.join(self.ext, "p5s8_E2ETEST")

    # ---- fixtures ---------------------------------------------------
    def _repo(self):
        repo = os.path.join(self.tmp, "codetree")
        code = os.path.join(repo, "dev2026")
        os.makedirs(code, exist_ok=True)
        subprocess.run(["git", "init", "-q", repo], check=True)
        for pkg in ("store", "ops"):
            shutil.copytree(os.path.join(_DEV, pkg), os.path.join(code, pkg),
                            dirs_exist_ok=True)
        with open(os.path.join(repo, "marker"), "w") as fh:
            fh.write("x")
        subprocess.run(["git", "-C", repo, "add", "-A"], check=True,
                       capture_output=True)
        subprocess.run(["git", "-C", repo, "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "x"], check=True, capture_output=True)
        sha = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"],
                             capture_output=True, text=True).stdout.strip()
        return code, sha

    def _shadow(self):
        root = os.path.join(self.tmp, "shadow")
        mroot = os.path.join(root, "p5_blocks")
        os.makedirs(mroot, exist_ok=True)
        days = fx.days_from("2025-01-01", 10)
        blk = fx.build_block(os.path.join(mroot, "b0.zarr"), days, ny=NY, nx=NX, seed=1)
        seg = _segment("s0", "b0.zarr", days, 0, blk)
        m = {"format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION, "generation": 1,
             "generation_id": "gen000001-e2e", "created_utc": "2027-01-01T00:00:00Z",
             "created_by": "p5s8-e2e", "predecessor_generation": None,
             "predecessor_manifest": None,
             "grid": {"ny": NY, "nx": NX, "region": [0, NY, 0, NX]},
             "variables": list(fx.VARS),
             "block_grid": {"anchor_day": "2025-01-01", "block_days": 10},
             "segments": [seg], "superseded": [], "manifest_checksum": ""}
        m["manifest_checksum"] = bm.compute_checksum(m)
        bm.publish(mroot, m)
        os.makedirs(os.path.join(root, "p5_repairs"), exist_ok=True)
        anchor = os.path.join(self.tmp, "anchor")
        os.makedirs(anchor, exist_ok=True)
        with open(os.path.join(anchor, "domain.json"), "w") as fh:
            json.dump({"domain_id": "independent-e2e"}, fh)
        return root, anchor

    def _stubs(self):
        _stub(self.bin, "findmnt",
              f'if [[ "$*" == *"{self.G}"* ]]; then S=/dev/prod; F=ext4; '
              f'else S=/dev/other; F=xfs; fi\n'
              'if [[ "$*" == *"SOURCE,FSTYPE"* ]]; then echo "$S $F /mnt"; else echo "$S"; fi')
        _stub(self.bin, "df", 'echo "Filesystem Size Used Avail Use% Mounted"; '
                              'echo "/dev/prod 6.0T 5.4T 267G 96% /mnt"')
        pids = [4101, 4102]
        proc = os.path.join(self.tmp, "proc")
        for pid in pids:
            d = os.path.join(proc, str(pid), "fd")
            os.makedirs(d, exist_ok=True)
            for k in range(3):
                with open(os.path.join(d, str(k)), "w"):
                    pass
            with open(os.path.join(proc, str(pid), "status"), "w") as fh:
                fh.write("VmRSS:\t 2048 kB\n")
        jl = json.dumps([{"name": "ghrsst", "pid": p, "pm2_env": {"status": "online"}}
                         for p in pids])
        _stub(self.bin, "pm2", f"cat <<'JSON'\n{jl}\nJSON")
        body = json.dumps([{"date": f"d{i}", "sst": 1.0} for i in range(366)])
        _stub(self.bin, "curl", f'''
args="$*"
if [[ "$args" == *"time_total"* ]]; then echo "200 0.011"; exit 0; fi
cat <<'JSON'
{body}
JSON
if [[ "$args" == *"http_code"* ]]; then echo "200"; fi''')

    # ---- runner -----------------------------------------------------
    def run_sequence(self, profile, stop_after=None):
        """Run the producer blocks in order, threading exported state between them."""
        env = dict(self.env)
        env["PROFILE"] = profile
        env["ART"] = self.art
        results = []
        for tag in SEQUENCE[profile]:
            self.assertIn(tag, BLOCKS, f"missing block {tag}")
            r = subprocess.run(["bash", "-c", BLOCKS[tag]], env=env, capture_output=True,
                               text=True, cwd=self.tmp)
            results.append((tag, r))
            if r.returncode != 0:
                return results
            if tag == stop_after:
                break
        return results

    def assemble(self, profile):
        env = dict(self.env)
        env.update({"PROFILE": profile, "ART": self.art})
        return subprocess.run(["bash", "-c", BLOCKS["assemble"]], env=env,
                              capture_output=True, text=True, cwd=self.tmp)

    def assert_all_ok(self, results):
        for tag, r in results:
            self.assertEqual(r.returncode, 0,
                             f"block {tag} failed:\n{r.stdout}\n{r.stderr}")


class TestTheLegacySequenceRunsEndToEnd(_E2E):

    def test_the_producer_sequence_completes_and_assembles(self):
        res = self.run_sequence("legacy_baseline")
        self.assert_all_ok(res)
        self.assertEqual(len(res), len(SEQUENCE["legacy_baseline"]))
        a = self.assemble("legacy_baseline")
        self.assertEqual(a.returncode, 0, a.stdout + a.stderr)
        report = os.path.join(self.art, "p5s8_readonly.json")
        self.assertTrue(os.path.isfile(report))
        with open(report) as fh:
            data = json.load(fh)
        self.assertEqual(data["profile"], "legacy_baseline")

    def test_the_preflight_is_DERIVED_not_read_from_a_hopeful_environment(self):
        """The blocking defect: these fields were read from env vars nothing ever set."""
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        with open(os.path.join(self.art, "preflight.json")) as fh:
            pre = json.load(fh)
        self.assertEqual(pre["filesystem_device"], "/dev/prod")
        self.assertEqual(pre["filesystem_fstype"], "ext4")
        self.assertGreater(pre["free_now_bytes"], 0)
        self.assertGreater(pre["inodes_free"], 0)
        self.assertEqual(pre["deploy_sha"], self.sha)
        for p5_field in ("anchor_root", "wal_root", "manifest_generation"):
            with self.subTest(p5_field):
                self.assertIn("not_applicable_with_reason", str(pre[p5_field]))

    def test_g16_writes_its_own_artifact(self):
        """Item 2: the block printed to stdout and the prose asked for a manual `tee`, while
        the assembler required the file. Prose is not a producer."""
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        path = os.path.join(self.art, "g16_metadata_scan.txt")
        self.assertTrue(os.path.isfile(path), "g16-scan did not write its artifact")
        with open(path) as fh:
            self.assertIn("files", fh.read())

    def test_preflight_record_REFUSES_when_its_input_is_missing(self):
        """The handoff, tested directly. `preflight-record` derives its capacity fields from
        `capacity.json`; without it the block must abort rather than emit blanks or fall back
        to an environment variable nobody set."""
        res = self.run_sequence("legacy_baseline", stop_after="capacity")
        self.assert_all_ok(res)
        os.remove(os.path.join(self.art, "capacity.json"))
        env = dict(self.env)
        env.update({"PROFILE": "legacy_baseline", "ART": self.art})
        r = subprocess.run(["bash", "-c", BLOCKS["preflight-record"]], env=env,
                           capture_output=True, text=True, cwd=self.tmp)
        self.assertNotEqual(r.returncode, 0, "a preflight was recorded without its input")
        self.assertIn("NO-GO", r.stdout + r.stderr)
        self.assertFalse(os.path.isfile(os.path.join(self.art, "preflight.json")))

    def test_a_preflight_field_with_NO_derivation_path_is_refused(self):
        """The other half of the handoff. A field added to `S8_PREFLIGHT_FIELDS` without a
        derivation and without a not-applicable entry must abort the block, not be written as
        an empty string. Mutation testing showed the missing-input test could not reach this
        branch — the required-input check fires first — so it needs its own case."""
        self.assert_all_ok(self.run_sequence("legacy_baseline", stop_after="wal-record"))
        roadmap = os.path.join(self.code, "ops", "p5_roadmap.py")
        with open(roadmap) as fh:
            src = fh.read()
        with open(roadmap, "w") as fh:
            fh.write(src.replace('"inodes_free", "label",',
                                 '"inodes_free", "label", "a_field_nobody_derives",'))
        env = dict(self.env)
        env.update({"PROFILE": "legacy_baseline", "ART": self.art})
        r = subprocess.run(["bash", "-c", BLOCKS["preflight-record"]], env=env,
                           capture_output=True, text=True, cwd=self.tmp)
        self.assertNotEqual(r.returncode, 0, "an underivable field was written as a blank")
        self.assertIn("a_field_nobody_derives", r.stdout + r.stderr)
        self.assertFalse(os.path.isfile(os.path.join(self.art, "preflight.json")))

    def test_a_MISSING_producer_output_prevents_the_report(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        os.remove(os.path.join(self.art, "g16_metadata_scan.txt"))
        a = self.assemble("legacy_baseline")
        self.assertNotEqual(a.returncode, 0)
        self.assertFalse(os.path.isfile(os.path.join(self.art, "p5s8_readonly.json")))

    def test_a_MALFORMED_producer_output_prevents_the_report(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        with open(os.path.join(self.art, "pm2_jlist.json"), "w") as fh:
            fh.write("{ truncated")
        a = self.assemble("legacy_baseline")
        self.assertNotEqual(a.returncode, 0)
        self.assertFalse(os.path.isfile(os.path.join(self.art, "p5s8_readonly.json")))

    def test_a_STALE_producer_output_prevents_the_report(self):
        """An artifact carried over from an earlier run is the subtlest failure: every number
        in it is real, and it describes a different run."""
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        path = os.path.join(self.art, "preflight.json")
        with open(path) as fh:
            pre = json.load(fh)
        pre["run_id"] = "SOME-OTHER-RUN"
        with open(path, "w") as fh:
            json.dump(pre, fh)
        a = self.assemble("legacy_baseline")
        self.assertNotEqual(a.returncode, 0, "a preflight from another run was accepted")
        self.assertFalse(os.path.isfile(os.path.join(self.art, "p5s8_readonly.json")))

    def test_an_empty_series_prevents_the_report(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        with open(os.path.join(self.art, "g4_366d_seconds.txt"), "w"):
            pass
        a = self.assemble("legacy_baseline")
        self.assertNotEqual(a.returncode, 0)


class TestTheShadowSequenceRunsEndToEnd(_E2E):

    def test_the_verification_only_sequence_completes_and_assembles(self):
        res = self.run_sequence("p5_shadow")
        self.assert_all_ok(res)
        a = self.assemble("p5_shadow")
        self.assertEqual(a.returncode, 0, a.stdout + a.stderr)
        with open(os.path.join(self.art, "p5s8_readonly.json")) as fh:
            data = json.load(fh)
        self.assertEqual(data["profile"], "p5_shadow")
        self.assertEqual(data["adjudicates"], [])

    def test_the_shadow_preflight_derives_its_P5_fields(self):
        self.assert_all_ok(self.run_sequence("p5_shadow"))
        with open(os.path.join(self.art, "preflight.json")) as fh:
            pre = json.load(fh)
        self.assertEqual(pre["anchor_root"], self.anchor)
        self.assertEqual(pre["anchor_domain_id"], "independent-e2e")
        self.assertEqual(pre["manifest_generation"], 1)
        self.assertTrue(str(pre["manifest_root"]))
        self.assertNotIn("not_applicable", str(pre["manifest_generation"]))

    def test_the_verification_record_says_no_measurement_was_taken(self):
        self.assert_all_ok(self.run_sequence("p5_shadow"))
        with open(os.path.join(self.art, "shadow_verification.json")) as fh:
            rec = json.load(fh)
        self.assertIs(rec["measurement_taken"], False)
        self.assertGreater(rec["segments"], 0)

    def test_a_FABRICATED_verification_record_is_rejected(self):
        self.assert_all_ok(self.run_sequence("p5_shadow"))
        with open(os.path.join(self.art, "shadow_verification.json"), "w") as fh:
            json.dump({}, fh)
        a = self.assemble("p5_shadow")
        self.assertNotEqual(a.returncode, 0, "an empty verification record was accepted")

    def test_a_record_claiming_a_measurement_was_taken_is_rejected(self):
        """`p5_shadow` is verification-only. A record asserting otherwise is the profile
        quietly widening its own scope."""
        self.assert_all_ok(self.run_sequence("p5_shadow"))
        path = os.path.join(self.art, "shadow_verification.json")
        with open(path) as fh:
            rec = json.load(fh)
        rec["measurement_taken"] = True
        with open(path, "w") as fh:
            json.dump(rec, fh)
        a = self.assemble("p5_shadow")
        self.assertNotEqual(a.returncode, 0)

    def test_the_shadow_profile_produces_no_measurement_artifacts(self):
        self.assert_all_ok(self.run_sequence("p5_shadow"))
        for measurement in ("g4_366d_seconds.txt", "h7_fd_rss_timeseries.txt",
                            "g16_metadata_scan.txt"):
            with self.subTest(measurement):
                self.assertFalse(os.path.isfile(os.path.join(self.art, measurement)),
                                 "a verification-only profile produced a measurement")


if __name__ == "__main__":
    unittest.main()
