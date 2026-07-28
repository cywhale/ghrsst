# P4-S10 Production Swap Results

**Run date:** 2026-07-28 (Asia/Taipei)

## Result

The first production delta prune/swap completed successfully using the validated
P4-S10 `s2` executor. The executor performed the staleness check, stopped the
GHRSST PM2 process before the path switch, moved the old delta to the hold area,
installed the validated staging delta, restarted the saved PM2 definition, and
verified `/healthz` before releasing the ingest lock.

- Kept delta days: `2026-06-23..2026-07-26` (34 days)
- Dropped delta days: `2026-06-05..2026-06-22` (18 days, base-covered)
- Result: `swapped`, `swap_performed=true`
- Measured API downtime: **2.791 seconds** from `port_down` to `healthz_up`
- Old delta hold: `/home/odbadmin/Data/ghrsst/hold/mur_timecube_s8_t90_sh128.delta.zarr.pre-prune-20260728T075145Z-60dd21`
- Hard delete: not performed

## Verification

- `/healthz`: HTTP `200`, `status=ok`
- `delta_day_count=34`
- `delta_latest=2026-07-26`
- `spatial_window=["2026-06-23", "2026-07-26"]`
- `cube_latest_in_sync=true`
- Recent bbox: HTTP `200`
- Historical bbox outside the delta window: HTTP `400`
- Historical point range: HTTP `200`

Artifacts are retained on VM24 under:

`/home/odbadmin/Data/ghrsst/logs/p4_delta_prune_20260727T021310Z/`

The first runner attempt failed before mutation because non-interactive SSH
did not include the PM2 PATH. The service was recovered with the saved PM2
definition. The final run used `/home/odbadmin/.npm-global/bin/pm2` explicitly.
