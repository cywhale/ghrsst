# 04 — 待 Owner 決定事項

> 狀態（截至 2026-11-24）：**F2 readiness（owner 決定於 2026-11-20）— v1 契約政策已由 owner 決定；G-C/D 與 Stage E 已通過 Codex review；Ops-1 公開 POST spot check 通過（未測 GET cache）；G-B 已由 Codex review 通過；**F2 已由 owner 核准（2026-11-24）**，`info.version` 已改為 `1.0.0`；尚未 commit、尚未部署。** 已決定的項目見下方「決定紀錄」（含「F2 readiness 決定」各列）；表中「建議」欄保留決定前的內容。
> 每項都附「現況」與「建議」；建議不是決定。標 ⚠ 者會改變對外行為或形成新的服務承諾。
> 欄位「阻擋」表示未決時哪個 Stage／Gate 不能完成（見 [03](03_roadmap_and_validation.md)）。

## 決定紀錄（owner，2026-11-20）

| ID | 決定 |
| --- | --- |
| OD-1 | 基線 = production serving code `6b32e54`；Web API v1 使用獨立 branch／worktree（不在 P5 worktree 工作、不用 stash 共用工作目錄）；暫不 push。已執行，見 [00 §7](00_baseline_and_provenance.md#7-round-4-本機基線調整紀錄2026-11-20) |
| OD-2 | v1 契約以 `6b32e54` 的 production 行為為準；未部署的 `ee84037`（3 天快取、maintenance 503）不寫成現況，也不納入本輪 API v1 改動；是否先整合或部署由 owner 與 P5 另行協調 |
| OD-15 | API v1 負責：OpenAPI metadata、request／response schemas、examples、錯誤與 headers 的契約描述、契約 regression tests、核准的測試依賴。P5 負責 compaction、replacement、prune、maintenance 等資料流程與執行行為。`app.py` 的 API v1 修改限於明確的 OpenAPI 文件 metadata；可能影響輸入驗證、序列化、狀態碼、headers、快取或資料讀取的改動，一律先提案。P5 要改同檔時先通知 owner 協調 |
| OD-7、8、9、10、14、18 | 採用表中建議 |
| OD-11（部分；當時紀錄） | `contact`／`license`／`termsOfService` 暫時從 generated OpenAPI 省略，不使用 `TBD` 或其他佔位值；**當時**仍待 owner 決定是否提供內容。後續已由下方「OD-11（F2 readiness 決定）」取代：owner 決定 v1 省略這些欄位。（`servers` 原本也在此列，round 10 已由 OD-14 決定，見下） |
| OD-14（round 10） | **production server 已確定**：根層 `servers: [{url: https://eco.odb.ntu.edu.tw}]`，server object 只有 `url`（round 10 修補移除 `description: Production`：APIverse 會讀 server URL 組合請求）；paths 已含 `/api/...`，URL 不加 `/api`。本機 Swagger 的 Try it out 因此指向 production（本輪未執行任何 Swagger 或 production 請求）。範例日期部分維持原決定（空間範例不寫死日期、標明示意）。已實作（02 §2.1、§11.6） |
| operationId（round 10） | `GET /api/ghrsst` → `getGhrsstData`、`POST /api/ghrsst/points` → `postGhrsstPoints`；明確、穩定、唯一。已實作 |
| 認證（round 10） | 公開 API、不需認證：兩個 operation 各自 `security: []`，說明加一句免認證；不新增 security scheme、不設根層全域 `security`。已實作。Round 10 修補：若 FastAPI 原始文件出現認證需求（任何實際 security scheme，含與匿名 `{}` 並列），產生 OAS 時明確失敗（`OpenApiSecurityConflict`），不把認證需求覆蓋成公開；只表示匿名存取的 `[]`／`[{}]`（OAS 3.1.1 §4.8.30）正規化為 `security: []`（依 Codex review）；格式錯誤的值（`security: null`、非陣列；`securitySchemes` 為 `null`、`[]` 等非空物件以外的值）也拒絕，判斷依欄位存在與型別（Codex review P2）；不改 API 認證行為 |
| `append` 預設（round 11） | GET `append` 參數與 `PointsRequest.append` 的 schema 各加 `default: "sst"`，兩處一致；`append` 仍為 optional。`mode` 本輪保持原樣：不加 `enum`、不加空字串選項。只改 OpenAPI metadata，不改 handler／路由／執行行為。已實作（02 §11.7） |
| 版本（round 10 重申） | `info.version` 仍為 `0.4.1`；正式核准 v1（F2）時才升為 `1.0.0`。**當時 F2 尚未核准**（2026-11-24 已核准並升版，見「F2 核准」列） |
| OD-19 | 允許新增 `jsonschema` 作為 dev／test-only 依賴；不得加入 production runtime 依賴 |
| F1 | 同意開始 Stage C／D 的實作範圍；**不代表**核准對外宣告 OpenAPI v1.0.0 |
| OD-4（BC-1） | 核准：日曆不存在的日期改回 400。Round 5 已實作（05） |
| OD-5（BC-2） | 核准：非有限座標（NaN／±Inf，含溢位值）回 422 `HTTPValidationError`，有限超界座標維持 clamp。Round 5 試作發現 POST 字面值 NaN／Infinity 會 500；owner 隨後選定 **B2-b**（新增 `RequestValidationError` handler，保留 FastAPI 預設 422 形狀，只把非有限 float 轉成字串）。**Round 6 已實作**（05） |
| Codex round-4 review | `format` 的 OpenAPI 描述須符合 handler 的 trim＋小寫正規化：移除 `enum: [json]`，改寫 description。Round 5 已實作 |
| Codex round-5 review | 日期 pattern 的 `$` 會接受結尾換行，與 BC-1 handler 不一致：`Date` 與 `start`／`end` schema 加 `maxLength: 10`。Round 6 已實作 |
| OD-21（round 7） | **資料日期窗口（v1 契約方向）**：空間查詢（GET BBox、POST points）至少保證最新 31 個連續日曆日，終點為最新 spatial-query 可用產品日期；單點時間序列至少保證最新 366 個連續日曆日，終點為最新 point-query 可用產品日期（程式上各自計算；round 9 起 OAS 不再對一般使用者寫「可能不同」，見下列 round 9 決定）；皆含首尾，窗口內每一天都可用、不缺日（round 8 釐清）；更早日期可能可查但不保證、可能有缺口；「日期可查」不代表每個地點／變數都有非 null 值；`spatial_window_days=31` 是 retention target，不是實際窗口長度。依據：owner 2026-11-20 對 VM24 的唯讀確認（最新資料日 2026-11-18；同一極小 BBox 2026-10-19..2026-11-18 共 31 天皆 200 且各一列；health 空間窗口 2026-09-04..2026-11-18）。**狀態：已寫入本機 OAS 草案（02 §5.0）；OAS 公開用詞已由下方 round 12 列取代；P5 reviewer 已確認為資料涵蓋政策與設計依據、非永久保證（見「OD-21 P5 意見」列）；當時 F2 未核准（2026-11-24 已核准）** |
| OD-21 補充（round 9） | **兩種狀態分開**：(1) **目前覆蓋範圍**——point 目前自 2023-01-01 起每日可用至最新 point 日期（owner／Codex 2026-11-20 唯讀查核：2023-01-01..2026-11-18 共 1,418 天連續；2023-01-01 回 200 有 SST；2025-11-18..2026-11-18 回 366 個連續日期），OAS 以「currently」寫入，不宣稱起點永久不變；(2) **保證窗口**——空間 31／單點 366 日 gap-free，P5 保障當時仍待確認、F2 當時未核准（2026-11-24 已核准）。一般 OAS 移除「較早日期缺資料會略過」（handler 細節留在 01）；欄位缺漏（point 省略／BBox null）只寫在 `DataRow`／200 response。已實作（02 §11.5） |
| OD-21 公開用詞（round 12） | 已與 P5 Codex reviewer 討論，owner 接受簡潔寫法：Point「Point queries support the latest 366 consecutive calendar days ending on the latest date available to point queries. The archive currently starts on 2023-01-01.」；Spatial「Bounding-box queries support the latest 31 consecutive calendar days ending on the latest date available to spatial queries. The same window applies to `POST /api/ghrsst/points`.」。保留「consecutive calendar days」；**取代**上兩列在 OAS 中的「至少保證／窗口內不缺日／更早日期可能可查但不保證、可能有缺口」寫法——OAS 不寫 `guaranteed`、`no date missing`、`under normal service` 或內部維運例外；窗口外的錯誤與可用窗口資訊留在既有 400 response；`2023-01-01` 寫成目前 archive 起點、不是永久承諾；366 天涵蓋窗口與單次 request 的 366 天上限明確區分；P5 的 compaction、replacement、audit、prune、monitoring 等流程不寫進公開 OAS。**只定案公開用詞**：P5 資料流程保障的跨工作區確認與其他 F2 前置事項仍未完成；`info.version` 仍 `0.4.1`；`change_log.md` 待 F2 正式升版時再記錄。已實作（02 §11.8） |
| 最新日期與 freshness（round 9） | point／daily／spatial 最新日期目前相同（2026-11-18），移除面向一般使用者的「point 與 spatial 最新日期可能不同」；BBox 無日期仍照實寫 daily 最新日＋未可供 spatial 查詢時失敗、不回退。Freshness 採 owner 指定句「The newest product date typically lags 1–2 days behind today, depending on upstream GHRSST data availability.」，後接「Omit date parameters to request the latest available data.」；內部排程（晚上抓前一天、隔天早上補抓）不寫成 API 承諾。已實作（02 §11.5） |
| GET 說明層級（round 9） | 一句話用途 → Coordinates → Query type → **Point Query**（Dates／Coverage／Request limit）→ **Bounding-Box Query**（Dates／Coverage／Request limit／Swagger 提示）→ freshness 一處；不用「Point and point time series」「Mode」，移除冗長「Data date windows」段。BBox 單提供 `start` 或 `end` 都可查單日、兩者皆提供必須相同。BBox 格點上限沿用 `cfg.BBOX_POINT_LIMIT`（VM24 300,000、程式預設 1,000,000，皆不硬編碼）。已實作（02 §11.5） |
| Swagger 可讀性（round 7） | 保留 `default` 群組與展開方式（不改 tags／Swagger UI 設定）；移除全域 Contract notes，規則放到對應 endpoint／response；GET 說明移除「Mode」標題並分段；NaN／Inf 通則在 endpoint 只寫一次；`format` 說明精簡；POST 白話化、fan-out 數值只在 413。已實作（02 §11.3） |
| OD-20 | 維持：`info.version` 本輪仍為 `0.4.1`，改為 `1.0.0` 的時點是 F2（2026-11-24 已執行，見「F2 核准」列） |
| 路徑（F2 readiness 決定） | endpoint 路徑固定為 `/api/ghrsst`、`/api/ghrsst/points`；不新增 `/v1`，不改路由 |
| OD-16（F2 readiness 決定） | `1.0.0` 是第一個正式對外的 OpenAPI 契約版本；過去版本未正式公告，**不**設定舊版棄用期限或過渡期。從 `1.0.0` 起採 SemVer 契約版本慣例：不相容的契約變更升 major、向後相容的功能增補升 minor、相容修正升 patch。**不**因此承諾固定的棄用時程或公告管道 |
| OD-21 P5 意見（F2 readiness 記錄） | 已收到 P5 Codex reviewer 的確認：366／31 日可作為**資料涵蓋政策與 P5 流程的設計依據**；**不是**無條件或永久的保證（不代表任何情況、永遠無缺日）。來源：owner 於 2026-11-20 對話中提供；**未提供 P5 commit SHA**，本紀錄不補造。公開 OAS 沿用 round 12 用詞，不加入內部維運流程或「under normal service」 |
| OD-3（F2 readiness 決定） | 採建議：F2 核准時在舊 Draft `specs/ghrsst_api_v0_1_0.spec` 頂端加 SUPERSEDED 標示，並更新 `AGENTS.md` 改指 v1 規格；不刪舊檔。**實際修改留到 F2 核准時執行**（本輪未改） |
| OD-6、OD-17（F2 readiness 決定） | v1 維持現行行為並文件化（錯誤格式現況以 `oneOf` 描述、bbox 太大回 400、其他上限回 413；只給 `lon1` 或 `lat1` 視為 Point Query）。目前 OAS 已如此描述 |
| OD-11（F2 readiness 決定） | 暫不提供 `contact`、`license`、`termsOfService`（繼續從 OAS 省略，不放佔位值）；rate limit／SLA **不作承諾** |
| OD-12（F2 readiness 決定） | CORS 延至 Stage G 規劃；**不作 v1 承諾**，不列為 F2 blocker |
| change_log（F2 readiness 決定） | 升版時在 `change_log.md` 新增獨立的「OpenAPI contract v1.0.0」紀錄，與服務部署版本（ver 0.x）分開；須標明「已核准、尚未部署」，部署後再補部署資訊。F2 前不改 `change_log.md` |
| F2 → commit 流程（F2 readiness 決定；X1） | F2 前**不得** commit。F2 核准先綁定**已 review 的候選 diff**；核准後才升 `info.version` 為 `1.0.0` 並新增 changelog 紀錄；升版與 changelog 完成 review 後才提交；提交後再把 commit SHA 補入紀錄 |
| 部署目標（F2 readiness 記錄；X2） | v1 契約對應的部署目標是 `6b32e54` 加上 API v1 候選變更（含已核准的 BC-1／BC-2）；目前 production（`6b32e54`）**尚未包含**這些本機改動。若部署前整合 `ee84037` 或其他會改變 503／headers 的 P5 變更，必須先更新並 review OAS（02 §7）。整合時點仍由 owner 與 P5 協調（OD-15） |
| **F2 核准（2026-11-24）** | Owner 核准 F2：v1 契約（02）為正式公開契約，綁定已 review 的候選 diff。核准**不含** commit、push、部署授權。已執行：`info.version` `0.4.1` → `1.0.0`（OD-20）、snapshot 與版本測試、`change_log.md`「OpenAPI contract v1.0.0」（已核准、尚未部署）、02 → Approved、OD-3（SUPERSEDED 標示＋`AGENTS.md`）。待 Codex review → commit → 補 SHA |
| 行為變更 | 需要行為變更時，先交短提案（問題與證據、維持現況的選項、改變行為的選項與影響、建議與測試方式、是否獨立提交）；owner 回覆前不實作。本輪提案見 [05](05_behavior_change_proposals.md) |

| ID | 主題 | 現況（`6b32e54`／production） | 選項 | 建議 | 阻擋 |
| --- | --- | --- | --- | --- | --- |
| OD-1 | v1 基線 | serving = `6b32e54`（本機有完整 commit；GitHub 查無，00 §4）；`ghrsst_v1` = 舊 `main` | B1 `6b32e54`／B2 `v0.5.0`／B3 `d7ba064`／B4 merge 進 `ghrsst_v1`（00 §5） | B1：以本機完整 commit 為基線，`ghrsst_v1` 重指或另開本機 branch；推上 origin 留到 Stage G | A |
| OD-2 ⚠ | `ee84037`（未部署）的處理 | 該 commit 改 `max-age` 864000→259200、新增 maintenance 503 | (a) v1 以前先上線 `ee84037`，v1 描述含 maintenance 503 與 3 天快取；(b) v1 不含，之後以 minor／major 加入；(c) v1 把 `max-age` 秒數列為部署值，只描述規則 | (c)，並在部署順序上與 P5 協調 | A, G |
| OD-3 | 舊 Draft `specs/ghrsst_api_v0_1_0.spec` 與 `AGENTS.md` authority 行 | 舊 Draft 仍被 `AGENTS.md` 稱為 authoritative；與現況 9 處矛盾 | 加 SUPERSEDED 橫幅＋改 `AGENTS.md`／刪除／重寫 | F2 時加橫幅、不刪除；`AGENTS.md` 改指 v1 規格（02 §9） | F2 |
| OD-4 ⚠ | 日曆不合法日期（如 `2025-02-30`） | 基線：point 模式 500 `text/plain`（round 5 已改為 400，見決定紀錄） | 改為 400 `Invalid date ...`／維持 | 改為 400（bug fix，與 metadata 修補分開） | — |
| OD-5 ⚠ | 座標範圍／NaN | 基線：不驗證，clamp 到格網邊緣；NaN/inf 回 200＋邊緣格點（round 6 已改為 422，見決定紀錄） | 維持 clamp 並寫入文件／超出範圍回 400 | v1 維持 clamp 並文件化；NaN/inf 改 400 另議 | — |
| OD-6 ⚠ | 錯誤一致性 | 「太大」bbox 用 400、其他 413；`detail` 有 string 也有 object | 維持並用 `oneOf` 描述／統一 | v1 維持現況、完整文件化；統一留給 v2 | — |
| OD-7 | `spatial_window_days` 欄位語意 | 原始碼定義為 retention target（31）；目前 delta 實際保留 75 天。不是程式矛盾，而是欄位名稱容易被讀成「實際窗口天數」 | 文件澄清／改名並新增欄位／新增「實際天數」欄位 | 先在文件中澄清為「保留目標天數」，以 `available_spatial_window` 與逐日成員資格為準；是否新增欄位另議（minor） | F1 |
| OD-8 | `format` 參數在 v1 文件中 | production 只接受 `json`；說明提到 coveragejson/raster | 只列 `json`／列為 experimental | 只列 `json`；CoverageJSON 另走前端契約流程 | F1 |
| OD-9 | 觀測用 headers 的契約地位 | `X-Store-Route`、`X-Read-Ms`、`X-Encode-Ms` | contract／informational／移除 | informational（可變更、客戶端不得依賴） | F1 |
| OD-10 | 欄位不存在：point 省略 vs bbox/POST 為 null | 兩種語意並存，皆有測試釘住 | 維持並文件化／統一 | 維持並文件化（統一會破壞既有客戶端）；schema 不區分，以測試驗證 | F1 |
| OD-11 | 公開 metadata：contact、API 使用條款／license、rate limit／配額、SLA | 皆無；NGINX 有 `limit_req_zone 50r/s` 定義，是否套用到本 API 未驗證 | 由 owner 提供／標示未承諾 | 未提供前：`contact`／`license`／`termsOfService` 從 OpenAPI **省略**（不放佔位值，02 §2）；規劃文件中寫「待 owner 提供」；rate limit／SLA 在文件中寫「未承諾」，不捏造 | F2 |
| OD-12 ⚠ | CORS | 無任何 `Access-Control-*`；OPTIONS 405 | 維持（只限同源）／在 NGINX 或 app 加入 | owner 決定使用者族群；若要開放，屬部署變更，另行規劃 | G |
| OD-13 | POST proxy 快取回應錯置（01 §10.3） | 2026-11-19 觀察到不同 body 收到相同舊 400。**2026-11-20 起：F2 前置的公開 POST spot check 已完成**——owner 修改並 reload NGINX 後，有效日期 200、窗口外日期 400、不同的有效 body 200，皆符合各自的請求（01 §10.4）。僅涵蓋這個 POST 請求序列；GET cache、其他快取情境及修改後的 NGINX 設定 diff 未驗證，不列為 F2 阻擋項 | 可分開決定：清除特定 entry／修設定（POST 不快取需同時處理 `proxy_cache_bypass` 讀取與 `proxy_no_cache` 寫入，或從 `proxy_cache_methods` 移除 POST；或 key 納入 body）／處理時間 | 列為獨立 ops 待辦，不阻擋本機工作；F2 前確認公開 POST 對不同 body 回應正確 | —（F2 前置已完成，2026-11-20） |
| OD-14 | `servers` 與範例 | 無 `servers`；POST 範例日期已失效 | 寫入公開 host／不寫；範例使用相對日期說明 | 寫入 `https://eco.odb.ntu.edu.tw`（若 owner 同意公開承諾）；未決前 `servers` 從 OpenAPI 省略（02 §2）；空間範例不寫死日期。**Round 10：owner 已決定寫入（見決定紀錄）** | F1 |
| OD-15 | `dev2026/api/app.py` 的 ownership | P5 資料流程 session 也在改此檔（`ee84037`） | API track 只改 metadata 區段、P5 只改資料存取；或統一由一方合併 | 約定：v1 metadata 變更集中在少數區段；合併與部署順序由 owner 排定 | A, G |
| OD-16 | 版本與相容政策 | 版本字串自 v0.4.1 起未更新 | SemVer + 公告期／其他 | 02 §8 草案；過渡期長度由 owner 決定 | F2 |
| OD-17 ⚠ | 只給 `lon1` 或 `lat1` | 靜默當成 point 模式 | 維持並文件化／回 400 | v1 維持並文件化；改 400 另議 | — |
| OD-18 ⚠ | 上限值是否成為服務承諾 | 部署值：366 天、1000 點、fan-out 64、bbox 300,000（皆 env 可調）；超過時的狀態碼與 body 已盤點 | (a) 只記錄部署值與拒絕規則；(b) 承諾某些數值為最小保證 | 現階段採 (a)；任何數值承諾都是新的服務承諾，由 owner 另行決定 | F1（記錄方式） |
| OD-19 | Stage D 測試依賴 | venv 無 `jsonschema`／`openapi_schema_validator` | 新增 dev-only 測試依賴／手寫結構檢查 | 新增 dev-only `jsonschema`（不進 runtime）；需 owner 同意修改依賴檔 | F1 |
| OD-20（round 4 新增） | `info.version` 何時改為 `1.0.0` | 實作維持 `0.4.1`（F1 不代表核准宣告 v1.0.0），由測試釘住 | (a) F2 核准時改；(b) Stage C 就改（本機、未部署）；(c) 用預發布字串如 `1.0.0-rc.1` | (a)：版本字串即對外宣告，與 F2 綁在一起；改動只有一行加 snapshot 更新 | F2 |

## F2 readiness 狀態與後續事項（2026-11-20 起；狀態截至 2026-11-24；取代 round 9 的優先回覆清單）
1. **Ops-1／OD-13（公開 POST spot check 通過，2026-11-20）**：修改前（13:06 UTC）同型態錯置仍可重現；**owner** 修改並 reload VM24 NGINX 設定（修改前檢視 `/etc/nginx/conf2.d/api_cache_proxy.conf`）後，15:09 UTC 三筆公開 POST 序列通過（01 §10.4、03「Ops-1 驗收結果」）。未測 GET cache 行為，未宣稱驗證所有快取情境。根因：修改前設定允許快取 POST、cache key 不含 body、400 快取 1 分鐘（Codex 先前唯讀確認），足以解釋本次錯置；owner 修改後的設定 diff 未檢視。
2. **Stage E（Codex 已完成，2026-11-20）**：規格 ↔ generated OpenAPI ↔ handler ↔ 測試的一致性 review 已完成（150 tests passed）；E6 用詞修正 Codex 已確認。各項結論見 03「F2 readiness」。G-C/D 當時仍待 Codex review；其後 **G-C/D Codex review 已通過**（2026-11-20）。
3. **G-B（已通過）**：G-B 已由 Codex review 通過（00–05 文件；2026-11-24 記錄）。
4. **F2 核准（已完成，2026-11-24）**：owner 已核准，綁定已 review 的候選 diff；已升 `info.version` → `1.0.0`、加 changelog 條目（已核准、尚未部署）、02 → Approved、執行 OD-3。**後續**：Codex review → commit → 補 SHA（02／03／04 與 changelog）；commit、push、部署需另行授權。
5. **`app.py` 執行行為整合（Stage G，與 P5 協調）**：BC-1／BC-2 與 P5 變更的整合時點；若含會改變 503／headers 的 P5 變更（如 `ee84037`），先更新並 review OAS。不阻擋 F2 核准本身，但 v1 契約在部署前不會在 production 生效。
6. **OD-21**：P5 reviewer 的政策／設計依據確認已記錄（見決定紀錄）；它不是永久保證。若 P5 日後改變 point 覆蓋起點，只需更新 `POINT_COVERAGE_START` 與 snapshot。
7. 已決定：OD-3（已於 F2 後執行）、OD-6、OD-11、OD-16、OD-17、OD-20（已於 F2 後執行）；OD-12 屬 Stage G。
