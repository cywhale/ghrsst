#!/usr/bin/env python3
"""dev2026 — P4-S5: read-only retention / prune-eligibility / compaction audit.

STRICTLY READ-ONLY. This tool never mutates, deletes, moves, appends, compacts, or rebuilds anything. It
opens every Zarr store with ``mode='r'``, reads only ``attrs`` + directory listings, and emits a
deterministic JSON report describing what a FUTURE prune/compaction (P4-S6/S7/S8) *would* be allowed to do.
It emits **dry-run candidates only** — it does not act on them.

Design guarantees (enforced by tests in ``tests/test_phase2_p4s5.py``):
- It does NOT import any mutating path (``ingest.dual_write.append_to_delta`` / ``compact`` /
  ``ingest.build_timecube*``) and does NOT call ``shutil.rmtree`` / ``os.replace`` / ``os.rename`` /
  ``os.remove`` / any write. The only ``shutil`` symbol used is ``disk_usage`` (read-only).
- All windows are computed over **sorted calendar days** (never a bare ``len(days)`` / ``sorted(days)[-N:]``
  entry slice), per P4-S4 §4.1: the recent 31 calendar days must be contiguous or pruning is blocked.
- Delta ``attrs['days']`` may be in **physical append order** (not chronological) after a backfill; the tool
  reports both orders and always drives logic from ``max(days)`` / sorted days, never ``days[-1]``.

Spec: ``specs/p4_ingest_prune_retention_design.md`` (P4-S4 baseline). Results:
``specs/p4s5_retention_audit_results.md``.

Usage (local/shadow OR read-only on VM24):
    dev2026/.venv/bin/python dev2026/ops/p4_retention_audit.py \
        --daily /path/mur.zarr --base /path/base.zarr --delta /path/delta.zarr \
        [--spatial-window-days 31] [--staging-buffer-days 7] [--delta-buffer-days 3] \
        [--mode conservative|accepted_risk] [--measure-sizes] [--healthz-url http://127.0.0.1:8035] \
        [--json-out out.json] [--strict]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from shutil import disk_usage  # read-only FS stat; NOT rmtree/move
from typing import Dict, List, Optional, Sequence

import zarr

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from store.zarr_paths import list_existing_days  # noqa: E402  (pure read-only day scan)
from store import spatial_policy  # noqa: E402  (pure helper: spatial_window_bounds — same code the API uses)

AUDIT_VERSION = "p4s5-1"


# --------------------------------------------------------------------------- calendar helpers (pure)
def _d(s: str) -> date:
    return date.fromisoformat(s)


def _calendar_range(start: str, end: str) -> List[str]:
    """Inclusive list of ISO days from start..end (chronological). Empty if start > end."""
    d0, d1 = _d(start), _d(end)
    if d0 > d1:
        return []
    return [(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)]


def _minus_days(day: str, n: int) -> str:
    return (_d(day) - timedelta(days=n)).isoformat()


def _span_report(days: Sequence[str]) -> dict:
    """Chronological span facts for a day set. Never returns a bare count as a *driver* — callers use the
    sorted set + gaps. ``days`` is taken as-is (may be physical/append order for a delta)."""
    physical = list(days)
    uniq = sorted(set(physical))
    out = {
        "count": len(physical),
        "unique_count": len(uniq),
        "earliest": (uniq[0] if uniq else None),
        "latest": (uniq[-1] if uniq else None),        # chronological max — NOT days[-1]
        "is_unique": len(physical) == len(uniq),
        "is_sorted_chronological": physical == uniq,   # False => physical append order after a backfill
    }
    if uniq:
        expected = _calendar_range(uniq[0], uniq[-1])
        present = set(uniq)
        gaps = [d for d in expected if d not in present]
        out["calendar_span_days"] = len(expected)      # inclusive earliest..latest calendar length
        out["gap_count"] = len(gaps)
        out["gaps"] = gaps
    else:
        out["calendar_span_days"] = 0
        out["gap_count"] = 0
        out["gaps"] = []
    return out


# --------------------------------------------------------------------------- read-only store access
def _read_days(path: Optional[str]) -> dict:
    """Open a cube read-only and return its attrs['days'] (physical order) + a span report. Never writes."""
    if not path:
        return {"present": False, "path": None, "reason": "not provided"}
    if not os.path.isdir(path):
        return {"present": False, "path": path, "reason": "path does not exist"}
    try:
        g = zarr.open_group(path, mode="r")            # READ-ONLY
        days = list(g.attrs.get("days", []))
        info = {"present": True, "path": path, "physical_days": days, **_span_report(days)}
        # append-order vs chronological latest — the P4 invariant surface
        info["latest_physical"] = (days[-1] if days else None)
        info["latest_chronological"] = info["latest"]
        info["append_order_differs"] = bool(days) and (days[-1] != info["latest"])
        return info
    except Exception as exc:                            # never raise from a read
        return {"present": False, "path": path, "reason": f"open failed: {type(exc).__name__}: {exc}"}


def _read_daily(path: Optional[str]) -> dict:
    if not path:
        return {"present": False, "path": None, "reason": "not provided"}
    if not os.path.isdir(path):
        return {"present": False, "path": path, "reason": "path does not exist"}
    try:
        days = list_existing_days(path)                # READ-ONLY /YYYY/MM/DD scan
        return {"present": True, "path": path, "physical_days": days, **_span_report(days)}
    except Exception as exc:
        return {"present": False, "path": path, "reason": f"scan failed: {type(exc).__name__}: {exc}"}


def _dayset(store_info: dict) -> set:
    return set(store_info.get("physical_days", [])) if store_info.get("present") else set()


# --------------------------------------------------------------------------- report sections
def recent_spatial_window(delta: dict, window_days: int) -> dict:
    """The latest ``window_days`` CALENDAR days ending at max(delta.days); contiguity + missing list."""
    dd = sorted(_dayset(delta))
    if not dd:
        return {"available": False, "reason": "no delta days"}
    window_end = dd[-1]
    window_start = _minus_days(window_end, window_days - 1)
    required = _calendar_range(window_start, window_end)   # the 31 calendar days that MUST be present
    present = set(dd)
    missing = [d for d in required if d not in present]
    contiguous = not missing
    return {
        "available": True,
        "window_start": window_start,
        "window_end": window_end,
        "spatial_window_days": window_days,
        "required_calendar_days": len(required),
        "recent_window_contiguous": contiguous,
        "missing_in_window": missing,
        # prune is only safe when the served window has no hole (P4-S4 §4.1)
        "prune_eligible": contiguous,
        "action": ("proceed" if contiguous else "repair_first"),
        # same string the API/spatial_policy reports, for cross-check
        "expected_spatial_window": _window_str(spatial_policy.spatial_window_bounds(dd)),
    }


def staging_keep(daily: dict, base: dict, delta: dict, window_days: int, staging_buffer: int,
                 mode: str, *, window_ok: bool = True, is_gap: bool = False) -> dict:
    """Which daily-staging days a future prune would KEEP vs be a (dry-run) candidate to drop.

    conservative: keep {daily days NOT yet in base} ∪ {latest window+buffer calendar days}.
    accepted_risk: keep {latest window+buffer calendar days} only (older un-compacted days rely on NetCDF
    redownload for recovery — flagged in the manifest preview).

    GLOBAL PRECONDITION (P4-S4 §4.1): if the recent spatial window is not confirmed contiguous
    (``window_ok`` False), NO prune candidate is emitted — repair first."""
    daily_days = sorted(_dayset(daily))
    base_set = _dayset(base)
    anchor = _served_anchor(daily, delta)
    result = {"mode": mode, "anchor_latest": anchor}
    if not daily_days or anchor is None:
        return {**result, "available": False, "reason": "no daily days or no anchor"}
    keep_start = _minus_days(anchor, window_days + staging_buffer - 1)
    in_recent = {d for d in daily_days if d >= keep_start}       # calendar window, not a slice
    not_in_base = {d for d in daily_days if d not in base_set}
    if mode == "conservative":
        keep = in_recent | not_in_base
    elif mode == "accepted_risk":
        keep = set(in_recent)
    else:
        raise ValueError(f"unknown mode: {mode}")
    candidates = sorted(d for d in daily_days if d not in keep)
    result.update({
        "available": True,
        "keep_window_start": keep_start,
        "keep_window_days": window_days + staging_buffer,
        "staging_buffer_days": staging_buffer,
        "keep_count": len(keep),
        "daily_prune_candidates": candidates,       # DRY-RUN ONLY — nothing is deleted
        "daily_prune_candidate_count": len(candidates),
        "dry_run": True,
        "note": ("conservative: a day not yet compacted into base is NEVER a candidate"
                 if mode == "conservative"
                 else "accepted_risk: candidates older than base rely on NetCDF redownload (see manifest)"),
    })
    return _gate_window(result, "daily_prune_candidates", "daily_prune_candidate_count", window_ok, is_gap)


def delta_prune(base: dict, delta: dict, window_days: int, delta_buffer: int, *,
                window_ok: bool = True, is_gap: bool = False) -> dict:
    """Which delta days a future prune would drop — calendar keep-set, then base-coverage filter.

    keep_days = delta days within the latest (window_days + delta_buffer) CALENDAR days.
    drop candidates = delta_days - keep_days, BUT only eligible if base already covers the day (else the
    day would lose its point/range history -> not eligible; compaction must run first).

    GLOBAL PRECONDITION (P4-S4 §4.1): if the recent spatial window is not confirmed contiguous
    (``window_ok`` False), NO delta prune candidate is emitted — even for older days base already covers —
    repair the window hole first."""
    dd = sorted(_dayset(delta))
    if not dd:
        return {"available": False, "reason": "no delta days"}
    base_set = _dayset(base)
    keep_start = _minus_days(dd[-1], window_days + delta_buffer - 1)
    keep_days = [d for d in dd if d >= keep_start]              # calendar range (NOT sorted(dd)[-N:])
    drop_candidates = [d for d in dd if d < keep_start]
    eligible, blocked = [], []
    for d in drop_candidates:
        if d in base_set:
            eligible.append(d)                                 # safe: point/range history is in base
        else:
            blocked.append(d)                                  # base does not cover -> compaction deferred
    reason = None
    if drop_candidates and not eligible:
        reason = ("no delta prune candidates eligible: base does not cover the older delta days "
                  "(compaction deferred / O4) — compact into base first")
    elif not drop_candidates:
        reason = "no delta days older than the keep window; nothing to prune"
    result = {
        "available": True,
        "keep_window_start": keep_start,
        "keep_window_days": window_days + delta_buffer,
        "delta_buffer_days": delta_buffer,
        "keep_day_count": len(keep_days),
        # Four DIFFERENT sets. They were easy to confuse, and the 2026-08 rehearsal showed
        # what the confusion costs: 22 calendar candidates, only 4 of them base-covered.
        #
        #   drop_candidates_by_calendar   -- older than the keep window. Says nothing about base.
        #   delta_prune_candidates        -- of those, the ones base COVERS. The only set a prune
        #                                    may drop.
        #   blocked_need_compaction_first -- of those, the ones base does NOT cover. Never
        #                                    droppable until they are compacted in.
        #   base_uncovered_approved_candidates -- days an operator approved that base does not
        #                                    cover. Always empty here by construction; present so
        #                                    a consumer can assert on it rather than infer it.
        "drop_candidates_by_calendar": drop_candidates,
        # NOT "base covers the days we are about to drop" -- that is true of
        # `delta_prune_candidates` by construction. It means "base covers EVERY calendar drop
        # candidate, so nothing is blocked". False whenever any day is blocked, even when there
        # are eligible candidates to drop: the rehearsal's 4-of-22 case reports False.
        "base_covers_all_drop_candidates": (not blocked and bool(drop_candidates)),
        "delta_prune_candidates": eligible,          # eligible ONLY after base coverage -- dry-run
        "delta_prune_candidate_count": len(eligible),
        "blocked_need_compaction_first": blocked,
        "base_uncovered_approved_candidates": [],
        "reason": reason,
        "dry_run": True,
    }
    return _gate_window(result, "delta_prune_candidates", "delta_prune_candidate_count", window_ok, is_gap)


def compaction_status(daily: dict, base: dict, delta: dict, window_days: int) -> dict:
    """base/delta spans, delta days older than the spatial window, O4-defer condition, disk feasibility hint
    for a FULL rebuild (O1/O3). Never claims O2 (block-level) is implementable — that needs the §6.1 gate."""
    dd = sorted(_dayset(delta))
    base_set = _dayset(base)
    out = {
        "base_latest": base.get("latest") if base.get("present") else None,
        "delta_earliest": (dd[0] if dd else None),
        "delta_latest": (dd[-1] if dd else None),
        "delta_calendar_span_days": (delta.get("calendar_span_days", 0)),
        "delta_day_count": len(dd),
    }
    if dd:
        window_start = _minus_days(dd[-1], window_days - 1)
        older = [d for d in dd if d < window_start]
        older_not_in_base = [d for d in older if d not in base_set]
        out["spatial_window_start"] = window_start
        out["delta_days_older_than_window"] = older
        out["delta_days_older_than_window_not_in_base"] = older_not_in_base
        # O4 = defer compaction + let delta grow past the window (with an alarm). The audit reports whether
        # that condition is currently WARRANTED (old, un-compacted delta days exist), not whether an alarm
        # is wired (it cannot know that from read-only state).
        out["o4_defer_warranted"] = bool(older_not_in_base)
        out["interpretation"] = (
            "delta extends before the spatial window with days not yet in base -> compaction is behind; "
            "O4 defer-with-alarm is the safe current-disk strategy (P4-S4 §6)"
            if older_not_in_base else
            "delta does not extend before the spatial window (or all older days already in base) -> no "
            "compaction backlog")
    out["o2_block_level"] = "NOT implementable without the §6.1 base-segmentation design gate"
    return out


def disk_report(daily: dict, base: dict, delta: dict, measure_sizes: bool) -> dict:
    """Free/used for the filesystem(s) holding the stores (read-only). Optional heavy footprint walk gates
    the O1/O3 full-rebuild feasibility hint. Environment-dependent — excluded from the determinism claim."""
    out = {"filesystems": {}, "measured_sizes": bool(measure_sizes)}
    seen = {}
    for name, info in (("daily", daily), ("base", base), ("delta", delta)):
        p = info.get("path")
        if not p or not os.path.isdir(p):
            continue
        try:
            st = os.stat(p)
            key = st.st_dev
            if key not in seen:
                du = disk_usage(p)
                seen[key] = {"example_path": p, "total_bytes": du.total, "used_bytes": du.used,
                             "free_bytes": du.free}
            out["filesystems"].setdefault(str(seen[key]["example_path"]), seen[key])
        except Exception as exc:
            out.setdefault("errors", []).append(f"{name}: {type(exc).__name__}: {exc}")
    if measure_sizes and base.get("present"):
        base_bytes = _dir_size(base["path"])           # read-only stat walk (heavy; opt-in)
        out["base_footprint_bytes"] = base_bytes
        # O1 full rebuild needs a full copy of base beside the old one before the swap.
        free = None
        for fs in out["filesystems"].values():
            free = fs["free_bytes"]                     # single-FS common case
            break
        if free is not None and base_bytes is not None:
            out["o1_full_rebuild_disk_feasible_hint"] = bool(free >= base_bytes)
            out["o1_hint_note"] = ("free >= base footprint (a full rebuild MIGHT fit; add a hard safety "
                                   "margin before acting)" if free >= base_bytes else
                                   "free < base footprint -> full rebuild (O1/O3) NOT disk-feasible; use O4")
    else:
        out["o1_full_rebuild_disk_feasible_hint"] = None
        out["o1_hint_note"] = "pass --measure-sizes to estimate base footprint (heavy directory walk)"
    return out


def api_policy_check(delta: dict, window_days: int, healthz_url: Optional[str]) -> dict:
    """Optional READ-ONLY GET /healthz; compare API's served view against the computed expectation."""
    dd = sorted(_dayset(delta))
    expected = {
        "delta_latest": (dd[-1] if dd else None),
        "delta_day_count": len(dd),
        "spatial_window": list(spatial_policy.spatial_window_bounds(dd)) if dd else None,
    }
    if not healthz_url:
        return {"checked": False, "expected": expected, "note": "pass --healthz-url to cross-check /healthz"}
    import urllib.request                                # GET only
    url = healthz_url.rstrip("/") + "/healthz"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:   # READ-ONLY
            hz = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        return {"checked": False, "expected": expected, "healthz_url": url,
                "error": f"{type(exc).__name__}: {exc}"}
    got = {k: hz.get(k) for k in ("delta_latest", "delta_day_count", "spatial_window",
                                  "cube_latest_in_sync", "spatial_window_enforce", "coveragejson_enabled")}
    mismatches = {}
    for k, exp in expected.items():
        if got.get(k) != exp:
            mismatches[k] = {"expected": exp, "healthz": got.get(k)}
    # stale-metadata surface (P4-S0b finding): API delta_latest behind the on-disk delta
    stale = bool(dd) and got.get("delta_latest") is not None and got["delta_latest"] < dd[-1]
    return {"checked": True, "healthz_url": url, "expected": expected, "healthz": got,
            "mismatches": mismatches, "consistent": not mismatches,
            "api_delta_metadata_stale": stale}


def manifest_preview(staging: dict, base: dict, delta: dict, mode: str) -> List[dict]:
    """Would-be prune-manifest records for the DAILY prune candidates. dry_run=true on every record; no
    timestamps/operator (stamped by ops at real run time) so output stays deterministic."""
    if not staging.get("available"):
        return []
    base_set = _dayset(base)
    delta_set = _dayset(delta)
    recent_start = staging.get("keep_window_start")
    recs = []
    for day in staging["daily_prune_candidates"]:
        base_covers = day in base_set
        in_recent = (recent_start is not None and day >= recent_start)
        recs.append({
            "day": day,
            "action": "prune_daily_staging",
            "dry_run": True,
            "mode": mode,
            "base_covers": base_covers,
            "delta_present": day in delta_set,
            "in_recent_window": in_recent,
            # accepted_risk can pick days not in base -> recovery is NetCDF redownload
            "redownload_required": (mode == "accepted_risk" and not base_covers),
            "pruned_at": None, "operator": None, "hold_until": None,   # ops stamps at real run time
        })
    return recs


# --------------------------------------------------------------------------- small utils
def _window_str(bounds) -> Optional[str]:
    return f"{bounds[0]}..{bounds[1]}" if bounds else None


def _gate_window(result: dict, cand_key: str, count_key: str, window_ok: bool, is_gap: bool) -> dict:
    """Global prune precondition (P4-S4 §4.1): if the recent spatial window is not confirmed contiguous,
    suppress this section's prune candidates entirely and mark it blocked. Diagnostic fields (keep window,
    drop-by-calendar) are kept so a repairer can see WHAT is missing; only the actionable candidate list is
    zeroed. Always stamp the flags so the shape is stable whether blocked or not."""
    blocked_by_gap = is_gap
    if not window_ok:
        result[cand_key] = []
        result[count_key] = 0
        result["action"] = "repair_first"
        result["reason"] = (
            "recent spatial window has holes (missing_in_window non-empty) — prune blocked until repaired"
            if is_gap else
            "recent spatial window unavailable (no delta days) — cannot confirm the served window")
    result["blocked_by_recent_window_gap"] = blocked_by_gap
    result["blocked_by_recent_window"] = not window_ok
    return result


def _served_anchor(daily: dict, delta: dict) -> Optional[str]:
    cands = [x for x in (daily.get("latest") if daily.get("present") else None,
                         delta.get("latest") if delta.get("present") else None) if x]
    return max(cands) if cands else None


def _dir_size(path: str) -> Optional[int]:
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.stat(os.path.join(root, f)).st_size   # stat only — read-only
                except OSError:
                    pass
        return total
    except Exception:
        return None


def _collect_failures(report: dict) -> List[str]:
    """True audit FAILURES (drive nonzero exit under --strict). Warnings are separate."""
    fails = []
    rw = report["recent_spatial_window"]
    if rw.get("available") and not rw.get("recent_window_contiguous"):
        fails.append(f"recent spatial window has holes: missing {rw.get('missing_in_window')} -> repair_first")
    for name in ("base", "delta"):
        info = report["stores"][name]
        if info.get("present") and not info.get("is_unique"):
            fails.append(f"{name} cube has duplicate days (structural)")
    delta = report["stores"]["delta"]
    if delta.get("present") and delta.get("gap_count", 0) > 0:
        # a gap anywhere in delta is worth surfacing, but only the RECENT-window gap is a hard fail above
        pass
    ap = report.get("api_policy", {})
    if ap.get("checked") and ap.get("api_delta_metadata_stale"):
        fails.append("API /healthz delta_latest is behind the on-disk delta (stale metadata) — reload needed")
    return fails


def _collect_warnings(report: dict) -> List[str]:
    warns = []
    rw = report["recent_spatial_window"]
    if rw.get("available") and not rw.get("recent_window_contiguous"):
        warns.append("ALL prune candidates suppressed: recent spatial window has holes "
                     f"({rw.get('missing_in_window')}) -> repair_first (P4-S4 §4.1)")
    delta = report["stores"]["delta"]
    if delta.get("present") and delta.get("append_order_differs"):
        warns.append("delta attrs['days'] is in physical APPEND order (latest_physical != chronological "
                     "max) — expected after a backfill; logic uses chronological max, not days[-1]")
    if delta.get("present") and delta.get("gap_count", 0) > 0:
        warns.append(f"delta has {delta['gap_count']} calendar gap(s): {delta['gaps']}")
    dp = report.get("delta_prune", {})
    if dp.get("blocked_need_compaction_first"):
        warns.append(f"{len(dp['blocked_need_compaction_first'])} delta day(s) older than the window are "
                     "NOT in base yet -> compaction deferred (O4)")
    cs = report.get("compaction_status", {})
    if cs.get("o4_defer_warranted"):
        warns.append("O4 defer-with-alarm warranted: delta extends before the spatial window with days not "
                     "yet compacted into base")
    ap = report.get("api_policy", {})
    if ap.get("checked") and not ap.get("consistent"):
        warns.append(f"API /healthz differs from computed expectation: {ap.get('mismatches')}")
    return warns


# --------------------------------------------------------------------------- assembly
def build_report(args) -> dict:
    daily = _read_daily(args.daily)
    base = _read_days(args.base)
    delta = _read_days(args.delta)
    rw = recent_spatial_window(delta, args.spatial_window_days)
    # GLOBAL prune precondition (P4-S4 §4.1): a hole in the recent spatial window — OR an unavailable window
    # (no delta) — blocks EVERY prune candidate. `is_gap` is the specific "holes in the window" case the
    # reviewer flagged; `window_ok` also fails-closed when the window can't be confirmed at all.
    is_gap = bool(rw.get("available") and not rw.get("recent_window_contiguous"))
    window_ok = bool(rw.get("available") and rw.get("recent_window_contiguous"))
    report = {
        "audit_version": AUDIT_VERSION,
        "read_only": True,
        "params": {
            "spatial_window_days": args.spatial_window_days,
            "staging_buffer_days": args.staging_buffer_days,
            "delta_buffer_days": args.delta_buffer_days,
            "mode": args.mode,
        },
        "stores": {"daily": daily, "base": base, "delta": delta},
        "recent_spatial_window": rw,
        "staging_keep": staging_keep(daily, base, delta, args.spatial_window_days,
                                     args.staging_buffer_days, args.mode,
                                     window_ok=window_ok, is_gap=is_gap),
        "delta_prune": delta_prune(base, delta, args.spatial_window_days, args.delta_buffer_days,
                                   window_ok=window_ok, is_gap=is_gap),
        "compaction_status": compaction_status(daily, base, delta, args.spatial_window_days),
        "api_policy": api_policy_check(delta, args.spatial_window_days, args.healthz_url),
    }
    report["manifest_preview"] = (
        manifest_preview(report["staging_keep"], base, delta, args.mode) if window_ok else [])
    # disk is environment-dependent -> kept OUT of the deterministic logic core; placed last
    report["disk"] = disk_report(daily, base, delta, args.measure_sizes)
    report["alarm"] = _alarm(report, args)
    report["audit_failures"] = _collect_failures(report)
    report["warnings"] = _collect_warnings(report)
    return report


def _alarm(report: dict, args) -> dict:
    """P4-S8b: O4 defer-with-alarm ([RO], design spec §8). Fires when the delta's CALENDAR span exceeds
    the spatial window + slack (compaction is falling behind) or free disk drops under the hard margin.
    Evaluated always; drives a nonzero exit only under --alarm. Disk-unknown does not fire (reported)."""
    slack = getattr(args, "alarm_slack_days", 14)
    min_free_gb = getattr(args, "alarm_min_free_gb", 200)
    span = report["stores"]["delta"].get("calendar_span_days", 0)
    span_limit = args.spatial_window_days + slack
    fs = report.get("disk", {}).get("filesystems", {})
    free = min((f["free_bytes"] for f in fs.values()), default=None)
    reasons = []
    if span > span_limit:
        reasons.append(f"delta_span: {span}d > window({args.spatial_window_days}) + slack({slack}) = "
                       f"{span_limit}d — compaction is falling behind (O4); provision disk or decide §9-Q4")
    if free is not None and free < min_free_gb * 1024**3:
        reasons.append(f"free_disk: {round(free / 1024**3, 1)} GiB < hard margin {min_free_gb} GiB")
    return {"enabled": bool(getattr(args, "alarm", False)), "fired": bool(reasons), "reasons": reasons,
            "delta_span_days": span, "span_limit_days": span_limit,
            "min_free_gb": min_free_gb, "free_bytes": free, "free_unknown": free is None}


def deterministic_core(report: dict) -> dict:
    """The subset of the report that is a pure function of the stores' day sets (no disk/healthz/env). Tests
    assert this is stable across runs."""
    return {k: report[k] for k in ("audit_version", "params", "recent_spatial_window", "staging_keep",
                                   "delta_prune", "manifest_preview") if k in report}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P4-S5 read-only retention/prune-eligibility audit (no mutation).")
    ap.add_argument("--daily", help="daily staging store (mur.zarr) — read-only")
    ap.add_argument("--base", help="base time-cube — read-only")
    ap.add_argument("--delta", help="delta cube — read-only")
    ap.add_argument("--spatial-window-days", type=int, default=31)
    # Conservative default = 7: a whole week of buffer above the spatial window so a late backfill / delayed
    # compaction can still re-derive from staging before a day is ever a prune candidate (P4-S4 §4 rule 2).
    ap.add_argument("--staging-buffer-days", type=int, default=7)
    ap.add_argument("--delta-buffer-days", type=int, default=3)
    ap.add_argument("--mode", choices=("conservative", "accepted_risk"), default="conservative")
    ap.add_argument("--measure-sizes", action="store_true",
                    help="estimate base footprint via a read-only dir walk (heavy) to hint O1/O3 disk feasibility")
    ap.add_argument("--healthz-url", help="optional API base URL for a read-only /healthz cross-check")
    ap.add_argument("--json-out", help="write the JSON report here (still printed to stdout)")
    ap.add_argument("--strict", action="store_true", help="exit nonzero if there are true audit failures")
    # P4-S8b: O4 defer-with-alarm ([RO]; design spec §8). Exit 3 when fired — ops cron alerting hook.
    ap.add_argument("--alarm", action="store_true",
                    help="exit 3 if the O4 alarm fires (delta span > window+slack, or free disk < margin)")
    ap.add_argument("--alarm-slack-days", type=int, default=14,
                    help="alarm when delta calendar span exceeds spatial window + this slack (default 14)")
    ap.add_argument("--alarm-min-free-gb", type=int, default=200,
                    help="alarm when the stores' filesystem free space drops below this (GiB, default 200)")
    args = ap.parse_args(argv)

    report = build_report(args)
    text = json.dumps(report, indent=2, sort_keys=True)
    print(text)
    if args.json_out:
        with open(args.json_out, "w") as fh:          # the ONLY write: the user-requested report artifact
            fh.write(text + "\n")
    if args.strict and report["audit_failures"]:
        return 2                                       # audit failure outranks the alarm
    if args.alarm and report["alarm"]["fired"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
