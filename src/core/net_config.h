#pragma once
#include <Arduino.h>

// ---------------------------------------------------------------------------
// NetConfig -- this pod's WiFi + OSC-hub settings, set via the CLI's
// `net ...` commands and persisted to LittleFS (NetConfigStore below) so
// they survive reflashing/rebooting -- nothing here is compiled in.
//
// SSID/password buffers match the real 802.11/WPA2 maximums (32/63 chars),
// same reasoning as the arm-controller project's AP credential buffers.
// ---------------------------------------------------------------------------
struct NetConfig {
  uint8_t  podId = 1; // this pod's id, 1-6 -- becomes the OSC address
                       // prefix /pod/<id>/... that osc_link.cpp sends to
  char     ssid[33]     = "";
  char     password[64] = "";
  char     hubHost[40]  = ""; // hub's IP, e.g. "192.168.1.50" -- a dotted
                                // quad only, not resolved as a hostname
  uint16_t hubPort = 9000;
  uint8_t  sendHz  = 15; // telemetry rate per pod

  // Stays false until deliberately configured -- a pod with no SSID set
  // shouldn't spend every boot retrying a WiFi connect it can't complete.
  // See CLI `net enable` / `net disable`.
  bool enabled = false;
};

extern NetConfig netConfig;

namespace NetConfigStore {
  bool begin();               // mounts LittleFS -- formats on first-ever boot
  bool load(NetConfig& cfg);  // false if no file yet / parse failed --
                                // caller should keep the struct's defaults
  bool save(const NetConfig& cfg);
}
