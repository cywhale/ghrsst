// dev2026 — P3-S0 reusable browser/client benchmark harness (Codex round-2 #1).
// Makes the "Chrome freeze" regression-able instead of manual. Format-agnostic: row (array of
// objects) / columnar (flat) / grid (axes + 2-D fields) are auto-detected, so S1/S2 reuse this for
// grid/binary. Node lower bound (built-in fetch); the matching Chrome run uses bbox_bench.html.
//
// Segments (spec §2 browser split): T_fetch (url mode) / T_text (decode body) / T_parse (JSON.parse)
// / T_transform (minimal map-layer-input build: flat Float32Arrays of lon/lat/value over all points).
//
// Run:
//   node dev2026/bench/client/bbox_client_bench.mjs --file dev2026/bench/results/payloads/bbox_750k_grid.json
//   node dev2026/bench/client/bbox_client_bench.mjs --url 'http://127.0.0.1:8014/api/ghrsst?...&format=grid'
//   (add --runs 3 to average; prints one JSON object)
import { performance } from 'node:perf_hooks';
import { readFile } from 'node:fs/promises';

function args() {
  const a = process.argv.slice(2), o = { runs: 1 };
  for (let i = 0; i < a.length; i++) {
    if (a[i] === '--file') o.file = a[++i];
    else if (a[i] === '--url') o.url = a[++i];
    else if (a[i] === '--runs') o.runs = parseInt(a[++i], 10);
  }
  return o;
}

function detect(p) {
  if (Array.isArray(p)) return 'row';
  if (p && p.shape) return 'grid';
  return 'columnar';
}

function transform(p, fmt) {
  // Build the same map-layer input (lon/lat/value Float32Arrays over all points) for every format,
  // so the number is comparable client work, not format-specific shortcuts.
  if (fmt === 'row') {
    const n = p.length, lon = new Float32Array(n), lat = new Float32Array(n), val = new Float32Array(n);
    let valid = 0;
    for (let i = 0; i < n; i++) { const r = p[i]; lon[i] = r.lon; lat[i] = r.lat; const s = r.sst; if (s != null) { val[i] = s; valid++; } else val[i] = NaN; }
    return { n, valid };
  }
  if (fmt === 'columnar') {
    const lonA = p.lon, latA = p.lat, sstA = (p.fields && p.fields.sst) || [];
    const n = lonA.length, lon = new Float32Array(n), lat = new Float32Array(n), val = new Float32Array(n);
    let valid = 0;
    for (let i = 0; i < n; i++) { lon[i] = lonA[i]; lat[i] = latA[i]; const s = sstA[i]; if (s != null) { val[i] = s; valid++; } else val[i] = NaN; }
    return { n, valid };
  }
  // grid
  const lonAx = p.lon, latAx = p.lat, [nlat, nlon] = p.shape, n = nlat * nlon;
  const lon = new Float32Array(n), lat = new Float32Array(n), val = new Float32Array(n);
  const sst = p.fields && p.fields.sst; let k = 0, valid = 0;
  for (let i = 0; i < nlat; i++) {
    const row = sst ? sst[i] : null;
    for (let j = 0; j < nlon; j++) { lon[k] = lonAx[j]; lat[k] = latAx[i]; const s = row ? row[j] : null; if (s != null) { val[k] = s; valid++; } else val[k] = NaN; k++; }
  }
  return { n, valid };
}

async function once(o) {
  let buf, t_fetch = null;
  if (o.url) {
    const t0 = performance.now();
    const res = await fetch(o.url, { headers: { 'Accept-Encoding': 'br,gzip' } });
    const ab = await res.arrayBuffer();
    t_fetch = performance.now() - t0;
    buf = Buffer.from(ab);
    var content_encoding = res.headers.get('content-encoding');
  } else {
    buf = await readFile(o.file);
  }
  const bytes = buf.length;
  let t = performance.now(); const text = buf.toString('utf8'); const t_text = performance.now() - t;
  const h0 = process.memoryUsage().heapUsed;
  t = performance.now(); const parsed = JSON.parse(text); const t_parse = performance.now() - t;
  const fmt = detect(parsed);
  t = performance.now(); const r = transform(parsed, fmt); const t_transform = performance.now() - t;
  const heap_mb = Math.round((process.memoryUsage().heapUsed - h0) / 1e5) / 10;
  return { fmt, bytes, content_encoding: o.url ? content_encoding : null,
           t_fetch_ms: t_fetch == null ? null : +t_fetch.toFixed(1),
           t_text_ms: +t_text.toFixed(1), t_parse_ms: +t_parse.toFixed(1),
           t_transform_ms: +t_transform.toFixed(1), points: r.n, valid: r.valid, heap_mb };
}

const o = args();
if (!o.file && !o.url) { console.error('need --file <path> or --url <url>'); process.exit(2); }
const runs = [];
for (let i = 0; i < o.runs; i++) runs.push(await once(o));
const last = runs[runs.length - 1];
const avg = (k) => runs.every(r => r[k] == null) ? null
  : +(runs.reduce((s, r) => s + (r[k] || 0), 0) / runs.length).toFixed(1);
console.log(JSON.stringify({
  source: o.file || o.url, format: last.fmt, bytes: last.bytes,
  content_encoding: last.content_encoding, points: last.points, valid: last.valid,
  runs: o.runs, heap_mb: last.heap_mb,
  t_fetch_ms: avg('t_fetch_ms'), t_text_ms: avg('t_text_ms'),
  t_parse_ms: avg('t_parse_ms'), t_transform_ms: avg('t_transform_ms'),
  t_client_total_ms: +(['t_text_ms', 't_parse_ms', 't_transform_ms']
    .reduce((s, k) => s + (avg(k) || 0), 0)).toFixed(1)
}, null, 2));
