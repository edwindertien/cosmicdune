#!/usr/bin/env python3
"""
cosmic dune -- laser test patterns for the Helios DAC -> Laserworld CUBE 3 (ILDA).

Talks to the Helios DAC over USB only, so it runs the same on a Mac and on the Pi, fully
offline. No third-party Python packages are needed -- except python-osc for `show --osc-port`
(live pods), which you already have in the dashboard's venv.

Commands
  check                 render + safety-check every pattern AND every scene. No hardware.
  preview [what]        PNG of what will be drawn: a pattern, a scene (--sync, --t), `scenes`
                        (every scene at five sync values) or `all` (every pattern)
  list                  list the patterns and scenes
  probe                 find the DAC, print its name/firmware. Emits NO light.
  run <pattern>         project a test pattern (arm prompt first; stops itself after --seconds)
  cycle                 run through the test patterns one after another
  show <scene>          the pod visuals: six pods -> one pulse (see visuals.py). Demo pods by
                        default; --osc-port N uses the real pods. Live keys while it runs.
  blank                 make sure the laser output is off

Calibrating YOUR projector (all of these apply to `run`, `cycle`, `show` and `preview`):
  python3 laser_test.py run ramp --seconds 120     # each colour ramps 0 -> full, left to right
      the point where a line first becomes visible, as a fraction of its length, is that
      diode's threshold: give it to --rgb-min, e.g.  --rgb-min 0.25,0.4,0.15  (R,G,B).
      Low drive (dim mixed colours, small rings) vanishes below it. --rgb-gain R,G,B tames a
      channel that is too strong.
  --blank-shift N      lines start with a streak / end short -> try +1..+3 (delays the colour
                       vs. the mirrors); lines start late -> try -1..-3
  --settle N / --dwell N / --travel-step X
                       more settling points, more corner dwell, gentler blanked jumps (less
                       galvo noise and ringing). Try --travel-step 0.03 --settle 6 first.

First run, in this order:
  python3 laser_test.py check
  python3 laser_test.py preview --fit            # open laser_preview.png
  python3 laser_test.py probe                    # DAC plugged in, laser still dark
  python3 laser_test.py run circle               # small, dim, 60 s -- then work up from there

Pod visuals (needs no pods -- the demo pods scatter, converge and scatter again every 90 s):
  python3 laser_test.py preview scenes --t 6      # every scene at sync 0 .. 1 -> laser_preview.png
  python3 laser_test.py show merge --dry-run      # try it with no laser, no DAC
  python3 laser_test.py show merge                # for real; ENTER to arm
  python3 laser_test.py show bloom --sync 0.6     # hold sync fixed to tune a look
  python3 laser_test.py show converge --dry-run   # six pulsing rings slide together into one centre spot
  python3 laser_test.py show converge --dashboard # real pods, read from the dashboard running on this machine
  python3 laser_test.py show converge --dashboard --fill-demo   # ONE real pod + five simulated ones
  python3 laser_test.py show merge --osc-port 9001   # alternative: listen for the pods directly (not
                                                  # while the dashboard is running: one UDP port, one listener)
    live keys:  ] [  raise/lower the sync assist knob     0 / 1  knob to min / max
                n p  next / previous scene      b or SPACE  blackout (laser off, program keeps running)
                q  quit
  The sync used by the visuals = measured (how alike the pods' BPMs are) + the knob as an
  "assist" (0 = only what the pods do, 1 = fully merged regardless). With --sync it is fixed.

SAFETY (the CUBE 3 is a multi-watt laser, not a toy)
  * Defaults are deliberately timid: --size 0.3 --intensity 0.15 --seconds 60.
    --intensity above 0.5 needs --allow-high. Raise things in small steps.
  * run/cycle make you press ENTER to "arm" after the DAC is found.
  * Frames are played once (SINGLE_MODE): if this program dies, hangs or the Mac
    sleeps, the DAC turns the laser OFF instead of looping the last frame.
  * Every frame starts and ends with the beam off at the centre, and nothing is
    ever sent that would park a bright static dot (see patterns.validate()).
  * None of that replaces the laser's own interlock/key, a matte target at a few
    metres, nobody in the beam path, and eye safety. Aim at a wall, never at people,
    windows, mirrors or the sky (airspace is what the ILT permit is about).
  * The hardware side (this driver + your CUBE) was written against the SDK's source
    and tested against a mock library -- not against a real DAC. Treat the first
    runs as bring-up.

SETUP -- the Helios library (one-time; needs internet once, not at the venue)
  macOS (universal binary, Intel + Apple Silicon):
      git clone https://github.com/Grix/helios_dac.git
      mkdir -p lib
      cp helios_dac/sdk/cpp/shared_library/libHeliosLaserDAC.dylib lib/
      cp helios_dac/sdk/cpp/libusb_bin/macOS/libusb-1.0.0.dylib   lib/
    (both files in the same folder; `git clone` avoids macOS download quarantine)
  Raspberry Pi / Linux (the .so in the repo is x86-64 only, so build it):
      sudo apt-get install libusb-1.0-0-dev g++
      cd helios_dac/sdk/cpp/shared_library
      g++ -Wall -std=c++14 -fPIC -O2 -c HeliosDacAPI.cpp
      g++ -Wall -std=c++14 -fPIC -O2 -c ../HeliosDac.cpp
      g++ -Wall -std=c++14 -fPIC -O2 -c ../idn/idn.cpp
      g++ -Wall -std=c++14 -fPIC -O2 -c ../idn/idnServerList.cpp
      g++ -Wall -std=c++14 -fPIC -O2 -c ../idn/plt-posix.cpp
      g++ -shared -o libHeliosDacAPI.so HeliosDacAPI.o HeliosDac.o plt-posix.o idn.o idnServerList.o -lusb-1.0 -lpthread
    (-lusb-1.0 must come AFTER the .o files. The SDK's own doc has it before them, which on
     current Debian/Ubuntu/Raspberry Pi OS yields a .so that fails to load: "undefined symbol: libusb_open")
      cp libHeliosDacAPI.so <this folder>/lib/
    USB permissions without sudo -- create /etc/udev/rules.d/90-heliosdac.rules containing:
      ACTION=="add", SUBSYSTEM=="usb", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="e500", MODE="0660", GROUP="plugdev"
    then: sudo udevadm control --reload && sudo udevadm trigger   (and be in group plugdev)
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from typing import Dict, List, Optional

import patterns as P
import visuals as V
import helios_dac as HD
from helios_dac import DummyDAC, HeliosDAC, HeliosError

EXIT_USAGE, EXIT_DEVICE, EXIT_UNSAFE = 2, 3, 4


# ---- argument handling ------------------------------------------------------
def add_view_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("shape / power")
    g.add_argument("--size", type=float, default=0.3, help="fraction of the full scan field (0.05-1.0, default 0.3)")
    g.add_argument("--intensity", type=float, default=0.15,
                   help="colour level, 1.0 = full analog level (default 0.15; >0.5 needs --allow-high)")
    g.add_argument("--pps", type=int, default=20000, help="points per second (5000-60000, default 20000)")
    g.add_argument("--min-extent", type=float, default=0.0,
                   help="scenes never get narrower than this lit width (full field = 2.0 wide); smaller moments are "
                        "enlarged. Default 0 = off (0.2 is a reasonable start). Scenes only, not the test patterns")
    g.add_argument("--frame-ms", type=float, default=30.0, help="target frame length, 12-50 ms (default 30)")
    g = p.add_argument_group("orientation (use the 'orient' pattern to find these)")
    g.add_argument("--rotate", type=float, default=0.0, help="degrees")
    g.add_argument("--flip-x", action="store_true")
    g.add_argument("--flip-y", action="store_true")
    g.add_argument("--swap-xy", action="store_true")
    g.add_argument("--offset-x", type=float, default=0.0)
    g.add_argument("--offset-y", type=float, default=0.0)
    g = p.add_argument_group("'rays' pattern")
    g.add_argument("--bpm", type=float, default=72.0, help="pulse rate of the rays (default 72)")
    g.add_argument("--sync", type=float, default=None,
                   help="rays: 0 = out of step ... 1 = all together.  show: fix the sync at this value")
    g.add_argument("--sync-demo", action="store_true", help="rays: sweep sync 0 -> 1 -> 0 over 20 s")
    g = p.add_argument_group("projector calibration (see the notes at the top of --help)")
    g.add_argument("--rgb-min", default="0,0,0", help="R,G,B threshold fractions 0-0.8 (measure with `run ramp`)")
    g.add_argument("--rgb-gain", default="1,1,1", help="R,G,B ceilings 0.2-1")
    g.add_argument("--blank-shift", type=int, default=0, help="colour delay in points, -6..6 (default 0)")
    g.add_argument("--settle", type=int, default=P.SETTLE_POINTS, help="blanked settle points, 1-12")
    g.add_argument("--dwell", type=int, default=P.CORNER_DWELL, help="lit points at sharp corners, 1-8")
    g.add_argument("--travel-step", type=float, default=P.TRAVEL_STEP, help="size of blanked jumps, 0.01-0.12")


def add_run_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("output")
    g.add_argument("--seconds", type=float, default=60.0,
                   help="stop by itself after this long (default 60; 0 = until Ctrl-C)")
    g.add_argument("--yes", action="store_true", help="skip the ENTER-to-arm prompt")
    g.add_argument("--allow-high", action="store_true", help="permit --intensity above 0.5")
    g.add_argument("--dry-run", action="store_true", help="no hardware: exercise everything else")
    g.add_argument("--lib", help="path to the Helios library (or set $HELIOS_LIB)")
    g.add_argument("--device", type=int, default=0, help="DAC number if several are connected")


def _triple(text: str, name: str, lo: float, hi: float, errs: List[str]):
    try:
        v = tuple(float(x) for x in text.split(","))
        if len(v) != 3 or not all(lo <= x <= hi for x in v):
            raise ValueError
        return v
    except ValueError:
        errs.append(f"{name} must be three numbers R,G,B in {lo:g}..{hi:g}")
        return (0.0, 0.0, 0.0)


def check_args(args) -> None:
    errs = []
    args._rgb_min = _triple(args.rgb_min, "--rgb-min", 0.0, 0.8, errs)
    args._rgb_gain = _triple(args.rgb_gain, "--rgb-gain", 0.2, 1.0, errs)
    if not -6 <= args.blank_shift <= 6:
        errs.append("--blank-shift must be -6..6")
    if not 1 <= args.settle <= 12:
        errs.append("--settle must be 1-12")
    if not 1 <= args.dwell <= 8:
        errs.append("--dwell must be 1-8")
    if not 0.01 <= args.travel_step <= 0.12:
        errs.append("--travel-step must be 0.01-0.12")
    if args.sync is not None and not 0.0 <= args.sync <= 1.0:
        errs.append("--sync must be 0-1")
    if not 0.0 <= args.min_extent <= 1.0:
        errs.append("--min-extent must be 0-1 (0 = off)")
    if not 0.05 <= args.size <= 1.0:
        errs.append("--size must be 0.05-1.0")
    if not 0.0 < args.intensity <= 1.0:
        errs.append("--intensity must be above 0 and at most 1.0")
    if getattr(args, "allow_high", False) is False and args.intensity > 0.5:
        errs.append("--intensity above 0.5 needs --allow-high (and a good reason)")
    if not 5000 <= args.pps <= 60000:
        errs.append("--pps must be 5000-60000")
    if not P.MIN_FRAME_MS <= args.frame_ms <= P.MAX_FRAME_MS:
        errs.append(f"--frame-ms must be {P.MIN_FRAME_MS:g}-{P.MAX_FRAME_MS:g}")
    if getattr(args, "seconds", 1.0) < 0:
        errs.append("--seconds cannot be negative")
    if errs:
        print("error: " + "; ".join(errs), file=sys.stderr)
        sys.exit(EXIT_USAGE)


def view_from(args) -> P.View:
    return P.View(size=args.size, intensity=args.intensity, rotate_deg=args.rotate, flip_x=args.flip_x,
                  flip_y=args.flip_y, swap_xy=args.swap_xy, offset_x=args.offset_x, offset_y=args.offset_y)


def params_from(args) -> Dict[str, float]:
    return {"bpm": args.bpm, "sync": args.sync if args.sync is not None else 0.0, "sync_demo": args.sync_demo}


def calib_from(args) -> P.Calib:
    check_args(args)
    return P.Calib(settle=args.settle, corner_dwell=args.dwell, travel_step=args.travel_step,
                   blank_shift=args.blank_shift, rgb_min=args._rgb_min, rgb_gain=args._rgb_gain)


def render_at(name: str, t: float, view: P.View, params, args) -> P.Frame:
    fn, _anim, _desc = P.PATTERNS[name]
    fr = P.render(fn(t, params), view, args.pps, args.frame_ms, calib_from(args))
    P.validate(fr, view.intensity)
    return fr


def describe(name: str, fr: P.Frame, args) -> str:
    warn = ""
    if fr.over_budget:
        warn += "  [dense: lines may look dotted -- lower --size or raise --pps]"
    if fr.duration_ms > 40:
        warn += "  [slow frame rate: may flicker]"
    return (f"{name}: {len(fr.points)} points/frame = {fr.duration_ms:.1f} ms ({fr.fps:.0f} fps) "
            f"at {args.pps} pps, step {fr.step:.4f}" + (f", repeated x{fr.repeats}" if fr.repeats > 1 else "") + warn)


# ---- commands ---------------------------------------------------------------
def cmd_list(args) -> int:
    print("test patterns (run / cycle / preview):")
    for name, (_f, anim, desc) in P.PATTERNS.items():
        print(f"  {name:8} {'(animated)' if anim else '          '} {desc}")
    print("\npod visuals (show / preview):")
    for name, cls in V.SCENES.items():
        print(f"  {name:8} {cls.description}")
    return 0


def cmd_check(args) -> int:
    check_args(args)
    view, params, bad = view_from(args), params_from(args), 0
    print(f"{'pattern':8} {'pts':>5} {'ms':>6} {'fps':>4}  result")
    for name, (_f, anim, _d) in P.PATTERNS.items():
        for t in ([0.0, 1.7, 5.3, 11.0] if anim else [0.0]):
            try:
                fr = render_at(name, t, view, params, args)
                P.to_dac(fr)
                print(f"{name:8} {len(fr.points):5d} {fr.duration_ms:6.1f} {fr.fps:4.0f}  ok" +
                      (f" (t={t})" if anim else ""))
            except P.UnsafeFrame as e:
                bad += 1
                print(f"{name:8} REFUSED at t={t}: {e}")
    print(f"\n{'scene':8} {'sync':>5} {'max pts':>7} {'max ms':>7} {'min ms':>7} {'render ms avg/max':>18}  result   (8 s of demo pods each)")
    cal = calib_from(args)
    for name, cls in V.SCENES.items():
        for sync in (0.0, 0.5, 1.0):
            sc, demo, dt, t = cls(V.scene_min_r(view.size), P.PALETTES.get(getattr(args, "palette", "gsr"), P.gradient)), V.DemoInputs(), 1 / 30.0, 0.0
            npts, ms, refused, rt = [], [], 0, []
            for k in range(240):
                t += dt
                pods = demo.pods(t)
                sc.update(dt, pods, sync)
                if k % 3:
                    continue
                try:
                    t0 = time.perf_counter()
                    strokes = sc.strokes(pods)
                    v2, _b = V.fit_min_extent(strokes, view, args.min_extent)
                    fr = P.render(strokes, v2, args.pps, args.frame_ms, cal)
                    P.validate(fr, view.intensity)
                    rt.append((time.perf_counter() - t0) * 1000.0)
                    npts.append(len(fr.points))
                    ms.append(fr.duration_ms)
                except P.UnsafeFrame as e:
                    refused += 1
                    last = str(e)
            if refused:
                bad += 1
                print(f"{name:8} {sync:5.2f}  REFUSED {refused}x: {last}")
            else:
                print(f"{name:8} {sync:5.2f} {max(npts):7d} {max(ms):7.1f} {min(ms):7.1f} {sum(rt) / len(rt):8.1f}/{max(rt):<8.1f}  ok")
    print("all patterns and scenes pass" if not bad else f"{bad} problem(s)")
    print("(render time must stay well under the frame length, ~30 ms: if the max here is above ~15 ms on the Pi, "
          "expect 'late' frames in `show`)")
    return 0 if not bad else EXIT_UNSAFE


def _scene_frame(name: str, sync: float, t: float, view: P.View, args, demo_speed: float = 1.0):
    cal = calib_from(args)
    palette = P.PALETTES.get(getattr(args, "palette", "gsr"), P.gradient)
    sc = V.SCENES[name](V.scene_min_r(args.size), palette)
    pods, _eff, _m = V.simulate(sc, V.DemoInputs(demo_speed), None, max(t, 0.1), fixed_sync=sync)
    strokes = sc.strokes(pods)
    v2, _b = V.fit_min_extent(strokes, view, args.min_extent)
    fr = P.render(strokes, v2, args.pps, args.frame_ms, cal)
    P.validate(fr, view.intensity)
    return fr


def cmd_preview(args) -> int:
    check_args(args)
    view = view_from(args)
    if args.fit:
        view.size = 0.92
    params = params_from(args)
    what = args.pattern
    if what == "scenes":                                        # every scene x five sync values
        syncs = (0.0, 0.25, 0.5, 0.75, 1.0)
        names = list(V.SCENES)
        panel, gap = 230, 8
        W, H = len(syncs) * panel + (len(syncs) + 1) * gap, len(names) * panel + (len(names) + 1) * gap
        rgb = bytearray([16, 20, 24] * (W * H))
        pv = P.View(**{**view.__dict__, "size": 0.92})
        for r, nm in enumerate(names):
            for c, sy in enumerate(syncs):
                P.draw_frame(rgb, W, H, gap + c * (panel + gap), gap + r * (panel + gap), panel,
                             _scene_frame(nm, sy, args.t, pv, args))
        P.write_png(args.out, W, H, rgb)
        print(f"wrote {args.out}: rows = {', '.join(names)}; columns = sync {', '.join(f'{x:g}' for x in syncs)} "
              f"(all at t={args.t:g}s of the demo pods)")
        return 0
    if what in V.SCENES:
        sync = args.sync if args.sync is not None else 0.5
        fr = _scene_frame(what, sync, args.t, view, args)
        W = H = 600 + 16
        rgb = bytearray([16, 20, 24] * (W * H))
        P.draw_frame(rgb, W, H, 8, 8, 600, fr)
        P.write_png(args.out, W, H, rgb)
        print(describe(what, fr, args))
        print(f"wrote {args.out}  ({what}, sync {sync:g}, t={args.t:g}s)")
        return 0
    names = list(P.PATTERNS) if what in (None, "all") else [what]
    if names[0] not in P.PATTERNS:
        print(f"unknown pattern or scene {names[0]!r}; try `list`", file=sys.stderr)
        return EXIT_USAGE
    cols = 1 if len(names) == 1 else 3
    rows = (len(names) + cols - 1) // cols
    panel, gap = (600 if len(names) == 1 else 300), 8
    W, H = cols * panel + (cols + 1) * gap, rows * panel + (rows + 1) * gap
    rgb = bytearray([16, 20, 24] * (W * H))
    for i, name in enumerate(names):
        fr = render_at(name, args.t, view, params, args)
        print(describe(name, fr, args))
        P.draw_frame(rgb, W, H, gap + (i % cols) * (panel + gap), gap + (i // cols) * (panel + gap), panel, fr)
    P.write_png(args.out, W, H, rgb)
    print(f"wrote {args.out}  (panel border = the full scan field; grey = blanked moves)")
    if len(names) > 1:
        print("sheet order, left-to-right, top-to-bottom: " + ", ".join(names))
    return 0


def open_dac(args):
    dac = DummyDAC() if args.dry_run else HeliosDAC(lib_path=args.lib, device=args.device)
    dac.open()
    return dac


def cmd_probe(args) -> int:
    dac = open_dac(args)
    try:
        print(f"library : {dac.lib_path}")
        print(f"DAC     : {dac.name or '(unnamed)'}   firmware {dac.firmware}")
        print("OK -- output is blanked; no light was emitted.")
    finally:
        dac.close()
    return 0


def cmd_blank(args) -> int:
    dac = open_dac(args)
    dac.close()
    print("laser output stopped / blanked.")
    return 0


def arm_prompt(args, names: List[str]) -> None:
    print("\n" + "=" * 72)
    print(f"  ABOUT TO EMIT LASER LIGHT:  {', '.join(names)}")
    print(f"  size {args.size}   intensity {args.intensity}   {args.pps} pps"
          + (f"   auto-stop after {args.seconds:g} s" if args.seconds else "   until Ctrl-C"))
    print("  [ ] CUBE is in ILDA mode and the ILDA cable is connected to the DAC")
    print("  [ ] aimed at a matte wall/screen a few metres away -- not at people, windows,")
    print("      mirrors or shiny surfaces, and not upward")
    print("  [ ] nobody in the beam path, you are not at eye height in it")
    print("  [ ] you can cut the laser (key / power) within a second")
    print("=" * 72)
    if not sys.stdin.isatty():
        print("refusing to emit: no interactive terminal to arm from (use --yes to override)", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)
    input("Press ENTER to ARM and start, Ctrl-C to abort: ")


def play(dac, name: str, seconds: float, view: P.View, params, args) -> None:
    fn, animated, _d = P.PATTERNS[name]
    t0 = time.monotonic()
    static = None
    frames = late = 0
    last = None
    last_print = t0
    while True:
        t = time.monotonic() - t0
        if seconds and t >= seconds:
            break
        if animated or static is None:
            fr = render_at(name, t, view, params, args)
            pts = P.to_dac(fr)
            if not animated:
                static = (fr, pts)
        else:
            fr, pts = static
        dac.write(pts, args.pps)
        now = time.monotonic()
        if last is not None and (now - last) > 1.6 * fr.duration_ms / 1000.0:
            late += 1
        last = now
        frames += 1
        if now - last_print >= 1.0:
            last_print = now
            print(f"\r  {name}: t={t:5.1f}s  frames={frames}  late={late}   ", end="", flush=True)
    print(f"\r  {name}: done, {frames} frames, {late} late (a late frame = brief flicker)        ")


def session(args, names: List[str], seconds: float) -> int:
    check_args(args)
    view, params = view_from(args), params_from(args)
    for name in names:                       # pre-flight: refuse before touching the hardware
        for t in ([0.0, 1.7, 5.3, 11.0] if P.PATTERNS[name][1] else [0.0]):
            render_at(name, t, view, params, args)
    print(describe(names[0], render_at(names[0], 0.0, view, params, args), args))
    dac = open_dac(args)
    try:
        print(f"DAC: {dac.name or '(unnamed)'}  firmware {dac.firmware}  via {dac.lib_path}")
        if not args.dry_run and not args.yes:
            arm_prompt(args, names)
        for name in names:
            play(dac, name, seconds, view, params, args)
    except KeyboardInterrupt:
        print("\nstopping ...")
    finally:
        dac.close()
        print("laser output stopped.")
    return 0


class KeyReader:
    """Single keypresses without Enter (macOS/Linux terminals). Ctrl-C still works. Always
    restored in finally. Silently disabled when stdin is not a terminal."""

    def __init__(self) -> None:
        self.ok = False
        try:
            import select
            import termios
            import tty
            if sys.stdin.isatty():
                self._select, self._termios = select, termios
                self.fd = sys.stdin.fileno()
                self._old = termios.tcgetattr(self.fd)
                tty.setcbreak(self.fd)
                self.ok = True
        except Exception:
            self.ok = False

    def read(self) -> str:
        if not self.ok:
            return ""
        out = b""
        while self._select.select([self.fd], [], [], 0)[0]:
            chunk = os.read(self.fd, 64)
            if not chunk:
                break
            out += chunk
        return out.decode(errors="ignore")

    def restore(self) -> None:
        if self.ok:
            self._termios.tcsetattr(self.fd, self._termios.TCSADRAIN, self._old)
            self.ok = False


def show_session(args) -> int:
    check_args(args)
    view, cal = view_from(args), calib_from(args)
    palette = P.PALETTES[args.palette]
    min_r = V.scene_min_r(args.size)
    names = list(V.SCENES)
    idx = names.index(args.scene)
    # pre-flight: render + validate every scene across the sync range BEFORE touching the hardware
    for nm in names:
        for sy in (0.0, 0.5, 1.0):
            sc, demo, t = V.SCENES[nm](min_r, palette), V.DemoInputs(), 0.0
            for _ in range(90):
                t += 1 / 30.0
                pods = demo.pods(t)
                sc.update(1 / 30.0, pods, sy)
                strokes = sc.strokes(pods)
                v2, _b = V.fit_min_extent(strokes, view, args.min_extent)
                P.validate(P.render(strokes, v2, args.pps, args.frame_ms, cal), view.intensity)
    print(f"{args.scene}: pre-flight passed for all scenes (sync 0 / 0.5 / 1).")
    if args.osc_port and args.dashboard:
        print("error: use --osc-port OR --dashboard, not both", file=sys.stderr)
        return EXIT_USAGE
    try:
        if args.dashboard:
            inputs = V.DashboardInputs(args.dashboard)
            src = f"live pods via the dashboard at {args.dashboard}"
        elif args.osc_port:
            inputs = V.OscInputs(args.osc_port)
            src = f"live OSC on UDP :{args.osc_port}"
        else:
            inputs, src = V.DemoInputs(args.demo_speed), (f"demo (scatter -> converge -> scatter, 90 s cycle, "
                                                           f"x{args.demo_speed:g})")
        if args.fill_demo and (args.dashboard or args.osc_port):
            inputs = V.FilledInputs(inputs, V.DemoInputs(args.demo_speed))
            src += " + simulated pods in the empty slots"
    except (RuntimeError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_DEVICE
    print("pods: " + src)
    dac = open_dac(args)
    keys = None
    try:
        print(f"DAC: {dac.name or '(unnamed)'}  firmware {dac.firmware}  via {dac.lib_path}")
        if not args.dry_run and not args.yes:
            arm_prompt(args, [f"show {args.scene}"])
        scene = V.SCENES[names[idx]](min_r, palette)
        meter = V.SyncMeter(args.tol_bpm)
        fixed = args.sync is not None
        knob = args.sync if fixed else 0.0
        keys = KeyReader()
        print("keys:  ] [ sync assist   0 / 1 knob min / max   n p scene   b/SPACE blackout   q quit"
              if keys.ok else "(no interactive terminal: keys disabled)")
        t0 = last = last_print = time.monotonic()
        frames = late = refused = run_refused = 0
        blackout = False
        last_write = None
        fr = None
        while True:
            now = time.monotonic()
            dt = min(now - last, 0.1)
            last = now
            t = now - t0
            if args.seconds and t >= args.seconds:
                break
            quit_ = False
            for ch in keys.read():
                if ch == "]":
                    knob = min(1.0, knob + 0.05)
                elif ch == "[":
                    knob = max(0.0, knob - 0.05)
                elif ch == "0":
                    knob = 0.0
                elif ch == "1":
                    knob = 1.0
                elif ch in ("n", "p"):
                    idx = (idx + (1 if ch == "n" else -1)) % len(names)
                    scene = V.SCENES[names[idx]](min_r, palette)
                elif ch in ("b", " "):
                    blackout = not blackout
                    if blackout:
                        dac.stop()
                elif ch == "q":
                    quit_ = True
            if quit_:
                break
            pods = inputs.pods(t)
            bpms = [p.bpm for p in pods if p.online]
            measured = 0.0 if fixed else meter.update(dt, bpms)
            eff = knob if fixed else V.effective_sync(measured, knob)
            scene.update(dt, pods, eff)
            if blackout:
                time.sleep(0.03)
            else:
                try:
                    strokes = scene.strokes(pods)
                    v2, _b = V.fit_min_extent(strokes, view, args.min_extent)
                    fr = P.render(strokes, v2, args.pps, args.frame_ms, cal)
                    P.validate(fr, view.intensity)
                    run_refused = 0
                except P.UnsafeFrame:
                    refused += 1
                    run_refused += 1
                    if run_refused > 90:
                        raise
                    time.sleep(0.01)
                    continue
                dac.write(P.to_dac(fr), args.pps)
                done = time.monotonic()
                if last_write is not None and (done - last_write) > 1.6 * fr.duration_ms / 1000.0:
                    late += 1
                last_write = done
                frames += 1
            if now - last_print >= 1.0:
                last_print = now
                lo, hi = (min(bpms), max(bpms)) if bpms else (0, 0)
                live = sum(1 for p in pods if p.online and not p.simulated)
                note = inputs.note()
                print(f"\r  {names[idx]:8} sync {eff:4.2f} (measured {measured:4.2f}, knob {knob:4.2f}{' fixed' if fixed else ''})"
                      f"  pods {len(bpms)}/6 (live {live})  bpm {lo:3.0f}-{hi:3.0f}  frames {frames} late {late} refused {refused}"
                      f"{'  [BLACKOUT]' if blackout else ''}{'  ' + note if note else ''}     ", end="", flush=True)
        print()
    except KeyboardInterrupt:
        print("\nstopping ...")
    finally:
        if keys is not None:
            keys.restore()
        inputs.close()
        dac.close()
        print("laser output stopped.")
    return 0


def cmd_show(args) -> int:
    if args.scene not in V.SCENES:
        print(f"unknown scene {args.scene!r}; try `list`", file=sys.stderr)
        return EXIT_USAGE
    return show_session(args)


def cmd_run(args) -> int:
    if args.pattern not in P.PATTERNS:
        print(f"unknown pattern {args.pattern!r}; try `list`", file=sys.stderr)
        return EXIT_USAGE
    return session(args, [args.pattern], args.seconds)


def cmd_cycle(args) -> int:
    names = args.only.split(",") if args.only else list(P.PATTERNS)
    bad = [n for n in names if n not in P.PATTERNS]
    if bad:
        print(f"unknown pattern(s): {', '.join(bad)}", file=sys.stderr)
        return EXIT_USAGE
    return session(args, names, args.each)


def cmd_usbtest(args) -> int:
    """IDLE polling: ask the DAC 'ready?' in a loop with NO frames at all (output dark). If this fails, frames, polling modes and the
    engine are irrelevant: it is the USB path to the DAC (cable, port, hub, SDK build) or the DAC itself."""
    import collections
    dac = open_dac(args)
    kinds: "collections.Counter[str]" = collections.Counter()
    events: List[str] = []
    polls = errs = run = longest_polls = 0
    worst = total = 0.0
    first_err = run_t0 = None
    longest_ms = 0.0
    try:
        print(f"DAC    : {dac.name or '(unnamed)'}  firmware {dac.firmware}  ({dac.lib_path})")
        if args.dry_run:
            print("idle polling: (dry run: no DAC to poll)")
            return 0
        t0 = time.monotonic()
        end = t0 + args.seconds
        while time.monotonic() < end:
            tp = time.perf_counter()
            st = dac.status_raw()
            dt = time.perf_counter() - tp
            polls += 1
            total += dt
            worst = max(worst, dt)
            now = time.monotonic()
            if st < 0:
                errs += 1
                run += 1
                run_t0 = run_t0 or now
                first_err = first_err if first_err is not None else now - t0
                kind = str(HD.usb_error("GetStatus failed", st)).split(": ", 1)[-1]
                kinds[kind] += 1
                events.append(f"{now - t0:7.3f} s  {kind}  (poll took {1000 * dt:.1f} ms; {run} in a row)")
            elif run:
                longest_polls = max(longest_polls, run)
                longest_ms = max(longest_ms, 1000 * (now - run_t0))
                run, run_t0 = 0, None
            time.sleep(max(0.0, args.interval_ms / 1000.0))
        if run:
            longest_polls = max(longest_polls, run)
            longest_ms = max(longest_ms, 1000 * (time.monotonic() - run_t0))
    finally:
        dac.close()
    wall = max(time.monotonic() - t0, 0.001)
    print(f"idle polling, every {args.interval_ms:g} ms for {wall:.0f} s: {polls} polls, {errs} errors ({60.0 * errs / wall:.1f} per minute); "
          f"one poll {1000 * total / max(1, polls):.1f} ms avg / {1000 * worst:.1f} worst; "
          f"first error {('%.1f s in' % first_err) if first_err is not None else 'none'}; longest run {longest_polls} polls / {longest_ms:.0f} ms")
    for k, n in kinds.most_common():
        print(f"      {n:5d} x {k}")
    if len(events) > 12:
        events = events[:6] + ["   ... (left out) ..."] + events[-6:]
    for e in events:
        print(f"      {e}")
    if errs == 0:
        print("RESULT: idle polling is CLEAN: the DAC answers every status request when no frames are involved.")
    else:
        print("RESULT: idle polling FAILS even with no frames at all: this is the USB path or the DAC, not frames or polling modes.")
    return 0 if errs == 0 else EXIT_DEVICE


def cmd_streamtest(args) -> int:
    """Stream DARK fixed-size frames with NO engine involved until the DAC stops answering; then leave it completely alone and measure how
    long it stays silent and whether it recovers by itself. --mode poll asks 'ready?' before every frame (like the engine); --mode timed
    writes on a fixed period and NEVER polls the status, so comparing the two shows whether polling during streaming is what sets it off."""
    import math
    import statistics
    if args.dry_run:
        print("streamtest: (dry run: no DAC to stream to)")
        return 0
    n, pps = args.points, args.pps
    pts = [(int(2048 + 700 * math.cos(2 * math.pi * k / n)), int(2048 + 700 * math.sin(2 * math.pi * k / n)), 0, 0, 0) for k in range(n)]
    period = n / float(pps)
    dac = open_dac(args)
    episodes: List[dict] = []
    streamed_total = 0.0

    def reopen():
        nonlocal dac
        try:
            dac.close()
        except Exception:
            pass
        try:
            dac = open_dac(args)
            return True
        except HeliosError:
            return False

    try:
        print(f"DAC    : {dac.name or '(unnamed)'}  firmware {dac.firmware}  ({dac.lib_path})")
        print(f"streamtest, mode {args.mode}: {n} DARK points per frame ({1000 * period:.1f} ms), budget {args.seconds:g} s, up to {args.cycles} failure(s) "
              f"({'asks ready? before each frame' if args.mode == 'poll' else ('fixed timing, NO status polling at all' if args.poll_at is None else f'fixed timing, one status poll per frame at {args.poll_at:g} of the frame')})")
        end = time.monotonic() + args.seconds
        while time.monotonic() < end and len(episodes) < args.cycles:
            streak0 = time.monotonic()
            nxt = streak0
            frames = 0
            err = None
            while time.monotonic() < end:
                try:
                    if args.mode == "poll":
                        dac.wait_ready(1.0, poll_s=0.001)
                    t_w = time.monotonic()
                    dac.write(pts, pps, assume_ready=True)
                    frames += 1
                    if args.mode == "timed":
                        if args.poll_at is not None:             # ONE status poll per frame, at this fraction of the frame
                            time.sleep(max(0.0, t_w + args.poll_at * period - time.monotonic()))
                            st_ = dac.status_raw()
                            if st_ < 0:
                                raise HD.usb_error("GetStatus failed", st_)
                        nxt += period + 0.0015
                        time.sleep(max(0.0, nxt - time.monotonic()))
                except HeliosError as e:
                    err = e
                    break
            streamed = time.monotonic() - streak0
            streamed_total += streamed
            if err is None:
                break
            t_fail = time.monotonic()
            where = "GetStatus" if "GetStatus" in str(err) else "WriteFrame"
            time.sleep(args.quiet_ms / 1000.0)                  # the DAC is left completely alone for a while
            reopened, recovered, st = 0, False, 0
            while time.monotonic() - t_fail < args.give_up_s:
                try:
                    st = dac.status_raw()
                except HeliosError:
                    st = -1000                                  # (our handle is closed)
                if st >= 0:
                    recovered = True
                    break
                if st == -1000:
                    reopened += 1
                    reopen()
                time.sleep(args.watch_ms / 1000.0)
            silent = time.monotonic() - t_fail
            ep = {"streamed": streamed, "frames": frames, "where": where, "error": str(err).split(": ", 1)[-1], "silent": silent if recovered else None,
                  "reopened": reopened, "recovered": recovered}
            episodes.append(ep)
            print(f"cycle {len(episodes)}: streamed {streamed:.1f} s ({frames} frames), then {where} failed: {ep['error']}; "
                  + (f"the DAC was silent for {silent:.1f} s" if recovered else f"NOT recovered after {args.give_up_s:g} s: it needs a replug")
                  + (f" [the SDK closed the device; reopened x{reopened}]" if reopened else "") + ("; recovered by itself" if recovered else ""))
            if not recovered:
                break
            try:
                dac.stop()
            except Exception:
                pass
    finally:
        dac.close()
    f = len(episodes)
    if f == 0:
        print(f"RESULT mode {args.mode}: 0 failures in {streamed_total:.1f} s of streaming.")
        return 0
    silent = sorted(e["silent"] for e in episodes if e["silent"] is not None)
    rec = len(silent)
    tail = (f"; silent for {silent[0]:.1f} / {statistics.median(silent):.1f} / {silent[-1]:.1f} s (min / median / max)" if silent else "")
    print(f"RESULT mode {args.mode}: {f} failure(s) in {streamed_total:.1f} s of streaming ({60.0 * f / max(streamed_total, 0.001):.1f} per minute)"
          f"{tail}; recovered unaided {rec} of {f}" + ("" if rec == f else f" (the rest stayed silent for the full {args.give_up_s:g} s: replug needed)") + ".")
    return EXIT_DEVICE


def _machine_line() -> str:
    import platform
    parts = [f"{platform.system()} {platform.machine()}", f"Python {platform.python_version()}", f"{os.cpu_count()} cores"]
    try:
        parts.append("load avg " + "/".join(f"{x:.1f}" for x in os.getloadavg()))
    except (AttributeError, OSError):
        pass
    try:                                                    # Raspberry Pi: has the board been throttled (undervoltage / heat)?
        import shutil, subprocess
        if shutil.which("vcgencmd"):
            out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2).stdout.strip()
            parts.append(out.replace("throttled=", "throttled ") + " (0x0 = never throttled)")
    except Exception:
        pass
    return ", ".join(parts)


def cmd_bench(args) -> int:
    """Measure whether this computer can feed the DAC: the hub's real pipeline, colours forced to ZERO (nothing is emitted)."""
    try:
        import engine as E
    except ImportError as e:                                # e.g. a visuals.py from an older update
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE
    check_args(args)
    if args.scene not in V.SCENES:
        print(f"unknown scene {args.scene!r}; try `list`", file=sys.stderr)
        return EXIT_USAGE
    view, cal = view_from(args), calib_from(args)
    dac = open_dac(args)
    eng = E.LaserEngine(lambda: {}, view, cal, args.pps, args.frame_ms, P.PALETTES[args.palette], dac=dac, source="demo",
                        scene=args.scene, sync=args.sync if args.sync is not None else 0.5, sync_mode="hold",
                        max_intensity=0.0,                  # the engine clamps every colour to 0: the beam cannot light
                        auto_disarm_s=0, min_extent=args.min_extent, adaptive_poll=args.usb_polling == "adaptive",
                        poll_mode=args.usb_polling, frame_gap_s=args.frame_gap_ms / 1000.0,
                        usb_grace_s=3600.0)              # transient USB errors never latch here: a persistent failure is MEASURED, not cut short
    try:
        print(f"bench: scene {args.scene}, six simulated pods, {args.seconds:g} s measured, pps {args.pps}, frame {args.frame_ms:g} ms")
        print("       output is DARK: colours are forced to zero, the mirrors move but nothing is emitted.")
        print(f"       DAC polling: {args.usb_polling}")
        print(f"machine: {_machine_line()}")
        print(f"DAC    : {dac.name or '(unnamed)'}  ({dac.lib_path})")
        eng.start()
        time.sleep(0.5)
        ok, msg = eng.control({"arm": True})
        if not ok:
            print(f"error: {msg}", file=sys.stderr)
            return EXIT_DEVICE
        time.sleep(2.0)                                     # warm-up: caches, first USB transfers
        s0, c0, w0 = eng.state(), time.process_time(), time.monotonic()
        end = w0 + args.seconds
        failed = ""
        while time.monotonic() < end:
            time.sleep(0.25)
            st = eng.state()
            if st["mode"] == "error":
                failed = st["error"]                        # a FATAL error latched: stop here, but still report everything we know
                break
            if any(st["peak_rgb"]):
                print("\nABORT: a frame carried colour although the bench forces zero. Nothing was emitted by design, but stopping.", file=sys.stderr)
                return EXIT_UNSAFE
        s1, c1, w1 = eng.state(), time.process_time(), time.monotonic()
    finally:
        eng.shutdown()                                      # Stop + shutter closed + close
    wall = max(w1 - w0, 0.001)
    built, sent = s1["frames"] - s0["frames"], s1["sent"] - s0["sent"]
    late, dark = s1["late"] - s0["late"], s1["dark"] - s0["dark"]
    if built <= 0:                                          # never call that "keeps up": nothing was measured
        print()
        print(f"FAILED: the engine produced NO frames in {wall:.0f} s (built {built}, sent {sent}, refused {s1['refused']}).")
        print(f"        last problem: {s1['last_refusal'] or '(none recorded)'}")
        print("        This is not a speed result. Check that every file of the laser/ folder is from the same update.")
        return EXIT_DEVICE
    period = s1["frame_ms"]
    usb = s1.get("usb") or {}
    expected = wall * 1000.0 / period if period > 0 else 0.0
    usb_total, usb_warm = s1["usb_errors"], s0["usb_errors"]
    usb_win = usb_total - usb_warm
    print()
    print(f"frame period          {period:6.1f} ms   ({s1['points']} points)")
    print(f"building one frame    {s1['build_ms']:6.1f} ms avg   {s1['build_max_ms']:5.1f} ms worst   -> load {100 * s1['load']:.0f} % of the period"
          f"{'   <-- cannot keep up' if s1['load'] >= 1.0 else ''}")
    print(f"frames built / s      {built / wall:6.1f}")
    print(f"frames sent / s       {sent / wall:6.1f}    re-sent because none was ready: {late} ({100.0 * late / max(1, sent):.1f} %)    dark events: {dark}")
    print(f"delivered to the DAC  {100.0 * sent / max(1.0, expected):6.0f} %     of the ~{expected:.0f} frames it should have played in this window")
    if usb:
        print(f"USB (per frame)       waiting for ready {usb['ready_ms']:5.1f} ms avg ({usb['polls']:.0f} polls; one poll "
              f"{usb.get('poll_ms', 0):.1f} ms avg / {usb.get('poll_max_ms', 0):.1f} worst), "
              f"WriteFrame {usb['write_ms']:4.1f} ms avg / {usb['write_max_ms']:.1f} ms worst")
    if usb or usb_total:
        print(f"USB errors survived   {usb_win:6d}      = {60.0 * usb_win / wall:.1f} per minute"
              + (f"      last: {s1['last_usb_error']}" if usb_total else "")
              + (f"      (plus {usb_warm} during the 2 s warm-up)" if usb_warm else ""))
        if s1["usb_first_error_s"] is not None:
            print(f"first USB error       {s1['usb_first_error_s']:6.1f} s after arming")
        if usb_total:
            print(f"error runs            {s1['usb_runs']} ended; longest {s1['usb_longest_run_ms']} ms  (a run = errors in a row until a call succeeded)")
        for kind, n in sorted(s1["usb_kinds"].items(), key=lambda kv: -kv[1]):
            print(f"      {n:5d} x {kind}")
        for ev in (eng.usb_events_all(args.events) if usb_total else []):
            print(f"      {ev}")
    print(f"this process' CPU     {100.0 * (c1 - c0) / wall:6.0f} %  of one core (all threads together)")
    print()
    delivered = sent / max(1.0, expected)
    if failed:
        print(f"STOPPED EARLY: a fatal DAC error latched after {time.monotonic() - w0:.1f} s of the window: {failed}")
    if sent <= 0:
        print(f"FAILED: frames were built but NONE reached the DAC in {wall:.0f} s (built {built}, sent {sent}).")
        print("        This is not a speed result: the USB path to the DAC is not working at all.")
        return EXIT_DEVICE
    if failed or delivered < 0.8 or s1["usb_longest_run_ms"] > 500:
        print(f"VERDICT: the USB path is UNRELIABLE -- {100 * delivered:.0f} % of the frames got through, longest run of errors {s1['usb_longest_run_ms']} ms. "
              "This is not a speed result.")
        return EXIT_DEVICE
    repeat_pct = 100.0 * late / max(1, sent)
    if usb_win:
        print(f"NOTE: {usb_win} transient USB error(s) occurred and were ridden out ({s1['last_usb_error']}). "
              "A few are normal under load; many mean the USB path or the CPU is struggling.")
    if dark == 0 and repeat_pct < 2.0:
        print("VERDICT: keeps up. Frames reach the DAC on time; the laser would stay lit with a smooth animation.")
    else:
        why = []
        if s1["load"] >= 0.7:
            why.append(f"the computer needs {s1['build_ms']:.0f} ms to build a {period:.0f} ms frame (load {100 * s1['load']:.0f} %) -- CPU-bound")
        if usb and usb["write_max_ms"] > 0.6 * period:
            why.append(f"a USB write took up to {usb['write_max_ms']:.0f} ms of a {period:.0f} ms frame -- the USB path is slow")
        if s1["build_max_ms"] > 0.9 * period and s1["load"] < 0.7:
            why.append(f"most frames build quickly, but the slowest took {s1['build_max_ms']:.0f} ms (a frame lasts {period:.0f} ms): "
                       "other programs, the browser or throttling steal CPU time in bursts")
        if not why:
            why.append("building is fast and USB is quick, so something else is delaying the threads (other programs, the browser, throttling)")
        print(f"VERDICT: does NOT keep up -- {100.0 * late / max(1, sent):.0f} % of the slots re-sent the previous frame"
              f"{f', and output went dark {dark} time(s)' if dark else ''}.")
        for w in why:
            print(f"  - {w}")
        print("  Try: run the hub with --perf to see WHERE the CPU goes; start the browser with `nice -n 10`; set the CPU governor to")
        print("       'performance' and check `vcgencmd get_throttled` is 0x0 (heat / weak supply); --viz-fps 15. (--pps 15000 saves only")
        print("       ~15-25 %; going lower does not help: the frame cannot shrink below its geometry and starts to look dotted.)")
    return 0


# ---- diag: everything needed to diagnose USB trouble, in ONE small file ------------------------
def _run_text(cmd, timeout: float = 6.0, limit: int = 40) -> str:
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return f"(not available: {cmd[0]})"
    except Exception as e:
        return f"(failed: {type(e).__name__}: {e})"
    lines = (r.stdout + ("\n" + r.stderr if r.stderr.strip() and not r.stdout.strip() else "")).strip().splitlines()
    return "\n".join(lines[:limit] + ([f"... ({len(lines) - limit} more lines)"] if len(lines) > limit else []))


def _read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read().replace(b"\x00", b"").decode("utf-8", "replace").strip()
    except OSError:
        return ""


def _decode_throttled(text: str) -> str:
    import re
    m = re.search(r"0x([0-9a-fA-F]+)", text)
    if not m:
        return text
    v = int(m.group(1), 16)
    names = {0: "UNDER-VOLTAGE now", 1: "frequency capped now", 2: "throttled now", 3: "soft temperature limit now",
             16: "under-voltage HAS occurred", 17: "frequency cap HAS occurred", 18: "throttling HAS occurred", 19: "soft temp limit HAS occurred"}
    hits = [n for bit, n in names.items() if v & (1 << bit)]
    return f"0x{v:x}  " + ("(clean)" if not hits else "; ".join(hits))


def _diag_system(usb_root: str = "/sys/bus/usb/devices") -> List[str]:
    import glob
    import platform
    import socket
    L: List[str] = []
    def add(label: str, value: str) -> None:
        L.append(f"{label:<24}{value}")
    add("date", time.strftime("%Y-%m-%d %H:%M:%S %z"))
    add("host", socket.gethostname())
    add("platform", platform.platform())
    model = _read("/proc/device-tree/model")
    if model:
        add("board", model)
    for ln in _read("/etc/os-release").splitlines():
        if ln.startswith("PRETTY_NAME="):
            add("os", ln.split("=", 1)[1].strip('"'))
    add("python", platform.python_version())
    gov, cur, mx = (_read(f"/sys/devices/system/cpu/cpu0/cpufreq/{n}") for n in ("scaling_governor", "scaling_cur_freq", "cpuinfo_max_freq"))
    if gov:
        add("cpu governor / clock", f"{gov}, now {int(cur or 0) // 1000} MHz, max {int(mx or 0) // 1000} MHz")
    vc = _run_text(["vcgencmd", "get_throttled"], limit=2)
    if not vc.startswith("(not available"):
        add("throttled (vcgencmd)", _decode_throttled(vc))
        add("temperature", _run_text(["vcgencmd", "measure_temp"], limit=1))
        add("core voltage", _run_text(["vcgencmd", "measure_volts", "core"], limit=1))
    add("usb autosuspend", _read("/sys/module/usbcore/parameters/autosuspend") or "(n/a)")
    for dev in sorted(glob.glob(usb_root + "/*")):
        if _read(f"{dev}/idVendor") == "1209" and _read(f"{dev}/idProduct") == "e500":
            add("Helios USB device", os.path.basename(dev))
            for key in ("speed", "bMaxPower", "version", "power/control", "power/autosuspend_delay_ms", "power/runtime_status"):
                add(f"   {key}", _read(f"{dev}/{key}") or "(n/a)")
            parent = os.path.dirname(os.path.realpath(dev))
            proto = {"00": "full-speed hub", "01": "high-speed hub, ONE transaction translator shared by all its full/low-speed devices",
                     "02": "high-speed hub, one translator PER PORT"}.get(_read(f"{parent}/bDeviceProtocol"), "")
            add("parent (hub) of the DAC", f"{os.path.basename(parent)}: {_read(f'{parent}/product') or '?'} [{_read(f'{parent}/idVendor')}:{_read(f'{parent}/idProduct')}], "
                f"{_read(f'{parent}/speed')} Mbit/s, {_read(f'{parent}/maxchild') or '?'} ports" + (f"; {proto}" if _read(f"{parent}/bDeviceClass") == "09" and proto else ""))
            import re as _re
            for sub in sorted(glob.glob(parent + "/*")):
                nm = os.path.basename(sub)
                if _re.fullmatch(r"\d+-[\d.]+", nm) and os.path.realpath(sub) != os.path.realpath(dev):
                    add(f"   shares that hub: {nm}", f"{_read(f'{sub}/product') or '?'}, {_read(f'{sub}/speed')} Mbit/s")
    add("libusb package", _run_text(["dpkg-query", "-W", "libusb-1.0-0"], limit=1))
    L.append("")
    L.append("-- lsusb -t (the USB tree; the speed of the DAC's link is at the end of its line: 12M = full speed) --")
    tree = _run_text(["lsusb", "-t"], limit=30)
    L.append("(this lsusb has no -t; on the Pi it prints the tree)" if tree.lstrip().startswith("Usage") else tree)
    L.append("-- lsusb -d 1209:e500 (the Helios) --")
    L.append(_run_text(["lsusb", "-d", "1209:e500"], limit=3))
    if platform.system() == "Darwin":                       # macOS: the Helios block of the system profiler (speed, power)
        prof = _run_text(["system_profiler", "SPUSBDataType"], timeout=20, limit=4000).splitlines()
        hit = next((i for i, l in enumerate(prof) if "0x1209" in l), None)
        L.append("-- system_profiler, the Helios entry --")
        L.append("\n".join(prof[max(0, hit - 10):hit + 10]) if hit is not None else "(Helios not found in the profile)")
    return L


def _diag_sdk(args) -> List[str]:
    """Which Helios SDK build is this, and which libusb does it use? (The Mac's prebuilt dylib and a library built on the Pi are different builds.)"""
    import hashlib
    import platform
    L: List[str] = []
    try:
        path = HD.find_library(args.lib)
    except Exception as e:
        return [f"{'Helios library':<24}(not found: {e})"]
    try:
        data = open(path, "rb").read()
        st = os.stat(path)
        marks = {m.decode(): (m in data) for m in (b"Found new USB Helios DAC", b"Unknown Helios", b"Closing Helios USB DAC")}
        L.append(f"{'Helios library':<24}{path}")
        L.append(f"{'   file':<24}{st.st_size} bytes, modified {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}, sha256 {hashlib.sha256(data).hexdigest()[:12]}")
        L.append(f"{'   prints (SDK text)':<24}" + ", ".join(f"'{k}': {'yes' if v else 'no'}" for k, v in marks.items()))
        link = _run_text(["otool", "-L", path] if platform.system() == "Darwin" else ["ldd", path], limit=30)
        L.append(f"{'   links usb':<24}" + ("; ".join(l.strip() for l in link.splitlines() if "usb" in l.lower()) or "(no libusb line: statically linked or n/a)"))
    except OSError as e:
        L.append(f"{'Helios library':<24}{path} (unreadable: {e})")
    return L


def _kernel_usb_lines() -> str:
    import re
    want = re.compile(r"usb|xhci|dwc|helios|reset|disconnect|under-?volt", re.I)
    for cmd in (["dmesg", "--ctime"], ["journalctl", "-k", "-n", "600", "--no-pager"]):
        out = _run_text(cmd, limit=2000)
        if out and not out.startswith("(") and "Operation not permitted" not in out and "read kernel buffer failed" not in out:
            hits = [l for l in out.splitlines() if want.search(l)]
            return "\n".join(hits[-30:]) if hits else "(no usb/xhci/under-voltage lines in the kernel log)"
    return "(kernel log not readable as this user: run   sudo dmesg --ctime | grep -i -E 'usb|xhci|volt' | tail -30   and attach that too)"


def _group_libusb(stderr: str, limit: int = 12, only_informative: bool = False):
    """libusb's own log lines grouped: the same message 900 times becomes one line with a count. At debug level (3/4) libusb logs every
    transfer; then only lines about statuses/errors/stalls/timeouts/removal are kept (that is where -71, -32, -110 ... show up)."""
    import collections
    import re
    keep = re.compile(r"status|error|stall|overflow|remov|timeout|timed out|cancel|disconnect|discard|fail|warn|bus error|babble|crc", re.I)
    counts: "collections.Counter[str]" = collections.Counter()
    other: List[str] = []
    for ln in stderr.splitlines()[:400000]:
        if "libusb:" in ln:
            if only_informative and not keep.search(ln):
                continue
            msg = re.sub(r"^\s*\[\s*[\d.]+\]\s*\[[0-9a-fA-F]+\]\s*", "", ln)
            msg = re.sub(r"0x[0-9a-fA-F]+", "0x..", msg)
            msg = re.sub(r"\b\d{3,}\b", "N", msg)
            counts[msg.strip()] += 1
        elif ln.strip():
            other.append(ln.rstrip())
    return counts.most_common(limit), sum(counts.values()), other[:15]


def _diag_sub(args, sub_args: List[str], timeout: float):
    import subprocess
    cmd = [sys.executable, os.path.abspath(__file__)] + sub_args + (["--dry-run"] if args.dry_run else []) + (["--lib", args.lib] if args.lib else []) + ["--device", str(args.device)]
    env = dict(os.environ)
    if args.libusb_debug:
        env["LIBUSB_DEBUG"] = str(args.libusb_debug)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", "did not finish in time", -1


def _diag_settle(args) -> float:
    """After a stage that may have hung the DAC: wait (up to 30 s) until it answers again, and report how long that took. Without this the next stage
    starts on a DAC that is still silent (what happened in the first Pi 4 report) and is blamed for it."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < 30:
        _o, _e, rc = _diag_sub(args, ["usbtest", "--seconds", "0.6", "--interval-ms", "100"], 40)
        if rc == 0:
            break
        time.sleep(1.0)
    return time.monotonic() - t0


def _diag_bench(args, mode: str, seconds: float):
    import subprocess
    cmd = [sys.executable, os.path.abspath(__file__), "bench", args.scene, "--seconds", str(seconds), "--usb-polling", mode,
           "--events", "8", "--pps", str(args.pps), "--frame-ms", str(args.frame_ms), "--size", str(args.size), "--device", str(args.device)]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.lib:
        cmd += ["--lib", args.lib]
    env = dict(os.environ)
    if args.libusb_debug:
        env["LIBUSB_DEBUG"] = str(args.libusb_debug)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 90, env=env)
        return r.stdout, r.stderr, r.returncode
    except subprocess.TimeoutExpired:
        return "", "the bench did not finish in time", -1


def _bench_facts(out: str) -> dict:
    import re
    g = lambda pat, cast=float: (cast(m.group(1)) if (m := re.search(pat, out)) else None)
    verdict = re.search(r"^(VERDICT.*|FAILED.*)$", out, re.M)
    pm = re.search(r"waiting for ready\s+([\d.]+) ms avg \((\d+) polls; one poll ([\d.]+) ms avg / ([\d.]+) worst\), WriteFrame\s+([\d.]+) ms avg / ([\d.]+) ms worst", out)
    return {"polls": int(pm.group(2)) if pm else None, "poll_avg": float(pm.group(3)) if pm else None, "poll_max": float(pm.group(4)) if pm else None,
            "write_max": float(pm.group(6)) if pm else None, "errors": g(r"USB errors survived\s+(\d+)", int), "per_min": g(r"= ([\d.]+) per minute"), "load": g(r"-> load (\d+) %", int),
            "sent": g(r"frames sent / s\s+([\d.]+)"), "resent": g(r"\(([\d.]+) %\)\s+dark"),
            "verdict": (verdict.group(1).split(". ")[0][:60] if verdict else "(no verdict: see the bench text)")}


def _hub_pids() -> List[int]:
    """PIDs of running `python ... osc_web_dashboard.py` processes (the hub). NOT a substring search: an editor, tail, grep or shell
    that merely mentions the file name must not count."""
    import subprocess
    try:
        out = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return []
    mine = {os.getpid(), os.getppid()}
    pids = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3 or not parts[0].isdigit() or int(parts[0]) in mine:
            continue
        exe = os.path.basename(parts[1]).lower()
        if exe.startswith("python") and any(os.path.basename(tok) == "osc_web_dashboard.py" for tok in parts[2:]):
            pids.append(int(parts[0]))
    return pids


def cmd_diag(args) -> int:
    """One command, one small file: system facts + the dark bench in both polling modes (interleaved) with libusb's log -> diag_*.txt"""
    import collections
    import platform
    import socket
    check_args(args)
    if args.scene not in V.SCENES:
        print(f"unknown scene {args.scene!r}; try `list`", file=sys.stderr)
        return EXIT_USAGE
    if not args.dry_run and not args.force:
        pids = _hub_pids()
        if pids:
            print("The hub (osc_web_dashboard.py) is running and holds the DAC; only one program can. Stop it first "
                  f"(Ctrl-C, or `kill {' '.join(str(p) for p in pids)}`), or use --force.", file=sys.stderr)
            return EXIT_USAGE
    rounds = max(1, args.rounds)
    seg = max(5.0, args.seconds / rounds)
    print(f"diag: collecting system facts, then dark bench runs: {rounds} x ({args.bench_modes}) of {seg:g} s each, interleaved "
          f"so that drift in the machine does not favour one mode. Nothing is emitted.")
    system = _diag_system() + [""] + _diag_sdk(args)
    import re as _re
    probes, idles = [], []
    for i in range(max(0, args.probes)):
        out, err, rc = _diag_sub(args, ["probe"], 40)
        m = _re.search(r"DAC\s*:\s*(.*?)\s+firmware\s+(\S+)", out)
        probes.append({"rc": rc, "name": m.group(1) if m else "(no name)", "fw": m.group(2) if m else "?", "err": (err.strip().splitlines() or [""])[-1][:110]})
        print(f"  probe {i + 1}/{args.probes} (reads the DAC's name and firmware: control path only) ... "
              + (f"ok  [{probes[-1]['name']}]" if rc == 0 else f"FAILED  {probes[-1]['err']}"))
        time.sleep(1.0)
    if args.idle_seconds > 0:
        for ms in (3.0, 30.0):
            print(f"  idle polling every {ms:g} ms for {args.idle_seconds:g} s, NO frames ... ", end="", flush=True)
            out, err, rc = _diag_sub(args, ["usbtest", "--seconds", str(args.idle_seconds), "--interval-ms", str(ms)], args.idle_seconds + 60)
            m = _re.search(r"(\d+) polls, (\d+) errors .*?one poll ([\d.]+) ms avg / ([\d.]+) worst; first error (.*?); longest run (\d+) polls / (\d+) ms", out)
            groups, n_lines, other = _group_libusb(err, limit=40, only_informative=args.libusb_debug >= 3)
            idles.append({"ms": ms, "out": out, "rc": rc, "m": m, "groups": groups, "n": n_lines, "err": err.strip()[-200:]})
            print((f"{m.group(2)} errors in {m.group(1)} polls (worst poll {m.group(4)} ms)" if m else f"no result ({idles[-1]['err'] or 'rc ' + str(rc)})"))
    streams, settles = [], []
    dead = [False]                                              # the DAC stopped answering for good: do not wait for it before every stage

    def settle(label: str) -> None:
        if dead[0]:
            settles.append((label + " (not waited for: the DAC never came back)", 0.0))
            return
        w = _diag_settle(args)
        settles.append((label, w))
        if w >= 29.5:
            dead[0] = True

    if args.stream_seconds > 0:
        for mode in ("poll", "timed"):
            settle(f"before the {mode} stream test")
            print(f"  streaming DARK frames without the engine, mode {mode} ({'asks ready? before each frame' if mode == 'poll' else 'NO status polling'}), "
                  f"up to {args.stream_seconds:g} s ... ", end="", flush=True)
            out, err, rc = _diag_sub(args, ["streamtest", "--mode", mode, "--seconds", str(args.stream_seconds), "--cycles", "2", "--give-up-s", "30"], args.stream_seconds + 150)
            res = next((l for l in out.splitlines() if l.startswith("RESULT")), "RESULT: (no result: " + (err.strip()[-100:] or f"exit {rc}") + ")")
            streams.append({"mode": mode, "out": out, "rc": rc, "res": res})
            print(res.replace("RESULT ", ""))
    modes = [m.strip() for m in args.bench_modes.split(",") if m.strip() in ("adaptive", "fast", "timed")] or ["adaptive", "timed"]
    runs: "dict[str, list]" = {m: [] for m in modes}
    for r in range(rounds):
        for mode in modes:
            settle(f"before round {r + 1} {mode}")
            print(f"  round {r + 1}/{rounds}, DAC polling {mode} ... ", end="", flush=True)
            out, err, rc = _diag_bench(args, mode, seg)
            facts = _bench_facts(out)
            groups, n_lines, other = _group_libusb(err, limit=40, only_informative=args.libusb_debug >= 3)
            runs[mode].append({"out": out, "rc": rc, "facts": facts, "groups": groups, "n": n_lines, "other": other})
            print(f"{facts['verdict']}  (USB errors: {facts['errors'] if facts['errors'] is not None else '-'})")
    num = lambda v, f="{:.1f}": (f.format(v) if v is not None else "-")
    agg = {}
    for mode, rs in runs.items():
        fs = [r["facts"] for r in rs]
        errs = sum(f["errors"] or 0 for f in fs)
        mean = lambda k: (sum(f[k] for f in fs if f[k] is not None) / max(1, sum(1 for f in fs if f[k] is not None))) if any(f[k] is not None for f in fs) else None
        worst = lambda k: max((f[k] for f in fs if f[k] is not None), default=None)
        agg[mode] = {"errors": errs, "per_min": 60.0 * errs / (seg * len(rs)), "polls": mean("polls"), "poll_avg": mean("poll_avg"), "poll_max": worst("poll_max"),
                     "write_max": worst("write_max"), "load": mean("load"), "sent": mean("sent"), "ok": sum(1 for r in rs if "keeps up" in r["facts"]["verdict"])}
    summary = ["SUMMARY (paste just this block if you prefer; the full file has the details)",
               f"  {rounds} x {seg:g} s per mode, interleaved.     poll = one GetStatus call; ms",
               f"  {'polling':9} {'USB errors':>10} {'per minute':>11} {'polls/frame':>12} {'poll avg/worst':>15} {'write worst':>12} {'load':>6} {'frames/s':>9}  keeps up"]
    for mode, a in agg.items():
        summary.append(f"  {mode:9} {a['errors']:>10} {a['per_min']:>11.1f} {num(a['polls'], '{:.1f}'):>12} {num(a['poll_avg']) + '/' + num(a['poll_max']):>15} "
                       f"{num(a['write_max']):>12} {num(a['load'], '{:.0f}') + ' %':>6} {num(a['sent']):>9}  {a['ok']}/{rounds}")
    for s in streams:
        summary.append("  " + s["res"].replace("RESULT ", "stream test, ", 1))
    slow = [(w, s) for w, s in settles if s > 3.0]
    if dead[0]:
        summary.append("  !! the DAC stopped answering and did NOT come back within 30 s: it needs a replug (later stages ran on a dead DAC)")
    if settles:
        summary.append("  time the DAC needed to answer again before each stage: " + (", ".join(f"{w} {s:.0f} s" for w, s in slow) if slow else "all under 3 s"))
    if probes:
        names = collections.Counter(p["name"] for p in probes)
        summary.append(f"  probes x{len(probes)} (name + firmware, control path only): {sum(1 for p in probes if p['rc'] == 0)} ok, "
                       f"{sum(1 for p in probes if p['rc'] != 0)} failed;  names read: " + ", ".join(f"'{n}' x{c}" for n, c in names.items()))
    for it in idles:
        m = it["m"]
        summary.append(f"  idle polling, no frames, every {it['ms']:g} ms: " + (f"{m.group(2)} errors in {m.group(1)} polls, worst poll {m.group(4)} ms, longest run {m.group(7)} ms"
                                                                            if m else "no result"))
    kinds: "collections.Counter[str]" = collections.Counter()
    for mode, rs in runs.items():
        for r in rs:
            for ln in r["out"].splitlines():
                s = ln.strip()
                if " x " in s and s[:1].isdigit():
                    n, _, kind = s.partition(" x ")
                    kinds[f"[{mode}] {kind}"] += int(n)
    if kinds:
        summary.append("  error kinds:")
        summary += [f"    {n:4d} x {k}" for k, n in kinds.most_common(10)]
    summary += ["  " + ln for ln in system if ln.startswith(("throttled", "Helios USB", "   speed", "board", "parent (hub)", "   shares that hub", "Helios library", "   prints"))]
    body: List[str] = ["cosmic dune -- USB diagnostics", "=" * 60, ""] + summary + ["", "=" * 60, "SYSTEM", "=" * 60] + system
    if streams:
        body += ["", "=" * 60, "STREAMING dark fixed-size frames, no engine (silence and recovery)", "=" * 60]
        for s in streams:
            body += [f"-- mode {s['mode']} --", s["out"].strip()[:2500]]
        body += ["-- settle waits (how long the DAC needed to answer again) --"] + [f"  {w}: {sw:.1f} s" for w, sw in settles]
    if probes or idles:
        body += ["", "=" * 60, "PROBES and IDLE POLLING (no frames)", "=" * 60]
        for i, p in enumerate(probes, 1):
            body.append(f"probe {i}: exit {p['rc']}, name '{p['name']}', firmware {p['fw']}" + (f"  {p['err']}" if p["rc"] else ""))
        for it in idles:
            body += [f"-- idle polling every {it['ms']:g} ms --", it["out"].strip()[:1800]]
            body += [f"   libusb log: {it['n']} line(s)"] + [f"     {c:5d} x {m[:150]}" for m, c in it["groups"][:8]]
    for mode, rs in runs.items():
        merged: "collections.Counter[str]" = collections.Counter()
        for r in rs:
            for m, c in r["groups"]:
                merged[m] += c
        body += ["", "=" * 60, f"BENCH, DAC polling {mode}: {len(rs)} run(s); the first run in full, then only the lines that differ", "=" * 60]
        body += [rs[0]["out"].strip()[:3200]]
        for i, r in enumerate(rs[1:], 2):
            keep = [ln for ln in r["out"].splitlines() if any(k in ln for k in ("USB errors survived", "USB (per frame)", "building one frame", "frames sent", "VERDICT", "FAILED", " x ", "USB error", "USB recovered"))]
            body += [f"-- run {i} --"] + keep[:20]
        body += [f"-- libusb's own log over all {mode} runs (LIBUSB_DEBUG={args.libusb_debug}): {sum(merged.values())} line(s), grouped --"]
        body += [f"  {c:5d} x {m[:170]}" for m, c in merged.most_common(12)] or ["  (none)"]
        other = [o for r in rs for o in r["other"]][:15]
        if other:
            body += ["-- other stderr --"] + ["  " + o[:170] for o in other]
    body += ["", "=" * 60, "KERNEL LOG (usb / xhci / under-voltage lines)", "=" * 60, _kernel_usb_lines()]
    text = "\n".join(body)
    if len(text) > 24000:
        text = text[:24000] + "\n... (cut at 24 KB)"
    path = args.out or f"diag_{socket.gethostname()}_{time.strftime('%Y%m%d-%H%M%S')}.txt"
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text + "\n")
    print()
    print("\n".join(summary))
    print()
    host = socket.gethostname()
    print(f"wrote {path} ({len(text) / 1024:.1f} KB).")
    if platform.system() == "Linux":
        print(f"Copy it to your Mac, run on the Mac:   scp {os.environ.get('USER', 'pi')}@{host if '.' in host else host + '.local'}:{os.path.abspath(path)} .")
    print("then ATTACH the file in the chat (better than pasting).")
    return 0


# ---- main -------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list the patterns")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("check", help="render + safety-check every pattern (no hardware)")
    add_view_args(p)
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("preview", help="write a PNG of what a pattern will draw")
    p.add_argument("pattern", nargs="?", default="all", help="a pattern, a scene, `scenes`, or `all`")
    p.add_argument("--out", default="laser_preview.png")
    p.add_argument("--t", type=float, default=2.0, help="time in seconds, for animated patterns/scenes (scenes: try 6)")
    p.add_argument("--palette", choices=sorted(P.PALETTES), default="gsr", help="colour palette for scenes")
    p.add_argument("--fit", action="store_true", help="scale up to fill the panel (default: true size)")
    add_view_args(p)
    p.set_defaults(fn=cmd_preview)

    for nm, fn, helptext in (("probe", cmd_probe, "find the DAC (emits no light)"),
                             ("blank", cmd_blank, "make sure the laser output is off")):
        p = sub.add_parser(nm, help=helptext)
        p.add_argument("--lib")
        p.add_argument("--device", type=int, default=0)
        p.add_argument("--dry-run", action="store_true")
        p.set_defaults(fn=fn)

    p = sub.add_parser("run", help="project one pattern")
    p.add_argument("pattern")
    add_view_args(p)
    add_run_args(p)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("show", help="the pod visuals: six pods -> one pulse")
    p.add_argument("scene", nargs="?", default="converge", help="converge, merge, bloom, ripple or spokes (n/p switch live)")
    p.add_argument("--osc-port", type=int, default=0, help="listen for the real pods on this UDP port (needs python-osc)")
    p.add_argument("--dashboard", nargs="?", const="http://127.0.0.1:5000", default=None, metavar="URL",
                   help="read the pods from a running osc_web_dashboard.py instead (default http://127.0.0.1:5000); "
                        "lets the dashboard and this tool run side by side")
    p.add_argument("--fill-demo", action="store_true",
                   help="simulated pods fill the empty slots, so ONE real pod (net pod N) can be tried among six")
    p.add_argument("--demo-speed", type=float, default=1.0, help="demo pods: speed multiplier (default 1)")
    p.add_argument("--tol-bpm", type=float, default=10.0,
                   help="mean BPM deviation at which measured sync reaches 0 (default 10)")
    p.add_argument("--palette", choices=sorted(P.PALETTES), default="gsr",
                   help="gsr = the LED strip palette (white-blue-purple-red); spectrum = blue-cyan-green-yellow-red")
    add_view_args(p)
    add_run_args(p)
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("usbtest", help="poll the DAC with NO frames at all (output dark): does the USB path alone fail?")
    p.add_argument("--seconds", type=float, default=10.0, help="how long to poll (default 10)")
    p.add_argument("--interval-ms", type=float, default=3.0, help="pause between polls (default 3 ms, about the streaming rate)")
    p.add_argument("--dry-run", action="store_true", help="no DAC at all")
    p.add_argument("--lib", help="path to the Helios library (or set $HELIOS_LIB)")
    p.add_argument("--device", type=int, default=0, help="DAC number if several are connected")
    p.set_defaults(fn=cmd_usbtest)

    p = sub.add_parser("streamtest", help="stream DARK frames without the engine until the DAC stops answering; measure how long it stays silent")
    p.add_argument("--mode", choices=("poll", "timed"), default="poll",
                   help="poll = ask 'ready?' before each frame (like the engine); timed = fixed timing, never poll the status")
    p.add_argument("--poll-at", type=float, default=None, metavar="FRACTION",
                   help="with --mode timed: make ONE status poll per frame at this fraction of the frame (0.5 = mid-frame, 1.0 = at its end), "
                        "to see whether the hazard depends on when in the frame the poll happens")
    p.add_argument("--seconds", type=float, default=60.0, help="total time budget (default 60)")
    p.add_argument("--cycles", type=int, default=3, help="stop after this many failures (default 3)")
    p.add_argument("--points", type=int, default=570, help="points per frame (default 570, ~28.5 ms at 20000 pps)")
    p.add_argument("--pps", type=int, default=20000)
    p.add_argument("--quiet-ms", type=float, default=500.0, help="after a failure, leave the DAC completely alone this long (default 500)")
    p.add_argument("--watch-ms", type=float, default=250.0, help="then ask 'ready?' this often until it answers (default 250)")
    p.add_argument("--give-up-s", type=float, default=60.0, help="if it has not answered after this long: needs a replug (default 60)")
    p.add_argument("--dry-run", action="store_true", help="no DAC at all")
    p.add_argument("--lib", help="path to the Helios library (or set $HELIOS_LIB)")
    p.add_argument("--device", type=int, default=0, help="DAC number if several are connected")
    p.set_defaults(fn=cmd_streamtest)

    p = sub.add_parser("bench", help="can this computer feed the DAC? (output stays DARK; run it on the Mac and on the Pi)")
    p.add_argument("scene", nargs="?", default="converge", help="the scene to build (default converge, the heaviest)")
    p.add_argument("--seconds", type=float, default=15.0, help="measuring time after a 2 s warm-up (default 15)")
    p.add_argument("--palette", choices=sorted(P.PALETTES), default="gsr")
    p.add_argument("--dry-run", action="store_true", help="no DAC at all: measures only the computer")
    p.add_argument("--usb-polling", choices=("adaptive", "fast", "timed"), default="adaptive",
                   help="adaptive (default) / fast: ask 'ready?' before each frame; timed: NEVER poll the status, write on a fixed schedule")
    p.add_argument("--frame-gap-ms", type=float, default=1.5, help="timed mode: idle gap between frames (default 1.5 ms)")
    p.add_argument("--events", type=int, default=6, help="how many USB error lines to print (first and last; default 6)")
    p.add_argument("--lib", help="path to the Helios library (or set $HELIOS_LIB)")
    p.add_argument("--device", type=int, default=0, help="DAC number if several are connected")
    add_view_args(p)
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("diag", help="collect USB diagnostics into one small file to attach to the chat (output stays DARK)")
    p.add_argument("scene", nargs="?", default="converge", help="scene used for the load (default converge)")
    p.add_argument("--seconds", type=float, default=60.0, help="measuring time PER POLLING MODE in total, split over the rounds (default 60)")
    p.add_argument("--idle-seconds", type=float, default=10.0, help="length of each idle-polling test (no frames), run at 3 ms and 30 ms (default 10; 0 = skip)")
    p.add_argument("--stream-seconds", type=float, default=40.0, help="budget of each of the two streaming tests without the engine (poll, timed; default 40; 0 = skip)")
    p.add_argument("--probes", type=int, default=3, help="how many times to read the DAC's name and firmware first (default 3; 0 = skip)")
    p.add_argument("--bench-modes", default="adaptive,timed",
                   help="which pacing modes the engine benches compare, interleaved (default adaptive,timed; any of adaptive, fast, timed)")
    p.add_argument("--rounds", type=int, default=2, help="how many times each mode is run, interleaved adaptive, fast, adaptive, fast ... (default 2)")
    p.add_argument("--out", help="the report file (default diag_<host>_<time>.txt in the current folder)")
    p.add_argument("--libusb-debug", type=int, default=2, choices=(0, 1, 2, 3, 4),
                   help="libusb's own log level during the runs: 2 = warnings and errors (default), 3 = more, 0 = off")
    p.add_argument("--force", action="store_true", help="run even if the hub appears to be running")
    p.add_argument("--dry-run", action="store_true", help="no DAC at all (tries the tool itself, e.g. on the Mac)")
    p.add_argument("--lib", help="path to the Helios library (or set $HELIOS_LIB)")
    p.add_argument("--device", type=int, default=0, help="DAC number if several are connected")
    add_view_args(p)
    p.set_defaults(fn=cmd_diag)

    p = sub.add_parser("cycle", help="run through the patterns")
    p.add_argument("--each", type=float, default=8.0, help="seconds per pattern (default 8)")
    p.add_argument("--only", help="comma-separated subset, e.g. circle,square,star")
    add_view_args(p)
    add_run_args(p)
    p.set_defaults(fn=cmd_cycle, seconds=0.0)
    return ap


def _terminate(signum, frame):
    raise KeyboardInterrupt


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    for sig in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), _terminate)     # closing the terminal also darkens the laser
    try:
        return args.fn(args)
    except HeliosError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_DEVICE
    except P.UnsafeFrame as e:
        print(f"refused: {e}", file=sys.stderr)
        return EXIT_UNSAFE


if __name__ == "__main__":
    sys.exit(main())
