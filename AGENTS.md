# Repository Guidelines

## Project Structure & Module Organization
- `ghrsst_app.py` hosts the FastAPI entry point, combining routing, Zarr access, and validation helpers.
- `dev2026/` contains the 2026 refactor API, store layer, time-cube tooling, and deployment runbooks. As of v0.3.0, VM24 production runs the dev2026 API through the existing `ghrsst` PM2 app.
- `data/` stores the production Zarr tree (`mur.zarr`) plus optional `latest.json`; load MUR v4.1 slices here before serving traffic.
- `conf/` contains deployment scripts (`start_app.sh`, `simu.sh`), PM2 config, and TLS assets; treat certificates as secrets.
- `dev/` provides ingestion and sync utilities (cron, NetCDF→Zarr) that keep the store up to date.
- `tests/` holds validation tooling and sample NetCDF files under `ncfiles/` for parity checks.
- `specs/` captures the API contract (`ghrsst_api_v0_1_0.spec`) and should remain the authoritative interface reference.
- `src/.env` is a template for runtime overrides; copy to `.env` locally instead of editing in place.

## Build, Test, and Development Commands
- `uv venv dev2026/.venv --python 3.13` creates the tested dev2026 environment. Do not assume the production Python version from the legacy app instructions.
- `pip install fastapi uvicorn[standard] gunicorn orjson xarray zarr netCDF4` installs the runtime stack used by the app and parity tooling.
- `uvicorn ghrsst_app:app --reload --port 8035` runs the API for local development with interactive docs at `/docs`.
- `bash conf/start_app.sh` mirrors the production Gunicorn/Uvicorn setup (adjust the pyenv path if needed).

## Coding Style & Naming Conventions
- Follow PEP 8 with 4-space indentation and descriptive `snake_case` names (`_load_bounds`, `_group_exists`).
- Apply type hints on new public helpers and models, matching the patterns in `Cfg` and request validators.
- Keep module-level constants upper-case (e.g., `cfg.ALLOWED_FIELDS`) and minimise new mutable globals.
- Prefer FastAPI dependency injection over module state when extending endpoints.

## Testing Guidelines
- `python tests/test_compare_mur_point.py --nc tests/ncfiles/<file>.nc --zarr data/mur.zarr --date YYYY-MM-DD --verbose` validates NetCDF↔Zarr parity; run this before promoting new datasets.
- Extend deterministic fixtures in `tests/ncfiles/` and name them with the source date (`mur_2025_01_01.nc`) for traceability.
- Document bespoke validation steps in `README.md`, and ensure checks fail fast when required data is missing.

## Commit & Pull Request Guidelines
- Write concise, imperative commit subjects (`add bbox stride guard`) in the style of the existing history.
- Reference tickets or issues in the body and call out data refreshes explicitly (e.g., “refreshed mur.zarr to 2025-02-04”).
- PRs should include: purpose summary, test command output, API/spec changes, and curl traces or screenshots for new behaviour.

## Configuration Notes
- Set `GHRSST_ZARR_PATH` and `GHRSST_INDEX_JSON` to point at alternate stores during local runs; avoid committing absolute paths.
- Keep TLS secrets (`conf/fullchain.pem`, `conf/privkey.pem`) out of forks; replace them with environment mounts or placeholders when sharing.
- VM24 production data lives under `/home/odbadmin/Data/ghrsst/`. The daily store remains the ingestion/recovery source of truth for now; the deployed serving index is the tiered time-cube: `mur_timecube_s8_t90_sh128.zarr` plus `mur_timecube_s8_t90_sh128.delta.zarr`. The daily store has not been removed.
- The active API runtime is `/home/odbadmin/python/ghrsst-dev2026-phase2`, launched by the PM2 definition in `/home/odbadmin/python/ghrsst/conf/start_app.sh`. The legacy `/home/odbadmin/python/ghrsst` tree remains the production launcher/MCP/rollback tree; do not infer the serving code from its Git checkout.
- Do not modify NGINX or unrelated PM2/system processes when working on GHRSST. For a production path swap, acquire the GHRSST ingest lock, stop `ghrsst`, verify port `8035` is down, perform the swap, then start `ghrsst` and verify `/healthz`. Never use ad-hoc `pm2 ... --update-env` for production deployment.
- Multi-day point/range queries should route to the cube (`X-Store-Route: cube`). Point GET and point ranges remain full-history.
- Production enforces the spatial-window policy: bbox GET and `POST /api/ghrsst/points` are allowed only for days present in the delta window (currently 31 days); older spatial queries return 400 with `available_spatial_window`. Keep `GHRSST_ENABLE_COVERAGEJSON=0` until the compact-format/front-end contract is approved.
- The production cube is tiered. Use the delta cube for normal daily appends, the rolling 31-day spatial window, and small gap fixes. For large historical backfills (months/years), rebuild or compact the base cube from the daily store with the bulk builder, then reset delta to the intended recent window. Do not leave large backfills permanently in delta.
- Delta `attrs["days"]` may be physical append order after backfills; do not sort it by hand unless the arrays are rewritten too. Code must use date-to-index mapping for reads and chronological `max(days)` for latest.
- The daily retry schedule intentionally runs twice for the same UTC target day: the early run can obtain data sooner and the later run retries if the upstream was not ready. The daily wrapper skips an existing group; the delta wrapper logs `SKIP_ALREADY_VALID` for a finalized day and must not overwrite it or restart PM2.
- Current delta appends are logged under `/home/odbadmin/Data/ghrsst/logs/delta_append/`; the ingest lock is `/home/odbadmin/Data/ghrsst/mur_delta.ingest.lock`. Preserve append logs and S10 artifacts during incident review.
- The delta append wrapper uses `/home/odbadmin/.npm-global/bin/pm2` explicitly and must wait for `http://127.0.0.1:8035/healthz` after a reload; a failed health check is a nonzero cron result. Daily append reload is not a delta path swap.
- v0.4.1 production checkpoint: the first delta prune/swap retained 34 days (`2026-06-23..2026-07-26`) with measured API downtime of 2.791 seconds. The old delta is held under `/home/odbadmin/Data/ghrsst/hold/`; no hard delete was performed. See `dev2026/specs/p4s10_production_results.md`.
- `/home/odbadmin/Data/ghrsst/hold/` is an operational rollback hold. Never hard-delete hold contents automatically; delete only after the recorded `hold_until` and explicit operator review.
