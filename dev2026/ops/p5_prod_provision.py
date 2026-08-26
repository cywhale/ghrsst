"""dev2026 — P5 PRODUCTION root provisioning: create the roots, and nothing else.

`ops/p5_provision.py` cannot do this, and must not be made to. It refuses any stage root inside
the declared live root, and it declares its anchor with the same-filesystem waiver — correct for
a throwaway root on one filesystem, invalid for a deployment whose anchor is supposed to survive
the loss of the volume it witnesses. Adding a bypass there would make the isolation guarantee
optional, and an optional guarantee is not one. So production provisioning is a separate
operation with its own scope, its own approval and its own refusals.

## Why this does NOT publish a manifest

The first version of this module built a block from the delta and published it as generation 1.
That is wrong, and the reason is worth writing down:

* `api/app.py` treats a configured manifest as **the** authority — `if mf_dir:
  SegmentedCubeStore(mf_dir)` — and deliberately does not fall back to the monolith beside it,
  because "silently preferring the monolith would serve a generation nobody published".
* The production delta holds ~58 days. The monolith holds ~1331. A generation 1 whose only
  segment came from delta would therefore **remove roughly 1 273 days of history from the served
  view** the moment it was configured, under a `/healthz` that still said `ok`.

A valid generation 1 is a **`legacy_base` segment over the existing monolith** — the schema has
that kind (`store/block_manifest.py`: `kind must be legacy_base|block`), and
`p5_readiness.ALLOWED_LEGACY_PATHS` exists to allow exactly such a segment to point outside the
block root. Building one is real work — layout, metadata fingerprint and day digest derived from
the monolith on disk — and it is a separate reviewed step, not something to synthesize here.

## Why this does NOT initialize the WAL

`repair_wal.initialize_wal()` binds the log to a manifest authority, and
`manifest_authority()` calls `block_manifest.load_live()`, which raises when there is no
published manifest. So "roots plus WAL, without a manifest" is not reachable: WAL initialization
must follow a valid generation 1, and cannot precede it.

## What this does, therefore

Creates exactly two empty directories under the live root — `p5_blocks` and `p5_repairs` —
after verifying every precondition, and stops. They are inert: nothing reads them until a
manifest is published, and `GHRSST_BLOCK_MANIFEST_DIR` must stay unset until then.

## What it refuses, before writing anything

None of these is waivable by a flag: no production-scoped approval naming this live root and
this run; a live root that is missing or not a directory; lock paths that are not the canonical
production ones; P5 roots that already exist — neither adopted nor deleted; free space below the
**canonical** G18 floor, measured fresh and bound to the device holding the live root and
re-checked under the lock; a compaction already running; an anchor inside the live root, on the
live root's device, undeclared, or carrying the same-filesystem waiver.

The anchor is verified here even though the WAL is initialized later: an operator who cannot
produce a valid anchor should learn it before roots exist, not after.

## Interruption: recoverable, NOT atomic

This is not atomic and does not claim to be. A crash can leave a root on disk. What it
guarantees instead is that **whatever write did not happen, the record that survives names
every path that may exist** — so recovery is one call, not an investigation.

That works because the path set is knowable in advance: two fixed names under one root. The
ownership marker declares them as `planned_paths` **before anything is created**, and
`created_paths` grows as they really appear. The difference between the two lists is exactly
the interruption window, and `owned_paths()` covers it by taking the union. `contested_paths`
records a root that appeared under the lock — this run did not create it, so recovery must not
remove it, and refusing to start when a root already exists is what makes that inference sound.

Every boundary, and what a surviving marker says at each:

| interrupted at | on disk | marker says | recovery |
|---|---|---|---|
| before the stamp | nothing | no marker | nothing to do |
| after the stamp, before any root | marker only | planned=[A,B], created=[] | removes the marker |
| after creating A, before recording it | A | planned=[A,B], created=[] | removes A |
| after recording A, before B | A | planned=[A,B], created=[A] | removes A |
| after creating B, before recording it | A, B | planned=[A,B], created=[A] | removes both |
| after the completion write | A, B | complete: true | nothing to undo |

`_write_marker()` writes to a temporary file, fsyncs it, renames it, and then fsyncs the
**directory** — the rename has to reach disk too, or a crash can lose a marker whose contents
were durable.

**Operator recovery procedure.** Read `<live_root>/.p5_prod_provision_<run_id>.json`. If
`complete` is `true`, the roots are provisioned and there is nothing to undo. Otherwise the run
was interrupted: pass that JSON object to `rollback_production()`, which removes every owned
path that still exists and then the marker. If it raises `ProductionCleanupError`, its
`unrestored` attribute lists the exact paths it could not remove and the marker is deliberately
left in place — it is the record of what is still outstanding. Remove those paths by hand, then
re-run `rollback_production()` to clear the marker. Never delete the marker while any path it
owns is still there: that is the only thing making the leftover state identifiable.

## What it does not promise

Atomicity, as above. Cleanup is checked rather than best-effort — a path that cannot be removed
keeps the marker and raises `ProductionCleanupError` naming exactly what remains — but nothing
here makes the sequence a single filesystem transaction.
"""
from __future__ import annotations

import json
import math
import os
import shutil
from typing import Dict, List, Optional, Sequence

from ops import p5_legacy_base
from store import compaction_lock
from store import repair_wal

#: The label a production-provisioned root carries. Distinct from the staging label so a
#: readiness target cannot present one as the other.
PRODUCTION_LABEL = "P5_PRODUCTION_PROVISION"

#: The scope an approval must declare. A record approving something else — an assessor build,
#: a shadow run — is not an approval for writing into the live data root.
PRODUCTION_SCOPE = "p5_production_provisioning"

#: The ONLY names this creates, and the canonical lock names, all relative to the live root.
#: Computed from it, never taken from the caller: containment is only an argument if the path
#: cannot be pointed elsewhere.
MANIFEST_DIRNAME = "p5_blocks"
WAL_DIRNAME = "p5_repairs"
COMPACTION_LOCK_NAME = "p5_compaction.lock"
INGEST_LOCK_NAME = "mur_delta.ingest.lock"

#: Ownership marker, in the live root, named for the run.
MARKER_PREFIX = ".p5_prod_provision"


class ProductionProvisionRefused(Exception):
    """A production provisioning request that does not meet its preconditions."""


class ProductionApprovalRefused(ProductionProvisionRefused):
    """The approval is absent, malformed, or is not for this root and this run."""


class ProductionCleanupError(Exception):
    """Provisioning failed AND some created path could not be removed.

    Carries the live root and the exact paths still present, and chains the original
    provisioning failure as `__cause__`. The ownership marker is deliberately left in place:
    it is what a person needs to finish the cleanup."""

    def __init__(self, message: str, *, live_root: str, unrestored: Sequence[str]):
        super().__init__(message)
        self.live_root = live_root
        self.unrestored = list(unrestored)


def _inside(path: str, root: str) -> bool:
    """Is `path` inside `root`? By path components, never by string prefix."""
    path = os.path.realpath(path)
    root = os.path.realpath(root)
    return path == root or path.startswith(root + os.sep)


def _exact_str(value) -> bool:
    """A non-empty `str`, by exact type. `True` is an `int`; neither is a name."""
    return type(value) is str and bool(value.strip())


def marker_name(run_id: str) -> str:
    return f"{MARKER_PREFIX}_{run_id}.json"


def canonical_minimum_free_gib() -> float:
    """The G18 floor, from the ONE policy source readiness also reads.

    Imported at call time from `ops/p5s8_evidence.py`, where it is derived as
    `HARD_RESERVE + additional`. A copy here would be a second threshold to keep in step, and
    the copy is what goes stale — which is the whole reason `p5_readiness` reads it this way."""
    from ops import p5s8_evidence
    return p5s8_evidence.MINIMUM_FREE_GIB


def canonical_lock_paths(live_root: str) -> Dict[str, str]:
    """The two production lock paths, derived from the live root."""
    return {"compaction": os.path.join(live_root, COMPACTION_LOCK_NAME),
            "ingest": os.path.join(live_root, INGEST_LOCK_NAME)}


# ---------------------------------------------------------------- preconditions
def check_production_approval(approval, *, live_root: str, run_id: str) -> Dict:
    """The approval, validated. Returns it; raises `ProductionApprovalRefused`.

    Checked field by field against THIS root and THIS run rather than for mere presence. An
    approval for a different root is an approval for a different act, and a reusable one is a
    standing permission to write into production."""
    if type(approval) is not dict:
        raise ProductionApprovalRefused(
            f"no production approval was supplied (got {type(approval).__name__}); creating P5 "
            f"roots in the live data root is a production change and needs one")
    for field in ("scope", "approved_by", "approved_utc", "live_root", "run_id"):
        if field not in approval:
            raise ProductionApprovalRefused(
                f"the production approval records no {field}; an approval missing that names "
                f"nothing checkable")
        if not _exact_str(approval[field]):
            raise ProductionApprovalRefused(
                f"the production approval's {field} is {approval[field]!r}, which is not a "
                f"non-empty string")
    if approval["scope"] != PRODUCTION_SCOPE:
        raise ProductionApprovalRefused(
            f"the approval declares scope {approval['scope']!r}; this operation needs "
            f"{PRODUCTION_SCOPE!r}. An approval for one act is not an approval for another")
    if os.path.realpath(approval["live_root"]) != os.path.realpath(live_root):
        raise ProductionApprovalRefused(
            f"the approval is for live root {approval['live_root']!r}, not {live_root!r}. The "
            f"root being written to is the thing being approved")
    if approval["run_id"] != run_id:
        raise ProductionApprovalRefused(
            f"the approval is for run {approval['run_id']!r}, not {run_id!r}. A re-usable "
            f"approval is a standing permission to write into production")
    return dict(approval)


def check_lock_paths(*, live_root: str, compaction_lock_path: str,
                     ingest_lock_path: str) -> Dict[str, str]:
    """Both supplied paths must BE the canonical production locks, by realpath.

    They are parameters so an operator states which locks are being taken and can be refused
    when that is wrong — not so the locks can be moved. Compared through `realpath`, so a
    symlink pointing at the canonical file is accepted and a symlink pointing away is not:
    string equality would accept an alias that locks a different inode, which is the one
    two-writer state the compaction posture cannot survive."""
    canonical = canonical_lock_paths(live_root)
    supplied = {"compaction": compaction_lock_path, "ingest": ingest_lock_path}
    for role, path in supplied.items():
        if not _exact_str(path):
            raise ProductionProvisionRefused(
                f"no {role} lock path was supplied; the lock a production operation takes is "
                f"not left implicit")
        want = canonical[role]
        if os.path.realpath(path) != os.path.realpath(want):
            raise ProductionProvisionRefused(
                f"the {role} lock path {path!r} is not the canonical production lock "
                f"{want!r} (compared by realpath, so an alias to it would be accepted). "
                f"Taking a different lock excludes nobody")
    return canonical


def check_capacity_floor(min_free_gib) -> float:
    """The caller must state the floor, and it must be the canonical one.

    A floor the caller can lower is not a floor. This takes the value so the request records
    which policy it believed it was operating under, and refuses anything that is not exactly
    the canonical figure — including the shapes that compare equal or slip past a naive
    comparison: `True` is an `int`, `NaN` fails every comparison, and infinities are not
    thresholds."""
    canonical = canonical_minimum_free_gib()
    if type(min_free_gib) is bool:
        raise ProductionProvisionRefused(
            f"min_free_gib must be a number, got bool {min_free_gib!r}; True is an int and "
            f"would compare as 1 GiB")
    if not isinstance(min_free_gib, (int, float)):
        raise ProductionProvisionRefused(
            f"min_free_gib must be a number, got {type(min_free_gib).__name__}")
    if isinstance(min_free_gib, float) and (math.isnan(min_free_gib)
                                            or math.isinf(min_free_gib)):
        raise ProductionProvisionRefused(
            f"min_free_gib is {min_free_gib!r}, which is not a threshold: NaN fails every "
            f"comparison and an infinity is not a quantity of disk")
    if float(min_free_gib) != float(canonical):
        raise ProductionProvisionRefused(
            f"min_free_gib is {min_free_gib!r}, not the canonical G18 floor {canonical!r}. "
            f"The floor is policy, read from ops/p5s8_evidence.py; a caller-supplied one is "
            f"a bypass with an extra step")
    return float(canonical)


def measure_capacity(live_root: str, *, min_free_gib: float) -> Dict:
    """Free space and device, measured NOW against the root about to be written."""
    floor = check_capacity_floor(min_free_gib)
    st = os.statvfs(live_root)
    free_gib = st.f_bavail * st.f_frsize / (1024 ** 3)
    binding = {"data_root": os.path.realpath(live_root),
               "filesystem_dev": os.stat(live_root).st_dev,
               "free_now_gib": round(free_gib, 1),
               "min_free_gib": floor,
               "canonical_min_free_gib": floor,
               "inodes_free": st.f_favail}
    if free_gib < floor:
        raise ProductionProvisionRefused(
            f"free space on {live_root!r} is {free_gib:.1f} GiB, below the canonical G18 floor "
            f"of {floor:.1f} GiB. Provisioning roots on a volume that cannot hold the build "
            f"they exist for is a partial state waiting to happen")
    return binding


def check_anchor_domain(anchor_root: str, live_root: str) -> Dict:
    """The anchor, validated against the live root. Returns what was verified.

    `resolve_anchor_root()` accepts an `allow_same_filesystem=True` declaration as an auditable
    waiver. That is right for a throwaway staging root and wrong here: the anchor is the only
    thing that can notice the live volume being rolled back underneath the WAL, and one that
    shares the volume is restored with it. Production refuses the waiver outright, before any
    write, rather than discovering it later."""
    if not _exact_str(anchor_root):
        raise ProductionProvisionRefused(
            "no anchor_root was supplied. Production provisioning will not choose one: the "
            "anchor is a statement about storage nobody in this process can verify")
    if not os.path.isdir(anchor_root):
        raise ProductionProvisionRefused(
            f"anchor_root {anchor_root!r} is not an existing directory. It must be storage ops "
            f"have already provisioned, not something this run creates for itself")
    if _inside(anchor_root, live_root):
        raise ProductionProvisionRefused(
            f"anchor_root {anchor_root!r} is inside the live root {live_root!r}. A witness "
            f"stored in the domain it witnesses is lost with it")
    doc = repair_wal._anchor_domain_doc(os.path.realpath(anchor_root))
    if doc is None:
        raise ProductionProvisionRefused(
            f"anchor_root {anchor_root!r} declares no rollback domain. Run "
            f"declare_anchor_domain() against storage ops have confirmed is outside this "
            f"host's snapshot scope")
    if doc.get("allow_same_filesystem") is not False:
        raise ProductionProvisionRefused(
            f"anchor_root {anchor_root!r} is declared with allow_same_filesystem="
            f"{doc.get('allow_same_filesystem')!r}. That waiver is for an isolated staging "
            f"root; a deployment anchor must be on storage that survives the live volume")
    try:
        anchor_dev = os.stat(anchor_root).st_dev
        live_dev = os.stat(live_root).st_dev
    except OSError as exc:
        raise ProductionProvisionRefused(
            f"cannot compare the anchor and live devices ({exc}). An unanswerable question is "
            f"not a passing answer") from exc
    if anchor_dev == live_dev:
        raise ProductionProvisionRefused(
            f"anchor_root {anchor_root!r} is on the same device as the live root "
            f"({anchor_dev}). A different st_dev still would not prove a different backup "
            f"domain, but the same one rules it out")
    return {"anchor_root": os.path.realpath(anchor_root),
            "domain_id": doc.get("domain_id"), "anchor_dev": anchor_dev,
            "live_dev": live_dev, "allow_same_filesystem": False}


def _refuse_existing_roots(manifest_root: str, wal_root: str) -> None:
    """Never adopt, never delete. Both are the same refusal from opposite directions."""
    for path in (manifest_root, wal_root):
        if os.path.exists(path):
            raise ProductionProvisionRefused(
                f"refusing to provision: {path!r} already exists. This operation will not "
                f"adopt it — rollback removes what this run created, and adopting would hand "
                f"that authority over data it did not create — and it will not delete it, "
                f"because deleting production state is not a provisioning step")


# ---------------------------------------------------------------- the operation
def provision_production(*, live_root: str, anchor_root: str, approval: Dict,
                         monolith_path: str,
                         compaction_lock_path: str, ingest_lock_path: str,
                         run_id: str, operator: str, at_utc: str,
                         min_free_gib: float,
                         cleanup_on_failure: bool = True) -> Dict:
    """Create the P5 roots, publish generation 1 over the monolith, initialize the WAL.

    `monolith_path` is REQUIRED, not optional. A provisioner that might or might not publish is
    two operations wearing one name, and the one that stops after creating directories leaves a
    manifest root nothing can bind a WAL to.

    Ordering follows §7.1b: the compaction lock is taken FIRST and held across everything; the
    ingest lock is taken ONLY across the publish, the moment the store's authority changes and
    the only moment ingest must be excluded from. The WAL is initialized after the publish and
    still under the compaction lock, because it binds to the authority that now exists.

    It does NOT switch `GHRSST_BLOCK_MANIFEST_DIR`. Provisioning the roots and pointing the
    service at them are different production changes, and the second is not this one's to
    make."""
    approval = check_production_approval(approval, live_root=live_root, run_id=run_id)
    if not os.path.isdir(live_root):
        raise ProductionProvisionRefused(
            f"live_root {live_root!r} is not an existing directory; production provisioning "
            f"does not create the data root it writes into")
    for name, value in (("run_id", run_id), ("operator", operator), ("at_utc", at_utc)):
        if not _exact_str(value):
            raise ProductionProvisionRefused(f"{name} must be a non-empty string")
    locks = check_lock_paths(live_root=live_root, compaction_lock_path=compaction_lock_path,
                             ingest_lock_path=ingest_lock_path)

    manifest_root = os.path.join(live_root, MANIFEST_DIRNAME)
    wal_root = os.path.join(live_root, WAL_DIRNAME)
    for path in (manifest_root, wal_root):
        if not _inside(path, live_root):
            raise ProductionProvisionRefused(
                f"{path!r} did not resolve inside {live_root!r}; refusing rather than writing "
                f"somewhere the containment check does not cover")
    _refuse_existing_roots(manifest_root, wal_root)
    if not os.path.isdir(monolith_path):
        raise ProductionProvisionRefused(
            f"monolith_path {monolith_path!r} is not an existing directory; generation 1 is a "
            f"legacy base over a store that must already be there")
    # There is deliberately NO "is the monolith inside the block root" check. For it to be, the
    # block root would have to exist -- the monolith must be an existing directory -- and
    # `_refuse_existing_roots` above has already refused that. A branch that cannot fire is not
    # a guard, and it would read like one.
    anchor = check_anchor_domain(anchor_root, live_root)
    capacity = measure_capacity(live_root, min_free_gib=min_free_gib)

    busy = compaction_lock.refuse_if_compaction_running(
        locks["compaction"], operation="p5 production provisioning")
    if busy:
        raise ProductionProvisionRefused(
            f"a compaction is running ({busy}); provisioning the roots underneath it would "
            f"change what it is building against")

    marker_path = os.path.join(live_root, marker_name(run_id))
    marker = f"{PRODUCTION_LABEL}:{run_id}:{at_utc}:{operator}"
    stamp = {"label": PRODUCTION_LABEL, "marker": marker, "run_id": run_id,
             "live_root": os.path.realpath(live_root), "manifest_root": manifest_root,
             "wal_root": wal_root, "anchor_root": anchor["anchor_root"],
             "monolith_path": os.path.realpath(monolith_path), "locks": locks, "provisioned_utc": at_utc, "operator": operator,
             "approved_by": approval["approved_by"], "approved_utc": approval["approved_utc"],
             "capacity_at_start": capacity, "anchor_verified": anchor,
             # PLANNED before anything exists, because "what may be on disk" cannot be learned
             # after a crash from a record written after the fact. The set is fixed and known
             # in advance -- these two names, under this root -- so declaring it up front makes
             # every surviving marker complete, whichever write was the one that did not
             # happen. `created_paths` still grows as they really appear, and the difference
             # between the two lists is exactly the crash window.
             "planned_paths": [manifest_root, wal_root],
             "created_paths": [], "contested_paths": [],
             "complete": False, "failed_step": None}

    # Ownership before the first thing worth owning. The marker is itself a write and can fail;
    # if it does, nothing has been created and there is nothing to own.
    _write_marker(marker_path, stamp)
    created: List[str] = []
    contested: List[str] = []
    published: Dict = {}
    try:
        _create_roots(live_root=live_root, manifest_root=manifest_root, wal_root=wal_root,
                      capacity=capacity, locks=locks, run_id=run_id, created=created,
                      contested=contested, marker_path=marker_path, stamp=stamp,
                      monolith_path=monolith_path, anchor=anchor, at_utc=at_utc,
                      operator=operator, published=published)
        # The completion write is INSIDE the recovery path. Outside it, a failure here left the
        # roots on disk under a marker still saying `created_paths: []` and `complete: false` --
        # a marker that authorized removing nothing while two directories existed.
        _write_marker(marker_path, {**stamp, "created_paths": list(created), "complete": True})
    except BaseException as failure:
        _recover(failure, marker_path=marker_path, stamp=stamp, created=created,
                 contested=contested, live_root=live_root,
                 cleanup_on_failure=cleanup_on_failure)
        raise

    return {
        "label": PRODUCTION_LABEL, "is_production_state": True,
        "run_id": run_id, "live_root": os.path.realpath(live_root),
        "manifest_root": manifest_root, "wal_root": wal_root,
        "anchor_root": anchor["anchor_root"], "anchor_domain_id": anchor["domain_id"],
        "created_paths": list(created), "marker": marker, "marker_path": marker_path,
        "monolith_path": os.path.realpath(monolith_path),
        "locks": {**locks, "order": ("compaction lock held across creation; ingest lock NOT "
                                     "taken -- §7.1b scopes it to the publish critical "
                                     "section and this operation publishes nothing")},
        "capacity_at_start": capacity, "anchor_verified": anchor,
        "approved_by": approval["approved_by"], "approved_utc": approval["approved_utc"],
        "provisioned_utc": at_utc, "operator": operator,
        "manifest_published": True, "wal_initialized": True,
        "generation": published.get("generation"),
        "generation_id": published.get("generation_id"),
        "legacy_base": dict(published),
        "complete": True, "failed_step": None,
        "note": ("P5 roots, generation 1 over the existing monolith, and a WAL bound to it. "
                 "GHRSST_BLOCK_MANIFEST_DIR is NOT switched: pointing the service at these "
                 "roots is a separate production change. The deployment must also name the "
                 "monolith in GHRSST_ALLOWED_LEGACY_PATHS before this manifest can be served "
                 "-- the manifest does not authorize its own external path."),
    }


def _create_roots(*, live_root, manifest_root, wal_root, capacity, locks, run_id, created,
                  contested, marker_path, stamp, monolith_path, anchor, at_utc, operator,
                  published) -> None:
    """Roots, generation 1 and the WAL, under §7.1b ordering.

        flock(p5_compaction.lock, LOCK_EX)  ->  create roots
           ->  acquire ingest lock  ->  assert compaction lock still held
           ->  publish generation 1  ->  release ingest lock
           ->  initialize the WAL  ->  release compaction lock
    """
    holder = f"p5_prod_provision/{run_id}"
    with compaction_lock.CompactionLock(locks["compaction"], holder=holder) as clock:
        # Re-measured INSIDE the lock. A binding taken before the lock is a binding to a
        # different moment than the one being written in.
        now_dev = os.stat(live_root).st_dev
        if now_dev != capacity["filesystem_dev"]:
            raise ProductionProvisionRefused(
                f"the live root moved device between preflight ({capacity['filesystem_dev']}) "
                f"and the lock ({now_dev}); the capacity reading is about a different volume")
        st = os.statvfs(live_root)
        free_gib = st.f_bavail * st.f_frsize / (1024 ** 3)
        if free_gib < capacity["min_free_gib"]:
            raise ProductionProvisionRefused(
                f"free space fell to {free_gib:.1f} GiB under the lock, below the canonical "
                f"floor {capacity['min_free_gib']:.1f} GiB")
        for path in (manifest_root, wal_root):
            try:
                os.makedirs(path)                   # no exist_ok: refuse a racing creator
            except FileExistsError:
                # It appeared between the preflight and here, so this run did not create it and
                # has no authority to remove it. Recorded so recovery can tell "ours, created
                # just before a marker write failed" from "someone else's".
                contested.append(path)
                _write_marker(marker_path, {**stamp, "created_paths": list(created),
                                            "contested_paths": list(contested)})
                raise ProductionProvisionRefused(
                    f"{path!r} appeared while the lock was held; this run did not create it "
                    f"and will not adopt or remove it")
            created.append(path)
            # The marker is brought up to date the moment the path exists, so it is never
            # behind the filesystem. A crash between these two writes leaves a marker naming
            # one root while two exist -- which is why the recovery path uses the in-memory
            # list as well.
            _write_marker(marker_path, {**stamp, "created_paths": list(created)})

        # Everything expensive happens HERE, under the compaction lock and OUTSIDE the ingest
        # lock. `content_ledger()` walks every file in the monolith; on ~2 TB that is minutes
        # of stat calls, and it was running inside the ingest lock -- so the daily-append cron
        # would have blocked behind a scan that has nothing to do with it.
        prepared = p5_legacy_base.prepare_generation_one(
            monolith_path=monolith_path, manifest_root=manifest_root,
            at_utc=at_utc, operator=operator)

        # The publish critical section: a final cheap validation and the authority change, and
        # nothing else. This is the only moment ingest must be excluded from.
        with compaction_lock.CompactionLock(locks["ingest"],
                                            holder=f"{holder}/publish") as ingest:
            clock.assert_still_held()
            published.update(p5_legacy_base.publish_prepared(prepared))
        # The `with` block above is what releases the ingest lock, and it ends here -- before
        # the WAL is initialized. An explicit `release()` as the block's last statement was
        # redundant: removing it changed nothing, which is why a mutation deleting it survived.

        # After the publish, still under the compaction lock: `initialize_wal` binds to the
        # authority that now exists and reads it through `load_live()`.
        clock.assert_still_held()
        repair_wal.initialize_wal(wal_root, manifest_root=manifest_root, at_utc=at_utc,
                                  operator=operator, anchor_root=anchor["anchor_root"])
        clock.assert_still_held()


def _recover(failure: BaseException, *, marker_path, stamp, created, contested, live_root,
             cleanup_on_failure: bool) -> None:
    """Record the failure accurately, then clean up if asked. Never silently."""
    partial = {**stamp, "created_paths": list(created), "contested_paths": list(contested),
               "failed_step": repr(failure)}
    try:
        _write_marker(marker_path, partial)
    except Exception:                                         # noqa: BLE001
        # The marker cannot be written, so nothing on disk can describe what exists. Remove
        # what was created, checked, and say so if that fails too.
        _discard(owned_paths(stamp, created=created, contested=contested), marker_path,
                 live_root=live_root, cause=failure)
        return
    if not cleanup_on_failure:
        return
    rollback_production(partial, cause=failure)


def owned_paths(record: Dict, *, created: Sequence[str] = (),
                contested: Sequence[str] = ()) -> List[str]:
    """Every path this run may have put on disk, from a record plus anything held in memory.

    The union of what was PLANNED and what was observed created, minus anything contested. The
    planned set is what makes this complete after a crash: a run interrupted between creating a
    root and recording it leaves a marker whose `created_paths` is one short, and the planned
    list covers exactly that gap. Contested paths are excluded because this run did not create
    them -- refusing to start when a root already exists is what makes the rest of that
    inference sound."""
    out: List[str] = []
    skip = {os.path.realpath(p) for p in
            list(record.get("contested_paths") or []) + list(contested)}
    for path in (list(record.get("planned_paths") or [])
                 + list(record.get("created_paths") or []) + list(created)):
        if os.path.realpath(path) in skip:
            continue
        if path not in out:
            out.append(path)
    return out


def _remove_checked(paths: Sequence[str]) -> List[str]:
    """Remove each path and report the ones that are still there.

    `ignore_errors=True` reported success for a directory it had not removed, so a failed
    cleanup read as a clean one. Every path is attempted — one failure must not skip the rest —
    and the result is what `os.path.exists` says afterwards, not what `rmtree` returned."""
    unrestored = []
    for path in reversed(list(paths)):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            continue
        except OSError:
            pass                                   # verified below, not trusted here
        if os.path.exists(path):
            unrestored.append(path)
    return sorted(unrestored)


def _discard(created: Sequence[str], marker_path: str, *, live_root: str,
             cause: Optional[BaseException] = None) -> None:
    """Remove what was created when the marker itself cannot be kept — checked, not silent.

    The marker is removed only after every created path is verified absent; while anything
    remains it is the only record of what to finish, so it stays."""
    unrestored = _remove_checked(created)
    if unrestored:
        raise ProductionCleanupError(
            f"provisioning failed and its marker could not be written; these created paths "
            f"could not be removed either and are still present under {live_root!r}: "
            f"{unrestored}. Remove them by hand once the cause is understood",
            live_root=live_root, unrestored=unrestored) from cause
    try:
        os.remove(marker_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise ProductionCleanupError(
            f"every created path was removed, but the ownership marker {marker_path!r} could "
            f"not be ({exc}). Nothing is left to clean up under {live_root!r}; delete the "
            f"marker by hand",
            live_root=live_root, unrestored=[marker_path]) from cause


def rollback_production(provisioned: Dict, *, cause: Optional[BaseException] = None) -> None:
    """Remove exactly what this run created under the live root, and nothing else.

    The only call here that deletes, so the most suspicious of its argument: it removes the
    paths the marker RECORDS creating — not the live root, not `p5_blocks` because of its name,
    and never a path this run did not make. Fail-closed: if any recorded path is still there
    afterwards, the marker STAYS and `ProductionCleanupError` names what remains."""
    live_root = provisioned.get("live_root")
    run_id = provisioned.get("run_id")
    if not (_exact_str(live_root) and _exact_str(run_id)):
        raise ProductionProvisionRefused(
            "rollback needs the live_root and run_id the marker was written for")
    marker_path = os.path.join(live_root, marker_name(run_id))
    if not os.path.isfile(marker_path):
        raise ProductionProvisionRefused(
            f"refusing to remove anything under {live_root!r}: there is no "
            f"{marker_name(run_id)}, so this run did not create what is there")
    with open(marker_path) as fh:
        recorded = json.load(fh)
    if recorded.get("marker") != provisioned.get("marker"):
        raise ProductionProvisionRefused(
            f"refusing to roll back: the marker on disk ({recorded.get('marker')!r}) is not "
            f"the one being rolled back ({provisioned.get('marker')!r})")
    if os.path.realpath(recorded.get("live_root") or "") != os.path.realpath(live_root):
        raise ProductionProvisionRefused(
            f"refusing to roll back: the marker records live_root "
            f"{recorded.get('live_root')!r}, not {live_root!r}")
    # The union of what the marker records and what the caller holds: a crash between creating
    # a root and re-writing the marker leaves the marker one behind, and the caller's list is
    # the more complete of the two.
    recorded_paths = owned_paths(recorded, created=provisioned.get("created_paths") or (),
                                 contested=provisioned.get("contested_paths") or ())
    for path in recorded_paths:
        if not _inside(path, live_root):
            raise ProductionProvisionRefused(
                f"refusing to remove {path!r}: the marker records it as created but it is not "
                f"inside {live_root!r}")
        if os.path.realpath(path) == os.path.realpath(live_root):
            raise ProductionProvisionRefused(
                f"refusing to remove the live root itself ({path!r})")
    unrestored = _remove_checked(recorded_paths)
    if unrestored:
        raise ProductionCleanupError(
            f"rollback could not remove {unrestored} under {live_root!r}. The ownership marker "
            f"{marker_path!r} is left in place: it is the record of what still has to be "
            f"finished, and removing it would leave the state unowned",
            live_root=live_root, unrestored=unrestored) from cause
    os.remove(marker_path)


def _write_marker(path: str, payload: Dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    # The rename itself has to reach the disk, or a crash can lose a marker whose contents were
    # fsynced. The whole recovery protocol rests on the marker being there afterwards.
    fd = os.open(os.path.dirname(os.path.abspath(path)) or ".", os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
