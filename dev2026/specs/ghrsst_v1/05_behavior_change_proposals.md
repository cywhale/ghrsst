# 05 — 行為變更提案（Behavior Change Proposals）

> **目前狀態（截至 2026-11-24；G-C/D review 於 2026-11-20）**：
> - **BC-1（OD-4）：owner 已核准，已實作（round 5，本機，未 commit）。**
> - **BC-2（OD-5）：owner 已核准選項 B2-b，已實作（round 6，本機，未 commit）。**
> - 兩者已隨 G-C/D 經 Codex 正式 review 並通過（2026-11-20；Codex 明確檢查這兩項核准的行為變更）。尚未部署：production（`6b32e54`）不含這兩項（X2，Stage G）。G-B 已由 Codex review 通過；F2 已由 owner 核准（2026-11-24），兩者屬於已核准的 OpenAPI 契約 v1.0.0 候選；尚未 commit、尚未部署。
>
> 歷史狀態（round 10，2026-11-20，保留原文）：行為未變，round 7–10 只同步 OpenAPI 說明文字與 metadata 的記錄；BC-1 已實作（round 5），BC-2 已實作（round 6）；兩者當時皆待 Codex review。
>
> 規則（owner 2026-11-20）：需要行為變更才能修正明確 bug、消除契約歧義，或避免公開契約描述不可靠時，先交短提案；owner 回覆前不實作。
> 兩項都會修改 `dev2026/api/app.py` 的非 metadata 部分，依 OD-15 需 owner 同意，並由 owner 通知 P5。

## BC-1 — 日曆不存在的日期在 point 模式回 500（OD-4）— 已實作

| 項目 | 內容 |
| --- | --- |
| 問題 | 格式正確但不存在的日期（如 `2025-02-30`）在 GET point／range 模式回 **500 `text/plain` `Internal Server Error`**。同一個日期在 POST 回 400（`day 2025-02-30 not available; ...`），在 bbox（enforce=1）回 400 `SpatialWindowError`。同一個輸入錯誤，依路徑不同得到三種回應 |
| 證據（基線） | `_parse_date`（`6b32e54` `app.py:337-342`）只檢查 regex；point 模式在 `app.py:494` 的 `datetime.strptime` 拋出未處理的 `ValueError`（`_daterange`，`:380-381`，也會）。2026-11-19 在公開 API 重現（01 §7.6）；round 4／5 在本機 synthetic store 重現 |
| 核准的作法 | 選項 B：在 `_parse_date` 同時檢查日曆合法性，失敗時回 400 `{"detail":"Invalid date format. Use YYYY-MM-DD."}`（沿用現有訊息與 `ErrorMessage` schema，不新增錯誤格式） |
| 實作 | `dev2026/api/app.py` 的 `_parse_date`：regex 通過後再做 `datetime.strptime(s, "%Y-%m-%d")`；失敗就落到原本的 400。只改這一個函式（+7 行），其他程式不變 |

### BC-1 影響的路徑（round 5 實測，本機 synthetic tiered store，enforce=1 與 enforce=0 各一次）

`_parse_date` 是 GET（`start`、`end`，point 與 bbox 兩種模式）與 POST（`date`）共用的日期解析器，因此四條路徑都受影響：

| 路徑 | 基線 `6b32e54` | 修補後 |
| --- | --- | --- |
| GET point（`start` 或 `end` 單獨） | 500 `text/plain` | 400 `Invalid date format. Use YYYY-MM-DD.` |
| GET range（`start` 或 `end` 任一不存在） | 500 `text/plain` | 400 同上 |
| GET bbox，enforce=1 | 400 `SpatialWindowError`（物件 `detail`） | 400 同上（**body 改變**，status 不變） |
| GET bbox，enforce=0 | 400 `BBOX query only allows single-day data. Requested … is unavailable; …` | 400 同上（**body 改變**） |
| POST，enforce=1 | 400 `SpatialWindowError` | 400 同上（**body 改變**） |
| POST，enforce=0 | 400 `day … not available; …` | 400 同上（**body 改變**） |

- 實測輸入：`2025-02-30`、`2025-13-01`、`0000-01-01`、`2025-01-01\n`（結尾換行）。四者在基線上都通過 regex（Python 的 `$` 允許結尾換行）卻不是合法日曆日，表現完全相同（point／range 500，bbox／POST 400）；修補後全部是 400 `Invalid date format…`。
  結尾換行這一項是同一類輸入，status 轉換與上表相同，沒有新的行為類型。
- 不受影響（逐項比對 before／after 完全相同）：格式錯誤的日期（`2025/01/01` 等，原本就是同一個 400）、合法日期（含閏日 `2024-02-29`）。
- 檢查順序不變：GET point 模式仍是 `append` → `mode` → `format` → 無資料 503 → `sample`／`format` 模式檢查 → 日期；因此「無資料時回 503」等既有優先順序不變。
- 既有的非 ASCII 數字（例如全形數字）行為未改變，也未在本輪範圍內。

### BC-1 測試
- `test_bc1_non_existent_day_is_400_on_every_path`：GET point、range（start／end）、bbox、POST × 4 個不存在日期 + 2 個格式錯誤日期 → 400，body 等於上述字串且符合 generated OpenAPI 的 400 schema。
- `test_bc1_valid_days_unchanged`：合法日期（含閏日）仍為 200。
- `test_bc1_non_existent_day_is_a_date_error_not_a_window_error`（enforce=1）：bbox 與 POST 的 body 由 `SpatialWindowError` 改為日期錯誤字串，status 仍為 400。
- `test_no_500_is_documented`：兩個 operation 都不記錄 500。
- 原本釘住 500 的 `test_calendar_invalid_date_in_point_mode_is_500` 已移除（由上述測試取代）。
- OpenAPI（round 5）：`Date` 與 `start` 的說明改為「malformed or non-existent day → 400」；兩個 `invalid_date` example 加上 summary。Round 7 重整說明文字後：`Date` 寫「must be a real calendar date」，狀態碼與原因只寫在 400 response（「a date that is malformed or not a real calendar date」）。

## BC-2 — 非有限座標（NaN／±Inf）（OD-5）— 已實作（選項 B2-b）

| 項目 | 內容 |
| --- | --- |
| 問題（基線） | `lon0`／`lat0`／`lon1`／`lat1` 與 POST `points` 接受 `nan`、`inf`、`-inf`（以及溢位成 inf 的 `1e400`），回 200 並回傳格網邊緣的格點資料。另有一個基線就存在的 500：POST body 缺 `date` 且含字面值 `NaN` 時（`{"points":[[NaN,12]]}`），FastAPI 的「missing」錯誤會回顯整個 body，序列化失敗 |
| 核准的作法 | owner 選 **B2-b**（round 5 選項表）：GET 與 POST 的非有限座標都回 422 `HTTPValidationError`；新增 `RequestValidationError` handler，保留 FastAPI 預設 422 body 形狀，只把非有限 float 轉成字串；有限超界座標維持 200 並 clamp |
| Round 5 的暫停原因 | 只加 `allow_inf_nan=False` 時，POST 字面值 `NaN`／`Infinity`／`-Infinity`／`1e400` 會 500（422 body 回顯 float NaN，Starlette `JSONResponse` 以 `allow_nan=False` 序列化失敗）。B2-b 的 handler 正是解決這一點 |

### 實作（`dev2026/api/app.py`，皆為執行行為變更）

| 位置 | 變更 |
| --- | --- |
| `read_ghrsst` 的 4 個座標 `Query(...)` | 加 `allow_inf_nan=False`（其他參數不變） |
| `PointsRequest.points` | `List[List[float]]` → `List[List[FiniteFloat]]`（`pydantic.FiniteFloat`） |
| `_overloaded_handler` 之後（新增約 50 行） | `@app.exception_handler(RequestValidationError)`：先以 `jsonable_encoder(exc.errors())` 得到 FastAPI 預設 handler 會序列化的內容；**若其中沒有非有限 float，直接回傳 FastAPI 的 `request_validation_exception_handler`（預設行為，位元組相同）**；若有，只把非有限 float 轉成字串後回 `JSONResponse(422, {"detail": ...})` |

`input` 字串化規則：
- 只處理 **float** 型別的 `NaN` → `"NaN"`、`+inf` → `"Infinity"`、`-inf` → `"-Infinity"`；溢位的 `1e400` 解析後就是 `+inf`，因此是 `"Infinity"`。
- 在 `jsonable_encoder` 輸出的任意深度遞迴處理（dict 的值、list 的元素；tuple 已由 encoder 轉成 list）。
- 字串、整數、布林、`null`、有限 float 一律不變。因此 GET query string 的 `input`（本來就是字串，如 `"nan"`、`"1e400"`）維持原樣；POST 中以 JSON 字串送出的 `"nan"` 也維持原樣。
- `type`、`loc`、`msg`、`ctx` 的內容與 FastAPI 預設相同；回應形狀仍是 `{"detail":[...]}`，符合既有 `HTTPValidationError`（`input` 本來就無型別限制），沒有新增錯誤格式。

### 影響（round 6 實測，本機 synthetic store；前後逐項比對）

| 請求 | 基線 | 修補後 |
| --- | --- | --- |
| GET `lon0`／`lat0`／`lon1`／`lat1` = `nan`、`NaN`、`inf`、`-inf`、`Infinity`、`-Infinity`、`1e400`、`-1e400`（point 與 bbox） | 200（邊緣格點） | 422 `finite_number`，`loc` = `["query", <name>]`，`input` = 原字串 |
| POST `points` 含字面值 `NaN`、`Infinity`、`-Infinity`、`1e400`、`-1e400` | 200（邊緣格點） | 422 `finite_number`，`loc` = `["body","points",i,j]`，`input` = `"NaN"`／`"Infinity"`／`"-Infinity"` |
| POST `points` 含 JSON 字串 `"nan"`、`"inf"` | 200 | 422，`input` = `"nan"`／`"inf"`（原樣） |
| POST 缺 `date` 且含字面值 NaN／Infinity | **500**（基線既有） | 422；`missing` 錯誤的 `input` 是整個 body，其中的非有限值被字串化 |
| POST 其他欄位為非有限值（`date: NaN`、`points: -Infinity`、`mode: {...NaN...}`） | **500**（基線既有：422 body 回顯非有限 float，序列化失敗） | 422，`input` 中的非有限值字串化（含巢狀物件／陣列） |
| 有限超界座標（GET `lon0=1e300`、`lon0=200&lat0=-95`；bbox ±500；POST `[[200,-95],[-200,95]]`） | 200，clamp | **200，clamp（不變）** |
| 一般 422（缺參數、非數字、型別錯、JSON 解析失敗、空 body、陣列 body 等 15 種） | — | **位元組完全相同**（status、content-type、content-length、body sha256） |

優先順序（隨核准規則而來，記錄供 review）：框架驗證在 handler 自己的 400／413 檢查**之前**執行——這與現行「非數字座標」的處理相同（基線 `lon0=east&append=foo` 就是 422）。因此：
- 同時有非有限座標與其他錯誤的請求，回 422 而不是原本的 400（例：`lon0=nan&append=foo`；POST `date:"bad"` 加 NaN；POST `[[NaN]]` 長度也錯）。
- 只給 `lon1=nan`（無 `lat1`，原本是 point 模式、`lon1` 不被使用）也回 422。
- 超過 1000 點且含 NaN 的 POST 回 422 而非 413（已實測：1001 點、最後一點為 NaN → 422，`loc` = `["body","points",1000,0]`）。

### 與 P5 協調事項（供 owner 與 P5 reviewer）
- 新的全域 `RequestValidationError` handler 是 `dev2026/api/app.py` 的**執行行為**（影響所有 422 的產生路徑；一般 422 由測試證明不變）。座標 `Query` 與 `PointsRequest` 的修改也是。
- 以 `git merge-file` 對 P5 三個版本（`ee84037`、`d7ba064`、`1003b25`）的 `app.py` 做三方合併：0 衝突，合併結果可解析。本輪未修改或操作任何 P5／O4 worktree。
- P5 的 maintenance gate（`ee84037`，未部署）在 handler 內呼叫；框架驗證先於它執行，因此 maintenance 期間含非有限座標的請求會得到 422 而非 503——這與現行任何 422（如缺參數）相同，整合時一併確認即可。

### BC-2 測試（`dev2026/tests/test_api_v1_contract.py`）
- `test_bc2_get_non_finite_is_422`：4 個參數 × 8 種值（point 與 bbox）→ 422，body 完全比對並以 generated `HTTPValidationError` 驗證，且為標準 JSON（無 NaN／Infinity token）。
- `test_bc2_post_non_finite_is_422_with_stringified_input`：字面值 `NaN`／`Infinity`／`-Infinity`／`1e400`／`-1e400` 與字串 `"nan"`／`"inf"`，lon 與 lat 兩個位置 → 422，`input` 字串化規則逐項比對。
- `test_bc2_nested_input_is_json_safe`：缺 `date`（整個 body 回顯）、`date: NaN`、`points: -Infinity`、巢狀物件中的 NaN／Infinity。
- `test_bc2_ordinary_422_is_unchanged`：4 種一般 422 與基線錄得的 body 位元組完全相同。
- `test_bc2_handler_defers_to_fastapi_default`：單元層級——無非有限值時與 FastAPI 預設 handler 輸出位元組相同；有時只改非有限 float（tuple、`"inf"` 字串、`True`、`0`、`1.5` 不變）。
- `test_bc2_finite_out_of_grid_still_clamps`：GET point、GET bbox、POST 的有限超界座標仍為 200 且 clamp。
- `test_bc2_validation_precedes_handler_checks`：上述優先順序。
- 反向對照：同一組測試對「BC-2 前」的程式副本執行，BC-2 測試全部失敗（另見 03）。

OpenAPI（round 6）：座標參數與 `PointsRequest.points` 的說明加上非有限值規則，`info.description` 的 Contract notes 說明 422 `input` 的字串化。Round 7 重整後：非有限值規則在各 endpoint 說明的「Coordinates」只寫一次（「NaN, Infinity and -Infinity are rejected as validation errors」，有限超界值 clamp）；參數欄位不再重複；422 的原因寫在兩個 operation 的 422 response description，POST 的 422 另說明 `input` 中非有限值寫成 "NaN"／"Infinity"／"-Infinity"；全域 Contract notes 已移除。422 response 仍是 FastAPI 產生的 `$ref HTTPValidationError`，未改。
