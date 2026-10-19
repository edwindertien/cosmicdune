#!/usr/bin/env python3
"""
cosmic dune -- pod simulator: six fake pods feeding the hub over real UDP/OSC.

Sends exactly what the pod firmware sends, per simulated pod, per tick (default 15 Hz):
    /pod/<id>/bpm      int    beats per minute (stays at its last value when the pulse is lost)
    /pod/<id>/ibi      int    milliseconds, 60000 / bpm
    /pod/<id>/signal   int    1 = pulse sensor has a signal, 0 = no pulse
    /pod/<id>/gsrRaw   int    12-bit ADC counts
    /pod/<id>/gsr      float  0.0 .. 1.0
...so the hub cannot tell it from real pods: the pod tiles, the LIVE source of the laser engine,
the measured sync, OFFLINE after 3 s of silence -- all behave as with real hardware.

NO IP SPOOFING IS NEEDED. The hub identifies a pod by the id in the OSC address (/pod/3/...), not by
the sender's IP; it only records the source IP to show it in a pod's detail view. So one process can
play all six pods from one address (all six then show the same IP). For distinct addresses on one
machine, --distinct-ips sends pod N from 127.0.0.<N+1> (Linux: works out of the box; macOS: needs
`sudo ifconfig lo0 alias 127.0.0.2` ... `.7`, otherwise it falls back to one address) -- only useful
when the hub runs on the same machine. It is cosmetic.

Real pods use the same ids 1-6: if a real pod is connected, switch that simulated pod OFF
(or start with --off 1) so the two do not fight over its tile.

The UI listens on 127.0.0.1 only by default (--bind 0.0.0.0 to reach it from another machine).

Usage:
    python3 pod_simulator.py                      # UI on http://127.0.0.1:5100 , sends to 127.0.0.1:9000
    python3 pod_simulator.py --host 10.42.0.1     # feed a hub on the Pi (from the Mac, on the hotspot)
    python3 pod_simulator.py --off 1              # pod 1 is a real pod: do not simulate it

Requires: pip install flask python-osc   (see requirements.txt)
"""
import argparse
import math
import random
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from flask import Flask, Response, abort, jsonify, request
from pythonosc.osc_message_builder import OscMessageBuilder

NUM_PODS = 6
SCATTER = [56.0, 64.0, 72.0, 82.0, 92.0, 102.0]       # the same spread as the laser tool's demo pods


@dataclass
class SimPod:
    id: int
    on: bool = True
    bpm: float = 72.0          # what is being sent (the base value; wander adds noise on top)
    target: float = 72.0       # where a glide is heading
    glide_rate: float = 0.0    # BPM per second while gliding; 0 = not gliding
    gsr: float = 0.4
    signal: bool = True
    noise: float = 0.0         # Ornstein-Uhlenbeck noise on bpm (BPM), only while wander is on
    gnoise: float = 0.0        # same for gsr


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def make_dgram(address: str, value) -> bytes:
    b = OscMessageBuilder(address=address)
    b.add_arg(value)
    return b.build().dgram


class Simulator:
    def __init__(self, host: str = "127.0.0.1", port: int = 9000, rate: float = 15.0,
                 off: Tuple[int, ...] = (), distinct_ips: bool = False) -> None:
        self.lock = threading.Lock()
        self.pods = [SimPod(i + 1, on=(i + 1) not in off, bpm=SCATTER[i], target=SCATTER[i],
                            gsr=0.15 + 0.12 * i) for i in range(NUM_PODS)]
        self.host, self.port, self.rate = host, int(port), float(rate)
        self.paused, self.wander, self.glide = False, False, 8.0
        self.sent = self.errors = 0
        self.last_error = ""
        self._addr: Optional[Tuple[str, int]] = None
        self._resolve()
        self._shared = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._per_pod: Dict[int, socket.socket] = {}
        self.distinct_note = ""
        if distinct_ips:
            self._open_distinct()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._window: List[Tuple[float, int]] = []        # (time, cumulative sent) for msgs/s

    # ---------------------------------------------------------------- setup helpers
    def _resolve(self) -> None:
        try:
            ip = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_DGRAM)[0][4]
            self._addr = (ip[0], ip[1])
            self.last_error = ""
        except (socket.gaierror, OSError, UnicodeError) as e:
            self._addr = None
            self.last_error = f"cannot resolve '{self.host}': {e}"

    def _open_distinct(self) -> None:
        failed = []
        for pid in range(1, NUM_PODS + 1):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.bind((f"127.0.0.{pid + 1}", 0))
                self._per_pod[pid] = s
            except OSError:
                s.close()
                failed.append(pid)
        if failed:
            self.distinct_note = (f"--distinct-ips: could not bind 127.0.0.x for pod(s) {failed}; those use the shared "
                                  "address (macOS: sudo ifconfig lo0 alias 127.0.0.<N+1>)")
            print("note: " + self.distinct_note, file=sys.stderr)

    def _sock_for(self, pid: int) -> socket.socket:
        # A distinct loopback source only makes sense while the target is loopback too.
        if pid in self._per_pod and self._addr and self._addr[0].startswith("127."):
            return self._per_pod[pid]
        return self._shared

    # ---------------------------------------------------------------- control (UI / API)
    def state(self) -> Dict:
        with self.lock:
            now = time.monotonic()
            recent = [(t, n) for t, n in self._window if now - t <= 2.0]
            rate = (recent[-1][1] - recent[0][1]) / (recent[-1][0] - recent[0][0]) if len(recent) > 1 and recent[-1][0] > recent[0][0] else 0.0
            return {"host": self.host, "port": self.port, "rate": self.rate, "paused": self.paused, "wander": self.wander,
                    "glide": self.glide, "sent": self.sent, "errors": self.errors, "last_error": self.last_error,
                    "msgs_per_s": int(round(rate)), "note": self.distinct_note,
                    "pods": [{"id": p.id, "on": p.on, "bpm": round(p.bpm, 1), "target": p.target, "glide_rate": p.glide_rate,
                              "gsr": round(p.gsr, 3), "signal": p.signal} for p in self.pods]}

    def set_pod(self, pid: int, patch: Dict) -> None:
        with self.lock:
            p = self.pods[pid - 1]
            if "on" in patch:
                p.on = bool(patch["on"])
            if "bpm" in patch:
                p.bpm = p.target = _clamp(float(patch["bpm"]), 0.0, 250.0)      # a manual set cancels any glide
                p.glide_rate = 0.0
            if "gsr" in patch:
                p.gsr = _clamp(float(patch["gsr"]), 0.0, 1.0)
            if "signal" in patch:
                p.signal = bool(patch["signal"])

    def set_global(self, patch: Dict) -> None:
        with self.lock:
            if "rate" in patch:
                self.rate = _clamp(float(patch["rate"]), 1.0, 60.0)
            if "paused" in patch:
                self.paused = bool(patch["paused"])
            if "wander" in patch:
                self.wander = bool(patch["wander"])
                if not self.wander:
                    for p in self.pods:
                        p.noise = p.gnoise = 0.0
            if "glide" in patch:
                self.glide = _clamp(float(patch["glide"]), 0.0, 120.0)
            changed = False
            if "host" in patch and str(patch["host"]).strip():
                self.host = str(patch["host"]).strip()
                changed = True
            if "port" in patch:
                port = int(patch["port"])
                if not 1 <= port <= 65535:
                    raise ValueError("port must be 1-65535")
                self.port = port
                changed = True
            if changed:
                self._resolve()

    def preset(self, name: str, glide: Optional[float] = None) -> None:
        with self.lock:
            g = self.glide if glide is None else _clamp(float(glide), 0.0, 120.0)
            if name == "all_on":
                for p in self.pods:
                    p.on = True
            elif name == "all_off":
                for p in self.pods:
                    p.on = False
            elif name in ("converge", "scatter"):
                active = [p for p in self.pods if p.on] or self.pods
                common = sum(p.bpm for p in active) / len(active)
                for p in self.pods:
                    p.target = common if name == "converge" else SCATTER[p.id - 1]
                    if g <= 0 or abs(p.target - p.bpm) < 1e-9:
                        p.bpm, p.glide_rate = p.target, 0.0
                    else:
                        p.glide_rate = abs(p.target - p.bpm) / g
                self.glide = g
            else:
                raise ValueError("unknown preset")

    # ---------------------------------------------------------------- the sender
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="sender", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
        for s in list(self._per_pod.values()) + [self._shared]:
            s.close()

    def _step(self, dt: float) -> None:
        """Advance glides and (if wandering) the slow noise. Caller holds the lock."""
        for p in self.pods:
            if p.glide_rate > 0:
                d = p.target - p.bpm
                step = p.glide_rate * dt
                if abs(d) <= step:
                    p.bpm, p.glide_rate = p.target, 0.0
                else:
                    p.bpm += math.copysign(step, d)
            if self.wander:                               # slow, mean-reverting drift: ~2 BPM / ~0.08 GSR wide
                p.noise += (-p.noise / 6.0) * dt + 1.2 * math.sqrt(dt) * random.gauss(0, 1)
                p.gnoise += (-p.gnoise / 15.0) * dt + 0.03 * math.sqrt(dt) * random.gauss(0, 1)

    def _run(self) -> None:
        last = time.monotonic()
        while not self._stop.is_set():
            t0 = time.monotonic()
            dt = min(t0 - last, 0.5)
            last = t0
            with self.lock:
                self._step(dt)
                period = 1.0 / self.rate
                addr = self._addr
                out = []
                if not self.paused and addr is not None:
                    for p in self.pods:
                        if not p.on:
                            continue
                        bpm = int(round(_clamp(p.bpm + p.noise, 0.0, 250.0)))
                        gsr = _clamp(p.gsr + p.gnoise, 0.0, 1.0)
                        ibi = int(round(60000.0 / bpm)) if bpm > 0 else 0
                        pre = f"/pod/{p.id}/"
                        out.append((p.id, [make_dgram(pre + "bpm", bpm), make_dgram(pre + "ibi", ibi),
                                           make_dgram(pre + "signal", 1 if p.signal else 0),
                                           make_dgram(pre + "gsrRaw", int(2000 + gsr * 1500)),
                                           make_dgram(pre + "gsr", float(gsr))]))
            n = errs = 0
            err = ""
            for pid, dgrams in out:
                sock = self._sock_for(pid)
                for d in dgrams:
                    try:
                        sock.sendto(d, addr)
                        n += 1
                    except OSError as e:
                        errs += 1
                        err = f"{type(e).__name__}: {e}"[:80]
            with self.lock:
                self.sent += n
                self.errors += errs
                if err:
                    self.last_error = err
                elif self._addr is not None and n and self.last_error and not self.last_error.startswith("cannot resolve"):
                    self.last_error = ""
                self._window.append((time.monotonic(), self.sent))
                self._window = self._window[-60:]
            time.sleep(max(0.0, period - (time.monotonic() - t0)))


def build_app(sim: Simulator) -> Flask:
    app = Flask(__name__)

    @app.route("/")
    def index():
        return Response(PAGE, mimetype="text/html")

    @app.route("/api/state")
    def api_state():
        return jsonify(sim.state())

    def body():
        j = request.get_json(silent=True)          # JSON content type only: a plain cross-site form post is refused
        if not isinstance(j, dict):
            abort(400, "expected a JSON object")
        return j

    @app.route("/api/pod/<int:pid>", methods=["POST"])
    def api_pod(pid):
        if not 1 <= pid <= NUM_PODS:
            abort(404)
        try:
            sim.set_pod(pid, body())
        except (TypeError, ValueError) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(sim.state())

    @app.route("/api/global", methods=["POST"])
    def api_global():
        try:
            sim.set_global(body())
        except (TypeError, ValueError) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(sim.state())

    @app.route("/api/preset", methods=["POST"])
    def api_preset():
        j = body()
        try:
            sim.preset(str(j.get("name", "")), j.get("glide"))
        except (TypeError, ValueError) as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(sim.state())

    return app


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1", help="where the hub is (default 127.0.0.1)")
    ap.add_argument("--port", type=int, default=9000, help="the hub's OSC port (default 9000)")
    ap.add_argument("--rate", type=float, default=15.0, help="messages per pod per second (default 15, like the firmware)")
    ap.add_argument("--off", default="", help="comma list of pod ids to start switched OFF, e.g. 1,2 (real pods)")
    ap.add_argument("--distinct-ips", action="store_true", help="send pod N from 127.0.0.<N+1> (cosmetic; hub on this machine only)")
    ap.add_argument("--web-port", type=int, default=5100, help="port of this UI (default 5100)")
    ap.add_argument("--bind", default="127.0.0.1", help="address the UI listens on (default 127.0.0.1 = this machine only)")
    args = ap.parse_args(argv)
    try:
        off = tuple(int(x) for x in args.off.split(",") if x.strip())
    except ValueError:
        print("error: --off must be a comma list of pod ids", file=sys.stderr)
        return 2
    if not 1 <= args.rate <= 60 or not 1 <= args.port <= 65535 or any(not 1 <= o <= NUM_PODS for o in off):
        print("error: --rate 1-60, --port 1-65535, --off ids 1-6", file=sys.stderr)
        return 2
    sim = Simulator(args.host, args.port, args.rate, off, args.distinct_ips)
    sim.start()
    print(f"pod simulator: sending {NUM_PODS - len(off)} pods to {args.host}:{args.port} at {args.rate:g} Hz")
    if off:
        print(f"  pods switched OFF at start (real pods?): {', '.join(map(str, off))}")
    if sim.last_error:
        print(f"  WARNING: {sim.last_error}", file=sys.stderr)
    print(f"UI: http://{'127.0.0.1' if args.bind == '0.0.0.0' else args.bind}:{args.web_port}/   (Ctrl-C to stop)")
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    try:
        build_app(sim).run(host=args.bind, port=args.web_port, threaded=True)
    except KeyboardInterrupt:
        pass
    finally:
        sim.stop()
    return 0


# ---------------------------------------------------------------------------
# The page: plain string, system fonts, no external URLs.
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<title>pod simulator</title>
<style>
  :root { --bg:#14161a; --panel:#1c1f25; --line:#2a2d33; --text:#e8e8e8; --dim:#8a93a0; --ok:#4cd964; --bad:#ff5d5d; --warn:#ffb13b; --acc:#4a76b4; }
  * { box-sizing:border-box; -webkit-tap-highlight-color:transparent; }
  html, body { margin:0; height:100%; overflow:hidden; background:var(--bg); color:var(--text);
    font-family:system-ui, "Segoe UI", Roboto, "DejaVu Sans", Helvetica, Arial, sans-serif; -webkit-user-select:none; user-select:none; }
  button, input { font:inherit; color:inherit; }
  button { cursor:pointer; -webkit-appearance:none; appearance:none; }
  #app { height:100%; display:flex; flex-direction:column; }
  .bar { flex:none; display:flex; align-items:center; gap:7px; padding:0 10px; height:48px; border-bottom:1px solid var(--line); }
  .bar.foot { border-bottom:none; border-top:1px solid var(--line); height:52px; }
  .title { font-size:14px; font-weight:800; letter-spacing:.07em; white-space:nowrap; }
  .tag { font-size:9px; font-weight:800; letter-spacing:.08em; padding:2px 7px; border-radius:99px; background:#5a4412; color:#ffd58a; }
  label.f { display:flex; align-items:center; gap:4px; font-size:10px; font-weight:800; letter-spacing:.06em; color:var(--dim); }
  input[type=number]::-webkit-inner-spin-button, input[type=number]::-webkit-outer-spin-button { -webkit-appearance:none; margin:0; }
  input[type=number] { -moz-appearance:textfield; }
  input[type=text], input[type=number] { height:30px; background:#0f1115; border:1px solid var(--line); border-radius:7px; padding:0 8px; font-size:13px; }
  .btn { height:32px; min-width:52px; padding:0 10px; border-radius:8px; border:1px solid var(--line); background:#262a31; color:var(--dim);
         font-size:11px; font-weight:800; letter-spacing:.05em; white-space:nowrap; }
  .btn.on { background:#33507a; color:#fff; border-color:var(--acc); }
  .btn.go { color:var(--text); }
  .status { margin-left:auto; flex:1 1 0; min-width:0; overflow:hidden; text-overflow:ellipsis; font-size:11px; color:var(--dim);
            font-variant-numeric:tabular-nums; text-align:right; white-space:nowrap; }
  .status.err { color:var(--bad); }
  #rows { flex:1; min-height:0; display:flex; flex-direction:column; padding:6px 10px; gap:4px; }
  .row { flex:1; min-height:0; display:flex; align-items:center; gap:8px; background:var(--panel); border:1px solid var(--line); border-radius:9px; padding:0 8px; }
  .row.off { opacity:.45; }
  .row .id { width:56px; flex:none; font-weight:800; font-size:13px; letter-spacing:.04em; white-space:nowrap; }
  .row .sl { display:flex; align-items:center; gap:6px; }
  .row .sl.bpm { flex:1.35; } .row .sl.gsr { flex:1; }
  .row .k { font-size:9px; font-weight:800; letter-spacing:.07em; color:var(--dim); width:26px; }
  .row .v { font-size:13px; font-weight:700; min-width:50px; text-align:right; font-variant-numeric:tabular-nums; white-space:nowrap; }
  .row .v small { color:var(--warn); font-size:10px; }
  input[type=range] { -webkit-appearance:none; appearance:none; background:transparent; flex:1; min-width:0; height:26px; margin:0; }
  input[type=range]::-webkit-slider-runnable-track { height:7px; background:#2a2d33; border-radius:4px; }
  input[type=range]::-webkit-slider-thumb { -webkit-appearance:none; width:24px; height:24px; border-radius:50%; background:#6aa7ff; margin-top:-8.5px; border:2px solid #0c0e12; }
  input[type=range]::-moz-range-track { height:7px; background:#2a2d33; border-radius:4px; }
  input[type=range]::-moz-range-thumb { width:22px; height:22px; border-radius:50%; background:#6aa7ff; border:2px solid #0c0e12; }
  .row .sl.gsr input[type=range]::-webkit-slider-thumb { background:#c58bff; }
  .pulse.on { background:#1f4d2c; color:#9bf0b0; border-color:#2f6b3f; }
  .pulse.nop { background:#5a4412; color:#ffd58a; border-color:#8a6a24; }
  .onbtn.on { background:#1f4d2c; color:#9bf0b0; border-color:#2f6b3f; }
</style>
</head>
<body>
<div id="app">
  <header class="bar">
    <div class="title">POD SIMULATOR</div><div class="tag">SIMULATED</div>
    <label class="f">TO <input id="host" type="text" style="width:104px" value="127.0.0.1"></label>
    <label class="f">PORT <input id="port" type="number" min="1" max="65535" style="width:70px" value="9000"></label>
    <label class="f">HZ <input id="rate" type="number" min="1" max="60" style="width:54px" value="15"></label>
    <button id="apply" class="btn go" type="button">APPLY</button>
    <button id="pause" class="btn" type="button">PAUSE</button>
    <div id="status" class="status">...</div>
  </header>
  <main id="rows"></main>
  <footer class="bar foot">
    <label class="f">GLIDE <input id="glide" type="number" min="0" max="120" step="1" style="width:56px" value="8"> s</label>
    <button id="p-conv" class="btn go" type="button">CONVERGE</button>
    <button id="p-scat" class="btn go" type="button">SCATTER</button>
    <button id="wander" class="btn" type="button">WANDER</button>
    <button id="all-on" class="btn" type="button">ALL ON</button>
    <button id="all-off" class="btn" type="button">ALL OFF</button>
    <div id="counts" class="status"></div>
  </footer>
</div>
<script>
(() => {
'use strict';
const $ = id => document.getElementById(id);
const rows = {};
let st = null, inflight = false, pending = {};

// Requests are sent one at a time, in order; queued patches for the same path merge (last wins).
function send(path, patch) { pending[path] = Object.assign(pending[path] || {}, patch); pump(); }
async function pump() {
  if (inflight) return;
  const path = Object.keys(pending)[0];
  if (!path) return;
  const body = pending[path]; delete pending[path];
  inflight = true;
  try {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (r.ok) render(await r.json());
  } catch (e) { $('status').textContent = 'simulator not responding'; $('status').className = 'status err'; }
  finally { inflight = false; pump(); }
}

function bindSlider(el, onValue) {
  let drag = false, timer = 0;
  const flush = () => { clearTimeout(timer); timer = 0; onValue(Number(el.value)); };
  const end = () => { if (drag) { drag = false; flush(); } };
  el.addEventListener('pointerdown', () => { drag = true; });
  window.addEventListener('pointerup', end); window.addEventListener('pointercancel', end);
  el.addEventListener('input', () => { el.dispatchEvent(new CustomEvent('local')); if (!timer) timer = setTimeout(flush, 100); });
  el.addEventListener('change', flush);
  return () => drag;
}

for (let i = 1; i <= 6; i++) {
  const d = document.createElement('div'); d.className = 'row';
  d.innerHTML =
    '<button class="btn onbtn" type="button">ON</button><div class="id">POD ' + i + '</div>' +
    '<div class="sl bpm"><span class="k">BPM</span><input type="range" min="30" max="200" step="1" value="72"><span class="v bv">72</span></div>' +
    '<div class="sl gsr"><span class="k">GSR</span><input type="range" min="0" max="1" step="0.01" value="0.4"><span class="v gv">0.40</span></div>' +
    '<button class="btn pulse" type="button">PULSE</button>';
  $('rows').appendChild(d);
  const r = { root: d, on: d.querySelector('.onbtn'), bpm: d.querySelector('.bpm input'), bv: d.querySelector('.bv'),
              gsr: d.querySelector('.gsr input'), gv: d.querySelector('.gv'), pulse: d.querySelector('.pulse') };
  r.dragB = bindSlider(r.bpm, v => send('/api/pod/' + i, { bpm: v }));
  r.dragG = bindSlider(r.gsr, v => send('/api/pod/' + i, { gsr: v }));
  r.bpm.addEventListener('local', () => { r.bv.textContent = r.bpm.value; });
  r.gsr.addEventListener('local', () => { r.gv.textContent = Number(r.gsr.value).toFixed(2); });
  r.on.addEventListener('click', () => send('/api/pod/' + i, { on: !(st && st.pods[i - 1].on) }));
  r.pulse.addEventListener('click', () => send('/api/pod/' + i, { signal: !(st && st.pods[i - 1].signal) }));
  rows[i] = r;
}

function render(s) {
  st = s;
  s.pods.forEach(p => {
    const r = rows[p.id];
    r.root.classList.toggle('off', !p.on);
    r.on.textContent = p.on ? 'ON' : 'OFF'; r.on.classList.toggle('on', p.on);
    if (!r.dragB()) r.bpm.value = Math.round(p.bpm);
    if (!r.dragG()) r.gsr.value = p.gsr;
    const gl = p.glide_rate > 0 ? ' <small>&gt; ' + Math.round(p.target) + '</small>' : '';
    if (!r.dragB()) r.bv.innerHTML = Math.round(p.bpm) + gl;
    if (!r.dragG()) r.gv.textContent = p.gsr.toFixed(2);
    r.pulse.textContent = p.signal ? 'PULSE' : 'NO PULSE';
    r.pulse.className = 'btn pulse ' + (p.signal ? 'on' : 'nop');
  });
  const act = document.activeElement;
  if (act !== $('host') && act !== $('port')) { $('host').value = s.host; $('port').value = s.port; }
  if (act !== $('rate')) $('rate').value = s.rate;
  if (act !== $('glide')) $('glide').value = s.glide;
  $('pause').classList.toggle('on', s.paused); $('pause').textContent = s.paused ? 'PAUSED' : 'PAUSE';
  $('wander').classList.toggle('on', s.wander);
  const stEl = $('status');
  if (s.last_error) { stEl.textContent = 'ERROR: ' + s.last_error; stEl.className = 'status err'; }
  else { stEl.textContent = s.paused ? 'paused' : s.msgs_per_s + ' msg/s'; stEl.className = 'status'; }
  $('counts').textContent = s.sent + ' sent' + (s.errors ? '  ' + s.errors + ' errors' : '');
}

$('apply').addEventListener('click', () => send('/api/global', { host: $('host').value.trim(), port: Number($('port').value), rate: Number($('rate').value) }));
$('pause').addEventListener('click', () => send('/api/global', { paused: !(st && st.paused) }));
$('wander').addEventListener('click', () => send('/api/global', { wander: !(st && st.wander) }));
$('glide').addEventListener('change', () => send('/api/global', { glide: Number($('glide').value) }));
$('p-conv').addEventListener('click', () => send('/api/preset', { name: 'converge', glide: Number($('glide').value) }));
$('p-scat').addEventListener('click', () => send('/api/preset', { name: 'scatter', glide: Number($('glide').value) }));
$('all-on').addEventListener('click', () => send('/api/preset', { name: 'all_on' }));
$('all-off').addEventListener('click', () => send('/api/preset', { name: 'all_off' }));
document.addEventListener('contextmenu', e => e.preventDefault());

async function poll() {
  if (inflight || Object.keys(pending).length) return;
  try { const r = await fetch('/api/state', { cache: 'no-store' }); if (r.ok) render(await r.json()); }
  catch (e) { $('status').textContent = 'simulator not responding'; $('status').className = 'status err'; }
}
poll(); setInterval(poll, 300);
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
