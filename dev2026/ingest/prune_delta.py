"""dev2026 — P4-S6: safe delta pruning via REBUILD-THEN-SWAP (staging/shadow only for now).

The delta cube's ``attrs['days']`` is in PHYSICAL APPEND order (a backfill lands at the tail), and
``day_index[date]`` maps a date to its physical time slab. You therefore CANNOT prune a delta day by editing
``attrs['days']`` / ``var_valid`` in place or by truncating the time axis — that desyncs ``day_index`` from
the stored slabs and silently corrupts every later read (P4-S4 §2). The ONLY safe primitive is:

    build a FRESH delta containing exactly the kept days, written in CHRONOLOGICAL order, from a trusted
    source (daily staging preferred; else the existing delta read BY ``day_index``), validate it, and hand
    the caller an atomic-SWAP PLAN — the swap itself is a later phase.

**S9 field-failure hardening (VM24 step-3 incident):** the original wrapper died silently at production
scale — the OLD validation materialized FULL GLOBAL slabs (~2.6 GB per var-day at 17999×36000) just to
sample a few points, so the process was OOM-killed after the build finished, leaving no plan and no error
artifact. Fixes:
- **validation is now point-wise** (per-sample scalar reads; never a full slab) — the root-cause fix;
- **``engine='bulk'`` (default)**: pre-creates the final arrays at full shape ``(len(keep_days), ny, nx)``
  and writes disjoint (day, var, spatial-tile) units bounded-parallel; workers NEVER resize arrays or
  touch attrs; ``days``/``var_valid`` are finalized LAST; an append-only checkpoint enables ``resume=True``;
- **progress/error artifacts** (``artifacts_dir=``): ``prune_delta_progress.jsonl`` (per var-day
  completion + validation start/end, each line flushed+fsync'd — even a SIGKILL leaves an exact frontier),
  ``prune_delta_error.json`` (traceback + completed state) on any catchable exception, and
  ``prune_plan.json`` written by the tool itself ONLY on success.
- ``engine='perday'`` keeps the original per-day writers (small-scale/tests; its delta-source fallback
  materializes full slabs — do not use it at production scale).

No swap, no production mutation, no in-place edit of the sources. Compatible with the P4-S5 audit output
and the P4-S4 safety model. Spec: ``specs/p4_ingest_prune_retention_design.md`` (§2, §5).
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence

import numpy as np
import zarr

from ingest.dual_write import DELTA_SPATIAL_CHUNK, DELTA_SHARD_SPATIAL, append_to_delta
from store.compaction_lock import refuse_if_compaction_running  # noqa: E402
from store.zarr_paths import group_exists, group_path

_CK_NAME = "_bulk_prune_ck.jsonl"                    # append-only checkpoint inside out_path


# --------------------------------------------------------------------------- calendar helpers (mirror P4-S5 §4.1)
def _d(s: str) -> date:
    return date.fromisoformat(s)


def _calendar_range(start: str, end: str) -> List[str]:
    d0, d1 = _d(start), _d(end)
    return [] if d0 > d1 else [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def recent_window_contiguous(delta_days: Sequence[str], spatial_window_days: int) -> dict:
    """Reproduce the P4-S5 §4.1 gate: the latest ``spatial_window_days`` CALENDAR days ending at
    max(delta_days) must all be present. Returns {contiguous, missing, window_start, window_end}."""
    dd = sorted(set(delta_days))
    if not dd:
        return {"contiguous": False, "missing": [], "window_start": None, "window_end": None,
                "reason": "no delta days"}
    end = dd[-1]
    start = (_d(end) - timedelta(days=spatial_window_days - 1)).isoformat()
    required = _calendar_range(start, end)
    present = set(dd)
    missing = [x for x in required if x not in present]
    return {"contiguous": not missing, "missing": missing, "window_start": start, "window_end": end}


# --------------------------------------------------------------------------- progress / error artifacts
class _Journal:
    """Append-only progress JSONL; every line is flushed + fsync'd so even a SIGKILL (e.g. the OOM kill
    that ate the VM24 step-3 run) leaves an exact record of the last completed unit. No-op without a dir."""

    def __init__(self, artifacts_dir: Optional[str]):
        self._fh = None
        self._lock = threading.Lock()
        if artifacts_dir:
            os.makedirs(artifacts_dir, exist_ok=True)
            self.path = os.path.join(artifacts_dir, "prune_delta_progress.jsonl")
            self._fh = open(self.path, "a")

    def event(self, **kw) -> None:
        if self._fh is None:
            return
        kw.setdefault("ts", datetime.now(timezone.utc).isoformat())
        line = json.dumps(kw, sort_keys=True, default=str)
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def _write_error(artifacts_dir: Optional[str], exc: BaseException, context: dict) -> None:
    if not artifacts_dir:
        return
    with open(os.path.join(artifacts_dir, "prune_delta_error.json"), "w") as fh:
        json.dump({"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc(),
                   "ts": datetime.now(timezone.utc).isoformat(), **context},
                  fh, indent=2, sort_keys=True, default=str)


# --------------------------------------------------------------------------- read-only source access
def _open_delta_meta(delta_path: str) -> dict:
    """Read-only snapshot of a delta cube's metadata (physical-order days + day_index + var_valid)."""
    g = zarr.open_group(delta_path, mode="r")                 # READ-ONLY
    days = list(g.attrs["days"])                              # physical/append order
    vars_ = list(g.attrs.get("vars", []))
    vv = {v: list(x) for v, x in dict(g.attrs.get("var_valid", {})).items()}
    a0 = g[vars_[0]] if vars_ else None
    ny = int(a0.shape[1]) if a0 is not None else 0
    nx = int(a0.shape[2]) if a0 is not None else 0
    return {
        "group": g,
        "days": days,
        "day_index": {d: i for i, d in enumerate(days)},      # date -> PHYSICAL slab index
        "vars": vars_,
        "var_valid": vv,
        "ny": ny, "nx": nx,
        "region": (tuple(g.attrs["region"]) if "region" in g.attrs else None),
    }


_COORD_NAMES = ("lon", "lat", "time")               # coordinate/helper arrays — NEVER data vars


def _daily_data_var(gd, v: str, i0: int, j0: int, ny: int, nx: int) -> bool:
    """True iff ``v`` is a REAL 3-D spatial data var in a daily group, covering the target region.
    Excludes coordinate/helper arrays by NAME (lon/lat/time) AND by SHAPE (must be (t, y, x) with the
    spatial extent containing the region) — the VM24 S9 failure was a 1-D ``time`` array collected as a
    data var and then indexed ``[0, y, x]``."""
    if v in _COORD_NAMES or v not in gd:              # absent var (e.g. sea_ice missing that day) -> False
        return False
    arr = gd[v]
    return (len(arr.shape) == 3
            and int(arr.shape[-2]) >= i0 + ny and int(arr.shape[-1]) >= j0 + nx)


def _delta_data_var(orig: dict, v: str) -> bool:
    """True iff ``v`` exists in the source delta as a (T, ny, nx) 3-D field (same name+shape filter)."""
    if v in _COORD_NAMES or v not in orig["group"]:
        return False
    arr = orig["group"][v]
    return len(arr.shape) == 3 and int(arr.shape[-2]) == orig["ny"] and int(arr.shape[-1]) == orig["nx"]


def _src_present(orig: dict, day_src: dict, day: str, v: str, i0: int, j0: int) -> bool:
    """Is var ``v`` present for ``day`` in its source (daily group, or the old delta BY day_index)?
    Presence implies the source array is a real 3-D spatial field (coord/helper arrays never count)."""
    kind, gd = day_src[day]
    if kind == "daily":
        return _daily_data_var(gd, v, i0, j0, orig["ny"], orig["nx"])
    if not _delta_data_var(orig, v):
        return False
    t = orig["day_index"][day]
    vv = orig["var_valid"].get(v)
    return bool(vv[t]) if vv is not None else True


def _read_delta_day(meta: dict, day: str) -> Dict[str, Optional[np.ndarray]]:
    """FULL-SLAB read of one day BY day_index (perday engine only — memory-heavy at production scale)."""
    t = meta["day_index"][day]
    g = meta["group"]
    out: Dict[str, Optional[np.ndarray]] = {}
    for v in meta["vars"]:
        vv = meta["var_valid"].get(v, [True] * len(meta["days"]))
        present = (v in g) and bool(vv[t])
        out[v] = np.asarray(g[v][t, :, :]) if present else None
    return out


# --------------------------------------------------------------------------- perday writer (legacy engine)
def _append_prebuilt_day(out_path: str, day: str, data: Dict[str, Optional[np.ndarray]],
                         ny: int, nx: int, lon: np.ndarray, lat: np.ndarray, region: Optional[tuple],
                         spatial_chunk: int, shard_spatial: int) -> None:
    """Append one in-memory day into the staging delta with the SAME var-union convention as append_to_delta,
    so the two writers can be MIXED on the same output: a var first seen on a later day is created with a
    NaN backfill + ``var_valid False`` for the prior days. finalize ``attrs['days']`` LAST; absent var ->
    NaN slab + valid False. perday engine only (staging/shadow scale)."""
    present_today = [v for v, arr in data.items() if arr is not None]
    if not os.path.isdir(out_path):                           # CREATE (empty, time_chunk=1)
        cy, cx = min(spatial_chunk, ny), min(spatial_chunk, nx)
        sh_y = min((shard_spatial // cy) * cy or cy, ny)
        sh_x = min((shard_spatial // cx) * cx or cx, nx)
        g = zarr.open_group(out_path, mode="w", zarr_format=3)
        g.create_array("lon", shape=(nx,), dtype="float32", chunks=(nx,)); g["lon"][:] = lon.astype(np.float32)
        g.create_array("lat", shape=(ny,), dtype="float32", chunks=(ny,)); g["lat"][:] = lat.astype(np.float32)
        for v in present_today:
            g.create_array(v, shape=(0, ny, nx), dtype="float32", chunks=(1, cy, cx),
                           shards=(1, sh_y, sh_x), fill_value=float("nan"))
        g.attrs["days"] = []
        g.attrs["vars"] = list(present_today)
        g.attrs["var_valid"] = {v: [] for v in present_today}
        if region:
            g.attrs["region"] = [int(x) for x in region]
        g.attrs["layout"] = "time_lat_lon"
    else:
        g = zarr.open_group(out_path, mode="a")
    days = list(g.attrs["days"]); t = len(days)
    delta_vars = list(g.attrs.get("vars", []))
    var_valid = {k: list(v) for k, v in dict(g.attrs.get("var_valid", {})).items()}
    for v in delta_vars:
        var_valid.setdefault(v, [True] * t)
    if delta_vars:
        a0 = g[delta_vars[0]]
        cy, cx = int(a0.chunks[-2]), int(a0.chunks[-1])
        sh_y, sh_x = int(a0.shards[-2]), int(a0.shards[-1])
    else:
        cy, cx = min(spatial_chunk, ny), min(spatial_chunk, nx)
        sh_y = min((shard_spatial // cy) * cy or cy, ny); sh_x = min((shard_spatial // cx) * cx or cx, nx)
    for v in present_today:                                   # NEW var present today but not yet in the delta
        if v not in delta_vars:
            g.create_array(v, shape=(t, ny, nx), dtype="float32", chunks=(1, cy, cx),
                           shards=(1, sh_y, sh_x), fill_value=float("nan"))
            delta_vars.append(v)
            var_valid[v] = [False] * t                        # var was absent for every prior day
    for v in delta_vars:
        arr = g[v]
        arr.resize((t + 1, ny, nx))
        present = data.get(v) is not None
        arr[t, :, :] = np.asarray(data[v], dtype=np.float32) if present else np.float32(np.nan)
        var_valid.setdefault(v, [True] * t).append(bool(present))
    g.attrs["vars"] = list(delta_vars)
    g.attrs["var_valid"] = var_valid
    g.attrs["days"] = days + [day]                            # FINALIZE days LAST (never a half day)


# --------------------------------------------------------------------------- bulk engine (default)
def _bulk_build(orig: dict, out_path: str, keep_sorted: List[str], *,
                source_daily: Optional[str], region: Optional[tuple],
                spatial_chunk: int, shard_spatial: int, workers: int,
                read_block: Optional[int], journal: _Journal, resume: bool) -> dict:
    """Bulk rebuild: pre-create the final arrays at shape (T, ny, nx), then write disjoint
    (day, var, spatial-tile) units bounded-parallel. Workers ONLY write array regions — never resize,
    never touch attrs. ``days``/``vars``/``var_valid`` are finalized LAST (finalize-days-last invariant).
    Absent vars need NO writes at all (arrays are NaN-filled by fill_value). Append-only checkpoint
    (one fsync'd line per unit) makes ``resume=True`` skip completed units."""
    ny, nx = orig["ny"], orig["nx"]
    lon = np.asarray(orig["group"]["lon"][:]); lat = np.asarray(orig["group"]["lat"][:])
    reg = region or orig["region"]
    i0, j0 = (reg[0], reg[2]) if reg else (0, 0)

    # per-day source (daily preferred; else old delta BY day_index) + var union across sources.
    # DATA-VAR FILTER (VM24 S9 fix): only true 3-D spatial fields join the union — coordinate/helper
    # arrays (lon/lat/time, or anything not (t,y,x)-shaped over the grid) are never created/written
    # into the pruned delta.
    all_vars = [v for v in orig["vars"] if _delta_data_var(orig, v)]
    day_src: Dict[str, tuple] = {}
    for day in keep_sorted:
        if source_daily and group_exists(source_daily, day):
            gd = zarr.open_group(group_path(source_daily, day), mode="r")
            for v in gd.array_keys():
                if v not in all_vars and _daily_data_var(gd, v, i0, j0, ny, nx):
                    all_vars.append(v)
            day_src[day] = ("daily", gd)
        else:
            day_src[day] = ("delta", None)

    T = len(keep_sorted)
    if not os.path.isdir(out_path):
        cy, cx = min(spatial_chunk, ny), min(spatial_chunk, nx)
        sh_y = min((shard_spatial // cy) * cy or cy, ny)
        sh_x = min((shard_spatial // cx) * cx or cx, nx)
        g = zarr.open_group(out_path, mode="w", zarr_format=3)
        g.create_array("lon", shape=(nx,), dtype="float32", chunks=(nx,)); g["lon"][:] = lon.astype(np.float32)
        g.create_array("lat", shape=(ny,), dtype="float32", chunks=(ny,)); g["lat"][:] = lat.astype(np.float32)
        for v in all_vars:                            # FULL final shape up front — workers never resize
            g.create_array(v, shape=(T, ny, nx), dtype="float32", chunks=(1, cy, cx),
                           shards=(1, sh_y, sh_x), fill_value=float("nan"))
        # attrs (days/vars/var_valid) intentionally NOT written yet — finalize LAST.
    else:                                             # resume: derive layout from the existing arrays
        g = zarr.open_group(out_path, mode="a")
        a0 = g[all_vars[0]]
        if int(a0.shape[0]) != T:
            raise ValueError(f"resume shape mismatch: existing T={a0.shape[0]} != len(keep_days)={T}")
        sh_y, sh_x = int(a0.shards[-2]), int(a0.shards[-1])

    rb = read_block or max(1024, sh_y, sh_x)
    rb_y = min(max(sh_y, (rb // sh_y) * sh_y or sh_y), ((ny + sh_y - 1) // sh_y) * sh_y)
    rb_x = min(max(sh_x, (rb // sh_x) * sh_x or sh_x), ((nx + sh_x - 1) // sh_x) * sh_x)

    ck_path = os.path.join(out_path, _CK_NAME)
    done: set = set()
    if resume and os.path.isfile(ck_path):
        with open(ck_path) as fh:
            done = {ln.strip() for ln in fh if ln.strip()}
    ck = open(ck_path, "a")

    units = [(t, day, v, ti, tj)
             for t, day in enumerate(keep_sorted)
             for v in all_vars if _src_present(orig, day_src, day, v, i0, j0)
             for ti in range(0, ny, rb_y) for tj in range(0, nx, rb_x)
             if f"{day}|{v}|{ti}|{tj}" not in done]
    remaining: Dict[tuple, int] = {}
    for (t, day, v, ti, tj) in units:
        remaining[(day, v)] = remaining.get((day, v), 0) + 1
    var_start: Dict[tuple, float] = {}
    arrs = {v: g[v] for v in all_vars}
    lock = threading.Lock()
    src_used = set()
    journal.event(event="build_start", engine="bulk", days=T, vars=all_vars,
                  units=len(units), resumed_units=len(done), tile=(rb_y, rb_x))

    def proc(u):
        t, day, v, ti, tj = u
        bi, bj = min(ti + rb_y, ny), min(tj + rb_x, nx)
        kind, gd = day_src[day]
        with lock:
            var_start.setdefault((day, v), time.perf_counter())
        if kind == "daily":                            # read ONLY this tile from the daily group
            block = np.asarray(gd[v][0, i0 + ti:i0 + bi, j0 + tj:j0 + bj])
        else:                                          # read ONLY this tile from the old delta BY day_index
            block = np.asarray(orig["group"][v][orig["day_index"][day], ti:bi, tj:bj])
        arrs[v][t, ti:bi, tj:bj] = block.astype(np.float32, copy=False)   # disjoint region — no locks
        with lock:
            src_used.add(kind)
            ck.write(f"{day}|{v}|{ti}|{tj}\n"); ck.flush(); os.fsync(ck.fileno())
            remaining[(day, v)] -= 1
            fin = (remaining[(day, v)] == 0)
            elapsed = round(time.perf_counter() - var_start[(day, v)], 3) if fin else None
        if fin:
            journal.event(event="var_done", day=day, var=v, source=kind, target_index=t,
                          elapsed_s=elapsed, status="done")

    try:
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                list(ex.map(proc, units))
        else:
            for u in units:
                proc(u)
    finally:
        ck.close()

    # ---- finalize metadata LAST (days written last of all) ----
    var_valid = {v: [bool(_src_present(orig, day_src, day, v, i0, j0)) for day in keep_sorted] for v in all_vars}
    gf = zarr.open_group(out_path, mode="a")
    gf.attrs["vars"] = list(all_vars)
    gf.attrs["var_valid"] = var_valid
    if reg:
        gf.attrs["region"] = [int(x) for x in reg]
    gf.attrs["layout"] = "time_lat_lon"
    gf.attrs["days"] = list(keep_sorted)               # FINALIZE days LAST
    if os.path.isfile(ck_path):
        os.remove(ck_path)
    journal.event(event="build_finalized", days=T, vars=all_vars)
    return {"sources": src_used, "day_src": day_src}


# --------------------------------------------------------------------------- the helper
def prune_delta(delta_path: str, out_path: str, keep_days: Sequence[str], *,
                compaction_lock_path: Optional[str] = None,
                source_daily: Optional[str] = None, source_delta: Optional[str] = None,
                base_days: Optional[Sequence[str]] = None,
                spatial_window_days: int = 31,
                audit_recent_window: Optional[dict] = None,
                region: Optional[tuple] = None,
                spatial_chunk: int = DELTA_SPATIAL_CHUNK, shard_spatial: int = DELTA_SHARD_SPATIAL,
                sample_n: int = 32, seed: int = 0, workers: int = 2,
                engine: str = "bulk", artifacts_dir: Optional[str] = None,
                resume: bool = False, read_block: Optional[int] = None) -> dict:
    """Rebuild a delta containing exactly ``keep_days`` (chronological) into the staging ``out_path``, then
    return an atomic-SWAP PLAN. Never swaps, never mutates ``delta_path`` / the sources.

    ``engine='bulk'`` (default): pre-created full-shape arrays + disjoint (day,var,tile) parallel writes +
    checkpoint/resume — production-scale path. ``engine='perday'``: original per-day writers (small scale).
    ``artifacts_dir``: writes ``prune_delta_progress.jsonl`` (fsync'd per event), ``prune_delta_error.json``
    on exception, and ``prune_plan.json`` ONLY on success.

    Fail-closed gates (any failure -> status='refused', NOTHING built/swapped):
    - ``keep_days`` non-empty and a subset of the original delta days;
    - **recent-window contiguity is ALWAYS decided by LOCAL recomputation** on the original delta (P4-S5
      §4.1). ``audit_recent_window`` may only corroborate — a mismatch refuses (fail-closed); it can never
      force-pass a locally-detected gap. A hole -> refuse.
    - **keep_days must preserve the ENTIRE active recent spatial window** — else refuse with
      ``missing_from_keep_window``.
    - **base coverage is MANDATORY whenever anything is dropped**: ``base_days=None`` is allowed ONLY for a
      pure rebuild/reorder (``dropped_days`` empty); any uncovered dropped day -> refuse.
    """
    if engine not in ("bulk", "perday"):
        raise ValueError(f"engine must be 'bulk' or 'perday', got {engine!r}")
    # P5-S3 §7.1b: a block build may be reading the live delta right now. Retargeting the
    # delta path under it would freeze wrong bytes into a NEW IMMUTABLE BLOCK -- the P4-S8a
    # failure with a permanent consequence. Refuse, never wait: a prune blocking for hours
    # looks like a hang, and the operator needs to reschedule.
    if compaction_lock_path:
        busy = refuse_if_compaction_running(compaction_lock_path, operation="prune_delta")
        if busy:
            return busy
    source_delta = source_delta or delta_path
    if os.path.abspath(out_path) == os.path.abspath(delta_path):
        raise ValueError("out_path must differ from delta_path (never build over the live delta)")
    if os.path.exists(out_path):
        # allowed ONLY to resume an interrupted bulk build (checkpoint present)
        if not (engine == "bulk" and resume and os.path.isfile(os.path.join(out_path, _CK_NAME))):
            raise ValueError(f"out_path already exists (use a fresh staging path, or engine='bulk' + "
                             f"resume=True with its checkpoint): {out_path}")

    journal = _Journal(artifacts_dir)
    try:
        orig = _open_delta_meta(delta_path)
        orig_days = set(orig["days"])
        keep_sorted = sorted(set(keep_days))
        dropped = sorted(orig_days - set(keep_sorted))

        def _refuse(reason, **extra):
            journal.event(event="refused", reason=reason)
            return {"status": "refused", "reason": reason, "delta_path": delta_path, "out_path": out_path,
                    "keep_days": keep_sorted, "dropped_days": dropped, "production_mutation": False,
                    "swap_performed": False, **extra}

        if not keep_sorted:
            return _refuse("keep_days is empty — refusing to build an empty delta")
        missing_keep = [d for d in keep_sorted if d not in orig_days]
        if missing_keep:
            return _refuse(f"keep_days contains days not in the source delta: {missing_keep}")

        # recent-window gate — LOCAL recompute always decides (never a caller override).
        rw = recent_window_contiguous(orig["days"], spatial_window_days)
        if audit_recent_window is not None:
            a_contig = audit_recent_window.get("recent_window_contiguous")
            a_missing = sorted(audit_recent_window.get("missing_in_window", []) or [])
            if a_contig != rw["contiguous"] or a_missing != sorted(rw["missing"]):
                return _refuse("audit recent-window disagrees with local recomputation — refusing "
                               "(fail-closed)", recent_window=rw, audit_recent_window=audit_recent_window)
        if not rw["contiguous"]:
            return _refuse("recent spatial window has holes — repair before pruning (P4-S4 §4.1)",
                           recent_window=rw)

        # keep-preserves-window gate — dropping a day inside the active window would break bbox/POST after
        # the P4-S8 swap even if base covers it (base is bbox-hostile). Fail-closed (P4-S4 §5).
        required_window = _calendar_range(rw["window_start"], rw["window_end"])
        keep_set = set(keep_sorted)
        missing_from_keep_window = [d for d in required_window if d not in keep_set]
        if missing_from_keep_window:
            return _refuse("keep_days drops day(s) inside the active recent spatial window — would break "
                           "the spatial-window policy after swap; refuse (repair the keep-set)",
                           recent_window=rw, missing_from_keep_window=missing_from_keep_window)

        # base-coverage gate — MANDATORY when dropping anything.
        if dropped:
            if base_days is None:
                return _refuse("base_days is REQUIRED when dropping days (mandatory base-coverage gate); "
                               "base_days=None is allowed only for a pure rebuild/reorder with no dropped days")
            uncovered = [d for d in dropped if d not in set(base_days)]
            if uncovered:
                return _refuse("dropped days not covered by base (compaction must run first): "
                               f"{uncovered}", base_uncovered=uncovered)

        journal.event(event="gates_passed", engine=engine, keep=len(keep_sorted),
                      dropped=dropped, window=[rw["window_start"], rw["window_end"]])

        # ---- rebuild new_delta chronologically ----
        if engine == "bulk":
            build = _bulk_build(orig, out_path, keep_sorted, source_daily=source_daily, region=region,
                                spatial_chunk=spatial_chunk, shard_spatial=shard_spatial, workers=workers,
                                read_block=read_block, journal=journal, resume=resume)
            src_used = build["sources"]
        else:                                          # perday (legacy; small-scale/tests only)
            lon = np.asarray(orig["group"]["lon"][:]); lat = np.asarray(orig["group"]["lat"][:])
            src_used = set()
            for day in keep_sorted:                    # CHRONOLOGICAL write order
                t0 = time.perf_counter()
                if source_daily and group_exists(source_daily, day):
                    append_to_delta(source_daily, out_path, day, region=region,
                                    spatial_chunk=spatial_chunk, shard_spatial=shard_spatial,
                                    workers=workers)
                    src_used.add("daily"); kind = "daily"
                else:
                    data = _read_delta_day(orig, day)
                    _append_prebuilt_day(out_path, day, data, orig["ny"], orig["nx"], lon, lat,
                                         orig["region"], spatial_chunk, shard_spatial)
                    src_used.add("delta"); kind = "delta"
                journal.event(event="day_done", day=day, source=kind,
                              elapsed_s=round(time.perf_counter() - t0, 3), status="done")

        journal.event(event="validation_start", days=len(keep_sorted), sample_n=sample_n)
        validation = _validate(out_path, keep_sorted, orig, source_daily, region, sample_n, seed,
                               spatial_window_days, journal)
        journal.event(event="validation_end", all_ok=validation["all_ok"])

        status = "ok" if validation["all_ok"] else "invalid"
        result = {
            "status": status,
            "delta_path": delta_path,
            "out_path": out_path,
            "engine": engine,
            "source": ("+".join(sorted(src_used)) if src_used else None),
            "keep_days": keep_sorted,
            "dropped_days": dropped,
            "recent_window": rw,
            "validation": validation,
            "swap_plan": {
                "action": "atomic_swap",
                "from": out_path,
                "to": delta_path,
                "backup": delta_path + ".pre-prune",
                "performed": False,
                "note": ("P4-S6 returns the plan ONLY — no swap performed. A later phase (P4-S7/S8/S9) does "
                         "the atomic swap (keep <to>.pre-prune until /healthz confirms) then cube refresh."),
            },
            "production_mutation": False,
            "swap_performed": False,
        }
        if artifacts_dir and status == "ok":           # the tool writes the plan ONLY on success
            with open(os.path.join(artifacts_dir, "prune_plan.json"), "w") as fh:
                json.dump(result, fh, indent=2, sort_keys=True, default=str)
            journal.event(event="plan_written", path=os.path.join(artifacts_dir, "prune_plan.json"))
        return result
    except Exception as exc:                           # SIGKILL can't be caught — the journal covers that
        journal.event(event="error", error=f"{type(exc).__name__}: {exc}")
        _write_error(artifacts_dir, exc, {"delta_path": delta_path, "out_path": out_path,
                                          "keep_days": sorted(set(keep_days)), "engine": engine,
                                          "hint": "prune_delta_progress.jsonl records completed units; "
                                                  "engine='bulk' supports resume=True"})
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(), "delta_path": delta_path, "out_path": out_path,
                "engine": engine, "production_mutation": False, "swap_performed": False}
    finally:
        journal.close()


# --------------------------------------------------------------------------- validation (POINT-WISE)
def _validate(out_path: str, keep_sorted: List[str], orig: dict, source_daily: Optional[str],
              region: Optional[tuple], sample_n: int, seed: int, spatial_window_days: int,
              journal: Optional[_Journal] = None) -> dict:
    """Validate the rebuilt delta: day set == keep_sorted (sorted, unique), latest == max(days); the rebuilt
    delta's own recent spatial window is contiguous; var_valid preserved (incl. absent-var); sample parity
    per kept day (float32, NaN-aware) vs source.

    **POINT-WISE (S9 root-cause fix):** samples are read as per-cell scalars from BOTH sides — this
    function never materializes a full (ny, nx) slab. The old full-slab reads (~2.6 GB per var-day at
    production scale) were what got the VM24 step-3 run OOM-killed after a successful build."""
    ng = zarr.open_group(out_path, mode="r")                 # READ-ONLY
    new_days = list(ng.attrs["days"])
    new_index = {d: i for i, d in enumerate(new_days)}
    new_vv = {v: list(x) for v, x in dict(ng.attrs.get("var_valid", {})).items()}
    new_vars = list(ng.attrs.get("vars", []))

    day_set_ok = (new_days == keep_sorted)                   # written chronologically -> already sorted
    unique = (len(new_days) == len(set(new_days)))
    is_sorted = (new_days == sorted(new_days))
    latest_ok = (max(new_days) == keep_sorted[-1]) if new_days else False
    new_rw = recent_window_contiguous(new_days, spatial_window_days)   # post-build window invariant
    recent_window_ok = new_rw["contiguous"]

    reg = region or orig["region"]
    i0, j0 = (reg[0], reg[2]) if reg else (0, 0)
    rng = np.random.default_rng(seed)
    var_valid_ok = True
    parity_ok = True
    parity_checked = 0
    for day in keep_sorted:
        if day not in new_index:                             # day_set_ok already False; skip sampling
            continue
        t = new_index[day]
        daily_mode = bool(source_daily) and group_exists(source_daily, day)
        gd = zarr.open_group(group_path(source_daily, day), mode="r") if daily_mode else None
        ts = orig["day_index"].get(day)
        for v in new_vars:
            if daily_mode:
                src_present = _daily_data_var(gd, v, i0, j0,
                                              int(ng[v].shape[-2]), int(ng[v].shape[-1])) \
                    if v in ng else (v in gd)         # same data-var filter as the build (Codex note 1)
            else:
                vv = orig["var_valid"].get(v)
                src_present = (v in orig["group"]) and (bool(vv[ts]) if vv is not None else True) \
                    if ts is not None else False
            # var_valid parity (absent var must be recorded False)
            if v in new_vv:
                if bool(new_vv[v][t]) != src_present:
                    var_valid_ok = False
            elif src_present:
                var_valid_ok = False
            if not src_present:
                continue
            arr_new = ng[v]
            ny, nx = int(arr_new.shape[1]), int(arr_new.shape[2])
            n = min(sample_n, ny * nx)
            flat = rng.choice(ny * nx, size=n, replace=False)
            ii, jj = np.divmod(flat, nx)
            for a, b in zip(ii.tolist(), jj.tolist()):       # per-cell scalar reads — never a full slab
                got = np.float32(arr_new[t, a, b])
                if daily_mode:
                    exp = np.float32(gd[v][0, i0 + a, j0 + b])
                else:
                    exp = np.float32(orig["group"][v][ts, a, b])
                if not (got == exp or (np.isnan(got) and np.isnan(exp))):
                    parity_ok = False
                parity_checked += 1
        if journal is not None:
            journal.event(event="validation_day", day=day, status="done")
    all_ok = (day_set_ok and unique and is_sorted and latest_ok and recent_window_ok
              and var_valid_ok and parity_ok)
    return {
        "day_set_ok": day_set_ok, "unique": unique, "sorted_chronological": is_sorted,
        "latest": (max(new_days) if new_days else None), "latest_ok": latest_ok,
        "recent_window_ok": recent_window_ok, "recent_window_missing": new_rw["missing"],
        "var_valid_ok": var_valid_ok, "parity_ok": parity_ok, "parity_cells_checked": parity_checked,
        "new_day_count": len(new_days), "all_ok": all_ok,
    }
