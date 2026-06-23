"""dev2026 — P2-S5: HybridRouter — route each query to the best store.

Routing (spec phase2_timecube_design.md §2, evidence: P2-S2/S3):
  * multi-day point/range time-series -> TimeCubeStore (O(days/time_chunk))
  * single-day point, bbox, POST /points (single-day batch) -> daily P1 StoreAccess
    (the daily store is fine for these; not the bottleneck)

Routing is a PERFORMANCE choice only — every route must return identical values (parity is
tested). For safety, a multi-day series routes to the cube ONLY if the cube covers every
existing requested day (dual-write invariant); otherwise it falls back to the daily store.
If no cube is configured, everything uses the daily store (graceful: behaves exactly like P1).
"""
from __future__ import annotations

import threading
from typing import Iterator, List, Optional, Sequence

from .store_access import StoreAccess
from .time_cube import TimeCubeStore


class HybridRouter:
    def __init__(self, daily: StoreAccess, cube: Optional[TimeCubeStore] = None):
        self.daily = daily
        self.cube = cube
        self.route_counts = {"cube": 0, "daily": 0}   # observability (healthz)
        self._rc_lock = threading.Lock()

    # ---- routing decision: PURE (no side effects; safe to call for the
    #      X-Store-Route header AND inside point_series without double-counting) ----
    def route_point(self, days: Sequence[str]) -> str:
        """'cube' for a multi-day series fully covered by the cube; else 'daily'."""
        if self.cube is not None:
            existing = [d for d in days if self.daily.day_present(d)]
            if len(existing) > 1 and self.cube.covers_days(existing):
                return "cube"
        return "daily"

    # ---- point/range time-series (routed; counts ONCE per actual query) --
    def point_series(self, lon: float, lat: float, days: Sequence[str],
                     fields: Sequence[str]) -> List[dict]:
        route = self.route_point(days)
        with self._rc_lock:
            self.route_counts[route] += 1
        if route == "cube":
            return self.cube.point_series(lon, lat, days, fields)
        return self.daily.point_series(lon, lat, days, fields)

    # ---- single-day batch + bbox -> always daily P1 ----------------------
    def points_batch(self, points, day: str, fields: Sequence[str]) -> List[dict]:
        return self.daily.points_batch(points, day, fields)        # route: daily

    def bbox_arrays(self, day, lon0, lat0, lon1, lat1, fields, stride: int = 1):
        return self.daily.bbox_arrays(day, lon0, lat0, lon1, lat1, fields, stride)  # route: daily

    def bbox_batches(self, day, lon0, lat0, lon1, lat1, fields, stride: int = 1,
                     batch_rows: int = 50_000) -> Iterator[List[dict]]:
        return self.daily.bbox_batches(day, lon0, lat0, lon1, lat1, fields, stride, batch_rows)
