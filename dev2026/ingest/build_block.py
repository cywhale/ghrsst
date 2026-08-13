"""dev2026 — P5-S3: the tail-block builder (design spec §7.1a–§7.4, §5.5).

Builds ONE immutable base block into a fresh staging path and returns a **publish plan**. It
never publishes, never touches a live store, and never writes outside `out_path` and the
artifacts directory.

## The write-side guard (the point of this module)

Five review rounds of P5-S1 all had one shape: **validation reading a derived view instead of
the thing itself**, so a malformed store was laundered into a well-formed one. Every one of
those was on the READ side, because that was the only side that existed. This module is the
write side, and it is built so the same class cannot reappear:

> **`build → inspect → fingerprint → plan` is the only path.** The builder does not compute a
> segment entry from what it *intended* to write. It re-opens what it *actually wrote*, runs
> the same strict `inspect_store_contract()` a reader would, and derives `layout`,
> `day_digest` and `fingerprint.metadata` from that inspection. A block that violates the
> contract cannot produce a plan at all.

So a manifest entry can only describe a store that passed the reader's own validator.

## The two day sets (§5.5)

* **`classification_target`** — the newly aged days this fold materializes; resolves
  `unknown → present | confirmed_missing`.
* **`rebuild_source_set`** — **every `present` day of the successor**: the predecessor's
  present days *plus* the newly classified ones. Each needs a source-map entry. This is not a
  delta of the previous fold; a new immutable block must physically contain every day it
  claims.

Source authority per day, resolved fresh at each build (§7.1a): **delta → published block →
daily staging → hold → NetCDF**. The same order the read path applies, so a fold materializes
what the API is serving today — which is why a day repaired back into delta after a prune wins
over the predecessor block's stale copy.

## Memory

Reads are **tiled**. 17 999 × 36 000 float32 is ~2.6 GB per var-day; the P4-S6 full-slab
validation was OOM-SIGKILLed with no traceback. Nothing here materializes a full slab, and the
gate asserts peak RSS does not scale with grid area.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import sys
import time
import traceback
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import zarr

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from store import block_manifest as bm  # noqa: E402
from store import repair_wal as rw  # noqa: E402
from store import source_provenance as sp  # noqa: E402
from store.compaction_lock import CompactionLock  # noqa: E402

VARS = ("sst", "sst_anomaly", "sea_ice")
BASE_SPATIAL, BASE_TIME, BASE_SHARD = 8, 90, 128
CK_NAME = "_block_build_ck.jsonl"
PROGRESS = "p5_block_build_progress.jsonl"
ERROR_JSON = "p5_block_build_error.json"
PLAN_JSON = "p5_block_plan.json"

#: Source kinds, in the order §7.1a resolves them.
#: NOTE: `netcdf` is deliberately ABSENT. The spec lists it as the deep recovery backstop, but
#: no resolver or reader exists here, and advertising a kind that cannot be resolved would make
#: "unresolved -> refuse" look like a missing day rather than a missing feature. Declared as
#: not delivered in the results doc; a day only NetCDF could supply refuses, loudly.
SOURCE_ORDER = ("delta", "block", "daily", "hold")


class BuildRefused(Exception):
    """A precondition failed. Nothing was written; there is no plan."""


# --------------------------------------------------------------------------- journal
class _Journal:
    """Append-only, fsync'd per event, so a SIGKILL still leaves the exact frontier."""

    def __init__(self, artifacts_dir: Optional[str]):
        self.dir = artifacts_dir
        self.path = os.path.join(artifacts_dir, PROGRESS) if artifacts_dir else None
        if artifacts_dir:
            os.makedirs(artifacts_dir, exist_ok=True)

    def event(self, **kw) -> None:
        if not self.path:
            return
        with open(self.path, "a") as fh:
            fh.write(json.dumps(kw, sort_keys=True, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def error(self, exc: BaseException, **ctx) -> None:
        if not self.dir:
            return
        with open(os.path.join(self.dir, ERROR_JSON), "w") as fh:
            json.dump({"error": f"{type(exc).__name__}: {exc}",
                       "traceback": traceback.format_exc(), "context": ctx},
                      fh, indent=2, sort_keys=True, default=str)
            fh.flush()
            os.fsync(fh.fileno())


# --------------------------------------------------------------------------- source map
def _store_day_index(path: str, *, group=None) -> Dict[str, int]:
    """`day -> physical index` from a cube-like store, BY DATE.

    Never by position: delta `attrs['days']` is append order after a backfill (P4-S4 §2)."""
    g = zarr.open_group(path, mode="r") if group is None else group
    return {d: i for i, d in enumerate(g.attrs["days"])}


def _daily_has(daily_root: str, day: str) -> bool:
    y, m, d = day.split("-")
    return os.path.isdir(os.path.join(daily_root, y, m, d))


def resolve_source_map(days: Sequence[str], *, delta_path: Optional[str] = None,
                       predecessor_path: Optional[str] = None,
                       daily_root: Optional[str] = None,
                       hold_root: Optional[str] = None, reader=None) -> Dict[str, dict]:
    """`day -> {source_kind, source_path, source_day_index}` for EVERY requested day.

    Resolution order is the serving authority (§7.1a). A day present in delta resolves to
    delta even when the predecessor block also holds it — that is what makes a post-prune
    repair win over the block's stale copy, instead of being frozen out at the next fold."""
    indexes: Dict[str, Dict[str, int]] = {}
    for kind, path in (("delta", delta_path), ("block", predecessor_path)):
        if path and os.path.isdir(path):
            indexes[kind] = _store_day_index(
                path, group=None if reader is None else reader._group(path))

    out: Dict[str, dict] = {}
    for day in days:
        for kind in SOURCE_ORDER:
            if kind in indexes and day in indexes[kind]:
                path = delta_path if kind == "delta" else predecessor_path
                out[day] = {"source_kind": kind, "source_path": path,
                            "source_day_index": indexes[kind][day]}
                break
            if kind == "daily" and daily_root and _daily_has(daily_root, day):
                out[day] = {"source_kind": "daily", "source_path": daily_root,
                            "source_day_index": 0}
                break
            if kind == "hold" and hold_root and _daily_has(hold_root, day):
                out[day] = {"source_kind": "hold", "source_path": hold_root,
                            "source_day_index": 0}
                break
    return out


def source_map_record(day: str, source: dict) -> dict:
    """The per-day provenance record: WHICH bytes were read, not merely from which tier.

    `source_kind` alone cannot answer the audit question. Two blocks can both say `"block"`
    for day D and have read different predecessor versions; two can both say `"delta"` and have
    read different physical indices, which matters precisely because delta `attrs['days']` is
    APPEND order after a backfill (P4-S4 §2) — the same date can sit at a different index in
    two otherwise identical deltas. A late correction is exactly the case where "we read D from
    delta" is true of both the stale and the corrected read.

    `source_path` is stored **resolved** (`realpath`). The delta path is a stable alias that
    gets retargeted at swap; the alias records what we were *pointed at*, the resolved path
    records what we actually *read*. For provenance the latter is the answer worth keeping."""
    return {"source_kind": source["source_kind"],
            "source_path": os.path.realpath(source["source_path"]),
            "source_day_index": int(source["source_day_index"])}


def source_map_digest(sources: Dict[str, dict]) -> str:
    """sha256 over the canonicalized WHOLE map (§5 `build_provenance.source_map_digest`).

    Digesting `day:source_kind` made two builds that read different bytes hash identically,
    which defeats the only purpose the digest has.

    **What this does NOT prove.** It is a digest of the source *locator* map, not a checksum of
    the bytes read. If the store at a recorded `(source_path, source_day_index)` is illegally
    overwritten in place, this digest is unchanged and will not notice. It answers "which store
    and which physical slot did this block read", never "and the contents were these".

    Consequently it **must not be used on its own to authorize pruning a corrected day**. That
    authorization is identity-based and belongs to the repair WAL and the §7.5a prune gate,
    neither of which exists yet; immutable block paths and the §7.8 corrective refold supply
    the rest. Until those land, a stronger reading of this digest than the one above is wrong."""
    return hashlib.sha256(bm.canonical_json(
        {d: dict(sorted(rec.items())) for d, rec in sorted(sources.items())}).encode()
    ).hexdigest()


# --------------------------------------------------------------------------- disk precheck
def disk_precheck(out_path: str, *, block_bytes_estimate: int, hard_reserve_bytes: int,
                  growth_bytes: int = 0, temp_bytes: int = 0,
                  pinned_existing_bytes: int = 0) -> dict:
    """§7.1c — gate on ADDITIONAL allocation only.

    `free_now` already excludes everything on disk, so charging the predecessor and superseded
    blocks again makes the gate progressively stricter each fold and would refuse the very
    sealing fold that ends a cycle. They are reported as `pinned_existing` for the lifecycle
    forecast, never subtracted twice."""
    st = os.statvfs(os.path.dirname(os.path.abspath(out_path)) or ".")
    free_now = st.f_bavail * st.f_frsize
    additional = int(block_bytes_estimate) + int(temp_bytes) + int(growth_bytes)
    projected = free_now - additional
    return {"free_now_bytes": int(free_now), "additional_bytes": additional,
            "block_bytes_estimate": int(block_bytes_estimate),
            "temp_bytes": int(temp_bytes), "growth_bytes": int(growth_bytes),
            "pinned_existing_bytes": int(pinned_existing_bytes),   # reported, NOT charged
            "hard_reserve_bytes": int(hard_reserve_bytes),
            "projected_free_after_bytes": int(projected),
            "ok": bool(projected >= hard_reserve_bytes)}


# --------------------------------------------------------------------------- sources
def grid_identity(lon_digest, lat_digest, lon_shape, lat_shape, lon_dtype, lat_dtype) -> dict:
    """The S1 grid identity, in one place so every source is compared the same way.

    Shape alone is not identity. Two sources can both be 18000x36000 and still be different
    grids -- shifted by half a cell, or on a different longitude convention -- and folding them
    together writes two geographies into one axis pair. S1 already settled what identity means
    (value digest + shape + dtype, digest over float64 bytes); this reuses it rather than
    inventing a second, subtly different notion of "same grid"."""
    return {"lon_digest": lon_digest, "lat_digest": lat_digest,
            "lon_shape": tuple(int(x) for x in lon_shape),
            "lat_shape": tuple(int(x) for x in lat_shape),
            "lon_dtype": str(lon_dtype), "lat_dtype": str(lat_dtype)}


def inspect_daily_day(root: str, day: str, *, group=None) -> dict:
    """Strict contract check for ONE daily-group day.

    `inspect_store_contract` does not fit a daily group (no `days` attr, shape `(1,ny,nx)`),
    but the same principle applies: an OUTPUT inspection proves the output is structurally
    valid, never that the INPUT was semantically sound. A source with a corrupt dtype, shape
    or axis would otherwise be copied faithfully into a block that then inspects clean.

    The axes go through `block_manifest._axis` -- the same validator the cube inspection uses.
    Reimplementing it here would drift, and worse, would have to reproduce its digest
    convention exactly or every daily source would look like a different grid from every delta
    source. `group` lets the caller pass a handle it already holds, so the strict check costs
    no extra `zarr.open_group`."""
    y, m, d = day.split("-")
    path = os.path.join(root, y, m, d)
    if group is None:
        if not os.path.isdir(path):
            raise bm.ManifestError(f"{path}: daily group missing")
        group = zarr.open_group(path, mode="r")
    lon, lon_shape, lon_dtype = bm._axis(group, "lon", path)
    lat, lat_shape, lat_dtype = bm._axis(group, "lat", path)
    ny, nx = int(lat.size), int(lon.size)
    present = []
    for v in VARS:
        if v not in group:
            continue
        arr = group[v]
        if arr.ndim != 3 or arr.shape[0] != 1:
            raise bm.ManifestError(f"{path}: {v!r} must be (1, ny, nx), got {arr.shape}")
        if (arr.shape[1], arr.shape[2]) != (ny, nx):
            raise bm.ManifestError(
                f"{path}: {v!r} is {arr.shape[1]}x{arr.shape[2]}, axes say {ny}x{nx}")
        if str(arr.dtype) != "float32":
            raise bm.ManifestError(f"{path}: {v!r} has dtype {arr.dtype}, expected float32")
        present.append(v)
    if not present:
        raise bm.ManifestError(f"{path}: no data variables")
    identity = grid_identity(hashlib.sha256(lon.tobytes()).hexdigest(),
                             hashlib.sha256(lat.tobytes()).hexdigest(),
                             lon_shape, lat_shape, lon_dtype, lat_dtype)
    return {"ny": ny, "nx": nx, "vars": present, "grid": identity}


class _SourceReader:
    """Opens every distinct source ONCE, strictly, and holds the handles.

    Two defects this closes. **Performance:** re-opening a group per tile meant roughly
    `days x vars x tiles` opens — about 2.7 million for a 90-day block at production geometry
    (17999x36000, 256-tiles, 3 vars), which would put compaction back into the hours it exists
    to avoid. **Correctness:** each open also re-read `var_valid` raw and coerced it, which is
    the laundering pattern the read side spent five review rounds removing.
    """

    def __init__(self):
        self._groups: Dict[str, object] = {}
        self._cube_valid: Dict[str, Dict[str, list]] = {}
        self._daily_meta: Dict[str, dict] = {}
        self._grids: Dict[str, dict] = {}    # source label -> grid IDENTITY, not just size
        self.opens = 0                      # observable, so a gate can assert it stays small

    def _group(self, path: str):
        g = self._groups.get(path)
        if g is None:
            g = zarr.open_group(path, mode="r")
            self._groups[path] = g
            self.opens += 1
        return g

    def inspect_cube(self, path: str) -> None:
        """Strict inspection of a delta / predecessor-block source, once per path."""
        if path in self._cube_valid:
            return
        insp = bm.inspect_store_contract(path)          # raises on any contract violation
        self._cube_valid[path] = {v: list(flags) for v, flags in insp.var_valid}
        self._grids[path] = grid_identity(insp.lon_digest, insp.lat_digest,
                                          insp.lon_shape, insp.lat_shape,
                                          insp.lon_dtype, insp.lat_dtype)
        # `inspect_store_contract` opens the group itself, and its signature is S1's. That
        # open is real, so it is counted here rather than hidden -- see `opens`.
        self.opens += 1
        self._group(path)

    def inspect_daily(self, root: str, day: str) -> None:
        key = f"{root}|{day}"
        if key not in self._daily_meta:
            y, m, d = day.split("-")
            path = os.path.join(root, y, m, d)
            if not os.path.isdir(path):
                raise bm.ManifestError(f"{path}: daily group missing")
            meta = inspect_daily_day(root, day, group=self._group(path))   # reuse the handle
            self._daily_meta[key] = meta
            self._grids[key] = meta["grid"]

    def assert_uniform_grid(self) -> None:
        """Every source must be on the SAME grid -- by IDENTITY, not by size.

        A block has one `lon`/`lat` axis pair. Sources on different grids each write their
        tiles into it and the result inspects perfectly clean while interleaving two
        geographies day by day, invisible to the output guard, which sees only one axis.

        Comparing `(ny, nx)` is not enough: two sources can both be 18000x36000 and be shifted
        by half a cell, or use 0..360 against -180..180. Those are exactly the cases that
        produce plausible-looking wrong data rather than an error, so the comparison is over
        the full S1 identity -- axis value digest, shape and dtype."""
        keys = sorted(self._grids)
        if not keys:
            return
        ref = self._grids[keys[0]]
        for k in keys[1:]:
            other = self._grids[k]
            if other == ref:
                continue
            diff = sorted(f for f in ref if ref[f] != other[f])
            raise bm.ManifestError(
                f"sources disagree on the grid: {keys[0]!r} and {k!r} differ in {diff}. "
                f"A block has ONE axis pair, so folding these together would interleave "
                f"geographies day by day. "
                f"{keys[0]}={_grid_str(ref)} vs {k}={_grid_str(other)}")

    def grid(self) -> dict:
        return next(iter(self._grids.values()))

    def has_var(self, source: dict, var: str) -> bool:
        kind, path, t = source["source_kind"], source["source_path"], source["source_day_index"]
        if kind in ("daily", "hold"):
            return var in self._daily_meta[f"{path}|{source['day']}"]["vars"]
        valid = self._cube_valid[path]
        if var not in valid:
            return False
        flag = valid[var][t]
        return flag is True                 # strict: validated as a real bool by the inspection

    def read_tile(self, source: dict, var: str, i0, i1, j0, j1):
        """ONE spatial tile of one (day, var). Never a whole slab, never a fresh open."""
        kind, path, t = source["source_kind"], source["source_path"], source["source_day_index"]
        if kind in ("daily", "hold"):
            y, m, d = source["day"].split("-")
            g = self._group(os.path.join(path, y, m, d))
            return None if var not in g else np.asarray(g[var][0, i0:i1, j0:j1])
        if not self.has_var(source, var):
            return None
        return np.asarray(self._group(path)[var][t, i0:i1, j0:j1])


# --------------------------------------------------------------------------- the builder
def build_block(out_path: str, *, start_day: str, end_day: str,
                classification_target: Sequence[str],
                predecessor_present: Sequence[str] = (),
                delta_path: Optional[str] = None,
                predecessor_path: Optional[str] = None,
                daily_root: Optional[str] = None,
                hold_root: Optional[str] = None,
                confirmed_missing: Sequence[str] = (),
                spatial_window_days: int = 31,
                window_latest_day: Optional[str] = None,
                lock: Optional[CompactionLock],
                hard_reserve_bytes: int,
                artifacts_dir: Optional[str] = None,
                tile: int = 256, resume: bool = False,
                sealed: Optional[bool] = None,
                wal_root: Optional[str] = None,
                unsafe_skip_isolation: bool = False) -> dict:
    """Build one immutable block into `out_path` and return a publish plan.

    `lock` and `hard_reserve_bytes` are **required positional-by-keyword** arguments with no
    defaults. They used to default to `None` and `0`, which meant a caller who simply forgot
    them got a build with **no isolation and a disabled disk gate** — the guards existed but
    were opt-in, and the failure mode was silence. Now:

    * `lock` must be a **currently-held** `CompactionLock`, asserted BEFORE the first write as
      well as before the plan is emitted;
    * `hard_reserve_bytes` must be **> 0**.

    `unsafe_skip_isolation=True` is the only way past either, is named so it cannot be typed by
    accident, and is greppable. Tests that are not exercising isolation use it; nothing else
    should.

    Refuses (writing nothing) when: isolation is missing; the target path exists without a
    resumable checkpoint; a day in `rebuild_source_set` has no source; a source store violates
    its contract; a day falls inside the protected spatial window; or the disk precheck fails.
    """
    journal = _Journal(artifacts_dir)
    try:
        return _build_block(
            out_path, start_day=start_day, end_day=end_day,
            classification_target=list(classification_target),
            predecessor_present=list(predecessor_present), delta_path=delta_path,
            predecessor_path=predecessor_path, daily_root=daily_root, hold_root=hold_root,
            confirmed_missing=list(confirmed_missing),
            spatial_window_days=spatial_window_days, window_latest_day=window_latest_day,
            lock=lock, journal=journal, artifacts_dir=artifacts_dir, tile=tile,
            resume=resume, hard_reserve_bytes=hard_reserve_bytes, sealed=sealed,
            wal_root=wal_root, unsafe_skip_isolation=unsafe_skip_isolation)
    except BuildRefused as exc:
        journal.event(event="refused", reason=str(exc))
        raise
    except Exception as exc:                          # SIGKILL cannot be caught -- the
        journal.error(exc, out_path=out_path)         # fsync'd journal covers that case
        journal.event(event="error", error=str(exc))
        raise


def _build_block(out_path, *, start_day, end_day, classification_target, predecessor_present,
                 delta_path, predecessor_path, daily_root, hold_root, confirmed_missing,
                 spatial_window_days, window_latest_day, lock, journal, artifacts_dir,
                 tile, resume, hard_reserve_bytes, sealed, wal_root,
                 unsafe_skip_isolation) -> dict:
    # ---- isolation is MANDATORY unless explicitly, loudly waived
    if not unsafe_skip_isolation:
        if lock is None or not getattr(lock, "held", False):
            raise BuildRefused(
                "a held CompactionLock is required: without it a delta prune or swap can "
                "retarget the delta path mid-build and freeze wrong bytes into an immutable "
                "block (§7.1b). Pass a held lock, or unsafe_skip_isolation=True in a test "
                "that is not exercising isolation.")
        lock.assert_still_held()                       # before the FIRST write, not only at publish
        if int(hard_reserve_bytes) <= 0:
            raise BuildRefused(
                "hard_reserve_bytes must be > 0: a zero reserve disables the disk gate, and a "
                "compaction that fills the filesystem takes the API down (§7.1c)")

    span = bm.calendar_span(start_day, end_day)
    if not span:
        raise BuildRefused(f"end_day {end_day} precedes start_day {start_day}")

    # ---- §5.5 the two sets. rebuild_source_set is the WHOLE successor view.
    newly_present = [d for d in classification_target if d not in set(confirmed_missing)]
    rebuild_source_set = sorted(set(predecessor_present) | set(newly_present))
    stray = sorted(set(rebuild_source_set) - set(span))
    if stray:
        raise BuildRefused(f"days outside the block window: {stray[:3]}")

    # ---- §7.1 fold cutoff: never fold a day inside the protected spatial window
    if window_latest_day:
        cutoff = bm.add_days(window_latest_day, -(spatial_window_days - 1))
        inside = sorted(d for d in rebuild_source_set if d >= cutoff)
        if inside:
            raise BuildRefused(
                f"{len(inside)} day(s) fall inside the protected {spatial_window_days}-day "
                f"spatial window (>= {cutoff}), first {inside[0]}; folding them would remove "
                f"bbox/POST availability with no bbox-friendly tier to replace it")

    # ---- §7.1a source map for EVERY rebuild day, resolved BEFORE any write.
    # The reader is created here, before resolution, so the day-index read shares the same
    # handle the tiles will use instead of costing a third open of the delta.
    reader = _SourceReader()
    smap = resolve_source_map(rebuild_source_set, delta_path=delta_path,
                              predecessor_path=predecessor_path, daily_root=daily_root,
                              hold_root=hold_root, reader=reader)
    unresolved = [d for d in rebuild_source_set if d not in smap]
    if unresolved:
        raise BuildRefused(
            f"{len(unresolved)} day(s) in rebuild_source_set have no source, first "
            f"{unresolved[0]}. Refusing before writing anything.")
    for d, s in smap.items():
        s["day"] = d

    # ---- strict SOURCE inspection, once per distinct source, before anything is read.
    # An output inspection proves the output is structurally valid; it says nothing about
    # whether the input was sound. A corrupt source would otherwise be copied faithfully into
    # a block that then inspects clean.
    try:
        for path in {s["source_path"] for s in smap.values()
                     if s["source_kind"] in ("delta", "block")}:
            reader.inspect_cube(path)
        for s in smap.values():
            if s["source_kind"] in ("daily", "hold"):
                reader.inspect_daily(s["source_path"], s["day"])
        reader.assert_uniform_grid()
    except bm.ManifestError as exc:
        raise BuildRefused(f"source store violates its contract: {exc}") from exc
    journal.event(event="source_map",
                  rebuild_source_set=len(rebuild_source_set),
                  classification_target=len(classification_target),
                  by_kind={k: sum(1 for s in smap.values() if s["source_kind"] == k)
                           for k in SOURCE_ORDER})

    # ---- geometry from a source, then the disk precheck
    probe = smap[rebuild_source_set[0]]
    ny, nx, lon, lat = _probe_grid(probe, reader)
    T = len(rebuild_source_set)
    est = T * ny * nx * 4 * len(VARS)
    disk = disk_precheck(out_path, block_bytes_estimate=est,
                         hard_reserve_bytes=hard_reserve_bytes,
                         temp_bytes=max(est // 100, 1 << 20))
    journal.event(event="disk_precheck", **disk)
    if not disk["ok"]:
        raise BuildRefused(
            f"disk precheck failed: projected free after "
            f"{disk['projected_free_after_bytes']} < reserve {disk['hard_reserve_bytes']}")

    # ---- staging path discipline
    ck_path = os.path.join(out_path, CK_NAME)
    if os.path.exists(out_path):
        if not (resume and os.path.isfile(ck_path)):
            raise BuildRefused(
                f"{out_path} already exists and there is no resumable checkpoint; a block "
                f"path is written once and never reused")
    done = _load_ck(ck_path)

    # ---- create + fill, tiled
    present_flags = {v: [] for v in VARS}
    for d in rebuild_source_set:
        for v in VARS:
            present_flags[v].append(reader.has_var(smap[d], v))
    keep_vars = [v for v in VARS if any(present_flags[v])]

    if not done:
        _create(out_path, rebuild_source_set, keep_vars, ny, nx, lon, lat)
    g = zarr.open_group(out_path, mode="a")
    journal.event(event="build_start", days=T, vars=keep_vars, grid=[ny, nx], tile=tile)

    t0 = time.perf_counter()
    for t, day in enumerate(rebuild_source_set):
        for v in keep_vars:
            key = f"{day}|{v}"
            if key in done:
                continue
            if not present_flags[v][t]:
                _mark(ck_path, key)                   # absent -> NaN fill, no writes needed
                continue
            for i0 in range(0, ny, tile):
                for j0 in range(0, nx, tile):
                    i1, j1 = min(i0 + tile, ny), min(j0 + tile, nx)
                    block = reader.read_tile(smap[day], v, i0, i1, j0, j1)
                    if block is not None:
                        g[v][t, i0:i1, j0:j1] = block
            _mark(ck_path, key)
            journal.event(event="var_done", day=day, var=v,
                          source=smap[day]["source_kind"], target_index=t)

    # ---- §7.5a source-BYTE provenance, read from the SOURCE, not from what we wrote.
    # The locator map records where each day came from; this records what came out. One
    # tile-sized window per (day, var), through the same cached handle the fill used -- so it
    # is bounded I/O and it is the source's own bytes, not a re-read of our output (which would
    # only prove we can read back what we just wrote).
    prov_days = {}
    for t_idx, day in enumerate(rebuild_source_set):
        i0, i1, j0, j1 = sp.sample_window(ny, nx, seed=sp.SAMPLE_SEED, day_index=t_idx)
        # The variable domain is the canonical VARS, not `keep_vars`. A variable absent from
        # the whole block still has to appear as False: the verifier compares against the
        # source's real availability over VARS, and two sides digesting different key sets
        # disagree on every day for a reason that is not a difference.
        tiles, valid = {}, {}
        for v in VARS:
            has = bool(present_flags[v][t_idx]) and v in keep_vars
            valid[v] = has
            tiles[v] = reader.read_tile(smap[day], v, i0, i1, j0, j1) if has else None
        rec = source_map_record(day, smap[day])
        prov_days[day] = {
            "source_kind": rec["source_kind"], "source_path": rec["source_path"],
            "source_day_index": rec["source_day_index"], "day_index": t_idx,
            "source_fingerprint": sp.window_fingerprint(
                tiles, seed=sp.SAMPLE_SEED, day_index=t_idx, var_valid=valid),
            "var_valid": valid,
        }
    journal.event(event="source_fingerprints", days=len(prov_days), seed=sp.SAMPLE_SEED)

    # ---- finalize attrs LAST (a partial build is never a valid store)
    g.attrs["days"] = list(rebuild_source_set)
    g.attrs["vars"] = list(keep_vars)
    g.attrs["var_valid"] = {v: [bool(x) for x in present_flags[v]] for v in keep_vars}
    g.attrs["region"] = [0, ny, 0, nx]
    g.attrs["layout"] = "time_lat_lon"
    build_s = round(time.perf_counter() - t0, 2)
    journal.event(event="build_finalized", days=T, build_s=build_s,
                  source_group_opens=reader.opens)

    # ---- THE WRITE-SIDE GUARD: inspect what we ACTUALLY wrote, then derive from that
    if lock is not None:
        lock.assert_still_held()
    try:
        insp = bm.inspect_store_contract(out_path)
    except bm.ManifestError as exc:
        journal.event(event="inspection_failed", reason=str(exc))
        raise BuildRefused(
            f"the block we just wrote does not satisfy the store contract: {exc}. No plan is "
            f"emitted, so no manifest entry can describe it.") from exc

    unknown = [d for d in span
               if d not in set(rebuild_source_set) and d not in set(confirmed_missing)]
    is_sealed = (not unknown) if sealed is None else bool(sealed)
    if is_sealed and unknown:
        raise BuildRefused(f"cannot seal: {len(unknown)} day(s) still unknown")

    # §7.5a / §7.8: what repairs did this fold actually consume? DERIVED, never accepted -- the
    # caller does not get to say which correction a block carries. For every day this build read
    # FROM DELTA that has a committed repair in the WAL, record the repair id and the
    # fingerprint of the bytes we actually read. A day sourced from the predecessor block
    # carries the predecessor's claim forward and is not re-attested here: the fold did not read
    # the repair, so it must not say it did.
    materialized_repairs = {}
    if wal_root:
        from ingest.corrected_day import date_slot          # local: avoids an import cycle
        state = rw.read_wal(wal_root)                       # WalCorrupt -> the build refuses
        for t_idx, day in enumerate(rebuild_source_set):
            latest = state.latest_committed(day)
            if latest is None or smap[day]["source_kind"] != "delta":
                continue
            slot = date_slot(day)
            i0, i1, j0, j1 = sp.sample_window(ny, nx, seed=sp.SAMPLE_SEED, day_index=slot)
            tiles, valid = {}, {}
            for v in VARS:
                has = bool(present_flags[v][t_idx]) and v in keep_vars
                valid[v] = has
                tiles[v] = reader.read_tile(smap[day], v, i0, i1, j0, j1) if has else None
            materialized_repairs[day] = {
                "repair_id": latest.repair_id, "source_kind": "delta",
                "source_fingerprint": sp.window_fingerprint(
                    tiles, seed=sp.SAMPLE_SEED, day_index=slot, var_valid=valid),
            }
        journal.event(event="materialized_repairs", days=sorted(materialized_repairs))

    source_records = {d: source_map_record(d, smap[d]) for d in rebuild_source_set}
    segment = {
        "segment_id": os.path.basename(out_path.rstrip("/")),
        "kind": "block", "path": os.path.basename(out_path.rstrip("/")), "immutable": True,
        "boundary_kind": "calendar", "start_day": start_day, "end_day": end_day,
        "materialized_through": (max(set(rebuild_source_set) | set(confirmed_missing))
                                 if (rebuild_source_set or confirmed_missing) else start_day),
        "day_count": len(insp.days), "gaps": sorted(confirmed_missing),
        "unknown": sorted(unknown), "day_list": None,
        "layout": bm.segment_layout_from_inspection(insp),
        "variables": sorted(insp.vars),
        "fingerprint": {"algo": "sha256",
                        "metadata": bm.metadata_fingerprint_from_inspection(insp),
                        "day_digest": bm.day_digest(insp.days)},
        "precedence": 0, "sealed": is_sealed, "supersedes": None,
        "build_provenance": {
            "source_map_digest": source_map_digest(source_records),
            "materialized_repairs": materialized_repairs,
            "sources": source_records,
        },
    }
    # The artifact's grid comes from the INSPECTION of the block that was written, not from the
    # source probe that seeded the fill -- the block is the authority a verifier will re-read.
    # A divergence between the two would mean every fingerprint describes cells a verifier does
    # not read, and it cannot reach here: `inspect_store_contract` above already refuses a block
    # whose `region` does not match its own grid, so the build fails before a plan exists. An
    # explicit check here would be unreachable code that reads as a guard.
    provenance = sp.build_artifact(segment_id=segment["segment_id"], block_path=out_path,
                                   ny=int(insp.ny), nx=int(insp.nx), days=prov_days)
    provenance_path = None
    if artifacts_dir:
        provenance_path = sp.write_artifact(
            os.path.join(artifacts_dir, sp.ARTIFACT_NAME), provenance)
        journal.event(event="provenance_written", path=provenance_path,
                      days=len(prov_days))

    plan = {"status": "ok", "out_path": out_path, "segment": segment,
            "source_map": source_records,
            "provenance": provenance, "provenance_path": provenance_path,
            "rebuild_source_set": rebuild_source_set,
            "classification_target": sorted(classification_target),
            "disk": disk, "build_s": build_s, "performed_publish": False,
            "source_group_opens": reader.opens,
            "note": ("A plan, not a publication. The segment entry was derived from an "
                     "inspection of the block ACTUALLY written -- never from build intent.")}
    if artifacts_dir:                                  # success only
        with open(os.path.join(artifacts_dir, PLAN_JSON), "w") as fh:
            json.dump(plan, fh, indent=2, sort_keys=True, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        journal.event(event="plan_written", days=len(insp.days), sealed=is_sealed)
    return plan


# --------------------------------------------------------------------------- helpers
def _grid_str(g: dict) -> str:
    return (f"{g['lat_shape']}x{g['lon_shape']} {g['lat_dtype']}/{g['lon_dtype']} "
            f"lon:{g['lon_digest'][:8]} lat:{g['lat_digest'][:8]}")


def _probe_grid(source: dict, reader: "_SourceReader"):
    kind, path = source["source_kind"], source["source_path"]
    if kind in ("daily", "hold"):
        y, m, d = source["day"].split("-")
        g = reader._group(os.path.join(path, y, m, d))
    else:
        g = reader._group(path)
    lon = np.asarray(g["lon"][:], dtype="float32")
    lat = np.asarray(g["lat"][:], dtype="float32")
    return int(lat.size), int(lon.size), lon, lat


def _create(out_path, days, keep_vars, ny, nx, lon, lat):
    T = len(days)
    tc = min(BASE_TIME, T) if T else 1
    g = zarr.open_group(out_path, mode="w", zarr_format=3)
    g.create_array("lon", shape=(nx,), dtype="float32", chunks=(nx,))
    g["lon"][:] = lon
    g.create_array("lat", shape=(ny,), dtype="float32", chunks=(ny,))
    g["lat"][:] = lat
    for v in keep_vars:
        g.create_array(v, shape=(T, ny, nx), dtype="float32",
                       chunks=(tc, BASE_SPATIAL, BASE_SPATIAL),
                       shards=(tc, min(BASE_SHARD, ny), min(BASE_SHARD, nx)),
                       fill_value=float("nan"))


def _load_ck(path: str) -> set:
    if not os.path.isfile(path):
        return set()
    done = set()
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                done.add(line)
    return done


def _mark(path: str, key: str) -> None:
    with open(path, "a") as fh:
        fh.write(key + "\n")
        fh.flush()
        os.fsync(fh.fileno())
