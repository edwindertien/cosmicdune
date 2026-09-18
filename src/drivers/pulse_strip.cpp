#include "pulse_strip.h"
#include <math.h>

PulseStrip pulseStrip;

void PulseStrip::begin() {
  // WS2812B assumed for colour order (GRB) and timing. If colours come out
  // swapped (e.g. red/green flipped) on the real strip, that's a chipset/
  // colour-order mismatch, not a wiring fault -- try WS2812 or SK6812
  // here first. See platformio.ini for the FastLED-vs-Adafruit_NeoPixel
  // note (FastLED's RP2040 PIO driver is what keeps this LED write from
  // stalling the pulse-sensor ISR).
  FastLED.addLeds<WS2812B, HW::STRIP_PIN, GRB>(_leds, HW::STRIP_NUM_LEDS);
  FastLED.setBrightness(HW::STRIP_DEFAULT_BRIGHTNESS);
  FastLED.clear(true);
}

CRGB PulseStrip::gsrToColor(float t) {
  // Fixed 3-segment gradient across GsrSensor's already-normalized 0..1
  // range (baseline/range scaling happens there, not here): white (calm)
  // -> blue -> purple -> red (most "sweaty"), each third an even lerp.
  t = constrain(t, 0.0f, 1.0f);
  CRGB  a, b;
  float segT;
  if (t < 1.0f / 3.0f) {
    a = CRGB::White;
    b = CRGB(60, 120, 255); // blue
    segT = t / (1.0f / 3.0f);
  } else if (t < 2.0f / 3.0f) {
    a = CRGB(60, 120, 255);  // blue
    b = CRGB(160, 30, 220);  // purple
    segT = (t - 1.0f / 3.0f) / (1.0f / 3.0f);
  } else {
    a = CRGB(160, 30, 220); // purple
    b = CRGB::Red;
    segT = (t - 2.0f / 3.0f) / (1.0f / 3.0f);
  }
  return blend(a, b, (uint8_t)(segT * 255));
}

void PulseStrip::trigger(uint16_t bpm, float gsrLevel) {
  // Find a free slot; if all MAX_PULSES are in flight (BPM implausibly
  // fast for the strip's traversal time), steal the oldest one rather
  // than silently dropping the new beat.
  int      slot       = -1;
  int      oldestSlot = 0;
  uint32_t oldestStart = 0xFFFFFFFFUL;
  for (int i = 0; i < MAX_PULSES; ++i) {
    if (!_pulses[i].active) { slot = i; break; }
    if (_pulses[i].startMs < oldestStart) { oldestStart = _pulses[i].startMs; oldestSlot = i; }
  }
  if (slot < 0) slot = oldestSlot;

  Pulse& p = _pulses[slot];
  p.active      = true;
  p.positionLed = 0;
  p.startMs     = millis();
  p.color       = gsrToColor(gsrLevel);

  uint32_t traversalMs;
  if (_mode == PulseMode::FIXED_SPEED) {
    traversalMs = _fixedTraversalMs;
  } else {
    // IBI_SYNCED: one full strip traversal takes exactly one IBI, so a
    // steady heart rate reads as one continuous, evenly-spaced travelling
    // wave rather than pulses bunching up or spreading apart.
    traversalMs = (bpm > 0) ? (60000UL / bpm) : 1000UL;
  }
  p.speedLedPerMs = (float)HW::STRIP_NUM_LEDS / (float)traversalMs;
}

void PulseStrip::tick() {
  uint32_t now  = millis();
  uint32_t dtMs = now - _lastTickMs;
  if (dtMs < Timing::STRIP_FRAME_MS) return; // cap frame rate -- FastLED's
                                               // show() takes real time, no
                                               // benefit rendering faster
                                               // than the eye can use
  _lastTickMs = now;

  for (int i = 0; i < MAX_PULSES; ++i) {
    if (!_pulses[i].active) continue;
    _pulses[i].positionLed += _pulses[i].speedLedPerMs * dtMs;
    if (_pulses[i].positionLed - Config::PULSE_TAIL_LEDS > HW::STRIP_NUM_LEDS) {
      _pulses[i].active = false; // fully exited the far end, tail included
    }
  }
  renderFrame();
}

void PulseStrip::renderFrame() {
  fill_solid(_leds, HW::STRIP_NUM_LEDS, CRGB::Black);
  for (int i = 0; i < MAX_PULSES; ++i) {
    if (!_pulses[i].active) continue;
    const Pulse& p = _pulses[i];
    // Comet-style falloff: bright head, exponentially-scaled fading tail
    // behind it, drawn with fractional-position anti-aliasing (splitting
    // brightness between the two nearest integer LEDs) so the pulse reads
    // as smoothly moving light rather than discrete per-LED steps.
    for (int t = 0; t <= Config::PULSE_TAIL_LEDS; ++t) {
      float ledPos = p.positionLed - t;
      if (ledPos < 0 || ledPos >= HW::STRIP_NUM_LEDS) continue;
      uint8_t fade = 255 - (uint8_t)((255 * t) / Config::PULSE_TAIL_LEDS);
      int   idx  = (int)ledPos;
      float frac = ledPos - idx;

      CRGB base = p.color;
      base.nscale8_video(fade);

      CRGB head = base;
      head.nscale8_video((uint8_t)((1.0f - frac) * 255));
      _leds[idx] += head;

      if (idx + 1 < HW::STRIP_NUM_LEDS) {
        CRGB tail = base;
        tail.nscale8_video((uint8_t)(frac * 255));
        _leds[idx + 1] += tail;
      }
    }
  }
  FastLED.show();
}

void PulseStrip::idle() {
  uint32_t now = millis();
  if (now - _lastTickMs < Timing::STRIP_FRAME_MS) return;
  _lastTickMs = now;
  float   phase = (now % Config::IDLE_BREATH_PERIOD_MS) / (float)Config::IDLE_BREATH_PERIOD_MS;
  uint8_t level = (uint8_t)(40 + 40 * sinf(phase * 2.0f * PI));
  fill_solid(_leds, HW::STRIP_NUM_LEDS, CRGB(level, level, level));
  FastLED.show();
}
