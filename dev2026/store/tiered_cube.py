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

## One snapshot per tier, per request (P5-S2, risk R1) — and what that does NOT promise

Each tier is individually immutable, but consulting them at different moments is not. The
earlier implementation asked delta for membership, then read base, then read delta, so a
refresh landing *between* those steps changed the answer mid-read. `snapshot()` captures each
tier's metadata **once** and every read goes through `point_series_from` against exactly those,
so a request never re-reads a tier.

**What is guaranteed:** one stable snapshot per tier for the whole request; each half is
internally consistent; no live re-read.

**What is NOT guaranteed — stated plainly:** the two halves are not proven contemporaneous.
Identity double-reading shows each tier was stable across the capture; it cannot show the pair
belongs to the same instant, and capture ORDER cannot fix that either, because a tier's
freshness depends on when *its own* `refresh()` last ran, not on when this code reads the
attribute. So `snapshot()` may legitimately return an older base with a newer delta.

**Why that is safe here.** An older base with a newer delta is the *normal* steady state: the
delta cron appends days many times between compactions. The single pair that would lose data is
a **pre-publish base with a post-prune delta**, where folded days sit in neither tier. That
combination cannot reach a reader: the design spec §7.5 orders **publish → verify → prune**, and
the delta prune retargets the delta path, which runs under **request quiescence** (pm2 stop →
swap → start), so no reader is alive across it. The guarantee comes from the compaction protocol
and the quiescence posture, not from this capture — and this docstring exists so that is not
mistaken for something the snapshot mechanism provides on its own. Whether a shared
generation/epoch fence should replace that reliance is **risk R1, adjudicated at S5/G10**.

## Base/delta disjointness is enforced here (risk R1a)

`SegmentedCubeStore` cannot check it: by §4.0 it never sees the delta. So the composing layer
asserts it at construction and fails closed. It used to be an optional helper an operator had
to remember — an invariant that depends on memory is not an invariant.
"""
from __future__ import annotations

import os
import threading
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from .segmented_cube import SnapshotError
from .time_cube import TimeCubeStore


class TierCompositionError(SnapshotError):
    """Base and delta are not composable, or a consistent cross-tier view is unobtainable.

    A subclass of `SnapshotError` because it means the same thing to a caller: no consistent
    view could be established, so nothing is served."""


def _store_paths(store) -> List[str]:
    """Every on-disk store a tier resolves to, as realpaths.

    A `SegmentedCubeStore` is many stores; a `TimeCubeStore` is one. Both must be comparable,
    because the disjointness check has to work for the base shape actually deployed today
    (`TimeCubeStore`) and not only for the one that happens to expose a helper."""
    if hasattr(store, "segment_paths"):
        return [os.path.realpath(p) for p in store.segment_paths]
    return [os.path.realpath(store.path)]


class TieredSnapshot(NamedTuple):
    """An immutable, cross-tier view captured once and read from thereafter.

    `base_meta` / `delta_meta` are the tiers' own immutable snapshots. Reads use them
    explicitly, so nothing here re-consults a live store mid-request. The pair is not proven
    contemporaneous — see the module docstring for exactly what that does and does not
    promise."""
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
    #: how many times `snapshot()` retries before refusing to return a cross-tier view
    SNAPSHOT_ATTEMPTS = 8

    def __init__(self, base, delta: Optional[TimeCubeStore] = None):
        self.base = base
        self.delta = delta
        self._lock = threading.Lock()
        if delta is not None:
            self._assert_disjoint(base, delta)

    @staticmethod
    def _assert_disjoint(base, delta):
        """R1a: enforced HERE, for every base shape.

        The previous version only checked when the base exposed `assert_disjoint_from`, which
        a `SegmentedCubeStore` does and a `TimeCubeStore` does not — so it did nothing for the
        assembly actually deployed on VM24 today. Comparison is by realpath, so a symlinked
        delta is caught too."""
        delta_paths = set(_store_paths(delta))
        clash = sorted(set(_store_paths(base)) & delta_paths)
        if clash:
            raise TierCompositionError(
                f"base and delta must be disjoint stores (§4.0): {clash[0]} is both. A base "
                f"segment that IS the delta would serve delta bytes as immutable base.")
        if hasattr(base, "assert_disjoint_from"):     # belt and braces, richer message
            base.assert_disjoint_from(delta.path)

    # ---- the composite snapshot (R1) -------------------------------------
    def snapshot(self) -> TieredSnapshot:
        """Capture BOTH tiers' metadata as ONE consistent pair.

        The composite lock serializes refreshes made through this object; the double read
        (take both, take both again, accept only when neither moved) additionally ensures each
        captured meta was **stable across the capture** even when a tier is refreshed directly
        by the TTL loop or the delta cron. If metadata never settles, **refuse** rather than
        hand back something churning underneath the caller.

        This does NOT make the two halves contemporaneous — see the module docstring. An older
        base with a newer delta is accepted, is normal, and is safe under §7.5's
        publish-before-prune ordering plus the delta prune's request quiescence.
        """
        with self._lock:
            for _ in range(self.SNAPSHOT_ATTEMPTS):
                b1 = self.base._meta
                d1 = self.delta._meta if self.delta is not None else None
                b2 = self.base._meta
                d2 = self.delta._meta if self.delta is not None else None
                if b1 is b2 and d1 is d2:
                    return self._compose(b1, d1)
        raise TierCompositionError(
            f"could not capture stable per-tier metadata in {self.SNAPSHOT_ATTEMPTS} attempts; "
            f"refusing to hand back a view that is churning underneath the caller")

    def _compose(self, base_meta, delta_meta) -> TieredSnapshot:
        base_days = frozenset(base_meta.days)
        delta_days = frozenset(delta_meta.days) if delta_meta is not None else frozenset()
        union = sorted(base_days | delta_days)
        return TieredSnapshot(
            base=self.base, base_meta=base_meta, delta=self.delta, delta_meta=delta_meta,
            base_days=base_days, delta_days=delta_days,
            days=tuple(union), latest=(max(union) if union else None))

    # ---- refresh ---------------------------------------------------------
    def refresh(self):
        """P4-S3: refresh base + delta metadata (new delta days visible without a restart).

        Held under the composite lock so a `snapshot()` cannot observe the tiers half-updated
        when the refresh goes through this object."""
        with self._lock:
            self.base.refresh()
            if self.delta is not None:
                self.delta.refresh()

    def maybe_refresh(self, ttl_seconds: float) -> bool:
        with self._lock:
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
