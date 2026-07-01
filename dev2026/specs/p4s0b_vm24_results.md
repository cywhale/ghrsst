# P4-S0b — VM24 read-only binding gate: RESULTS (Codex/ops execution)

Codex ran `bench/p4s0b_readonly_gate.py` **read-only** on VM24 (2026-07-01T02:32:34Z). Artifacts on VM24:
`/home/odbadmin/Data/ghrsst/logs/p4s0b_readonly_20260701T023234Z.{json,log}`. No production mutation.

**Verdict: partial pass — `OVERALL_per_day_delta = false`, solely because delta POST /points `C8` p95
exceeds the ~1 s budget.** Everything else (point/range, single-day point, delta bbox, parity, policy
rejection) passed.

## Production state at run time
- base cube: **2023-01-01 .. 2026-06-26**; delta **on disk 2026-06-27 .. 2026-06-29** (3 days).
- **⚠ stale API metadata:** the running API `/healthz` reported `delta_latest = 2026-06-28` while disk
  delta had `2026-06-29` (see finding 2).

## Metrics (real prod, read-only)
| path | metric | budget | verdict |
|---|---|---|---|
| full-history point/range (cube, 365-day) | p95 **26 ms**, sampled parity **true** | route/perf | ✅ |
| single-day point | cube p95 **4.5 ms** vs daily 14.7 ms, parity **true** | < 50 ms | ✅ |
| delta bbox 250k | warm p95 **23.8–25.4 ms**, C8 **173.9–182.9 ms**, parity **true** | warm < 200 ms | ✅ |
| delta POST /points (1000) | warm p95 **85.8–90.0 ms**, parity **true** | warm < 100 ms | ✅ warm |
| delta POST /points (1000) | **C8 p95 1239–1335 ms** | C8 < ~1 s | ❌ **over** |
| policy dry-run | older/base day rejected **true** | — | ✅ |

**So on real prod data the cube comfortably serves point/range, single-day point, and recent-day bbox,
and delta POST warm is fine; only delta POST under C=8 concurrency (~1.2–1.3 s) misses the ~1 s bar.**

## Finding 1 — harness fan-out bug (FIXED in this PR)
The original gate sampled 1000 **globally random** POST points; on the global prod grid that spans >64
daily chunks and hit `StoreAccess.points_batch` fan-out limit (`batch spans 503 chunks > fan-out limit
64`) during the daily PARITY read — so the harness could fail by testing REJECTION, not parity/perf.
Codex patched the VM24 worktree to sample points inside the central bbox window; **that fix is folded in
here** (`_window_points`, both harnesses): POST points are now sampled **within the bbox window**, which
bounds the daily fan-out to the few chunks the window spans (the realistic spatial-query case). The
scattered worst-case remains the documented base-tier blocker in `p4s0_daily_vs_cube.md`.

## Finding 2 — stale cube metadata after delta append (production; → P4-S3)
The running API opens `TieredCube`/`TimeCubeStore` ONCE at lifespan and caches `delta.days`/`latest`, so
**new delta days are invisible until the process reloads**:
- disk delta has `2026-06-29`, but `/healthz` `delta_latest = 2026-06-28`;
- a range ending `2026-06-28` routes **cube**; ending `2026-06-29` routes **daily** (fallback).
This is fine today (daily still authoritative) but is a **blocker for time-cube-authoritative mode** —
if daily is demoted, an un-reloaded API would 4xx-or-mis-serve the newest day. **P4-S3 must include a
reload strategy** (§6.3): short-term ops = restart the `ghrsst` PM2 process after a successful delta
append; better = a code-path **metadata refresh / TTL-based reopen** of `TieredCube`/`TimeCubeStore` so
new delta days become visible without a restart.

## Decision needed — delta POST C8 budget (product/engineering)
Delta POST C8 p95 ≈ **1.24–1.34 s** vs the **~1 s** bar. Options (P4-S1 sign-off):
- **(revise, likely acceptable)** relax the POST **C8 budget to ~1.5 s** — POST /points at C=8 is a
  heavier, less common call than bbox/point; 1.3 s under 8-way concurrency is operationally reasonable
  and warm p95 (~90 ms) is well within budget; **or**
- **(optimize)** reduce delta POST C8 (e.g. cap batch fan-out / group tighter / bound concurrency for
  POST) before committing.
Recommendation: **revise C8 to ~1.5 s** unless product wants sub-second POST at C=8. Not decided here —
flagged for P4-S1.

## Gate status
`OVERALL_per_day_delta = false` (POST C8). This is the **per-day delta** gate only; it still does **not**
authorize pruning. Full **31-day retention** remains unvalidated (prod delta only ~3 days) → needs the
shadow harness or an explicitly-approved staging backfill. Next: fold findings → **P4-S1** (policy +
C8-budget decision) and **P4-S3** (reload strategy). Still no production mutation.
