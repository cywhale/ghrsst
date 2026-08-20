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
_COMMON = ("preflight.json", "wal_state.json", "df_h.txt", "df_i.txt", "findmnt.txt",
           "g4_366d_seconds.txt", "pm2_jlist.json", "h7_fd_rss_timeseries.txt",
           "g16_metadata_scan.txt")

#: The `p5_shadow` profile is verification-only (see `S8_PROFILES`), so it produces the
#: provisioning record and the identity preflight, and none of the measurement series.
_BY_PROFILE: Dict[str, tuple] = {
    "legacy_baseline": _COMMON,
    "p5_shadow": ("preflight.json", "wal_state.json", "shadow_verification.json"),
}

#: Minimal well-formed contents, used by tests to build a complete run without re-encoding the
#: schema in the test file. Keeping them here means the contract has exactly one definition.
EXAMPLE_STUBS = {
    "preflight.json": {f: ("not_applicable_with_reason: legacy profile"
                           if f in S8_PROFILES["legacy_baseline"]["not_applicable"] else "x")
                       for f in S8_PREFLIGHT_FIELDS},
    "wal_state.json": {"wal_repairs_pending": None,
                       "not_applicable_with_reason": "legacy_baseline: no P5 WAL is deployed"},
    "pm2_jlist.json": [{"name": "ghrsst", "pid": 1, "pm2_env": {"status": "online"}}],
    "shadow_verification.json": {"segments": 1, "wal": True, "anchor_domain_id": "independent"},
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


def _check_preflight(data, profile: str) -> None:
    if not isinstance(data, dict):
        raise ArtifactError("preflight.json must be an object")
    na = S8_PROFILES[profile]["not_applicable"]
    missing = [f for f in S8_PREFLIGHT_FIELDS if f not in data]
    if missing:
        raise ArtifactError(f"preflight.json is missing required field(s): {missing}")
    for field in na:
        value = data[field]
        if not (isinstance(value, str) and "not_applicable_with_reason" in value):
            raise ArtifactError(
                f"preflight.json field {field!r} does not apply under {profile!r} and must say "
                f"so with a reason; got {value!r}. An absent or bare-null field reads as clean.")


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
                _check_preflight(data, profile)
            elif name == "wal_state.json":
                _check_wal(data, profile)
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


if __name__ == "__main__":
    raise SystemExit(main())
