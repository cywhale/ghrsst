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
import time
from datetime import datetime
from typing import Dict, List, NamedTuple, Optional, Sequence

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


class _Meta(NamedTuple):
    """Immutable metadata snapshot. refresh() builds a NEW _Meta and swaps `self._meta` atomically, so a
    reader that captures `m = self._meta` ONCE sees fully-consistent metadata for its whole call — never a
    torn mix of new day_index with old var_valid / half-swapped array cache (Codex P4-S3 concurrency fix)."""
    g: object
    lon: np.ndarray
    lat: np.ndarray
    days: List[str]               # PHYSICAL/append order (day_index maps to the stored time index)
    day_index: Dict[str, int]
    vars: List[str]
    var_valid: Dict[str, list]
    arr: Dict[str, object]        # var -> zarr.Array handle (precomputed; immutable)
    latest_day: Optional[str]     # CHRONOLOGICAL max(days) — days may be append-order after backfills


class TimeCubeStore:
    def __init__(self, cube_path: str):
        self.path = cube_path
        self._lock = threading.Lock()          # serializes refresh builds; readers are lock-free
        self._meta = self._build_meta()
        self._last_refresh = time.monotonic()

    def _build_meta(self) -> _Meta:
        """Open the group + read ALL metadata into one immutable snapshot. `append_to_delta` finalizes
        `attrs['days']` LAST, so this only exposes fully-appended, VALIDATED days — never a partial one."""
        g = zarr.open_group(self.path, mode="r")
        lon = np.asarray(g["lon"][:]); lat = np.asarray(g["lat"][:])
        days = list(g.attrs["days"])
        vars_ = list(g.attrs.get("vars", ALLOWED_FIELDS))
        # per-(day,var) validity: ABSENT (omit, P1 parity) vs present-but-NaN land (null).
        var_valid = {v: list(flags) for v, flags in dict(g.attrs.get("var_valid", {})).items()}
        arr = {v: g[v] for v in vars_ if v in g}
        # `latest` must be CHRONOLOGICAL, not days[-1]: an out-of-order backfill (e.g. append recent
        # days then backfill older ones) leaves attrs['days'] in APPEND order. ISO date strings sort
        # chronologically, so max(days) is the true latest. day_index keeps the PHYSICAL mapping — the
        # stored arrays are NOT reordered.
        latest_day = max(days) if days else None
        return _Meta(g, lon, lat, days, {d: i for i, d in enumerate(days)}, vars_, var_valid, arr, latest_day)

    def refresh(self):
        """P4-S3: re-read cube metadata so new delta days become visible WITHOUT a process restart. The
        zarr I/O (reopen + attr read) happens OUTSIDE the lock; only the immutable-snapshot SWAP is under
        the lock. Read-only. In-flight reads keep their captured snapshot; the next read sees the new days
        (eventual consistency)."""
        new = self._build_meta()               # I/O outside the lock
        with self._lock:
            self._meta = new                   # atomic snapshot swap
            self._last_refresh = time.monotonic()

    def maybe_refresh(self, ttl_seconds: float) -> bool:
        """Refresh at most once per `ttl_seconds` (cheap monotonic guard). Returns True if it refreshed."""
        if ttl_seconds and (time.monotonic() - self._last_refresh) >= ttl_seconds:
            self.refresh()
            return True
        return False

    # ---- accessors: each reads the CURRENT snapshot atomically (single-field reads) ----
    @property
    def days(self) -> List[str]:
        return self._meta.days

    @property
    def vars(self) -> List[str]:
        return self._meta.vars

    @property
    def var_valid(self) -> Dict[str, list]:
        return self._meta.var_valid

    @property
    def _day_index(self) -> Dict[str, int]:
        return self._meta.day_index

    @property
    def _lon(self):
        return self._meta.lon

    @property
    def _lat(self):
        return self._meta.lat

    @property
    def latest(self) -> Optional[str]:
        return self._meta.latest_day        # chronological max(days), not days[-1] (see _build_meta)

    @property
    def day_count(self) -> int:
        return len(self._meta.days)

    def covers_days(self, days: Sequence[str]) -> bool:
        """True iff every given day is present in the cube (one consistent snapshot)."""
        di = self._meta.day_index
        return all(d in di for d in days)

    def _array(self, var: str):
        return self._meta.arr.get(var)         # immutable snapshot -> lock-free

    @staticmethod
    def _nearest(m: _Meta, lon, lat):
        lo = _clamp(float(lon), float(m.lon.min()), float(m.lon.max()))
        la = _clamp(float(lat), float(m.lat.min()), float(m.lat.max()))
        jj = _nearest_idx(lo, m.lon); ii = _nearest_idx(la, m.lat)
        return ii, jj, float(m.lon[jj]), float(m.lat[ii])

    def nearest_indices(self, lon, lat):
        return self._nearest(self._meta, lon, lat)

    @staticmethod
    def _check_fields(fields):
        bad = [f for f in fields if f not in ALLOWED_FIELDS]
        if bad:
            raise ValueError(f"Unsupported field(s): {','.join(bad)}")
        return list(fields)

    def point_series(self, lon, lat, days: Sequence[str], fields: Sequence[str]) -> List[dict]:
        return self.point_series_from(self._meta, lon, lat, days, fields)

    def point_series_from(self, m: _Meta, lon, lat, days: Sequence[str],
                          fields: Sequence[str]) -> List[dict]:
        """Read against a CALLER-SUPPLIED snapshot.

        P5-S2 needs this: `TieredSnapshot` captures base and delta metadata together and must
        then read from exactly those, never re-consulting `self._meta`. Re-reading per tier is
        what let a refresh landing mid-request mix generations across tiers (R1)."""
        fields = self._check_fields(fields)
        ii, jj, glon, glat = self._nearest(m, lon, lat)
        # requested days that exist in the cube, in requested order
        idx = [(d, m.day_index[d]) for d in days if d in m.day_index]
        if not idx:
            return []
        t_indices = [t for _, t in idx]
        # read each field's time-column for this (ii,jj) in ONE indexed read (contiguous range -> a
        # slice; else fancy index). Zarr reads happen OUTSIDE any lock via the captured snapshot handles.
        t0, t1 = min(t_indices), max(t_indices)
        contiguous = (t1 - t0 + 1) == len(t_indices)
        colvals: Dict[str, np.ndarray] = {}
        for f in fields:
            arr = m.arr.get(f)
            if arr is None:
                continue                      # absent field -> omit (P1 parity)
            if contiguous:
                colvals[f] = np.asarray(arr[t0:t1 + 1, ii, jj])
            else:
                colvals[f] = np.asarray(arr[t_indices, ii, jj])
        rows = []
        for k, (day, t) in enumerate(idx):
            row = {"lon": glon, "lat": glat, "date": day}
            pos = (t - t0) if contiguous else k
            for f in fields:
                if f not in colvals:
                    continue                       # field never in cube -> omit
                valid = m.var_valid.get(f)
                if valid is not None and not valid[t]:
                    continue                       # var ABSENT on this day -> omit (P1 parity)
                v = float(colvals[f][pos])
                row[f] = None if np.isnan(v) else v   # present-but-NaN land -> null
            rows.append(row)
        return rows
