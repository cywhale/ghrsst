# P4-S4 — ingest / prune / retention design (SPEC-ONLY, no implementation)

Status: **DRAFT — reviewer round 1 patched** (Claude authored; Codex reviewed 1×). **Spec/design only — no
production mutation, no implementation until sign-off.** This spec answers the P4-S4 questions (target
architecture, daily-staging retention, delta pruning, base compaction, cron flow, validation gates,
post-prune API behaviour) and lays out the P4-S5..S10 step sequence with explicit **read-only vs mutating**
labels, gates, and rollback.

> **Reviewer round-1 patches folded (safety model):** (1) new **[MUT-LIVE-APPEND]** / **[MUT-LIVE-OVERWRITE]**
> tags — `append_to_delta` on the production cron mutates the *live* served delta, no longer mislabelled
> `[MUT-STG]` (§0.1, §7). (2) **Append vs overwrite**: normal cron **skips** an already-valid day; overwrite
> only in explicit repair/force mode via rebuild-then-swap or a maintenance lock, never silent live slab
> mutation; logs distinguish `skip/already_valid | append | repair_overwrite` (§7.1). (3) **O2 rolling/block
> compaction downgraded** to a candidate behind a design gate (segmented base vs one Zarr, segment reads,
> swap granularity, read benchmark); **O4 defer-with-alarm is the only safe current-disk strategy** (§6, §6.1).
> (4) **Daily-staging prune under deferred compaction** made explicit: default **conservative** (never prune a
> day not yet in base) vs opt-in **accepted-risk** (prune + rely on NetCDF redownload, SLA+manifest) (§4).
> (5) **Gap-aware retention**: all windows computed over sorted calendar days, require the latest 31
> contiguous, report gaps, and **stop-and-repair** if the recent window has a hole — never a bare
> `delta_day_count` (§4.1, §5).

It builds on and does not re-decide: [`p4_storage_policy_and_bbox_strategy.md`](p4_storage_policy_and_bbox_strategy.md)
(policy = Option A + D, `SPATIAL_WINDOW_DAYS = 31`, delta retention invariant, §6 ingest/compaction/prune
first-draft), [`p4s0_daily_vs_cube.md`](p4s0_daily_vs_cube.md) (bbox-from-base is a structural blocker →
spatial served only from the delta tier), [`p4s0b_vm24_results.md`](p4s0b_vm24_results.md) (per-day delta
gate green; full-31-day retention still unvalidated), and the VM24 v0.3.1 deployment state in
[`../README.md`](../README.md).

---

## 0. Standards / current-state alignment (brief)

**Adopted policy (unchanged, the invariants this spec must preserve):**
- **Time-cube authoritative** (base + delta); daily Zarr demoted to short-term staging.
- **Spatial availability == delta membership.** bbox + `POST /points` served only for days in the delta
  tier (`t1/s256`), currently the latest `SPATIAL_WINDOW_DAYS = 31` days; older → HTTP 400 naming
  `available_spatial_window`. point GET / range = full history from base+delta, never gated
  ([`store/spatial_policy.py`](../store/spatial_policy.py), [`api/app.py`](../api/app.py) `_spatial_window_gate`).
- **Delta retention invariant:** delta keeps **≥ 31 chronological days**; compaction may fold **only days
  older than the window** into base.
- **Daily source of truth *today*:** VM24 v0.3.1 still ingests into `mur.zarr` first; base+delta are a
  derived serving index. P4 flips this to cube-authoritative — **but only after the binding gates below.**

**Current VM24 state (2026-07-03, read from README + P4-S0b):**
- base `2023-01-01..2026-06-26` (`t90/s8/shard128`); delta `2026-05-30..2026-07-01` (33 days, `t1/s256`).
- `GHRSST_SPATIAL_WINDOW_ENFORCE=1`, `GHRSST_CUBE_REFRESH_TTL_SECONDS=300`, `GHRSST_ENABLE_COVERAGEJSON=0`.
- **No pruning of any kind is implemented yet** (delta already 33 > 31; daily is full-history). PM2
  restart-after-append fallback still in the cron.

**Relevant external standard / prior art:** the delta+base+compaction shape is the standard LSM-tree
pattern (append-optimized recent tier + read-optimized compacted tier + background compaction). The
retention/prune-manifest discipline mirrors WORM/audit-log practice (append-only manifest, verify-before-
delete, hold window before hard delete). Nothing here needs a new external dependency.

### 0.1 Operation-safety legend (used throughout)

Every operation in this spec is tagged:

| tag | meaning |
|---|---|
| **[RO]** | **read-only** — opens stores `mode='r'`, issues only GET/read queries, writes nothing. Safe to run against production once approved as read load. |
| **[MUT-STG]** | **mutating, staging/shadow only** — writes to a *copy* / shadow / temp path, never the live served store. Reversible by discarding the copy. |
| **[MUT-LIVE-APPEND]** | **mutating the LIVE served delta by appending a NEW day** — safe *only* because a new day is invisible to readers until `append_to_delta` finalizes `attrs["days"]` LAST (§2), so no reader ever sees a half-written day. This is the one mutation the production cron makes to the live store. It is **append-only**: it must NOT overwrite an already-visible day's slabs (that is not atomic — see [MUT-LIVE-OVERWRITE] / §7.1). A crash mid-append leaves the checkpoint and the day still invisible. |
| **[MUT-LIVE-OVERWRITE]** | **mutating an already-VISIBLE live delta day's array slabs in place** — **NOT atomic**: `append_to_delta`'s overwrite branch rewrites slabs (and flips `var_valid[t]`) while `day_index[D]` is already served, so a concurrent reader can see a torn mix of old/new slab data. **Forbidden in the normal cron path.** Allowed only in an explicit repair/force mode and only via rebuild-then-swap OR under a maintenance lock / serving-disable window (§7.1). |
| **[MUT-SWAP]** | **mutating via atomic swap** — builds a new store beside the old, then renames/repoints; the old store survives until the swap and is the rollback target. |
| **[MUT-DEL]** | **mutating, destructive delete** — hard-removes data. Gated hardest: requires manifest + parity + hold-window expiry + explicit approval. |

**Golden rule:** no **[MUT-DEL]** on any daily-full-history span until a dry-run has proven, per day, that
the day is *either re-derivable* (NetCDF redownload or staging/trash) *or no longer needed*. See §10.

---

## 1. Goal / non-goals / ops boundary

**Goal:** define the long-term ingest → validate → (delta) append → prune → compact lifecycle for
cube-authoritative mode, such that (a) storage is one growing store, not two ~2 TB stores; (b) every
served query stays correct and fast; (c) every prune/compaction is auditable, reversible within a hold
window, and never breaks the delta physical time-index mapping.

**Non-goals:** implementing anything (P4-S5+); touching VM24 production, cron, deploy, or NGINX; changing
the adopted policy numbers; re-chunking the base; finalizing the frontend contract (P3-S3).

**Ops boundary (hard, restated):** this session is **design + local/shadow only**. VM24 binding, cron
changes, and production rollout are executed by **Codex/ops** after sign-off. Local test harness path:
`export GHRSST_ZARR_PATH=$(pwd)/bak/data/mur.zarr` + `dev2026/.venv/bin/python -m unittest …`.

---

## 2. Append-order delta invariant (the correctness spine of this whole spec)

This governs every delta prune/compaction rule below; read it first.

- delta `attrs["days"]` is in **physical / append order**, NOT chronological. After an out-of-order
  backfill (append recent days, then backfill an older one) the older day sits at the *end* of the array
  and of `days`. ([`time_cube.py`](../store/time_cube.py) `_Meta.days` comment; [`dual_write.py`](../ingest/dual_write.py)
  `append_to_delta` appends `days + [day]`.)
- `day_index[date] = position in days = the physical time index of that day's slab in the (T,ny,nx) array`.
- **`latest` = chronological `max(days)`, NEVER `days[-1]`** (PR #24; `_Meta.latest_day`,
  `sync_missing`/`check_coverage` all use `max`/`min`). Do not regress this.
- **Therefore:** you may **never** prune a delta day by editing `attrs["days"]`/`var_valid` in place or by
  sorting/trimming metadata — that desyncs `day_index` from the physical slabs and silently corrupts every
  subsequent read. `zarr` `resize()` only truncates the *tail* of the time axis; the day you want to drop
  is generally *not* at the tail (append order + backfills).
- **The only safe delta prune/compaction primitive is REBUILD-THEN-SWAP:** build a fresh delta containing
  exactly the days to keep, written in **chronological** order (which also repairs append-order as a side
  effect), from a trusted source (daily staging, or the existing delta read *by `day_index`*), then
  **[MUT-SWAP]** the new store in. This is exactly what [`dual_write.py`](../ingest/dual_write.py) `compact()`
  already does for its `new_delta` (`later = [d for d in list_existing_days(daily) if d > end_day]`, rebuilt
  via `append_to_delta` in order). We reuse that shape; we do not invent in-place slab deletion.

> Acceptance hook: any prune/compaction step that identifies days **by array position** instead of **by
> date via `day_index`**, or that mutates `attrs["days"]` without rewriting the arrays, is a spec violation.

---

## 3. Q1 — Target architecture (long-term source of truth)

Three candidate lifecycles. All three keep base+delta as the *serving* index (that is decided); they
differ in **what the durable source of truth is** and **how much daily staging is retained**.

| | **A: NetCDF → daily staging → delta → base compaction** | **B: NetCDF → delta directly; daily staging N days only** | **C: NetCDF → delta; no daily staging, redownload to recover** |
|---|---|---|---|
| **Durable source of truth** | raw MUR NetCDF (external) + daily staging (rolling) + base+delta | raw MUR NetCDF + short daily staging (N≈few days) + base+delta | raw MUR NetCDF (external) only + base+delta |
| **Recovery path** | re-derive delta/base from **local daily staging** (fast) OR redownload NetCDF | staging for the last N days (fast); older → redownload NetCDF | **always** redownload NetCDF → re-append |
| **Disk usage** | highest: NetCDF-cache + 31-day daily staging + base + delta | medium: NetCDF-cache + N-day daily + base + delta | lowest: NetCDF-cache (transient) + base + delta |
| **Operational risk** | lowest (local re-derive for the whole spatial window without external dep) | medium (recovery beyond N days depends on the MUR archive being up) | highest (any base/delta corruption for a span with no local copy ⇒ full external redownload; blocked if MUR archive/URL changes or rate-limits) |
| **Complexity** | highest (staging prune + delta prune + compaction all live) | medium (staging prune trivial at small N; delta prune + compaction live) | lowest to run, but recovery is the most fragile / slowest |
| **Restore latency after a discovered cube defect** | minutes (local staging read) for ≤31 days | minutes for ≤N days; hours (redownload) beyond | hours+ (redownload the affected span), bounded by MUR archive availability |

**Recommendation → Option A with the daily-staging window == `SPATIAL_WINDOW_DAYS` (31), NetCDF redownload
as the deeper backstop.** Rationale:
- It keeps a **local, fast re-derivation source for exactly the spatially-served window** — so any day that
  can be served spatially (delta membership) is also locally re-derivable without depending on the external
  MUR archive being reachable at recovery time. That directly satisfies the §6.2 policy invariant "daily
  staging must not be shorter than `SPATIAL_WINDOW_DAYS` unless NetCDF redownload is explicitly accepted."
- The extra disk vs B is bounded and *shrinking relative to the problem*: 31 daily days is tiny next to the
  ~2 TB base. The sustainability win (dropping the *full-history* daily store) is already captured — A only
  keeps a **31-day rolling** daily staging, not the full history.
- C is held **in reserve**, selectable later by shortening the staging window to 0, *only if* the
  orchestrator explicitly accepts NetCDF-redownload-only recovery and confirms the MUR archive's redownload
  window/SLA (open question §12-Q1). C is a config point of A (staging N = 0), not a separate build.

**Decision recorded:** long-term source of truth = **raw MUR NetCDF (external, re-downloadable) + base+delta
serving index**, with a **rolling 31-day daily staging** as the fast local re-derivation buffer. Full-history
daily Zarr is retired (pruned to the rolling window) — but only after the §10 gates.

---

## 4. Q2 — Daily-staging retention

**Baseline: 31 days, aligned with `SPATIAL_WINDOW_DAYS` (policy §6.2).**

**Is 31 enough?** 31 is the *floor for the spatial window only*; the actual staging retention is driven by
**which days are safely in base**, computed from chronological sorted days (NOT a raw count — see §4.1). Three
rules we bake in:
1. Staging must cover **every spatially-served day** so any such day is locally re-derivable. Spatial
   availability == delta membership, so staging must keep **≥ the delta's chronological day set**, not a bare
   count. Define it over sorted days (§4.1), never `delta_day_count` (a count hides gaps).
2. Add a small **safety buffer** `STAGING_BUFFER_DAYS` (default 3–7) above the floor so a late-arriving
   backfill or a delayed compaction can still re-derive from staging.
3. **Compaction-deferred rule (reviewer P4-S4 fix — the decisive one).** Under O4 (compaction deferred, §6)
   the delta grows past 31 and those extra days are **not yet in base**. A day whose point/range history is
   only in the (transient, append-optimized) delta and **not** in base has **no durable local re-derive
   source** once staging drops it. We resolve this explicitly by choosing **one** policy:
   - **(default) CONSERVATIVE — do NOT prune a daily-staging day until it is compacted into base.** i.e. the
     staging keep-set is `{days not yet in base.days} ∪ {latest window+buffer}`. Under O4 this means staging
     grows in lockstep with the un-compacted delta tail — accepted, because it is a bounded set of *recent*
     days (tiny vs the ~2 TB base) and it guarantees every served day has a durable local copy (base OR
     staging) with zero external dependency. The same `delta_span` / `free_disk` alarm that gates O4 also
     bounds this growth: provision disk / run compaction before it matters.
   - **(opt-in) ACCEPTED-RISK — prune staging down to `window+buffer` right after delta validation,** relying
     on **NetCDF redownload** to recover any un-compacted day pruned from staging. Allowed ONLY if the
     orchestrator explicitly accepts it AND the MUR redownload SLA/window is confirmed (§12-Q1) AND every such
     prune is recorded in the manifest with `redownload.available=true`. This trades local durability for
     disk; it is NOT the default.

   The two are mutually exclusive and **must be set explicitly** (config flag, e.g.
   `GHRSST_STAGING_PRUNE_MODE=conservative|accepted_risk`, default `conservative`). P4-S5 audit tooling reports
   which days would be pruned under each mode so the choice is made on real numbers.

### 4.1 Gap-aware retention math (reviewer P4-S4 fix — never use a bare day count)

All retention/window computation is over **chronological sorted days**, and every prune is preceded by a
**gap check**. A raw `delta_day_count` (or `len(days)`) is forbidden as a window driver — it silently treats
a gappy 31-entry set as "31 contiguous days" when it may span 40 calendar days with holes.

Define (all **[RO]**), given `dd = sorted(set(delta.days))` and the daily calendar:
- **`window_days` = the latest `SPATIAL_WINDOW_DAYS` *calendar* days** ending at `max(dd)` — i.e. the date
  range `[max(dd) - 30d, max(dd)]`, computed on the calendar, not by slicing the list.
- **`recent_window_contiguous`** = every calendar day in `window_days` is present in `dd` (no holes in the
  most-recent 31). This is a **hard precondition for any prune**.
- **`missing_in_window`** = `window_days − dd` (the holes). **If non-empty, PRUNE STOPS** and a **repair**
  (backfill the missing day via `append_to_delta`, §5/§7) must run first. Pruning while the recent window has
  a hole could remove the only remaining copy of a day the window is *supposed* to serve.
- **`staging_keep_set`** (per §4 rule 3):
  - conservative: `{d ∈ daily : d ∉ base.days}  ∪  {latest window+buffer calendar days}`;
  - accepted-risk: `{latest window+buffer calendar days}` only.
- The audit tool (P4-S5) reports `dd`, `min/max(dd)`, `recent_window_contiguous`, `missing_in_window`, the
  full **gap list** across the delta span, and the `staging_keep_set` under both modes — **before** any prune
  is proposed. `check_coverage` (`structural_ok`, `missing_within_cube_span`) is the existing building block.

**Prune only after delta append + parity/sample validation succeeds — yes, hard requirement.** A daily
staging day D is **prune-eligible** only when ALL hold (all **[RO]** checks):
0. **`recent_window_contiguous` is True** (no gap in the latest 31; else stop + repair — §4.1).
1. **delta contains D** (`D in delta.days`) and D's `var_valid` is present/correct for every expected var.
2. **sample parity D passes:** N random (point + bbox-cell) samples read equal between the daily-staging D
   and the delta D (semantic float32 parity, not byte — see §8.4). Absent-var/omit/null semantics match.
3. **D is not in `staging_keep_set`** (§4.1) — under conservative mode this additionally requires **D ∈
   base.days** (compacted), so an un-compacted day is never pruned from staging.
4. **D is re-derivable if later needed:** conservative ⇒ D is in base (durable local) or in trash/hold;
   accepted-risk ⇒ NetCDF for D is confirmed redownloadable (recorded in the manifest) or D is in trash/hold.

**Prune manifest (append-only, required before any daily prune) — the pre-prune record for each day:**
```jsonc
{
  "day": "2026-05-15",
  "action": "prune_daily_staging",
  "pruned_at": "<UTC ISO, stamped by ops at run time>",   // scripts can't call Date.now(); ops stamps
  "operator": "<who/cron-id>",
  "delta_present": true,                 // D in delta.days at prune time
  "delta_location": "<delta path>",
  "base_covers": false,                  // whether base also covers D (informational)
  "var_valid": {"sst": true, "sst_anomaly": true, "sea_ice": true},
  "sample_parity": {"n": 200, "ok": true, "max_abs_diff_float32": 0.0},
  "digest": "<sha256 of a fixed sample vector from daily D>",   // proves what was removed
  "redownload": {"available": true, "source_url": "<MUR granule/url pattern>", "window": "<archive availability>"},
  "hold_until": "<UTC ISO: pruned_at + HOLD_DAYS>",   // moved to trash, hard-deleted only after this
  "rollback": "restore from trash/hold, or redownload+re-append"
}
```
The manifest is **append-only** and lives beside the daily store (e.g. `<daily>/_prune_manifest.jsonl`), so
"what was removed and was it safe" is always auditable. Prune is a **[MUT-DEL]** step; the *pre-checks* and
*manifest write* are **[RO]/[MUT-STG]** and run first. Hard delete is deferred to hold-window expiry (§10).

---

## 5. Q3 — Delta retention and pruning

**Invariant:** delta retains **≥ `SPATIAL_WINDOW_DAYS` (31) chronological days**; spatial availability ==
delta membership. Today VM24 delta = 33 days (no pruning yet).

**Pruning rule (precise) — calendar/gap-aware (reviewer P4-S4 fix):**
- **Gap precondition first (§4.1):** compute `dd = sorted(set(delta.days))`; require `recent_window_contiguous`
  (no hole in the latest `SPATIAL_WINDOW_DAYS` calendar days). If `missing_in_window` is non-empty, **STOP and
  repair** (backfill the missing day) before pruning — never prune with a hole in the served window.
- **Keep = all delta days within the latest `SPATIAL_WINDOW_DAYS + DELTA_BUFFER_DAYS` *calendar* days** ending
  at `max(dd)` (buffer default 2–3 so the window edge never flickers under refresh/compaction lag), i.e.
  `keep_days = [d ∈ dd : d ≥ max(dd) − (WINDOW+BUFFER−1) calendar days]`. This is **calendar-based, not a
  list slice** — `sorted(dd)[-N:]` would keep N *entries* and, in a gappy delta, silently retain days older
  than the intended window. Today delta is 33 contiguous days ⇒ keep all ⇒ prunes **0**; correct, no action.
- The days to **drop** = `set(dd) − set(keep_days)` (a date-set difference), never a position/append-order
  slice — `days[-N:]` is append order and would keep the wrong days after a backfill.
- A delta day is only pruned once it is **safely represented for point/range elsewhere** — i.e. it has been
  **compacted into base** (§6) AND base coverage of that day is verified. Never drop a delta day whose
  point/range history isn't confirmed in base, or you lose that day for point queries too. (Spatial loss
  beyond the window is *intended*; point/range loss is *not*.) So the ordering is always **compact-into-base
  first (verify base covers the day), then prune from delta.** Under O4 (compaction deferred) this means
  **delta prune does not run at all** — the delta simply grows, gated by the `delta_span` alarm.

**How to prune delta safely without breaking the physical time-index mapping (the core mechanic):**
- **Do NOT** delete slabs in place or edit `attrs["days"]`. Use **rebuild-then-swap** (§2):
  1. Determine `keep_days` via the calendar/gap-aware rule (§4.1, §5 above), **[RO]**: `dd = sorted(set(
     delta.days))`; **require `recent_window_contiguous == true`** (else STOP + repair first — never prune
     with a hole in the served window); `keep_days = [d for d in dd if d >= max(dd) − (SPATIAL_WINDOW_DAYS +
     DELTA_BUFFER_DAYS − 1) calendar days]`. This is a **calendar range**, not an entry-count slice — no
     `sorted(...)[-N:]` may drive pruning.
  2. Build `new_delta` **[MUT-STG]** by appending each day in `keep_days` **in chronological order** via
     `append_to_delta`, sourced from **daily staging** (preferred — it's the trusted re-derivation source and
     staging still holds these recent days) OR, if staging already pruned them, from the **existing delta read
     by `day_index`** (read slab at `day_index[d]`, never by naive position). Chronological rebuild also
     *repairs* any append-order disorder as a bonus.
  3. **Validate** `new_delta` (§8): `check_coverage`-style day set == `keep_days`, `var_valid` correct,
     sample parity vs the old delta for every kept day.
  4. **[MUT-SWAP]** atomically: keep old delta as `<delta>.pre-prune` until healthz confirms the new one,
     then move old to trash/hold. Repoint `GHRSST_DELTACUBE_PATH` (or swap the directory) and trigger a cube
     `refresh()` (TTL loop or explicit) so the running API rebuilds its `_Meta` snapshot.
- Because the new delta is a fresh chronological build, `days` is naturally chronological, `day_index` maps
  1:1 to contiguous physical slabs, and `latest = max(days) = days[-1]` *coincidentally* holds again — but
  code still uses `max`, so a future backfill stays correct.

**Reuse note:** this is `compact()`'s `new_delta` branch generalized (rebuild delta with a chosen day set +
atomic swap). P4-S6 should factor a `prune_delta(delta, keep_days, source) -> new_delta` helper that both
the standalone prune and `compact()` call, so there is one audited rebuild path.

---

## 6. Q4 — Base compaction

**What compaction does:** fold delta days that are **older than the spatial window** into the base
(`t90/s8/shard128`, point-series-optimal), then drop those days from delta. Compaction **lags the window by
≥31 days** — it may fold ONLY `day < window_start`; folding a recent day into base would make it bbox-hostile
and silently break spatial for that day.

**The disk blocker (unchanged reality):** the existing [`dual_write.py`](../ingest/dual_write.py) `compact()`
does a **full staged base rebuild** (`build_timecube_bulk(... end_day, overwrite=True)` into
`<base>.compact`), which needs **~2 TB+ free** beside the current base for the atomic swap. **That headroom
does not exist on VM24 today** → full-rebuild compaction is a **blocker, not a routine op**. Every compaction
option below must run a **disk precheck [RO]** first and refuse if free disk < the option's requirement + a
hard safety margin.

**Options (choose per measured free disk; recorded with the binding gate):**

| option | peak extra disk | when usable | mechanic | rollback |
|---|---|---|---|---|
| **O1 full rebuild** | ≈ full base (~2 TB) | only if free ≥ base + margin (**not today**) | current `compact()` — rebuild base through `end_day`, rebuild delta with days after, atomic swap | old base+delta kept until swap; revert = repoint back |
| **O2 rolling / block-level** | ≈ one 90-day time-block | **candidate — requires a design gate first (§6.1)**; NOT usable as-is | fold ONE completed 90-day block at a time; peak extra ≈ one block, repeat oldest-first — **but atomically replacing one block inside the current monolithic base Zarr is not straightforward** (see §6.1) | per-block staging; revert = discard the block staging before swap |
| **O3 external temp volume** | ≈ full base on a *separate* volume | only if a 2nd ~2 TB volume is provisioned (not today) | stage `new_base` on the temp volume, then move onto the primary | old base intact until move completes |
| **O4 defer-with-alarm** | 0 | when none of O1–O3 fit the free-disk margin | **do not compact**; let delta grow past 31; raise a `delta_span` / `free_disk` alarm until disk is provisioned | trivial (nothing mutated); the cost is a larger delta |

**Recommended path (reviewer P4-S4 fix):** **O4 defer-with-alarm is the ONLY safe strategy on the current
disk.** O1/O3 need ~2 TB free (base rebuild or a 2nd volume) — not available today. **O2 is downgraded from
"default target" to a "candidate requiring design"** because folding one 90-day block into the *current
monolithic base Zarr* is **not** a simple atomic op: a single Zarr array/group has no notion of an
independently-swappable time-block, so an in-place block fold would be exactly the kind of live in-place
mutation this spec forbids (a reader could see a torn base). O2 only becomes safe with a structural change
(segmented base or a block manifest/router). Until that design gate (§6.1) is passed and benchmarked, **do
not implement O2**; run O4 (let delta grow past 31 with a `delta_span` / `free_disk` alarm) and provision
disk for O1/O3 in parallel.

**Disk precheck [RO] (mandatory preamble to any compaction):**
- measure free disk on the base's filesystem; require `free ≥ required(option) + HARD_MARGIN` (HARD_MARGIN
  e.g. 200–300 GB, tuned with ops). Refuse + alarm otherwise. Never let compaction drive free disk below the
  margin (a stuck compaction that fills the disk would take the API down).
- report `delta_span`, `base_span`, `free_disk`, `required`, and the chosen option to the manifest/log.

**Compaction manifest + rollback:** like the prune manifest — record `compacted_through`, the day set folded,
digests before/after, the staging path, and keep `<base>.pre-compact` (or the old delta) until `/healthz`
confirms the new base+delta serve the folded span for point/range. Revert = repoint to the pre-compact
stores. Compaction is **[MUT-SWAP]** (never in-place base mutation while serving — the daily/staging store is
the re-derive source, so a rebuild is always a clean re-derive).

**Append-order safety in compaction (§2):** the days to fold are chosen **by date** (`day < window_start`) via
`day_index`, and `compact()` already rebuilds `new_delta` from `list_existing_days(daily)` filtered by date —
never by array position. Preserve that. The new base is bulk-built in chronological order from daily/staging,
so its time axis is well-formed (`check_coverage.structural_ok`).

### 6.1 O2 design gate (block-level compaction is blocked until this is answered)

O2 (rolling/block-level compaction) is the only option that fits the *current* disk envelope, but it cannot
be implemented against the present monolithic base Zarr without risking live in-place base mutation. Before
any P4-S8 O2 work, a **separate design + benchmark gate** must decide:

1. **Base layout: one Zarr or segmented?** Does the base stay a single Zarr array/group (in which case a
   "block" is not independently swappable and O2 is effectively unsafe), or does it become a **segmented base**
   — e.g. one Zarr per 90-day time-block plus a **block manifest / router** that lists `(block_id, day_range,
   path)` — so a block can be rebuilt and swapped atomically on its own?
2. **How do reads resolve segments?** Define how `TimeCubeStore` / `TieredCube` map a requested day/range to
   the right segment(s): manifest-driven day→segment lookup, cross-segment `point_series` stitching (like
   `TieredCube` already stitches base+delta), and how the immutable `_Meta` snapshot spans multiple segment
   handles without a torn read during a segment swap.
3. **Atomic swap granularity.** What is the unit of atomic replacement — one time-block segment? — and how is
   the manifest updated atomically (manifest write is the commit point; old segment kept until confirmed).
4. **Benchmark before adopting.** Measure (a) **read impact** of segmentation on the *primary* point-series /
   range workload (must not regress the `s8/t90` read-optimality that the whole policy rests on), and (b)
   **append + compaction cost** per block. A read regression on point-series is a veto — the base layout
   exists for that workload (§1 non-goals).

**Until this gate is passed and benchmarked, O2 is not implementable; O4 defer-with-alarm is the standing
current-disk strategy.** The gate is its own step (fits under/before P4-S8); this spec does not pre-commit to
segmenting the base.

---

## 7. Q5 — Cron flow

**Current (VM24, README):** a daily MUR job writes the daily Zarr; a **separate delta-append cron**
(`ops/cron_mur_delta_append.sh`, twice daily 20:30 + 07:30 UTC) appends yesterday into delta; PM2
restart-after-append is the metadata-visibility fallback. **Note: `ops/` scripts live only on VM24, not in
the repo tree** — so the P4 cron is authored fresh (P4-S7+), reviewed, then ops-installed.

**Target cron (idempotent, lock-protected, structured logs). Ordered steps with tags:**
1. **download/convert** MUR NetCDF for the target day → local NetCDF cache. **[MUT-STG]** (writes only the
   NetCDF cache / daily staging, not the served cube).
2. **validate daily/source** (§8.1): daily-group or NetCDF for the day exists, expected vars present,
   range/NaN-fraction sane. **[RO]** on the served cube.
3. **append-or-skip to delta** (§7.1). **[MUT-LIVE-APPEND]** for a genuinely new day: `append_to_delta`
   finalizes `attrs["days"]` **last**, so the served snapshot never sees a half-written day; a crash
   mid-append leaves the checkpoint `_delta_append_<day>.json` for a resume, and the day stays invisible
   until finalized. **If day D already exists in `delta.days` and passed validation, SKIP (do NOT
   overwrite)** — see §7.1. A silent live overwrite ([MUT-LIVE-OVERWRITE]) is forbidden in the cron path.
4. **validate delta visibility** (§8.2): `D in delta.days`, `var_valid` correct, sample parity vs
   daily/NetCDF, and `/healthz` shows `delta_latest`/`spatial_window` including D **within the refresh TTL**.
   **[RO]**.
5. **optional prune** (daily staging §4 and/or delta §5) — only when eligibility + manifest pass. **[MUT-DEL]**
   for hard delete, but default is move-to-hold; gated by §10.
6. **optional reload / TTL wait**: the TTL loop (`GHRSST_CUBE_REFRESH_TTL_SECONDS=300`) makes the appended
   day visible without restart (PR #20; verified on VM24 shadow). Cron either **waits one TTL and asserts
   visibility** (step 4) or triggers an explicit refresh; PM2 restart stays as belt-and-braces.

### 7.1 Append vs overwrite (production-cron safety — reviewer P4-S4 fix)

Production logs show that a cron **retry currently OVERWRITES an already-present delta day**.
`append_to_delta`'s overwrite branch rewrites the day's array slabs and flips `var_valid[t]` **while
`day_index[D]` is already visible to readers** ([MUT-LIVE-OVERWRITE]) — this is **not atomic**: a concurrent
point/range/bbox read can observe a torn mix of old and new slab bytes (the finalize-days-last guarantee only
protects *new* days; it does nothing for a day already in `days`). This must change before cube-authoritative
rollout:

- **Normal cron = append-or-skip, never overwrite.** Before appending D, check membership + validity **[RO]**:
  - `D ∈ delta.days` **and** D passes the §8 validation gates ⇒ **SKIP** (`op=skip/already_valid`). The day is
    already correctly served; re-writing it only risks a torn read for no gain.
  - `D ∉ delta.days` ⇒ **[MUT-LIVE-APPEND]** (`op=append`), the safe finalize-last path.
  - `D ∈ delta.days` but validation **fails** (partial/corrupt day) ⇒ **do NOT silently overwrite**; route to
    repair mode below and **alarm**.
- **Overwrite only in an explicit repair/force mode** (`--repair` / `GHRSST_INGEST_ALLOW_OVERWRITE=1`, off by
  default), and even then never as a live in-place slab rewrite. Repair uses one of:
  - **rebuild-then-swap** (§2/§5): rebuild the delta with the corrected day (chronological, sourced from
    staging/NetCDF), validate, atomic **[MUT-SWAP]** — no reader ever sees a torn day; **preferred**, and it
    also repairs append-order; OR
  - a **maintenance lock / serving-disable window**: take the delta out of service (or drop it from routing)
    for the duration, do the in-place overwrite, re-validate, re-enable — only when a full rebuild is too
    expensive and a brief spatial-window outage is acceptable. This is [MUT-LIVE-OVERWRITE] made safe by
    *removing concurrent readers*, not by pretending the slab write is atomic.
- **Logs must name the branch** (§8): `op ∈ {skip/already_valid, append, repair_overwrite}` per day, so a
  retry that skipped is distinguishable from one that appended or repaired. `repair_overwrite` lines must also
  record the repair mechanic (`rebuild_swap` vs `maintenance_lock`) and the operator/mode flag.

> Note: [`dual_write.py`](../ingest/dual_write.py) `append_to_delta` today does an idempotent *upsert*
> (overwrite if present). That upsert is fine for **offline staging/shadow rebuilds** ([MUT-STG]) but is the
> exact behaviour the cron must **not** invoke on the live delta. P4-S7 adds the membership/validity guard in
> the cron wrapper (not necessarily in `append_to_delta` itself, which stays a low-level primitive).

**Idempotency / locking / logging requirements:**
- **Lock:** a per-day (or global-delta) file lock / `flock` so overlapping cron runs (the two daily slots +
  manual reruns) can't append concurrently and race the `attrs["days"]` finalize. `append_to_delta` is
  per-`(day,var,tile)` checkpointed but is **not** concurrency-safe against *itself* on the same delta.
- **Idempotent:** re-running any step for the same day converges without a live overwrite — **append =
  append-or-skip** (§7.1: skip if present+valid, append if new, repair-mode only on validation failure),
  prune = skip if already pruned/held, compaction = skip if already folded. Resumable via the existing
  `_delta_append_<day>.json` checkpoint (a resume completes an *unfinished new* day; it does not overwrite a
  finalized one).
- **Structured logs:** one JSONL line per step per day (`day, step, op, elapsed_s, rss_mb, result, error`),
  to `…/logs/…/append_YYYY-MM-DD.log` (matches the current log convention). Enough to reconstruct what ran.

**When does PM2 restart become optional / removable?** Gate it on the TTL-refresh path being proven in
production:
- **Precondition:** `GHRSST_CUBE_REFRESH_TTL_SECONDS > 0` (300 on VM24) AND a shadow/VM24 assertion that,
  after an append, `/healthz` `delta_latest` advances within one TTL **without** a restart (Codex already
  demonstrated this on VM24 shadow — cite that run in the P4-S9 gate).
- **Step-down, not step-off:** (i) keep PM2 restart but make it *conditional* — only restart if step-4
  visibility assertion **fails** after `TTL + slack`; (ii) after K consecutive days of TTL-only success in
  production, mark the unconditional restart **removable**; (iii) remove it in a separate ops change, leaving
  the conditional restart as the failure fallback. **Never** remove the fallback and the visibility assertion
  in the same change.

---

## 8. Q6 — Validation gates (definition of "a day is successfully ingested")

A day D is **successfully ingested** iff ALL of the following pass (all **[RO]** except the append itself):

**8.1 Source presence** — the daily group for D exists (`group_path(daily, D)` is a valid Zarr group) OR the
MUR NetCDF for D is present in the cache; expected vars (`sst`, `sst_anomaly`, `sea_ice`) enumerated;
absent-var recorded (P1 omit-vs-null semantics, `var_valid` = False for a truly absent var, not an error).

**8.2 Delta membership + validity** — `D in delta.days`; `var_valid[var][day_index[D]]` correct for each var
(present ⇒ True, absent ⇒ False); `check_coverage`-style `structural_ok` for the delta (unique days; base
still `latest_in_sync` for point/range). Note append-order: assert via `day_index[D]`, not position.

**8.3 `var_valid` correctness** — for each var, the delta slab at `day_index[D]` is all-NaN iff the var was
absent in the source, and finite where the source had data (spot-checked, not full-scan).

**8.4 Sample parity vs daily / NetCDF (semantic float32, NOT byte)** — N random points + N random bbox cells:
values equal after casting both sides to `np.float32` (P4 parity rule — orjson shortest-round-trip text may
differ while float32 values match; compare in float32, never bytes). Absent→omit (point) / null (bbox/batch),
land-NaN→null semantics reproduced. Sample POST/batch points **within a bbox window** so the daily-parity read
stays under the `points_batch` fan-out limit **64** (global-random points would trip the limit and test
rejection instead of parity — the P4-S0b harness bug; already fixed in `_window_points`).

**8.5 `/healthz` freshness** — within the refresh TTL, `/healthz` reports `delta_latest ≥ D` (chronological)
and `spatial_window` includes D; route for a multi-day range ending D returns `X-Store-Route: cube` (not a
daily fallback). This is the P4-S0b "stale metadata after append" fix (PR #20) asserted per-day.

**8.6 Spatial window correctness** — `spatial_day_allowed(D, delta.days)` is True; a day just outside the
window returns the 400 `available_spatial_window` payload (`rejection_payload`). Confirms the gate tracks the
window as it slides.

**Failure behaviour + retry (append-or-skip, never silent live overwrite — §7.1):**
- Any gate fails ⇒ **the day is NOT marked ingested**; do **not** prune anything for that day. Recovery
  depends on *whether the day was finalized* (`D ∈ delta.days`):
- **(a) Append crashed BEFORE finalizing `attrs["days"]`** (D not yet in `delta.days`): the day is still
  **invisible** to readers (finalize-last, §2). **Retry = resume the checkpoint** (`_delta_append_<day>.json`)
  to complete the *unfinalized* append (`op=resume_unfinalized_append`). This only ever finishes a new,
  not-yet-visible day — it is **not** an overwrite of a live day.
- **(b) Day is already FINALIZED (`D ∈ delta.days`) and validation fails** (partial/corrupt visible day):
  **do NOT re-run a live overwrite** ([MUT-LIVE-OVERWRITE] is forbidden in cron). Enter **explicit repair
  mode** (§7.1): preferably **rebuild-then-swap** (rebuild the delta with the corrected day from
  staging/NetCDF, validate, atomic [MUT-SWAP]), or a **maintenance-lock / serving-disable window** if a full
  rebuild is too costly. Alarm; hold; human/Codex triage.
- **Normal cron retry must converge to `skip/already_valid`** (D present + valid) **or
  `resume_unfinalized_append`** (case a) — **never a silent overwrite**. Bounded retries with backoff; after
  M failures, **alarm** and stop — never auto-prune or auto-compact on a day that failed validation.
- **Parity failure specifically** ⇒ the finalized delta day is suspect → case (b) repair path (rebuild-then-
  swap / maintenance window), not a live re-append. Never prune the daily/staging copy of a day whose delta
  parity is failing.

---

## 9. Q7 — API behaviour after pruning (must stay invariant)

Pruning/compaction must not change the *served contract* beyond the intended spatial-window slide:
- **point GET + range = full history**, served from **base + delta** (delta precedence on overlap;
  [`tiered_cube.py`](../store/tiered_cube.py) `point_series`). Compacting a day from delta into base keeps it
  fully point/range-servable — that's why compaction precedes delta prune (§5).
- **bbox + `POST /points` = delta window only.** Days in the current delta window → 200. Days outside (older
  than the window, incl. days just compacted into base) → **HTTP 400** with `available_spatial_window`
  (`_spatial_window_gate` + `rejection_payload`). The window edge slides forward exactly as delta prunes its
  oldest kept day.
- **`/healthz`** reflects the new state after refresh: `delta_latest`, `delta_day_count`, `spatial_window`,
  `cube_latest_in_sync`, `route_counts`. Ops asserts these post-prune/compaction.
- **CoverageJSON stays OFF** (`GHRSST_ENABLE_COVERAGEJSON=0`) until the frontend contract (P3-S3) is
  approved. **Pruning/compaction logic must not depend on CoverageJSON** in any way — it is a wire-format flag
  on the *spatial* path only, gated *behind* the window enforcement, and orthogonal to retention.
- **No new 5xx / no regressions** on point/range while a swap is in flight: swaps are atomic and the old store
  survives until `/healthz` confirms the new one, so there is no window where a served day disappears.

---

## 10. Rollback strategy (consolidated — required by the acceptance criteria)

**Layered recovery, cheapest first:**
1. **Atomic-swap revert (seconds–minutes):** every mutating store op (`prune_delta`, `compact`) builds a new
   store beside the old and keeps the old as `<store>.pre-<op>` until `/healthz` confirms the new one. Revert
   = repoint the env/path back and `refresh()`. No data loss.
2. **Trash/hold window (days):** hard delete is **deferred** — pruned daily-staging days and pre-swap stores
   move to a **trash/hold** location and are hard-deleted only after `HOLD_DAYS` (default ≥ the rollback
   window, e.g. 7–14). A cube defect discovered within the hold window is recovered by restoring from hold —
   **no redownload needed**.
3. **NetCDF redownload (hours):** beyond the hold window, the canonical recovery is **redownload the MUR
   NetCDF for the affected span → re-append to delta / rebuild base** (NOT "restore the daily Zarr"). The
   prune manifest identifies exactly which spans need re-derivation and records the redownload source/window.
   Recovery is only *guaranteed* where redownload is confirmed available (§12-Q1).

**Corruption plan:** if base/delta is found corrupt for a span → (a) if within hold, restore pre-swap store;
(b) else re-derive from redownloaded NetCDF (primary) using the manifest's span list. Compaction/prune never
proceed while a corruption is under investigation for an overlapping span.

**Hard stop (the golden rule, restated as an acceptance gate):** **no [MUT-DEL] of any full-history daily
span** until a **dry-run** ([MUT-STG], on staging/shadow) proves, **per day**, that the day is *either*
re-derivable (redownload confirmed OR in hold OR in the delta window) *or* no longer needed. The dry-run
emits the prune manifest without deleting; only after Codex/ops sign-off on that manifest does hard delete
run — and even then to trash/hold first, not `rm`.

---

## 11. Q8 — Deliverables + step sequence

**Deliverable of P4-S4 (this task):** this spec file, `p4_ingest_prune_retention_design.md`; a short status
line may be added to [`../README.md`](../README.md) noting "P4-S4 ingest/prune/retention design drafted
(spec-only)". **No production mutation. No implementation until Codex sign-off.**

**Step sequence (each step names its safety envelope):**
- **P4-S4 — spec/design only (this task).** Output: this file. No code.
- **P4-S5 — read-only / audit tools [RO].** Implement, run local/shadow: `retention_report` (staging vs delta
  vs base spans over **sorted calendar days**, `recent_window_contiguous`, full **gap list**,
  `missing_in_window`, and `staging_keep_set` under **both** `conservative` and `accepted_risk` modes — §4.1),
  `prune_eligibility` (which daily days pass §4 checks incl. the gap precondition, which delta days are
  §5-eligible *after verified base coverage*), `disk_report` (free disk vs each compaction option's
  requirement + margin, current `delta_span` vs the O4 alarm threshold). Pure reporting; writes nothing to
  served stores. Emits the *would-be* manifest and **refuses to mark anything prune-eligible if a recent-window
  gap exists** (surfaces "repair first").
- **P4-S6 — safe delta pruning on staging/shadow [MUT-STG].** Implement `prune_delta(delta, keep_days,
  source) -> new_delta` (rebuild-then-swap, §5), factored so `compact()` reuses it. Prove append-order safety
  with a fixture that backfills an out-of-order day then prunes (assert `day_index`/physical-slab mapping and
  chronological repair). Swap tested on a copy, never live.
- **P4-S7 — daily-staging prune + validation manifest [MUT-STG]→[MUT-DEL-to-hold].** Implement the §4
  eligibility + §8 gates + append-only manifest + move-to-hold (not `rm`). Dry-run mode emits the manifest
  without deleting.
- **P4-S8 — compaction strategy [MUT-SWAP] or defer [O4].** **Default deliverable = O4 defer-with-alarm**
  (delta-span / free-disk alarm) — the only safe current-disk strategy. O1/O3 unlock only if ~2 TB disk is
  provisioned. **O2 (rolling/block-level) is blocked behind the §6.1 design gate** (segmented base vs one
  Zarr, segment read path, swap granularity, read-impact benchmark); do not implement O2 until that gate is
  passed. Whichever lands uses the disk precheck + manifest + swap. Decision recorded with the binding gate.
- **P4-S9 — VM24 shadow / dry-run gate (Codex / ops).** Full-31-day retention validated on a shadow/staging
  delta (build 31+ recent days, run §8 gates, run a dry-run prune emitting the manifest, assert §9 API
  behaviour). Confirms TTL-refresh visibility so PM2 restart can step down (§7). **Read-only against
  production; all mutation on shadow/staging.**
- **P4-S10 — production rollout (Codex / ops).** Only after S9 green + sign-off: enable staging prune (to
  hold), then delta prune (post-compaction), then compaction (or keep O4). Staged, reversible, alarmed.

---

## 12. Open questions (for orchestrator / Codex)

1. **NetCDF redownload SLA/window** — what is the MUR archive's guaranteed redownload availability (how far
   back, rate limits, URL stability)? This decides whether Option C (staging N=0) is ever acceptable and
   bounds the §10 hours-scale recovery. Until confirmed, keep the 31-day (+buffer) daily staging (Option A).
2. **Compaction disk** — is a second ~2 TB volume provisionable (enables O1/O3), or do we commit to
   block-level/rolling O2, or defer (O4) until disk is provisioned? Sets P4-S8 scope.
3. **Hold-window length** `HOLD_DAYS` and **staging buffer** `STAGING_BUFFER_DAYS` / `DELTA_BUFFER_DAYS`
   concrete values (proposed HOLD=7–14, staging buffer 3–7, delta buffer 2–3) — tune with ops' disk budget.
4. **Compaction cadence** — event-driven (compact when `delta_span > SPATIAL_WINDOW_DAYS + threshold`) vs a
   maintenance-window schedule? Affects the O4 alarm thresholds.
5. **Confirm** the ordering guarantee is acceptable: **compact-into-base BEFORE delta-prune**, so no delta
   day is ever dropped before its point/range history is in base. (This spec assumes yes.)

---

### Acceptance self-check (against the P4-S4 criteria + reviewer round 1)
- ✅ Every operation tagged **[RO] / [MUT-STG] / [MUT-LIVE-APPEND] / [MUT-LIVE-OVERWRITE] / [MUT-SWAP] /
  [MUT-DEL]** (§0.1 legend, used throughout) — the live-delta append is its own category (reviewer #1).
- ✅ **Append vs overwrite** distinguished: cron skips-or-appends, overwrite only in repair mode via
  rebuild-then-swap / maintenance lock, logs name the branch (§7.1, reviewer #2).
- ✅ **O2 downgraded to a design-gated candidate**; O4 defer-with-alarm is the only safe current-disk
  strategy (§6, §6.1, reviewer #3).
- ✅ **Daily-staging prune under deferred compaction** explicit: conservative (default) vs accepted-risk
  (§4 rule 3, reviewer #4).
- ✅ **Gap-aware retention**: sorted calendar days, latest-31-contiguous precondition, gap report,
  stop-and-repair — never a bare count (§4.1, §5, reviewer #5).
- ✅ Rollback strategy present and layered (§10) + per-step envelope (§11).
- ✅ **No proposal to delete full-history daily** before a per-day dry-run proves re-derivable-or-not-needed
  (§4 eligibility, §10 golden rule, P4-S7 dry-run manifest).
- ✅ **Append-order delta days handled explicitly** (§2 spine; §5 rebuild-then-swap by `day_index`/
  chronological, never `days[-1]` / never in-place metadata edit; §6 fold-by-date).
- ✅ Spec-only; no production mutation; implementation deferred to P4-S5+ after sign-off.
