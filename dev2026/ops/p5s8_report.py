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


class ArtifactError(Exception):
    """A run whose evidence is incomplete or unreadable. Never downgraded to a warning."""


#: Artifacts every profile must produce. `wal_state.json` is included for BOTH profiles: under
#: `legacy_baseline` it carries the not-applicable record, which is itself evidence.
_COMMON = ("preflight.json", "wal_state.json", "capacity.json", "df_h.txt", "df_i.txt",
           "findmnt.txt", "g4_366d_seconds.txt", "pm2_jlist.json",
           "h7_fd_rss_timeseries.txt", "g16_metadata_scan.txt")

#: The `p5_shadow` profile is verification-only (see `S8_PROFILES`), so it produces the
#: provisioning record and the identity preflight, and none of the measurement series.
_BY_PROFILE: Dict[str, tuple] = {
    "legacy_baseline": _COMMON,
    "p5_shadow": ("preflight.json", "wal_state.json", "capacity.json", "df_h.txt", "df_i.txt",
                  "findmnt.txt", "shadow_verification.json"),
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
    "pm2_jlist.json": [{"name": "ghrsst", "pid": 1, "pm2_env": {"status": "online"}}],
    "capacity.json": {"filesystem_device": "/dev/stub", "filesystem_fstype": "ext4",
                      "free_now_bytes": 1, "inodes_free": 1},
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
    try:
        size = os.path.getsize(path)
    except OSError as e:
        raise ArtifactError(f"{name} could not be stat'd: {e}") from None
    if size == 0:
        raise ArtifactError(f"{name} is empty; an empty series is not a measurement")
    return size


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

    summary = {}
    for name in names:
        path = os.path.join(art_dir, name)
        if name.endswith(".json"):
            data = _load_json(path, name)
            if name == "preflight.json":
                _check_preflight(data, profile, run_id=_run_id_from(art_dir))
            elif name == "wal_state.json":
                _check_wal(data, profile)
            elif name == "shadow_verification.json":
                check_shadow_verification(data)
            summary[name] = {"kind": "json", "ok": True}
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
EXAMPLE_STUBS["preflight.json"] = _example_preflight()


if __name__ == "__main__":
    raise SystemExit(main())
