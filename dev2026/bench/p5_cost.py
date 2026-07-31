"""dev2026 — P5-S0: shared, self-validating measurement primitives.

The P5 design spec (`specs/p5_segmented_timecube_compaction_design.md`) carries numbers
tagged **[PROBE]** that came from throwaway scratch scripts. §14 P5-S0 requires them to be
re-derived by a COMMITTED harness before any P5 step may advance. This module holds the
primitives that harness is built from, kept separate so they can be unit-tested on their
own (`tests/test_phase2_p5s0.py`).

Two counter families, deliberately distinct:

  * **CACHE-INDEPENDENT (analytic)** — `point_column_cost` derives `chunk_count` /
    `decompressed_bytes` / `read_amplification` from the chunk geometry, the P2 method.
    These are the architectural discriminators: they do not move with page cache or CPU.
    Geometry is ALWAYS read back from disk (`read_geometry`), never taken from the build
    parameters, so a fixture that silently differs from its declaration is caught.

  * **OBSERVED** — `CountingLocalStore` counts real store-level reads, and
    `snapshot_tree`/`changed_files` detect real file rewrites by CONTENT DIGEST.

Why digests and not (mtime, size): a shard rewrite can preserve both. An (mtime, size)
snapshot would then report "0 bytes rewritten" and fabricate a passing H1/H2 result --
exactly the kind of false green this phase cannot afford. Digesting is affordable because
S0 fixtures are small; production-scale runs use the same code on the same small fixtures.
"""
from __future__ import annotations

import hashlib
import os
import time
from typing import Dict, Iterable, Optional, Sequence, Tuple

import numpy as np
import zarr
from zarr.storage import LocalStore

DIGEST_CHUNK = 1 << 20


# --------------------------------------------------------------------------- file-level observation
def _digest(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(DIGEST_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def snapshot_tree(path: str) -> Dict[str, Tuple[str, int]]:
    """{relpath: (sha256, size)} for every file under `path`.

    Content-addressed on purpose -- see the module docstring on why (mtime, size) is not
    sufficient to detect a shard rewrite."""
    out: Dict[str, Tuple[str, int]] = {}
    for root, _, files in os.walk(path):
        for f in files:
            fp = os.path.join(root, f)
            out[os.path.relpath(fp, path)] = (_digest(fp), os.path.getsize(fp))
    return out


def changed_files(before: Dict[str, Tuple[str, int]],
                  after: Dict[str, Tuple[str, int]]) -> Tuple[int, int]:
    """(count, bytes) of files that are new or whose CONTENT changed."""
    changed = [k for k, v in after.items() if before.get(k) != v]
    return len(changed), sum(after[k][1] for k in changed)


def tree_size(path: str) -> Tuple[int, int]:
    """(file_count, total_bytes)."""
    n = b = 0
    for root, _, files in os.walk(path):
        for f in files:
            n += 1
            b += os.path.getsize(os.path.join(root, f))
    return n, b


# --------------------------------------------------------------------------- store-level observation
class CountingLocalStore(LocalStore):
    """A `LocalStore` that records every key it is asked to read.

    Used to validate the ANALYTIC `chunk_count` against the chunk keys a real read
    actually touches (`tests/test_phase2_p5s0.py`), and to report the per-segment
    store-call overhead that H3 predicts."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.read_keys: list = []

    def with_read_only(self, read_only: bool = False):
        """`zarr.open_group(store, mode='r')` calls this, and `LocalStore.with_read_only`
        builds a FRESH `type(self)` -- which would silently start a new, empty counter and
        make every observed count 0. Share the list so reads through the derived store are
        still attributed to this one."""
        new = type(self)(root=self.root, read_only=read_only)
        new.read_keys = self.read_keys
        return new

    async def get(self, key, prototype=None, byte_range=None):
        self.read_keys.append(key)
        return await super().get(key, prototype, byte_range)

    def reset(self):
        """Clear IN PLACE. Rebinding (`self.read_keys = []`) would orphan the list shared
        with any store returned by `with_read_only`, so every later read would be recorded
        on a list nobody inspects -- silently reporting 0 observed reads."""
        self.read_keys.clear()

    @property
    def read_count(self) -> int:
        return len(self.read_keys)


def observed_data_keys(store: CountingLocalStore) -> set:
    """Distinct CHUNK keys touched.

    Zarr v3 chunk keys carry the `/c/` chunk-grid prefix (`sst/c/0/1/0`). Opening a group
    also probes v2 metadata (`.zgroup`, `.zattrs`, `.zmetadata`) and v3 `zarr.json`;
    matching on `/c/` selects data and excludes all of it, rather than blacklisting
    metadata names one at a time and silently miscounting when a new one appears."""
    return {k for k in store.read_keys if "/c/" in k}


# --------------------------------------------------------------------------- geometry (from disk)
def read_geometry(path: str, var: str = "sst") -> dict:
    """Read shape/chunks/shards BACK FROM DISK. Never trust build parameters."""
    g = zarr.open_group(path, mode="r")
    arr = g[var]
    shards = getattr(arr, "shards", None)
    return {"shape": tuple(int(x) for x in arr.shape),
            "chunks": tuple(int(x) for x in arr.chunks),
            "shards": tuple(int(x) for x in shards) if shards else None,
            "dtype": str(arr.dtype)}


def assert_geometry(path: str, var: str, *, chunks: Sequence[int],
                    shards: Optional[Sequence[int]] = None,
                    shape: Optional[Sequence[int]] = None) -> dict:
    """Fail loudly if the fixture on disk is not the fixture we think we built."""
    geom = read_geometry(path, var)
    assert tuple(geom["chunks"]) == tuple(chunks), \
        f"chunks on disk {geom['chunks']} != declared {tuple(chunks)}"
    if shards is not None:
        assert geom["shards"] == tuple(shards), \
            f"shards on disk {geom['shards']} != declared {tuple(shards)}"
    if shape is not None:
        assert tuple(geom["shape"]) == tuple(shape), \
            f"shape on disk {geom['shape']} != declared {tuple(shape)}"
    return geom


# --------------------------------------------------------------------------- analytic cost
def point_column_cost(geom: dict, *, t0: int, t1: int, ii: int, jj: int) -> dict:
    """Cache-independent cost of reading `arr[t0:t1, ii, jj]` (one variable).

    chunk_count            = number of chunks the time column intersects
    decompressed_bytes     = sum of the (clipped) byte size of those chunks
    useful_bytes           = (t1-t0) * 4        (float32 values actually wanted)
    read_amplification     = decompressed / useful
    """
    shape = tuple(geom["shape"])
    ct, cy, cx = tuple(geom["chunks"])
    nt, ny, nx = shape
    t0 = max(0, t0)
    t1 = min(nt, t1)
    if t1 <= t0:
        return {"chunk_count": 0, "decompressed_bytes": 0, "useful_bytes": 0,
                "read_amplification": None}
    first, last = t0 // ct, (t1 - 1) // ct
    ci, cj = ii // cy, jj // cx
    ext_y = min(cy, ny - ci * cy)
    ext_x = min(cx, nx - cj * cx)
    total = 0
    for blk in range(first, last + 1):
        ext_t = min(ct, nt - blk * ct)
        total += ext_t * ext_y * ext_x * 4
    n_chunks = last - first + 1
    useful = (t1 - t0) * 4
    return {"chunk_count": n_chunks,
            "decompressed_bytes": int(total),
            "useful_bytes": int(useful),
            "read_amplification": round(total / useful, 1) if useful else None}


# --------------------------------------------------------------------------- timing
def timeit_ms(fn, repeats: int = 15) -> dict:
    ts = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000.0)
    ts.sort()
    return {"repeats": repeats,
            "p50_ms": round(ts[len(ts) // 2], 3),
            "p95_ms": round(ts[min(len(ts) - 1, int(len(ts) * 0.95))], 3),
            "min_ms": round(ts[0], 3),
            "max_ms": round(ts[-1], 3)}


# --------------------------------------------------------------------------- gate
def evaluate_s0_gate(results: dict) -> dict:
    """P5-S0 gate (spec §14): H1 and H2 must be REPRODUCED by this committed harness.

    Deliberately written so it can FAIL -- `tests/test_phase2_p5s0.py` feeds it a
    fabricated low-amplification result and requires a FAIL verdict. A gate that cannot
    fail certifies nothing."""
    failures = []
    h1 = results.get("h1") or {}
    h2 = results.get("h2") or {}
    h3 = results.get("h3") or {}
    if not h1.get("h1_confirmed"):
        failures.append(
            f"H1 NOT reproduced: single-day overwrite amplification "
            f"{h1.get('amplification_vs_one_day')} (expected ~time_chunk-fold); "
            f"candidate A must be re-opened and spec §3.A rewritten")
    if not h2.get("h2_confirmed"):
        failures.append(
            "H2 NOT reproduced: appending into a partial tail shard did not rewrite the "
            "whole shard; §7 may simplify to incremental extension")
    if h3 and not h3.get("h3_supported"):
        ctc = h3.get("constant_time_chunk") or {}
        failures.append(
            "H3 NOT supported: at CONSTANT inner time chunk, segmentation moved "
            f"chunk_count (invariant={ctc.get('chunk_count_invariant')}) or "
            f"decompressed_bytes (invariant={ctc.get('decompressed_bytes_invariant')}); "
            "if decompressed_bytes grows with segmentation, block sizes < 90 d are vetoed "
            "outright (spec §2 H3)")
    return {"verdict": "FAIL" if failures else "PASS",
            "failures": failures,
            "checked": ["H1", "H2"] + (["H3"] if h3 else [])}
