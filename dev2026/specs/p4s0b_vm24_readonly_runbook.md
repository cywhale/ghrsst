# P4-S0b — VM24 READ-ONLY binding gate runbook (for Codex / ops)

**Claude authored this; Codex/ops EXECUTE it on VM24.** Validates the adopted P4 §4.1 policy against the
**real production base+delta cube, strictly READ-ONLY**. It confirms the cube can serve the daily-routed
paths at production scale before any storage-policy commitment. It **does NOT authorize pruning** and
**does NOT mutate anything**.

## Hard read-only / ops boundary
- Every store is opened `mode='r'`; the harness imports **no** write path (no append/compact/prune).
- HTTP issued is only **GET** (point/range, `/healthz`) and **POST /api/ghrsst/points** (a read query).
- **Do NOT**: write base/delta/daily, change cron, change NGINX, restart/redeploy, or backfill delta.
- Runs read load (small `C=4/8` bursts) against the cube files — bounded; run off-peak if desired.

## Caveat — what this gate can and cannot prove
Production delta currently holds only **~2 days** (`2026-06-27..2026-06-28`). So this gate validates
**per-day delta performance** (bbox/POST on the existing delta days) — **NOT** the full
`SPATIAL_WINDOW_DAYS = 31` retention policy (there aren't 31 delta days to serve yet). The full-window
test is a **separate** shadow/staging action: `bench/p4s0b_shadow_31day.py` on a shadow delta or an
**explicitly-approved** staging backfill — **never** under this read-only gate.

## Preconditions
- Production paths (read-only): daily `…/mur.zarr`, base `…/mur_timecube_s8_t90_sh128.zarr`,
  delta `…/mur_timecube_s8_t90_sh128.delta.zarr` (see dev2026/README VM24 section).
- The dev2026 venv on the runtime worktree; production API up on `127.0.0.1:8035` (optional, for the
  API cross-check + `/healthz`).

## Run
```bash
cd <runtime worktree>            # e.g. /home/odbadmin/python/ghrsst-dev2026-phase2
dev2026/.venv/bin/python dev2026/bench/p4s0b_readonly_gate.py \
  --daily /home/odbadmin/Data/ghrsst/mur.zarr \
  --base  /home/odbadmin/Data/ghrsst/mur_timecube_s8_t90_sh128.zarr \
  --delta /home/odbadmin/Data/ghrsst/mur_timecube_s8_t90_sh128.delta.zarr \
  --api-url http://127.0.0.1:8035 \
  --out /home/odbadmin/Data/ghrsst/logs/p4s0b_readonly_$(date -u +%Y%m%d).json
```

## What it validates (→ artifact JSON)
- **A. full-history point/range** from base+delta (cube) + latency + **sampled parity vs daily** (a few
  days across the range); optional API `X-Store-Route: cube`.
- **B. single-day point GET** — cube vs daily **parity** + latency (cube ≈ or faster than daily).
- **C. bbox + POST /points on EXISTING delta days** — per delta day: latency (warm p95 + `C8`),
  `read_amp`/`chunk_count`, and **parity** vs the daily store for that day.
- **D. policy dry-run** — delta days are **served**; a historical/base day is **rejected** by the
  spatial policy (`store/spatial_policy.py`, delta-membership). Enforcement is not yet wired into the
  API (P4-S3) — this validates the decision logic.
- **E. `/healthz` + RSS + API POST smoke** — `cube_kind`/`cube_latest`/`delta_day_count`/`rss_mb` +
  harness process RSS, and a read-only **POST `/api/ghrsst/points`** smoke. Note: the API POST is
  **daily-served today** (policy wiring is P4-S3); the cube POST feasibility is in **C** (prototype).

## Pass / fail (§3.2 absolute budgets)
- bbox (recent/delta) **warm p95 < 200 ms**;
- POST /points (recent/delta) **warm p95 < 100 ms** and **`C8` p95 < ~1 s**;
- single-day point **< ~50 ms** (or ≤ daily p95 + 25 %);
- **parity_ok** for A(sampled)/B/C vs daily; **policy rejects the older day**;
- RSS bounded (no OOM), route header `cube` for point/range.
`pass_fail` splits **`PERF_overall_per_day_delta`** (budgets + parity), **`POLICY_rejects_older`**
(spatial-policy decision logic), and **`OVERALL_per_day_delta`** (perf **and** policy).
`OVERALL_per_day_delta == true` means the per-day delta gate passed — **not** that daily can be pruned.
Demoting daily still requires (a) the full 31-day retention validated (shadow/staging), and (b) explicit
approval + the P4-S1 sign-off.

## Hand-back
Return the artifact JSON (+ console summary). Claude/orchestrator fold it into the P4-S1 decision. If any
budget/parity fails, capture which day/path and the numbers; the outcome is to constrain the failing
path (per §4.1), never to re-chunk the base.
