# P5-S1 — manifest schema + segmented-store prototype: RESULTS

Status: **DONE — gate PASS. [MUT-STG]: staging/synthetic only.** No VM24 path was opened, no
`GHRSST_*` store was read, no production file was touched, and nothing was published anywhere
but a temp directory. The compaction builder (`build_block`), the publish executor and the
proof tests remain unwritten — those are P5-S3, P5-S4 and P5-S5.

Implements P5-S1 of [`p5_segmented_timecube_compaction_design.md`](p5_segmented_timecube_compaction_design.md) §14.

- Manifest: [`../store/block_manifest.py`](../store/block_manifest.py)
- Segmented store: [`../store/segmented_cube.py`](../store/segmented_cube.py)
- Fixtures (F1–F3, F6, F11, F12): [`../tests/p5_fixtures.py`](../tests/p5_fixtures.py)
- Tests: [`../tests/test_phase2_p5s1.py`](../tests/test_phase2_p5s1.py) — **49/49 green**
- Full local suite: **327 tests OK** (17 skipped), up from 278; no regressions.

## 1. Gate

> **§14 P5-S1 gate:** every malformed/stale manifest is rejected **without ever exposing a
> partial snapshot**; plus **G14** (fixed calendar boundaries + three-way classification).
> **Stop if** a fail-closed path is found that can serve a partial view.

**PASS — but only after a review round that found three ways to reach a serving snapshot.**
The first cut of this step claimed the gate on the strength of tests that did not probe path
authority or structural binding at all, and Codex reproduced all three. They are fixed, each
has a regression test, and each guard is mutation-proved (§4). Recording the failure honestly
matters more than the eventual PASS: **the gate conclusion was wrong when first stated.**

| defect | reproduction | now |
|---|---|---|
| **`day_list` escaped the calendar window** — it was taken verbatim, so a segment could declare a 2026 window and serve 2027 days, splitting the block grid from real availability (G14) | `DAYLIST_ACCEPTED 2027-06-27 2027-09-24` | rejected at schema validation |
| **`path: "../delta.zarr"` on a segment innocently named `not-delta-by-key`** — the §4.0 guard only inspected key *names*, never path authority | `DELTA_PATH_ACCEPTED …/delta.zarr` | rejected at snapshot build |
| **Grid mismatch** — a 16×16 block loaded happily under a manifest declaring 32×32, so two segments could map the same lon/lat to different physical cells | `GRID_MISMATCH_ACCEPTED manifest 32×32 / actual 16×16` | rejected at snapshot build |

## 2. What was built

### 2.1 `block_manifest.py`

Schema, validation, calendar arithmetic, atomic publish, and copy-then-replace rollback.

- **Calendar block grid (§5.2).** `block_bounds(anchor, S, k)` is pure arithmetic —
  `[anchor + k·S, anchor + (k+1)·S − 1]`, fully determined before any data is read. A
  `boundary_kind: "calendar"` segment whose declared bounds disagree with the grid is
  rejected. Verified: anchor `2026-06-27`, S=90 → `2026-06-27..2026-09-24`,
  `2026-09-25..2026-12-23`, `2026-12-24..2027-03-23`, each exactly 90 calendar days.
- **Three-way classification (§5.3).** `present + confirmed_missing + unknown == S` is a hard
  invariant for calendar blocks; the three sets must be disjoint and lie inside the window;
  every day past `materialized_through` must be `unknown`.
- **Seal condition (§5.4).** `sealed: true` with a non-empty `unknown` is a **schema
  violation**, not a warning.
- **§4.0 delta guard.** Any top-level or per-segment key containing `delta` is rejected with
  an explicit message. Two authorities over the same days is the ambiguity this prevents.
- **Ordering (§5.1).** Segments must be ascending **and non-duplicating** by `start_day`;
  order is normative and never inferred from filenames.
- **Publish/rollback (§9.3).** `publish` validates → writes the immutable
  `manifest.gen<N>.json` (fsync) → `os.replace` of a temp copy onto `manifest.json` → fsync
  of the directory. Re-publishing an existing generation is refused (archives are immutable).
  `rollback_to` **copies** the archive → verifies (parse, checksum, generation) → fsyncs →
  `os.replace(temp, live)`, and asserts afterwards that the archive still exists.

### 2.2 `segmented_cube.py`

`SegmentedCubeStore` — immutable snapshot, base-only, grouped reads.

- **Snapshot** (`_Meta`, mirroring `TimeCubeStore`): generation, checksum, segment handles,
  the merged `day → (segment idx, local idx)` map with precedence applied, chronological
  `days`, `latest`, union `vars`. Built entirely off-lock, swapped atomically.
- **Never sees delta.** Asserted by spying on `zarr.open_group` for a whole build + read
  cycle and requiring that no opened path contains `delta`, with a real 31-day delta fixture
  present on disk beside it.
- **Grouped reads (§6.2).** Requested days are resolved through the map, grouped by segment,
  and served with **one `point_series` call per segment**. Asserted by wrapping each segment's
  `point_series` with a counter: a 90-day request spanning two segments issues exactly **2**
  calls, not 90.
- **Generation-aware refresh.** `refresh_if_changed()` reads only the small manifest; if the
  generation and checksum are unchanged it returns `False` **without reopening any segment**
  (asserted by identity: `store._meta` must be the same object).

## 3. `SegmentedCubeStore` is STATIC-COMPOSITION compatible with `TieredCube`

Not required until S2, but exercised now because it de-risks that step. **Scope the claim
carefully** — what is proven is *static* composition:

> fixed base snapshot + fixed delta snapshot ⇒ correct precedence, order and dedup.

**Not** proven, and explicitly still open: that a base manifest refresh or a delta refresh
landing **mid-request** cannot mix generations across tiers. `TieredCube.point_series` consults
delta membership and then calls each tier separately; it never captures one **composite**
snapshot. That is risk **R1** in the spec's new §17 register — implemented in **S2**
(`TieredSnapshot(base_snapshot, delta_snapshot, day_source_map)`) and proved in **S5/G10**.

With that scoping, `TieredCube(SegmentedCubeStore(...), TimeCubeStore(delta))` composes:

| property | result |
|---|---|
| union availability / `latest` | base ∪ delta, `latest = max(delta days)` |
| **delta wins on overlap** | the delta fixture offsets values by +1000, so precedence is directly observable — the overlapping day returns the delta value, a base-only day returns the block value |
| crossing range | requested order preserved, no missing days, no duplicates |

`TieredCube` needed no modification **for static composition** — it consumes `_day_index` for
membership and `point_series` for reads, and the segmented store provides both. It *will* need
modification in S2 for the composite per-request snapshot (R1). The earlier phrasing here
("needed no modification") overclaimed and has been corrected.

## 4. Fail-closed behaviour, proved by mutation

Fail-closed tests are worthless if they pass for the wrong reason. Each guard was disabled in
turn and the suite re-run; **every one must fail**.

| guard disabled | isolating test | result |
|---|---|---|
| day-**set** comparison | `test_stale_manifest_same_count_different_days_fails_closed` | **FAILS** ✅ |
| equal-precedence duplicate-day check | `test_overlapping_days_at_equal_precedence_fail_closed_at_snapshot` | **FAILS** ✅ |
| `day_digest` comparison | `test_wrong_day_digest_fails_closed` | **FAILS** ✅ |
| retain-previous-snapshot-on-failure | 3 tests in `TestSnapshotFailsClosed` | **FAIL** ✅ |
| **`day_list` window validation** | `test_day_list_cannot_escape_the_calendar_window` (+2) | **FAILS** ✅ |
| **block path confinement** | `test_a_segment_path_may_not_point_at_the_delta` (+2) | **FAILS** ✅ |
| **grid ny/nx check** | `test_grid_mismatch_fails_closed` | **FAILS** ✅ |
| **lon/lat axes identity** | `test_segments_must_share_identical_lon_lat_axes` | **FAILS** ✅ |
| **layout check** | `test_layout_mismatch_fails_closed` | **FAILS** ✅ |
| **`fingerprint.metadata` verification** | `test_metadata_fingerprint_is_required_and_verified` | **FAILS** ✅ |
| **`var_valid` length check** | `test_var_valid_length_must_match_day_count` | **FAILS** ✅ |
| **`fingerprint.metadata` required** | same | **FAILS** ✅ |

**Two of these tests did not exist until the mutation run exposed them as vacuous**, and that
is the honest part of this step:

- the original stale-manifest test corrupted a segment to a *1-day* set, so the **day-count**
  check fired first and the day-**set** check was never exercised. Replaced with a set of the
  **same length** but shifted by a year.
- the original duplicate-day test used two segments sharing a `start_day`, which schema
  validation now rejects *earlier*, so the snapshot-level equal-precedence check was never
  reached. Replaced with **distinct `start_day`s and overlapping days at equal precedence**,
  plus a counterpart proving that overlap at **different** precedence is legal and resolves
  silently (otherwise the first test could be satisfied by rejecting all overlap).

## 5. Test coverage (37)

| area | tests |
|---|---|
| calendar grid (§5.2, G14) | pure arithmetic; a gap never shifts a boundary; bounds must match the grid |
| classification (§5.3/§5.4) | unsealed block with `unknown` is valid; `sealed` + `unknown` rejected; partition enforced; overlapping classes rejected |
| schema guards (§4.0/§5.1) | round-trip; checksum tamper; missing field (×6); unknown field; **three delta-field shapes rejected**; ordering; duplicate `start_day` |
| superseded (§5.6) | cumulative entries survive generations; missing required field rejected |
| snapshot fail-closed | happy path; day-count mismatch; **same-count-different-days**; **wrong day_digest**; missing path; bad first build serves nothing; **equal-precedence duplicate days**; distinct precedence legal |
| precedence + reads (§5.1/§6) | higher precedence wins and days dedupe; values come from the winning segment; **one call per segment**; absent days omitted, never fabricated |
| base-only (§4.0) | `zarr.open_group` spy proves the delta path is never opened |
| publish/rollback (§9.3) | immutable archive + atomic pointer; **archive byte-unchanged after rollback**; missing generation refused; corrupt archive never becomes live; refresh is a no-op on an unchanged generation |

## 5a. Path authority and structural binding (added in review)

**Path authority (§4.0).** Rejecting delta by *key name* was never sufficient — authority lives
in the path. Now kind-specific:

- `block` → `realpath` must resolve **inside the block root**; absolute paths refused; symlink
  escape refused (`realpath` + containment, not string prefixes).
- `legacy_base` → inside the root, **or** on the deployment's explicit
  `allowed_legacy_paths`. Production's legacy monolith legitimately sits outside the root
  (`../mur_timecube_s8_t90_sh128.zarr`), so `..` cannot simply be banned; the escape must be
  **named by configuration**, never accepted from the manifest alone.
- `assert_disjoint_from(delta_path)` — a composition-time assertion that no base segment
  resolves to the delta.

**Structural binding (§5 fingerprints).** A day set cannot detect a block built on a different
grid. Snapshot build now verifies, per segment: `ny`/`nx` against the manifest `grid`, `region`,
**identical lon/lat digests across all segments**, actual chunk/shard `layout`, `var_valid`
length against the day count, and `fingerprint.metadata` — which is now **required** and
recomputed from disk (`bm.metadata_fingerprint`, covering axes, region, variables, per-array
shape/chunks/shards/dtype and `var_valid` lengths).

The test helper builds segment entries by **deriving `layout` / `day_digest` /
`fingerprint.metadata` from disk**, the way S3's builder will — so a test that then mutates the
store or the manifest exercises a genuine mismatch rather than a hand-written constant.

## 6. Deliberately not done here

- **`build_block`** (P5-S3), the **publish executor + `pre_swap` machinery** (P5-S4), and the
  **adversarial crash/concurrency proof tests** (P5-S5). §9.3a restore-before-rollback,
  the repair WAL (§7.5a-1) and the compaction `flock` (§7.1b) all belong to those steps.
- **Full P1-oracle parity and performance** — P5-S2. This step proves the manifest and the
  snapshot; S2 proves the read path is semantically identical to `TieredCube` on every fixture
  and measures it against the S0 baseline of record.
- **R1 composite base+delta snapshot** (§17) — S2/S5. S1 proves static composition only.
- **Sizing.** Untouched. `S`/`C` remain the §10.8 provisional recommendation for **P5-S6** to
  adjudicate on the production calendar anchor.

## 7. Reproduce

```bash
dev2026/.venv/bin/python -m unittest dev2026.tests.test_phase2_p5s1
dev2026/.venv/bin/python -m unittest discover -s dev2026/tests -p "test_*.py"
```
