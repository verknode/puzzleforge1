from __future__ import annotations

import json
from dataclasses import asdict
from decimal import Decimal, localcontext
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .coordinator import Coordinator
from .generator_lab import generator_dashboard_status
from .local import LocalProfile, load_profile
from .sweep import load_sweep_record
from .telemetry import TelemetryCache


def dashboard_payload(
    profile: LocalProfile,
    telemetry: dict[str, object],
) -> dict[str, object]:
    campaign = Coordinator(Path(profile.database)).status()
    checked = Decimal(campaign["checked_keys"])
    total = Decimal(campaign["total_keys"])
    with localcontext() as context:
        context.prec = 60
        coverage_percent = checked / total * Decimal(100)
        benchmark_day = min(
            total,
            Decimal(str(profile.measured_rate_keys_per_second)) * Decimal(86_400),
        )
        benchmark_day_percent = benchmark_day / total * Decimal(100)
    sweep_record = load_sweep_record(Path(profile.database).with_name("sweep.json"))
    sweep = {
        "state": (
            str(sweep_record.get("state", "pending"))
            if sweep_record
            else ("armed" if profile.auto_sweep_enabled else "disabled")
        ),
        "destination_address": profile.sweep_address,
        "txid": None if not sweep_record else sweep_record.get("txid"),
        "output_value_sats": (
            None if not sweep_record else sweep_record.get("output_value_sats")
        ),
        "fee_sats": None if not sweep_record else sweep_record.get("fee_sats"),
        "detail": "" if not sweep_record else sweep_record.get("detail", ""),
        "updated_at": None if not sweep_record else sweep_record.get("updated_at"),
    }
    return {
        "schema": 1,
        "local": {
            "puzzle": profile.puzzle,
            "binary": profile.binary,
            "tuning": asdict(profile.tuning),
            "measured_rate_keys_per_second": profile.measured_rate_keys_per_second,
            "benchmark_relative_spread": profile.benchmark_relative_spread,
            "chunk_size": profile.chunk_size,
            "target_chunk_seconds": profile.target_chunk_seconds,
            "planner_mode": profile.planner_mode,
            "device_probe": profile.device_probe,
            "thermal_guard": {
                "maximum_c": profile.max_temperature_c,
                "resume_c": profile.resume_temperature_c,
                "poll_seconds": profile.thermal_poll_seconds,
                "max_retries": profile.thermal_max_retries,
            },
            "hypothesis_lab": {
                "enabled": profile.hypothesis_enabled,
                "research_percent": profile.hypothesis_research_percent,
                "search_percent": profile.hypothesis_search_percent,
            },
            "generator_lab": {
                "enabled": profile.generator_lab_enabled,
                "cpu_duty_percent": profile.generator_lab_cpu_percent,
                "gpu_reserved_percent": 0,
                "wordlist_configured": bool(profile.generator_lab_wordlist),
            },
            "auto_sweep": {
                "enabled": profile.auto_sweep_enabled,
                "destination_address": profile.sweep_address,
                "fee_floor_sat_vb": profile.sweep_fee_floor_sat_vb,
                "fee_cap_sat_vb": profile.sweep_fee_cap_sat_vb,
            },
        },
        "campaign": campaign,
        "generator_lab": generator_dashboard_status(
            profile.database,
            enabled=profile.generator_lab_enabled,
            duty_percent=profile.generator_lab_cpu_percent,
        ),
        "sweep": sweep,
        "telemetry": telemetry,
        "derived": {
            "coverage_percent": str(coverage_percent),
            "benchmark_day_percent": str(benchmark_day_percent),
        },
    }


def create_dashboard_server(
    profile_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8788,
) -> ThreadingHTTPServer:
    profile = load_profile(profile_path)
    monitor = TelemetryCache(profile.tuning.device)

    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "PuzzleForgeDashboard/0.1"

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            request = urlsplit(self.path)
            path = request.path
            if path == "/":
                self._send(HTTPStatus.OK, DASHBOARD_HTML, "text/html; charset=utf-8")
                return
            if path == "/api/status":
                try:
                    payload = dashboard_payload(profile, monitor.sample())
                    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                    self._send(HTTPStatus.OK, body, "application/json")
                except (OSError, RuntimeError, ValueError) as exc:
                    body = json.dumps({"error": str(exc)}).encode("utf-8")
                    self._send(HTTPStatus.INTERNAL_SERVER_ERROR, body, "application/json")
                return
            if path == "/api/range-map":
                try:
                    raw_bins = parse_qs(request.query).get("bins", ["4096"])[0]
                    payload = Coordinator(Path(profile.database)).range_map(
                        bins=int(raw_bins)
                    )
                    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
                    self._send(HTTPStatus.OK, body, "application/json")
                except (OSError, RuntimeError, ValueError) as exc:
                    body = json.dumps({"error": str(exc)}).encode("utf-8")
                    self._send(HTTPStatus.BAD_REQUEST, body, "application/json")
                return
            if path == "/healthz":
                self._send(HTTPStatus.OK, b"ok\n", "text/plain; charset=utf-8")
                return
            self._send(HTTPStatus.NOT_FOUND, b"not found\n", "text/plain")

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"read only\n", "text/plain")

        def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
            self.send_response(status.value)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer((host, port), DashboardHandler)
    server.daemon_threads = True
    return server


def serve_dashboard(
    profile_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8788,
) -> None:
    server = create_dashboard_server(profile_path, host=host, port=port)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>PuzzleForge Local</title>
<style>
:root{color-scheme:dark;--bg:#090b0d;--panel:#111519;--line:#263038;--text:#edf2f5;--muted:#81909a;--hot:#ffb000;--good:#45e08a;--bad:#ff5d64}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 80% 0,#182026 0,transparent 34%),var(--bg);color:var(--text);font:14px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace;min-height:100vh}
main{width:min(1120px,100%);margin:auto;padding:28px 18px 48px}header{display:flex;justify-content:space-between;gap:18px;align-items:end;margin-bottom:24px}.brand{font-size:clamp(25px,5vw,46px);font-weight:900;letter-spacing:-.06em}.brand b{color:var(--hot)}.sub{color:var(--muted);font-size:12px}.state{display:flex;align-items:center;gap:8px;text-transform:uppercase;font-weight:800}.dot{width:9px;height:9px;border-radius:50%;background:var(--good);box-shadow:0 0 15px var(--good)}
.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px}.card{grid-column:span 3;background:linear-gradient(145deg,rgba(19,24,29,.95),rgba(13,16,19,.96));border:1px solid var(--line);border-radius:16px;padding:17px;min-height:126px}.wide{grid-column:span 6}.full{grid-column:1/-1}.label{color:var(--muted);font-size:11px;letter-spacing:.14em;text-transform:uppercase}.value{font-size:clamp(25px,4vw,40px);font-weight:850;letter-spacing:-.05em;margin-top:12px}.unit{font-size:12px;color:var(--muted);margin-left:5px}.meta{color:var(--muted);margin-top:8px}.bar{height:9px;background:#07090a;border:1px solid var(--line);border-radius:20px;overflow:hidden;margin-top:18px}.fill{height:100%;width:0;background:linear-gradient(90deg,var(--hot),#ffe08a);box-shadow:0 0 15px #ffb00088;transition:width .5s}.row{display:flex;justify-content:space-between;gap:15px;padding:8px 0;border-bottom:1px solid #20272c}.row:last-child{border:0}.error{color:var(--bad)}
.map-head{display:flex;align-items:center;justify-content:space-between;gap:16px}.map-legend{display:flex;flex-wrap:wrap;gap:12px;color:var(--muted);font-size:11px}.map-legend span{display:flex;align-items:center;gap:5px}.swatch{width:8px;height:8px;border-radius:2px;background:#111820;border:1px solid #34414a}.swatch.done{background:var(--good);border-color:var(--good)}.swatch.active{background:var(--hot);border-color:var(--hot)}.swatch.retry{background:var(--bad);border-color:var(--bad)}.range-map{display:block;width:100%;height:260px;margin-top:14px;border:1px solid var(--line);border-radius:9px;background:#090c0f;cursor:crosshair;image-rendering:pixelated}.map-detail{min-height:21px;color:var(--muted);margin-top:9px;overflow-wrap:anywhere}.map-note{font-size:11px;color:#65727b;margin-top:4px}
@media(max-width:760px){main{padding:20px 12px 40px}header{align-items:start}.card{grid-column:span 6}.wide{grid-column:1/-1}.value{font-size:28px}.map-head{align-items:start;flex-direction:column;gap:8px}.range-map{height:320px}}
@media(max-width:420px){.card{grid-column:1/-1;min-height:112px}.brand{font-size:29px}}
</style>
</head>
<body><main>
<header><div><div class="brand">PUZZLE<b>FORGE</b></div><div class="sub" id="gpuName">LOCAL GPU / CONNECTING</div></div><div class="state"><i class="dot" id="dot"></i><span id="state">CONNECTING</span></div></header>
<section class="grid">
<article class="card"><div class="label">Speed</div><div class="value" id="speed">—</div><div class="meta">measured keys / second</div></article>
<article class="card"><div class="label">GPU load</div><div class="value"><span id="load">—</span><span class="unit">%</span></div><div class="bar"><div class="fill" id="loadBar"></div></div></article>
<article class="card"><div class="label">Temperature</div><div class="value"><span id="temp">—</span><span class="unit">°C</span></div><div class="meta" id="clock">telemetry pending</div></article>
<article class="card"><div class="label">Power</div><div class="value"><span id="power">—</span><span class="unit">W</span></div><div class="meta" id="powerLimit">limit —</div></article>
<article class="card wide"><div class="label">Exact unique coverage</div><div class="value" id="coverage">—</div><div class="bar"><div class="fill" id="coverageBar"></div></div><div class="meta" id="checked">— checked</div></article>
<article class="card wide"><div class="label">Campaign</div><div class="row"><span>Puzzle</span><b id="puzzle">—</b></div><div class="row"><span>Mode</span><b id="mode">—</b></div><div class="row"><span>Completed chunks</span><b id="chunks">—</b></div><div class="row"><span>Failures / retries</span><b id="failures">—</b></div></article>
<article class="card full"><div class="map-head"><div class="label">Keyspace map / low → high</div><div class="map-legend"><span><i class="swatch done"></i>checked</span><span><i class="swatch active"></i>active</span><span><i class="swatch retry"></i>retry</span><span><i class="swatch"></i>untouched</span></div></div><canvas class="range-map" id="rangeMap"></canvas><div class="map-detail" id="rangeMapDetail">Loading range positions…</div><div class="map-note">A lit cell contains one or more chunks; it does not mean the entire coarse cell was checked. Tap a cell for its exact key range.</div></article>
<article class="card full"><div class="label">Hypothesis Lab / Model Zoo</div><div class="row"><span>Cycle</span><b id="labCycle">—</b></div><div class="row"><span>Research / GPU search</span><b id="labRatio">—</b></div><div class="row"><span>Models / eligible / shadow</span><b id="labCounts">—</b></div><div class="row"><span>Best eligible candidate</span><b id="labCandidate">—</b></div><div class="row"><span>Selected model</span><b id="labModel">—</b></div><div class="row"><span>Empirical evidence gate</span><b id="labEvidence">—</b></div></article>
<article class="card full"><div class="label">Generator Lab / public-puzzle seed research</div><div class="row"><span>Status</span><b id="genStatus">—</b></div><div class="row"><span>CPU duty / GPU reserved</span><b id="genDuty">—</b></div><div class="row"><span>Generator candidates / completed seeds</span><b id="genCounts">—</b></div><div class="row"><span>Current source</span><b id="genSource">—</b></div><div class="row"><span>Current scheme</span><b id="genScheme">—</b></div><div class="row"><span>Best control match (diagnostic only)</span><b id="genBits">—</b></div><div class="row"><span>Exact validated generators</span><b id="genValidated">—</b></div></article>
<article class="card full"><div class="label">Local profile</div><div class="row"><span>24h coverage at benchmark speed</span><b id="day">—</b></div><div class="row"><span>Durable chunk target</span><b id="chunkTarget">—</b></div><div class="row"><span>Configured thermal policy</span><b id="thermalGuard">—</b></div><div class="row"><span>GPU memory</span><b id="memory">—</b></div><div class="row"><span>Last update</span><b id="updated">—</b></div><div class="meta error" id="error"></div></article>
<article class="card full"><div class="label">Verified-match auto-sweep</div><div class="row"><span>Status</span><b id="sweepState">—</b></div><div class="row"><span>Destination</span><b id="sweepAddress">—</b></div><div class="row"><span>Transaction</span><b id="sweepTxid">—</b></div><div class="row"><span>Amount / fee</span><b id="sweepAmount">—</b></div></article>
</section></main>
<script>
const $ = id => document.getElementById(id);
const num = value => Number(value || 0);
const fmt = value => new Intl.NumberFormat('en', {
  maximumFractionDigits: 2,
  notation: 'compact'
}).format(value);
const pct = value => {
  const number = num(value);
  if (number === 0) return '0%';
  if (number < .000001) return number.toExponential(3) + '%';
  return number.toFixed(
    Math.min(8, Math.max(3, -Math.floor(Math.log10(number)) + 2))
  ) + '%';
};
let rangeMapData = null;

const sparseCounts = values => new Map((values || []).map(value => [value[0], value[1]]));
const hex = value => value.toString(16).padStart(18, '0');

function renderRangeMap() {
  if (!rangeMapData) return;
  const canvas = $('rangeMap');
  const bounds = canvas.getBoundingClientRect();
  const width = Math.max(280, Math.floor(bounds.width));
  const columns = width < 620 ? 64 : 128;
  const rows = Math.ceil(rangeMapData.bins / columns);
  const cell = width / columns;
  const height = Math.max(1, rows * cell);
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  canvas.style.height = height + 'px';
  const context = canvas.getContext('2d');
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.fillStyle = '#090c0f';
  context.fillRect(0, 0, width, height);

  const completed = sparseCounts(rangeMapData.states.completed);
  const active = sparseCounts(rangeMapData.states.active);
  const retry = sparseCounts(rangeMapData.states.retry);
  const maximum = Math.max(1, ...completed.values());
  const gap = cell >= 6 ? 1 : .55;
  for (let index = 0; index < rangeMapData.bins; index += 1) {
    const x = (index % columns) * cell;
    const y = Math.floor(index / columns) * cell;
    const doneCount = completed.get(index) || 0;
    if (active.has(index)) {
      context.fillStyle = '#ffb000';
    } else if (retry.has(index)) {
      context.fillStyle = '#ff5d64';
    } else if (doneCount) {
      const alpha = .45 + .55 * Math.log1p(doneCount) / Math.log1p(maximum);
      context.fillStyle = `rgba(69,224,138,${alpha})`;
    } else {
      context.fillStyle = '#111820';
    }
    context.fillRect(x + gap / 2, y + gap / 2, cell - gap, cell - gap);
  }
  canvas._rangeLayout = {columns, rows, cell, completed, active, retry};

  const done = (rangeMapData.states.completed || []).reduce((sum, item) => sum + item[1], 0);
  const touched = completed.size;
  $('rangeMapDetail').textContent = `${done.toLocaleString()} completed chunks across ` +
    `${touched.toLocaleString()} of ${rangeMapData.bins.toLocaleString()} display cells`;
}

function showRangeMapCell(event) {
  if (!rangeMapData) return;
  const canvas = $('rangeMap');
  const layout = canvas._rangeLayout;
  if (!layout) return;
  const bounds = canvas.getBoundingClientRect();
  const column = Math.floor((event.clientX - bounds.left) / layout.cell);
  const row = Math.floor((event.clientY - bounds.top) / layout.cell);
  const index = row * layout.columns + column;
  if (index < 0 || index >= rangeMapData.bins) return;

  const totalChunks = BigInt(rangeMapData.total_chunks);
  const span = BigInt(rangeMapData.bucket_span_chunks);
  const chunkSize = BigInt(rangeMapData.chunk_size);
  const puzzleStart = BigInt('0x' + rangeMapData.start_hex);
  const puzzleEnd = BigInt('0x' + rangeMapData.end_hex);
  const firstChunk = BigInt(index) * span;
  const afterLast = firstChunk + span < totalChunks ? firstChunk + span : totalChunks;
  const keyStart = puzzleStart + firstChunk * chunkSize;
  const calculatedEnd = puzzleStart + afterLast * chunkSize - 1n;
  const keyEnd = calculatedEnd < puzzleEnd ? calculatedEnd : puzzleEnd;
  const done = layout.completed.get(index) || 0;
  const active = layout.active.get(index) || 0;
  const retry = layout.retry.get(index) || 0;
  $('rangeMapDetail').textContent = `0x${hex(keyStart)} – 0x${hex(keyEnd)} · ` +
    `checked ${done} · active ${active} · retry ${retry}`;
}

async function refreshRangeMap() {
  try {
    const response = await fetch('/api/range-map?bins=4096', {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || response.status);
    rangeMapData = data;
    renderRangeMap();
  } catch (error) {
    $('rangeMapDetail').textContent = 'Range map unavailable: ' + error.message;
  }
}

async function refresh() {
  try {
    const response = await fetch('/api/status', {cache: 'no-store'});
    const data = await response.json();
    if (!response.ok) throw Error(data.error || response.status);

    const campaign = data.campaign;
    const local = data.local;
    const telemetry = data.telemetry || {};
    const derived = data.derived;
    const hypothesis = campaign.hypothesis_lab || {};
    const report = hypothesis.report || {};
    const score = (report.scores || []).find(
      value => value.name === report.selected_model
    );
    const generator = data.generator_lab || {};
    const sweep = data.sweep || {};

    $('state').textContent = campaign.state;
    $('dot').style.background = campaign.state === 'running'
      ? 'var(--good)'
      : campaign.state === 'found' ? 'var(--hot)' : 'var(--muted)';
    $('speed').textContent = fmt(local.measured_rate_keys_per_second);
    $('coverage').textContent = pct(derived.coverage_percent);
    $('coverageBar').style.width = Math.min(100, num(derived.coverage_percent)) + '%';
    $('checked').textContent = fmt(num(campaign.checked_keys)) + ' / ' +
      fmt(num(campaign.total_keys)) + ' checked';
    $('puzzle').textContent = '#' + campaign.puzzle;
    $('mode').textContent = campaign.planner_mode.toUpperCase();
    $('chunks').textContent = campaign.completed_chunks;
    $('failures').textContent = campaign.worker_failures + ' / ' + campaign.retry_queue;
    $('day').textContent = pct(derived.benchmark_day_percent);
    $('chunkTarget').textContent = local.target_chunk_seconds + ' sec / ' +
      fmt(local.chunk_size);
    $('thermalGuard').textContent = local.thermal_guard.maximum_c + '°C → ' +
      local.thermal_guard.resume_c + '°C';
    $('updated').textContent = new Date(campaign.updated_at).toLocaleString();

    $('labCycle').textContent = hypothesis.enabled ? hypothesis.cycle : 'OFF';
    $('labRatio').textContent = hypothesis.enabled
      ? hypothesis.research_percent + '% / ' + hypothesis.search_percent + '%'
      : '—';
    $('labCounts').textContent = report.model_count !== undefined
      ? report.model_count + ' / ' + report.eligible_model_count + ' / ' +
        report.shadow_model_count
      : 'pending';
    $('labCandidate').textContent = report.best_candidate || 'pending';
    $('labModel').textContent = report.selected_model || 'pending';
    $('labEvidence').textContent = report.selected_model
      ? report.uniform_fallback
        ? 'UNIFORM FALLBACK / 0 validated'
        : report.selected_model_validated
          ? 'VALIDATED / ' + report.validated_model_count
          : 'experimental / ' + (score
            ? Number(score.geometric_lift).toFixed(3) + '× holdout'
            : 'no score')
      : 'pending';

    $('genStatus').textContent = (generator.status || 'disabled').toUpperCase();
    $('genDuty').textContent = generator.enabled
      ? generator.cpu_duty_percent + '% / ' + generator.gpu_reserved_percent + '%'
      : 'OFF';
    $('genCounts').textContent = fmt(num(generator.checked_candidates)) + ' / ' +
      fmt(num(generator.completed_seed_candidates));
    $('genSource').textContent = generator.current_source || 'pending';
    $('genScheme').textContent = generator.current_scheme || 'pending';
    $('genBits').textContent = num(generator.best_low_bits_total)
      ? generator.best_low_bits + ' / ' + generator.best_low_bits_total + ' bits'
      : 'pending';
    $('genValidated').textContent = generator.validated_known_generators || 0;

    $('sweepState').textContent = (sweep.state || 'disabled').toUpperCase();
    $('sweepAddress').textContent = sweep.destination_address || 'not configured';
    $('sweepTxid').textContent = sweep.txid || '—';
    $('sweepAmount').textContent = sweep.output_value_sats
      ? fmt(sweep.output_value_sats) + ' sats / ' + fmt(sweep.fee_sats) + ' sats'
      : '—';

    if (telemetry.available) {
      $('gpuName').textContent = 'LOCAL GPU / ' + telemetry.name;
      $('load').textContent = Math.round(num(telemetry.utilization_percent));
      $('loadBar').style.width = Math.min(100, num(telemetry.utilization_percent)) + '%';
      $('temp').textContent = Math.round(num(telemetry.temperature_c));
      $('power').textContent = num(telemetry.power_w).toFixed(0);
      $('powerLimit').textContent = 'limit ' +
        num(telemetry.power_limit_w).toFixed(0) + ' W';
      $('clock').textContent = fmt(num(telemetry.sm_clock_mhz)) + ' MHz';
      $('memory').textContent = fmt(num(telemetry.memory_used_mib)) + ' / ' +
        fmt(num(telemetry.memory_total_mib)) + ' MiB';
      $('error').textContent = generator.status === 'error'
        ? generator.last_error || 'Generator Lab error'
        : '';
    } else {
      $('error').textContent = telemetry.error || 'GPU telemetry unavailable';
    }
  } catch (error) {
    $('state').textContent = 'OFFLINE';
    $('dot').style.background = 'var(--bad)';
    $('error').textContent = error.message;
  }
}

refresh();
refreshRangeMap();
setInterval(refresh, 3000);
$('rangeMap').addEventListener('click', showRangeMapCell);
window.addEventListener('resize', renderRangeMap);
setInterval(refreshRangeMap, 15000);
</script></body></html>""".encode("utf-8")
