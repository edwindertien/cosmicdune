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
stands. **`SETUP.md` is the step-by-step checklist for bringing up one pod and the
Pi** (hotspot, dashboard, laser).

**Status tags in this file.** *(confirmed)* = seen working on the real
hardware. *(untested)* = written and checked only in simulation, against a
mock, or in a headless browser -- treat as bring-up, not as known-good.

**Everything must work with no internet at the venue.** Nothing is loaded
from a CDN or font host. Things that need internet *once* (to build or
install, never at run time): PlatformIO libraries, `pip install`, the Helios
SDK download, and on the Pi `apt install libusb-1.0-0-dev g++`.

## Repository layout

```
cosmic-dune-pod/             (the repository root: the folder with platformio.ini, src/ and tools/)
  .gitignore                 keeps the per-machine files out of git (venv, Helios library, caches)
  platformio.ini
  README.md  SETUP.md  context.md    SETUP = step-by-step bring-up: one pod + the Pi
  src/                       pod firmware (Raspberry Pi Pico W)
    config.h  main.cpp
    drivers/  core/  net/  io/
  tools/
    requirements.txt         python-osc, flask
    osc_test_hub.py          terminal monitor + fake pods (command line)
    pod_simulator.py         six fake pods with a small browser UI (BPM/GSR sliders, presets)
    osc_web_dashboard.py     THE HUB: dashboard + laser visualiser + laser controls (Flask)
    chart.umd.js             Chart.js 4.5.1, bundled so the dashboard works offline
    laser/                   Helios DAC -> ILDA laser tools
      laser_test.py  patterns.py  visuals.py  helios_dac.py
      engine.py              the laser engine the hub runs (frames -> DAC + browser)
      lib/                   the Helios shared library (you add it, see "Laser tools")
```

## One branch for Mac and Pi

**There are no Mac and Pi variants of the code, so there is no reason for two branches.** The only
OS-dependent logic is automatic: `helios_dac.py` looks for `libHeliosLaserDAC.dylib` on macOS and
`libHeliosDacAPI.so` on Linux, and the hub's address listing uses the Linux `ip` command with a
fallback elsewhere. Everything else is plain Python (3.9 or newer) and runs the same on both.

What IS per machine -- and therefore never committed (see `.gitignore`):

| per machine | Mac | Pi |
|---|---|---|
| Python virtualenv | `tools/.venv`, made on the Mac | `tools/.venv`, made on the Pi (`pip install -r requirements.txt`) |
| Helios library in `tools/laser/lib/` | the two `.dylib` files from the SDK | a `libHeliosDacAPI.so` you build on the Pi |
| how you start the hub | usually plain `python osc_web_dashboard.py` (preview) | `--laser --source live --rgb-min ... --auto-disarm 0` for the show |
| OS setup | none | hotspot, udev rule, kiosk browser (SETUP.md) |

Keep the Pi's start command in a small shell script on the Pi (outside the repo, or untracked), so
calibration values like `--rgb-min` live next to the machine they were measured on.

**Moving changes from the Mac to the Pi:** edit and test on the Mac, then run the `rsync` command from
SETUP.md step 1 (it skips the per-machine files and never deletes on the Pi), or `git pull` on the Pi.

**Tracking what changed.** Put the folder under git once, *before* copying in any new files:
```
git init && git add -A && git commit -m "baseline"
```
After that, anything copied in (from me or from you) shows up as exactly what it is:
`git status` lists the touched files and `git diff` shows the lines. Commit when it works
(`git commit -am "..."`), `git log --stat` is the history, and `git checkout -- <file>` undoes a bad
copy. Hardware variants (e.g. a Pico 2 W instead of a Pico W) belong in `platformio.ini` as separate
`[env:...]` sections, not in a branch.

## How the pieces connect

```
pods (Pico W) --WiFi, OSC over UDP :9000--> Raspberry Pi, hosting the WiFi network (10.42.0.1)
                                             '- osc_web_dashboard.py  = THE HUB, one process:
                                                  OSC in -> pod state -> laser engine -> frames
                                                  |- web page :5000 (800x480): visualiser + pods + controls
                                                  '- (with --laser) Helios DAC --ILDA--> Laserworld CUBE 3
```

`osc_web_dashboard.py` is now the operating console: it receives the pods, runs the
laser engine (`laser/engine.py`) and serves a page that shows **exactly the frames
the engine hands to the DAC**. Without `--laser` it only *previews* (nothing is sent
anywhere), which doubles as a **dummy mode** for trying patterns with no pod and no
laser.

`laser/laser_test.py` stays as the bench/calibration tool (first light, `ramp`,
`--blank-shift`, ...). **Only one program can hold the USB DAC**: stop the hub
before using `laser_test.py` with the laser, and vice versa. `laser_test.py show
--dashboard` still works against a hub running in preview mode (it reads the hub's
`/api/summary`).

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

### The hub: `tools/osc_web_dashboard.py` (dashboard + laser visualiser + controls)
One process: receives the pods' OSC, runs the laser engine, serves one page built
for the 800x480 touch screen. *(runs on a Pi 3 and a Pi 4 with the real Helios + CUBE 3, as reported;
touch ergonomics on the real 800x480 screen not confirmed; the automated checks use a headless
browser at exactly 800x480 against a mock DAC and simulated pods)*

```
cd tools
python osc_web_dashboard.py                  # PREVIEW: visualiser + dashboard, nothing sent to any laser
python osc_web_dashboard.py --laser          # also drives the Helios DAC (starts SAFE; ARM on screen)
python osc_web_dashboard.py --laser --source live --rgb-min 0.25,0.4,0.15 --auto-disarm 0
# the command that has run steadily on a Pi 4 + Helios + CUBE 3 (all options are explained below; pass them yourself,
# the hub's own defaults are the cautious ones):
python osc_web_dashboard.py --laser --usb-polling timed --intensity 0.4 --max-intensity 0.8 --allow-high \
       --usb-log-file usb.log --auto-disarm 0 --rgb-min R,G,B
# + --usb-recover-s 90   to let the hub reopen a DAC that stopped answering (new, not yet confirmed on the real DAC)
```
Run it as a script, **not** with `flask run` (the OSC listener and the engine are
started by the script). The page works from `http://<the Pi>:5000`. Calibration and
shape options are the same as `laser_test.py` (`--size --intensity --pps --rgb-min
--blank-shift --settle --travel-step --flip-x ...`). It needs the `laser/` folder
next to it; without it the pods and graphs still work and the page says the
visualiser is unavailable.

**Screen layout.**
- **Left, always:** the *visualiser*, a live drawing of the frames the engine sends
  to the DAC (same points, after calibration: orientation, `--rgb-min`, blank
  shift), with a persistence fade. Chip: `PREVIEW`, `LASER DARK` (safe), `ARMED` or
  `LASER ERROR`, plus the source. `ZOOM` magnifies a small pattern. A big red
  `BLACKOUT` appears on the visualiser whenever the laser is armed.
- **Right, tab PODS:** six compact tiles (BPM, GSR meter in the LED strip's colours,
  online / `no pulse` / `OFFLINE` / never heard). The same six show as pills in the
  header; tap either for the full-screen detail (BPM and GSR graphs for the last
  60 s, IBI, raw GSR, IP, time since the last packet). Pod ids above 6 are received
  but not shown.
- **Right, tab LASER:** source `LIVE | FILL | DEMO`; 15 buttons (5 scenes, 10 test
  patterns); sliders `SYNC` (with `AUTO | HOLD`), `SIZE`, `LEVEL`; `ARM` and `BLACKOUT`.

**Dummy mode.** Source **DEMO** feeds the scenes with six simulated pods (they
scatter, converge and scatter again every 90 s); the test patterns need no pods at
all. So everything can be tried with no pod connected, and with no laser at all.
**FILL** uses the real pods where they are online and simulated ones elsewhere (one
real pod among six: set it with `net pod 1`); **LIVE** uses only real pods (an idle
ring when none). Start in a given mode with `--source`. SYNC: in **AUTO** the slider
is an *assist* on top of how alike the pods' BPMs are (the label shows
`slider > effective`); in **HOLD** the slider *is* the sync. For the `rays` pattern
the slider is its sync.

**Safety model** (the page is a remote control for a laser, so the server decides):
- No `--laser`: PREVIEW only; no DAC is opened.
- With `--laser`: starts **SAFE** (dark). Frames reach the DAC only while **ARMED**;
  ARM needs **two taps** (the first only asks, and expires after 3 s). BLACKOUT is
  immediate and disarms.
- `LEVEL` and `SIZE` are clamped **on the server** to `--max-intensity` (default 0.3;
  above 0.5 needs `--allow-high`) and `--max-size` (default 0.6); the smallest
  accepted size is 0.10. A client asking for more still gets the limit.
- **Who may control:** in preview anyone (it only changes a drawing). With `--laser`
  only the hub's own machine (the Pi's touch screen); other devices on the hotspot
  get a view-only visualiser, because the hotspot is open. `--allow-remote-control`
  overrides this -- do not use it on an open network.
- Auto-disarm after `--auto-disarm` minutes (default 20; 0 = never) without any
  control activity; **set `--auto-disarm 0` for a show**, or it will go dark mid-show.
- USB trouble: a transient error (timeout, busy ...) is ridden out for `--usb-grace-ms` (default
  1000) and counted. An outage longer than that, or one the SDK gives up on, disarms and latches
  an error (restart the hub) -- unless you started with `--usb-recover-s`, which keeps the laser
  dark, reopens the DAC and resumes if still ARMED (see "Feeding the DAC" below). Every frame is
  still validated, and the DAC runs in fail-dark `SINGLE_MODE`. Control requests from the
  page are sent strictly in order (an old ARM can never overtake a newer BLACKOUT).

**Minimum pattern size (`--min-extent`, OFF by default; 0.20 is the value to try).** When set, the scenes never get narrower than this lit
width (the full scan field is 2.0 wide, so 0.20 is 10 % of it): when a pattern would be smaller, the
*whole pattern is enlarged* for that frame, smoothly, and never beyond the full field. It matters most at
**high sync**, when all six pulses hit their "dot" together. Reason: Laserworld's FAQ says the scan-fail
safety of their lasers can block "a single beam or a very small pattern" (general to Laserworld, not a
CUBE 3 manual; *unverified* for your unit), and a small pattern also concentrates the beam. Only scenes are
affected; test patterns are drawn as asked. The price is a flatter pulse: at SIZE 0.3, `converge` at max
sync pulses 0.20-0.35 wide (x1.7) instead of 0.084-0.35, a lone pod's spoke or ring hardly pulses, while
`merge`, `ripple`, `bloom` and `spokes` keep x2.8. Raise it (e.g. `--min-extent 0.3`) if cut-outs persist,
raise SIZE if you want the pulse back, `0` turns it off. The two-line readout under the visualiser shows
what is really happening: line 2 is `ext` (the lit width now), `(xN)` when it was enlarged, and `rgb` (the
peak 8-bit drive of each colour channel; a channel near 0 is why a colour is missing).
*(Not yet tried on the real laser. In the one long run so far the converge scene's small moments showed up as a
few re-sent frames, not as cut-outs -- see "Feeding the DAC".)*

**Restarting the hub** resets what you chose on screen to the start-up defaults: source
**DEMO**, scene `converge`, SYNC 0 in AUTO. To come back in another state, say so in the start
command (`--source live` or `fill`, `--scene`, `--sync`). A browser tab left open recovers by
itself after a restart (no refresh needed); the pods and the picture resume.

**Offline:** it loads nothing from the internet. `chart.umd.js` must sit next to the
script; if it is missing, tiles and numbers still work and the graph area says so.

**How in sync is the picture?** The browser is shown the engine's newest frame,
pushed as soon as it exists. In the (loaded) test VM the drawing was at the engine's
frame at the median, within 2-3 frames (60-90 ms) 90 % of the time, with occasional
spikes of 4-6 frames. The DAC plays a frame about one frame after the engine
produces it, so expect the picture and the laser to differ by tens of milliseconds
at most. *(measured in the test VM only, not on a Pi)*

For debugging with curl: `/api/summary`, `/api/pod/<id>`, `/api/data`, `/api/info`,
`/api/laser/state`, `/api/laser/frame.bin?since=<seq>` (8-byte header + 8 bytes per
point, the same x12/y12/r/g/b the DAC gets), `POST /api/laser/control` (JSON, needs
the header `X-Cosmic: 1`).

### Feeding the DAC: the info line, pacing, USB errors, recovery

**The info line** (two lines under the visualiser) -- what the numbers mean:

| field | meaning |
|---|---|
| `pts`, `ms`, `fps` | the current frame: points, how long the DAC plays it, frames per second |
| `load` | the share of a frame's duration the computer needs to *build* the next one (100 % = it cannot keep up) |
| `sent` | frames handed to the DAC so far; it must keep rising while armed |
| `rep` | frames **re-sent** because the next one was not ready in time: the picture holds for one frame and the laser stays lit. Not a missed frame. A few per thousand are normal; they cluster when frames get short (a fully converged `converge` frame is 19 ms instead of 27-30 ms, so the builder has about 30 % less slack) |
| `dark` | times the output stopped because no new frame arrived for `--max-stale-ms` (200): a stalled computer goes dark, never a frozen picture |
| `usb err`, `reconnects` | transient USB errors survived; times the DAC was reopened by `--usb-recover-s` |
| `refused` | frames the safety check refused (the laser goes dark at once; the reason is shown on the picture) |
| line 2: `ext`, `rgb`, `pods` | lit width of the pattern now (full field = 2.0; `(xN)` = enlarged by `--min-extent`); the peak 8-bit drive of each colour channel; pods online / live |

**How frames reach the DAC.** Two threads. The *producer* builds frames (pods -> scene -> render ->
validate), publishes them to the browser and leaves the newest in a one-slot mailbox; it never touches
the DAC. The *writer* is the only thread that does: if the producer is late it re-sends the last frame
(`rep`), and if nothing new arrives for `--max-stale-ms` it stops the DAC (dark). Blackout, disarm and
error handling are acted on by the writer within a frame. Before this split a late frame on a slow
computer was a dark gap (the Helios plays each frame once); on an emulated Pi 3 the old loop was dark
60 % of the time, the new one 0.2 %.

**Pacing: `--usb-polling`.** `adaptive` (the default) and `fast` ask the DAC "ready?" over USB before
each frame. **`timed`** never asks: it writes each frame on a fixed schedule (the frame's own duration
plus `--frame-gap-ms`, default 1.5, so the DAC idles about 5 % of the time between frames and you give
up about 5 % of the light). `timed` has no queue to absorb a late wake-up, so lateness becomes dark time
(5.7 % for a realistic busy computer in the model, about 19 % in an extreme case).

**What was measured on a Raspberry Pi 4 + Helios** (the DAC at 12 Mbit/s behind the Pi's 480 Mbit/s hub,
shared with a 1.5 Mbit/s mouse; throttling, voltage, heat, CPU and kernel log all clean):
- polling the DAC while idle (no frames): 1,724 polls, no error.
- streaming dark frames **and polling before each frame**: the DAC stopped answering after 11 s and 9 s, for
  2.4 s each time, then recovered by itself. The engine, retrying every 5 ms, made 50 failed calls in a row
  within about a second -- which is when the SDK closes its own handle ("Closing Helios DAC: too many
  errors", then `-1000` for good). The retries now back off (5, 10, 20, 40 ms).
- streaming the same frames **on a fixed timer without polling**: no failure in 40 s; the hub then ran 5 minutes /
  10,000 frames with 12 re-sent. After 10-15 minutes a `-1002` outage still happened: `timed` made it much
  rarer, not impossible.
- the Mac with the same DAC shows the same kind of timeouts (about 7 per minute while polling constantly) but
  recovers within ~0.2 s; the Pi's lasted seconds.
- **Not known:** why the DAC goes silent; whether the hub's shared transaction translator matters (the mouse
  was never unplugged for a test); `-1002` is the SDK's own "USB control transfer failed" and hides libusb's
  reason. Frame size is not implicated (the failing frames were 560, 571 and 591 points).

**USB errors in the hub.** Transient errors (`-7` timeout, `-1002` control transfer failed, busy, interrupted)
are ridden out for `--usb-grace-ms` and counted. Every one is written, with context, to `--usb-log-file`: which
call, how long after the last frame, the frame size, how many in a row, and each recovery; the terminal
shows a rate-limited copy. **A log that holds only its start-up header means there was no USB trouble.**

**Recovery: `--usb-recover-s SECONDS`** (off by default; *new, only tested against a model and a mock DAC, not yet on
the real one*). When errors outlast the grace period, or the SDK has closed the device, the writer keeps the laser
dark, leaves the DAC alone, reopens it in place (backoff 0.5 s doubling to 5 s), checks that it answers a status
request (safe: it is idle) and resumes if the laser is still ARMED. After the limit it gives up: error latched, SAFE,
"replug the DAC or restart the hub". BLACKOUT or DISARM during an outage cancels the resume; errors a reopen cannot
fix (e.g. access denied) still latch at once. The page shows `DAC not answering: laser DARK, reconnecting (3.2 s of 90)`.
It is opt-in because it lets the laser come back on without a tap.

### Test tools: `tools/osc_test_hub.py`
`pip install -r tools/requirements.txt`, then:
- `osc_test_hub.py monitor` -- terminal table of live pods, with each of this
  machine's addresses and the matching `net hub` command.
- `osc_test_hub.py simulate --pods 6` -- fake telemetry for N pods, so the
  dashboard (and the laser tool's `show --osc-port`) can be tried with no
  hardware. Both listen on the same default port, so run one listener at a time.

### Pod simulator: `tools/pod_simulator.py`
Six fake pods with a small UI (`http://127.0.0.1:5100`, fits 800x480), sending real
UDP/OSC to the hub, so you can **steer the heart rates by hand** and watch the hub's
LIVE source, the measured sync and the laser visuals respond. It sends exactly what the
firmware sends (`/pod/<id>/bpm`, `ibi`, `signal`, `gsrRaw`, `gsr`, same types, 15 Hz).
*(tested against the real hub and a headless browser; not against real pods side by side)*

```
python3 pod_simulator.py                    # hub on this machine, UDP :9000
python3 pod_simulator.py --host 10.42.0.1   # hub on the Pi, simulator on your Mac (same hotspot)
python3 pod_simulator.py --off 1            # pod 1 is a REAL pod: don't simulate it
```
- Per pod: **ON/OFF** (OFF = stops sending, so the hub shows `OFFLINE` after 3 s), a **BPM**
  slider (30-200), a **GSR** slider, and **PULSE / NO PULSE** (sends `signal 0`; the BPM keeps
  being sent, as on a real pod).
- **CONVERGE** glides all pods to their common mean over `GLIDE` seconds (0 = instant);
  **SCATTER** returns them to a 56-102 spread; **WANDER** adds slow drift (about 2 BPM,
  0.08 GSR); **ALL ON / ALL OFF**; **PAUSE**; target host/port/rate (a bad host shows an error).
- In the hub choose source **LIVE** (or **FILL** to mix with real pods) to see the simulated
  pods drive the scenes and the sync.
- **No IP spoofing is needed.** The hub identifies a pod by the id in the OSC address, not by
  the sender's IP; it only records the IP to show in a pod's detail view. All six simulated
  pods therefore come from one address (and show the same IP). `--distinct-ips` sends pod N
  from `127.0.0.<N+1>` so each tile shows its own address -- cosmetic, only for a hub on the
  same machine, works out of the box on Linux; on macOS it needs `sudo ifconfig lo0 alias
  127.0.0.2` ... `.7` first, otherwise it falls back to one address *(macOS untested)*.
- Real pods use the same ids 1-6: switch the matching simulated pod OFF (or `--off`) while a
  real pod is connected. The UI listens on this machine only unless you pass `--bind 0.0.0.0`.

## Laser tools: `tools/laser/` (Helios DAC -> Laserworld CUBE 3 over ILDA)

The bench/calibration tool: standalone, no sensors, no network, no extra Python
packages (only `python-osc` for `show --osc-port`). Runs the same on a Mac and on
the Pi. The hub above runs the same patterns and scenes with a browser front end.
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
- `--min-extent X` (default 0.2): scenes are enlarged whenever the lit pattern would be narrower than X
  (full field = 2.0). Hub and `laser_test.py show` both honour it; `0` = off.
- Orientation: `--flip-x --flip-y --swap-xy --rotate DEG --offset-x/-y`.

### Pod visuals: `show <scene>` *(untested on the real laser)*
Six pods become one pulse. Each pod has a heartbeat oscillator at its own BPM;
a single number, **sync** (0..1), pulls them toward one shared phase and shape.
A beat swells fast and relaxes slowly: a ring goes from a small circle ("dot")
to a big one and back.

| Scene | sync 0 | sync 1 |
|---|---|---|
| `converge` | six small pulsing rings round a hexagon, each in its pod's colour | all slid to the centre and fused into one centre spot, whose pulse grows |
| `merge` | six small rings round a hexagon, each in its pod's colour | one big ring pulsing as one |
| `bloom` | one ring in six sectors, one per pod (pod 1 at the top, clockwise) | a single round ring |
| `ripple` | six concentric rings | one ring |
| `spokes` | ragged six-ray starburst | symmetric star |

```
python3 laser_test.py preview scenes --t 6      # PNG: every scene at sync 0..1
python3 laser_test.py show converge --dry-run   # no laser, no DAC
python3 laser_test.py show converge             # demo pods; ENTER to arm
python3 laser_test.py show converge --dashboard # real pods, read from the running dashboard
python3 laser_test.py show converge --dashboard --fill-demo   # one real pod (net pod 1) + five simulated
python3 laser_test.py show bloom --sync 0.6     # fix the sync to tune a look
python3 laser_test.py show merge --osc-port 9001   # alternative: listen to the pods directly
                                                # (only while the dashboard is NOT running)
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

### Is the USB link healthy? `probe`, `usbtest`, `streamtest`, `bench`, `diag`

All of these keep the output **dark** (no light is emitted) and need the DAC to themselves: stop the hub first.

| command | what it tells you |
|---|---|
| `probe` | finds the DAC and reads its name and firmware (control path only). A name of `Unknown Helios 00` means the DAC is not answering right now |
| `usbtest [--seconds 10 --interval-ms 3]` | asks "ready?" in a loop with **no frames at all**. Clean = the control path is fine; failing = the USB path, the DAC or the SDK build, not frames or polling modes |
| `streamtest --mode poll\|timed [--poll-at F]` | streams dark fixed-size frames without the engine; at the first failure it leaves the DAC alone and measures how long it stays silent and whether it recovers unaided (reopening it if the SDK closed it). `timed` never polls; `--poll-at 0.5` adds one poll per frame at that point in the frame |
| `bench [scene] --usb-polling adaptive\|fast\|timed` | the hub's real pipeline with the colours forced to zero: load, share of frames delivered, USB errors per minute, time of the first error, longest error run, error kinds and events, and a verdict: `keeps up`, `does NOT keep up`, or `UNRELIABLE` (never "keeps up" when no frames got through). USB errors never latch inside the bench |
| `diag [--seconds 60]` | everything above in one go, written to one small file (under 24 KB, summary first): system facts (board, OS, clock, throttling and under-voltage history), the DAC's hub and what else shares it, the SDK library's fingerprint, three probes, idle polling at 3 and 30 ms, two stream tests, interleaved bench rounds (`--bench-modes adaptive,timed`) and the relevant kernel log lines. It waits for the DAC to answer again between stages and reports how long that took |

To get a report to someone else: stop the hub, run `python laser/laser_test.py diag --seconds 60` on the Pi, copy the
`diag_<host>_<time>.txt` to your Mac (`scp pi@<address>:<path> .`) and attach the file. `--libusb-debug 4 --seconds 10
--rounds 1 --idle-seconds 5` adds libusb's own transfer status codes (-71 bus error, -32 stall, ...) without root;
`--dry-run` tries the tool without a DAC.

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

Pi and hub:
- **The 800x480 hub on the real touch screen:** touch ergonomics and the Chromium
  kiosk. Layout, clicks, drags, ARM/BLACKOUT and the control-ordering fix are checked in a
  headless browser against a mock DAC. On a Pi 3 the engine alone "keeps up" (`bench`), but the
  whole hub with polling pacing showed 170-240 % load; the Pi 3 with `timed` pacing has not been
  measured. A Pi 4 runs the hub at about 45 % load (as reported).
- **The hub driving the real DAC** works on a Pi 4 + Helios + CUBE 3 in `timed` mode (5 minutes, 10,000
  frames, 12 re-sent). Not confirmed: runs longer than 10-15 minutes without a USB outage,
  `--usb-recover-s` on the real SDK (the reopen is tested only against a model and a mock),
  the auto-disarm on the hardware, and `--min-extent`. The cause of the rare DAC silences is unknown.
- **Hotspot stability across reboots.** The cause of the earlier
  start/vanish/rejoin-home-WiFi flapping was not pinned down; the single-profile
  recipe above is the recommended fix, not a confirmed one.

Laser:
- **Everything after the first bench round** (calibration flags, `ramp`, `show`,
  `converge`, the OSC and dashboard inputs, the corrected Pi build line) has only been run against a mock
  Helios library, a real SDK build with no DAC attached, and rendered previews.
- The diode-threshold explanation for `sweep`/`rays`: in practice low drive was the cause (all
  colours respond at LEVEL 0.4); the per-channel drive is on the `rgb` readout.
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

- **Hub polish:** the hub exists (dashboard + engine + visualiser + controls) and runs on a Pi with the real laser. Not built: saving the chosen scene/levels across
  restarts, a sync *measurement* that uses beat events (below), and any
  authentication beyond "controls only from this machine".
- **Beat events from the pods** (a small firmware addition: send a message at
  each detected beat). Without it, "sync" can only mean similar heart *rates*;
  with it, the laser could fire on the musicians' actual heartbeats and measure
  real beat-phase coherence.
- **Pi boot automation:** starting the hub at power-on, and the Helios library build
  + udev rule on the Pi.
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
