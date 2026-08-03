"""dev2026 — P5-S2: grouped multi-segment read path + composite snapshot (tests FIRST).

Gates (design spec §14 P5-S2): **G1** semantic equality with the P1 oracle on every fixture,
**G2** day integrity, **G3** point p95 within +25 % of the S0 baseline, **G12** route
contract, plus **G6/G16 groundwork** (no per-request store opens) and the two risks S2 owns:

  **R1**  ONE composite snapshot per request — a base manifest refresh or a delta refresh
          landing mid-request must not mix generations across tiers.
  **R1a** base/delta disjointness enforced BY THE ASSEMBLY, not by an operator remembering
          to call a helper.

Fixtures exercised here: F4 append-order delta, F5 overlap precedence, F6 gaps, F7 absent
vars across a segment boundary, F8 present NaNs, F9 cube+daily newest day (`mixed`),
F18 two-week outage then bulk backfill.

Local/synthetic only: no VM24 path, no `GHRSST_*` store, nothing outside a temp dir.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

import numpy as np

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import p5_fixtures as fx  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store.segmented_cube import SegmentedCubeStore, SnapshotError  # noqa: E402
from store.store_access import StoreAccess  # noqa: E402
from store.tiered_cube import TieredCube  # noqa: E402
from store.time_cube import TimeCubeStore  # noqa: E402

ANCHOR = "2026-06-27"
LON, LAT = 105.0, 5.0
FIELDS = ("sst", "sst_anomaly", "sea_ice")


def _seg(seg_id, path, start, end, day_count, *, precedence, store_path,
         gaps=(), kind="block", boundary_kind="calendar", vars_=fx.VARS):
    return {
        "segment_id": seg_id, "kind": kind, "path": path, "immutable": True,
        "boundary_kind": boundary_kind, "start_day": start, "end_day": end,
        "materialized_through": end, "day_count": day_count,
        "gaps": list(gaps), "unknown": [], "day_list": None,
        "layout": bm.segment_layout(store_path), "variables": sorted(vars_),
        "fingerprint": {"algo": "sha256",
                        "metadata": bm.metadata_fingerprint(store_path),
                        "day_digest": bm.day_digest(fx.read_days(store_path))},
        "precedence": precedence, "sealed": True, "supersedes": None,
    }


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.root = fx.make_root(self.tmp)

    def _manifest(self, segments, generation=1):
        m = {
            "format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION,
            "generation": generation,
            "generation_id": f"gen{generation:06d}-20270101T000000Z",
            "created_utc": "2027-01-01T00:00:00Z", "created_by": "p5s2-tests",
            "predecessor_generation": generation - 1 if generation > 1 else None,
            "predecessor_manifest": (f"manifest.gen{generation - 1:06d}.json"
                                     if generation > 1 else None),
            "grid": {"ny": 32, "nx": 32, "region": [0, 32, 0, 32]},
            "variables": sorted({v for s in segments for v in s["variables"]}),
            "block_grid": {"anchor_day": ANCHOR, "block_days": 90},
            "segments": list(segments), "superseded": [], "manifest_checksum": "",
        }
        m["manifest_checksum"] = bm.compute_checksum(m)
        return m

    def _two_blocks(self, **kw):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        s1, e1 = bm.block_bounds(ANCHOR, 90, 1)
        d0, d1 = fx.calendar_span(s0, e0), fx.calendar_span(s1, e1)
        p0 = fx.build_block(os.path.join(self.root, "b0"), d0, seed=1, **kw)
        p1 = fx.build_block(os.path.join(self.root, "b1"), d1, seed=90)
        segs = [_seg("b0", "b0", s0, e0, len(d0), precedence=1, store_path=p0),
                _seg("b1", "b1", s1, e1, len(d1), precedence=2, store_path=p1)]
        bm.publish(self.root, self._manifest(segs))
        return SegmentedCubeStore(self.root), d0 + d1

    def _delta(self, days, name="delta.zarr"):
        p = os.path.join(self.tmp, name)
        fx.build_delta(p, days)
        return TimeCubeStore(p), p


# ============================================================ R1a: assembly enforces disjointness
class TestAssemblyEnforcesDisjointness(_Base):
    def test_tieredcube_refuses_a_base_segment_that_is_the_delta(self):
        """R1a: the helper existed but relied on someone remembering to call it. An invariant
        that depends on memory is not an invariant."""
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        days = fx.calendar_span(s0, e0)
        shared = os.path.join(self.root, "b0")
        fx.build_block(shared, days)
        bm.publish(self.root, self._manifest(
            [_seg("b0", "b0", s0, e0, len(days), precedence=1, store_path=shared)]))
        base = SegmentedCubeStore(self.root)
        with self.assertRaises(SnapshotError) as cm:
            TieredCube(base, TimeCubeStore(shared))          # delta IS a base segment
        self.assertIn("disjoint", str(cm.exception).lower())

    def test_distinct_paths_compose_normally(self):
        base, _ = self._two_blocks()
        delta, _ = self._delta(fx.days_from("2026-12-24", 31))
        TieredCube(base, delta)                               # no raise


# ============================================================ R1: one composite snapshot
class TestCompositeSnapshot(_Base):
    def setUp(self):
        super().setUp()
        self.base, self.base_days = self._two_blocks()
        self.delta_days = self.base_days[-5:] + fx.days_from("2026-12-24", 26)
        self.delta, self.delta_path = self._delta(self.delta_days)
        self.cube = TieredCube(self.base, self.delta)

    def test_snapshot_is_captured_once_and_is_immutable(self):
        snap = self.cube.snapshot()
        self.assertIs(snap.base_meta, self.base._meta)
        self.assertIs(snap.delta_meta, self.delta._meta)
        with self.assertRaises(AttributeError):
            snap.days = ()                                    # NamedTuple: immutable

    def test_delta_refresh_midflight_cannot_change_a_captured_snapshot(self):
        """R1: the read must be complete-old or complete-new, never a mix."""
        snap = self.cube.snapshot()
        before = snap.point_series(LON, LAT, self.base_days[-3:], ["sst"])

        fx.append_delta_day(self.delta_path, "2027-01-19")
        self.delta.refresh()

        after_same_snapshot = snap.point_series(LON, LAT, self.base_days[-3:], ["sst"])
        self.assertEqual(before, after_same_snapshot,
                         "a captured snapshot must not see a later delta refresh")
        self.assertNotIn("2027-01-19", snap.days)
        self.assertIn("2027-01-19", self.cube.snapshot().days)   # a NEW snapshot does

    def test_base_generation_change_midflight_cannot_change_a_captured_snapshot(self):
        snap = self.cube.snapshot()
        n_before = len(snap.days)

        s2, e2 = bm.block_bounds(ANCHOR, 90, 2)
        d2 = fx.calendar_span(s2, e2)
        p2 = fx.build_block(os.path.join(self.root, "b2"), d2, seed=300)
        live = bm.load_live(self.root)
        segs = list(live["segments"]) + [
            _seg("b2", "b2", s2, e2, len(d2), precedence=3, store_path=p2)]
        bm.publish(self.root, self._manifest(segs, generation=2))
        self.base.refresh()

        self.assertEqual(len(snap.days), n_before, "captured snapshot must not grow")
        self.assertGreater(len(self.cube.snapshot().days), n_before)

    def test_reads_use_the_snapshot_not_the_live_store(self):
        """The decisive check: a snapshot read must never re-consult `store._meta`."""
        snap = self.cube.snapshot()
        sentinel = object()
        real_base_meta, real_delta_meta = self.base._meta, self.delta._meta
        self.base._meta = sentinel
        self.delta._meta = sentinel
        try:
            rows = snap.point_series(LON, LAT, self.base_days[:4], ["sst"])
            self.assertEqual(len(rows), 4)
        finally:
            self.base._meta = real_base_meta
            self.delta._meta = real_delta_meta


# ============================================================ adversarial: atomic capture
class TestSnapshotCaptureIsAtomic(_Base):
    """R1 round 2: capturing `base._meta` and then `delta._meta` is two reads. A refresh
    landing BETWEEN them yields base-old + delta-new — a mixed snapshot the earlier tests
    could not see, because they refreshed before or after a capture, never during one."""

    def setUp(self):
        super().setUp()
        self.base, self.base_days = self._two_blocks()
        self.delta, self.delta_path = self._delta(fx.days_from("2026-12-24", 10))

    def _interleaving_base(self, on_access):
        base = self.base

        class Interleaving:
            """Delegates to the real base but runs a callback whenever `_meta` is read."""
            def __init__(self):
                self._n = 0

            def __getattr__(self, name):
                return getattr(base, name)

            @property
            def _meta(self):
                self._n += 1
                on_access(self._n)
                return base._meta

        return Interleaving()

    def test_a_mixed_generation_pair_IS_accepted_this_is_the_contract(self):
        """The limit of what identity double-reading can promise — pinned deliberately.

        `b1 is b2 and d1 is d2` proves each tier was STABLE across the capture. It does not
        prove the pair is contemporaneous: a delta refresh completing before `d1` is read
        yields old-base + new-delta, both stable, and the capture accepts it.

        That is **correct for normal operation** — the delta cron appends a day many times
        between compactions and the base is legitimately older. The one pair that would lose
        data is a *pre-publish* base with a *post-prune* delta, where folded days exist in
        neither tier. That cannot reach a reader: §7.5 orders publish → verify → prune, and
        the delta prune retargets the delta path, which runs under request quiescence
        (pm2 stop → swap → start), so no reader is alive across it.

        This test exists so the weaker guarantee is recorded rather than assumed away."""
        cube = TieredCube(self.base, self.delta)
        old_delta_meta = self.delta._meta
        fx.append_delta_day(self.delta_path, "2027-04-04")
        self.delta.refresh()                       # delta moves on; base does not
        snap = cube.snapshot()

        self.assertIsNot(snap.delta_meta, old_delta_meta, "delta is the NEW generation")
        self.assertIs(snap.base_meta, self.base._meta, "base is its OWN, older generation")
        self.assertIn("2027-04-04", snap.days)
        # each half is internally consistent, which is what the capture actually guarantees
        self.assertEqual(set(snap.delta_days), set(snap.delta_meta.days))
        self.assertEqual(set(snap.base_days), set(snap.base_meta.days))

    def test_refresh_between_the_two_captures_leaves_each_tier_self_consistent(self):
        fired = {"n": 0}

        def refresh_delta_once(n):
            if n == 1 and fired["n"] == 0:      # exactly between base and delta capture
                fired["n"] = 1
                fx.append_delta_day(self.delta_path, "2027-02-02")
                self.delta.refresh()

        cube = TieredCube(self._interleaving_base(refresh_delta_once), self.delta)
        snap = cube.snapshot()
        self.assertEqual(fired["n"], 1, "the interleaved refresh must actually have happened")
        # whatever it returns must be SELF-CONSISTENT: the captured delta meta is the one
        # whose days the snapshot reports
        self.assertEqual(set(snap.delta_days), set(snap.delta_meta.days))
        self.assertEqual(set(snap.base_days), set(snap.base_meta.days))

    def test_a_never_settling_store_fails_closed_rather_than_mixing(self):
        """If metadata keeps moving under us, refuse. A mixed snapshot is worse than an
        error, which is the whole lesson of P4-S8a."""
        def always_refresh(n):
            self.delta.refresh()

        cube = TieredCube(self._interleaving_base(always_refresh), self.delta)
        # force delta._meta to be a NEW object on every refresh so the capture never settles
        real_build = self.delta._build_meta
        self.delta._build_meta = lambda: real_build()
        with self.assertRaises(SnapshotError) as cm:
            cube.snapshot()
        self.assertIn("stable", str(cm.exception).lower())

    def test_refresh_updates_both_tiers_under_one_lock(self):
        cube = TieredCube(self.base, self.delta)
        seen = []
        real = self.base.refresh

        def base_refresh():
            seen.append(cube._lock.locked())
            return real()
        self.base.refresh = base_refresh
        cube.refresh()
        self.assertEqual(seen, [True], "tier refreshes must happen under the composite lock")


# ============================================================ adversarial: segment metas
class TestSegmentMetadataIsCaptured(_Base):
    def test_snapshot_read_survives_inner_segment_meta_replacement(self):
        """R1 round 2: the outer sentinel test replaced `base._meta` but the grouped read
        still reached into each SEGMENT's live `_meta`. Replacing those must not matter."""
        base, base_days = self._two_blocks()
        cube = TieredCube(base, None)
        snap = cube.snapshot()

        sentinel = object()
        originals = {}
        for sid in ("b0", "b1"):
            st = base.segment_store(sid)
            originals[sid] = st._meta
            st._meta = sentinel
        try:
            rows = snap.point_series(LON, LAT, base_days[:5] + base_days[-5:], ["sst"])
            self.assertEqual(len(rows), 10)
        finally:
            for sid, m in originals.items():
                base.segment_store(sid)._meta = m


# ============================================================ R1a for a monolithic base
class TestDisjointnessForAnyBase(_Base):
    def test_timecubestore_base_equal_to_delta_is_rejected(self):
        """R1a was only enforced when the base happened to expose `assert_disjoint_from`.
        The CURRENT VM24 assembly uses a `TimeCubeStore` base, which does not — so the check
        silently did nothing exactly where it is deployed today."""
        days = fx.days_from("2026-12-24", 10)
        p = os.path.join(self.tmp, "delta.zarr")
        fx.build_delta(p, days)
        with self.assertRaises(SnapshotError) as cm:
            TieredCube(TimeCubeStore(p), TimeCubeStore(p))
        self.assertIn("disjoint", str(cm.exception).lower())

    def test_distinct_timecubestore_paths_are_fine(self):
        a = os.path.join(self.tmp, "a.zarr")
        b = os.path.join(self.tmp, "b.zarr")
        fx.build_delta(a, fx.days_from("2026-12-24", 5))
        fx.build_delta(b, fx.days_from("2027-01-01", 5))
        TieredCube(TimeCubeStore(a), TimeCubeStore(b))          # no raise

    def test_symlinked_delta_is_still_caught(self):
        real = os.path.join(self.tmp, "real.zarr")
        fx.build_delta(real, fx.days_from("2026-12-24", 5))
        link = os.path.join(self.tmp, "link.zarr")
        os.symlink(real, link)
        with self.assertRaises(SnapshotError):
            TieredCube(TimeCubeStore(real), TimeCubeStore(link))


# ============================================================ request-level query snapshot
class TestRequestLevelSnapshot(_Base):
    def test_availability_routing_and_read_share_one_snapshot(self):
        """A request used to re-snapshot for availability, again for routing, and again for
        the read. A refresh between them could answer 'available' and then read a view that
        no longer matches."""
        from store.hybrid_router import HybridRouter
        from store.store_access import StoreAccess

        base, base_days = self._two_blocks()
        delta_days = fx.days_from("2026-12-24", 10)
        delta, delta_path = self._delta(delta_days)
        daily_root = os.path.join(self.tmp, "mur.zarr")
        fx.build_daily(daily_root, delta_days[-2:], seed=700)
        router = HybridRouter(StoreAccess(daily_root), TieredCube(base, delta))

        qs = router.query_snapshot()
        days_before = list(qs.point_days())
        bounds_before = qs.point_bounds()

        fx.append_delta_day(delta_path, "2027-03-03")          # lands mid-"request"
        delta.refresh()

        self.assertEqual(list(qs.point_days()), days_before,
                         "a captured query snapshot must not see a later refresh")
        self.assertEqual(qs.point_bounds(), bounds_before)
        self.assertFalse(qs.point_day_present("2027-03-03"))
        rows = qs.point_series(LON, LAT, ["2027-03-03"], ["sst"])
        self.assertEqual(rows, [], "the day is not in this snapshot, so it is not served")

        fresh = router.query_snapshot()                        # a NEW request does see it
        self.assertTrue(fresh.point_day_present("2027-03-03"))

    def test_bbox_does_not_depend_on_the_cube_snapshot(self):
        """A bbox request reads the DAILY store only. Building a cube `QuerySnapshot` before
        the bbox/point split coupled bbox to cube health: a manifest that will not settle
        would fail a bbox that daily could have served perfectly well."""
        from store.hybrid_router import HybridRouter
        from store.store_access import StoreAccess
        from store.tiered_cube import TierCompositionError

        base, base_days = self._two_blocks()
        delta, _ = self._delta(fx.days_from("2026-12-24", 10))
        daily_root = os.path.join(self.tmp, "mur.zarr")
        fx.build_daily(daily_root, ["2027-05-05"], seed=700)
        cube = TieredCube(base, delta)
        router = HybridRouter(StoreAccess(daily_root), cube)

        def wont_settle():
            raise TierCompositionError("cube metadata will not settle")
        cube.snapshot = wont_settle

        with self.assertRaises(TierCompositionError):
            router.query_snapshot()                    # point/range path is affected
        # ...but the daily-only surface still works
        self.assertTrue(router.daily.day_present("2027-05-05"))
        rows = router.points_batch([(105.0, 5.0)], "2027-05-05", ["sst"])
        self.assertEqual(len(rows), 1)

    def test_bbox_HTTP_endpoint_still_serves_when_the_cube_snapshot_fails(self):
        """The router-level test above cannot catch a regression in `app.py`'s branch ORDER —
        it never enters `read_ghrsst`. This drives the real HTTP path: with `cube.snapshot()`
        raising, a bbox request must still return data from the daily store."""
        from fastapi.testclient import TestClient

        from api.app import app
        from store.tiered_cube import TierCompositionError

        daily_root = os.path.join(self.tmp, "mur.zarr")
        day = "2027-05-05"
        fx.build_daily(daily_root, [day], seed=700)

        # Restore EVERY global this test touches: three env vars and the app's router.
        # Restoring only GHRSST_ZARR_PATH leaves the others cleared and the router pointing at
        # a cube rigged to raise, which would leak into whatever runs next.
        env_keys = ("GHRSST_ZARR_PATH", "GHRSST_TIMECUBE_PATH", "GHRSST_DELTACUBE_PATH")
        prev_env = {k: os.environ.get(k) for k in env_keys}
        os.environ["GHRSST_ZARR_PATH"] = daily_root
        for k in env_keys[1:]:
            os.environ.pop(k, None)
        prev_router = getattr(app.state, "router", None)
        try:
            with TestClient(app) as client:
                base, _ = self._two_blocks()
                delta, _ = self._delta(fx.days_from("2026-12-24", 10))
                cube = TieredCube(base, delta)

                def wont_settle():
                    raise TierCompositionError("cube metadata will not settle")
                cube.snapshot = wont_settle
                app.state.router.cube = cube          # a cube that cannot be snapshotted

                r = client.get("/api/ghrsst", params={
                    "lon0": 100.0, "lat0": 0.0, "lon1": 103.0, "lat1": 3.0,
                    "start": day, "end": day, "append": "sst"})
                self.assertEqual(r.status_code, 200,
                                 f"bbox must not depend on cube health: {r.text[:200]}")
                self.assertGreater(len(r.json()), 0)

                # ...and the point path, which legitimately depends on the cube, still fails
                with self.assertRaises(TierCompositionError):
                    client.get("/api/ghrsst", params={
                        "lon0": 100.0, "lat0": 0.0, "start": day, "end": day,
                        "append": "sst"})
        finally:
            for k, v in prev_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            if prev_router is not None:
                app.state.router = prev_router

    def test_query_snapshot_freezes_daily_MEMBERSHIP_not_daily_DATA(self):
        """Documented limitation: the daily day-set is captured, the daily VALUES are read
        live. Freezing daily data would mean copying it; the contract is narrowed instead of
        overclaimed, and P4 already forbids a live overwrite of a visible day."""
        from store.hybrid_router import HybridRouter
        from store.store_access import StoreAccess

        base, _ = self._two_blocks()
        daily_root = os.path.join(self.tmp, "mur.zarr")
        fx.build_daily(daily_root, ["2027-05-05"], seed=700)
        router = HybridRouter(StoreAccess(daily_root), TieredCube(base, None))

        qs = router.query_snapshot()
        self.assertTrue(qs.point_day_present("2027-05-05"))
        fx.build_daily(daily_root, ["2027-06-06"], seed=701)      # a new day appears
        self.assertFalse(qs.point_day_present("2027-06-06"),
                         "membership IS frozen for the request")
        router.daily.existing_days(force=True)                    # past the day-scan TTL
        self.assertTrue(router.query_snapshot().point_day_present("2027-06-06"),
                        "a NEW request picks it up")

    def test_route_and_read_agree_within_one_snapshot(self):
        from store.hybrid_router import HybridRouter
        from store.store_access import StoreAccess

        base, base_days = self._two_blocks()
        delta, _ = self._delta(fx.days_from("2026-12-24", 10))
        daily_root = os.path.join(self.tmp, "mur.zarr")
        fx.build_daily(daily_root, ["2027-05-05"], seed=700)
        router = HybridRouter(StoreAccess(daily_root), TieredCube(base, delta))

        qs = router.query_snapshot()
        req = [base_days[0], "2027-05-05"]                     # cube day + daily-only day
        self.assertEqual(qs.route(req), "mixed")
        rows = qs.point_series(LON, LAT, req, ["sst"])
        self.assertEqual([r["date"] for r in rows], req, "mixed must not truncate")


# ============================================================ G1/G2: parity with the P1 oracle
class TestParityWithDailyOracle(_Base):
    """G1: every route must return what the P1 daily store returns, value for value."""

    def _oracle_and_cube(self, *, absent=None, nan_cell=None):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        s1, e1 = bm.block_bounds(ANCHOR, 90, 1)
        d0, d1 = fx.calendar_span(s0, e0), fx.calendar_span(s1, e1)
        daily_root = os.path.join(self.tmp, "mur.zarr")
        fx.build_daily(daily_root, d0 + d1, absent_vars_on=absent, nan_cell=nan_cell,
                       seed=700)
        # build blocks FROM the same daily data so parity is meaningful
        p0 = fx.build_block(os.path.join(self.root, "b0"), d0, seed=700,
                            absent_vars=(), nan_cells=None)
        return StoreAccess(daily_root), d0, d1, p0

    def test_values_match_the_daily_oracle_day_for_day(self):
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        days = fx.calendar_span(s0, e0)
        daily_root = os.path.join(self.tmp, "mur.zarr")
        fx.build_daily(daily_root, days, seed=700, nan_cell=(5, 5))
        blk = fx.build_block_from_daily(daily_root, days, os.path.join(self.root, "b0"))
        bm.publish(self.root, self._manifest(
            [_seg("b0", "b0", s0, e0, len(days), precedence=1, store_path=blk)]))
        cube = TieredCube(SegmentedCubeStore(self.root), None)

        oracle = StoreAccess(daily_root).point_series(LON, LAT, days, FIELDS)
        got = cube.point_series(LON, LAT, days, FIELDS)
        self.assertEqual([r["date"] for r in got], [r["date"] for r in oracle])
        for a, b in zip(oracle, got):
            for f in FIELDS:
                self.assertEqual(f in a, f in b, f"{f} presence differs on {a['date']}")
                if f in a:
                    if a[f] is None or b[f] is None:
                        self.assertEqual(a[f], b[f], f"{f} null-ness differs on {a['date']}")
                    else:
                        self.assertAlmostEqual(np.float32(a[f]), np.float32(b[f]), places=4)

    def test_parity_holds_when_a_variable_is_absent_on_some_days(self):
        """G1 with P1 omit semantics: a day missing `sea_ice` must omit the KEY, on both
        sides, while a land NaN keeps the key with a null."""
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        days = fx.calendar_span(s0, e0)
        daily_root = os.path.join(self.tmp, "mur.zarr")
        absent = {days[3]: ("sea_ice",), days[7]: ("sst_anomaly", "sea_ice")}
        fx.build_daily(daily_root, days, absent_vars_on=absent, nan_cell=(5, 5), seed=700)
        blk = fx.build_block_from_daily(daily_root, days, os.path.join(self.root, "b0"))
        bm.publish(self.root, self._manifest(
            [_seg("b0", "b0", s0, e0, len(days), precedence=1, store_path=blk)]))
        cube = TieredCube(SegmentedCubeStore(self.root), None)

        oracle = StoreAccess(daily_root).point_series(LON, LAT, days, FIELDS)
        got = cube.point_series(LON, LAT, days, FIELDS)
        self.assertEqual(len(got), len(oracle))
        for a, b in zip(oracle, got):
            self.assertEqual(sorted(a), sorted(b), f"key set differs on {a['date']}")

    def test_absent_var_across_a_segment_boundary_is_omitted_per_day(self):
        """F7: a variable absent in one block must be omitted for THAT block's days only."""
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        s1, e1 = bm.block_bounds(ANCHOR, 90, 1)
        d0, d1 = fx.calendar_span(s0, e0), fx.calendar_span(s1, e1)
        p0 = fx.build_block(os.path.join(self.root, "b0"), d0, seed=1,
                            absent_vars=("sea_ice",))
        p1 = fx.build_block(os.path.join(self.root, "b1"), d1, seed=90)
        segs = [_seg("b0", "b0", s0, e0, len(d0), precedence=1, store_path=p0,
                     vars_=("sst", "sst_anomaly")),
                _seg("b1", "b1", s1, e1, len(d1), precedence=2, store_path=p1)]
        bm.publish(self.root, self._manifest(segs))
        cube = TieredCube(SegmentedCubeStore(self.root), None)

        rows = cube.point_series(LON, LAT, [d0[0], d1[0]], FIELDS)
        self.assertNotIn("sea_ice", rows[0], "absent in b0 -> omitted for b0's days")
        self.assertIn("sea_ice", rows[1], "present in b1 -> returned for b1's days")

    def test_present_nan_is_null_not_omitted(self):
        """F8: land NaN -> null, which is NOT the same as an absent variable."""
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        days = fx.calendar_span(s0, e0)
        p0 = fx.build_block(os.path.join(self.root, "b0"), days, seed=1,
                            nan_cells=[(0, 5, 5)])
        bm.publish(self.root, self._manifest(
            [_seg("b0", "b0", s0, e0, len(days), precedence=1, store_path=p0)]))
        cube = TieredCube(SegmentedCubeStore(self.root), None)
        st = SegmentedCubeStore(self.root)
        ii, jj, glon, glat = st.nearest_indices(105.0, 5.0)
        rows = cube.point_series(float(glon), float(glat), [days[0]], ["sst"])
        # the NaN cell is (5,5); read exactly there
        lonv = 100.0 + 5
        rows = cube.point_series(lonv, 5.0, [days[0]], ["sst"])
        self.assertIn("sst", rows[0], "a NaN day must still carry the key")
        self.assertIsNone(rows[0]["sst"], "present-but-NaN must be null")


# ============================================================ G2/G12: ordering, dedup, routes
class TestOrderingAndRoutes(_Base):
    def test_requested_order_is_preserved_with_no_duplicates(self):
        base, base_days = self._two_blocks()
        delta_days = base_days[-5:] + fx.days_from("2026-12-24", 20)
        delta, _ = self._delta(delta_days)
        cube = TieredCube(base, delta)
        req = [base_days[100], base_days[0], delta_days[-1], base_days[50]]
        rows = cube.point_series(LON, LAT, req, ["sst"])
        dates = [r["date"] for r in rows]
        self.assertEqual(dates, req)
        self.assertEqual(len(dates), len(set(dates)))

    def test_delta_wins_on_overlap(self):
        """F5: overlap is legal and resolves to delta, silently and deterministically."""
        base, base_days = self._two_blocks()
        overlap = base_days[-3:]
        delta, _ = self._delta(overlap + fx.days_from("2026-12-24", 10))
        cube = TieredCube(base, delta)
        rows = cube.point_series(LON, LAT, [base_days[0], overlap[0]], ["sst"])
        self.assertLess(rows[0]["sst"], 500.0, "base-only day comes from the block")
        self.assertGreater(rows[1]["sst"], 500.0, "overlapping day comes from delta (+1000)")

    def test_gap_days_are_omitted_never_fabricated(self):
        """F6: a day nobody has is simply absent from the result."""
        s0, e0 = bm.block_bounds(ANCHOR, 90, 0)
        span = fx.calendar_span(s0, e0)
        missing = span[40]
        days = [d for d in span if d != missing]
        p0 = fx.build_block(os.path.join(self.root, "b0"), days, seed=1)
        bm.publish(self.root, self._manifest(
            [_seg("b0", "b0", s0, e0, len(days), precedence=1, store_path=p0,
                  gaps=[missing])]))
        cube = TieredCube(SegmentedCubeStore(self.root), None)
        rows = cube.point_series(LON, LAT, [span[39], missing, span[41]], ["sst"])
        self.assertEqual([r["date"] for r in rows], [span[39], span[41]])

    def test_append_order_delta_latest_is_chronological_max(self):
        """F4: a backfilled day sits at the TAIL of attrs['days']; latest must be max()."""
        days = fx.days_from("2026-12-24", 10)
        delta, path = self._delta(days)
        fx.append_delta_day(path, "2026-12-01")             # older day, appended last
        delta.refresh()
        self.assertEqual(delta.days[-1], "2026-12-01", "physical append order")
        self.assertEqual(delta.latest, max(delta.days))
        self.assertNotEqual(delta.latest, delta.days[-1])

        base, base_days = self._two_blocks()
        cube = TieredCube(base, delta)
        self.assertEqual(cube.latest, max(base_days + days + ["2026-12-01"]))
        rows = cube.point_series(LON, LAT, ["2026-12-01"], ["sst"])
        self.assertEqual(len(rows), 1, "the backfilled day must be readable by DATE")


# ============================================================ G6/G16 groundwork: grouped reads
class TestGroupedReads(_Base):
    def test_one_call_per_segment_and_no_opens_in_the_read_path(self):
        base, base_days = self._two_blocks()
        delta, _ = self._delta(fx.days_from("2026-12-24", 10))
        cube = TieredCube(base, delta)

        calls = {"n": 0}
        for sid in ("b0", "b1"):
            st = base.segment_store(sid)
            orig = st.point_series_from

            def counting(*a, _o=orig, **k):
                calls["n"] += 1
                return _o(*a, **k)
            st.point_series_from = counting

        import zarr as _z
        real_open, opened = _z.open_group, []

        def spy(path, *a, **k):
            opened.append(str(path))
            return real_open(path, *a, **k)
        _z.open_group = spy
        try:
            rows = cube.point_series(LON, LAT, base_days, ["sst"])
        finally:
            _z.open_group = real_open

        self.assertEqual(len(rows), len(base_days))
        self.assertEqual(calls["n"], 2, "one call per segment touched, not per day")
        self.assertEqual(opened, [], "the read path must open no store")


# ============================================================ F18: outage then bulk backfill
class TestOutageThenBulkBackfill(_Base):
    def test_two_week_gap_then_backfill(self):
        """R4/F18 — four invariants at once: append-order days, the gap being visible before
        repair, every backfilled day readable by date afterwards, and requested order held."""
        base, base_days = self._two_blocks()
        recent = fx.days_from("2026-12-24", 30)
        outage = recent[10:24]                              # 14 consecutive days missing
        present = [d for d in recent if d not in outage]
        delta, path = self._delta(present)
        cube = TieredCube(base, delta)

        rows = cube.point_series(LON, LAT, recent, ["sst"])
        self.assertEqual([r["date"] for r in rows], present,
                         "before repair the outage days are simply absent")

        for d in outage:                                    # bulk backfill, out of order
            fx.append_delta_day(path, d)
        delta.refresh()

        self.assertNotEqual(delta.days, sorted(delta.days), "append order, not sorted")
        self.assertEqual(delta.latest, max(delta.days))
        rows = cube.point_series(LON, LAT, recent, ["sst"])
        self.assertEqual([r["date"] for r in rows], recent, "all days back, requested order")
        self.assertEqual(len(rows), len(set(r["date"] for r in rows)))


# ============================================================ doc ↔ artifact consistency
class TestResultsDocMatchesArtifact(unittest.TestCase):
    """The committed results document must quote the committed artifact — nothing else.

    Transcription drift was the review finding on four consecutive rounds. Generating the
    table fixed the *generator*; this fixes the *guard*, which until now existed only as a
    command I happened to run. A check that lives in someone's shell history is exactly the
    kind of invariant that depends on memory, which is not an invariant.

    Anyone hand-editing either file now fails the suite.
    """

    ARTIFACT = os.path.join(HERE, "..", "bench", "results", "p5s2_segmented_read.json")
    DOC = os.path.join(HERE, "..", "specs", "p5s2_grouped_read_parity_results.md")

    @classmethod
    def setUpClass(cls):
        import json
        with open(cls.ARTIFACT) as fh:
            cls.art = json.load(fh)
        with open(cls.DOC) as fh:
            cls.doc = fh.read()

    def test_run_digest_is_reproducible_from_the_rows(self):
        """The digest must be derived from the rows, not merely stored beside them —
        otherwise a hand-edited artifact would still look self-consistent."""
        import hashlib
        import json
        recomputed = hashlib.sha256(
            json.dumps(self.art["rows"], sort_keys=True).encode()).hexdigest()[:16]
        self.assertEqual(recomputed, self.art["run_digest"],
                         "artifact rows do not hash to the stored run_digest")

    def test_doc_cites_the_artifact_run_digest(self):
        self.assertIn(self.art["run_digest"], self.doc,
                      "the results doc does not name the artifact run it quotes")

    def test_doc_contains_the_generated_table_verbatim(self):
        self.assertIn(self.art["markdown_table"], self.doc,
                      "the results table was edited by hand instead of copied from the "
                      "harness output")

    def test_doc_quotes_the_artifact_numbers(self):
        art = self.art
        checks = [
            (str(art["added_ms_per_extra_array_call"]), "per-call median"),
            (str(art["added_ms_per_extra_array_call_band"]), "per-call band"),
            (f"{art['projected_added_ms_at_s90']} ms", "projected added ms at S=90"),
            (f"{art['rows'][0]['range_366d']['p95_ms']:.1f} ms", "baseline 366-day p95"),
        ]
        lo, hi = art["projected_regression_pct_at_s90"]
        checks.append((f"+{lo}–{hi} %", "projection band"))
        for row in art["rows"]:
            checks.append((f"{row['crossing']['p95_ms']:.1f} ms",
                           f"crossing p95 @ {row['segments']} segments"))
            checks.append((f"{row['snapshot_open_ms']['p95_ms']:.1f}",
                           f"snapshot-open p95 @ {row['segments']} segments"))
        for needle, what in checks:
            with self.subTest(value=what):
                self.assertIn(needle, self.doc, f"{what}: {needle!r} not in the results doc")

    def test_g3_is_not_claimed_as_passed_anywhere(self):
        """A standing guard: G3 must stay deferred until S6/S7 measures it on production
        geometry. The synthetic ratio must never be promoted to a pass by a later edit."""
        self.assertFalse(self.art["g3_adjudicable_here"])
        self.assertIn("DEFERRED", self.art["g3_verdict"])
        self.assertNotIn("g3_pass", self.art)
        self.assertIn("DEFERRED", self.doc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
