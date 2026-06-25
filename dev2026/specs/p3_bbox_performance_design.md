# P3 design — single-day bbox payload / wire-format performance (dev2026)

Status: **DRAFT — revised per Codex review round 1** (Claude authored; Codex reviewed; Claude folded
the 7 review points below and will implement starting P3-S0). Builds on
[`bbox_performance_notes.md`](bbox_performance_notes.md) and the v0.3.0 VM24 deployment.

> Codex round-1 verdict: direction correct, proceed to S0 after spec hygiene fixes. Folded:
> (1) compression = **verify existing NGINX brotli/gzip**, not add; (2) default JSON = **semantic**
> compatibility, not byte-for-byte; (3) S0 **bypasses the 300k prod cap** via direct bench / shadow;
> (4) **grid-aware columnar is the primary** S1 implementation, flat is comparison; (5) **absent-var
> `field_status` compact semantics**; (6) browser gate **splits fetch/text/parse/render**; (7)
> **large-bbox NGINX cache policy** is a risk + gate. Codex judgement: the likely fix is response
> format + front-end contract, not a new bbox Zarr store.

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
- **No breaking change to the default wire format.** `format=json` (today's row-array) keeps
  **schema / media-type / semantic compatibility** — same row shape, field names, ordering, and
  null-for-NaN/absent meaning. It is **NOT** promised byte-for-byte: streaming whitespace, chunk
  boundaries, and float formatting are implementation detail, not contract (Codex review #2). New
  formats are **opt-in** query params only.
- Not multi-day bbox (single-day semantics fixed in v0.3.0 stay).
- **No new bbox-optimized STORE** unless S0/S1 prove server read is dominant AFTER the format fix
  (deferred Tier; current evidence says read is not dominant).
- Not a forced device/browser matrix; one representative headless measure + a documented manual Chrome
  measure on the public URL is the gate (can be tightened by Codex).

## 2. Critical path as TESTABLE hypotheses (estimate AND measure) — **P3-S0 is the gate**
Split end-to-end into measured segments for the SAME bbox, at **250k / 750k / 1M points × 3 vars**.
**Cap bypass (Codex review #3):** S0 must NOT go through the production default path — prod
`GHRSST_BBOX_POINT_LIMIT=300000` blocks 750k/1M. Server segments call `store.bbox_arrays` + the
encoder **directly** (no HTTP cap); HTTP/transfer/browser segments run on a **staging/shadow** app
with a raised `GHRSST_BBOX_POINT_LIMIT`. **Production cap stays 300k until P3 ships a compact format.**

Server side:
| seg | what | tool |
|---|---|---|
| `T_read` | `bbox_arrays` zarr slice → numpy | server bench (direct) |
| `T_rows` | `bbox_rows_window` dict construction (row path) | server bench |
| `T_encode` | `orjson.dumps` row / columnar encode | server bench |
| `bytes` | **uncompressed** payload size | server bench |
| `bytes_enc` | **compressed** size + `Content-Encoding` | curl (below) |
| `T_transfer` | `bytes_enc` / bandwidth (model + real VM24 curl) | curl `-w` |

**Compression (Codex review #1):** VM24 NGINX already serves **brotli (fallback gzip)** by default —
S0 verifies it is actually applied, it does NOT add new compression. curl MUST send
`--compressed` (or explicit `Accept-Encoding: br,gzip`) and record `Content-Encoding` + compressed vs
uncompressed bytes, else compression effectiveness is mis-judged.

Browser side — **split parse from render (Codex review #6)**, headless node as lower bound + a
documented manual Chrome run (record the exact page/flow):
| seg | what |
|---|---|
| `T_fetch` | raw fetch to first/last byte (network) |
| `T_text` | `response.text()` (buffer the body) |
| `T_parse` | `JSON.parse` (row) / `JSON.parse`+typed-array decode (compact/binary) |
| `T_render` | minimal transform → **map-layer input construction** / front-end state build |

**Hypotheses (to confirm/refute):**
- H1: `T_read` ≪ total (≈ tens–hundreds ms). → store redesign NOT the lever.
- H2: `bytes`/`T_parse`/`T_render` dominate; they grow super-linearly near the freeze point (object
  graph / GC). → format + object-count reduction is the lever.
- H3: **JSON-array streaming does NOT help the browser** — a JSON array can't be incrementally parsed,
  so the client buffers all 108 MB before `JSON.parse`. (If true, NDJSON/columnar-chunked or binary is
  needed for incremental handling; streaming only helped server RSS.) **Measure explicitly.**
- H4: `T_rows`+`T_encode` are a non-trivial server slice (per-row Python dict × 752k). → columnar
  removes the per-row dict entirely.
- H5: the freeze is **`T_render`** (map-layer / state build over 752k objects), not just `T_parse`. →
  object-count reduction (grid-aware) helps even if compressed transfer is fine.

**S0 decision gates (drive S1+):**
- `bytes`/`T_parse`/`T_render` dominate (expected) → **grid-aware columnar first** (S1), binary (S2)
  if still too big.
- parse/render is object-count bound (not byte bound) → grid-aware (few big arrays) is the structural fix.
- `T_read` dominates → escalate to the deferred bbox-store Tier (S4) — only then.
- streaming gives no client benefit (H3) → front-end must cap object size (sample/tile, S3) regardless.

## 3. Candidate solutions — risk-ordered, each with a decision gate
### Tier 0 — no contract change (measure / verify; do NOT add compression)
- **Verify existing NGINX brotli/gzip** (Codex review #1) — VM24 already serves brotli (fallback
  gzip) by default; P3 does **not** add app/NGINX compression. S0 confirms it is applied to the bbox
  response (`Content-Encoding`, compressed vs uncompressed bytes via `curl --compressed`) and quantifies
  the `T_transfer` reduction. Compression shrinks transfer but does **not** change `T_parse`/`T_render`
  (the decompressed object graph is identical) — so it cannot fix the browser freeze on its own.
- **`BBOX_STREAM_BATCH` tuning** + confirm whether streaming helps the client at all (H3).
These bound the baseline; they are not expected to be sufficient alone.

### Tier 1 — opt-in compact columnar (additive; default unchanged) — **primary candidate**
A bbox is a **rectilinear grid**, so the natural compact form is **grid-aware** — axes once, fields as
2-D arrays. **Grid-aware is the PREFERRED first implementation (Codex review #4); flat columnar is the
comparison baseline in S1.**

**Grid-aware (`format=grid`, preferred):**
```json
{
  "date": "2026-01-01",
  "lon": [...],                       // nlon axis (once, not per point)
  "lat": [...],                       // nlat axis
  "shape": [nlat, nlon],
  "fields": { "sst": [[...],...], "sea_ice": [[...],...] },
  "field_status": { "sst_anomaly": "absent" }
}
```
vs flat columnar (`format=columnar`, comparison): `{date, lon[], lat[], sst[], ...}` — still repeats
lon/lat per point. Grid-aware removes BOTH the 752k repeated key sets AND the per-point lon/lat
repetition → smallest `bytes` and a ~handful-of-arrays object graph (fast parse + cheap map-layer
build). **Server-side** both skip `bbox_rows_window`'s per-point dict (encode directly from the 2-D
`cols`).

**Absent / NaN compact semantics (Codex review #5)** — a new opt-in format need not copy row JSON's
redundant shape, but must be **semantically equivalent**:
- **whole-day-absent var** (requested but the day lacks it): emit `fields.<var> = null` **plus**
  `field_status.<var> = "absent"` — NOT a giant all-null 2-D array.
- **land / masked NaN** within a present var: keep the array, value `null` at that cell (JSON has no
  NaN). `field_status.<var> = "present"`.
- a present var that is entirely NaN over the bbox is still `"present"` with a null-filled array
  (distinct from `"absent"`), preserving the row-path absent-vs-NaN distinction.

Gate: vs row JSON, measured **≥X× smaller `bytes`** and **≥Y× faster `T_parse`+`T_render`** at 750k
(X/Y set from S0). **Parity test:** grid-aware/flat → reconstruct row records and assert identical data
to the row path (same lon/lat ordering, same absent→omit-equivalent / NaN→null semantics).

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
  points × 3 vars; reuse `store/zarr_paths.py` + `StoreAccess.bbox_arrays`. **Cap bypass:** server
  microbench calls the store/encoder directly; the HTTP/transfer/browser measure uses a staging/shadow
  app with a raised `GHRSST_BBOX_POINT_LIMIT` — **production cap stays 300k** (Codex review #3).
- **Browser (split fetch/text/parse/render, Codex review #6):** headless node `response.text()` →
  `JSON.parse` (+ arrow-js / typed-array decode for S2) as the CI lower bound; **plus** a documented
  manual Chrome devtools Performance run (record the exact page + flow: fetch → parse → state/map-layer
  build) on a shadow URL for the 752k case (the real freeze). Both recorded in
  `specs/p3s0_bbox_breakdown.md`.
- **Thresholds (refined after S0):** 750k-point bbox must be **browser-usable (no freeze;
  `T_parse`+`T_render` within an interactive budget)**; compressed transfer no longer a wall; default
  `format=json` path unchanged and **semantically** parity-exact (not byte-for-byte).

## 5. Per-step plan — every step ships code + benchmark + gate
- **P3-S0** — instrumentation + critical-path benchmark (`bench/bench_bbox_wire.py`): the
  server+browser segment breakdown (incl. compressed bytes / `Content-Encoding`) at 250k/750k/1M;
  confirm/refute H1–H5; **no format change yet**; cap bypassed per §4. Output
  `specs/p3s0_bbox_breakdown.md`. **This step decides S1/S2 scope.** (Disproof gate, like P2-S1/S2.)
- **P3-S1** — opt-in compact columnar, **grid-aware (`format=grid`) primary** + flat (`format=columnar`)
  comparison: encode directly from `cols` (bypass per-row dicts), with the absent/NaN `field_status`
  semantics (§3 Tier 1); `format=json` default untouched; parity tests (compact↔row identical data +
  absent/NaN semantics + cache headers + `sample`); bytes/parse/render benchmark + gate.
- **P3-S2** — *(conditional on S0/S1)* binary format eval + one opt-in `format=arrow|netcdf|...`;
  benchmark vs grid-aware; client-decode note.
- **P3-S3** — front-end large-bbox contract: auto-sample/tile rule + server hint on cap; (front-end
  change vs spec-only per open question §7.1).
- **P3-S4** — *(conditional)* bbox-store only if read proves dominant after S1.
- **P3-S5** — VM24 binding + rollout: re-measure on VM24 (verify brotli applied); set the large-bbox
  **cache policy** (§8); raise/relax `GHRSST_BBOX_POINT_LIMIT` only once a compact format makes large
  bbox safe; document default-vs-opt-in in the public API notes.

## 6. Backward-compatibility contract (non-negotiable)
- `format` defaults to `json` = **today's row-array**, kept **schema/media-type/semantically**
  compatible (Codex review #2) — same row shape, field names, ordering, null-for-NaN/absent meaning.
  **NOT** byte-for-byte (streaming whitespace, chunk boundaries, float formatting are not contract).
  All new formats are opt-in `format=` values.
- `sample`, `mode`/`truncate`, single-day semantics, cache headers (`X-Served-Rows`, `X-Stride`,
  Cache-Control — but see §8), 400 messages — all preserved. New: `X-Bbox-Format` echo header.
- Streaming behavior of the default path unchanged; new formats may stream differently (e.g. columnar
  may buffer columns) — documented and gated on server RSS (reuse P1-S4 BBOX RSS gate).

## 7. Large-bbox cache policy — risk + gate (Codex review #7)
A fixed-date bbox is currently `Cache-Control: public, max-age=864000`, so NGINX may cache the whole
36–108 MB (or larger at 1M) response. Big-object caching risks **disk / cache-eviction pressure** and
can pin huge entries. P3 must DECIDE and gate (in S5, set with ops):
- **(a)** large bbox (estimated points > threshold) → `no-store` / `private` (don't cache big objects); or
- **(b)** long-cache ONLY the **compact** formats (smaller, cache-friendly), `no-store` the row default
  for large areas; or
- **(c)** a separate cache zone / `proxy_cache_min_uses` / max-object-size for the bbox location.
Gate: chosen policy is explicit, measured against NGINX cache size, and does not regress small-bbox
cacheability. Default (small bbox) caching is unchanged.

## 8. Open questions (for orchestrator / Codex)
**Resolved by this review** — folded into the spec above:
- *Compression* → verify existing NGINX brotli/gzip, don't add (§3 Tier 0).
- *Default JSON contract* → semantic, not byte-for-byte (§6).
- *S0 cap* → bypass via direct bench / shadow-raised limit; prod cap stays 300k (§4).
- *Columnar shape* → **grid-aware primary**, flat as comparison (§3 Tier 1).
- *Absent/NaN* → `field_status` compact semantics (§3 Tier 1).
- *Browser measure* → split fetch/text/parse/render (§2, §4).
- *Cache* → large-bbox cache policy gate (§7).

**Still open:**
1. **Front-end scope:** does Claude implement the `eco.odb` front-end auto-sample/tile (S3), or is P3
   server-side + a written front-end contract only? (No front-end in this repo.)
2. **Binary format choice** if S2 triggers: Arrow IPC vs NetCDF vs typed-array blob — which does the
   science client prefer / already consume elsewhere?
3. **Manual-Chrome fidelity:** is one documented Chrome devtools run (page+flow recorded) enough, or is
   a device/browser matrix required?
4. **Target max bbox:** what point count should the lifted `GHRSST_BBOX_POINT_LIMIT` allow once a
   compact format ships (e.g. 1M? unbounded only with a mandatory compact `format`)?
