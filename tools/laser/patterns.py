"""
cosmic dune -- laser test patterns and frame renderer (no hardware needed).

Coordinates: x right, y up; -1..+1 is the full scan field. Colours 0..1.
A pattern is a function  f(t, params) -> list of Stroke  (t = seconds).
render() turns strokes into one DAC frame: it samples lines at a density
chosen to land on a sensible frame length, adds the blanked moves and
settling/corner points that real galvos need, and always starts AND ends the
frame with the beam off at the centre -- so if frames ever stop arriving,
the mirrors are resting at a blanked point, never on a bright one.

validate() is a seatbelt, not a feature: it refuses frames that could park a
bright static dot or exceed the intensity cap, whatever generated them.
"""
from __future__ import annotations

import math
import struct
import zlib
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Optional, Tuple

Point2 = Tuple[float, float]
Color = Tuple[float, float, float]

# ---- limits / tuning --------------------------------------------------------
HELIOS_MAX_POINTS = 4095      # USB Helios frame limit (0xFFF)
MIN_STEP = 0.002              # densest sampling of a lit line (fraction of full field)
MAX_STEP = 0.020              # coarsest before a line starts to look dotted
TRAVEL_STEP = 0.060           # blanked moves may be coarser
SETTLE_POINTS = 4             # blanked dwell where the beam turns on / off
CORNER_DWELL = 4              # lit repeats at sharp corners so mirrors reach them
MIN_FRAME_MS = 12.0           # SDK: 10 ms absolute minimum, 20-40 ms recommended
MAX_FRAME_MS = 50.0           # slower than this flickers visibly
REPEAT_TO_MS = 20.0           # frames shorter than this repeat the shape (SDK: 20-40 ms is robust
                              # against host hiccups; 10 ms is its absolute minimum)
MIN_EXTENT = 0.04             # lit extent below this = a static dot -> refused
MAX_DWELL_ON = 8              # max consecutive lit points at one spot (0.4 ms @ 20 kpps)


class UnsafeFrame(Exception):
    """Frame refused by validate()."""


@dataclass
class Stroke:
    """A connected lit polyline. smooth=True for finely sampled curves
    (no corner dwell); False for polygons/lines with real corners."""
    pts: List[Point2]
    color: Color = (1.0, 1.0, 1.0)
    smooth: bool = False
    colors: Optional[List[Color]] = None     # per-vertex colours (blended along each segment);
                                             # overrides `color`. Lets one continuous stroke change colour
                                             # without the blanking gaps separate strokes would need.


@dataclass
class View:
    """How pattern space maps onto the scan field, plus the power cap."""
    size: float = 0.3          # fraction of the full field the pattern occupies
    intensity: float = 0.15    # multiplies every colour; 1.0 = full analog level
    rotate_deg: float = 0.0
    flip_x: bool = False
    flip_y: bool = False
    swap_xy: bool = False
    offset_x: float = 0.0
    offset_y: float = 0.0

    def apply(self, x: float, y: float) -> Point2:
        x *= self.size
        y *= self.size
        if self.rotate_deg:
            a = math.radians(self.rotate_deg)
            x, y = x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)
        if self.swap_xy:
            x, y = y, x
        if self.flip_x:
            x = -x
        if self.flip_y:
            y = -y
        x += self.offset_x
        y += self.offset_y
        return (max(-1.0, min(1.0, x)), max(-1.0, min(1.0, y)))


@dataclass
class Calib:
    """Hardware calibration -- properties of YOUR projector/DAC, not of any pattern.
    settle        blanked points where the beam turns on/off (lets the mirrors arrive)
    corner_dwell  lit repeats at sharp corners
    travel_step   size of blanked jumps (smaller = gentler on the galvos, less ringing/noise)
    blank_shift   delay the colour stream by N points vs. the mirrors (negative = earlier).
                  Positive compensates mirror lag: use it if lines start with a streak from
                  the previous position or end short.
    rgb_min       per-channel threshold: the fraction of --intensity where that diode just
                  starts to emit (measure with the 'ramp' pattern). 0 = no compensation.
    rgb_gain      per-channel ceiling (<=1) to balance a channel that is too strong.
    The drive for a pattern colour c (0..1) is  intensity * gain * (min + (1-min)*c),  and 0
    for c == 0, so the intensity cap still bounds every channel."""
    settle: int = SETTLE_POINTS
    corner_dwell: int = CORNER_DWELL
    travel_step: float = TRAVEL_STEP
    blank_shift: int = 0
    rgb_min: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    rgb_gain: Tuple[float, float, float] = (1.0, 1.0, 1.0)


@dataclass
class Frame:
    points: List[Tuple[float, float, float, float, float]]   # x, y, r, g, b (floats)
    pps: int
    step: float
    repeats: int
    over_budget: bool
    on_length: float

    @property
    def duration_ms(self) -> float:
        return 1000.0 * len(self.points) / self.pps

    @property
    def fps(self) -> float:
        return self.pps / max(1, len(self.points))


# ---- geometry helpers -------------------------------------------------------
def _dist(a: Point2, b: Point2) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def _lerp(a: Point2, b: Point2, t: float) -> Point2:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def circle_pts(cx: float, cy: float, r: float, n: int = 180, start_deg: float = 0.0) -> List[Point2]:
    """Closed circle: n+1 points, last == first."""
    return [(cx + r * math.cos(math.radians(start_deg) + 2 * math.pi * k / n),
             cy + r * math.sin(math.radians(start_deg) + 2 * math.pi * k / n)) for k in range(n + 1)]


# Same four-stop palette the pod's LED strip uses: white -> blue -> purple -> red.
_STOPS = [(1.0, 1.0, 1.0), (60 / 255, 120 / 255, 1.0), (160 / 255, 30 / 255, 220 / 255), (1.0, 0.0, 0.0)]


def gradient(level: float) -> Color:
    level = max(0.0, min(1.0, level))
    seg = min(2, int(level * 3))
    f = level * 3 - seg
    a, b = _STOPS[seg], _STOPS[seg + 1]
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f)


def _make_palette(stops: List[Color]) -> Callable[[float], Color]:
    nseg = len(stops) - 1

    def fn(level: float) -> Color:
        level = max(0.0, min(1.0, level))
        seg = min(nseg - 1, int(level * nseg))
        f = level * nseg - seg
        a, b = stops[seg], stops[seg + 1]
        return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f)
    return fn


# 'spectrum': blue -> cyan -> green -> yellow -> red. Green is what the eye sees best.
spectrum = _make_palette([(0.0, 0.2, 1.0), (0.0, 1.0, 1.0), (0.0, 1.0, 0.0), (1.0, 1.0, 0.0), (1.0, 0.0, 0.0)])
PALETTES: Dict[str, Callable[[float], Color]] = {"gsr": gradient, "spectrum": spectrum}


# ---- the renderer -----------------------------------------------------------
def _lerp3(a: Color, b: Color, t: float) -> Color:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t)


def _build(strokes: List[Stroke], step: float, intensity: float, cal: Calib):
    """Sample strokes (already in final coordinates). Returns (points, on-segment lengths)."""
    pts: List[Tuple[float, float, float, float, float]] = []
    seg_lengths: List[float] = []
    cur: Point2 = (0.0, 0.0)

    def blank(p: Point2) -> None:
        pts.append((p[0], p[1], 0.0, 0.0, 0.0))

    def drive(c: Color) -> Color:
        out = []
        for ch in range(3):
            v = c[ch]
            out.append(0.0 if v <= 1e-6 else
                       intensity * cal.rgb_gain[ch] * (cal.rgb_min[ch] + (1.0 - cal.rgb_min[ch]) * min(1.0, v)))
        return (out[0], out[1], out[2])

    def lit(p: Point2, c: Color) -> None:
        d = drive(c)
        pts.append((p[0], p[1], d[0], d[1], d[2]))

    for _ in range(cal.settle):
        blank(cur)
    for s in strokes:
        if len(s.pts) < 2:
            continue
        cols = s.colors if (s.colors and len(s.colors) == len(s.pts)) else None
        start = s.pts[0]
        n = max(1, math.ceil(_dist(cur, start) / cal.travel_step))
        for k in range(1, n + 1):
            blank(_lerp(cur, start, k / n))
        for _ in range(cal.settle):
            blank(start)
        dwell = 1 if s.smooth else cal.corner_dwell
        flat = None if cols else drive(s.color)            # one colour for the whole stroke: convert it once
        for _ in range(dwell):
            lit(start, cols[0] if cols else s.color)
        for j, (a, b) in enumerate(zip(s.pts, s.pts[1:])):
            ca = cols[j] if cols else s.color
            cb = cols[j + 1] if cols else s.color
            length = _dist(a, b)
            seg_lengths.append(length)
            n = max(1, math.ceil(length / step))
            if flat is not None:
                for k in range(1, n + 1):
                    p = _lerp(a, b, k / n)
                    pts.append((p[0], p[1], flat[0], flat[1], flat[2]))
            else:
                for k in range(1, n + 1):
                    f = k / n
                    lit(_lerp(a, b, f), _lerp3(ca, cb, f))
            for _ in range(dwell - 1):
                lit(b, cb)
        cur = s.pts[-1]
        for _ in range(cal.settle):
            blank(cur)
    n = max(1, math.ceil(_dist(cur, (0.0, 0.0)) / cal.travel_step))
    for k in range(1, n + 1):
        blank(_lerp(cur, (0.0, 0.0), k / n))
    for _ in range(cal.settle):
        blank((0.0, 0.0))
    return pts, seg_lengths


def _base_layout(strokes: List[Stroke], cal: Calib):
    """What _build(strokes, step=1e9, ...) would return as (len(points), seg_lengths), without building the points:
    the search for the right point spacing needs only these two numbers."""
    settle = cal.settle
    total = settle
    seg_lengths: List[float] = []
    cur: Point2 = (0.0, 0.0)
    ceil = math.ceil
    for s in strokes:
        if len(s.pts) < 2:
            continue
        start = s.pts[0]
        dwell = 1 if s.smooth else cal.corner_dwell
        total += max(1, ceil(_dist(cur, start) / cal.travel_step)) + settle + dwell + settle
        for a, b in zip(s.pts, s.pts[1:]):
            seg_lengths.append(_dist(a, b))
            total += dwell                                  # 1 sample per segment + (dwell - 1) corner dwells
        cur = s.pts[-1]
    total += max(1, ceil(_dist(cur, (0.0, 0.0)) / cal.travel_step)) + settle
    return total, seg_lengths


def _shift_colors(pts, n: int):
    """Delay the colour stream by n points relative to the positions (n < 0: advance it)."""
    if n == 0:
        return pts
    N = len(pts)
    out = []
    for i, p in enumerate(pts):
        j = i - n
        c = pts[j][2:5] if 0 <= j < N else (0.0, 0.0, 0.0)
        out.append((p[0], p[1], c[0], c[1], c[2]))
    for idx in (0, N - 1):                       # the frame stays parked dark, whatever the shift
        out[idx] = (out[idx][0], out[idx][1], 0.0, 0.0, 0.0)
    return out


def render(strokes: List[Stroke], view: View, pps: int, target_ms: float = 30.0,
           cal: Optional[Calib] = None) -> Frame:
    """Strokes (pattern space) -> one frame sized for roughly target_ms of playback."""
    cal = cal or Calib()
    cal = replace(cal, settle=max(cal.settle, abs(cal.blank_shift) + 2))     # room for the shift
    final: List[Stroke] = []
    for s in strokes:
        final.append(Stroke([view.apply(x, y) for x, y in s.pts], s.color, s.smooth, s.colors))

    base, seg_lengths = _base_layout(final, cal)                              # 1 sample per segment
    on_length = sum(seg_lengths)
    ceil = math.ceil

    def count(step: float) -> int:
        total = base
        for L in seg_lengths:
            n = ceil(L / step)
            if n > 1:                                      # (== max(1, ceil(L/step)) - 1, without the calls)
                total += n - 1
        return total

    target = int(max(20, min(HELIOS_MAX_POINTS, pps * target_ms / 1000.0)))
    over_budget = False
    if count(MIN_STEP) <= target:
        step = MIN_STEP
    elif count(MAX_STEP) > target:
        step, over_budget = MAX_STEP, True
    else:
        lo, hi = MIN_STEP, MAX_STEP           # count(lo) > target >= count(hi)
        for _ in range(30):
            mid = (lo + hi) / 2
            if count(mid) > target:
                lo = mid
            else:
                hi = mid
        step = hi

    pts, _ = _build(final, step, view.intensity, cal)
    pts = _shift_colors(pts, cal.blank_shift)
    repeats = 1
    min_pts = pps * min(REPEAT_TO_MS, max(MIN_FRAME_MS, target_ms)) / 1000.0
    if len(pts) < min_pts:
        repeats = int(math.ceil(min_pts / len(pts) - 0.25))     # only repeat if >25% short of the target,
                                                                # so a 19.9 ms shape is not doubled to 40 ms
        repeats = max(1, min(repeats, HELIOS_MAX_POINTS // max(1, len(pts))))
        pts = pts * repeats
    return Frame(pts, pps, step, repeats, over_budget, on_length)


def validate(frame: Frame, intensity_cap: float) -> None:
    """Refuse anything that could park a bright static dot or exceed the cap."""
    pts = frame.points
    if not (20 <= len(pts) <= HELIOS_MAX_POINTS):
        raise UnsafeFrame(f"{len(pts)} points per frame (allowed 20..{HELIOS_MAX_POINTS}); "
                          "simplify the pattern or lower --pps")
    for (x, y, r, g, b) in pts:
        if not all(math.isfinite(v) for v in (x, y, r, g, b)) or abs(x) > 1 or abs(y) > 1:
            raise UnsafeFrame("non-finite or out-of-field coordinate")
        if max(r, g, b) > intensity_cap + 1e-9 or min(r, g, b) < 0:
            raise UnsafeFrame("colour above the intensity cap")
    if any(pts[0][2:]) or any(pts[-1][2:]):
        raise UnsafeFrame("frame must start and end with the beam off")
    lit = [p for p in pts if p[2] or p[3] or p[4]]
    if lit:
        xs = [p[0] for p in lit]
        ys = [p[1] for p in lit]
        if max(max(xs) - min(xs), max(ys) - min(ys)) < MIN_EXTENT:
            raise UnsafeFrame("lit area is a static dot (size too small?)")
    run = 0
    prev = None
    for p in pts:
        is_lit = bool(p[2] or p[3] or p[4])
        if is_lit and prev is not None and abs(p[0] - prev[0]) < 1e-4 and abs(p[1] - prev[1]) < 1e-4 \
                and (prev[2] or prev[3] or prev[4]):
            run += 1
            if run >= MAX_DWELL_ON:
                raise UnsafeFrame("beam dwells on one spot too long")
        else:
            run = 0
        prev = p


def _u12(v: float) -> int:
    return min(4095, max(0, int(round((v + 1.0) * 0.5 * 4095))))


def _u8(v: float) -> int:
    return min(255, max(0, int(round(v * 255))))


def to_dac(frame: Frame) -> List[Tuple[int, int, int, int, int]]:
    """Float frame -> (x12, y12, r8, g8, b8) tuples for the Helios driver."""
    out: List[Tuple[int, int, int, int, int]] = []
    append = out.append
    lr = lg = lb = None
    cr = cg = cb = 0
    for x, y, r, g, b in frame.points:
        px = round((x + 1.0) * 0.5 * 4095)
        py = round((y + 1.0) * 0.5 * 4095)
        if px < 0: px = 0
        elif px > 4095: px = 4095
        if py < 0: py = 0
        elif py > 4095: py = 4095
        if r != lr or g != lg or b != lb:                  # a stroke has one colour for many points: convert it once
            cr = round(r * 255); cg = round(g * 255); cb = round(b * 255)
            cr = 0 if cr < 0 else (255 if cr > 255 else cr)
            cg = 0 if cg < 0 else (255 if cg > 255 else cg)
            cb = 0 if cb < 0 else (255 if cb > 255 else cb)
            lr, lg, lb = r, g, b
        append((px, py, cr, cg, cb))
    return out


# ---- patterns ---------------------------------------------------------------
Params = Dict[str, float]


def p_circle(t: float, params: Params) -> List[Stroke]:
    return [Stroke(circle_pts(0, 0, 1.0), (1, 1, 1), smooth=True)]


def p_square(t: float, params: Params) -> List[Stroke]:
    return [Stroke([(-1, -1), (1, -1), (1, 1), (-1, 1), (-1, -1)], (1, 1, 1))]


def p_orient(t: float, params: Params) -> List[Stroke]:
    """Asymmetric on purpose: shows mirroring, rotation and axis swaps at a glance."""
    f_shape = Stroke([(0.4, 0.8), (-0.5, 0.8), (-0.5, -0.8)], (1, 1, 1))
    f_bar = Stroke([(-0.5, 0.1), (0.2, 0.1)], (1, 1, 1))
    marker = Stroke(circle_pts(0.72, 0.72, 0.14, 48), (0, 1, 0), smooth=True)       # green, top-right
    tri = Stroke([(0.5, -0.9), (0.9, -0.9), (0.7, -0.5), (0.5, -0.9)], (1, 0, 0))   # red, bottom-right
    return [f_shape, f_bar, marker, tri]


def p_colors(t: float, params: Params) -> List[Stroke]:
    """Top to bottom: red, green, blue, white -- shows how the colour channels are wired."""
    rows = [((1, 0, 0), 0.6), ((0, 1, 0), 0.2), ((0, 0, 1), -0.2), ((1, 1, 1), -0.6)]
    return [Stroke([(-0.8, y), (0.8, y)], c) for c, y in rows]


def p_ramp(t: float, params: Params) -> List[Stroke]:
    """Each colour ramps from 0 to full along its line (left to right). The point where a line
    first becomes visible, as a FRACTION of its length, is that diode's threshold -- enter it
    as --rgb-min for that channel. Ticks at the bottom mark 0, 25, 50, 75, 100 %."""
    rows = [((1, 0, 0), 0.65), ((0, 1, 0), 0.30), ((0, 0, 1), -0.05), ((1, 1, 1), -0.40)]
    out: List[Stroke] = []
    for c, y in rows:
        out.append(Stroke([(-0.8, y), (0.8, y)], colors=[(0, 0, 0), c]))
    for k in range(5):
        x = -0.8 + 1.6 * k / 4
        out.append(Stroke([(x, -0.62), (x, -0.78)], (1, 1, 1)))
    return out


def p_grid(t: float, params: Params) -> List[Stroke]:
    s: List[Stroke] = [Stroke([(-1, -1), (1, -1), (1, 1), (-1, 1), (-1, -1)], (1, 1, 1))]
    for v in (-0.5, 0.0, 0.5):
        s.append(Stroke([(v, -1), (v, 1)], (1, 1, 1)))
        s.append(Stroke([(-1, v), (1, v)], (1, 1, 1)))
    return s


def p_star(t: float, params: Params) -> List[Stroke]:
    v = [(math.cos(math.radians(90 + k * 144)), math.sin(math.radians(90 + k * 144))) for k in range(6)]
    return [Stroke(v, (1, 1, 1))]


def p_sweep(t: float, params: Params) -> List[Stroke]:
    y = 0.85 * math.sin(t * 0.9)
    return [Stroke([(-1, y), (1, y)], gradient(0.5 + 0.5 * math.sin(t * 0.5)))]


def p_spiral(t: float, params: Params) -> List[Stroke]:
    turns, n = 3.0, 240
    rot = t * 0.8
    pts = [((0.08 + 0.92 * k / n) * math.cos(rot + 2 * math.pi * turns * k / n),
            (0.08 + 0.92 * k / n) * math.sin(rot + 2 * math.pi * turns * k / n)) for k in range(n + 1)]
    return [Stroke(pts, gradient((t * 0.1) % 1.0), smooth=True)]


_RAY_PHASES = [0.0, 2.1, 4.2, 1.0, 3.1, 5.2]


def p_rays(t: float, params: Params) -> List[Stroke]:
    """The project's concept in miniature: six rays (one per pod) that each pulse at
    --bpm. sync=0: every ray pulses out of step; sync=1: all pulse together (the
    'converged' moment). --sync-demo slowly sweeps sync 0 -> 1 -> 0 over 20 s."""
    bpm = params.get("bpm", 72.0)
    sync = params.get("sync", 0.0)
    if params.get("sync_demo"):
        sync = 0.5 - 0.5 * math.cos(2 * math.pi * t / 20.0)
    strokes = []
    for i in range(6):
        ang = math.radians(90 - i * 60)
        phase = 2 * math.pi * (bpm / 60.0) * t + (1.0 - sync) * _RAY_PHASES[i]
        r = 0.30 + 0.70 * (0.5 + 0.5 * math.sin(phase))
        r0 = 0.10
        strokes.append(Stroke([(r0 * math.cos(ang), r0 * math.sin(ang)),
                               (r * math.cos(ang), r * math.sin(ang))], gradient(0.15 + 0.7 * i / 5)))
    return strokes


# name -> (function, animated, description)
PATTERNS: Dict[str, Tuple[Callable[[float, Params], List[Stroke]], bool, str]] = {
    "circle": (p_circle, False, "one circle - first alignment / sanity check"),
    "square": (p_square, False, "square outline - are the corners sharp or rounded off?"),
    "orient": (p_orient, False, "asymmetric F + green circle (top-right) + red triangle (bottom-right) - finds flips/rotation"),
    "colors": (p_colors, False, "red, green, blue, white lines top to bottom - checks colour wiring"),
    "ramp": (p_ramp, False, "each colour ramps 0 -> full left to right: where a line becomes visible = that diode's threshold"),
    "grid": (p_grid, False, "frame + 3x3 grid - linearity, many blanked moves"),
    "star": (p_star, False, "5-point star - sharp corners at speed"),
    "sweep": (p_sweep, True, "horizontal line sweeping up and down, colour drifting"),
    "spiral": (p_spiral, True, "rotating spiral, colour drifting"),
    "rays": (p_rays, True, "six pulsing rays (one per pod) - the project's look; try --bpm, --sync, --sync-demo"),
}


# ---- preview (pure Python, no dependencies) ---------------------------------
def write_png(path: str, w: int, h: int, rgb: bytearray) -> None:
    raw = b"".join(b"\x00" + bytes(rgb[y * w * 3:(y + 1) * w * 3]) for y in range(h))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))
    with open(path, "wb") as f:
        f.write(png)


def _plot(rgb: bytearray, w: int, h: int, x: int, y: int, c: Tuple[int, int, int], add: bool) -> None:
    if 0 <= x < w and 0 <= y < h:
        i = (y * w + x) * 3
        if add:
            rgb[i] = min(255, rgb[i] + c[0])
            rgb[i + 1] = min(255, rgb[i + 1] + c[1])
            rgb[i + 2] = min(255, rgb[i + 2] + c[2])
        else:
            rgb[i], rgb[i + 1], rgb[i + 2] = c


def _line(rgb: bytearray, w: int, h: int, x0: int, y0: int, x1: int, y1: int,
          c: Tuple[int, int, int], add: bool) -> None:
    dx, dy = abs(x1 - x0), -abs(y1 - y0)
    sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
    err = dx + dy
    while True:
        _plot(rgb, w, h, x0, y0, c, add)
        if x0 == x1 and y0 == y1:
            break
        e2 = 2 * err
        if e2 >= dy:
            err += dy
            x0 += sx
        if e2 <= dx:
            err += dx
            y0 += sy


def draw_frame(rgb: bytearray, w: int, h: int, ox: int, oy: int, size: int, frame: Frame) -> None:
    """Draw one frame into a square panel. Blanked travel is a faint grey line,
    drawn FIRST so it can never paint over the lit path; the lit path is drawn
    additively on top, so spots where the beam dwells (corners) come out brighter.
    Brightness is relative to the brightest point in the frame.
    The full scan field is the panel border."""
    def px(x: float, y: float) -> Tuple[int, int]:
        return (ox + int(round((x + 1) / 2 * (size - 1))), oy + int(round((1 - (y + 1) / 2) * (size - 1))))

    border = (40, 46, 56)
    for k in range(size):
        for (a, b) in ((ox + k, oy), (ox + k, oy + size - 1), (ox, oy + k), (ox + size - 1, oy + k)):
            _plot(rgb, w, h, a, b, border, False)
    segs = list(zip(frame.points, frame.points[1:]))
    for a, b in segs:                                   # pass 1: blanked moves
        if not (b[2] or b[3] or b[4]):
            x0, y0 = px(a[0], a[1])
            x1, y1 = px(b[0], b[1])
            if (x0, y0) != (x1, y1):
                _line(rgb, w, h, x0, y0, x1, y1, (46, 46, 46), False)
    peak = max([max(p[2], p[3], p[4]) for p in frame.points] + [1e-9])
    for a, b in segs:                                   # pass 2: lit path on top
        if b[2] or b[3] or b[4]:
            m = max(b[2], b[3], b[4]) or 1.0
            lvl = 130 * (0.3 + 0.7 * m / peak)           # hue from the colour, brightness from the drive
            c = (int(b[2] / m * lvl), int(b[3] / m * lvl), int(b[4] / m * lvl))
            x0, y0 = px(a[0], a[1])
            x1, y1 = px(b[0], b[1])
            _line(rgb, w, h, x0, y0, x1, y1, c, True)
