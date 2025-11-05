# mur2zarr_v3.py
import sys, pathlib, numpy as np, xarray as xr, zarr
from zarr.storage import LocalStore
import zarr.codecs as ZC

def _infer_group(nc: pathlib.Path) -> str:
    for tok in nc.name.split("-"):
        if tok.isdigit() and len(tok) == 14:  # 20251101090000
            return f"/{tok[0:4]}/{tok[4:6]}/{tok[6:8]}"
    with xr.open_dataset(nc, engine="netcdf4", decode_timedelta=False) as d:
        t = np.datetime_as_string(d["time"].values[0], unit="D")
        y, m, d0 = t.split("-")
        return f"/{y}/{m}/{d0}"

def main(nc_path, zarr_root):
    nc = pathlib.Path(nc_path)
    group = _infer_group(nc)

    # Open without chunks; avoid timedelta auto-decoding warnings
    ds0 = xr.open_dataset(nc, engine="netcdf4", decode_timedelta=False)

    # Keep only needed vars & rename
    rename = {}
    if "analysed_sst" in ds0:     rename["analysed_sst"] = "sst"
    if "sea_ice_fraction" in ds0: rename["sea_ice_fraction"] = "sea_ice"
    ds0 = ds0.rename(rename)
    keep = [v for v in ("sst", "sst_anomaly", "sea_ice") if v in ds0]
    ds = ds0[keep]

    # Units guard - ONLY for absolute temperature (sst), NOT for anomalies
    # sst_anomaly is already a difference in Celsius, never needs conversion
    if "sst" in ds:
        u = str(ds["sst"].attrs.get("units", "")).lower()
        if u in ("k", "kelvin"):
            ds["sst"] = ds["sst"] - 273.15
            ds["sst"].attrs["units"] = "degree_Celsius"
    
    # Ensure sst_anomaly units are correct (should already be °C)
    if "sst_anomaly" in ds:
        anom_units = str(ds["sst_anomaly"].attrs.get("units", "")).lower()
        if anom_units in ("k", "kelvin"):
            # This should never happen - anomalies should be in Celsius
            # But if it does, just fix the units attribute, don't subtract 273.15!
            print(f"WARNING: sst_anomaly has units={anom_units}, fixing to degree_Celsius")
            ds["sst_anomaly"].attrs["units"] = "degree_Celsius"
        elif not anom_units or anom_units == "":
            ds["sst_anomaly"].attrs["units"] = "degree_Celsius"

    # Rechunk after selection
    ds = ds.chunk({"lat": 1024, "lon": 1024})

    # Write Zarr v3
    store = LocalStore(zarr_root)
    enc = {
        v: {
            "compressors": [ZC.BloscCodec(cname="zstd", clevel=5, shuffle=ZC.BloscShuffle.bitshuffle)],
            "dtype": "float32",
            "chunks": (1024, 1024),
            "_FillValue": np.float32(np.nan),
        } for v in ds.data_vars
    }
    ds.to_zarr(store, group=group, mode="w", encoding=enc, zarr_format=3)

    # Minimal attrs to keep readers happy
    root = zarr.open_group(store=store, mode="a", zarr_format=3)
    g = root[group.lstrip("/")]
    for v in ds.data_vars:
        a = g[v]
        if a.ndim == 2:
            a.attrs["dimension_names"] = ["lat", "lon"]
            a.attrs["_ARRAY_DIMENSIONS"] = ["lat", "lon"]
    g.attrs.update({
        "date": group.strip("/").replace("/", "-"),
        "source": "GHRSST L4 MUR v4.1",
        "variables": list(ds.data_vars),
        "coords": ["lat", "lon"],
    })

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
