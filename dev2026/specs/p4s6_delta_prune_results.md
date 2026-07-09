# P4-S6 — safe delta pruning (rebuild-then-swap): results

> ## S9 field-failure hardening (2026-07-09, after the VM24 step-3 incident)
> **Incident:** on VM24 the step-3 wrapper vanished silently — `delta_new.zarr` complete (37 GB, 34
> days, correct metadata) but no `prune_plan.json`, no traceback, no artifact. **Root cause:** the OLD
> `_validate` materialized FULL GLOBAL slabs — `read_daily_day`/`_read_delta_day` (~8 GB per day, 3 vars
> × 17999×36000 float32) plus `ng[v][t,:,:]` (~2.6 GB per var-day) — just to sample 32 points. At
> production scale the process was **OOM-SIGKILLed** after the build succeeded; SIGKILL cannot be
> caught, hence no error artifact. **Fixes (all tested):**
> 1. **Point-wise validation** (root cause): samples are per-cell scalar reads on BOTH sides — no full
>    slab is ever materialized, for either engine.
> 2. **`engine="bulk"` (new default):** pre-creates the final arrays at shape `(len(keep_days), ny, nx)`
>    and writes disjoint `(day, var, spatial-tile)` units bounded-parallel. Workers NEVER resize arrays
>    or write attrs; `days`/`vars`/`var_valid` finalize LAST; absent vars need zero writes (NaN
>    fill_value). Append-only fsync'd checkpoint (`_bulk_prune_ck.jsonl`) → `resume=True` skips
>    completed units. All fail-closed gates unchanged; old-delta reads stay BY `day_index`; the plan
>    schema is unchanged (plus an `engine` field).
> 3. **Progress/error artifacts** (`artifacts_dir=`): `prune_delta_progress.jsonl` — every event
>    (gates, `build_start`, per-`(day,var)` `var_done` with day/source/target_index/elapsed/status,
>    `build_finalized`, `validation_start`/`validation_day`/`validation_end`, `plan_written`/`refused`/
>    `error`) is written+flushed+**fsync'd**, so even a SIGKILL leaves the exact frontier;
>    `prune_delta_error.json` (traceback + context) on any catchable exception; **`prune_plan.json` is
>    written by the tool itself ONLY on success**.
> 4. `engine="perday"` retained for small-scale/tests (its delta-source fallback is full-slab —
>    documented as not for production scale).
>
> **Benchmark (synthetic):** 2048², 16 days, 3 vars, workers=4 — bulk == perday on speed (7.6 s vs
> 7.6 s daily-source; 4.5 s vs 4.7 s delta-source). **8192² delta-source (isolated processes): bulk
> peak RSS 127 MB vs perday 1691 MB** at identical speed — the tile-bounded memory profile is the
> production-scale win (the reviewer's acceptance bar: "at least same speed, with
> progress/resume/failure artifacts" — met, plus a 13× memory reduction).
>
> **Tests:** suite now **22/22** — all 17 originals pass unchanged under the bulk default (semantic
> equivalence), plus: journal/plan artifacts + schema, refusal-writes-no-plan, **mid-build failure →
> error artifact + exact frontier → `resume=True` completes and validates green (values by date)**,
> perday engine still works, finalize-strictly-after-last-var_done ordering. S9 runbook step 3 updated
> to the bulk/artifacts path (success == `prune_plan.json` exists; step 4 auto-skips without it).
>
> ### Incident #2 (2026-07-09, VM24 step-3 retry): 1-D `time` coord array collected as a data var
> The REAL MUR daily groups carry a **1-D `time` array**; the bulk var-union excluded only `lon`/`lat` by
> name, so `time` joined the union and was read `[0, y, x]` → `IndexError` mid-build. (The new
> progress/error artifacts worked exactly as designed: the journal showed `vars=[..., "time"]` and the
> error JSON carried the traceback — this is what made the diagnosis immediate.) **Fix — DATA-VAR
> FILTER, by name AND shape:** `_daily_data_var` / `_delta_data_var` admit only 3-D `(t, y, x)` fields
> whose spatial extent covers the target region; `lon`/`lat`/`time` excluded by name; absent vars return
> False (membership-checked). Applied to the var union, `_src_present`, and validation. **The same
> latent bug in `prune_staging`'s extra-var gate is fixed too** (a `time` array would have skipped EVERY
> day in S9 step 6). Tests → **25/25** (S6) + 16/16 (S7): daily fixture with 1-D `time` → bulk + perday
> green, `time` absent from output attrs/arrays/journal; **2-D helper array also excluded (shape-based,
> not name-only)**; `time` no longer trips the staging extra-var gate. Runbook: a staging delta from a
> failed run under OLD code is **poisoned — never resume across a code fix**; fresh `RUN_ID` or delete
> only `delta_new.zarr`.

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
