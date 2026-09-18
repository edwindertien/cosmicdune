#include "net_config.h"
#include <string.h>
#include <LittleFS.h>
#include <ArduinoJson.h>

NetConfig netConfig;

namespace NetConfigStore {

static constexpr const char* PATH     = "/netconfig.json";
static constexpr const char* PATH_TMP = "/netconfig.json.tmp";

bool begin() {
  // false = don't format automatically if mount fails silently the first
  // time; formatOnFail below handles the genuine first-ever-boot case
  // (brand new flash, LittleFS superblock absent) -- same shape as the
  // arm-controller project's ConfigStore::begin().
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

bool load(NetConfig& cfg) {
  if (!LittleFS.exists(PATH)) return false;
  File f = LittleFS.open(PATH, "r");
  if (!f) return false;

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, f);
  f.close();
  if (err) {
    Serial.print(F("[CFG] netconfig parse error: "));
    Serial.println(err.c_str());
    return false;
  }

  cfg.podId   = doc["podId"]   | cfg.podId;
  cfg.hubPort = doc["hubPort"] | cfg.hubPort;
  cfg.sendHz  = doc["sendHz"]  | cfg.sendHz;
  cfg.enabled = doc["enabled"] | cfg.enabled;

  const char* ssid = doc["ssid"];
  if (ssid) strncpy(cfg.ssid, ssid, sizeof(cfg.ssid) - 1);
  const char* pass = doc["password"];
  if (pass) strncpy(cfg.password, pass, sizeof(cfg.password) - 1);
  const char* host = doc["hubHost"];
  if (host) strncpy(cfg.hubHost, host, sizeof(cfg.hubHost) - 1);

  return true;
}

bool save(const NetConfig& cfg) {
  JsonDocument doc;
  doc["podId"]    = cfg.podId;
  doc["ssid"]     = cfg.ssid;
  doc["password"] = cfg.password;
  doc["hubHost"]  = cfg.hubHost;
  doc["hubPort"]  = cfg.hubPort;
  doc["sendHz"]   = cfg.sendHz;
  doc["enabled"]  = cfg.enabled;

  File f = LittleFS.open(PATH_TMP, "w");
  if (!f) return false;
  size_t written = serializeJson(doc, f);
  f.close();
  if (written == 0) { LittleFS.remove(PATH_TMP); return false; }

  // Atomic-ish swap: remove the old file, then rename the temp file over
  // it. LittleFS has no atomic rename-replace, so there's a brief window
  // with no /netconfig.json at all -- acceptable since a missing file
  // just falls back to in-RAM defaults on next boot, never a crash or a
  // corrupt half-written file that fails to parse. Same reasoning as the
  // arm-controller project's ConfigStore::save().
  LittleFS.remove(PATH);
  return LittleFS.rename(PATH_TMP, PATH);
}

} // namespace NetConfigStore
