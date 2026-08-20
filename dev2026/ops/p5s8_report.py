"""dev2026 — P5-S8: assemble the read-only evidence report, or refuse.

The runbook's artifact table used to be a **promise**, not a contract: it listed files the
command blocks never created, so a run could exit 0 and leave almost none of its own evidence.
This module is the contract in executable form — it names the artifacts a profile must produce,
validates each one, and **fails closed** rather than assembling a report around a hole.

## Why refusal is the whole point

A partially-collected run is the most dangerous artifact of all: it looks like evidence. Every
figure in it is real, and the reader has no way to see that the measurement it was meant to
support never ran. So a missing file, an unparseable one, or a preflight lacking a required
field all raise; nothing is written.

## `not_applicable` is a value, not an absence

Under `legacy_baseline` there is no P5 WAL, so `wal_repairs_pending` cannot carry a number — and
a `0` there would assert a clean WAL that does not exist. The field must be present and carry
`not_applicable_with_reason`. A bare `null` is refused too: "no answer" and "this question does
not apply, because …" are different states, and only the second is evidence.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List

from ops.p5_roadmap import S8_PROFILES, S8_PREFLIGHT_FIELDS

LABEL = "VM24_PRODUCTION_READ_ONLY"

#: The endpoint every G4 request must use, and the parameters it must carry.
#: The canonical VM24 service endpoint. `conf/start_app.sh` runs
#: `gunicorn ... -b 127.0.0.1:8035`, and staging found the contract pinned 8080, where nothing
#: listens -- so `g4-probe` could not run at all. Still pinned BY VALUE rather than made
#: configurable: an arbitrary endpoint would need its own deployment binding to mean anything,
#: and this run targets the deployed service.
G4_ENDPOINT = "http://127.0.0.1:8035/api/ghrsst"
G4_REQUIRED_PARAMS = ("lon0", "lat0", "start", "end", "append")
#: The canonical probe parameters. Pinned by VALUE, not merely required to exist: a request
#: with a different coordinate or variable is a different measurement, and `span_days: 366`
#: says nothing about which point was sampled. Changing these is an explicit edit here, which
#: is the point -- it should not be possible by editing an artifact after the fact.
G4_EXPECTED_PARAMS = {"lon0": "135", "lat0": "20", "append": "sst"}


class ArtifactError(Exception):
    """A run whose evidence is incomplete or unreadable. Never downgraded to a warning."""


#: Artifacts every profile must produce. `wal_state.json` is included for BOTH profiles: under
#: `legacy_baseline` it carries the not-applicable record, which is itself evidence.
_COMMON = ("run.json", "identity.json", "preflight.json", "wal_state.json", "capacity.json", "df_h.txt",
           "df_i.txt", "findmnt.txt", "g4_request.json", "g4_366d_seconds.txt",
           "pm2_jlist.json", "h7_fd_rss_timeseries.txt", "g16_metadata_scan.txt")

#: The `p5_shadow` profile is verification-only (see `S8_PROFILES`), so it produces the
#: provisioning record and the identity preflight, and none of the measurement series.
_BY_PROFILE: Dict[str, tuple] = {
    "legacy_baseline": _COMMON,
    "p5_shadow": ("run.json", "identity.json", "preflight.json", "wal_state.json", "capacity.json", "df_h.txt",
                  "df_i.txt", "findmnt.txt", "shadow_verification.json"),
}

#: Minimal well-formed contents, used by tests to build a complete run without re-encoding the
#: schema in the test file. Keeping them here means the contract has exactly one definition.
def _example_preflight(run_id="TEST"):
    """A stub that PASSES `check_preflight_values`. It has to: a stub that would be rejected
    makes every test built on it a test of the stub."""
    na = S8_PROFILES["legacy_baseline"]["not_applicable"]
    out = {}
    for f in S8_PREFLIGHT_FIELDS:
        if f in na:
            out[f] = f"not_applicable_with_reason: {na[f]}"
        elif f in _COUNT_FIELDS:
            out[f] = 1
        elif f == "deploy_sha":
            out[f] = "0" * 40
        elif f == "label":
            out[f] = LABEL
        elif f == "run_id":
            out[f] = run_id
        else:
            out[f] = f"/stub/{f}"
    return out


EXAMPLE_STUBS = {
    "preflight.json": None,          # filled below, once the validators exist
    "wal_state.json": {"wal_repairs_pending": None,
                       "not_applicable_with_reason": "legacy_baseline: no P5 WAL is deployed"},
    "pm2_jlist.json": {"run_id": "TEST",
                       "roster": [{"name": "ghrsst", "pid": 1,
                                   "pm2_env": {"status": "online"}}]},
    "capacity.json": {"run_id": "TEST", "filesystem_device": "/dev/stub",
                      "filesystem_fstype": "ext4", "free_now_bytes": 1, "inodes_free": 1},
    "run.json": {"run_id": "TEST", "created_utc": "2027-01-01T00:00:00Z"},
    #: Filled at import time from a real file, so the stub passes `check_identity` -- its
    #: module fingerprint must match something that actually exists on disk.
    "identity.json": None,
    #: A stub that PASSES `check_g4_request`, for the same reason the preflight stub must pass
    #: its validator: a rejected stub makes every test built on it a test of the stub.
    "g4_request.json": {"run_id": "TEST", "start": "2026-01-01", "end": "2027-01-01",
                        "url": (G4_ENDPOINT + "?lon0=135&lat0=20"
                                "&start=2026-01-01&end=2027-01-01&append=sst"),
                        "span_days": 366, "probe_rows": 366, "g4_samples": 1},
    "shadow_verification.json": {"segments": 1, "generation": 1,
                                 "wal_root": "/stub/p5_repairs", "anchor_root": "/stub/anchor",
                                 "anchor_domain_id": "independent",
                                 "measurement_taken": False},
}


def required_artifacts(profile: str) -> List[str]:
    if profile not in _BY_PROFILE:
        raise ArtifactError(f"unknown profile {profile!r}")
    return list(_BY_PROFILE[profile])


def _load_json(path: str, name: str):
    try:
        with open(path) as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise ArtifactError(f"{name} is not valid JSON: {e}") from None
    except OSError as e:
        raise ArtifactError(f"{name} could not be read: {e}") from None


#: Fields whose value must be a non-empty path-ish string.
_PATH_FIELDS = ("code_root", "python", "pythonpath", "filesystem_device", "filesystem_fstype")
#: Fields whose value must be a raw non-negative int (bool excluded: `True` is not a count).
_COUNT_FIELDS = ("free_now_bytes", "inodes_free")
_NA = "not_applicable_with_reason"


def _is_na(value) -> bool:
    return isinstance(value, str) and value.startswith(_NA)


def _na_reason(value) -> str:
    return value[len(_NA):].lstrip(": ").strip() if _is_na(value) else ""


def check_preflight_values(data, profile: str, *, run_id=None) -> None:
    """Types and semantics, not merely presence.

    A preflight carrying `deploy_sha: "x"` and `free_now_bytes: -1` has every required key and
    describes nothing. Each rule below exists because the value it guards is one somebody would
    otherwise read as evidence."""
    if not isinstance(data, dict):
        raise ArtifactError("preflight.json must be an object")
    na_allowed = S8_PROFILES[profile]["not_applicable"]
    missing = [f for f in S8_PREFLIGHT_FIELDS if f not in data]
    if missing:
        raise ArtifactError(f"preflight.json is missing required field(s): {missing}")

    for field, value in data.items():
        if not _is_na(value):
            continue
        if field not in na_allowed:
            raise ArtifactError(
                f"preflight.json excuses {field!r} as not-applicable, but {profile!r} requires "
                f"it. Excusing a field is how a check that never ran stops being visible.")
        if not _na_reason(value):
            raise ArtifactError(
                f"preflight.json field {field!r} is not-applicable without a reason; "
                f"'no answer' and 'does not apply, because ...' are different states")
    for field in na_allowed:
        if not _is_na(data[field]):
            raise ArtifactError(
                f"preflight.json field {field!r} does not apply under {profile!r} and must say "
                f"so with a reason; got {data[field]!r}")

    if run_id is not None and data["run_id"] != run_id:
        raise ArtifactError(
            f"preflight.json belongs to run {data['run_id']!r}, not {run_id!r}. An artifact "
            f"carried over from an earlier run describes a different run in every figure.")

    sha = data["deploy_sha"]
    if not (isinstance(sha, str) and len(sha) == 40
            and all(c in "0123456789abcdef" for c in sha)):
        raise ArtifactError(
            f"preflight.json deploy_sha must be a full 40-char lowercase hex commit id; got "
            f"{sha!r}. An abbreviation or a movable ref does not pin anything.")

    for field in _PATH_FIELDS:
        value = data[field]
        if _is_na(value):
            continue
        if not (isinstance(value, str) and value.strip()):
            raise ArtifactError(f"preflight.json {field!r} must be a non-empty string; "
                                f"got {value!r}")
    for field in _COUNT_FIELDS:
        value = data[field]
        if _is_na(value):
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ArtifactError(
                f"preflight.json {field!r} must be a non-negative integer; got {value!r}")

    if data["label"] != LABEL:
        raise ArtifactError(f"preflight.json label must be {LABEL!r}; got {data['label']!r}. "
                            f"A local label on production evidence is a provenance error.")


#: What a `p5_shadow` verification record must carry. The profile takes no measurement, so
#: `measurement_taken` must be exactly `False` -- a `True` there is the profile widening its
#: own scope, and a truthy string or 0 is a type confusion pretending to answer.
_SHADOW_FIELDS = ("segments", "generation", "wal_root", "anchor_root", "anchor_domain_id",
                  "measurement_taken")


def check_shadow_verification(data) -> None:
    if not isinstance(data, dict):
        raise ArtifactError("shadow_verification.json must be an object")
    missing = [f for f in _SHADOW_FIELDS if f not in data]
    if missing:
        raise ArtifactError(f"shadow_verification.json is missing field(s): {missing}; "
                            f"a partial record is not a verification")
    for field in ("segments", "generation"):
        value = data[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ArtifactError(
                f"shadow_verification.json {field!r} must be a positive integer; got {value!r}")
    for field in ("wal_root", "anchor_root", "anchor_domain_id"):
        value = data[field]
        if not (isinstance(value, str) and value.strip()):
            raise ArtifactError(
                f"shadow_verification.json {field!r} must be a non-empty string; got {value!r}")
    if data["measurement_taken"] is not False:
        raise ArtifactError(
            f"shadow_verification.json measurement_taken must be exactly False -- p5_shadow is "
            f"verification only; got {data['measurement_taken']!r}")


def _check_preflight(data, profile: str, run_id=None) -> None:
    check_preflight_values(data, profile, run_id=run_id)


def _check_wal(data, profile: str) -> None:
    if not isinstance(data, dict):
        raise ArtifactError("wal_state.json must be an object")
    if "wal_repairs_pending" not in data:
        raise ArtifactError("wal_state.json omits wal_repairs_pending; absence is not zero")
    pending = data["wal_repairs_pending"]
    if profile == "legacy_baseline":
        if pending is not None:
            raise ArtifactError(
                f"wal_state.json reports {pending!r} pending repairs under legacy_baseline, "
                f"where no WAL exists; that is an assertion about a store that is not there")
        if not str(data.get("not_applicable_with_reason", "")).strip():
            raise ArtifactError(
                "wal_state.json is null under legacy_baseline without a reason; "
                "'no answer' and 'does not apply, because ...' are different states")
    else:
        if not isinstance(pending, int) or isinstance(pending, bool):
            raise ArtifactError(
                f"wal_state.json must carry an integer count under {profile!r}; got {pending!r}")


def _check_nonempty(path: str, name: str) -> int:
    """Size only. Used for the human-readable `df`/`findmnt` copies, which have no schema of
    their own -- never for a measurement series, where "not empty" once let the single line
    `lol` pass as thirty latency samples."""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise ArtifactError(f"{name} could not be stat'd: {e}") from None
    if size == 0:
        raise ArtifactError(f"{name} is empty; an empty file is not a measurement")
    return size


def _header(path: str, name: str, run_id) -> dict:
    """Parse the `# run_id=... k=v ...` first line every series carries."""
    with open(path) as fh:
        first = fh.readline()
    if not first.startswith("#"):
        raise ArtifactError(f"{name} has no run header; it cannot be bound to a run")
    fields = {}
    for token in first[1:].split():
        key, _, value = token.partition("=")
        if key:
            fields[key] = value
    if "run_id" not in fields:
        raise ArtifactError(f"{name} header declares no run_id")
    if run_id is not None and fields["run_id"] != run_id:
        raise ArtifactError(
            f"{name} belongs to run {fields['run_id']!r}, not {run_id!r}; a file left by an "
            f"earlier run is real evidence of a different run")
    return fields


def _body_lines(path: str):
    with open(path) as fh:
        return [ln.strip() for ln in fh.readlines()[1:] if ln.strip()]


#: An exact non-negative integer token: digits only. No sign, no decimal point, no exponent,
#: no underscores, no surrounding space, no `True`.
_INT_TOKEN = __import__("re").compile(r"\A[0-9]+\Z")


def _exact_nonneg_int(token, name: str, what: str) -> int:
    """A COUNT, parsed without coercion.

    `int(float("2.5"))` is `2`: a worker count of 2.5 becomes legal by being rounded, and the
    parser loses the ability to report that anything was wrong. Counts are digits or they are
    not counts."""
    if isinstance(token, bool) or not isinstance(token, str):
        raise ArtifactError(f"{name}: {what} must be an integer token; got {token!r}")
    if not _INT_TOKEN.match(token):
        raise ArtifactError(
            f"{name}: {what} is not an exact non-negative integer: {token!r}. Fractional, "
            f"signed, exponent and boolean-like forms are refused rather than coerced.")
    return int(token)


def _finite_nonneg(token: str, name: str, what: str) -> float:
    try:
        value = float(token)
    except (TypeError, ValueError):
        raise ArtifactError(f"{name}: {what} is not a number: {token!r}") from None
    if value != value or value in (float("inf"), float("-inf")):
        raise ArtifactError(f"{name}: {what} is not finite: {token!r}")
    if value < 0:
        raise ArtifactError(f"{name}: {what} is negative: {token!r}")
    return value


def check_g4_series(path: str, name: str, *, run_id=None, expected=None) -> dict:
    """Exact sample count, finite non-negative seconds."""
    head = _header(path, name, run_id)
    lines = _body_lines(path)
    if not lines:
        raise ArtifactError(f"{name} carries no samples")
    values = [_finite_nonneg(ln, name, f"sample {i}") for i, ln in enumerate(lines)]
    declared = head.get("samples")
    if declared is not None and str(len(values)) != declared:
        raise ArtifactError(
            f"{name} declares {declared} samples and carries {len(values)}")
    if expected is not None and len(values) != expected:
        raise ArtifactError(
            f"{name} holds {len(values)} samples; the validated request planned {expected}. "
            f"A truncated series is a shorter measurement, not the one claimed.")
    return {"samples": len(values)}


def check_h7_series(path: str, name: str, *, run_id=None, expected_pids=None) -> dict:
    """`ts npids fds rss` per line: numeric, non-negative, and a STABLE worker count."""
    head = _header(path, name, run_id)
    lines = _body_lines(path)
    if not lines:
        raise ArtifactError(f"{name} carries no samples")
    counts = set()
    for i, ln in enumerate(lines):
        parts = ln.split()
        if len(parts) != 4:
            raise ArtifactError(f"{name} line {i} has {len(parts)} columns, expected 4: {ln!r}")
        # Every column is a count -- timestamp, workers, descriptors, kilobytes -- so every one
        # is an exact integer. None of them has a meaningful fractional value.
        values = [_exact_nonneg_int(col, name, f"line {i} {what}")
                  for col, what in zip(parts, ("timestamp", "pid count", "fd count", "rss"))]
        npids = values[1]
        if npids < 1:
            raise ArtifactError(f"{name} line {i} reports {npids} workers")
        counts.add(npids)
    if len(counts) > 1:
        raise ArtifactError(
            f"{name} worker count changes across the series: {sorted(counts)}. A series "
            f"spanning two different sets of processes is not a series.")
    declared = head.get("samples")
    if declared is not None and str(len(lines)) != declared:
        raise ArtifactError(f"{name} declares {declared} samples and carries {len(lines)}")
    workers = counts.pop()

    if expected_pids is not None:
        header_pids = _parse_pid_list(head.get("pids"), name)
        if header_pids != set(expected_pids):
            raise ArtifactError(
                f"{name} header names workers {sorted(header_pids)} but the PM2 roster recorded "
                f"{sorted(expected_pids)}; the series describes a different set of processes")
        if workers != len(expected_pids):
            raise ArtifactError(
                f"{name} samples report {workers} workers while the roster names "
                f"{len(expected_pids)}")
    return {"samples": len(lines), "workers": workers}


def _parse_pid_list(token, name: str):
    """`[101,102]` -> `{101, 102}`. Refuses anything that is not exactly that."""
    if not isinstance(token, str) or not (token.startswith("[") and token.endswith("]")):
        raise ArtifactError(f"{name} header pid list is malformed: {token!r}")
    inner = token[1:-1].strip()
    if not inner:
        raise ArtifactError(f"{name} header names no workers")
    pids = {_exact_nonneg_int(part.strip(), name, "header pid") for part in inner.split(",")}
    if 0 in pids:
        raise ArtifactError(f"{name} header names pid 0")
    return pids


#: The modules each profile actually imports and executes. Fingerprinting only
#: `store.block_manifest` left the validator itself (`ops.p5s8_report`), the profile
#: declarations (`ops.p5_roadmap`) and the measured paths outside the identity evidence -- any
#: of them could change without changing a byte of what identity recorded.
IDENTITY_MODULES = {
    "common": ("ops.p5s8_report", "ops.p5_roadmap", "store.block_manifest"),
    "legacy_baseline": ("ops.quiescence",),
    "p5_shadow": ("store.repair_wal", "store.segmented_cube"),
}


def g4_url(start: str, end: str) -> str:
    """The canonical G4 request URL — the ONE place it is built.

    The probe calls this and `check_g4_request` validates against the same constants, so the
    runbook cannot carry a second endpoint that drifts from the validator. That drift is exactly
    what staging hit: a hardcoded 8080 in the probe beside a pin the deployment had moved past."""
    params = dict(G4_EXPECTED_PARAMS)
    params["start"], params["end"] = start, end
    query = "&".join(f"{k}={params[k]}" for k in G4_REQUIRED_PARAMS)
    return f"{G4_ENDPOINT}?{query}"


def identity_modules(profile: str):
    """The inventory for `profile`, common modules first."""
    if profile not in S8_PROFILES:
        raise ArtifactError(f"unknown profile {profile!r}")
    return tuple(IDENTITY_MODULES["common"]) + tuple(IDENTITY_MODULES[profile])


#: Fields every G4 response row must carry.
G4_ROW_FIELDS = ("date", "sst")


def check_g4_response(rows, *, start: str, end: str) -> dict:
    """The response body, row by row — the ONE validator both the probe and the sampler use.

    A length check accepted a list of 366 integers, or one repeating a single day 366 times,
    exactly as it accepted the real answer. What makes a 366-day response is the *day set*:
    every day from `start` through `end`, once each, with no extras."""
    import datetime as _dt

    if not isinstance(rows, list):
        raise ArtifactError(f"G4 response is not a list: {type(rows).__name__}")
    try:
        first = _dt.date.fromisoformat(start)
        last = _dt.date.fromisoformat(end)
    except ValueError as e:
        raise ArtifactError(f"G4 window is not canonical ISO: {e}") from None
    wanted = [first + _dt.timedelta(days=i) for i in range((last - first).days + 1)]
    if len(rows) != len(wanted):
        raise ArtifactError(f"G4 response has {len(rows)} rows, expected {len(wanted)}")

    seen = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            raise ArtifactError(
                f"G4 response row {i} is {type(row).__name__}, not an object. A list of the "
                f"right length is not a list of rows.")
        missing = [f for f in G4_ROW_FIELDS if f not in row]
        if missing:
            raise ArtifactError(f"G4 response row {i} is missing field(s): {missing}")
        value = row["date"]
        if not isinstance(value, str):
            raise ArtifactError(f"G4 response row {i} date is not a string: {value!r}")
        try:
            parsed = _dt.date.fromisoformat(value)
        except ValueError:
            raise ArtifactError(
                f"G4 response row {i} date is not canonical ISO: {value!r}") from None
        if parsed.isoformat() != value:
            raise ArtifactError(f"G4 response row {i} date is not canonical: {value!r}")
        seen.append(parsed)

    duplicates = sorted({d.isoformat() for d in seen if seen.count(d) > 1})
    if duplicates:
        raise ArtifactError(f"G4 response repeats day(s): {duplicates[:3]}")
    missing_days = sorted(d.isoformat() for d in set(wanted) - set(seen))
    if missing_days:
        raise ArtifactError(
            f"G4 response is missing day(s) it claims to cover: {missing_days[:3]}")
    extra = sorted(d.isoformat() for d in set(seen) - set(wanted))
    if extra:
        raise ArtifactError(f"G4 response carries day(s) outside the window: {extra[:3]}")
    return {"rows": len(rows), "start": start, "end": end}


def check_identity(data, *, run_id=None, profile=None) -> dict:
    """What the identity block actually verified.

    A 40-character SHA in the preflight proves the preflight holds a SHA. It says nothing about
    whether any block resolved that commit, or whether the modules the run imported came from
    `$CODE_ROOT`. This is the record that does, and the assembler cross-checks the preflight
    against it."""
    import hashlib

    if not isinstance(data, dict):
        raise ArtifactError("identity.json must be an object")
    if run_id is not None and data.get("run_id") != run_id:
        raise ArtifactError(f"identity.json belongs to run {data.get('run_id')!r}")
    sha = data.get("deploy_sha")
    if not (isinstance(sha, str) and len(sha) == 40
            and all(c in "0123456789abcdef" for c in sha)):
        raise ArtifactError(
            f"identity.json deploy_sha must be a full 40-char lowercase hex commit; got {sha!r}")
    for field in ("code_root", "python"):
        value = data.get(field)
        if not (isinstance(value, str) and value.strip()):
            raise ArtifactError(f"identity.json {field} must be a non-empty string; got {value!r}")
    head = data.get("head_sha")
    if head != sha:
        raise ArtifactError(
            f"identity.json head_sha={head!r} is not the deployed commit {sha!r}. "
            f"`git rev-parse --verify <sha>^{{commit}}` echoes any existing commit back, so a "
            f"valid ANCESTOR passes that check while the checkout is something else.")
    if data.get("worktree_clean") is not True:
        raise ArtifactError(
            "identity.json does not record a clean worktree; uncommitted or untracked changes "
            "mean the running code is not the commit being pinned")

    modules = data.get("modules")
    if not isinstance(modules, dict) or not modules:
        raise ArtifactError("identity.json records no imported modules")
    if profile is not None:
        expected = set(identity_modules(profile))
        if set(modules) != expected:
            missing = sorted(expected - set(modules))
            extra = sorted(set(modules) - expected)
            raise ArtifactError(
                f"identity.json module inventory does not match {profile!r}: "
                f"missing {missing}, unexpected {extra}. An unrecorded module can change the "
                f"validator or the measured path without changing identity evidence.")
    root = os.path.realpath(data["code_root"])
    for name, info in modules.items():
        if not isinstance(info, dict) or "path" not in info or "sha256" not in info:
            raise ArtifactError(f"identity.json module {name!r} has no path and fingerprint")
        path = os.path.realpath(info["path"])
        if not (path == root or path.startswith(root + os.sep)):
            raise ArtifactError(
                f"identity.json module {name!r} was imported from {path}, outside "
                f"CODE_ROOT {root}. A runbook that reads correctly and imports from elsewhere "
                f"is the failure the P4 rehearsal hit.")
        try:
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
        except OSError as e:
            raise ArtifactError(f"identity.json module {name!r} is unreadable: {e}") from None
        if digest != info["sha256"]:
            raise ArtifactError(
                f"identity.json module {name!r} fingerprint does not match the file on disk")
    return data


def check_pm2_roster(data, *, run_id=None):
    """The roster that names which processes the fd/RSS series describes.

    It was written and never read, so the series' own claim about *whose* descriptors it counted
    went unchecked. Returns the pid set, which `check_h7_series` cross-checks."""
    if not isinstance(data, dict):
        raise ArtifactError("pm2_jlist.json must be an object with run_id and roster")
    if run_id is not None and data.get("run_id") != run_id:
        raise ArtifactError(f"pm2_jlist.json belongs to run {data.get('run_id')!r}")
    roster = data.get("roster")
    if not isinstance(roster, list) or not roster:
        raise ArtifactError(f"pm2_jlist.json roster must be a non-empty list; got {roster!r}")
    pids = []
    for proc in roster:
        if not isinstance(proc, dict) or proc.get("name") != "ghrsst":
            continue
        status = (proc.get("pm2_env") or {}).get("status")
        if status != "online":
            raise ArtifactError(
                f"pm2_jlist.json records the ghrsst app as {status!r}; a series sampled while "
                f"the service was not online describes something else")
        pid = proc.get("pid")
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
            raise ArtifactError(f"pm2_jlist.json pid must be a positive integer; got {pid!r}")
        pids.append(pid)
    if not pids:
        raise ArtifactError("pm2_jlist.json names no ghrsst worker")
    if len(set(pids)) != len(pids):
        raise ArtifactError(f"pm2_jlist.json repeats a pid: {sorted(pids)}")
    return set(pids)


def check_g16_scan(path: str, name: str, *, run_id=None, expected_root=None) -> dict:
    """`root=... files=N dirs=M wall_s=X`, all present, finite and non-negative."""
    _header(path, name, run_id)
    lines = _body_lines(path)
    if len(lines) != 1:
        raise ArtifactError(f"{name} must carry exactly one result line; got {len(lines)}")
    fields = {}
    for token in lines[0].split():
        key, _, value = token.partition("=")
        if key:
            fields[key] = value
    for required in ("root", "files", "dirs", "wall_s"):
        if required not in fields:
            raise ArtifactError(f"{name} is missing {required!r}: {lines[0]!r}")
    if not fields["root"].strip():
        raise ArtifactError(f"{name} records no scan root")
    if expected_root is not None and os.path.realpath(fields["root"]) != expected_root:
        raise ArtifactError(
            f"{name} scanned {fields['root']!r} but the run's data root is {expected_root!r}. "
            f"A scan of a different tree is a valid measurement of the wrong thing.")
    files = _exact_nonneg_int(fields["files"], name, "files")   # counts, not measurements
    dirs = _exact_nonneg_int(fields["dirs"], name, "dirs")
    _finite_nonneg(fields["wall_s"], name, "wall_s")            # a duration; may be fractional
    return {"files": files, "dirs": dirs}


_G4_REQUEST_FIELDS = ("run_id", "start", "end", "url", "span_days", "probe_rows", "g4_samples")


def check_g4_request(data, *, run_id=None) -> dict:
    if not isinstance(data, dict):
        raise ArtifactError("g4_request.json must be an object")
    missing = [f for f in _G4_REQUEST_FIELDS if f not in data]
    if missing:
        raise ArtifactError(f"g4_request.json is missing field(s): {missing}")
    if run_id is not None and data["run_id"] != run_id:
        raise ArtifactError(f"g4_request.json belongs to run {data['run_id']!r}")
    for field in ("span_days", "probe_rows"):
        value = data[field]
        if isinstance(value, bool) or not isinstance(value, int) or value != 366:
            raise ArtifactError(
                f"g4_request.json {field} must be exactly 366; got {value!r}. Editing it "
                f"afterwards would let a shorter run be reported as a 366-day one.")
    planned = data["g4_samples"]
    if isinstance(planned, bool) or not isinstance(planned, int) or planned < 1:
        raise ArtifactError(f"g4_request.json g4_samples must be a positive integer; "
                            f"got {planned!r}")
    for field in ("start", "end", "url"):
        if not (isinstance(data[field], str) and data[field].strip()):
            raise ArtifactError(f"g4_request.json {field} must be a non-empty string")

    # Non-empty is not "describes the request that was made". A record could keep
    # span_days: 366 while its dates, endpoint, coordinates or `append` said something else --
    # and the sampler would then spend thirty production requests on it.
    import datetime as _dt
    from urllib.parse import parse_qs, urlparse

    dates = {}
    for field in ("start", "end"):
        value = data[field]
        try:
            parsed = _dt.date.fromisoformat(value)
        except ValueError:
            raise ArtifactError(
                f"g4_request.json {field} is not a canonical ISO date: {value!r}") from None
        if parsed.isoformat() != value:
            raise ArtifactError(f"g4_request.json {field} is not canonical: {value!r}")
        dates[field] = parsed
    span = (dates["end"] - dates["start"]).days + 1
    if span != data["span_days"]:
        raise ArtifactError(
            f"g4_request.json dates span {span} days but span_days says {data['span_days']}. "
            f"Editing one without the other is how a shorter run gets reported as 366.")

    url = urlparse(data["url"])
    endpoint = f"{url.scheme}://{url.netloc}{url.path}"
    if endpoint != G4_ENDPOINT:
        raise ArtifactError(
            f"g4_request.json endpoint is {endpoint!r}, expected {G4_ENDPOINT!r}. The G4 "
            f"baseline is the local service; another host or path is another measurement.")
    params = parse_qs(url.query)
    missing_params = [p for p in G4_REQUIRED_PARAMS if p not in params]
    if missing_params:
        raise ArtifactError(f"g4_request.json url is missing parameter(s): {missing_params}")
    for field in ("start", "end"):
        if params[field] != [data[field]]:
            raise ArtifactError(
                f"g4_request.json url asks for {field}={params[field]} while the record says "
                f"{data[field]!r}; the URL and the claim describe different windows")
    for field, expected in G4_EXPECTED_PARAMS.items():
        if params[field] != [expected]:
            raise ArtifactError(
                f"g4_request.json url has {field}={params[field]}, expected {[expected]}. "
                f"A different coordinate or variable is a different measurement.")
    return data


def _run_id_from(art_dir: str):
    """The run this directory belongs to, from its own name (`p5s8_<RUN_ID>`)."""
    base = os.path.basename(os.path.normpath(art_dir))
    return base[len("p5s8_"):] if base.startswith("p5s8_") else None


def assemble(art_dir: str, *, profile: str) -> dict:
    """Validate every required artifact and return the assembled report.

    Raises `ArtifactError` on the first problem. Nothing is written here — the caller writes
    only what this returns, so a refusal cannot leave a half-report behind."""
    names = required_artifacts(profile)
    missing = [n for n in names if not os.path.isfile(os.path.join(art_dir, n))]
    if missing:
        raise ArtifactError(f"the run did not produce required artifact(s): {missing}")

    run_id = _run_id_from(art_dir)
    marker = _load_json(os.path.join(art_dir, "run.json"), "run.json")
    if not isinstance(marker, dict) or marker.get("run_id") != run_id:
        raise ArtifactError(
            f"run.json declares run {marker.get('run_id')!r} but the artifact directory is "
            f"{run_id!r}; the run this evidence belongs to is not the one being assembled")

    identity = check_identity(
        _load_json(os.path.join(art_dir, "identity.json"), "identity.json"),
        run_id=run_id, profile=profile)

    roster_pids = None
    if "pm2_jlist.json" in names:
        roster_pids = check_pm2_roster(
            _load_json(os.path.join(art_dir, "pm2_jlist.json"), "pm2_jlist.json"),
            run_id=run_id)

    request = None
    if "g4_request.json" in names:
        request = check_g4_request(
            _load_json(os.path.join(art_dir, "g4_request.json"), "g4_request.json"),
            run_id=run_id)

    capacity = _load_json(os.path.join(art_dir, "capacity.json"), "capacity.json") \
        if "capacity.json" in names else {}
    expected_root = capacity.get("data_root")
    if expected_root is not None:
        expected_root = os.path.realpath(expected_root)

    #: Series parsers. `_check_nonempty` is kept only for the human-readable df/findmnt copies.
    parsers = {
        "g4_366d_seconds.txt": lambda p, n: check_g4_series(
            p, n, run_id=run_id, expected=(request or {}).get("g4_samples")),
        "h7_fd_rss_timeseries.txt": lambda p, n: check_h7_series(
            p, n, run_id=run_id, expected_pids=roster_pids),
        "g16_metadata_scan.txt": lambda p, n: check_g16_scan(
            p, n, run_id=run_id, expected_root=expected_root),
    }

    summary = {}
    for name in names:
        path = os.path.join(art_dir, name)
        if name.endswith(".json"):
            data = _load_json(path, name)
            if name == "preflight.json":
                _check_preflight(data, profile, run_id=run_id)
                # The preflight is cross-checked against what identity-preflight VERIFIED.
                # A valid-looking SHA nobody resolved, or a code root nothing imported from,
                # is refused here rather than believed.
                for field in ("deploy_sha", "code_root", "python"):
                    if data[field] != identity[field]:
                        raise ArtifactError(
                            f"preflight.json {field}={data[field]!r} disagrees with "
                            f"identity.json {identity[field]!r}; the preflight names something "
                            f"the identity check never verified")
                if expected_root is not None and data.get("data_root") not in (None, expected_root):
                    raise ArtifactError(
                        f"preflight.json data_root={data['data_root']!r} disagrees with the "
                        f"capacity record {expected_root!r}")
            elif name == "wal_state.json":
                _check_wal(data, profile)
            elif name == "shadow_verification.json":
                check_shadow_verification(data)
            if (isinstance(data, dict) and "run_id" in data
                    and data["run_id"] != run_id and name != "run.json"):
                raise ArtifactError(
                    f"{name} belongs to run {data['run_id']!r}, not {run_id!r}")
            summary[name] = {"kind": "json", "ok": True}
        elif name in parsers:
            summary[name] = {"kind": "series", **parsers[name](path, name)}
        else:
            summary[name] = {"kind": "text", "bytes": _check_nonempty(path, name)}

    return {
        "step": "P5-S8", "label": LABEL, "profile": profile,
        "artifacts": summary,
        "adjudicates": list(S8_PROFILES[profile]["may_adjudicate"]),
        "not_adjudicated": list(S8_PROFILES[profile]["may_not_adjudicate"]),
        "scope_note": ("read-only evidence collection. No gate is adjudicated by this report "
                       "alone; adjudication is a separate step with its own review."),
    }


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--art", required=True)
    ap.add_argument("--profile", required=True)
    args = ap.parse_args(argv)
    report = assemble(args.art, profile=args.profile)      # raises before anything is written
    out = os.path.join(args.art, "p5s8_readonly.json")
    with open(out, "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    print(f"wrote {out}")
    return 0


#: Filled here rather than at the literal, because it must pass the validators defined above.
def _example_identity(run_id="TEST", profile="legacy_baseline"):
    """A stub carrying the FULL inventory for `profile`, fingerprinted from the real files.

    A stub with one module would be rejected by the inventory check, and every test built on it
    would become a test of the stub."""
    import hashlib
    import importlib
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    modules = {}
    for name in identity_modules(profile):
        path = os.path.realpath(importlib.import_module(name).__file__)
        with open(path, "rb") as fh:
            modules[name] = {"path": path, "sha256": hashlib.sha256(fh.read()).hexdigest()}
    return {"run_id": run_id, "deploy_sha": "0" * 40, "head_sha": "0" * 40,
            "worktree_clean": True, "code_root": root, "python": "/stub/python",
            "modules": modules}


EXAMPLE_STUBS["preflight.json"] = _example_preflight()
EXAMPLE_STUBS["identity.json"] = _example_identity()
#: The preflight stub must AGREE with the identity stub, since the assembler cross-checks them.
EXAMPLE_STUBS["preflight.json"]["code_root"] = EXAMPLE_STUBS["identity.json"]["code_root"]
EXAMPLE_STUBS["preflight.json"]["python"] = EXAMPLE_STUBS["identity.json"]["python"]


if __name__ == "__main__":
    raise SystemExit(main())
