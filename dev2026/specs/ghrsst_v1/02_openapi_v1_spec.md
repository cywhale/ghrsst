# 02 — OpenAPI v1.0.0 契約規格

> **狀態：APPROVED — OpenAPI 契約 v1.0.0。Gate F2 已由 owner 核准（2026-11-24）；`info.version` 已改為 `1.0.0`（OD-20）。Codex 最終 review 已完成；實作 commit SHA：`b9bd4e46097289632874d1615c5737c37ebc0156`（2026-11-24，parent `6b32e54`；其後的文件同步 commit 另有自己的 SHA）。尚未部署（production `6b32e54` 尚未提供此契約；部署屬 Stage G）。Gate F1 已通過（2026-11-20，實作範圍同意）。** 以下為核准前各 round 的紀錄（歷史；當時狀態為 DRAFT、F2 未核准）。Round 5：`format` 描述修正、BC-1；Round 6：BC-2（B2-b）、日期 `maxLength: 10`；Round 7：owner 選定的資料日期窗口（§5.0，**P5 資料流程保障尚待跨工作區確認**）與 Swagger 說明文字重整（§11.3）；Round 8：窗口內連續性、缺日說明與兩種「最新日期」的用詞修正（§11.4）；Round 9：GET 說明依 owner 指定層級重整（Point Query／Bounding-Box Query）、point 目前覆蓋起點 2023-01-01、freshness 提示、BBox 日期參數精確描述（§11.5）；Round 10：`servers`（production，server object 只有 `url`）、穩定 `operationId`、操作層 `security: []`（公開、免認證），以及「generated 文件出現認證需求時拒絕產生」的防護（§11.6）；Round 11：`append` 在 GET 參數與 `PointsRequest` 加 `default: "sst"`（仍為 optional；`mode` 不變），並補 `components: null`／`[]` 拒絕案例的 regression tests（§11.7）；Round 12：日期窗口文字改為 owner 接受的簡潔寫法（已與 P5 Codex reviewer 討論；§5.0、§11.8）——不代表 OD-21 的 P5 資料流程保障或其他 F2 前置事項已完成。F2 readiness 更新（2026-11-20）：§5.0 記錄 P5 reviewer 的政策／設計依據確認（非永久保證），§7 部署目標、§8 版本政策、§9 OD-3 依 owner 決定更新；OAS 文字未改，**F2 仍未核准**。 Stage E（2026-11-20）：Codex 已完成；依結果把 GET Point Query 的 Dates 終點改為「the latest date available to point queries」（E6，§11.9），Codex 已確認。
> - **Gate F1（實作範圍同意）**：owner 已同意本草案作為 Stage C／D 的實作範圍；F1 不是對外承諾。
> - **Gate F2（正式公開契約核准）**：Stage C–E 完成後，owner 核准最終規格，才可對外宣告為 v1 契約。
>
> 詳見 [03](03_roadmap_and_validation.md)。基礎：[01](01_current_contract_inventory.md) 在 `6b32e54` 盤點的實際行為；基線 = `6b32e54`（OD-1 已決定）。
> Stage C 實作對照見 [§11](#11-stage-c-實作對照round-4)。
> 原則：**v1 描述現有行為**。任何會改變回應的項目一律標成 `[OD-n]`，等待 [04](04_owner_decisions.md) 決定；
> 核准前的起草原則（歷史）：未決項目以當時現況為預設值寫入；未決政策不列入正式契約。

## 1. 範圍與原則

1. 路由不變：`GET /api/ghrsst`、`POST /api/ghrsst/points`；不做 URL 版本化（無 `/v1/`）。
2. v1 = **完整描述**（schema、錯誤、headers、範例、行為規則）+ `info.version: "1.0.0"`。只改版本字串不構成 v1。
3. 實作方式（Stage C）只能新增 OpenAPI metadata（schemas、responses、headers、examples、descriptions），**不得改變執行期行為**；
   特別是不可改用 `response_model`（會改變序列化路徑與效能，且 point／bbox 列的「省略 vs null」語意無法以 schema 單獨表達），
   也不可在 `Query(...)`／`Field(...)` 加入會被 pydantic 執行的限制（會把現行 400／413 變成 422）。
   Round 4 的實作把上述 metadata 全部放在獨立模組，只改寫 generated OpenAPI 文件（§11）。改變行為的修補只能在 owner 核准的 OD 項目下各自進行。
4. 契約層級：
   - **Contract**：本文件 §3–§6 標示為 contract 的項目；v1 之後修改需遵循 §8 版本政策。
   - **Informational**：明確標示的觀測用 headers／欄位，可在不升 major 的情況下變更（OD-9）。
   - **上限值（request caps）**：v1 文件只記錄**目前部署值**與**超過時的拒絕規則**（狀態碼、body 中回報的上限）；
     它們是單次請求的處理上限，**不是** rate limit、配額，也不是資料日期窗口承諾（OD-18）。
   - **資料日期窗口**（round 7，owner 決定，OD-21；round 12 定案公開用詞）：Bounding-Box Query 與 POST points 支援最新 31 個連續日曆日（終點＝最新可供 spatial 查詢的日期），Point Query 支援最新 366 個連續日曆日（終點＝最新可供 point 查詢的日期）；point archive 目前起點 2023-01-01（現況，非承諾）（§5.0）。
     這是 owner 選定的 v1 契約方向，固定寫在 OAS 中、不隨部署設定變動；P5 reviewer 已確認其為資料涵蓋政策與設計依據（非無條件或永久保證，§5.0）；F2 已於 2026-11-24 核准，此窗口為 v1.0.0 契約的一部分。

## 2. `info` 與 top-level

以下 YAML 只含 v1 的 top-level 欄位，且符合 OpenAPI 3.1 結構；owner 依 OD-11 決定在 v1 省略的欄位**不出現在 YAML 中**（見下方旁註）。

```yaml
openapi: 3.1.0
info:
  title: ODB Open API of GHRSST (MUR v4.1)
  version: 1.0.0
  description: |
    (資料來源引用；round 7 起不再有全域「Contract notes」，規則放在各 endpoint／response)
servers:                       # round 10，owner 決定（OD-14）
  - url: https://eco.odb.ntu.edu.tw   # 只有 url；paths 已含 /api/...，因此 server URL 不加 /api
# 不設根層 `security`，也沒有 `components.securitySchemes`；兩個 operation 各自明確寫 `security: []`（§2.1）
```

`info.version: 1.0.0` 是 **F2 核准後**的值。Owner 的 F1 同意明確「不代表核准對外宣告 OpenAPI v1.0.0」，
因此 Round 4 實作**維持 `0.4.1`**，並由測試釘住；改為 `1.0.0` 列為 F2 時的單一變更（OD-20）。Round 10 仍維持 `0.4.1`（當時 F2 尚未核准）。
**F2 已於 2026-11-24 核准**：`info.version` 已改為 `1.0.0`（`dev2026/api/app.py` 的 `version=` 一行），snapshot 與版本測試同步（§11.10）。這是 OpenAPI 契約版本，與服務的 `ver 0.x` 發布編號分開。

v1 刻意省略的欄位（owner 依 OD-11 決定，2026-11-20：v1 不提供 `contact`、`license`、`termsOfService`）。產生的 OpenAPI 中**不得**出現這些欄位或任何佔位值；日後若要提供，屬新的 owner 決定與契約變更（§8）：

| OpenAPI 欄位 | 結構要求（OpenAPI 3.1） | 狀態 |
| --- | --- | --- |
| `info.contact` | Contact Object（`name`／`url`／`email`） | v1 省略（owner 決定）[OD-11] |
| `info.license` | License Object，`name` 必填，另可 `identifier` 或 `url`（二擇一） | v1 省略（owner 決定）[OD-11]；資料引用（PO.DAAC MUR v4.1 DOI）照現行 `description` 保留，不等於 API license |
| `info.termsOfService` | URL 字串 | v1 省略（owner 決定）[OD-11] |

Rate limit／配額、SLA、可用性目標：owner 依 OD-11 決定**不作承諾**，v1 文件中不寫任何數字承諾。

### 2.1 已決定的 top-level 與 operation metadata（round 10，owner 決定）

| 項目 | 值 | 說明 |
| --- | --- | --- |
| `servers` | `[{url: https://eco.odb.ntu.edu.tw}]` | OD-14 已決定。server object **只有 `url`**，不放 `description`（APIverse 會讀 server URL 組合請求，不應把說明文字放進 server object）。paths 已含 `/api/...`，server URL 不加 `/api`。本機 Swagger 的 Try it out 因此會指向 production |
| `operationId` | `GET /api/ghrsst` → `getGhrsstData`；`POST /api/ghrsst/points` → `postGhrsstPoints` | 明確、穩定、唯一；取代 FastAPI 依 handler 名稱產生的 `read_ghrsst_api_ghrsst_get`／`read_points_api_ghrsst_points_post`。路由、method、handler 名稱不變 |
| 認證 | 兩個 operation 各自 `security: []`；operation 說明首段加「This is a public API; no authentication is required.」 | 公開 API、不需認證。**不**新增 security scheme，**不**設根層全域 `security`。防護（檢查 FastAPI 原始文件的根層與兩個目標操作）：匿名存取的寫法（`[]`，或只含空 Security Requirement Object 的 `[{}]`；OAS 3.1.1 §4.8.30：`{}` 表示支援匿名存取，單獨的 `[{}]` 表示匿名即可）在沒有 security scheme 時接受，並正規化為契約的 `security: []`（根層則移除）；只要出現實際認證方案——單獨或與 `{}` 並列（如 `[{}, {"bearerAuth": []}]`）——或格式錯誤的 `security`（`null`、非陣列、含非物件元素），或 `components.securitySchemes` 不是「未出現或空物件 `{}`」（含 `null`、`[]` 等錯誤型別），就丟出 `OpenApiSecurityConflict`、拒絕產生文件。判斷依欄位是否存在與型別，不依真假值（空的 `securitySchemes: {}` 接受並移除），不刪除或覆蓋認證需求。這只保護 OAS 描述一致性，不涉及 API 認證行為 |
| `info.version` | `1.0.0` | F2 核准（2026-11-24）時由 `0.4.1` 改為 `1.0.0`（OD-20） |
§5.0 的資料日期窗口是「哪些日期可查」的承諾方向，不是 SLA、uptime 或回應時間承諾。

## 3. 共用 schemas（contract）

設計說明（round 2 修正）：
- GET 的 point 列與 bbox 列**結構相同**（只有「欄位不存在時省略 vs null」的差異），因此以**單一** `DataRow` 描述，
  不用 `oneOf`（兩個結構相同的分支會讓正常回應同時符合兩者而被 `oneOf` 拒絕）。省略／null 的差異以 §5.2 的模式規則
  文字描述，並由 Stage D 的行為測試驗證，不靠 schema 區分。
- `PointsBatchRow` **直接完整定義**（含 `index`），不以 `allOf` 擴充封閉的 `DataRow`（父 schema 的
  `additionalProperties: false` 會拒絕新增的 `index`）。

```yaml
components:
  schemas:
    Date:
      type: string
      pattern: '^\d{4}-\d{2}-\d{2}$'
      maxLength: 10          # round 6：`$` 會接受結尾換行；maxLength 讓 schema 與 BC-1 handler 一致
      description: Product day (UTC calendar date of the MUR analysis), YYYY-MM-DD.
      # 基線 6b32e54 只檢查 pattern，日曆不存在的日期在 point 模式回 500；BC-1（round 5）後所有路徑回 400 "Invalid date format. Use YYYY-MM-DD." [OD-4]

    FieldName:
      type: string
      enum: [sst, sst_anomaly, sea_ice]

    DataRow:             # GET（point / range / bbox format=json）共用
      type: object
      required: [lon, lat, date]
      properties:
        lon:         {type: number, description: Longitude of the nearest grid cell (not the requested value).}
        lat:         {type: number, description: Latitude of the nearest grid cell.}
        date:        {$ref: '#/components/schemas/Date'}
        sst:         {type: [number, 'null'], description: '°C'}
        sst_anomaly: {type: [number, 'null'], description: '°C'}
        sea_ice:     {type: [number, 'null'], description: 'fraction 0–1'}
      additionalProperties: false
      description: >
        Fields that were not requested never appear. A requested field with no value (e.g. land) is null.
        A requested field whose variable does not exist for that day is OMITTED in point/range mode
        and null in bbox mode (see operation description).   # [OD-10]

    PointsBatchRow:      # POST /points 的列；完整定義，不使用 allOf
      type: object
      required: [index, lon, lat, date]
      properties:
        index:       {type: integer, minimum: 0, description: Position of the point in the request array.}
        lon:         {type: number}
        lat:         {type: number}
        date:        {$ref: '#/components/schemas/Date'}
        sst:         {type: [number, 'null']}
        sst_anomaly: {type: [number, 'null']}
        sea_ice:     {type: [number, 'null']}
      additionalProperties: false
      description: Every requested field is present; null when absent for the day or without value.

    PointsRequest:
      type: object
      required: [date, points]
      properties:
        date: {$ref: '#/components/schemas/Date'}
        points:
          type: array
          minItems: 1                     # 現況：空 list → 400（非 422）
          maxItems: 1000                  # 目前部署值；超過 → 413（非 422）[OD-18]
          items:
            type: array
            prefixItems: [{type: number, description: lon}, {type: number, description: lat}]
            minItems: 2
            maxItems: 2                   # 現況：長度 ≠ 2 → 400
        append: {type: [string, 'null'], default: sst, description: Comma-separated FieldName list; default sst.}   # round 11；不在 required
        mode:   {type: [string, 'null'], description: 'Only "truncate".'}
```

說明：`PointsRequest` 中的 `minItems`/`maxItems`/`pattern` 是**文件**；現行 runtime 由 handler 檢查並回 400/413（不是 pydantic 422）。
Stage C 必須確保加入這些文件屬性時**不**被 pydantic 轉成 422 驗證（只寫進 OpenAPI／`json_schema_extra`，不寫成 `Field(min_length=...)`）——否則就是行為變更（[OD-6]）。
因此 request schema 會比 runtime 嚴格（例如空 list 依 schema 不合法，runtime 回 400）；Stage D 的測試需以「runtime 回應」為準，request schema 只驗證合法請求。

## 4. 錯誤 schemas（contract）

```yaml
    ErrorMessage:            # 大部分 4xx/503
      type: object
      required: [detail]
      properties: {detail: {type: string}}

    RangeTooLarge:           # 413 point range
      type: object
      required: [detail]
      properties:
        detail:
          type: object
          required: [error, max_days, requested_days, hint]
          properties:
            error: {const: requested range too large}
            max_days: {type: integer}
            requested_days: {type: integer}
            hint: {type: string}

    TooManyPoints:           # 413 POST points
      type: object
      required: [detail]
      properties:
        detail:
          type: object
          required: [error, max_points, requested_points, hint]
          properties:
            error: {const: too many points}
            max_points: {type: integer}
            requested_points: {type: integer}
            hint: {type: string}

    SpatialWindowError:      # 400 bbox / POST 日期不在空間窗口
      type: object
      required: [detail]
      properties:
        detail:
          type: object
          required: [error, requested_day, spatial_window_days, available_spatial_window, hint]
          properties:
            error: {const: spatial query outside the available window}
            requested_day: {type: string}             # 原樣回顯（可能不是合法日期）
            spatial_window_days: {type: integer}      # retention target（設定值，目前 31），不是實際窗口天數 [OD-7]
            available_spatial_window: {type: string}  # "YYYY-MM-DD..YYYY-MM-DD"（delta 最早..最晚日）或 "(none)"；目前服務中空間資料的最早與最晚日期
            hint: {type: string}

    # 422：沿用 FastAPI 產生的兩個 component，名稱與結構不變。
    # 來源：調查基線 6b32e54 的 generated OpenAPI（components.schemas，與 production openapi.json 語意相同，見 01 §14），
    # 由 fastapi 0.138.0 / pydantic 2.13.4 產生。以下為逐欄轉錄：
    HTTPValidationError:
      title: HTTPValidationError
      type: object
      properties:
        detail:
          title: Detail
          type: array
          items: {$ref: '#/components/schemas/ValidationError'}

    ValidationError:
      title: ValidationError
      type: object
      required: [loc, msg, type]
      properties:
        loc:
          title: Location
          type: array
          items: {anyOf: [{type: string}, {type: integer}]}
        msg:   {title: Message, type: string}
        type:  {title: Error Type, type: string}
        input: {title: Input}                  # 無型別限制
        ctx:   {title: Context, type: object}
```

422 的來源與一致性（已核實）：
- generated OpenAPI 中 `GET /api/ghrsst` 與 `POST /api/ghrsst/points` 的 `422` **都是** `{"description": "Validation Error", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/HTTPValidationError"}}}}`，引用一致。
- 實測 body（01 §12，缺 `lat0`）：`{"detail":[{"type":"missing","loc":["query","lat0"],"msg":"Field required","input":null}]}`，符合上述 schema。
- v1 不另定義 422 schema，直接引用這兩個 component；Stage D 以 snapshot 鎖定其結構（FastAPI／pydantic 升級可能改變它，屆時需 review）。

`ErrorMessage` 與 `SpatialWindowError`（以及 `ErrorMessage` 與 `RangeTooLarge`／`TooManyPoints`）互斥：`detail` 一個是 string、一個是 object，
因此錯誤回應使用 `oneOf` 是安全的（每個 body 恰好符合一個分支）。

## 5. `GET /api/ghrsst`（contract）

### 5.0 資料日期窗口（round 7，owner 決定 2026-11-20）

| 查詢 | 支援窗口 | 適用 |
| --- | --- | --- |
| 空間查詢（Bounding-Box Query） | 最新 **31** 個連續日曆日，以**最新可供 spatial 查詢的日期**為終點 | GET `/api/ghrsst` 的 BBox 查詢、POST `/api/ghrsst/points` |
| Point Query（單日或日期範圍） | 最新 **366** 個連續日曆日，以**最新可供 point 查詢的日期**為終點 | GET `/api/ghrsst` 的 point 查詢 |

- **OAS 公開用詞（round 12，owner 接受；已與 P5 Codex reviewer 討論）**：
  - Point（GET Point Query 的 Coverage）：「Point queries support the latest 366 consecutive calendar days ending on the latest date available to point queries. The archive currently starts on 2023-01-01.」
  - Spatial（GET Bounding-Box Query 的 Coverage）：「Bounding-box queries support the latest 31 consecutive calendar days ending on the latest date available to spatial queries. The same window applies to `POST /api/ghrsst/points`.」POST 的 Date 段以同一窗口描述，並指回 GET 的 Bounding-Box Query。
  - 保留「consecutive calendar days」；不寫 `guaranteed`、`no date missing`、`under normal service` 或內部維運例外，也不寫「較早日期可能可用但不保證」。窗口外的錯誤與目前可用窗口資訊留在既有 400 response 與 `SpatialWindowError`。
  - 不得寫成「剛好 31／366 天」。
- **兩種狀態分開寫**（round 9，owner；round 12 用詞）：
  - **目前 archive 起點（現況，非承諾）**：point 資料目前自 **2023-01-01** 起每日可用，直到最新 point 日期（2026-11-20 唯讀查核：2023-01-01..2026-11-18 共 1,418 天，等於首尾間的日曆天數）。OAS 寫「The archive currently starts on 2023-01-01.」，不宣稱此起點永久不變（`openapi_contract.POINT_COVERAGE_START`）。
  - **支援窗口（OD-21 契約方向）**：上表的 366／31 日。公開用詞已於 round 12 定案；P5 reviewer 已確認其為資料涵蓋政策與設計依據，但不是無條件或永久保證（見下方「P5 意見」）。
- 終點是該查詢種類的最新可用產品日期，**不是今天**。OAS 的 freshness 提示採 owner 指定句：「The newest product date typically lags 1–2 days behind today, depending on upstream GHRSST data availability.」——是典型落後，不是排程承諾；內部抓取時程（晚上抓前一天、隔天早上補抓等）**不寫進 API 契約**。
- 最新日期：point、daily、spatial 三者目前相同（2026-11-20 唯讀查核皆為 2026-11-18；日期無關的 point 與極小 BBox 查詢都回 2026-11-18）。
  OAS 因此不再對一般使用者寫「point 與 spatial 最新日期可能不同」（round 9，owner）；但兩者在程式上仍各自計算（point = base∪delta∪daily；spatial = 近期空間資料層），
  BBox 無日期時依 daily 最新日、不回退的行為照實描述（§5.2）。
- 連續性：OAS 只以「consecutive calendar days」表達（round 12）；不再另寫「no date missing」，也不再寫比窗口更早的日期可能有缺口或不保證（round 8 的寫法已移除）。Point 目前覆蓋範圍本身也連續，因此一般 OAS 不提「缺日略過」；handler 細節保留在 01。
- 「日期可查」不代表每個地點、變數都有非 null 值——此點寫在 `DataRow`／`PointsBatchRow` 與 200 response，不放在 GET 主敘述（round 9）。
- 單次 point/range request 上限為含首尾 366 天（`GHRSST_MAX_DAYS`，部署值）；它與 366 天資料涵蓋窗口是兩件事：
  前者由 `cfg` 讀出、會隨部署設定變動；後者是 OAS 中的固定常數（`openapi_contract.POINT_WINDOW_DAYS`；round 12 前名為 `POINT_WINDOW_GUARANTEE_DAYS`），測試確認兩者互不牽動。
  OAS 中兩者分屬不同 bullet（Coverage／Request limit），Request limit 另寫「This limits the length of one request, not the coverage window above」；
  各 request cap 的「not a rate limit, a quota or a data coverage window」（round 12 前為「… a data-date guarantee」）也表達同一區分。
- `spatial_window_days`（目前 31）是 retention target，不是目前實際窗口長度；實際窗口可能更長。
- 依據：owner 於 2026-11-20 對 VM24 的唯讀確認——production 最新資料日 2026-11-18；同一極小 BBox 逐日查詢
  2026-10-19 至 2026-11-18 共 31 天皆 200 且各有一列；health check 空間窗口 2026-09-04 至 2026-11-18（01 §2；Claude 本輪未連 VM24）。
  Round 9 另依 owner／Codex 同日唯讀查核：point 2023-01-01..2026-11-18 連續；point GET 查 2023-01-01 回 200 且有 SST；
  2025-11-18..2026-11-18 回 366 列、366 個連續日期；VM24 的 BBox 格點上限目前 300,000（程式預設 1,000,000，OAS 由 `cfg.BBOX_POINT_LIMIT` 讀出、不硬編碼）。
- **P5 意見（F2 readiness，2026-11-20）**：P5 Codex reviewer 確認 366／31 日可作為資料涵蓋政策與 P5 資料流程（compaction、replacement、prune、retention）的設計依據；
  這**不是**無條件或永久保證（不代表任何情況、永遠無缺日）。來源為 owner 於對話中提供的答覆，未附 P5 commit SHA（04 OD-21）。
  本規格不得寫成 P5 已驗證任何機制或測試。F2 已於 2026-11-24 由 owner 核准，366／31 日窗口是 v1.0.0 契約的一部分；
  但 F2 核准不改變上述界線：P5 意見仍只是政策／設計依據，不代表 P5 機制或測試已驗證，也不是永久保證。這些內部流程與維運細節**不寫進公開 OAS**（契約測試檢查），OAS 也不加「under normal service」。

### 5.1 參數
同 [01 §3](01_current_contract_inventory.md#3-get-apighrsst--參數)，在 OpenAPI 中補上：
- `start`/`end`：`$ref: Date`；
- `append`：說明允許值與預設 `sst`；schema 另寫 `default: "sst"`（round 11，與 `PointsRequest.append` 一致；參數仍是 `required: false`）；
- `sample`：`minimum: 1`（文件）；說明 point 模式只允許 1；
- `mode`：說明只允許 `truncate`（5 位座標／3 位數值）；
- `format`：v1 文件只列 `json`（[OD-8]）。Handler 先 trim 再轉小寫（空值視為預設 `json`），所以 `JSON`、` json ` 也合法；
  因此 schema 保持 `type: string`、**不加 `enum`**（round 5，Codex round-4 review）。Round 7 description 縮短為
  「Only JSON is supported (case-insensitive). Omitted or empty means JSON.」；不支援值回 400 寫在 400 response；
- `lon0`/`lat0`/`lon1`/`lat1`：參數欄位只寫角色與範圍、不支援跨換日線；「必須是有限數值、NaN／Infinity／-Infinity 為驗證錯誤；有限超界值 clamp 到最近格點」
  在 endpoint 說明中**只寫一次**，實際狀態碼（422）與錯誤格式寫在 Response（round 7；[OD-5]，BC-2 round 6 已實作）。
- Round 7 起參數說明不使用「`-> 400`」之類的簡寫；狀態碼一律在 Response 區說明。

### 5.2 查詢類型與日期規則（寫入 operation description）
- Query type：`lon1` 與 `lat1` 都提供且 `(lon1,lat1)` 不等於 `(lon0,lat0)` → Bounding-Box Query；否則 Point Query（只提供其一也是 point，[OD-17]）。
- Point Query：
  - Dates：省略 `start`、`end` → 最新 point 日期；只提供其一（`start` 或 `end`）→ 單日；兩者都提供 → 含首尾範圍、順序不限。
  - Coverage：最新 366 個連續日曆日（終點＝最新可供 point 查詢的日期）；archive 目前起點 2023-01-01（現況，非承諾）（§5.0，round 12 用詞）。
  - Request limit：單次最多 366 個含首尾日期（`cfg.MAX_DAYS`，部署值）→ 超過 413；較長範圍分批。明寫此上限限制的是單次 request 長度，不是 Coverage 窗口（round 12）。
  - Handler 細節（不寫進一般 OAS 主敘述，見 01 §4）：範圍 clamp 到 point 可用期間；範圍內不存在的日期靜默略過、全部不存在 → 400；
    欄位對該日不存在 → 該列**省略**該欄位（寫在 `DataRow` 與 200 example `point_field_absent`）。
- Bounding-Box Query：
  - Dates：單日。只提供 `start` 或只提供 `end` 都可查該日；兩者都提供時必須相同，不同 → 400。
    省略時使用**每日資料（daily）的最新日**，再套用空間窗口檢查；若該日尚未可供 spatial 查詢，回 400 `SpatialWindowError`，**不會**回退到較早日期。
  - Coverage：最新 31 個連續日曆日（終點＝最新可供 spatial 查詢的日期；§5.0，round 12 用詞），同樣適用 `POST /api/ghrsst/points`；指定日期不在空間窗口 → 400 `SpatialWindowError`（寫在 400 response，不寫在 Coverage）。
  - Request limit：`sample` 抽稀後格點數上限 `cfg.BBOX_POINT_LIMIT`（目前 VM24 300,000；程式預設 1,000,000），OAS 動態顯示，標明為目前 request cap、不是 quota；Swagger 小 BBox 提示留在此段。
  - 欄位對該日不存在 → 值為 **null**（寫在 `DataRow`）。
- 空間窗口：以**逐日**是否屬於近期資料層（delta tier）判定。`available_spatial_window` 告知目前服務的最早與最晚日；
  OAS 中此欄位只寫「First and last date of the spatial data currently served, as "YYYY-MM-DD..YYYY-MM-DD", or "(none)".」；
  round 12 起不再附「最新 31 日皆可用、更早日期可能有缺口」的補充（窗口本身只在 Coverage 寫一次）。
  `spatial_window_days` 是保留目標天數，不是目前窗口長度（[OD-7]）。
- 最新日期：point／daily／spatial 目前相同（§5.0）；OAS 不再寫「可能不同」，但 BBox 無日期的 daily-latest＋不回退行為照實描述。
- Round 7 起 `info.description` 不再寫「Current production data starts at 2023-01-01」；round 9 改在 GET 的 Point Query「Coverage」寫成**目前**覆蓋範圍（不是承諾）。
- OAS 呈現方式（round 9，owner 指定）：一句話用途 → **Coordinates**（有限數值、拒絕 NaN／±Infinity、有限超界 clamp）→ **Query type** →
  **Point Query**（Dates／Coverage／Request limit）→ **Bounding-Box Query**（Dates／Coverage／Request limit／Swagger 提示）→ **Data freshness**（一句 freshness、
  「Omit date parameters to request the latest available data.」、範例日期為示意）。不使用「Mode」「Point and point time series」，也不再有獨立的「Data date windows」大段。
- POST 說明的「Date」段：31 日窗口（同 Bounding-Box Query）＋同一句 freshness＋範例日期提醒；每個 operation 各一處 freshness，`info.description` 不放。

### 5.3 回應

| Status | Media type | Schema | 說明 |
| --- | --- | --- | --- |
| 200 | application/json | `{type: array, items: DataRow}` | point 與 bbox 共用；兩種情況各附 example |
| 400 | application/json | `oneOf: [ErrorMessage, SpatialWindowError]` | 見 01 §12 |
| 413 | application/json | `RangeTooLarge` | point range 過長 |
| 422 | application/json | `$ref: '#/components/schemas/HTTPValidationError'` | 型別錯誤／缺必填（與 POST 相同引用） |
| 503 | application/json | `ErrorMessage` | 三種觸發共用此 status，見 §5.3.1；**只有 overload 帶 `Retry-After`** |

500：**不列為 contract 回應**。基線中可由日曆不存在的日期觸發；BC-1（round 5）已改為 400（[OD-4]，05）。

#### 5.3.1 GET 的 503 情境

OpenAPI 每個 operation 的同一 status 只能有一個 Response Object，因此 GET 的 503 用**一個** response 描述三種情境，
並把 `Retry-After` 宣告為**非必有**（`required: false`）的 header：

| 情境 | 觸發 | Body（`ErrorMessage`） | `Retry-After` | 證據（`6b32e54` 基線行號，見 01 標頭） |
| --- | --- | --- | --- | --- |
| 無 point 可用日 | point 模式，point 可用期間為空 | `{"detail":"No available dates."}` | **無** | `app.py:470`（`HTTPException(503, ...)`，未設 headers） |
| 無 daily 可用日 | bbox 模式，daily store 為空 | `{"detail":"No available dates for spatial queries."}` | **無** | `app.py:541` |
| Overload | admission 在 `GHRSST_OVERLOAD_WAIT_MS` 內取不到 permit（point 讀取或 bbox stream 開始前） | `{"detail":"server overloaded; retry later"}` | **有**，值 `1` | `app.py:329-333`（exception handler 明確設定 header） |

```yaml
'503':
  description: >
    Service unavailable. Either no dates are available (no Retry-After), or the server is
    overloaded (Retry-After present). Clients should only rely on Retry-After when it is sent.
  headers:
    Retry-After:
      required: false
      description: Seconds to wait before retrying. Sent ONLY for the overload case (currently 1).
      schema: {type: integer}
  content:
    application/json:
      schema: {$ref: '#/components/schemas/ErrorMessage'}
```

證據強度：三種情境的 status／body／header 依程式碼核實，並已由 Stage D 的**本機 HTTP 層測試**覆蓋
（`dev2026/tests/test_api_v1_contract.py`；FastAPI TestClient 搭配模擬資料）：空資料庫時兩種「無可用日」503 不帶 `Retry-After`
（`test_no_available_dates_503s_have_no_retry_after`）；0 permit executor 模擬 overload 時 GET point、GET bbox、POST 的 503 帶 `Retry-After: 1`（`test_overload_503_has_retry_after`）。
**未對 production 實測**，也沒有在 production 觀察到任何 503。基線原有的 `test_api.py:229`（`test_overload_sheds`）只斷言 executor 層拋出 `Overloaded`。

### 5.4 Response headers

| Header | 適用 | 值 | 層級 |
| --- | --- | --- | --- |
| `Cache-Control` | 所有 200 | `no-store` 或 `public, max-age=<seconds>`；規則見 01 §10.1 | contract（規則）；秒數為部署值（[OD-2]） |
| `X-Store-Route` | point 200 | `cube` \| `daily` \| `mixed` | informational（[OD-9]） |
| `X-Served-Rows` | bbox 200 | integer，回傳格點數 | contract（建議） |
| `X-Stride` | bbox 200 | integer，實際 stride | contract（建議） |
| `X-Bbox-Format` | bbox 200 | `json`（compact 開啟時為 `coveragejson`） | contract（建議） |
| `X-Read-Ms`／`X-Encode-Ms` | bbox 200 | 數字（ms） | informational（[OD-9]） |
| `Retry-After` | **僅** overload 造成的 503（無可用日期的 503 不帶） | `1`（秒） | contract（「僅 overload 帶」為規則；秒數為部署行為） |

Proxy 產生的 `X-API-Cache`、`Vary`、`Server` **不屬於** API 契約，只在部署文件說明。

### 5.5 Examples（取自 2026-11-19 實測；空間範例會隨窗口移動而過期）
- point latest：`?lon0=122.5&lat0=23.5` → `[{"lon":122.5,"lat":23.5,"date":"<latest>","sst":...}]`
- point range：見 01 §4 實測。
- bbox：`?lon0=121&lat0=24&lon1=121.02&lat1=24.02`（無日期 → daily latest）→ 9 列。
- 錯誤：400 日期格式、400 SpatialWindowError（`start=2024-06-01` 的 bbox）、413 RangeTooLarge、422 缺 `lat0`（皆有實測）。
- 503 範例需分兩個 named examples，並標明來自程式碼而非實測：
  - `no_available_dates`：body `{"detail":"No available dates."}`，**無** `Retry-After`；
  - `overloaded`：body `{"detail":"server overloaded; retry later"}`，header `Retry-After: 1`。
- 空間範例日期**不寫死**，或明確標示「日期需在 `available_spatial_window` 內且屬於空間窗口」（[OD-14]）；現行 POST 範例 `2026-05-31` 已失效。

## 6. `POST /api/ghrsst/points`（contract）

| Status | Schema | 說明 |
| --- | --- | --- |
| 200 | `{type: array, items: PointsBatchRow}` | 與輸入同序、重複保留；`Cache-Control: no-store` |
| 400 | `oneOf: [ErrorMessage, SpatialWindowError]` | 日期格式、欄位、mode、空 list、點長度、窗口外、日期不可用 |
| 413 | `oneOf: [TooManyPoints, ErrorMessage]` | 點數超過目前上限（object）；chunk fan-out 超過目前上限（string）[OD-6] |
| 422 | `$ref: '#/components/schemas/HTTPValidationError'` | body 缺欄位或型別錯（與 GET 相同引用） |
| 503 | `ErrorMessage`，header `Retry-After`（值 `1`） | **只有** overload 一種情境：`{"detail":"server overloaded; retry later"}`（`app.py:329-333`；admission 在 `bex.run` 時取得，`app.py:667`）。POST 沒有「無可用日期」的 503：日期不可用時回 400（`app.py:663-665`）。依程式碼核實，並由 Stage D 本機 HTTP 層測試覆蓋（TestClient＋模擬資料：`test_overload_503_has_retry_after`、空資料庫時回 400 的 `test_post_with_no_data_is_400_not_503`）；未對 production 實測 |

說明：POST 的 `Retry-After` 可在此 operation 宣告為 `required: true`（唯一的 503 情境必帶），
或與 GET 一致宣告為 `required: false` 並在 description 說明。**Stage C 選擇 `required: false`**，description 寫明
「overload 是本 operation 唯一的 503，因此目前一定會送」。理由：與 GET 一致，且不把「每個 503 都帶 header」寫成承諾——
P5 的 `ee84037`（未部署）會新增 maintenance 503，整合時本段需重新檢視（§7）。

規則：單日；日期必須屬於空間窗口（§5.0：最新 31 個連續日曆日，與 GET Bounding-Box Query 相同）；座標 clamp + 最近格點；回應帶 `Cache-Control: no-store`（寫在 200 header）。
Round 7 呈現方式：主要描述以白話說明順序、重複點與 `index`（輸入陣列中的零起算位置，可用來對回原始點）；
不使用 `storage chunks` 等內部術語，只說「點位分散於過大範圍時可能被拒絕，請依地理位置分成較小批次」。
Fan-out 的目前部署值（64）只寫在 413 response，並說明依地理區分批。

## 7. 非 contract 但需在文件說明的部署事實
- 公開 base URL、Swagger 位置。
- CORS：目前沒有 CORS headers（[OD-12]）。
- Proxy 快取：GET 依 `Cache-Control` 快取；錯誤可能被 proxy 快取約 1 分鐘。2026-11-19 曾觀察到 POST 回應錯置（01 §10.3）；
  正式宣告 POST 契約前（Gate F2）需確認公開端點對不同 body 回應正確（[OD-13]）：**已完成**——2026-11-20 owner 修改並 reload NGINX 設定後，公開 POST spot check 通過（01 §10.4、03「Ops-1 驗收結果」）。範圍只限該請求序列的 POST body 正確性；未測 GET cache 行為，也未驗證其他快取情境。
- `/healthz` 不公開。
- **與 P5 整合時必須重新檢視的契約內容**（不在本輪處理，由 owner 安排整合）：`ee84037` 新增的 maintenance gate 會讓 GET 與 POST
  回 503 + `Retry-After: 60` + `Cache-Control: no-store`。整合後「`Retry-After` 只在 overload 送出」、「POST 唯一的 503 是 overload」、
  「錯誤回應沒有 `Cache-Control`」三點都不再成立；契約測試（`test_api_v1_contract.py`）不會自動發現這個差異，除非整合時補上 maintenance 情境的測試。
  同一 commit 的 `max-age` 秒數改變只影響 description 中的「目前值」（由設定讀取），不影響規則。
- **部署目標（F2 readiness，X2）**：v1 契約對應 `6b32e54` 加上 API v1 候選變更（含已核准的 BC-1／BC-2）。目前 production（`6b32e54`）尚未包含這些本機改動。若部署前整合 `ee84037` 或其他會改變 503／headers 的 P5 變更，必須先更新並 review OAS（04 決定紀錄）。

## 8. 版本與相容政策（[OD-16]，owner 決定 2026-11-20）
- endpoint 路徑固定為 `/api/ghrsst`、`/api/ghrsst/points`；不做 URL 版本化（不加 `/v1`），不改路由。
- `1.0.0` 是第一個正式對外的 OpenAPI 契約版本；過去版本未正式公告，不設定舊版棄用期限或過渡期。
- 從 `1.0.0` 起 `info.version` 採 SemVer 契約版本慣例：不相容的契約變更（移除或改變既有欄位語意、狀態碼、預設值等）→ major；向後相容的功能增補（新增欄位／header／錯誤細節等）→ minor；相容修正 → patch。
- 不承諾固定的棄用時程或公告管道。
- 實際部署版本需能從 commit 追溯（00 §2 顯示版本字串目前無法識別部署）。

## 9. 舊 Draft 的處置
`specs/ghrsst_api_v0_1_0.spec` 描述舊 `ghrsst_app.py`，與現況在至少 9 處矛盾（01 §13）。Owner 已採下列建議（[OD-3]，2026-11-20）；**已於 F2 核准（2026-11-24）後執行**（已收入實作 commit `b9bd4e4`）：
1. Gate F2 核准時，在舊檔頂端加「SUPERSEDED by dev2026/specs/ghrsst_v1/…（v1.0.0）；僅描述 legacy `ghrsst_app.py`」橫幅，內容不再維護；
2. 同一 PR 更新 `AGENTS.md` 中「authoritative interface reference」那一行，改指 v1 規格與 generated OpenAPI；
3. 不刪除舊檔（legacy rollback launcher 仍指向 `ghrsst_app.py`）。
第 1、2 項已完成：舊檔頂端已加 SUPERSEDED 標示，`AGENTS.md` 已改指 v1 規格與 generated OpenAPI；舊檔未刪除。

## 10. 未納入 v1（除非 owner 決定）
- CoverageJSON-lite（`format=coveragejson|raster`）：production 關閉，前端契約未核准。
- `/healthz` 公開化。
- 認證、配額、分頁、跨換日線 bbox、插值。

## 11. Stage C 實作對照（round 4）

實作位置：
- `dev2026/api/openapi_contract.py`（新檔）：§2–§6 的全部內容。`install(app, cfg)` 取代 `app.openapi`，在 FastAPI 產生的文件上套用契約；
  沒有任何 request path 呼叫它。FastAPI 自己的 `app.openapi_schema` 快取保留原始文件，契約版本另外快取。
- `dev2026/api/app.py`：metadata 部分只在檔尾新增 9 行（註解、import、`install(app, cfg)`）；round 5 的 BC-1 另外只改 `_parse_date`（§11.1）；round 6 的 BC-2 另改座標 `Query`、`PointsRequest.points` 並新增 422 handler（§11.2）。兩者皆與 metadata 分開審查。以下為 round 4 的 metadata 範圍：沒有修改 `Query(...)`、`PointsRequest`、decorator、
  `FastAPI(...)` 參數或任何 handler，因此 P5 分支（`ee84037`、`d7ba064`、`1003b25`）在 `app.py` 的修改區段都不重疊（以 `patch --dry-run` 確認可乾淨套用）。

與 §3–§6 草案的對照與實作細節：

| 項目 | 實作 |
| --- | --- |
| `info.version` | Round 4 維持 `0.4.1`（§2，OD-20）；F2 核准後改為 `1.0.0`（§11.10） |
| `info.description` | （round 4）保留 `app.py` 的原文，後面附加「Contract notes」。Round 7 改為只留資料來源引用與 Swagger 提示，見 §11.3 |
| `contact`／`license`／`termsOfService`／`servers` | （round 4）不產生；若 generated 文件中出現會被移除；測試確認不存在且全文無 `TBD`。Round 10：`servers` 改為 owner 決定的 production host（§2.1）；其餘三項仍不產生 |
| `DataRow`、`PointsBatchRow`、錯誤 schemas | 結構同 §3–§4；另加欄位說明 |
| `PointsRequest` | 取代 pydantic 產生的 component：`date` → `$ref Date`；`points` 加 `minItems: 1`、`maxItems: <cfg.POINTS_BATCH_MAX>`、`prefixItems`；未加 `additionalProperties: false`（runtime 會忽略多餘欄位）。舊的 `2026-05-31` 範例隨之移除，改在 requestBody 放一個標明「illustrative，date 必須在目前空間窗口內」的 named example。測試確認文件欄位與 pydantic model 欄位、必填集合一致 |
| `HTTPValidationError`／`ValidationError`／兩個 422 | 不動，沿用 FastAPI 產生的內容；測試逐欄比對 §4 的轉錄 |
| 參數（§5.1） | （round 4；round 7 起說明文字已精簡，見 §11.3）只改說明與文件關鍵字：`start`／`end` 的 string 分支加 `pattern`（round 6 另加 `maxLength: 10`）；`sample` 加 `minimum: 1`；`format` 只改 description（round 5 移除 round 4 的 `enum: [json]`，見 §5.1）；座標說明 clamp、最近格點、不支援跨換日線。位置、必填、型別、預設值與 FastAPI 原始文件相同（測試比對） |
| `summary` | 改為可讀文字；`operationId`（round 4）不變（測試釘住）。Round 10 依 owner 決定改為 `getGhrsstData`／`postGhrsstPoints`（§2.1） |
| 數值上限 | 由部署設定 `cfg` 讀出，寫成「current deployment limit, not a service guarantee」（OD-18）。Snapshot 以 production 設定產生（bbox 上限 300000、空間窗口 enforce=1） |
| 空間窗口說明 | 依 `cfg.SPATIAL_WINDOW_ENFORCE` 產生：開啟時描述窗口規則；關閉時寫明該部署不 enforce |
| Headers | GET 200：`Cache-Control`（required）、`X-Store-Route`／`X-Read-Ms`（informational）、`X-Served-Rows`／`X-Stride`／`X-Bbox-Format`；POST 200：`Cache-Control`；503：`Retry-After`（`required: false`，兩個 operation 皆同）。`X-Encode-Ms` 只出現在已關閉的 compact 格式，未列入（OD-8） |
| Examples | 標「Observed 2026-11-19」的取自 01 的實測；其餘標「illustrative」只示範形狀。所有 examples 由測試以其 schema 驗證 |

已知取捨：`app.py` 中 `Query(description=...)` 的原文仍在，但公開文件中的參數說明以 `openapi_contract.py` 為準。
把說明搬回 `app.py` 會碰到 P5 正在修改的相鄰行，因此留到 owner 安排的整合階段再決定（OD-15）。

### 11.1 Round 5 變更（2026-11-20；三個可分開審查的區塊）

| 區塊 | 檔案 | 目的 |
| --- | --- | --- |
| Metadata（format） | `api/openapi_contract.py`（`format` 參數）、`tests/test_api_v1_contract.py`（2 個新測試）、snapshot | 移除 `enum: [json]`；description 寫明 trim＋不分大小寫、空值為預設、其他值 400；測試驗證 schema 不再限制 enum，並以 TestClient 驗證 `json`／`JSON`／前後空白／空值在 point 與 bbox 都 200，不支援的格式 400 |
| BC-1（行為變更，已核准） | `api/app.py`（`_parse_date`，1 行改為 9 行，淨 +8）、`api/openapi_contract.py`（`Date`／`start` 說明、`invalid_date` example summary）、`tests/test_api_v1_contract.py`（BC-1 測試，移除 500 釘住測試）、snapshot | 日曆不存在的日期在所有路徑回 400（05） |
| BC-2 | （round 5 未實作；見 §11.2） | round 5 暫停：只加 `allow_inf_nan=False` 會讓 POST 字面值 NaN／Infinity 由 200 變 500（05） |

### 11.2 Round 6 變更（2026-11-20；兩個可分開審查的區塊）

| 區塊 | 檔案 | 目的 |
| --- | --- | --- |
| Metadata（日期 `maxLength`，Codex round-5 review） | `api/openapi_contract.py`（`Date` 與 `start`／`end` 的 string 分支加 `maxLength: 10`）、`tests/test_api_v1_contract.py`（2 個新測試）、snapshot | `^\d{4}-\d{2}-\d{2}$` 的 `$` 在部分 regex 方言（含 Python）會接受結尾換行；加 `maxLength: 10` 後，generated schema 拒絕 `2025-01-01\n`，與 BC-1 handler 回 400 一致。Pattern 與 BC-1 錯誤文字不變 |
| BC-2（行為變更，owner 核准 B2-b） | `api/app.py`（4 個座標 `Query` 加 `allow_inf_nan=False`；`PointsRequest.points` 改 `List[List[FiniteFloat]]`；新增全域 `RequestValidationError` handler）、`api/openapi_contract.py`（座標與 `points` 說明、Contract notes）、`tests/test_api_v1_contract.py`（7 個 BC-2 測試）、snapshot | 非有限座標 → 422 `HTTPValidationError`；422 `input` 中的非有限 float 寫成 `"NaN"`／`"Infinity"`／`"-Infinity"`；一般 422 位元組不變；有限超界座標仍 clamp。422 response 仍是 FastAPI 產生的 `$ref HTTPValidationError`，沒有新的錯誤格式 |

`app.py` 的執行行為變更（BC-1 的 `_parse_date`、BC-2 的參數驗證與 422 handler）需由 owner 與 P5 reviewer 協調；三方合併檢查見 05。

### 11.3 Round 7 變更（2026-11-20；只改 OpenAPI metadata）

| 項目 | 實作（`api/openapi_contract.py`） |
| --- | --- |
| 資料日期窗口（§5.0） | 新常數 `SPATIAL_WINDOW_GUARANTEE_DAYS = 31`、`POINT_WINDOW_GUARANTEE_DAYS = 366`（固定、不讀 `cfg`）。GET 說明的「Data date windows」段同時寫 point（366）與 bounding box（31，並註明同樣適用 POST）；POST 說明的「Data date window」段只寫 31 日窗口。兩處都說明終點是最新產品日期、可能落後今天，以及「日期可查不代表每個地點／變數非 null」 |
| 全域 `Contract notes` | 移除。`info.description` 只留資料來源引用與「Swagger 中請用小範圍 BBox」提示 |
| 錯誤格式與原因 | 寫在各 Error Response：400 以條列列出原因（含 `SpatialWindowError` 與 `spatial_window_days` 語意）；413 寫上限與切分方式；422 只改 description（原因；POST 另說明 `input` 中非有限值的字串化），content 仍是 FastAPI 的 `$ref HTTPValidationError` |
| Headers | 仍在各自 Response（GET 200：`Cache-Control`、`X-Store-Route`、`X-Served-Rows`、`X-Stride`、`X-Bbox-Format`、`X-Read-Ms`；POST 200：`Cache-Control`；503：`Retry-After`） |
| Request caps | range 天數與 BBox 格點數寫在 GET 說明對應段落與 413／400；點數寫在 POST 說明與 413；fan-out（64）只在 POST 413。皆註明為目前請求處理上限，不是 rate limit、配額或資料日期窗口承諾 |
| 日期範例提醒 | GET 說明的日期窗口段末、POST 說明的日期窗口段末、各 example summary |
| GET 說明結構 | 用途與 BBox 判定 → Point and point time series → Bounding box → Data date windows → Coordinates；移除「**Mode**」 |
| 參數 | 精簡；不重複 NaN／clamp 規則，不寫 `-> 4xx` |
| POST | 白話說明順序、重複點與零起算 `index`；主要描述不出現 chunk／fan-out 數值 |
| 不變 | tags 與 Swagger UI 設定（仍是 `default` 群組）；`info.version` `0.4.1`；所有 schema 結構、型別、必填、status、header 宣告、路由——以「忽略 description／summary 後的 snapshot 完全相同」驗證；`app.py` 本輪未修改 |

### 11.4 Round 8 變更（2026-11-20；只改 OpenAPI metadata）

| 項目 | 實作 |
| --- | --- |
| 窗口內連續性 | 兩個窗口句子都加上「no date in this window is missing」，並把「Older dates ... are not guaranteed」補成「... and may have gaps」——缺口只可能出現在保證窗口以外的較早日期 |
| Point 缺日規則 | GET「Point and point time series」段：「Every date inside the guaranteed point window (below) has a row. For older parts of a range, dates without data are skipped.」；200 response：「every date inside the guaranteed point window has a row, and only older dates without data are skipped」 |
| `available_spatial_window` | 「The latest 31 consecutive calendar dates ending on the last date are all available; only dates earlier than those may have gaps.」 |
| 兩種最新日期 | point 窗口終點寫成「newest product date available to point queries」、空間窗口寫成「newest product date available to spatial queries」；GET 加一句「Point queries and spatial queries each have their own newest product date; the two can differ.」；無日期 BBox：「the newest date in the daily data is used and then checked against the spatial window; if it is not served yet, the request fails instead of falling back to an earlier date」 |
| Swagger 提示 | 小範圍 BBox 提醒由 `info.description` 移到 GET「Bounding box」段 |
| POST 413 fan-out example | `value` 仍是 runtime 實際 body；新增 summary「Points cover too many areas: split them into smaller batches of points that are close together」 |
| 其他 | `point_field_absent` example summary 由「missing」改為「does not exist」，避免與日期缺漏混淆 |
| 不變 | 路由、handler、驗證、狀態碼、headers、快取；`default` 群組與 Swagger UI 設定；`info.version` `0.4.1`；snapshot 去掉 description／summary 後與 round 7、round 6 皆相同 |

### 11.5 Round 9 變更（2026-11-20；只改 OpenAPI metadata）

| 項目 | 實作（`api/openapi_contract.py`） |
| --- | --- |
| GET 說明結構 | 一句話用途 → **Coordinates** → **Query type** → **Point Query**（Dates／Coverage／Request limit）→ **Bounding-Box Query**（Dates／Coverage／Request limit／Swagger 提示）→ **Data freshness**。移除「Point and point time series」「Bounding box」「Data date windows」 |
| Point 目前覆蓋 | 新常數 `POINT_COVERAGE_START = "2023-01-01"`；Coverage 寫「Point data is currently available for every date from 2023-01-01 through the newest point date.」，後接 366 日 gap-free 保證句。全文件只出現一次 2023-01-01 |
| 缺日略過 | 一般 OAS 全部移除「skipped」（GET 主敘述與 200 response）；200 改為「one row per available date in the requested range, oldest first」。Handler 行為不變，細節保留在 01 |
| 欄位缺漏 | 「point 省略／BBox null」不再出現在 Point Query／Bounding-Box Query 主敘述；保留在 `DataRow` 說明與 200 example `point_field_absent` |
| 最新日期 | 移除「Point queries and spatial queries each have their own newest product date; the two can differ.」；point 窗口終點寫「newest point date」、空間窗口寫「newest date available to spatial queries」；BBox 無日期仍寫 daily 最新日＋不回退 |
| Freshness | 新常數 `FRESHNESS`（owner 指定句）；GET 的 Data freshness 段與 POST 的 Date 段各一次；GET 後接「Omit date parameters to request the latest available data.」。移除舊的「The newest product date can lag behind today. …non-null value.」 |
| BBox 日期參數 | 主敘述：「Give `start` or `end` alone, or both with the same date.」；`start`／`end` 參數說明改為 Point Query／Bounding-Box Query 兩種角色（`end` 註明與 `start` 同時提供時必須相同），不再寫 first／last date |
| BBox 格點上限 | 說明、400 cause 與 400 example `bbox_too_large` 的上限都由 `cfg.BBOX_POINT_LIMIT` 產生（example summary 改為「Box larger than the current cell limit」）；snapshot 以 production 設定 300000 產生，因此 example 值與 round 8 相同 |
| GET summary | 「Point Query or Bounding-Box Query」 |
| POST | 「Data date window」段改為「Date」段（31 日窗口＋freshness＋範例日期提醒）；其他不變 |
| 不變 | 路由、handler、驗證、狀態碼、headers、快取；`default` 群組與 Swagger UI 設定；`info.version` `0.4.1`；`available_spatial_window`／`spatial_window_days` 說明；snapshot 去掉 description／summary 後與 round 8 相同（所有 example 值也相同） |

### 11.6 Round 10 變更（2026-11-20；只改 OpenAPI metadata）

| 項目 | 實作（`api/openapi_contract.py`） |
| --- | --- |
| `servers` | 常數 `SERVERS = [{"url": "https://eco.odb.ntu.edu.tw"}]`（只有 `url`；round 10 修補移除原本的 `description: Production`）；`apply_contract` 寫入根層 `servers`（原本是 `doc.pop("servers")`） |
| `operationId` | 常數 `OPERATION_IDS`；只改寫 OpenAPI 文件中的 `operationId`，不改 handler 名稱、路由或 method。FastAPI 原始文件（`app.openapi_schema`）仍是自動產生的 ID，測試確認 |
| `security` | 兩個 operation 各寫 `security: []`；不新增 `securitySchemes`。Round 10 修補：寫入前先由 `_check_no_generated_security` 檢查原始文件（在任何修改之前；失敗時文件不會被快取或送出）。依 Codex review 修正：匿名存取的寫法（`[]`，或只含空 Security Requirement Object 的 `[{}]`；OAS 3.1.1 §4.8.30：`{}` 表示支援匿名存取，單獨的 `[{}]` 表示匿名即可）在沒有 security scheme 時接受，並正規化為契約的 `security: []`（根層則移除）；只要出現實際認證方案——單獨或與 `{}` 並列（如 `[{}, {"bearerAuth": []}]`）——或格式錯誤的 `security`（`null`、非陣列、含非物件元素），或 `components.securitySchemes` 不是「未出現或空物件 `{}`」（含 `null`、`[]` 等錯誤型別），就丟出 `OpenApiSecurityConflict`、拒絕產生文件。判斷依欄位是否存在與型別，不依真假值（空的 `securitySchemes: {}` 接受並移除）（`_anonymous_only`） |
| 免認證說明 | 常數 `PUBLIC_NO_AUTH`（「This is a public API; no authentication is required.」）接在 GET、POST 說明第一句之後；其他說明文字不變 |
| 不變 | 路由、methods、參數、request／response schemas、status codes、headers、examples、其他說明文字；`info.version` `0.4.1`；`contact`／`license`／`termsOfService` 仍省略。以 snapshot 前後比對確認：差異只有 `servers`、兩個 `operationId`、兩個 `security` 與兩處加句 |

Round 10 修補（同日，依 owner 指示；只改 OpenAPI metadata 處理與測試）：OpenAPI 前後比對只有一處差異——移除 `/servers/0/description`（`Production`）。
認證防護不改變正常輸出；它只在 FastAPI 原始文件出現認證需求時讓文件產生失敗。
依 Codex review 再修正：單獨的 `[{}]`（匿名即可）不再視為衝突，改為正規化成 `security: []`；匿名與認證方案並列仍拒絕。Snapshot 與正常輸出不變。
依 Codex review（P2）再修正：guard 改以欄位是否存在與型別判斷——根層 `security: null`、`securitySchemes: null`／`[]` 原本會因 `None`／falsey 被略過（根層 `null` 還會被移除），現在一律拒絕；空的 `securitySchemes: {}` 接受並移除。Snapshot 與正常輸出不變。

### 11.7 Round 11 變更（2026-11-20；只改 OpenAPI metadata 與測試）

| 項目 | 實作（`api/openapi_contract.py`） |
| --- | --- |
| GET `append` | `_param_docs` 的 `append` 加 `schema_patch: {"default": "sst"}`，寫入參數 schema（與 `anyOf`、`description`、`example` 同層）。參數 `required: false` 不變 |
| POST `PointsRequest.append` | `component_schemas` 的 `append` 加 `default: "sst"`；`required` 仍為 `["date", "points"]` |
| 意義 | 文件化既有的有效預設：省略（或空字串）時 handler 回 `sst`。handler 的 `Query(None)`／`Optional[str] = None`、路由與執行行為都不變 |
| 不變 | `mode` schema（不加 `default`、不加 `enum`、不加空字串選項）；其他 OAS metadata；`info.version` `0.4.1` |
| OAS 前後比對 | 修補前生成的文件與修補前 snapshot 位元組相同；修補後差異只有兩處新增：`/components/schemas/PointsRequest/properties/append/default` 與 GET `append` 參數的 `/schema/default`，值皆為 `"sst"` |
| Regression tests（不改程式邏輯） | `test_contract_refuses_to_hide_authentication` 加 `components: null`、`components: []` 兩個拒絕案例：確認 `apply_contract()` 丟出 `OpenApiSecurityConflict` 且輸入文件前後相同。程式本來就拒絕非物件 `components`（round 10 P2 修正） |

### 11.8 Round 12 變更（2026-11-20；只改 OpenAPI 說明文字、snapshot 與測試）

用詞已與 P5 Codex reviewer 討論、owner 接受；這只定案公開用詞，不代表 OD-21 的 P5 資料流程保障或其他 F2 前置事項已完成。

| 項目 | 實作（`api/openapi_contract.py`） |
| --- | --- |
| GET Point Query Coverage | 「Point queries support the latest 366 consecutive calendar days ending on the latest date available to point queries. The archive currently starts on 2023-01-01.」（取代「point data is currently available for every date from 2023-01-01 …」＋「… are guaranteed to be available, with no date missing.」） |
| GET Point Query Request limit | 加一句「This limits the length of one request, not the coverage window above」；上限值仍由 `cfg.MAX_DAYS` 讀出 |
| GET Bounding-Box Query Coverage | 「Bounding-box queries support the latest 31 consecutive calendar days ending on the latest date available to spatial queries. The same window applies to `POST /api/ghrsst/points`.」（移除 guaranteed、no date missing、「Earlier dates may be available but are not guaranteed.」） |
| POST Date Coverage | 「This endpoint supports the latest 31 consecutive calendar days ending on the latest date available to spatial queries. This is the same window as a Bounding-Box Query on `GET /api/ghrsst`.」 |
| `SpatialWindowError.available_spatial_window` | 移除「The latest 31 … are all available; only dates earlier than those may have gaps.」，只留欄位格式說明。窗口外的錯誤說明仍在 GET／POST 的 400 response（未改） |
| `NOT_A_QUOTA`（GET 400／413、POST 413） | 「… not a rate limit, a quota or a data-date guarantee.」→「… or a data coverage window.」：移除 guarantee 用詞，並與資料涵蓋窗口區分 |
| POST request body example summary | 「use a date inside the latest 31 available dates」→「use a date inside the 31-day spatial window」 |
| 常數改名 | `SPATIAL_WINDOW_GUARANTEE_DAYS`／`POINT_WINDOW_GUARANTEE_DAYS` → `SPATIAL_WINDOW_DAYS`／`POINT_WINDOW_DAYS`（值不變：31／366；仍不讀 `cfg`）；新增 `SPATIAL_WINDOW_SPAN` 供 GET／POST 共用 |
| 不變 | freshness 句與「Omit date parameters to request the latest available data.」；`spatial_window_days` 說明；handler、路由、request validation、response schema 結構、HTTP 行為；`info.version` `0.4.1`；`change_log.md` |
| OAS 前後比對 | 結構（key 集合）不變；只有 7 個字串值改變：GET description、GET 400／413 description、POST description、POST 413 description、POST body example summary、`available_spatial_window` description |

依 Codex review 再修正（只改文件與測試；生成的 OAS、snapshot 不變）：§1 窗口摘要改為 round 12 用詞（不再寫「至少」）；§4 `available_spatial_window` YAML 註解只寫欄位格式與代表的資料範圍；`openapi_contract.py` 與契約測試中指稱窗口用詞的註解由 round 11 更正為 round 12；`test_no_absolute_or_internal_window_wording` 限縮到日期窗口相關描述（GET Point Query／Bounding-Box Query／Data freshness 段、POST Date 段、`SpatialWindowError` 欄位說明、POST body example summary、GET／POST 400 中提到 window 的句子），不再全域禁止一般詞彙。第二次修正（Codex review）：`test_gap_wording_never_contradicts_the_consecutive_windows` 也改用同一範圍，只在日期窗口相關描述中禁止 skipped／gap(s)／missing，不再掃描整份 OAS 的所有 description；移除不再使用的 `_descriptions()` helper。

### 11.9 Stage E 後續修正（2026-11-20；只改一句 OpenAPI 說明文字、snapshot 與一個測試）

Codex 已完成 Stage E（03「Stage E」）；本節只處理 E6 的用詞差異。**Codex 已確認**（2026-11-20 最終確認 review，150 tests passed）。

| 項目 | 實作（`api/openapi_contract.py`） |
| --- | --- |
| GET Point Query Dates | 「Dates: without `start` and `end`, the newest point date is used. …」→「Dates: without `start` and `end`, the latest date available to point queries is used. …」；與同段 Coverage 的終點用詞相同。句子其餘部分不變 |
| 不變 | `FRESHNESS` 句（owner 指定，「The newest product date typically lags 1–2 days …」）；BBox 無日期的「the newest date in the daily data」（指 daily 資料）；handler、路由、request validation、response schema、HTTP 行為；`info.version` `0.4.1`；`change_log.md` |
| OAS 前後比對 | key 集合相同；只有 GET `description` 一個字串值改變 |
| 測試 | `test_bbox_dates_are_described_exactly` 改用新句子，另斷言 Point Query 段不含「newest point date」，且「the latest date available to point queries」在該段恰好出現 2 次；測試數仍 79 |
| E4／E5 的處理 | 不改 OAS：proxy 專屬 header 不寫入 OAS（§7 仍記錄現況），proxy 正確性由 Ops-1 處理；部署後 production Swagger smoke 屬 Stage G（03） |

### 11.10 Gate F2 核准後的升版（2026-11-24；只改版本字串、snapshot、版本測試與文件）

Owner 於 2026-11-24 核准 F2。此核准**不包含** commit、push 或部署授權。

| 項目 | 實作 |
| --- | --- |
| `info.version` | `dev2026/api/app.py` 的 `version="0.4.1"` → `version="1.0.0"`（只改這一行；路由與執行邏輯不變） |
| OAS 前後比對 | key 集合相同；只有 `/info/version` 一個值改變（`0.4.1` → `1.0.0`）。Snapshot 以 `GHRSST_UPDATE_OPENAPI_SNAPSHOT=1` 更新 |
| 測試 | `test_version_not_bumped_before_f2` 改為 `test_version_is_the_approved_contract`（斷言 `1.0.0` 與 `openapi: 3.1.0`）；`test_servers_operation_ids_and_security` 的版本斷言改為 `1.0.0`；測試數仍 79 |
| OD-3 | `specs/ghrsst_api_v0_1_0.spec` 頂端加 SUPERSEDED 標示並指向本規格；`AGENTS.md` 不再稱舊 Draft 為 authoritative，改指本規格與 generated OpenAPI；舊檔未刪除 |
| `change_log.md` | 新增獨立的「OpenAPI contract v1.0.0」紀錄，標明「已核准、尚未部署」；不含部署日期、部署 commit 或 production smoke；服務的 `ver 0.x` 不變 |
| 未做（該輪當時） | commit、push、部署、production 請求。其後：Codex 最終 review 完成，owner 授權建立實作 commit `b9bd4e46097289632874d1615c5737c37ebc0156`（2026-11-24）；部署仍屬 Stage G |
