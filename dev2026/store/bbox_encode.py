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
                truncate: bool = False, bbox_requested=None) -> bytes:
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
                    truncate: bool = False, bbox_requested=None) -> bytes:
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


# --- P4-S2: CoverageJSON-lite (profile `ghrsst-raster-json-1`) -----------------------------------
# A valid CoverageJSON Coverage(Grid) subset (spec: specs/p4s2_raster_format_design.md). Compact
# start/stop/num axes; NdArray ranges = FLAT row-major (last axisName fastest) with null nodata; CRS84
# (x=lon, y=lat); y axis DESCENDS (north->south). ghrsst:-prefixed extension members carry field_status
# + bbox. NOTE: the `unit` labels below are metadata — the wire VALUES are emitted unchanged from the
# store (identical to the row `format=json` path), so parity holds regardless; confirm the label matches
# the actual MUR units at deployment (Codex P4-S2 impl-confirmation).
_CRS84 = "http://www.opengis.net/def/crs/OGC/1.3/CRS84"
_PARAM_META = {
    "sst":         {"type": "Parameter", "description": {"en": "Sea surface temperature"},
                    "unit": {"symbol": "K", "label": {"en": "kelvin"}},
                    "observedProperty": {"label": {"en": "Sea Surface Temperature"}}},
    "sst_anomaly": {"type": "Parameter", "description": {"en": "SST anomaly"},
                    "unit": {"symbol": "K", "label": {"en": "kelvin"}},
                    "observedProperty": {"label": {"en": "Sea Surface Temperature Anomaly"}}},
    "sea_ice":     {"type": "Parameter", "description": {"en": "Sea ice fraction"},
                    "unit": {"symbol": "1", "label": {"en": "fraction"}},
                    "observedProperty": {"label": {"en": "Sea Ice Area Fraction"}}},
}
# MUR nominal daily analysis time (UTC). Confirm against source metadata/filename at deployment.
_MUR_ANALYSIS_TIME = "T09:00:00Z"


def encode_coveragejson(lons, lats, cols: Dict[str, np.ndarray], fields: Sequence[str], day: str,
                        truncate: bool = False, bbox_requested=None) -> bytes:
    lon = _f32(lons); lat = _f32(lats)               # lon ascending W->E, lat ascending S->N
    if truncate:
        lon = np.round(lon, 5); lat = np.round(lat, 5)
    nlon, nlat = int(lon.size), int(lat.size)
    ranges: Dict[str, object] = {}
    parameters: Dict[str, object] = {}
    field_status: Dict[str, str] = {}
    for f in fields:
        if f in cols:                                # present -> full NdArray (NaN -> null via orjson)
            v = _f32(cols[f])
            if truncate:
                v = np.round(v, 3)
            values = v[::-1, :].ravel()              # flip lat rows to NORTH->south, then flat row-major
            ranges[f] = {"type": "NdArray", "dataType": "float", "axisNames": ["t", "y", "x"],
                         "shape": [1, nlat, nlon], "values": values}
            parameters[f] = _PARAM_META.get(f, {"type": "Parameter"})
            field_status[f] = "present"
        else:                                        # absent -> OMIT range (never null) + field_status
            field_status[f] = "absent"
    obj = {
        "type": "Coverage", "profile": "ghrsst-raster-json-1", "ghrsst:format_version": 1,
        "domain": {"type": "Domain", "domainType": "Grid",
                   "axes": {"x": {"start": float(lon[0]), "stop": float(lon[-1]), "num": nlon},
                            "y": {"start": float(lat[-1]), "stop": float(lat[0]), "num": nlat},  # N->S
                            "t": {"values": [f"{day}{_MUR_ANALYSIS_TIME}"]}},
                   "referencing": [{"coordinates": ["x", "y"], "system": {"type": "GeographicCRS", "id": _CRS84}},
                                   {"coordinates": ["t"], "system": {"type": "TemporalRS", "calendar": "Gregorian"}}]},
        "parameters": parameters,
        "ranges": ranges,
        "ghrsst:field_status": field_status,
        "ghrsst:bbox_actual": [float(lon[0]), float(lat[0]), float(lon[-1]), float(lat[-1])],
    }
    if bbox_requested is not None:
        obj["ghrsst:bbox_requested"] = [float(x) for x in bbox_requested]
    return orjson.dumps(obj, option=_NPOPT)


# `raster` is an accepted alias for `coveragejson` (both -> the ghrsst-raster-json-1 profile).
ENCODERS = {"grid": encode_grid, "columnar": encode_columnar,
            "coveragejson": encode_coveragejson, "raster": encode_coveragejson}
COMPACT_FORMATS = frozenset(ENCODERS)
ALL_FORMATS = frozenset({"json"}) | COMPACT_FORMATS
