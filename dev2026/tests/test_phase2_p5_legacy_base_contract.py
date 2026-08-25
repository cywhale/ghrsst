"""dev2026 — what a `legacy_base` generation 1 would have to be, and why it cannot ship yet.

A delta-only generation 1 is invalid: `api/app.py` treats a configured manifest as THE base and
does not fall back to the monolith beside it, so a manifest holding only delta-derived days
would drop the monolith's history from the served view. The correct generation 1 is a
`legacy_base` segment over the existing monolith — the schema has that kind, and
`p5_readiness.ALLOWED_LEGACY_PATHS` exists to let such a segment point outside the block root.

This suite builds one against a synthetic monolith, using only the existing contracts
(`block_manifest` for the segment and its fingerprints, `SegmentedCubeStore` for the read path),
and establishes what it does and does not achieve. **No second manifest or provenance format is
invented, and nothing here is shipped as an ops tool**, because of the blocker it documents:

  `api/app.py` constructs the store as `SegmentedCubeStore(mf_dir)` — with no
  `allowed_legacy_paths`. A `legacy_base` segment pointing at the production monolith therefore
  resolves outside the block root, is refused at construction, and the app's handler leaves
  `cube = None`: the service silently degrades to daily-only.

So the builder cannot be made safe by anything in the provisioner. It needs an API change that
passes the deployment's allow-list, which is its own review and its own deployment commit. What
this suite does is settle the semantics now, so that change has a specification and an oracle to
be checked against rather than starting from scratch.
"""
from __future__ import annotations

import math
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
_DEV = os.path.dirname(HERE)
for p in (_DEV, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

import numpy as np  # noqa: E402
import p5_fixtures as fx  # noqa: E402
from ops import p5_readiness as rd  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from store.segmented_cube import SegmentedCubeStore, SnapshotError  # noqa: E402
from store.time_cube import TimeCubeStore  # noqa: E402

NY = NX = 16
LON, LAT = 100.0, 1.0


def legacy_base_segment(monolith: str, manifest_root: str) -> dict:
    """A `legacy_base` segment over `monolith`, derived from disk through `block_manifest`.

    Nothing here is invented: the layout, the metadata fingerprint and the day digest all come
    from the same helpers a block segment uses, and the path is stored RELATIVE to the manifest
    root because `SegmentedCubeStore` refuses an absolute segment path outright — before it ever
    consults the allow-list."""
    insp = bm.inspect_store_contract(monolith)
    return {
        "segment_id": "legacy_base",
        "kind": "legacy_base",
        "path": os.path.relpath(monolith, manifest_root),
        "immutable": True,
        "boundary_kind": "legacy",
        "start_day": insp.days[0], "end_day": insp.days[-1],
        "materialized_through": insp.days[-1],
        "day_count": len(insp.days),
        "gaps": [], "unknown": [], "day_list": None,
        "layout": bm.segment_layout_from_inspection(insp),
        "variables": sorted(insp.vars),
        "fingerprint": {"algo": "sha256",
                        "metadata": bm.metadata_fingerprint_from_inspection(insp),
                        "day_digest": bm.day_digest(insp.days)},
        "precedence": 0, "sealed": True, "supersedes": None,
    }


def legacy_base_manifest(segment: dict, *, days) -> dict:
    m = {
        "format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION,
        "generation": 1, "generation_id": "gen000001-legacy-base",
        "created_utc": "2026-08-25T12:00:00Z", "created_by": "p5 legacy_base contract suite",
        "predecessor_generation": None, "predecessor_manifest": None,
        "grid": {"ny": NY, "nx": NX, "region": [0, NY, 0, NX]},
        "variables": list(segment["variables"]),
        "block_grid": {"anchor_day": days[0], "block_days": 90},
        "segments": [dict(segment)], "superseded": [], "manifest_checksum": "",
    }
    m["manifest_checksum"] = bm.compute_checksum(m)
    return m


class _LegacyBase(unittest.TestCase):
    """A synthetic `$G`: a monolith holding the long history, and a P5 manifest root beside it.

    Deliberately the production shape — the monolith is a sibling of the block root, not inside
    it — because that placement is the whole difficulty."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p5-legacy-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.G = os.path.join(self.tmp, "G")
        os.makedirs(self.G)
        self.monolith = os.path.join(self.G, "mur_timecube_s8_t90_sh128.zarr")
        self.mroot = os.path.join(self.G, "p5_blocks")
        os.makedirs(self.mroot)
        self.days = fx.days_from("2026-01-01", 20)
        fx.build_block(self.monolith, self.days, ny=NY, nx=NX)
        self.segment = legacy_base_segment(self.monolith, self.mroot)
        bm.publish(self.mroot, legacy_base_manifest(self.segment, days=self.days))

    _variant = 0

    def variant_root(self, segment, *, days):
        """Publish a variant into a FRESH manifest root.

        Generation archives are immutable — `block_manifest.publish` refuses to rewrite
        `manifest.gen000001.json` — so a test that wants a different generation 1 has to build
        it somewhere new rather than overwrite. That refusal is a contract worth keeping, and
        it is also why the provisioner must refuse an existing block root."""
        type(self)._variant += 1
        root = os.path.join(self.tmp, f"variant_blocks_{type(self)._variant}")
        os.makedirs(root)
        seg = dict(segment)
        if not os.path.isabs(seg["path"]):
            seg["path"] = os.path.relpath(self.monolith, root)
        bm.publish(root, legacy_base_manifest(seg, days=days))
        return root

    def store(self, root=None, **kw):
        return SegmentedCubeStore(root or self.mroot, **kw)

    def allowed(self, root=None):
        return self.store(root, allowed_legacy_paths=[self.monolith])

    def oracle(self):
        return TimeCubeStore(self.monolith)


class TestTheBlockerThatStopsItShipping(_LegacyBase):
    """Why no builder is shipped: the deployed read path cannot construct this manifest."""

    def test_the_api_constructs_the_store_without_an_allow_list(self):
        """The root cause, read from the source that does it."""
        with open(os.path.join(_DEV, "api", "app.py")) as fh:
            src = fh.read()
        self.assertIn("SegmentedCubeStore(mf_dir)", src)
        self.assertNotIn("allowed_legacy_paths", src,
                         "api/app.py now passes an allow-list; this blocker may be closed")

    def test_a_legacy_base_manifest_is_refused_by_that_construction(self):
        with self.assertRaises(SnapshotError) as caught:
            self.store()
        self.assertIn("allowed_legacy_paths", str(caught.exception))

    def test_the_app_would_degrade_to_daily_only_rather_than_raise(self):
        """The refusal is caught by the startup handler, which leaves `cube = None`. That is
        correct fail-closed behaviour and it is also a silent loss of the whole base — which is
        why this must not be provisioned before the API can accept it."""
        with open(os.path.join(_DEV, "api", "app.py")) as fh:
            src = fh.read()
        self.assertIn("cube load skipped", src)
        self.assertIn("cube = None", src)

    def test_the_deployment_allow_list_is_still_empty(self):
        """`ALLOWED_LEGACY_PATHS` is assessor configuration and fails closed at `()`. Even once
        the API accepts an allow-list, the deployment has to name the monolith explicitly."""
        self.assertEqual(rd.ALLOWED_LEGACY_PATHS, ())


class TestWhatTheSegmentMustBe(_LegacyBase):
    """The contract details, settled now so the later change has a specification."""

    def test_an_absolute_segment_path_is_refused_before_the_allow_list(self):
        """So the segment path must be stored relative to the manifest root — naming the
        monolith by absolute path does not work even when it is allow-listed."""
        root = self.variant_root(dict(self.segment, path=self.monolith), days=self.days)
        with self.assertRaises(SnapshotError) as caught:
            self.allowed(root)
        self.assertIn("absolute path", str(caught.exception))

    def test_the_relative_path_resolves_to_the_monolith(self):
        resolved = os.path.realpath(os.path.join(self.mroot, self.segment["path"]))
        self.assertEqual(resolved, os.path.realpath(self.monolith))
        self.assertFalse(os.path.isabs(self.segment["path"]))

    def test_the_allow_list_is_matched_by_realpath(self):
        """A symlink to the monolith is the same store; a path that merely looks similar is
        not."""
        link = os.path.join(self.tmp, "alias.zarr")
        os.symlink(self.monolith, link)
        self.allowed()                                    # the real path
        self.store(allowed_legacy_paths=[link])           # an alias resolving to it
        with self.assertRaises(SnapshotError):
            self.store(allowed_legacy_paths=[os.path.join(self.G, "mur_timecube_other.zarr")])

    def test_the_fingerprints_are_derived_from_disk_not_asserted(self):
        insp = bm.inspect_store_contract(self.monolith)
        self.assertEqual(self.segment["fingerprint"]["metadata"],
                         bm.metadata_fingerprint_from_inspection(insp))
        self.assertEqual(self.segment["fingerprint"]["day_digest"], bm.day_digest(insp.days))
        self.assertEqual(self.segment["layout"], bm.segment_layout_from_inspection(insp))

    def test_the_manifest_validates_under_the_real_schema(self):
        """No second format: it goes through `block_manifest.publish`, which validates."""
        live = bm.load_live(self.mroot)
        copy = dict(live)
        copy["manifest_checksum"] = ""
        self.assertEqual(bm.compute_checksum(copy), live["manifest_checksum"])
        self.assertEqual(live["segments"][0]["kind"], "legacy_base")
        self.assertEqual(live["segments"][0]["boundary_kind"], "legacy")


class TestItCoversTheWholeDayDomain(_LegacyBase):
    """The property a delta-only generation 1 fails: complete coverage."""

    def test_the_segment_covers_every_day_the_monolith_holds(self):
        insp = bm.inspect_store_contract(self.monolith)
        self.assertEqual(self.segment["start_day"], insp.days[0])
        self.assertEqual(self.segment["end_day"], insp.days[-1])
        self.assertEqual(self.segment["day_count"], len(insp.days))
        self.assertEqual(self.segment["unknown"], [])
        self.assertEqual(self.segment["gaps"], [])

    def test_the_served_day_domain_equals_the_monoliths(self):
        served = set(self.allowed().days)
        self.assertEqual(served, set(self.oracle().days))
        self.assertEqual(len(served), len(self.days))

    def test_a_legacy_base_declaring_fewer_days_than_the_store_holds_is_REFUSED(self):
        """Stronger than "it would lose history": the store will not serve it at all.

        A `legacy_base` segment is checked against the store it points at, so under-declaring
        the monolith is caught as a stale manifest rather than quietly narrowing the served
        range. This is the guarantee that makes a partially covered manifest unservable."""
        tail = self.days[-3:]
        seg = dict(self.segment, start_day=tail[0], end_day=tail[-1], day_count=len(tail),
                   materialized_through=tail[-1],
                   fingerprint=dict(self.segment["fingerprint"],
                                    day_digest=bm.day_digest(tail)))
        root = self.variant_root(seg, days=tail)
        with self.assertRaises(SnapshotError) as caught:
            self.allowed(root)
        self.assertIn("STALE MANIFEST", str(caught.exception))

    def test_the_real_hazard_is_a_BLOCK_segment_over_delta_only_data(self):
        """Where the danger actually lives, and why generation 1 must be `legacy_base`.

        A block built from delta is self-consistent — it holds exactly what its segment
        declares, so no staleness check fires — and it covers only the delta tail. Configure
        that as the manifest and the monolith is never consulted, because `api/app.py` takes
        the manifest as THE base. The manifest is valid; the served history is gone."""
        tail = self.days[-3:]
        block_root = os.path.join(self.tmp, "delta_only_blocks")
        os.makedirs(block_root)
        block = os.path.join(block_root, "block_v1.zarr")
        fx.build_block(block, tail, ny=NY, nx=NX)
        insp = bm.inspect_store_contract(block)
        # A block segment's bounds must match the block grid -- 90 calendar days from the
        # anchor -- so the days it does not hold are declared `unknown` rather than shortening
        # the span. That is exactly the shape `build_block` produces from a delta-only fold.
        span = fx.calendar_span(tail[0], bm.block_bounds(tail[0], 90, 0)[1])
        unknown = sorted(set(span) - set(insp.days))
        seg = {"segment_id": "block_v1", "kind": "block", "path": "block_v1.zarr",
               "immutable": True, "boundary_kind": "calendar",
               "start_day": span[0], "end_day": span[-1],
               "materialized_through": insp.days[-1], "day_count": len(insp.days),
               "gaps": [], "unknown": unknown, "day_list": None,
               "layout": bm.segment_layout_from_inspection(insp),
               "variables": sorted(insp.vars),
               "fingerprint": {"algo": "sha256",
                               "metadata": bm.metadata_fingerprint_from_inspection(insp),
                               "day_digest": bm.day_digest(insp.days)},
               # Unsealed, because a sealed block must have no unknown days (§5.4). A
               # delta-only fold is exactly this: an open block with most of its span unknown.
               "precedence": 0, "sealed": False, "supersedes": None}
        bm.publish(block_root, legacy_base_manifest(seg, days=tail))
        # No allow-list needed: the block is inside its own root, so this is exactly what the
        # API would construct — and it succeeds.
        served = set(SegmentedCubeStore(block_root).days)
        self.assertEqual(served, set(tail))
        lost = set(self.days) - served
        self.assertEqual(len(lost), len(self.days) - len(tail))
        self.assertTrue(lost, "the delta-only manifest lost nothing; the fixture is wrong")


class TestValuesMatchTheMonolithOracle(_LegacyBase):
    """`point_series()` through the manifest must equal the monolith, day for day."""

    def representative(self):
        """First, last, and a spread through the middle — boundaries included deliberately."""
        mid = self.days[len(self.days) // 2]
        return [self.days[0], self.days[1], mid, self.days[-2], self.days[-1]]

    def test_values_match_across_representative_days(self):
        want = self.oracle().point_series(LON, LAT, self.representative(), ["sst"])
        got = self.allowed().point_series(LON, LAT, self.representative(), ["sst"])
        self.assertEqual(len(got), len(want))
        for a, b in zip(want, got):
            with self.subTest(a.get("date")):
                self.assertEqual(a["date"], b["date"])
                self.assertEqual(a["sst"], b["sst"])

    def test_values_match_across_every_day_and_every_variable(self):
        variables = sorted(self.segment["variables"])
        want = self.oracle().point_series(LON, LAT, self.days, variables)
        got = self.allowed().point_series(LON, LAT, self.days, variables)
        self.assertEqual(len(got), len(want))
        for a, b in zip(want, got):
            with self.subTest(a.get("date")):
                self.assertEqual(a, b)

    def test_the_boundary_days_are_served_not_dropped(self):
        for day in (self.days[0], self.days[-1]):
            with self.subTest(day):
                rows = self.allowed().point_series(LON, LAT, [day], ["sst"])
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["date"], day)


class TestVarValidAndNaNSemanticsArePreserved(_LegacyBase):
    """A variable that is absent, and one that is present but NaN, are different facts."""

    def test_a_nan_value_reads_as_nan_through_both_paths(self):
        import zarr
        idx = 3
        day = self.days[idx]
        g = zarr.open_group(self.monolith, mode="a")
        arr = np.asarray(g["sst"][idx])
        arr[:] = np.nan
        g["sst"][idx] = arr
        # Re-derive the segment: the store changed, so its fingerprints did too.
        root = self.variant_root(legacy_base_segment(self.monolith, self.mroot),
                                 days=self.days)
        want = self.oracle().point_series(LON, LAT, [day], ["sst"])
        got = self.allowed(root).point_series(LON, LAT, [day], ["sst"])
        self.assertEqual(len(got), len(want))
        a, b = want[0].get("sst"), got[0].get("sst")
        self.assertEqual(a is None, b is None)
        if isinstance(a, float) and isinstance(b, float):
            self.assertEqual(math.isnan(a), math.isnan(b))

    def test_every_declared_variable_is_present_in_the_segment(self):
        insp = bm.inspect_store_contract(self.monolith)
        self.assertEqual(sorted(self.segment["variables"]), sorted(insp.vars))
        live = bm.load_live(self.mroot)
        self.assertEqual(sorted(live["variables"]), sorted(insp.vars))

    def test_implicit_var_valid_is_allowed_only_on_a_legacy_base_segment(self):
        """The schema rule, pinned: the waiver exists for the monolith and for nothing else."""
        seg = dict(self.segment, kind="block", boundary_kind="calendar",
                   var_valid_mode="implicit_all_true")
        with self.assertRaises(bm.ManifestError) as caught:
            self.variant_root(seg, days=self.days)
        self.assertIn("legacy_base", str(caught.exception))


class TestAPartiallyCoveredManifestCannotBeHealthy(_LegacyBase):
    """A manifest that does not cover what it claims must fail closed, never serve quietly."""

    def test_a_segment_naming_days_the_store_does_not_hold_is_refused(self):
        seg = dict(self.segment, end_day="2026-03-01", day_count=len(self.days))
        with self.assertRaises(Exception) as caught:
            root = self.variant_root(seg, days=self.days)
            self.allowed(root).point_series(LON, LAT, [self.days[0]], ["sst"])
        self.assertNotIsInstance(caught.exception, AssertionError)

    def test_a_wrong_day_digest_is_refused(self):
        """The fingerprint is what makes "this manifest describes that store" checkable."""
        seg = dict(self.segment)
        seg["fingerprint"] = dict(seg["fingerprint"], day_digest="0" * 64)
        root = self.variant_root(seg, days=self.days)
        with self.assertRaises(SnapshotError):
            self.allowed(root)

    def test_a_wrong_metadata_fingerprint_is_refused(self):
        seg = dict(self.segment)
        seg["fingerprint"] = dict(seg["fingerprint"], metadata="0" * 64)
        root = self.variant_root(seg, days=self.days)
        with self.assertRaises(SnapshotError):
            self.allowed(root)

    def test_an_empty_manifest_root_is_refused_rather_than_served_empty(self):
        empty = os.path.join(self.tmp, "empty_blocks")
        os.makedirs(empty)
        with self.assertRaises(Exception):
            SegmentedCubeStore(empty)


class TestEmptyRootsRemainInsufficient(unittest.TestCase):
    """What the production provisioner creates must not move any store requirement.

    Two empty directories are a precondition, not evidence. `manifest`, `blocks` and
    `wal_anchor` all have to keep refusing until a valid generation 1 and an initialized WAL
    exist — otherwise provisioning roots would look like progress toward readiness, which is
    the confusion this whole phase has been unpicking."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p5-empty-roots-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.G = os.path.join(self.tmp, "G")
        # Exactly what ops/p5_prod_provision.py produces: two empty directories.
        for name in ("p5_blocks", "p5_repairs"):
            os.makedirs(os.path.join(self.G, name))
        import socket
        self.target = {
            "label": rd.QUALIFYING_LABEL, "host": socket.gethostname(),
            "root": self.G,
            "manifest_root": os.path.join(self.G, "p5_blocks"),
            "wal_root": os.path.join(self.G, "p5_repairs"),
            "anchor_root": os.path.join(self.tmp, "anchor"),
        }

    def test_manifest_is_still_refused(self):
        verdict, reason = rd._check_manifest(self.target)
        self.assertEqual(verdict, rd.BLOCKED, reason)

    def test_blocks_is_still_refused(self):
        verdict, reason = rd._check_blocks(self.target)
        self.assertEqual(verdict, rd.BLOCKED, reason)

    def test_wal_anchor_is_still_refused(self):
        verdict, reason = rd._check_wal_anchor(self.target)
        self.assertEqual(verdict, rd.BLOCKED, reason)

    def test_empty_roots_do_not_make_a_deployment_ready(self):
        report = rd.assess(dict(self.target, identity={}))
        self.assertFalse(report["compaction_ready"])
