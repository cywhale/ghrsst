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

# A FRESH directory, never a reused one. `mkdir -p` on an existing path silently adopts an
# interrupted run's leftovers as this run's evidence; `mkdir` without -p refuses.
[ -e "$ART" ] && { echo "NO-GO: artifact directory already exists: $ART"; exit 1; }
mkdir "$ART"

# (2) ... and must not share the production device. A sibling volume-mate still competes for
# the free space G18 is blocked on. `set -e` makes a FAILED findmnt an abort, not two empty
# strings comparing equal -- which is how this check would otherwise pass while proving nothing.
PROD_DEV="$(findmnt -no SOURCE --target "$G")"
ART_DEV="$(findmnt -no SOURCE --target "$ART")"
[ -n "$PROD_DEV" ] && [ -n "$ART_DEV" ] || { echo "NO-GO: could not resolve mount sources"; exit 1; }
[ "$PROD_DEV" != "$ART_DEV" ] || { echo "NO-GO: ART is on the production device ($PROD_DEV)"; exit 1; }
# The run marker. Every producer stamps this RUN_ID into its output and the assembler checks
# each one, so a file left by another run cannot pass as this run's evidence.
"$PYTHON" - "$ART" "$RUN_ID" <<'PYEOF'
import json, os, sys, datetime as dt
art, run_id = sys.argv[1], sys.argv[2]
with open(os.path.join(art, "run.json"), "w") as fh:
    json.dump({"run_id": run_id,
               "created_utc": dt.datetime.now(dt.timezone.utc).isoformat()},
              fh, indent=2, sort_keys=True)
PYEOF
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
: "${CODE_ROOT:?set CODE_ROOT}"; : "${PROFILE:?set PROFILE}"
: "${ART:?run the art-root block first}"; : "${RUN_ID:?run the art-root block first}"
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
# `rev-parse --verify <sha>^{commit}` echoes ANY existing commit back, so the check above
# proves the commit exists -- not that it is the one checked out. A valid ancestor passed it.
HEAD_SHA="$(cd "$CODE_ROOT/.." && git rev-parse --verify HEAD)" || { echo "NO-GO: cannot resolve HEAD"; exit 1; }
[ "$HEAD_SHA" = "$DEPLOY_SHA" ] || { echo "NO-GO: HEAD is $HEAD_SHA, not DEPLOY_SHA $DEPLOY_SHA"; exit 1; }
# ... and the tree must match that commit. `--untracked-files=all` so a stray file counts:
# uncommitted or untracked changes mean the running code is not the commit being pinned.
DIRTY="$(cd "$CODE_ROOT/.." && git status --porcelain --untracked-files=all)"
[ -z "$DIRTY" ] || { echo "NO-GO: worktree is dirty:"; echo "$DIRTY"; exit 1; }

# Record WHAT WAS VERIFIED. A SHA in the preflight proves the preflight holds a SHA; only this
# record says which commit was resolved, from which tree, and which module files were imported.
HEAD_SHA="$HEAD_SHA" PYTHONPATH="$CODE_ROOT" "$PYTHON" - "$CANON" <<'PYEOF'
import hashlib, json, os, sys
sys.path.insert(0, os.environ["CODE_ROOT"])
import importlib
from ops.p5s8_report import identity_modules

art, code_root = os.environ["ART"], os.environ["CODE_ROOT"]
# Every module this profile imports or executes -- the validator and the profile declarations
# included. One inventory, shared with the assembler, so neither side can drift.
modules = {}
for name in identity_modules(os.environ["PROFILE"]):
    module = importlib.import_module(name)
    path = os.path.realpath(module.__file__)
    with open(path, "rb") as fh:
        modules[name] = {"path": path, "sha256": hashlib.sha256(fh.read()).hexdigest()}
rec = {"run_id": os.environ["RUN_ID"], "deploy_sha": sys.argv[1],
       "head_sha": os.environ["HEAD_SHA"], "worktree_clean": True,
       "code_root": code_root, "python": os.environ["PYTHON"], "modules": modules}
with open(os.path.join(art, "identity.json"), "w") as fh:
    json.dump(rec, fh, indent=2, sort_keys=True)
print(f"identity recorded: {len(modules)} module fingerprint(s)")
PYEOF
echo "identity preflight OK: $CANON"
```

**Reachability from the workstation** is a separate check on a separate machine:

```bash
# block: reachability
# Run on: LOCAL
set -euo pipefail
ssh -o BatchMode=yes vm24 'date -Is; hostname' || { echo "NO-GO: host unreachable"; exit 1; }
```

### 1a. WAL state — recorded per profile

**Run on: VM24.** Under `legacy_baseline` there is no P5 WAL, and a `0` would assert a clean WAL
that does not exist. The record says so explicitly instead, and `ops/p5s8_report.py` refuses a
bare `null` with no reason.

```bash
# block: wal-record
# Run on: VM24
set -euo pipefail
: "${PYTHON:?set PYTHON}"; : "${CODE_ROOT:?set CODE_ROOT}"; : "${ART:?}"; : "${PROFILE:?}"
PYTHONPATH="$CODE_ROOT" "$PYTHON" - <<'PYEOF'
import json, os, sys
sys.path.insert(0, os.environ["CODE_ROOT"])
profile, art = os.environ["PROFILE"], os.environ["ART"]
if profile == "legacy_baseline":
    rec = {"run_id": os.environ["RUN_ID"],
           "wal_repairs_pending": None,
           "not_applicable_with_reason": ("legacy_baseline: no P5 repair WAL is deployed on "
                                          "this host, so neither a count nor zero is "
                                          "meaningful")}
else:
    from store.repair_wal import read_wal
    wal_root = os.path.join(os.environ["SHADOW_ROOT"], "p5_repairs")
    # `blocked_days` is the operationally meaningful count: days an unresolved repair keeps
    # from being pruned. It is read, never resolved -- closing an open intent is an ops
    # decision and never a step in a read-only runbook.
    state = read_wal(wal_root)
    pending = len(state.blocked_days)
    if isinstance(pending, bool) or not isinstance(pending, int):
        raise SystemExit(f"NO-GO: WAL pending state is not an integer count: {pending!r}")
    rec = {"run_id": os.environ["RUN_ID"], "wal_repairs_pending": pending,
           "wal_root": wal_root, "blocked_days": sorted(state.blocked_days)}
with open(os.path.join(art, "wal_state.json"), "w") as fh:
    json.dump(rec, fh, indent=2, sort_keys=True)
print(json.dumps(rec, sort_keys=True))
PYEOF
```

The anchor is verified inside `shadow-verify` (§6b), which records its declared domain — so the
`p5_shadow` preflight derives `anchor_root` and `anchor_domain_id` from that record rather than
from an environment variable nobody set.

**What the anchor preflight can and cannot prove**, carried forward verbatim from P4-S10
because it is still exactly true: *`anchor_root` preflight verifies the configured path, the
declaration, and the local filesystem relationship; it cannot prove that the path is outside the
VM's snapshot/rollback domain.*

## 2. Capacity preflight (G18)

**Run on: VM24.** The human-readable `df` output is kept for the operator; the *numbers* the
preflight uses come from `statvfs` and `findmnt` directly. Parsing `267G` out of `df -h` throws
away the precision and unit information a gate then depends on.

```bash
# block: capacity
# Run on: VM24
set -euo pipefail
: "${G:?set G}"; : "${PYTHON:?set PYTHON}"; : "${ART:?run the art-root block first}"
df -h "$G" > "$ART/df_h.txt"
df -i "$G" > "$ART/df_i.txt"
findmnt -no SOURCE,FSTYPE,TARGET --target "$G" > "$ART/findmnt.txt"
[ -s "$ART/findmnt.txt" ] || { echo "NO-GO: findmnt produced no output for $G"; exit 1; }
: "${RUN_ID:?run the art-root block first}"
"$PYTHON" - "$G" "$ART" <<'PYEOF'
import json, os, sys
g, art = sys.argv[1], sys.argv[2]
st = os.statvfs(g)
with open(os.path.join(art, "findmnt.txt")) as fh:
    parts = fh.read().split()
if len(parts) < 2:
    raise SystemExit(f"NO-GO: findmnt output is not SOURCE FSTYPE TARGET: {parts!r}")
rec = {"run_id": os.environ["RUN_ID"],
       "data_root": os.path.realpath(g),          # what the G16 scan must have walked
       "filesystem_device": parts[0], "filesystem_fstype": parts[1],
       "free_now_bytes": int(st.f_bavail) * int(st.f_frsize),
       "inodes_free": int(st.f_favail)}
for key, value in rec.items():
    if value in ("", None):
        raise SystemExit(f"NO-GO: capacity field {key} is empty")
with open(os.path.join(art, "capacity.json"), "w") as fh:
    json.dump(rec, fh, indent=2, sort_keys=True)
print(json.dumps(rec, sort_keys=True))
PYEOF
```

Recorded baseline — **historical, 2026-08-19**: `/dev/sdc1`, ext4, 6.0 T total, **267 GiB
free**, 96 % used; inodes 44 % used with 225,490,340 free. **These figures are provisional and
must be refreshed by this step before any go/no-go rests on them**; the volume has been filling
since.

**G18 gate:** `free_now >= 403.3 GiB` (**600 GiB recommended**). At the historical 267 GiB this
is **BLOCKED**, and this step is expected to report BLOCKED until the volume is expanded.

**`HARD_RESERVE` is not adjusted here, or anywhere.** Lowering it changes the verdict without
changing one byte of free space.

## 2a. Record the preflight — derived, not declared

**Run on: VM24, after §1, §1a and §2** (and after §6 under `p5_shadow`). Every field is
**derived from an artifact an earlier block actually wrote**. The first version read
`FILESYSTEM_DEVICE`, `FREE_NOW_BYTES`, `ANCHOR_ROOT`, `MANIFEST_GENERATION` and the rest from
environment variables the runbook never set, so running the sequence in order aborted on blank
fields — and the only way it passed was to hand-write the artifact this block exists to produce.

```bash
# block: preflight-record
# Run on: VM24
set -euo pipefail
: "${PYTHON:?set PYTHON}"; : "${CODE_ROOT:?set CODE_ROOT}"; : "${ART:?}"; : "${PROFILE:?}"
: "${DEPLOY_SHA:?}"; : "${RUN_ID:?}"
PYTHONPATH="$CODE_ROOT" "$PYTHON" - <<'PYEOF'
import json, os, socket, sys, datetime as dt
sys.path.insert(0, os.environ["CODE_ROOT"])
from ops.p5_roadmap import S8_PREFLIGHT_FIELDS, S8_PROFILES

profile, art = os.environ["PROFILE"], os.environ["ART"]
na = S8_PROFILES[profile]["not_applicable"]

def read(name, required):
    """Load an artifact an earlier block wrote. Fail closed: a preflight assembled around a
    missing input is a preflight describing a check that never ran."""
    path = os.path.join(art, name)
    if not os.path.isfile(path):
        if required:
            raise SystemExit(f"NO-GO: {name} is missing; run its block before this one")
        return {}
    with open(path) as fh:
        return json.load(fh)

capacity = read("capacity.json", True)          # both profiles record where they ran
wal = read("wal_state.json", True)
shadow = read("shadow_verification.json", profile == "p5_shadow")

derived = {
    "run_id": os.environ["RUN_ID"],
    "host": socket.gethostname(),
    "measured_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
    "deploy_sha": os.environ["DEPLOY_SHA"],
    "code_root": os.environ["CODE_ROOT"],
    "python": os.environ["PYTHON"],
    "pythonpath": os.environ.get("PYTHONPATH", os.environ["CODE_ROOT"]),
    "label": "VM24_PRODUCTION_READ_ONLY",
}
derived.update({k: capacity[k] for k in
                ("data_root", "filesystem_device", "filesystem_fstype",
                 "free_now_bytes", "inodes_free")
                if k in capacity})
if profile == "p5_shadow":
    if "wal_repairs_pending" not in wal:
        raise SystemExit("NO-GO: wal_state.json carries no wal_repairs_pending")
    derived.update({
        "anchor_root": shadow["anchor_root"],
        "anchor_domain_id": shadow["anchor_domain_id"],
        "wal_root": shadow["wal_root"],
        "wal_repairs_pending": wal["wal_repairs_pending"],
        "manifest_root": os.path.join(os.environ["SHADOW_ROOT"], "p5_blocks"),
        "manifest_generation": shadow["generation"],
    })

out = {}
for field in S8_PREFLIGHT_FIELDS:
    if field in derived:
        out[field] = derived[field]
    elif field in na:
        out[field] = f"not_applicable_with_reason: {na[field]}"
    else:
        raise SystemExit(f"NO-GO: preflight field {field!r} was neither derived nor excusable "
                         f"under {profile!r}; the sequence is incomplete")
with open(os.path.join(art, "preflight.json"), "w") as fh:
    json.dump(out, fh, indent=2, sort_keys=True)
print(f"preflight recorded: {len(out)} fields, profile {profile}")
PYEOF
```

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
: "${PYTHON:?set PYTHON}"; : "${ART:?run the art-root block first}"
: "${RUN_ID:?run the art-root block first}"; : "${CODE_ROOT:?set CODE_ROOT}"
PYTHONPATH="$CODE_ROOT" "$PYTHON" - <<'PYEOF'
import datetime as dt, json, os, subprocess, sys
sys.path.insert(0, os.environ["CODE_ROOT"])
from ops.p5s8_report import check_g4_response, ArtifactError   # one validator, both paths

art, run_id = os.environ["ART"], os.environ["RUN_ID"]
days = int(os.environ.get("FORCE_SPAN_DAYS", "366"))   # override exists so the guard is testable
samples = int(os.environ.get("G4_SAMPLES", "30"))

end = dt.date.today(); start = end - dt.timedelta(days=days - 1)
span = (end - start).days + 1
if span != 366:
    raise SystemExit(f"NO-GO: span is {span} days, not exactly 366")

url = ("http://127.0.0.1:8080/api/ghrsst"
       f"?lon0=135&lat0=20&start={start}&end={end}&append=sst")
out = subprocess.run(["curl", "-s", "-w", "\n%{http_code}", url],
                     capture_output=True, text=True)
if out.returncode != 0:
    raise SystemExit(f"NO-GO: curl failed with {out.returncode}")
# rstrip first: a trailing newline would leave the status in the wrong partition
body, _, code = out.stdout.rstrip("\n").rpartition("\n")
if code.strip() != "200":
    raise SystemExit(f"NO-GO: probe returned HTTP {code.strip()}; discard the entire run")
try:
    rows = json.loads(body)
except Exception as e:
    raise SystemExit(f"NO-GO: probe body is not JSON ({e}); discard the entire run")
# Row by row: a list of 366 integers, or one repeating a day, has the right length and is
# not an answer. The day set is what makes it a 366-day response.
try:
    check_g4_response(rows, start=start.isoformat(), end=end.isoformat())
except ArtifactError as e:
    raise SystemExit(f"NO-GO: {e}; discard the entire run")

# The validated request, bound to this run. The sampler reads it rather than inheriting a
# variable -- separate blocks are separate shells -- and the assembler cross-checks the sample
# count against the series actually produced.
with open(os.path.join(art, "g4_request.json"), "w") as fh:
    json.dump({"run_id": run_id, "start": start.isoformat(), "end": end.isoformat(),
               "url": url, "span_days": span, "probe_rows": len(rows),
               "g4_samples": samples}, fh, indent=2, sort_keys=True)
print(f"probe OK: {start}..{end}, {len(rows)} rows, {samples} samples planned")
PYEOF
```

Only once that probe passes are timing samples taken — and **every sample is validated the same
way**. Checking the status alone would time a service that has degraded to a short, valid-looking
200, and report the result as a 366-day latency:

```bash
# block: g4-sample
# Run on: VM24
set -euo pipefail
: "${PYTHON:?set PYTHON}"; : "${ART:?run the art-root block first}"
: "${RUN_ID:?run the art-root block first}"; : "${CODE_ROOT:?set CODE_ROOT}"
PYTHONPATH="$CODE_ROOT" "$PYTHON" - <<'PYEOF'
import json, math, os, subprocess, sys, time
sys.path.insert(0, os.environ["CODE_ROOT"])
from ops.p5s8_report import check_g4_request, check_g4_response, ArtifactError

art, run_id = os.environ["ART"], os.environ["RUN_ID"]
sleep_s = float(os.environ.get("G4_SLEEP", "0.3"))

req_path = os.path.join(art, "g4_request.json")
if not os.path.isfile(req_path):
    raise SystemExit("NO-GO: g4_request.json is missing; run g4-probe first")
with open(req_path) as fh:                     # context-managed: no handle left to the GC
    req = json.load(fh)
# The SAME validation the assembler applies, run BEFORE any production request. Catching an
# edited request only at assembly time means thirty requests have already been made against
# the service on the strength of a record nobody checked.
try:
    check_g4_request(req, run_id=run_id)
except ArtifactError as e:
    raise SystemExit(f"NO-GO: {e}")

url, want = req["url"], int(req["g4_samples"])
seconds = []
for _ in range(want):
    out = subprocess.run(["curl", "-s", "-w", "\n%{http_code} %{time_total}", url],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise SystemExit(f"NO-GO: curl failed with {out.returncode}; discard the run")
    body, _, trailer = out.stdout.rstrip("\n").rpartition("\n")
    parts = trailer.split()
    if len(parts) != 2:
        raise SystemExit(f"NO-GO: unparseable curl trailer {trailer!r}")
    code, elapsed = parts[0], float(parts[1])
    if code != "200":
        raise SystemExit(f"NO-GO: HTTP {code} during sampling; discard the run")
    # EVERY response, not just the probe's: a degraded service returns 200 with fewer rows.
    try:
        rows = json.loads(body)
    except Exception as e:
        raise SystemExit(f"NO-GO: sample body is not JSON ({e}); discard the run")
    try:
        check_g4_response(rows, start=req["start"], end=req["end"])
    except ArtifactError as e:
        raise SystemExit(f"NO-GO: sample response invalid ({e}); discard the run")
    if not math.isfinite(elapsed) or elapsed < 0:
        raise SystemExit(f"NO-GO: non-finite sample time {elapsed!r}")
    seconds.append(elapsed)
    time.sleep(sleep_s)

if len(seconds) != want:
    raise SystemExit(f"NO-GO: collected {len(seconds)} samples, planned {want}")
path = os.path.join(art, "g4_366d_seconds.txt")
with open(path, "w") as fh:
    fh.write(f"# run_id={run_id} samples={len(seconds)} validated_rows=366\n")
    for value in seconds:
        fh.write(f"{value}\n")
    fh.flush(); os.fsync(fh.fileno())
print(f"g4: {len(seconds)} validated samples")
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
run_id = os.environ.get("RUN_ID", "")
if not (art and run_id):
    raise SystemExit("NO-GO: ART and RUN_ID must be set; run the art-root block first")
with open(os.path.join(art, "pm2_jlist.json"), "w") as fh:   # the roster IS evidence
    json.dump({"run_id": run_id, "roster": roster}, fh)
series = os.path.join(art, "h7_fd_rss_timeseries.txt")
with open(series, "w") as fh:
    # No spaces: the header is whitespace-tokenised, so `[4101, 4102]` would split in two.
    pid_list = "[" + ",".join(str(p) for p in sorted(first)) + "]"
    fh.write(f"# run_id={run_id} pids={pid_list} samples={samples}\n")
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
    with open(series, "a") as fh:
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
root = os.environ["G"]
line = (f"root={root} files={files} dirs={dirs} "
        f"wall_s={time.perf_counter() - t0:.3f}")
# The block writes its OWN artifact and fsyncs it. Asking the operator to `tee` in prose left
# the assembler requiring a file nothing produced.
path = os.path.join(os.environ["ART"], "g16_metadata_scan.txt")
with open(path, "w") as fh:
    fh.write(f"# run_id={os.environ['RUN_ID']}\n{line}\n")
    fh.flush(); os.fsync(fh.fileno())
print(line)
PYEOF
```

On the host, add `ionice -c3`. The block writes and fsyncs `g16_metadata_scan.txt` itself, so a
scan that aborts leaves no artifact rather than a truncated one.

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
# Recorded as the SOURCE the preflight derives from, so those fields trace to a check that
# actually ran rather than to an environment variable somebody exported by hand.
with open(os.path.join(anchor, "domain.json")) as fh:
    domain_id = json.load(fh).get("domain_id") or ""
if not domain_id:
    nogo("the anchor's domain.json declares no domain_id")
record = {"run_id": os.environ["RUN_ID"],
          "segments": len(store.segment_paths), "generation": store.generation,
          "wal_root": os.path.join(shadow, "p5_repairs"), "anchor_root": anchor,
          "anchor_domain_id": domain_id, "measurement_taken": False}
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
| `run.json` | both | the run marker: `run_id` and creation time, written by `art-root` into a **fresh** directory |
| `g4_request.json` | `legacy_baseline` | the validated request: span, URL, probe row count and the planned sample count the series is checked against |
| `preflight.json` | both | every field in §9, with `not_applicable_with_reason` for those the profile cannot answer |
| `wal_state.json` | both | `wal_repairs_pending`, or the not-applicable record under `legacy_baseline` |
| `df_h.txt`, `df_i.txt`, `findmnt.txt` | `legacy_baseline` | capacity and filesystem identity |
| `g4_366d_seconds.txt` | `legacy_baseline` | per-sample timings, after a validated probe |
| `pm2_jlist.json` | `legacy_baseline` | the worker roster the fd/RSS series was taken from |
| `h7_fd_rss_timeseries.txt` | `legacy_baseline` | aggregate fd and RSS over time |
| `g16_metadata_scan.txt` | `legacy_baseline` | file/dir counts and wall time |
| `shadow_verification.json` | `p5_shadow` | segments, generation, WAL and anchor state — and `measurement_taken: false` |
| `p5s8_readonly.json` | both | the assembled report, written only if every artifact above validates |

**Every artifact is bound to the run.** JSON records carry `run_id`; the three series carry a
`# run_id=... ` header. The assembler checks each against `run.json` and against the directory
name, so a file left behind by an earlier interrupted run — real content, wrong run — is refused
rather than adopted. `art-root` refuses to start in a directory that already exists, which is
where that reuse came from.

The series are **parsed, not sized**. `_check_nonempty` survives only for the human-readable
`df`/`findmnt` copies, which have no schema; it once let the single line `lol` pass as thirty
latency samples.

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
