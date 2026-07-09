# P4-S9 — VM24 shadow rehearsal: RESULTS (Codex/ops execution)

**Verdict: PASS** — `RUN_ID = 20260709T065917Z`, code at `5ef079c`
(`dev2026-p4-s8-swap-design`). Full pipeline rehearsed on shadow copies: audit → bulk `prune_delta` →
S8a swap executor → shadow-API probes → staging prune (dry-run + real, subset only) → manifest review.
**Production untouched: the final `/healthz` stable-field diff (`delta_latest`, `delta_day_count`,
`spatial_window`, `cube_latest_in_sync`) was EMPTY.**

## Probe results (corrected rerun — see the runbook bug below)
| probe | result |
|---|---|
| kept bbox `2026-06-04` (`start=end=`) | **200** ✅ |
| dropped bbox `2026-05-30` | **400** + `available_spatial_window` ✅ |
| dropped point GET `2026-05-30` | **200** (served by base — no point/range history lost) ✅ |
| point range `2026-05-30..2026-06-04` | **200** ✅ |

## Artifacts (VM24)
`/home/odbadmin/Data/ghrsst/logs/p4s9_20260709T065917Z/`:
`prune_plan.json` · `swap_result.json` · `shadow_api_probes_rerun.json` · `staging_dryrun.json` ·
`staging_realrun.json` · `healthz_stable_diff.json` (empty diff).

## Runbook bug found during execution (FIXED in the runbook §5a)
The first bbox probe used `date=<day>` — the GET endpoint has **no `date` parameter**, so it was
silently ignored and the request served the **date-less LATEST day → false 200** (probe passed without
testing the window gate). bbox/point GET day selection is **`start=<day>&end=<day>`**; `date` is a
**POST /points body field only**. The runbook now carries concrete `curl` commands and a warning.

## The two field incidents the rehearsal surfaced earlier (both fixed + tested before this PASS)
1. **OOM in full-slab validation** (first attempt): `prune_delta` validation materialized ~2.6 GB
   slabs per var-day → SIGKILL, no artifacts. → point-wise validation + `engine="bulk"` +
   fsync'd progress journal / error JSON / plan-written-only-on-success (`77af7c0`).
2. **1-D `time` coord array** (second attempt): var union collected `time` as a data var →
   `IndexError`. → name+shape data-var filter across build/presence/validation + the same fix in
   `prune_staging`'s extra-var gate (`5ef079c`). The progress/error artifacts from fix #1 made this
   diagnosis immediate — the hardening loop worked as designed.

## What this PASS does and does not authorize
- ✅ The P4 retention/prune tooling (S5 audit, S6 bulk prune, S8a executor, S8b alarm, S7 staging
  prune) is **rehearsal-proven at production scale on VM24 shadow data**, including the fail-closed
  gates, artifacts, and the untouched-production invariant.
- ❌ It does **NOT** authorize production mutation. **P4-S10 (production rollout) remains a separate
  approval** (orchestrator + Codex), with: request-quiescence swap posture (PM2 restart-under-lock),
  compact-before-prune ordering re-checked by a fresh audit at execution time, hold-only deletion
  (`hold_until` + ops-only hard delete), and the O4 alarm wired into ops cron.
- ⏳ Reminder: the compact-free prune window (June rebuild artifact) continues to erode daily — an S10
  decision should re-run the audit rather than reuse this rehearsal's day sets.
