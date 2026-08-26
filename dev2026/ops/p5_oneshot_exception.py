"""dev2026 — the one-shot compaction risk exception, and the checks that surround it.

A human has decided to accept the risk of running **one** compaction without the read-only
mount barrier, on the grounds that VM24 has full automated VM backups. This module is that
decision expressed as something checkable.

## What this is not

It is **not** a `force` flag, a `skip_seal` switch, an `allow_unsealed` parameter or an
environment variable. Every one of those is a bypass whose blast radius is "whatever the caller
felt like", reusable by the next caller who finds it, and impossible to audit afterwards. This
is a record bound to one host, one run, one monolith at one measured state, one verified backup
and one expiry — and if any of those does not match what is in front of it, it refuses.

The binding to the monolith is checked twice over, because a record and a fresh measurement
agreeing with each other prove only that whoever assembled them was consistent. The caller that
is about to act supplies `subject_realpath` from its own argument, so the store named in the
record is checked against the store the run will actually touch, not against the run's own
account of it.

It also does **not** weaken `monolith_barrier`. That requirement goes on reporting `BLOCKED` or
`NOT_ADJUDICATED` exactly as before, and `compaction_ready` goes on being `false` without a
read-only mount. The exception is reported separately, as its own eligibility, because the two
say different things: "this deployment is safe to compact" and "a person accepted the risk of
compacting this deployment once, and here is what they bound that acceptance to".

## Why the backup is not an argument that the risk is small

The operator's reasoning is that a VM-level backup makes the failure recoverable, not that it
makes it unlikely. Those are different claims and this module treats them differently: the
backup is required, verified and bound into the record, and nothing here treats its existence
as a reason to relax any other check. §"Rollback limits" in the runbook records what restoring
a VM backup actually costs and what it cannot undo.

## The stronger route is still the preferred one

A read-only mount makes the monolith unable to change, which no amount of after-the-fact
detection matches. This exception exists because one compaction is wanted before that layout
change can be arranged, and it expires.
"""
from __future__ import annotations

import datetime
from typing import Dict, Optional, Sequence

#: The scope an exception record must declare. A record approving anything else -- an assessor
#: build, a provisioning run, a shadow -- is not this.
ONESHOT_SCOPE = "p5_oneshot_compaction_risk_acceptance"

#: Every field the record must carry. There is no partial form: a record missing any of these
#: does not bind the thing it is supposed to bind.
EXCEPTION_FIELDS = (
    "scope", "host", "run_id",
    "monolith_realpath", "monolith_metadata_fingerprint", "monolith_day_digest",
    "monolith_content_ledger",
    "backup_id", "backup_completed_utc", "backup_verified", "backup_retention_ref",
    "expires_utc", "approved_by", "approved_utc", "reason",
)

#: Fields the fresh pre-compaction evidence must carry, each answering a question that cannot
#: be answered from a document.
PRE_COMPACTION_FIELDS = (
    "capacity",              # fresh statvfs bound to the device holding the data root
    "healthz",               # the service's own current view
    "identity",              # a clean checkout of a reviewed commit
    "quiescence",            # process / PM2 / cron state
    "monolith_writers",      # who has the monolith open for writing
    "locks",                 # compaction and ingest lock state
    "monolith_state",        # fresh fingerprints and content ledger
    "backup",                # the verified backup this run is covered by
    "plan",                  # exact source map and destination manifest plan
)

#: Gates that must all pass AFTER the compaction and BEFORE anything is deleted or switched.
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


class OneShotExceptionRefused(Exception):
    """The exception record is absent, malformed, or is not for what is in front of it."""


def _exact_str(value) -> bool:
    return type(value) is str and bool(value.strip())


def _is_sha256(value) -> bool:
    if type(value) is not str or len(value) != 64:
        return False
    return all(c in "0123456789abcdef" for c in value)


def _parse_utc(value):
    if not _exact_str(value):
        return None
    try:
        stamped = datetime.datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return stamped.replace(tzinfo=datetime.timezone.utc)


def _now(now=None):
    return now or datetime.datetime.now(datetime.timezone.utc)


def exception_problem(record, *, host: str, run_id: str, observed: Optional[Dict] = None,
                      subject_realpath: Optional[str] = None, now=None):
    """The first thing wrong with a one-shot exception record, or `None`.

    `observed` is what is TRUE NOW -- the monolith's realpath, metadata fingerprint, day digest
    and content ledger, measured fresh. The record says what a person looked at when they
    accepted the risk; `observed` says what they would be accepting it for today. A record whose
    subject has changed since it was signed is not an acceptance of this risk, it is an
    acceptance of a different one, and there is no way to tell from the record alone which.

    `subject_realpath` is the store the CALLER is about to act on, and it is not optional in
    spirit: record and `observed` agreeing with each other proves only that whoever assembled
    them was consistent, not that either describes the monolith this run will publish over. The
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
    for field in ("host", "run_id", "monolith_realpath", "backup_id", "backup_retention_ref",
                  "approved_by", "reason"):
        if not _exact_str(record[field]):
            return f"records {field} {record[field]!r}, which is not a non-empty string"
    if len(record["reason"].split()) < 5:
        return (f"records reason {record['reason']!r}, which is not a reason. The whole point "
                f"of an exception is that someone said why in words a reviewer can weigh")

    # ---- bound to THIS host and THIS run -------------------------------------------------
    if record["host"] != host:
        return (f"was approved for host {record['host']!r}, not {host!r}. A risk accepted for "
                f"one machine is not accepted for another")
    if record["run_id"] != run_id:
        return (f"was approved for run {record['run_id']!r}, not {run_id!r}. ONE run is what "
                f"one-shot means; a record that outlives its run is a standing permission")

    # ---- bound to the monolith AS IT IS NOW ----------------------------------------------
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
    for field, label in (("monolith_realpath", "realpath"),
                         ("monolith_metadata_fingerprint", "metadata fingerprint"),
                         ("monolith_day_digest", "day digest")):
        if field not in observed:
            return f"the fresh measurement records no {field}"
        if observed[field] != record[field]:
            return (f"was approved for a monolith whose {label} was {record[field]!r}, but it "
                    f"is now {observed[field]!r}. The store changed after the risk was "
                    f"accepted, so what was accepted is not what would be run")
    observed_ledger = observed.get("monolith_content_ledger")
    if type(observed_ledger) is not dict:
        return "the fresh measurement records no monolith_content_ledger"
    if observed_ledger.get("ledger_digest") != ledger["ledger_digest"]:
        return ("was approved for a monolith whose content ledger has since changed; the store "
                "was written to between the approval and now, which is the exact hazard the "
                "read-only mount exists to prevent")

    # ---- bound to a verified backup --------------------------------------------------------
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


def pre_compaction_problem(evidence, *, now=None):
    """The first thing wrong with the fresh pre-compaction evidence, or `None`.

    Every field answers a question that cannot be answered from a document: what the disk holds
    now, what the service currently serves, what is running, what holds the locks, and what the
    plan actually is. A compaction started on stale answers is one whose preconditions were true
    at some point."""
    problem = _missing(evidence, PRE_COMPACTION_FIELDS, "the pre-compaction evidence")
    if problem:
        return problem
    writers = evidence["monolith_writers"]
    if type(writers) is not dict or "count" not in writers:
        return "the pre-compaction evidence does not say how many processes hold the monolith"
    if type(writers["count"]) is bool or not isinstance(writers["count"], int):
        return f"monolith_writers.count is {writers['count']!r}, which is not a count"
    if writers["count"] != 0:
        return (f"reports {writers['count']} process(es) with the monolith open for writing. "
                f"Compacting a base something else is writing is the hazard, not the risk")
    plan = evidence["plan"]
    for field in ("source_map", "destination_manifest", "days"):
        if type(plan) is not dict or field not in plan:
            return f"the plan records no {field}; an unstated plan cannot be reviewed"
    return None


def post_compaction_problem(evidence):
    """The first thing wrong with the post-compaction gates, or `None`.

    All of them must pass BEFORE anything is deleted or any service is switched. The ordering is
    the point: a parity failure discovered after the old data is gone is a parity failure with
    nothing to go back to."""
    problem = _missing(evidence, POST_COMPACTION_FIELDS, "the post-compaction evidence")
    if problem:
        return problem
    if evidence["nothing_deleted"] is not True:
        return (f"records nothing_deleted={evidence['nothing_deleted']!r}. No daily or base "
                f"data may be removed until every gate here has passed")
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
    if type(parity["days_checked"]) is bool or not isinstance(parity["days_checked"], int) \
            or parity["days_checked"] <= 0:
        return f"reports days_checked={parity['days_checked']!r}, which checks nothing"
    if evidence["base_delta_disjoint"] is not True:
        return ("reports base and delta are not disjoint; a day claimed by both is a day whose "
                "served value depends on which tier answers")
    if evidence["source_map_complete"] is not True:
        return "reports an incomplete source map; a day with no named source is unaccounted for"
    return None


def evaluate(*, record, host, run_id, observed=None, subject_realpath=None, pre=None,
             post=None, now=None) -> Dict:
    """Is a one-shot compaction eligible? Returns a verdict record, never a bare bool.

    `eligible` is true only when the bound record checks out against a fresh measurement AND the
    pre-compaction evidence passes. It says nothing about `compaction_ready`, which stays false
    without the read-only mount, and it authorizes exactly the run it names."""
    problems = {}
    problems["record"] = exception_problem(record, host=host, run_id=run_id,
                                           observed=observed,
                                           subject_realpath=subject_realpath, now=now)
    problems["pre_compaction"] = (pre_compaction_problem(pre, now=now) if pre is not None
                                  else "no fresh pre-compaction evidence was collected")
    if post is not None:
        problems["post_compaction"] = post_compaction_problem(post)
    blocking = {k: v for k, v in problems.items() if v}
    return {
        "scope": ONESHOT_SCOPE,
        "eligible": not blocking,
        "problems": blocking,
        "host": host, "run_id": run_id,
        "authorizes": ("one compaction, on this host, in this run, against the monolith state "
                       "named in the record" if not blocking else None),
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
