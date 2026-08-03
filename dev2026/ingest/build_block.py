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
from store.compaction_lock import CompactionLock  # noqa: E402

VARS = ("sst", "sst_anomaly", "sea_ice")
BASE_SPATIAL, BASE_TIME, BASE_SHARD = 8, 90, 128
CK_NAME = "_block_build_ck.jsonl"
PROGRESS = "p5_block_build_progress.jsonl"
ERROR_JSON = "p5_block_build_error.json"
PLAN_JSON = "p5_block_plan.json"

#: source kinds, in the order §7.1a resolves them
SOURCE_ORDER = ("delta", "block", "daily", "hold", "netcdf")


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
def _store_day_index(path: str) -> Dict[str, int]:
    """`day -> physical index` from a cube-like store, BY DATE.

    Never by position: delta `attrs['days']` is append order after a backfill (P4-S4 §2)."""
    g = zarr.open_group(path, mode="r")
    return {d: i for i, d in enumerate(g.attrs["days"])}


def _daily_has(daily_root: str, day: str) -> bool:
    y, m, d = day.split("-")
    return os.path.isdir(os.path.join(daily_root, y, m, d))


def resolve_source_map(days: Sequence[str], *, delta_path: Optional[str] = None,
                       predecessor_path: Optional[str] = None,
                       daily_root: Optional[str] = None,
                       hold_root: Optional[str] = None) -> Dict[str, dict]:
    """`day -> {source_kind, source_path, source_day_index}` for EVERY requested day.

    Resolution order is the serving authority (§7.1a). A day present in delta resolves to
    delta even when the predecessor block also holds it — that is what makes a post-prune
    repair win over the block's stale copy, instead of being frozen out at the next fold."""
    indexes: Dict[str, Dict[str, int]] = {}
    for kind, path in (("delta", delta_path), ("block", predecessor_path)):
        if path and os.path.isdir(path):
            indexes[kind] = _store_day_index(path)

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


# --------------------------------------------------------------------------- reads
def _read_tile(source: dict, var: str, i0: int, i1: int, j0: int, j1: int):
    """Read ONE spatial tile of one (day, var). Never a whole slab."""
    kind, path, t = source["source_kind"], source["source_path"], source["source_day_index"]
    if kind in ("daily", "hold"):
        y, m, d = source["day"].split("-")
        g = zarr.open_group(os.path.join(path, y, m, d), mode="r")
        if var not in g:
            return None
        return np.asarray(g[var][0, i0:i1, j0:j1])
    g = zarr.open_group(path, mode="r")
    if var not in g:
        return None
    valid = dict(g.attrs.get("var_valid", {}))
    if var in valid and not valid[var][t]:
        return None                                  # absent on that day -> stays absent
    return np.asarray(g[var][t, i0:i1, j0:j1])


def _source_has_var(source: dict, var: str) -> bool:
    kind, path, t = source["source_kind"], source["source_path"], source["source_day_index"]
    if kind in ("daily", "hold"):
        y, m, d = source["day"].split("-")
        g = zarr.open_group(os.path.join(path, y, m, d), mode="r")
        return var in g
    g = zarr.open_group(path, mode="r")
    if var not in g:
        return False
    valid = dict(g.attrs.get("var_valid", {}))
    return bool(valid[var][t]) if var in valid else True


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
                lock: Optional[CompactionLock] = None,
                artifacts_dir: Optional[str] = None,
                tile: int = 256, resume: bool = False,
                hard_reserve_bytes: int = 0,
                sealed: Optional[bool] = None) -> dict:
    """Build one immutable block into `out_path` and return a publish plan.

    Refuses (writing nothing) when: the target path exists without a resumable checkpoint; a
    day in `rebuild_source_set` has no source; a day falls inside the protected spatial
    window; or the disk precheck fails.
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
            resume=resume, hard_reserve_bytes=hard_reserve_bytes, sealed=sealed)
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
                 tile, resume, hard_reserve_bytes, sealed) -> dict:
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

    # ---- §7.1a source map for EVERY rebuild day, resolved BEFORE any write
    smap = resolve_source_map(rebuild_source_set, delta_path=delta_path,
                              predecessor_path=predecessor_path, daily_root=daily_root,
                              hold_root=hold_root)
    unresolved = [d for d in rebuild_source_set if d not in smap]
    if unresolved:
        raise BuildRefused(
            f"{len(unresolved)} day(s) in rebuild_source_set have no source, first "
            f"{unresolved[0]}. Refusing before writing anything.")
    for d, s in smap.items():
        s["day"] = d
    journal.event(event="source_map",
                  rebuild_source_set=len(rebuild_source_set),
                  classification_target=len(classification_target),
                  by_kind={k: sum(1 for s in smap.values() if s["source_kind"] == k)
                           for k in SOURCE_ORDER})

    # ---- geometry from a source, then the disk precheck
    probe = smap[rebuild_source_set[0]]
    ny, nx, lon, lat = _probe_grid(probe)
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
            present_flags[v].append(_source_has_var(smap[d], v))
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
                    block = _read_tile(smap[day], v, i0, i1, j0, j1)
                    if block is not None:
                        g[v][t, i0:i1, j0:j1] = block
            _mark(ck_path, key)
            journal.event(event="var_done", day=day, var=v,
                          source=smap[day]["source_kind"], target_index=t)

    # ---- finalize attrs LAST (a partial build is never a valid store)
    g.attrs["days"] = list(rebuild_source_set)
    g.attrs["vars"] = list(keep_vars)
    g.attrs["var_valid"] = {v: [bool(x) for x in present_flags[v]] for v in keep_vars}
    g.attrs["region"] = [0, ny, 0, nx]
    g.attrs["layout"] = "time_lat_lon"
    build_s = round(time.perf_counter() - t0, 2)
    journal.event(event="build_finalized", days=T, build_s=build_s)

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
            "source_map_digest": bm.day_digest(
                [f"{d}:{smap[d]['source_kind']}" for d in rebuild_source_set]),
            "materialized_repairs": {},
            "sources": {d: smap[d]["source_kind"] for d in rebuild_source_set},
        },
    }
    plan = {"status": "ok", "out_path": out_path, "segment": segment,
            "rebuild_source_set": rebuild_source_set,
            "classification_target": sorted(classification_target),
            "disk": disk, "build_s": build_s, "performed_publish": False,
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
def _probe_grid(source: dict):
    kind, path = source["source_kind"], source["source_path"]
    if kind in ("daily", "hold"):
        y, m, d = source["day"].split("-")
        g = zarr.open_group(os.path.join(path, y, m, d), mode="r")
    else:
        g = zarr.open_group(path, mode="r")
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
