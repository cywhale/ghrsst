# P4-S9 — VM24 SHADOW rehearsal runbook (for Codex / ops execution)

**Claude authored this; Codex/ops EXECUTE it on VM24.** Rehearses the full P4 retention/prune pipeline —
audit → delta-prune plan → swap → verification → staging prune — **entirely on shadow copies**. It
validates the tooling and the operator workflow before any production decision.

**Hard boundary (unchanged):**
- **S9 is SHADOW rehearsal.** The live daily store, base cube, delta cube, cron, NGINX, and the
  production PM2 app are **never mutated** by this runbook. **S10 (production) is a separate approval.**
- **No hard delete anywhere** — S9/S10 automation only moves to hold; hold cleanup is **ops-only after
  `hold_until`**.
- If production swap is ever approved (S10), it **must use request quiescence**: **PM2
  restart-under-lock** or a separately-designed serving-disable/drain mechanism. Bare S1/S2 swaps are
  staging/shadow-only (S8a proof-test verdict: silent old-meta × new-bytes mixing;
  [`p4s8a_swap_executor_results.md`](p4s8a_swap_executor_results.md)).

> **⏳ Time-sensitive note — do NOT trust the spec's dates.** The S8 design §6 records a *compact-free
> prune window* (the June full rebuild left base covering through `2026-06-26`, so delta days older than
> the spatial window were base-covered as of 2026-07-08). Base is fixed while delta advances daily, so
> days `2026-06-27+` are **delta-only** and this window erodes every day. **Re-evaluate at run time via
> the step-2 audit** (`delta_days_older_than_window_not_in_base` must be empty for the prune to have
> eligible days); if it has closed, the delta-prune rehearsal degrades to "plan refused at the
> base-coverage gate" — which is itself a valid rehearsal result, not a failure of the runbook.

---

## 0. Code / environment

- **Run the env-setup block below FIRST** (it defines `$ART`, used by every command including the next
  one).
- Branch: **`dev2026-p4-s8-swap-design`** at its **tip**, which must **contain** commit `0133f3e`
  (the S9 runbook; ancestor-check is robust to later doc commits — a fixed pin would go stale):
  ```bash
  cd /home/odbadmin/python/ghrsst-dev2026-phase2
  git fetch && git checkout dev2026-p4-s8-swap-design && git pull --ff-only
  git merge-base --is-ancestor 0133f3e HEAD && echo "ancestor-check OK" || { echo "NO-GO: branch tip lacks 0133f3e"; exit 1; }
  git rev-parse HEAD > "$ART/git_head.txt"          # record the ACTUAL commit executed
  ```
  **Checking out this branch in the worktree does NOT restart or change the production app** (PM2 runs
  the already-loaded process; do not `pm2 restart` for the checkout).
- Python: `dev2026/.venv/bin/python` in that worktree. Sanity: run the shipped tests once —
  `dev2026/.venv/bin/python -m unittest tests.test_phase2_p4s7 tests.test_phase2_p4s8a` (from `dev2026/`)
  → must be OK.

### Paths (all under `/home/odbadmin/Data/ghrsst` — anything outside it, except the worktree, is a NO-GO)

| role | path | touched? |
|---|---|---|
| live daily | `/home/odbadmin/Data/ghrsst/mur.zarr` | **read-only** |
| live base | `/home/odbadmin/Data/ghrsst/mur_timecube_s8_t90_sh128.zarr` | **read-only** |
| live delta | `/home/odbadmin/Data/ghrsst/mur_timecube_s8_t90_sh128.delta.zarr` | **read-only** |
| shadow root | `/home/odbadmin/Data/ghrsst/shadow_p4s9_<RUN_ID>/` | created fresh per run |
| shadow delta ("shadow-live") | `$SH/delta_live.zarr` | copy; mutated by rehearsal |
| shadow staging delta | `$SH/delta_new.zarr` | built by `prune_delta` |
| shadow daily subset | `$SH/daily_subset.zarr/` | partial copy; mutated by staging-prune real run |
| hold | `$SH/hold/` | rehearsal hold + manifests |
| artifacts | `/home/odbadmin/Data/ghrsst/logs/p4s9_<UTCSTAMP>/` | all JSON/log outputs |

Set once per session — the shadow root is **UNIQUE PER RUN** (a fixed dir + `mkdir -p` could silently
mix stale data from a prior rehearsal into this one). Do **not** auto-`rm` old shadow dirs in this
runbook; leftover `shadow_p4s9_*` dirs are cleaned by ops separately, after review:
```bash
export G=/home/odbadmin/Data/ghrsst
export WT=/home/odbadmin/python/ghrsst-dev2026-phase2/dev2026
export PY=$WT/.venv/bin/python
export RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
export SH=$G/shadow_p4s9_$RUN_ID
export ART=$G/logs/p4s9_$RUN_ID
test ! -e "$SH" || { echo "NO-GO: $SH already exists"; exit 1; }
mkdir -p "$SH/hold" "$ART"
```

## 1. Preconditions (all **[RO]**; any failure = NO-GO before anything is copied)

1. **PM2 posture confirmed:** `pm2 ls` shows app **`ghrsst`** online at `127.0.0.1:8035`;
   `curl -fsS http://127.0.0.1:8035/healthz | tee $ART/healthz_before.json` succeeds. Record
   `delta_latest`, `delta_day_count`, `spatial_window`, `spatial_window_enforce`. The production
   restart-under-lock posture is **documented, not executed**, in this runbook (step 5).
2. **Free space:** `df -h $G; du -sh $G/mur_timecube_s8_t90_sh128.delta.zarr`. Require
   **free ≥ 3 × delta size + 100 GB** (shadow-live copy + staging rebuild + working margin; the daily
   subset adds roughly one delta-worth). Below that → NO-GO (do not shrink the margin to fit).
3. **Audit gate (also produces the required artifact):**
   ```bash
   cd $WT
   set +e            # the audit uses exit codes as SIGNALS (2/3); don't let set -e abort before capture
   $PY ops/p4_retention_audit.py \
     --daily $G/mur.zarr --base $G/mur_timecube_s8_t90_sh128.zarr \
     --delta $G/mur_timecube_s8_t90_sh128.delta.zarr \
     --healthz-url http://127.0.0.1:8035 \
     --alarm --json-out $ART/audit.json --strict
   AUDIT_RC=$?
   set -e
   echo "$AUDIT_RC" > "$ART/audit.exit"; echo "audit rc=$AUDIT_RC"
   ```
   - **rc=2** (audit failure: recent-window hole, duplicate days, stale healthz) → **NO-GO, stop**.
   - **rc=3** (alarm fired) → record it; **operator decides** whether to proceed (an alarm about
     delta growth does not by itself invalidate a shadow rehearsal, but a `free_disk` alarm does —
     see precondition 2).
   - **rc=0** → continue.
   - In `audit.json` confirm: `recent_spatial_window.recent_window_contiguous == true`;
     `delta_prune.blocked_need_compaction_first` — if non-empty, the compact-free window has (partly)
     closed: only the `delta_prune.delta_prune_candidates` days are eligible; if THAT is empty, skip
     steps 3–5 (delta prune/swap) and continue with step 6 (staging-prune rehearsal is independent).
4. **Nothing else touched + cron windows (Asia/Taipei local time, NOT UTC):** confirm no other process
   writes `$SH` (fresh dir). VM24's ingest crons run in **Asia/Taipei** local time:
   - daily ingest: **05:05** and **18:05** Asia/Taipei;
   - delta append: **07:30** and **20:30** Asia/Taipei.
   Keep a **30–60 min safety window around all four times** for the shadow-copy and rehearsal steps
   (i.e. do not start step 2 within an hour of any of them), so the shadow copy is internally
   consistent and the rehearsal never overlaps a live append.

## 2. Build the shadow copies (**[MUT-SHADOW]** — writes only under `$SH`)

```bash
# --- consistency guard A: the latest delta-append log must show a COMPLETION MARKER (not mid-run) ---
# NOTE for the operator: the cron script lives only on VM24 — confirm its actual completion marker
# string first (expected "DONE" or "APPEND_DONE"); adjust the grep if the script uses another wording,
# and record which marker was required in the PASS/NO-GO summary.
LOG=$G/logs/delta_append/$(ls -t $G/logs/delta_append/ | head -1)
tail -20 "$LOG" > "$ART/latest_delta_append_tail.txt"
grep -Eq "DONE|APPEND_DONE" "$ART/latest_delta_append_tail.txt" \
  || { echo "NO-GO: newest append log ($LOG) has no completion marker" | tee "$ART/NO_GO_append_log.txt"; exit 1; }

# --- snapshot live delta BEFORE the copy (latest day + count + full day list) ---
snap() { $PY -c "import zarr,sys,json; d=list(zarr.open_group(sys.argv[1],mode='r').attrs['days']); \
print(json.dumps({'latest': max(d), 'count': len(d), 'days': sorted(d)}))" "$1"; }
snap $G/mur_timecube_s8_t90_sh128.delta.zarr > "$ART/delta_days_before_copy.json"

# --- shadow-live delta = full copy of the live delta ---
cp -a $G/mur_timecube_s8_t90_sh128.delta.zarr $SH/delta_live.zarr

# --- consistency guard B (HARD STOP on mismatch): live-after AND the copied shadow must both equal
#     the pre-copy live snapshot — a mismatch means the copy raced an append; discard + retry outside
#     the cron windows. This compares the SHADOW itself, not only live before/after. ---
snap $G/mur_timecube_s8_t90_sh128.delta.zarr > "$ART/delta_days_after_copy.json"
snap $SH/delta_live.zarr                     > "$ART/delta_days_shadow_copy.json"
cmp -s "$ART/delta_days_before_copy.json" "$ART/delta_days_after_copy.json" \
  && cmp -s "$ART/delta_days_before_copy.json" "$ART/delta_days_shadow_copy.json" \
  || { echo "NO-GO: delta changed mid-copy (live-before vs live-after vs shadow disagree) — discard \
$SH/delta_live.zarr and retry outside the cron windows" | tee "$ART/NO_GO_copy_inconsistent.txt"; exit 1; }
# shadow daily subset: the latest ~45 days only (enough to exceed window+buffer; NEVER the full 2TB)
$PY - <<'EOF'
import os, shutil, sys
sys.path.insert(0, os.environ["WT"])
from store.zarr_paths import list_existing_days, group_path
G, SH = os.environ["G"], os.environ["SH"]
daily = os.path.join(G, "mur.zarr"); dst = os.path.join(SH, "daily_subset.zarr")
days = list_existing_days(daily)[-45:]
for d in days:
    shutil.copytree(group_path(daily, d), group_path(dst, d))
print("copied", len(days), "days:", days[0], "..", days[-1])
EOF
```
Verify the shadow stores with the audit tool pointed at the SHADOW paths (live base stays read-only):
```bash
cd $WT && $PY ops/p4_retention_audit.py \
  --daily $SH/daily_subset.zarr \
  --base  $G/mur_timecube_s8_t90_sh128.zarr \
  --delta $SH/delta_live.zarr \
  --json-out $ART/audit_shadow.json
```
Confirm in `audit_shadow.json`: shadow delta spans/day-count match `delta_days_shadow_copy.json`;
`recent_spatial_window.recent_window_contiguous == true`; the daily-subset span covers the intended
~45 (or enlarged) days. (No `--strict`/`--alarm` here — this is a shape check on copies, not a gate;
the production gate already ran in step 1.3.)

## 3. Delta-prune plan against the SHADOW delta (**[MUT-SHADOW]**: writes only `$SH/delta_new.zarr`)

```bash
cd $WT && $PY - <<'EOF'
import json, os, sys
sys.path.insert(0, os.environ["WT"])
import zarr
from datetime import date, timedelta
from ingest.prune_delta import prune_delta
G, SH, ART = os.environ["G"], os.environ["SH"], os.environ["ART"]
shadow = os.path.join(SH, "delta_live.zarr")
base_days = list(zarr.open_group(os.path.join(G, "mur_timecube_s8_t90_sh128.zarr"), mode="r").attrs["days"])
dd = sorted(zarr.open_group(shadow, mode="r").attrs["days"])
W, BUF = 31, 3                                  # keep = latest W+BUF CALENDAR days (P4-S4 §5)
keep_start = (date.fromisoformat(max(dd)) - timedelta(days=W + BUF - 1)).isoformat()
keep = [d for d in dd if d >= keep_start]
plan = prune_delta(shadow, os.path.join(SH, "delta_new.zarr"), keep,
                   source_daily=os.path.join(SH, "daily_subset.zarr"), source_delta=shadow,
                   base_days=base_days, spatial_window_days=W)
with open(os.path.join(ART, "prune_plan.json"), "w") as fh:
    json.dump(plan, fh, indent=2, sort_keys=True)   # the plan dict is plain JSON — step 4 reloads it
print(plan["status"], "| keep:", len(plan.get("keep_days", [])), "| dropped:", plan.get("dropped_days"))
EOF
```
- `status == "refused"` at the base-coverage gate ⇒ the compact-free window has closed (see the
  time-sensitive note): record the refusal as the rehearsal result for this step and continue at step 6.
- `status == "ok"` ⇒ confirm `validation.all_ok == true`, `dropped_days` all appear in the base day
  list, and `keep_days` spans the full latest-31 window.

## 4. Swap rehearsal on the shadow copy (**[MUT-SHADOW]**; S2 mode — shadow paths are plain dirs)

```bash
cd $WT && $PY - <<'EOF'
import json, os, sys
sys.path.insert(0, os.environ["WT"])
from ingest.swap_delta import execute_swap_plan
from store.time_cube import TimeCubeStore
SH, ART = os.environ["SH"], os.environ["ART"]
with open(os.path.join(ART, "prune_plan.json")) as fh:
    plan = json.load(fh)                          # the step-3 plan, verbatim; the executor re-validates
                                                  # everything under its lock (staleness guard + verifier)
if plan.get("status") != "ok":                    # step-3 refusal (e.g. compact-free window closed):
    out = {"status": "skipped", "reason": f"prune plan status={plan.get('status')!r} — swap rehearsal "
           "not applicable; see prune_plan.json", "plan_reason": plan.get("reason")}
    with open(os.path.join(ART, "swap_result.json"), "w") as fh:
        json.dump(out, fh, indent=2, sort_keys=True)
    print("SKIPPED:", out["reason"]); raise SystemExit(0)     # clean skip, not an error
store = TimeCubeStore(os.path.join(SH, "delta_live.zarr"))   # a live reader, rehearsing the serving process
res = execute_swap_plan(plan, mode="s2", hold_dir=os.path.join(SH, "hold"),
                        refresh_fn=store.refresh, operator="p4s9-codex")
with open(os.path.join(ART, "swap_result.json"), "w") as fh:
    json.dump(res, fh, indent=2, sort_keys=True, default=str)
print(res["status"], "| verify.ok:", res.get("verify", {}).get("ok"), "| backup:", res.get("backup"))
EOF
```
Expected: `status == "swapped"`, `verify.ok == true`, backup dir present under `$SH/hold/`, one
`delta_prune_swap` line in `$SH/hold/manifest.jsonl`. An `aborted_stale` here means something wrote the
shadow delta mid-rehearsal — investigate (nothing should).

## 5. Verification probes + the quiescence posture (**[RO]** on production; shadow API optional)

**5a. Shadow API probes (recommended):** start a THROWAWAY read-only API instance against the shadow
delta (+ the live base, read-only) on a NON-production port, run the §4 probe set, then kill it:
```bash
# app.py imports `from store...` -> MUST run from the dev2026/ dir with module `api.app:app`
# (same as bench/bench_bbox_http.py does). NOT port 8035, NOT pm2.
# PID + trap so port 8036 can NEVER stay occupied, even if a probe fails mid-way.
cd $WT
GHRSST_ZARR_PATH=$G/mur.zarr \
  GHRSST_TIMECUBE_PATH=$G/mur_timecube_s8_t90_sh128.zarr \
  GHRSST_DELTACUBE_PATH=$SH/delta_live.zarr \
  GHRSST_SPATIAL_WINDOW_ENFORCE=1 GHRSST_CUBE_REFRESH_TTL_SECONDS=0 \
  $PY -m uvicorn api.app:app --host 127.0.0.1 --port 8036 \
  > $ART/shadow_uvicorn.log 2>&1 &
SHADOW_PID=$!
echo "$SHADOW_PID" > $ART/shadow_uvicorn.pid
trap 'kill $SHADOW_PID 2>/dev/null || true' EXIT INT TERM
sleep 3 && curl -fsS http://127.0.0.1:8036/healthz > $ART/healthz_shadow.json   # readiness gate
```
Probe (save all output to `$ART/probes.txt`): `/healthz` (`delta_latest == max(keep)`,
`delta_day_count == len(keep)`, `spatial_window == [min(keep), max(keep)]`); bbox + POST /points on a
kept day → 200; bbox on a **dropped** day → **400** with `available_spatial_window`; **point GET on the
same dropped day → 200** (base serves it); a multi-day range ending `max(keep)` → `X-Store-Route: cube`.
When done (or on any failure — the trap covers aborts too):
```bash
kill $SHADOW_PID; wait $SHADOW_PID 2>/dev/null; trap - EXIT INT TERM
curl -s http://127.0.0.1:8036/healthz && echo "PORT 8036 STILL OCCUPIED — investigate" || echo "8036 released"
```
Confirm port 8035 was never touched; `$ART/shadow_uvicorn.log` is a required artifact.

**5b. Quiescence posture — documented rehearsal, NOT a production restart:** the production sequence
(S10, if approved) is: `flock` ingest lock → swap → **`pm2 restart ghrsst --update-env`** → `/healthz`
verify → release lock → backup to hold. In S9, rehearse this by **restarting the shadow uvicorn** between
swap and probes (kill + relaunch = process-replacement quiescence, same shape). **Do not `pm2 restart`
the production app** unless Codex explicitly chooses to time a no-op restart as a separate, recorded
decision.

## 6. Staging-prune rehearsal on the daily SUBSET (**[MUT-SHADOW]**; dry-run FIRST, always)

```bash
cd $WT && $PY - <<'EOF'
import json, os, sys
sys.path.insert(0, os.environ["WT"])
from ingest.prune_staging import prune_daily_staging
G, SH, ART = os.environ["G"], os.environ["SH"], os.environ["ART"]
res = prune_daily_staging(os.path.join(SH, "daily_subset.zarr"), os.path.join(SH, "hold"),
                          delta_path=os.path.join(SH, "delta_live.zarr"),
                          base_path=os.path.join(G, "mur_timecube_s8_t90_sh128.zarr"),
                          spatial_window_days=31, staging_buffer_days=7,
                          mode="conservative", dry_run=True, operator="p4s9-codex")
with open(os.path.join(ART, "staging_dryrun.json"), "w") as fh:   # direct json.dump — never tee (a
    json.dump(res, fh, indent=2, sort_keys=True, default=str)     # warning on stdout would corrupt it)
print("status:", res["status"], "| candidates:", len(res["candidates"]),
      "| would-prune:", len(res["pruned"]), "| skipped:", len(res["skipped"]))
EOF
```
(For the real run, same script with `dry_run=False` and the output file changed to
`staging_realrun.json`.)
Review `manifest.dryrun.jsonl`. If (and only if) the dry-run is clean and the candidates make sense,
repeat with `dry_run=False` (**still only mutates the shadow daily subset**) →
`$ART/staging_realrun.json`. Confirm: moved day groups are readable under `$SH/hold/`, one
`status:"moved"` line per day in `$SH/hold/manifest.jsonl` (fsync'd per day), skipped days still present,
`hard_delete == false`.

> **Runtime caveat (same erosion as the compact-free window):** conservative candidates = subset days
> older than the keep window **AND covered by base** (base is fixed through its build date). If the
> 45-day subset's oldest days postdate base coverage at run time, `candidates` comes back **empty** —
> a correct, recorded outcome; to rehearse actual moves, enlarge the subset copy in step 2 to reach
> base-covered days (e.g. latest 60–75 days). Decide from the audit's spans, not from this spec's dates.

## 7. Abort / NO-GO conditions (any of these stops the rehearsal; record which fired)

- recent spatial window has a gap (audit exit 2 / `recent_window_contiguous == false`);
- proposed dropped delta days NOT covered by base (plan `refused`, `base_uncovered` non-empty) — for the
  delta-prune steps only; staging rehearsal may continue;
- `--alarm` fired and the operator chooses not to proceed (free-disk alarm = hard stop);
- free disk below the precondition-2 threshold at ANY point;
- `aborted_stale` from the executor (shadow delta changed under the lock);
- any `verify`/parity failure, `rolled_back`, or `rollback_failed` (the last = investigate before ANY
  further step);
- any manifest inconsistency (missing line for a moved day; a `dry_run:true` line in `manifest.jsonl`);
- any operation targeting a path outside `/home/odbadmin/Data/ghrsst` or the runtime worktree;
- port 8035 / PM2 app `ghrsst` affected by anything other than the (optional, explicitly-chosen) no-op
  restart in 5b.

## 8. Required artifacts (hand back to Claude/orchestrator)

| artifact | from |
|---|---|
| `git_head.txt` (actual commit executed) | §0 |
| `audit.json` + `audit.exit` (rc; alarm block inside the JSON) | step 1.3 |
| `latest_delta_append_tail.txt` (+ any `NO_GO_*.txt`) | step 2 guard A |
| `audit_shadow.json` + `delta_days_{before,after,shadow}_copy.json` | step 2 |
| `prune_plan.json` | step 3 |
| `swap_result.json` (may be `status:"skipped"` — a valid outcome) | step 4 |
| `probes.txt` + `healthz_before.json` + `healthz_shadow.json` + `healthz_after.json` + `shadow_uvicorn.log`/`.pid` | steps 1.1 / 5a / §8 |
| `staging_dryrun.json` / `staging_realrun.json` | step 6 |
| `$SH/hold/manifest.jsonl` + `manifest.dryrun.jsonl` (copies into `$ART`) | steps 4/6 |
| **final PASS / NO-GO summary** (one paragraph per step: pass / fail / skipped-and-why) | operator |

**Final untouched-production check (run last, concrete command):**
```bash
curl -fsS http://127.0.0.1:8035/healthz > "$ART/healthz_after.json"
$PY - <<'EOF'
import json, os
ART = os.environ["ART"]
STABLE = ("delta_latest", "delta_day_count", "spatial_window", "cube_latest_in_sync")
b = json.load(open(os.path.join(ART, "healthz_before.json")))
a = json.load(open(os.path.join(ART, "healthz_after.json")))
diff = {k: {"before": b.get(k), "after": a.get(k)} for k in STABLE if b.get(k) != a.get(k)}
print("STABLE-FIELD MATCH" if not diff else f"STABLE-FIELD MISMATCH: {json.dumps(diff)}")
# rss_mb / pid / queue depth / route_counts are EXCLUDED by design — they legitimately change.
EOF
```
A mismatch is a NO-GO **unless** a scheduled cron append legitimately ran between the two snapshots —
in that case `delta_latest`/`delta_day_count` advance by exactly that append; the operator must add an
explicit note (which cron slot, which day appended) to the PASS/NO-GO summary.

**PASS criteria:** every executed step green (or explicitly skipped via the documented degradation
paths), zero NO-GO conditions fired, all artifacts present (incl. `shadow_uvicorn.log`, `audit.exit`,
`git_head.txt`), and the stable-field comparison above passes (or carries the operator's cron-append
note).

## 9. After S9

PASS → the orchestrator + Codex decide whether to plan **S10** (production rollout: separate approval,
quiescence posture per §5b, hold cleanup ops-only after `hold_until`, and the O4 alarm wired into cron).
NO-GO → hand the artifacts back; Claude patches tooling/specs and S9 re-runs. Either way, **nothing in
S9 authorizes production mutation.**
