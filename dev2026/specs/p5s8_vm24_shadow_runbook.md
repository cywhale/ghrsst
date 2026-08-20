# P5-S8 — VM24 READ-ONLY / shadow evidence runbook (authoring only; NOT approved to run)

**AUTHORING ONLY. NOTHING IN THIS DOCUMENT HAS BEEN EXECUTED.** It was written locally, without
contacting VM24, from the layout recorded in `bench/results/p5s6_vm24_readonly.json`
(read-only, 2026-08-19). Executing it needs a **separate human approval** that the P5-EX-1
roadmap exception does not confer.

**The S7 gate remains `NOT MET`.** This runbook exists because G3, G4, H7 and G16 can only be
settled by production evidence, and it is the instrument that collects it — see
[`p5_critical_path_memo.md`](p5_critical_path_memo.md) for the exception (P5-EX-1) and the
critical path.

## Hard read-only boundary

Every command below is a read. The run performs **no** prune, swap, compaction, repair, hold
deletion, rebuild, production write, deployment, or cron/PM2/systemd/NGINX change. If a step
here appears to require a write, that is a defect in this runbook — **stop and report it**,
do not improvise a write.

The one exception to "touch nothing" is the **shadow copy** in §6, which reads production bytes
and writes only into a scratch path the operator supplies, on a volume that is not the
production data volume. It is still never a write to `$G`.

## 0. Environment

The interpreter and the code live in different trees. They were one variable once, and the
P4-S10 rehearsal found the consequence: a cron that read correctly pointed at a file that did
not exist under that tree, so it never ran.

```bash
export G=/home/odbadmin/Data/ghrsst
export CODE_ROOT=/home/odbadmin/python/ghrsst-p5s8/dev2026        # the P5 code under test
export PYTHON=/home/odbadmin/python/ghrsst-dev2026-phase2/dev2026/.venv/bin/python
export PYTHONPATH=$CODE_ROOT
export RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
export ART=$G/logs/p5s8_$RUN_ID; mkdir -p "$ART"        # logs only; never under a data root
export MANIFEST_ROOT=$G/p5_blocks                       # generation manifests (may not exist yet)
export WAL_ROOT=$G/p5_repairs                           # repair WAL (§7.5a-1)
# ANCHOR_ROOT is deliberately NOT under $G: the WAL freshness anchor exists to survive a
# rollback of the WAL's own host, and VM24 has had two whole-VM snapshot rollbacks. An anchor
# restored alongside the WAL it checks proves nothing.
export ANCHOR_ROOT=${ANCHOR_ROOT:?set ANCHOR_ROOT to an INDEPENDENT rollback domain (§7.5a-2)}
export DEPLOY_SHA=${DEPLOY_SHA:?set DEPLOY_SHA to the exact commit under measurement}
# Option-B only. Leave unset for option A.
export SHADOW_ROOT=${SHADOW_ROOT:-}
```

## 1. Identity preflight — every check is a hard gate

A measurement whose provenance is unknown is not evidence. Each check below **exits non-zero**
rather than printing a warning; the P4-S10 review found a preflight that printed its result and
returned 0, which accepted exactly the configuration it was written to reject.

```bash
ssh -o BatchMode=yes vm24 'date -Is; hostname' || { echo "NO-GO: host unreachable"; exit 1; }

test -d "$CODE_ROOT" || { echo "NO-GO: CODE_ROOT missing: $CODE_ROOT"; exit 1; }
test -x "$PYTHON"    || { echo "NO-GO: PYTHON missing: $PYTHON"; exit 1; }
# The modules this runbook calls must import FROM CODE_ROOT. A runbook that reads correctly and
# cannot import is the failure the P4 rehearsal hit.
PYTHONPATH=$CODE_ROOT "$PYTHON" -c "
import sys, store.block_manifest as bm, store.segmented_cube as sc
assert bm.__file__.startswith('$CODE_ROOT'), bm.__file__
assert sc.__file__.startswith('$CODE_ROOT'), sc.__file__
" || { echo "NO-GO: modules do not import from CODE_ROOT"; exit 1; }

# Exact-SHA pin. `git rev-parse` resolves HEAD, a branch, a tag or an abbreviation, so the
# canonical form must equal what was supplied -- otherwise a movable ref passes as a pin.
cd "$CODE_ROOT/.." && \
  CANON=$(git rev-parse --verify "${DEPLOY_SHA}^{commit}" 2>/dev/null) && \
  [ "$CANON" = "$DEPLOY_SHA" ] || { echo "NO-GO: DEPLOY_SHA is not an exact commit id"; exit 1; }
```

**Anchor domain — NO-GO, not a warning.** `anchor_domain_id()` returns `None` for an undeclared
root and exits 0, so a `print(...) || NO-GO` would accept an undeclared anchor. Assert it:

```bash
PYTHONPATH=$CODE_ROOT "$PYTHON" -c "
import sys
from store import repair_wal as rw
dom = rw.anchor_domain_id('$ANCHOR_ROOT')
assert dom is not None, 'anchor root declares no rollback domain'
root = rw.resolve_anchor_root('$ANCHOR_ROOT')     # refuses a same-filesystem anchor
print('anchor_domain_id', dom); print('anchor_root', root)
" || { echo "NO-GO: anchor domain undeclared or same-filesystem"; exit 1; }
```

**What the anchor preflight can and cannot prove.** Carried forward verbatim from P4-S10,
because it is still exactly true here: *`anchor_root` preflight verifies the configured path,
the declaration, and the local filesystem relationship; it cannot prove that the path is outside
the VM's snapshot/rollback domain.*

**WAL state.** An open repair intent blocks prune by design and never self-resolves. Record it;
do not resolve it here.

```bash
PYTHONPATH=$CODE_ROOT "$PYTHON" -c "
import json
from store import repair_wal as rw
st = rw.pending_state('$WAL_ROOT')            # read-only
print(json.dumps({'wal_repairs_pending': st}, sort_keys=True))
" | tee "$ART/wal_state.json"
```

`wal_repairs_pending` must be recorded as a **number, or an explicit `null` with a reason**.
A missing field and a zero are different states, and an absent field must never read as "clean".

## 2. Capacity preflight (G18)

```bash
df -h  /home/odbadmin/Data | tee "$ART/df_h.txt"
df -i  /home/odbadmin/Data | tee "$ART/df_i.txt"
findmnt -no SOURCE,FSTYPE,TARGET /home/odbadmin/Data | tee "$ART/findmnt.txt"
```

Recorded baseline (2026-08-19): `/dev/sdc1`, ext4, 6.0 T total, **267 GiB free**, 96 % used;
inodes 44 % used with 225,490,340 free.

**G18 gate:** `free_now >= 403.3 GiB` (**600 GiB recommended**). At 267 GiB this is **BLOCKED**,
and S8's own capacity step is expected to report BLOCKED until the volume is expanded.

**`HARD_RESERVE` is not adjusted here, or anywhere.** Lowering it changes the verdict without
changing one byte of free space. If it must change, that is an ops policy decision raised
separately.

## 3. G3 / G4 — production latency

The recorded 2026-08-19 samples are **not** G3 or G4: they were 7 serial samples per window at
concurrency 1, one coordinate, three windows. G3 needs a p95 against the current production
`TieredCube` baseline; G4 needs a **366-day** request.

```bash
# G4: a genuine 366-day span. Confirm it IS 366 days before believing the number.
S=$(date -u -d '366 days ago' +%F); E=$(date -u +%F)
for i in $(seq 1 30); do
  curl -s -o /dev/null -w '%{time_total}\n' \
    "http://127.0.0.1:8080/api/ghrsst?lon0=135&lat0=20&start=$S&end=$E&append=sst"
  sleep 0.3
done | tee "$ART/g4_366d_seconds.txt"
```

- **Rate:** serial, 0.3 s apart. This is a production service; a load generator is not
  authorized here.
- **G3 baseline:** the comparison is against the **current** production `TieredCube` on the same
  coordinate and windows. Until a segmented manifest is deployed there is no segmented arm to
  compare, so **G3 stays `NOT_ADJUDICATED`** and S8 records the baseline half only. Reporting a
  ratio with one arm measured would be the local-to-production promotion this whole phase exists
  to avoid.
- **Percentiles:** 30 serial samples give a usable p50 and a weak p95. Record the sample count
  beside the percentile; a p95 over 7 samples is the second-worst value, not a percentile.

## 4. H7 — inode headroom and sustained fd / RSS / threads

Inode headroom is already **PASS_EVIDENCE_AVAILABLE** from the recorded run: 225,490,340 free
against a +10-year projection of ~4.9 M additional files, roughly 46× headroom. Re-confirm and
record it, then collect what is actually missing — the sustained part:

```bash
PID=$(pgrep -f 'uvicorn|ghrsst' | head -1)
for i in $(seq 1 60); do
  printf '%s %s %s\n' \
    "$(date -u +%s)" \
    "$(ls /proc/$PID/fd | wc -l)" \
    "$(awk '/VmRSS/{print $2}' /proc/$PID/status)"
  sleep 5
done | tee "$ART/h7_fd_rss_timeseries.txt"
```

**Sustained under what load?** H7 asks for fd/RSS at concurrency 8 and 16. S8 is read-only and
must not generate that load against production. So S8 records the **at-rest and natural-traffic**
time series and H7's sustained-load half stays `NOT_ADJUDICATED` until S9 can run it against a
**shadow copy**. Sampling an idle process for five minutes and calling it a sustained-load
result would be the same category error as the withdrawn P5-S6 measurements.

Judge growth on the **plateau**, not against the first sample: a worker pool sizing itself once
is not a leak, and the local S7 harness reported exactly that as growth before it was corrected.

## 5. G16 — metadata scan and snapshot-open

```bash
# Wall time of a metadata scan. Nice'd and ionice'd: this walks a 5.4 T tree on a live service.
/usr/bin/time -v nice -n 19 ionice -c3 \
  "$PYTHON" -c "
import os, sys, time
t0 = time.perf_counter(); files = dirs = 0
for _, d, f in os.walk('$G'):
    dirs += len(d); files += len(f)
print(f'{files} files, {dirs} dirs, {time.perf_counter()-t0:.1f} s')
" 2>&1 | tee "$ART/g16_metadata_scan.txt"
```

**Abort this step** if service p95 rises above twice its pre-scan value, or if load average
exceeds 4. The recorded baseline is load 0.10 on a 5-day-uptime host; a walk of 5.4 T is the one
step here with real service impact, and it is the reason this scan is nice'd, ionice'd, and
abortable.

**Snapshot-open cost** needs a deployed segmented manifest. There is none on VM24
(`p5_roots_present: false` in the recorded evidence), so `SegmentedCubeStore` construction
cannot be timed against production, and **G16 stays `NOT_ADJUDICATED`** on that half. Record the
scan; do not synthesise the rest.

## 6. Shadow copy (option B for evidence, not for production)

Only if the operator supplies `SHADOW_ROOT` **on a volume that is not the production data
volume**, and only to collect the evidence §4 and §5 cannot take from production:

```bash
test -n "$SHADOW_ROOT" || { echo "SKIP: no SHADOW_ROOT supplied"; exit 0; }
# Refuse a shadow root on the production device: a shadow that shares the volume competes for
# the very free space G18 is blocked on, and a fill-up there is a production outage.
PROD_DEV=$(findmnt -no SOURCE --target /home/odbadmin/Data)
SHAD_DEV=$(findmnt -no SOURCE --target "$SHADOW_ROOT")
[ "$PROD_DEV" != "$SHAD_DEV" ] || { echo "NO-GO: SHADOW_ROOT is on the production device"; exit 1; }
rsync -a --info=progress2 "$G/mur_timecube_s8_t90_sh128.delta.zarr" "$SHADOW_ROOT/"
```

Everything downstream of this point runs **against `$SHADOW_ROOT`**, never `$G`.

## 7. Abort conditions

Stop immediately, record what has been collected, and hand back. Do not "finish the run first".

| condition | why it aborts |
|---|---|
| any command would write under `$G` | the read-only boundary is the whole basis of this step |
| `/healthz` stops returning `ok`, or `pm2` reports the API not `online` | a read-only run must not coincide with a service problem, whoever caused it |
| load average > 4, or service p95 > 2× its pre-step baseline | §5's scan is the one step with real impact |
| free space falls below 250 GiB (`HARD_RESERVE`) at any sample | the host is closer to the edge than the plan assumed |
| the anchor domain check fails, or `DEPLOY_SHA` is not an exact commit | provenance is unknown, so nothing collected is evidence |
| an open repair intent is found in the WAL | record it and stop; resolving it is an ops decision, never a step here |
| the host is unstable or unreachable at any point | mark **BLOCKED**, exactly as the 2026-08-19 run was prepared to |
| a shadow root resolves onto the production device | it would consume the free space G18 is blocked on |

## 8. Artifacts and provenance

Every file lands under `$ART` (`$G/logs/p5s8_$RUN_ID/`) — a log path, never under a data root.

| artifact | content |
|---|---|
| `preflight.json` | every field in §9 |
| `wal_state.json` | `wal_repairs_pending`, number or explicit `null` + reason |
| `df_h.txt`, `df_i.txt`, `findmnt.txt` | capacity and filesystem identity |
| `g4_366d_seconds.txt` | per-sample timings, with the sample count |
| `h7_fd_rss_timeseries.txt` | fd and RSS over time, with the sampling interval |
| `g16_metadata_scan.txt` | file/dir counts and wall time |
| `p5s8_readonly.json` | the assembled report |

Every record carries `label: VM24_PRODUCTION_READ_ONLY`. Local synthetic numbers are **never**
merged into these files, and no number here may be promoted into a local artifact.

## 9. Required preflight fields

A run missing any of these is not evidence and must be re-run:

`run_id`, `host`, `measured_utc`, `deploy_sha`, `code_root`, `python`, `pythonpath`,
`anchor_root`, `anchor_domain_id`, `wal_root`, `wal_repairs_pending`, `manifest_root`,
`manifest_generation`, `filesystem_device`, `filesystem_fstype`, `free_now_bytes`,
`inodes_free`, `label`.

The list is enforced from `ops/p5_roadmap.py::S8_PREFLIGHT_FIELDS`, and a test compares this
section against it — so a field dropped from either side fails rather than going unnoticed.

## 10. What this runbook can and cannot settle

| gate | after a successful S8 run |
|---|---|
| **G18** | measurable directly; expected **BLOCKED** until the volume is expanded |
| **H7** inode headroom | adjudicable |
| **H7** sustained fd/RSS | **NOT_ADJUDICATED** — needs load S8 must not generate against production |
| **G3** | **NOT_ADJUDICATED** — no segmented base is deployed, so there is no segmented arm |
| **G4** | measurable as a baseline; the production p95 needs more samples than a read-only pass should take |
| **G16** metadata scan | measurable |
| **G16** snapshot-open | **NOT_ADJUDICATED** — no deployed segmented manifest to open |

**No S8 result marks the S7 gate PASS.** S8 collects evidence; adjudication is step 6 of the
critical path, and S10 needs its own fresh written approval regardless.
