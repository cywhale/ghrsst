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
import hashlib
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
        return {"label": "PRODUCTION_GEOMETRY_FIXTURE", "s": s, "days": days,
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
        return {"label": "LOCAL", "segments": segments, "wall_ms": round(wall_ms, 3),
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


def measure_tail_rebuild(*, s: int, c: int, touch_without_changing: bool = False) -> dict:
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

        return {"label": "LOCAL", "s": s, "c": c,
                "rewrite_evidence": "sha256-content-digest",
                "carried_forward_days": carried, "days_in_new_tail": total,
                "rewritten_files": rewritten,
                "rewritten_files_touched_only": touched_only,
                "rebuild_wall_ms": round(wall_ms, 2),
                "modelled_amplification": model_write_amplification(s=s, c=c)["amplification"]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
        "H5": {
            "label": "MODEL",
            "delta_spans": list(H5_DELTA_SPANS),
            "by_span": {str(span): {"label": "MODEL", "delta_span_days": span,
                                    "delta_gb": round(span * DELTA_GB_PER_DAY, 1),
                                    "implied_cadence": span - SPATIAL_WINDOW_DAYS
                                                       - DELTA_BUFFER_DAYS}
                        for span in H5_DELTA_SPANS},
        },
        "H7": {"label": "MODEL", "horizons": model_segment_horizons()},
        "measured": {
            "segment_calls": [measure_segment_calls(s=s, days=days) for s in s_grid],
            "snapshot_open": [measure_snapshot_open(segments=n) for n in horizons],
            "metadata_scan": [measure_metadata_scan(segments=n) for n in horizons],
            "tail_rebuild": [measure_tail_rebuild(s=s, c=30) for s in s_grid],
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
