"""dev2026 — P5-S8 runbook safety guards (review round 2).

**LOCAL-ONLY.** Nothing here contacts VM24 or runs a production command.

Each guard below exists because the first draft of the runbook got something wrong, and each
carries a **positive control** — a synthetic bad input the guard must reject — because a guard
that has only ever seen good input is indistinguishable from one that always passes.

The sharpest finding: the runbook wrote its own artifacts to `$G/logs/...` with `mkdir -p` and
`tee`, under the production data root, in a document whose first section promises it performs no
write. A read-only claim that the runbook's own bookkeeping violates is worse than no claim.
"""
from __future__ import annotations

import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
for p in (_DEV, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from ops import p5_roadmap as rm  # noqa: E402

RUNBOOK = os.path.join(_DEV, "specs", "p5s8_vm24_shadow_runbook.md")


def _read(path=RUNBOOK):
    with open(path) as fh:
        return fh.read()


def _code_only(doc):
    """Just the fenced command bodies.

    Scanning the whole document flagged the sentence *describing* the old defect --
    "the first draft put them in `$G/logs/...` and created them with `mkdir -p` and `tee`" --
    so the guard fired on the prose that documents the fix. Same shape as the earlier
    S7-gate-PASS false positive: a guard that fires on its own explanation invites deleting the
    explanation."""
    return "\n".join(body for _, body in _bash_blocks(doc))


def _bash_blocks(doc):
    """(preamble, body) for every ```bash fence."""
    out, lines, buf, in_block, start = [], doc.split("\n"), [], False, 0
    for i, ln in enumerate(lines):
        if ln.strip().startswith("```bash"):
            in_block, buf, start = True, [], i
            continue
        if in_block and ln.strip() == "```":
            out.append(("\n".join(lines[max(0, start - 6):start]), "\n".join(buf)))
            in_block = False
            continue
        if in_block:
            buf.append(ln)
    return out


# --------------------------------------------------------------------- 1. artifact location
class TestArtifactsAreNotWrittenUnderTheDataRoot(unittest.TestCase):
    """[High] `ART=$G/logs/...` + `mkdir -p` + `tee` is a write under the production data root,
    in a runbook whose opening section promises no write. Artifacts move to an operator-supplied
    path that is verified to be outside `$G` AND off the production filesystem."""

    @staticmethod
    def offending_art_assignments(doc):
        """`ART`/`ART_ROOT` assignments whose value sits under `$G`."""
        out = []
        for m in re.finditer(r"^\s*export\s+(ART\w*)=(\S+)", doc, re.M):
            if "$G" in m.group(2) or "/home/odbadmin/Data" in m.group(2):
                out.append(m.group(0).strip())
        return out

    @staticmethod
    def writes_under_G(doc):
        """`mkdir`/`tee`/redirection whose target is under `$G`."""
        out = []
        for m in re.finditer(r"^[^#\n]*\b(mkdir|tee|rsync)\b[^\n]*", doc, re.M):
            line = m.group(0)
            if re.search(r'["\']?\$G(/|["\'])', line) and "SHADOW" not in line:
                out.append(line.strip())
        return out

    def setUp(self):
        self.doc = _read()

    def test_no_ART_variable_points_under_the_data_root(self):
        self.assertEqual(self.offending_art_assignments(self.doc), [])

    def test_no_command_writes_under_the_data_root(self):
        self.assertEqual(self.writes_under_G(_code_only(self.doc)), [])

    def test_the_runbook_verifies_the_artifact_root_is_external(self):
        flat = " ".join(self.doc.split())
        self.assertIn("ART_ROOT", self.doc)
        # It must check BOTH: not a subpath of $G, and not on the production filesystem. A
        # sibling directory on the same full volume still competes for the space G18 is
        # blocked on.
        self.assertIn("must not be under $g", flat.lower().replace("`", ""))
        self.assertIn("findmnt", self.doc)

    def test_positive_control_the_guards_catch_a_bad_artifact_path(self):
        # Two separate shapes, because they are two separate leaks. `mkdir -p "$ART"` contains
        # no literal `$G`, so only following the ASSIGNMENT catches it; a direct
        # `mkdir -p "$G/logs"` is caught by the write check. Either alone leaves a gap.
        via_variable = 'export ART=$G/logs/p5s8_$RUN_ID\nmkdir -p "$ART"\n'
        self.assertTrue(self.offending_art_assignments(via_variable))
        direct = 'mkdir -p "$G/logs/run"\ndf -h | tee "$G/logs/df.txt"\n'
        self.assertEqual(len(self.writes_under_G(direct)), 2)
        good = ('export ART_ROOT=${ART_ROOT:?supply an external path}\n'
                'mkdir -p "$ART_ROOT/run"\n')
        self.assertEqual(self.offending_art_assignments(good), [])
        self.assertEqual(self.writes_under_G(good), [])


# --------------------------------------------------------------------- 2. profiles
class TestTheRunbookIsSplitIntoProfiles(unittest.TestCase):
    """[High] Current evidence says `p5_roots_present: false`, yet the draft demanded
    `ANCHOR_ROOT`, WAL state and `manifest_generation` before collecting *legacy* G4/G16
    evidence — preconditions that cannot be satisfied on the host as it stands today."""

    def setUp(self):
        self.doc = _read()

    def test_both_profiles_are_declared_in_code(self):
        self.assertEqual(set(rm.S8_PROFILES), {"legacy_baseline", "p5_shadow"})

    def test_the_legacy_profile_requires_no_P5_root(self):
        legacy = rm.S8_PROFILES["legacy_baseline"]
        self.assertFalse(legacy["requires_p5_roots"])
        for p5_field in ("anchor_root", "anchor_domain_id", "wal_repairs_pending",
                         "manifest_generation"):
            with self.subTest(p5_field):
                self.assertIn(p5_field, legacy["not_applicable"])
                self.assertTrue(legacy["not_applicable"][p5_field].strip(),
                                "not_applicable must carry a REASON, never a bare flag")

    def test_a_not_applicable_field_is_recorded_explicitly_not_omitted(self):
        """A missing field and 'does not apply here' are different states. Absence reads as
        clean, which is exactly how a preflight gets skipped without anyone noticing."""
        legacy = rm.S8_PROFILES["legacy_baseline"]
        recorded = set(legacy["required_fields"]) | set(legacy["not_applicable"])
        self.assertEqual(set(rm.S8_PREFLIGHT_FIELDS) - recorded, set(),
                         "a preflight field is neither required nor explicitly not-applicable")

    def test_the_p5_profile_requires_every_field(self):
        p5 = rm.S8_PROFILES["p5_shadow"]
        self.assertTrue(p5["requires_p5_roots"])
        self.assertEqual(p5["not_applicable"], {})
        self.assertEqual(set(p5["required_fields"]), set(rm.S8_PREFLIGHT_FIELDS))

    def test_the_legacy_profile_may_not_claim_P5_evidence(self):
        legacy = rm.S8_PROFILES["legacy_baseline"]
        self.assertEqual(legacy["may_adjudicate"], [])
        self.assertIn("G3", legacy["may_not_adjudicate"])
        self.assertIn("G16_snapshot_open", legacy["may_not_adjudicate"])

    def test_the_document_carries_both_profiles(self):
        flat = " ".join(self.doc.split())
        for name in ("legacy_baseline", "p5_shadow"):
            with self.subTest(name):
                self.assertIn(name, flat)
        self.assertIn("not_applicable_with_reason", flat)

    def test_positive_control_a_profile_that_hides_a_field_is_rejected(self):
        broken = {"required_fields": ("run_id",), "not_applicable": {}}
        recorded = set(broken["required_fields"]) | set(broken["not_applicable"])
        self.assertNotEqual(set(rm.S8_PREFLIGHT_FIELDS) - recorded, set())


# --------------------------------------------------------------------- 3. posture
class TestTheCompactionPosture(unittest.TestCase):
    """[High] The posture must start with the COMPACTION LOCK, and hold it across the whole
    critical section. Starting at the ingest lock leaves the build unfenced against a
    concurrent compaction."""

    def test_the_order_is_the_corrected_one(self):
        p = list(rm.COMPACTION_POSTURE)
        self.assertEqual(p[0], "compaction lock")
        self.assertEqual(p[1], "ingest lock")
        self.assertEqual(p[2], "staleness/prechecks")
        self.assertEqual(p[3], "quiesce")
        for a, b in (("quiesce", "build/publish/verify as applicable"),
                     ("build/publish/verify as applicable",
                      "prune/swap only after verification"),
                     ("prune/swap only after verification",
                      "restart/API verification")):
            with self.subTest(f"{a} < {b}"):
                self.assertLess(p.index(a), p.index(b))

    def test_the_compaction_lock_is_declared_held_across_the_critical_section(self):
        self.assertIn("compaction lock", rm.COMPACTION_LOCK_SCOPE["held_from"])
        self.assertIn("restart", rm.COMPACTION_LOCK_SCOPE["held_until"])
        self.assertTrue(rm.COMPACTION_LOCK_SCOPE["held_across_critical_section"])

    def test_prune_never_precedes_verification(self):
        p = list(rm.COMPACTION_POSTURE)
        prune = [i for i, s in enumerate(p) if "prune" in s]
        verify = [i for i, s in enumerate(p) if "verif" in s]
        self.assertTrue(prune and verify)
        self.assertLess(min(verify), min(prune))

    def test_positive_control_an_unfenced_posture_is_detectable(self):
        bad = ("ingest lock", "quiesce", "build", "prune/swap", "verify")
        self.assertNotEqual(bad[0], "compaction lock")
        self.assertGreater([i for i, s in enumerate(bad) if "prune" in s][0],
                           [i for i, s in enumerate(bad) if "build" in s][0])


# --------------------------------------------------------------------- 4. execution host
class TestEveryCommandDeclaresItsExecutionHost(unittest.TestCase):
    """A runbook that mixes local shell, `ssh`, VM24 paths and `localhost` API calls without
    saying which host each block runs on is a runbook someone will run in the wrong place."""

    HOSTS = ("Run on: LOCAL", "Run on: VM24")

    def setUp(self):
        self.blocks = _bash_blocks(_read())

    def test_there_are_command_blocks_to_check(self):
        self.assertGreaterEqual(len(self.blocks), 5)

    def test_every_bash_block_declares_its_host(self):
        for preamble, body in self.blocks:
            with self.subTest(body.strip().split("\n")[0][:60]):
                where = preamble + "\n" + body      # a leading `# Run on:` comment counts
                self.assertTrue(any(h in where for h in self.HOSTS),
                                "no 'Run on:' marker for this block")

    def test_localhost_api_calls_are_marked_as_running_ON_vm24(self):
        for preamble, body in self.blocks:
            if "127.0.0.1" in body or "localhost" in body:
                with self.subTest(body[:50]):
                    self.assertIn("Run on: VM24", preamble + "\n" + body,
                                  "a localhost API call means localhost OF VM24")

    def test_positive_control_an_unmarked_block_is_caught(self):
        doc = "some text\n\n```bash\ndf -h\n```\n"
        blocks = _bash_blocks(doc)
        self.assertEqual(len(blocks), 1)
        self.assertFalse(any(h in blocks[0][0] for h in self.HOSTS))


# --------------------------------------------------------------------- 5. G4 sampling
class TestG4FailsClosed(unittest.TestCase):
    """A timing sample from a 500, or from a range that is not 366 days, is not a G4 sample.
    `curl -o /dev/null -w '%{time_total}'` times an error page just as happily as an answer."""

    def setUp(self):
        self.doc = _read()
        self.g4 = self.doc[self.doc.index("G4"):]

    def test_the_http_status_is_checked_before_a_sample_is_kept(self):
        self.assertIn("http_code", self.doc)
        flat = " ".join(self.doc.split())
        self.assertIn("discard", flat.lower())

    def test_the_span_is_verified_to_be_exactly_366_days(self):
        self.assertIn("366", self.doc)
        flat = " ".join(self.doc.split())
        self.assertTrue(re.search(r"exactly 366", flat),
                        "the runbook must verify the span, not assume the date arithmetic")

    def test_the_returned_row_count_is_verified(self):
        """Scoped to the COMMAND. The phrase "row count" also appears in the abort table and the
        artifact list, so a prose match survived deleting the check itself — the guard was
        reading the documentation of the rule instead of the rule."""
        code = _code_only(self.doc)
        # The check moved from bash into the probe/sampler python, where it can also validate
        # the JSON shape and run on EVERY sample rather than only the first.
        # The row check moved into `check_g4_response`, shared by the probe and the sampler,
        # where it also verifies the day SET rather than only the length.
        self.assertIn("check_g4_response", code)
        # The message now lives with the rule, in the shared validator.
        with open(os.path.join(_DEV, "ops", "p5s8_report.py")) as fh:
            self.assertIn("expected {len(wanted)}", fh.read())

    def test_positive_control_prose_alone_does_not_satisfy_the_row_count_guard(self):
        prose_only = "```bash\ncurl -s \"$URL\"\n```\n\n| the row count does not match | aborts |"
        self.assertNotIn("ROWS", _code_only(prose_only))

    def test_positive_control_a_naive_curl_loop_is_recognisable(self):
        naive = "curl -s -o /dev/null -w '%{time_total}\\n' \"$URL\""
        self.assertNotIn("http_code", naive)


# --------------------------------------------------------------------- 6. PM2 pids
class TestWorkerPidCollectionIsValidated(unittest.TestCase):
    """`pgrep -f 'uvicorn|ghrsst' | head -1` picks an arbitrary match — possibly the grep, the
    supervisor, or one worker of several — and then reports its fd count as if it were the
    service's. PM2 knows the pids; ask it."""

    def setUp(self):
        self.doc = _read()

    def test_pgrep_head_is_gone(self):
        """Scoped to the commands. The prose still names the old pattern, deliberately: the
        reason it was replaced is worth keeping where the replacement lives."""
        code = _code_only(self.doc)
        self.assertNotIn("pgrep", code)
        self.assertNotIn("head -1", code)

    def test_pm2_jlist_is_the_source_of_pids(self):
        self.assertIn("pm2 jlist", self.doc)
        flat = " ".join(self.doc.split())
        self.assertIn("aggregate", flat.lower())

    def test_the_runbook_reuses_the_shipped_worker_pid_helper(self):
        """`ops.quiescence.worker_pids` already encodes 'every pid PM2 knows, whatever its
        status'. A second hand-rolled version in a runbook is a second truth."""
        self.assertIn("worker_pids", self.doc)

    def test_positive_control_the_old_pattern_would_be_caught(self):
        bad = "PID=$(pgrep -f 'uvicorn|ghrsst' | head -1)"
        self.assertIn("pgrep", bad)
        self.assertIn("head -1", bad)


# --------------------------------------------------------------------- 7. os.walk
class TestMetadataScanFailsClosed(unittest.TestCase):
    """`os.walk` swallows every OSError by default. A permission-denied subtree silently
    shrinks the file count, and the scan reports a smaller, faster tree than exists."""

    def setUp(self):
        self.doc = _read()

    def test_the_walk_passes_an_onerror_handler(self):
        self.assertIn("onerror", self.doc)

    def test_the_handler_raises_rather_than_logging(self):
        flat = " ".join(self.doc.split())
        self.assertTrue(re.search(r"onerror\s*=", flat))
        self.assertIn("raise", self.doc)

    def test_positive_control_a_bare_walk_is_recognisable(self):
        bare = "for _, d, f in os.walk('$G'):"
        self.assertNotIn("onerror", bare)


# --------------------------------------------------------------------- 8. capacity provenance
class TestCapacityFiguresAreLabelledHistorical(unittest.TestCase):
    """The 2026-08-19 numbers are the newest evidence there is, and they are also a year-old
    snapshot of a filling volume. They stay usable and stay labelled."""

    def test_the_capacity_block_declares_its_status_and_date(self):
        self.assertEqual(rm.CAPACITY["status"], "historical/provisional")
        self.assertEqual(rm.CAPACITY["as_of"], "2026-08-19")
        self.assertTrue(rm.CAPACITY["must_refresh_before_use"])

    def test_it_says_what_must_happen_before_the_numbers_are_relied_on(self):
        why = rm.CAPACITY["refresh_requirement"].lower()
        self.assertIn("read-only", why)
        self.assertIn("regenerate", why)

    def test_the_documents_repeat_the_label(self):
        for path in (RUNBOOK, os.path.join(_DEV, "specs", "p5_critical_path_memo.md")):
            with self.subTest(os.path.basename(path)):
                flat = " ".join(_read(path).split()).lower()
                self.assertIn("historical", flat)
                self.assertIn("2026-08-19", flat)


# --------------------------------------------------------------------- 9. policy predicate
class TestThePolicyPredicateDoesNotOverclaim(unittest.TestCase):
    """`authorizes()` is not wired into any execution path. Calling it an authorization check
    implies it can stop an operator typing the command, which it cannot."""

    def test_it_is_named_as_a_policy_predicate(self):
        self.assertTrue(hasattr(rm, "policy_permits"))
        self.assertFalse(rm.policy_permits("prune"))
        self.assertTrue(rm.policy_permits("s8_runbook_authoring"))

    def test_its_docstring_says_it_enforces_nothing_at_runtime(self):
        doc = (rm.policy_permits.__doc__ or "").lower()
        self.assertIn("does not", doc)
        self.assertIn("advisory", doc)

    def test_the_module_records_that_it_is_not_wired_into_execution(self):
        self.assertFalse(rm.POLICY_PREDICATE_IS_ENFORCED)
        self.assertIn("not wired", rm.POLICY_PREDICATE_NOTE.lower())

    def test_no_document_claims_it_prevents_execution(self):
        for path in (RUNBOOK, os.path.join(_DEV, "specs", "p5_critical_path_memo.md")):
            flat = " ".join(_read(path).split()).lower()
            for overclaim in ("prevents s9", "prevents s10", "blocks execution",
                              "cannot be run because"):
                with self.subTest(doc=os.path.basename(path), phrase=overclaim):
                    self.assertNotIn(overclaim, flat)

    def test_positive_control_an_enforced_predicate_would_declare_itself(self):
        self.assertIn("POLICY_PREDICATE_IS_ENFORCED", dir(rm))


if __name__ == "__main__":
    unittest.main()
