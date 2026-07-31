# P5 — segmented time-cube / block-level compaction (DESIGN SPEC — SPEC-ONLY, NO IMPLEMENTATION)

Status: **v8 — ARCHITECTURE SIGNED OFF (Codex, round-7); round-8 consistency cleanups applied.** Claude
authored. **This spec file is committed** (`9cf4c07`, branch `dev2026-p5-segmented-compaction-design`,
based on `14f26db` = v0.5.0). **No implementation code and no tests have been written for P5, no other file
has been created or modified by this design work, and no VM24 path has been touched.** Implementation
begins at P5-S0 (§14), which is local, read-only benchmark harness work only.

> **Round-8 cleanups (consistency only; no architecture review required).** (1) **Writers validate the
> complete existing WAL before appending** (§7.5a-1 write protocol step 2): under `LOCK_EX`, run the same
> strict whole-file parser readers use; only a fully valid WAL permits deriving `last_seq`/`last_checksum`
> and appending. Any integrity failure **refuses the write and alarms**, leaving the WAL byte-unchanged —
> **valid records are never appended after a corrupt tail.** F13f gains case (6). (2) **`prev_checksum` is
> mandatory** — `null` only at `seq 1`, otherwise it must equal `seq-1`'s `record_checksum`; a chain break
> fails closed like any other integrity failure. (3) Acceptance self-check corrected to **22 gates (G1–G22)**.
>
> **Round-7 patch folded (one item; not an architecture change).** Because `p5_repairs.jsonl` is the **only
> authorizing evidence** for pruning a repaired day, its serialization and parser semantics are now part of
> the contract — **§7.5a-1**: dedicated **`p5_repairs.lock`** (distinct from the ingest and compaction
> locks) serializing appends and allocating a **monotonic, gap-free `seq`** under the lock (`repair_id` may
> be UUID + seq); every record carries `seq` + `record_checksum` and a **mandatory `prev_checksum` chain**
> (`null` only at `seq 1`, otherwise equal to `seq-1`'s `record_checksum` — made mandatory in round-8);
> **append + flush + fsync per record**, plus a **parent-directory fsync on creation**; **exactly one
> terminal state per intent**, with **identical terminal replay idempotent** and any **committed-vs-aborted
> conflict invalid**; and a **strict, whole-file, fail-closed parser** — malformed/truncated JSON, checksum
> failure, `seq` gap, or duplicate conflicting `seq`/`repair_id` each **disable the affected prune and are
> never skipped or ignored**, with an explicit administrative `log_tail_repaired` recovery for a torn tail.
> Fail-closed here only pauses compaction: the affected days stay in delta and keep being served correctly.
> New fixture **F13f** (concurrent intents, torn final line, conflicting terminals, idempotent replay,
> seq/checksum corruption) and gate **G22**; **Q17(c)** extended to name the owner of open intents and WAL
> repairs, and to surface both in the O4-style audit. All round-6 repair-identity and base-only verification
> changes are retained unchanged.

> **Round-6 patches folded (2 High, both in the correction proof chain).** Architecture main line unchanged:
> **B immutable versioned blocks + C legacy segment zero**, **S = 90 / C = 30** still a provisional
> hypothesis for P5-S6. Round-4/5 accepted items (`classification_target` vs `rebuild_source_set`, kernel
> `flock` + inode fencing, cumulative superseded lifecycle, sealed corrective refold, supervisor-only lock
> fd) are retained unchanged.
> 1. **[High] Repair provenance is now identity-based and crash-safe** (§5 `build_provenance`, §7.5a).
>    Round-5's E1 compared a block's **build time** to `repaired_at_utc` — but time ordering does not prove
>    the block *consumed* the corrected value, and there is a crash window where the delta repair becomes
>    visible before its log line is appended. `p5_repairs.jsonl` is now a **fail-closed WAL**:
>    `repair_intent` (fsync, **before** the repair) → rebuild/swap/validate → `repair_committed` (fsync) →
>    `repair_aborted` only on a confirmed failure. **Any non-aborted intent blocks prune**, with no timeout
>    and no self-resolution. A corrective block records per repaired day the **latest committed
>    `repair_id`**, `source_kind: "delta"`, and the source fingerprint; prune requires
>    `materialized_repairs[D].repair_id == latest_committed(D)` **plus** a fingerprint match. **Build
>    timestamps are diagnostic only and authorize nothing.** Four crash boundaries specified and tested
>    (**F13e**), gate **G21**.
> 2. **[High] The pre-prune verification could not prove the corrected block** (§7.6 probe 2, §7.8 step 5,
>    new **§7.8a**). Pre-prune the repaired day is still in delta and `TieredCube` is delta-wins, so a public
>    point GET returns **delta** — round-5's probe would have passed whether or not the refold worked.
>    Verification is now three-phase: **A (pre-prune, base-only)** read through a base-only
>    `SegmentedCubeStore`, assert the manifest resolves the day to the **new** version, compare
>    value + `var_valid` against the repaired delta day, and check provenance identity — *this is the proof*;
>    **B** run the delta prune only after A passes; **C (post-prune, public)** assert the normal point GET
>    still returns the corrected value. **F13d** now asserts all three states, and §7.6's routine-fold probe
>    is split the same way (block proof = base-only; public GET = serving contract, never block proof).

> **Round-5 patches folded (1 High + 1 Medium).** Architecture main line unchanged: **B immutable versioned
> blocks + C legacy segment zero**, **S = 90 / C = 30** still a provisional hypothesis for P5-S6. Round-4's
> accepted items (`classification_target` vs `rebuild_source_set`, unsealed `confirmed_missing` promotion,
> kernel `flock` with inode fencing, cumulative superseded lifecycle) are retained unchanged.
> 1. **[High] Sealed blocks now have a correction path** (§5.5, §7.5a, §7.7, §7.8). Round-4 said sealed
>    blocks are "never re-checked", which loses data: a day in a sealed block repaired into delta looks
>    correct via delta precedence, then the day is pruned because **base has day membership**, and the
>    sealed block's old value/gap becomes permanent. Corrected on three fronts. **(a)** The rule is restated
>    as *"a sealed block's **path** is never modified; corrections are represented by a new immutable version
>    that supersedes it"* — path immutability, not logical freezing. **(b)** New **§7.8 corrective refold**:
>    build a new immutable version of the **same fixed calendar window** at a new path, rebuild its complete
>    `present` set under normal delta-first authority (promoting a backfilled `confirmed_missing` day),
>    publish, verify — **and only then** may the repaired day leave delta. **(c)** New **§7.5a hard prune
>    gate**: day membership in base is **not sufficient**; dropping a day requires evidence the current base
>    generation holds the *corrected value* — **E1** deterministic provenance-recency against an append-only
>    repair registry, **E2** seeded point-wise float32/`var_valid` parity as defence in depth, **E3** the gap
>    case already covered by base-coverage. Fixture **F13d**, gate **G20**, sign-off **Q17**.
> 2. **[Medium] Lock-fd ownership resolved** (§7.1b-1). Round-4 simultaneously claimed "process death
>    releases the lock" and "helpers must inherit the fd" — incompatible, since an inherited fd keeps the
>    lock alive past the parent's death. Now: the build is **threads in one process** by default (no child
>    processes at all); if children are ever used, **only the supervisor owns the fd** (`O_CLOEXEC`, never
>    inherited), **workers can write staging but never publish**, the supervisor outlives its workers, and an
>    orphaned worker can at worst corrupt an *unpublished* staging tree. If inheritance is unavoidable, the
>    runbook must kill the **process group/cgroup** and **verify a fresh non-blocking acquisition** before
>    proceeding. Fixture **F16c**, folded into gate **G17**.

> **Round-4 patches folded (2 High + 1 Medium).** Architecture main line unchanged: **B immutable versioned
> blocks + C legacy segment zero**, **S = 90 / C = 30** still a provisional hypothesis for P5-S6. Round-3's
> accepted items (three-state day schema, delta-first late-correction authority, additional-only disk gate,
> archive copy/fsync/replace, Candidate D evidence correction) are retained unchanged.
> 1. **[High] Source-map scope contradiction resolved** (§5.5, §7.1a). Round-3 limited resolution to
>    `(prev materialized_through, new materialized_through]` — for v2 that is **B only**, yet v2 is a *new
>    immutable block* that must write **A + B**, and F13/F13b require A to be re-resolved. Now two sets:
>    **`classification_target`** (newly aged days; resolves `unknown` → `present`/`confirmed_missing`) and
>    **`rebuild_source_set`** (**every `present` day of the successor**; one source-map entry each, resolved
>    fresh against delta → published block → daily → hold → NetCDF). `unknown` days need no source. Added
>    **F13c**: a `confirmed_missing` day backfilled later is **promoted** on an unsealed block (same defect
>    class as F13b). Gate **G13** rewritten. No change to §10/§11 — the cost model already assumed full
>    re-materialization.
> 2. **[High] TTL lease withdrawn; build isolation is now a held kernel `flock`** (§7.1b). A renewable TTL
>    lease cannot fence a **paused** holder: it expires, a takeover legitimately swaps delta, the holder
>    resumes and renews — "same `lease_id`, no *observed* gap" is not a fencing property, because the paused
>    holder observed nothing. Adopted: one dedicated `p5_compaction.lock` held `LOCK_EX` for the whole build;
>    prune/swap/repair try `LOCK_EX|LOCK_NB` and **refuse**; daily append does not take it; the status JSON is
>    observability only; process death releases it. Publish additionally asserts **the fd is still held AND
>    `fstat(fd).st_ino == stat(path).st_ino`** (a deleted-and-recreated lock file is the only two-writer
>    path). Fencing requirements for the TTL variant are specified if it is ever chosen. New **F16/F16b**
>    split-brain tests, gate **G17**, ops rules in **Q15**.
> 3. **[Medium] `superseded[]` is now cumulative** (§5.6). It carries **every** non-hard-deleted superseded
>    block across generations, so a manifest generation is self-sufficient as a rollback target and the
>    `pinned_existing` forecast is complete. Entries update in place through
>    `referenced → releasable → held` and are removed only by an ops hard delete, itself a new generation.
>    A missing entry **alarms and disables that rollback without failing the served snapshot** (deliberate
>    asymmetry with §5.1). New **F17** multi-generation test, gate **G19**.

> **Round-3 patches folded (four blockers + one wording sync).** Architecture main line unchanged:
> **B immutable versioned blocks + C legacy segment zero**, with **S = 90 / C = 30** still a provisional
> hypothesis for P5-S6.
> 1. **Unsealed blocks are now representable** (§5.3, §5.4, §5.5). The round-2 two-way invariant
>    `present + confirmed_missing == S` was **unsatisfiable for v1/v2** under a fixed 90-day window
>    materialized in 30-day passes. Added a third state **`unknown`** plus `materialized_through`:
>    unsealed ⇒ `present + confirmed_missing + unknown == S` with `unknown > 0`; sealed ⇒ `unknown == 0`.
>    The source map resolves **only this fold's target days**, never future/unknown days. Fixture **F14**.
> 2. **Source precedence is now the current serving authority** (§7.1a): **delta → published block → daily
>    → hold → NetCDF**. A day repaired back into delta after a prune must be taken from **delta**, or the
>    correction would be frozen out of the successor block and lost at the next prune. Carry-forward parity
>    is redefined as **successor == currently served authoritative value**. Fixture **F13b**, gate **G13**.
> 3. **Build isolation** (§7.1b). The build reads live delta for hours outside the ingest lock, so a
>    concurrent `prune_delta`/`swap_delta` **retarget** would write S8a-class wrong bytes into a *new
>    immutable block*. Delta prune/swap/repair must **refuse**; daily append continues; publish still takes
>    the short ingest lock. *(The renewable TTL lease proposed here was **superseded in round-4** by a held
>    kernel `flock` — see the round-4 note above.)* Immutable hardlink delta snapshot documented as the
>    measured fallback. Fixtures **F16/F16b**, gate **G17**, proof test in P5-S5(f), sign-off **Q14/Q15**.
> 4. **Disk gate no longer double-counts** (§7.1c, §11). `free_now` already excludes existing files, so the
>    gate charges **`additional` only** (staging block + temp/checkpoint + measured in-build growth);
>    predecessor/superseded blocks become **`pinned_existing`** — reported, forecast, alarmed, never charged
>    twice. Worked v1/v2/v3 example shows the corrected gate passes the **sealing** fold (288 GB free vs a
>    250 GB reserve) where the double-counting formula would have wrongly refused it (147 GB). Gate **G18**.
> 5. **§4.1 rule 2 wording synced** with §9.3: rollback is copy archive → verify → fsync → replace temp,
>    never `os.replace` of the archive itself.

> **Round-2 patches folded (architecture direction accepted; these are corrections, not a redesign).**
> The reviewer accepted **B immutable versioned blocks + C legacy-monolith migration** as the main line and
> **S = 90 / C = 30 as a provisional hypothesis for P5-S6 to validate**. Eight corrections:
> 1. **Tail rebuild must source from the currently published tail block** (§7.1a): after the first prune,
>    the earlier days are gone from delta/daily/hold, so v2 = block v1 + newly-aged delta, v3 = block v2 +
>    delta. New fixture **F13** (v1→v2→v3 with a delta prune between versions) and gate **G13**.
> 2. **Manifest/delta authority ambiguity removed** (§4.0): the manifest describes **legacy base +
>    immutable extension blocks only**. No delta path, cutoff or precedence in the manifest; `TieredCube`
>    keeps owning `GHRSST_DELTACUBE_PATH`. Manifest-referenced delta is a **P6** candidate (Q5).
> 3. **Production gap example corrected** (§5, §5.1, §12-M0, F6): `2023-01-01..2026-06-26` inclusive is
>    **1 273 days with no gap**, which contradicted the illustrative `gaps: ["2025-06-22"]`. All
>    `day_count` / `gaps` / `day_digest` values are now **derived by a fresh live attrs audit at M0**, never
>    hand-written; F6 uses a **synthetic** gap and no production day is claimed to be one.
> 4. **Fixed calendar block boundaries** (§5.2, §7.7): `S` is 90 **calendar** days from a fixed anchor; a
>    gap never shifts a boundary; seal requires the watermark past the fixed `end_day` **and** every day in
>    the window classified `present` / `confirmed_missing`. Gate **G14**, fixture **F14**.
> 5. **Rollback no longer `os.replace`s the archive** (§9.3): copy predecessor archive → verify → fsync →
>    replace temp onto `manifest.json`, leaving the immutable generation archive intact; plus **§9.3a
>    restore-before-rollback** when a predecessor block is already in hold. Gate **G15**, fixture **F15**.
> 6. **File-count model corrected** (§10.6): ≈ 119 k files per 90-day block, ≈ **+4.9 M** over ten years,
>    ≈ **6.6 M** total — not "~40 metadata files". The comparative conclusion (segmentation ≈ neutral vs an
>    equivalent monolith) stands; new inode / metadata-scan / snapshot-open gate **G16** added to S6/H7.
> 7. **Disk precheck strengthened** (§7.1c): gate on **`projected_free_after ≥ HARD_RESERVE`**, counting
>    unreleased superseded tail generations, the predecessor tail kept as a build input, and delta/daily
>    growth during the build — not `new_block + margin`.
> 8. **Candidate D downgraded from "rejected (S8a-unsafe)" to "not selected / deferred"** (§3.D): S8a proved
>    the risk of a **retargeted data path**, which does not by itself establish that immutable subgroups
>    under atomic root-metadata updates would mix. B remains the recommendation on simplicity and lifecycle
>    affordances, not on a disproof of D.

This spec answers the **P4-S4 §6.1 O2 base-segmentation design gate** that has blocked block-level
compaction since P4-S8 ([`p4_ingest_prune_retention_design.md`](p4_ingest_prune_retention_design.md) §6/§6.1,
[`p4s8_swap_compaction_design.md`](p4s8_swap_compaction_design.md) §10 "O2 status"). It decides whether the
base cube can evolve from one ~2 TB monolith into **immutable, individually replaceable time blocks** so
that folding aged delta days into base costs **one block**, not **one full base rebuild**.

It inherits and does not re-decide: the adopted spatial policy (`SPATIAL_WINDOW_DAYS = 31`, spatial
availability == delta membership — [`p4s0_daily_vs_cube.md`](p4s0_daily_vs_cube.md)), the append-order delta
invariant (P4-S4 §2), the two availability scopes (point = cube ∪ daily; spatial = delta —
[`p4s11_point_availability_regression.md`](p4s11_point_availability_regression.md)), the rebuild-then-swap
primitive ([`p4s6_delta_prune_results.md`](p4s6_delta_prune_results.md)), and the swap-safety verdict
([`p4s8a_swap_executor_results.md`](p4s8a_swap_executor_results.md)).

> ### Evidence provenance — read before trusting any number here
> Numbers tagged **[PROBE]** come from two throwaway probe scripts run **in the session scratchpad**
> (`p5_probe.py`, `p5_probe2.py` — *not* committed, *not* repo code) against **synthetic random float32**
> on a **512×512** grid using the **real production chunk/shard geometry** (`chunks=(90,8,8)`,
> `shards=(90,128,128)`, zarr v3, default compressor), warm, on the development laptop. They are
> **preliminary structural evidence**, adequate to *reject* candidates on hard structural grounds and to
> *shape* the design — they are **NOT** a gate and **NOT** binding performance. **P5-S0 and P5-S6 must
> re-derive every one of them with committed harnesses under `dev2026/bench/`**, and no step may advance on
> a scratch probe alone.
> Numbers tagged **[VM24]** are production measurements already recorded in the repo. Numbers tagged
> **[MODEL]** are arithmetic projections from **[VM24]** / **[PROBE]** inputs, explicitly labelled as
> projections. Numbers tagged **[SPEC]** come from the Zarr v3 specification or zarr-python 3.2.1 source.

---

## 1. Current-state diagnosis

### 1.1 The blocker, stated exactly

`ingest/dual_write.compact()` folds delta days into base by calling
`build_timecube_bulk(..., end_day, overwrite=True)` into `<base>.compact` — a **full staged rebuild of the
entire base**, then an atomic swap. On VM24 that requires **≈ 2 TB free beside the existing ≈ 2 TB base**.
That headroom does not exist and is not provisioned. Consequences already recorded:

- P4-S4 §6 ranked the compaction options and concluded **O4 defer-with-alarm is the only safe strategy on
  the current disk**; O1 (full rebuild) and O3 (second volume) both need ~2 TB.
- P4-S8 §10 left **O2 (block-level) blocked behind the §6.1 design gate** — the gate this spec answers.
- P4-S8 §6 records the consequence: with compaction deferred, **delta prune does not run at all**. Delta
  simply grows, bounded only by the O4 alarm (span > 31 + 14 = 45 days).

So today the system has **no routine path to move an aged day out of delta**. That is the problem P5 exists
to solve; every other P5 property is subordinate to it.

### 1.2 Why the delta cannot simply absorb the backlog

Delta is `time_chunk=1 / spatial_chunk=256 / shard 256` — append-optimal, **read-hostile for long point
series**. A point-day costs one `(1,256,256)` chunk = **262 144 B decompressed per var-day** [MODEL, from the
declared layout in `dual_write.DELTA_SPATIAL_CHUNK`], versus base `(90,8,8)` = **23 040 B per var per
90-day column**, i.e. **256 B per var-day amortized** — a ~1 024× difference in decompressed bytes per
point-day. This is visible in production: a pure-base 366-day range is **76 ms** while a base→delta
**crossing** range is **219 ms** [VM24, v0.5.0 edge results] with delta at only 36 days.

**Therefore delta span is itself a read-latency driver**, and "let delta grow" is not a neutral deferral —
it degrades the primary workload monotonically. Any P5 design must bound delta span, not merely tolerate it.

### 1.3 What the base layout must not lose

`base = s8/t90/shard128` is read-optimal for the **primary** workload (long point series) and is
**bbox-hostile by design** (~90× read amplification, tens of thousands of tiny chunks per bbox —
[`p4s0_daily_vs_cube.md`](p4s0_daily_vs_cube.md)). Re-chunking base for bbox is off the table and remains
so. A point-series read regression is a **veto** for any P5 candidate (P4-S4 §6.1 item 4).

### 1.4 The safety lesson P5 must not re-learn

The P4-S8a proof test established, empirically, that **an atomic path switch is not metadata consistency**:
with a `TimeCubeStore` opened through a retargeted symlink, a pre-refresh read returned **the new target's
bytes against the old `day_index`** (silent wrong value), and a shrunk target returned **silent `None`**.
zarr's `LocalStore` resolves the path **per chunk read**, so cached handles follow a path whose content
changed underneath them. Neither failure raises.

The generalized rule P5 must obey: **a path whose content can change while a reader holds a snapshot of its
metadata is unsafe.** P5's entire consistency model is built on removing that possibility rather than
guarding it — see §8.

### 1.5 Current-state facts this spec builds on

| fact | value | source |
|---|---|---|
| base span / layout | `2023-01-01..2026-06-26`, `s8/t90/shard128`, ~2 TB | README, [VM24] |
| delta span / layout | 36 finalized days, `t1/s256/shard256`, ~37 GB @ 34 days | README, P4-S10 [VM24] |
| daily staging | 38 recent groups; 1 267 historical groups in `hold`, **not** hard-deleted | change_log v0.5.0 [VM24] |
| point availability | base ∪ delta ∪ daily = `2023-01-01..2026-07-28`, 1 305 days | `/healthz` [VM24] |
| spatial availability | delta membership only | `store/spatial_policy.py` |
| grid | 17 999 × 36 000 float32 → **2.59 GB per var-day** uncompressed | P4-S6 OOM incident |
| base bytes/day | ≈ 2 TB / 1 273 d ≈ **1.57 GB/day** (3 vars, compressed) | [MODEL] from [VM24] |
| delta bytes/day | ≈ 37 GB / 34 d ≈ **1.09 GB/day** (3 vars, compressed) | [MODEL] from [VM24] |
| 366-day point range | 76 ms; 365-day 100 ms; crossing 219 ms | v0.5.0 edge [VM24] |
| production swap downtime | 2.791 s (v0.4.1 stop→swap→start) | P4-S10 results [VM24] |

---

## 2. Measurable hypotheses

Each hypothesis is falsifiable, has a named owning step, and a decision consequence. **No P5 step may
advance on argument alone; each of these must be measured by a committed harness.**

| # | hypothesis | measure | owner | consequence if FALSE |
|---|---|---|---|---|
| **H1** | Rewriting one day inside an existing full 90-day base block rewrites ~100 % of that block's shards (≈ 90× write amplification), because every shard spans the full time block. | shard files rewritten + bytes rewritten for a single-day write into a complete block | S0 | Candidate A becomes viable; re-open §3-A. |
| **H2** | Appending one day into a **partial** tail shard rewrites the whole partial shard, so growing a block day-by-day from 1→S days costs ≈ `(S+1)/2 ×` the block's final size. | bytes rewritten per append at tail lengths 1, 15, 30, 60, 89 | S0 | Per-day tail append is affordable → §7 simplifies to incremental extension. |
| **H3** *(scoped by P5-S0 — read the conditions)* | **(i)** At a **constant inner time chunk**, segmentation leaves both `chunk_count` and `decompressed_bytes` invariant. **(ii)** For an **aligned, full-span** read, `decompressed_bytes` is equal across block sizes while `chunk_count` rises as the time chunk shrinks (the §10.4 sizing trade, not a violation). **(iii) Outside (i)/(ii), `decompressed_bytes` is NOT invariant** — it moves with the **request offset** and the **block anchor**, because boundary over-read depends on block geometry. P5-S0 measured **0.333× – 1.036×** segmented/monolith across offsets and anchors: smaller blocks usually decompress *fewer* bytes on a partial window, while an off-anchor grid can decompress *more*. Beyond that, segmentation adds a **per-segment array-call overhead** roughly linear in segments touched. | chunk_count, decompressed_bytes, p50/p95, measured separately in the constant-time-chunk regime, the shrinking-time-chunk regime, **and a segmented-vs-monolith sweep over request offsets × block anchors × S ∈ {30,45,90}** | S0 *(synthetic; establishes anchor-sensitivity)*, **S6 *(adjudicates on the production calendar anchor with representative 1-day / 366-day / crossing windows)*** | If **decompressed bytes** grow materially with segmentation at the production anchor, block sizes < 90 d are vetoed. Note the S0 sweep found the common direction is a *reduction*, so the veto is unlikely to bind — but it must be decided on real anchors, not extrapolated. |
| **H4** | With **immutable versioned block paths**, a reader holding an older snapshot observes **stable-old** values across a manifest generation change — never the S8a silent-mixing or silent-`None` outcome. | adversarial concurrency proof test: reader holds snapshot, publisher publishes generation N+1, reader re-reads pre-refresh | S5 | **P5's central safety claim fails** → segmented publication needs the same PM2 quiescence as a delta swap, and §8's "no-downtime publish" is withdrawn. |
| **H5** | Point/range latency grows monotonically with **delta span** (delta days in the requested range), so bounding delta span is a read-performance requirement, not only a disk one. | p50/p95 for a 366-day range crossing delta at delta spans 31 / 45 / 64 / 90 / 124 days | S0, S6 | Cadence may be relaxed toward C = S (fewer compactions). |
| **H6** | Peak compaction temp disk equals **one block + margin**, independent of total base size, and never approaches a second full base. | measured peak staging bytes + `df` delta during a tail-block build | S3 | The whole premise of P5 fails; fall back to O1/O3 + provisioned disk. |
| **H7** | A segmented read path that **groups requested days by segment** holds open file handles and RSS bounded under C=8/16 with no growth across sustained load, **and the inode/metadata scale of a multi-million-file block tree stays operationally tractable** (§10.6). | open-fd count, RSS, thread count at C=1/4/8/16 over 10 min; **free-inode headroom, metadata-scan wall time, snapshot-open cost at 5/13/41/82/122 segments** | S2, S6, S7 | Segment count must be hard-capped, forcing periodic super-block merges. |
| **H8** | Zarr v3 offers **no** production-safe native mechanism that avoids shard read-modify-write for our read layout (rectilinear shards trade an unacceptable point-read regression for cheap appends). | build time, point-read p50/p95, file count, on-disk bytes for rectilinear-shard vs regular layout | S0 | Candidate E returns; a single mutable base with cheap native appends may beat segmentation. |

---

## 3. Candidate comparison

All five candidates were probed structurally. **[PROBE] numbers are scratch, synthetic, 512², warm** — see
the provenance banner. They are used here to *reject on structure*, which they can do; they are not used to
*accept on performance*, which they cannot.

### 3.A — Single monolithic base, rewrite only the last time block / shard

**Mechanic.** Keep one Zarr array; when folding aged days, rewrite only the shards covering the tail time
block.

**Measured [PROBE].** With `chunks=(90,8,8)`, `shards=(90,128,128)`, a complete 90-day block of 16 shards
(83.68 MB):

| operation | shard files rewritten | bytes rewritten | amplification |
|---|---|---|---|
| overwrite **one day** (t=45) inside the full block | **16 / 16** | **83.68 MB** (100 % of the block) | **90.0×** one day's logical bytes |
| append day 31 into a **partial** 30-day tail shard | 17 | 29.15 MB | **30.6×** |

This confirms **H1** and **H2** and matches the Zarr v3 sharding spec: updating a subset of inner chunks
requires reading the existing shard and re-emitting the whole bytestream; the byte-range partial-write
optimization is permitted **only** for inner chunks of **fixed byte size (e.g. uncompressed)** on a store
supporting partial writes [SPEC]. Our inner chunks are compressed and our store is `LocalStore` → the
optimization is unavailable.

Projected to production [MODEL]: rewriting one day inside a 90-day base block rewrites the whole block
(**≈ 141 GB**); growing a tail block one day at a time costs `Σ(1..90)` day-writes ≈ **45.5 ×** the block's
final size ≈ **6.4 TB of writes per 90 days of data**.

**Additional defect.** Rewriting shards *in place inside the live base* is exactly the
`[MUT-LIVE-OVERWRITE]` class P4-S4 §0.1/§7.1 forbids: a concurrent reader can observe a torn mix of old and
new shard bytes, with no error. Doing it safely would require a staged copy of the block — at which point
the design *is* segmentation, minus the manifest.

**Verdict: REJECTED.** Write amplification is structurally unavoidable, and safe execution collapses into
candidate B anyway.

### 3.B — Immutable segmented base (one versioned Zarr per N days) + atomic manifest

**Mechanic.** Base becomes an ordered set of **immutable, versioned** Zarr stores
`base_blocks/block_<start>_<end>_<version>.zarr` plus a `manifest.json` that names the ordered segments.
Compaction builds a **new** block at a **new** path and publishes a **new manifest generation**. No
published path is ever rewritten, retargeted, or reused.

**Measured [PROBE].** Building a block is linear and cheap; per-day on-disk bytes are flat across block
sizes (929–936 KB/day at 512², i.e. block size does not change storage efficiency):

| block size | build time | files | bytes | bytes/day |
|---|---|---|---|---|
| 30 d | 0.39 s | 18 | 28.07 MB | 935 543 |
| 45 d | 0.42 s | 18 | 41.96 MB | 932 487 |
| 90 d | 0.46 s | 18 | 83.68 MB | 929 796 |

**Read tax measured [PROBE]** — same 1 080 days, same total chunks, cached handles:

| segments | days/segment | read p50 | read p95 | vs monolith | with open-per-request |
|---|---|---|---|---|---|
| 1 | 1 080 | 4.82 ms | 5.10 ms | 1.00× | 5.12 ms |
| 12 | 90 | 12.03 ms | 13.23 ms | 2.50× | 16.39 ms |
| 24 | 45 | 23.72 ms | 24.88 ms | 4.92× | 32.28 ms |
| 36 | 30 | 35.40 ms | 37.51 ms | 7.35× | 47.80 ms |

Two readings matter. (i) The tax is **per segment touched**, ≈ **0.65 ms per extra array call** at this
grid — and total decompressed bytes are unchanged, supporting **H3** *under this measurement's conditions*.
(These rows read the **full span** with the time chunk held at 90, which is exactly case (i)/(ii) of the
scoped H3 in §2. P5-S0 later measured that an **off-anchor or partial** window breaks the byte equality —
0.333×–1.036× — so this row must not be read as unconditional invariance.)
(ii) **Opening the stores per request
adds a further ~30–35 %** (12 segments: 12.03 → 16.39 ms) — so the read path **must** cache handles in the
snapshot and must **never** open a store per day or per request (§6).

**Safety.** Because a published block path is never written again, the S8a failure mode is *structurally
absent*: a cached handle can only ever read the bytes it was opened against. This is the central claim, and
it is **H4** — it must be proven adversarially in P5-S5, not assumed.

**Verdict: RECOMMENDED architecture** (subject to H4 and the S6 sizing gate).

### 3.C — Legacy monolith retained as immutable segment zero + extension blocks

**Mechanic.** Identical to B, with the existing ~2 TB base entered in the manifest as an oversized,
immutable **segment zero** whose exact span and day set come from M0's live audit (§12). All new blocks
cover days **after the legacy segment's `end_day`**, on the fixed calendar grid anchored there (§5.2). The
monolith is never rebuilt.

This is **not a different architecture from B — it is B's migration strategy**, and it is the one that
avoids the ~2 TB rebuild that P5 exists to eliminate. Segment-count projections [MODEL], with
`MAX_DAYS = 366` capping how many segments any single public request can touch:

| horizon | total segments (90-day blocks) | max segments per 366-day request |
|---|---|---|
| today | 1 legacy + 0 | 1 (+delta) |
| +3 y | 1 legacy + 13 | 5 (+delta) |
| +5 y | 1 legacy + 21 | 5 (+delta) |
| +10 y | 1 legacy + 41 | 5 (+delta) |

The per-request segment count is bounded by `MAX_DAYS / S`, **not** by the archive's age — this is the
property that makes segmentation sustainable.

**Verdict: ADOPTED as the migration path for B.**

### 3.D — Segmented groups/arrays inside a single Zarr hierarchy

**Mechanic.** One root Zarr group; each block a subgroup; publication = adding a subgroup and updating root
attrs.

**Concern (not a proof).** Publication requires **mutating the root group's `zarr.json` / attrs in place**
while readers hold snapshots derived from it. There is no atomic all-or-nothing boundary spanning "root
attrs + block subdirectory", so a snapshot built mid-publication could observe an inconsistent root. It also
gives no natural place for the generation / rollback / checksum / superseded-lifecycle fields §5 requires,
and makes a superseded block indistinguishable from a live one on disk.

**Scope correction (round-2).** The P4-S8a proof test demonstrated silent mixing for a **retargeted data
path** — cached chunk handles resolving through a path whose *content* changed. D's subgroups would be
**immutable** like B's blocks; what changes is only the **root metadata**. S8a therefore does **not**
establish that D mixes, and this spec must not claim it does. D's actual liabilities are (a) an unproven
atomicity story for root-attrs mutation under live snapshots, and (b) missing affordances for generation,
rollback and superseded-block lifecycle.

**Verdict: NOT SELECTED — deferred.** B obtains the same immutability with a strictly simpler and already
externally-atomic commit point (`os.replace` of one small file), so D carries added risk for no benefit.
D is **not** ruled out on evidence: reviving it would require its own adversarial proof test (an
S8a-equivalent for root-attrs mutation under concurrent snapshot reads), which P5 does not fund. **B remains
the recommendation.**

### 3.E — Zarr v3 native append / partial-shard techniques

Investigated against primary sources, not prior assumption.

**E1 — sharding partial-write optimization.** The Zarr v3 sharding-indexed codec spec permits replacing an
inner chunk by writing at a byte range **only** "when working with inner chunks that have a fixed byte size
(e.g. uncompressed) and a store that supports partial writes" [SPEC]. Our inner chunks are compressed
(variable size) and the store is `LocalStore`. **Not applicable.** The spec's general path is explicitly
read-shard → re-emit whole bytestream, i.e. RMW — matching the [PROBE] measurement in §3.A.

**E2 — rectilinear (variable-length) chunk grids.** zarr-python **3.2.1** — the version in
`dev2026/.venv` — ships rectilinear chunk grids behind `zarr.config` flag `array.rectilinear_chunks`
(default `False`), documented as **experimental**, expected to stabilize in 3.3. The promise is exactly our
problem: append a new day as a size-1 chunk while leaving 90-day history chunks untouched.

Measured [PROBE]:

- **Rectilinear *chunks* + sharding is rejected by the library**:
  `ValueError: Rectilinear chunks with sharding is not supported. Use rectilinear shards instead:
  chunks=(inner_size, ...), shards=[[shard_sizes], ...]`.
- **Rectilinear *shards* work**, but the inner chunk size is uniform array-wide, so mixing 90-day history
  shards with 1-day tail shards **forces inner time chunk = 1 for the entire array**. Consequences,
  measured against the regular `(90,8,8)/(90,128,128)` baseline on identical data:

| layout | build (150–180 d) | 180-day point read p50 | 1-day point read p50 | files | bytes | 1-day append |
|---|---|---|---|---|---|---|
| regular `(90,8,8)/(90,128,128)` | **0.98 s** | **1.23 ms** | 1.00 ms | 34 | 167.4 MB | — |
| rectilinear shards, inner `(1,8,8)` | 60.75 s | **32.07 ms** | 1.03 ms | 530 | 209.1 MB | 0.375 s, 1.16 MB rewritten |

  The append is indeed cheap (**1.16 MB** rewritten — only the 1-day tail shard). The price is a **26×
  point-series read regression** (1.23 → 32.07 ms), a **62× slower build**, **15.6× more files**, and **25 %
  more bytes** (tiny chunks compress worse). A point-series regression is a **veto** by P4-S4 §6.1.
- Rectilinear chunks without sharding: works, but produced **8 194 files** for 121 days at 512² — a file-count
  explosion at production scale, and it discards sharding entirely.

**E3 — plain `resize()` + append.** Already how delta works and already used by `bulk_append_day`; against a
`time_chunk=90` base it is precisely the RMW measured in §3.A ([`build_timecube_bulk.py`](../ingest/build_timecube_bulk.py)
documents this in `bulk_append_day`'s own docstring).

**Verdict: REJECTED for the base read layout.** Rectilinear shards buy exactly what the delta tier already
buys (cheap appends) at the cost of the thing the base exists for (fast point series) — it is a worse delta,
not a better base. It stays on file as a **possible future delta-tier layout** (§16-Q7), and **H8 must be
re-tested when zarr-python 3.3 stabilizes the feature**, because the constraint that forced inner chunk = 1
is a library limitation, not a spec one.

### 3.x Summary

| candidate | verdict | decisive evidence |
|---|---|---|
| A single base, rewrite tail block/shard | **REJECT** | 90× RMW measured; in-place live shard rewrite forbidden by P4-S4 §7.1 |
| B immutable segmented base + manifest | **RECOMMEND** | temp disk = one block; no path reuse; read tax bounded by `MAX_DAYS/S` |
| C legacy monolith + extension blocks | **ADOPT as B's migration** | avoids the ~2 TB rebuild entirely; segment count per request unchanged |
| D segmented groups inside one hierarchy | **NOT SELECTED (deferred, not disproven)** | root-attrs mutation under live snapshots is **unproven**, not shown unsafe; B gets the same immutability with an already-atomic commit point and the lifecycle fields D lacks |
| E Zarr v3 native append / partial shard | **REJECT (revisit at zarr 3.3)** | partial-write optimization inapplicable (compressed); rectilinear shards cost 26× point-read |

---

## 4. Recommended initial architecture

```
GHRSST_TIMECUBE_PATH        legacy_base.zarr          # segment zero, immutable, NEVER rebuilt
GHRSST_BLOCKS_DIR/          base_blocks/
                              block_20260627_20260924_v1.zarr     # immutable, sealed
                              block_20260925_20261223_v1.zarr     # immutable, sealed
                              block_20261224_20270323_v3.zarr     # immutable, current tail (v3 = 3rd fold)
GHRSST_MANIFEST_PATH        base_blocks/manifest.json             # atomically replaced pointer
                              manifest.gen000017.json             # immutable archived generations
GHRSST_DELTACUBE_PATH       delta.zarr        # NOT in the manifest — owned by TieredCube, as today
```

**Serving view = (`legacy_base` + 0..N immutable extension blocks) + `delta`**, presented to the API as a
single cube abstraction.

### 4.0 Authority split (round-2 correction — the manifest does NOT own delta)

> **The manifest describes the BASE only: the legacy monolith plus the immutable extension blocks. It says
> nothing about delta — no delta path, no cutoff day, no delta precedence.**

`SegmentedCubeStore` is a drop-in replacement for the **base** `TimeCubeStore` inside `TieredCube`, nothing
more. `TieredCube` keeps holding `GHRSST_DELTACUBE_PATH` independently and keeps applying **delta-wins-on-
overlap** in code, exactly as it does today ([`tiered_cube.py`](../store/tiered_cube.py) `point_series`).

Why this matters, concretely: if the manifest also declared a delta path and cutoff, then **two independent
authorities** would describe the same days — a manifest generation could be published claiming a cutoff that
the live delta (which the cron mutates twice daily, outside the manifest's control) no longer matches, and
the read path would have to decide which one is right. That is a new consistency surface with no
corresponding benefit, since delta precedence is already unconditional and needs no configuration.

Consequences carried through this spec: no `delta` object in the manifest (§5); publication never advances a
"delta cutoff" (§7.4); `precedence` in the manifest orders **base segments only** and delta's precedence is
a code property of `TieredCube`, not data (§5.1); delta prune remains entirely governed by the existing
`prune_delta` + `swap_delta` machinery (§7.5). **Manifest-referenced delta is explicitly a P6 candidate**
(§16-Q5), not a P5 deliverable.

### 4.1 Load-bearing rules

Five rules:

1. **Immutable versioned block paths.** A path, once published in a manifest generation, is never written
   to, renamed, retargeted, or reused. A rebuilt tail block gets a **new version suffix and a new path**.
   This is the structural answer to §1.4 / P4-S8a.
2. **Atomic manifest publication, and generation archives are immutable.** The manifest is the single commit
   point. Publication = write `manifest.gen<N+1>.json` (immutable archive, fsync'd) → write a temp copy →
   `os.replace(temp, manifest.json)` (atomic, same-filesystem). **Rollback is the same shape and never
   consumes an archive**: **copy** `manifest.gen<N>.json` → **verify** (parse, checksum, generation) →
   **fsync** (file + directory) → **`os.replace(temp, manifest.json)`**, leaving `manifest.gen<N>.json`
   byte-unchanged (§9.3). `os.replace`-ing an archive directly would rename the archive away and destroy the
   record a second rollback or an audit needs.
3. **One immutable snapshot per request.** `SegmentedCubeStore` builds an immutable `_Meta` (manifest
   generation + per-block handles + per-block `day_index`) and swaps it atomically on refresh — the exact
   discipline `TimeCubeStore._Meta` already uses. A reader captures it once per call.
4. **Block `attrs["days"]` is authoritative for indexing; the manifest is authoritative for membership,
   ordering and precedence — and the two are cross-checked at snapshot build.** A mismatch **fails the
   snapshot build and keeps the previous snapshot** (fail-closed), it never produces a half-built view.
5. **Superseded blocks are moved to hold, never `rm`, and only after a grace period** ≥ refresh TTL + max
   request duration + margin (§8.5). Deleting a block a live snapshot still references would reproduce the
   S8a *silent fabricated null* (missing chunk → fill value → `None`).

**API contract is unchanged.** `HybridRouter` continues to see one cube object. No new public route name;
`X-Store-Route` keeps emitting `cube` / `daily` / `mixed` (§6.6). This is deliberate — §16-Q4 records the
one circumstance that would justify revisiting it.

---

## 5. Manifest schema

```jsonc
{
  "format": "ghrsst.timecube.manifest",
  "version": 1,                                  // schema version, not content version
  "generation": 17,                              // monotonic; the ONLY ordering authority
  "generation_id": "gen000017-20270122T031500Z", // unique, embeds the UTC stamp supplied by the caller
  "created_utc": "2027-01-22T03:15:00Z",         // stamped by the publisher (never Date.now() inside a script)
  "created_by": "p5-compaction/codex-ops",
  "predecessor_generation": 16,                  // rollback target; null only for generation 1
  "predecessor_manifest": "manifest.gen000016.json",

  "grid": {"ny": 17999, "nx": 36000, "region": [0, 17999, 0, 36000]},
  "variables": ["sst", "sst_anomaly", "sea_ice"],   // union across segments; per-segment set may be smaller

  "block_grid": {                                 // FIXED calendar boundaries (§5.2) — blocks are calendar
    "anchor_day": "2026-06-27",                   // windows, NOT "S valid entries"
    "block_days": 90                              // S, in CALENDAR days; boundaries never drift on a gap
  },

  "segments": [                                   // ORDERED, ascending by start_day; order is explicit,
    {                                             // never inferred from filenames or directory listing
      "segment_id": "legacy",
      "kind": "legacy_base",                      // legacy_base | block
      "path": "../mur_timecube_s8_t90_sh128.zarr",// relative to the manifest; resolved once per snapshot
      "immutable": true,
      "start_day": "<from live audit>",           // M0 DERIVES these from the live store; never hand-written
      "end_day": "<from live audit>",
      "day_count": 0,                             // EXACT len(attrs["days"]); never derived from the span
      "gaps": [],                                 // calendar days in [start,end] classified NOT present
      "day_list": null,                           // optional explicit list; when present it wins over gaps
      "layout": {"time_chunk": 90, "spatial_chunk": 8, "shard": [90, 128, 128]},
      "variables": ["sst", "sst_anomaly", "sea_ice"],
      "fingerprint": {
        "algo": "sha256",
        "metadata": "<sha256 of the segment's zarr.json + attrs days/vars/var_valid, canonical JSON>",
        "day_digest": "<sha256 of the sorted day list>"
      },
      "precedence": 0,                            // orders BASE SEGMENTS ONLY (delta is not in this manifest)
      "sealed": true, "supersedes": null,
      "boundary_kind": "legacy"                   // legacy segment zero is exempt from the block grid
    },
    {
      "segment_id": "block_20260627_20260924_v1",
      "kind": "block",
      "path": "block_20260627_20260924_v1.zarr",
      "immutable": true,
      "boundary_kind": "calendar",                // start/end come from block_grid arithmetic, not from data
      "start_day": "2026-06-27", "end_day": "2026-09-24",   // exactly 90 CALENDAR days, inclusive
      "materialized_through": "2026-09-24",       // fold has processed the window through this day
      "day_count": 89,                            // |present| — the block array's T
      "gaps": ["2026-08-03"],                     // |confirmed_missing| — classified, not merely unseen
      "unknown": [],                              // not yet materialized; MUST be [] to seal (§5.3)
      "day_list": null,
      "layout": {"time_chunk": 90, "spatial_chunk": 8, "shard": [90, 128, 128]},
      "variables": ["sst", "sst_anomaly", "sea_ice"],
      "fingerprint": {"algo": "sha256", "metadata": "...", "day_digest": "..."},
      "precedence": 1,
      "sealed": true,                             // §5.3 seal condition met
      "supersedes": null,                         // a rebuilt tail names its predecessor version here,
                                                  // e.g. the v2 entry carries "..._v1"
      "build_provenance": {
        "built_at_utc": "...",                    // DIAGNOSTIC ONLY — never authorizes a prune (§7.5a)
        "source_map_digest": "<sha256 of day -> source_kind/source_path/source_day_index>",
        "materialized_repairs": {                 // per repaired day: WHICH repair this block consumed
          "2026-08-11": {"repair_id": 42, "source_kind": "delta",
                         "source_fingerprint": "<sample digest + var_valid, matches the WAL commit>"}
        }
      }
    },
    {
      // ---- UNSEALED tail block, version 2 of 3 (S=90, C=30): 60 of 90 calendar days materialized ----
      "segment_id": "block_20260925_20261223_v2",
      "kind": "block", "path": "block_20260925_20261223_v2.zarr", "immutable": true,
      "boundary_kind": "calendar",
      "start_day": "2026-09-25", "end_day": "2026-12-23",   // FIXED 90-day window, known in advance
      "materialized_through": "2026-11-23",       // 60 calendar days folded so far
      "day_count": 59,                            // 59 present  (one day confirmed missing)
      "gaps": ["2026-10-14"],                     // 1 confirmed_missing
      "unknown": ["2026-11-24", "...", "2026-12-23"],   // 30 days after materialized_through
      "day_list": null,
      "layout": {"time_chunk": 59, "spatial_chunk": 8, "shard": [59, 128, 128]},   // T = day_count
      "variables": ["sst", "sst_anomaly", "sea_ice"],
      "fingerprint": {"algo": "sha256", "metadata": "...", "day_digest": "..."},
      "precedence": 2,
      "sealed": false,                            // 59 + 1 + 30 == 90 == S  (§5.3 unsealed invariant)
      "supersedes": "block_20260925_20261223_v1"
    }
  ],

  "superseded": [                                 // CUMULATIVE: every superseded block NOT yet hard-deleted,
                                                  // carried forward across generations (§5.6)
    {"segment_id": "block_20261224_20270122_v2", "path": "block_..._v2.zarr",
     "superseded_at_generation": 17,              // when it LEFT `segments` (may be far behind `generation`)
     "release_after_utc": "2027-01-22T03:35:00Z", // >= refresh TTL + max request duration + margin (Q11)
     "hold_until_utc": "2027-02-05T03:15:00Z",    // move-to-hold expiry; hard delete is ops-only
     "status": "referenced",                      // referenced -> releasable -> held -> (ops) deleted
     "current_path": "block_..._v2.zarr",         // original path, or the hold path once status == held
     "bytes": 94000000000,                        // for the §7.1c pinned_existing forecast
     "fingerprint": {"algo": "sha256", "day_digest": "..."}}   // verified on restore (§9.3a)
  ],

  "manifest_checksum": "<sha256 over this document with this field set to \"\">"
}
```

### 5.1 Resolution rules (normative)

- **Scope.** These rules govern **base segments only**. Delta is not described by the manifest (§4.0); the
  delta-wins-on-overlap rule stays where it already lives, in `TieredCube.point_series`.
- **Overlap.** Higher `precedence` wins for a given day; a block always wins over `legacy_base`. Overlap
  between base segments is *legal* (it is the compaction window) and must be resolved silently and
  deterministically, never reported as an error. Overlap **between the resolved base view and delta** is
  likewise legal and resolves to delta — in code, unconditionally, as today.
- **Duplicate days *within* one precedence level are illegal.** If two segments at the same precedence claim
  the same day → **snapshot build fails**, previous snapshot retained, alarm. There is no "pick one" rule.
- **Gaps.** A calendar day absent from every base segment (and, one level up in `TieredCube`, from delta and
  daily) is simply *not available*; it is never
  fabricated, never interpolated, and never silently skipped inside a returned range — the existing
  omit-the-row semantics apply (a range returns only the days it has, in requested order).
- **Ordering.** The `segments` array order is normative and must be ascending, non-duplicating by
  `start_day`. Readers must **not** infer order from filenames or directory listing.
- **`day_count` is exact and mandatory** and is cross-checked against the segment's own
  `len(attrs["days"])` at snapshot build. **Never derive a day count from `end_day − start_day`**: a
  calendar block is a fixed 90-day window (§5.2) whose `day_count` is `|present|` and is below `S` whenever
  the window contains any `confirmed_missing` **or** `unknown` day (§5.3). A segment's declared span says
  nothing about how many days it actually holds — that is the whole reason an unsealed tail block is
  representable at all.
- **Every `day_count` / `gaps` / `day_digest` value is DERIVED from a live audit of the segment's own
  `attrs["days"]`, never authored by hand.** For the legacy segment this is M0's job (§12): read the live
  monolith, emit the exact day set, and record whatever gaps it actually has — which may well be none.
  Deriving these fields is also what makes the §5.1 stale-manifest check meaningful; a hand-written count
  would simply encode the author's belief and then validate against itself at build time.
- **Stale-manifest detection.** At snapshot build, for every segment: the path exists, is a readable Zarr
  group, its `attrs["days"]` set matches the manifest's declared **present** set (`day_list`, else
  `[start..end] − gaps − unknown`), its `len(days) == day_count`, its `fingerprint.day_digest` matches, and
  the §5.3 sealed/unsealed invariant holds. Any mismatch → **fail closed**: keep the previous snapshot, emit
  a structured alarm, do not serve a partial view. `manifest_checksum` is verified before any of this.
- **Precedence is stored, not computed.** It appears in the manifest so a rollback generation restores the
  precedence that generation was validated with.

### 5.2 Fixed calendar block boundaries (normative)

> **A block is a fixed 90-**calendar**-day window, not "90 valid entries".** Boundaries are pure arithmetic
> on `block_grid.anchor_day` + `k × block_grid.block_days`, and are **fully determined before any data is
> read**. A missing day must never shift the boundary of the block it falls in, nor of any later block.

- `block_k = [anchor + k·S, anchor + (k+1)·S − 1]` inclusive, in calendar days. `start_day` / `end_day` for
  a `boundary_kind: "calendar"` segment must equal that arithmetic exactly; a mismatch is a manifest
  validation error.
- **Consequence for sizing.** Because boundaries are calendar-fixed, the §10 model's `S` and `C` are
  calendar quantities and gaps do not perturb them. The legacy segment is `boundary_kind: "legacy"` and is
  exempt — it predates the grid, and `anchor_day` is chosen as the day after its `end_day`.

### 5.3 Three-way day classification: `present` / `confirmed_missing` / `unknown` (normative)

**The problem this fixes (round-3).** A fixed 90-day window is declared the moment the grid is anchored, but
under `C = 30` it is materialized in three passes (30 → 60 → 90 days). A two-way `present` +
`confirmed_missing == S` invariant is therefore **unsatisfiable for v1 and v2**: 30 present + 0 missing ≠ 90,
and the remaining 60 days are not "missing" — most have not happened yet. The schema needs a third state.

Every calendar day in `[start_day, end_day]` is classified as **exactly one** of:

| state | meaning | in the block array? |
|---|---|---|
| `present` | folded, validated, readable from this block | yes — contributes to `T` |
| `confirmed_missing` | materialized and **proven** absent (evidence recorded, see below) | no |
| `unknown` | **not yet materialized** — a future day, or an aged day this fold did not target | no |

**Invariants** (checked at manifest validation, both directions):

```
unsealed:   |present| + |confirmed_missing| + |unknown|  ==  S      and  |unknown| > 0
sealed:     |unknown| == 0   and   |present| + |confirmed_missing| == S
always:     day_count == |present| == len(segment.attrs["days"])
always:     the three sets are pairwise disjoint and their union is the full calendar window
always:     every day > materialized_through is in `unknown`
```

`materialized_through` is the day up to which the fold has processed the window. Days after it are
necessarily `unknown`. A day **at or before** `materialized_through` may still be `unknown` only in the
explicit "resolution pending" case (e.g. absent from every source but a re-download decision is outstanding,
§16-Q13); that keeps the block honest instead of prematurely recording a permanent gap.

**A block is never "wrong" for having unknown days** — it is simply incomplete, and the manifest says so.
Reads are unaffected: the block serves exactly its `present` days, and any `unknown` day that exists is
served by delta (or, during ingest lag, daily) via the normal precedence — which is why publishing an
unsealed block is a **no-op for the served view**.

### 5.4 Seal condition

`sealed: true` may be published only when **both** hold:

1. the ingest **watermark** (`max` day available anywhere in delta ∪ daily staging) is **strictly past**
   `end_day` by at least `SEAL_LAG_DAYS` (default = `SPATIAL_WINDOW_DAYS` = 31, §16-Q10 — so a sealed block
   can never contain a day still inside the protected spatial window); **and**
2. **`|unknown| == 0`** — every calendar day in the window is `present` or `confirmed_missing`.

A block failing (2) stays `sealed: false` and is republished as a later version when the outstanding days
resolve. **A manifest carrying `sealed: true` together with a non-empty `unknown` is invalid and must be
rejected by validation** — it is not a warning, it is a schema violation.

**`confirmed_missing` requires evidence**, recorded in the build artifact: the day is absent from delta,
from daily staging, from hold, and (where the SLA applies, §16-Q13) from a NetCDF re-download attempt. "We
did not look" is `unknown`, never `confirmed_missing`. A wrongly-sealed gap is **recoverable only via a
corrective refold** (§7.8) — a full-block rebuild — so the evidence bar exists to keep that an exception
rather than a routine cost.

### 5.5 Two distinct day sets per fold: `classification_target` vs `rebuild_source_set`

**The contradiction this fixes (round-4).** Round-3 said the source map covers only
`(previous materialized_through, new materialized_through]`. For v2 that is **B only** — but v2 is a *new
immutable block* that must physically contain **A + B**, and F13/F13b require **A to be re-resolved** so a
late correction in delta overrides predecessor v1. One set cannot serve both purposes. There are two:

| set | contents | purpose | source required? |
|---|---|---|---|
| **`classification_target`** | the **newly aged** days of this fold: `(previous materialized_through, new materialized_through] ∩ window` | resolve `unknown` → `present` \| `confirmed_missing`, and advance `materialized_through` | resolved as part of classification |
| **`rebuild_source_set`** | **every `present` day of the successor block** — i.e. the complete materialized successor view: predecessor's `present` days **plus** the days this fold newly classified `present` | every slab the successor must physically write | **yes — one source-map entry per day, no exceptions** |
| `unknown` | days after `materialized_through` (plus any resolution-pending day) | none this fold | **no source; never demanded** |

Normative consequences:

- **`classification_target ∩ rebuild_source_set`** = the newly-classified-`present` days.
  `rebuild_source_set ⊇` the predecessor's `present` set — **it is not a delta of the previous fold.**
- **Every day in `rebuild_source_set` must have a source-map entry, resolved fresh at each build** against
  the current serving authority (§7.1a). A single unresolved day → **refuse before writing anything**.
- **No `unknown` day is ever given a source**, and the "refuse if unresolved" rule never applies to it.
- **Re-check of `confirmed_missing` (same principle as F13b).** For an **unsealed** block, days previously
  classified `confirmed_missing` are re-checked by a cheap membership test (no slab read) against the
  current authorities; a day that now exists is **promoted to `present`** and joins `rebuild_source_set`.
  Without this, a day that was genuinely absent at v1 but backfilled afterwards would be frozen as a
  permanent gap by the sealing fold while delta was still serving it — and lost at the next prune, which is
  exactly the F13b failure in a different costume.
- **Sealed blocks are not re-checked by a ROUTINE fold** (that is a cost decision, not an immutability one).
  A correction that lands inside an already-sealed window is handled by an explicit **corrective refold**
  (§7.8), which publishes a **new immutable version of the same fixed calendar block**. Immutability applies
  to the **path**, never to the logical content of a calendar window:

  > **A sealed block's path is never modified. Corrections are represented by a new immutable version that
  > supersedes it.**
- **Cost note (no model change):** because `rebuild_source_set` is the *whole* successor view, each fold
  writes the full materialized block. That is what §10.2's amplification model `(S/C + 1)/2` and §7.1c's
  worked example already assume (v1 → 47 GB, v2 → 94 GB, v3 → 141 GB), so §10/§11 are unchanged.

### 5.6 `superseded[]` is cumulative, not per-generation

**The gap this fixes (round-4).** Round-3's schema comment described `superseded[]` as the blocks removed
"in THIS generation". But three separate mechanisms need the **whole** non-deleted history:

- **rollback** (§9.3) to generation `N` needs every block generation `N` referenced, including ones
  superseded several generations ago;
- **restore-before-rollback** (§9.3a) needs each such block's hold path and `day_digest`;
- the **`pinned_existing` forecast** (§7.1c) needs every block still occupying disk.

A per-generation list would force replaying a separate log to answer any of these, and a manifest generation
that cannot describe its own rollback prerequisites is not a usable rollback target.

**Rule (normative).** `superseded[]` carries **every superseded block that has not yet been hard-deleted**,
carried forward unchanged from generation to generation. Entries are:

- **added** when a block leaves `segments` (recording `superseded_at_generation`, timers, `bytes`,
  `fingerprint`);
- **updated in place** as `status` advances `referenced → releasable → held` (a `held` entry updates
  `current_path` to its hold path);
- **removed only** when ops hard-deletes the block after `hold_until_utc` — and that removal is itself a new
  generation, so the deletion is visible in the generation history rather than silently rewriting the past.

**Validation.** At snapshot build, every `superseded[]` entry must resolve at its `current_path`. A missing
entry does **not** fail the snapshot — superseded blocks are never served, so serving is unaffected — but it
**must alarm** and must mark rollback to any generation referencing that block as **unavailable**. This is a
deliberate asymmetry with §5.1's fail-closed rule: fail-closed protects *correctness of what is served*;
this protects *recoverability*, and blocking serving over a missing rollback artifact would be the wrong
trade.

**Audit trail.** The append-only manifest log remains the chronological record (`op: block_superseded`,
`hold_move`, `hold_restore`, `hard_delete`, `manifest_rollback`). The manifest's cumulative `superseded[]`
is the **self-contained state** a single generation needs; the log is the **history**. Both exist; neither
replaces the other.

---

## 6. Read-path design

### 6.1 Component placement

`SegmentedCubeStore` is a **new** store class exposing exactly the surface `TimeCubeStore` already offers
`TieredCube` as its **base** (`days`, `day_count`, `latest`, `covers_days`, `point_series`, `refresh`,
`maybe_refresh`, `_day_index`). `TieredCube` is generalized so that its `base` may be **either** a
`TimeCubeStore` (today) **or** a `SegmentedCubeStore` (P5) — and `TieredCube` keeps owning
`GHRSST_DELTACUBE_PATH` and the delta-precedence merge unchanged (§4.0). **`SegmentedCubeStore` never sees
delta and never reads the delta path.**

**`TimeCubeStore` is not modified**: each segment *is* a `TimeCubeStore`, which preserves per-segment
`_Meta` immutability, per-segment `day_index` (the append-order invariant), and per-segment `var_valid`
semantics for free.

### 6.2 The grouping rule (hard requirement)

> **Never open a store per day, and never issue one array call per day.** The segment router must
> (a) resolve every requested day to a segment **once**, using the snapshot's precomputed
> `day → (segment_idx, day_index)` map, (b) **group** the days by segment, and (c) issue **one**
> `point_series` call per `(segment, var)` — exactly the shape `TieredCube.point_series` already uses for
> base/delta.

[PROBE] justification: the same 1 080-day read costs 12.03 ms with 12 cached segment handles but 16.39 ms
when the segments are opened per request (+36 %); a per-day open would be ~90× worse still.

### 6.3 Required cases

| case | resolution |
|---|---|
| **single historical point** | one map lookup → one segment → one `point_series` call. Must remain `X-Store-Route: cube`. **Chunk count identical to today's monolith; decompressed bytes are ≤ the monolith's** and depend on the block's time chunk — a single day costs 90 day-cells from a `t90` monolith but `S'` from an `S'`-day block (P5-S0 measured 90 → 30 at S=30). Not "identical" [H3(iii)]. |
| **366-day range inside one block** | one segment, one call per var. Byte cost equals the monolith's only when the window is aligned and full-span; an off-anchor window differs (§2 H3(iii)). |
| **366-day range spanning several blocks** | days grouped by segment; ≤ `ceil(366/S) + 1` segments touched (5 + delta at S=90); rows merged **in requested order**, delta precedence applied. |
| **legacy → extension block → delta crossing range** | three precedence levels in one request; resolution is by precedence per day, not by tier order; result order is the requested order. |
| **newest day present only in daily staging (ingest lag)** | unchanged: `HybridRouter.route_point` returns `mixed` and merges cube days with daily days. Segmentation is invisible to this path. |
| **absent variable across a block boundary** | each segment carries its own `vars` + `var_valid`. A var absent in segment k is **omitted** for that segment's days while still being returned for other segments' days — per-day omit semantics, not per-request. This is the highest-risk parity case in P5 (§13). |
| **present NaN** | `null`, unchanged. |
| **day in no segment and not in delta/daily** | row omitted; the range's availability message comes from `HybridRouter.point_bounds()` as today. |

### 6.4 Snapshot contents

The `SegmentedCubeStore._Meta` is an immutable `NamedTuple` holding: `generation`, `generation_id`, the
ordered segment handles (each a live `TimeCubeStore`), the merged `day → (segment_idx, local_day_index)` map
with **base-segment** precedence already applied, the chronological `days` list (availability view only —
never used to index an array), `latest = max(days)`, and the union `vars`. **No delta state appears in this
snapshot**; delta is `TieredCube`'s, and its own `_Meta` refreshes independently as it does today. The
segmented snapshot is built entirely before publication and swapped under the lock, exactly as
`TimeCubeStore.refresh()` does.

### 6.5 Availability scopes — unchanged

- **Point availability** = `SegmentedCubeStore.days ∪ delta.days ∪ daily.existing_days()`, surfaced through
  `HybridRouter.point_days / point_bounds / point_primary_bounds / point_day_present`. **Never**
  `StoreAccess.bounds()` alone (P4-S11).
- **Spatial availability** = **delta membership only**, and `_spatial_window_gate` continues to run
  **before** any daily-availability check (commit `5099613`). **No block is ever spatially served** — base
  blocks inherit the monolith's `s8/t90` bbox hostility (≈ 90× read amplification), so segmentation does not
  and must not widen the spatial contract.

### 6.6 Route naming

`X-Store-Route` continues to report `cube` for any request served entirely by legacy+blocks+delta, `daily`,
or `mixed`. **Segmentation is an internal storage detail and gets no public route name.** Per-segment
attribution is exposed for observability only, via `/healthz` (`cube_kind`, `segment_count`,
`manifest_generation`, `segments_touched_p95`) and structured logs — never as a contract surface.

### 6.7 TTL refresh

`maybe_refresh(ttl)` re-reads `manifest.json` first; if `generation` and `manifest_checksum` are unchanged,
**no segment is reopened** (cheap no-op — one small file read per TTL). If the generation advanced, a full
new snapshot is built off-lock and swapped atomically. This preserves the existing
`GHRSST_CUBE_REFRESH_TTL_SECONDS=300` behavior and the invariant that refresh exposes only validated state.

---

## 7. Compaction algorithm

Envelope tags follow P4-S4 §0.1: **[RO]**, **[MUT-STG]** (new staging path beside live), **[MUT-SWAP]**
(atomic publication), **[MUT-LIVE]** (touches the serving process), **[MUT-HOLD]** (move-to-hold, never
delete).

### 7.1 Preconditions [RO] — all must hold, fail-closed

1. **Fresh audit**, never a reused day set (P4-S10 §2 rule): recent delta window contiguous, no duplicate
   days, `/healthz` metadata not stale.
2. **Fold cutoff respects the spatial window**: only days `< max(delta.days) − (SPATIAL_WINDOW_DAYS − 1)`
   calendar days may be folded. `SPATIAL_WINDOW_DAYS = 31` remains an API rule — **no day inside the
   protected window may leave delta**, because no bbox-friendly tier would replace it.
3. **Per-day source map resolved and recorded** — see §7.1a. Every day to fold must have a *named, verified*
   source before any byte is written; the plan records it per day. **The algorithm must never require a
   full-history daily store — it no longer exists** (1 267 groups moved to hold, v0.5.0).
4. **Disk precheck** — see §7.1c. Gate is on **projected free-after**, not on the new block alone.
5. **Ingest lock** (`flock` on the shared `mur_delta.ingest.lock`) held for the publish critical section.

### 7.1a Per-day source map — resolve every `rebuild_source_set` day against the CURRENT serving authority

> **A tail rebuild (v1 → v2 → v3) is NOT "re-fold everything from delta", and it is NOT "copy the
> predecessor block". It re-resolves, per day, whatever is CURRENTLY the serving authority.**

Two facts have to be satisfied at once. First, after v1 is published and verified, **delta is pruned of
days A** (§7.5) — that is the entire point of the fold. Days A are then gone from delta, daily staging holds
only a recent window, `hold` expires, and NetCDF re-download is a slow external backstop. So by the time v2
is built, **the published block v1 is the only routine local source for days A**. Second, a day that was
folded and pruned **can come back into delta** through the explicit repair path (P4-S4 §7.1
`repair_overwrite` via rebuild-then-swap). When it does, **delta is what the API serves for that day** —
so a successor block that blindly copied the predecessor would silently freeze the stale value, and the
correction would be lost for good at the next delta prune.

**Rule (round-3): source precedence == current serving authority.**

> **delta membership → currently published block covering the day → daily staging → hold (inside its
> approved rollback window) → NetCDF re-download (recovery only, explicitly enabled, never routine).**

This is deliberately the same precedence the read path applies (`TieredCube`: delta wins, else base), so a
fold can be stated in one line: **a fold materializes into an immutable block exactly what the API is
serving today.**

| version | `classification_target` (newly aged) | `rebuild_source_set` (every present day written) | source resolved per day |
|---|---|---|---|
| v1 | A (days 1–30, now aged past the spatial window) | **A** | A → **delta** |
| v2 | B (days 31–60) | **A + B** | A → **block v1** if A is no longer in delta; A → **delta** if A was repaired back; B → **delta** |
| v3 (seals) | C (days 61–90) | **A + B + C** | same rule applied per day, re-resolved from scratch |

**Rules.**
- The builder resolves a **source map** `day → (source_kind, source_path, source_day_index)` for **every day
  in `rebuild_source_set`** (§5.5 — the *complete* materialized successor view, **not** a delta of the
  previous fold, and **never** the `unknown` days) **before writing anything**, and refuses to start if any
  such day is unresolved.
- Reading from the predecessor block is a **read of an immutable published path** — the same safety class as
  a serving read, and it is *not* a mutation of that block. The predecessor is superseded only at
  publication (§8.5), never during the build.
- The source map, with per-day `source_kind`, is written into the build artifact and into the manifest's
  build provenance, so "where did each day in this block actually come from" is auditable after the fact.
  A day whose `source_kind` **changes** between versions (block → delta) is a **late correction** and is
  logged as such.
- **Because a rebuilt tail may read its predecessor, the predecessor must still exist**: the §8.5
  supersession lifecycle keeps the immediately-preceding tail version available until the successor is
  published and verified.
- **Carry-forward parity obligation (round-3 definition):** for **every day in `rebuild_source_set`**, the
  successor's value must equal **the currently served authoritative value at build time** — *not* "equal to
  the predecessor block". When no correction happened these coincide, and the successor is a byte-faithful
  carry-forward; when a correction happened, the successor must show the **corrected** value. Both
  directions are tested (§13.1 F13 / F13b).

### 7.1b Build isolation — a held kernel `flock`, not a renewable TTL lease

**The hazard (round-3).** The block build runs **outside** the ingest lock, for hours, and (per §7.1a) it
**reads from the live delta**. Meanwhile `prune_delta` + `swap_delta` **retarget the live delta path**
(rename/double-rename). That is precisely the P4-S8a configuration: a live handle whose path content changes
underneath it, returning **new bytes against an old `day_index`** with no error. The consequence here is
strictly worse than a bad request — the wrong bytes would be **frozen into a new immutable block** and
published as history.

Holding the *ingest* lock for the whole build is not an option: it would block the twice-daily delta append
for hours.

**Why the round-3 renewable TTL lease was wrong (round-4 correction).** A TTL lease with a heartbeat has a
**split-brain hole**: a holder that is paused (`SIGSTOP`, a long GC/IO stall, a suspended VM) lets its lease
expire; ops or another run then legitimately takes over and performs a delta swap; the original process
resumes and — with nothing preventing it — renews its lease and publishes a block built from bytes that were
retargeted underneath it. "Same `lease_id`, no *observed* expiry gap" is not a fencing property: the paused
holder observed nothing at all. Making TTL safe requires a monotonic fence token with atomic CAS on
acquire/renew/break, which is real distributed-systems machinery for a problem we do not have.

**Design (adopted): one dedicated kernel `flock`, held for the whole build.**

- Lock file `<data_root>/p5_compaction.lock` (path from env, never hardcoded), **separate from the ingest
  lock** `mur_delta.ingest.lock`.
- The builder opens it once and holds `flock(fd, LOCK_EX)` for the **entire build**, from before the source
  map is resolved until after publication and verification. The fd stays open in the building process.
- Everything else is observability: a sidecar `p5_compaction.status.json` (holder, run id, target window,
  started-at, last progress) carries **no authority whatsoever** — it is for `ps`-level human context only,
  and no code may branch on it.

| actor | behaviour while the compaction lock is held |
|---|---|
| **delta prune / swap / any delta path retarget** | attempt `flock(LOCK_EX \| LOCK_NB)` on the **same file**; on `EWOULDBLOCK` → **refuse** with `refused: compaction_lock_held` (+ the status sidecar's contents for the operator message). **Non-blocking refusal, never a blocking wait.** New fail-closed gate at the `prune_delta` / `execute_swap_plan` entry points. |
| **repair overwrite** (`repair_overwrite` via rebuild-then-swap) | retargets the path ⇒ **refused**, same as a prune. A repair needed mid-build aborts the build instead (a discarded build is cheap; a torn block is not). |
| **daily delta append** (`[MUT-LIVE-APPEND]`) | **does NOT take this lock** and continues normally. It appends a *new* day (finalize-`days`-last, never overwrites a visible day, never retargets the path), and new days are recent — outside any fold window by ≥ `SPATIAL_WINDOW_DAYS`. It cannot affect the build. |
| **manifest publication** | still takes the **short ingest lock** (§7.4) *in addition to* the compaction lock it already holds. |

**Why this fences correctly, with no TTL:**

- **A paused holder still holds the lock.** `SIGSTOP`, a stalled IO, a suspended VM — the kernel keeps the
  `flock` because the fd is still open. A prune attempted meanwhile is refused. That is the *correct*
  outcome, and it is exactly the case a TTL lease gets wrong.
- **A dead holder releases the lock automatically.** Process exit (including `SIGKILL`) closes the fd and the
  kernel drops the lock. There is no stale-lease state to reason about and nothing for ops to expire.
- **There is no renew path, so there is no way for an old holder to become valid again.** Validity is
  identical to "this process still exists and still holds the fd".

**Publish-time assertion (the fencing check that remains necessary).** Under the ingest lock, before
publishing, the builder asserts:

1. the lock fd is **still open and still held** by this process; **and**
2. `os.fstat(fd).st_ino == os.stat(lock_path).st_ino` — the lock **file itself** has not been deleted or
   replaced underneath us. This matters because `rm` + recreate produces a *new inode* that another process
   can lock freely while we still hold a lock on the orphaned one — the only way two writers can coexist
   here. A mismatch → **abort, publish nothing, discard the staging block.**

**Operational rules for a wedged build (must be in the S8 runbook):**

- To clear a stuck build, **kill the process**. The kernel then releases the lock.
- **Never `rm` the lock file to "unstick" it** — deleting it does not release the holder's lock and creates
  the two-inode split-brain described above. The publish-time inode check turns that mistake into a refusal
  rather than a corrupt block, but the mistake must not be made.
- **fd ownership across workers — see §7.1b-1.** (Round-4 said helper processes "must inherit the fd", which
  contradicts "process death releases the lock": an inherited fd keeps the lock alive after the parent dies.
  Corrected below.)
- **`flock` is only reliable on a local filesystem.** The S8 read-only runbook must record the filesystem
  type and `st_dev` for `<data_root>` and confirm it is local, not NFS. If it is ever not, this design
  reverts to the snapshot alternative below.

#### 7.1b-1 Lock-fd ownership: supervisor-only, never inherited (round-5)

`flock` is held per **open file description**. If a child inherits the fd, killing the parent does **not**
release the lock — the two round-4 statements ("process death releases the lock" and "helpers must inherit
the fd") are incompatible. The resolution:

- **The build's default shape is threads in a single process** (`ThreadPoolExecutor`, as in
  `build_timecube_bulk` / `prune_delta` today). Threads share the fd naturally, there is no inheritance
  question, and this is the recommended implementation. **No child processes at all** is the simplest way to
  satisfy everything below.
- **If child processes are ever used**, then normatively:
  1. **Only the supervisor (parent) owns the compaction-lock fd.** It is opened with `O_CLOEXEC` /
     `os.set_inheritable(fd, False)` so **no worker inherits it**.
  2. **Workers may write the staging block only.** They **cannot publish** — publication (manifest write +
     `os.replace`) is supervisor-only, and the supervisor is the only party that can assert the §7.1b
     held-and-inode condition.
  3. **The supervisor stays alive until every worker has finished** and is the only process that releases
     the lock.
  4. **Supervisor death releases the lock**, and any surviving worker must be **terminated or rendered
     unable to publish** — which rule 2 already guarantees structurally. An orphaned worker can at worst
     corrupt an **unpublished staging tree**, never a published block.
  5. **A resume must re-acquire the lock and revalidate** a staging tree whose owning build was orphaned —
     it may not trust the checkpoint frontier of a build that died while the delta path may have been
     retargeted. (This composes with the existing rule that resume is valid only under the same code
     version.)
- **Runbook rule when inheritance cannot be avoided:** clearing a stuck build must kill the **entire process
  group / cgroup**, not just the parent, and must then **verify that a fresh non-blocking
  `flock(LOCK_EX|LOCK_NB)` succeeds** before any prune/swap proceeds. A successful test acquisition — not
  the absence of a parent process — is the proof that the lock is free.

**If a TTL lease is nonetheless preferred (not recommended), it must specify:** a monotonic **fence token**
persisted alongside the lease; **atomic exclusive acquire** and **CAS renew** (renew succeeds only if the
stored token still equals the holder's token); **break = CAS-increment the token**, which permanently
invalidates the previous holder; and a **publish-time check under the ingest lock that the current stored
token is still the holder's** — i.e. the holder owns the highest fence. Split-brain tests are mandatory
(§13.1 **F16b**).

**Alternative if ops rejects the refusal window: an immutable delta source snapshot.** Clone the delta at
build start into a versioned path the build reads exclusively — **metadata files copied** (zarr.json / attrs
are rewritten in place by appends, so they must not be shared) and **chunk/shard files hardlinked**
(append-only delta never rewrites an existing shard, and a hardlinked tree survives the swap's rename of the
original). This lifts the constraint on prune/swap entirely, at the cost of an inode-heavy clone. **It must
be measured, not assumed** — P5-S3 reports clone wall time, inode count and disk delta; P5-S5 must prove the
clone returns **stable-old values** across a concurrent delta swap (a value-level S8a-style assertion, not
merely "no exception"). The `flock` is the recommended primary because it is ~zero-cost and needs no new
consistency reasoning.

**Ordering (normative).**

```
flock(p5_compaction.lock, LOCK_EX)  →  disk precheck (§7.1c)  →  resolve source map (§7.1a)
   →  BUILD (hours; lock simply stays held — no heartbeat, no renewal)
   →  acquire ingest lock  →  staleness guard  →  ASSERT lock still held AND lock-file inode unchanged
   →  publish (§7.4)  →  release ingest lock  →  verify (§7.6)  →  release compaction lock
```

**§16-Q14 / Q15** record the mechanism choice and the wedged-build procedure for sign-off.

### 7.1c Disk precheck [RO] — gate on additional allocation, not on bytes already spent

Two errors to avoid, in opposite directions. Asking only "does one block fit" ignores that delta keeps
growing through a multi-hour build and that the serving system needs a floor. But **counting the predecessor
and superseded blocks as part of `required` double-counts them**: those bytes are *already on disk*, so
`free_now` (from `df`) has **already** subtracted them. Charging them again makes the gate progressively
stricter each fold and would wrongly refuse the very sealing fold that ends the cycle.

> **The gate subtracts only ADDITIONAL allocation this run will make. Bytes already spent are already
> reflected in `free_now`.**

```
additional  =  new_staging_block_estimate          # the only large NEW allocation
             + temp_checkpoint_artifacts           # progress JSONL, checkpoint, plan/manifest candidates
             + growth_during_build                 # measured ingest rate x measured build duration

gate:  free_now - additional  >=  HARD_RESERVE      # HARD_RESERVE = the floor the SERVING system needs,
                                                    # 200-300 GB, tuned with ops (P4-S4 §6)
```

- `growth_during_build` uses the **measured** build rate (P5-S3) and the **measured** ingest rate, not
  assumed values.
- **Predecessor and superseded blocks are `pinned_existing`, not `additional`.** They are reported, and they
  drive the *forecast* below, but they are never subtracted from `free_now` a second time.
- It must **never** be satisfiable only by a second full base — that requirement appearing at all is a P5
  bug, not an ops condition (**H6**).
- Refuse + alarm otherwise; record `free_now`, every term of `additional`, `pinned_existing`, and
  `projected_free_after` in the plan artifact.
- **The delta prune that follows a fold has its own, separate precheck** (P4-S10 §2: free ≥ live delta × 2 +
  100 GB, because rebuild-then-swap stages a second delta). It is not folded into this gate.

**Pinned-existing lifecycle forecast (reporting + alarm, NOT a gate).** Separately from the gate, each run
records `pinned_existing` (predecessor tail + unreleased superseded blocks + hold contents) together with
each entry's `release_after_utc` / `hold_until_utc`, and projects free disk at +1 / +3 / +6 fold cycles
assuming those releases happen on schedule. **Alarm** when the forecast dips below `HARD_RESERVE` inside the
horizon — that is the signal to release, hard-delete expired hold entries (ops-only), or provision disk.
This keeps the growth visible without letting it veto a run whose own allocation is affordable.

#### Worked example — S = 90, C = 30, one full cycle [MODEL, illustrative]

Inputs: base 1.57 GB/day ⇒ 30 d ≈ 47 GB, 60 d ≈ 94 GB, 90 d ≈ 141 GB. `HARD_RESERVE = 250 GB`. `free_now`
is **measured by `df` at each fold** — the values below are illustrative and consume ≈ 80 GB per cycle
(newly pinned block ≈ 47 GB + delta growth ≈ 33 GB). **The real starting figure comes from the P5-S8
read-only VM24 audit; nothing here asserts VM24's actual free disk.**

| fold | materialized | staging block | temp | growth | **additional** | `free_now` | **`free_after` (correct)** | gate | `pinned_existing` (reported, not charged) | `free_after` under the WRONG formula |
|---|---|---|---|---|---|---|---|---|---|---|
| **v1** | 30 d | 47 | 2 | 3 | **52** | 600 | **548** | ✅ | — | 548 ✅ |
| **v2** | 60 d | 94 | 3 | 5 | **102** | 520 | **418** | ✅ | v1 block 47 | 418 − 47 = 371 ✅ (false pressure) |
| **v3 (seals)** | 90 d | 141 | 4 | 7 | **152** | 440 | **288** | ✅ | v2 block 94 + superseded v1 47 | 288 − 94 − 47 = **147 ❌ wrongly refused** |

The corrected gate passes all three folds, including the sealing fold, with 288 GB left against a 250 GB
reserve. The double-counting formula would have **blocked v3 — the very fold that seals the block and ends
the cycle** — while the disk it was worried about was already accounted for in `free_now`. After v3 seals,
v1 and v2 become releasable and the forecast recovers ≈ 141 GB.

### 7.2 Build the block [MUT-STG]

Reuses the hardened patterns from `prune_delta`'s bulk engine — these are not optional, they are the
lessons of two field incidents:

- **Pre-create final-shape arrays** `(len(days), ny, nx)` with `chunks=(min(90,T),8,8)`,
  `shards=(min(90,T),128,128)`, `fill_value=NaN`; write disjoint `(day, var, spatial-tile)` units with
  bounded workers; finalize `attrs["days"] / vars / var_valid` **LAST**.
- **Never materialize a full slab.** 17 999 × 36 000 float32 ≈ 2.6 GB per var-day; the P4-S6 full-slab
  validation was **OOM-SIGKILLed with no traceback**. Reads are tiled; validation is **point-wise**.
- **Data-var filter by name AND shape.** Real MUR daily groups carry a 1-D `time` array; admit only 3-D
  `(t,y,x)` fields whose spatial extent covers the region. Name-only filtering caused an `IndexError`
  mid-build (P4-S6 incident #2).
- **fsync'd per-event progress JSONL** (`p5_block_build_progress.jsonl`) so a SIGKILL still leaves the exact
  frontier; `p5_block_build_error.json` on any catchable exception; the **plan/manifest-candidate artifact
  is written only on success**.
- **Checkpoint/resume** per `(day, var, tile)`. **Resume is valid only under the same code version** — after
  any code fix, delete the staging block and rebuild (the 2026-07-09 lesson).
- Days are written in **chronological** order, so the new block's `days` is chronological and its
  `day_index` maps 1:1 to contiguous slabs.

### 7.3 Validate the block [RO] — fail-closed, no publication on any failure

- day set == planned set, unique, sorted; `day_count` exact; `latest == max(days)`.
- `var_valid` correct including absent-var (all-NaN slab ⇔ `False`).
- **Semantic float32, NaN-aware sample parity** (never byte comparison) against the source tier for every
  folded day, point-wise, seeded.
- Cross-boundary parity: the day immediately before and after each block boundary reads identically through
  the new manifest and the old one, for every variable.
- Compute `fingerprint.metadata` and `fingerprint.day_digest`.

### 7.4 Publish [MUT-SWAP]

Under the ingest lock:

1. Re-read live delta `attrs["days"]` → **staleness guard**: unique days, and the folded set still ⊆ live
   delta (or ⊆ the recorded alternate sources). Any drift → **abort, publish nothing** (the P4-S8a
   `aborted_stale` shape).
2. Build manifest generation `N+1` in memory: insert the new block, set `supersedes` if it replaces an
   earlier tail version, move the replaced entry into `superseded[]` with `release_after_utc` and
   `hold_until_utc`, set `predecessor_generation = N`, compute `manifest_checksum`. **No delta field is
   touched — the manifest does not describe delta** (§4.0), so publication is a pure base-side operation and
   the served view is unchanged at this instant (delta still wins for the folded days).
3. Write `manifest.gen<N+1>.json` (immutable, fsync).
4. `os.replace(tmp, manifest.json)` — **the commit point**, atomic on a same-filesystem POSIX rename.
5. Refresh the serving snapshot (TTL or explicit) and run the §7.6 verification probes.

### 7.5 Prune delta [MUT-SWAP] — mechanics unchanged, two preconditions

Delta prune remains **rebuild-then-swap** via the existing `prune_delta` + `swap_delta` executor with all
its fail-closed gates. Two changes:

1. The base-coverage gate's meaning: **"covered by base" now means "covered by the published manifest
   generation's segments"**.
2. **New hard gate — day membership in base is NOT sufficient (round-5).** See §7.5a.

Ordering is unchanged and mandatory:

> **fold into a block → publish the manifest → verify → only then prune delta.**

Because the delta path is still a **reused path**, the delta swap still requires **request quiescence
(pm2 stop → swap → start)** per P4-S10 §3. Manifest publication itself does not (§8.3) — but that
distinction only becomes usable after **H4** is proven, and even then the first production run keeps the
quiesced posture (§15).

### 7.5a Unmaterialized-correction prune gate (round-5, hard)

**The failure this closes.** A day X inside an already-**sealed** block is later repaired into delta
(P4-S4 §7.1 `repair_overwrite` via rebuild-then-swap). Delta precedence makes the API correct *while X is in
delta*. If X is then pruned merely because **base has day membership for X**, the sealed block still holds
the **old value** (or a `confirmed_missing` gap) and the correction is **permanently lost** — silently, and
with every gate as currently written passing.

> **A delta day may be dropped only if the current base generation demonstrably contains the CORRECT value
> for that day — not merely the day.**

#### The repair WAL — identity-based, crash-safe (round-6 correction)

Round-5 made E1 a **timestamp comparison** ("the block was built after `repaired_at_utc`"). That is not
proof: build time does not establish that the build *consumed* the corrected value, and if the delta repair
becomes visible **before** its registry line is appended, a fold in that window reads the corrected value
but records nothing — or worse, a fold reads the *old* value and the later-written timestamp makes it look
authorized. Ordering is not identity.

`<data_root>/p5_repairs.jsonl` is therefore a **write-ahead log**, append-only, **fsync'd per record**, with
the intent written **before** the repair becomes visible:

| # | record | when | contents |
|---|---|---|---|
| 1 | `repair_intent` | **before** the delta repair starts | `repair_id` (unique **and** monotonic), `day`, `operator`, `at_utc`, expected variables |
| 2 | — | rebuild / swap / validate the delta repair (P4-S4 §7.1 rebuild-then-swap) | — |
| 3 | `repair_committed` | after the repaired delta day validates | `repair_id`, `day`, **repaired-day fingerprint** (seeded sample digest + `var_valid`), `at_utc` |
| 4 | `repair_aborted` | only on a **confirmed** failed repair | `repair_id`, `day`, reason |

> **Any `repair_intent` that is neither committed nor aborted BLOCKS prune of that day.** An open intent
> means the day's state is unknown, and unknown is refused. Closing it is a deliberate act: re-validate the
> delta day and append `repair_committed`, or establish the repair did not land and append `repair_aborted`.
> There is no timeout, no expiry, and no automatic resolution.

#### 7.5a-1 WAL serialization and parser semantics (round-7, normative)

`p5_repairs.jsonl` is the **only authorizing evidence** for dropping a repaired day, so its integrity rules
are part of the contract, not an implementation detail.

**Record schema** — one canonical JSON object per line (sorted keys, no `NaN`/`Infinity`):

```jsonc
{"seq": 128,                                   // monotonic, gap-free, allocated under the WAL lock
 "repair_id": "6f1c…-128",                     // unique; UUID + seq is acceptable and recommended
 "day": "2026-08-11",
 "record": "repair_intent",                    // repair_intent | repair_committed | repair_aborted
 "at_utc": "…", "operator": "…",
 "payload": { … },                             // per record type (expected vars / fingerprint / reason)
 "prev_checksum": "<record_checksum of seq-1>",   // MANDATORY: null at seq 1; at seq > 1 it MUST equal
                                                 // the record_checksum of seq-1 (chains the whole log)
 "record_checksum": "<sha256 over this record canonicalized with record_checksum set to \"\">"}
```

`prev_checksum` is **required**, not optional: it is what makes the chain verifiable end to end, and F13f's
corruption cases assert on it. `null` is legal **only** at `seq == 1`; any other `null`, or a value that does
not equal the previous record's `record_checksum`, is a **chain break** and fails closed like any other
integrity failure.

**Write protocol.** A dedicated **`p5_repairs.lock`** (distinct from `mur_delta.ingest.lock` and from
`p5_compaction.lock`) serializes appends. It is held only for the append — **not** for the duration of a
repair — so it is a short blocking lock, unlike the compaction lock's non-blocking-refusal semantics.

1. `flock(p5_repairs.lock, LOCK_EX)`;
2. **validate the ENTIRE existing WAL with the same strict whole-file parser readers use** (below) — every
   record's `record_checksum`, the `prev_checksum` chain, `seq` monotonicity and gap-freedom, and the
   per-`repair_id` state machine. **Only if the WAL is fully valid** may the writer proceed;
3. derive `last_seq` / `last_checksum` from that validated parse and **allocate `seq = last_seq + 1` under
   the lock** (this is what makes `seq` monotonic and gap-free under concurrency);
4. canonicalize, compute `record_checksum`, set `prev_checksum` (`null` only at `seq == 1`);
5. **append one line, `flush`, `os.fsync(fd)`** — per record, no batching;
6. **on file creation, additionally `fsync` the parent directory** (otherwise the file itself can be lost);
7. release the lock.

> **Writers validate before they append; there is no "write-only" fast path.** Any malformed/truncated
> record, checksum failure, chain break, `seq` gap, or state-machine violation makes step 2 fail → **refuse
> the append and alarm**, leaving the WAL byte-unchanged. **Valid records are never appended after a corrupt
> tail** — doing so would bury the corruption under legitimate-looking history and make the administrative
> `log_tail_repaired` recovery ambiguous about what was lost. A writer that cannot append is not a
> data-loss event: the repair simply does not proceed until the log is repaired.

**Readers** (the prune gate) take `flock(LOCK_SH)` for the duration of the read, so a benign
in-progress append is never mistaken for a crash tear. Readers allocate nothing and write nothing.

**State machine per `repair_id`:** `repair_intent → { repair_committed | repair_aborted }`.

- **Exactly one terminal state per intent.**
- **Identical terminal replay is idempotent.** A second terminal record with the same `repair_id`, the same
  `record` type, and a byte-identical canonical `payload` is accepted as a **no-op** — it is the expected
  retry after an uncertain fsync. It still receives its own `seq` (the log is append-only) but does not
  change state.
- **A committed-vs-aborted conflict is INVALID**, as is a second `repair_committed` carrying a *different*
  fingerprint, or a terminal record with **no matching intent**. Invalid → fail closed (below).

**Parser semantics — strict, whole-file, fail-closed, never skip.**

> The parser validates **every** record before answering any query. It **must never** "skip the bad line and
> carry on" — that is precisely the behaviour that would silently authorize a prune.

| condition | scope disabled | resolution |
|---|---|---|
| **malformed / truncated final line** (the expected crash artifact) | **all prune**, because the torn record's `day` is unknowable | operator inspects, truncates to the last valid record, and appends an administrative `log_tail_repaired` record (`{truncated_bytes, last_valid_seq, operator}`, itself checksummed). Intents open before the tear stay open and still block their days. |
| **`record_checksum` mismatch**, or a **`prev_checksum` chain break** (wrong value, or `null` at `seq > 1`) | **all prune** — a corrupt record makes the whole chain untrustworthy, and its own `day` field cannot be believed either | administrative review + `log_tail_repaired`-style resolution; never edit a record in place |
| **`seq` gap** (records lost) | **all prune** | same |
| **duplicate `seq` with differing content**, or a `repair_id` reused for a different `day` | **all prune** | same |
| **conflicting terminal records** for one `repair_id` | **that `repair_id`'s day(s)** | human decides the true outcome and records it administratively |
| **terminal without a matching intent** | **that record's day** | same |

Fail-closed here means the affected days simply **stay in delta**, where they are served correctly — the API
contract is unaffected, only compaction is paused. That is the correct trade: a paused prune costs disk; a
wrongly authorized prune loses data permanently.

#### The corrective block must record repair identity

A block that materializes a repaired day records, in its `build_provenance.materialized_repairs` (§5, per
day):

- the **latest committed `repair_id`** it consumed;
- `source_kind: "delta"` (a corrective refold reads the repaired day from delta by construction, §7.1a);
- the **repaired-day / source fingerprint** it read, so the record can be checked against the WAL's.

#### Prune eligibility (normative)

For every day D being dropped from delta, **all** must hold; a day failing any check is **not
prune-eligible** and is flagged for a corrective refold (§7.8):

- **(E1) Repair-identity match — deterministic, the only authorizing evidence.** Let
  `latest_committed(D)` = the highest committed `repair_id` for D in the WAL (absent if D was never
  repaired). Then require:
  - **no open `repair_intent` for D**; and
  - if `latest_committed(D)` exists: the covering block's
    `build_provenance.materialized_repairs[D].repair_id` **equals** `latest_committed(D)`, and its recorded
    fingerprint matches the WAL's committed fingerprint.

  **Build timestamps are diagnostic only and never authorize a prune.** A block built "after" a repair but
  carrying a different (or absent) `repair_id` is refused exactly like a stale block.
- **(E2) Sampled value parity — defence in depth, never sufficient alone.** Seeded, **point-wise** (never
  full-slab — the P4-S6 OOM lesson), float32-semantic, NaN-aware comparison of the delta day against the
  base-resolved day, plus `var_valid` equality per variable (this is what catches gap↔present and
  present↔absent). A mismatch → **refuse regardless of E1**, and raise a correction alarm: it means either a
  repair that bypassed the WAL or a genuine base defect, and both need a human.
- **(E3) Gap case.** If the covering block classifies D as `confirmed_missing`, base has **no** day
  membership for D, so the pre-existing base-coverage gate already refuses. E1/E3 together mean the gap
  variant cannot reach a prune.

E2 is sampling and therefore probabilistic; **E1 is the deterministic check**, and it is identity-based, so
a repair that never reached the WAL cannot be silently authorized by clock ordering. §16-Q17 asks ops to
confirm the repair tooling writes the WAL — a repair performed without an intent record is a process
violation, and E2 is the only net that would catch it.

#### Crash boundaries (each is a required test — §13.1 F13e)

| crash point | on-disk state | prune of D | resolution |
|---|---|---|---|
| `repair_intent` written, repair never started | open intent; delta unchanged | **blocked** | append `repair_aborted` after confirming nothing landed |
| delta swapped, `repair_committed` **not** written | open intent; delta **already corrected** | **blocked** | re-validate the delta day, then append `repair_committed` (or `repair_aborted` if it did not land) |
| `repair_committed` written, corrective refold **not** published | committed repair; block lacks / has a stale `materialized_repairs[D]` | **blocked** (E1 mismatch) | run the §7.8 corrective refold |
| a **later** repair supersedes an earlier one | two committed ids; block materialized the earlier | **blocked** (E1 mismatch: `materialized ≠ latest_committed`) | run a fresh corrective refold consuming the latest |

Every one of these fails **closed**: the day stays in delta, where it is served correctly, until the base
demonstrably carries the corrected value.

### 7.6 Post-publication verification [RO] — every probe is a hard gate

1. `/healthz`: `manifest_generation == N+1`; `segment_count` as expected; point availability bounds
   unchanged or extended, never reduced; `cube_latest_in_sync == true`; `spatial_window` unchanged by the
   fold (folding must not move the spatial window — only the later delta prune does).
2. A folded day, **two distinct checks — do not conflate them** (round-6): **(a) block proof, base-only** —
   read the day through a **base-only `SegmentedCubeStore`** (or the block path directly) and assert it is
   float32-equal to the source it was folded from, with matching `var_valid`. **(b) serving contract** —
   a public point GET returns 200 and the correct value; note that pre-prune this is served by **delta**
   (delta-wins), so (b) alone never proves the block. Only (a) does.
3. A day straddling each new block boundary, and a 366-day range crossing the boundary: 200, correct row
   count, **requested order preserved, no duplicates, no missing days**.
4. Spatial contract: bbox/POST on a delta day → 200; on a folded (non-delta) day → **400 with
   `available_spatial_window`**. Folding must not change either answer.
5. RSS bounded, open-fd count bounded, `route_counts` shows `cube`.

Any failure → **rollback (§9.3)**.

### 7.7 Sealing

A tail block is published `sealed: true` only when **both** §5.2 seal conditions hold: the ingest watermark
is past the block's **fixed calendar `end_day`** by ≥ `SEAL_LAG_DAYS`, **and** every calendar day in the
window is classified `present` or `confirmed_missing`. Sealing is therefore a statement about the calendar
window, not about having accumulated `S` entries — a block with a confirmed-missing day seals at
`day_count < S`, and a block with an unresolved day does **not** seal no matter how many days it holds.

**Sealing means the routine fold is finished with that window — it does not freeze the window's logical
content.** The rule is:

> **A sealed block's path is never modified. A correction is represented by a NEW immutable version of the
> same fixed calendar block, which supersedes it (§7.8).**

An unsealed tail block may be superseded by a longer version built per §7.1a (predecessor block +
newly-aged delta days); each supersession creates a **new path** and a new generation, and the predecessor
survives until the successor is published and verified (§7.1a, §8.5).

### 7.8 Corrective refold of a sealed block [MUT-STG → MUT-SWAP] (round-5)

**Trigger.** Either (a) a repair-registry entry (§7.5a E1) lands on a day inside an already-sealed block's
calendar window, or (b) a §7.5a prune gate refuses a day and flags an unmaterialized correction, or (c) a
§7.5a E2 parity mismatch raises a correction alarm. All three converge on the same operation.

**Procedure — identical machinery to a tail refold, applied to a sealed window:**

1. **Never touch the sealed path.** The existing sealed version stays exactly as published, and keeps
   serving, until the corrected version is published and verified.
2. Build a **new immutable version of the same fixed calendar block** — same `start_day` / `end_day` /
   `boundary_kind: "calendar"`, new version suffix, new path.
3. **Rebuild its complete `present` set** (§5.5 `rebuild_source_set`) using the normal **delta-first**
   serving authority (§7.1a): the repaired day resolves to **delta**; untouched days resolve to the
   **previous sealed version**. A `confirmed_missing` day that has since been backfilled is **promoted to
   `present`**, so `day_count` may rise and `gaps` may shrink.
4. The corrected version is published `sealed: true` in its own right — it is complete by construction
   (`unknown == []`), and it carries `supersedes: <previous sealed version id>`.
5. **Publish and verify in three phases — §7.8a.** The round-5 wording ("point GET returns the corrected
   value from the new block") was **unprovable at that moment**: pre-prune the repaired day is still in
   delta and `TieredCube` is delta-wins, so a public point GET returns **delta**, not the block. It would
   have passed whether or not the corrective refold worked.
6. **Only after §7.8a phase A passes** may the repaired day leave delta, and the §7.5a gate enforces it:
   E1's repair-identity match now succeeds (the block records `materialized_repairs[D].repair_id ==
   latest_committed(D)`) and E2's sampled parity succeeds (base now matches delta).

### 7.8a Verification ordering for a corrective refold (round-6, normative)

**A — pre-prune, BASE-ONLY (this is the proof).** With the repaired day still in delta:

1. Read the corrected day **directly from the new block path**, or — preferred, because it also exercises
   manifest resolution — through a **base-only `SegmentedCubeStore`**. §4.0/§6.1 make this trivial: the
   segmented store never sees delta, so a read through it is by construction a base read.
2. Verify the **manifest generation selects the corrected version** for that day: the snapshot's
   `day → (segment_idx, local_day_index)` map resolves D to the **new** version, not the superseded one.
3. Compare the **base value and `var_valid`** against the **repaired delta day** — seeded, point-wise,
   float32-semantic, NaN-aware, per variable. Equality here is the actual assertion that the correction was
   materialized.
4. Verify **provenance identity**: the block's `build_provenance.materialized_repairs[D].repair_id` equals
   `latest_committed(D)` in the WAL, and the recorded fingerprint matches the WAL's commit fingerprint.
5. *(Serving-contract check, recorded but not proof)* a public point GET still returns the **delta** value —
   expected, and confirming delta precedence is intact.

**B — delta prune.** Run **only after A passes** (§7.5, §7.5a).

**C — post-prune, PUBLIC.** With the day no longer in delta, call the **normal public point GET** and assert
it **still returns the corrected value** — now necessarily served from the corrected block. This is the
end-to-end assertion that the correction survived the prune.

**Ordering (mandatory, and the whole point):**

```
repair into delta (WAL intent -> swap -> WAL commit)
   →  corrective refold builds a new sealed version  →  publish
   →  PHASE A: base-only verification + provenance identity  ← the proof
   →  ONLY NOW: prune the repaired day from delta
   →  PHASE C: public point GET still returns the corrected value
```

> The same trap applies in the ordinary direction: **any check performed while the day is still in delta
> proves nothing about the block.** §7.6 probe 2 is qualified accordingly.

**Cost.** A corrective refold rewrites one whole block — at `S = 90` that is ≈ 141 GB and one §7.1c disk
gate, i.e. the same unit cost as a routine sealing fold. It is rare by construction (it needs a repair on a
day older than `SPATIAL_WINDOW_DAYS + SEAL_LAG_DAYS`), so it is not modelled in the §10 steady-state cadence;
it is an event, and §16-Q17 asks ops how promptly it must run — because **until it does, the repaired day
cannot leave delta**, so a deferred corrective refold grows delta span.

**Superseded lifecycle** is the ordinary one (§5.6, §8.5): the previous sealed version moves through
`referenced → releasable → held`, remains a rollback target, and is hard-deleted only by ops after
`hold_until`.

---

## 8. Consistency & concurrency model

### 8.1 The invariant

> An in-flight read observes **either a complete old snapshot or a complete new snapshot** — never a mix of
> metadata and chunks from different generations, and never a fabricated value.

### 8.2 How immutability delivers it

The S8a failure required a path whose **bytes changed while a handle was live**. Under §4 rule 1 that cannot
happen: a published block path is write-once. A reader holding generation `N`'s snapshot reads paths that
still exist and still contain exactly the bytes they contained at open. This is the argument; **H4 is the
proof obligation** (P5-S5) and the design does not get credit for it until the adversarial test passes.

### 8.3 Publication is not a swap

Publishing generation `N+1` adds files and atomically replaces one small JSON file. It **retargets nothing**.
Consequently, *if* H4 holds, manifest publication needs **no PM2 quiescence** — a genuine improvement over
the delta swap's measured 2.791 s of downtime. The delta prune that follows still reuses the delta path and
still requires quiescence (§7.5), and the delta remains outside the manifest entirely (§4.0). Making the
delta itself manifest-referenced and versioned would remove the last quiescence requirement; that is
deliberately **out of P5 scope** and recorded as §16-Q5 as a P6 candidate.

### 8.4 Refresh and snapshot lifetime

`maybe_refresh(TTL)` reads `manifest.json`; unchanged generation → no-op. Changed → build off-lock, swap
under the lock. A request captures the snapshot **once** at entry and uses it for the whole call. Snapshot
lifetime is therefore bounded by `TTL + longest in-flight request`.

### 8.5 Superseded-block lifecycle (the one deletion hazard)

`referenced` → (after `release_after_utc = publish + TTL + max_request + margin`) `releasable` →
**move to hold** `[MUT-HOLD]` with `hold_until_utc = publish + HOLD_DAYS` (default 14, per P4-S8 §7) →
**hard delete is ops-only, manual, after `hold_until`**. Deleting or moving a block earlier reproduces the
S8a *silent fabricated null* (missing chunk → fill value → `None`). The snapshot builder additionally
**fails closed** if a referenced path is missing, so a mistake surfaces as a retained-old-snapshot + alarm
rather than as wrong data.

**Two extra holds on the immediately-preceding tail version** (round-2, from §7.1a and §9.3a):

1. **It is a build input.** The next tail rebuild reads its carried-forward days from it, so it must remain
   at its published path until the successor is **published and verified** — never released "at supersession
   time".
2. **It is the rollback target.** §9.3a must be able to restore it if a defect is found; if it has already
   been moved to hold, `restore-before-rollback` runs first, and if it has been hard-deleted, rollback to
   that generation is gone. `release_after_utc` must therefore be strictly later than the realistic
   discover-a-defect window, not merely later than the refresh TTL.

### 8.6 Concurrency with ingest

The daily delta append is `[MUT-LIVE-APPEND]` on a different store and remains append-or-skip, never
overwrite (P4-S4 §7.1). Compaction takes the **same ingest lock** for its staleness guard + publication, so
append and publish are mutually exclusive. A block build itself ([MUT-STG]) runs **outside** the lock — it
touches no live path — and is re-validated under the lock before publication.

### 8.7 Ordering guarantees preserved

Chronological ordering, overlap precedence, no duplicate days, and **no silent truncation** are all
properties of the merged snapshot map, and each gets a dedicated test (§13). Silent truncation is worse than
an error (P4-S11); the `mixed` route exists precisely to avoid it and is unchanged.

---

## 9. Crash / rollback model

| moment of failure | on-disk state | serving state | recovery |
|---|---|---|---|
| during block build | partial staging block + fsync'd progress JSONL | untouched | resume (same code version) or delete staging and rebuild |
| after build, before validation | complete staging block, no manifest change | untouched | discard or re-validate; nothing published |
| after validation, before `manifest.gen<N+1>.json` write | staging block complete | untouched | re-run publication |
| after `manifest.gen<N+1>.json` write, before `os.replace` | new generation file exists but is **unreferenced** | untouched, still generation `N` | safe: garbage-collect the orphan generation file, or re-attempt the replace |
| **during `os.replace`** | atomic — either old or new inode | one or the other, complete | none needed |
| after publish, before verification | generation `N+1` live | new view | if probes fail → §9.3 |
| after publish, verification fails | generation `N+1` live | possibly wrong | **§9.3 rollback to `N`** |
| during delta prune/swap | as P4-S8a/S10 | quiesced | existing executor rollback under the still-held lock |

### 9.3 Rollback to the predecessor generation

**Mechanic (round-2 correction — never `os.replace` the archive itself).** Archived generation files are
**immutable artifacts**; `os.replace(manifest.gen<N>.json, manifest.json)` would *consume* the archive
(rename removes the source name), destroying the very record a second rollback, an audit, or a
forward-recovery would need. The correct sequence is:

1. **copy** `manifest.gen<N>.json` → `manifest.json.rollback-tmp-<id>` (same directory, same filesystem);
2. **verify** the copy: parse, check `manifest_checksum`, check `generation == N`;
3. **fsync** the temp file, and fsync its containing directory;
4. **`os.replace(manifest.json.rollback-tmp-<id>, manifest.json)`** — the atomic commit;
5. refresh the serving snapshot and re-run the §7.6 probes against the **generation-`N`** expectations;
6. append a manifest-log line (`op: manifest_rollback`, `from_generation`, `to_generation`, operator,
   reason) — the audit trail is append-only in both directions.

`manifest.gen<N>.json` is **still present and unmodified** afterwards. Rolling *forward* again is the same
procedure with `N+1`.

**Preconditions.**
- `predecessor_generation` and `predecessor_manifest` are mandatory manifest fields.
- Every generation file is retained for at least `HOLD_DAYS`, and is never renamed or overwritten.
- **Every block referenced by generation `N` must be present at its recorded path before step 4.** This is
  normally guaranteed by §8.5 (`release_after_utc` is strictly later than any plausible rollback window),
  but it must be **verified, not assumed** — see §9.3a.

### 9.3a Restore-before-rollback (when a predecessor block is already in hold)

If verification finds that a block referenced by generation `N` has already been moved to hold (or is
otherwise missing), **the rollback must not proceed** — replacing `manifest.json` first would publish a
generation whose snapshot build immediately fails closed, leaving the service pinned to a stale in-memory
snapshot with no valid manifest on disk. Order is mandatory:

1. **Locate** each missing block via the `superseded[]` entry and the hold manifest line (both record the
   original path and the hold path).
2. **Restore** it to its **original recorded path** by same-filesystem rename from hold `[MUT-HOLD-restore]`
   — the path is the one generation `N` names, and restoring it re-establishes an immutable, byte-identical
   store (a hold move is a rename, so the content was never altered).
3. **Verify** the restored block: readable Zarr group, `attrs["days"]` matches generation `N`'s declared day
   set, `fingerprint.day_digest` matches. A mismatch is a **hard stop** requiring human triage — never
   "restore anyway".
4. Only then run §9.3 steps 1–6.
5. Record the restore in the hold manifest (`op: hold_restore`, block id, from/to path, reason, operator).

If a referenced block has been **hard-deleted** (only possible after `hold_until` and an explicit ops
action), rollback to that generation is **no longer available**; recovery is the P4-S4 §10 ladder — rebuild
the block from delta / daily / hold / NetCDF and publish it as a *new* generation. The spec's answer to this
is prevention: §8.5's release grace plus `HOLD_DAYS` must exceed the maximum realistic
discover-a-defect-and-decide window, which is why §16-Q6 asks ops to confirm 14 days is enough.

### 9.4 Ambiguous states

If publication fails in a way that leaves the live `manifest.json` unreadable or checksum-invalid, the
serving process **keeps its current snapshot** (fail-closed refresh) and alarms. The P4-S10 posture applies:
**fail closed, leave the app in a known state, require a human to verify on-disk state before proceeding.**

---

## 10. Block-size / cadence model

Two independent parameters, and separating them is the main design insight of this section:

- **`S` = block size** (days per sealed block) → drives **segments per request**, **peak temp disk**, and
  total segment count.
- **`C` = compaction cadence** (days between folds) → drives **compaction frequency**, **delta span**, and
  **write amplification**.

### 10.1 Delta span is driven by cadence, not block size

Delta must retain (a) every day inside the protected 31-day spatial window and (b) every day not yet folded.
Folding only happens every `C` days, so:

> **`delta_span_max = SPATIAL_WINDOW_DAYS + C + DELTA_BUFFER_DAYS`**

This holds for any `S`. It is the reason cadence, not block size, governs the read cost of the delta tail
(§1.2, **H5**).

### 10.2 Write amplification is driven by S/C

If a growing tail block is republished every `C` days until it seals at `S`:

> **amplification = (S/C + 1) / 2**, and per-`S`-day cost = `amplification × S` day-writes.

### 10.3 Modelled comparison [MODEL] (base 1.57 GB/day, delta 1.09 GB/day, `MAX_DAYS = 366`)

| S | C | segments per 366-d request | peak temp disk | folds per year | write amp | bytes written per year | delta span max | delta bytes |
|---|---|---|---|---|---|---|---|---|
| 30 | 30 | **13** (+delta) | **47 GB** | 12.2 | 1.0× | 573 GB | **64 d** | 70 GB |
| 45 | 45 | **9** (+delta) | **71 GB** | 8.1 | 1.0× | 573 GB | **79 d** | 86 GB |
| 90 | 90 | **5** (+delta) | **141 GB** | 4.1 | 1.0× | 573 GB | **124 d** | 135 GB |
| **90** | **30** | **5** (+delta) | **141 GB** | 12.2 | **2.0×** | 1 146 GB | **64 d** | **70 GB** |
| 90 | 45 | 5 (+delta) | 141 GB | 8.1 | 1.5× | 860 GB | 79 d | 86 GB |

Compare the status quo: **one full base rebuild ≈ 2 000 GB peak temp disk**, which is why compaction has
never run.

### 10.4 Read-latency projection [MODEL] — the sizing tension

Decompressed bytes for a 366-day point read are **close to, but not exactly, invariant** to block size —
chunk `(S',8,8)` with `S' = min(90,S)` gives 5 × 23 040 B at S=90, 9 × 11 520 B at S=45, 13 × 7 680 B at
S=30 = **115 / 104 / 100 KB per var**. Those three numbers are *not* equal, and an earlier draft that called
them "invariant" contradicted its own arithmetic. The precise statement is §2's scoped **H3**: bytes are
equal only for an **aligned, full-span** read; a 366-day window landing off the block anchor differs, and
P5-S0 measured the spread at **0.333× – 1.036×** segmented/monolith across offsets and anchors
([`p5s0_baseline_results.md`](p5s0_baseline_results.md) §4c). The dominant, *systematic* cost that scales
with block size is therefore the **number of array calls** (and the chunk count) — bytes move by tens of
percent and usually **downward** for smaller blocks, which is why sizing is a calls-vs-blocks trade rather
than a bytes trade. Applying the marginal cost of **0.65 ms per extra segment call** to the [VM24] 366-day
baseline of 76–100 ms, over 3 variables:

| S | extra segment calls (3 vars) | projected added latency | projected regression vs baseline |
|---|---|---|---|
| 90 | 12 | ≈ +8 ms | **≈ +8 %** |
| 45 | 24 | ≈ +16 ms | ≈ +16 % |
| 30 | 36 | ≈ +23 ms | **≈ +25 % — exactly at the gate** |

This is a projection from a laptop-measured per-call overhead, **not** a measurement; **P5-S6 must measure
it directly** and the projection must not be cited as a result.

### 10.5 Segment-count horizon [MODEL]

| horizon | S=90 | S=45 | S=30 |
|---|---|---|---|
| +3 y | 1 + 13 | 1 + 25 | 1 + 37 |
| +5 y | 1 + 21 | 1 + 41 | 1 + 61 |
| +10 y | 1 + 41 | 1 + 82 | 1 + 122 |

Per-request segments stay capped by `MAX_DAYS/S` in all cases; the horizon numbers drive **open handles,
snapshot build cost and manifest size**, which is what **H7** measures. If S=30 at +10 y proves unbounded,
a periodic **super-block merge** (merge k sealed blocks into one) is the escape hatch — it is just another
immutable publication and needs no new machinery.

### 10.6 File count: segmentation is ~neutral, but the ABSOLUTE count is large and must be gated

At production geometry, a 90-day block holds `ceil(17999/128) × ceil(36000/128) = 141 × 282 = 39 762` shard
files per variable, ≈ **119 286 files per 3-var block** ≈ **1 325 files/day** [MODEL].

**The correct conclusion is comparative, and the absolute numbers are big** [MODEL]:

| | files |
|---|---|
| legacy monolith today (≈ 1 273 days) | ≈ **1.69 M** |
| one 90-day block | ≈ **119 k** |
| +10 years of blocks (3 653 days ≈ 41 blocks) | ≈ **+4.9 M** |
| **total base at +10 y (legacy + blocks)** | ≈ **6.6 M** |
| an equivalent *monolith* holding the same 4 926 days | ≈ 6.5 M |

So **segmentation itself costs ~0.1 M files over a decade — essentially per-block metadata only** (the
[PROBE] 1 / 12 / 36 segments over 1 080 days gave 194 / 216 / 648 files, the growth being per-block
`zarr.json` plus smaller-block edge effects). **But the growth from ~1.7 M to ~6.6 M inodes is real and is
driven by data volume**, and it lands on the same filesystem whether or not we segment. An earlier draft of
this section claimed segmentation adds "only ~40 metadata files" — that conflated the *marginal* cost of
segmentation with the *absolute* file count, and is corrected here.

**Therefore P5-S6 / H7 must gate on inode-scale behaviour, not only on read latency:**

| gate | why |
|---|---|
| **inode headroom**: `df -i` free inodes ≥ projected 10-year file count × safety factor | a full inode table is an outage with plenty of free bytes |
| **metadata-scan cost**: time to `os.walk` / `du` one block and the whole `base_blocks/` tree | backup, `du`, audit tooling and the disk precheck all traverse this; it must stay in seconds-to-minutes |
| **snapshot-open cost**: wall time and syscall count to build a `SegmentedCubeStore` snapshot at 5 / 13 / 41 / 82 / 122 segments | this runs on every generation change and on process start; it must stay well inside the refresh TTL |
| **per-request open fd ceiling** at C = 8/16 | already H7; unchanged |

If the metadata-scan or snapshot-open cost degrades super-linearly with segment count, the mitigation is the
same as §10.5's: periodic **super-block merges**, which reduce segment count without reducing file count.

### 10.7 Failure and rollback cost by size

Rebuild-on-failure cost = one block: **47 / 71 / 141 GB** and (scaling the [PROBE] build rate by the
production/probe cell ratio, and cross-checked against the P4-S6 bulk-engine field behaviour) **hours, not
days**. Rollback cost is **one `os.replace` regardless of size** — a property of the manifest design, not of
the sizing.

### 10.8 Recommendation

**Provisional recommendation: `S = 90`, `C = 30`** — sealed blocks match the base `time_chunk=90` read
layout exactly, a 366-day request touches ≤ 5 segments (+8 % projected, comfortably inside the 25 % gate),
delta stays bounded at ≈ 64 days (protecting the crossing-range latency that is already 219 ms at 36 delta
days), compaction runs monthly, and peak temp disk is **141 GB instead of 2 TB**. The price is a 2× write
amplification (≈ 1.1 TB written per year — trivial against the blocker it removes) and up to two superseded
tail-block generations in hold.

**Fallback: `S = 45`, `C = 45`** if the S6 benchmark shows the tail-block rebuild does not fit the ingest
window, or if superseded-generation hold disk proves unacceptable: it costs 1.0× amplification and half the
peak temp disk, at ~+16 % projected read latency and a 79-day delta.

**S6 adjudication gate.** Choose the configuration that, measured on production-geometry fixtures:
(i) keeps 366-day point p95 within **+25 %** of the current `TieredCube` baseline, (ii) keeps
`delta_span_max` such that the crossing-range p95 stays within the same bound, (iii) keeps peak temp disk
**< 200 GB**, (iv) completes a tail-block rebuild inside the daily ingest window with margin, and
(v) minimizes folds per year subject to (i)–(iv). If several qualify, prefer the larger `S` (fewer segments)
and the smaller `C` (smaller delta).

---

## 11. Disk / cost model

| quantity | today | P5 (S=90, C=30) | source |
|---|---|---|---|
| peak compaction temp disk | **≈ 2 000 GB** (full base rebuild) | **≈ 141 GB + margin** | [MODEL], **H6** |
| steady-state base bytes | ~2 TB, growing 1.57 GB/day | unchanged (blocks partition the same data) | [VM24] |
| delta bytes | 1.09 GB/day × span | ≤ ~70 GB (64-day cap) | [MODEL] |
| superseded / predecessor tail versions on disk | n/a | ≤ 2 partial blocks ≈ 47 + 94 GB — **`pinned_existing`**: forecast and alarmed, never charged to a run's gate | §7.1a, §7.1c, §8.5 |
| bytes written per year by compaction | 0 (never runs) | ≈ 1 146 GB | §10.3 |
| files per 3-var 90-day block | — | ≈ 119 286 (≈ 1 325 files/day) | [MODEL] |
| base file count at +10 y | ≈ 1.7 M today | ≈ **6.6 M** (≈ +4.9 M from data volume; segmentation itself ≈ +0.1 M) | §10.6 |
| free inodes required | not tracked | gated in S6/H7 | §10.6 |
| rollback cost | full re-swap + PM2 cycle | **copy-archive → fsync → one `os.replace` + refresh** | §9.3 |

**Hard budget rule (P4-S4 §6, corrected in round-2 and again in round-3):** every compaction runs the §7.1c
disk precheck **[RO]** and gates on **`free_now − additional ≥ HARD_RESERVE`**, where `additional` is the
**new allocation only** — staging block + temp/checkpoint/artifacts + measured growth during the build.
Predecessor and superseded blocks are **`pinned_existing`**: already subtracted from `free_now` by the
filesystem, reported and forecast, **never charged twice**. Gating on `new_block + margin` alone is
insufficient; gating on `new_block + pinned` is over-strict and would refuse the sealing fold (§7.1c worked
example). **A P5 compaction that ever requires a second full base is a bug**, not an ops condition.

---

## 12. Migration plan from the existing monolithic base

Five stages, each independently reversible, **none of which rebuilds the ~2 TB monolith**.

**M0 — manifest over the status quo [RO audit → MUT-STG → MUT-SWAP].** Publish generation 1 containing
exactly one segment: `legacy_base` = the existing monolith, `immutable: true`, `boundary_kind: "legacy"`.
Delta is **not** in the manifest (§4.0) and `TieredCube` keeps holding it exactly as today. The served view
is **byte-for-byte the current one**; the only change is that the base is now described by a manifest. This
makes the read path switchable and rollback-able before any data moves.

**M0 is a generator, not a transcription.** `start_day` / `end_day` / `day_count` / `gaps` / `day_digest`
for the legacy segment are **produced by a fresh read-only audit of the live monolith's `attrs["days"]`** at
run time — never hand-written into the spec or the runbook, and never carried over from a previous
document. The audit emits the exact day set, computes the digest, and derives `gaps` as
`calendar[start..end] − present`; whatever it finds is what gets published (it may well be a fully
contiguous span with no gaps at all). `block_grid.anchor_day` is set to the day after the audited
`end_day`, fixing the calendar grid (§5.2) from that point on.

Gate: full parity vs the current `TieredCube` on the P1 oracle; `/healthz` stable fields unchanged;
manifest `day_digest` reproducible by a second independent audit run.

**M1 — first extension block, shadow only [MUT-STG].** Build one block from delta days older than the
window on a shadow copy; validate; publish into a **shadow manifest** consumed by a shadow API. Production
manifest untouched.

**M2 — first production block publication [MUT-SWAP].** Publish generation 2 with the first extension block.
**Delta is not pruned in this step** — the days exist in both the block and delta, overlap precedence
resolves to delta, and the fold is therefore a pure no-op for served values. This is the safest possible
first production mutation: it is *observably* a no-op, and rollback is one `os.replace`.

**M3 — first delta prune against a block [MUT-SWAP + MUT-LIVE].** Only after M2 verifies: prune delta of the
folded days via the existing `prune_delta` + `swap_delta` executor with quiescence. The base-coverage gate
now consults the manifest. This is the first step that actually shrinks delta.

**M4 — steady state.** Fold every `C` days; seal at `S`; superseded tail blocks to hold; O4 alarm
re-parameterized (§16-Q2).

**The legacy monolith is never rebuilt and never modified.** If it ever needs to be split (for example to
retire very old data), that is a separate, later, independently-approved operation — and it is cheap to
express in this architecture: build the replacement blocks, publish a generation that swaps segment zero for
them, move the monolith to hold.

**Rollback at every stage** is `os.replace` of the predecessor manifest, except M3 which uses the existing
delta-swap rollback.

---

## 13. Local-first test matrix & gates

**Discipline (non-negotiable, carried from P1–P4):** every step ships code + deterministic tests +
benchmark + machine-readable artifact + explicit PASS/FAIL gate + an updated results document. **A step
must never advance on synthetic warm latency alone.** Tests are written to fail first, then the
implementation follows. Local suite today: **256 green** — P5 must keep it green throughout.

### 13.1 Required fixtures

| fixture | purpose |
|---|---|
| **F1 legacy monolith** | multi-block-span single Zarr, production chunk geometry `(90,8,8)/(90,128,128)` |
| **F2 multiple immutable blocks** | ≥ 3 sealed blocks + 1 unsealed tail, adjacent and non-overlapping |
| **F3 delta ≥ 31 contiguous days** | the protected window, so window-violating folds can be rejected |
| **F4 append-order delta metadata** | recent days appended, then an older day backfilled → `days` is *not* sorted; asserts `day_index` mapping and `latest = max(days)`, never `days[-1]` |
| **F5 overlaps** | a day present in both a block and delta → delta precedence, exactly one row |
| **F6 gaps** | a **synthetic** calendar day absent everywhere, inside a fixed 90-day block window → in the **sealed** case `day_count = 89`, `gaps = [that day]`, `unknown = []`, `present + confirmed_missing + unknown == S`, and the **next block's boundary is unchanged** (§5.2/§5.3). No production day is asserted to be a gap — real gaps are whatever M0's live audit finds. |
| **F7 absent vars** | a variable absent from one block but present in its neighbours |
| **F8 present NaNs** | land NaN → `null`, distinct from absent → omitted |
| **F9 cube + daily newest day** | ingest-lag day only in daily → `mixed` route, no truncation |
| **F10 crash/recovery** | staging block killed mid-build (frontier JSONL); manifest written but not replaced; manifest replaced but block missing |
| **F11 stale manifest** | manifest `day_count` disagrees with the block's `attrs["days"]` |
| **F12 duplicate day at equal precedence** | two blocks claiming the same day → snapshot build must fail closed |
| **F13 tail-rebuild chain v1→v2→v3 with delta pruned in between** | the §7.1a source map, end to end: build v1 from delta days A → publish → **prune delta of A** → build v2 whose only available source for A is **published block v1** → publish → prune delta of B → build v3 → seal. Asserts: v2/v3 build **succeeds after A is gone from delta/daily/hold**; **`rebuild_source_set` for v2 is A+B and for v3 is A+B+C — every present day has a source-map entry**, while `classification_target` is only B resp. C (§5.5); the successor physically contains every present slab; the source map records `block` for carried-forward days and `delta` for new ones; the predecessor is **still present** at build time and released only after the successor verifies; a v2 build attempted with the predecessor missing **refuses before writing anything**; **no `unknown` day is ever given a source**. |
| **F13b late correction after a prune (round-3)** | fold day A → publish v1 → prune A from delta → **repair A back into delta with a different value** (rebuild-then-swap repair path) → build v2. Asserts: A **is in `rebuild_source_set`** and its source resolves to **delta, not block v1**; v2 carries the **corrected** value; the served value after publish + a later prune of A is still the corrected one; and the `source_kind` change (block → delta) is logged as a late correction. This is the test that fails if carry-forward is defined as "copy the predecessor" or if `rebuild_source_set` is narrowed to newly-aged days. |
| **F13c `confirmed_missing` promoted on a later fold (round-4)** | day X is `confirmed_missing` in v1 → X is later backfilled into delta → build v2. Asserts: for an **unsealed** block X is re-checked, **promoted to `present`**, joins `rebuild_source_set`, and `gaps` shrinks accordingly; a **sealed** block is not re-checked by a routine fold — the same situation on a sealed window is handled by **F13d**'s corrective refold instead. Without this a backfilled day would be sealed as a permanent gap while delta was still serving it, then lost at the next prune. |
| **F13d sealed-block correction, three-state (round-5, tightened round-6)** | seal block v1 containing day X with an **old value** (and a second variant where X is `confirmed_missing`) → **repair X into delta** through the WAL (`repair_intent` → swap → `repair_committed`) → **attempt to prune X: REFUSED** by §7.5a even though base has day membership → **corrective refold** producing sealed **v2** of the *same fixed calendar window* at a *new path* → publish. Then assert **all three states** (§7.8a): **(1) pre-prune, base-only** — reading X through a base-only `SegmentedCubeStore` returns the **corrected** value with matching `var_valid`, the manifest resolves X to **v2 not v1**, and `build_provenance.materialized_repairs[X].repair_id == latest_committed(X)`; **(2) pre-prune, public** — a normal point GET returns the **delta** value (delta precedence intact, and *not* proof of the block); **(3) post-prune, public** — after X leaves delta, the public point GET **still returns the corrected value**, now from v2. Also asserts: v1's path is byte-unchanged throughout; v2's `day_count` rises and `gaps` shrinks in the `confirmed_missing` variant; v2 carries `supersedes: v1`; v1 enters the normal superseded lifecycle. **This is the test that fails if a sealed block is treated as logically frozen, or if the correction is "proved" by a delta-served read.** |
| **F13e repair-WAL crash boundaries (round-6)** | the four §7.5a boundaries, each asserting **prune of the day is refused**: (a) `repair_intent` written, repair never started; (b) delta swapped but `repair_committed` **not** written (open intent, delta already corrected); (c) `repair_committed` written but the corrective refold **not** published (block lacks or has a stale `materialized_repairs[X]`); (d) a **later** repair supersedes an earlier one and the block materialized the earlier `repair_id`. Plus: an **open intent has no timeout and never self-resolves** — only an explicit `repair_committed` or `repair_aborted` clears it; and **a block built after a repair but carrying the wrong/absent `repair_id` is refused exactly like a stale block** (build timestamp must not authorize anything). |
| **F13f WAL integrity & parser semantics (round-7)** | the five §7.5a-1 cases. **(1) concurrent intent creation** — N writers append intents simultaneously; every `seq` is unique, monotonic and gap-free, every `repair_id` distinct, and the chain (`prev_checksum`) verifies. **(2) crash leaving a partial final line** — a truncated last record makes the parser **fail closed for all prune** (never "skip the bad line"); after the administrative `log_tail_repaired`, normal semantics resume and intents open before the tear **still block their days**. **(3) conflicting terminal records** — `repair_committed` + `repair_aborted` for one `repair_id` (and a second `committed` with a *different* fingerprint) are **invalid** and disable prune for that day; a terminal with no matching intent likewise. **(4) identical idempotent replay** — a byte-identical duplicate terminal record is a **no-op**, gets its own `seq`, and does **not** change state or unblock anything it should not. **(5) sequence / checksum corruption** — a `seq` gap, a `record_checksum` mismatch, a **`prev_checksum` chain break** (wrong value, or `null` at `seq > 1`), a duplicate `seq` with differing content, and a `repair_id` reused for another day each **fail closed** and are **never ignored**. **(6) writer validates before appending** — with a torn final record present, an attempted new `repair_intent` append is **REFUSED** and the **WAL bytes are unchanged** (byte-for-byte, asserted); after the administrative `log_tail_repaired`, the same append **succeeds and takes the next valid `seq`** with a correct `prev_checksum`. All cases assert the affected days remain in delta and continue to be served correctly. |
| **F14 unsealed manifests + seal transition (round-3)** | v1 (`|present|=30, |unknown|=60`) and v2 (`|present|=59, |gaps|=1, |unknown|=30`) manifests **validate and publish**; `present + confirmed_missing + unknown == S` holds for both; v3 with `|unknown| == 0` **seals**; a manifest asserting `sealed: true` with non-empty `unknown` is **rejected**; a block at its calendar `end_day` with one day still unresolved does **not** seal, and seals in a later version once the day resolves. |
| **F15 rollback with predecessor in hold** | generation `N+1` published, predecessor block already moved to hold → rollback must **restore-before-rollback** (§9.3a), verify the restored digest, and leave `manifest.gen<N>.json` **unmodified** afterwards |
| **F16 compaction lock (round-4)** | a delta prune / swap / repair attempted while the builder holds `flock(p5_compaction.lock)` is **refused** with `compaction_lock_held` (non-blocking `LOCK_NB`) and touches nothing; daily delta **append** proceeds normally (it does not take this lock); the holder **exiting** releases the lock and a prune then proceeds. |
| **F16b split-brain / fencing (round-4)** | (a) the holder is **paused** (`SIGSTOP`) past any plausible TTL → the lock is **still held** → a prune is **still refused** → the holder resumes and publishes correctly (this is the case a TTL lease gets wrong); (b) the lock file is **deleted and recreated** underneath the holder, a second process locks the new inode and performs a delta swap → the original holder's publish-time **inode check fails → it refuses to publish**; (c) *if the TTL-lease variant is ever chosen instead*: an expired holder that resumes **after** a new holder took over and swapped must **fail to renew and fail to publish** (fence-token check). |
| **F16c lock-fd ownership (round-5)** | with the recommended single-process/threaded build: killing the builder releases the lock and a prune then proceeds, while any staging tree left behind **cannot be published**. With a child-process build: (a) fd is **not inherited** (`O_CLOEXEC`) → parent death frees the lock and a surviving worker cannot publish; (b) **the hazard case** — if a child *did* inherit the fd, parent death leaves the lock held by the child, so a prune is still refused and the runbook's process-group kill + **fresh non-blocking test acquisition** is required before proceeding; (c) a resume over an orphaned staging tree re-acquires the lock and revalidates rather than trusting the frontier. |
| **F17 multi-generation superseded lifecycle (round-4)** | v1 is superseded at generation `N` and moved to hold, while manifests advance to `N+2` / `N+3`. Asserts: **v1's entry is still present in generation `N+3`'s `superseded[]`** with `superseded_at_generation == N` and `status: held` and `current_path` = its hold path; the `pinned_existing` forecast counts it; **rollback from `N+3` to `N` restores it** via §9.3a; a v1 entry disappears **only** when ops hard-deletes it, and that removal is itself a new generation; a `superseded[]` entry whose `current_path` is missing **alarms and marks the affected rollback unavailable without failing the served snapshot** (§5.6). |

### 13.2 Required performance tests

1-day point; 366-day point; **3-year point at the store level** (note: `MAX_DAYS = 366` caps the public API,
so this is a store/bench path, not an API path); legacy→block→delta crossing range; C = 1/4/8/16;
cold and warm; RSS; open file-handle count; **segment-count sensitivity** (1/5/9/13/25/41 segments);
block build time; **tail-block rebuild time**; peak temp disk; **manifest refresh under concurrent reads**.
Every bench emits a JSON artifact under `dev2026/bench/results/`.

### 13.3 Initial gates

| # | gate | threshold |
|---|---|---|
| G1 | **Semantic equality with the current `TieredCube` / P1 oracle** | **exact**, on every fixture, for point GET, range, absent-var, NaN, ordering — float32 semantic, NaN-aware, never byte comparison |
| G2 | Day integrity | zero missing, zero duplicate, zero out-of-order days in any returned range |
| G3 | Point p95 | ≤ **+25 %** vs the current `TieredCube` baseline on the same fixture |
| G4 | 366-day p95 | **< 4 s** hard |
| G5 | Memory | RSS bounded at C = 8 and C = 16; no growth across a sustained run |
| G6 | Handles | thread count and open file handles bounded; no unbounded growth |
| G7 | Compaction temp disk | ≈ one block + margin; **never a second full base** |
| G8 | Pre-publication failure | old manifest fully usable; served view bit-identical to before |
| G9 | Post-publication failure | rollback to the predecessor generation restores the previous served view exactly |
| G10 | **Concurrency** | an in-flight read returns a **complete old** or **complete new** snapshot — **never mixed metadata/chunks, never a fabricated `None`** (this is **H4**; modelled directly on the P4-S8a proof test) |
| G11 | Spatial contract | bbox/POST answers are **unchanged** by any fold or publication; no block is ever spatially served |
| G12 | Route contract | `X-Store-Route` values and `MAX_DAYS`/413 behaviour unchanged; `mixed` ingest-lag behaviour unchanged |
| G13 | **Tail-rebuild source authority + set scope** | `rebuild_source_set` = **every `present` day of the successor** (not the newly-aged delta), and **every one of them has a source-map entry**; `classification_target` = newly-aged days only; `unknown` days are never given a source. Each day's value equals **the currently served authoritative value at build time** — carry-forward when unchanged (F13), **corrected value when the day was repaired back into delta** (F13b), **promoted when a `confirmed_missing` day was backfilled** (F13c). A build with any unresolved `rebuild_source_set` day **refuses before writing**. |
| G14 | **Calendar boundaries + three-way classification** | a gap never shifts any block boundary; `present + confirmed_missing + unknown == S` for every calendar block; `sealed ⇒ unknown == 0`; `sealed: true` with non-empty `unknown` is rejected (F6, F14) |
| G15 | **Archive immutability** | after any rollback, every `manifest.gen<N>.json` still exists byte-unchanged; rollback with a held predecessor restores it first and verifies its digest (F15) |
| G16 | **Inode / metadata scale** | free-inode headroom, metadata-scan wall time and snapshot-open cost within the §10.6 bounds at the +10-year segment horizon |
| G17 | **Build isolation + fencing** | no delta path retarget can occur during a block build: prune/swap/repair are refused (non-blocking) while the compaction `flock` is held, daily append is unaffected, a dead holder releases automatically (F16); **a paused holder still blocks, and a replaced lock inode makes publish refuse** (F16b). If the TTL variant is chosen, the fence-token split-brain test must pass instead. If the snapshot alternative is chosen, the clone must be provably **stable-old** across a concurrent swap. |
| G18 | **Disk gate correctness** | the §7.1c gate charges only `additional`; `pinned_existing` is reported but never subtracted twice; the v1/v2/v3 worked example passes at every fold, **including the sealing fold** |
| G19 | **Superseded lifecycle persistence** | `superseded[]` is cumulative across generations; entries survive until ops hard-deletes them; rollback several generations back still finds and restores its blocks; a missing entry alarms and disables that rollback **without** failing the served snapshot (F17) |
| G20 | **Sealed-block correction path** | a correction landing inside a sealed window is never lost: the §7.5a gate **refuses** to prune a delta day carrying an unmaterialized correction (day-membership in base is not sufficient); a corrective refold publishes a **new immutable version of the same calendar window** and the old path is byte-unchanged; the day leaves delta only after **§7.8a phase A (base-only + provenance identity)** passes; the public GET returns the corrected value **post-prune** (F13d, all three states) |
| G21 | **Repair-WAL identity & crash safety** | prune authorization is **identity-based**: `materialized_repairs[D].repair_id == latest_committed(D)` plus fingerprint match; **build timestamps authorize nothing**; **any open `repair_intent` blocks prune** with no timeout or self-resolution; all four crash boundaries fail closed (F13e) |
| G22 | **WAL serialization & parser integrity** | `seq` is monotonic and gap-free under concurrent writers (allocated under `p5_repairs.lock`); every record is checksummed, chained via a **mandatory** `prev_checksum` (`null` only at `seq 1`), and fsync'd, with a parent-directory fsync on creation; **writers validate the whole WAL before appending and refuse to append after a corrupt tail, leaving bytes unchanged**; each intent has **exactly one** terminal state; identical terminal replay is **idempotent**; committed-vs-aborted conflicts are **invalid**; and malformed/truncated JSON, checksum failure, chain break, `seq` gap or duplicate conflicting `seq`/`repair_id` **all fail closed and disable the affected prune — the parser never skips or ignores a bad record** (F13f) |

Gates may be tightened or corrected in a later revision **only with the evidence that justifies the change
recorded alongside it**.

---

## 14. P5 step sequence

Every step names its **mutation envelope**, inputs, outputs, tests, artifacts, gate, rollback, and stop
condition. Steps S0–S7 are **local-only**; S8–S10 are the VM24 boundary (§15).

### P5-S0 — current compaction/read baseline + cost model  **[RO]**
- **In:** existing base/delta fixtures; production geometry constants.
- **Out:** `bench/bench_p5_baseline.py`, `bench/bench_p5_rmw.py`; results doc `p5s0_baseline_results.md`.
- **Tests:** harness self-tests (the fixture really has the declared geometry; the counters really count).
- **Measures:** H1, H2, H3 (all three cases, **including a segmented-vs-monolith sweep over request
  offsets × block anchors × S ∈ {30,45,90}** — the case that shows byte equality is conditional), H8 —
  reproducing every **[PROBE]** number in §3 with a **committed** harness,
  plus current `TieredCube` point/range p50/p95, chunk_count, decompressed_bytes as the **baseline of
  record** for G3.
- **Artifacts:** `bench/results/p5s0_*.json`.
- **Gate:** H1 and H2 confirmed (else candidate A re-opens and §3 is rewritten); baseline recorded.
- **Rollback:** n/a (read-only). **Stop if:** the harness cannot reproduce §3's structural findings — stop
  and re-derive the candidate comparison before designing anything.

### P5-S1 — manifest schema + segmented-store prototype  **[MUT-STG]**
- **Out:** `store/block_manifest.py` (schema, validation, checksum, **calendar block-grid arithmetic**,
  atomic publish + copy-then-replace rollback helpers), `store/segmented_cube.py` (prototype
  `SegmentedCubeStore`, **base-only — it never reads the delta path**), fixtures F1–F3, F6.
- **Tests:** schema round-trip; checksum tamper → reject; missing/extra fields → reject; **stale manifest
  (F11) → snapshot build fails closed**; **duplicate day at equal precedence (F12) → fails closed**;
  overlap precedence among base segments; **three-way classification `present + confirmed_missing + unknown
  == S`, `sealed ⇒ unknown == 0`, and `sealed: true` with non-empty `unknown` rejected (§5.3/§5.4, F14)**;
  **calendar-boundary arithmetic and non-drift (F6, G14)**; `day_count` cross-check against live
  `attrs["days"]`; **a manifest containing any delta field is rejected** (§4.0 guard).
- **Gate:** every malformed/stale manifest is rejected **without** ever exposing a partial snapshot; G14.
- **Rollback:** delete staging; nothing published. **Stop if:** a fail-closed path is found that can serve a
  partial view.

### P5-S2 — grouped multi-segment read path + parity  **[MUT-STG]**
- **Out:** production `SegmentedCubeStore.point_series` with per-segment grouping; `TieredCube` generalized
  to accept a segmented base; fixtures F4–F9.
- **Tests:** G1 semantic equality on every fixture; append-order (F4); overlap (F5); gap (F6); absent var
  across a boundary (F7); NaN (F8); `mixed` (F9); **assert one array call per (segment,var)** — a test that
  counts calls and fails on per-day access.
- **Bench:** segment-count sensitivity; cached vs per-request open.
- **Gate:** G1, G2, G3, G12. **Rollback:** unmerged branch. **Stop if:** G1 fails anywhere — parity is not
  negotiable, and a parity failure means the read model is wrong, not the test.

### P5-S3 — tail-block bulk builder + checkpoint/resume  **[MUT-STG]**
- **Out:** `ingest/build_block.py` — **§7.1a/§5.5 two-set resolution: `classification_target` (newly aged)
  vs `rebuild_source_set` (every present day of the successor), with a source-map entry for every day of the
  latter, recorded before any write**, in serving-authority order (**delta → published block → daily → hold
  → NetCDF**); **§7.1b compaction `flock` acquire/hold + publish-time held-and-inode assertion** + the
  non-blocking refusal gate wired into `prune_delta` / `execute_swap_plan`; **§7.1c `free_now − additional`
  disk precheck** with the `pinned_existing` forecast; point-wise validation, tiled reads, name+shape
  data-var filter, fsync'd progress JSONL, error JSON, artifact-on-success-only, per-`(day,var,tile)`
  resume. **Never a full-history daily store.**
- **Tests:** build from each source kind; **F13 v1→v2→v3 with delta pruned between versions** (asserting
  `rebuild_source_set` = A+B then A+B+C); **F13b late correction** (repaired day must come from delta, not
  the predecessor block); **F13c `confirmed_missing` promotion on an unsealed block**; **F14 unsealed
  manifests validate and only `unknown == 0` seals**; **F16 / F16b lock refusals and fencing**; any
  unresolved `rebuild_source_set` day → **refuse before writing**; `unknown` days are never given a source;
  predecessor missing → refuse; **F13d corrective refold of a sealed block, asserted in all three §7.8a
  states (pre-prune base-only, pre-prune public/delta, post-prune public)**; **F13e repair-WAL crash
  boundaries**; **F13f WAL serialization + strict parser** (concurrent intents, torn final line, conflicting
  terminals, idempotent replay, seq/checksum corruption — all fail closed, never skipped); the
  **§7.5a prune gate** (E1 repair-identity match + E2 sampled point-wise parity + E3 gap
  case) → a day carrying an unmaterialized correction or an open intent is **not prune-eligible**, and a
  build timestamp never authorizes one; **F16c lock-fd ownership**;
  **mid-build SIGKILL → exact frontier → resume completes and validates green by date**; 1-D `time` array
  excluded; absent var preserved as `valid=False` + all-NaN; refuses to build over an existing path;
  refuses to fold a day inside the protected window; disk precheck refuses when
  `free_now − additional < HARD_RESERVE` **and** passes the §7.1c v1/v2/v3 worked example at every fold.
- **Bench:** build time and peak temp disk at S = 30/45/90; **read cost of a carried-forward day from a
  predecessor block vs from delta** (the predecessor's `(90,8,8)` layout should make v2/v3 builds *cheaper*
  than v1); **if the snapshot alternative is in play (§7.1b), clone wall time, inode count and disk delta**.
- **Gate:** G7, G13, G14, G17, G18, **G20, G21, G22**, plus a peak-RSS bound proving **no full slab is ever
  materialized** (E2's parity sampling is point-wise for exactly this reason).
- **Rollback:** delete the staging block. **Stop if:** peak RSS scales with grid area — that is the P4-S6
  OOM regression returning.

### P5-S4 — atomic manifest publish + immutable snapshot refresh  **[MUT-SWAP]** (staging only)
- **Out:** publish executor; **copy-then-fsync-then-replace rollback executor (§9.3) that leaves the
  generation archive intact**; `restore-before-rollback` path (§9.3a); `SegmentedCubeStore.refresh` /
  `maybe_refresh` generation-aware TTL.
- **Tests:** publish advances the generation; refresh exposes it; unchanged generation → no segment reopen;
  rollback restores the exact previous view; **`manifest.gen<N>.json` is byte-unchanged after rollback**
  (G15); **F15 rollback with the predecessor block already in hold** → restore first, verify digest, then
  replace; a hard-deleted predecessor → rollback **refuses** with the §9.3a recovery instruction; append-only
  manifest log in both directions.
- **Gate:** G8, G9, G15. **Rollback:** copy-then-replace of the predecessor archive. **Stop if:** any
  publication or rollback path can leave `manifest.json` invalid **and** the server serving from it, or can
  destroy a generation archive.

### P5-S5 — crash / concurrency / rollback proof tests  **[MUT-STG]** — the decisive step
- **Out:** `tests/test_phase2_p5s5.py`; results doc `p5s5_proof_results.md`.
- **Tests (adversarial, modelled on `TestCachedHandleProof`):**
  (a) reader holds generation `N` snapshot; publisher publishes `N+1`; **pre-refresh reads must be
  stable-old, value-asserted** — not merely "no exception";
  (b) same, with the new generation **shrinking** the day set (the S8a silent-`None` variant);
  (c) a superseded block deleted too early → snapshot build **fails closed**, never fabricates `None`;
  (d) every §9 crash point, injected;
  (e) concurrent refresh + reads under load, asserting no torn snapshot;
  (f) **build isolation + fencing (round-4, F16/F16b/G17):** with a block build in flight holding
  `flock(p5_compaction.lock)`, an attempted `prune_delta` / `execute_swap_plan` / repair-overwrite is
  **refused non-blocking** and touches nothing; a concurrent daily append proceeds and does not perturb the
  build; a **killed** holder releases the lock and a prune then proceeds; a **paused (`SIGSTOP`)** holder
  still blocks a prune and then resumes and publishes correctly; a **deleted-and-recreated lock file** makes
  the publish-time inode assertion fail and the build **refuse to publish**. If the TTL variant is chosen
  instead, the fence-token split-brain case must pass: an expired holder that resumes after a takeover +
  swap must fail both renew and publish. If the §7.1b snapshot alternative is chosen, the equivalent test is
  that the hardlinked clone returns **stable-old** values across a concurrent delta swap — an S8a-style
  value-level assertion, not merely "no exception".
- **Gate:** **G10 / H4**, plus **G17**. **Stop condition (hard):** if any pre-refresh read returns new-generation bytes
  against old metadata, **P5's no-quiescence claim is withdrawn**, §8.3 is deleted, and publication inherits
  the full PM2 stop→swap→start posture. The design survives; the downtime saving does not.

### P5-S6 — 30/45/90 block-size & cadence decision benchmark  **[RO]/[MUT-STG]**
- **Out:** `bench/bench_p5_sizing.py`; results doc `p5s6_sizing_results.md`; a **recorded decision**.
- **Measures:** H3, H5, H7 at S = 30/45/90 and C = 30/45/90 — **on the production calendar anchor**
  (`block_grid.anchor_day`, §5.2) with representative **1-day / 366-day / legacy→block→delta crossing**
  windows, since P5-S0 established that decompressed bytes are **anchor- and offset-sensitive** and S0's
  synthetic sweep does not settle the real geometry — segments per request, point/range p50/p95,
  crossing-range p95 vs delta span, build/rebuild time, peak temp disk, open handles, snapshot build cost at
  the +3/+5/+10-year segment horizons, **plus the §10.6 inode / metadata-scan / snapshot-open gates (G16)**
  and the **tail-rebuild** cost including carry-forward from the predecessor block.
- **Gate:** the §10.8 adjudication gate, extended with **G16**. **Stop if:** no configuration satisfies G3+G4 — then the read tax is
  real and the architecture must be reconsidered (super-block merging, or a hybrid where recent history is
  segmented and deep history stays monolithic).

### P5-S7 — full local HTTP / load / disk gate  **[MUT-STG]**
- **Out:** `bench/loadtest.py` run against the segmented API; results doc `p5s7_local_gate_results.md`.
- **Tests:** the whole API surface against a segmented store — point GET, range, bbox, POST, `mixed`,
  `MAX_DAYS`/413, `X-Store-Route`, `/healthz`, TTL refresh **during** load, publication **during** load.
- **Gate:** G1–G12 all green simultaneously. **Stop if:** any gate fails — S8 is not authored until this is
  green.

### P5-S8 — VM24 read-only / shadow runbook (authoring only)  **[RO]**
- **Out:** `p5s8_vm24_shadow_runbook.md` — read-only production measurement plus a shadow-copy rehearsal;
  concrete commands, abort conditions, artifact list, operator summary template. **Authoring only; nothing
  executed.**

### P5-S9 — VM24 shadow execution (Codex / ops)  **[MUT-STG] on shadow copies**
- Executed by Codex/ops. Production read-only throughout; the closing evidence is an **empty `/healthz`
  stable-field diff**, as in P4-S9.

### P5-S10 — production rollout (separate approval)  **[MUT-SWAP] + [MUT-LIVE]**
- Follows §12 M0 → M4 with a **fresh audit per stage** and its own written approval. **Nothing in this spec
  authorizes it.**

---

## 15. VM24 promotion boundary

- **Claude authors specs, code, tests, benchmarks and runbooks — locally, only.** Claude does **not** SSH to
  VM24, does not modify production/cron/PM2/NGINX, does not touch `base` / `delta` / `daily` / `hold`, and
  does not execute a live swap or publication. **Codex is the independent reviewer and the executor;** the
  human is the orchestrator and the approval authority.
- **S0–S7 are local.** **S8 is authoring only.** **S9 is Codex/ops on shadow copies with production
  read-only.** **S10 requires its own fresh, written approval** — an S9 PASS does not confer it.
- **Production posture for the first rollout:** even if **H4** passes, M2's first production publication is
  performed with the **existing quiesced posture** (ingest lock → `pm2 stop` → publish → `pm2 start` →
  verify). Dropping quiescence is a **separate, later, evidence-backed change** (§16-Q5) — the S8a lesson is
  that "should be safe by construction" is exactly the claim that failed last time.
- **`hold` is never hard-deleted by any P5 tool.** The 1 267 held daily groups stay until their `hold_until`
  and an explicit ops decision. Superseded blocks follow the same rule.
- **No P5 step may reduce point availability or widen the spatial contract.** Both are regressions by
  definition (P4-S11).

---

## 16. Unresolved decisions requiring Codex / human sign-off

| # | question | why it matters | default if unanswered |
|---|---|---|---|
| **Q1** | **Confirm `S = 90` / `C = 30`** (§10.8), or mandate the S6 benchmark decide unconditionally? | Sets peak temp disk (141 vs 71 GB), fold frequency (12 vs 8 /yr) and delta span (64 vs 79 d). | Proceed to S6 with S=90/C=30 as the hypothesis to beat. |
| **Q2** | **Re-parameterize the O4 alarm.** Today it fires at `delta_span > 31 + 14 = 45 d`. Under any P5 cadence delta *by design* oscillates up to `31 + C + buffer` (64 d at C=30) — the current alarm would fire every cycle. | An alarm that always fires is not an alarm. | Propose `alarm at delta_span > 31 + C + buffer + 14`, **plus a new "compaction overdue" alarm** at `days_since_last_fold > C + slack`. Needs ops sign-off. |
| **Q3** | **Do we publish growing (unsealed) tail blocks at all?** C < S buys a smaller delta at 2× write amplification and superseded-generation churn; C = S is simplest. | Directly changes §7.7, §8.5 and the hold-disk budget. | Publish unsealed tail blocks (C=30, S=90). |
| **Q4** | **Public route contract:** keep `X-Store-Route: cube` for segmented storage (recommended), or expose segment attribution publicly? | A new public route name is an API contract change with frontend impact. | Keep `cube`; expose segments only via `/healthz` and logs. |
| **Q5** | **Should the delta also become manifest-referenced and versioned in P6?** That would remove the last path-reuse and eliminate the remaining 2.79 s swap downtime. **P5 explicitly keeps delta out of the manifest** (§4.0) so there is exactly one authority per store. | Real operational win, but it re-opens a settled, proven-safe mechanism and creates a second authority over the same days. | **P6 candidate; not P5.** |
| **Q6** | **`HOLD_DAYS` for superseded blocks** — reuse 14 (P4-S8 §7), or shorter given blocks are re-derivable from delta/daily/NetCDF? | Superseded partial tail blocks can hold ~141 GB. | Reuse 14 days for consistency. |
| **Q7** | **Revisit rectilinear chunks at zarr-python 3.3?** The inner-chunk-uniformity constraint that produced the measured 26× point-read regression is a **library** limitation, not a spec one. | If 3.3 lifts it, candidate E's cheap appends could apply to the **delta tier**, not the base. | Re-run H8 when 3.3 ships; do not block P5 on it. |
| **Q8** | **NetCDF re-download SLA** (still open from P4-S4 §12-Q1). | It is the deep backstop for folding a day whose delta/daily/hold copies are all gone. | Keep hold as the primary backstop; do not design a path that depends on re-download. |
| **Q9** | **Confirm the ordering guarantee** `fold → publish → verify → prune delta` (§7.5) and that no delta day is ever pruned before the published manifest covers it. | This is the invariant that prevents point-history loss. | Assume yes (this spec does). |
| **Q10** | **`SEAL_LAG_DAYS`** (§5.2) default = `SPATIAL_WINDOW_DAYS` = 31. Confirm, or set independently. | Guarantees a sealed block never contains a day still inside the protected spatial window. | Default 31. |
| **Q11** | **`release_after_utc` for a superseded tail version** must now cover *both* the refresh-TTL window *and* the realistic discover-a-defect window, because the predecessor is a build input (§7.1a) and a rollback target (§9.3a). What is that window in ops terms — one fold cycle (30 d)? | Setting it too short makes §9.3a's restore path the normal case instead of the exception. | Propose: not released until the **successor is published, verified, and one further fold cycle has passed**; needs ops sign-off. |
| **Q12** | **Inode budget** on the VM24 filesystem: does `df -i` have headroom for ≈ 6.6 M base files at +10 y plus delta/daily/hold? | §10.6 corrected the file-count model upward by ~5 M; an inode ceiling is an outage with free bytes. | Measure in P5-S8 (read-only) and gate in S6/G16. |
| **Q13** | **`confirmed_missing` evidence bar** (§5.3/§5.4): is "absent from delta + daily + hold" sufficient, or must a NetCDF re-download attempt be required before a day may be permanently recorded as a gap in a **sealed** block? | Undoing a wrongly-sealed gap costs a **full-block corrective refold** (§7.8, ≈ 141 GB at S=90). An unsealed block can always keep the day as `unknown` instead, at no cost. | Propose: re-download attempt required to seal; `unknown` is the honest state until then. Depends on Q8. |
| **Q14** | **Build isolation mechanism (round-4, §7.1b): held kernel `flock` (recommended) vs immutable delta source snapshot?** The `flock` makes `prune_delta`/`swap_delta`/repair **refuse for the duration of a multi-hour build**; the snapshot lifts that constraint at the cost of an inode-heavy hardlink clone. The renewable TTL lease is **withdrawn** — it cannot fence a paused holder without CAS/fence-token machinery. | Without one of the two, a delta swap during a build writes S8a-class wrong bytes **into a new immutable block**. | `flock` as primary (≈ zero cost, no TTL, kernel-released on death, non-blocking refusal); snapshot as the measured fallback if ops will not accept the refusal window. **Ops must confirm the refusal window is acceptable.** |
| **Q15** | **Wedged-build operational procedure** (§7.1b): confirm that clearing a stuck build means **killing the process**, and that the lock file is **never `rm`'d** (deleting it does not release the holder and creates a two-inode split-brain, which the publish-time inode check then turns into a refusal). Also confirm `<data_root>` is a **local** filesystem, since `flock` is unreliable on NFS. | Wrong intervention converts a stuck build into a potential two-writer situation. | Kill-the-process only; never `rm` the lock; S8 read-only runbook records the filesystem type and `st_dev`. |
| **Q16** | **`confirmed_missing` promotion on unsealed blocks** (§5.5, F13c): confirm that a day recorded `confirmed_missing` in v1 and backfilled afterwards is **promoted to `present`** in a later unsealed version, rather than frozen. | Without promotion, a backfilled day is sealed as a permanent gap while delta still serves it — then lost at the next prune (the F13b defect in another form). | Promote on unsealed (free); on a sealed window the same outcome is reached by a **corrective refold** (§7.8), which costs a full block — hence Q17's SLA question. |
| **Q17** | **Corrective-refold SLA and repair-registry ownership (round-5, §7.5a/§7.8).** Two parts: (a) how promptly must a corrective refold run after a repair lands inside a sealed window — **until it completes, the repaired day cannot leave delta**, so a deferred refold grows delta span and costs a full block rebuild (≈ 141 GB at S=90); (b) confirm the **repair tooling will write the `p5_repairs.jsonl` WAL** — `repair_intent` **before** the repair becomes visible, `repair_committed` after it validates, `repair_aborted` on a confirmed failure — since E1 (the only authorizing evidence) is identity-based and E2 sampling alone is probabilistic; **(c)** confirm who is responsible for closing an **open intent** left by a crash, and for the administrative `log_tail_repaired` resolution of a torn/corrupt WAL (§7.5a-1) — both block prune indefinitely and by design never self-resolve. | Without (b) the gate degrades to sampling; without (a) or (c) delta grows unboundedly after a deep repair, a crashed one, or a torn log. | Propose: WAL write mandatory in the repair path (a repair without an intent record is a process violation); corrective refold at the next fold cycle, or immediately if delta span would breach its alarm; **open intents and WAL-integrity failures surfaced by the O4-style audit and resolved by ops** — the audit should report them as first-class conditions alongside `delta_span` and `free_disk`. |

---

### Acceptance self-check

- ✅ 16 sections as specified, in order.
- ✅ Candidates A–E each evaluated with **measured** structural evidence, not assertion; E researched against
  the **Zarr v3 specification and zarr-python 3.2.1 source**, not prior API assumptions; **D's verdict is
  scoped to what the evidence actually shows** (not selected / deferred, not disproven).
- ✅ **Round-2 item 1:** tail rebuild can source from the published predecessor block (§7.1a — refined in
  round-3 to "current serving authority", of which the predecessor block is one tier), with F13/G13
  covering v1→v2→v3 across a delta prune.
- ✅ **Round-2 item 2:** one authority per store — manifest = base only; delta stays with `TieredCube`
  (§4.0, §5.1, §6.1, §6.4, §7.4, §8.3, Q5).
- ✅ **Round-2 item 3:** no production day is asserted to be a gap; `day_count`/`gaps`/`day_digest` are
  audit-derived at M0 (§5, §5.1, §12-M0, F6).
- ✅ **Round-2 item 4:** fixed calendar boundaries + classified-days seal condition (§5.2, §7.7, G14, F14).
- ✅ **Round-2 item 5:** copy-then-replace rollback preserving the archive, plus restore-before-rollback
  (§9.3, §9.3a, G15, F15).
- ✅ **Round-2 item 6:** corrected file-count model with inode/metadata-scan/snapshot-open gates
  (§10.6, H7, G16, Q12).
- ✅ **Round-2 item 7:** disk gate covers in-build growth and the serving reserve (§7.1c, §11) — **corrected
  in round-3** to charge additional allocation only.
- ✅ **Round-3 blocker 1:** `unknown` / `materialized_through` make unsealed v1/v2 manifests valid; seal
  requires `unknown == 0`; `unknown` days are never given a source (§5.3–§5.5, F14, G14) — **the source-map
  scope itself was corrected in round-4 to `rebuild_source_set`**.
- ✅ **Round-3 blocker 2:** source precedence = serving authority; late corrections win over the predecessor
  block; carry-forward parity redefined (§7.1a, F13b, G13).
- ✅ **Round-3 blocker 3:** build isolation with refusal for prune/swap/repair, append unaffected, snapshot
  fallback to be measured (§7.1b, F16, G17, P5-S5(f), Q14/Q15) — **mechanism replaced in round-4**.
- ✅ **Round-4 High #1:** `classification_target` vs `rebuild_source_set` defined and used consistently;
  every successor `present` day has a source-map entry; `unknown` days never do (§5.5, §7.1a, F13/F13b/F13c,
  G13); cost model unchanged because it already assumed full re-materialization.
- ✅ **Round-4 High #2:** held kernel `flock` replaces the TTL lease; paused-holder and replaced-inode
  split-brain cases specified and tested; fence-token requirements written down for the TTL variant should
  it ever be chosen (§7.1b, F16/F16b, G17, P5-S5(f), Q14/Q15).
- ✅ **Round-4 Medium #3:** `superseded[]` cumulative across generations; lifecycle states and ops-only
  removal defined; missing entry alarms without failing serving (§5.6, F17, G19).
- ✅ **Round-5 High #1:** sealed-block correction path — path immutability restated, corrective refold
  defined, and prune gated on *corrected-value* evidence rather than day membership (§5.5, §7.5a, §7.7,
  §7.8, F13d, G20, Q17).
- ✅ **Round-5 Medium #2:** lock-fd ownership is supervisor-only and non-inherited, with the
  process-group/cgroup fallback and a fresh-acquisition proof (§7.1b-1, F16c, G17).
- ✅ **Round-6 High #1:** repair provenance is a fail-closed WAL with identity matching; open intents block
  prune; build timestamps authorize nothing; four crash boundaries fail closed (§5 `build_provenance`,
  §7.5a, F13e, G21, Q17).
- ✅ **Round-6 High #2:** correction is proved **base-only pre-prune** and re-asserted **publicly
  post-prune**; the pre-prune public GET is recorded as a delta-precedence check, never as block proof
  (§7.6, §7.8, §7.8a, F13d, G20).
- ✅ **Round-7:** WAL serialization and parser semantics specified — dedicated lock, monotonic gap-free
  `seq`, per-record checksum + **mandatory `prev_checksum` chain**, fsync per record and on directory creation, one terminal
  state per intent, idempotent identical replay, invalid conflicting terminals, and a strict fail-closed
  parser that never skips a bad record (§7.5a-1, F13f, G22, Q17(c)).
- ✅ **Round-8:** writers validate the whole WAL before appending and refuse to append after a corrupt tail;
  `prev_checksum` mandatory and chain-verified; gate count corrected to 22 (§7.5a-1, F13f case 6, G22).
- ✅ **Round-3 blocker 4:** no double-counting; `pinned_existing` forecast/alarm separated from the gate;
  worked example proves the sealing fold is not blocked (§7.1c, §11, G18).
- ✅ **Round-3 item 5:** §4.1 rule 2 rollback wording matches §9.3 exactly.
- ✅ Every measurement labelled **[PROBE] / [VM24] / [MODEL] / [SPEC]** with an explicit warning that
  scratch-probe numbers are preliminary and must be re-derived by committed harnesses in S0/S6.
- ✅ Preserved semantics enumerated and each assigned a gate: chronological ordering (G2), overlap
  precedence (§5.1, G1), no duplicate days (G2, F12), no silent truncation (§8.7, G12), append-order delta
  `day_index` (F4), absent var → omitted (F7, G1), present NaN → null (F8, G1), `var_valid` (§7.3),
  full-history point GET/range (G1, §6.5), recent delta-only bbox/POST (G11), `X-Store-Route` (G12),
  `MAX_DAYS = 366` (G12), `mixed` ingest-lag (F9, G12), TTL metadata refresh safety (§6.7, §8.4, G10).
- ✅ `SPATIAL_WINDOW_DAYS = 31` respected: only days older than the protected window may be folded (§7.1).
- ✅ Forward-compaction sources are delta / recent daily staging / hold / NetCDF — **never a pruned
  full-history daily store** (§7.1).
- ✅ 30/45/90 modelled on compaction frequency, rebuild volume, delta span, temp disk, 3/5/10-year segment
  counts, read latency, handles/file count, and failure/rollback cost (§10), with a recommendation **and** an
  adjudicable S6 gate.
- ✅ Manifest schema carries every required field; overlap/gap/duplicate/stale/atomic-publish/refresh/
  per-request-snapshot/rollback/superseded-retention/crash-before-and-after semantics all defined (§5, §8, §9).
- ✅ Read path specifies grouped multi-segment access with an explicit **no per-day store open** rule and a
  test that enforces it (§6.2, P5-S2).
- ✅ Public route contract unchanged unless evidence demands otherwise (§6.6, Q4).
- ✅ Local-first discipline, fixtures, performance tests and **22 gates (G1–G22)** defined (§13); step sequence with
  envelope / inputs / outputs / tests / artifacts / gate / rollback / stop condition (§14).
- ✅ **Spec-only:** no implementation, no other file changed, no commit, no VM24 contact.
