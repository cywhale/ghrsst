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
from api import app as app_mod  # noqa: E402
from ops import p5_legacy_base as lb  # noqa: E402
from store import repair_wal as rw  # noqa: E402

NY = NX = 16
LON, LAT = 100.0, 1.0


def seal(case, path):
    """Make a store read-only, as a production legacy base must be, and restore it on cleanup.

    Registered after the tempdir cleanup so it runs BEFORE it -- `rmtree` cannot remove a
    read-only directory."""
    os.chmod(path, 0o555)
    case.addCleanup(unseal, path)


def unseal(path):
    try:
        os.chmod(path, 0o755)
    except OSError:
        pass


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
        seal(self, self.monolith)
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


class TestTheRuntimePathNowAuthorizesLegacyBase(_LegacyBase):
    """The blocker this file was written to document is closed, and this is what closed it.

    `api/app.py` used to construct `SegmentedCubeStore(mf_dir)` with no allow-list, so a
    `legacy_base` segment pointing at the production monolith was refused and the service
    degraded to daily-only. It now passes the deployment's configured list — and only that
    list, never anything the manifest says."""

    def test_the_api_passes_a_deployment_configured_allow_list(self):
        with open(os.path.join(_DEV, "api", "app.py")) as fh:
            src = fh.read()
        self.assertIn("allowed_legacy_paths=resolve_allowed_legacy_paths", src)
        self.assertIn(app_mod.ALLOWED_LEGACY_ENV, src)

    def test_without_the_allow_list_the_external_segment_is_still_refused(self):
        """Absent authorization still means no external segment. The fix authorizes; it does
        not open."""
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


class TestTheRuntimeAllowListIsDeploymentControlled(unittest.TestCase):
    """The one setting that authorizes an external `legacy_base` path — validated fail-closed.

    A manifest must never authorize its own external path: it is a document, and a document
    that grants itself permission to read anything on the host is not a boundary. So the
    authorization comes from `GHRSST_ALLOWED_LEGACY_PATHS`, it is bound to the monolith the
    deployment already declares, and every way of getting it wrong is a refusal rather than a
    skipped entry."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="p5-allowlist-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.G = os.path.realpath(os.path.join(self.tmp, "G"))
        os.makedirs(self.G)
        self.monolith = os.path.join(self.G, "mur_timecube.zarr")
        os.makedirs(self.monolith)
        self.other = os.path.join(self.G, "another_store.zarr")
        os.makedirs(self.other)

    _DEFAULT = object()

    def resolve(self, value, *, timecube=_DEFAULT):
        """`timecube=None` means "not configured"; omitting it means "the fixture monolith".

        A single `None` sentinel conflated the two, so the no-monolith case silently ran with
        one configured and tested nothing."""
        env = {}
        if value is not None:
            env[app_mod.ALLOWED_LEGACY_ENV] = value
        tc = self.monolith if timecube is self._DEFAULT else timecube
        if tc is not None:
            env["GHRSST_TIMECUBE_PATH"] = tc
        return app_mod.resolve_allowed_legacy_paths(env)

    # ---------------------------------------------------------------- accepted
    def test_a_valid_entry_naming_the_deployed_monolith_is_accepted(self):
        self.assertEqual(self.resolve(self.monolith), [self.monolith])

    def test_absent_configuration_is_legal_and_authorizes_nothing(self):
        """A manifest whose segments all live inside the block root needs no allow-list, and
        that deployment must still start — authorizing nothing, not everything."""
        self.assertEqual(self.resolve(None), [])
        self.assertEqual(self.resolve(""), [])
        self.assertEqual(self.resolve("   "), [])

    # ---------------------------------------------------------------- refused
    def test_a_path_that_is_not_the_deployed_monolith_is_refused(self):
        with self.assertRaises(app_mod.LegacyAllowListError) as caught:
            self.resolve(self.other)
        self.assertIn("not the deployed monolith", str(caught.exception))

    def test_a_relative_path_is_refused(self):
        with self.assertRaises(app_mod.LegacyAllowListError) as caught:
            self.resolve("mur_timecube.zarr")
        self.assertIn("not absolute", str(caught.exception))

    def test_a_missing_directory_is_refused(self):
        with self.assertRaises(app_mod.LegacyAllowListError) as caught:
            self.resolve(os.path.join(self.G, "not_there.zarr"))
        self.assertIn("not an existing directory", str(caught.exception))

    def test_a_symlink_alias_is_refused_even_though_it_resolves_correctly(self):
        """The store would accept it — it compares by `realpath`. It is refused here because a
        reader of the configuration cannot see where it points, and re-pointing the link would
        move what is authorized without the configuration changing."""
        alias = os.path.join(self.tmp, "alias.zarr")
        os.symlink(self.monolith, alias)
        with self.assertRaises(app_mod.LegacyAllowListError) as caught:
            self.resolve(alias)
        self.assertIn("resolves to", str(caught.exception))

    def test_a_symlink_escaping_to_another_store_is_refused(self):
        escape = os.path.join(self.G, "looks_right.zarr")
        os.symlink(self.other, escape)
        with self.assertRaises(app_mod.LegacyAllowListError):
            self.resolve(escape)

    def test_an_empty_entry_in_a_list_is_refused(self):
        with self.assertRaises(app_mod.LegacyAllowListError) as caught:
            self.resolve(self.monolith + os.pathsep + os.pathsep + self.monolith)
        self.assertIn("empty entry", str(caught.exception))

    def test_a_non_string_setting_is_refused(self):
        with self.assertRaises(app_mod.LegacyAllowListError):
            app_mod.resolve_allowed_legacy_paths(
                {app_mod.ALLOWED_LEGACY_ENV: ["/x"], "GHRSST_TIMECUBE_PATH": self.monolith})

    def test_it_is_refused_when_no_monolith_is_configured_to_bind_to(self):
        with self.assertRaises(app_mod.LegacyAllowListError) as caught:
            self.resolve(self.monolith, timecube=None)
        self.assertIn("nothing to check the entry against", str(caught.exception))

    def test_the_manifest_cannot_supply_the_allow_list(self):
        """The setting is read from the environment and nowhere else.

        By AST over the function's CODE, not its prose: the docstring explains at length why a
        manifest must not authorize itself, and a substring scan cannot tell that explanation
        from an implementation doing it."""
        import ast
        import inspect
        tree = ast.parse(inspect.getsource(app_mod.resolve_allowed_legacy_paths))
        names, called = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                called.add(node.func.attr)
        self.assertIn("environ", names)
        for banned in ("load_live", "inspect_store_contract", "segments", "manifest"):
            with self.subTest(banned):
                self.assertNotIn(banned, names)
                self.assertNotIn(banned, called)


class TestTheRealApiLifespanServesLegacyBase(unittest.TestCase):
    """End to end through the app's own lifespan, not through helper functions.

    The helper tests above prove the allow-list validates. This proves the deployment actually
    serves — that `SegmentedCubeStore` is constructed with the list, that a `legacy_base`
    segment over an external monolith loads, and that the values reaching a client are the
    monolith's."""

    ENV = ("GHRSST_ZARR_PATH", "GHRSST_BLOCK_MANIFEST_DIR", "GHRSST_TIMECUBE_PATH",
           "GHRSST_DELTACUBE_PATH", "GHRSST_ALLOWED_LEGACY_PATHS")

    def setUp(self):
        from store.zarr_paths import group_path
        import zarr
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="p5-lifespan-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.G = os.path.join(self.tmp, "G")
        os.makedirs(self.G)
        self.monolith = os.path.join(self.G, "mur_timecube_s8_t90_sh128.zarr")
        self.mroot = os.path.join(self.G, "p5_blocks")
        os.makedirs(self.mroot)
        self.days = fx.days_from("2026-01-01", 12)
        fx.build_block(self.monolith, self.days, ny=NY, nx=NX)
        seg = legacy_base_segment(self.monolith, self.mroot)
        bm.publish(self.mroot, legacy_base_manifest(seg, days=self.days))
        # A daily store, because StoreAccess needs one to boot.
        self.daily = os.path.join(self.tmp, "daily")
        day = self.days[-1]
        g = zarr.open_group(group_path(self.daily, day), mode="w", zarr_format=3)
        g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,))
        g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,))
        g["lon"][:] = np.linspace(100.0, 100.0 + NX - 1, NX, dtype=np.float32)
        g["lat"][:] = np.linspace(0.0, NY - 1.0, NY, dtype=np.float32)
        for v in fx.VARS:
            g.create_array(v, shape=(1, NY, NX), dtype="float32", chunks=(1, 8, 8))
            g[v][0] = np.full((NY, NX), 42.0, dtype=np.float32)

    def boot(self, **env):
        from fastapi.testclient import TestClient
        from api.app import app
        saved = {k: os.environ.get(k) for k in self.ENV}
        for k in self.ENV:
            os.environ.pop(k, None)
        os.environ["GHRSST_ZARR_PATH"] = self.daily
        for k, v in env.items():
            os.environ[k] = v
        cm = TestClient(app)
        client = cm.__enter__()

        def restore():
            cm.__exit__(None, None, None)
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.addCleanup(restore)
        return client, app.state.router.cube

    def test_with_the_allow_list_the_legacy_base_loads_and_serves(self):
        client, cube = self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.mroot,
                                 GHRSST_TIMECUBE_PATH=self.monolith,
                                 GHRSST_ALLOWED_LEGACY_PATHS=self.monolith)
        h = client.get("/healthz").json()
        self.assertEqual(h["status"], "ok")
        self.assertTrue(h["cube_loaded"])
        self.assertEqual(h["cube_base_kind"], "segmented")
        self.assertEqual(h["cube_segment_count"], 1)
        self.assertEqual(h["cube_generation"], 1)
        self.assertIsNone(h["cube_error"])
        self.assertEqual(h["cube_day_count"], len(self.days))
        self.assertIsInstance(cube, SegmentedCubeStore)

    def test_the_values_served_are_the_monoliths(self):
        client, _ = self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.mroot,
                              GHRSST_TIMECUBE_PATH=self.monolith,
                              GHRSST_ALLOWED_LEGACY_PATHS=self.monolith)
        oracle = TimeCubeStore(self.monolith)
        for day in (self.days[0], self.days[len(self.days) // 2], self.days[-1]):
            with self.subTest(day):
                r = client.get(f"/api/ghrsst?lon0={LON}&lat0={LAT}"
                               f"&start={day}&end={day}&append=sst")
                self.assertEqual(r.status_code, 200, r.text)
                served = r.json()[0]["sst"]
                want = oracle.point_series(LON, LAT, [day], ["sst"])[0]["sst"]
                self.assertAlmostEqual(served, want, places=5)

    def test_without_the_allow_list_it_is_degraded_and_does_not_fall_back(self):
        """The requirement in one test: no authorization, no cube, and NO monolith fallback —
        even though `GHRSST_TIMECUBE_PATH` names a perfectly good monolith."""
        client, cube = self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.mroot,
                                 GHRSST_TIMECUBE_PATH=self.monolith)
        h = client.get("/healthz").json()
        self.assertIsNone(cube, "a cube was installed without authorization")
        self.assertEqual(h["status"], "degraded")
        self.assertFalse(h["cube_loaded"])
        self.assertTrue(h["manifest_configured"])
        self.assertIn("allowed_legacy_paths", h["cube_error"])
        self.assertIsNone(h["cube_base_kind"], "the monolith was served as a fallback")

    def test_an_invalid_allow_list_is_degraded_not_healthy(self):
        other = os.path.join(self.G, "some_other_store.zarr")
        os.makedirs(other)
        client, cube = self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.mroot,
                                 GHRSST_TIMECUBE_PATH=self.monolith,
                                 GHRSST_ALLOWED_LEGACY_PATHS=other)
        h = client.get("/healthz").json()
        self.assertIsNone(cube)
        self.assertEqual(h["status"], "degraded")
        self.assertIn("LegacyAllowListError", h["cube_error"])
        self.assertIn("not the deployed monolith", h["cube_error"])

    def test_a_malformed_allow_list_is_degraded_not_healthy(self):
        client, cube = self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.mroot,
                                 GHRSST_TIMECUBE_PATH=self.monolith,
                                 GHRSST_ALLOWED_LEGACY_PATHS="relative/path")
        h = client.get("/healthz").json()
        self.assertIsNone(cube)
        self.assertEqual(h["status"], "degraded")
        self.assertIn("not absolute", h["cube_error"])

    def test_the_monolith_fallback_still_works_when_no_manifest_is_configured(self):
        """The existing behaviour, preserved: with no manifest the monolith is the base and the
        allow-list is irrelevant."""
        client, cube = self.boot(GHRSST_TIMECUBE_PATH=self.monolith)
        h = client.get("/healthz").json()
        self.assertTrue(h["cube_loaded"])
        self.assertEqual(h["status"], "ok")
        self.assertEqual(h["cube_base_kind"], "monolith")
        self.assertFalse(h["manifest_configured"])
        self.assertIsInstance(cube, TimeCubeStore)

    def test_an_under_declared_legacy_manifest_refuses_before_service_use(self):
        """A stale manifest must fail at construction, not at the first request."""
        stale_root = os.path.join(self.tmp, "stale_blocks")
        os.makedirs(stale_root)
        tail = self.days[-3:]
        seg = legacy_base_segment(self.monolith, stale_root)
        seg.update(start_day=tail[0], end_day=tail[-1], day_count=len(tail),
                   materialized_through=tail[-1],
                   fingerprint=dict(seg["fingerprint"], day_digest=bm.day_digest(tail)))
        bm.publish(stale_root, legacy_base_manifest(seg, days=tail))
        client, cube = self.boot(GHRSST_BLOCK_MANIFEST_DIR=stale_root,
                                 GHRSST_TIMECUBE_PATH=self.monolith,
                                 GHRSST_ALLOWED_LEGACY_PATHS=self.monolith)
        h = client.get("/healthz").json()
        self.assertIsNone(cube)
        self.assertEqual(h["status"], "degraded")
        self.assertIn("STALE MANIFEST", h["cube_error"])


class TestTheGenerationOneBuilder(unittest.TestCase):
    """`ops/p5_legacy_base.py` — everything derived from the monolith, proved against an oracle.

    The builder exists only now that the runtime path can serve what it produces. It publishes
    into a manifest root it is given; it creates no roots, touches no WAL, and knows nothing
    about VM24 or `ops/p5_prod_provision.py`."""

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="p5-genone-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.G = os.path.join(self.tmp, "G")
        os.makedirs(self.G)
        self.monolith = os.path.join(self.G, "mur_timecube_s8_t90_sh128.zarr")
        self.mroot = os.path.join(self.G, "p5_blocks")
        os.makedirs(self.mroot)
        self.days = fx.days_from("2026-01-01", 15)
        fx.build_block(self.monolith, self.days, ny=NY, nx=NX)
        seal(self, self.monolith)

    def build(self, **over):
        kw = dict(monolith_path=self.monolith, manifest_root=self.mroot,
                  at_utc="2026-08-25T12:00:00Z", operator="tests")
        kw.update(over)
        return lb.build_generation_one(**kw)

    def store(self):
        return SegmentedCubeStore(self.mroot, allowed_legacy_paths=[self.monolith])

    # ------------------------------------------------- derived, not asserted
    def test_it_covers_the_complete_monolith_day_domain(self):
        out = self.build()
        insp = bm.inspect_store_contract(self.monolith)
        self.assertEqual(out["days"], len(insp.days))
        self.assertEqual(out["first_day"], insp.days[0])
        self.assertEqual(out["last_day"], insp.days[-1])
        self.assertEqual(set(self.store().days), set(insp.days))

    def test_the_variables_come_from_the_store(self):
        out = self.build()
        insp = bm.inspect_store_contract(self.monolith)
        self.assertEqual(out["variables"], sorted(insp.vars))
        self.assertEqual(sorted(bm.load_live(self.mroot)["variables"]), sorted(insp.vars))

    def test_the_grid_comes_from_the_store_not_from_a_caller(self):
        import inspect as _i
        self.assertNotIn("grid", _i.signature(lb.build_generation_one).parameters)
        self.build()
        live = bm.load_live(self.mroot)
        insp = bm.inspect_store_contract(self.monolith)
        self.assertEqual(live["grid"]["ny"], insp.ny)
        self.assertEqual(live["grid"]["nx"], insp.nx)

    def test_the_fingerprints_are_recomputable_from_the_monolith(self):
        out = self.build()
        insp = bm.inspect_store_contract(self.monolith)
        self.assertEqual(out["fingerprint"]["metadata"],
                         bm.metadata_fingerprint_from_inspection(insp))
        self.assertEqual(out["fingerprint"]["day_digest"], bm.day_digest(insp.days))

    def test_the_var_valid_mode_follows_the_store(self):
        """`implicit_all_true` is a statement about a store that carries no validity arrays.
        Claiming it over one that does would read every absent variable as present."""
        out = self.build()
        insp = bm.inspect_store_contract(self.monolith)
        expected = "implicit_all_true" if insp.var_valid_implicit else "explicit"
        self.assertEqual(out["var_valid_mode"], expected)

    def test_the_segment_path_is_relative_and_the_allow_list_is_separate(self):
        out = self.build()
        self.assertFalse(os.path.isabs(out["segment_path_relative"]))
        self.assertEqual(out["requires_runtime_allow_list"],
                         os.path.realpath(self.monolith))
        # The manifest names the path; it does not authorize it.
        with self.assertRaises(SnapshotError):
            SegmentedCubeStore(self.mroot)

    def test_it_publishes_through_the_existing_publisher(self):
        import inspect as _i
        src = _i.getsource(lb)
        self.assertIn("block_manifest.publish(", src)
        self.build()
        live = bm.load_live(self.mroot)
        copy = dict(live)
        copy["manifest_checksum"] = ""
        self.assertEqual(bm.compute_checksum(copy), live["manifest_checksum"])

    def test_it_cannot_silently_replace_an_existing_generation(self):
        self.build()
        with self.assertRaises(bm.ManifestError) as caught:
            self.build()
        self.assertIn("archive already exists", str(caught.exception))

    # ------------------------------------------------- the oracle
    def test_every_day_and_every_variable_matches_the_monolith_oracle(self):
        self.build()
        variables = sorted(bm.inspect_store_contract(self.monolith).vars)
        want = TimeCubeStore(self.monolith).point_series(LON, LAT, self.days, variables)
        got = self.store().point_series(LON, LAT, self.days, variables)
        self.assertEqual(len(got), len(want))
        for a, b in zip(want, got):
            with self.subTest(a.get("date")):
                self.assertEqual(a, b)

    def test_the_first_and_last_days_are_served(self):
        self.build()
        for day in (self.days[0], self.days[-1]):
            with self.subTest(day):
                rows = self.store().point_series(LON, LAT, [day], ["sst"])
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["date"], day)

    def test_nan_values_survive_the_manifest_path(self):
        import zarr
        idx = 4
        day = self.days[idx]
        g = zarr.open_group(self.monolith, mode="a")
        arr = np.asarray(g["sst"][idx])
        arr[:] = np.nan
        g["sst"][idx] = arr
        self.build()
        want = TimeCubeStore(self.monolith).point_series(LON, LAT, [day], ["sst"])[0]
        got = self.store().point_series(LON, LAT, [day], ["sst"])[0]
        a, b = want.get("sst"), got.get("sst")
        self.assertEqual(a is None, b is None)
        if isinstance(a, float) and isinstance(b, float):
            self.assertEqual(math.isnan(a), math.isnan(b))

    # ------------------------------------------------- refusals
    def test_a_directory_that_is_not_a_store_is_refused(self):
        """Refused by the inspection, before the day-count guard is reached."""
        empty = os.path.join(self.G, "not_a_store")
        os.makedirs(empty)
        with self.assertRaises(Exception) as caught:
            self.build(monolith_path=empty)
        self.assertNotIsInstance(caught.exception, AssertionError)

    def test_a_store_with_no_days_is_refused_by_the_inspection_itself(self):
        """Where the day-domain refusal lives, checked rather than duplicated.

        The builder carries no second day-count guard: `inspect_store_contract` is the
        authority and refuses a dayless store, so a guard after it could never fire. An
        unreachable branch is not a check, and it would read like one."""
        import zarr
        dayless = os.path.join(self.G, "dayless.zarr")
        g = zarr.open_group(dayless, mode="w", zarr_format=3)
        g.attrs["days"] = []
        g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,))
        g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,))
        g["lon"][:] = np.linspace(100.0, 100.0 + NX - 1, NX, dtype=np.float32)
        g["lat"][:] = np.linspace(0.0, NY - 1.0, NY, dtype=np.float32)
        for v in fx.VARS:
            g.create_array(v, shape=(0, NY, NX), dtype="float32", chunks=(1, 8, 8))
        seal(self, dayless)
        with self.assertRaises(Exception) as inspected:
            bm.inspect_store_contract(dayless)
        with self.assertRaises(type(inspected.exception)):
            self.build(monolith_path=dayless)
        import inspect as _i
        self.assertNotIn("holds no days", _i.getsource(lb),
                         "a second day-count guard was added after the inspection, where it "
                         "can never fire")

    def test_a_missing_monolith_is_refused(self):
        with self.assertRaises(lb.LegacyBaseRefused):
            self.build(monolith_path=os.path.join(self.G, "not_there.zarr"))

    def test_a_missing_manifest_root_is_refused(self):
        with self.assertRaises(lb.LegacyBaseRefused) as caught:
            self.build(manifest_root=os.path.join(self.G, "no_root"))
        self.assertIn("does not create the root", str(caught.exception))

    def test_it_creates_no_roots_and_touches_no_wal(self):
        import inspect as _i
        src = _i.getsource(lb)
        for banned in ("initialize_wal", "makedirs", "p5_repairs", "CompactionLock"):
            with self.subTest(banned):
                self.assertNotIn(banned, src)

    def test_a_delta_only_block_cannot_pass_as_a_legacy_base(self):
        """The shape the first provisioner published: a block over the delta tail. It is a
        `block`, not a `legacy_base`, and the schema will not let it claim the legacy waiver."""
        tail = self.days[-3:]
        block_root = os.path.join(self.tmp, "delta_blocks")
        os.makedirs(block_root)
        block = os.path.join(block_root, "block_v1.zarr")
        fx.build_block(block, tail, ny=NY, nx=NX)
        seg = lb.legacy_base_segment(block, block_root)
        seg.update(kind="block", boundary_kind="calendar")
        seg["var_valid_mode"] = "implicit_all_true"
        m = legacy_base_manifest(seg, days=tail)
        with self.assertRaises(bm.ManifestError) as caught:
            bm.publish(block_root, m)
        self.assertIn("legacy_base", str(caught.exception))


class TestTheMonolithIsNotSealedByTheFingerprintsAlone(_LegacyBase):
    """What the manifest's fingerprints do and do not cover, established rather than assumed.

    The design calls the monolith "immutable, NEVER rebuilt" (§4) — an intention about how the
    deployment is operated, not something any code enforces. Nothing in this project writes to
    it, but nothing stops a write either, so the question is what the read path notices.

    Measured: the metadata fingerprint catches a changed day set or array layout, and the day
    digest catches a changed day list. **Neither covers chunk bytes.** A value rewritten in
    place inside an existing day changed neither, and was served as though nothing happened —
    the manifest still describing a store that no longer holds what it says.

    `content_seal` closes that. It is change detection over file metadata (path, size, mtime),
    not a cryptographic seal: hashing ~2 TB at every service start is not available, which is
    why the fingerprints are metadata-only in the first place. It catches a store that was
    written to, which is the failure that matters."""

    def setUp(self):
        super().setUp()
        # `_LegacyBase.setUp` already published a generation into `self.mroot`, and archives are
        # immutable -- so a class that builds its OWN generation needs a root nobody has used.
        self.mroot = os.path.join(self.tmp, "built_blocks")
        os.makedirs(self.mroot)
        self.segment = legacy_base_segment(self.monolith, self.mroot)

    def seal_of(self, root=None):
        live = bm.load_live(root or self.mroot)
        return live["segments"][0].get("content_seal")

    def test_a_generation_one_carries_a_content_seal(self):
        out = lb.build_generation_one(monolith_path=self.monolith, manifest_root=self.mroot,
                                      at_utc="t", operator="tests")
        seal = self.seal_of()
        self.assertIsNotNone(seal, "the generation carries no content seal")
        self.assertEqual(seal["algo"], "sha256-path-size-mtime")
        self.assertGreater(seal["file_count"], 0)
        self.assertEqual(out["content_seal"]["ledger_digest"], seal["ledger_digest"])

    def test_it_is_named_as_change_detection_not_as_a_seal(self):
        """The claim has to match what it does, or someone will rely on the stronger one."""
        import inspect as _i
        doc = _i.getdoc(bm.content_ledger) or ""
        self.assertIn("not a cryptographic seal", doc)
        self.assertIn("METADATA, not contents", doc)

    def test_a_value_rewritten_in_place_is_REFUSED_not_served(self):
        """The gap, closed. Before the seal this served the mutated value silently."""
        import zarr
        lb.build_generation_one(monolith_path=self.monolith, manifest_root=self.mroot,
                                at_utc="t", operator="tests")
        unseal(self.monolith)                 # the mutation these tests exist to catch
        day_index = 3
        before = self.allowed().point_series(
            LON, LAT, [self.days[day_index]], ["sst"])[0]["sst"]
        g = zarr.open_group(self.monolith, mode="a")
        arr = np.asarray(g["sst"][day_index])
        g["sst"][day_index] = arr + 100.0
        with self.assertRaises(SnapshotError) as caught:
            self.allowed()
        self.assertIn("content_seal mismatch", str(caught.exception))
        self.assertIn("changed since the manifest described it", str(caught.exception))
        del before

    def test_without_the_seal_that_same_mutation_is_invisible(self):
        """The counter-example, so the seal is shown to be what refuses rather than something
        else happening to. Same store, same mutation, seal removed from the segment."""
        import zarr
        lb.build_generation_one(monolith_path=self.monolith, manifest_root=self.mroot,
                                at_utc="t", operator="tests")
        unseal(self.monolith)
        seg = dict(bm.load_live(self.mroot)["segments"][0])
        seg.pop("content_seal")
        root = self.variant_root(seg, days=self.days)
        g = zarr.open_group(self.monolith, mode="a")
        g["sst"][3] = np.asarray(g["sst"][3]) + 100.0
        served = self.allowed(root).point_series(LON, LAT, [self.days[3]], ["sst"])[0]["sst"]
        self.assertIsNotNone(served,
                             "without the seal the mutated value is served; that is the gap")

    def test_a_day_appended_after_the_build_is_refused(self):
        """Already covered by the metadata fingerprint, recorded so the seal is not credited
        with a refusal that predates it."""
        import zarr
        lb.build_generation_one(monolith_path=self.monolith, manifest_root=self.mroot,
                                at_utc="t", operator="tests")
        unseal(self.monolith)
        g = zarr.open_group(self.monolith, mode="a")
        for v in fx.VARS:
            arr = np.asarray(g[v][:])
            g[v].resize((arr.shape[0] + 1, arr.shape[1], arr.shape[2]))
        g.attrs["days"] = list(self.days) + ["2026-01-21"]
        with self.assertRaises(SnapshotError):
            self.allowed()

    def test_a_manifest_written_before_the_field_existed_still_loads(self):
        """Additive: absent seal means nothing to check, not a refusal."""
        seg = dict(self.segment)
        seg.pop("content_seal", None)
        root = self.variant_root(seg, days=self.days)
        self.assertEqual(set(self.allowed(root).days), set(self.days))


class TestTheBlockGridIsPolicyNotAnObservation(_LegacyBase):
    """`block_days` is S, and S is not measured from the monolith.

    There is no canonical S in the code to derive it from: the design carries `S = 90 / C = 30`
    as prose, and the generated blockers table says it is "provisional only; nothing has
    selected it". So it is one named constant, read by the builder, and bound by this test —
    rather than a literal `90` sitting in a manifest builder where it reads like a measurement."""

    def setUp(self):
        super().setUp()
        self.mroot = os.path.join(self.tmp, "built_blocks")
        os.makedirs(self.mroot)

    def build(self):
        return lb.build_generation_one(monolith_path=self.monolith, manifest_root=self.mroot,
                                       at_utc="t", operator="tests")

    def test_the_generated_manifest_uses_the_declared_policy(self):
        self.build()
        live = bm.load_live(self.mroot)
        self.assertEqual(live["block_grid"]["block_days"], lb.BLOCK_DAYS_S)

    def test_changing_the_policy_changes_the_manifest(self):
        """The binding, demonstrated: a literal would not move."""
        self.addCleanup(setattr, lb, "BLOCK_DAYS_S", lb.BLOCK_DAYS_S)
        lb.BLOCK_DAYS_S = 45
        self.build()
        self.assertEqual(bm.load_live(self.mroot)["block_grid"]["block_days"], 45)

    def test_it_is_recorded_as_policy_and_as_provisional(self):
        out = self.build()
        self.assertIs(out["block_days_is_policy"], True)
        self.assertIs(out["block_days_is_provisional"], True)
        self.assertIs(lb.BLOCK_DAYS_S_IS_PROVISIONAL, True)

    def test_the_anchor_is_the_day_after_the_audited_end_day(self):
        """Design §12 M0: the calendar grid starts where the legacy base stops, so the first
        future block abuts it instead of overlapping it."""
        self.build()
        live = bm.load_live(self.mroot)
        self.assertEqual(live["block_grid"]["anchor_day"], bm.add_days(self.days[-1], 1))
        self.assertNotEqual(live["block_grid"]["anchor_day"], self.days[0])

    def test_gaps_are_derived_from_the_calendar_span(self):
        """Design §12 M0: `gaps = calendar[start..end] - present`. Writing `[]` would claim a
        contiguity the store may not have, and the day digest -- computed over the days that
        ARE there -- matches either way, so nothing downstream would catch it."""
        out = self.build()
        insp = bm.inspect_store_contract(self.monolith)
        expected = sorted(set(bm.calendar_span(insp.days[0], insp.days[-1])) - set(insp.days))
        self.assertEqual(out["gaps"], expected)

    def test_a_monolith_with_a_hole_reports_it(self):
        """Built here rather than found: a store whose day set skips a calendar day."""
        holed = os.path.join(self.G, "holed.zarr")
        days = fx.days_from("2026-03-01", 6)
        keep = days[:2] + days[3:]                       # days[2] is missing
        fx.build_block(holed, keep, ny=NY, nx=NX)
        seal(self, holed)
        root = os.path.join(self.tmp, "holed_blocks")
        os.makedirs(root)
        out = lb.build_generation_one(monolith_path=holed, manifest_root=root,
                                      at_utc="t", operator="tests")
        self.assertEqual(out["gaps"], [days[2]])
        self.assertIn(days[2], bm.load_live(root)["segments"][0]["gaps"])


class TestEndToEndIsolatedProvisionAndServe(unittest.TestCase):
    """The whole path in one place, with no component stubbed.

    The real production operation creates the roots, publishes generation 1 through the real
    builder and publisher, and initializes the real WAL; then the real API lifespan loads that
    manifest through `SegmentedCubeStore` under the real runtime allow-list, and the values a
    client receives are compared against a `TimeCubeStore` oracle over the monolith.

    Entirely inside a temporary directory. Nothing here is VM24, `$G`, or a service."""

    ENV = ("GHRSST_ZARR_PATH", "GHRSST_BLOCK_MANIFEST_DIR", "GHRSST_TIMECUBE_PATH",
           "GHRSST_DELTACUBE_PATH", "GHRSST_ALLOWED_LEGACY_PATHS")
    RUN_ID = "20260826T090000Z"
    FLOOR = 0.001

    def setUp(self):
        from store.zarr_paths import group_path
        import zarr
        from ops import p5_prod_provision as pp
        from ops import p5s8_evidence as ev
        self.pp = pp
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="p5-e2e-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.live = os.path.join(self.tmp, "G")
        self.anchor = os.path.join(self.tmp, "anchor_domain")
        os.makedirs(self.live)
        os.makedirs(self.anchor)
        rw.declare_anchor_domain(self.anchor, domain_id="p5-e2e-domain", at_utc="t",
                                 operator="tests", note="unit-test domain")
        self.monolith = os.path.join(self.live, "mur_timecube_s8_t90_sh128.zarr")
        self.days = fx.days_from("2026-01-01", 14)
        fx.build_block(self.monolith, self.days, ny=NY, nx=NX)
        seal(self, self.monolith)
        self.addCleanup(setattr, ev, "MINIMUM_FREE_GIB", ev.MINIMUM_FREE_GIB)
        ev.MINIMUM_FREE_GIB = self.FLOOR
        # A daily store so StoreAccess can boot.
        self.daily = os.path.join(self.tmp, "daily")
        g = zarr.open_group(group_path(self.daily, self.days[-1]), mode="w", zarr_format=3)
        g.create_array("lon", shape=(NX,), dtype="float32", chunks=(NX,))
        g.create_array("lat", shape=(NY,), dtype="float32", chunks=(NY,))
        g["lon"][:] = np.linspace(100.0, 100.0 + NX - 1, NX, dtype=np.float32)
        g["lat"][:] = np.linspace(0.0, NY - 1.0, NY, dtype=np.float32)
        for v in fx.VARS:
            g.create_array(v, shape=(1, NY, NX), dtype="float32", chunks=(1, 8, 8))
            g[v][0] = np.full((NY, NX), 42.0, dtype=np.float32)
        self.provisioned = self._provision()

    def _provision(self):
        """The REAL production operation, with only the anchor's device substituted."""
        from tests.test_phase2_p5_prod_provision import dev_map
        locks = self.pp.canonical_lock_paths(self.live)
        approval = {"scope": self.pp.PRODUCTION_SCOPE, "approved_by": "cywhale",
                    "approved_utc": "t", "live_root": self.live, "run_id": self.RUN_ID}
        with dev_map({self.anchor: 4242}):
            return self.pp.provision_production(
                live_root=self.live, anchor_root=self.anchor, approval=approval,
                monolith_path=self.monolith,
                compaction_lock_path=locks["compaction"], ingest_lock_path=locks["ingest"],
                run_id=self.RUN_ID, operator="tests", at_utc="2026-08-26T09:00:00Z",
                min_free_gib=self.FLOOR)

    def boot(self, **env):
        from fastapi.testclient import TestClient
        from api.app import app
        saved = {k: os.environ.get(k) for k in self.ENV}
        for k in self.ENV:
            os.environ.pop(k, None)
        os.environ["GHRSST_ZARR_PATH"] = self.daily
        for k, v in env.items():
            os.environ[k] = v
        cm = TestClient(app)
        client = cm.__enter__()

        def restore():
            cm.__exit__(None, None, None)
            for k, v in saved.items():
                os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
        self.addCleanup(restore)
        return client, app.state.router.cube

    def serving(self):
        return self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.provisioned["manifest_root"],
                         GHRSST_TIMECUBE_PATH=self.monolith,
                         GHRSST_ALLOWED_LEGACY_PATHS=self.monolith)

    # ---------------------------------------------------------------- what was provisioned
    def test_the_operation_published_a_generation_and_bound_a_wal(self):
        self.assertIs(self.provisioned["manifest_published"], True)
        self.assertIs(self.provisioned["wal_initialized"], True)
        self.assertEqual(self.provisioned["generation"], 1)

    # ---------------------------------------------------------------- parity with the oracle
    def test_every_day_and_variable_matches_the_oracle_through_the_store(self):
        store = SegmentedCubeStore(self.provisioned["manifest_root"],
                                   allowed_legacy_paths=[self.monolith])
        variables = sorted(bm.inspect_store_contract(self.monolith).vars)
        want = TimeCubeStore(self.monolith).point_series(LON, LAT, self.days, variables)
        got = store.point_series(LON, LAT, self.days, variables)
        self.assertEqual(len(got), len(want))
        for a, b in zip(want, got):
            with self.subTest(a.get("date")):
                self.assertEqual(a, b)

    def test_the_api_serves_the_oracles_values_including_boundaries(self):
        client, cube = self.serving()
        self.assertIsInstance(cube, SegmentedCubeStore)
        oracle = TimeCubeStore(self.monolith)
        for day in (self.days[0], self.days[len(self.days) // 2], self.days[-1]):
            with self.subTest(day):
                r = client.get(f"/api/ghrsst?lon0={LON}&lat0={LAT}"
                               f"&start={day}&end={day}&append=sst")
                self.assertEqual(r.status_code, 200, r.text)
                self.assertAlmostEqual(r.json()[0]["sst"],
                                       oracle.point_series(LON, LAT, [day], ["sst"])[0]["sst"],
                                       places=5)

    def test_nan_semantics_survive_the_whole_path(self):
        """A monolith holding a NaN day BEFORE it is provisioned over.

        The NaN cannot be introduced after provisioning and re-provisioned over: the content
        seal refuses the changed store, and replacing the WAL root under the same anchor is
        refused again as a rollback. Both refusals are correct, so the fixture puts the NaN in
        first -- which is also the real case, since a monolith is not edited in place."""
        import zarr
        from tests.test_phase2_p5_prod_provision import dev_map
        idx = 5
        day = self.days[idx]
        live2 = os.path.join(self.tmp, "G2")
        anchor2 = os.path.join(self.tmp, "anchor2")
        os.makedirs(live2)
        os.makedirs(anchor2)
        rw.declare_anchor_domain(anchor2, domain_id="p5-e2e-domain-2", at_utc="t",
                                 operator="tests")
        mono2 = os.path.join(live2, "mur_timecube_s8_t90_sh128.zarr")
        fx.build_block(mono2, self.days, ny=NY, nx=NX)
        g = zarr.open_group(mono2, mode="a")
        arr = np.asarray(g["sst"][idx])
        arr[:] = np.nan
        g["sst"][idx] = arr
        seal(self, mono2)                     # sealed BEFORE the manifest describes it
        locks = self.pp.canonical_lock_paths(live2)
        run = "20260826T091500Z"
        with dev_map({anchor2: 4343}):
            out = self.pp.provision_production(
                live_root=live2, anchor_root=anchor2,
                approval={"scope": self.pp.PRODUCTION_SCOPE, "approved_by": "cywhale",
                          "approved_utc": "t", "live_root": live2, "run_id": run},
                monolith_path=mono2, compaction_lock_path=locks["compaction"],
                ingest_lock_path=locks["ingest"], run_id=run, operator="tests",
                at_utc="t", min_free_gib=self.FLOOR)
        store = SegmentedCubeStore(out["manifest_root"], allowed_legacy_paths=[mono2])
        want = TimeCubeStore(mono2).point_series(LON, LAT, [day], ["sst"])[0]
        got = store.point_series(LON, LAT, [day], ["sst"])[0]
        a, b = want.get("sst"), got.get("sst")
        self.assertEqual(a is None, b is None)
        if isinstance(a, float) and isinstance(b, float):
            self.assertEqual(math.isnan(a), math.isnan(b))

    def test_replacing_the_wal_root_under_the_same_anchor_is_refused(self):
        """The anchor doing its job, found while writing the test above.

        Removing the P5 roots and provisioning again against the SAME anchor is a rollback of
        the WAL root, and the anchor's durable high-water mark is what notices: the new log's
        head is 0 while the anchor names seq 1. Recovering from that is an operator decision,
        not something a provisioner should paper over."""
        shutil.rmtree(os.path.join(self.live, self.pp.MANIFEST_DIRNAME))
        shutil.rmtree(os.path.join(self.live, self.pp.WAL_DIRNAME))
        os.remove(os.path.join(self.live, self.pp.marker_name(self.RUN_ID)))
        with self.assertRaises(rw.WalRolledBack) as caught:
            self._provision()
        self.assertIn("anchor", str(caught.exception).lower())

    # ---------------------------------------------------------------- fail closed
    def test_absent_allow_list_is_degraded_and_never_falls_back(self):
        client, cube = self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.provisioned["manifest_root"],
                                 GHRSST_TIMECUBE_PATH=self.monolith)
        h = client.get("/healthz").json()
        self.assertIsNone(cube)
        self.assertEqual(h["status"], "degraded")
        self.assertIsNone(h["cube_base_kind"], "the monolith was served as a fallback")

    def test_a_wrong_allow_list_path_is_degraded(self):
        other = os.path.join(self.live, "some_other_store.zarr")
        os.makedirs(other)
        client, cube = self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.provisioned["manifest_root"],
                                 GHRSST_TIMECUBE_PATH=self.monolith,
                                 GHRSST_ALLOWED_LEGACY_PATHS=other)
        h = client.get("/healthz").json()
        self.assertIsNone(cube)
        self.assertEqual(h["status"], "degraded")
        self.assertIn("not the deployed monolith", h["cube_error"])

    def test_a_monolith_changed_after_provisioning_is_degraded_not_served(self):
        """The seal, end to end: the values on disk no longer match what the manifest
        described, and the service refuses rather than serving them."""
        import zarr
        unseal(self.monolith)
        g = zarr.open_group(self.monolith, mode="a")
        g["sst"][2] = np.asarray(g["sst"][2]) + 50.0
        client, cube = self.serving()
        h = client.get("/healthz").json()
        self.assertIsNone(cube)
        self.assertEqual(h["status"], "degraded")
        self.assertIn("content_seal mismatch", h["cube_error"])
        self.assertIsNone(h["cube_base_kind"])

    def test_a_missing_monolith_after_provisioning_is_degraded(self):
        unseal(self.monolith)
        shutil.rmtree(self.monolith)
        client, cube = self.boot(GHRSST_BLOCK_MANIFEST_DIR=self.provisioned["manifest_root"],
                                 GHRSST_TIMECUBE_PATH=self.monolith,
                                 GHRSST_ALLOWED_LEGACY_PATHS=self.monolith)
        h = client.get("/healthz").json()
        self.assertIsNone(cube)
        self.assertEqual(h["status"], "degraded")


class TestTheGeneratedManifestSaysBlockDaysIsPolicy(_LegacyBase):
    """The artifact itself must not present a policy number as a measurement."""

    def setUp(self):
        super().setUp()
        self.mroot = os.path.join(self.tmp, "policy_blocks")
        os.makedirs(self.mroot)

    def test_the_provenance_line_names_it_as_policy_and_provisional(self):
        lb.build_generation_one(monolith_path=self.monolith, manifest_root=self.mroot,
                                at_utc="t", operator="tests")
        created_by = bm.load_live(self.mroot)["created_by"]
        self.assertIn(f"block_days={lb.BLOCK_DAYS_S} is POLICY", created_by)
        self.assertIn("not derived from the store", created_by)
        self.assertIn("PROVISIONAL", created_by)

    def test_the_provenance_follows_the_policy_constant(self):
        self.addCleanup(setattr, lb, "BLOCK_DAYS_S", lb.BLOCK_DAYS_S)
        lb.BLOCK_DAYS_S = 30
        lb.build_generation_one(monolith_path=self.monolith, manifest_root=self.mroot,
                                at_utc="t", operator="tests")
        live = bm.load_live(self.mroot)
        self.assertEqual(live["block_grid"]["block_days"], 30)
        self.assertIn("block_days=30 is POLICY", live["created_by"])

    def test_the_document_does_not_claim_S_is_selected(self):
        with open(os.path.join(_DEV, "specs", "p5_deployment_readiness.md")) as fh:
            flat = " ".join(fh.read().split())
        self.assertIn("S = 90 / C = 30 is not selected", flat)
        self.assertIn("explicit operator decision recorded in the roadmap, and there is none",
                      flat)
