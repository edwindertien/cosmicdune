#pragma once
#include <Arduino.h>

// =============================================================================
// HARDWARE CONFIGURATION -- cosmic dune sensor pod, Raspberry Pi Pico W
//
// Interaction is now both CLI (USB CDC) and an on-pod SH1107 OLED + M5Stack
// encoder -- see io/menu.h for the split of what's adjustable where.
// =============================================================================
namespace HW {
  // Grove Ear-clip Heart Rate Sensor: onboard comparator, DIGITAL output
  // (idles HIGH, pulses LOW on each detected beat) -- this is NOT the
  // analog PulseSensor-Amped module, so there's no ADC/peak-detection here,
  // just edge timing. Moved from GP9 to GP26 to free GP8/GP9 for I2C0
  // (below) -- GP26 is ADC-capable but that's irrelevant here, it's just
  // used as a plain digital input.
  // NOTE (unverified against the physical module -- confirm with `beat`
  // vs. a live earclip and the `status` command): if beats never register,
  // or register at roughly double a manually-checked pulse, the edge
  // polarity in drivers/pulse_sensor.cpp (currently FALLING) is the first
  // thing to flip to RISING.
  static constexpr uint8_t PULSE_PIN = 26; // GP26

  // GSR (galvanic skin response) sensor -- analog, GP27/ADC1, unchanged by
  // this pinout revision. Sets the travelling pulse's colour (see
  // Config::gsrToColor's white->blue->purple->red gradient in
  // pulse_strip.cpp); heart rate (PULSE_PIN) still drives motion/tempo,
  // independently.
  static constexpr uint8_t GSR_PIN = 27; // GP27 / ADC1

  // WS2812B-style ("neopixel") strip, 5m @ 50 LED/m. Originally moved
  // from GP6 to GP10 when GP6 became I2C1 SDA, then wired to GP17 on the
  // physical build instead. FastLED's PIO-based driver isn't tied to
  // specific pins the way I2C/SPI hardware peripherals are, so this is
  // just a constant change, nothing structural.
  static constexpr uint8_t  STRIP_PIN                = 17;  // GP17
  static constexpr uint16_t STRIP_NUM_LEDS           = 250;
  static constexpr uint8_t  STRIP_DEFAULT_BRIGHTNESS = 120; // 0-255, FastLED global scale
  // POWER BUDGET, READ BEFORE FIRST POWER-ON: 250x WS2812B at full white,
  // full brightness draws on the order of 15A @ 5V -- nowhere near what a
  // Pico's USB/VBUS or a small 5V regulator can supply. This strip needs
  // its own adequately-rated 5V supply, fed into the strip directly (not
  // through the Pico), with a common GND back to the Pico, and injected at
  // both ends of a 5m run if you see colour-shift/dimming toward the far
  // end from voltage drop. STRIP_DEFAULT_BRIGHTNESS=120 and the fact that
  // only a travelling comet (not the whole strip) is ever lit keeps typical
  // draw far below the full-white worst case, but size the supply and wire
  // gauge for the worst case, not the typical one.

  // I2C0 -- SH1107 OLED (M5Stack "Unit OLED", 128x64, addr 0x3C). GP8/GP9
  // is a valid RP2040 I2C0 pin pair (GPIO%4==0 -> I2C0 SDA, %4==1 -> I2C0
  // SCL) -- confirmed against the RP2040 GPIO function table, not guessed.
  static constexpr uint8_t I2C0_SDA = 8; // GP8
  static constexpr uint8_t I2C0_SCL = 9; // GP9
  static constexpr uint8_t DISPLAY_I2C_ADDR = 0x3C;

  // I2C1 -- M5Stack Unit Encoder (addr 0x40). GP6/GP7 is a valid RP2040
  // I2C1 pin pair (GPIO%4==2 -> I2C1 SDA, %4==3 -> I2C1 SCL), same
  // confirmation basis as I2C0 above.
  static constexpr uint8_t I2C1_SDA = 6; // GP6
  static constexpr uint8_t I2C1_SCL = 7; // GP7
  static constexpr uint8_t ENCODER_I2C_ADDR = 0x40;

  // Confirmed working value from a previous project using the same OLED
  // and encoder units (picoST3215DXL) -- applied to both I2C buses.
  static constexpr uint32_t I2C_HZ = 400000UL;

  // Onboard status LED. On Pico W, LED_BUILTIN is routed through the CYW43
  // WiFi chip by the arduino-pico core (not a plain GPIO) -- digitalWrite()
  // on it still works, but confirm this on your specific core version.
  static constexpr int16_t LED_PIN = LED_BUILTIN;

  // Not used by this pinout revision, and not available even if you
  // wanted them: GP23/24/25/29 are wired internally to the Pico W's CYW43
  // WiFi chip, not exposed as general-purpose GPIO on this board.
}

// =============================================================================
// PULSE STRIP RENDERING
// =============================================================================
namespace Config {
  // Comet tail length behind each travelling pulse's head, in LEDs.
  static constexpr uint8_t PULSE_TAIL_LEDS = 18;

  // No-signal (sensor unclipped / cable fault) idle animation period.
  static constexpr uint32_t IDLE_BREATH_PERIOD_MS = 4000;

  // Default full-strip traversal time for PulseMode::FIXED_SPEED (see
  // pulse_strip.h) -- runtime-tunable via the CLI's `mode speed <ms>` or
  // the on-pod menu, this is just the value a fresh boot starts with.
  static constexpr uint32_t FIXED_SPEED_TRAVERSAL_MS = 1500;

  // GSR baseline->"fully sweaty" scaling, in raw 12-bit ADC counts
  // (0-4095) -- runtime-tunable via the CLI's `gsr range <counts>` or the
  // on-pod menu, this is just the value a fresh boot starts with.
  // UNVERIFIED against a real sensor/person: GSR sensors vary a lot
  // between individuals and even electrode placement, so treat this as a
  // starting guess to be corrected with `gsr raw`/`gsr` readings once
  // someone is actually wearing the pod, not a calibrated figure.
  static constexpr uint16_t GSR_DEFAULT_RANGE = 500;
}

// =============================================================================
// DISPLAY / MENU
// =============================================================================
namespace Screen {
  static constexpr uint8_t  WIDTH  = 128;
  static constexpr uint8_t  HEIGHT = 64;

  // How long the beat indicator stays visually "on" after a detected
  // heartbeat, in ms -- long enough to read as a clean flash rather than
  // a flicker, short enough not to still be lit for a fast heart rate's
  // next beat.
  static constexpr uint16_t BEAT_FLASH_MS = 120;

  // How many recent GSR samples the on-screen trend graph keeps (BPM's
  // graph is a live scrolling pulse waveform instead, not a value
  // history -- see drivers/display.h). At one sample per
  // GSR_GRAPH_SAMPLE_MS below, this is roughly a minute of trend.
  static constexpr uint8_t GRAPH_HISTORY_LEN = 60;

  // Monitor-screen (non-menu) redraw rate -- an OLED redraw over I2C
  // isn't free, and nothing here needs updating faster than this to look
  // smooth to a human eye.
  static constexpr uint16_t MONITOR_FRAME_MS = 100; // ~10fps

  // GSR graph sample interval -- deliberately much coarser than the
  // redraw rate above. GSR moves on a timescale of seconds (see
  // gsr_sensor.h), so a new point every ~1s gives GRAPH_HISTORY_LEN
  // samples ~= a full minute of trend, instead of a few seconds of an
  // essentially-flat line if sampled every redraw. BPM's graph has no
  // equivalent constant -- it's a live scrolling pulse waveform (one
  // column per redraw), not a sampled value history.
  static constexpr uint16_t GSR_GRAPH_SAMPLE_MS = 1000;
}

namespace MenuCfg {
  // Step sizes for each adjustable menu item, applied per encoder detent.
  // No poll-rate constant here -- M5Encoder::update() (drivers/m5_encoder.cpp)
  // polls unconditionally every call, unrate-limited, matching the proven
  // reference implementation it was ported from.
  static constexpr uint16_t GSR_RANGE_STEP    = 20;   // counts
  static constexpr uint16_t FIXED_SPEED_STEP  = 50;   // ms
  static constexpr uint8_t  BRIGHTNESS_STEP   = 5;    // 0-255 scale
}

// =============================================================================
// TIMING
// =============================================================================
namespace Timing {
  // Debounce/refractory floor for the pulse ISR. 250ms = a 240bpm cap,
  // comfortably above any real heart rate -- guards against a cheap
  // comparator's output chattering right at an edge and being counted as
  // two beats instead of one.
  static constexpr uint16_t PULSE_REFRACTORY_MS = 250;

  // No edge for this long -> PulseSensor::hasSignal() goes false (earclip
  // not worn / cable fault) and the strip switches to its idle animation.
  static constexpr uint32_t PULSE_SIGNAL_TIMEOUT_MS = 3000;

  static constexpr uint16_t STRIP_FRAME_MS = 20; // ~50fps cap on FastLED.show()

  // GSR changes on a timescale of seconds, not milliseconds -- no need to
  // sample faster than this, and slower sampling means more effective
  // smoothing for the same IIR coefficient (see GsrSensor::tick()).
  static constexpr uint16_t GSR_SAMPLE_MS = 50; // ~20Hz
}