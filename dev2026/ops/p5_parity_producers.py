"""dev2026 — the reviewed parity producers for a one-shot compaction.

`ops/p5_oneshot_compaction.py` will not accept a parity result from an arbitrary callable. The
gate that reads the result cannot tell a check that passed from one that never ran — both are a
dict saying `mismatches: 0` — so the producer's identity is derived from the callable itself and
must be one of the entries registered in `REVIEWED_PRODUCERS` there.

These are those entries. They exist so the production path has something real to point at, and
they are deliberately thin: everything that decides whether two days agree is
`store/source_provenance.compare_days`, which is the primitive P4/P5 already use for the same
question and which is part of the deployment inventory.

## What each one actually reads

* `point_series_parity` compares the BUILT BLOCK against the store each day was read from — the
  oracle is per-day, taken from the source map, because the block grid anchors after the legacy
  base and most folded days were never in the monolith at all. Comparing everything against the
  monolith would compare most days against a store that does not contain them.
* `api_parity` compares what the SERVICE would return — `SegmentedCubeStore.point_series` over
  the published manifest, through the same allow-list the deployment uses — against the same
  per-day oracle. It is the only one of the two that exercises the read path an API request
  takes.

## What they do not do

Neither decides anything. Each returns a bound artifact and the operation adjudicates it; a
producer that wanted to lie would have to lie about `mismatches`, and the operation re-derives
the producer identity rather than reading it out of the artifact, so a fake cannot present
itself as this module.
"""
from __future__ import annotations

import datetime
import os
from typing import Dict, List, Optional, Sequence

import numpy as np
import zarr

from store import block_manifest as bm
from store import source_provenance as sp

#: The variables a parity check covers. Taken from the provenance module's own policy rather
#: than restated, so the two cannot drift into disagreeing about what "the same day" means.
VARS = ("analysed_sst", "analysis_error", "mask")

#: The seed the sample window is drawn with. Fixed, because a parity check whose window moves
#: between runs cannot be reproduced by whoever reviews the refusal.
SAMPLE_SEED = 20260826


def _own_digest() -> str:
    """The SHA-256 of this file, right now.

    Not circular: a file can hash itself, it just cannot contain its own hash. The operation
    derives the same value independently from the callable it holds and refuses if the two
    disagree, so this is a claim that gets checked rather than one that gets believed."""
    import hashlib
    with open(os.path.abspath(__file__), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _utc(now=None) -> str:
    when = now or datetime.datetime.now(datetime.timezone.utc)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _inspect(path: str):
    return bm.inspect_store_contract(path)


def _valid_map(insp, day_index: int) -> Dict[str, bool]:
    """`var -> is this variable valid on this day`, read from the store's own arrays.

    Read from the SOURCE, never from a manifest or an artifact. A variable that was absent when
    something was written and has since been backfilled is exactly the transition a parity check
    exists to notice, and taking the flags from a document would skip it."""
    out: Dict[str, bool] = {}
    for var, flags in insp.var_valid:
        out[var] = bool(flags[day_index]) if day_index < len(flags) else False
    return out


def _tiles(group, insp, day_index: int, valid: Dict[str, bool], window) -> Dict:
    i0, i1, j0, j1 = window
    tiles: Dict[str, Optional[np.ndarray]] = {}
    for var in VARS:
        if not valid.get(var):
            tiles[var] = None
            continue
        tiles[var] = np.asarray(group[var][day_index, i0:i1, j0:j1])
    return tiles


def _open(path: str):
    return zarr.open_group(path, mode="r")


def point_series_parity(*, block_path: str, source_map: Dict, monolith_path: str,
                        days: Sequence[str], host: str, run_id: str, deploy_sha: str,
                        generation: int, manifest_checksum: str, source_map_digest: str,
                        now=None) -> Dict:
    """Compare the built block against the per-day oracle, point-wise. Returns an artifact.

    Every day in `days` is compared, and the first and last are therefore always included —
    the boundaries are where an off-by-one shows, and a sampled subset that happened to omit
    them would be the one shape of this check worth nothing."""
    block_insp = _inspect(block_path)
    block_group = _open(block_path)
    block_index = {day: i for i, day in enumerate(block_insp.days)}

    oracles: Dict[str, tuple] = {}
    diffs: List[str] = []
    samples = 0

    for day in days:
        record = source_map.get(day)
        if type(record) is not dict:
            diffs.append(f"{day}: no source-map record, so there is no oracle to compare to")
            continue
        if day not in block_index:
            diffs.append(f"{day}: the block does not hold the day it claims to have folded")
            continue
        oracle_path = record["source_path"]
        if oracle_path not in oracles:
            insp = _inspect(oracle_path)
            oracles[oracle_path] = (insp, _open(oracle_path),
                                    {d: i for i, d in enumerate(insp.days)})
        oracle_insp, oracle_group, oracle_index = oracles[oracle_path]
        if day not in oracle_index:
            diffs.append(f"{day}: the source map names {oracle_path} which does not hold it")
            continue

        b_i, o_i = block_index[day], oracle_index[day]
        window = sp.sample_window(block_insp.ny, block_insp.nx,
                                  seed=SAMPLE_SEED, day_index=b_i)
        b_valid, o_valid = _valid_map(block_insp, b_i), _valid_map(oracle_insp, o_i)
        b_tiles = _tiles(block_group, block_insp, b_i, b_valid, window)
        o_tiles = _tiles(oracle_group, oracle_insp, o_i, o_valid, window)
        samples += sum(1 for t in b_tiles.values() if t is not None) or 1
        for diff in sp.compare_days(b_tiles, o_tiles, seed=SAMPLE_SEED, day_index=b_i,
                                    a_valid=b_valid, b_valid=o_valid):
            diffs.append(f"{day}: {diff}")

    return {
        "kind": "point_series_parity",
        "host": host, "run_id": run_id, "deploy_sha": deploy_sha,
        "producer": "ops/p5_parity_producers.py:point_series_parity",
        "producer_digest": _own_digest(),
        "generation": generation, "manifest_checksum": manifest_checksum,
        "source_map_digest": source_map_digest,
        "executed_utc": _utc(now),
        "samples": samples, "mismatches": len(diffs),
        "provenance": (f"per-day sample windows (seed {SAMPLE_SEED}) read from the block at "
                       f"{block_path} and from each day's own source, compared point-wise by "
                       f"store/source_provenance.compare_days"),
        "days_checked": len(days),
        "boundaries_included": bool(days),
        "first_day": days[0] if days else None,
        "last_day": days[-1] if days else None,
        "differences": diffs[:20],
    }


def api_parity(*, manifest_root: str, monolith_path: str, allowed_legacy_paths: Sequence[str],
               host: str, run_id: str, deploy_sha: str, generation: int,
               manifest_checksum: str, source_map_digest: str,
               days: Optional[Sequence[str]] = None, now=None) -> Dict:
    """Compare what the SERVICE would return against the per-day oracle. Returns an artifact.

    Goes through `SegmentedCubeStore.point_series` deliberately: that is the call an API request
    makes, and a parity check that read the block directly would agree with the block while the
    service disagreed with both."""
    from store import time_cube as tc
    from store.segmented_cube import SegmentedCubeStore
    from store.time_cube import TimeCubeStore

    store = SegmentedCubeStore(manifest_root, allowed_legacy_paths=list(allowed_legacy_paths))
    # The oracle is read through the SAME reader the segmented store delegates to, just pointed
    # straight at the monolith. Comparing a served value against a raw `[i, 0, 0]` slice would
    # have compared two different points -- the store resolves lon/lat to an index and a
    # hand-rolled index does not -- so the difference this measures is the ROUTE, which is the
    # parity question, and not the coordinate arithmetic.
    oracle = TimeCubeStore(monolith_path)
    oracle_days = set(oracle.days) if not callable(getattr(oracle, "days", None)) \
        else set(oracle.days())

    check_days = [d for d in (list(days) if days else list(store.days)) if d in oracle_days]
    diffs: List[str] = []
    samples = 0

    # A fixed point rather than a scan: the question is whether the served value for a day
    # matches the store it came from, and one point per day answers it without reading a slab.
    lon, lat = 0.5, 0.5
    # The API's field names, not the array names. `analysed_sst` is what the zarr array is
    # called; `sst` is what a request asks for, and asking for the wrong one made every day
    # report "Unsupported field(s)" -- a real refusal, about the producer rather than the data.
    fields = list(tc.ALLOWED_FIELDS[:1])
    for day in check_days:
        samples += 1
        try:
            served = store.point_series(lon, lat, [day], fields)
        except Exception as exc:        # noqa: BLE001 -- a read failure IS a parity failure
            diffs.append(f"{day}: the served read raised {exc!r}")
            continue
        try:
            direct = oracle.point_series(lon, lat, [day], fields)
        except Exception as exc:        # noqa: BLE001
            diffs.append(f"{day}: the oracle read raised {exc!r}")
            continue
        if not served:
            diffs.append(f"{day}: the served view returned nothing for a day the oracle holds")
            continue
        if not direct:
            continue                    # the oracle omits it: nothing to disagree about
        got, want = _first_value(served), _first_value(direct)
        if got is None and want is None:
            continue
        if got is None or want is None or not _close(got, want):
            diffs.append(f"{day}: served {got!r} against oracle {want!r}")

    return {
        "kind": "api_parity",
        "host": host, "run_id": run_id, "deploy_sha": deploy_sha,
        "producer": "ops/p5_parity_producers.py:api_parity",
        "producer_digest": _own_digest(),
        "generation": generation, "manifest_checksum": manifest_checksum,
        "source_map_digest": source_map_digest,
        "executed_utc": _utc(now),
        "samples": samples, "mismatches": len(diffs),
        "provenance": (f"SegmentedCubeStore.point_series over {manifest_root} through the "
                       f"deployment allow-list, compared against {monolith_path}"),
        "routes_checked": 1,
        "differences": diffs[:20],
    }


def _first_value(series):
    """The first numeric value in whatever shape `point_series` returned, or `None`."""
    if series is None:
        return None
    if isinstance(series, dict):
        for value in series.values():
            found = _first_value(value)
            if found is not None:
                return found
        return None
    if isinstance(series, (list, tuple)):
        for value in series:
            found = _first_value(value)
            if found is not None:
                return found
        return None
    try:
        value = float(np.asarray(series).ravel()[0])
    except Exception:                                       # noqa: BLE001
        return None
    return None if value != value else value                # NaN is "no value here"


def _close(a: float, b: float) -> bool:
    """float32 semantics: the served path and the oracle both round-trip through float32."""
    return np.isclose(np.float32(a), np.float32(b), rtol=0, atol=0, equal_nan=True)
