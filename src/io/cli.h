#pragma once
#include <Arduino.h>

// ---------------------------------------------------------------------------
// Cli -- line-based command shell over USB CDC (same physical USB
// connection as MIDI). Exists for bring-up and bench-testing before any
// hub/wireless link is in place -- `beat <bpm>` in particular lets the LED
// and MIDI paths be exercised with no sensor wired up at all.
// ---------------------------------------------------------------------------
class Cli {
public:
  void begin();
  void tick(); // call every loop() -- non-blocking, reads whatever is available

private:
  void handleLine(const String& line);
  void printHelp();
  void printStatus();
  void streamTick();

  String   _lineBuf;
  bool     _streaming = false;
  uint32_t _lastStreamMs = 0;
};

extern Cli cli;
