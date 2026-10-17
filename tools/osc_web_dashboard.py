#!/usr/bin/env python3
"""
cosmic dune -- OSC web dashboard (touch-screen layout, fully offline)

Listens for pods' OSC telemetry and serves a live web page designed for a
small 800x480 touch screen (run the browser fullscreen/kiosk):

  * Grid view: six big status tiles, one per pod slot (1-6) -- BPM, a GSR
    meter in the same white/blue/purple/red colours the pod's LED strip
    uses, online/offline/no-pulse state. A slot is greyed out until that
    pod has been heard from.
  * Detail view: tap a tile to see that pod's BPM and GSR graphs (last 60 s)
    plus its numbers (IBI, raw GSR, IP address, time since last packet).

Works with no internet at all: nothing is loaded from a CDN or font host.
Chart.js is served from chart.umd.js, which must sit next to this script.
If that file is missing, the tiles and numbers still work; only the graphs
are unavailable (the page says so).

Same message format as osc_test_hub.py (a single default handler parsing
"/pod/<id>/<leaf>"). osc_test_hub.py's `simulate` mode works unchanged as a
fake data source for this dashboard.

Usage:
    python3 osc_web_dashboard.py [--osc-port 9000] [--web-port 5000]
    python3 osc_test_hub.py simulate --port 9000        # fake pods, for testing

Requires: pip install flask python-osc   (see requirements.txt)
"""
import argparse
import os
import socket
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from flask import Flask, Response, abort, jsonify, send_file
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

# Chart.js is served from a copy next to this script. There is deliberately
# NO CDN fallback: this has to work with no internet (a Pi running its own
# hotspot usually has none).
HERE = os.path.dirname(os.path.abspath(__file__))
CHART_JS_FILE = os.path.join(HERE, "chart.umd.js")

# A pod not heard from in this long is shown OFFLINE.
STALE_AFTER_S = 3.0

# Graph history per pod: one point every HISTORY_INTERVAL_S, HISTORY_LEN
# points = 60 s. (Pods send ~30 messages/s; storing every one gave only ~13 s
# of history and shipped far more data than a graph needs.)
HISTORY_INTERVAL_S = 0.25
HISTORY_LEN = 240


def get_local_ip() -> str:
    """Best-effort guess at this machine's LAN IP (the interface the OS
    would use to reach the internet). See get_local_ips() for why that is
    often the wrong one on a Pi running its own hotspot."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def get_local_ips() -> list:
    """Every non-loopback IPv4 address on this machine, as (interface, ip).
    Uses `ip -4 -o addr` (Linux); falls back to the single default-route
    guess where that is missing (macOS)."""
    try:
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"],
                             capture_output=True, text=True, timeout=2).stdout
        found = []
        for line in out.splitlines():
            parts = line.split()          # "2: eth0  inet 192.168.1.5/24 ..."
            ip = parts[3].split("/")[0]
            if not ip.startswith("127."):
                found.append((parts[1], ip))
        if found:
            return found
    except (OSError, subprocess.SubprocessError, IndexError):
        pass
    return [("default route", get_local_ip())]


@dataclass
class Sample:
    t: float
    bpm: int
    gsr: float


@dataclass
class PodState:
    last_seen: float = 0.0
    last_hist: float = 0.0
    ip: str = ""
    bpm: int = 0
    ibi_ms: int = 0
    signal: bool = False
    gsr_raw: int = 0
    gsr_norm: float = 0.0
    seen: set = field(default_factory=set)   # which leaves this pod has sent so far
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_LEN))


class Dashboard:
    """Holds all pod state and does the OSC receiving -- Flask's routes
    below just read from this under its lock."""

    def __init__(self, osc_port: int):
        self.osc_port = osc_port
        self.pods = {}
        self.lock = threading.Lock()

    def on_message(self, client_address: tuple, address: str, *args) -> None:
        parts = address.split("/")
        if len(parts) != 4 or parts[1] != "pod":
            return
        try:
            pod_id = int(parts[2])
        except ValueError:
            return
        leaf = parts[3]
        if not args:
            return
        value = args[0]

        with self.lock:
            st = self.pods.setdefault(pod_id, PodState())
            st.last_seen = time.time()
            st.ip = client_address[0]
            st.seen.add(leaf)
            graphable = False
            if leaf == "bpm":
                st.bpm = int(value)
                graphable = True
            elif leaf == "ibi":
                st.ibi_ms = int(value)
            elif leaf == "signal":
                st.signal = bool(value)
            elif leaf == "gsrRaw":
                st.gsr_raw = int(value)
            elif leaf == "gsr":
                st.gsr_norm = float(value)
                graphable = True
            # bpm and gsr arrive as separate messages; a graph point records
            # the pod's current combined state, at most one per interval --
            # and only once BOTH have arrived at least once, otherwise the
            # first point would plot the not-yet-received one as 0.
            if (graphable and 'bpm' in st.seen and 'gsr' in st.seen
                    and (st.last_seen - st.last_hist) >= HISTORY_INTERVAL_S):
                st.last_hist = st.last_seen
                st.history.append(Sample(t=st.last_seen, bpm=st.bpm, gsr=st.gsr_norm))

    @staticmethod
    def _pod_dict(st: PodState, now: float, with_history: bool) -> dict:
        age = now - st.last_seen
        d = {
            "age": round(age, 2),
            "status": "OFFLINE" if age > STALE_AFTER_S else "ok",
            "ip": st.ip,
            "bpm": st.bpm,
            "ibi_ms": st.ibi_ms,
            "signal": st.signal,
            "gsr_raw": st.gsr_raw,
            "gsr_norm": round(st.gsr_norm, 3),
        }
        if with_history:
            hist = list(st.history)
            # Negative seconds-ago, computed server-side so the browser never
            # has to reason about clock skew: oldest on the left, ~0 = now.
            d["history"] = {
                "t": [-round(now - s.t, 2) for s in hist],
                "bpm": [s.bpm for s in hist],
                "gsr": [round(s.gsr, 3) for s in hist],
            }
        return d

    def snapshot(self, with_history: bool = True) -> dict:
        now = time.time()
        with self.lock:
            return {pid: self._pod_dict(st, now, with_history) for pid, st in self.pods.items()}

    def pod(self, pod_id: int):
        now = time.time()
        with self.lock:
            st = self.pods.get(pod_id)
            return None if st is None else self._pod_dict(st, now, True)

    def run_osc_server(self) -> None:
        disp = Dispatcher()
        disp.set_default_handler(self.on_message, needs_reply_address=True)
        server = ThreadingOSCUDPServer(("0.0.0.0", self.osc_port), disp)
        threading.Thread(target=server.serve_forever, daemon=True).start()


# ---------------------------------------------------------------------------
# The page. Plain string (no templating) -- everything dynamic comes from the
# /api/* endpoints. System fonts only, no external URLs.
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>cosmic dune</title>
<script src="/chart.js"></script>
<style>
  :root { --bg:#14161a; --panel:#1c1f25; --line:#2a2d33; --text:#e8e8e8; --dim:#8a93a0;
          --ok:#4cd964; --bad:#ff5d5d; --warn:#ffb13b; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { margin:0; height:100%; overflow:hidden; background:var(--bg); color:var(--text);
    font-family: system-ui, "Segoe UI", Roboto, "DejaVu Sans", Helvetica, Arial, sans-serif;
    -webkit-user-select:none; user-select:none; touch-action:manipulation; }
  .view { display:none; height:100%; flex-direction:column; }
  .view.active { display:flex; }

  .topbar { height:48px; flex:none; display:flex; align-items:center; gap:12px; padding:0 12px;
            border-bottom:1px solid var(--line); }
  .title { font-size:18px; font-weight:700; letter-spacing:.06em; }
  .count { font-size:15px; color:var(--dim); }
  .srv { margin-left:auto; font-size:13px; color:var(--warn); }

  #grid { flex:1; min-height:0; display:grid; gap:8px; padding:8px;
          grid-template-columns:repeat(3, 1fr); grid-template-rows:repeat(2, 1fr); }
  .foot { height:24px; flex:none; padding:0 12px; display:flex; align-items:center;
          font-size:11px; color:var(--dim); white-space:nowrap; overflow:hidden; text-overflow:ellipsis;
          border-top:1px solid var(--line); }

  .tile { appearance:none; -webkit-appearance:none; font:inherit; color:inherit; text-align:left;
          background:var(--panel); border:2px solid var(--line); border-radius:12px; padding:8px 12px;
          display:grid; grid-template-rows:auto 1fr auto auto; min-width:0; min-height:0; overflow:hidden;
          cursor:pointer; }
  .tile:disabled { color:inherit; cursor:default; }
  .tile:active:not(:disabled) { transform:scale(.97); }
  .tile.ok { border-color:#2f6b3f; }
  .tile.nopulse { border-color:#8a6a24; }
  .tile.offline { border-color:#6b2f2f; opacity:.55; }
  .tile.nodata { opacity:.3; }
  .t-head { display:flex; justify-content:space-between; align-items:center;
            font-size:max(14px, 3.6vh); font-weight:700; letter-spacing:.05em; color:#cfd6df; }
  .dot { width:max(10px, 2.4vh); height:max(10px, 2.4vh); border-radius:50%; background:var(--dim); }
  .ok .dot { background:var(--ok); } .nopulse .dot { background:var(--warn); } .offline .dot { background:var(--bad); }
  .t-mid { align-self:center; display:flex; align-items:baseline; gap:6px; }
  .t-bpm { font-size:min(15vh, 13vw); font-weight:800; line-height:1; font-variant-numeric:tabular-nums; }
  .t-unit { font-size:max(12px, 2.8vh); color:var(--dim); }
  .t-tag { font-size:max(11px, 2.6vh); min-height:1.2em; color:var(--dim); }
  .t-tag.warn { color:var(--warn); } .t-tag.bad { color:var(--bad); }
  .t-gsr { display:flex; align-items:center; gap:8px; font-size:max(11px, 2.6vh); color:var(--dim);
           font-variant-numeric:tabular-nums; }
  .meter { flex:1; height:max(8px, 1.8vh); background:#2a2d33; border-radius:99px; overflow:hidden; }
  .meter > i { display:block; height:100%; width:0; border-radius:99px; transition:width .4s linear; }

  .back { appearance:none; -webkit-appearance:none; font:inherit; color:inherit; background:#262a31;
          border:1px solid var(--line); border-radius:10px; min-height:40px; padding:0 16px 0 10px;
          display:flex; align-items:center; gap:4px; font-size:16px; font-weight:600; cursor:pointer; }
  .back:active { background:#323843; }
  .d-title { font-size:22px; font-weight:800; letter-spacing:.05em; }
  .chip { font-size:13px; font-weight:700; padding:3px 10px; border-radius:99px; background:#2a2d33; color:var(--dim); }
  .chip.good { background:#1f4d2c; color:#9bf0b0; } .chip.warn { background:#5a4412; color:#ffd58a; }
  .chip.bad { background:#5a2020; color:#ffadad; }
  .d-body { flex:1; min-height:0; display:flex; gap:8px; padding:8px; }
  .d-charts { flex:1; min-width:0; display:flex; flex-direction:column; gap:8px; }
  .chartbox { flex:1; min-height:0; position:relative; background:var(--panel); border-radius:12px; padding:6px 8px; }
  .warnbox { flex:1; display:flex; align-items:center; justify-content:center; text-align:center;
             background:var(--panel); border-radius:12px; padding:16px; color:var(--warn); font-size:14px; }
  .d-stats { width:236px; flex:none; background:var(--panel); border-radius:12px; padding:6px 12px;
             display:flex; flex-direction:column; justify-content:space-between; }
  .row { display:flex; justify-content:space-between; align-items:baseline; padding:3px 0;
         border-bottom:1px solid var(--line); }
  .row:last-child { border-bottom:none; }
  .row .k { font-size:13px; color:var(--dim); }
  .row .v { font-size:19px; font-weight:700; font-variant-numeric:tabular-nums; }
  .row .v.big { font-size:44px; font-weight:800; line-height:1.05; }
  .row .v.small { font-size:16px; }
  .row.meterrow { flex-direction:column; align-items:stretch; gap:4px; }
  .row.meterrow .top { display:flex; justify-content:space-between; align-items:baseline; }
</style>
</head>
<body>

<section id="grid-view" class="view active">
  <header class="topbar">
    <div class="title">COSMIC DUNE</div>
    <div id="count" class="count">0 / 6 online</div>
    <div id="srv" class="srv"></div>
  </header>
  <main id="grid"></main>
  <footer id="foot" class="foot"></footer>
</section>

<section id="detail-view" class="view">
  <header class="topbar">
    <button id="back" class="back" type="button" aria-label="Back">
      <svg viewBox="0 0 24 24" width="22" height="22" aria-hidden="true">
        <path d="M15 5l-7 7 7 7" fill="none" stroke="currentColor" stroke-width="3"
              stroke-linecap="round" stroke-linejoin="round"/>
      </svg>
      <span>Back</span>
    </button>
    <div id="d-title" class="d-title">POD</div>
    <div id="d-status" class="chip">--</div>
  </header>
  <main class="d-body">
    <div id="charts" class="d-charts">
      <div class="chartbox"><canvas id="bpmChart"></canvas></div>
      <div class="chartbox"><canvas id="gsrChart"></canvas></div>
    </div>
    <div id="chartWarn" class="warnbox" style="display:none">
      Graphs unavailable: chart.umd.js is missing next to osc_web_dashboard.py.
      The numbers on the right still update.
    </div>
    <aside class="d-stats">
      <div class="row"><span class="k">BPM</span><span id="s-bpm" class="v big">--</span></div>
      <div class="row"><span class="k">IBI</span><span id="s-ibi" class="v">--</span></div>
      <div class="row"><span class="k">Pulse signal</span><span id="s-sig" class="v">--</span></div>
      <div class="row"><span class="k">GSR raw</span><span id="s-raw" class="v">--</span></div>
      <div class="row meterrow">
        <div class="top"><span class="k">GSR level</span><span id="s-norm" class="v">--</span></div>
        <div class="meter"><i id="s-fill"></i></div>
      </div>
      <div class="row"><span class="k">IP</span><span id="s-ip" class="v small">--</span></div>
      <div class="row"><span class="k">Last packet</span><span id="s-age" class="v small">--</span></div>
    </aside>
  </main>
</section>

<script>
(() => {
'use strict';
const SLOTS = [1, 2, 3, 4, 5, 6];
const $ = id => document.getElementById(id);

// Same 4-stop gradient the pod's LED strip uses: white -> blue -> purple -> red.
const WHITE = [255, 255, 255], BLUE = [60, 120, 255], PURPLE = [160, 30, 220], RED = [255, 0, 0];
const mix = (a, b, t) => a.map((v, i) => Math.round(v + (b[i] - v) * t));
function gsrColor(t) {
  t = Math.max(0, Math.min(1, t));
  const c = t < 1 / 3 ? mix(WHITE, BLUE, t * 3)
          : t < 2 / 3 ? mix(BLUE, PURPLE, (t - 1 / 3) * 3)
          :             mix(PURPLE, RED, (t - 2 / 3) * 3);
  return 'rgb(' + c[0] + ',' + c[1] + ',' + c[2] + ')';
}
const fmtAge = s => (s < 60 ? Math.round(s) + 's' : Math.floor(s / 60) + 'm');

let view = 'grid', selected = null, busy = false, failCount = 0;
let bpmChart = null, gsrChart = null;
const tiles = {};

// ---- grid ----------------------------------------------------------------
for (const id of SLOTS) {
  const b = document.createElement('button');
  b.type = 'button'; b.className = 'tile nodata'; b.disabled = true;
  b.innerHTML =
    '<div class="t-head"><span>POD ' + id + '</span><span class="dot"></span></div>' +
    '<div class="t-mid"><span class="t-bpm">--</span><span class="t-unit">bpm</span></div>' +
    '<div class="t-tag">no data</div>' +
    '<div class="t-gsr"><span class="meter"><i></i></span><span class="t-gsrv">--</span></div>';
  b.addEventListener('click', () => openDetail(id));
  $('grid').appendChild(b);
  tiles[id] = { root: b, bpm: b.querySelector('.t-bpm'), tag: b.querySelector('.t-tag'),
                fill: b.querySelector('.meter i'), gsr: b.querySelector('.t-gsrv') };
}

function renderTiles(pods) {
  let online = 0;
  for (const id of SLOTS) {
    const t = tiles[id], p = pods[id];
    if (!p) {
      t.root.className = 'tile nodata'; t.root.disabled = true;
      t.bpm.textContent = '--'; t.tag.textContent = 'no data'; t.tag.className = 't-tag';
      t.fill.style.width = '0'; t.gsr.textContent = '--';
      continue;
    }
    const off = p.status === 'OFFLINE';
    if (!off) online++;
    t.root.disabled = false;
    t.root.className = 'tile ' + (off ? 'offline' : (p.signal ? 'ok' : 'nopulse'));
    t.bpm.textContent = p.bpm > 0 ? p.bpm : '--';
    t.tag.textContent = off ? 'OFFLINE ' + fmtAge(p.age) : (p.signal ? '' : 'no pulse');
    t.tag.className = 't-tag ' + (off ? 'bad' : 'warn');
    t.fill.style.width = Math.round(p.gsr_norm * 100) + '%';
    t.fill.style.background = gsrColor(p.gsr_norm);
    t.gsr.textContent = p.gsr_norm.toFixed(2);
  }
  $('count').textContent = online + ' / ' + SLOTS.length + ' online';
}

// ---- detail --------------------------------------------------------------
function mkChart(canvasId, title, yMin, yMax, step, color) {
  return new Chart($(canvasId), {
    type: 'line',
    data: { datasets: [{ data: [], borderColor: color, borderWidth: 2, pointRadius: 0, tension: 0.25, fill: false }] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false, parsing: false, normalized: true,
      events: [],                      // touch screen: no hover/tooltips, saves CPU
      layout: { padding: { right: 6 } },
      scales: {
        x: { type: 'linear', min: -60, max: 0,
             ticks: { color: '#8a93a0', font: { size: 10 }, maxTicksLimit: 7 }, grid: { color: '#2a2d33' } },
        y: { min: yMin, max: yMax,
             ticks: { color: '#8a93a0', font: { size: 10 }, stepSize: step, maxTicksLimit: 8 }, grid: { color: '#2a2d33' } }
      },
      plugins: { legend: { display: false }, tooltip: { enabled: false },
                 title: { display: true, text: title, color: '#cfd6df', font: { size: 12 },
                          align: 'start', padding: { top: 0, bottom: 4 } } }
    }
  });
}

function makeCharts() {
  const ok = typeof Chart !== 'undefined';
  $('charts').style.display = ok ? 'flex' : 'none';
  $('chartWarn').style.display = ok ? 'none' : 'flex';
  if (!ok) return;
  bpmChart = mkChart('bpmChart', 'BPM  -  last 60 s', 30, 180, 30, '#ff5d73');
  gsrChart = mkChart('gsrChart', 'GSR  -  last 60 s', 0, 1, 0.2, '#6aa7ff');
}
function destroyCharts() {
  if (bpmChart) { bpmChart.destroy(); bpmChart = null; }
  if (gsrChart) { gsrChart.destroy(); gsrChart = null; }
}

function openDetail(id) {
  selected = id; view = 'detail';
  $('grid-view').classList.remove('active');
  $('detail-view').classList.add('active');
  $('d-title').textContent = 'POD ' + id;
  makeCharts();
  poll();
}
function closeDetail() {
  destroyCharts();
  selected = null; view = 'grid';
  $('detail-view').classList.remove('active');
  $('grid-view').classList.add('active');
  poll();
}

function renderDetail(p) {
  const off = p.status === 'OFFLINE';
  const chip = $('d-status');
  chip.textContent = off ? 'OFFLINE' : (p.signal ? 'online' : 'no pulse');
  chip.className = 'chip ' + (off ? 'bad' : (p.signal ? 'good' : 'warn'));
  $('s-bpm').textContent = p.bpm > 0 ? p.bpm : '--';
  $('s-ibi').textContent = p.ibi_ms + ' ms';
  $('s-sig').textContent = p.signal ? 'yes' : 'no';
  $('s-raw').textContent = p.gsr_raw;
  $('s-norm').textContent = p.gsr_norm.toFixed(2);
  $('s-fill').style.width = Math.round(p.gsr_norm * 100) + '%';
  $('s-fill').style.background = gsrColor(p.gsr_norm);
  $('s-ip').textContent = p.ip || '--';
  $('s-age').textContent = p.age.toFixed(1) + ' s ago';
  if (bpmChart) {
    bpmChart.data.datasets[0].data = p.history.t.map((x, i) => ({ x: x, y: p.history.bpm[i] }));
    bpmChart.update('none');
  }
  if (gsrChart) {
    gsrChart.data.datasets[0].data = p.history.t.map((x, i) => ({ x: x, y: p.history.gsr[i] }));
    gsrChart.update('none');
  }
}

// ---- polling -------------------------------------------------------------
async function poll() {
  if (busy) return;
  busy = true;
  const mode = view, sel = selected;
  try {
    const res = await fetch(mode === 'grid' ? '/api/summary' : '/api/pod/' + sel, { cache: 'no-store' });
    if (mode !== view || sel !== selected) return;   // user navigated while we waited
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const json = await res.json();
    failCount = 0; $('srv').textContent = '';
    if (mode === 'grid') renderTiles(json.pods); else renderDetail(json);
  } catch (e) {
    if (++failCount >= 3) $('srv').textContent = 'server not responding';
  } finally {
    busy = false;
  }
}

async function loadInfo() {
  try {
    const r = await fetch('/api/info', { cache: 'no-store' });
    const info = await r.json();
    $('foot').textContent = 'Pods: ' + info.ips.map(x =>
      'net hub ' + x.ip + ' ' + info.osc_port + ' (' + x.name + ')').join('   |   ');
  } catch (e) { /* footer is a convenience, not essential */ }
}

$('back').addEventListener('click', closeDetail);
document.addEventListener('keydown', e => { if (e.key === 'Escape' && view === 'detail') closeDetail(); });
document.addEventListener('contextmenu', e => e.preventDefault());   // no long-press menu on the touch screen

loadInfo();
poll();
setInterval(poll, 1000);
})();
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser(description="cosmic dune OSC web dashboard")
    ap.add_argument("--osc-port", type=int, default=9000, help="UDP port to receive pod telemetry on")
    ap.add_argument("--web-port", type=int, default=5000, help="port to serve the dashboard web page on")
    args = ap.parse_args()

    dashboard = Dashboard(args.osc_port)
    dashboard.run_osc_server()

    local_ips = get_local_ips()
    have_chart = os.path.exists(CHART_JS_FILE)
    print("cosmic dune -- OSC web dashboard")
    print(f"Listening for pod telemetry on UDP :{args.osc_port} (all interfaces)")
    print("This machine's addresses -- on each pod's CLI use the one on the pod's own network, then `net save`:")
    for name, ip in local_ips:
        print(f"    {name:<14} net hub {ip} {args.osc_port}    (dashboard: http://{ip}:{args.web_port}/)")
    if not have_chart:
        print(f"\nNOTE: {CHART_JS_FILE} not found -- the graphs will be unavailable "
              "(no CDN fallback, by design: this must work offline). Tiles and numbers still work.")

    app = Flask(__name__)

    @app.route("/")
    def index():
        return Response(PAGE, mimetype="text/html")

    @app.route("/chart.js")
    def chart_js():
        if not have_chart:
            abort(404)
        return send_file(CHART_JS_FILE, mimetype="application/javascript")

    @app.route("/api/info")
    def api_info():
        return jsonify({"osc_port": args.osc_port,
                        "ips": [{"name": n, "ip": i} for n, i in local_ips],
                        "chart": have_chart})

    @app.route("/api/summary")
    def api_summary():
        return jsonify({"pods": dashboard.snapshot(with_history=False)})

    @app.route("/api/pod/<int:pod_id>")
    def api_pod(pod_id):
        d = dashboard.pod(pod_id)
        if d is None:
            abort(404)
        return jsonify(d)

    @app.route("/api/data")
    def api_data():
        # Everything including history -- kept for debugging with curl.
        return jsonify({"pods": dashboard.snapshot()})

    app.run(host="0.0.0.0", port=args.web_port)


if __name__ == "__main__":
    main()