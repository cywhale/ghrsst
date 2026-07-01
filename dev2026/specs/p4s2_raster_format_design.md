# P4-S2 — compact raster wire format (CoverageJSON-lite) — design spec

Status: **DRAFT — revised per Codex round-1** (Claude authored; Codex reviewed; fixes folded; ready for
sign-off). **Do not implement yet.** Round-1 fixes: media type = `application/vnd.cov+json` (not
`prs.coverage+json`), `application/json` first (§2/§7); `cell_ref=center` is a profile convention, NOT
CRS84 (§1/§2.1/§2.3); absent field OMITS `ranges.<var>` (never `null`) (§2.2); `ghrsst:` prefix + profile
URIs defined (§2.3); both `format=coveragejson` + `format=raster`; `t` resolved from MUR source (09:00Z);
units match row `format=json` exactly (§7). Supersedes the ad-hoc RasterJSON draft in
[`p4_storage_policy_and_bbox_strategy.md`](p4_storage_policy_and_bbox_strategy.md) §5 by aligning it to a
small, stable **CoverageJSON profile**. Default **`format=json` stays unchanged**; the raster format is
**opt-in**. Serves the recent-31-day spatial window only (P4 §4.1).

Standards checked against the OGC **CoverageJSON** Community Standard (21-069r2) + OGC API — Coverages.

## 0. Goal
A **web / API / AI-agent-friendly** compact spatial response: simple, explicit, self-describing, easy to
parse in a browser, and **recognisable to existing geospatial tooling / LLM agents** — without inventing
a bespoke format nor being bound to the full CoverageJSON standard.

## 1. Standards alignment review (the three options)
### Option 1 — full CoverageJSON / OGC coverage model
Structure: `type:"Coverage"` → `domain` (`domainType:"Grid"`, `axes`, `referencing`) → `parameters`
(per-variable metadata) → `ranges` (`NdArray` per variable). Verified facts (21-069r2):
- axes support a **compact regular form** `{start, stop, num}` **and** an explicit `{values:[...]}`;
- `NdArray` = `{type, dataType, axisNames, shape, values}` with `values` a **flat row-major** array (last
  `axisNames` dim varies fastest) and **`null`** for missing;
- CRS via `referencing`, WGS84 lon,lat = `http://www.opengis.net/def/crs/OGC/1.3/CRS84` (CRS84);
- **custom extension members are allowed**.
- **Pros:** an open standard; existing readers (covjson-reader, leaflet-coverage) and AI agents recognise
  it; self-describing; the NdArray IS a flat row-major array (exactly our need); null = nodata built-in.
- **Cons:** the full spec is large (many domainTypes, TiledNdArray, tiling, categorical encodings,
  units/observedProperty URI vocabularies) — more than a single-day gridded SST slice needs; the envelope
  is verbose if taken literally.

### Option 2 — simpler custom RasterJSON (§5 draft)
`{bbox_requested, bbox_actual, nx, ny, x0, y0, dx, dy, axis_order, cell_ref, scan, order, index_formula,
fields:{var:[...]}, field_status, nodata:null}`.
- **Pros:** minimal, dead-simple, every field explicit.
- **Cons:** bespoke — no ecosystem, no tooling, "yet another format" for clients/agents to learn;
  re-invents what CoverageJSON already standardises (axes, CRS, row-major ranges, null).

### Option 3 — binary (GeoTIFF / NetCDF / Arrow) — DEFERRED
Only if a JSON raster still fails the frontend/browser benchmarks (P3-S0 showed compact JSON already
takes 750k from 240 ms/142 MB to ~57 ms/12 MB, so JSON raster is expected to suffice). Not in P4-S2.

### Recommendation — **CoverageJSON-lite profile `ghrsst-raster-json-1`**
A **valid CoverageJSON `Coverage` (Grid) subset** so generic covjson tooling / agents can read it, using:
- the **compact `start/stop/num`** regular axes (small envelope);
- `ranges` as **`NdArray`** flat row-major arrays with `null` nodata (the compact win from P3-S0/S1);
- **CRS84** referencing — this fixes **`axis_order = lon,lat`** (x=lon, y=lat). **`cell_ref = center`** is
  a **GHRSST profile convention** (documented), NOT implied by CRS84 — the axis values are the MUR
  grid-point (cell-center) locations;
- a stable **`"profile":"ghrsst-raster-json-1"`** + **`ghrsst:format_version`** so clients pin behaviour;
- **`ghrsst:`-prefixed convenience members** (`bbox_requested`/`bbox_actual`/`field_status`) that generic
  readers ignore but our frontend/agents can use directly.
This is the user's conclusion: **align to CoverageJSON concepts, don't be bound by the full standard.**

## 2. The `ghrsst-raster-json-1` profile
Opt-in via **`format=coveragejson`** with **`format=raster` as an accepted alias** (both map to this
profile). **Media type:** serve **`application/json`** initially (trivial `fetch().json()` in the
browser); the CoverageJSON registered media type is **`application/vnd.cov+json`** (OGC 21-069r2; file
extension `.covjson`), which also carries an optional **`profile`** parameter per RFC 6906 — so a future
content-negotiated response can be `Content-Type: application/vnd.cov+json; profile="<profile-URI>"`
(§7). `format=json` (row array) is untouched.

```json
{
  "type": "Coverage",
  "profile": "ghrsst-raster-json-1",
  "ghrsst:format_version": 1,
  "domain": {
    "type": "Domain",
    "domainType": "Grid",
    "axes": {
      "x": { "start": 135.005, "stop": 149.995, "num": 1500 },
      "y": { "start": 19.995,  "stop": 15.005,  "num": 500  },
      "t": { "values": ["2026-06-29T09:00:00Z"] }
    },
    "referencing": [
      { "coordinates": ["x","y"], "system": { "type": "GeographicCRS", "id": "http://www.opengis.net/def/crs/OGC/1.3/CRS84" } },
      { "coordinates": ["t"],     "system": { "type": "TemporalRS", "calendar": "Gregorian" } }
    ]
  },
  "parameters": {
    "sst":         { "type": "Parameter", "description": {"en":"Sea surface temperature"},   "unit": {"symbol":"K","label":{"en":"kelvin"}},   "observedProperty": {"label":{"en":"Sea Surface Temperature"}} },
    "sst_anomaly": { "type": "Parameter", "description": {"en":"SST anomaly"},                "unit": {"symbol":"K","label":{"en":"kelvin"}},   "observedProperty": {"label":{"en":"Sea Surface Temperature Anomaly"}} },
    "sea_ice":     { "type": "Parameter", "description": {"en":"Sea ice fraction"},           "unit": {"symbol":"1","label":{"en":"fraction"}}, "observedProperty": {"label":{"en":"Sea Ice Area Fraction"}} }
  },
  "ranges": {
    "sst":     { "type":"NdArray", "dataType":"float", "axisNames":["t","y","x"], "shape":[1,500,1500], "values":[ /* 750000, row-major, null for land */ ] },
    "sea_ice": { "type":"NdArray", "dataType":"float", "axisNames":["t","y","x"], "shape":[1,500,1500], "values":[ /* ... */ ] }
  },
  "ghrsst:field_status": { "sst": "present", "sst_anomaly": "absent", "sea_ice": "present" },
  "ghrsst:bbox_requested": [135, 15, 150, 20],
  "ghrsst:bbox_actual":    [135.005, 15.005, 149.995, 19.995]
}
```

### 2.1 Field-by-field (and how the §5 RasterJSON fields map)
| §5 RasterJSON field | CoverageJSON-lite equivalent |
|---|---|
| `nx` / `ny` | `domain.axes.x.num` / `domain.axes.y.num` |
| `x0` / `y0` | `domain.axes.x.start` / `domain.axes.y.start` (northmost for y) |
| `dx` / `dy` | derived: `(stop-start)/(num-1)` per axis (y is **negative** → north→south) |
| `crs` | `domain.referencing[…].system.id` = CRS84 |
| `axis_order = lon,lat` | fixed by **CRS84** (x=lon, y=lat) |
| `cell_ref = center` | **GHRSST profile convention** (documented §3) — axis values = MUR grid-point/cell centers; NOT implied by CRS84 |
| `scan/order/index_formula` | `NdArray.axisNames` order + **row-major** convention + y-descending (documented §3) |
| `fields:{var:[…]}` | `ranges.<var>.values` (flat row-major, length `nx*ny` per single `t`) |
| `nodata = null` | `null` inside `values` (CoverageJSON standard) |
| `field_status` | `ghrsst:field_status` (present / absent) |
| `bbox_requested`/`bbox_actual` | `ghrsst:bbox_requested`/`ghrsst:bbox_actual` |

### 2.2 Semantics (identical meaning to the row `format=json`, SEMANTIC float32 parity)
- **flat row-major, length `nx*ny`** per variable (single `t`); `values[k]`, `k = iy*nx + ix`.
- **missing/masked/land → `null`**, never skipped (positional integrity; every cell present).
- **whole-field ABSENT** (requested var not present that day) → **OMIT its `ranges` entry entirely** (do
  NOT emit `ranges.<var> = null` — that would be invalid CoverageJSON) AND set
  `ghrsst:field_status.<var> = "absent"` — never a giant all-null array. A **present-but-all-NaN** var
  stays a **full `NdArray` of `null`s** with `field_status = "present"` (preserves the absent-vs-NaN
  distinction, as P3-S1). (`parameters.<var>` may also be omitted for absent vars, or kept for schema
  stability — §7.)
- **y axis descends** (`start` = northmost center, `stop` = southmost) → north→south scan.
- float rendering is orjson shortest-round-trip (same float32 value as the row path; **semantic** parity).

### 2.3 Extension members & profile identity (interoperability — Codex #4)
CoverageJSON permits custom members, but a compact prefix like `ghrsst:` must be **defined**, not ad-hoc:
- **`profile`** identifies this document as the **`ghrsst-raster-json-1`** profile. It also has a
  canonical **profile URI** (e.g. `https://eco.odb.ntu.edu.tw/ns/ghrsst-raster-json-1`) for use as the
  RFC 6906 `profile=` media-type parameter (§2). Exact URI: §7.
- **`ghrsst:`** is a **compact-URI prefix defined by this profile**, expanding to a namespace URI
  (e.g. `https://eco.odb.ntu.edu.tw/ns/ghrsst#`), so `ghrsst:field_status` ≡ `<ns>field_status`. The
  profile document is the authority that defines the prefix→namespace mapping and each member's meaning.
- Generic CoverageJSON readers **ignore** unknown members (forward-compatible); profile-aware clients /
  AI-agents resolve `profile` + `ghrsst:*` per this spec. (A JSON-LD-style `@context` could formalise the
  mapping later; not required for v1.)

## 3. Frontend / API / AI-agent parsing guidance
The payload is self-describing; a client needs only the axes + `ranges`. Reconstruct a coordinate or an
image grid like this (single `t`):
```js
const cov = await (await fetch(url + "&format=coveragejson")).json();
const ax = cov.domain.axes, nx = ax.x.num, ny = ax.y.num;
const x0 = ax.x.start, dx = ax.x.num > 1 ? (ax.x.stop - ax.x.start)/(ax.x.num - 1) : 0;   // lon
const y0 = ax.y.start, dy = ax.y.num > 1 ? (ax.y.stop - ax.y.start)/(ax.y.num - 1) : 0;   // lat (dy < 0, N→S)
const lon = ix => x0 + ix*dx, lat = iy => y0 + iy*dy;
function value(varName, iy, ix) {                 // null = nodata (land/masked)
  const r = cov.ranges[varName];
  if (!r || cov["ghrsst:field_status"][varName] === "absent") return undefined;   // var not available
  return r.values[iy*nx + ix];                    // k = iy*nx + ix (row-major, x fastest)
}
```
Guidance points to document for consumers:
- **CRS84 ⇒ x = longitude, y = latitude** (axis order lon,lat). **Axis values are cell CENTERS** — a
  GHRSST profile convention (matching MUR grid points), not something CRS84 states.
- **`ranges.<var>.values` is flat row-major**, length `nx*ny`; index `k = iy*nx + ix`.
- **`null` = nodata**; **`ghrsst:field_status[var]=="absent"`** = variable not available that day (distinct
  from a present variable that is all-null).
- **`profile` / `ghrsst:format_version`** — clients should check these and treat unknown future versions
  defensively.
- **AI/MCP agents:** the `type:"Coverage"` + `domainType:"Grid"` envelope is recognisable as CoverageJSON;
  the `ghrsst:` members give the bbox + per-field availability without domain-specific inference.

## 4. Examples
### 4.1 Tiny concrete grid (2 lat × 3 lon; sst present, sst_anomaly absent, one land cell)
```json
{
  "type": "Coverage", "profile": "ghrsst-raster-json-1", "ghrsst:format_version": 1,
  "domain": { "type":"Domain","domainType":"Grid",
    "axes": { "x": {"start":135.005,"stop":135.025,"num":3},
              "y": {"start":20.005,"stop":19.995,"num":2},
              "t": {"values":["2026-06-29T09:00:00Z"]} },
    "referencing": [ {"coordinates":["x","y"],"system":{"type":"GeographicCRS","id":"http://www.opengis.net/def/crs/OGC/1.3/CRS84"}} ] },
  "parameters": { "sst": {"type":"Parameter","unit":{"symbol":"K"},"observedProperty":{"label":{"en":"Sea Surface Temperature"}}},
                  "sea_ice": {"type":"Parameter","unit":{"symbol":"1"},"observedProperty":{"label":{"en":"Sea Ice Area Fraction"}}} },
  "ranges": {
    "sst":     {"type":"NdArray","dataType":"float","axisNames":["t","y","x"],"shape":[1,2,3],"values":[290.10, 290.20, null, 289.90, 290.00, 290.10]},
    "sea_ice": {"type":"NdArray","dataType":"float","axisNames":["t","y","x"],"shape":[1,2,3],"values":[0.0, 0.0, null, 0.0, 0.0, 0.0]}
  },
  "ghrsst:field_status": { "sst":"present", "sst_anomaly":"absent", "sea_ice":"present" },
  "ghrsst:bbox_requested": [135.0,19.99,135.03,20.01], "ghrsst:bbox_actual": [135.005,19.995,135.025,20.005]
}
```
Reading: north row (`iy=0`, lat 20.005) = `sst.values[0..2]` = `[290.10, 290.20, null]`; south row
(`iy=1`, lat 19.995) = `values[3..5]`. Cell (`iy=0,ix=2`) is land → `null`. `sst_anomaly` was requested
but absent → no range + `field_status="absent"`.

### 4.2 Whole-field absent
`sst_anomaly` omitted from `ranges`; `ghrsst:field_status.sst_anomaly = "absent"`. (No 750k-null array.)

## 5. Compatibility notes
- **Valid CoverageJSON.** The envelope is a conformant CoverageJSON `Coverage` with a `Grid` domain,
  compact axes, CRS84, and `NdArray` ranges — so covjson-reader / leaflet-coverage / agents that know
  CoverageJSON can parse it. Our extras are **`ghrsst:`-prefixed** custom members (defined in §2.3; safely
  ignored by generic readers).
- **We do NOT implement the full standard:** single `domainType:"Grid"`, single `t`, three fixed
  parameters, CRS84 only, no `TiledNdArray`/tiling, no categorical encodings. The stable `profile`
  fences that subset.
- **Default `format=json` unchanged;** raster is opt-in; **semantic float32 parity** (not byte).
- **Size:** the envelope (domain+parameters, ~1–2 KB fixed) is negligible vs the `values` arrays, so this
  keeps the P3-S0/S1 compact-format wins (≈8× smaller than row JSON, fast parse, small heap).
- **P3-S1 `format=grid`/`columnar`** prototypes are **retired / kept clearly-experimental** once this
  lands (this profile is the finalized compact format).

## 6. Step plan (after sign-off)
- **P4-S2a** implement `format=coveragejson` in `api/app.py` (new `store/bbox_encode.py` encoder from the
  2-D `cols`, like `encode_grid`), default `format=json` untouched; `X-Bbox-Format: coveragejson`.
- **P4-S2b** parity tests (reconstruct rows from the CoverageJSON-lite ↔ row `format=json`, semantic
  float32, absent/nodata) + a bytes/parse bench via the P3 harness (add a CoverageJSON transform).
- gated by the P4 spatial-window policy (served only for delta-window days; older → 4xx).

## 7. Resolved (this review) + open questions
**Resolved (Codex round-1):**
- **Param name:** support **both** `format=coveragejson` (canonical) **and** `format=raster` (alias) → this profile.
- **Media type:** serve **`application/json`** first (browser `fetch().json()`); add
  **`application/vnd.cov+json`** (+ RFC 6906 `profile=` param) via content negotiation later — the correct
  registered type (NOT `application/prs.coverage+json`).
- **`t` axis:** resolve from the **MUR source metadata/filename**, not a placeholder; MUR's nominal daily
  analysis time is **`T09:00:00Z`** — encode `axes.t.values = ["<date>T09:00:00Z"]` (confirm against the
  actual source attribute at implementation).
- **Units:** emit values that are **byte-for-value identical to the row `format=json`** wire values; the
  `parameters.<var>.unit` label MUST describe exactly those values (no on-the-wire unit conversion).
- **Absent field:** OMIT `ranges.<var>` (never `null`); `ghrsst:field_status="absent"` (§2.2).
- **`cell_ref=center`** is a profile convention, not CRS84 (§2.1/§2.3).

**Still open:**
1. **Confirm the `t` analysis time** value from MUR source metadata (expected `09:00:00Z`) and the exact
   wire **units** the row `format=json` currently emits (K vs °C; fraction vs %) so `parameters` match.
2. **Profile / namespace URIs (§2.3):** confirm the canonical `profile` URI and the `ghrsst:` namespace URI
   (proposed `https://eco.odb.ntu.edu.tw/ns/ghrsst-raster-json-1` / `…/ns/ghrsst#`).
3. **`observedProperty.id` URIs:** include canonical vocabulary URIs (CF standard names) or labels only?
4. **`parameters` for absent vars:** omit, or keep for schema stability (§2.2)?

## Standards references
- OGC CoverageJSON Community Standard, 21-069r2 (2023-08-22) — https://docs.ogc.org/cs/21-069r2/21-069r2.html
- OGC API — Coverages — https://ogcapi.ogc.org/coverages/
- CoverageJSON spec — https://covjson.org/spec/ ; media type `application/vnd.cov+json`, ext `.covjson`.
