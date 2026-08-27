"""dev2026 — generate the review package for a deploy candidate. Reads only; approves nothing.

A reviewer deciding whether a commit should become an approved deploy SHA needs the same four
things every time: what changed, what the tree contains, what each module hashes to, and what
would have to be edited if they said yes. Assembling that by hand produces a package whose
digests were true when someone pasted them, so this derives all of it from the commit.

## It cannot approve anything, and does not try

Nothing here writes to `REVIEWED_DEPLOY_SHAS`, creates an approval record, or marks a candidate
as anything. `manifest()` returns a description of a commit; whether that commit is approved is
a person's decision recorded elsewhere. The one opinion it holds is `identity_update_plan()`,
which states what an approval would REQUIRE — and says so as a plan, not as a change.

## Why digests come from the commit, not the worktree

`git cat-file` at the candidate SHA, not `open()` on disk. A package generated from a dirty
worktree describes a tree that does not exist anywhere, and would reassure a reviewer about
bytes nobody can retrieve again.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from typing import Dict, List, Optional, Sequence

#: Where the dev tree sits inside the repository, as git paths see it.
DEV_PREFIX = "dev2026"

def operation_modules() -> Sequence[str]:
    """The tooling that RUNS a compaction, DERIVED from the runtime gate's own inventory.

    Not a second list. It was one -- a hand-written seven beside a runtime inventory of one --
    and two lists of "the operation's modules" that nothing reconciles are two lists that
    disagree the moment either moves. The gate is the authority because the gate is what
    refuses; this package reports what the gate enforces, plus itself, because a reviewer
    checking the package's digests should be able to check the package's own bytes too."""
    from ops import p5_oneshot_compaction as oc
    return tuple(sorted(set(oc.OPERATION_INVENTORY) | {"ops/p5_deploy_candidate.py"}))


class DeployCandidateError(Exception):
    """The candidate could not be described. Never "described it approximately"."""


def _repo_root() -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(here))


def _git(*args: str) -> str:
    proc = subprocess.run(("git", "-C", _repo_root()) + args,
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise DeployCandidateError(
            f"git {' '.join(args)} failed: {proc.stderr.strip() or proc.returncode}")
    return proc.stdout


def _blob(sha: str, relpath: str) -> Optional[bytes]:
    proc = subprocess.run(("git", "-C", _repo_root(), "cat-file", "blob",
                           f"{sha}:{DEV_PREFIX}/{relpath}"), capture_output=True)
    return proc.stdout if proc.returncode == 0 else None


def digests_at(sha: str, modules: Sequence[str]) -> Dict[str, Optional[str]]:
    """SHA-256 of each module AT `sha`, read from git. `None` where the commit lacks it.

    `None` is reported rather than skipped: a module missing from the candidate is exactly what
    a reviewer needs to see, and dropping the key would make the absence look like an oversight
    in the package instead of a fact about the commit."""
    out: Dict[str, Optional[str]] = {}
    for rel in modules:
        blob = _blob(sha, rel)
        out[rel] = hashlib.sha256(blob).hexdigest() if blob is not None else None
    return out


def tree_files(sha: str) -> List[str]:
    """Every dev-tree path the candidate contains."""
    listing = _git("ls-tree", "-r", "--name-only", sha, "--", DEV_PREFIX)
    prefix = DEV_PREFIX + "/"
    return sorted(line[len(prefix):] for line in listing.splitlines()
                  if line.startswith(prefix))


def dirty_paths() -> List[str]:
    """Everything git considers untracked or modified, with `--untracked-files=all`.

    The default `--untracked-files=normal` COLLAPSES an untracked directory to a single entry
    and, more importantly here, a file inside one is never named. A package is a claim about
    exactly which bytes are in the tree, so the mode that lists them individually is the one
    that supports the claim."""
    out = _git("status", "--porcelain", "--untracked-files=all")
    return [line[3:] for line in out.splitlines() if line.strip()]


def ignored_modules() -> List[str]:
    """Ignored `.py` files under the dev tree. Invisible to `status` at ANY untracked mode.

    `--untracked-files=all` does not list ignored files -- that is what ignoring means -- so a
    module in `.gitignore` sits on disk, gets imported at run time, and appears in no listing
    the package produces. The archive would then be missing a file the operation actually runs,
    and every digest in the manifest would still match.

    This is not hypothetical for THIS operation: `producer_identity()` imports a module by path
    and hashes the file on disk. An ignored module at a registry path is precisely the shape
    that would pass the on-disk check and be absent from the reviewed commit."""
    out = _git("status", "--porcelain", "--ignored=matching", "--untracked-files=all")
    prefix = DEV_PREFIX + "/"
    return sorted(line[3:] for line in out.splitlines()
                  if line.startswith("!! ") and line[3:].startswith(prefix)
                  and line.rstrip().endswith(".py"))


def worktree_is_clean() -> bool:
    """No untracked, modified OR ignored-python state anywhere in the tree."""
    return not dirty_paths() and not ignored_modules()


#: Which changed modules bear on which capability. A reviewer's question is never "did bytes
#: change" but "what could this have broken", and only a person who read the diff can answer
#: that. This maps a module to the capabilities it participates in, so the package can say which
#: questions a given candidate raises rather than pretending to answer them.
CAPABILITY_MODULES = {
    "api_parity": ("api/app.py", "store/segmented_cube.py", "store/time_cube.py",
                   "store/block_manifest.py", "store/__init__.py"),
    "generation_one": ("store/block_manifest.py", "ingest/publish_manifest.py",
                       "ops/p5_legacy_base.py", "ops/p5_prod_provision.py"),
    "provisioning": ("ops/p5_prod_provision.py", "ops/p5_readiness.py", "ops/quiescence.py"),
    "one_shot_compaction": ("ops/p5_oneshot_compaction.py", "ops/p5_oneshot_exception.py",
                            "ops/p5_parity_producers.py", "ops/p5_legacy_base.py",
                            "ingest/build_block.py", "ingest/publish_manifest.py",
                            "store/segmented_cube.py", "store/time_cube.py",
                            "store/block_manifest.py", "store/repair_wal.py",
                            "store/source_provenance.py", "store/compaction_lock.py"),
}


def compatibility_matrix(candidate: str, baselines: Sequence[str] = ()) -> Dict:
    """Which commits can actually HOST this operation, computed from git blobs.

    Three bindings have to hold at once for a composition to be executable:

        HEAD == deploy_sha and the worktree is clean   -> the files on disk ARE deploy_sha
        the 23 service modules match the deploy approval
        the 21 operation modules match the operation approval

    `deploy_sha` and `approved_sha` need not be the same commit, but a clean checkout of
    `deploy_sha` is what is on disk, and the on-disk bytes are what the operation gate compares
    against `approved_sha`. So the two commits must agree BYTE-FOR-BYTE on all 21 operation
    modules. A deploy commit that does not contain them cannot host the operation however the
    approval is written -- which is a fact about the pair, not a defect in either.

    Reported per commit rather than as a verdict: `absent` and `differs` are the two distinct
    ways a pair fails, and collapsing them would hide that one is "never reviewed here" and the
    other is "reviewed at other bytes"."""
    from ops import p5_readiness as rd
    from ops import p5_oneshot_compaction as oc
    service = tuple(rd.INVENTORY_MODULES)
    operation = tuple(oc.OPERATION_INVENTORY)
    resolved = _git("rev-parse", candidate).strip()
    shas = [_git("rev-parse", s).strip()
            for s in (tuple(baselines) or tuple(rd.REVIEWED_DEPLOY_SHAS))]
    target = digests_at(resolved, sorted(set(service) | set(operation)))

    rows = {}
    for sha in shas:
        here = digests_at(sha, sorted(set(service) | set(operation)))
        absent = [m for m in operation if here[m] is None]
        differs = [m for m in operation if here[m] is not None and here[m] != target[m]]
        service_absent = [m for m in service if here[m] is None]
        service_differs = [m for m in service
                           if here[m] is not None and here[m] != target[m]]
        rows[sha] = {
            "subject": _git("log", "-1", "--format=%s", sha).strip(),
            "operation_modules_absent": absent,
            "operation_modules_differing": differs,
            "operation_modules_identical": [m for m in operation
                                            if here[m] is not None and here[m] == target[m]],
            "service_modules_absent": service_absent,
            "service_modules_differing": service_differs,
            # The whole question, in one field. False means no operation approval can make this
            # pair runnable -- not that the approval is missing, that the code is.
            "can_host_the_operation": not absent and not differs,
            "why_not": (f"{len(absent)} of the {len(operation)} operation modules are absent and "
                        f"{len(differs)} differ" if (absent or differs) else None),
        }
    capabilities = {}
    for name, mods in sorted(CAPABILITY_MODULES.items()):
        touched = sorted({m for sha in shas for m in mods
                          if digests_at(sha, (m,))[m] != target[m]})
        capabilities[name] = touched
    return {
        "candidate": resolved,
        "service_inventory_size": len(service),
        "operation_inventory_size": len(operation),
        "shared_modules": sorted(set(service) & set(operation)),
        "service_only_modules": sorted(set(service) - set(operation)),
        "operation_only_modules": sorted(set(operation) - set(service)),
        "candidate_hosts_the_operation": all(target[m] is not None for m in operation),
        "baselines": rows,
        "capabilities_touched_vs_baselines": capabilities,
        "current_deploy_sha": rd.CURRENT_DEPLOY_SHA,
        "candidate_is_approved": resolved in rd.REVIEWED_DEPLOY_SHAS,
        "approvals_required": [
            "SERVICE DEPLOYMENT APPROVAL -- add the candidate to "
            "p5_readiness.REVIEWED_DEPLOY_SHAS, and separately decide CURRENT_DEPLOY_SHA. This "
            "governs the 23 modules the service imports.",
            "OPERATION APPROVAL -- an external record at "
            f"{oc.OPERATION_APPROVAL_ENV} naming approved_sha and all "
            f"{len(operation)} operation modules. This governs the code the compaction runs.",
            "ASSESSOR APPROVAL -- p5_readiness.APPROVAL_RECORD_PATH, which governs whether the "
            "readiness assessor's own verdicts may be relied on.",
        ],
        "not_done_here": ("This function computes and edits nothing. None of the three "
                          "approvals above is created, moved or implied by generating it."),
    }


def identity_update_plan(sha: str) -> Dict:
    """What approving this candidate would REQUIRE. A plan, not a change.

    Derived rather than remembered: which producer modules are already in the deployment
    inventory decides which of these steps are still outstanding, and that changes commit to
    commit."""
    from ops import p5_readiness as rd
    from ops import p5_oneshot_compaction as oc
    service_inventory = set(rd.INVENTORY_MODULES)
    operation_inventory = list(oc.OPERATION_INVENTORY)
    # Overlap is EXPECTED and is reported as a fact, not a problem. When the operation
    # inventory held one module it looked like the two sets should be disjoint; measuring the
    # real runtime closure showed they share the whole store and ingest layer, because the
    # service and a compaction genuinely run the same code to read and publish. What separates
    # the inventories is which approval authority governs each, not which files are in them.
    shared = sorted(set(operation_inventory) & service_inventory)
    operation_only = sorted(set(operation_inventory) - service_inventory)
    record, approval_problem = oc.operation_approval()
    steps = [
        f"write an OPERATION APPROVAL RECORD outside the repository naming approved_sha={sha} "
        f"and listing all {len(operation_inventory)} modules of the runtime closure, then "
        f"point {oc.OPERATION_APPROVAL_ENV} at it. The allow-list is deliberately NOT in the "
        f"operation: a module that carries its own approval can approve a change to itself",
    ]
    if approval_problem:
        steps.append(f"no usable approval record today: {approval_problem}")
    for module in operation_inventory:
        if rd._git_blob(sha, module) is None:
            steps.append(
                f"NOTE: {module} is not present at {sha}; approving that commit would not "
                f"cover it")
    if operation_only:
        steps.append(
            f"note that {operation_only} run in the operation and are NOT part of the "
            f"service's import closure, so the deploy approval does not cover them; that is "
            f"what the operation approval record is for")
    steps.append(
        "decide SEPARATELY whether this commit belongs in p5_readiness.REVIEWED_DEPLOY_SHAS. "
        "That set is about the code the SERVICE runs; it is not implied by approving operator "
        "tooling, and merging the two would mean re-approving tooling on every service change")
    steps.append("re-run tests/test_phase2_p5_readiness.py: the assessor measures its own "
                 "runtime closure by executing the build, prune_swap and API paths, and "
                 "reports any inventory entry that closure does not reach")
    return {
        "candidate": sha,
        "operation_inventory": operation_inventory,
        "operation_inventory_size": len(operation_inventory),
        "operation_approval": {"configured": bool(oc.OPERATION_APPROVAL_PATH),
                               "problem": approval_problem,
                               "scope": oc.OPERATION_APPROVAL_SCOPE,
                               "env": oc.OPERATION_APPROVAL_ENV},
        "service_inventory_size": len(service_inventory),
        "shared_with_service_inventory": shared,
        "operation_only_modules": operation_only,
        "approved_deploy_shas": list(rd.REVIEWED_DEPLOY_SHAS),
        "operation_modules_present_at_candidate": {
            m: rd._git_blob(sha, m) is not None for m in operation_inventory},
        "steps": steps,
        "not_done_here": ("This function edits nothing. Approving a candidate is a human "
                          "decision recorded outside this repository, and a tool that could "
                          "add its own SHA to the approved set would be the circle the "
                          "assessor's approval record exists to break."),
    }


def manifest(sha: str = "HEAD", *, require_clean: bool = True) -> Dict:
    """Everything a reviewer needs about one candidate commit, derived from that commit."""
    from ops import p5_readiness as rd
    resolved = _git("rev-parse", sha).strip()
    if require_clean:
        dirty = dirty_paths()
        ignored = ignored_modules()
        if dirty or ignored:
            raise DeployCandidateError(
                "the worktree is dirty. A package generated from uncommitted changes describes "
                "a tree that exists nowhere, and its digests could not be recomputed by anyone "
                "else."
                + (f" Untracked or modified: {dirty[:10]}." if dirty else "")
                + (f" IGNORED python modules under {DEV_PREFIX}: {ignored[:10]} -- these are on "
                   f"disk, importable at run time, and in no listing this package produces."
                   if ignored else ""))
    subject = _git("log", "-1", "--format=%s", resolved).strip()
    committed = _git("log", "-1", "--format=%cI", resolved).strip()
    files = tree_files(resolved)
    return {
        "schema": "ghrsst.p5.deploy_candidate/1",
        "candidate_sha": resolved,
        "subject": subject,
        "committed_utc": committed,
        "approved": False,
        "approval_note": ("NOT APPROVED. This package describes a candidate; approving it is a "
                          "human decision recorded outside this repository."),
        "dev_prefix": DEV_PREFIX,
        "file_count": len(files),
        "files": files,
        "deployment_inventory_digests": digests_at(resolved, rd.INVENTORY_MODULES),
        "operation_module_digests": digests_at(resolved, operation_modules()),
        "compatibility_matrix": compatibility_matrix(resolved),
        "changed_vs_current_deploy": changed_files(rd.CURRENT_DEPLOY_SHA, resolved),
        "identity_update_plan": identity_update_plan(resolved),
    }


def changed_files(base: str, candidate: str) -> Dict:
    """Every dev-tree path that differs between two commits, by git's own status letter.

    `--name-status` rather than `--name-only`: a reviewer reading a candidate needs to know
    whether a path was ADDED -- five of the operation modules do not exist at the current deploy
    commit at all -- and a flat list of names cannot say that."""
    out = _git("diff", "--name-status", f"{base}..{candidate}", "--", DEV_PREFIX)
    prefix = DEV_PREFIX + "/"
    groups: Dict[str, List[str]] = {"added": [], "modified": [], "deleted": [], "other": []}
    for line in out.splitlines():
        if not line.strip():
            continue
        status, _, path = line.partition("\t")
        path = path.strip()
        if path.startswith(prefix):
            path = path[len(prefix):]
        key = {"A": "added", "M": "modified", "D": "deleted"}.get(status[:1], "other")
        groups[key].append(path)
    for value in groups.values():
        value.sort()
    rows: Dict = dict(groups)
    rows["total"] = sum(len(v) for v in groups.values())
    rows["base"] = base
    return rows


def write_package(out_dir: str, sha: str = "HEAD", *, require_clean: bool = True) -> Dict:
    """Write the manifest, the diff and an archive of the candidate. Returns what was written.

    Deliberately writes OUTSIDE the repository by default caller convention: a tarball of the
    tree committed into the tree is a second copy that ages, and the archive is regenerable from
    the commit at any time."""
    doc = manifest(sha, require_clean=require_clean)
    resolved = doc["candidate_sha"]
    os.makedirs(out_dir, exist_ok=True)

    manifest_path = os.path.join(out_dir, f"p5_deploy_candidate_{resolved[:12]}.json")
    with open(manifest_path, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)

    diff_path = os.path.join(out_dir, f"p5_deploy_candidate_{resolved[:12]}.diff")
    with open(diff_path, "w") as fh:
        fh.write(_git("show", "--stat", "--patch", resolved))

    archive_path = os.path.join(out_dir, f"p5_deploy_candidate_{resolved[:12]}.tar")
    with open(archive_path, "wb") as fh:
        proc = subprocess.run(("git", "-C", _repo_root(), "archive", "--format=tar",
                               resolved, "--", DEV_PREFIX), capture_output=True)
        if proc.returncode != 0:
            raise DeployCandidateError(f"git archive failed: {proc.stderr.decode()[:200]}")
        fh.write(proc.stdout)

    written = {"manifest": manifest_path, "diff": diff_path, "archive": archive_path}
    # The archive's own digest, so the reviewer can confirm the bytes they received are the
    # bytes this described.
    with open(archive_path, "rb") as fh:
        written["archive_sha256"] = hashlib.sha256(fh.read()).hexdigest()
    written["candidate_sha"] = resolved
    return written
