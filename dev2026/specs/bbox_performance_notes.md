# Bbox Performance Notes

Status: preliminary assessment after the v0.3.0 VM24 deployment.

## Current behavior

Bbox queries remain single-day queries served from the daily store. They do not
use the time-cube because the time-cube was designed for multi-day point/range
access, while bbox is a spatial extraction workload.

Production guard:

```bash
GHRSST_BBOX_POINT_LIMIT=300000
```

This is a front-end safety guard, not a final product decision.

## VM24 observations

Example bbox:

```text
lon0=135&lat0=15&lon1=150&lat1=20&append=sst,sst_anomaly,sea_ice
```

Observed scale:

- Points: 752,001
- JSON payload: about 108 MB
- Server-side local VM24 time: about 4 seconds
- Browser impact: can appear much slower or freeze due to JSON parse/render cost

Smaller bbox:

```text
lon0=135&lat0=15&lon1=140&lat1=20&append=sst,sst_anomaly,sea_ice
```

Observed scale:

- Points: 251,001
- JSON payload: about 36 MB
- Public request time: about 1.5 to 1.8 seconds

## Preliminary critical path

The current critical path is not the same as the old multi-day point-series
failure.

For bbox, the likely bottlenecks are:

- Row-oriented JSON expansion: every point repeats keys such as `lon`, `lat`,
  `date`, `sst`, `sst_anomaly`, and `sea_ice`.
- Browser-side parsing and rendering: 36 to 108 MB JSON can be expensive even if
  the server streams it quickly.
- Large response transfer and front-end memory pressure.
- The daily-store read itself is not currently the dominant observed cost for
  the tested 752k-point bbox.

## Semantics fixed in v0.3.0

Bbox is single-day only. If both `start` and `end` are supplied, they must be the
same date. A request such as:

```text
...&lon0=135&lat0=15&lon1=140&lat1=20&start=2026-01-01&end=2026-12-31
```

now returns HTTP 400 instead of silently using only `start`.

## Candidate improvements

These require a separate design step because they may change client behavior or
add new response modes.

1. Columnar JSON mode

   Return arrays by column, for example:

   ```json
   {
     "date": "2026-01-01",
     "lon": [...],
     "lat": [...],
     "sst": [...],
     "sst_anomaly": [...],
     "sea_ice": [...]
   }
   ```

   This keeps JSON compatibility but removes repeated object keys. It is likely
   the lowest-risk optional mode, e.g. `format=columnar`.

2. Binary/array mode

   Return NetCDF, Zarr region, Arrow IPC, Parquet, or compressed NumPy-like
   arrays. This would be much more efficient but is a larger client contract
   change.

3. Tiled bbox API

   Let clients request or stream spatial tiles. This prevents a single browser
   request from receiving a very large object graph.

4. Front-end sampling/paging contract

   Require `sample`, tile, or a max point count in UI flows. The API already has
   `sample`; the front-end can use it automatically when the estimated point
   count is large.

5. Bbox-specific storage

   Only consider a spatially optimized store if server-side read time becomes
   dominant after response-format changes. Current evidence suggests payload
   format and client handling should be addressed first.

## Recommended next phase

Create a P3 bbox performance spec with:

- A benchmark matrix for 250k, 750k, and 1M points.
- Server timings split into read, row construction, encoding, transfer size, and
  client parse/render time.
- A backward-compatible `format=json` default and an opt-in `format=columnar`
  or binary response.
- Front-end behavior for large bbox queries: auto-sample, tile, or reject with a
  clear hint.
