#pragma once
#include <Arduino.h>
#include <Wire.h>
#include "../config.h"

// ---------------------------------------------------------------------------
// M5Encoder -- raw I2C driver for the M5Stack Unit Encoder (STM32F030,
// addr 0x40), on I2C1 (GP6/GP7).
//
// Talks to the device's registers directly rather than depending on the
// official m5stack/M5Unit-Encoder Arduino library -- ported from a
// previous project (picoST3215DXL) that already had this exact chip
// working this way, register map and all. That project's own comment on
// the button register says plainly: "Your module uses inverted logic" --
// i.e. this was verified against real hardware, not read off a datasheet,
// which is exactly the class of detail that would otherwise have been an
// unverified guess here.
//
// Register map (confirmed working):
//   0x00  MODE_REG     -- written once at begin() (value 0)
//   0x10  ENCODER_REG  -- 2 bytes, little-endian signed raw count.
//                         Increments by COUNTS_PER_STEP per physical
//                         detent, not by 1 -- readSteps() divides it out.
//   0x20  BUTTON_REG   -- 1 byte, ACTIVE-LOW (0 = pressed)
//   0x30  RGB_LED_REG  -- write [index, R, G, B], 4 bytes
// ---------------------------------------------------------------------------
class M5Encoder {
public:
  void begin();
  void update(); // call every loop() -- polls over I2C1 and updates the
                   // debounced button state machine. Not rate-limited
                   // internally; Menu calls it every tick like the
                   // proven reference does.

  int  readSteps();       // delta detents since the last call, then
                             // resets. Positive = clockwise (unverified
                             // which physical direction that actually is
                             // on this unit -- flip the sign in menu.cpp
                             // if list navigation feels backwards)
  bool wasShortPressed();  // true once per short press (released before
                             // the long-press threshold)
  bool wasLongPressed();   // true once per long press (fires once when
                             // the threshold is crossed, doesn't wait for release)
  bool isPressed() const;  // instantaneous debounced state
  void clearEvents();      // clears accumulated steps + pending press events

  void setLedColor(uint8_t index, uint32_t rgb24); // 0xRRGGBB -- not used
                                                      // yet, available if
                                                      // the menu wants
                                                      // visual feedback later

private:
  bool writeBytes(uint8_t reg, const uint8_t* buffer, uint8_t length);
  bool readBytes(uint8_t reg, uint8_t* buffer, uint8_t length);

  int16_t readRawCount();
  bool    readButtonRaw();

  int16_t _lastRawCount = 0;
  int     _stepAccumulator = 0;

  bool _lastRawButton = false;
  bool _stableButton  = false;
  unsigned long _lastDebounceMs = 0;

  unsigned long _pressStartMs = 0;
  bool _longPressFired = false;
  bool _waitForReleaseAfterLong = false;

  bool _shortPressEvent = false;
  bool _longPressEvent  = false;
  unsigned long _lastEventMs = 0;

  static constexpr uint8_t MODE_REG    = 0x00;
  static constexpr uint8_t ENCODER_REG = 0x10;
  static constexpr uint8_t BUTTON_REG  = 0x20;
  static constexpr uint8_t RGB_LED_REG = 0x30;

  static constexpr unsigned long DEBOUNCE_MS  = 30;
  static constexpr unsigned long LONGPRESS_MS = 700;
  static constexpr unsigned long EVENT_GAP_MS = 120;
  static constexpr int COUNTS_PER_STEP = 2;
};

extern M5Encoder m5Encoder;
