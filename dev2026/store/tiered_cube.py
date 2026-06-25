"""dev2026 — TieredCube: base (time_chunk=90, fast reads) + delta (time_chunk=1, cheap appends).

Production append design (review): writing a day into the time_chunk=90 BASE cube read-modify-writes
the whole 90-step shard (slow). Instead keep a small **delta** cube with time_chunk=1 — each daily
append is its own fresh shard (NO RMW = cheap, O(one day)). Reads come from BOTH: delta for its
(recent) days, base for the rest. Periodic **compaction** (ingest/dual_write.compact) folds the delta
into the base via the fast bulk builder, then resets the delta.

TieredCube exposes the same surface the HybridRouter uses (point_series / covers_days / latest /
day_count), so it is a drop-in for a single TimeCubeStore. Delta takes precedence on overlap (during
the brief compaction window). Each underlying cube keeps its own per-(day,var) validity, so old/P1
omit-vs-null semantics are preserved.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from .time_cube import TimeCubeStore


class TieredCube:
    def __init__(self, base: TimeCubeStore, delta: Optional[TimeCubeStore] = None):
        self.base = base
        self.delta = delta

    @property
    def latest(self) -> Optional[str]:
        cands = [c.latest for c in (self.base, self.delta) if c is not None and c.latest]
        return max(cands) if cands else None

    @property
    def day_count(self) -> int:
        days = set(self.base.days)
        if self.delta:
            days |= set(self.delta.days)
        return len(days)

    def _has(self, store, day) -> bool:
        return store is not None and day in store._day_index

    def covers_days(self, days: Sequence[str]) -> bool:
        return all(self._has(self.delta, d) or self._has(self.base, d) for d in days)

    def point_series(self, lon: float, lat: float, days: Sequence[str],
                     fields: Sequence[str]) -> List[dict]:
        # delta takes precedence; each cube reads its own subset (keeps its validity semantics)
        delta_days = [d for d in days if self._has(self.delta, d)]
        delta_set = set(delta_days)
        base_days = [d for d in days if d not in delta_set and self._has(self.base, d)]
        by_day = {}
        if base_days:
            for r in self.base.point_series(lon, lat, base_days, fields):
                by_day[r["date"]] = r
        if delta_days:
            for r in self.delta.point_series(lon, lat, delta_days, fields):
                by_day[r["date"]] = r
        return [by_day[d] for d in days if d in by_day]
