# VM24 P1 binding gate — result: NO-GO (triggers Phase-2)

> Binding run by Codex/ops on VM24 (full store), commit `d74b024`. This is the
> authoritative gate per `p1s5_s6_runbook.md` §P1-S5. **Decision: do NOT cut over to
> the P1 API; trigger Phase-2 (time-optimized store).** Production untouched.

## Setup (as run)
- worktree `/home/odbadmin/python/ghrsst-dev2026`, branch commit `d74b024`, isolated venv.
- data `/home/odbadmin/Data/ghrsst/mur.zarr` (full store).
- shadow app `127.0.0.1:8036` (stopped after test). Production `8035` + MCP `8765` untouched. NGINX unmodified.
- preflight real-store tests: **46/46 OK**.
- artifacts on VM24: `dev2026/results/vm24_g1_baseline.json`, `vm24_lr.json`, `vm24_total_rss.tsv`.

## Results
**G1 baseline — 365-day point series, C=1**
| metric | measured | target | verdict |
|---|---|---|---|
| p50 | **5048.5 ms** | < 2500 ms | FAIL |
| p95 | **14075.0 ms** | < 4000 ms | FAIL |
| p99 | 14901.3 ms | — | — |

**G1′ — LR concurrency** (p95): C=4 **14190 ms**, C=8 **17452 ms**, C=16 **22078 ms**; timeouts=0, 503=0.
→ FAIL (baseline `B` already fails; absolute latency far over gate).

**RSS — NOT the blocker this run**: per-worker rssMax ≤ 184 MB; external total RSS max ≈ 422.8 MB.

## Interpretation
- The per-day-group layout makes a 365-day point series **O(days) group opens + per-day chunk decompress**; on the full store under cold cache this dominates (≈14 s p95). P1's caching/offload removed the metadata re-parse but **cannot remove the O(days) fan-out** — that is a data-structure problem, exactly the Phase-2 trigger (P1 spec §6).
- **Local vs VM24 gap is the key lesson**: local P1 measured ~1.7 s for a "365-day" series, but the local store was warm and only ~260 contiguous days. VM24 full-store + cold cache → ~5 s p50 / 14 s p95. ⇒ Phase-2 needs a **local-first critical-path gate** that does not over-trust warm/partial local numbers.
- **bbox / single-day / RSS were not the failures** → Phase-2 should NOT re-architect those (hybrid routing; see `phase2_timecube_design.md`).

## Decision
No NGINX cutover. Proceed to Phase-2 design + local-gated implementation. VM24 is the
final binding gate only, after Phase-2 passes its local gate.
