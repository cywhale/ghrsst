# P5-S1 — manifest schema + segmented-store prototype: RESULTS

Status: **DONE — gate PASS. [MUT-STG]: staging/synthetic only.** No VM24 path was opened, no
`GHRSST_*` store was read, no production file was touched, and nothing was published anywhere
but a temp directory. The compaction builder (`build_block`), the publish executor and the
proof tests remain unwritten — those are P5-S3, P5-S4 and P5-S5.

Implements P5-S1 of [`p5_segmented_timecube_compaction_design.md`](p5_segmented_timecube_compaction_design.md) §14.

- Manifest: [`../store/block_manifest.py`](../store/block_manifest.py)
- Segmented store: [`../store/segmented_cube.py`](../store/segmented_cube.py)
- Fixtures (F1–F3, F6, F11, F12): [`../tests/p5_fixtures.py`](../tests/p5_fixtures.py)
- Tests: [`../tests/test_phase2_p5s1.py`](../tests/test_phase2_p5s1.py) — **80/80 green**
- Full local suite: **358 tests OK** (17 skipped), up from 278; no regressions.

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

A **second** review round then found three more, because the first fix made the fingerprint
*structural* but not *semantic* — and because a manifest generated from its own store is
self-consistent by construction, so anything the fingerprint alone guards is untested:

| defect | why the first fix missed it | now |
|---|---|---|
| **`var_valid` CONTENT was not fingerprinted** — only its length. Flipping one flag turns a day from "returns `sst`" into "omits `sst`", an API-visible semantic change inside a block that claims to be immutable | the fingerprint recorded `var_valid_len`, so a flip was invisible | `var_valid_digest` per variable; the snapshot rejects the mutated store |
| **Only `vars_[0]`'s layout was checked** — `sea_ice` shortened to 89 days loaded fine and raised `IndexError` on day 90; a variable with different chunking was also accepted | `segment_layout` summarized the first array and the snapshot compared only that | `segment_layout` **errors** if arrays disagree; the snapshot checks **every** variable's time length and spatial shape |
| **`variables` and `grid.region` were unbound** — a segment could declare `["sst"]` while serving three, and a manifest could declare any region against a store carrying none | the region check required *both* sides to be present; `variables` was never compared to the store | `segment.variables == store.vars`; top-level `variables == union(segments)`; a store without `attrs["region"]` is admissible **only** for the full grid |

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
| **`var_valid` CONTENT in the fingerprint** | `test_var_valid_CONTENT_changes_the_fingerprint` (+1) | **FAILS** ✅ |
| **cross-variable layout agreement** | `test_variables_with_inconsistent_layout_are_rejected` | **FAILS** ✅ |
| **per-variable time length** | `test_every_variable_is_layout_checked_not_just_the_first` | **FAILS** ✅ |
| **per-variable spatial shape** | `test_a_variable_with_the_wrong_spatial_shape_is_rejected` | **FAILS** ✅ |
| **`segment.variables == store.vars`** | `test_segment_variables_must_equal_the_store_variables` | **FAILS** ✅ |
| **top-level `variables == union`** | `test_top_level_variables_must_be_the_union_of_segments` | **FAILS** ✅ |
| **region without a stored region** | `test_region_must_be_verifiable_even_when_the_store_omits_it` | **FAILS** ✅ |
| **`var_valid` keys == `vars`** | `test_missing_var_valid_key_is_rejected` | **FAILS** ✅ |
| **`var_valid` strict `bool` type** | `test_non_boolean_var_valid_entry_is_rejected` | **FAILS** ✅ |
| **`var_valid` length** | same | **FAILS** ✅ |
| **declared var has a real array** | `test_declared_var_without_a_real_array_is_rejected` | **FAILS** ✅ |
| **`vars` uniqueness** | `test_raw_declared_vars_must_be_unique_and_three_dimensional` | **FAILS** ✅ |
| **`dtype == float32`** | `test_non_float32_or_non_nan_fill_variable_is_rejected` | **FAILS** ✅ |
| **`fill_value` is NaN** | same | **FAILS** ✅ |
| **implicit `var_valid` needs a declared mode** | `test_absent_var_valid_requires_an_explicit_legacy_migration_mode` | **FAILS** ✅ |
| **implicit mode is `legacy_base` only** | same | **FAILS** ✅ |

Two mutations survived the first pass and both were instructive. One was a **badly chosen
mutation** (renaming the fingerprint key changed both sides equally, so the difference test
still held — re-run by replacing the digest with a constant, which does fail). The other was a
**genuine gap**: nothing covered a variable whose *spatial* shape disagreed with the lon/lat
axes, since `axes` comes from the coordinate arrays and `layout` compares chunking. A test was
added for it.

A **third** round found three more, and they share one shape with the first two — which is
the finding that actually matters:

> **Validation must read RAW state. Every helper that filters (`if v in g`) or coerces
> (`bool(x)`) launders a malformed store into a well-formed one, and every downstream check
> then passes because it is looking at the laundered view.**

| defect | how the laundering worked | now |
|---|---|---|
| **`var_valid` was not validated as a complete boolean vector** — deleting a key made the variable silently "valid on every day"; a string `"false"` was accepted, while the read path tests `is False` so it would not behave as its value implies | only the *lengths of existing entries* were checked, and `bool(x)` ran **before** validation | keys must equal `attrs["vars"]`; every entry a `list`; every item `type(x) is bool`; every length `== len(days)`; **no coercion before validation** |
| **`attrs["vars"]` could name an array that does not exist** — `.vars` still advertised `sea_ice` while point reads silently omitted it | `store_variables()` filtered with `if v in g`, so a self-inconsistent store became a "valid" one | raw `vars` validated first: unique, every declared variable has a real **3-D** array; `metadata_fingerprint` now **refuses to fingerprint** a broken store, so a builder cannot mint a manifest for one either |
| **dtype and fill-value were only in the self-derived fingerprint** — a `float64` / `fill_value=0` array was accepted, turning an unwritten or missing chunk from `null` into a real `0.0` at the API | the fingerprint matched by construction; nothing checked the builder contract independently | per variable: `dtype == float32` and `fill_value` is NaN, verified independently (and `fill_is_nan` added to the fingerprint) |

Absent `var_valid` now needs an **explicit, checkable migration mode**: `var_valid_mode:
"implicit_all_true"`, permitted **only** on a `legacy_base` segment. Silent inference is what
made a malformed store valid in the first place.

A **fourth** round closed the last two bypasses around the validator — and they are the ones
that matter, because until they were closed the "single raw-state validator" was not actually
single:

| bypass | what got through | now |
|---|---|---|
| **Container coercion** — `list()` / `dict()` ran before the type was confirmed | `var_valid` written as a JSON **list-of-pairs** was laundered into a dict; duplicate keys inside it would have been resolved to the last one, silently | `_exact()` requires `type(x) is list` / `is dict` / `is str` **before any conversion**; `days` elements must parse as ISO dates; `vars` elements must be `str`; `var_valid` flags must be a `list` |
| **The fingerprint path skipped the validator entirely** — it read the store directly | a builder could still mint a manifest for non-boolean `var_valid`, duplicate `vars`, or `fill_value=0`; only the missing-array case was covered | one path only: `inspect_store_contract()` → `metadata_fingerprint_from_inspection()`. The public `metadata_fingerprint()` inspects strictly first, and the legacy allowance is an **explicit argument** because a fingerprint has no segment context to infer it from |

`StoreInspection` carries a module-private token that only `inspect_store_contract` sets, and
`metadata_fingerprint_from_inspection` refuses anything else — a hand-built look-alike cannot
side-step the strict path.

**Snapshot ordering was also corrected.** The build used to construct a `TimeCubeStore` first
and validate raw state afterwards. It still failed closed, but it broke the model. The order is
now: **inspect → verify grid/axes/layout/fingerprint/day-set against that one view → construct
the reader → assert the reader's metadata equals the inspection.** A test installs a tripwire
`TimeCubeStore` and requires that it is never constructed for a malformed store.

A **fifth** round closed the last two, and both were on surfaces the previous rounds had not
reached:

| defect | what got through | now |
|---|---|---|
| **The reader↔inspection binding was partial** — it compared only `days` and `vars` | a `var_valid` flip landing **between** the verified inspection and the reader's metadata capture was installed unvalidated, omitting `sst` on day 0 while the fingerprint attested to the *old* inspection. Textbook TOCTOU, and it changes API semantics | the **whole** inspection is bound: `days`, `vars`, effective `var_valid` (legacy implicit normalized to all-true), lon/lat digests, and every array's shape/chunks/shards/dtype/`fill_is_nan`. The error names which keys differ |
| **Coordinate axes had no contract** while the read path assumes one | `lon.shape = (1,32)` was accepted and blew up inside the read; a **descending** axis was accepted and silently resolved lon `100` to grid lon `131` | axes must exist, be **1-D**, non-empty, all-finite and **strictly increasing** — because `TimeCubeStore._nearest_idx` uses `np.searchsorted`. The error says so, so relaxing it later means changing the index resolver first, not the validator |

`region` is validated at the same time: exactly **four raw `int`s** (no `int(x)` laundering a
`"0"` or a `0.5`), ordered, and inside `ny`/`nx`.

**Correction to an earlier overclaim.** `_INSPECTION_TOKEN` was described as making a forged
inspection impossible. It does not: it is a module-private convention that prevents
**accidental** bypass — a stale dict from an older code path, a hand-built look-alike — and a
caller who reaches for `bm._INSPECTION_TOKEN` can forge one. It is not a security boundary and
the code no longer claims otherwise.

**A note on why fingerprints alone are not enough.** When a manifest is generated from the
store it describes — which is exactly what S3's builder will do — `fingerprint.metadata`
matches by construction. Anything guarded *only* by the fingerprint is therefore untested
against a store that was wrong from the start. That is why the snapshot now carries
**independent** checks (grid, axes identity, per-variable shapes, variable sets, region) rather
than delegating everything to one hash. Also why the day set is deliberately **excluded** from
`metadata_fingerprint`: including it made the fingerprint fire before the day-set comparison
and robbed that comparison of its isolating test.

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
- **R1a mandatory disjointness at assembly** (§17) — `assert_disjoint_from` ships here as an
  *optional* helper; **S2** must make `TieredCube`/`TieredSnapshot` construction call it
  unconditionally and fail closed. An invariant that relies on an operator remembering is not
  an invariant.
- **Sizing.** Untouched. `S`/`C` remain the §10.8 provisional recommendation for **P5-S6** to
  adjudicate on the production calendar anchor.

## 7. Reproduce

```bash
dev2026/.venv/bin/python -m unittest dev2026.tests.test_phase2_p5s1
dev2026/.venv/bin/python -m unittest discover -s dev2026/tests -p "test_*.py"
```
