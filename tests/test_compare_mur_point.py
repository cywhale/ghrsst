# test_compare_mur_point.py
import argparse, pathlib, numpy as np, xarray as xr, random

def pick_coord_names(ds, lon_candidates=("lon","longitude","x"), lat_candidates=("lat","latitude","y")):
    lon_name = next((c for c in lon_candidates if c in ds.coords or c in ds.dims), None)
    lat_name = next((c for c in lat_candidates if c in ds.coords or c in ds.dims), None)
    if not lon_name or not lat_name:
        raise RuntimeError(f"Cannot find lon/lat coords in {list(ds.coords)} / dims {list(ds.dims)}")
    return lon_name, lat_name

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nc", required=True)
    ap.add_argument("--zarr", required=True)
    ap.add_argument("--date", required=True)   # YYYY-MM-DD
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--verbose", action="store_true", help="Print all coordinate comparisons")
    args = ap.parse_args()

    # NetCDF open (avoid timedelta decoding noise)
    ds_nc = xr.open_dataset(args.nc, engine="netcdf4", decode_timedelta=False)
    lon_nc, lat_nc = pick_coord_names(ds_nc)

    # Zarr v3 open (no consolidation by default)
    grp = f"/{args.date.replace('-', '/')}"
    ds_za = xr.open_zarr(args.zarr, group=grp, zarr_format=3, consolidated=None)
    lon_za, lat_za = pick_coord_names(ds_za)

    # Map variables
    var_map_nc = {
        "sst": "analysed_sst" if "analysed_sst" in ds_nc else "sst",
        "sst_anomaly": "sst_anomaly",
        "sea_ice": "sea_ice_fraction" if "sea_ice_fraction" in ds_nc else "sea_ice",
    }
    vars_to_check = [k for k in ("sst","sst_anomaly","sea_ice") if (var_map_nc[k] in ds_nc) and (k in ds_za)]

    # Variable-specific tolerances for float32 precision
    # sst_anomaly can have slightly larger differences due to computation from climatology
    tolerances = {
        "sst": 1e-5,           # Standard float32 tolerance
        "sst_anomaly": 1e-5,   # If needed, use a relaxed tolerance for computed anomalies
        "sea_ice": 1e-5,       # Standard float32 tolerance
    }

    # Get exact coordinate arrays
    lon_arr_nc = ds_nc[lon_nc].values
    lat_arr_nc = ds_nc[lat_nc].values
    lon_arr_za = ds_za[lon_za].values
    lat_arr_za = ds_za[lat_za].values
    
    # Check if coordinate arrays match exactly
    print(f"Coordinate array comparison:")
    print(f"  NC  lon shape: {lon_arr_nc.shape}, dtype: {lon_arr_nc.dtype}")
    print(f"  Zarr lon shape: {lon_arr_za.shape}, dtype: {lon_arr_za.dtype}")
    print(f"  NC  lat shape: {lat_arr_nc.shape}, dtype: {lat_arr_nc.dtype}")
    print(f"  Zarr lat shape: {lat_arr_za.shape}, dtype: {lat_arr_za.dtype}")
    lon_match = np.allclose(lon_arr_nc, lon_arr_za, rtol=1e-10, atol=1e-10)
    lat_match = np.allclose(lat_arr_nc, lat_arr_za, rtol=1e-10, atol=1e-10)
    print(f"  Lon arrays match: {lon_match}")
    print(f"  Lat arrays match: {lat_match}")
    if not lon_match:
        max_lon_diff = np.max(np.abs(lon_arr_nc - lon_arr_za))
        print(f"  Max lon diff: {max_lon_diff:.10e}")
    if not lat_match:
        max_lat_diff = np.max(np.abs(lat_arr_nc - lat_arr_za))
        print(f"  Max lat diff: {max_lat_diff:.10e}")
    print()
    
    # Pick random INDICES
    rng = random.Random(42)
    nlat, nlon = len(lat_arr_nc), len(lon_arr_nc)
    test_indices = [(rng.randint(0, nlat-1), rng.randint(0, nlon-1)) for _ in range(args.n)]

    diffs = {k: [] for k in vars_to_check}
    mismatch_details = {k: [] for k in vars_to_check}
    coord_mismatches = 0
    
    for test_num, (lat_idx, lon_idx) in enumerate(test_indices):
        # Get exact coordinate values from NetCDF
        lo_nc = float(lon_arr_nc[lon_idx])
        la_nc = float(lat_arr_nc[lat_idx])
        
        # NC side - use exact indexing
        vals_nc = {}
        for k in vars_to_check:
            src = var_map_nc[k]
            v = ds_nc[src].isel({lat_nc: lat_idx, lon_nc: lon_idx}).values.item()
            if k in ("sst"):
                u = str(ds_nc[src].attrs.get("units", "")).lower()
                if u in ("k","kelvin"):
                    v = v - 273.15
            vals_nc[k] = float(v)

        # Zarr side - find which point we actually select
        vals_za = {}
        selected_coords_za = {}
        for k in vars_to_check:
            da = ds_za[k].sel({lon_za: lo_nc, lat_za: la_nc}, method="nearest")
            vals_za[k] = float(da.compute().values.item())
            # Get the actual selected coordinates
            selected_coords_za[k] = {
                'lon': float(da[lon_za].values),
                'lat': float(da[lat_za].values)
            }

        # Check if we're actually comparing the same point
        first_var = vars_to_check[0]
        lo_za = selected_coords_za[first_var]['lon']
        la_za = selected_coords_za[first_var]['lat']
        
        coord_match = (abs(lo_nc - lo_za) < 1e-9) and (abs(la_nc - la_za) < 1e-9)
        if not coord_match:
            coord_mismatches += 1
        
        if args.verbose or not coord_match:
            print(f"Test {test_num}: idx=({lat_idx},{lon_idx})")
            print(f"  NC  coords: lon={lo_nc:.10f}, lat={la_nc:.10f}")
            print(f"  Zarr coords: lon={lo_za:.10f}, lat={la_za:.10f}")
            print(f"  Coord match: {coord_match}")
            if not coord_match:
                print(f"  Δlon={abs(lo_nc-lo_za):.10e}, Δlat={abs(la_nc-la_za):.10e}")

        for k in vars_to_check:
            if np.isfinite(vals_nc[k]) and np.isfinite(vals_za[k]):
                diff = abs(vals_nc[k] - vals_za[k])
                diffs[k].append(diff)
                tol = tolerances.get(k, 1e-5)
                if diff > tol or (args.verbose and coord_match):
                    detail = {
                        'test_num': test_num,
                        'idx': (lat_idx, lon_idx),
                        'lon_nc': lo_nc, 'lat_nc': la_nc,
                        'lon_za': lo_za, 'lat_za': la_za,
                        'coord_match': coord_match,
                        'nc': vals_nc[k], 'zarr': vals_za[k],
                        'diff': diff,
                        'tolerance': tol
                    }
                    if diff > tol:
                        mismatch_details[k].append(detail)
                    if args.verbose or diff > tol:
                        print(f"  {k}: nc={vals_nc[k]:.10f}, zarr={vals_za[k]:.10f}, diff={diff:.10e}, tol={tol:.0e}")
            
        if args.verbose or not coord_match:
            print()

    # Print summary
    print("\n" + "="*80)
    print("SUMMARY")
    print("="*80)
    if coord_mismatches > 0:
        print(f"WARNING: {coord_mismatches}/{args.n} tests had coordinate mismatches!")
        print("This suggests the Zarr coordinate arrays don't match the NetCDF exactly.\n")
    
    any_fail = False
    for k, arr in diffs.items():
        if arr:
            m = float(np.max(arr))
            tol = tolerances.get(k, 1e-5)
            mismatch_count = sum(1 for d in arr if d > tol)
            print(f"\n{k}:")
            print(f"  Tolerance: {tol:.0e}")
            print(f"  Max abs diff: {m:.3e} over {len(arr)} comparable points")
            print(f"  Mismatches (>{tol:.0e}): {mismatch_count}")
            
            if mismatch_count > 0:
                any_fail = True
                print(f"  Top {min(5, len(mismatch_details[k]))} mismatches:")
                for detail in sorted(mismatch_details[k], key=lambda x: x['diff'], reverse=True)[:5]:
                    print(f"    Test {detail['test_num']}: idx={detail['idx']}, diff={detail['diff']:.3e}")
                    print(f"      Coord match: {detail['coord_match']}")
                    print(f"      NC={detail['nc']:.6f}, Zarr={detail['zarr']:.6f}")
                if len(mismatch_details[k]) > 5:
                    print(f"    ... and {len(mismatch_details[k])-5} more")
            else:
                print(f"  ✓ All values within tolerance")
        else:
            print(f"\n{k}: (no comparable finite pairs)")
    
    if any_fail:
        raise SystemExit("\n✗ FAIL: Some values exceed their tolerance thresholds")
    else:
        print("\n✓ PASS: All Zarr vs NetCDF values match within acceptable tolerances")

if __name__ == "__main__":
    main()
