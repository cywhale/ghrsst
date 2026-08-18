"""One durable append-only JSONL writer, shared by everything that records evidence.

Both the swap executor's `manifest.jsonl` and the ops-side `quiescence_evidence.jsonl` are
append-only audit logs whose whole purpose is to survive the failure they describe. They were
written twice, and the second copy — the one living in the runbook's markdown — got the
directory `fsync` wrong, because a copy in a document cannot be tested (P5-S5 Part 3, review
round 4).

Two syncs are needed, not one:

- **the file**, so the record itself is on the medium rather than in the page cache;
- **the containing directory, on first create**, so the *dirent* is on the medium too. Without
  it a machine that dies just after the write can come back with the record durable and the
  file it lives in absent from its directory — the log is gone, and nothing says so.
"""
from __future__ import annotations

import json
import os
from typing import Optional


def append(path: str, record: dict, *, dir_fd_path: Optional[str] = None) -> None:
    """Append one JSON record, fsync'd; fsync the directory when the file is created.

    `dir_fd_path` defaults to the file's own directory. Keys are sorted so two records of the
    same event are byte-comparable across processes."""
    directory = dir_fd_path or os.path.dirname(os.path.abspath(path))
    fresh = not os.path.exists(path)
    with open(path, "a") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    if fresh:
        dfd = os.open(directory, os.O_RDONLY | os.O_CLOEXEC)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
