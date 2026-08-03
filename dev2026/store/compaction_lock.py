"""dev2026 — P5-S3: the compaction lock (design spec §7.1b).

A block build runs for hours **outside** the ingest lock and reads the live delta. Meanwhile
`prune_delta` / `swap_delta` **retarget the delta path**. That is exactly the P4-S8a
configuration — a live handle whose path content changes underneath it — except the
consequence is worse: the wrong bytes would be frozen into a **new immutable block** and
published as history.

Holding the ingest lock for the whole build is not an option; it would block the twice-daily
delta append for hours. So compaction takes a **separate, dedicated `flock`**:

* the builder holds `LOCK_EX` on `p5_compaction.lock` for the entire build;
* delta prune / swap / repair try `LOCK_EX | LOCK_NB` on the same file and **refuse** on
  contention — non-blocking, never a blocking wait;
* the daily delta append does **not** take this lock and continues normally;
* publication additionally takes the short ingest lock.

Why a kernel `flock` rather than a renewable TTL lease: a lease cannot fence a **paused**
holder. It expires, a takeover legitimately swaps the delta, and the holder resumes and renews.
`flock` has no renew path — validity is "this process still exists and still holds the fd" — so
a paused holder keeps the lock (correct) and a dead one releases it (also correct).

**The residual hazard is the lock FILE, not the lock.** `rm` + recreate produces a new inode
that another process can lock freely while we still hold a lock on the orphaned one. That is
the only way two writers can coexist here, so publication asserts the fd is still held **and**
that the path still resolves to the same inode.
"""
from __future__ import annotations

import errno
import fcntl
import json
import os
from typing import Optional


class CompactionLockError(Exception):
    """The compaction lock could not be acquired, or is no longer trustworthy."""


class CompactionLockBusy(CompactionLockError):
    """Another holder has it. Callers must REFUSE, not wait."""


class CompactionLock:
    """Exclusive, non-reentrant, released by the kernel on process exit.

    Usage is a context manager for the whole build:

        with CompactionLock(path, holder="p5-compaction/<run_id>") as lock:
            ...build...
            lock.assert_still_held()      # immediately before publishing
    """

    def __init__(self, path: str, holder: str = "", status_path: Optional[str] = None):
        self.path = path
        self.holder = holder
        # Observability ONLY. Nothing may branch on this file: it is for a human reading
        # `ps`, not for code deciding whether the lock is held.
        self.status_path = status_path
        self._fd: Optional[int] = None
        self._ino: Optional[int] = None

    # ---- acquire / release ----------------------------------------------
    def acquire(self) -> "CompactionLock":
        os.makedirs(os.path.dirname(os.path.abspath(self.path)) or ".", exist_ok=True)
        # O_CLOEXEC: a child process must NOT inherit this fd. If it did, killing the parent
        # would leave the lock held by the child, contradicting "process death releases it"
        # (§7.1b-1). Only the supervisor owns the lock.
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
                raise CompactionLockBusy(
                    f"compaction lock {self.path} is held by another process; refusing "
                    f"rather than waiting") from exc
            raise CompactionLockError(f"cannot lock {self.path}: {exc}") from exc
        self._fd = fd
        self._ino = os.fstat(fd).st_ino
        if self.status_path:
            self._write_status()
        return self

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None
        if self.status_path and os.path.exists(self.status_path):
            try:
                os.remove(self.status_path)
            except OSError:                       # observability only; never fail on it
                pass

    def __enter__(self) -> "CompactionLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()

    # ---- the fencing check ----------------------------------------------
    @property
    def held(self) -> bool:
        return self._fd is not None

    def assert_still_held(self) -> None:
        """Call immediately before publishing.

        Two conditions, both necessary: the fd is still open and held by us, **and** the lock
        path still resolves to the same inode. If someone `rm`'d and recreated the file, a
        second process can hold a lock on the new inode while we hold one on the orphaned
        old inode — the only two-writer state reachable here."""
        if self._fd is None:
            raise CompactionLockError(
                "compaction lock is not held; refusing to publish a block built without it")
        try:
            live_ino = os.stat(self.path).st_ino
        except FileNotFoundError as exc:
            raise CompactionLockError(
                f"compaction lock file {self.path} was deleted while held; another process "
                f"can now lock a fresh inode. Refusing to publish.") from exc
        if live_ino != self._ino:
            raise CompactionLockError(
                f"compaction lock file {self.path} was replaced while held (inode "
                f"{self._ino} -> {live_ino}); another process may hold the new inode. "
                f"Refusing to publish.")

    def _write_status(self) -> None:
        try:
            with open(self.status_path, "w") as fh:
                json.dump({"holder": self.holder, "pid": os.getpid(),
                           "lock_path": os.path.abspath(self.path),
                           "note": "observability only -- no code may branch on this file"},
                          fh, indent=2, sort_keys=True)
        except OSError:
            pass


def refuse_if_compaction_running(lock_path: str, *, operation: str) -> Optional[dict]:
    """The gate `prune_delta` / `swap_delta` / repair call BEFORE touching the delta.

    Returns a refusal dict when a build holds the lock, else `None`. Non-blocking by design:
    a prune that waited hours for a build would look like a hang, and the operator needs to
    reschedule rather than block."""
    if not os.path.exists(lock_path):
        return None
    fd = os.open(lock_path, os.O_RDWR | os.O_CLOEXEC)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK):
            status = {}
            side = lock_path.replace(".lock", ".status.json")
            if os.path.isfile(side):
                try:
                    with open(side) as fh:
                        status = json.load(fh)
                except (OSError, ValueError):
                    status = {}
            return {"status": "refused", "reason": "compaction_lock_held",
                    "operation": operation, "lock_path": lock_path, "holder": status,
                    "hint": ("a block build is reading the live delta; retargeting the delta "
                             "path now would freeze wrong bytes into an immutable block. "
                             "Reschedule after the build completes.")}
        raise
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)
