"""
cosmic dune -- Helios laser DAC driver (ctypes wrapper around the official SDK library).

Design rule: FAIL DARK.
  * Every frame is written with HELIOS_FLAGS_SINGLE_MODE: the DAC plays it ONCE and,
    if no new frame arrives, turns the laser off. (The SDK's own Python example uses
    flags=0, which makes the DAC loop the last frame forever if the host program dies.)
  * open() blanks and centres the output before anything else happens.
  * close() always runs Stop + shutter closed + CloseDevices, including via atexit.
  * Only hard kills (kill -9, power loss) bypass close() -- and SINGLE_MODE covers those.

Library: libHeliosLaserDAC.dylib (macOS, universal), libHeliosDacAPI.so (Linux -- the
one shipped in the SDK repo is x86-64 only, so on a Raspberry Pi build it, see
laser_test.py --help), HeliosLaserDAC.dll (Windows). Searched for in: --lib / $HELIOS_LIB,
next to this file, ./lib, the current directory, /usr/local/lib, /opt/homebrew/lib.
"""
from __future__ import annotations

import atexit
import collections
import ctypes
import os
import platform
import struct
import time
from typing import Iterable, List, Optional, Tuple

HELIOS_FLAGS_SINGLE_MODE = 1 << 1
HELIOS_MIN_PPS = 7
HELIOS_MAX_PPS = 0xFFFF
HELIOS_MAX_POINTS = 0xFFF

_ERRORS = {
    -1: "library not initialised", -2: "invalid device number", -3: "no points given",
    -4: "too many points", -5: "pps too high", -6: "pps too low", -7: "frame too small",
    -1000: "device closed / unplugged", -1001: "previous frame still transferring",
    -1002: "USB control transfer failed", -1003: "device reported an error",
    -1004: "null buffer", -1005: "signal too long", -1006: "not supported",
    -1007: "network error",
}


def describe_error(code: int) -> str:
    if code <= -5000:
        return f"libusb error {code + 5000}"
    return _ERRORS.get(code, "unknown error")


class HeliosError(Exception):
    pass


class NoDeviceError(HeliosError):
    pass


# libusb error codes (the SDK reports them as -5000 + code). A TIMEOUT / BUSY / INTERRUPTED / PIPE / IO / OVERFLOW answer is
# the normal result of a busy or slow host (a Raspberry Pi 3 under load): the next attempt usually works. NO_DEVICE,
# ACCESS, NOT_FOUND, NO_MEM, NOT_SUPPORTED are not worth retrying.
_TRANSIENT_LIBUSB = {-1: "I/O error", -6: "busy", -7: "timeout", -8: "overflow", -9: "pipe error", -10: "interrupted"}


class HeliosUsbError(HeliosError):
    """A failed USB transfer. .code is the libusb code (e.g. -7 = timeout); .transient says whether retrying makes sense."""

    def __init__(self, message: str, code: int, transient: bool) -> None:
        super().__init__(message)
        self.code, self.transient = code, transient


def usb_error(what: str, rc: int) -> HeliosError:
    """Build the right exception for a negative SDK return value."""
    msg = f"{what}: {describe_error(rc)} ({rc})" if what.startswith("WriteFrame") else f"{what}: {describe_error(rc)}"
    if rc <= -5000:
        lib = rc + 5000
        return HeliosUsbError(msg, lib, lib in _TRANSIENT_LIBUSB)
    return HeliosError(msg)


class HeliosPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_uint16), ("y", ctypes.c_uint16),
                ("r", ctypes.c_uint8), ("g", ctypes.c_uint8), ("b", ctypes.c_uint8),
                ("i", ctypes.c_uint8)]


assert ctypes.sizeof(HeliosPoint) == 8, "unexpected HeliosPoint layout"
_PACK = struct.Struct("<HHBBBB")


def _library_names() -> List[str]:
    return {"Darwin": ["libHeliosLaserDAC.dylib", "libHeliosDacAPI.dylib"],
            "Linux": ["libHeliosDacAPI.so", "libHeliosLaserDAC.so"],
            "Windows": ["HeliosLaserDAC.dll"]}.get(platform.system(), [])


def _search_dirs() -> List[str]:
    here = os.path.dirname(os.path.abspath(__file__))
    return [here, os.path.join(here, "lib"), os.getcwd(), os.path.join(os.getcwd(), "lib"),
            "/usr/local/lib", "/opt/homebrew/lib"]


def find_library(explicit: Optional[str] = None) -> str:
    for cand in (explicit, os.environ.get("HELIOS_LIB")):
        if cand:
            if os.path.isfile(cand):
                return cand
            raise HeliosError(f"Helios library not found at {cand}")
    for d in _search_dirs():
        for n in _library_names():
            p = os.path.join(d, n)
            if os.path.isfile(p):
                return p
    raise HeliosError(
        "Helios library not found (looked for " + ", ".join(_library_names()) + " in:\n  "
        + "\n  ".join(_search_dirs()) + "\nGive it explicitly with --lib PATH or $HELIOS_LIB. "
        "See `laser_test.py --help` for where to get it.")


class HeliosDAC:
    """One Helios DAC. Use as a context manager, or call open()/close() yourself."""

    def __init__(self, lib_path: Optional[str] = None, device: int = 0) -> None:
        self.lib_path = lib_path
        self.device = device
        self.name = ""
        self.firmware = -1
        self._lib = None
        self._opened = False
        self._closed = False
        # USB timing, last 120 frames: how long each frame waited for the DAC to be ready, how many status polls
        # that took, and how long WriteFrame itself took. Tells a slow USB path from a slow computer.
        self._t_ready = collections.deque(maxlen=120)
        self._t_polls = collections.deque(maxlen=120)
        self._t_write = collections.deque(maxlen=120)

    def stats(self) -> dict:
        """Averages/maxima (ms) over the last <=120 frames; all zeros before the first frame."""
        ready, polls, write = tuple(self._t_ready), tuple(self._t_polls), tuple(self._t_write)   # snapshots: the writer appends meanwhile
        def avg(d): return 1000.0 * sum(d) / len(d) if d else 0.0
        def mx(d): return 1000.0 * max(d) if d else 0.0
        return {"ready_ms": avg(ready), "ready_max_ms": mx(ready),
                "polls": (sum(polls) / len(polls)) if polls else 0.0,
                "write_ms": avg(write), "write_max_ms": mx(write)}

    # -- lifecycle ----------------------------------------------------------
    def open(self) -> "HeliosDAC":
        path = find_library(self.lib_path)
        self.lib_path = path
        try:
            libdir = os.path.dirname(os.path.abspath(path))
            for n in ("libusb-1.0.0.dylib", "libusb-1.0.dylib"):       # macOS: bundled next to it
                if os.path.isfile(os.path.join(libdir, n)):
                    try:
                        ctypes.CDLL(os.path.join(libdir, n), mode=ctypes.RTLD_GLOBAL)
                    except OSError:
                        pass
            self._lib = ctypes.CDLL(path)
        except OSError as e:
            hint = ""
            if platform.system() == "Darwin":
                hint = ("\nOn macOS this is usually quarantine or an unsigned binary. Try:\n"
                        f"  xattr -dr com.apple.quarantine \"{os.path.dirname(path)}\"\n"
                        f"  codesign --force --sign - \"{path}\" \"{os.path.dirname(path)}/libusb-1.0.0.dylib\"\n"
                        "(git clone does not quarantine files; browser zip downloads do.)")
            raise HeliosError(f"could not load {path}: {e}{hint}")
        self._bind()
        opener = getattr(self._lib, "OpenDevicesOnlyUsb", None) or self._lib.OpenDevices
        n = opener()
        if n <= 0:
            self._lib.CloseDevices()
            raise NoDeviceError(
                "No Helios DAC found. Check: USB cable, DAC powered by USB.  Linux/Pi: either run with "
                "sudo or install the udev rule (see `laser_test.py --help`).")
        if self.device >= n:
            self._lib.CloseDevices()
            raise HeliosError(f"device {self.device} requested but only {n} found")
        self._opened = True
        buf = ctypes.create_string_buffer(64)
        if self._lib.GetName(self.device, buf) >= 0:
            self.name = buf.value.decode(errors="replace")
        self.firmware = self._lib.GetFirmwareVersion(self.device)
        self._lib.Stop(self.device)          # blank + centre before anything else
        atexit.register(self.close)
        return self

    def _bind(self) -> None:
        L = self._lib
        L.OpenDevices.restype = ctypes.c_int
        if hasattr(L, "OpenDevicesOnlyUsb"):
            L.OpenDevicesOnlyUsb.restype = ctypes.c_int
        L.GetStatus.argtypes = [ctypes.c_uint]
        L.GetStatus.restype = ctypes.c_int
        L.WriteFrame.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.c_uint8,
                                 ctypes.POINTER(HeliosPoint), ctypes.c_int]
        L.WriteFrame.restype = ctypes.c_int
        L.SetShutter.argtypes = [ctypes.c_uint, ctypes.c_bool]
        L.SetShutter.restype = ctypes.c_int
        L.Stop.argtypes = [ctypes.c_uint]
        L.Stop.restype = ctypes.c_int
        L.GetName.argtypes = [ctypes.c_uint, ctypes.c_char_p]
        L.GetName.restype = ctypes.c_int
        L.GetFirmwareVersion.argtypes = [ctypes.c_uint]
        L.GetFirmwareVersion.restype = ctypes.c_int
        L.CloseDevices.restype = ctypes.c_int

    def close(self) -> None:
        """Idempotent. Always leaves the laser dark."""
        if self._closed or self._lib is None:
            return
        self._closed = True
        if self._opened:
            for call in (lambda: self._lib.Stop(self.device),
                         lambda: self._lib.SetShutter(self.device, False)):
                try:
                    call()
                except Exception:
                    pass
        try:
            self._lib.CloseDevices()
        except Exception:
            pass

    def __enter__(self) -> "HeliosDAC":
        return self.open()

    def __exit__(self, *exc) -> None:
        self.close()

    # -- output -------------------------------------------------------------
    def stop(self) -> None:
        if self._opened and not self._closed:
            self._lib.Stop(self.device)

    def write(self, points: Iterable[Tuple[int, int, int, int, int]], pps: int,
              timeout: float = 1.0, assume_ready: bool = False) -> None:
        """Send one frame of (x12, y12, r8, g8, b8) points. Blocks until the DAC can accept
        it (unless the caller has just returned from wait_ready: assume_ready=True). Played once
        (SINGLE_MODE) -- the caller must keep supplying frames."""
        if not self._opened or self._closed:
            raise HeliosError("DAC is not open")
        pts = list(points)
        n = len(pts)
        if not (1 <= n <= HELIOS_MAX_POINTS):
            raise HeliosError(f"{n} points (allowed 1..{HELIOS_MAX_POINTS})")
        if not (HELIOS_MIN_PPS <= pps <= HELIOS_MAX_PPS):
            raise HeliosError(f"pps {pps} out of range")
        buf = b"".join(_PACK.pack(x, y, r, g, b, 255) for (x, y, r, g, b) in pts)   # i=255: "unused"
        arr = (HeliosPoint * n).from_buffer_copy(buf)
        if not assume_ready:
            self.wait_ready(timeout)
        t0 = time.perf_counter()
        rc = self._lib.WriteFrame(self.device, pps, HELIOS_FLAGS_SINGLE_MODE,
                                  ctypes.cast(arr, ctypes.POINTER(HeliosPoint)), n)
        self._t_write.append(time.perf_counter() - t0)
        if rc != 1:
            raise usb_error("WriteFrame failed", rc)

    def wait_ready(self, timeout: float = 1.0, poll_s: float = 0.0005) -> None:
        """Block until the DAC can take another frame. Separate from write() so a caller can choose WHICH frame
        to send at the last moment (the newest one) instead of committing to one before waiting."""
        if not self._opened or self._closed:
            raise HeliosError("DAC is not open")
        t0 = time.perf_counter()
        deadline = time.monotonic() + timeout
        polls = 0
        while True:
            polls += 1
            st = self._lib.GetStatus(self.device)
            if st == 1:
                break
            if st < 0:
                raise usb_error("GetStatus failed", st)
            if time.monotonic() > deadline:
                raise HeliosError("timed out waiting for the DAC to become ready")
            time.sleep(poll_s)
        self._t_ready.append(time.perf_counter() - t0)
        self._t_polls.append(polls)


class DummyDAC:
    """Same interface, no hardware: for --dry-run. Sleeps for each frame's playback time."""

    lib_path = "(dry run)"
    name = "dry-run (no hardware)"
    firmware = 0

    def open(self) -> "DummyDAC":
        return self

    def write(self, points, pps, timeout: float = 1.0, assume_ready: bool = False) -> None:
        time.sleep(max(0.0, len(list(points)) / float(pps) - 0.001))

    def stats(self) -> dict:
        return {}

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass
