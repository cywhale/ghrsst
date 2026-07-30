"""dev2026 — P4 adopted spatial-window policy (WIRED into the API since P4-S3).

Adopted policy (P4 spec §4.1): spatial queries (bbox + POST /points) are served ONLY from days in the
bbox-friendly recent tier (the delta cube). `SPATIAL_WINDOW_DAYS = 31` is the retention target that keeps
that tier ~31 days deep. So the enforceable rule is exactly **delta membership**:

    spatial served  ⇔  requested day ∈ delta.days

Older / non-delta spatial requests return a clear 4xx naming the available spatial window (= the delta's
[earliest .. latest]).

Enforcement: `api/app.py::_spatial_window_gate` calls `spatial_day_allowed` / `rejection_payload` on both
the bbox and `POST /points` paths, behind `GHRSST_SPATIAL_WINDOW_ENFORCE` (production: ON). The gate runs
BEFORE those paths' daily-availability check, so that after the P4 daily prune a historical day — which
is absent from the pruned daily store as well — is still answered with THIS policy payload rather than
the daily staging range. The gate is a no-op when enforcement is off or no delta tier is loaded
(transition safety), and those configurations keep the daily-availability message.

Point GET / point range are NOT gated here: their availability is the full base+delta+daily union
(`HybridRouter.point_*`). Spatial availability and point availability are deliberately different scopes.
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
