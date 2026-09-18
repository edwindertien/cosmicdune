#include "cli.h"
#include <string.h>
#include <IPAddress.h>
#include "../config.h"
#include "../drivers/pulse_sensor.h"
#include "../drivers/gsr_sensor.h"
#include "../drivers/pulse_strip.h"
#include "../core/net_config.h"
#include "../core/pod_config.h"
#include "../net/osc_link.h"

Cli cli;

void Cli::begin() {
  _lineBuf.reserve(96);
  Serial.println(F("[CLI] ready -- type 'help'"));
}

void Cli::tick() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      handleLine(_lineBuf);
      _lineBuf = "";
    } else if (_lineBuf.length() < 120) {
      _lineBuf += c;
    }
  }
  streamTick();
}

void Cli::streamTick() {
  if (!_streaming) return;
  uint32_t now = millis();
  if (now - _lastStreamMs < 100) return; // ~10Hz
  _lastStreamMs = now;
  Serial.print(F("STREAM,"));
  Serial.print(now);
  Serial.print(',');
  Serial.print(pulseSensor.bpm());
  Serial.print(',');
  Serial.print(pulseSensor.lastIbiMs());
  Serial.print(',');
  Serial.println(pulseSensor.hasSignal() ? F("signal") : F("nosignal"));
}

// ---------------------------------------------------------------------------
// Tiny tokenizer -- up to 4 space-separated tokens, in place on a copy of
// the line so we can safely use strtok without touching the String buffer.
// ---------------------------------------------------------------------------
static int tokenize(const String& line, char* buf, size_t bufLen, char* tok[], int maxTok) {
  strncpy(buf, line.c_str(), bufLen - 1);
  buf[bufLen - 1] = '\0';
  int n = 0;
  char* saveptr = nullptr;
  char* t = strtok_r(buf, " ", &saveptr);
  while (t && n < maxTok) { tok[n++] = t; t = strtok_r(nullptr, " ", &saveptr); }
  return n;
}

// ---------------------------------------------------------------------------
// Returns everything in `line` after skipping `skipTokens` space-separated
// words -- unlike tok[] above (which stops at maxTok and can't represent a
// value containing spaces), this preserves the rest of the line verbatim.
// Used for SSID/password values, which real-world WiFi networks routinely
// have spaces in.
// ---------------------------------------------------------------------------
static String remainderAfterTokens(const String& line, int skipTokens) {
  int pos = 0;
  for (int i = 0; i < skipTokens; ++i) {
    int sp = line.indexOf(' ', pos);
    if (sp < 0) return String();
    pos = sp + 1;
    while (pos < (int)line.length() && line[pos] == ' ') ++pos;
  }
  return line.substring(pos);
}

void Cli::handleLine(const String& lineIn) {
  String line = lineIn;
  line.trim();
  if (line.length() == 0) return;

  char  buf[128];
  char* tok[4];
  int   n = tokenize(line, buf, sizeof(buf), tok, 4);
  if (n == 0) return;
  String cmd = tok[0];

  if (cmd == "help") {
    printHelp();

  } else if (cmd == "status") {
    printStatus();

  } else if (cmd == "stream") {
    if (n >= 2 && String(tok[1]) == "on")  { _streaming = true;  Serial.println(F("OK stream on")); }
    else if (n >= 2 && String(tok[1]) == "off") { _streaming = false; Serial.println(F("OK stream off")); }
    else Serial.println(F("usage: stream on|off"));

  } else if (cmd == "beat") {
    // Inject one synthetic heartbeat -- exercises the LED pulse path with
    // no sensor attached. Optional 2nd arg previews a GSR level (0-100)
    // for the resulting colour instead of using the live GSR reading.
    uint16_t bpm = (n >= 2) ? (uint16_t)atoi(tok[1]) : 72;
    if (bpm < 20 || bpm > 250) { Serial.println(F("usage: beat [bpm] [gsrPercent]")); return; }
    float gsrLevel = gsrSensor.normalized();
    if (n >= 3) {
      int pct = atoi(tok[2]);
      if (pct < 0 || pct > 100) { Serial.println(F("usage: beat [bpm] [gsrPercent 0-100]")); return; }
      gsrLevel = pct / 100.0f;
    }
    pulseStrip.trigger(bpm, gsrLevel);
    Serial.print(F("OK injected beat @ ")); Serial.print(bpm);
    Serial.print(F(" bpm, gsr=")); Serial.println(gsrLevel, 2);

  } else if (cmd == "led" && n >= 3 && String(tok[1]) == "brightness") {
    int b = atoi(tok[2]);
    if (b < 0 || b > 255) { Serial.println(F("usage: led brightness <0-255>")); return; }
    pulseStrip.setBrightness((uint8_t)b);
    Serial.print(F("OK brightness=")); Serial.println(b);

  } else if (cmd == "mode") {
    if (n < 2) { Serial.println(F("usage: mode fixed|ibi | mode speed <ms>")); return; }
    String a = tok[1];
    if (a == "fixed") {
      pulseStrip.setMode(PulseMode::FIXED_SPEED);
      Serial.println(F("OK mode=fixed"));
    } else if (a == "ibi") {
      pulseStrip.setMode(PulseMode::IBI_SYNCED);
      Serial.println(F("OK mode=ibi"));
    } else if (a == "speed" && n >= 3) {
      long ms = atol(tok[2]);
      if (ms < 50 || ms > 10000) { Serial.println(F("usage: mode speed <ms>  (50-10000)")); return; }
      pulseStrip.setFixedTraversalMs((uint32_t)ms);
      Serial.print(F("OK fixed traversal=")); Serial.print(ms); Serial.println(F("ms"));
    } else {
      Serial.println(F("usage: mode fixed|ibi | mode speed <ms>"));
    }

  } else if (cmd == "gsr") {
    if (n < 2) {
      // bare 'gsr' -- quick calibration readout
      Serial.print(F("raw="));       Serial.print(gsrSensor.raw());
      Serial.print(F(" baseline=")); Serial.print(gsrSensor.baseline());
      Serial.print(F(" range="));    Serial.print(gsrSensor.range());
      Serial.print(F(" inverted=")); Serial.print(gsrSensor.inverted() ? F("yes") : F("no"));
      Serial.print(F(" normalized=")); Serial.println(gsrSensor.normalized(), 2);
      return;
    }
    String a = tok[1];
    if (a == "baseline") {
      if (n < 3) { Serial.print(F("baseline=")); Serial.println(gsrSensor.baseline()); return; }
      if (String(tok[2]) == "set") {
        gsrSensor.calibrateBaselineNow();
        Serial.print(F("OK baseline set to current reading (")); Serial.print(gsrSensor.baseline()); Serial.println(')');
      } else {
        long v = atol(tok[2]);
        if (v < 0 || v > 4095) { Serial.println(F("usage: gsr baseline set | gsr baseline <0-4095>")); return; }
        gsrSensor.setBaseline((uint16_t)v);
        Serial.print(F("OK baseline=")); Serial.println(v);
      }
    } else if (a == "range" && n >= 3) {
      long v = atol(tok[2]);
      if (v <= 0 || v > 4095) { Serial.println(F("usage: gsr range <1-4095>")); return; }
      gsrSensor.setRange((uint16_t)v);
      Serial.print(F("OK range=")); Serial.println(v);
    } else if (a == "invert" && n >= 3) {
      if (String(tok[2]) == "on")       { gsrSensor.setInverted(true);  Serial.println(F("OK invert=on")); }
      else if (String(tok[2]) == "off") { gsrSensor.setInverted(false); Serial.println(F("OK invert=off")); }
      else Serial.println(F("usage: gsr invert on|off"));
    } else {
      Serial.println(F("usage: gsr | gsr baseline [set|<raw>] | gsr range <counts> | gsr invert on|off"));
    }

  } else if (cmd == "net") {
    if (n < 2) {
      // bare 'net' -- status readout. Password is never echoed back, only
      // whether one is set and how long -- same reasoning as the
      // arm-controller project's `config show` for its AP password.
      Serial.print(F("pod="));  Serial.print(netConfig.podId);
      Serial.print(F(" ssid=")); Serial.print(netConfig.ssid[0] ? netConfig.ssid : "(unset)");
      Serial.print(F(" pass="));
      if (netConfig.password[0]) {
        Serial.print(F("set ("));
        Serial.print(strlen(netConfig.password));
        Serial.print(F(" chars)"));
      } else {
        Serial.print(F("(unset)"));
      }
      Serial.print(F(" hub=")); Serial.print(netConfig.hubHost[0] ? netConfig.hubHost : "(unset)");
      Serial.print(':'); Serial.print(netConfig.hubPort);
      Serial.print(F(" rate=")); Serial.print(netConfig.sendHz); Serial.print(F("Hz"));
      Serial.print(F(" enabled=")); Serial.print(netConfig.enabled ? "yes" : "no");
      Serial.print(F(" wifi="));
      if (oscLink.connected()) {
        Serial.print(F("connected ("));
        Serial.print(oscLink.localIp());
        Serial.print(')');
      } else {
        Serial.print(F("not connected"));
      }
      Serial.println();
      return;
    }
    String a = tok[1];
    if (a == "ssid") {
      String val = remainderAfterTokens(line, 2);
      if (val.length() == 0) { Serial.println(F("usage: net ssid <name>")); return; }
      if (val.length() > sizeof(netConfig.ssid) - 1) { Serial.println(F("ERR ssid too long (max 32 chars)")); return; }
      strncpy(netConfig.ssid, val.c_str(), sizeof(netConfig.ssid) - 1);
      netConfig.ssid[sizeof(netConfig.ssid) - 1] = '\0';
      Serial.print(F("OK ssid=")); Serial.println(netConfig.ssid);

    } else if (a == "pass") {
      String val = remainderAfterTokens(line, 2);
      if (val.length() > sizeof(netConfig.password) - 1) { Serial.println(F("ERR password too long (max 63 chars)")); return; }
      strncpy(netConfig.password, val.c_str(), sizeof(netConfig.password) - 1);
      netConfig.password[sizeof(netConfig.password) - 1] = '\0';
      Serial.print(F("OK password set (")); Serial.print(val.length()); Serial.println(F(" chars)"));

    } else if (a == "pod") {
      if (n < 3) { Serial.println(F("usage: net pod <1-6>")); return; }
      int id = atoi(tok[2]);
      if (id < 1 || id > 6) { Serial.println(F("usage: net pod <1-6>")); return; }
      netConfig.podId = (uint8_t)id;
      Serial.print(F("OK pod=")); Serial.println(id);

    } else if (a == "hub") {
      if (n < 4) { Serial.println(F("usage: net hub <ip> <port>")); return; }
      IPAddress test;
      if (!test.fromString(tok[2])) { Serial.println(F("usage: net hub <ip> <port>")); return; }
      long port = atol(tok[3]);
      if (port <= 0 || port > 65535) { Serial.println(F("usage: net hub <ip> <port>")); return; }
      strncpy(netConfig.hubHost, tok[2], sizeof(netConfig.hubHost) - 1);
      netConfig.hubHost[sizeof(netConfig.hubHost) - 1] = '\0';
      netConfig.hubPort = (uint16_t)port;
      Serial.print(F("OK hub=")); Serial.print(netConfig.hubHost); Serial.print(':'); Serial.println(netConfig.hubPort);

    } else if (a == "rate") {
      if (n < 3) { Serial.println(F("usage: net rate <1-60>")); return; }
      int hz = atoi(tok[2]);
      if (hz < 1 || hz > 60) { Serial.println(F("usage: net rate <1-60>")); return; }
      netConfig.sendHz = (uint8_t)hz;
      Serial.print(F("OK rate=")); Serial.print(hz); Serial.println(F("Hz"));

    } else if (a == "enable") {
      netConfig.enabled = true;
      Serial.println(F("OK net enabled -- will attempt WiFi on next tick"));

    } else if (a == "disable") {
      netConfig.enabled = false;
      Serial.println(F("OK net disabled"));

    } else if (a == "save") {
      bool ok = NetConfigStore::save(netConfig);
      Serial.println(ok ? F("OK saved to /netconfig.json") : F("ERR save failed"));

    } else {
      Serial.println(F("usage: net | net ssid <name> | net pass <pass> | net pod <1-6> |"));
      Serial.println(F("       net hub <ip> <port> | net rate <1-60> | net enable|disable | net save"));
    }

  } else if (cmd == "save") {
    // Persists GSR range/invert + strip mode/speed -- NOT WiFi settings
    // (see `net save` for those) and NOT GSR baseline (see `gsr` command's
    // help -- it's deliberately never persisted).
    bool ok = PodConfigStore::save();
    Serial.println(ok ? F("OK saved to /podconfig.json") : F("ERR save failed"));

  } else if (cmd == "reboot") {
    Serial.println(F("Rebooting..."));
    delay(50);
    rp2040.reboot();

  } else {
    Serial.println(F("unknown command -- type 'help'"));
  }
}

void Cli::printHelp() {
  Serial.println(F(
    "commands:\n"
    "  status                 current bpm/IBI/signal + strip mode + GSR\n"
    "  stream on|off          stream 't,bpm,ibiMs,signal' CSV lines (~10Hz)\n"
    "  beat [bpm] [gsrPct]    inject one synthetic heartbeat (default 72bpm,\n"
    "                         live GSR reading) -- exercises the LED pulse\n"
    "                         with no sensors attached\n"
    "  led brightness <0-255> set strip brightness (not persisted -- see\n"
    "                         config.h HW::STRIP_DEFAULT_BRIGHTNESS)\n"
    "  mode fixed|ibi         fixed = constant traversal speed regardless of\n"
    "                         BPM (fast heart rate -> multiple pulses in\n"
    "                         flight at once); ibi = one traversal per\n"
    "                         heartbeat, speed follows BPM (default)\n"
    "  mode speed <ms>        set the fixed-mode traversal time (default\n"
    "                         config.h Config::FIXED_SPEED_TRAVERSAL_MS)\n"
    "  gsr                    raw/baseline/range/inverted/normalized readout\n"
    "  gsr baseline [set|<raw>]\n"
    "                         set = capture the current reading as the new\n"
    "                         \"calm\" reference; <raw> sets it explicitly.\n"
    "                         Auto-captured once at boot -- see 'gsr' output\n"
    "  gsr range <counts>     raw ADC counts (12-bit) from baseline to\n"
    "                         \"fully sweaty\" (default\n"
    "                         config.h Config::GSR_DEFAULT_RANGE) -- widen\n"
    "                         if colour maxes out to red too easily, narrow\n"
    "                         if it barely leaves white\n"
    "  gsr invert on|off      flip if sweat reads as ADC counts DOWN on your\n"
    "                         wiring instead of up\n"
    "  save                   persist current GSR range/invert + strip\n"
    "                         mode/speed to /podconfig.json (not baseline --\n"
    "                         see 'gsr baseline'; not WiFi -- see 'net save')\n"
    "  net                    WiFi/OSC config + connection status (password\n"
    "                         never echoed, only whether one is set)\n"
    "  net ssid <name>        set the WiFi SSID (may contain spaces)\n"
    "  net pass <pass>        set the WPA password (may contain spaces)\n"
    "  net pod <1-6>          this pod's id -- becomes OSC address /pod/<id>/...\n"
    "  net hub <ip> <port>    hub's IP (dotted quad, not a hostname) + UDP port\n"
    "  net rate <1-60>        telemetry rate in Hz (default 15)\n"
    "  net enable|disable     turn WiFi/OSC telemetry on or off\n"
    "  net save               persist current net settings to /netconfig.json\n"
    "                         (settings apply live immediately; save just\n"
    "                         makes them survive a reboot)\n"
    "  reboot"
  ));
}

void Cli::printStatus() {
  Serial.print(F("bpm="));    Serial.print(pulseSensor.bpm());
  Serial.print(F(" ibi="));   Serial.print(pulseSensor.lastIbiMs()); Serial.print(F("ms"));
  Serial.print(F(" signal=")); Serial.print(pulseSensor.hasSignal() ? F("yes") : F("no"));
  Serial.print(F(" mode="));
  if (pulseStrip.mode() == PulseMode::FIXED_SPEED) {
    Serial.print(F("fixed("));
    Serial.print(pulseStrip.fixedTraversalMs());
    Serial.print(F("ms)"));
  } else {
    Serial.print(F("ibi"));
  }
  Serial.print(F(" gsr="));      Serial.print(gsrSensor.raw());
  Serial.print(F(" (norm="));    Serial.print(gsrSensor.normalized(), 2);
  Serial.print(')');
  Serial.print(F(" net="));
  Serial.println(oscLink.connected() ? F("connected") : (netConfig.enabled ? F("connecting") : F("off")));
}
