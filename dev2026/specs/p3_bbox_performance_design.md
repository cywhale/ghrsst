# P3 design — single-day bbox payload / wire-format performance (dev2026)

Status: **DRAFT for Codex review** (Claude authored the plan; Codex reviews; Claude then implements).
Builds on [`bbox_performance_notes.md`](bbox_performance_notes.md) and the v0.3.0 VM24 deployment.

## 0. Problem restated (from VM24 v0.3.0 evidence)
v0.3.0 fixed multi-day point/range (time-cube). **Bbox is now the bottleneck.** Public example:
- `lon0=135&lat0=15&lon1=150&lat1=20&append=sst,sst_anomaly,sea_ice` → **752,001 points**, **~108 MB**
  row-oriented JSON. VM24 produces/streams in ~4 s, but the **browser freezes** parsing/rendering it.
- Shrunk to `lon1=140` → 251,001 points, ~36 MB → ~1.6 s, OK.
- Production guard (temporary): `GHRSST_BBOX_POINT_LIMIT=300000` to stop the front-end being blown up.

Current path (`api/app.py` read_ghrsst bbox branch → `store/store_access.py`):
`bbox_arrays()` (zarr slice → `lons,lats,cols` 2-D float32) → `bbox_rows_window()` (one **dict per
point**: `{lon,lat,date,sst,sst_anomaly,sea_ice}`, keys repeated 752k×) → `orjson.dumps` per
`BBOX_STREAM_BATCH=50_000` → `StreamingResponse` JSON-array. **Already streamed**; `sample`(stride)
already exists.

**Codex hypothesis:** the critical path is NOT the Zarr read (752k×3 floats ≈ 9 MB raw is small). It
is **row-oriented JSON key bloat + browser JSON.parse/render of a huge object graph**, plus transfer.
This spec treats that as a hypothesis to **measure before choosing a fix** (project discipline: a
candidate is rejected/accepted by benchmark, never by intuition — cf. P2 E1/F).

## 1. Goal / non-goals
**Goal:** make a single-day bbox up to ~1M points usable in the browser without freezing, by
attacking whichever segment the **measured** critical path identifies — primarily payload size and
browser parse/render — via **opt-in** compact response formats and a front-end large-bbox contract.
**Non-goals:**
- **No breaking change to the default wire format.** `format=json` (today's row-array) stays
  byte-compatible (the bbox analog of the project's "no silent contract break" rule). New formats are
  **opt-in** query params only.
- Not multi-day bbox (single-day semantics fixed in v0.3.0 stay).
- **No new bbox-optimized STORE** unless S0/S1 prove server read is dominant AFTER the format fix
  (deferred Tier; current evidence says read is not dominant).
- Not a forced device/browser matrix; one representative headless measure + a documented manual Chrome
  measure on the public URL is the gate (can be tightened by Codex).

## 2. Critical path as TESTABLE hypotheses (estimate AND measure) — **P3-S0 is the gate**
Split end-to-end into 5 measured segments for the SAME bbox, at **250k / 750k / 1M points × 3 vars**:

| seg | what | tool |
|---|---|---|
| `T_read` | `bbox_arrays` zarr slice → numpy | server bench |
| `T_rows` | `bbox_rows_window` dict construction | server bench |
| `T_encode` | `orjson.dumps` (row) / columnar encode | server bench |
| `bytes` | payload size on the wire (+ gzip size) | server bench |
| `T_transfer` | `bytes` / bandwidth (model + real VM24 curl) | curl `-w` |
| `T_parse` | browser `JSON.parse` + minimal render | headless (node/puppeteer) + **manual Chrome devtools note** |

**Hypotheses (to confirm/refute):**
- H1: `T_read` ≪ total (≈ tens–hundreds ms). → store redesign NOT the lever.
- H2: `bytes` and `T_parse` dominate; `T_parse` grows super-linearly near the freeze point (object
  graph / GC). → format + object-count reduction is the lever.
- H3: **JSON-array streaming does NOT help the browser** — a JSON array can't be incrementally parsed,
  so the browser buffers all 108 MB before `JSON.parse`. (If true, NDJSON/columnar-chunked or binary
  is needed for incremental client handling; streaming only helped server RSS.) **Measure explicitly.**
- H4: `T_rows`+`T_encode` are a non-trivial server slice (per-row Python dict × 752k). → columnar
  removes the per-row dict entirely.

**S0 decision gates (drive S1+):**
- `bytes`/`T_parse` dominate (expected) → **columnar first** (S1), then binary (S2) if still too big.
- `T_parse` is object-count bound (not byte bound) → columnar (few big arrays) is the structural fix.
- `T_read` dominates → escalate to the deferred bbox-store Tier (S4) — only then.
- Streaming gives no client benefit (H3) → front-end must cap object size (sample/tile, S3) regardless.

## 3. Candidate solutions — risk-ordered, each with a decision gate
### Tier 0 — no contract change (measure cheap wins first)
- **gzip/deflate** at NGINX (or app) for the bbox response — JSON compresses ~8–15×. Gate: measured
  `bytes` and `T_transfer` reduction; confirm it does NOT change `T_parse` (decompressed graph is the
  same). Likely helps transfer, not the browser freeze.
- **`BBOX_STREAM_BATCH` tuning** + confirm/expose whether streaming helps the client (H3).
These are non-breaking and bound the baseline; they are not expected to be sufficient alone.

### Tier 1 — opt-in `format=columnar` (additive; default unchanged) — **primary candidate**
Return arrays-by-column instead of array-of-objects:
```json
{ "date":"2026-01-01", "lon":[...], "lat":[...], "sst":[...], "sst_anomaly":[...], "sea_ice":[...] }
```
Removes 752k repeated key sets and 752k JS objects → far smaller `bytes` and a tiny object graph
(6 arrays) the browser parses fast. **Server-side** it also skips `bbox_rows_window`'s per-point dict
(encode columns directly from the 2-D `cols`). Gate: vs row JSON, measured **≥X× smaller bytes** and
**≥Y× faster `T_parse`** at 750k (X/Y set from S0). Parity test: columnar ↔ row carry identical data
(same lon/lat ordering, same null-for-NaN/absent semantics).
> Open: grid-aware variant (return `lon[]`,`lat[]` axes + 2-D `sst[nlat][nlon]`) is even smaller (no
> lon/lat repetition) — evaluate in S1 against the flat columnar.

### Tier 2 — opt-in binary (additive) — only if columnar still too big/slow
Evaluate ONE of: **Arrow IPC** (arrow-js, zero-copy typed arrays), **NetCDF** (familiar to the science
client), **Parquet**, or raw little-endian typed-array blob + tiny JSON header. Gate: vs columnar
JSON, measured bytes + browser decode time + client-integration cost; pick exactly one. Binary is a
larger client contract change, so it ships behind `format=` and only if S1 is insufficient.

### Tier 3 — front-end large-bbox contract (object-graph cap; defense regardless of format)
The API already has `sample`(stride). Spec the front-end rule: when estimated points
(`(nlon*nlat)` after stride) exceed a threshold, **auto-increase stride** or **tile** the bbox into
sub-requests, OR request a compact `format`. Server keeps `GHRSST_BBOX_POINT_LIMIT` as defense and
returns a **clear 400 with a suggested stride/format** (today's message already hints stride).
> The public front-end lives on `eco.odb.ntu.edu.tw` (NOT in this repo). **Open question:** is the
> front-end in-scope for Claude, or is this spec-only (recommended contract for the front-end owner)?

### Deferred Tier — bbox-optimized store
Only if S0 (and re-measure after S1) shows `T_read` dominant. Current evidence says no. Documented to
avoid premature spatial-store work.

## 4. LOCAL gate + thresholds
- **Fixture:** a single real day (or a synthetic global-scale day) sliced to **250k / 750k / 1M**
  points × 3 vars; reuse `store/zarr_paths.py` + `StoreAccess.bbox_arrays`.
- **Browser parse:** headless node `JSON.parse` (+ arrow-js / typed-array decode for S2) as the CI
  proxy; **plus** a documented manual Chrome devtools Performance measure on the VM24 public URL for
  the 752k case (the real freeze). Both recorded in `specs/p3s0_bbox_breakdown.md`.
- **Thresholds (refined after S0):** 750k-point bbox must be **browser-usable (no freeze, parse+render
  within an interactive budget, e.g. < ~1 s parse)**; payload `bytes` reduced enough that transfer is
  no longer a wall; default `format=json` path unchanged and parity-exact.

## 5. Per-step plan — every step ships code + benchmark + gate
- **P3-S0** — instrumentation + critical-path benchmark (`bench/bench_bbox_wire.py`): the 5-segment
  breakdown table at 250k/750k/1M; confirm/refute H1–H4; **no format change yet**. Output
  `specs/p3s0_bbox_breakdown.md`. **This step decides S1/S2 scope.** (Disproof gate, like P2-S1/S2.)
- **P3-S1** — `format=columnar` opt-in: server columnar encoder (from `cols` directly, bypass per-row
  dicts), `format=json` default untouched; parity tests (columnar↔row identical data + null/absent
  semantics + cache headers + `sample`); bytes/parse benchmark + gate.
- **P3-S2** — *(conditional on S0/S1)* binary format eval + one opt-in `format=arrow|netcdf|...`;
  benchmark vs columnar; client-decode note.
- **P3-S3** — front-end large-bbox contract: auto-sample/tile rule + server hint on cap; (front-end
  change vs spec-only per the open question).
- **P3-S4** — *(conditional)* bbox-store only if read proves dominant after S1.
- **P3-S5** — VM24 binding + rollout: re-measure on VM24; raise/relax `GHRSST_BBOX_POINT_LIMIT` once a
  compact format makes large bbox safe; document default-vs-opt-in in the public API notes.

## 6. Backward-compatibility contract (non-negotiable)
- `format` defaults to `json` = **today's row-array, byte-for-byte**. All new formats are opt-in.
- `sample`, `mode`/`truncate`, single-day semantics, cache headers (`X-Served-Rows`, `X-Stride`,
  Cache-Control), 400 messages — all preserved. New: `X-Bbox-Format` echo header.
- Streaming behavior of the default path unchanged; new formats may stream differently (e.g. columnar
  may buffer columns) — documented and gated on server RSS (reuse P1-S4 BBOX RSS gate).

## 7. Open questions (for orchestrator / Codex)
1. **Front-end scope:** does Claude implement the `eco.odb` front-end auto-sample/tile (S3), or is P3
   server-side + a written front-end contract only? (No front-end in this repo.)
2. **Binary format choice** if S2 triggers: Arrow IPC vs NetCDF vs typed-array blob — which does the
   science client prefer / already consume elsewhere?
3. **Browser-parse measurement fidelity:** is headless node `JSON.parse` + a manual Chrome note an
   acceptable gate, or is a device/browser matrix required?
4. **Target max bbox:** what point count should the lifted `GHRSST_BBOX_POINT_LIMIT` allow once a
   compact format ships (e.g. 1M? unbounded with mandatory `format=columnar`)?
5. **gzip ownership:** is response compression in P3 (app/NGINX) scope, or an ops-side NGINX change?
6. **Columnar shape:** flat columns (`lon[]`,`lat[]` per point) vs grid-aware (`lon[]`/`lat[]` axes +
   2-D field) — pick in S1 by benchmark, or fix now?
