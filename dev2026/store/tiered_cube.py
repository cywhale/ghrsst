"""dev2026 — TieredCube: base (read-optimized) + delta (append-optimized), composed.

Production append design (review): writing a day into the time_chunk=90 BASE cube
read-modify-writes the whole 90-step shard (slow). Instead keep a small **delta** cube with
time_chunk=1 — each daily append is its own fresh shard (NO RMW = cheap, O(one day)). Reads
come from BOTH: delta for its (recent) days, base for the rest. Periodic **compaction** folds
the delta into the base, then resets the delta.

The **base may be a `TimeCubeStore` (a single monolith) or a `SegmentedCubeStore`** (P5's
immutable versioned blocks + manifest). Nothing above this class changes: `HybridRouter` sees
one cube either way, and delta precedence is applied here, in code — the manifest never
describes the delta (design spec §4.0).

## One composite snapshot per request (P5-S2, risk R1)

Each tier is individually immutable, but consulting them at different moments is not. The
earlier implementation asked delta for membership, then read base, then read delta — so a base
manifest refresh or a delta refresh landing *between* those steps produced a result mixing
generations across tiers. `snapshot()` now captures **both tiers' metadata together, once**,
and every read goes through `point_series_from` against exactly those. A request therefore sees
a **complete-old or complete-new** view, never a mix.

## Base/delta disjointness is enforced here (risk R1a)

`SegmentedCubeStore` cannot check it: by §4.0 it never sees the delta. So the composing layer
asserts it at construction and fails closed. It used to be an optional helper an operator had
to remember — an invariant that depends on memory is not an invariant.
"""
from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from .time_cube import TimeCubeStore


class TieredSnapshot(NamedTuple):
    """An immutable, cross-tier view captured once and read from thereafter.

    `base_meta` / `delta_meta` are the tiers' own immutable snapshots, taken together. Reads
    use them explicitly, so nothing here re-consults a live store mid-request."""
    base: object
    base_meta: object
    delta: Optional[object]
    delta_meta: Optional[object]
    base_days: frozenset
    delta_days: frozenset
    days: Tuple[str, ...]                 # chronological union; AVAILABILITY view only
    latest: Optional[str]

    # ---- membership ------------------------------------------------------
    def has(self, day: str) -> bool:
        return day in self.delta_days or day in self.base_days

    def covers_days(self, days: Sequence[str]) -> bool:
        return all(self.has(d) for d in days)

    def day_source(self, day: str) -> Optional[str]:
        """'delta' | 'base' | None — the tier that answers for this day, delta first."""
        if day in self.delta_days:
            return "delta"
        if day in self.base_days:
            return "base"
        return None

    # ---- the read --------------------------------------------------------
    def point_series(self, lon: float, lat: float, days: Sequence[str],
                     fields: Sequence[str]) -> List[dict]:
        """Delta takes precedence; each tier reads its own subset from the CAPTURED metadata,
        and rows are returned in the requested order. Days nobody has are omitted — never
        fabricated, and never silently truncating the rest."""
        delta_days = [d for d in days if d in self.delta_days]
        delta_set = set(delta_days)
        base_days = [d for d in days if d not in delta_set and d in self.base_days]

        by_day: Dict[str, dict] = {}
        if base_days:
            for r in self.base.point_series_from(self.base_meta, lon, lat, base_days, fields):
                by_day[r["date"]] = r
        if delta_days:
            for r in self.delta.point_series_from(self.delta_meta, lon, lat,
                                                  delta_days, fields):
                by_day[r["date"]] = r
        return [by_day[d] for d in days if d in by_day]


class TieredCube:
    def __init__(self, base, delta: Optional[TimeCubeStore] = None):
        self.base = base
        self.delta = delta
        # R1a: the assembly enforces disjointness -- not an operator remembering to call it.
        if delta is not None and hasattr(base, "assert_disjoint_from"):
            base.assert_disjoint_from(delta.path)

    # ---- the composite snapshot (R1) -------------------------------------
    def snapshot(self) -> TieredSnapshot:
        """Capture BOTH tiers' metadata together. Callers that need request-level atomicity
        should take one of these and use it for every decision and read in that request."""
        bm_ = self.base._meta
        dm_ = self.delta._meta if self.delta is not None else None
        base_days = frozenset(bm_.days)
        delta_days = frozenset(dm_.days) if dm_ is not None else frozenset()
        union = sorted(base_days | delta_days)
        return TieredSnapshot(
            base=self.base, base_meta=bm_, delta=self.delta, delta_meta=dm_,
            base_days=base_days, delta_days=delta_days,
            days=tuple(union), latest=(max(union) if union else None))

    # ---- refresh ---------------------------------------------------------
    def refresh(self):
        """P4-S3: refresh base + delta metadata (new delta days visible without a restart)."""
        self.base.refresh()
        if self.delta is not None:
            self.delta.refresh()

    def maybe_refresh(self, ttl_seconds: float) -> bool:
        refreshed = self.base.maybe_refresh(ttl_seconds)
        if self.delta is not None:
            refreshed = self.delta.maybe_refresh(ttl_seconds) or refreshed
        return refreshed

    # ---- surface consumed by HybridRouter (unchanged) --------------------
    @property
    def latest(self) -> Optional[str]:
        return self.snapshot().latest

    @property
    def days(self) -> List[str]:
        """CHRONOLOGICAL union of base+delta days (deduped).

        Each tier stores its own days in PHYSICAL/append order; this union is the
        tier-agnostic *availability* view (P4 point-availability fix), so it is sorted. Do NOT
        use it to index a tier's arrays — index through that tier's own `day_index`
        (append-order invariant, P4-S4 §2)."""
        return list(self.snapshot().days)

    @property
    def day_count(self) -> int:
        return len(self.snapshot().days)

    def covers_days(self, days: Sequence[str]) -> bool:
        return self.snapshot().covers_days(days)

    def point_series(self, lon: float, lat: float, days: Sequence[str],
                     fields: Sequence[str]) -> List[dict]:
        return self.snapshot().point_series(lon, lat, days, fields)
