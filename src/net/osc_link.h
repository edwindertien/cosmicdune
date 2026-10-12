#pragma once
#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include "../core/net_config.h"

// ---------------------------------------------------------------------------
// NetLinkState -- IDLE (not enabled/configured), CONNECTING (WiFi.begin()
// called, waiting), CONNECTED, or GAVE_UP (timed out without connecting --
// stays here until OscLink::retry() is called explicitly; does NOT keep
// retrying on its own). See OscLink below for why GAVE_UP exists.
// ---------------------------------------------------------------------------
enum class NetLinkState : uint8_t { IDLE, CONNECTING, CONNECTED, GAVE_UP };

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
// Non-blocking connect, then give up after a timeout: the plain
// WiFi.begin() on this core blocks until the join succeeds or fails, which
// freezes everything else in loop() -- LEDs, heartrate, display, CLI -- for
// as long as that takes. With no matching AP in range that's a long time,
// and the CYW43 blocking join has real, documented cases of never returning
// at all. (An earlier version of this file called begin() and wrapped a
// timeout around it, which could never help: the timeout check only runs
// once the call has returned.) startConnecting() now uses WiFi.beginNoBlock(),
// which returns immediately, and tick() polls WiFi.status() while the rest
// of loop() keeps running. After Timing::WIFI_CONNECT_TIMEOUT_MS it
// disconnects (stopping the driver's own background retries) and sits in
// GAVE_UP; only an explicit retry() (CLI `net retry`, or the menu's "Net
// Retry") tries again, so a missing network never occupies the pod
// automatically. The hardware watchdog armed in main.cpp stays as the
// backstop for the case where something still never returns.
// ---------------------------------------------------------------------------
class OscLink {
public:
  void begin(); // no config to store (see above) -- exists only so setup()
                 // calls every module's begin() the same way; does not block
  void tick();   // call every loop() -- advances the connection state
                  // machine; a cheap no-op once CONNECTED or GAVE_UP
  void retry();  // force a fresh connection attempt right now, regardless
                  // of current state -- no-op if not netConfig.enabled or
                  // no SSID set (prints why via Serial either way)

  void sendTelemetry(uint16_t bpm, uint32_t ibiMs, bool hasSignal,
                      uint16_t gsrRaw, float gsrNormalized);
                      // rate-limited internally to netConfig.sendHz -- safe
                      // to call every loop() unconditionally

  bool         connected() const { return _state == NetLinkState::CONNECTED; }
  NetLinkState state() const { return _state; }
  IPAddress    localIp() const { return WiFi.localIP(); }

private:
  WiFiUDP      _udp;
  NetLinkState _state = NetLinkState::IDLE;
  uint32_t     _connectStartMs = 0;
  uint32_t     _lastSendMs = 0;

  void startConnecting();

  void addressFor(char* buf, size_t bufLen, const char* leaf) const;
  void sendInt(const IPAddress& hub, const char* addr, int32_t v);
  void sendFloat(const IPAddress& hub, const char* addr, float v);
};

extern OscLink oscLink;