# 00 — 基線與來源（Baseline & Provenance）

> 狀態：**Round 10（2026-11-20；本檔內容自 round 4 未變）— 基線已由 owner 決定：`6b32e54`（B1）。** 本機 branch 調整紀錄見 [§7](#7-round-4-本機基線調整紀錄2026-11-20)。
> §1–§6 為 2026-11-19 的調查紀錄（當時 worktree 在 `e302fc4`），保留原文作為來源證據。
> 調查方式：本機 Git 物件唯讀檢查 + VM24 唯讀 SSH 檢查 + 公開 API 唯讀請求。本文件不變更 production。

## 1. 本 worktree 狀態

| 項目 | 值 |
| --- | --- |
| Worktree | `/Users/cywhale/proj/ghrsst-v1` |
| Branch | `ghrsst_v1` |
| HEAD | `e302fc4838d2aa805c4e6c7ce8bbf9147315fe90`（= `main` = `origin/main`） |
| Parent | `29b9ad3ff812be2f020f0de57fd17e85fddbc00a` |
| 開始時 `git status --short` | 乾淨（無輸出） |
| 適用 AGENTS.md | 根目錄 `AGENTS.md`（@e302fc4 版本） |

`e302fc4` 是舊版 production 程式樹：只有 `ghrsst_app.py`（change_log 記為 ver 0.2.1）、`conf/`、`dev/`、`mcp/`、
`specs/`、`tests/`，**沒有 `dev2026/`**，因此沒有 `dev2026/api/app.py`，也沒有 `dev2026/tests/`。
GitHub `main` 從未合併任何 dev2026 工作；dev2026 全部存在於 stacked branches 與 tags。

`AGENTS.md`（@e302fc4）仍寫：`specs/` 的 `ghrsst_api_v0_1_0.spec`「should remain the authoritative interface
reference」。該文件描述的是舊 `ghrsst_app.py`（31 天上限等），與現行 API 不符（見 [01 §13](01_current_contract_inventory.md#13-與舊-draft-specsghrsst_api_v0_1_0spec-的矛盾)）。本輪不修改該檔，列入 [03](03_roadmap_and_validation.md) Stage B/F 的文件一致性修正。

## 2. `dev2026/api/app.py` 存在於哪些 refs

以 `git cat-file -e <ref>:dev2026/api/app.py` 掃描所有 local/remote refs 與 tags（重點節錄）：

| Ref / commit | 日期 | app.py `version=` | 說明 |
| --- | --- | --- | --- |
| `origin/main` = `e302fc4` | 2026-06-21 | — | **無 dev2026**（本 worktree 指定 base） |
| `origin/dev2026-*`（phase2 / p3 / p4 早期） | 2026-06-22..07-01 | 未設定（FastAPI 預設） | 多個 stacked 開發分支 |
| `origin/release/v0.3.1-pre-p4` = `bb5e105` | — | `0.3.1` | pre-P4 release 分支 |
| tag `v0.4.1` → `9a8d3d4` | 2026-07-28 | `0.4.1` | production retention checkpoint |
| tag `v0.5.0`（annotated，物件 `792b15b`）→ `14f26db` | 2026-07-30 | `0.4.1` | time-cube authority + daily retention |
| P5 chain（`dev2026-p5-*`） | 2026-07-31.. | `0.4.1` | P5 segmented base / 資料生命週期 |
| **`6b32e54`**（無 remote ref） | 2026-08-28 | `0.4.1` | **目前 production serving code**（§3） |
| `a833b4b` | 2026-09-17 | `0.4.1` | 文件化「0.6.0 operational milestone」（文件版本，非 API 版本） |
| `d7ba064`（`dev2026-p5-s6-o2-local`） | 2026-11 | `0.4.1` | `/Users/cywhale/proj/ghrsst`；受保護、有 dirty 檔案；本輪只讀 Git 物件，未讀其 working tree |
| `1003b25`（O4，`dev2026-p5-gen3-block-replacement`） | — | `0.4.1` | O4；不得帶入 |

結論：自 `v0.4.1` 起 `info.version` 一直停在 `"0.4.1"`，**版本字串不能用來識別部署內容**；必須以 commit / module hash 判定。

## 3. Production 實際 serving code 的證據（VM24 唯讀檢查）

| 證據 | 觀察值 |
| --- | --- |
| PM2 launcher `/home/odbadmin/python/ghrsst/conf/start_app.sh` | `cd /home/odbadmin/p5deploy/20260830T082136Z/checkout`；`exec /home/odbadmin/python/ghrsst-dev2026-phase2/dev2026/.venv/bin/gunicorn api.app:app -w 2 -k uvicorn.workers.UvicornWorker -b 127.0.0.1:8035 --timeout 180`；`PYTHONPATH=dev2026`；launcher mtime 2026-09-14 09:17 |
| Deploy checkout HEAD | `6b32e54710db5b65fae3e30081d48eae71b26f26`，`git status` 乾淨（0 個變更） |
| 執行中 gunicorn master（`pgrep -f "gunicorn api.app:app"`，取 cwd = 上述 checkout 者） | 啟動於 2026-11-18 20:30:36，`/proc/<pid>/cwd` = deploy checkout（晚於 launcher mtime，故使用現行 launcher） |
| Module hash | VM24 checkout 的 `dev2026/api/app.py` 與 `dev2026/store/*.py` 共 **18 個檔案的 sha256 與本機 `6b32e54` 完全一致**（`app.py` = `15ed3ca4…87d9c7`） |
| 執行環境套件 | VM24 venv：Python 3.13.7、fastapi 0.138.0、pydantic 2.13.4、starlette 1.3.1、zarr 3.2.1；與本機 `/Users/cywhale/proj/ghrsst/dev2026/.venv` 版本相同 |
| Generated OpenAPI | 以本機 `6b32e54` 快照在隔離環境 import app 後產生的 `openapi()`，與 `https://eco.odb.ntu.edu.tw/api/swagger/ghrsst/openapi.json`（2026-11-19 取得）**JSON 語意完全相同** |

限制（照實記錄）：hash 比對只涵蓋 `api/app.py` 與 `store/*.py`；未逐一比對 venv 內所有檔案；
未讀取 PM2 runtime env（`pm2` 在非互動 shell 不在 PATH，`pm2 jlist` 失敗），launcher 中的 env 被視為實際 env。
`GHRSST_ENABLE_COVERAGEJSON` 在 launcher 中**未設定**，因此採 code default `0`（`/healthz` 顯示 `coveragejson_enabled: false`，一致）。

注意：`AGENTS.md`（@6b32e54）寫「active API runtime is `/home/odbadmin/python/ghrsst-dev2026-phase2`」——現已**過時**：
目前該目錄只提供 venv；程式碼來自 `p5deploy/.../checkout`。`ghrsst-dev2026-phase2` 本身 HEAD `a9db4b7`
（`dev2026-phase2-tiled-append`）且有 9 個 dirty 檔，**不是** serving code。`/home/odbadmin/python/ghrsst` 在 `e302fc4`
（舊 main），也不是 serving code。

## 4. 落差與風險

1. **`6b32e54` 目前在 GitHub 上查不到**（round 2 補充遠端查核，2026-11-19 08:59 UTC）：
   - `gh api repos/cywhale/ghrsst/commits/6b32e547…` → HTTP 422 `No commit found for SHA`；
   - `git ls-remote origin 'refs/heads/*' 'refs/tags/*'` 共 42 個 refs，其 SHA 全部存在於本機，且**沒有一個**以 `6b32e54` 為祖先；
   - 本機 remote-tracking refs（`git branch -r --contains`）同樣為空——此項單獨只反映最後一次 fetch，故以上兩項為主要證據。
   查核範圍限 branch／tag refs 與 commits API；未檢查 PR refs 以外的其他 GitHub 物件。
   它存在於本機 `dev2026-p5-s6-o2-local` 與 O4 branch 的歷史中，以及 VM24 deploy bundle `p5deploy_6b32e54.bundle`。
   最近的 origin 祖先是 `origin/dev2026-p4s10-execution-findings`（`24d0413`，落後 96 commits）。
   → 目前 production 程式**無法只從 GitHub 重現**；但本機工作只需本機完整 commit 與保存的來源證據即可進行（§5）。
2. **6b32e54 之後已有影響 API 行為、但未部署的 commit**：`ee84037`（2026-11-05，O2-local lineage，亦不在 origin）
   修改 `dev2026/api/app.py`：
   - `GHRSST_FIXED_CACHE_SECONDS` 預設由 `864000`（10 天）改為 `259200`（3 天）——**對外可見的 `Cache-Control` 變更**；
   - 新增 maintenance gate / in-flight counter（correction 期間資料讀取被暫停，可能回應 503）。
   這些行為**不屬於目前 API**，本系列文件不把它們寫成現況（見 OD-2）。
3. **P5 資料流程 session 仍在修改同一個 `app.py`**（`ee84037` 即為一例）。API v1 工作若平行修改同檔，未來必有衝突，需要 ownership 規則（OD-15）。
4. `6b32e54` 帶有完整 P5 store/ops 程式（segmented cube、repair WAL、ops/ 等），API 工作以它為基線時會連帶這些檔案，但 v1 工作不應修改它們。

### O4 / gen3 分離性
- `d7ba064..1003b25` 只有 3 個 commit（`93625b3`、`c7dd5c5`、`1003b25`），`git diff --stat d7ba064 1003b25` 只觸及
  4 個檔案（`dev2026/specs/p5_source_prune_runbook.md`、`dev2026/tests/test_phase2_p5_gen3_block_replacement.py` 等），
  **沒有** `dev2026/api` 或 `dev2026/store` 變更。
- `6b32e54` 是 `d7ba064` 與 `1003b25` 的祖先，不含任何 O4 commit。→ 以 `6b32e54` 為基線可與 O4 **明確分離**。

## 5. 基線選項（2026-11-20 owner 已決定 B1；以下為決定前的比較）

| 選項 | 內容 | 優點 | 風險 / 缺點 |
| --- | --- | --- | --- |
| **B1（建議）** | 以本機完整 commit `6b32e54`（as-served）為 v1 盤點與實作基線；由 owner 決定 `ghrsst_v1` 是否重新指向它或另開本機 branch。推上 origin 屬後續發布準備（Stage G），不是本機工作的前置條件 | 與 production 逐檔相同；generated OpenAPI 與 production 相同；不含 O4；71 個 API 相關測試在其快照上通過（見 01 §15） | 需 owner 決定本機 branch 安排；帶入 P5 store/ops 檔案（v1 不改它們）；與 `ee84037` 的先後關係需另決（OD-2）；發布前需確保遠端可取得 |
| B2 | 以 tag `v0.5.0`（`14f26db`，已在 origin）為基線 | 已發佈、可從 GitHub 取得 | `app.py` hash 與 production 不同（缺 P5 segmented base wiring / QuerySnapshot）；在其上實作的 v1 部署時會回退 P5 serving；不建議 |
| B3 | 以 `d7ba064`（O2-local tip，含 `ee84037`）為基線 | 包含 P5 最新程式 | 含未部署的行為變更（cache 3 天、maintenance 503）；該 worktree 受保護且 dirty；屬 P5 session 所有；契約會描述尚未上線的行為 |
| B4 | 保留 `e302fc4`，由 owner 以 merge 方式把 dev2026 帶入 `ghrsst_v1` | `ghrsst_v1` 與 GitHub `main` 保持同源 | 需大量歷史合併（`6b32e54` 距 `main` 253 commits）；本 session 不得自行 merge/cherry-pick；結果仍須以 B1 的 hash 驗證 |

建議：**B1**。理由：v1 契約必須描述「實際在服務的 API」，而唯一被 hash 與 OpenAPI 雙重證明等同 production 的 commit 是 `6b32e54`。
`ee84037` 應視為「待併入的上游行為變更」，在 Stage C 之前由 owner 決定它是否先於 v1 上線（OD-2）。

（決定前的註記）在基線決定前，本系列文件的所有盤點均標記「investigated at `6b32e54`」，不宣稱規格或實作基線已定案。

## 6. 重現指令（皆為唯讀）

```bash
cd /Users/cywhale/proj/ghrsst-v1
git rev-parse HEAD HEAD~1 && git status --short
git branch -a --contains 6b32e54          # 只應列出 O2-local 與 O4 兩個 local branch
git branch -r --contains 6b32e54          # 應為空（只反映最後一次 fetch）
gh api repos/cywhale/ghrsst/commits/6b32e54710db5b65fae3e30081d48eae71b26f26   # 2026-11-19：HTTP 422 No commit found
git ls-remote origin 'refs/heads/*' 'refs/tags/*'   # 逐一以 git merge-base --is-ancestor 6b32e54 <sha> 檢查
git diff --stat d7ba064 1003b25           # O4 只有 4 個檔案，無 api/store
git show 6b32e54:dev2026/api/app.py | shasum -a 256   # 15ed3ca4…87d9c7
git diff 6b32e54 d7ba064 -- dev2026/api/app.py         # ee84037 的變更
```

```bash
# VM24（唯讀）
ssh vm24 'grep -nE "^(cd|export|exec)" /home/odbadmin/python/ghrsst/conf/start_app.sh'
ssh vm24 'git -C /home/odbadmin/p5deploy/20260830T082136Z/checkout rev-parse HEAD; git -C /home/odbadmin/p5deploy/20260830T082136Z/checkout status --short | wc -l'
ssh vm24 'cd /home/odbadmin/p5deploy/20260830T082136Z/checkout/dev2026 && sha256sum api/app.py store/*.py'
```

```bash
# 公開 OpenAPI（唯讀）
curl -sS https://eco.odb.ntu.edu.tw/api/swagger/ghrsst/openapi.json
```

## 7. Round 4 本機基線調整紀錄（2026-11-20）

Owner 決定：v1 的盤點與實作基線 = production serving code `6b32e54710db5b65fae3e30081d48eae71b26f26`；
Web API v1 使用獨立 branch／worktree；暫不 push。

| 步驟 | 內容 | 結果 |
| --- | --- | --- |
| 1. 保存文件 | 六份 round-3 文件（皆 untracked）複製到 session scratchpad，並以 `git hash-object -w` 寫入本機物件庫；記錄 sha256 | 6 個 sha256 皆核對 OK |
| 2. 安全性檢查 | `ghrsst_v1` 只有 1 筆 reflog（由 `e302fc4` 建立），無 upstream，無自己的 commit；`e302fc4` 是 `6b32e54` 的祖先（`git rev-list --left-right --count e302fc4...6b32e54` = `0 253`）；`6b32e54` 沒有追蹤 `dev2026/specs/ghrsst_v1/` 下的任何檔案；`.gitignore` 不會忽略這些文件；worktree 無 ignored 檔 | 可安全 fast-forward |
| 3. 調整 | 在 `/Users/cywhale/proj/ghrsst-v1` 執行 `git merge --ff-only 6b32e54710db5b65fae3e30081d48eae71b26f26`。**不是** reset／force-update，沒有 merge commit，沒有改寫任何既有 commit；`e302fc4` 仍是祖先 | reflog：`ghrsst_v1@{0}: merge 6b32e547…: Fast-forward`、`ghrsst_v1@{1}: branch: Created from e302fc4…` |
| 4. 驗證 | HEAD = `6b32e54`；六份文件 sha256 與步驟 1 相同；`dev2026/api/app.py` sha256 = `15ed3ca4…87d9c7`（= VM24 serving，§3）；`d7ba064`、`1003b25`、`93625b3` 皆**不是** `6b32e54` 的祖先（不含 O2-local 後續或 O4） | 全部符合 |
| 5. 其他 worktree | `git worktree list`：`/Users/cywhale/proj/ghrsst` 仍在 `d7ba064`、`/Users/cywhale/proj/ghrsst-gen3-replace` 仍在 `1003b25`；本輪未讀寫兩者的 working tree | 未變動 |

測試環境：`dev2026/.venv`（gitignored，位於本 worktree）以 uv 建立，套件版本與 P5 worktree venv 的 `uv pip freeze`
一致（Python 3.13.11、fastapi 0.138.0、pydantic 2.13.4、starlette 1.3.1、zarr 3.2.1），另加 dev-only `jsonschema 4.26.0`。
P5 worktree 的 venv 只被唯讀查詢版本，未安裝或修改任何套件。

O4 code approval 綁定的精確 commit 為 `1003b256d36f47a18fd2a76dec9fcbb48b7024c7`（40 字元）。任何 API v1 commit 都不在其歷史中，也不改變它；若日後 P5 使用整合版本，需另建 code approval。
