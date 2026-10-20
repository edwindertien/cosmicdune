"""
cosmic dune -- laser engine: pods + controls -> ILDA frames -> (optionally) the DAC, and every
frame published for the browser visualiser.

This is the piece that makes the on-screen visualiser TRUSTWORTHY: the browser is shown the very
same (x12, y12, r8, g8, b8) points that are handed to the DAC, after calibration (orientation,
--rgb-min, blank shift ...), not a re-drawing of what the pattern was meant to look like.

THREADS AND THE DAC. Two engine threads, with a strict split of duties:
  * the PRODUCER builds frames (pods -> scene -> render -> validate), publishes them to the browser and
    leaves the newest one in a one-slot mailbox. It never touches the DAC.
  * the WRITER is the only thread that calls the DAC (wait, write, stop) until shutdown closes it. At the
    moment the DAC is ready it sends the NEWEST frame; if the producer has not delivered a newer one (a slow
    computer, e.g. a Raspberry Pi 3) it re-sends the last one, so the DAC is never starved and the laser
    does not flicker dark -- only the animation steps. If NO new frame has arrived for max_stale_s the
    writer stops the DAC: a stalled producer means dark, never a frozen picture.
Web handlers only set flags under a lock; the writer acts on them within one frame (~30 ms).
(The Helios library is not documented as thread-safe, so nothing else touches it.)

SAFETY MODEL
  * No DAC (dac=None): PREVIEW. Frames are generated and published, nothing is sent anywhere.
  * DAC present: starts SAFE (disarmed, laser dark). Frames are only sent while ARMED.
  * arm() needs an explicit request; blackout/disarm is immediate (flag -> Stop within a frame).
  * The operator-adjustable limits are enforced HERE, not in the browser: intensity is clamped to
    max_intensity and size to max_size whatever a client asks for.
  * Auto-disarm after auto_disarm_s without any control activity (0 = never).
  * Every frame still goes through patterns.validate(); a refused frame is never sent and the mailbox is
    emptied, so the writer goes dark at once (SINGLE_MODE: no frames -> the laser turns itself off).
  * A DAC error disarms and latches an error; the engine keeps previewing.
"""
from __future__ import annotations

import collections
import inspect
import os
import struct
import threading
import time
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Tuple

import patterns as P
import visuals as V

# The files of this folder are updated one by one by hand, so a stale one is a real risk. Without this check an old
# visuals.py made the engine's thread die on its first frame: no frames, no laser output, a silent dashboard.
_MISSING = [n for n in ("fit_min_extent", "lit_extent") if not hasattr(V, n)]
if _MISSING:
    raise ImportError(f"visuals.py is older than engine.py (it lacks {', '.join(_MISSING)}). "
                      f"Copy the new visuals.py into {os.path.dirname(os.path.abspath(V.__file__))} and start again.")

# Smallest size the UI/engine will accept: below ~0.08 the smallest ring of a scene is under the
# validator's static-dot limit and every frame would be refused (a blank visualiser).
MIN_SIZE = 0.10

_HDR = struct.Struct("<IHH")           # seq, n points, flags
_PT = struct.Struct("<HHBBBB")         # x12, y12, r, g, b, pad


def pack_frame(seq: int, pts: List[Tuple[int, int, int, int, int]]) -> bytes:
    """The binary frame the browser fetches: an 8-byte header + n x 8-byte points."""
    return _HDR.pack(seq, len(pts), 0) + b"".join(_PT.pack(x, y, r, g, b, 0) for x, y, r, g, b in pts)


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


class LaserEngine:
    SOURCES = ("live", "fill", "demo")

    def __init__(self, summary_fn: Callable[[], Dict], view: P.View, cal: P.Calib, pps: int, frame_ms: float,
                 palette: Callable[[float], P.Color] = P.gradient, *, dac=None, source: str = "demo",
                 scene: str = "converge", sync: float = 0.0, sync_mode: str = "auto",
                 max_intensity: float = 0.3, max_size: float = 0.6, auto_disarm_s: float = 1200.0,
                 bpm: float = 72.0, tol_bpm: float = 10.0, demo_speed: float = 1.0,
                 min_extent: float = 0.0, max_stale_s: float = 0.2, usb_grace_s: float = 1.0, viz_fps: float = 30.0,
                 adaptive_poll: bool = True, usb_log: Optional[Callable[[str], None]] = None) -> None:
        self.summary_fn, self.base_view, self.cal = summary_fn, view, cal
        self.pps, self.frame_ms, self.palette = pps, frame_ms, palette
        self.dac = dac
        self.max_intensity, self.max_size = max_intensity, max_size
        self.auto_disarm_s, self.bpm, self.tol_bpm = auto_disarm_s, bpm, tol_bpm
        self.min_extent = max(0.0, float(min_extent))     # scenes never get narrower than this (field units); 0 = off
        self.max_stale_s = max(0.05, float(max_stale_s))  # no new frame for this long while armed -> the writer goes dark
        self.usb_grace_s = max(0.1, float(usb_grace_s))   # transient USB errors (timeouts...) are ridden out for this long
        self.viz_fps = max(2.0, float(viz_fps))           # the visualiser's fetch rate ceiling (it backs off when the hub is overloaded)
        self.adaptive_poll = bool(adaptive_poll)          # False = the old way: poll the DAC every 0.5 ms (more USB traffic)
        self._usb_log = usb_log                           # called (from the writer thread) with one line per USB event
        self._usb_events: collections.deque = collections.deque(maxlen=50)    # the LAST events
        self._usb_first: List[str] = []                                       # the FIRST events (how the trouble started)
        self._usb_kinds: collections.Counter = collections.Counter()          # error text -> how often
        self._usb_first_t = 0.0                                               # monotonic time of the first USB error
        self._usb_runs = 0                                                    # runs of consecutive errors that ended (recovered)
        self._usb_longest_ms = 0.0                                            # the longest such run
        self._usb_run_start = 0.0                                             # start of a run that is still going on (0 = none)
        self.dac_ok = dac is not None
        self._mb_lock = threading.Lock()
        self._mb: Tuple[int, Optional[List], float] = (0, None, 0.0)   # newest frame for the writer: (seq, points, built at)
        self._taken = threading.Event()                   # the writer sets it each time it sent a frame (paces the producer)
        self._wthread: Optional[threading.Thread] = None
        self._build_hist: collections.deque = collections.deque(maxlen=60)
        self._stage_hist: collections.deque = collections.deque(maxlen=60)    # per frame: (pods, scene, render, validate, convert, publish) seconds
        self._demo = V.DemoInputs(demo_speed)
        self._lock = threading.Lock()
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        now = time.monotonic()
        self._c = {"source": source if source in self.SOURCES else "demo", "kind": "scene", "name": scene,
                   "sync": _clamp(sync, 0.0, 1.0), "sync_mode": sync_mode if sync_mode in ("auto", "hold") else "auto",
                   "size": _clamp(view.size, MIN_SIZE, max_size), "intensity": _clamp(view.intensity, 0.0, max_intensity),
                   "armed": False, "activity": now}
        self._arm_time = 0.0
        self._stop_pending = False
        self._error = ""
        self._note = ""
        self._seq = 0
        self._frame: Tuple[int, bytes] = (0, b"")
        self._s = {"frames": 0, "late": 0, "refused": 0, "points": 0, "frame_ms": 0.0, "fps": 0.0,
                   "measured": 0.0, "effective": 0.0, "live": 0, "online": 0, "last_refusal": "",
                   "extent": 0.0, "boost": 1.0, "peak_rgb": (0, 0, 0),
                   "dark": 0, "sent": 0, "build_ms": 0.0, "build_max_ms": 0.0, "load": 0.0, "refusal_t": 0.0,
                   "usb_errors": 0, "last_usb_error": ""}

    # ------------------------------------------------------------------ controls
    def control(self, patch: Dict) -> Tuple[bool, str]:
        """Apply a partial update from the UI. Returns (ok, message). Values are clamped here."""
        try:
            with self._lock:
                c = self._c
                if "source" in patch:
                    if patch["source"] not in self.SOURCES:
                        return False, "bad source"
                    c["source"] = patch["source"]
                if "select" in patch:
                    sel = patch["select"] or {}
                    kind, name = sel.get("kind"), sel.get("name")
                    if not ((kind == "scene" and name in V.SCENES) or (kind == "pattern" and name in P.PATTERNS)):
                        return False, "unknown scene/pattern"
                    c["kind"], c["name"] = kind, name
                if "sync" in patch:
                    c["sync"] = _clamp(float(patch["sync"]), 0.0, 1.0)
                if "sync_mode" in patch:
                    if patch["sync_mode"] not in ("auto", "hold"):
                        return False, "bad sync_mode"
                    c["sync_mode"] = patch["sync_mode"]
                if "size" in patch:
                    c["size"] = _clamp(float(patch["size"]), MIN_SIZE, self.max_size)
                if "intensity" in patch:
                    c["intensity"] = _clamp(float(patch["intensity"]), 0.0, self.max_intensity)
                if patch.get("blackout"):
                    self._disarm_locked("blackout")
                if "arm" in patch:
                    if patch["arm"]:
                        if self.dac is None:
                            return False, "no laser: the hub was not started with --laser"
                        if self._error:
                            return False, "laser error: " + self._error
                        c["armed"] = True
                        self._arm_time = time.monotonic()
                        self._note = "armed"
                    else:
                        self._disarm_locked("disarmed")
                c["activity"] = time.monotonic()
            return True, ""
        except (TypeError, ValueError, AttributeError) as e:
            return False, f"bad value: {e}"

    def _disarm_locked(self, why: str) -> None:
        self._c["armed"] = False
        self._stop_pending = True        # the engine thread calls dac.stop() on its next pass
        self._note = why

    def state(self) -> Dict:
        now = time.monotonic()
        with self._lock:
            c, s = dict(self._c), dict(self._s)
            err, note, seq = self._error, self._note, self._seq
            arm_time = self._arm_time
        if self.dac is None:
            mode = "preview"
        elif err:
            mode = "error"
        else:
            mode = "armed" if c["armed"] else "safe"
        remaining = max(0.0, self.auto_disarm_s - (now - c["activity"])) if (c["armed"] and self.auto_disarm_s) else None
        return {
            "enabled": True, "laser": self.dac is not None, "mode": mode,
            "dac": {"name": getattr(self.dac, "name", ""), "firmware": getattr(self.dac, "firmware", 0)} if self.dac else None,
            "source": c["source"], "select": {"kind": c["kind"], "name": c["name"]},
            "sync": c["sync"], "sync_mode": c["sync_mode"], "size": c["size"], "intensity": c["intensity"],
            "limits": {"max_intensity": self.max_intensity, "max_size": self.max_size, "min_size": MIN_SIZE},
            "measured_sync": round(s["measured"], 3), "effective_sync": round(s["effective"], 3),
            "pods_online": s["online"], "pods_live": s["live"],
            "frames": s["frames"], "late": s["late"], "refused": s["refused"], "last_refusal": s["last_refusal"],
            "refusal_age_s": round(now - s["refusal_t"], 1) if s["refusal_t"] else None,
            "points": s["points"], "frame_ms": round(s["frame_ms"], 1), "fps": round(s["fps"], 1), "seq": seq,
            "min_extent": self.min_extent, "extent": round(s["extent"], 3), "boost": round(s["boost"], 2),
            "peak_rgb": list(s["peak_rgb"]),
            "dark": s["dark"], "sent": s["sent"], "build_ms": round(s["build_ms"], 1),
            "build_max_ms": round(s["build_max_ms"], 1), "load": round(s["load"], 2),
            "max_stale_ms": int(self.max_stale_s * 1000), "usb": self._usb_stats(),
            "usb_errors": s["usb_errors"], "last_usb_error": s["last_usb_error"], "viz_ms": self._viz_ms(s["load"]),
            "stages": self._stages(), "usb_events": list(self._usb_events)[-6:], "adaptive_poll": self.adaptive_poll,
            "usb_first_error_s": (round(self._usb_first_t - arm_time, 2) if (self._usb_first_t and arm_time) else None),
            "usb_runs": self._usb_runs,
            "usb_longest_run_ms": round(max(self._usb_longest_ms, 1000.0 * (now - self._usb_run_start) if self._usb_run_start else 0.0)),
            "usb_kinds": dict(self._usb_kinds.most_common(6)),
            "armed_for_s": round(now - arm_time, 1) if c["armed"] else None,
            "auto_disarm_in_s": round(remaining, 1) if remaining is not None else None,
            "error": err, "note": note,
            "scenes": list(V.SCENES), "patterns": list(P.PATTERNS),
        }

    # ------------------------------------------------------------------ frames out
    def _stages(self) -> Dict[str, float]:
        """Where a frame's build time goes (ms, average of the last ~60 frames)."""
        h = tuple(self._stage_hist)
        names = ("pods", "scene", "render", "validate", "convert", "publish")
        if not h:
            return {n: 0.0 for n in names}
        return {n: round(1000.0 * sum(x[i] for x in h) / len(h), 2) for i, n in enumerate(names)}

    def usb_events_all(self, limit: int = 20) -> List[str]:
        """The first and the last USB events (at most `limit` lines), for a compact report."""
        first, last = list(self._usb_first), [e for e in self._usb_events if e not in self._usb_first]
        evs = first + last
        if len(evs) > limit:
            half = max(1, limit // 2)
            evs = evs[:half] + ["   ... (events in between left out) ..."] + evs[-half:]
        return evs

    def _viz_ms(self, load: float) -> int:
        """How often the page should fetch a frame. The laser has priority over the screen: when the hub cannot keep
        up the visualiser slows down by itself (~15 -> ~7 frames/s)."""
        ms = 1000.0 / self.viz_fps
        if load >= 0.9:
            ms = max(ms, 150.0)
        elif load >= 0.5:
            ms = max(ms, 66.0)
        return int(round(ms))

    def _usb_stats(self) -> Optional[Dict]:
        fn = getattr(self.dac, "stats", None)
        try:
            return fn() if (self.dac is not None and callable(fn)) else None
        except Exception:
            return None

    def wait_frame(self, since: int, timeout: float = 0.25) -> Optional[Tuple[int, bytes]]:
        """Block (up to timeout) for a frame newer than `since`; None if there is none."""
        deadline = time.monotonic() + timeout
        with self._cond:
            if since > self._frame[0]:
                # The caller's cursor is AHEAD of this engine's counter: it comes from an earlier run (the hub was
                # restarted while a browser tab stayed open). Without this the tab would wait until the new counter
                # overtook the old one -- minutes to hours -- while everything else on the page kept updating.
                since = 0
            while self._frame[0] <= since:                 # loop: a wake-up is not proof of a NEWER frame
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(remaining)
            return self._frame

    def _publish(self, pts: List[Tuple[int, int, int, int, int]]) -> None:
        with self._cond:
            self._seq += 1
            self._frame = (self._seq, pack_frame(self._seq, pts))
            self._cond.notify_all()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="laser-engine", daemon=True)
        self._thread.start()
        if self.dac is not None:
            self._wthread = threading.Thread(target=self._dac_loop, name="laser-dac", daemon=True)
            self._wthread.start()

    def shutdown(self) -> None:
        """Stop the thread, then leave the DAC dark (Stop + shutter closed + close)."""
        self._stop.set()
        self._taken.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        if self._wthread:
            self._wthread.join(timeout=3.0)
        if self.dac is not None:
            self.dac.close()                 # only after both threads ended: nothing else is using the DAC now

    # ------------------------------------------------------------------ the loop
    def _pods(self, source: str, t: float) -> List[V.PodInput]:
        live = V.summary_to_pods(self.summary_fn())
        if source == "live":
            return live
        demo = self._demo.pods(t)
        if source == "demo":
            return demo
        return [r if r.online else f for r, f in zip(live, demo)]

    def _fail(self, msg: str) -> None:
        with self._lock:
            self._error = msg
            self._disarm_locked("error")
        self.dac_ok = False

    # ---- hand-over between the two threads ---------------------------------------------------
    def _offer(self, pts: List[Tuple[int, int, int, int, int]]) -> None:
        with self._mb_lock:
            self._mb = (self._seq, pts, time.monotonic())

    def _invalidate(self) -> None:
        """A refused/failed frame: nothing may be sent until a good one arrives, so the writer goes dark."""
        with self._mb_lock:
            self._mb = (self._seq, None, 0.0)

    def _dac_loop(self) -> None:
        """THE WRITER: the only thread that calls the DAC while the engine runs."""
        dac = self.dac
        has_wait = callable(getattr(dac, "wait_ready", None))
        try:
            has_poll = has_wait and "poll_s" in inspect.signature(dac.wait_ready).parameters
        except (TypeError, ValueError):
            has_poll = False
        last_sent = 0
        dark = False                                    # output currently stopped because frames went missing
        busy_until = last_dur = 0.0                     # host-side model of the DAC's queue: when it will want the next frame
        err_since = 0.0                                 # start of the current run of transient USB errors (0 = none)
        run_n = 0                                       # errors in the current run
        last_write_t = 0.0                              # when the last frame was handed over successfully
        last_n = 0                                      # how many points that frame had

        def note(text: str) -> None:
            line = f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {text}"
            self._usb_events.append(line)
            if len(self._usb_first) < 10:
                self._usb_first.append(line)
            if self._usb_log:
                try:
                    self._usb_log(line)
                except Exception:
                    pass

        def survive(e: Exception, stage: str, n_pts: int = 0) -> bool:
            """A transient USB error (timeout, busy, interrupted ...) is ridden out for usb_grace_s: count it, back off,
            try again with the next frame. Anything else, or a run that lasts too long, is a real failure."""
            nonlocal err_since, busy_until, run_n
            now = time.monotonic()
            gap = f"{1000 * (now - last_write_t):.0f} ms after the last frame" if last_write_t else "before the first frame"
            n_pts = n_pts or last_n
            if n_pts:                                   # (n - 45) % 64 == 0 is the frame size the SDK has to work around (USB packet boundary)
                gap += f"; frame {n_pts} pts" + (" [= 45 mod 64]" if (n_pts - 45) % 64 == 0 else "")
            if not getattr(e, "transient", False):
                note(f"USB FATAL while {stage}: {e}  ({gap})")
                self._usb_kinds[f"FATAL {'GetStatus' if 'GetStatus' in stage else 'WriteFrame'}: {str(e).split(': ', 1)[-1]}"] += 1
                return False
            err_since = err_since or now
            if not self._usb_first_t:
                self._usb_first_t = now
            self._usb_run_start = self._usb_run_start or now
            run_n += 1
            with self._lock:
                self._s["usb_errors"] += 1
                self._s["last_usb_error"] = str(e)
                total = self._s["usb_errors"]
            self._usb_kinds[f"{'GetStatus' if 'GetStatus' in stage else 'WriteFrame'}: {str(e).split(': ', 1)[-1]}"] += 1
            note(f"USB error #{total} while {stage}: {e}  ({gap}; {run_n}{'th' if 10 <= run_n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(run_n % 10, 'th')} in a row)")
            if now - err_since > self.usb_grace_s:
                note(f"USB errors lasted over {self.usb_grace_s:g} s: giving up")
                return False
            busy_until = 0.0
            self._stop.wait(0.005)
            return True

        while not self._stop.is_set():
            with self._lock:
                armed = self._c["armed"]
                stop_now, self._stop_pending = self._stop_pending, False
            if stop_now:
                try:
                    dac.stop()
                except Exception as e:                  # a failing Stop must not kill the writer
                    self._fail(str(e))
                last_sent, dark, busy_until, err_since, run_n = 0, False, 0.0, 0.0, 0
            if not armed or not self.dac_ok:
                self._stop.wait(0.005)
                continue
            if has_wait and self.adaptive_poll:         # polling the DAC over USB is not free (and a Pi 3 has no CPU to spare):
                eta = busy_until - last_dur - time.monotonic() - 0.004
                if eta > 0:                             # sleep until shortly before it can want a frame, then poll finely
                    self._stop.wait(min(eta, 0.1))
            try:
                if has_wait:
                    if has_poll:
                        dac.wait_ready(1.0, poll_s=0.001 if self.adaptive_poll else 0.0005)   # ready -> only now choose WHICH frame
                    else:
                        dac.wait_ready(1.0)
            except Exception as e:
                if not survive(e, "waiting for the DAC (GetStatus)", last_n):
                    self._fail(str(e))
                continue
            with self._mb_lock:
                seq, pts, built = self._mb
            with self._lock:                            # an ARM-off / BLACKOUT that arrived while we waited wins
                if not self._c["armed"] or self._stop_pending:
                    continue
            now = time.monotonic()
            if pts is None or (seq == last_sent and now - built > self.max_stale_s):
                if not dark:                            # nothing valid and fresh to show: DARK, not a frozen picture
                    try:
                        dac.stop()
                    except Exception as e:
                        self._fail(str(e))
                    dark, busy_until = True, 0.0
                    if pts is not None:                 # (an empty mailbox is a refusal or start-up, counted elsewhere)
                        with self._lock:
                            self._s["dark"] += 1
                self._stop.wait(0.005)
                continue
            repeat = seq == last_sent
            try:
                if has_wait:
                    dac.write(pts, self.pps, assume_ready=True)
                else:
                    dac.write(pts, self.pps)            # (test doubles: write itself blocks for pacing)
            except Exception as e:
                if not survive(e, "sending a frame (WriteFrame)", len(pts)):
                    self._fail(str(e))
                continue
            now2 = time.monotonic()
            last_write_t = now2
            last_n = len(pts)
            last_dur = len(pts) / float(self.pps)
            busy_until = max(now2, busy_until) + last_dur
            if run_n:
                note(f"USB recovered after {run_n} error(s), {1000 * (now2 - err_since):.0f} ms")
                self._usb_runs += 1
                self._usb_longest_ms = max(self._usb_longest_ms, 1000.0 * (now2 - err_since))
                self._usb_run_start = 0.0
            err_since, run_n, dark, last_sent = 0.0, 0, False, seq
            self._taken.set()
            with self._lock:
                self._s["sent"] += 1
                if repeat:
                    self._s["late"] += 1                # re-sent: the producer was too slow for this slot

    def _run(self) -> None:
        """THE PRODUCER: pods + controls -> validated frames -> browser + mailbox. Never touches the DAC."""
        meter = V.SyncMeter(self.tol_bpm)
        t0 = last = time.monotonic()
        scene: Optional[V.Scene] = None
        scene_key = None
        pat_t0 = t0
        while not self._stop.is_set():
            now = time.monotonic()
            dt = min(now - last, 0.1)
            last = now
            with self._lock:
                c = dict(self._c)
                if c["armed"] and self.auto_disarm_s and now - c["activity"] > self.auto_disarm_s:
                    self._disarm_locked("auto-disarm after inactivity")
                    c["armed"] = False
            p0 = time.perf_counter()
            try:
                pods = self._pods(c["source"], now - t0)
                p1 = time.perf_counter()
                view = replace(self.base_view, size=c["size"], intensity=min(c["intensity"], self.max_intensity))
                online = [p for p in pods if p.online]
                measured = effective = 0.0
                boost = 1.0
                if c["kind"] == "scene":
                    key = ("scene", c["name"])
                    if key != scene_key:
                        scene, scene_key = V.SCENES[c["name"]](V.scene_min_r(view.size), self.palette), key
                    scene.min_r = V.scene_min_r(view.size)
                    measured = meter.update(dt, [p.bpm for p in online])
                    effective = c["sync"] if c["sync_mode"] == "hold" else V.effective_sync(measured, c["sync"])
                    scene.update(dt, pods, effective)
                    strokes = scene.strokes(pods)
                    # keep the pattern from shrinking below the floor (only scenes; see visuals.fit_min_extent)
                    view, boost = V.fit_min_extent(strokes, view, self.min_extent)
                else:
                    key = ("pattern", c["name"])
                    if key != scene_key:
                        scene_key, pat_t0 = key, now
                    fn, _anim, _desc = P.PATTERNS[c["name"]]
                    effective = c["sync"]
                    strokes = fn(now - pat_t0, {"bpm": self.bpm, "sync": c["sync"], "sync_demo": False})
                p2 = time.perf_counter()
                fr = P.render(strokes, view, self.pps, self.frame_ms, self.cal)
                p3 = time.perf_counter()
                P.validate(fr, view.intensity)
                p4 = time.perf_counter()
            except P.UnsafeFrame as e:
                self._invalidate()
                with self._lock:
                    self._s["refused"] += 1
                    self._s["last_refusal"] = str(e)
                    self._s["refusal_t"] = time.monotonic()
                time.sleep(0.02)
                continue
            except Exception as e:                           # a bug in a pattern must not take the engine down
                self._invalidate()
                with self._lock:
                    self._s["refused"] += 1
                    self._s["last_refusal"] = f"{type(e).__name__}: {e}"
                    self._s["refusal_t"] = time.monotonic()
                time.sleep(0.05)
                continue
            try:                                             # NOTHING in here may kill the thread: a dead producer is a silent dashboard
                pts = P.to_dac(fr)
                extent = V.lit_extent(strokes, view)
                peak = (max(p[2] for p in pts), max(p[3] for p in pts), max(p[4] for p in pts))
                p5 = time.perf_counter()
                self._publish(pts)
                self._offer(pts)
                p6 = time.perf_counter()
                self._stage_hist.append((p1 - p0, p2 - p1, p3 - p2, p4 - p3, p5 - p4, p6 - p5))
                built = time.monotonic() - now               # how long the computer needed for this frame
                self._build_hist.append(built)
                period = fr.duration_ms / 1000.0
                with self._lock:
                    s = self._s
                    s["frames"] += 1
                    avg = sum(self._build_hist) / len(self._build_hist)
                    s.update(points=len(fr.points), frame_ms=fr.duration_ms, fps=fr.fps, measured=measured,
                             effective=effective, online=len(online), live=sum(1 for p in online if not p.simulated),
                             extent=extent, boost=boost, peak_rgb=peak, build_ms=avg * 1000.0,
                             build_max_ms=max(self._build_hist) * 1000.0, load=avg / period if period > 0 else 0.0)
            except Exception as e:
                self._invalidate()
                with self._lock:
                    self._s["refused"] += 1
                    self._s["last_refusal"] = f"{type(e).__name__}: {e}"
                    self._s["refusal_t"] = time.monotonic()
                time.sleep(0.05)
                continue
            if self._wthread is not None and c["armed"] and self.dac_ok:
                self._taken.wait(0.1)                        # armed: the DAC's consumption paces us ...
                self._taken.clear()                          # ... and when it is starved we simply run flat out
            else:                                            # preview / safe: pace like the DAC would
                time.sleep(max(0.0, min(0.1, period - (time.monotonic() - now))))
