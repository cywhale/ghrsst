# GHRSST API (MUR v4.1, daily 1-km) — Functional Spec

**Version:** 0.1.0
**Status:** Draft (implementation-ready)
**Service:** `ghrsst_app.py` (FastAPI)
**Owner:** ODB / APIverse
**Runtime:** Python 3.14, FastAPI + Uvicorn/Gunicorn, xarray, zarr v3
**Default base path:** `https://eco.odb.ntu.edu.tw` (dev uses `http://127.0.0.1:8035`)

---

## 1) Purpose

Expose near-real-time daily **GHRSST Level-4 MUR v4.1** fields for:

* `sst` (°C),
* `sst_anomaly` (°C),
* `sea_ice` (fraction 0–1).

Two query modes:

* **Point mode** (lon0,lat0): latest day or a **range ≤ 31 days**.
* **BBox mode** (lon0,lat0,lon1,lat1): **single day** only.

Designed to be consistent with existing ODB API patterns (e.g., `/api/mhw`).

---

## 2) Data Source & Layout

* **Zarr store (v3):** `data/mur.zarr` (project-relative).
  Env override: `GHRSST_ZARR_PATH`.
* **Daily subgroups:** `/YYYY/MM/DD/` per day (e.g., `/2025/11/02`).
* **Coordinates:** `lon` (float32, -180..180), `lat` (float32, -90..90).
* **Variables:** `sst`, `sst_anomaly`, `sea_ice` (float32, NaN for missing).
* **Index file (optional):** `data/latest.json` with:

  ```json
  { "earliest": "YYYY-MM-DD", "latest": "YYYY-MM-DD" }
  ```

  Env override: `GHRSST_INDEX_JSON`.
  If absent, the app scans the Zarr tree for bounds.

**Zarr read options:** `zarr_format=3`, `consolidated=None` (no consolidated metadata).
**Chunking:** utilize dataset’s native chunking; no Dask required.

---

## 3) Endpoint

### `GET /api/ghrsst`

Single endpoint serving both **point** and **bbox** queries.

#### Query parameters

| Name     | Type         | Required | Description                                                                         |
| -------- | ------------ | -------- | ----------------------------------------------------------------------------------- |
| `lon0`   | float        | yes      | Longitude or min-lon in bbox, range [-180,180].                                     |
| `lat0`   | float        | yes      | Latitude or min-lat in bbox, range [-90,90].                                        |
| `lon1`   | float        | no       | Max-lon (bbox mode). If omitted → point mode.                                       |
| `lat1`   | float        | no       | Max-lat (bbox mode). If omitted → point mode.                                       |
| `start`  | `YYYY-MM-DD` | no       | Start date (inclusive). See mode rules below.                                       |
| `end`    | `YYYY-MM-DD` | no       | End date (inclusive). See mode rules below.                                         |
| `append` | string       | no       | Comma list of fields. Allowed: `sst`, `sst_anomaly`, `sea_ice`. **Default:** `sst`. |
| `sample` | int ≥ 1      | no       | **BBox only:** stride/decimation factor. Default `1`.                               |

**Content type:** `application/json` (ORJSON).

---

## 4) Mode & Date Semantics

### 4.1 Point mode (lon0,lat0 only)

* If **no `start/end`**: return **latest** available day (from `latest.json` or scan).
* If **only one** of `start/end` provided: treat as **single day**.
* If **both** provided: return a **range** (inclusive) with rules:

  * Range is **clamped** into `[earliest, latest]`.
  * **Hard cap:** at most **31 days** served (from the clamped `start`).
  * **Missing days** within the clamped range are **skipped**.
  * If the intersection is **empty** → **400** `"Data not exist … available date is EARLIEST/LATEST."`

### 4.2 BBox mode (lon0,lat0,lon1,lat1 and not equal to point)

* **Single day only.**

  * If `start` and `end` given → **use `start`**.
  * If only one given → use that date.
  * If none → **use latest**.
* If the **specified** date is outside `[earliest, latest]` **or** its subgroup folder is missing → **400** `"Data not exist, available date is EARLIEST/LATEST."`
* **Size limit:** `nx * ny ≤ POINT_LIMIT` (see §7).
  Optional `sample` (stride) can be used to reduce `nx`/`ny`.

---

## 5) Output Schema

Each item is a row (point/day). Schema:

```json
{
  "lon": 150.0,
  "lat": 20.0,
  "date": "2025-11-02",
  "sst": 27.43,
  "sst_anomaly": 0.18,
  "sea_ice": null
}
```

* `sst`, `sst_anomaly`, `sea_ice` appear only if requested (via `append`).
* Values are `null` where NaN/missing.
* **Units:** `sst` & `sst_anomaly` in °C, `sea_ice` fraction (0–1).

**Headers (bbox mode only):**

* `X-Stride`: effective stride (`sample`).
* `X-Served-Rows`: total rows returned.

---

## 6) Behavior Details

* **Coordinate selection (point mode):** nearest‐neighbor by index using sorted `lon`, `lat`.
* **BBox normalization:** parameters are reordered if `(lon1 < lon0)` or `(lat1 < lat0)`.
  Then clamped to dataset bounds.
* **Stride math:** number of picked columns/rows is `floor((max_idx - min_idx)/stride) + 1`.
  (Example: indices 10..13, stride=2 → picks 10,12 → 2 = `(13-10)//2 + 1`.)
* **Serialization:** `ORJSONResponse` for performance.
* **No pagination**; enforce result limits with `POINT_LIMIT`.

---

## 7) Limits & Errors

* **POINT_LIMIT:** maximum number of **bbox** grid points per request:

  ```
  total_points = nj * ni
  if total_points > POINT_LIMIT → 400
  ```

  Default `POINT_LIMIT = 1_000_000`. Override via env.

* **Range cap (point mode):** `MAX_DAYS = 31`.

* **Representative errors**

  * `400 Invalid date format. Use YYYY-MM-DD.`
  * `400 Unsupported field(s): ... Allowed: sst,sst_anomaly,sea_ice`
  * `400 Data not exist, available date is EARLIEST/LATEST.`
  * `400 Too many points (N). Increase 'sample' or shrink bbox (limit POINT_LIMIT).`
  * `503 No available dates.` (empty store)

---

## 8) Environment & Config

Environment variables (optional):

```
GHRSST_ZARR_PATH=/abs/path/to/mur.zarr         # default: project_root/data/mur.zarr
GHRSST_INDEX_JSON=/abs/path/to/latest.json     # default: project_root/data/latest.json
GHRSST_POINT_LIMIT=1000000
```

Gunicorn example:

```bash
/home/odbadmin/.pyenv/versions/py314/bin/gunicorn ghrsst_app:app \
  -k uvicorn.workers.UvicornWorker \
  -b 127.0.0.1:8035 --timeout 180
```

PM2 `ecosystem.config.js`:

```js
module.exports = {
  apps: [{
    name: "ghrsst-api",
    script: "/home/odbadmin/.pyenv/versions/py314/bin/gunicorn",
    args: "ghrsst_app:app -k uvicorn.workers.UvicornWorker -b 127.0.0.1:8035 --timeout 180",
    cwd: "/home/odbadmin/python/ghrsst",
    env: {
      GHRSST_ZARR_PATH: "/home/odbadmin/Data/ghrsst/mur.zarr",
      GHRSST_INDEX_JSON: "/home/odbadmin/Data/ghrsst/latest.json",
      GHRSST_POINT_LIMIT: "1000000"
    }
  }]
}
```

---

## 9) Examples

### 9.1 Latest point (default `append=sst`)

```
GET /api/ghrsst?lon0=121&lat0=23.5
```

**200**:

```json
[
  {"lon": 121.0, "lat": 23.5, "date": "2025-11-02", "sst": 26.93}
]
```

### 9.2 Point 7-day range with sst + anomaly

```
GET /api/ghrsst?lon0=121&lat0=23.5&start=2025-10-27&end=2025-11-02&append=sst,sst_anomaly
```

**200** (missing days silently skipped):

```json
[
  {"lon":121.0,"lat":23.5,"date":"2025-10-28","sst":26.71,"sst_anomaly":0.12},
  ...
  {"lon":121.0,"lat":23.5,"date":"2025-11-02","sst":26.93,"sst_anomaly":0.18}
]
```

### 9.3 BBox single day (explicit date)

```
GET /api/ghrsst?lon0=120&lat0=20&lon1=122&lat1=22&start=2025-11-02&append=sst
```

**200**, headers:

```
X-Stride: 1
X-Served-Rows: 16641
```

Body: array of points for that day.

### 9.4 BBox exceeding limit

```
GET /api/ghrsst?lon0=100&lat0=-30&lon1=160&lat1=30&start=2025-11-02
```

**400**:

```
Too many points (36000001). Increase 'sample' or shrink bbox (limit 1,000,000).
```

### 9.5 BBox with out-of-range date

```
GET /api/ghrsst?lon0=120&lat0=20&lon1=122&lat1=22&start=2024-01-01
```

**400**:

```
Data not exist, available date is 2025-11-01/2025-11-02.
```

---

## 10) Non-Goals / Deferrals

* CSV streaming endpoint (can be added later if needed for large bboxes).
* Interpolation options (bilinear). Current selection is nearest neighbor only.
* Pagination (current output bounded by POINT_LIMIT and MAX_DAYS).
* Aggregations (spatial/temporal means) — to be handled at APIverse UI or future endpoints.

---

## 11) Testing Notes

* Unit test compares Zarr vs. NetCDF at random points for a given day:

  * Absolute tolerance: `1e-5` for `sst`, `sst_anomaly`, `sea_ice`.
  * Confirm lon/lat coordinate arrays match.
* Integration smoke tests:

  * **Point latest** (no dates) → 200, length 1.
  * **Point range** across gaps → 200, >0 and ≤31 rows.
  * **BBox** with `sample=1` below limit → 200, rows = `nj*ni`.
  * **BBox** over limit → 400 error.
  * **BBox** out-of-range date → 400 error.

---

## 12) Implementation Hints (for Codex)

* Keep `_fields_from_append()` default to `["sst"]`.
* Use `ORJSONResponse` for data payloads; set headers via an explicit instance in bbox mode.
* Keep date parsing strict `YYYY-MM-DD`.
* Use `_group_exists(day)` (directory check) to skip missing days in point-range mode.
* Re-use computed indices (`jj`, `ii`) for point across days.
* Vectorize bbox extraction (slice → `.compute()` once, then reshape to rows).
* Ensure stride math uses **inclusive** indexing: `floor((j1-j0)/stride) + 1`.

---

## 13) Change Log

* **0.1.0**

  * Default `append` → `sst` only.
  * BBox: single day only; removed soft target row limit; kept hard `POINT_LIMIT`.
  * Point: range allowed (≤31), clamp & skip missing.
  * Paths default to `data/mur.zarr`, `data/latest.json` (project-relative).

