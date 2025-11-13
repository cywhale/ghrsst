import importlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from fastapi.testclient import TestClient


@pytest.fixture
def bbox_client(tmp_path, monkeypatch):
    zarr_root = tmp_path / "mur.zarr"
    lon = np.array([135.0, 136.0], dtype=np.float32)
    lat = np.array([15.0, 16.0], dtype=np.float32)

    days = ["2025-10-30", "2025-11-03"]
    for idx, day in enumerate(days):
        vals = np.array([[20.0 + idx, 21.0 + idx], [22.0 + idx, 23.0 + idx]], dtype=np.float32)
        ds = xr.Dataset(
            data_vars={"sst": (("lat", "lon"), vals)},
            coords={"lon": lon, "lat": lat},
        )
        ds.to_zarr(
            zarr_root,
            group=f"/{day.replace('-', '/')}",
            mode="w" if idx == 0 else "a",
            consolidated=False,
            zarr_format=3,
        )

    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps({"earliest": days[0], "latest": days[-1]}), encoding="utf-8")

    monkeypatch.setenv("GHRSST_ZARR_PATH", str(zarr_root))
    monkeypatch.setenv("GHRSST_INDEX_JSON", str(latest))

    repo_root = Path(__file__).resolve().parent.parent
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    module = importlib.reload(importlib.import_module("ghrsst_app"))
    module.rt.earliest = None
    module.rt.latest = None
    module.rt.lon = None
    module.rt.lat = None

    return TestClient(module.app)


def test_bbox_single_day_selected_from_range(bbox_client):
    resp = bbox_client.get(
        "/api/ghrsst",
        params={
            "lon0": 135,
            "lat0": 15,
            "lon1": 136,
            "lat1": 16,
            "start": "2025-10-30",
            "end": "2025-11-05",
            "append": "sst",
        },
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 4
    assert all(row["date"] == "2025-10-30" for row in rows)
    coords = {(row["lon"], row["lat"]) for row in rows}
    assert coords == {(135.0, 15.0), (136.0, 15.0), (135.0, 16.0), (136.0, 16.0)}


def test_bbox_sample_stride(bbox_client):
    resp = bbox_client.get(
        "/api/ghrsst",
        params={
            "lon0": 135,
            "lat0": 15,
            "lon1": 136,
            "lat1": 16,
            "start": "2025-10-30",
            "sample": 2,
            "append": "sst",
        },
    )
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    row = rows[0]
    assert row["lon"] == pytest.approx(135.0)
    assert row["lat"] == pytest.approx(15.0)
    assert row["date"] == "2025-10-30"


def test_bbox_range_without_overlap_errors(bbox_client):
    resp = bbox_client.get(
        "/api/ghrsst",
        params={
            "lon0": 135,
            "lat0": 15,
            "lon1": 136,
            "lat1": 16,
            "start": "2025-10-21",
            "end": "2025-10-31",
        },
    )
    assert resp.status_code == 400
    detail = resp.json().get("detail")
    assert "single-day" in detail.lower()
    assert "2025-10-21" in detail
