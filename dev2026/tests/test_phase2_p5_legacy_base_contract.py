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
