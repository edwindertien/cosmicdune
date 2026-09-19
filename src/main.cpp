#include <Arduino.h>
#include "config.h"
#include "drivers/pulse_sensor.h"
#include "drivers/gsr_sensor.h"
#include "drivers/pulse_strip.h"
#include "drivers/display.h"
#include "core/net_config.h"
#include "core/pod_config.h"
#include "net/osc_link.h"
#include "io/cli.h"
#include "io/menu.h"

// ---------------------------------------------------------------------------
// cosmic dune -- sensor pod firmware, iteration 1e:
//   Grove ear-clip pulse (GP26) sets the travelling pulse's speed/tempo;
//   GSR (GP27) sets its colour (white -> blue -> purple -> red). Both
//   render on the 250-LED strip (GP10). Telemetry (bpm/ibi/signal/gsr)
//   goes out over WiFi as OSC to a hub, once configured via the CLI's
//   `net` commands. An SH1107 OLED (I2C0, GP8/GP9) shows a live
//   beat-flash + BPM graph + WiFi status, and an M5Stack encoder (I2C1,
//   GP6/GP7) drives an on-pod settings menu for GSR/strip settings --
//   see io/menu.h for exactly what's covered there vs. CLI-only. The USB
//   CLI keeps working unmodified alongside all of this.
// ---------------------------------------------------------------------------

static void onBeat(uint16_t bpm, uint32_t ibiMs) {
  (void)ibiMs;
  pulseStrip.trigger(bpm, gsrSensor.normalized());
  display.notifyBeat();
}

void setup() {
  Serial.begin(115200);
  delay(50);
  Serial.println(F("=== cosmic dune sensor pod booting ==="));

  cli.begin();

  Serial.println(F("[BOOT] arming pulse sensor on GP26..."));
  pulseSensor.begin(HW::PULSE_PIN);
  pulseSensor.setOnBeat(onBeat);
  Serial.println(F("[BOOT] pulse sensor armed"));

  Serial.println(F("[BOOT] calibrating GSR baseline on GP27 (~0.5s, hold still)..."));
  gsrSensor.begin(HW::GSR_PIN);
  Serial.print(F("[BOOT] GSR baseline=")); Serial.println(gsrSensor.baseline());

  Serial.println(F("[BOOT] starting LED strip (FastLED, GP10, 250 LEDs)..."));
  pulseStrip.begin();
  Serial.println(F("[BOOT] LED strip ready"));
  // If "[BOOT] LED strip ready" never appears, FastLED.addLeds() (in
  // pulse_strip.cpp) is hanging/panicking -- comment out the
  // pulseStrip.begin() line above and reflash to confirm.

  Serial.println(F("[BOOT] starting OLED display (I2C0, GP8/GP9)..."));
  display.begin();
  Serial.println(F("[BOOT] display ready"));

  Serial.println(F("[BOOT] starting menu + encoder (I2C1, GP6/GP7)..."));
  menu.begin();
  Serial.println(F("[BOOT] encoder armed -- press it to open the on-pod menu"));
  // If either of the two lines above never completes, the encoder
  // library's begin() overload is the prime suspect -- see
  // drivers/m5_encoder.h's header comment for exactly what's unverified there.

  // PodConfigStore::load() runs AFTER the sensor/strip begin() calls
  // above so it overrides their compiled-in defaults, not the other way
  // around -- GSR baseline itself is untouched either way (see
  // pod_config.h for why it's not persisted).
  PodConfigStore::begin();
  if (PodConfigStore::load()) {
    Serial.println(F("[BOOT] loaded /podconfig.json (GSR range/invert, strip mode/speed)"));
  } else {
    Serial.println(F("[BOOT] no /podconfig.json yet -- using config.h defaults "
                      "(see CLI/menu 'save' to persist current gsr/mode settings)"));
  }

  Serial.println(F("[BOOT] mounting LittleFS for network config..."));
  NetConfigStore::begin();
  if (NetConfigStore::load(netConfig)) {
    Serial.println(F("[BOOT] loaded /netconfig.json"));
  } else {
    Serial.println(F("[BOOT] no /netconfig.json yet -- WiFi stays off until "
                      "configured (see CLI 'net') and saved ('net save')"));
  }
  oscLink.begin();

  Serial.println(F("=== pod ready -- type 'help', or press the encoder for the menu ==="));
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

  menu.tick(); // always -- polls the encoder (incl. detecting the button
                // press that opens the menu) and, once open, handles its
                // own input + drawing every call
  if (!menu.isOpen()) {
    // The menu owns the screen while open; this is the normal view the
    // rest of the time. Only ever shows the pod's OWN IP (from WiFi.
    // localIP() via oscLink), not the hub's -- there's no reason for a
    // pod to know the hub's address, only the other way around.
    String ipText = oscLink.connected() ? oscLink.localIp().toString() : String();
    display.renderMonitor(ipText, oscLink.connected(), pulseSensor.hasSignal(),
                           pulseSensor.bpm(), gsrSensor.raw(), gsrSensor.normalized());
  }

  cli.tick();
}