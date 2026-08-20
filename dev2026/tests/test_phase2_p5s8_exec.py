"""dev2026 — P5-S8 runbook: the command blocks are EXECUTED against stubs.

**LOCAL-ONLY. THE RUNBOOK IS NOT RUN AGAINST VM24.** Every block here runs in a `tempfile`
sandbox with fake `findmnt`, `pm2`, `curl` and `df` on `PATH`, against fake data directories.
No host is contacted and no production command is issued.

## Why grep was not enough

The previous round's guards read the runbook as text. Text guards prove a check is *written*;
they cannot prove it *fires*. A block can contain `[ "$ROWS" -eq 366 ]` and still accept a bad
run, because without `set -e` the failing test is one line among many and the script continues
to the next command with a `$?` nobody reads.

So each block is extracted by its `# block:` tag, run under `bash`, and asserted to **exit
non-zero** when a failure is injected — and zero on the good path, so an always-failing block
cannot masquerade as a working guard.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


def _touch(path):
    """Create an empty file with a context-managed handle.

    `open(p, "w").close()` closes promptly and raises no ResourceWarning, but it still hands a
    handle to an expression rather than a `with`, which is the pattern the S7 audit flags."""
    with open(path, "w"):
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
if _DEV not in sys.path:
    sys.path.insert(0, _DEV)

RUNBOOK = os.path.join(_DEV, "specs", "p5s8_vm24_shadow_runbook.md")
VENV_PY = os.path.join(_DEV, ".venv", "bin", "python")


def _blocks():
    """`{tag: body}` for every fenced block whose first line is `# block: <tag>`."""
    with open(RUNBOOK) as fh:
        doc = fh.read()
    out, lines, buf, inb, = {}, doc.split("\n"), [], False
    for ln in lines:
        if ln.strip().startswith("```bash"):
            inb, buf = True, []
            continue
        if inb and ln.strip() == "```":
            inb = False
            if buf:
                m = re.match(r"#\s*block:\s*(\S+)", buf[0].strip())
                if m:
                    out[m.group(1)] = "\n".join(buf)
            continue
        if inb:
            buf.append(ln)
    return out


BLOCKS = _blocks()


def _stub(bin_dir, name, script):
    path = os.path.join(bin_dir, name)
    with open(path, "w") as fh:
        fh.write("#!/bin/bash\n" + script + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


class _Sandbox(unittest.TestCase):
    """A temp world with stubbed system tools."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p5s8-exec-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.bin = os.path.join(self.tmp, "bin")
        self.G = os.path.join(self.tmp, "data", "ghrsst")
        self.ext = os.path.join(self.tmp, "external")
        for d in (self.bin, self.G, self.ext):
            os.makedirs(d, exist_ok=True)
        # In a real run the `art-root` block creates this; blocks that write artifacts are
        # exercised on their own here, so the sandbox provides it.
        os.makedirs(os.path.join(self.ext, "p5s8_TEST"), exist_ok=True)
        self.env = dict(os.environ)
        self.env["PATH"] = self.bin + os.pathsep + self.env["PATH"]
        self.env["G"] = self.G
        self.env["PYTHON"] = VENV_PY
        self.env["ART_ROOT"] = self.ext
        self.env["RUN_ID"] = "TEST"
        self.env["ART"] = os.path.join(self.ext, "p5s8_TEST")
        # Point the blocks at the real ops.quiescence helper rather than a copy.
        self.env["CODE_ROOT"] = _DEV
        # Default stubs: distinct devices, so the good path passes.
        _stub(self.bin, "findmnt", 'if [[ "$*" == *"'+self.G+'"* ]]; then echo /dev/prod; '
                                   'else echo /dev/other; fi')

    def run_block(self, tag, extra_env=None, **kw):
        self.assertIn(tag, BLOCKS, f"the runbook has no block tagged {tag}")
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run(["bash", "-c", BLOCKS[tag]], env=env, capture_output=True,
                              text=True, cwd=self.tmp, **kw)


# --------------------------------------------------------------------- 2. fail closed
class TestEveryBlockFailsClosed(unittest.TestCase):

    def test_every_tagged_block_sets_euo_pipefail(self):
        self.assertTrue(BLOCKS, "no tagged blocks found; the extractor or the tags are wrong")
        for tag, body in BLOCKS.items():
            with self.subTest(tag):
                self.assertIn("set -euo pipefail", body,
                              "an independently copyable block must fail closed")

    def test_positive_control_a_block_without_it_is_detected(self):
        self.assertNotIn("set -euo pipefail", "# block: x\ndf -h\n")

    def test_a_failing_pipeline_stage_is_not_swallowed(self):
        """`python … | tee` without `pipefail` reports tee's success and writes a truncated
        artifact. This asserts the shell option actually behaves as intended."""
        script = 'set -euo pipefail\nfalse | tee /dev/null\necho REACHED'
        r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("REACHED", r.stdout)


# --------------------------------------------------------------------- 1. artifact lifecycle
class TestArtifactLifecycleIsExecutable(_Sandbox):
    """The artifact root is created ON VM24 and copied back. The previous draft created it in
    a LOCAL block and then wrote to it from VM24 blocks — two different machines, so every
    later `> "$ART/..."` would have failed on a path that does not exist there."""

    def test_the_artifact_root_is_created_on_the_host_that_writes_it(self):
        r = self.run_block("art-root", extra_env={"ART": "", "RUN_ID": ""})
        self.assertEqual(r.returncode, 0, r.stderr)
        made = [d for d in os.listdir(self.ext) if d.startswith("p5s8_")]
        self.assertTrue(made, "the block did not create the artifact directory")

    def test_it_refuses_an_artifact_root_under_the_data_root(self):
        inside = os.path.join(self.G, "logs")
        r = self.run_block("art-root", extra_env={"ART_ROOT": inside, "ART": "", "RUN_ID": ""})
        self.assertNotEqual(r.returncode, 0, "an ART under $G was accepted")
        self.assertIn("NO-GO", r.stdout + r.stderr)

    def test_it_refuses_a_path_under_the_data_root_even_on_a_DIFFERENT_device(self):
        """The case that makes containment a separate check rather than a duplicate of the
        device check: a bind mount inside `$G` reports its own device, so the device comparison
        passes and only the containment test refuses it. Mutation testing found this — deleting
        the containment check broke nothing, because every other fixture put the path on the
        production device too."""
        inside = os.path.join(self.G, "bindmount")
        os.makedirs(inside, exist_ok=True)
        _stub(self.bin, "findmnt",
              f'if [[ "$*" == *"{inside}"* ]]; then echo /dev/bind; '
              f'elif [[ "$*" == *"{self.G}"* ]]; then echo /dev/prod; else echo /dev/other; fi')
        r = self.run_block("art-root", extra_env={"ART_ROOT": inside, "ART": "", "RUN_ID": ""})
        self.assertNotEqual(r.returncode, 0,
                            "a bind mount inside $G was accepted because its device differed")
        self.assertIn("under", (r.stdout + r.stderr))

    def test_it_refuses_an_artifact_root_on_the_production_device(self):
        _stub(self.bin, "findmnt", "echo /dev/prod")     # every path is the prod device
        r = self.run_block("art-root", extra_env={"ART": "", "RUN_ID": ""})
        self.assertNotEqual(r.returncode, 0, "an ART on the production device was accepted")
        self.assertIn("NO-GO", r.stdout + r.stderr)

    def test_it_refuses_when_findmnt_itself_fails(self):
        """An unreadable mount table is not a passing check. Without `set -e` the empty output
        compares equal to the other empty output and the guard passes."""
        _stub(self.bin, "findmnt", "exit 3")
        r = self.run_block("art-root", extra_env={"ART": "", "RUN_ID": ""})
        self.assertNotEqual(r.returncode, 0, "a failed findmnt was treated as a pass")

    def test_the_runbook_transports_artifacts_back_explicitly(self):
        with open(RUNBOOK) as fh:
            doc = fh.read()
        self.assertIn("scp", doc, "no copy-back step for artifacts created on the host")
        self.assertIn("copy-back", doc.lower())


# --------------------------------------------------------------------- 3. G4 probe
class TestG4ProbeFailsClosed(_Sandbox):

    def _curl(self, code="200", body='[{"date":"x","sst":1.0}]', rows=366):
        payload = json.dumps([{"date": f"d{i}", "sst": 1.0} for i in range(rows)]) \
            if rows is not None else body
        # Emulates `curl -s -w '\n%{http_code}'`: body, then the status on the last line.
        # The timing loop asks for `%{http_code} %{time_total}` and gets just those two.
        _stub(self.bin, "curl", f'''
args="$*"
if [[ "$args" == *"time_total"* ]]; then echo "{code} 0.012"; exit 0; fi
cat <<'JSON'
{payload}
JSON
if [[ "$args" == *"http_code"* ]]; then echo "{code}"; fi''')

    def test_the_good_path_passes(self):
        self._curl()
        r = self.run_block("g4-probe")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_non_200_probe_discards_the_run(self):
        self._curl(code="503")
        r = self.run_block("g4-probe")
        self.assertNotEqual(r.returncode, 0, "a 503 probe was accepted")
        self.assertIn("NO-GO", r.stdout + r.stderr)

    def test_a_wrong_row_count_discards_the_run(self):
        self._curl(rows=42)
        r = self.run_block("g4-probe")
        self.assertNotEqual(r.returncode, 0, "a 42-row response was accepted for a 366-day span")

    def test_a_malformed_json_body_discards_the_run(self):
        self._curl(rows=None, body="<html>gateway timeout</html>")
        r = self.run_block("g4-probe")
        self.assertNotEqual(r.returncode, 0, "an HTML error page was accepted as JSON")

    def test_a_json_shape_that_is_not_a_list_of_rows_discards_the_run(self):
        self._curl(rows=None, body='{"detail": "range too large"}')
        r = self.run_block("g4-probe")
        self.assertNotEqual(r.returncode, 0, "an error object was accepted as a row list")

    def test_a_span_that_is_not_exactly_366_days_discards_the_run(self):
        self._curl()
        r = self.run_block("g4-probe", extra_env={"FORCE_SPAN_DAYS": "365"})
        self.assertNotEqual(r.returncode, 0, "a 365-day span was accepted as 366")


# --------------------------------------------------------------------- 4. H7 pid set
class TestH7DetectsWorkerChanges(_Sandbox):

    def _pm2(self, sequences):
        """`pm2 jlist` returns a different pid set on each successive call."""
        state = os.path.join(self.tmp, "pm2_call")
        with open(state, "w") as fh:
            fh.write("0")
        payloads = " ".join(f"'{json.dumps(s)}'" for s in sequences)
        _stub(self.bin, "pm2", f'''
n=$(cat {state}); arr=({payloads})
i=$(( n < ${{#arr[@]}} ? n : ${{#arr[@]}} - 1 ))
echo $(( n + 1 )) > {state}
echo "${{arr[$i]}}"''')

    @staticmethod
    def _jlist(pids):
        return [{"name": "ghrsst", "pid": p, "pm2_env": {"status": "online"}} for p in pids]

    def _live(self, pids):
        """Fake /proc entries are not portable, so the sampler is pointed at a fake root."""
        root = os.path.join(self.tmp, "proc")
        for p in pids:
            d = os.path.join(root, str(p), "fd")
            os.makedirs(d, exist_ok=True)
            for k in range(3):
                _touch(os.path.join(d, str(k)))
            with open(os.path.join(root, str(p), "status"), "w") as fh:
                fh.write("VmRSS:\t 1024 kB\n")
        return root

    def test_a_stable_pid_set_samples_cleanly(self):
        self._pm2([self._jlist([101, 102])] * 4)
        r = self.run_block("h7-sample", extra_env={
            "PROC_ROOT": self._live([101, 102]), "SAMPLES": "3", "INTERVAL": "0"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_worker_ADDED_mid_window_aborts(self):
        self._pm2([self._jlist([101, 102]), self._jlist([101, 102, 103])])
        r = self.run_block("h7-sample", extra_env={
            "PROC_ROOT": self._live([101, 102, 103]), "SAMPLES": "3", "INTERVAL": "0"})
        self.assertNotEqual(r.returncode, 0, "a new worker mid-window was not detected")

    def test_a_worker_REMOVED_mid_window_aborts(self):
        self._pm2([self._jlist([101, 102]), self._jlist([101])])
        r = self.run_block("h7-sample", extra_env={
            "PROC_ROOT": self._live([101, 102]), "SAMPLES": "3", "INTERVAL": "0"})
        self.assertNotEqual(r.returncode, 0, "a lost worker mid-window was not detected")

    def test_a_REPLACED_pid_aborts_even_though_the_count_is_unchanged(self):
        """The subtle one: same number of workers, different processes. A count check passes."""
        self._pm2([self._jlist([101, 102]), self._jlist([101, 999])])
        r = self.run_block("h7-sample", extra_env={
            "PROC_ROOT": self._live([101, 102, 999]), "SAMPLES": "3", "INTERVAL": "0"})
        self.assertNotEqual(r.returncode, 0, "a replaced pid was accepted; the count was equal")

    def test_no_pids_at_all_aborts(self):
        self._pm2([[]])
        r = self.run_block("h7-sample", extra_env={
            "PROC_ROOT": self._live([]), "SAMPLES": "2", "INTERVAL": "0"})
        self.assertNotEqual(r.returncode, 0)

    def test_a_failing_pm2_aborts(self):
        _stub(self.bin, "pm2", "exit 1")
        r = self.run_block("h7-sample", extra_env={
            "PROC_ROOT": self._live([101]), "SAMPLES": "2", "INTERVAL": "0"})
        self.assertNotEqual(r.returncode, 0, "a failed pm2 jlist produced an accepted artifact")


# --------------------------------------------------------------------- G16 walk
class TestG16WalkFailsClosed(_Sandbox):

    def test_a_readable_tree_scans_cleanly(self):
        os.makedirs(os.path.join(self.G, "a", "b"), exist_ok=True)
        _touch(os.path.join(self.G, "a", "f.txt"))
        r = self.run_block("g16-scan")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("files", r.stdout)

    @unittest.skipIf(os.geteuid() == 0, "root ignores directory permissions")
    def test_an_unreadable_subtree_aborts_rather_than_under_reporting(self):
        blocked = os.path.join(self.G, "locked")
        os.makedirs(blocked, exist_ok=True)
        _touch(os.path.join(blocked, "hidden.txt"))
        os.chmod(blocked, 0o000)
        self.addCleanup(os.chmod, blocked, 0o755)
        r = self.run_block("g16-scan")
        self.assertNotEqual(r.returncode, 0, "a permission-denied subtree was silently skipped")
        self.assertIn("NO-GO", r.stdout + r.stderr)


# --------------------------------------------------------------------- 5. shadow completeness
#
# Shadow provisioning moved to `test_phase2_p5s8_artifacts.py`, where it is checked through the
# CANONICAL validator (`SegmentedCubeStore`) against a manifest built by the real publisher.
# The hand-rolled `os.path` fixture that used to live here could not exercise absolute-path
# refusal, `../` traversal or symlink escape -- the rules the shipped code already owns.

if __name__ == "__main__":
    unittest.main()
