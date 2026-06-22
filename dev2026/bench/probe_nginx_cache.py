"""dev2026 — NGINX cache behaviour probe for the GHRSST API.

Validates the cache concerns raised in specs/01_refactor_spec.md P1-S6 against a
LIVE NGINX-fronted endpoint, using the existing `X-api-cache` response header
(`add_header X-api-cache $upstream_cache_status;`). Works two ways:

  * PRE-FIX  : run against current production to *demonstrate* the issues.
  * POST-FIX : run against staging/new-app to *gate* that they are gone
               (exit code != 0 if any correctness problem is detected).

Checks
  [GET-1]  fixed-date point GET is cacheable      -> MISS then HIT (informational)
  [GET-2]  default-latest (no date) GET is cached -> MISS then HIT == 10d stale-latest RISK
  [GET-3]  two distinct query strings don't share -> distinct entries (no cross-serve)
  [POST-1] POST /points endpoint present?         -> 200 vs 404/405
  [POST-2] POST responses get cached at all       -> identical body twice: MISS then HIT
  [POST-3] CACHE-KEY COLLISION (the landmine)     -> two DIFFERENT bodies, same URI:
           does request B get B's data, or A's?   FAIL if B is served A's cached result.

No hardcoded host. Base URL from --base or GHRSST_API_BASE. Read-only (GET + a
query-only POST). It DOES hit a live server — the banner says which.

Run:
  export GHRSST_API_BASE=https://eco.odb.ntu.edu.tw/api/ghrsst
  dev2026/.venv/bin/python dev2026/bench/probe_nginx_cache.py
  # or:  dev2026/.venv/bin/python dev2026/bench/probe_nginx_cache.py --base https://staging.../api/ghrsst
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

# Two geographically DISTANT point sets so a collision is unmistakable.
POINTS_A = [[119.30672, 22.28274], [121.0, 23.5]]   # around Taiwan
POINTS_B = [[-157.85, 21.30], [-156.5, 20.8]]        # around Hawaii
NEAR_DEG = 0.05  # returned nearest-grid coord must be within this of the request


def _req(method, url, body=None, timeout=30):
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            hdr = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, hdr, raw
    except urllib.error.HTTPError as e:
        hdr = {k.lower(): v for k, v in (e.headers or {}).items()}
        return e.code, hdr, e.read()


def _cache(hdr):
    return hdr.get("x-api-cache", "<no X-api-cache header>")


def _json(raw):
    try:
        return json.loads(raw)
    except Exception:
        return None


def _rows_match_points(rows, points):
    """True if response rows correspond (by nearest-grid coords) to `points`."""
    if not isinstance(rows, list) or len(rows) != len(points):
        return False
    for row, (lon, lat) in zip(rows, points):
        try:
            if abs(float(row["lon"]) - lon) > NEAR_DEG or abs(float(row["lat"]) - lat) > NEAR_DEG:
                return False
        except (KeyError, TypeError, ValueError):
            return False
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("GHRSST_API_BASE"),
                    help="API base, e.g. https://eco.odb.ntu.edu.tw/api/ghrsst (or GHRSST_API_BASE)")
    ap.add_argument("--date", default=None, help="a fixed date present in the store (YYYY-MM-DD)")
    ap.add_argument("--lon", type=float, default=119.30672)
    ap.add_argument("--lat", type=float, default=22.28274)
    ap.add_argument("--strict", "--require-post", dest="strict", action="store_true",
                    help="staging/cutover gate: missing /points, missing date, non-200, or "
                         "inconclusive POST-3 are FAILURES (exit 1), not skips.")
    args = ap.parse_args()

    if not args.base:
        raise SystemExit("Set --base or GHRSST_API_BASE (no hardcoded host).")
    base = args.base.rstrip("/")
    points_url = base + "/points"

    print("=" * 70)
    print(f"NGINX cache probe — TARGET (live server): {base}")
    print("read-only: GET + query-only POST. Uses X-api-cache header.")
    print("=" * 70)

    problems = []   # correctness failures (gate)
    risks = []      # stale-data risks worth flagging

    # ---- [GET-1] fixed-date point cacheable -------------------------------
    if args.date:
        q = urllib.parse.urlencode({"lon0": args.lon, "lat0": args.lat,
                                    "append": "sst", "start": args.date})
        u = f"{base}?{q}"
        s1, h1, _ = _req("GET", u)
        s2, h2, _ = _req("GET", u)
        print(f"\n[GET-1] fixed-date point  ({args.date})")
        print(f"        call#1 status={s1} X-api-cache={_cache(h1)}")
        print(f"        call#2 status={s2} X-api-cache={_cache(h2)}  (expect HIT => cacheable, harmless for fixed date)")
    else:
        print("\n[GET-1] skipped (pass --date <YYYY-MM-DD> present in store to test fixed-date caching)")

    # ---- [GET-2] default-latest (no date) cached == stale-latest risk -----
    q = urllib.parse.urlencode({"lon0": args.lon, "lat0": args.lat, "append": "sst"})
    u = f"{base}?{q}"
    s1, h1, b1 = _req("GET", u)
    s2, h2, _ = _req("GET", u)
    c1, c2 = _cache(h1).upper(), _cache(h2).upper()

    # discover a usable date from the latest GET response (rows carry "date")
    discovered_date = None
    jlatest = _json(b1)
    if isinstance(jlatest, list) and jlatest and isinstance(jlatest[0], dict):
        discovered_date = jlatest[0].get("date")
    print(f"\n[GET-2] default-latest (no date)")
    print(f"        call#1 status={s1} X-api-cache={c1}")
    print(f"        call#2 status={s2} X-api-cache={c2}")
    if "HIT" in c2:
        risks.append("default-latest GET is cacheable (HIT). Under proxy_cache_valid 200 10d it "
                     "will become STALE after the next daily ingest unless bypassed/purged "
                     "(doc 00 §4.6). New app must set Cache-Control: no-store for date-less GET.")
        print("        => RISK: default-latest is cacheable; will go stale after next ingest "
              "unless bypassed/purged.")
    else:
        print("        => OK-ish: default-latest not served from cache here "
              "(verify new app sets Cache-Control: no-store for date-less GET).")

    # ---- [GET-3] distinct query strings keep distinct entries -------------
    qa = urllib.parse.urlencode({"lon0": args.lon, "lat0": args.lat, "append": "sst"})
    qb = urllib.parse.urlencode({"lon0": args.lon + 10, "lat0": args.lat + 5, "append": "sst"})
    _, _, ra = _req("GET", f"{base}?{qa}")
    _, _, rb = _req("GET", f"{base}?{qb}")
    ja, jb = _json(ra), _json(rb)
    print(f"\n[GET-3] two distinct query strings")
    if isinstance(ja, list) and isinstance(jb, list) and ja and jb:
        same = ja == jb
        print(f"        distinct query strings returned {'SAME' if same else 'different'} payloads")
        if same:
            problems.append("Distinct GET query strings returned identical payloads — unexpected key collapse.")
    else:
        print(f"        (could not compare payloads; got {type(ja).__name__}/{type(jb).__name__})")

    # ---- resolve a date for POST (contract requires date; null -> 400/422) --
    post_date = args.date or discovered_date
    if not post_date:
        print("\n[POST] SKIPPED — NOT VERIFIED: no usable date.")
        print("       POST /points contract requires a non-null `date`; sending null would 400/422")
        print("       and never reach the cache-collision test. Pass --date <YYYY-MM-DD present in store>,")
        print("       or ensure the date-less GET returns a row with a `date` field.")
        msg = "POST cache-collision test NOT VERIFIED (no usable date supplied/discovered)."
        (problems if args.strict else risks).append(msg)
        _summary(problems, risks)
        return
    print(f"\n[POST] using date={post_date} ({'from --date' if args.date else 'discovered from latest GET'})")

    # ---- [POST-1] endpoint present? --------------------------------------
    sA, hA, rA = _req("POST", points_url, body={"date": post_date, "points": POINTS_A, "append": "sst"})
    print(f"[POST-1] POST /points present?  status={sA} X-api-cache={_cache(hA)}")
    if sA in (404, 405):
        print("         => /points NOT deployed here (expected against current production).")
        print("            Run POST-2/POST-3 against staging with the new app to gate the fix.")
        if args.strict:
            problems.append("--strict: POST /points absent (404/405); cache-collision gate NOT run.")
        _summary(problems, risks)
        return
    if sA != 200:
        print(f"         => unexpected status; body head: {rA[:200]!r}")
        if args.strict:
            problems.append(f"--strict: POST /points returned {sA}; cache-collision gate NOT run.")
        _summary(problems, risks)
        return

    jA = _json(rA)
    a_ok = _rows_match_points(jA, POINTS_A)
    print(f"         body A returned {len(jA) if isinstance(jA, list) else '?'} rows; "
          f"match A points = {a_ok}")

    # ---- [POST-2] POST cached at all? identical body twice ---------------
    sA2, hA2, rA2 = _req("POST", points_url, body={"date": post_date, "points": POINTS_A, "append": "sst"})
    print(f"\n[POST-2] identical body twice")
    print(f"         call#1 X-api-cache={_cache(hA)}   call#2 X-api-cache={_cache(hA2)}")
    post_cached = "HIT" in _cache(hA2).upper()
    if post_cached:
        print("         => POST responses ARE being cached (precondition for the key-collision landmine).")

    # ---- [POST-3] THE LANDMINE: different body, same URI ----------------
    sB, hB, rB = _req("POST", points_url, body={"date": post_date, "points": POINTS_B, "append": "sst"})
    jB = _json(rB)
    b_match_b = _rows_match_points(jB, POINTS_B)
    b_match_a = _rows_match_points(jB, POINTS_A)
    print(f"\n[POST-3] CACHE-KEY COLLISION TEST (body A then DIFFERENT body B, same URI)")
    print(f"         body B status={sB} X-api-cache={_cache(hB)}")
    print(f"         body B rows match B's points = {b_match_b}")
    print(f"         body B rows match A's points = {b_match_a}  (TRUE here == COLLISION)")
    if b_match_a and not b_match_b:
        problems.append("CACHE-KEY COLLISION: POST body B was served body A's cached result "
                        "(URI-only proxy_cache_key + cached POST). MUST fix per P1-S6 "
                        "(app Cache-Control: no-store + NGINX location no-cache).")
        print("         => FAIL: COLLISION CONFIRMED.")
    elif b_match_b:
        print("         => OK: body B got B's own correct result (no collision).")
        if post_cached and "HIT" in _cache(hB).upper():
            problems.append("POST returned HIT for a DIFFERENT body — even though content looked "
                            "correct this run, URI-only keyed caching of POST is unsafe; ensure no-store.")
    else:
        print(f"         => inconclusive (B matched neither set; body head: {rB[:200]!r})")
        msg = ("POST-3 inconclusive: body B matched neither point set; cache-collision gate "
               "could not be evaluated.")
        (problems if args.strict else risks).append(msg)

    _summary(problems, risks)


def _summary(problems, risks):
    print("\n" + "=" * 70)
    if risks:
        print("RISKS (stale-data, flag for new app):")
        for r in risks:
            print(f"  - {r}")
    if problems:
        print("FAIL — correctness problems detected:")
        for p in problems:
            print(f"  - {p}")
        print("=" * 70)
        sys.exit(1)
    print("PASS — no cache correctness problems detected.")
    print("=" * 70)
    sys.exit(0)


if __name__ == "__main__":
    main()
