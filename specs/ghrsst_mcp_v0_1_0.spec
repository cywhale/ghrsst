# GHRSST MCP Server — Tooling Spec (v0.1)

**Goal**
Expose two small, high-signal MCP tools (not a REST dump) that LLMs can reliably use:

1. **`ghrsst.point_value`** – single point, single day
2. **`ghrsst.bbox_mean`** – average over a bbox, single day (auto-downsample to respect API limits)

Backed by your deployed REST at `/api/ghrsst`.

---

## 0) Server layout & config

**Folder:** `~/python/ghrsst/mcp/`

**Env vars**

```
GHRSST_API_BASE=https://eco.odb.ntu.edu.tw/api/ghrsst # REST base for /api/ghrsst
GHRSST_TIMEOUT_S=15                           # HTTP timeout per call
GHRSST_POINT_LIMIT=1000000                    # mirror REST limit for sanity
GHRSST_DEG_PER_CELL=0.01                      # MUR v4.1 ~1km grid (~0.01°)
GHRSST_NEAREST_TOLERANCE_DAYS=7               # Max day-gap allowed for method=nearest fallback
```

**Transport**

* Run FastMCP with HTTP transport bound to `0.0.0.0:8765` and `--path /mcp/ghrsst`.
* Terminate TLS at NGINX. Externally the endpoint is `https://eco.odb.ntu.edu.tw/mcp/ghrsst`, internally proxy to `http://127.0.0.1:8765/mcp/ghrsst`.
* Example NGINX snippet (HTTP pass-through, HTTPS handled by NGINX):

  ```nginx
  upstream ghrsstmcp {
    server 127.0.0.1:8765;
  }

  location /mcp/ghrsst {
    rewrite ^/(.*)$ /$1 break;
    proxy_redirect off;
    proxy_pass http://ghrsstmcp;
    include /etc/nginx/conf2.d/aio_cache_proxy.conf;
  }
  ```

* No additional CORS headers required for server-to-server calls; add only if a browser client appears.
* **No auth** assumed (internal network). If needed, add an `Authorization` header field in config.

**Dependencies**

* HTTP client with timeouts & retries (3x, backoff; treat 429/5xx as retryable).
* JSON parser; nothing heavy.

---

## 1) Tool: `ghrsst.point_value`

**Purpose**
Return SST and/or SST anomaly at a **single** lon/lat for a **single day** (default latest).

**Input schema (JSON Schema)**

```json
{
  "type": "object",
  "required": ["longitude", "latitude"],
  "properties": {
    "longitude": { "type": "number", "minimum": -180, "maximum": 180, "description": "Decimal degrees, east positive." },
    "latitude":  { "type": "number", "minimum":  -90, "maximum":  90, "description": "Decimal degrees, north positive." },
    "date":      { "type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$", "description": "YYYY-MM-DD. Optional; defaults to latest." },
    "fields":    {
      "type": "array",
      "items": { "enum": ["sst", "sst_anomaly"] },
      "minItems": 1,
      "uniqueItems": true,
      "description": "Defaults to ['sst'] if omitted."
    },
    "method": {
      "type": "string",
      "enum": ["exact", "nearest"],
      "default": "exact",
      "description": "exact (default) requires the requested date; nearest falls back to the closest available day."
    }
  }
}
```

> Rationale: **array** is clearer for LLMs than comma strings.

**Behavior**

* If `date` omitted → use latest (let REST default handle it).
* Build REST call:
  `/api/ghrsst?lon0={longitude}&lat0={latitude}&start={date?}&append={csv(fields or ['sst'])}`
* If `method=nearest` and the requested day is missing, re-issue the call using whichever of `[earliest, latest]` is closest (parsed from the API error) **provided the gap ≤ GHRSST_NEAREST_TOLERANCE_DAYS (default 7)**. Return `method="nearest"` and `requested_date` when fallback occurs.
* Expect one row back; map to MCP output.

**Output schema**

```json
{
  "type": "object",
  "required": ["region", "date"],
  "properties": {
    "region": { "type": "array", "items": { "type": "number" }, "minItems": 2, "maxItems": 2, "description": "[lon, lat]" },
    "date":   { "type": "string" },
    "sst":         { "type": ["number", "null"] },
    "sst_anomaly": { "type": ["number", "null"] }
  }
}
```

**Errors**

* If REST returns empty array → `NOT_FOUND`: “No data for {date}; available range may differ.”
* If REST 400 with “Data not exist, available date is E/L” → surface same message (unless method=nearest triggered a fallback within tolerance).
* Network/timeout → `UNAVAILABLE` with retry_hint.

**Examples**

* Input:

  ```json
  {"longitude":121.5,"latitude":23.7,"date":"2025-11-02","fields":["sst","sst_anomaly"]}
  ```

  Output:

  ```json
  {"region":[121.5,23.7],"date":"2025-11-02","sst":26.93,"sst_anomaly":0.18}
  ```

---

## 2) Tool: `ghrsst.bbox_mean`

**Purpose**
Return **mean** SST / SST anomaly over a bbox for a **single day**.
Automatically choose a `sample` (stride) so the REST request stays under `POINT_LIMIT`.

**Input schema**

```json
{
  "type": "object",
  "required": ["bbox", "date"],
  "properties": {
    "bbox": {
      "type": "array",
      "minItems": 4,
      "maxItems": 4,
      "items": { "type": "number" },
      "description": "[lon0, lat0, lon1, lat1] (can be unordered; tool will normalize)"
    },
    "date":   { "type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$" },
    "fields": {
      "type": "array",
      "items": { "enum": ["sst", "sst_anomaly"] },
      "minItems": 1,
      "uniqueItems": true,
      "description": "Defaults to ['sst']."
    },
    "method": {
      "type": "string",
      "enum": ["exact", "nearest"],
      "default": "exact",
      "description": "exact requires the requested date; nearest falls back to the closest available day when the request misses."
    }
  }
}
```

**Sampling logic (pre-flight)**

* Normalize bbox: `(lon0,lon1) = sorted`, `(lat0,lat1) = sorted`.
* Estimate grid counts at 1-km MUR:

  ```
  DEG = GHRSST_DEG_PER_CELL (~0.01)
  nx ≈ floor((lon1 - lon0) / DEG) + 1
  ny ≈ floor((lat1 - lat0) / DEG) + 1
  total = nx * ny
  ```
* If `total <= POINT_LIMIT` → `sample = 1`.
  Else → `sample = ceil( sqrt(total / POINT_LIMIT) )`.

  * This keeps `(nx/ sample)*(ny/ sample) ≲ POINT_LIMIT`.
* Then call REST with that `sample`.

**REST call**

```
/api/ghrsst?lon0=...&lat0=...&lon1=...&lat1=...&start={date}&append={csv(fields)}&sample={sample}
```

* If `method=nearest` and the requested day is missing, retry with whichever of `[earliest, latest]` is closest **so long as the gap ≤ GHRSST_NEAREST_TOLERANCE_DAYS**; include `method="nearest"` and `requested_date` in the output when fallback happens.

**Aggregation**

* Parse JSON rows; for each requested field, compute **mean ignoring null/NaN**.
* If all nulls → return `null` for that field.
* Also return the effective `sample` for transparency.

**Output schema**

```json
{
  "type": "object",
  "required": ["region", "date", "sample"],
  "properties": {
    "region": { "type": "array", "items": { "type": "number" }, "minItems": 4, "maxItems": 4, "description": "[lon0,lat0,lon1,lat1]" },
    "date":   { "type": "string" },
    "sample": { "type": "integer", "minimum": 1 },
    "sst":         { "type": ["number", "null"] },
    "sst_anomaly": { "type": ["number", "null"] }
  }
}
```

**Errors**

* If REST responds `400 Data not exist…` (date out of range) → surface same text (unless method=nearest triggered a fallback).
* If after downsampling the server still returns `Too many points` (rare if estimate was off) → multiply `sample` by 2 and retry up to 2 times; otherwise error.
* Network/timeout → `UNAVAILABLE` with retry_hint.

**Examples**

* Input:

  ```json
  {"bbox":[119,20,123,26],"date":"2025-11-02","fields":["sst","sst_anomaly"]}
  ```

  Output:

  ```json
  {"region":[119,20,123,26],"date":"2025-11-02","sample":1,"sst":26.42,"sst_anomaly":0.21}
  ```

---

## 3) Shared conventions

* **Fields default** to `["sst"]` if omitted.
* **Units**: `sst`/`sst_anomaly` in °C. (No unit strings in output; keep compact.)
* **Date**: **required** for bbox tool; **optional** for point tool (defaults to latest).
* **Numeric precision**: keep Python floats; MCP runtime will serialize JSON (no rounding).
* **Time bounds**: Let REST enforce availability (no separate index read in MCP).

---

## 4) Tool metadata (names & descriptions)

* `ghrsst.point_value`
  *“Get MUR (1-km) sea surface temperature and/or anomaly at a single lon/lat for a single date (defaults to latest).”*

* `ghrsst.bbox_mean`
  *“Get mean MUR (1-km) sea surface temperature and/or anomaly over a bounding box for a single date. The tool auto-downsamples to respect server point limits.”*

Keep names short, verb-first where possible; these are easy for LLMs to pick.

---

## 5) Pseudocode (robustness essentials)

**point_value**

```python
def point_value(longitude, latitude, date=None, fields=None):
    fields = fields or ["sst"]
    qs = {
      "lon0": longitude, "lat0": latitude,
      "append": ",".join(fields)
    }
    if date: qs["start"] = date

    r = http_get(API_BASE + "/api/ghrsst", qs, timeout=TIMEOUT)
    if r.status == 400 and "Data not exist" in r.text: raise MCPError("NOT_FOUND", r.text)
    r.raise_for_status()
    arr = r.json()
    if not arr: raise MCPError("NOT_FOUND", "No data for requested parameters")
    row = arr[0]
    return {
      "region": [longitude, latitude],
      "date": row["date"],
      **({ "sst": row.get("sst") } if "sst" in fields else {}),
      **({ "sst_anomaly": row.get("sst_anomaly") } if "sst_anomaly" in fields else {})
    }
```

**bbox_mean**

```python
def bbox_mean(bbox, date, fields=None):
    fields = fields or ["sst"]
    lon0, lat0, lon1, lat1 = normalize_bbox(*bbox)
    nx_est = int((abs(lon1 - lon0) / DEG_PER_CELL)) + 1
    ny_est = int((abs(lat1 - lat0) / DEG_PER_CELL)) + 1
    total  = nx_est * ny_est
    sample = 1 if total <= POINT_LIMIT else math.ceil(math.sqrt(total / POINT_LIMIT))

    for attempt in range(3):  # 1 initial + 2 backoffs if server still complains
        qs = {
          "lon0": lon0, "lat0": lat0, "lon1": lon1, "lat1": lat1,
          "start": date,
          "append": ",".join(fields),
          "sample": sample
        }
        r = http_get(API_BASE + "/api/ghrsst", qs, timeout=TIMEOUT)
        if r.status == 400 and "Too many points" in r.text:
            sample *= 2
            continue
        if r.status == 400 and "Data not exist" in r.text:
            raise MCPError("NOT_FOUND", r.text)
        r.raise_for_status()
        arr = r.json()
        break
    else:
        raise MCPError("FAILED_PRECONDITION", "Could not get data under point limit after retries")

    means = {}
    for f in fields:
        vals = [x.get(f) for x in arr if x.get(f) is not None]
        means[f] = (sum(vals)/len(vals)) if vals else None

    out = { "region": [lon0, lat0, lon1, lat1], "date": date, "sample": sample }
    if "sst" in fields: out["sst"] = means["sst"]
    if "sst_anomaly" in fields: out["sst_anomaly"] = means["sst_anomaly"]
    return out
```

---

## 6) Validation & messaging guidelines

* Keep error strings **short and actionable**; prefer surfacing REST’s clear messages when relevant.
* Always echo the **effective `sample`** in `bbox_mean` output.
* Reject obviously invalid inputs before calling REST (bad date format, bbox degenerate, fields empty).

---

## 7) Test checklist (MCP level)

* `point_value`:

  * latest-by-default returns 200 with one row.
  * explicit date in range → 200.
  * date out of range → NOT_FOUND, message contains “available date is”.
* `bbox_mean`:

  * small bbox (e.g., 0.1° × 0.1°) → `sample=1`, returns numeric means.
  * very large bbox → tool computes `sample>1`, server accepts; output includes `sample`.
  * out-of-range date → NOT_FOUND.
* Network errors → retried up to 3x; then UNAVAILABLE.

---

## 8) Future extensions

* Add optional `field="sea_ice"` support to both tools.
* Add a “regional percentile” tool (e.g., p90 of anomaly).
* Add a **resource** that streams a small geojson grid for visualization (separate from these tools).

---

**Summary**
Two curated tools, clean inputs/outputs, built-in downsampling logic, and clear error propagation. This keeps the LLM’s action space small and reliable while fully leveraging your high-performance `/api/ghrsst` backend.
