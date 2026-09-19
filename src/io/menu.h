#pragma once
#include <Arduino.h>

// ---------------------------------------------------------------------------
// Menu -- on-pod settings menu, navigated with the M5Stack encoder and
// shown on the SH1107 display. Covers: GSR range/invert/baseline, strip
// mode/speed, brightness, WiFi status (SSID shown, password only as
// set/not-set -- same convention as the CLI's `net`), hub IP/port
// (numeric, so genuinely knob-editable), net enable, and saving either
// settings group. Deliberately does NOT let you enter an SSID or
// password from here -- typing text with a rotary knob is bad UX no
// matter how it's built, so that stays CLI-only.
//
// Navigation: rotate to move/adjust, short-press to select/confirm,
// long-press to exit to the monitor view from anywhere (a shortcut
// alongside the explicit "Exit" list item, not a replacement for it).
//
// Reads/writes gsrSensor, pulseStrip, and netConfig directly -- the same
// "no separate copy" approach PodConfigStore already uses -- see its
// header for why that matters (it's the same lesson OscLink's staleness
// bug taught earlier in this project).
//
// The CLI keeps working unmodified alongside this -- it's USB-serial
// based and entirely independent of the I2C/display/encoder subsystem.
// ---------------------------------------------------------------------------
class Menu {
public:
  void begin();
  void tick(); // call every loop() -- cheap no-op-ish when closed (still
                // polls the encoder, just to catch the opening button
                // press), does real work (input handling + redraw) when open

  bool isOpen() const { return _state != State::CLOSED; }

private:
  enum class State { CLOSED, LIST, EDIT };
  // INFO items behave like VALUE/TOGGLE for navigation (short-press opens
  // a detail view) but ignore rotation -- used for WiFi SSID/password
  // status, which are shown, not edited, here.
  enum class ItemKind { TOGGLE, VALUE, ACTION, INFO };

  struct Item {
    const char* label;
    ItemKind    kind;
  };

  static constexpr uint8_t NUM_ITEMS = 17;
  static const Item ITEMS[NUM_ITEMS];

  State    _state = State::CLOSED;
  uint8_t  _selected = 0;
  uint32_t _actionConfirmUntilMs = 0; // brief "OK" shown near the selected
                                        // row after an ACTION item fires

  void   handleList(int16_t delta, bool shortPress);
  void   handleEdit(int16_t delta, bool shortPress);
  void   applyDelta(uint8_t itemIndex, int16_t delta); // TOGGLE/VALUE items
  void   performAction(uint8_t itemIndex);              // ACTION items

  void   drawList();
  void   drawEdit();
  String valueTextFor(uint8_t itemIndex) const;
};

extern Menu menu;