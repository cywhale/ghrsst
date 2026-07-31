"""dev2026 — P5 test fixtures (F1-F3, F6, F11, F12 of the design spec §13.1).

Shared builders for the segmented-base tests. NOT collected by
`unittest discover -p "test_*.py"` -- it is a helper module, imported by the P5 test files.

Everything here is synthetic and lives in a caller-supplied temp directory. No GHRSST store,
no VM24 path, no env store is ever opened.
"""
from __future__ import annotations

import json
import os
from datetime import date, timedelta
from typing import Optional, Sequence

import numpy as np
import zarr

VARS = ("sst", "sst_anomaly", "sea_ice")
BASE_SPATIAL, BASE_TIME, BASE_SHARD = 8, 90, 128


def days_from(start: str, n: int) -> list:
    d0 = date.fromisoformat(start)
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]


def calendar_span(start: str, end: str) -> list:
    d0, d1 = date.fromisoformat(start), date.fromisoformat(end)
    out = []
    while d0 <= d1:
        out.append(d0.isoformat())
        d0 += timedelta(days=1)
    return out


def _field(T, ny, nx, seed):
    """Smooth, sst-like: a per-day constant plus a spatial gradient, so a point read has a
    predictable value per day (tests assert values, not just shapes)."""
    rng = np.random.default_rng(seed)
    lat_g = np.linspace(0, 1, ny)[None, :, None]
    lon_g = np.linspace(0, 1, nx)[None, None, :]
    t_g = np.arange(T, dtype=np.float32)[:, None, None]
    return (10.0 + 5.0 * lat_g + 2.0 * lon_g + t_g
            + rng.normal(0, 0.01, (T, ny, nx))).astype(np.float32)


def build_block(path: str, days: Sequence[str], ny: int = 32, nx: int = 32, *,
                vars_: Sequence[str] = VARS,
                absent_vars: Sequence[str] = (),
                nan_cells: Optional[Sequence[tuple]] = None,
                seed: int = 0, time_chunk: int = BASE_TIME) -> str:
    """A base block / legacy monolith in the production read layout (s8 / t<=90 / shard128).

    `absent_vars` are omitted entirely (P1 omit semantics, `var_valid` False);
    `nan_cells` are (day_index, ii, jj) triples set to NaN (land -> null semantics).
    """
    days = list(days)
    T = len(days)
    tc = min(time_chunk, T) if T else 1
    g = zarr.open_group(path, mode="w", zarr_format=3)
    g.create_array("lon", shape=(nx,), dtype="float32", chunks=(nx,))
    g["lon"][:] = np.linspace(100.0, 100.0 + nx - 1, nx, dtype=np.float32)
    g.create_array("lat", shape=(ny,), dtype="float32", chunks=(ny,))
    g["lat"][:] = np.linspace(0.0, ny - 1.0, ny, dtype=np.float32)
    present = [v for v in vars_ if v not in absent_vars]
    for i, v in enumerate(present):
        g.create_array(v, shape=(T, ny, nx), dtype="float32",
                       chunks=(tc, BASE_SPATIAL, BASE_SPATIAL),
                       shards=(tc, min(BASE_SHARD, ny), min(BASE_SHARD, nx)),
                       fill_value=float("nan"))
        block = _field(T, ny, nx, seed=seed + i * 101)
        for (t, ii, jj) in (nan_cells or []):
            if 0 <= t < T:
                block[t, ii, jj] = np.nan
        g[v][:] = block
    g.attrs["days"] = days
    g.attrs["vars"] = list(present)
    g.attrs["var_valid"] = {v: [True] * T for v in present}
    g.attrs["layout"] = "time_lat_lon"
    return path


def build_delta(path: str, days: Sequence[str], ny: int = 32, nx: int = 32,
                seed: int = 900) -> str:
    """F3 -- an append-optimized delta (t1/s256-shaped, clipped to the fixture grid).

    Present so tests can prove the segmented store NEVER touches it."""
    days = list(days)
    T = len(days)
    g = zarr.open_group(path, mode="w", zarr_format=3)
    g.create_array("lon", shape=(nx,), dtype="float32", chunks=(nx,))
    g["lon"][:] = np.linspace(100.0, 100.0 + nx - 1, nx, dtype=np.float32)
    g.create_array("lat", shape=(ny,), dtype="float32", chunks=(ny,))
    g["lat"][:] = np.linspace(0.0, ny - 1.0, ny, dtype=np.float32)
    for i, v in enumerate(VARS):
        g.create_array(v, shape=(T, ny, nx), dtype="float32",
                       chunks=(1, ny, nx), shards=(1, ny, nx), fill_value=float("nan"))
        g[v][:] = _field(T, ny, nx, seed=seed + i * 7) + 1000.0   # distinguishable values
    g.attrs["days"] = days
    g.attrs["vars"] = list(VARS)
    g.attrs["var_valid"] = {v: [True] * T for v in VARS}
    g.attrs["layout"] = "time_lat_lon"
    return path


def read_days(path: str) -> list:
    return list(zarr.open_group(path, mode="r").attrs["days"])


def corrupt_days(path: str, days: Sequence[str]) -> None:
    """F11 -- make the store disagree with what a manifest declares."""
    g = zarr.open_group(path, mode="a")
    g.attrs["days"] = list(days)


def write_json(path: str, obj) -> str:
    with open(path, "w") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
    return path


def make_root(tmpdir: str) -> str:
    root = os.path.join(tmpdir, "base_blocks")
    os.makedirs(root, exist_ok=True)
    return root
