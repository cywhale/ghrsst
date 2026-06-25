"""dev2026 — production daily-ingest CLI: tiled append of one day into the delta cube.

Wraps `dual_write.append_to_delta` (tiled/streaming, bounded workers, checkpoint/resume — never
materializes a full global array). Intended for the daily ingest cron. Prints a JSON result
(op, append_s, tile_count, max_block_cells, grid_cells, rss_mb).

Run:
  dev2026/.venv/bin/python dev2026/ingest/append_delta_day.py \
    --daily /srv/ghrsst/data/mur.zarr --delta /srv/ghrsst/data/mur.delta.zarr \
    --day 2026-06-23 --workers 4
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ingest.dual_write import append_to_delta, DELTA_SPATIAL_CHUNK, DELTA_SHARD_SPATIAL  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", required=True)
    ap.add_argument("--delta", required=True)
    ap.add_argument("--day", required=True)
    # APPEND-optimized delta layout (decoupled from the base read-cube's s8/shard128); only applied
    # at delta CREATE — on append the existing delta's layout is used.
    ap.add_argument("--delta-spatial-chunk", type=int, default=DELTA_SPATIAL_CHUNK, dest="spatial_chunk")
    ap.add_argument("--delta-shard-spatial", type=int, default=DELTA_SHARD_SPATIAL, dest="shard_spatial")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--read-block", type=int, default=None, dest="read_block")
    ap.add_argument("--region", default=None, help="i0,i1,j0,j1 lat/lon index slice")
    args = ap.parse_args()
    region = tuple(int(x) for x in args.region.split(",")) if args.region else None
    res = append_to_delta(args.daily, args.delta, args.day, spatial_chunk=args.spatial_chunk,
                          shard_spatial=args.shard_spatial, region=region,
                          workers=args.workers, read_block=args.read_block)
    print(json.dumps(res))
    # memory-safety invariant: on any multi-tile (production) grid the peak per-worker block is a
    # single read tile, strictly smaller than the full grid (no full-global materialization).
    if res["tile_count"] > 1:
        assert res["max_block_cells"] < res["grid_cells"], "block materialized the full grid"


if __name__ == "__main__":
    main()
