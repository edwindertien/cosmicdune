#pragma once
#include <Arduino.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SH110X.h>
#include "../config.h"

// ---------------------------------------------------------------------------
// Display -- M5Stack "Unit OLED" (SH1107 controller, 128x64, addr 0x3C) on
// I2C0 (GP8/GP9). Two things use this screen: the normal monitor view
// (beat-flash indicator + a faux heartbeat waveform/BPM number + GSR
// graph/number + WiFi/IP status, drawn here), and io/menu.h's settings
// menu (drawn there, using this class's low-level primitives so both
// views share one small vocabulary instead of each reaching into the raw
// Adafruit_SH1107 object separately).
// ---------------------------------------------------------------------------
class Display {
public:
  void begin(); // brings up I2C0 and the panel

  void notifyBeat(); // call once per detected heartbeat -- flashes the
                       // beat indicator and restarts the pulse-waveform
                       // blip from the beginning (see below)

  void renderMonitor(const String& ipText, bool wifiConnected, bool hasSignal,
                      uint16_t bpm, uint16_t gsrRaw, float gsrNormalized);
                      // call every loop() when the menu isn't active --
                      // rate-limited internally to Screen::MONITOR_FRAME_MS.
                      // Also advances the pulse waveform by one column and
                      // pushes a new GSR graph sample on its own (much
                      // coarser) timer -- see Screen::GSR_GRAPH_SAMPLE_MS.

  // Low-level primitives -- io/menu.cpp draws its own layout with these
  // rather than duplicating Adafruit_GFX's whole API surface here.
  void    clear() { _panel.clearDisplay(); }
  void    text(int16_t x, int16_t y, const String& s, uint8_t size = 1);
  void    show() { _panel.display(); }
  Adafruit_SH1107& raw() { return _panel; }

private:
  // Adafruit_SH1107's constructor is documented as (width, height, ...),
  // but this exact panel needs (64, 128) -- height first -- paired with
  // setRotation(1) in begin(). Confirmed against a previous project
  // (picoST3215DXL) already running this exact OLED unit this way: the
  // SH1107 controller's native RAM is addressed as 64 columns x 128 rows
  // ("portrait"), and setRotation(1) is what presents it to Adafruit_GFX
  // (and everything in this file) as the physical 128x64 landscape panel.
  // Passing (128, 64) here, or skipping the setRotation(1) in begin(),
  // both produce a wrapped/corrupted image -- not a typo to "simplify".
  Adafruit_SH1107 _panel{Screen::HEIGHT, Screen::WIDTH, &Wire, -1};

  // "Faux" heartbeat trace -- there's no raw analog waveform from this
  // sensor (it's a digital comparator, see pulse_sensor.h), so this isn't
  // real PPG/ECG data. It's a small stylized blip, injected at the right
  // edge and scrolled left, once per REAL detected beat -- the timing is
  // genuine even though the shape is synthetic, same idea as a bedside
  // monitor's sweep display. One buffer slot per pixel column (matches
  // the graph box width below) avoids needing any interpolation.
  static constexpr uint8_t PULSE_WAVE_LEN = 84;
  static constexpr int8_t  PULSE_SHAPE[] = {0, 1, -2, 8, -6, 2, -1, 0};
  static constexpr uint8_t PULSE_SHAPE_LEN = sizeof(PULSE_SHAPE) / sizeof(PULSE_SHAPE[0]);

  int8_t  _pulseWave[PULSE_WAVE_LEN] = {0}; // height offset from baseline,
                                              // per column, oldest at [0]
  uint8_t _pulseShapeIndex = PULSE_SHAPE_LEN; // >= PULSE_SHAPE_LEN means
                                                // "inactive, emit baseline
                                                // (0) on the next scroll"

  float    _gsrHistory[Screen::GRAPH_HISTORY_LEN] = {0};
  uint8_t  _gsrHistoryCount = 0;
  uint8_t  _gsrHistoryHead  = 0;
  uint32_t _lastGsrPushMs   = 0;

  uint32_t _beatFlashUntilMs = 0;
  uint32_t _lastFrameMs = 0;

  void drawBeatIndicator(bool hasSignal);
  void drawPulseWave(uint16_t bpm);
  void drawGsrGraph(uint16_t gsrRaw);
  void scrollPulseWave(); // advances the trace by one column -- called
                            // once per renderMonitor() redraw, not on its
                            // own separate timer (see .cpp)
};

extern Display display;