#include "osc_link.h"
#include <OSCMessage.h>
#include "../config.h"

OscLink oscLink;

void OscLink::begin() {
  // Deliberately no WiFi connect here -- see tick() -- so begin() never
  // blocks setup(), matching every other module's begin() in this codebase.
}

void OscLink::startConnecting() {
  Serial.print(F("[NET] connecting to '"));
  Serial.print(netConfig.ssid);
  Serial.println(F("'..."));
  // beginNoBlock(), NOT begin(): on this core WiFi.begin() blocks until the
  // join succeeds or fails, freezing all of loop() (LEDs, heartrate,
  // display, CLI) for as long as that takes -- and for a network that
  // isn't there, that's a long time, or for good (see osc_link.h).
  // beginNoBlock() returns right away; tick() then polls WiFi.status().
  // An empty password means "open network" -> pass nullptr rather than
  // an empty WPA passphrase.
  const char* pass = netConfig.password[0] ? netConfig.password : nullptr;
  rp2040.wdt_reset();   // full watchdog window for the call below, in case
                          // a core version still holds the caller for a few
                          // seconds inside it
  WiFi.beginNoBlock(netConfig.ssid, pass);
  rp2040.wdt_reset();
  _connectStartMs = millis();
  _state = NetLinkState::CONNECTING;
}

void OscLink::retry() {
  if (!netConfig.enabled || netConfig.ssid[0] == '\0') {
    Serial.println(F("[NET] retry: not configured -- see CLI 'net ssid'/'net enable'"));
    return;
  }
  startConnecting();
}

void OscLink::tick() {
  if (!netConfig.enabled || netConfig.ssid[0] == '\0') {
    _state = NetLinkState::IDLE; // covers `net disable` while connected/connecting too
    return;
  }

  if (_state == NetLinkState::IDLE) {
    startConnecting(); // fresh enable (boot, or `net enable` after being off)
    return;
  }

  if (_state == NetLinkState::CONNECTING) {
    if (WiFi.status() == WL_CONNECTED) {
      _udp.begin(0); // any local port -- this pod only ever sends
      _state = NetLinkState::CONNECTED;
      Serial.print(F("[NET] WiFi connected, IP="));
      Serial.println(WiFi.localIP());
      return;
    }
    if (millis() - _connectStartMs >= Timing::WIFI_CONNECT_TIMEOUT_MS) {
      _state = NetLinkState::GAVE_UP;
      WiFi.disconnect(); // stop the driver's own background join attempt too --
                          // otherwise "gave up" would only be our bookkeeping,
                          // with the radio still retrying behind it
      rp2040.wdt_reset();
      Serial.println(F("[NET] gave up connecting -- no matching WiFi found, or "
                        "bad credentials. CLI 'net retry' or the menu's 'Net "
                        "Retry' to try again."));
    }
    return;
  }

  if (_state == NetLinkState::CONNECTED) {
    if (WiFi.status() != WL_CONNECTED) {
      Serial.println(F("[NET] WiFi dropped, reconnecting..."));
      startConnecting();
    }
    return;
  }

  // GAVE_UP -- deliberately does nothing automatically here. Only retry()
  // (CLI/menu) moves it back to CONNECTING; see osc_link.h for why an
  // endless auto-retry loop was the thing being fixed.
}

void OscLink::addressFor(char* buf, size_t bufLen, const char* leaf) const {
  snprintf(buf, bufLen, "/pod/%u/%s", netConfig.podId, leaf);
}

void OscLink::sendInt(const IPAddress& hub, const char* addr, int32_t v) {
  OSCMessage msg(addr);
  msg.add(v);
  _udp.beginPacket(hub, netConfig.hubPort);
  msg.send(_udp);
  _udp.endPacket();
  msg.empty();
}

void OscLink::sendFloat(const IPAddress& hub, const char* addr, float v) {
  OSCMessage msg(addr);
  msg.add(v);
  _udp.beginPacket(hub, netConfig.hubPort);
  msg.send(_udp);
  _udp.endPacket();
  msg.empty();
}

void OscLink::sendTelemetry(uint16_t bpm, uint32_t ibiMs, bool hasSignal,
                             uint16_t gsrRaw, float gsrNormalized) {
  if (_state != NetLinkState::CONNECTED) return;

  uint32_t intervalMs = 1000UL / (netConfig.sendHz > 0 ? netConfig.sendHz : 1);
  uint32_t now = millis();
  if (now - _lastSendMs < intervalMs) return;
  _lastSendMs = now;

  IPAddress hub;
  if (!hub.fromString(netConfig.hubHost)) return; // bad/unset hub IP -- see CLI `net hub <ip> <port>`

  char addr[24];
  addressFor(addr, sizeof(addr), "bpm");    sendInt(hub, addr, bpm);
  addressFor(addr, sizeof(addr), "ibi");    sendInt(hub, addr, (int32_t)ibiMs);
  addressFor(addr, sizeof(addr), "signal"); sendInt(hub, addr, hasSignal ? 1 : 0);
  addressFor(addr, sizeof(addr), "gsrRaw"); sendInt(hub, addr, gsrRaw);
  addressFor(addr, sizeof(addr), "gsr");    sendFloat(hub, addr, gsrNormalized);
}