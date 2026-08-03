"""dev2026 — P5-S4: the repair write-ahead log (design spec §7.5a-1).

`p5_repairs.jsonl` is the **only authorizing evidence** for dropping a repaired day from delta.
Everything else about a repair — the delta rebuild, the swap, the block fold — is mechanics; this
file is the record that says *which* correction the base demonstrably carries. Its integrity
rules are therefore part of the contract, not an implementation detail.

**Why identity and not time.** Round-5 of the spec review made the prune gate a timestamp
comparison ("the block was built after `repaired_at_utc`"). That is not proof. Build time does
not establish that the build *consumed* the corrected value, and if the delta repair becomes
visible before its log line is appended, a fold in that window reads the corrected value and
records nothing — or worse, reads the *old* value while the later-written timestamp makes it
look authorized. Ordering is not identity. So every record carries a `repair_id`, and the prune
gate compares ids, never clocks.

**Fail-closed everywhere.** The parser validates the whole file before answering any query and
**never skips a bad line** — skipping is precisely the behaviour that would silently authorize a
prune. When the log is untrustworthy the affected days simply stay in delta, where they are
served correctly. A paused prune costs disk; a wrongly authorized prune loses data permanently.

**What "blocked" costs.** An open `repair_intent` blocks its day forever, with no timeout and no
automatic resolution. That is deliberate: an open intent means the day's state is *unknown*, and
closing it is a human act — re-validate and append `repair_committed`, or establish the repair
never landed and append `repair_aborted`.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from typing import Dict, List, NamedTuple, Optional, Sequence

WAL_NAME = "p5_repairs.jsonl"
LOCK_NAME = "p5_repairs.lock"

INTENT = "repair_intent"
COMMITTED = "repair_committed"
ABORTED = "repair_aborted"
LOG_TAIL_REPAIRED = "log_tail_repaired"

RECORD_TYPES = (INTENT, COMMITTED, ABORTED, LOG_TAIL_REPAIRED)
TERMINAL = (COMMITTED, ABORTED)

#: every record carries exactly these keys -- no more, no less. An unknown key is a different
#: schema version or a hand-edit, and either way the record is not the one we validated.
_FIELDS = ("seq", "repair_id", "day", "record", "at_utc", "operator", "payload",
           "prev_checksum", "record_checksum")


class WalError(Exception):
    """The WAL is not trustworthy, or an append would violate its rules."""


class WalCorrupt(WalError):
    """Integrity failure. Scope of the resulting refusal is on `.scope`."""

    def __init__(self, message: str, *, scope: str = "all", days: Sequence[str] = ()):
        super().__init__(message)
        self.scope = scope                     # "all" | "days"
        self.days = tuple(days)


# --------------------------------------------------------------------------- serialization
def canonical(record: dict) -> str:
    """Canonical JSON: sorted keys, no whitespace drift, and no NaN/Infinity.

    `allow_nan=False` is not decoration. `json.dumps` emits bare `NaN`/`Infinity` tokens by
    default, which are not JSON, and a checksum computed over a document another parser cannot
    read is not evidence of anything."""
    return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)


def record_checksum(record: dict) -> str:
    """sha256 over the record canonicalized with `record_checksum` set to `""`.

    The field must be *present and empty* rather than absent, so that the digest covers the
    same key set the stored record has."""
    base = dict(record)
    base["record_checksum"] = ""
    return hashlib.sha256(canonical(base).encode()).hexdigest()


# --------------------------------------------------------------------------- parse
class RepairState(NamedTuple):
    repair_id: str
    day: str
    state: str                                  # "open" | COMMITTED | ABORTED
    fingerprint: Optional[str]                  # committed fingerprint, else None


class WalState(NamedTuple):
    records: tuple
    last_seq: int
    last_checksum: Optional[str]
    repairs: dict                               # repair_id -> RepairState
    blocked_days: frozenset                     # days with an OPEN intent
    #: days whose evidence is individually untrustworthy (conflicting terminals, orphan
    #: terminal). Distinct from a whole-log failure, which raises instead.
    poisoned_days: frozenset

    def latest_committed(self, day: str) -> Optional[RepairState]:
        """The highest-`seq` committed repair for `day`, or None if it was never repaired."""
        best, best_seq = None, -1
        for rec in self.records:
            if rec["day"] == day and rec["record"] == COMMITTED:
                if rec["seq"] > best_seq:
                    best_seq, best = rec["seq"], self.repairs.get(rec["repair_id"])
        return best


def _fail(msg, **kw):
    raise WalCorrupt(msg, **kw)


def parse_wal(path: str) -> WalState:
    """Strict, whole-file, fail-closed. Never skips a record.

    Reading is done under a SHARED flock by `read_wal()`, so a benign in-progress append is
    never mistaken for a crash tear. This function is the pure parser and does no locking, so
    it can also be called on a copy during administrative recovery."""
    if not os.path.isfile(path):
        return WalState((), 0, None, {}, frozenset(), frozenset())
    with open(path, "r") as fh:
        raw = fh.read()
    return parse_text(raw, path=path)


def parse_text(raw: str, *, path: str = "<wal>") -> WalState:
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines.pop()                              # the trailing newline of a complete record
    else:
        if lines:
            _fail(f"{path}: the final record has no terminating newline -- it is torn. All "
                  f"prune is refused: the torn record's day is unknowable, so no day can be "
                  f"shown to be unaffected.")

    records: List[dict] = []
    prev_checksum: Optional[str] = None
    prev_seq = 0
    for i, line in enumerate(lines):
        n = i + 1
        try:
            rec = json.loads(line)
        except ValueError as exc:
            _fail(f"{path}: line {n} is not valid JSON ({exc}). All prune is refused.")
        if not isinstance(rec, dict):
            _fail(f"{path}: line {n} is not a JSON object. All prune is refused.")
        missing = [k for k in _FIELDS if k not in rec]
        extra = [k for k in rec if k not in _FIELDS]
        if missing or extra:
            _fail(f"{path}: line {n} has the wrong field set (missing={missing}, "
                  f"unexpected={extra}). All prune is refused.")
        if rec["record"] not in RECORD_TYPES:
            _fail(f"{path}: line {n} has unknown record type {rec['record']!r}.")
        if not isinstance(rec["seq"], int) or isinstance(rec["seq"], bool):
            _fail(f"{path}: line {n} `seq` must be an integer, got {rec['seq']!r}.")

        # ---- checksum BEFORE anything is believed, including this record's own `day`
        want = record_checksum(rec)
        if rec["record_checksum"] != want:
            _fail(f"{path}: line {n} (seq {rec['seq']}) fails its record_checksum. A corrupt "
                  f"record makes the whole chain untrustworthy and its own `day` field cannot "
                  f"be believed either, so ALL prune is refused.")
        if rec["seq"] != prev_seq + 1:
            _fail(f"{path}: seq gap at line {n}: expected {prev_seq + 1}, got {rec['seq']}. "
                  f"Records are missing; all prune is refused.")
        if prev_seq == 0:
            if rec["prev_checksum"] is not None:
                _fail(f"{path}: seq 1 must carry prev_checksum=null, got "
                      f"{rec['prev_checksum']!r}.")
        else:
            if rec["prev_checksum"] is None:
                _fail(f"{path}: line {n} (seq {rec['seq']}) has prev_checksum=null, which is "
                      f"legal only at seq 1. Chain break; all prune is refused.")
            if rec["prev_checksum"] != prev_checksum:
                _fail(f"{path}: line {n} (seq {rec['seq']}) breaks the checksum chain. All "
                      f"prune is refused.")
        prev_checksum, prev_seq = rec["record_checksum"], rec["seq"]
        records.append(rec)

    repairs, poisoned = _replay(records, path)
    blocked = frozenset(r.day for r in repairs.values() if r.state == "open")
    return WalState(tuple(records), prev_seq, prev_checksum, repairs, blocked,
                    frozenset(poisoned))


def _replay(records: Sequence[dict], path: str):
    """The per-`repair_id` state machine: intent -> {committed | aborted}."""
    repairs: Dict[str, RepairState] = {}
    id_day: Dict[str, str] = {}
    poisoned = set()
    for rec in records:
        rid, day, kind = rec["repair_id"], rec["day"], rec["record"]
        if kind == LOG_TAIL_REPAIRED:
            continue                              # administrative; carries no repair state
        if rid in id_day and id_day[rid] != day:
            _fail(f"{path}: repair_id {rid!r} is reused for a different day "
                  f"({id_day[rid]} then {day}). All prune is refused.")
        id_day[rid] = day

        if kind == INTENT:
            if rid in repairs:
                _fail(f"{path}: duplicate repair_intent for {rid!r}. All prune is refused.")
            repairs[rid] = RepairState(rid, day, "open", None)
            continue

        # terminal
        cur = repairs.get(rid)
        if cur is None:
            # A terminal with no intent: scoped to this day, not the whole log. The rest of
            # the chain is intact, so refusing everything would be an overreaction -- but this
            # day's evidence is incoherent and must not authorize anything.
            poisoned.add(day)
            continue
        fp = rec["payload"].get("fingerprint") if kind == COMMITTED else None
        if cur.state == "open":
            repairs[rid] = RepairState(rid, day, kind, fp)
            continue
        # already terminal -- only a byte-identical replay of the SAME terminal is a no-op
        if cur.state != kind or (kind == COMMITTED and cur.fingerprint != fp):
            poisoned.add(day)
    return repairs, poisoned


def read_wal(root: str) -> WalState:
    """Parse under a SHARED lock, so an in-progress append is never read as a crash tear."""
    path = os.path.join(root, WAL_NAME)
    lock = os.path.join(root, LOCK_NAME)
    if not os.path.isfile(path):
        return parse_wal(path)
    os.makedirs(root, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        try:
            return parse_wal(path)
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- append
def append(root: str, *, record: str, repair_id: str, day: str, at_utc: str,
           operator: str, payload: Optional[dict] = None) -> dict:
    """Validate the ENTIRE existing log, then append exactly one fsync'd record.

    There is no write-only fast path. If the log does not fully validate the append is refused
    and the file is left byte-unchanged -- valid records are never appended after a corrupt
    tail, because that would bury the corruption under legitimate-looking history and make the
    administrative `log_tail_repaired` recovery ambiguous about what was lost.

    A writer that cannot append is not a data-loss event: the repair simply does not proceed
    until the log is repaired."""
    if record not in RECORD_TYPES:
        raise WalError(f"unknown record type {record!r}")
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, WAL_NAME)
    lock = os.path.join(root, LOCK_NAME)
    existed = os.path.isfile(path)

    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)           # blocking: held only for the append itself
        state = parse_wal(path)                  # <- refuses the append if anything is wrong
        _check_transition(state, record=record, repair_id=repair_id, day=day,
                          payload=payload or {})
        seq = state.last_seq + 1
        rec = {"seq": seq, "repair_id": repair_id, "day": day, "record": record,
               "at_utc": at_utc, "operator": operator, "payload": dict(payload or {}),
               "prev_checksum": state.last_checksum if seq > 1 else None,
               "record_checksum": ""}
        rec["record_checksum"] = record_checksum(rec)
        line = canonical(rec) + "\n"
        wfd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_CLOEXEC, 0o644)
        try:
            os.write(wfd, line.encode())
            os.fsync(wfd)                        # per record, never batched
        finally:
            os.close(wfd)
        if not existed:
            # A brand-new file can be lost entirely on a crash even after its own fsync,
            # because the directory entry is a separate write.
            _fsync_dir(root)
        return rec
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _check_transition(state: WalState, *, record: str, repair_id: str, day: str, payload: dict):
    if record == LOG_TAIL_REPAIRED:
        return
    cur = state.repairs.get(repair_id)
    for rec in state.records:
        if rec["repair_id"] == repair_id and rec["day"] != day:
            raise WalError(f"repair_id {repair_id!r} is already bound to day {rec['day']}, "
                           f"cannot reuse it for {day}")
    if record == INTENT:
        if cur is not None:
            raise WalError(f"repair_intent for {repair_id!r} already exists (state "
                           f"{cur.state!r}); allocate a new repair_id")
        return
    if cur is None:
        raise WalError(f"{record} for {repair_id!r} has no matching repair_intent. A terminal "
                       f"record without an intent cannot authorize anything.")
    if cur.state == "open":
        return
    # Idempotent replay: the expected retry after an uncertain fsync.
    if cur.state == record and (record != COMMITTED
                                or cur.fingerprint == payload.get("fingerprint")):
        return
    raise WalError(
        f"repair {repair_id!r} is already {cur.state!r}; a conflicting terminal record is "
        f"invalid. Exactly one terminal state per intent, and only a byte-identical replay of "
        f"the same terminal is a no-op.")


def _fsync_dir(dirpath: str):
    fd = os.open(dirpath, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- the prune gate
def prune_authorization(state: WalState, days: Sequence[str], *,
                        materialized_repairs: Optional[dict] = None) -> dict:
    """E1 (§7.5a): may these delta days be dropped, on the WAL's evidence alone?

    Returns `{"authorized": [...], "refused": {day: reason}}`. **Never raises for a refusal** —
    a refused day is a normal, expected outcome that leaves the day in delta where it is served
    correctly.

    `materialized_repairs` is the covering block's `build_provenance.materialized_repairs`. For
    a day that was repaired, the block must name the **latest committed** `repair_id` and carry
    the **same fingerprint** the WAL committed. Anything else — an absent entry, an older id, a
    matching id with a different fingerprint — is refused exactly like a stale block.

    Build timestamps appear nowhere in this function, by design."""
    mr = dict(materialized_repairs or {})
    authorized, refused = [], {}
    for day in days:
        if day in state.poisoned_days:
            refused[day] = ("the WAL's evidence for this day is incoherent (a conflicting or "
                            "orphan terminal record); a human must record the true outcome")
            continue
        if day in state.blocked_days:
            refused[day] = ("an open repair_intent exists for this day: its state is unknown, "
                            "and unknown is refused. Append repair_committed or repair_aborted "
                            "to close it -- there is no timeout")
            continue
        latest = state.latest_committed(day)
        if latest is None:
            authorized.append(day)               # never repaired -> the WAL has no objection
            continue
        entry = mr.get(day)
        if not entry:
            refused[day] = (f"repair {latest.repair_id} is committed for this day but the "
                            f"covering block records no materialized repair for it -- the "
                            f"correction has not been folded into base")
            continue
        if entry.get("repair_id") != latest.repair_id:
            refused[day] = (f"the block materialized repair {entry.get('repair_id')!r} but the "
                            f"latest committed repair is {latest.repair_id!r}; a later repair "
                            f"supersedes what the block carries")
            continue
        if entry.get("source_fingerprint") != latest.fingerprint:
            refused[day] = (f"the block names repair {latest.repair_id} but its recorded "
                            f"fingerprint does not match the WAL's committed fingerprint")
            continue
        authorized.append(day)
    return {"authorized": authorized, "refused": refused}


def refuse_all_reason(exc: WalCorrupt) -> dict:
    """Shape a whole-log integrity failure into the refusal callers return."""
    return {"status": "refused", "reason": "repair_wal_untrustworthy",
            "scope": exc.scope, "detail": str(exc),
            "hint": ("The affected days stay in delta, where they are served correctly. "
                     "Resolution is administrative: inspect, truncate to the last valid "
                     "record, and append a checksummed log_tail_repaired record. Never edit "
                     "a record in place.")}
