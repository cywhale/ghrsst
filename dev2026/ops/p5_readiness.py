"""dev2026 — may a first P5 compaction run against THIS deployment?

Everything built so far answers a different question. `ops/p5s8_evidence.py` measured the
legacy monolith on VM24; the P5 store layer is proven against `tempfile` trees. Neither says
whether the target has a manifest, a WAL, an anchor or a lock — and VM24, today, has none of
them: it runs the phase-2 tiered deployment, with no `p5_blocks`, no `p5_repairs`, no anchor
and no compaction lock.

So this module adjudicates a deployment, not a document. Every checker looks at the target that
was handed to it and reports what it found.

## Why the verdicts are shaped this way

`READY` / `BLOCKED` / `NOT_ADJUDICATED` / `NEEDS_HUMAN_APPROVAL` — and deliberately no `PASS`.
The failure this phase kept correcting is a measurement of one thing quoted as a verdict on
another: an S8 legacy baseline read as a P5 result, a synthetic fixture read as production
evidence, a figure from two runs ago read as current. `NON_QUALIFYING_LABELS` closes that at the
top: evidence carrying one of those labels cannot produce `READY` for any requirement, whatever
the checker found, and the reason names the label that stopped it.

`NEEDS_HUMAN_APPROVAL` is neither a pass nor a failure. It marks the things no amount of checking
can settle — switching the live service being the obvious one. It still blocks readiness.

## What this module is NOT

It builds nothing, changes nothing and asks for nothing. It re-implements no manifest, WAL or
provenance logic either: every structural check calls the shipped module the store itself calls,
so a manifest this accepts is one `SegmentedCubeStore` would accept.
"""
from __future__ import annotations

import os
from typing import Dict, List

from store import block_manifest, repair_wal

READY = "READY"
BLOCKED = "BLOCKED"
NOT_ADJUDICATED = "NOT_ADJUDICATED"
NEEDS_APPROVAL = "NEEDS_HUMAN_APPROVAL"

VERDICTS = (READY, BLOCKED, NOT_ADJUDICATED, NEEDS_APPROVAL)

#: Evidence labels that may never produce READY for a P5 compaction requirement. Each names a
#: real thing that was measured -- and a different thing from the one being adjudicated here.
NON_QUALIFYING_LABELS = (
    "LOCAL_SYNTHETIC",              # a tempfile tree is not a deployment
    "MODEL",                        # arithmetic is not a measurement
    "PRODUCTION_GEOMETRY_FIXTURE",  # the right shape, still a fixture
    "LEGACY_BASELINE",              # the monolith S8 measured has no P5 roots at all
    "DEFERRED",                     # explicitly not measured
    #: An isolated staging root built by `ops/p5_provision.py`. Structurally complete, and a
    #: deliberate non-deployment: proving the provisioning recipe works is a different claim
    #: from a deployment being ready, and the label is what keeps the two apart.
    "P5_ISOLATED_STAGING",
)

#: The label a target must carry for its checkers to be believed: evidence read from the real
#: deployment, read-only.
QUALIFYING_LABEL = "VM24_PRODUCTION_READ_ONLY"

#: Commits that have completed review and may be deployed. Declared HERE, where a reviewed
#: change is needed to alter it -- never taken from the target.
#:
#: `reviewed_sha == head_sha` compared the target against itself: both fields come from one
#: collected JSON, so anything that could write that file could declare its own commit
#: reviewed. An approved set is only worth anything if the thing being approved cannot edit it.
REVIEWED_DEPLOY_SHAS = (
    # RETAINED, not retired. Three reasons, recorded because removing it would have changed
    # what this tuple MEANS without saying so:
    #
    #  1. Review is a historical fact. `3223fba` completed review; dropping it would assert
    #     something false about the past.
    #  2. This tuple is not only "may be deployed". `_check_failure_semantics` reads it twice
    #     more -- for which commits an executed result may be READ FROM, and which deploy
    #     commit a recorded run may be ABOUT. Removing `3223fba` would retroactively invalidate
    #     every result ever recorded against it.
    #  3. Retention is not a loophole. `3223fba` predates `ops/p5_provision.py`, so
    #     `deployment_tools` BLOCKS it and it cannot reach `compaction_ready` however long it
    #     stays here.
    "3223fba37da5d292ea6acb58ccef0da2b5138d31",
    # The deployment candidate. Its 23 runtime modules are byte-identical to `3223fba` -- it
    # adds `ops/p5_provision.py`, the assessor, tests, docs and evidence artifacts, and
    # modifies nothing.
    "708058e15b37a02e1d7aa5a66b9978c6e13bd446",
)

#: Which of the approved commits a NEW deployment should be made from.
#:
#: Separate from `REVIEWED_DEPLOY_SHAS` because the two answer different questions. "Was this
#: reviewed" is historical and does not expire; "which should be deployed now" is a current
#: recommendation and moves. Collapsing them into one tuple is what made retaining `3223fba`
#: look like endorsing it.
#:
#: This is a recommendation recorded in reviewed code. It authorizes no deployment by itself:
#: deploying is a production change and needs its own approval.
CURRENT_DEPLOY_SHA = "708058e15b37a02e1d7aa5a66b9978c6e13bd446"

#: Revisions PROPOSED for deployment review, and approved by nothing. Kept apart from
#: `REVIEWED_DEPLOY_SHAS` on purpose: `_check_identity` reads only that tuple, so a revision
#: named here can be measured, argued about and rejected without ever being deployable. Adding
#: one of these to the approved set is a review decision and a separate commit.
#:
#: Empty: `708058e` was reviewed and moved into `REVIEWED_DEPLOY_SHAS` above. Nothing is
#: currently proposed. The mechanism stays because the next candidate will need it, and a test
#: keeps this set disjoint from the approved one -- an overlap would mean a proposal had been
#: promoted without the commit that promotes it.
PROPOSED_DEPLOY_SHAS = ()

#: What the deployment must CARRY but no runtime stage imports.
#:
#: What the deployment must CARRY to provision the P5 roots IN PRODUCTION.
#:
#: `ops/p5_prod_provision.py`, not `ops/p5_provision.py`. The latter is an isolated-staging
#: tool and refuses any stage root inside the declared live root, so a deployment carrying only
#: it cannot provision anything in `$G` -- and the gate was reading its presence as though it
#: could. It also declares its anchor with `allow_same_filesystem=True`, which is correct for a
#: throwaway root and invalid for a deployment. Carrying a tool that refuses the job is not
#: carrying the tool for the job.
#:
#: An operator runs these as commands; a compaction never imports them, so they are
#: deliberately NOT in `INVENTORY_MODULES` -- `deployment_inventory_problem()` refuses a
#: declared module nothing runs, and it is right to. "Present at the deployment commit" is a
#: different claim from "on the runtime path", checked as a different one.
DEPLOYMENT_TOOLS = ("ops/p5_prod_provision.py",)

#: The isolated-staging tool, named separately and NOT gated on. It proves the provisioning
#: recipe in a throwaway root; it is useful, it is not a production capability, and conflating
#: the two is what this split exists to prevent. Nothing requires a deployment to carry it.
STAGING_TOOLS = ("ops/p5_provision.py",)

#: Where the assessor's repository lives, and the directory prefix inside it. Module-level so
#: tests can point the whole git-backed path at a purpose-built repository instead of stubbing
#: the checks out.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEV_PREFIX = os.path.basename(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

#: Where the ASSESSOR's own approval is checked. Separate from `REPO_ROOT` because the two
#: answer different questions -- "is the deployment's code approved" and "is the code asking
#: that question approved" -- and a test exercising one must not have to fake the other.
#: `None` means the same repository.
ASSESSOR_REPO_ROOT = None

#: The ONE committed directory an executed-result artifact may live in, relative to the dev
#: directory. A path is not a provenance: `/tmp/x.json` and `../../x.json` are paths too.
EXECUTIONS_REL = os.path.join("bench", "results", "p5_failure_semantics")

#: Legacy monoliths a `legacy_base` segment may point at, OUTSIDE the block root.
#:
#: This came from the target, so a manifest could ship the permission that lets its own segment
#: escape -- an allow-list taken from the thing it governs is not an allow-list. It is assessor
#: configuration now, and empty by default: fail closed, because the deployments this runs
#: against have no legacy segment at all.
ALLOWED_LEGACY_PATHS = ()

#: The DEPLOYMENT-target inventory: what a host must carry to RUN a compaction.
#:
#: The assessor's own modules used to be on this list, on the reasoning that a deployment
#: matching review while the tool judging it ran from elsewhere is only half checked. That
#: reasoning was right and the remedy was wrong: the assessor's integrity is `ASSESSOR_MODULES`
#: plus an external approval, and mixing the two produced a module the deployment never ships.
#: `ops/p5_readiness.py` was declared for the deployment and does not exist at the approved
#: deployment commit at all -- it was added eleven commits later -- so the comparison silently
#: shrank to the seven modules that happened to be there.
#:
#: Two inventories, two questions: "is the deployment running approved compaction code" and
#: "is the tool asking that question approved code". Neither answers the other.
#:
#: ## Measured, not written out
#:
#: This was six paths chosen by hand and defended as "the compaction code". It was not.
#: `ingest/build_block.py` and `ingest/publish_manifest.py` both import
#: `store/source_provenance.py`, which was absent from the list -- so identity could pass
#: without ever fingerprinting a first-party module the compaction path runs. And `api/app.py`
#: was listed while none of its dependencies were: `hybrid_router`, `store_access`,
#: `tiered_cube`, `time_cube`, `segmented_cube`, `bbox_encode`, `spatial_policy`,
#: `zarr_paths`. Naming an entry point is not declaring what it runs.
#:
#: So the deployment inventory is a measured closure, exactly as the assessor's own is:
#: `tests/test_phase2_p5_readiness.py` imports the entry points below in a fresh interpreter
#: and requires the declaration to EQUAL what loaded, in both directions.

#: The lifecycle, split by stage. One list per stage because they are separately reviewable
#: and separately reachable -- a deployment can ship a build path and be missing the prune/swap
#: half, and "the compaction modules" as one bag hid exactly that.
COMPACTION_BUILD_ENTRY_POINTS = ("ingest.build_block", "ingest.publish_manifest",
                                 "store.compaction_lock")
PRUNE_SWAP_ENTRY_POINTS = ("ingest.prune_delta", "ingest.swap_delta", "ingest.corrected_day")

#: Kept for the older call sites; the build stage by another name.
COMPACTION_ENTRY_POINTS = COMPACTION_BUILD_ENTRY_POINTS

#: The entry point whose parity `api_parity` is about.
API_ENTRY_POINTS = ("api.app",)

#: The first-party closure of the BUILD stage, measured -- by importing the entry points AND
#: by executing a controlled build with WAL and anchor enabled.
#:
#: Import-only probing is not enough here. `ingest/build_block.py` imports
#: `ingest.corrected_day` inside a function, in the WAL-attestation branch, so a deployment
#: could be missing that module entirely and an import of `build_block` would never notice.
#: The smoke below actually runs a build with `wal_root` set, which is what reaches it.
COMPACTION_BUILD_RUNTIME_MODULES = (
    "ingest/build_block.py", "ingest/corrected_day.py", "ingest/publish_manifest.py",
    "store/__init__.py", "store/block_manifest.py", "store/compaction_lock.py",
    "store/repair_wal.py", "store/segmented_cube.py", "store/source_provenance.py",
    "store/time_cube.py",
)

#: The first-party closure of the PRUNE/SWAP stage, measured the same way.
PRUNE_SWAP_RUNTIME_MODULES = (
    "ingest/build_block.py", "ingest/build_timecube.py", "ingest/corrected_day.py",
    "ingest/dual_write.py", "ingest/prune_delta.py", "ingest/publish_manifest.py",
    "ingest/swap_delta.py",
    "store/__init__.py", "store/block_manifest.py", "store/compaction_lock.py",
    "store/durable_jsonl.py", "store/repair_wal.py", "store/segmented_cube.py",
    "store/source_provenance.py", "store/time_cube.py", "store/zarr_paths.py",
)

#: The union of both compaction stages, for callers that want "the compaction runtime".
COMPACTION_RUNTIME_MODULES = tuple(sorted(set(COMPACTION_BUILD_RUNTIME_MODULES)
                                          | set(PRUNE_SWAP_RUNTIME_MODULES)))

#: The first-party closure of `API_ENTRY_POINTS`, measured. Kept separate from the compaction
#: runtime because they answer different questions -- what performs a compaction, and what
#: serves the API whose parity is being claimed -- and a deployment must carry both.
API_RUNTIME_MODULES = (
    "api/__init__.py", "api/app.py",
    "store/__init__.py", "store/bbox_encode.py", "store/block_manifest.py",
    "store/hybrid_router.py", "store/segmented_cube.py", "store/spatial_policy.py",
    "store/store_access.py", "store/tiered_cube.py", "store/time_cube.py",
    "store/zarr_paths.py",
)

#: What the deployment must carry: every lifecycle stage plus the API. `api_parity` is a
#: readiness requirement, so the API runtime is unioned in for identity while staying its own
#: declaration -- the two are reviewed separately and fail separately.
INVENTORY_MODULES = tuple(sorted(set(COMPACTION_BUILD_RUNTIME_MODULES)
                                 | set(PRUNE_SWAP_RUNTIME_MODULES)
                                 | set(API_RUNTIME_MODULES)))

#: Stage -> (entry points, declared closure, smoke). Defined once, below the smokes, so a
#: stage added later cannot be measured by one check and forgotten by another. Every reader
#: goes through this table rather than the tuples, which is also what lets a test substitute a
#: whole synthetic lifecycle instead of patching five names and hoping they stay consistent.
RUNTIME_GROUPS = {}         # populated at the end of this section, once the smokes exist

#: The ASSESSOR runtime inventory -- its own modules, plus every module the G18 threshold is
#: derived through. Distinct from `INVENTORY_MODULES` above, which is the DEPLOYMENT's.
#:
#: `canonical_minimum_free_gib()` imports `ops/p5s8_evidence.py` from the working tree, so one
#: edited line of local policy moved the bar the assessor then enforced -- the tool checking
#: whether the deployment ran approved code was not itself approved code. The threshold reaches
#: here through the roadmap and the report module, so a policy dependency left off this list is
#: a place to change the bar unobserved.
#: MEASURED, not remembered: every first-party module the readiness path actually imports.
#: `tests/test_phase2_p5_readiness.py` recomputes the closure and fails if anything executes
#: that is not listed here, so an import added later cannot quietly leave the runtime
#: unguarded -- which is exactly what happened to `store/segmented_cube.py`, the module
#: `_check_blocks` takes its verdict from.
ASSESSOR_MODULES = (
    "ops/p5_readiness.py", "ops/p5_oneshot_exception.py", "ops/p5s8_evidence.py",
    "ops/p5_roadmap.py", "ops/p5s8_report.py", "ops/quiescence.py",
    "store/__init__.py", "store/block_manifest.py", "store/durable_jsonl.py",
    "store/repair_wal.py", "store/segmented_cube.py", "store/time_cube.py",
)

#: Approval for the assessor comes from OUTSIDE the assessor.
#:
#: An approved-SHA set used to live in this module -- which is in `ASSESSOR_MODULES`, so the
#: code being approved carried its own approval. That is a circle with no way in: a commit's id
#: is a hash of its content, so no commit can list itself, and a later commit Y listing X is
#: refused at run time for `HEAD != X`. Both doors locked, so the guard could only ever say no.
#:
#: The operator holds a record instead, on a path outside the repository, naming the exact
#: commit they approved. `None` is the shipped default and fails closed: no record, no
#: approval. Nothing here names a SHA, because approving one is not this code's to do.
APPROVAL_RECORD_DEFAULT = None
APPROVAL_RECORD_PATH = os.environ.get("P5_ASSESSOR_APPROVAL") or APPROVAL_RECORD_DEFAULT

#: What a record must say it is for. A record approving something else is not this approval.
APPROVAL_SCOPE = "p5_readiness_assessor"

#: The suites that may stand as failure-semantics evidence, named in approved code.
#:
#: `suite` was any name whose run was green. A trivial suite asserting `True` exits 0 with a
#: positive test count, so the evidence said "some tests ran and did not fail" -- about nothing
#: in particular. These are the ones whose subject IS the refusal, rollback and partial-staging
#: behaviour being claimed.
FAILURE_SEMANTICS_SUITES = (
    "tests.test_phase2_p5_provision",
    "tests.test_phase2_p5s8_evidence",
)

#: How long a free-space reading may be believed. Free space is true of a moment: an ingest
#: window, a restart or an overnight pause can all move it, and none of them announce
#: themselves in the artifact. Short enough that a schedule tick invalidates the reading.
MAX_CAPACITY_AGE_S = 2 * 3600

#: How far a capacity reading may sit from the collection that carries it. They are supposed to
#: be the same act; a large gap means one of them was reused.
MAX_CAPACITY_SKEW_S = 300

#: Clocks are not synchronised to the second. A reading a little ahead of the assessor is
#: skew; one materially ahead cannot be aged at all, and an unaged reading never expires.
MAX_CLOCK_LEAD_S = 60

#: How far a lock acquisition probe may sit from the collection that carries it. Same reason as
#: the capacity skew: a probe from another sitting is evidence about another moment.
MAX_PROBE_SKEW_S = 900

_SHA256 = None
_SHA1 = None


def _is_sha1(value) -> bool:
    """Exactly 40 lowercase hex characters.

    Fail closed on shape BEFORE anything is sliced or handed to git: an int has no `[:7]`, a
    bool is an int, `"main"` is a valid ref and a terrible identity, and `""` resolves to
    nothing anywhere. Each of those used to reach further in than it should."""
    global _SHA1
    if _SHA1 is None:
        import re
        _SHA1 = re.compile(r"^[0-9a-f]{40}$")
    return type(value) is str and bool(_SHA1.match(value))


#: Absent, as distinct from present-and-wrong. `dict.get()` collapses the two into `None`, and
#: for evidence they are opposite findings: a key nobody wrote is a collector that never looked,
#: a key holding rubbish is a collector that ran and produced something unusable.
_MISSING = object()


def _is_sha256(value) -> bool:
    """Exactly 64 lowercase hex characters. Shape is not identity, but the wrong shape is not
    a digest at all, and 'non-empty' was the only test it previously had to pass."""
    global _SHA256
    if _SHA256 is None:
        import re
        _SHA256 = re.compile(r"^[0-9a-f]{64}$")
    return isinstance(value, str) and bool(_SHA256.match(value))

REQUIREMENTS: List[Dict] = [
    {"id": "identity", "evidence_kind": "preflight",
     "title": "the reviewed commit, checked out clean and detached",
     "why": ("a compaction is run BY code. If the deployed tree is not exactly the reviewed "
             "commit, the review adjudicated something else.")},
    {"id": "monolith_barrier", "evidence_kind": "measurement",
     "title": "the legacy monolith on a genuinely read-only mount, beside writable P5 paths",
     "why": ("a `legacy_base` generation 1 makes the monolith the served authority, and "
             "`content_seal` only DETECTS that it changed -- after the fact, and blind to a "
             "write that restores size and mtime. The barrier that stops it changing is "
             "operational: a read-only mount. No code can arrange that, so this adjudicates "
             "collected evidence of it, and the P5 roots, WAL and delta must stay writable on "
             "a different mount or the deployment cannot ingest at all.")},
    {"id": "deployment_tools", "evidence_kind": "preflight",
     "title": "the operator tools the deployment must carry, at the commit it reports",
     "why": ("provisioning the P5 roots is a prerequisite for every store requirement below, "
             "and it is run FROM the deployment. A deployment missing `ops/p5_provision.py` "
             "cannot create the manifest, WAL and anchor the rest of this list asks for -- so "
             "it is adjudicated here rather than left as a note somebody has to remember.")},
    {"id": "manifest", "evidence_kind": "store",
     "title": "a live segmented manifest the store would accept",
     "why": ("the manifest is the authority for what is served. Without one there is no "
             "segmented base to compact into, and `SegmentedCubeStore` has nothing to open.")},
    {"id": "blocks", "evidence_kind": "store",
     "title": "every referenced block present and passing the strict contract",
     "why": ("a manifest referencing a block that is absent or malformed publishes a view "
             "nobody can read; the failure surfaces at request time, on the live service.")},
    {"id": "wal_anchor", "evidence_kind": "store",
     "title": ("an initialized P5 WAL, an anchor outside the domain, and a declared "
               "rollback domain"),
     "why": ("the WAL is how a half-finished compaction is recognised as half-finished. An "
             "anchor inside the domain it witnesses is lost with it, and an undeclared domain "
             "means no one has said what a rollback would restore.")},
    {"id": "locks", "evidence_kind": "store",
     "title": "canonical compaction and ingest lock paths, in the declared order",
     "why": ("compaction and ingest both mutate the served view. Two writers with no shared "
             "lock path are not excluded from each other at all.")},
    {"id": "api_parity", "evidence_kind": "service",
     "title": "the API serves the segmented base with values matching the current base",
     "why": ("routing and value parity can only be shown by a service actually serving the "
             "manifest -- which means switching the live deployment, or standing up an "
             "isolated second one.")},
    {"id": "capacity", "evidence_kind": "measurement",
     "title": "free space above the minimum, from a fresh measurement",
     "why": ("a block build needs the space before it starts. A figure with no run behind it "
             "is a number in a file.")},
    {"id": "failure_semantics", "evidence_kind": "proven_locally",
     "title": "fail-closed refusal, rollback and partial-staging behaviour",
     "why": ("the first compaction is the one most likely to be interrupted. What happens then "
             "has to be a property of the code, not a hope.")},
]

#: Requirements no inspection can settle, with the reason. They are reported as needing a
#: person rather than quietly failing -- and they still block readiness.
APPROVAL_REQUIRED = {
    "api_parity": ("proving this needs a service serving the manifest. On VM24 that is either a "
                   "live switchover or a second isolated service -- both are production changes "
                   "and neither is authorized by a read-only preflight."),
}


def _reason(**kw) -> str:
    return "; ".join(f"{k}={v}" for k, v in kw.items())


def _dev_dir() -> str:
    return os.path.join(REPO_ROOT, DEV_PREFIX)


def expected_module_digests(sha: str) -> Dict[str, str]:
    """sha256 of each inventory module AT `sha`, read from git by the assessor.

    The target reports what it found on the host. This is what the approved commit actually
    contains. Comparing the two is the only version of the check that a target cannot satisfy
    by describing itself."""
    import hashlib
    out = {}
    for rel in INVENTORY_MODULES:
        blob = _git_blob(sha, rel)
        if blob is not None:
            out[rel] = hashlib.sha256(blob).hexdigest()
    return out


#: What the smokes import to FABRICATE their synthetic inputs, and which the deployment
#: therefore does not have to ship. Named one path at a time rather than excluding `tests/`
#: wholesale: the justification is "this builds the fixture", not "it lives under tests", and
#: the broad rule would silently drop a real dependency that happened to sit there.
SMOKE_FABRICATORS = ("tests/p5_fixtures.py",)

#: The shared setup every compaction smoke needs: a synthetic delta, a published generation
#: and an initialized WAL carrying one committed repair. Entirely inside a throwaway temp
#: directory of the target tree's own making -- no store, no VM24 path, no environment root.
_SMOKE_SETUP = """
import os, tempfile
import numpy as np
import zarr
import p5_fixtures as fx
from ingest.build_block import build_block
from store import block_manifest as bm
from store import repair_wal as rw

ANCHOR, S = "2026-06-27", 90
tmp = tempfile.mkdtemp(prefix="p5-smoke-")
root = os.path.join(tmp, "manifest_root"); wal = os.path.join(tmp, "wal")
arts = os.path.join(tmp, "artifacts"); hold = os.path.join(tmp, "hold")
for d in (root, wal, arts, hold):
    os.makedirs(d, exist_ok=True)
clock = os.path.join(tmp, "p5_compaction.lock")
# The anchor shares a filesystem with the WAL under `mkdtemp`, so the waiver is named here
# rather than smuggled: this smoke measures which MODULES run, not whether a deployment's
# rollback domain is real. G14 adjudicates that separately, from evidence.
AK = {"unsafe_allow_colocated_anchor": True}

s0, e0 = bm.block_bounds(ANCHOR, S, 0)
days = fx.calendar_span(s0, e0)[:6]
repaired, folded = days[0], days[2]
keep_days = [d for d in days if d not in (repaired, folded)]

delta_v1 = os.path.join(tmp, "delta_v1.zarr")
fx.build_delta(delta_v1, days)
live = os.path.join(tmp, "delta_live.zarr")
os.symlink(delta_v1, live)                       # the live delta is a symlink, as on the host


def publish(segment, generation):
    m = {"format": bm.MANIFEST_FORMAT, "version": bm.SCHEMA_VERSION,
         "generation": generation, "generation_id": f"gen{generation:06d}-x",
         "created_utc": "2027-01-01T00:00:00Z", "created_by": "p5-readiness-smoke",
         "predecessor_generation": generation - 1 if generation > 1 else None,
         "predecessor_manifest": (bm.archive_name(generation - 1) if generation > 1 else None),
         "grid": {"ny": 32, "nx": 32, "region": [0, 32, 0, 32]},
         "variables": list(fx.VARS), "block_grid": {"anchor_day": ANCHOR, "block_days": S},
         "segments": [dict(segment)], "superseded": [], "manifest_checksum": ""}
    m["manifest_checksum"] = bm.compute_checksum(m)
    bm.publish(root, m)


def inline_fingerprint(path, idx):
    \"\"\"The repaired bytes, hashed HERE. The build smoke must not import
    `ingest.corrected_day` to compute this: that module is exactly what the smoke exists to
    observe `build_block` reaching on its own, and importing it would answer the question.\"\"\"
    import hashlib
    h = hashlib.sha256()
    g = zarr.open_group(path, mode="r")
    for v in fx.VARS:
        h.update(np.ascontiguousarray(g[v][idx]).tobytes())
    return h.hexdigest()


v1 = build_block(os.path.join(root, "b_v1.zarr"), start_day=s0, end_day=e0,
                 classification_target=[d for d in days if d != folded],
                 delta_path=live, artifacts_dir=arts, lock=None,
                 hard_reserve_bytes=0, unsafe_skip_isolation=True)
v1_id = v1["segment"]["segment_id"]
publish(v1["segment"], 1)
rw.initialize_wal(wal, manifest_root=root, at_utc="t", operator="ops", **AK)

# A real repair: the day is corrected in delta and the correction committed to the WAL.
rec = rw.open_repair(wal, day=repaired, at_utc="t", operator="ops",
                     payload={"expected_vars": list(fx.VARS)}, **AK)
idx = list(bm.inspect_store_contract(live).days).index(repaired)
g = zarr.open_group(live, mode="a")
g["sst"][idx] = np.asarray(g["sst"][idx]) + 21.0
if stage == "prune_swap":
    from ingest import corrected_day as cd    # a declared entry point of THIS stage
    fp = cd.fingerprint_day(live, repaired, physical_index=idx)[0]
else:
    fp = inline_fingerprint(live, idx)
rw.append(wal, record=rw.COMMITTED, repair_id=rec["repair_id"], day=repaired,
          at_utc="t", operator="ops", payload={"fingerprint": fp}, **AK)
"""

#: The BUILD stage: a fold with the WAL enabled. `build_block` imports `ingest.corrected_day`
#: from inside that branch, so a deployment could ship without the module and an import of
#: `build_block` would never notice. This call is where it shows.
_SMOKE_BUILD = _SMOKE_SETUP + """
v2 = build_block(os.path.join(root, "b_v2.zarr"), start_day=s0, end_day=e0,
                 classification_target=list(days), delta_path=live, artifacts_dir=arts,
                 lock=None, hard_reserve_bytes=0, unsafe_skip_isolation=True,
                 wal_root=wal, **AK)
# Not merely "it ran": the days the fold ATTESTED. An empty list would mean the WAL branch
# was entered and did nothing, which is not the path a compaction takes.
result = {"attested": sorted(((v2.get("segment") or {}).get("build_provenance") or {})
                             .get("materialized_repairs") or ())}
"""

#: The PRUNE/SWAP stage: the §7.8 refold, the §7.5a eligibility gate, a real prune with its
#: validation, and a swap plan actually executed. The swap's corrected-day re-authorization
#: imports `ingest.corrected_day` from inside `execute_swap_plan` -- another function-local
#: import, on the path that decides whether a day may be dropped.
_SMOKE_PRUNE_SWAP = _SMOKE_SETUP + """
from ingest.prune_delta import prune_delta
from ingest.swap_delta import execute_swap_plan

refold = cd.corrective_refold(
    day=repaired, manifest_root=root, delta_path=live, wal_root=wal, **AK,
    new_block_path=os.path.join(root, "b_v2.zarr"), artifacts_dir=arts,
    start_day=s0, end_day=e0, classification_target=list(days),
    predecessor_segment_id=v1_id, ingest_lock_path=os.path.join(tmp, "ingest.lock"),
    compaction_lock_path=clock, hard_reserve_bytes=1 << 20, operator="ops")
elig = cd.prune_eligibility(sorted(set(days) - set(keep_days)), manifest_root=root,
                            delta_path=live, wal_root=wal, **AK)
plan = prune_delta(live, os.path.join(tmp, "staging.zarr"), list(keep_days), base_days=days,
                   spatial_window_days=1, wal_root=wal, manifest_root=root, **AK)
swap = execute_swap_plan(
    plan, mode="s1", hold_dir=hold, wal_root=wal, manifest_root=root,
    compaction_lock_path=clock, **AK,
    pre_swap_quiesce_fn=lambda **kw: {"drained": True, "observed_inflight": 0,
                                      "evidence": "smoke: a private tree, no readers",
                                      "attestation_id": "att-smoke", **kw})
result = {"refold": refold.get("status"), "authorized": sorted(elig.get("authorized") or ()),
          "swap": swap.get("status"), "swap_performed": swap.get("swap_performed"),
          "swap_reason": swap.get("reason")}
"""

# The API stage has no smoke: `api/app.py` wires its routers at import, so importing it
# observes the whole surface. Claiming a smoke there would be claiming evidence that does not
# exist -- and `api_parity` is adjudicated from measured responses, not from this.
RUNTIME_GROUPS.update({
    "build": (COMPACTION_BUILD_ENTRY_POINTS, COMPACTION_BUILD_RUNTIME_MODULES, _SMOKE_BUILD),
    "prune_swap": (PRUNE_SWAP_ENTRY_POINTS, PRUNE_SWAP_RUNTIME_MODULES, _SMOKE_PRUNE_SWAP),
    "api": (API_ENTRY_POINTS, API_RUNTIME_MODULES, ""),
})

#: The probe that measures a TARGET tree's first-party closure. Run in a fresh interpreter
#: with the materialized tree first on `sys.path`, so nothing the assessor imported can satisfy
#: an import the target should have provided.
#: ONE group per process. Clearing `sys.modules` between groups and re-importing fails on
#: anything backed by a C extension -- "cannot load module more than once per process" -- and
#: the groups share the `store` package, so a single process could measure only the first.
#: Entry points are imported first, then the stage's smoke RUNS. Import-only probing cannot
#: see a function-local import, and three of this lifecycle's dependencies are exactly that.
_TARGET_CLOSURE_PROBE = """
import json, os, sys
tree, stage = sys.argv[1], sys.argv[2]
sys.path.insert(0, tree)
# The fabricators live in `tests/`; they build the synthetic inputs and are not deployed. The
# closure below excludes them for that reason -- what is measured is what the LIFECYCLE runs.
sys.path.insert(0, os.path.join(tree, "tests"))
fabricators = set(json.loads(sys.argv[5]))
error = None
# Which entry point failed, not merely that one did: "publish_manifest raised RuntimeError" is
# actionable and "the compaction group failed" sends someone reading four modules.
for name in json.loads(sys.argv[3]):
    try:
        __import__(name)
    except Exception as exc:
        error = f"{name} does not import: {type(exc).__name__}: {exc}"
        break
executed = None
if error is None and sys.argv[4]:
    try:
        ns = {"__name__": "__p5_smoke__", "stage": stage}
        exec(compile(sys.argv[4], "<p5-smoke>", "exec"), ns)
        executed = ns.get("result")
    except BaseException as exc:
        error = f"the {stage} lifecycle does not run: {type(exc).__name__}: {exc}"
found = set()
for mod in list(sys.modules.values()):
    f = getattr(mod, "__file__", None)
    if not f:
        continue
    f = os.path.abspath(f)
    if f.startswith(tree + os.sep) and f.endswith(".py"):
        rel = os.path.relpath(f, tree)
        if rel not in fabricators:
            found.add(rel)
print(json.dumps({"closure": sorted(found), "error": error, "executed": executed},
                 default=str))
"""

_TARGET_CLOSURE_CACHE = {}


def materialize_target_tree(sha: str) -> str:
    """The deployment's own source AT `sha`, written to a throwaway directory.

    From git blobs, never from the working tree: the closure was previously measured against
    whatever the assessor happened to be sitting on, which says nothing about the commit being
    adjudicated. Caller removes the directory."""
    import subprocess
    import tempfile
    dest = tempfile.mkdtemp(prefix=f"p5-target-{sha[:7]}-")
    proc = subprocess.run(f"git archive {sha} {DEV_PREFIX} | tar -x -C {dest}",
                          shell=True, cwd=REPO_ROOT, capture_output=True)
    if proc.returncode != 0:
        import shutil
        shutil.rmtree(dest, ignore_errors=True)
        raise ExecutionRefused(
            f"could not materialize {sha[:7]}: {proc.stderr.decode(errors='replace')[:400]}")
    # `git archive` writes `<prefix>/...`; the importable root is that directory.
    return os.path.join(dest, DEV_PREFIX)


def target_runtime_closure(sha: str) -> Dict:
    """The first-party modules the TARGET commit's entry points import.

    Importing runs module-level code, and this does not pretend otherwise. What bounds it: the
    caller has already checked `sha` against `REVIEWED_DEPLOY_SHAS`, so only approved code is
    ever imported; it runs from a throwaway tree with its own `sys.path`; and the subprocess is
    fresh, so nothing the assessor loaded can stand in for a module the target should ship. That
    is isolation, not a sandbox -- the safety is that the commit was approved first."""
    import json
    import shutil
    import subprocess
    if sha in _TARGET_CLOSURE_CACHE:
        return _TARGET_CLOSURE_CACHE[sha]
    import sys as _sys
    tree = materialize_target_tree(sha)
    out, errors, executed = {}, {}, {}
    try:
        for group, (entries, _declared, smoke) in RUNTIME_GROUPS.items():
            proc = subprocess.run(
                [_sys.executable, "-c", _TARGET_CLOSURE_PROBE, tree, group,
                 json.dumps(list(entries)), smoke or "",
                 json.dumps(list(SMOKE_FABRICATORS))],
                capture_output=True, text=True, cwd=tree)
            if proc.returncode != 0 or not proc.stdout.strip():
                raise ExecutionRefused(
                    f"the {group} closure probe failed for {sha[:7]}: {proc.stderr[-400:]}")
            result = json.loads(proc.stdout)
            out[group] = result["closure"]
            if result["executed"] is not None:
                executed[group] = result["executed"]
            if result["error"]:
                errors[group] = result["error"]
    finally:
        shutil.rmtree(os.path.dirname(tree), ignore_errors=True)
    out["errors"] = errors
    out["executed"] = executed
    _TARGET_CLOSURE_CACHE[sha] = out
    return out


def target_closure_problem(sha: str):
    """Does the TARGET commit import exactly what the inventories declare? Or `None`."""
    try:
        observed = target_runtime_closure(sha)
    except Exception as exc:                                  # noqa: BLE001
        return f"its runtime closure could not be computed: {exc}"
    errors = observed.get("errors") or {}
    if errors:
        return (f"its lifecycle does not run at {sha[:7]}: {errors}; a deployment whose build, "
                f"prune/swap or API stage cannot be loaded and run is not a deployment")
    for group, (_entries, declared, _smoke) in RUNTIME_GROUPS.items():
        seen = set(observed.get(group) or ())
        undeclared = sorted(seen - set(declared))
        if undeclared:
            return (f"its {group} runtime imports {undeclared}, which the inventory does not "
                    f"declare; the closure is measured from the target, not from the "
                    f"assessor's own tree")
        absent = sorted(set(declared) - seen)
        if absent:
            return (f"its {group} runtime does not import {absent}, which the inventory "
                    f"declares; a declaration the commit does not satisfy is not a check")
    return None


def deployment_inventory_problem():
    """Is the declared deployment inventory the union of the two measured runtimes?

    Guards the declaration itself: shrinking `INVENTORY_MODULES` would otherwise make every
    inventory check pass on a subset, which is how a hand-written list came to omit
    `store/source_provenance.py` and every dependency of `api/app.py`."""
    declared = set(INVENTORY_MODULES)
    required = set().union(*(set(d) for _e, d, _s in RUNTIME_GROUPS.values()))
    undeclared = sorted(required - declared)
    if undeclared:
        return (f"runs {undeclared} and does not declare them; naming an entry point is not "
                f"declaring what it runs")
    extra = sorted(declared - required)
    if extra:
        return (f"declares {extra}, which neither runtime imports; a declaration nothing "
                f"executes is not a check")
    return None


def deployment_tools_problem(sha: str):
    """Tools the deployment must carry that are absent at `sha`, or `None`.

    Deliberately NOT wired into `_check_identity`, and so not part of any verdict: the reviewed
    deployment `3223fba` predates `ops/p5_provision.py` entirely, and folding its absence into
    `identity` would restate one blocker as another. Promoting this to a requirement is a
    review decision, not something this module should take on its own."""
    absent = sorted(rel for rel in DEPLOYMENT_TOOLS if _git_blob(sha, rel) is None)
    if absent:
        return (f"does not carry {absent}; provisioning the P5 roots is an operator command, "
                f"and a command absent from the deployment cannot be run on it")
    return None


def missing_inventory_modules(sha: str) -> List[str]:
    """Declared deployment modules that do not exist at `sha`.

    `expected_module_digests()` returns only what it FOUND, so a declared module absent at the
    approved commit vanished from the comparison instead of failing it -- the check quietly
    shrinking to fit whatever the commit happened to contain. A declaration that a commit
    cannot satisfy is a defect in one of the two, and either way it is not a pass."""
    return sorted(rel for rel in INVENTORY_MODULES if _git_blob(sha, rel) is None)


class ExecutionRefused(Exception):
    """An executed-result claim that does not name a reviewable, committed artifact."""


def _git_blob(sha: str, rel: str):
    """The bytes of one repository path at one commit, or None if it is not there."""
    import subprocess
    proc = subprocess.run(["git", "show", f"{sha}:{DEV_PREFIX}/{rel}"], cwd=REPO_ROOT,
                          capture_output=True)
    return proc.stdout if proc.returncode == 0 else None


def _git(*args, cwd=None):
    """Run git in a repository, returning (returncode, stdout)."""
    import subprocess
    proc = subprocess.run(("git",) + args, cwd=cwd or REPO_ROOT, capture_output=True, text=True)
    return proc.returncode, proc.stdout.strip()


def _assessor_root() -> str:
    return ASSESSOR_REPO_ROOT or REPO_ROOT


def commit_descends_from(ancestor: str, descendant: str) -> bool:
    """Is `descendant` in history that contains `ancestor`? A commit is its own descendant.

    A result about commit N has to be recorded in history containing N. An approved commit on
    an unrelated line is approved -- and says nothing about N."""
    if not (_is_sha1(ancestor) and _is_sha1(descendant)):
        return False
    rc, _ = _git("merge-base", "--is-ancestor", ancestor, descendant)
    return rc == 0


def _exact_nonempty_str(value) -> bool:
    """A real string with something in it. `True` is not a name and `""` is not a date."""
    return type(value) is str and value.strip() != ""


def _approval_schema_problem(record: Dict):
    """The first thing wrong with an approval record, or `None`.

    `approved_by` and `approved_utc` were documented and unchecked, so a record naming nobody
    and dated never was accepted. An approval no one signed and nothing dates is not an audit
    trail; it is a file that happens to contain a SHA."""
    if "scope" not in record:
        return "declares no scope; this assessor needs " + repr(APPROVAL_SCOPE)
    if type(record["scope"]) is not str or record["scope"] != APPROVAL_SCOPE:
        return (f"declares scope {record['scope']!r}; this assessor needs "
                f"{APPROVAL_SCOPE!r}")
    if "approved_sha" not in record:
        return "names no approved_sha"
    if not _is_sha1(record["approved_sha"]):
        return (f"names approved_sha {record['approved_sha']!r}, which is not a commit id: "
                f"exactly 40 lowercase hex characters")
    if "approved_by" not in record:
        return "records no approved_by, so nobody is named as having approved it"
    if not _exact_nonempty_str(record["approved_by"]):
        return f"records approved_by {record['approved_by']!r}, which is not a name"
    if "approved_utc" not in record:
        return "records no approved_utc, so nothing dates the approval"
    if not _exact_nonempty_str(record["approved_utc"]):
        return f"records approved_utc {record['approved_utc']!r}, which is not a timestamp"
    if _parse_utc(record["approved_utc"]) is None:
        return f"records approved_utc {record['approved_utc']!r}, which is not a timestamp"
    return None


def assessor_is_approved():
    """Is the code doing the assessing itself reviewed, and is the tree it runs from that code?

    Returns `(ok, reason)`. Everything else in this module points outward; this is the one
    check that points at the checker. Below this bar no requirement may read READY, because a
    verdict from an unapproved assessor is a verdict from unreviewed code."""
    import hashlib
    import json
    root = _assessor_root()
    if not APPROVAL_RECORD_PATH:
        return False, ("no approval record is configured, so no assessor build is approved and "
                       "nothing this assessor produces may authorize anything")
    given = os.path.abspath(APPROVAL_RECORD_PATH)
    if not os.path.isfile(given):
        return False, f"the approval record {given!r} does not exist"
    # The boundary is about where the BYTES are, so both the name and what it resolves to are
    # checked. Comparing `abspath` alone meant a link whose name sat outside the repository
    # counted as outside while the file it named was in the tree being approved -- the
    # self-reference restored by one symlink. The literal path is checked too, conservatively:
    # a link inside the approved tree is the tree choosing what its own approval says, whatever
    # it happens to point at today.
    real_root = os.path.realpath(root)
    for label, candidate in (("resolves to", os.path.realpath(given)), ("is at", given)):
        if candidate == real_root or candidate.startswith(real_root + os.sep) \
                or candidate == os.path.abspath(root) \
                or candidate.startswith(os.path.abspath(root) + os.sep):
            return False, (f"the approval record {label} {candidate!r}, inside the repository "
                           f"it approves; the authority has to be outside the thing it "
                           f"authorizes")
    record_path = given
    try:
        with open(record_path) as fh:
            record = json.load(fh)
    except Exception as exc:                                  # noqa: BLE001
        return False, f"the approval record {record_path!r} is not readable JSON: {exc}"
    # An approval record is external input, and `[]`, `null`, a string and a number are all
    # valid JSON. None of them has `.get` -- which the refusal message used to call anyway.
    if not isinstance(record, dict):
        return False, (f"the approval record is a JSON {type(record).__name__}, not an object; "
                       f"there are no fields to read")
    problem = _approval_schema_problem(record)
    if problem:
        return False, f"the approval record {problem}"
    approved_sha = record["approved_sha"]

    rc, head = _git("rev-parse", "HEAD", cwd=root)
    if rc != 0 or not _is_sha1(head):
        return False, f"the assessor's own HEAD could not be read from {root!r}"
    if approved_sha != head:
        return False, (f"the approval record approves {approved_sha[:7]}, and the assessor is "
                       f"running at {head[:7]}")

    # A record may point at a signed annotated tag instead of standing alone. If it names one,
    # the signature has to verify AND the tag has to be this commit -- an unverifiable tag is
    # a refusal, never a shrug.
    tag = record.get("signed_tag")
    if tag:
        rc, _ = _git("verify-tag", str(tag), cwd=root)
        if rc != 0:
            return False, (f"the tag {tag!r} named by the approval record has no verifiable "
                           f"signature")
        rc, resolved = _git("rev-list", "-n", "1", str(tag), cwd=root)
        if rc != 0 or resolved != head:
            return False, (f"the tag {tag!r} resolves to {resolved[:7] or '?'}, not the "
                           f"running {head[:7]}")
    # The commit is approved; the whole TREE has to be that commit.
    #
    # Comparing named modules is not enough on its own: it says nothing about a file nobody
    # thought to list, nor about a new one dropped in beside them. `git status` answers for
    # everything at once -- tracked edits, deletions and untracked additions alike -- and the
    # per-module comparison below then says WHICH runtime file differs, which a status line
    # about some unrelated path would not.
    rc, dirty = _git("status", "--porcelain", "--untracked-files=all", cwd=root)
    if rc != 0:
        return False, f"the assessor's own worktree status could not be read from {root!r}"
    if dirty:
        entries = [ln.strip() for ln in dirty.splitlines()][:8]
        untracked = any(ln.startswith("??") for ln in dirty.splitlines())
        return False, (f"the assessor's worktree is not clean at {head[:7]}"
                       f"{' (including untracked files)' if untracked else ''}: "
                       f"{entries}. The code running is not the code that was approved")
    for rel in ASSESSOR_MODULES:
        rc, _ = _git("cat-file", "-e", f"{head}:{DEV_PREFIX}/{rel}", cwd=root)
        blob = None
        if rc == 0:
            import subprocess
            blob = subprocess.run(["git", "show", f"{head}:{DEV_PREFIX}/{rel}"], cwd=root,
                                  capture_output=True).stdout
        if blob is None:
            return False, f"{rel} is not present at the approved assessor commit {head[:7]}"
        try:
            with open(os.path.join(root, DEV_PREFIX, rel), "rb") as fh:
                actual = fh.read()
        except OSError as exc:
            return False, f"{rel} cannot be read from the assessor's tree: {exc}"
        if hashlib.sha256(actual).hexdigest() != hashlib.sha256(blob).hexdigest():
            return False, (f"{rel} on disk differs from the approved assessor commit "
                           f"{head[:7]}: the policy being enforced is not the policy that was "
                           f"reviewed")
    return True, (f"assessor at {head[:7]}, approved by an external record, "
                  f"{len(ASSESSOR_MODULES)} modules matching")


def canonical_minimum_free_gib() -> float:
    """G18's threshold, from the capacity model -- never from the target.

    The assessor re-measured free space and then compared it against a `minimum_free_gib` the
    TARGET supplied: half the gate hardened, half left as a field anyone could write. Lower it
    and an empty device passes.

    Read at call time, from `ops/p5s8_evidence.MINIMUM_FREE_GIB`, which is itself derived as
    `HARD_RESERVE + additional`. A copy of the number here would be a second threshold to keep
    in step, and the copy is what goes stale."""
    from ops import p5s8_evidence
    return p5s8_evidence.MINIMUM_FREE_GIB


def executions_dir() -> str:
    return os.path.join(REPO_ROOT, DEV_PREFIX, EXECUTIONS_REL)


def resolve_execution_path(claim: str) -> str:
    """The canonical path of an executed-result artifact, or a refusal.

    Absolute paths and `..` are refused by name -- saying which rule stopped it is more useful
    than a generic containment message -- and the resolved path must land inside the one
    committed directory, which is what catches a symlink pointing out of it."""
    if os.path.isabs(claim):
        raise ExecutionRefused(
            f"{claim!r} is an absolute path; an executed-result artifact is named relative to "
            f"the committed {EXECUTIONS_REL} directory")
    if ".." in claim.replace("\\", "/").split("/"):
        raise ExecutionRefused(
            f"{claim!r} contains a '..' component; the artifact must be inside the committed "
            f"directory, not reached by walking out of it")
    root = os.path.realpath(executions_dir())
    resolved = os.path.realpath(os.path.join(executions_dir(), claim))
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ExecutionRefused(
            f"{claim!r} resolves to {resolved}, outside the committed {root}. A symlink out of "
            f"the directory is not in the directory")
    return resolved


def load_execution(claim: str, sha: str) -> Dict:
    """An executed-result artifact, read FROM GIT at `sha`.

    Read from the working tree, the artifact was whatever happened to be on disk -- so the hand
    that wrote the claim could write the evidence. Reading the committed blob makes it a
    reviewable object: it is in the history, or it does not count."""
    import json
    resolved = resolve_execution_path(claim)
    # Both sides canonical. `resolved` is a realpath, and on macOS the temp tree lives behind
    # a `/var` -> `/private/var` symlink -- so relpath against a non-canonical base produces a
    # `../..` walk and every lookup misses.
    rel = os.path.relpath(resolved, os.path.realpath(os.path.join(REPO_ROOT, DEV_PREFIX)))
    blob = _git_blob(sha, rel)
    if blob is None:
        raise ExecutionRefused(
            f"{claim!r} is not tracked at {sha[:7]}; an artifact that was never committed "
            f"cannot be reviewed and is not evidence")
    try:
        return json.loads(blob)
    except ValueError as exc:
        raise ExecutionRefused(f"{claim!r} is not readable JSON at that commit: {exc}") from exc


def expected_suite_digest(suite: str, sha: str):
    """sha256 of a test module AT `sha`, recomputed by the assessor.

    A digest that matches nothing certifies nothing. This is what the approved commit actually
    contains, so a claim can be checked against the suite it names rather than believed."""
    import hashlib
    blob = _git_blob(sha, suite.replace(".", "/") + ".py")
    return hashlib.sha256(blob).hexdigest() if blob is not None else None


def _check_identity(t: Dict) -> tuple:
    ident = t.get("identity") or {}
    reviewed, head = ident.get("reviewed_sha"), ident.get("head_sha")
    if not reviewed or not head:
        return NOT_ADJUDICATED, "no identity preflight was collected from the target"
    if reviewed not in REVIEWED_DEPLOY_SHAS:
        return BLOCKED, (f"{reviewed!r} is not in the approved deploy set; a target cannot "
                         f"declare its own commit reviewed")
    if reviewed != head:
        return BLOCKED, f"HEAD {head!r} is not the reviewed commit {reviewed!r}"
    if ident.get("worktree_clean") is not True:
        return BLOCKED, "the checkout is not clean; the deployed tree is not the reviewed tree"
    reported = ident.get("modules")
    if not reported:
        return NOT_ADJUDICATED, ("no module inventory was fingerprinted, so a matching SHA "
                                 "proves only what git says, not what is on disk")
    # The declaration itself, before anything is compared against it. A shrunken inventory
    # makes every check below pass on a subset, which is exactly how the hand-written list
    # came to omit `store/source_provenance.py` and every dependency of `api/app.py`.
    declaration = deployment_inventory_problem()
    if declaration:
        return BLOCKED, f"the declared deployment inventory {declaration}"
    expected = expected_module_digests(reviewed)
    if not expected:
        return NOT_ADJUDICATED, (f"could not recompute the module fingerprints for {reviewed} "
                                 f"here, so the reported ones cannot be checked against "
                                 f"anything")
    # Fail closed on a declared module the approved commit does not contain. Skipping it let
    # identity pass having never examined one of its own declared modules.
    absent = missing_inventory_modules(reviewed)
    if absent:
        return BLOCKED, (f"declared deployment module(s) {absent} are not present at the "
                         f"approved commit {reviewed[:7]}, so the inventory cannot be checked "
                         f"in full; a declaration the commit cannot satisfy is a defect in "
                         f"one of the two, never a pass")
    # And the same question asked of the TARGET's commit rather than the assessor's tree: the
    # declaration agreeing with what today's source imports says nothing about `reviewed_sha`.
    #
    # AFTER the git-only checks above, deliberately. This one materializes a tree and imports
    # it, so it is both the expensive check and the one whose failure is least specific -- a
    # missing declared module makes the entry point fail to import, and "your entry point did
    # not load" is a worse answer than naming the module that is gone.
    target_problem = target_closure_problem(reviewed)
    if target_problem:
        return BLOCKED, f"the approved commit {reviewed[:7]} {target_problem}"
    missing = sorted(set(expected) - set(reported))
    if missing:
        return BLOCKED, (f"the inventory does not cover {missing}; an absent module reads as "
                         f"clean and is the easiest thing to leave out")
    wrong = sorted(k for k, v in expected.items() if reported.get(k) != v)
    if wrong:
        return BLOCKED, (f"module fingerprint mismatch against {reviewed[:7]}: {wrong}. The "
                         f"deployed bytes are not the approved bytes")
    return READY, (f"clean checkout at {head[:7]}, {len(expected)} module fingerprints "
                   f"recomputed from the approved commit and matched")


#: Fields a mount-barrier record must carry. Collected on the host by `findmnt` and `stat`;
#: this module ADJUDICATES that record and cannot establish any of it for a remote filesystem.
MOUNT_BARRIER_FIELDS = ("monolith_path", "monolith_realpath", "source", "target", "fstype",
                        "options", "device", "run_id", "host", "collected_utc")

#: The paths that must stay WRITABLE while the monolith is frozen. A deployment whose P5 roots
#: and delta went read-only with it cannot ingest, cannot repair and cannot compact -- so the
#: barrier is only meaningful if it is selective.
MOUNT_BARRIER_WRITABLE_ROLES = ("p5_blocks", "p5_repairs", "delta")


def _mount_is_read_only(options) -> bool:
    """Is this mount read-only, by its option list? By exact token, never by substring.

    `"rw,relatime"` contains no standalone `ro`, but a substring test for `"ro"` finds one
    inside `relatime` -- and would call a writable mount frozen."""
    if type(options) is not str:
        return False
    return "ro" in [tok.strip() for tok in options.split(",")]


def mount_barrier_problem(evidence, *, run_id=None, host=None, now=None):
    """The first thing wrong with a mount-barrier record, or `None`.

    Adjudicates COLLECTED evidence. Nothing here can look at VM24's mount table; what it can do
    is refuse a record that is absent, malformed, stale, about another host or run, or that
    describes a mount which is not actually read-only."""
    import datetime
    if type(evidence) is not dict:
        return (f"is {type(evidence).__name__}, not a record; a barrier nobody measured is not "
                f"a barrier")
    for field in MOUNT_BARRIER_FIELDS:
        if field not in evidence:
            return f"records no {field}"
        if field == "device":
            if type(evidence[field]) is bool or not isinstance(evidence[field], int):
                return f"records device {evidence[field]!r}, which is not a device number"
        elif not _exact_nonempty_str(evidence[field]):
            return f"records {field} {evidence[field]!r}, which is not a non-empty string"

    if evidence["monolith_path"] != evidence["monolith_realpath"]:
        return (f"names {evidence['monolith_path']!r} but it resolves to "
                f"{evidence['monolith_realpath']!r}. A symlinked monolith can be re-pointed "
                f"without the record changing, so the path that is frozen and the path that is "
                f"served would be different questions")
    if not _mount_is_read_only(evidence["options"]):
        return (f"reports mount options {evidence['options']!r} for {evidence['target']!r}, "
                f"which is not read-only. Permission bits are not this check: `chmod` is "
                f"reversible by anything with write access to the parent, and a root process "
                f"ignores it entirely")
    if not evidence["monolith_realpath"].startswith(evidence["target"].rstrip("/") + "/") \
            and evidence["monolith_realpath"] != evidence["target"]:
        return (f"reports the monolith at {evidence['monolith_realpath']!r} but the read-only "
                f"mount at {evidence['target']!r}; the record does not show the monolith to be "
                f"ON that mount")

    writable = evidence.get("writable_paths")
    if type(writable) is not list or not writable:
        return ("records no writable_paths. A read-only mount for the monolith has to coexist "
                "with writable P5 roots, WAL and delta -- freezing all of them would leave a "
                "deployment that cannot ingest, repair or compact")
    seen_roles = set()
    for entry in writable:
        if type(entry) is not dict:
            return f"records a writable path entry {entry!r} that is not a record"
        for field in ("role", "path", "source", "target", "options"):
            if not _exact_nonempty_str(entry.get(field)):
                return f"a writable_paths entry records no {field}"
        seen_roles.add(entry["role"])
        if _mount_is_read_only(entry["options"]):
            return (f"reports {entry['role']} at {entry['path']!r} on a READ-ONLY mount "
                    f"({entry['target']!r}, {entry['options']!r}); that deployment cannot "
                    f"ingest, repair or compact")
        if entry["target"] == evidence["target"]:
            return (f"reports {entry['role']} on the SAME mount as the monolith "
                    f"({entry['target']!r}). A mount is read-only or it is not: if the monolith "
                    f"is frozen then so is {entry['role']}, and if {entry['role']} is writable "
                    f"then the monolith is not frozen. The two must be separate mounts")
    missing_roles = [r for r in MOUNT_BARRIER_WRITABLE_ROLES if r not in seen_roles]
    if missing_roles:
        return (f"records no writable mount for {missing_roles}; the barrier is only meaningful "
                f"if it is selective, and an unrecorded role is one nobody checked")

    if run_id is not None and evidence["run_id"] != run_id:
        return (f"was collected in run {evidence['run_id']!r}, not {run_id!r}; a mount layout "
                f"observed in another run is a fact about another moment")
    if host is not None and evidence["host"] != host:
        return (f"was collected on {evidence['host']!r}, not {host!r}; a mount layout "
                f"observed on another host says nothing about this one")
    stamped = _parse_utc(evidence["collected_utc"])
    if stamped is None:
        return f"records collected_utc {evidence['collected_utc']!r}, which is not a timestamp"
    when = now or datetime.datetime.now(datetime.timezone.utc)
    age = (when - stamped).total_seconds()
    if age < -MAX_CLOCK_LEAD_S:
        return f"was collected {int(-age)}s in the future; that is not a reading"
    if age > MAX_CAPACITY_AGE_S:
        return (f"is {int(age)}s old, past the {MAX_CAPACITY_AGE_S}s collection window. A "
                f"mount can be remounted read-write at any time, so a stale record says what "
                f"was true then")
    return None


def _check_monolith_barrier(t: Dict) -> tuple:
    """Is the legacy monolith on a genuinely read-only mount, beside writable P5 paths?

    NOT the same question as `check_monolith_sealed()` in `ops/p5_legacy_base.py`, which reads
    permission bits at build time and is explicitly necessary-not-sufficient. This adjudicates
    the operational barrier, and it is the one that has to hold for a `legacy_base` generation
    to be a safe authority: `content_seal` detects a changed store after the fact, and is blind
    to a write that restores size and mtime."""
    evidence = t.get("monolith_barrier", _MISSING)
    if evidence is _MISSING:
        return NOT_ADJUDICATED, ("no mount-barrier record was collected. Nothing here can read "
                                 "VM24's mount table; this adjudicates a record and there is "
                                 "none, which is missing evidence rather than a barrier shown "
                                 "to be absent")
    problem = mount_barrier_problem(evidence, run_id=t.get("run_id"), host=t.get("host"))
    if problem:
        return BLOCKED, f"the mount-barrier record {problem}"
    return READY, _reason(monolith=evidence["monolith_realpath"], mount=evidence["target"],
                          source=evidence["source"], fstype=evidence["fstype"],
                          options=evidence["options"],
                          writable=len(evidence["writable_paths"]),
                          collected=evidence["collected_utc"])


def _check_deployment_tools(t: Dict) -> tuple:
    """Does the deployment carry its operator tools, and are they the approved bytes?

    Asked of whatever commit the target REPORTS, not of the approved set. Whether that commit
    may be deployed at all is `identity`'s question and identity answers it; readiness needs
    every requirement READY, so this one cannot promote anything by passing. That separation is
    what lets a packaging revision be measured on its merits while staying undeployable."""
    import hashlib
    ident = t.get("identity") or {}
    # `head_sha`, deliberately, NOT `reviewed_sha`. The question is whether the tree that is
    # DEPLOYED can run the provisioner, and on VM24 the two differ: the target reports
    # reviewed_sha=3223fba beside head_sha=a9db4b7. Reading `reviewed_sha` would answer a
    # question about a commit that is not on that disk, and would compare the digests the
    # collector took FROM the disk against blobs from a different commit -- a mismatch
    # manufactured by the assessor. Whether the deployed commit is allowed to be deployed is
    # `identity`'s question, and identity refuses when head is not the reviewed commit.
    deployed = ident.get("head_sha")
    if not deployed:
        return NOT_ADJUDICATED, ("no identity preflight was collected, so there is no deployed "
                                 "commit to check the operator tools against")
    if not _is_sha1(deployed):
        return BLOCKED, (f"the target reports head_sha {deployed!r}, which is not a commit "
                         f"id, so no tool can be recomputed from it")
    # The declaration before the comparison. An empty tuple would make every check below pass
    # over nothing -- a gate disarmed by emptying the list it enforces.
    if not DEPLOYMENT_TOOLS:
        return BLOCKED, ("no operator tool is declared, so this requirement checks nothing; "
                         "an empty declaration is not a passing one")
    problem = deployment_tools_problem(deployed)
    if problem:
        return BLOCKED, f"the deployment at {deployed[:7]} {problem}"
    reported = ident.get("tools", _MISSING)
    if reported is _MISSING:
        # NOT_ADJUDICATED, not BLOCKED -- and ONLY for a field nobody wrote. A collector that
        # never looked has not shown the tool to be missing, and the artifact collected on
        # 2026-08-21 predates this field entirely. It still blocks readiness: `assess()`
        # requires every requirement READY, and this is not that.
        #
        # The tool being ABSENT AT THE COMMIT is a different finding, refused above from git
        # blobs without the collector's help. `identity` draws the same line for an uncollected
        # module inventory.
        return NOT_ADJUDICATED, (f"no operator tool was fingerprinted by the collector, so "
                                 f"what is present at {deployed[:7]} says only what git holds "
                                 f"and not what is on the deployment's disk; this is missing "
                                 f"evidence, not a tool shown to be missing")
    # Everything below is a field that EXISTS and cannot be used. That is not an evidence gap:
    # something wrote it, and what it wrote is unusable. Reporting that as "not adjudicated"
    # would let a malformed collector read the same as an old one, which is how a broken
    # pipeline stays broken quietly.
    if reported is None:
        return BLOCKED, ("the target reports tools=null; a field written as null is a collector "
                         "that ran and recorded nothing, not one that never looked")
    if type(reported) is not dict:
        return BLOCKED, (f"the target reports tools as {type(reported).__name__}, which cannot "
                         f"name a fingerprint per tool")
    if not reported:
        return BLOCKED, ("the target reports an empty tools object; something wrote it and put "
                         "no fingerprint in it, which is malformed evidence rather than absent "
                         "evidence")
    malformed = sorted(k for k, v in reported.items() if not _is_sha256(v))
    if malformed:
        return BLOCKED, (f"the target reports tool fingerprints that are not SHA-256 digests: "
                         f"{malformed}; a value that cannot be a digest cannot be compared "
                         f"with one")
    expected = {}
    for rel in DEPLOYMENT_TOOLS:
        blob = _git_blob(deployed, rel)
        if blob is None:                       # unreachable: deployment_tools_problem refused
            return BLOCKED, f"{rel} vanished from {deployed[:7]} between two reads"
        expected[rel] = hashlib.sha256(blob).hexdigest()
    missing = sorted(set(expected) - set(reported))
    if missing:
        return BLOCKED, (f"the target does not fingerprint {missing}; an unreported tool reads "
                         f"as clean and is the easiest thing to leave out")
    wrong = sorted(k for k, v in expected.items() if reported.get(k) != v)
    if wrong:
        return BLOCKED, (f"operator tool fingerprint mismatch against {deployed[:7]}: {wrong}. "
                         f"The deployed tool is not the approved tool")
    return READY, (f"{len(expected)} operator tool fingerprints recomputed from {deployed[:7]} "
                   f"and matched: {sorted(expected)}")


def _live_manifest(manifest_root: str):
    """The live manifest, via the shipped reader. Returns (manifest, None) or (None, reason)."""
    if not manifest_root or not os.path.isdir(manifest_root):
        return None, f"no manifest root at {manifest_root!r}"
    live = os.path.join(manifest_root, block_manifest.LIVE_NAME)
    if not os.path.isfile(live):
        return None, f"no live manifest at {live}"
    import json
    try:
        with open(live) as fh:
            raw = json.load(fh)
    except Exception as exc:                                  # noqa: BLE001
        return None, f"the live manifest is unreadable: {exc!r}"
    try:
        return block_manifest.validate_manifest(raw), None
    except Exception as exc:                                  # noqa: BLE001
        return None, f"the live manifest is invalid: {exc!r}"


def _check_manifest(t: Dict) -> tuple:
    manifest, why = _live_manifest(t.get("manifest_root"))
    if manifest is None:
        return BLOCKED, why
    return READY, _reason(generation=manifest["generation"],
                          segments=len(manifest["segments"]),
                          id=manifest["generation_id"])


def _check_blocks(t: Dict) -> tuple:
    """Open the manifest the way the deployment opens it, then add what the store does not ask.

    This joined the segment path to the root and asked whether it existed, which a `../` or a
    symlink out of the block root satisfies -- and those are exactly what
    `SegmentedCubeStore._resolve_segment_path` refuses: a block may not point at the delta, at
    another store, or through a symlink. Two path authorities disagreeing is worse than one
    being strict, because the readiness check certifies a manifest the deployment then refuses
    to open. So the store IS the authority here: constructing it applies containment, the
    absolute-path refusal, the legacy allow-list and the layout contract in one go."""
    manifest, why = _live_manifest(t.get("manifest_root"))
    if manifest is None:
        return BLOCKED, f"cannot check blocks: {why}"
    from store.segmented_cube import SegmentedCubeStore
    try:
        # From the module, never from `t`: the target is the thing being governed.
        store = SegmentedCubeStore(t["manifest_root"],
                                   allowed_legacy_paths=list(ALLOWED_LEGACY_PATHS))
    except Exception as exc:                                  # noqa: BLE001
        return BLOCKED, f"the deployment could not open this manifest: {exc}"
    # `build_provenance` is optional in the schema and the store does not require it, so it is
    # checked here -- the one thing this adds rather than delegates.
    for seg in manifest["segments"]:
        if not seg.get("build_provenance"):
            return BLOCKED, (f"segment {seg['segment_id']!r} carries no build_provenance, so "
                             f"nothing records which sources produced its bytes")
    return READY, _reason(segments_verified=len(store.segment_paths),
                          ids=",".join(s["segment_id"] for s in manifest["segments"]))


def ignorable_days(state) -> set:
    """Days poisoned SOLELY by the log's own initialization record.

    `repair_wal._replay` classifies any non-INTENT record with no matching intent as a terminal
    orphan and poisons its `day`. The log's own `wal_initialized` record carries `day == ""`, so
    every freshly initialized WAL reports `poisoned_days == {""}` while being entirely healthy.

    The first version of this dropped every empty-string day, which is broader than the wart it
    works around: "no real day is empty today" is an observation about current data, not a
    property of the format, and a filter justified by an observation outlives it. So the days
    forgiven are computed from the records -- a day is ignorable only if every record carrying
    it is a `wal_initialized`.

    Reported upstream rather than fixed at the source: correcting the replay changes signed-off
    P5-S5 WAL semantics, which is not this phase's call to make."""
    from collections import defaultdict
    kinds = defaultdict(set)
    for rec in getattr(state, "records", ()) or ():
        kinds[rec.get("day")].add(rec.get("record"))
    return {day for day, seen in kinds.items() if seen == {"wal_initialized"}}


def _real_days(days, state) -> set:
    """The poisoned/blocked days that actually name something, given this log's records."""
    return set(days) - ignorable_days(state)


def _check_wal_anchor(t: Dict) -> tuple:
    wal_root, anchor_root = t.get("wal_root"), t.get("anchor_root")
    if not wal_root or not os.path.isdir(wal_root):
        return BLOCKED, f"no P5 WAL root at {wal_root!r}"
    if not anchor_root or not os.path.isdir(anchor_root):
        return BLOCKED, f"no anchor root at {anchor_root!r}"
    # An explicit `anchor_domain_id(...) is None` check stood here and was removed: mutation
    # testing showed it could be deleted with every test still passing, because
    # `resolve_anchor_root` already refuses an undeclared domain. Two copies of one rule is the
    # second truth this module exists to avoid -- and the copy is the one that goes stale.
    try:
        resolved = repair_wal.resolve_anchor_root(wal_root, anchor_root)
    except Exception as exc:                                  # noqa: BLE001
        return BLOCKED, (f"the anchor root is refused by the shipped resolver, which is the "
                         f"authority on rollback domains: {exc}")
    try:
        state = repair_wal.read_wal(wal_root, anchor_root=resolved)
    except Exception as exc:                                  # noqa: BLE001
        return BLOCKED, f"the WAL is not readable: {exc!r}"
    # `authority` is None until `wal_initialized` binds the log to a manifest. An empty
    # directory parses cleanly and reports nothing wrong -- reading that as a healthy WAL is
    # how "no WAL at all" would have been adjudicated READY.
    if state.authority is None:
        return BLOCKED, ("the WAL has never been initialized: no wal_initialized record binds "
                         "it to a manifest authority")
    bound = state.authority.get("authority") or state.authority
    blocked = _real_days(state.blocked_days, state)
    if blocked:
        return BLOCKED, f"{len(blocked)} day(s) have an OPEN repair intent: {sorted(blocked)}"
    poisoned = _real_days(state.poisoned_days, state)
    if poisoned:
        return BLOCKED, (f"{len(poisoned)} day(s) carry untrustworthy repair evidence: "
                         f"{sorted(poisoned)}")
    try:
        state.assert_fresh()
    except Exception as exc:                                  # noqa: BLE001
        return BLOCKED, f"the WAL is not fresh against its anchor: {exc!r}"
    return READY, _reason(anchor=resolved, domain=repair_wal.anchor_domain_id(resolved),
                          authority=repair_wal.authority_digest(bound)
                          if isinstance(bound, dict) else "recorded",
                          open_repairs=0)


def _check_locks(t: Dict) -> tuple:      # noqa: C901 -- one binding per branch, by design
    locks = t.get("locks") or {}
    missing = [k for k in ("compaction", "ingest") if not locks.get(k)]
    if missing:
        return BLOCKED, f"no canonical path declared for lock(s): {missing}"
    # Distinct STRINGS are not distinct locks. A symlink or a bind mount makes two paths one
    # file, and two writers each holding "their" lock are excluded from nothing.
    if os.path.realpath(locks["compaction"]) == os.path.realpath(locks["ingest"]):
        return BLOCKED, ("the compaction and ingest lock paths resolve to the same file; one "
                         "lock cannot express the ordering between two writers")
    for kind in ("compaction", "ingest"):
        parent = os.path.dirname(os.path.abspath(locks[kind])) or "."
        if not os.path.isdir(parent):
            return BLOCKED, f"the {kind} lock directory does not exist: {parent}"
        if not os.access(parent, os.W_OK | os.X_OK):
            return BLOCKED, (f"the {kind} lock directory is not writable: {parent}. A lock "
                             f"that cannot be created is a lock that is never held")
    # "The directory exists" is not evidence that a lock can be taken in it, and
    # `p5_compaction.lock` does not exist on the target at all. Only a controlled acquisition
    # says whether the fencing this whole posture rests on actually works.
    probe = locks.get("acquire_probe")
    if not probe:
        return NOT_ADJUDICATED, (f"no lock acquisition probe was recorded. The paths are "
                                 f"declared and distinct, but nothing has taken "
                                 f"{locks['compaction']!r} and released it, so acquirability "
                                 f"is assumed rather than shown")
    # An unbound probe claims some lock somewhere was taken once. The declared compaction lock
    # is the one the whole posture fences with, and a probe from another path, host, run or
    # sitting is evidence about a different event. Each binding is checked separately so no
    # missing field is covered by another field being present.
    for field in ("path", "run_id", "host", "at_utc", "actor"):
        if not probe.get(field):
            return (NOT_ADJUDICATED if field == "actor" else BLOCKED), (
                f"the lock probe declares no {field}, so it cannot be attributed to this "
                f"lock, this host and this collection")
    if os.path.realpath(probe["path"]) != os.path.realpath(locks["compaction"]):
        return BLOCKED, (f"the probe took {probe['path']!r}, which is a different file from "
                         f"the declared compaction lock {locks['compaction']!r}")
    if probe["run_id"] != t.get("run_id"):
        return BLOCKED, (f"the probe is from run {probe['run_id']!r}, not the collection's "
                         f"{t.get('run_id')!r}")
    if str(probe["host"]).split(".")[0] != str(t.get("host") or "").split(".")[0]:
        return BLOCKED, (f"the probe was taken on host {probe['host']!r}, not the target's "
                         f"{t.get('host')!r}")
    probed_at, collected = _parse_utc(probe["at_utc"]), _parse_utc(t.get("collected_utc"))
    if probed_at is None or collected is None:
        return NOT_ADJUDICATED, ("the probe or the collection carries no usable timestamp, so "
                                 "the probe cannot be placed in this collection")
    drift = abs((probed_at - collected).total_seconds())
    if drift > MAX_PROBE_SKEW_S:
        return BLOCKED, (f"the probe was taken {int(drift)}s from the collection that reports "
                         f"it; that is a different sitting, and lock state does not carry")
    if probe.get("acquired") is not True or probe.get("released") is not True:
        return BLOCKED, (f"the lock acquisition probe did not complete: {probe}. A lock that "
                         f"cannot be taken, or cannot be let go, fences nothing")
    return READY, _reason(compaction=locks["compaction"], ingest=locks["ingest"],
                          probe=f"acquired and released by {probe['actor']}",
                          order="compaction acquired before ingest")


def _parse_utc(text):
    """An ISO timestamp, or None. `Z` is not a timezone Python parses without help."""
    from datetime import datetime
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def capacity_expires_utc(t: Dict):
    """When this target's capacity reading stops being believable."""
    from datetime import timedelta
    measured = _parse_utc((t.get("capacity") or {}).get("measured_utc"))
    if measured is None:
        return None
    return (measured + timedelta(seconds=MAX_CAPACITY_AGE_S)).isoformat()


def _check_capacity(t: Dict, now=None) -> tuple:
    cap = t.get("capacity") or {}
    if not cap:
        return NOT_ADJUDICATED, "no capacity evidence was supplied"
    if not cap.get("run_id") or not cap.get("measured_utc"):
        return NOT_ADJUDICATED, ("the capacity figure carries no run_id/measured_utc, so it is "
                                 "a number in a file rather than a measurement")
    # Bound to the collection that carries it -- and the collection has to say who it is.
    # `if target_run and ...` skipped the binding when the field was absent, which made
    # omitting it safer than getting it wrong. An absent field must never be the easy path.
    target_run = t.get("run_id")
    if not target_run:
        return NOT_ADJUDICATED, ("the target declares no run_id, so the capacity reading cannot "
                                 "be bound to the collection that carries it")
    if cap["run_id"] != target_run:
        return NOT_ADJUDICATED, (f"the capacity reading is from run {cap['run_id']!r} while the "
                                 f"collection is run {target_run!r}: different moments")
    measured = _parse_utc(cap["measured_utc"])
    if measured is None:
        return NOT_ADJUDICATED, f"measured_utc {cap['measured_utc']!r} is not a timestamp"
    collected = _parse_utc(t.get("collected_utc"))
    if collected is None:
        return NOT_ADJUDICATED, ("the target declares no usable collected_utc, so the reading "
                                 "cannot be placed against the collection")
    skew = abs((measured - collected).total_seconds())
    if skew > MAX_CAPACITY_SKEW_S:
        return NOT_ADJUDICATED, (f"the capacity reading sits {int(skew)}s from the collection "
                                 f"that carries it; they are meant to be one act, so one of "
                                 f"them was reused")
    # Free space is true of a moment. An ingest window, a restart or an overnight pause moves
    # it, and none of them announce themselves in the artifact.
    if now is None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
    elif isinstance(now, str):
        now = _parse_utc(now)
    age = (now - measured).total_seconds()
    # A reading ahead of the clock is a clock problem or a fabricated one. Either way it cannot
    # be aged, and an unaged reading never expires -- so it would be believed forever.
    if age < -MAX_CLOCK_LEAD_S:
        return NOT_ADJUDICATED, (f"the capacity reading is {int(-age)}s in the future against "
                                 f"the assessing clock; a reading that cannot be aged cannot "
                                 f"expire")
    if age > MAX_CAPACITY_AGE_S:
        return NOT_ADJUDICATED, (f"the capacity reading has expired: measured "
                                 f"{cap['measured_utc']}, {int(age)}s ago, against a "
                                 f"{MAX_CAPACITY_AGE_S}s window. Re-collect before relying "
                                 f"on it")
    # A free-space reading is a fact about a DEVICE. Nothing tied it to the one holding the
    # target root, so a roomy unrelated volume would have produced READY while the volume that
    # matters was full.
    on_host, host_reason = _on_host(t)
    if not on_host:
        return NOT_ADJUDICATED, (f"the reading cannot be tied to the device holding "
                                 f"{t.get('root')!r}: {host_reason}")
    if cap.get("filesystem_dev") is None:
        return NOT_ADJUDICATED, ("the reading records no filesystem_dev, so it names no "
                                 "device and cannot be matched to the target root")
    declared_root = cap.get("data_root")
    if not declared_root:
        return NOT_ADJUDICATED, "the reading records no data_root, so it measured no named path"
    root = t.get("root")
    if not root or os.path.realpath(declared_root) != os.path.realpath(root):
        return BLOCKED, (f"the reading measured data_root {declared_root!r}, not the target "
                         f"root {root!r}")
    try:
        actual_dev = os.stat(root).st_dev
    except OSError as exc:
        return NOT_ADJUDICATED, f"cannot stat the target root {root!r}: {exc}"
    if actual_dev != cap["filesystem_dev"]:
        return BLOCKED, (f"the reading is of device {cap['filesystem_dev']}, but {root!r} is "
                         f"on device {actual_dev}: free space was measured somewhere else")

    collected_free = cap.get("free_now_gib")
    if collected_free is None:
        return NOT_ADJUDICATED, "the capacity evidence is missing free_now_gib"
    # The THRESHOLD is policy, not collected data. The artifact's copy is provenance: it says
    # what the collection believed the bar to be, and it has to agree with the bar.
    minimum = canonical_minimum_free_gib()
    collected_minimum = cap.get("minimum_free_gib")
    if collected_minimum is None:
        return NOT_ADJUDICATED, ("the capacity evidence records no minimum_free_gib, so there "
                                 "is nothing to check the canonical threshold against")
    if collected_minimum != minimum:
        return BLOCKED, (f"the collection's minimum_free_gib is {collected_minimum}, and the "
                         f"canonical threshold is {minimum}. A gate whose bar travels with the "
                         f"evidence is not a gate")
    # The artifact's figure decided G18. An artifact is a record of what someone saw; the gate
    # is about what is there NOW, on this device, and the assessor is standing on it. So it
    # measures. The collected figure stays as provenance -- it says what the collection
    # observed -- and decides nothing.
    try:
        st = os.statvfs(root)
    except OSError as exc:
        return NOT_ADJUDICATED, f"cannot statvfs the target root {root!r}: {exc}"
    free = round(st.f_bavail * st.f_frsize / (1024 ** 3), 1)
    drift = f"; collected {collected_free} GiB" if collected_free != free else ""
    if free < minimum:
        return BLOCKED, (f"statvfs reports {free} GiB free on {root}, below the canonical "
                         f"{minimum} GiB minimum (run {cap['run_id']}){drift}")
    recommended = cap.get("recommended_free_gib")
    note = "" if recommended is None or free >= recommended else "; below the recommended target"
    return READY, (f"statvfs reports {free} GiB free on {root} against the canonical "
                   f"{minimum} GiB minimum (run {cap['run_id']}){drift}{note}")


#: Fields an executed-result artifact must carry. A source digest says which bytes exist; it
#: says nothing about whether anything ran them. These say what ran, against which commit, with
#: what result, and when.
EXECUTION_FIELDS = ("suite", "deploy_sha", "command", "exit_code", "tests_run", "ran_utc",
                    "source_digest")

#: Where executed-result artifacts are committed.
EXECUTIONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "bench", "results", "p5_failure_semantics")


def _check_failure_semantics(t: Dict) -> tuple:
    proven = t.get("failure_semantics_proven_by")
    if not proven:
        return NOT_ADJUDICATED, ("no suite was named as proving the refusal, rollback and "
                                 "partial-staging behaviour for this target")
    # A sentence is not evidence. Naming a suite says nothing about WHICH code it ran against
    # or whether that run happened, so the claim has to carry the commit and a digest of the
    # run that produced it.
    if not isinstance(proven, str):
        return NOT_ADJUDICATED, (f"the claim {proven!r} is not a path to an executed-result "
                                 f"artifact. A source digest says which bytes exist; it says "
                                 f"nothing about whether anything ran them")
    # WHERE the record lives and WHAT it is about are separate claims, and conflating them was
    # the bug: reading from `REVIEWED_DEPLOY_SHAS[0]` assumed the artifact sits at the first
    # approved commit. A result recording a run of commit N is committed AFTER N, so in
    # production it lives at some later commit and the lookup simply misses.
    evidence_commit = t.get("failure_semantics_evidence_commit")
    if evidence_commit in (None, ""):
        return NOT_ADJUDICATED, ("the target declares no failure_semantics_evidence_commit, so "
                                 "nothing says which commit the executed result is read from")
    if not _is_sha1(evidence_commit):
        return NOT_ADJUDICATED, (f"failure_semantics_evidence_commit {evidence_commit!r} is not "
                                 f"a commit id: exactly 40 lowercase hex characters. A branch "
                                 f"name, a short SHA or a number is not an identity")
    if evidence_commit not in REVIEWED_DEPLOY_SHAS:
        return BLOCKED, (f"the evidence commit {evidence_commit[:7]} is not in the approved "
                         f"set; a target cannot nominate its own history to be read from")
    try:
        run = load_execution(proven, evidence_commit)
    except ExecutionRefused as exc:
        return NOT_ADJUDICATED, f"no executed-result artifact at {proven!r}: {exc}"
    except Exception as exc:                                  # noqa: BLE001
        return NOT_ADJUDICATED, f"no executed-result artifact at {proven!r}: {exc}"
    missing = [k for k in EXECUTION_FIELDS if run.get(k) in (None, "")]
    if missing:
        return NOT_ADJUDICATED, (f"the executed-result artifact is missing {missing}; without "
                                 f"them it records no run of anything in particular")
    sha, suite, digest = run["deploy_sha"], run["suite"], run["source_digest"]
    if not _is_sha256(digest):
        return NOT_ADJUDICATED, (f"source_digest {digest!r} is not a SHA-256: 64 lowercase hex "
                                 f"characters")
    if not _is_sha1(sha):
        return NOT_ADJUDICATED, (f"the artifact's deploy_sha {sha!r} is not a commit id: "
                                 f"exactly 40 lowercase hex characters")
    if suite not in FAILURE_SEMANTICS_SUITES:
        return BLOCKED, (f"{suite!r} is not an authorized failure-semantics suite. A green run "
                         f"of some other suite says tests ran and did not fail, about nothing "
                         f"in particular; the authorized set is {list(FAILURE_SEMANTICS_SUITES)}")
    if sha not in REVIEWED_DEPLOY_SHAS:
        return BLOCKED, (f"the run was against {sha[:7]}, which is not in the approved deploy "
                         f"set")
    # After the approved-set check, deliberately: an unapproved sha is usually not a commit in
    # this history at all, so asking whether anything descends from it answers a different
    # question and hides the real objection.
    if not commit_descends_from(sha, evidence_commit):
        return BLOCKED, (f"the evidence commit {evidence_commit[:7]} does not descend from the "
                         f"deploy commit {sha[:7]}: a result about one line of history was "
                         f"recorded on another")
    if type(run["exit_code"]) is not int or run["exit_code"] != 0:
        return BLOCKED, (f"the recorded run exited {run['exit_code']!r}: a failing or unknown "
                         f"run proves nothing about fail-closed behaviour")
    if type(run["tests_run"]) is not int or run["tests_run"] <= 0:
        return BLOCKED, (f"the run reports {run['tests_run']!r} tests; a green run of nothing "
                         f"is the easiest green there is")
    if _parse_utc(run["ran_utc"]) is None:
        return NOT_ADJUDICATED, f"ran_utc {run['ran_utc']!r} is not a timestamp"
    if suite not in str(run["command"]):
        return BLOCKED, (f"the recorded command {run['command']!r} does not name the suite "
                         f"{suite!r} it claims to have run")
    # And the suite it names has to BE the one at the approved commit -- a run of a file that
    # has since changed is a result about different code.
    expected = expected_suite_digest(suite, sha)
    if expected is None:
        return BLOCKED, (f"{suite!r} is not present at {sha[:7]}, so no run of it could have "
                         f"proven anything about that commit")
    if digest != expected:
        return BLOCKED, (f"source_digest does not match {suite} at {sha[:7]}: the run was of a "
                         f"different file than the one it names")
    return READY, _reason(suite=suite, deploy_sha=sha[:7],
                          evidence_commit=evidence_commit[:7], exit_code=0,
                          tests_run=run["tests_run"], ran_utc=run["ran_utc"])


#: Requirements whose verdict comes from stat-ing paths. They mean nothing off the host that
#: owns those paths -- see `_on_host`.
FILESYSTEM_BACKED = ("manifest", "blocks", "wal_anchor", "locks")


def _on_host(target: Dict) -> tuple:
    """Is this process running on the host the target describes?

    The first real assessment reported `locks` BLOCKED because "the compaction lock directory
    does not exist". It does exist -- on VM24. The check ran on the auditor's laptop and
    reported on the wrong machine, and every other filesystem verdict in that run was right
    only by coincidence: the P5 roots happened to be absent in both places.

    So filesystem-backed requirements are adjudicated only where their paths live."""
    import socket
    declared = target.get("host")
    if not declared:
        return False, "the target names no host, so its paths cannot be attributed to one"
    here = socket.gethostname().split(".")[0]
    if declared.split(".")[0] != here:
        return False, (f"collected on host {declared!r}, adjudicated on {here!r}: stat-ing "
                       f"these paths here would report a different machine")
    return True, ""


CHECKERS = {
    "identity": _check_identity,
    "monolith_barrier": _check_monolith_barrier,
    "deployment_tools": _check_deployment_tools,
    "manifest": _check_manifest,
    "blocks": _check_blocks,
    "wal_anchor": _check_wal_anchor,
    "locks": _check_locks,
    "capacity": _check_capacity,
    "failure_semantics": _check_failure_semantics,
}


def _one_shot_exception(target: Dict, results: List[Dict], now=None) -> Dict:
    """Adjudicate a one-shot compaction risk acceptance, if one was supplied.

    Absent is the normal state and is reported as such rather than as a failure: almost every
    assessment has no exception, and an assessment without one is not deficient.

    Eligibility here NEVER implies readiness. `monolith_barrier` keeps whatever verdict it
    earned, `compaction_ready` keeps whatever value it had, and this field says only that a
    person bound an acceptance to this host, this run and this monolith state, and that the
    fresh operational checks passed."""
    from ops import p5_oneshot_exception as ex
    record = target.get("one_shot_exception")
    if record is None:
        return {"supplied": False, "eligible": False,
                "note": ("no one-shot exception was supplied, which is the normal state. "
                         "Compaction waits for the read-only mount barrier")}
    verdict = ex.evaluate(
        record=record, host=target.get("host"), run_id=target.get("run_id"),
        observed=target.get("monolith_state"), pre=target.get("pre_compaction"),
        post=target.get("post_compaction"), now=now)
    verdict["supplied"] = True
    # The barrier's own verdict is carried alongside so nobody has to infer it: an eligible
    # exception beside a BLOCKED barrier is the expected shape, not a contradiction.
    barrier = next((r for r in results if r["id"] == "monolith_barrier"), None)
    verdict["monolith_barrier_verdict"] = barrier["verdict"] if barrier else None
    return verdict


def assess(target: Dict, now=None) -> Dict:
    """Adjudicate one deployment target and return a machine-readable report.

    `now` is injectable so freshness is testable, and defaults to the wall clock."""
    label = target.get("label") or "UNLABELLED"
    disqualified = label in NON_QUALIFYING_LABELS or label != QUALIFYING_LABEL
    on_host, host_reason = _on_host(target)
    # The checker checks itself first. An unapproved assessor demotes everything: its findings
    # may be perfectly correct, and they are findings of unreviewed code, which is a different
    # statement from an approved one.
    assessor_ok, assessor_note = assessor_is_approved()

    results = []
    for req in REQUIREMENTS:
        rid = req["id"]
        if rid in APPROVAL_REQUIRED:
            verdict, reason = NEEDS_APPROVAL, APPROVAL_REQUIRED[rid]
        elif rid in FILESYSTEM_BACKED and not on_host:
            verdict, reason = NOT_ADJUDICATED, host_reason
        elif rid == "capacity":
            verdict, reason = _check_capacity(target, now=now)
        else:
            verdict, reason = CHECKERS[rid](target)
        if verdict == READY and not assessor_ok:
            verdict = NOT_ADJUDICATED
            reason = (f"the assessor is not approved, so this finding cannot authorize "
                      f"anything: {assessor_note}. The check itself found: {reason}")
        elif verdict == READY and disqualified:
            # The checker found the thing. It found it in evidence that is not of this
            # deployment, which is a different statement -- and the one that keeps being
            # collapsed. Report what stopped it, not merely that it stopped.
            verdict = NOT_ADJUDICATED
            reason = (f"evidence carries label {label!r}, which cannot settle a P5 compaction "
                      f"requirement; the checker's finding was: {reason}")
        results.append({"id": rid, "title": req["title"], "why": req["why"],
                        "evidence_kind": req["evidence_kind"],
                        "verdict": verdict, "reason": reason})

    summary = {v: [r["id"] for r in results if r["verdict"] == v] for v in VERDICTS}
    ready = all(r["verdict"] == READY for r in results)
    outstanding = summary[BLOCKED] + summary[NOT_ADJUDICATED] + summary[NEEDS_APPROVAL]
    return {
        "schema": "ghrsst.p5.readiness/1",
        "label": label,
        "assessor_approved": assessor_ok,
        "assessor_note": assessor_note,
        "adjudicated_on_host": on_host,
        "adjudicated_on_host_note": host_reason or "paths were stat-ed on the host that owns them",
        "target": {k: target.get(k) for k in
                   ("root", "manifest_root", "wal_root", "anchor_root", "locks")},
        "requirements": results,
        "summary": summary,
        "compaction_ready": ready,
        #: SEPARATE from `compaction_ready`, and deliberately not folded into it. A human may
        #: accept the risk of one compaction without the read-only mount; that acceptance does
        #: not make the deployment ready, does not make `monolith_barrier` READY, and does not
        #: authorize a second run. Reporting the two in one field would lose exactly the
        #: distinction the exception exists to preserve.
        "one_shot_compaction_exception": _one_shot_exception(target, results, now=now),
        #: Stated, not implied: this report describes one collection. Past this timestamp the
        #: capacity reading it rests on must be taken again -- a schedule tick, a restart or an
        #: overnight pause all move free space without announcing it here.
        "capacity_expires_utc": capacity_expires_utc(target),
        "max_capacity_age_s": MAX_CAPACITY_AGE_S,
        "re_collect_before_acting": True,
        "before_first_compaction": outstanding,
        "next_safe_step": _next_step(summary),
    }


def _next_step(summary: Dict) -> str:
    """The smallest reversible thing to do next, chosen from what is outstanding."""
    if summary[BLOCKED]:
        return (f"resolve the blocked requirement(s) {summary[BLOCKED]} — provisioning an "
                f"isolated P5 staging root is the reversible way to do it, and touches no "
                f"live path")
    if summary[NOT_ADJUDICATED]:
        return (f"collect qualifying evidence for {summary[NOT_ADJUDICATED]} from the real "
                f"deployment, read-only")
    if summary[NEEDS_APPROVAL]:
        return (f"obtain explicit human approval for {summary[NEEDS_APPROVAL]}; nothing in a "
                f"read-only preflight confers it")
    return "every requirement is READY; a first compaction may be scheduled under its own approval"


#: Where an assessed run's collected target and generated report live.
RUNS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "bench", "results", "p5_readiness_runs")
ARTIFACT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "bench", "results", "p5_readiness.json")


def load_target(path: str) -> Dict:
    """A target collected on the host, read back verbatim. Nothing here edits a reading."""
    import json
    with open(path) as fh:
        return json.load(fh)


def main(argv=None) -> int:
    """Assess a collected target and write the machine-readable readiness artifact."""
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", required=True, help="path to a collected target JSON")
    ap.add_argument("--out", default=ARTIFACT)
    args = ap.parse_args(argv)

    target = load_target(args.target)
    report = assess(target)
    # Provenance travels with the verdict: a readiness report that does not say which
    # collection it adjudicated is the same kind of unanchored claim as a bare free-space
    # figure, and this phase has spent long enough on those.
    report["provenance"] = {k: target.get(k) for k in ("run_id", "host", "collected_utc")}
    report["provenance"]["target_artifact"] = os.path.relpath(
        args.target, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    report["observed"] = {
        "service": target.get("service"),
        "p5_roots_present": target.get("p5_roots_present"),
        "staging": target.get("staging"),
        "identity": {k: v for k, v in (target.get("identity") or {}).items()
                     if k != "modules"},
        "capacity": target.get("capacity"),
    }
    with open(args.out, "w") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
    print(f"wrote {args.out}")
    for v in VERDICTS:
        ids = report["summary"][v]
        print(f"  {v:<22} {len(ids):>2}  {', '.join(ids) if ids else '-'}")
    print(f"  compaction_ready: {report['compaction_ready']}")
    print(f"  next: {report['next_safe_step']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
