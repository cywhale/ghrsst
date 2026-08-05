# P5-S5 — proof step: RESULTS (PARTIAL — one of three parts delivered)

Status: **Part 1 (source-byte provenance) DELIVERED. Parts 2 and 3 NOT DELIVERED.**
`[MUT-STG]`: staging/synthetic only. No VM24 path, no `GHRSST_*` store, no cron, nothing
written outside a temp dir.

Branch `dev2026-p5-s5-proof`, stacked on `9bc1795`.

- Provenance primitive + artifact: [`../store/source_provenance.py`](../store/source_provenance.py)
- Builder emits the artifact: [`../ingest/build_block.py`](../ingest/build_block.py)
- Publication verifies it: [`../ingest/publish_manifest.py`](../ingest/publish_manifest.py)
- Tests: [`../tests/test_phase2_p5s5.py`](../tests/test_phase2_p5s5.py) — **28/28 green**
- P5-S4 regression: **155/155 green**
- Full local suite: **639 tests OK** (17 skipped), up from 611
- `-W error::ResourceWarning` over S4+S5: **183 OK**

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

- `build_block(..., provenance_seed=20260805)` — writes `p5_block_provenance.json` into
  `artifacts_dir`, and returns `provenance` / `provenance_path` on the plan.
- `execute_publication(..., build_artifact_path=...)` is now **required**, with
  `unsafe_skip_provenance=True` as the single greppable waiver (the `build_block`
  `unsafe_skip_isolation` pattern). `recheck_sources=False` exists for fixtures whose block is
  not built from its declared source; the P5-S4 locator tests use it and say so.
- The publish result carries `provenance: {verified, days, sources_rechecked}`.
  `sources_rechecked` is asserted against the day count, so a source quietly not re-read shows
  up as a number rather than as a pass.

## 3. Mutation verification

16 guards disabled in turn; **all 16 fail**.

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
