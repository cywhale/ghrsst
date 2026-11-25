"""GHRSST Web API v1 — the published OpenAPI description (documentation only).

Scope (dev2026/specs/ghrsst_v1/02_openapi_v1_spec.md, owner Gate F1):
  * This module ONLY rewrites the generated OpenAPI document served at
    `app.openapi_url`. It never touches request parsing, validation, serialization,
    status codes, response headers, caching or data reads. Nothing here is imported
    by a request path; `install()` replaces `app.openapi` and nothing else.
  * It describes the behaviour of the handlers in `api/app.py` as they are. Where
    the handlers are looser or stricter than a schema can say, the schema documents
    the valid request and the handler stays authoritative (spec 02 §3).
  * Not-yet-decided public metadata (contact, license, termsOfService) is deliberately ABSENT —
    no placeholders (spec 02 §2, OD-11). `servers` is the owner-decided production host (OD-14).
  * Both operations get owner-decided stable `operationId`s and an operation-level `security: []`
    (public, no authentication). No security scheme and no root-level `security` are added.
    If FastAPI ever generates an authentication requirement, `apply_contract` raises
    `OpenApiSecurityConflict` instead of publishing `security: []` over it; anonymous-only `[{}]`
    is normalised to `security: []`.
  * Request caps (range length, points per batch, bbox cells, fan-out) are read from the
    deployment config (`cfg`) and documented as current request-handling limits, not
    guarantees (OD-18).
  * The two data-date windows (SPATIAL_WINDOW_DAYS, POINT_WINDOW_DAYS) are the owner-chosen v1
    contract direction (2026-11-20), NOT deployment values: they are fixed here and independent
    of `cfg`. The public wording is the owner-accepted concise form (round 12). Approved at Gate F2
    (2026-11-24) as OpenAPI contract v1.0.0.
  * POINT_COVERAGE_START is the CURRENT point archive start (verified read-only 2026-11-20),
    documented as "currently"; it is not a permanent start date and may change without a
    contract change.

Kept outside `app.py` on purpose: `app.py` is shared with the P5 data-flow work
(OD-15), so the API v1 track touches it with a single `install()` call.
"""
from __future__ import annotations

import copy
from typing import Any, Dict

GET_PATH = "/api/ghrsst"
POST_PATH = "/api/ghrsst/points"
DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
# Owner decision (OD-14): the production host. Paths already start with /api, so no /api here.
# URL only: clients (e.g. APIverse) build request URLs from the server object.
SERVERS = [{"url": "https://eco.odb.ntu.edu.tw"}]
# Owner decision: stable, unique operationIds (replace FastAPI's generated function-based ones).
OPERATION_IDS = {GET_PATH: ("get", "getGhrsstData"), POST_PATH: ("post", "postGhrsstPoints")}
PUBLIC_NO_AUTH = "This is a public API; no authentication is required."
# `$` also matches before a trailing newline in some regex dialects (Python's included), so the
# pattern alone would accept "2025-01-01\n", which the handler rejects with 400 (BC-1).
# maxLength pins the documented value to exactly YYYY-MM-DD.
DATE_MAX_LENGTH = 10
DATE_STRING_PATCH = {"pattern": DATE_PATTERN, "maxLength": DATE_MAX_LENGTH}
FIELDS = ["sst", "sst_anomaly", "sea_ice"]

INFORMATIONAL = ("Informational (observability): may change or disappear without a version "
                 "change; clients must not depend on it.")

# Owner decision 2026-11-20 (spec 04, OD-21): data-date windows of consecutive calendar days,
# ending on the latest date available to the query kind.
SPATIAL_WINDOW_DAYS = 31     # Bounding-Box Query (GET) + POST /points
POINT_WINDOW_DAYS = 366      # Point Query (GET)
# Current point coverage start (owner/Codex read-only check 2026-11-20: 2023-01-01..2026-11-18,
# every date present). Documented as current coverage only, never as a permanent start date.
POINT_COVERAGE_START = "2023-01-01"

NOT_A_QUOTA = ("This is a current request-handling limit for one request, not a rate limit, a "
               "quota or a data coverage window.")
NON_FINITE_422 = ("Non-finite numbers echoed in `input` are written as the strings \"NaN\", "
                  "\"Infinity\" or \"-Infinity\".")

DATA_SOURCE = (
    "Open API for daily 1-km GHRSST MUR v4.1 sea-surface temperature data.\n\n"
    "* Data source: MUR-JPL-L4-GLOB-v4.1, JPL MUR MEaSUREs Project. 2015. "
    "GHRSST Level 4 MUR Global Foundation Sea Surface Temperature Analysis, Ver. 4.1. "
    "PO.DAAC, CA, USA. https://doi.org/10.5067/GHGMR-4FJ04\n"
)

# Owner wording (2026-11-20): a typical lag, not a schedule. Internal ingest timing is not a promise.
FRESHNESS = ("The newest product date typically lags 1–2 days behind today, depending on upstream "
             "GHRSST data availability.")


SPATIAL_WINDOW_SPAN = (f"the latest {SPATIAL_WINDOW_DAYS} consecutive calendar days ending on the "
                       "latest date available to spatial queries")


def point_window_text() -> str:
    return (f"Point queries support the latest {POINT_WINDOW_DAYS} consecutive calendar days "
            "ending on the latest date available to point queries.")


def spatial_window_text() -> str:
    return f"Bounding-box queries support {SPATIAL_WINDOW_SPAN}."


def point_coverage_text() -> str:
    return f"The archive currently starts on {POINT_COVERAGE_START}."


# Example bodies. "Observed" ones were captured from the public service on 2026-11-19
# (spec 01 §4, §6, §8.6, §12); "illustrative" ones show the shape only. Dates in
# spatial examples go stale as the spatial window moves (OD-14) — they are not a promise
# that those days are served.
_OBSERVED_POINT_RANGE = [
    {"lon": 122.5, "lat": 23.5, "date": "2026-11-15", "sst": 26.621000289916992,
     "sst_anomaly": -0.07100000232458115, "sea_ice": None},
    {"lon": 122.5, "lat": 23.5, "date": "2026-11-16", "sst": 26.156999588012695,
     "sst_anomaly": -0.32899999618530273, "sea_ice": None},
]
_OBSERVED_POINTS_BATCH = [
    {"index": 0, "lon": 122.5, "lat": 23.5, "date": "2026-11-15", "sst": 26.621000289916992,
     "sea_ice": None},
    {"index": 1, "lon": 122.5, "lat": 23.5, "date": "2026-11-15", "sst": 26.621000289916992,
     "sea_ice": None},
    {"index": 2, "lon": -69.0, "lat": 32.0, "date": "2026-11-15", "sst": 25.138999938964844,
     "sea_ice": None},
]
_OBSERVED_SPATIAL_WINDOW_ERROR = {"detail": {
    "error": "spatial query outside the available window",
    "requested_day": "2024-06-01",
    "spatial_window_days": 31,
    "available_spatial_window": "2026-09-04..2026-11-17",
    "hint": ("bbox / POST points are limited to the recent spatial window "
             "(2026-09-04..2026-11-17); use point time-series / single-day point for older days"),
}}


def _ref(name: str) -> Dict[str, str]:
    return {"$ref": f"#/components/schemas/{name}"}


def _value_field(desc: str) -> Dict[str, Any]:
    return {"type": ["number", "null"], "description": desc}


def component_schemas(cfg) -> Dict[str, Any]:
    """The v1 component schemas (spec 02 §3–§4). FastAPI's own `HTTPValidationError` /
    `ValidationError` (422) are NOT redefined here; they stay exactly as generated."""
    return {
        "Date": {
            "type": "string",
            "pattern": DATE_PATTERN,
            "maxLength": DATE_MAX_LENGTH,
            "description": "Product date of the MUR analysis, YYYY-MM-DD; must be a real calendar date.",
        },
        "FieldName": {"type": "string", "enum": list(FIELDS)},
        "DataRow": {
            "type": "object",
            "required": ["lon", "lat", "date"],
            "properties": {
                "lon": {"type": "number",
                        "description": "Longitude of the nearest grid cell (not the requested value)."},
                "lat": {"type": "number", "description": "Latitude of the nearest grid cell."},
                "date": _ref("Date"),
                "sst": _value_field("Sea-surface temperature, °C."),
                "sst_anomaly": _value_field("SST anomaly, °C."),
                "sea_ice": _value_field("Sea-ice fraction, 0–1."),
            },
            "additionalProperties": False,
            "description": (
                "One row of GET /api/ghrsst. Only requested variables appear. A variable with no "
                "value at that cell (for example land) is null. A variable that does not exist for "
                "that date is left out of point rows and is null in bounding-box rows."),
        },
        "PointsBatchRow": {
            "type": "object",
            "required": ["index", "lon", "lat", "date"],
            "properties": {
                "index": {"type": "integer", "minimum": 0,
                          "description": ("Zero-based position of this point in the request "
                                          "`points` array; use it to match the row to its input.")},
                "lon": {"type": "number", "description": "Longitude of the nearest grid cell."},
                "lat": {"type": "number", "description": "Latitude of the nearest grid cell."},
                "date": _ref("Date"),
                "sst": _value_field("Sea-surface temperature, °C."),
                "sst_anomaly": _value_field("SST anomaly, °C."),
                "sea_ice": _value_field("Sea-ice fraction, 0–1."),
            },
            "additionalProperties": False,
            "description": ("One row of POST /api/ghrsst/points. Every requested variable is present; "
                            "it is null when the variable does not exist for that date or has no value."),
        },
        "PointsRequest": {
            "title": "PointsRequest",
            "type": "object",
            "required": ["date", "points"],
            "properties": {
                "date": _ref("Date"),
                "points": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": cfg.POINTS_BATCH_MAX,
                    "description": (f"[lon, lat] pairs, at least 1 and at most {cfg.POINTS_BATCH_MAX} "
                                    f"per request (current request-handling limit)."),
                    "items": {
                        "type": "array",
                        "prefixItems": [{"type": "number", "description": "lon"},
                                        {"type": "number", "description": "lat"}],
                        "minItems": 2,
                        "maxItems": 2,
                    },
                },
                "append": {"type": ["string", "null"],
                           "default": "sst",     # the effective default; not in `required`
                           "description": ("Variables to return, comma-separated: sst, sst_anomaly, "
                                           "sea_ice. Default: sst.")},
                "mode": {"type": ["string", "null"],
                         "description": ("Optional. `truncate` rounds coordinates to 5 and values to "
                                         "3 decimals.")},
            },
        },
        "ErrorMessage": {
            "type": "object",
            "required": ["detail"],
            "properties": {"detail": {"type": "string"}},
        },
        "RangeTooLarge": {
            "type": "object",
            "required": ["detail"],
            "properties": {"detail": {
                "type": "object",
                "required": ["error", "max_days", "requested_days", "hint"],
                "properties": {
                    "error": {"const": "requested range too large"},
                    "max_days": {"type": "integer"},
                    "requested_days": {"type": "integer"},
                    "hint": {"type": "string"},
                },
            }},
        },
        "TooManyPoints": {
            "type": "object",
            "required": ["detail"],
            "properties": {"detail": {
                "type": "object",
                "required": ["error", "max_points", "requested_points", "hint"],
                "properties": {
                    "error": {"const": "too many points"},
                    "max_points": {"type": "integer"},
                    "requested_points": {"type": "integer"},
                    "hint": {"type": "string"},
                },
            }},
        },
        "SpatialWindowError": {
            "type": "object",
            "required": ["detail"],
            "properties": {"detail": {
                "type": "object",
                "required": ["error", "requested_day", "spatial_window_days",
                             "available_spatial_window", "hint"],
                "properties": {
                    "error": {"const": "spatial query outside the available window"},
                    "requested_day": {"type": "string",
                                      "description": "The requested day, echoed as sent."},
                    "spatial_window_days": {
                        "type": "integer",
                        "description": ("Retention target of the recent spatial data, in days. It "
                                        "is not the length of the window currently served, which "
                                        "may be longer; see available_spatial_window."),
                    },
                    "available_spatial_window": {
                        "type": "string",
                        "description": ("First and last date of the spatial data currently served, "
                                        "as \"YYYY-MM-DD..YYYY-MM-DD\", or \"(none)\"."),
                    },
                    "hint": {"type": "string"},
                },
            }},
        },
    }


# ---- parameters -----------------------------------------------------------------------
def _param_docs(cfg) -> Dict[str, Dict[str, Any]]:
    """name -> {description, schema_patch}. The patch only adds documentation keywords.
    Rules shared by several parameters (finite coordinates, clamping, date windows) live once in
    the operation description; status codes live in the responses."""
    return {
        "lon0": {"description": "Longitude of the point, or of one bounding-box corner (-180 to 180)."},
        "lat0": {"description": "Latitude of the point, or of one bounding-box corner (-90 to 90)."},
        "lon1": {"description": ("Longitude of the opposite bounding-box corner; give it together with "
                                 "`lat1`. Boxes crossing the antimeridian are not supported.")},
        "lat1": {"description": ("Latitude of the opposite bounding-box corner; give it together with "
                                 "`lon1`.")},
        "start": {
            "description": ("Date (YYYY-MM-DD). Point Query: a single date, or one end of a range "
                            "together with `end`. Bounding-Box Query: the query date."),
            "string_patch": DATE_STRING_PATCH,
        },
        "end": {
            "description": ("Date (YYYY-MM-DD). Point Query: a single date, or the other end of a "
                            "range together with `start`. Bounding-Box Query: the query date; if "
                            "`start` is also given, it must be the same date."),
            "string_patch": DATE_STRING_PATCH,
        },
        "append": {
            "description": "Variables to return, comma-separated: sst, sst_anomaly, sea_ice. Default: sst.",
            # Documents the effective default (omitted -> sst); the parameter stays optional.
            "schema_patch": {"default": "sst"},
        },
        "sample": {
            "description": ("Bounding box only: keep every Nth grid cell along each axis (N of 1 or "
                            "more). Default 1."),
            "schema_patch": {"minimum": 1},
        },
        "mode": {"description": "Optional. `truncate` rounds coordinates to 5 and values to 3 decimals."},
        "format": {
            # No `enum`: the handler trims and lower-cases the value, so `JSON` / ` json ` are valid.
            "description": "Only JSON is supported (case-insensitive). Omitted or empty means JSON.",
        },
    }


def _patch_parameters(op: Dict[str, Any], cfg) -> None:
    docs = _param_docs(cfg)
    for p in op.get("parameters", []):
        d = docs.get(p.get("name"))
        if d is None:
            continue
        p["description"] = d["description"]
        schema = p.setdefault("schema", {})
        schema["description"] = d["description"]
        schema.update(d.get("schema_patch", {}))
        if "string_patch" in d:
            for branch in schema.get("anyOf", []):
                if branch.get("type") == "string":
                    branch.update(d["string_patch"])


# ---- responses -------------------------------------------------------------------------
def _cache_control_header(cfg) -> Dict[str, Any]:
    return {
        "required": True,
        "description": (
            "`no-store` when the result can still change (no date given; a point range reaching "
            "the newest date; a bounding box without a date or on the newest date). Otherwise "
            f"`public, max-age=<seconds>`; the seconds are a deployment value (currently "
            f"{cfg.LONG_CACHE_SECONDS})."),
        "schema": {"type": "string"},
    }


def _retry_after_header(only_overload_note: str) -> Dict[str, Any]:
    return {
        "required": False,
        "description": ("Seconds to wait before retrying. Sent ONLY when the 503 is caused by "
                        f"overload (currently 1). {only_overload_note}"),
        "schema": {"type": "integer"},
    }


def _json(schema: Dict[str, Any], examples: Dict[str, Any]) -> Dict[str, Any]:
    return {"application/json": {"schema": schema, "examples": examples}}


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def _bullets(title: str, items) -> str:
    return "\n".join([title, ""] + [f"* {i}" for i in items])


def _get_responses(cfg) -> Dict[str, Any]:
    max_days = cfg.MAX_DAYS
    return {
        "200": {
            "description": (
                "JSON array of DataRow. Point Query: one row per available date in the requested "
                "range, oldest first. Bounding-Box Query: one row per grid cell, south to north and "
                "then west to east; the body is streamed but is a single JSON array."),
            "headers": {
                "Cache-Control": _cache_control_header(cfg),
                "X-Store-Route": {"required": False, "schema": {"type": "string",
                                                                "enum": ["cube", "daily", "mixed"]},
                                  "description": f"Point queries only. {INFORMATIONAL}"},
                "X-Served-Rows": {"required": False, "schema": {"type": "integer"},
                                  "description": "Bounding box only: number of rows (grid cells) in the body."},
                "X-Stride": {"required": False, "schema": {"type": "integer"},
                             "description": "Bounding box only: the `sample` stride applied."},
                "X-Bbox-Format": {"required": False, "schema": {"type": "string", "enum": ["json"]},
                                  "description": "Bounding box only: the body format."},
                "X-Read-Ms": {"required": False, "schema": {"type": "number"},
                              "description": f"Bounding box only: read time in ms. {INFORMATIONAL}"},
            },
            "content": _json({"type": "array", "items": _ref("DataRow")}, {
                "point_range": {
                    "summary": "Point time series (observed 2026-11-19; dates are illustrative)",
                    "description": ("?lon0=122.5&lat0=23.5&start=2026-11-15&end=2026-11-16"
                                    "&append=sst,sst_anomaly,sea_ice"),
                    "value": _OBSERVED_POINT_RANGE,
                },
                "point_field_absent": {
                    "summary": "Point: a variable that does not exist for that date is left out (illustrative)",
                    "value": [{"lon": 122.5, "lat": 23.5, "date": "2026-11-15", "sst": 26.621}],
                },
                "bbox": {
                    "summary": "Bounding box: missing or empty values are null (illustrative)",
                    "value": [
                        {"lon": 121.0, "lat": 24.0, "date": "2026-11-15", "sst": None, "sst_anomaly": None},
                        {"lon": 121.01000213623047, "lat": 24.0, "date": "2026-11-15", "sst": 25.9,
                         "sst_anomaly": None},
                    ],
                },
            }),
        },
        "400": {
            "description": _bullets(
                "Bad request. `detail` is a message string, except for SpatialWindowError, whose "
                "`detail` is an object. Causes:", [
                    "a date that is malformed or not a real calendar date;",
                    "an unsupported variable (`append`), `mode` or `format`;",
                    "`sample` other than 1, or a format other than JSON, in a point query; `sample` below 1;",
                    "a bounding box with two different dates;",
                    "no data in the requested point date range;",
                    f"a bounding box larger than {cfg.BBOX_POINT_LIMIT} grid cells after `sample` "
                    f"thinning; increase `sample` or shrink the box. {NOT_A_QUOTA}",
                    "a bounding-box date that is not available, or that is outside the spatial data "
                    "currently served (SpatialWindowError). Its `available_spatial_window` gives the "
                    "dates currently served; `spatial_window_days` is a retention target, not the "
                    "current window length.",
                ]),
            "content": _json({"oneOf": [_ref("ErrorMessage"), _ref("SpatialWindowError")]}, {
                "invalid_date": {"summary": "Malformed or non-existent date (2025/01/01, 2025-02-30)",
                                 "value": {"detail": "Invalid date format. Use YYYY-MM-DD."}},
                "unsupported_field": {"value": {"detail": ("Unsupported field(s): foo. Allowed: "
                                                           "sst,sst_anomaly,sea_ice")}},
                "bbox_too_large": {"summary": "Box larger than the current cell limit",
                                   "value": {"detail": (f"bbox too large (16008001 > "
                                                        f"{cfg.BBOX_POINT_LIMIT}); increase "
                                                        f"stride or shrink bbox")}},
                "spatial_window": {"summary": "Observed 2026-11-19 (window dates go stale)",
                                   "value": _OBSERVED_SPATIAL_WINDOW_ERROR},
            }),
        },
        "413": {
            "description": (
                f"Point range too long: more than {max_days} inclusive calendar dates in one request, "
                f"counted before the range is trimmed to available data. {NOT_A_QUOTA} Split the "
                f"request into shorter ranges. `detail` is a RangeTooLarge object."),
            "content": _json(_ref("RangeTooLarge"), {
                "range_too_large": {"value": {"detail": {
                    "error": "requested range too large", "max_days": max_days,
                    "requested_days": 731, "hint": f"split into ranges of <= {max_days} days"}}},
            }),
        },
        "422": {
            "description": ("Validation error (FastAPI format, `detail` is a list): a required "
                            "parameter is missing, a value has the wrong type, or a coordinate is "
                            "not a finite number."),
        },
        "503": {
            "description": (
                "Service unavailable. Either no dates are available (no Retry-After), or the server "
                "is overloaded (Retry-After present). Clients should only rely on Retry-After when "
                "it is sent."),
            "headers": {"Retry-After": _retry_after_header(
                "The two no-available-dates cases do not send it.")},
            "content": _json(_ref("ErrorMessage"), {
                "no_available_dates": {"summary": "Point query, no data at all (no Retry-After)",
                                       "value": {"detail": "No available dates."}},
                "no_available_spatial_dates": {"summary": "Bounding box, no daily data (no Retry-After)",
                                               "value": {"detail": "No available dates for spatial queries."}},
                "overloaded": {"summary": "Overload (sent with Retry-After: 1)",
                               "value": {"detail": "server overloaded; retry later"}},
            }),
        },
    }


def _post_responses(cfg) -> Dict[str, Any]:
    max_pts = cfg.POINTS_BATCH_MAX
    fanout = cfg.BATCH_CHUNK_FANOUT_MAX
    return {
        "200": {
            "description": ("JSON array of PointsBatchRow: one row per input point, in the same order "
                            "as `points`. Duplicate points give duplicate rows; `index` is the "
                            "zero-based position in `points`."),
            "headers": {"Cache-Control": {"required": True, "schema": {"type": "string"},
                                          "description": "Always `no-store`."}},
            "content": _json({"type": "array", "items": _ref("PointsBatchRow")}, {
                "batch": {"summary": "Observed 2026-11-19 (append=sst,sea_ice; dates are illustrative)",
                          "value": _OBSERVED_POINTS_BATCH},
            }),
        },
        "400": {
            "description": _bullets(
                "Bad request. `detail` is a message string, except for SpatialWindowError, whose "
                "`detail` is an object. Causes:", [
                    "a date that is malformed or not a real calendar date;",
                    "an unsupported variable (`append`) or `mode`;",
                    "an empty `points` list, or a point that is not exactly [lon, lat];",
                    "a date that is outside the spatial data currently served (SpatialWindowError; "
                    "`spatial_window_days` is a retention target, not the current window length), "
                    "or not available.",
                ]),
            "content": _json({"oneOf": [_ref("ErrorMessage"), _ref("SpatialWindowError")]}, {
                "invalid_date": {"summary": "Malformed or non-existent date (2025/01/01, 2025-02-30)",
                                 "value": {"detail": "Invalid date format. Use YYYY-MM-DD."}},
                "empty_points": {"value": {"detail": "points must be a non-empty list of [lon,lat]."}},
                "bad_point": {"value": {"detail": "each point must be [lon, lat]."}},
                "spatial_window": {"summary": "Observed 2026-11-19 (window dates go stale)",
                                   "value": {"detail": dict(_OBSERVED_SPATIAL_WINDOW_ERROR["detail"],
                                                            requested_day="2020-01-01")}},
            }),
        },
        "413": {
            "description": _bullets(
                f"Request too large. {NOT_A_QUOTA} Causes:", [
                    f"more than {max_pts} points (`detail` is a TooManyPoints object); split them "
                    f"into batches of at most {max_pts};",
                    f"points spread too widely: they fall in more than {fanout} grid blocks of the "
                    f"daily data (current deployment value; `detail` is a string). Split the points "
                    f"into smaller batches by geographic area, so each batch covers a compact region.",
                ]),
            "content": _json({"oneOf": [_ref("TooManyPoints"), _ref("ErrorMessage")]}, {
                "too_many_points": {"value": {"detail": {
                    "error": "too many points", "max_points": max_pts, "requested_points": max_pts + 1,
                    "hint": f"split into batches of <= {max_pts} points"}}},
                "fan_out": {"summary": ("Points cover too many areas: split them into smaller "
                                        "batches of points that are close together"),
                            "value": {"detail": (f"batch spans {fanout + 1} chunks > fan-out limit "
                                                 f"{fanout}; shrink/split the request")}},
            }),
        },
        "422": {
            "description": ("Validation error (FastAPI format, `detail` is a list): a required field "
                            "is missing, a value has the wrong type, or a coordinate is not a finite "
                            f"number. {NON_FINITE_422}"),
        },
        "503": {
            "description": ("Server overloaded; retry after Retry-After seconds. This is the only "
                            "503 of this operation (an unavailable date is a 400)."),
            "headers": {"Retry-After": _retry_after_header(
                "Overload is the only 503 of this operation, so it is currently always sent.")},
            "content": _json(_ref("ErrorMessage"), {
                "overloaded": {"value": {"detail": "server overloaded; retry later"}},
            }),
        },
    }


# ---- operations ------------------------------------------------------------------------
COORDINATE_RULE = ("must be finite numbers; NaN, Infinity and -Infinity are rejected as "
                   "validation errors. Finite values outside the grid are clamped to the nearest "
                   "grid cell, and each row reports that cell's coordinates.")


def _get_description(cfg) -> str:
    return "\n".join([
        "Query daily sea-surface temperature at a single point (one date or a date range) or over "
        f"a bounding box (one date). {PUBLIC_NO_AUTH}",
        "",
        f"**Coordinates** {COORDINATE_RULE}",
        "",
        "**Query type** The request is a Bounding-Box Query when both `lon1` and `lat1` are given "
        "and (`lon1`, `lat1`) differs from (`lon0`, `lat0`); otherwise it is a Point Query.",
        "",
        _bullets("**Point Query**", [
            "Dates: without `start` and `end`, the latest date available to point queries is used. "
            "With only one of them, that single date. With both, every date in the inclusive range; "
            "the two dates may be given in either order.",
            f"Coverage: {_lower_first(point_window_text())} {point_coverage_text()}",
            f"Request limit: one request may cover at most {cfg.MAX_DAYS} inclusive calendar dates "
            "(current request-handling limit, not a quota). This limits the length of one request, "
            "not the coverage window above; split longer periods into several requests.",
        ]),
        "",
        _bullets("**Bounding-Box Query**", [
            "Dates: one date per request. Give `start` or `end` alone, or both with the same date. "
            "Without a date, the newest date in the daily data is used; if that date is not yet "
            "available to spatial queries, the request fails instead of falling back to an earlier "
            "date.",
            f"Coverage: {_lower_first(spatial_window_text())} The same window applies to "
            "`POST /api/ghrsst/points`.",
            f"Request limit: use `sample` to thin large areas. One request may return at most "
            f"{cfg.BBOX_POINT_LIMIT} grid cells after thinning (current request-handling limit, not "
            "a quota).",
            "In Swagger UI, keep boxes small to avoid large browser responses.",
        ]),
        "",
        f"**Data freshness** {FRESHNESS} Omit date parameters to request the latest available "
        "data. Dates in the examples are illustrative.",
    ])


def _post_description(cfg) -> str:
    return "\n".join([
        f"Query many points for one date in a single request. {PUBLIC_NO_AUTH}",
        "",
        _bullets("**Points**", [
            "Send a `date` and a list of [lon, lat] `points`. The response has one row per point, in "
            "the same order; duplicate points are kept.",
            "Each row's `index` is the zero-based position of its point in `points`, so rows can be "
            "matched back to the input.",
            "Every requested variable appears in every row; it is null when the variable does not "
            "exist for that date or has no value.",
            f"At most {cfg.POINTS_BATCH_MAX} points per request (current request-handling limit, "
            "not a quota). Points spread over a very large area may be rejected; split them into "
            "smaller batches by geographic area.",
        ]),
        "",
        _bullets("**Date**", [
            f"Coverage: this endpoint supports {SPATIAL_WINDOW_SPAN}. This is the same window as a "
            "Bounding-Box Query on `GET /api/ghrsst`.",
            f"{FRESHNESS} The example date is illustrative; choose a date inside this window.",
        ]),
        "",
        f"**Coordinates** {COORDINATE_RULE}",
    ])


def _merge_responses(op: Dict[str, Any], documented: Dict[str, Any]) -> None:
    """Add/replace documented responses. For 422 only the description is replaced: the content
    stays FastAPI's own `$ref HTTPValidationError` (no new error format)."""
    responses = op.setdefault("responses", {})
    for status, spec in documented.items():
        if status == "422" and status in responses:
            responses[status]["description"] = spec["description"]
        else:
            responses[status] = spec


class OpenApiSecurityConflict(RuntimeError):
    """The generated OpenAPI declares an authentication requirement, so the contract must not
    publish the owner-decided public `security: []` over it."""


def _anonymous_only(requirement: Any) -> bool:
    """True for `[]` or a list of only empty Security Requirement Objects (`[{}]`): OAS 3.1.1
    §4.8.30 — an empty object means anonymous access is supported, so `[{}]` alone means anonymous
    access suffices. Any named scheme (e.g. `[{}, {"bearerAuth": []}]`) or a malformed value is not."""
    return isinstance(requirement, list) and all(isinstance(r, dict) and not r for r in requirement)


def _check_no_generated_security(doc: Dict[str, Any]) -> None:
    """Refuse (raise) when FastAPI generated any authentication requirement or a malformed
    security value. Checked by key presence and type, never by truthiness:
      * root / target-operation `security`: absent, or anonymous-only (`[]`, `[{}]`) — anything
        else (a named scheme, alone or alongside `{}`; `null`; a non-list) is refused;
      * `components.securitySchemes`: absent, or an empty object — anything else (a defined
        scheme; `null`; `[]` or another non-object) is refused.
    Accepted values are normalised by `apply_contract` (operation `security: []`, root `security`
    and an empty `securitySchemes` removed). Nothing is deleted or overwritten here; this only
    keeps the published description honest."""
    found = []
    if "security" in doc and not _anonymous_only(doc["security"]):
        found.append(f"root security={doc['security']!r}")
    components = doc.get("components", {})
    if not isinstance(components, dict):
        found.append(f"components={components!r}")
    elif "securitySchemes" in components and components["securitySchemes"] != {}:
        found.append(f"components.securitySchemes={components['securitySchemes']!r}")
    for path, (method, _) in OPERATION_IDS.items():
        op = doc.get("paths", {}).get(path, {}).get(method)
        if op is not None and "security" in op and not _anonymous_only(op["security"]):
            found.append(f"{method.upper()} {path} security={op['security']!r}")
    if found:
        raise OpenApiSecurityConflict(
            "generated OpenAPI declares authentication or a malformed security value; refusing to "
            "publish `security: []` (public, no authentication) over it: " + "; ".join(found))


def apply_contract(doc: Dict[str, Any], cfg) -> Dict[str, Any]:
    """Rewrite a FastAPI-generated OpenAPI dict in place into the v1 description.

    Raises OpenApiSecurityConflict (before changing anything) if the generated document declares
    an authentication requirement."""
    _check_no_generated_security(doc)
    info = doc.setdefault("info", {})
    info["description"] = DATA_SOURCE    # concise; rules live with their endpoints and responses
    for key in ("contact", "license", "termsOfService"):
        info.pop(key, None)              # OD-11: never publish placeholders
    doc["servers"] = copy.deepcopy(SERVERS)   # OD-14 (owner decision)
    doc.pop("security", None)            # only anonymous-only root `[]` / `[{}]` can reach here

    components = doc.setdefault("components", {})
    components.pop("securitySchemes", None)   # only an empty `{}` can reach here
    schemas = components.setdefault("schemas", {})
    schemas.update(component_schemas(cfg))

    paths = doc.get("paths", {})
    for path, (method, op_id) in OPERATION_IDS.items():
        op = paths.get(path, {}).get(method)
        if op is not None:
            op["operationId"] = op_id
            op["security"] = []          # explicit: no authentication for this operation
    get_op = paths.get(GET_PATH, {}).get("get")
    if get_op is not None:
        get_op["summary"] = "Point Query or Bounding-Box Query"
        get_op["description"] = _get_description(cfg)
        _patch_parameters(get_op, cfg)
        _merge_responses(get_op, _get_responses(cfg))
    post_op = paths.get(POST_PATH, {}).get("post")
    if post_op is not None:
        post_op["summary"] = "Batch point query for one day"
        post_op["description"] = _post_description(cfg)
        body = post_op.get("requestBody", {}).get("content", {}).get("application/json")
        if body is not None:
            body["examples"] = {"batch": {
                "summary": ("Illustrative date: use a date inside the "
                            f"{SPATIAL_WINDOW_DAYS}-day spatial window"),
                "value": {"date": "2026-11-15", "points": [[122.5, 23.5], [-69.0, 32.0]],
                          "append": "sst,sst_anomaly"},
            }}
        _merge_responses(post_op, _post_responses(cfg))
    return doc


CACHE_ATTR = "_ghrsst_v1_openapi"


def install(app, cfg) -> None:
    """Serve the v1 description from `app.openapi()` (documentation only).

    FastAPI's own generator and its `app.openapi_schema` cache are left as they are (the raw
    generated document); the rewritten copy is cached separately in `CACHE_ATTR`, so clearing that
    cache can never apply the contract twice."""
    generate = app.openapi            # FastAPI's bound generator (caches the RAW document)
    setattr(app, CACHE_ATTR, None)

    def openapi() -> Dict[str, Any]:
        if getattr(app, CACHE_ATTR, None) is None:
            setattr(app, CACHE_ATTR, apply_contract(copy.deepcopy(generate()), cfg))
        return getattr(app, CACHE_ATTR)

    app.openapi = openapi
