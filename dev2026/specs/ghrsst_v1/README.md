# GHRSST API v1.0.0 契約規劃

> 狀態（截至 2026-11-24）：**G-C/D 已通過 Codex review（2026-11-20）；Stage E 已由 Codex 完成（150 tests passed；diff／whitespace 檢查乾淨），E6 用詞統一 Codex 已確認。v1 契約政策已由 owner 決定（04）；OD-21 已記錄 P5 reviewer 的政策／設計依據確認（非永久保證）；Ops-1 公開 POST spot check 通過（owner 修改並 reload NGINX 後；未測 GET cache）；G-B 已由 Codex review 通過；**F2 已由 owner 核准（2026-11-24）**，`info.version` 已改為 `1.0.0`；Codex 最終 review 已完成；實作 commit SHA：`b9bd4e46097289632874d1615c5737c37ebc0156`（parent `6b32e54`；其後的文件同步 commit 另有自己的 SHA）；尚未部署（Stage G）。** **Round 12（2026-11-20）— 基線 `6b32e54`；F1 已同意；Stage C／D 與 round 5–12 修補已在本機實作，當時待 Codex review（其後 G-C/D 已通過）。Round 7 寫入 owner 選定的資料日期窗口（空間 31 日、單點 366 日；**P5 資料流程保障尚待確認**），round 8 修正窗口內連續性與兩種最新日期的用詞，round 9 依 owner 指定層級重整 GET 說明並加入 point 目前覆蓋（2023-01-01 起，現況）與 freshness 提示，round 10 加入 production `servers`、穩定 `operationId` 與操作層 `security: []`（公開、免認證），round 11 為 `append` 加 `default: "sst"`，round 12 把日期窗口改為 owner 接受的簡潔用詞（已與 P5 Codex reviewer 討論；其後 P5 reviewer 確認 366／31 日為資料涵蓋政策與設計依據，不是永久保證）。當時 Gate F2 未核准、`info.version` 仍為 `0.4.1`（2026-11-24 已核准並升版）。沒有 commit、push、部署；Claude 本輪未啟動或重啟本機 server（本機 Swagger 曾由 Codex 啟動供 owner 檢視）。**

## 範圍
為現有公開 API 建立可對外承諾的 OpenAPI v1.0.0 契約：
- `GET /api/ghrsst`（point／range／bbox）
- `POST /api/ghrsst/points`

維持既有路由；不做 URL 版本化；不在本規劃中改變 API 行為——任何行為變更都列為 owner decision。
資料流程（P5 之後的 ingest／compaction／prune、O4／gen3）**不在範圍內**，由其他 session 負責。

## 角色
| 角色 | 負責 |
| --- | --- |
| Human（owner／orchestrator） | 基線、行為變更、公開政策、核准（F1／F2）、發布與部署授權 |
| Claude | 調查、文件、之後的實作 |
| Codex | Review |
| 部署執行者 | **未定** |

## 文件索引
| 檔案 | 內容 |
| --- | --- |
| [00_baseline_and_provenance.md](00_baseline_and_provenance.md) | worktree／commit 狀態；production serving code 的證據（`6b32e54`）；遠端查核；基線選項 |
| [01_current_contract_inventory.md](01_current_contract_inventory.md) | 依 `6b32e54` handler 與實測整理的現行行為、錯誤、headers、快取、proxy 觀察 |
| [02_openapi_v1_spec.md](02_openapi_v1_spec.md) | v1 契約規格（**Approved**，F2 核准 2026-11-24；尚未部署） |
| [03_roadmap_and_validation.md](03_roadmap_and_validation.md) | A、B、F1、C、D、E、F2、G 與獨立 Ops-1 |
| [04_owner_decisions.md](04_owner_decisions.md) | OD-1 … OD-21；2026-11-20 決定紀錄 |
| [05_behavior_change_proposals.md](05_behavior_change_proposals.md) | 行為變更：BC-1（OD-4）、BC-2（OD-5，選項 B2-b）皆已實作，含前後對照、測試與 P5 協調事項 |

Round 4 的程式與測試（本 worktree；當時未 commit，其後收入實作 commit `b9bd4e4`）：
- `dev2026/api/openapi_contract.py`（新）、`dev2026/api/app.py`（檔尾新增 9 行）——只改寫 generated OpenAPI（02 §11）
- `dev2026/api/app.py` 的 `_parse_date`（1 行改為 9 行，淨 +8）——BC-1 行為修補（round 5，05）
- `dev2026/api/app.py` 的 4 個座標 `Query`、`PointsRequest.points`、新增的 `RequestValidationError` handler——BC-2 行為修補（round 6，05）；屬執行行為，需與 P5 協調
- `dev2026/tests/test_api_v1_contract.py`、`dev2026/tests/snapshots/openapi_v1.json`（新）——契約測試（03 Stage D）
- `dev2026/requirements-test.txt`（新）——dev／test-only `jsonschema`

## 目前狀態摘要
0. **Round 4**：owner 決定基線 `6b32e54`；`ghrsst_v1` 已從 `e302fc4` fast-forward 到 `6b32e54`（非 reset／force；文件保存並核對 sha256，00 §7）。
   以下第 1 點為 round 1–3 時的狀態，保留作紀錄。
1. 本 worktree（`ghrsst_v1` @ `e302fc4` = GitHub `main`）**沒有 `dev2026/`**；API 程式只存在於 dev2026 branches／tags。
2. Production 實際執行 **`6b32e54`**：VM24 launcher 指向 `p5deploy/20260830T082136Z/checkout`（HEAD `6b32e54`，乾淨），18 個 api/store 模組 hash 與本機一致，generated OpenAPI 與公開 `openapi.json` 完全相同。
3. `6b32e54` 本機有完整 commit，GitHub 查無（commits API 422；42 個遠端 refs 皆不含）。本機工作不需先 push；遠端可取得列為 Stage G 發布準備。
4. `ee84037`（未部署）已改變快取秒數並加入 maintenance 503，不寫成現況（OD-2）。
5. 已驗證：OpenAPI 3.1.0、`info.version 0.4.1`、200 schema `{}`、400／413／503 完全未記錄；422 在 GET 與 POST 都引用 FastAPI 產生的 `HTTPValidationError`／`ValidationError`（02 §4 已逐欄轉錄）。
   503 有三種觸發（GET 兩種「無可用日」、GET/POST 共用的 overload），**只有 overload 帶 `Retry-After: 1`**；此結論最初來自程式碼；Stage D 已以本機 HTTP 層測試覆蓋（FastAPI TestClient 搭配模擬資料：空資料庫、0 permit executor），**未對 production 實測**（02 §5.3.1；03 Stage E 的 E3）。
6. 其他發現（詳 01）：point 模式日曆不存在的日期在基線回 500（round 5 BC-1 已改為 400）；NaN／Inf 座標在基線回 200＋邊緣格點（round 6 BC-2 已改為 422）；`spatial_window_days` 是保留目標值（31），欄位名稱易與實際窗口（目前 75 天）混淆；bbox 無日期時先取 daily latest 再檢查空間窗口、不會退回；座標不驗證而是 clamp；`/api/ghrsst` 沒有 CORS headers。
7. 資料日期窗口（round 7，owner 決定，OD-21）：空間查詢至少保證最新 31 個連續日曆日（終點＝最新 spatial-query 可用產品日期）、單點時間序列至少保證最新 366 個連續日曆日（終點＝最新 point-query 可用產品日期），窗口內每一天都可用、不缺日；更早日期可能可查但不保證、可能有缺口。P5 資料流程保障當時尚待確認；2026-11-20 P5 reviewer 確認為資料涵蓋政策與設計依據（非永久保證）。當時 F2 未核准（2026-11-24 已核准；02 §5.0）。
   Round 9（owner／Codex 2026-11-20 唯讀查核）：point 目前 2023-01-01..2026-11-18 連續、point／daily／spatial 最新日皆 2026-11-18——這是**現況**，OAS 以「currently」描述；「P5 未來保障」仍是待確認的另一件事。OAS 不再寫「point 與 spatial 最新日期可能不同」與「缺日略過」；BBox 無日期仍照實寫 daily 最新日＋不回退；freshness 只寫「typically lags 1–2 days」，不寫內部排程。
8. 正常的窗口外 POST 請求揭露了既有 proxy 快取設定的回應錯置：之後不同 body 收到相同舊 400（01 §10.3）。列為獨立 ops 待辦（OD-13），不阻擋本機工作；F2 前需確認公開端點正確。2026-11-20：修改前仍可重現；**owner** 修改並 reload NGINX 設定後，公開 POST spot check 通過（01 §10.4；未測 GET cache 行為）。
9. 舊 Draft `specs/ghrsst_api_v0_1_0.spec` 與現況至少 9 處矛盾，且 `AGENTS.md` 仍稱它 authoritative（OD-3）。
10. Round 11（2026-11-20，當時待 Codex review；其後 G-C/D 已通過）：`append` 在 GET 參數與 `PointsRequest` 加 `default: "sst"`（仍為 optional；`mode` 不變）；OAS 前後差異只有這兩處；另補 `components: null`／`[]` 的拒絕 regression tests（02 §11.7、03 Stage D）。
11. Round 12（2026-11-20，當時待 Codex review；其後 G-C/D 已通過）：日期窗口改為 owner 接受的簡潔用詞（已與 P5 Codex reviewer 討論）——Point「Point queries support the latest 366 consecutive calendar days ending on the latest date available to point queries. The archive currently starts on 2023-01-01.」；Spatial「Bounding-box queries support the latest 31 consecutive calendar days ending on the latest date available to spatial queries. The same window applies to `POST /api/ghrsst/points`.」。取代第 7 點在 OAS 中的「至少保證／不缺日／更早日期可能有缺口」寫法；366 天涵蓋窗口與單次 request 上限明確區分；P5 內部流程不寫進 OAS。只定案用詞，P5 資料流程保障與其他 F2 前置事項仍未完成（02 §5.0、§11.8；04 OD-21 round 12 列）。
12. F2 readiness（2026-11-20，owner 決定）：路徑固定、不加 `/v1`；`1.0.0` 為第一個正式契約版本，不設棄用期限，之後採 SemVer；OD-3（F2 時執行）、OD-6、OD-11、OD-16、OD-17 已決定，OD-12 屬 Stage G；OD-21 記錄 P5 reviewer 的政策／設計依據確認（非永久保證，無 P5 commit SHA）；F2 前不 commit，核准綁定已 review 的候選 diff；changelog 另立「OpenAPI contract v1.0.0」紀錄；X2 部署目標為 `6b32e54`＋API v1 候選變更（production 尚未包含）。Ops-1 只做離線整理、公開驗收待授權（03）——這是 **2026-11-20 當時的狀態**；後來的公開 POST spot check 結果見 Ops-1 驗收紀錄（01 §10.4、03「Ops-1 驗收結果」）。
13. F2（2026-11-24，owner 核准）：v1 契約 Approved；`info.version` `1.0.0`；snapshot 與版本測試同步；`change_log.md` 新增「OpenAPI contract v1.0.0」（已核准、尚未部署）；OD-3 已執行（舊 Draft SUPERSEDED、`AGENTS.md` 改指 v1）。當時尚未 commit（其後見第 14 點）；尚未部署（Stage G）。
14. 實作 commit（2026-11-24，owner 授權；Codex 已完成 F2 候選的最終 review）：**實作 commit SHA：`b9bd4e46097289632874d1615c5737c37ebc0156`**（parent `6b32e54`；15 個檔案：BC-1／BC-2、OpenAPI 契約、測試與 snapshot、`AGENTS.md`、舊 Draft SUPERSEDED 標示、`change_log.md`、v1 規格文件）。其後的文件狀態同步是另一個 commit，SHA 與實作 commit 不同。尚未部署（Stage G）；部署前須先處理 `6b32e54` 不在 GitHub 遠端歷史中的問題。

## Review gates
| Gate | 通過條件 | 誰 | 狀態（截至 2026-11-24） |
| --- | --- | --- | --- |
| G-A | 本機基線已決定並保存來源證據（OD-1、OD-2、OD-15） | Owner | 通過 |
| G-B | 00–05 經 Codex review，無未標出處的結論；schema 結構正確 | Codex | **通過**（Codex review，00–05 文件；2026-11-24 記錄） |
| G-F1 | owner 同意實作範圍（非對外承諾） | Owner | 通過 |
| G-C/D | metadata-only 修補 + 契約測試通過；既有失敗與新失敗分開報告；`git diff --check` 乾淨 | Claude → Codex | **通過**（Codex review，2026-11-20） |
| G-E | 規格 ↔ generated OpenAPI ↔ handler ↔ tests 無未說明差異 | Codex | **已完成**（150 tests passed；E4 proxy header 不入 OAS、由 Ops-1 處理；E5 改查本機 wiring，production Swagger smoke 移到 G；E6 用詞已統一，Codex 已確認（150 tests passed）；見 03） |
| G-F2 | owner 核准正式公開契約，綁定 Stage E 已 review 的候選 diff；核准後升 `info.version` → `1.0.0`（OD-20）並加 changelog，review 後才 commit、再補 SHA；公開 POST 正確性已確認（Ops-1） | Owner | **通過**（owner 核准 2026-11-24；不含 commit／push／部署授權）。已升 `info.version` → `1.0.0`、加 changelog 條目、執行 OD-3。Codex 最終 review 已完成；實作 commit `b9bd4e4`（見上方第 14 點） |
| G-G | 發布與部署獲獨立授權；部署目標 `6b32e54`＋API v1 候選變更（含 BC-1／BC-2，X2）；CORS（OD-12）在此規劃；部署後 production Swagger smoke（由 E5 移入） | Owner | 未開始 |

## 調查範圍紀錄
> 以下各條記錄的是**該輪當時**的狀態與做法，保留作歷史；目前狀態見本檔頂端與「Review gates」。

- Round 1（Claude）：本機 Git 物件唯讀調查；在 scratchpad 解出 `6b32e54` 快照並以隔離 env 產生 OpenAPI、執行 71 個 API 相關測試（OK）；VM24 唯讀 SSH（launcher、checkout HEAD、hash、`/healthz` loopback、NGINX 設定與 access log）；對公開 API 約 20 次唯讀請求。
- Round 2（Claude）：GitHub 唯讀查核（commits API、`git ls-remote`）；確認 venv 無 `jsonschema`；依 review 修訂文件。未再對 production 送 API 請求。
- Round 3（Claude）：依 round-1 保存的 generated OpenAPI（本機檔案）轉錄 422 schema；依 `6b32e54` 程式碼釐清 503 與 `Retry-After`；OpenAPI 範例移除無效佔位值。未連 VM24、未送 production 請求、未執行 API tests（本輪只改規劃文件）。
- Round 4（Claude，依 owner 2026-11-20 決定）：保存六份文件後把 `ghrsst_v1` fast-forward 到 `6b32e54`；在本 worktree 建立 gitignored venv；實作 Stage C（OpenAPI metadata）與 Stage D（契約測試），結果見 03；提出 BC-1／BC-2（05，未實作）；同步 00–04 與本檔。未連 VM24、未送 production 請求。
- Round 5（Claude，依 Codex round-4 review 與 owner 決定）：`format` 描述改為符合 trim＋小寫正規化（移除 enum）；實作 BC-1；BC-2 試作發現 POST 字面值 NaN／Infinity 會 500，停下回報（05）；同步文件與 snapshot。未連 VM24、未送 production 請求。
- Round 6（Claude，依 Codex round-5 review 與 owner 選定 B2-b）：實作 BC-2（座標非有限值 → 422、422 `input` 中非有限 float 字串化、一般 422 不變）；日期 schema 加 `maxLength: 10`；同步 01–05、README 與 snapshot。未連 VM24、未送 production 請求、未啟動 API server。
- Round 7（Claude，依 owner 2026-11-20 決定）：OAS 寫入資料日期窗口（空間至少最新 31 日、單點至少最新 366 日，P5 確認待完成）；移除全域 Contract notes，錯誤、headers、request caps、日期範例提醒放到對應 endpoint／response；GET 說明分段、移除「Mode」；參數與 `format` 說明精簡；POST 白話化。只改 metadata、文件、snapshot、測試。未連 VM24、未送 production 請求、未啟動 API server。
- Round 8（Claude，依 owner 決定）：OAS／文件寫明保證窗口內不缺日、缺日只限更早日期；point／spatial 窗口終點分別寫成各自的最新可用產品日期；小範圍 BBox 提醒移到 GET Bounding box 段；POST 413 fan-out example 加白話 summary。只改 metadata、文件、snapshot、測試。
- Round 9（Claude，依 owner 決定）：GET 說明改為 Coordinates → Query type → Point Query → Bounding-Box Query → Data freshness；point 目前覆蓋 2023-01-01（現況）＋366 日保證；移除「缺日略過」與「最新日期可能不同」；BBox `start`／`end` 單獨提供的描述；BBox 格點上限 example 改由 `cfg` 產生。只改 metadata、文件、snapshot、測試。未連 VM24、未啟動 API server。
- Round 10（Claude，依 owner 決定）：OAS 根層 `servers`（`https://eco.odb.ntu.edu.tw`，server object 只有 `url`；OD-14 已決定）；`operationId` 改為 `getGhrsstData`／`postGhrsstPoints`；兩個 operation `security: []` 並加一句免認證；不新增 security scheme、不設全域 `security`；`info.version` 當時仍 `0.4.1`（該輪當時 F2 未核准）。只改 metadata、文件、snapshot、測試。Claude 未執行任何 Swagger 或 production 請求、未啟動或重啟 Swagger／API server、未連 VM24。
  Round 10 修補（同日）：server object 移除 `description: Production`；`apply_contract` 在 FastAPI 原始文件出現認證需求時丟出 `OpenApiSecurityConflict`、拒絕產生文件（不改 API 認證行為；依 Codex review，只表示匿名存取的 `[{}]` 正規化為 `security: []`，與認證方案並列仍拒絕；`security: null`、錯誤型別 `securitySchemes` 等格式錯誤值依欄位存在與型別拒絕）；02 標頭改為 round 10；本段與頂端改寫為「Claude 本輪未啟動或重啟本機 server」。
- Round 12（Claude，依 owner 接受的用詞；已與 P5 Codex reviewer 討論）：GET Point／Bounding-Box 與 POST Date 的 Coverage 改為「support the latest N consecutive calendar days ending on the latest date available to … queries」；移除 guaranteed、no date missing、較早日期可能可用但不保證／可能有缺口；Request limit 明寫限制單次 request 長度、不是 Coverage 窗口；request cap 的「data-date guarantee」改為「data coverage window」；常數改名為 `SPATIAL_WINDOW_DAYS`／`POINT_WINDOW_DAYS`。只改 OAS 說明文字、snapshot、契約測試與文件；`info.version` 當時仍 `0.4.1`；當時未改 `change_log.md`。Claude 未執行任何 Swagger 或 production 請求、未啟動或重啟 Swagger／API server、未連 VM24。
- F2 readiness 紀錄更新（Claude，依 owner 2026-11-20 決定）：只改 02／03／04／README 的紀錄文字；Ops-1 依 owner 指示不做本機重現（不下載 image、不啟動 NGINX／container），只做離線整理與驗收計畫。未改 OAS 文字、snapshot、測試、API 行為、`info.version`、`change_log.md`；未 commit、push、部署；未連 VM24、未送 production 請求、未清快取、未改設定。
- Stage E 後續（Claude，依 Codex Stage E 結果）：GET Point Query 的 Dates 改為「the latest date available to point queries」（E6）；snapshot 與一個契約測試同步；03／README 記錄 Stage E 完成、E4／E5 的處理與 production Swagger smoke 移到 Stage G；02 §11.9。未改 handler 行為、`info.version`、`change_log.md`；未 commit、push、部署；未連 VM24、未送 production 請求。
- Stage E 紀錄同步（Claude，依 Codex 最終確認）：E6 改記為 Codex 已確認（150 tests passed）；03 檢查清單的 E1–E3 改為具測試依據的結論（E3：無可用日期 503 不帶 `Retry-After`、overload 503 帶 `Retry-After: 1`、POST 無資料日期回 400）。G-C/D、F2、Ops-1 狀態不變。只改 03／README／02 的紀錄文字；未改 OAS、snapshot、程式、測試；未 commit、push、部署；未連 VM24、未送 production 請求。
- G-C/D review 前置自查（Claude，2026-11-20）：修正過期紀錄——04 Stage E 改為 Codex 已完成；01 標頭的本機行號位移（BC-2 新增 46 行、BC-1 淨 +8）；README／02 的 BC-1 行數；02 §5.3.1 註明行號指基線；03 Stage C 驗收補記 `app.py` diff 的 hunk 歸屬；03 驗證紀律補記 Stage E 時 Codex 已重跑測試。G-C/D 當時仍待 Codex review（該輪當時狀態）。同輪依 owner 授權對公開 POST 做 Ops-1 spot check：**結果不符合預期，已停止**；Ops-1 紀錄未更新，證據只保存在本機 scratchpad，待 owner 決定。
- F2 升版（Claude，依 owner 2026-11-24 核准）：`app.py` 只改 `version=` 一行為 `1.0.0`；snapshot（只有 `/info/version` 改變）、版本測試、OD-3（舊 Draft 標示、`AGENTS.md`）、`change_log.md`「OpenAPI contract v1.0.0」（已核准、尚未部署）；02 → Approved（§11.10），03／04／05／README 狀態同步。該輪當時未 commit、push、部署；未送 production 請求；未碰 P5／O4。
- G-B 狀態同步（Claude，依 Codex G-B review）：README、03、04、05 的 G-B 由「未結案／是否為 F2 前置未決定」改為「已由 Codex review 通過」；該輪當時 F2 未核准、`info.version` `0.4.1` 不變。只改文件。
- OD-13 狀態同步（Claude）：04 OD-13 列改為「F2 前置的公開 POST spot check 已完成」（僅該請求序列；GET cache、其他快取情境與修改後 NGINX diff 未驗證，不列為 F2 阻擋項）；03 的離線驗收計畫標為當時擬定，第 4–5 項不列為 F2／發佈阻擋項。只改文件。
- Review 狀態對齊（Claude，依 Codex review）：05 與 03 的 BC-1／BC-2 改為已隨 G-C/D 經 Codex review 通過；02 §7 的 Ops-1 前置改為已完成（只限該 POST 序列、未測 GET cache）；01 §10.4 移除矛盾的根因斷言，改記 Codex 唯讀確認的修改前設定缺陷足以解釋本次錯置、owner 修改後的 diff 未檢視；G-B 記為未結案、是否為 F2 前置未決定，不再寫 F2 只剩 owner 核准；round 11／12 等歷史狀態標為「當時」。只改文件；未改 API、OAS、snapshot、測試；未送 production 請求。
- Ops-1 驗收紀錄（Claude，依 owner 提供的結果）：記錄修改前 spot check（13:06 UTC，錯置仍可重現）、**owner** 修改並 reload VM24 NGINX（修改前檢視 `/etc/nginx/conf2.d/api_cache_proxy.conf`），以及修改後 15:09 UTC 三筆公開 POST 序列通過；未測 GET cache。G-C/D 記為 Codex review 通過；該輪當時 F2 仍待 owner 書面核准。只改 01／03／04／README 的紀錄文字；Claude 本輪未送 production 請求、未連 SSH／VM24、未改設定、未清快取、未 reload、未 commit／push。
- 503 證據紀錄同步（Claude，依 Codex review）：README 第 5 點、02 §5.3.1 與 §6 POST 503、03 Stage D 第 5 項、01 §12 的 503 證據改記為「已由 Stage D 本機 HTTP 層測試覆蓋（TestClient＋模擬資料），未對 production 實測」；01 保留盤點當時的原文並加後續註記。只改紀錄文字；未改 OAS、snapshot、程式、測試；該輪當時 G-C/D 待 review、F2 未核准、Ops-1 待驗收的狀態不變。
- 實作 commit 與文件同步（Claude，2026-11-24，依 owner 授權與 Codex review）：逐檔暫存、建立本機實作 commit `b9bd4e4`（15 個檔案，工作樹乾淨）；其後把 README／02／03／04／05 的目前狀態改為已由 Codex review、已 commit、尚未部署，並明確記錄 `b9bd4e4…` 為實作 commit SHA（文件同步 commit 有自己的 SHA）；修正 03 指向 02 §5.0 的錨點。未改 API、OAS、snapshot、測試、`change_log.md`；未部署、未連 VM24、未碰 P5／O4。
- Round 1–3 未做：修改程式／測試／設定、commit、push、merge、cherry-pick、切換 branch。
- 全程未做（實作 commit 與其後的文件同步 commit 除外，兩者皆經 owner 授權／Codex review）：push、cherry-pick、碰觸 `/Users/cywhale/proj/ghrsst` working tree 或 O4 worktree（含 VM24 的 O4 候選 checkout）、修改 production 設定或 NGINX、清除快取、重啟或部署。
