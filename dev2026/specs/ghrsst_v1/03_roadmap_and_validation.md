# 03 — Roadmap 與驗證計畫

> 狀態（截至 2026-11-25）：**G-C/D 已通過 Codex review（2026-11-20）；Stage E 已由 Codex 完成，E6 用詞統一 Codex 已確認；Ops-1 公開 POST spot check 通過（2026-11-20，owner 修改 NGINX 設定後；未測 GET cache）；G-B 已由 Codex review 通過；**F2 已由 owner 核准（2026-11-24）**；`info.version` 已改為 `1.0.0`；Codex 最終 review 已完成；實作 commit SHA：`b9bd4e46097289632874d1615c5737c37ebc0156`（parent `6b32e54`；其後的文件同步 commit 另有自己的 SHA）；基線 `6b32e54` 已由 owner 發布為 GitHub branch `dev2026-api-v1-base-6b32e54`（SHA 未改寫；非新的 F2 核准）；API branch 尚未 push、未開 PR；尚未部署（部署未授權；部署準備清單見「Stage G」）。** **Round 12（2026-11-20）— A 完成、F1 已同意、C／D 已實作；round 5–10 修補（`format`、BC-1、BC-2、日期 `maxLength`、資料日期窗口、Swagger 說明重整、窗口連續性用詞、GET 說明層級／目前覆蓋／freshness、`servers`／`operationId`／`security: []`；round 11 `append` 的 `default: "sst"`；round 12 日期窗口簡潔用詞）當時待 Codex review（其後 G-C/D 已通過）。** **F2 readiness 更新（2026-11-20）**：v1 契約政策已由 owner 決定（04 決定紀錄）；OD-21 已記錄 P5 reviewer 對政策／設計依據的確認（**不是**永久保證）；E（Codex）與 Ops-1（ops 驗收）當時未完成（其後皆已完成，見本行開頭）；當時 **F2 未核准**（2026-11-24 已核准）；G 未開始。見下方「F2 readiness」。
> 角色：Owner/Orchestrator = Human；文件與實作 = Claude；Review = Codex；部署 = **未定**。
> Round 2 變更：核准拆成 F1（實作範圍）／F2（公開契約），消除「實作需核准、核准需實作」的循環；
> 基線只需本機完整 commit，推上 origin 改列為發布準備；快取問題改為不阻擋本機工作的獨立 ops 待辦。

## 流程總覽

```text
A 基線決策（本機） ─┐
B 文件 review ──────┼─> F1 owner 同意實作範圍 ─> C metadata 修補 ─> D 契約測試 ─> E 一致性 review ─> F2 owner 核准公開契約 ─> G 發布／部署規劃與授權
Ops-1 快取（獨立）───────────────────────────────────────────────────────────────────────────────┘（F2 前確認公開 POST 正確）
```

## 進度（round 4 起；截至 2026-11-24）

| Stage／Gate | 狀態 | 依據 |
| --- | --- | --- |
| A | **完成**：owner 決定 OD-1＝`6b32e54`、OD-2、OD-15；`ghrsst_v1` 已 fast-forward 到 `6b32e54` | [00 §7](00_baseline_and_provenance.md#7-round-4-本機基線調整紀錄2026-11-20) |
| B | **G-B 已由 Codex review 通過**（00–05 文件；2026-11-24 記錄） | 本目錄 |
| F1 | **已同意**（實作範圍；不是對外宣告 v1.0.0） | [04](04_owner_decisions.md) |
| C | **已實作；G-C/D Codex review 已通過**（2026-11-20） | [02 §11](02_openapi_v1_spec.md#11-stage-c-實作對照round-4) |
| D | **已實作；G-C/D Codex review 已通過**（2026-11-20）；結果見下方 Stage D | 本文件 |
| 行為變更 | BC-1（OD-4）：**已核准、已實作**（round 5）。BC-2（OD-5）：**owner 選 B2-b、已實作**（round 6）。兩者已隨 G-C/D 經 Codex 正式 review 並通過（2026-11-20） | [05](05_behavior_change_proposals.md) |
| 資料日期窗口（OD-21） | owner 選定空間 31 日／單點 366 日最小保證；已寫入本機 OAS 草案（round 7）；round 9 另把 point「目前自 2023-01-01 起連續」寫成現況（非承諾）；round 12 公開用詞改為 owner 接受的簡潔寫法（「support the latest N consecutive calendar days」，已與 P5 Codex reviewer 討論）。**F2 readiness**：已收到 P5 reviewer 確認 366／31 日為資料涵蓋政策與設計依據（owner 於對話中提供；無 P5 commit SHA）；不是無條件或永久保證 | [02 §5.0](02_openapi_v1_spec.md#50-資料日期窗口round-7owner-決定-2026-11-20) |
| E | **Codex 已完成**（2026-11-20：150 tests passed；`git diff --check` 與 untracked whitespace 乾淨）；依結果統一 E6 用詞，**Codex 已確認**（最終確認 review，150 tests passed） | 下方「Stage E」與「F2 readiness」 |
| F2 | **已核准**（owner，2026-11-24）。前置全部完成：v1 政策（04）、G-B、G-C/D、E、Ops-1 公開 POST spot check。核准後已升 `info.version` → `1.0.0`、snapshot／版本測試、OD-3、changelog 條目；Codex 最終 review 已完成，實作 commit SHA：`b9bd4e46097289632874d1615c5737c37ebc0156`（2026-11-24） | 下方 |
| G | 未開始；CORS（OD-12）、`app.py` 整合（X2）與部署後 production Swagger smoke 屬此階段 | 下方 |
| Ops-1 | **公開 POST spot check 通過**（2026-11-20 15:09:11–15:09:22 UTC，owner 修改並 reload NGINX 後）；未測 GET cache 行為，未宣稱驗證所有快取情境 | 下方「Ops-1 驗收結果」；01 §10.4 |

## Ops-1（獨立 ops 待辦，不阻擋 A–E）— POST proxy 快取回應錯置

| 項目 | 內容 |
| --- | --- |
| 現況 | 2026-11-19 觀察到：不同 body 的 POST `/api/ghrsst/points` 在公開端點收到相同的舊 400，而 upstream 對新 body 回 200（01 §10.3）。2026-11-20 修改前仍可重現；owner 修改並 reload NGINX 設定後，公開 POST spot check 通過（01 §10.4、下方「Ops-1 驗收結果」） |
| 可分開決定的事 | (1) 是否清除特定 cache entry；(2) 是否修改設定，以及修法；(3) 處理時間 |
| 修法注意 | 若選「POST 不快取」，需同時涵蓋**讀取**（`proxy_cache_bypass`）與**寫入**（`proxy_no_cache`），或把 POST 從 `proxy_cache_methods` 移除；只加 `proxy_no_cache` 仍可能讀到既有 entry。另一方案是把 body 納入 cache key（需評估） |
| 驗收 | 在 F2 之前：對公開 URL 交替送出窗口內與窗口外 body，各自得到正確結果 |
| 角色 | Owner 決定；ops 執行；Claude 不執行 |

### Ops-1 離線整理（2026-11-20，F2 readiness）

Owner 指示：不下載 Docker image、不安裝或啟動 NGINX／container，取消本機重現與候選設定驗證（本機重現不是 F2 readiness 的必要工作，也不能代替公開端點驗收）。以下只根據現有紀錄整理；沒有連 VM24、沒有送 production 請求、沒有清快取或改設定。

**現有資料**

| 來源 | 內容 | 限制 |
| --- | --- | --- |
| 01 §10.2（[P]，VM24 唯讀檢查時記錄的指令摘要） | `proxy_cache_methods GET POST`；`proxy_cache_key "$http_host$request_uri"`（不含 body）；`proxy_cache_valid 400 404 1m`；`proxy_cache_use_stale ... updating`；`proxy_cache_background_update on`；`proxy_no_cache`／`proxy_cache_bypass` 只對大於 50k、寫入暫存檔的 body 生效；POST 永不 bypass；`X-api-cache` 未加 `always` | 只是摘要，本機**沒有** `api_cache_proxy.conf` 原檔；include 鏈其他層、server 層設定未逐行保存 |
| 01 §10.3（2026-11-19 實測） | 窗口外日期 400 之後，約 100 秒後送窗口內日期仍收到舊 400；同 body 經 loopback → 200；URL 加 `?_probe=` → 200 | 單次觀察；持續時間、影響範圍未知；access log 中只有本調查的 4 筆 POST |
| 01 §10.1 | 成功的 POST 回 `Cache-Control: no-store`；錯誤回應沒有 `Cache-Control` | 錯誤回應因此依 `proxy_cache_valid 400 1m` 被快取 |
| `dev2026/bench/probe_nginx_cache.py`（既有） | 可對指定 base URL 跑 GET／POST 快取檢查；POST-3 送兩個不同點集合的**成功**請求比對是否互相錯置 | **不涵蓋**本次事故型態（先 400、後窗口內）；4xx 回應沒有 `X-api-cache`，只能靠 body（`requested_day`）判斷；會打 live server，執行需授權 |

**機制推論（未驗證）**：錯誤回應被快取 1 分鐘；過期後若 `use_stale updating`＋`background_update` 在背景更新期間回傳舊 entry，而背景更新拿到的是 `no-store` 的 200（不會寫回快取），舊 400 entry 可能一直維持「過期但可當 stale 回傳」的狀態。這可以解釋超過 1 分鐘仍收到舊 body，但目前**沒有證據**，只能由隔離環境重現或 ops 驗證確認。

**候選修法（供 owner／ops 選擇，皆未驗證）**

| 修法 | 效果 | 注意 |
| --- | --- | --- |
| (a) 從 `proxy_cache_methods` 移除 POST（建議） | POST 不再查詢也不寫入快取；GET 不受影響 | 依 NGINX 文件語意，舊的 POST entry 不會再被讀到，未被存取 600 分鐘後由 `inactive` 移除；仍需驗收確認 |
| (b) 對 POST 同時設定 `proxy_cache_bypass`（讀）與 `proxy_no_cache`（寫） | 同 (a) | 只加 `proxy_no_cache` 仍可能讀到既有 entry |
| (c) 把 body 納入 cache key | POST 可被快取 | 需評估 body 大小、正規化與記憶體；不建議 |

**驗收計畫（離線整理當時擬定；需 owner 授權，由 ops 在維護時段執行；當時未執行）**——其後 owner 約定的 F2 前置範圍是公開 POST spot check（第 2–3 項的 POST 部分），已於 2026-11-20 完成（下方「Ops-1 驗收結果」）；第 4–5 項與多輪／直連對照不列為 F2 或發佈阻擋項。
1. 前置：備份設定；`nginx -t` 通過後 reload；記錄設定 diff 與時間。是否清除既有 POST entry 由 owner 決定（採 (a) 時通常不需要）。
2. 從公開 URL 依序送小 body、低頻率：窗口外日期（預期 400，`requested_day` 等於送出日期）→ 窗口內日期（預期 200）→ 間隔超過 1 分鐘再送窗口內 → 換一組點位 → 再送窗口外。至少 3 輪。
3. 每筆比對：回應的 `requested_day`／`date`／座標與送出的 body 一致；與 VM24 loopback（繞過 NGINX）結果一致；成功的 POST 不出現 `X-api-cache: HIT`。4xx 沒有 `X-api-cache`，以 body 判斷。
4. GET 回歸：固定日期 GET 仍可快取、無日期 GET 仍為 `no-store`（可用 `probe_nginx_cache.py` 的 GET 檢查）。
5. 若要沿用 `probe_nginx_cache.py` 作為 gate，需先補「先 400、後窗口內」的檢查（另案，需 owner 同意修改）。
6. 結果寫入 01 §10.3 與本節；全部通過才算 Ops-1 驗收（離線整理當時的標準；目前的 F2 前置以公開 POST spot check 為準，見上方說明）。

**狀態（離線整理當時）**：未修正、未驗收；待 owner 授權的 ops 驗證。其後結果見下節。

### Ops-1 驗收結果（2026-11-20）

| 項目 | 內容 |
| --- | --- |
| 修改前 spot check（13:06 UTC；Claude 依 owner 授權，未改設定、未清快取） | 有效 200 → 窗口外 400 → 不同 body 的有效請求收到**第二筆日期（2024-01-15）的舊 400**。不符預期，立即停止，未重試 |
| 設定修改 | **owner** 檢視 VM24 的 `/etc/nginx/conf2.d/api_cache_proxy.conf` 後修改並 reload NGINX。Claude／Codex 未連 VM24、未讀取或修改設定、未清快取、未 reload。修改內容未提供給 Claude，不推論採用哪個候選修法 |
| 修改後 spot check（owner 提供的結果；同一路徑、無 query string／cache-buster） | 15:09:11 UTC `2026-11-19`、1 點 → 200、1 列，符合請求；15:09:17 UTC `2024-01-15`（窗口外）→ 400，`requested_day` 正確；15:09:22 UTC `2026-11-19`、不同的 2 點 → 200、2 列，`index` 0、1，日期符合請求 |
| 結論 | **公開 API spot check 通過**：修改前同型態的第三筆收到舊 400，修改後序列通過 |
| 界線 | 只證明這個請求序列的公開 POST body 正確性；**未測 GET cache 行為**；未驗證其他快取情境（間隔超過 1 分鐘、多輪交替、直連對照）；根因：修改前設定的缺陷（POST 可快取、cache key 不含 body、400 快取 1 分鐘；Codex 先前唯讀確認）足以解釋本次錯置（01 §10.4）；owner 修改後的設定 diff 未檢視。上方驗收計畫的第 4–5 項（GET 回歸、probe 腳本補強）未執行，不列為 F2 或發佈阻擋項 |

## Stage A — 基線決策（本機）

| 項目 | 內容 |
| --- | --- |
| 產出 | owner 對 OD-1（B1–B4）、OD-2（`ee84037`）、OD-15（`app.py` ownership）的決定；本機 branch 安排（`ghrsst_v1` 重指或另開本機 branch） |
| 依賴 | 00 文件 |
| 驗收 | 選定的本機完整 commit 可 `git cat-file -e`；其 `dev2026/api/app.py` sha256 與 VM24 serving 相符（或明確記錄差異）；O4 commits 不在其歷史中（`git merge-base --is-ancestor 93625b3 <base>` 為 false）；來源與重現證據保存在 00 |
| 不要求 | 推上 origin（列入 Stage G 發布準備；其後基線已由 owner 發布為 `dev2026-api-v1-base-6b32e54`，見 Stage G） |
| 角色 | Owner 決定；Claude 驗證並更新 00；Codex review |

## Stage B — 現況盤點與 SPEC review

| 項目 | 內容 |
| --- | --- |
| 產出 | 本目錄 README、00–04；依 review 修訂的各 round |
| 依賴 | 可與 A 平行；A 決定後重新確認基線欄位 |
| 驗收 | Codex 確認：01 每項結論都有程式／測試／實測出處；未把其他 branch 行為寫成現況；02 所有行為變更都標成 OD；schema 結構正確（無重疊 `oneOf`、無對封閉 schema 的 `allOf` 擴充） |
| 角色 | Claude 撰寫；Codex review；Owner 回覆 04 |

## Gate F1 — Owner 同意實作範圍

| 項目 | 內容 |
| --- | --- |
| 產出 | owner 同意以 02（當時 round）作為 Stage C／D 的實作範圍；列出 C 範圍內必須先有結論的 OD（至少 OD-7、OD-8、OD-9、OD-10、OD-14 的文件寫法，OD-18 的記錄方式） |
| 性質 | **不是**對外承諾；02 仍為 Draft |
| 依賴 | A、B |

## Stage C — 最小契約修補（只動 OpenAPI metadata）

| 項目 | 內容 |
| --- | --- |
| 產出 | 在選定本機基線上的變更：兩個 operation 的 responses（400/413/422/503 + headers + examples）；`components` 中 02 §3–§4 的 schemas；參數說明修正；過期範例更新。未決的 `contact`／`license`／`termsOfService`／`servers` **不得**以佔位值寫入 generated OpenAPI（02 §2）。`info.version` 在 C **不**改（維持 `0.4.1`），改為 `1.0.0` 屬 F2（OD-20） |
| Round 4 實作 | `dev2026/api/openapi_contract.py`（新）＋ `dev2026/api/app.py` 檔尾 9 行；細節見 02 §11 |
| 不包含 | 任何行為變更。OD-4/5/6/7/12/17 等若被核准，**各自另做**，並附行為前後對照。舊 Draft 橫幅與 `AGENTS.md` 修正留到 F2（OD-3） |
| 依賴 | F1 |
| 驗收 | `git diff` 只觸及 OpenAPI metadata（round 4：新模組＋ `app.py` 檔尾的 install 呼叫）、新測試、測試依賴檔與文件；行為測試全數維持（Stage D） |
| Round 5–6 之後的 `app.py` diff（2026-11-20 自查） | 5 個變更區塊（`git diff -U0`）：檔尾 9 行 install hook（Stage C metadata）；`_parse_date` 1 行改為 9 行（BC-1）；overload handler 後新增 46 行 422 handler、4 個座標 `Query` 加 `allow_inf_nan=False`、`PointsRequest.points` 改 `FiniteFloat`（BC-2）。BC-1／BC-2 是 owner 核准的行為變更（05），不屬 metadata-only 範圍，G-C/D review 時應依 hunk 分開審查 |
| 角色 | Claude 實作；Codex review |

## Stage D — Schema regression tests 與相關 API tests

新增測試（建議檔名 `dev2026/tests/test_api_v1_contract.py`）：
1. **路由不變**：generated OpenAPI 的 `paths` 與 methods 集合 == `{GET /api/ghrsst, POST /api/ghrsst/points}`；docs／openapi URL 不變。
2. **OpenAPI snapshot**：generated `openapi.json` 與 repo 中的 snapshot 比對（變更需同時更新 snapshot 並經 review）。
3. **Response ↔ schema**：用 TestClient + synthetic fixtures 打出每一種可達回應（point 200、range 200 含省略欄位、bbox 200 含 null 欄位、POST 200、各種 400/413/422/503），依 generated OpenAPI 驗證 body。
   - 注意：現有 venv 沒有 `jsonschema`（2026-11-19 確認；`openapi_schema_validator` 亦無）。新增測試依賴（dev-only）屬實作決策，需在 F1 時一併同意；若不新增依賴，改以手寫的結構檢查實作（較弱，需在報告中註明）。
   - 驗證必須能偵測本輪 review 指出的兩類 schema 錯誤：同一 body 同時符合多個 `oneOf` 分支；封閉 schema 拒絕合法欄位。
4. **模式語意**：point/range 中不存在的欄位**被省略**；bbox 與 POST 中**為 null**（schema 不區分，由測試驗證，OD-10）。
5. **Headers**：`Cache-Control` 規則（無日期／範圍觸及 latest／固定日期）、`X-Store-Route` 值、bbox 的 `X-Served-Rows`/`X-Stride`/`X-Bbox-Format`；
   503 分情境驗證（02 §5.3.1、§6）：overload 503 帶 `Retry-After: 1`；GET 的兩種「無可用日」503 **不帶** `Retry-After`。（計畫當時三者只有程式碼證據。）**已由 Stage D 測試覆蓋**：本機 TestClient 搭配模擬資料（空資料庫、0 permit executor）的 HTTP 層測試 `test_no_available_dates_503s_have_no_retry_after`、`test_overload_503_has_retry_after`，另以 `test_post_with_no_data_is_400_not_503` 確認 POST 無資料日期回 400；未對 production 實測。
   422：GET 與 POST 的 generated OpenAPI 都引用 `#/components/schemas/HTTPValidationError`，且其結構與 02 §4 轉錄一致（snapshot）。
6. **既有行為釘住**：01 §15 列出的未覆蓋項目（日曆不合法日期、座標 clamp、跨換日線、fan-out 413、`spatial_window_days`、bbox 無日期時 daily latest 不在 delta → 400 而不退回），先以「現況」寫成測試；若 OD 決定改變，再於該 OD 的變更中改測試。
7. 不依賴網路、VM24 或真實資料。

| 項目 | 內容 |
| --- | --- |
| 依賴 | C（或與 C 同一變更） |
| 驗收 | 新測試通過；既有 API 相關測試（至少 01 §15 的 7 個模組）通過；`git diff --check` 乾淨。**基線既有失敗與新失敗分開報告**：先在未修改的基線上跑一次並記錄，再跑修改後版本 |
| 角色 | Claude 實作與執行；Codex 獨立重跑並 review |

Round 4 實作與結果（Claude 執行；當時 Codex 尚未重跑）：
- 測試檔 `dev2026/tests/test_api_v1_contract.py`（44 tests）與 snapshot `dev2026/tests/snapshots/openapi_v1.json`（以 production 設定產生）。
  更新 snapshot：`GHRSST_UPDATE_OPENAPI_SNAPSHOT=1`，更新後需 review diff。
- 測試依賴：`dev2026/requirements-test.txt`（`jsonschema>=4.18,<5`，dev/test-only；根目錄 runtime `requirements.txt` 未改）。沒有 `jsonschema` 時 44 個測試全部 **skip**（附原因），不會假裝通過。
- 測試不重載 `api.app`：以暫時覆寫共用 `cfg` 與 `GHRSST_*` env 的方式設定，結束後還原，避免替換其他測試模組已 import 的類別。
- 驗證 response 的方式：以 TestClient 打出 handler 的實際回應，再用 generated OpenAPI 中該 operation、該 status 的 schema 以 Draft 2020-12 驗證；未記錄的 status 直接失敗；`required` header 必須存在。

| 執行（`dev2026/.venv`，`TMPDIR`=scratchpad） | 結果 |
| --- | --- |
| 未修改的 `6b32e54`：7 個既有模組（01 §15） | 71 tests OK |
| 修改後：同 7 個模組 | 71 tests OK |
| 修改後：契約測試單獨 | 44 tests OK |
| 修改後：契約測試＋7 模組，兩種順序（契約在前／在後） | 115 tests OK（兩種順序皆是） |
| 修改後：`test_phase2_p4s3gate`（會 reload `api.app`）→ 契約測試 | OK |
| 修改後：shell 帶 `GHRSST_MAX_DAYS=10`、`GHRSST_SPATIAL_WINDOW_ENFORCE=1` | 44 tests OK（結果不受開發者 env 影響） |
| 阻擋 `jsonschema` import | 44 skipped |
| 變異檢查：在記憶體中把契約改錯（`DataRow.lat` 型別、`spatial_window_days` 型別、POST `Retry-After` 改 required、移除 POST 413） | 20 個測試失敗（確認測試能抓到錯誤契約） |

Round 5 實作與結果（Claude 執行；當時 Codex 尚未重跑；2026-11-20）：
- 三個區塊：metadata（`format` 描述）、BC-1（`_parse_date`）、BC-2（未實作）。檔案與目的見 [02 §11.1](02_openapi_v1_spec.md#111-round-5-變更2026-11-20三個可分開審查的區塊)；行為前後對照見 [05](05_behavior_change_proposals.md)。
- 契約測試由 44 增為 49：+2 format（schema 不含 enum；`json`／`JSON`／前後空白／空值在 point 與 bbox 皆 200，`xml`／` coveragejson `／`raster`／空白字串 400），
  +3 BC-1（所有路徑 400、合法日期不變、enforce=1 時 bbox／POST 的 body 由 `SpatialWindowError` 改為日期錯誤字串），+1 「不記錄 500」，−1 舊的 500 釘住測試。Snapshot 已更新（差異只在 `format` 描述與 enum、`Date`／`start` 說明、兩個 example summary）。

| 執行（`dev2026/.venv`，`-W default`，`TMPDIR`=scratchpad） | 結果 | Warnings |
| --- | --- | --- |
| 未修改的 `6b32e54` 快照：7 個既有模組 | 71 OK | 1 種（StarletteDeprecationWarning：httpx → httpx2），pre-existing |
| 修改後：7 個既有模組 | 71 OK | 同上 |
| 修改後：契約測試單獨 | 49 OK | 同上 |
| 修改後：7 模組＋契約，兩種順序 | 120 OK（兩種順序皆是） | 同上 |
| 修改後：`test_phase2_p4s3gate` → 契約 | 53 OK | 同上 |
| 修改後：shell 帶 `GHRSST_MAX_DAYS=10`、`GHRSST_SPATIAL_WINDOW_ENFORCE=1` | 49 OK | 同上 |
| 阻擋 `jsonschema` import | 49 skipped | — |
| 反向對照：新測試對「BC-1 前」的程式副本 | 22 failures（BC-1 測試與 snapshot） | — |
| 反向對照：新測試對「round 4」程式副本（含 `enum`） | 23 failures（另含 enum 測試） | — |

（round 5 當時）BC-2 的試作只在 scratchpad 拋棄式副本進行；round 6 依 owner 選定的 B2-b 實作於 worktree（見下）。

Round 6 實作與結果（Claude 執行；當時 Codex 尚未重跑；2026-11-20）：
- 兩個區塊：metadata（日期 `maxLength: 10`）、BC-2（B2-b）。檔案與目的見 [02 §11.2](02_openapi_v1_spec.md#112-round-6-變更2026-11-20兩個可分開審查的區塊)；前後對照與協調事項見 [05](05_behavior_change_proposals.md)。
- 契約測試由 49 增為 58：+2 日期 schema（generated `Date`／`start`／`end` 拒絕 `2025-01-01\n`，API 回 400），+7 BC-2。Snapshot 已更新（差異：3 處 `maxLength: 10`；座標、`points`、operation 與 `info` 的說明文字）。
- 一般 422 不變的證據：15 種一般 422（缺參數、非數字、型別錯、JSON 解析失敗、空 body、陣列 body 等）修補前後的 status、content-type、content-length、body sha256 完全相同；測試另以 4 種 body 位元組比對與 handler 單元比對釘住。

| 執行（`dev2026/.venv`，`-W default`，`TMPDIR`=scratchpad） | 結果 | Warnings |
| --- | --- | --- |
| 未修改的 `6b32e54` 快照：7 個既有模組 | 71 OK | 1 種（StarletteDeprecationWarning：httpx → httpx2），pre-existing |
| 修改後：7 個既有模組 | 71 OK | 同上 |
| 修改後：契約測試單獨 | 58 OK | 同上 |
| 修改後：7 模組＋契約，兩種順序 | 129 OK（兩種順序皆是） | 同上 |
| 修改後：`test_phase2_p4s3gate` → 契約 | 62 OK | 同上 |
| 修改後：shell 帶 `GHRSST_MAX_DAYS=10`、`GHRSST_SPATIAL_WINDOW_ENFORCE=1` | 58 OK | 同上 |
| 阻擋 `jsonschema` import | 58 skipped | — |
| 反向對照：新測試對「BC-2 前」（round 6 metadata 後）的程式副本 | BC-2 測試全部失敗（另 snapshot）；一般 422 不變與有限值 clamp 測試照常通過 | — |
| 反向對照：新測試對 round 5 程式副本 | 另加日期 `maxLength` 的 2 個測試失敗 | — |
| `app.py` 對 P5 三個版本（`ee84037`、`d7ba064`、`1003b25`）的三方合併（`git merge-file`） | 0 衝突，合併結果可解析 | — |


Round 7 實作與結果（Claude 執行；當時 Codex 尚未重跑；2026-11-20）——只改 OpenAPI metadata、文件、snapshot 與測試：
- 內容見 [02 §11.3](02_openapi_v1_spec.md#113-round-7-變更2026-11-20只改-openapi-metadata)。`app.py`、`requirements-test.txt` 本輪未改（以檔案比對確認）。
- Snapshot：忽略 `description`／`summary` 後與 round 6 完全相同（schema 結構、型別、必填、status、header 宣告、路由皆未變）。
- 契約測試由 58 增為 65：+7（兩個窗口的文字與 endpoint 對應、窗口不隨 request cap 變動、無全域 Contract notes、Swagger tags／UI 設定不變、GET 說明分段順序、規則只寫一次且無 `-> 4xx` 簡寫、request caps／headers／錯誤說明位置）；另改寫 3 個測試以符合新文字（422 只檢查 description 原因與不變的 `$ref` content、`format` 精簡說明、cap 數值）。

| 執行（`dev2026/.venv`，`-W default`，`TMPDIR`=scratchpad） | 結果 | Warnings |
| --- | --- | --- |
| 未修改的 `6b32e54` 快照：7 個既有模組 | 71 OK | 1 種（StarletteDeprecationWarning），pre-existing |
| 修改後：7 個既有模組 | 71 OK | 同上 |
| 修改後：契約測試單獨 | 65 OK | 同上 |
| 修改後：7 模組＋契約，兩種順序 | 136 OK（兩種順序皆是） | 同上 |
| 修改後：`test_phase2_p4s3gate` → 契約 | 69 OK | 同上 |
| 修改後：shell 帶 `GHRSST_MAX_DAYS=10`、`GHRSST_SPATIAL_WINDOW_ENFORCE=0` | 65 OK | 同上 |
| 阻擋 `jsonschema` import | 65 skipped | — |
| 反向對照：新測試對 round 6 文字副本 | 10 個失敗（新增／改寫的文字測試與 snapshot）；Swagger 群組不變測試照常通過 | — |

Round 8 實作與結果（Claude 執行；當時 Codex 尚未重跑；2026-11-20）——只改 OpenAPI metadata、文件、snapshot 與測試：
- 內容見 [02 §11.4](02_openapi_v1_spec.md#114-round-8-變更2026-11-20只改-openapi-metadata)。`app.py` 自 round 6 起未改（檔案比對確認）。
- Snapshot：去掉 `description`／`summary` 後與 round 7、round 6 皆相同。
- 契約測試由 65 增為 68：+3（全文件中提到 skipped／gap／missing 日期的句子都必須限定為較早日期或是窗口承諾本身；兩個窗口終點分別寫成 point／spatial 各自的最新可用產品日期、無日期 BBox 不回退；POST fan-out example 保留 runtime body 並有白話 summary）；改寫 2 個（窗口完整句子、`info.description` 不含 Swagger 提示且提示在 Bounding box 段）。

| 執行（`dev2026/.venv`，`-W default`，`TMPDIR`=scratchpad） | 結果 | Warnings |
| --- | --- | --- |
| 未修改的 `6b32e54` 快照：7 個既有模組 | 71 OK | 1 種（StarletteDeprecationWarning），pre-existing |
| 修改後：7 個既有模組 | 71 OK | 同上 |
| 修改後：契約測試單獨 | 68 OK | 同上 |
| 修改後：7 模組＋契約，兩種順序 | 139 OK（兩種順序皆是） | 同上 |
| 修改後：`test_phase2_p4s3gate` → 契約 | 72 OK | 同上 |
| 修改後：shell 帶 `GHRSST_MAX_DAYS=10`、`GHRSST_SPATIAL_WINDOW_ENFORCE=0` | 68 OK | 同上 |
| 阻擋 `jsonschema` import | 68 skipped | — |
| 反向對照：新測試對 round 7 文字副本 | 6 個失敗；矛盾檢查抓到 round 7 未限定的「Dates without data are skipped.」 | — |

Round 9 實作與結果（Claude 執行；當時 Codex 尚未重跑；2026-11-20）——只改 OpenAPI metadata、文件、snapshot 與測試：
- 內容見 [02 §11.5](02_openapi_v1_spec.md#115-round-9-變更2026-11-20只改-openapi-metadata)。`app.py` 自 round 6 起未改（檔案比對確認）。
- Snapshot：去掉 `description`／`summary` 後與 round 8 相同；所有 example 值也相同（`bbox_too_large` 的上限改由 `cfg` 產生，production 設定下仍是 300000）。
- 契約測試由 68 增為 72：+4（BBox／Point 日期描述與 `start`／`end` 參數說明；point 目前覆蓋 2023-01-01 只出現一次且不寫成永久；freshness 句子逐字、每個 operation 一次、無排程字眼；BBox 格點上限在 300000／1000000／12345 三種設定下都跟著 `cfg` 走、文件中不出現另一個值）；
  改寫 6 個（窗口句子與所在段落、不寫「can differ」、版面順序 Coordinates → Query type → Point Query → Bounding-Box Query → Data freshness 且各段 Dates／Coverage／Request limit 順序、全文件無「skipped」、Swagger 提示只出現一次且在 Bounding-Box 段、range cap 測試加 2023 覆蓋句）。

| 執行（`dev2026/.venv`，`-W default`，`TMPDIR`=scratchpad） | 結果 | Warnings |
| --- | --- | --- |
| 未修改的 `6b32e54` 快照：7 個既有模組 | 71 OK | 1 種（StarletteDeprecationWarning），pre-existing |
| 修改後：7 個既有模組 | 71 OK | 同上 |
| 修改後：契約測試單獨 | 72 OK | 同上 |
| 修改後：7 模組＋契約，兩種順序 | 143 OK（兩種順序皆是） | 同上 |
| 修改後：`test_phase2_p4s3gate` → 契約 | 76 OK | 同上 |
| 修改後：shell 帶 `GHRSST_MAX_DAYS=10`、`GHRSST_SPATIAL_WINDOW_ENFORCE=0`、`GHRSST_BBOX_POINT_LIMIT=777` | 72 OK | 同上 |
| 阻擋 `jsonschema` import | 72 skipped | — |
| 反向對照：新測試對 round 8 文字副本（完整 `dev2026` 副本，只換回 round 8 的 `openapi_contract.py`） | 12 個失敗（6 failures＋6 errors）；含 BBox 上限 example 在 1000000／12345 設定下仍寫死 300000 | — |

Round 10 實作與結果（Claude 執行；當時 Codex 尚未重跑；2026-11-20）——只改 OpenAPI metadata、文件、snapshot 與測試：
- 內容見 [02 §11.6](02_openapi_v1_spec.md#116-round-10-變更2026-11-20只改-openapi-metadata)。`app.py` 自 round 6 起未改。
- OpenAPI 前後比對（snapshot）：差異只有根層 `servers`、兩個 `operationId`、兩個操作層 `security: []`、兩個說明各加一句免認證；移除這些後與 round 9 完全相同。
- 契約測試由 72 增為 73：+1（`servers` 值且 URL 不含 `/api`、operationId 唯一且為指定值、兩個 `security` 為空陣列、沒有根層 `security` 與 `securitySchemes`、免認證句各一次、`info.version` 仍 `0.4.1`）；
  改寫 3 個（路由測試改釘新 operationId；佔位 metadata 測試不再要求無 `servers`；「只改文件」測試改為確認 FastAPI 原始 operationId 不變、原始文件無 `servers`）。

| 執行（`dev2026/.venv`，`-W default`，`TMPDIR`=scratchpad） | 結果 | Warnings |
| --- | --- | --- |
| 未修改的 `6b32e54` 快照：7 個既有模組 | 71 OK | 1 種（StarletteDeprecationWarning），pre-existing |
| 修改後：7 個既有模組 | 71 OK | 同上 |
| 修改後：契約測試單獨 | 73 OK | 同上 |
| 修改後：7 模組＋契約，兩種順序 | 144 OK（兩種順序皆是） | 同上 |
| 修改後：`test_phase2_p4s3gate` → 契約 | 77 OK | 同上 |
| 修改後：shell 帶 `GHRSST_MAX_DAYS=10`、`GHRSST_SPATIAL_WINDOW_ENFORCE=0`、`GHRSST_BBOX_POINT_LIMIT=777` | 73 OK | 同上 |
| 阻擋 `jsonschema` import | 73 skipped | — |
| 反向對照：新測試對 round 9 的 `openapi_contract.py` | 3 個失敗（新測試、路由測試、snapshot） | — |

Round 10 修補（Claude 執行；當時 Codex 尚未重跑；2026-11-20）——只改 OpenAPI metadata 處理、文件、snapshot 與測試：
- `servers` 的 server object 只留 `url`（移除 `description: Production`）。OpenAPI 前後比對：**只有一處差異**，`/servers/0/description` 被移除。
- 認證防護：`apply_contract` 先檢查 FastAPI 原始文件；實際認證方案（根層或目標操作，單獨或與 `{}` 並列）、非空 `components.securitySchemes` 一律丟出 `OpenApiSecurityConflict`，在任何修改之前、不快取、不送出文件；只表示匿名存取的 `[]`／`[{}]` 正規化為 `security: []`（根層移除）。正常輸出不變。不改 API 認證行為。（最初版本把單獨的 `[{}]` 也拒絕，依 Codex review 修正，見下方）
- 契約測試由 73 增為 76：+3（實際原始文件無認證需求且正常輸出兩個 `security: []`、無 schemes；注入根層／根層 `[{}]`／GET／POST／`securitySchemes` 五種認證需求時 transformer 明確失敗且輸入未被修改，經 `install()` 的文件產生路徑也失敗；空的根層 `security: []` 可接受）；改寫 1 個（server object 只有 `url`）。

| 執行（`dev2026/.venv`，`-W default`，`TMPDIR`=scratchpad） | 結果 | Warnings |
| --- | --- | --- |
| 未修改的 `6b32e54` 快照：7 個既有模組 | 71 OK | 1 種（StarletteDeprecationWarning），pre-existing |
| 修改後：7 個既有模組 | 71 OK | 同上 |
| 修改後：契約測試單獨 | 76 OK | 同上 |
| 修改後：7 模組＋契約，兩種順序 | 147 OK（兩種順序皆是） | 同上 |
| 修改後：`test_phase2_p4s3gate` → 契約 | 80 OK | 同上 |
| 修改後：shell 帶 `GHRSST_MAX_DAYS=10`、`GHRSST_SPATIAL_WINDOW_ENFORCE=0`、`GHRSST_BBOX_POINT_LIMIT=777` | 76 OK | 同上 |
| 阻擋 `jsonschema` import | 76 skipped | — |
| 反向對照：新測試對修補前的 `openapi_contract.py`（只補上例外類別、不加檢查） | 8 個失敗：五種注入與 `install()` 路徑皆「OpenApiSecurityConflict not raised」（舊邏輯會靜默產生 `security: []`）、server object 測試、snapshot | — |

Codex review 後修正（`[{}]` 語義；Claude 執行；當時 Codex 尚未重跑；2026-11-20）：
- OAS 3.1.1 §4.8.30：空 Security Requirement Object `{}` 表示支援匿名存取；單獨的 `[{}]` 表示匿名即可，`[{}, {"bearerAuth": []}]` 才是「匿名或 bearer」。
- 修正：新增 `_anonymous_only`；根層或目標操作的 `[]`／`[{}]` 在沒有 security scheme 時接受並正規化為 `security: []`；任何實際方案（單獨或與 `{}` 並列）、非空 `securitySchemes` 仍拒絕。
- 測試數不變（76）：拒絕案例由五種改為六種（移除單獨根層 `[{}]`，新增根層 `[{}, {"bearerAuth": []}]` 與 GET `[{}, {"apiKey": []}]`）；原「空根層可接受」測試改為正規化測試，涵蓋根層 `[]`、根層 `[{}]`、兩個操作 `[{}]`。Snapshot 位元組相同，正常輸出不變。

| 執行（`dev2026/.venv`，`-W default`，`TMPDIR`=scratchpad） | 結果 |
| --- | --- |
| 未修改的 `6b32e54` 快照：7 個既有模組 | 71 OK |
| 修改後：7 個既有模組／契約測試單獨 | 71 OK／76 OK |
| 修改後：7 模組＋契約，兩種順序 | 147 OK（兩種順序皆是） |
| 修改後：`test_phase2_p4s3gate` → 契約；shell 帶其他設定值 | 80 OK；76 OK |
| 阻擋 `jsonschema` import | 76 skipped |
| 反向對照：新測試對修正前的 `openapi_contract.py` | 2 個失敗：根層 `[{}]` 與操作 `[{}]` 被舊邏輯拒絕；並列案例新舊版本都拒絕 |

Codex review P2 修正（malformed 值；Claude 執行；當時 Codex 尚未重跑；2026-11-20）：
- 問題：guard 用 `doc.get("security")` 與 `securitySchemes` 的真假值判斷，根層 `security: null` 會被當成「未出現」而接受、之後被 `pop` 移除；`securitySchemes: null`／`[]` 因 falsey 被略過。
- 修正：依欄位是否存在與型別判斷。`security`（根層、兩個目標操作）只接受未出現、`[]` 或只含空物件的陣列；`components.securitySchemes` 只接受未出現或空物件 `{}`（`{}` 接受後移除）；`components` 本身不是物件也拒絕。錯誤訊息改為「declares authentication or a malformed security value」。
- 測試數不變（76）：拒絕案例由 6 種增為 12 種（+根層 `null`、根層非陣列 `{}`、GET `null`、POST `["apiKey"]`、`securitySchemes: null`、`securitySchemes: []`）；正規化測試 +空 `securitySchemes: {}`。Snapshot 位元組相同，正常輸出不變。

| 執行（`dev2026/.venv`，`-W default`，`TMPDIR`=scratchpad） | 結果 |
| --- | --- |
| 未修改的 `6b32e54` 快照：7 個既有模組 | 71 OK |
| 修改後：7 個既有模組／契約測試單獨 | 71 OK／76 OK |
| 修改後：7 模組＋契約，兩種順序 | 147 OK（兩種順序皆是） |
| 修改後：`test_phase2_p4s3gate` → 契約；shell 帶其他設定值 | 80 OK；76 OK |
| 阻擋 `jsonschema` import | 76 skipped |
| 反向對照：新測試對修正前的 `openapi_contract.py` | 4 個失敗：根層 `null`、`securitySchemes: null`、`securitySchemes: []` 皆「not raised」（即 review 指出的缺口），以及空 `{}` 未移除；操作層 `null`、根層非陣列在修正前已會拒絕 |

Pre-existing（基線上就存在，非本輪造成）：`test_phase2_p4s3gate` 排在 `test_api` **之前**執行時，`test_api.BoundedExecutorTests.test_overload_sheds` 失敗
（p4s3gate 的 `importlib.reload(api.app)` 換掉了 `Overloaded` 類別）。已在未修改的 `6b32e54` 快照上重現（`app.py` sha256 `15ed3ca4…`）。
01 §15 的固定順序（`test_api` 在前）不受影響；本輪未修改既有測試。

Round 11（Claude 執行；當時 Codex 尚未 review；2026-11-20）——只改 OpenAPI metadata、snapshot、契約測試與本紀錄：
- 變更：GET `append` 參數 schema 與 `PointsRequest.append` 各加 `default: "sst"`；兩者仍為 optional（GET `required: false`；POST `required` 仍是 `["date", "points"]`）。`mode` 不變；handler、路由、執行行為不變（02 §11.7）。
- OAS 前後比對：修補前生成的文件與修補前 snapshot 位元組相同；修補後差異只有上述兩處新增 `default`。Snapshot 以 `GHRSST_UPDATE_OPENAPI_SNAPSHOT=1` 更新，更新後與生成文件位元組相同。
- 契約測試由 76 增為 77：+1 `test_append_default_is_sst_and_stays_optional`（兩處 default 為 `"sst"`、GET 非 required、POST required 不含 `append`、兩處都沒有 `enum`、`mode` 兩處都沒有 `default`／`enum`、`"sst"` 符合兩個 schema）；改寫 1 個：`test_contract_rewrites_documentation_only` 原本要求所有 GET 參數的 `default` 與 FastAPI 原始文件相同，現在明確只允許 `append` 多出 `"sst"`，其餘仍須相同，並確認原始文件的 `append` 沒有 default。
- Regression tests（先前 review 的測試缺口；不改程式）：拒絕案例由 12 種增為 14 種（+`components: null`、`components: []`），確認丟出 `OpenApiSecurityConflict` 且輸入文件前後相同。反向對照：在暫時移除非物件 `components` 檢查的程序內（未改檔案）執行，這兩個案例皆失敗（`TypeError`，不是 `OpenApiSecurityConflict`）。

| 執行（`dev2026/.venv`，Python 3.13.11，`-W default`，`TMPDIR`=scratchpad，清除 `GHRSST_*`） | 結果 |
| --- | --- |
| 修改前：契約測試 | 76 OK |
| 修改後：7 個既有模組（01 §15）／契約測試單獨 | 71 OK／77 OK（StarletteDeprecationWarning：httpx → httpx2，pre-existing） |
| 修改後：7 模組＋契約，兩種順序 | 148 OK（兩種順序皆是） |
| `git diff --check`（tracked）與 untracked 修改檔的行尾空白檢查 | 無輸出 |

Round 12（Claude 執行；當時 Codex 尚未 review；2026-11-20）——只改 OpenAPI 說明文字、snapshot、契約測試與 02／03／04／01／README：
- 變更：日期窗口改為 owner 接受的簡潔用詞（已與 P5 Codex reviewer 討論；不代表其他 F2 前置事項完成）。Point：「Point queries support the latest 366 consecutive calendar days ending on the latest date available to point queries. The archive currently starts on 2023-01-01.」；Spatial：「Bounding-box queries support the latest 31 consecutive calendar days ending on the latest date available to spatial queries. The same window applies to `POST /api/ghrsst/points`.」；POST Date 段同一窗口。移除 guaranteed、no date missing、「Earlier dates may be available but are not guaranteed」與 `available_spatial_window` 的「earlier … may have gaps」補充；Request limit 明寫限制的是單次 request 長度、不是 Coverage 窗口；request cap 的「not … a data-date guarantee」改為「not … a data coverage window」。細節見 02 §11.8。handler、路由、validation、response schema 結構、HTTP 行為、`info.version`（`0.4.1`）、`change_log.md` 皆未改。
- OAS 前後比對：key 集合相同；只有 7 個字串值改變（GET description、GET 400／413、POST description、POST 413、POST body example summary、`available_spatial_window` description）。Snapshot 以 `GHRSST_UPDATE_OPENAPI_SNAPSHOT=1` 更新。
- 契約測試由 77 增為 79：+2——`test_request_cap_is_distinct_from_coverage_window`（Coverage 與 Request limit 分屬不同 bullet、Request limit 帶「not the coverage window above」、`NOT_A_QUOTA` 結尾）、`test_no_absolute_or_internal_window_wording`（全文件不含 guarantee、no date missing、under normal service、earlier dates may be available、may have gaps、both ends included；任何 description／summary 不含 compaction、replacement、replace、audit、prune、monitoring、maintenance；兩個 operation 的 400 仍引用 `SpatialWindowError`）。改寫 6 個以符合新用詞：`test_service_windows_are_documented_per_endpoint`（完整句子、各 endpoint 位置、POST 只出現一次窗口）、`test_service_windows_do_not_follow_request_caps`、`test_gap_wording_never_contradicts_the_consecutive_windows`（任何談日期的句子都不得有 skipped／gap／missing，不再允許「earlier」限定）、`test_window_end_dates_match_the_query_kind`、`test_point_coverage_is_current_not_permanent`、`test_request_caps_live_with_their_endpoint_and_response`。
- 反向對照：新測試對修改前的 `openapi_contract.py`（scratchpad 副本）執行 → 8 個失敗、1 個錯誤（改名常數的 `AttributeError`）。
- 依 Codex review 再修正（只改文件與測試註解／範圍；生成的 OAS、snapshot 不變；測試數仍 79）：02 §1 窗口摘要與 §4 `available_spatial_window` 註解改為 round 12 用詞；程式與測試註解 round 11 → round 12；`test_no_absolute_or_internal_window_wording` 改為只檢查日期窗口相關描述（02 §11.8），並確認範圍內有 3 處「consecutive calendar days」。範圍檢查（程序內注入、未改檔案）：窗口描述內注入 guaranteed／may have gaps／compaction／audit 皆失敗；422、503、POST Points 段注入 guarantee／maintenance／replace 皆通過。
- 第二次修正（Codex review；只改測試範圍與本紀錄，生成的 OAS、snapshot 不變；測試數仍 79）：`test_gap_wording_never_contradicts_the_consecutive_windows` 原本掃描所有 description，禁止任何描述出現 skipped、任何提到日期的句子出現 gap／missing；改為使用同一個日期窗口範圍（02 §11.8），只在該範圍內禁止 skipped／gap(s)／missing，並確認範圍內有 3 處「consecutive calendar days」；移除不再使用的 `_descriptions()` helper。範圍檢查（程序內注入、未改檔案）：GET Point／BBox Coverage、POST Date 段、`available_spatial_window` 注入 skipped／gaps／missing／gap 皆失敗；422、`DataRow`、POST Points 段注入 missing／skipped 皆通過。

| 執行（`dev2026/.venv`，Python 3.13.11） | 結果 |
| --- | --- |
| 修改前：契約測試 | 77 OK |
| 修改後：7 個既有模組（01 §15）／契約測試單獨 | 71 OK／79 OK（StarletteDeprecationWarning：httpx → httpx2，pre-existing） |
| 修改後：7 模組＋契約，兩種順序 | 150 OK（兩種順序皆是） |
| 修改後：`test_phase2_p4s3gate` → 契約；shell 帶 `GHRSST_BBOX_POINT_LIMIT=12345`、`GHRSST_MAX_DAYS=10` | 83 OK；79 OK |
| 阻擋 `jsonschema` import | 79 skipped |
| `git diff --check`（tracked）與 11 個 untracked 檔的行尾空白檢查 | 無輸出 |

## Stage E — Generated OpenAPI／handler／tests 一致性 review

| 項目 | 內容 |
| --- | --- |
| 產出 | 一致性報告：02 規格 ↔ generated OpenAPI ↔ handler 實際回應 ↔ 測試；差異清單 |
| 依賴 | C、D |
| 驗收 | 0 個未說明的差異；每個差異要嘛修正，要嘛記錄為 OD／已知問題 |
| 角色 | Codex 主導；Claude 回應修正 |
| 結果（2026-11-20） | **Codex 已完成 Stage E**：150 tests passed（7 個既有 API 模組＋契約測試）；`git diff --check` 與 untracked 檔 whitespace 檢查乾淨。依結果：E4 記為不把 proxy 專屬 header 寫入 OAS、proxy 正確性由 Ops-1 處理；E5 範圍改為本機 route 與 generated OpenAPI wiring，部署後 production Swagger smoke 移到 Stage G；E6 用詞已統一（見下方「Stage E 後續修正」）。**最終確認（2026-11-20）**：Codex review E6 後續修正後確認，150 tests passed |

### Stage E 後續修正（2026-11-20，Claude）

- 變更：GET Point Query 的 Dates 由「without `start` and `end`, the newest point date is used」改為「without `start` and `end`, the latest date available to point queries is used」，與同段 Coverage 的終點用詞一致（E6）。同句其餘文字不變。
- OAS 前後比對：key 集合相同；只有 `/paths/~1api~1ghrsst/get/description` 一個字串值改變；`info.version` 仍 `0.4.1`。Snapshot 以 `GHRSST_UPDATE_OPENAPI_SNAPSHOT=1` 更新。
- 測試（數量仍 79）：`test_bbox_dates_are_described_exactly` 的 Point Query Dates 句改為新用詞，並新增兩個斷言：Point Query 段不含「newest point date」、「the latest date available to point queries」在該段恰好出現 2 次（Dates 與 Coverage）。
- 未改：handler、路由、request validation、response schema、HTTP 行為；`FRESHNESS` 句（owner 指定，仍用「newest product date」）；BBox 無日期的「newest date in the daily data」（指 daily 資料，不是 point 窗口終點）；`app.py`、`change_log.md`。

| 執行（`dev2026/.venv`，Python 3.13.11；Claude） | 結果 |
| --- | --- |
| 契約測試（更新 snapshot 後再跑一次） | 79 OK |
| 7 個既有 API 模組＋契約測試（`TMPDIR` 指向 scratchpad） | 150 OK |
| `git diff --check`（tracked）與 11 個 untracked 檔的行尾空白檢查 | 無輸出 |

最終確認（2026-11-20）：Codex review 上述修正後確認 E6，150 tests passed。其後只同步本節、檢查清單、README 與 02 §11.9 的 review 紀錄；OAS、snapshot、程式、測試未改。Claude 同步時在本機重跑 7 個既有 API 模組＋契約測試：150 OK；E1–E3 引用的 11 個測試個別執行皆 ok。

## Gate F2 — Owner 核准正式公開契約

| 項目 | 內容 |
| --- | --- |
| 產出 | Owner 書面核准，綁定 Stage E **已 review 的候選 diff**（F2 前不 commit）。核准後依序：(1) `info.version` 改為 `1.0.0`（`dev2026/api/app.py` 的 `version=` 一行；OD-20），更新 snapshot 與 `test_version_not_bumped_before_f2`；(2) `change_log.md` 新增獨立的「OpenAPI contract v1.0.0」紀錄（與服務部署版本分開，標明「已核准、尚未部署」）；(3) 02 狀態 Draft → Approved（記錄核准日期）；(4) OD-3：舊 Draft 加 SUPERSEDED 標示、`AGENTS.md` 改指 v1 規格；(5) 以上完成 review 後才 commit；(6) commit 後把 SHA 補入 02／03／04 與 changelog |
| 前置 | E 完成（Codex 2026-11-20 已完成；E6 後續修正 Codex 已確認）；Ops-1 驗收（公開 POST 對不同 body 回應正確）——宣告 POST 契約的前提，**公開 POST spot check 已通過**（2026-11-20，owner 修改 NGINX 後；未測 GET cache）；G-C/D Codex review 已通過；G-B 已由 Codex review 通過（00–05 文件；2026-11-24 記錄）；OD-21：P5 reviewer 確認 366／31 日為資料涵蓋政策與設計依據（**已收到**，2026-11-20；非無條件或永久保證）。v1 政策項（OD-3、6、11、16、17、20）已由 owner 決定；OD-12 屬 Stage G，不是 F2 前置 |
| 驗收 | owner 書面核准 |
| 角色 | Owner |
| 結果（2026-11-24） | **Owner 已核准 F2**（不含 commit、push、部署授權）。已完成產出 (1)–(4)：`info.version` `1.0.0` 與 snapshot／`test_version_is_the_approved_contract`；`change_log.md`「OpenAPI contract v1.0.0」（已核准、尚未部署）；02 狀態 → Approved（2026-11-24）；OD-3 SUPERSEDED 標示與 `AGENTS.md`。當時待辦：(5) Codex review 後 commit；(6) commit 後補 SHA——其後皆已完成：實作 commit `b9bd4e46097289632874d1615c5737c37ebc0156`，文件已記錄此 SHA（2026-11-24）。changelog 的部署 commit 依 (2) 的約定留到部署後補。詳見 02 §11.10 |

## F2 readiness（2026-11-20 owner 決定後；狀態截至 2026-11-24）

**F2 已由 owner 核准（2026-11-24）**；下表為核准前的 readiness 整理，保留作紀錄。

| 類別 | 項目 |
| --- | --- |
| 已由 owner 決定 | 路徑固定 `/api/ghrsst`、`/api/ghrsst/points`，不加 `/v1`；OD-16：`1.0.0` 為第一個正式契約版本，不設棄用期限／過渡期，之後採 SemVer（不承諾棄用時程或公告管道）；OD-3（F2 時執行）；OD-6、OD-17 維持現行行為並文件化；OD-11 暫不提供 contact／license／terms，rate limit／SLA 不承諾；OD-12 延至 Stage G；OD-20（F2 時升版）；OD-21 公開用詞（round 12）；changelog 另立「OpenAPI contract v1.0.0」紀錄；F2 → 升版與 changelog → review → commit → 補 SHA；X2 部署目標 |
| 已有 P5 reviewer 意見，但不是永久保證 | OD-21：366／31 日為資料涵蓋政策與 P5 流程的設計依據（owner 於 2026-11-20 對話中提供；無 P5 commit SHA）。不代表任何情況、永遠無缺日；公開 OAS 不寫內部維運流程或「under normal service」 |
| Ops-1（公開 POST spot check 通過） | Ops-1／OD-13：owner 修改並 reload NGINX 設定後，2026-11-20 15:09 UTC 的三筆公開 POST 序列通過（上方「Ops-1 驗收結果」）。未測 GET cache 行為，未宣稱驗證所有快取情境 |
| G-C/D | **Codex review 已通過**（2026-11-20；含 BC-1／BC-2） |
| G-B | **已由 Codex review 通過**（00–05 文件；2026-11-24 記錄） |
| Codex Stage E | **已完成**（2026-11-20）：150 tests passed；`git diff --check` 與 untracked whitespace 乾淨。E6 後續修正 Codex 已確認（150 tests passed）；各項處理見下表 |
| Owner 核准 | **已核准**（2026-11-24），綁定已 review 的候選 diff；`info.version` 已改為 `1.0.0` |

Stage E 檢查清單（Codex；2026-11-20 已完成）：

| # | 檢查 | 處理 |
| --- | --- | --- |
| E1 | 02 §2–§6（含 §4 YAML）與 generated OpenAPI／snapshot 逐項對照，特別是 round 7–12 改過的說明與 schema 描述 | 一致：generated OpenAPI 與 snapshot 相同（`test_snapshot`）；所有 `$ref` 可解析、schema 有效（`test_every_ref_resolves_and_schemas_are_valid`）；所有 example 符合其 schema（`test_every_example_matches_its_schema`）。Codex 對照後只要求 E6 一處用詞修正 |
| E2 | OAS ↔ handler：契約描述 BC-1／BC-2 之後的行為，只存在本機 `app.py`；確認契約與部署目標（X2）一致，並確認 production 現況差異已明確記錄 | 一致：參數與 request model 對應 handler（`test_documented_parameters_match_the_handler`、`test_documented_request_matches_the_model`）；契約只改寫文件、不改 route（`test_contract_rewrites_documentation_only`）；BC-1（各路徑不存在日期 400）與 BC-2（非有限座標 422）有 HTTP 層測試。X2：production `6b32e54` 尚未包含 BC-1／BC-2，已記錄於 02 §7 與下方 Stage G |
| E3 | 503：兩種「無可用日期」503 不帶 `Retry-After` 的寫法（02 §5.3.1 原記為由程式碼推論）是否有 HTTP 層測試覆蓋 | **已有 HTTP 層測試**（空資料庫與 0 permit executor 的 TestClient，非 production）：GET point「No available dates.」與 GET bbox「No available dates for spatial queries.」的 503 **不帶** `Retry-After`（`test_no_available_dates_503s_have_no_retry_after`）；overload 503（GET point、GET bbox、POST）**帶** `Retry-After: 1`、body 為「server overloaded; retry later」（`test_overload_503_has_retry_after`）；POST 在無資料日期時回 **400**（不是 503），body 符合 OAS（`test_post_with_no_data_is_400_not_503`）。OAS 中 `Retry-After` 為 optional、只標於 overload（`test_retry_after_is_optional_and_overload_only`） |
| E4 | Proxy 層行為（錯誤回應約 1 分鐘快取、`X-api-cache`、`Vary`）只記在 02 §7、不寫進 OAS | **不把 proxy 專屬 header 寫入 OAS**；公開 proxy 的正確性由 Ops-1 處理（不是 OAS 範圍） |
| E5 | 本機 route 與 generated OpenAPI wiring：`app.py` 的 install hook 讓 FastAPI 的 `openapi()` 輸出本契約，route 與 OAS paths／operations 一致 | 隨 Stage E 完成。**部署後 smoke 不在 E**：production 使用 VM24 專用的 Swagger 設定（change_log 0.5.0），部署目標的 `/api/swagger/ghrsst/openapi.json` 是否輸出此契約、`servers` 加上 `/api/...` 後的 URL 是否正確，移到 Stage G |
| E6 | 用詞小差異：Point Query 的 Dates 寫「newest point date」、Coverage 寫「latest date available to point queries」 | **已統一**為「the latest date available to point queries」（上方「Stage E 後續修正」）；**Codex 已確認** |
| E7 | Codex 重跑 7 個既有模組＋契約測試；pre-existing 的測試順序問題（`p4s3gate` 在 `test_api` 前）不影響結論 | **150 tests passed**；`git diff --check` 與 untracked whitespace 乾淨 |
| E8 | 文件狀態欄（README review gates、05 標頭、各 round 的「Codex 尚未 review」）與實際 review 狀態是否一致 | 03 進度表、本節與 README G-E 已改為 Stage E 完成；G-C/D 當時仍記為待 Codex review（未推論已通過），其後 Codex review 已通過（2026-11-20）；各 round 的歷史紀錄保留原文 |

## Stage G — 發布準備與部署規劃（獨立授權）

| 項目 | 內容 |
| --- | --- |
| 產出 | 發布準備：選定基線與 v1 變更在遠端可取得（push／tag 由 owner 授權；基線已由 owner 發布為 `dev2026-api-v1-base-6b32e54`，HEAD 為原始 `6b32e54`；API branch 的 push／PR 待 owner 安排）；部署計畫：目標 commit、與 P5 serving checkout 的關係、與 `ee84037` 等 P5 變更的合併順序、hash 驗證、rollback、上線後 smoke（公開 `openapi.json` 的 `info.version == 1.0.0`、路由與回應抽查、headers） |
| 依賴 | F2；部署執行者確定；P5 session 協調（OD-15） |
| 部署目標（X2） | `6b32e54` 加上 API v1 候選變更（含已核准的 BC-1／BC-2）。目前 production（`6b32e54`）**尚未包含**這些本機改動，部署前 v1 契約描述的 BC-1／BC-2 行為不會在 production 生效。若部署前整合 `ee84037` 或其他會改變 503／headers 的 P5 變更，必須先更新並 review OAS（02 §7）。部署 commit 的識別方式見下方 G-1 |
| Production Swagger smoke（由 E5 移入） | 部署後確認：production 使用 VM24 專用的 Swagger 設定（change_log 0.5.0），`/api/swagger/ghrsst/openapi.json` 輸出此契約（`info.version == 1.0.0`）；`servers` URL 加上 `/api/...` 路徑後正確；Swagger UI 可載入。需部署授權後由部署者執行；Claude 不執行 |
| 其他 G 項目 | CORS（OD-12）：v1 不承諾，是否開放在此階段規劃；部署後在 `change_log.md` 的「OpenAPI contract v1.0.0」紀錄補部署日期、部署 commit 與 smoke 結果 |
| 驗收 | owner 對發布與部署的獨立授權；部署後 smoke 結果記錄 |
| 角色 | Owner 授權；部署者執行；Claude 只撰寫計畫，不執行 |

### Stage G 部署準備清單（2026-11-25 整理；**部署未授權**；待 Codex review）

本清單只供 review 與規劃，本身**不授權**任何 production 變更（同 p4s10 runbook §1 的原則）。Claude 不執行清單中任何 VM24／production 步驟。

**依據（皆為本 worktree 內的 repo 文件與腳本，唯讀整理）：**

| 來源 | 用到的內容 |
| --- | --- |
| 00 §3、§4 第 1 點 | 2026-11-19 VM24 唯讀證據：PM2 launcher `/home/odbadmin/python/ghrsst/conf/start_app.sh` 以 `cd /home/odbadmin/p5deploy/20260830T082136Z/checkout` 啟動 `gunicorn api.app:app`（`127.0.0.1:8035`，`PYTHONPATH=dev2026`，venv 為 `/home/odbadmin/python/ghrsst-dev2026-phase2/dev2026/.venv`）；deploy checkout HEAD `6b32e54`、乾淨；18 個 api／store 模組 hash 與本機一致；VM24 上有 `p5deploy_6b32e54.bundle` |
| `conf/ecosystem.config.js` | PM2 app 名稱 `ghrsst`，`script: ./conf/start_app.sh` |
| `conf/start_app.sh`（repo 版） | 仍是舊的 `ghrsst_app:app`（py314）；**與 production launcher 內容不同**（00 §3），不能拿 repo 版當部署依據 |
| `dev2026/README.md`「VM24 production deployment (v0.5.0)」 | launcher env 清單；routine restart（`pm2 restart ghrsst`，不換路徑）；回到 pre-dev2026 app 的 rollback；operational checks；Swagger URL |
| `AGENTS.md`（@`b9bd4e4`） | production path swap：取 GHRSST ingest lock → stop `ghrsst` → 確認 `8035` 已停 → swap → start → 驗 `/healthz`；禁止 ad-hoc `pm2 … --update-env`；不改 NGINX 或無關 PM2 程序。同檔「active API runtime is `ghrsst-dev2026-phase2`」一句已過時（00 §3） |
| `dev2026/specs/p4s10_production_rollout_runbook.md` | §1：`$DEPLOY_SHA` 必須是完整 40 字元 commit SHA、取自**外部核准紀錄**（文件不能記錄包含自身的 SHA），並在 HEAD 上核對；§3：Asia/Taipei cron 時窗、PM2 lifecycle preflight、stop→swap→start、禁用 `--update-env`；§7：以 HTTP status 為 hard gate，`X-Store-Route` 只記錄；§8：artifacts 與 operator summary；§9：rollback ladder。**該 runbook 是 P4 delta prune 的流程**，此處只引用其通用部署紀律，不表示 v1 部署要執行 prune／swap |
| `dev2026/specs/p5_integration_candidate.md` §6、`dev2026/ops/p5_readiness.py` | P5 以 `REVIEWED_DEPLOY_SHAS`（目前 `3223fba`、`708058e`）／`CURRENT_DEPLOY_SHA`（`708058e`）／`PROPOSED_DEPLOY_SHAS`（空）管理 service deployment approval，涵蓋含 `api/app.py` 在內的 service modules |
| `dev2026/api/app.py` | `docs_url="/api/swagger/ghrsst"`、`openapi_url="/api/swagger/ghrsst/openapi.json"` |
| `dev2026/tests/test_api_v1_contract.py`、05 | BC-1／BC-2 的預期行為（smoke 預期值來源）：`test_bc1_non_existent_day_is_400_on_every_path`、`test_bc1_non_existent_day_is_a_date_error_not_a_window_error`、`test_bc2_get_non_finite_is_422`、`test_bc2_post_non_finite_is_422_with_stringified_input` |
| `dev2026/requirements-test.txt`、`dev2026/api/openapi_contract.py` | `jsonschema` 只供測試、不裝在 serving host；`openapi_contract.py` 只 import 標準函式庫（`copy`、`typing`），v1 不新增 runtime 依賴 |

#### G-1 部署目標識別

1. `$DEPLOY_SHA` 只能是 owner 在**外部核准紀錄**（部署授權）中寫明的完整 40 字元 commit SHA；branch、tag、縮寫一律不接受（p4s10 §1）。本文件不預先寫入部署 SHA。
   同一份部署授權也要寫明 `$REVIEWED_API_SHA`：**最新一個已經 Codex review 的 API commit 的完整 SHA**（其 tree 就是核准部署的內容）。本文件寫到的 `b9bd4e4`（實作）與 `956fbe4`（文件同步）只是截至 2026-11-25 已 review 的 commits；之後若有新的已 review commit（例如本輪文件修改提交後），以授權中寫明的 SHA 為準，不以本文件為準。
2. 候選來源是 API PR merge 後的 commit。merge 方式（merge commit／squash／rebase）由 owner 決定：squash 或 rebase 會產生新的 commit，`b9bd4e4` 不再是部署 commit 的祖先，因此**以 tree 內容比對為主、ancestry 為輔**。
3. 部署前在完整 clone 上核對（唯讀 git 指令）：
   - `git rev-parse --verify "$DEPLOY_SHA^{commit}"` 的輸出必須等於輸入本身（完整 SHA）；
   - `git merge-base --is-ancestor 6b32e54710db5b65fae3e30081d48eae71b26f26 "$DEPLOY_SHA"` 為真（包含 production 基線）；
   - `$REVIEWED_API_SHA` 同樣以 `git rev-parse --verify "$REVIEWED_API_SHA^{commit}"` 核對為完整 SHA，且 `git merge-base --is-ancestor b9bd4e46097289632874d1615c5737c37ebc0156 "$REVIEWED_API_SHA"` 為真（它承接已 review 的 API 實作）；
   - `git diff --quiet "$REVIEWED_API_SHA" "$DEPLOY_SHA"` 為真：部署 tree 與授權指定的已 review tree 完全相同。**若不同，或授權中的 SHA 與實際候選對不上（例如 merge 後又有新 commit、squash 產生的 tree 不同），立即停止**：以 `git diff --stat` 逐檔列出差異，交 Codex 重新 review，再由 owner 更新授權中的 SHA；**不得**夾帶未 review 的變更部署；
   - `git log --oneline 6b32e54710db5b65fae3e30081d48eae71b26f26.."$DEPLOY_SHA"` 只出現 API v1 的 commits（及 merge 方式產生的 merge commit）；出現任何 P5／O4 或其他 commit → NO-GO；
   - O4 排除：部署前由 owner／P5 提供**目前要排除的 O4 候選 commit 的完整 SHA**（記在部署授權或 preflight 紀錄中），preflight 對每一個 SHA 核對 `git merge-base --is-ancestor <O4 SHA> "$DEPLOY_SHA"` 為假。00 §4 與 Stage A 用的 `93625b3`（2026-11-19／11-20 當時 O4 lineage 的第一個 commit）只是當時的查核點，**不能據以宣稱目前的 O4 歷史已排除**；Claude 不讀取 O4 worktree。O4 SHA 尚未提供前，此項**待確認**（P-h），preflight 不得視為通過；
   - `ee84037` 不在歷史中（OD-2；若 owner 另行決定先整合，必須先更新並 review OAS，02 §7）。
4. 部署後身分確認（沿用 00 §3 的取證方式）：新 deploy checkout 的 `git rev-parse HEAD` 等於 `$DEPLOY_SHA`、`git status` 乾淨；執行中 gunicorn master 的 cwd 是該 checkout；`dev2026/api/app.py`、`dev2026/api/openapi_contract.py` 與 `dev2026/store/*.py` 的 sha256 等於 `$DEPLOY_SHA` 中的 blob（00 §3 當時只比對 `app.py` 與 `store/*.py`，v1 新增的 `openapi_contract.py` 也要比）。

#### G-2 部署前檢查

有 repo 依據的項目：

1. Owner 的書面部署授權，寫明部署者、時間窗、`$DEPLOY_SHA` 與 `$REVIEWED_API_SHA`，以及 owner／P5 提供的 O4 排除 SHA（G-1；p4s10 §1）。
2. 時間窗避開 Asia/Taipei cron（daily ingest 05:05／18:05、delta append 07:30／20:30），前後留 30–60 分鐘（p4s10 §3）。
3. 部署前狀態留存：`pm2 describe ghrsst`、production launcher 內容的備份、目前 deploy checkout 的 HEAD（應為 `6b32e54`）與 `git status`、`/healthz`、目前公開 `openapi.json`（應為 `info.version` `0.4.1`）（p4s10 §3／§8；00 §3）。
4. 環境不變：沿用 launcher 現有 env（`dev2026/README.md` 列出的 `GHRSST_*`）；v1 不需要新增 env；不使用 `pm2 … --update-env`（`AGENTS.md`、p4s10 §3）。
5. 執行環境：沿用 production venv（00 §3：Python 3.13.7、fastapi 0.138.0、pydantic 2.13.4、starlette 1.3.1）；本機契約測試也使用 fastapi 0.138.0。v1 不新增 runtime 依賴（見上表）。
6. 新 checkout 的 import 檢查：以 production venv、`PYTHONPATH=dev2026` import `api.app`，確認 `app.openapi()["info"]["version"] == "1.0.0"`（p4s10 §1 import preflight 的原則）。`api.app` 在 import 時是否開啟 store、應在什麼環境下執行，**待確認**。
7. 不改 NGINX（`dev2026/README.md`、`AGENTS.md`）。

**待確認（repo 內沒有依據，不自行假設）：**

| # | 事項 | 現有線索 | 由誰決定 |
| --- | --- | --- | --- |
| P-a | 新 deploy checkout 如何建立（路徑、來源） | 00 §3／§4 只證明 production 來自 `/home/odbadmin/p5deploy/20260830T082136Z/checkout` 與 VM24 上的 `p5deploy_6b32e54.bundle`；repo 沒有記錄該 checkout 的建立與切換步驟。`p5_deployment_readiness.md` 記錄過以 git bundle 建立的唯讀 readiness checkout，但那不是 serving 部署 | Owner／部署者 |
| P-b | production launcher 如何改指新 checkout、備份檔名與位置 | production launcher 不在 repo（repo 的 `conf/start_app.sh` 是舊版） | Owner／部署者 |
| P-c | 重啟方式：plain `pm2 restart ghrsst`，或 path swap 的 lock → stop → 確認 `8035` 已停 → 切換 → start | `dev2026/README.md` 的 restart 是「不換路徑」的情境；`AGENTS.md` 對 path swap 要求取 ingest lock 並 stop／start。換 deploy checkout 是否屬 path swap、要不要取 lock 與做 PM2 lifecycle preflight（p4s10 §3），repo 沒有針對 API-only 部署的規定 | Owner（必要時與 P5 協調） |
| P-d | 是否需要 P5 的 service deployment approval | P5 以 `REVIEWED_DEPLOY_SHAS` 管理 service 部署（含 `api/app.py`）；目前 production 的 `6b32e54` 本身也不在該 tuple 中。API v1 的部署 SHA 是否要走 P5 的 propose → promote 流程、P5 readiness 的 `identity` 檢查會如何看待新 SHA，repo 沒有結論 | Owner 與 P5（OD-15）；**待確認**，未決定前不得視為可部署 |
| P-e | 與 `ee84037` 等 P5 變更的整合順序 | OD-2、X2；若先整合，OAS 必須先更新並 review（02 §7） | Owner 與 P5 |
| P-f | CORS | OD-12：v1 不承諾；是否開放在 Stage G 規劃 | Owner |
| P-g | 公開 `openapi.json` 是否受 NGINX GET 快取影響 | Ops-1 只驗證公開 POST，**未測 GET cache**（01 §10.4、02 §7）；部署後若公開端點仍回 `0.4.1`，可能是 proxy 快取。清快取屬 ops 操作，需另行授權 | Owner／ops |
| P-h | 目前要排除的 O4 候選完整 SHA | repo 中的 `93625b3` 只是 2026-11-19／11-20 的查核點；目前 O4 lineage 需由 owner／P5 提供（Claude 不讀 O4 worktree） | Owner／P5 |
| P-i | spatial smoke 日期 `D_spatial` 的確認方式 | 見 G-3 的選日規則：`/healthz` 只給 delta 的 `[earliest, latest]` 與 daily 的 bounds，無法證明某一天同時在 delta 中且 daily store 有資料 | Owner／部署者 |

#### G-3 部署後 smoke 計畫（只寫計畫；由部署者在授權範圍內執行，Claude 不執行、不發 production 請求）

先在 VM24 本機查 `http://127.0.0.1:8035`（繞過 NGINX，判斷 app 本身行為），再查公開 `https://eco.odb.ntu.edu.tw`。Hard gate 以 HTTP status 與下列指定欄位為準；headers（如 `X-Store-Route`、`X-API-Cache`）只記錄（p4s10 §7）。GET 日期一律明寫 `start=<日>&end=<日>`（p4s10 §7：GET 不認得 `date=`）；BBox 不可省略日期（理由見下）。

**smoke 日期分兩種（依 `dev2026/api/app.py` 的 `healthz`、`read_ghrsst`、`post_points` 與 `store/spatial_policy.py`）：**

- **`D_point`（S6、S12）**：取 `/healthz` 的 `point_latest`（base＋delta＋daily 聯集的最新日）。Point 查詢以這個聯集為準，所以 `point_latest` 本身就是 point 可查的日期。
- **`D_spatial`（S7、S8、S13）**：必須是**目前確實可供 spatial query 使用**的日期。程式的條件是：
  1. 當 `spatial_window_enforce` 為 true 且 delta 已載入時，日期必須**屬於 delta 的日期集合**（`spatial_day_allowed` 判斷的是成員資格，不是落在區間內）；
  2. 之後還要在 daily store 的 bounds 內，且 `store.day_present(日)` 為真（daily store 有這一天）。
  BBox 不帶日期時，handler 先取 **daily store 的最新日**，再交給 spatial window gate 判斷，**沒有自動回退**到較早的日期；所以 `daily_latest` 不能一律當成 spatial 可用日期——若它不在 delta 中，請求會回 400。
  **候選規則**：`D_spatial` = `/healthz` 的 `delta_latest`。前提是 `/healthz` 顯示 `spatial_window_enforce == true`（與 production launcher 的 `GHRSST_SPATIAL_WINDOW_ENFORCE=1` 一致；不一致即停止並記錄）、`spatial_window` 非 null 且 `spatial_window[1] == delta_latest`，並且 `daily_earliest <= delta_latest <= daily_latest`。
  **仍無法由 repo 證明的部分**：`/healthz` 只回報 delta 的 `[earliest, latest]`（`spatial_window`）與 daily store 的 bounds，不提供 delta 的日期清單，也不提供 daily store 的逐日存在狀態；因此上述前提成立時，仍不能保證 `delta_latest` 這一天 daily store 確實有資料。`D_spatial` 的最終確認方式（例如由部署者在部署前以唯讀方式查核，或在部署前對現行 production 以同一日期執行 S7／S8 作為基準）**部署前待確認**（P-i），由 owner 決定；在確認前，S7／S8 回 400 不能直接判定為 v1 的缺陷，要先排除選日問題。

| # | 檢查 | 預期 |
| --- | --- | --- |
| S1 | `GET /api/swagger/ghrsst/openapi.json` | 200；`openapi == "3.1.0"`；`info.version == "1.0.0"`；`servers == [{"url": "https://eco.odb.ntu.edu.tw"}]` |
| S2 | 同上文件的 paths／operations | 只有 `/api/ghrsst`（get，`operationId` `getGhrsstData`）與 `/api/ghrsst/points`（post，`operationId` `postGhrsstPoints`）；兩個 operation 都是 `security: []`，無 `securitySchemes` |
| S3 | 同上文件與 `dev2026/tests/snapshots/openapi_v1.json` 比對 | 差異只能來自部署值（例如由 `cfg` 讀出的 request cap：VM24 `GHRSST_BBOX_POINT_LIMIT=300000`，程式預設 1000000）；每一處差異逐項記錄 |
| S4 | `GET /api/swagger/ghrsst` | Swagger UI 可載入（不需要按 Try it out） |
| S5 | `GET /healthz` | 200 |
| S6 | GET point：`/api/ghrsst?lon0=121&lat0=24&start=D_point&end=D_point&append=sst` | 200 |
| S7 | GET bbox：`/api/ghrsst?lon0=135&lat0=15&lon1=136&lat1=16&start=D_spatial&end=D_spatial&append=sst` | 200 |
| S8 | `POST /api/ghrsst/points`，body `{"date":"D_spatial","points":[[135,15]],"append":"sst"}` | 200 |
| S9 | BC-1，GET point：`start=2025-02-30&end=2025-02-30&lon0=121&lat0=24` | 400，body `{"detail":"Invalid date format. Use YYYY-MM-DD."}`（部署前是 500） |
| S10 | BC-1，GET bbox：S7 的座標加 `start=2025-02-30&end=2025-02-30` | 400，同上 body（不是 `SpatialWindowError`） |
| S11 | BC-1，POST：`{"date":"2025-02-30","points":[[135,15]],"append":"sst"}` | 400，同上 body |
| S12 | BC-2，GET：`lon0=nan&lat0=24&start=D_point&end=D_point` | 422；`detail[0].type == "finite_number"`、`loc == ["query","lon0"]` |
| S13 | BC-2，POST：raw body `{"date":"D_spatial","points":[[NaN,15]],"append":"sst"}`（JSON 字面值 `NaN`；422 在日期檢查前發生） | 422；`loc == ["body","points",0,0]`、`input == "NaN"`（部署前是 200） |

- 公開端點的錯誤回應可能被 proxy 快取約 1 分鐘（02 §7），因此 BC-1／BC-2 的行為以 `127.0.0.1:8035` 的結果為準；公開端點只確認可達，結果與本機不一致時記錄、不清快取（見 P-g）。
- 結果記入 `change_log.md`「OpenAPI contract v1.0.0」紀錄：部署日期、部署 commit、smoke 結果（Gate F2 產出 (2) 的約定）。任何 hard gate 失敗 → G-4。

#### G-4 回復準備

- **回復目標**：部署前的 serving 狀態，即 deploy checkout `/home/odbadmin/p5deploy/20260830T082136Z/checkout` @ `6b32e54710db5b65fae3e30081d48eae71b26f26`（00 §3），加上部署前備份的 production launcher 與 PM2 定義（G-2 第 3 項）。部署期間與部署後，舊 checkout 不得刪除或改動，保留期限由 owner 決定。
- **repo 中可查證的既有回復流程只有一個**：`dev2026/README.md`「Rollback to the pre-dev2026 app」——以 `start_app.sh.pre-dev2026` 還原 launcher，再 `pm2 stop ghrsst`／`pm2 start ghrsst` 並查 `/healthz`。它回到的是 **pre-dev2026 舊 app，不是 `6b32e54`**，不能直接當作 v1 的回復步驟；可沿用的只有它的形式（還原 launcher 備份 → PM2 stop／start，不用 `--update-env` → `/healthz`）。
- v1 只改程式與文件、不改資料或 store，回復只涉及 checkout 與 launcher；p4s10 §9 的資料 hold／reverse-swap 不適用。
- **回復後驗證要分兩處看**：
  - **VM24 本機服務**（`http://127.0.0.1:8035`，繞過 NGINX）：判斷應用是否已回復，以這裡的結果為準。預期 `/api/swagger/ghrsst/openapi.json` 回到 `info.version` `0.4.1`，BC-1／BC-2 也回到基線行為（不存在日期在 point 模式回 500、非有限座標回 200）。
  - **公開 URL**（`https://eco.odb.ntu.edu.tw`）：公開 `openapi.json` 是否受 NGINX GET proxy cache 影響仍**未驗證**（P-g），因此回復後公開 URL 可能暫時仍傳回回復前已被快取的契約內容（v1，`info.version` `1.0.0`）。未經確認，不得把這種情況直接判定為應用回復失敗，也不得據此判定回復成功；只記錄觀察到的內容與時間，並與本機結果並列。
  - **不清快取**。若需要清除快取，或要對公開 URL 做額外驗證（例如等待快取過期後重查），都屬於需 Owner／ops 另行決定並授權的事項，不包含在回復步驟內。
- **待確認**：launcher 備份檔名與位置（P-b）；回復的觸發條件與決策者（建議：G-3 任一 hard gate 失敗即回復，由 owner 決定）；回復時是否同樣取 ingest lock（P-c）。

#### G-5 責任與授權界線

- 部署**尚未授權**。部署者、時間窗與 `$DEPLOY_SHA` 由 owner 安排。
- Claude：只撰寫並更新本清單；不連 VM24、不送 production 請求、不操作 PM2／NGINX／快取、不部署。API branch 的 push／PR 依 owner 指示另行處理；merge 由 owner。
- Codex：review 本清單；部署時的角色由 owner 指派。
- P5：P-d、P-e 需與 P5 協調（OD-15）。
- 基線 branch `dev2026-api-v1-base-6b32e54` 的發布不是新的 F2 核准，也不是部署授權。

## 驗證紀律
- 只報告實際執行的結果；未執行的測試標示「未執行」。
- 每次報告附：基線 commit、測試命令、通過／失敗數、`git diff --check` 結果。
- 基線上已存在的失敗（若有）列為「pre-existing」，不得混入新變更的結果。
- Round 4 的測試結果見上方 Stage D；皆由 Claude 在本機執行，當時 Codex 尚未重跑。沒有對 production 或 VM24 執行任何測試。Stage E（2026-11-20）時 Codex 已重跑 7 個既有 API 模組＋契約測試（150 passed）；其後 G-C/D 已通過 Codex review（2026-11-20）。
