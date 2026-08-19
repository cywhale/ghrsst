"""dev2026 — P5-S7: concurrency, generation lifecycle and the disk gate, through real HTTP.

**LOCAL AND SYNTHETIC ONLY (`LOCAL_SYNTHETIC`).** No VM24, no `GHRSST_*` production store, no
cron/PM2/systemd/NGINX, no prune/swap/compaction/repair/hold cleanup, no production deletion.
Resource figures describe *this test process on this machine* and are not VM24 capacity or
production-performance evidence.

## "Concurrency" here means simultaneous in-flight HTTP requests

It is written `concurrency` throughout and never `C`. The spec's `C` is the **fold cadence**
(`C = 30` days), a completely unrelated quantity that happens to share the letter. `C = 16` in a
load table and `C = 30` in a sizing table would otherwise read as the same axis.

## What the resource numbers can and cannot show

RSS, file descriptors and thread count are sampled from **one process** — the test process,
which also runs the client. They can show *unbounded growth* across a sustained run, which is
what G5/G6 actually ask for. They cannot establish a production memory or fd budget: there is one
worker, a fixture-sized store, and the client's own allocations are inside the same RSS.
"""
from __future__ import annotations

import gc
import json
import os
import statistics
import sys
import threading
import time
import unittest

import numpy as np
import zarr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import p5_fixtures as fx  # noqa: E402
from store import block_manifest as bm  # noqa: E402
from ingest.build_block import disk_precheck  # noqa: E402
from test_phase2_p5s7_http import (  # noqa: E402
    _AppCase, _segment, BLOCK_DAYS, DELTA_DAYS, LAT, LON, NY, NX, N_BLOCKS,
)

#: Simultaneous in-flight HTTP requests. NOT the fold cadence C=30 (see the module docstring).
CONCURRENCY_LEVELS = (1, 4, 8, 16)

#: A real reserve, deliberately never 0. A gate that always passes measures nothing; earlier P5
#: work published a "gate" whose reserve had been zeroed to let a fixture run.
GATE_RESERVE_BYTES = 256 * 1024 * 1024


def _read_bytes(path: str) -> bytes:
    """Context-managed read. `open(...).read()` leaves the handle to the garbage collector,
    which emits a ResourceWarning from a finalizer — where it is *printed* rather than raised,
    so the run still reports OK while stderr is dirty."""
    with open(path, "rb") as fh:
        return fh.read()


def _open_fds() -> int:
    """Open descriptors for THIS process. `/dev/fd` on macOS, `/proc/self/fd` on Linux."""
    for d in ("/proc/self/fd", "/dev/fd"):
        if os.path.isdir(d):
            try:
                return len(os.listdir(d))
            except OSError:
                continue
    return -1


def _rss_mb():
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1e6
    except Exception:
        try:
            with open("/proc/self/statm") as fh:
                return int(fh.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
        except Exception:
            return None


class _LoadCase(_AppCase):
    """Adds a mixed request generator and a resource sampler."""

    def _requests(self):
        """point / range / bbox / POST, all against days the oracle can independently check."""
        f = self.fx
        return [
            ("point", dict(lon0=LON, lat0=LAT, start=f.base_days[3], end=f.base_days[3])),
            ("range", dict(lon0=LON, lat0=LAT, start=f.base_days[0], end=f.base_days[-1])),
            ("crossing", dict(lon0=LON, lat0=LAT, start=f.base_days[-5],
                              end=f.delta_days[-1])),
            ("delta", dict(lon0=LON, lat0=LAT, start=f.delta_days[0], end=f.delta_days[-1])),
            # bbox and POST share the process with the cube reads. Leaving them out would have
            # measured a point/range-only load while the `_issue` branches for them sat unused.
            ("bbox", dict(lon0=LON, lat0=LAT, lon1=LON + 2, lat1=LAT + 2,
                          start=f.delta_days[-1], end=f.delta_days[-1])),
            ("post", {"date": f.delta_days[-1], "points": [[LON, LAT]]}),
        ]

    def _issue(self, kind, params):
        """One request, fully verified against the independent oracle."""
        if kind == "bbox":
            r = self.client.get("/api/ghrsst", params=params)
            self.assertEqual(r.status_code, 200, r.text)
            rows = json.loads(r.content)
            got = {(round(p["lon"], 3), round(p["lat"], 3)): p.get("sst") for p in rows}
            self.assertAlmostEqual(got[(round(LON, 3), round(LAT, 3))],
                                   self.fx.expect(params["start"]), places=4)
            return r
        if kind == "post":
            r = self.client.post("/api/ghrsst/points", json=params)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertAlmostEqual(r.json()[0]["sst"],
                                   self.fx.expect(params["date"]), places=4)
            return r
        r = self.client.get("/api/ghrsst", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.headers["X-Store-Route"], "cube")
        rows = r.json()
        want = fx.calendar_span(params["start"], params["end"])
        self.assertEqual([x["date"] for x in rows], want)          # G2 order + completeness
        for x in rows:
            self.assertIsNotNone(x["sst"], "a fabricated null")
            self.assertAlmostEqual(x["sst"], self.fx.expect(x["date"]), places=4)
        return r

    def _run_load(self, concurrency, rounds=6):
        """`concurrency` threads x `rounds` passes over the mixed request set."""
        errors, done = [], []
        reqs = self._requests()

        def worker(wid):
            try:
                for n in range(rounds):
                    kind, params = reqs[(wid + n) % len(reqs)]
                    self._issue(kind, params)
                    done.append(1)
            except BaseException as e:                     # noqa: BLE001 - reported, not hidden
                errors.append(f"{type(e).__name__}: {e}")

        ts = [threading.Thread(target=worker, args=(i,)) for i in range(concurrency)]
        t0 = time.perf_counter()
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=180)
        wall = time.perf_counter() - t0
        self.assertFalse(errors, f"errors under concurrency={concurrency}: {errors[:3]}")
        self.assertEqual(len(done), concurrency * rounds)
        return {"concurrency": concurrency, "requests": len(done),
                "wall_s": round(wall, 3)}


class TestConcurrencyParityAndContract(_LoadCase):
    """G1/G2/G12 hold under simultaneous load, not only single-threaded."""

    def test_data_parity_and_route_hold_at_every_concurrency(self):
        for c in CONCURRENCY_LEVELS:
            with self.subTest(concurrency=c):
                out = self._run_load(c)
                self.assertEqual(out["requests"], c * 6)

    def test_the_error_contract_is_unchanged_under_load(self):
        """G12: a 413 stays a 413 when the process is busy, and does not become a 500."""
        codes, lock = [], threading.Lock()

        def worker():
            r = self.client.get("/api/ghrsst", params=dict(
                lon0=LON, lat0=LAT, start="2020-01-01", end=self.fx.delta_days[-1]))
            with lock:
                codes.append(r.status_code)

        ts = [threading.Thread(target=worker) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)
        self.assertEqual(set(codes), {413})


class TestResourceBoundsUnderSustainedLoad(_LoadCase):
    """G5 memory and G6 handles/threads: the claim is NO UNBOUNDED GROWTH, not a budget."""

    def test_rss_fds_and_threads_do_not_grow_across_a_sustained_run(self):
        self._run_load(8, rounds=4)                       # warm caches, LRU, executor threads
        gc.collect()
        base_rss, base_fds, base_thr = _rss_mb(), _open_fds(), threading.active_count()

        for _ in range(6):                                # sustained
            self._run_load(8, rounds=4)
        gc.collect()
        end_rss, end_fds, end_thr = _rss_mb(), _open_fds(), threading.active_count()

        self.assertLessEqual(end_fds - base_fds, 8,
                             f"fd growth {base_fds} -> {end_fds}: per-request opens leak")
        self.assertLessEqual(end_thr - base_thr, 4,   # workers are reaped asynchronously; G6 targets monotonic growth
                             f"thread growth {base_thr} -> {end_thr}")
        if base_rss and end_rss:
            # Generous: this process holds the client too, and Python does not return arenas
            # promptly. A leak shows as steady growth, not as a tight bound.
            self.assertLess(end_rss - base_rss, 150.0,
                            f"RSS grew {base_rss:.1f} -> {end_rss:.1f} MB across the run")

    def test_segment_handles_are_not_reopened_per_request(self):
        """R3/G6: a segmented base holds N stores; per-request opens would scale fd churn with
        segment count. Compares fds around many requests, not around one."""
        self._run_load(4, rounds=2)
        before = _open_fds()
        self._run_load(4, rounds=8)
        self.assertLessEqual(_open_fds() - before, 4)


class TestGenerationChangeDuringLoad(_LoadCase):
    """G10 / H4 / R1: an in-flight read sees a COMPLETE old or COMPLETE new view.

    The successor republishes the LAST segment's days with different values, so a mixed read is
    detectable: a response holding some v1 and some v2 values for that segment's days is exactly
    the failure this gate exists to catch. A test that published identical values could not
    tell mixing from correctness."""

    def _publish_v2(self):
        f = self.fx
        days = f.base_days[-BLOCK_DAYS:]
        # Inside the manifest root: the store refuses a segment path that resolves outside
        # it, which is the containment guard, not an inconvenience.
        p = fx.build_block(os.path.join(f.root, "block_2_v2.zarr"), days,
                           ny=NY, nx=NX, seed=9999)         # different values, same days
        segs = [dict(s) for s in f.segments[:-1]]
        segs.append(_segment("s2", os.path.relpath(p, f.root), days, N_BLOCKS - 1, p))
        f.publish(segs)
        return p, days

    def _values(self, path, days):
        g = zarr.open_group(path, mode="r")
        idx = {d: i for i, d in enumerate(g.attrs["days"])}
        j = int(np.argmin(np.abs(np.asarray(g["lon"][:]) - LON)))
        i = int(np.argmin(np.abs(np.asarray(g["lat"][:]) - LAT)))
        return {d: float(np.asarray(g["sst"][idx[d], i, j])) for d in days}

    def test_a_publication_during_load_is_never_observed_half_applied(self):
        f = self.fx
        old_path = f.block_paths["block_2.zarr"]
        days = f.base_days[-BLOCK_DAYS:]
        old = self._values(old_path, days)
        new_path, _ = self._publish_v2()
        new = self._values(new_path, days)
        self.assertNotEqual(old, new, "v1 and v2 are identical; mixing would be invisible")

        cube = self.client.app.state.router.cube
        verdicts, errors, stop = [], [], threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    r = self.client.get("/api/ghrsst", params=dict(
                        lon0=LON, lat0=LAT, start=days[0], end=days[-1]))
                    if r.status_code != 200:
                        errors.append(f"status {r.status_code}")
                        return
                    got = {x["date"]: x["sst"] for x in r.json()}
                    if len(got) != len(days):
                        errors.append(f"short read: {len(got)} of {len(days)}")
                        return
                    if any(v is None for v in got.values()):
                        errors.append("fabricated None")   # G10 names this explicitly
                        return
                    is_old = all(abs(got[d] - old[d]) < 1e-4 for d in days)
                    is_new = all(abs(got[d] - new[d]) < 1e-4 for d in days)
                    if not (is_old or is_new):
                        mixed = [d for d in days if abs(got[d] - old[d]) >= 1e-4
                                 and abs(got[d] - new[d]) >= 1e-4]
                        errors.append(f"MIXED generation; first divergent day {mixed[:1]}")
                        return
                    verdicts.append("old" if is_old else "new")
            except BaseException as e:                      # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")

        ts = [threading.Thread(target=reader) for _ in range(8)]
        for t in ts:
            t.start()
        for _ in range(12):                                 # the real composite refresh path
            time.sleep(0.02)
            cube.refresh()
        stop.set()
        for t in ts:
            t.join(timeout=60)

        self.assertFalse(errors, f"G10 violated: {errors[:3]}")
        self.assertTrue(verdicts)
        self.assertIn("new", verdicts, "the refresh never became visible; nothing was proven")

    def test_the_refresh_goes_through_the_composite_path_not_a_reassignment(self):
        """Rebinding `app.state.router.cube` would make any generation change atomic for free
        and would prove nothing about the real refresh."""
        cube = self.client.app.state.router.cube
        before = id(cube)
        self._publish_v2()
        cube.refresh()
        self.assertEqual(id(self.client.app.state.router.cube), before,
                         "the cube object was replaced; this is not the refresh path")
        self.assertEqual(self.client.get("/healthz").json()["cube_generation"],
                         self.fx.generation)


class TestPrePublicationFailure(_LoadCase):
    """G8 — a publication that fails before the commit point leaves the old manifest usable."""

    def _served(self, days):
        r = self.client.get("/api/ghrsst", params=dict(
            lon0=LON, lat0=LAT, start=days[0], end=days[-1]))
        self.assertEqual(r.status_code, 200)
        return {x["date"]: x["sst"] for x in r.json()}

    def test_a_rejected_publication_leaves_the_served_view_identical(self):
        f = self.fx
        days = f.base_days[-BLOCK_DAYS:]
        before = self._served(days)
        live_before = bm.load_live(f.root)

        bad = f.manifest(f.segments, f.generation + 1)
        bad["segments"][0]["day_count"] = 999_999          # rejected by validate_manifest
        with self.assertRaises(bm.ManifestError):
            bm.publish(f.root, bad)

        self.assertEqual(bm.load_live(f.root), live_before, "the live pointer moved")
        self.client.app.state.router.cube.refresh()
        self.assertEqual(self._served(days), before, "G8: the served view changed")
        self.assertEqual(self.client.get("/healthz").json()["cube_generation"], f.generation)

    def test_a_failure_after_the_archive_but_before_the_pointer_serves_the_OLD_view(self):
        """`publish` writes the immutable archive first and `os.replace`s the pointer second.
        A crash in between leaves an archive ahead of the live generation — which must not be
        served, because nothing committed it."""
        f = self.fx
        days = f.base_days[-BLOCK_DAYS:]
        before = self._served(days)
        gen = f.generation + 1
        text = bm.canonical_json(f.manifest(f.segments, gen))
        with open(os.path.join(f.root, bm.archive_name(gen)), "w") as fh:
            fh.write(text)                                  # archive only; pointer untouched

        self.client.app.state.router.cube.refresh()
        self.assertEqual(self._served(days), before)
        self.assertEqual(self.client.get("/healthz").json()["cube_generation"], f.generation)
        os.remove(os.path.join(f.root, bm.archive_name(gen)))   # fixture cleanup only


class TestPostPublicationRollback(_LoadCase):
    """G9 — rollback restores the predecessor's served view exactly; G15 — archives survive."""

    def _served(self, days):
        r = self.client.get("/api/ghrsst", params=dict(
            lon0=LON, lat0=LAT, start=days[0], end=days[-1]))
        self.assertEqual(r.status_code, 200)
        return {x["date"]: x["sst"] for x in r.json()}

    def test_rollback_restores_the_predecessor_view_and_keeps_every_archive(self):
        f = self.fx
        days = f.base_days[-BLOCK_DAYS:]
        gen1 = f.generation
        before = self._served(days)
        archives_before = {n: _read_bytes(os.path.join(f.root, n))
                           for n in os.listdir(f.root) if n.startswith("manifest.gen")}

        p = fx.build_block(os.path.join(f.root, "rb_v2.zarr"), days, ny=NY, nx=NX, seed=4242)
        segs = [dict(s) for s in f.segments[:-1]]
        segs.append(_segment("s2", os.path.relpath(p, f.root), days, N_BLOCKS - 1, p))
        f.publish(segs)
        self.client.app.state.router.cube.refresh()
        after = self._served(days)
        self.assertNotEqual(after, before, "the publication changed nothing to roll back")

        bm.rollback_to(f.root, gen1)
        self.client.app.state.router.cube.refresh()
        self.assertEqual(self._served(days), before, "G9: predecessor view not restored")
        self.assertEqual(self.client.get("/healthz").json()["cube_generation"], gen1)

        for name, blob in archives_before.items():
            with self.subTest(archive=name):
                path = os.path.join(f.root, name)
                self.assertTrue(os.path.isfile(path), "G15: an archive was consumed")
                with open(path, "rb") as fh:
                    self.assertEqual(fh.read(), blob, "G15: an archive changed")
        f.generation = gen1


class TestDiskGate(unittest.TestCase):
    """G7 — the SHIPPED `build_block.disk_precheck`, with a real reserve.

    `hard_reserve_bytes=0` is never used: it makes `ok` true for every input, so the gate would
    report PASS without being able to fail."""

    def setUp(self):
        self.tmp = os.path.dirname(os.path.abspath(__file__))

    def test_a_modest_block_passes_against_a_real_reserve(self):
        out = disk_precheck(os.path.join(self.tmp, "x.zarr"),
                            block_bytes_estimate=1024 * 1024,
                            hard_reserve_bytes=GATE_RESERVE_BYTES)
        self.assertGreater(out["hard_reserve_bytes"], 0, "a zeroed reserve gates nothing")
        self.assertTrue(out["ok"], f"local disk too small to run this gate: {out}")
        self.assertEqual(out["additional_bytes"], 1024 * 1024)

    def test_an_oversized_block_FAILS_the_gate(self):
        free = disk_precheck(os.path.join(self.tmp, "x.zarr"), block_bytes_estimate=0,
                             hard_reserve_bytes=GATE_RESERVE_BYTES)["free_now_bytes"]
        out = disk_precheck(os.path.join(self.tmp, "x.zarr"),
                            block_bytes_estimate=free,          # consumes the whole device
                            hard_reserve_bytes=GATE_RESERVE_BYTES)
        self.assertFalse(out["ok"], "the gate passed a block that consumes all free space")
        self.assertLess(out["projected_free_after_bytes"], out["hard_reserve_bytes"])

    def test_temp_and_growth_are_charged_but_pinned_existing_is_NOT(self):
        """§7.1c: `free_now` already excludes bytes on disk, so charging pinned blocks again
        would make each fold stricter than the last and refuse the sealing fold."""
        common = dict(block_bytes_estimate=10, hard_reserve_bytes=GATE_RESERVE_BYTES)
        base = disk_precheck(os.path.join(self.tmp, "x.zarr"), **common)
        charged = disk_precheck(os.path.join(self.tmp, "x.zarr"), temp_bytes=1000,
                                growth_bytes=500, **common)
        pinned = disk_precheck(os.path.join(self.tmp, "x.zarr"),
                               pinned_existing_bytes=10 ** 12, **common)
        self.assertEqual(charged["additional_bytes"], base["additional_bytes"] + 1500)
        self.assertEqual(pinned["additional_bytes"], base["additional_bytes"])
        self.assertTrue(pinned["ok"])


if __name__ == "__main__":
    unittest.main()
