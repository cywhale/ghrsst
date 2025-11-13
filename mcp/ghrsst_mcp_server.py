"""
GHRSST MCP server exposing two curated tools:

1. ghrsst.point_value – fetch SST / anomaly for a single point & date.
2. ghrsst.bbox_mean  – compute mean SST / anomaly over a bbox for a single day.

The implementation follows specs/ghrsst_mcp_v0_1_0.spec and relies on the FastAPI
endpoint at /api/ghrsst. Configure runtime via the GHRSST_* environment vars.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Iterable, Literal, Sequence

import httpx
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.server import FastMCP
from fastmcp.tools import Tool

DEFAULT_API_BASE = "https://eco.odb.ntu.edu.tw/api/ghrsst"
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_POINT_LIMIT = 1_000_000
DEFAULT_DEG_PER_CELL = 0.01
DEFAULT_NEAREST_TOLERANCE_DAYS = 7
USER_AGENT = "ghrsst-mcp/0.1.0"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
INITIAL_BACKOFF = 0.75
MAX_SAMPLE_ATTEMPTS = 4
VALID_FIELDS = {"sst", "sst_anomaly"}


def _env_float(var: str, default: float) -> float:
    value = os.getenv(var)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:  # pragma: no cover - configuration error
        raise ToolError(f"Invalid float value for {var}: {value}") from exc


def _env_int(var: str, default: int) -> int:
    value = os.getenv(var)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:  # pragma: no cover - configuration error
        raise ToolError(f"Invalid int value for {var}: {value}") from exc


API_BASE = os.getenv("GHRSST_API_BASE", DEFAULT_API_BASE)
TIMEOUT_S = _env_float("GHRSST_TIMEOUT_S", DEFAULT_TIMEOUT_S)
POINT_LIMIT = _env_int("GHRSST_POINT_LIMIT", DEFAULT_POINT_LIMIT)
DEG_PER_CELL = _env_float("GHRSST_DEG_PER_CELL", DEFAULT_DEG_PER_CELL)
NEAREST_TOLERANCE_DAYS = _env_int("GHRSST_NEAREST_TOLERANCE_DAYS", DEFAULT_NEAREST_TOLERANCE_DAYS)

AUTH_TOKEN = os.getenv("GHRSST_AUTH_TOKEN")
DEFAULT_HEADERS = {"User-Agent": USER_AGENT}
if AUTH_TOKEN:
    DEFAULT_HEADERS["Authorization"] = AUTH_TOKEN

http_client = httpx.Client(timeout=TIMEOUT_S)

logger = logging.getLogger("ghrsst_mcp")
if not logger.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

app = FastMCP(
    name="ghrsst-mcp",
    version="0.1.0",
    instructions=(
        "Provides curated GHRSST tools backed by the /api/ghrsst REST endpoint. "
        "Use ghrsst.point_value for single-point lookups and ghrsst.bbox_mean for "
        "single-day regional means (with automatic stride selection)."
    ),
)


def _validate_date(date_str: str) -> str:
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as exc:
        raise ToolError(f"Invalid date (expected YYYY-MM-DD): {date_str}") from exc
    return date_str


def _unique_fields(fields: Sequence[str] | None) -> list[str]:
    if not fields:
        return ["sst"]
    seen: set[str] = set()
    ordered: list[str] = []
    for field in fields:
        if field not in VALID_FIELDS:
            raise ToolError(f"Unsupported field '{field}'. Allowed: {sorted(VALID_FIELDS)}")
        if field not in seen:
            ordered.append(field)
            seen.add(field)
    if not ordered:
        raise ToolError("At least one field must be requested.")
    return ordered


def _normalize_bbox(bbox: Sequence[float]) -> tuple[float, float, float, float]:
    if len(bbox) != 4:
        raise ToolError("bbox must contain exactly four numbers: [lon0, lat0, lon1, lat1].")
    lon0, lat0, lon1, lat1 = map(float, bbox)
    for lon in (lon0, lon1):
        if not -180 <= lon <= 180:
            raise ToolError(f"Longitude {lon} out of bounds [-180, 180].")
    for lat in (lat0, lat1):
        if not -90 <= lat <= 90:
            raise ToolError(f"Latitude {lat} out of bounds [-90, 90].")
    nlon0, nlon1 = sorted((lon0, lon1))
    nlat0, nlat1 = sorted((lat0, lat1))
    if nlon0 == nlon1 or nlat0 == nlat1:
        raise ToolError("bbox must cover a non-zero area.")
    return nlon0, nlat0, nlon1, nlat1


def _parse_available_range(detail: str) -> tuple[str, str] | None:
    """Extract earliest/latest dates from an API error message."""
    matches = re.findall(r"\d{4}-\d{2}-\d{2}", detail)
    if len(matches) >= 2:
        earliest, latest = matches[-2], matches[-1]
        if earliest > latest:
            earliest, latest = latest, earliest
        return earliest, latest
    return None


def _select_nearest_date(requested: str, earliest: str, latest: str) -> str:
    req_dt = datetime.strptime(requested, "%Y-%m-%d").date()
    ear_dt = datetime.strptime(earliest, "%Y-%m-%d").date()
    lat_dt = datetime.strptime(latest, "%Y-%m-%d").date()
    if req_dt <= ear_dt:
        return earliest
    if req_dt >= lat_dt:
        return latest
    diff_ear = (req_dt - ear_dt).days
    diff_lat = (lat_dt - req_dt).days
    return earliest if diff_ear <= diff_lat else latest


def _days_between(date_a: str, date_b: str) -> int:
    a = datetime.strptime(date_a, "%Y-%m-%d").date()
    b = datetime.strptime(date_b, "%Y-%m-%d").date()
    return abs((a - b).days)


def _extract_detail(resp: httpx.Response) -> str:
    text = resp.text.strip()
    if not text:
        text = resp.reason_phrase or ""
    try:
        data = resp.json()
    except ValueError:
        return text
    if isinstance(data, dict) and "detail" in data:
        detail = data["detail"]
        return detail if isinstance(detail, str) else str(detail)
    return text or str(data)


def _ghrsst_request(params: dict[str, Any]) -> httpx.Response:
    last_error: Any = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = http_client.get(API_BASE, params=params, headers=DEFAULT_HEADERS)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
        else:
            if resp.status_code in RETRYABLE_STATUS:
                last_error = resp
            else:
                return resp
        if attempt < MAX_RETRIES - 1:
            sleep_for = INITIAL_BACKOFF * (2**attempt)
            time.sleep(sleep_for)
    if isinstance(last_error, httpx.Response):
        detail = _extract_detail(last_error)
        raise ToolError(f"GHRSST API error ({last_error.status_code}): {detail}")
    raise ToolError(f"GHRSST API request failed: {last_error}")


def _handle_common_errors(resp: httpx.Response) -> None:
    if resp.status_code == 400:
        detail = _extract_detail(resp)
        if (
            "Data not exist" in detail
            or "available date" in detail
            or "available range" in detail
            or "BBOX query only allows single-day data" in detail
        ):
            raise NotFoundError(detail)
        raise ToolError(detail)
    if resp.status_code == 404:
        detail = _extract_detail(resp)
        raise NotFoundError(detail)
    if resp.is_error:
        detail = _extract_detail(resp)
        raise ToolError(f"GHRSST API error ({resp.status_code}): {detail}")


def _ensure_sample(fields: Iterable[float], label: str) -> None:
    for value in fields:
        if math.isnan(value) or math.isinf(value):
            raise ToolError(f"{label} contains invalid values.")


def _estimate_stride(lon0: float, lat0: float, lon1: float, lat1: float) -> int:
    nx = max(1, int(math.floor(abs(lon1 - lon0) / DEG_PER_CELL)) + 1)
    ny = max(1, int(math.floor(abs(lat1 - lat0) / DEG_PER_CELL)) + 1)
    total = nx * ny
    if total <= POINT_LIMIT:
        return 1
    stride = math.ceil(math.sqrt(total / POINT_LIMIT))
    return max(1, stride)


def ghrsst_point_value(
    *,
    longitude: float,
    latitude: float,
    date: str | None = None,
    fields: list[Literal["sst", "sst_anomaly"]] | None = None,
    method: Literal["exact", "nearest"] = "exact",
) -> dict[str, Any]:
    """Get SST and/or anomaly at a single lon/lat for a single day (defaults to latest)."""

    logger.info(
        "ghrsst.point_value invoked lon=%s lat=%s date=%s method=%s fields=%s",
        longitude,
        latitude,
        date,
        method,
        fields or ["sst"],
    )

    if not -180 <= longitude <= 180:
        raise ToolError(f"Longitude {longitude} out of bounds [-180, 180].")
    if not -90 <= latitude <= 90:
        raise ToolError(f"Latitude {latitude} out of bounds [-90, 90].")
    if method not in {"exact", "nearest"}:
        raise ToolError(f"Unsupported method '{method}'. Allowed: ['exact', 'nearest'].")
    if date:
        date = _validate_date(date)
    chosen_fields = _unique_fields(fields)

    def fetch(target_date: str | None) -> dict[str, Any]:
        params: dict[str, Any] = {
            "lon0": longitude,
            "lat0": latitude,
            "append": ",".join(chosen_fields),
        }
        if target_date:
            params["start"] = target_date

        resp = _ghrsst_request(params)
        if resp.status_code == 400:
            detail = _extract_detail(resp)
            if (
                "available date" in detail
                or "available range" in detail
                or "Data not exist" in detail
                or "BBOX query only allows single-day data" in detail
            ):
                raise NotFoundError(detail)
        _handle_common_errors(resp)

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ToolError("Invalid JSON in GHRSST response.") from exc

        if not isinstance(payload, list) or not payload:
            raise NotFoundError("No data returned for requested point/date.")

        row = payload[0]
        result: dict[str, Any] = {
            "region": [longitude, latitude],
            "date": row.get("date"),
        }
        if "sst" in chosen_fields:
            result["sst"] = row.get("sst")
        if "sst_anomaly" in chosen_fields:
            result["sst_anomaly"] = row.get("sst_anomaly")
        return result

    try:
        return fetch(date)
    except NotFoundError as exc:
        if method != "nearest" or not date:
            raise
        range_dates = _parse_available_range(str(exc))
        if not range_dates:
            raise
        fallback = _select_nearest_date(date, *range_dates)
        if fallback == date:
            raise
        delta_days = _days_between(date, fallback)
        if delta_days > NEAREST_TOLERANCE_DAYS:
            raise NotFoundError(
                f"No data for {date}; nearest available date {fallback} is {delta_days} days away (tolerance {NEAREST_TOLERANCE_DAYS} days)."
            ) from exc
        logger.info(
            "ghrsst.point_value falling back to nearest date %s (requested %s, Δ=%sd)",
            fallback,
            date,
            delta_days,
        )
        try:
            result = fetch(fallback)
            result["requested_date"] = date
            result["method"] = "nearest"
            return result
        except NotFoundError:
            raise NotFoundError(
                f"No data for {date}; nearest available date {fallback} is also unavailable."
            ) from exc


def ghrsst_bbox_mean(
    *,
    bbox: list[float],
    date: str,
    fields: list[Literal["sst", "sst_anomaly"]] | None = None,
    method: Literal["exact", "nearest"] = "exact",
) -> dict[str, Any]:
    """Compute mean SST/anomaly over a bbox for a single day (auto-downsample)."""

    logger.info(
        "ghrsst.bbox_mean invoked bbox=%s date=%s method=%s fields=%s",
        bbox,
        date,
        method,
        fields or ["sst"],
    )

    lon0, lat0, lon1, lat1 = _normalize_bbox(bbox)
    date = _validate_date(date)
    if method not in {"exact", "nearest"}:
        raise ToolError(f"Unsupported method '{method}'. Allowed: ['exact', 'nearest'].")
    chosen_fields = _unique_fields(fields)

    initial_sample = _estimate_stride(lon0, lat0, lon1, lat1)

    params_base: dict[str, Any] = {
        "lon0": lon0,
        "lat0": lat0,
        "lon1": lon1,
        "lat1": lat1,
        "append": ",".join(chosen_fields),
    }

    def fetch(target_date: str, stride: int) -> dict[str, Any]:
        params = dict(params_base)
        params["start"] = target_date
        params["sample"] = stride

        resp = _ghrsst_request(params)
        if resp.status_code == 400:
            detail = _extract_detail(resp)
            if "Too many points" in detail:
                raise ToolError("Too many points")
            if (
                "available date" in detail
                or "available range" in detail
                or "Data not exist" in detail
                or "BBOX query only allows single-day data" in detail
            ):
                raise NotFoundError(detail)
        _handle_common_errors(resp)

        try:
            payload = resp.json()
        except ValueError as exc:
            raise ToolError("Invalid JSON in GHRSST response.") from exc

        if not isinstance(payload, list) or not payload:
            raise NotFoundError("No data returned for requested bbox/date.")

        means: dict[str, float | None] = {}
        for field in chosen_fields:
            values = [row.get(field) for row in payload if row.get(field) is not None]
            if values:
                _ensure_sample(values, f"{field} values")
                means[field] = sum(values) / len(values)
            else:
                means[field] = None

        result: dict[str, Any] = {
            "region": [lon0, lat0, lon1, lat1],
            "date": target_date,
            "sample": stride,
        }
        if "sst" in chosen_fields:
            result["sst"] = means["sst"]
        if "sst_anomaly" in chosen_fields:
            result["sst_anomaly"] = means["sst_anomaly"]
        return result

    target_date = date
    sample = initial_sample

    while True:
        for attempt in range(MAX_SAMPLE_ATTEMPTS):
            try:
                result = fetch(target_date, sample)
            except ToolError as exc:
                if "Too many points" in str(exc) and attempt < MAX_SAMPLE_ATTEMPTS - 1:
                    sample *= 2
                    continue
                raise
            except NotFoundError as exc:
                if method == "nearest" and target_date == date:
                    range_dates = _parse_available_range(str(exc))
                    if range_dates:
                        fallback = _select_nearest_date(date, *range_dates)
                        if fallback != target_date:
                            delta_days = _days_between(date, fallback)
                            if delta_days > NEAREST_TOLERANCE_DAYS:
                                raise NotFoundError(
                                    f"No data for {date}; nearest available date {fallback} is {delta_days} days away (tolerance {NEAREST_TOLERANCE_DAYS} days)."
                                ) from exc
                            logger.info(
                                "ghrsst.bbox_mean falling back to nearest date %s (requested %s, Δ=%sd)",
                                fallback,
                                date,
                                delta_days,
                            )
                            target_date = fallback
                            sample = initial_sample
                            break
                raise
            else:
                if method == "nearest" and target_date != date:
                    result["requested_date"] = date
                    result["method"] = "nearest"
                return result
        else:
            # exhausted attempts without success
            raise ToolError("Unable to satisfy bbox request within point limit retries.")



app.add_tool(
    Tool.from_function(
        ghrsst_point_value,
        name="ghrsst.point_value",
        description="Get MUR (1-km) SST and/or anomaly at a single lon/lat for a single date.",
    )
)

app.add_tool(
    Tool.from_function(
        ghrsst_bbox_mean,
        name="ghrsst.bbox_mean",
        description=(
            "Compute mean MUR (1-km) SST and/or anomaly over a lon/lat bounding box "
            "for a single day. Automatically adjusts sample stride to respect point limits."
        ),
    )
)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the GHRSST MCP server.")
    parser.add_argument(
        "--transport",
        choices=["http", "stdio", "sse", "streamable-http"],
        default="http",
        help="Transport protocol to use (default: http).",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Host for HTTP transport (default: 0.0.0.0).")
    parser.add_argument("--port", type=int, default=8765, help="Port for HTTP transport.")
    parser.add_argument(
        "--path",
        default="/mcp/ghrsst",
        help="Base URL path when using HTTP transports (useful when reverse-proxied).",
    )
    args = parser.parse_args(argv)

    run_kwargs: dict[str, Any] = {}
    if args.transport in {"http", "sse", "streamable-http"}:
        run_kwargs.update({"host": args.host, "port": args.port, "path": args.path})
    app.run(transport=args.transport, **run_kwargs)


if __name__ == "__main__":  # pragma: no cover - manual invocation entrypoint
    main(sys.argv[1:])
