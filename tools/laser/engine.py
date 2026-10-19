"""
cosmic dune -- laser engine: pods + controls -> ILDA frames -> (optionally) the DAC, and every
frame published for the browser visualiser.

This is the piece that makes the on-screen visualiser TRUSTWORTHY: the browser is shown the very
same (x12, y12, r8, g8, b8) points that are handed to the DAC, after calibration (orientation,
--rgb-min, blank shift ...), not a re-drawing of what the pattern was meant to look like.

THREADS AND THE DAC. One thread (the engine's) makes every call into the DAC: write, stop, close.
Web handlers only set flags under a lock and the engine acts on them within one frame (~30 ms).
(The Helios library is not documented as thread-safe, so nothing else touches it.)

SAFETY MODEL
  * No DAC (dac=None): PREVIEW. Frames are generated and published, nothing is sent anywhere.
  * DAC present: starts SAFE (disarmed, laser dark). Frames are only sent while ARMED.
  * arm() needs an explicit request; blackout/disarm is immediate (flag -> Stop within a frame).
  * The operator-adjustable limits are enforced HERE, not in the browser: intensity is clamped to
    max_intensity and size to max_size whatever a client asks for.
  * Auto-disarm after auto_disarm_s without any control activity (0 = never).
  * Every frame still goes through patterns.validate(); a refused frame is simply not sent
    (SINGLE_MODE: no frames -> the laser turns itself off).
  * A DAC error disarms and latches an error; the engine keeps previewing.
"""
from __future__ import annotations

import struct
import threading
import time
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Tuple

import patterns as P
import visuals as V

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
                 bpm: float = 72.0, tol_bpm: float = 10.0, demo_speed: float = 1.0) -> None:
        self.summary_fn, self.base_view, self.cal = summary_fn, view, cal
        self.pps, self.frame_ms, self.palette = pps, frame_ms, palette
        self.dac = dac
        self.max_intensity, self.max_size = max_intensity, max_size
        self.auto_disarm_s, self.bpm, self.tol_bpm = auto_disarm_s, bpm, tol_bpm
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
                   "measured": 0.0, "effective": 0.0, "live": 0, "online": 0, "last_refusal": ""}

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
            "points": s["points"], "frame_ms": round(s["frame_ms"], 1), "fps": round(s["fps"], 1), "seq": seq,
            "armed_for_s": round(now - arm_time, 1) if c["armed"] else None,
            "auto_disarm_in_s": round(remaining, 1) if remaining is not None else None,
            "error": err, "note": note,
            "scenes": list(V.SCENES), "patterns": list(P.PATTERNS),
        }

    # ------------------------------------------------------------------ frames out
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

    def shutdown(self) -> None:
        """Stop the thread, then leave the DAC dark (Stop + shutter closed + close)."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        if self.dac is not None:
            self.dac.close()

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

    def _run(self) -> None:
        meter = V.SyncMeter(self.tol_bpm)
        t0 = last = last_write = time.monotonic()
        scene: Optional[V.Scene] = None
        scene_key = None
        pat_t0 = t0
        self.dac_ok = self.dac is not None
        while not self._stop.is_set():
            now = time.monotonic()
            dt = min(now - last, 0.1)
            last = now
            with self._lock:
                c = dict(self._c)
                stop_now, self._stop_pending = self._stop_pending, False
                if c["armed"] and self.auto_disarm_s and now - c["activity"] > self.auto_disarm_s:
                    self._disarm_locked("auto-disarm after inactivity")
                    c["armed"] = False
            if stop_now and self.dac is not None and self.dac_ok:
                try:
                    self.dac.stop()
                except Exception as e:                      # a failing Stop must not kill the engine
                    self._fail(str(e))
            try:
                pods = self._pods(c["source"], now - t0)
                view = replace(self.base_view, size=c["size"], intensity=min(c["intensity"], self.max_intensity))
                online = [p for p in pods if p.online]
                measured = effective = 0.0
                if c["kind"] == "scene":
                    key = ("scene", c["name"])
                    if key != scene_key:
                        scene, scene_key = V.SCENES[c["name"]](V.scene_min_r(view.size), self.palette), key
                    scene.min_r = V.scene_min_r(view.size)
                    measured = meter.update(dt, [p.bpm for p in online])
                    effective = c["sync"] if c["sync_mode"] == "hold" else V.effective_sync(measured, c["sync"])
                    scene.update(dt, pods, effective)
                    strokes = scene.strokes(pods)
                else:
                    key = ("pattern", c["name"])
                    if key != scene_key:
                        scene_key, pat_t0 = key, now
                    fn, _anim, _desc = P.PATTERNS[c["name"]]
                    effective = c["sync"]
                    strokes = fn(now - pat_t0, {"bpm": self.bpm, "sync": c["sync"], "sync_demo": False})
                fr = P.render(strokes, view, self.pps, self.frame_ms, self.cal)
                P.validate(fr, view.intensity)
            except P.UnsafeFrame as e:
                with self._lock:
                    self._s["refused"] += 1
                    self._s["last_refusal"] = str(e)
                time.sleep(0.02)
                continue
            except Exception as e:                           # a bug in a pattern must not take the engine down
                with self._lock:
                    self._s["refused"] += 1
                    self._s["last_refusal"] = f"{type(e).__name__}: {e}"
                time.sleep(0.05)
                continue
            pts = P.to_dac(fr)
            self._publish(pts)
            sent = False
            if c["armed"] and self.dac is not None and self.dac_ok:
                try:
                    self.dac.write(pts, self.pps)            # blocks until the DAC wants it: real-time pacing
                    sent = True
                except Exception as e:
                    self._fail(str(e))
            done = time.monotonic()
            if not sent:                                     # preview / safe: pace like the DAC would
                time.sleep(max(0.0, min(0.1, fr.duration_ms / 1000.0 - (done - now))))
                done = time.monotonic()
            with self._lock:
                s = self._s
                s["frames"] += 1
                if sent and (done - last_write) > 1.6 * fr.duration_ms / 1000.0:
                    s["late"] += 1
                last_write = done
                s.update(points=len(fr.points), frame_ms=fr.duration_ms, fps=fr.fps, measured=measured,
                         effective=effective, online=len(online), live=sum(1 for p in online if not p.simulated))
