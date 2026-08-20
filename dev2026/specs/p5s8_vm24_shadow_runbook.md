# P5-S8 — VM24 READ-ONLY / shadow evidence runbook (authoring only; NOT approved to run)

**AUTHORING ONLY. NOTHING IN THIS DOCUMENT HAS BEEN EXECUTED.** It was written locally, without
contacting VM24, from the layout recorded in `bench/results/p5s6_vm24_readonly.json`
(read-only, **2026-08-19 — historical**). Executing it needs a **separate human approval** that
the P5-EX-1 roadmap exception does not confer.

**The S7 gate remains `NOT MET`.** This runbook exists because G3, G4, H7 and G16 can only be
settled by production evidence, and it is the instrument that collects it — see
[`p5_critical_path_memo.md`](p5_critical_path_memo.md).

## Hard read-only boundary

Every command below is a read **of `$G`**. The run performs **no** prune, swap, compaction,
repair, hold deletion, rebuild, production write, deployment, or cron/PM2/systemd/NGINX change.
If a step here appears to require a write under `$G`, that is a defect in this runbook — **stop
and report it**, do not improvise a write.

**Artifacts are written outside `$G`, and outside the production filesystem.** The first draft
of this runbook put them in `$G/logs/...` and created them with `mkdir -p` and `tee` — a write
to the production data root, in a document whose opening line promises none. Worse, it consumed
the very free space G18 is blocked on. `$ART_ROOT` is operator-supplied and verified in §0.

The only other thing that writes anywhere is the **shadow copy** in §7, into a scratch path the
operator supplies. It is still never a write to `$G`.

## Execution host

Every command block is prefixed **Run on: LOCAL** or **Run on: VM24**. They are different
machines with different paths, and a `localhost` API call means *localhost of VM24*. A runbook
that mixes the two silently is one someone runs in the wrong place.

## Profiles — pick one before starting

The host today has **no P5 roots** (`p5_roots_present: false` in the recorded evidence). A
runbook that demands `ANCHOR_ROOT`, WAL state and a manifest generation before collecting
*legacy* baselines is unrunnable on the only host available.

| | **`legacy_baseline`** | **`p5_shadow`** |
|---|---|---|
| **Where** | the current production monolith deployment | a shadow copy with P5 roots deployed |
| **Collects** | G4 span timing, G16 metadata scan, G18 capacity, H7 inode headroom | everything the legacy profile cannot reach |
| **P5 roots** | not required | required |
| **May adjudicate** | nothing on its own | nothing on its own |
| **May NOT adjudicate** | `G3`, `G16_snapshot_open`, `H7_sustained_load` | `G3_production_baseline` — a shadow host is not the production service |

Fields that do not apply to the chosen profile are recorded as
**`not_applicable_with_reason`**, never omitted. A missing field reads as clean; *"does not
apply, because there is no WAL"* reads as what it is. For `legacy_baseline` those are
`anchor_root`, `anchor_domain_id`, `wal_root`, `wal_repairs_pending`, `manifest_root` and
`manifest_generation`. **A `wal_repairs_pending: 0` in the legacy profile would assert a clean
WAL that does not exist** — the exact coercion failure the P5-S6 hold audit was rewritten to
remove.

The profile list is enforced from `ops/p5_roadmap.py::S8_PROFILES`.

## 0. Environment and the artifact root

**Run on: LOCAL** — set up and verify before touching the host.

```bash
export PROFILE=${PROFILE:?set PROFILE to legacy_baseline or p5_shadow}
case "$PROFILE" in legacy_baseline|p5_shadow) ;; *) echo "NO-GO: unknown PROFILE"; exit 1;; esac
export RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
# Artifacts live OUTSIDE the production data root and its filesystem: the path must not be
# under $G, because writing there breaks the read-only boundary; and it must not share the
# volume, because a sibling directory still eats the free space G18 is blocked on.
export ART_ROOT=${ART_ROOT:?supply an artifact path OUTSIDE the VM24 data volume}
export ART="$ART_ROOT/p5s8_$RUN_ID"
mkdir -p "$ART"
```

**Run on: VM24** — the host-side variables, and the artifact-root checks.

```bash
export G=/home/odbadmin/Data/ghrsst
export CODE_ROOT=/home/odbadmin/python/ghrsst-p5s8/dev2026        # the P5 code under test
export PYTHON=/home/odbadmin/python/ghrsst-dev2026-phase2/dev2026/.venv/bin/python
export PYTHONPATH=$CODE_ROOT
export DEPLOY_SHA=${DEPLOY_SHA:?set DEPLOY_SHA to the exact commit under measurement}
export SHADOW_ROOT=${SHADOW_ROOT:-}                                # p5_shadow profile only

# The artifact root must not be under $G ...
case "$(readlink -f "$ART")/" in
  "$(readlink -f "$G")"/*) echo "NO-GO: ART resolves under \$G: $ART"; exit 1;; esac
# ... and must not be on the production filesystem.
PROD_DEV=$(findmnt -no SOURCE --target "$G")
ART_DEV=$(findmnt -no SOURCE --target "$ART")
[ "$PROD_DEV" != "$ART_DEV" ] || \
  { echo "NO-GO: ART is on the production device ($PROD_DEV)"; exit 1; }
```

If the operator cannot supply an external path, **stop**. Do not fall back to `$G`.

## 1. Identity preflight — every check is a hard gate

**Run on: VM24.** A measurement whose provenance is unknown is not evidence. Each check
**exits non-zero** rather than printing a warning; the P4-S10 review found a preflight that
printed its result and returned 0, which accepted exactly the configuration it was written to
reject.

```bash
hostname; date -Is
test -d "$CODE_ROOT" || { echo "NO-GO: CODE_ROOT missing: $CODE_ROOT"; exit 1; }
test -x "$PYTHON"    || { echo "NO-GO: PYTHON missing: $PYTHON"; exit 1; }
# The modules this runbook calls must import FROM CODE_ROOT. A runbook that reads correctly and
# cannot import is the failure the P4 rehearsal hit.
PYTHONPATH=$CODE_ROOT "$PYTHON" -c "
import store.block_manifest as bm, store.segmented_cube as sc
assert bm.__file__.startswith('$CODE_ROOT'), bm.__file__
assert sc.__file__.startswith('$CODE_ROOT'), sc.__file__
" || { echo "NO-GO: modules do not import from CODE_ROOT"; exit 1; }

# Exact-SHA pin. `git rev-parse` resolves HEAD, a branch, a tag or an abbreviation, so the
# canonical form must equal what was supplied -- otherwise a movable ref passes as a pin.
cd "$CODE_ROOT/.." && \
  CANON=$(git rev-parse --verify "${DEPLOY_SHA}^{commit}" 2>/dev/null) && \
  [ "$CANON" = "$DEPLOY_SHA" ] || { echo "NO-GO: DEPLOY_SHA is not an exact commit id"; exit 1; }
```

**Reachability from the workstation** is a separate check on a separate machine:

```bash
# Run on: LOCAL
ssh -o BatchMode=yes vm24 'date -Is; hostname' || { echo "NO-GO: host unreachable"; exit 1; }
```

### 1a. P5 identity — `p5_shadow` profile only

**Run on: VM24.** Skipped entirely under `legacy_baseline`, where these are recorded as
`not_applicable_with_reason`.

```bash
if [ "$PROFILE" = "p5_shadow" ]; then
  export ANCHOR_ROOT=${ANCHOR_ROOT:?set ANCHOR_ROOT to an INDEPENDENT rollback domain (§7.5a-2)}
  export WAL_ROOT=$SHADOW_ROOT/p5_repairs
  export MANIFEST_ROOT=$SHADOW_ROOT/p5_blocks
  # ASSERT, do not print: anchor_domain_id() returns None for an undeclared root and exits 0,
  # so `print(...) || NO-GO` would accept exactly what it was written to reject.
  PYTHONPATH=$CODE_ROOT "$PYTHON" -c "
from store import repair_wal as rw
dom = rw.anchor_domain_id('$ANCHOR_ROOT')
assert dom is not None, 'anchor root declares no rollback domain'
print('anchor_domain_id', dom)
print('anchor_root', rw.resolve_anchor_root('$ANCHOR_ROOT'))
" || { echo "NO-GO: anchor domain undeclared or same-filesystem"; exit 1; }
  PYTHONPATH=$CODE_ROOT "$PYTHON" -c "
import json
from store import repair_wal as rw
print(json.dumps({'wal_repairs_pending': rw.pending_state('$WAL_ROOT')}, sort_keys=True))
" > "$ART/wal_state.json"
else
  cat > "$ART/wal_state.json" <<'JSON'
{"wal_repairs_pending": null,
 "not_applicable_with_reason": "legacy_baseline profile: no P5 repair WAL is deployed on this host, so neither a count nor zero is meaningful. A 0 here would assert a clean WAL that does not exist."}
JSON
fi
```

**What the anchor preflight can and cannot prove**, carried forward verbatim from P4-S10
because it is still exactly true: *`anchor_root` preflight verifies the configured path, the
declaration, and the local filesystem relationship; it cannot prove that the path is outside the
VM's snapshot/rollback domain.*

## 2. Capacity preflight (G18)

**Run on: VM24.**

```bash
df -h  /home/odbadmin/Data > "$ART/df_h.txt"
df -i  /home/odbadmin/Data > "$ART/df_i.txt"
findmnt -no SOURCE,FSTYPE,TARGET /home/odbadmin/Data > "$ART/findmnt.txt"
```

Recorded baseline — **historical, 2026-08-19**: `/dev/sdc1`, ext4, 6.0 T total, **267 GiB
free**, 96 % used; inodes 44 % used with 225,490,340 free. **These figures are provisional and
must be refreshed by this step before any go/no-go rests on them**; the volume has been filling
since.

**G18 gate:** `free_now >= 403.3 GiB` (**600 GiB recommended**). At the historical 267 GiB this
is **BLOCKED**, and this step is expected to report BLOCKED until the volume is expanded.

**`HARD_RESERVE` is not adjusted here, or anywhere.** Lowering it changes the verdict without
changing one byte of free space.

## 3. G4 — span latency, fail closed

**Run on: VM24.** A timing sample taken from a 500 is not a latency sample:
`curl -o /dev/null -w '%{time_total}'` times an error page just as happily as an answer. And a
"366-day" measurement over a shorter span is a different measurement wearing the same name.

```bash
S=$(date -u -d '365 days ago' +%F); E=$(date -u +%F)
# Verify the span is EXACTLY 366 days inclusive before any sample is taken.
DAYS=$(( ( $(date -u -d "$E" +%s) - $(date -u -d "$S" +%s) ) / 86400 + 1 ))
[ "$DAYS" -eq 366 ] || { echo "NO-GO: span is $DAYS days, not exactly 366"; exit 1; }

URL="http://127.0.0.1:8080/api/ghrsst?lon0=135&lat0=20&start=$S&end=$E&append=sst"
# One probe request: the row count must match the span, or the endpoint is not answering the
# question we think we asked.
ROWS=$(curl -s "$URL" | "$PYTHON" -c 'import json,sys; print(len(json.load(sys.stdin)))')
[ "$ROWS" -eq 366 ] || { echo "NO-GO: expected row count 366, got $ROWS"; exit 1; }

: > "$ART/g4_366d_seconds.txt"
for i in $(seq 1 30); do
  OUT=$(curl -s -o /dev/null -w '%{http_code} %{time_total}' "$URL")
  CODE=${OUT%% *}; T=${OUT##* }
  if [ "$CODE" != "200" ]; then
    echo "NO-GO: HTTP $CODE during sampling; discard the run" | tee -a "$ART/g4_errors.txt"
    exit 1
  fi
  echo "$T" >> "$ART/g4_366d_seconds.txt"
  sleep 0.3
done
```

- **Rate:** serial, 0.3 s apart. This is a production service; a load generator is not
  authorized here.
- **G3** compares against the **current production** baseline. Until a segmented base is
  deployed there is no segmented arm, so **G3 stays `NOT_ADJUDICATED`** and this profile records
  the baseline half only. A ratio with one arm measured would be the local-to-production
  promotion this whole phase exists to avoid.
- **Percentiles:** record the sample count beside every percentile. A p95 over 7 samples is the
  second-worst value, not a percentile.

## 4. H7 — inode headroom and fd / RSS across ALL workers

**Run on: VM24.** `pgrep -f 'uvicorn|ghrsst' | head -1` picks an arbitrary match — possibly the
`pgrep` itself, possibly the supervisor, possibly one worker of several — and then reports its
fd count as though it were the service's. PM2 knows the pids; ask PM2, and **aggregate** across
every worker.

```bash
pm2 jlist > "$ART/pm2_jlist.json"
PYTHONPATH=$CODE_ROOT "$PYTHON" - "$ART" <<'PY' | tee "$ART/h7_fd_rss_timeseries.txt"
import json, os, sys, time
sys.path.insert(0, os.environ["CODE_ROOT"])
from ops.quiescence import worker_pids          # the shipped helper: EVERY pid PM2 knows
art = sys.argv[1]
with open(os.path.join(art, "pm2_jlist.json")) as fh:
    jlist = json.load(fh)
pids = worker_pids(jlist, "ghrsst")
if not pids:
    raise SystemExit("NO-GO: PM2 reports no pid for the app; nothing to sample")
print(f"# pids={pids}")
for _ in range(60):
    fds = rss = 0
    for pid in pids:                            # aggregate, not one arbitrary worker
        try:
            fds += len(os.listdir(f"/proc/{pid}/fd"))
            with open(f"/proc/{pid}/status") as fh:
                rss += next(int(l.split()[1]) for l in fh if l.startswith("VmRSS"))
        except FileNotFoundError:
            raise SystemExit(f"NO-GO: pid {pid} vanished mid-sample; the service restarted")
    print(f"{int(time.time())} {len(pids)} {fds} {rss}")
    time.sleep(5)
PY
```

Inode headroom is already **PASS_EVIDENCE_AVAILABLE** from the historical run: 225,490,340 free
against a +10-year projection of ~4.9 M additional files, roughly 46× headroom. Re-confirm it
from §2's `df -i`.

**Sustained under what load?** H7 asks for fd/RSS at concurrency 8 and 16. This runbook is
read-only and must not generate that load against production, so **H7's sustained half stays
`NOT_ADJUDICATED`** until the `p5_shadow` profile can run it against a shadow copy. Sampling an
idle process for five minutes and calling it a sustained-load result would be the same category
error as the withdrawn P5-S6 measurements.

Judge growth on the **plateau**, not against the first sample: a worker pool sizing itself once
is not a leak, and the local S7 harness reported exactly that as growth before it was corrected.

## 5. G16 — metadata scan, fail closed

**Run on: VM24.** `os.walk` swallows every `OSError` by default, so a permission-denied subtree
silently shrinks the count and the scan reports a smaller, faster tree than exists.

```bash
/usr/bin/time -v nice -n 19 ionice -c3 \
  "$PYTHON" -c "
import os, sys, time
def boom(err):
    raise SystemExit(f'NO-GO: traversal error at {err.filename}: {err}')
t0 = time.perf_counter(); files = dirs = 0
for _, d, f in os.walk('$G', onerror=boom):   # fail closed on ANY traversal error
    dirs += len(d); files += len(f)
print(f'{files} files, {dirs} dirs, {time.perf_counter()-t0:.1f} s')
" 2>&1 | tee "$ART/g16_metadata_scan.txt"
```

**Abort this step** if service p95 rises above twice its pre-scan value, or if load average
exceeds 4. A walk of 5.4 T is the one step here with real service impact, which is why it is
nice'd, ionice'd and abortable.

**Snapshot-open cost** needs a deployed segmented manifest. There is none on VM24, so
`SegmentedCubeStore` construction cannot be timed against production and **G16 stays
`NOT_ADJUDICATED`** on that half under `legacy_baseline`. Record the scan; do not synthesise the
rest.

## 6. Shadow copy — `p5_shadow` profile only

**Run on: VM24.** Only if the operator supplies `SHADOW_ROOT` on a volume that is **not** the
production data volume:

```bash
if [ "$PROFILE" = "p5_shadow" ]; then
  test -n "$SHADOW_ROOT" || { echo "NO-GO: p5_shadow needs SHADOW_ROOT"; exit 1; }
  PROD_DEV=$(findmnt -no SOURCE --target "$G")
  SHAD_DEV=$(findmnt -no SOURCE --target "$SHADOW_ROOT")
  [ "$PROD_DEV" != "$SHAD_DEV" ] || \
    { echo "NO-GO: SHADOW_ROOT is on the production device"; exit 1; }
  rsync -a --info=progress2 "$G/mur_timecube_s8_t90_sh128.delta.zarr" "$SHADOW_ROOT/"
fi
```

Everything downstream of this point runs **against `$SHADOW_ROOT`**, never `$G`.

## 7. Abort conditions

Stop immediately, record what has been collected, and hand back. Do not "finish the run first".

| condition | why it aborts |
|---|---|
| any command would write under `$G` | the read-only boundary is the whole basis of this step |
| `$ART` resolves under `$G`, or onto the production device | artifacts would breach the boundary and eat the space G18 is blocked on |
| `/healthz` stops returning `ok`, or `pm2` reports the API not `online` | a read-only run must not coincide with a service problem, whoever caused it |
| load average > 4, or service p95 > 2× its pre-step baseline | §5's scan is the one step with real impact |
| free space falls below 250 GiB (`HARD_RESERVE`) at any sample | the host is closer to the edge than the plan assumed |
| any non-200 during G4 sampling | a timing sample from an error page is not a latency measurement |
| the measured span is not exactly 366 days, or the row count does not match | the measurement is not the one being claimed |
| a traversal error during the metadata scan | a partial walk under-reports the tree it is measuring |
| PM2 reports no pid, or a sampled pid vanishes | the service restarted mid-measurement |
| the anchor domain check fails, or `DEPLOY_SHA` is not an exact commit | provenance is unknown, so nothing collected is evidence |
| an open repair intent is found in the WAL (`p5_shadow`) | record it and stop; resolving it is an ops decision, never a step here |
| the host is unstable or unreachable at any point | mark **BLOCKED**, exactly as the 2026-08-19 run was prepared to |
| a shadow root resolves onto the production device | it would consume the free space G18 is blocked on |

## 8. Artifacts and provenance

Every file lands under **`$ART` = `$ART_ROOT/p5s8_$RUN_ID`** — operator-supplied, outside `$G`,
and off the production filesystem.

| artifact | content |
|---|---|
| `preflight.json` | every field in §9, per profile |
| `wal_state.json` | `wal_repairs_pending`, or `not_applicable_with_reason` under `legacy_baseline` |
| `df_h.txt`, `df_i.txt`, `findmnt.txt` | capacity and filesystem identity |
| `g4_366d_seconds.txt` | per-sample timings, with the verified span and row count |
| `pm2_jlist.json`, `h7_fd_rss_timeseries.txt` | worker pids, aggregate fd and RSS over time |
| `g16_metadata_scan.txt` | file/dir counts and wall time |
| `p5s8_readonly.json` | the assembled report, carrying `profile` |

Every record carries `label: VM24_PRODUCTION_READ_ONLY` and the `profile` it was collected
under. Local synthetic numbers are **never** merged into these files, and no number here may be
promoted into a local artifact.

## 9. Required preflight fields

A run missing any of these — without an explicit `not_applicable_with_reason` for its profile —
is not evidence and must be re-run:

`run_id`, `host`, `measured_utc`, `deploy_sha`, `code_root`, `python`, `pythonpath`,
`anchor_root`, `anchor_domain_id`, `wal_root`, `wal_repairs_pending`, `manifest_root`,
`manifest_generation`, `filesystem_device`, `filesystem_fstype`, `free_now_bytes`,
`inodes_free`, `label`.

Enforced from `ops/p5_roadmap.py::S8_PREFLIGHT_FIELDS` and `S8_PROFILES`; a test asserts every
field is either required or explicitly not-applicable for each profile, so a field dropped from
either side fails rather than going unnoticed.

## 10. What this runbook can and cannot settle

| gate | after a successful run |
|---|---|
| **G18** | measurable directly; expected **BLOCKED** until the volume is expanded |
| **H7** inode headroom | adjudicable |
| **H7** sustained fd/RSS | **NOT_ADJUDICATED** — needs load this runbook must not generate against production |
| **G3** | **NOT_ADJUDICATED** — no segmented base is deployed, so there is no segmented arm |
| **G4** | measurable as a baseline; a production p95 needs more samples than a read-only pass should take |
| **G16** metadata scan | measurable |
| **G16** snapshot-open | **NOT_ADJUDICATED** — no deployed segmented manifest to open |

**No S8 result marks the S7 gate PASS.** S8 collects evidence; adjudication is step 6 of the
critical path, and S10 needs its own fresh written approval regardless.
