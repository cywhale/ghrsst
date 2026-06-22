# P2-S1 — Candidate E1 (manifest sidecar) — result: REJECT (for the gate)

> Tier-1 no-rewrite candidate (spec §4). Goal: cheaply prove whether a manifest/index
> sidecar over the daily-group store reduces the O(days) critical path. **It does not.**
> Tools: `dev2026/store/e1_manifest.py`, `bench_timecube_microcost.py --engine e1`,
> `dev2026/tests/test_phase2_s1.py` (4/4).

## Parity (hard requirement) — PASS
E1 returns **exactly** the same `point_series` as P1 `StoreAccess` (it changes only the
per-day read path: direct `zarr.open_array` via a prebuilt manifest + handle LRU, skipping the
per-request rescan / day_present / group-open). Tests: manifest coverage, value parity at 4
points, **absent-field omit parity** (a day lacking `sst_anomaly` omits the key), new-day
visibility via rescan.

## Micro-cost — E1 vs P1 baseline (real store, 96-day contiguous span, warm)
| engine | chunk_count | decompressed | read_amp | latency p50 | p95 |
|---|---|---|---|---|---|
| baseline (P1) | 288 | 1207.96 MB | 1.05M× | 1209.7 ms | 1226.9 ms |
| **E1 manifest** | **288** | **1207.96 MB** | **1.05M×** | **1172.7 ms** | 1226.4 ms |

## Verdict: REJECT (proceed to P2-S2 / F)
- **Cache-independent metrics are IDENTICAL** (`chunk_count` 288 = 96×3 = O(days);
  `decompressed_bytes` 1.2 GB unchanged) — exactly as H4 predicted: a manifest cannot change
  *what* must be read/decompressed, only per-day open overhead.
- Latency improved only **~3 %** (1210→1173 ms on 96 days) — negligible; the per-day cost is
  dominated by the 4 MB chunk **decompress**, not the group open (which P1 already made cheap).
  Extrapolated to 365 days on VM24 cold cache, E1 would still be ~14 s (decompress-bound).
- **Not promotion-eligible regardless** (local largest contiguous real span = 96 days < 365 →
  shadow-only; spec §5.1/§7). So E1 is rejected for the gate and not carried further.

**Conclusion**: manifest-only is disproved cheaply (the intended outcome). The O(days) fan-out
must be attacked by reducing reads — next: **P2-S2 Candidate F** (parallelize O(days), bounded,
coordinated with the API executor) and, if F fails the gate, **Tier-2 time-cube** (cut
`chunk_count` to O(days/time_chunk) + smaller per-point decompressed bytes).
