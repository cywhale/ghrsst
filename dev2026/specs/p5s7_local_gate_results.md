# P5-S7 — full local HTTP / load / disk gate: RESULTS

Status: **9 of 12 gates green. THE S7 GATE IS NOT MET, AND CANNOT BE MET LOCALLY.** G3 and G4
carry production thresholds that no fixture-extent measurement can settle, and G7's real
threshold is the temp disk a production-geometry fold consumes. **S8 is therefore not authored**
(§14 P5-S7, "Stop if").

`[MUT-STG]` — **local and synthetic only**. Every store is a `tempfile` tree. No VM24, no SSH, no
`GHRSST_*` production store, no cron/PM2/systemd/NGINX change, no prune, swap, compaction,
repair, hold cleanup or production deletion of any kind. Nothing here is VM24 capacity or
production-performance evidence.

Branch `dev2026-p5-s6-o2-local`, based on `5f8450c`.

- Harness: [`../bench/bench_p5s7_local_gate.py`](../bench/bench_p5s7_local_gate.py)
- Tests: [`../tests/test_phase2_p5s7_http.py`](../tests/test_phase2_p5s7_http.py) (HTTP surface)
  and [`../tests/test_phase2_p5s7_load.py`](../tests/test_phase2_p5s7_load.py) (load, lifecycle,
  disk gate) and
  [`../tests/test_phase2_p5s7_wiring.py`](../tests/test_phase2_p5s7_wiring.py) (base-tier
  authority), committed **failing first**; consistency guards in
  [`../tests/test_phase2_p5s7_report.py`](../tests/test_phase2_p5s7_report.py) — **55/55** across
  the four modules, **12 mutations killed**, no survivors
- Artifact: `../bench/results/p5s7_local_gate.json`

**S = 90 / C = 30 remains a provisional configuration.** Nothing in this step selects it.

## The finding that came before any measurement

`api/app.py` **could not be pointed at a segmented store at all.** `lifespan` wired
`GHRSST_TIMECUBE_PATH → TimeCubeStore` and nothing else, so `/healthz` reported
`cube_loaded: false`, every point request 400'd, a full-span range returned only the daily
staging days, and `X-Store-Route` was `daily`. Any earlier claim of "the API over a segmented
store" would have been measured against a monolith, or against no cube.

S7 adds `GHRSST_BLOCK_MANIFEST_DIR → SegmentedCubeStore`, which **wins when both are set**: a
manifest is an explicit statement of which blocks are authoritative, and silently preferring a
monolith beside it would serve a generation nobody published. On construction failure the cube is
dropped rather than replaced — falling back from a broken manifest to the monolith next to it
would serve unpublished bytes under a healthy `/healthz`.

This is why the tests go through `TestClient` rather than calling `HybridRouter` or `TieredCube`.
Testing the layer beneath the contract would not have noticed any of it.

## Manifest authority must fail closed — the review finding

The first version of that wiring selected the base with `if os.path.isdir(mf_dir) … elif
tc_path …`. A configured-but-**missing** manifest path — a typo, or a volume not yet mounted —
therefore fell through the `elif` and **silently loaded the legacy monolith**, serving a
different base than the deployment asked for, under a `/healthz` that looked healthy.

The predicate is now *"is a manifest **configured**"*, never *"does the manifest path happen to
exist right now"*:

| `GHRSST_BLOCK_MANIFEST_DIR` | outcome |
|---|---|
| unset, or empty (`FOO=` clears a variable in a unit file) | legacy `GHRSST_TIMECUBE_PATH` fallback remains allowed |
| set and valid | `SegmentedCubeStore` wins; the monolith is **never opened** |
| set but missing, a non-directory, malformed, or stale | **fail closed**: no cube of any kind, `cube_loaded: false`, explicitly daily-only |

The failure cases assert the actual object in `app.state.router.cube` as well as `/healthz` — a
test reading only the endpoint would pass on a build that installed a monolith and mislabelled
it — and walk every store in the tier to prove the monolith path was not opened. A day only the
base holds is refused with 400 rather than answered from the monolith, while the daily window
keeps serving: failing closed on the base must not take the daily tier down with it.

### A second defect this uncovered, in signed-off S2 code

`GHRSST_BLOCK_MANIFEST_DIR` **without** a delta is a legal deployment, and it only became
reachable once the app could build a segmented base. It crashed:
`HybridRouter._CubeSnapshot.covers_days` read `_meta.day_index`, a `TimeCubeStore` field that a
`SegmentedCubeStore` snapshot does not carry — until now a segmented base was only ever reached
*through* `TieredCube`, which has its own snapshot.

**This required a one-line change to S2's `store/hybrid_router.py`, and is flagged for review.**
It does not alter S2/S5 concurrency semantics: the snapshot is still captured once per request in
`__init__`, and the fix answers from the day set *already captured there* instead of dereferencing
`_meta` a second time. It is membership-identical for the monolith, whose `day_index` is built as
`{d: i for i, d in enumerate(days)}` — the same keys. No new read is introduced; one is removed.

## Where the verdicts come from

Correctness gates are **derived by running the suite and reading the outcome per gate**, never
typed into this table. `GATE_TESTS` in the harness maps each gate to the test ids that adjudicate
it; a gate whose tests fail to load is reported `NOT_ADJUDICATED`, not PASS. A hand-written
verdict is a claim about tests rather than a result from them, and the two drift the moment a
test is renamed.

| gate | verdict | basis |
|---|---|---|
| **G1** semantic equality | **PASS** `LOCAL_SYNTHETIC` | point/range/crossing parity against raw block and delta cells, through HTTP |
| **G2** day integrity | **PASS** `LOCAL_SYNTHETIC` | a range spanning every segment and the delta: no missing, duplicate or out-of-order day |
| **G3** point p95 ≤ +25 % | **NOT_ADJUDICATED** | measured locally against a monolith holding the identical days; the threshold is against the **production** baseline on VM24 |
| **G4** 366-day p95 < 4 s | **NOT_ADJUDICATED** | a real 366-day request is measured, but on a 16×16 fixture; production geometry is 17999 × 36000 |
| **G5** memory | **PASS** `LOCAL_SYNTHETIC` | no RSS growth across repeats at fixed maximum concurrency |
| **G6** handles | **PASS** `LOCAL_SYNTHETIC` | no fd or thread growth across the same repeats; segment handles are not reopened per request |
| **G7** compaction temp disk | **PARTIAL** | the shipped `disk_precheck` provably passes *and* fails against a real reserve; the temp disk of a real fold is **not** measured |
| **G8** pre-publication failure | **PASS** `LOCAL_SYNTHETIC` | rejected publication, and an archive written without the pointer move |
| **G9** post-publication failure | **PASS** `LOCAL_SYNTHETIC` | `rollback_to` restores the predecessor's served view exactly; every archive survives byte-unchanged |
| **G10** concurrency | **PASS** `LOCAL_SYNTHETIC` | publication + composite refresh under 8 concurrent readers: complete-old or complete-new, never mixed, never a fabricated `null` |
| **G11** spatial contract | **PASS** `LOCAL_SYNTHETIC` | bbox and POST answers unchanged; a base-only day is never spatially served from a block |
| **G12** route contract | **PASS** `LOCAL_SYNTHETIC` | `X-Store-Route: cube` for point and range; `MAX_DAYS`/413 preserved, including under load |

**Timings are not reproduced in this document.** They live in
`bench/results/p5s7_local_gate.json` under `measured.latency` and
`measured.concurrency.by_concurrency`, and a test asserts this file carries no hand-typed
millisecond figures. Two earlier P5 documents drifted from their artifacts within one round by
hand-copying exactly this kind of table.

## G10 — the gate that had to be made falsifiable

The successor republishes the **last segment's days with different values**. A publication that
changed nothing would make mixing indistinguishable from correctness: every reader would agree
with every other reader for the wrong reason. Eight concurrent readers request that segment's
span while the real composite `refresh()` runs repeatedly, and each response must be *entirely*
old or *entirely* new — never short, never carrying a `null` the store invented. A companion test
asserts `app.state.router.cube` is **not rebound**, because replacing the object would make any
generation change atomic for free and prove nothing about the refresh path.

## Concurrency here is not the cadence

The load axis is **simultaneous in-flight HTTP requests** (1, 4, 8, 16). It is written
`concurrency` everywhere and never abbreviated `C`, because the spec's `C` is the **fold cadence**
of 30 days — an unrelated quantity that happens to share the letter. A `C = 16` load row beside a
`C = 30` sizing row would read as one axis.

### What the resource numbers can and cannot show

RSS, file descriptors and thread count are sampled from **one process**, which also runs the
client, so the client's own allocations sit inside the same RSS. They can show the absence of
unbounded growth — which is what G5 and G6 ask for — and they cannot establish a production
memory or fd budget.

The first version of this measurement **got that distinction wrong**: it compared the
concurrency-16 sample against the concurrency-1 baseline and reported thread growth, which was
the executor pool sizing itself once as load rose. That is a pool doing its job, not a leak. The
harness now runs a **sustained phase at fixed maximum concurrency** and judges G5/G6 on growth
across those repeats; the one-time pool sizing is reported separately under `pool_sizing` and
deliberately not gated. Growth is judged on the **plateau** — the second half of the samples — after three warm-up
bursts at maximum concurrency. The pool climbs for the first repeats and then flattens at its
ceiling; measuring from the first sample reports that ramp as growth, which is what made an
earlier run flip G6 to `NOT_ADJUDICATED`. The full sample list is recorded in the artifact so the
shape can be checked rather than taken on trust, and the threshold is 4 threads rather than 0
because workers are reaped asynchronously. The failure mode G6 names is monotonic growth.

## G7 — why PARTIAL and not PASS

The shipped `build_block.disk_precheck` is called directly, with a **non-zero reserve**, and
asserted in both directions: a modest block passes, a block sized to consume all free space
fails. `hard_reserve_bytes=0` is never used anywhere in S7 — it makes `ok` true for every input,
so a gate built on it would report PASS while being incapable of failing.

What is **not** measured is G7's actual threshold: the temp disk a real production-geometry fold
consumes ("≈ one block + margin; never a second full base"). The gate's arithmetic is verified;
the quantity it gates is not. Local free space also says nothing about VM24, where **G18 remains
BLOCKED (267 GiB available against a 403.3 GiB minimum)**.

## Mutation verification

Twelve mutations, each run **serially** from a git-clean tree and restored with `git checkout`,
never from a local copy — an earlier P5 round corrupted its own source by re-copying a backup
taken from an already-mutated file. Every mutation targets **production code**, not the harness's
own counters; a mutation that only broke a bench tally would prove the bench measures itself.

| mutation | killed by |
|---|---|
| the app prefers the monolith over the manifest | segmented-store wiring tests |
| the segment count is exposed as the public route name | route-contract test |
| the `MAX_DAYS` 413 is dropped | route-contract test, and the under-load error-contract test |
| `rollback_to` consumes the generation archive | post-publication rollback tests |
| `publish` skips manifest validation | pre-publication failure test |
| the disk gate always returns `ok` | disk-gate tests |
| the segmented refresh becomes a no-op | generation-change-during-load tests |
| `/healthz` reports a segmented base as a monolith | wiring test |
| the base is selected on `isdir` (**the reported blocker, reintroduced verbatim**) | missing-manifest fail-closed tests |
| the monolith is loaded when the manifest fails to construct | malformed/stale manifest tests |
| an empty manifest env is treated as configured | legacy-fallback tests |
| `covers_days` reads `_meta.day_index` again | segmented-without-delta tests |

**No survivors.** One *target* within the disk-gate mutation did not fail — the artifact
consistency test, which reads the committed JSON and therefore cannot observe a change in live
code. That is a scope boundary rather than a hole, and it was verified rather than assumed:
regenerating the artifact under the mutation makes that test fail. The code path itself is killed
by the disk-gate tests, so the two directions are both covered.

## Still not adjudicated, and why

| item | state |
|---|---|
| **G3**, **G4** | production thresholds; need the VM24 baseline (S8 read-only) |
| **G7** | PARTIAL — real fold temp-disk unmeasured |
| **H7**, **G16** | inode/metadata scale at the +10-year horizon; VM24 `df -i` |
| **G18** | **BLOCKED** — 267 GiB < 403.3 GiB minimum. No local result changes this |

None of these may be closed by a local synthetic run, and none is closed here.

## Not delivered in this pass

- **S8 is not authored.** The S7 gate is not met, and the spec is explicit that S8 waits.
- No VM24 contact, no production action, no deployment, no cron/PM2/systemd/NGINX change, and no
  data was pruned, swapped, compacted, repaired or deleted.
