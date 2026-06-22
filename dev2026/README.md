# dev2026 — GHRSST API 重構工作區

所有 2026 重構開發隔離於此資料夾,**不影響 production 檔案**(`ghrsst_app.py`、`dev/`、`conf/`)。

## 給 reviewer 的閱讀順序
1. [`specs/00_diagnosis_and_evidence.md`](specs/00_diagnosis_and_evidence.md) — 專案現況、問題、**實測證據**、根因。
2. [`specs/01_refactor_spec.md`](specs/01_refactor_spec.md) — 目標、非目標、分階段 steps、驗收標準、開放問題。

## 重現 benchmark(請自跑驗證數字)
```bash
uv venv dev2026/.venv --python 3.13
uv pip install --python dev2026/.venv/bin/python "zarr>=3" "xarray>=2025.1" numpy orjson
export GHRSST_ZARR_PATH=$(pwd)/bak/data/mur.zarr     # 或 production 路徑;切勿硬編碼
dev2026/.venv/bin/python dev2026/bench/bench_pointseries.py --days 150
dev2026/.venv/bin/python dev2026/bench/bench_concurrency.py --n 16
dev2026/.venv/bin/python dev2026/bench/bench_bbox.py
```

## 慣例
- `uv` 管理 Python(3.13)與 venv。
- store 路徑一律 `GHRSST_ZARR_PATH`,程式不硬編碼;`bak/` 僅本機部分複本,**仍在持續拷貝/每日增長**,工具須容忍缺日。

## 現況
- [x] 診斷 + benchmark harness(`bench/`、`store/zarr_paths.py`)
- [x] spec(`specs/00`、`specs/01`)— **reviewer accepted (v9)**
- [x] **P1-S1** `store/store_access.py`(座標快取 + thread-safe LRU + 逐 chunk 釋放;`tests/test_store_access.py` 13 tests pass:real-store parity / thread-safety stress / new-day visibility / memory-bound;point_series 12.4×、points_batch 818×)
- [ ] P1-S2 新 FastAPI(GET 相容 + `POST /points` + streaming bbox + 503 背壓)
- [ ] P1-S3 parity test / P1-S4 load+RSS scenarios / P1-S5 部署 / P1-S6 cutover
