# P4-S8 — atomic swap execution + compaction ordering design (SPEC-ONLY)

Status: **ACCEPTED (Codex rounds 1–2) + S8a VERDICT FOLDED IN.** (Claude authored.) **No live swap, no
production mutation.** The §2/§3/§9 swap-posture sections are now **evidence-based**: the S8a
cached-handle proof test ran and FAILED → S1/S2 are staging/shadow-only; production swap requires request
quiescence (PM2 restart-under-lock now; serving-disable as future design). See
[`p4s8a_swap_executor_results.md`](p4s8a_swap_executor_results.md).

> **Round-1 patches folded:** (1) S1 symlink swap downgraded to *candidate*: atomic **path switch ≠
> application metadata consistency** — production-eligible only after the S8a cached-zarr-handle proof
> test (§2); (2) §4 verification now distinguishes **served_window** (delta membership,
> `[min(keep), max(keep)]` — what the deployed gate/`/healthz` actually implement) from
> **policy_window_expected** (latest 31 calendar days, must be ⊆ served) — with a code+production-evidence
> correction and new §9-Q6 for a possible 31-day public cap; (3) TTL-wait refresh is **shadow/staging
> only**; production swap **blocked until §9-Q5** (admin refresh endpoint or PM2-restart-under-lock);
> (4) staleness guard requires **uniqueness + exact set**, not set equality alone; (5) VM24 evidence
> updated to 2026-07-08 state (delta 2026-05-30..2026-07-06, 38 days). Defines HOW the swap plan returned by `ingest/prune_delta.py` (P4-S6) gets executed
safely, the rollback path, the `/healthz`/refresh verification, the compact-before-prune ordering, the O4
defer-with-alarm deliverable, and the O2 base-segmentation gate status. Any live swap happens only in
**P4-S9 (VM24 shadow) / P4-S10 (production), executed by Codex/ops with separate approval** — never in S8.

Builds on: [`p4_ingest_prune_retention_design.md`](p4_ingest_prune_retention_design.md) (P4-S4 baseline,
§0.1 safety legend reused), [`p4s5_retention_audit_results.md`](p4s5_retention_audit_results.md),
[`p4s6_delta_prune_results.md`](p4s6_delta_prune_results.md). PR #25 is the committed baseline.

---

## 0. Scope / non-goals

**In scope (this spec):** swap ordering + concurrency guards; rollback; verification probes; the
compact-before-prune constraint as an executor precondition; the O4 alarm deliverable; hold/trash lifecycle
conventions (shared with P4-S7); the O2 gate restated as blocked.

**Non-goals:** implementing the executor (S8a, after sign-off); touching VM24/cron/live stores; O2
block-level compaction (blocked behind §6.1 of the P4-S4 spec); the daily-staging prune itself (P4-S7 —
but §7 below fixes the conventions S7 must follow so S7 doesn't get reworked).

---

## 1. The swap problem, precisely

`prune_delta` builds a validated staging delta and returns a plan
`{from: <staging>, to: <live delta>, backup: <live>.pre-prune, performed: false}`. Executing it must
answer three hazards:

1. **Reader torn-path window.** `TimeCubeStore._Meta` caches zarr array handles, but **chunk reads resolve
   by path at read time**. A naive `rename(live → backup); rename(staging → live)` has a window (ms) where
   the live path does not exist → an in-flight bbox/point read can ENOENT → 5xx.
2. **Writer race (stale plan).** The delta-append cron may have appended a new day between plan build and
   swap. Swapping then would **silently delete the newest day** — the exact class of loss this whole phase
   exists to prevent.
3. **Metadata visibility.** After the swap the serving process must refresh (`TieredCube.refresh()` / TTL
   loop) before `/healthz` and routing reflect the new day set; verification must wait for that.

## 2. Swap mechanics — two options, one recommended

### Option S1: symlink indirection — **PROOF TEST FAILED → REJECTED for production (S8a verdict)**
- One-time ops migration (S9/S10, Codex): move the real delta dir to a versioned name
  (`…delta.zarr.v<UTCSTAMP>`) and make `GHRSST_DELTACUBE_PATH` a **symlink** to it. No API code change:
  zarr resolves the symlink per open/read.
- Swap = `symlink(new_target, tmp); rename(tmp, live_symlink)` — **one atomic rename**, POSIX-guaranteed.
  No window where the path is missing.
- Rollback = the same atomic rename back to the old target. Old versioned dir **is** the backup (no
  second copy).
- Cost: a one-time prod path migration + the (existing) rule that paths come from env, never hardcoded.

> **⚠ Two DIFFERENT guarantees — do not conflate (Codex S8-review #1).** The atomic rename guarantees an
> **atomic path switch** (no ENOENT window). It does **NOT** guarantee **application metadata
> consistency**: the API's `TimeCubeStore._Meta` snapshot (`days`/`day_index`/`var_valid` + cached zarr
> array handles) is captured at open/refresh time. Between the symlink retarget and the next `refresh()`,
> a reader holding the **old `_Meta`** may issue chunk reads that resolve **through the new symlink
> target** (depending on how zarr's LocalStore resolves paths — per-read path resolution vs a resolved
> root captured at open). That is the real torn-read risk: **old `day_index` × new chunk bytes** — worse
> than ENOENT because it returns *wrong data for the wrong day* silently.
>
> **S8a PROOF TEST RAN — VERDICT: FAILED (silent mixing confirmed).** `tests/test_phase2_p4s8a.py`
> `TestCachedHandleProof` + `specs/p4s8a_swap_executor_results.md`: with a `TimeCubeStore` opened through
> the symlink and the symlink atomically retargeted, a **pre-refresh read returned the NEW target's bytes
> against the OLD `day_index`** (asked for day1 of A = 101.0, got B's 501.0 — no error); retargeting to a
> **smaller** delta returned **silent `None`** (missing chunk → fill NaN → "absent"), also no error. zarr's
> LocalStore resolves the path **per chunk read**. Neither outcome is "stable-old or error".
>
> **Consequences (binding):** (a) **S1 bare symlink retarget is REJECTED for production**; (b) **S2
> double-rename shares the same path-reuse defect** plus the ENOENT window — also not production-safe;
> (c) **both S1 and S2 are retained for staging/shadow/rehearsal only**; (d) **production swap REQUIRES
> request quiescence**: **PM2 restart-under-lock** (available on VM24 today) or a **serving-disable/drain
> window** (future design). An admin-refresh endpoint is at most an auxiliary aid — it fixes new-request
> visibility only and does NOT quiesce in-flight `_Meta` holders, so it is **no longer listed as a
> standalone acceptable option**. The proof test pins this behavior: a future zarr upgrade that changes
> path-resolution semantics fails the test loudly and the verdict must be re-derived.

### Option S2 (fallback): double rename under lock + quiet window
- `rename(live → live.pre-prune); rename(staging → live)` back-to-back under the ingest lock, run
  **off-peak**; accept the ms-scale ENOENT window (a failed read surfaces as one retryable 5xx).
- Only acceptable for **shadow/staging** and as a fallback if ops rejects the symlink migration.

**Decision (§9-Q1, RESOLVED by the S8a proof test):** S1 is **rejected** for production; S2 was never
production-eligible. The S8a executor implements both modes for **staging/shadow rehearsal only**; the
production swap posture is **quiescence-based** (§3 step 7): PM2 restart-under-lock now, serving-disable
as a future design. Production must not reuse the executor without an external quiescence wrapper
(plugged in via `refresh_fn`).

Both options require **same-filesystem** staging (`os.rename` must not cross devices) — executor prechecks
`st_dev` of staging vs live parent **[RO]**.

## 3. Execution ordering (the swap runbook the S8a executor encodes)

| step | op | tag |
|---|---|---|
| 1 | run the P4-S5 audit; require no `audit_failures`, window contiguous | **[RO]** |
| 2 | build staging delta via `prune_delta` (all S6 fail-closed gates); require `status=="ok"` | **[MUT-STG]** |
| 3 | **acquire the ingest lock** (same `flock` the delta-append cron holds — swap and append are mutually exclusive) | — |
| 4 | **staleness guard:** re-read live `attrs["days"]` under the lock; require **(a) `len(live_days) == len(set(live_days))` (unique, no duplicate days)** and **(b) `set(live_days) == set(keep_days) ∪ set(dropped_days)`** from the plan, **exactly**. Set equality alone misses duplicates (Codex S8-review #4). Any new/absent/duplicate day → **abort, release lock, discard nothing, rebuild plan** | **[RO]** |
| 5 | re-verify staging: re-run the plan's `validation` block (cheap re-read) + `st_dev` precheck | **[RO]** |
| 6 | swap (S1 symlink rename, or S2 double rename); record swap manifest line (swap id, UTC, operator, old/new targets, keep/dropped sets, plan digest) | **[MUT-SWAP]** |
| 7 | refresh + quiescence — **posture RESOLVED by the S8a proof-test verdict (§2):** **shadow/staging** may use the TTL-wait path (wait `GHRSST_CUBE_REFRESH_TTL_SECONDS + slack`, poll `/healthz`) — mixing during a rehearsal is observable, not served to users. **Production MUST quiesce in-flight requests before/at the swap**: (a) **PM2 restart under the still-held lock** — the currently available VM24 posture (process replacement kills in-flight `_Meta` holders AND reopens metadata), or (b) a **serving-disable/drain window** (future design, §9-Q5). An **admin-refresh endpoint is auxiliary only** — it fixes new-request visibility, not in-flight reads — and is NOT a standalone option (proof test: pre-refresh reads silently mix old meta × new bytes). TTL-wait is never sufficient in production. | **[RO]/ops** |
| 8 | **verify (§4)**; on failure → **rollback (§5)** | **[RO]** |
| 9 | release lock; move the old delta (backup) into the **hold** area (§7); schedule hard delete only after `HOLD_DAYS` | **[MUT-DEL-to-hold]** |

The lock spans steps 3–9 so no append can land mid-swap; the lock file + timeout/backoff semantics are
defined with the cron design (P4-S4 §7) and shared by S7.

## 4. Post-swap verification (all [RO]; every probe must pass)

**Two windows — keep them distinct (Codex S8-review #2, corrected against the deployed code):**

- **`served_window = [min(keep), max(keep)]`** — what the API actually serves and reports. Enforcement is
  **delta membership** (`spatial_day_allowed(day, delta.days)`, [`api/app.py`](../api/app.py)
  `_spatial_window_gate`) and `/healthz.spatial_window` is `spatial_window_bounds(delta.days)` = the
  **full delta span**, not a 31-day cutoff. Live evidence: VM24 today holds 38 delta days
  (`2026-05-30..2026-07-06`) and `/healthz` reports that whole span; a bbox on a 35-day-old delta day
  returns 200. So when `keep` includes buffer days, **buffer days are publicly served while in delta** —
  by design (P4 §4.1 round-4: spatial availability == delta membership).
- **`policy_window_expected = [max(keep) − (SPATIAL_WINDOW_DAYS − 1), max(keep)]`** — the latest 31
  calendar days: the **minimum guarantee** the retention invariant promises. The keep-set may be larger
  (buffer/hold policy) but never smaller.

Asserting `/healthz.spatial_window == policy_window_expected` would **fail against current production
behavior** whenever buffer days exist; the correct assertions are:

1. `/healthz`: `delta_latest == max(keep)`; `delta_day_count == len(keep)`;
   **`spatial_window == served_window`** ( `[min(keep), max(keep)]` ); **`policy_window_expected ⊆
   served_window`** (the ≥31 invariant); `cube_latest_in_sync == true`; `rss_mb` bounded;
   `spatial_window_enforce` unchanged.
2. **Kept-day probes:** bbox GET + POST /points on (a) a day inside `policy_window_expected` AND (b) a
   buffer day older than it but still in `keep` → both 200, values sane; multi-day range ending
   `max(keep)` → `X-Store-Route: cube`.
3. **Dropped-day probes (policy intact):** bbox/POST on a just-dropped day → **400 with
   `available_spatial_window == served_window`** (the `rejection_payload` reports the delta span);
   **point GET on the same dropped day → 200 (served from base)** — the "no point/range history lost"
   invariant made observable.
4. **Parity spot-check:** N sampled points on 2–3 kept days equal pre-swap values (float32 semantic).
5. Timing: production verification runs after the step-7 explicit refresh / restart (never TTL-wait);
   shadow/staging may TTL-wait one cycle + slack. If `/healthz` still shows pre-swap metadata after the
   chosen mechanism, treat as **verify-fail → rollback** (do not "wait and hope").
6. **Quiescence caveat (Codex round-2):** these probes are NEW requests — they prove post-refresh
   visibility, **not** that reads already in flight during the swap were safe. In-flight safety comes
   from the step-7 mechanism choice (restart/serving-disable quiesces; admin-refresh alone does not) and
   is gated on the S8a cached-handle proof test's verdict (§2).

> **New open question (§9-Q6):** if Codex/product wants the *public* window capped at exactly 31 days
> regardless of buffer days in delta (i.e. gate + `/healthz` computed from a 31-day cutoff instead of
> membership), that is an **API behavior change** to `_spatial_window_gate`/`spatial_policy` — a separate
> reviewed change, not a swap-verification assertion. Until then the spec verifies the deployed
> membership semantics.

## 5. Rollback

- **Verify-fail rollback (minutes):** reverse the swap under the still-held lock (S1: atomic rename back to
  the old target; S2: reverse double rename), refresh, re-run §4 against the OLD expected state, release
  lock, mark the staging delta `.failed-<swap id>` for diagnosis. Nothing is lost: the old delta was never
  modified, only renamed/retargeted.
- **Late-discovered defect (within `HOLD_DAYS`):** the pre-swap delta lives in hold → re-point/rename it
  back (same mechanics, new swap id), refresh, verify. The swap manifest identifies exactly which swap to
  undo.
- **Beyond hold:** rebuild the delta from daily staging / base+NetCDF per the P4-S4 §10 recovery ladder.
- Rollback is itself a **[MUT-SWAP]** with its own manifest line — audit trail is append-only both ways.

## 6. Compact-before-prune — executor precondition, and the current VM24 reality

- **Constraint (unchanged from P4-S4 §5):** a delta day may be dropped only if its point/range history is
  already served by base. `prune_delta` enforces this (`base_days` mandatory when dropping); the S8a
  executor **re-asserts it at swap time** (step 4 re-reads live days; step 5 re-checks dropped ⊆ base) so a
  base rollback between plan and swap can't slip through.
- **Ordering:** `compact into base → verify base coverage → prune delta → swap`. Under **O4 (compaction
  deferred)** the chain stops before prune: delta grows, alarm fires (§8), **no swap runs**.
- **Current VM24 note (evidence, not authorization; state as of 2026-07-08, Codex S8-review #5):** base
  covers `2023-01-01..2026-06-26`; daily + delta through `2026-07-06`; delta span `2026-05-30..2026-07-06`
  (38 days). The latest-31 policy window is `2026-06-06..2026-07-06`; the 7 delta days older than it
  (`2026-05-30..2026-06-05`) are all ≤ `2026-06-26`, i.e. **already inside base's span** (the June full
  rebuild acted as compaction for them) — so a *first* shadow prune would pass the S6 base-coverage gate
  **without any new compaction**. This is a one-time artifact of the rebuild and this note goes stale daily
  (base is fixed while delta advances): days `2026-06-27+` are delta-only, so once they age past the
  window, O4 applies and pruning stops at the gate. S9 should exploit the compact-free state while it
  lasts and re-check spans at run time (the P4-S5 audit reports exactly this).

## 7. Hold / trash conventions (fixed here so P4-S7 conforms)

- Hold root: `<data_root>/hold/` (env `GHRSST_HOLD_DIR`, never hardcoded), same filesystem as the stores.
- Naming: `<store_basename>.pre-<op>-<UTCSTAMP>-<swap id>` (op ∈ prune/compact/swap-rollback).
- Every entry has a manifest line (JSONL, append-only, `<data_root>/hold/manifest.jsonl`): day set,
  digests, op, swap id, `hold_until = created + HOLD_DAYS` (default **14**, §9-Q2), operator.
- **Hard delete only by ops, only after `hold_until`, never by the executor or cron** in S7/S8 —
  automating hard delete is a separate later approval.
- P4-S7's daily-staging prune moves pruned day-groups into the same hold root with the same manifest
  schema (the P4-S5 `manifest_preview` fields + `dry_run:false` + timestamps/operator/hold_until).

## 8. O4 defer-with-alarm — the S8 deliverable that ships regardless

Since O4 is the standing strategy, S8's guaranteed deliverable is the **alarm**, not the compactor:
- Extend `ops/p4_retention_audit.py` (or a thin wrapper) with `--alarm` **[RO]**: exit nonzero when
  `delta_calendar_span_days > SPATIAL_WINDOW_DAYS + ALARM_SLACK_DAYS` (default slack **14**, §9-Q3) OR
  `free_disk < HARD_MARGIN_BYTES`. Structured JSON line for ops alerting; cron-schedulable by ops (their
  change, not ours).
- The alarm is what converts "defer" from *ignoring* the problem into *bounding* it: it fires well before
  the delta's size or the disk margin becomes an operational incident, prompting the disk-provisioning /
  compaction decision (§9-Q4).

## 9. Open questions (orchestrator / Codex)

1. ~~S1 symlink migration for production?~~ — **RESOLVED: REJECTED.** The S8a cached-handle proof test
   failed (silent old-meta × new-bytes mixing; silent `None` on a shrunk target — §2). S1/S2 are
   staging/shadow/rehearsal-only; production swap = quiescence posture (§3 step 7).
2. **`HOLD_DAYS`** default 14 OK?
3. **`ALARM_SLACK_DAYS`** default 14 OK (alarm at delta span > 45 days)?
4. When the alarm fires: provision a second volume (unlocks O1/O3) or fund the O2 §6.1 segmentation
   design? (Decision can wait for the first alarm.)
5. **Q5 NARROWED by the S8a verdict:** admin-refresh-alone is **off the table** (does not quiesce
   in-flight reads; §2). The remaining choice is **PM2 restart-under-lock (available on VM24 today —
   default)** vs **designing a serving-disable/drain mechanism** (only worth it if restart-per-prune is
   operationally unacceptable, e.g. prune frequency makes brief restarts disruptive). An admin-refresh
   endpoint may still be added later as an *auxiliary* tool, but it no longer blocks anything.
6. Should the **public spatial window be capped at exactly 31 days** (gate + `/healthz` from a calendar
   cutoff) instead of the deployed delta-membership semantics that also serves buffer days (§4)? If yes,
   that is a separate reviewed API change to `_spatial_window_gate`/`spatial_policy`, plus a frontend
   contract note.

## 10. Step plan after this spec is signed off

- **S8a — DONE** (`ingest/swap_delta.py` + `tests/test_phase2_p4s8a.py`, 16/16;
  `specs/p4s8a_swap_executor_results.md`). Executor: staleness guard (unique + exact set), same-fs
  prechecks incl. hold_dir-vs-live for S2, refresh-exception → rollback (never leaves a swapped live
  behind; `rollback_failed` status if rollback itself fails), hold + append-only manifest. **Proof test
  RAN → S1 REJECTED for production (§2 verdict); S1/S2 staging/shadow-only; production posture =
  quiescence (§3 step 7).** No prod paths, no cron.
- **S8b** — `--alarm` mode on the audit tool **[RO]** + tests.
- **P4-S7** — daily-staging prune + manifest impl, conforming to §7 conventions (move-to-hold, dry-run
  default, hard delete out of scope).
- **P4-S9** — VM24 shadow gate (Codex/ops): shadow-delta swap rehearsal + §4 probes + the §6 one-time
  compact-free prune window; **P4-S10** — production, separate approval.

### O2 status (unchanged)
Block-level compaction remains **blocked behind the P4-S4 §6.1 design gate** (segmented base vs monolith,
segment read path in `TimeCubeStore`/`TieredCube`, swap granularity, read-impact benchmark with
point-series regression as a veto). Nothing in S8 assumes or partially implements O2.
