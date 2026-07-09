# P4-S7 — daily-staging prune + validation manifest: results

Status: **DONE (staging/shadow only). NO production mutation, NO hard delete anywhere.** Implements the
P4-S4 §4/§4.1 daily-staging prune as **move-to-hold** with the S8-design §7 hold/manifest conventions
(same `hold_dir` + append-only `manifest.jsonl` the S8a swap executor writes).

- Implementation: [`../ingest/prune_staging.py`](../ingest/prune_staging.py) —
  `prune_daily_staging(daily_path, hold_dir, delta_path, base_path, spatial_window_days=31,
  staging_buffer_days=7, mode='conservative', dry_run=True, allow_redownload_only=False,
  hold_days=14, ...)`.
- Tests: [`../tests/test_phase2_p4s7.py`](../tests/test_phase2_p4s7.py) — **15/15 green**.

> **Codex S7-review round-1 patches folded:** (1) **crash-safe manifest** — the real-run manifest is
> opened before the loop and each moved day's line (incl. `source_path`, `hold_path`, `status:"moved"`) is
> written + flushed + **fsync'd immediately after its rename**, so a mid-run crash can never leave a moved
> day unrecorded; a later day's move failure is a per-day skip, earlier records stand; dry-run still
> batch-writes to its own file. (2) **extra-var protection** — a daily day carrying a data var the
> covering tier lacks fails validation and is skipped (pruning would lose daily-only data). (3) **hold
> destination precheck** — an already-existing `dst` skips that day (never rename-over). (4) **honest
> result fields** — `daily_staging_mutation: not dry_run` (a real run DOES mutate staging),
> `hard_delete: false` (always), `production_mutation: false` meaning strictly "no VM24/live production
> path touched by this phase".

## Behavior (fail-closed at every layer)

1. **GLOBAL gate:** the delta's recent spatial window must be contiguous (local recompute via the same
   `recent_window_contiguous` prune_delta uses) — a hole → `status="refused"`, zero candidates, nothing
   moved.
2. **Keep-set (calendar range, never an entry slice):** keep the latest
   `spatial_window_days + staging_buffer_days` calendar days (anchored on max(daily latest, delta
   latest)); **conservative (default)** additionally keeps **every day not covered by base** — an
   un-compacted day is never a candidate. `accepted_risk` keeps only the recent window.
3. **Per-day validation (skip, don't prune, on failure — the run continues):**
   - **coverage tier: delta preferred, else base.** (Design note: the P4-S4 §4 gates assumed steady-state
     candidates still in delta; the *first* full-history prune's candidates are in base only — so parity
     is checked against whichever tier actually serves the day.)
   - `var_valid` agreement (absent-var days prune fine when the tier records the absence);
   - float32 NaN-aware **sample parity** daily-vs-tier, read **by `day_index`** (append-order safe) and
     point-wise (cheap even on the `t90/s8` base);
   - **no cube coverage at all** → skipped, unless `mode='accepted_risk'` **AND** the explicit
     `allow_redownload_only=True` (the P4-S4 "NetCDF redownload explicitly accepted" acceptance) — then
     pruned with `redownload_required: true` and `validated_against: null` in the manifest.
4. **Dry-run is the DEFAULT.** It computes + validates everything and writes the would-be records to
   **`manifest.dryrun.jsonl`** (a separate file — the real audit manifest is never polluted), moving
   nothing.
5. **Real run = MOVE-TO-HOLD, never delete:** each eligible day group is `os.rename`d (same-filesystem
   precheck via the factored `_st_dev`, cross-device → refused before touching anything) to
   `hold_dir/<daily_basename>.day-<day>.pre-prune-<run_id>`, still a readable Zarr group. One manifest
   line per day (§7 schema: run_id, ts, mode, operator, base_covers/delta_present/in_recent_window/
   redownload_required, validation summary, `hold_until = now + 14d`, `hard_delete: ops-only`). Runs
   under the shared `flock` ingest-lock convention.

## What the tests prove (15/15)
- dry-run default: nothing moved/touched (mtime/size), dry-run manifest separate, tier attribution
  correct (recent candidates → delta, older → base);
- real run: 8 candidates moved to hold and still readable, kept days intact, real manifest 1 line/day;
- recent-window hole → refuses ALL, nothing moved;
- conservative keeps old-but-uncompacted days (not even candidates);
- tampered day (parity fail) → that day skipped with reason, others still pruned;
- absent-var day prunes (var_valid agreement);
- accepted_risk + uncovered day: skipped without the flag; pruned + `redownload_required` with the
  explicit flag; conservative never touches it;
- cross-device hold_dir refused pre-move; untouched days byte-identical; **every candidate ends up
  either still in daily or readable under hold — nothing ever vanishes**;
- **crash simulation** (move fails after the first day): moved day is in hold AND already
  manifest-recorded; later days skipped with the move error and still in daily; nothing vanished;
- **extra daily var** (tier lacks it) → day skipped, data preserved, others still pruned;
- **pre-existing hold destination** → that day skipped (pinned `_run_id`), others proceed.

## Boundary (unchanged)
No VM24 / production / cron mutation; no live swap; hard delete is ops-only after `hold_until` and is
NOT automated here. Production execution of this prune is P4-S9/S10 (Codex/ops) — the S9 shadow
rehearsal should run **dry-run first, then a real run on a shadow copy**, exactly as these tests do.

## What remains
- **P4-S9 (Codex/ops):** VM24 shadow rehearsal — audit (`--alarm`) → `prune_delta` plan → swap rehearsal
  (S8a executor, PM2-restart-under-lock posture) → staging prune dry-run → manifest review.
- **P4-S10:** production rollout, separate approval.
- Compaction (O2) still blocked behind the §6.1 base-segmentation gate; O4 alarm ships (S8b).
