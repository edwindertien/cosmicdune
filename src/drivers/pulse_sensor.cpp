#include "pulse_sensor.h"
#include "../config.h"

PulseSensor* PulseSensor::_instance = nullptr;
PulseSensor  pulseSensor;

void PulseSensor::begin(uint8_t pin) {
  _pin = pin;
  _instance = this;
  // See config.h HW::PULSE_PIN for the polarity assumption (idle HIGH,
  // FALLING on a beat) and how to flip it if beats don't register.
  pinMode(_pin, INPUT);
  attachInterrupt(digitalPinToInterrupt(_pin), isrTrampoline, FALLING);
}

void PulseSensor::isrTrampoline() {
  if (_instance) _instance->handleEdge();
}

void PulseSensor::handleEdge() {
  uint32_t now = micros();
  if (_isrLastEdgeUs == 0) {
    // First edge since boot -- no previous timestamp to measure an
    // interval against, so just establish the baseline. Without this
    // check the first beat would compute an "IBI" of micros()-since-boot,
    // producing one bogus near-zero BPM reading before settling.
    _isrLastEdgeUs = now;
    return;
  }
  uint32_t sinceLast = now - _isrLastEdgeUs; // unsigned subtraction handles
                                               // micros() rollover correctly
  if (sinceLast < (uint32_t)Timing::PULSE_REFRACTORY_MS * 1000UL) return; // debounce
  _isrIbiUs = sinceLast;
  _isrLastEdgeUs = now;
  _isrNewBeat = true;
}

void PulseSensor::tick() {
  if (!_isrNewBeat) return;
  noInterrupts();
  uint32_t ibiUs = _isrIbiUs;
  _isrNewBeat = false;
  interrupts();

  _lastBeatMs = millis();
  _lastIbiMs  = ibiUs / 1000UL;
  if (_lastIbiMs > 0) {
    uint16_t instantBpm = (uint16_t)(60000UL / _lastIbiMs);
    // Light IIR smoothing -- a single stray IBI (sensor twitch, earclip
    // shifting) shouldn't visibly jump the LED colour or MIDI CC; a real
    // BPM change settles into the average within a few beats.
    _bpm = (_bpm == 0) ? instantBpm : (uint16_t)((_bpm * 3 + instantBpm) / 4);
    if (_onBeat) _onBeat(_bpm, _lastIbiMs);
  }
}

bool PulseSensor::hasSignal() const {
  return (_lastBeatMs != 0) && (millis() - _lastBeatMs < Timing::PULSE_SIGNAL_TIMEOUT_MS);
}
