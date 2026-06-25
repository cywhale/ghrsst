# dev2026 — GHRSST API 重構工作區

所有 2026 重構開發隔離於此資料夾,**不影響 production 檔案**(`ghrsst_app.py`、`dev/`、`conf/`)。

## 給 reviewer 的閱讀順序
1. [`specs/00_diagnosis_and_evidence.md`](specs/00_diagnosis_and_evidence.md) — 專案現況、問題、**實測證據**、根因。
2. [`specs/01_refactor_spec.md`](specs/01_refactor_spec.md) — 目標、非目標、分階段 steps、驗收標準、開放問題。

## 環境與相依(P1-S1..S4)
```bash
uv venv dev2026/.venv --python 3.13
# diagnostics/store + API(P1-S2)+ load harness(P1-S4):
uv pip install --python dev2026/.venv/bin/python \
  "zarr>=3" "xarray>=2025.1" numpy orjson \
  fastapi "uvicorn[standard]" httpx psutil
export GHRSST_ZARR_PATH=$(pwd)/bak/data/mur.zarr     # 或 production 路徑;切勿硬編碼
```
> `fastapi`/`uvicorn`/`httpx` 為 P1-S2 app 與 P1-S4 harness/parity 測試所需;`psutil` 供 `/healthz` worker RSS 取樣(缺時退回 `/proc`)。VM24 部署相依見 P1-S5。

## 重現 benchmark / 測試
```bash
dev2026/.venv/bin/python dev2026/bench/bench_pointseries.py --days 150
dev2026/.venv/bin/python dev2026/bench/bench_concurrency.py --n 16
dev2026/.venv/bin/python dev2026/bench/bench_bbox.py
dev2026/.venv/bin/python -m unittest dev2026.tests.test_store_access dev2026.tests.test_api dev2026.tests.test_parity
```

## 慣例
- `uv` 管理 Python(3.13)與 venv。
- store 路徑一律 `GHRSST_ZARR_PATH`,程式不硬編碼;`bak/` 僅本機部分複本,**仍在持續拷貝/每日增長**,工具須容忍缺日。

## VM24 production deployment (v0.3.0, 2026-06-25)

Production on VM24 now runs the dev2026 API through the existing PM2 app `ghrsst`
and the existing NGINX upstream `127.0.0.1:8035`. NGINX was not changed.

Paths:
- Production daily source of truth: `/home/odbadmin/Data/ghrsst/mur.zarr`
- Base time-cube: `/home/odbadmin/Data/ghrsst/mur_timecube_s8_t90_sh128.zarr`
- Delta cube: `/home/odbadmin/Data/ghrsst/mur_timecube_s8_t90_sh128.delta.zarr`
- Runtime worktree: `/home/odbadmin/python/ghrsst-dev2026-phase2`
- Production launcher: `/home/odbadmin/python/ghrsst/conf/start_app.sh`
- Rollback launcher backup: `/home/odbadmin/python/ghrsst/conf/start_app.sh.pre-dev2026`

Production launcher environment:
```bash
GHRSST_ZARR_PATH=/home/odbadmin/Data/ghrsst/mur.zarr
GHRSST_TIMECUBE_PATH=/home/odbadmin/Data/ghrsst/mur_timecube_s8_t90_sh128.zarr
GHRSST_DELTACUBE_PATH=/home/odbadmin/Data/ghrsst/mur_timecube_s8_t90_sh128.delta.zarr
GHRSST_ZARR_WORKERS=4
GHRSST_RSS_CEILING_MB=4096
GHRSST_BBOX_POINT_LIMIT=300000
```

Restart current production app:
```bash
pm2 restart ghrsst --update-env
curl -fsS http://127.0.0.1:8035/healthz
```

Rollback to the pre-dev2026 app:
```bash
cp /home/odbadmin/python/ghrsst/conf/start_app.sh.pre-dev2026 \
   /home/odbadmin/python/ghrsst/conf/start_app.sh
chmod +x /home/odbadmin/python/ghrsst/conf/start_app.sh
pm2 restart ghrsst --update-env
```

Operational checks:
```bash
curl -sS http://127.0.0.1:8035/healthz
curl -sS -D - -o /tmp/ghrsst_365.json \
  "http://127.0.0.1:8035/api/ghrsst?lon0=121&lat0=24&start=2025-06-24&end=2026-06-23&append=sst,sst_anomaly,sea_ice"
```
Expected for multi-day point/range queries: `X-Store-Route: cube`.

Daily delta append cron:
```cron
# Runs after the existing MUR daily retries. Idempotent.
30 20 * * * /home/odbadmin/python/ghrsst-dev2026-phase2/ops/cron_mur_delta_append.sh $(date -u -d "yesterday" +\%Y-\%m-\%d)
30 07 * * * /home/odbadmin/python/ghrsst-dev2026-phase2/ops/cron_mur_delta_append.sh $(date -u -d "yesterday" +\%Y-\%m-\%d)
```
Logs: `/home/odbadmin/Data/ghrsst/logs/delta_append/append_YYYY-MM-DD.log`.

### Known bbox limitation

The v0.3.0 performance work primarily fixes multi-day point/range queries. Bbox
requests still use the daily store and the existing JSON-array wire format. A
752,001-point bbox currently produces about 108 MB of row-oriented JSON; VM24 can
stream it in roughly 4 seconds, but browsers/front-ends may spend much longer
parsing, formatting, and rendering the payload. Production therefore sets
`GHRSST_BBOX_POINT_LIMIT=300000` as a safety guard. A deeper bbox redesign should
be handled as a separate phase; see [`specs/bbox_performance_notes.md`](specs/bbox_performance_notes.md).

## 現況
- [x] 診斷 + benchmark harness(`bench/`、`store/zarr_paths.py`)
- [x] spec(`specs/00`、`specs/01`)— **reviewer accepted (v9)**
- [x] **P1-S1** `store/store_access.py`(座標快取 + thread-safe LRU + 逐 chunk 釋放;`tests/test_store_access.py` 13 tests pass:real-store parity / thread-safety stress / new-day visibility / memory-bound;point_series 12.4×、points_batch 818×)
- [x] **P1-S2** `api/app.py`(full GET 相容 + `POST /points` + streaming bbox(JSON-array)+ 有界 executor/503 背壓 + Cache-Control + MAX_DAYS=365/413);`tests/test_api.py` 15 tests pass + live uvicorn smoke(real store:point/range/bbox-40k-stream/points)
- [x] **P1-S3** `tests/test_parity.py`(新 API vs **真實舊 `ghrsst_app.py`** 並排比對:point/range/bbox/append/mode/sample 的 **parsed JSON / data-shape 相同**(比較 parsed JSON equality,**非位元組級** —— 新 bbox 走 streaming,空白/分塊本就不同)+ 錯誤語意相同;刻意 divergence: cache header、31→365/413 已 assert)。11 tests pass;三模組合併 46/46 兩序皆綠
- [~] **P1-S4** `bench/loadtest.py`(HTTP load harness:SB/LR/BBOX/OL,p50/p95/p99 + shed/timeout/err + `/healthz` queue+RSS 取樣 + JSON 輸出)。**本機已驗證 harness 與 G1′/G6/G7 機制**(LR 隨並發退化、503 shed+16.9ms 恢復、RSS 取樣);結果 **non-binding**,見 [`specs/p1s4_local_results.md`](specs/p1s4_local_results.md)。**待 VM24 全資料跑同工具作為正式 gate**
- [x] **P1-S5/S6 runbook** → [`specs/p1s5_s6_runbook.md`](specs/p1s5_s6_runbook.md)。VM24 binding gate 由 Codex/ops 執行。
- [x] **VM24 P1 binding gate = NO-GO** → [`specs/vm24_p1_binding_results.md`](specs/vm24_p1_binding_results.md)(365-day point series p95 14s vs 4s target;RSS 非瓶頸)。不 cutover。
- [~] **Phase-2 設計(待 review)** → [`specs/phase2_timecube_design.md`](specs/phase2_timecube_design.md):time-optimized store + **hybrid routing**(point/range→新 time store;bbox/單日+POST/points→保留 daily store);**local-first gate**(每 step code+benchmark+gate,local 過才上 VM24);候選資料結構各有 decision gate;promotion checklist。
- [x] **P2-S0a/S0b** harness:[`ingest/build_fixture.py`](ingest/build_fixture.py)(Fixture A 合成)+ [`bench/bench_timecube_microcost.py`](bench/bench_timecube_microcost.py)(per-query chunk_count/decompressed_bytes/read_amp/py_alloc/rss;Fixture B 選 largest contiguous real span)+ `tests/test_phase2_s0.py`(9/9)。**harness 已證可靠**;結果見 [`specs/p2s0_harness_results.md`](specs/p2s0_harness_results.md)。關鍵發現:local 最大連續 real span 僅 96 天 → promotion gate **INCOMPLETE(僅 shadow,不可 cutover)**;prod chunking 下 96 天點序列 decompress ~1.2GB(read_amp ~1M×)= cache-independent 的失敗根因。
- [x] **P2-S1 E1 manifest = REJECT**(`store/e1_manifest.py`,`tests/test_phase2_s1.py` 4/4 parity)。micro-cost:chunk_count/decompressed **與 baseline 完全相同**(288 / 1.2GB),latency 只快 ~3% → manifest-only 無法解 O(days)×4MB decompress。結果 [`specs/p2s1_e1_results.md`](specs/p2s1_e1_results.md)。(本就不可 promote:local 連續僅 96 天)
- [x] **P2-S2 F bounded-parallel = REJECT for gate**(`store/f_engine.py`,`bench/bench_f_engine.py`,`tests/test_phase2_s2.py` 4/4)。資源 hard-gate **PASS**(global bounded pool,in_flight==parallelism,RSS 有界,env pinned);但 F **fails LR multiplier**(P=8:C=1 72ms→C=4 256ms=3.6×B),且 chunk_count/decompressed 不變(total-work ceiling 相同)→ 非結構解。結果 [`specs/p2s2_f_results.md`](specs/p2s2_f_results.md)。**Tier-1 兩候選(E1/F)皆已 benchmark 否決 → 進 Tier-2 time-cube。**
- [x] **P2-S3 Tier-2 time-cube = STRUCTURAL WIN**(`ingest/build_timecube.py`,`store/time_cube.py`,`bench/bench_timecube_sweep.py`,`tests/test_phase2_s3.py` 5/5 parity)。sweep:chunk_count **1095(O days)→3–15**,read_amp 4096×→16–79×,warm latency **1003ms→2–5ms(~200–480×)**;v3 sharding 收檔數 50–143×;append 有界(time_chunk=90 一片)。建議 chunking **spatial=8,time_chunk=90,sharded**。結果 [`specs/p2s3_timecube_results.md`](specs/p2s3_timecube_results.md)。
- [x] **P2-S4 var-absence HANDLED**(`store/time_cube.py` + `ingest/build_timecube.py` 加 per-(day,var) **validity mask**;`tests/test_phase2_s4.py` 5/5 含**真實資料 regional parity**)。dense cube 用 mask 區分 absent(omit)vs land-NaN(null),完全重現 P1 per-day 語意;converter 加 `region=`/`days=` 子集。結果 [`specs/p2s4_var_absence_results.md`](specs/p2s4_var_absence_results.md)。
- [x] **P2-S5 hybrid router**(`store/hybrid_router.py` + `api/app.py` 接 `GHRSST_TIMECUBE_PATH`;`tests/test_phase2_s5.py` 13/13)。多日 point/range→cube,single-day/bbox/POST→daily;routing perf-only,每路 parity==P1;API 設 `X-Store-Route` header;未覆蓋日 fallback daily。結果 [`specs/p2s5_hybrid_router_results.md`](specs/p2s5_hybrid_router_results.md)。Full suite 86/86。
- [x] **P2-S6 dual-write ingest**(`ingest/dual_write.py`:sync_day/upsert idempotent、sync_missing recovery、check_coverage;`/healthz` cube 觀測 + route_counts;`bench/bench_append_scale.py`;`tests/test_phase2_s6.py` 10/10)。append 隨 grid 面積成長;sharding 減 append ~½ 與檔數 40–60×。**(append 驅動的 chunking 結論已被 P2-S7 修正:append 為營運約束,非淘汰 s8 的理由 —— 見下)**。結果 [`specs/p2s6_dual_write_results.md`](specs/p2s6_dual_write_results.md)(部分 superseded by P2-S7)。Full suite 94/94。
- [~] **P2-S7 chunking selection(read-first)**(`bench/bench_chunking_select.py`)。修正 S6:**不因 append 淘汰小 chunk**;read_amp 為讀取判準(s8=79× vs s64=5050×),warm p95 各 spatial 相近(~4–5ms,絕對遠低於 4s gate);append global est s8~67min/s16~22/s32~9/s64~6 **皆在 3h ingest window 內可行**;**file count 由 shard 大小決定(與 inner chunk 獨立)**→ 調大 shard 降檔數。**read-first 建議:spatial=8/t90/sharded(shard 調大控檔數);窗口緊或檔數不可接受才升 s16/s32 或 regional cube**。結果 [`specs/p2s7_chunking_selection_results.md`](specs/p2s7_chunking_selection_results.md)。
  - [x] **P2-S7 full HTTP gate** done(`s8/t90/shard=128` cube-backed API,loadtest 365-day LR):**p95 50/116/205ms @ C=4/8/16(« 4s,比 daily VM24 14s 快 ~70–280×)**,RSS 74–85MB(/healthz),0 timeout/503,**route_counts {cube:6438, daily:57} 確認多日→cube**。
- [~] **P2-S8 VM24 runbook authored** → [`specs/p2s8_vm24_runbook.md`](specs/p2s8_vm24_runbook.md)(operational gates:ingest window/file-count tolerance/disk precheck;full 或 tiled cube build(`build_timecube --region`);cold+warm LR/SB loadtest;append/upsert 量測;coverage/healthz;go/no-go + rollback)。**VM24 執行(binding gate + cutover)由 Codex/ops**,Claude 不執行。
- [x] **Bulk build fix**(`ingest/build_timecube_bulk.py`,`tests/test_phase2_bulk.py` 6/6)。修正部署時 build 過慢(per-day 寫法每天 RMW 整個 90-step shard,~90× 寫放大 → 20+ 天)。改為 **shard-block 批次寫**(time_block × spatial super-tile,每 shard 寫一次)+ bounded workers + checkpoint/resume + `--latest-days`/`--exclude-latest`。**實測 256² 90 天:41.8s→0.5s(77.7×)**;輸出與 per-day 完全相同(parity 通過)。read chunking(s8/t90/shard128)不變。runbook §2 已改用 bulk。結果 [`specs/bulk_build_results.md`](specs/bulk_build_results.md)。
- [x] **Append fix(base+delta 生產設計 + bulk_append_day 過渡)**(`store/tiered_cube.py`,`ingest/dual_write.py` append_to_delta/compact,`ingest/build_timecube_bulk.bulk_append_day`,`tests/test_phase2_append.py` 7/7)。部署發現 append >80min(同 RMW 問題:寫一天 RMW 整個 90-step shard)。**生產解:DELTA cube(time_chunk=1,每日 append 寫一個新 shard,無 RMW)+ TieredCube(base+delta 讀)+ 週期 compaction(bulk 重建 base + reset delta)**;app 接 `GHRSST_DELTACUBE_PATH`,`/healthz` cube_kind=tiered。RMW 成本在「讀取量」,global 才顯著(小 grid 1.2×,global ~80min→分鐘級,binding 在 VM24)。bulk_append_day 為過渡(tiled/bounded/streaming/resume,仍 RMW)。**`append_to_delta` 已改為 tiled/streaming**(逐 tile 讀寫,不再 materialize 全域陣列;bounded workers + per-(day,var,tile) checkpoint/resume;cron CLI `ingest/append_delta_day.py`)——2048² benchmark 峰值 RSS +58.9MB→+2.5MB,峰值 block 受 `read_block²` 限制(global ≈ grid 的 1/618),消除觀測到的 ~7.8GB RSS。**delta chunking 與 base 解耦**(PR #13 VM24 follow-up:VM24 記憶體已修好但 append 仍 >2h/day,真正瓶頸是 delta 沿用 base s8 → 每日 global append 寫 ~10M 個 8×8 tiny chunks/var)——base 維持 `s8/shard128/t90`(讀取),delta 改用 append-optimized `s256/shard256`(預設;create 時套用,append 時沿用既有 layout);`bench/bench_delta_layout.py` 顯示 s256 比 s8 **append 快 215×**、檔案最少、point-read 延遲不變(~3.4ms)。結果 [`specs/append_strategy_results.md`](specs/append_strategy_results.md)。Full suite 115/115。
- [ ] P2-S8 VM24 binding 執行(Codex/ops:bulk build base + delta append + compaction)→ 確認 chunking → cutover
