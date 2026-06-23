# P2-S4 — time-cube real-data variable absence — result: HANDLED (parity preserved)

> The [Medium] gate item from P2-S3: the dense cube must preserve P1's per-day semantics on
> real data where some days lack `sst_anomaly`. Done via a per-(day,var) **validity mask**.
> Tools: `ingest/build_timecube.py`, `store/time_cube.py`, `tests/test_phase2_s4.py` (5/5).

## The problem
A cube cell is dense float32, so a NaN is ambiguous:
- **land** (var present that day, value NaN) → P1 returns `field: null` (key present)
- **absent** (var missing that day) → P1 **omits** the key
Without extra metadata the reader cannot tell these apart.

## The fix — validity mask
- Converter now takes the **union of vars across ALL days** (not just day 0) and records, from a
  cheap filesystem check, a per-(day,var) **validity flag**; absent (day,var) cells are left as
  `fill_value=NaN`. Stored in `attrs["var_valid"] = {var: [bool,...]}`.
- `append_day` resizes every cube var and updates the mask (a day missing a var → NaN + valid=False).
- `TimeCubeStore.point_series`: for each (day, field) — field not in cube → omit; `var_valid[field]
  [day] == False` → **omit** (absent); else value (NaN → **null**). Exactly reproduces P1.

## Tests (5/5)
- **mixed-presence ocean point**: cube == P1 over 7 days where `sst_anomaly` is present on 0/2/4/6,
  absent on 1/3/5 → absent days omit the key, present days carry it.
- **land point absent-vs-null**: at a NaN land cell, present days → `sst_anomaly: null` (key present),
  absent days → omitted. One parity assertion (P1 as oracle) covers both distinctions.
- **append**: appending a day without `sst_anomaly` → reader omits it; a later day with it → present.
- **validity mask built** correctly.
- **REAL-DATA regional parity**: a 3°×3° cube built from the actual MUR store over 20 days
  (`region=` + `days=` subset) == P1 exactly for 3 points → the cube works on real values/chunks,
  not just synthetic. (Local real days are all-present, so mixed-absence is covered synthetically.)

## Notes / carried forward
- Converter now supports `region=(i0,i1,j0,j1)` + `days=` (needed to build a REGIONAL cube from the
  global store — reading whole global fields is infeasible). **Global/tiled conversion at scale is
  P2-S6 ingest work**, not done here.
- **Append-at-scale** (P2-S3's 30 ms was 64×64 synthetic) — P2-S6 must benchmark `time_chunk=90` +
  sharding resize/write on a larger grid.
- `s8/t90/sharded` remains a **starting** recommendation, not final production chunking.
- **Still no promotion claim** — synthetic + 20-day real region; the binding gate needs real ≥365
  contiguous + cold on VM24.

## Next
P2-S5 hybrid router (point/range→cube, bbox/single-day+POST/points→daily P1) → P2-S6 dual-write
ingest (append-at-scale bench) → P2-S7 full local gate → P2-S8 VM24 binding.
