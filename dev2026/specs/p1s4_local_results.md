# P1-S4 — load harness + LOCAL results (NON-BINDING)

> ⚠️ **These numbers are NOT a release gate.** They are **harness validation +
> relative/mechanics evidence** on a developer Mac against a **partial, still-copying**
> local store. The **authoritative G1′ / G6 / G7 gate is `dev2026/bench/loadtest.py`
> re-run on VM24** against the full store with the spec parameters
> (SB C=32 / T=120 s, LR C=4/8/16, BBOX near `POINT_LIMIT`). See doc 01 P1-S4/P1-S5.

## Harness: `dev2026/bench/loadtest.py`
- configurable `--base` (or `GHRSST_LOADTEST_BASE`); default `http://127.0.0.1:8036` (local, non-prod). No hardcoded production host.
- scenarios **SB / LR / BBOX / OL** (spec §P1-S4).
- per (scenario, C): aggregate p50/p95/p99 latency, `ok` / `shed_503` / `errors` / `timeouts`, throughput, **executor queue depth** + **worker RSS** time series from `/healthz`, OL **recovery_ms**.
- **windowed metrics** (`--window-s`, default 10 s; `--warmup-s`, default 10 s): per-bucket p50/p95/p99 + counts + rps in `windowed.windows`, plus a `windowed.stability` summary (first-vs-last **full** bucket: `tail_drift_ok` = last p95 ≤ 1.2× first, `rps_within_10pct`) — this is the **computable source for the G2′ stability gate** (partial trailing buckets are flagged and excluded).
- **`--sb-range-max`**: SB point-request day span — `1` = short single-day (G2 latency SLO), `>1` = range-heavy (G2′ stability).
- machine-readable **JSON** output (`--out`). **`--run-kind local|vm24`** sets `meta.binding` (local→false, vm24→true; `--binding` forces true) so the same tool produces both the local validation artifact and the VM24 authoritative gate artifact.
- `/healthz` returns `executor_queue_depth`, `executor_limit`, `rss_mb` (psutil; `/proc` fallback), `pid`. The sampler **preserves `pid` + `executor_limit`** per sample and reports **`by_pid`** (per-worker `rss_mb_max` / `queue_depth_max`) — because under gunicorn `-w N` each `/healthz` is one worker's view. `rss_mb_max` is the max across sampled workers, **not a total**; total RSS must be collected externally (e.g. `ps` over SSH, sum by worker pid).

## Local environment (context only)
- 28-core Mac, Python 3.13, uvicorn single worker. Store = `bak/data/mur.zarr` (partial, ~260 contiguous recent days + sparse older; still copying).
- Default app config: `GHRSST_ZARR_WORKERS=8` → executor limit `8+32=40`.

## Run 1 — normal config, all scenarios (duration 8 s each)
| scen | C | req | ok | 503 | err | p50 ms | p95 ms | p99 ms | RSS max MB | qDepth max | recov ms |
|---|---|---|---|---|---|---|---|---|---|---|---|
| SB | 32 | 316 | 302 | 0 | 14 | 827 | 1251 | 1775 | 295 | 32 | — |
| LR | 4 | 20 | 20 | 0 | 0 | 1905 | 2624 | 2672 | 295 | 4 | — |
| LR | 8 | 21 | 21 | 0 | 0 | 3026 | 5307 | 5936 | 294 | 8 | — |
| LR | 16 | 22 | 22 | 0 | 0 | 5579 | 10663 | 10723 | 301 | 11 | — |
| BBOX | 4 | 46 | 46 | 0 | 0 | 737 | 787 | 867 | 364 | 4 | — |
| OL | 64 | 73 | 73 | 0 | 0 | 18629 | 32347 | 33457 | 370 | 9 | 19 |

Notes:
- **SB 14 errors** = short ranges landing entirely on internal gap days (e.g. a missing day) → correct `400`; expected on a sparse local store, should be ~0 on a contiguous VM24 store.
- **LR degradation with concurrency is the headline G1′ signal**: p99 2.7 s → 5.9 s → 10.7 s at C=4/8/16 reading ~260-day series. On VM24/full store this is the metric that decides whether P1 software suffices or Phase-2 time-cube is triggered (doc 01 §6).
- **OL @ normal config sheds 0**: with `OVERLOAD_WAIT_MS=2000` and 40 slots turning over, waiters get a slot within budget → the gate **queues** rather than rejects (p99 grows). This is correct; to *exercise* the reject path see Run 2.

## Run 2 — tight shed config, demonstrate the 503 path
Config: `GHRSST_ZARR_WORKERS=2 GHRSST_OVERLOAD_QUEUE_MAX=4 GHRSST_OVERLOAD_WAIT_MS=150` → limit 6.
| scen | C | req | ok | 503 | err | p99 ms | RSS max MB | recov ms |
|---|---|---|---|---|---|---|---|---|
| OL | 32 | 105 | 12 | **93** | 0 | 13908 | 154 | **16.9** |

- **G7 confirmed**: under saturation the gate sheds with `503 + Retry-After` (93/105), RSS stays bounded (154 MB), and a single probe **recovers in 16.9 ms** after load stops. No timeouts, no OOM.

## Mechanics validated locally (all non-binding)
- **G1′** concurrent long-range latency measured (LR C=4/8/16) ✓
- **G6** worker RSS sampled per scenario ✓ (single-worker; VM24 gunicorn `-w N` → aggregate per-worker RSS / sum vs `GHRSST_RSS_CEILING_MB`)
- **G7** 503 shed + bounded RSS + fast recovery demonstrated (Run 2) ✓
- executor queue-depth backlog captured ✓
- machine-readable JSON for VM24 diffing ✓

## Release gate (TODO on VM24)
Run the same tool on VM24 against the full store with spec params; bind G1′/G6/G7
pass/fail there. Set `GHRSST_RSS_CEILING_MB` per VM24 RAM ÷ `-w` first (doc 01 P1-S5).
