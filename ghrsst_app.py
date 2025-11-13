# ghrsst_app.py
import os, json, re, math
from datetime import date, datetime, timedelta
from typing import Optional, List, Set
import numpy as np
import xarray as xr
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import ORJSONResponse, JSONResponse
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from contextlib import asynccontextmanager
from pydantic import BaseModel

# =========================
# Config & runtime
# =========================

class Cfg:
    # default to project-relative path: data/mur.zarr
    ZARR_PATH = os.environ.get("GHRSST_ZARR_PATH", os.path.abspath(os.path.join(os.path.dirname(__file__), "data/mur.zarr")))
    INDEX_JSON = os.environ.get("GHRSST_INDEX_JSON", os.path.abspath(os.path.join(os.path.dirname(__file__), "data", "latest.json")))
    # hard ceiling for bbox points (nx * ny), no soft 200k rule anymore
    POINT_LIMIT = int(os.environ.get("GHRSST_POINT_LIMIT", "1000000"))
    # allowed fields
    ALLOWED_FIELDS = ("sst", "sst_anomaly", "sea_ice")
    # max days for point-range requests (inclusive)
    MAX_DAYS = 31

cfg = Cfg()

class RT:
    earliest: Optional[str] = None
    latest:   Optional[str] = None
    # a sample lon/lat for bounds (loaded lazily)
    lon: Optional[np.ndarray] = None
    lat: Optional[np.ndarray] = None
    LonRange = (-180.0, 180.0)
    LatRange = (-90.0, 90.0)
    index_mtime: Optional[float] = None

rt = RT()

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def _err(msg: str, code: int = 400):
    raise HTTPException(code, msg)

def _parse_date(s: Optional[str]) -> Optional[str]:
    if s is None: return None
    if isinstance(s, date): return s.isoformat()
    if isinstance(s, str) and DATE_RE.match(s): return s
    _err("Invalid date format. Use YYYY-MM-DD.")

def _scan_bounds(zarr_root: str):
    """Find earliest/latest by scanning directory levels /YYYY/MM/DD."""
    earliest = None; latest = None
    try:
        for y in sorted(os.listdir(zarr_root)):
            yp = os.path.join(zarr_root, y)
            if not (y.isdigit() and len(y)==4 and os.path.isdir(yp)): continue
            for m in sorted(os.listdir(yp)):
                mp = os.path.join(yp, m)
                if not (m.isdigit() and len(m)==2 and os.path.isdir(mp)): continue
                for d in sorted(os.listdir(mp)):
                    dp = os.path.join(mp, d)
                    if d.isdigit() and len(d)==2 and os.path.isdir(dp):
                        s = f"{y}-{m}-{d}"
                        if earliest is None: earliest = s
                        latest = s
    except FileNotFoundError:
        pass
    return earliest, latest

def _load_bounds():
    # prefer minimal latest.json with {"earliest":"...", "latest":"..."}
    e = l = None
    mtime = None
    if os.path.isfile(cfg.INDEX_JSON):
        try:
            mtime = os.path.getmtime(cfg.INDEX_JSON)
        except OSError:
            mtime = None
        try:
            j = json.load(open(cfg.INDEX_JSON, "r", encoding="utf-8"))
            e = j.get("earliest"); l = j.get("latest")
        except Exception:
            e = l = None
    if not e or not l:
        e2, l2 = _scan_bounds(cfg.ZARR_PATH)
        e = e or e2; l = l or l2
        if mtime is None:
            try:
                mtime = os.path.getmtime(cfg.ZARR_PATH)
            except OSError:
                mtime = None
    rt.earliest, rt.latest = e, l
    rt.index_mtime = mtime


def _refresh_bounds_if_index_changed():
    if os.path.isfile(cfg.INDEX_JSON):
        try:
            mtime = os.path.getmtime(cfg.INDEX_JSON)
        except OSError:
            mtime = None
        if (
            mtime is not None
            and (rt.index_mtime is None or mtime > rt.index_mtime + 1e-9)
        ):
            _load_bounds()


def _latest_or_503():
    _refresh_bounds_if_index_changed()
    if not rt.latest:
        _load_bounds()
    if not rt.latest:
        _err("No available dates.", 503)
    return rt.latest

def _group_path(day: str) -> str:
    y, m, d = day.split("-")
    return os.path.join(cfg.ZARR_PATH, y, m, d)

def _group_exists(day: str) -> bool:
    return os.path.isdir(_group_path(day))

def _open_group(day: str):
    grp = f"/{day.replace('-','/')}"
    return xr.open_zarr(cfg.ZARR_PATH, group=grp, zarr_format=3, consolidated=None)

def _daterange_inclusive(s: str, e: str) -> List[str]:
    d0 = datetime.strptime(s, "%Y-%m-%d").date()
    d1 = datetime.strptime(e, "%Y-%m-%d").date()
    n  = (d1 - d0).days + 1
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]

def _clamp(v, vmin, vmax): return max(vmin, min(vmax, v))

def _idx_from_coord(val: float, arr: np.ndarray) -> int:
    j = int(np.searchsorted(arr, val))
    if j == 0: return 0
    if j >= arr.size: return arr.size - 1
    return j if abs(arr[j]-val) < abs(arr[j-1]-val) else j-1

def _fields_from_append(append: Optional[str]) -> List[str]:
    if not append:
        return ["sst"]  # default
    want = [t.strip() for t in append.split(",") if t.strip()]
    bad = [w for w in want if w not in cfg.ALLOWED_FIELDS]
    if bad:
        _err(f"Unsupported field(s): {','.join(bad)}. Allowed: {','.join(cfg.ALLOWED_FIELDS)}")
    return want

ALLOWED_MODES = {"truncate"}

def _parse_modes(mode: Optional[str]) -> Set[str]:
    if not mode:
        return set()
    items = {m.strip().lower() for m in mode.split(",") if m.strip()}
    bad = [m for m in items if m not in ALLOWED_MODES]
    if bad:
        _err(f"Unsupported mode(s): {','.join(bad)}. Allowed: {','.join(sorted(ALLOWED_MODES))}")
    return items

def _apply_modes(rows: List[dict], modes: Set[str], data_fields: List[str]) -> List[dict]:
    if "truncate" in modes:
        for row in rows:
            if "lon" in row and row["lon"] is not None:
                row["lon"] = round(float(row["lon"]), 5)
            if "lat" in row and row["lat"] is not None:
                row["lat"] = round(float(row["lat"]), 5)
            for field in data_fields:
                if field in row and row[field] is not None:
                    row[field] = round(float(row[field]), 3)
    return rows

def _load_coords_from_any(days: List[str]):
    """Open the first existing day's group to fetch lon/lat & bounds."""
    for d in days:
        if _group_exists(d):
            with _open_group(d) as ds:
                lon_name = "lon" if "lon" in ds else ("longitude" if "longitude" in ds else None)
                lat_name = "lat" if "lat" in ds else ("latitude" if "latitude" in ds else None)
                if not lon_name or not lat_name:
                    continue
                lon = np.asarray(ds[lon_name].values)
                lat = np.asarray(ds[lat_name].values)
                return lon, lat, lon_name, lat_name
    _err("No existing daily groups in the requested span.", 400)

# =========================
# FastAPI app & docs
# =========================

@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_bounds()
    # best-effort load of lon/lat from latest (optional)
    try:
        if rt.latest and _group_exists(rt.latest):
            with _open_group(rt.latest) as ds:
                lon_name = "lon" if "lon" in ds else ("longitude" if "longitude" in ds else None)
                lat_name = "lat" if "lat" in ds else ("latitude" if "latitude" in ds else None)
                if lon_name and lat_name:
                    rt.lon = np.asarray(ds[lon_name].values)
                    rt.lat = np.asarray(ds[lat_name].values)
                    rt.LonRange = (float(rt.lon.min()), float(rt.lon.max()))
                    rt.LatRange = (float(rt.lat.min()), float(rt.lat.max()))
    except Exception as e:
        print(f"[GHRSST] startup coord load skipped: {e}")
    yield

app = FastAPI(docs_url=None, lifespan=lifespan, default_response_class=ORJSONResponse)

def generate_custom_openapi():
    if app.openapi_schema: return app.openapi_schema
    openapi_schema = get_openapi(
        title="ODB Open API of GHRSST (MUR v4.1)",
        version="1.0.0",
        description=(
            "Open API to query daily 1-km GHRSST MUR v4.1 (SST, SST anomaly, sea ice) data.\n\n"
            "* Data source: MUR-JPL-L4-GLOB-v4.1. JPL MUR MEaSUREs Project. 2015. GHRSST Level 4 MUR Global Foundation Sea Surface Temperature Analysis. Ver. 4.1. PO.DAAC, CA, USA. https://doi.org/10.5067/GHGMR-4FJ04\n"
            "* Point mode (lon0,lat0 only): default latest day or requested range ≤31 days; clamped to available [earliest, latest] with missing days skipped.\n"
            f"* BBox mode (lon0,lat0,lon1,lat1): single-day only (uses provided start/end to choose the day); requested day must exist; limit nx*ny ≤ {cfg.POINT_LIMIT}.\n"
        ),
        routes=app.routes,
    )
    openapi_schema["servers"] = [{"url": "https://eco.odb.ntu.edu.tw"}]
    app.openapi_schema = openapi_schema
    return app.openapi_schema

@app.get("/api/swagger/ghrsst/openapi.json", include_in_schema=False)
async def custom_openapi(): return JSONResponse(generate_custom_openapi())

@app.get("/api/swagger/ghrsst", include_in_schema=False)
async def custom_swagger_ui_html():
    return get_swagger_ui_html(openapi_url="/api/swagger/ghrsst/openapi.json", title="GHRSST API Docs")

class GHRSSTRow(BaseModel):
    lon: float
    lat: float
    date: date
    sst: Optional[float] = None
    sst_anomaly: Optional[float] = None
    sea_ice: Optional[float] = None

# =========================
# Endpoint
# =========================

@app.get("/api/ghrsst", response_model=List[GHRSSTRow], tags=["GHRSST"], summary="Query GHRSST (MUR v4.1, daily 1-km) data")
async def read_ghrsst(
    lon0: float = Query(..., description="Longitude (or min lon) [-180,180]"),
    lat0: float = Query(..., description="Latitude (or min lat) [-90,90]"),
    lon1: Optional[float] = Query(None, description="Max longitude (BBox mode)"),
    lat1: Optional[float] = Query(None, description="Max latitude (BBox mode)"),
    start: Optional[str] = Query(None, description="Start date YYYY-MM-DD (inclusive)"),
    end: Optional[str] = Query(None, description="End date YYYY-MM-DD (inclusive)"),
    append: Optional[str] = Query(None, description="Fields: sst,sst_anomaly,sea_ice (default: sst)"),
    sample: int = Query(1, description="(BBox) re-sample every N points (default 1) to thin the grid"),
    mode: Optional[str] = Query(
        None,
        description=(
            "Allowed modes: truncate. Multiple modes can be comma-separated. "
            "The mode 'truncate' rounds lon/lat to 5 decimals and data values to 3 decimals."
        ),
    ),
):
    """
    Query global marine SST (sst), SST anomalies (sst_anomaly) and sea ice fraction (sea_ice) from NASA MUR v4.1 by using spatial and temporal filters.

    #### Usage
    * Point query (lon0,lat0 only): single day (default latest) or ≤ 31-day range; clamped to available [earliest, latest]; missing days are skipped.
    * /api/ghrsst?lon0=-60&lat0=10&append=sst,sst_anomaly (point query, default date).
    * BBox query (lon0,lat0,lon1,lat1): single day only (uses `start` when both start/end provided, otherwise whichever is supplied, otherwise latest); requested day must exist in the dataset; per-day limit nx*ny ≤ 1,000,000 and supports `sample` stride to thin grids.
    * /api/ghrsst?lon0=125&lat0=15&lon1=126&lat1=16&start=2025-10-30 (bbox query, specific date).
    * Use mode=truncate to round lon/lat (5 dp) and data fields (3 dp) for lighter payloads.
    """

    _refresh_bounds_if_index_changed()

    fields = _fields_from_append(append)
    modes = _parse_modes(mode)

    # Determine mode
    bbox_mode = (lon1 is not None) and (lat1 is not None) and not (lon1 == lon0 and lat1 == lat0)

    if not bbox_mode and sample != 1:
        _err("Parameter 'sample' is only supported in BBox mode.")

    # Load / refresh earliest/latest if missing
    if not (rt.earliest and rt.latest):
        _load_bounds()

    # Guard: Have any data at all?
    if not (rt.earliest and rt.latest):
        _err("No available dates.", 503)

    # ----- POINT MODE: allow range up to 31 days (clamp & skip missing) -----
    if not bbox_mode:
        # Resolve requested range
        s = _parse_date(start); e = _parse_date(end)
        if not s and not e:
            # default latest
            day = _latest_or_503()
            wanted = [day]
        else:
            if s and not e: e = s
            if e and not s: s = e
            if s > e: s, e = e, s

            # clamp to available bounds
            if s < rt.earliest: s = rt.earliest
            if e > rt.latest:   e = rt.latest

            # cap to MAX_DAYS
            max_end = (datetime.strptime(s, "%Y-%m-%d").date() + timedelta(days=cfg.MAX_DAYS-1)).isoformat()
            if e > max_end: e = max_end

            wanted = _daterange_inclusive(s, e)

        # keep only existing days (skip missing)
        existing = [d for d in wanted if _group_exists(d)]
        if not existing:
            _err(f"Data not exist for requested period; available date range is {rt.earliest}/{rt.latest}.", 400)

        # fetch lon/lat
        lon, lat, lon_name, lat_name = _load_coords_from_any(existing)

        # nearest point index
        lo = float(_clamp(lon0, float(lon.min()), float(lon.max())))
        la = float(_clamp(lat0, float(lat.min()), float(lat.max())))
        jj = _idx_from_coord(lo, lon); ii = _idx_from_coord(la, lat)

        rows = []
        for day in existing:
            with _open_group(day) as ds:
                row = {"lon": float(lon[jj]), "lat": float(lat[ii]), "date": day}
                for f in fields:
                    if f in ds:
                        v = ds[f].isel({lat_name: ii, lon_name: jj}).compute().values.item()
                        row[f] = None if (v is None or np.isnan(v)) else float(v)
                rows.append(row)

        rows = _apply_modes(rows, modes, fields)
        return ORJSONResponse(rows)

    # ----- BBOX MODE: single day only -----
    if start and end:
        chosen = _parse_date(start)
    elif start or end:
        chosen = _parse_date(start or end)
    else:
        chosen = _latest_or_503()

    if chosen < rt.earliest or chosen > rt.latest or (not _group_exists(chosen)):
        _err(
            f"BBOX query only allows single-day data. Requested {chosen} is unavailable; available range is {rt.earliest}/{rt.latest}.",
            400,
        )

    # open coords from the chosen day
    lon, lat, lon_name, lat_name = _load_coords_from_any([chosen])

    # normalize bbox and clamp
    lo0, lo1 = (lon0, lon1) if lon1 >= lon0 else (lon1, lon0)
    la0, la1 = (lat0, lat1) if lat1 >= lat0 else (lat1, lat0)
    lo0 = _clamp(float(lo0), float(lon.min()), float(lon.max()))
    lo1 = _clamp(float(lo1), float(lon.min()), float(lon.max()))
    la0 = _clamp(float(la0), float(lat.min()), float(lat.max()))
    la1 = _clamp(float(la1), float(lat.min()), float(lat.max()))

    j0 = _idx_from_coord(lo0, lon); j1 = _idx_from_coord(lo1, lon)
    i0 = _idx_from_coord(la0, lat); i1 = _idx_from_coord(la1, lat)
    if j1 < j0: j0, j1 = j1, j0
    if i1 < i0: i0, i1 = i1, i0

    if sample < 1:
        _err("Parameter 'sample' must be >= 1.")
    stride = int(sample)
    nj = (j1 - j0) // stride + 1
    ni = (i1 - i0) // stride + 1
    total = nj * ni
    if total > cfg.POINT_LIMIT:
        _err(f"Too many points ({total:,}). Increase 'sample' or shrink bbox (limit {cfg.POINT_LIMIT:,}).", 400)

    with _open_group(chosen) as ds:
        drop_vars = [v for v in ds.data_vars if v not in fields]
        sub = ds.drop_vars(drop_vars).isel(
            **{lat_name: slice(i0, i1 + 1, stride), lon_name: slice(j0, j1 + 1, stride)}
        ).compute()

        lats = sub[lat_name].values.astype(float)
        lons = sub[lon_name].values.astype(float)

        lon_grid, lat_grid = np.meshgrid(lons, lats)
        cols = {"lon": lon_grid.ravel(), "lat": lat_grid.ravel()}
        for f in fields:
            if f in sub:
                cols[f] = sub[f].values.astype(np.float32).reshape(-1)
            else:
                cols[f] = np.full(lon_grid.size, np.nan, dtype=np.float32)

        rows = []
        for idx in range(lon_grid.size):
            row = {"lon": float(cols["lon"][idx]), "lat": float(cols["lat"][idx]), "date": chosen}
            for f in fields:
                v = cols[f][idx]
                row[f] = None if np.isnan(v) else float(v)
            rows.append(row)

    rows = _apply_modes(rows, modes, fields)
    resp = ORJSONResponse(rows)
    resp.headers["X-Stride"] = str(stride)
    resp.headers["X-Served-Rows"] = str(len(rows))
    return resp
