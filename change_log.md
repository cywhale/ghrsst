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
- Restored Swagger/OpenAPI under `/api/swagger/ghrsst` and `/api/swagger/ghrsst/openapi.json`.
- Added the daily-only gap day `2025-06-22` to the delta cube so ranges crossing that day still route to the cube.
- User-facing unavailable-date errors now report the main contiguous production range, excluding isolated test days such as `2023-03-06`.

#### ver 0.3.1 (2026-07-01)
- Rebuilt VM24 base time-cube to cover `2023-01-01..2026-06-26` and backfilled the delta cube to a 31-day spatial window (`2026-05-30..2026-06-29` at deployment time).
- Enabled production spatial-window enforcement: bbox GET and `POST /api/ghrsst/points` are available only for days in the delta window; older spatial requests return HTTP 400 with `available_spatial_window`.
- Kept point GET and point time-series/range queries full-history through the tiered cube.
- Enabled cube metadata refresh (`GHRSST_CUBE_REFRESH_TTL_SECONDS=300`) so cron-appended delta days become visible without a routine PM2 restart.
- Fixed chronological latest handling for append-order delta metadata. Backfilled delta `attrs["days"]` may not be sorted; the API now reports latest using chronological `max(days)` while preserving physical day-to-index mapping.
- Left CoverageJSON-lite (`format=coveragejson` / `format=raster`) disabled in production until the compact bbox wire-format contract is finalized.

#### ver 0.4.0 (2026-07-10)
- Completed the P4 time-cube operational rollout on VM24: base time-cube plus append-optimized delta serving, with production point/range queries routed through the cube.
- Enforced the recent spatial policy for bbox and `POST /api/ghrsst/points`; spatial availability follows finalized delta membership, while point and range queries retain full-history coverage.
- Added TTL-based cube metadata refresh (`GHRSST_CUBE_REFRESH_TTL_SECONDS=300`) and finalized-day idempotency in the delta cron wrapper: retries skip valid days without rewriting Zarr or restarting PM2.
- Completed the S10 shadow-to-production swap rehearsal and rollout safety model: fresh audit, lock/staleness guard, stop-before-swap quiescence, healthz verification, rollback/hold artifacts, and no automated hard delete.
- Retained the full daily Zarr as ingestion/recovery staging for now. It has **not** been removed; daily pruning and long-term compaction remain separate, gated operations.
- Kept CoverageJSON-lite disabled in production (`GHRSST_ENABLE_COVERAGEJSON=0`) until the frontend/API contract is finalized.

#### ver 0.4.1 (2026-07-28)
- Added fail-fast `/healthz` verification after the scheduled delta append PM2 reload; a failed recovery is now visible in the append log and exits nonzero.
- Completed the first production delta prune/swap rehearsal: retained 34 finalized days (`2026-06-23..2026-07-26`), moved the previous delta to the rollback hold area, and verified point/range, recent bbox, historical bbox rejection, and `/healthz`.
- Recorded measured API downtime during the stop-before-swap sequence: 2.791 seconds from port-down to healthz recovery.
- Daily Zarr remains the ingestion/recovery staging source; daily staging prune and hard-delete cleanup remain separate future operations.

#### ver 0.5.0 (2026-07-30)
- Completed the first production daily-staging prune: retained 38 recent daily groups (`2026-06-21..2026-07-28`) and moved 1,267 historical groups to the rollback hold area. No hard delete was performed.
- Made the tiered time-cube authoritative for full-history point availability. Point GET and ranges now derive bounds and membership from the base+delta+daily union rather than the recent daily staging window.
- Added cube-first single-day routing and a `mixed` route for the ingest-lag edge case, preventing silent truncation when a range spans cube history plus a daily-only newest day.
- Preserved the spatial contract: bbox GET and `POST /api/ghrsst/points` remain limited to delta membership. Their policy gate now runs before daily-staging availability checks, so historical requests consistently return `available_spatial_window`.
- Extended `/healthz` with explicit `point_*` and `daily_*` availability fields. At deployment, point history was `2023-01-01..2026-07-28` (1,305 days), while daily staging was 38 days.
- Verified VM24 production performance after pruning: historical 365/366-day ranges completed in about 76–100 ms through the cube; base→delta crossing range in about 219 ms; recent bbox in 39 ms; recent `POST /points` in 31 ms.
- Deployed source logic from commit `5099613` while preserving the VM24-specific Swagger documentation. PM2 recovery took 1.422 seconds. Deployment artifacts are under `/home/odbadmin/Data/ghrsst/logs/p4_point_availability_deploy_20260730T064815Z`.
