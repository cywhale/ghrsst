"""dev2026 — P5-S8: no coercion, and every recorded fact bound to the request that produced it.

**LOCAL-ONLY. THE RUNBOOK IS NOT EXECUTED AGAINST VM24.** Parser-level tests plus sandboxed
blocks; no host is contacted.

Three coercions and one missing binding, all the same family as the ones this project keeps
finding — a value that is *accepted* rather than *established*:

- `int(float("2.5"))` is `2`. A worker count of `2.5` is not a worker count, and truncating it
  into a legal one hides whatever produced it.
- `g4_request.json` was checked for non-empty strings, so a record could keep `span_days: 366`
  while its dates, URL, coordinates or `append` said something else entirely — and the sampler
  would then spend thirty production requests on it.
- `pm2_jlist.json` was written and never read. The roster that names which processes the fd/RSS
  series describes was, itself, unexamined.

Every class carries a **valid control**: a validator that rejects everything is not a validator.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
for p in (_DEV, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from ops import p5s8_report as rep  # noqa: E402
from test_phase2_p5s8_exec import BLOCKS  # noqa: E402
from test_phase2_p5s8_e2e import _E2E  # noqa: E402

RUN = "E2ETEST"
#: Tokens that `int(float(...))` turns into a legal count, plus the other shapes an integer
#: field must refuse outright.
#: Whitespace forms are tested against the token validator directly rather than through a
#: series line: the line format is whitespace-delimited, so `" 2"` cannot survive `.split()`
#: and the fixture would be asserting something the format cannot express.
NOT_EXACT_INTS = ("2.5", "1.9", "2.0", "2e0", "+2", "-2", "0x2", "True", "true",
                  "", "nan", "inf", "1_000")
WHITESPACE_FORMS = (" 2", "2 ", "\t2", "2\n")


class _Tmp(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p5s8-val-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, ignore_errors=True)

    def write(self, name, text):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            fh.write(text)
        return path


# ------------------------------------------------------------------ 1. no coercion
class TestCountsAreExactIntegers(_Tmp):

    def _h7(self, npids="2", ts="100", fds="6", rss="4096", samples=1):
        return self.write("h7.txt",
                          f"# run_id={RUN} pids=[1,2] samples={samples}\n{ts} {npids} {fds} {rss}\n")

    def test_a_valid_series_passes(self):
        out = rep.check_h7_series(self._h7(), "h7.txt", run_id=RUN)
        self.assertEqual(out["samples"], 1)
        self.assertEqual(out["workers"], 2)

    def test_a_fractional_worker_count_is_refused_not_truncated(self):
        """`int(float("2.5"))` is 2. The count becomes legal by being rounded, which is how a
        parser stops being able to tell you anything went wrong."""
        for token in NOT_EXACT_INTS:
            with self.subTest(token=repr(token)):
                with self.assertRaises(rep.ArtifactError):
                    rep.check_h7_series(self._h7(npids=token), "h7.txt", run_id=RUN)

    def test_the_token_validator_refuses_whitespace_padding(self):
        for token in WHITESPACE_FORMS:
            with self.subTest(token=repr(token)):
                with self.assertRaises(rep.ArtifactError):
                    rep._exact_nonneg_int(token, "x", "count")
        self.assertEqual(rep._exact_nonneg_int("2", "x", "count"), 2)

    def test_every_h7_column_requires_an_exact_integer(self):
        for column, kwargs in (("timestamp", {"ts": "1.5"}), ("fds", {"fds": "6.7"}),
                               ("rss", {"rss": "4096.1"})):
            with self.subTest(column):
                with self.assertRaises(rep.ArtifactError):
                    rep.check_h7_series(self._h7(**kwargs), "h7.txt", run_id=RUN)

    def _g16(self, files="2", dirs="1", wall="0.5"):
        return self.write("g16.txt",
                          f"# run_id={RUN}\nroot=/x files={files} dirs={dirs} wall_s={wall}\n")

    def test_a_valid_scan_passes(self):
        out = rep.check_g16_scan(self._g16(), "g16.txt", run_id=RUN)
        self.assertEqual((out["files"], out["dirs"]), (2, 1))

    def test_fractional_file_and_dir_counts_are_refused(self):
        for token in NOT_EXACT_INTS:
            with self.subTest(token=repr(token)):
                with self.assertRaises(rep.ArtifactError):
                    rep.check_g16_scan(self._g16(files=token), "g16.txt", run_id=RUN)
                with self.assertRaises(rep.ArtifactError):
                    rep.check_g16_scan(self._g16(dirs=token), "g16.txt", run_id=RUN)

    def test_wall_seconds_may_be_fractional_because_it_is_a_duration(self):
        """The rule is exactness where the value is a COUNT, not everywhere."""
        rep.check_g16_scan(self._g16(wall="0.001"), "g16.txt", run_id=RUN)
        with self.assertRaises(rep.ArtifactError):
            rep.check_g16_scan(self._g16(wall="-0.1"), "g16.txt", run_id=RUN)
        with self.assertRaises(rep.ArtifactError):
            rep.check_g16_scan(self._g16(wall="nan"), "g16.txt", run_id=RUN)


# ------------------------------------------------------------------ 2. g4_request binding
class TestTheG4RequestDescribesTheRequestActuallyMade(_Tmp):

    def _req(self, **over):
        rec = {"run_id": RUN, "start": "2026-01-01", "end": "2027-01-01",
               "url": ("http://127.0.0.1:8080/api/ghrsst"
                       "?lon0=135&lat0=20&start=2026-01-01&end=2027-01-01&append=sst"),
               "span_days": 366, "probe_rows": 366, "g4_samples": 2}
        rec.update(over)
        return rec

    def _refuse(self, **over):
        with self.assertRaises(rep.ArtifactError):
            rep.check_g4_request(self._req(**over), run_id=RUN)

    def test_the_valid_control_passes(self):
        rep.check_g4_request(self._req(), run_id=RUN)

    def test_dates_must_be_canonical_iso(self):
        for bad in ("2026-1-1", "01/01/2026", "2026-01-01T00:00:00", "20260101", "not-a-date"):
            with self.subTest(bad):
                self._refuse(start=bad)

    def test_a_NON_CANONICAL_but_parseable_date_is_refused(self):
        """`date.fromisoformat` accepts `20260101` and `2026-W01-1`. Mutation testing showed the
        canonical check was never actually reached: every other bad date raised in the parser,
        and the compact form was caught by the URL cross-check instead. So the URL is written to
        agree, leaving the canonicity rule as the only thing that can refuse it — otherwise two
        artifacts could record the same day in two spellings and compare unequal."""
        compact = ("http://127.0.0.1:8080/api/ghrsst?lon0=135&lat0=20"
                   "&start=20260101&end=2027-01-01&append=sst")
        self._refuse(start="20260101", url=compact)

    def test_the_span_arithmetic_must_agree_with_span_days(self):
        """The blocking case: `span_days` stays 366 while the dates say something else."""
        self._refuse(start="2026-06-01",
                     url=("http://127.0.0.1:8080/api/ghrsst?lon0=135&lat0=20"
                          "&start=2026-06-01&end=2027-01-01&append=sst"))

    def test_the_url_must_be_the_expected_local_endpoint(self):
        for bad_host in ("http://vm24:8080/api/ghrsst", "https://127.0.0.1:8080/api/ghrsst",
                         "http://127.0.0.1:9999/api/ghrsst", "http://127.0.0.1:8080/api/other"):
            with self.subTest(bad_host):
                self._refuse(url=bad_host + "?lon0=135&lat0=20&start=2026-01-01"
                                            "&end=2027-01-01&append=sst")

    def test_every_required_query_parameter_must_be_present(self):
        for drop in ("lon0", "lat0", "start", "end", "append"):
            with self.subTest(drop):
                params = {"lon0": "135", "lat0": "20", "start": "2026-01-01",
                          "end": "2027-01-01", "append": "sst"}
                params.pop(drop)
                query = "&".join(f"{k}={v}" for k, v in params.items())
                self._refuse(url=f"http://127.0.0.1:8080/api/ghrsst?{query}")

    def test_the_url_dates_must_match_the_recorded_dates(self):
        """A record whose URL asks for a different window than it claims to have measured."""
        self._refuse(url=("http://127.0.0.1:8080/api/ghrsst?lon0=135&lat0=20"
                          "&start=2026-06-01&end=2026-06-30&append=sst"))

    def test_span_days_and_probe_rows_stay_pinned_to_366(self):
        self._refuse(span_days=365)
        self._refuse(probe_rows=365)

    def test_the_sampler_refuses_an_edited_request_BEFORE_calling_production(self):
        """Both sides, as required: the assembler catches it afterwards, but by then thirty
        requests have already been made against the service."""
        self.assertIn("check_g4_request", BLOCKS["g4-sample"])


class TestTheSamplerRejectsAnEditedRequest(_E2E):

    def test_an_edited_url_is_refused_before_any_sample(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline", stop_after="g4-probe"))
        path = os.path.join(self.art, "g4_request.json")
        with open(path) as fh:
            req = json.load(fh)
        req["url"] = req["url"].replace("lon0=135", "lon0=999")
        with open(path, "w") as fh:
            json.dump(req, fh)
        env = dict(self.env)
        env.update({"PROFILE": "legacy_baseline", "ART": self.art})
        r = subprocess.run(["bash", "-c", BLOCKS["g4-sample"]], env=env,
                           capture_output=True, text=True, cwd=self.tmp)
        self.assertNotEqual(r.returncode, 0, "the sampler used an edited request")
        self.assertFalse(os.path.isfile(os.path.join(self.art, "g4_366d_seconds.txt")))

    def test_the_unedited_request_still_samples(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        self.assertTrue(os.path.isfile(os.path.join(self.art, "g4_366d_seconds.txt")))


# ------------------------------------------------------------------ 3. pm2 roster
class TestThePm2RosterIsValidatedAndBound(_Tmp):

    def _roster(self, procs=None, **over):
        rec = {"run_id": RUN,
               "roster": procs if procs is not None else
               [{"name": "ghrsst", "pid": 101, "pm2_env": {"status": "online"}},
                {"name": "ghrsst", "pid": 102, "pm2_env": {"status": "online"}}]}
        rec.update(over)
        return rec

    def _refuse(self, **kw):
        with self.assertRaises(rep.ArtifactError):
            rep.check_pm2_roster(self._roster(**kw), run_id=RUN)

    def test_the_valid_control_passes(self):
        pids = rep.check_pm2_roster(self._roster(), run_id=RUN)
        self.assertEqual(pids, {101, 102})

    def test_an_empty_or_absent_roster_is_refused(self):
        self._refuse(procs=[])
        with self.assertRaises(rep.ArtifactError):
            rep.check_pm2_roster({}, run_id=RUN)
        with self.assertRaises(rep.ArtifactError):
            rep.check_pm2_roster([], run_id=RUN)

    def test_a_roster_without_the_ghrsst_app_is_refused(self):
        self._refuse(procs=[{"name": "other", "pid": 1, "pm2_env": {"status": "online"}}])

    def test_an_offline_app_is_refused(self):
        self._refuse(procs=[{"name": "ghrsst", "pid": 1, "pm2_env": {"status": "stopped"}}])

    def test_non_integer_or_non_positive_pids_are_refused(self):
        for bad in ("101", 101.0, True, 0, -1, None):
            with self.subTest(bad):
                self._refuse(procs=[{"name": "ghrsst", "pid": bad,
                                     "pm2_env": {"status": "online"}}])

    def test_duplicate_pids_are_refused(self):
        self._refuse(procs=[{"name": "ghrsst", "pid": 7, "pm2_env": {"status": "online"}},
                            {"name": "ghrsst", "pid": 7, "pm2_env": {"status": "online"}}])

    def test_a_roster_from_another_run_is_refused(self):
        self._refuse(run_id="STALE")


class TestTheRosterIsCrossCheckedAgainstTheSeries(_Tmp):

    def _series(self, header_pids="[101,102]", npids="2", samples=1):
        return self.write("h7.txt", f"# run_id={RUN} pids={header_pids} samples={samples}\n"
                                    f"100 {npids} 6 4096\n")

    def test_a_matching_roster_and_series_pass(self):
        out = rep.check_h7_series(self._series(), "h7.txt", run_id=RUN,
                                  expected_pids={101, 102})
        self.assertEqual(out["workers"], 2)

    def test_a_header_pid_set_that_disagrees_with_the_roster_is_refused(self):
        with self.assertRaises(rep.ArtifactError):
            rep.check_h7_series(self._series(header_pids="[101,999]"), "h7.txt",
                                run_id=RUN, expected_pids={101, 102})

    def test_a_sampled_worker_count_that_disagrees_with_the_roster_is_refused(self):
        """The row count of workers must match how many the roster names — a series describing
        three processes when PM2 knew two describes a different service."""
        with self.assertRaises(rep.ArtifactError):
            rep.check_h7_series(self._series(npids="3"), "h7.txt", run_id=RUN,
                                expected_pids={101, 102})

    def test_the_header_pid_list_must_parse(self):
        for bad in ("[]", "[a,b]", "101,102", "[101,", "[1.5]"):
            with self.subTest(bad):
                with self.assertRaises(rep.ArtifactError):
                    rep.check_h7_series(self._series(header_pids=bad), "h7.txt",
                                        run_id=RUN, expected_pids={101, 102})


if __name__ == "__main__":
    unittest.main()
