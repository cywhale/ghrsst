"""dev2026 — P5-S0: committed harness for hypotheses H1, H2, H3, H8.

Re-derives, with a COMMITTED and self-tested harness, the structural findings the P5 design
spec recorded as **[PROBE]** (scratch, uncommitted). §14 P5-S0 makes this a precondition:
no P5 step may advance on a scratch probe.

  H1  rewriting ONE day inside a full time block rewrites ~100 % of that block's shards
      (~time_chunk-fold write amplification)                       -> spec §3.A, candidate A
  H2  appending into a PARTIAL tail shard rewrites the whole partial shard, so growing a
      block day-by-day costs ~(S+1)/2x its final size              -> spec §3.A, §7
  H3  segmentation cost                                            -> spec §3.B, §10.4
      `measure_h3`:          at a CONSTANT inner time chunk, chunk_count and
                             decompressed_bytes are invariant; a smaller block shrinks the
                             time chunk, raising chunk_count at constant bytes
      `measure_h3_boundary`: bytes are equal segmented-vs-monolith ONLY for aligned,
                             full-span reads. Off-anchor windows change the boundary
                             over-read, so bytes DO move with block size and anchor.
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
    changed_split,
    evaluate_s0_gate,
    observed_data_keys,
    point_column_cost,
    read_geometry,
    snapshot_tree,
    timeit_ms,
    tree_size,
    tree_size_split,
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

    size = tree_size_split(path)
    before = snapshot_tree(path)
    t = time_chunk // 2
    write = timeit_ms(lambda: g[VAR].__setitem__((t, slice(None), slice(None)),
                                                 _data(1, ny, nx, seed=2)[0]), repeats=1)
    ch = changed_split(before, snapshot_tree(path))

    avg_stored_day = size["data_bytes"] / time_chunk
    logical_day = ny * nx * 4                          # one var-day, float32, uncompressed
    amp_stored = round(ch["data_bytes"] / avg_stored_day, 1) if avg_stored_day else None
    amp_logical = round(ch["data_bytes"] / logical_day, 1) if logical_day else None
    return {
        "hypothesis": "H1",
        "geometry": geom,
        "block_days": time_chunk,
        "data_shard_files_total": size["data_files"],
        "data_shard_files_rewritten": ch["data_files"],
        "metadata_files_changed": ch["metadata_files"],
        "stored_data_bytes": size["data_bytes"],
        "data_bytes_rewritten": ch["data_bytes"],
        "average_stored_bytes_per_day": int(avg_stored_day),
        "logical_uncompressed_bytes_per_day": int(logical_day),
        "amplification_vs_average_stored_day": amp_stored,
        "amplification_vs_uncompressed_day": amp_logical,
        "write_ms": write["p50_ms"],
        "h1_confirmed": bool(amp_stored is not None and amp_stored >= time_chunk * 0.5
                             and ch["data_files"] >= size["data_files"]),
        "note": ("H1 holds when a one-day write rewrites every DATA shard spanning the "
                 "block, i.e. amplification approaches time_chunk. The gate uses data-shard "
                 "metrics only; metadata churn is reported separately. Two amplification "
                 "bases are given because they answer different questions: vs the average "
                 "STORED (compressed) day, and vs one uncompressed var-day."),
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
        size = tree_size_split(path)
        before = snapshot_tree(path)
        arr = g[VAR]
        arr.resize((L + 1, ny, nx))
        arr[L, :, :] = _data(1, ny, nx, seed=99)[0]
        ch = changed_split(before, snapshot_tree(path))
        avg_stored_day = size["data_bytes"] / L
        logical_day = ny * nx * 4
        rows.append({
            "tail_len": L,
            "stored_data_bytes": size["data_bytes"],
            "data_shard_files_total": size["data_files"],
            "data_shard_files_rewritten": ch["data_files"],
            "metadata_files_changed": ch["metadata_files"],
            "data_bytes_rewritten": ch["data_bytes"],
            "average_stored_bytes_per_day": int(avg_stored_day),
            "logical_uncompressed_bytes_per_day": int(logical_day),
            "amplification_vs_average_stored_day": round(ch["data_bytes"] / avg_stored_day, 1),
            "amplification_vs_uncompressed_day": round(ch["data_bytes"] / logical_day, 1)})
    amps = [r["amplification_vs_average_stored_day"] for r in rows]
    grows = all(amps[i] <= amps[i + 1] for i in range(len(amps) - 1))
    # each append should rewrite roughly the whole partial shard => amp ~ tail_len
    whole_shard = all(r["amplification_vs_average_stored_day"] >= r["tail_len"] * 0.5
                      for r in rows)
    # ...and it must rewrite EVERY data shard, not merely a lot of bytes. Without this the
    # JSON gate could PASS on a partial-shard rewrite; only the unit test would have caught
    # it, so the artifact would disagree with CI.
    all_data_shards = all(r["data_shard_files_rewritten"] == r["data_shard_files_total"]
                          for r in rows)
    total_cost = sum(range(1, time_chunk + 1))
    return {
        "hypothesis": "H2",
        "rows": rows,
        "monotonic_in_tail_length": grows,
        "rewrites_whole_partial_shard": whole_shard,
        "all_data_shards_rewritten": all_data_shards,
        "h2_confirmed": bool(grows and whole_shard and all_data_shards),
        "projected_day_by_day_block_cost_x": round(total_cost / time_chunk, 1),
        "note": ("Growing a block one day at a time costs sum(1..S) day-writes ~= (S+1)/2 x "
                 "the block's final size -- the measured basis for rejecting candidate A. "
                 "The gate uses DATA-shard metrics; the array resize also rewrites "
                 "`zarr.json`, counted separately as metadata_files_changed."),
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


# --------------------------------------------------------------------------- H3 boundary sweep
def _segment_bounds(T: int, S: int, anchor: int) -> list:
    """Half-open [start, end) day ranges for S-day blocks anchored at `anchor`.

    `anchor` models the calendar anchor of the block grid (§5.2): days before it form a
    partial leading block, exactly as the legacy segment's tail does in production."""
    out = []
    if anchor > 0:
        out.append((0, min(anchor, T)))
    s = anchor
    while s < T:
        out.append((s, min(s + S, T)))
        s += S
    return [(a, b) for a, b in out if b > a]


def measure_h3_boundary(workdir: str, *, ny: int = 128, nx: int = 128, total_days: int = 450,
                        block_sizes: Sequence[int] = (30, 45, 90),
                        anchors: Sequence[int] = (0, 13),
                        windows: Optional[Sequence] = None,
                        ii: int = 5, jj: int = 7,
                        shard: int = 128, spatial_chunk: int = 8) -> dict:
    """Compare decompressed bytes SEGMENTED vs MONOLITH across request offsets and anchors.

    `measure_h3` shows the two cache-independent counters are invariant for an **aligned,
    full-span** read. That does NOT generalize: a window that starts mid-chunk over-reads a
    different amount under a different block grid. Reading `[84,450]` of a 450-day archive:

        monolith t90        -> 5 chunks x 90 = 450 day-cells
        S=45, anchor 0      -> 9 segments x 45 = 405 day-cells

    so bytes are **not** strictly invariant; they move with the boundary geometry. This
    function measures that directly instead of extrapolating, and validates each analytic
    figure against the chunk keys a real read touches.
    """
    ii, jj = _clamp_point(ii, jj, ny, nx)
    if windows is None:
        windows = [(0, total_days), (0, 360), (84, total_days), (225, 226), (100, 190)]

    mono_path = _fresh(os.path.join(workdir, "h3b_mono"))
    mono_store = CountingLocalStore(mono_path)
    gm = _create(mono_path, total_days, ny, nx, 90, shard, spatial_chunk, store=mono_store)
    gm[VAR][:] = _data(total_days, ny, nx, seed=400)
    mono_geom = read_geometry(mono_path, VAR)
    mono_handle = zarr.open_group(mono_store, mode="r")

    def observed_for(store, handle, t0, t1):
        store.reset()
        vals = np.asarray(handle[VAR][t0:t1, ii, jj])
        return int(vals.size), len(observed_data_keys(store))

    rows = []
    for S in block_sizes:
        for anchor in anchors:
            bounds = _segment_bounds(total_days, S, anchor)
            root = _fresh(os.path.join(workdir, f"h3b_S{S}_a{anchor}"))
            segs = []
            for k, (a, b) in enumerate(bounds):
                spath = os.path.join(root, f"seg{k}")
                st = CountingLocalStore(spath)
                g = _create(spath, b - a, ny, nx, 90, shard, spatial_chunk, store=st)
                g[VAR][:] = _data(b - a, ny, nx, seed=500 + k)
                segs.append({"bounds": (a, b), "path": spath, "store": st,
                             "handle": zarr.open_group(st, mode="r"),
                             "geom": read_geometry(spath, VAR)})
            for (t0, t1) in windows:
                mono_cost = point_column_cost(mono_geom, t0=t0, t1=t1, ii=ii, jj=jj)
                m_vals, m_obs = observed_for(mono_store, mono_handle, t0, t1)

                seg_chunks = seg_bytes = 0
                seg_vals = seg_obs = 0
                touched = 0
                for s in segs:
                    a, b = s["bounds"]
                    lo, hi = max(t0, a), min(t1, b)
                    if hi <= lo:
                        continue
                    touched += 1
                    c = point_column_cost(s["geom"], t0=lo - a, t1=hi - a, ii=ii, jj=jj)
                    seg_chunks += c["chunk_count"]
                    seg_bytes += c["decompressed_bytes"]
                    v, o = observed_for(s["store"], s["handle"], lo - a, hi - a)
                    seg_vals += v
                    seg_obs += o
                rows.append({
                    "block_size": S, "anchor": anchor, "window": [t0, t1],
                    "days_requested": t1 - t0,
                    "segments_touched": touched,
                    "monolith_chunk_count": mono_cost["chunk_count"],
                    "monolith_decompressed_bytes": mono_cost["decompressed_bytes"],
                    "monolith_analytic_matches_observed": mono_cost["chunk_count"] == m_obs,
                    "segmented_chunk_count": seg_chunks,
                    "segmented_decompressed_bytes": seg_bytes,
                    "segmented_analytic_matches_observed": seg_chunks == seg_obs,
                    "values_match": m_vals == seg_vals == (t1 - t0),
                    "bytes_ratio_segmented_over_monolith": round(
                        seg_bytes / mono_cost["decompressed_bytes"], 4)
                    if mono_cost["decompressed_bytes"] else None,
                })

    ratios = [r["bytes_ratio_segmented_over_monolith"] for r in rows
              if r["bytes_ratio_segmented_over_monolith"] is not None]
    aligned = [r for r in rows if r["anchor"] == 0 and r["window"] == [0, total_days]]
    aligned_equal = all(r["bytes_ratio_segmented_over_monolith"] == 1.0 for r in aligned)
    all_validated = all(r["monolith_analytic_matches_observed"]
                        and r["segmented_analytic_matches_observed"]
                        and r["values_match"] for r in rows)
    return {
        "hypothesis": "H3-boundary",
        "total_days": total_days,
        "rows": rows,
        "analytic_validated_against_observed": all_validated,
        "bytes_equal_in_aligned_full_span": aligned_equal,
        "bytes_ratio_min": min(ratios) if ratios else None,
        "bytes_ratio_max": max(ratios) if ratios else None,
        "bytes_can_differ_at_boundaries": bool(ratios and (min(ratios) < 1.0 or max(ratios) > 1.0)),
        "note": ("Decompressed bytes are equal segmented-vs-monolith ONLY for aligned, "
                 "full-span reads. Off-anchor windows change the over-read at the leading "
                 "and trailing boundary, so bytes move with block size and anchor. S6 must "
                 "adjudicate on the production calendar anchor and representative windows."),
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
            ch = changed_split(before, snapshot_tree(p))
            gr = zarr.open_group(p, mode="r")
            files, nbytes = tree_size(p)
            rect_shards = {"supported": True,
                           "hist_build_ms": b["p50_ms"], "files": files, "bytes": nbytes,
                           "append_ms": app["p50_ms"],
                           "append_data_files_rewritten": ch["data_files"],
                           "append_data_bytes_rewritten": ch["data_bytes"],
                           "append_metadata_files_changed": ch["metadata_files"],
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
        "h3_boundary": measure_h3_boundary(workdir),
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
    h1 = res["h1"]
    print(f"H1 amplification: {h1['amplification_vs_average_stored_day']}x stored / "
          f"{h1['amplification_vs_uncompressed_day']}x uncompressed "
          f"({h1['data_shard_files_rewritten']}/{h1['data_shard_files_total']} data shards, "
          f"+{h1['metadata_files_changed']} metadata)")
    print(f"H2 confirmed: {res['h2']['h2_confirmed']} "
          f"(day-by-day block cost ~{res['h2']['projected_day_by_day_block_cost_x']}x)")
    ctc = res["h3"]["constant_time_chunk"]
    print(f"H3 constant-time-chunk: chunk_count invariant={ctc['chunk_count_invariant']} "
          f"bytes invariant={ctc['decompressed_bytes_invariant']} "
          f"(+{ctc['marginal_ms_per_extra_segment']} ms per extra segment)")
    bs = res["h3"]["block_size_sweep"]
    if bs["rows"]:
        print(f"H3 block-size sweep (same layout, aligned): bytes invariant="
              f"{bs['decompressed_bytes_invariant']}; "
              + ", ".join(f"{r['days_per_segment']}d->{r['chunk_count']} chunks"
                          f"/{r['latency']['p50_ms']}ms" for r in bs["rows"]))
    hb = res["h3_boundary"]
    print(f"H3 boundary sweep: analytic==observed={hb['analytic_validated_against_observed']}; "
          f"aligned full-span bytes equal={hb['bytes_equal_in_aligned_full_span']}; "
          f"segmented/monolith byte ratio {hb['bytes_ratio_min']}..{hb['bytes_ratio_max']} "
          f"(differ at boundaries={hb['bytes_can_differ_at_boundaries']})")
    print(f"H8: {res['h8']['verdict']} (regression {res['h8']['point_series_regression_x']}x)")
    if res["gate"]["verdict"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
