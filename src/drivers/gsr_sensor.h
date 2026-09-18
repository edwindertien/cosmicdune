#pragma once
#include <Arduino.h>

// ---------------------------------------------------------------------------
// GsrSensor -- periodic (non-interrupt) ADC read of a galvanic skin
// response sensor on GP27/ADC1. Unlike the heart-rate sensor, there's
// nothing to trigger off -- GSR is a slow-moving level, not a series of
// discrete events, so this just samples + smooths on its own timer.
//
// There's no universal "sweaty" ADC value -- GSR varies a lot between
// people and even electrode placement/skin moisture on the day. That's
// why baseline and range are both runtime-tunable (CLI `gsr baseline` /
// `gsr range`) rather than compiled constants: expect to recalibrate
// every time a pod goes on a different musician, or even the same one on
// a different day.
// ---------------------------------------------------------------------------
class GsrSensor {
public:
  void begin(uint8_t pin); // blocks for ~0.5s to establish a startup
                             // baseline average -- see .cpp for why
  void tick();              // call every loop() -- samples + smooths on
                             // its own timer, safe to call as often as you like

  uint16_t raw() const { return _smoothed; }  // smoothed ADC counts, 0-4095 (12-bit)
  float    normalized() const;                 // 0.0 (at/below baseline) .. 1.0
                                                 // (baseline + range), clamped

  uint16_t baseline() const { return _baseline; }
  void     setBaseline(uint16_t counts) { _baseline = counts; }
  void     calibrateBaselineNow() { _baseline = _smoothed; } // capture the
                                                                // current smoothed
                                                                // reading as the
                                                                // new "calm" reference

  uint16_t range() const { return _range; }
  void     setRange(uint16_t counts) { _range = (counts == 0) ? 1 : counts; } // guard /0

  void setInverted(bool inv) { _inverted = inv; }
  bool inverted() const { return _inverted; }

private:
  uint8_t  _pin = 0;
  uint16_t _smoothed = 0;
  uint16_t _baseline = 0;
  uint16_t _range    = 500; // overwritten from Config::GSR_DEFAULT_RANGE in begin()
  bool     _inverted = false; // flip with `gsr invert on` if your wiring makes
                                // conductance UP read as ADC counts DOWN
  uint32_t _lastSampleMs = 0;
};

extern GsrSensor gsrSensor;
