# P4-S8b — O4 defer-with-alarm: results

Status: **DONE ([RO] only; no mutation).** Implements the S8-design §8 alarm — the deliverable that makes
O4 "defer compaction" a *bounded* posture instead of an ignored one.

- Implementation: `--alarm` mode on [`../ops/p4_retention_audit.py`](../ops/p4_retention_audit.py)
  (same read-only tool; the alarm block is always evaluated and included in the JSON report, and drives a
  **nonzero exit (3)** only under `--alarm`).
- Tests: `TestO4Alarm` in [`../tests/test_phase2_p4s5.py`](../tests/test_phase2_p4s5.py) — tool suite
  **18/18** (full P4 set 59 green).

## Behavior

```
p4_retention_audit.py --daily … --base … --delta … \
  --alarm [--alarm-slack-days 14] [--alarm-min-free-gb 200]
```

Fires (→ `alarm.fired: true`, exit **3** under `--alarm`) when EITHER:
1. **`delta_calendar_span_days > spatial_window_days + alarm_slack_days`** (default 31+14=45) —
   compaction is falling behind; prompts the §9-Q4 disk/segmentation decision; or
2. **free disk < `alarm_min_free_gb`** (default 200 GiB) on the stores' filesystem — approaching the hard
   margin below which no compaction/swap may run.

Report block: `{enabled, fired, reasons[], delta_span_days, span_limit_days, min_free_gb, free_bytes,
free_unknown}`. Unknown disk (no valid store paths) is reported, not fired. Exit-code precedence:
`--strict` audit failure = 2 outranks alarm = 3; plain run always exits 0 (report-only).

Ops wiring (cron schedule + alerting on exit 3) is a Codex/ops change at S9/S10 — not part of this repo's
scope.

## Tests
- span > limit → exit 3 (disk trigger isolated with `--alarm-min-free-gb 0`);
- span == limit → exit 0;
- absurd disk margin (~1 EiB) → exit 3 via the free-disk trigger on a real filesystem;
- without `--alarm` the block is still evaluated (`fired: true, enabled: false`) and exit stays 0.

Read-only guarantees unchanged (the S5 AST + mtime/size tests still pass over the same file).
