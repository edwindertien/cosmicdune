#!/usr/bin/env python3
"""
cosmic dune -- OSC web dashboard

A graphical alternative to osc_test_hub.py's terminal `monitor` mode:
listens for pods' OSC telemetry and serves a live-updating web page with
BPM/GSR graphs per pod, instead of an ASCII table. Point a browser at it
from any machine on the network, not just the one running this script.

Reuses the exact same message format and OSC-receiving approach as
osc_test_hub.py (a single default handler parsing "/pod/<id>/<leaf>" --
see that script for why, re: python-osc wildcard-matching uncertainty).
osc_test_hub.py's `simulate` mode works unchanged as a fake data source
for this dashboard too -- both scripts just listen on the same UDP port.

Usage:
    python3 osc_web_dashboard.py [--osc-port 9000] [--web-port 5000]

Then, in another terminal, either point real pods at this machine (see
the startup banner for the exact `net hub` command), or feed it fake data
for testing:
    python3 osc_test_hub.py simulate --port 9000

Requires: pip install flask python-osc   (see requirements.txt)
"""
import argparse
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from flask import Flask, jsonify, render_template_string
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer

# A pod not heard from in this long is shown OFFLINE -- same reasoning and
# rough magnitude as osc_test_hub.py's STALE_AFTER_S.
STALE_AFTER_S = 3.0
# How many recent (bpm-or-gsr) samples to keep per pod for the graphs.
# At a pod's default 15Hz send rate this is roughly the last 25-30s;
# not meant to be a precise window, just enough to see a trend.
HISTORY_LEN = 400


def get_local_ip() -> str:
    """Best-effort guess at this machine's LAN IP -- see osc_test_hub.py's
    copy of this function for the full explanation; duplicated here
    rather than shared so this script stays independently runnable."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


@dataclass
class Sample:
    t: float
    bpm: int
    gsr: float


@dataclass
class PodState:
    last_seen: float = 0.0
    ip: str = ""
    bpm: int = 0
    ibi_ms: int = 0
    signal: bool = False
    gsr_raw: int = 0
    gsr_norm: float = 0.0
    history: deque = field(default_factory=lambda: deque(maxlen=HISTORY_LEN))


class Dashboard:
    """Holds all pod state and does the OSC receiving -- Flask's routes
    below just read from this under its lock. One instance, created in
    main() and closed over by the Flask routes."""

    def __init__(self, osc_port: int):
        self.osc_port = osc_port
        self.pods: dict[int, PodState] = {}
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
            st.ip = client_address[0]  # source IP -- lets you tell which
                                         # physical pod is reporting as
                                         # which id, e.g. if two pods ever
                                         # end up sharing a `net pod` value
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
            # A bpm-or-gsr update appends one history point using the
            # pod's current combined state -- bpm and gsr arrive as
            # separate messages, not bundled together, so this is an
            # approximation (whichever just changed, plus whatever the
            # other one's last known value was), good enough for a
            # debugging graph.
            if graphable:
                st.history.append(Sample(t=st.last_seen, bpm=st.bpm, gsr=st.gsr_norm))

    def snapshot(self) -> dict:
        """Everything the /api/data route needs, computed once against a
        single consistent `now` so every pod's ages and every sample's
        relative offset agree with each other."""
        now = time.time()
        with self.lock:
            pods = {}
            for pod_id, st in self.pods.items():
                hist = list(st.history)
                pods[pod_id] = {
                    "age": round(now - st.last_seen, 2),
                    "status": "OFFLINE" if (now - st.last_seen) > STALE_AFTER_S else "ok",
                    "ip": st.ip,
                    "bpm": st.bpm,
                    "ibi_ms": st.ibi_ms,
                    "signal": st.signal,
                    "gsr_raw": st.gsr_raw,
                    "gsr_norm": st.gsr_norm,
                    "history": {
                        # Negative seconds-ago, so charting x ascending
                        # (most-negative/oldest on the left) reads as time
                        # moving left-to-right, ending at ~0 (now) -- all
                        # computed server-side so the browser never needs
                        # to reason about clock skew.
                        "t": [-round(now - s.t, 2) for s in hist],
                        "bpm": [s.bpm for s in hist],
                        "gsr": [s.gsr for s in hist],
                    },
                }
        return pods

    def run_osc_server(self) -> None:
        disp = Dispatcher()
        disp.set_default_handler(self.on_message, needs_reply_address=True)
        server = ThreadingOSCUDPServer(("0.0.0.0", self.osc_port), disp)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()


PAGE = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>cosmic dune -- OSC dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
<style>
  body { font-family: -apple-system, Helvetica, Arial, sans-serif; background: #14161a; color: #e8e8e8; margin: 0; padding: 20px; }
  h1 { font-size: 18px; font-weight: 600; margin: 0 0 4px; }
  .banner { color: #9aa; font-size: 13px; margin-bottom: 18px; }
  .banner code { background: #22252b; padding: 2px 6px; border-radius: 4px; color: #ffd479; }
  .charts { display: flex; gap: 20px; flex-wrap: wrap; margin-bottom: 20px; }
  .chart-box { background: #1c1f25; border-radius: 8px; padding: 12px; flex: 1 1 460px; min-width: 320px; }
  canvas { max-height: 280px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: right; padding: 6px 10px; border-bottom: 1px solid #2a2d33; }
  th:first-child, td:first-child { text-align: left; }
  th:nth-child(2), td:nth-child(2) { text-align: left; }
  .ok { color: #6fd66f; }
  .offline { color: #e05a5a; font-weight: 600; }
  .empty { color: #778; padding: 10px 0; }
</style>
</head>
<body>
  <h1>cosmic dune -- OSC dashboard</h1>
  <div class="banner">
    Listening on UDP :{{ osc_port }} &nbsp;|&nbsp; this machine's IP: <code>{{ local_ip }}</code>
    &nbsp;|&nbsp; on each pod's CLI: <code>net hub {{ local_ip }} {{ osc_port }}</code>
  </div>

  <div class="charts">
    <div class="chart-box"><canvas id="bpmChart"></canvas></div>
    <div class="chart-box"><canvas id="gsrChart"></canvas></div>
  </div>

  <table id="statusTable">
    <thead>
      <tr><th>pod</th><th>ip</th><th>age</th><th>status</th><th>bpm</th><th>ibi (ms)</th><th>signal</th><th>gsr raw</th><th>gsr norm</th></tr>
    </thead>
    <tbody><tr><td colspan="9" class="empty">(no pods heard from yet)</td></tr></tbody>
  </table>

<script>
const COLORS = ['#e6194b','#3cb44b','#4363d8','#f58231','#911eb4','#46c0c0','#f032e6','#bcbd22'];
function colorFor(podId) { return COLORS[(podId - 1) % COLORS.length]; }

function ensureDataset(chart, podId) {
  let ds = chart.data.datasets.find(d => d.podId === podId);
  if (!ds) {
    ds = { podId: podId, label: 'Pod ' + podId, data: [], borderColor: colorFor(podId),
           backgroundColor: colorFor(podId), fill: false, tension: 0.2, pointRadius: 0, borderWidth: 2 };
    chart.data.datasets.push(ds);
  }
  return ds;
}

function makeChart(canvasId, yTitle, yMin, yMax) {
  const yScale = { title: { display: true, text: yTitle, color: '#ccc' }, ticks: { color: '#aaa' }, grid: { color: '#2a2d33' } };
  if (yMin !== undefined) yScale.min = yMin;
  if (yMax !== undefined) yScale.max = yMax;
  return new Chart(document.getElementById(canvasId), {
    type: 'line',
    data: { datasets: [] },
    options: {
      responsive: true,
      animation: false,
      parsing: false,
      scales: {
        x: { type: 'linear', title: { display: true, text: 'seconds ago', color: '#ccc' }, ticks: { color: '#aaa' }, grid: { color: '#2a2d33' } },
        y: yScale,
      },
      plugins: { legend: { labels: { color: '#ddd' } } },
    },
  });
}

let bpmChart, gsrChart;

async function poll() {
  let json;
  try {
    const res = await fetch('/api/data');
    json = await res.json();
  } catch (e) {
    return; // server hiccup -- try again next tick, don't crash the page
  }
  const pods = json.pods;
  const ids = Object.keys(pods).map(Number).sort((a, b) => a - b);

  const tbody = document.querySelector('#statusTable tbody');
  if (ids.length === 0) {
    tbody.innerHTML = '<tr><td colspan="9" class="empty">(no pods heard from yet)</td></tr>';
  } else {
    tbody.innerHTML = '';
    for (const id of ids) {
      const p = pods[id];
      const tr = document.createElement('tr');
      const statusClass = p.status === 'OFFLINE' ? 'offline' : 'ok';
      tr.innerHTML = `<td>${id}</td><td>${p.ip}</td><td>${p.age.toFixed(1)}s</td><td class="${statusClass}">${p.status}</td>` +
        `<td>${p.bpm}</td><td>${p.ibi_ms}</td><td>${p.signal ? 'yes' : 'no'}</td>` +
        `<td>${p.gsr_raw}</td><td>${p.gsr_norm.toFixed(2)}</td>`;
      tbody.appendChild(tr);

      const bpmDs = ensureDataset(bpmChart, id);
      bpmDs.data = p.history.t.map((x, i) => ({x: x, y: p.history.bpm[i]}));

      const gsrDs = ensureDataset(gsrChart, id);
      gsrDs.data = p.history.t.map((x, i) => ({x: x, y: p.history.gsr[i]}));
    }
  }
  bpmChart.update('none');
  gsrChart.update('none');
}

window.addEventListener('DOMContentLoaded', () => {
  bpmChart = makeChart('bpmChart', 'BPM', 30, 180);
  gsrChart = makeChart('gsrChart', 'GSR (normalized)', 0, 1);
  poll();
  setInterval(poll, 1000);
});
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

    local_ip = get_local_ip()
    print("cosmic dune -- OSC web dashboard")
    print(f"Listening for pod telemetry on UDP :{args.osc_port}")
    print(f"This machine's IP: {local_ip}")
    print(f"On each pod's CLI, run:\n    net hub {local_ip} {args.osc_port}\n    net save")
    print(f"\nOpen http://{local_ip}:{args.web_port}/  (or http://localhost:{args.web_port}/ on this machine)")

    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template_string(PAGE, osc_port=args.osc_port, local_ip=local_ip)

    @app.route("/api/data")
    def api_data():
        return jsonify({"pods": dashboard.snapshot()})

    app.run(host="0.0.0.0", port=args.web_port)


if __name__ == "__main__":
    main()