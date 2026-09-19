#include "m5_encoder.h"

M5Encoder m5Encoder;

void M5Encoder::begin() {
  // I2C bus recovery -- see display.cpp's begin() for the full
  // explanation; same technique, applied here since I2C1 is a physically
  // separate bus that can just as easily be left wedged by an abrupt reset.
  pinMode(HW::I2C1_SDA, INPUT_PULLUP);
  pinMode(HW::I2C1_SCL, OUTPUT);
  for (int i = 0; i < 9; ++i) {
    digitalWrite(HW::I2C1_SCL, HIGH); delayMicroseconds(5);
    digitalWrite(HW::I2C1_SCL, LOW);  delayMicroseconds(5);
  }
  pinMode(HW::I2C1_SDA, OUTPUT);
  digitalWrite(HW::I2C1_SDA, LOW);
  digitalWrite(HW::I2C1_SCL, HIGH); delayMicroseconds(5);
  digitalWrite(HW::I2C1_SDA, HIGH); delayMicroseconds(5);

  Wire1.setSDA(HW::I2C1_SDA);
  Wire1.setSCL(HW::I2C1_SCL);
  Wire1.begin();
  Wire1.setClock(HW::I2C_HZ);

  uint8_t mode = 0;
  writeBytes(MODE_REG, &mode, 1);

  _lastRawCount   = readRawCount();
  _lastRawButton  = readButtonRaw();
  _stableButton   = _lastRawButton;
  _lastDebounceMs = millis();
}

bool M5Encoder::writeBytes(uint8_t reg, const uint8_t* buffer, uint8_t length) {
  Wire1.beginTransmission(HW::ENCODER_I2C_ADDR);
  Wire1.write(reg);
  for (uint8_t i = 0; i < length; ++i) Wire1.write(buffer[i]);
  return Wire1.endTransmission() == 0;
}

bool M5Encoder::readBytes(uint8_t reg, uint8_t* buffer, uint8_t length) {
  Wire1.beginTransmission(HW::ENCODER_I2C_ADDR);
  Wire1.write(reg);
  if (Wire1.endTransmission() != 0) return false;
  if (Wire1.requestFrom((int)HW::ENCODER_I2C_ADDR, (int)length) != length) return false;
  for (uint8_t i = 0; i < length; ++i) buffer[i] = Wire1.available() ? Wire1.read() : 0;
  return true;
}

int16_t M5Encoder::readRawCount() {
  uint8_t data[2] = {0, 0};
  if (!readBytes(ENCODER_REG, data, 2)) return _lastRawCount;
  return (int16_t)(data[0] | (data[1] << 8));
}

bool M5Encoder::readButtonRaw() {
  uint8_t data = 0;
  if (!readBytes(BUTTON_REG, &data, 1)) return _stableButton;
  return data == 0; // active-LOW -- confirmed against real hardware, see header
}

void M5Encoder::update() {
  _shortPressEvent = false;
  _longPressEvent  = false;

  int16_t raw = readRawCount();
  int16_t delta = (int16_t)(raw - _lastRawCount);
  _lastRawCount = raw;
  _stepAccumulator += delta;

  bool rawButton = readButtonRaw();
  if (rawButton != _lastRawButton) {
    _lastRawButton = rawButton;
    _lastDebounceMs = millis();
  }
  if ((millis() - _lastDebounceMs) < DEBOUNCE_MS) return;

  if (_stableButton != _lastRawButton) {
    _stableButton = _lastRawButton;
    if (_stableButton) {
      _pressStartMs = millis();
      _longPressFired = false;
    } else {
      if (_waitForReleaseAfterLong) {
        _waitForReleaseAfterLong = false;
      } else if (_pressStartMs != 0 && !_longPressFired &&
                 (millis() - _lastEventMs) > EVENT_GAP_MS) {
        _shortPressEvent = true;
        _lastEventMs = millis();
      }
      _pressStartMs = 0;
      _longPressFired = false;
    }
  }

  if (_stableButton && !_longPressFired && !_waitForReleaseAfterLong &&
      _pressStartMs != 0 && (millis() - _pressStartMs) >= LONGPRESS_MS &&
      (millis() - _lastEventMs) > EVENT_GAP_MS) {
    _longPressEvent = true;
    _longPressFired = true;
    _waitForReleaseAfterLong = true;
    _lastEventMs = millis();
  }
}

int M5Encoder::readSteps() {
  int steps = 0;
  while (_stepAccumulator >= COUNTS_PER_STEP) { _stepAccumulator -= COUNTS_PER_STEP; steps++; }
  while (_stepAccumulator <= -COUNTS_PER_STEP) { _stepAccumulator += COUNTS_PER_STEP; steps--; }
  return steps;
}

bool M5Encoder::wasShortPressed() {
  bool e = _shortPressEvent;
  _shortPressEvent = false;
  return e;
}

bool M5Encoder::wasLongPressed() {
  bool e = _longPressEvent;
  _longPressEvent = false;
  return e;
}

bool M5Encoder::isPressed() const { return _stableButton; }

void M5Encoder::clearEvents() {
  _shortPressEvent = false;
  _longPressEvent  = false;
  _stepAccumulator = 0;
  _pressStartMs = 0;
  _longPressFired = false;
  _lastDebounceMs = millis();
  if (!_stableButton) {
    _lastRawButton = false;
    _waitForReleaseAfterLong = false;
  } else {
    _waitForReleaseAfterLong = true; // still held after a long press -- swallow the upcoming release
  }
}

void M5Encoder::setLedColor(uint8_t index, uint32_t rgb24) {
  uint8_t data[4];
  data[0] = index;
  data[1] = (rgb24 >> 16) & 0xFF;
  data[2] = (rgb24 >> 8) & 0xFF;
  data[3] = rgb24 & 0xFF;
  writeBytes(RGB_LED_REG, data, 4);
}
