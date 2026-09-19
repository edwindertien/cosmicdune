#!/usr/bin/env python3
"""
cosmic dune -- standalone OSC test hub

A small command-line tool for testing/debugging the pod->hub OSC link
before the real hub (which will additionally output DMX to the Laserworld
CUBE 3) exists. Two modes -- run one at a time:

  monitor   Listens for pods' OSC telemetry and prints a live-refreshing
            dashboard. This is the "server" side -- point your pods'
            `net hub <this machine's IP> <port>` at wherever this runs.

  simulate  Sends fake telemetry for N synthetic pods to a target
            host:port, so `monitor` (or the real hub, later) can be
            developed and tested without any physical pods connected.
            This is the "client" side.

Usage:
    python3 osc_test_hub.py monitor [--port 9000]
    python3 osc_test_hub.py simulate [--host 127.0.0.1] [--port 9000] [--pods 6]

Requires: pip install python-osc   (see requirements.txt)

Message format expected from each pod (see the firmware's src/net/osc_link.cpp):
    /pod/<id>/bpm      int    0 if no pulse signal
    /pod/<id>/ibi      int    milliseconds
    /pod/<id>/signal   int    0 or 1
    /pod/<id>/gsrRaw   int    smoothed 12-bit ADC counts
    /pod/<id>/gsr      float  0.0-1.0 normalized
"""
import argparse
import random
import socket
import sys
import threading
import time
from dataclasses import dataclass

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import ThreadingOSCUDPServer
from pythonosc.udp_client import SimpleUDPClient


def get_local_ip() -> str:
    """Best-effort guess at this machine's LAN IP -- the one a pod on the
    same network should be pointed at. Opens a UDP socket toward a public
    address without actually sending anything; the OS resolves which
    local interface/IP it would route through, which is the standard
    trick for this and works even offline (falls back to 127.0.0.1).
    On a machine with several active interfaces (WiFi + Ethernet, a VPN)
    this picks whichever one the OS would use by default, which may not
    be the one the pod is actually on -- double-check with
    ipconfig/ifconfig if a pod still can't reach this."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()

# A pod not heard from in this long is shown OFFLINE rather than just
# displaying its last-known (increasingly stale) values -- roughly the
# same order of magnitude as the firmware's own
# Timing::PULSE_SIGNAL_TIMEOUT_MS, not a value that needs to match exactly.
STALE_AFTER_S = 3.0
REFRESH_HZ = 4


@dataclass
class PodState:
    last_seen: float = 0.0
    bpm: int = 0
    ibi_ms: int = 0
    signal: bool = False
    gsr_raw: int = 0
    gsr_norm: float = 0.0


class Monitor:
    """Listens on UDP `port` and renders a live dashboard of every pod
    heard from. Uses one OSC default handler rather than per-address
    wildcard mappings -- simpler, and doesn't depend on exactly how
    python-osc's '*' wildcard matching handles path segments, which
    varies between OSC implementations."""

    def __init__(self, port: int):
        self.port = port
        self.local_ip = get_local_ip()  # cached once -- doesn't need
                                          # recomputing every render() cycle,
                                          # and IP shouldn't change mid-session
        self.pods: dict[int, PodState] = {}
        self.lock = threading.Lock()

    def on_message(self, address: str, *args) -> None:
        # Expect "/pod/<id>/<leaf>" -- anything else is ignored rather
        # than raising, since a stray message here shouldn't crash the
        # monitor mid-session.
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
            if leaf == "bpm":
                st.bpm = int(value)
            elif leaf == "ibi":
                st.ibi_ms = int(value)
            elif leaf == "signal":
                st.signal = bool(value)
            elif leaf == "gsrRaw":
                st.gsr_raw = int(value)
            elif leaf == "gsr":
                st.gsr_norm = float(value)
            # Unknown leaves are ignored -- new fields the firmware might
            # add later show up as a no-op here until this script is
            # updated to display them too, not a crash.

    def render(self) -> None:
        now = time.time()
        with self.lock:
            rows = sorted(self.pods.items())

        # Clear screen + home cursor (ANSI) -- fine for any real terminal;
        # if this ever needs to run somewhere that doesn't support ANSI,
        # drop these two escapes and let it scroll instead.
        sys.stdout.write("\x1b[2J\x1b[H")
        print(f"cosmic dune -- OSC test hub, listening on UDP :{self.port}")
        print(f"This machine's IP: {self.local_ip}  ->  on each pod: net hub {self.local_ip} {self.port}")
        print(f"{'pod':>4} {'age':>6} {'status':>8} {'bpm':>5} {'ibi(ms)':>8} "
              f"{'signal':>7} {'gsr raw':>8} {'gsr norm':>9}")
        if not rows:
            print("  (no pods heard from yet)")
        for pod_id, st in rows:
            age = now - st.last_seen
            status = "OFFLINE" if age > STALE_AFTER_S else "ok"
            print(f"{pod_id:>4} {age:>5.1f}s {status:>8} {st.bpm:>5} {st.ibi_ms:>8} "
                  f"{('yes' if st.signal else 'no'):>7} {st.gsr_raw:>8} {st.gsr_norm:>9.2f}")
        print("\n(Ctrl+C to quit)")

    def run(self) -> None:
        disp = Dispatcher()
        disp.set_default_handler(self.on_message)
        server = ThreadingOSCUDPServer(("0.0.0.0", self.port), disp)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            while True:
                self.render()
                time.sleep(1.0 / REFRESH_HZ)
        except KeyboardInterrupt:
            pass
        finally:
            server.shutdown()


def simulate(host: str, port: int, num_pods: int, rate_hz: float) -> None:
    """Sends fake telemetry for `num_pods` synthetic pods -- each with its
    own slowly-drifting BPM/GSR baseline plus small per-tick noise, so
    `monitor` (or the real hub) has something plausible-looking to react
    to without real hardware connected. Ctrl+C to stop."""
    client = SimpleUDPClient(host, port)
    state = [
        {"bpm": random.uniform(60, 90), "gsr": random.uniform(0.1, 0.4)}
        for _ in range(num_pods)
    ]
    print(f"Simulating {num_pods} pod(s) -> {host}:{port} at {rate_hz}Hz each. Ctrl+C to stop.")
    try:
        while True:
            for i, st in enumerate(state):
                pod_id = i + 1
                # Small random walk, clamped to a plausible range -- not
                # trying to model a real heart/skin response, just giving
                # the dashboard something that visibly moves and varies
                # between pods.
                st["bpm"] = max(45, min(180, st["bpm"] + random.uniform(-1.5, 1.5)))
                st["gsr"] = max(0.0, min(1.0, st["gsr"] + random.uniform(-0.02, 0.02)))
                bpm = int(round(st["bpm"]))
                ibi_ms = int(round(60000 / bpm))
                gsr_raw = int(2000 + st["gsr"] * 1500)  # arbitrary plausible 12-bit-ish range
                client.send_message(f"/pod/{pod_id}/bpm", bpm)
                client.send_message(f"/pod/{pod_id}/ibi", ibi_ms)
                client.send_message(f"/pod/{pod_id}/signal", 1)
                client.send_message(f"/pod/{pod_id}/gsrRaw", gsr_raw)
                client.send_message(f"/pod/{pod_id}/gsr", round(st["gsr"], 3))
            time.sleep(1.0 / rate_hz)
    except KeyboardInterrupt:
        print("\nstopped")


def main() -> None:
    ap = argparse.ArgumentParser(description="cosmic dune standalone OSC test hub")
    sub = ap.add_subparsers(dest="mode", required=True)

    mon = sub.add_parser("monitor", help="listen for pod telemetry and show a live dashboard")
    mon.add_argument("--port", type=int, default=9000)

    sim = sub.add_parser("simulate", help="send fake telemetry for N synthetic pods")
    sim.add_argument("--host", default="127.0.0.1")
    sim.add_argument("--port", type=int, default=9000)
    sim.add_argument("--pods", type=int, default=6)
    sim.add_argument("--rate", type=float, default=10.0, help="messages per pod per second")

    args = ap.parse_args()
    if args.mode == "monitor":
        Monitor(args.port).run()
    elif args.mode == "simulate":
        simulate(args.host, args.port, args.pods, args.rate)


if __name__ == "__main__":
    main()
