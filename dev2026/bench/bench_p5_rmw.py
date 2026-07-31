"""dev2026 — P5-S0: committed harness for hypotheses H1, H2, H3, H8.

Re-derives, with a COMMITTED and self-tested harness, the structural findings the P5 design
spec recorded as **[PROBE]** (scratch, uncommitted). §14 P5-S0 makes this a precondition:
no P5 step may advance on a scratch probe.

  H1  rewriting ONE day inside a full time block rewrites ~100 % of that block's shards
      (~time_chunk-fold write amplification)                       -> spec §3.A, candidate A
  H2  appending into a PARTIAL tail shard rewrites the whole partial shard, so growing a
      block day-by-day costs ~(S+1)/2x its final size              -> spec §3.A, §7
  H3  segmentation does NOT change chunk_count / decompressed_bytes; it adds only a
      per-segment store-call overhead                              -> spec §3.B, §10.4
      (refined here: decompressed_bytes is invariant in every regime, chunk_count while
       the inner time chunk is unchanged -- see `measure_h3`)
  H8  Zarr v3 has no production-safe native way to avoid shard RMW for our read layout
      (rectilinear shards trade a point-series regression for cheap appends) -> spec §3.E

[RO] with respect to production: everything runs in a caller-supplied temp directory on
synthetic data. No GHRSST store, no VM24 path, no env store is opened.

Run:
  dev2026/.venv/bin/python dev2026/bench/bench_p5_rmw.py \
      --out dev2026/bench/results/p5s0_rmw.json
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from typing import Optional, Sequence

import numpy as np
import zarr

sys.path.insert(0, os.path.dirname(__file__))
from p5_cost import (  # noqa: E402
    CountingLocalStore,
    assert_geometry,
    changed_files,
    evaluate_s0_gate,
    point_column_cost,
    read_geometry,
    snapshot_tree,
    timeit_ms,
    tree_size,
)

VAR = "sst"


def _fresh(path: str) -> str:
    if os.path.exists(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)
    return path


def _data(T, ny, nx, seed=0):
    rng = np.random.default_rng(seed)
    return rng.random((T, ny, nx), dtype=np.float32) * 30.0


def _create(path, T, ny, nx, time_chunk, shard, spatial_chunk=8, store=None, clamp=True):
    """`clamp=True` sizes the time chunk to the materialized days (`min(time_chunk, T)`) --
    the layout a PUBLISHED block actually gets (§7.2). `clamp=False` declares the full
    time block up front even though only T days exist, i.e. a block being grown in place;
    that is the configuration H2 exists to measure."""
    tc = min(time_chunk, T) if clamp else time_chunk
    target = store if store is not None else path
    g = zarr.open_group(target, mode="w", zarr_format=3)
    g.create_array(VAR, shape=(T, ny, nx), dtype="float32",
                   chunks=(tc, spatial_chunk, spatial_chunk),
                   shards=(tc, shard, shard), fill_value=float("nan"))
    return g


def _clamp_point(ii, jj, ny, nx):
    """Keep the sampled point inside the grid so small test fixtures stay valid."""
    return min(ii, ny - 1), min(jj, nx - 1)


# --------------------------------------------------------------------------- H1
def measure_h1(workdir: str, *, ny: int = 512, nx: int = 512,
               time_chunk: int = 90, shard: int = 128, spatial_chunk: int = 8) -> dict:
    """Build a COMPLETE time block, then overwrite a single day inside it."""
    path = _fresh(os.path.join(workdir, "h1"))
    g = _create(path, time_chunk, ny, nx, time_chunk, shard, spatial_chunk)
    g[VAR][:] = _data(time_chunk, ny, nx, seed=1)
    geom = assert_geometry(path, VAR, chunks=(time_chunk, spatial_chunk, spatial_chunk),
                           shards=(time_chunk, shard, shard), shape=(time_chunk, ny, nx))

    files_total, bytes_total = tree_size(path)
    before = snapshot_tree(path)
    t = time_chunk // 2
    write = timeit_ms(lambda: g[VAR].__setitem__((t, slice(None), slice(None)),
                                                 _data(1, ny, nx, seed=2)[0]), repeats=1)
    n_rw, b_rw = changed_files(before, snapshot_tree(path))

    one_day_logical = bytes_total / time_chunk
    amp = round(b_rw / one_day_logical, 1) if one_day_logical else None
    # shard files only (exclude zarr.json and friends)
    shard_files_total = sum(1 for k in before if not k.endswith("zarr.json"))
    return {
        "hypothesis": "H1",
        "geometry": geom,
        "block_days": time_chunk,
        "build_files": files_total,
        "build_bytes": bytes_total,
        "shard_files_total": shard_files_total,
        "shard_files_rewritten": n_rw,
        "bytes_rewritten": b_rw,
        "one_day_logical_bytes": int(one_day_logical),
        "amplification_vs_one_day": amp,
        "write_ms": write["p50_ms"],
        "h1_confirmed": bool(amp is not None and amp >= time_chunk * 0.5
                             and n_rw >= shard_files_total),
        "note": ("H1 holds when a one-day write rewrites every shard spanning the block, "
                 "i.e. amplification approaches time_chunk."),
    }


# --------------------------------------------------------------------------- H2
def measure_h2(workdir: str, *, ny: int = 512, nx: int = 512,
               tail_lengths: Sequence[int] = (1, 15, 30, 60, 89),
               time_chunk: int = 90, shard: int = 128, spatial_chunk: int = 8) -> dict:
    """For each tail length L: build an L-day PARTIAL block, append day L+1, measure."""
    rows = []
    for L in tail_lengths:
        path = _fresh(os.path.join(workdir, f"h2_{L}"))
        # clamp=False: the block declares the FULL time block, so days 1..L occupy one
        # PARTIAL shard -- the in-place-growth configuration H2 measures.
        g = _create(path, L, ny, nx, time_chunk, shard, spatial_chunk, clamp=False)
        g[VAR][:] = _data(L, ny, nx, seed=10 + L)
        _, bytes_L = tree_size(path)
        before = snapshot_tree(path)
        arr = g[VAR]
        arr.resize((L + 1, ny, nx))
        arr[L, :, :] = _data(1, ny, nx, seed=99)[0]
        n_rw, b_rw = changed_files(before, snapshot_tree(path))
        one_day_logical = bytes_L / L
        rows.append({"tail_len": L,
                     "base_bytes": bytes_L,
                     "shard_files_rewritten": n_rw,
                     "bytes_rewritten": b_rw,
                     "one_day_logical_bytes": int(one_day_logical),
                     "amplification_vs_one_day": round(b_rw / one_day_logical, 1)})
    amps = [r["amplification_vs_one_day"] for r in rows]
    grows = all(amps[i] <= amps[i + 1] for i in range(len(amps) - 1))
    # each append should rewrite roughly the whole partial shard => amp ~ tail_len
    whole_shard = all(r["amplification_vs_one_day"] >= r["tail_len"] * 0.5 for r in rows)
    total_cost = sum(range(1, time_chunk + 1))
    return {
        "hypothesis": "H2",
        "rows": rows,
        "monotonic_in_tail_length": grows,
        "rewrites_whole_partial_shard": whole_shard,
        "h2_confirmed": bool(grows and whole_shard),
        "projected_day_by_day_block_cost_x": round(total_cost / time_chunk, 1),
        "note": ("Growing a block one day at a time costs sum(1..S) day-writes ~= (S+1)/2 x "
                 "the block's final size -- the measured basis for rejecting candidate A."),
    }


# --------------------------------------------------------------------------- H3
def _h3_rows(workdir: str, tag: str, ny: int, nx: int, total_days: int,
             segment_counts: Sequence[int], shard: int, spatial_chunk: int,
             ii: int, jj: int, repeats: int) -> list:
    rows = []
    for nseg in segment_counts:
        per = total_days // nseg
        root = _fresh(os.path.join(workdir, f"h3_{tag}_{nseg}"))
        stores, handles = [], []
        for k in range(nseg):
            spath = os.path.join(root, f"seg{k}")
            st = CountingLocalStore(spath)
            g = _create(spath, per, ny, nx, min(90, per), shard, spatial_chunk, store=st)
            g[VAR][:] = _data(per, ny, nx, seed=200 + k)
            stores.append(st)
            handles.append(zarr.open_group(st, mode="r"))

        chunk_count = 0
        decompressed = 0
        for k in range(nseg):
            geom = read_geometry(os.path.join(root, f"seg{k}"), VAR)
            c = point_column_cost(geom, t0=0, t1=per, ii=ii, jj=jj)
            chunk_count += c["chunk_count"]
            decompressed += c["decompressed_bytes"]

        def read_all():
            return np.concatenate([np.asarray(h[VAR][:, ii, jj]) for h in handles])

        read_all()                                   # warm
        for st in stores:
            st.reset()
        vals = read_all()
        store_reads = sum(st.read_count for st in stores)
        lat = timeit_ms(read_all, repeats=repeats)
        files, nbytes = tree_size(root)
        rows.append({"segments": nseg, "days_per_segment": per,
                     "time_chunk": int(read_geometry(os.path.join(root, "seg0"),
                                                     VAR)["chunks"][0]),
                     "values_returned": int(vals.size),
                     "chunk_count": chunk_count,
                     "decompressed_bytes": decompressed,
                     "useful_bytes": total_days * 4,
                     "store_reads": store_reads,
                     "file_count": files, "bytes": nbytes,
                     "latency": lat})
    return rows


def _marginal_ms(rows) -> Optional[float]:
    if len(rows) < 2 or rows[-1]["segments"] <= 1:
        return None
    extra = rows[-1]["latency"]["p50_ms"] - rows[0]["latency"]["p50_ms"]
    return round(extra / (rows[-1]["segments"] - rows[0]["segments"]), 3)


def measure_h3(workdir: str, *, ny: int = 512, nx: int = 512, total_days: int = 360,
               segment_counts: Sequence[int] = (1, 2, 4),
               block_size_segments: Optional[Sequence[int]] = None,
               shard: int = 128, spatial_chunk: int = 8, ii: int = 137, jj: int = 301,
               repeats: int = 15) -> dict:
    """Serve the SAME total days from 1..k segments; compare analytic cost vs latency.

    Two regimes, kept SEPARATE -- an earlier version of this harness conflated them and
    reported a spurious H3 failure:

      * `segment_counts` -- **constant time chunk**: every segment holds >= `time_chunk`
        days, so the inner time chunk stays 90. This is H3 as stated in the spec: neither
        `chunk_count` nor `decompressed_bytes` may move; only per-segment call overhead.
      * `block_size_segments` (optional) -- **shrinking time chunk**: segments smaller than
        90 days necessarily shrink the inner time chunk, so `chunk_count` RISES while
        `decompressed_bytes` stays put. That is not an H3 violation; it is the §10.4 sizing
        input, and it is reported as such.
    """
    ii, jj = _clamp_point(ii, jj, ny, nx)
    rows = _h3_rows(workdir, "const", ny, nx, total_days, segment_counts,
                    shard, spatial_chunk, ii, jj, repeats)
    base = rows[0]
    const_tc = len({r["time_chunk"] for r in rows}) == 1
    same_chunks = all(r["chunk_count"] == base["chunk_count"] for r in rows)
    same_bytes = all(r["decompressed_bytes"] == base["decompressed_bytes"] for r in rows)
    all_complete = all(r["values_returned"] == total_days for r in rows)

    sweep = []
    if block_size_segments:
        sweep = _h3_rows(workdir, "sizing", ny, nx, total_days, block_size_segments,
                         shard, spatial_chunk, ii, jj, repeats)
    sweep_bytes_invariant = (all(r["decompressed_bytes"] == sweep[0]["decompressed_bytes"]
                                 for r in sweep) if sweep else None)

    return {
        "hypothesis": "H3",
        "total_days": total_days,
        "constant_time_chunk": {
            "rows": rows,
            "time_chunk_constant": const_tc,
            "chunk_count_invariant": same_chunks,
            "decompressed_bytes_invariant": same_bytes,
            "marginal_ms_per_extra_segment": _marginal_ms(rows),
        },
        "block_size_sweep": {
            "rows": sweep,
            "decompressed_bytes_invariant": sweep_bytes_invariant,
            "marginal_ms_per_extra_segment": _marginal_ms(sweep) if sweep else None,
        },
        "all_segmentations_return_all_days": all_complete,
        "h3_supported": bool(const_tc and same_chunks and same_bytes and all_complete
                             and (sweep_bytes_invariant in (None, True))),
        "note": ("H3 (refined by this harness): decompressed_bytes is invariant under "
                 "segmentation in EVERY regime; chunk_count is additionally invariant while "
                 "the inner time chunk is unchanged. A sub-90-day block shrinks the time "
                 "chunk, raising chunk_count at constant decompressed_bytes -- the §10.4 "
                 "sizing trade, not an H3 violation."),
    }


# --------------------------------------------------------------------------- H8
def measure_h8(workdir: str, *, ny: int = 512, nx: int = 512, hist_days: int = 90,
               tail_days: int = 30, shard: int = 128, spatial_chunk: int = 8,
               ii: int = 137, jj: int = 301, repeats: int = 15) -> dict:
    """Regular (90,8,8)/(90,128,128) vs zarr's rectilinear variants."""
    ii, jj = _clamp_point(ii, jj, ny, nx)
    total = hist_days + tail_days

    reg_path = _fresh(os.path.join(workdir, "h8_regular"))
    g = _create(reg_path, total, ny, nx, 90, shard, spatial_chunk)
    build_reg = timeit_ms(lambda: g[VAR].__setitem__(slice(None), _data(total, ny, nx, seed=7)),
                          repeats=1)
    greg = zarr.open_group(reg_path, mode="r")
    reg_files, reg_bytes = tree_size(reg_path)
    regular = {"build_ms": build_reg["p50_ms"], "files": reg_files, "bytes": reg_bytes,
               "read_series": timeit_ms(lambda: np.asarray(greg[VAR][0:total, ii, jj]), repeats),
               "read_1d": timeit_ms(lambda: np.asarray(greg[VAR][10:11, ii, jj]), repeats),
               "geometry": read_geometry(reg_path, VAR)}

    # (a) rectilinear CHUNKS + sharding
    rect_chunks = {"supported": None, "error": None}
    try:
        with zarr.config.set({"array.rectilinear_chunks": True}):
            p = _fresh(os.path.join(workdir, "h8_rect_chunks"))
            gg = zarr.open_group(p, mode="w", zarr_format=3)
            gg.create_array(VAR, shape=(total, ny, nx), dtype="float32",
                            chunks=([90] + [1] * tail_days,
                                    [spatial_chunk] * (ny // spatial_chunk),
                                    [spatial_chunk] * (nx // spatial_chunk)),
                            shards=(90, shard, shard), fill_value=float("nan"))
            rect_chunks["supported"] = True
    except Exception as exc:                          # noqa: BLE001 - we record the message
        rect_chunks = {"supported": False, "error": f"{type(exc).__name__}: {exc}"[:400]}

    # (b) rectilinear SHARDS (the form zarr points at) -- inner time chunk is forced to 1
    rect_shards = {"supported": None, "error": None}
    try:
        with zarr.config.set({"array.rectilinear_chunks": True}):
            p = _fresh(os.path.join(workdir, "h8_rect_shards"))
            gg = zarr.open_group(p, mode="w", zarr_format=3)
            arr = gg.create_array(VAR, shape=(total, ny, nx), dtype="float32",
                                  chunks=(1, spatial_chunk, spatial_chunk),
                                  shards=([hist_days] + [1] * tail_days,
                                          [shard] * (ny // shard), [shard] * (nx // shard)),
                                  fill_value=float("nan"))
            b = timeit_ms(lambda: arr.__setitem__(slice(0, hist_days),
                                                  _data(hist_days, ny, nx, seed=8)), repeats=1)
            for k in range(tail_days):
                arr[hist_days + k] = _data(1, ny, nx, seed=300 + k)[0]
            before = snapshot_tree(p)
            arr.resize((total + 1, ny, nx))
            app = timeit_ms(lambda: arr.__setitem__(total, _data(1, ny, nx, seed=999)[0]),
                            repeats=1)
            n_rw, b_rw = changed_files(before, snapshot_tree(p))
            gr = zarr.open_group(p, mode="r")
            files, nbytes = tree_size(p)
            rect_shards = {"supported": True,
                           "hist_build_ms": b["p50_ms"], "files": files, "bytes": nbytes,
                           "append_ms": app["p50_ms"],
                           "append_files_rewritten": n_rw, "append_bytes_rewritten": b_rw,
                           "read_series": timeit_ms(
                               lambda: np.asarray(gr[VAR][0:total, ii, jj]), repeats),
                           "read_1d": timeit_ms(
                               lambda: np.asarray(gr[VAR][10:11, ii, jj]), repeats)}
    except Exception as exc:                          # noqa: BLE001
        rect_shards = {"supported": False, "error": f"{type(exc).__name__}: {exc}"[:400]}

    regression = None
    if rect_shards.get("supported"):
        base = regular["read_series"]["p50_ms"]
        if base:
            regression = round(rect_shards["read_series"]["p50_ms"] / base, 1)
    verdict = ("rectilinear rejected for the base read layout"
               if (regression is None or regression > 1.25)
               else "RE-EXAMINE: rectilinear did not regress point-series reads")
    return {
        "hypothesis": "H8",
        "zarr_version": zarr.__version__,
        "regular": regular,
        "rect_chunks_with_sharding": rect_chunks,
        "rect_shards": rect_shards,
        "point_series_regression_x": regression,
        "verdict": verdict,
        "note": ("Rectilinear SHARDS force a uniform inner time chunk of 1, which is what "
                 "costs the point-series read. Re-test when zarr-python 3.3 stabilizes the "
                 "feature (spec §16-Q7)."),
    }


# --------------------------------------------------------------------------- driver
def run_all(workdir: str, *, ny: int, nx: int, total_days: int,
            segment_counts: Sequence[int], tail_lengths: Sequence[int],
            block_size_segments: Optional[Sequence[int]] = None) -> dict:
    results = {
        "harness": "bench_p5_rmw.py",
        "step": "P5-S0",
        "provenance": "COMMITTED harness, synthetic data, local, warm; supersedes the spec's [PROBE] numbers",
        "env": {"zarr": zarr.__version__, "python": sys.version.split()[0],
                "platform": platform.platform()},
        "grid": [ny, nx],
        "h1": measure_h1(workdir, ny=ny, nx=nx),
        "h2": measure_h2(workdir, ny=ny, nx=nx, tail_lengths=tail_lengths),
        "h3": measure_h3(workdir, ny=ny, nx=nx, total_days=total_days,
                         segment_counts=segment_counts,
                         block_size_segments=block_size_segments),
        "h8": measure_h8(workdir, ny=ny, nx=nx),
    }
    results["gate"] = evaluate_s0_gate(results)
    return results


def main():
    ap = argparse.ArgumentParser(description="P5-S0 RMW / segmentation / rectilinear harness")
    ap.add_argument("--workdir", default=None, help="scratch dir (default: a temp dir)")
    ap.add_argument("--ny", type=int, default=512)
    ap.add_argument("--nx", type=int, default=512)
    ap.add_argument("--total-days", type=int, default=360, dest="total_days")
    ap.add_argument("--segments", default="1,2,4",
                    help="constant-time-chunk regime: each segment holds >= time_chunk days")
    ap.add_argument("--block-size-segments", default="4,8,12", dest="block_size_segments",
                    help="sizing sweep: segment counts that shrink the inner time chunk")
    ap.add_argument("--tail-lengths", default="1,15,30,60,89", dest="tail_lengths")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    import tempfile
    workdir = args.workdir or tempfile.mkdtemp(prefix="p5s0_")
    res = run_all(workdir,
                  ny=args.ny, nx=args.nx, total_days=args.total_days,
                  segment_counts=[int(x) for x in args.segments.split(",")],
                  tail_lengths=[int(x) for x in args.tail_lengths.split(",")],
                  block_size_segments=[int(x) for x in args.block_size_segments.split(",")])
    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w") as fh:
            json.dump(res, fh, indent=2, sort_keys=True)
        print(f"wrote {args.out}")
    print(json.dumps(res["gate"], indent=2))
    print(f"H1 amplification: {res['h1']['amplification_vs_one_day']}x "
          f"({res['h1']['shard_files_rewritten']}/{res['h1']['shard_files_total']} shards)")
    print(f"H2 confirmed: {res['h2']['h2_confirmed']} "
          f"(day-by-day block cost ~{res['h2']['projected_day_by_day_block_cost_x']}x)")
    ctc = res["h3"]["constant_time_chunk"]
    print(f"H3 constant-time-chunk: chunk_count invariant={ctc['chunk_count_invariant']} "
          f"bytes invariant={ctc['decompressed_bytes_invariant']} "
          f"(+{ctc['marginal_ms_per_extra_segment']} ms per extra segment)")
    bs = res["h3"]["block_size_sweep"]
    if bs["rows"]:
        print(f"H3 block-size sweep: bytes invariant={bs['decompressed_bytes_invariant']}; "
              + ", ".join(f"{r['days_per_segment']}d->{r['chunk_count']} chunks"
                          f"/{r['latency']['p50_ms']}ms" for r in bs["rows"]))
    print(f"H8: {res['h8']['verdict']} (regression {res['h8']['point_series_regression_x']}x)")
    if res["gate"]["verdict"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
