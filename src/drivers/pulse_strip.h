#pragma once
#include <Arduino.h>
#include <FastLED.h>
#include "../config.h"

// ---------------------------------------------------------------------------
// PulseStrip -- renders travelling "comet"s of light on the 250-LED strip,
// one per heartbeat. Two independent signals drive it: heart rate (BPM)
// sets motion/tempo (see the timing modes below), and GSR ("sweatiness",
// see drivers/gsr_sensor.h) sets each pulse's colour -- white (calm) ->
// blue -> purple -> red (most aroused), fixed thirds across GsrSensor's
// already-normalized 0..1 output. Up to MAX_PULSES can be in flight at
// once.
//
// Two timing modes, switchable at runtime (CLI `mode fixed|ibi`):
//   IBI_SYNCED  (default) -- one full strip traversal takes exactly one
//               IBI, so a steady heart rate always reads as the same "one
//               continuous, evenly-spaced wave" animation, whatever the
//               actual BPM is. At a fast heart rate the wave just moves
//               faster; pulses don't overlap unless BPM is rising quickly.
//   FIXED_SPEED -- every pulse takes the same configured time (CLI `mode
//               speed <ms>`, default Config::FIXED_SPEED_TRAVERSAL_MS) to
//               cross the strip, regardless of BPM. At higher heart rates,
//               beats arrive faster than one traversal finishes, so
//               multiple pulses end up travelling the strip at once
//               (MAX_PULSES concurrent slots) instead of the strip's
//               apparent speed changing with heart rate.
// ---------------------------------------------------------------------------
enum class PulseMode : uint8_t { IBI_SYNCED, FIXED_SPEED };

class PulseStrip {
public:
  void begin();
  void trigger(uint16_t bpm, float gsrLevel); // call once per detected
                                                // heartbeat -- starts a new
                                                // travelling pulse. bpm sets
                                                // its speed (see PulseMode),
                                                // gsrLevel (0..1, from
                                                // GsrSensor::normalized())
                                                // sets its colour
  void tick();  // call every loop() when PulseSensor::hasSignal() is true --
                // advances + renders + FastLED.show(), rate-limited internally
  void idle();  // call every loop() instead of tick() when hasSignal() is
                // false -- slow neutral breathing so "no signal" reads as
                // "waiting", not "broken"
  void setBrightness(uint8_t b) { FastLED.setBrightness(b); }

  void      setMode(PulseMode m) { _mode = m; }
  PulseMode mode() const { return _mode; }

  void     setFixedTraversalMs(uint32_t ms) { _fixedTraversalMs = ms; }
  uint32_t fixedTraversalMs() const { return _fixedTraversalMs; }

private:
  // Sized for FIXED_SPEED mode at a fast heart rate outrunning a slow
  // configured traversal (e.g. 180bpm / 333ms IBI against a 1500ms
  // traversal implies ~5 pulses in flight at once) -- if this is ever
  // exceeded anyway, trigger() recycles the oldest pulse rather than
  // dropping the new beat, so it degrades gracefully either way.
  static constexpr uint8_t MAX_PULSES = 8;

  struct Pulse {
    bool     active = false;
    float    positionLed   = 0; // fractional LED index, head of the pulse
    float    speedLedPerMs = 0; // set at trigger() from the active mode
    CRGB     color;
    uint32_t startMs = 0;
  };

  CRGB      _leds[HW::STRIP_NUM_LEDS];
  Pulse     _pulses[MAX_PULSES];
  uint32_t  _lastTickMs = 0;
  PulseMode _mode = PulseMode::IBI_SYNCED;
  uint32_t  _fixedTraversalMs = Config::FIXED_SPEED_TRAVERSAL_MS;

  static CRGB gsrToColor(float t);
  void renderFrame();
};

extern PulseStrip pulseStrip;
