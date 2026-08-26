"""dev2026 — the one-shot compaction: build a block, publish it, verify, delete NOTHING.

This is the operation `ops/p5_oneshot_exception.OP_COMPACTION` names. It exists to make the
space in the old daily/base store *releasable*; it does not release it, and the first
implementation deliberately removes nothing at all.

## What it does, in order

    verify the DEPLOYMENT IDENTITY                  before any lock, before any scan
    refuse if a generation is QUARANTINED
    flock(p5_compaction.lock, LOCK_EX)              outermost, held across everything
      -> writer-fence probe BEFORE anything is read
      -> flock(mur_delta.ingest.lock, LOCK_EX)      BEFORE the scan, not before the publish
           -> measure the monolith HERE and mint the single-use compaction capability
           -> inventory every path that must not shrink
           -> build_block(...)                      the expensive scan
           -> re-probe the fence; it must not have moved
           -> re-measure the monolith; it must not have moved either
           -> plan generation N+1 in memory (writes nothing)
           -> PRE-PUBLISH gates: a bound point-series parity ARTIFACT against the
              per-day oracle (first and last days included), source-map
              completeness, and base/delta disjointness on the PLANNED manifest.
              A failure here refuses with NOTHING published.
           -> execute_publication (borrowing both locks)
      <- ingest lock released
      -> POST-PUBLISH gates: manifest checksum/generation, WAL and anchor binding,
         a bound API parity ARTIFACT, freshly stamped capacity, second inventory
      -> post_compaction_problem(evidence, expected=<this run's own state>)
      -> a finalization clearance, if and only if every gate passed;
         otherwise QUARANTINE the generation and refuse

## The identity, and why an approval is not enough

A production approval names a live root and a run. It says nothing about whether the code about
to publish a served authority was ever reviewed — and this operation publishes one. So an
identity artifact is **required**: an approved `deploy_sha`, `HEAD` at that commit, a clean
worktree, and module fingerprints compared against the blobs **git holds at that commit**, exact
in both directions. Checked before any lock is taken, so an operator with a dirty checkout learns
it before a multi-minute scan rather than after.

There is deliberately no `expected_module_fingerprints` parameter. One existed, was accepted, and
was never read — an optional argument that does nothing is worse than an absent one, because a
reader passing it believes a check is happening.

## Parity evidence is executed and bound

The producers are injected, because they must read the deployment's own stores through the
deployment's own reader. What they RETURN is not taken on trust: each result is an artifact
carrying host, run, `deploy_sha`, the candidate generation and manifest checksum, the source-map
digest, an execution time, real sample and mismatch counts, and written provenance — and its
`producer_digest` must belong to a module the identity artifact fingerprinted. A callable
returning `{"mismatches": 0}` is refused: reviewed code has a digest git holds, and a callable
defined in a shell does not. Zero mismatches out of zero samples is an absent check, not a
passing one.

## A failed generation is quarantined, not left lying about

Post-publish gates run after generation N+1 exists. Raising alone would leave it live and
unclassified — the next run would read the live manifest, plan N+2 on top of it, and the failure
would sit underneath a generation nobody questioned. So `.p5_generation_quarantine.json` is
written durably **before** the raise, and every later publish, switch and finalization refuses
while it exists, including one holding a valid clearance and a human approval.

Not an automatic rollback, deliberately: restoring the predecessor is itself an authority change,
and making one in the failure path on the strength of a gate this run could not satisfy is a
second unreviewed change chasing the first. The predecessor archive is immutable (§9.3) and
intact, so a person can still roll back — what the record guarantees is that nobody proceeds
*without* deciding.

## What it does NOT do, and why that is the design rather than an omission

**It deletes nothing.** No prune, no swap, no daily removal, no `GHRSST_BLOCK_MANIFEST_DIR`
switch. Publishing a new generation is reversible in a way that removing the data it was built
from is not, and the two decisions are different: "the new generation is trustworthy" is
established by the gates below, while "the old data should go" is a person's judgement about
retention and recovery that no amount of parity checking settles.

So the gates gate a **clearance**, not a deletion. `p5_oneshot_exception.refuse_destructive_step`
is the contract a future finalization must satisfy, and it requires the clearance *and* a
separate human approval. Nothing in this tree implements a destructive step.

**It does not switch the service.** The served view is unchanged at the instant of publication —
the manifest does not describe delta, and delta still wins for the folded days (§4.0). Pointing
the service at the new generation is a separate production change with its own approval.

## The fence, and what it does not cover

`flock` is advisory. The ingest lock excludes the ingest path because the ingest path takes it;
it excludes nothing that does not. That is why the operation requires a writer fence —
writer-producing services stopped, append cron disabled — probed **three times**: before
anything is read, after the build, and after the publish. Each probe is validated and the
service sets are compared. A one-time writer count is an observation about an instant, and the
interval this protects is minutes long.

The shipped default probe **refuses**. Whether `pm2` stopped a service is a fact about the
deployment host that nothing in this process can observe, and a default returning a plausible
posture would make every local test pass while proving nothing about any deployment.

Two things the fence does not establish, stated rather than left to be inferred:

* **Probing is sampling.** A service that stopped and restarted entirely between two probes is
  invisible to all of them. Proving a posture held continuously needs something watching
  continuously, which is not available here.
* **An arbitrary writer obeys neither the lock nor the service posture** — a shell, a stray
  script, a service someone restarts by hand.

What catches both after the fact is the monolith re-measurement before the publish and the
parity gates: detection, not prevention. That is why the read-only mount remains the stronger
route and why this stays a one-shot exception rather than a way of working.
"""
from __future__ import annotations

import os
from typing import Callable, Dict, List, Optional, Sequence

from ops import p5_oneshot_exception as ex
from ops import p5_prod_provision as prod
from store import block_manifest as bm
from store import compaction_lock
from store import repair_wal

#: The scope a production approval must declare for THIS operation. Distinct from the
#: provisioning scope: an approval for creating roots is not an approval for publishing a
#: compaction generation over them.
COMPACTION_SCOPE = "p5_oneshot_compaction"

#: Paths whose contents must not shrink across the run. The inventory over these is what
#: `nothing_deleted` is re-derived from -- see `p5_oneshot_exception.nothing_deleted_problem`.
PROTECTED_ROLES = ("monolith", "delta", "daily")


class OneShotCompactionRefused(Exception):
    """A precondition, a gate, or the fence did not hold. Nothing destructive ever ran."""


def _exact_str(value) -> bool:
    return type(value) is str and bool(value.strip())


# ---------------------------------------------------------------- canonical paths
def canonical_roots(live_root: str) -> Dict[str, str]:
    """The manifest and WAL roots, COMPUTED from the live root rather than taken from a caller.

    The approval names a live root. Accepting a manifest root beside it meant the approval
    authorized writing wherever the caller pointed -- an approval for `$G` would publish a
    generation into `/tmp/anywhere` and the record would still read as approved. Containment is
    only an argument if the path cannot be pointed elsewhere."""
    return {"manifest_root": os.path.join(live_root, prod.MANIFEST_DIRNAME),
            "wal_root": os.path.join(live_root, prod.WAL_DIRNAME)}


def _inside(path: str, root: str) -> bool:
    real_root = os.path.realpath(root)
    real = os.path.realpath(path)
    return real == real_root or real.startswith(real_root + os.sep)


def _bound_source_problem(role: str, path: Optional[str], *, declared, live_root: str):
    """An external source path must be the one the approved preflight named.

    `delta_path` and `daily_root` are read during the build, so a caller free to name them could
    have the block folded from a store nobody approved -- and the manifest would record the
    substituted path as provenance, which is worse than no record. So each is checked against
    the plan the preflight carries.

    Symlinks are refused rather than resolved-and-accepted: `path` and `realpath` differing
    means the name that was approved and the bytes that get read are two different questions,
    and a link can be re-pointed between the approval and the run."""
    if path is None:
        return None
    if not _exact_str(path):
        return f"{role} is {path!r}, which is not a path"
    if not os.path.isabs(path):
        return (f"{role} {path!r} is not absolute; what it names would depend on the directory "
                f"the operation happened to start in")
    if not os.path.exists(path):
        return f"{role} {path!r} does not exist"
    # The PATH ITSELF, not its ancestors. `/var` is a symlink to `/private/var` on macOS, so
    # comparing realpath against abspath refuses every temp path and says nothing about the
    # store. What matters is a link AT the name that was approved: that is the one that can be
    # re-pointed between the approval and the run without the record changing.
    if os.path.islink(path.rstrip(os.sep)):
        return (f"{role} {path!r} is a symlink to {os.readlink(path.rstrip(os.sep))!r}; the "
                f"name that was approved and the bytes that get read are then two different "
                f"questions, and the link can be re-pointed between them")
    if declared is None:
        return (f"{role} was supplied but the approved preflight plan names no {role}; an "
                f"unapproved source would be folded into the block and recorded as provenance")
    if os.path.realpath(declared) != os.path.realpath(path):
        return (f"{role} {path!r} is not the {declared!r} the approved preflight names")
    return None


def check_operation_paths(*, live_root: str, manifest_root: str, wal_root: str,
                          block_out_path: str, artifacts_dir: str, anchor_root: str,
                          monolith_path: str, delta_path, daily_root, pre: Dict) -> Dict:
    """Bind every path this operation writes or reads to something that approved it.

    Returns the canonical roots, or raises. Grouped here rather than scattered through the
    operation so the whole path surface is visible in one place -- it was not, and the gap was
    that an approval for one live root authorized a manifest root anywhere on the filesystem."""
    if not os.path.isdir(live_root):
        raise OneShotCompactionRefused(
            f"live_root {live_root!r} is not an existing directory")
    canonical = canonical_roots(live_root)
    for role, want in canonical.items():
        got = {"manifest_root": manifest_root, "wal_root": wal_root}[role]
        if os.path.realpath(got) != os.path.realpath(want):
            raise OneShotCompactionRefused(
                f"{role} {got!r} is not the canonical {want!r} for the approved live root. The "
                f"approval names the live root; the roots under it are computed, not chosen, "
                f"or the approval would authorize writing wherever the caller pointed")

    # The block and the build artifacts are written by this run, so they must land inside the
    # roots the approval covers.
    if not _inside(block_out_path, canonical["manifest_root"]):
        raise OneShotCompactionRefused(
            f"block_out_path {block_out_path!r} is not inside the manifest root "
            f"{canonical['manifest_root']!r}; a block written outside the approved root is one "
            f"the approval does not cover and the cleanup path does not know about")
    if not _exact_str(artifacts_dir) or not os.path.isabs(artifacts_dir):
        raise OneShotCompactionRefused(
            f"artifacts_dir {artifacts_dir!r} must be an absolute path")

    # The anchor must be OUTSIDE the live root and on another device -- it witnesses the volume
    # it is meant to survive. `check_anchor_domain` is the existing authority for that and is
    # reused rather than reimplemented.
    if _inside(anchor_root, live_root):
        raise OneShotCompactionRefused(
            f"anchor_root {anchor_root!r} is inside the live root; an anchor that dies with the "
            f"volume it witnesses witnesses nothing")
    anchor = prod.check_anchor_domain(anchor_root, live_root)

    # The monolith and the external sources must be the ones the approved preflight named.
    plan = (pre or {}).get("plan") or {}
    declared_paths = plan.get("paths") or {}
    problem = _bound_source_problem(
        "monolith_path", monolith_path,
        declared=declared_paths.get("monolith", pre.get("monolith_state", {})
                                    .get("monolith_realpath")),
        live_root=live_root)
    if problem:
        raise OneShotCompactionRefused(problem)
    for role, path in (("delta_path", delta_path), ("daily_root", daily_root)):
        problem = _bound_source_problem(role, path, declared=declared_paths.get(role),
                                        live_root=live_root)
        if problem:
            raise OneShotCompactionRefused(problem)
    return {**canonical, "anchor": anchor}


# ---------------------------------------------------------------- the quarantine state
#: Written into the manifest root when a generation is published and then FAILS its
#: post-publish gates. Named with a leading dot so it sits beside the manifest rather than
#: inside the generation archives, which are immutable (§9.3).
QUARANTINE_NAME = ".p5_generation_quarantine.json"


class GenerationQuarantined(OneShotCompactionRefused):
    """A published generation failed its gates and has not been reconciled."""


class QuarantineWriteFailed(OneShotCompactionRefused):
    """A generation is published, unverified, and could NOT be quarantined.

    The worst state this operation can reach, and it gets its own type so nothing can report it
    as an ordinary quarantine. A quarantine record is what makes every later publish, switch and
    finalization refuse; without one on disk, the next run sees a normal live manifest and has no
    way to know. That needs a person, now.

    Carries `generation`, `manifest_root` and `predecessor_archive` as attributes so an operator
    -- or a supervising script -- can act on them without parsing the message."""

    def __init__(self, message, *, generation, manifest_root, predecessor_archive):
        super().__init__(message)
        self.generation = generation
        self.manifest_root = manifest_root
        self.predecessor_archive = predecessor_archive


def quarantine_path(manifest_root: str) -> str:
    return os.path.join(manifest_root, QUARANTINE_NAME)


def read_quarantine(manifest_root: str) -> Optional[Dict]:
    """The quarantine record, or `None`. Unreadable is NOT the same as absent."""
    path = quarantine_path(manifest_root)
    if not os.path.isfile(path):
        return None
    import json
    with open(path) as fh:
        return json.load(fh)


def quarantine_problem(manifest_root: str) -> Optional[str]:
    """The refusal every later operation owes an unreconciled generation, or `None`.

    A post-publish gate failure leaves generation N+1 on disk and unverified. Doing nothing
    about it is the dangerous option: the next run reads the live manifest, sees generation N+1,
    plans N+2 on top of it, and the failure is now underneath a generation nobody questioned.
    So the state is written down, and everything that could build on it refuses until a person
    reconciles it."""
    try:
        record = read_quarantine(manifest_root)
    except Exception as exc:                                        # noqa: BLE001
        return (f"the quarantine record at {quarantine_path(manifest_root)!r} could not be "
                f"read ({exc!r}). An unreadable quarantine is not an absent one: refusing")
    if record is None:
        return None
    return (f"generation {record.get('generation')} in {manifest_root!r} is QUARANTINED: "
            f"{record.get('reasons')}. It was published by run {record.get('run_id')!r} and "
            f"then failed its post-publish gates, so what the manifest points at has not been "
            f"verified. The predecessor archive {record.get('predecessor_archive')!r} is intact "
            f"and is what a rollback would restore. Nothing may publish, switch or finalize "
            f"over this state until a person reconciles it and removes "
            f"{quarantine_path(manifest_root)!r}")


def _quarantine_or_fail_closed(manifest_root, *, generation, predecessor_generation, run_id,
                               host, operator, reasons, block_path, result, now, cause):
    """Record the unverified generation and raise. Never returns.

    Two outcomes and they are different facts, so they are different exceptions:

    * the record is written -> `GenerationQuarantined`, and every later publish, switch and
      finalization now refuses;
    * the record CANNOT be written -> `QuarantineWriteFailed`, which says so explicitly and
      names the published-but-unquarantined generation. Reporting a normal quarantine there
      would be claiming a guarantee that does not exist on disk: the next run would find a
      clean manifest root and proceed."""
    predecessor_archive = (None if predecessor_generation is None
                           else bm.archive_name(int(predecessor_generation)))
    try:
        quarantine = quarantine_generation(
            manifest_root, generation=generation,
            predecessor_generation=predecessor_generation,
            run_id=run_id, host=host, operator=operator,
            reasons=reasons, block_path=block_path, now=now)
    except BaseException as write_failure:                          # noqa: BLE001
        raise QuarantineWriteFailed(
            f"MANUAL INTERVENTION REQUIRED. Generation {generation} is PUBLISHED, was NOT "
            f"verified ({reasons}), and could NOT be quarantined: writing "
            f"{quarantine_path(manifest_root)!r} failed with {write_failure!r}. Nothing was "
            f"deleted and no service was switched, but there is no record on disk to make the "
            f"next run refuse -- it would see a normal live manifest. The predecessor archive "
            f"{predecessor_archive!r} is immutable and should still be present. Reconcile by "
            f"hand before any further publish, switch or finalization.",
            generation=generation, manifest_root=manifest_root,
            predecessor_archive=predecessor_archive) from (cause or write_failure)
    if result is not None:
        result["quarantine"] = quarantine
    raise GenerationQuarantined(
        f"the post-compaction step did not pass: {reasons}. Generation {generation} is "
        f"published, NOT verified, and now QUARANTINED: nothing was deleted, no service was "
        f"switched, and no finalization clearance was issued. The predecessor archive "
        f"{quarantine['predecessor_archive']!r} is intact. Every publish, switch and "
        f"finalization refuses until a person reconciles this and removes "
        f"{quarantine_path(manifest_root)!r}") from cause


def quarantine_generation(manifest_root: str, *, generation: int, predecessor_generation,
                          run_id: str, host: str, operator: str, reasons,
                          block_path: str, now=None) -> Dict:
    """Record the unverified generation durably, so nothing can build on it unnoticed.

    Deliberately NOT a rollback. Restoring the predecessor is itself a manifest publication --
    it changes what the service resolves -- and doing that automatically, in the failure path,
    on the strength of a gate this run could not satisfy, is a second unreviewed authority
    change chasing the first. The archive for the predecessor is immutable and still on disk, so
    a rollback remains available to a person who decides on it; what this guarantees is that
    nobody proceeds *without* deciding."""
    import json
    record = {
        "scope": "p5_generation_quarantine",
        "generation": int(generation),
        "predecessor_generation": (None if predecessor_generation is None
                                   else int(predecessor_generation)),
        "predecessor_archive": (None if predecessor_generation is None
                                else bm.archive_name(int(predecessor_generation))),
        "run_id": run_id, "host": host, "operator": operator,
        "block_path": block_path,
        "reasons": reasons,
        "quarantined_utc": ex._now(now).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nothing_deleted": True,
        "service_switched": False,
        "how_to_reconcile": (
            "Either roll the manifest back to the predecessor generation with "
            "ingest.publish_manifest.execute_rollback (its archive is immutable and intact), or "
            "establish by review that the published generation is in fact sound. Then remove "
            "this file. Until it is removed every publish, switch and finalization refuses."),
    }
    path = quarantine_path(manifest_root)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(record, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    # The rename has to reach disk too, or a crash can lose a quarantine whose contents were
    # durable -- and a lost quarantine is exactly the silent reuse this exists to prevent.
    fd = os.open(manifest_root, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return record


# ---------------------------------------------------------------- the deployment identity
#: What an identity artifact must carry. A production approval names a root and a run; it says
#: nothing about whether the code about to run was ever reviewed, and those are different
#: questions. An approval for the right root executing unreviewed code is the failure this
#: closes.
IDENTITY_FIELDS = ("host", "run_id", "deploy_sha", "head_sha", "worktree_clean",
                   "module_fingerprints", "collected_utc", "verified_by")


def identity_problem(artifact, *, host: str, run_id: str, now=None):
    """The first thing wrong with the deployment identity artifact, or `None`.

    Four independent things, none of which implies another:

    * the commit is in `p5_readiness.REVIEWED_DEPLOY_SHAS` — a deployment cannot declare its
      own commit reviewed;
    * `HEAD` is that commit, and the worktree is clean — otherwise what runs is the reviewed
      commit plus whatever someone edited, which is how the live checkout came to be running
      `a9db4b7` plus five modified files;
    * the module fingerprints match the blobs **git holds at that commit** — recomputed here
      from the repository rather than compared against a list the artifact supplies, because a
      matching SHA proves what git says and fingerprints prove what is on disk;
    * and it was collected for THIS host and THIS run, inside the freshness window.

    The comparison is exact in both directions. A shrunken inventory makes every check pass on a
    subset, which is exactly how a hand-written module list came to omit
    `store/source_provenance.py`."""
    from ops import p5_readiness as rd
    problem = ex._missing(artifact, IDENTITY_FIELDS, "the identity artifact")
    if problem:
        return problem
    max_age_s, max_lead_s = ex._policy()

    if artifact["host"] != host:
        return (f"was collected on {artifact['host']!r}, not {host!r}; an identity established "
                f"on another machine says nothing about this one")
    if artifact["run_id"] != run_id:
        return (f"was collected in run {artifact['run_id']!r}, not {run_id!r}; a checkout that "
                f"was clean in another run may not be clean now")
    collected = ex._parse_utc(artifact["collected_utc"])
    if collected is None:
        return f"records collected_utc {artifact['collected_utc']!r}, which is not a timestamp"
    age = (ex._now(now) - collected).total_seconds()
    if age < -max_lead_s:
        return f"was collected {int(-age)}s in the future; that is not a reading"
    if age > max_age_s:
        return (f"was collected {int(age)}s ago, past the {max_age_s}s window. A deploy can be "
                f"restarted onto a different tree at any time")

    sha = artifact["deploy_sha"]
    if sha not in rd.REVIEWED_DEPLOY_SHAS:
        return (f"declares deploy_sha {sha!r}, which is not in the approved deploy set. A "
                f"production approval names a root and a run; it does not make the code that "
                f"runs reviewed, and this operation publishes a served authority")
    if artifact["head_sha"] != sha:
        return (f"records HEAD {artifact['head_sha']!r}, which is not the reviewed commit "
                f"{sha!r}; what would run is not what was approved")
    if artifact["worktree_clean"] is not True:
        return ("records a checkout that is not clean. The reviewed commit plus uncommitted "
                "edits is a tree no review has seen")

    reported = artifact["module_fingerprints"]
    if type(reported) is not dict or not reported:
        return ("records no module fingerprints. A matching commit id says what git holds; "
                "fingerprints say what is on disk, and only the second is what executes")
    for name, digest in sorted(reported.items()):
        if not ex._is_sha256(digest):
            return f"records module fingerprint {name}={digest!r}, which is not a SHA-256"

    expected = rd.expected_module_digests(sha)
    if not expected:
        return (f"cannot be checked: the module blobs for {sha} are not readable from this "
                f"repository, so there is nothing to compare the fingerprints against")
    drifted = sorted(n for n, d in expected.items() if n in reported and reported[n] != d)
    if drifted:
        return (f"records module fingerprints that do not match {sha}: {drifted}. What is on "
                f"disk is not what was reviewed")
    absent = sorted(set(expected) - set(reported))
    if absent:
        return (f"fingerprints no {absent}. An unmeasured module is one nobody compared, and a "
                f"shrunken inventory makes every other comparison pass on a subset")
    # The deployment inventory is what git holds at the approved commit. The operation's own
    # producer modules are NOT part of it -- they belong to this tooling rather than to the
    # deployed service -- so the artifact may carry them in addition, and `producer_identity()`
    # checks each against the file on disk. Anything else is a module the review never saw
    # running alongside the ones it did.
    extra = sorted(set(reported) - set(expected) - set(REVIEWED_PRODUCER_MODULES))
    if extra:
        return (f"fingerprints {extra}, which the approved commit does not contain and which "
                f"are not the operation's own producer modules "
                f"{list(REVIEWED_PRODUCER_MODULES)}")
    if not _exact_str(artifact["verified_by"]):
        return "does not say who established it; an unattributed identity is not provenance"
    return None


# ---------------------------------------------------------------- parity evidence
#: What an executed parity artifact must carry. A callable that returns `{"mismatches": 0}`
#: satisfies a gate reading only that key while having measured nothing at all -- so the gate
#: reads a bound RESULT instead, and the operation checks every binding against its own state.
PARITY_ARTIFACT_FIELDS = ("kind", "host", "run_id", "deploy_sha",
                          "producer", "producer_digest",
                          "generation", "manifest_checksum", "source_map_digest",
                          "executed_utc", "samples", "mismatches", "provenance")

#: The two parity kinds, named so an artifact for one cannot be presented for the other.
PARITY_POINT_SERIES = "point_series_parity"
PARITY_API = "api_parity"

#: The reviewed producers, per kind: (module path relative to the dev2026 tree, qualname).
#:
#: A digest carried IN the artifact was forgeable -- a lambda could copy the digest of any
#: approved module and present itself as that module, returning a fully green result while
#: measuring nothing. So the identity is DERIVED from the callable the operation holds, and this
#: registry is the reviewed list of what may be called. Both halves matter: derivation stops an
#: artifact naming a producer it did not come from, and the registry stops a genuine but
#: unreviewed function in an approved module standing in for a parity check.
REVIEWED_PRODUCERS = {
    PARITY_POINT_SERIES: ("ops/p5_parity_producers.py", "point_series_parity"),
    PARITY_API: ("ops/p5_parity_producers.py", "api_parity"),
}

#: Modules that hold the producers. They are part of the OPERATION rather than of the
#: deployment inventory `p5_readiness.INVENTORY_MODULES` describes, so the identity artifact is
#: allowed to fingerprint them in addition to the inventory -- and their on-disk digest is
#: checked against that fingerprint. See `identity_problem`, and the limitation recorded there.
REVIEWED_PRODUCER_MODULES = ("ops/p5_parity_producers.py",)


def _repo_relative(path: str) -> Optional[str]:
    """`/…/dev2026/ops/x.py` -> `ops/x.py`, or `None` when it is not in the tree."""
    dev = os.path.dirname(os.path.abspath(__file__))          # …/dev2026/ops
    dev = os.path.dirname(dev)                                # …/dev2026
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(dev) + os.sep):
        return None
    return os.path.relpath(real, os.path.realpath(dev))


def _file_digest(path: str) -> str:
    import hashlib
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _import_registered(relpath: str, qualname: str):
    """Import the reviewed module and resolve the registered attribute. `(module, fn)`.

    Imported BY PATH from the registry, never from anything the caller handed over. This is the
    difference between "what is the reviewed producer" and "what does this object say it is",
    and only the first is a fact."""
    import importlib
    if not relpath.endswith(".py"):
        raise OneShotCompactionRefused(
            f"the registry names {relpath!r}, which is not a module path")
    module_name = relpath[:-3].replace(os.sep, ".").replace("/", ".")
    module = importlib.import_module(module_name)
    resolved = module
    for part in qualname.split("."):
        resolved = getattr(resolved, part)
    return module, resolved


def producer_identity(fn, *, kind: str, module_fingerprints: Dict):
    """Who this callable ACTUALLY is: `(identity, None)` or `(None, problem)`.

    **The callable must BE the registered function, not merely describe itself as it.** An
    earlier revision read `__module__` and `__qualname__` off the object and looked the module
    up from them — and both are plain writable attributes, so:

        fake.__module__  = "ops.p5_parity_producers"
        fake.__qualname__ = "point_series_parity"

    was enough for a stub returning `{"mismatches": 0}` to obtain a valid identity carrying the
    real module's digest. Everything downstream then agreed it came from reviewed code.

    So the registry is resolved FIRST: the module is imported by the path the registry names,
    the attribute is fetched from it, and the callable must be that same object — `is`, not
    equality, because a copy with the same code is still not the function the review read. What
    the object claims about itself is never consulted.

    Then the file behind that module must hash to the fingerprint the identity artifact
    recorded, so the reviewed producer is also the code on disk right now."""
    registered = REVIEWED_PRODUCERS.get(kind)
    if registered is None:
        return None, (f"no reviewed producer is registered for {kind!r}; the operation cannot "
                      f"adjudicate a parity result it has no approved producer for")
    relpath, qualname = registered
    try:
        module, resolved = _import_registered(relpath, qualname)
    except OneShotCompactionRefused:
        raise
    except BaseException as exc:                                    # noqa: BLE001
        return None, (f"the reviewed {kind} producer {relpath}:{qualname} could not be "
                      f"resolved ({exc!r}); with nothing to compare against, no callable can "
                      f"be shown to be it")
    if fn is not resolved:
        return None, (f"the {kind} producer is not the reviewed "
                      f"{relpath}:{qualname}. `__module__` and `__qualname__` are writable "
                      f"attributes, so a callable saying it is that function proves nothing; "
                      f"this compares the object itself")

    source_file = getattr(module, "__file__", None)
    if not source_file:
        return None, (f"the reviewed module {relpath} has no file on disk to fingerprint")
    resolved_relpath = _repo_relative(source_file)
    if resolved_relpath != relpath:
        return None, (f"{relpath} imported from {source_file!r}, which is not where the "
                      f"registry says it lives; the import resolved somewhere else on the path")
    fingerprint = module_fingerprints.get(relpath)
    if fingerprint is None:
        return None, (f"the identity artifact does not fingerprint {relpath}, so the producer "
                      f"about to measure this run is code nobody recorded")
    on_disk = _file_digest(source_file)
    if on_disk != fingerprint:
        return None, (f"{relpath} on disk hashes to {on_disk[:12]}… but the identity artifact "
                      f"recorded {fingerprint[:12]}…; the reviewed producer is not the one that "
                      f"would run")
    return {"producer": f"{relpath}:{qualname}", "producer_digest": on_disk,
            "module": relpath, "qualname": qualname}, None


# ---------------------------------------------------------------- parity evidence
#: What an executed parity artifact must carry. A callable that returns `{"mismatches": 0}`
#: satisfies a gate reading only that key while having measured nothing at all -- so the gate
#: reads a bound RESULT instead, and the operation checks every binding against its own state.
PARITY_ARTIFACT_FIELDS = ("kind", "host", "run_id", "deploy_sha",
                          "producer", "producer_digest",
                          "generation", "manifest_checksum", "source_map_digest",
                          "executed_utc", "samples", "mismatches", "provenance")

#: The two parity kinds, named so an artifact for one cannot be presented for the other.
PARITY_POINT_SERIES = "point_series_parity"
PARITY_API = "api_parity"

#: The reviewed producers, per kind: (module path relative to the dev2026 tree, qualname).
#:
#: A digest carried IN the artifact was forgeable -- a lambda could copy the digest of any
#: approved module and present itself as that module, returning a fully green result while
#: measuring nothing. So the identity is DERIVED from the callable the operation holds, and this
#: registry is the reviewed list of what may be called. Both halves matter: derivation stops an
#: artifact naming a producer it did not come from, and the registry stops a genuine but
#: unreviewed function in an approved module standing in for a parity check.
REVIEWED_PRODUCERS = {
    PARITY_POINT_SERIES: ("ops/p5_parity_producers.py", "point_series_parity"),
    PARITY_API: ("ops/p5_parity_producers.py", "api_parity"),
}

#: Modules that hold the producers. They are part of the OPERATION rather than of the
#: deployment inventory `p5_readiness.INVENTORY_MODULES` describes, so the identity artifact is
#: allowed to fingerprint them in addition to the inventory -- and their on-disk digest is
#: checked against that fingerprint. See `identity_problem`, and the limitation recorded there.
REVIEWED_PRODUCER_MODULES = ("ops/p5_parity_producers.py",)


def _repo_relative(path: str) -> Optional[str]:
    """`/…/dev2026/ops/x.py` -> `ops/x.py`, or `None` when it is not in the tree."""
    dev = os.path.dirname(os.path.abspath(__file__))          # …/dev2026/ops
    dev = os.path.dirname(dev)                                # …/dev2026
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(dev) + os.sep):
        return None
    return os.path.relpath(real, os.path.realpath(dev))


def _file_digest(path: str) -> str:
    import hashlib
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def parity_artifact_problem(artifact, *, kind: str, host: str, run_id: str, deploy_sha: str,
                            generation: int, manifest_checksum: str, source_map_digest: str,
                            producer: Dict, now=None):
    """The first thing wrong with an executed parity artifact, or `None`.

    The producer is injected -- it has to be, since it reads the deployment's own stores through
    the deployment's own reader, and a parity check written inside this module would be this
    module comparing its own output against its own idea of the truth. What is NOT optional is
    that the producer be reviewed code and that its result be bound to this act:

    * `producer` is DERIVED from the callable by `producer_identity()` and checked against what
      the artifact claims. An earlier revision read the digest out of the artifact and merely
      required it to be *some* approved module's — which a lambda could satisfy by copying one,
      returning a fully green result while measuring nothing.
    * the artifact names the candidate generation, the manifest checksum and the source-map
      digest THIS run produced, so a green result from another run or another generation is
      refused rather than reused.
    * `samples` must be a positive count. Zero mismatches out of zero samples is not a passing
      check, it is an absent one."""
    problem = ex._missing(artifact, PARITY_ARTIFACT_FIELDS, f"the {kind} artifact")
    if problem:
        return problem
    max_age_s, max_lead_s = ex._policy()

    if artifact["kind"] != kind:
        return (f"declares kind {artifact['kind']!r}; this gate needs {kind!r}. An artifact for "
                f"one parity check is not evidence for another")
    for field, want in (("host", host), ("run_id", run_id), ("deploy_sha", deploy_sha),
                        ("generation", generation),
                        ("manifest_checksum", manifest_checksum),
                        ("source_map_digest", source_map_digest)):
        if artifact[field] != want:
            return (f"records {field}={artifact[field]!r} while this run produced {want!r}. A "
                    f"green result about another act reads exactly like one about this act")

    # The producer identity is DERIVED by the caller from the callable it holds, and passed in
    # here. What the artifact says about itself is then checked against it -- an artifact that
    # names a producer it did not come from is refused, rather than believed.
    if artifact["producer"] != producer["producer"]:
        return (f"claims producer {artifact['producer']!r}, but it came from "
                f"{producer['producer']!r}. What a result says about its own origin is a "
                f"string it chose")
    if artifact["producer_digest"] not in (None, producer["producer_digest"]):
        return (f"claims producer_digest {str(artifact['producer_digest'])[:12]}…, but "
                f"{producer['module']} on disk hashes to {producer['producer_digest'][:12]}…")
    if not _exact_str(artifact["provenance"]):
        return ("states no verification provenance -- what was read, from where, and how it "
                "was compared. Without it the counts below are two integers")

    executed = ex._parse_utc(artifact["executed_utc"])
    if executed is None:
        return f"records executed_utc {artifact['executed_utc']!r}, which is not a timestamp"
    age = (ex._now(now) - executed).total_seconds()
    if age < -max_lead_s:
        return f"records an execution {int(-age)}s in the future"
    if age > max_age_s:
        return (f"was executed {int(age)}s ago, past the {max_age_s}s window; it describes a "
                f"store that has had time to move")

    samples = artifact["samples"]
    if not ex._raw_int(samples) or samples <= 0:
        return (f"records samples={samples!r}. Zero mismatches out of zero samples is not a "
                f"passing check, it is an absent one")
    mismatches = artifact["mismatches"]
    if not ex._raw_int(mismatches) or mismatches < 0:
        return f"records mismatches={mismatches!r}, which is not a count"
    if mismatches != 0:
        return f"reports {mismatches} mismatch(es) out of {samples} sample(s)"
    return None


# ---------------------------------------------------------------- the fence probe
def refusing_fence_probe(*_a, **_k):
    """The default writer-fence probe: it refuses.

    There is no way for this process to learn whether `pm2` stopped a service or whether the
    append cron is commented out — those live on the deployment host, and a probe that guessed
    would be fabricating the one piece of evidence the whole window rests on. So the shipped
    default fails closed, and the operator tooling supplies a real probe.

    This is deliberately NOT a `verified: true` stub. A default that returns something
    plausible is worse than one that refuses: it makes every local test pass while proving
    nothing about any deployment."""
    raise OneShotCompactionRefused(
        "no writer_fence_probe was supplied. This operation runs over a WRITABLE monolith, so "
        "the writer-producing services must be stopped and the append cron disabled for the "
        "whole scan-to-publish window -- and whether they are is a fact about the deployment "
        "host that nothing in this process can observe. Supply a probe that reports the real "
        "posture; there is no default that can honestly say the fence is up")


def inventory(paths: Dict[str, str]) -> Dict[str, Dict]:
    """Bytes and file count for each named path. The raw material for `nothing_deleted`.

    Cheap relative to the build (it is one more walk of stores already being walked), and it is
    the only way the claim "nothing was removed" can be re-derived by a checker rather than
    taken on the operation's word."""
    out: Dict[str, Dict] = {}
    for role, path in sorted(paths.items()):
        if path is None:
            continue
        total = 0
        files = 0
        real = os.path.realpath(path)
        if not os.path.exists(real):
            out[role] = {"path": real, "bytes": 0, "files": 0, "present": False}
            continue
        for dirpath, _dirs, names in os.walk(real):
            for name in names:
                try:
                    total += os.stat(os.path.join(dirpath, name)).st_size
                    files += 1
                except FileNotFoundError:              # vanished mid-walk: count it as gone
                    continue
        out[role] = {"path": real, "bytes": total, "files": files, "present": True}
    return out


def _fence_moved(before: Dict, after: Dict) -> Optional[str]:
    """Did the SET of fenced services shrink between two probes? The problem, or `None`.

    Only the set difference, deliberately. Earlier revisions of this function also re-checked
    for a service that had come back up and for a re-enabled cron — and both branches were
    unreachable, because `writer_fence_problem()` runs on every probe first and refuses any
    posture outside `SAFE_PM2_POSTURES` / `SAFE_CRON_POSTURES`. A branch that cannot fire is not
    a guard, and keeping one invites the belief that something is being checked. What comparison
    genuinely adds is the case each individual probe cannot see: a service that was fenced
    before the scan and is simply *absent* from the later report, which is valid-looking on its
    own and is a fence that stopped covering something.

    **What no amount of probing establishes.** These are samples — before the scan, after the
    build, after the publish. A service that stopped and restarted entirely between two samples
    is invisible to all of them, and nothing available here can close that: proving a posture
    held continuously needs something watching continuously. This is why the fence is a
    *precondition an operator arranges*, not a guarantee this code provides, and why the
    content-ledger recheck and the parity gates sit behind it."""
    was = {s["name"] for s in before.get("services_stopped", [])
           if type(s) is dict and "name" in s}
    now = {s["name"] for s in after.get("services_stopped", [])
           if type(s) is dict and "name" in s}
    dropped = sorted(was - now)
    if dropped:
        return (f"services {dropped} were fenced before the scan and are not accounted for "
                f"after it; a fence that stops being reported is not one that held")
    return None


# ---------------------------------------------------------------- the operation
def compact_one_shot(*, live_root: str, manifest_root: str, wal_root: str, anchor_root: str,
                     monolith_path: str, delta_path: Optional[str] = None,
                     daily_root: Optional[str] = None,
                     compaction_lock_path: str, ingest_lock_path: str,
                     approval: Dict, one_shot_exception: Dict, one_shot_pre_compaction: Dict,
                     run_id: str, operator: str, at_utc: str, min_free_gib: float,
                     start_day: str, end_day: str, classification_target: Sequence[str],
                     artifacts_dir: str, hard_reserve_bytes: int,
                     block_out_path: str,
                     identity_artifact: Dict,
                     writer_fence_probe: Callable = refusing_fence_probe,
                     point_series_parity: Callable,
                     api_parity: Callable,
                     now=None) -> Dict:
    """Build one block from the monolith, publish it, verify it, and delete nothing.

    `identity_artifact` is REQUIRED and is checked before any compaction work begins. A
    production approval names a root and a run; it does not establish that the code about to
    publish a served authority was ever reviewed. There is deliberately no
    `expected_module_fingerprints` parameter any more: it was accepted, never read, and an
    optional argument that does nothing is worse than an absent one, because a reader passing it
    believes a check is happening.

    `point_series_parity` and `api_parity` are injected rather than implemented here — both must
    read the deployment's own stores through the deployment's own reader, and a parity check
    written inside this module would be this module comparing its own output against its own
    idea of the truth. What they RETURN, though, is not taken on trust: each must be a fully
    bound artifact (see `parity_artifact_problem`), produced by a module the identity artifact
    fingerprinted. A callable that returns `{"mismatches": 0}` is refused, because nothing in
    that dict distinguishes a check that passed from one that never ran.

    There is no `force`, no `skip`, no `unsafe_*` parameter, and this operation never passes one
    to `execute_publication` either — a test asserts that by AST."""
    if not callable(point_series_parity) or not callable(api_parity):
        raise OneShotCompactionRefused(
            "point_series_parity and api_parity are required callables: the gates are only "
            "worth anything if something actually measures them")
    for name, value in (("run_id", run_id), ("operator", operator), ("at_utc", at_utc),
                        ("start_day", start_day), ("end_day", end_day)):
        if not _exact_str(value):
            raise OneShotCompactionRefused(f"{name} must be a non-empty string")

    # ---- approval, scoped to THIS operation ------------------------------------------------
    approval = _check_approval(approval, live_root=live_root, run_id=run_id,
                               now=now)

    # ---- and the code that would run, BEFORE any compaction work --------------------------
    # Checked here rather than under the locks on purpose: an operator whose checkout is dirty
    # should learn it before a lock is taken and a multi-minute scan begins, not after.
    problem = identity_problem(identity_artifact, host=_hostname(), run_id=run_id, now=now)
    if problem:
        raise OneShotCompactionRefused(f"the deployment identity artifact {problem}")
    deploy_sha = identity_artifact["deploy_sha"]
    module_fingerprints = dict(identity_artifact["module_fingerprints"])

    # ---- and who is going to measure the parity, derived from the callables themselves ----
    # Up here with the identity, and for the same reason: a caller who passed an unreviewed
    # producer should learn it before a lock is taken, not after a multi-minute scan.
    point_series_identity, problem = producer_identity(
        point_series_parity, kind=PARITY_POINT_SERIES,
        module_fingerprints=module_fingerprints)
    if problem:
        raise OneShotCompactionRefused(problem)
    api_identity, problem = producer_identity(
        api_parity, kind=PARITY_API, module_fingerprints=module_fingerprints)
    if problem:
        raise OneShotCompactionRefused(problem)

    # ---- a quarantined generation stops everything ----------------------------------------
    problem = quarantine_problem(manifest_root)
    if problem:
        raise OneShotCompactionRefused(problem)

    # ---- the record must name this operation, before anything is read ----------------------
    problem = ex.operation_problem(
        one_shot_exception.get("operation") if type(one_shot_exception) is dict else None)
    if problem:
        raise OneShotCompactionRefused(f"the one-shot exception record {problem}")
    if one_shot_exception.get("operation") != ex.OP_COMPACTION:
        raise OneShotCompactionRefused(
            f"the record authorizes {one_shot_exception.get('operation')!r}; this operation is "
            f"{ex.OP_COMPACTION!r}. A provisioning acceptance is not a compaction acceptance")

    # ---- every path this run reads or writes, bound to something that approved it ---------
    paths = check_operation_paths(
        live_root=live_root, manifest_root=manifest_root, wal_root=wal_root,
        block_out_path=block_out_path, artifacts_dir=artifacts_dir, anchor_root=anchor_root,
        monolith_path=monolith_path, delta_path=delta_path, daily_root=daily_root,
        pre=one_shot_pre_compaction or {})

    locks = prod.check_lock_paths(live_root=live_root,
                                  compaction_lock_path=compaction_lock_path,
                                  ingest_lock_path=ingest_lock_path)
    capacity = prod.measure_capacity(live_root, min_free_gib=min_free_gib)

    busy = compaction_lock.refuse_if_compaction_running(
        locks["compaction"], operation="p5 one-shot compaction")
    if busy:
        raise OneShotCompactionRefused(
            f"a compaction is already running ({busy}); two compactors against one store is "
            f"the state the reservation exists to make impossible")

    protected = {"monolith": monolith_path, "delta": delta_path, "daily": daily_root}
    protected = {k: v for k, v in protected.items() if v}

    holder = f"p5_oneshot_compaction/{run_id}"
    result: Dict = {"deleted_anything": False, "service_switched": False}

    with compaction_lock.CompactionLock(locks["compaction"], holder=holder) as clock:
        # ---- the fence goes up BEFORE anything is read ------------------------------------
        fence_before = writer_fence_probe()
        problem = ex.writer_fence_problem(fence_before)
        if problem:
            raise OneShotCompactionRefused(f"the writer fence {problem}")

        with compaction_lock.CompactionLock(locks["ingest"],
                                            holder=f"{holder}/scan-to-publish") as ilock:
            clock.assert_still_held()

            # ---- measured HERE, by this code, under both locks ----------------------------
            capability, verdict = ex.authorize_compaction(
                record=one_shot_exception, host=_hostname(), run_id=run_id,
                monolith_path=monolith_path, pre=one_shot_pre_compaction,
                expected_dev=capacity["filesystem_dev"], now=now)
            # There is deliberately NO `isinstance(capability, OneShotCompactionAuthorization)`
            # check here. `authorize_compaction()` mints that exact class and nothing else, so
            # the branch could not fire -- and a guard that cannot fire is not a guard, it is a
            # reassurance. The type binding does real work where a capability arrives from
            # somewhere else: `prepare_generation_one()` checks it, because a caller hands one
            # in there. Here the operation mints its own.
            measured = capability.measured
            # SPENT here, at the act it authorizes. Minting without consuming would leave a
            # capability that is single-use in principle and never used in practice -- the
            # object would still be holdable after the run, which is the standing permission
            # the whole design refuses. `consume()` re-checks the store and the expiry.
            authorization_evidence = capability.consume(
                operation=ex.OP_COMPACTION, subject_realpath=monolith_path, now=now)
            inventory_before = inventory(protected)

            # ---- the expensive part, inside the fence ------------------------------------
            plan = _build(out_path=block_out_path, start_day=start_day, end_day=end_day,
                          classification_target=classification_target,
                          monolith_path=monolith_path, delta_path=delta_path,
                          daily_root=daily_root, artifacts_dir=artifacts_dir,
                          hard_reserve_bytes=hard_reserve_bytes, lock=clock,
                          wal_root=wal_root, anchor_root=anchor_root)
            clock.assert_still_held()
            ilock.assert_still_held()

            # ---- the fence must not have moved while we scanned --------------------------
            fence_mid = writer_fence_probe()
            problem = ex.writer_fence_problem(fence_mid) or _fence_moved(fence_before, fence_mid)
            if problem:
                raise OneShotCompactionRefused(
                    f"the writer fence did not hold across the scan: {problem}")

            # ---- the monolith must still be what was measured ----------------------------
            remeasured = ex.measure_monolith(monolith_path, now=now)
            drift = ex._state_mismatch(measured, remeasured,
                                       left_name="the measurement taken before the scan",
                                       right_name="the monolith after the build")
            if drift:
                raise OneShotCompactionRefused(
                    f"the monolith changed during the build: {drift}. Nothing was published")

            # ---- PRE-PUBLISH gates: a failure here publishes nothing ----------------------
            source_map = dict(plan.get("source_map") or {})
            # The oracle is PER DAY, not one store. The block grid starts where the legacy
            # base stops, so a compaction folds delta days -- and the truth for such a day is
            # the delta it was read from, not the monolith, which never held it. The source map
            # is what says which; handing the probe a single `monolith_path` would have made it
            # compare most days against a store that does not contain them.
            candidate = _plan(plan, manifest_root=manifest_root)
            candidate_generation = int(candidate["manifest"]["generation"])
            candidate_checksum = candidate["manifest"]["manifest_checksum"]
            source_map_digest = _source_map_digest(source_map)
            parity = point_series_parity(block_path=plan["out_path"],
                                         source_map=source_map,
                                         monolith_path=monolith_path,
                                         days=list(classification_target),
                                         host=_hostname(), run_id=run_id,
                                         deploy_sha=deploy_sha,
                                         generation=candidate_generation,
                                         manifest_checksum=candidate_checksum,
                                         source_map_digest=source_map_digest)
            problem = parity_artifact_problem(
                parity, kind=PARITY_POINT_SERIES, host=_hostname(), run_id=run_id,
                deploy_sha=deploy_sha, generation=candidate_generation,
                manifest_checksum=candidate_checksum, source_map_digest=source_map_digest,
                producer=point_series_identity, now=now)
            if problem:
                raise OneShotCompactionRefused(
                    f"refusing to publish: the point-series parity artifact {problem}. The "
                    f"block is built and unreferenced; nothing was published")
            # `candidate` was composed above, before the parity call, because the parity
            # artifact must be BOUND to the generation and checksum this run is about to
            # publish. `plan_publication` writes nothing, so composing it early costs nothing
            # and is what lets the binding exist before the authority changes.
            pre_gates = {
                "point_series_parity": parity,
                "source_map_complete": _source_map_complete(source_map, classification_target),
                "base_delta_disjoint": _base_delta_disjoint(candidate["manifest"]),
            }
            problem = _pre_publish_problem(pre_gates)
            if problem:
                raise OneShotCompactionRefused(
                    f"refusing to publish: {problem}. The block is built and unreferenced; "
                    f"nothing was published, nothing was deleted")

            # ---- the authority change, borrowing both locks -------------------------------
            published = _execute(candidate, locks=locks, clock=clock, ilock=ilock,
                                 delta_path=delta_path, operator=operator,
                                 artifacts_dir=artifacts_dir, block_plan=plan)
            if not published.get("published"):
                raise OneShotCompactionRefused(
                    f"publication did not commit: {published.get('reason') or published}")

        # ---- ingest lock released; still under the compaction lock ------------------------
        # From here the authority HAS changed, so every failure below -- a returned problem, a
        # probe that raises, an unreadable manifest, a reader that throws -- must leave the
        # generation classified. `_post_publish` does the work and this converts anything it
        # raises into a quarantine, because an exception escaping unclassified is the exact
        # state the next run would build on without noticing.
        published_generation = int(published.get("generation") or 0)
        predecessor = (published.get("predecessor_generation")
                       if published.get("predecessor_generation") is not None
                       else candidate.get("predecessor_generation"))
        result["predecessor_generation"] = predecessor
        try:
            return _post_publish(
                result=result, manifest_root=manifest_root, live_root=live_root,
                wal_root=wal_root, anchor_root=anchor_root, monolith_path=monolith_path,
                protected=protected, inventory_before=inventory_before,
                remeasured=remeasured, source_map=source_map,
                source_map_digest=source_map_digest, pre_gates=pre_gates, parity=parity,
                api_parity=api_parity, api_identity=api_identity,
                writer_fence_probe=writer_fence_probe, fence_before=fence_before,
                fence_mid=fence_mid, clock=clock, capability=capability,
                authorization_evidence=authorization_evidence, verdict=verdict,
                deploy_sha=deploy_sha, run_id=run_id, operator=operator, at_utc=at_utc,
                min_free_gib=min_free_gib, plan=plan, now=now)
        except GenerationQuarantined:
            raise
        except QuarantineWriteFailed:
            raise
        except BaseException as failure:
            _quarantine_or_fail_closed(
                manifest_root, generation=published_generation,
                predecessor_generation=predecessor, run_id=run_id, host=_hostname(),
                operator=operator, reasons=f"{type(failure).__name__}: {failure}",
                block_path=plan["out_path"], result=result, now=now, cause=failure)


def _post_publish(*, result, manifest_root, live_root, wal_root, anchor_root, monolith_path,
                  protected, inventory_before, remeasured, source_map, source_map_digest,
                  pre_gates, parity, api_parity, api_identity, writer_fence_probe,
                  fence_before, fence_mid, clock, capability, authorization_evidence, verdict,
                  deploy_sha, run_id, operator, at_utc, min_free_gib, plan, now):
    """Everything after the authority changed. Raising from here quarantines the generation."""
    if True:
        clock.assert_still_held()
        live = bm.load_live(manifest_root)
        fence_after = writer_fence_probe()
        problem = ex.writer_fence_problem(fence_after) or _fence_moved(fence_before, fence_after)
        if problem:
            raise OneShotCompactionRefused(
                f"the writer fence did not hold across the whole window: {problem}")

        api_evidence = api_parity(
            manifest_root=manifest_root, monolith_path=monolith_path,
            allowed_legacy_paths=[os.path.realpath(monolith_path)],
            host=_hostname(), run_id=run_id, deploy_sha=deploy_sha,
            generation=int(live["generation"]),
            manifest_checksum=live["manifest_checksum"],
            source_map_digest=source_map_digest)
        api_problem = parity_artifact_problem(
            api_evidence, kind=PARITY_API, host=_hostname(), run_id=run_id,
            deploy_sha=deploy_sha, generation=int(live["generation"]),
            manifest_checksum=live["manifest_checksum"],
            source_map_digest=source_map_digest,
            producer=api_identity, now=now)

        evidence = {
            "host": _hostname(), "run_id": run_id,
            "manifest": {"generation": int(live["generation"]),
                         "checksum": live["manifest_checksum"],
                         "checksum_ok": bm.compute_checksum(live) == live["manifest_checksum"]},
            "wal_anchor": _wal_anchor_evidence(wal_root, manifest_root=manifest_root,
                                               anchor_root=anchor_root),
            "source_map_complete": pre_gates["source_map_complete"],
            "base_delta_disjoint": pre_gates["base_delta_disjoint"],
            "source_map_digest": source_map_digest,
            "point_series_parity": parity,
            "api_parity": api_evidence,
            # Stamped HERE. `measure_capacity` returns free space and a device and no time, so
            # a reading of it is indistinguishable from the preflight's -- which is exactly what
            # `capacity_after` must not be. The stamp is taken at the moment of the call.
            "capacity_after": {**prod.measure_capacity(live_root, min_free_gib=min_free_gib),
                               "measured_utc": ex._now(now).strftime("%Y-%m-%dT%H:%M:%SZ")},
            "monolith_state": remeasured,
            "nothing_deleted": {
                "value": None, "derived_from": "inventory",
                "before": inventory_before, "after": inventory(protected),
            },
        }
        # Re-derived from the operation's OWN inventory, never accepted as a claim. Recomputing
        # it here and letting `nothing_deleted_problem` recompute it again is not redundant:
        # this fills the field, that one refuses to believe it.
        evidence["nothing_deleted"]["value"] = _nothing_shrank(
            evidence["nothing_deleted"]["before"], evidence["nothing_deleted"]["after"])

        expected = {
            "host": _hostname(), "run_id": run_id,
            "generation": int(live["generation"]),
            "manifest_checksum": live["manifest_checksum"],
            "source_map_digest": source_map_digest,
            "monolith_state": remeasured,
        }
        # The API parity artifact's own binding failure is a post-gate failure like any other,
        # and must reach the quarantine path rather than raising past it.
        problem = api_problem or ex.post_compaction_problem(evidence, expected=expected)
        result.update({
            "run_id": run_id, "operator": operator, "at_utc": at_utc,
            "generation": int(live["generation"]),
            "manifest_root": manifest_root, "block_path": plan["out_path"],
            "one_shot_exception": verdict,
            "capability_consumed": capability.consumed,
            "authorization": authorization_evidence,
            "fence": {"before": fence_before, "mid": fence_mid, "after": fence_after,
                      "held_over": "compaction lock outermost; ingest lock from before the "
                                   "scan through the publish; writer fence probed before, "
                                   "after the build, and after the publish"},
            "post_compaction": evidence,
            "post_compaction_problem": problem,
        })
        if problem:
            # Published but NOT verified. Raising alone would leave generation N+1 live and
            # unclassified: the next run would read it, plan N+2 on top of it, and the failure
            # would sit underneath a generation nobody questioned. So the state is written down
            # FIRST, durably, and only then does the run fail -- the record is what makes every
            # later publish, switch and finalization refuse.
            #
            # Deliberately not an automatic rollback. Restoring the predecessor is itself an
            # authority change, and performing one in the failure path on the strength of a gate
            # this run could not satisfy is a second unreviewed change chasing the first. The
            # predecessor archive is immutable and intact, so a person can still roll back.
            _quarantine_or_fail_closed(
                manifest_root, generation=int(live["generation"]),
                predecessor_generation=result.get("predecessor_generation"),
                run_id=run_id, host=_hostname(), operator=operator,
                reasons=problem, block_path=plan["out_path"], result=result, now=now,
                cause=None)

        result["finalization_clearance"] = ex.issue_finalization_clearance(
            host=_hostname(), run_id=run_id, generation=int(live["generation"]),
            manifest_checksum=live["manifest_checksum"],
            monolith_day_digest=remeasured["monolith_day_digest"],
            source_map_digest=_source_map_digest(source_map),
            issued_by=f"ops/p5_oneshot_compaction.compact_one_shot ({operator})",
            gates_passed=True, now=now)
        result["note"] = (
            "Generation published and verified. NOTHING was deleted and no service was "
            "switched: this operation does neither, by design. The clearance records that the "
            "gates passed; releasing the old space is a separate operation that does not exist "
            "yet, and it needs this clearance AND a separate human approval.")
    return result


# ---------------------------------------------------------------- helpers
def _hostname() -> str:
    import socket
    return socket.gethostname()


#: Every field a compaction approval must carry.
APPROVAL_FIELDS = ("scope", "approved_by", "approved_utc", "live_root", "run_id")


def approval_problem(approval, *, live_root: str, run_id: str, now=None):
    """The first thing wrong with a compaction approval, or `None`.

    Each check is INDEPENDENT of its neighbours on purpose. An earlier version tested only
    presence and equality, so `approved_by: True` and `approved_utc: "soon"` satisfied it — the
    record looked complete because the fields it needed were there, and a field being there is
    not the same as its value meaning anything.

    `approved_utc` in particular carries weight: it is what places the approval before the run
    rather than after it, and a string that does not parse cannot be placed anywhere."""
    if type(approval) is not dict:
        return (f"is {type(approval).__name__}, not a record")
    for field in APPROVAL_FIELDS:
        if field not in approval:
            return f"records no {field}"
    if approval["scope"] != COMPACTION_SCOPE:
        return (f"declares scope {approval['scope']!r}; this needs {COMPACTION_SCOPE!r}. An "
                f"approval to create roots is not an approval to publish a compaction "
                f"generation over them")
    # Raw types, not truthiness. `True` is not a name, and `1` is not a run id.
    #
    # There is deliberately no separate "and it is not blank" pass here. Every one of these
    # fields has its own check below that a blank value fails -- `approved_by` on length,
    # `approved_utc` on parsing, `live_root` on being absolute, `run_id` on matching a run id
    # that is itself required non-empty. A loop refusing blanks in front of those could not
    # fire, and a mutation deleting it survived precisely because nothing depended on it.
    for field in ("approved_by", "approved_utc", "live_root", "run_id"):
        if type(approval[field]) is not str:
            return (f"records {field}={approval[field]!r}, which is "
                    f"{type(approval[field]).__name__} rather than a string")
    if len(approval["approved_by"].split()) < 1 or len(approval["approved_by"].strip()) < 2:
        return (f"records approved_by {approval['approved_by']!r}, which does not name anyone")
    approved = ex._parse_utc(approval["approved_utc"])
    if approved is None:
        return (f"records approved_utc {approval['approved_utc']!r}, which is not a timestamp. "
                f"An approval that cannot be placed in time cannot be placed before this run")
    max_age_s, max_lead_s = ex._policy()
    when = ex._now(now)
    if (approved - when).total_seconds() > max_lead_s:
        return (f"is dated {approval['approved_utc']}, which is in the future. An approval "
                f"nobody could have given yet is not one")
    if not os.path.isabs(approval["live_root"]):
        return f"names live_root {approval['live_root']!r}, which is not an absolute path"
    if os.path.realpath(approval["live_root"]) != os.path.realpath(live_root):
        return (f"names live_root {approval['live_root']!r}, not {live_root!r}")
    if approval["run_id"] != run_id:
        return f"is for run {approval['run_id']!r}, not {run_id!r}"
    # Optional, and checked when present rather than ignored: an approval that carries an
    # expiry means it.
    if "expires_utc" in approval:
        expires = ex._parse_utc(approval["expires_utc"])
        if expires is None:
            return (f"records expires_utc {approval['expires_utc']!r}, which is not a "
                    f"timestamp")
        if expires <= approved:
            return "expires at or before it was approved, so it authorizes nothing"
        if when >= expires:
            return f"expired at {approval['expires_utc']}"
    return None


def _check_approval(approval, *, live_root: str, run_id: str, now=None) -> Dict:
    problem = approval_problem(approval, live_root=live_root, run_id=run_id, now=now)
    if problem:
        raise OneShotCompactionRefused(f"the compaction approval {problem}")
    return approval


def _build(*, out_path, start_day, end_day, classification_target, monolith_path, delta_path,
           daily_root, artifacts_dir, hard_reserve_bytes, lock, wal_root, anchor_root) -> Dict:
    """The block build, through the existing authority. No `unsafe_*` argument is passed."""
    from ingest.build_block import build_block
    return build_block(
        out_path, start_day=start_day, end_day=end_day,
        classification_target=list(classification_target),
        predecessor_path=monolith_path, delta_path=delta_path, daily_root=daily_root,
        artifacts_dir=artifacts_dir, lock=lock, hard_reserve_bytes=hard_reserve_bytes,
        wal_root=wal_root, anchor_root=anchor_root)


def _plan(block_plan, *, manifest_root) -> Dict:
    """Compose generation N+1 in memory. Reads the live manifest; writes nothing."""
    from ingest import publish_manifest as pub
    return pub.plan_publication(manifest_root, block_plan)


def _execute(candidate, *, locks, clock, ilock, delta_path, operator, artifacts_dir,
             block_plan) -> Dict:
    """Commit generation N+1, borrowing BOTH locks this run already holds.

    Borrowing is not an optimisation. `execute_publication` takes the ingest lock with a
    blocking `flock` on a fresh descriptor, and flock locks are per open file description — so
    a caller holding the fence across the scan would deadlock against itself. The borrow is what
    lets "held from before the scan through the publish" be true rather than intended."""
    from ingest import publish_manifest as pub
    # Taken from the build's own report, not composed from a naming convention. `build_block`
    # writes the provenance artifact and returns where it put it; guessing the path is how a
    # publication ends up verifying an artifact belonging to a different build.
    artifact = block_plan.get("provenance_path")
    if not artifact:
        raise OneShotCompactionRefused(
            "the build produced no provenance artifact path. A block published without one "
            "carries only locator provenance, which cannot show that the bytes it holds are "
            "the bytes its sources held (§7.5a)")
    return pub.execute_publication(
        candidate,
        ingest_lock_path=locks["ingest"], ingest_lock=ilock,
        compaction_lock_path=locks["compaction"], compaction_lock=clock,
        delta_path=delta_path, build_artifact_path=artifact, operator=operator)


def _source_map_complete(source_map: Dict, days: Sequence[str]) -> bool:
    """Every day the block claims has a NAMED source, with the bytes it was read from.

    Not "the map is non-empty", and not "the day is a key". `source_map_record` exists because
    `source_kind` alone cannot answer the audit question -- two builds can both say `"delta"`
    for day D and have read different physical indices. So a record missing its resolved path
    or its index is a day whose provenance nobody can reconstruct, which is the same problem as
    a day with no record at all."""
    if not source_map:
        return False
    for day in days:
        record = source_map.get(day)
        if type(record) is not dict:
            return False
        if not record.get("source_kind") or not record.get("source_path"):
            return False
        if "source_day_index" not in record:
            return False
    return True


def _segment_days(segment: Dict) -> set:
    """The days a manifest segment claims to serve."""
    if segment.get("day_list"):
        return set(segment["day_list"])
    start, end = segment.get("start_day"), segment.get("end_day")
    if not start or not end:
        return set()
    return set(bm.calendar_span(start, end)) - set(segment.get("gaps") or [])


def _base_delta_disjoint(live_manifest: Dict) -> bool:
    """No day is claimed by two LIVE segments of the published manifest.

    Deliberately checked against the manifest rather than against the source map. The source
    map resolves every day to exactly one tier by construction -- the resolver breaks out of
    its loop on the first hit -- so checking it for overlap would be checking a property the
    data structure cannot violate, and would pass forever regardless of what was published.

    What CAN overlap is two segments in a manifest: a new block covering days the legacy base
    also holds, where the old segment was not superseded. That is a day whose served value
    depends on which segment answers first, which is a correctness question wearing the clothes
    of a performance one. Superseded entries are excluded because they are no longer served."""
    seen: Dict[str, str] = {}
    for segment in live_manifest.get("segments", []):
        for day in _segment_days(segment):
            if day in seen:
                return False
            seen[day] = segment.get("segment_id")
    return True


def _source_map_digest(source_map: Dict) -> str:
    from ingest.build_block import source_map_digest
    return source_map_digest(source_map)


def _nothing_shrank(before: Dict, after: Dict) -> bool:
    for role, was in sorted(before.items()):
        now = after.get(role)
        if type(now) is not dict:
            return False
        if now["bytes"] < was["bytes"] or now["files"] < was["files"]:
            return False
    return True


def _pre_publish_problem(gates: Dict) -> Optional[str]:
    """The gates that MUST be settled before the authority changes.

    Parity, completeness and disjointness are properties of the BUILT BLOCK, so they can all be
    established while the block is still unreferenced — and a failure then costs a rebuild
    rather than a rollback. Only the checks that need a published manifest (its checksum, the
    WAL binding, API parity) wait until afterwards."""
    parity = gates["point_series_parity"]
    if type(parity) is not dict:
        return "the point-series parity result is not a record"
    for field in ("days_checked", "mismatches", "boundaries_included"):
        if field not in parity:
            return f"the point-series parity result records no {field}"
    if parity["mismatches"] != 0:
        return (f"{parity['mismatches']} point-series mismatch(es) against the monolith oracle")
    if parity["boundaries_included"] is not True:
        return ("the parity check skipped the first and last days; the boundaries are where an "
                "off-by-one shows")
    if type(parity["days_checked"]) is bool or not isinstance(parity["days_checked"], int) \
            or parity["days_checked"] <= 0:
        return f"the parity check covered {parity['days_checked']!r} days, which checks nothing"
    if gates["source_map_complete"] is not True:
        return "the source map is incomplete; a day with no named source is unaccounted for"
    if gates["base_delta_disjoint"] is not True:
        return "base and delta are not disjoint; a day claimed by both is a served-value race"
    return None


def _wal_anchor_evidence(wal_root: str, *, manifest_root: str, anchor_root: str) -> Dict:
    """Is the WAL still bound to the authority that now exists, with its anchor intact?

    A published generation moves the manifest authority. A WAL still bound to the PREVIOUS one
    is a log whose repairs are about a store the service no longer serves — and `repair_wal`
    refuses to append to it, so the failure surfaces at the next repair rather than here. This
    reads both and compares them, so it surfaces now."""
    try:
        authority = repair_wal.manifest_authority(manifest_root, anchor_root)
    except Exception as exc:                                        # noqa: BLE001
        return {"bound": False, "anchor_intact": False,
                "reason": f"the manifest authority could not be read: {exc!r}"}
    try:
        state = repair_wal.read_wal(wal_root, anchor_root=anchor_root)
    except Exception as exc:                                        # noqa: BLE001
        return {"bound": False, "anchor_intact": False, "authority": authority,
                "reason": f"the WAL could not be read: {exc!r}"}
    if state.authority is None:
        return {"bound": False, "anchor_intact": state.anchor is not None,
                "authority": authority,
                "reason": ("the WAL has never been initialized, so nothing binds it to the "
                           "generation that was just published")}
    bound_to = state.authority.get("manifest_root") or state.authority.get("root")
    same_root = (bound_to is None
                 or os.path.realpath(bound_to) == os.path.realpath(manifest_root))
    return {
        "bound": bool(same_root),
        "anchor_intact": state.anchor is not None,
        "authority": authority,
        "wal_authority": dict(state.authority),
        "wal_last_seq": state.last_seq,
        "reason": None if same_root else (
            f"the WAL is bound to {bound_to!r}, not to the manifest root that was just "
            f"published ({manifest_root!r})"),
    }
