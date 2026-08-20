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

**Nothing else writes anywhere.** The shadow used by `p5_shadow` is **pre-provisioned by ops**,
not created here — §6 only verifies it, and verification is a read. So the complete set of paths
this runbook writes is `$ART` and nothing besides, on a volume that is **not the production data
volume**.

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

**Artifacts are created ON VM24**, at an operator-supplied path outside `$G` and off the
production filesystem, and are **copied back to the workstation at the end** (§9a). The previous
draft created `$ART` in a LOCAL block and then wrote to it from VM24 blocks — two different
machines, so every later `> "$ART/..."` would have failed on a path that does not exist there.

**Run on: VM24** — everything in §0 through §7 runs on the host.

```bash
# block: env
set -euo pipefail
export G=/home/odbadmin/Data/ghrsst
export CODE_ROOT=/home/odbadmin/python/ghrsst-p5s8/dev2026        # the P5 code under test
export PYTHON=/home/odbadmin/python/ghrsst-dev2026-phase2/dev2026/.venv/bin/python
export PYTHONPATH=$CODE_ROOT
export PROFILE=${PROFILE:?set PROFILE to legacy_baseline or p5_shadow}
case "$PROFILE" in legacy_baseline|p5_shadow) ;; *) echo "NO-GO: unknown PROFILE"; exit 1;; esac
export DEPLOY_SHA=${DEPLOY_SHA:?set DEPLOY_SHA to the exact commit under measurement}
export SHADOW_ROOT=${SHADOW_ROOT:-}                                # p5_shadow profile only
```

**Run on: VM24** — the artifact root, verified before anything is written to it.

```bash
# block: art-root
set -euo pipefail
: "${G:?set G}"
: "${PYTHON:?set PYTHON}"
: "${ART_ROOT:?supply an artifact path OUTSIDE the VM24 data volume}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
ART="${ART:-$ART_ROOT/p5s8_$RUN_ID}"
[ -n "$ART" ] || ART="$ART_ROOT/p5s8_$RUN_ID"

# (1) the artifact root must not be under $G. Resolved with realpath semantics, so a symlink
# or a `..` cannot smuggle it back inside.
UNDER=$("$PYTHON" - "$ART_ROOT" "$G" <<'PYEOF'
import os, sys
a = os.path.realpath(sys.argv[1]); g = os.path.realpath(sys.argv[2])
print("yes" if a == g or a.startswith(g + os.sep) else "no")
PYEOF
)
[ "$UNDER" = "no" ] || { echo "NO-GO: ART_ROOT is under \$G: $ART_ROOT"; exit 1; }

mkdir -p "$ART"

# (2) ... and must not share the production device. A sibling volume-mate still competes for
# the free space G18 is blocked on. `set -e` makes a FAILED findmnt an abort, not two empty
# strings comparing equal -- which is how this check would otherwise pass while proving nothing.
PROD_DEV="$(findmnt -no SOURCE --target "$G")"
ART_DEV="$(findmnt -no SOURCE --target "$ART")"
[ -n "$PROD_DEV" ] && [ -n "$ART_DEV" ] || { echo "NO-GO: could not resolve mount sources"; exit 1; }
[ "$PROD_DEV" != "$ART_DEV" ] || { echo "NO-GO: ART is on the production device ($PROD_DEV)"; exit 1; }
export RUN_ID ART
echo "ART=$ART"
```

If the operator cannot supply an external path, **stop**. Do not fall back to `$G`.

## 1. Identity preflight — every check is a hard gate

**Run on: VM24.** A measurement whose provenance is unknown is not evidence. Each check
**exits non-zero** rather than printing a warning; the P4-S10 review found a preflight that
printed its result and returned 0, which accepted exactly the configuration it was written to
reject.

```bash
# block: identity-preflight
# Run on: VM24
set -euo pipefail
: "${CODE_ROOT:?set CODE_ROOT}"
: "${PYTHON:?set PYTHON}"
: "${DEPLOY_SHA:?set DEPLOY_SHA to the exact commit under measurement}"
test -d "$CODE_ROOT" || { echo "NO-GO: CODE_ROOT missing: $CODE_ROOT"; exit 1; }
test -x "$PYTHON"    || { echo "NO-GO: PYTHON missing: $PYTHON"; exit 1; }
# The modules this runbook calls must import FROM CODE_ROOT. A runbook that reads correctly and
# cannot import is the failure the P4 rehearsal hit.
PYTHONPATH="$CODE_ROOT" "$PYTHON" -c "
import store.block_manifest as bm
assert bm.__file__.startswith('$CODE_ROOT'), bm.__file__
" || { echo "NO-GO: modules do not import from CODE_ROOT"; exit 1; }
# Exact-SHA pin. `git rev-parse` resolves HEAD, a branch, a tag or an abbreviation, so the
# canonical form must equal what was supplied -- otherwise a movable ref passes as a pin.
CANON="$(cd "$CODE_ROOT/.." && git rev-parse --verify "${DEPLOY_SHA}^{commit}" 2>/dev/null)" \
  || { echo "NO-GO: DEPLOY_SHA does not resolve to a commit"; exit 1; }
[ "$CANON" = "$DEPLOY_SHA" ] || { echo "NO-GO: DEPLOY_SHA is not an exact commit id"; exit 1; }
echo "identity preflight OK: $CANON"
```

The preflight is also **recorded**, with every field the profile requires and an explicit
`not_applicable_with_reason` for every field it does not. `ops/p5s8_report.py` refuses a
preflight that merely omits one — absence reads as clean.

```bash
# block: preflight-record
# Run on: VM24
set -euo pipefail
: "${PYTHON:?set PYTHON}"; : "${CODE_ROOT:?set CODE_ROOT}"; : "${ART:?}"; : "${PROFILE:?}"
PYTHONPATH="$CODE_ROOT" "$PYTHON" - <<'PYEOF'
import json, os, socket, subprocess, sys, datetime as dt
sys.path.insert(0, os.environ["CODE_ROOT"])
from ops.p5_roadmap import S8_PREFLIGHT_FIELDS, S8_PROFILES
profile = os.environ["PROFILE"]; art = os.environ["ART"]
na = S8_PROFILES[profile]["not_applicable"]

def measured(field):
    if field == "run_id":          return os.environ.get("RUN_ID", "")
    if field == "host":            return socket.gethostname()
    if field == "measured_utc":    return dt.datetime.now(dt.timezone.utc).isoformat()
    if field == "label":           return "VM24_PRODUCTION_READ_ONLY"
    return os.environ.get(field.upper(), "")

out = {}
for f in S8_PREFLIGHT_FIELDS:
    out[f] = (f"not_applicable_with_reason: {na[f]}" if f in na else measured(f))
blank = [f for f, v in out.items() if v == ""]
if blank:
    raise SystemExit(f"NO-GO: preflight fields not populated: {blank}")
with open(os.path.join(art, "preflight.json"), "w") as fh:
    json.dump(out, fh, indent=2, sort_keys=True)
print(f"preflight recorded: {len(out)} fields")
PYEOF
```

**Reachability from the workstation** is a separate check on a separate machine:

```bash
# block: reachability
# Run on: LOCAL
set -euo pipefail
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
"366-day" measurement over a shorter span is a different measurement wearing the same name. The
probe therefore validates **status, JSON shape, row count and span** before a single timing
sample is kept, and any failure discards the whole run.

```bash
# block: g4-probe
# Run on: VM24
set -euo pipefail
: "${PYTHON:?set PYTHON}"
DAYS="${FORCE_SPAN_DAYS:-366}"          # override exists so the guard itself can be tested
read -r S E <<<"$("$PYTHON" - "$DAYS" <<'PYEOF'
import datetime as dt, sys
n = int(sys.argv[1])
e = dt.date.today(); s = e - dt.timedelta(days=n - 1)
print(s.isoformat(), e.isoformat())
PYEOF
)"
# The span is measured from the request that will actually be sent, not from the intent.
SPAN=$("$PYTHON" - "$S" "$E" <<'PYEOF'
import datetime as dt, sys
a = dt.date.fromisoformat(sys.argv[1]); b = dt.date.fromisoformat(sys.argv[2])
print((b - a).days + 1)
PYEOF
)
[ "$SPAN" -eq 366 ] || { echo "NO-GO: span is $SPAN days, not exactly 366"; exit 1; }

URL="http://127.0.0.1:8080/api/ghrsst?lon0=135&lat0=20&start=$S&end=$E&append=sst"
RESP="$(curl -s -w '\n%{http_code}' "$URL")"
CODE="$(printf '%s' "$RESP" | tail -n 1)"
BODY="$(printf '%s' "$RESP" | sed '$d')"
[ "$CODE" = "200" ] || { echo "NO-GO: probe returned HTTP $CODE; discard the entire run"; exit 1; }

# Shape before count: an HTML error page and a `{"detail": ...}` both have a length.
ROWS="$(printf '%s' "$BODY" | "$PYTHON" -c "
import json, sys
d = json.load(sys.stdin)
assert isinstance(d, list), 'response is not a list of rows'
assert d, 'response is an empty list'
assert isinstance(d[0], dict) and 'date' in d[0] and 'sst' in d[0], 'unexpected row shape'
print(len(d))
")" || { echo "NO-GO: probe body failed validation; discard the entire run"; exit 1; }
[ "$ROWS" -eq 366 ] || { echo "NO-GO: expected row count 366, got $ROWS"; exit 1; }
echo "probe OK: $S..$E, $ROWS rows"
```

Only once that probe passes are timing samples taken, and any non-200 **during** sampling
discards the run too:

```bash
# block: g4-sample
# Run on: VM24
set -euo pipefail
: "${ART:?run the art-root block first}"
: > "$ART/g4_366d_seconds.txt"
for i in $(seq 1 30); do
  OUT="$(curl -s -o /dev/null -w '%{http_code} %{time_total}' "$URL")"
  CODE="${OUT%% *}"; T="${OUT##* }"
  [ "$CODE" = "200" ] || { echo "NO-GO: HTTP $CODE during sampling; discard the run"; exit 1; }
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
fd count as though it were the service's. PM2 knows the pids; ask PM2, **aggregate** across
every worker, and **re-ask on every sample**.

A restart mid-window is the case that matters: a fd/RSS series spanning two different sets of
processes is not a series. Comparing counts is not enough either — a worker replaced by another
keeps the count identical, so the **pid set** is compared, not its size.

```bash
# block: h7-sample
# Run on: VM24
set -euo pipefail
: "${PYTHON:?set PYTHON}"
: "${CODE_ROOT:?set CODE_ROOT}"
PROC_ROOT="${PROC_ROOT:-/proc}" SAMPLES="${SAMPLES:-60}" INTERVAL="${INTERVAL:-5}" \
"$PYTHON" - <<'PYEOF'
import json, os, subprocess, sys, time
sys.path.insert(0, os.environ["CODE_ROOT"])
from ops.quiescence import worker_pids        # the shipped helper: EVERY pid PM2 knows

proc = os.environ["PROC_ROOT"]
samples = int(os.environ["SAMPLES"]); interval = float(os.environ["INTERVAL"])

def current():
    """One `pm2 jlist` per call -- the raw roster is returned with the pids so the artifact is
    written from the SAME reading the guard used, not from an extra invocation."""
    out = subprocess.run(["pm2", "jlist"], capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit("NO-GO: pm2 jlist failed; no artifact from this run is accepted")
    try:
        jlist = json.loads(out.stdout)
    except Exception as e:
        raise SystemExit(f"NO-GO: pm2 jlist is not JSON: {e}")
    return set(worker_pids(jlist, "ghrsst")), jlist

first, roster = current()
if not first:
    raise SystemExit("NO-GO: PM2 reports no pid for the app; there is nothing to sample")
art = os.environ.get("ART") or ""
if art:                                  # the pid roster IS evidence; persist it, not just fds
    with open(os.path.join(art, "pm2_jlist.json"), "w") as fh:
        json.dump(roster, fh)
print(f"# pids={sorted(first)}")
for _ in range(samples):
    now, _ = current()                   # refreshed EVERY sample, not once at the start
    if now != first:
        raise SystemExit(f"NO-GO: worker pid set changed mid-window: "
                         f"{sorted(first)} -> {sorted(now)}")
    fds = rss = 0
    for pid in sorted(first):
        try:
            fds += len(os.listdir(f"{proc}/{pid}/fd"))
            with open(f"{proc}/{pid}/status") as fh:
                rss += next(int(l.split()[1]) for l in fh if l.startswith("VmRSS"))
        except FileNotFoundError:
            raise SystemExit(f"NO-GO: pid {pid} vanished mid-sample; the service restarted")
    line = f"{int(time.time())} {len(first)} {fds} {rss}"
    print(line)
    if art:
        with open(os.path.join(art, "h7_fd_rss_timeseries.txt"), "a") as fh:
            fh.write(line + "\n")
    time.sleep(interval)
PYEOF
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
silently shrinks the count and the scan reports a smaller, faster tree than exists — an
under-report that looks like good news.

```bash
# block: g16-scan
# Run on: VM24
set -euo pipefail
: "${G:?set G}"
: "${PYTHON:?set PYTHON}"
nice -n 19 "$PYTHON" - <<'PYEOF'
import os, time
def boom(err):
    raise SystemExit(f"NO-GO: traversal error at {err.filename}: {err}")
t0 = time.perf_counter(); files = dirs = 0
for _, d, f in os.walk(os.environ["G"], onerror=boom):   # fail closed on ANY traversal error
    dirs += len(d); files += len(f)
print(f"{files} files, {dirs} dirs, {time.perf_counter() - t0:.1f} s")
PYEOF
```

On the host, run it under `ionice -c3` as well and tee to `"$ART/g16_metadata_scan.txt"`; with
`pipefail` set, a failing scan cannot produce an accepted artifact through `tee`.

**Abort this step** if service p95 rises above twice its pre-scan value, or if load average
exceeds 4. A walk of 5.4 T is the one step here with real service impact, which is why it is
nice'd, ionice'd and abortable.

**Snapshot-open cost** needs a deployed segmented manifest. There is none on VM24, so
`SegmentedCubeStore` construction cannot be timed against production and **G16 stays
`NOT_ADJUDICATED`** on that half under `legacy_baseline`. Record the scan; do not synthesise the
rest.

## 6. Shadow provisioning — `p5_shadow` profile, VERIFICATION ONLY

**This profile takes no measurement.** It proves a pre-provisioned shadow is complete and safely
placed, and adjudicates nothing. G4, H7's sustained half and both halves of G16 stay
**`NOT_ADJUDICATED`** under it: nothing here redirects those collections away from production,
and isolating them needs a real second service that a read-only step does not build. The earlier
draft said "everything downstream runs against `$SHADOW_ROOT`" while G4 still called
`127.0.0.1:8080`, H7 still read production PM2 and `/proc`, and G16 still walked `$G` — a false
claim, and production measurements wearing a shadow label.

### 6a. Filesystem safety gate

**Run on: VM24.** The same two properties as `$ART`, for the same reasons, checked on the
shadow's own path.

```bash
# block: shadow-fs-gate
# Run on: VM24
set -euo pipefail
: "${G:?set G}"
: "${PYTHON:?set PYTHON}"
: "${SHADOW_ROOT:?p5_shadow needs SHADOW_ROOT}"
[ -d "$SHADOW_ROOT" ] || { echo "NO-GO: SHADOW_ROOT does not exist: $SHADOW_ROOT"; exit 1; }
# realpath, so a symlink pointing back into $G cannot pass a string comparison.
UNDER="$("$PYTHON" - "$SHADOW_ROOT" "$G" <<'PYEOF'
import os, sys
a = os.path.realpath(sys.argv[1]); g = os.path.realpath(sys.argv[2])
print("yes" if a == g or a.startswith(g + os.sep) else "no")
PYEOF
)"
[ "$UNDER" = "no" ] || { echo "NO-GO: SHADOW_ROOT resolves under \$G: $SHADOW_ROOT"; exit 1; }
PROD_DEV="$(findmnt -no SOURCE --target "$G")"
SHAD_DEV="$(findmnt -no SOURCE --target "$SHADOW_ROOT")"
[ -n "$PROD_DEV" ] && [ -n "$SHAD_DEV" ] || { echo "NO-GO: could not resolve mount sources"; exit 1; }
[ "$PROD_DEV" != "$SHAD_DEV" ] || { echo "NO-GO: SHADOW_ROOT is on the production device"; exit 1; }
echo "shadow filesystem gate OK"
```

### 6b. Provisioning verification, through the canonical validator

**Run on: VM24.** The shadow is **pre-provisioned by ops** and verified here, never created.
Verification goes through **`SegmentedCubeStore`** — the same code the read path uses — rather
than hand-rolled `os.path` checks. That is what rejects an absolute segment path, a `../`
traversal and a symlink out of the block root; a second implementation in a runbook would drift
from the one that actually serves data.

```bash
# block: shadow-verify
# Run on: VM24
set -euo pipefail
: "${PYTHON:?set PYTHON}"
: "${CODE_ROOT:?set CODE_ROOT}"
: "${SHADOW_ROOT:?p5_shadow needs SHADOW_ROOT}"
: "${ANCHOR_ROOT:?p5_shadow needs an INDEPENDENT ANCHOR_ROOT}"
: "${ART:?run the art-root block first}"
PYTHONPATH="$CODE_ROOT" "$PYTHON" - "$SHADOW_ROOT" "$ANCHOR_ROOT" "$ART" <<'PYEOF'
import json, os, sys
sys.path.insert(0, os.environ["CODE_ROOT"])
from store.segmented_cube import SegmentedCubeStore

shadow, anchor, art = sys.argv[1], sys.argv[2], sys.argv[3]

def nogo(msg):
    raise SystemExit(f"NO-GO: {msg}")

mroot = os.path.join(shadow, "p5_blocks")
if not os.path.isfile(os.path.join(mroot, "manifest.json")):
    nogo(f"no published manifest under {mroot}; a delta copy is not a P5 shadow")
try:
    # Canonical validation: manifest schema, segment containment, absolute-path refusal,
    # `..` and symlink escape, and every referenced block present on disk.
    store = SegmentedCubeStore(mroot)
except Exception as e:
    nogo(f"shadow manifest failed canonical validation: {e}")
if not os.path.isdir(os.path.join(shadow, "p5_repairs")):
    nogo("no WAL root (p5_repairs) in the shadow")
if not os.path.isdir(anchor):
    nogo(f"anchor root missing: {anchor}")
if not os.path.isfile(os.path.join(anchor, "domain.json")):
    nogo("the anchor root declares no rollback domain")
record = {"segments": len(store.segment_paths), "generation": store.generation,
          "wal": True, "anchor_domain_declared": True, "measurement_taken": False}
with open(os.path.join(art, "shadow_verification.json"), "w") as fh:
    json.dump(record, fh, indent=2, sort_keys=True)
print(f"shadow verified: {record['segments']} segment(s), generation {record['generation']}")
PYEOF
```

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
and off the production filesystem — and is copied back to the workstation in §8a.

The list below is not a wish: `ops/p5s8_report.py::required_artifacts(profile)` is its single
definition, §8b refuses to assemble a report if any is missing, unparseable or incomplete, and a
test compares this table against the generator so neither side can drift.

| artifact | profile | content |
|---|---|---|
| `preflight.json` | both | every field in §9, with `not_applicable_with_reason` for those the profile cannot answer |
| `wal_state.json` | both | `wal_repairs_pending`, or the not-applicable record under `legacy_baseline` |
| `df_h.txt`, `df_i.txt`, `findmnt.txt` | `legacy_baseline` | capacity and filesystem identity |
| `g4_366d_seconds.txt` | `legacy_baseline` | per-sample timings, after a validated probe |
| `pm2_jlist.json` | `legacy_baseline` | the worker roster the fd/RSS series was taken from |
| `h7_fd_rss_timeseries.txt` | `legacy_baseline` | aggregate fd and RSS over time |
| `g16_metadata_scan.txt` | `legacy_baseline` | file/dir counts and wall time |
| `shadow_verification.json` | `p5_shadow` | segments, generation, WAL and anchor state — and `measurement_taken: false` |
| `p5s8_readonly.json` | both | the assembled report, written only if every artifact above validates |

Every record carries `label: VM24_PRODUCTION_READ_ONLY` and the `profile` it was collected
under. Local synthetic numbers are **never** merged into these files, and no number here may be
promoted into a local artifact.

## 8b. Assemble the report — or refuse

**Run on: VM24.** The artifact table is a contract, not a wish-list. This step validates every
artifact the profile promised and **writes nothing** unless all of them are present, parseable
and complete. A partially-collected run is the most dangerous artifact of all: every figure in
it is real, and the reader cannot see that the measurement it was meant to support never ran.

```bash
# block: assemble
# Run on: VM24
set -euo pipefail
: "${PYTHON:?set PYTHON}"
: "${CODE_ROOT:?set CODE_ROOT}"
: "${ART:?run the art-root block first}"
: "${PROFILE:?set PROFILE}"
PYTHONPATH="$CODE_ROOT" "$PYTHON" -m ops.p5s8_report --art "$ART" --profile "$PROFILE"
```

The generator is `ops/p5s8_report.py`; `required_artifacts(profile)` is the single definition of
what a run must produce, and the table in §8 is checked against it by test.

## 8a. Artifact copy-back

**Run on: LOCAL.** The artifacts were created on VM24, outside `$G`. Bring them to the
workstation for review; nothing is deleted from the host by this runbook.

```bash
# block: copy-back
# Run on: LOCAL
set -euo pipefail
: "${ART:?the ART path reported by the art-root block on VM24}"
DEST="${DEST:-./p5s8_artifacts}"
mkdir -p "$DEST"
scp -r "vm24:$ART" "$DEST/"
echo "copy-back complete: $DEST"
```

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
