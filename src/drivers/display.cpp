#include "display.h"

Display display;

void Display::begin() {
  // I2C bus recovery -- clock SCL 9 times before Wire.begin() to free any
  // slave device left holding SDA low from a previous failed transaction.
  // Without this, Wire.begin() on RP2040 can hang indefinitely if the
  // panel was mid-transaction when the MCU was reset. Ported from a
  // previous project (picoST3215DXL) that hit exactly this hang on this
  // same OLED unit.
  pinMode(HW::I2C0_SDA, INPUT_PULLUP);
  pinMode(HW::I2C0_SCL, OUTPUT);
  for (int i = 0; i < 9; ++i) {
    digitalWrite(HW::I2C0_SCL, HIGH); delayMicroseconds(5);
    digitalWrite(HW::I2C0_SCL, LOW);  delayMicroseconds(5);
  }
  // STOP condition: SDA low -> SDA high while SCL high
  pinMode(HW::I2C0_SDA, OUTPUT);
  digitalWrite(HW::I2C0_SDA, LOW);
  digitalWrite(HW::I2C0_SCL, HIGH); delayMicroseconds(5);
  digitalWrite(HW::I2C0_SDA, HIGH); delayMicroseconds(5);

  Wire.setSDA(HW::I2C0_SDA);
  Wire.setSCL(HW::I2C0_SCL);
  Wire.begin();
  Wire.setClock(HW::I2C_HZ);

  _panel.begin(HW::DISPLAY_I2C_ADDR, true);
  // Presents the panel's native 64x128 "portrait" RAM as 128x64
  // landscape -- see the constructor comment in display.h. This is not
  // about which way the unit is physically mounted; if it ever needs
  // flipping for mounting orientation, add 2 to this (1 -> 3), don't
  // replace it with 0 or 2.
  _panel.setRotation(1);
  clear();
  show();
}

void Display::text(int16_t x, int16_t y, const String& s, uint8_t size) {
  _panel.setTextSize(size);
  _panel.setTextColor(SH110X_WHITE);
  _panel.setCursor(x, y);
  _panel.print(s);
}

void Display::notifyBeat() {
  _beatFlashUntilMs = millis() + Screen::BEAT_FLASH_MS;
  // Restarts the blip from its first sample -- whatever was left of any
  // still-in-flight previous blip (only possible at a heart rate faster
  // than PULSE_SHAPE_LEN columns' worth of redraws, well above anything
  // physiological) gets cut short in favour of the new beat, which is
  // the right priority.
  _pulseShapeIndex = 0;
}

void Display::drawBeatIndicator(bool hasSignal) {
  const int16_t cx = 8, cy = 8, r = 6;
  if (!hasSignal) {
    // Hollow circle, no fill ever -- distinguishes "no sensor" from a
    // beat simply not having happened in the last flash window yet.
    _panel.drawCircle(cx, cy, r, SH110X_WHITE);
    return;
  }
  bool flashing = millis() < _beatFlashUntilMs;
  if (flashing) {
    _panel.fillCircle(cx, cy, r, SH110X_WHITE);
  } else {
    _panel.drawCircle(cx, cy, r, SH110X_WHITE);
  }
}

void Display::scrollPulseWave() {
  // Shift every column one step toward the oldest end, then write the
  // newest value in at the end -- a plain left-scroll. Whatever's left
  // of the current blip (if any) is what gets written; once
  // _pulseShapeIndex reaches PULSE_SHAPE_LEN, new columns are flat
  // baseline (0) until the next real beat calls notifyBeat().
  for (uint8_t i = 0; i + 1 < PULSE_WAVE_LEN; ++i) _pulseWave[i] = _pulseWave[i + 1];

  int8_t newVal = 0;
  if (_pulseShapeIndex < PULSE_SHAPE_LEN) {
    newVal = PULSE_SHAPE[_pulseShapeIndex];
    _pulseShapeIndex++;
  }
  _pulseWave[PULSE_WAVE_LEN - 1] = newVal;
}

void Display::drawPulseWave(uint16_t bpm) {
  // Number sits between the beat circle (x 0-14) and the graph box below.
  text(18, 4, String(bpm));

  const int16_t x0 = 42, y0 = 2, w = 84, h = 18; // PULSE_WAVE_LEN matches
                                                    // w exactly -- one
                                                    // buffer slot per
                                                    // pixel column, no
                                                    // interpolation needed
  _panel.drawRect(x0 - 1, y0 - 1, w + 2, h + 2, SH110X_WHITE);

  int16_t midY = y0 + h / 2;
  int16_t prevX = -1, prevY = -1;
  for (uint8_t i = 0; i < PULSE_WAVE_LEN; ++i) {
    int16_t px = (int16_t)(x0 + i);
    int16_t py = (int16_t)constrain(midY - _pulseWave[i], y0, y0 + h - 1);
    if (prevX >= 0) _panel.drawLine(prevX, prevY, px, py, SH110X_WHITE);
    prevX = px;
    prevY = py;
  }
}

void Display::drawGsrGraph(uint16_t gsrRaw) {
  // Small (size-1) raw ADC count next to the graph, same treatment as
  // the BPM number above it.
  text(0, 28, String(gsrRaw), 1);

  const int16_t x0 = 42, y0 = 24, w = 84, h = 18; // same width as the
                                                     // pulse-wave box, directly below it
  _panel.drawRect(x0 - 1, y0 - 1, w + 2, h + 2, SH110X_WHITE);
  if (_gsrHistoryCount < 2) return;

  int16_t prevX = -1, prevY = -1;
  for (uint8_t i = 0; i < _gsrHistoryCount; ++i) {
    uint8_t idx = (uint8_t)((_gsrHistoryHead + Screen::GRAPH_HISTORY_LEN - _gsrHistoryCount + i) % Screen::GRAPH_HISTORY_LEN);

    float clamped = constrain(_gsrHistory[idx], 0.0f, 1.0f); // already
                                                                // normalized
                                                                // 0..1 by
                                                                // GsrSensor
    int16_t py = (int16_t)(y0 + (h - 1) - (int16_t)(clamped * (h - 1)));
    int16_t px = (int16_t)(x0 + ((uint32_t)i * (w - 1)) / (Screen::GRAPH_HISTORY_LEN - 1));

    if (prevX >= 0) _panel.drawLine(prevX, prevY, px, py, SH110X_WHITE);
    prevX = px;
    prevY = py;
  }
}

void Display::renderMonitor(const String& ipText, bool wifiConnected, bool hasSignal,
                             uint16_t bpm, uint16_t gsrRaw, float gsrNormalized) {
  uint32_t now = millis();
  if (now - _lastFrameMs < Screen::MONITOR_FRAME_MS) return;
  _lastFrameMs = now;

  // Pulse wave advances exactly one column per actual redraw -- no
  // separate scroll timer, since a faster internal tick would never be
  // seen anyway (nothing's shown between redraws). At MONITOR_FRAME_MS
  // (100ms), PULSE_WAVE_LEN columns take ~8.4s to fully sweep across,
  // a normal bedside-monitor-like pace.
  scrollPulseWave();

  // GSR graph sample -- its own, much coarser timer (see config.h's
  // Screen::GSR_GRAPH_SAMPLE_MS) since GSR moves far slower than this
  // function gets called.
  if (now - _lastGsrPushMs >= Screen::GSR_GRAPH_SAMPLE_MS) {
    _lastGsrPushMs = now;
    _gsrHistory[_gsrHistoryHead] = gsrNormalized;
    _gsrHistoryHead = (uint8_t)((_gsrHistoryHead + 1) % Screen::GRAPH_HISTORY_LEN);
    if (_gsrHistoryCount < Screen::GRAPH_HISTORY_LEN) _gsrHistoryCount++;
  }

  clear();
  drawBeatIndicator(hasSignal);
  drawPulseWave(bpm);
  drawGsrGraph(gsrRaw);
  text(0, 56, wifiConnected ? ipText : String(F("no wifi")));
  show();
}