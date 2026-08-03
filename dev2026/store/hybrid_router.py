"""dev2026 — P2-S5: HybridRouter — route each query to the best store.

Routing (spec phase2_timecube_design.md §2, evidence: P2-S2/S3):
  * point/range time-series (single-day INCLUDED) -> the cube when it covers every requested day
  * bbox, POST /points (single-day batch)         -> daily P1 StoreAccess

Routing is a PERFORMANCE choice only — every route must return identical values (parity is
tested). A series routes to the cube only if the cube covers every requested day; if the daily
store covers them all instead it routes there; if NEITHER covers the whole request the days are
served per-tier and merged ('mixed') rather than truncated. If no cube is configured, everything
uses the daily store (graceful: behaves exactly like P1).

This module also owns POINT AVAILABILITY (`point_days` / `point_bounds` / `point_primary_bounds` /
`point_day_present`) = cube ∪ daily. That abstraction exists because the P4 daily-staging prune
demoted the daily store to a recent-only window: treating `StoreAccess.bounds()/day_present()` as
the availability authority made the whole pre-prune history look "not available" (production 400s,
2026-07-28). Spatial availability stays a SEPARATE, narrower rule (delta membership) enforced in
the API layer.
"""
from __future__ import annotations

import threading
from typing import Iterator, List, Optional, Sequence

from .store_access import StoreAccess, primary_contiguous_bounds
from .time_cube import TimeCubeStore


class QuerySnapshot:
    """ONE consistent view for ONE request (P5-S2, risk R1).

    A request used to re-derive its view three times: availability, then routing, then the
    read. A refresh landing between them could answer "available", route on a second view and
    read from a third. This captures the cube's composite snapshot and the daily day-set
    **once**, and every decision in the request is answered from it."""

    __slots__ = ("_cube_snap", "_daily", "_daily_days", "_dayset")

    def __init__(self, daily: StoreAccess, cube_snapshot):
        self._cube_snap = cube_snapshot
        self._daily = daily
        self._daily_days = frozenset(daily.existing_days())
        self._dayset = set(self._daily_days)
        if cube_snapshot is not None:
            self._dayset |= set(cube_snapshot.days)

    # ---- availability (point scope = cube ∪ daily) ----------------------
    def point_days(self) -> List[str]:
        return sorted(self._dayset)

    def point_bounds(self) -> tuple:
        return (min(self._dayset), max(self._dayset)) if self._dayset else (None, None)

    def point_primary_bounds(self) -> tuple:
        return primary_contiguous_bounds(self.point_days())

    def point_day_present(self, day: str) -> bool:
        return day in self._dayset

    @property
    def cube_days(self) -> frozenset:
        return frozenset(self._cube_snap.days) if self._cube_snap is not None else frozenset()

    @property
    def daily_days(self) -> frozenset:
        return self._daily_days

    # ---- routing + read, both answered from THIS view -------------------
    def route(self, days: Sequence[str]) -> str:
        if not days:
            return "daily"
        if self._cube_snap is not None and self._cube_snap.covers_days(days):
            return "cube"
        if all(d in self._daily_days for d in days):
            return "daily"
        return "mixed"

    def point_series(self, lon: float, lat: float, days: Sequence[str],
                     fields: Sequence[str]) -> List[dict]:
        route = self.route(days)
        if route == "cube":
            return self._cube_snap.point_series(lon, lat, days, fields)
        if route == "daily":
            return self._daily.point_series(lon, lat, days, fields)
        cube_set = self.cube_days
        cube_days = [d for d in days if d in cube_set]
        rest = [d for d in days if d not in cube_set]
        by_day = {}
        if cube_days:
            for r in self._cube_snap.point_series(lon, lat, cube_days, fields):
                by_day[r["date"]] = r
        if rest:
            for r in self._daily.point_series(lon, lat, rest, fields):
                by_day[r["date"]] = r
        return [by_day[d] for d in days if d in by_day]


class _WholeCube:
    """Adapter for a cube with no `snapshot()` (a bare `TimeCubeStore` in transition configs).

    It captures that store's own immutable `_Meta` once, so the request still gets a single
    view rather than repeated live reads."""

    __slots__ = ("_store", "_meta", "days")

    def __init__(self, store):
        self._store = store
        self._meta = store._meta
        self.days = tuple(self._meta.days)

    def covers_days(self, days):
        di = self._meta.day_index
        return all(d in di for d in days)

    def point_series(self, lon, lat, days, fields):
        return self._store.point_series_from(self._meta, lon, lat, days, fields)


class HybridRouter:
    def __init__(self, daily: StoreAccess, cube: Optional[TimeCubeStore] = None):
        self.daily = daily
        self.cube = cube
        self.route_counts = {"cube": 0, "daily": 0, "mixed": 0}   # observability (healthz)
        self._rc_lock = threading.Lock()

    def query_snapshot(self) -> QuerySnapshot:
        """Take ONE view for a whole request. Callers that answer availability, routing and
        the read separately must use this rather than calling the router three times."""
        if self.cube is None:                    # no cube configured: pure P1 behaviour
            cube_snap = None
        elif hasattr(self.cube, "snapshot"):     # TieredCube -> composite cross-tier capture
            cube_snap = self.cube.snapshot()
        else:                                    # bare TimeCubeStore (transition configs)
            cube_snap = _WholeCube(self.cube)
        return QuerySnapshot(self.daily, cube_snap)

    # ---- POINT AVAILABILITY (P4 post-prune fix) --------------------------
    # After the P4 daily-staging prune the daily store holds only a recent window, so it is NO LONGER
    # the availability authority: point/range availability is the base+delta cube UNION the daily
    # store. (Spatial availability is a separate, narrower rule — delta membership — enforced in the
    # API's bbox / POST /points paths, NOT here.)
    def _point_dayset(self) -> set:
        days = set(self.daily.existing_days())
        if self.cube is not None:
            days |= set(self.cube.days)
        return days

    def point_days(self) -> List[str]:
        """Every day a point/range query can be served for, chronological."""
        return sorted(self._point_dayset())

    def point_bounds(self) -> tuple:
        """(earliest, latest) of the full point availability; (None, None) when empty."""
        days = self._point_dayset()
        return (min(days), max(days)) if days else (None, None)

    def point_primary_bounds(self) -> tuple:
        """Main contiguous run of the point availability (for user-facing range messages)."""
        return primary_contiguous_bounds(self.point_days())

    def point_day_present(self, day: str) -> bool:
        """Cube first (O(1) index lookup, covers history); else the daily store's authoritative
        filesystem check (which also picks up a day ingested since the last scan)."""
        if self.cube is not None and self.cube.covers_days([day]):
            return True
        return self.daily.day_present(day)

    # ---- routing decision: PURE (no side effects; safe to call for the
    #      X-Store-Route header AND inside point_series without double-counting) ----
    def route_point(self, days: Sequence[str]) -> str:
        """'cube' when the cube covers EVERY requested day (single-day included), 'daily' when the
        daily store does, else 'mixed'.

        NO daily pre-filter: filtering the requested days by `daily.day_present` first (the pre-P4
        behaviour) silently dropped every historical day once daily became a 38-day staging window,
        which is what produced the post-prune 400s. Single-day is routed to the cube too — the old
        `len(existing) > 1` guard kept historical single-day points on a store that no longer has them
        (cube single-day point reads are also *faster*: P4-S0b VM24 4.5 ms vs daily 14.7 ms)."""
        if not days:
            return "daily"
        if self.cube is not None and self.cube.covers_days(days):
            return "cube"
        daily_days = set(self.daily.existing_days())          # TTL-cached; avoids one stat per day
        if all(d in daily_days for d in days):
            return "daily"
        return "mixed"

    def count_route(self, route: str) -> None:
        """Record a route decision made by a `QuerySnapshot` (observability only)."""
        with self._rc_lock:
            self.route_counts[route] = self.route_counts.get(route, 0) + 1

    # ---- point/range time-series (routed; counts ONCE per actual query) --
    def point_series(self, lon: float, lat: float, days: Sequence[str],
                     fields: Sequence[str]) -> List[dict]:
        route = self.route_point(days)
        with self._rc_lock:
            self.route_counts[route] = self.route_counts.get(route, 0) + 1
        if route == "cube":
            return self.cube.point_series(lon, lat, days, fields)
        if route == "daily":
            return self.daily.point_series(lon, lat, days, fields)
        # MIXED: neither tier alone covers the request (e.g. a range spanning history plus a brand-new
        # day already in daily but not yet appended to delta). Serve each day from the tier that has
        # it — routing to one store would silently TRUNCATE the series.
        cube_set = set(self.cube.days) if self.cube is not None else set()
        cube_days = [d for d in days if d in cube_set]
        rest = [d for d in days if d not in cube_set]
        by_day = {}
        if cube_days:
            for r in self.cube.point_series(lon, lat, cube_days, fields):
                by_day[r["date"]] = r
        if rest:
            for r in self.daily.point_series(lon, lat, rest, fields):
                by_day[r["date"]] = r
        return [by_day[d] for d in days if d in by_day]        # requested order preserved

    # ---- single-day batch + bbox -> always daily P1 ----------------------
    def points_batch(self, points, day: str, fields: Sequence[str]) -> List[dict]:
        return self.daily.points_batch(points, day, fields)        # route: daily

    def bbox_arrays(self, day, lon0, lat0, lon1, lat1, fields, stride: int = 1):
        return self.daily.bbox_arrays(day, lon0, lat0, lon1, lat1, fields, stride)  # route: daily

    def bbox_batches(self, day, lon0, lat0, lon1, lat1, fields, stride: int = 1,
                     batch_rows: int = 50_000) -> Iterator[List[dict]]:
        return self.daily.bbox_batches(day, lon0, lat0, lon1, lat1, fields, stride, batch_rows)
