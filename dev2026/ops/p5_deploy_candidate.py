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

#: The tooling that RUNS a compaction. Distinct from the deployment inventory: these modules
#: are the operation, and the inventory is what the deployment serves. A reviewer needs both,
#: and needs to be told which is which -- conflating them is how a module ends up approved for
#: one purpose and used for another.
OPERATION_MODULES = (
    "ops/p5_oneshot_compaction.py",
    "ops/p5_oneshot_exception.py",
    "ops/p5_parity_producers.py",
    "ops/p5_prod_provision.py",
    "ops/p5_legacy_base.py",
    "ops/p5_readiness.py",
    "ops/p5_deploy_candidate.py",
)


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


def worktree_is_clean() -> bool:
    return not _git("status", "--porcelain").strip()


def identity_update_plan(sha: str) -> Dict:
    """What approving this candidate would REQUIRE. A plan, not a change.

    Derived rather than remembered: which producer modules are already in the deployment
    inventory decides which of these steps are still outstanding, and that changes commit to
    commit."""
    from ops import p5_readiness as rd
    from ops import p5_oneshot_compaction as oc
    inventory = set(rd.INVENTORY_MODULES)
    producers = list(oc.REVIEWED_PRODUCER_MODULES)
    outside = [m for m in producers if m not in inventory]
    steps = [
        f"add {sha} to p5_readiness.REVIEWED_DEPLOY_SHAS",
    ]
    for module in outside:
        steps.append(
            f"decide whether {module} joins p5_readiness.INVENTORY_MODULES. While it does not, "
            f"identity_problem() admits it as an operation-side module verified against DISK "
            f"rather than against the git blob at an approved commit, which is the weaker of "
            f"the two checks")
        steps.append(
            f"if it joins the inventory, remove it from "
            f"p5_oneshot_compaction.REVIEWED_PRODUCER_MODULES -- leaving it in both keeps the "
            f"weaker path alive alongside the stronger one")
    steps.append("re-run tests/test_phase2_p5_readiness.py: the assessor measures its own "
                 "runtime closure and reports a module the readiness path does not import")
    return {
        "candidate": sha,
        "producer_modules": producers,
        "producer_modules_outside_the_deployment_inventory": outside,
        "already_approved_shas": list(rd.REVIEWED_DEPLOY_SHAS),
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
    if require_clean and not worktree_is_clean():
        raise DeployCandidateError(
            "the worktree is dirty. A package generated from uncommitted changes describes a "
            "tree that exists nowhere, and its digests could not be recomputed by anyone else")
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
        "operation_module_digests": digests_at(resolved, OPERATION_MODULES),
        "identity_update_plan": identity_update_plan(resolved),
    }


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
