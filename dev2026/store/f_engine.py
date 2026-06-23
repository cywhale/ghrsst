"""dev2026 — P2-S2: Candidate F — smarter read engine over the daily-group store.

F (Tier-1 no-rewrite, spec §4): read a point's per-day chunks in PARALLEL instead of
P1's sequential loop. It does NOT remove the O(days) fan-out (chunk_count / decompressed
bytes are unchanged vs P1/E1) — it only overlaps the per-day 4 MB decompress across cores.

HARD resource constraints (review-driven; the real risk is server-side, not correctness):
  * ONE GLOBAL bounded pool — never a per-request scheduler/pool, never unbounded Dask.
  * Max concurrent chunk reads across the WHOLE process = `parallelism` (the pool size),
    regardless of how many requests are in flight. So RSS/threads do NOT grow with request
    concurrency — concurrent requests SHARE the one pool (and therefore degrade, which is
    the point: F is expected to help C=1 but not LR concurrency).
  * In production this pool IS the shared capacity budget (coordinate with the API
    BoundedExecutor); `gunicorn_workers × parallelism ≤ cores`.

We use a directly-bounded ThreadPoolExecutor (decompress releases the GIL, so threads give
real parallelism — proven in P1 §3.2) rather than Dask, because Dask's default schedulers
are hard to bound and tend to oversubscribe — the exact failure mode to avoid.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Sequence

import numpy as np
import zarr

from .store_access import StoreAccess
from .zarr_paths import group_path


class FReadEngine:
    def __init__(self, zarr_path: Optional[str] = None, parallelism: int = 4, lru_max: int = 512):
        self.sa = StoreAccess(zarr_path)
        self.root = self.sa.root
        self.parallelism = max(1, int(parallelism))
        # ONE global pool, shared across all requests/threads for this engine instance
        self._pool = ThreadPoolExecutor(max_workers=self.parallelism, thread_name_prefix="f-read")
        # thread-safe direct-array handle cache (like E1, but reads run in parallel)
        self._arr_lru: "OrderedDict[tuple, zarr.Array]" = OrderedDict()
        self._lru_lock = threading.Lock()
        self.lru_max = int(lru_max)
        # observability for the benchmark hard gates
        self._inflight = 0
        self._inflight_peak = 0
        self._inflight_lock = threading.Lock()

    @property
    def in_flight_peak(self) -> int:
        return self._inflight_peak

    def reset_inflight_peak(self):
        with self._inflight_lock:
            self._inflight_peak = 0

    def _open_array(self, day: str, var: str) -> Optional[zarr.Array]:
        import os
        path = group_path(self.root, day)
        ap = os.path.join(path, var)
        if not os.path.isdir(ap):
            return None
        key = (day, var)
        with self._lru_lock:
            a = self._arr_lru.get(key)
            if a is not None:
                self._arr_lru.move_to_end(key)
                return a
        a = zarr.open_array(ap, mode="r")
        with self._lru_lock:
            if key in self._arr_lru:
                self._arr_lru.move_to_end(key)
                return self._arr_lru[key]
            self._arr_lru[key] = a
            self._arr_lru.move_to_end(key)
            while len(self._arr_lru) > self.lru_max:
                self._arr_lru.popitem(last=False)
            return a

    def _read_day(self, day: str, ii: int, jj: int, fields: Sequence[str],
                  glon: float, glat: float) -> dict:
        with self._inflight_lock:
            self._inflight += 1
            self._inflight_peak = max(self._inflight_peak, self._inflight)
        try:
            row = {"lon": glon, "lat": glat, "date": day}
            for f in fields:
                arr = self._open_array(day, f)
                if arr is None:
                    continue                       # absent field omitted (P1 parity)
                v = float(arr[0, ii, jj]) if arr.ndim == 3 else float(arr[ii, jj])
                row[f] = None if np.isnan(v) else v
            return row
        finally:
            with self._inflight_lock:
                self._inflight -= 1

    def point_series(self, lon: float, lat: float, days: Sequence[str],
                     fields: Sequence[str]) -> List[dict]:
        """Same contract as P1 point_series; per-day reads run on the shared bounded pool."""
        fields = self.sa._check_fields(fields)
        ii, jj, glon, glat = self.sa.nearest_indices(lon, lat)
        existing = [d for d in days if self.sa.day_present(d)]
        if not existing:
            return []
        # submit all per-day reads; the GLOBAL pool runs <= parallelism at once (bounded)
        futs = {d: self._pool.submit(self._read_day, d, ii, jj, fields, glon, glat)
                for d in existing}
        return [futs[d].result() for d in existing]    # preserve day order

    def shutdown(self):
        self._pool.shutdown(wait=False)
