#### ver 0.1.0 first-commit ghrsst_app.py  

#### ver 0.1.1 (2025-11-05)
- Enforced single-day bbox behaviour with explicit error messaging when the requested day is missing.
- Added optional truncate mode, bbox `sample` stride (default 1), and API tests covering rounding and bbox validation.
- GHRSST MCP tools now support `method=nearest` fallbacks (≤7-day tolerance) and HTTPS deployment via `/mcp/ghrsst`.

#### ver 0.2.0
- Renamed the MCP surface to `metocean-mcp`, kept GHRSST tools intact, and added the new `tide.forecast` tool for tide/sun/moon contexts at `/mcp/metocean`.

#### ver 0.2.1
- small fix for TLS only on NGINX, and Gunicorn use only HTTP upsream/A small test for API/WMS data consistency

#### ver 0.3.0 (2026-06-25)
- Deployed the 2026 refactor API on VM24 through the existing `ghrsst` PM2 app and unchanged NGINX upstream `127.0.0.1:8035`.
- Added tiered time-cube serving for multi-day point/range queries: base cube `mur_timecube_s8_t90_sh128.zarr` plus append-optimized delta cube `mur_timecube_s8_t90_sh128.delta.zarr`.
- Kept the original daily Zarr store as the source of truth and fallback path for single-day, bbox, and batch point requests.
- Added daily delta append cron wrappers after the existing MUR daily ingest retries.
- Reduced default production bbox guard to 300,000 points to avoid browser/front-end freezes from very large row-oriented JSON payloads.
- Fixed bbox date semantics in the refactor API: bbox is single-day only; `start` and `end` must match when both are supplied.
- Set the production range cap to 366 days so one-calendar-year queries work across leap years.
