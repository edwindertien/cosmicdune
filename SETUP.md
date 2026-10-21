# Bring-up: one pod + the Raspberry Pi (hotspot, dashboard, laser)

A checklist, in order. Each step ends with a **check** -- don't move on until it passes.
`README.md` is the reference; `context.md` explains why things are the way they are.
Tags: *(confirmed)* = seen working on your hardware; *(untested)* = not run on real hardware yet.

**Safety, once, before any laser step:** the CUBE 3 is a multi-watt laser. Indoors, aimed at a
matte wall a few metres away; nobody in the beam path; no mirrors, windows or shiny surfaces; not
upward; the laser's key/interlock within reach. The software fails dark if it crashes, but it
does not replace any of that.

## What you need

- 1 pod, flashed with the **latest** firmware (check in step 3: its CLI help must list `net retry`)
- the Raspberry Pi 3 with the 800x480 touch screen
- the Helios DAC (USB) and the CUBE 3 with the ILDA cable
- **internet, once**, for the Pi: either Ethernet, or do the downloads on your Mac and copy
  them over. The Pi's WiFi radio is the hotspot, so it has no internet over WiFi.

## 1. Put the project on the Pi

There is ONE codebase for the Mac and the Pi -- no branches. What differs per machine is only: the
Python virtualenv, the Helios library file, and the flags you start the hub with (see README, "One
branch for Mac and Pi"). Your project folder is the one containing `platformio.ini`, `src/` and `tools/`.

From that folder on the Mac, copy it to the Pi (works over the hotspot, no internet needed; it skips
the per-machine files and never deletes anything on the Pi, so a library you built there is safe):

```
rsync -av --exclude .git --exclude .venv --exclude __pycache__ --exclude .pio --exclude .DS_Store \
      --exclude '*.dylib' --exclude '*.so' ./ pi@<Pi address>:~/cosmic-dune/
```
(or `git clone` it, or a USB stick). Repeat the same command whenever you have changed something.
Then on the Pi -- the virtualenv is created ON the Pi and never copied:

```
cd ~/cosmic-dune/tools
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt        # needs internet once (Ethernet)
```
**Check:** `python osc_web_dashboard.py --help` prints usage. (The pod firmware needs no change for this
test beyond being up to date; you build and flash it from the Mac as before.)

## 2. The hotspot (you set this up manually)

What "done" looks like -- check each:
- The hotspot is **open** for now (WPA comes later, see the end of this section).
- `nmcli -f NAME,TYPE,DEVICE connection show --active` lists the hotspot on `wlan0`.
- `ip -4 addr show wlan0` shows the Pi's address on it. With NetworkManager's default hotspot it
  is `10.42.0.1` *(confirmed)*; note yours -- it is the **hub address** below.
- There is exactly **one** hotspot profile (`nmcli connection show`), and every other WiFi
  profile on the Pi has autoconnect **off**:
  `sudo nmcli connection modify "<other wifi>" connection.autoconnect no`
- It comes back by itself after `sudo reboot`. If it flaps (appears, vanishes, the Pi rejoins
  your home WiFi): `sudo journalctl -u NetworkManager -f` and read the `reason '...'` on the
  `deactivating` lines.

**WPA later** *(guesses, untested)*: use a password of 8-63 characters; set the *same* one on the
pod (`net pass <password>`, `net save`) and check `net` shows `pass=set`; try making the
hotspot WPA2-only with `wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp`; and
watch the journal while the pod tries to join.

## 3. One pod

Connect the pod by USB, open the serial monitor (`pio device monitor`, 115200), then:

```
help                         # the net section must list `net retry` -- if not, reflash the latest firmware
net ssid <hotspot name>
net pass                     # with NOTHING after it: clears any old password -> open network
net pod 1                    # slot 1 = the top of the hexagon in the laser visuals
net hub <the Pi's address> 9000
net enable
net save
net
```
**Check:** `net` shows `wifi=connected (10.42.0.x)` and `hub=<Pi address>:9000` *(confirmed on
your setup)*. If it says `connecting` for 8 s and then `gave up`, the hotspot isn't reachable:
fix that, then `net retry` (no reboot needed). While it is searching, the LED strip and heart
rate should keep working *(untested with no network in range)*.

Put the ear-clip on and check `status` shows a `bpm`.

## 4. The hub on the Pi -- preview first, no laser

`osc_web_dashboard.py` is the **hub**: it receives the pods, runs the laser engine and serves one
page with the **laser visualiser**, the pod tiles and the laser controls. Without `--laser` it only
*previews*: nothing is sent to any laser, so this step is completely safe.

```
cd ~/cosmic-dune/tools
. .venv/bin/activate
python osc_web_dashboard.py
```
Open `http://localhost:5000` in the Pi's browser (or `http://<Pi address>:5000` from a device on
the hotspot -- from another device it is view-only for the laser, see step 6).

**Check -- pods:** tile **POD 1** (and its pill in the header) goes green and shows your BPM;
tapping it opens the graphs and numbers. The other five are greyed out ("no data").

**Check -- dummy mode (no pod needed):** the left half draws six pulsing rings: that is the
`converge` scene on **DEMO** pods, exactly as it will go to the laser. Tap the **LASER** tab:
- tap pattern buttons (`circle`, `orient`, `colors`, `ramp` ...) and watch the drawing change; `ZOOM`
  magnifies small patterns;
- drag **SIZE**, **LEVEL**, **SYNC** (in `AUTO` the slider assists the measured sync; tap `AUTO` to
  switch to `HOLD` and set the sync directly);
- `LIVE | FILL | DEMO` picks the pods: LIVE = real ones only, FILL = real + simulated, DEMO = all
  simulated. The `ARM` button says `NO LASER` -- correct, there is no laser in preview.

*(the layout is untested on the real 800x480 screen -- tell me what is cramped or too small to hit)*

Once it works in a normal window, full-screen it: `chromium-browser --kiosk http://localhost:5000`
(`chromium` on newer images). If the on-screen keyboard pops up: *Raspberry menu > Preferences >
Control Centre > Display > On-screen keyboard > Disabled*.

**No pods to hand? Simulate six.** In a second terminal on the same machine:
`python3 pod_simulator.py` and open `http://127.0.0.1:5100`: six fake pods with BPM/GSR sliders, ON/OFF,
NO PULSE, and CONVERGE / SCATTER presets, sending real OSC to the hub. In the hub's LASER tab pick
source **LIVE** and drag the simulated BPMs together to watch the measured sync rise. With a real pod
connected as pod 1, start it with `--off 1`. (Simulator on your Mac, hub on the Pi: `--host <Pi address>`.)
No spoofing is involved: the hub tells pods apart by the id in the OSC address, not by IP.

Stop the hub (Ctrl-C) before step 5: the bench tool and `--laser` can't both hold the USB DAC.

## 5. The laser on the Pi

Steps 5b and 5c use the bench tool `laser_test.py` and need the DAC to themselves: the hub must
not be running with `--laser` (a hub in preview is fine).

### 5a. The Helios library (one-time, needs internet)

On a machine with internet get the SDK -- `git clone https://github.com/Grix/helios_dac.git` --
and make its `sdk/cpp` folder available on the Pi (clone on the Pi over Ethernet, or copy it).
Then, on the Pi:

```
sudo apt-get install libusb-1.0-0-dev g++          # needs internet
cd <path to>/helios_dac/sdk/cpp/shared_library
g++ -Wall -std=c++14 -fPIC -O2 -c HeliosDacAPI.cpp
g++ -Wall -std=c++14 -fPIC -O2 -c ../HeliosDac.cpp
g++ -Wall -std=c++14 -fPIC -O2 -c ../idn/idn.cpp
g++ -Wall -std=c++14 -fPIC -O2 -c ../idn/idnServerList.cpp
g++ -Wall -std=c++14 -fPIC -O2 -c ../idn/plt-posix.cpp
g++ -shared -o libHeliosDacAPI.so HeliosDacAPI.o HeliosDac.o plt-posix.o idn.o idnServerList.o -lusb-1.0 -lpthread
cp libHeliosDacAPI.so ~/cosmic-dune/tools/laser/lib/
```
**`-lusb-1.0 -lpthread` must come after the `.o` files** (the SDK's own doc has it before them,
which gives a library that fails to load with `undefined symbol: libusb_open`).

USB permission without sudo: create `/etc/udev/rules.d/90-heliosdac.rules` containing

```
ACTION=="add", SUBSYSTEM=="usb", ATTRS{idVendor}=="1209", ATTRS{idProduct}=="e500", MODE="0660", GROUP="plugdev"
```
then `sudo udevadm control --reload && sudo udevadm trigger`, unplug/replug the DAC, and make
sure `groups` includes `plugdev`. *(the Pi build and udev rule are untested on the Pi; the
build commands were checked on x86 Linux)*

### 5b. No light yet

Plug the DAC into the Pi's USB. Put the CUBE in **ILDA mode** (see its manual) and connect the
ILDA cable. Still laser-dark:

```
cd ~/cosmic-dune/tools/laser
python3 laser_test.py check          # every pattern + scene; look at the "render ms avg/max" column
lsusb | grep 1209                    # the DAC is listed as 1209:e500
python3 laser_test.py probe          # "DAC: ... firmware ..." and "no light was emitted"
```
**Check:** `probe` finds the DAC. In `check`, the **render max** should stay well under ~15 ms --
that is the Pi 3 keeping up *(untested; this is the number to send me)*. If `probe` says "No
Helios DAC found": udev rule / group / cable. If it says `undefined symbol: libusb_open`: the
link order above.

### 5c. First light -- small, dim, on a wall

```
python3 laser_test.py run circle                     # ENTER to arm; stops itself after 60 s
python3 laser_test.py run ramp --seconds 120         # find each colour's threshold
python3 laser_test.py run colors
```
**Check:** the circle appears and is round. `ramp` draws four lines (red, green, blue, white),
each ramping from dark to full left to right, with tick marks at 0/25/50/75/100 %: for each
colour, note the **fraction of the line where it first becomes visible**. Then feed them back:
`--rgb-min R,G,B` (e.g. `--rgb-min 0.25,0.4,0.15`) on every later command. You can also try
`--blank-shift 2` if line starts/ends look wrong, and `--travel-step 0.03 --settle 6` if the
galvos are noisy.

### 5d. Is the USB link healthy? (once, on the Pi, still no light)

The Helios can stop answering over USB while it plays. On a Pi 4 this was measured (see the README, "Feeding the DAC")
and avoided with `--usb-polling timed`. Before relying on a Pi, let the tool look at it. The hub must be stopped; the output
stays dark.

```
cd ~/cosmic-dune/tools && . .venv/bin/activate
python laser/laser_test.py diag --seconds 60          # about 4 minutes; writes diag_<host>_<time>.txt
```
Copy the file to your Mac (`scp pi@<address>:~/cosmic-dune/tools/diag_*.txt .`) and read the SUMMARY at the top (or attach the file
when asking for help). What good looks like: probes `ok`, idle polling `0 errors`, the `timed` stream test `0 failures`, and
`throttled 0x0 (clean)`. If the DAC was just replugged, wait 10-15 s first.

## 6. The six circles converging -- with one real pod, from the touch screen

Make sure `laser_test.py` is not running. Start the hub **with the laser**, using the calibration
you found in 5c (the visualiser then shows the calibrated frames):

```
cd ~/cosmic-dune/tools
. .venv/bin/activate
python osc_web_dashboard.py --laser --source fill --rgb-min R,G,B      # add --blank-shift N etc. as needed
```
On a Pi 4 with the Helios the command that has run steadily is (the hub's own defaults are the cautious ones, so
pass these yourself; `--allow-high` is needed for `--max-intensity` above 0.5):

```
python osc_web_dashboard.py --laser --usb-polling timed --intensity 0.4 --max-intensity 0.8 --allow-high \
       --usb-log-file usb.log --auto-disarm 0 --rgb-min R,G,B
```
Add `--usb-recover-s 90` to let the hub reopen a DAC that stopped answering and carry on if the laser is still armed
*(new; not yet confirmed on the real DAC)*. A `usb.log` that holds only its start-up header means there was no USB trouble.

It prints `LASER: DAC ... open, output SAFE (dark)` and the limits. Then, **on the Pi's own touch
screen** (with `--laser`, other devices get a view-only picture; that is deliberate -- the hotspot is
open):
1. **LASER** tab, `CONVERGE`, source `FILL`. The visualiser shows your pod as slot 1 (top of the
   hexagon, real BPM, colour from its GSR) among five simulated pods. Chip: `LASER DARK`.
2. Wall, ILDA mode, nobody in the beam (see the safety box at the top). Tap **ARM** -- it changes to
   `TAP AGAIN TO ARM` for 3 s -- tap again. The chip turns red `ARMED`, the big **BLACKOUT** button
   appears on the visualiser, the laser draws what you see on screen. **BLACKOUT** darkens it at once.
3. Move **SYNC**: in `AUTO` it assists the measured sync (with one real pod that is mostly the
   simulated pods' doing -- use it); tap `AUTO` -> `HOLD` to set the sync exactly. At low sync: six
   separate pulsing rings; as sync rises they slide to the centre and fuse into **one centre spot**
   whose pulse grows as they lock together.
4. Scenes and patterns switch with the buttons; `LEVEL` and `SIZE` stop at the hub's limits
   (`--max-intensity` 0.3, `--max-size` 0.6 unless you changed them) -- the server enforces them.

The hub disarms itself after 20 minutes without any control activity (`--auto-disarm MIN`; `0`
turns it off -- do that deliberately, for a show). A `LASER ERROR` chip means the DAC stopped answering or was
unplugged and the hub gave up: look at `usb.log`, then restart the hub (or start with `--usb-recover-s` so it tries to reopen
the DAC itself). The info line under the visualiser shows `load`, `sent`, `rep` (frames re-sent because the next one was late:
harmless), `dark`, `usb err` and `reconnects`; the README explains each. The older route still works when the hub runs in *preview*:
`python3 laser/laser_test.py show converge --dashboard --fill-demo`.

## If something doesn't work

| symptom | look at |
|---|---|
| pod `gave up` | hotspot up? SSID spelled right? `net pass` cleared? then `net retry` |
| pod connected, no tile on the dashboard | `net hub` address/port; `net enable`; `net pod 1`; `net save` |
| dashboard blank | open the browser console (F12); `curl localhost:5000/api/summary` shows the pods? |
| `laser_test.py show` says `DASHBOARD UNREACHABLE` | the hub is not running, or a different `--web-port`: use `--dashboard http://127.0.0.1:<port>` |
| `show` status: `late` keeps growing | Pi too slow for the frame rate: raise `--frame-ms 40`, or lower `--size`; send me the `check` timings |
| laser dark, mirrors move | thresholds (`run ramp`, then `--rgb-min`), intensity too low, or CUBE not in ILDA mode |
| mirrors noisy / corners rounded | `--travel-step 0.03 --settle 6`, a lower `--pps` (try 15000) |
| anything odd with the laser | tap **BLACKOUT** (or `b` / `q` in `laser_test.py`); `python3 laser_test.py blank` forces the output dark |
| hub: `NO ENGINE` / "visualiser unavailable" | the `laser/` folder is not next to `osc_web_dashboard.py` (keep the zip's layout) |
| hub: controls greyed, "view only" | you opened the page from another device while the hub runs with `--laser`; use the Pi's own screen |
| hub: `ARM` says `NO LASER` | the hub was started without `--laser` |
| hub: `No Helios DAC found` at start | USB / udev rule / group, or `laser_test.py` is holding the DAC |
| hub: `LASER ERROR` chip | the DAC stopped answering or was unplugged; read `usb.log`; restart the hub, or use `--usb-recover-s 90` so it reopens the DAC itself |
| hub: laser went dark by itself | auto-disarm (default 20 min): re-ARM, or start with `--auto-disarm 0` |
| laser cuts out on small patterns (a lone spoke, a ring at its dot) | read the readout under the visualiser: `refused` up = our safety check, `rep` / `dark` up = the computer stalling, neither = the projector. Then try `--min-extent 0.2` (off by default), raise SIZE, or test a small `circle` at SIZE 0.10 |
| hub: `load` near or above 100 %, or `rep` growing fast | the computer is too slow for engine + web + browser: `--viz-fps 15`, `nice -n 10` for the browser, governor `performance`, `--frame-ms 40`, lower `--size`; `--perf` shows where the CPU goes, `laser_test.py bench` the engine alone |
| hub: `usb err` counts up; `GetStatus failed ... (-1002)` or `libusb error -7` | the DAC stopped answering for a moment: use `--usb-polling timed` (polling during playback is what triggered it), keep `--usb-log-file`, run `diag` and send the file; README "Feeding the DAC" |
| laser dark for a few seconds, then back by itself (with `--usb-recover-s`) | the DAC went silent and the hub reopened it; `reconnects` counts it and `usb.log` has the story |
| `probe` / the hub says `Unknown Helios 00` or `No Helios DAC found` right after a failure | the DAC is still silent: wait ~10 s or replug; `probe` shows when it answers again |
| hub with `--laser`: "the laser code did not load" | a file in `laser/` is older than the others (for example `visuals.py`): copy all the files of one update together |

## Send me, after the first session

1. the `check` table from the Pi (render times); 2. the `ramp` thresholds you read (R, G, B);
3. whether `--blank-shift` helped and which value; 4. the hub's info line under the visualiser
after a minute armed (`load`, `rep`, `dark`, `usb err`); 5. whether the picture on screen matches the beam (shape,
orientation, timing) and whether anything was cramped or too small to hit on the touch screen;
6. whether the hotspot survived a reboot; 7. the `usb.log` after any USB error (a header only is good news), or a
`diag` file (step 5d).
