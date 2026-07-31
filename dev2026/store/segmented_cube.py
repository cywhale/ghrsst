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


def _within(child: str, parent: str) -> bool:
    """True iff `child` resolves inside `parent`, after following symlinks."""
    child, parent = os.path.realpath(child), os.path.realpath(parent)
    return child == parent or child.startswith(parent + os.sep)


class SegmentedCubeStore:
    def __init__(self, manifest_dir: str, allowed_legacy_paths: Optional[Sequence[str]] = None):
        """`allowed_legacy_paths` is the deployment's explicit list of legacy monoliths.

        A `legacy_base` segment legitimately lives OUTSIDE the block root (production's is
        `../mur_timecube_s8_t90_sh128.zarr`), so `..` cannot simply be banned. Instead the
        escape must be named by the deployment: any legacy path not on this list is refused.
        `block` segments are always confined to the block root."""
        self.path = manifest_dir
        self.allowed_legacy_paths = [os.path.realpath(p) for p in (allowed_legacy_paths or [])]
        self._lock = threading.Lock()
        self._meta = self._build_meta()          # first build: no previous snapshot to keep
        self._last_refresh = time.monotonic()

    # ---- path authority (review finding #2) ----------------------------------
    def _resolve_segment_path(self, seg: dict) -> str:
        raw = seg["path"]
        if os.path.isabs(raw):
            raise SnapshotError(
                f"segment {seg['segment_id']!r}: absolute path {raw!r} is not allowed; "
                f"segment paths are relative to the manifest")
        resolved = os.path.realpath(os.path.join(self.path, raw))
        if seg["kind"] == "block":
            if not _within(resolved, self.path):
                raise SnapshotError(
                    f"segment {seg['segment_id']!r}: block path resolves OUTSIDE the block "
                    f"root ({resolved} not within {os.path.realpath(self.path)}). A block "
                    f"may not point at the delta, at another store, or through a symlink.")
        elif seg["kind"] == "legacy_base":
            if not (_within(resolved, self.path) or resolved in self.allowed_legacy_paths):
                raise SnapshotError(
                    f"segment {seg['segment_id']!r}: legacy_base path resolves OUTSIDE the "
                    f"block root ({resolved}) and is not in the deployment's "
                    f"allowed_legacy_paths. A legacy monolith outside the root must be named "
                    f"explicitly by configuration, never accepted from the manifest alone.")
        else:                                    # unreachable: schema restricts `kind`
            raise SnapshotError(f"segment {seg['segment_id']!r}: unsupported kind")
        return resolved

    def assert_disjoint_from(self, other_path: str) -> None:
        """Assert no base segment resolves to `other_path` (used for the delta at composition
        time). A base segment that IS the delta would serve delta bytes as immutable base."""
        target = os.path.realpath(other_path)
        for sid, store in zip(self._meta.segment_ids, self._meta.stores):
            if os.path.realpath(store.path) == target:
                raise SnapshotError(
                    f"base segment {sid!r} resolves to {target}, which is also the delta "
                    f"path; base and delta must be disjoint stores (§4.0)")

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

        grid = manifest["grid"]
        axes_ref = None

        for idx, seg in enumerate(manifest["segments"]):
            spath = self._resolve_segment_path(seg)
            if not os.path.isdir(spath):
                raise SnapshotError(
                    f"segment {seg['segment_id']!r} missing at {spath}; refusing to build a "
                    f"partial snapshot")
            try:
                store = TimeCubeStore(spath)
            except Exception as exc:                       # noqa: BLE001 - surface as snapshot failure
                raise SnapshotError(
                    f"segment {seg['segment_id']!r} unreadable at {spath}: {exc}") from exc

            # ---- structural binding (review finding #3): the manifest must describe the
            # store it names. A day set alone cannot detect a block built on another grid,
            # which would map the same lon/lat to different physical cells -- silent wrong
            # data, the failure class this whole phase exists to avoid.
            axes = bm.store_axes(spath)
            if (axes["ny"], axes["nx"]) != (int(grid["ny"]), int(grid["nx"])):
                raise SnapshotError(
                    f"segment {seg['segment_id']!r} grid mismatch: store is "
                    f"{axes['ny']}x{axes['nx']}, manifest declares "
                    f"{grid['ny']}x{grid['nx']}")
            if grid.get("region") and axes["region"] and \
                    [int(x) for x in grid["region"]] != axes["region"]:
                raise SnapshotError(
                    f"segment {seg['segment_id']!r} region mismatch: store {axes['region']} "
                    f"vs manifest {grid['region']}")
            if axes_ref is None:
                axes_ref = axes
            elif (axes["lon_digest"], axes["lat_digest"]) != \
                    (axes_ref["lon_digest"], axes_ref["lat_digest"]):
                raise SnapshotError(
                    f"segment {seg['segment_id']!r} has different lon/lat axes from the "
                    f"first segment; all segments must share identical axes or the same "
                    f"lon/lat would resolve to different cells per block")
            actual_layout = bm.segment_layout(spath)
            if seg["layout"] != actual_layout:
                raise SnapshotError(
                    f"segment {seg['segment_id']!r} layout mismatch: store {actual_layout} "
                    f"vs manifest {seg['layout']}")
            fp = bm.metadata_fingerprint(spath)
            if seg["fingerprint"]["metadata"] != fp:
                raise SnapshotError(
                    f"segment {seg['segment_id']!r} metadata fingerprint mismatch: "
                    f"store {fp[:12]}… vs manifest "
                    f"{str(seg['fingerprint']['metadata'])[:12]}…")

            actual = list(store.days)
            for v, flags in store.var_valid.items():
                if len(flags) != len(actual):
                    raise SnapshotError(
                        f"segment {seg['segment_id']!r} var_valid[{v}] has {len(flags)} "
                        f"flag(s) for {len(actual)} day(s)")
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
