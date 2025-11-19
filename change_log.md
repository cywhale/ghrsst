#### ver 0.1.0 first-commit ghrsst_app.py  

#### ver 0.1.1 (2025-11-05)
- Enforced single-day bbox behaviour with explicit error messaging when the requested day is missing.
- Added optional truncate mode, bbox `sample` stride (default 1), and API tests covering rounding and bbox validation.
- GHRSST MCP tools now support `method=nearest` fallbacks (≤7-day tolerance) and HTTPS deployment via `/mcp/ghrsst`.

#### ver 0.2.0
- Renamed the MCP surface to `metocean-mcp`, kept GHRSST tools intact, and added the new `tide.forecast` tool for tide/sun/moon contexts at `/mcp/metocean`.
