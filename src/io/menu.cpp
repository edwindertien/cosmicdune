#include "menu.h"
#include <string.h>
#include <IPAddress.h>
#include <FastLED.h>
#include "../config.h"
#include "../drivers/display.h"
#include "../drivers/m5_encoder.h"
#include "../drivers/gsr_sensor.h"
#include "../drivers/pulse_strip.h"
#include "../core/pod_config.h"
#include "../core/net_config.h"

Menu menu;

const Menu::Item Menu::ITEMS[Menu::NUM_ITEMS] = {
  { "GSR Range",    ItemKind::VALUE  },
  { "GSR Invert",   ItemKind::TOGGLE },
  { "GSR Baseline", ItemKind::ACTION },
  { "Strip Mode",   ItemKind::TOGGLE },
  { "Fixed Speed",  ItemKind::VALUE  },
  { "Brightness",   ItemKind::VALUE  },
  { "WiFi SSID",    ItemKind::INFO   },
  { "WiFi WPA",     ItemKind::INFO   },
  { "Hub IP.1",     ItemKind::VALUE  },
  { "Hub IP.2",     ItemKind::VALUE  },
  { "Hub IP.3",     ItemKind::VALUE  },
  { "Hub IP.4",     ItemKind::VALUE  },
  { "Hub Port",     ItemKind::VALUE  },
  { "Net Enable",   ItemKind::TOGGLE },
  { "Save Pod",     ItemKind::ACTION },
  { "Save WiFi",    ItemKind::ACTION },
  { "Exit",         ItemKind::ACTION },
};

// ---------------------------------------------------------------------------
// netConfig.hubHost is a dotted-quad string, not 4 separate bytes -- these
// use IPAddress's own operator[] to read/write one octet at a time without
// hand-rolling string parsing. A hubHost that doesn't parse (empty/unset)
// is treated as 0.0.0.0, so the octet rows always show *something* editable
// rather than garbage.
// ---------------------------------------------------------------------------
static uint8_t getHubOctet(uint8_t index) {
  IPAddress ip;
  if (!ip.fromString(netConfig.hubHost)) ip = IPAddress(0, 0, 0, 0);
  return ip[index];
}

static void setHubOctet(uint8_t index, uint8_t value) {
  IPAddress ip;
  if (!ip.fromString(netConfig.hubHost)) ip = IPAddress(0, 0, 0, 0);
  ip[index] = value;
  String s = ip.toString();
  strncpy(netConfig.hubHost, s.c_str(), sizeof(netConfig.hubHost) - 1);
  netConfig.hubHost[sizeof(netConfig.hubHost) - 1] = '\0';
}

void Menu::begin() {
  m5Encoder.begin();
}

void Menu::tick() {
  m5Encoder.update();
  int16_t delta      = (int16_t)m5Encoder.readSteps();
  bool    shortPress = m5Encoder.wasShortPressed();
  bool    longPress  = m5Encoder.wasLongPressed();

  // Long-press exits to the monitor view from anywhere, LIST or EDIT --
  // a shortcut on top of the explicit "Exit" list item, not a replacement
  // for it (in case the gesture isn't obvious/remembered in the moment).
  if (longPress) { _state = State::CLOSED; return; }

  if (_state == State::CLOSED) {
    if (shortPress) { _state = State::LIST; _selected = 0; }
    return; // main.cpp's monitor view owns the display while closed
  }

  if (_state == State::LIST) {
    handleList(delta, shortPress);
    if (_state == State::LIST) drawList(); // may have moved to EDIT or CLOSED above
  } else {
    handleEdit(delta, shortPress);
    if (_state == State::EDIT) drawEdit();
  }
}

void Menu::handleList(int16_t delta, bool shortPress) {
  if (delta != 0) {
    int16_t next = (int16_t)((_selected + delta) % (int16_t)NUM_ITEMS);
    if (next < 0) next += NUM_ITEMS;
    _selected = (uint8_t)next;
  }
  if (shortPress) {
    if (ITEMS[_selected].kind == ItemKind::ACTION) {
      performAction(_selected);
    } else {
      _state = State::EDIT; // VALUE, TOGGLE, or INFO -- INFO just won't
                              // respond to rotation once there, see applyDelta()
    }
  }
}

void Menu::handleEdit(int16_t delta, bool shortPress) {
  if (delta != 0) applyDelta(_selected, delta);
  if (shortPress) _state = State::LIST;
}

void Menu::applyDelta(uint8_t itemIndex, int16_t delta) {
  switch (itemIndex) {
    case 0: { // GSR Range
      int32_t v = (int32_t)gsrSensor.range() + delta * (int32_t)MenuCfg::GSR_RANGE_STEP;
      v = constrain(v, 1, 4095);
      gsrSensor.setRange((uint16_t)v);
      break;
    }
    case 1: // GSR Invert -- any turn flips it, direction doesn't matter for a toggle
      gsrSensor.setInverted(!gsrSensor.inverted());
      break;
    case 3: // Strip Mode -- toggle
      pulseStrip.setMode(pulseStrip.mode() == PulseMode::FIXED_SPEED
                            ? PulseMode::IBI_SYNCED : PulseMode::FIXED_SPEED);
      break;
    case 4: { // Fixed Speed
      int32_t v = (int32_t)pulseStrip.fixedTraversalMs() + delta * (int32_t)MenuCfg::FIXED_SPEED_STEP;
      v = constrain(v, 50, 10000);
      pulseStrip.setFixedTraversalMs((uint32_t)v);
      break;
    }
    case 5: { // Brightness
      int32_t v = (int32_t)FastLED.getBrightness() + delta * (int32_t)MenuCfg::BRIGHTNESS_STEP;
      v = constrain(v, 0, 255);
      pulseStrip.setBrightness((uint8_t)v);
      break;
    }
    // 6, 7 = WiFi SSID / WPA -- INFO, read-only, no case needed (falls to default)
    case 8: case 9: case 10: case 11: { // Hub IP octets
      uint8_t octetIdx = (uint8_t)(itemIndex - 8);
      int32_t v = (int32_t)getHubOctet(octetIdx) + delta;
      v = constrain(v, 0, 255);
      setHubOctet(octetIdx, (uint8_t)v);
      break;
    }
    case 12: { // Hub Port
      int32_t v = (int32_t)netConfig.hubPort + delta;
      v = constrain(v, 1, 65535);
      netConfig.hubPort = (uint16_t)v;
      break;
    }
    case 13: // Net Enable -- toggle
      netConfig.enabled = !netConfig.enabled;
      break;
    default:
      break; // ACTION items don't reach here (EDIT state is only entered
              // for TOGGLE/VALUE/INFO items -- see handleList)
  }
}

void Menu::performAction(uint8_t itemIndex) {
  switch (itemIndex) {
    case 2: // GSR Baseline Reset
      gsrSensor.calibrateBaselineNow();
      _actionConfirmUntilMs = millis() + 600;
      break;
    case 14: // Save Pod (GSR range/invert + strip mode/speed -> /podconfig.json)
      PodConfigStore::save();
      _actionConfirmUntilMs = millis() + 600;
      break;
    case 15: // Save WiFi (-> /netconfig.json)
      NetConfigStore::save(netConfig);
      _actionConfirmUntilMs = millis() + 600;
      break;
    case 16: // Exit
      _state = State::CLOSED;
      break;
    default:
      break;
  }
}

String Menu::valueTextFor(uint8_t itemIndex) const {
  switch (itemIndex) {
    case 0: return String(gsrSensor.range());
    case 1: return gsrSensor.inverted() ? String(F("ON")) : String(F("OFF"));
    case 3: return (pulseStrip.mode() == PulseMode::FIXED_SPEED) ? String(F("fixed")) : String(F("ibi"));
    case 4: return String(pulseStrip.fixedTraversalMs()) + F("ms");
    case 5: return String(FastLED.getBrightness());
    case 6: return netConfig.ssid[0] ? String(netConfig.ssid) : String(F("(unset)"));
    // Password itself is never shown, only whether one is set -- same
    // convention as the CLI's `net` command.
    case 7: return netConfig.password[0] ? String(F("set")) : String(F("not set"));
    case 8: return String(getHubOctet(0));
    case 9: return String(getHubOctet(1));
    case 10: return String(getHubOctet(2));
    case 11: return String(getHubOctet(3));
    case 12: return String(netConfig.hubPort);
    case 13: return netConfig.enabled ? String(F("ON")) : String(F("OFF"));
    default: return String(); // ACTION items have no value column
  }
}

void Menu::drawList() {
  display.clear();
  display.text(0, 0, F("-- MENU --"));

  // Only 5 rows fit readably on a 64px-tall screen -- show a window
  // centered on the current selection rather than all NUM_ITEMS at once.
  const uint8_t visibleRows = 5;
  int8_t firstRow = (int8_t)_selected - (int8_t)(visibleRows / 2);
  if (firstRow < 0) firstRow = 0;
  if (firstRow > (int8_t)(NUM_ITEMS - visibleRows)) firstRow = (int8_t)(NUM_ITEMS - visibleRows);

  // Value column is narrow (x=90 to the 128px edge) -- long values like
  // "(unset)"/"not set" (WiFi SSID/WPA) overflow it. Truncated here to a
  // fixed width for every row rather than special-cased to just those
  // two, since anything else that grows long later hits the same
  // problem. The full, untruncated value is still one press away --
  // drawEdit() below shows it at 2x size with no truncation.
  const uint8_t valueMaxChars = 6;

  for (uint8_t row = 0; row < visibleRows; ++row) {
    uint8_t idx = (uint8_t)(firstRow + row);
    if (idx >= NUM_ITEMS) break;
    int16_t y = 12 + row * 10;
    String line = String(idx == _selected ? F("> ") : F("  ")) + ITEMS[idx].label;
    display.text(0, y, line);
    if (ITEMS[idx].kind != ItemKind::ACTION) {
      String val = valueTextFor(idx);
      if (val.length() > valueMaxChars) val = val.substring(0, valueMaxChars);
      display.text(90, y, val);
    }
    if (idx == _selected && millis() < _actionConfirmUntilMs) {
      display.text(90, y, F("OK"));
    }
  }
  display.show();
}

void Menu::drawEdit() {
  display.clear();
  bool isInfo = ITEMS[_selected].kind == ItemKind::INFO;
  display.text(0, 0, isInfo ? F("-- VIEW --") : F("-- EDIT --"));
  display.text(0, 16, ITEMS[_selected].label);
  display.text(0, 32, valueTextFor(_selected), 2);
  display.text(0, 56, isInfo ? F("press to close") : F("press to confirm"));
  display.show();
}