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
               "url": rep.g4_url("2026-01-01", "2027-01-01"),
               "span_days": 366, "probe_rows": 366, "g4_samples": 2,
               "healthz_url": rep.HEALTHZ_URL, "point_latest": "2027-01-01",
               "point_day_count": 1326}
        rec.update(over)
        return rec

    def _refuse(self, **over):
        with self.assertRaises(rep.ArtifactError):
            rep.check_g4_request(self._req(**over), run_id=RUN)

    def test_the_valid_control_passes(self):
        rep.check_g4_request(self._req(), run_id=RUN)

    def test_the_canonical_endpoint_is_the_deployed_one(self):
        """VM24 serves gunicorn on 127.0.0.1:8035 (`conf/start_app.sh -b 127.0.0.1:8035`).
        Staging found the contract pinned 8080, where nothing listens, so `g4-probe` could not
        run at all. Pinned by value still — this run targets the canonical VM24 service
        endpoint, not an arbitrary one."""
        self.assertEqual(rep.G4_ENDPOINT, "http://127.0.0.1:8035/api/ghrsst")

    def test_a_request_on_8035_is_ACCEPTED(self):
        rep.check_g4_request(self._req(), run_id=RUN)          # positive regression

    def test_a_request_on_the_stale_8080_endpoint_is_REFUSED(self):
        """The negative half: the old pin must now be rejected, not merely unused."""
        self._refuse(url=("http://127.0.0.1:8080/api/ghrsst?lon0=135&lat0=20"
                          "&start=2026-01-01&end=2027-01-01&append=sst"))

    def test_the_runbook_and_the_validator_share_one_endpoint(self):
        """No second hardcoded URL: the probe builds its request from the shared constant."""
        code = "\n".join(BLOCKS[tag] for tag in ("g4-probe", "g4-sample"))
        self.assertNotIn("8080", code)
        self.assertNotIn("http://127.0.0.1", code, "the probe hardcodes an endpoint")
        self.assertIn("g4_url", code)

    def test_no_stale_endpoint_survives_anywhere_in_the_contract(self):
        import re as _re
        for rel in ("ops/p5s8_report.py", "specs/p5s8_vm24_shadow_runbook.md"):
            with self.subTest(rel):
                with open(os.path.join(_DEV, rel)) as fh:
                    body = fh.read()
                live = [ln for ln in body.split("\n")
                        if "127.0.0.1:8080" in ln and "stale" not in ln.lower()
                        and "8035" not in ln]
                self.assertEqual(live, [], f"a stale 8080 endpoint survives in {rel}")

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
        compact = (rep.G4_ENDPOINT + "?lon0=135&lat0=20"
                   "&start=20260101&end=2027-01-01&append=sst")
        self._refuse(start="20260101", url=compact)

    def test_the_span_arithmetic_must_agree_with_span_days(self):
        """The blocking case: `span_days` stays 366 while the dates say something else."""
        # end stays at point_latest so the window-authority rule cannot be what fires here;
        # only the span arithmetic distinguishes this case.
        self._refuse(start="2026-06-01", url=rep.g4_url("2026-06-01", "2027-01-01"),
                     span_days=366)

    def test_the_url_must_be_the_expected_local_endpoint(self):
        for bad_host in ("http://vm24:8035/api/ghrsst", "https://127.0.0.1:8035/api/ghrsst",
                         "http://127.0.0.1:9999/api/ghrsst", "http://127.0.0.1:8080/api/ghrsst",
                         "http://127.0.0.1:8035/api/other"):
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
                self._refuse(url=f"{rep.G4_ENDPOINT}?{query}")

    def test_the_url_dates_must_match_the_recorded_dates(self):
        """A record whose URL asks for a different window than it claims to have measured."""
        self._refuse(url=rep.g4_url("2026-06-01", "2026-06-30"))  # url dates != record dates

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


# ------------------------------------------------------------------ 2b. healthz window authority
class TestHealthzIsTheWindowAuthority(_Tmp):
    """The aborted VM24 run derived `end = date.today()` = 2026-08-20 while the service's
    `point_latest` was 2026-08-18, so the API returned 364 rows and the probe refused — correctly,
    but for a window nothing should have asked for. The host clock does not decide what data
    exists; `/healthz.point_latest` does."""

    def _hz(self, **over):
        rec = {"status": "ok", "point_latest": "2026-08-18", "point_day_count": 1326,
               "point_earliest": "2023-01-01", "latest": "2026-08-18",
               "daily_latest": "2026-08-18"}
        rec.update(over)
        return rec

    def _refuse(self, **over):
        with self.assertRaises(rep.ArtifactError):
            rep.check_healthz(self._hz(**over))

    def test_the_valid_control_passes(self):
        out = rep.check_healthz(self._hz())
        self.assertEqual(out["point_latest"], "2026-08-18")
        self.assertEqual(out["point_day_count"], 1326)

    def test_the_endpoint_is_the_canonical_service_on_8035(self):
        self.assertEqual(rep.HEALTHZ_URL, "http://127.0.0.1:8035/healthz")

    def test_a_non_object_body_is_refused(self):
        for bad in ([], "ok", None, 7):
            with self.subTest(bad):
                with self.assertRaises(rep.ArtifactError):
                    rep.check_healthz(bad)

    def test_point_latest_must_be_present_and_canonical(self):
        for bad in (None, "", "2026-8-18", "20260818", "2026-08-18T00:00:00", 20260818):
            with self.subTest(bad):
                self._refuse(point_latest=bad)
        hz = self._hz(); hz.pop("point_latest")
        with self.assertRaises(rep.ArtifactError):
            rep.check_healthz(hz)

    def test_the_generic_latest_fields_are_NOT_used_as_the_authority(self):
        """`latest` and `daily_latest` are different questions. Only `point_latest` states how
        far the POINT history reaches, which is what a 366-day point request depends on."""
        hz = self._hz(); hz.pop("point_latest")
        hz["latest"] = "2026-08-20"; hz["daily_latest"] = "2026-08-20"
        with self.assertRaises(rep.ArtifactError):
            rep.check_healthz(hz)

    def test_point_day_count_is_required_and_strictly_validated(self):
        for bad in (None, "1326", 1326.0, True, 0, -5):
            with self.subTest(bad):
                self._refuse(point_day_count=bad)
        hz = self._hz(); hz.pop("point_day_count")
        with self.assertRaises(rep.ArtifactError):
            rep.check_healthz(hz)

    def test_a_day_count_too_small_for_the_window_is_refused(self):
        """Fewer than 366 point days means the 366-day window cannot exist, whatever
        `point_latest` says."""
        self._refuse(point_day_count=100)


class TestTheG4WindowComesFromHealthz(_Tmp):

    def test_the_window_ends_at_point_latest_not_at_the_host_date(self):
        start, end = rep.g4_window("2026-08-18")
        self.assertEqual(end, "2026-08-18")
        self.assertEqual(start, "2025-08-18")

    def test_the_span_is_exactly_366_days(self):
        import datetime as dt
        start, end = rep.g4_window("2026-08-18")
        span = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1
        self.assertEqual(span, 366)

    def test_a_later_host_date_does_not_move_the_window(self):
        """Whatever today is, the window is anchored to the service's own point_latest."""
        for anchor in ("2026-08-18", "2025-01-01", "2030-06-30"):
            with self.subTest(anchor):
                self.assertEqual(rep.g4_window(anchor)[1], anchor)

    def test_a_non_canonical_anchor_is_refused(self):
        for bad in ("20260818", "2026-8-18", "", None):
            with self.subTest(bad):
                with self.assertRaises(rep.ArtifactError):
                    rep.g4_window(bad)


class TestTheRequestIsBoundToTheHealthzWindow(_Tmp):

    def _req(self, **over):
        rec = {"run_id": RUN, "start": "2025-08-18", "end": "2026-08-18",
               "url": rep.g4_url("2025-08-18", "2026-08-18"),
               "span_days": 366, "probe_rows": 366, "g4_samples": 2,
               "healthz_url": rep.HEALTHZ_URL, "point_latest": "2026-08-18",
               "point_day_count": 1326}
        rec.update(over)
        return rec

    def _refuse(self, **over):
        with self.assertRaises(rep.ArtifactError):
            rep.check_g4_request(self._req(**over), run_id=RUN)

    def test_the_valid_control_passes(self):
        rep.check_g4_request(self._req(), run_id=RUN)

    def test_the_healthz_window_fields_are_required(self):
        for field in ("healthz_url", "point_latest", "point_day_count"):
            with self.subTest(field):
                rec = self._req(); rec.pop(field)
                with self.assertRaises(rep.ArtifactError):
                    rep.check_g4_request(rec, run_id=RUN)

    def test_the_end_date_must_equal_point_latest(self):
        """The aborted run in one assertion: end=2026-08-20 against point_latest=2026-08-18."""
        self._refuse(start="2025-08-20", end="2026-08-20",
                     url=rep.g4_url("2025-08-20", "2026-08-20"))

    def test_a_healthz_url_that_is_not_the_canonical_one_is_refused(self):
        self._refuse(healthz_url="http://127.0.0.1:8080/healthz")

    def test_an_edited_point_latest_alone_is_refused(self):
        """Editing the recorded authority without the window it produced."""
        self._refuse(point_latest="2026-08-20")


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
