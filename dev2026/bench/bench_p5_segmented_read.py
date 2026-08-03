"""dev2026 — P5-S2: segmented read path vs the S0 baseline of record.

Answers the three questions §14 P5-S2 puts on this step:

  **G3**  is the segmented point/range p95 within **+25 %** of the `TieredCube` baseline the
          S0 harness recorded, measured on the same fixture shape?
  **R3**  do open file descriptors, RSS and snapshot-open cost stay bounded as the segment
          count grows (groundwork for the S6/S7 gates, not those gates themselves)?
  **§6.2** does the read path open **zero** stores per request?

The comparison is monolith-vs-segmented on IDENTICAL data: one block holding N days versus the
same N days split across k segments. Anything else compares two datasets, not two read paths.

[MUT-STG]/[RO]: synthetic fixtures in a temp dir. No `GHRSST_*` store, no VM24 path.

Run:
  dev2026/.venv/bin/python dev2026/bench/bench_p5_segmented_read.py \
      --out dev2026/bench/results/p5s2_segmented_read.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import os
import platform
import shutil
import sys
import tempfile
from datetime import date, timedelta

import numpy as np
import zarr

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "tests"))

import p5_fixtures as fx  # noqa: E402
from p5_cost import timeit_ms  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store.segmented_cube import SegmentedCubeStore  # noqa: E402
from store.tiered_cube import TieredCube  # noqa: E402
from store.time_cube import TimeCubeStore  # noqa: E402

try:
    import psutil
    _PROC = psutil.Process()
except Exception:                                     # pragma: no cover
    _PROC = None

ANCHOR = "2026-06-27"
LON, LAT = 105.0, 5.0
FIELDS = ("sst", "sst_anomaly", "sea_ice")
#: [VM24] v0.5.0 measured 366-day point-range p95 band (README edge results).
VM24_BASELINE_MS = (76.0, 100.0)


def _rss_mb():
    return round(_PROC.memory_info().rss / 1e6, 1) if _PROC else None


def _open_fds():
    if _PROC is None:
        return None
    try:
        return _PROC.num_fds()
    except Exception:                                  # pragma: no cover - platform-specific
        return None


def _manifest(root, segments, ny, nx, generation=1):
    m = {"format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION,
         "generation": generation,
         "generation_id": f"gen{generation:06d}-20270101T000000Z",
         "created_utc": "2027-01-01T00:00:00Z", "created_by": "p5s2-bench",
         "predecessor_generation": None, "predecessor_manifest": None,
         "grid": {"ny": ny, "nx": nx, "region": [0, ny, 0, nx]},
         "variables": sorted({v for s in segments for v in s["variables"]}),
         "block_grid": {"anchor_day": ANCHOR, "block_days": 90},
         "segments": list(segments), "superseded": [], "manifest_checksum": ""}
    m["manifest_checksum"] = bm.compute_checksum(m)
    return m


def _seg(seg_id, rel, start, end, day_count, precedence, store_path):
    return {"segment_id": seg_id, "kind": "block", "path": rel, "immutable": True,
            "boundary_kind": "legacy", "start_day": start, "end_day": end,
            "materialized_through": end, "day_count": day_count,
            "gaps": [], "unknown": [], "day_list": None,
            "layout": bm.segment_layout(store_path), "variables": sorted(fx.VARS),
            "fingerprint": {"algo": "sha256",
                            "metadata": bm.metadata_fingerprint(store_path),
                            "day_digest": bm.day_digest(fx.read_days(store_path))},
            "precedence": precedence, "sealed": True, "supersedes": None}


def _build(workdir: str, total_days: int, n_segments: int, ny: int, nx: int):
    """`n_segments == 1` is the monolith baseline; k > 1 splits the SAME days."""
    root = os.path.join(workdir, f"seg{n_segments}")
    os.makedirs(root, exist_ok=True)
    days = fx.days_from(ANCHOR, total_days)
    per = total_days // n_segments
    segs = []
    for k in range(n_segments):
        lo = k * per
        hi = total_days if k == n_segments - 1 else (k + 1) * per
        chunk_days = days[lo:hi]
        p = fx.build_block(os.path.join(root, f"b{k}"), chunk_days, ny=ny, nx=nx, seed=k * 7)
        segs.append(_seg(f"b{k}", f"b{k}", chunk_days[0], chunk_days[-1],
                         len(chunk_days), k, p))
    bm.publish(root, _manifest(root, segs, ny, nx))
    return root, days


def measure(workdir: str, *, total_days: int, segment_counts, ny: int, nx: int,
            delta_days: int, repeats: int) -> dict:
    if total_days < 366:
        raise SystemExit(f"--total-days must be >= 366 (got {total_days}); otherwise the "
                         f"\"366-day\" case silently measures fewer days")
    rows = []
    delta_path = os.path.join(workdir, "delta.zarr")
    fx.build_delta(delta_path, fx.days_from(bm.add_days(ANCHOR, total_days), delta_days),
                   ny=ny, nx=nx)

    for k in segment_counts:
        root, days = _build(workdir, total_days, k, ny, nx)

        fds0, rss0 = _open_fds(), _rss_mb()
        open_ms = timeit_ms(lambda: SegmentedCubeStore(root), repeats=3)
        base = SegmentedCubeStore(root)
        cube = TieredCube(base, TimeCubeStore(delta_path))
        fds1, rss1 = _open_fds(), _rss_mb()

        one = [days[total_days // 2]]
        span = days[-366:]
        assert len(span) == 366, f"the 366-day case has {len(span)} days"
        crossing = days[-(366 - delta_days):] + fx.read_days(delta_path)[:delta_days]

        cube.point_series(LON, LAT, span, FIELDS)          # warm
        # §6.2: the READ path must open nothing
        real_open, opened = zarr.open_group, []

        def spy(path, *a, **kw):
            opened.append(str(path))
            return real_open(path, *a, **kw)
        zarr.open_group = spy
        try:
            cube.point_series(LON, LAT, span, FIELDS)
        finally:
            zarr.open_group = real_open

        rows.append({
            "segments": k, "days_per_segment": total_days // k,
            "snapshot_open_ms": open_ms,
            "single_day": timeit_ms(lambda: cube.point_series(LON, LAT, one, FIELDS), repeats),
            "range_366d": timeit_ms(lambda: cube.point_series(LON, LAT, span, FIELDS), repeats),
            "crossing": timeit_ms(lambda: cube.point_series(LON, LAT, crossing, FIELDS),
                                  repeats),
            "segments_touched_366d": len({base.resolve(d)[0] for d in span
                                          if d in base._day_index}),
            "stores_opened_during_read": len(opened),
            "open_fds_before": fds0, "open_fds_after": fds1,
            "rss_mb_before": rss0, "rss_mb_after": rss1,
        })
        shutil.rmtree(root, ignore_errors=True)

    base_row = rows[0]
    for r in rows:
        for case in ("single_day", "range_366d", "crossing"):
            b = base_row[case]["p95_ms"]
            r.setdefault("vs_monolith_p95_x", {})[case] = (
                round(r[case]["p95_ms"] / b, 3) if b else None)

    worst = max(r["vs_monolith_p95_x"]["range_366d"] for r in rows)
    # Scale-independent cost: the extra wall time per EXTRA array call. A ratio is not
    # portable between fixtures -- it depends entirely on how large the baseline is -- but
    # the added cost per call is, and it is what a production projection needs.
    n_fields = len(FIELDS)
    per_call = []
    for r in rows[1:]:
        extra_calls = (r["segments_touched_366d"] - base_row["segments_touched_366d"]) * n_fields
        if extra_calls > 0:
            per_call.append((r["range_366d"]["p95_ms"] - base_row["range_366d"]["p95_ms"])
                            / extra_calls)
    # Report the SPREAD, not a single figure. Repeated runs of this harness produced
    # 0.47-0.75 ms for the same quantity; quoting one value to four decimals would be false
    # precision, and any document citing it would be stale on the next run.
    per_call = sorted(round(x, 3) for x in per_call)
    # statistics.median, not per_call[len//2]: with an even number of samples the latter is
    # the upper-middle value, not the median (for [0.631, 0.636] it returns 0.636, not 0.6335).
    added_ms_per_call = round(statistics.median(per_call), 4) if per_call else None
    per_call_band = [per_call[0], per_call[-1]] if per_call else None
    # Projection to production: at S=90 a 366-day request touches ceil(366/90)=5 segments,
    # i.e. 4 extra vs a monolith, across every variable.
    recommended_extra_calls = (math.ceil(366 / 90) - 1) * len(FIELDS)
    projected_added_ms = (added_ms_per_call or 0.0) * recommended_extra_calls
    projected_pct = tuple(100.0 * projected_added_ms / b for b in VM24_BASELINE_MS)
    fd_growth = None
    if all(r["open_fds_after"] is not None for r in rows):
        fd_growth = max(r["open_fds_after"] - r["open_fds_before"] for r in rows)
    return {
        "harness": "bench_p5_segmented_read.py", "step": "P5-S2",
        "provenance": ("COMMITTED harness, synthetic fixtures, local, warm. Monolith vs "
                       "segmented on IDENTICAL days. Not VM24 performance."),
        "env": {"zarr": zarr.__version__, "python": sys.version.split()[0],
                "platform": platform.platform()},
        "grid": [ny, nx], "total_days": total_days, "delta_days": delta_days,
        "rows": rows,
        "worst_range_366d_regression_x": worst,
        "added_ms_per_extra_array_call": added_ms_per_call,
        "added_ms_per_extra_array_call_samples": per_call,
        "added_ms_per_extra_array_call_band": per_call_band,
        "g3_bar_x": 1.25,
        "g3_ratio_on_this_fixture": worst,
        "g3_adjudicable_here": False,
        "g3_verdict": "DEFERRED to S6/S7 (production geometry)",
        # Computed from THIS run, never hardcoded: a prose constant here silently goes
        # stale the moment the fixture changes, and then the artifact contradicts the results
        # doc that quotes it.
        "g3_reason": (
            f"A RATIO is not portable between fixtures: it is the added per-call cost divided "
            f"by the baseline's absolute magnitude. On this synthetic fixture a "
            f"{len(FIELDS)}-variable 366-day read costs "
            f"{base_row['range_366d']['p95_ms']:.1f} ms p95, so the per-call overhead IS "
            f"essentially the whole measurement and the ratio approaches the call-count "
            f"ratio. Grid size does not change this -- a point read touches one chunk per "
            f"time block regardless of ny/nx. Against the [VM24] 366-day baseline of "
            f"{VM24_BASELINE_MS[0]}-{VM24_BASELINE_MS[1]} ms, the same absolute overhead "
            f"({recommended_extra_calls} extra calls x {added_ms_per_call} ms median = "
            f"{projected_added_ms:.1f} ms at S=90; per-call samples this run "
            f"{per_call_band}) projects to roughly "
            f"+{projected_pct[1]:.0f}-{projected_pct[0]:.0f}%. "
            f"G3 must therefore be "
            f"adjudicated against the real baseline, which is S6/S7's job, not asserted "
            f"here."),
        "vm24_baseline_ms": list(VM24_BASELINE_MS),
        "projected_added_ms_at_s90": round(projected_added_ms, 2),
        "projected_regression_pct_at_s90": [round(projected_pct[1], 1),
                                            round(projected_pct[0], 1)],
        "no_store_opens_in_read_path": all(r["stores_opened_during_read"] == 0 for r in rows),
        "max_fd_growth_on_open": fd_growth,
        # What S2 CAN decide on its own fixtures:
        "s2_gate_pass": bool(all(r["stores_opened_during_read"] == 0 for r in rows)
                             and (fd_growth is None or fd_growth <= 8)),
        "markdown_table": None,          # filled in below, from these very rows
        "run_digest": None,
        "note": ("G3 compares like with like: the same days, one block vs k. The absolute "
                 "numbers are laptop/synthetic and are NOT the VM24 gate -- S6/S7 own that."),
    }


def _markdown_table(rows) -> str:
    """Render the results table FROM the rows.

    Hand-transcribing this into the results document drifted on four separate rounds. A
    generated string can be copied verbatim and checked against `run_digest`, so a doc quoting
    one run's numbers under another run's heading is detectable instead of plausible."""
    out = ["| segments | segments touched | 1-day p95 | 366-day p95 | vs monolith | "
           "crossing p95 | snapshot open p95 |",
           "|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(
            f"| {r['segments']} | {r['segments_touched_366d']} | "
            f"{r['single_day']['p95_ms']:.2f} ms | **{r['range_366d']['p95_ms']:.2f} ms** | "
            f"{r['vs_monolith_p95_x']['range_366d']:.2f}× | "
            f"{r['crossing']['p95_ms']:.1f} ms | {r['snapshot_open_ms']['p95_ms']:.1f} ms |")
    return "\n".join(out)


def _finalize(res: dict) -> dict:
    res["markdown_table"] = _markdown_table(res["rows"])
    res["run_digest"] = hashlib.sha256(
        json.dumps(res["rows"], sort_keys=True).encode()).hexdigest()[:16]
    return res


def main():
    ap = argparse.ArgumentParser(description="P5-S2 segmented read path vs baseline")
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--ny", type=int, default=64)
    ap.add_argument("--nx", type=int, default=64)
    ap.add_argument("--total-days", type=int, default=400, dest="total_days",
                    help="must be >= 366 so the 366-day case is really 366 days")
    ap.add_argument("--segments", default="1,4,12")
    ap.add_argument("--delta-days", type=int, default=31, dest="delta_days")
    ap.add_argument("--repeats", type=int, default=31,
                    help="raised from 15: p95 on a laptop is noisy at 15")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    workdir = args.workdir or tempfile.mkdtemp(prefix="p5s2_")
    res = _finalize(measure(workdir, total_days=args.total_days,
                  segment_counts=[int(x) for x in args.segments.split(",")],
                  ny=args.ny, nx=args.nx, delta_days=args.delta_days,
                  repeats=args.repeats))
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as fh:
            json.dump(res, fh, indent=2, sort_keys=True)
        print(f"wrote {args.out}")
    for r in res["rows"]:
        print(f"segments={r['segments']:>3} touched={r['segments_touched_366d']:>3} "
              f"1d p95={r['single_day']['p95_ms']:>7} "
              f"366d p95={r['range_366d']['p95_ms']:>8} "
              f"({r['vs_monolith_p95_x']['range_366d']:>5}x) "
              f"crossing p95={r['crossing']['p95_ms']:>8} "
              f"snapshot_open p95={r['snapshot_open_ms']['p95_ms']:>7} "
              f"opens_in_read={r['stores_opened_during_read']}")
    print(f"added cost per extra array call: {res['added_ms_per_extra_array_call']} ms "
          f"(median; samples {res['added_ms_per_extra_array_call_samples']})")
    print(f"G3: {res['g3_verdict']} -- ratio here {res['g3_ratio_on_this_fixture']}x "
          f"(bar {res['g3_bar_x']}x), NOT adjudicable on a synthetic fixture")
    print(f"run_digest: {res['run_digest']}")
    print(res["markdown_table"])
    print(f"S2 gate: {res['s2_gate_pass']} | no opens in read path: "
          f"{res['no_store_opens_in_read_path']} | max fd growth: "
          f"{res['max_fd_growth_on_open']}")
    if not res["s2_gate_pass"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
