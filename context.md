# cosmic dune -- sensor pod: design history & handover

This is the "why", not the "how" -- `README.md` covers wiring/build/CLI.
Written for whoever (including future Edwin) needs to pick this back up
without re-deriving every decision from scratch.

## The project, one level up

*cosmic dune*: an artistic project sending a laser distress signal into
the sky, requiring an ILT (Inspectie Leefomgeving en Transport) permit.
The signal is triggered by 6 musicians' biosignals synchronizing on stage.
Each musician wears a **pod** (this repo): a Raspberry Pi Pico W reading
their heart rate and skin conductance, driving a 250-LED strip and an
on-pod OLED display live, and reporting telemetry to a central **hub**
(not built yet) that watches all 6 pods for synchronization and drives
the actual Laserworld CUBE 3.

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
- `src/main.cpp` -- wires it all together; boot order matters (see below).

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

## Current state (as of this writing)

Confirmed working on real hardware: pulse sensing, GSR sensing + colour
mapping, both pulse timing modes, WiFi connect + OSC telemetry to a real
hub-side listener, LittleFS persistence for both config files, the
SH1107 display (monitor view + menu), the M5Stack encoder (navigation,
short/long press), the full build (all libraries resolving, `chain` +
`lib_ignore` combination). `osc_test_hub.py` (terminal monitor +
simulate) and `osc_web_dashboard.py` (Flask graphical dashboard) both
verified end-to-end against real pod traffic.

Not yet built: the actual hub (OSC-receiving + DMX/ILDA control of the
Laserworld CUBE 3), USB MIDI reinstatement, the wired Ethernet fallback,
any workflow for keeping 6 physical pods' `net pod` ids straight, menu
submenu grouping (if the current flat 17-item list turns out to need it).

## Open questions for next time

- Default `PulseMode` for actual performance use -- `ibi` (current
  default) or `fixed`? Needs eyes-on with real musicians, not a desk
  decision.
- Encoder rotation direction (`readSteps()` positive = clockwise) --
  carried over as an assumption from the reference project, not
  independently reconfirmed on this exact unit.
- Whether the 17-item flat menu needs grouping into submenus once used
  hands-on for real adjustments, or whether that's over-engineering a
  problem that doesn't actually bite in practice.
- Laser control path: DMX (preset patterns, coarse) vs ILDA (via an
  external DAC, full custom vector control) for the CUBE 3 -- see the
  laser-control discussion for where this landed.