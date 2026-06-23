"""dev2026 — P2-S3: TimeCubeStore — point/range time-series read over a time-cube.

Reads a point's time-series from a `(time, lat, lon)` cube (built by
ingest/build_timecube.py): resolve (ii,jj) once, map requested days -> time indices,
read the time-column `arr[t_slice, ii, jj]`. This touches O(days/time_chunk) chunks of
(time_chunk × small_spatial × small_spatial) — the structural win over the daily store's
O(days) × 1024² chunks.

Field-absence: the cube carries the vars present at build time (group attr "vars"). A
requested field not in the cube is OMITTED (P1 parity). Per-day var presence on REAL data
(e.g. a day lacking sst_anomaly) is a P2-S4 design item — for S3's structural proof the
synthetic fixtures have all vars on all days. Value parity vs the daily store is tested.
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Dict, List, Optional, Sequence

import numpy as np
import zarr

ALLOWED_FIELDS = ("sst", "sst_anomaly", "sea_ice")


def _nearest_idx(val, arr):
    j = int(np.searchsorted(arr, val))
    if j <= 0:
        return 0
    if j >= arr.size:
        return arr.size - 1
    return j if abs(arr[j] - val) < abs(arr[j - 1] - val) else j - 1


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


class TimeCubeStore:
    def __init__(self, cube_path: str):
        self.path = cube_path
        self._g = zarr.open_group(cube_path, mode="r")
        self._lon = np.asarray(self._g["lon"][:])
        self._lat = np.asarray(self._g["lat"][:])
        days = list(self._g.attrs["days"])
        self._day_index = {d: i for i, d in enumerate(days)}
        self.days = days
        self.vars = list(self._g.attrs.get("vars", ALLOWED_FIELDS))
        # per-(day,var) validity: distinguishes ABSENT (omit, P1 parity) from
        # present-but-NaN land (null). Missing mask -> treat all present (back-compat).
        self.var_valid = {v: list(flags) for v, flags in
                          dict(self._g.attrs.get("var_valid", {})).items()}
        self._lock = threading.Lock()
        self._arr = {}   # var -> zarr.Array (handles are read-only/concurrent-safe)

    @property
    def latest(self) -> Optional[str]:
        return self.days[-1] if self.days else None

    @property
    def day_count(self) -> int:
        return len(self.days)

    def covers_days(self, days: Sequence[str]) -> bool:
        """True iff every given day is present in the cube (dual-write coverage check)."""
        return all(d in self._day_index for d in days)

    def _array(self, var: str):
        with self._lock:
            a = self._arr.get(var)
            if a is None and var in self.vars:
                a = self._g[var]
                self._arr[var] = a
            return a

    def nearest_indices(self, lon, lat):
        lo = _clamp(float(lon), float(self._lon.min()), float(self._lon.max()))
        la = _clamp(float(lat), float(self._lat.min()), float(self._lat.max()))
        jj = _nearest_idx(lo, self._lon)
        ii = _nearest_idx(la, self._lat)
        return ii, jj, float(self._lon[jj]), float(self._lat[ii])

    @staticmethod
    def _check_fields(fields):
        bad = [f for f in fields if f not in ALLOWED_FIELDS]
        if bad:
            raise ValueError(f"Unsupported field(s): {','.join(bad)}")
        return list(fields)

    def point_series(self, lon, lat, days: Sequence[str], fields: Sequence[str]) -> List[dict]:
        fields = self._check_fields(fields)
        ii, jj, glon, glat = self.nearest_indices(lon, lat)
        # requested days that exist in the cube, in requested order
        idx = [(d, self._day_index[d]) for d in days if d in self._day_index]
        if not idx:
            return []
        t_indices = [t for _, t in idx]
        # read each field's time-column for this (ii,jj) in ONE indexed read
        # (contiguous range -> a slice; else fancy index). Both touch only the
        # chunks covering (t-range, ii_chunk, jj_chunk): O(days/time_chunk).
        t0, t1 = min(t_indices), max(t_indices)
        contiguous = (t1 - t0 + 1) == len(t_indices)
        colvals: Dict[str, np.ndarray] = {}
        for f in fields:
            arr = self._array(f)
            if arr is None:
                continue                      # absent field -> omit (P1 parity)
            if contiguous:
                col = np.asarray(arr[t0:t1 + 1, ii, jj])
                colvals[f] = col
            else:
                colvals[f] = np.asarray(arr[t_indices, ii, jj])
        rows = []
        for k, (day, t) in enumerate(idx):
            row = {"lon": glon, "lat": glat, "date": day}
            pos = (t - t0) if contiguous else k
            for f in fields:
                if f not in colvals:
                    continue                       # field never in cube -> omit
                valid = self.var_valid.get(f)
                if valid is not None and not valid[t]:
                    continue                       # var ABSENT on this day -> omit (P1 parity)
                v = float(colvals[f][pos])
                row[f] = None if np.isnan(v) else v   # present-but-NaN land -> null
            rows.append(row)
        return rows
