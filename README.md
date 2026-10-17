# cosmic dune -- sensor pod and tools (manual)

One of 6 wearable pods for the *cosmic dune* project: an artistic laser
distress signal sent into the sky, triggered by 6 musicians' biosignals
synchronizing. Each pod reads one musician's heart rate (ear-clip pulse
sensor) and skin conductance (GSR), renders that live on a 250-LED strip
worn on the body and on an on-pod OLED display, and reports telemetry
over WiFi (OSC) to a central hub.

The hub is not one program yet. Its parts exist separately: a Raspberry Pi
that hosts the pods' WiFi network and runs a touch-screen dashboard
(`tools/`), and test/visual tools that drive the Helios laser DAC
(`tools/laser/`). `context.md` says why each is the way it is and where it
stands.

**Status tags in this file.** *(confirmed)* = seen working on the real
hardware. *(untested)* = written and checked only in simulation, against a
mock, or in a headless browser -- treat as bring-up, not as known-good.

**Everything must work with no internet at the venue.** Nothing is loaded
from a CDN or font host. Things that need internet *once* (to build or
install, never at run time): PlatformIO libraries, `pip install`, the Helios
SDK download, and on the Pi `apt install libusb-1.0-0-dev g++`.

## Repository layout

```
cosmic-dune-pod/
  platformio.ini
  src/                       pod firmware (Raspberry Pi Pico W)
    config.h  main.cpp
    drivers/  core/  net/  io/
  tools/
    requirements.txt         python-osc, flask
    osc_test_hub.py          terminal monitor + fake pods
    osc_web_dashboard.py     touch-screen dashboard (Flask)
    chart.umd.js             Chart.js 4.5.1, bundled so the dashboard works offline
    laser/                   Helios DAC -> ILDA laser tools
      laser_test.py  patterns.py  visuals.py  helios_dac.py
      lib/                   the Helios shared library (you add it, see "Laser tools")
```

## How the pieces connect

```
pods (Pico W) --WiFi, OSC over UDP :9000--> Raspberry Pi, hosting the WiFi network (10.42.0.1)
                                             |- osc_web_dashboard.py   web page :5000 (800x480 touch screen)
                                             '- (planned) hub process --USB--> Helios DAC --ILDA--> Laserworld CUBE 3
```

Today the laser tools run on their own (on a Mac first) and either take
their own OSC input (`show --osc-port`) or use simulated pods. A pod has
exactly one hub address, and only one program can listen on a UDP port --
so until the single hub process exists, the dashboard and the laser tool
can't both receive the real pods at once.

## Wiring

| Signal                            | Pico W pin  |
|------------------------------------|-------------|
| Grove ear-clip signal              | GP26        |
| Grove ear-clip VCC / GND           | 3V3 / GND   |
| GSR sensor signal                  | GP27 (ADC1) |
| GSR sensor VCC / GND               | 3V3 / GND   |
| LED strip data-in                  | GP17        |
| LED strip 5V / GND                 | **external 5V supply, NOT the Pico** -- common GND back to the Pico |
| OLED display (I2C0) SDA / SCL      | GP8 / GP9   |
| M5Stack encoder (I2C1) SDA / SCL   | GP6 / GP7   |

**Power, read this first:** 250x WS2812B at full white/full brightness is on
the order of 15A @ 5V -- far beyond USB or a small onboard regulator. Feed
the strip from its own adequately-rated 5V supply with a common ground back
to the Pico, and inject power at both ends of the 5m run if you see
dimming/colour-shift toward the far end. Normal operation (one travelling
comet, not the whole strip lit) draws much less than the full-white worst
case -- size the supply for the worst case anyway.

## Build

Requires [PlatformIO](https://platformio.org/) (VS Code extension or CLI).

```
pio run -t upload
pio device monitor
```

If the `pico_w` environment fails to resolve, check the note at the top of
`platformio.ini` -- the board id used by the `maxgerhardt` platform fork can
shift between versions. If a build fails on a missing header from a file
under `src/core/` or `src/net/`, double check that file actually exists at
that path locally -- these are newer folders in the project and it's easy
to end up with a file saved flat in `src/` instead of its subfolder.

## First bring-up (no sensors needed)

1. Flash and open the serial monitor (115200 baud).
2. `status` -- confirms the CLI is alive; should print `bpm=0 ... signal=no
   mode=ibi gsr=... net=off`.
3. `beat 72` -- injects one synthetic heartbeat. Confirms, with no sensors
   attached at all, that a white/blue comet travels the strip once, and
   the OLED's beat circle flashes with a pulse-waveform blip.
4. `beat 140 80` -- same, but with a synthetic 80% GSR level: the comet
   should read purple/red instead of white/blue (see `Config::gsrToColor`
   in `src/drivers/pulse_strip.cpp` to retune the gradient).
5. Clip on the ear-clip sensor, `stream on` -- watch live `bpm,ibiMs,signal`
   CSV lines. If `bpm` never moves off 0, or reads roughly double a manual
   pulse count, see the polarity note in `src/config.h` (`HW::PULSE_PIN`) --
   the sensor's beat edge may be RISING rather than the assumed FALLING on
   your specific module.
6. `gsr` -- watch the raw ADC count move as you rub your fingers together
   or relax. Set `gsr range <counts>` to whatever span actually takes you
   from white to red; `gsr invert on` if the colour runs backwards.
7. Turn the encoder knob -- press it to open the on-pod menu (see below).

## CLI reference

### Status / diagnostics
```
status                   bpm/IBI/signal + strip mode + GSR + net, one line
stream on|off             stream 't,bpm,ibiMs,signal' CSV lines (~10Hz)
beat [bpm] [gsrPct]       inject one synthetic heartbeat (default 72bpm,
                          live GSR reading) -- exercises the LED pulse with
                          no sensors attached; gsrPct (0-100) previews a
                          GSR level instead of using the live sensor
reboot
```

### LED strip
```
led brightness <0-255>   strip brightness (not persisted via CLI -- see
                          `save` below, or config.h HW::STRIP_DEFAULT_
                          BRIGHTNESS for the boot default)
mode fixed|ibi            fixed = constant traversal speed regardless of
                          BPM (fast heart rate -> multiple pulses in flight
                          at once); ibi = one traversal per heartbeat,
                          speed follows BPM (default)
mode speed <ms>            set the fixed-mode traversal time (default
                          config.h Config::FIXED_SPEED_TRAVERSAL_MS)
```

### GSR
```
gsr                       raw/baseline/range/inverted/normalized readout
gsr baseline [set|<raw>]  set = capture the current reading as the new
                          "calm" reference; <raw> sets it explicitly.
                          Auto-captured once at boot -- never persisted
                          (see context.md for why)
gsr range <counts>        raw ADC counts (12-bit) from baseline to "fully
                          sweaty" (default config.h Config::GSR_DEFAULT_RANGE)
                          -- widen if colour maxes to red too easily,
                          narrow if it barely leaves white
gsr invert on|off         flip if sweat reads as ADC counts DOWN on your
                          wiring instead of up
```

### WiFi / OSC telemetry
```
net                        WiFi/OSC config + live connection status
                          (password never echoed, only whether one is set)
net ssid <name>            set the WiFi SSID (may contain spaces)
net pass <pass>            set the WPA password (may contain spaces); leave
                          unset for an open network
net pod <1-6>              this pod's id -- becomes the OSC address prefix
                          /pod/<id>/...  Set a DIFFERENT id on each of the
                          6 physical pods before saving, or they'll collide.
net hub <ip> <port>        hub's IP (dotted quad, not a hostname) + UDP port
net rate <1-60>            telemetry rate in Hz (default 15)
net enable|disable         turn WiFi/OSC telemetry on or off
net retry                  force a fresh connect attempt now -- what to use
                          after `gave up` shows in `net`/`status`
net save                   persist current net settings to /netconfig.json
```
`status` and `net` show the connection as `off`, `connecting`, `connected`
or `gave up (try 'net retry')`.

OSC messages sent per pod (see `src/net/osc_link.cpp`):
```
/pod/<id>/bpm      int    0 if no pulse signal
/pod/<id>/ibi      int    milliseconds
/pod/<id>/signal   int    0 or 1
/pod/<id>/gsrRaw   int    smoothed 12-bit ADC counts
/pod/<id>/gsr      float  0.0-1.0 normalized
```
The pods send a smoothed BPM, not individual beat events -- so a receiver
can compare heart *rates*, but cannot see whether two hearts beat at the
same moment.

### WiFi behaviour: never blocks, gives up, watchdog
- Connecting never blocks the pod: the LED strip, heart-rate sensing,
  display, menu and CLI keep running while it searches (`WiFi.beginNoBlock()`;
  the plain `WiFi.begin()` blocks inside the call, possibly for good, which
  is what froze the pod -- see `context.md`). *(untested with no network
  in range; joining the Pi's hotspot works, confirmed)*
- If it hasn't connected after 8 s (`Timing::WIFI_CONNECT_TIMEOUT_MS`) it
  **gives up**: the radio is told to disconnect and the pod does *not* try
  again by itself. Retry with `net retry` or the menu's **Net Retry**;
  `net disable` then `net enable` also restarts it.
- If a working connection drops, it reconnects once (same 8 s window), then
  gives up the same way.
- A hardware watchdog (15 s, `Timing::WATCHDOG_MS`) is armed at boot and fed
  at the end of every `loop()`. If anything ever stalls the pod that long
  -- WiFi or not -- it reboots instead of staying frozen. A reboot costs the
  GSR baseline calibration. **If the boot banner repeats every ~15 s, something
  is still blocking: tell whoever maintains this.**

### Persistence
```
save                       persist current GSR range/invert + strip
                          mode/speed to /podconfig.json
```
Two separate config files, two separate save commands -- `net save` for
WiFi (`/netconfig.json`), bare `save` for GSR/strip settings
(`/podconfig.json`). GSR **baseline** is never persisted by either.

## On-pod display and menu

An SH1107 OLED (M5Stack "Unit OLED", 128x64, I2C0) shows a live monitor
view when the menu isn't open:

- A beat indicator circle (top-left) -- hollow with no signal, flashes
  filled on each detected heartbeat.
- A "faux" heartbeat trace next to it, with the current BPM as a number.
  There's no raw analog waveform available from this sensor (it's a
  digital comparator, not analog PPG) -- this is a small stylized blip
  injected at the right edge and scrolled left once per *real* detected
  beat, similar in spirit to a bedside monitor's sweep display. The
  timing is genuine; the shape is synthetic.
- A GSR trend graph below that, with the current raw ADC count as a
  number next to it -- sampled roughly once a second (GSR moves on a
  timescale of seconds, so this gives close to a minute of visible trend).
- WiFi status/IP at the bottom.

An M5Stack Unit Encoder (I2C1, addr 0x40) drives an on-pod settings menu
over this same screen: rotate to navigate/adjust, short-press to
select/confirm, long-press to exit to the monitor view from anywhere.
Covers GSR range/invert/baseline-reset, strip mode/speed, brightness,
WiFi status (SSID shown, password only as set/not-set) and hub IP/port
(individually adjustable, since those are numeric), net enable, and
saving either settings group (`Save Pod` / `Save WiFi`). Entering an SSID
or password itself stays CLI-only -- typing free text with a rotary knob
isn't good UX no matter how it's built.

The menu is 19 items long, navigated as one flat scrolling list (not
grouped into submenus): GSR Range, GSR Invert, GSR Baseline, Strip Mode,
Fixed Speed, Brightness, WiFi SSID / WPA / **Status** (view only), Hub IP.1-4,
Hub Port, Net Enable, **Net Retry**, Save Pod, Save WiFi, Exit. Worth
watching in practice: if that's tedious to scroll through for routine
adjustments, grouping related items behind a submenu (e.g. one "WiFi" entry
that opens its own short list) would be the natural next step -- not built
since it's unclear if the flat list is actually a problem until it's used
hands-on.

## The Raspberry Pi: hotspot, touch screen, dashboard

### The Pi hosts the pods' network
No dependency on venue WiFi: the Pi runs its own access point, so the pods
always have a network to join once the Pi is on. Current Raspberry Pi OS
(Bookworm/Trixie) does this natively with NetworkManager. One profile, made
once (the recommended form -- your test hotspot may have been made another way):

```
sudo nmcli connection add type wifi ifname wlan0 con-name cosmic-ap ssid <name> \
  802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.channel 6 \
  wifi-sec.key-mgmt wpa-psk wifi-sec.psk '<password>' \
  ipv4.method shared ipv4.addresses 192.168.4.1/24 \
  connection.autoconnect yes connection.autoconnect-priority 100 \
  connection.autoconnect-retries 0 802-11-wireless.powersave 2
sudo nmcli connection up cosmic-ap
```

Rules that matter:
- **One radio:** while hosting, the Pi cannot also be a WiFi client. Any other
  saved WiFi profile on the Pi (home, lab) must have autoconnect **off**, or
  NetworkManager and the hotspot fight over the radio. The symptom is the
  hotspot appearing, vanishing, the Pi rejoining the home WiFi, and repeating.
- **Exactly one hotspot profile.** `nmcli connection show` should list one;
  delete duplicates by UUID.
- **No boot script needed.** Autoconnect with priority 100 brings it up at
  boot. (A systemd unit running `nmcli connection up Hotspot` by name was
  suggested earlier and is not recommended: it adds a second activation
  mechanism, and a name shared by two profiles is ambiguous.)
- To see why it drops: `sudo journalctl -u NetworkManager -f` and read the
  `reason '...'` on the `activated -> deactivating` lines. `reason
  'new-activation'` means something else activated another profile.
- **Addresses:** with the profile above the Pi is `192.168.4.1`. A hotspot made
  with NetworkManager's defaults uses `10.42.0.x` instead, with the Pi at
  `10.42.0.1` -- that is what the current test setup uses *(confirmed: a pod
  joined and got 10.42.0.38)*. On each pod: `net hub <the Pi's address> 9000`
  then `net save`.
- The current test hotspot (SSID `distress`) is **open** -- the pod shows
  `pass=(unset)`. Set WPA2 before the show if other devices will be nearby.

### The touch screen (800x480)
- Turn off the automatic on-screen keyboard: *Raspberry menu > Preferences >
  Control Centre > Display > On-screen keyboard > Disabled*, or `sudo
  raspi-config` > Display Options > D6 Onscreen Keyboard > S3 Always Off.
  (A forum post gives `sudo raspi-config nonint do_squeekboard S3` for scripts;
  the S1/S2/S3 values match the official docs.)
- Full-screen browser: `chromium-browser --kiosk http://localhost:5000`
  (`chromium` on newer images).

### Dashboard: `tools/osc_web_dashboard.py`
Receives the pods' OSC and serves a page built for the 800x480 touch screen.
*(untested on the real screen; checked in a headless browser at exactly
800x480 with simulated pods)*

- **Grid:** six fixed tiles, pod 1-6. Each shows BPM (large), a GSR meter in the
  same colours as the pod's LED strip (white, blue, purple, red), and the state:
  green = online, amber = `no pulse`, red = `OFFLINE <s>` (nothing heard for
  3 s), greyed and not tappable = never heard from. The header counts
  `n / 6 online`; the footer prints the `net hub <ip> <port>` line for each of
  the Pi's addresses, to copy onto a pod.
- **Detail:** tap a tile for that pod's BPM graph (fixed 30-180) and GSR graph
  (0-1) over the last 60 s, plus IBI, pulse signal, raw GSR, level, IP and time
  since the last packet. The Back button (or Esc) returns to the grid.
- Pod ids above 6 are received but not shown (the pod CLI only allows 1-6).

```
cd tools
python osc_web_dashboard.py [--osc-port 9000] [--web-port 5000]
```
Run it as a script, **not** with `flask run` -- the OSC listener is started by
the script, and `flask run` binds to localhost only. The page works from
`http://<the Pi>:5000` on any device on the hotspot.

**Offline:** it loads nothing from the internet. `chart.umd.js` must sit next to
the script; if it is missing, the tiles and numbers still work and the graph
area says so. (An earlier version loaded Chart.js from a CDN; on the hotspot
there was no internet, the page's script crashed before it started polling,
and the page stayed blank while the server was receiving data fine.)

For debugging, `curl http://<pi>:5000/api/summary` (all pods, no history),
`/api/pod/<id>` (one pod with 60 s history), `/api/data` (everything), `/api/info`.

### Test tools: `tools/osc_test_hub.py`
`pip install -r tools/requirements.txt`, then:
- `osc_test_hub.py monitor` -- terminal table of live pods, with each of this
  machine's addresses and the matching `net hub` command.
- `osc_test_hub.py simulate --pods 6` -- fake telemetry for N pods, so the
  dashboard (and the laser tool's `show --osc-port`) can be tried with no
  hardware. Both listen on the same default port, so run one listener at a time.

## Laser tools: `tools/laser/` (Helios DAC -> Laserworld CUBE 3 over ILDA)

Standalone: no sensors, no network, no extra Python packages (only
`python-osc` for `show --osc-port`). Runs the same on a Mac and on the Pi.
**`python3 laser_test.py -h` prints the complete setup, calibration and safety
notes**; this section is the short version.

### Safety -- read first
The CUBE 3 is a multi-watt laser. Aim at a matte wall a few metres away, never
at people, windows, mirrors or the sky (airspace is what the ILT permit is
for), with nobody in the beam path, and keep the laser's own key/interlock
within reach. What the software adds, and what it does not replace:
- **Fail dark.** Every frame is sent with the DAC's `SINGLE_MODE`: the DAC plays
  it once and, if no new frame arrives, turns the laser **off**. If the program
  dies, hangs, or the Mac sleeps, the laser goes dark. (The SDK's own Python
  example sends flags=0, which loops the last frame forever -- deliberately not
  used.)
- Ctrl-C, `kill`, closing the terminal, "no DAC found" and a refused arm all
  run Stop + shutter closed + close. Only `kill -9` or power loss skip that --
  and `SINGLE_MODE` covers those.
- Every frame starts and ends with the beam **off at the centre**, and any frame
  that could park a bright static dot (a lit area under 0.04 wide on the +-1
  field, or more than 8 lit points on one spot) or exceed the intensity cap is **refused**.
  The "dot" in the visuals is a small, dimmed ring, never a point.
- Timid defaults: `--size 0.3 --intensity 0.15 --seconds 60`; `--intensity` above
  0.5 needs `--allow-high`; `run`/`show` make you press ENTER to arm after the
  DAC is found.

### One-time setup
**macOS** (the dylib is a universal binary: Intel and Apple Silicon):
```
git clone https://github.com/Grix/helios_dac.git
mkdir -p tools/laser/lib
cp helios_dac/sdk/cpp/shared_library/libHeliosLaserDAC.dylib tools/laser/lib/
cp helios_dac/sdk/cpp/libusb_bin/macOS/libusb-1.0.0.dylib   tools/laser/lib/
```
`git clone` avoids macOS's download quarantine. If loading still fails, the
error prints the `xattr -dr com.apple.quarantine` and `codesign --force --sign -`
commands to try.

**Raspberry Pi / Linux.** The `.so` in the SDK repo is x86-64 only, so build it
on the Pi once (needs internet once):
```
sudo apt-get install libusb-1.0-0-dev g++
cd helios_dac/sdk/cpp/shared_library
g++ -Wall -std=c++14 -fPIC -O2 -c HeliosDacAPI.cpp
g++ -Wall -std=c++14 -fPIC -O2 -c ../HeliosDac.cpp
g++ -Wall -std=c++14 -fPIC -O2 -c ../idn/idn.cpp
g++ -Wall -std=c++14 -fPIC -O2 -c ../idn/idnServerList.cpp
g++ -Wall -std=c++14 -fPIC -O2 -c ../idn/plt-posix.cpp
g++ -shared -o libHeliosDacAPI.so HeliosDacAPI.o HeliosDac.o plt-posix.o idn.o idnServerList.o -lusb-1.0 -lpthread
cp libHeliosDacAPI.so <repo>/tools/laser/lib/
```
**`-lusb-1.0` must come after the `.o` files.** The SDK's own doc puts it before
them; on current Debian/Ubuntu/Raspberry Pi OS the linker then silently drops
the library and loading fails with `undefined symbol: libusb_open`. For USB
access without sudo, create `/etc/udev/rules.d/90-heliosdac.rules`:
```
ACTION=="add", SUBSYSTEM=="usb", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="e500", MODE="0660", GROUP="plugdev"
```
then `sudo udevadm control --reload && sudo udevadm trigger` (and be in group
`plugdev`). The Pi-side build was checked on x86 Linux only. *(untested on the Pi)*

### First run, in this order
```
python3 laser_test.py check          # every pattern and scene, safety-checked. No hardware.
python3 laser_test.py preview --fit  # writes laser_preview.png of the test patterns
python3 laser_test.py probe          # finds the DAC. Emits no light.
python3 laser_test.py run circle     # small, dim, stops itself after 60 s
```
Put the CUBE in ILDA mode first (see its manual) and connect the ILDA cable.

### Test patterns (`run <name>`, or `cycle`)
`circle` (alignment), `square`/`star` (corner sharpness), `orient` (asymmetric F
with a green circle top-right and a red triangle bottom-right: shows mirroring
and rotation), `colors` (red, green, blue, white top to bottom), `ramp` (each
colour ramps 0 to full, left to right: see Calibration), `grid` (linearity),
`sweep`, `spiral`, `rays` (animated). `python3 laser_test.py list` describes them.
The sweep palette is the pod's LED palette (white, blue, purple, red) -- it has
**no green by design**; `--palette spectrum` gives blue, cyan, green, yellow, red.

### Calibration -- properties of your projector
Bench results so far *(confirmed)*: orientation correct; circle, orient, colors
correct; linearity good; line starts/stops need calibrating; `sweep` showed red,
blue, white with a long pause; `rays` moved the mirrors but showed no light.
The likely cause of the last two is **diode thresholds** (see below). The
calibration options themselves are *(untested)* on the real laser.

- `run ramp --seconds 120`: each colour ramps from 0 to full along its line.
  Where a line first becomes visible, as a fraction of its length, is that
  diode's threshold. Pass it as `--rgb-min R,G,B`, e.g. `--rgb-min 0.25,0.4,0.15`.
  Below its threshold a diode emits nothing, so dim mixed colours and small
  rings can vanish. `--rgb-gain R,G,B` (<= 1) tames a channel that is too strong.
  The intensity cap still bounds every channel.
- `--blank-shift N` delays the colour relative to the mirrors by N points
  (-6..6). Try +1 to +3 if lines start with a streak from the previous position or
  end short; negative if they start late.
- `--settle N`, `--dwell N`, `--travel-step X`: more settling points, more
  corner dwell, gentler blanked jumps. If the galvos are noisy or ring, try
  `--travel-step 0.03 --settle 6`.
- `--pps` (default 20000): raise to 25000-30000 if corners stay sharp; lower if
  `square`/`star` come out rounded.
- Orientation: `--flip-x --flip-y --swap-xy --rotate DEG --offset-x/-y`.

### Pod visuals: `show <scene>` *(untested on the real laser)*
Six pods become one pulse. Each pod has a heartbeat oscillator at its own BPM;
a single number, **sync** (0..1), pulls them toward one shared phase and shape.
A beat swells fast and relaxes slowly: a ring goes from a small circle ("dot")
to a big one and back.

| Scene | sync 0 | sync 1 |
|---|---|---|
| `merge` | six small rings round a hexagon, each in its pod's colour | one big ring pulsing as one |
| `bloom` | one ring in six sectors, one per pod (pod 1 at the top, clockwise) | a single round ring |
| `ripple` | six concentric rings | one ring |
| `spokes` | ragged six-ray starburst | symmetric star |

```
python3 laser_test.py preview scenes --t 6      # PNG: every scene at sync 0..1
python3 laser_test.py show merge --dry-run      # no laser, no DAC
python3 laser_test.py show merge                # demo pods; ENTER to arm
python3 laser_test.py show bloom --sync 0.6     # fix the sync to tune a look
python3 laser_test.py show merge --osc-port 9001   # real pods: `net hub <this ip> 9001`
```
Demo pods scatter, converge and scatter again on a 90 s cycle. **Where sync
comes from:** *measured* = how alike the pods' BPMs are (0 when they average
10 BPM apart -- `--tol-bpm`), plus a manual **assist knob**: 0 = only what the
pods do, 1 = fully merged regardless. `--sync` fixes it instead. Live keys:
`]` `[` knob up/down, `0`/`1` knob min/max, `n` `p` scene, `b` or SPACE
**blackout** (laser off, program keeps running), `q` quit.

This is *rate* sync (similar BPM), not beat-phase sync: see "Deliberately deferred".
At 20 kpps the busiest states (six large rings) take about 33 ms per frame; if
they flicker, raise `--pps` or lower `--size`. Rendering took 1-6 ms per frame
on the small Linux VM used for testing; on a Mac and on the Pi 3 it is *(untested)*.

## Known unverified assumptions

Pod firmware and hardware:
- **`board = rpipicow`** in `platformio.ini` -- see the note there.
- **Grove ear-clip edge polarity** (FALLING) -- flip to RISING in
  `src/drivers/pulse_sensor.cpp` if beats don't register or double-count.
- **GSR polarity and default range** -- both need calibrating per physical
  sensor/wearer; see the bring-up steps above.
- **Encoder rotation direction** -- `M5Encoder::readSteps()` returns
  positive for clockwise, ported from a previous project's assumption but
  not independently reconfirmed on this exact unit. If menu navigation
  feels backwards, flip the sign where it's consumed in `src/io/menu.cpp`.
- **`lib_ldf_mode = chain`** in `platformio.ini`, with
  `lib_ignore = lwIP_ESPHost` alongside it to exclude one irrelevant WiFi
  backend file that doesn't apply to this board (see `platformio.ini`'s
  comment, and the confirmed upstream issue it links, for why).
- **WiFi with no network in range:** that the LED strip and heart-rate keep
  running, the pod gives up after ~8 s and `net retry` recovers, and the
  watchdog never fires in normal use (no ~15 s reboot loop).
- **Older arduino-pico cores** (5.2.0 and earlier, per a GitHub comment) held
  the caller for several seconds even inside `beginNoBlock()`; the watchdog is
  fed around the call, but if you see short stalls at connect time, check the
  core version.

Pi and dashboard:
- **The 800x480 dashboard on the real touch screen:** touch ergonomics, the
  Chromium kiosk, and Pi 3 performance. (Layout was checked headlessly.)
- **Hotspot stability across reboots.** The cause of the earlier
  start/vanish/rejoin-home-WiFi flapping was not pinned down; the single-profile
  recipe above is the recommended fix, not a confirmed one.

Laser:
- **Everything after the first bench round** (calibration flags, `ramp`, `show`,
  the OSC input, the corrected Pi build line) has only been run against a mock
  Helios library, a real SDK build with no DAC attached, and rendered previews.
- Whether the diode-threshold explanation for `sweep`/`rays` is right; `ramp`
  measures it.
- The CUBE's own ILDA scan size/amplitude and its safe `--pps`.

**Resolved, not concerns (kept here briefly since they looked like real
problems at first):** FastLED's `"Forcing software SPI"` build message is
about a code path (clocked SPI chipsets) we never use -- WS2812B goes
through FastLED's separate clockless controller, and RP2040 is confirmed
(FastLED's own docs) to re-enable interrupts between pixels rather than
holding them off for the whole frame. The SH1107's `(64, 128)`
constructor argument order plus `setRotation(1)` in `display.cpp`'s
`begin()` are a real, confirmed-working pair for this exact 128x64
panel, not a mismatched typo. The pod's WiFi freeze came from the blocking
`WiFi.begin()`, not from the timeout logic around it. The dashboard going blank
on the hotspot was its CDN dependency. See `context.md` for each story.

## Deliberately deferred (not in this iteration)

- **The hub as one program:** OSC in -> sync -> visuals -> laser out, with the
  dashboard showing the sync and a sync slider on the touch screen. The
  pieces exist; they are not joined.
- **Beat events from the pods** (a small firmware addition: send a message at
  each detected beat). Without it, "sync" can only mean similar heart *rates*;
  with it, the laser could fire on the musicians' actual heartbeats and measure
  real beat-phase coherence.
- **Pi boot automation:** starting the dashboard (and later the hub) at power-on,
  and the Helios library build + udev rule on the Pi.
- **USB MIDI** -- removed while debugging an unrelated CLI issue (see
  `context.md`); not yet reinstated.
- **Wired telemetry path** -- discussed (a WIZnet W5500 Ethernet module
  running the same OSC/UDP code, just swapping `WiFiUDP` for
  `EthernetUDP`) but not implemented. The CLI's `stream on` over USB is
  the current zero-extra-hardware fallback for bench use.
- **Per-pod physical labeling/workflow** for assigning and tracking which
  of the 6 physical pods has which `net pod <id>` -- currently manual and
  easy to get wrong across 6 units.
- **Menu grouping/submenus** -- see "On-pod display and menu" above.
- **A reset-reason guard** (skip auto-connect after a watchdog reboot, so a
  pathological WiFi hang could never cause a reboot loop). Not built:
  `rp2040.reboot()` also goes through the watchdog, so the reset reason may not
  tell the two apart.