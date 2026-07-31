"""dev2026 — P5-S1: SegmentedCubeStore — a manifest-driven, BASE-ONLY segmented cube.

Drop-in for the **base** `TimeCubeStore` inside `TieredCube` (design spec §6.1). It exposes
the same surface `TieredCube` consumes (`days`, `day_count`, `latest`, `covers_days`,
`point_series`, `_day_index`) and nothing else changes above it.

Two properties this class exists to guarantee:

* **It never sees delta.** §4.0 keeps delta with `TieredCube`, so there is exactly one
  authority per store. This class does not accept, read or resolve a delta path — a manifest
  that even mentions one is rejected by `block_manifest`.
* **Reads are grouped by segment, never by day.** §6.2: resolve every requested day through
  the snapshot's precomputed `day -> (segment, local index)` map, group by segment, then
  issue ONE `point_series` per segment. P5-S0 measured that opening stores per request costs
  ~36 % on its own; a per-day open would be far worse.

Snapshot discipline mirrors `TimeCubeStore._Meta`: an immutable snapshot is built entirely
off-lock and swapped atomically, so a reader that captures it once sees a consistent view for
its whole call. A snapshot that cannot be fully validated is **never installed** — the
previous one is retained and `SnapshotError` is raised (fail-closed, §5.1).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

from . import block_manifest as bm
from .time_cube import TimeCubeStore


class SnapshotError(Exception):
    """A snapshot could not be built. The store keeps serving its previous snapshot."""


class _Meta(NamedTuple):
    """Immutable snapshot. `refresh()` builds a NEW one and swaps it atomically, so a reader
    that captures `m = self._meta` once never sees a torn mix of generations."""
    generation: int
    generation_id: str
    checksum: str
    segment_ids: List[str]
    stores: List[TimeCubeStore]
    day_map: Dict[str, Tuple[int, int]]     # day -> (segment index, local time index)
    days: List[str]                         # chronological; AVAILABILITY view only
    latest: Optional[str]
    vars: List[str]


class SegmentedCubeStore:
    def __init__(self, manifest_dir: str):
        self.path = manifest_dir
        self._lock = threading.Lock()
        self._meta = self._build_meta()          # first build: no previous snapshot to keep
        self._last_refresh = time.monotonic()

    # ---- snapshot construction (fail-closed) ---------------------------------
    def _build_meta(self) -> _Meta:
        try:
            manifest = bm.load_live(self.path)
            bm.validate_manifest(manifest)
        except bm.ManifestError as exc:
            raise SnapshotError(f"manifest invalid: {exc}") from exc

        segment_ids: List[str] = []
        stores: List[TimeCubeStore] = []
        # day -> (precedence, segment index, local index); precedence resolves overlap
        chosen: Dict[str, Tuple[int, int, int]] = {}
        vars_: List[str] = []

        for idx, seg in enumerate(manifest["segments"]):
            spath = os.path.normpath(os.path.join(self.path, seg["path"]))
            if not os.path.isdir(spath):
                raise SnapshotError(
                    f"segment {seg['segment_id']!r} missing at {spath}; refusing to build a "
                    f"partial snapshot")
            try:
                store = TimeCubeStore(spath)
            except Exception as exc:                       # noqa: BLE001 - surface as snapshot failure
                raise SnapshotError(
                    f"segment {seg['segment_id']!r} unreadable at {spath}: {exc}") from exc

            actual = list(store.days)
            declared = bm.declared_present_days(seg)
            if sorted(actual) != sorted(declared):
                raise SnapshotError(
                    f"STALE MANIFEST: segment {seg['segment_id']!r} holds "
                    f"{len(actual)} day(s) but the manifest declares {len(declared)}; "
                    f"day sets differ. Keeping the previous snapshot.")
            if len(actual) != int(seg["day_count"]):
                raise SnapshotError(
                    f"STALE MANIFEST: segment {seg['segment_id']!r} day_count "
                    f"{seg['day_count']} != len(attrs['days'])={len(actual)}")
            declared_digest = (seg.get("fingerprint") or {}).get("day_digest")
            if declared_digest and declared_digest != bm.day_digest(actual):
                raise SnapshotError(
                    f"STALE MANIFEST: segment {seg['segment_id']!r} day_digest mismatch")

            prec = int(seg["precedence"])
            for day in actual:
                local = store._day_index[day]          # per-tier index; append-order safe
                prior = chosen.get(day)
                if prior is None:
                    chosen[day] = (prec, idx, local)
                elif prior[0] == prec:
                    raise SnapshotError(
                        f"duplicate day {day} claimed at equal precedence {prec} by "
                        f"{segment_ids[prior[1]]!r} and {seg['segment_id']!r}; there is no "
                        f"'pick one' rule (§5.1)")
                elif prec > prior[0]:
                    chosen[day] = (prec, idx, local)

            segment_ids.append(seg["segment_id"])
            stores.append(store)
            for v in store.vars:
                if v not in vars_:
                    vars_.append(v)

        day_map = {d: (i, l) for d, (_, i, l) in chosen.items()}
        days = sorted(day_map)
        return _Meta(generation=int(manifest["generation"]),
                     generation_id=manifest["generation_id"],
                     checksum=manifest["manifest_checksum"],
                     segment_ids=segment_ids, stores=stores, day_map=day_map,
                     days=days, latest=(max(days) if days else None), vars=vars_)

    def refresh(self):
        """Rebuild off-lock, then swap atomically. On failure the previous snapshot stays
        installed and the error propagates — a stale-but-consistent view beats a partial one."""
        new = self._build_meta()
        with self._lock:
            self._meta = new
            self._last_refresh = time.monotonic()

    def refresh_if_changed(self) -> bool:
        """Cheap generation check: read the small manifest and rebuild ONLY if the generation
        or checksum moved. Reopening every segment on each TTL tick would be pure waste."""
        try:
            live = bm.load_live(self.path)
        except bm.ManifestError as exc:
            raise SnapshotError(str(exc)) from exc
        m = self._meta
        if (int(live.get("generation", -1)) == m.generation
                and live.get("manifest_checksum") == m.checksum):
            return False
        self.refresh()
        return True

    def maybe_refresh(self, ttl_seconds: float) -> bool:
        if ttl_seconds and (time.monotonic() - self._last_refresh) >= ttl_seconds:
            self._last_refresh = time.monotonic()
            return self.refresh_if_changed()
        return False

    # ---- TieredCube-compatible surface ---------------------------------------
    @property
    def generation(self) -> int:
        return self._meta.generation

    @property
    def days(self) -> List[str]:
        return self._meta.days

    @property
    def day_count(self) -> int:
        return len(self._meta.days)

    @property
    def latest(self) -> Optional[str]:
        return self._meta.latest

    @property
    def vars(self) -> List[str]:
        return self._meta.vars

    @property
    def _day_index(self) -> Dict[str, Tuple[int, int]]:
        """Membership view for `TieredCube._has`. NOT an array index — each tier keeps its
        own `day_index` (the append-order invariant, P4-S4 §2)."""
        return self._meta.day_map

    def covers_days(self, days: Sequence[str]) -> bool:
        dm = self._meta.day_map
        return all(d in dm for d in days)

    def nearest_indices(self, lon, lat):
        m = self._meta
        if not m.stores:
            raise SnapshotError("no segments")
        return m.stores[0].nearest_indices(lon, lat)

    # ---- introspection (tests / observability) -------------------------------
    def resolve(self, day: str) -> Tuple[int, int]:
        return self._meta.day_map[day]

    def segment_id(self, idx: int) -> str:
        return self._meta.segment_ids[idx]

    def segment_store(self, segment_id: str) -> TimeCubeStore:
        m = self._meta
        return m.stores[m.segment_ids.index(segment_id)]

    # ---- the read path (§6.2: grouped by segment, never by day) --------------
    def point_series(self, lon: float, lat: float, days: Sequence[str],
                     fields: Sequence[str]) -> List[dict]:
        m = self._meta                              # ONE snapshot for the whole call
        by_segment: Dict[int, List[str]] = {}
        for d in days:
            hit = m.day_map.get(d)
            if hit is None:
                continue                            # absent day -> omitted, never fabricated
            by_segment.setdefault(hit[0], []).append(d)
        if not by_segment:
            return []

        rows: Dict[str, dict] = {}
        for seg_idx, seg_days in by_segment.items():
            # ONE call per segment, not per day
            for row in m.stores[seg_idx].point_series(lon, lat, seg_days, fields):
                rows[row["date"]] = row
        return [rows[d] for d in days if d in rows]   # requested order preserved
