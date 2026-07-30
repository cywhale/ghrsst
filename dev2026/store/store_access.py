"""dev2026 — P1-S1 StoreAccess: the cached, thread-safe Zarr read layer.

Design (spec dev2026/specs/01_refactor_spec.md §4, doc 00 §3.1.1):
  * NO long-lived root handle and NO reliance on consolidated metadata for day
    discovery (avoids the 3 MB root re-parse AND the new-day staleness class of bug).
  * Coordinates (lon/lat) cached once — immutable across the whole dataset.
  * Day discovery via filesystem rescan with a short TTL (always sees newly copied
    days; never caches a "missing -> present" negative).
  * Day groups opened directly by path into a thread-safe bounded LRU.
  * point_series / points_batch read one chunk's worth at a time and release it
    (peak memory ≈ one chunk × fields, not the whole series / fan-out).
  * bbox is exposed as a batch generator so the HTTP layer (P1-S2) can stream a
    JSON array without materialising all rows.

This module is SYNCHRONOUS and thread-safe; the async API layer (P1-S2) wraps calls
in a bounded executor. It deliberately does not import fastapi/asyncio.
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from datetime import date, timedelta
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np
import zarr

from .zarr_paths import (
    group_path,
    group_exists,
    list_existing_days,
    resolve_zarr_path,
)

ALLOWED_FIELDS = ("sst", "sst_anomaly", "sea_ice")


def _nearest_idx(val: float, arr: np.ndarray) -> int:
    """Nearest-neighbour index. Mirrors ghrsst_app._idx_from_coord exactly."""
    j = int(np.searchsorted(arr, val))
    if j <= 0:
        return 0
    if j >= arr.size:
        return arr.size - 1
    return j if abs(arr[j] - val) < abs(arr[j - 1] - val) else j - 1


def primary_contiguous_bounds(days: Sequence[str]) -> Tuple[Optional[str], Optional[str]]:
    """The main contiguous run within `days` (which must be sorted ascending).

    Some stores contain isolated test days before the real production run (for example VM24 has a
    single 2023-03-06 day). User-facing "available range" messages should not advertise such isolated
    days as the start of the usable range. Pick the longest contiguous run; if tied, pick the latest.

    Module-level so the DAILY store (`StoreAccess.primary_bounds`) and the tier-agnostic POINT
    availability (`HybridRouter.point_primary_bounds`, P4 post-prune fix) share ONE implementation.
    """
    days = list(days)
    if not days:
        return (None, None)
    best_start = best_end = cur_start = cur_end = date.fromisoformat(days[0])
    best_len = 1
    for s in days[1:]:
        d = date.fromisoformat(s)
        if d == cur_end + timedelta(days=1):
            cur_end = d
        else:
            cur_len = (cur_end - cur_start).days + 1
            if cur_len > best_len or (cur_len == best_len and cur_end > best_end):
                best_start, best_end, best_len = cur_start, cur_end, cur_len
            cur_start = cur_end = d
    cur_len = (cur_end - cur_start).days + 1
    if cur_len > best_len or (cur_len == best_len and cur_end > best_end):
        best_start, best_end = cur_start, cur_end
    return (best_start.isoformat(), best_end.isoformat())


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class StoreAccess:
    def __init__(
        self,
        zarr_path: Optional[str] = None,
        lru_max: int = 64,
        day_ttl_seconds: float = 10.0,
        batch_chunk_fanout_max: int = 64,
        points_batch_max: int = 1000,
        bbox_point_limit: int = 1_000_000,
    ):
        self.root = zarr_path or resolve_zarr_path()
        self.lru_max = int(lru_max)
        self.day_ttl_seconds = float(day_ttl_seconds)
        self.batch_chunk_fanout_max = int(batch_chunk_fanout_max)
        self.points_batch_max = int(points_batch_max)
        self.bbox_point_limit = int(bbox_point_limit)  # defense-in-depth; P1-S2 also guards

        # thread-safe bounded LRU of day -> zarr group (handles used READ-ONLY)
        self._lru: "OrderedDict[str, zarr.Group]" = OrderedDict()
        self._lru_lock = threading.Lock()

        # immutable coords, cached once under a one-shot lock
        self._coords_lock = threading.Lock()
        self._lon: Optional[np.ndarray] = None
        self._lat: Optional[np.ndarray] = None

        # day-list cache with TTL (filesystem rescan)
        self._days_lock = threading.Lock()
        self._days: List[str] = []
        self._days_at: float = -1.0

    # ---- day discovery (TTL filesystem rescan; sees new days) -------------
    def existing_days(self, force: bool = False) -> List[str]:
        now = time.monotonic()
        with self._days_lock:
            if force or not self._days or (now - self._days_at) > self.day_ttl_seconds:
                self._days = list_existing_days(self.root)
                self._days_at = now
            return list(self._days)

    def bounds(self) -> Tuple[Optional[str], Optional[str]]:
        days = self.existing_days()
        return (days[0], days[-1]) if days else (None, None)

    def primary_bounds(self) -> Tuple[Optional[str], Optional[str]]:
        """The daily store's main contiguous range (see `primary_contiguous_bounds`)."""
        return primary_contiguous_bounds(self.existing_days())

    def day_present(self, day: str) -> bool:
        # authoritative filesystem check (don't trust a stale day-list for a miss)
        if group_exists(self.root, day):
            return True
        # maybe it appeared since last scan
        return day in self.existing_days(force=True)

    # ---- day group handle (thread-safe bounded LRU) ----------------------
    def _open_day(self, day: str) -> zarr.Group:
        with self._lru_lock:
            g = self._lru.get(day)
            if g is not None:
                self._lru.move_to_end(day)
                return g
        # open OUTSIDE the lock (open is ~4 ms; don't serialise all opens)
        g = zarr.open_group(group_path(self.root, day), mode="r")
        with self._lru_lock:
            existing = self._lru.get(day)
            if existing is not None:          # another thread won the race
                self._lru.move_to_end(day)
                return existing
            self._lru[day] = g
            self._lru.move_to_end(day)
            while len(self._lru) > self.lru_max:
                self._lru.popitem(last=False)
            return g

    # ---- coords (cached once; immutable) ---------------------------------
    def _coords(self) -> Tuple[np.ndarray, np.ndarray]:
        if self._lon is not None and self._lat is not None:
            return self._lon, self._lat
        with self._coords_lock:
            if self._lon is None or self._lat is None:
                days = self.existing_days()
                if not days:
                    raise ValueError(f"No /YYYY/MM/DD groups under {self.root}")
                g = self._open_day(days[0])
                self._lon = np.asarray(g["lon"][:])
                self._lat = np.asarray(g["lat"][:])
            return self._lon, self._lat

    def nearest_indices(self, lon: float, lat: float) -> Tuple[int, int, float, float]:
        lon_arr, lat_arr = self._coords()
        lo = _clamp(float(lon), float(lon_arr.min()), float(lon_arr.max()))
        la = _clamp(float(lat), float(lat_arr.min()), float(lat_arr.max()))
        jj = _nearest_idx(lo, lon_arr)
        ii = _nearest_idx(la, lat_arr)
        return ii, jj, float(lon_arr[jj]), float(lat_arr[ii])

    @staticmethod
    def _check_fields(fields: Sequence[str]) -> List[str]:
        bad = [f for f in fields if f not in ALLOWED_FIELDS]
        if bad:
            raise ValueError(f"Unsupported field(s): {','.join(bad)}")
        return list(fields)

    @staticmethod
    def _read_point(g: zarr.Group, field: str, ii: int, jj: int) -> Optional[float]:
        arr = g[field]
        v = float(arr[0, ii, jj]) if arr.ndim == 3 else float(arr[ii, jj])
        return None if np.isnan(v) else v

    # ---- point time-series (per-day open + read + release) ---------------
    def point_series(
        self, lon: float, lat: float, days: Sequence[str], fields: Sequence[str]
    ) -> List[dict]:
        """Field-absence semantics MATCH the old GET point path: a requested field
        that is ABSENT from a day group is OMITTED from that row (key not present);
        a field present but NaN (land) is `null`. (bbox/points_batch null absent
        fields instead — see those methods.)"""
        fields = self._check_fields(fields)
        ii, jj, glon, glat = self.nearest_indices(lon, lat)
        rows: List[dict] = []
        for day in days:
            if not self.day_present(day):
                continue
            g = self._open_day(day)              # cheap; handle reused read-only
            row = {"lon": glon, "lat": glat, "date": day}
            for f in fields:
                if f in g:                       # absent field -> omit key (old-parity)
                    row[f] = self._read_point(g, f, ii, jj)
            rows.append(row)
            # nothing else to release: we read scalars, not whole chunks held
        return rows

    # ---- batch points, single day (chunk-grouped; read->extract->release) -
    def points_batch(
        self, points: Sequence[Sequence[float]], day: str, fields: Sequence[str]
    ) -> List[dict]:
        """Field-absence semantics (NEW endpoint; pinned explicitly): a requested
        field absent from the day group is `null` (key present), matching the old
        bbox path — NOT omitted like point_series. Land/NaN is also `null`."""
        fields = self._check_fields(fields)
        if len(points) > self.points_batch_max:
            raise ValueError(
                f"too many points ({len(points)} > {self.points_batch_max})"
            )
        if not self.day_present(day):
            raise ValueError(f"day not available: {day}")

        lon_arr, lat_arr = self._coords()
        # resolve indices once
        idx: List[Tuple[int, int, float, float]] = []
        for lon, lat in points:
            lo = _clamp(float(lon), float(lon_arr.min()), float(lon_arr.max()))
            la = _clamp(float(lat), float(lat_arr.min()), float(lat_arr.max()))
            jj = _nearest_idx(lo, lon_arr)
            ii = _nearest_idx(la, lat_arr)
            idx.append((ii, jj, float(lon_arr[jj]), float(lat_arr[ii])))

        g = self._open_day(day)
        # derive the spatial chunk shape from the array (don't hardcode 1024)
        present = [f for f in fields if f in g]
        if present:
            cshape = g[present[0]].chunks
            ci, cj = int(cshape[-2]), int(cshape[-1])
        else:
            ci = cj = 1
        # group points by containing chunk so each chunk is decompressed once
        groups: "OrderedDict[Tuple[int,int], List[int]]" = OrderedDict()
        for k, (ii, jj, _, _) in enumerate(idx):
            key = (ii // ci, jj // cj)
            groups.setdefault(key, []).append(k)
        if len(groups) > self.batch_chunk_fanout_max:
            raise ValueError(
                f"batch spans {len(groups)} chunks > fan-out limit "
                f"{self.batch_chunk_fanout_max}; shrink/split the request"
            )

        rows: List[Optional[dict]] = [None] * len(points)
        for _key, members in groups.items():
            iis = [idx[k][0] for k in members]
            jjs = [idx[k][1] for k in members]
            i0, i1 = min(iis), max(iis)
            j0, j1 = min(jjs), max(jjs)
            # read the minimal block covering this chunk's points, per field, then release
            blocks: Dict[str, np.ndarray] = {}
            for f in fields:
                if f in g:
                    arr = g[f]
                    sub = arr[0, i0 : i1 + 1, j0 : j1 + 1] if arr.ndim == 3 else arr[i0 : i1 + 1, j0 : j1 + 1]
                    blocks[f] = np.asarray(sub)
            for k in members:
                ii, jj, glon, glat = idx[k]
                row = {"index": k, "lon": glon, "lat": glat, "date": day}
                for f in fields:
                    if f in blocks:
                        v = float(blocks[f][ii - i0, jj - j0])
                        row[f] = None if np.isnan(v) else v
                    else:
                        row[f] = None
                rows[k] = row
            del blocks  # release this chunk's data before the next group
        return [r for r in rows if r is not None]

    # ---- bbox as batched row generator (HTTP layer streams it) -----------
    def bbox_arrays(
        self,
        day: str,
        lon0: float,
        lat0: float,
        lon1: float,
        lat1: float,
        fields: Sequence[str],
        stride: int = 1,
    ) -> Tuple[np.ndarray, np.ndarray, Dict[str, np.ndarray]]:
        """Read the bbox slice (the blocking I/O part; offload this). Returns
        (lons, lats, cols) where cols[field] is a 2-D (nlat,nlon) float32 array.

        NOTE (P1-S2/P1-S4 follow-up): the working set is the WHOLE bbox slice (not
        one batch). Far below the old row-dict explosion (doc 00 §4.8) and fine within
        POINT_LIMIT, but P1-S4 scenario-BBOX worker RSS is the authoritative gate; if
        it fails, switch to row/chunk-window streaming reads here."""
        fields = self._check_fields(fields)
        if not self.day_present(day):
            raise ValueError(f"day not available: {day}")
        if stride < 1:
            raise ValueError("stride must be >= 1")
        lon_arr, lat_arr = self._coords()
        lo0 = _clamp(min(lon0, lon1), float(lon_arr.min()), float(lon_arr.max()))
        lo1 = _clamp(max(lon0, lon1), float(lon_arr.min()), float(lon_arr.max()))
        la0 = _clamp(min(lat0, lat1), float(lat_arr.min()), float(lat_arr.max()))
        la1 = _clamp(max(lat0, lat1), float(lat_arr.min()), float(lat_arr.max()))
        j0, j1 = sorted((_nearest_idx(lo0, lon_arr), _nearest_idx(lo1, lon_arr)))
        i0, i1 = sorted((_nearest_idx(la0, lat_arr), _nearest_idx(la1, lat_arr)))

        nj = (j1 - j0) // stride + 1
        ni = (i1 - i0) // stride + 1
        if ni * nj > self.bbox_point_limit:
            raise ValueError(
                f"bbox too large ({ni * nj} > {self.bbox_point_limit}); "
                f"increase stride or shrink bbox"
            )

        g = self._open_day(day)
        cols: Dict[str, np.ndarray] = {}
        for f in fields:
            if f in g:
                arr = g[f]
                sub = (
                    arr[0, i0 : i1 + 1 : stride, j0 : j1 + 1 : stride]
                    if arr.ndim == 3
                    else arr[i0 : i1 + 1 : stride, j0 : j1 + 1 : stride]
                )
                cols[f] = np.asarray(sub, dtype=np.float32)
        lats = lat_arr[i0 : i1 + 1 : stride].astype(float)
        lons = lon_arr[j0 : j1 + 1 : stride].astype(float)
        return lons, lats, cols

    @staticmethod
    def bbox_rows_window(
        lons: np.ndarray,
        lats: np.ndarray,
        cols: Dict[str, np.ndarray],
        fields: Sequence[str],
        day: str,
        k0: int,
        k1: int,
    ) -> List[dict]:
        """Build row dicts for flat row indices [k0, k1) over the (nlat*nlon) grid.
        Pure CPU; no I/O. Absent field -> null (matches old bbox path)."""
        nlon = lons.size
        rows: List[dict] = []
        for k in range(k0, k1):
            ri, cj = divmod(k, nlon)
            row = {"lon": float(lons[cj]), "lat": float(lats[ri]), "date": day}
            for f in fields:
                if f in cols:
                    v = cols[f][ri, cj]
                    row[f] = None if np.isnan(v) else float(v)
                else:
                    row[f] = None
            rows.append(row)
        return rows

    def bbox_batches(
        self,
        day: str,
        lon0: float,
        lat0: float,
        lon1: float,
        lat1: float,
        fields: Sequence[str],
        stride: int = 1,
        batch_rows: int = 50_000,
    ) -> Iterator[List[dict]]:
        """Convenience generator (tests / non-streaming callers). The HTTP layer
        uses bbox_arrays + bbox_rows_window directly so it can offload per batch."""
        fields = self._check_fields(fields)
        lons, lats, cols = self.bbox_arrays(day, lon0, lat0, lon1, lat1, fields, stride)
        total = lons.size * lats.size
        for k0 in range(0, total, batch_rows):
            yield self.bbox_rows_window(lons, lats, cols, fields, day,
                                        k0, min(k0 + batch_rows, total))
