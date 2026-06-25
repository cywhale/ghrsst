"""dev2026 — delta-layout benchmark (Codex PR #13 follow-up): the delta must NOT inherit base s8.

Compares append-time / file-count / RSS / recent-day point-read latency across delta chunkings:
  s8/shard128 (= base read layout, WRONG for append), s128/shard128, s256/shard256 (append-opt).

Run: dev2026/.venv/bin/python dev2026/bench/bench_delta_layout.py [GRID] [NDAYS]
"""
from __future__ import annotations

import gc
import os
import sys
import tempfile
import threading
import time

import numpy as np
import psutil
import zarr

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
from store.zarr_paths import group_path  # noqa: E402
from store.time_cube import TimeCubeStore  # noqa: E402
from ingest.dual_write import append_to_delta  # noqa: E402

_PROC = psutil.Process()


class Peak:
    def __init__(self):
        self.base = _PROC.memory_info().rss

    def __enter__(self):
        self.run = True; self.peak = _PROC.memory_info().rss
        self.th = threading.Thread(target=self._p, daemon=True); self.th.start(); return self

    def _p(self):
        while self.run:
            self.peak = max(self.peak, _PROC.memory_info().rss); time.sleep(0.003)

    def __exit__(self, *a):
        self.run = False; self.th.join()

    @property
    def mb(self):
        return round((self.peak - self.base) / 1e6, 1)


def nfiles(p):
    return sum(len(f) for _, _, f in os.walk(p))


def main():
    import datetime
    G = int(sys.argv[1]) if len(sys.argv) > 1 else 2048
    NDAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    tmp = tempfile.mkdtemp(prefix="delta_bench_")
    col = (10 + 0.001 * np.arange(G, dtype=np.float32))[:, None]
    field = np.ascontiguousarray(np.broadcast_to(col, (G, G)).astype(np.float32))
    daily = os.path.join(tmp, "daily")
    days = [(datetime.date(2024, 1, 1) + datetime.timedelta(days=i)).isoformat() for i in range(NDAYS)]
    for i, d in enumerate(days):
        g = zarr.open_group(group_path(daily, d), mode="w", zarr_format=3)
        g.create_array("lon", shape=(G,), dtype="float32", chunks=(G,)); g["lon"][:] = np.linspace(100, 130, G).astype(np.float32)
        g.create_array("lat", shape=(G,), dtype="float32", chunks=(G,)); g["lat"][:] = np.linspace(0, 30, G).astype(np.float32)
        for v in ("sst", "sst_anomaly", "sea_ice"):
            g.create_array(v, shape=(1, G, G), dtype="float32", chunks=(1, 1024, 1024)); g[v][0] = field + 0.01 * i

    print(f"grid={G}x{G}, NDAYS={NDAYS}, daily chunk=1024")
    print(f"{'delta layout':>16} | {'chunks/var':>11} | {'append_s/day':>12} | {'peak RSS MB':>11} | {'files(all)':>10} | {'point-read ms':>13}")
    for sc, sh in [(8, 128), (128, 128), (256, 256)]:
        delta = os.path.join(tmp, f"d_{sc}_{sh}")
        gc.collect(); p = Peak()
        with p:
            r0 = append_to_delta(daily, delta, days[0], spatial_chunk=sc, shard_spatial=sh, workers=4)
        appends = [r0["append_s"]]
        for d in days[1:]:
            appends.append(append_to_delta(daily, delta, d, spatial_chunk=sc, shard_spatial=sh, workers=4)["append_s"])
        tcs = TimeCubeStore(delta)
        t = time.perf_counter()
        for _ in range(20):
            tcs.point_series(119.3, 22.3, days, ["sst", "sst_anomaly", "sea_ice"])
        rd = (time.perf_counter() - t) / 20 * 1000
        chunks_per_var = (G // sc) ** 2
        print(f"{f's{sc}/shard{sh}':>16} | {chunks_per_var:>11,} | {np.mean(appends):>12.2f} | {p.mb:>11} | {nfiles(delta):>10,} | {rd:>13.1f}")
    import shutil
    shutil.rmtree(tmp)


if __name__ == "__main__":
    main()
