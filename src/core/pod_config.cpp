#include "pod_config.h"
#include <Arduino.h>
#include <LittleFS.h>
#include <ArduinoJson.h>
#include "../drivers/gsr_sensor.h"
#include "../drivers/pulse_strip.h"

namespace PodConfigStore {

static constexpr const char* PATH     = "/podconfig.json";
static constexpr const char* PATH_TMP = "/podconfig.json.tmp";

bool begin() {
  if (!LittleFS.begin()) {
    Serial.println(F("[CFG] LittleFS mount failed, formatting..."));
    LittleFS.format();
    if (!LittleFS.begin()) {
      Serial.println(F("[CFG] LittleFS format+mount failed -- check "
                        "board_build.filesystem_size in platformio.ini"));
      return false;
    }
  }
  return true;
}

bool load() {
  if (!LittleFS.exists(PATH)) return false;
  File f = LittleFS.open(PATH, "r");
  if (!f) return false;

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, f);
  f.close();
  if (err) {
    Serial.print(F("[CFG] podconfig parse error: "));
    Serial.println(err.c_str());
    return false;
  }

  // `doc["key"] | currentValue` -- same fallback idiom as NetConfigStore:
  // a missing key just keeps whatever gsrSensor/pulseStrip already have.
  gsrSensor.setRange(doc["gsrRange"] | gsrSensor.range());
  gsrSensor.setInverted(doc["gsrInverted"] | gsrSensor.inverted());

  uint8_t currentModeVal = (pulseStrip.mode() == PulseMode::FIXED_SPEED) ? 1 : 0;
  uint8_t modeVal = doc["stripMode"] | currentModeVal;
  pulseStrip.setMode(modeVal == 1 ? PulseMode::FIXED_SPEED : PulseMode::IBI_SYNCED);
  pulseStrip.setFixedTraversalMs(doc["fixedTraversalMs"] | pulseStrip.fixedTraversalMs());

  return true;
}

bool save() {
  JsonDocument doc;
  doc["gsrRange"]         = gsrSensor.range();
  doc["gsrInverted"]      = gsrSensor.inverted();
  doc["stripMode"]        = (pulseStrip.mode() == PulseMode::FIXED_SPEED) ? (uint8_t)1 : (uint8_t)0;
  doc["fixedTraversalMs"] = pulseStrip.fixedTraversalMs();

  File f = LittleFS.open(PATH_TMP, "w");
  if (!f) return false;
  size_t written = serializeJson(doc, f);
  f.close();
  if (written == 0) { LittleFS.remove(PATH_TMP); return false; }

  // Same atomic-ish swap reasoning as NetConfigStore::save().
  LittleFS.remove(PATH);
  return LittleFS.rename(PATH_TMP, PATH);
}

} // namespace PodConfigStore
