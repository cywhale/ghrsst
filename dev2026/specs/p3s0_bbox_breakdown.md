# P3-S0 — bbox wire-format critical-path breakdown (results)

Disproof gate for P3 (like P2-S1/S2). **No API change** — measurement only; the shipped encoder lands
in S1. Production 300k cap bypassed via direct store/encoder calls (spec §4).

Tools (re-runnable):
- server `bench/bench_bbox_wire.py` (synthetic 2048² day, 3 vars, ~5% NaN → JSON null).
- client `bench/client/bbox_client_bench.mjs` (Node lower bound) + `bench/client/bbox_bench.html`
  (manual Chrome run).
- artifacts: `bench/results/p3s0_bbox_breakdown_<UTCDATE>.json` (server),
  `bench/results/p3s0_client_750k_<fmt>_<UTCDATE>.json` (client),
  `bench/results/payloads/bbox_750k_<fmt>.json` (payloads). Date tag: **20260625**.

## Server side — read / row-construction / encode / bytes
| size | points | T_read | T_rows (row only) | fmt | T_encode | MB (uncompressed) | gzip MB* | vs row |
|---|---|---|---|---|---|---|---|---|
| 250k | 250,000 | 7.0 ms | 366 ms | row | 28.7 ms | 38.5 | 5.25 | 1.00× |
| | | | | columnar | 14.5 ms | 9.7 | 0.27 | 0.25× |
| | | | | **grid** | 7.4 ms | **4.6** | **0.25** | **0.12×** |
| 750k | 750,000 | 8.1 ms | 1072 ms | row | 83.3 ms | 116.0 | 15.76 | 1.00× |
| | | | | columnar | 36.5 ms | 29.7 | 0.86 | 0.26× |
| | | | | **grid** | 22.2 ms | **14.5** | **0.80** | **0.13×** |
| 1M | 1,000,000 | 8.5 ms | 1466 ms | row | 117.1 ms | 154.6 | 20.86 | 1.00× |
| | | | | columnar | 52.7 ms | 39.5 | 1.14 | 0.26× |
| | | | | **grid** | 30.2 ms | **19.4** | **1.06** | **0.13×** |

\* gzip = zlib L6 **local estimate**; actual **brotli / `Content-Encoding`** is verified on VM24 with
`curl --compressed` (S5 runbook). NGINX already does br/gzip — P3 does not add compression.

## Client side — Node lower bound (750k, the freeze point), 3-run avg
| fmt | bytes | T_text | T_parse | T_transform | **heap (object graph)** | client total |
|---|---|---|---|---|---|---|
| row | 116.0 MB | 11.6 ms | 223 ms | 5.3 ms | **+141.8 MB** | 240 ms |
| columnar | 29.7 MB | 3.0 ms | 91 ms | 3.3 ms | +64.2 MB | 97 ms |
| **grid** | **14.5 MB** | 1.4 ms | **52.8 ms** | 2.8 ms | **+11.8 MB** | **57 ms** |

> **Manual Chrome run (pending, documented step):** `bench/client/bbox_bench.html` against a shadow app
> (raised `GHRSST_BBOX_POINT_LIMIT`; prod stays 300k). Node's V8 `JSON.parse` is heavily optimized and
> **understates the real Chrome freeze**, which adds framework reactivity, state, and **map-layer build
> over the object graph** — so the **heap / object-count delta is the better freeze predictor**. Record
> fetch/text/parse/transform/render there to confirm H5.

## Hypotheses — verdict
- **H1 (read ≪ total): CONFIRMED.** `T_read` 7–8.5 ms regardless of size → a bbox store is NOT the
  lever; the deferred store Tier stays deferred.
- **H2 (bytes + client dominate): CONFIRMED.** grid is **~8× smaller uncompressed** (0.12–0.13×),
  **~4× faster parse**, **~12× less heap** than row at 750k.
- **H3 (JSON-array streaming doesn't help the client): CONFIRMED by construction.** `JSON.parse`
  needs the COMPLETE body (the harness must buffer all 116 MB before parsing); streaming only helped
  server RSS. grid's far smaller body is the real mitigation. (NDJSON/binary would add incrementality
  — evaluate only if needed in S2.)
- **H4 (row construction is a big server slice): CONFIRMED.** `T_rows` = 1072 ms @750k / 1466 ms @1M
  of pure per-point dict building — grid skips it entirely (encode straight from the 2-D `cols`).
- **H5 (freeze is render/object-graph, not just parse): SUPPORTED.** heap +141.8 MB (750k JS objects)
  vs +11.8 MB (grid) is the object-graph cost that drives Chrome GC/render; confirm magnitude in the
  manual Chrome run.

## Decision → S1
**Grid-aware (`format=grid`) is the clear S1 primary** — it wins on every axis (server CPU −1072 ms,
bytes −8×, gzip −20×, parse −4×, heap −12×). Flat `columnar` is a real but smaller win (kept as the
S1 comparison). Binary (S2) is **not** justified yet: grid already takes 750k from a 240 ms / 142 MB
client cost to 57 ms / 12 MB; revisit binary only if the manual Chrome run shows grid still freezes.
No bbox store (H1). Next: **P3-S1** wires the `format=grid` (+ `columnar`) encoder into the API with
the absent/NaN `field_status` semantics, default `format=json` untouched, parity + bytes/parse gates.

### Finding for S1 parity (float rendering)
The compact encoders use `orjson` numpy serialization → **shortest round-trip** float strings
(e.g. `5.001`), while the row path emits `float(np.float32)` (e.g. `5.000999927520752`). These are the
**same float32 value**, different decimal rendering. S1 parity must therefore assert **semantic**
equality (compare as float32 / tolerance), consistent with spec §6 (default JSON unchanged; new
formats semantically equal, not byte-identical). Locked by `tests/test_phase2_p3s0.py` (3 reconstruct
checks incl. absent-var `field_status`).

