# P1-S5 / P1-S6 — VM24 deploy + cutover runbook

> **Ownership**: authored by Claude; **executed by Codex/ops on VM24** (SSH, secrets,
> process mgmt, NGINX, production-adjacent validation). Claude does NOT run VM24 steps.
> Single source of truth for contracts: [`01_refactor_spec.md`](01_refactor_spec.md)
> §P1-S5/§P1-S6/§4. This file is the actionable checklist + command templates.
> Replace every `<PLACEHOLDER>`. No secrets in git.

---

## P1-S5 — deployment sizing + binding VM24 load run

### 1. Environment
```bash
# on VM24, in a checkout of the branch (dev2026-refactor-baseline)
uv venv dev2026/.venv --python 3.13        # py314 also acceptable; match prod stack
uv pip install --python dev2026/.venv/bin/python \
  "zarr>=3" "xarray>=2025.1" numpy orjson fastapi "uvicorn[standard]" gunicorn httpx psutil
export GHRSST_ZARR_PATH=<ABS PATH to the FULL mur.zarr on VM24>   # NOT under bak/
```

### 2. Tunables (defaults in spec §P1-S5; set RSS ceiling per VM24 RAM)
| env | default | set on VM24 to |
|---|---|---|
| `GHRSST_ZARR_WORKERS` | `min(8,CPU)` | `<W_THREADS>` (per gunicorn worker) |
| `GHRSST_RSS_CEILING_MB` | 1024 | **`floor(RAM_MB / GUNICORN_WORKERS) - headroom`** |
| `GHRSST_MAX_DAYS` | 365 | 365 (or 730 after evidence) |
| `GHRSST_POINTS_BATCH_MAX` | 1000 | 1000 |
| `GHRSST_BATCH_CHUNK_FANOUT_MAX` | 64 | 64 |
| `GHRSST_LRU_MAX` | 64 | 64 |
| `GHRSST_OVERLOAD_WAIT_MS` | 2000 | 2000 |
| `GHRSST_OVERLOAD_QUEUE_MAX` | `4×workers` | tune |

**Sizing check (must hold):** `GUNICORN_WORKERS × GHRSST_ZARR_WORKERS ≤ CPU cores`,
and `GUNICORN_WORKERS × GHRSST_ZARR_WORKERS × (one chunk 4 MB × max fields 3) ≤ total RAM`.
Example `-w 2`, `GHRSST_ZARR_WORKERS=8` → 16 threads, ~192 MB concurrent-decompress floor.

### 3. Start (separate port; production app/ports untouched)
```bash
cd dev2026
GHRSST_ZARR_PATH=$GHRSST_ZARR_PATH GHRSST_RSS_CEILING_MB=<X> GHRSST_ZARR_WORKERS=<W_THREADS> \
PYTHONPATH=. ../dev2026/.venv/bin/gunicorn api.app:app \
  -w <GUNICORN_WORKERS> -k uvicorn.workers.UvicornWorker -b 127.0.0.1:<NEW_PORT> --timeout 180
curl -s http://127.0.0.1:<NEW_PORT>/healthz   # {latest, executor_limit, rss_mb, pid}
```

### 4. Binding load run (authoritative G1′/G6/G7 gate)
```bash
cd dev2026
B=http://127.0.0.1:<NEW_PORT>
P=../dev2026/.venv/bin/python
$P bench/loadtest.py --base $B --run-kind vm24 --scenario SB   --concurrency 32 --duration 120 --out vm24_sb.json
$P bench/loadtest.py --base $B --run-kind vm24 --scenario LR   --duration 120 --out vm24_lr.json   # C=4,8,16
$P bench/loadtest.py --base $B --run-kind vm24 --scenario BBOX --bbox-deg 10 --duration 120 --out vm24_bbox.json  # ~1M pts
$P bench/loadtest.py --base $B --run-kind vm24 --scenario OL   --concurrency <4×limit> --duration 120 --out vm24_ol.json
```
- `--run-kind vm24` stamps `meta.binding=true`.
- **Total RSS** (harness gives per-worker max, not a sum): collect externally during the run, e.g.
  `ssh vm24 'ps -o rss= -p $(pgrep -f "gunicorn api.app")' | awk '{s+=$1} END{print s/1024" MB total"}'`.

### 5. Binding pass/fail (from spec; record in the JSON + a short note)
- **G1** single 365-day p50<2.5 s, p95<4 s (= baseline `B`).
- **G1′** LR: C=4 served p95 < 2·B; C=8 < 3·B (zero reject/timeout); C=16 may 503-shed but served p95 < 4·B, **zero timeout/OOM**, log shed %.
- **G2′** SB: p99<150 ms, last-window p95 ≤ 1.2× first, no executor-queue/backlog growth, **zero 5xx**.
- **G6** every worker RSS ≤ `GHRSST_RSS_CEILING_MB`, no monotonic growth (check `by_pid` + external `ps`).
- **G7** OL: fast `503`+`Retry-After`, RSS bounded, `recovery_ms` small.
- **If G1′/G6 fail → trigger Phase-2 time-cube** (spec §6); do NOT loosen gates to pass.

---

## P1-S6 — NGINX cutover + cache fix (all reverse-proxy; no app code change)

### Current VM24 topology (from spec §P1-S6)
```nginx
location /api/ghrsst      { ... proxy_pass http://ghrsstapi; include conf2.d/aio_cache_proxy.conf; }
location /api/ghrsst/mcp  { ... proxy_pass http://ghrsstapi; include conf2.d/aio_cache_proxy.conf; }
upstream ghrsstapi  { server 127.0.0.1:8035; }   # OLD app
upstream ghrsst_mcp { server 127.0.0.1:8765; }   # defined but UNUSED — not a live path
```
Switching `ghrsstapi` affects **both** `/api/ghrsst` and `/api/ghrsst/mcp` together.

### Step 1 — add the `/points` location (cache OFF + CORS/OPTIONS)
Insert the `location /api/ghrsst/points {...}` block from spec §P1-S6 (longest-prefix wins;
`proxy_cache off`, `add_header X-api-cache BYPASS always`, full CORS + `OPTIONS`→204; do NOT
`include aio_cache_proxy.conf`). Decide CORS credentials per spec備註2 (drop `Allow-Credentials`
if no credentialed clients, else reflect Origin + `Vary: Origin`). `nginx -t && reload`.

### Step 2 — fix the GET cache landmine (default-latest 10d stale)
The new app already sends `Cache-Control: no-store` for date-less GET (verified P1-S2/S3), and
NGINX honours upstream Cache-Control (no `proxy_ignore_headers` set). No NGINX change needed for
GET beyond confirming with the probe (Step 4).

### Step 3 — cutover the upstream (shadow → canary → full)
```bash
# shadow: new app running on <NEW_PORT>, NOT yet in nginx; run §4 binding load + probe here.
# canary: point a fraction / internal test traffic at new app, BYPASSING cache:
#   upstream ghrsstapi { server 127.0.0.1:<NEW_PORT> weight=1; server 127.0.0.1:8035 weight=9; }
#   (or a test-only location with proxy_cache_bypass)
# full:  upstream ghrsstapi { server 127.0.0.1:<NEW_PORT>; }   then reload + purge cache.
```

### Step 4 — cache go/no-go probe (must pass before full)
```bash
cd dev2026
# GET behavior (current prod / canary):
../dev2026/.venv/bin/python bench/probe_nginx_cache.py --base https://eco.odb.ntu.edu.tw/api/ghrsst
# STRICT gate once /points is live on the canary/new app (fails CI on collision/absence):
../dev2026/.venv/bin/python bench/probe_nginx_cache.py \
  --base https://eco.odb.ntu.edu.tw/api/ghrsst --strict
```
Pass = date-less GET no longer cached-stale (MISS/BYPASS or no-store), and **POST-3 shows no
cache-key collision** (two different bodies return their own results). `X-api-cache: MISS|BYPASS`
or absent-on-`/points` + no collision both count (spec §P1-S6 canary条件).

### Step 5 — purge + rollback
- Full cutover: **purge `api_proxy` cache** (avoid 10d-TTL mixing of old/new). Mind
  `proxy_cache_use_stale updating` + background update during purge.
- **Rollback (1-step, seconds):** `upstream ghrsstapi { server 127.0.0.1:8035; }` → `reload` → purge.

### Go/No-Go for full cutover
All green: **G1/G1′/G2/G2′/G6/G7** (P1-S5) **+ parity** (P1-S3) **+ cache probe `--strict`** (Step 4).

---

## Order of operations
1. P1-S5 deploy on `<NEW_PORT>` (shadow) → binding load run → record G* verdicts.
2. Add `/points` NGINX location; `nginx -t && reload`.
3. Canary (cache-bypassed) → probe `--strict` + spot parity.
4. If all gates green → full upstream switch → purge cache → re-probe.
5. Keep rollback ready until soak is clean.
