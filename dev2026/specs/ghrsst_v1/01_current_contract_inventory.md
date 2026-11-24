# 01 — 現行契約盤點（Current Contract Inventory）

> 狀態：**Round 12 — 現況盤點，非規格、未核准。** 「現況」指基線 `6b32e54`（production）；BC-1、BC-2 修補後的差異逐項標明（§3、§6、§7.6、§8.1、§12）。Round 7 新增 owner 2026-11-20 的 VM24 唯讀觀察（§2）；round 8 補充現況與 OD-21 窗口承諾的關係（§4、§7、§8.6）；round 9 補 owner／Codex 2026-11-20 的 point 連續性與 BBox 上限查核（§2），並記錄哪些 handler 細節不再寫進一般 OAS（§4、§5、§7）；round 12 註記 OAS 改用 owner 接受的窗口用詞（§4、§7、§8.6；handler 現況不變）。
> **調查與實作基準：commit `6b32e54`**（VM24 production serving code，證據見 [00 §3](00_baseline_and_provenance.md#3-production-實際-serving-code-的證據vm24-唯讀檢查)；owner 2026-11-20 定為基線）。
> 以下所有 `app.py:N`、`store_access.py:N`、`tests/...:N` 指 `6b32e54` 中 `dev2026/` 下的檔案行號（`git show 6b32e54:dev2026/api/app.py`）。
> Round 4 起本 worktree 已 fast-forward 到 `6b32e54`（00 §7）；Stage C 在 `app.py` 檔尾新增 9 行；BC-2 在 overload handler 之後新增 46 行（422 handler），另改 4 個座標 `Query` 與 `PointsRequest.points`（行數不變）；BC-1 把 `_parse_date` 的 1 行改為 9 行（淨 +8）。因此本機修補後，基線 `:335`–`:341` 的行號 +46、`:342` 之後 +54（例：基線 `:470` 在本機是 `:524`）；本文件行號一律指基線。
> 其他 branch（特別是 `ee84037` 之後）的行為**不**寫成現況；有差異處另行標示。

來源標記：
- **[H]** handler／app 程式（`dev2026/api/app.py`）
- **[S]** store 層（`dev2026/store/*.py`），由 handler 呼叫
- **[F]** FastAPI／Starlette framework 預設行為
- **[D]** 部署設定（VM24 launcher env、gunicorn 參數）
- **[P]** reverse proxy（VM24 NGINX，`/etc/nginx/conf2.d/routes-vm124.conf` → `aio_cache_proxy.conf` → `api_cache_proxy.conf`）
- **[L]** 2026-11-19 對 `https://eco.odb.ntu.edu.tw` 的唯讀實測（約 16:30–16:50 UTC+8）
- **[T]** 測試覆蓋（`dev2026/tests/...` @6b32e54）
- **[O]** owner 於 2026-11-20 對 VM24 的唯讀確認（由 owner 執行並提供，Claude 未連 VM24）

## 1. 對外 surface

| Method | Path | 來源 | 公開? | 說明 |
| --- | --- | --- | --- | --- |
| GET | `/api/ghrsst` | [H] `app.py:432` | 是 | point / range / bbox 共用 |
| POST | `/api/ghrsst/points` | [H] `app.py:641` | 是 | 單日批次 points |
| GET | `/api/swagger/ghrsst` | [F] `app.py:319` `docs_url` | 是 | Swagger UI |
| GET | `/api/swagger/ghrsst/openapi.json` | [F] `app.py:320` | 是 | generated OpenAPI |
| GET | `/healthz` | [H] `app.py:716`（`include_in_schema=False`） | **否** | NGINX 只代理 `/api/ghrsst*`；公開 `https://eco.odb.ntu.edu.tw/healthz` → 404 [L]；僅 VM24 loopback 可用 |
| OPTIONS 等 | `/api/ghrsst*` | [F] | — | 405 `{"detail":"Method Not Allowed"}` [L] |
| GET | `/redoc` | [F] 預設存在 | 否（未被 NGINX 代理） | — |

`/api/ghrsst/mcp` 由同一 NGINX 導向另一個 upstream（metocean MCP），不屬本 API 契約。

## 2. 部署設定（VM24 launcher）與預設值

| 設定 | Code default（`app.py` Cfg `:133-156`） | VM24 值 [D] | 影響 |
| --- | --- | --- | --- |
| `GHRSST_MAX_DAYS` | 366 | 366 | point range 上限 → 413 |
| `GHRSST_POINTS_BATCH_MAX` | 1000 | 未設（1000） | POST points 上限 → 413 |
| `GHRSST_BATCH_CHUNK_FANOUT_MAX` | 64 | 未設（64） | POST points 跨 chunk 上限 → 413 |
| `GHRSST_BBOX_POINT_LIMIT` | 1,000,000 | **300,000** | bbox 格點上限 → **400** |
| `GHRSST_ZARR_WORKERS` | min(8, cpu) | 4 | executor |
| `GHRSST_OVERLOAD_QUEUE_MAX` | 4×workers | 未設（16） | admission limit = 20/worker process（`/healthz` `executor_limit: 20`） |
| `GHRSST_OVERLOAD_WAIT_MS` | 2000 | 未設 | 等待 permit 逾時 → 503 |
| `GHRSST_FIXED_CACHE_SECONDS` | 864000 | 未設（864000） | 固定日期回應 `max-age` |
| `GHRSST_BBOX_STREAM_BATCH` | 50,000 | 未設 | bbox streaming 批次 |
| `GHRSST_CUBE_REFRESH_TTL_SECONDS` | 0 | 300 | 新 delta 日在 ≤300 s 內可見 |
| `GHRSST_ENABLE_COVERAGEJSON` | 0 | **未設（0）** | `format=coveragejson/raster` → 400 |
| `GHRSST_SPATIAL_WINDOW_ENFORCE` | 0 | **1** | bbox / POST 只允許 delta 中的日期 |
| `GHRSST_SPATIAL_WINDOW_DAYS` | 31（`spatial_policy.py:28`） | 未設 | **只出現在 400 payload 的 `spatial_window_days`；不決定實際窗口**（§8.6） |
| gunicorn | — | `-w 2 -k uvicorn.workers.UvicornWorker --timeout 180` | 2 個 worker process；每個有獨立 executor／admission |

`/healthz`（2026-11-19 loopback）：`point_earliest 2023-01-01`、`point_latest 2026-11-17`、`point_day_count 1417`（連續）、
`daily 2026-08-06..2026-11-17`（104 天）、`delta_day_count 75`、`spatial_window ["2026-09-04","2026-11-17"]`、
`cube_base_kind segmented`、`cube_generation 2`、`coveragejson_enabled false`、`spatial_window_enforce true`。

**[O] 2026-11-20 owner 唯讀確認**：production 最新資料日 **2026-11-18**；對同一極小 BBox 逐日查詢 2026-10-19 至 2026-11-18 共 31 天，全部 200 且各有一列；health check 空間窗口 **2026-09-04 至 2026-11-18**。這是 owner 選定資料日期窗口（02 §5.0，OD-21：空間 31 日、單點 366 日）的依據；P5 資料流程能否持續保障尚待確認。

**[O] 2026-11-20 owner／Codex 唯讀查核（round 9 依據；Claude 未連 VM24）**：
- production 最新 point、daily、spatial 日期皆為 **2026-11-18**；日期無關的 point 查詢與極小 BBox 查詢都回 2026-11-18。
- point 日期 2023-01-01..2026-11-18 共 **1,418** 天，等於首尾間的日曆天數（連續）；point GET 查 2023-01-01 回 200 且有 SST 值；查 2025-11-18..2026-11-18 回 366 列、366 個連續日期。
- VM24 的 BBox 格點上限目前 **300,000**（`GHRSST_BBOX_POINT_LIMIT`）；程式預設 1,000,000。OAS 由 `cfg.BBOX_POINT_LIMIT` 讀出，不硬編碼任何一個。
- 上述是**目前狀態**；「2023 起連續」不等於未來保障。366／31 日窗口：2026-11-20 P5 reviewer 已確認為資料涵蓋政策與設計依據，但不是無條件或永久保證（04 OD-21）；本盤點不把它寫成已驗證的保障。

## 3. `GET /api/ghrsst` — 參數

| 參數 | 型別（FastAPI） | 必填 | 預設 | 驗證／行為 | 來源 |
| --- | --- | --- | --- | --- | --- |
| `lon0` | float | 是 | — | 非數字 → 422；基線接受 NaN／±Inf，BC-2（round 6）後非有限值 → 422。有限值**無範圍檢查**（說明文字寫 [-180,180]，但未強制，§8.1） | [F][H] `:435` |
| `lat0` | float | 是 | — | 同上 | `:436` |
| `lon1` | float\|null | 否 | null | 必須與 `lat1` 同時給才進 bbox；BC-2 後非有限值 → 422（即使只給 `lon1`、仍是 point 模式） | `:437` |
| `lat1` | float\|null | 否 | null | 同上 | `:438` |
| `start` | string\|null | 否 | null | `^\d{4}-\d{2}-\d{2}$`，否則 400。基線**不檢查日曆合法性**；BC-1（round 5）後日曆不存在的日期（含結尾換行的 `2025-01-01\n`）也回 400（§7.6）。OpenAPI schema 自 round 6 另加 `maxLength: 10`，使文件與 handler 一致 | `:439`, `:337-342` |
| `end` | string\|null | 否 | null | 同上 | `:440` |
| `append` | string\|null | 否 | `sst` | 逗號分隔；允許 `sst`,`sst_anomaly`,`sea_ice`；空字串 → `sst`；其他 → 400 | `:441`, `:345-353` |
| `sample` | int | 否 | 1 | 非整數 → 422；point 模式 ≠1 → 400；bbox 模式 <1 → 400 | `:442`, `:471`, `:524` |
| `mode` | string\|null | 否 | null | 逗號分隔，大小寫不敏感；只允許 `truncate`，其他 → 400 | `:443`, `:356-363` |
| `format` | string | 否 | `json` | trim + lower；production 只允許 `json`（coveragejson/raster 受 flag 控制；grid/columnar 永不公開）；point 模式非 `json` → 400 | `:444`, `:450-457`, `:473` |

### 3.1 模式判定與驗證順序 [H]
1. `bbox_mode = lon1 與 lat1 皆非 null，且 (lon1,lat1) ≠ (lon0,lat0)`（`:463`）。
   只給 `lon1` 或只給 `lat1` → **靜默當成 point 模式**（不報錯）。
2. 驗證順序：`append` → `mode` → `format`（是否在允許集合）→（point 模式）建立 QuerySnapshot → 無資料 503 →
   `sample≠1` 400 → `format≠json` 400 → 日期解析。先失敗者決定回應。

## 4. Point / range 模式（`app.py:477-521`）

- **無日期**：回傳 point availability 的 `latest`（chronological max of base∪delta∪daily）單一日；`Cache-Control: no-store`。[H][T `test_api.py:83`]
- **只給 start 或只給 end**：視為單日。**start > end**：靜默交換。[H `:485-490`]
- **上限**：以「請求的」天數（clamp 前，含端點）計算，`> MAX_DAYS` → **413**（不截斷、不分頁）。[H `:494-501`][T `test_api.py:106`, `p5s7_http:257`][L 731 天 → 413]
- **Clamp**：再把範圍 clamp 到 point availability `[point_earliest, point_latest]`（全歷史，非 daily staging）。[H `:503-506`][T `p4_point_availability:222`]
- **缺日**：範圍內不存在的日期**靜默略過**；若全部不存在 → 400 `Data not exist for requested period; available range is A/B.`（若存在主連續區段以外的孤立日，改寫 `available contiguous range is A/B`）。[H `:511-514`, `:393-411`][T `test_api.py:114`, `p4_point_availability:229`]
  契約（OD-21，草案）：保證的最新 366 個連續日曆日（以最新 point 日期為終點）內不應缺日。Round 9（owner）：point 目前自 2023-01-01 起連續，一般 OAS 不再寫「缺日略過」；此 handler 行為（含全部不存在 → 400）只保留在本節，handler 本身不變。Round 12（owner 接受的用詞）：OAS 寫「Point queries support the latest 366 consecutive calendar days ending on the latest date available to point queries. The archive currently starts on 2023-01-01.」，不再寫 guaranteed／no date missing（02 §5.0）。
- **排序**：rows 依日期遞增（請求範圍順序）。[S `hybrid_router.py:78-95`]
- **Routing**：cube 覆蓋全部日期 → `cube`；daily 覆蓋全部 → `daily`；否則逐日合併 → `mixed`（不截斷）。只影響 `X-Store-Route`，值須相同（parity tests）。[S `hybrid_router.py:69-76`][T `p4_point_availability:369`]
- **快取**：`e_req < point_latest`（請求上界早於最新日）→ `Cache-Control: public, max-age=864000`；否則 `no-store`。[H `:509`, `:519`][T `test_api.py:91,100`, `p4_point_availability:245`][L]
- **回應**：JSON array（非 streaming），每列 `{lon, lat, date, <fields>}`；lon/lat 是最近格點座標（§8.2）。
  - 請求的欄位在該日**不存在** → **省略該 key**（cube 以 `var_valid`、daily 以 group 內是否有變數判斷）。[S `time_cube.py`/`store_access.py:194-215`][T `test_api.py:121`]
  - 存在但為 NaN（陸地等）→ `null`。
- **Headers**：`Content-Type: application/json`、`Cache-Control`、`X-Store-Route: cube|daily|mixed`。[H `:519-521`]

實測範例 [L]：`?lon0=122.5&lat0=23.5&start=2026-11-15&end=2026-11-16&append=sst,sst_anomaly,sea_ice` → 200、`cache-control: public, max-age=864000`、`x-store-route: cube`：
```json
[{"lon":122.5,"lat":23.5,"date":"2026-11-15","sst":26.621000289916992,"sst_anomaly":-0.07100000232458115,"sea_ice":null},
 {"lon":122.5,"lat":23.5,"date":"2026-11-16","sst":26.156999588012695,"sst_anomaly":-0.32899999618530273,"sea_ice":null}]
```

## 5. BBox 模式（`app.py:523-620`）

- **單日**：`start` 與 `end` 都給且不同 → 400 `BBOX query only allows single-day data; use start=end ...`；只給一個 → 該日；都不給 → **daily store 的 latest**（注意：bbox 的 latest 來自 daily，不是 point latest）。[H `:526-544`][T `test_api.py:157`][L]
  無日期時，handler **先**選定 daily latest，**再**送進 spatial gate；若該日不在 delta，回 400 `SpatialWindowError`，**不會**自動退回較早的可空間查詢日。[H `:542-550`]（目前 daily latest = delta latest，2026-11-19 為 2026-11-17、2026-11-20 為 2026-11-18，故實測未出現；delta append 落後 daily 時可達。）
  OAS（round 9）：Bounding-Box Query 的 Dates 寫「Give `start` or `end` alone, or both with the same date」與無日期時 daily latest＋不回退。
- **空間窗口 gate**（enforce=1 時）：日期不在 delta → 400 dict payload（§8.6）；在 daily 可用性檢查**之前**執行。[H `:550`, `:414-428`][T `p4s3gate:88`, `p4_point_availability:257,272`][L]
- **daily 可用性**：不在 daily → 400 `BBOX query only allows single-day data. Requested D is unavailable; available range is A/B.`（enforce=1 時通常先被 gate 擋下；transition 配置下可達）。[H `:551-553`][T `p4_point_availability:455`]
- **大小上限**：`ni × nj > BBOX_POINT_LIMIT` → **400** `bbox too large (N > L); increase stride or shrink bbox`。[S `store_access.py:319-323`][H `:566-568`][L：40°×40° → `16008001 > 300000`]
- **sample**：stride N，lon 與 lat 兩軸各取每第 N 個格點。[S `store_access.py:326-337`][T `test_api.py:149`]
- **快取**：有明確日期且 `< daily latest` → `public, max-age=864000`；無日期或等於 latest → `no-store`。[H `:554`]
- **回應（`format=json`，production 唯一可用）**：**StreamingResponse**，仍是單一 JSON array（非 NDJSON）；無 `Content-Length`。
  順序：**緯度遞增（南→北）為外層、經度遞增為內層**（row-major）。[S `store_access.py:341-363`][L]
  每列包含所有請求欄位；欄位不存在 → `null`（與 point 模式「省略」不同）；NaN → `null`。[T `test_api.py:141`]
- **Headers**：`Cache-Control`、`X-Served-Rows`（= 格點數）、`X-Stride`、`X-Bbox-Format: json`、`X-Read-Ms`（讀取耗時，ms）。[H `:615-620`][L]
- **Admission**：整個 stream 持有 1 個 permit；取得 permit 前逾時 → 503（尚未送出任何 bytes）。[H `:556-571`][T `test_api.py:202,213,229`]
- **Compact 格式**（`coveragejson` / `raster` alias）：程式存在（buffered Response、`X-Encode-Ms`、`X-Bbox-Format: coveragejson`），但 **production 關閉**，請求回 400 `Unsupported format 'coveragejson'. Allowed: json`。[H `:575-591`][T `p4s2:145`][L]

## 6. `POST /api/ghrsst/points`（`app.py:624-673`）

Request body（`PointsRequest`，pydantic）：

| 欄位 | 型別 | 必填 | 驗證 |
| --- | --- | --- | --- |
| `date` | string | 是 | `YYYY-MM-DD` pattern，否則 400；缺少 → 422 |
| `points` | `List[List[float]]`（BC-2 後為 `List[List[FiniteFloat]]`） | 是 | 非數字 → 422；BC-2 後非有限值（含 JSON 字面值 `NaN`／`Infinity`、溢位的 `1e400`）→ 422；空 list → 400；`len > 1000` → 413 dict；任一點長度 ≠2 → 400 |
| `append` | string\|null | 否 | 同 GET |
| `mode` | string\|null | 否 | 同 GET（`truncate`） |

框架驗證（422，含 BC-2 的非有限值）在下列 handler 檢查**之前**執行。處理順序：fields → modes → date 格式 → 空 list → 數量上限 → 每點長度 → **空間窗口 gate** → daily 可用性 → 讀取。

- **Fan-out**：同一請求涵蓋的 daily chunk 數 > 64 → **413**，`detail` 為**字串** `batch spans N chunks > fan-out limit 64; shrink/split the request`（以字串比對判定 413）。[S `store_access.py:254-258`][H `:666-671`]
- **回應**：JSON array，**與輸入順序相同**，重複點保留；每列 `{index, lon, lat, date, <fields>}`；欄位不存在 → `null`；NaN → `null`。`Cache-Control: no-store`。[S][T `test_api.py:168`][L]
- 座標超出範圍同 GET 規則被 clamp（§8.1）。

實測 [L]（以 query string 避開 proxy 快取，見 §10.3）：
```json
[{"index":0,"lon":122.5,"lat":23.5,"date":"2026-11-15","sst":26.621000289916992,"sea_ice":null},
 {"index":1,"lon":122.5,"lat":23.5,"date":"2026-11-15","sst":26.621000289916992,"sea_ice":null},
 {"index":2,"lon":-69.0,"lat":32.0,"date":"2026-11-15","sst":25.138999938964844,"sea_ice":null}]
```

## 7. 日期規則摘要

1. 格式：只接受 `YYYY-MM-DD`（regex）。時區／時刻無；日期是 MUR 的產品日。
2. 端點：point range **含兩端**；bbox／POST 單日。
3. `latest`：point = base∪delta∪daily 的 chronological max；bbox 無日期 = daily latest；spatial = delta latest。三者目前相同（2026-11-20 查核皆為 2026-11-18），但定義不同。OD-21 的兩個窗口各以自己的最新可用日期為終點（point：最新 point 日期；spatial：最新 spatial-query 可用日期）。Round 9（owner）：OAS 不再對一般使用者寫「兩者可能不同」；無日期 BBox 取 daily latest 後再做空間窗口檢查、不回退，這點仍照實寫在 OAS。
4. 可用期間：point/range = 全歷史（2026-11-20 查核：2023-01-01..2026-11-18，1,418 天連續；OAS 寫成「currently」的目前覆蓋範圍）；spatial（bbox、POST）= delta 成員（目前 2026-09-04..2026-11-17，75 天）。
5. 缺日：point range 靜默略過（handler 行為，round 9 起不寫進一般 OAS）；bbox/POST 回 400。契約（OD-21，草案）下，point 最新 366 日、spatial 最新 31 日內不應缺日，缺日只可能出現在保證窗口以外的較早日期。Round 12 起 OAS 只以「consecutive calendar days」表達窗口，不再另寫缺日或較早日期的補充（02 §5.0）。
6. **日曆不存在但格式正確的日期**（如 `2025-02-30`；同類還有 `2025-13-01`、`0000-01-01`、結尾帶換行的 `2025-01-01\n`）：
   - **基線 `6b32e54`（production 現況）**：point/range 模式 → **500 `text/plain` `Internal Server Error`**（`datetime.strptime` 在 `:494` 拋出未處理的 ValueError）[H][L 已重現]；
     bbox／POST → 400（enforce=1 為 spatial gate payload；enforce=0 為「unavailable／not available」字串）。
   - **BC-1 修補後（round 5，本機，未部署）**：所有路徑 → 400 `{"detail":"Invalid date format. Use YYYY-MM-DD."}`；bbox／POST 的 status 不變、body 改變。路徑對照見 [05](05_behavior_change_proposals.md#bc-1-影響的路徑round-5-實測本機-synthetic-tiered-storeenforce1-與-enforce0-各一次)。
7. 資料新鮮度：TTL refresh 300 s；新日出現延遲 ≤ 300 s + cron 時間（內部時程，**不是** API 承諾）。OAS（round 9，owner）只寫「The newest product date typically lags 1–2 days behind today, depending on upstream GHRSST data availability.」

## 8. 空間規則

1. **座標無範圍驗證**：所有座標 clamp 到格網範圍後取最近格點；`lon0=200` → 回傳 `lon: 180.0`、200 OK。[S `store_access.py:172-178`][L]
   `nan`/`inf` 會被 pydantic 接受為 float。Round 4 以本機 synthetic store（TestClient，非 production）實測：
   `inf`/`-inf` 依 clamp 規則落在格網邊緣；**`nan` 也回 200，並回傳格網邊緣的格點**（`lon0=nan` → `lon` = 格網最大經度；bbox 與 POST 同樣回 200）。
   **BC-2 修補後（round 6，選項 B2-b，本機，未部署）**：GET 四個座標與 POST `points` 中的 NaN、Infinity、-Infinity（含溢位成 inf 的 `1e400`）→ **422 `HTTPValidationError`**（`finite_number`）；
   422 body 中回顯的非有限 float 寫成 `"NaN"`／`"Infinity"`／`"-Infinity"`，一般 422 不變。**有限**超界座標仍為 200 並 clamp。上述基線行為（200＋邊緣格點）只描述 `6b32e54`。前後對照見 [05](05_behavior_change_proposals.md)。
2. **最近格點**：回傳的 `lon`/`lat` 是格點座標（float32 轉 float64，故可能顯示 `121.01000213623047`）；`mode=truncate` 時座標 round 5 位、數值 round 3 位。[H `:366-376`]
3. **BBox 角點**：`lon0/lon1`、`lat0/lat1` 取 min/max，順序不拘。**不支援跨換日線**：`lon0=170, lon1=-170` 會被解讀為 -170..170。[S `store_access.py:310-315`]
4. 取樣：`sample` stride，見 §5。
5. 上限：`BBOX_POINT_LIMIT`（prod 300,000）以 stride 後格點數計算。
6. **空間窗口**：允許條件 = 日期 ∈ delta cube 的 days（不是「最近 31 天」）。400 payload：
   ```json
   {"detail":{"error":"spatial query outside the available window","requested_day":"2024-06-01",
     "spatial_window_days":31,"available_spatial_window":"2026-09-04..2026-11-17",
     "hint":"bbox / POST points are limited to the recent spatial window (2026-09-04..2026-11-17); use point time-series / single-day point for older days"}}
   ```
   **語意需澄清（非程式矛盾）**：原始碼把 `SPATIAL_WINDOW_DAYS = 31` 定義為讓 delta 保持約 31 天深的 **retention target**
   （`spatial_policy.py` module docstring 與 `:27-28`），payload 的 `spatial_window_days` 回報的就是這個目標值；
   而目前 delta 實際保留 75 天（`/healthz` `delta_day_count: 75`）。欄位名稱容易被讀成「實際窗口天數」，需要在文件中澄清（OD-7）。[L][`spatial_policy.py:42-51`]
   `available_spatial_window` 只呈現 delta 的**最早與最晚日**（`spatial_window_bounds` 取 sorted 的首尾，`:31-34`）；實際允許條件是**逐日** delta 成員資格（`:37-39`），
   基線程式不檢查區間內是否有缺日。契約（OD-21，草案）：以最晚日為終點的最新 31 個連續日曆日必須完整可用；若回報區間涵蓋更早日期，只有那些更早日期可能有缺口。Round 12 起 OAS 的 `available_spatial_window` 說明只寫欄位格式，不再附此補充（02 §5.2）。
   格式：`available_spatial_window` 為 `"A..B"`，而 point 的 400 訊息用 `"A/B"`（斜線）——格式不一致。

## 9. 查詢限制與容量

| 限制 | 值（prod） | 超過時 | Body 形態 |
| --- | --- | --- | --- |
| Point range 天數 | 366（含端點） | 413 | `{"detail":{"error":"requested range too large","max_days":366,"requested_days":N,"hint":"split into ranges of <= 366 days"}}` |
| POST points 數 | 1000 | 413 | `{"detail":{"error":"too many points","max_points":1000,"requested_points":N,"hint":"split into batches of <= 1000 points"}}` |
| POST chunk fan-out | 64 | 413 | `{"detail":"batch spans N chunks > fan-out limit 64; shrink/split the request"}` |
| BBox 格點 | 300,000 | **400** | `{"detail":"bbox too large (N > 300000); increase stride or shrink bbox"}` |
| Admission（每 worker process） | 20 permits，等待 2 s | 503 + `Retry-After: 1` | `{"detail":"server overloaded; retry later"}` |
| Gunicorn timeout | 180 s | worker 被殺（proxy 端可能 502/504，未實測） | — |
| Rate limit | `nginx.conf` 定義 `limit_req_zone ... rate=50r/s`，**是否套用於 `/api/ghrsst` 未驗證** | — | — |

注意：同屬「資源太大」，bbox 用 400，其他用 413（OD-6）。

## 10. 快取

### 10.1 App 層 [H]
| 回應 | `Cache-Control` |
| --- | --- |
| point 無日期；range 上界 ≥ latest | `no-store` |
| point range 上界 < latest | `public, max-age=864000` |
| bbox 無日期或 = daily latest | `no-store` |
| bbox 明確日期 < daily latest | `public, max-age=864000` |
| POST points（成功） | `no-store` |
| 錯誤（4xx/5xx） | **無**（503 overload 另有 `Retry-After: 1`） |

`ee84037`（未部署）會把 864000 改為 259200；**不是現況**。

### 10.2 Proxy 層 [P]（`api_cache_proxy.conf`）
> 以下是 round 1 唯讀檢查時的設定摘要（**修改前**）。2026-11-20 owner 已修改並 reload 這份設定（`/etc/nginx/conf2.d/api_cache_proxy.conf`），修改後的內容未在本文件記錄；見 §10.4。

- `proxy_cache_methods GET POST;` `proxy_cache_key "$http_host$request_uri";`（**POST body 不在 key 中**）
- `proxy_cache_valid 200 302 10d; proxy_cache_valid 400 404 1m;`（upstream 有 `Cache-Control` 時以其為準；錯誤回應沒有 `Cache-Control`，故 400/404 依 1 分鐘規則快取）
- `proxy_cache_use_stale error timeout updating http_500 ... ; proxy_cache_background_update on; proxy_cache_revalidate on; proxy_cache_lock on;`
- `proxy_no_cache $request_body_file;`（只有 body 被寫入暫存檔時——大於 `client_body_buffer_size 50k`——才不快取）
- `proxy_cache_bypass $request_body_file $api_cache_bypass;`，`$api_cache_bypass`：GET 帶 `Cache-Control` request header 時 bypass；**POST 永不 bypass**。
- `add_header X-api-cache $upstream_cache_status;`（無 `always`，所以只出現在 2xx/3xx 回應）
- 另有 `Vary: Accept-Encoding`、`Server: nginx/1.30.2`（gzip 設定）。

### 10.3 實測觀察：正常 POST 請求揭露 proxy 快取設定的回應錯置（2026-11-19）
送出窗口外日期而得到 400，是正常、正確的 API 行為。以下記錄的是其後觀察到的**回應錯置**：

| 時間（UTC+8） | 請求 | 公開端點結果 | 對照 |
| --- | --- | --- | --- |
| 16:41:48 | POST `/api/ghrsst/points`，`date=2020-01-01`（窗口外） | 400，`requested_day: 2020-01-01`（正確） | — |
| 16:43:28、16:43:40 | 同一 URL，`date=2026-11-15`（窗口內） | 400，**仍是 `requested_day: 2020-01-01` 的 body** | 同一 body 經 VM24 loopback（繞過 NGINX）→ 200；URL 加 `?_probe=<ts>`（不同 cache key）→ 200 |

可確認的事實：
- 在上述時間點，不同 body 的 POST 從公開端點收到相同的舊 400，而 upstream 對同一 body 回 200。
- 設定上 `proxy_cache_methods` 含 POST、`proxy_cache_key` 不含 body（§10.2），與觀察一致。
- 第二次請求距第一次約 100 秒，已超過 `proxy_cache_valid 400 1m`，仍收到舊 body。

尚未證實（需要隔離重現與 NGINX cache 更新流程的證據，例如在非 production 的 NGINX 上重現）：
- 錯置會持續多久。可能相關的設定是 `proxy_cache_use_stale ... updating` + `proxy_cache_background_update on`，以及成功回應為 `no-store`；但「過期 entry 是否會一直被當成 stale 回傳」本輪沒有證據。
- 錯置是否影響所有小型 POST。
- 補充：`proxy_cache_path ... inactive=600m` 是「**未被存取**超過 600 分鐘即移除」，不是固定的到期倒數；entry 持續被存取時不會因 `inactive` 而移除。

其他：
- NGINX access log（`access.log`、`access.log.1`，約 4 天）中 `POST /api/ghrsst/points` 只有本調查的 4 筆。
- 本調查沒有清除快取或修改 NGINX。是否清除特定 entry、是否修設定、何時處理，皆為獨立的 ops 待辦（OD-13），**不阻擋**本機文件與實作工作；但正式對外宣告 POST 契約前，需確認公開端點的正確性（[03](03_roadmap_and_validation.md) Gate F2 前置）。

### 10.4 Ops-1 公開 POST spot check（2026-11-20）

**修改前（Claude 依 owner 授權送出；未改設定、未清快取）**：同型態錯置仍可重現——第三筆有效請求收到第二筆日期的舊 400。前置查詢：不帶日期的小範圍 bbox GET 回 200，資料日期 2026-11-19。

| 時間（UTC） | 送出的 body（同一路徑，無 query string／cache-buster） | 結果 |
| --- | --- | --- |
| 13:06:00 | `{"date":"2026-11-19","points":[[122.0,22.0]]}` | 200，1 列，`index` 0、`date` 2026-11-19（正確） |
| 13:06:00 | `{"date":"2024-01-15","points":[[122.0,22.0]]}`（窗口外） | 400，`requested_day` 2024-01-15（正確） |
| 13:06:00 | `{"date":"2026-11-19","points":[[130.0,25.0],[-69.0,32.0]]}` | **400，仍是 `requested_day` 2024-01-15 的 body**（錯置；預期 200） |

**設定修改（owner 執行）**：owner 檢視 VM24 的 `/etc/nginx/conf2.d/api_cache_proxy.conf` 後修改並 reload NGINX。Claude 與 Codex 都沒有連 VM24、讀取或修改設定、清快取或 reload。

**修改後（owner 提供的結果）**：對公開 `https://eco.odb.ntu.edu.tw/api/ghrsst/points` 送出三筆同一路徑的 POST，序列通過：

| 時間（UTC） | 送出的 body（同一路徑，無 query string／cache-buster） | 結果 |
| --- | --- | --- |
| 15:09:11 | 有效日期 `2026-11-19`，1 點 | 200，1 列，內容符合請求 |
| 15:09:17 | 窗口外日期 `2024-01-15` | 400，`requested_day` 2024-01-15（正確） |
| 15:09:22 | 有效日期 `2026-11-19`，不同的 2 點 body | 200，2 列，`index` 0、1，日期符合請求 |

結論：**公開 API spot check 通過**——修改前同型態的第三筆收到第二筆日期的舊 400，修改後第三筆回 200 且內容符合自己的 body。界線：此 spot check 只證明修改後的這個請求序列，公開 POST 回應與各自的 body 一致；**未測 GET cache 行為**，也沒有驗證其他快取情境（例如間隔超過 1 分鐘、多輪交替、繞過 NGINX 的直連對照）。修改內容（diff）未提供給 Claude，本紀錄不推論採用了哪一個候選修法。

**根因（修改前設定）**：Codex 先前唯讀檢查修改前的設定（§10.2），確認三項設定缺陷：POST 可被快取（`proxy_cache_methods GET POST`）、cache key 不含 body（`$http_host$request_uri`）、400 會快取 1 分鐘（`proxy_cache_valid 400 404 1m`）。這組設定足以解釋本次錯置——同一路徑、不同 body 的 POST 在 1 分鐘內收到前一筆的 400——與觀察到的錯誤回應相符，且與 handler 行為無關。**未檢視**：owner 修改後的設定 diff；**未驗證**：GET cache 行為與其他快取情境；§10.3 中超過 1 分鐘仍收到舊 body 的持續機制也未另行證實。

## 11. CORS 與 headers 由誰提供

- `/api/ghrsst` 回應**沒有任何 `Access-Control-*` header**（帶 `Origin` 的 GET 實測無 `Access-Control-Allow-Origin`）[L]。
  原因（[P] 推論）：location 內經 include 的 `add_header X-api-cache` 使 server 層的 CORS `add_header` 不被繼承。App 本身沒有 CORS middleware。[H]
- Preflight `OPTIONS` → 405（FastAPI）[L]。→ 跨網域瀏覽器 JavaScript 無法直接呼叫（OD-12）。

| Header | 提供者 |
| --- | --- |
| `Content-Type: application/json` | [H]（500 為 `text/plain; charset=utf-8`，[F]） |
| `Cache-Control` | [H] |
| `X-Store-Route`、`X-Served-Rows`、`X-Stride`、`X-Bbox-Format`、`X-Read-Ms`、`X-Encode-Ms` | [H] |
| `Retry-After` | [H]（503 overload） |
| `X-API-Cache`、`Vary`、`Server`、HTTP/2 | [P] |

## 12. 錯誤回應總表

| Status | 觸發 | `detail` 形態 | 位置 |
| --- | --- | --- | --- |
| 400 | 日期格式錯；BC-1 後也包含日曆不存在的日期（所有路徑） | string `Invalid date format. Use YYYY-MM-DD.` | `:342` |
| 400 | 欄位不支援 | string `Unsupported field(s): X. Allowed: sst,sst_anomaly,sea_ice` | `:351` |
| 400 | mode 不支援 | string `Unsupported mode(s): X. Allowed: truncate` | `:362` |
| 400 | format 不允許 | string `Unsupported format 'X'. Allowed: json` | `:457` |
| 400 | point 模式帶 sample≠1 / format≠json | string | `:472`, `:474` |
| 400 | point range 無任何可用日 | string `Data not exist for requested period; available range is A/B.` | `:513` |
| 400 | bbox sample<1；bbox start≠end | string | `:525`, `:530` |
| 400 | spatial window gate（bbox／POST） | **object**（§8.6） | `:428` |
| 400 | bbox／POST 日期不在 daily | string | `:552`, `:665` |
| 400 | bbox 太大；`day not available`（race） | string（ValueError 轉換） | `:568` |
| 400 | POST points 空 list／點長度≠2 | string | `:649`, `:658` |
| 413 | point range 過長 | **object** | `:497` |
| 413 | POST points 過多 | **object** | `:651` |
| 413 | POST fan-out | string | `:671` |
| 422 | 型別／必填（FastAPI） | `HTTPValidationError`：`detail` 為 `ValidationError` 陣列（必填 `loc`/`msg`/`type`，選填 `input`/`ctx`）；GET 與 POST 都引用 `#/components/schemas/HTTPValidationError`（generated OpenAPI，§14；完整結構見 [02 §4](02_openapi_v1_spec.md#4-錯誤-schemascontract)）。實測：`{"detail":[{"type":"missing","loc":["query","lat0"],"msg":"Field required","input":null}]}` | [F][L] |
| 422 | 非有限座標（BC-2，round 6；基線為 200） | 同上 `HTTPValidationError`（`type: finite_number`）；回顯的非有限 float 寫成 `"NaN"`／`"Infinity"`／`"-Infinity"`（基線中缺 `date` 且 body 含 NaN 等情況會 500，round 6 已一併修正） | [F][H] |
| 405 | 不支援的 method | string | [F] |
| 503 | point 模式完全無可用日（GET） | string `No available dates.`；**無** `Retry-After` | `:470` |
| 503 | bbox 時 daily 無任何日（GET） | string `No available dates for spatial queries.`；**無** `Retry-After` | `:541` |
| 503 | admission 逾時（GET 與 POST） | string `server overloaded; retry later` + **`Retry-After: 1`** | `:329-333` |
| 500 | point 模式日曆不存在日期（**僅基線**；BC-1 後為 400）；cube snapshot 無法建立（`TierCompositionError`/`SnapshotError`，程式推論，未重現）；其他未處理例外 | `text/plain` `Internal Server Error` | `:494`, `:467` |

`detail` 有時是 string、有時是 object（400 與 413 都混用）——v1 schema 必須以 `oneOf` 描述，或由 owner 決定是否統一（OD-6）。

503 證據強度：三種 503 皆**依程式碼**核實（`HTTPException(503, ...)` 未設 headers；overload 由 exception handler 設定 `Retry-After`）；
本調查**未實測**到任何 503，現有測試也未在 HTTP 層斷言 503 與 `Retry-After`（`test_api.py:229` 只斷言 executor 拋出 `Overloaded`）。
（後續註記：以上為 round 1–3 盤點當時的狀態。Stage D 之後已新增本機 HTTP 層測試——FastAPI TestClient 搭配模擬資料——覆蓋三種 503 與 POST 無資料日期回 400，見 02 §5.3.1、03 Stage E 的 E3；production 仍未實測。）
POST 沒有「無可用日期」的 503（日期不可用回 400，`:663-665`），只有 overload 503。

## 13. 與舊 Draft `specs/ghrsst_api_v0_1_0.spec` 的矛盾

| 項目 | 舊 Draft（v0.1.0，描述 `ghrsst_app.py`） | 現行（`6b32e54`） |
| --- | --- | --- |
| 實作 | `ghrsst_app.py` | `dev2026/api/app.py` |
| Range 上限 | 31 天、超過則**截斷**為 31 天 | 366 天、超過則 **413** |
| BBox start≠end | 使用 `start` | 400 |
| 超過 bbox 上限的訊息 | `Too many points (N)...` | `bbox too large (N > L)...` |
| `POINT_LIMIT` 預設 | 1,000,000 | code 1,000,000、prod 300,000（env 名稱改為 `GHRSST_BBOX_POINT_LIMIT`） |
| POST `/api/ghrsst/points` | 不存在 | 存在 |
| 空間窗口 | 無 | bbox／POST 限 delta 窗口 |
| 資料來源 | `data/mur.zarr` + `latest.json` | tiered time-cube（segmented base + delta）+ daily staging |
| 413 / 503 overload / Retry-After | 無 | 有 |
| `X-Store-Route` 等 headers | 只有 `X-Stride`、`X-Served-Rows` | 見 §11 |

結論：舊 Draft **不可**作為 v1 基礎，也不應繼續被稱為 authoritative（處置見 [02 §9](02_openapi_v1_spec.md)）。

## 14. Generated OpenAPI 檢查（已驗證）

以 `6b32e54` 快照在隔離 env（`env -i`、無 `GHRSST_*`、只 import 不啟動 lifespan）產生；import 不觸發 store I/O（store 在 lifespan 開啟）。結果與 production `openapi.json` 完全相同：

- `openapi: 3.1.0`、`info.version: "0.4.1"`——**已驗證**。
- 兩個操作的 `200` schema 皆為 `{}`——**已驗證**。
- 只記錄 `200` 與 `422`；**400／413／503／500 完全沒有記錄**——**已驗證**。
- 無 response headers、無 tags、無 `servers`、無 `contact`／`license`／`termsOfService`。
- operationId 為自動產生（`read_ghrsst_api_ghrsst_get`、`read_points_api_ghrsst_points_post`）。（基線的 generated OpenAPI。Round 10 的 v1 草案依 owner 決定改為 `getGhrsstData`／`postGhrsstPoints`，並加入 `servers` 與操作層 `security: []`，見 02 §2.1）
- 舊 `ghrsst_app.py`（legacy）的 OpenAPI 自行寫入 `servers: [{url: https://eco.odb.ntu.edu.tw}]`；dev2026 `app.py` 基線沒有 `servers`。
- `PointsRequest` 無 `minItems`／`maxItems`／點長度限制、`date` 無 pattern。
- 範例：POST 範例日期 `2026-05-31` **已在空間窗口外**，照範例送出會得到 400；`format` 說明提及 coveragejson/raster，但 production 不接受。
- `info.description` 的「Point/range mode supports at most 366 inclusive days」與「data starts at 2023-01-01」與現況一致。（基線的 generated OpenAPI。Round 7 的 v1 草案改寫 `info.description`，不再寫這兩句；366 天 request cap 移到 GET 說明與 413，起始日不再寫成承諾，見 02 §5.0。Round 9 在 GET Point Query 的 Coverage 以「currently」寫回 2023-01-01，標明是目前覆蓋範圍）

## 15. 測試（`6b32e54` 快照）

在 scratchpad 解出的 `6b32e54` 快照上、以隔離 env 與 `TMPDIR` 執行（使用 synthetic tempfile fixtures，未觸及任何真實資料或 VM24）：

```text
python -m unittest dev2026.tests.test_api dev2026.tests.test_phase2_p3s0 dev2026.tests.test_phase2_p4s2 \
  dev2026.tests.test_phase2_p4s3 dev2026.tests.test_phase2_p4s3gate \
  dev2026.tests.test_phase2_p4_point_availability dev2026.tests.test_phase2_p5s7_http
Ran 71 tests in 11.779s — OK   （另有 StarletteDeprecationWarning：httpx → httpx2）
```

這證明「快照與其測試一致」，**不是**對 v1 的驗證。
Round 4：同一組 7 個模組在本 worktree（`6b32e54`，未修改）上重跑 71 tests OK；Stage C／D 之後的結果見 [03](03_roadmap_and_validation.md#stage-d--schema-regression-tests-與相關-api-tests)。
下列項目在 round 3 時未被現有測試覆蓋；除「跨換日線」只驗證了「不 wrap」以外，Round 4 已由 `dev2026/tests/test_api_v1_contract.py` 釘住現況（其中日曆不存在日期在 round 5 改為測試 BC-1 的 400）：日曆不存在日期、座標 clamp、跨換日線 bbox、POST fan-out 413、`spatial_window_days`（retention target）與實際窗口天數的差異、HTTP 層的 503 body 與 `Retry-After`（含「無可用日 503 不帶 `Retry-After`」）、OpenAPI 文件內容本身。
