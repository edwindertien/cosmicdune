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
    import engine as E
    check_args(args)
    if args.scene not in V.SCENES:
        print(f"unknown scene {args.scene!r}; try `list`", file=sys.stderr)
        return EXIT_USAGE
    view, cal = view_from(args), calib_from(args)
    dac = open_dac(args)
    eng = E.LaserEngine(lambda: {}, view, cal, args.pps, args.frame_ms, P.PALETTES[args.palette], dac=dac, source="demo",
                        scene=args.scene, sync=args.sync if args.sync is not None else 0.5, sync_mode="hold",
                        max_intensity=0.0,                  # the engine clamps every colour to 0: the beam cannot light
                        auto_disarm_s=0, min_extent=args.min_extent)
    try:
        print(f"bench: scene {args.scene}, six simulated pods, {args.seconds:g} s measured, pps {args.pps}, frame {args.frame_ms:g} ms")
        print("       output is DARK: colours are forced to zero, the mirrors move but nothing is emitted.")
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
        while time.monotonic() < end:
            time.sleep(0.25)
            st = eng.state()
            if st["mode"] == "error":
                print(f"\nDAC error during the run: {st['error']}", file=sys.stderr)
                return EXIT_DEVICE
            if any(st["peak_rgb"]):
                print("\nABORT: a frame carried colour although the bench forces zero. Nothing was emitted by design, but stopping.", file=sys.stderr)
                return EXIT_UNSAFE
        s1, c1, w1 = eng.state(), time.process_time(), time.monotonic()
    finally:
        eng.shutdown()                                      # Stop + shutter closed + close
    wall = w1 - w0
    built, sent = s1["frames"] - s0["frames"], s1["sent"] - s0["sent"]
    late, dark = s1["late"] - s0["late"], s1["dark"] - s0["dark"]
    period = s1["frame_ms"]
    usb = s1.get("usb") or {}
    print()
    print(f"frame period          {period:6.1f} ms   ({s1['points']} points)")
    print(f"building one frame    {s1['build_ms']:6.1f} ms avg   {s1['build_max_ms']:5.1f} ms worst   -> load {100 * s1['load']:.0f} % of the period"
          f"{'   <-- cannot keep up' if s1['load'] >= 1.0 else ''}")
    print(f"frames built / s      {built / wall:6.1f}")
    print(f"frames sent / s       {sent / wall:6.1f}    re-sent because none was ready: {late} ({100.0 * late / max(1, sent):.1f} %)    dark events: {dark}")
    if usb:
        print(f"USB (per frame)       waiting for ready {usb['ready_ms']:5.1f} ms avg ({usb['polls']:.0f} polls), "
              f"WriteFrame {usb['write_ms']:4.1f} ms avg / {usb['write_max_ms']:.1f} ms worst")
    print(f"this process' CPU     {100.0 * (c1 - c0) / wall:6.0f} %  of one core (all threads together)")
    print()
    repeat_pct = 100.0 * late / max(1, sent)
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
        print("  Try: --pps 15000 (fewer points), close the browser/other programs, `nice -n -5`, a cooler/stronger supply (check throttled above).")
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

    p = sub.add_parser("bench", help="can this computer feed the DAC? (output stays DARK; run it on the Mac and on the Pi)")
    p.add_argument("scene", nargs="?", default="converge", help="the scene to build (default converge, the heaviest)")
    p.add_argument("--seconds", type=float, default=15.0, help="measuring time after a 2 s warm-up (default 15)")
    p.add_argument("--palette", choices=sorted(P.PALETTES), default="gsr")
    p.add_argument("--dry-run", action="store_true", help="no DAC at all: measures only the computer")
    p.add_argument("--lib", help="path to the Helios library (or set $HELIOS_LIB)")
    p.add_argument("--device", type=int, default=0, help="DAC number if several are connected")
    add_view_args(p)
    p.set_defaults(fn=cmd_bench)

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
