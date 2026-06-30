"""dev2026 — P3-S1 compact bbox response encoders (opt-in `format=grid` / `format=columnar`).

EXPERIMENTAL / PROTOTYPE (Codex review of PR #16): these compact wire shapes are MEASUREMENT
prototypes, NOT a finalized contract. After the VM24 time-cube rebuild + storage review, P4
(`specs/p4_storage_policy_and_bbox_strategy.md`) will likely REDEFINE the compact format as
raster-style (explicit bbox_actual/nx/ny/x0/y0/dx/dy/crs/scan/index_formula, flat row-major arrays of
length nx*ny). Only `format=json` is a stable contract; do not build a frontend contract on `grid`
yet.

ONE canonical implementation shared by the API (`api/app.py`), the benches (`bench/bench_bbox_wire.py`,
`bench/bench_bbox_http.py`), and the parity tests — so the prototype never drifts. Default
`format=json` (the row-array streaming path) is untouched and lives in app.py.

Both compact formats encode straight from the 2-D `cols` arrays (no per-point dicts), so they skip the
row path's per-point dict construction (the P3-S0 server hot spot). Semantics (P3 spec §3 Tier 1):
  * requested var present  -> field_status='present', values as arrays; land/masked NaN -> JSON null.
  * requested var ABSENT   -> field_status='absent', fields[var]=null (NOT a giant null array).
Float rendering is orjson shortest-round-trip (e.g. 5.001) vs the row path's float(np.float32)
(5.000999…) — the SAME float32 value. Parity is therefore SEMANTIC (float32), not byte-identical
(spec §6). `truncate` mode mirrors the row path: lon/lat rounded to 5 dp, values to 3 dp.

  grid (preferred): {date, format:'grid', lon:[nlon], lat:[nlat], shape:[nlat,nlon],
                     fields:{var:[[...]]}, field_status:{var:'present'|'absent'}}
  columnar (flat) : {date, format:'columnar', lon:[npts], lat:[npts],
                     fields:{var:[...]}, field_status:{...}}   # lon/lat repeated per point
"""
from __future__ import annotations

from typing import Dict, Sequence

import numpy as np
import orjson

_NPOPT = orjson.OPT_SERIALIZE_NUMPY


def _f32(a):
    return np.asarray(a, dtype=np.float32)


def encode_grid(lons, lats, cols: Dict[str, np.ndarray], fields: Sequence[str], day: str,
                truncate: bool = False) -> bytes:
    lon = _f32(lons); lat = _f32(lats)
    if truncate:
        lon = np.round(lon, 5); lat = np.round(lat, 5)
    obj = {"date": day, "format": "grid", "lon": lon, "lat": lat,
           "shape": [int(lat.size), int(lon.size)], "fields": {}, "field_status": {}}
    for f in fields:
        if f in cols:
            v = _f32(cols[f])
            obj["fields"][f] = np.round(v, 3) if truncate else v   # 2-D; NaN -> null via orjson
            obj["field_status"][f] = "present"
        else:
            obj["fields"][f] = None
            obj["field_status"][f] = "absent"
    return orjson.dumps(obj, option=_NPOPT)


def encode_columnar(lons, lats, cols: Dict[str, np.ndarray], fields: Sequence[str], day: str,
                    truncate: bool = False) -> bytes:
    lon = _f32(lons); lat = _f32(lats)
    nlon, nlat = lon.size, lat.size
    lon_flat = np.tile(lon, nlat); lat_flat = np.repeat(lat, nlon)
    if truncate:
        lon_flat = np.round(lon_flat, 5); lat_flat = np.round(lat_flat, 5)
    obj = {"date": day, "format": "columnar", "lon": lon_flat, "lat": lat_flat,
           "fields": {}, "field_status": {}}
    for f in fields:
        if f in cols:
            v = _f32(cols[f]).ravel()
            obj["fields"][f] = np.round(v, 3) if truncate else v
            obj["field_status"][f] = "present"
        else:
            obj["fields"][f] = None
            obj["field_status"][f] = "absent"
    return orjson.dumps(obj, option=_NPOPT)


ENCODERS = {"grid": encode_grid, "columnar": encode_columnar}
COMPACT_FORMATS = frozenset(ENCODERS)
ALL_FORMATS = frozenset({"json"}) | COMPACT_FORMATS
