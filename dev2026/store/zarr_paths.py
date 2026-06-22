"""dev2026 — store path resolution and day enumeration.

No hardcoded data paths. The store location is resolved from the environment
(GHRSST_ZARR_PATH), mirroring ghrsst_app.Cfg. The local copy under bak/ is only
one possible value passed at run time; production lives elsewhere on VM24.

The store is also assumed to be *live* (still being copied / appended daily), so
day enumeration always re-scans and never assumes a contiguous span.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from typing import List, Optional


def resolve_zarr_path() -> str:
    """Resolve the Zarr root. Priority: GHRSST_ZARR_PATH env, else error.

    We deliberately do NOT fall back to a baked-in bak/ path: the production
    store is elsewhere, and silent defaults hide misconfiguration in benchmarks.
    """
    p = os.environ.get("GHRSST_ZARR_PATH")
    if not p:
        raise SystemExit(
            "GHRSST_ZARR_PATH is not set. Point it at a mur.zarr root, e.g.\n"
            "  export GHRSST_ZARR_PATH=$(pwd)/bak/data/mur.zarr   # local copy\n"
            "  export GHRSST_ZARR_PATH=/srv/ghrsst/data/mur.zarr  # production"
        )
    p = os.path.abspath(p)
    if not os.path.isdir(p):
        raise SystemExit(f"GHRSST_ZARR_PATH does not exist or is not a dir: {p}")
    return p


def group_path(root: str, day: str) -> str:
    y, m, d = day.split("-")
    return os.path.join(root, y, m, d)


def group_exists(root: str, day: str) -> bool:
    return os.path.isdir(group_path(root, day))


def group_ref(day: str) -> str:
    """xarray group= argument for a day, e.g. '/2025/10/01'."""
    return "/" + day.replace("-", "/")


def list_existing_days(root: str) -> List[str]:
    """Re-scan /YYYY/MM/DD groups present *right now*. Tolerates gaps and a
    store still being copied (file count growing)."""
    days: List[str] = []
    try:
        for y in sorted(os.listdir(root)):
            yp = os.path.join(root, y)
            if not (y.isdigit() and len(y) == 4 and os.path.isdir(yp)):
                continue
            for m in sorted(os.listdir(yp)):
                mp = os.path.join(yp, m)
                if not (m.isdigit() and len(m) == 2 and os.path.isdir(mp)):
                    continue
                for d in sorted(os.listdir(mp)):
                    dp = os.path.join(mp, d)
                    if d.isdigit() and len(d) == 2 and os.path.isdir(dp):
                        days.append(f"{y}-{m}-{d}")
    except FileNotFoundError:
        pass
    return days


def daterange_inclusive(s: str, e: str) -> List[str]:
    d0 = datetime.strptime(s, "%Y-%m-%d").date()
    d1 = datetime.strptime(e, "%Y-%m-%d").date()
    n = (d1 - d0).days + 1
    return [(d0 + timedelta(days=i)).isoformat() for i in range(n)]
