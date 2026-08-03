# P5-S2 — grouped multi-segment read path + composite snapshot: RESULTS

Status: **DONE for the gates S2 can decide — G3 is DEFERRED to S6/S7, with the reason
measured rather than assumed.** `[MUT-STG]`: staging/synthetic only. No VM24 path, no
`GHRSST_*` store, nothing written outside a temp dir.

Implements P5-S2 of [`p5_segmented_timecube_compaction_design.md`](p5_segmented_timecube_compaction_design.md) §14.

- Composite snapshot: [`../store/tiered_cube.py`](../store/tiered_cube.py) (`TieredSnapshot`)
- Meta-explicit reads: [`../store/time_cube.py`](../store/time_cube.py),
  [`../store/segmented_cube.py`](../store/segmented_cube.py) (`point_series_from`)
- Fixtures F4–F9, F18: [`../tests/p5_fixtures.py`](../tests/p5_fixtures.py)
- Tests: [`../tests/test_phase2_p5s2.py`](../tests/test_phase2_p5s2.py) — **25/25 green**
- Bench: [`../bench/bench_p5_segmented_read.py`](../bench/bench_p5_segmented_read.py) →
  [`../bench/results/p5s2_segmented_read.json`](../bench/results/p5s2_segmented_read.json)
- Full local suite: **387 tests OK** (17 skipped), up from 362.
- Request-level snapshot: [`../store/hybrid_router.py`](../store/hybrid_router.py)
  (`QuerySnapshot`), consumed by [`../api/app.py`](../api/app.py)

## 1. R1 — one composite snapshot per request

Each tier was already individually immutable, but **consulting them at different moments is
not**. The previous `TieredCube.point_series` asked delta for membership, then read base, then
read delta — so a base manifest refresh or a delta refresh landing *between* those steps
produced a result mixing generations across tiers.

`TieredCube.snapshot()` now captures **both tiers' metadata together, once**, into an immutable
`TieredSnapshot(base, base_meta, delta, delta_meta, base_days, delta_days, days, latest)`, and
every read goes through `point_series_from(meta, …)` against exactly those. To make that
possible, `TimeCubeStore` and `SegmentedCubeStore` gained a **meta-explicit** read
(`point_series_from`); their public `point_series` is unchanged and simply passes `self._meta`.

Proven by four tests:

**Round 2 corrected four ways this was still not true.** The first implementation captured the
snapshot in one place and then leaked live reads at three others — the same "capture once"
idea applied at one level but not carried up or down:

| gap | what still read live | now |
|---|---|---|
| **cross-tier capture was two reads** | `base._meta` then `delta._meta`: a refresh landing *between* them yields base-old + delta-new | a composite lock serializes refreshes made through `TieredCube`, **plus** a double-read verification (take both, take both again, accept only if neither moved) so a tier refreshed *directly* — the TTL loop, the delta cron — is detected. If metadata will not settle, **refuse**: `TierCompositionError`, a subclass of `SnapshotError` |
| **the segmented base read live segment metadata** | the grouped read used each segment's `store._meta`, so the outer sentinel test never covered it | `SegmentedCubeStore._Meta` now carries **`segment_metas`**, and the read passes the captured one per segment |
| **the API re-derived its view three times** | availability, then routing, then the read, then the route header | `HybridRouter.query_snapshot()` → **`QuerySnapshot`**, captured once per request in `api/app.py` and used for all four |
| **R1a was not unconditional** | it only ran when the base exposed `assert_disjoint_from` — which `SegmentedCubeStore` does and **`TimeCubeStore` does not**, so it did nothing for the assembly deployed on VM24 today | `TieredCube` compares **realpaths** for any base shape (symlinked delta caught), with the segmented helper kept as a richer secondary message |

| test | asserts |
|---|---|
| snapshot identity + immutability | `snap.base_meta is base._meta`; the `NamedTuple` cannot be mutated |
| **delta refresh mid-flight** | appending a day and refreshing delta leaves a captured snapshot's values **and** day set unchanged; a *new* snapshot sees the day |
| **base generation change mid-flight** | publishing generation 2 and refreshing the base does not grow a captured snapshot |
| **reads never re-consult the live store** | both `store._meta` are replaced with a sentinel object and the snapshot still reads correctly |
| **a refresh interleaved between the two captures** | a callback fires a delta refresh *during* the capture; the returned snapshot is still self-consistent (`delta_days == delta_meta.days`) |
| **metadata that never settles** | refuses with `TierCompositionError` rather than returning a mixed view |
| **refresh holds the composite lock** | tier refreshes observed to run with the lock held |
| **inner segment metas** | replacing every segment's `_meta` with a sentinel does not affect a captured snapshot's read |
| **request-level** | a delta refresh after `query_snapshot()` changes neither availability, nor routing, nor the read; a *new* snapshot sees it |
| **R1a for a `TimeCubeStore` base** | same path as base and delta is rejected; a symlinked delta is rejected; distinct paths compose |

That last one is the decisive check: if any read path still reached for `self._meta`, it would
raise instead of returning rows.

## 2. R1a — disjointness enforced by the assembly

`SegmentedCubeStore` cannot check this: by §4.0 it never sees the delta. So `TieredCube.__init__`
now calls `base.assert_disjoint_from(delta.path)` **unconditionally** and fails closed. A base
segment resolving to the delta path would serve delta bytes as immutable base. It was previously
an optional helper an operator had to remember; an invariant that depends on memory is not an
invariant.

## 3. G1/G2 — parity with the P1 oracle

**The fixture relationship matters more than the assertion.** A first attempt compared a block
and a daily store generated independently and "failed parity" — correctly, because the two held
different numbers. That proves nothing about the read path. `build_block_from_daily()` now
builds the block **by reading the daily store**, which is both the only meaningful oracle and
the relationship S3's builder will have.

| property | result |
|---|---|
| value equality vs `StoreAccess` day-for-day, all 3 variables | ✅ float32, NaN-aware |
| **absent variable → key omitted** on the days it is absent (P1 omit semantics) | ✅ key sets equal to the oracle's, per day |
| **present NaN → `null`**, key retained — not the same as absent | ✅ |
| absent var across a **segment boundary** (F7) | ✅ omitted for its block's days, returned for the neighbour's |
| requested order preserved, no duplicates (G2) | ✅ |
| gap days omitted, never fabricated (F6) | ✅ |
| overlap resolves to delta (F5) | ✅ |

## 4. F4 / F18 — append-order and the outage-then-backfill case

- **F4**: a backfilled day lands at the **tail** of `attrs["days"]`, so the list is append
  order. `latest` comes from `max(days)` and is asserted **≠ `days[-1]`**, and the day is
  readable by date.
- **F18** (risk R4): a 14-day outage then a bulk backfill, out of order. Before repair the
  outage days are simply absent from the result; after it, all 30 days return **in requested
  order with no duplicates**, `days` is provably not sorted, and `latest` is still `max(days)`.

## 5. §6.2 — grouped reads, and zero opens in the read path

A 180-day request spanning two segments issues **exactly 2** `point_series_from` calls — not
180 — and `zarr.open_group` is called **zero** times during the read (asserted by spying on it
for the whole call). Confirmed again in the benchmark at 1, 4 and 12 segments:
`stores_opened_during_read = 0` in every row, and **`max fd growth on open = 0`**.

## 6. G3 — DEFERRED to S6/S7, and why

Measured, monolith vs segmented on **identical days** (**400 stored days, a genuine 366-day request**, 3 variables, delta 31 days):

| segments | segments touched | 1-day p95 | 366-day p95 | vs monolith | crossing p95 | snapshot open p95 |
|---|---|---|---|---|---|---|
| 1 | 1 | 2.61 ms | **7.33 ms** | 1.00× | 38.9 ms | 7.4 ms |
| 4 | 4 | 2.84 ms | **15.11 ms** | **2.06×** | 50.1 ms | 26.8 ms |
| 12 | 11 | 2.59 ms | **26.43 ms** | **3.61×** | 58.1 ms | 83.4 ms |

**Added cost per extra array call: 0.751 ms** — consistent with P5-S0's independently measured
0.65 ms per extra segment.

On its face that is a G3 failure. It is not reported as one, because **a ratio is not portable
between fixtures**: it is the added per-call cost divided by the baseline's absolute magnitude.
On this synthetic fixture a 366-day 3-variable read costs ~7.3 ms, so ~2 ms/call of Python+zarr
call overhead *is essentially the whole measurement*, and the ratio approaches the call-count
ratio.

**I checked the obvious explanation and it was wrong, which is worth recording.** I expected the
ratio to shrink as the grid grew, since real decompression work would then dominate the fixed
per-call cost. It does not — measured at 64², 256² and 512² the ratio stayed **1.67× / 1.78× /
1.83×**. The reason is that a *point* read touches one chunk per time block regardless of `ny`
and `nx`, so enlarging the grid does not enlarge the work either.

What does change the ratio is the **baseline's absolute magnitude**, which on VM24 is **76–100
ms** for a 366-day range (v0.5.0). The same absolute overhead — ~12 extra calls × 0.545 ms ≈
**+9 ms** at S = 90 — projects to roughly **+9–12 %** there. That is a projection, not a
measurement, and it is exactly the adjudication **S6/S7** own on production geometry.

So this step records:

- **G3 verdict: `DEFERRED to S6/S7`**, with `g3_adjudicable_here: false` and the reason stored
  in the artifact. It is not marked passed.
- The **scale-independent** number S6 actually needs: `added_ms_per_extra_array_call`.
- The S2 gate the fixture *can* decide — zero store opens in the read path, bounded fd growth —
  which is what the harness exits non-zero on.

**Snapshot-open cost** (7.5 → 26.5 → 84.1 ms for 1 → 4 → 12 segments) is roughly linear in
segment count and is R3/G16 groundwork: it runs on process start and on each generation change,
not per request, and must stay well inside the refresh TTL. At the +10-year horizon (41 segments
at S = 90) this projects to ~0.3 s — comfortable, but S6 must measure it rather than extrapolate.

## 7. Risk register

| risk | status after S2 |
|---|---|
| **R1** composite base+delta snapshot | **implemented** at all three levels (cross-tier capture, segment metas, request-level `QuerySnapshot`); the adversarial pause/refresh/resume proof is **S5/G10**, so it stays `OPEN` until then |
| **R1a** disjointness at assembly | **CLOSED** — realpath comparison in `TieredCube` for **any** base shape, `TimeCubeStore` and symlink cases tested |
| **R3** multi-store RSS/FD/snapshot-open | groundwork recorded (fd growth 0, snapshot-open curve); the gates are **S6/S7** |
| **R4** outage + bulk backfill | fixture **F18** exists and passes; the compaction-ordering half needs **S3** |

## 8. Reproduce

```bash
dev2026/.venv/bin/python -m unittest dev2026.tests.test_phase2_p5s2
dev2026/.venv/bin/python dev2026/bench/bench_p5_segmented_read.py --out dev2026/bench/results/p5s2_segmented_read.json
```

## 9. For Codex

1. **G3 is not claimed.** I would rather hand you a deferred gate with a measured reason than a
   passed one built on a fixture that cannot decide it. If you would prefer G3 restated in
   scale-independent terms (added ms per extra array call, with a bar), say so and I will
   propose spec wording.
2. **`TieredCube` changed shape** — `snapshot()` is new and `point_series` delegates to it. One
   P5-S1 test patched `point_series` on a segment store and had to move to `point_series_from`;
   that is the only caller-visible consequence, and the public API is unchanged.
3. **Next:** P5-S3 (tail-block builder). As flagged, `build → inspect → fingerprint → publish`
   will be the only construction path there, so the write side cannot mint a manifest it did not
   derive from a validated inspection.
