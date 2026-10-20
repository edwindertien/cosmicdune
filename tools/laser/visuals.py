"""
cosmic dune -- pod-driven laser visuals: six pods -> one pulse.

Inputs are six PodInput (bpm, gsr, online) plus ONE number, sync (0..1), that says how
much the six individual pulses are merged into a single shared one:

    sync = 0   every pod pulses on its own: own rate, own colour, own place
    sync = 1   one pulse: all in the same phase, merged into one shape

Every scene is the same idea drawn differently. Each pod has a heartbeat oscillator
running at ITS bpm. What is drawn uses a display phase that is pulled toward the shared
group phase by `sync` -- so sync is literally "how locked together the six pulses are".
A pulse swells quickly and relaxes slowly (a heartbeat, not a sine), taking a ring from a
small circle ("dot") to a big one and back.

SAFETY NOTES
  * The "dot" is never a dot: rings never shrink below min_r, and shrinking rings are
    dimmed (ring_dim) so the energy per unit length stays roughly constant.
  * Near-identical rings are drawn ONCE (merge_circles), both to save the point budget and
    so merging pods don't pile six beams onto the same line.
  * Offline pods are skipped; with nobody online a dim idle ring breathes at 72 bpm.

Where sync comes from (see SyncMeter / effective_sync): measured from how close the pods'
BPMs are, plus a manual "assist" knob so an operator can push the piece toward unity.
Note this is RATE sync (similar BPM). True beat-phase sync would need the pods to send a
beat event; today they only send a smoothed BPM.
"""
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, replace
from typing import Callable, Dict, List, Optional, Tuple

from patterns import Color, Stroke, circle_pts, gradient

NUM_PODS = 6
TAU = 2.0 * math.pi
DIM_MIN = 0.45          # brightness of the smallest ring relative to a big one


# ---- pulse shape / small helpers ---------------------------------------------
def beat_shape(ph: float, attack: float = 0.18, gamma: float = 1.8) -> float:
    """0..1 heartbeat-ish pulse over one cycle (ph in cycles): quick swell, slow relax."""
    ph %= 1.0
    if ph < attack:
        return 0.5 - 0.5 * math.cos(math.pi * ph / attack)
    x = (ph - attack) / (1.0 - attack)
    return (1.0 - x) ** gamma


def wrap_half(x: float) -> float:
    """Wrap a phase difference (cycles) to (-0.5, 0.5]."""
    return (x + 0.5) % 1.0 - 0.5


def ring_dim(r: float, min_r: float) -> float:
    return DIM_MIN + (1.0 - DIM_MIN) * max(0.0, min(1.0, (r - min_r) / (0.5 - min_r)))


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass
class PodInput:
    bpm: float = 0.0
    gsr: float = 0.0          # 0..1 normalised, as sent by the pod
    online: bool = False
    simulated: bool = False   # True for demo pods (including ones filling empty slots)


# ---- inputs ------------------------------------------------------------------
class DemoInputs:
    """Six scripted pods. Their BPMs start scattered (56..102), converge to one rate, hold,
    then scatter again, on a 90 s cycle (x speed). GSR wanders so the colours move."""

    BASE = [56.0, 64.0, 72.0, 82.0, 92.0, 102.0]

    def __init__(self, speed: float = 1.0, cycle_s: float = 90.0) -> None:
        self.speed = speed
        self.cycle = cycle_s

    def converge(self, t: float) -> float:
        u = ((t * self.speed) % self.cycle) / self.cycle

        def sm(x: float) -> float:
            x = clamp01(x)
            return x * x * (3 - 2 * x)
        if u < 0.33:
            return sm(u / 0.33)
        if u < 0.50:
            return 1.0
        if u < 0.83:
            return 1.0 - sm((u - 0.5) / 0.33)
        return 0.0

    def pods(self, t: float) -> List[PodInput]:
        c = self.converge(t)
        mean = sum(self.BASE) / NUM_PODS
        out = []
        for i, b in enumerate(self.BASE):
            bpm = mean + (b - mean) * (1.0 - c) + 0.6 * math.sin(0.7 * t + i)
            gsr = 0.5 + 0.42 * math.sin(TAU * t / (24.0 + 5.0 * i) + 1.3 * i)
            out.append(PodInput(bpm, gsr, True, simulated=True))
        return out

    def note(self) -> str:
        return ""

    def close(self) -> None:
        pass


class OscInputs:
    """Live pods over OSC -- same messages the pod firmware sends (/pod/<1-6>/bpm|signal|gsr).
    Needs python-osc. Point the pods at this machine: `net hub <this ip> <port>` on each pod."""

    STALE_S = 3.0

    def __init__(self, port: int) -> None:
        try:
            from pythonosc.dispatcher import Dispatcher
            from pythonosc.osc_server import ThreadingOSCUDPServer
        except ImportError:
            raise RuntimeError("--osc-port needs python-osc:  pip install python-osc")
        self._lock = threading.Lock()
        self._d: Dict[int, Dict[str, float]] = {}
        disp = Dispatcher()
        disp.set_default_handler(self._on)
        self._server = ThreadingOSCUDPServer(("0.0.0.0", port), disp)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def _on(self, address: str, *args) -> None:
        parts = address.split("/")
        if len(parts) != 4 or parts[1] != "pod" or not args:
            return
        try:
            pid = int(parts[2])
        except ValueError:
            return
        if not 1 <= pid <= NUM_PODS:
            return
        with self._lock:
            st = self._d.setdefault(pid, {"t": 0.0, "bpm": 0.0, "gsr": 0.0, "signal": 1.0})
            st["t"] = time.time()
            if parts[3] == "bpm":
                st["bpm"] = float(args[0])
            elif parts[3] == "gsr":
                st["gsr"] = float(args[0])
            elif parts[3] == "signal":
                st["signal"] = float(args[0])

    def pods(self, t: float) -> List[PodInput]:
        now = time.time()
        out = []
        with self._lock:
            for pid in range(1, NUM_PODS + 1):
                st = self._d.get(pid)
                online = bool(st and now - st["t"] < self.STALE_S and st["signal"] and 30.0 <= st["bpm"] <= 220.0)
                out.append(PodInput(st["bpm"], st["gsr"], True) if online else PodInput())
        return out

    def note(self) -> str:
        return ""

    def close(self) -> None:
        self._server.shutdown()


def summary_to_pods(summary: Dict) -> List[PodInput]:
    """The dashboard's per-pod summary ({pod id: {status, bpm, signal, gsr_norm, ...}}, ids as
    ints or strings) -> six PodInput. ONE definition of 'online' for everything that reads the
    dashboard: status ok, a pulse signal, and a plausible BPM."""
    pods = [PodInput() for _ in range(NUM_PODS)]
    for key, p in summary.items():
        try:
            pid = int(key)
            bpm = float(p.get("bpm", 0))
        except (TypeError, ValueError):
            continue
        if 1 <= pid <= NUM_PODS and p.get("status") == "ok" and p.get("signal") and 30.0 <= bpm <= 220.0:
            pods[pid - 1] = PodInput(bpm, float(p.get("gsr_norm", 0.0)), True)
    return pods


class DashboardInputs:
    """Pods as osc_web_dashboard.py sees them: polls <url>/api/summary in a background thread
    (standard library only; the render loop never waits on HTTP). This is how the laser tool
    runs ALONGSIDE the dashboard: the dashboard stays the only OSC listener on the pods' UDP
    port, and the pods need no second hub address."""

    STALE_S = 2.0

    def __init__(self, base_url: str = "http://127.0.0.1:5000", interval: float = 0.1) -> None:
        self._url = base_url.rstrip("/") + "/api/summary"
        self._interval = interval
        self._lock = threading.Lock()
        self._pods = [PodInput() for _ in range(NUM_PODS)]
        self._ok_at = -1e9
        self._stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        import json
        import urllib.request
        while not self._stop:
            try:
                with urllib.request.urlopen(self._url, timeout=1.0) as r:
                    data = json.loads(r.read().decode())
                pods = summary_to_pods(data.get("pods", {}))
                with self._lock:
                    self._pods = pods
                    self._ok_at = time.monotonic()
            except Exception:
                pass
            time.sleep(self._interval)

    def _fresh(self) -> bool:
        return time.monotonic() - self._ok_at < self.STALE_S

    def pods(self, t: float) -> List[PodInput]:
        with self._lock:
            return list(self._pods) if self._fresh() else [PodInput() for _ in range(NUM_PODS)]

    def note(self) -> str:
        with self._lock:
            return "" if self._fresh() else "DASHBOARD UNREACHABLE"

    def close(self) -> None:
        self._stop = True


class FilledInputs:
    """Real pods where they are online; simulated pods in every empty slot. For testing with
    fewer than six units: your one pod (set `net pod 1`) drives slot 1 with its real BPM and
    GSR colour, and the other five are demo pods."""

    def __init__(self, primary, filler) -> None:
        self.primary, self.filler = primary, filler

    def pods(self, t: float) -> List[PodInput]:
        real, fake = self.primary.pods(t), self.filler.pods(t)
        return [r if r.online else f for r, f in zip(real, fake)]

    def note(self) -> str:
        return self.primary.note()

    def close(self) -> None:
        self.primary.close()
        self.filler.close()


# ---- sync --------------------------------------------------------------------
class SyncMeter:
    """How alike are the pods' heart rates? 1 - (mean absolute deviation / tol_bpm),
    low-pass filtered (tau seconds) so the visuals ease rather than jump."""

    def __init__(self, tol_bpm: float = 10.0, tau: float = 1.5) -> None:
        self.tol = tol_bpm
        self.tau = tau
        self.value = 0.0

    def update(self, dt: float, bpms: List[float]) -> float:
        if len(bpms) >= 2:
            mean = sum(bpms) / len(bpms)
            mad = sum(abs(b - mean) for b in bpms) / len(bpms)
            raw = max(0.0, 1.0 - mad / self.tol)
        else:
            raw = 0.0
        self.value += (1.0 - math.exp(-dt / self.tau)) * (raw - self.value)
        return self.value


def effective_sync(measured: float, knob: float) -> float:
    """The knob is an 'assist': 0 = purely what the pods do, 1 = fully merged regardless."""
    return clamp01(measured + knob * (1.0 - measured))


# ---- scenes ------------------------------------------------------------------
def merge_circles(circles: List[Tuple[float, float, float, Color]], tol: float):
    """Near-duplicate circles (cx, cy, r, colour) are drawn once, at their mean."""
    groups: List[List[Tuple[float, float, float, Color]]] = []
    for c in circles:
        for g in groups:
            ref = g[0]
            if abs(ref[0] - c[0]) + abs(ref[1] - c[1]) + abs(ref[2] - c[2]) < tol:
                g.append(c)
                break
        else:
            groups.append([c])
    out = []
    for g in groups:
        n = float(len(g))
        out.append((sum(c[0] for c in g) / n, sum(c[1] for c in g) / n, sum(c[2] for c in g) / n,
                    (sum(c[3][0] for c in g) / n, sum(c[3][1] for c in g) / n, sum(c[3][2] for c in g) / n)))
    return out


class Scene:
    name = "scene"
    description = ""

    def __init__(self, min_r: float = 0.14, palette: Callable[[float], Color] = gradient) -> None:
        self.min_r = min_r
        self.palette = palette
        self.phase = [i / NUM_PODS for i in range(NUM_PODS)]   # staggered start: unsynced looks unsynced
        self.group = 0.0
        self.disp = [0.0] * NUM_PODS
        self.pulse = [0.0] * NUM_PODS
        self.group_pulse = 0.0
        self.sync = 0.0

    def update(self, dt: float, pods: List[PodInput], sync: float) -> None:
        online = [p.bpm for p in pods if p.online and p.bpm > 0]
        group_bpm = sum(online) / len(online) if online else 72.0
        self.group = (self.group + group_bpm / 60.0 * dt) % 1.0
        self.sync = sync
        for i, p in enumerate(pods):
            bpm = p.bpm if (p.online and p.bpm > 0) else group_bpm
            self.phase[i] = (self.phase[i] + bpm / 60.0 * dt) % 1.0
            self.disp[i] = (self.phase[i] + sync * wrap_half(self.group - self.phase[i])) % 1.0
            self.pulse[i] = beat_shape(self.disp[i])
        self.group_pulse = beat_shape(self.group)

    def strokes(self, pods: List[PodInput]) -> List[Stroke]:
        if not any(p.online for p in pods):
            return [self.ring(0.0, 0.0, self.min_r + (0.6 - self.min_r) * self.group_pulse, (0.6, 0.6, 0.6))]
        return self._strokes(pods)

    def _strokes(self, pods: List[PodInput]) -> List[Stroke]:
        raise NotImplementedError

    def color(self, pod: PodInput) -> Color:
        return self.palette(clamp01(pod.gsr))

    def ring(self, cx: float, cy: float, r: float, color: Color) -> Stroke:
        d = ring_dim(r, self.min_r)
        return Stroke(circle_pts(cx, cy, r, 48 + int(80 * r)), (color[0] * d, color[1] * d, color[2] * d), smooth=True)


class Bloom(Scene):
    name = "bloom"
    description = ("ONE ring in six 60-degree sectors, one per pod (pod 1 at the top, clockwise). Each sector "
                   "breathes dot<->circle on its own pod's pulse and glows in its pod's colour. sync 0: a pulsing "
                   "six-petal flower. sync 1: a single round ring breathing as one.")

    def _strokes(self, pods: List[PodInput]) -> List[Stroke]:
        n, rmax = 180, 0.95
        rs: List[float] = []
        cols: List[Color] = []
        for i, p in enumerate(pods):
            pulse = self.pulse[i] if p.online else self.group_pulse
            rs.append(self.min_r + (rmax - self.min_r) * pulse)
            cols.append(self.color(p) if p.online else (0.6, 0.6, 0.6))
        pts: List[Tuple[float, float]] = []
        colors: List[Color] = []
        for k in range(n + 1):
            th = TAU * k / n
            r = c0 = c1 = c2 = 0.0
            for i in range(NUM_PODS):
                d = (th - math.radians(90 - 60 * i) + math.pi) % TAU - math.pi
                if abs(d) < math.pi / 3:
                    w = 0.5 + 0.5 * math.cos(3 * d)          # raised cosine: weights of neighbours sum to 1
                    r += w * rs[i]
                    c0 += w * cols[i][0]
                    c1 += w * cols[i][1]
                    c2 += w * cols[i][2]
            dim = ring_dim(r, self.min_r)
            pts.append((r * math.cos(th), r * math.sin(th)))
            colors.append((c0 * dim, c1 * dim, c2 * dim))
        return [Stroke(pts, colors=colors, smooth=True)]


class Converge(Scene):
    name = "converge"
    description = ("Six small pulsing rings, one per pod, scattered round a hexagon. As sync rises they all slide "
                   "to the centre and fuse into ONE spot there, whose pulse grows as they lock together. "
                   "sync 0: six separate pulses. sync 1: one pulsing spot.")

    def _strokes(self, pods: List[PodInput]) -> List[Stroke]:
        rho = 0.62 * (1.0 - self.sync)                     # distance of each ring from the centre
        rmax = 0.28 + 0.30 * self.sync * self.sync        # how big the pulse gets (grows as they lock)
        circles = []
        for i, p in enumerate(pods):
            if not p.online:
                continue
            ang = math.radians(90 - 60 * i)
            r = self.min_r + (rmax - self.min_r) * self.pulse[i]
            circles.append((rho * math.cos(ang), rho * math.sin(ang), r, self.color(p)))
        return [self.ring(*c) for c in merge_circles(circles, 0.05)]


class Merge(Scene):
    name = "merge"
    description = ("Six small rings, one per pod, each breathing at its own rate and colour, scattered round a "
                   "hexagon. As sync rises they drift together and grow; at sync 1 they are one big ring "
                   "pulsing as one.")

    def _strokes(self, pods: List[PodInput]) -> List[Stroke]:
        rho = 0.55 * (1.0 - self.sync) ** 1.2
        rmax = 0.30 + 0.62 * self.sync
        circles = []
        for i, p in enumerate(pods):
            if not p.online:
                continue
            ang = math.radians(90 - 60 * i)
            r = self.min_r + (rmax - self.min_r) * self.pulse[i]
            circles.append((rho * math.cos(ang), rho * math.sin(ang), r, self.color(p)))
        return [self.ring(*c) for c in merge_circles(circles, 0.05)]


class Ripple(Scene):
    name = "ripple"
    description = ("Six concentric rings, one per pod, each pulsing dot<->circle at its own rate. As sync rises "
                   "their pulses lock and their sizes converge until they fuse into a single ring.")

    def _strokes(self, pods: List[PodInput]) -> List[Stroke]:
        circles = []
        for i, p in enumerate(pods):
            if not p.online:
                continue
            ri = 0.92 - 0.07 * i * (1.0 - self.sync)
            r = self.min_r + (ri - self.min_r) * self.pulse[i]
            circles.append((0.0, 0.0, r, self.color(p)))
        return [self.ring(*c) for c in merge_circles(circles, 0.04)]


class Spokes(Scene):
    name = "spokes"
    description = ("Six rays from the centre, one per pod, each lengthening and shrinking on its pulse, in its "
                   "pod's colour. sync 0: a ragged starburst. sync 1: a symmetric star breathing as one.")

    def _strokes(self, pods: List[PodInput]) -> List[Stroke]:
        out = []
        for i, p in enumerate(pods):
            if not p.online:
                continue
            ang = math.radians(90 - 60 * i)
            r0, r1 = 0.08, 0.28 + 0.67 * self.pulse[i]
            out.append(Stroke([(r0 * math.cos(ang), r0 * math.sin(ang)), (r1 * math.cos(ang), r1 * math.sin(ang))],
                              self.color(p)))
        return out


SCENES: Dict[str, type] = {"converge": Converge, "merge": Merge, "bloom": Bloom, "ripple": Ripple, "spokes": Spokes}


def lit_extent(strokes: List[Stroke], view) -> float:
    """Widest side of the lit pattern's bounding box in final field units (the full scan field is 2.0
    wide), i.e. how big the pattern really is on the wall. Fast path when the view has no rotation."""
    xs_lo = ys_lo = 1e9
    xs_hi = ys_hi = -1e9
    rotated = bool(view.rotate_deg)
    for s in strokes:
        if len(s.pts) < 2:
            continue
        for x, y in s.pts:
            if rotated:
                x, y = view.apply(x, y)
            if x < xs_lo: xs_lo = x
            if x > xs_hi: xs_hi = x
            if y < ys_lo: ys_lo = y
            if y > ys_hi: ys_hi = y
    if xs_hi < xs_lo:
        return 0.0
    ext = max(xs_hi - xs_lo, ys_hi - ys_lo)
    return ext if rotated else ext * view.size


def fit_min_extent(strokes: List[Stroke], view, min_extent: float, max_size: float = 1.0):
    """Never let the lit pattern get smaller than `min_extent` (field units): if it would, enlarge the whole
    pattern about the centre -- by scaling the view's size, per frame, so it varies smoothly with the pulse
    -- but never beyond `max_size` (default: the full scan field). The cap is NOT the SIZE slider's maximum:
    an enlargement only happens when the pattern is small, and its result is then exactly `min_extent`, so
    it cannot grow the pattern's footprint. Returns (view to render with, boost factor >= 1).

    WHY: the scan-fail safety of Laserworld projectors can block "a single beam or a very small pattern"
    (Laserworld FAQ), and a small pattern also concentrates the beam. Scenes are smallest when every pod
    pulses in unison (high sync), so that is exactly when this matters. Only for scenes: the static test
    patterns are left alone so small sizes can still be tried deliberately."""
    if min_extent <= 0:
        return view, 1.0
    ext = lit_extent(strokes, view)
    if ext <= 1e-9 or ext >= min_extent:
        return view, 1.0
    size_eff = min(max(view.size, max_size), view.size * (min_extent / ext))
    if size_eff <= view.size:
        return view, 1.0
    return replace(view, size=size_eff), size_eff / view.size


def scene_min_r(size: float) -> float:
    """Smallest ring radius (pattern units): never a point, and always big enough that the
    final ring is wide enough to pass the static-dot check at the chosen --size."""
    return min(0.25, max(0.14, 0.03 / max(size, 1e-6)))


def simulate(scene: Scene, inputs, meter: Optional[SyncMeter], seconds: float, fixed_sync: Optional[float] = None,
             knob: float = 0.0, fps: float = 30.0):
    """Step a scene through `seconds` of simulated time. Returns (pods, effective sync, measured)."""
    dt = 1.0 / fps
    pods: List[PodInput] = []
    eff = measured = 0.0
    t = 0.0
    for _ in range(max(1, int(seconds * fps))):
        t += dt
        pods = inputs.pods(t)
        if fixed_sync is not None:
            measured, eff = 0.0, fixed_sync
        else:
            measured = meter.update(dt, [p.bpm for p in pods if p.online]) if meter else 0.0
            eff = effective_sync(measured, knob)
        scene.update(dt, pods, eff)
    return pods, eff, measured
