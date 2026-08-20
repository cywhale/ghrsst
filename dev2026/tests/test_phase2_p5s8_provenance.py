"""dev2026 — P5-S8: response rows, scan root and build identity, each bound to its own evidence.

**LOCAL-ONLY. THE RUNBOOK IS NOT EXECUTED AGAINST VM24.** Sandboxed blocks and stubbed tools.

Three facts were recorded and then trusted rather than checked:

- a G4 response was accepted as "366 rows" if it was a list of length 366 — a list of 366
  integers, or one repeating the same day 366 times, satisfied it exactly as the real answer
  would;
- `check_g16_scan` required a non-empty `root` string, so `root=/tmp/small-tree files=10` was a
  perfectly valid metadata scan of the wrong tree;
- a 40-character SHA in the preflight proves the *preflight* holds a SHA, not that the identity
  block ever verified that commit, or that the modules it imported came from `$CODE_ROOT`.

Producer and validator share one schema in `ops/p5s8_report.py`: a runbook that re-implements a
rule drifts from the code that enforces it, which is how a check ends up passing in one place
and failing in the other.
"""
from __future__ import annotations

import hashlib
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

from ops import p5s8_report as rep  # noqa: E402
from test_phase2_p5s8_exec import BLOCKS  # noqa: E402
from test_phase2_p5s8_e2e import _E2E  # noqa: E402

RUN = "E2ETEST"
START, END = "2026-01-01", "2027-01-01"


def _rows(n=366, start=START):
    import datetime as dt
    d0 = dt.date.fromisoformat(start)
    return [{"date": (d0 + dt.timedelta(days=i)).isoformat(), "sst": 1.0} for i in range(n)]


# ------------------------------------------------------------------ 1. response rows
class TestTheG4ResponseIsValidatedRowByRow(unittest.TestCase):

    def _refuse(self, rows, start=START, end=END):
        with self.assertRaises(rep.ArtifactError):
            rep.check_g4_response(rows, start=start, end=end)

    def test_the_valid_control_passes(self):
        out = rep.check_g4_response(_rows(), start=START, end=END)
        self.assertEqual(out["rows"], 366)

    def test_a_list_of_366_SCALARS_is_refused(self):
        """The exact shape the length check accepted: right count, no rows at all."""
        self._refuse(list(range(366)))

    def test_a_row_missing_a_required_field_is_refused(self):
        rows = _rows()
        del rows[10]["sst"]
        self._refuse(rows)
        rows = _rows()
        del rows[10]["date"]
        self._refuse(rows)

    def test_a_non_canonical_date_is_refused(self):
        rows = _rows()
        rows[5]["date"] = "20260106"
        self._refuse(rows)

    def test_a_DUPLICATE_date_is_refused_even_at_the_right_length(self):
        """366 rows, all real, one day counted twice and another absent."""
        rows = _rows()
        rows[7]["date"] = rows[6]["date"]
        self._refuse(rows)

    def test_a_MISSING_day_is_refused_even_at_the_right_length(self):
        rows = _rows(365) + [{"date": "2030-01-01", "sst": 1.0}]
        self._refuse(rows)

    def test_the_date_set_must_run_from_start_through_end(self):
        self._refuse(_rows(366, start="2026-02-01"))

    def test_a_short_or_long_response_is_refused(self):
        self._refuse(_rows(365))
        self._refuse(_rows(367))

    def test_both_paths_use_the_SAME_validator(self):
        """A runbook that re-implements the rule drifts from the code that enforces it."""
        for tag in ("g4-probe", "g4-sample"):
            with self.subTest(tag):
                self.assertIn("check_g4_response", BLOCKS[tag])


class TestTheSamplerRefusesADegradedResponseShape(_E2E):

    def _curl_returning(self, payload_after_probe):
        state = os.path.join(self.tmp, "curl_calls")
        with open(state, "w") as fh:
            fh.write("0")
        import datetime as dt
        from test_phase2_p5s8_e2e import POINT_LATEST
        end = dt.date.fromisoformat(POINT_LATEST)
        good = json.dumps([{"date": (end - dt.timedelta(days=365 - i)).isoformat(),
                            "sst": 1.0} for i in range(366)])
        healthz = json.dumps({"status": "ok", "point_latest": POINT_LATEST,
                              "point_day_count": 1326})
        from test_phase2_p5s8_exec import _stub
        _stub(self.bin, "curl", f'''
args="$*"
if [[ "$args" == *"/healthz"* ]]; then printf '%s' '{healthz}'; printf '\\n200'; exit 0; fi
n=$(cat {state}); echo $((n+1)) > {state}
if [ "$n" -lt 1 ]; then BODY='{good}'; else BODY='{payload_after_probe}'; fi
printf '%s' "$BODY"
if [[ "$args" == *"time_total"* ]]; then printf '\\n200 0.011';
elif [[ "$args" == *"http_code"* ]]; then printf '\\n200'; fi''')

    def test_a_366_element_scalar_list_during_sampling_refuses(self):
        self._curl_returning(json.dumps(list(range(366))))
        res = self.run_sequence("legacy_baseline")
        self.assertIn("g4-sample", [tag for tag, r in res if r.returncode != 0])
        self.assertFalse(os.path.isfile(os.path.join(self.art, "g4_366d_seconds.txt")))

    def test_the_undegraded_path_still_samples(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        self.assertTrue(os.path.isfile(os.path.join(self.art, "g4_366d_seconds.txt")))


# ------------------------------------------------------------------ 2. scan root
class TestTheScanRootIsBoundToTheDataRoot(_E2E):

    def test_the_valid_control_passes(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        self.assertEqual(self.assemble("legacy_baseline").returncode, 0)

    def test_the_capacity_record_carries_the_data_root(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        with open(os.path.join(self.art, "capacity.json")) as fh:
            self.assertEqual(json.load(fh)["data_root"], os.path.realpath(self.G))

    def test_the_preflight_carries_the_data_root(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        with open(os.path.join(self.art, "preflight.json")) as fh:
            self.assertEqual(json.load(fh)["data_root"], os.path.realpath(self.G))

    def test_a_scan_of_a_DIFFERENT_tree_is_refused(self):
        """Only the root changes: run_id valid, counts valid, wall time valid."""
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        path = os.path.join(self.art, "g16_metadata_scan.txt")
        with open(path, "w") as fh:
            fh.write(f"# run_id={RUN}\nroot=/tmp/small-tree files=10 dirs=2 wall_s=0.1\n")
        a = self.assemble("legacy_baseline")
        self.assertNotEqual(a.returncode, 0, "a scan of another tree was accepted")
        self.assertFalse(os.path.isfile(os.path.join(self.art, "p5s8_readonly.json")))


# ------------------------------------------------------------------ 3. identity
class TestIdentityIsRecordedAndBound(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p5s8-ident-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def _identity(self, **over):
        code = os.path.join(self.tmp, "dev2026")
        os.makedirs(os.path.join(code, "store"), exist_ok=True)
        mod = os.path.join(code, "store", "block_manifest.py")
        with open(mod, "w") as fh:
            fh.write("x = 1\n")
        digest = hashlib.sha256(b"x = 1\n").hexdigest()
        rec = {"run_id": RUN, "deploy_sha": "a" * 40, "head_sha": "a" * 40,
               "worktree_clean": True, "code_root": code, "python": "/opt/py",
               "modules": {"store.block_manifest": {"path": mod, "sha256": digest}}}
        rec.update(over)
        return rec

    def _refuse(self, **over):
        with self.assertRaises(rep.ArtifactError):
            rep.check_identity(self._identity(**over), run_id=RUN)

    def test_the_valid_control_passes(self):
        rep.check_identity(self._identity(), run_id=RUN)

    def test_a_module_imported_from_OUTSIDE_the_code_root_is_refused(self):
        outside = os.path.join(self.tmp, "elsewhere.py")
        with open(outside, "w") as fh:
            fh.write("x = 1\n")
        self._refuse(modules={"store.block_manifest": {
            "path": outside, "sha256": hashlib.sha256(b"x = 1\n").hexdigest()}})

    def test_a_module_whose_fingerprint_does_not_match_is_refused(self):
        rec = self._identity()
        rec["modules"]["store.block_manifest"]["sha256"] = "0" * 64
        with self.assertRaises(rep.ArtifactError):
            rep.check_identity(rec, run_id=RUN)

    def test_an_abbreviated_or_movable_sha_is_refused(self):
        for bad in ("HEAD", "a" * 7, "Z" * 40, "", None):
            with self.subTest(bad):
                self._refuse(deploy_sha=bad)

    def test_an_empty_code_root_or_python_is_refused(self):
        self._refuse(code_root="")
        self._refuse(python="")

    def test_no_modules_recorded_is_refused(self):
        self._refuse(modules={})


class TestTheReportRequiresIdentityToAgree(_E2E):

    def test_the_valid_control_passes(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        self.assertEqual(self.assemble("legacy_baseline").returncode, 0)

    def test_the_identity_block_writes_its_record(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        path = os.path.join(self.art, "identity.json")
        self.assertTrue(os.path.isfile(path), "identity-preflight recorded nothing")
        with open(path) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["deploy_sha"], self.sha)
        self.assertEqual(rec["code_root"], self.code)
        self.assertTrue(rec["modules"], "no module fingerprints recorded")

    def test_a_preflight_naming_a_DIFFERENT_valid_sha_is_refused(self):
        """A different but perfectly valid commit id: only cross-checking against what the
        identity block actually verified can tell the two apart."""
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        path = os.path.join(self.art, "preflight.json")
        with open(path) as fh:
            pre = json.load(fh)
        pre["deploy_sha"] = "b" * 40
        with open(path, "w") as fh:
            json.dump(pre, fh)
        a = self.assemble("legacy_baseline")
        self.assertNotEqual(a.returncode, 0, "a preflight SHA nobody verified was accepted")

    def test_a_preflight_naming_a_different_code_root_is_refused(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        path = os.path.join(self.art, "preflight.json")
        with open(path) as fh:
            pre = json.load(fh)
        pre["code_root"] = "/opt/somewhere-else"
        with open(path, "w") as fh:
            json.dump(pre, fh)
        self.assertNotEqual(self.assemble("legacy_baseline").returncode, 0)

    def test_a_missing_identity_record_refuses_assembly(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        os.remove(os.path.join(self.art, "identity.json"))
        self.assertNotEqual(self.assemble("legacy_baseline").returncode, 0)


# ------------------------------------------------------------------ 4. HEAD and worktree
class TestIdentityPinsTheCheckoutNotJustACommit(_E2E):
    """[High] `git rev-parse --verify <sha>^{commit}` echoes any existing commit back, so
    comparing it to DEPLOY_SHA proves the commit EXISTS — not that it is checked out. A valid
    ancestor passed while the working tree held something else, and a dirty tree passed too."""

    def _identity_block(self, **over):
        env = dict(self.env)
        env.update({"PROFILE": "legacy_baseline", "ART": self.art})
        env.update(over)
        os.makedirs(self.art, exist_ok=True)
        return subprocess.run(["bash", "-c", BLOCKS["identity-preflight"]], env=env,
                              capture_output=True, text=True, cwd=self.tmp)

    def test_the_clean_exact_sha_control_passes(self):
        r = self._identity_block()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_a_valid_ANCESTOR_as_deploy_sha_is_refused(self):
        """The reported hole: a real commit, correctly formed, that is not what is checked out."""
        self.assertNotEqual(self.old_sha, self.sha)
        r = self._identity_block(DEPLOY_SHA=self.old_sha)
        self.assertNotEqual(r.returncode, 0, "an ancestor passed as the deployed commit")
        self.assertIn("HEAD", r.stdout + r.stderr)

    def test_a_dirty_TRACKED_file_is_refused(self):
        target = os.path.join(self.code, "store", "block_manifest.py")
        with open(target, "a") as fh:
            fh.write("\n# local edit\n")
        r = self._identity_block()
        self.assertNotEqual(r.returncode, 0, "a modified tracked file passed as a clean tree")
        self.assertIn("NO-GO", r.stdout + r.stderr)

    def test_an_UNTRACKED_file_is_refused(self):
        with open(os.path.join(self.code, "store", "scratch.py"), "w") as fh:
            fh.write("x = 1\n")
        r = self._identity_block()
        self.assertNotEqual(r.returncode, 0, "an untracked file passed as a clean tree")

    def test_the_record_states_head_and_cleanliness(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        with open(os.path.join(self.art, "identity.json")) as fh:
            rec = json.load(fh)
        self.assertEqual(rec["head_sha"], self.sha)
        self.assertIs(rec["worktree_clean"], True)

    def test_the_assembler_refuses_a_record_whose_head_is_not_the_deploy_sha(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        path = os.path.join(self.art, "identity.json")
        with open(path) as fh:
            rec = json.load(fh)
        rec["head_sha"] = self.old_sha
        with open(path, "w") as fh:
            json.dump(rec, fh)
        self.assertNotEqual(self.assemble("legacy_baseline").returncode, 0)

    def test_the_assembler_refuses_a_record_admitting_a_dirty_tree(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        path = os.path.join(self.art, "identity.json")
        with open(path) as fh:
            rec = json.load(fh)
        rec["worktree_clean"] = False
        with open(path, "w") as fh:
            json.dump(rec, fh)
        self.assertNotEqual(self.assemble("legacy_baseline").returncode, 0)


# ------------------------------------------------------------------ 5. module inventory
class TestTheModuleInventoryCoversEveryModuleTheRunUses(_E2E):
    """[High] Only `store.block_manifest` was fingerprinted, while the run imports and executes
    `ops.p5s8_report`, `ops.p5_roadmap`, `ops.quiescence`, `store.repair_wal` and
    `store.segmented_cube`. An unrecorded module could change the validator itself, or the
    measured path, without changing a byte of identity evidence."""

    def test_the_inventory_is_declared_per_profile(self):
        for profile in ("legacy_baseline", "p5_shadow"):
            with self.subTest(profile):
                names = rep.identity_modules(profile)
                self.assertIn("ops.p5s8_report", names)
                self.assertIn("ops.p5_roadmap", names)
                self.assertIn("store.block_manifest", names)
        self.assertIn("ops.quiescence", rep.identity_modules("legacy_baseline"))
        for name in ("store.repair_wal", "store.segmented_cube"):
            self.assertIn(name, rep.identity_modules("p5_shadow"))

    def test_the_record_fingerprints_every_module_in_the_inventory(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        with open(os.path.join(self.art, "identity.json")) as fh:
            rec = json.load(fh)
        self.assertEqual(set(rec["modules"]),
                         set(rep.identity_modules("legacy_baseline")))
        for name, info in rec["modules"].items():
            with self.subTest(name):
                # realpath both sides: macOS resolves /var -> /private/var.
                self.assertTrue(os.path.realpath(info["path"]).startswith(
                    os.path.realpath(self.code)))
                self.assertEqual(len(info["sha256"]), 64)

    def test_a_record_missing_an_inventory_module_is_refused(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        path = os.path.join(self.art, "identity.json")
        with open(path) as fh:
            rec = json.load(fh)
        rec["modules"].pop("ops.p5s8_report")
        with open(path, "w") as fh:
            json.dump(rec, fh)
        a = self.assemble("legacy_baseline")
        self.assertNotEqual(a.returncode, 0, "an incomplete inventory was accepted")

    def test_changing_an_UNRECORDED_consumer_after_identity_is_refused(self):
        """The mutation the finding asks for: edit `ops/p5s8_report.py` — the validator itself —
        after identity is recorded. Before the inventory existed this changed nothing visible."""
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        target = os.path.join(self.code, "ops", "p5s8_report.py")
        with open(target, "a") as fh:
            fh.write("\n# changed after identity was recorded\n")
        a = self.assemble("legacy_baseline")
        self.assertNotEqual(a.returncode, 0,
                            "the validator was edited after identity and assembly accepted it")
        self.assertFalse(os.path.isfile(os.path.join(self.art, "p5s8_readonly.json")))

    def test_the_positive_control_still_assembles(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        self.assertEqual(self.assemble("legacy_baseline").returncode, 0)


# ------------------------------------------------------------------ 6. window authority
class TestTheWindowFollowsTheServiceNotTheClock(_E2E):
    """The aborted VM24 run in test form: the host was at 2026-08-20, the service's point
    history ended 2026-08-18, and a clock-derived window asked for two days that did not
    exist."""

    def _healthz_stub(self, point_latest, *, code="200", body=None):
        from test_phase2_p5s8_exec import _stub
        import datetime as dt
        end = dt.date.fromisoformat("2026-08-18")
        rows = json.dumps([{"date": (end - dt.timedelta(days=365 - i)).isoformat(),
                            "sst": 1.0} for i in range(366)])
        hz = body if body is not None else json.dumps(
            {"status": "ok", "point_latest": point_latest, "point_day_count": 1326})
        script = (
            'args="$*"\n'
            'if [[ "$args" == *"/healthz"* ]]; then\n'
            "  printf '%s' '" + hz + "'\n"
            "  printf '\\n" + code + "'; exit 0\n"
            'fi\n'
            "printf '%s' '" + rows + "'\n"
            'if [[ "$args" == *"time_total"* ]]; then printf \'\\n200 0.011\';\n'
            'elif [[ "$args" == *"http_code"* ]]; then printf \'\\n200\'; fi\n')
        _stub(self.bin, "curl", script)

    def _run_sampler(self):
        env = dict(self.env)
        env.update({"PROFILE": "legacy_baseline", "ART": self.art})
        return subprocess.run(["bash", "-c", BLOCKS["g4-sample"]], env=env,
                              capture_output=True, text=True, cwd=self.tmp)

    def test_the_request_ends_at_point_latest_not_at_todays_date(self):
        import datetime as dt
        from test_phase2_p5s8_e2e import POINT_LATEST
        self.assertNotEqual(POINT_LATEST, dt.date.today().isoformat(),
                            "the fixture must differ from today or it proves nothing")
        self.assert_all_ok(self.run_sequence("legacy_baseline", stop_after="g4-probe"))
        with open(os.path.join(self.art, "g4_request.json")) as fh:
            req = json.load(fh)
        self.assertEqual(req["end"], POINT_LATEST)
        self.assertEqual(req["point_latest"], POINT_LATEST)
        self.assertEqual(req["span_days"], 366)
        self.assertEqual(req["healthz_url"], rep.HEALTHZ_URL)
        self.assertEqual(req["start"], rep.g4_window(POINT_LATEST)[0])

    def test_the_2026_08_20_window_that_returned_364_rows_is_refused(self):
        """The exact aborted request, as a record."""
        from test_phase2_p5s8_e2e import POINT_LATEST
        bad = {"run_id": RUN, "start": "2025-08-20", "end": "2026-08-20",
               "url": rep.g4_url("2025-08-20", "2026-08-20"), "span_days": 366,
               "probe_rows": 366, "g4_samples": 2, "healthz_url": rep.HEALTHZ_URL,
               "point_latest": POINT_LATEST, "point_day_count": 1326}
        with self.assertRaises(rep.ArtifactError):
            rep.check_g4_request(bad, run_id=RUN)

    def test_point_latest_moving_between_probe_and_sampling_refuses(self):
        """A day landing mid-run. The sampler re-reads the authority live, so the series is
        discarded BEFORE the first timed sample rather than after thirty."""
        self.assert_all_ok(self.run_sequence("legacy_baseline", stop_after="g4-probe"))
        self._healthz_stub("2026-08-19")            # the service moved on
        r = self._run_sampler()
        self.assertNotEqual(r.returncode, 0, "the sampler timed a stale window")
        self.assertIn("point_latest moved", r.stdout + r.stderr)
        self.assertFalse(os.path.isfile(os.path.join(self.art, "g4_366d_seconds.txt")))

    def test_an_edited_request_cannot_bypass_the_live_recheck(self):
        """Editing the artifact to agree with itself does not help: the sampler compares
        against the SERVICE, not against anything the file claims."""
        self.assert_all_ok(self.run_sequence("legacy_baseline", stop_after="g4-probe"))
        path = os.path.join(self.art, "g4_request.json")
        start, end = rep.g4_window("2026-08-19")
        with open(path) as fh:
            req = json.load(fh)
        req.update({"start": start, "end": end, "point_latest": "2026-08-19",
                    "url": rep.g4_url(start, end)})
        with open(path, "w") as fh:
            json.dump(req, fh)                      # internally consistent, but not the service
        r = self._run_sampler()
        self.assertNotEqual(r.returncode, 0, "a self-consistent edited request was sampled")

    def test_a_non_200_healthz_refuses_before_any_request(self):
        self._healthz_stub("2026-08-18", code="503")
        res = self.run_sequence("legacy_baseline")
        self.assertIn("g4-probe", [tag for tag, r in res if r.returncode != 0])

    def test_a_malformed_healthz_body_refuses(self):
        self._healthz_stub("2026-08-18", body="<html>gateway</html>")
        res = self.run_sequence("legacy_baseline")
        self.assertIn("g4-probe", [tag for tag, r in res if r.returncode != 0])

    def test_a_healthz_without_point_latest_refuses(self):
        """`latest` and `daily_latest` present, `point_latest` absent: the generic fields must
        not stand in for the one that answers the question."""
        self._healthz_stub("2026-08-18",
                           body=json.dumps({"status": "ok", "latest": "2026-08-20",
                                            "daily_latest": "2026-08-20",
                                            "point_day_count": 1326}))
        res = self.run_sequence("legacy_baseline")
        self.assertIn("g4-probe", [tag for tag, r in res if r.returncode != 0])

    def test_the_valid_control_still_completes_and_assembles(self):
        self.assert_all_ok(self.run_sequence("legacy_baseline"))
        self.assertEqual(self.assemble("legacy_baseline").returncode, 0)


if __name__ == "__main__":
    unittest.main()
