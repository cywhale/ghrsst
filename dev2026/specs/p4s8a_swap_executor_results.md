# P4-S8a — swap executor + symlink cached-handle proof test: results

Status: **DONE (staging/shadow only). NO production mutation.** Implements the S8-design §3 executor and
runs the §2 proof test that decides S1's production eligibility (§9-Q1).

- Executor: [`../ingest/swap_delta.py`](../ingest/swap_delta.py) — `execute_swap_plan(plan, mode='s1'|'s2',
  hold_dir, lock_path, refresh_fn, verifier, hold_days, operator)`.
- Tests: [`../tests/test_phase2_p4s8a.py`](../tests/test_phase2_p4s8a.py) — **12/12 green** (full P4 set
  51/51).
- Design spec: [`p4s8_swap_compaction_design.md`](p4s8_swap_compaction_design.md) (committed `e21097a`).

## ⚠ THE HEADLINE — proof-test verdict on §9-Q1 (S1 production eligibility): **FAILED**

The reviewer's round-1 concern is **empirically confirmed, in the worst form**. With a `TimeCubeStore`
opened **through a symlink**, `_Meta` + array handles captured, then the symlink atomically retargeted to a
different delta and read **before** `refresh()`:

| scenario | pre-refresh behavior observed | classification |
|---|---|---|
| retarget to same-shape delta (different values) | returns the **NEW target's bytes against the OLD `day_index`** (asked for day1 of A, got B's value **501.0**, not A's 101.0 and not an error) | **SILENT MIXING** |
| retarget to a **smaller** delta (fewer days) | reading an out-of-range old day index does **not raise** — the missing chunk resolves to fill (NaN) → the API's absent/land semantics return **silent `None`** | **SILENT FABRICATED NULL** |
| after `refresh()` | wholly-new metadata + data, correct | OK |

zarr's LocalStore resolves the path **per chunk read**, so cached handles follow the symlink to the new
target while the snapshot metadata stays old. Neither failure mode is "stable-old" and neither is a
detectable error — **both silently return wrong data**. Per the §2 eligibility bar ("stable-old or error,
never silent mixing"):

- **S1 bare symlink rename is REJECTED for production.**
- **S2 double-rename shares the same defect** (path reuse → same pre-refresh window) *plus* the ENOENT
  window; it remains shadow/staging-only.
- **Consequence (folded into the design spec §2/§3/§9):** the *only* production-safe swap postures are
  ones with **request quiescence** — **PM2 restart-under-lock is the currently available VM24 production
  posture** (quiesces by process replacement); a **serving-disable/drain mechanism is a future design
  option**. An admin-refresh endpoint alone is **insufficient** (fixes visibility for new requests; does
  not quiesce in-flight `_Meta` holders) — it may only be an auxiliary aid. **S1/S2 in this executor are
  staging/shadow/rehearsal-only**; production must not reuse the executor without an external quiescence
  wrapper plugged in via `refresh_fn`.

The proof test **pins** this behavior (`TestCachedHandleProof`): if a future zarr upgrade changes path
resolution, the test fails loudly and the verdict must be re-derived.

## The executor (staging/shadow rehearsal + S9 tooling)

> **S10-review addendum (2026-07-09):** the executor gained a **`pre_swap_quiesce_fn` hook** — called
> INSIDE the lock, after the staleness guard + same-fs prechecks, BEFORE any rename. Production (S10)
> passes "pm2 stop + verify port down" here so **quiescence and swap share one critical section**
> (restart-AFTER-swap is rejected: a graceful-shutdown reader can still mix old `_Meta` with new bytes
> across the switch). A stale plan aborts before quiesce (the app is never stopped for a doomed swap);
> a quiesce failure returns `status="quiesce_failed"` with nothing touched + a `swap_quiesce_failed`
> manifest line. Tests: `TestPreSwapQuiesce` (ordering, stale-skips-quiesce, failure-touches-nothing) —
> suite now **19/19**.

`execute_swap_plan` implements the design-spec §3 ordering; policy refusals return a status dict, never
raise:

1. requires a **validated `prune_delta` plan** (`status=="ok"`; anything else → `refused`);
2. mode preconditions: `s1` requires the live path to BE a symlink, `s2` requires it NOT be;
3. **ingest lock** (`flock`, `lock_path` shareable with the future cron) held through verify/rollback;
4. **staleness guard** under the lock: live days must be **unique** (duplicates → `aborted_stale`) and
   `set(live) == set(keep) ∪ set(dropped)` **exactly** (a cron-appended new day → `aborted_stale` with
   `unexpected_new_days`; live left untouched; staging kept for plan rebuild);
5. same-filesystem prechecks (`st_dev`): staging vs live parent (both modes) **and hold_dir vs live parent
   (S2)** — a cross-device hold_dir would otherwise fail MID-swap after live was already moved (Codex
   S8a-review #2); refused cleanly before touching anything;
6. swap: `s1` atomic symlink retarget (backup = old target dir, record-only) / `s2` double rename (backup
   moved into `hold_dir`);
7. `refresh_fn` hook = the step-7 mechanism (tests pass `TimeCubeStore.refresh`; production plugs the
   quiescence mechanism here);
8. verify: built-in local verifier (day set == keep, unique, latest == max) + optional extra verifier;
   **verify-fail OR a raising `refresh_fn`/verifier → rollback under the still-held lock** (a refresh
   exception is treated exactly as a verify failure — a swapped live delta is never left behind; Codex
   S8a-review #1), original live restored and re-verified (best-effort re-refresh; the authoritative check
   is a re-read of live days), `swap_rollback` manifest line; **if rollback itself fails →
   `status="rollback_failed"` + `swap_rollback_failed` manifest line (ambiguous state, human required)**;
9. success: **hold lifecycle + append-only JSONL manifest** (§7 conventions: `swap_id`, `hold_until` =
   now + 14d default, dropped days, verify summary, `hard_delete: ops-only`).

## Test coverage (16/16)

| test | proves |
|---|---|
| `test_pre_refresh_read_is_silent_mixing` | **the S1 verdict** (new-bytes-through-old-meta, value-level assertion) + post-refresh correctness |
| `test_shrunk_target_pre_refresh_is_silent_none` | the silent-`None` variant (no error raised) |
| `test_happy_path` (S2) | swap → live == pruned delta, reader refreshed, backup in hold with ALL original days, manifest line complete |
| `test_happy_path_symlink` (S1) | atomic retarget, backup = old target untouched in place |
| `test_new_day_after_plan_aborts` | cron-race staleness abort, live untouched, staging preserved |
| `test_duplicate_live_day_aborts` | duplicate-day staleness abort (set-equality alone would miss it) |
| `test_rollback_restores_original` / `test_rollback_s1` | injected verify failure → rollback, original live restored + re-verified, rollback audited |
| `test_s2_refresh_raises_rolls_back` / `test_s1_refresh_raises_retargets_back` | **a raising `refresh_fn`** (raising even during rollback) → rolled back, original live restored, staging preserved, reason in the `swap_rollback` manifest line (review #1) |
| `test_cross_device_hold_dir_refuses` / `test_s1_ignores_hold_dir_device` | **cross-device hold_dir refused for S2 before any swap** (simulated via the factored `_st_dev`); S1 unaffected (record-only backup) (review #2) |
| `test_s2_refuses_symlink_live` / `test_s1_refuses_non_symlink_live` / `test_refuses_non_ok_plan` / `test_refuses_bad_mode` | mode/plan refusals |

## What remains

- **S8b** — `--alarm` mode on the audit tool ([RO]) — next.
- **P4-S7** — daily-staging prune + manifest (conforms to the §7 hold/manifest conventions the executor
  already writes).
- **P4-S9 (Codex/ops)** — VM24 shadow rehearsal. Given the proof-test verdict, the S9 runbook should
  rehearse **PM2-restart-under-lock** as the production posture; the executor's `refresh_fn` hook is where
  that plugs in. **Production swap remains BLOCKED** until Q5 is decided between
  restart-under-lock and a serving-disable mechanism.

**No production mutation:** all tests run on synthetic stores in temp dirs; the executor never sees a VM24
path; production execution is S9/S10 (Codex/ops, separate approval).
