"""dev2026 — P4-S0 PROTOTYPE: single-day reads from a time-cube (base or delta).

EXPERIMENTAL / benchmark-only (P4-S0 feasibility). Today single-day bbox / single-day point /
POST points all route to the DAILY store; this module prototypes serving them from the time-cube so
P4-S0 can measure whether the cube could replace the daily store for those paths. NOT wired into the
API; NOT a contract. Local/shadow only — no VM24 / production data is touched.

Semantics mirror the daily `StoreAccess` exactly so parity is meaningful:
  * cube_bbox_arrays  -> (lons,lats,cols) like StoreAccess.bbox_arrays (absent var omitted from cols;
                          the row builder then nulls it, matching the bbox path).
  * cube_point        -> like StoreAccess.point_series single day: absent var OMITS the key; NaN -> null.
  * cube_points_batch -> like StoreAccess.points_batch: absent var -> null (key present); NaN -> null.
"absent on day t" = the cube's per-(day,var) `var_valid[f][t]` is False (or var not in the cube).

`chunk_cost(...)` returns the cache-INDEPENDENT discriminators (chunk_count / decompressed_bytes /
read_amp) the P2 work established — computed analytically from the array chunk shape + access extent.
"""
from __future__ import annotations

import math
from typing import Dict, List, Sequence

import numpy as np

from .time_cube import _clamp, _nearest_idx


def _day_t(tc, day):
    if day not in tc._day_index:
        raise ValueError(f"day not in cube: {day}")
    return tc._day_index[day]


def _present(tc, f, t):
    if f not in tc.vars:
        return False
    flags = tc.var_valid.get(f)
    return True if flags is None else bool(flags[t])


def cube_bbox_arrays(tc, day, lon0, lat0, lon1, lat1, fields, stride: int = 1):
    t = _day_t(tc, day)
    lon, lat = tc._lon, tc._lat
    lo0 = _clamp(min(lon0, lon1), float(lon.min()), float(lon.max()))
    lo1 = _clamp(max(lon0, lon1), float(lon.min()), float(lon.max()))
    la0 = _clamp(min(lat0, lat1), float(lat.min()), float(lat.max()))
    la1 = _clamp(max(lat0, lat1), float(lat.min()), float(lat.max()))
    j0, j1 = sorted((_nearest_idx(lo0, lon), _nearest_idx(lo1, lon)))
    i0, i1 = sorted((_nearest_idx(la0, lat), _nearest_idx(la1, lat)))
    cols: Dict[str, np.ndarray] = {}
    for f in fields:
        if _present(tc, f, t):
            arr = tc._array(f)
            cols[f] = np.asarray(arr[t, i0:i1 + 1:stride, j0:j1 + 1:stride], dtype=np.float32)
    lats = lat[i0:i1 + 1:stride].astype(float)
    lons = lon[j0:j1 + 1:stride].astype(float)
    return lons, lats, cols


def cube_point(tc, day, lon, lat, fields) -> List[dict]:
    t = _day_t(tc, day)
    ii, jj, glon, glat = tc.nearest_indices(lon, lat)
    row = {"lon": glon, "lat": glat, "date": day}
    for f in fields:
        if _present(tc, f, t):                          # absent -> OMIT key (point_series parity)
            v = float(np.asarray(tc._array(f)[t, ii, jj]))
            row[f] = None if np.isnan(v) else v
    return [row]


def cube_points_batch(tc, day, points: Sequence[Sequence[float]], fields) -> List[dict]:
    t = _day_t(tc, day)
    lon, lat = tc._lon, tc._lat
    idx = []
    for lo, la in points:
        jj = _nearest_idx(_clamp(float(lo), float(lon.min()), float(lon.max())), lon)
        ii = _nearest_idx(_clamp(float(la), float(lat.min()), float(lat.max())), lat)
        idx.append((ii, jj, float(lon[jj]), float(lat[ii])))
    rows = []
    for k, (ii, jj, glon, glat) in enumerate(idx):
        row = {"index": k, "lon": glon, "lat": glat, "date": day}   # "index" matches points_batch
        for f in fields:                                # absent -> null key present (points_batch parity)
            if _present(tc, f, t):
                v = float(np.asarray(tc._array(f)[t, ii, jj]))
                row[f] = None if np.isnan(v) else v
            else:
                row[f] = None
        rows.append(row)
    return rows


def chunk_cost(chunks, ni: int, nj: int, n_present: int, access="bbox", ij_list=None) -> dict:
    """Cache-independent read cost for a single-day access. `chunks`=(ct,cy,cx) of the array.
    bbox: ni×nj contiguous block. point: ni=nj=1. batch: pass ij_list of (i,j) -> distinct chunks."""
    ct, cy, cx = int(chunks[0]), int(chunks[-2]), int(chunks[-1])
    if access == "batch" and ij_list is not None:
        distinct = {(i // cy, j // cx) for i, j in ij_list}
        n_chunks = len(distinct) * n_present
        useful = len(ij_list) * 4 * n_present
    else:
        n_chunks = math.ceil(ni / cy) * math.ceil(nj / cx) * n_present
        useful = ni * nj * 4 * n_present
    decompressed = n_chunks * (ct * cy * cx * 4)
    return {"chunk_count": n_chunks, "decompressed_bytes": decompressed,
            "useful_bytes": useful, "read_amp": round(decompressed / max(useful, 1), 1),
            "chunk_shape": [ct, cy, cx]}
