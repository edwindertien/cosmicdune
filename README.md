# cosmic dune -- sensor pod (iteration 1)

Worn by a musician: a Grove ear-clip heart rate sensor drives a travelling
colour pulse along a 5m/250-LED strip in sync with their pulse, and reports
beats + BPM over USB MIDI. This is the pod half of the project; the central
hub (gathering all 6 pods and driving the Laserworld CUBE 3 over DMX) is a
separate, later piece.

## Wiring

| Signal                         | Pico W pin |
|---------------------------------|------------|
| Grove ear-clip signal            | GP9        |
| Grove ear-clip VCC / GND         | 3V3 / GND  |
| LED strip data-in                | GP6        |
| LED strip 5V / GND               | **external 5V supply, NOT the Pico** -- common GND back to the Pico |
| (reserved, not wired) GSR signal | GP26 / ADC0 |

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
shift between versions.

## First bring-up (no sensor needed)

1. Flash and open the serial monitor (115200 baud) -- also the USB MIDI
   device, so a DAW/MIDI monitor can be watching in parallel.
2. `status` -- confirms the CLI is alive and MIDI enumerated (`midi=mounted`).
3. `beat 72` -- injects one synthetic heartbeat. Confirms, with no sensor
   attached at all:
   - a white/blue comet travels the strip once
   - a Note On/Off (channel 1, note 60, velocity ~72) hits your MIDI monitor
4. `beat 140` -- same, but the comet should read purple instead of white/blue
   (see `Config::BPM_LOW/_MID/_HIGH` in `src/config.h` to retune the gradient).
5. Clip on the ear-clip sensor, `stream on` -- watch live `bpm,ibiMs,signal`
   CSV lines. If `bpm` never moves off 0, or reads roughly double a manual
   pulse count, see the polarity note in `src/config.h` (`HW::PULSE_PIN`) --
   the sensor's beat edge may be RISING rather than the assumed FALLING on
   your specific module.

## CLI reference

Type `help` over USB CDC for the full list (`status`, `stream on|off`,
`beat [bpm]`, `led brightness <0-255>`, `reboot`).

## Known unverified assumptions

- **FastLED on RP2040**: picked over Adafruit_NeoPixel specifically because
  its native RP2040 support drives WS2812 via PIO rather than bit-banging
  with interrupts briefly disabled -- important here since beat detection
  is interrupt-driven. Not yet run against real hardware; if `pio run` can't
  resolve a PIO-capable FastLED build for `rpipicow`, see the fallback note
  in `platformio.ini`.
- **Grove ear-clip edge polarity** (FALLING) -- see step 5 above.
- **`board = rpipicow`** -- see the note in `platformio.ini`.

## Deliberately deferred (not in this iteration)

- GSR sensor (pin reserved, not read).
- Any persisted config (brightness, gradient stops, MIDI channel/CC/note
  are all compile-time constants in `src/config.h` for now -- add a
  LittleFS-backed `ConfigStore` once these need to be field-tunable without
  reflashing).
- Hub link (WiFi/wired) -- `ENABLE_WIFI_AP` in `platformio.ini` is a
  placeholder only, not read by any code yet.
