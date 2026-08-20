"""dev2026 — P5-S8: run binding and real parsers for every producer output.

**LOCAL-ONLY. THE RUNBOOK IS NOT EXECUTED AGAINST VM24.** Sandboxed blocks, stubbed tools.

Round 5 made the sequence run. This round makes its outputs *provably belong to the run that
claims them*:

- `$ART` could be reused, so a producer file left by an earlier interrupted run satisfied the
  assembler exactly as a fresh one would;
- `_check_nonempty()` accepted any one-line string as a measurement series, so `"lol"` passed
  as 30 latency samples;
- only the G4 *probe* validated its response — every later sample checked HTTP 200 and timed
  whatever came back, so a service degrading to a short 200 mid-run produced a clean report.

Each is the same failure in a different place: something that looks like evidence, is not, and
carries no marker saying which run it came from.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
for p in (_DEV, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from test_phase2_p5s8_exec import BLOCKS, _stub  # noqa: E402
from test_phase2_p5s8_e2e import _E2E, SEQUENCE  # noqa: E402

RUN = "E2ETEST"


# ------------------------------------------------------------------ 1. run binding
class TestArtifactDirectoryIsFresh(_E2E):

    def test_art_root_creates_a_run_marker(self):
        env = dict(self.env)
        env.update({"PROFILE": "legacy_baseline", "ART": ""})
        r = subprocess.run(["bash", "-c", BLOCKS["art-root"]], env=env,
                           capture_output=True, text=True, cwd=self.tmp)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        marker = os.path.join(self.art, "run.json")
        self.assertTrue(os.path.isfile(marker), "no run marker was written")
        with open(marker) as fh:
            self.assertEqual(json.load(fh)["run_id"], RUN)

    def test_art_root_REFUSES_an_existing_directory(self):
        """A reused `$ART` is how an interrupted run's files become this run's evidence."""
        os.makedirs(self.art, exist_ok=True)
        with open(os.path.join(self.art, "leftover.txt"), "w") as fh:
            fh.write("from an earlier run\n")
        env = dict(self.env)
        env.update({"PROFILE": "legacy_baseline", "ART": ""})
        r = subprocess.run(["bash", "-c", BLOCKS["art-root"]], env=env,
                           capture_output=True, text=True, cwd=self.tmp)
        self.assertNotEqual(r.returncode, 0, "an existing artifact directory was reused")
        self.assertIn("NO-GO", r.stdout + r.stderr)

    def test_the_assembler_refuses_without_a_run_marker(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        os.remove(os.path.join(self.art, "run.json"))
        a = self.assemble("legacy_baseline")
        self.assertNotEqual(a.returncode, 0, "a run with no marker assembled a report")

    def test_the_assembler_refuses_a_marker_from_another_run(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        path = os.path.join(self.art, "run.json")
        with open(path) as fh:
            data = json.load(fh)
        data["run_id"] = "SOMETHING-ELSE"
        with open(path, "w") as fh:
            json.dump(data, fh)
        a = self.assemble("legacy_baseline")
        self.assertNotEqual(a.returncode, 0)


class TestEveryProducerOutputIsBoundToTheRun(_E2E):

    BOUND = ("g4_366d_seconds.txt", "h7_fd_rss_timeseries.txt", "g16_metadata_scan.txt",
             "g4_request.json", "capacity.json", "wal_state.json", "preflight.json")

    def test_each_required_artifact_carries_the_run_id(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        for name in self.BOUND:
            with self.subTest(name):
                path = os.path.join(self.art, name)
                self.assertTrue(os.path.isfile(path), f"{name} was not produced")
                with open(path) as fh:
                    self.assertIn(RUN, fh.read(), f"{name} carries no run binding")

    def test_a_producer_output_from_ANOTHER_run_is_refused(self):
        """The stale-reuse case, per artifact: real content, wrong run.

        `capacity.json` and `wal_state.json` are in the list deliberately: they have no
        validator of their own that checks a run id, so the assembler's generic binding is the
        ONLY thing standing between them and adoption from an earlier run. Mutation testing
        found that omitting them left that check unbound."""
        for name in ("g4_366d_seconds.txt", "h7_fd_rss_timeseries.txt",
                     "g16_metadata_scan.txt", "g4_request.json",
                     "capacity.json", "wal_state.json"):
            with self.subTest(name):
                self.setUp()
                self.assert_all_ok(self.run_sequence("legacy_baseline"))
                path = os.path.join(self.art, name)
                with open(path) as fh:
                    text = fh.read()
                with open(path, "w") as fh:
                    fh.write(text.replace(RUN, "STALE-RUN"))
                a = self.assemble("legacy_baseline")
                self.assertNotEqual(a.returncode, 0,
                                    f"{name} from another run was accepted")
                self.assertFalse(os.path.isfile(
                    os.path.join(self.art, "p5s8_readonly.json")))


class TestTheG4RequestIsRequiredAndValidated(_E2E):

    def test_it_is_a_required_legacy_artifact(self):
        from ops import p5s8_report as rep
        self.assertIn("g4_request.json", rep.required_artifacts("legacy_baseline"))

    def test_a_missing_request_refuses_assembly(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        os.remove(os.path.join(self.art, "g4_request.json"))
        self.assertNotEqual(self.assemble("legacy_baseline").returncode, 0)

    def test_an_EDITED_request_refuses_assembly(self):
        """Editing the recorded span after the fact would let a 30-day run be reported as 366."""
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        path = os.path.join(self.art, "g4_request.json")
        with open(path) as fh:
            rec = json.load(fh)
        rec["span_days"] = 30
        with open(path, "w") as fh:
            json.dump(rec, fh)
        self.assertNotEqual(self.assemble("legacy_baseline").returncode, 0)

    def test_a_sample_count_that_disagrees_with_the_series_refuses(self):
        """The cross-binding: the request records how many samples were intended, the series
        holds how many were taken. A truncated series is a shorter measurement, not the one
        claimed."""
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        path = os.path.join(self.art, "g4_366d_seconds.txt")
        with open(path) as fh:
            lines = fh.read().rstrip("\n").split("\n")
        with open(path, "w") as fh:
            fh.write("\n".join(lines[:-1]) + "\n")      # drop one sample
        self.assertNotEqual(self.assemble("legacy_baseline").returncode, 0)


# ------------------------------------------------------------------ 2. real parsers
class TestTextArtifactsAreParsedNotJustSized(_E2E):

    GARBAGE = ("lol\n", "\n", "   \n", "not a number\n", "-1\n", "nan\n", "inf\n")

    def _corrupt(self, name, text):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        with open(os.path.join(self.art, name), "w") as fh:
            fh.write(text)
        return self.assemble("legacy_baseline")

    def test_the_valid_series_pass(self):
        """Guards the guard: a suite that refuses everything proves nothing."""
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        a = self.assemble("legacy_baseline")
        self.assertEqual(a.returncode, 0, a.stdout + a.stderr)

    def test_a_one_line_string_is_not_a_latency_series(self):
        for junk in self.GARBAGE:
            with self.subTest(junk=junk.strip()):
                self.setUp()
                r = self._corrupt("g4_366d_seconds.txt", f"# run_id={RUN}\n{junk}")
                self.assertNotEqual(r.returncode, 0)

    def test_garbage_with_the_RIGHT_number_of_lines_is_still_refused(self):
        """The count check is not a value check. Two junk lines satisfy a two-sample series
        unless each value is actually parsed — which is what the mutation exposed."""
        self.setUp()
        r = self._corrupt("g4_366d_seconds.txt", f"# run_id={RUN} samples=2\nlol\nlol\n")
        self.assertNotEqual(r.returncode, 0)

    def test_a_series_shortened_CONSISTENTLY_is_still_refused(self):
        """Header and body edited together, so the file is self-consistent. Only the
        cross-binding to the request's planned sample count catches it."""
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        path = os.path.join(self.art, "g4_366d_seconds.txt")
        with open(path) as fh:
            lines = fh.read().rstrip("\n").split("\n")
        body = lines[1:-1]
        with open(path, "w") as fh:
            fh.write(f"# run_id={RUN} samples={len(body)} validated_rows=366\n")
            fh.write("\n".join(body) + "\n")
        self.assertNotEqual(self.assemble("legacy_baseline").returncode, 0)

    def test_wrong_columns_with_the_RIGHT_line_count_are_refused(self):
        """The pid column is held STABLE on purpose. With `1 2 3` / `4 5 6` the worker-stability
        check fires instead (2 vs 5) and the column check is never reached — mutation testing
        showed that masked it. Here every line reports the same worker count, so only the
        column-arity check can refuse."""
        self.setUp()
        r = self._corrupt("h7_fd_rss_timeseries.txt",
                          f"# run_id={RUN} samples=2\n1 2 3\n4 2 6\n")
        self.assertNotEqual(r.returncode, 0, "a 3-column series was accepted as fd/RSS samples")

    def test_a_negative_or_non_finite_latency_is_refused(self):
        for junk in ("-0.5", "nan", "inf", "1e400"):
            with self.subTest(junk):
                self.setUp()
                r = self._corrupt("g4_366d_seconds.txt",
                                  f"# run_id={RUN} samples=2\n0.01\n{junk}\n")
                self.assertNotEqual(r.returncode, 0)

    def test_a_malformed_fd_rss_series_is_refused(self):
        for junk in ("lol\n", "1 2 3\n", "a b c d\n", "1 2 -3 4\n", "1 0 3 4\n"):
            with self.subTest(junk=junk.strip()):
                self.setUp()
                r = self._corrupt("h7_fd_rss_timeseries.txt", f"# run_id={RUN} samples=2\n{junk}")
                self.assertNotEqual(r.returncode, 0)

    def test_a_worker_count_that_changes_mid_series_is_refused(self):
        self.setUp()
        r = self._corrupt("h7_fd_rss_timeseries.txt",
                          f"# run_id={RUN} pids=[1,2] samples=2\n"
                          "100 2 6 4096\n101 3 9 6144\n")
        self.assertNotEqual(r.returncode, 0, "a changing worker count was accepted")

    def test_a_malformed_metadata_scan_is_refused(self):
        for junk in ("lol\n", "files=x dirs=1 wall_s=1\n", "files=-1 dirs=1 wall_s=1\n",
                     "files=1 dirs=1\n", "files=1 dirs=1 wall_s=nan\n"):
            with self.subTest(junk=junk.strip()):
                self.setUp()
                r = self._corrupt("g16_metadata_scan.txt", f"# run_id={RUN}\n{junk}")
                self.assertNotEqual(r.returncode, 0)

    def test_the_metadata_scan_records_the_root_it_walked(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        with open(os.path.join(self.art, "g16_metadata_scan.txt")) as fh:
            text = fh.read()
        self.assertIn("root=", text)
        self.assertIn(self.G, text)


# ------------------------------------------------------------------ 3. per-sample validation
class TestEverySampledResponseIsValidated(_E2E):

    def _degrading_curl(self, good_calls=1):
        """Valid 366-row responses for the probe, then a short 200 for later samples."""
        state = os.path.join(self.tmp, "curl_calls")
        with open(state, "w") as fh:
            fh.write("0")
        import datetime as _dt
        _end = _dt.date.today()
        good = json.dumps([{"date": (_end - _dt.timedelta(days=365 - i)).isoformat(),
                            "sst": 1.0} for i in range(366)])
        short = json.dumps([{"date": _end.isoformat(), "sst": 1.0}])
        _stub(self.bin, "curl", f'''
n=$(cat {state}); echo $((n+1)) > {state}
if [ "$n" -lt {good_calls} ]; then BODY='{good}'; else BODY='{short}'; fi
args="$*"
printf '%s' "$BODY"
if [[ "$args" == *"time_total"* ]]; then printf '\\n200 0.011';
elif [[ "$args" == *"http_code"* ]]; then printf '\\n200'; fi''')

    def test_a_short_200_during_sampling_refuses_the_run(self):
        """The probe passes, then the service degrades to a valid-looking short response. Only
        checking the status code would time it and report a clean run."""
        self._degrading_curl(good_calls=1)
        res = self.run_sequence("legacy_baseline")
        failed = [tag for tag, r in res if r.returncode != 0]
        self.assertIn("g4-sample", failed,
                      "a short 200 was accepted as a 366-day latency sample")
        self.assertFalse(os.path.isfile(os.path.join(self.art, "p5s8_readonly.json")))

    def test_the_undegraded_path_still_samples_cleanly(self):
        self._degrading_curl(good_calls=99)
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        a = self.assemble("legacy_baseline")
        self.assertEqual(a.returncode, 0, a.stdout + a.stderr)

    def test_the_SAMPLER_itself_refuses_a_request_from_another_run(self):
        """Tested at the sampler, not only at the assembler. The assembler's own check would
        catch it later, but a sampler that times a stale request has already spent 30 requests
        against production before anyone finds out."""
        res = self.run_sequence("legacy_baseline", stop_after="g4-probe")
        self.assert_all_ok(res)
        path = os.path.join(self.art, "g4_request.json")
        with open(path) as fh:
            req = json.load(fh)
        req["run_id"] = "STALE-RUN"
        with open(path, "w") as fh:
            json.dump(req, fh)
        env = dict(self.env)
        env.update({"PROFILE": "legacy_baseline", "ART": self.art})
        r = subprocess.run(["bash", "-c", BLOCKS["g4-sample"]], env=env,
                           capture_output=True, text=True, cwd=self.tmp)
        self.assertNotEqual(r.returncode, 0, "the sampler used a request from another run")
        self.assertIn("NO-GO", r.stdout + r.stderr)

    def test_the_sampler_records_its_per_sample_validation(self):
        """The evidence that each response was checked, not just the timings."""
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        with open(os.path.join(self.art, "g4_366d_seconds.txt")) as fh:
            header = fh.readline()
        self.assertIn("validated_rows=366", header)


# ------------------------------------------------------------------ 4. handles
class TestNoUnmanagedHandlesInTheBlocks(unittest.TestCase):

    def test_no_block_opens_a_file_outside_a_context_manager(self):
        """`json.load(open(...))` in a runbook leaks exactly as it does in a test."""
        import re
        for tag, body in BLOCKS.items():
            with self.subTest(tag):
                self.assertNotIn("json.load(open(", body)
                stray = re.findall(r"(?<!with )\bopen\([^)]*\)\s*(?:\.|\))", body)
                self.assertEqual([s for s in stray if "with open" not in s], [],
                                 f"unmanaged open() in block {tag}")


if __name__ == "__main__":
    unittest.main()
