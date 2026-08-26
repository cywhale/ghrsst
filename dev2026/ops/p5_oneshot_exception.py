"""dev2026 — the one-shot compaction risk exception, and the checks that surround it.

A human has decided to accept the risk of running **one** operation over a still-writable
monolith, on the grounds that VM24 has full automated VM backups. This module is that decision
expressed as something checkable.

## What this is not

It is **not** a `force` flag, a `skip_seal` switch, an `allow_unsealed` parameter or an
environment variable. Every one of those is a bypass whose blast radius is "whatever the caller
felt like", reusable by the next caller who finds it, and impossible to audit afterwards. This
is a record bound to one host, one run, one operation, one monolith at one measured state, one
attested backup and one expiry — and if any of those does not match what is in front of it, it
refuses.

`ops/p5_legacy_base.py` used to carry a `require_sealed: bool = True` parameter that could be
passed `False`. It had no caller anywhere that ever did, so it was a bypass sitting unused with
the door open; it is **gone**. What replaces it is `UnsealedMonolithAuthorization` — a
capability object that cannot be constructed by a caller, cannot be pickled or copied, is minted
only by `authorize_unsealed()` after the whole chain of checks passes, is bound to one store and
one run, and is **consumed by its single use**.

## The measurement has to be taken by the code that acts on it

An `observed` dict handed in alongside the record proves only that whoever assembled the two was
consistent. So `authorize_unsealed()` does not accept a measurement: it calls
`measure_monolith()` itself, at the protected execution point, and compares what it found
against both the record and the same-run preflight evidence. `evaluate()` called with
caller-supplied evidence — as the readiness assessor calls it — reports
`eligible_pending_recomputation` and is never `eligible`, because a report is not an
authorization.

## It authorizes ONE operation, and compaction is not yet one of them

The record names an `operation`. Only `provision_generation_one` is implemented, and the
compaction family — build, prune, swap, delete, switch — is **explicitly refused**: no one-shot
entry point exists for it, so nothing would enforce the post-compaction gates. Provisioning
generation 1 publishes a manifest over the monolith; it deletes nothing, prunes nothing, swaps
nothing and switches nothing. Describing it as authorization for the later lifecycle would be
claiming a gate that no operation runs.

## It also does not weaken `monolith_barrier`

That requirement goes on reporting `BLOCKED` or `NOT_ADJUDICATED` exactly as before, and
`compaction_ready` goes on being `false` without a read-only mount. The exception is reported
separately, as its own eligibility, because the two say different things: "this deployment is
safe to compact" and "a person accepted the risk of one operation on this deployment, and here
is what they bound that acceptance to".

## Why the backup is not an argument that the risk is small

The operator's reasoning is that a VM-level backup makes the failure recoverable, not that it
makes it unlikely. Those are different claims and this module treats them differently: the
backup is required, attested and bound into the record, and nothing here treats its existence
as a reason to relax any other check. Nor is `backup_verified: true` treated as proof —
`backup_verification()` reports `NOT_ADJUDICATED`, because no check available to this process
can reach the hypervisor's backup catalogue. §"Rollback limits" in the runbook records what
restoring a VM backup actually costs and what it cannot undo.

## The stronger route is still the preferred one

A read-only mount makes the monolith unable to change, which no amount of after-the-fact
detection matches. This exception exists because one operation is wanted before that layout
change can be arranged, and it expires.
"""
from __future__ import annotations

import datetime
import os
from typing import Dict, Optional, Sequence, Tuple

#: The scope an exception record must declare. A record approving anything else -- an assessor
#: build, a provisioning run, a shadow -- is not this.
ONESHOT_SCOPE = "p5_oneshot_compaction_risk_acceptance"

#: Publishing a `legacy_base` manifest over the monolith. Deletes nothing.
OP_PROVISION = "provision_generation_one"

#: Building a block from the monolith and publishing it as the next generation. Deletes
#: nothing EITHER -- see `ops/p5_oneshot_compaction.py`. The name is exact on purpose: a record
#: saying "compaction" names an intention, and this names an operation.
OP_COMPACTION = "one_shot_compaction"

#: Operations a record may name. A record naming anything else authorizes nothing, because
#: nothing would be listening for it.
IMPLEMENTED_OPERATIONS = (OP_PROVISION, OP_COMPACTION)

#: The DESTRUCTIVE family: everything that can remove data or reroute what is served. None of
#: these is implemented, none is reachable from either implemented operation, and each is
#: refused BY NAME so the message can say which wall was hit. `one_shot_compaction` builds and
#: publishes; releasing the old space is a separate operation with its own approval, and the
#: clearance this one can issue is the only thing that would ever unlock it.
DESTRUCTIVE_FAMILY = ("prune", "prune_delta", "prune_staging", "swap", "swap_delta",
                      "delete", "delete_daily", "switch", "api_switchover", "finalize",
                      "finalization", "release_space")

#: Vague aliases for the compaction step. Refused, with a pointer to the exact name: an
#: approval should say which code path it authorizes, and "compaction" names several.
COMPACTION_ALIASES = ("compaction", "compact", "build_blocks", "fold")

#: Every field the record must carry. There is no partial form: a record missing any of these
#: does not bind the thing it is supposed to bind.
EXCEPTION_FIELDS = (
    "scope", "operation", "host", "run_id",
    "monolith_realpath", "monolith_metadata_fingerprint", "monolith_day_digest",
    "monolith_content_ledger",
    "backup_id", "backup_completed_utc", "backup_verified", "backup_retention_ref",
    "expires_utc", "approved_by", "approved_utc", "reason",
    "residual_writer_risk",
)

#: What the residual-risk acknowledgement must carry. The fence stops the writers anyone knows
#: about; an arbitrary one -- a shell, a stray script, a service restarted by hand -- obeys
#: neither the lock nor the service posture, and three probes are samples rather than
#: continuous proof. That residue cannot be engineered away here, so the only honest options
#: are to remain ineligible or to have a person accept it EXPLICITLY, in writing, having been
#: told what it is. This is that decision, and it is part of the approved record rather than a
#: parameter, because it is a judgement and not a setting.
RESIDUAL_RISK_FIELDS = ("acknowledged", "unrecoverable_outcome_possible", "statement",
                        "accepted_by")

#: Fields the fresh pre-compaction evidence must carry, each answering a question that cannot
#: be answered from a document.
PRE_COMPACTION_FIELDS = (
    "host",                  # the machine it was collected on
    "run_id",                # the run it was collected for
    "collected_utc",         # when -- so staleness is a refusal, not a judgement call
    "capacity",              # fresh statvfs bound to the device holding the data root
    "healthz",               # the service's own current view
    "identity",              # a clean checkout of a reviewed commit
    "quiescence",            # process / PM2 / cron state
    "monolith_writers",      # who has the monolith open for writing
    "writer_fence",          # what STOPS them -- a count is an observation, not a fence
    "locks",                 # compaction and ingest lock state
    "monolith_state",        # fresh fingerprints and content ledger
    "backup",                # the attested backup this run is covered by
    "plan",                  # exact source map and destination manifest plan
)

#: Gates that must all pass AFTER a compaction and BEFORE anything is deleted or switched.
#: Callable here; enforced by nothing, because no operation that can prune, swap, delete or
#: switch exists yet. `post_compaction_enforcement()` says so rather than leaving a reader to
#: assume the presence of a checker means something runs it.
POST_COMPACTION_FIELDS = (
    "manifest",              # checksum and generation of what was published
    "wal_anchor",            # the WAL still bound to that authority, anchor intact
    "source_map_complete",   # every day accounted for by a named source
    "base_delta_disjoint",   # no day claimed by both
    "point_series_parity",   # values match the monolith oracle
    "api_parity",            # the service returns what it returned before
    "capacity_after",        # fresh measurement, not the pre-run figure
    "nothing_deleted",       # explicit: no daily or base data removed yet
)

#: The four properties that identify a monolith's state. Named once, because the record, the
#: preflight evidence and the at-execution measurement must all be about the same four things.
MONOLITH_STATE_FIELDS = ("monolith_realpath", "monolith_metadata_fingerprint",
                         "monolith_day_digest", "monolith_content_ledger")

#: The only PM2 postures under which an exceptional write to the served store is safe. The
#: API may keep serving (it reads); anything that WRITES the monolith must be stopped, and
#: "online" for a writer-producing process is not a posture, it is the hazard.
SAFE_PM2_POSTURES = ("stopped", "disabled", "not-installed")

#: The append cron must be off for the whole window. "no append window right now" is a
#: statement about the clock, not about the crontab, and the clock moves during a long scan.
SAFE_CRON_POSTURES = ("disabled", "commented-out", "not-installed")

#: Fields the writer fence must carry. See `writer_fence_problem` for why each is here.
WRITER_FENCE_FIELDS = ("method", "services_stopped", "cron_disabled", "flock_is_advisory",
                       "arbitrary_writer_policy", "observed_utc", "verified")

NOT_ADJUDICATED = "NOT_ADJUDICATED"


class OneShotExceptionRefused(Exception):
    """The exception record is absent, malformed, or is not for what is in front of it."""


class UnsealedMonolithAuthorizationError(OneShotExceptionRefused):
    """A capability was constructed, reused, or presented for something it is not for."""


def _exact_str(value) -> bool:
    return type(value) is str and bool(value.strip())


def _is_sha256(value) -> bool:
    if type(value) is not str or len(value) != 64:
        return False
    return all(c in "0123456789abcdef" for c in value)


def _is_sha1(value) -> bool:
    if type(value) is not str or len(value) != 40:
        return False
    return all(c in "0123456789abcdef" for c in value)


def _real_number(value) -> bool:
    """A number, and NOT a bool. `True` is an `int`, and `True >= 403.3` is a real comparison
    that quietly answers `False`."""
    if type(value) is bool:
        return False
    return isinstance(value, (int, float)) and value == value      # NaN fails every comparison


def _raw_int(value) -> bool:
    return type(value) is not bool and isinstance(value, int)


def _parse_utc(value):
    if not _exact_str(value):
        return None
    try:
        stamped = datetime.datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return stamped.replace(tzinfo=datetime.timezone.utc)


def _is_day(value) -> bool:
    if not _exact_str(value):
        return False
    try:
        datetime.datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _now(now=None):
    return now or datetime.datetime.now(datetime.timezone.utc)


def _policy():
    """The canonical freshness window and clock-lead tolerance, from the module that owns them.

    Imported here rather than copied: a second constant with the same name is a second policy,
    and the one that drifts is always the copy. Function-local so this module stays importable
    by the provisioner without dragging the whole readiness import chain behind it."""
    from ops.p5_readiness import MAX_CAPACITY_AGE_S, MAX_CLOCK_LEAD_S
    return MAX_CAPACITY_AGE_S, MAX_CLOCK_LEAD_S


# ---------------------------------------------------------------- measuring the store
def measure_monolith(monolith_path: str, *, now=None) -> Dict:
    """Measure the store AS IT IS NOW: realpath, metadata fingerprint, day digest, ledger.

    This is the only source of `observed` that means anything at an execution point. It is
    expensive -- `content_ledger()` walks every file -- and that cost is the reason the sealed
    path does not pay it twice and the unsealed path does."""
    from store import block_manifest
    insp = block_manifest.inspect_store_contract(monolith_path)
    return {
        "monolith_realpath": os.path.realpath(monolith_path),
        "monolith_metadata_fingerprint":
            block_manifest.metadata_fingerprint_from_inspection(insp),
        "monolith_day_digest": block_manifest.day_digest(insp.days),
        "monolith_content_ledger": block_manifest.content_ledger(monolith_path),
        "measured_utc": _now(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "measured_by": "ops/p5_oneshot_exception.measure_monolith",
    }


def _state_mismatch(left: Dict, right: Dict, *, left_name: str, right_name: str):
    """The first way two monolith-state records disagree, or `None`."""
    for field in MONOLITH_STATE_FIELDS:
        if field not in left:
            return f"{left_name} records no {field}"
        if field not in right:
            return f"{right_name} records no {field}"
    for field in ("monolith_realpath", "monolith_metadata_fingerprint", "monolith_day_digest"):
        if left[field] != right[field]:
            return (f"{left_name} and {right_name} disagree about {field}: "
                    f"{left[field]!r} against {right[field]!r}")
    left_led, right_led = left["monolith_content_ledger"], right["monolith_content_ledger"]
    if type(left_led) is not dict or type(right_led) is not dict:
        return f"{left_name} or {right_name} records a content ledger that is not a ledger"
    if left_led.get("ledger_digest") != right_led.get("ledger_digest"):
        return (f"{left_name} and {right_name} disagree about the content ledger; the store "
                f"was written to between them, which is the exact hazard the read-only mount "
                f"exists to prevent")
    return None


# ---------------------------------------------------------------- the record
def exception_problem(record, *, host: str, run_id: str, operation: str,
                      observed: Optional[Dict] = None,
                      subject_realpath: Optional[str] = None, now=None):
    """The first thing wrong with a one-shot exception record, or `None`.

    `observed` is what is TRUE NOW -- the monolith's realpath, metadata fingerprint, day digest
    and content ledger. The record says what a person looked at when they accepted the risk;
    `observed` says what they would be accepting it for today. A record whose subject has
    changed since it was signed is not an acceptance of this risk, it is an acceptance of a
    different one, and there is no way to tell from the record alone which.

    `subject_realpath` is the store the CALLER is about to act on, and it is not optional in
    spirit: record and `observed` agreeing with each other proves only that whoever assembled
    them was consistent, not that either describes the monolith this run will act on. The
    operation knows that path and supplies it, so the binding is established by the code doing
    the work rather than asserted by the argument it was handed."""
    if type(record) is not dict:
        return (f"is {type(record).__name__}, not a record. This is not a flag: there is no "
                f"form of it that is merely 'on'")
    for field in EXCEPTION_FIELDS:
        if field not in record:
            return f"records no {field}; a partial record binds nothing"
    if record["scope"] != ONESHOT_SCOPE:
        return (f"declares scope {record['scope']!r}; this needs {ONESHOT_SCOPE!r}. An "
                f"approval for one act is not an approval for another")
    for field in ("operation", "host", "run_id", "monolith_realpath", "backup_id",
                  "backup_retention_ref", "approved_by", "reason"):
        if not _exact_str(record[field]):
            return f"records {field} {record[field]!r}, which is not a non-empty string"
    problem = residual_risk_problem(record["residual_writer_risk"])
    if problem:
        return f"the residual writer risk acknowledgement {problem}"
    if len(record["reason"].split()) < 5:
        return (f"records reason {record['reason']!r}, which is not a reason. The whole point "
                f"of an exception is that someone said why in words a reviewer can weigh")

    # ---- bound to ONE operation, and compaction is not one of them -------------------------
    problem = operation_problem(record["operation"])
    if problem:
        return problem
    if record["operation"] != operation:
        return (f"authorizes {record['operation']!r}, but this is {operation!r}. An acceptance "
                f"names the act it accepts the risk of")

    # ---- bound to THIS host and THIS run ---------------------------------------------------
    if record["host"] != host:
        return (f"was approved for host {record['host']!r}, not {host!r}. A risk accepted for "
                f"one machine is not accepted for another")
    if record["run_id"] != run_id:
        return (f"was approved for run {record['run_id']!r}, not {run_id!r}. ONE run is what "
                f"one-shot means; a record that outlives its run is a standing permission")

    # ---- bound to the monolith AS IT IS NOW ------------------------------------------------
    for field in ("monolith_metadata_fingerprint", "monolith_day_digest"):
        if not _is_sha256(record[field]):
            return f"records {field} {record[field]!r}, which is not a SHA-256 digest"
    ledger = record["monolith_content_ledger"]
    if type(ledger) is not dict:
        return f"records monolith_content_ledger {ledger!r}, which is not a ledger"
    for field in ("algo", "ledger_digest", "file_count", "total_bytes"):
        if field not in ledger:
            return f"the recorded content ledger has no {field}"
    if not _is_sha256(ledger["ledger_digest"]):
        return "the recorded content ledger digest is not a SHA-256 digest"

    if subject_realpath is not None and record["monolith_realpath"] != subject_realpath:
        return (f"was approved for the store at {record['monolith_realpath']!r}, but this run "
                f"acts on {subject_realpath!r}. A risk accepted for one store is not accepted "
                f"for another, however alike they look")

    if type(observed) is not dict or not observed:
        return ("cannot be checked against the monolith: no fresh measurement was supplied. A "
                "record verified against nothing is a record verified against itself")
    mismatch = _state_mismatch(record, observed, left_name="the record",
                               right_name="the fresh measurement")
    if mismatch:
        return (f"{mismatch}. The store changed after the risk was accepted, so what was "
                f"accepted is not what would be run")

    # ---- bound to an attested backup -------------------------------------------------------
    # `backup_verified` is a human attestation, NOT proof; `backup_verification()` reports that
    # as NOT_ADJUDICATED. What is checked here is that the attestation was actually made and
    # that it was made about a backup that already existed when the risk was accepted.
    if record["backup_verified"] is not True:
        return (f"records backup_verified={record['backup_verified']!r}. A backup nobody "
                f"confirmed is a plan, and the whole exception rests on the restore working")
    completed = _parse_utc(record["backup_completed_utc"])
    if completed is None:
        return (f"records backup_completed_utc {record['backup_completed_utc']!r}, which is "
                f"not a timestamp")
    approved = _parse_utc(record["approved_utc"])
    if approved is None:
        return f"records approved_utc {record['approved_utc']!r}, which is not a timestamp"
    if completed > approved:
        return ("records a backup completing after the approval was signed; the person "
                "accepting the risk must have been looking at a backup that already existed")

    # ---- and it expires ---------------------------------------------------------------------
    expires = _parse_utc(record["expires_utc"])
    if expires is None:
        return f"records expires_utc {record['expires_utc']!r}, which is not a timestamp"
    if expires <= approved:
        return "expires at or before it was approved, so it authorizes nothing"
    when = _now(now)
    if when >= expires:
        return (f"expired at {record['expires_utc']}. An exception that outlives its window is "
                f"a standing permission, which is what it was written not to be")
    return None


def residual_risk_problem(block):
    """The first thing wrong with the residual-writer-risk acknowledgement, or `None`.

    Not a checkbox. `unrecoverable_outcome_possible` must be `True` -- the person accepting has
    to have been told, and to have recorded, that a writer nobody fenced can corrupt the served
    authority in a way no gate here prevents and no rollback fully undoes. Recording `False`
    would be recording a belief the code does not share, and the refusal says so."""
    if type(block) is not dict:
        return (f"is {type(block).__name__}, not an acknowledgement. There is no form of this "
                f"that is merely truthy")
    for field in RESIDUAL_RISK_FIELDS:
        if field not in block:
            return f"records no {field}"
    if block["acknowledged"] is not True:
        return (f"records acknowledged={block['acknowledged']!r}. The fence stops the writers "
                f"that obey it; an arbitrary one does not, and the operation stays ineligible "
                f"unless a person accepts that explicitly")
    if block["unrecoverable_outcome_possible"] is not True:
        return (f"records unrecoverable_outcome_possible="
                f"{block['unrecoverable_outcome_possible']!r}. "
                f"A writer nobody fenced can change the store between the samples, and neither "
                f"the parity gates nor a VM restore fully undo what was served in between. "
                f"Recording otherwise is recording a belief this code does not share")
    if not _exact_str(block["accepted_by"]):
        return "does not say who accepted it"
    if not _exact_str(block["statement"]) or len(block["statement"].split()) < 8:
        return ("carries no statement in words a reviewer can weigh. What is being accepted is "
                "specific -- an unfenced writer, during a minutes-long interval, against the "
                "store that is about to become the served authority -- and a person accepting "
                "it should be able to say so")
    return None


def operation_problem(operation):
    """Is `operation` one this module has an entry point for? Returns the refusal, or `None`.

    The compaction family gets its own refusal because "not implemented" and "implemented but
    not for you" are different facts, and an operator reading a generic message would not learn
    which one they hit."""
    if not _exact_str(operation):
        return f"names operation {operation!r}, which is not an operation"
    if operation in IMPLEMENTED_OPERATIONS:
        return None
    if operation in DESTRUCTIVE_FAMILY:
        return (f"names operation {operation!r}. NOTHING implements it, and no one-shot "
                f"exception reaches it: removing data or rerouting what is served is a "
                f"separate operation with its own approval, gated on a finalization clearance "
                f"that {OP_COMPACTION!r} issues only when every post-compaction gate has "
                f"passed. Neither implemented operation prunes, swaps, deletes or switches. "
                f"Implemented: {list(IMPLEMENTED_OPERATIONS)}")
    if operation in COMPACTION_ALIASES:
        return (f"names operation {operation!r}, which is an intention rather than a code "
                f"path -- several things are called compaction. The exact name is "
                f"{OP_COMPACTION!r}, which builds a block and publishes it and deletes "
                f"nothing. Implemented: {list(IMPLEMENTED_OPERATIONS)}")
    return (f"names operation {operation!r}, which nothing implements. Implemented: "
            f"{list(IMPLEMENTED_OPERATIONS)}")


# ---------------------------------------------------------------- the preflight
def _missing(evidence, fields, what):
    if type(evidence) is not dict:
        return f"{what} is {type(evidence).__name__}, not a record"
    absent = [f for f in fields if f not in evidence]
    if absent:
        return f"{what} records nothing for {absent}"
    empty = [f for f in fields if evidence[f] in (None, "", {}, [])]
    if empty:
        return f"{what} records an empty {empty}; an unanswered check is not a passing one"
    return None


def _validated(block, name, *, required):
    """A block that carries `verified: True` AND its own fields, or the refusal.

    Presence was the old bar and it was too low: `{"pm2": "probably fine"}` satisfied it. A
    result that nothing verified is a note, and a note is not evidence."""
    if type(block) is not dict:
        return f"{name} is {type(block).__name__}, not a validated result"
    if block.get("verified") is not True:
        return (f"{name} records verified={block.get('verified')!r}; an observation nobody "
                f"validated is a note, not a result")
    for field in required:
        if field not in block:
            return f"{name} records no {field}"
    return None


def writer_fence_problem(fence):
    """The first thing wrong with the writer fence, or `None`.

    **A writer count is not a fence.** `monolith_writers.count == 0` says what was true at one
    instant; the scan that follows runs for minutes, and the append cron does not consult it.
    What makes the interval safe is the writer-producing services being STOPPED and the append
    cron being DISABLED for its whole duration.

    Nor is the ingest lock a fence against everything. `flock` is advisory: it excludes the
    ingest path because the ingest path takes it, and it excludes nothing that does not. A
    process writing the monolith directly -- an operator with a shell, a stray script, a
    restarted service -- is not stopped by it. That residue is real, and the fence must SAY so
    rather than leave a reader to infer the lock covers more than it does."""
    problem = _missing(fence, WRITER_FENCE_FIELDS, "the writer fence")
    if problem:
        return problem
    if fence["verified"] is not True:
        return (f"records verified={fence['verified']!r}; a fence nobody confirmed is a "
                f"plan for a fence")
    if not _exact_str(fence["method"]):
        return "does not say HOW the writers were fenced; an unnamed method is not a fence"
    stopped = fence["services_stopped"]
    if type(stopped) is not list or not stopped:
        return ("names no stopped services. If nothing had to be stopped, say which processes "
                "were checked and why none writes -- an empty fence is a claim, not a check")
    for entry in stopped:
        if type(entry) is not dict:
            return f"records a stopped service {entry!r} that is not a record"
        for field in ("name", "posture"):
            if field not in entry:
                return f"a stopped service records no {field}"
        if entry["posture"] not in SAFE_PM2_POSTURES:
            return (f"records service {entry['name']!r} as {entry['posture']!r}; a writer that "
                    f"is still running is the hazard. Safe: {list(SAFE_PM2_POSTURES)}")
    if fence["cron_disabled"] not in SAFE_CRON_POSTURES:
        return (f"records cron_disabled={fence['cron_disabled']!r}. "
                f"\"No append window right now\" is a statement about the clock, and the clock "
                f"moves during a long scan. Safe: {list(SAFE_CRON_POSTURES)}")
    # Stated, not assumed. A fence that does not acknowledge what it fails to cover invites the
    # belief that it covers everything.
    if fence["flock_is_advisory"] is not True:
        return ("does not acknowledge that flock is advisory. The ingest lock excludes the "
                "ingest path because the ingest path takes it; it excludes nothing else, and a "
                "fence that does not say so reads as stronger than it is")
    if not _exact_str(fence["arbitrary_writer_policy"]):
        return ("states no policy for arbitrary writers -- a process writing the monolith "
                "directly obeys neither the lock nor the service posture. What covers that is "
                "an operator instruction and the content-ledger recheck, and it has to be "
                "written down to be followed")
    if _parse_utc(fence["observed_utc"]) is None:
        return f"records observed_utc {fence['observed_utc']!r}, which is not a timestamp"
    return None


def pre_compaction_problem(evidence, *, record=None, host=None, run_id=None,
                           expected_dev=None, expected_fingerprints=None, now=None):
    """The first thing wrong with the fresh pre-compaction evidence, or `None`.

    Every field answers a question that cannot be answered from a document: what the disk holds
    now, what the service currently serves, what is running, what holds the locks, and what the
    plan actually is. A compaction started on stale answers is one whose preconditions were true
    at some point.

    Field PRESENCE is not the bar. Each block must be a validated result bound to this host and
    this run, the capacity reading must be fresh against the canonical window and bound to the
    device it was taken on, and the record's expiry must not outlive the capacity evidence it
    rests on -- an exception valid for six hours over a reading valid for two authorizes the
    last four on nothing."""
    problem = _missing(evidence, PRE_COMPACTION_FIELDS, "the pre-compaction evidence")
    if problem:
        return problem
    max_age_s, max_lead_s = _policy()
    when = _now(now)

    # ---- bound to this host, this run, and a moment ----------------------------------------
    if host is not None and evidence["host"] != host:
        return (f"was collected on {evidence['host']!r}, not {host!r}; preflight from another "
                f"machine says nothing about this one")
    if run_id is not None and evidence["run_id"] != run_id:
        return (f"was collected in run {evidence['run_id']!r}, not {run_id!r}; preflight from "
                f"another run is a fact about another moment")
    collected = _parse_utc(evidence["collected_utc"])
    if collected is None:
        return f"records collected_utc {evidence['collected_utc']!r}, which is not a timestamp"
    age = (when - collected).total_seconds()
    if age < -max_lead_s:
        return f"was collected {int(-age)}s in the future; that is not a reading"
    if age > max_age_s:
        return (f"was collected {int(age)}s ago, past the {max_age_s}s window. Everything in "
                f"it -- free space, what is running, what holds the locks -- moves without "
                f"announcing it here")

    # ---- capacity: fresh, real, and about THIS device ---------------------------------------
    capacity = evidence["capacity"]
    if type(capacity) is not dict:
        return "the capacity evidence is not a reading"
    for field in ("free_now_gib", "filesystem_dev", "measured_utc"):
        if field not in capacity:
            return f"the capacity evidence records no {field}"
    if not _real_number(capacity["free_now_gib"]):
        return (f"records free_now_gib {capacity['free_now_gib']!r}, which is not a number. "
                f"`True` is an `int` and would compare against a floor without complaint")
    if not _raw_int(capacity["filesystem_dev"]):
        return (f"records filesystem_dev {capacity['filesystem_dev']!r}, which is not a "
                f"device number")
    measured = _parse_utc(capacity["measured_utc"])
    if measured is None:
        return f"records capacity measured_utc {capacity['measured_utc']!r}, not a timestamp"
    cap_age = (when - measured).total_seconds()
    if cap_age < -max_lead_s:
        return f"records a capacity reading {int(-cap_age)}s in the future"
    if cap_age > max_age_s:
        return (f"rests on a capacity reading {int(cap_age)}s old, past the {max_age_s}s "
                f"window; a schedule tick, a restart or an overnight pause all move free space")
    if expected_dev is not None and capacity["filesystem_dev"] != expected_dev:
        return (f"records a capacity reading taken on device {capacity['filesystem_dev']}, but "
                f"this run acts on device {expected_dev}; the reading is about another volume")

    # ---- the expiry may not outlive the reading it rests on ---------------------------------
    if record is not None and type(record) is dict and "expires_utc" in record:
        expires = _parse_utc(record["expires_utc"])
        if expires is None:
            return f"records expires_utc {record['expires_utc']!r}, which is not a timestamp"
        capacity_good_until = measured + datetime.timedelta(seconds=max_age_s)
        if expires > capacity_good_until:
            return (f"expires at {record['expires_utc']}, after the capacity reading it rests "
                    f"on stops being current "
                    f"({capacity_good_until.strftime('%Y-%m-%dT%H:%M:%SZ')}). The last stretch "
                    f"of such a window is authorized by a measurement nobody re-took")

    # ---- the service's own view --------------------------------------------------------------
    healthz = evidence["healthz"]
    if type(healthz) is not dict:
        return "the healthz evidence is not a result"
    if healthz.get("status") != "ok":
        return (f"records healthz status {healthz.get('status')!r}; a service that is not "
                f"serving normally is not one to start an exceptional operation on")
    if not _is_day(healthz.get("latest")):
        return (f"records healthz latest {healthz.get('latest')!r}, which is not a day. What "
                f"the service currently serves is the baseline any parity check compares to")
    if not _exact_str(healthz.get("route")):
        return ("records no healthz route. Which store the service actually loaded is the "
                "thing API parity is measured against, and `status: ok` is reported by a "
                "service serving from any route at all")

    # ---- validated results, not notes --------------------------------------------------------
    problem = _validated(evidence["identity"], "the identity evidence",
                         required=("reviewed_sha", "worktree_clean", "approved",
                                   "module_fingerprints"))
    if problem:
        return problem
    identity = evidence["identity"]
    if not _is_sha1(identity["reviewed_sha"]):
        return (f"records reviewed_sha {identity['reviewed_sha']!r}, which is not a commit id")
    if identity["worktree_clean"] is not True:
        return ("records a worktree that is not clean; what would run is then not the commit "
                "that was reviewed")
    if identity["approved"] is not True:
        return ("records a deployment that is not an approved one. A clean checkout of an "
                "UNREVIEWED commit is still unreviewed code, and `worktree_clean` says only "
                "that it matches its own HEAD")
    prints = identity["module_fingerprints"]
    if type(prints) is not dict or not prints:
        return ("records no module fingerprints. A commit id says what was checked out; "
                "fingerprints say what is on disk now, and they are what caught a live "
                "checkout running HEAD plus five edited files")
    for name, digest in sorted(prints.items()):
        if not _is_sha256(digest):
            return f"records module fingerprint {name}={digest!r}, which is not a SHA-256"
    if expected_fingerprints is not None:
        drifted = sorted(n for n, d in prints.items()
                         if n in expected_fingerprints and expected_fingerprints[n] != d)
        if drifted:
            return (f"records module fingerprints that do not match the reviewed commit: "
                    f"{drifted}. What would run is not what was approved")
        absent = sorted(set(expected_fingerprints) - set(prints))
        if absent:
            return (f"records no fingerprint for {absent}; an unmeasured module is one nobody "
                    f"compared")

    problem = _validated(evidence["locks"], "the lock evidence",
                         required=("compaction", "ingest", "canonical_paths",
                                   "acquisition_probe"))
    if problem:
        return problem
    locks = evidence["locks"]
    for name in ("compaction", "ingest"):
        if locks[name] != "free":
            return (f"records the {name} lock as {locks[name]!r}; starting under "
                    f"another holder is starting against whatever it is doing")
    paths = locks["canonical_paths"]
    if type(paths) is not dict or set(paths) != {"compaction", "ingest"}:
        return ("does not record both canonical lock paths. A lock taken on some other path "
                "is a lock nothing else contends for, and it would read as held")
    for name, path in sorted(paths.items()):
        if not _exact_str(path) or not os.path.isabs(path):
            return (f"records the {name} lock path as {path!r}, which is not an absolute "
                    f"path; what it names would depend on the working directory")
    probe = locks["acquisition_probe"]
    if type(probe) is not dict:
        return "records no lock acquisition probe"
    for name in ("compaction", "ingest"):
        if probe.get(name) != "acquired_and_released":
            return (f"does not demonstrate that the {name} lock can be ACQUIRED. \"Free\" is "
                    f"read off a file; acquiring it and letting go is the only thing that "
                    f"shows the path is lockable by this process at all")

    problem = _validated(evidence["quiescence"], "the quiescence evidence",
                         required=("pm2", "cron", "readers", "reader_policy"))
    if problem:
        return problem
    quiescence = evidence["quiescence"]
    readers = quiescence["readers"]
    if not _raw_int(readers) or readers < 0:
        return f"records readers={readers!r}, which is not a count"
    if quiescence["pm2"] not in SAFE_PM2_POSTURES:
        return (f"records the PM2 posture as {quiescence['pm2']!r}. A writer-producing process "
                f"that is still up is the hazard this window exists to remove. Safe: "
                f"{list(SAFE_PM2_POSTURES)}")
    if quiescence["cron"] not in SAFE_CRON_POSTURES:
        return (f"records the cron posture as {quiescence['cron']!r}, which is a statement "
                f"about the clock rather than the crontab. Safe: {list(SAFE_CRON_POSTURES)}")
    if not _exact_str(quiescence["reader_policy"]):
        return ("states no reader policy. Readers are ALLOWED -- the old generation stays "
                "served until a separate switch -- but that has to be the stated position "
                "rather than an unexamined one")

    # ---- who holds the monolith --------------------------------------------------------------
    writers = evidence["monolith_writers"]
    if type(writers) is not dict or "count" not in writers:
        return "the pre-compaction evidence does not say how many processes hold the monolith"
    if not _raw_int(writers["count"]):
        return f"monolith_writers.count is {writers['count']!r}, which is not a count"
    if not _exact_str(writers.get("method")):
        return ("does not say HOW the monolith writers were counted; a zero from an unnamed "
                "method is a zero nobody can reproduce")
    if writers["count"] != 0:
        return (f"reports {writers['count']} process(es) with the monolith open for writing. "
                f"Compacting a base something else is writing is the hazard, not the risk")
    # And the count is only an observation. What makes the interval safe is the fence.
    problem = writer_fence_problem(evidence["writer_fence"])
    if problem:
        return f"the writer fence {problem}"

    # ---- the monolith state the preflight saw ------------------------------------------------
    if record is not None and type(record) is dict:
        mismatch = _state_mismatch(record, evidence["monolith_state"],
                                   left_name="the record",
                                   right_name="the preflight monolith_state")
        if mismatch:
            return mismatch

    # ---- the backup, bound to the record ------------------------------------------------------
    backup = evidence["backup"]
    problem = _validated(backup, "the backup evidence",
                         required=("backup_id", "method", "verified_utc"))
    if problem:
        return problem
    if record is not None and type(record) is dict and "backup_id" in record:
        if backup["backup_id"] != record["backup_id"]:
            return (f"attests backup {backup['backup_id']!r} while the record binds to "
                    f"{record['backup_id']!r}; the run would be covered by a different backup "
                    f"from the one the risk was accepted against")
    verified_utc = _parse_utc(backup["verified_utc"])
    if verified_utc is None:
        return f"records backup verified_utc {backup['verified_utc']!r}, not a timestamp"
    if not _exact_str(backup["method"]):
        return "does not say how the backup was verified; an unnamed method is not provenance"
    if record is not None and type(record) is dict and "backup_completed_utc" in record:
        completed = _parse_utc(record["backup_completed_utc"])
        if completed is not None and verified_utc < completed:
            return ("attests the backup was verified before it finished; the verification is "
                    "then about something that did not yet exist")

    # ---- the plan --------------------------------------------------------------------------
    plan = evidence["plan"]
    for field in ("source_map", "destination_manifest", "days"):
        if type(plan) is not dict or field not in plan:
            return f"the plan records no {field}; an unstated plan cannot be reviewed"
    return None


def backup_verification(record, pre=None) -> Dict:
    """What can and cannot be established locally about the backup. Always NOT_ADJUDICATED.

    `backup_verified: true` is a human attestation. Nothing available to this process can reach
    the hypervisor's backup catalogue, mount the image, or confirm a restore would succeed, so
    treating the flag as proof would be the same mistake as treating `immutable: true` in a
    segment as proof that a store is immutable. It is reported as NOT_ADJUDICATED so a reader
    sees what the eligibility actually rests on."""
    attested = type(record) is dict and record.get("backup_verified") is True
    return {
        "verdict": NOT_ADJUDICATED,
        "attested": attested,
        "backup_id": record.get("backup_id") if type(record) is dict else None,
        "attested_by": record.get("approved_by") if type(record) is dict else None,
        "method": (pre or {}).get("backup", {}).get("method") if type(pre) is dict else None,
        "why": ("no check available to this process can reach the hypervisor's backup "
                "catalogue, mount the image, or confirm that a restore would succeed. The "
                "attestation is a person's word, recorded and bound to the record; it is not "
                "proof, and nothing here treats it as any reason to relax another check"),
        "consequence": ("if the attestation is wrong, the recoverability the whole exception "
                        "rests on is not there. See the rollback limits for what a restore "
                        "does NOT undo even when the backup is exactly as described"),
    }


# ---------------------------------------------------------------- the post gates
def nothing_deleted_problem(block):
    """The first thing wrong with the `nothing_deleted` evidence, or `None`.

    **Re-derived here, never believed.** `nothing_deleted: true` written by a caller is a claim
    about the caller's intentions. What this takes instead is the raw before/after inventory the
    operation measured, recomputes the verdict from it, and refuses if the recomputed answer and
    the recorded one disagree -- so a hand-written `true` over an inventory showing a shrunken
    store fails, which is the only case where the distinction matters."""
    if type(block) is not dict:
        return (f"is {type(block).__name__}, not a measurement. A bare true/false here is the "
                f"operation's own claim about itself")
    for field in ("value", "derived_from", "before", "after"):
        if field not in block:
            return f"records no {field}; a verdict without its evidence cannot be re-derived"
    if block["derived_from"] != "inventory":
        return (f"says it was derived from {block['derived_from']!r}. Only an inventory taken "
                f"before and after can establish this; anything else is an assertion")
    before, after = block["before"], block["after"]
    if type(before) is not dict or type(after) is not dict or not before:
        return "records an inventory that is not a pair of measurements"
    missing = sorted(set(before) - set(after))
    if missing:
        return (f"the after-inventory does not cover {missing}; a path that stopped being "
                f"measured is indistinguishable from one that stopped existing")
    shrunk = []
    for path, was in sorted(before.items()):
        now_state = after[path]
        for side in (was, now_state):
            if type(side) is not dict or "bytes" not in side or "files" not in side:
                return f"the inventory entry for {path!r} records no bytes/files"
        if now_state["bytes"] < was["bytes"] or now_state["files"] < was["files"]:
            shrunk.append(path)
    recomputed = not shrunk
    if block["value"] is not recomputed:
        return (f"records value={block['value']!r} over an inventory that shows "
                f"{'no ' if recomputed else ''}removal"
                + (f" ({shrunk} shrank)" if shrunk else "")
                + ". The verdict is recomputed from the inventory, not read off the claim")
    if not recomputed:
        return (f"data was removed from {shrunk} before the gates passed. Nothing may be "
                f"deleted until a separately approved finalization step")
    return None


def post_compaction_problem(evidence, *, expected=None):
    """The first thing wrong with the post-compaction gates, or `None`.

    All of them must pass BEFORE anything is deleted or any service is switched. The ordering is
    the point: a parity failure discovered after the old data is gone is a parity failure with
    nothing to go back to.

    `expected` binds the whole set to ONE act: the host, the run, the generation that was
    published, the monolith state that was measured at the execution point, the manifest
    checksum and the source-map digest. Without it a passing result is a passing result about
    *something* -- gates collected from a different run, or a different generation, read exactly
    the same. `ops/p5_oneshot_compaction.py` supplies it from its own state.

    Enforcement: `ops/p5_oneshot_compaction.compact_one_shot()` calls this and refuses to issue
    a finalization clearance unless it returns `None`. See `post_compaction_enforcement()`."""
    problem = _missing(evidence, POST_COMPACTION_FIELDS, "the post-compaction evidence")
    if problem:
        return problem
    problem = nothing_deleted_problem(evidence["nothing_deleted"])
    if problem:
        return f"the nothing_deleted evidence {problem}"
    manifest = evidence["manifest"]
    if type(manifest) is not dict:
        return "the manifest evidence is not a record"
    if manifest.get("checksum_ok") is not True:
        return (f"records checksum_ok={manifest.get('checksum_ok')!r}: the published manifest "
                f"does not hash to the checksum it carries, so what is on disk is not what "
                f"was composed")
    if not _raw_int(manifest.get("generation")) or manifest["generation"] <= 0:
        return f"records generation={manifest.get('generation')!r}, which is not a generation"

    wal = evidence["wal_anchor"]
    if type(wal) is not dict:
        return "the wal_anchor evidence is not a record"
    if wal.get("bound") is not True:
        return (f"the WAL is not bound to the generation that was just published"
                + (f": {wal.get('reason')}" if wal.get("reason") else "")
                + ". A log whose authority is a store the service no longer serves cannot "
                  "record a repair to the one it does")
    if wal.get("anchor_intact") is not True:
        return ("the WAL anchor is missing; without the durable high-water mark a restored or "
                "truncated log cannot be told from a fresh one")

    capacity = evidence["capacity_after"]
    if type(capacity) is not dict or "free_now_gib" not in capacity:
        return "the capacity_after evidence records no free space"
    if not _real_number(capacity["free_now_gib"]):
        return (f"records capacity_after free_now_gib {capacity['free_now_gib']!r}, which is "
                f"not a number")
    if _parse_utc(capacity.get("measured_utc")) is None:
        return (f"records capacity_after measured_utc {capacity.get('measured_utc')!r}, which "
                f"is not a timestamp. A figure carried over from the preflight is not an "
                f"'after' measurement")

    parity = evidence["point_series_parity"]
    if type(parity) is not dict:
        return "point_series_parity is not a record"
    for field in ("days_checked", "mismatches", "boundaries_included"):
        if field not in parity:
            return f"point_series_parity records no {field}"
    if parity["mismatches"] != 0:
        return (f"reports {parity['mismatches']} point-series mismatch(es) against the "
                f"monolith oracle")
    if parity["boundaries_included"] is not True:
        return ("reports parity that did not include the first and last days; the boundaries "
                "are where an off-by-one shows")
    if not _raw_int(parity["days_checked"]) or parity["days_checked"] <= 0:
        return f"reports days_checked={parity['days_checked']!r}, which checks nothing"
    api = evidence["api_parity"]
    if type(api) is not dict or "mismatches" not in api:
        return "api_parity records no mismatches"
    if api["mismatches"] != 0:
        return f"reports {api['mismatches']} API response mismatch(es) against the baseline"
    if evidence["base_delta_disjoint"] is not True:
        return ("reports base and delta are not disjoint; a day claimed by both is a day whose "
                "served value depends on which tier answers")
    if evidence["source_map_complete"] is not True:
        return "reports an incomplete source map; a day with no named source is unaccounted for"

    # ---- bound to ONE act ------------------------------------------------------------------
    if expected is None:
        return None
    manifest = evidence["manifest"]
    if type(manifest) is not dict:
        return "the manifest evidence is not a record"
    binding = (
        ("host", evidence.get("host"), expected.get("host")),
        ("run_id", evidence.get("run_id"), expected.get("run_id")),
        ("generation", manifest.get("generation"), expected.get("generation")),
        ("manifest_checksum", manifest.get("checksum"), expected.get("manifest_checksum")),
        ("source_map_digest", evidence.get("source_map_digest"),
         expected.get("source_map_digest")),
    )
    for field, got, want in binding:
        if want is None:
            continue
        if got is None:
            return (f"records no {field}; without it these gates are a passing result about "
                    f"some act, not about this one")
        if got != want:
            return (f"records {field}={got!r} while this run produced {want!r}. Gates "
                    f"collected against another run or another generation read exactly like "
                    f"gates that passed")
    want_state = expected.get("monolith_state")
    if want_state is not None:
        got_state = evidence.get("monolith_state")
        if type(got_state) is not dict:
            return ("records no monolith_state; the gates must be about the store that was "
                    "actually compacted")
        mismatch = _state_mismatch(want_state, got_state,
                                   left_name="the measurement taken at the execution point",
                                   right_name="the post-compaction monolith_state")
        if mismatch:
            return mismatch
    return None


def post_compaction_enforcement() -> Dict:
    """Which implemented operation enforces the post-compaction gates, and what it gates."""
    return {
        "verdict": "ENFORCED",
        "enforced_by": "ops/p5_oneshot_compaction.compact_one_shot",
        "gates": list(POST_COMPACTION_FIELDS),
        "how": ("the operation assembles this evidence from its OWN state -- the generation it "
                "published, the manifest checksum it got back, the monolith it measured under "
                "the locks, and a before/after inventory -- and calls "
                "`post_compaction_problem(evidence, expected=...)`. A problem refuses the run "
                "and issues no finalization clearance"),
        "what_it_gates": ("the finalization clearance, which is the ONLY thing that would ever "
                          "unlock prune, swap, delete or a service switch. No clearance, no "
                          "destructive step -- and the compaction itself deletes nothing "
                          "either way"),
        "still_not_implemented": list(DESTRUCTIVE_FAMILY),
    }


# ---------------------------------------------------------------- the capability
_MINT = object()


class _Authorization:
    """Permission to act on ONE writable store, for ONE run, ONE operation, exactly ONCE.

    Not a flag and not a boolean: a caller cannot write one down, cannot construct one, cannot
    serialize one into a config, and cannot use one twice. The `authorize_*` functions are the
    only way to obtain one, and they mint only after measuring the store themselves and finding
    that the record, the preflight and the measurement all agree.

    `consume()` is the single use. It re-checks the binding at the moment of use -- an
    authorization minted for another store, another run or another operation is refused there
    too -- and then marks itself spent, so a second call cannot proceed even inside the same
    process.

    **Subclassed per operation, and the subclass is part of the binding.** Provisioning and
    compaction take different capability TYPES, so a consumer that accepts one cannot be handed
    the other by accident: `isinstance` decides it, not a string comparison that a refactor can
    quietly widen. The operation name is checked too, and is the belt to that pair of braces."""

    #: The one operation a capability of this class may authorize. `None` on the base, which is
    #: therefore unmintable -- a capability for "some operation" is what this design refuses.
    OPERATION = None

    __slots__ = ("_host", "_run_id", "_operation", "_realpath", "_expires_utc",
                 "_measured", "_backup_id", "_approved_by", "_consumed")

    def __init__(self, mint, *, host, run_id, operation, realpath, expires_utc, measured,
                 backup_id, approved_by):
        if mint is not _MINT:
            raise UnsealedMonolithAuthorizationError(
                f"{type(self).__name__} cannot be constructed. It is a capability, not "
                "a value: the only way to hold one is an authorize_* function, which mints it "
                "after measuring the store and checking the record, the preflight and the "
                "measurement against each other")
        if type(self).OPERATION is None:
            raise UnsealedMonolithAuthorizationError(
                "_Authorization is abstract: a capability that names no operation would "
                "authorize whatever it was presented for")
        if operation != type(self).OPERATION:
            raise UnsealedMonolithAuthorizationError(
                f"{type(self).__name__} authorizes {type(self).OPERATION!r}, not "
                f"{operation!r}")
        self._host = host
        self._run_id = run_id
        self._operation = operation
        self._realpath = realpath
        self._expires_utc = expires_utc
        self._measured = measured
        self._backup_id = backup_id
        self._approved_by = approved_by
        self._consumed = False

    # A capability that can be pickled is one that can be written to a file and read back by
    # the next run, which is exactly the standing permission this is not.
    _NO_COPIES = ("an UnsealedMonolithAuthorization cannot be serialized or copied; one that "
                  "survives the process that earned it is a standing permission, and a second "
                  "one beside a spent original is a second use")

    def __reduce__(self):
        raise UnsealedMonolithAuthorizationError(self._NO_COPIES)

    def __copy__(self):
        raise UnsealedMonolithAuthorizationError(self._NO_COPIES)

    def __deepcopy__(self, memo):
        raise UnsealedMonolithAuthorizationError(self._NO_COPIES)

    def __repr__(self):
        return (f"<UnsealedMonolithAuthorization operation={self._operation!r} "
                f"run={self._run_id!r} consumed={self._consumed}>")

    @property
    def operation(self) -> str:
        return self._operation

    @property
    def realpath(self) -> str:
        return self._realpath

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def consumed(self) -> bool:
        return self._consumed

    @property
    def measured(self) -> Dict:
        return dict(self._measured)

    def matches(self, *, host: str, run_id: str) -> bool:
        """Is this capability bound to this host and run? For a caller that knows both.

        Separate from `consume()` on purpose. The code that spends the capability -- the
        builder -- knows the store it is about to act on and the operation it is performing,
        and does NOT independently know the run id; asking it to supply one would let it name
        whichever run it liked. The provisioner does know, and checks with this BEFORE handing
        the capability over."""
        return host == self._host and run_id == self._run_id

    def consume(self, *, operation: str, subject_realpath: str, now=None) -> Dict:
        """Spend it. Returns the evidence to record; raises if it is not for this.

        Checks what the spending code genuinely knows: the operation it is performing, the
        store it is about to act on, and the clock. Host and run were bound at the mint and are
        checked by `matches()` in the caller that knows them -- re-deriving them from the
        capability here would be the capability vouching for itself."""
        if self._consumed:
            raise UnsealedMonolithAuthorizationError(
                f"this authorization has already been used. One-shot means one use: minting a "
                f"second requires the whole chain again, including a fresh measurement of the "
                f"store ({self._realpath!r})")
        if operation != self._operation:
            raise UnsealedMonolithAuthorizationError(
                f"authorizes {self._operation!r}, presented for {operation!r}")
        if os.path.realpath(subject_realpath) != self._realpath:
            raise UnsealedMonolithAuthorizationError(
                f"authorizes the store at {self._realpath!r}, presented for "
                f"{os.path.realpath(subject_realpath)!r}")
        expires = _parse_utc(self._expires_utc)
        if expires is not None and _now(now) >= expires:
            raise UnsealedMonolithAuthorizationError(
                f"expired at {self._expires_utc}; a capability that outlives its window is a "
                f"standing permission")
        self._consumed = True
        return {
            "authorized": True,
            "scope": ONESHOT_SCOPE,
            "operation": self._operation,
            "host": self._host,
            "run_id": self._run_id,
            "monolith_realpath": self._realpath,
            "expires_utc": self._expires_utc,
            "backup_id": self._backup_id,
            "approved_by": self._approved_by,
            "measured_at_execution": dict(self._measured),
            "consumed_utc": _now(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "seal_check": ("REPLACED by this authorization. The store is writable and a person "
                           "accepted that risk for this one run; the content ledger is "
                           "re-verified immediately before the publish, which detects a change "
                           "rather than preventing one"),
        }


class UnsealedMonolithAuthorization(_Authorization):
    """Permission to publish generation 1 over ONE writable store, once."""

    __slots__ = ()
    OPERATION = OP_PROVISION


class OneShotCompactionAuthorization(_Authorization):
    """Permission to build and publish ONE compaction generation over a writable store, once.

    A DIFFERENT TYPE from the provisioning capability, not merely a different string. The
    consumer of each checks `isinstance`, so handing the compaction path a provisioning
    capability is a type error at the door rather than something a widened string comparison
    could let through. It authorizes building and publishing; it does not authorize removing
    anything, and no capability in this module does."""

    __slots__ = ()
    OPERATION = OP_COMPACTION


def _authorize(cls, *, record, host, run_id, monolith_path, pre, expected_dev, now):
    """Measure the store, check everything against it, and mint a single-use capability.

    Deliberately takes NO `observed` argument. A measurement supplied alongside the record
    proves only that whoever assembled the pair was consistent; this one is taken here, at the
    protected execution point, by the code that is about to act."""
    operation = cls.OPERATION
    measured = measure_monolith(monolith_path, now=now)
    verdict = evaluate(record=record, host=host, run_id=run_id, operation=operation,
                       observed=measured, subject_realpath=os.path.realpath(monolith_path),
                       pre=pre, expected_dev=expected_dev, now=now, observed_recomputed=True)
    if not verdict["eligible"]:
        raise OneShotExceptionRefused(
            f"the one-shot exception does not hold for {operation!r} on "
            f"{os.path.realpath(monolith_path)!r}: {verdict['problems']}")
    capability = cls(
        _MINT, host=host, run_id=run_id, operation=operation,
        realpath=os.path.realpath(monolith_path), expires_utc=record["expires_utc"],
        measured=measured, backup_id=record["backup_id"],
        approved_by=record["approved_by"])
    return capability, verdict


def authorize_unsealed(*, record, host: str, run_id: str, monolith_path: str,
                       pre: Dict, expected_dev: Optional[int] = None, operation: str = None,
                       now=None) -> Tuple[UnsealedMonolithAuthorization, Dict]:
    """Mint the PROVISIONING capability. `operation`, if given, must be that operation.

    It is accepted only so a caller that names the operation explicitly is checked rather than
    silently ignored -- passing the compaction name here must fail, not quietly provision."""
    if operation is not None and operation != OP_PROVISION:
        raise OneShotExceptionRefused(
            f"authorize_unsealed mints a {OP_PROVISION!r} capability; {operation!r} needs its "
            f"own authorize_* function, because the capability TYPE is part of the binding")
    return _authorize(UnsealedMonolithAuthorization, record=record, host=host, run_id=run_id,
                      monolith_path=monolith_path, pre=pre, expected_dev=expected_dev, now=now)


def authorize_compaction(*, record, host: str, run_id: str, monolith_path: str,
                         pre: Dict, expected_dev: Optional[int] = None, operation: str = None,
                         now=None) -> Tuple[OneShotCompactionAuthorization, Dict]:
    """Mint the COMPACTION capability. Same chain, different type, different operation name."""
    if operation is not None and operation != OP_COMPACTION:
        raise OneShotExceptionRefused(
            f"authorize_compaction mints a {OP_COMPACTION!r} capability; {operation!r} needs "
            f"its own authorize_* function, because the capability TYPE is part of the binding")
    return _authorize(OneShotCompactionAuthorization, record=record, host=host, run_id=run_id,
                      monolith_path=monolith_path, pre=pre, expected_dev=expected_dev, now=now)


# ---------------------------------------------------------------- the verdict
def evaluate(*, record, host, run_id, operation: str = OP_PROVISION, observed=None,
             subject_realpath=None, pre=None, post=None, expected_dev=None,
             observed_recomputed: bool = False, now=None) -> Dict:
    """Is a one-shot exception eligible? Returns a verdict record, never a bare bool.

    `eligible` is true only when the bound record checks out against a measurement THIS code
    took, and the pre-compaction evidence passes. A caller-supplied `observed` can never make it
    true -- `eligible_pending_recomputation` says everything else held -- because a report is
    not an authorization and the readiness assessor is a reporter.

    It says nothing about `compaction_ready`, which stays false without the read-only mount, and
    it authorizes exactly the one operation it names."""
    problems = {}
    problems["record"] = exception_problem(record, host=host, run_id=run_id,
                                           operation=operation, observed=observed,
                                           subject_realpath=subject_realpath, now=now)
    problems["pre_compaction"] = (
        pre_compaction_problem(pre, record=record, host=host, run_id=run_id,
                               expected_dev=expected_dev, now=now)
        if pre is not None else "no fresh pre-compaction evidence was collected")
    # The preflight and the at-execution measurement must be about the same store. Each is
    # already checked against the record; this catches the case where the record is the stale
    # one and the two live readings agree with it only by construction.
    if (type(observed) is dict and type(pre) is dict
            and type(pre.get("monolith_state")) is dict):
        problems["measurement_agreement"] = _state_mismatch(
            pre["monolith_state"], observed,
            left_name="the preflight monolith_state", right_name="the fresh measurement")
    if post is not None:
        problems["post_compaction"] = post_compaction_problem(post)
    blocking = {k: v for k, v in problems.items() if v}
    pending = not blocking
    if not observed_recomputed:
        blocking["measurement"] = (
            "the measurement was supplied by the caller, not recomputed by the operation at "
            "the point it acts. A record and an `observed` that agree prove only that whoever "
            "assembled them was consistent; authorize_unsealed() measures the store itself")
    return {
        "scope": ONESHOT_SCOPE,
        "operation": operation,
        "eligible": not blocking,
        "eligible_pending_recomputation": pending,
        "problems": blocking,
        "host": host, "run_id": run_id,
        "measurement_source": ("recomputed at the execution point" if observed_recomputed
                               else "supplied by the caller"),
        "authorizes": (f"one {operation}, on this host, in this run, against the monolith "
                       f"state measured at the execution point" if not blocking else None),
        "compaction_authorized": False,
        "compaction_note": operation_problem("compaction"),
        "backup_verification": backup_verification(record, pre),
        "post_compaction_enforcement": post_compaction_enforcement(),
        "note": ("This is a human risk acceptance, not evidence that the risk is small. It does "
                 "not make monolith_barrier READY, does not make compaction_ready true, and "
                 "does not authorize any later run. The read-only mount remains the stronger "
                 "and preferred route."),
    }


def rollback_limits() -> Sequence[str]:
    """What restoring the VM backup does NOT undo. Stated so the acceptance is informed."""
    return (
        "Restoring the VM rolls the WHOLE machine back, not the P5 roots alone: every other "
        "service on it loses the same interval.",
        "Delta days appended between the backup and the restore are lost, and the ingest cron "
        "will not re-fetch them on its own.",
        "Any repair committed to the WAL in that interval is lost with it, and the anchor's "
        "high-water mark will refuse the restored log as rolled back -- which is correct, and "
        "is manual recovery.",
        "Requests already answered from a bad generation cannot be recalled; clients that "
        "cached them keep them.",
        "The restore has to be decided and started by a person, so the exposure is the "
        "detection time plus the restore time, not the restore time alone.",
    )


# ---------------------------------------------------------------- the finalization clearance
#: What a clearance declares itself to be. A record of another scope is not this one.
CLEARANCE_SCOPE = "p5_finalization_clearance"

#: Everything a clearance binds. The same discipline as the exception record: a clearance that
#: does not name the act it came out of authorizes whatever it is shown to.
CLEARANCE_FIELDS = ("scope", "host", "run_id", "generation", "manifest_checksum",
                    "monolith_day_digest", "source_map_digest", "issued_utc", "issued_by",
                    "gates_passed")


class FinalizationRefused(Exception):
    """A destructive step was reached without a clearance that covers it."""


def issue_finalization_clearance(*, host, run_id, generation, manifest_checksum,
                                 monolith_day_digest, source_map_digest, issued_by,
                                 gates_passed, now=None) -> Dict:
    """Mint the record that says every post-compaction gate passed for ONE published act.

    Deliberately a plain dict rather than a capability object: unlike the unsealed
    authorization, this one is *meant* to outlive the process. A person has to read it, review
    it and approve a separate finalization on the strength of it, and a capability that cannot
    be serialized cannot be reviewed. What keeps it from becoming a standing permission is that
    `check_finalization_clearance()` binds it to a generation and a manifest checksum, both of
    which move the moment anything else is published.

    It is NOT an approval. It records that the gates passed; a human still has to approve the
    destructive step separately, and nothing in this tree implements one."""
    if gates_passed is not True:
        raise FinalizationRefused(
            "a clearance may only be issued when every post-compaction gate passed; issuing "
            "one otherwise is the failure it exists to prevent")
    return {
        "scope": CLEARANCE_SCOPE,
        "host": host, "run_id": run_id,
        "generation": generation,
        "manifest_checksum": manifest_checksum,
        "monolith_day_digest": monolith_day_digest,
        "source_map_digest": source_map_digest,
        "issued_utc": _now(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "issued_by": issued_by,
        "gates_passed": True,
        "authorizes": None,
        "note": ("Evidence that the gates passed for this generation. NOT an approval to "
                 "delete anything: the destructive step is a separate operation with its own "
                 "human approval, and nothing in this tree implements one. Presenting this "
                 "where a compaction capability is wanted does not work either -- it is a "
                 "record, not a capability."),
    }


def clearance_problem(clearance, *, host, run_id, generation, manifest_checksum, now=None):
    """The first thing wrong with a finalization clearance for this act, or `None`."""
    if type(clearance) is not dict:
        return (f"is {type(clearance).__name__}, not a clearance. There is no form of "
                f"'cleared' that is merely truthy")
    for field in CLEARANCE_FIELDS:
        if field not in clearance:
            return f"records no {field}; a partial clearance binds nothing"
    if clearance["scope"] != CLEARANCE_SCOPE:
        return f"declares scope {clearance['scope']!r}; this needs {CLEARANCE_SCOPE!r}"
    if clearance["gates_passed"] is not True:
        return (f"records gates_passed={clearance['gates_passed']!r}. A clearance that says "
                f"the gates did not pass is a refusal someone kept the paperwork for")
    for field, want in (("host", host), ("run_id", run_id), ("generation", generation),
                        ("manifest_checksum", manifest_checksum)):
        if want is not None and clearance[field] != want:
            return (f"was issued for {field}={clearance[field]!r}, not {want!r}. A clearance "
                    f"for one published generation says nothing about another")
    if _parse_utc(clearance["issued_utc"]) is None:
        return f"records issued_utc {clearance['issued_utc']!r}, which is not a timestamp"
    return None


def refuse_destructive_step(operation, clearance=None, *, host=None, run_id=None,
                            generation=None, manifest_checksum=None, approval=None,
                            manifest_root=None, now=None):
    """The gate a future prune / swap / delete / switch MUST call before touching anything.

    Three independent things are required and none substitutes for another:

    * **no quarantined generation** in the manifest root. A generation that was published and
      then failed its gates is unverified, and removing the data underneath an unverified
      authority is the worst available order of operations;
    * a **clearance** proving the post-compaction gates passed for exactly this generation, and
    * a separate **human approval** for the destructive step itself.

    Gates passing means the new generation is trustworthy. It does not mean anyone decided the
    old data should go. Those are different judgements and the second is a person's.

    Nothing in this tree implements a destructive step, so today this function exists to be
    called by code that does not exist yet -- and to make the shape of that contract fixed
    before someone writes it in a hurry."""
    if operation not in DESTRUCTIVE_FAMILY:
        raise FinalizationRefused(
            f"{operation!r} is not a destructive step; this gate is for "
            f"{list(DESTRUCTIVE_FAMILY)}")
    if manifest_root is not None:
        from ops.p5_oneshot_compaction import quarantine_problem
        quarantined = quarantine_problem(manifest_root)
        if quarantined:
            raise FinalizationRefused(f"{operation!r} refused: {quarantined}")
    problem = clearance_problem(clearance, host=host, run_id=run_id, generation=generation,
                                manifest_checksum=manifest_checksum, now=now)
    if problem:
        raise FinalizationRefused(
            f"{operation!r} refused: the finalization clearance {problem}")
    if approval is not True:
        raise FinalizationRefused(
            f"{operation!r} refused: the gates passing is not a decision to remove anything. "
            f"A separate human approval for the destructive step is required, and "
            f"approval={approval!r} is not one")
    return {"operation": operation, "cleared": True,
            "generation": clearance["generation"],
            "note": ("Both conditions met. This function does not perform the step -- nothing "
                     "in this tree does."),
            }
