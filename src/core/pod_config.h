#pragma once

// ---------------------------------------------------------------------------
// PodConfigStore -- persists GSR calibration (range, inversion) and the
// LED strip's timing mode (fixed/ibi, fixed-mode speed) to LittleFS at
// /podconfig.json, so a chosen setup survives a reboot instead of
// resetting to config.h's compiled-in defaults every time.
//
// Deliberately reads/writes gsrSensor and pulseStrip's LIVE state
// directly (their existing getters/setters) rather than mirroring it
// into a separate PodConfig struct -- a second copy is exactly what let
// OscLink go stale earlier in this project (it held its own snapshot of
// NetConfig instead of reading the global), so this avoids repeating
// that mistake: there is only ever one place the current settings live.
//
// GSR baseline is deliberately NOT persisted here -- see gsr_sensor.h:
// it's captured fresh every boot and expected to need recalibrating most
// sessions regardless, sometimes even the same person on a different day,
// so saving a stale one would do more harm than good.
// ---------------------------------------------------------------------------
namespace PodConfigStore {
  bool begin();  // mounts LittleFS -- formats on first-ever boot. Shares
                  // the same LittleFS partition as NetConfigStore; safe to
                  // call independently since mounting twice is a cheap no-op
  bool load();   // reads /podconfig.json (if present) and applies it
                  // directly to gsrSensor/pulseStrip -- false if no file
                  // yet / parse failed, in which case those objects keep
                  // whatever defaults they already have
  bool save();   // writes gsrSensor/pulseStrip's CURRENT settings to
                  // /podconfig.json
}
