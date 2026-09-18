#pragma once
#include <Arduino.h>

// ---------------------------------------------------------------------------
// PulseSensor -- interrupt-driven beat detection for the Grove Ear-clip
// Heart Rate Sensor (digital comparator output, not the analog
// PulseSensor-Amped module -- no ADC or peak-detection algorithm needed,
// just edge timing + debounce).
//
// The ISR does the bare minimum (timestamp + software debounce) and sets a
// flag; all BPM math and the beat callback happen in tick(), from normal
// loop() context. This matters because the callback is expected to do
// things an ISR must not (FastLED.show() via the PIO driver, USB MIDI
// sends, Serial prints) -- calling any of those directly from
// attachInterrupt() context would be unsafe.
// ---------------------------------------------------------------------------
using PulseBeatCallback = void (*)(uint16_t bpm, uint32_t ibiMs);

class PulseSensor {
public:
  void begin(uint8_t pin);
  void tick(); // call every loop() -- drains the ISR's beat flag, updates
               // BPM, and invokes the beat callback (if any) outside ISR context
  void setOnBeat(PulseBeatCallback cb) { _onBeat = cb; }

  uint16_t bpm() const { return _bpm; }
  uint32_t lastIbiMs() const { return _lastIbiMs; }
  bool     hasSignal() const; // false once no beat has arrived for
                                // Timing::PULSE_SIGNAL_TIMEOUT_MS (earclip
                                // not worn / cable fault)

private:
  static void isrTrampoline();
  void handleEdge();

  PulseBeatCallback _onBeat = nullptr;
  uint8_t  _pin = 0;

  // Written by the ISR, read+cleared by tick() -- volatile because they
  // cross the ISR/main boundary.
  volatile uint32_t _isrLastEdgeUs = 0;
  volatile uint32_t _isrIbiUs      = 0;
  volatile bool     _isrNewBeat    = false;

  uint32_t _lastBeatMs = 0;
  uint32_t _lastIbiMs  = 0;
  uint16_t _bpm        = 0;

  static PulseSensor* _instance; // attachInterrupt needs a free function
};

extern PulseSensor pulseSensor;
