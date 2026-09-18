#pragma once
#include <Arduino.h>

// =============================================================================
// HARDWARE CONFIGURATION -- cosmic dune sensor pod, Raspberry Pi Pico W
//
// No display, no buttons -- all interaction is CLI over USB CDC (plain
// Serial for now -- see platformio.ini for why TinyUSB/MIDI is out
// temporarily).
// =============================================================================
namespace HW {
  // Grove Ear-clip Heart Rate Sensor: onboard comparator, DIGITAL output
  // (idles HIGH, pulses LOW on each detected beat) -- this is NOT the
  // analog PulseSensor-Amped module, so there's no ADC/peak-detection here,
  // just edge timing. GP9 is a plain digital GPIO, which is correct for
  // this sensor (it wouldn't be usable on an ADC-only pin anyway).
  // NOTE (unverified against the physical module -- confirm with `beat`
  // vs. a live earclip and the `status` command): if beats never register,
  // or register at roughly double a manually-checked pulse, the edge
  // polarity in drivers/pulse_sensor.cpp (currently FALLING) is the first
  // thing to flip to RISING.
  static constexpr uint8_t PULSE_PIN = 9; // GP9

  // GSR (galvanic skin response) sensor -- analog, GP27/ADC1. Sets the
  // travelling pulse's colour (see Config::gsrToColor's white->blue->
  // purple->red gradient in pulse_strip.cpp); heart rate (PULSE_PIN)
  // still drives motion/tempo, independently.
  static constexpr uint8_t GSR_PIN = 27; // GP27 / ADC1

  // WS2812B-style ("neopixel") strip, 5m @ 50 LED/m.
  static constexpr uint8_t  STRIP_PIN                = 6;   // GP6
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

  // Onboard status LED. On Pico W, LED_BUILTIN is routed through the CYW43
  // WiFi chip by the arduino-pico core (not a plain GPIO) -- digitalWrite()
  // on it still works, but confirm this on your specific core version.
  static constexpr int16_t LED_PIN = LED_BUILTIN;
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
  // pulse_strip.h) -- runtime-tunable via the CLI's `mode speed <ms>`,
  // this is just the value a fresh boot starts with.
  static constexpr uint32_t FIXED_SPEED_TRAVERSAL_MS = 1500;

  // GSR baseline->"fully sweaty" scaling, in raw 12-bit ADC counts
  // (0-4095) -- runtime-tunable via the CLI's `gsr range <counts>`, this
  // is just the value a fresh boot starts with. UNVERIFIED against a real
  // sensor/person: GSR sensors vary a lot between individuals and even
  // electrode placement, so treat this as a starting guess to be
  // corrected with `gsr raw`/`gsr` readings once someone is actually
  // wearing the pod, not a calibrated figure.
  static constexpr uint16_t GSR_DEFAULT_RANGE = 500;
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
