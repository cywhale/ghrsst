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
        end = dt.date.today()
        good = json.dumps([{"date": (end - dt.timedelta(days=365 - i)).isoformat(),
                            "sst": 1.0} for i in range(366)])
        from test_phase2_p5s8_exec import _stub
        _stub(self.bin, "curl", f'''
n=$(cat {state}); echo $((n+1)) > {state}
if [ "$n" -lt 1 ]; then BODY='{good}'; else BODY='{payload_after_probe}'; fi
args="$*"
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
        rec = {"run_id": RUN, "deploy_sha": "a" * 40, "code_root": code,
               "python": "/opt/py",
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


if __name__ == "__main__":
    unittest.main()
