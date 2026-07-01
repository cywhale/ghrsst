"""dev2026 — P4 adopted spatial-window policy (helper; NOT yet wired into the API).

Adopted policy (P4 spec §4.1): spatial queries (bbox + POST /points) are served ONLY from days in the
bbox-friendly recent tier (the delta cube). `SPATIAL_WINDOW_DAYS = 31` is the retention target that keeps
that tier ~31 days deep. So the enforceable rule is exactly **delta membership**:

    spatial served  ⇔  requested day ∈ delta.days

Older / non-delta spatial requests return a clear 4xx naming the available spatial window (= the delta's
[earliest .. latest]). This module is a pure helper used by the P4-S0b harness to SIMULATE the policy
(enforcement is not yet implemented in the API); P4-S3 will wire it into `api/app.py`.
"""
from __future__ import annotations

import os
from typing import List, Optional, Sequence

# Adopted default; deployment may later override via env (kept read-only here).
SPATIAL_WINDOW_DAYS = int(os.environ.get("GHRSST_SPATIAL_WINDOW_DAYS", "31"))


def spatial_window_bounds(delta_days: Sequence[str]) -> Optional[tuple]:
    """The available spatial window = the delta's [earliest, latest] (None if delta empty)."""
    days = sorted(d for d in delta_days if d)
    return (days[0], days[-1]) if days else None


def spatial_day_allowed(day: str, delta_days: Sequence[str]) -> bool:
    """Spatial availability == delta membership (base blocks are t90/s8 = bbox-hostile, never served)."""
    return day in set(delta_days)


def rejection_payload(day: str, delta_days: Sequence[str]) -> dict:
    """The 4xx body an API would return for a spatial query outside the window."""
    bounds = spatial_window_bounds(delta_days)
    win = f"{bounds[0]}..{bounds[1]}" if bounds else "(none)"
    return {"error": "spatial query outside the available window",
            "requested_day": day,
            "spatial_window_days": SPATIAL_WINDOW_DAYS,
            "available_spatial_window": win,
            "hint": (f"bbox / POST points are limited to the recent spatial window "
                     f"({win}); use point time-series / single-day point for older days")}


def classify_days(days: Sequence[str], delta_days: Sequence[str]) -> dict:
    """Split requested spatial days into served (in delta) vs rejected (not in delta)."""
    dset = set(delta_days)
    served = [d for d in days if d in dset]
    rejected = [d for d in days if d not in dset]
    return {"served": served, "rejected": rejected}
