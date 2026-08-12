# P5-S5 — proof step: RESULTS (PARTIAL — one of three parts delivered)

Status: **Part 1 (source-byte provenance) DELIVERED. Parts 2 and 3 NOT DELIVERED.**
`[MUT-STG]`: staging/synthetic only. No VM24 path, no `GHRSST_*` store, no cron, nothing
written outside a temp dir.

Branch `dev2026-p5-s5-proof`, stacked on `9bc1795`.

- Provenance primitive + artifact: [`../store/source_provenance.py`](../store/source_provenance.py)
- Builder emits the artifact: [`../ingest/build_block.py`](../ingest/build_block.py)
- Publication verifies it: [`../ingest/publish_manifest.py`](../ingest/publish_manifest.py)
- Tests: [`../tests/test_phase2_p5s5.py`](../tests/test_phase2_p5s5.py) — **59/59 green**
- P5-S4 regression: **155/155 green**
- Full local suite: **670 tests OK** (17 skipped), up from 611
- `-W error::ResourceWarning` over S4+S5: **214 OK**

> **This document does not claim the P5-S5 gate.** G10/H4 and G17 require the crash and
> concurrency proof in Part 3, which is not written. Nothing below is marked PASS on the
> strength of an argument.

## Gate status

| gate / requirement | status | evidence |
|---|---|---|
| **1. Source-byte provenance** | | |
| locator map and byte provenance are distinct concepts | **PASS** | `TestLocatorIsNotByteProvenance` |
| locator digest cannot alone authorize a prune | **PASS** (by construction) | prune authorization is `repair_wal.prune_authorization`; it takes no locator digest |
| builder emits a checksummed provenance artifact | **PASS** | `TestArtifactIntegrity` |
| artifact records source identity **and** bytes | **PASS** | `test_the_builder_emits_a_checksummed_artifact`, `..._comes_from_the_SOURCE_not_from_what_was_written` |
| publication verifies artifact vs block | **PASS** | `test_a_BLOCK_altered_after_the_build_is_refused` |
| publication verifies artifact vs manifest | **PASS** | `test_a_build_artifact_that_disagrees_on_a_LOCATOR_is_refused` (S4), `test_an_artifact_covering_different_days_is_refused` |
| source changed → refuse | **PASS** | `test_a_SOURCE_modified_in_place_after_the_build_is_refused` |
| artifact missing → refuse | **PASS** | `test_the_artifact_is_REQUIRED`, `test_a_missing_artifact_is_refused` |
| checksum mismatch → refuse | **PASS** | `test_a_tampered_artifact_is_refused`, `test_a_truncated_artifact_is_refused` |
| incomplete fields → refuse | **PASS** | `test_an_incomplete_day_record_is_refused`, `test_an_unknown_extra_field_is_refused` |
| E2 primitive: point-wise, float32, NaN-aware | **PASS** | `TestFingerprintSemantics` |
| `var_valid` transition (absent↔present) in the source → refuse | **PASS** (round 2) | `TestSourceVarValidTransitionsAreCaught` — delta and daily |
| the artifact cannot choose its own sample or grid | **PASS** (round 2) | `TestTheArtifactCannotChooseItsOwnSample` |
| `day_index` is bound to the DATE, not just the range | **PASS** (round 3) | `TestDayIndexIsBoundToTheDate` |
| `var_valid` covers exactly the canonical variables | **PASS** (round 3) | `test_var_valid_must_cover_exactly_the_canonical_variables` |
| `block_path` is verified against the published block | **PASS** (round 3) | `test_a_renamed_block_path_is_refused` |
| verification holds **at the commit point**, under both locks | **PASS** (round 4) | `TestProvenanceIsVerifiedInsideTheCriticalSection` |
| every bypass is named `unsafe_*` and reports unverified | **PASS** (round 4) | `TestTheSourceRecheckWaiverIsLabelled` |
| **2. Repair WAL / corrected-day lifecycle** | | |
| WAL authorization wired into `prune_delta` | **NOT DELIVERED** | — |
| E2 wired as a prune gate | **NOT DELIVERED** | primitive exists (`compare_days`); no caller |
| §7.8 corrective refold orchestration | **NOT DELIVERED** | — |
| §7.8a three-phase verification / base-only probe | **NOT DELIVERED** | — |
| **3. Crash / concurrency proof** | | |
| every §9 crash boundary injected | **NOT DELIVERED** | — |
| retry / committed-but-unlogged / archive-exists | **NOT DELIVERED** | carried from S4 §12.3 |
| (a)(b)(c) stable-old / shrinking / early-delete | **NOT DELIVERED** | — |
| (e2) composite base+delta generation fence (R1) | **NOT DELIVERED** | see the note below |
| (f) build isolation, kill/SIGSTOP/inode fence | **PARTIAL, from S3/S4** | `p5s3` lock tests; `p5s4` reservation + fence tests. No kill or `SIGSTOP` case |
| **G10 / H4 / G17** | **NOT CLAIMED** | Part 3 not written |

**On (e2), stated now rather than discovered later:** P5-S2 deliberately narrowed
`TieredSnapshot`'s contract to **per-tier stability, not cross-tier contemporaneity**, and
documented why. Nothing in S3–S5 changed that. So the composite base+delta fence is **not
currently implemented**, and a test written today would fail. That is a design decision to be
made in Part 3 — either build the fence or invoke the §14 stop condition and withdraw the
no-quiescence claim. **No complete-old/complete-new claim is made here.**

## 1. What Part 1 delivers, and the distinction it turns on

P5-S3 round 4 gave every block a `source_map` — per day, `source_kind` / `source_path` /
`source_day_index`. P5-S4 rounds 2–9 bound that map to the segment, to the block on disk, to the
live manifest, and to the builder's own record. Every one of those is **locator** provenance:
*which store, which slot*. None of them says anything about *which bytes came out*.

> A source modified in place after the build leaves **every** locator check passing.

`source_provenance.py` adds the other half. During the fold the builder reads one tile-sized
window per `(day, var)` **from the source**, through the same cached handle the fill used, and
records a fingerprint of it in a checksummed artifact. At publication that fingerprint is
re-derived from the **block's own cells** and compared, and the **source is re-read** and
compared again.

`TestLocatorIsNotByteProvenance` states the distinction as an executable claim: an artifact
whose every locator field agrees with the manifest, on a manifest that agrees with itself, is
still refused when the bytes are wrong.

### The design choices worth defending

**A seeded sample, not a full digest.** A full digest of a 90-day block is ~141 GB of reads, and
the P4-S6 lesson is not to materialize slabs at all. §7.5a E2 already specifies the shape:
seeded, point-wise, float32-semantic, NaN-aware, plus `var_valid` equality per variable.

**One window per (day, var), not scattered points.** Scattered sampling looks stronger and is
worse: on a sharded store each point pulls a whole shard, so N points cost N shard reads. A
window costs one. Bounded I/O is what lets this run at publication rather than be skipped.

**The window moves with the day.** A fixed window would leave the same region of every day
unexamined for the life of the store. `test_the_window_moves_with_the_day` pins it.

**Fingerprinted from the source, never from the output.** An output fingerprint would say "the
block contains what the block contains" — true of any block, including one written wrongly.
`test_the_fingerprint_comes_from_the_SOURCE_not_from_what_was_written` corrupts the *fill* while
leaving the fingerprint pass clean, so the artifact records the source and publication catches
the bad write. This is the test that distinguishes provenance from self-certification.

**A gone source is a refusal, not a skip.** My first version skipped unreadable sources as
"legitimately pruned". That branch is unreachable — the §7.4 staleness guard already requires
every recorded source to resolve — and a skip turns *we could not check* into *we checked*.

**`absent` and `present-but-NaN` are distinct tokens.** P1 omit semantics versus land. Collapsing
them is exactly the gap↔present confusion §7.5a E2 exists to catch.

### What Part 1 does NOT establish

The comparison is a **seeded sample**. A corruption confined to unsampled cells survives it.
§7.5a is explicit that E2 is *defence in depth, never sufficient alone*, and that **E1's repair
identity is the deterministic authorization**. This step implements E2's primitive and the
artifact E1 will need; it does **not** upgrade E2 into a proof, and it does not implement E1's
wiring — that is Part 2.

## 2. Interface changes

- `build_block(...)` writes `p5_block_provenance.json` into `artifacts_dir` and returns
  `provenance` / `provenance_path` on the plan. It has **no** seed parameter: the sample seed
  is `source_provenance.SAMPLE_SEED`, policy rather than payload (§6.2).
- `execute_publication(..., build_artifact_path=...)` is now **required**, with
  `unsafe_skip_provenance=True` and `unsafe_skip_source_recheck=True` as the two greppable
  waivers (the `build_block` `unsafe_skip_isolation` pattern). Either one makes the result
  report `verified: False` with `source_recheck: "waived"` and a reason — a bypass that
  reported success would be worse than no bypass.
- The publish result carries
  `provenance: {verified, source_recheck, days, sources_rechecked[, reason]}`.
  `sources_rechecked` is asserted against the day count, so a source quietly not re-read shows
  up as a number rather than as a pass.
- **Verification runs inside the critical section**, after both locks and after the staleness
  guard, immediately before the commit — and nowhere else (§8.1).

## 3. Mutation verification

37 guards disabled in turn; **all 37 fail**.

| guard disabled | result |
|---|---|
| block bytes not compared to the fingerprint | FAILED (3) |
| source not re-read | FAILED (4) |
| gone source skipped instead of refused | FAILED |
| artifact checksum not verified | FAILED |
| day-record field set not checked | FAILED (1 + 1) |
| artifact format/version not checked | FAILED |
| artifact day set not compared to the source map | FAILED |
| artifact segment identity unchecked | FAILED |
| `var_valid` excluded from the fingerprint | FAILED |
| sample window fixed for every day | FAILED |
| artifact not required at publication | FAILED |
| builder fingerprints its OUTPUT instead of the source | FAILED |
| source `var_valid` taken from the artifact | FAILED (3) |
| `var_valid` transition not compared | FAILED (3) |
| grid taken from the artifact | FAILED |
| artifact grid not compared to the block | FAILED |
| sample policy not enforced | FAILED (6) |
| sample fields not compared to policy | FAILED (3) |
| grid/sample field sets unchecked | FAILED |
| int coercion allowed in grid/sample | FAILED |
| `var_valid` flags accepted by truthiness | FAILED |
| builder narrows the fingerprint domain to `keep_vars` | FAILED (2 + 1) |
| `day_index` not bound to the date | FAILED (2) |
| date binding relaxed to a range check only | FAILED |
| `var_valid` key set not compared to canonical `VARS` | FAILED |
| `block_path` field unchecked | FAILED |
| `version` coerced | FAILED (3) |
| `source_day_index` not strictly validated | FAILED (4) |
| `build_artifact` mints a foreign seed | FAILED (15 + 10) |
| no in-lock provenance verification at all | FAILED (19 + 3) |
| waived recheck still reports `verified` | FAILED |
| the waiver does not disable the reader | FAILED |
| skipping provenance entirely reports `verified` | FAILED |

**Two mutations survive by construction and are recorded rather than listed above:**

- **NaN normalisation.** Removing the explicit `isnan` branch leaves the suite green, because
  CPython's `format(float('nan'), '.9g')` already yields `"nan"` for both sign bits — verified
  empirically, not assumed. The branch is redundant on this platform and is kept as an explicit
  statement of the semantic, not as a gated guard.
- **`absent` token spelling.** Encoding an absent variable as `None` instead of `"absent"`
  changes the canonical JSON but leaves every distinction intact, so no test can separate them.

Four mutations initially survived. Two were the equivalences above; two were real test gaps
(artifact segment identity, and source-vs-output fingerprinting) and now have tests.

## 4. Reproduce

```bash
dev2026/.venv/bin/python -m unittest dev2026.tests.test_phase2_p5s5
```

## 5. Residual risks

1. **Sampling is probabilistic** (§1). E1 is the deterministic gate and is not yet wired.
2. **Parts 2 and 3 are not delivered**, so the corrected-day lifecycle still has no enforced
   ordering: `prune_delta` does not consult the WAL, and **delta prune must not be run against a
   corrected day** on the strength of this step.
3. **The (e2) composite fence does not exist**, and the §14 stop condition has not been
   evaluated. Until Part 3 decides, publication should keep the quiesced posture (§15).
4. **Post-commit audit-log failure** remains ambiguous (S4 §12.3, §14.3).


## 6. Review round 2 — two findings

### 1. [High] A `var_valid: False → True` source backfill was invisible

`_read_source_window` used the **artifact's** `var_valid` to decide whether to read a variable
from the source at all. A variable absent at build time was skipped — `tiles[var] = None`, no
read — so if the source was later **backfilled to present**, the fingerprints still matched and
the publication passed.

That is the worst possible case to miss: an absent→present transition is precisely what a
corrective refold exists to materialize, and it is invisible to a value-only comparison, because
backfilling a *new* variable changes nothing about the sampled cells of the variables that were
already there. So the one case the check most needed to catch was the one it structurally could
not see.

The source's `var_valid` is now **read from the source** over the canonical `VARS` domain and
compared to what the build recorded, **before** any value is read. Both directions are tested,
on both source kinds — delta and daily resolve `var_valid` through different code paths
(`inspect_cube` vs `inspect_daily`), so one test would not have covered the other. A legitimately
partial source that has *not* moved must still publish, so the guard cannot pass by refusing
everything.

*This also forced a real fix underneath:* the builder digested only `keep_vars` while the
verifier now spans `VARS`, so the two sides digested different key sets and disagreed on every
day for a reason that was not a difference. `VARS` is now the canonical domain on all three
sides (builder, block check, source check).

### 2. [Medium] The artifact could choose how weakly it was checked

The verifier read `ny`, `nx` and `seed` **out of the artifact**. Anyone able to edit it and
recompute the checksum could declare a 4×4 grid or a different seed and be verified against a
sample of their own choosing — smaller, or aimed away from whatever was altered. **A verifier
that takes its strictness from the document it is verifying has none.**

- **Grid** now comes from `inspect_store_contract(block_path)`. The artifact's declaration is
  compared against it and a mismatch refuses.
- **Seed** is `sp.SAMPLE_SEED`, a module constant. It was a `build_block` parameter; that
  parameter is **removed**. Rotating the sample is now a code change on both sides, which is
  reviewable — not something an artifact can request.
- **`sample` and `grid`** are validated strictly: exact field sets, `algo == "sha256"`, and
  `seed`/`points`/`window` equal to policy, with **no coercion** (`int("32")` and `int(True)`
  both succeed, and a coerced value is not the value that was written).
- **`var_valid` flags must be raw `bool`.** Truthiness would let `1`, `"true"` and `[]` each
  mean something, and this field decides whether a variable is read from the source at all.
- The builder writes the artifact's grid from the **block's inspection**, not the source probe.

### A guard I added and then removed

I first added an explicit `BuildRefused` when the fill's probe grid disagreed with the block's
inspection. It can never fire: `inspect_store_contract` already refuses a block whose `region`
does not match its own grid, so the build fails before a plan or artifact exists. The test now
pins **that** behaviour instead, and asserts no artifact is left behind. Unreachable code that
reads as a guard is worse than no guard — the same call made in P5-S4 round 8.

### Two mutations survive by construction (round 2)

- **seed read from the artifact.** With `load_artifact` forcing `sample.seed == SAMPLE_SEED`,
  reading it from the document is equivalent to reading the constant. The constant is used
  anyway, so the two checks are layered rather than redundant-in-effect.
- **builder uses the probe grid.** The store-contract guard makes probe and inspection grids
  provably equal, so the substitution cannot change any output.

Both are recorded here rather than listed as verified guards.

## 7. Review round 3 — five findings

### 1. [High] `day_index` was bound to the array bounds, not to the date

The verifier checked only that the index was in range. An in-range index still **selects a
slot**, and if that slot's sampled cells happen to agree — an all-NaN region, a repeated value,
a short block — the fingerprint matches and the artifact has attested day D against another
day's bytes. Now `insp.days[t_idx]` must equal `day`.

The test does not take the easy route of leaving a stale fingerprint behind: it **re-fingerprints
against the slot the artifact now points at**, so the byte comparison passes and *only* the date
binding can refuse. A mutation that relaxes the check back to a range test fails it, which is
what proves the test is testing the binding and not the bounds.

### 2. [Medium] `var_valid` was not required to cover the canonical variables

Only "non-empty dict" was enforced, so a missing key was silently read as `False` by the
verifier's `.get(..., False)` and an unknown key was ignored. **A partial map bought a partial
check** — and `var_valid` is the field that decides whether a variable is read from the source
at all.

`load_artifact` now takes `expected_vars` as a **required** keyword and demands exact set
equality. Required rather than optional because the canonical set is not knowable from the
document, and being forced to state it is what stops the check being skipped. Tested in both
directions plus a `TypeError` test on omitting the argument.

### 3. [Medium] `block_path` was recorded and never checked

Renaming it in the artifact published successfully. It is part of the audit record, so an
unchecked field is a *wrong* audit record rather than a harmless label. Now compared against the
canonical basename of the block being published. (The alternative the review offered — delete
the field — would have removed information an auditor wants; checking it keeps it.)

### 4. [Low] `version` and `source_day_index` accepted coercion

`int("1")` and `int(True)` both succeed. Both now go through the same strict `_exact_int` the
grid and sample fields use.

### 5. [Low] `build_artifact()` still accepted a `seed`

It defaulted to policy, so the only thing the parameter could do was mint an artifact that
`load_artifact` would later always reject. **An API that can produce only-invalid output is a
trap**: the failure surfaces at publication, far from the call that caused it. The parameter is
removed and the seed is taken from the constant.

*Knock-on:* the P5-S4 artifact helper minted artifacts with the old signature and a
`keep_vars`-shaped `var_valid`. Updated — the S4 suite is regression coverage for this contract,
so it has to build artifacts the way the builder does.

## 8. Review round 4 — two findings

### 1. [High] Verification ran before the locks, so it verified a state that could then change

The whole byte check happened before the compaction reservation and the ingest lock were taken.
Between it and `bm.publish` the staging block or a source could be modified, and the only thing
left in the way was `staleness_guard` — which compares **locators** and structurally cannot see
bytes. So the guarantee "source changed → refuse" held at a moment that was not the moment that
mattered.

Verification now runs **inside the critical section**, after both locks and after the staleness
guard, immediately before the fence assertion and the commit.

**There is deliberately no earlier copy.** A pre-lock check would be a cheap fail-fast, and it
was tempting to keep one — but with the in-lock check present, no mutation could kill it. It
would read as a guard while being none, which is the same call made in P5-S4 round 8 and P5-S5
round 2. A test pins the consequence: with the artifact deleted after planning, the refusal must
come from inside the critical section, proving the locks were taken first.

**On cost, since this is the mirror of a decision made the other way.** `measure_bytes` was kept
*out* of the lock because it walks ~94 GB. This is one sample window per (day, var) from the
block and from each source — tens of megabytes for a 90-day fold, seconds. Three orders of
magnitude smaller, and unlike a size measurement it is a correctness gate that has to hold at
the commit point rather than approximately before it.

Ordering inside the lock is cheap-check-first: the staleness guard reads delta attrs and runs
before the byte comparison. One consequence, recorded rather than hidden: a *vanished* source is
now caught by the staleness guard, so `verify_build_artifact`'s own gone-source refusal is
shadowed in the publication path. It is therefore tested **directly** against
`verify_build_artifact`, where it can be reached — an untestable refusal is not a refusal.

The interleaving tests are deterministic — no threads, no sleeps. They hook
`bind_segment_to_block`, which sits immediately before the byte verification inside the lock, and
mutate the block or the source there. A third test asserts the hook actually fired, so the other
two cannot pass for the wrong reason.

### 2. [High] `recheck_sources=False` was an unlabelled bypass reporting success

It disabled the source recheck — the entire `source changed → refuse` guarantee — while the
result still said `verified: True`. **A bypass that reports success is worse than no bypass**,
and it contradicted the doc's claim that `unsafe_skip_provenance` was the only waiver.

Renamed to `unsafe_skip_source_recheck`, matching the established pattern, and the result now
reports `verified: False`, `source_recheck: "waived"`, `sources_rechecked: 0` and a reason
saying which guarantee does not hold. The same is true when provenance is skipped entirely.

Tested by making a source change that **must** refuse without the waiver and **must** publish
unverified with it — so the waiver is what makes the difference, not the fixture. The P5-S4
locator tests, which legitimately use it, now assert the waived reporting, so their bypass
cannot be mistaken for a pass.