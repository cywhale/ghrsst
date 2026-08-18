"""P5-S5 Part 3 §7.9 — build a quiescence attestation the swap executor will accept.

This lives in the codebase rather than inside the runbook's markdown for one reason: the first
version of it *was* runbook markdown, and a runbook cannot be tested. The review that followed
found both a wrong liveness check and a set of missing executor arguments that no test could
have seen, because nothing imported the code they were written in.

## Why "0 online workers" is not a drain

`pm2 jlist` reports a worker's PM2 **status**. A worker moving through `stopping` / `stopped`
leaves the `online` set immediately, while the OS process is still there, still finishing the
request it had already accepted. That is exactly the state `(e2)` must exclude: a reader alive
across the delta path switch. Counting `online` workers answers a question about PM2's
bookkeeping, not about whether anyone is still reading the delta.

So the measurement here is: capture **every** worker pid BEFORE the stop, then prove each of
those pids is **gone** afterwards — `os.kill(pid, 0)` raising `ProcessLookupError` — and only
then look at the port and at PM2's own list. A pid that is merely no longer `online` is counted
as in-flight, and the attestation is refused.
"""
from __future__ import annotations

import os
import sys
from typing import Callable, Dict, Iterable, List, Optional, Sequence

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from store import durable_jsonl  # noqa: E402

#: PM2 statuses that mean "this worker is winding down but the process may still be serving".
DRAINING_STATUSES = ("stopping", "launching", "one-launch-status")


class NotQuiesced(RuntimeError):
    """The drain could not be proven. Raised so the swap executor sees a failed hook -- which
    leaves the live delta untouched -- rather than a fabricated attestation."""


def pid_alive(pid: int) -> bool:
    """Does this pid still exist? Signal 0 checks for existence without delivering anything.

    `PermissionError` means the process exists and belongs to someone else: alive, and a state
    worth failing on rather than silently reading as gone."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def worker_pids(jlist: Sequence[dict], app: str) -> List[int]:
    """EVERY pid PM2 knows for this app, whatever its status.

    Deliberately not filtered to `online`: the pids that matter most are the ones that just left
    that set. A pid of 0/None means PM2 has no process, so there is nothing to outlive."""
    out = []
    for proc in jlist:
        if proc.get("name") != app:
            continue
        pid = proc.get("pid")
        if isinstance(pid, int) and pid > 0:
            out.append(pid)
    return out


def live_statuses(jlist: Sequence[dict], app: str) -> List[str]:
    """Statuses PM2 reports for this app that are not a settled stop."""
    return [p.get("pm2_env", {}).get("status") for p in jlist
            if p.get("name") == app
            and p.get("pm2_env", {}).get("status") in ("online",) + DRAINING_STATUSES]


def attest_drain(*, app: str, before_pids: Iterable[int], jlist_after: Sequence[dict],
                 port_open: bool, attestation_id: str, checked_utc: str,
                 alive: Callable[[int], bool] = pid_alive,
                 extra_evidence: Optional[str] = None) -> Dict[str, object]:
    """Prove the drain, or raise. Returns an attestation `execute_swap_plan` accepts.

    `before_pids` must be captured BEFORE the stop: the whole point is to outlive PM2's own
    bookkeeping. `attestation_id` is the join key -- the ops-side evidence file and the
    executor's manifest both record it, so two records can be matched after a retry instead of
    guessed at by order."""
    if not attestation_id or not isinstance(attestation_id, str):
        raise NotQuiesced("attestation_id must be a non-empty string: it is the only thing that "
                          "ties the ops evidence to the executor's record after a retry")
    survivors = sorted(p for p in dict.fromkeys(before_pids) if alive(p))
    draining = live_statuses(jlist_after, app)
    still_listed = worker_pids(jlist_after, app)
    inflight = len(survivors)
    if survivors:
        raise NotQuiesced(
            f"{inflight} pre-stop worker pid(s) still exist after the stop: {survivors}. PM2 may "
            f"already have dropped them from `online`, but the OS process is there and may still "
            f"be serving the request it accepted before the stop -- the (e2) state itself")
    if draining:
        raise NotQuiesced(f"PM2 still reports {app} in {draining}; not a settled stop")
    if port_open:
        raise NotQuiesced(f"the {app} port still accepts connections after the stop")
    if still_listed:
        raise NotQuiesced(
            f"PM2 lists new pid(s) {still_listed} for {app} after the stop -- something restarted "
            f"it, and a fresh worker can open the delta before the path switch")
    return {
        "drained": True,
        "observed_inflight": 0,
        "attestation_id": attestation_id,
        "evidence": (f"pm2 stop {app}; {len(list(dict.fromkeys(before_pids)))} pre-stop pid(s) "
                     f"verified gone via kill(pid, 0); no online/draining worker in pm2 jlist; "
                     f"port refused" + (f"; {extra_evidence}" if extra_evidence else "")),
        "checked_utc": checked_utc,
    }


def write_evidence(hold_dir: str, attestation: Dict[str, object], *,
                   run_id: Optional[str] = None) -> str:
    """Record the ops-side copy of the attestation, durably, BEFORE the swap proceeds.

    Uses the same writer as the executor's manifest (`store.durable_jsonl`) so both get the file
    fsync AND the first-create directory fsync. The runbook's own version of this had the file
    sync and not the directory one: a machine dying between the evidence write and the
    executor's first manifest record could come back with the record durable and its dirent
    gone. The two records are joined by `attestation_id`, not by their order."""
    path = os.path.join(hold_dir, "quiescence_evidence.jsonl")
    record = dict(attestation)
    if run_id is not None:
        record["swap_run_id"] = run_id
    durable_jsonl.append(path, record, dir_fd_path=hold_dir)
    return path
