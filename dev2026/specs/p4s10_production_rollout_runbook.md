# P4-S10 — PRODUCTION rollout runbook (Codex / ops execution; SEPARATE HUMAN APPROVAL REQUIRED)

**Claude authored this; Codex/ops EXECUTE it on VM24 — and ONLY after the orchestrator's explicit,
written approval for THIS run.** This is the **first production mutation** of the P4 program: it
activates the rehearsal-proven pipeline (S9 PASS, `RUN_ID 20260709T065917Z`) against the **live** delta
cube and the **live** daily staging store.

## 1. Scope / boundary (hard)

- **S10 executes ONLY with a fresh, explicit human approval** (orchestrator + Codex sign-off recorded in
  the operator summary). An S9 PASS does **not** authorize S10; this document does **not** authorize S10.
- Operation tags used below:
  | tag | meaning |
  |---|---|
  | **[RO]** | read-only — no store/system mutation |
  | **[MUT-STG]** | writes a NEW staging path beside the live store; live untouched |
  | **[MUT-SWAP]** | atomic swap of the live delta (old delta preserved as backup) |
  | **[MUT-LIVE]** | touches the live serving process (PM2 stop/start of `ghrsst`) |
  | **[MUT-HOLD]** | moves live data into the hold area — **never deletes** |
- **Never touched:** NGINX, unrelated projects/processes/files, the base cube (read-only throughout),
  crontab entries other than the single O4-alarm line in §6 (itself an ops change recorded in the
  summary).
- **Hard delete is NEVER automated.** Everything mutating is move-to-hold with `hold_until = +14 d`;
  hard delete is a later, manual, ops-only action (§9). **S10 itself frees NO disk** — space is
  reclaimed only when ops hard-deletes expired hold entries after `hold_until`.
- All paths live under `/home/odbadmin/Data/ghrsst` (`$G`) or the runtime worktree; anything else in a
  command is an abort.

Environment (run first):
```bash
export G=/home/odbadmin/Data/ghrsst
export WT=/home/odbadmin/python/ghrsst-dev2026-phase2/dev2026
export PY=$WT/.venv/bin/python
export RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
export ART=$G/logs/p4s10_$RUN_ID; mkdir -p "$ART"
export HOLD=$G/hold; mkdir -p "$HOLD"
export LIVE_DELTA=$G/mur_timecube_s8_t90_sh128.delta.zarr
export NEW_DELTA=$G/mur_timecube_s8_t90_sh128.delta.zarr.new-$RUN_ID   # staging, SAME filesystem
export LOCK=$G/mur_delta.ingest.lock                                   # shared ingest lock (see §3)
```
Code: branch `dev2026-p4-s8-swap-design` tip; `git merge-base --is-ancestor 2fa8b43 HEAD` must pass;
record `git rev-parse HEAD > $ART/git_head.txt`. Checking out the worktree does NOT restart the app.

## 2. Fresh-audit precondition (**[RO]** — the gate for everything below)

**Never reuse S9 (or any earlier) day sets** — the compact-free window and the staging keep-set both
erode daily. Immediately before execution:
```bash
cd $WT
curl -fsS http://127.0.0.1:8035/healthz > "$ART/healthz_before.json"
set +e
$PY ops/p4_retention_audit.py \
  --daily $G/mur.zarr --base $G/mur_timecube_s8_t90_sh128.zarr --delta $LIVE_DELTA \
  --healthz-url http://127.0.0.1:8035 \
  --alarm --strict --json-out "$ART/audit.json"
AUDIT_RC=$?; set -e; echo "$AUDIT_RC" > "$ART/audit.exit"; echo "audit rc=$AUDIT_RC"
```
| condition | action |
|---|---|
| rc=2 (recent window NOT contiguous / duplicate days / **API metadata stale**) | **NO-GO — stop everything** |
| rc=3 with a **free-disk** reason | **NO-GO** (below margin) |
| rc=3 with only a delta-span reason | operator decision, recorded; §4 may still proceed |
| `delta_prune.blocked_need_compaction_first` **non-empty** | **delta prune/swap (§4) is NO-GO or degraded**: only the days in `delta_prune.delta_prune_candidates` may be dropped; if that list is empty, **skip §4 entirely** and (if approved) run only §5 staging prune |
| `staging_keep.blocked_by_recent_window*` true | §5 is NO-GO too |
| rc=0, candidates present | proceed |

The audit's `delta_prune.keep_window_start` / candidate lists are the ONLY source of this run's day
sets. Free disk must also satisfy: ≥ (live delta size × 2) + 100 GB (staging rebuild + margin), checked
with `df -h $G; du -sh $LIVE_DELTA` → `$ART/disk_before.txt`.

## 3. Cron windows / ingest lock / quiescence (posture — read before §4)

- **Schedule strictly outside the Asia/Taipei cron windows** (daily ingest 05:05 / 18:05; delta append
  07:30 / 20:30 local), with a 30–60 min margin on both sides — the whole §4 sequence must fit.
- **Append-log completion check** (as in S9 step 2 guard A): newest `$G/logs/delta_append/` log must
  show its completion marker → `$ART/latest_delta_append_tail.txt`; missing → NO-GO.
- **Ingest lock — FAIL CLOSED (Codex S10-review #3):** the executor takes `flock` on `$LOCK` for the
  entire §4 sequence, AND one of the following must hold **before** §4 starts (record which in the
  summary):
  - **(a)** the delta-append cron wrapper honors the SAME lock file (`flock -n "$LOCK"` guard —
    confirmed by inspection, or added as a small recorded ops change); **or**
  - **(b)** the delta-append cron entries are **temporarily disabled** for the S10 window (crontab
    comment-out, recorded, **re-enabled and verified in the summary before closing the run**).
  **Schedule separation alone is NOT the normal safe path** — it is an explicit operator override that
  must be justified and recorded as such.
- **Staleness guard:** the executor re-reads live `attrs["days"]` **inside the lock** and requires
  unique days + exact `set(keep) ∪ set(dropped)` equality with the plan — any drift → `aborted_stale`
  (nothing touched; rebuild the plan from a fresh audit).
- **Production quiescence = STOP SERVING BEFORE THE PATH SWITCH (Codex S10-review #1).** The required
  sequence is:
  **lock → staleness guard → `pm2 stop ghrsst` → verify no worker is serving (port down) → swap →
  `pm2 start ghrsst` → healthz verify → API probes → release lock.**
  This is **enforced by the executor itself** via its `pre_swap_quiesce_fn` hook (quiesce runs inside
  the SAME critical section as the staleness guard and the rename — an external `pm2 stop` before the
  executor call would sit outside the lock, and wrapping our own flock would deadlock the executor's
  nested acquire). Tested: `tests/test_phase2_p4s8a.py::TestPreSwapQuiesce` (ordering, stale-plan
  never quiesces, quiesce-failure touches nothing).
- **PM2 lifecycle preflight (Codex S10-review #3, [MUT-LIVE], inside the approved window, BEFORE §2):**
  verify that plain `pm2 stop ghrsst` → `pm2 start ghrsst` restores the saved process definition
  (env/cwd/args) — a no-op rehearsal with a few seconds of downtime, NO swap involved:
  ```bash
  pm2 describe ghrsst > "$ART/pm2_describe_before.txt"
  pm2 stop ghrsst && sleep 2 && pm2 start ghrsst
  for i in $(seq 1 45); do sleep 2; curl -fsS http://127.0.0.1:8035/healthz > "$ART/healthz_preflight.json" && break; done
  pm2 describe ghrsst > "$ART/pm2_describe_after.txt"
  diff <(grep -E "script|cwd|exec|interpreter|args" "$ART/pm2_describe_before.txt") \
       <(grep -E "script|cwd|exec|interpreter|args" "$ART/pm2_describe_after.txt") && echo "pm2 lifecycle OK"
  ```
  `healthz_preflight.json` stable fields must match `healthz_before`-to-be (same delta view). Any
  difference in definition or served view → **NO-GO before anything mutating starts**. Never
  `--update-env`.
  - **`pm2 restart` AFTER the swap is REJECTED for production**: during PM2's graceful-shutdown/kill
    window an in-flight request in an old worker still holds its pre-swap `_Meta` and can read
    **through the already-switched path** → the exact silent old-meta × new-bytes corruption the S8a
    proof test demonstrated. Quiescence must be COMPLETE before the rename, not merely follow it.
    *(This supersedes the looser "restart-under-lock" wording in the S8 design §3 step 7 — the intent
    was always "no reader alive across the switch"; stop→swap→start is what actually delivers it.)*
  - TTL refresh and/or an admin-refresh endpoint are likewise **NOT** acceptable as production
    quiescence on their own (same verdict).
  - Cost: a few seconds of visible 503s (NGINX upstream down) during stop→start, run off-peak. If a
    lower-downtime path is ever wanted, that requires a **real serving-disable/drain design** (its own
    spec + review) — not restart-after-swap.
- **PM2 env hygiene (Codex S10-review #2):** S10 changes NO environment. Use `pm2 stop ghrsst` /
  `pm2 start ghrsst` (the saved process definition + saved env). **Never pass `--update-env` from an
  ad-hoc shell** — it can silently replace the production env with the operator's shell env. If a
  manual recovery restart is ever needed, it is likewise plain `pm2 restart ghrsst` (no flags), or —
  only if the saved definition itself is suspected broken — the documented production launcher
  (`/home/odbadmin/python/ghrsst/conf/start_app.sh`) per the README rollback procedure.

## 4. Delta prune — LIVE sequence

### 4a. Build the plan to a STAGING path (**[MUT-STG]**; live untouched)
```bash
cd $WT && $PY - <<'EOF' > $ART/prune_step.log 2>&1
import os, sys
sys.path.insert(0, os.environ["WT"])
import zarr, json
from ingest.prune_delta import prune_delta
G, ART = os.environ["G"], os.environ["ART"]
live, new = os.environ["LIVE_DELTA"], os.environ["NEW_DELTA"]
with open(os.path.join(ART, "audit.json")) as fh:
    audit = json.load(fh)
dp = audit["delta_prune"]
keep_start = dp["keep_window_start"]                       # FRESH audit is the only day-set source
dd = sorted(zarr.open_group(live, mode="r").attrs["days"])
keep = [d for d in dd if d >= keep_start]
base_days = list(zarr.open_group(os.path.join(G, "mur_timecube_s8_t90_sh128.zarr"), mode="r").attrs["days"])
plan = prune_delta(live, new, keep, source_daily=os.path.join(G, "mur.zarr"), source_delta=live,
                   base_days=base_days, spatial_window_days=31,
                   audit_recent_window=audit["recent_spatial_window"],   # corroboration, never override
                   engine="bulk", artifacts_dir=ART, workers=4)
print("status:", plan["status"], "| keep:", len(plan.get("keep_days", [])),
      "| dropped:", plan.get("dropped_days"), "| reason:", plan.get("reason"))
EOF
tail -3 $ART/prune_step.log
```
- Success == **`$ART/prune_plan.json` exists** (the tool writes it ONLY after green validation);
  `prune_delta_progress.jsonl` is fsync'd per event; any exception → `prune_delta_error.json`.
- Monitor: `tail -f $ART/prune_delta_progress.jsonl`. If the process dies: **resume is only valid under
  the SAME code version** — rerun with `resume=True`. **After any code fix, never resume**: delete only
  `$NEW_DELTA` and rebuild (the 2026-07-09 lesson).
- Plan `refused` (e.g. base-coverage) → §4 is over (degradation per §2 table); continue at §5 if
  approved.

### 4b. Quiesce (pm2 stop) → swap → pm2 start, under the lock (**[MUT-SWAP] + [MUT-LIVE]**) → verify → hold
```bash
cd $WT && $PY - <<'EOF' > $ART/swap_step.log 2>&1
import json, os, subprocess, sys, time, urllib.request
sys.path.insert(0, os.environ["WT"])
from ingest.swap_delta import execute_swap_plan
ART, HOLD, LOCK = os.environ["ART"], os.environ["HOLD"], os.environ["LOCK"]
HZ = "http://127.0.0.1:8035/healthz"
with open(os.path.join(ART, "prune_plan.json")) as fh:
    plan = json.load(fh)

def serving() -> bool:
    try:
        urllib.request.urlopen(HZ, timeout=3); return True
    except Exception:
        return False

def start_and_wait():                              # executor refresh_fn: START (saved env; NO --update-env)
    subprocess.run(["pm2", "start", "ghrsst"], check=True)
    for _ in range(45):
        time.sleep(2)
        if serving():
            return
    raise RuntimeError("app did not come back after pm2 start")     # -> executor rolls back under lock

def healthz_verifier(live_path, keep_sorted):      # extra verifier: the SERVED view must match the plan
    hz = json.loads(urllib.request.urlopen(HZ, timeout=10).read())
    ok = (hz.get("delta_latest") == keep_sorted[-1]
          and hz.get("delta_day_count") == len(keep_sorted)
          and hz.get("spatial_window") == [keep_sorted[0], keep_sorted[-1]]
          and hz.get("cube_latest_in_sync") is True)
    return {"ok": ok, "healthz": {k: hz.get(k) for k in
            ("delta_latest", "delta_day_count", "spatial_window", "cube_latest_in_sync")}}

def quiesce():                                     # pre_swap_quiesce_fn: runs INSIDE the executor lock,
    subprocess.run(["pm2", "stop", "ghrsst"], check=True)   # after staleness guard, BEFORE any rename
    for _ in range(30):
        time.sleep(1)
        if not serving():
            print("quiesced: port 8035 down, no worker serving"); return
    raise RuntimeError("app still serving after pm2 stop")  # -> executor returns quiesce_failed,
                                                            #    NOTHING touched

def ensure_serving():
    if serving():
        return True
    subprocess.run(["pm2", "start", "ghrsst"])              # plain start; saved env
    for _ in range(45):
        time.sleep(2)
        if serving():
            return True
    return False

# The quiescence + swap share ONE critical section: the executor's own lock (an external pm2-stop
# before this call would sit OUTSIDE the lock, and wrapping our own flock would deadlock its nested
# acquire). Order inside: lock -> staleness -> prechecks -> quiesce() -> swap -> start_and_wait() ->
# verifiers -> release.
try:
    res = execute_swap_plan(plan, mode="s2", hold_dir=HOLD, lock_path=LOCK,
                            pre_swap_quiesce_fn=quiesce, refresh_fn=start_and_wait,
                            verifier=healthz_verifier, operator="p4s10-codex")
except Exception as exc:                           # an exception ESCAPING the executor after quiesce =
    import traceback                               # unknown on-disk state -> FAIL CLOSED: app stays down
    print("AMBIGUOUS: executor raised:", exc); traceback.print_exc()
    print("APP LEFT STOPPED DELIBERATELY — do not start until on-disk state is verified (§9). "
          "Live delta and hold backup must be inspected by a human before serving resumes.")
    raise SystemExit(2)

# ---- CONDITIONAL recovery (Codex S10-review #2): auto-start ONLY in provably-safe states ----
SAFE = (res["status"] in ("refused", "quiesce_failed", "aborted_stale")            # nothing touched
        or (res["status"] == "swapped" and res.get("verify", {}).get("ok"))        # swapped + verified
        or (res["status"] == "rolled_back" and res.get("restored_ok") is True))    # restored + verified
if SAFE:
    if not ensure_serving():
        print("CRITICAL: safe on-disk state but app did not come back — manual `pm2 start ghrsst`")
else:
    # rollback_failed, rolled_back w/o restored_ok, swapped w/o verify: on-disk state NOT verified.
    print(f"FAIL-CLOSED: status={res['status']} — APP LEFT STOPPED (maintenance/503 posture). "
          "Do NOT start the app: it could serve corrupt/wrong data. Human recovery per §9, "
          "then start manually and re-run §7 probes.")

with open(os.path.join(ART, "swap_result.json"), "w") as fh:
    json.dump(res, fh, indent=2, sort_keys=True, default=str)
print(res["status"], "| verify.ok:", res.get("verify", {}).get("ok"), "| backup:", res.get("backup"))
EOF
tail -6 $ART/swap_step.log
```
Sequence semantics — **ONE critical section, enforced by the executor itself** (`pre_swap_quiesce_fn`
hook; Codex S10-review #1): lock → staleness guard → same-fs prechecks (staging AND `$HOLD` vs live) →
**`quiesce()` = `pm2 stop` + port-down verify** → double rename → `refresh_fn` = **`pm2 start` +
healthz-up wait** → local verifier + the healthz verifier above → release. A stale plan aborts **before**
quiesce (the app is never stopped for a doomed swap); a quiesce failure returns `quiesce_failed` with
**nothing touched**. On verify failure/exception the executor **rolls back under the still-held lock**
(files restored; rollback re-invokes `refresh_fn`, i.e. starts the app on the restored delta). Old delta
lands in `$HOLD/...pre-prune-<swap_id>` with a `delta_prune_swap` manifest line (`hold_until` +14 d,
hard delete ops-only). No reader is alive across the rename — that is the whole point.

**App recovery is CONDITIONAL, never unconditional (Codex S10-review #2):** the wrapper auto-starts the
app ONLY in provably-safe states — `refused` / `quiesce_failed` / `aborted_stale` (nothing touched),
`swapped` + `verify.ok`, `rolled_back` + `restored_ok`. In **ambiguous states** (`rollback_failed`,
`rolled_back` without `restored_ok`, an exception escaping the executor after quiesce) the app is
**deliberately LEFT STOPPED** (maintenance/503 posture): starting it could serve corrupt or wrong data.
Human recovery per §9 first, manual `pm2 start ghrsst` after the on-disk state is verified, then §7
probes.
| `swap_result.status` | app | action |
|---|---|---|
| `swapped` + `verify.ok` | auto-started | continue to §7 probes |
| `quiesce_failed` | auto-start attempted (nothing touched) | NO-GO for this window; check PM2 state (`pm2 ls`), fix, retry in a new window |
| `aborted_stale` | never stopped (quiesce not reached) | nothing touched; NO-GO for this window; rebuild plan from a fresh audit |
| `rolled_back` + `restored_ok: true` | auto-started (safe: restore verified) | NO-GO; investigate with the artifacts; confirm `/healthz` stable fields match `healthz_before.json` |
| `rolled_back` + `restored_ok: false` | **LEFT STOPPED (fail-closed)** | restoration NOT verified → treat as ambiguous: human inspects live delta vs `backup` (§9) before any start |
| `rollback_failed` (or an exception escaping the executor after quiesce) | **LEFT STOPPED (fail-closed)** | **AMBIGUOUS STATE — hard NO-GO**: do not retry, do NOT start the app (it could serve corrupt/wrong data); recover manually from `backup` (§9); start + §7 probes only after a human verifies the on-disk state; keep the lock file in place until resolved |

## 5. Daily staging prune — LIVE sequence (**[MUT-HOLD]**; independent of §4's outcome)

Conservative mode, **dry-run first, always**; real run only if the dry-run is clean and reviewed.
```bash
cd $WT && $PY - <<'EOF' > $ART/staging_step.log 2>&1
import json, os, sys
sys.path.insert(0, os.environ["WT"])
from ingest.prune_staging import prune_daily_staging
G, ART, HOLD = os.environ["G"], os.environ["ART"], os.environ["HOLD"]
res = prune_daily_staging(os.path.join(G, "mur.zarr"), HOLD,
                          delta_path=os.environ["LIVE_DELTA"],
                          base_path=os.path.join(G, "mur_timecube_s8_t90_sh128.zarr"),
                          spatial_window_days=31, staging_buffer_days=7,
                          mode="conservative", dry_run=True, operator="p4s10-codex",
                          lock_path=os.environ["LOCK"])
with open(os.path.join(ART, "staging_dryrun.json"), "w") as fh:
    json.dump(res, fh, indent=2, sort_keys=True, default=str)
print("status:", res["status"], "| candidates:", len(res.get("candidates", [])),
      "| would-prune:", len(res.get("pruned", [])), "| skipped:", len(res.get("skipped", [])))
EOF
tail -2 $ART/staging_step.log
```
- Review `manifest.dryrun.jsonl` + the counts. **Note the scale:** conservative candidates on the full
  production daily store = every base-covered day older than the keep window — potentially **>1000 day
  groups** on the first run. **Recommended pilot:** for the first real run, enlarge
  `staging_buffer_days` so only the oldest ~30–60 days fall outside the keep window (compute from the
  audit's daily span: `buffer ≈ (anchor − oldest_day_to_keep) − 31`), verify hold/manifest behavior at
  that scale, then step the buffer down toward 7 in later runs. Record the chosen value.
- Real run: same script with `dry_run=False` → `$ART/staging_realrun.json`. Per-day **fsync'd**
  manifest lines land in `$HOLD/manifest.jsonl` as each group moves; a mid-run failure leaves every
  moved day recorded; **skipped days remain in daily staging** (their reasons are in the result);
  nothing is ever deleted.
- `hard_delete` stays ops-only after each entry's `hold_until` (§9).

## 6. O4 defer-with-alarm cron (**[RO] tool; ops installs the cron line**)

```cron
# O4 alarm (P4-S8b): daily, OUTSIDE ingest windows (Asia/Taipei). Exit 3 == alarm.
40 09 * * * /home/odbadmin/python/ghrsst-dev2026-phase2/dev2026/.venv/bin/python \
  /home/odbadmin/python/ghrsst-dev2026-phase2/dev2026/ops/p4_retention_audit.py \
  --daily /home/odbadmin/Data/ghrsst/mur.zarr \
  --base  /home/odbadmin/Data/ghrsst/mur_timecube_s8_t90_sh128.zarr \
  --delta /home/odbadmin/Data/ghrsst/mur_timecube_s8_t90_sh128.delta.zarr \
  --alarm --json-out /home/odbadmin/Data/ghrsst/logs/o4_alarm/alarm_$(date -u +\%Y\%m\%d).json \
  || <ops alerting hook>
```
- **Exit-code policy:** 0 = quiet; **3 = alarm fired** → the `|| <ops alerting hook>` (mail/notify —
  ops' existing mechanism) MUST page a human; 2 cannot occur without `--strict` (do not add it here —
  the alarm cron is a monitor, not a gate).
- **The alarm NEVER prunes/compacts/mutates** — it only notifies. Response to an alarm is a human
  decision (provision disk → unlock O1/O3, fund the O2 §6.1 design, or schedule another S10-style
  prune run).

## 7. API probes / gates (**[RO]**; after §4b success)

**Hard gate = HTTP status ONLY. `X-Store-Route` is record-only, never a gate.** bbox/point day
selection **MUST use `start=<day>&end=<day>` — never `date=`** (the GET endpoint silently ignores
unknown params → false-200 trap; `date` exists only in the POST /points body). With
`KEPT = min(keep_days)` (oldest kept), `LATEST = max(keep_days)`, `DROPPED = a just-dropped day`:
```bash
B=http://127.0.0.1:8035/api/ghrsst
{
curl -s -o /dev/null -w "kept_bbox %{http_code}\n"        "$B?start=$KEPT&end=$KEPT&lon0=135&lat0=15&lon1=136&lat1=16&append=sst"
curl -s -w "\ndropped_bbox %{http_code}\n"                "$B?start=$DROPPED&end=$DROPPED&lon0=135&lat0=15&lon1=136&lat1=16&append=sst"
curl -s -o /dev/null -w "dropped_pointGET %{http_code}\n" "$B?start=$DROPPED&end=$DROPPED&lon0=135&lat0=15&append=sst"
curl -s -o /dev/null -w "point_range %{http_code}\n"      "$B?start=$DROPPED&end=$LATEST&lon0=135&lat0=15&append=sst"
curl -s -o /dev/null -X POST -H 'Content-Type: application/json' -w "POST_kept %{http_code}\n" \
  -d "{\"date\":\"$LATEST\",\"points\":[[135,15],[136,16]],\"append\":\"sst\"}" "$B/points"
curl -s -X POST -H 'Content-Type: application/json' -w "\nPOST_dropped %{http_code}\n" \
  -d "{\"date\":\"$DROPPED\",\"points\":[[135,15]],\"append\":\"sst\"}" "$B/points"
curl -s -D - -o /dev/null "$B?start=$DROPPED&end=$LATEST&lon0=135&lat0=15&append=sst" | grep -i "x-store-route" || true   # RECORD-ONLY
curl -fsS http://127.0.0.1:8035/healthz
} > "$ART/probes.txt" 2>&1
```
**Required (each is a hard gate):** kept bbox **200**; dropped bbox **400** with
`available_spatial_window` in the body; dropped point GET **200**; point range **200**; POST kept
**200**; POST dropped **400**; `/healthz` shows `delta_day_count == len(keep)`,
`spatial_window == [min(keep), max(keep)]`, `delta_latest == max(keep)`, `cube_latest_in_sync == true`
(→ `$ART/healthz_after.json`). Any gate failing after a `swapped` result = **verify-fail → rollback per
§4b** (if the lock was already released: re-acquire it, quiesce with `pm2 stop ghrsst`, rollback via the hold backup, `pm2 start ghrsst`, re-verify).

## 8. Artifacts (all under `$ART = $G/logs/p4s10_<RUN_ID>/`)

`git_head.txt` · `pm2_describe_before/after.txt` + `healthz_preflight.json` (§3 preflight) ·
`healthz_before.json` / `healthz_after.json` · `audit.json` + `audit.exit` ·
`disk_before.txt` · `latest_delta_append_tail.txt` · `prune_step.log` + `prune_delta_progress.jsonl`
(+ `prune_delta_error.json` on failure) · `prune_plan.json` (success only) · `swap_step.log` +
`swap_result.json` · `probes.txt` · `staging_step.log` + `staging_dryrun.json` / `staging_realrun.json`
· copies of `$HOLD/manifest.jsonl` lines this run added · **operator summary**:

```
P4-S10 SUMMARY — RUN_ID: ............  operator: ............  approval ref: ............
step 2 fresh audit: rc=.. / window contiguous Y/N / blocked_need_compaction_first: [..] / decision: ..
step 4a plan: status .. / keep .. days / dropped [..] / (or SKIPPED because ..)
step 4b swap: status .. / verify.ok .. / posture: stop-swap-start (REQUIRED; restart-after-swap is rejected) / backup: ..
step 3 cron guard: flock-in-wrapper | cron-disabled(+re-enabled Y/N) | schedule-separation-OVERRIDE(justification: ..)
step 5 staging: dryrun candidates .. -> realrun moved .. skipped .. (buffer used: ..) / (or NOT RUN)
step 6 alarm cron: installed Y/N (line recorded above)
step 7 probes: 7/7 hard gates green Y/N (list any failure)
VERDICT: PASS / NO-GO (which condition fired: ..............................)
```

## 9. Rollback / abort (consolidated)

**Abort conditions** (any → stop; record which): audit rc=2; free-disk alarm; recent-window hole;
`blocked_need_compaction_first` non-empty (for §4); append log without completion marker; inside a cron
window; `aborted_stale`; plan `refused`/`invalid`/`error`; `rolled_back`; **`rollback_failed`
(ambiguous — hard stop, manual recovery)**; any §7 hard gate failing; any command targeting a path
outside `$G`/worktree.

**Recovery ladder:**
1. `rolled_back` — live already restored + re-verified by the executor; nothing further to recover.
2. Defect discovered AFTER a successful swap, within `hold_until`: the pre-swap delta is intact in
   `$HOLD` — reverse-swap it back (same mechanics under the lock: quiesce-stop → reverse swap → start, §7 probes),
   new manifest line records the reversal.
3. Staging-prune regret within `hold_until`: move the day group back from `$HOLD` to its
   `$G/mur.zarr/YYYY/MM/DD` path (manifest records both paths), verify with the audit tool.
4. Beyond `hold_until` (only if ops already hard-deleted): re-derive per P4-S4 §10 — base+delta serve
   point/range; NetCDF redownload is the deep backstop.

**No cleanup command in this runbook deletes live or hold data — there is none.** Hold entries expire
into *eligibility* for manual ops deletion after their `hold_until`; that action is outside S10 and
needs its own ops decision.

---
*Docs-only. Nothing in this file has been executed. Execution requires the §1 approval, a fresh §2
audit, and Codex/ops at the keyboard — Claude never touches VM24.*
