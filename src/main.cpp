#include <Arduino.h>
#include "config.h"
#include "drivers/pulse_sensor.h"
#include "drivers/gsr_sensor.h"
#include "drivers/pulse_strip.h"
#include "core/net_config.h"
#include "net/osc_link.h"
#include "io/cli.h"

// ---------------------------------------------------------------------------
// cosmic dune -- sensor pod firmware, iteration 1d:
//   Grove ear-clip pulse (GP9) sets the travelling pulse's speed/tempo;
//   GSR (GP27) sets its colour (white -> blue -> purple -> red). Both
//   render on the 250-LED strip (GP6). Telemetry (bpm/ibi/signal/gsr) goes
//   out over WiFi as OSC to a hub, once configured via the CLI's `net`
//   commands and persisted to LittleFS (see core/net_config.*) -- WiFi is
//   entirely optional and never blocks the pod's core sensing/LED loop.
//   USB MIDI/TinyUSB is still out for now -- see the CLI-not-responding
//   debugging earlier -- plain Serial remains the core's default USB CDC.
// ---------------------------------------------------------------------------

static void onBeat(uint16_t bpm, uint32_t ibiMs) {
  (void)ibiMs;
  pulseStrip.trigger(bpm, gsrSensor.normalized());
}

void setup() {
  Serial.begin(115200);
  // NOTE: no delay-and-wait-for-mount loop here on purpose (that was the
  // TinyUSB/MIDI-specific dance) -- plain Serial.begin() on this core
  // doesn't need one. If "[BOOT] ..." below still never appears on a
  // freshly opened serial monitor, that in itself is useful information:
  // it'd point at something more fundamental than CLI logic (wrong COM
  // port, board not actually flashed, or a crash before setup() even
  // gets here) rather than anything in this file.
  delay(50);
  Serial.println(F("=== cosmic dune sensor pod booting ==="));

  cli.begin();

  Serial.println(F("[BOOT] arming pulse sensor on GP9..."));
  pulseSensor.begin(HW::PULSE_PIN);
  pulseSensor.setOnBeat(onBeat);
  Serial.println(F("[BOOT] pulse sensor armed"));

  Serial.println(F("[BOOT] calibrating GSR baseline on GP27 (~0.5s, hold still)..."));
  gsrSensor.begin(HW::GSR_PIN);
  Serial.print(F("[BOOT] GSR baseline=")); Serial.println(gsrSensor.baseline());

  Serial.println(F("[BOOT] starting LED strip (FastLED, GP6, 250 LEDs)..."));
  pulseStrip.begin();
  Serial.println(F("[BOOT] LED strip ready"));
  // If "[BOOT] LED strip ready" never appears, FastLED.addLeds() (in
  // pulse_strip.cpp) is hanging/panicking -- comment out the
  // pulseStrip.begin() line above and reflash to confirm.

  Serial.println(F("[BOOT] mounting LittleFS for network config..."));
  NetConfigStore::begin();
  if (NetConfigStore::load(netConfig)) {
    Serial.println(F("[BOOT] loaded /netconfig.json"));
  } else {
    Serial.println(F("[BOOT] no /netconfig.json yet -- WiFi stays off until "
                      "configured (see CLI 'net') and saved ('net save')"));
  }
  oscLink.begin();

  Serial.println(F("=== pod ready -- type 'help' ==="));
}

void loop() {
  gsrSensor.tick();   // samples + smooths on its own timer (no-op most
                        // loop() iterations -- see Timing::GSR_SAMPLE_MS)
  pulseSensor.tick(); // drains the beat ISR flag; fires onBeat() when a
                        // new beat has been confirmed
  if (pulseSensor.hasSignal()) {
    pulseStrip.tick();
  } else {
    pulseStrip.idle();
  }
  oscLink.tick(); // (re)connects WiFi on its own timer; a cheap no-op
                   // once connected, or always if `net` is unconfigured
  oscLink.sendTelemetry(pulseSensor.bpm(), pulseSensor.lastIbiMs(),
                         pulseSensor.hasSignal(), gsrSensor.raw(),
                         gsrSensor.normalized()); // rate-limited internally;
                                                    // a no-op if not connected
  cli.tick();
}
