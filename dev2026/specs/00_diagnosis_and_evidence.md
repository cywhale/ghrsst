# GHRSST API 重構 — 診斷與證據 (dev2026 / doc 00)

> 本文是給獨立 reviewer AI 審查用的「問題與證據」基準文件。
> 姊妹文件：[`01_refactor_spec.md`](01_refactor_spec.md)(目標與解法 steps)。
> 所有量測皆可由 `dev2026/bench/` 重現,reviewer 應自行重跑驗證數字。

## 0. TL;DR(給趕時間的 reviewer)

- 三種查詢型態(單點長序列 / 多點併發 / 單日 bbox)效能瓶頸**同一個根因**:
  **每個 request 都重新 `xr.open_zarr(...)`(重解析 3.02 MB consolidated metadata)+ 重讀整條 lon/lat 座標**。
- 這段工作是 **GIL-bound 的純 Python**,佔單點查詢每日成本的 ~90%、佔 bbox 的大半,且讓 async endpoint 在併發下**序列化**。
- Zarr 的 **chunk 大小(1024×1024)不是點查詢瓶頸**(單點解壓僅 ~3.7–5 ms)。
- 結論:**解除 31 天上限、修好多點併發,不需要重灌資料**;純軟體的「座標快取一次 + 直接開單日 group(bounded LRU)+ threadpool offload」即可(設計細節見 §3.1.1)。實測:
  - 單點 150 天序列:**8.9 s → 0.73 s(12×)**,值完全一致。
  - 16 點併發單日:**882 ms → 13 ms(~70×)**。
- chunk 重整 / time-cube 屬**第二階段選配**,僅在要支援多年序列或加速 bbox 時才需要。

## 1. 專案現況

- repo `cywhale/ghrsst`,FastAPI 單檔 `ghrsst_app.py`(routing + Zarr 存取 + 驗證混在一起)。
- 資料源:NASA GIBS earthdata `MUR-JPL-L4-GLOB-v4.1`,VM24 每日排程抓 NetCDF → 轉 Zarr。
- production 部署於 **VM24**(192.168.2.24 / eco.odb.ntu.edu.tw),gunicorn `-w 2 -k uvicorn.workers.UvicornWorker`,**非本機 Mac**。
- 也對外提供 MCP server(`mcp/metocean_mcp_server.py`),透過 HTTP 打同一個 `/api/ghrsst`,因此 API 變快 MCP 同步受惠。
- 轉檔程式 `dev/mur2zarr_v3.py`。

### 1.1 Zarr 儲存結構(現況)

- Zarr **v3**,階層 group:`/YYYY/MM/DD`,**每天一個獨立 group**。
- 每個 day-group 內含陣列:`lon`(1D, 36000)、`lat`(1D, 17999)、`sst`、`sea_ice`(部分含 `sst_anomaly`)。
- `sst`:`shape=[1, 17999, 36000]`(time, lat, lon),`chunk=[1, 1024, 1024]`,codec `blosc(zstd, clevel=5, bitshuffle)`,`float32`。
- root `zarr.json` 為 inline consolidated metadata,**目前 3.02 MB,隨天數線性增長**。
- 原生網格 **EPSG:4326 0.01°**。API 以 `np.searchsorted` 最近鄰取點。

### 1.2 query 型態(由 orchestrator 確認的優先序)

1. **主要**:單點長時間序列(目前硬上限 `MAX_DAYS=31`,目標是速度上來後解除)。
2. 次要:單日 bbox(較吃重;速度上來後再優化)。
3. 「多點」目前**沒有**單一 batch query,client(地圖平台)只能對每點各打一次 API。

### 1.3 量測環境

- 本機 Mac,28 cores;`uv` 管理,Python **3.13.11**,`xarray 2026.4.0`,`zarr 3.2.1`,`numcodecs 0.16.5`。
- 資料:本機 `bak/data/mur.zarr` 為 VM24 的**部分複本,仍在持續拷貝中**(量測當下 189 天,2025-11-08…2026-04-30,含缺日)。
- 路徑一律由 `GHRSST_ZARR_PATH` 提供,**程式不硬編碼**。`bak/` 僅本機複本位置。

## 2. 問題描述

- 單點查詢被迫限制 31 天,使用者要求解除但擔心速度。
- **多點查詢時 API 速度明顯下降**(地圖平台一次點選多點 → 併發多個單點 request)。
- 疑慮:瓶頸是否與 Zarr 格點儲存結構有關?是否需要 re-indexing / rechunk?

## 3. 根因分析與證據

### 3.1 單點長序列:per-day open 是 90% 成本

`read_ghrsst` 點查路徑(`ghrsst_app.py:337-344`)對每一天呼叫 `_open_group()`(= `xr.open_zarr(root, group=/Y/M/D, consolidated=None)`),而 `_load_coords_from_any` 另外再 open 一次讀座標。

`dev2026/bench/bench_pointseries.py`,點 (119.30672, 22.28274),最近 150 天:

```
[root zarr.json] size=3.02 MB  parse: min=6.97 ms  median=7.22 ms
[sst array] shape=[1,17999,36000] chunk=[1,1024,1024] -> 4.19 MB uncompressed / point-read

=== PRODUCTION path (per-day open + per-request coords) ===
  group opens     : 151
  coords read     :   118.73 ms
  per-day open    :  8002.45 ms   (53.35 ms/day)      <-- 90% of cost
  point isel/comp :   794.34 ms   (5.30 ms/day)
  TOTAL           :  8915.52 ms   (59.44 ms/day)

=== OPTIMIZED path (open root once, reuse coords, direct chunk index) ===
  open root       :    59.01 ms
  coords read     :     1.55 ms
  point read      :   674.35 ms   (4.50 ms/day)
  TOTAL           :   734.91 ms   (4.90 ms/day)

SUMMARY: optimized 12.1x faster on 150 days; parity OK (identical values)
extrapolated 365-day: production ~21.7 s -> optimized ~1.8 s
```

**判讀**:31 天上限幾乎完全是「每天重開 group + 重解析 3 MB metadata」的人為產物,**不是 chunk 大小**。此處 OPTIMIZED 為「root 開一次重用 + 座標快取一次」的量測情境,即得 12× 且值完全一致 —— 但**採用的設計不是這個**(它對新日有 stale 風險),最終採直接開單日 group(見 §3.1.1),效能相同而無 stale 風險。

### 3.1.1 per-day open 策略:直接開 day-group 即可,**不需常駐 root handle**

`53 ms/day` 的成本來自 `consolidated=None` 觸發讀取 **3 MB root consolidated metadata**;若**直接以路徑開單日 group**(`zarr.open_group(<root>/Y/M/D)`),只讀該日的小 metadata,不碰 root。

`dev2026/bench/bench_open_strategy.py`,60 天、單點:

```
(1) PROD  open_zarr(consolidated=None)   57.49 ms/day   <-- 讀 3MB root
(2) ROOT-ONCE  open_group(root) 重用       4.33 ms/day   <-- 快但對新日 STALE
(3) DIRECT  per-day open by path           4.26 ms/day   <-- fresh,無 root handle,= root-once
(4) DIRECT+LRU warm                        3.46 ms/day
```

**判讀(直接回應後續 store_access 設計)**:**直接開單日 group(4.26)= 常駐 root handle(4.33)**。

> **此 benchmark 的結論範圍(回應 Codex review [Medium]):它只證明「持有 root handle 沒有效能收益」,不證明 correctness。** 也就是:既然 root handle 對速度無益,就沒有理由為了速度去長持它、並承擔 consolidated metadata 對新日 stale 的風險。

基於此,設計選擇為:座標快取一次(整資料集不變)+ day 以 filesystem 重掃發現 + 單日 group 以 bounded LRU 直接開。此設計**在架構上不依賴 consolidated metadata 做 day 發現**,因此從設計上排除了「root handle 對新日 stale」這一類失效;且不斷增長的 3 MB root metadata **退出 hot path**(同時改善 §4.1 的長期成長疑慮)。
> **但「新日可見性」的正確性必須由 integration test 證明,不能由本 benchmark 推斷**(見 doc 01 P1-S1 驗收):worker 暖機後於 store 新增一日,經 TTL 後須讀得到。
> 旁證(非正式):本機複本在量測期間自動長到 `2026-06-20`(初次量測時為 `2026-04-30`),filesystem 重掃即時看到新日 —— 但這只是觀察,正式驗收仍以 integration test 為準。

### 3.2 多點併發:async endpoint 在併發下序列化

`read_ghrsst` 是 `async def`,卻直接呼叫同步 `xr.open_zarr(...).compute()`,**沒有 threadpool offload** → 阻塞 event loop。production 又只有 `-w 2`,有效併發 ≈ 2。

`dev2026/bench/bench_concurrency.py`,16 點併發、單日:

```
SERIAL (sum)              :  882.5 ms
asyncio.gather BLOCKING   :  876.1 ms   (current async endpoint)   blocking/serial = 0.99x
asyncio.gather TO_THREAD  :  865.7 ms   (offload only)             speedup = 1.02x  <-- 沒用!

=== CACHED body (root open once + coords cached) ===
  CACHED serial (sum)     :   59.7 ms   (3.73 ms/point)
  CACHED gather BLOCKING  :   57.5 ms
  CACHED gather TO_THREAD :   12.7 ms
  cached offload speedup  : 4.72x   (chunk 解壓會釋放 GIL => 真正並行)
  full win vs current     : 69.71x  (882 ms -> 13 ms)
```

**判讀**(關鍵且反直覺):
- `blocking/serial = 0.99x` → 證實 async 在現行路徑下完全沒有併發效益(event loop 被序列化)。
- **單純加 threadpool offload 無效(1.02×)**:因為主成本 `open_zarr` 的 metadata parse 是 **GIL-bound 純 Python**,執行緒搶不到並行。
- **必須先移除 per-request open**(座標快取 + 直接開日 LRU),之後每點只剩 chunk 解壓(blosc/zstd 釋放 GIL),offload 才產生 4.7× 並行 → 合計 ~70×。

> 對 reviewer 的重點:本案的修法順序有依賴 — **先快取、再 offload**,順序反了 offload 無效。

### 3.3 bbox 單日:Zarr 讀取很便宜,瓶頸在 open + 逐列建構

`dev2026/bench/bench_bbox.py`,單日:

```
open+coords: 105.2 ms
  bbox  1deg   101x101  = 10,201 pts    compute=7.8 ms
  bbox  5deg   501x501  = 251,001 pts   compute=5.0 ms
  bbox 10deg   1001x1001= 1,002,001 pts compute=6.1 ms
```

**判讀**:Zarr slice 讀取對 1M 點僅 ~6 ms(連續 slice 落在少數 chunk)。bbox 真正的成本是 (a) `open+coords` 105 ms(同根因,快取可解)、(b) `read_ghrsst:405-411` 的 `for idx in range(lon_grid.size)` **逐列建 dict 的 Python 迴圈**(對 1M 點為 O(N) Python,未在此計時但程式碼可見)。→ bbox 的後續優化是「向量化序列化」,與 chunk 結構無關。

### 3.4 共同根因(三型態收斂到同一點)

| 型態 | 主成本 | 根因 | 是否需動資料 |
|---|---|---|---|
| 單點長序列 | per-day `open_zarr` 53 ms/day | 重複 open + metadata parse | 否 |
| 多點併發 | per-request open(GIL-bound)+ 無 offload | 同上 + async 阻塞 | 否 |
| 單日 bbox | open 105 ms + 逐列 Python 迴圈 | 同上 + 未向量化序列化 | 否 |

**三者皆不需重灌資料即可大幅改善。** chunk/cube 重整是第二階段選配。

## 4. 衍生觀察(非本期阻擋項,但 spec 需記錄)

1. **consolidated metadata 隨天數線性增長**(已 3 MB),每次 open 重 parse;即便快取,group 數成長也讓 `open_group` 變慢、`_scan_bounds` 目錄掃描變慢 → 長期仍建議 time 維度化或 metadata 分層。
2. **無 time 維度**:單點序列在現結構必然是「N 次 group 存取」;time-cube 可把一個點的整條序列收斂到少數 chunk(見 doc 01 第二階段)。
3. **小空間 chunk vs 每日全球 append 的小檔爆炸**:若為點查改小空間 chunk,全球每日 append 會產生大量小檔;**Zarr v3 sharding** 是化解槓桿(邏輯 chunk 小、實體 shard 大)。
4. **bbox 逐列建構**未向量化(`ghrsst_app.py:405-411`)。
5. **store 仍在拷貝中 / 每日增長**:任何工具與 benchmark 都必須容忍缺日與檔數變動;`dev2026/store/zarr_paths.py` 已採每次重掃、不假設連續。
6. **(現存 production 快取問題)default-latest GET 在 10 天 TTL 下會 stale**:VM24 `aio_cache_proxy.conf` 設 `proxy_cache_valid 200 302 10d` 且 `proxy_cache_key "$http_host$request_uri"`。對**不帶 date 的 GET**(預設回 latest),URI 每天相同。
   - **已實測(可快取性)**(`dev2026/bench/probe_nginx_cache.py` 對 `https://eco.odb.ntu.edu.tw/api/ghrsst`):date-less GET 連兩次 `X-api-cache` = **MISS → HIT** → **date-less latest response 確實會被快取**;固定日期 GET 亦 MISS→HIT(無害,資料不可變);兩個不同 query string 回不同 payload(GET key 正常)。
   - **精確結論**:此 probe 證明的是「可被快取」,**尚未跨日比對到實際 stale 值**。但在 10 天 TTL + URI 不變下,**下一次每日 ingest 之後必然回傳過時的 latest,除非 bypass/purge** —— 故仍列為需修的 bug。
   - 固定日期查詢長快取無害;date-less 查詢需 `no-store`/極短 max-age。詳見 doc 01 P1-S6(cache)與 P1-S2 Cache-Control 政策。
7. **(新 endpoint 的快取 landmine)`POST /points` 在現有 cache 設定下會撞 key**:`proxy_cache_methods GET POST` + cache key 不含 body → 不同點集合互相回傳對方快取結果;現有 `$request_body_file` 守門對小 body 不生效。**必須**在開放 batch 前處理(doc 01 P1-S6 第 2 點)。
8. **(記憶體,已實測)大 bbox 的回應序列化是真正的記憶體熱點,且「向量化」不等於省記憶體**:`dev2026/bench/bench_bbox_memory.py`(tracemalloc,1,002,001 點單日 bbox、2 fields):
   - [A] 現行逐列 dict + orjson:peak Python 配置 **396 MB**(payload 109 MB)。
   - [B] 改成 columnar lists(`.tolist()` + list comprehension):**429 MB —— 反而更糟,不是解法**(仍 materialize 百萬 Python 物件)。
   - [C] **分批 streaming**(每 5 萬列 encode 後釋放):**21.6 MB**(payload 仍 ~109 MB 但不常駐)→ 約 **18× 改善**。
   - **程序級 RSS(`ru_maxrss`)**:上述三路徑依序跑完,process 峰值 RSS 由 148 → **~1.2 GB**。此數遠高於 tracemalloc 的 396 MB,因為含 numpy/zarr/解壓/orjson 的**原生緩衝**(tracemalloc 只計 Python 物件)。**注意:此 `ru_maxrss` 是單一 process 內 A→B→C 累積的 high-water mark,僅為「naive 路徑會把 RSS 推高」的佐證,無法隔離 C 的逐路徑 RSS。**
   - **權威驗收**:G6 以 **HTTP-level worker RSS**(doc 01 P1-S4 scenario-BBOX/-LR,每 1 s 取樣)為準,非此 in-proc 數字;tracemalloc/`ru_maxrss` 僅佐證。
   - **結論**:bbox 的修法必須是**分批 streaming 回應**(peak = 單批),而非單純「向量化」;此記憶體在併發下會每個 in-flight request 疊加 → 是 G6/RSS 上限與有界 executor 的直接動機。詳見 doc 01 P1-S2 / P2 選項 B 與 §7。

## 5. 非本期範圍(明確排除)

- **WMS / API 數值不一致**:orchestrator 決議**本期擱置**,只做效能。根因初判為 GIBS Web-Mercator 重投影/重取樣像素 vs 原生 0.01° 最近鄰的取樣差異(色階每段 0.15°C,梯度區易跨段),非 API bug;codex 的「tile 偏移」說法與 Leaflet 圖台和 Worldview 一致的事實矛盾,判為 red herring。詳留待後續獨立文件。
- 不修改任何 production 檔案;所有新開發置於 `dev2026/`。

## 6. 重現方式(reviewer 請自跑)

```bash
cd <ghrsst repo>
uv venv dev2026/.venv --python 3.13
uv pip install --python dev2026/.venv/bin/python "zarr>=3" "xarray>=2025.1" numpy orjson
export GHRSST_ZARR_PATH=$(pwd)/bak/data/mur.zarr     # 或 production 路徑
dev2026/.venv/bin/python dev2026/bench/bench_pointseries.py --lon 119.30672 --lat 22.28274 --days 150
dev2026/.venv/bin/python dev2026/bench/bench_concurrency.py --n 16
dev2026/.venv/bin/python dev2026/bench/bench_bbox.py
```

> 註:本機數字為 OS page cache 大致 warm 的情況;VM24 冷快取下絕對值會更高,但相對結論(per-request open 為主因、快取+offload 為解)不變。建議 reviewer 在 VM24 上以完整資料量再跑一次確認絕對值。
