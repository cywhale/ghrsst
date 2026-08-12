"""dev2026 — P5-S5: source-BYTE provenance (design spec §5, §7.5a E2, §7.8a).

**The distinction this module exists to make.**

P5-S3 round 4 gave every block a `source_map`: per day, `source_kind` / `source_path` /
`source_day_index`. P5-S4 bound that map to the segment, to the block on disk, and to the live
manifest. All of it is **locator** provenance — *which store, which physical slot*. None of it
says anything about *which bytes came out of that slot*. A source modified in place after the
build leaves every locator check passing.

> **A locator digest must never, on its own, authorize a prune.** It answers "where did we read
> from", and the question a prune asks is "does base carry the correct value".

So this module adds the other half: a **byte fingerprint** of what the builder actually read,
recorded in a checksummed artifact the builder emits alongside the block, and re-verifiable
against the block at publication and against the source whenever the source is still there.

**Why a seeded sample and not a full digest.** A full-slab digest of a 90-day block is ~141 GB
of reads and the P4-S6 OOM lesson says not to materialize slabs at all. §7.5a E2 already
specifies the shape: *seeded, point-wise, float32-semantic, NaN-aware*, plus `var_valid`
equality per variable. The sample is deterministic in `(seed, ny, nx, day_index)`, so the same
points are compared on every side of every check.

The sample is drawn from **one tile-sized window per (day, var)**, not scattered across the
grid. Scattered points look stronger and are much worse in practice: on a sharded store each
point pulls a whole shard, so N scattered points cost N shard reads, while a window costs one.
Bounded I/O is what lets this run at publication instead of being skipped.

**What a sample can and cannot prove.** It is probabilistic: a corruption confined to unsampled
cells is not detected. That is why §7.5a makes **E1 (repair identity)** the deterministic
authorization and E2 "defence in depth, never sufficient alone". This module implements E2's
primitive and the artifact E1 needs; it does not upgrade E2 into a proof.
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Dict, List, Optional, Sequence

import numpy as np

ARTIFACT_NAME = "p5_block_provenance.json"
ARTIFACT_FORMAT = "ghrsst.p5.block_provenance"
ARTIFACT_VERSION = 1

#: Points sampled per (day, var). Small on purpose: the window read dominates the cost, and
#: more points inside the same window buy very little extra detection.
SAMPLE_POINTS = 64
#: Edge of the square sample window, in cells. Matches the builder's default tile so the window
#: is one tile read.
SAMPLE_WINDOW = 256
#: The sampling seed is **policy, not payload**. It was a `build_block` parameter recorded in
#: the artifact and read back at verification -- which let anyone able to edit the artifact and
#: recompute its checksum choose the seed, and therefore choose which cells get compared. A
#: verifier that takes its own strictness from the document it is verifying has no strictness.
#: Rotating the sample means changing this constant on both sides, which is a code change and
#: is reviewable; it is not something an artifact can ask for.
SAMPLE_SEED = 20260805


class ProvenanceError(Exception):
    """The provenance artifact is missing, malformed, or disagrees with what it describes."""


def _canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


# --------------------------------------------------------------------------- sampling
def sample_window(ny: int, nx: int, *, seed: int, day_index: int) -> tuple:
    """The `(i0, i1, j0, j1)` window sampled for one day. Deterministic, never empty.

    Varying with `day_index` matters: a fixed window would leave the same region of every day
    unexamined for the life of the store, so a corruption that avoids one rectangle avoids the
    check entirely."""
    h = min(SAMPLE_WINDOW, ny)
    w = min(SAMPLE_WINDOW, nx)
    rng = np.random.default_rng([int(seed), int(day_index), int(ny), int(nx)])
    i0 = int(rng.integers(0, max(1, ny - h + 1)))
    j0 = int(rng.integers(0, max(1, nx - w + 1)))
    return i0, i0 + h, j0, j0 + w


def sample_offsets(height: int, width: int, *, seed: int, day_index: int) -> List[tuple]:
    """Point offsets WITHIN the window. Point-wise by construction -- the values compared are
    individual cells, never a whole slab."""
    rng = np.random.default_rng([int(seed), int(day_index), int(height), int(width), 7])
    n = min(SAMPLE_POINTS, height * width)
    flat = rng.choice(height * width, size=n, replace=False)
    return [(int(f // width), int(f % width)) for f in sorted(flat)]


def _cell(value) -> str:
    """One cell, in float32 semantics, NaN-aware.

    `float32(x)` first: comparing a float64 promotion of a float32 store against a float64 read
    of another would differ in the low bits and report a mismatch that is not one. NaN gets an
    explicit token because `nan != nan` -- without it every land cell would look like a
    difference, and with a naive `==` every land cell would look like a match."""
    v = np.float32(value)
    if np.isnan(v):
        return "nan"
    return format(float(v), ".9g")


def window_fingerprint(tiles: Dict[str, Optional[np.ndarray]], *, seed: int, day_index: int,
                       var_valid: Dict[str, bool]) -> str:
    """Digest of the sampled cells of one day, across variables, plus `var_valid`.

    `tiles` maps variable -> the sampled window as an array, or `None` when the variable is
    absent on that day. Absence is recorded as a distinct token rather than as NaN: `absent`
    and `present-but-NaN` are different states (P1 omit semantics vs land), and collapsing them
    is exactly the gap↔present confusion §7.5a E2 exists to catch.
    """
    doc: Dict[str, object] = {"var_valid": {k: bool(v) for k, v in sorted(var_valid.items())}}
    for var in sorted(tiles):
        arr = tiles[var]
        if arr is None:
            doc[var] = "absent"
            continue
        a = np.asarray(arr)
        offs = sample_offsets(a.shape[0], a.shape[1], seed=seed, day_index=day_index)
        doc[var] = [_cell(a[i, j]) for i, j in offs]
    return hashlib.sha256(_canonical(doc).encode()).hexdigest()


def compare_days(a_tiles, b_tiles, *, seed: int, day_index: int,
                 a_valid: Dict[str, bool], b_valid: Dict[str, bool]) -> List[str]:
    """§7.5a E2, stated as differences rather than as a digest.

    A digest answers "same or not"; an operator staring at a refused prune needs "which variable
    and which cell". Returns a list of human-readable differences, empty when they agree."""
    diffs: List[str] = []
    for var in sorted(set(a_valid) | set(b_valid)):
        if bool(a_valid.get(var)) != bool(b_valid.get(var)):
            diffs.append(f"{var}: var_valid {a_valid.get(var)!r} vs {b_valid.get(var)!r}")
    for var in sorted(set(a_tiles) | set(b_tiles)):
        x, y = a_tiles.get(var), b_tiles.get(var)
        if (x is None) != (y is None):
            diffs.append(f"{var}: present on one side only "
                         f"({'absent' if x is None else 'present'} vs "
                         f"{'absent' if y is None else 'present'})")
            continue
        if x is None:
            continue
        xa, ya = np.asarray(x), np.asarray(y)
        if xa.shape != ya.shape:
            diffs.append(f"{var}: sample window shape {xa.shape} vs {ya.shape}")
            continue
        for i, j in sample_offsets(xa.shape[0], xa.shape[1], seed=seed, day_index=day_index):
            lhs, rhs = _cell(xa[i, j]), _cell(ya[i, j])
            if lhs != rhs:
                diffs.append(f"{var}: cell ({i},{j}) {lhs} vs {rhs}")
                if len(diffs) >= 8:              # enough to diagnose; not a data dump
                    return diffs
    return diffs


# --------------------------------------------------------------------------- the artifact
def artifact_checksum(doc: dict) -> str:
    base = dict(doc)
    base["artifact_checksum"] = ""
    return hashlib.sha256(_canonical(base).encode()).hexdigest()


_REQUIRED = ("format", "version", "segment_id", "block_path", "grid", "sample",
             "days", "artifact_checksum")
_DAY_REQUIRED = ("source_kind", "source_path", "source_day_index", "day_index",
                 "source_fingerprint", "var_valid")
_GRID_REQUIRED = ("ny", "nx")
_SAMPLE_REQUIRED = ("algo", "seed", "points", "window")


def _exact_int(value, what: str) -> int:
    """A raw `int`, never something that converts to one. `int("64")` and `int(True)` both
    succeed, and a coerced value is not the value that was written."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProvenanceError(f"{what} must be an int, got {type(value).__name__} {value!r}")
    return value


def _assert_sample_policy(doc: dict, path: str) -> None:
    """The sample parameters are POLICY and must match this module, exactly.

    The verifier previously took `ny`, `nx` and `seed` from the artifact. An artifact declaring
    a 4x4 grid, or a different seed, would then be checked against a sample of its own
    choosing -- a weaker one, or one aimed away from whatever was altered. Strictness taken
    from the artifact is not strictness."""
    grid, sample = doc["grid"], doc["sample"]
    for name, obj, required in (("grid", grid, _GRID_REQUIRED),
                                ("sample", sample, _SAMPLE_REQUIRED)):
        if not isinstance(obj, dict):
            raise ProvenanceError(f"{path}: `{name}` must be an object, got "
                                  f"{type(obj).__name__}")
        missing = [k for k in required if k not in obj]
        extra = [k for k in obj if k not in required]
        if missing or extra:
            raise ProvenanceError(f"{path}: `{name}` has the wrong field set "
                                  f"(missing={missing}, unexpected={extra})")
    for axis in _GRID_REQUIRED:
        if _exact_int(grid[axis], f"{path}: grid[{axis!r}]") <= 0:
            raise ProvenanceError(f"{path}: grid[{axis!r}] must be positive, got {grid[axis]}")
    if sample["algo"] != "sha256":
        raise ProvenanceError(f"{path}: sample algo {sample['algo']!r}, expected 'sha256'")
    for field, policy in (("seed", SAMPLE_SEED), ("points", SAMPLE_POINTS),
                          ("window", SAMPLE_WINDOW)):
        got = _exact_int(sample[field], f"{path}: sample[{field!r}]")
        if got != policy:
            raise ProvenanceError(
                f"{path}: sample[{field!r}] is {got}, but policy is {policy}. The sampling "
                f"parameters are fixed by `source_provenance`, not chosen by the artifact -- "
                f"an artifact that picks its own sample picks how weakly it is checked.")


def build_artifact(*, segment_id: str, block_path: str, ny: int, nx: int,
                   days: Dict[str, dict], seed: int = SAMPLE_SEED) -> dict:
    doc = {
        "format": ARTIFACT_FORMAT, "version": ARTIFACT_VERSION,
        "segment_id": segment_id, "block_path": os.path.basename(block_path.rstrip("/")),
        "grid": {"ny": int(ny), "nx": int(nx)},
        "sample": {"algo": "sha256", "seed": int(seed), "points": SAMPLE_POINTS,
                   "window": SAMPLE_WINDOW},
        "days": days, "artifact_checksum": "",
    }
    doc["artifact_checksum"] = artifact_checksum(doc)
    return doc


def write_artifact(path: str, doc: dict) -> str:
    with open(path, "w") as fh:
        fh.write(_canonical(doc))
        fh.flush()
        os.fsync(fh.fileno())
    d = os.path.dirname(os.path.abspath(path)) or "."
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return path


def load_artifact(path: str) -> dict:
    """Read and validate structurally. Every failure is a refusal, never a repair."""
    if not os.path.isfile(path):
        raise ProvenanceError(
            f"{path}: no provenance artifact. A block published without one carries only "
            f"locator provenance, which cannot show that the bytes it holds are the bytes its "
            f"sources held (§7.5a).")
    try:
        with open(path) as fh:
            doc = json.load(fh)
    except ValueError as exc:
        raise ProvenanceError(f"{path}: not valid JSON ({exc})") from exc
    if not isinstance(doc, dict):
        raise ProvenanceError(f"{path}: not a JSON object")
    missing = [k for k in _REQUIRED if k not in doc]
    extra = [k for k in doc if k not in _REQUIRED]
    if missing or extra:
        raise ProvenanceError(f"{path}: wrong field set (missing={missing}, "
                              f"unexpected={extra})")
    if doc["format"] != ARTIFACT_FORMAT or int(doc["version"]) != ARTIFACT_VERSION:
        raise ProvenanceError(f"{path}: format/version is "
                              f"{doc['format']!r}/{doc['version']!r}, expected "
                              f"{ARTIFACT_FORMAT!r}/{ARTIFACT_VERSION}")
    want = artifact_checksum(doc)
    if doc["artifact_checksum"] != want:
        raise ProvenanceError(
            f"{path}: artifact_checksum does not match its contents. The artifact is the only "
            f"record of what the build read, so a tampered or truncated one is refused rather "
            f"than partially believed.")
    _assert_sample_policy(doc, path)
    if not isinstance(doc["days"], dict) or not doc["days"]:
        raise ProvenanceError(f"{path}: `days` must be a non-empty object")
    for day, rec in sorted(doc["days"].items()):
        if not isinstance(rec, dict):
            raise ProvenanceError(f"{path}: day {day} is not an object")
        dmissing = [k for k in _DAY_REQUIRED if k not in rec]
        dextra = [k for k in rec if k not in _DAY_REQUIRED]
        if dmissing or dextra:
            raise ProvenanceError(
                f"{path}: day {day} has the wrong field set (missing={dmissing}, "
                f"unexpected={dextra}); an incomplete record cannot be verified, and a record "
                f"that cannot be verified is refused")
        if not isinstance(rec["source_fingerprint"], str) or not rec["source_fingerprint"]:
            raise ProvenanceError(f"{path}: day {day} has no source_fingerprint")
        if not isinstance(rec["var_valid"], dict) or not rec["var_valid"]:
            raise ProvenanceError(f"{path}: day {day} var_valid must be a non-empty object")
        for var, flag in sorted(rec["var_valid"].items()):
            if not isinstance(var, str) or not var:
                raise ProvenanceError(f"{path}: day {day} var_valid key {var!r} is not a "
                                      f"variable name")
            if not isinstance(flag, bool):
                # Truthiness would make `1`, `"false"` and `[]` all mean something, and
                # `var_valid` decides whether a variable is even read from the source.
                raise ProvenanceError(
                    f"{path}: day {day} var_valid[{var!r}] is {type(flag).__name__} "
                    f"{flag!r}, must be a raw bool")
        if _exact_int(rec["day_index"], f"{path}: day {day} day_index") < 0:
            raise ProvenanceError(f"{path}: day {day} day_index must be non-negative")
    return doc
