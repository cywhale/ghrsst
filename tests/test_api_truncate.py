import importlib
import json
import sys
from pathlib import Path

import numpy as np
import pytest
import xarray as xr
from fastapi.testclient import TestClient


@pytest.fixture
def api_client(tmp_path, monkeypatch):
    """Spin up a TestClient backed by a temporary Zarr store."""
    zarr_root = tmp_path / "mur.zarr"

    # create minimal 1x1 grid with deliberate extra precision
    lon = np.array([120.1234567], dtype=np.float32)
    lat = np.array([23.7654321], dtype=np.float32)
    ds = xr.Dataset(
        data_vars={
            "sst": (("lat", "lon"), np.array([[1.2345678]], dtype=np.float32)),
        },
        coords={"lon": lon, "lat": lat},
    )
    ds.to_zarr(zarr_root, group="/2024/05/01", mode="w", consolidated=False, zarr_format=3)

    latest = tmp_path / "latest.json"
    latest.write_text(json.dumps({"earliest": "2024-05-01", "latest": "2024-05-01"}), encoding="utf-8")

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


def test_truncate_mode_rounds_payload(api_client):
    resp = api_client.get(
        "/api/ghrsst",
        params={
            "lon0": 120.1234567,
            "lat0": 23.7654321,
            "mode": "truncate",
        },
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload) == 1
    row = payload[0]
    assert row["lon"] == pytest.approx(120.12346, abs=1e-6)
    assert row["lat"] == pytest.approx(23.76543, abs=1e-6)
    assert row["sst"] == pytest.approx(1.235, abs=1e-6)


def test_mode_none_preserves_full_precision(api_client):
    expected_lon = float(np.float32(120.1234567))
    expected_lat = float(np.float32(23.7654321))
    expected_sst = float(np.float32(1.2345678))

    resp = api_client.get(
        "/api/ghrsst",
        params={
            "lon0": 120.1234567,
            "lat0": 23.7654321,
        },
    )
    assert resp.status_code == 200
    row = resp.json()[0]
    assert row["lon"] == pytest.approx(expected_lon, abs=1e-9)
    assert row["lat"] == pytest.approx(expected_lat, abs=1e-9)
    assert row["sst"] == pytest.approx(expected_sst, abs=1e-9)


def test_invalid_mode_rejected(api_client):
    resp = api_client.get(
        "/api/ghrsst",
        params={
            "lon0": 120.1234567,
            "lat0": 23.7654321,
            "mode": "unknown",
        },
    )
    assert resp.status_code == 400
