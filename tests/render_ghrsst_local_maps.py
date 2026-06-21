#!/usr/bin/env python3
"""Render GHRSST SST maps on a projected Web Mercator grid."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import xarray as xr
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = Path('/home/odbadmin/Data/ghrsst')
ZARR_ROOT = DATA_ROOT / 'mur.zarr'
WMS_SETTINGS = ROOT / 'conf' / 'wms_settings.json'
TARGET = {'lat': 22.28274, 'lon': 119.30672}
DATES = ['2026-06-08', '2026-06-09']
ZOOM = 8
TILE_SIZE = 256
OUT_SCALE = 4
R = 6378137.0
MAX_MERC = math.pi * R
EAST_TILES = 2
WEST_TILES = 1
NORTH_TILES = 1
SOUTH_TILES = 1


def _load_wms_bins():
    obj = json.loads(WMS_SETTINGS.read_text(encoding='utf-8'))
    entries = obj['maps'][0]['entries']
    values = entries['values']
    colors = entries['colors']
    edges = np.array([float(hi) for _, hi in values[:-1]], dtype=float)
    rgb = np.array([tuple(int(c[i:i+2], 16) for i in (0, 2, 4)) for c in colors], dtype=np.uint8)
    if len(rgb) != len(edges) + 1:
        raise ValueError(f'Expected 215 colors and 214 edges, got {len(rgb)} colors / {len(edges)} edges')
    return edges, rgb


def _coord_name(ds: xr.Dataset, candidates):
    for name in candidates:
        if name in ds.coords or name in ds.dims:
            return name
    raise KeyError(f'Cannot find coordinate from {candidates} in {list(ds.coords)} / {list(ds.dims)}')


def _open_day(day: str):
    group = '/' + day.replace('-', '/')
    ds = xr.open_zarr(ZARR_ROOT, group=group, zarr_format=3, consolidated=None)
    lon_name = _coord_name(ds, ('lon', 'longitude', 'x'))
    lat_name = _coord_name(ds, ('lat', 'latitude', 'y'))
    sst = ds['sst'].astype('float32')
    if 'time' in sst.dims:
        sst = sst.isel(time=0, drop=True)
    lon_vals = np.asarray(ds[lon_name].values, dtype=float)
    lat_vals = np.asarray(ds[lat_name].values, dtype=float)
    return ds, sst, lon_name, lat_name, lon_vals, lat_vals


def _mercator_tile_bounds(lon: float, lat: float, zoom: int):
    lat = float(np.clip(lat, -85.05112878, 85.05112878))
    n = 2 ** zoom
    xtile = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    ytile = int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    xtile = max(0, min(n - 1, xtile))
    ytile = max(0, min(n - 1, ytile))
    tile_m = 2.0 * math.pi * R / n
    x_min = -MAX_MERC + xtile * tile_m
    x_max = x_min + tile_m
    y_max = MAX_MERC - ytile * tile_m
    y_min = y_max - tile_m
    return xtile, ytile, x_min, y_min, x_max, y_max


def _normalize_longitudes(lons: np.ndarray, targets: np.ndarray):
    lon_min = float(np.nanmin(lons))
    lon_max = float(np.nanmax(lons))
    if lon_min >= 0.0 and lon_max > 180.0:
        return np.mod(targets, 360.0)
    return targets


def _prepare_source_grid(sst: xr.DataArray, lon_vals: np.ndarray, lat_vals: np.ndarray, lon_name: str, lat_name: str):
    if lat_vals[0] > lat_vals[-1]:
        sst = sst.sortby(lat_name)
        lat_vals = lat_vals[::-1]
    if lon_vals[0] > lon_vals[-1]:
        sst = sst.sortby(lon_name)
        lon_vals = lon_vals[::-1]
    return np.asarray(sst.values, dtype=float), lon_vals, lat_vals


def _bilinear_sample(values: np.ndarray, src_lon: np.ndarray, src_lat: np.ndarray, tgt_lon: np.ndarray, tgt_lat: np.ndarray):
    # Assumes src_lon/src_lat are ascending regular grids.
    lon = np.clip(tgt_lon, src_lon[0], src_lon[-1])
    lat = np.clip(tgt_lat, src_lat[0], src_lat[-1])

    x1 = np.searchsorted(src_lon, lon, side='right')
    y1 = np.searchsorted(src_lat, lat, side='right')
    x1 = np.clip(x1, 1, len(src_lon) - 1)
    y1 = np.clip(y1, 1, len(src_lat) - 1)
    x0 = x1 - 1
    y0 = y1 - 1

    x0v = src_lon[x0]
    x1v = src_lon[x1]
    y0v = src_lat[y0]
    y1v = src_lat[y1]

    dx = np.divide(lon - x0v, x1v - x0v, out=np.zeros_like(lon), where=(x1v != x0v))
    dy = np.divide(lat - y0v, y1v - y0v, out=np.zeros_like(lat), where=(y1v != y0v))

    v00 = values[np.ix_(y0, x0)]
    v10 = values[np.ix_(y0, x1)]
    v01 = values[np.ix_(y1, x0)]
    v11 = values[np.ix_(y1, x1)]

    w00 = (1.0 - dx)[None, :] * (1.0 - dy)[:, None]
    w10 = dx[None, :] * (1.0 - dy)[:, None]
    w01 = (1.0 - dx)[None, :] * dy[:, None]
    w11 = dx[None, :] * dy[:, None]

    stacked = np.stack([v00, v10, v01, v11])
    weights = np.stack([w00, w10, w01, w11])
    finite = np.isfinite(stacked)
    weighted_sum = np.sum(np.where(finite, stacked * weights, 0.0), axis=0)
    weight_sum = np.sum(np.where(finite, weights, 0.0), axis=0)
    out = np.divide(weighted_sum, weight_sum, out=np.full_like(weighted_sum, np.nan), where=weight_sum > 0.0)
    return out


def _classify(values: np.ndarray, edges: np.ndarray, rgb: np.ndarray):
    out = np.full(values.shape + (3,), 255, dtype=np.uint8)
    valid = np.isfinite(values)
    idx = np.digitize(values[valid], edges, right=False)
    idx = np.clip(idx, 0, len(rgb) - 1)
    out[valid] = rgb[idx]
    return out


def _draw_legend(draw: ImageDraw.ImageDraw, x0: int, y0: int, rgb: np.ndarray, edges: np.ndarray, font):
    swatch_w = 18
    swatch_h = 12
    gap = 3
    per_col = 4
    for i in range(len(rgb)):
        col = i // per_col
        row = i % per_col
        x = x0 + col * 250
        y = y0 + row * (swatch_h + gap)
        draw.rectangle([x, y, x + swatch_w, y + swatch_h], fill=tuple(int(v) for v in rgb[i]), outline=(35, 35, 35))
        if i == 0:
            label = '< 0.00'
        elif i == len(rgb) - 1:
            label = '>= 32.00'
        else:
            label = f'{edges[i-1]:.2f} - {edges[i]:.2f}'
        draw.text((x + swatch_w + 5, y - 1), label, fill=(20, 20, 20), font=font)


def render_day(day: str, out_path: Path, edges: np.ndarray, rgb: np.ndarray):
    ds, sst, lon_name, lat_name, lon_vals, lat_vals = _open_day(day)
    try:
        xtile, ytile, x_min, y_min, x_max, y_max = _mercator_tile_bounds(TARGET['lon'], TARGET['lat'], ZOOM)
        tile_m = (x_max - x_min)
        width_tiles = WEST_TILES + EAST_TILES + 1
        height_tiles = NORTH_TILES + SOUTH_TILES + 1
        x_min = x_min - WEST_TILES * tile_m
        x_max = x_max + EAST_TILES * tile_m
        y_min = y_min - SOUTH_TILES * tile_m
        y_max = y_max + NORTH_TILES * tile_m
        map_w = width_tiles * TILE_SIZE * OUT_SCALE
        map_h = height_tiles * TILE_SIZE * OUT_SCALE

        xs = x_min + (np.arange(map_w) + 0.5) * (x_max - x_min) / map_w
        ys = y_max - (np.arange(map_h) + 0.5) * (y_max - y_min) / map_h
        lon_targets = np.degrees(xs / R)
        lat_targets = np.degrees(np.arctan(np.sinh(ys / R)))

        values, lon_vals, lat_vals = _prepare_source_grid(sst, lon_vals, lat_vals, lon_name, lat_name)
        lon_targets = _normalize_longitudes(lon_vals, lon_targets)
        data_c = _bilinear_sample(values, lon_vals, lat_vals, lon_targets, lat_targets)
        classified = _classify(data_c, edges, rgb)
        plot_img = Image.fromarray(classified, mode='RGB')

        tx = (TARGET['lon'] + 180.0) / 360.0 * (2 ** ZOOM)
        lat_rad = math.radians(TARGET['lat'])
        ty = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * (2 ** ZOOM)
        px = int(round((tx - (xtile - WEST_TILES)) * TILE_SIZE * OUT_SCALE))
        py = int(round((ty - (ytile - NORTH_TILES)) * TILE_SIZE * OUT_SCALE))

        legend_h = 290
        title_h = 72
        pad = 18
        canvas = Image.new('RGB', (plot_img.width + pad * 2, plot_img.height + title_h + legend_h + pad * 2), 'white')
        draw = ImageDraw.Draw(canvas)
        font = ImageFont.load_default()

        center_idx_lon = int(np.abs(lon_vals - TARGET['lon']).argmin())
        center_idx_lat = int(np.abs(lat_vals - TARGET['lat']).argmin())
        center_val = float(values[center_idx_lat, center_idx_lon])

        draw.text((pad, 10), f'GHRSST projected map {day}  z={ZOOM} tile=({xtile},{ytile})  center={center_val:.3f} C', fill=(0, 0, 0), font=font)
        draw.text((pad, 24), f'target=({TARGET["lat"]:.5f}, {TARGET["lon"]:.5f})  bbox_3857=({x_min:.1f}, {y_min:.1f}, {x_max:.1f}, {y_max:.1f})', fill=(0, 0, 0), font=font)
        canvas.paste(plot_img, (pad, title_h))

        mx = pad + px
        my = title_h + py
        for d in range(-7, 8):
            if 0 <= mx + d < canvas.width:
                canvas.putpixel((mx + d, my), (0, 0, 0))
            if 0 <= my + d < canvas.height:
                canvas.putpixel((mx, my + d), (0, 0, 0))
        draw.ellipse((mx - 3, my - 3, mx + 3, my + 3), outline=(255, 255, 255), width=1)
        draw.ellipse((mx - 1, my - 1, mx + 1, my + 1), fill=(0, 0, 0))

        legend_y = title_h + plot_img.height + 14
        draw.text((pad, legend_y - 16), 'WMS bins', fill=(0, 0, 0), font=font)
        center_bin = int(np.digitize([center_val], edges, right=False)[0])
        start_idx = max(0, center_bin - 8)
        end_idx = min(len(rgb), center_bin + 9)
        _draw_legend(draw, pad, legend_y, rgb[start_idx:end_idx], edges[max(start_idx - 1, 0): min(end_idx, len(edges))], font)

        canvas.save(out_path)
    finally:
        ds.close()


def main():
    edges, rgb = _load_wms_bins()
    out_dir = ROOT / 'tests'
    out_dir.mkdir(parents=True, exist_ok=True)
    for day in DATES:
        out = out_dir / f'ghrsst_local_map_{day}.png'
        render_day(day, out, edges, rgb)
        print(out)


if __name__ == '__main__':
    main()
