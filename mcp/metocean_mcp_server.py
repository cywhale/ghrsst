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
from datetime import date as date_cls, datetime, time as time_cls, timedelta, timezone
from typing import Any, Iterable, Literal, Sequence, Tuple

from zoneinfo import ZoneInfo

import httpx
from fastmcp.exceptions import NotFoundError, ToolError
from fastmcp.server import FastMCP
from fastmcp.tools import Tool

DEFAULT_GHRSST_API_BASE = "https://eco.odb.ntu.edu.tw/api/ghrsst"
DEFAULT_TIDE_API_BASE = "https://eco.odb.ntu.edu.tw/api/tide/forecast"
DEFAULT_TIMEOUT_S = 15.0
DEFAULT_POINT_LIMIT = 1_000_000
DEFAULT_DEG_PER_CELL = 0.01
DEFAULT_NEAREST_TOLERANCE_DAYS = 7
USER_AGENT = "metocean-mcp/0.2.0"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
INITIAL_BACKOFF = 0.75
MAX_SAMPLE_ATTEMPTS = 4
VALID_FIELDS = {"sst", "sst_anomaly"}
TIDE_NOTE = "Note: Tide heights are relative to NTDE 1983–2001 MSL and provided for reference only."


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


API_BASE = os.getenv("GHRSST_API_BASE", DEFAULT_GHRSST_API_BASE)
TIDE_API_BASE = os.getenv("TIDE_API_BASE", DEFAULT_TIDE_API_BASE)
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
    name="metocean-mcp",
    version="0.2.0",
    instructions=(
        "Provides curated GHRSST (SST) and tide forecast tools backed by ODB APIs. "
        "Use ghrsst.point_value / ghrsst.bbox_mean for SST data and tide.forecast for "
        "daily tide & sun/moon context."
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


def _parse_iso_datetime(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ToolError(f"Invalid query_time format: {value}") from exc


def _format_offset(td: timedelta) -> str:
    total_minutes = int(td.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    minutes = abs(total_minutes)
    hours_part = minutes // 60
    minutes_part = minutes % 60
    return f"{sign}{hours_part:02d}:{minutes_part:02d}"


def _offset_string_for_date(tzinfo: timezone | ZoneInfo, day: date_cls) -> str:
    probe = datetime(day.year, day.month, day.day, 12, tzinfo=tzinfo)
    offset = tzinfo.utcoffset(probe)
    if offset is None:
        raise ToolError("Unable to determine timezone offset.")
    return _format_offset(offset)


def _timezone_from_numeric(value: str) -> timezone:
    raw = value.strip()
    if not raw:
        raise ValueError
    sign = 1
    if raw[0] in "+-":
        sign = 1 if raw[0] == "+" else -1
        raw = raw[1:]
    hours = 0
    minutes = 0
    if raw.count(":") == 1:
        h, m = raw.split(":")
        hours = int(h)
        minutes = int(m)
    elif "." in raw:
        val = float(raw)
        hours = int(val)
        minutes = int(round((val - hours) * 60))
    else:
        hours = int(raw)
    total_minutes = sign * (hours * 60 + minutes)
    return timezone(timedelta(minutes=total_minutes))


def _parse_timezone_input(tz_value: str) -> tuple[timezone | ZoneInfo, str | None, bool]:
    if not tz_value:
        raise ToolError("timezone (tz) required (use IANA name or numeric offset)")
    raw = tz_value.strip()
    upper = raw.upper()
    if upper == "Z":
        return timezone.utc, None, True
    if upper in {"UTC", "GMT"}:
        return timezone.utc, raw, False
    if "/" in raw or raw.isalpha():
        try:
            return ZoneInfo(raw), raw, False
        except Exception as exc:
            raise ToolError(f"Unknown timezone '{raw}'.") from exc
    try:
        return _timezone_from_numeric(raw), None, False
    except Exception as exc:
        raise ToolError(f"Invalid timezone '{raw}'.") from exc


def _format_duration(delta: timedelta) -> str:
    total_seconds = int(delta.total_seconds())
    if total_seconds < 0:
        total_seconds = abs(total_seconds)
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


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


def _api_get(base_url: str, params: dict[str, Any]) -> httpx.Response:
    last_error: Any = None
    for attempt in range(MAX_RETRIES):
        try:
            resp = http_client.get(base_url, params=params, headers=DEFAULT_HEADERS)
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

        resp = _api_get(API_BASE, params)
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

        resp = _api_get(API_BASE, params)
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


def _build_tide_messages(usno_status: str, tz_note: bool) -> list[str]:
    messages: list[str] = []
    if tz_note:
        messages.append("Timezone not provided; times shown are in UTC (+00:00). Convert to your local time if needed.")
    if usno_status:
        messages.append("Sun/Moon data unavailable at this time (USNO service error). Tide extrema are provided below.")
    messages.append(TIDE_NOTE)
    return messages


def _map_tide_entry(entry: dict[str, Any]) -> dict[str, Any]:
    value = entry.get("height_cm")
    if value is None:
        value = entry.get("height")
    height = None
    if value is not None:
        try:
            height = round(float(value), 2)
        except (TypeError, ValueError):
            height = None
    return {"time": entry.get("time"), "height_cm": height, "phen": entry.get("phen")}


def _compute_tide_state(events: list[tuple[datetime, dict[str, Any]]], query_dt: datetime) -> dict[str, Any]:
    if not events:
        return {}
    events = sorted(events, key=lambda item: item[0])
    prev_evt: tuple[datetime, dict[str, Any]] | None = None
    next_evt: tuple[datetime, dict[str, Any]] | None = None
    for dt_val, payload in events:
        if dt_val <= query_dt:
            prev_evt = (dt_val, payload)
        if dt_val > query_dt:
            next_evt = (dt_val, payload)
            break
    if not prev_evt or not next_evt:
        return {}
    prev_type = prev_evt[1].get("phen")
    next_type = next_evt[1].get("phen")
    if prev_type == next_type:
        return {}
    if prev_type == "LOW" and next_type == "HIGH":
        state = "rising"
    elif prev_type == "HIGH" and next_type == "LOW":
        state = "falling"
    else:
        return {}
    since = _format_duration(query_dt - prev_evt[0])
    until = _format_duration(next_evt[0] - query_dt)
    return {
        "state_now": state,
        "since_extreme": since,
        "until_extreme": until,
        "last_extreme": {
            "type": prev_type,
            "time": prev_evt[1].get("time"),
            "height_cm": prev_evt[1].get("height_cm"),
        },
        "next_extreme": {
            "type": next_type,
            "time": next_evt[1].get("time"),
            "height_cm": next_evt[1].get("height_cm"),
        },
    }


def tide_forecast(
    *,
    longitude: float,
    latitude: float,
    tz: str,
    date: str | None = None,
    query_time: str | None = None,
) -> dict[str, Any]:
    """Return tide plus sun/moon info, with optional now-state."""

    logger.info(
        "tide.forecast invoked lon=%s lat=%s tz=%s date=%s query_time=%s",
        longitude,
        latitude,
        tz,
        date,
        query_time,
    )

    if not (-180 <= longitude <= 180 and -90 <= latitude <= 90):
        raise ToolError("Longitude/latitude out of bounds.")
    if not tz:
        raise ToolError("timezone (tz) required (use IANA name or numeric offset)")

    tzinfo_obj, tz_name, tz_note = _parse_timezone_input(tz)

    query_dt_local: datetime | None = None
    if query_time:
        qt = _parse_iso_datetime(query_time)
        if qt.tzinfo is None:
            qt = qt.replace(tzinfo=tzinfo_obj)
        query_dt_local = qt.astimezone(tzinfo_obj)

    if date:
        date_used = _validate_date(date)
        date_obj = datetime.strptime(date_used, "%Y-%m-%d").date()
    else:
        base_dt = query_dt_local or datetime.now(tzinfo_obj)
        date_obj = base_dt.date()
        date_used = date_obj.isoformat()

    offset_str = _offset_string_for_date(tzinfo_obj, date_obj)

    params = {
        "lon": longitude,
        "lat": latitude,
        "date": date_used,
        "tz": offset_str,
    }

    resp = _api_get(TIDE_API_BASE, params)
    if resp.status_code >= 500:
        raise ToolError("Tide backend unavailable")
    try:
        payload = resp.json()
    except ValueError as exc:
        raise ToolError("Invalid JSON from tide backend") from exc

    meta = payload.get("meta", {}) or {}
    days = payload.get("days") or []
    if not days:
        raise ToolError("Tide backend returned no data.")

    day_data = None
    for entry in days:
        local_date_str = entry.get("local_date")
        if not local_date_str:
            continue
        try:
            local_dt = _parse_iso_datetime(local_date_str)
        except ToolError:
            continue
        if local_dt.date().isoformat() == date_used:
            day_data = entry
            break
    if day_data is None:
        day_data = days[0]
        local_date_str = day_data.get("local_date")
        if local_date_str:
            try:
                local_dt = _parse_iso_datetime(local_date_str)
                date_obj = local_dt.date()
                date_used = date_obj.isoformat()
            except ToolError:
                pass

    tide_entries = day_data.get("tide") or []

    highs: list[dict[str, Any]] = []
    lows: list[dict[str, Any]] = []
    event_times: list[tuple[datetime, dict[str, Any]]] = []
    for raw in tide_entries:
        mapped = _map_tide_entry(raw)
        phen = mapped.get("phen")
        simplified = {"time": mapped.get("time"), "height_cm": mapped.get("height_cm")}
        if phen == "HIGH":
            highs.append(simplified)
        elif phen == "LOW":
            lows.append(simplified)
        ts = mapped.get("time")
        if ts:
            try:
                dt_val = _parse_iso_datetime(ts)
            except ToolError:
                continue
            if query_dt_local:
                dt_val = dt_val.astimezone(query_dt_local.tzinfo)
            event_times.append((dt_val, mapped))

    sun_events = day_data.get("sun") or []
    moon_events = day_data.get("moon") or []

    def _summarize_events(events: list[dict[str, Any]]) -> dict[str, Any]:
        summary: dict[str, Any] = {}
        for ev in events:
            phen = str(ev.get("phen") or "").lower()
            if phen == "rise" and "rise" not in summary:
                summary["rise"] = ev.get("time")
            elif phen == "set" and "set" not in summary:
                summary["set"] = ev.get("time")
            elif phen == "upper transit" and "transit" not in summary:
                summary["transit"] = ev.get("time")
            elif phen == "begin civil twilight" and "begin_civil_twilight" not in summary:
                summary["begin_civil_twilight"] = ev.get("time")
            elif phen == "end civil twilight" and "end_civil_twilight" not in summary:
                summary["end_civil_twilight"] = ev.get("time")
        return summary

    sun_data = _summarize_events(sun_events) if sun_events else None
    moon_data = _summarize_events(moon_events) if moon_events else None

    moonphase_raw = day_data.get("moonphase") or None
    moonphase = None
    if moonphase_raw:
        closest = moonphase_raw.get("closestphase") or {}
        moonphase = {
            "current": moonphase_raw.get("curphase"),
            "illumination": moonphase_raw.get("fracillum"),
            "closest": {
                "time": closest.get("time"),
                "phase": closest.get("phase"),
            },
        }

    usno_status = meta.get("status") or ""
    messages = _build_tide_messages(usno_status, tz_note)

    tide_output: dict[str, Any] = {"highs": highs, "lows": lows}

    if query_dt_local and query_dt_local.date() == date_obj:
        state_bits = _compute_tide_state(event_times, query_dt_local)
        tide_output.update(state_bits)

    timezone_str = meta.get("timezone") or offset_str

    result: dict[str, Any] = {
        "region": [longitude, latitude],
        "date": date_used,
        "timezone": timezone_str,
        "reference": meta.get("reference"),
        "messages": messages,
        "tide": tide_output,
        "backend": {
            "usno_status": usno_status,
            "tide_api_status": meta.get("tide_status", "OK"),
        },
    }

    if tz_name:
        result["tz_name"] = tz_name
    if sun_data:
        result["sun"] = sun_data
    if moon_data:
        result["moon"] = moon_data
    if moonphase:
        result["moonphase"] = moonphase

    return result


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

app.add_tool(
    Tool.from_function(
        tide_forecast,
        name="tide.forecast",
        description=(
            "Get one day of tide highs/lows plus sun/moon info for a location (includes optional now-state)."
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
        default="/mcp/metocean",
        help="Base URL path when using HTTP transports (useful when reverse-proxied).",
    )
    args = parser.parse_args(argv)

    run_kwargs: dict[str, Any] = {}
    if args.transport in {"http", "sse", "streamable-http"}:
        run_kwargs.update({"host": args.host, "port": args.port, "path": args.path})
    app.run(transport=args.transport, **run_kwargs)


if __name__ == "__main__":  # pragma: no cover - manual invocation entrypoint
    main(sys.argv[1:])
