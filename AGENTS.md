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
- `python -m venv .venv && source .venv/bin/activate` creates an isolated Python 3.14 environment as expected by deployment scripts.
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
- VM24 production data lives under `/home/odbadmin/Data/ghrsst/`. The daily store remains the source of truth; the deployed time-cube paths are `mur_timecube_s8_t90_sh128.zarr` plus `mur_timecube_s8_t90_sh128.delta.zarr`.
- Do not modify NGINX or unrelated PM2/system processes when working on GHRSST. The deployed API is switched through `/home/odbadmin/python/ghrsst/conf/start_app.sh`; rollback uses `start_app.sh.pre-dev2026`.
- Multi-day point/range queries should route to the cube (`X-Store-Route: cube`). Bbox queries are still daily-store, single-day JSON-array responses and have a production point guard; see `dev2026/specs/bbox_performance_notes.md` before changing bbox behavior.
