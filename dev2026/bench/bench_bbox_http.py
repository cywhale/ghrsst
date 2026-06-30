"""dev2026 — P3-S1 real-store / SHADOW HTTP gate (Codex S1 #1).

Runs the ACTUAL API over a real HTTP round-trip on a shadow port and reports the full chain per
format (json / grid / columnar) at a production-like bbox size:
  server : X-Read-Ms / X-Encode-Ms (from response headers), X-Served-Rows
  transfer: curl `--compressed` size_download + Content-Encoding + time_total
  client : Node harness (--expose-gc) fetch / text / parse / transform + heap

Ops boundary (Codex S1 #5): defaults to a SYNTHETIC store in a tempdir; never writes any store. Pass
`--store <path>` to read-only-serve a real local daily store. **Do NOT point this at VM24 / production
mur.zarr / cube / delta while backfill is running.** Compression is owned by NGINX (brotli/gzip); a
bare local uvicorn does NOT compress, so Content-Encoding is normally empty here — the real br/CE
check is a VM24 step, DEFERRED while backfill runs. This gate validates the wire/encode/client path.

Run: dev2026/.venv/bin/python dev2026/bench/bench_bbox_http.py [--store PATH] [--points 750000] [--date YYYYMMDD]
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import numpy as np
import zarr

HERE = os.path.dirname(__file__)
DEV = os.path.join(HERE, "..")
sys.path.insert(0, DEV)
from store.zarr_paths import group_path, list_existing_days  # noqa: E402

VARS = ("sst", "sst_anomaly", "sea_ice")
RESULTS = os.path.join(HERE, "results")
NODE_HARNESS = os.path.join(HERE, "client", "bbox_client_bench.mjs")


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def _build_synth(root, day, G):
    g = zarr.open_group(group_path(root, day), mode="w", zarr_format=3)
    g.create_array("lon", shape=(G,), dtype="float32", chunks=(G,)); g["lon"][:] = np.linspace(100, 180, G).astype(np.float32)
    g.create_array("lat", shape=(G,), dtype="float32", chunks=(G,)); g["lat"][:] = np.linspace(-10, 30, G).astype(np.float32)
    rng = np.random.default_rng(0)
    for k, v in enumerate(("sst", "sea_ice")):                  # synth lacks sst_anomaly (absent-var path)
        field = (5 + k + 0.01 * np.arange(G)[:, None] + 0.001 * np.arange(G)[None, :]).astype(np.float32)
        field[rng.random((G, G)) < 0.05] = np.nan
        g.create_array(v, shape=(1, G, G), dtype="float32", chunks=(1, 1024, 1024)); g[v][0] = field


def _wait_health(port, timeout=30):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def _bbox_for_points(store_path, day, target):
    """Return (lon0,lat0,lon1,lat1) over an n×n region centred in the store grid yielding ~target pts."""
    g = zarr.open_group(group_path(store_path, day), mode="r")
    lon = np.asarray(g["lon"][:]); lat = np.asarray(g["lat"][:])
    side = int(target ** 0.5)
    ni = min(side, lat.size); nj = min(max(target // ni, 1), lon.size)
    i0 = (lat.size - ni) // 2; j0 = (lon.size - nj) // 2
    return float(lon[j0]), float(lat[i0]), float(lon[j0 + nj - 1]), float(lat[i0 + ni - 1]), ni * nj


def _curl(url):
    # -D - dumps headers; -o /dev/null discards body; -w gives transfer metrics. --compressed advertises br,gzip.
    out = subprocess.run(["curl", "-s", "--compressed", "-o", "/dev/null", "-D", "-",
                          "-w", "\\nSIZE %{size_download}\\nTIME %{time_total}\\n", url],
                         capture_output=True, text=True, timeout=120)
    hdr = {}
    size = tt = None
    for line in out.stdout.splitlines():
        if ":" in line and not line.startswith(("SIZE", "TIME")):
            k, _, v = line.partition(":"); hdr[k.strip().lower()] = v.strip()
        elif line.startswith("SIZE"):
            size = int(line.split()[1])
        elif line.startswith("TIME"):
            tt = float(line.split()[1])
    return {"transfer_bytes": size, "time_total_s": tt,
            "content_encoding": hdr.get("content-encoding") or "(none)",
            "x_read_ms": hdr.get("x-read-ms"), "x_encode_ms": hdr.get("x-encode-ms"),
            "x_served_rows": hdr.get("x-served-rows"), "x_bbox_format": hdr.get("x-bbox-format")}


def _node(url):
    out = subprocess.run(["node", "--expose-gc", NODE_HARNESS, "--url", url, "--runs", "3"],
                         capture_output=True, text=True, timeout=180)
    try:
        return json.loads(out.stdout)
    except Exception:
        return {"error": out.stderr[-400:] or out.stdout[-400:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=None, help="real local daily store (read-only); default synthetic")
    ap.add_argument("--points", type=int, default=750000)
    ap.add_argument("--date", default=os.environ.get("P3S1_DATE", "unknown"))
    args = ap.parse_args()

    tmp = None
    if args.store:
        store_path = args.store
        days = list_existing_days(store_path)
        if not days:
            print(f"no days in {store_path}"); return
        day = days[-1]; store_kind = f"real:{store_path}"
    else:
        tmp = tempfile.mkdtemp(prefix="p3s1http_")
        store_path = os.path.join(tmp, "daily"); day = "2026-01-01"
        _build_synth(store_path, day, 2048); store_kind = "synthetic(2048²,~5%NaN,no sst_anomaly)"

    lon0, lat0, lon1, lat1, npts = _bbox_for_points(store_path, day, args.points)
    port = _free_port()
    env = dict(os.environ, GHRSST_ZARR_PATH=store_path, GHRSST_BBOX_POINT_LIMIT="3000000",
               GHRSST_TIMECUBE_PATH="", GHRSST_DELTACUBE_PATH="")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "api.app:app", "--host", "127.0.0.1",
                             "--port", str(port), "--log-level", "warning"], cwd=DEV, env=env)
    report = {"date_tag": args.date, "store": store_kind, "day": day, "points": npts,
              "shadow_port": port, "results": {},
              "ops_note": "shadow uvicorn (no NGINX) -> Content-Encoding normally empty; real brotli/CE "
                          "is a VM24 check, DEFERRED while backfill runs. Store never written."}
    try:
        if not _wait_health(port):
            print("uvicorn did not become healthy"); return
        base = (f"http://127.0.0.1:{port}/api/ghrsst?lon0={lon0}&lat0={lat0}&lon1={lon1}&lat1={lat1}"
                f"&start={day}&end={day}&append=sst,sst_anomaly,sea_ice")
        print(f"# P3-S1 shadow HTTP gate — store={store_kind}, day={day}, points={npts:,}\n")
        print(f"| fmt | server read/encode ms | transfer MB | Content-Encoding | "
              f"client fetch/text/parse/transform ms | heap MB (gc) |")
        print("|" + "---|" * 6)
        for fmt in ("json", "grid", "columnar"):
            url = f"{base}&format={fmt}"
            c = _curl(url); n = _node(url)
            report["results"][fmt] = {"curl": c, "client": n}
            srv = f"{c['x_read_ms']}/{c['x_encode_ms'] or '-'}"
            cl = (f"{n.get('t_fetch_ms')}/{n.get('t_text_ms')}/{n.get('t_parse_ms')}/{n.get('t_transform_ms')}"
                  if "error" not in n else f"ERR {n['error'][:40]}")
            print(f"| {fmt} | {srv} | {(c['transfer_bytes'] or 0)/1e6:.1f} | {c['content_encoding']} | "
                  f"{cl} | {n.get('heap_mb')} ({n.get('gc_used')}) |")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
        if tmp:
            import shutil; shutil.rmtree(tmp, ignore_errors=True)

    os.makedirs(RESULTS, exist_ok=True)
    art = os.path.join(RESULTS, f"p3s1_bbox_http_{args.date}.json")
    with open(art, "w") as fh:
        json.dump(report, fh, indent=2)
    print(f"\nartifact -> {os.path.relpath(art, DEV)}")


if __name__ == "__main__":
    main()
