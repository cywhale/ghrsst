"""dev2026 — P1-S2 FastAPI app: drop-in GET + POST /points + streaming bbox.

Design (spec dev2026/specs/01_refactor_spec.md P1-S2/§4):
  * Full GET parity with the old /api/ghrsst (point / range / bbox / append / mode /
    sample) so the NGINX cutover (which switches the whole ghrsstapi upstream) does
    not break existing REST/MCP clients.
  * POST /api/ghrsst/points — batch, explicit schema (index, dup-preserving, null
    absent fields), GHRSST_POINTS_BATCH_MAX / GHRSST_BATCH_CHUNK_FANOUT_MAX.
  * bbox -> StreamingResponse that keeps the JSON-array wire format (not NDJSON).
  * All Zarr work goes through a BOUNDED executor with admission backpressure:
    overload -> fast 503 + Retry-After (canonical; 429 reserved for per-client RL).
  * Cache-Control: no-store for date-less GET (default latest) and POST; long cache
    only for explicit fixed-date requests that cannot touch the moving latest.

Run (dev): GHRSST_ZARR_PATH=... dev2026/.venv/bin/uvicorn api.app:app --port 8036
(from the dev2026/ dir, or set PYTHONPATH=dev2026)
"""
from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import List, Optional

import orjson
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from store.store_access import ALLOWED_FIELDS, StoreAccess  # noqa: E402
from store.hybrid_router import HybridRouter  # noqa: E402
from store.time_cube import TimeCubeStore  # noqa: E402
from store.tiered_cube import TieredCube  # noqa: E402

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


class Cfg:
    # Allow "one year" queries across leap years without forcing clients to split.
    MAX_DAYS = _env_int("GHRSST_MAX_DAYS", 366)
    POINTS_BATCH_MAX = _env_int("GHRSST_POINTS_BATCH_MAX", 1000)
    BATCH_CHUNK_FANOUT_MAX = _env_int("GHRSST_BATCH_CHUNK_FANOUT_MAX", 64)
    LRU_MAX = _env_int("GHRSST_LRU_MAX", 64)
    BBOX_POINT_LIMIT = _env_int("GHRSST_BBOX_POINT_LIMIT", 1_000_000)
    ZARR_WORKERS = _env_int("GHRSST_ZARR_WORKERS", min(8, os.cpu_count() or 4))
    OVERLOAD_WAIT_MS = _env_int("GHRSST_OVERLOAD_WAIT_MS", 2000)
    OVERLOAD_QUEUE_MAX = _env_int("GHRSST_OVERLOAD_QUEUE_MAX", 4 * ZARR_WORKERS)
    LONG_CACHE_SECONDS = _env_int("GHRSST_FIXED_CACHE_SECONDS", 864000)  # 10d (immutable)
    BBOX_STREAM_BATCH = _env_int("GHRSST_BBOX_STREAM_BATCH", 50_000)


cfg = Cfg()


class Overloaded(Exception):
    pass


class BoundedExecutor:
    """Bounded thread pool + admission gate. .run() admits up to
    (workers + queue_max) concurrent tasks, waiting at most wait_ms for a slot,
    else raises Overloaded (-> 503). .run_ungated() runs already-admitted chunked
    work (e.g. per-batch bbox encode) without re-gating to avoid mid-stream 503."""

    def __init__(self, workers: int, queue_max: int, wait_ms: int):
        from concurrent.futures import ThreadPoolExecutor
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="zarr")
        self.limit = workers + queue_max
        self.wait_s = wait_ms / 1000.0
        self.sem = asyncio.Semaphore(self.limit)

    def queue_depth(self) -> int:
        # admitted-but-not-yet-released permits (in-flight reqs incl. active bbox streams)
        return self.limit - self.sem._value  # noqa: SLF001 (metrics only)

    async def acquire(self):
        """Admit one unit of work (a request, or a whole bbox stream). Raises
        Overloaded if no permit frees within wait_ms. Caller MUST release()."""
        try:
            await asyncio.wait_for(self.sem.acquire(), timeout=self.wait_s)
        except asyncio.TimeoutError:
            raise Overloaded()

    def release(self):
        self.sem.release()

    async def run(self, fn, *args):
        """Admission-gated one-shot: acquire -> run in thread -> release."""
        await self.acquire()
        try:
            return await self.run_ungated(fn, *args)
        finally:
            self.release()

    async def run_ungated(self, fn, *args):
        """Run blocking work in the pool WITHOUT acquiring a permit. Only valid
        while the caller already holds a permit (e.g. inside a held bbox stream)."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self.executor, fn, *args)

    def shutdown(self):
        self.executor.shutdown(wait=False)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = StoreAccess(
        lru_max=cfg.LRU_MAX,
        batch_chunk_fanout_max=cfg.BATCH_CHUNK_FANOUT_MAX,
        points_batch_max=cfg.POINTS_BATCH_MAX,
        bbox_point_limit=cfg.BBOX_POINT_LIMIT,
    )
    # optional time-cube for multi-day point/range routing (P2-S5 hybrid routing).
    # Unset / missing -> router uses the daily store for everything (== P1 behaviour).
    cube = None
    tc_path = os.environ.get("GHRSST_TIMECUBE_PATH")
    if tc_path and os.path.isdir(tc_path):
        try:
            cube = TimeCubeStore(tc_path)
            # optional delta cube (time_chunk=1, cheap appends) -> tier base+delta
            dc_path = os.environ.get("GHRSST_DELTACUBE_PATH")
            if dc_path and os.path.isdir(dc_path):
                cube = TieredCube(cube, TimeCubeStore(dc_path))
        except Exception as e:  # noqa: BLE001
            print(f"[GHRSST] time-cube load skipped: {e}")
    app.state.router = HybridRouter(app.state.store, cube)
    app.state.bex = BoundedExecutor(cfg.ZARR_WORKERS, cfg.OVERLOAD_QUEUE_MAX, cfg.OVERLOAD_WAIT_MS)
    yield
    app.state.bex.shutdown()


app = FastAPI(lifespan=lifespan)


def _json(obj, status_code: int = 200, headers: Optional[dict] = None) -> Response:
    return Response(orjson.dumps(obj), status_code=status_code,
                    media_type="application/json", headers=headers)


@app.exception_handler(Overloaded)
async def _overloaded_handler(request: Request, exc: Overloaded):
    # canonical capacity-shed: 503 + Retry-After (429 reserved for per-client RL)
    return _json({"detail": "server overloaded; retry later"},
                 status_code=503, headers={"Retry-After": "1"})


# ---- helpers -------------------------------------------------------------
def _parse_date(s: Optional[str]) -> Optional[str]:
    if s is None:
        return None
    if isinstance(s, str) and DATE_RE.match(s):
        return s
    raise HTTPException(400, "Invalid date format. Use YYYY-MM-DD.")


def _fields(append: Optional[str]) -> List[str]:
    if not append:
        return ["sst"]
    want = [t.strip() for t in append.split(",") if t.strip()]
    bad = [w for w in want if w not in ALLOWED_FIELDS]
    if bad:
        raise HTTPException(400, f"Unsupported field(s): {','.join(bad)}. "
                                 f"Allowed: {','.join(ALLOWED_FIELDS)}")
    return want


def _parse_modes(mode: Optional[str]):
    if not mode:
        return set()
    items = {m.strip().lower() for m in mode.split(",") if m.strip()}
    bad = [m for m in items if m != "truncate"]
    if bad:
        raise HTTPException(400, f"Unsupported mode(s): {','.join(bad)}. Allowed: truncate")
    return items


def _apply_modes(rows, modes, fields):
    if "truncate" in modes:
        for r in rows:
            if r.get("lon") is not None:
                r["lon"] = round(float(r["lon"]), 5)
            if r.get("lat") is not None:
                r["lat"] = round(float(r["lat"]), 5)
            for f in fields:
                if r.get(f) is not None:
                    r[f] = round(float(r[f]), 3)
    return rows


def _daterange(s: str, e: str) -> List[str]:
    d0 = datetime.strptime(s, "%Y-%m-%d").date()
    d1 = datetime.strptime(e, "%Y-%m-%d").date()
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def _no_store():
    return {"Cache-Control": "no-store"}


def _fixed_cache():
    return {"Cache-Control": f"public, max-age={cfg.LONG_CACHE_SECONDS}"}


# ---- GET /api/ghrsst (full parity) ---------------------------------------
@app.get("/api/ghrsst")
async def read_ghrsst(
    request: Request,
    lon0: float = Query(...),
    lat0: float = Query(...),
    lon1: Optional[float] = Query(None),
    lat1: Optional[float] = Query(None),
    start: Optional[str] = Query(None),
    end: Optional[str] = Query(None),
    append: Optional[str] = Query(None),
    sample: int = Query(1),
    mode: Optional[str] = Query(None),
):
    store: StoreAccess = request.app.state.store
    bex: BoundedExecutor = request.app.state.bex
    fields = _fields(append)
    modes = _parse_modes(mode)
    earliest, latest = store.bounds()
    if not latest:
        raise HTTPException(503, "No available dates.")

    bbox_mode = (lon1 is not None) and (lat1 is not None) and not (lon1 == lon0 and lat1 == lat0)
    if not bbox_mode and sample != 1:
        raise HTTPException(400, "Parameter 'sample' is only supported in BBox mode.")

    # ---------------- POINT MODE ----------------
    if not bbox_mode:
        s = _parse_date(start)
        e = _parse_date(end)
        date_less = (s is None and e is None)
        if date_less:
            wanted = [latest]
            cacheable = False                     # default-latest: must not cache (stale risk)
        else:
            if s and not e:
                e = s
            if e and not s:
                s = e
            if s > e:
                s, e = e, s
            e_req = e                             # requested upper bound (pre-clamp)
            # MAX_DAYS cap on the REQUESTED span (BEFORE clamping to bounds) -> 413;
            # no clamping-to-fit / pagination, client must split (product decision).
            req_days = (datetime.strptime(e, "%Y-%m-%d").date()
                        - datetime.strptime(s, "%Y-%m-%d").date()).days + 1
            if req_days > cfg.MAX_DAYS:
                raise HTTPException(413, {
                    "error": "requested range too large",
                    "max_days": cfg.MAX_DAYS, "requested_days": req_days,
                    "hint": f"split into ranges of <= {cfg.MAX_DAYS} days",
                })
            if s < earliest:
                s = earliest
            if e > latest:
                e = latest
            wanted = _daterange(s, e)
            # cacheable only if the upper bound can never be affected by new ingest
            cacheable = e_req < latest

        existing = [d for d in wanted if store.day_present(d)]
        if not existing:
            raise HTTPException(400, f"Data not exist for requested period; "
                                     f"available range is {earliest}/{latest}.")
        router = request.app.state.router
        route = router.route_point(existing)             # 'cube' (multi-day) or 'daily'
        rows = await bex.run(router.point_series, lon0, lat0, existing, fields)
        rows = _apply_modes(rows, modes, fields)
        headers = _fixed_cache() if cacheable else _no_store()
        headers["X-Store-Route"] = route                 # observability (canary/tests)
        return _json(rows, headers=headers)

    # ---------------- BBOX MODE (single day, streaming JSON array) ----------------
    if sample < 1:
        raise HTTPException(400, "Parameter 'sample' must be >= 1.")
    if start and end:
        s = _parse_date(start)
        e = _parse_date(end)
        if s != e:
            raise HTTPException(
                400,
                "BBOX query only allows single-day data; use start=end (or a single start/end) "
                "and increase sample or shrink bbox for large areas.",
            )
        chosen = s
    elif start or end:
        chosen = _parse_date(start or end)
    else:
        chosen = None
    date_less = chosen is None
    if date_less:
        chosen = latest
    if chosen < earliest or chosen > latest or not store.day_present(chosen):
        raise HTTPException(400, f"BBOX query only allows single-day data. Requested "
                                 f"{chosen} is unavailable; available range is {earliest}/{latest}.")
    cacheable = (not date_less) and (chosen < latest)

    # Hold ONE admission permit for the WHOLE bbox stream lifecycle (read + every
    # encode batch + the resident lons/lats/cols working set), so concurrent bbox
    # streams are capacity-accounted (queue_depth) and RSS/threadpool are gated
    # (G6/G7). Released in the generator's finally on completion OR client disconnect.
    await bex.acquire()                       # Overloaded -> 503 (before any bytes)
    try:
        lons, lats, cols = await bex.run_ungated(
            store.bbox_arrays, chosen, lon0, lat0, lon1, lat1, fields, int(sample))
    except ValueError as ve:
        bex.release()
        raise HTTPException(400, str(ve))
    except BaseException:
        bex.release()
        raise
    total = int(lons.size) * int(lats.size)

    def _encode_window(k0: int, k1: int) -> bytes:
        rows = StoreAccess.bbox_rows_window(lons, lats, cols, fields, chosen, k0, k1)
        if "truncate" in modes:
            _apply_modes(rows, modes, fields)
        return orjson.dumps(rows)  # b"[{...},...]"

    async def gen():
        try:
            yield b"["
            first = True
            for k0 in range(0, total, cfg.BBOX_STREAM_BATCH):
                k1 = min(k0 + cfg.BBOX_STREAM_BATCH, total)
                payload = await bex.run_ungated(_encode_window, k0, k1)  # under held permit
                inner = payload[1:-1]  # strip [ ]
                if inner:
                    yield (inner if first else b"," + inner)
                    first = False
            yield b"]"
        finally:
            bex.release()                     # release on completion OR disconnect/aclose

    headers = _fixed_cache() if cacheable else _no_store()
    headers["X-Served-Rows"] = str(total)
    headers["X-Stride"] = str(int(sample))
    return StreamingResponse(gen(), media_type="application/json", headers=headers)


# ---- POST /api/ghrsst/points (batch) -------------------------------------
class PointsRequest(BaseModel):
    date: str
    points: List[List[float]]
    append: Optional[str] = None
    mode: Optional[str] = None


@app.post("/api/ghrsst/points")
async def read_points(request: Request, body: PointsRequest):
    store: StoreAccess = request.app.state.store
    bex: BoundedExecutor = request.app.state.bex
    fields = _fields(body.append)
    modes = _parse_modes(body.mode)
    day = _parse_date(body.date)
    if not body.points:
        raise HTTPException(400, "points must be a non-empty list of [lon,lat].")
    if len(body.points) > cfg.POINTS_BATCH_MAX:
        raise HTTPException(413, {
            "error": "too many points", "max_points": cfg.POINTS_BATCH_MAX,
            "requested_points": len(body.points),
            "hint": f"split into batches of <= {cfg.POINTS_BATCH_MAX} points",
        })
    for p in body.points:
        if len(p) != 2:
            raise HTTPException(400, "each point must be [lon, lat].")
    earliest, latest = store.bounds()
    if not latest or not store.day_present(day):
        raise HTTPException(400, f"day {day} not available; range {earliest}/{latest}.")
    try:
        rows = await bex.run(store.points_batch, body.points, day, fields)
    except ValueError as ve:
        # fan-out cap exceeded -> 413 (resource bound); others -> 400
        code = 413 if "fan-out" in str(ve) or "too many" in str(ve) else 400
        raise HTTPException(code, str(ve))
    rows = _apply_modes(rows, modes, fields)
    return _json(rows, headers=_no_store())  # batch: never cache (URI-only key)


def _rss_mb() -> Optional[float]:
    """Current process RSS in MB (this worker). Portable; None if unavailable."""
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1e6, 1)
    except Exception:
        try:
            with open("/proc/self/statm") as fh:           # Linux fallback
                pages = int(fh.read().split()[1])
            return round(pages * os.sysconf("SC_PAGE_SIZE") / 1e6, 1)
        except Exception:
            return None


@app.get("/healthz", include_in_schema=False)
async def healthz(request: Request):
    e, l = request.app.state.store.bounds()
    bex = request.app.state.bex
    router = request.app.state.router
    cube = router.cube
    return {"status": "ok", "earliest": e, "latest": l,
            "executor_queue_depth": bex.queue_depth(),
            "executor_limit": bex.limit,
            "rss_mb": _rss_mb(),
            "pid": os.getpid(),
            # P2-S6 cube observability
            "cube_loaded": cube is not None,
            "cube_kind": (None if cube is None else
                          ("tiered" if isinstance(cube, TieredCube) else "timecube")),
            "cube_latest": (cube.latest if cube else None),
            "cube_day_count": (cube.day_count if cube else 0),
            "cube_latest_in_sync": (cube.latest == l if cube else None),
            "delta_latest": (cube.delta.latest if isinstance(cube, TieredCube) and cube.delta else None),
            "delta_day_count": (cube.delta.day_count if isinstance(cube, TieredCube) and cube.delta else 0),
            "route_counts": dict(router.route_counts)}
