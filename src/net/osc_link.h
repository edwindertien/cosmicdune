#pragma once
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include "../core/net_config.h"

// ---------------------------------------------------------------------------
// OscLink -- connects to WiFi per the global `netConfig` and sends this
// pod's telemetry as individual OSC messages to the configured hub, one
// UDP datagram per value:
//   /pod/<id>/bpm      int   (0 if no signal)
//   /pod/<id>/ibi      int   milliseconds
//   /pod/<id>/signal   int   0 or 1 -- PulseSensor::hasSignal()
//   /pod/<id>/gsrRaw   int   smoothed 12-bit ADC counts
//   /pod/<id>/gsr      float 0.0-1.0 normalized ("sweatiness")
// One-way (pod -> hub) -- nothing here listens for incoming OSC.
//
// Deliberately reads `netConfig` live rather than taking its own copy at
// begin() -- the CLI's `net ...` commands edit that global directly, so a
// snapshot taken once at boot would silently stop reflecting `net enable`,
// `net ssid`, etc. until the next reboot.
//
// Also deliberately non-blocking: connect attempts are retried on a timer
// from tick() rather than blocking setup()/loop() while WiFi comes up,
// since a pod with no WiFi in range (or not yet configured) should still
// run its pulse/GSR/LED pipeline normally -- WiFi telemetry is an add-on,
// not a dependency of the pod's core function.
// ---------------------------------------------------------------------------
class OscLink {
public:
  void begin(); // no config to store (see above) -- exists only so setup()
                 // calls every module's begin() the same way; does not block
  void tick();   // call every loop() -- (re)connects on a timer when not
                 // connected, a cheap no-op otherwise
  void sendTelemetry(uint16_t bpm, uint32_t ibiMs, bool hasSignal,
                      uint16_t gsrRaw, float gsrNormalized);
                      // rate-limited internally to netConfig.sendHz -- safe
                      // to call every loop() unconditionally

  bool      connected() const { return WiFi.status() == WL_CONNECTED; }
  IPAddress localIp() const { return WiFi.localIP(); }

private:
  WiFiUDP  _udp;
  bool     _wifiWasUp = false;
  uint32_t _lastConnectAttemptMs = 0;
  uint32_t _lastSendMs = 0;

  void addressFor(char* buf, size_t bufLen, const char* leaf) const;
  void sendInt(const IPAddress& hub, const char* addr, int32_t v);
  void sendFloat(const IPAddress& hub, const char* addr, float v);
};

extern OscLink oscLink;
