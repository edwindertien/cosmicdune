#!/usr/bin/env python3
"""
cosmic dune -- the hub: OSC dashboard + laser visualiser/controls (touch screen, fully offline)

Listens for pods' OSC telemetry and serves ONE live web page designed for a small 800x480 touch
screen (run the browser fullscreen/kiosk):

  * LEFT, always visible: the LASER VISUALISER -- a live drawing of exactly the frames the laser
    engine hands to the ILDA DAC (same points, after calibration), with a safe/armed indicator.
  * RIGHT, two tabs:  PODS -- six compact status tiles (BPM, GSR meter in the LED strip's colours,
    online/offline/no-pulse);  LASER -- a scene/pattern selector (buttons), SIZE / LEVEL / SYNC
    sliders, the pod source (LIVE / FILL / DEMO) and ARM / BLACKOUT.
  * Tap a pod (tile or header pill) for the full-screen detail: BPM and GSR graphs (last 60 s)
    plus IBI, raw GSR, IP address and time since the last packet.

DUMMY MODE: the source DEMO feeds the scenes with six simulated pods, and the test patterns need
no pods at all -- so every pattern can be tried (and watched in the visualiser) with no pod
connected, and with no laser at all (default: PREVIEW, nothing is sent anywhere).

LASER OUTPUT is off unless the hub is started with --laser. Even then it starts SAFE (dark) and
only emits after ARM (two taps on the screen). The limits (--max-intensity, --max-size) are
enforced on the server, not in the page. With --laser, controls are accepted only from this
machine's own screen unless --allow-remote-control is given (the hotspot is open: anyone on it
can load this page, but they get a view-only visualiser). See laser/engine.py for the full model.

Works with no internet at all: nothing is loaded from a CDN or font host.
Chart.js is served from chart.umd.js, which must sit next to this script.
If that file is missing, the tiles and numbers still work; only the graphs
are unavailable (the page says so).

Same message format as osc_test_hub.py (a single default handler parsing
"/pod/<id>/<leaf>"). osc_test_hub.py's `simulate` mode works unchanged as a
fake data source for this dashboard.

Usage:
    python3 osc_web_dashboard.py                        # dashboard + visualiser, NO laser (preview)
    python3 osc_web_dashboard.py --laser                # + drive the Helios DAC (starts SAFE; ARM on screen)
    python3 osc_web_dashboard.py --osc-port 9000 --web-port 5000
    python3 osc_test_hub.py simulate --port 9000        # fake pods, for testing
    (calibration/shape options are the same as laser/laser_test.py: --size --intensity --pps --rgb-min ...)

Requires: pip install flask python-osc   (see requirements.txt)
"""
import argparse
import ipaddress
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from flask import Flask, Response, abort, jsonify, request, send_file
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer

# Chart.js is served from a copy next to this script. There is deliberately
# NO CDN fallback: this has to work with no internet (a Pi running its own
# hotspot usually has none).
HERE = os.path.dirname(os.path.abspath(__file__))
CHART_JS_FILE = os.path.join(HERE, "chart.umd.js")

# The laser engine lives in laser/ next to this file. The dashboard still runs without it (no
# visualiser, no controls) -- the page says so.
LASER_DIR = os.path.join(HERE, "laser")
LT = LE = LV = LP = None
HeliosDAC = HeliosError = None
LASER_IMPORT_ERROR = ""
try:
    sys.path.insert(0, LASER_DIR)
    import laser_test as LT          # shared option definitions (--size, --rgb-min, ...)
    import engine as LE
    import patterns as LP
    import visuals as LV
    from helios_dac import HeliosDAC, HeliosError
except Exception as _e:              # folder missing, or an import problem: degrade, don't crash
    LT = LE = LV = LP = None
    LASER_IMPORT_ERROR = f"{type(_e).__name__}: {_e}"

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
        # ONE thread receives every datagram in turn (the handler only updates a dict). The threading server made a new
        # thread per datagram -- ~450 a second with six pods -- which cost 2.6-2.9x the CPU on an x86 core, far more on a Pi.
        server = BlockingOSCUDPServer(("0.0.0.0", self.osc_port), disp)
        threading.Thread(target=server.serve_forever, daemon=True, name="osc").start()


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
          --ok:#4cd964; --bad:#ff5d5d; --warn:#ffb13b; --acc:#4a76b4; }
  * { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
  html, body { margin:0; height:100%; overflow:hidden; background:var(--bg); color:var(--text);
    font-family: system-ui, "Segoe UI", Roboto, "DejaVu Sans", Helvetica, Arial, sans-serif;
    -webkit-user-select:none; user-select:none; touch-action:manipulation; }
  button { font:inherit; color:inherit; cursor:pointer; -webkit-appearance:none; appearance:none; }
  .view { display:none; height:100%; flex-direction:column; }
  .view.active { display:flex; }

  .topbar { height:48px; flex:none; display:flex; align-items:center; gap:8px; padding:0 8px 0 12px;
            border-bottom:1px solid var(--line); }
  .title { font-size:15px; font-weight:800; letter-spacing:.07em; white-space:nowrap; }
  .srv { font-size:11px; color:var(--warn); white-space:nowrap; }
  .pills { display:flex; gap:4px; margin-left:6px; }
  .pill { height:34px; min-width:54px; padding:0 7px; display:flex; align-items:center; gap:5px; font-size:11px;
          background:var(--panel); border:2px solid var(--line); border-radius:8px; color:var(--dim); }
  .pill b { font-size:15px; font-weight:800; color:var(--text); font-variant-numeric:tabular-nums; }
  .pill .pdot { width:8px; height:8px; border-radius:50%; background:var(--dim); flex:none; }
  .pill.ok { border-color:#2f6b3f; } .pill.ok .pdot { background:var(--ok); }
  .pill.nopulse { border-color:#8a6a24; } .pill.nopulse .pdot { background:var(--warn); }
  .pill.offline { border-color:#6b2f2f; opacity:.55; } .pill.offline .pdot { background:var(--bad); }
  .pill.nodata { opacity:.3; }
  .tabs { margin-left:auto; display:flex; gap:4px; }
  .tab { height:34px; min-width:66px; padding:0 10px; border-radius:8px; border:1px solid var(--line);
         background:#262a31; color:var(--dim); font-size:12px; font-weight:800; letter-spacing:.06em; }
  .tab.active { background:#33507a; color:#fff; border-color:var(--acc); }

  .split { flex:1; min-height:0; display:flex; gap:8px; padding:8px; }
  .viz { position:relative; flex:none; height:100%; aspect-ratio:1 / 1; background:#0c0e12;
         border:1px solid var(--line); border-radius:10px; overflow:hidden; }
  .viz canvas { width:100%; height:100%; display:block; }
  .vchip { position:absolute; left:8px; top:8px; padding:3px 9px; border-radius:99px; font-size:11px; font-weight:800;
           letter-spacing:.05em; background:#2a2d33; color:var(--dim); }
  .vchip.warn { background:#5a4412; color:#ffd58a; } .vchip.bad { background:#6b2020; color:#ffc2c2; }
  .vchip.armed { background:#b02020; color:#fff; animation:pulse 1s infinite; }
  @keyframes pulse { 50% { opacity:.65; } }
  .vbtn { position:absolute; right:8px; top:8px; height:28px; padding:0 10px; border-radius:8px; font-size:11px; font-weight:800;
          background:#262a31; border:1px solid var(--line); color:var(--dim); }
  .vbtn.on { background:#33507a; color:#fff; border-color:var(--acc); }
  .vinfo { position:absolute; left:8px; bottom:6px; max-width:calc(100% - 150px); font-size:10px; line-height:1.35; white-space:pre-wrap; color:var(--dim); font-variant-numeric:tabular-nums; }
  .vmsg { position:absolute; left:8px; right:8px; bottom:38px; text-align:center; font-size:11px; color:var(--warn); }
  .vblack { position:absolute; right:8px; bottom:8px; height:44px; padding:0 16px; border-radius:10px; font-size:15px;
            font-weight:900; letter-spacing:.06em; background:#b02020; border:2px solid #ff8a8a; color:#fff; }
  .voff { position:absolute; inset:0; display:flex; align-items:center; justify-content:center; text-align:center;
          padding:20px; color:var(--warn); font-size:13px; background:#0c0e12; }

  .side { flex:1; min-width:0; min-height:0; }
  .panel { display:none; height:100%; }
  .panel.active { display:flex; }
  #grid { width:100%; display:grid; gap:8px; grid-template-columns:1fr 1fr; grid-template-rows:repeat(3, 1fr); }
  .foot { height:20px; flex:none; padding:0 12px; display:flex; align-items:center; font-size:10px; color:var(--dim);
          white-space:nowrap; overflow:hidden; text-overflow:ellipsis; border-top:1px solid var(--line); }

  .tile { text-align:left; background:var(--panel); border:2px solid var(--line); border-radius:10px; padding:6px 10px;
          display:grid; grid-template-rows:auto 1fr auto auto; min-width:0; min-height:0; overflow:hidden; }
  .tile:disabled { color:inherit; cursor:default; }
  .tile:active:not(:disabled) { transform:scale(.97); }
  .tile.ok { border-color:#2f6b3f; } .tile.nopulse { border-color:#8a6a24; }
  .tile.offline { border-color:#6b2f2f; opacity:.55; } .tile.nodata { opacity:.3; }
  .t-head { display:flex; justify-content:space-between; align-items:center; font-size:12px; font-weight:700;
            letter-spacing:.05em; color:#cfd6df; }
  .dot { width:10px; height:10px; border-radius:50%; background:var(--dim); }
  .ok .dot { background:var(--ok); } .nopulse .dot { background:var(--warn); } .offline .dot { background:var(--bad); }
  .t-mid { align-self:center; display:flex; align-items:baseline; gap:5px; }
  .t-bpm { font-size:38px; font-weight:800; line-height:1; font-variant-numeric:tabular-nums; }
  .t-unit { font-size:11px; color:var(--dim); }
  .t-tag { font-size:10px; min-height:12px; color:var(--dim); }
  .t-tag.warn { color:var(--warn); } .t-tag.bad { color:var(--bad); }
  .t-gsr { display:flex; align-items:center; gap:6px; font-size:10px; color:var(--dim); font-variant-numeric:tabular-nums; }
  .meter { flex:1; height:7px; background:#2a2d33; border-radius:99px; overflow:hidden; }
  .meter > i { display:block; height:100%; width:0; border-radius:99px; transition:width .4s linear; }

  #panel-laser { flex-direction:column; gap:5px; width:100%; }
  #panel-laser.ro > * { opacity:.5; pointer-events:none; }
  .seg { display:flex; gap:4px; flex:none; }
  .seg button, .sel button { border:1px solid var(--line); background:#262a31; color:var(--dim); border-radius:7px; font-weight:800;
                             letter-spacing:.04em; text-transform:uppercase; }
  .seg button { flex:1; height:30px; font-size:11px; }
  .seg button.on, .sel button.on { background:#33507a; color:#fff; border-color:var(--acc); }
  .sel { display:grid; grid-template-columns:repeat(3, 1fr); gap:4px; flex:none; }
  .sel button { height:26px; font-size:10px; padding:0 2px; }
  .sel button.scene { border-color:#3a557d; color:#b9cdea; }
  .sel button.scene.on { color:#fff; }
  .sl { flex:none; }
  .sl .lab { display:flex; justify-content:space-between; align-items:center; height:19px; font-size:10px; color:var(--dim);
             font-weight:800; letter-spacing:.06em; }
  .sl .lab em { font-style:normal; color:var(--text); font-variant-numeric:tabular-nums; font-weight:700; }
  .sl .lab.first { height:26px; }
  .sl .lab button { height:24px; min-width:48px; padding:0 9px; font-size:10px; font-weight:800; border-radius:7px; background:#262a31;
                    border:1px solid var(--line); color:var(--dim); }
  .sl .lab button.on { color:#fff; background:#33507a; border-color:var(--acc); }
  input[type=range] { -webkit-appearance:none; appearance:none; background:transparent; width:100%; height:24px; margin:0; }
  input[type=range]::-webkit-slider-runnable-track { height:8px; background:#2a2d33; border-radius:4px; }
  input[type=range]::-webkit-slider-thumb { -webkit-appearance:none; width:24px; height:24px; border-radius:50%;
                                            background:#6aa7ff; margin-top:-8px; border:2px solid #0c0e12; }
  input[type=range]::-moz-range-track { height:8px; background:#2a2d33; border-radius:4px; }
  input[type=range]::-moz-range-thumb { width:22px; height:22px; border-radius:50%; background:#6aa7ff; border:2px solid #0c0e12; }
  .act { display:flex; gap:6px; flex:none; margin-top:auto; }
  .act button { flex:1; height:40px; border-radius:10px; font-size:14px; font-weight:900; letter-spacing:.06em; border:2px solid var(--line);
                background:#262a31; color:var(--text); }
  #btn-arm.pending { background:#8a6a24; border-color:#ffd58a; }
  #btn-arm.armed { background:#b02020; border-color:#ff8a8a; }
  #btn-arm:disabled, #btn-black:disabled { opacity:.35; }
  #btn-black { background:#5a2020; border-color:#a04040; }

  .back { background:#262a31; border:1px solid var(--line); border-radius:10px; min-height:40px; padding:0 16px 0 10px;
          display:flex; align-items:center; gap:4px; font-size:16px; font-weight:600; }
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
  .row { display:flex; justify-content:space-between; align-items:baseline; padding:3px 0; border-bottom:1px solid var(--line); }
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

<section id="main" class="view active">
  <header class="topbar">
    <div class="title">COSMIC DUNE</div>
    <div id="pills" class="pills"></div>
    <div class="tabs">
      <button id="tab-pods" class="tab active" type="button">PODS</button>
      <button id="tab-laser" class="tab" type="button">LASER</button>
    </div>
    <div id="srv" class="srv"></div>
  </header>
  <main class="split">
    <section class="viz" id="vizbox">
      <canvas id="viz"></canvas>
      <div id="v-state" class="vchip">...</div>
      <button id="v-zoom" class="vbtn" type="button">ZOOM</button>
      <div id="v-msg" class="vmsg"></div>
      <div id="v-info" class="vinfo"></div>
      <button id="v-black" class="vblack" type="button" style="display:none">BLACKOUT</button>
      <div id="v-off" class="voff" style="display:none"></div>
    </section>
    <section class="side">
      <div id="panel-pods" class="panel active"><div id="grid"></div></div>
      <div id="panel-laser" class="panel">
        <div class="seg" id="src-seg">
          <button type="button" data-src="live">LIVE</button>
          <button type="button" data-src="fill">FILL</button>
          <button type="button" data-src="demo">DEMO</button>
        </div>
        <div class="sel" id="sel-grid"></div>
        <div class="sl"><div class="lab first"><span>SYNC</span><em id="v-sync"></em><button id="sync-mode" type="button">AUTO</button></div>
          <input id="sl-sync" type="range" min="0" max="1" step="0.01" value="0"></div>
        <div class="sl"><div class="lab"><span>SIZE</span><em id="v-size"></em></div>
          <input id="sl-size" type="range" min="0.1" max="0.6" step="0.01" value="0.3"></div>
        <div class="sl"><div class="lab"><span>LEVEL</span><em id="v-int"></em></div>
          <input id="sl-int" type="range" min="0" max="0.3" step="0.005" value="0.15"></div>
        <div class="act">
          <button id="btn-arm" type="button">ARM</button>
          <button id="btn-black" type="button">BLACKOUT</button>
        </div>
      </div>
    </section>
  </main>
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
const sleep = ms => new Promise(r => setTimeout(r, ms));

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
const mmss = s => Math.floor(s / 60) + ':' + String(Math.floor(s % 60)).padStart(2, '0');

let view = 'main', selected = null, busy = false, failCount = 0, tab = 'pods';
let bpmChart = null, gsrChart = null;
const tiles = {}, pills = {};

// ---- pods: compact tiles + header pills -------------------------------------
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
  const p = document.createElement('button');
  p.type = 'button'; p.className = 'pill nodata'; p.disabled = true;
  p.innerHTML = '<span class="pdot"></span><span>' + id + '</span><b>--</b>';
  p.addEventListener('click', () => openDetail(id));
  $('pills').appendChild(p);
  pills[id] = { root: p, bpm: p.querySelector('b') };
}

function renderTiles(pods) {
  for (const id of SLOTS) {
    const t = tiles[id], pl = pills[id], p = pods[id];
    if (!p) {
      t.root.className = 'tile nodata'; t.root.disabled = true;
      t.bpm.textContent = '--'; t.tag.textContent = 'no data'; t.tag.className = 't-tag';
      t.fill.style.width = '0'; t.gsr.textContent = '--';
      pl.root.className = 'pill nodata'; pl.root.disabled = true; pl.bpm.textContent = '--';
      continue;
    }
    const off = p.status === 'OFFLINE';
    const cls = off ? 'offline' : (p.signal ? 'ok' : 'nopulse');
    t.root.disabled = false; t.root.className = 'tile ' + cls;
    t.bpm.textContent = p.bpm > 0 ? p.bpm : '--';
    t.tag.textContent = off ? 'OFFLINE ' + fmtAge(p.age) : (p.signal ? '' : 'no pulse');
    t.tag.className = 't-tag ' + (off ? 'bad' : 'warn');
    t.fill.style.width = Math.round(p.gsr_norm * 100) + '%';
    t.fill.style.background = gsrColor(p.gsr_norm);
    t.gsr.textContent = p.gsr_norm.toFixed(2);
    pl.root.disabled = false; pl.root.className = 'pill ' + cls; pl.bpm.textContent = p.bpm > 0 ? p.bpm : '--';
  }
}

function showTab(t) {
  tab = t;
  $('panel-pods').classList.toggle('active', t === 'pods');
  $('panel-laser').classList.toggle('active', t === 'laser');
  $('tab-pods').classList.toggle('active', t === 'pods');
  $('tab-laser').classList.toggle('active', t === 'laser');
}
$('tab-pods').addEventListener('click', () => showTab('pods'));
$('tab-laser').addEventListener('click', () => showTab('laser'));

// ---- detail (full screen) -----------------------------------------------------
function mkChart(canvasId, title, yMin, yMax, step, color) {
  return new Chart($(canvasId), {
    type: 'line',
    data: { datasets: [{ data: [], borderColor: color, borderWidth: 2, pointRadius: 0, tension: 0.25, fill: false }] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false, parsing: false, normalized: true,
      events: [],
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
  $('main').classList.remove('active');
  $('detail-view').classList.add('active');
  $('d-title').textContent = 'POD ' + id;
  makeCharts();
  poll();
}
function closeDetail() {
  destroyCharts();
  selected = null; view = 'main';
  $('detail-view').classList.remove('active');
  $('main').classList.add('active');
  sizeCanvas();
  vizLoop();
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

// ---- pod polling ----------------------------------------------------------------
async function poll() {
  if (busy) return;
  busy = true;
  const mode = view, sel = selected;
  try {
    const res = await fetch(mode === 'main' ? '/api/summary' : '/api/pod/' + sel, { cache: 'no-store' });
    if (mode !== view || sel !== selected) return;
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const json = await res.json();
    failCount = 0; $('srv').textContent = '';
    if (mode === 'main') renderTiles(json.pods); else renderDetail(json);
  } catch (e) {
    if (++failCount >= 3) $('srv').textContent = 'server not responding';
  } finally {
    busy = false;
  }
}

// ---- laser visualiser: draws the frames the engine sends to the DAC -------------
const cv = $('viz'), ctx = cv.getContext('2d');
let lstate = null, engineOn = true, zoomOn = false, vizSeq = 0, vizRunning = false, lastFrameAt = 0, vizMs = 33;

function sizeCanvas() {
  const r = cv.getBoundingClientRect();
  if (r.width < 50) return;
  cv.width = Math.round(r.width); cv.height = Math.round(r.height);
  ctx.globalCompositeOperation = 'source-over';
  ctx.fillStyle = '#0c0e12'; ctx.fillRect(0, 0, cv.width, cv.height);
}
window.addEventListener('resize', sizeCanvas);

function drawFrame(buf) {
  const W = cv.width, H = cv.height, half = Math.min(W, H) / 2, cx = W / 2, cy = H / 2;
  const dv = new DataView(buf);
  const n = dv.getUint16(4, true);
  const s = lstate;
  const k = (zoomOn && s) ? Math.min(0.92 / Math.max(s.size, 0.05), 6) : 1;
  const maxI = (s ? Math.max(s.intensity, 0.02) : 0.15) * 255;
  const gain = s ? 0.35 + 0.65 * Math.min(1, s.intensity / Math.max(s.limits.max_intensity, 0.01)) : 1;
  ctx.globalCompositeOperation = 'source-over';
  ctx.fillStyle = 'rgba(12,14,18,0.32)';            // persistence of vision: old frames fade
  ctx.fillRect(0, 0, W, H);
  ctx.strokeStyle = '#2a2d33'; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(cx - 6, cy); ctx.lineTo(cx + 6, cy); ctx.moveTo(cx, cy - 6); ctx.lineTo(cx, cy + 6); ctx.stroke();
  ctx.globalCompositeOperation = 'lighter';          // additive, like light
  ctx.lineWidth = 2; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
  let px = 0, py = 0, off = 8, curCol = '', open = false;
  for (let i = 0; i < n; i++, off += 8) {
    const X = cx + ((dv.getUint16(off, true) / 4095) * 2 - 1) * half * k;
    const Y = cy - ((dv.getUint16(off + 2, true) / 4095) * 2 - 1) * half * k;
    const r = dv.getUint8(off + 4), g = dv.getUint8(off + 5), b = dv.getUint8(off + 6);
    if (i > 0 && (r | g | b)) {
      const m = Math.max(r, g, b);
      const f = Math.min(1, gain * (0.35 + 0.65 * m / maxI)) * 255 / m;
      const col = 'rgb(' + Math.round(r * f) + ',' + Math.round(g * f) + ',' + Math.round(b * f) + ')';
      if (col !== curCol) {
        if (open) ctx.stroke();
        ctx.strokeStyle = col; ctx.beginPath(); ctx.moveTo(px, py); curCol = col; open = true;
      } else if (!open) { ctx.beginPath(); ctx.moveTo(px, py); open = true; }
      ctx.lineTo(X, Y);
    } else if (open) { ctx.stroke(); open = false; curCol = ''; }
    px = X; py = Y;
  }
  if (open) ctx.stroke();
}

async function vizLoop() {
  if (vizRunning) return;
  vizRunning = true;
  while (view === 'main' && engineOn) {
    const t0 = performance.now();
    try {
      const r = await fetch('/api/laser/frame.bin?since=' + vizSeq, { cache: 'no-store' });
      if (r.status === 200) {
        vizSeq = Number(r.headers.get('X-Seq'));
        drawFrame(await r.arrayBuffer());
        lastFrameAt = performance.now();
      } else if (r.status !== 204) { await sleep(500); }
    } catch (e) { await sleep(500); }
    const spare = vizMs - (performance.now() - t0);      // the laser has priority over the screen: never draw more often than the hub says
    if (spare > 2) await sleep(spare);
  }
  vizRunning = false;
}

$('v-zoom').addEventListener('click', () => { zoomOn = !zoomOn; $('v-zoom').classList.toggle('on', zoomOn); });

// ---- laser controls ---------------------------------------------------------------
let selBuilt = false, armPending = 0, msgTimer = 0;
const sliders = {};

function toast(t) {
  $('v-msg').textContent = t;
  clearTimeout(msgTimer);
  msgTimer = setTimeout(() => { $('v-msg').textContent = ''; }, 3500);
}

// Control requests are SERIALISED: one in flight at a time, strictly in order. The server handles
// requests on parallel threads, so firing several at once could let an older one land last -- harmless
// for a slider, but an old ARM overtaking a newer BLACKOUT would re-arm the laser. Queued slider
// values coalesce (last wins); a queued BLACKOUT cancels a not-yet-sent ARM and vice versa.
let ctlInflight = false, ctlPending = null;
function send(patch) {
  ctlPending = Object.assign(ctlPending || {}, patch);
  if (patch.blackout) delete ctlPending.arm;
  if (patch.arm === true) delete ctlPending.blackout;
  pumpControl();
}
async function pumpControl() {
  if (ctlInflight || !ctlPending) return;
  ctlInflight = true;
  const patch = ctlPending; ctlPending = null;
  try {
    const r = await fetch('/api/laser/control', {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Cosmic': '1' }, body: JSON.stringify(patch) });
    const j = await r.json();
    if (r.ok) { if (!ctlPending) renderLaser(j); }
    else { toast(j.error || ('control failed: HTTP ' + r.status)); if (j.state && !ctlPending) renderLaser(j.state); }
  } catch (e) { toast('control failed: ' + e.message); }
  finally { ctlInflight = false; if (ctlPending) pumpControl(); }
}

function buildSelector(s) {
  const g = $('sel-grid');
  const add = (kind, name) => {
    const b = document.createElement('button');
    b.type = 'button'; b.textContent = name; b.dataset.kind = kind; b.dataset.name = name;
    b.className = kind === 'scene' ? 'scene' : '';
    b.addEventListener('click', () => send({ select: { kind: kind, name: name } }));
    g.appendChild(b);
  };
  s.scenes.forEach(n => add('scene', n));
  s.patterns.forEach(n => add('pattern', n));
  selBuilt = true;
}

function bindSlider(id, key) {
  // The latest value ALWAYS goes out: a trailing-edge throttle (<= 100 ms after the last movement) plus a
  // send on release. Relying on 'change' alone loses the final value when a drag ends where it began
  // (the browser fires no 'change' then), and a leading-edge throttle drops the last inputs.
  const el = $(id);
  let drag = false, timer = 0;
  const flush = () => { clearTimeout(timer); timer = 0; send({ [key]: Number(el.value) }); };
  const end = () => { if (drag) { drag = false; flush(); } };
  el.addEventListener('pointerdown', () => { drag = true; });
  window.addEventListener('pointerup', end);
  window.addEventListener('pointercancel', end);
  el.addEventListener('input', () => { labels(); if (!timer) timer = setTimeout(flush, 100); });
  el.addEventListener('change', flush);
  sliders[key] = { el: el, dragging: () => drag };
}
bindSlider('sl-sync', 'sync'); bindSlider('sl-size', 'size'); bindSlider('sl-int', 'intensity');

function labels() {
  const s = lstate;
  $('v-size').textContent = Number($('sl-size').value).toFixed(2);
  $('v-int').textContent = Number($('sl-int').value).toFixed(3);
  const eff = s ? s.effective_sync : Number($('sl-sync').value);
  $('v-sync').textContent = Number($('sl-sync').value).toFixed(2) + (s && s.select.kind === 'scene' ? '  >  ' + eff.toFixed(2) : '');
}

document.querySelectorAll('#src-seg button').forEach(b =>
  b.addEventListener('click', () => send({ source: b.dataset.src })));
$('sync-mode').addEventListener('click', () => {
  if (lstate) send({ sync_mode: lstate.sync_mode === 'hold' ? 'auto' : 'hold' });
});
$('v-black').addEventListener('click', () => send({ blackout: true }));
$('btn-black').addEventListener('click', () => send({ blackout: true }));
$('btn-arm').addEventListener('click', () => {
  const s = lstate;
  if (!s || !s.laser || !s.can_control) return;
  if (s.mode === 'armed') { send({ arm: false }); return; }
  if (!armPending || Date.now() > armPending) {          // two taps: the first only asks
    armPending = Date.now() + 3000; renderArm();
    setTimeout(() => { if (Date.now() > armPending) { armPending = 0; renderArm(); } }, 3100);
    return;
  }
  armPending = 0; send({ arm: true });
});

function renderArm() {
  const b = $('btn-arm'), s = lstate;
  b.className = '';
  if (!s || !s.laser) { b.textContent = 'NO LASER'; b.disabled = true; return; }
  b.disabled = !s.can_control || s.mode === 'error';
  if (s.mode === 'armed') {
    b.className = 'armed';
    b.textContent = 'DISARM' + (s.auto_disarm_in_s != null ? '  ' + mmss(s.auto_disarm_in_s) : '');
  } else if (armPending && Date.now() < armPending) { b.className = 'pending'; b.textContent = 'TAP AGAIN TO ARM'; }
  else { b.textContent = s.mode === 'error' ? 'LASER ERROR' : 'ARM'; }
}

function renderLaser(s) {
  lstate = s;
  if (!s.enabled) {
    engineOn = false;
    $('v-off').style.display = 'flex';
    $('v-off').textContent = 'Visualiser unavailable: ' + (s.reason || 'laser engine not loaded') +
      '. Pods and graphs still work.';
    $('v-state').textContent = 'NO ENGINE'; $('panel-laser').classList.add('ro');
    return;
  }
  if (!selBuilt) buildSelector(s);
  const chip = $('v-state');
  let txt = 'PREVIEW', cls = 'vchip';
  if (s.mode === 'armed') { txt = 'ARMED'; cls = 'vchip armed'; }
  else if (s.mode === 'safe') { txt = 'LASER DARK'; cls = 'vchip warn'; }
  else if (s.mode === 'error') { txt = 'LASER ERROR'; cls = 'vchip bad'; }
  chip.textContent = txt + '  -  ' + s.source.toUpperCase();
  chip.className = cls;
  document.querySelectorAll('#src-seg button').forEach(b => b.classList.toggle('on', b.dataset.src === s.source));
  document.querySelectorAll('#sel-grid button').forEach(b =>
    b.classList.toggle('on', b.dataset.kind === s.select.kind && b.dataset.name === s.select.name));
  const lim = s.limits;
  const set = (key, min, max) => {
    const sl = sliders[key];
    sl.el.min = min; sl.el.max = max;
    if (!sl.dragging()) sl.el.value = s[key];
  };
  set('sync', 0, 1); set('size', lim.min_size, lim.max_size); set('intensity', 0, lim.max_intensity);
  $('sync-mode').textContent = s.sync_mode === 'hold' ? 'HOLD' : 'AUTO';
  $('sync-mode').classList.toggle('on', s.sync_mode === 'hold');
  labels();
  $('v-black').style.display = s.mode === 'armed' ? 'block' : 'none';
  $('btn-black').disabled = !s.laser || !s.can_control;
  $('panel-laser').classList.toggle('ro', !s.can_control);
  renderArm();
  // load = how much of each frame period the computer spends BUILDING a frame (100% = cannot keep up);
  // sent = frames handed to the DAC so far (must keep rising while armed); rep = frames re-sent because the next one was not ready;
  // dark = times output stopped for lack of frames
  vizMs = s.viz_ms || 33;
  let l1 = s.points + ' pts  ' + s.frame_ms + ' ms  ' + s.fps + ' fps  load ' + Math.round(100 * (s.load || 0)) + '%';
  if (s.laser && s.mode === 'armed') l1 += '  sent ' + (s.sent || 0) + '  rep ' + (s.late || 0) + (s.dark ? '  dark ' + s.dark : '');
  if (s.usb_errors) l1 += '  usb err ' + s.usb_errors;
  if (s.refused) l1 += '  refused ' + s.refused;
  // ext = how wide the lit pattern really is (full field = 2.0); (xN) = enlarged to keep it above --min-extent;
  // rgb = peak 8-bit drive of each colour channel in the last frame
  const l2 = 'ext ' + (s.extent || 0).toFixed(2) + (s.boost > 1.01 ? ' (x' + s.boost.toFixed(1) + ')' : '') +
             '  rgb ' + (s.peak_rgb || []).join('/') + '  pods ' + s.pods_online + '/6 live ' + s.pods_live;
  $('v-info').textContent = l1 + '\n' + l2;
  if (!s.can_control) $('v-msg').textContent = 'view only: controls work from the hub\'s own screen';
  else if (s.error) $('v-msg').textContent = s.error;
  else if (s.last_refusal && s.refusal_age_s != null && s.refusal_age_s < 5) $('v-msg').textContent = 'no picture: ' + s.last_refusal;
  else if (s.mode === 'safe' && s.note && /auto-disarm|blackout|error/.test(s.note)) $('v-msg').textContent = s.note;
  else if (!msgTimer || !$('v-msg').textContent.startsWith('control')) { if (s.can_control && !s.error) $('v-msg').textContent = ''; }
}

async function pollLaser() {
  if (view !== 'main') return;
  try {
    const r = await fetch('/api/laser/state', { cache: 'no-store' });
    renderLaser(await r.json());
  } catch (e) { /* the pod poll already reports a dead server */ }
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
document.addEventListener('contextmenu', e => e.preventDefault());

window.cosmicDebug = () => ({ vizSeq: vizSeq, lastFrameAt: lastFrameAt, tab: tab, view: view, renderLaser: renderLaser });   // for tests
sizeCanvas();
loadInfo();
poll();
pollLaser().then(() => vizLoop());
setInterval(poll, 1000);
setInterval(pollLaser, 300);
setInterval(() => { if (armPending && Date.now() > armPending) { armPending = 0; renderArm(); } }, 500);
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Flask app. A function (not inline in main) so tests can drive it without sockets.
# ---------------------------------------------------------------------------
def build_app(dashboard, engine, local_ips, have_chart: bool, osc_port: int, allow_remote_control: bool = False):
    app = Flask(__name__)

    def may_control(req) -> bool:
        """Who may change the laser's settings. PREVIEW (no DAC): anyone -- it only changes a drawing.
        With a DAC: only this machine, because the hotspot is open and a stranger on it must not be
        able to arm a laser -- unless the operator explicitly allowed remote control."""
        if engine is None:
            return False
        if engine.dac is None or allow_remote_control:
            return True
        try:
            return ipaddress.ip_address(req.remote_addr or "").is_loopback
        except ValueError:
            return False

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
        return jsonify({"osc_port": osc_port,
                        "ips": [{"name": n, "ip": i} for n, i in local_ips],
                        "chart": have_chart, "laser_engine": engine is not None})

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

    # ---- laser engine ----------------------------------------------------------
    @app.route("/api/laser/state")
    def laser_state():
        if engine is None:
            return jsonify({"enabled": False, "reason": LASER_IMPORT_ERROR or "laser engine not loaded"})
        s = engine.state()
        s["can_control"] = may_control(request)
        return jsonify(s)

    @app.route("/api/laser/frame.bin")
    def laser_frame():
        """The newest frame as binary (8-byte header + 8 bytes per point), held up to 250 ms
        for one newer than ?since=<seq>; 204 if none came."""
        if engine is None:
            abort(404)
        try:
            since = int(request.args.get("since", "0"))
        except ValueError:
            since = 0
        f = engine.wait_frame(since, 0.25)
        if f is None:
            return Response(status=204)
        resp = Response(f[1], mimetype="application/octet-stream")
        resp.headers["X-Seq"] = str(f[0])
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/api/laser/control", methods=["POST"])
    def laser_control():
        if engine is None:
            abort(404)
        if request.headers.get("X-Cosmic") != "1":          # a plain cross-site form post can't set this
            return jsonify({"error": "missing X-Cosmic header"}), 400
        if not may_control(request):
            return jsonify({"error": "view only: controls work from the hub's own screen"}), 403
        patch = request.get_json(silent=True)
        if not isinstance(patch, dict):
            return jsonify({"error": "expected a JSON object"}), 400
        ok, msg = engine.control(patch)
        s = engine.state()
        s["can_control"] = True
        if not ok:
            return jsonify({"error": msg, "state": s}), 400
        return jsonify(s)

    return app


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--osc-port", type=int, default=9000, help="UDP port to receive pod telemetry on")
    ap.add_argument("--web-port", type=int, default=5000, help="port to serve the page on")
    g = ap.add_argument_group("laser hub (needs the laser/ folder next to this file)")
    g.add_argument("--laser", action="store_true",
                   help="drive the Helios DAC. Default: PREVIEW only (visualiser, nothing sent). Starts SAFE: ARM on screen")
    g.add_argument("--lib", help="path to the Helios library (or set $HELIOS_LIB)")
    g.add_argument("--device", type=int, default=0, help="DAC number if several are connected")
    g.add_argument("--source", choices=("live", "fill", "demo"), default="demo",
                   help="where the scenes get their pods: live pods, live + simulated fill, or all simulated (default demo)")
    g.add_argument("--scene", default="converge", help="scene shown at start")
    g.add_argument("--palette", choices=("gsr", "spectrum"), default="gsr")
    g.add_argument("--max-intensity", type=float, default=0.3,
                   help="the LEVEL slider's ceiling, enforced on the server (default 0.3; >0.5 needs --allow-high)")
    g.add_argument("--max-size", type=float, default=0.6, help="the SIZE slider's ceiling (default 0.6)")
    g.add_argument("--auto-disarm", type=float, default=20.0, metavar="MIN",
                   help="disarm after this many minutes without any control activity (default 20; 0 = never)")
    g.add_argument("--max-stale-ms", type=int, default=200, metavar="MS",
                   help="with --laser: if no NEW frame has been built for this long while armed, the DAC is stopped (dark) "
                        "instead of repeating the last frame (default 200; a slow computer re-sends the last frame "
                        "to keep the laser lit, a stalled one goes dark)")
    g.add_argument("--usb-grace-ms", type=int, default=1000, metavar="MS",
                   help="with --laser: transient USB errors (a timeout, 'libusb error -7') are ridden out for this long before the "
                        "laser is disarmed with an error (default 1000)")
    g.add_argument("--usb-log-file", metavar="FILE",
                   help="with --laser: append EVERY USB event (error, recovery) to this file (the terminal shows a rate-limited "
                        "version). A small file you can attach to the chat")
    g.add_argument("--usb-polling", choices=("adaptive", "fast", "timed"), default="adaptive",
                   help="how frames are paced to the DAC: adaptive (default) / fast = ask 'ready?' over USB before each frame; "
                        "timed = NEVER poll the status: write on a fixed schedule (one frame every its own duration + --frame-gap-ms). "
                        "Measured on a Raspberry Pi 4: polling during streaming makes the DAC go silent for ~2.4 s about once per "
                        "2000 polls, while timed writes ran clean")
    g.add_argument("--frame-gap-ms", type=float, default=1.5, metavar="MS",
                   help="with --usb-polling timed: the DAC idles this long between frames (default 1.5 = ~5%% of the light; the value "
                        "tested; smaller puts writes closer to the frame boundary)")
    g.add_argument("--viz-fps", type=float, default=30.0, metavar="FPS",
                   help="ceiling for the on-screen visualiser's frame rate (default 30; ~15 saves a Raspberry Pi's browser a lot of "
                        "work). It also slows down by itself when the hub is overloaded")
    g.add_argument("--perf", nargs="?", const=5.0, type=float, default=None, metavar="SECONDS",
                   help="print every SECONDS (default 5) where the CPU goes: per thread, per stage of a frame, and the board's clock "
                        "and throttling. Run it on the Pi and send me the lines")
    g.add_argument("--allow-high", action="store_true", help="permit --max-intensity above 0.5")
    g.add_argument("--allow-remote-control", action="store_true",
                   help="with --laser, accept control (incl. ARM) from other machines too. DANGEROUS on an open hotspot")
    g.add_argument("--tol-bpm", type=float, default=10.0, help="BPM spread at which measured sync reaches 0")
    g.add_argument("--demo-speed", type=float, default=1.0, help="demo pods: speed multiplier")
    if LT is not None:
        LT.add_view_args(ap)           # --size --intensity --pps --frame-ms --rgb-min --blank-shift ... (as laser_test.py)
    return ap


class RateLimitedPrinter:
    """Prints at most `per_second` lines a second and then says how many it held back, so a burst of USB errors tells the
    whole story at the start without flooding the terminal."""

    def __init__(self, per_second: float = 4.0, out=None) -> None:
        self.per_second, self.out = per_second, out
        self._lock = threading.Lock()
        self._window, self._n, self._held = 0.0, 0, 0

    def __call__(self, line: str) -> None:
        now = time.monotonic()
        with self._lock:
            if now - self._window >= 1.0:
                held, self._held, self._n, self._window = self._held, 0, 0, now
                if held:
                    print(f"      ... {held} more USB line(s) held back", file=self.out or sys.stdout, flush=True)
            if self._n < self.per_second:
                self._n += 1
                print(line, file=self.out or sys.stdout, flush=True)
            else:
                self._held += 1


class UsbLogSink:
    """Every USB event goes to the log FILE (capped, so a long error storm cannot fill the disk) and, rate-limited, to the terminal."""

    def __init__(self, path, printer, cap_bytes: int = 500_000) -> None:
        self.printer, self.cap, self.written, self.fh = printer, cap_bytes, 0, None
        if path:
            self.fh = open(path, "a", encoding="utf-8")
            self._write(f"--- hub started {time.strftime('%Y-%m-%d %H:%M:%S')} ---")

    def _write(self, line: str) -> None:
        if self.fh and self.written < self.cap:
            self.fh.write(line + "\n")
            self.fh.flush()
            self.written += len(line) + 1
            if self.written >= self.cap:
                self.fh.write("--- log size cap reached: no more lines are written to this file ---\n")
                self.fh.flush()

    def __call__(self, line: str) -> None:
        if self.printer:
            self.printer(line)
        self._write(line)


def start_perf_reporter(engine, interval: float) -> None:
    """--perf: every `interval` seconds print where the CPU goes (per thread), where a frame's time goes, and the board's
    clock / throttling. For finding out WHAT makes a slow computer (a Raspberry Pi 3) fall behind."""
    def thread_cpu():                                           # CPU seconds per named thread (Linux)
        out = {}
        try:
            tick = os.sysconf("SC_CLK_TCK")
            for t in threading.enumerate():
                tid = getattr(t, "native_id", None)
                if tid:
                    with open(f"/proc/self/task/{tid}/stat") as f:
                        p = f.read().rsplit(") ", 1)[1].split()
                    out[t.name] = out.get(t.name, 0.0) + (int(p[11]) + int(p[12])) / tick
        except (OSError, ValueError, AttributeError, IndexError):
            return None
        return out

    def board() -> str:
        bits = []
        try:
            mhz = int(open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").read()) // 1000
            gov = open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor").read().strip()
            bits.append(f"cpu {mhz} MHz ({gov})")
        except (OSError, ValueError):
            pass
        if shutil.which("vcgencmd"):
            for what, key in (("get_throttled", "throttled"), ("measure_temp", "temp")):
                try:
                    out = subprocess.run(["vcgencmd", what], capture_output=True, text=True, timeout=1).stdout.strip()
                    bits.append(out.split("=", 1)[-1] if what == "measure_temp" else out.replace("throttled=", "throttled "))
                except Exception:
                    pass
        return "  ".join(bits)

    def loop() -> None:
        t0, c0, th0, s0 = time.monotonic(), time.process_time(), thread_cpu(), engine.state()
        while True:
            time.sleep(interval)
            t1, c1, th1, s1 = time.monotonic(), time.process_time(), thread_cpu(), engine.state()
            dt = t1 - t0
            parts = [f"process {100 * (c1 - c0) / dt:.0f}%"]
            if th0 is not None and th1 is not None:
                named = 0.0
                for label, key in (("engine", "laser-engine"), ("dac", "laser-dac"), ("osc", "osc")):
                    v = 100 * (th1.get(key, 0.0) - th0.get(key, 0.0)) / dt
                    named += v
                    parts.append(f"{label} {v:.0f}%")
                parts.append(f"web+other {100 * (c1 - c0) / dt - named:.0f}%")
            st = s1["stages"]
            frame = " + ".join(f"{k} {v:g}" for k, v in st.items())
            line = (f"[perf {interval:g}s] CPU (% of one core): " + " | ".join(parts) +
                    f"  ||  one frame: {s1['build_ms']:.1f} ms = {frame}  (load {100 * s1['load']:.0f}% of a {s1['frame_ms']:.0f} ms period)"
                    f"  ||  frames/s sent {(s1['sent'] - s0['sent']) / dt:.0f}, re-sent {(s1['late'] - s0['late']) / dt:.0f}, "
                    f"dark {s1['dark']}, usb errors {s1['usb_errors']}")
            b = board()
            print(line + (f"  ||  {b}" if b else ""), flush=True)
            t0, c0, th0, s0 = t1, c1, th1, s1

    threading.Thread(target=loop, daemon=True, name="perf").start()


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if LT is not None:
        LT.check_args(args)            # exits 2 with a message on a bad shared option
    if args.laser and LT is None:
        print(f"error: --laser cannot start, the laser code did not load: {LASER_IMPORT_ERROR}\n"
              f"       (the laser/ folder must sit next to this file, and its files must all be from the same update)", file=sys.stderr)
        return 3
    if LT is not None:
        errs = []
        if not 0.0 < args.max_intensity <= 1.0:
            errs.append("--max-intensity must be above 0 and at most 1")
        if args.max_intensity > 0.5 and not args.allow_high:
            errs.append("--max-intensity above 0.5 needs --allow-high")
        if not 0.1 <= args.max_size <= 1.0:
            errs.append("--max-size must be 0.1-1.0")
        if args.intensity > args.max_intensity:
            errs.append(f"--intensity {args.intensity} is above --max-intensity {args.max_intensity}")
        if args.scene not in LV.SCENES:
            errs.append(f"--scene must be one of: {', '.join(LV.SCENES)}")
        if args.auto_disarm < 0:
            errs.append("--auto-disarm cannot be negative")
        if not 50 <= args.max_stale_ms <= 2000:
            errs.append("--max-stale-ms must be 50-2000")
        if not 0 <= args.frame_gap_ms <= 10:
            errs.append("--frame-gap-ms must be 0-10")
        if not 100 <= args.usb_grace_ms <= 10000:
            errs.append("--usb-grace-ms must be 100-10000")
        if not 2 <= args.viz_fps <= 60:
            errs.append("--viz-fps must be 2-60")
        if errs:
            print("error: " + "; ".join(errs), file=sys.stderr)
            return 2

    dashboard = Dashboard(args.osc_port)
    dashboard.run_osc_server()

    engine = dac = None
    if LT is not None:
        if args.laser:
            try:
                dac = HeliosDAC(lib_path=args.lib, device=args.device).open()
            except HeliosError as e:
                print(f"error: {e}", file=sys.stderr)
                return 3
        engine = LE.LaserEngine(
            lambda: dashboard.snapshot(with_history=False), LT.view_from(args), LT.calib_from(args),
            args.pps, args.frame_ms, LP.PALETTES[args.palette], dac=dac, source=args.source, scene=args.scene,
            sync=args.sync if args.sync is not None else 0.0, sync_mode="hold" if args.sync is not None else "auto",
            max_intensity=args.max_intensity, max_size=args.max_size, auto_disarm_s=args.auto_disarm * 60.0,
            bpm=args.bpm, tol_bpm=args.tol_bpm, demo_speed=args.demo_speed, min_extent=args.min_extent,
            max_stale_s=args.max_stale_ms / 1000.0, usb_grace_s=args.usb_grace_ms / 1000.0, viz_fps=args.viz_fps,
            adaptive_poll=args.usb_polling == "adaptive", poll_mode=args.usb_polling, frame_gap_s=args.frame_gap_ms / 1000.0, usb_log=UsbLogSink(args.usb_log_file, RateLimitedPrinter()) if dac is not None else None)
        engine.start()

        def _selfcheck() -> None:                                # a dashboard that is quietly blank is the worst failure
            st = engine.state()
            if st["frames"] == 0:
                print(f"\nWARNING: the laser engine has produced NO frames after 2.5 s. Last problem: "
                      f"{st['last_refusal'] or '(none recorded)'}\n         The picture will be blank and nothing can reach the laser.", file=sys.stderr)
        _t = threading.Timer(2.5, _selfcheck)
        _t.daemon = True
        _t.start()
        if args.perf:
            start_perf_reporter(engine, max(1.0, args.perf))

    local_ips = get_local_ips()
    have_chart = os.path.exists(CHART_JS_FILE)
    print("cosmic dune -- hub (OSC dashboard + laser visualiser)")
    print(f"Listening for pod telemetry on UDP :{args.osc_port} (all interfaces)")
    print("This machine's addresses -- on each pod's CLI use the one on the pod's own network, then `net save`:")
    for name, ip in local_ips:
        print(f"    {name:<14} net hub {ip} {args.osc_port}    (dashboard: http://{ip}:{args.web_port}/)")
    if not have_chart:
        print(f"\nNOTE: {CHART_JS_FILE} not found -- the graphs will be unavailable "
              "(no CDN fallback, by design: this must work offline). Tiles and numbers still work.")
    if engine is not None:
        if args.min_extent > 0:
            print(f"Scenes never get narrower than {args.min_extent:g} (lit width, full field = 2.0; --min-extent, 0 = off).")
    if engine is None:
        print(f"\nNOTE: laser engine not loaded ({LASER_IMPORT_ERROR}) -- no visualiser, no controls.")
    elif dac is None:
        print("\nLASER: PREVIEW only -- the visualiser runs, NOTHING is sent to any laser (start with --laser to drive the DAC).")
    else:
        print(f"\nLASER: DAC {dac.name or '(unnamed)'} firmware {dac.firmware} open, output SAFE (dark). "
              f"ARM on the touch screen. Limits: LEVEL <= {args.max_intensity}, SIZE <= {args.max_size}; "
              f"auto-disarm {'off' if not args.auto_disarm else f'after {args.auto_disarm:g} min'}; "
              f"dark if no new frame for {args.max_stale_ms} ms; USB errors ridden out for {args.usb_grace_ms} ms (logged); "
              f"DAC polling {args.usb_polling}; "
              f"controls from {'ANY machine (!)' if args.allow_remote_control else 'this machine only'}.")

    app = build_app(dashboard, engine, local_ips, have_chart, args.osc_port, args.allow_remote_control)

    def _terminate(signum, frame):
        raise KeyboardInterrupt
    for sig in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _terminate)        # closing the terminal also darkens the laser
    logging.getLogger("werkzeug").setLevel(logging.ERROR)          # the frame endpoint is hit ~30x/s
    try:
        from flask import cli as flask_cli
        flask_cli.show_server_banner = lambda *a, **k: None
    except Exception:
        pass
    print(f"\nServing http://0.0.0.0:{args.web_port}/   (Ctrl-C to stop)")
    try:
        app.run(host="0.0.0.0", port=args.web_port, threaded=True)
    except KeyboardInterrupt:
        print("\nstopping ...")
    finally:
        if engine is not None:
            engine.shutdown()
            if dac is not None:
                print("laser output stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
