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

## Scope of what these artifacts prove — corrections from the actual VM24 rehearsal

The following statements are the authoritative reading of the P4-S10 artifacts. Where an earlier
summary or conversation implied more, this section governs.

- **P4-S10 artifacts record the quiescence and preflight state of this execution only.**
- **`anchor_root` preflight verifies the configured path, declaration, and local filesystem
  relationship; it cannot prove that the path is outside the VM's snapshot/rollback domain.**
- **Delta prune proves only that readers were quiesced before this swap; it does not close the
  retained-snapshot deletion gap.**
- **G10 and whole-VM snapshot rollback remain PARTIAL until they have their own corresponding
  operational evidence.**

Stated the other way round, so it cannot be read as modesty about a result that was actually
achieved: a successful delta prune is evidence about **one swap**. It is not evidence about where
the anchor lives, and it is not evidence about what a reader holding an open snapshot sees when a
referenced block is deleted. Those are different questions with different experiments, and
neither experiment has been run.

## Degraded candidate-only run (2026-08 rehearsal) — operator-reported

Recorded from the operator's report of the rehearsal; **not independently verified here**, since
this work has no VM24 access.

| fact | value |
|---|---|
| base latest | `2026-06-26` |
| delta after the degraded prune | `2026-06-27`..`2026-08-17` (52 days) |
| calendar drop candidates | 22 |
| approved candidates dropped | 4 |
| `blocked_need_compaction_first` retained | 18 |
| free disk | ~267 G |
| hold | ~2.4 T, including expired historical daily data |

Expired hold data **must not be hard-deleted** before the P5 / O2 source and retention policy is
decided: the hold is currently the only copy of days that base does not cover, and those are
exactly the 18 blocked days.

### What the rehearsal changed in the runbook

The §4a keep-set arithmetic (`every day newer than the keep window`) would have offered all 22
calendar candidates for dropping, including the 18 base-uncovered ones. `prune_delta`'s
base-coverage gate refused that plan, so **nothing was lost** — but the day set was wrong before
the gate saw it. The arithmetic now lives in `ops/degraded_prune.py`, is a pure function, and is
tested; `blocked_need_compaction_first` non-empty is a **NO-GO by default**, and degraded
candidate-only mode requires a recorded operator approval naming exactly the audit's candidates.
