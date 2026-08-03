"""dev2026 — P5-S1: base-block manifest schema, validation, atomic publish and rollback.

The manifest is the **commit point** of the segmented base
(`specs/p5_segmented_timecube_compaction_design.md` §5). Everything here is deliberately
fail-closed: a manifest that cannot be fully validated is never used to build a serving
snapshot, because the alternative — serving a partial or mismatched view — is the class of
silent-wrong-data failure P4-S8a already cost us once.

Three rules this module exists to enforce:

* **§4.0 the manifest describes the BASE only.** No delta path, cutoff or precedence. Two
  authorities over the same days would let a published generation disagree with the live
  delta that the cron mutates twice daily. Any delta-ish key is a schema violation.
* **§5.2/§5.3 blocks are FIXED calendar windows** with a three-way day classification
  (`present` / `confirmed_missing` / `unknown`). A gap never shifts a boundary, and an
  unsealed block is representable because `unknown` exists.
* **§9.3 generation archives are immutable.** Rollback COPIES an archive onto the live
  pointer; it never `os.replace`s the archive itself, which would rename it away and destroy
  the record a second rollback or an audit needs.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import date, timedelta
from typing import Iterable, List, NamedTuple, Optional, Sequence

MANIFEST_FORMAT = "ghrsst.timecube.manifest"
SCHEMA_VERSION = 1
LIVE_NAME = "manifest.json"

_TOP_REQUIRED = ("format", "version", "generation", "generation_id", "created_utc",
                 "created_by", "predecessor_generation", "predecessor_manifest",
                 "grid", "variables", "block_grid", "segments", "superseded",
                 "manifest_checksum")
_TOP_OPTIONAL = ()
_SEG_REQUIRED = ("segment_id", "kind", "path", "immutable", "boundary_kind",
                 "start_day", "end_day", "materialized_through", "day_count", "gaps",
                 "unknown", "day_list", "layout", "variables", "fingerprint",
                 "precedence", "sealed", "supersedes")
_SEG_OPTIONAL = ("build_provenance", "var_valid_mode")
_SUP_REQUIRED = ("segment_id", "path", "superseded_at_generation", "release_after_utc",
                 "hold_until_utc", "status", "current_path", "bytes", "fingerprint")
_SUP_STATUS = ("referenced", "releasable", "held")
# §4.0 guard: any of these (or any key containing "delta") means the manifest is claiming
# authority it must not have.
_FORBIDDEN_SUBSTR = "delta"


class ManifestError(Exception):
    """Raised for any schema, consistency or integrity failure. Always fail-closed."""


# --------------------------------------------------------------------------- calendar
def next_day(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


def add_days(day: str, n: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=n)).isoformat()


def calendar_span(start: str, end: str) -> List[str]:
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    if d1 < d0:
        return []
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def block_bounds(anchor_day: str, block_days: int, k: int) -> tuple:
    """Block `k` = `[anchor + k*S, anchor + (k+1)*S - 1]`, inclusive, in CALENDAR days.

    Pure arithmetic, fully determined before any data is read (§5.2), so a missing day can
    never shift this block's boundary nor any later block's."""
    if block_days <= 0:
        raise ManifestError(f"block_days must be positive, got {block_days}")
    start = add_days(anchor_day, k * block_days)
    return start, add_days(start, block_days - 1)


def block_index_for(anchor_day: str, block_days: int, day: str) -> int:
    delta = (date.fromisoformat(day) - date.fromisoformat(anchor_day)).days
    if delta < 0:
        raise ManifestError(f"day {day} precedes the block-grid anchor {anchor_day}")
    return delta // block_days


# --------------------------------------------------------------------------- digests
def canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False)


def day_digest(days: Iterable[str]) -> str:
    return hashlib.sha256(canonical_json(sorted(days)).encode()).hexdigest()


def compute_checksum(manifest: dict) -> str:
    """sha256 over the canonical document with `manifest_checksum` blanked."""
    stripped = dict(manifest)
    stripped["manifest_checksum"] = ""
    return hashlib.sha256(canonical_json(stripped).encode()).hexdigest()


# --------------------------------------------------------------------------- validation
def _require_keys(obj: dict, required: Sequence[str], optional: Sequence[str], what: str):
    missing = [k for k in required if k not in obj]
    if missing:
        raise ManifestError(f"{what}: missing required field(s) {missing}")
    extra = [k for k in obj if k not in required and k not in optional]
    if extra:
        raise ManifestError(f"{what}: unknown field(s) {extra}")


def _reject_delta_authority(manifest: dict):
    """§4.0. Checked on the top level and on every segment."""
    for key in manifest:
        if _FORBIDDEN_SUBSTR in key.lower():
            raise ManifestError(
                f"manifest declares '{key}': the manifest describes the BASE only and must "
                f"carry no delta path, cutoff or precedence (§4.0). Delta stays with "
                f"TieredCube.")


def _validate_segment(seg: dict, grid: dict, idx: int):
    what = f"segment[{idx}] {seg.get('segment_id')!r}"
    _require_keys(seg, _SEG_REQUIRED, _SEG_OPTIONAL, what)
    for key in seg:
        if _FORBIDDEN_SUBSTR in key.lower():
            raise ManifestError(f"{what}: declares '{key}' (§4.0 forbids delta authority)")
    if seg["kind"] not in ("legacy_base", "block"):
        raise ManifestError(f"{what}: kind must be legacy_base|block, got {seg['kind']!r}")
    if seg["boundary_kind"] not in ("calendar", "legacy"):
        raise ManifestError(f"{what}: boundary_kind must be calendar|legacy")
    if not seg.get("immutable", False):
        raise ManifestError(f"{what}: segments must be immutable")

    span = calendar_span(seg["start_day"], seg["end_day"])
    if not span:
        raise ManifestError(f"{what}: end_day precedes start_day")
    gaps, unknown = list(seg["gaps"]), list(seg["unknown"])
    if len(set(gaps)) != len(gaps) or len(set(unknown)) != len(unknown):
        raise ManifestError(f"{what}: duplicate entries in gaps/unknown")
    overlap = set(gaps) & set(unknown)
    if overlap:
        raise ManifestError(
            f"{what}: {sorted(overlap)} classified BOTH confirmed_missing and unknown; the "
            f"three classes must partition the window (§5.3)")
    stray = (set(gaps) | set(unknown)) - set(span)
    if stray:
        raise ManifestError(f"{what}: {sorted(stray)} lie outside [start_day, end_day]")

    n = int(seg["day_count"])
    if n < 0:
        raise ManifestError(f"{what}: day_count must be >= 0")
    if seg["boundary_kind"] == "calendar":
        # §5.3 partition invariant, on the FIXED window
        if n + len(gaps) + len(unknown) != len(span):
            raise ManifestError(
                f"{what}: present({n}) + confirmed_missing({len(gaps)}) + "
                f"unknown({len(unknown)}) = {n + len(gaps) + len(unknown)}, expected "
                f"{len(span)} (the fixed calendar window). §5.3 requires a partition.")
    else:
        if n + len(gaps) + len(unknown) > len(span):
            raise ManifestError(f"{what}: classified days exceed the declared span")

    # §5.4 seal condition
    if seg["sealed"] and unknown:
        raise ManifestError(
            f"{what}: sealed=true with {len(unknown)} unknown day(s). A sealed block must "
            f"have unknown == [] (§5.4); this is a schema violation, not a warning.")
    if unknown:
        latest_known = [d for d in span if d not in set(unknown)]
        if latest_known and seg["materialized_through"] > max(latest_known):
            raise ManifestError(
                f"{what}: materialized_through {seg['materialized_through']} is past the "
                f"last classified day {max(latest_known)}")
        for d in span:
            if d > seg["materialized_through"] and d not in set(unknown):
                raise ManifestError(
                    f"{what}: {d} is past materialized_through but not classified unknown")

    # day_list, when present, is the AUTHORITATIVE present set -- so it must be pinned to the
    # declared window. Taking it verbatim let a segment declare a 2026 window and serve 2027
    # days, splitting the block grid from real availability (review finding #1).
    dl = seg.get("day_list")
    if dl:
        dl = list(dl)
        if len(set(dl)) != len(dl):
            raise ManifestError(f"{what}: day_list contains duplicate days")
        outside = sorted(set(dl) - set(span))
        if outside:
            raise ManifestError(
                f"{what}: day_list contains {len(outside)} day(s) outside "
                f"[{seg['start_day']}, {seg['end_day']}], first {outside[0]}. day_list may "
                f"never escape the declared calendar window (§5.2/G14).")
        expected = set(span) - set(gaps) - set(unknown)
        if set(dl) != expected:
            raise ManifestError(
                f"{what}: day_list must equal span - gaps - unknown "
                f"({len(expected)} day(s)), got {len(set(dl))}")
        if len(dl) != n:
            raise ManifestError(f"{what}: len(day_list)={len(dl)} != day_count={n}")
        late = [d for d in dl if d > seg["materialized_through"]]
        if late:
            raise ManifestError(
                f"{what}: day_list has present day(s) past materialized_through "
                f"{seg['materialized_through']}, first {late[0]}")

    if not isinstance(seg["precedence"], int):
        raise ManifestError(f"{what}: precedence must be an int")
    fp = seg["fingerprint"]
    if not isinstance(fp, dict) or "algo" not in fp or "day_digest" not in fp:
        raise ManifestError(f"{what}: fingerprint must carry algo + day_digest")
    mode = seg.get("var_valid_mode", "explicit")
    if mode not in ("explicit", "implicit_all_true"):
        raise ManifestError(
            f"{what}: var_valid_mode must be explicit|implicit_all_true, got {mode!r}")
    if mode == "implicit_all_true" and seg["kind"] != "legacy_base":
        raise ManifestError(
            f"{what}: var_valid_mode='implicit_all_true' is allowed only on a legacy_base "
            f"segment; a block is built by us and must carry an explicit var_valid")

    if not fp.get("metadata"):
        raise ManifestError(
            f"{what}: fingerprint.metadata is required -- it is what binds the manifest to "
            f"the segment's actual grid, axes and chunk layout (§5)")


def _validate_superseded(entry: dict, idx: int, generation: int):
    what = f"superseded[{idx}] {entry.get('segment_id')!r}"
    _require_keys(entry, _SUP_REQUIRED, (), what)
    if entry["status"] not in _SUP_STATUS:
        raise ManifestError(f"{what}: status must be one of {_SUP_STATUS}")
    if int(entry["superseded_at_generation"]) > generation:
        raise ManifestError(f"{what}: superseded_at_generation is in the future")


def validate_manifest(manifest: dict) -> dict:
    """Pure schema + self-consistency validation. No disk access. Raises `ManifestError`."""
    if not isinstance(manifest, dict):
        raise ManifestError("manifest must be a JSON object")
    _require_keys(manifest, _TOP_REQUIRED, _TOP_OPTIONAL, "manifest")
    _reject_delta_authority(manifest)

    if manifest["format"] != MANIFEST_FORMAT:
        raise ManifestError(f"unknown format {manifest['format']!r}")
    if int(manifest["version"]) != SCHEMA_VERSION:
        raise ManifestError(f"unsupported schema version {manifest['version']}")
    gen = int(manifest["generation"])
    if gen < 1:
        raise ManifestError("generation must be >= 1")
    pred = manifest["predecessor_generation"]
    if gen == 1 and pred is not None:
        raise ManifestError("generation 1 must have predecessor_generation null")
    if gen > 1 and (pred is None or int(pred) >= gen):
        raise ManifestError("predecessor_generation must be set and < generation")

    grid = manifest["grid"]
    _require_keys(grid, ("ny", "nx"), ("region",), "grid")
    for k in ("ny", "nx"):
        if type(grid[k]) is not int or isinstance(grid[k], bool) or grid[k] <= 0:
            raise ManifestError(
                f"grid.{k}={grid[k]!r} must be a positive int (raw -- int(x) would launder a "
                f"string or a float, and the store side already refuses that)")
    if "region" in grid and grid["region"] is not None:
        reg = grid["region"]
        _exact(reg, list, "grid.region")
        if len(reg) != 4:
            raise ManifestError(
                f"grid.region must have exactly four entries [i0,i1,j0,j1], got {len(reg)}")
        for i, x in enumerate(reg):
            if type(x) is not int or isinstance(x, bool):
                raise ManifestError(
                    f"grid.region[{i}]={x!r} is {type(x).__name__}, expected int")
        i0, i1, j0, j1 = reg
        if not (0 <= i0 < i1 <= grid["ny"]) or not (0 <= j0 < j1 <= grid["nx"]):
            raise ManifestError(
                f"grid.region={reg} is out of order or outside the grid "
                f"{grid['ny']}x{grid['nx']}")

    bgrid = manifest["block_grid"]
    _require_keys(bgrid, ("anchor_day", "block_days"), (), "block_grid")
    anchor, bdays = bgrid["anchor_day"], int(bgrid["block_days"])

    segs = manifest["segments"]
    if not isinstance(segs, list) or not segs:
        raise ManifestError("segments must be a non-empty list")
    seen_ids = set()
    prev_start = None
    for i, seg in enumerate(segs):
        _validate_segment(seg, manifest["grid"], i)
        if seg["segment_id"] in seen_ids:
            raise ManifestError(f"duplicate segment_id {seg['segment_id']!r}")
        seen_ids.add(seg["segment_id"])
        if prev_start is not None and seg["start_day"] == prev_start:
            raise ManifestError(
                f"duplicate start_day {seg['start_day']} in segments (§5.1 requires them to "
                f"be ascending and NON-DUPLICATING by start_day). A superseding version "
                f"replaces its predecessor in `segments` and the predecessor moves to "
                f"`superseded`; both must never be listed as live segments.")
        if prev_start is not None and seg["start_day"] < prev_start:
            raise ManifestError(
                "segments must be ordered ascending by start_day; order is normative and "
                "must never be inferred from filenames (§5.1)")
        prev_start = seg["start_day"]
        if seg["boundary_kind"] == "calendar":
            k = block_index_for(anchor, bdays, seg["start_day"])
            want = block_bounds(anchor, bdays, k)
            if (seg["start_day"], seg["end_day"]) != want:
                raise ManifestError(
                    f"segment {seg['segment_id']!r} bounds "
                    f"{(seg['start_day'], seg['end_day'])} do not match the block grid "
                    f"{want} (anchor {anchor}, block_days {bdays})")

    # top-level `variables` is the union across segments -- not a free-text wish list
    union = sorted({v for seg in segs for v in seg["variables"]})
    if sorted(manifest["variables"]) != union:
        raise ManifestError(
            f"top-level variables {sorted(manifest['variables'])} != the union of segment "
            f"variables {union}; it must be exactly that union")

    sup = manifest["superseded"]
    if not isinstance(sup, list):
        raise ManifestError("superseded must be a list")
    for i, entry in enumerate(sup):
        _validate_superseded(entry, i, gen)

    expected = compute_checksum(manifest)
    if manifest["manifest_checksum"] != expected:
        raise ManifestError(
            f"manifest_checksum mismatch: stored {manifest['manifest_checksum'][:12]}… "
            f"computed {expected[:12]}…")
    return manifest


def declared_present_days(seg: dict) -> List[str]:
    """The `present` set a segment declares.

    `day_list` and `span − gaps − unknown` are required to be EQUAL by `_validate_segment`,
    so either is safe here; the explicit list wins only as documentation of intent."""
    if seg.get("day_list"):
        return sorted(seg["day_list"])
    excluded = set(seg["gaps"]) | set(seg["unknown"])
    return [d for d in calendar_span(seg["start_day"], seg["end_day"]) if d not in excluded]


# --------------------------------------------------------------------------- store binding
def _array_layout(arr) -> dict:
    shards = getattr(arr, "shards", None)
    return {"time_chunk": int(arr.chunks[0]),
            "spatial_chunk": int(arr.chunks[-1]),
            "shard": [int(x) for x in shards] if shards else None}


def segment_layout(store_path: str) -> dict:
    """The layout a segment ACTUALLY has, in manifest form.

    Reading only the first variable was a real defect: a second variable with different
    chunking (or a different length) passed unnoticed and then raised `IndexError` on a read.
    A segment has ONE layout by definition, so disagreement between its arrays is itself an
    error rather than something to summarize."""
    import zarr
    g = zarr.open_group(store_path, mode="r")
    vars_ = sorted(v for v in g.attrs.get("vars", []) if v in g)
    if not vars_:
        raise ManifestError(f"{store_path}: no data variables")
    layouts = {v: _array_layout(g[v]) for v in vars_}
    first = layouts[vars_[0]]
    disagree = {v: l for v, l in layouts.items() if l != first}
    if disagree:
        raise ManifestError(
            f"{store_path}: inconsistent layout across variables — {vars_[0]}={first} vs "
            f"{ {v: l for v, l in disagree.items()} }. A segment must have one layout.")
    return first


DTYPE = "float32"

# Only `inspect_store_contract` sets this. `metadata_fingerprint_from_inspection` refuses
# anything else, which prevents ACCIDENTAL bypass (a hand-built look-alike, a stale dict from
# an older code path). It is a module-private convention, not a security boundary: a caller
# that reaches for `bm._INSPECTION_TOKEN` can forge one, and nothing here pretends otherwise.
_INSPECTION_TOKEN = object()


class StoreInspection(NamedTuple):
    """The ONE validated view of a segment store. Everything downstream consumes this rather
    than re-reading (and re-laundering) the store's attrs."""
    token: object
    path: str
    days: tuple
    vars: tuple
    var_valid: tuple            # ((var, (bool, ...)), ...) -- ordered, hashable, no dict
    var_valid_implicit: bool
    arrays: tuple               # ((var, shape, chunks, shards, dtype, fill_is_nan), ...)
    ny: int
    nx: int
    lon_digest: str
    lat_digest: str
    # A digest is computed over BYTES and therefore cannot distinguish (32,) from (1,32)
    # holding identical values -- and `.size` reports 32 either way. Shape and dtype must be
    # carried explicitly or a post-inspection reshape slips through every comparison.
    lon_shape: tuple
    lat_shape: tuple
    lon_dtype: str
    lat_dtype: str
    region: tuple


def _axis(g, name: str, store_path: str):
    """Validate a coordinate axis against what the READ PATH actually requires.

    `TimeCubeStore._nearest_idx` resolves a point with `np.searchsorted`, which is only
    correct on a 1-D, strictly INCREASING axis. A 2-D `lon` blew up inside the read, and a
    descending one silently returned the wrong grid cell (asking for lon 100 resolved to
    131). Relaxing this later means changing the index resolver first, not the validator."""
    import numpy as np
    if name not in g:
        raise ManifestError(f"{store_path}: coordinate array {name!r} is missing")
    raw = g[name]
    if raw.ndim != 1:
        raise ManifestError(
            f"{store_path}: {name!r} is {raw.ndim}-D, expected 1-D — the read path indexes "
            f"it as a 1-D axis")
    arr = np.asarray(raw[:], dtype="float64")
    if arr.size == 0:
        raise ManifestError(f"{store_path}: {name!r} is empty")
    if not np.all(np.isfinite(arr)):
        raise ManifestError(f"{store_path}: {name!r} contains non-finite values")
    if arr.size > 1 and not np.all(np.diff(arr) > 0):
        raise ManifestError(
            f"{store_path}: {name!r} is not strictly increasing. The read path uses "
            f"np.searchsorted, which requires an ascending axis; a descending one resolves "
            f"to the wrong cell without any error.")
    return arr, tuple(int(x) for x in raw.shape), str(raw.dtype)


def _exact(value, typ, what: str):
    if type(value) is not typ:
        raise ManifestError(
            f"{what}: expected {typ.__name__}, got {type(value).__name__}. The type is "
            f"checked BEFORE any conversion -- calling list()/dict() first would launder a "
            f"malformed value into a well-formed one.")
    return value


def inspect_store_contract(store_path: str, *, allow_implicit_var_valid: bool = False
                           ) -> StoreInspection:
    """Validate a segment store's RAW attrs and arrays, then return the single view every
    other function consumes.

    Nothing here converts before it checks. `list(attrs["days"])`, `dict(var_valid)` and
    friends all silently accept the wrong container -- a `var_valid` written as a JSON
    list-of-pairs was laundered into a dict, and duplicate keys inside it would have been
    resolved to the last one without a word.
    """
    import numpy as np
    import zarr
    g = zarr.open_group(store_path, mode="r")
    attrs = dict(g.attrs)                      # zarr's own mapping -> plain dict, values raw

    # ---- days
    if "days" not in attrs:
        raise ManifestError(f"{store_path}: attrs['days'] is missing")
    raw_days = _exact(attrs["days"], list, f"{store_path}: attrs['days']")
    for i, d in enumerate(raw_days):
        _exact(d, str, f"{store_path}: attrs['days'][{i}]")
        try:
            date.fromisoformat(d)
        except ValueError as exc:
            raise ManifestError(
                f"{store_path}: attrs['days'][{i}]={d!r} is not an ISO date") from exc
    if len(set(raw_days)) != len(raw_days):
        raise ManifestError(f"{store_path}: attrs['days'] contains duplicate days")

    # ---- vars
    if "vars" not in attrs:
        raise ManifestError(f"{store_path}: attrs['vars'] is missing")
    raw_vars = _exact(attrs["vars"], list, f"{store_path}: attrs['vars']")
    if not raw_vars:
        raise ManifestError(f"{store_path}: attrs['vars'] is empty")
    for i, v in enumerate(raw_vars):
        _exact(v, str, f"{store_path}: attrs['vars'][{i}]")
    if len(set(raw_vars)) != len(raw_vars):
        dupes = sorted({v for v in raw_vars if raw_vars.count(v) > 1})
        raise ManifestError(f"{store_path}: attrs['vars'] has duplicate entries {dupes}")

    arrays = []
    for v in sorted(raw_vars):
        if v not in g:
            raise ManifestError(
                f"{store_path}: attrs['vars'] declares {v!r} but the store has no such "
                f"array. Filtering it out would turn a malformed store into a valid one "
                f"while reads silently omit the variable.")
        arr = g[v]
        if arr.ndim != 3:
            raise ManifestError(
                f"{store_path}: variable {v!r} is {arr.ndim}-D, expected a 3-D (t,y,x) "
                f"data variable")
        if str(arr.dtype) != DTYPE:
            raise ManifestError(
                f"{store_path}: variable {v!r} has dtype {arr.dtype}, expected {DTYPE}")
        fill = arr.fill_value
        fill_is_nan = fill is not None and bool(np.isnan(np.asarray(fill, dtype="float64")))
        if not fill_is_nan:
            raise ManifestError(
                f"{store_path}: variable {v!r} has fill_value {fill!r}, expected NaN -- a "
                f"non-NaN fill makes a missing chunk read as data instead of null")
        if int(arr.shape[0]) != len(raw_days):
            raise ManifestError(
                f"{store_path}: variable {v!r} has {int(arr.shape[0])} time step(s) but "
                f"attrs['days'] declares {len(raw_days)}")
        shards = getattr(arr, "shards", None)
        arrays.append((v, tuple(int(x) for x in arr.shape),
                       tuple(int(x) for x in arr.chunks),
                       tuple(int(x) for x in shards) if shards else None,
                       str(arr.dtype), fill_is_nan))

    layouts = {a[0]: (a[2][0], a[2][-1], a[3]) for a in arrays}
    first = layouts[arrays[0][0]]
    disagree = {v: l for v, l in layouts.items() if l != first}
    if disagree:
        raise ManifestError(
            f"{store_path}: inconsistent layout across variables -- {arrays[0][0]}={first} "
            f"vs {disagree}. A segment must have one layout.")

    # ---- var_valid
    implicit = False
    if "var_valid" not in attrs or attrs["var_valid"] is None:
        if not allow_implicit_var_valid:
            raise ManifestError(
                f"{store_path}: attrs['var_valid'] is missing. It is required; a store "
                f"predating it is admissible only when the manifest declares "
                f"var_valid_mode='implicit_all_true' on a legacy_base segment.")
        implicit = True
        var_valid = tuple((v, tuple([True] * len(raw_days))) for v in sorted(raw_vars))
    else:
        raw_vv = _exact(attrs["var_valid"], dict, f"{store_path}: attrs['var_valid']")
        if set(raw_vv) != set(raw_vars):
            missing = sorted(set(raw_vars) - set(raw_vv))
            extra = sorted(set(raw_vv) - set(raw_vars))
            raise ManifestError(
                f"{store_path}: var_valid keys do not match attrs['vars'] -- "
                f"missing {missing}, unexpected {extra}")
        pairs = []
        for v in sorted(raw_vv):
            flags = _exact(raw_vv[v], list, f"{store_path}: var_valid[{v!r}]")
            bad = [(i, x) for i, x in enumerate(flags) if type(x) is not bool]
            if bad:
                i, x = bad[0]
                raise ManifestError(
                    f"{store_path}: var_valid[{v!r}][{i}] is {type(x).__name__} {x!r}, not a "
                    f"bool. Coercing it would hide the type error, and the read path tests "
                    f"`is False`, so a truthy string would not behave as the value implies.")
            if len(flags) != len(raw_days):
                raise ManifestError(
                    f"{store_path}: var_valid[{v!r}] has {len(flags)} flag(s) for "
                    f"{len(raw_days)} day(s)")
            pairs.append((v, tuple(flags)))
        var_valid = tuple(pairs)

    lon, lon_shape, lon_dtype = _axis(g, "lon", store_path)
    lat, lat_shape, lat_dtype = _axis(g, "lat", store_path)

    # ---- region: exactly four RAW ints, consistent with the grid
    region = attrs.get("region", None)
    if region is not None:
        _exact(region, list, f"{store_path}: attrs['region']")
        if len(region) != 4:
            raise ManifestError(
                f"{store_path}: attrs['region'] must have exactly four entries "
                f"[i0,i1,j0,j1], got {len(region)}")
        for i, x in enumerate(region):
            if type(x) is not int or isinstance(x, bool):
                raise ManifestError(
                    f"{store_path}: attrs['region'][{i}]={x!r} is "
                    f"{type(x).__name__}, expected int. int(x) would launder a string or a "
                    f"float into a bound.")
        i0, i1, j0, j1 = region
        ny_, nx_ = int(lat.size), int(lon.size)
        if not (0 <= i0 < i1 <= ny_) or not (0 <= j0 < j1 <= nx_):
            raise ManifestError(
                f"{store_path}: attrs['region']={region} is out of order or outside the "
                f"grid {ny_}x{nx_} (need 0 <= i0 < i1 <= ny and 0 <= j0 < j1 <= nx)")

    return StoreInspection(
        token=_INSPECTION_TOKEN, path=store_path,
        days=tuple(raw_days), vars=tuple(sorted(raw_vars)), var_valid=var_valid,
        var_valid_implicit=implicit, arrays=tuple(arrays),
        ny=int(lat.size), nx=int(lon.size),
        lon_digest=hashlib.sha256(lon.tobytes()).hexdigest(),
        lat_digest=hashlib.sha256(lat.tobytes()).hexdigest(),
        lon_shape=lon_shape, lat_shape=lat_shape,
        lon_dtype=lon_dtype, lat_dtype=lat_dtype,
        region=tuple(region or ()))


def effective_var_valid(insp: StoreInspection) -> dict:
    """`{var: [bool, ...]}` as the READ PATH will see it, with the legacy implicit case
    normalized to all-true so the two sides are comparable."""
    return {v: list(flags) for v, flags in insp.var_valid}


def reader_binding(store, insp: StoreInspection) -> dict:
    """The reader's OWN captured metadata, in the same shape as `inspection_binding`.

    Comparing only `days` and `vars` left a TOCTOU window: a `var_valid` flip landing between
    the inspection and the reader's capture was installed unvalidated, and it changes what the
    API returns for that day."""
    import hashlib as _h
    import numpy as np
    m = store._meta
    vv = {v: list(flags) for v, flags in dict(m.var_valid).items()}
    if insp.var_valid_implicit and not vv:
        vv = {v: [True] * len(m.days) for v in m.vars}
    arrays = {}
    for v in sorted(m.vars):
        arr = m.arr.get(v)
        if arr is None:
            continue
        shards = getattr(arr, "shards", None)
        fill = arr.fill_value
        arrays[v] = {"shape": [int(x) for x in arr.shape],
                     "chunks": [int(x) for x in arr.chunks],
                     "shards": [int(x) for x in shards] if shards else None,
                     "dtype": str(arr.dtype),
                     "fill_is_nan": bool(fill is not None and np.isnan(
                         np.asarray(fill, dtype="float64")))}
    return {
        "days": list(m.days), "vars": sorted(m.vars), "var_valid": vv, "arrays": arrays,
        "lon_digest": _h.sha256(np.asarray(m.lon, dtype="float64").tobytes()).hexdigest(),
        "lat_digest": _h.sha256(np.asarray(m.lat, dtype="float64").tobytes()).hexdigest(),
        "lon_shape": [int(x) for x in np.shape(m.lon)],
        "lat_shape": [int(x) for x in np.shape(m.lat)],
        "lon_dtype": str(np.asarray(m.lon).dtype), "lat_dtype": str(np.asarray(m.lat).dtype),
    }


def inspection_binding(inspection) -> dict:
    insp = _require_inspection(inspection)
    return {
        "days": list(insp.days), "vars": sorted(insp.vars),
        "var_valid": effective_var_valid(insp),
        "arrays": {v: {"shape": list(shape), "chunks": list(chunks),
                       "shards": list(shards) if shards else None,
                       "dtype": dtype, "fill_is_nan": fill_is_nan}
                   for (v, shape, chunks, shards, dtype, fill_is_nan) in insp.arrays},
        "lon_digest": insp.lon_digest, "lat_digest": insp.lat_digest,
        "lon_shape": list(insp.lon_shape), "lat_shape": list(insp.lat_shape),
        "lon_dtype": insp.lon_dtype, "lat_dtype": insp.lat_dtype,
    }


def _require_inspection(inspection) -> StoreInspection:
    if not isinstance(inspection, StoreInspection) or inspection.token is not _INSPECTION_TOKEN:
        raise ManifestError(
            "expected a StoreInspection produced by inspect_store_contract(); a hand-built "
            "look-alike would bypass the strict validation this exists to guarantee")
    return inspection


def segment_layout_from_inspection(inspection) -> dict:
    insp = _require_inspection(inspection)
    _, _, chunks, shards, _, _ = insp.arrays[0]
    return {"time_chunk": int(chunks[0]), "spatial_chunk": int(chunks[-1]),
            "shard": [int(x) for x in shards] if shards else None}


def metadata_fingerprint_from_inspection(inspection) -> str:
    """sha256 over a VALIDATED segment's structural and semantic metadata: axes, region,
    variables, per-array shape/chunks/shards/dtype/fill, and each `var_valid` vector's
    CONTENT (not merely its length -- one flipped flag changes what the API returns)."""
    insp = _require_inspection(inspection)
    doc = {
        "axes": {"ny": insp.ny, "nx": insp.nx,
                 "lon_digest": insp.lon_digest, "lat_digest": insp.lat_digest,
                 "lon_shape": list(insp.lon_shape), "lat_shape": list(insp.lat_shape),
                 "lon_dtype": insp.lon_dtype, "lat_dtype": insp.lat_dtype,
                 "region": list(insp.region)},
        "vars": list(insp.vars),
        "arrays": {v: {"shape": list(shape), "chunks": list(chunks),
                       "shards": list(shards) if shards else None,
                       "dtype": dtype, "fill_is_nan": fill_is_nan}
                   for (v, shape, chunks, shards, dtype, fill_is_nan) in insp.arrays},
        "var_valid_digest": {v: hashlib.sha256(
            canonical_json(list(flags)).encode()).hexdigest() for v, flags in insp.var_valid},
        "var_valid_len": {v: len(flags) for v, flags in insp.var_valid},
        "var_valid_implicit": insp.var_valid_implicit,
        # NOTE: the day set is deliberately NOT included. It is covered by
        # `fingerprint.day_digest` and the explicit day-set comparison at snapshot build;
        # duplicating it here would make this fingerprint fire first and rob those checks of
        # their isolating test.
    }
    return hashlib.sha256(canonical_json(doc).encode()).hexdigest()


def segment_layout(store_path: str, *, allow_implicit_var_valid: bool = False) -> dict:
    """The layout a segment ACTUALLY has -- via the strict inspection, never a direct read."""
    return segment_layout_from_inspection(
        inspect_store_contract(store_path,
                               allow_implicit_var_valid=allow_implicit_var_valid))


def metadata_fingerprint(store_path: str, *, allow_implicit_var_valid: bool = False) -> str:
    """Public entry point: inspect strictly, then fingerprint.

    A builder must not be able to mint a manifest for a store that violates the contract, so
    this refuses every malformed store rather than only the missing-array case. The legacy
    allowance is an explicit argument because a fingerprint has no segment context to infer
    it from."""
    return metadata_fingerprint_from_inspection(
        inspect_store_contract(store_path,
                               allow_implicit_var_valid=allow_implicit_var_valid))


def store_axes(store_path: str, *, allow_implicit_var_valid: bool = False) -> dict:
    insp = inspect_store_contract(store_path,
                                  allow_implicit_var_valid=allow_implicit_var_valid)
    return {"ny": insp.ny, "nx": insp.nx, "lon_digest": insp.lon_digest,
            "lat_digest": insp.lat_digest, "region": list(insp.region)}


# --------------------------------------------------------------------------- publish/rollback
def _fsync_dir(dirpath: str):
    fd = os.open(dirpath, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_fsync(path: str, text: str):
    with open(path, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def archive_name(generation: int) -> str:
    return f"manifest.gen{int(generation):06d}.json"


def publish(root: str, manifest: dict) -> str:
    """Validate → write the immutable generation archive → atomically replace the pointer.

    `os.replace` is the commit point: atomic on a same-filesystem POSIX rename, so a reader
    sees either the old inode or the new one, never a partial file."""
    validate_manifest(manifest)
    os.makedirs(root, exist_ok=True)
    text = json.dumps(manifest, indent=2, sort_keys=True)
    arch = os.path.join(root, archive_name(manifest["generation"]))
    if os.path.exists(arch):
        raise ManifestError(f"generation archive already exists: {arch} (archives are "
                            f"immutable and must never be rewritten)")
    _write_fsync(arch, text)
    tmp = os.path.join(root, f".{LIVE_NAME}.tmp-{manifest['generation']}")
    _write_fsync(tmp, text)
    os.replace(tmp, os.path.join(root, LIVE_NAME))
    _fsync_dir(root)
    return arch


def rollback_to(root: str, generation: int) -> str:
    """Make `generation` live again WITHOUT consuming its archive (§9.3).

    copy archive → verify (parse, checksum, generation) → fsync → `os.replace(temp, live)`.
    `os.replace(archive, live)` would rename the archive away, destroying the record a second
    rollback, a forward-recovery or an audit needs."""
    arch = os.path.join(root, archive_name(generation))
    if not os.path.isfile(arch):
        raise ManifestError(f"no archive for generation {generation}: {arch}")
    tmp = os.path.join(root, f".{LIVE_NAME}.rollback-tmp-{generation}")
    shutil.copyfile(arch, tmp)
    try:
        with open(tmp) as fh:
            candidate = json.load(fh)
        validate_manifest(candidate)
        if int(candidate["generation"]) != int(generation):
            raise ManifestError(
                f"archive {arch} declares generation {candidate['generation']}, "
                f"expected {generation}")
        with open(tmp, "a") as fh:
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    os.replace(tmp, os.path.join(root, LIVE_NAME))
    _fsync_dir(root)
    if not os.path.isfile(arch):                      # defensive: must still be there
        raise ManifestError("rollback consumed the generation archive")
    return arch


def load_live(root: str) -> dict:
    path = os.path.join(root, LIVE_NAME)
    if not os.path.isfile(path):
        raise ManifestError(f"no live manifest at {path}")
    with open(path) as fh:
        return json.load(fh)
