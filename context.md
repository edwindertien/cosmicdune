# cosmic dune -- sensor pod: design history & handover

This is the "why", not the "how" -- `README.md` covers wiring/build/CLI.
Written for whoever (including future Edwin) needs to pick this back up
without re-deriving every decision from scratch.

## The project, one level up

*cosmic dune*: an artistic project sending a laser distress signal into
the sky, requiring an ILT (Inspectie Leefomgeving en Transport) permit.
The signal is triggered by 6 musicians' biosignals synchronizing on stage.
Each musician wears a **pod** (this repo): a Raspberry Pi Pico W reading
their heart rate and skin conductance, driving a 250-LED strip live, and
reporting telemetry to a central **hub** (not built yet) that watches all
6 pods for synchronization and drives the actual Laserworld CUBE 3.

## Hardware decisions

- **Grove Ear-clip Heart Rate Sensor, not the analog PulseSensor-Amped
  module.** This one matters because it changes the whole sensing
  approach: it's a digital comparator output (idles HIGH, pulses LOW per
  beat), so beat detection is edge-timing + debounce on a plain GPIO
  (GP9), not ADC peak-detection. Confirmed against real hardware; the
  remaining unknown is which edge polarity this specific module uses
  (currently assumed FALLING, easy to flip if wrong -- see README).
- **GSR sensor added later, on GP27/ADC1** -- not the pin originally
  reserved for it (GP26/ADC0) in an earlier iteration; GP27 is what
  actually got wired, and the code follows the hardware.
- **250-LED WS2812B strip on GP6**, powered from its own 5V supply, not
  the Pico -- see README's power budget note (15A worst case at full
  white/full brightness).

## Firmware architecture

- `src/drivers/` -- pulse_sensor (interrupt-driven beat detection),
  gsr_sensor (polled ADC + smoothing), pulse_strip (FastLED comet
  animation, two timing modes).
- `src/core/` -- net_config (WiFi/OSC settings) and pod_config (GSR/strip
  settings), each persisted to LittleFS as their own JSON file.
- `src/net/` -- osc_link (WiFi + OSC telemetry sender).
- `src/io/` -- cli (the only UI this pod has -- USB CDC serial).
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
(e.g. for a DAW), treat it as a fresh, isolated addition, not a
revert.

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
concerns -- rhythm from the heart, colour from arousal -- not colour
being "replaced" so much as reassigned to a signal it fits better.

### Two pulse timing modes

Added once it became clear that "one pulse takes one heartbeat's worth of
time to cross the strip" (`IBI_SYNCED`) doesn't handle a fast heart rate
gracefully with a single-comet design. `FIXED_SPEED` mode lets multiple
comets travel simultaneously at a constant, configurable speed --
switchable and tunable at runtime (`mode fixed|ibi`, `mode speed <ms>`),
default stays `IBI_SYNCED` so nothing changes underfoot. `MAX_PULSES` was
bumped from 4 to 8 to give `FIXED_SPEED` mode headroom for a fast heart
rate against a slow configured traversal.

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
model. Art-Net stays a live option if the hub's laser-driving side ends
up wanting a unified protocol, or if the venue already runs an Art-Net
lighting network -- not ruled out, just not chosen for pod telemetry.

The wired alternative (WIZnet W5500 Ethernet module, same OSC message
code over `EthernetUDP`) was discussed but not built. The CLI's `stream
on` over USB is the current zero-hardware fallback for bench use.

### `OscLink` reads `netConfig` live, not a cached copy

First version of `OscLink::begin()` took a copy of `NetConfig` at boot.
Caught before shipping: since the CLI edits the global `netConfig`
directly (`net enable`, `net ssid`, ...), a cached copy would silently
stop reflecting those changes until reboot. Fixed by having `OscLink`
read the global directly. This exact class of bug -- a second copy of
config drifting from the live source of truth -- came up again with GSR/
mode persistence (see below), and was avoided there from the start
because of this.

### `PodConfigStore` reads/writes live sensor objects, not a mirrored struct

When GSR range/invert and strip mode/speed needed persisting, the
straightforward approach would have been a `PodConfig` struct mirroring
`NetConfig`'s shape. Deliberately not done that way: `PodConfigStore::
save()`/`load()` call `gsrSensor`'s and `pulseStrip`'s own getters/
setters directly. There is only ever one place these settings live,
avoiding a repeat of the `OscLink` staleness bug above.

**GSR baseline is deliberately never persisted**, in either the CLI or
the config file -- `gsr_sensor.h`'s own comments explain why: baseline is
expected to need recalibrating most sessions, sometimes even the same
person on a different day, so saving a stale one would actively work
against the auto-calibration-at-boot design. `gsrRange` and `gsrInverted`
*are* persisted -- those describe the sensor/wiring, not the wearer, and
don't drift session to session.

### `lib_ldf_mode`: deep+ -> chain+ -> off

Three rounds of the same complaint ("scanning takes forever"), three
different actual causes:
1. **deep+** was the default carried over from the arm-controller
   project. Once FastLED was added, scans became very slow -- FastLED
   ships dozens of per-platform variant headers, and `deep+` parses all
   of them regardless of which one is actually used.
2. Switched to **chain+** (follows `#include` chains, still does extra
   work for libraries with incomplete metadata). Slow again once WiFi +
   ArduinoJson + the CNMAT OSC library were added -- the OSC library is
   fetched raw from GitHub with no PlatformIO registry manifest, which is
   exactly what triggers `chain+`'s expensive fallback scanning.
3. Settled on **off**: every library actually used (FastLED, ArduinoJson,
   OSC) is listed explicitly in `lib_deps`, and none of them pull in
   further sub-dependencies of their own -- so there's nothing for
   automatic discovery to usefully find. If a future dependency needs
   something not listed and the build fails with missing symbols, that's
   the sign to add it explicitly, not to reach for `chain` again.

### CLI bugs worth knowing about (already fixed, but instructive)

- **Leftover files still compiled after removal from `lib_deps`.**
  PlatformIO compiles every `.cpp` under `src/` as its own translation
  unit regardless of whether anything still includes it. Deleting
  `midi_engine.cpp`/`midi_bridge.cpp` from `lib_deps` didn't stop them
  compiling -- the files had to be deleted from disk. Worth remembering
  for any future removal, not just that one.
- **Silent fallthrough on malformed subcommands.** `net hub`, `net pod`,
  and `net rate` originally matched via a combined `a == "x" && n >= N`
  condition -- so `net hub <ip>` with a missing port didn't print a
  hub-specific error, it fell through to the generic multi-command usage
  text and was easy to miss. This is exactly what caused a real
  debugging session: `net save` appeared to succeed, but `net hub` had
  silently never taken effect, so telemetry silently never sent. Fixed by
  checking the subcommand keyword first, independent of argument count,
  so each subcommand always reports its own specific usage.
- **python-osc wildcard matching uncertainty.** The Python test tools
  deliberately use one `set_default_handler` with manual
  `"/pod/<id>/<leaf>"` parsing rather than per-address wildcard mappings
  like `/pod/*/bpm` -- OSC wildcard semantics vary enough between
  implementations that this felt safer to verify than trust, and was
  confirmed end-to-end with a live send/receive test rather than just
  read from docs.

## Current state (as of this writing)

Confirmed working on real hardware: pulse sensing, GSR sensing + colour
mapping, both pulse timing modes, WiFi connect + OSC telemetry to a
real hub-side listener, LittleFS persistence for both config files.
`osc_test_hub.py` (terminal monitor + simulate) and `osc_web_dashboard.py`
(Flask graphical dashboard, BPM/GSR graphs + per-pod source IP) both
verified end-to-end against real pod traffic, not just simulated.

Not yet built: the actual hub (OSC-receiving + DMX/ILDA control of the
Laserworld CUBE 3), USB MIDI reinstatement, the wired Ethernet fallback,
any workflow for keeping 6 physical pods' `net pod` ids straight.

## Open questions for next time

- Default `PulseMode` for actual performance use -- `ibi` (current
  default) or `fixed`? Needs eyes-on with real musicians, not a desk
  decision.
- Laser control path: DMX (preset patterns, coarse) vs ILDA (via an
  external DAC, full custom vector control) for the CUBE 3 -- see the
  laser-control discussion for where this landed.