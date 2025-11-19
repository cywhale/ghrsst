# metocean-mcp v0.2.0 — Spec (delta)

## Server envelope (changed)

* **Server name/version:** `metocean-mcp` `0.2.0`
* **HTTP base path (default):** `/mcp/metocean`
* **User-Agent:** `metocean-mcp/0.2.0`
* **ENV (additions):**

  * `TIDE_API_BASE` default `https://eco.odb.ntu.edu.tw/api/tide/forecast`
* **Note:** GHRSST tools and their behavior are **unchanged** (see list below).

## Tools unchanged (do not modify)

* `ghrsst.point_value` — unchanged inputs/outputs/logic
* `ghrsst.bbox_mean` — unchanged inputs/outputs/logic

---

## NEW/UPDATED: `tide.forecast` (LLM-friendly + messages)

### Purpose

Return **one local day** of tide extremes (HIGH/LOW) and sun/moon events for a coordinate and date, and—if client time is supplied—report whether it is **rising** or **falling now**, plus countdowns to the next extreme.

### Inputs (JSON)

```json
{
  "longitude": 123.0,         // required, [-180, 180]
  "latitude":  23.0,          // required, [-90, 90]
  "date": "2025-11-12",       // optional; if omitted, derive from tz+query_time (or today in tz)
  "tz": "Asia/Taipei",        // REQUIRED for now-state; IANA name preferred; accepts "+08:00", 8, -5.5, "Z"
  "query_time": "2025-11-12T16:03:00+08:00"  // optional but recommended; ISO-8601 with offset
}
```

**Input rules**

* If `tz` is omitted → **400** with `detail: "timezone (tz) required (use IANA name or numeric offset)"`.
* If `date` is omitted → use the local calendar date derived from `tz` and `query_time`; if `query_time` absent, use “today” in `tz`.
* If `query_time` is omitted → response omits `state_now`/countdowns, but still returns the daily events.

### Backend call

* `GET {TIDE_API_BASE}?lon={lon}&lat={lat}&date={date}&tz={offset_or_name}`
* Tide API response shape:

  ```json
  {
    "meta": {
      "lon": 123,
      "lat": 23,
      "timezone": "+08:00",
      "reference": "TPXO9_atlas_v5 relative to MSL (NTDE 1983–2001)",
      "status": ""   // empty when USNO OK; non-empty error string when USNO fails
    },
    "days": [
      {
        "local_date": "2025-11-12T00:00:00+08:00",
        "moonphase": {
          "closestphase": { "time": "…", "phase": "…" },
          "curphase": "Waning Gibbous",
          "fracillum": "51%"
        },
        "moon": [
          { "phen": "Upper Transit", "time": "…" },
          { "phen": "Set",           "time": "…" },
          { "phen": "Rise",          "time": "…" }
        ],
        "sun": [
          { "phen": "Begin Civil Twilight", "time": "…" },
          { "phen": "Rise",                 "time": "…" },
          { "phen": "Upper Transit",        "time": "…" },
          { "phen": "Set",                  "time": "…" },
          { "phen": "End Civil Twilight",   "time": "…" }
        ],
        "tide": [
          { "phen": "LOW",  "time": "…", "height": -57.63 },
          { "phen": "HIGH", "time": "…", "height": 23.55 },
          { "phen": "LOW",  "time": "…", "height": -4.25 }
        ]
      }
    ]
  }
  ```

* Fields `sun`, `moon`, `moonphase` become `null` when USNO data is unavailable; `tide` is always populated.

### Output (JSON)

```json
{
  "region": [123.0, 23.0],
  "date": "2025-11-12",
  "timezone": "+08:00",                 // normalized offset used for the call
  "tz_name": "Asia/Taipei",             // optional echo if client sent IANA
  "reference": "TPXO9_atlas_v5 relative to MSL (NTDE 1983–2001)",

  "sun":  { "rise": "2025-11-12T06:02:00+08:00", "set": "2025-11-12T17:07:00+08:00" },   // may be omitted if USNO failed
  "moon": { "rise": "2025-11-12T23:49:00+08:00", "set": "2025-11-12T12:22:00+08:00" },   // may be omitted if USNO failed
  "moonphase": {
    "current": "Waning Gibbous",
    "illumination": "51%",
    "closest": { "time": "2025-11-12T13:28:00+08:00", "phase": "Last Quarter" }
  },                                                                                     // may be omitted if USNO failed

  "tide": {
    "highs": [ { "time": "2025-11-12T12:36:46+08:00", "height_cm": 23.55 } ],
    "lows":  [
      { "time": "2025-11-12T05:26:13+08:00", "height_cm": -57.63 },
      { "time": "2025-11-12T17:34:57+08:00", "height_cm": -4.25 }
    ],

    "state_now": "rising",               // present only if query_time provided and within this local date
    "since_extreme": "01:12:34",         // HH:MM:SS since last bracket extreme
    "until_extreme": "00:47:26",         // HH:MM:SS until next bracket extreme
    "last_extreme":  { "type": "LOW",  "time": "2025-11-12T15:50:00+08:00", "height_cm": -6.10 },
    "next_extreme":  { "type": "HIGH", "time": "2025-11-12T18:42:00+08:00", "height_cm": 22.90 }
  },

  "messages": [
    "Note: Tide heights are relative to NTDE 1983–2001 MSL and provided for reference only."
  ],

  "backend": {
    "usno_status": "",                    // empty string when OK; error text when USNO failed
    "tide_api_status": "OK"               // "OK" unless the Tide API itself had internal issues
  }
}
```

### “messages” policy (what to include and when)

1. **Tide API fails (5xx)**

   * The MCP returns an error (e.g., 502 with `detail: "Tide backend unavailable"`). No JSON body above.

2. **USNO fails but Tide API works**

   * Still return tide data.
   * Omit `sun`, `moon`, `moonphase` (or set them absent).
   * Include both messages:

     * `"Sun/Moon data unavailable at this time (USNO service error). Tide extrema are provided below."`
     * `"Note: Tide heights are relative to NTDE 1983–2001 MSL and provided for reference only."`
   * Set `backend.usno_status` to the error string from `meta.status`.

3. **All good**

   * Include:

     * `"Note: Tide heights are relative to NTDE 1983–2001 MSL and provided for reference only."`

4. **Timezone cannot be determined (client sent 'Z' or no tz)**

   * If client sends `"Z"` explicitly, proceed in UTC and add (at the **beginning** of `messages`):

     * `"Timezone not provided; times shown are in UTC (+00:00). Convert to your local time if needed."`
   * (If `tz` is missing entirely → the tool returns **400** as stated above.)

> Codex: keep `messages` as **an array of short, human-readable strings** ordered by importance (warning first, then general note). The LLM can directly paraphrase them.

### State & countdowns (same algorithm as previous spec)

* Determine bracketing LOW/HIGH around `query_time` (in the given `tz`); report `rising`/`falling` and durations as `HH:MM:SS`.
* If not bracketable within the day, omit `state_now` and countdowns.

### Errors

* **400** invalid inputs (e.g., missing `tz`, out-of-range lon/lat, bad date) with concise `detail`.
* **502/504** Tide API unreachable/timeout (`detail: "Tide backend unavailable"`).
* **500** unexpected backend payload.

---

## Implementation notes for Codex

* Keep existing GHRSST tools; only add `tide.forecast` and the new **server envelope changes** (name, path, UA).
* Normalize tz:

  * If IANA name is given, compute `timezone` as `±HH:MM` for the REST call, and echo both `tz_name` and `timezone` in output.
  * If numeric/offset is given, pass through to the REST call and echo as `timezone`.
* Map Tide API payload:

  * `meta.reference` → `reference`
  * `meta.status` → `backend.usno_status`
  * split `days[0].tide` by `phen` into `highs`/`lows` with `height_cm` (2 decimal places).
  * `sun`/`moon`/`moonphase` present only when available.
* Build `messages` per policy above. Keep them short; do not repeat raw backend errors—only place raw USNO error in `backend.usno_status`.

That’s it—this keeps the interface tidy for LLMs, signals when sun/moon are missing, and always carries the NTDE caution (plus UTC fallback notice when applicable).
