#include "gsr_sensor.h"
#include "../config.h"

GsrSensor gsrSensor;

void GsrSensor::begin(uint8_t pin) {
  _pin = pin;
  _range = Config::GSR_DEFAULT_RANGE;
  pinMode(_pin, INPUT);
  analogReadResolution(12); // RP2040's ADC is 12-bit natively; the Arduino
                             // API default is 10-bit for cross-platform
                             // compatibility -- opt into the full range
                             // here since nothing else on this board reads
                             // analog inputs.

  // Establish a startup baseline by averaging ~0.5s of samples --
  // assumes the musician is roughly at rest for that first half-second
  // after power-on, which is a rough starting point at best. Recalibrate
  // any time with the CLI (`gsr baseline set`) once they're actually
  // wearing the pod and settled.
  const uint8_t  kSamples     = 20;
  const uint16_t kSampleGapMs = 25; // 20 x 25ms = 500ms total
  uint32_t sum = 0;
  for (uint8_t i = 0; i < kSamples; ++i) {
    sum += analogRead(_pin);
    delay(kSampleGapMs);
  }
  _smoothed = (uint16_t)(sum / kSamples);
  _baseline = _smoothed;
}

void GsrSensor::tick() {
  uint32_t now = millis();
  if (now - _lastSampleMs < Timing::GSR_SAMPLE_MS) return;
  _lastSampleMs = now;
  uint16_t sample = (uint16_t)analogRead(_pin);
  // Light IIR smoothing -- GSR moves on a timescale of seconds, so heavy
  // smoothing costs no real responsiveness and kills a lot of ADC/
  // electrode-contact noise.
  _smoothed = (uint16_t)((_smoothed * 7UL + sample) / 8UL);
}

float GsrSensor::normalized() const {
  int32_t delta = (int32_t)_smoothed - (int32_t)_baseline;
  if (_inverted) delta = -delta;
  float t = (float)delta / (float)_range;
  return constrain(t, 0.0f, 1.0f);
}
