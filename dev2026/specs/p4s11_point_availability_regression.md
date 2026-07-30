# P4-S11 — post-prune point-availability regression: root cause + fix

Status: **FIXED locally; tests green; NOT yet deployed.** Found by Codex/ops on VM24 right after the
first real P4 daily-staging prune (2026-07-28, production v0.4.1). Claude reproduced it locally,
fixed it, and ran the full regression; **VM24 deployment, rollback and the `hold` lifecycle stay with
Codex/ops** — Claude touched no production system.

## Symptom (production)

After the prune left daily at 38 days (`2026-06-21..2026-07-28`) while base+delta still held the full
1305-day history, **full-history point queries began returning 400**:

```
GET /api/ghrsst?lat0=32&lon0=-69&start=2025-01-01&end=2025-12-31&append=sst,sst_anomaly
-> 400  {"detail":"Data not exist for requested period; available range is 2026-06-21/2026-07-28."}
GET /api/ghrsst?lat0=32&lon0=-69&start=2025-06-21&end=2025-06-21
-> 400  (same)
```

Recent paths were healthy (28-day range 200 via cube; latest single-day 200; bbox/POST 200; older
bbox/POST 400 per policy). **No data was lost** — the history was in base+delta the whole time and the
1267 pruned daily groups are still in `hold` (never hard-deleted). This was purely an *availability
bookkeeping* fault in the serving layer.

## Root cause — three compounding defects, one wrong assumption

The wrong assumption, inherited from P1/P2 and never revisited: **the daily store is the availability
authority**. That was true while daily held the full history. The P4 prune made it false, and the
first prune exposed all three places that relied on it:

| # | site | defect |
|---|---|---|
| 1 | `api/app.py` point branch | the requested range is clamped to `store.bounds()` (**daily**): `if s < earliest: s = earliest`. A 2025 request became `s=2026-06-21, e=2025-12-31` → `s > e` → **empty range** before any routing happened. This is why even a *single* historical day 400'd. |
| 2 | `api/app.py` point branch | `existing = [d for d in wanted if store.day_present(d)]` — membership filtered by **daily** only. |
| 3 | `store/hybrid_router.py` `route_point` | pre-filtered by `daily.day_present` **and** required `len(existing) > 1`, so a historical single-day point could never reach the cube even if the other two were fixed. |

Defect 1 is the one that made the failure total (and fast — ~15 ms, no store read at all).

## Fix — separate the two availability scopes

P4 has **two different** availability rules; the code had one. They are now explicit:

- **POINT availability = cube ∪ daily** (full history). New abstraction on `HybridRouter`:
  `point_days()`, `point_bounds()`, `point_primary_bounds()`, `point_day_present(day)`.
  `TieredCube.days` was added (chronological base∪delta union) to back it — documented as an
  *availability* view only, **not** for indexing arrays (the append-order invariant, P4-S4 §2, still
  requires per-tier `day_index`).
- **SPATIAL availability = daily + delta membership** (recent window) — unchanged: bbox and
  `POST /points` still use `store.bounds()` / `store.day_present()` and are still gated by
  `_spatial_window_gate`. **The fix deliberately does not widen the spatial contract.**

`StoreAccess.primary_bounds`'s longest-contiguous-run algorithm was extracted to a module-level
`primary_contiguous_bounds(days)` so the daily store and point availability share one implementation
(no duplicated logic, identical "available contiguous range is …" semantics).

### Routing

`route_point` now: **cube** when the cube covers every requested day (single-day included) → **daily**
when the daily store does → **mixed** otherwise. Two notes:

- *Single-day now prefers the cube.* This changes the P2-S5 preference (which pinned single-day to
  daily when daily was full-history). It is required for correctness post-prune, and it is also
  faster: P4-S0b VM24 measured cube 4.5 ms vs daily 14.7 ms; the local gate measures cube 0.68 ms vs
  daily 0.86 ms. Values are identical — asserted in `test_parity_single_day_via_cube` and at the API
  level.
- *`mixed` is new.* Previously impossible (the daily pre-filter hid it). It triggers when neither tier
  alone covers the request — e.g. a range spanning history plus a brand-new day that daily has but the
  delta append has not yet picked up. Serving such a request from one store would **silently truncate**
  the series, which is worse than the 400 being fixed; instead each day is served by the tier that has
  it and the rows are merged in requested order. `X-Store-Route: mixed`, counted in
  `route_counts["mixed"]`.

### Gate ordering on the spatial paths (round-2 review fix)

A second, smaller instance of the same stale assumption survived the first pass: bbox and
`POST /points` checked **daily availability before** `_spatial_window_gate`. Pre-prune that was
invisible (daily held every day, so only the gate could reject); post-prune a historical day is
missing from the pruned daily store too, so the daily check fired first and answered a *spatial*
request with the **daily staging range** — telling clients the spatial window was
`2026-06-21/2026-07-28` instead of returning the adopted policy payload with
`available_spatial_window`.

Both paths now run `_spatial_window_gate` **first**. Because the gate is already a no-op when
enforcement is off or no delta tier is loaded, transition configurations keep the daily-availability
message unchanged — asserted by `test_transition_no_delta_keeps_daily_availability_message`.
`store/spatial_policy.py`'s docstring (still claiming "NOT yet wired … P4-S3 will wire it") was
corrected to describe the shipped enforcement, this ordering rule, and the point-vs-spatial scope
split.

### `/healthz`

`earliest` / `latest` / `primary_*` now describe **public point availability** (the union), so a client
can no longer read the 38-day staging window as "all the data there is". Added, per the fix contract:
`point_earliest`, `point_latest`, `point_day_count`, and `daily_earliest`, `daily_latest`,
`daily_day_count`. `cube_*` / `delta_*` are unchanged. Point 400s now quote the full point range;
bbox/POST rejections still quote the spatial window.

## Reproduction (local, no VM24)

`tests/test_phase2_p4_point_availability.py` builds the post-prune shape:
`daily 38 d (06-21..07-28)` · `base 38 d (05-20..06-26)` · `delta 36 d (06-23..07-28)` ·
union 70 d — union > daily, with 32 base-only history days.

Evidence, same tests, source reverted vs fixed:

```
BEFORE (source at v0.4.1):  Ran 22 tests — FAILED (failures=11, errors=2)
  historical single-day -> AssertionError: 400 != 200 :
  {"detail":"Data not exist for requested period; available range is 2026-06-21/2026-07-28."}
AFTER  (fix applied):       Ran 22 tests — OK
```

The pre-fix failure reproduces the production error **string-for-string**. All five spatial tests pass
*both* before and after — the spatial contract is untouched by the fix.

## Test coverage

`test_phase2_p4_point_availability.py` (22): historical single-day (route=cube + value parity vs a
direct cube read); historical range; base→delta crossing range (order + per-day parity via
`TieredCube`); full-union 70-day range; latest single-day (+parity vs daily); default-latest;
clamp-not-error for a range starting before history; genuinely-out-of-range 400 quoting the **full**
range; MAX_DAYS 413; Cache-Control; bbox/POST rejected outside the delta window (incl. a day that is
in daily but not in delta) and served inside it; healthz union/daily/cube/delta fields; **mixed-route
no-truncation** (+ pure cube-only / daily-only sub-ranges); no-cube P1 fallback.

`test_phase2_p4_point_availability_perf.py` (4) — local gates, synthetic 32×32 grid but the **real
production cube chunk geometry** `(90, 8, 8)`:

| path | measured p95 | bar |
|---|---|---|
| historical 366-day range (route=cube) | **7.1 ms** | < 4 s |
| historical single-day (route=cube) | **1.3 ms** | < 1 s |
| recent 28-day range / bbox / POST | 9.1 / 1.7 / 1.6 ms | < 2 s |
| single-day cube vs daily | 0.68 ms vs 0.86 ms | cube not worse |

These do **not** claim VM24 binding performance — that is measured by Codex/ops after deployment.

Updated: `tests/test_phase2_s5.py` — the four assertions that pinned single-day to `daily` now assert
the P4 rule, each strengthened to also prove value parity (the routing switch must not change
semantics). Full suite: **255 tests, OK (17 pre-existing skips)**.

## Standing invariants this restores / records

- **Full-history point authority = TieredCube (base+delta).** Daily is *recent staging only*.
- **Spatial availability = delta membership** (unchanged, still enforced).
- Point GET: single-day and range (≤ 366 days) = full history. bbox + POST: recent window only.
- **`hold` must not be hard-deleted** until this fix is deployed and production edge tests pass —
  the 1267 pruned daily groups remain the local re-derivation path if anything else surfaces.

## Deployment (Codex/ops)

Fetch and check out the fix commit on `dev2026-p4-point-availability-fix` (stacked on the v0.4.1
production tip `9a8d3d4`). After deployment, re-run the production edge checks: the two 400-ing
historical queries above must return 200 with `X-Store-Route: cube`; the recent range/bbox/POST and
the older-bbox/POST 400s must be unchanged; `/healthz` must show `earliest` at the true history start
with `daily_day_count` ≈ 38. Only after that is the `hold` cleanup decision in scope — and it remains
ops-only, after `hold_until`.
