"""P5-S6 — block-size / cadence sizing harness.  **LOCAL AND SYNTHETIC ONLY.**

No VM24, no SSH, no `GHRSST_*` store, no cron, no PM2, no production path. Every fixture is a
`tempfile` tree and is removed afterwards.

## The four labels, and why they are not decoration

| label | means |
|---|---|
| `MODEL` | arithmetic at production geometry. No I/O. May be wrong; is never *evidence*. |
| `PRODUCTION_GEOMETRY_FIXTURE` | measured on a store whose **chunk/shard geometry** matches production while its spatial extent does not. The *shape* of the cost is real; the magnitude is not production's. |
| `LOCAL` | measured here, on this machine, at fixture scale. |
| `DEFERRED` | needs VM24 read-only validation. Carries a reason and **no number**. |

A metric that cannot be measured locally is `DEFERRED`. It is never an invented PASS, and this
harness never emits a production G3 / H7 verdict — `build_report()` returns `NOT_ADJUDICATED`
for both, by construction.

## Why the fixture cannot be production-sized

One day of one variable at 17999 × 36000 float32 is **2.6 GB**; a 90-day 3-var block is
**700 GB**. Nothing here materializes a full slab, and `assert_fixture_bounds()` refuses any
request that would. What the fixture *does* preserve is the chunk and shard geometry, which is
what the per-segment call cost actually depends on.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import re
import json
import math
import os
import shutil
import sys
import tempfile
import time
from typing import Dict, List, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                "tests"))

# --------------------------------------------------------------------------- production geometry
NY = 17999
NX = 36000
VARS = ("sst", "sst_anomaly", "sea_ice")
BYTES_PER_CELL = 4                                   # float32
LEGACY_TIME_CHUNK = 90
DELTA_GEOMETRY = {"time_chunk": 1, "spatial_chunk": 256, "shard_spatial": 256}
SHARD_SPATIAL = 128                                  # base blocks, §10.6

#: §10.1 constants.
SPATIAL_WINDOW_DAYS = 31
DELTA_BUFFER_DAYS = 3
MAX_DAYS = 366

#: §10.3 modelled volumes.
BASE_GB_PER_DAY = 1.57
DELTA_GB_PER_DAY = 1.09

#: The horizons H7 must report snapshot-open cost at (§10.5).
SNAPSHOT_HORIZONS = (5, 13, 41, 82, 122)
#: The delta spans H5 must cover.
H5_DELTA_SPANS = (31, 45, 64, 90, 124)

#: The probe point for every read measurement (matches bench_p5_segmented_read).
LON, LAT = 100.0, 0.0

FIXTURE_ANCHOR_DAY = "2026-06-27"
FIXTURE_ANCHOR_NOTE = (
    "Committed historical FIXTURE anchor, used so the block grid is reproducible across runs. "
    "It describes this harness only, and says nothing about the state of any live store."
)

#: A fixture may not exceed this many cells in total. 17999x36000 is ~648M cells for ONE day of
#: ONE variable; this ceiling is three orders of magnitude below that, and is enforced rather
#: than documented.
MAX_FIXTURE_CELLS = 4_000_000


class FixtureTooLarge(Exception):
    """A fixture request that would materialize an unacceptable slab."""


def assert_fixture_bounds(*, ny: int, nx: int, days: int) -> None:
    cells = ny * nx * days
    if cells > MAX_FIXTURE_CELLS:
        raise FixtureTooLarge(
            f"{ny}x{nx}x{days} = {cells:,} cells exceeds the {MAX_FIXTURE_CELLS:,} ceiling. "
            f"Production geometry is modelled, never materialized: one production day of one "
            f"variable is {NY * NX * BYTES_PER_CELL / 1e9:.1f} GB.")


def production_geometry_fixture_shape() -> Dict[str, int]:
    """The fixture: production CHUNK geometry, a spatial extent that fits in a temp dir."""
    return {"ny": 256, "nx": 512, "days": 30}


# --------------------------------------------------------------------------- §10 model
def _model(**kw) -> dict:
    return {"label": "MODEL", **kw}


def model_delta_span(*, s: int, c: int) -> dict:
    """§10.1 — driven by cadence, not block size."""
    return _model(s=s, c=c,
                  delta_span_max=SPATIAL_WINDOW_DAYS + c + DELTA_BUFFER_DAYS,
                  delta_gb=round((SPATIAL_WINDOW_DAYS + c + DELTA_BUFFER_DAYS)
                                 * DELTA_GB_PER_DAY, 1))


def model_write_amplification(*, s: int, c: int) -> dict:
    """§10.2 — a tail block republished every C days until it seals at S."""
    amp = (s / c + 1) / 2
    folds_per_year = 365.25 / c
    return _model(s=s, c=c, amplification=amp, folds_per_year=round(folds_per_year, 1),
                  gb_written_per_year=round(amp * 365.25 * BASE_GB_PER_DAY, 0))


def model_segments_per_request(*, s: int, days: int = MAX_DAYS) -> dict:
    """§10.3 — base segments a request spans. The delta tier is counted separately ("+delta"
    in the spec table), so this is `ceil(days/S)` and not `+1`."""
    return _model(s=s, days=days, segments=math.ceil(days / s), plus_delta_tier=1)


def model_segment_horizons() -> dict:
    """§10.5 — legacy monolith plus blocks, at +3 / +5 / +10 years."""
    out: Dict[str, dict] = {"label": "MODEL"}
    for s in (30, 45, 90):
        # ceil, not floor: a partially-filled tail block is still a segment the snapshot opens,
        # which is what the horizon numbers are for. floor gave 40 at S=90/+10y where the spec
        # says 41, and the tail block is exactly the difference.
        out[f"S={s}"] = {f"+{y}y": math.ceil(round(y * 365.25) / s) for y in (3, 5, 10)}
    return out


def model_file_counts(*, s: int) -> dict:
    """§10.6 — shard files per block at production geometry."""
    shards_per_var = math.ceil(NY / SHARD_SPATIAL) * math.ceil(NX / SHARD_SPATIAL)
    per_block = shards_per_var * len(VARS)
    return _model(s=s, shards_per_var=shards_per_var, files_per_block=per_block,
                  files_per_day=round(per_block / s, 1),
                  legacy_monolith_files=round(1273 * per_block / s),
                  base_files_at_plus_10y=round((1273 + 3653) * per_block / s))


def model_peak_temp_disk_gb(*, s: int) -> float:
    """§10.3 — rebuilding one block, not the 2 TB monolith."""
    return round(s * BASE_GB_PER_DAY, 1)


def model_decompressed_bytes_per_var(*, s: int, days: int = MAX_DAYS) -> dict:
    """§10.4 — chunk (S', 8, 8) with S' = min(90, S). Bytes are NOT invariant to S."""
    s_prime = min(LEGACY_TIME_CHUNK, s)
    chunks = math.ceil(days / s_prime)
    per_chunk = s_prime * 8 * 8 * BYTES_PER_CELL
    return _model(s=s, days=days, chunks=chunks, bytes_per_chunk=per_chunk,
                  bytes_per_var=chunks * per_chunk)


def model_all() -> dict:
    """Every model number, with no filesystem access at all."""
    return {
        "label": "MODEL",
        "delta_span": {f"C={c}": model_delta_span(s=90, c=c) for c in (30, 45, 90)},
        "write_amplification": {f"S={s},C={c}": model_write_amplification(s=s, c=c)
                                for s in (30, 45, 90) for c in (30, 45, 90) if c <= s},
        "segments_per_request": {f"S={s}": model_segments_per_request(s=s) for s in (30, 45, 90)},
        "horizons": model_segment_horizons(),
        "file_counts": {f"S={s}": model_file_counts(s=s) for s in (30, 45, 90)},
        "peak_temp_disk_gb": {f"S={s}": model_peak_temp_disk_gb(s=s) for s in (30, 45, 90)},
        "decompressed_bytes": {f"S={s}": model_decompressed_bytes_per_var(s=s)
                               for s in (30, 45, 90)},
    }


# --------------------------------------------------------------------------- measurement
def _digest_tree(root: str) -> Dict[str, str]:
    """Content digests. mtime and size are NOT rewrite evidence (the P5-S0 rule)."""
    out: Dict[str, str] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = os.path.join(dirpath, name)
            h = hashlib.sha256()
            with open(p, "rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
            out[os.path.relpath(p, root)] = h.hexdigest()
    return out


class _OpenSpy:
    """Records which segment stores were actually opened during a read.

    This is the point where a counter stops measuring itself: the harness computes which
    segments a request *should* touch from the day->segment map, and this records which ones
    zarr *did* open. The two are compared."""

    def __init__(self):
        self.opened: List[str] = []
        self._real = None

    def __enter__(self):
        import zarr
        self._real = zarr.open_group

        def spy(path, *a, **k):
            self.opened.append(str(path))
            return self._real(path, *a, **k)

        zarr.open_group = spy
        return self

    def __exit__(self, *exc):
        import zarr
        zarr.open_group = self._real


def _build_segments(tmp: str, *, s: int, days: int, shape: Dict[str, int]) -> List[str]:
    """One store per block of `s` days, at production CHUNK geometry."""
    import p5_fixtures as fx
    assert_fixture_bounds(ny=shape["ny"], nx=shape["nx"], days=days)
    start = FIXTURE_ANCHOR_DAY
    all_days = fx.days_from(start, days)
    paths = []
    for i in range(0, days, s):
        chunk_days = all_days[i:i + s]
        path = os.path.join(tmp, f"block_{i // s:03d}.zarr")
        fx.build_block(path, chunk_days, ny=shape["ny"], nx=shape["nx"],
                       time_chunk=min(LEGACY_TIME_CHUNK, s))
        paths.append(path)
    return paths


def measure_segment_calls(*, s: int, days: int = 90,
                          shape: Optional[Dict[str, int]] = None,
                          open_only_first: bool = False) -> dict:
    """How many segment stores a `days`-long read actually opens, at block size `s`."""
    shape = shape or {"ny": 64, "nx": 64, "days": days}
    shape = {**shape, "days": days}
    tmp = tempfile.mkdtemp(prefix="p5s6-calls-")
    try:
        paths = _build_segments(tmp, s=s, days=days, shape=shape)
        reported = len(paths)                     # the harness's own tally
        import zarr
        with _OpenSpy() as spy:
            chunk_keys = 0
            # `open_only_first` reads a window that lands inside ONE block while `reported`
            # stays the block count. Without a case where the two MUST differ, `observed ==
            # reported` holds no matter where `observed` came from -- which is how a mutation
            # replacing the observation with the tally survived.
            for path in (paths[:1] if open_only_first else paths):
                g = zarr.open_group(path, mode="r")
                chunk_keys += int(g["sst"][:, 0, 0].size)
        observed_paths = sorted({p for p in spy.opened if p.startswith(tmp)})
        observed = len(observed_paths)
        return {"label": "PRODUCTION_GEOMETRY_FIXTURE",
                "measures": ("stores this harness opened directly; NOT the grouped read path. "
                             "No manifest, no SegmentedCubeStore, no point_series()."),
                "s": s, "days": days,
                "reported_segment_calls": reported,
                "observed_segment_calls": observed,
                # The paths themselves, so a test can check the count against something the
                # harness cannot fabricate. Asserting only `reported == observed` passes if the
                # observation is quietly replaced by the tally -- which is the whole failure
                # mode this measurement exists to avoid.
                "observed_open_paths": [os.path.relpath(p, tmp) for p in observed_paths],
                "open_only_first": open_only_first,
                "observed_chunk_keys": chunk_keys,
                "fixture_shape": shape,
                "note": "chunk geometry is production's; spatial extent is not"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def measure_snapshot_open(*, segments: int) -> dict:
    """Wall time and observed metadata reads to build a snapshot over `segments` blocks."""
    import p5_fixtures as fx
    tmp = tempfile.mkdtemp(prefix="p5s6-snap-")
    try:
        shape = {"ny": 16, "nx": 16}
        assert_fixture_bounds(ny=shape["ny"], nx=shape["nx"], days=segments)
        for i in range(segments):
            fx.build_block(os.path.join(tmp, f"b{i:04d}.zarr"),
                           fx.days_from(FIXTURE_ANCHOR_DAY, 1), ny=shape["ny"], nx=shape["nx"])
        import zarr
        with _OpenSpy() as spy:
            t0 = time.perf_counter()
            for i in range(segments):
                zarr.open_group(os.path.join(tmp, f"b{i:04d}.zarr"), mode="r")
            wall_ms = (time.perf_counter() - t0) * 1000.0
        return {"label": "LOCAL",
                "measures": "raw zarr.open_group cost, NOT O2 snapshot-open cost",
                "not_measured": ("SegmentedCubeStore(manifest_dir): manifest validation, "
                                 "segment inspection, metadata binding, snapshot build"),
                "segments": segments, "wall_ms": round(wall_ms, 3),
                "observed_metadata_reads": len([p for p in spy.opened if p.startswith(tmp)]),
                "ms_per_segment": round(wall_ms / max(segments, 1), 4)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def measure_metadata_scan(*, segments: int) -> dict:
    """`os.walk` cost over a block tree — what backup, `du` and the disk precheck all pay."""
    import p5_fixtures as fx
    tmp = tempfile.mkdtemp(prefix="p5s6-walk-")
    try:
        for i in range(segments):
            fx.build_block(os.path.join(tmp, f"b{i:04d}.zarr"),
                           fx.days_from(FIXTURE_ANCHOR_DAY, 1), ny=16, nx=16)
        t0 = time.perf_counter()
        files = sum(len(f) for _d, _s, f in os.walk(tmp))
        wall_ms = (time.perf_counter() - t0) * 1000.0
        return {"label": "LOCAL", "segments": segments, "files": files,
                "wall_ms": round(wall_ms, 3),
                "ms_per_1k_files": round(wall_ms / max(files, 1) * 1000, 4)}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _tail_rebuild_probe(*, s: int, c: int, touch_without_changing: bool = False) -> dict:
    """Rebuilding a tail block after `c` more days, carrying the predecessor's days forward.

    Rewrite evidence is a **content digest**. A file whose mtime moved and whose bytes did not
    is not a rewrite, and `touch_without_changing` exists to prove this harness agrees."""
    import p5_fixtures as fx
    tmp = tempfile.mkdtemp(prefix="p5s6-rebuild-")
    try:
        shape = {"ny": 32, "nx": 32}
        first = min(c, s)
        v1 = os.path.join(tmp, "tail_v1.zarr")
        fx.build_block(v1, fx.days_from(FIXTURE_ANCHOR_DAY, first), **shape)
        before = _digest_tree(v1)

        if touch_without_changing:
            now = time.time() + 10
            for rel in before:
                os.utime(os.path.join(v1, rel), (now, now))
            after_touch = _digest_tree(v1)
            touched_only = sum(1 for k, v in after_touch.items() if before.get(k) != v)
        else:
            touched_only = 0

        carried = min(first, s)
        total = min(first + c, s)
        t0 = time.perf_counter()
        v2 = os.path.join(tmp, "tail_v2.zarr")
        fx.build_block(v2, fx.days_from(FIXTURE_ANCHOR_DAY, total), **shape)
        wall_ms = (time.perf_counter() - t0) * 1000.0
        after = _digest_tree(v2)
        rewritten = sum(1 for k, v in after.items() if before.get(k) != v)

        return {"label": "PROBE",
                "reason": ("v2 is rebuilt from fixtures, not read from the predecessor block. "
                           "`carried_forward_days` is arithmetic, not read evidence, and there "
                           "is no source map. Real carry-forward needs the O2 materialization "
                           "strategy, which does not exist yet."),
                "s": s, "c": c,
                "rewrite_evidence": "sha256-content-digest",
                "carried_forward_days": carried, "days_in_new_tail": total,
                "rewritten_files": rewritten,
                "rewritten_files_touched_only": touched_only,
                "rebuild_wall_ms": round(wall_ms, 2),
                "modelled_amplification": model_write_amplification(s=s, c=c)["amplification"]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def measure_tail_rebuild(*, s: int, c: int) -> dict:
    """DEFERRED — and therefore carrying NO numbers. Runs nothing.

    It used to accept `touch_without_changing` and ignore it, which is worse than not accepting
    it: a caller could pass the flag and believe a probe had run. The content-digest guard lives
    in `_tail_rebuild_probe()`, which does run the fixture work — a file whose mtime moved and
    whose bytes did not is not a rewrite — but whose timings are deliberately not published
    here, because a `rebuild_wall_ms` in a DEFERRED row reads as evidence it is not."""
    return {
        "label": "DEFERRED", "s": s, "c": c,
        "reason": ("no carry-forward was measured: v2 is rebuilt from fixtures, never read from "
                   "the predecessor block, and there is no source map. Real carry-forward needs "
                   "the O2 materialization strategy, which does not exist yet. The probe's "
                   "timings are deliberately NOT published here -- a wall-clock number in a "
                   "DEFERRED row reads as evidence it is not."),
        "rewrite_evidence": "sha256-content-digest (probe only)",
    }


# ------------------------------------------------- items 1 & 2: the REAL O2 read path
def _real_segmented_fixture(tmp: str, *, segments: int, total_days: int, ny: int, nx: int):
    """A published manifest and its blocks, via the P5-S2 bench builder.

    Reused rather than reimplemented: `bench_p5_segmented_read._build` is the fixture the
    grouped-read work already trusts, so this measurement and that one describe the same store.
    """
    import bench_p5_segmented_read as segread
    assert_fixture_bounds(ny=ny, nx=nx, days=total_days)
    return segread._build(tmp, total_days, segments, ny, nx)


def measure_grouped_read(*, segments: int, total_days: int, request_days: int,
                         request_within_one_segment: bool = False,
                         ny: int = 24, nx: int = 24,
                         _negative_control: bool = False) -> dict:
    """`SegmentedCubeStore.point_series()` against a real manifest, observed from outside.

    The spy replaces `zarr.open_group` **in the implementation's own module namespace**
    (`store.segmented_cube`), so nothing the harness does on its own can satisfy it. Earlier
    rounds looped over `zarr.open_group()` here and called the result a read-path measurement;
    it was a measurement of the loop.

    `request_within_one_segment` is the falsifiable case: the manifest declares N segments and
    the request lands inside one, so `reported` and `observed` MUST differ. Without it,
    observed == reported holds no matter where either number came from."""
    from store import segmented_cube as sc
    from store import time_cube as tc
    tmp = tempfile.mkdtemp(prefix="p5s6-grouped-")
    try:
        root, days = _real_segmented_fixture(tmp, segments=segments, total_days=total_days,
                                             ny=ny, nx=nx)
        if request_within_one_segment:
            per = max(total_days // segments, 1)
            requested = days[:min(request_days, per)]
        else:
            requested = days[-request_days:]

        probe = sc.SegmentedCubeStore(root)
        manifest_segments = len(probe._meta.stores)

        # The sentinel is written BEFORE the measured read, so parity is checked on the ROWS the
        # measurement actually returned. Writing it afterwards and verifying with a second
        # single-day read proved only that some other read could see it -- not that the request
        # under measurement served those bytes.
        import numpy as np
        probe_day = requested[len(requested) // 2]
        seg_path, day_idx = None, None
        for sp, meta in zip(probe._meta.stores, probe._meta.segment_metas):
            days_here = list(getattr(meta, "days", []))
            if probe_day in days_here:
                seg_path, day_idx = sp.path, days_here.index(probe_day)
                break
        sentinel = -12345.5
        if seg_path is not None:
            g = tc.zarr.open_group(seg_path, mode="a")
            g["sst"][day_idx] = np.full_like(np.asarray(g["sst"][day_idx]), sentinel)

        # A snapshot taken AFTER the write, so the measured read serves the sentinel bytes.
        store = sc.SegmentedCubeStore(root)
        expected = {sp for sp, meta in zip(store._meta.stores, store._meta.segment_metas)
                    if set(getattr(meta, "days", [])) & set(requested)}
        store.point_series(LON, LAT, requested, list(VARS))          # warm

        real_open, opened = tc.zarr.open_group, []

        def spy(path, *a, **kw):
            opened.append(str(path))
            return real_open(path, *a, **kw)

        tc.zarr.open_group = spy
        try:
            t0 = time.perf_counter()
            rows = store.point_series(LON, LAT, requested, list(VARS))   # THE measured read
            read_ms = (time.perf_counter() - t0) * 1000.0
        finally:
            tc.zarr.open_group = real_open

        touched = {p for p in opened if p.startswith(tmp)}

        # AFTER the measured read, overwrite the same cell with a different value. Now the rows
        # the measurement returned and anything read later disagree by construction -- so a
        # parity check that quietly re-reads instead of using those rows is detectable. Without
        # this, "check `rows`" and "issue a fresh read" are observationally identical and a
        # mutation swapping one for the other survives.
        post_read_value = sentinel - 1.0
        if seg_path is not None:
            g2 = tc.zarr.open_group(seg_path, mode="a")
            g2["sst"][day_idx] = np.full_like(np.asarray(g2["sst"][day_idx]), post_read_value)

        # `_negative_control` compares against a value never written, so the check must report a
        # mismatch. `parity_ok: True` is unfalsifiable without it -- disabling the comparison
        # entirely also yields True, which is how a mutation of exactly that survived.
        expected_cell = sentinel + (1.0 if _negative_control else 0.0)
        mismatches = []
        if seg_path is None:
            mismatches.append({"day": probe_day, "error": "no segment claims this day"})
        else:
            measured = [r for r in rows if r.get("date") == probe_day]
            if not measured:
                mismatches.append({"day": probe_day,
                                   "error": "the measured request did not return the probe day"})
            elif abs(float(measured[0]["sst"]) - expected_cell) > 1e-6:
                mismatches.append({"day": probe_day, "expected_raw_cell": expected_cell,
                                   "got": measured[0]["sst"],
                                   "source": "the measured request's own row"})
        parity_note = ("the cell was overwritten after the measured read, so a check that "
                       "re-reads instead of using the returned rows sees "
                       f"{post_read_value} and fails")

        return {
            "label": "LOCAL_SYNTHETIC",
            "used_manifest": True,
            "read_path": "SegmentedCubeStore.point_series",
            "spy_target": "store.time_cube.zarr.open_group",
            "manifest_segments": manifest_segments,
            "expected_segments_for_request": len(expected),
            # No `or len(expected)` fallback. It substituted the EXPECTED count whenever the
            # observation was empty, so detaching the spy entirely still produced a plausible
            # number -- a fallback that turns "we saw nothing" into "we saw what we predicted".
            # ZERO is the correct answer for a warm read, and that is the point: P5-S2 §6.2
            # guarantees the read path opens nothing -- it reads through metadata captured at
            # snapshot time. So `open_group` cannot count segments touched by a read, and the
            # earlier `or len(expected)` fallback was hiding that by substituting the prediction.
            "observed_segments_touched": len({os.path.relpath(p, tmp).split(os.sep)[0]
                                              for p in touched}),
            "opens_during_read_must_be_zero": True,
            "segments_touched_observable": {
                "label": "DEFERRED",
                "reason": ("counting segments a READ touches needs a chunk-level observable "
                           "(a counting store around each segment), not `open_group`, which "
                           "§6.2 guarantees is never called during a read. Not built here."),
            },
            "observed_open_calls": len(opened),
            "returned_days": len(rows),
            "requested_days": len(requested),
            "read_ms": round(read_ms, 3),
            "raw_cell_canary_ok": not mismatches,
            "raw_cell_canary_is_falsifiable": True,
            "raw_cell_canary_note": parity_note,
            "raw_cell_canary_scope": ("ONE probe day, ONE variable (sst), ONE cell. It shows the "
                                      "measured request serves real bytes; it is NOT parity over "
                                      "all returned rows or all variables."),
            "raw_cell_canary_oracle": ("a sentinel written directly into the block's raw cells via "
                              "zarr BEFORE the measured read, then checked on that "
                              "request's own returned row"),
            "raw_cell_canary_mismatches": mismatches,
            "fixture_geometry": {"ny": ny, "nx": nx, "total_days": total_days,
                                 "segments": segments},
            "note": "LOCAL SYNTHETIC. Not VM24 evidence.",
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def measure_snapshot_construction(*, segments: int, ny: int = 16, nx: int = 16,
                                  days_per_segment: int = 30, repeats: int = 5) -> dict:
    """The real `SegmentedCubeStore(manifest_dir)` cost: validation, inspection, binding, build.

    This is what a generation change and a process start actually pay. `measure_snapshot_open`
    remains beside it and is explicitly labelled raw `zarr.open_group` cost, because the two
    were conflated once and the difference is most of the work."""
    from store import segmented_cube as sc
    tmp = tempfile.mkdtemp(prefix="p5s6-construct-")
    try:
        # One day per segment made 5/13/41/82/122 into horizons of ONE-day blocks, which
        # understates day-metadata and inspection cost -- the very thing this measures. Blocks
        # now carry a representative day count, and the artifact records it.
        total = max(segments * days_per_segment, 1)
        root, _days = _real_segmented_fixture(tmp, segments=segments, total_days=total,
                                              ny=ny, nx=nx)
        sc.SegmentedCubeStore(root)                                   # warm the cache
        samples = []
        for _ in range(repeats):
            t0 = time.perf_counter()
            sc.SegmentedCubeStore(root)
            samples.append((time.perf_counter() - t0) * 1000.0)
        samples.sort()
        p50 = samples[len(samples) // 2]
        p95 = samples[min(int(len(samples) * 0.95), len(samples) - 1)]
        return {
            "label": "LOCAL_SYNTHETIC",
            "measures": "SegmentedCubeStore(manifest_dir) construction",
            "includes": ["manifest_validation", "segment_inspection", "metadata_binding",
                         "snapshot_build"],
            "segments": segments,
            "p50_ms": round(p50, 3), "p95_ms": round(p95, 3),
            "fixture_geometry": {"ny": ny, "nx": nx, "segments": segments,
                                 "days_per_segment": days_per_segment, "total_days": total},
            "cache_assumption": "warm: the tree was constructed once before sampling",
            "note": "LOCAL SYNTHETIC. Not VM24 evidence; adjudicates no gate.",
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------- item 3: H5 crossing latency
#: Provisional O2 design target (operator decision). CONFIGURABLE, never hardcoded at a call
#: site: production adjudication still waits on VM24's G3/H7/G16.
DEFAULT_S = 90
DEFAULT_C = 30


def _percentiles(samples: List[float]) -> Dict[str, float]:
    s = sorted(samples)
    return {"p50_ms": round(s[len(s) // 2], 3),
            "p95_ms": round(s[min(int(len(s) * 0.95), len(s) - 1)], 3)}


def measure_h5_span(*, delta_span: int, ny: int = 16, nx: int = 16, repeats: int = 7,
                    s: int = DEFAULT_S, _negative_control: bool = False) -> dict:
    """Crossing-range latency at one delta span, through a real `TieredCube`.

    Base and delta come from ONE oracle: the delta days are written with values derived from
    the same generator the base block used, then the overlapping days are given a distinct
    delta value so precedence is observable. Two independently generated fixtures would make
    any disagreement meaningless -- it would not be a bug, just two different datasets."""
    import numpy as np
    import p5_fixtures as fx
    from store import segmented_cube as sc
    from store.tiered_cube import TieredCube
    from store.time_cube import TimeCubeStore

    tmp = tempfile.mkdtemp(prefix=f"p5s6-h5-{delta_span}-")
    try:
        base_days_n = max(s, 30)
        total = base_days_n + delta_span
        assert_fixture_bounds(ny=ny, nx=nx, days=total)
        root, all_days = _real_segmented_fixture(
            tmp, segments=max(total // max(s, 1), 1), total_days=total, ny=ny, nx=nx)

        # The delta holds the newest `delta_span` days -- the same DAYS the base tail holds, so
        # the overlap is where precedence is observable.
        delta_days = all_days[-delta_span:]
        delta_path = os.path.join(tmp, "delta.zarr")
        fx.build_delta(delta_path, delta_days, ny=ny, nx=nx)

        # One oracle: stamp each overlapping delta day with a value derived from its date, so
        # base and delta are the same data except where delta deliberately supersedes.
        g = tc_zarr_open(delta_path)
        marks = {}
        for i, day in enumerate(delta_days):
            mark = 900.0 + i
            g["sst"][i] = np.full_like(np.asarray(g["sst"][i]), mark)
            marks[day] = mark

        base = sc.SegmentedCubeStore(root)
        cube = TieredCube(base, TimeCubeStore(delta_path))

        base_only = [d for d in all_days if d not in set(delta_days)][-10:]
        delta_only = delta_days[-10:]
        crossing = base_only[-5:] + delta_only[:5]

        def timed(days):
            cube.point_series(LON, LAT, days, ["sst"])          # warm
            samples = []
            for _ in range(repeats):
                t0 = time.perf_counter()
                rows = cube.point_series(LON, LAT, days, ["sst"])
                samples.append((time.perf_counter() - t0) * 1000.0)
            snap = cube.snapshot()
            # Do the TIMED rows carry delta's values where delta owns the day? Base holds the
            # same days, so a timed loop reading the base store alone still returns the right
            # COUNT -- and a mutation doing exactly that survived until this was checked.
            delta_rows = [r for r in rows if r["date"] in marks]
            served_delta = all(abs(float(r["sst"]) - marks[r["date"]]) <= 1e-6
                               for r in delta_rows)
            return {"requested_days": len(days), "returned_days": len(rows),
                    "day_sources": {d: snap.day_source(d) for d in days},
                    "delta_owned_days_in_response": len(delta_rows),
                    "timed_rows_served_delta_values": served_delta,
                    **_percentiles(samples)}

        requests = {"base_only": timed(base_only), "delta_only": timed(delta_only),
                    "crossing": timed(crossing)}

        # Precedence: for a day BOTH tiers hold, the served value must be delta's mark.
        expected_shift = 1.0 if _negative_control else 0.0
        mismatches = []
        for day in delta_only[:3]:
            got = cube.point_series(LON, LAT, [day], ["sst"])
            want = marks[day] + expected_shift
            if not got or abs(float(got[0]["sst"]) - want) > 1e-6:
                mismatches.append({"day": day, "expected": want,
                                   "got": (got[0]["sst"] if got else None)})

        return {
            "label": "LOCAL_SYNTHETIC",
            "read_path": "TieredCube.point_series",
            "used_base_and_delta": True,
            "delta_span_days": delta_span,
            "s": s,
            "requests": requests,
            "delta_precedence_ok": not mismatches,
            "delta_precedence_mismatches": mismatches,
            "fixture_geometry": {"ny": ny, "nx": nx, "base_days": base_days_n,
                                 "delta_days": delta_span, "total_days": total},
            "cache_assumption": "warm: each request is issued once before sampling",
            "note": "LOCAL_SYNTHETIC. Not VM24 evidence; adjudicates no gate.",
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def tc_zarr_open(path):
    from store import time_cube as tc
    return tc.zarr.open_group(path, mode="a")


def measure_h5_latency(*, quick: bool = False, s: int = DEFAULT_S) -> dict:
    spans = list(H5_DELTA_SPANS)[:2] if quick else list(H5_DELTA_SPANS)
    rows = [measure_h5_span(delta_span=span, s=s) for span in spans]
    return {"label": "LOCAL_SYNTHETIC", "measured": True, "spans_measured": spans,
            "s": s, "by_span": {str(r["delta_span_days"]): r for r in rows},
            "note": ("LOCAL_SYNTHETIC crossing latency through a real TieredCube. It does NOT "
                     "adjudicate G3, which is a VM24 measurement.")}


# ------------------------------------------------- capacity gate (§7.1c, G18)
#: canonical zero-padded ISO day; `strptime` alone accepts 2026-6-1
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

GB = 1 << 30
#: The floor the SERVING system needs (§7.1c / P4-S4 §6). NOT tunable from here: lowering it to
#: make a gate pass is an ops policy decision, not an implementation detail, and this harness
#: must never be the place it happens.
HARD_RESERVE_GB = 250


def capacity_gate(*, free_now_gb: float, s: int = DEFAULT_S,
                  base_gb_per_day: float, ingest_gb_per_day: float,
                  build_days: float = 2.0, temp_gb: float = 2.0,
                  hard_reserve_gb: float = HARD_RESERVE_GB) -> dict:
    """§7.1c: `free_now - additional >= HARD_RESERVE`, with every term named.

    `additional` is the block estimate PLUS temp/checkpoint artifacts PLUS growth during the
    build -- never the block size alone. Reporting only the block size is how "141 GB fits"
    gets said about a machine that cannot host the build.

    The arithmetic is `build_block.disk_precheck`'s, and a test asserts the two agree on
    identical inputs rather than trusting that they look similar."""
    new_block_gb = round(s * base_gb_per_day, 1)
    growth_gb = round(ingest_gb_per_day * build_days, 1)
    additional_gb = round(new_block_gb + temp_gb + growth_gb, 1)
    projected_gb = round(free_now_gb - additional_gb, 1)
    ok = projected_gb >= hard_reserve_gb
    return {
        "label": "VM24_PRODUCTION_READ_ONLY",
        "formula": "projected_free_after = free_now - (new_block + temp + growth)",
        "s": s,
        "free_now_gb": free_now_gb,
        "new_block_gb": new_block_gb,
        "temp_checkpoint_artifacts_gb": temp_gb,
        "growth_during_build_gb": growth_gb,
        "build_days_assumed": build_days,
        "additional_gb": additional_gb,
        "hard_reserve_gb": hard_reserve_gb,
        "projected_free_after_gb": projected_gb,
        "gate_ok": ok,
        "verdict": "PASS" if ok else "BLOCKED",
        "shortfall_gb": 0.0 if ok else round(hard_reserve_gb - projected_gb, 1),
        "free_now_required_gb": round(hard_reserve_gb + additional_gb, 1),
        "note": ("HARD_RESERVE is NOT adjustable here. If it needs to change, that is an ops "
                 "policy decision raised separately -- never a change made to let a gate pass."),
    }


# ------------------------------------------------- hold / lifecycle audit (read-only)
HOLD_CLASSES = ("referenced", "blocked_unique_copy", "repair_state_unknown_pre_p5",
                "recovery_policy_pending", "releasable_after_approval",
                "inconsistent_or_unknown")

#: The only three things that count as proof a hold's CONTENT is recoverable. Day-set coverage
#: is not on this list, and neither is supersession: both describe which dates exist somewhere,
#: not whether the bytes can be got back.
RECOVERY_EVIDENCE_KINDS = ("value_parity", "external_manifest", "immutable_generation")

_REQUIRED_EVIDENCE_FIELDS = {
    "value_parity": ("variables", "var_valid_checked", "nan_semantics_checked",
                     "verification_provenance", "day_range"),
    "external_manifest": ("source", "day_coverage", "download_procedure",
                          "verification_method"),
    "immutable_generation": ("generation_id", "fingerprint", "provenance",
                             "supports_rollback"),
}


def validate_recovery_evidence(evidence) -> Optional[str]:
    """`None` when the evidence proves content recovery; otherwise why it does not.

    Strict about types on purpose. `bool(x)` accepts the string "false"; a missing field
    defaulting to `True` accepts nothing at all; a set collapses duplicates into apparent
    agreement. Each of those has already produced a wrong verdict somewhere in this project, so
    the fields are checked for exact types rather than truthiness."""
    if evidence is None:
        return "no recovery evidence supplied"
    if type(evidence) is not dict:
        return f"recovery evidence must be a dict, got {type(evidence).__name__}"
    kind = evidence.get("kind")
    if kind not in RECOVERY_EVIDENCE_KINDS:
        return (f"unknown recovery evidence kind {kind!r}; must be one of "
                f"{', '.join(RECOVERY_EVIDENCE_KINDS)}")
    missing = [f for f in _REQUIRED_EVIDENCE_FIELDS[kind] if f not in evidence]
    if missing:
        return f"{kind} evidence is missing {', '.join(missing)}"
    for flag in ("var_valid_checked", "nan_semantics_checked", "supports_rollback"):
        if flag in evidence and evidence[flag] is not True:
            return f"{flag} must be exactly True, got {evidence[flag]!r}"
    for field in ("verification_provenance", "verification_method", "download_procedure",
                  "fingerprint", "generation_id", "provenance", "source"):
        if field in evidence:
            if type(evidence[field]) is not str or not evidence[field].strip():
                return f"{field} must be a non-empty string"
    if kind == "value_parity":
        if type(evidence["variables"]) is not list or not evidence["variables"]:
            return "value_parity evidence must list the variables it checked"
    return None


def classify_hold_entry(*, entry: dict, now_utc: str, base_days: set,
                        spatial_window: Sequence[str],
                        recovery_evidence: Optional[dict] = None,
                        p5_roots_present: bool = False,
                        wal_repairs_pending: Optional[set] = None,
                        referenced_by_rollback: bool = False,
                        in_active_serving_path: bool = False,
                        tracked_in_manifest: bool = True) -> dict:
    """Classify ONE hold entry, read-only. Deletes nothing and authorizes nothing.

    ## Evidence, as separate facts

    - `base_membership_covered` -- base's day SET contains the day. Not a statement about values.
    - `bbox_spatially_unneeded` -- outside the enforced spatial window, so bbox does not need it.
      **Never sufficient**: silent about point/time-range, base rebuild and recovery.
    - `recovery_content_verified` -- one of the three `RECOVERY_EVIDENCE_KINDS`, validated. Day-set
      coverage and supersession are deliberately NOT among them: "the live delta holds the same
      dates" says nothing about whether these bytes can be recovered.
    - `release_authorized` -- only with validated recovery evidence and no outstanding objection.

    ## Pre-P5 hosts

    Absent P5 roots mean there is no repair WAL to read. That is
    `repair_state_unknown_pre_p5` -- unchecked, not clear -- and it blocks authorization. Accepting
    a pre-P5 release is an operator policy, never a classifier default.
    """
    days = list(entry.get("days") or ([entry["day"]] if entry.get("day") else []))
    why = validate_recovery_evidence(recovery_evidence)
    ev = {"base_membership_covered": None, "bbox_spatially_unneeded": None,
          "recovery_content_verified": why is None,
          "recovery_evidence_kind": (recovery_evidence or {}).get("kind") if why is None else None,
          "recovery_evidence_rejected_because": why,
          "release_authorized": False}

    def out(klass, reason):
        # Every OTHER outstanding objection, not just the first one to fire. Reporting only the
        # blocker that happened to be checked first reads as "fix this and it is releasable",
        # which for these entries is false twice over.
        also = []
        if klass != "recovery_policy_pending" and why is not None and klass not in (
                "inconsistent_or_unknown", "blocked_unique_copy"):
            also.append(f"recovery evidence: {why}")
        if klass != "repair_state_unknown_pre_p5" and not p5_roots_present:
            also.append("repair state unchecked (P5 roots absent)")
        return {"klass": klass, "days": days, "evidence": ev, "reason": reason,
                "also_blocking": also}

    if not tracked_in_manifest:
        return out("inconsistent_or_unknown",
                   "present on disk but absent from the hold manifest, so no lifecycle state "
                   "exists; an untracked path is what a manual cleanup leaves behind")
    if not days:
        return out("inconsistent_or_unknown", "the record names no day")
    if not entry.get("hold_until"):
        return out("inconsistent_or_unknown", "no hold_until; the retention window is unknown")
    if in_active_serving_path:
        return out("referenced", "still on the active serving path")
    if referenced_by_rollback:
        return out("referenced", "named as a rollback/reverse-swap target")

    ev["base_membership_covered"] = all(d in base_days for d in days)
    ev["bbox_spatially_unneeded"] = not (
        spatial_window and any(spatial_window[0] <= d <= spatial_window[1] for d in days))

    if not ev["base_membership_covered"]:
        missing = sorted(d for d in days if d not in base_days)
        return out("blocked_unique_copy",
                   f"{len(missing)} day(s) are not in base, first {missing[0]}: only local copy")
    if not ev["bbox_spatially_unneeded"]:
        return out("referenced", "day(s) fall inside the enforced spatial window")
    if not p5_roots_present:
        return out("repair_state_unknown_pre_p5",
                   "the P5 roots are absent, so no repair WAL exists to consult. Repair state is "
                   "UNCHECKED, not clear, and an unchecked precondition cannot authorize a "
                   "release. Accepting a pre-P5 release is an operator policy decision")
    if set(days) & set(wal_repairs_pending or ()):
        return out("repair_state_unknown_pre_p5", "repair/refold outstanding for day(s) held")
    if entry["hold_until"] > now_utc:
        return out("referenced", f"hold_until {entry['hold_until']} has not passed")

    if why is not None:
        return out("recovery_policy_pending",
                   f"every necessary condition holds, but none is sufficient: {why}. Day-set "
                   f"coverage and supersession describe which DATES exist elsewhere, not "
                   f"whether these BYTES can be recovered. Needs an operator policy decision")

    ev["release_authorized"] = True
    return out("releasable_after_approval",
               f"validated {ev['recovery_evidence_kind']} recovery evidence, base covers the "
               f"days, the window has passed and nothing references it. RELEASE STILL REQUIRES "
               f"HUMAN APPROVAL: this authorizes nothing on its own")


def _strict_bool(value, what: str) -> bool:
    """A raw `bool`, or a refusal. `bool("false")` is True, and that is not a typo anyone
    catches by reading -- it is caught by refusing the conversion."""
    if type(value) is not bool:
        raise _EvidenceError(f"{what} must be a raw bool, got {type(value).__name__} "
                             f"({value!r}). It is not converted: bool('false') is True.")
    return value


class _EvidenceError(Exception):
    """Raw evidence that cannot be trusted. Always fails the audit closed."""


def expand_day_range(spec, what: str) -> List[str]:
    """A calendar range expanded and VERIFIED, not two endpoints treated as a day set.

    The evidence used to carry `["2023-01-01", "2026-06-26"]` and the audit did
    `set(evidence["base_days"])` -- a two-element set standing in for 1273 days, so a membership
    check tested two dates and passed. The test helper expanded the range before calling, which
    meant the test fixed the evidence and then checked the audit: the audit itself never
    validated anything.

    The range now declares its own `day_count`, and the arithmetic must agree."""
    if type(spec) is not dict:
        raise _EvidenceError(f"{what} must be a dict with start/end/day_count, got "
                             f"{type(spec).__name__}")
    for field in ("start", "end", "day_count"):
        if field not in spec:
            raise _EvidenceError(f"{what} is missing {field}")
    for field in ("start", "end"):
        if type(spec[field]) is not str or not _ISO_DAY.match(spec[field]):
            raise _EvidenceError(f"{what}.{field} must be a canonical YYYY-MM-DD string")
    if type(spec["day_count"]) is not int:
        raise _EvidenceError(f"{what}.day_count must be an int")
    try:
        start = _dt.date.fromisoformat(spec["start"])
        end = _dt.date.fromisoformat(spec["end"])
    except ValueError as exc:
        raise _EvidenceError(f"{what}: {exc}") from None
    if end < start:
        raise _EvidenceError(f"{what}: end {spec['end']} precedes start {spec['start']}")
    span = (end - start).days + 1
    if span != spec["day_count"]:
        raise _EvidenceError(
            f"{what}: the range {spec['start']}..{spec['end']} spans {span} days but declares "
            f"day_count {spec['day_count']}. A gap or a wrong bound -- either way the day set "
            f"cannot be derived from it.")
    if "gaps" in spec and spec["gaps"] not in (0, []):
        raise _EvidenceError(f"{what}: declares gaps {spec['gaps']!r}; the expansion would be "
                             f"wrong and membership cannot be established")
    return [(start + _dt.timedelta(days=i)).isoformat() for i in range(span)]


def _repairs_pending(value) -> set:
    """A raw list of canonical days, or a refusal. `set(x)` on a string yields its characters
    and on a malformed list quietly collapses duplicates -- both turn broken input into a
    legal-looking set, and a set that looks legal is one that gets reasoned from."""
    if value is None:
        return set()
    if type(value) is not list:
        raise _EvidenceError(f"wal_repairs_pending must be a list or null, got "
                             f"{type(value).__name__}; a string would iterate into characters")
    for d in value:
        if type(d) is not str or not _ISO_DAY.match(d):
            raise _EvidenceError(f"wal_repairs_pending contains {d!r}; every entry must be a "
                                 f"canonical YYYY-MM-DD string")
    dupes = sorted({d for d in value if value.count(d) > 1})
    if dupes:
        raise _EvidenceError(f"wal_repairs_pending repeats {dupes}; de-duplicating would "
                             f"launder a malformed list into a legal-looking set")
    return set(value)


def audit_hold_lifecycle_from_evidence(evidence: dict) -> dict:
    """The committed, reproducible audit path: raw evidence in, classified verdicts out.

    It consumes the evidence file AS STORED -- expanding and verifying the calendar ranges
    itself, and refusing every non-bool where a bool is required. Nothing may be corrected on
    the way in; a caller that has to fix the evidence first is testing its own fixture."""
    required = ("measured_utc", "host", "now_utc", "base_day_range", "spatial_window",
                "p5_roots_present", "entries")
    missing = [f for f in required if f not in evidence]
    if missing:
        return {"audit_only": True, "deletion_performed": False, "verdict": "FAIL_CLOSED",
                "reason": f"evidence is missing {', '.join(missing)}"}
    try:
        base_days = set(expand_day_range(evidence["base_day_range"], "base_day_range"))
        p5_present = _strict_bool(evidence["p5_roots_present"], "p5_roots_present")
        window = evidence["spatial_window"]
        if type(window) is not list or len(window) != 2:
            raise _EvidenceError("spatial_window must be a two-element list")
        results = []
        for i, e in enumerate(evidence["entries"]):
            what = f"entries[{i}]"
            if type(e) is not dict:
                raise _EvidenceError(f"{what} must be a dict")
            rec = e.get("record")
            if type(rec) is not dict:
                raise _EvidenceError(f"{what}.record must be a dict")
            days = expand_day_range(rec["day_range"], f"{what}.record.day_range") \
                if "day_range" in rec else []
            entry = {"days": days, "hold_until": rec.get("hold_until")}
            r = classify_hold_entry(
                entry=entry, now_utc=evidence["now_utc"], base_days=base_days,
                spatial_window=window, recovery_evidence=e.get("recovery_evidence"),
                p5_roots_present=p5_present,
                wal_repairs_pending=_repairs_pending(evidence.get("wal_repairs_pending")),
                referenced_by_rollback=_strict_bool(
                    e.get("referenced_by_rollback", False), f"{what}.referenced_by_rollback"),
                in_active_serving_path=_strict_bool(
                    e.get("in_active_serving_path", False), f"{what}.in_active_serving_path"),
                tracked_in_manifest=_strict_bool(
                    e.get("tracked_in_manifest", True), f"{what}.tracked_in_manifest"))
            results.append({"what": e.get("what"), "bytes": e.get("bytes"),
                            "gib": round((e.get("bytes") or 0) / (1 << 30), 1),
                            "day_count": len(days), **r})
    except _EvidenceError as exc:
        return {"audit_only": True, "deletion_performed": False, "verdict": "FAIL_CLOSED",
                "reason": str(exc)}

    reclaimable = sum(r["bytes"] or 0 for r in results
                      if r["klass"] == "releasable_after_approval")
    return {
        "audit_only": True, "deletion_performed": False, "verdict": "COMPLETE",
        "measured_utc": evidence["measured_utc"], "host": evidence["host"],
        "p5_roots_present": p5_present, "entries": results,
        "currently_reclaimable_bytes": reclaimable,
        "currently_reclaimable_gib": round(reclaimable / (1 << 30), 1),
        "note": ("every `release_authorized` traces to validated recovery evidence in the "
                 "input, and every day set was expanded from a verified calendar range. "
                 "Nothing was deleted."),
    }


# --------------------------------------------------------------------------- deferred
def deferred_metrics() -> dict:
    """What cannot be established here. Each carries a reason and NO number."""
    return {
        "inode_headroom": {
            "label": "DEFERRED",
            "reason": "`df -i` free inodes on the production filesystem. A temp dir on this "
                      "machine says nothing about VM24's inode table, and a full inode table "
                      "is an outage with plenty of free bytes. Needs VM24 read-only."},
        "production_366d_point_latency": {
            "label": "DEFERRED",
            "reason": "§10.4's 0.65 ms per extra segment call is a laptop-measured projection "
                      "against a VM24 baseline of 76-100 ms. Measuring it at fixture extent "
                      "would produce a number that looks like the gate and is not."},
        "peak_temp_disk_at_production_scale": {
            "label": "DEFERRED",
            "reason": "47/71/141 GB is arithmetic from 1.57 GB/day. Confirming it requires "
                      "building one real block, which is a VM24 mutation."},
        "metadata_scan_on_the_production_tree": {
            "label": "DEFERRED",
            "reason": "Scan cost is dominated by the filesystem's directory implementation and "
                      "cache state at ~1.7M-6.6M files. Local timings do not extrapolate."},
        "open_fd_and_RSS_under_sustained_load": {
            "label": "DEFERRED",
            "reason": "H7's C=8/16 over 10 minutes is an S7 load-gate measurement against the "
                      "real store; this harness runs no HTTP layer."},
    }


# --------------------------------------------------------------------------- report
def build_report(*, quick: bool = False) -> dict:
    """The whole S6 picture. `quick` keeps the measured grid small for the test suite."""
    horizons = [5, 13] if quick else list(SNAPSHOT_HORIZONS)
    s_grid = [30, 90] if quick else [30, 45, 90]
    days = 60 if quick else 90

    report: dict = {
        "harness": "bench_p5_sizing",
        "scope": "LOCAL AND SYNTHETIC ONLY — no VM24, no production path, no cron",
        "fixture_anchor_day": FIXTURE_ANCHOR_DAY,
        "fixture_anchor_note": FIXTURE_ANCHOR_NOTE,
        "production_geometry": {"ny": NY, "nx": NX, "vars": list(VARS),
                                "legacy_time_chunk": LEGACY_TIME_CHUNK,
                                "delta": dict(DELTA_GEOMETRY)},
        "grid": {"S": [30, 45, 90], "C": [30, 45, 90]},
        "model": model_all(),
        "H3": {
            "1_day": model_decompressed_bytes_per_var(s=90, days=1),
            "366_day": {f"S={s}": model_decompressed_bytes_per_var(s=s, days=366)
                        for s in (30, 45, 90)},
            "legacy_block_delta_crossing": model_segments_per_request(s=90, days=366),
        },
        "H5": measure_h5_latency(quick=quick),
        "H7": {"label": "MODEL", "horizons": model_segment_horizons()},
        "measured": {
            "segment_calls": [measure_segment_calls(s=s, days=days) for s in s_grid],
            "snapshot_open": [measure_snapshot_open(segments=n) for n in horizons],
            "metadata_scan": [measure_metadata_scan(segments=n) for n in horizons],
            "tail_rebuild": [measure_tail_rebuild(s=s, c=30) for s in s_grid],
            # The two real-path measurements belong in the ARTIFACT, not only in the prose.
            # They were described in the results doc and absent from the JSON, so the
            # consistency tests could not see the omission.
            "grouped_read": [
                # A request spanning ALL FOUR segments. The persisted rows previously covered
                # only 10- and 5-day requests, which at total_days=40/segments=4 both land
                # inside one segment -- so the artifact held no cross-segment evidence at all.
                measure_grouped_read(segments=4, total_days=40, request_days=40),
                # ...and one spanning two, so the artifact shows the count moving.
                measure_grouped_read(segments=4, total_days=40, request_days=20),
                measure_grouped_read(segments=4, total_days=40, request_days=5,
                                     request_within_one_segment=True),
            ],
            "snapshot_construction": [measure_snapshot_construction(segments=n)
                                      for n in horizons],
        },
        "deferred": deferred_metrics(),
        "gates": {
            "G3": {"verdict": "NOT_ADJUDICATED",
                   "reason": "the 366-day p95 gate is against the VM24 baseline; nothing "
                             "measured at fixture extent can decide it"},
            "H7": {"verdict": "NOT_ADJUDICATED",
                   "reason": "inode headroom and sustained-load fd/RSS are DEFERRED"},
            "G16": {"verdict": "NOT_ADJUDICATED",
                    "reason": "metadata-scan and snapshot-open shapes are measured locally; "
                              "the production thresholds are not"},
        },
    }
    return report


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="P5-S6 sizing harness (local/synthetic only)")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                  "results"))
    args = ap.parse_args(argv)
    os.makedirs(args.out, exist_ok=True)
    report = build_report(quick=args.quick)
    stamp = "quick" if args.quick else "full"
    path = os.path.join(args.out, f"p5s6_sizing_{stamp}.json")
    with open(path, "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"wrote {path}")
    for row in report["measured"]["segment_calls"]:
        print(f"  S={row['s']:>3}  segment calls reported={row['reported_segment_calls']} "
              f"observed={row['observed_segment_calls']}")
    for name in report["gates"]:
        print(f"  gate {name}: {report['gates'][name]['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
