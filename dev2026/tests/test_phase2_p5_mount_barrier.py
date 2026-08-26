"""dev2026 — the operational barrier that a `legacy_base` generation 1 rests on.

`content_seal` DETECTS that the monolith changed. It does so after the fact, and it is blind to
a write that restores size and mtime — both stated where it is defined. `check_monolith_sealed()`
reads permission bits at build time and is explicitly necessary-not-sufficient: `chmod` is
reversible by anything with write access to the parent, and a root process ignores it.

What actually stops the monolith changing is a **read-only mount**, and no code in this project
can arrange one. So the readiness contract adjudicates *collected evidence* of it, and this
suite holds that adjudication to failing closed on every way the evidence can be wrong.

## The part that is not a formality

The monolith today is `$G/mur_timecube_s8_t90_sh128.zarr`, and `$G` is on `/dev/sdc1` mounted at
`/home/odbadmin/Data`. The delta lives there too, and the P5 roots would. **That mount cannot be
made read-only**: it would freeze the delta and the WAL along with the monolith, leaving a
deployment that can neither ingest nor repair nor compact.

So the barrier requires a mount-layout change — the monolith on its own read-only mount, beside
writable P5 paths — which is an IT decision, not a code change. The contract checks for exactly
that shape and refuses a record where the two share a mount, because a mount is read-only or it
is not.

Every fixture here is stubbed. Nothing contacts VM24.
"""
from __future__ import annotations

import datetime
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
for p in (_DEV, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

from ops import p5_readiness as rd  # noqa: E402

RUN_ID = "20260826T120000Z"
HOST = "odb24"
MONO = "/mnt/ghrsst_base/mur_timecube_s8_t90_sh128.zarr"
G = "/home/odbadmin/Data/ghrsst"


def now_iso(offset_s=0):
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=offset_s)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def barrier(**over):
    """A record describing the layout the barrier requires: the monolith on its own read-only
    mount, with the P5 roots, WAL and delta writable on another."""
    ev = {
        "monolith_path": MONO, "monolith_realpath": MONO,
        "source": "/dev/sdd1", "target": "/mnt/ghrsst_base",
        "fstype": "ext4", "options": "ro,relatime", "device": 2082,
        "run_id": RUN_ID, "host": HOST, "collected_utc": now_iso(),
        "writable_paths": [
            {"role": "p5_blocks", "path": f"{G}/p5_blocks", "source": "/dev/sdc1",
             "target": "/home/odbadmin/Data", "options": "rw,relatime"},
            {"role": "p5_repairs", "path": f"{G}/p5_repairs", "source": "/dev/sdc1",
             "target": "/home/odbadmin/Data", "options": "rw,relatime"},
            {"role": "delta", "path": f"{G}/mur_timecube_s8_t90_sh128.delta.zarr",
             "source": "/dev/sdc1", "target": "/home/odbadmin/Data", "options": "rw,relatime"},
        ],
    }
    ev.update(over)
    return ev


class TestTheBarrierRecordIsAdjudicatedNotAssumed(unittest.TestCase):

    def problem(self, **over):
        return rd.mount_barrier_problem(barrier(**over), run_id=RUN_ID, host=HOST)

    # ---------------------------------------------------------------- accepted
    def test_a_read_only_mount_beside_writable_p5_paths_is_accepted(self):
        """The positive control: every refusal below must be about its own defect."""
        self.assertIsNone(self.problem())

    def test_other_read_only_option_spellings_are_accepted(self):
        for options in ("ro", "ro,relatime,nosuid", "nosuid,ro"):
            with self.subTest(options):
                self.assertIsNone(self.problem(options=options))

    # ---------------------------------------------------------------- the mount itself
    def test_a_writable_mount_is_refused(self):
        problem = self.problem(options="rw,relatime")
        self.assertTrue(problem)
        self.assertIn("not read-only", problem)

    def test_relatime_alone_is_not_read_ONLY(self):
        """By token, never by substring: `relatime` contains the letters `ro`, and a substring
        test would call a writable mount frozen."""
        problem = self.problem(options="rw,relatime,errors=remount-ro")
        self.assertTrue(problem, "a substring match accepted a read-write mount")

    def test_permission_bits_are_not_this_check(self):
        """The record may say the directory is unwritable; that is not the barrier. `chmod` is
        reversible by anything with write access to the parent, and root ignores it."""
        problem = self.problem(options="rw,relatime", mode="0o555")
        self.assertTrue(problem)
        self.assertIn("Permission bits are not this check", problem)

    def test_a_monolith_not_on_the_recorded_mount_is_refused(self):
        problem = self.problem(target="/mnt/somewhere_else")
        self.assertTrue(problem)
        self.assertIn("does not show the monolith to be", problem)

    # ---------------------------------------------------------------- coexistence
    def test_writable_paths_sharing_the_monoliths_mount_are_refused(self):
        """The architectural point. A mount is read-only or it is not."""
        shared = [dict(e, source="/dev/sdd1", target="/mnt/ghrsst_base")
                  for e in barrier()["writable_paths"]]
        problem = self.problem(writable_paths=shared)
        self.assertTrue(problem)
        self.assertIn("SAME mount as the monolith", problem)

    def test_a_writable_role_that_is_actually_read_only_is_refused(self):
        frozen = [dict(e) for e in barrier()["writable_paths"]]
        frozen[1]["options"] = "ro,relatime"
        problem = self.problem(writable_paths=frozen)
        self.assertTrue(problem)
        self.assertIn("cannot ingest, repair or compact", problem)

    def test_a_missing_writable_role_is_refused(self):
        for role in rd.MOUNT_BARRIER_WRITABLE_ROLES:
            with self.subTest(role):
                kept = [e for e in barrier()["writable_paths"] if e["role"] != role]
                problem = self.problem(writable_paths=kept)
                self.assertTrue(problem)
                self.assertIn(role, problem)

    def test_absent_or_empty_writable_paths_are_refused(self):
        for value in (None, [], {}, "none"):
            with self.subTest(repr(value)):
                self.assertTrue(self.problem(writable_paths=value))

    # ---------------------------------------------------------------- the path itself
    def test_a_symlinked_monolith_is_refused(self):
        problem = self.problem(monolith_path="/mnt/ghrsst_base/current.zarr")
        self.assertTrue(problem)
        self.assertIn("resolves to", problem)

    def test_a_changed_realpath_is_refused(self):
        problem = self.problem(monolith_realpath="/mnt/ghrsst_base/other.zarr")
        self.assertTrue(problem)

    # ---------------------------------------------------------------- provenance
    def test_a_record_from_another_run_is_refused(self):
        problem = self.problem(run_id="some-other-run")
        self.assertTrue(problem)
        self.assertIn("another run", problem)

    def test_a_record_from_another_host_is_refused(self):
        problem = self.problem(host="somewhere-else")
        self.assertTrue(problem)
        self.assertIn("another host", problem)

    def test_a_stale_record_is_refused(self):
        problem = self.problem(collected_utc=now_iso(-(rd.MAX_CAPACITY_AGE_S + 60)))
        self.assertTrue(problem)
        self.assertIn("collection window", problem)

    def test_a_record_from_the_future_is_refused(self):
        problem = self.problem(collected_utc=now_iso(rd.MAX_CLOCK_LEAD_S + 120))
        self.assertTrue(problem)
        self.assertIn("in the future", problem)

    # ---------------------------------------------------------------- shape
    def test_every_required_field_is_required(self):
        for field in rd.MOUNT_BARRIER_FIELDS:
            with self.subTest(field):
                ev = barrier()
                del ev[field]
                problem = rd.mount_barrier_problem(ev, run_id=RUN_ID, host=HOST)
                self.assertTrue(problem)
                self.assertIn(field, problem)

    def test_a_device_that_is_not_a_number_is_refused(self):
        for value in ("2082", True, None, 1.5):
            with self.subTest(repr(value)):
                self.assertTrue(self.problem(device=value))

    def test_a_record_that_is_not_a_record_is_refused(self):
        for value in (None, "ro", [], 0):
            with self.subTest(repr(value)):
                self.assertTrue(rd.mount_barrier_problem(value, run_id=RUN_ID, host=HOST))


class TestTheRequirementIsWiredIn(unittest.TestCase):
    """It must reach a verdict through `assess()`, not only through the helper."""

    def target(self, **over):
        t = {"label": rd.QUALIFYING_LABEL, "host": HOST, "run_id": RUN_ID,
             "monolith_barrier": barrier()}
        t.update(over)
        return t

    def verdict(self, **over):
        by_id = {r["id"]: r for r in rd.assess(self.target(**over))["requirements"]}
        return by_id["monolith_barrier"]

    def test_it_is_a_declared_requirement(self):
        self.assertIn("monolith_barrier", [r["id"] for r in rd.REQUIREMENTS])
        self.assertIn("monolith_barrier", rd.CHECKERS)

    def test_an_absent_record_is_not_adjudicated_rather_than_blocked(self):
        """Nothing collected is missing evidence, not a barrier shown to be absent. It still
        blocks readiness."""
        t = self.target()
        del t["monolith_barrier"]
        by_id = {r["id"]: r for r in rd.assess(t)["requirements"]}
        self.assertEqual(by_id["monolith_barrier"]["verdict"], rd.NOT_ADJUDICATED)
        self.assertFalse(rd.assess(t)["compaction_ready"])

    def test_a_writable_mount_blocks(self):
        v = self.verdict(monolith_barrier=barrier(options="rw,relatime"))
        self.assertEqual(v["verdict"], rd.BLOCKED)
        self.assertIn("not read-only", v["reason"])

    def test_a_valid_record_is_not_blocked(self):
        """It cannot read READY here -- the assessor is unapproved in this fixture and every
        READY is demoted -- but it must not be BLOCKED, or the positive control proves nothing."""
        self.assertNotEqual(self.verdict()["verdict"], rd.BLOCKED)

    def test_it_never_reaches_ready_without_the_barrier(self):
        self.assertFalse(rd.assess(self.target(
            monolith_barrier=barrier(options="rw"))) ["compaction_ready"])


class TestTheDocumentRecordsTheCriticalPath(unittest.TestCase):
    """The barrier is step one of a sequence, and the sequence has to be written down."""

    def _flat(self):
        with open(os.path.join(_DEV, "specs", "p5_deployment_readiness.md")) as fh:
            return " ".join(fh.read().split())

    def test_the_critical_path_is_recorded_in_order(self):
        flat = self._flat()
        steps = ["IT mount-layout decision", "fresh VM24 read-only collection",
                 "candidate deployment review", "provisioning approval",
                 "provision generation 1 + WAL", "API switchover approval",
                 "compaction approval"]
        positions = []
        for step in steps:
            with self.subTest(step):
                self.assertIn(step, flat)
                positions.append(flat.index(step))
        self.assertEqual(positions, sorted(positions),
                         "the critical path is recorded out of order")

    def test_the_document_says_why_G_cannot_simply_be_mounted_read_only(self):
        flat = self._flat()
        self.assertIn("The delta lives on that mount and the P5 roots would", flat)
        self.assertIn("separate** read-only mount", flat)
        self.assertIn("not a change this project can make", flat)

    def test_the_document_records_what_the_requirement_refuses(self):
        flat = self._flat()
        for phrase in ("a mount is read-only or it is not",
                       "`chmod` is not the barrier",
                       "by option token", "would call a writable mount frozen"):
            with self.subTest(phrase):
                self.assertIn(phrase, flat)

    def test_the_document_does_not_claim_the_code_establishes_the_mount(self):
        flat = self._flat()
        self.assertIn("adjudicates a collected record", flat)
        import inspect
        doc = inspect.getdoc(rd.mount_barrier_problem) or ""
        self.assertIn("Nothing here can look at VM24's mount table", doc)


class TestTheOperatorChecklistMatchesTheContract(unittest.TestCase):
    """A checklist that collects the wrong fields produces a record the validator refuses.

    The document is what an operator will actually run, and the validator is what will judge
    the result. Nothing but a test keeps the two in step — and "the checklist drifted from the
    thing it feeds" is precisely the class of defect this phase keeps finding."""

    def _doc(self):
        path = os.path.join(_DEV, "specs", "p5_monolith_mount_barrier_requirement.md")
        with open(path) as fh:
            return fh.read()

    def test_the_checklist_names_every_field_the_contract_requires(self):
        flat = " ".join(self._doc().split())
        for field in rd.MOUNT_BARRIER_FIELDS:
            with self.subTest(field):
                self.assertIn(f"`{field}`", flat,
                              f"the checklist does not say where {field} comes from")

    def test_the_checklist_names_every_writable_role(self):
        flat = " ".join(self._doc().split())
        for role in rd.MOUNT_BARRIER_WRITABLE_ROLES:
            with self.subTest(role):
                self.assertIn(role, flat)

    def test_the_checklist_states_the_freshness_window_the_contract_enforces(self):
        flat = " ".join(self._doc().split())
        hours = rd.MAX_CAPACITY_AGE_S // 3600
        self.assertIn(f"**{hours}-hour** freshness window", flat)

    def test_the_checklist_matches_read_only_by_token_not_substring(self):
        """The same rule the validator uses, and the same reason given."""
        flat = " ".join(self._doc().split())
        self.assertIn("grep -qx 'ro'", flat)
        self.assertIn("would report a read-write mount as frozen", flat)

    def test_the_checklist_refuses_to_claim_a_bind_mount_works(self):
        flat = " ".join(self._doc().split())
        self.assertIn("must not be assumed to work", flat)
        self.assertIn("does not make the underlying files read-only through their original "
                      "path", flat)
        self.assertIn("Nothing in this document claims a bind mount is sufficient", flat)

    def test_the_checklist_requires_the_write_denial_test_as_the_service_user(self):
        flat = " ".join(self._doc().split())
        self.assertIn("It must **fail**", flat)
        self.assertIn("A denial for `root` proves nothing about the service", flat)
        self.assertIn("Do not adjust permissions to make the test pass", flat)

    def test_the_checklist_records_the_current_fail_closed_status(self):
        flat = " ".join(self._doc().split())
        self.assertIn("`monolith_barrier` is `NOT_ADJUDICATED`", flat)
        self.assertIn("**`compaction_ready` is `false`**", flat)
        self.assertIn("remain provisional", flat)

    def test_the_checklist_says_nothing_writes_under_G(self):
        flat = " ".join(self._doc().split())
        self.assertIn("Nothing writes under `$G`", flat)
