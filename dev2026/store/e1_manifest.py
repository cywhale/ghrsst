"""dev2026 — P2-S1: Candidate E1 — manifest/index sidecar over the daily-group store.

E1 is a Tier-1 NO-REWRITE candidate (spec phase2_timecube_design.md §4): precompute a
manifest mapping each day → per-variable ARRAY path (+ chunk/shape), so a point time-series
opens each day's ARRAY directly (cached) and skips the per-request filesystem rescan,
day_present checks, and zarr GROUP open that P1 pays.

What E1 CANNOT do (stated up front, H4): it still reads 1 chunk/day/field, so
`chunk_count` stays **O(days)** and `decompressed_bytes` is unchanged vs the P1 daily
baseline. Its only possible win is per-day open/overhead. The micro-cost benchmark will
show whether that is enough (expected: NO — decompress dominates, per VM24/S0 finding 2).

Reuses StoreAccess for the immutable coords / nearest-index / day discovery so E1 differs
from P1 ONLY in the per-day read path (clean apples-to-apples).
"""
from __future__ import annotations

import os
import threading
from collections import OrderedDict
from typing import Dict, List, Optional, Sequence

import numpy as np
import zarr

from .store_access import ALLOWED_FIELDS, StoreAccess
from .zarr_paths import group_path


class E1ManifestStore:
    def __init__(self, zarr_path: Optional[str] = None, lru_max: int = 256):
        self.sa = StoreAccess(zarr_path)          # coords / nearest / day discovery (P1)
        self.root = self.sa.root
        self.lru_max = int(lru_max)
        self._manifest: Dict[str, Dict[str, str]] = {}   # day -> {var -> array path}
        self._arr_lru: "OrderedDict[tuple, zarr.Array]" = OrderedDict()
        self._lock = threading.Lock()

    # ---- manifest: day -> {var -> array path} (built by one scan) --------
    def build_manifest(self, days: Optional[Sequence[str]] = None) -> dict:
        days = list(days) if days is not None else self.sa.existing_days()
        man: Dict[str, Dict[str, str]] = {}
        for day in days:
            dpath = group_path(self.root, day)
            if not os.path.isdir(dpath):
                continue
            vmap = {}
            for v in ALLOWED_FIELDS:
                ap = os.path.join(dpath, v)
                if os.path.isdir(ap):
                    vmap[v] = ap
            man[day] = vmap
        self._manifest = man
        return man

    def manifest_stats(self) -> dict:
        return {"days": len(self._manifest),
                "var_coverage": {v: sum(v in m for m in self._manifest.values())
                                 for v in ("sst", "sst_anomaly", "sea_ice")}}

    # ---- direct ARRAY open (skip group open) with thread-safe LRU --------
    def _open_array(self, day: str, var: str) -> Optional[zarr.Array]:
        path = self._manifest.get(day, {}).get(var)
        if path is None:
            return None
        key = (day, var)
        with self._lock:
            a = self._arr_lru.get(key)
            if a is not None:
                self._arr_lru.move_to_end(key)
                return a
        a = zarr.open_array(path, mode="r")       # array-level open; no group metadata
        with self._lock:
            if key in self._arr_lru:
                self._arr_lru.move_to_end(key)
                return self._arr_lru[key]
            self._arr_lru[key] = a
            self._arr_lru.move_to_end(key)
            while len(self._arr_lru) > self.lru_max:
                self._arr_lru.popitem(last=False)
            return a

    # ---- point time-series (E1 path) -------------------------------------
    def point_series(self, lon: float, lat: float, days: Sequence[str],
                     fields: Sequence[str]) -> List[dict]:
        """Same contract/semantics as StoreAccess.point_series (absent field OMITTED),
        but per-day reads go through the manifest's direct array handles."""
        fields = self.sa._check_fields(fields)
        if not self._manifest:
            self.build_manifest()
        ii, jj, glon, glat = self.sa.nearest_indices(lon, lat)
        rows: List[dict] = []
        for day in days:
            if day not in self._manifest:
                if not self.sa.day_present(day):
                    continue
                self.build_manifest()             # newly appeared day -> rescan once
                if day not in self._manifest:
                    continue
            row = {"lon": glon, "lat": glat, "date": day}
            for f in fields:
                arr = self._open_array(day, f)
                if arr is None:                   # absent field -> omit (old/P1 parity)
                    continue
                v = float(arr[0, ii, jj]) if arr.ndim == 3 else float(arr[ii, jj])
                row[f] = None if np.isnan(v) else v
            rows.append(row)
        return rows
