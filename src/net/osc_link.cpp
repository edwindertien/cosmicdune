#include "osc_link.h"
#include <OSCMessage.h>

OscLink oscLink;

void OscLink::begin() {
  // Deliberately no WiFi.begin() here -- see tick() -- so begin() never
  // blocks setup(), matching every other module's begin() in this codebase.
}

void OscLink::tick() {
  if (!netConfig.enabled || netConfig.ssid[0] == '\0') return; // not configured yet -- see CLI `net`

  if (WiFi.status() == WL_CONNECTED) {
    if (!_wifiWasUp) {
      _udp.begin(0); // any local port -- this pod only ever sends
      _wifiWasUp = true;
      Serial.print(F("[NET] WiFi connected, IP="));
      Serial.println(WiFi.localIP());
    }
    return;
  }

  _wifiWasUp = false;
  uint32_t now = millis();
  if (now - _lastConnectAttemptMs < 5000) return; // retry every 5s, never block
  _lastConnectAttemptMs = now;
  Serial.print(F("[NET] connecting to '"));
  Serial.print(netConfig.ssid);
  Serial.println(F("'..."));
  WiFi.begin(netConfig.ssid, netConfig.password);
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
  if (WiFi.status() != WL_CONNECTED) return;

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
