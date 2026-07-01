"""dev2026 — P3-S0 bbox wire-format critical-path breakdown (server side).

Measures, per bbox size (250k/750k/1M points x 3 vars), the segments the P3 spec hypothesises:
  T_read    : zarr slice -> numpy (the I/O part)
  T_rows    : bbox_rows_window dict construction (current ROW path only)
  T_encode  : orjson encode (row / flat-columnar / grid-aware)
  bytes     : uncompressed payload size
  gzip      : zlib-level-6 size = LOCAL ESTIMATE of NGINX gzip (brotli is verified on VM24 with
              `curl --compressed`, recorded in the runbook; NGINX already does br/gzip — we do NOT add)

It encodes THREE formats so S1/S2 can compare directly: `row` (today's default), `columnar` (flat,
comparison), `grid` (axes once + 2-D fields + field_status; the spec's PRIMARY candidate). NO API
change — this is measurement only; the shipped encoders land in S1. Production 300k cap is bypassed
(direct store/encoder calls, per spec §4).

Writes:
  bench/results/p3s0_bbox_breakdown_<UTCDATE>.json   (machine-readable, Codex round-2 #2)
  bench/results/payloads/bbox_<N>_<fmt>.json         (for the client harness, Codex round-2 #1)
and prints a markdown table for specs/p3s0_bbox_breakdown.md.

Run: dev2026/.venv/bin/python dev2026/bench/bench_bbox_wire.py [--date YYYYMMDD]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import zlib

import numpy as np
import orjson
import zarr

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from store.zarr_paths import group_path  # noqa: E402
from store.store_access import StoreAccess  # noqa: E402
from store.bbox_encode import (encode_columnar as _enc_col_canon, encode_grid as _enc_grid_canon,  # noqa: E402
                               encode_coveragejson as _enc_cov)

VARS = ("sst", "sst_anomaly", "sea_ice")
SIZES = [("250k", 500, 500), ("750k", 1000, 750), ("1M", 1000, 1000)]
RESULTS = os.path.join(HERE, "results")
PAYLOADS = os.path.join(RESULTS, "payloads")
_NPOPT = orjson.OPT_SERIALIZE_NUMPY


def _build_day(root, day, G):
    g = zarr.open_group(group_path(root, day), mode="w", zarr_format=3)
    g.create_array("lon", shape=(G,), dtype="float32", chunks=(G,)); g["lon"][:] = np.linspace(100, 180, G).astype(np.float32)
    g.create_array("lat", shape=(G,), dtype="float32", chunks=(G,)); g["lat"][:] = np.linspace(-10, 30, G).astype(np.float32)
    rng = np.random.default_rng(0)
    for k, v in enumerate(VARS):
        field = (5 + k + 0.01 * np.arange(G)[:, None] + 0.001 * np.arange(G)[None, :]).astype(np.float32)
        mask = rng.random((G, G)) < 0.05            # ~5% land/masked NaN -> JSON null
        field[mask] = np.nan
        g.create_array(v, shape=(1, G, G), dtype="float32", chunks=(1, 1024, 1024)); g[v][0] = field


def _gzip(b):
    return len(zlib.compress(b, 6))


def _enc_row(lons, lats, cols, day):
    rows = StoreAccess.bbox_rows_window(lons, lats, cols, VARS, day, 0, lons.size * lats.size)
    return rows


# Delegate to the CANONICAL encoders (store/bbox_encode.py) so the bench measures the exact bytes the
# API ships — no prototype drift. Thin wrappers keep the (lons,lats,cols,day) call sites + the S0 test.
def _enc_columnar(lons, lats, cols, day):
    return _enc_col_canon(lons, lats, cols, VARS, day)


def _enc_grid(lons, lats, cols, day):
    return _enc_grid_canon(lons, lats, cols, VARS, day)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="UTC date tag YYYYMMDD for the artifact filename")
    args = ap.parse_args()
    date_tag = args.date or os.environ.get("P3S0_DATE") or "unknown"
    os.makedirs(PAYLOADS, exist_ok=True)

    tmp = tempfile.mkdtemp(prefix="p3s0_")
    day = "2026-01-01"
    G = 2048
    _build_day(tmp, day, G)
    gd = zarr.open_group(group_path(tmp, day), mode="r")
    lon_full = np.asarray(gd["lon"][:]); lat_full = np.asarray(gd["lat"][:])

    out = {"date_tag": date_tag, "grid": G, "vars": list(VARS),
           "gzip_note": "gzip=zlib L6 LOCAL ESTIMATE; brotli/Content-Encoding verified on VM24 (curl --compressed)",
           "sizes": []}
    print(f"# P3-S0 bbox wire breakdown (synthetic {G}x{G} day, 3 vars, ~5% NaN)\n")
    hdr = ("| size | points | T_read ms | T_rows ms | fmt | T_encode ms | MB | gzip MB | "
           "vs row bytes |")
    print(hdr); print("|" + "---|" * 8)

    for label, ni, nj in SIZES:
        t = time.perf_counter()
        cols = {v: np.asarray(gd[v][0, 0:ni, 0:nj]) for v in VARS}
        lons = lon_full[0:nj]; lats = lat_full[0:ni]
        t_read = (time.perf_counter() - t) * 1000
        npoints = ni * nj

        # ROW (current default): rows dicts then orjson
        t = time.perf_counter(); rows = _enc_row(lons, lats, cols, day); t_rows = (time.perf_counter() - t) * 1000
        t = time.perf_counter(); row_bytes = orjson.dumps(rows); t_enc_row = (time.perf_counter() - t) * 1000
        # COLUMNAR (flat, comparison)
        t = time.perf_counter(); col_bytes = _enc_columnar(lons, lats, cols, day); t_enc_col = (time.perf_counter() - t) * 1000
        # GRID (comparison)
        t = time.perf_counter(); grid_bytes = _enc_grid(lons, lats, cols, day); t_enc_grid = (time.perf_counter() - t) * 1000
        # COVERAGEJSON-lite (P4-S2 finalized format)
        t = time.perf_counter(); cov_bytes = _enc_cov(lons, lats, cols, VARS, day); t_enc_cov = (time.perf_counter() - t) * 1000

        entry = {"label": label, "ni": ni, "nj": nj, "points": npoints,
                 "T_read_ms": round(t_read, 1), "T_rows_ms": round(t_rows, 1),
                 "formats": {}}
        for fmt, payload, t_enc in (("row", row_bytes, t_enc_row),
                                    ("columnar", col_bytes, t_enc_col),
                                    ("grid", grid_bytes, t_enc_grid),
                                    ("coveragejson", cov_bytes, t_enc_cov)):
            gz = _gzip(payload)
            entry["formats"][fmt] = {"bytes": len(payload), "gzip_bytes": gz,
                                     "T_encode_ms": round(t_enc, 1)}
            ratio = len(payload) / len(row_bytes)
            print(f"| {label} | {npoints:,} | {t_read:.1f} | {t_rows if fmt=='row' else 0:.1f} | "
                  f"{fmt} | {t_enc:.1f} | {len(payload)/1e6:.1f} | {gz/1e6:.2f} | {ratio:.2f}× |")
            # save the 750k payloads for the client harness (the freeze point)
            if label == "750k":
                with open(os.path.join(PAYLOADS, f"bbox_{label}_{fmt}.json"), "wb") as fh:
                    fh.write(payload)
        out["sizes"].append(entry)

    art = os.path.join(RESULTS, f"p3s0_bbox_breakdown_{date_tag}.json")
    with open(art, "w") as fh:
        json.dump(out, fh, indent=2)
    print(f"\nartifact -> {os.path.relpath(art, os.path.join(HERE, '..'))}")
    print(f"payloads -> {os.path.relpath(PAYLOADS, os.path.join(HERE, '..'))}/bbox_750k_<fmt>.json")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
