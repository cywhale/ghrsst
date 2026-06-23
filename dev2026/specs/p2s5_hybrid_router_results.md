# P2-S5 — hybrid router — result: routed, parity-preserved

> Routes each query to the best store (spec §2): multi-day point/range → time-cube; single-day
> point / bbox / `POST /points` → daily P1 store. Routing is performance-only; every route
> returns identical values. Tools: `store/hybrid_router.py`, `api/app.py`, `tests/test_phase2_s5.py`
> (13/13). NON-BINDING (synthetic/regional).

## Routing rule
`HybridRouter(daily, cube)`:
- `route_point(days)` → **'cube'** iff a cube is configured AND the existing requested days number
  >1 AND **every** existing requested day is in the cube (dual-write coverage invariant); else
  **'daily'** (safe fallback). No cube → everything is 'daily' (== P1).
- `points_batch`, `bbox_*` → always daily.
- The GET point endpoint sets **`X-Store-Route: cube|daily`** (observability for canary/tests).
- Cube wired into the API via optional `GHRSST_TIMECUBE_PATH` (unset/missing → daily-only, exactly P1).

## Tests (13/13)
Routing decisions: default-latest single-day → daily; fixed single-day → daily; multi-day range →
cube; no-cube → daily; **uncovered day (in daily, not in cube) → daily fallback**.
Per-route parity (routed == P1): multi-day via cube (3 points); single-day via daily; batch + bbox
via daily; **absent-field preserved through the cube route** (mixed-presence days still omit per P1).
API: default-latest/single-day → `X-Store-Route: daily`; **multi-day → `X-Store-Route: cube` and
values match the P1 daily result**; bbox + POST still work.

Full suite 86/86 (incl. `test_api` / `test_parity` unaffected — no cube env → router = daily = P1).

## Notes / carried forward
- bbox / single-day / batch stay on the daily store **unless a later benchmark proves a cube path
  is better** (spec §2/§4). Not changed here.
- **No promotion claim** — synthetic/regional; binding gate needs real ≥365 contiguous + cold on VM24.
- Next: **P2-S6 dual-write ingest** (the routing's coverage invariant depends on the cube being
  appended in lock-step with daily) + **append-at-scale benchmark**; then P2-S7 full local gate, P2-S8 VM24.
