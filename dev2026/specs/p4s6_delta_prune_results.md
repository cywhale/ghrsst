# P4-S6 — safe delta pruning (rebuild-then-swap): results

Status: **DONE (staging/shadow only).** **NO production mutation was performed.** Implements the
rebuild-then-swap delta-prune primitive from the P4-S4 baseline (§2, §5) as a reusable helper + tests. The
helper builds a fresh delta and returns an **atomic-swap PLAN** — it never swaps, never touches the live
delta/daily, and never edits delta metadata in place.

- Helper: [`../ingest/prune_delta.py`](../ingest/prune_delta.py) — `prune_delta(delta_path, out_path,
  keep_days, source_daily=None, source_delta=None, base_days=None, spatial_window_days=31,
  audit_recent_window=None, ...)`.
- Tests: [`../tests/test_phase2_p4s6.py`](../tests/test_phase2_p4s6.py) — **14/14 green**.
- Also folded the optional P4-S5 cleanup: `test_phase2_p4s5.py` now routes `main()` calls through a
  stdout-silencing wrapper (`_quiet_main`) for clean CI output; still **14/14 green**.

## The mechanic (why it is safe)

Delta `attrs['days']` is **physical append order**; `day_index[date]` maps a date to its physical time slab
(a backfill lands at the tail). So a delta day **cannot** be pruned by editing `attrs['days']`/`var_valid` or
truncating the time axis — that desyncs `day_index` from the slabs. `prune_delta` instead:

1. **Builds a fresh delta** at a caller-supplied **staging `out_path`** (must differ from `delta_path`; must
   not already exist — refuses to clobber or to build over the live delta).
2. **Writes kept days in CHRONOLOGICAL order** (`sorted(keep_days)`), so the new delta's `days` is naturally
   chronological, `day_index` maps 1:1 to contiguous slabs, and append-order disorder is repaired as a side
   effect.
3. **Sources each day** from **daily staging when available** (via the tiled, production-scale
   `append_to_delta` — the same writer `compact()` uses, so this is compaction-compatible) **else from the
   old delta read BY `day_index[day]`** (never by position in the keep list). `finalize days LAST` /
   `var_valid` parallel / absent-var→NaN+False invariants are preserved in both paths.
4. **Validates** the rebuilt delta and returns a **swap plan** (`from`/`to`/`backup`, `performed: false`) with
   a note that a later phase performs the atomic swap and cube refresh.

**Fail-closed gates (any failure → `status:"refused"`, nothing built, no swap plan emitted):**
- `keep_days` non-empty and ⊆ the source delta's days;
- **recent-window contiguity is ALWAYS decided by LOCAL recomputation** on the original delta (reproduces
  P4-S5 §4.1). A hole → refuse. The optional `audit_recent_window` (the P4-S5 `recent_spatial_window` dict)
  can only **corroborate**: if it disagrees with the local recompute → refuse (mismatch is suspicious). **It
  can never force-pass a locally-detected gap** — there is no bare-bool override (reviewer P4-S6 fix #1);
- **keep_days MUST preserve the ENTIRE active recent spatial window.** After the local window check passes,
  the required window `rw.window_start..rw.window_end` is computed and **every** required day must be in
  `keep_sorted`; if any is missing → **refuse, build nothing, no swap plan**, and return
  `missing_from_keep_window`. Dropping a day *inside* the active window would break bbox / POST /points for
  that day after the P4-S8 swap even if base covers it (base is bbox-hostile) — reviewer P4-S6 re-review fix.
  The rebuilt staging delta's own recent window is **also** re-checked post-build (`validation.recent_window_ok`);
- **base coverage is MANDATORY whenever anything is dropped.** If `dropped_days` is non-empty and
  `base_days` is None → **refuse and build nothing**. `base_days=None` is allowed **only** for a pure
  rebuild/reorder (`dropped_days` empty, e.g. repairing append-order to chronological). When given, every
  dropped day must be covered by base, else refuse (reviewer P4-S6 fix #2).

## What P4-S6 proves (tests — 17/17)

| test | proves |
|---|---|
| `test_append_order_rebuild_from_delta` | append-order delta (recent then older backfill); prune from the **old delta read by `day_index`** rebuilds a **chronological, unique** delta; **every kept day's slab matches the original by DATE** (physical mapping preserved); `latest == max == last`. |
| `test_rebuild_from_daily_source` | same rebuild sourcing from **daily** (tiled `append_to_delta`); parity + all-ok. |
| `test_gap_refuses` | a recent-window **hole prevents the prune** (`refused`, nothing built). |
| `test_audit_claiming_ok_cannot_bypass_local_gap` | an `audit_recent_window` **falsely claiming contiguous cannot force-pass** a locally-detected gap → refused, nothing built, **no swap plan** (fix #1). |
| `test_matching_audit_proceeds` | a matching audit dict corroborates and proceeds. |
| `test_dropped_with_base_none_refuses` | **dropping days with `base_days=None` refuses and builds nothing** (fix #2). |
| `test_pure_rebuild_base_none_ok` | **no dropped days + `base_days=None` succeeds** as a pure rebuild/reorder (append-order → chronological, values match by date). |
| `test_drop_day_inside_window_refuses` | dropping a day **inside** the active recent window (base covers it) → **refused**, no out_path, no swap plan, `missing_from_keep_window` set (re-review fix). |
| `test_drop_latest_window_day_refuses` | dropping the **latest** window day → refused. |
| `test_drop_only_older_than_window_ok` | dropping only days **older** than the active window succeeds; post-build `validation.recent_window_ok` holds. |
| `test_uncovered_drop_refuses` / `test_covered_drop_ok` | **base-coverage gate** — a dropped day not in base refuses; all-covered proceeds. |
| `test_absent_var_preserved` | **var_valid preserved incl. absent-var** — an absent var stays `valid False` with an all-NaN slab; present vars stay `True`. |
| `test_var_first_appears_in_fallback_day` | **mixed daily+delta sourcing**; a var first appearing in a **fallback delta day** is created with a NaN backfill + `var_valid False` for the earlier days; parity + all-ok. |
| `test_sources_unchanged` | source **delta + daily byte-identical** after prune (mtime/size snapshot); **no swap performed**; `production_mutation: false`; `swap_plan.to` is the live delta but untouched. |
| `test_refuses_out_equals_delta` / `test_refuses_existing_out_path` | refuses to build over the live delta or clobber an existing staging path. |

Validation asserts: `day_set == sorted(keep_days)`, unique, sorted, `latest == max(days)`, `var_valid_ok`
(incl. absent-var), and **sample parity** per kept day (seeded, float32, NaN-aware) vs the source.

## Compatibility with P4-S5 / P4-S4
- **Consumes P4-S5 audit output:** `keep_days` = the audit's calendar keep-set; `audit_recent_window` takes
  the audit's `recent_spatial_window` dict and is **cross-checked against** the local recompute (corroborate
  only, never override — fix #1); `base_days` gates dropped days exactly as the audit's base-coverage filter
  (and is mandatory when dropping — fix #2). The helper **reproduces** the §4.1 recent-window check
  standalone, so it is safe even if called without the audit.
- **P4-S4 safety model:** rebuild-then-swap only; no in-place metadata edit; `days[-1]` never used as latest;
  fold/keep decisions are by date via `day_index`; swap deferred.

## What remains (P4-S7 / P4-S8 / P4-S9)
- **P4-S7** — daily-staging prune + validation manifest (move-to-hold, not `rm`), emitting the P4-S5
  `manifest_preview` schema for real; dry-run first.
- **P4-S8** — the **atomic swap** execution (`from`→`to`, keep `<to>.pre-prune` until `/healthz` confirms,
  trigger cube `refresh()`), the ordering `compact-into-base → prune delta`, and the compaction strategy
  (default O4 defer-with-alarm; **O2 blocked behind the §6.1 base-segmentation gate**). `prune_delta` already
  returns the swap plan this phase will execute.
- **P4-S9/S10** — VM24 shadow/dry-run gate + production rollout (Codex/ops).

**No production mutation, no cron changes, no live delete, no in-place metadata edits were performed in
P4-S6.** All work is against synthetic staging stores in temp dirs; the source delta/daily are proven
unchanged and no swap is executed.
