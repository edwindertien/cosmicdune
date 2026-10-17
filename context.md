# cosmic dune -- sensor pod: design history & handover

This is the "why", not the "how" -- `README.md` covers wiring, build, CLI, the
Pi setup and the laser tools. Written for whoever (including future Edwin)
needs to pick this back up without re-deriving every decision from scratch.
Despite the title it now covers the whole system: the pod firmware, the Pi
(hotspot + dashboard) and the laser tools.

## The project, one level up

*cosmic dune*: an artistic project sending a laser distress signal into
the sky, requiring an ILT (Inspectie Leefomgeving en Transport) permit.
The signal is triggered by 6 musicians' biosignals synchronizing on stage.
Each musician wears a **pod** (this repo): a Raspberry Pi Pico W reading
their heart rate and skin conductance, driving a 250-LED strip and an
on-pod OLED display live, and reporting telemetry to a central **hub**
that watches all 6 pods for synchronization and drives the actual
Laserworld CUBE 3. The hub is not one program yet: a Raspberry Pi hosts the
pods' WiFi and a dashboard, and separate tools drive the laser (below). **A
standing constraint: everything must work with no internet at the venue.**

## Hardware decisions

- **Grove Ear-clip Heart Rate Sensor, not the analog PulseSensor-Amped
  module.** This one matters because it changes the whole sensing
  approach: it's a digital comparator output (idles HIGH, pulses LOW per
  beat), so beat detection is edge-timing + debounce on a plain GPIO, not
  ADC peak-detection. Confirmed against real hardware; the remaining
  unknown is which edge polarity this specific module uses (currently
  assumed FALLING, easy to flip if wrong -- see README).
- **GSR sensor on GP27/ADC1.**
- **250-LED WS2812B strip**, powered from its own 5V supply, not the
  Pico -- see README's power budget note (15A worst case at full
  white/full brightness).
- **SH1107 OLED (M5Stack "Unit OLED", 128x64) + M5Stack Unit Encoder**,
  added later as an on-pod display/menu alongside the CLI -- see below
  for the design decisions specific to these.

### Pin history

Pins moved more than once as the board's role grew:
- Heart rate: GP9 -> **GP26**, to free GP8/GP9 for the OLED's I2C0 bus.
- LED strip: GP6 -> GP10 (when GP6 became the encoder's I2C1 SDA) ->
  **GP17** on the actual physical build. `config.h` reflects the real
  wiring, not the intermediate guess.
- GSR stayed on GP27 throughout.

Current pinout is in README.md's wiring table; `config.h`'s `HW`
namespace is the single source of truth if the two ever disagree.

## Firmware architecture

- `src/drivers/` -- pulse_sensor (interrupt-driven beat detection),
  gsr_sensor (polled ADC + smoothing), pulse_strip (FastLED comet
  animation, two timing modes), display (SH1107 OLED), m5_encoder (raw
  I2C driver for the M5Stack Unit Encoder).
- `src/core/` -- net_config (WiFi/OSC settings) and pod_config (GSR/strip
  settings), each persisted to LittleFS as their own JSON file.
- `src/net/` -- osc_link (WiFi + OSC telemetry sender).
- `src/io/` -- cli (USB CDC serial) and menu (on-pod OLED+encoder settings UI).
- `src/main.cpp` -- wires it all together; boot order matters (see below). It
  also arms a 15 s hardware watchdog first thing in `setup()` and feeds it at
  the end of every `loop()`.
- `src/net/osc_link` is a small state machine (IDLE / CONNECTING / CONNECTED /
  GAVE_UP), and the menu has 19 items -- see "WiFi" below.
- `tools/` -- `osc_web_dashboard.py` (Flask, receives OSC, serves the touch-screen
  page), `osc_test_hub.py` (terminal monitor + fake pods), `chart.umd.js`.
- `tools/laser/` -- `helios_dac.py` (fail-dark ctypes driver), `patterns.py`
  (test patterns + frame renderer + safety validator + PNG previewer),
  `visuals.py` (pod-driven scenes, sync), `laser_test.py` (the CLI).

## Decisions and why

### CLI + MIDI + WiFi combo, then MIDI came back out

The very first iteration ported a working TinyUSB CDC+MIDI composite
device pattern from an earlier Pico W project (the arm-controller repo),
on the theory that combining USB CDC (CLI) and USB MIDI would be useful
for a hub that (eventually) wants a literal MIDI "click" per heartbeat.
Once real hardware arrived, **the CLI produced no output or response at
all** -- not a logic bug, a boot hang. The suspects, in order: the
TinyUSB/MIDI mount-wait dance (copied from working code, so lower
suspicion), and FastLED's RP2040/PIO support (new to this project,
flagged unverified from the start).

Rather than keep guessing, the fix was to strip TinyUSB/MIDI out
entirely and go back to the arduino-pico core's plain, much better-tested
default USB CDC `Serial`. That fixed it. MIDI has not been reinstated --
it's not needed for the current OSC-based telemetry design (see below),
and reintroducing it means re-litigating the exact composite-device
ordering that was removed. If a literal MIDI output is wanted later
(e.g. for a DAW), treat it as a fresh, isolated addition, not a revert.

### FastLED's "Forcing software SPI" message -- resolved, was a false alarm

FastLED was chosen over Adafruit_NeoPixel specifically because it doesn't
disable interrupts for the whole frame on RP2040 -- important because
beat detection is interrupt-driven, and Adafruit_NeoPixel's RP2040 driver
is known to bit-bang with interrupts held off per frame. Build logs
repeatedly showed FastLED's `"Forcing software SPI - no hardware
accelerated SPI for you!"` pragma message, which looked like it might be
undermining that reasoning. It doesn't: that message is about FastLED's
*clocked SPI* output path (for chipsets like APA102/DotStar that need a
separate clock wire), which `fastspi.h` compiles unconditionally as part
of `FastLED.h` regardless of which chipset a sketch actually uses. WS2812B
(what we use) goes through FastLED's entirely separate *clockless*
controller instead -- the message is boilerplate about a code path we
never touch, not a statement about our actual LED output.

The real, relevant guarantee is FastLED's own documented per-platform
`FASTLED_ALLOW_INTERRUPTS` default: RP2040 is explicitly listed at the
"interrupts re-enabled between pixels" setting (unlike AVR/STM32, which
hold interrupts off for the entire frame). The pulse-sensor ISR just
timestamps an edge -- a handful of instructions, far under WS2812's
~30us-per-pixel budget -- so it's genuinely safe under this. FastLED was
the right call; this just needed sourcing properly instead of staying
flagged as an open question.

### Colour source moved from BPM to GSR

Originally the travelling pulse's colour was a function of heart rate
(white->blue->purple across a configurable BPM range). Once GSR was
added, colour was switched to depend on GSR instead (white->blue->
purple->red across GSR's already-normalized 0..1 range), with heart rate
now driving only motion/tempo. This is a deliberate separation of
concerns -- rhythm from the heart, colour from arousal.

### Two pulse timing modes

Added once it became clear that "one pulse takes one heartbeat's worth of
time to cross the strip" (`IBI_SYNCED`) doesn't handle a fast heart rate
gracefully with a single-comet design. `FIXED_SPEED` mode lets multiple
comets travel simultaneously at a constant, configurable speed --
switchable and tunable at runtime (`mode fixed|ibi`, `mode speed <ms>`,
or the on-pod menu), default stays `IBI_SYNCED`. `MAX_PULSES` was bumped
from 4 to 8 to give `FIXED_SPEED` mode headroom for a fast heart rate
against a slow configured traversal.

### Networking: OSC over WiFi, not Art-Net or BLE MIDI

Evaluated three options for pod -> hub telemetry:

| | OSC/UDP | Art-Net/WiFi | BLE MIDI |
|---|---|---|---|
| Library on Pico W | mature | mature (confirmed via `ArtnetWifi`, tested on Pico W) | none -- hand-rolled GATT service |
| Fits this data | natural (typed, addressable) | awkward (8-bit DMX channels, no native float) | ok for beat+CC, awkward for GSR |
| 6 senders -> 1 hub | trivial (stateless UDP listener) | workable but built for 1-controller->many-fixtures | 6 simultaneous BLE connections to one central -- unverified on this stack |
| Wired fallback, same code | yes (`WiFiUDP` -> `EthernetUDP`) | yes | no |

OSC won mainly because the data (BPM, IBI in ms, a 12-bit GSR reading)
doesn't shoehorn into 8-bit lighting channels without an unnecessary
encode/decode step, and because a UDP listener handles N independent
senders with no per-connection state, unlike BLE's central/peripheral
model. The wired alternative (WIZnet W5500 Ethernet module, same OSC
message code over `EthernetUDP`) was discussed but not built. The CLI's
`stream on` over USB is the current zero-hardware fallback for bench use.

### `OscLink` reads `netConfig` live, not a cached copy

First version of `OscLink::begin()` took a copy of `NetConfig` at boot.
Caught before shipping: since the CLI edits the global `netConfig`
directly (`net enable`, `net ssid`, ...), a cached copy would silently
stop reflecting those changes until reboot. Fixed by having `OscLink`
read the global directly. This exact class of bug -- a second copy of
config drifting from the live source of truth -- came up again with GSR/
mode persistence, and was avoided there from the start because of this.

### `PodConfigStore` reads/writes live sensor objects, not a mirrored struct

When GSR range/invert and strip mode/speed needed persisting, the
straightforward approach would have been a `PodConfig` struct mirroring
`NetConfig`'s shape. Deliberately not done that way: `PodConfigStore::
save()`/`load()` call `gsrSensor`'s and `pulseStrip`'s own getters/
setters directly. There is only ever one place these settings live,
avoiding a repeat of the `OscLink` staleness bug above.

**GSR baseline is deliberately never persisted**, in either the CLI or
the config file, or the on-pod menu -- `gsr_sensor.h`'s own comments
explain why: baseline is expected to need recalibrating most sessions,
sometimes even the same person on a different day, so saving a stale one
would actively work against the auto-calibration-at-boot design.
`gsrRange` and `gsrInverted` *are* persisted -- those describe the
sensor/wiring, not the wearer, and don't drift session to session.

### On-pod display + menu: sourced from a previous project, not built from scratch

Once the SH1107 OLED and M5Stack Unit Encoder were added, a previous Pico
project (`picoST3215DXL`, a servo controller using the exact same two
units, on different pins and sharing one I2C bus rather than two
separate ones) turned out to already have proven, working code for both
-- reviewing it resolved several things that would otherwise have stayed
guesses:

- **SH1107 orientation.** The constructor takes `(64, 128)` for this
  128x64 panel -- height first, confirmed against Adafruit's own official
  example -- but that alone leaves the coordinate system in the
  controller's native 64-wide "portrait" addressing. `picoST3215DXL`
  pairs this with `setRotation(1)`, which is what actually presents it as
  128x64 landscape to Adafruit_GFX. Passing `(64, 128)` without the
  matching `setRotation(1)` (an earlier version of this code did exactly
  that) produces a wrapped/corrupted image -- the two go together, not
  either alone.
- **M5Stack Unit Encoder register map.** Rather than depend on the
  official `m5stack/M5Unit-Encoder` Arduino library (whose exact
  `begin()` overload for a non-default I2C bus wasn't fully confirmed),
  `drivers/m5_encoder.cpp` talks to the device over raw I2C directly,
  using the register map `picoST3215DXL` already had working: `0x00`
  MODE, `0x10` ENCODER (2 bytes, little-endian, 2 counts per detent),
  `0x20` BUTTON (**active-low** -- that project's own code comment says
  plainly "Your module uses inverted logic", i.e. verified against real
  hardware), `0x30` RGB LED. This also dropped an entire external
  dependency (the official library) in favour of ~100 lines of
  already-proven code.
- **I2C bus recovery.** `picoST3215DXL` clocks SCL 9 times plus a manual
  STOP condition before `Wire.begin()`, to free a slave device left
  holding SDA low from a previous abrupt reset -- without this,
  `Wire.begin()` on RP2040 can hang indefinitely. Ported into both
  `display.cpp` and `m5_encoder.cpp`'s `begin()`, since each is its own
  physically separate I2C bus that can independently end up wedged.

One thing carried over as an assumption rather than independently
reconfirmed: `M5Encoder::readSteps()` returning positive for clockwise.

### On-pod menu: what's covered there vs. CLI-only

The menu grew in two passes. First pass covered GSR range/invert/
baseline-reset, strip mode/speed, brightness, and saving -- deliberately
excluding WiFi entirely, on the reasoning that entering an SSID/password
by turning a knob is bad UX regardless of implementation. Second pass
added WiFi *status* (SSID shown, password only as set/not-set -- same
convention as the CLI's `net`) and hub IP/port editing, once it was
clear those are different asks: numeric IP octets and a port number are
genuinely knob-friendly (handled via `IPAddress`'s own `operator[]`, not
hand-rolled string parsing), and viewing what's already configured isn't
the same as typing new text. Free-text SSID/password entry is still
excluded and still CLI-only.

This grew the menu to 17 flat (non-nested) items. Not yet clear whether
that's actually a UX problem in practice -- see README's note on
possible future submenu grouping.

### The graph redesign: BPM trend line -> "faux" pulse waveform

The OLED's first graph originally plotted a BPM value history (a normal
line chart, min/max scale, one point per beat). Replaced with a
scrolling waveform instead: a small stylized blip
(`{0, 1, -2, 8, -6, 2, -1, 0}`) gets injected at the right edge and
scrolled left, restarted from the beginning on every real detected beat.
The *timing* is genuine (driven by actual heartbeats); the *shape* is
synthetic, since the ear-clip sensor is a digital comparator with no raw
analog waveform to display -- hence "faux". Sweep speed is fixed (one
column per screen redraw, ~8.4s to cross the full width), independent of
actual heart rate, matching how a real bedside monitor's sweep works
(fixed paper/sweep speed, rate shown as a separate number). The BPM
number readout next to it is unchanged. `Screen::GRAPH_BPM_MIN/MAX` and
the old `_bpmHistory` ring buffer were removed as dead code, not left
around unused.

The GSR graph is a normal value-history trend line (unlike BPM's
waveform) since GSR doesn't have discrete "events" to trigger off --
sampled on its own ~1s timer, independent of the display's own ~100ms
redraw rate, since GSR moves far slower than either.

### Two repeats of the same C++ mistake, both real build breaks

Twice, a `namespace X` in `config.h` collided with a `class X` of the
same name elsewhere (`Display`/`Display`, then `Menu`/`Menu`) -- C++
doesn't allow a namespace and a class to share one name. Both were fixed
by renaming the *namespace* (to `Screen`, then `MenuCfg`), keeping the
class name intact since that's the more prominently-referenced identifier
everywhere else. Worth deliberately checking for this pattern before
introducing a new config namespace alongside a same-named class, since it
happened twice despite the first occurrence.

### `lib_ldf_mode`: deep+ -> chain+ -> off -> chain (+ lib_ignore)

Four rounds of build-tooling trouble, four different actual causes:
1. **deep+** (from the arm-controller project) got slow once FastLED was
   added -- it ships dozens of per-platform variant headers that `deep+`
   parses regardless of which one is actually used.
2. **chain+** was faster but slow again once WiFi + ArduinoJson + the
   CNMAT OSC library were added -- the OSC library, fetched raw from
   GitHub, has no PlatformIO registry manifest, which triggers `chain+`'s
   expensive fallback scanning for libraries with unclear metadata.
3. **off** (every used library explicit in `lib_deps`, no automatic
   discovery) seemed to fix it -- until it broke `Wire.h`/`LittleFS.h`
   resolution outright once the SH1107/encoder libraries reshuffled the
   dependency graph. Turns out `off` also blocks discovery of
   framework-*bundled* headers, not just third-party ones; it had only
   appeared to work due to leftover build cache from before `off` was set.
4. **chain** (current): real dependency discovery restored, without
   `chain+`/`deep+`'s extra exhaustive-scan pass (the actual source of
   the earlier slowness, not discovery itself). This in turn exposed a
   separate, confirmed-upstream issue: arduino-pico bundles all its WiFi
   backends (CYW43, ESPHost, WINC1500) under one library folder, and
   `chain` compiles every file in a library it pulls in -- including
   `lwIP_ESPHost.cpp`, meant only for boards with an external ESP32
   co-processor, which hard-fails to compile for `rpipicow` (needs a pin
   macro that's never defined for this board). Fixed with
   `lib_ignore = lwIP_ESPHost` in `platformio.ini` -- confirmed as a real,
   currently-open, unresolved issue in `maxgerhardt/platform-raspberrypi`
   (#83), not something specific to this project.

### CLI bugs worth knowing about (already fixed, but instructive)

- **Leftover files still compiled after removal from `lib_deps`.**
  PlatformIO compiles every `.cpp` under `src/` as its own translation
  unit regardless of whether anything still includes it. Deleting
  `midi_engine.cpp`/`midi_bridge.cpp` from `lib_deps` didn't stop them
  compiling -- the files had to be deleted from disk.
- **Silent fallthrough on malformed subcommands.** `net hub`, `net pod`,
  and `net rate` originally matched via a combined `a == "x" && n >= N`
  condition -- so `net hub <ip>` with a missing port didn't print a
  hub-specific error, it fell through to the generic multi-command usage
  text and was easy to miss. This is exactly what caused a real
  debugging session: `net save` appeared to succeed, but `net hub` had
  silently never taken effect, so telemetry silently never sent. Fixed by
  checking the subcommand keyword first, independent of argument count.
- **python-osc wildcard matching uncertainty.** The Python test tools
  deliberately use one `set_default_handler` with manual
  `"/pod/<id>/<leaf>"` parsing rather than per-address wildcard mappings
  like `/pod/*/bpm` -- OSC wildcard semantics vary enough between
  implementations that this felt safer to verify than trust, and was
  confirmed end-to-end with a live send/receive test rather than just
  read from docs.

### WiFi: non-blocking connect, give-up, watchdog

**Symptom:** about 3 s after boot the pod froze -- LED strip and heart-rate
dead -- and stayed frozen when no WiFi could be found.

**First attempt, wrong.** A state machine with an 8 s give-up timer around
`WiFi.begin()`. It could never help: on this core `WiFi.begin()` blocks *inside
the call* until the join succeeds or fails, and the timer only runs after the
call returns. (The header comment claimed "non-blocking"; it wasn't. A timeout
cannot guard a blocking call from the same thread.)

**Evidence it can be worse than slow.** The CYW43 join has documented hangs
(pico-sdk issue 2186: a hang while connecting in station mode), and other
projects report that the blocking join can wait forever (Meshtastic's RP2040
WiFi fix runs it in a separate task for that reason). So "wait it out" is not
an option.

**What it does now.**
- `WiFi.beginNoBlock()` returns immediately; `OscLink::tick()` polls
  `WiFi.status()` while the rest of `loop()` keeps running. (Rob Miles' write-up
  of `beginNoBlock` as a background connection on the Pico W is what surfaced it.)
- After `Timing::WIFI_CONNECT_TIMEOUT_MS` (8 s) it calls `WiFi.disconnect()`
  (so the driver stops retrying in the background -- otherwise "gave up" would
  be mere bookkeeping) and enters GAVE_UP. It does **not** retry on its own:
  `net retry`, the menu's Net Retry, or toggling enable/disable restarts it.
  Rationale: a network that isn't there should not occupy the pod forever, and
  a person is back in the loop.
- An empty password is passed as `nullptr` (an open network), not as an empty
  WPA passphrase. (The test hotspot is open.)
- The menu gained **WiFi Status** (view) and **Net Retry**; `status`/`net` print
  off / connecting / connected / gave up.
- **Watchdog backstop:** `rp2040.wdt_begin(15000)` early in `setup()`,
  `rp2040.wdt_reset()` last in `loop()` and around the WiFi calls. If anything
  stalls the pod 15 s -- WiFi or not -- it reboots instead of freezing through a
  performance. Cost: a reboot loses the GSR baseline. Risk: if something still
  blocks >= 15 s, the pod reboots forever (signature: the boot banner every ~15 s).
  A guard that skips auto-connect after a watchdog reset was considered and not
  built, because `rp2040.reboot()` also goes through the watchdog.

**Rejected:** running WiFi on the RP2040's second core so a hang there can't
touch the first. A comment on an arduino-pico pull request reports WiFi used from both
cores is unstable, and this firmware also does I2C and FastLED.

**Status:** joining the Pi's hotspot and sending OSC works *(confirmed)*. The
behaviour with no network in range -- LEDs and heart-rate keep going, give-up
after 8 s, `net retry` recovers, no reboot loop -- is *(untested)*.

### The Pi hosts the network; the hotspot saga

**Why a Pi access point:** venue WiFi is an unknown (client isolation, captive
portals, no coverage on the day). A network the Pi creates is always there once
the Pi is on, and it removes the "no network found" case from the show.

**How:** current Raspberry Pi OS (Bookworm/Trixie) runs NetworkManager, which
has hotspot built in (`nmcli`); no `hostapd`/`dnsmasq` by hand. The
`ipv4.method shared` profile gives DHCP to the pods; by default the Pi is
`10.42.0.1`.

**The flapping.** The first attempts -- a manual profile plus a systemd unit
(`nmcli connection up Hotspot`) plus autoconnect -- gave a hotspot that
appeared, vanished, let the Pi rejoin the home WiFi, and came back, repeatedly.
The Pi has **one radio**: it is an AP *or* a client, never both, and
NetworkManager picks among saved profiles by priority. Likely contributors:
two activation mechanisms (autoconnect and the boot unit), possibly duplicate
profiles of the same name (the unit's by-name `up` is ambiguous then), and the
home profile still set to autoconnect. The recommended recipe (README): one
profile, priority 100, `autoconnect-retries 0` (the default of 4 can hand the
radio back to the home network after a few failures), other WiFi profiles
autoconnect-off, no boot unit. A hotspot was later created and a pod joined it
(10.42.0.38); **the actual root cause was never pinned down**, and stability
across reboots is unconfirmed. Next time it flaps: read the `reason '...'` in
`journalctl -u NetworkManager -f`.

**Security note:** the current test hotspot (`distress`) is open (`pass=(unset)`).

### Offline-first, and the dashboard

**The bug that taught the rule.** The first dashboard loaded Chart.js from a CDN.
On the hotspot the Pi had no internet, so the page's setup threw before it ever
started polling -- the page sat on "no pods heard yet" while the server was
receiving data fine (confirmed by `curl /api/data` and by running the page's
JavaScript in Node with Chart.js absent). Fix: `chart.umd.js` (Chart.js 4.5.1,
MIT, from npm) is served by the dashboard itself; the CDN fallback was removed
on purpose (it would only ever fail slowly); the page also no longer depends on
Chart.js to start -- tiles and numbers keep working and the graph area says what
is missing. The page contains no external URL (checked by logging every request
in a headless browser).

**The 800x480 redesign.** Six always-present tiles (pod slots), tap for detail,
because 12 small graphs don't fit and a status screen wants values at a glance.
Decisions worth knowing:
- A tile's GSR meter uses the same four-stop palette as the LED strip, so the
  screen shows what the musician's strip is showing.
- States: online (green), `no pulse` (amber: pod online, `signal` = 0), `OFFLINE`
  (nothing for 3 s), never heard (greyed, not tappable).
- The grid polls a small `/api/summary`; only the detail view fetches
  `/api/pod/<id>` with history. Less data per second on a Pi 3 (and over the
  hotspot) than shipping every pod's history every second.
- History is one point per pod per 250 ms for 60 s. BPM and GSR messages arrive ~30
  times a second (15 Hz x 2), and storing every one gave only ~13 s of history. A point is recorded only after
  both a BPM and a GSR value have arrived (otherwise the first point plotted
  the missing one as 0).
- It is a plain script, not `flask run`: the OSC listener starts in the script.
- Verification method: a headless Chromium (`@sparticuz/chromium` +
  `puppeteer-core`, installed in a scratch directory, not part of the repo)
  drove the page at exactly 800x480 with simulated pods: screenshots, taps,
  overflow check, console errors, the Chart.js-blocked case. **What that cannot
  tell:** touch ergonomics, kiosk mode, and performance on the real Pi.
- Also fixed on the way: the banner printed one IP (the interface the OS uses to
  reach the internet -- the Ethernet, not the hotspot); it now lists every
  interface's address with the `net hub` command for each.

### Laser: bring-up on its own, and a driver that fails dark

**Order:** the laser is the one place a software mistake can hurt someone, so
it was brought up separately from the sensors, with test patterns first.

**Helios SDK facts** (read from the SDK source, `github.com/Grix/helios_dac`,
MIT): USB frames are at most 4095 points; 20-40 ms per frame is recommended
(10 ms is the absolute minimum); `OpenDevicesOnlyUsb` skips the network (IDN)
scan; the macOS dylib in the repo is universal and finds its libusb next to
itself (`@rpath` + `@loader_path`); the Linux `.so` in the repo is x86-64 only.

**Fail dark.** The SDK's Python example writes frames with flags = 0, which
makes the DAC **loop the last frame forever** if the host program stops. The
driver always uses `SINGLE_MODE` (the SDK's own default): play once, and turn the
laser off if no new frame arrives. The price: a Python stall longer than a frame
(~30 ms) shows as a brief flicker (the status line counts "late" frames). The
hub may later want frame generation in its own thread.

**The official Pi build line is wrong for modern distros.** It lists
`-lusb-1.0` before the object files; with `--as-needed` (the Debian/Ubuntu/
Raspberry Pi OS default) the linker drops it and the `.so` fails to load
(`undefined symbol: libusb_open`). Found by building the real library and
loading it; the fix is to put `-lusb-1.0 -lpthread` after the `.o` files.

**Frame renderer** (`patterns.py`). Strokes are sampled at a density chosen to
land on a ~30 ms frame (bounded between 0.002 and 0.02 of the field per point),
with blanked moves between strokes, settle points where the beam turns on/off,
and corner dwell at sharp corners. Frames shorter than ~15 ms repeat the shape
up to ~20 ms (robust against host hiccups); repeating only when >25% short, so a
19.9 ms shape isn't doubled to 40 ms. Every frame starts and ends with the beam
off at the centre, so a frame stream that stops always rests on a blanked point.
**Validator:** refuses a frame with a static dot (lit extent < 0.04 on the +-1
field, or more than 8 lit points on one spot), a colour above the intensity cap,
a non-finite or out-of-field coordinate, a lit first/last point, or a bad point
count. It is a seatbelt against bugs in future pattern code, not a feature.

**First bench results** *(from the real laser)*: orientation correct; `circle`,
`orient`, `colors` correct; linearity good; start/stop of lines needs
calibrating; `sweep` showed red, blue, white but no green and a long pause;
`rays` moved the mirrors (audible) but showed no light.
- No green in `sweep` is by design: the palette is the pod's LED palette (white,
  blue, purple, red). `--palette spectrum` adds green.
- Pause / no light: **hypothesis** -- diode thresholds. At intensity 0.15 mixed
  colours drive some channels very low (purple is about 24/255 red, 4/255 green,
  33/255 blue) and a laser diode emits nothing below its threshold, per channel.
  Not confirmed. Hence `run ramp` (measure each channel's threshold) and
  `--rgb-min` / `--rgb-gain` (drive = intensity * gain * (min + (1-min)*c), 0 for
  c = 0, so the cap still bounds every channel).
- Line starts/stops: the galvos lag the beam, so `--blank-shift N` delays the
  colour N points; `--settle`, `--dwell`, `--travel-step` expose the settling
  and blanked-jump behaviour (the audible noise in `rays` may be blanked jumps
  that are too fast). **All of this is the second iteration and has not been
  run on the real laser.**

**Verification method:** a mock Helios shared library (same exported functions,
logs every call and dumps every frame) plus ~70 scripted checks, run in the
working session and **not part of the repo**: startup order (Stop before any
frame), flags = 2 on every frame, every shutdown path (Ctrl-C, SIGTERM,
SIGHUP, no DAC, refused arm) leaving Stop + shutter closed + close, interactive
keys through a pseudo-terminal, terminal restore, parameter limits refused before
the DAC is opened, live OSC input with simulated pods. Plus a real SDK build with
no DAC attached (symbol binding, error path) and rendered previews. **Not
tested:** actual USB traffic, the CUBE's ILDA behaviour, the Pi.

### Visuals: six pods into one pulse (`visuals.py`)

**The idea.** Each pod has a heartbeat oscillator at its own BPM. What is drawn
uses a *display* phase pulled toward the shared group phase by `sync`: sync 0 =
six independent pulses, sync 1 = one pulse. A beat is a quick swell and slow
relaxation, so a ring goes small -> big -> small. Scenes: `merge` (six rings
drift together and fuse), `bloom` (one ring in six sectors, raised-cosine
blended, colour blended too), `ripple` (concentric), `spokes` (rays). The preview
sheet (`preview scenes`) shows all of them at five sync values.

**Safety of the "dot".** Rings never shrink below `min_r` (0.14, raised for small
`--size` so the validator still passes), and small rings are dimmed (45% at the
smallest) so energy per unit length stays about constant. Near-identical rings
are drawn once (`merge_circles`): saves the point budget and stops merging pods
piling six beams on one line. With nobody online a dim idle ring breathes at 72.

**Where sync comes from.** `SyncMeter`: 1 - (mean absolute BPM deviation /
tolerance, default 10 BPM), low-passed over ~1.5 s. `effective_sync` adds a
manual *assist* knob: 0 = only what the pods do, 1 = fully merged. `--sync`
fixes it. **This is rate sync.** The pods send a smoothed BPM, not beat events, so
the visuals' pulse is our oscillator at their rate, not their actual heartbeat.
For real beat-phase sync the firmware would send a message per detected beat
(see Open questions).

**Limits found while testing:** at 20 kpps six large rings reach ~33 ms per frame
(`ripple`/`merge` at mid sync), i.e. ~30 fps; below that it is fine. Rendering
took 1-6 ms per frame on the small Linux VM used for testing (a one-off 28 ms spike
was seen, probably garbage collection); a Mac and the Pi 3 are untested.

**Inputs:** `DemoInputs` (scatter -> converge -> scatter, 90 s cycle) or
`OscInputs` (`--osc-port`, python-osc, the same `/pod/<id>/...` messages). Live
keys: `] [` sync assist, `0 1` knob min/max, `n p` scene, `b`/SPACE blackout, `q`.

### Lessons that cost time here

- A timeout around a blocking call can't fire until the call returns -- find the
  non-blocking call (`beginNoBlock`) instead of wrapping the blocking one.
- Anything fetched from the internet at run time is a bug on a hotspot with no
  internet (the CDN, and the dashboard's first IP banner assumed a default route).
- Vendor docs can be wrong for current systems (Helios link order). Build the real
  thing and load it, even without the hardware attached.
- State what was tested, and how: the mock, the headless browser and the previews
  each prove something specific and nothing more.

## Current state (as of this writing)

**Confirmed on real hardware:** pulse sensing, GSR sensing + colour mapping,
both pulse timing modes, LittleFS persistence for both config files, the
SH1107 display (monitor view + menu), the M5Stack encoder, the full build
(`chain` + `lib_ignore`); the pod joining the Pi's hotspot (got 10.42.0.38) and
OSC data reaching the dashboard on the Pi; the first round of laser test
patterns (orientation, circle, orient, colors, linearity -- see "Laser").

**Written but untested (do not assume they work):**
- Pod: WiFi non-blocking connect / give-up / `net retry` / watchdog with *no*
  network in range; the 19-item menu (WiFi Status, Net Retry).
- Pi: the 800x480 tile + detail dashboard on the real touch screen; the
  hotspot's stability across reboots.
- Laser: calibration flags, `ramp`, `show` and all four scenes, OSC input, the
  Pi-side Helios build. Previewed and mock-tested only.

**Not built:** the hub as one program (OSC -> sync -> visuals -> laser, with a
sync slider on the dashboard), beat events from the pods, Pi boot automation,
USB MIDI reinstatement, the wired Ethernet fallback, any workflow for keeping 6
physical pods' `net pod` ids straight, menu submenu grouping.

## Open questions for next time

- **Why `sweep` paused and `rays` showed nothing:** thresholds (run `ramp`)? Or
  something else -- blanked jumps too fast, size too small? Needs the laser.
- **Beat events.** Add `/pod/<id>/beat` to the firmware so the hub can measure
  real beat-phase coherence and the laser can fire on actual heartbeats? It
  changes what "sync" means for the piece (rate vs. phase), which is an artistic
  as much as a technical decision. Telemetry is 15 Hz over WiFi, so beat timing
  would be good to tens of milliseconds, not better.
- **The hub:** one process owning the OSC port, feeding both the dashboard and the
  laser; a sync slider on the touch screen. The dashboard and the laser tool
  currently each want the UDP port the pods send to.
- Is the hotspot stable across reboots, and should it get a WPA2 password before
  the show?
- The CUBE's safe `--pps` and scan size in ILDA mode; whether a Pi 3 renders and
  feeds `show` without late frames.
- Default `PulseMode` for actual performance use -- `ibi` (current
  default) or `fixed`? Needs eyes-on with real musicians, not a desk
  decision.
- Encoder rotation direction (`readSteps()` positive = clockwise) --
  carried over as an assumption from the reference project, not
  independently reconfirmed on this exact unit.
- Whether the 19-item flat menu needs grouping into submenus once used
  hands-on for real adjustments, or whether that's over-engineering a
  problem that doesn't actually bite in practice.
- Should the mock Helios library and its test scripts join the repo, so the
  driver's fail-dark behaviour can be re-checked after any change?

*Resolved since the last version:* DMX vs ILDA for the CUBE 3 -- ILDA via the
Helios USB DAC, in bring-up (see "Laser").