# cosmic dune -- sensor pod (manual)

One of 6 wearable pods for the *cosmic dune* project: an artistic laser
distress signal sent into the sky, triggered by 6 musicians' biosignals
synchronizing. Each pod reads one musician's heart rate (ear-clip pulse
sensor) and skin conductance (GSR), renders that live on a 250-LED strip
worn on the body, and reports telemetry over WiFi (OSC) to a central hub.
The hub (gathering all 6 pods and driving the Laserworld CUBE 3) is a
separate, not-yet-built piece -- see `context.md` for where that stands.

## Wiring

| Signal                            | Pico W pin  |
|------------------------------------|-------------|
| Grove ear-clip signal              | GP9         |
| Grove ear-clip VCC / GND           | 3V3 / GND   |
| GSR sensor signal                  | GP27 (ADC1) |
| GSR sensor VCC / GND               | 3V3 / GND   |
| LED strip data-in                  | GP6         |
| LED strip 5V / GND                 | **external 5V supply, NOT the Pico** -- common GND back to the Pico |

**Power, read this first:** 250x WS2812B at full white/full brightness is on
the order of 15A @ 5V -- far beyond USB or a small onboard regulator. Feed
the strip from its own adequately-rated 5V supply with a common ground back
to the Pico, and inject power at both ends of the 5m run if you see
dimming/colour-shift toward the far end. Normal operation (one travelling
comet, not the whole strip lit) draws much less than the full-white worst
case -- size the supply for the worst case anyway.

## Build

Requires [PlatformIO](https://platformio.org/) (VS Code extension or CLI).

```
pio run -t upload
pio device monitor
```

If the `pico_w` environment fails to resolve, check the note at the top of
`platformio.ini` -- the board id used by the `maxgerhardt` platform fork can
shift between versions. If a build fails on a missing header from a file
under `src/core/` or `src/net/`, double check that file actually exists at
that path locally -- these are newer folders in the project and it's easy
to end up with a file saved flat in `src/` instead of its subfolder.

## First bring-up (no sensors needed)

1. Flash and open the serial monitor (115200 baud).
2. `status` -- confirms the CLI is alive; should print `bpm=0 ... signal=no
   mode=ibi gsr=... net=off`.
3. `beat 72` -- injects one synthetic heartbeat. Confirms, with no sensors
   attached at all, that a white/blue comet travels the strip once.
4. `beat 140 80` -- same, but with a synthetic 80% GSR level: the comet
   should read purple/red instead of white/blue (see `Config::gsrToColor`
   in `src/drivers/pulse_strip.cpp` to retune the gradient).
5. Clip on the ear-clip sensor, `stream on` -- watch live `bpm,ibiMs,signal`
   CSV lines. If `bpm` never moves off 0, or reads roughly double a manual
   pulse count, see the polarity note in `src/config.h` (`HW::PULSE_PIN`) --
   the sensor's beat edge may be RISING rather than the assumed FALLING on
   your specific module.
6. `gsr` -- watch the raw ADC count move as you rub your fingers together
   or relax. Set `gsr range <counts>` to whatever span actually takes you
   from white to red; `gsr invert on` if the colour runs backwards.

## CLI reference

### Status / diagnostics
```
status                   bpm/IBI/signal + strip mode + GSR + net, one line
stream on|off             stream 't,bpm,ibiMs,signal' CSV lines (~10Hz)
beat [bpm] [gsrPct]       inject one synthetic heartbeat (default 72bpm,
                          live GSR reading) -- exercises the LED pulse with
                          no sensors attached; gsrPct (0-100) previews a
                          GSR level instead of using the live sensor
reboot
```

### LED strip
```
led brightness <0-255>   strip brightness (not persisted -- see config.h
                          HW::STRIP_DEFAULT_BRIGHTNESS for the boot default)
mode fixed|ibi            fixed = constant traversal speed regardless of
                          BPM (fast heart rate -> multiple pulses in flight
                          at once); ibi = one traversal per heartbeat,
                          speed follows BPM (default)
mode speed <ms>            set the fixed-mode traversal time (default
                          config.h Config::FIXED_SPEED_TRAVERSAL_MS)
```

### GSR
```
gsr                       raw/baseline/range/inverted/normalized readout
gsr baseline [set|<raw>]  set = capture the current reading as the new
                          "calm" reference; <raw> sets it explicitly.
                          Auto-captured once at boot -- never persisted
                          (see context.md for why)
gsr range <counts>        raw ADC counts (12-bit) from baseline to "fully
                          sweaty" (default config.h Config::GSR_DEFAULT_RANGE)
                          -- widen if colour maxes to red too easily,
                          narrow if it barely leaves white
gsr invert on|off         flip if sweat reads as ADC counts DOWN on your
                          wiring instead of up
```

### WiFi / OSC telemetry
```
net                        WiFi/OSC config + live connection status
                          (password never echoed, only whether one is set)
net ssid <name>            set the WiFi SSID (may contain spaces)
net pass <pass>            set the WPA password (may contain spaces)
net pod <1-6>              this pod's id -- becomes the OSC address prefix
                          /pod/<id>/...  Set a DIFFERENT id on each of the
                          6 physical pods before saving, or they'll collide.
net hub <ip> <port>        hub's IP (dotted quad, not a hostname) + UDP port
net rate <1-60>            telemetry rate in Hz (default 15)
net enable|disable         turn WiFi/OSC telemetry on or off
net save                   persist current net settings to /netconfig.json
```
OSC messages sent per pod (see `src/net/osc_link.cpp`):
```
/pod/<id>/bpm      int    0 if no pulse signal
/pod/<id>/ibi      int    milliseconds
/pod/<id>/signal   int    0 or 1
/pod/<id>/gsrRaw   int    smoothed 12-bit ADC counts
/pod/<id>/gsr      float  0.0-1.0 normalized
```

### Persistence
```
save                       persist current GSR range/invert + strip
                          mode/speed to /podconfig.json
```
Two separate config files, two separate save commands -- `net save` for
WiFi (`/netconfig.json`), bare `save` for GSR/strip settings
(`/podconfig.json`). GSR **baseline** is never persisted by either.

## Testing the OSC link without the real hub

`tools/` has two standalone Python scripts (`pip install -r
tools/requirements.txt`):

- **`osc_test_hub.py monitor`** -- terminal dashboard: listens for real (or
  simulated) pods and prints a live-refreshing table, including this
  machine's IP and the exact `net hub` command to point a pod at it.
- **`osc_test_hub.py simulate --pods 6`** -- sends fake telemetry for N
  synthetic pods, so anything downstream (the dashboard, eventually the
  real hub) can be developed and tested with zero physical hardware.
- **`osc_web_dashboard.py`** -- a graphical alternative to `monitor`: a
  Flask web page with live BPM (fixed 30-180 scale) and GSR (fixed 0-1
  scale) graphs per pod, plus the same status table (now including each
  pod's source IP). Works with `osc_test_hub.py simulate` unchanged as a
  test data source -- both just listen on the same UDP port. Runs fine on
  a Raspberry Pi as well as a laptop (see `context.md`).

## Known unverified assumptions

- **`board = rpipicow`** in `platformio.ini` -- see the note there.
- **FastLED on RP2040**: chosen over Adafruit_NeoPixel specifically for its
  PIO-based (not interrupt-disabling) WS2812 driver -- but the build log's
  `"Forcing software SPI"` message from FastLED suggests this platform/pin
  combination may not actually be taking that PIO path. Not confirmed
  either way yet; see `context.md` for what to watch for.
- **Grove ear-clip edge polarity** (FALLING) -- flip to RISING in
  `src/drivers/pulse_sensor.cpp` if beats don't register or double-count.
- **GSR polarity and default range** -- both need calibrating per physical
  sensor/wearer; see the bring-up steps above.
- **`lib_ldf_mode = off`** in `platformio.ini` requires every used library
  to be listed explicitly in `lib_deps` -- if a future dependency needs
  something not listed and the build fails with missing symbols rather
  than a normal compile error, that's the sign to add it explicitly.

## Deliberately deferred (not in this iteration)

- **USB MIDI** -- removed while debugging an unrelated CLI issue (see
  `context.md`); not yet reinstated.
- **Wired telemetry path** -- discussed (a WIZnet W5500 Ethernet module
  running the same OSC/UDP code, just swapping `WiFiUDP` for
  `EthernetUDP`) but not implemented. The CLI's `stream on` over USB is
  the current zero-extra-hardware fallback for bench use.
- **The hub itself** -- OSC-receiving + DMX/ILDA laser control for the
  Laserworld CUBE 3 is a separate, not-yet-started piece.
- **Per-pod physical labeling/workflow** for assigning and tracking which
  of the 6 physical pods has which `net pod <id>` -- currently manual and
  easy to get wrong across 6 units.