# P3-S1 — opt-in compact bbox formats (`format=grid` / `format=columnar`)

Ships the S0 decision: **grid-aware `format=grid`** as the primary opt-in bbox format (flat `columnar`
as comparison), default **`format=json` unchanged**. No binary (S0: not justified). Server-only +
shadow validation; **VM24 backfill is running → no production/store touch** (Codex S1 #5).

## What changed (server only)
- `store/bbox_encode.py` — ONE canonical encoder (`encode_grid` / `encode_columnar`) shared by the
  API, benches, and tests (no prototype drift). Encodes straight from the 2-D `cols` (skips the row
  path's per-point dict — the S0 server hot spot). `field_status` absent/NaN semantics; `truncate`
  mirrors the row path (lon/lat 5 dp, values 3 dp).
- `api/app.py` bbox branch — new `format` query (`json` default | `grid` | `columnar`):
  - `json` → the existing row-array **StreamingResponse, byte-path UNCHANGED**.
  - `grid`/`columnar` → buffered single `Response` (compact). Headers: `X-Bbox-Format`, `X-Read-Ms`,
    `X-Encode-Ms` (+ existing `X-Served-Rows`/`X-Stride`/cache).
  - `format` outside bbox mode → 400; unknown `format` → 400.
- Default JSON keeps **schema/media-type/semantic** compatibility, **not byte-for-byte** (spec §6).

## Real-store / shadow HTTP gate (Codex S1 #1) — `bench/bench_bbox_http.py`
Real HTTP round-trip on a shadow uvicorn (synthetic 2048² store by default; `--store` for a real local
store, read-only). Reports server (headers) + transfer (curl `--compressed`) + client (Node
`--expose-gc`). **~750k points, 3 requested vars (sst, sea_ice present; sst_anomaly absent):**

| fmt | server read/encode ms | transfer MB | Content-Encoding | client fetch/text/parse/transform ms | heap MB (gc) |
|---|---|---|---|---|---|
| json | 12.5 / – | **106.4** | (none) | **944.5** / 10.7 / 189.2 / 4.4 | **130.8** (gc) |
| **grid** | 7.6 / 14.1 | **10.2** | (none) | **39.0** / 1.1 / 41.4 / 4.3 | **34.9** (gc) |
| columnar | 7.6 / 29.6 | 24.8 | (none) | 60.3 / 3.9 / 67.6 / 3.3 | 46.8 (gc) |

What the real HTTP gate adds over S0 (file-mode/Node-only):
- **Transfer is now visible and is the wall**: row `fetch` 944 ms / 106 MB vs grid 39 ms / 10 MB
  (**~24× faster fetch, ~10× smaller transfer**). S0's file mode hid this.
- **Server `X-Read-Ms` confirms read ≈ 8–12 ms** over real HTTP too (H1 holds end-to-end; no bbox store).
- **`X-Encode-Ms`**: grid 14 ms encodes straight from arrays; the row path's ~1 s dict build is gone.
- **Compression (Codex S1 #3):** `Content-Encoding (none)` because a bare local uvicorn doesn't
  compress — correctly **not** counted as transfer savings. brotli/gzip is owned by NGINX; the real
  `Content-Encoding`/compressed-transfer check is a **VM24 step, DEFERRED while backfill runs**. The
  Node harness reports `decoded_bytes` (decompressed), never equating it with transfer.
- **Heap (Codex S1 #2):** measured with `node --expose-gc` + explicit GC, per-run; grid +34.9 MB vs
  row +130.8 MB. Chrome shadow/manual (`bbox_bench.html`) remains the browser-freeze gate.

> S0 was synthetic + warm + Node lower-bound (Codex S1 #1) — this S1 gate is the real-HTTP complement.
> Both still understate the Chrome freeze; the manual Chrome run on a shadow app is the binding browser
> gate (pending; production cap stays 300k; backfill must finish first).

## Parity & correctness (Codex S1 #4: semantic float32, not byte)
`tests/test_phase2_p3s1.py` (6/6, over the real API via TestClient):
- **grid/columnar ↔ json parity** — reconstruct rows and assert **float32-equal** lon/lat/values
  (orjson shortest-round-trip vs `float(np.float32)` differ in text, same float32).
- **absent var** → `field_status='absent'`, `fields.<var>=null` (not a giant null array).
- **default (no `format`)** → `X-Bbox-Format: json`, row array (unchanged).
- `format` outside bbox → 400; unknown `format` → 400; **`truncate` parity** across formats.
- `tests/test_phase2_p3s0.py` still green (now exercises the canonical encoders via thin wrappers).

## Cache note (spec §7, deferred to its own step)
S1 keeps the existing cache logic for all formats (fixed past date → cacheable). Compact formats are
incidentally far more cache-friendly (10 MB vs 106 MB), which softens the large-object NGINX-cache risk;
the explicit large-bbox cache policy (no-store large bbox / long-cache compact only) is still a
separate gate (§7 / S5), set with ops.

## Artifacts
`bench/results/p3s1_bbox_http_<UTCDATE>.json` (server+transfer+client per format). Harness:
`bench/bench_bbox_http.py` + updated `bench/client/bbox_client_bench.mjs` (`--expose-gc`,
`decoded_bytes` labeling). Next: **P3-S3** writes the front-end contract (format/sample/tiling
guidance); binary (S2) only if the manual Chrome run shows grid still struggles.
