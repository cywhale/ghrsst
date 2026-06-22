"""dev2026 — P2-S0a: build a synthetic/regional daily-group fixture (Fixture A).

Fixture A is for **pre-gate / CI / candidate comparison only** — NOT a promotion gate
(spec phase2_timecube_design.md §5.1). It produces a daily-group Zarr v3 store in the
SAME layout as production (`/YYYY/MM/DD` groups with lon/lat/sst/sst_anomaly/sea_ice,
spatial-chunked like prod) over N CONTIGUOUS days, so the time-fan-out critical path can
be exercised at full time length without needing 365 real contiguous days locally.

Synthetic data uses a smooth field + low-amplitude noise (moderate, sst-like
compressibility). It CANNOT certify real compression/decompression behaviour — that is
why Fixture B (real data) is the promotion gate. A `fixture_meta.json` records day
count, contiguity, real-vs-synthetic composition, grid, and chunk shape.

NOTE: this builder is currently **synthetic-only** (composition.real = 0). Real+synth
seeding (copy real days from a source mur.zarr where available, synthesize the rest) is
deferred until needed — Fixture A is never promotion-eligible, so the real-data path is
Fixture B (`bench_timecube_microcost.py --fixture-b`), not this tool.

Run:
  dev2026/.venv/bin/python dev2026/ingest/build_fixture.py \
    --out /tmp/fixtureA --days 365 --ny 256 --nx 256 --chunk 256 --start 2024-01-01
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta

import numpy as np
import zarr

VARS = ("sst", "sst_anomaly", "sea_ice")


def _synth_day(ny, nx, day_index, rng):
    """Smooth lat/lon gradient + small day drift + low noise -> moderate compressibility."""
    lat_g = np.linspace(0, 1, ny)[:, None]
    lon_g = np.linspace(0, 1, nx)[None, :]
    base = 10.0 + 18.0 * lat_g + 2.0 * np.sin(6.28 * lon_g) + 0.01 * day_index
    noise = rng.normal(0, 0.05, size=(ny, nx))
    sst = (base + noise).astype(np.float32)
    sst[ny // 5, nx // 7] = np.float32(np.nan)            # a land/NaN cell
    anomaly = (0.2 * np.sin(6.28 * (lat_g + 0.001 * day_index)) + noise * 0.5).astype(np.float32)
    ice = np.zeros((ny, nx), np.float32)
    return {"sst": sst, "sst_anomaly": anomaly, "sea_ice": ice}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--start", default="2024-01-01", help="first day (contiguous from here)")
    ap.add_argument("--ny", type=int, default=256)
    ap.add_argument("--nx", type=int, default=256)
    ap.add_argument("--chunk", type=int, default=256, help="spatial chunk (clamped to grid)")
    ap.add_argument("--lat0", type=float, default=0.0)
    ap.add_argument("--lat1", type=float, default=30.0)
    ap.add_argument("--lon0", type=float, default=100.0)
    ap.add_argument("--lon1", type=float, default=130.0)
    ap.add_argument("--seed", type=int, default=12345)
    args = ap.parse_args()

    cy = min(args.chunk, args.ny)
    cx = min(args.chunk, args.nx)
    lon = np.linspace(args.lon0, args.lon1, args.nx).astype(np.float32)
    lat = np.linspace(args.lat0, args.lat1, args.ny).astype(np.float32)
    rng = np.random.default_rng(args.seed)
    d0 = datetime.strptime(args.start, "%Y-%m-%d").date()
    days = [(d0 + timedelta(days=i)).isoformat() for i in range(args.days)]

    os.makedirs(args.out, exist_ok=True)
    for i, day in enumerate(days):
        y, m, d = day.split("-")
        g = zarr.open_group(os.path.join(args.out, y, m, d), mode="w", zarr_format=3)
        g.create_array("lon", shape=(args.nx,), dtype="float32", chunks=(args.nx,))
        g.create_array("lat", shape=(args.ny,), dtype="float32", chunks=(args.ny,))
        g["lon"][:] = lon
        g["lat"][:] = lat
        data = _synth_day(args.ny, args.nx, i, rng)
        for v in VARS:
            g.create_array(v, shape=(1, args.ny, args.nx), dtype="float32", chunks=(1, cy, cx))
            g[v][0] = data[v]
        if (i + 1) % 50 == 0 or i == len(days) - 1:
            print(f"  built {i + 1}/{len(days)} days", flush=True)

    meta = {
        "fixture_kind": "A_synthetic",
        "promotion_gate_eligible": False,
        "note": "synthetic-fill; pre-gate/CI only. Real compression/decompression behaviour "
                "NOT certified — Fixture B (real data) is the promotion gate (spec §5.1).",
        "days": len(days), "start": days[0], "end": days[-1], "contiguous": True,
        "composition": {"real": 0, "synthetic": len(days)},
        "grid": {"ny": args.ny, "nx": args.nx, "lat": [args.lat0, args.lat1], "lon": [args.lon0, args.lon1]},
        "chunk": [1, cy, cx], "vars": list(VARS), "seed": args.seed,
    }
    with open(os.path.join(args.out, "fixture_meta.json"), "w") as fh:
        json.dump(meta, fh, indent=2)
    print(f"fixture A: {len(days)} contiguous days {days[0]}..{days[-1]} -> {args.out}")
    print(f"meta -> {os.path.join(args.out, 'fixture_meta.json')}")


if __name__ == "__main__":
    main()
