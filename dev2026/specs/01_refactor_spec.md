# GHRSST API 重構 — 目標與解法 (dev2026 / doc 01)

> 姊妹文件:[`00_diagnosis_and_evidence.md`](00_diagnosis_and_evidence.md)(問題與證據)。
> 本文定義目標、非目標、分階段 steps 與**驗收標準**,供獨立 reviewer 審查;審查通過後才進入開發。
>
> **修訂 v2(回應第一輪 Codex review)**:新增 P1-S6 cutover;store_access 改為無長壽 root handle(新增 doc 00 §3.1.1 實測佐證);batch payload 契約定死;驗收改 p50/p95/p99 + sustained(G2′)+ 部署 sizing。
> **修訂 v3(回應第二輪 Codex review)**:P1-S6 寫死實際 NGINX 拓撲(兩 location 同指 `ghrsstapi`、`aio_cache_proxy` 快取處理、`ghrsst_mcp` 為未用殘留);§3.1.1 措辭改為「benchmark 只證明無效能收益、correctness 由 integration test 證明」;batch 補明文 request/response schema;G2′ 補 sustained burst 的 pass/fail 操作型定義(C/T/視窗/backlog);全文移除殘留的「常駐 root group」敘述。逐項對照見 §8。
> **修訂 v4(納入實際 cache 設定)**:依 VM24 `aio_cache_proxy.conf` 內容,P1-S6 cache 段改寫並標出**必修 correctness landmine**(`POST /points` 因 cache key 不含 body 而撞 key)+ 修法(app `no-store` + NGINX location 停快取);P1-S2 加 Cache-Control 政策;doc 00 §4 新增兩條快取相關發現(default-latest 10 天 stale、batch 撞 key)。
> **修訂 v5(回應第三輪 Codex review)**:probe 修掉 `date:null` 前提(自動從 latest GET 推日期,推不出則 skip 標 NOT VERIFIED);GET-2 措辭精確化為「可被快取 → 下次 ingest 後必然 stale」而非「已觀測 stale」;P1-S6 補 `/api/ghrsst/points` 的**明確最長前綴 NGINX location 片段**(`proxy_cache off` 且不 include cache conf);date-less GET 的 Cache-Control 由 `no-cache` 改 **`no-store`**(無 ETag 可 revalidate)。
> **修訂 v6(回應第四輪 Codex review)**:probe 加 **`--strict`/`--require-post`** —— staging/cutover 下 `/points` 缺席/無日期/非 200/inconclusive 一律 exit 1(不再靜默綠燈);`/points` NGINX 片段補**完整 CORS + OPTIONS 204 preflight**(子 location 不繼承父層 add_header)與 **`add_header X-api-cache BYPASS always`**;canary 通過條件改為「`X-api-cache`∈{MISS,BYPASS} **或** 無 header 但 POST-3 無 collision」;probe 輸出文字同步 v5 的 `no-store` 措辭。
> **修訂 v7(回應第五輪 Codex review — 資源/併發系統性)**:新增 **G1′**(並發 365 天序列)、**G6**(RSS 有界)、**G7**(過載降級);§4 加**執行緒安全規則**(LRU 加鎖、handle 唯讀並發、fallback)與**有界 executor + 背壓**(過載 503/429 + `Retry-After`);取值 API 改「逐 chunk 讀→取→釋放」+ batch fan-out 上限 + bbox streaming 序列化;P1-S4 加 **scenario-LR / -BBOX / -OL** 與 RSS 量測、executor 佇列納入 backlog;P1-S5 加 `GHRSST_RSS_CEILING_MB` / `GHRSST_ZARR_WORKERS` 等 tunables 與 sizing 算式;§6 decision gate 以 G1′ 為主要觸發;CORS 補 credentials caveat。
> **修訂 v8(回應第六輪 Codex review — P1 前最後收斂)**:bbox streaming **維持既有 JSON-array wire format**(StreamingResponse 串 `[`+逗號批次+`]`;NDJSON 僅 opt-in,不改預設);scenario-LR 給**具體門檻**(C=4 p95<2·B、C=8 p95<3·B、C=16 可 shed 但 served p95<4·B 且零逾時/OOM);過載 canonical 定為 **`503`+`Retry-After`** + **容量模式**(SLO 內不得拒絕、過載才拒絕);**G6 權威 gate = HTTP-level RSS**(tracemalloc 僅佐證,bench 已補 ru_maxrss,實測單發 1M bbox 推升 process RSS 至 ~1.2 GB);P1-S5 給**全部 tunables 的保守預設表**(`GHRSST_POINTS_BATCH_MAX=1000`、`GHRSST_BATCH_CHUNK_FANOUT_MAX=64` 等)。
> **修訂 v9(回應第七輪 Codex review — spec hygiene,P1 放行前)**:bbox 相容性措辭由「位元組級相容」改為「**schema / media-type / JSON-array 相容**(非位元組級相同)」;**全文過載碼統一為 `503`+`Retry-After`**(移除殘留 `429/503`,`429` 註明保留給未來 per-client rate-limit);`/points` 契約直接引用 P1-S5 的 `GHRSST_POINTS_BATCH_MAX=1000`/`FANOUT_MAX=64`(不再「待 §8 定」);doc 00 §4.8 補 `ru_maxrss` 程序級 RSS(~1.2 GB,並註明為累積 high-water、非逐路徑、權威 gate 仍是 HTTP worker RSS);bench 輸出補同樣 caveat。

## 1. 目標(可量測)

| # | 目標 | 量測基準(本機 28-core,live 複本) |
|---|---|---|
| G1 | 解除單點 31 天上限,支援至少 365 天序列 | 365 天單點序列 **p50 < 2.5 s 且 p95 < 4 s**(現況外插 ~21.7 s) |
| **G1′** | **長序列在多人併發下不崩**(回應 Codex review [High] #1) | **並發 365 天序列** C∈{4,8,16}:**p95/p99 有界、零逾時、RSS 有界**;pass/fail 見 P1-S4 §scenario-LR |
| G2 | 多點併發不再雪崩 | 16 點併發單日 **p50 < 50 ms 且 p99 < 150 ms**;見 G2′ sustained | 
| G2′ | 持續 burst 不退化(tail latency) | sustained load 下 **p95/p99 不隨時間單調上升、無 queue 累積(含 executor queue,非只 HTTP in-flight)**;操作型定義見 **P1-S4** |
| **G6** | **記憶體有界**(回應 Codex review [High] #2) | G1′/G2/G2′/最大 bbox/batch fan-out 下,**worker RSS ≤ `GHRSST_RSS_CEILING_MB`**(VM24 依實體 RAM 設,見 P1-S5);無單調成長(疑似洩漏) |
| **G7** | **過載時可控降級**(回應 Codex review [High] #3) | Zarr 工作經**有界 executor/semaphore**;過載回**快速 `503` + `Retry-After`**(canonical;`429` 保留給未來 per-client rate-limit),不無限排隊、不 OOM |
| G3 | 新增單一 batch 多點 endpoint | 一次 request 取 N 點/同日,契約見 §5 P1-S2(順序、index、上限明確) |
| G4 | production **檔案**零影響,且 traffic 可受惠 | 不修改任何既有檔案(新 app 另一 port 並存)**＋ 明確 cutover 路徑**(§5 P1-S6),否則 G1/G2 對真實使用者無效 |
| G5 | 行為與舊 API 一致 | 相同 (lon,lat,date) 回傳值與舊 API 逐筆相符(parity test) |

> 絕對門檻為本機基準;VM24 冷快取下以「相對舊路徑的加速倍數」為準(G1 ≥8×、G2 ≥15×),且 G1′/G2′/G6/G7 在 VM24 部署設定下複測。
> **驗收一律報 p50 / p95 / p99 三個分位數,不得只報 p50**(Codex review [Medium])。
> **資源面與功能面同等重要**:速度達標但 RSS 無界、executor 無背壓、或 LRU 非執行緒安全,**一律不算通過**(Codex 第五輪:這些才決定加速能否在 VM24 真實多人併發下存活)。

## 2. 非目標(本期不做)

- **不重灌 / 不 rechunk production 資料**(第一階段)。資料層變更僅在第二階段、且需 benchmark 證據才啟動。
- **不處理 WMS/API 一致性**(orchestrator 已擱置)。
- 不改 MCP server 介面(它打 HTTP,API 變快即受惠)。
- 不引入新的查詢語意(除 batch 多點外);時間範圍語意沿用舊 API。

## 3. 設計原則(reviewer 檢查點)

1. **production 檔案唯讀**:`ghrsst_app.py`、`dev/`、`conf/` 一律不改;新程式只進 `dev2026/`。
2. **路徑不硬編碼**:一律 `GHRSST_ZARR_PATH`(見 `dev2026/store/zarr_paths.py`)。
3. **容忍 live store**:store 仍在拷貝/每日增長,所有 day 列舉每次重掃、跳過缺日、不假設連續。
4. **修法順序有依賴**:**先消除 per-request open(快取),再加 threadpool offload**;順序反了 offload 無效(doc 00 §3.2 實證)。
5. **deterministic parity 優先**:任何快取/最佳化都必須通過「與舊 API 逐筆比值」測試。

## 4. 架構:dev2026 store 存取層

```
dev2026/
  store/
    zarr_paths.py     (已建) 路徑解析、day 列舉、group ref —— 容忍 live store
    store_access.py   [P1-S1] lon/lat 快取一次、直接開單日 group + thread-safe bounded LRU、
                              nearest-index、point_series/points_batch/bbox 取值(逐 chunk 釋放)
  api/
    app.py            [P1-S2] 新 FastAPI(另一 port);endpoint 經「有界 executor + 背壓」offload
  bench/
    bench_pointseries.py / bench_concurrency.py / bench_bbox.py   (已建) 證據與回歸
    bench_after.py    [P1-S4] 重構後對照(同點同日,新 vs 舊路徑)
  tests/
    test_parity.py    [P1-S3] 新 API vs 舊 API 逐筆比值
  specs/              本資料夾
```

**store_access 契約(核心)** — 設計依據 doc 00 §3.1.1 實測,回應 Codex review [High] #2:

- **不持有長壽的 root handle**,也不靠 consolidated metadata 做 day 發現(故無 stale 問題)。實測:直接以路徑開單日 group(4.26 ms/day)= 常駐 root handle(4.33 ms/day),且不碰那份會持續長大的 3 MB root metadata。
- **座標(lon/lat)快取一次常駐**:整個 dataset 不變,屬唯一安全的長壽快取。(設一個明確的 reload hook:若日後座標網格真的變更,需重啟或顯式 invalidate;預設視為不變。)
- **day 發現走 filesystem 重掃 + TTL**(`zarr_paths.list_existing_days`,純檔案系統,永遠看得到剛拷貝/新增的日;不快取「不存在→存在」的負結果)。沿用 app 的 `latest.json` mtime 機制做刷新節流。
- **單日 group 以路徑直接開,bounded LRU**(keyed by date,上限可設;避免長序列把所有 group 物件常駐)。新日由重掃發現後直接開、進 LRU。
- **明確失效規則**:LRU 只快取「已確認存在」的日;TTL 到期重掃;不存在的日不進 LRU。長跑 worker 因此一定看得到新日資料。
- 取值 API:
  - `point_series(lon, lat, days, fields)` → 用快取座標解 index,**逐日 chunk 取值後即釋放**(不得一次把整段序列的 chunk 全載入記憶體再組裝;peak 記憶體 = 單一 chunk×fields)。
  - `points_batch(pairs, day, fields)` → 座標解一次,**依 1024×1024 chunk 分組,逐 chunk 讀→取點→釋放**(peak = 單一 chunk×fields,而非同時持有所有 fan-out chunk);受 `GHRSST_POINTS_BATCH_MAX` 與「單一 batch 可觸及之不同 chunk 數上限 `GHRSST_BATCH_CHUNK_FANOUT_MAX`」雙重限制。
  - **欄位缺失語意(P1-S1 定案,回應 Codex review #1;與舊 API 對齊)**:被請求但「該日 group 不含」的 allowed 欄位 —— **`point_series` 省略該 key**(與舊 GET point path 一致:`if f in ds` 才寫);**`bbox` 回 `null`**(與舊 bbox path 一致:缺欄填 NaN→null);**`points_batch`(新端點)明確定為 `null`**(與 bbox 對齊,給 client 穩定欄位)。三者皆有 deterministic 測試(`sst_anomaly` 缺日)。陸地/NaN 一律 `null`。
  - **bbox 點數上限**:store 層 `bbox_point_limit`(預設 1,000,000,defense-in-depth)+ P1-S2 API 層同樣 guard(回應 Codex review #3)。
  - `bbox(day, bbox, fields, stride)` → **分批 streaming 回應,但維持既有 wire format**(回應 Codex review [High] #1):**預設仍輸出單一 JSON array of row objects** —— 實作為 `StreamingResponse`,先送 `[`,再分批 encode「以逗號相接的 row 物件」,最後送 `]`(每批 encode 後釋放)。**對 client 而言 schema / media-type / JSON-array 相容**(回應 Codex review:**非位元組級相同** —— 空白、chunk 邊界、flush 時機可能與舊 `ORJSONResponse` 不同,但解析出的 JSON 陣列與欄位一致),REST/MCP 既有 client 不需改。
    - **不得**把 `/api/ghrsst` 預設回應改成 NDJSON(那是 wire-format 破壞性變更)。
    - 如需 NDJSON,只能走**opt-in**(例如 `?format=ndjson` 或 `Accept: application/x-ndjson`),預設不啟用。
    - **實測警告(doc 00 §4.8)**:單純改 columnar lists 的「向量化」**不省記憶體**(1M 點:逐列 396 MB、columnar 429 MB、streaming 21.6 MB)。故硬性要求是 **streaming**(逐批釋放),不是「向量化」。

**執行緒安全(回應 Codex review [Medium] #4)** —— store_access 會被 threadpool offload 並發呼叫,LRU 與快取的 zarr handle 成為共享可變狀態:
- **LRU 的 get/put/evict 必須加鎖**(`threading.Lock`)保護 dict 結構。
- 快取的 zarr group/array handle **僅供唯讀並發使用**;需以壓力測試(P1-S1 驗收)確認「同一 handle 多執行緒並發讀」在本 zarr/numcodecs 版本下值正確、不崩。
- **fallback(若壓測發現 handle 非並發安全)**:改為 **thread-local handle 快取**或**每次操作直接開**(實測 4.26 ms/day,成本可接受),不得以「全域 handle 加大鎖序列化同日讀取」當解(那會殺掉並發)。
- 座標快取為唯讀不可變,免鎖。

**有界 executor 與背壓(回應 Codex review [High] #3)** —— 結構性修法是「先除 GIL-bound metadata parse、再 offload 剩餘 blocking Zarr 讀」,但 offload 無界時瓶頸只是從 event loop 移到 threadpool 排隊 / disk 競爭 / 記憶體尖峰:
- **所有同步 Zarr 重工經一個「有界」executor 或 semaphore**(大小 `GHRSST_ZARR_WORKERS`,預設 ≈ CPU 數),**不得**用無界 `asyncio.to_thread` 預設池硬接所有並發。
- **容量模式定義(回應 Codex review [Medium] #3)**:
  - **在 SLO 容量內**(scenario-SB、scenario-LR @ C∈{4,8})→ **必須服務,不得拒絕**(零 5xx)。
  - **過載**(scenario-OL、scenario-LR @ C=16 視結果)→ **可快速拒絕**,但**零逾時、零 OOM、RSS ≤ ceiling**,且負載移除後秒級恢復。
  - 換言之:拒絕是「保護機制」,只在超出容量時啟動;容量內不得用拒絕來假裝達標。
- **canonical 過載回應 = `HTTP 503` + `Retry-After`**(非 429)。理由:這是**全域容量**保護(伺服器暫時無法處理),非 per-client 配額;client 依 `Retry-After` 退避重試。`429` 保留給未來真正的 per-client rate-limit。門檻可設(`GHRSST_OVERLOAD_QUEUE_MAX` / `GHRSST_OVERLOAD_WAIT_MS`)。
- **可觀測性**:輸出 executor 佇列深度與等待時間指標,供 G2′/G7 驗收(backlog 判定須含 executor 佇列,非只 HTTP in-flight)。
- **串流請求(bbox)必須在「整個串流生命週期」持有一個 admission permit**(回應 PR review):不可只 gate 初始 slice 讀取就釋放 permit、再用 ungated 工作串流剩餘批次 —— 否則進行中的串流對 `queue_depth` 不可見、RSS/threadpool 不被容量計入,違反 G6/G7。permit 於串流完成或 client 斷線(generator `finally`/`aclose`)釋放;permit 持有期間的分批 encode 才可用 ungated 執行。P1-S2 已實作並以「成功/錯誤路徑皆不洩漏 permit」測試驗證。

## 5. 第一階段(純軟體,不動資料)— steps 與驗收

### P1-S1 `store/store_access.py`(快取座標 + 直接開日 LRU)
- 內容:快取 lon/lat、filesystem day 發現(TTL)、bounded LRU 直接開單日 group(**無長壽 root handle**)、nearest-index、`point_series` / `points_batch` / `bbox` 取值函式。
- **驗收**:
  - 單點 150 天序列 ≥ **8×** 快於舊路徑(本機目標重現 doc 00 §3.1 的 ~12×)。
  - 取值與舊路徑 parity 完全一致(0 mismatch)。
  - 對缺日輸入正確跳過、不丟例外。
  - **新日可見性測試**:在 process 已暖機(座標已快取、LRU 已有舊日)後,於 store 新增一個 day-group,經 TTL 後查詢該新日**必須讀得到**(回應 Codex [High] #2)。
  - **執行緒安全壓力測試(回應 Codex [Medium] #4)**:多執行緒並發呼叫 store_access,混合 (a) 同日同點、(b) 同日不同點(觸發 chunk fan-out)、(c) 不同日(觸發 LRU churn/eviction)、(d) 持續超過 LRU 容量的 day 輪替。要求:**結果與單執行緒 parity 完全一致、無例外/崩潰、無資料串味**;若失敗則啟用 §4 fallback(thread-local / 每次開)。
  - **單請求記憶體上界**:`point_series`(365 天)與 `points_batch`(達 `GHRSST_POINTS_BATCH_MAX`)的 peak 額外記憶體 ≈ 單一 chunk×fields 等級(證明「逐 chunk 釋放」確實生效,非一次全載)。

### P1-S2 `api/app.py`(新 FastAPI,另一 port)
- 端點:
  - `GET /api/ghrsst`(相容舊參數:point / bbox / range / append / mode / sample)。
  - `POST /api/ghrsst/points`(新)。
  - point range 上限 `GHRSST_MAX_DAYS`(**預設 365**,定案)。**請求 span > 上限 → HTTP 413**,body 含 `max_days` / `requested_days` / 拆分建議;**不做 pagination**;上限對「**請求 span(clamp 前)**」計算。VM24 staging 實測後可調(如 730)。
- 所有 zarr 工作經**有界 executor**(`GHRSST_ZARR_WORKERS`)+ 背壓 offload(**先快取後 offload**,順序見 §3.4;細節見 §4「有界 executor 與背壓」)。
- **過載降級(G7)**:佇列深度/等待超門檻 → 快速 **`503` + `Retry-After`**(canonical,見 §4 容量模式),不無限排隊;容量內不得拒絕。
- **Cache-Control 政策(與 P1-S6 cache 一致)**:
  - `POST /api/ghrsst/points` → `Cache-Control: no-store`(避免 §P1-S6 的 URI-only key 撞 key)。
  - date-less GET(預設 latest)→ **`Cache-Control: no-store`** 或極短 `max-age`(避免 doc 00 §4.6 的 10 天 stale latest)。**不要用 `no-cache`**:`no-cache` 仍允許存 response、只是用前需 revalidate,而目前 API 沒有 ETag/Last-Modified 可供正確 revalidation;對「每天變、URI 不變」的 date-less 查詢,`no-store` 更直接安全。
  - 帶固定 date 的 GET → 可長快取(資料不可變;可沿用既有 10d)。

**`POST /api/ghrsst/points` 契約(回應 Codex review [Medium] — 明文 schema)**:

request:
```json
{
  "date": "2026-04-30",            // 單日(必填);語意同 GET 的單日 bbox
  "points": [[119.30672, 22.28274], [120.5, 23.0]],   // [[lon,lat], ...]
  "append": "sst,sst_anomaly,sea_ice",                 // 選填,預設 "sst"
  "mode": "truncate"                                    // 選填,沿用 GET 的 mode
}
```
response(HTTP 200):
```json
[
  { "index": 0, "lon": 119.31, "lat": 22.28, "date": "2026-04-30",
    "sst": 28.303, "sst_anomaly": -0.481, "sea_ice": null },
  { "index": 1, "lon": 120.50, "lat": 23.00, "date": "2026-04-30",
    "sst": 27.91,  "sst_anomaly": -0.20,  "sea_ice": null }
]
```
規則:
- **輸出順序 = 輸入順序**,**N in → N out**(每個輸入點恰好一列)。
- 每列**必帶 `index`**(= 輸入陣列位置),client 不需靠座標回推對齊。
- `lon`/`lat` 回傳的是**最近鄰格點座標**(非輸入原值),與 GET 行為一致。
- **重複點保留**(API 層不去重;內部 chunk-grouping 去重為最佳化、對外不可見、不改列數/順序)。
- append 未列出的欄位**不出現在列中**(與 GET 一致);陸地 / NaN → 該欄 `null`;超出網格 → 沿用 GET 的 clamp。
- `len(points)` 上限 `GHRSST_POINTS_BATCH_MAX`(**預設 1000,見 P1-S5 tunables 表**);另受 `GHRSST_BATCH_CHUNK_FANOUT_MAX`(預設 64)限制 chunk fan-out;超過任一上限回 **HTTP 413** 並於 body 附上限值。
- 錯誤(無效日期、空 points、欄位不合法)回 4xx,與 GET 的錯誤語意一致。
- **驗收**:
  - 16 點併發單日 **p50 < 50 ms、p99 < 150 ms**(重現 doc 00 §3.2 的 ~70×)。
  - 365 天單點序列 **p50 < 2.5 s、p95 < 4 s**。
  - batch 契約測試:N in = N out、順序一致、`index` 正確、重複點保留、上限拒絕。
  - **過載行為測試(G7)**:以遠超 `GHRSST_ZARR_WORKERS` 的並發灌入,須觀察到**快速 `503` + `Retry-After`**(canonical;而非逾時/卡死),RSS 不失控,且負載移除後**秒級恢復**正常延遲。
  - 與舊 app 並存(不同 port),舊 app 行為不受影響。

### P1-S3 `tests/test_parity.py`(回歸保證)
- 對一組固定 (lon,lat,date) 與多個 bbox,逐筆比對新 API vs 舊 `ghrsst_app` 取值。
- 涵蓋:海上點、近岸點、NaN(陸地)點、日界/缺日邊界、bbox stride。
- **驗收**:全數一致(NaN 對 NaN 視為相符)。

### P1-S4 `bench/bench_after.py` + 報告(含 tail latency 與 sustained load)
- 重跑 doc 00 三支 bench 的「after」版,產出 before/after 對照表寫回 `00`(或附錄)。
- **必須對真實 HTTP endpoint(經 uvicorn/gunicorn,套用 P1-S5 的部署設定)做負載量測**,而非只測函式層。

所有負載情境**同時記錄 worker RSS**(每 1 s 取樣;`-w` 多 worker 時取總和與單 worker 峰值),供 **G6** 判定。

**scenario-SB:sustained burst(短查詢混合;G2′)**:
- **負載產生器**:固定並發連線數 **C**(預設 C=32,另跑 C=8/64);closed-loop。混合:80% 單點(隨機點、隨機 1–60 天)+ 20% batch points(每批 16 點),打真實 HTTP。
- **持續時間**:**T=120 s**;丟棄前 10 s warmup;統計窗口其後 110 s,切 **10 s 滾動視窗**各算 p50/p95/p99。
- **pass(全須成立)**:
  1. 整體 **p99 < 150 ms**(單點)且 batch p99 < 對應上限(報告載明)。
  2. **無 tail 漂移**:末視窗 p95 ≤ 首視窗 p95 × **1.2**。
  3. **無 backlog 累積**:HTTP in-flight **與 executor 佇列深度/等待時間**皆不單調上升(回應 Codex [High] #3:backlog 須含 executor 佇列,非只 HTTP);throughput ±10% 內;此情境(在容量內)**零逾時 / 零 5xx**。
  4. backlog 判定:任一 10s 視窗平均 in-flight > C 或 executor 佇列等待 > `GHRSST_OVERLOAD_WAIT_MS` 的時間佔比 > 5% → fail。
  5. **G6**:單/總 worker RSS ≤ `GHRSST_RSS_CEILING_MB`,且窗間無單調成長。

**scenario-LR:concurrent long-range series(最貴路徑;G1′,回應 Codex [High] #1/#2)**:
- **這是解除 31 天上限後真正的壓力來源** —— 多人同時拉長序列,而非 16 個單日點。
- 並發 **C∈{4,8,16}** 的 **365 天單點序列**(隨機點),closed-loop,T=120 s。
- **明確 pass/fail 門檻(以單發 365 天序列 p95 為 baseline `B`,G1 要求 B<4 s;本機與 VM24 皆以「相對 B 的倍數」為準,絕對值於報告載明)**:
  - **C=4**:**served p95 < 2·B**;零拒絕、零逾時、零非預期 5xx;RSS ≤ ceiling 且無單調成長。
  - **C=8**:**served p95 < 3·B**;零拒絕、零逾時;RSS ≤ ceiling。
  - **C=16**:**允許**以 `503 + Retry-After` 主動 shed(見容量模式);但**零逾時、零 OOM、RSS ≤ ceiling**,**被服務**的請求 **p95 < 4·B**,且 shed 比例需於報告載明(供判斷是否觸發第二階段)。
- 若 C=4/8 無法在不拒絕下達標,或任一 C 出現逾時/OOM/RSS 破表 → 即為「P1 純軟體不足、需進第二階段 time-cube」的觸發證據(§6 decision gate)。

**scenario-BBOX:max bbox 記憶體(G6)**:
- 連續打接近 `POINT_LIMIT`(1M 點)的單日 bbox(含多 fields),**以 HTTP-level RSS 取樣**量測 worker RSS 峰值(非僅 tracemalloc;見下「RSS 為權威 gate」);要求 ≤ `GHRSST_RSS_CEILING_MB`。這是 streaming 序列化是否生效的證據(逐列 dict / columnar 會在此爆掉,doc 00 §4.8)。
- **已知限制(回應 Codex review #2)**:P1-S1 `bbox_batches` 雖 streaming **row dicts**,但目前一次讀入整個 bbox 的 data slice 作為 working set(遠小於舊的 row-list 爆量,且在 POINT_LIMIT 內可接受)。**若此情境 RSS 破表**,需把 `bbox_batches` 改為 row-window / chunk-window 逐窗讀取(而非整片 slice);RSS gate 是此決策的觸發點。

**scenario-OL:overload 降級(G7)**:
- 故意以遠超 `GHRSST_ZARR_WORKERS` 的並發灌入。**pass**:回**快速 `503` + `Retry-After`**(canonical;非逾時/卡死);RSS 不失控;**負載移除後秒級恢復**正常延遲。

**RSS 為權威 gate(回應 Codex review [Medium] #4)**:
- `bench_bbox_memory.py` 的 `tracemalloc` 只證明 **Python 物件**爆量,**不含** numpy/xarray/zarr/解壓/orjson 的**原生緩衝**。
- 故 **G6 的實際驗收以「HTTP-level 真實 worker RSS 取樣」為準**(scenario-BBOX/-LR/-SB 期間每 1 s 取 RSS;`-w` 多 worker 取總和與單 worker 峰值)。tracemalloc bench 僅為佐證與相對比較。

- **驗收**:G1/G1′/G2/G2′/G6/G7 證據齊全(各情境三分位數 + 分窗 + RSS 曲線 + executor 佇列指標 + 上述 pass 條件),在 store 持續增長下穩定;報告須載明 C、T、視窗、混合比例、`GHRSST_*` 設定與部署設定。

### P1-S5 部署設定(VM24,不改 production 服務)
- 文件化在 VM24 以新 port 起 `dev2026/api/app.py`:gunicorn worker 數、`-k uvicorn.workers.UvicornWorker`、與 VM24 核心數/RAM 的對應。
- **資源 sizing 參數 — 全部須有保守預設(回應 Codex review [Low] #5:這些現在是資源/併發安全關鍵,實作前先定保守值)**:

  | 參數 | 預設(保守) | 說明 |
  |---|---|---|
  | `GHRSST_ZARR_WORKERS` | `min(8, CPU)` | 有界 executor 大小。**`-w` × 此值 = 總執行緒**,須 ≤ 核心數,避免超訂 |
  | `GHRSST_RSS_CEILING_MB` | `1024`(暫定,**部署前運維依 RAM÷`-w` 覆寫**) | 每 worker RSS 硬上限;不臆測 VM24 RAM,但給保守 fallback 而非無界 |
  | `GHRSST_MAX_DAYS` | `365` | point range 上限(原 31 解除後) |
  | `GHRSST_POINTS_BATCH_MAX` | `1000` | 單一 batch 最大點數(超過回 413) |
  | `GHRSST_BATCH_CHUNK_FANOUT_MAX` | `64` | 單一 batch 可觸及之不同 1024² chunk 數上限;典型地圖視野點群聚、fan-out 小,64 已寬鬆;超過回 413 並提示縮小範圍/分批 |
  | `GHRSST_LRU_MAX` | `64` | day-group handle LRU 容量(handle 為輕量 metadata 物件) |
  | `GHRSST_OVERLOAD_WAIT_MS` | `2000` | executor 佇列等待超此 → 503 |
  | `GHRSST_OVERLOAD_QUEUE_MAX` | `4 × GHRSST_ZARR_WORKERS` | 佇列深度超此 → 503 |

  這些是**保守起點**,P1-S4 量測後可調;但實作前即生效,確保預設安全。
- **sizing 須一致校驗**:`-w` × `GHRSST_ZARR_WORKERS` 的總並發解壓數 × (單 chunk 4MB × fields) 的記憶體上界要 ≤ `GHRSST_RSS_CEILING_MB`,於文件中以算式呈現(例:`-w 2` × 8 workers × 4MB × 3 fields ≈ 192 MB 並發解壓基線,遠低於 1024 MB ceiling,留給 streaming 批次與 Python overhead)。
- **驗收**:orchestrator 可依文件在 VM24 起新 app,並在所載設定下複現 **G1/G1′/G2/G2′/G6/G7**(非僅最佳化後的最佳值)。

### P1-S6 cutover 路徑(讓真實 traffic 受惠)— 回應 Codex review [High] #1

**現況 NGINX 拓撲(VM24,寫死於 spec 以免文件與現況脫節)**:

```nginx
# 兩個 location 都 proxy 到「同一個」upstream ghrsstapi
location /api/ghrsst      { ... proxy_pass http://ghrsstapi; include conf2.d/aio_cache_proxy.conf; }
location /api/ghrsst/mcp  { ... proxy_pass http://ghrsstapi; include conf2.d/aio_cache_proxy.conf; }
upstream ghrsstapi  { server 127.0.0.1:8035; }   # 舊 ghrsst_app
upstream ghrsst_mcp { server 127.0.0.1:8765; }   # 已定義但「未被任何 location 使用」— 非實際流量路徑
```

**由此確立的事實**:
- `/api/ghrsst`(REST)與 `/api/ghrsst/mcp` **都指向同一個 `ghrsstapi` upstream(8035)**。因此切換 `ghrsstapi` 會**同時**影響 REST 與該 mcp location,**不可只考慮 REST**。
- `upstream ghrsst_mcp`(8765)**在此設定中沒有任何 location 使用**,屬殘留;spec **不得**把它當成一條實際 MCP 流量路徑(Codex 指正)。
- MCP server(`metocean_mcp_server.py:66`)預設打**公開 URL** `https://eco.odb.ntu.edu.tw/api/ghrsst`,經 NGINX 前置 → 切 `ghrsstapi` 後 MCP 自動受惠,**MCP 不需改**;`GHRSST_API_BASE` env 僅供繞過 NGINX 直連。
- 兩個 location 都 `include aio_cache_proxy.conf` → **有 proxy 快取層**。這是 cutover/canary 的關鍵風險:舊回應可能仍在 cache,canary 期間會被快取遮住、看不到新 app 的行為。

**cutover 機制(皆不改 production 程式碼,只動 NGINX conf 與服務啟停)**:
1. **upstream 切換**:`upstream ghrsstapi { server 127.0.0.1:<新app port>; }`(或在區塊內加入新 server 並以 `weight=`/`down` 做 canary 比例)。reverse proxy 層切換,`ghrsst_app.py` 不動。
2. **cache 處理(必做)— 含一個必須先修的 correctness landmine**:

   現況 `aio_cache_proxy.conf`(VM24,節錄):
   ```nginx
   proxy_cache api_proxy;
   proxy_cache_methods GET POST;                 # POST 也會被快取
   proxy_cache_key "$http_host$request_uri";     # key 只看 URI,不含 body
   proxy_cache_valid 200 302 10d;                # 200 快取 10 天
   proxy_no_cache       $request_body_file ...;  # 僅在 body 落地成 temp file 時才生效
   proxy_cache_bypass   $request_body_file $api_cache_bypass;
   add_header X-api-cache $upstream_cache_status; # 可用於驗證 HIT/MISS/BYPASS
   client_body_buffer_size 50k;                   # body < 50k 不落檔
   ```

   **landmine(會回傳錯誤資料,必須在開放 `POST /points` 前修掉)**:
   - `proxy_cache_methods GET POST` + `proxy_cache_key` **只用 `$request_uri`(不含 body)** → `POST /api/ghrsst/points` 的 URI 對所有 body 都一樣 → **不同點集合會撞到同一個 cache key、互相回傳對方的結果**。
   - 現有的 `proxy_no_cache/$request_body_file` 守門**只在 body 落地成 temp file(body > `client_body_buffer_size 50k`)時生效**;16 點 batch ≈ 1 KB 留在記憶體 → `$request_body_file` 為空 → **守門不觸發 → 小 body 被快取** → 撞 key。

   **修法(P1-S6 採雙保險,皆不改 production app 程式碼)**:
   - **(a) 新 app 對 `POST /api/ghrsst/points` 回 `Cache-Control: no-store`**;NGINX 預設會尊重上游 `Cache-Control` 而不快取(本 config 未設 `proxy_ignore_headers`,故有效)。
   - **(b) NGINX 對 `/api/ghrsst/points` 加 location 級停用快取**作為 defense-in-depth(避免單靠上游 header)。NGINX prefix matching 取**最長前綴**,故 `location /api/ghrsst/points` 比既有 `location /api/ghrsst` 更具體,會優先吃到此路徑(不會被既有 location 蓋掉);需確認無其他更長/正則 location 搶先。
     - **必須複製既有 `/api/ghrsst` 的 CORS/OPTIONS 行為**(回應 Codex review):nginx `add_header` 在子 location 有自己的 `add_header` 時**不繼承**父層,故 CORS header 要在此 location 重列;`/points` 是給瀏覽器 batch 呼叫的,preflight `OPTIONS` 必須短路回 204,否則 CORS 會壞。
     - **`add_header X-api-cache BYPASS always;`**(回應 Codex review):因為 `proxy_cache off` 時 `$upstream_cache_status` 為空、原本的 `X-api-cache` 不會出現;補一個靜態 header 讓 probe / canary 的 gate 文字仍可驗證(否則 gate 改為「無 cache header + 無 collision 即通過」,見驗收)。
     ```nginx
     location /api/ghrsst/points {
         # 比 /api/ghrsst 更長前綴 => 此 location 勝出
         # --- CORS:複製自既有 /api/ghrsst,子 location 不繼承父層 add_header ---
         add_header 'Access-Control-Allow-Origin' '*' always;
         add_header 'Access-Control-Allow-Credentials' 'true' always;
         add_header 'Access-Control-Allow-Methods' 'GET, POST, OPTIONS' always;
         add_header 'Access-Control-Allow-Headers' 'DNT,Keep-Alive,User-Agent,X-Requested-With,If-Modified-Since,Cache-Control,Content-Type,Authorization,Range' always;
         if ($request_method = OPTIONS) {        # preflight 短路
             add_header 'Access-Control-Allow-Origin' '*' always;
             add_header 'Access-Control-Allow-Methods' 'GET, POST, OPTIONS' always;
             add_header 'Access-Control-Allow-Headers' 'DNT,Keep-Alive,User-Agent,X-Requested-With,If-Modified-Since,Cache-Control,Content-Type,Authorization,Range' always;
             add_header 'Content-Length' 0;
             return 204;
         }
         # --- 停用快取(defense-in-depth) ---
         proxy_cache off;
         proxy_no_cache 1;
         proxy_cache_bypass 1;
         add_header X-api-cache BYPASS always;   # proxy_cache off 時補靜態 header 供 gate 驗證
         proxy_pass http://ghrsstapi;            # cutover 時與 /api/ghrsst 指向同一新 upstream
         proxy_read_timeout 900;
         client_max_body_size 500m;
         # 注意:此 location 不要 include aio_cache_proxy.conf(那會把 proxy_cache 開回來)
     }
     ```
     > 備註1:既有 `/api/ghrsst` 用 `proxy_set_header` 設 `Access-Control-Allow-Methods/Headers`(其實是設到「往 upstream 的 request header」,對瀏覽器 response 無效);此處改用 `add_header ... always` 才是正確的 response CORS。部署時請與運維確認既有行為是否刻意如此,避免無意改變現況。
     > 備註2(回應 Codex [Low] #5):`Access-Control-Allow-Origin: *` 與 `Access-Control-Allow-Credentials: true` 併用,對**帶 credentials 的瀏覽器請求無效**(瀏覽器會拒)。若 `/points` 的 client **不帶 credentials**,維持 `*` 即可(此時 `Allow-Credentials: true` 其實多餘,可移除);若**會帶 credentials**,須改成**反射 Origin**(`add_header Access-Control-Allow-Origin $http_origin always;` + `Vary: Origin`)而非 `*`。上線前依實際 client 決定。
   - 若未來真要快取 batch,必須把 cache key 改成含 body hash(`$request_body` / 自訂 key);本期**不快取 batch**。

   **canary/驗證期間繞過快取並確認**:對測試流量觸發 `$api_cache_bypass`(或上述 location 設定)。驗證通過條件為下列任一:**(i)** 回應 `X-api-cache` ∈ {`MISS`,`BYPASS`}(GET 路徑或有補靜態 header 的 `/points`);或 **(ii)** `/points` 在 `proxy_cache off` 下**無 `X-api-cache` header,但 POST-3 無 collision**(不同 body 各自回正確結果)。兩者皆代表沒打到舊 cache。
   - full cutover 後 **purge 既有 cache**(避免 10 天 TTL 內混用新舊回應);注意 `proxy_cache_use_stale updating` + `proxy_cache_background_update on` + `proxy_cache_lock on` 會在更新期間先回 stale,purge 時須一併考量。

   **回歸驗證工具**:`dev2026/bench/probe_nginx_cache.py`(用 `X-api-cache` header;POST 測試會在未給 `--date` 時自動從 latest GET 回應推出可用日期,推不出則 skip 並標「NOT VERIFIED」,避免送 `date:null` 撞契約)。
   - 對 production 已**實測**:date-less GET = MISS→HIT → **可被快取**(在 10d TTL 下,下次 ingest 後必然 stale,除非 bypass/purge;見 doc 00 §4.6,非「已觀測到 stale 值」);`POST /points` 目前 404(未部署)。
   - **兩種執行模式**:
     - **預設(寬鬆)**:`/points` 不存在/無日期/inconclusive 只標 risk、**exit 0** —— 適合對現行 production 做現況巡檢。
     - **`--strict`(=`--require-post`,staging/cutover gate)**:`/points` 404/405、無可用日期、非 200、或 POST-3 inconclusive 一律視為 **failure、exit 1** —— 防止 CI/deploy 在「從未跑到 POST-2/3」的情況下綠燈通過(回應 Codex review [High])。
   - **staging 上線新 app 後必以 `--strict` 跑 POST-2/POST-3**:POST-3 對同一 URI 送兩個不同 body(台灣 vs 夏威夷點集),若 body B 被回以 body A 的快取結果即判 **FAIL(collision)**;修法生效後應為各自正確(`X-api-cache` 為 `BYPASS` 或在 `proxy_cache off` 下無此 header 皆可,見上 canary 通過條件)。此腳本(`--strict`)作為 cutover 的 cache go/no-go gate(exit code 0/1)。
3. **rollback**:`upstream ghrsstapi` 改回 `127.0.0.1:8035` 並 reload;一併 purge cache。一鍵、秒級。

**分階段 rollout**:shadow(新 app 起於新 port、不收流量,VM24 本機自跑 bench)→ canary(NGINX 對部分/內部測試流量導向新 app,**且繞過 cache**)→ full(`ghrsstapi` 全切 + cache purge)。

**驗收**:
- spec/部署文件給出**明確的 NGINX upstream 切換片段、cache bypass/purge 步驟、rollback 步驟**。
- 明確聲明此切換**同時改變 `/api/ghrsst` 與 `/api/ghrsst/mcp`**,並驗證兩者皆命中新 app。
- canary:經 `eco.odb.ntu.edu.tw/api/ghrsst` 實打(且確認**未被 cache 命中**),回應可由 header/log 辨識為新 app,且 parity 一致。
- 明定 go/no-go:**G1/G1′/G2/G2′/G6/G7 + parity 全綠**,且 cache 行為已確認。

> **第一階段結束即可滿足使用者主訴**(解除 31 天上限 + 多點不雪崩),**完全不動資料**,且 P1-S6 確保真實 traffic 受惠。是否進第二階段由 P1-S4 的數字(尤其 **scenario-LR / G1′**)決定。

## 6. 第二階段(選配,動資料)— 僅在 P1 數字不足時啟動

**決策閘門(任一成立即觸發)**:
- **scenario-LR / G1′ 未達標**(並發 365 天序列在 VM24 下 p95/p99/RSS 撐不住)—— 這是最可能的觸發點,因為長序列在現結構仍是 N 次 chunk 解壓。
- P1 後 365 天序列單發仍 > 目標、或需求延伸到**多年序列**(point read 4–5 ms/day × 730+ 天 ≈ 3 s+,主要落在 chunk 解壓)。
- 要顯著加速 bbox(則優先 P2 選項 B)。

### P2 選項 A — time-series cube(point 序列最佳化)
- 新建 `mur_ts.zarr`,dims `(time, lat, lon)`,**time 連續分塊 + 空間小 chunk**,使「單點整條序列」落在少數 chunk。
- 為避免全球每日 append 的小檔爆炸,採 **Zarr v3 sharding**:inner chunk(time 連續、空間如 32×32)+ shard(空間如 512×512)。
- 每日 `append_dim='time'` 增量寫入;time chunk 取折衷(如 30–90 天)平衡 append 成本與讀取。
- 與既有每日 group store **並存**:point 序列走 cube,bbox 單日走原 store。
- **驗收**:單點 730 天序列 p50 < 1.5 s;每日 append 不需重寫既有資料;cube vs 原 store parity 一致。

### P2 選項 B — bbox streaming 序列化(若 bbox 成主訴)
- 取代 `read_ghrsst:405-411` 逐列 dict 迴圈,改 **分批 streaming**(每批 encode 後釋放)。
- **不要只做 columnar「向量化」**:doc 00 §4.8 實測 columnar 反而更耗記憶體(429 MB > 396 MB);streaming 才是解(21.6 MB)。
- **驗收**:1M 點 bbox 的 peak Python 配置降至 ~單批等級(實測目標 ~20–30 MB),序列化時間亦改善;RSS 符合 G6。
- 註:此項同時是 P1 的 bbox 記憶體基本要求(P1-S2 已要求 streaming),P2-B 為其進一步最佳化/獨立交付。

> 選項 A/B 各自獨立,可只做其一。chunk 重整有風險(資料量、ingest 改動、回填),故列為選配並以 benchmark 把關。

## 7. 風險與緩解

| 風險 | 緩解 |
|---|---|
| 快取 store handle 與 live append 不同步(新日讀不到) | bounds/day 列舉用 TTL 重掃(沿用 app 機制);group handle LRU 不快取「不存在→存在」的負結果;P1-S1 新日可見性測試 |
| **記憶體尖峰:transient chunk fan-out + 回應序列化(非座標)** | 座標僅 ~0.3 MB 常駐;真正風險是「並發 × 單 chunk 4MB×fields」與大 bbox JSON。緩解:point_series/batch **逐 chunk 讀→取→釋放**、batch fan-out 上限、bbox 向量化/streaming 序列化、**有界 executor 限制同時解壓數**、`GHRSST_RSS_CEILING_MB` 硬上限 + G6 量測(回應 Codex [High] #2) |
| **offload 把瓶頸從 event loop 移到 threadpool 排隊 / OOM** | **有界 executor + 背壓**:過載快速 `503` + `Retry-After`(canonical);G2′ backlog 判定含 executor 佇列;G7 過載測試(回應 Codex [High] #3) |
| **共享 LRU / zarr handle 的並發競態** | LRU get/put/evict 加鎖;handle 唯讀並發;P1-S1 執行緒安全壓力測試(同日/異日/eviction churn);失敗則 thread-local / 每次開 fallback(回應 Codex [Medium] #4) |
| **長序列在多人併發崩潰(主用例)** | scenario-LR / G1′ 專測並發 365 天序列;未過即觸發第二階段 time-cube(§6) |
| 本機數字無法代表 VM24 冷快取/RAM | P1-S5 要求 VM24 完整資料重跑 + 運維填 `GHRSST_RSS_CEILING_MB`;驗收看絕對值、加速倍數與 RSS |
| store 仍在拷貝,bench 數字波動 | 工具每次重掃、報告使用的日數;以中位數/多次量測 |
| 第二階段 rechunk 破壞既有服務 | cube 為**新增並存** store,不改原 store;decision gate 把關 |
| 新舊 API 行為漂移 | P1-S3 parity test 為硬性 gate |

## 8. 給 reviewer 的開放問題(部分已於 v2 修補)

1. ~~point range 解除後的硬上限~~ **已定案(P1-S2):`GHRSST_MAX_DAYS=365`,超過回 413(含 max_days/requested_days/拆分建議),不做 pagination,可設定。**
2. 第二階段是否預先排程,或嚴格以 P1 數字觸發?
3. 新 app 與舊 app 的最終關係:cutover 後**長期取代**舊 `ghrsst_app.py`(deprecate),或**永遠並存**?影響 P1 是否要做到 100% 參數相容。
4. cutover 的 canary 流量來源:用內部測試流量,或對 production 直接小比例導流?go/no-go 由誰簽核?

### 已於 v2 回應第一輪 Codex review 的項目
- [High] cutover 路徑 → 新增 **P1-S6**。
- [High] root handle stale → doc 00 §3.1.1 + §4 契約改為**無長壽 root handle**;P1-S1 加新日可見性測試。
- [Medium] batch contract → P1-S2 定死順序/`index`/重複點/上限/NaN。
- [Medium] tail latency → G2′ + p50/p95/p99;P1-S4/S5 sizing。

### 已於 v3 回應第二輪 Codex review 的項目
- [High] P1-S6 寫死實際 NGINX 拓撲 + **`aio_cache_proxy` cache bypass/purge/rollback**;聲明兩 location 同切;`ghrsst_mcp`(8765)標註為未用殘留、非實際流量路徑。
- [Medium] benchmark ≠ stale-risk 證明 → §3.1.1 改為「只證明無效能收益」,**correctness 由 P1-S1 integration test(worker 暖機後新增一日須掃到)證明**。
- [Medium] batch schema → P1-S2 補**明文 request/response JSON**。
- [Medium] burst 指標 → P1-S4 補 **pass/fail 操作型定義**(C=32、T=120s、10s 滾動視窗、tail 漂移 ≤1.2×、backlog/逾時/5xx 判準)。
- [Low] 內部一致性 → 全文移除殘留「常駐 root group / open_group(root) 一次」敘述。

### 已於 v4 納入實際 `aio_cache_proxy.conf` 後的新增處理
- **(必修 landmine)** `POST /points` 在 `proxy_cache_methods GET POST` + URI-only cache key 下會**撞 key、回傳他人結果** → P1-S6 修法:新 app 回 `Cache-Control: no-store` + NGINX location 級停快取(雙保險);本期不快取 batch。
- **(現存 production bug)** date-less GET 因 `proxy_cache_valid 200 10d` 可被快取 10 天 → 下次 ingest 後必然 stale latest → P1-S2 對 date-less GET 設 `no-store`/極短 max-age(見 v5 修正)。
- canary 用既有 `X-api-cache` header 驗證 MISS/BYPASS;cutover purge 須考量 `use_stale updating` + background update。

### 已於 v6 回應第四輪 Codex review 的項目
- [High] probe 在 POST gate 未驗時仍 exit 0 → 加 **`--strict`**:staging/cutover 下 `/points` 404/405、無日期、非 200、POST-3 inconclusive 皆 exit 1(預設寬鬆模式維持 exit 0 供現況巡檢)。
- [Medium] `/points` location 丟失 CORS/OPTIONS → 片段補**完整 CORS `add_header ... always` + OPTIONS 204 preflight**(註明子 location 不繼承父層 add_header;既有用 `proxy_set_header` 設 CORS 其實無效,部署時與運維確認)。
- [Medium] `proxy_cache off` 下 `X-api-cache` 可能不存在,與 gate 文字衝突 → 片段補 **`add_header X-api-cache BYPASS always`**;canary 通過條件改為「`MISS`/`BYPASS` **或** 無 header + POST-3 無 collision」。
- [Low] probe 輸出仍說「STALE / short-no-cache」→ 改為「cacheable;下次 ingest 後 stale 除非 bypass/purge」+ `no-store`,與 doc 00 一致。

### 已於 v7 回應第五輪 Codex review 的項目(資源/併發系統性)
- [High] 併發 gate 沒測最貴路徑 → 新增 **G1′ + scenario-LR**:並發 365 天序列 C∈{4,8,16},p95/p99/逾時/RSS 有界;未過即觸發第二階段(§6 主要決策點)。
- [High] chunk fan-out 記憶體未規範/測試 → **G6 + RSS 量測**(各情境 + scenario-BBOX);取值「逐 chunk 釋放」、batch fan-out 上限、bbox streaming 序列化;`GHRSST_RSS_CEILING_MB` 硬上限 + sizing 算式。
- [High] offload 需背壓非僅 sizing → **G7 + 有界 executor + 過載 `503` + `Retry-After`**(canonical;v8 定案);G2′ backlog 含 executor 佇列;scenario-OL 測過載與恢復。
- [Medium] LRU/handle 執行緒安全 → §4 加鎖規則 + handle 唯讀並發 + fallback;P1-S1 並發壓力測試(同日/異日/eviction)。
- [Low] CORS `*` + credentials 無效 → P1-S6 備註2:無 credentials 維持 `*`(可移除多餘 `Allow-Credentials`),有則反射 Origin + `Vary: Origin`。

### 已於 v8 回應第六輪 Codex review 的項目(P1 前最後收斂)
- [High] streaming 不可破壞 wire format → §4 bbox 改「**StreamingResponse 維持單一 JSON array**(串 `[`+逗號批次+`]`),NDJSON 僅 opt-in」,明令不改 `/api/ghrsst` 預設格式。
- [High] scenario-LR 缺具體門檻 → 以單發 p95 為 baseline `B` 定:**C=4 p95<2·B、C=8 p95<3·B 且零拒絕;C=16 可 503 shed 但 served p95<4·B、零逾時/OOM、shed 比例載明**。
- [Medium] 背壓 normal vs overload 不清 → §4 加**容量模式**(SLO 內必服務、過載才快速拒絕)+ canonical **`503`+`Retry-After`**(429 留給未來 per-client rate-limit)。
- [Medium] memory bench 只測 Python alloc → 明定 **G6 權威 gate = HTTP-level worker RSS**(P1-S4);`bench_bbox_memory.py` 補 `ru_maxrss`(實測單發 1M bbox 使 process RSS 達 ~1.2 GB,遠高於 tracemalloc 的 396 MB,佐證原生緩衝需 RSS 才量得到)。
- [Low] batch 預設值現在是資源關鍵 → P1-S5 給**保守預設表**:`GHRSST_POINTS_BATCH_MAX=1000`、`GHRSST_BATCH_CHUNK_FANOUT_MAX=64`、`GHRSST_ZARR_WORKERS=min(8,CPU)`、`GHRSST_RSS_CEILING_MB=1024`(運維覆寫)、`GHRSST_OVERLOAD_WAIT_MS=2000` 等。

### 已於 v9 回應第七輪 Codex review 的項目(spec hygiene)
- [Medium] 「位元組級相容」過強 → 改「schema / media-type / JSON-array 相容(非位元組級相同;空白/chunk 邊界/flush 時機可能不同)」。
- [Medium] 過載碼不一致 → **全文統一 `503`+`Retry-After`**(G7 表、P1-S2 測試、§7 風險、§8 v7 條目皆改;`429` 註明保留 per-client rate-limit)。
- [Medium] `/points` 契約仍寫「待 §8 定」→ 直接引用 P1-S5 預設(`MAX=1000`、`FANOUT_MAX=64`),超過回 413。
- [Low] doc 00 未記 RSS 證據 → §4.8 補 `ru_maxrss`(~1.2 GB)+「累積 high-water、非逐路徑、權威 gate 為 HTTP worker RSS」。
- [Low] bench `ru_maxrss` 是累積值 → bench 輸出補 caveat(illustrative,非 per-path 比較)。

### 已於 v5 回應第三輪 Codex review 的項目
- [High] probe `date:null` 撞契約 → probe 改為自動從 latest GET 推日期,推不出則 skip POST-2/3 並標「NOT VERIFIED」(不再送 `date:null`)。
- [Medium] GET-2 措辭 → doc 00 §4.6 / P1-S6 改為「date-less latest **可被快取**;10d TTL 下**下次 ingest 後必然 stale**,除非 bypass/purge」,不宣稱「已觀測 stale 值」。
- [Medium] `/api/ghrsst/points` location → P1-S6 補**明確最長前綴 NGINX 片段**(`proxy_cache off`、不 include cache conf、`proxy_pass` 同新 upstream),並註明 longest-prefix 勝出。
- [Low] default-latest Cache-Control → P1-S2 由 `no-cache` 改 **`no-store`**(無 ETag/Last-Modified 可正確 revalidate);固定日期 GET 維持長快取。
