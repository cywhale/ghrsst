# P4-S5 — read-only retention / prune-eligibility audit tool: results

Status: **DONE (read-only tooling; no mutation).** Implements the audit half of the P4-S4 baseline
([`p4_ingest_prune_retention_design.md`](p4_ingest_prune_retention_design.md)). Ships a strictly read-only
CLI + tests; **no pruning, compaction, cron, delete, or move** — those are P4-S6/S7/S8.

- Tool: [`../ops/p4_retention_audit.py`](../ops/p4_retention_audit.py) (new `dev2026/ops/` dir).
- Tests: [`../tests/test_phase2_p4s5.py`](../tests/test_phase2_p4s5.py) — **12/12 green**.
- Runs local/shadow now; **safe for Codex/ops to run read-only on VM24** later (opens every store `mode='r'`,
  issues only `GET /healthz`, never writes a store).

## What the tool does

One command emits a **deterministic JSON report** describing what a *future* prune/compaction would be
*allowed* to do — as **dry-run candidates only**. It never acts.

```
dev2026/.venv/bin/python dev2026/ops/p4_retention_audit.py \
  --daily <mur.zarr> --base <base.zarr> --delta <delta.zarr> \
  [--spatial-window-days 31] [--staging-buffer-days 7] [--delta-buffer-days 3] \
  [--mode conservative|accepted_risk] [--measure-sizes] \
  [--healthz-url http://127.0.0.1:8035] [--json-out out.json] [--strict]
```

### Report sections (maps 1:1 to the P4-S4 audit requirements)
1. **stores** — daily/base/delta chronological spans: `count`, `earliest`, `latest` (chronological max),
   `is_unique`, `is_sorted_chronological`, `calendar_span_days`, `gap_count`, `gaps`. For the delta it also
   reports `latest_physical` vs `latest_chronological` and `append_order_differs` — surfacing the append-order
   invariant explicitly.
2. **recent_spatial_window** — `window_start/window_end` from **max(delta.days)**, the required 31 *calendar*
   days, `recent_window_contiguous`, `missing_in_window`; if a hole exists → `prune_eligible=false`,
   `action=repair_first`. Cross-checks `expected_spatial_window` via the same `spatial_policy` helper the API
   uses.

> **Global prune precondition (P4-S4 §4.1) — the tool REFUSES to emit any prune candidate when the recent
> spatial window has holes.** If `recent_spatial_window.recent_window_contiguous == false` (or the window
> can't be confirmed at all, e.g. no delta), then `staging_keep.daily_prune_candidates`,
> `delta_prune.delta_prune_candidates`, and `manifest_preview` are **all forced to `[]`/`0`** — *even for
> older delta days that base already covers* — and each blocked section carries
> `blocked_by_recent_window_gap: true`, `blocked_by_recent_window: true`, `action: "repair_first"`, and a
> reason. Diagnostic fields (keep windows, `drop_candidates_by_calendar`, `missing_in_window`) stay populated
> so a repairer can see exactly what to backfill first. `--strict` exits 2; non-strict exits 0 with the
> failure + warning recorded.
3. **staging_keep** — daily-staging keep-set + `daily_prune_candidates` (dry-run) under the chosen mode
   (conservative / accepted_risk). Clearly `dry_run: true`.
4. **delta_prune** — calendar `keep_days` = latest `(spatial_window_days + delta_buffer_days)` calendar days;
   `drop_candidates_by_calendar`; then the **base-coverage filter** → `delta_prune_candidates` (eligible only
   if base covers the day) vs `blocked_need_compaction_first`. Under deferred compaction candidates are `[]`
   with a `reason`.
5. **compaction_status** — `base_latest`, `delta_earliest/latest`, `delta_calendar_span_days`,
   `delta_days_older_than_window(_not_in_base)`, `o4_defer_warranted`, and an explicit note that **O2
   block-level is NOT implementable without the §6.1 base-segmentation gate**.
6. **disk** — free/used per filesystem holding the stores (read-only `shutil.disk_usage`). `--measure-sizes`
   opt-in does a read-only footprint walk to hint **O1/O3** full-rebuild disk feasibility (never claims O2).
7. **api_policy** — optional `--healthz-url` read-only `GET /healthz`; compares `delta_latest`,
   `delta_day_count`, `spatial_window`, `cube_latest_in_sync`, `spatial_window_enforce`,
   `coveragejson_enabled` against the computed expectation; flags `api_delta_metadata_stale` (the P4-S0b
   stale-after-append finding).
8. **manifest_preview** — would-be prune-manifest records for the daily candidates, each with `base_covers`,
   `delta_present`, `in_recent_window`, `mode`, `redownload_required`, and `dry_run: true`. Timestamp/operator
   fields are `null` (ops stamps them at real run time) so output stays deterministic.

Plus `audit_failures` (drive `--strict` nonzero exit) and `warnings` (informational; exit stays 0).

### Chosen defaults (documented per the reviewer ask)
- **`--spatial-window-days 31`** — the adopted policy window.
- **`--staging-buffer-days 7` (conservative default).** A full week above the spatial window, so a late
  backfill / delayed compaction can still re-derive from staging before a day is ever a prune candidate. 7
  (not 3) is the *conservative* choice — it keeps more, deletes less; disk cost is trivial (7 daily days ≪ the
  ~2 TB base). Tunable down with ops' disk budget.
- **`--delta-buffer-days 3`** — keeps the delta window edge from flickering under refresh/compaction lag.
- **`--mode conservative`** — never proposes pruning a daily day not yet compacted into base.

### Exit codes
- Default: **exit 0** — always emit the report + warnings, even with gaps/issues.
- `--strict`: **exit 2** only on true audit *failures* (recent-window hole → repair_first; duplicate cube
  days; API delta metadata stale). Warnings never fail the run.

## Read-only guarantees (enforced by tests)
- Every Zarr store opened `mode='r'`; the **only** write is the optional `--json-out` report artifact.
- The tool does **not** import the `ingest` package and does **not** call `append_to_delta` / `compact` /
  `build_timecube*` / `rmtree` / `os.replace|rename|remove` / any array write. `test_source_has_no_mutating_calls`
  proves this by **AST**: no `ingest` import, those names absent from the module namespace, every
  `zarr.open_group` uses `mode='r'`, and no mutating fs-op calls.
- `test_no_store_mutation` snapshots every store file's `(mtime_ns, size)` before/after a full run (incl. the
  `--measure-sizes` walk) and asserts **zero change**.

## What P4-S5 proves (test coverage — 14/14)
1. **Sorted contiguous delta** → no append-order flag, recent window contiguous, `prune_eligible=true`.
2. **Append-order delta** (recent days appended, older days backfilled last) → `latest_physical` ≠
   `latest_chronological`, `append_order_differs=true`, and **all window logic anchors on the chronological
   max, never `days[-1]`**. This is the core P4 invariant, tested directly.
3. **Gappy recent window** → `missing_in_window` non-empty, `prune_eligible=false`, `action=repair_first`,
   `--strict` exits 2 (and exits 0 without `--strict`). **Plus:** a window hole **suppresses all staging
   candidates + manifest** (even in-base days) and **all delta candidates even when base covers them**, each
   marked `blocked_by_recent_window_gap`.
4. **Conservative mode** never proposes pruning a daily day not in base — even outside the recent window.
5. **Accepted-risk mode** reports older-than-base candidates but flags `redownload_required=true` in the
   manifest (and `false` for days base already covers).
6. **Delta prune requires base coverage** — un-compacted older delta days are `blocked_need_compaction_first`,
   not eligible; a fully-covered case yields eligible candidates with nothing blocked.
7. **Read-only** — no store mutation (mtime/size), AST-verified no mutating calls, `--json-out` is the only
   write.
- Plus: deterministic logic core across repeated runs; graceful handling of missing base/delta paths.

## What remains (hand-off to P4-S6/S7/S8)
- **P4-S6** — implement **safe delta pruning** (rebuild-then-swap, `prune_delta(delta, keep_days, source)`)
  on staging/shadow; this tool already computes the `keep_days` / eligibility it will consume.
- **P4-S7** — implement **daily-staging prune + validation manifest** (move-to-hold, not `rm`; dry-run first).
  The `manifest_preview` records here are the schema it will emit for real.
- **P4-S8** — implement **compaction strategy** (default O4 defer-with-alarm; O1/O3 gated on disk; **O2 blocked
  behind the §6.1 base-segmentation design gate**). This tool's `compaction_status` + `disk` sections feed
  that decision.
- **P4-S9/S10** — VM24 shadow/dry-run gate and production rollout (Codex/ops).

This tool is **audit-only**: it decides *nothing irreversible*. It is the read-only lens P4-S6+ will use to
choose candidates, and the safe way for Codex/ops to inspect VM24 retention state before any mutating step is
authorized.
