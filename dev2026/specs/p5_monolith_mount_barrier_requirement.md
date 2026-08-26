# P5 — storage requirement for the legacy monolith, and how to verify it

**Audience: whoever owns the VM24 volumes, and the operator who will run the verification.**
Nothing in this document is executed by any code in this repository, and nothing in it should be
run until the storage layout has actually been changed. It states what P5 needs, why the
cheaper-looking alternatives do not work, and the exact read-only commands that establish
whether the requirement has been met.

No VM24 action has been taken to produce this. The geometry in §2 is from the collection of
2026-08-25 and is a snapshot, not a live reading.

---

## 1. What is being asked, and why it is not a formality

P5's first generation makes the existing monolith the **served authority** — a `legacy_base`
segment in a published manifest. From that moment, a change to the monolith is a change to what
the API returns, with no build step in between.

Two things in the code look like they cover this, and neither does:

| mechanism | what it actually does | why it is not the barrier |
|---|---|---|
| `content_seal` (a ledger of path, size and mtime) | notices that the store changed | **after the fact**, and blind to a write that restores size and mtime — demonstrated by a test, not assumed |
| `check_monolith_sealed()` (permission bits on the store root) | refuses an obviously-writable store at build time | `chmod` is reversible by anything with write access to the parent, a process running as root ignores it, and it says nothing about subdirectories |

Both are documented as necessary-not-sufficient where they are defined. **The barrier that
actually holds is a read-only mount**, and no code in this project can create one. That is the
dependency this document exists to hand over.

Until it is met, the `monolith_barrier` readiness requirement stays unsatisfied and
`compaction_ready` stays `false`. That is the intended behaviour, not a defect to work around.

---

## 2. Current geometry (as collected 2026-08-25 — a snapshot)

```
$G                  /home/odbadmin/Data/ghrsst
mount               /dev/sdc1  ->  /home/odbadmin/Data   (ext4, rw)   st_dev 2081
free                1252.1 GiB of 7054.4 GiB

monolith            $G/mur_timecube_s8_t90_sh128.zarr           GHRSST_TIMECUBE_PATH
delta               $G/mur_timecube_s8_t90_sh128.delta.zarr     GHRSST_DELTACUBE_PATH
daily               $G/mur.zarr                                 GHRSST_ZARR_PATH
p5_blocks           $G/p5_blocks                                (does not exist yet)
p5_repairs          $G/p5_repairs                               (does not exist yet)
```

**Everything is on one mount.** That is the problem in one line: `$G` cannot be made read-only,
because the delta receives a daily append, the repair WAL must be writable, and the P5 roots do
not exist yet and will be created there. Freezing that mount would leave a deployment that can
neither ingest, nor repair, nor compact.

---

## 3. Required target geometry

1. The **monolith path resolves through a genuinely read-only mount** — the mount, not the
   directory permissions.
2. **`p5_blocks`, `p5_repairs` and `delta` remain writable, on a different mount.** The
   readiness contract refuses a record that places any of them on the monolith's mount, because
   a mount is read-only or it is not and the two claims cannot both hold.
3. The monolith path is **not a symlink**: `path` and `realpath` must be equal. A link can be
   re-pointed without the mount record changing, which would make "what is frozen" and "what is
   served" different questions.

### On a read-only bind mount

A bind mount is worth considering and **must not be assumed to work**. Two specific reasons:

* A plain `mount --bind` inherits the source's read-write flag. Making it read-only needs a
  second step (`mount -o remount,ro,bind …`), and on some kernel/systemd combinations the
  remount silently does not take. `findmnt` output is the only evidence that it did.
* **More importantly: a read-only bind mount does not make the underlying files read-only
  through their original path.** If the monolith stays inside `$G` and is *also* exposed
  read-only elsewhere, then writes through `$G/mur_timecube_s8_t90_sh128.zarr` still succeed —
  and `$G` has to stay writable for the delta. The barrier would exist only for readers who
  happen to use the new path, which is not a barrier.

So the shape that actually satisfies the requirement is the monolith **relocated out of `$G`**
onto its own read-only mount (its own volume, or a filesystem whose only writable exposure is
withdrawn). A bind mount may be part of how that is arranged, but the write-denial test in §4.7
is what decides, and it must be run against **every path by which the monolith is reachable**.

Nothing in this document claims a bind mount is sufficient. §4 is how that claim would be
earned.

---

## 4. Verification checklist

**Run only after IT confirms the layout has been changed.** Every command here is read-only
except §4.7, which attempts one write **to the monolith** in order to be denied, and creates
nothing if the barrier holds. Nothing writes under `$G`.

Substitute the real paths if the layout moved the monolith.

```bash
MONO=/path/to/mur_timecube_s8_t90_sh128.zarr     # the NEW monolith location
G=/home/odbadmin/Data/ghrsst
DELTA=$G/mur_timecube_s8_t90_sh128.delta.zarr
RUN_ID=$(date -u +%Y%m%dT%H%M%SZ)
```

### 4.1 Mount facts for every path the contract names

```bash
for p in "$MONO" "$G" "$DELTA" "$G/p5_blocks" "$G/p5_repairs"; do
  printf '%s\n' "$p"
  findmnt -T "$p" -o SOURCE,TARGET,FSTYPE,OPTIONS --noheadings 2>&1 | sed 's/^/    /'
done
```

`p5_blocks` and `p5_repairs` do not exist yet; `findmnt -T` reports the mount of the nearest
existing ancestor, which is the answer the contract wants for them.

### 4.2 Device numbers

```bash
for p in "$MONO" "$G" "$DELTA"; do
  printf '%-60s st_dev=%s\n' "$p" "$(stat -c %d "$p")"
done
```

The monolith's `st_dev` must differ from `$G`'s. Equal device numbers mean one filesystem, and
therefore one read-only flag for both.

### 4.3 Realpath and symlink check

```bash
printf 'path     %s\nrealpath %s\n' "$MONO" "$(realpath "$MONO")"
test "$MONO" = "$(realpath "$MONO")" && echo "  not a symlink: OK" || echo "  SYMLINK: refused"
ls -ld "$MONO"
```

### 4.4 Read-only option token

```bash
OPTS=$(findmnt -T "$MONO" -o OPTIONS --noheadings | tr -d ' ')
echo "options: $OPTS"
echo "$OPTS" | tr ',' '\n' | grep -qx 'ro' && echo "  read-only: OK" || echo "  NOT read-only: refused"
```

Matched as a whole token, deliberately. `relatime` contains the letters `ro`, and a substring
test would report a read-write mount as frozen.

### 4.5 Writable-path separation

```bash
MONO_TGT=$(findmnt -T "$MONO" -o TARGET --noheadings | tr -d ' ')
for p in "$G/p5_blocks" "$G/p5_repairs" "$DELTA"; do
  T=$(findmnt -T "$p" -o TARGET --noheadings | tr -d ' ')
  O=$(findmnt -T "$p" -o OPTIONS --noheadings | tr -d ' ')
  status=OK
  [ "$T" = "$MONO_TGT" ] && status="SAME MOUNT AS MONOLITH: refused"
  echo "$O" | tr ',' '\n' | grep -qx 'ro' && status="READ-ONLY: refused"
  printf '%-46s target=%-28s %s\n' "$p" "$T" "$status"
done
```

### 4.6 Every path by which the monolith is reachable

If a bind mount is used, list the other paths to the same filesystem and repeat §4.4 and §4.7
for each. A read-only view does not constrain a read-write one.

```bash
findmnt -o SOURCE,TARGET,OPTIONS | grep -F "$(findmnt -T "$MONO" -o SOURCE --noheadings | tr -d ' ')"
```

### 4.7 Write-denial test, as the service user

The only step that attempts a write. It must **fail**.

```bash
sudo -u odbadmin touch "$MONO/.p5_write_denial_test" \
  && { echo "WROTE: the barrier does not hold"; sudo -u odbadmin rm -f "$MONO/.p5_write_denial_test"; } \
  || echo "write denied: OK"
```

Run it as the user the API and the ingest cron actually run as (`odbadmin` at the time of
writing — confirm before trusting the result). A denial for `root` proves nothing about the
service, and a denial caused by permission bits rather than the mount is not the barrier: §4.4
is what distinguishes them.

If the file *is* created, remove it and record that the requirement is not met. Do not adjust
permissions to make the test pass — that would be reintroducing exactly the mechanism §1 rules
out.

### 4.8 Nothing was written under `$G`

```bash
find "$G" -newermt "$(date -u -d '10 minutes ago' '+%Y-%m-%d %H:%M:%S')" 2>/dev/null | head
```

Empty is the expected result. The scheduled delta append writes under `$G` on its own cadence;
if it ran during the checklist, its log will say so and that is not this checklist's doing.

---

## 5. What the readiness contract does with the output

The verification above produces exactly the fields `monolith_barrier` adjudicates:

| checklist step | record field |
|---|---|
| §4.3 | `monolith_path`, `monolith_realpath` |
| §4.1 | `source`, `target`, `fstype`, `options` |
| §4.2 | `device` |
| §4.5 | `writable_paths[]` — `role`, `path`, `source`, `target`, `options` for `p5_blocks`, `p5_repairs`, `delta` |
| the run itself | `run_id`, `host`, `collected_utc` |

The record must be collected **in the same run** as the rest of the readiness evidence, on the
host it describes, and inside the **2-hour** freshness window — a mount can be remounted
read-write at any time, so an older record says what was true then.

`§4.6` and `§4.7` are not fields. They are what an operator must have satisfied before treating
the record as meaningful, and they belong in the run's notes.

---

## 6. Status while this is open — fail closed

* **No mount-barrier record exists**, so `monolith_barrier` is `NOT_ADJUDICATED`: missing
  evidence, not a barrier shown to be absent. It blocks readiness either way.
* **`compaction_ready` is `false`** and no local result claims otherwise.
* **`S = 90` / `C = 30` remain provisional.** The generated manifest names them as policy and as
  unselected; selecting them needs an explicit operator decision recorded in the roadmap, and
  there is none.
* **No VM24 evidence will be collected until the operator is ready to continue**, because every
  readiness figure expires two hours after collection and a collection nobody is ready to act on
  expires unused.

## 7. Where this sits

```
>>> 1. IT mount-layout decision  <<<   this document
    2. fresh VM24 read-only collection
    3. candidate deployment review / approval
    4. provisioning approval
    5. provision generation 1 + WAL
    6. API switchover approval
    7. compaction approval
```

Step 1 gates every step after it. Nothing further is buildable locally until the layout question
is answered, which is why this is a requirement handed over rather than more implementation.
