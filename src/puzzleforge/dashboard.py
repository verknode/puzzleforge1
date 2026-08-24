from __future__ import annotations

import json
from dataclasses import asdict
from decimal import Decimal, localcontext
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .coordinator import Coordinator
from .local import LocalProfile, load_profile
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
        },
        "campaign": campaign,
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
            path = urlsplit(self.path).path
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
@media(max-width:760px){main{padding:20px 12px 40px}header{align-items:start}.card{grid-column:span 6}.wide{grid-column:1/-1}.value{font-size:28px}}
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
<article class="card full"><div class="label">Hypothesis Lab</div><div class="row"><span>Cycle</span><b id="labCycle">—</b></div><div class="row"><span>Research / GPU search</span><b id="labRatio">—</b></div><div class="row"><span>Selected model</span><b id="labModel">—</b></div><div class="row"><span>Forward holdout result</span><b id="labEvidence">—</b></div></article>
<article class="card full"><div class="label">Local profile</div><div class="row"><span>24h coverage at benchmark speed</span><b id="day">—</b></div><div class="row"><span>Durable chunk target</span><b id="chunkTarget">—</b></div><div class="row"><span>Configured thermal policy</span><b id="thermalGuard">—</b></div><div class="row"><span>GPU memory</span><b id="memory">—</b></div><div class="row"><span>Last update</span><b id="updated">—</b></div><div class="meta error" id="error"></div></article>
</section></main>
<script>
const $=id=>document.getElementById(id),num=v=>Number(v||0),fmt=n=>new Intl.NumberFormat('en',{maximumFractionDigits:2,notation:'compact'}).format(n),pct=v=>{const n=num(v);return n===0?'0%':n<.000001?n.toExponential(3)+'%':n.toFixed(Math.min(8,Math.max(3,-Math.floor(Math.log10(n))+2)))+'%'};
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});const d=await r.json();if(!r.ok)throw Error(d.error||r.status);const c=d.campaign,l=d.local,t=d.telemetry||{},x=d.derived;
$('state').textContent=c.state;$('dot').style.background=c.state==='running'?'var(--good)':c.state==='found'?'var(--hot)':'var(--muted)';$('speed').textContent=fmt(l.measured_rate_keys_per_second);$('coverage').textContent=pct(x.coverage_percent);$('coverageBar').style.width=Math.min(100,num(x.coverage_percent))+'%';$('checked').textContent=fmt(num(c.checked_keys))+' / '+fmt(num(c.total_keys))+' checked';$('puzzle').textContent='#'+c.puzzle;$('mode').textContent=c.planner_mode.toUpperCase();$('chunks').textContent=c.completed_chunks;$('failures').textContent=c.worker_failures+' / '+c.retry_queue;$('day').textContent=pct(x.benchmark_day_percent);$('chunkTarget').textContent=l.target_chunk_seconds+' sec / '+fmt(l.chunk_size);$('thermalGuard').textContent=l.thermal_guard.maximum_c+'°C → '+l.thermal_guard.resume_c+'°C';$('updated').textContent=new Date(c.updated_at).toLocaleString();const h=c.hypothesis_lab||{},r=h.report||{},s=(r.scores||[]).find(v=>v.name===r.selected_model);$('labCycle').textContent=h.enabled?h.cycle:'OFF';$('labRatio').textContent=h.enabled?h.research_percent+'% / '+h.search_percent+'%':'—';$('labModel').textContent=r.selected_model||'pending';$('labEvidence').textContent=r.selected_model?(r.uniform_fallback?'UNIFORM FALLBACK':r.selected_model_validated?'VALIDATED':('experimental / '+(s?Number(s.geometric_lift).toFixed(3)+'× holdout':'no score'))):'pending';
if(t.available){$('gpuName').textContent='LOCAL GPU / '+t.name;$('load').textContent=Math.round(num(t.utilization_percent));$('loadBar').style.width=Math.min(100,num(t.utilization_percent))+'%';$('temp').textContent=Math.round(num(t.temperature_c));$('power').textContent=num(t.power_w).toFixed(0);$('powerLimit').textContent='limit '+num(t.power_limit_w).toFixed(0)+' W';$('clock').textContent=fmt(num(t.sm_clock_mhz))+' MHz';$('memory').textContent=fmt(num(t.memory_used_mib))+' / '+fmt(num(t.memory_total_mib))+' MiB';$('error').textContent=''}else{$('error').textContent=t.error||'GPU telemetry unavailable'}
}catch(e){$('state').textContent='OFFLINE';$('dot').style.background='var(--bad)';$('error').textContent=e.message}}
refresh();setInterval(refresh,3000);
</script></body></html>""".encode("utf-8")
