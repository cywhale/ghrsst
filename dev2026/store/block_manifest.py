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
from typing import Iterable, List, Optional, Sequence

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


def verify_store_self_consistency(store_path: str, *, allow_implicit_var_valid: bool = False
                                  ) -> dict:
    """Validate a segment store's RAW attrs before anything derives a view from them.

    Every helper that filters (`if v in g`) or coerces (`bool(x)`) launders a malformed store
    into a well-formed one — the store then passes every downstream check because the
    downstream check is looking at the laundered view. So this runs FIRST, on the raw values,
    and everything else consumes its output.

    Returns `{"days", "vars", "var_valid", "var_valid_implicit"}`. Raises `ManifestError`.
    """
    import numpy as np
    import zarr
    g = zarr.open_group(store_path, mode="r")
    attrs = dict(g.attrs)

    if "days" not in attrs:
        raise ManifestError(f"{store_path}: attrs['days'] is missing")
    days = list(attrs["days"])
    if len(set(days)) != len(days):
        raise ManifestError(f"{store_path}: attrs['days'] contains duplicate days")

    raw_vars = list(attrs.get("vars", []))
    if not raw_vars:
        raise ManifestError(f"{store_path}: attrs['vars'] is missing or empty")
    if len(set(raw_vars)) != len(raw_vars):
        dupes = sorted({v for v in raw_vars if raw_vars.count(v) > 1})
        raise ManifestError(f"{store_path}: attrs['vars'] has duplicate entries {dupes}")

    for v in raw_vars:
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
        # builder contract (§7.2): float32 with a NaN fill. A non-NaN fill turns an
        # unwritten or missing chunk from `null` into a real value at the API.
        if str(arr.dtype) != DTYPE:
            raise ManifestError(
                f"{store_path}: variable {v!r} has dtype {arr.dtype}, expected {DTYPE}")
        fill = arr.fill_value
        if fill is None or not np.isnan(np.asarray(fill, dtype="float64")):
            raise ManifestError(
                f"{store_path}: variable {v!r} has fill_value {fill!r}, expected NaN — a "
                f"non-NaN fill makes a missing chunk read as data instead of null")

    raw_vv = attrs.get("var_valid", None)
    implicit = False
    if raw_vv is None:
        if not allow_implicit_var_valid:
            raise ManifestError(
                f"{store_path}: attrs['var_valid'] is missing. It is required; a store "
                f"predating it is admissible only when the manifest declares "
                f"var_valid_mode='implicit_all_true' on a legacy_base segment.")
        implicit = True
        var_valid = {v: [True] * len(days) for v in raw_vars}
    else:
        raw_vv = dict(raw_vv)
        if set(raw_vv) != set(raw_vars):
            missing = sorted(set(raw_vars) - set(raw_vv))
            extra = sorted(set(raw_vv) - set(raw_vars))
            raise ManifestError(
                f"{store_path}: var_valid keys do not match attrs['vars'] — "
                f"missing {missing}, unexpected {extra}")
        var_valid = {}
        for v, flags in raw_vv.items():
            if not isinstance(flags, (list, tuple)):
                raise ManifestError(f"{store_path}: var_valid[{v!r}] is not a list")
            flags = list(flags)
            bad = [(i, x) for i, x in enumerate(flags) if type(x) is not bool]
            if bad:
                i, x = bad[0]
                raise ManifestError(
                    f"{store_path}: var_valid[{v!r}][{i}] is {type(x).__name__} {x!r}, not a "
                    f"bool. Coercing it would hide the type error, and the read path tests "
                    f"`is False`, so a truthy string would not behave as the value implies.")
            if len(flags) != len(days):
                raise ManifestError(
                    f"{store_path}: var_valid[{v!r}] has {len(flags)} flag(s) for "
                    f"{len(days)} day(s)")
            var_valid[v] = flags

    for v in raw_vars:
        if int(g[v].shape[0]) != len(days):
            raise ManifestError(
                f"{store_path}: variable {v!r} has {int(g[v].shape[0])} time step(s) but "
                f"attrs['days'] declares {len(days)}")

    return {"days": days, "vars": raw_vars, "var_valid": var_valid,
            "var_valid_implicit": implicit}


def array_shapes(store_path: str) -> dict:
    """`{var: [T, ny, nx]}` for every DECLARED variable. Raises if a declared variable has no
    array — it must not be silently filtered out."""
    import zarr
    g = zarr.open_group(store_path, mode="r")
    out = {}
    for v in sorted(g.attrs.get("vars", [])):
        if v not in g:
            raise ManifestError(f"{store_path}: declared variable {v!r} has no array")
        out[v] = [int(x) for x in g[v].shape]
    return out


def store_variables(store_path: str) -> List[str]:
    """The RAW declared variables, sorted. No `if v in g` filter -- a declared variable with
    no array is a defect to surface, not one to hide."""
    import zarr
    g = zarr.open_group(store_path, mode="r")
    return sorted(g.attrs.get("vars", []))


def store_axes(store_path: str) -> dict:
    """ny/nx plus lon/lat digests -- the identity two segments must share, or the same
    lon/lat would resolve to different physical cells in different blocks."""
    import numpy as np
    import zarr
    g = zarr.open_group(store_path, mode="r")
    lon = np.asarray(g["lon"][:], dtype="float64")
    lat = np.asarray(g["lat"][:], dtype="float64")
    return {"ny": int(lat.size), "nx": int(lon.size),
            "lon_digest": hashlib.sha256(lon.tobytes()).hexdigest(),
            "lat_digest": hashlib.sha256(lat.tobytes()).hexdigest(),
            "region": [int(x) for x in g.attrs.get("region", [])]}


def metadata_fingerprint(store_path: str) -> str:
    """sha256 over a segment's structural AND semantic metadata: axes, region, variables,
    per-array shape/chunks/shards/dtype/fill_value, and a digest of each `var_valid` vector
    (its CONTENT, not merely its length -- a single flipped flag changes what the API returns
    for that day).

    This is `fingerprint.metadata` in the manifest. It is what makes a manifest entry
    falsifiable against the store it names -- a day set alone cannot detect a block that was
    built on a different grid."""
    import numpy as np
    import zarr
    g = zarr.open_group(store_path, mode="r")
    vars_ = sorted(g.attrs.get("vars", []))
    for v in vars_:
        if v not in g:
            raise ManifestError(
                f"{store_path}: cannot fingerprint -- declared variable {v!r} has no array. "
                f"A builder must not be able to produce a manifest for a broken store.")
    var_valid = {k: list(v) for k, v in dict(g.attrs.get("var_valid", {}) or {}).items()}
    axes = store_axes(store_path)
    doc = {
        "axes": axes,
        "vars": vars_,
        "arrays": {v: {"shape": [int(x) for x in g[v].shape],
                       "chunks": [int(x) for x in g[v].chunks],
                       "shards": ([int(x) for x in g[v].shards]
                                  if getattr(g[v], "shards", None) else None),
                       "dtype": str(g[v].dtype),
                       "fill_is_nan": bool(g[v].fill_value is not None and np.isnan(
                           np.asarray(g[v].fill_value, dtype="float64")))}
                   for v in vars_},
        # var_valid CONTENT, not just its length. A single flipped flag turns a day from
        # "returns sst" into "omits sst" -- an API-visible semantic change inside a block
        # that claims to be immutable. Length alone cannot see it.
        # RAW values, not `bool(x)`: coercing here would make a poisoned entry hash the same
        # as a correct one, which is precisely how a bad type slips past.
        "var_valid_digest": {v: hashlib.sha256(
            canonical_json(list(var_valid.get(v, []))).encode()).hexdigest()
            for v in vars_},
        "var_valid_len": {v: len(var_valid.get(v, [])) for v in vars_},
        # NOTE: the day set is deliberately NOT included here. It is covered by
        # `fingerprint.day_digest` and by the explicit day-set comparison at snapshot build.
        # Duplicating it would make this fingerprint fire first and rob those checks of their
        # isolating test -- the same vacuous-test trap that bit the H2 gate earlier.
    }
    return hashlib.sha256(canonical_json(doc).encode()).hexdigest()


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
