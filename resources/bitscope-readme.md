# Installing BitScope DSO for the BS05U on Raspberry Pi 3 (armhf)

Notes for getting the BitScope Micro (BS05U) working with BitScope DSO on a
Raspberry Pi 3 running a 32-bit (armhf) OS. bitscope.com is currently down,
so this uses the software mirror hosted on element14.

## 1. Confirm architecture

```
dpkg --print-architecture
```

Should return `armhf`. BitScope's Linux packages are 32-bit ARM only — they
won't run on a 64-bit (arm64) userspace without extra compatibility setup.

## 2. Get the software

bitscope.com is down, so grab the packages from element14 instead:

**[armhf Bitscope Software (element14)](https://community.element14.com/products/manufacturers/bitscope/m/files/150616)**
(a free element14 account may be required to download)

The zip contains:

```
bitscope-server_1.0.FK26A_armhf.deb
bitscope-chart_2.0.FK22M_armhf.deb
bitscope-console_1.0.FK29A_armhf.deb
bitscope-display_1.0.EC17A_armhf.deb
bitscope-dso_2.8.FE22H_armhf.deb
bitscope-logic_1.2.FC20C_armhf.deb
bitscope-meter_2.0.FK22G_armhf.deb
bitscope-proto_0.9.FG13B_armhf.deb
```

For basic scope/logic use, **bitscope-dso** is the only package you need.

Other useful docs on the same element14 support-files page:
- "USB Bitscope installation guide" (PDF)
- "Bitscope DSO user guide" (PDF)

## 3. Install

```bash
unzip armhf-bitscope-software.zip
sudo dpkg -i bitscope-dso_2.8.FE22H_armhf.deb
```

If it complains about missing libraries (common on modern Raspberry Pi OS,
since this build targets older GTK2 libs), pull them in and retry:

```bash
sudo apt-get install -f
sudo dpkg -i bitscope-dso_2.8.FE22H_armhf.deb   # if the first install didn't finish
```

### Known dependency gotcha

If you install the *whole* bundle at once, `bitscope-console` (and
`bitscope-proto`) will fail because they depend on `bitscope-link`, which
isn't included in this armhf zip. Skip those two packages unless you
specifically need them — `bitscope-dso` alone installs cleanly without it.

## 4. Connect and run

```bash
lsusb            # confirm the BS05U shows up once plugged in
bitscope-dso      # launch the app
```

In the app: click the power/plug icon (top right) → select the BitScope
Micro from the list → OK.

No manual driver/udev setup should be needed — the `.deb` installs its own
udev rule for non-root USB access.

## 5. If the device isn't detected

- Re-check `lsusb` after unplugging/replugging.
- If it shows up in `lsusb` but DSO still can't see it, it's a udev/group
  issue:
  ```bash
  sudo usermod -aG plugdev $USER   # then log out and back in
  ```
  Check `/etc/udev/rules.d/` for a BitScope-installed rule; if missing, add
  one manually using the vendor/product ID from `lsusb` and reload udev
  (`sudo udevadm control --reload && sudo udevadm trigger`).

## Optional: headless/remote use

`bitscope-server_1.0.FK26A_armhf.deb` runs the scope as a network service
instead of a local GUI, so you can connect to it remotely from BitScope DSO
on another machine. Same install method (`dpkg -i`), but it doesn't set
itself up as a systemd service — you'd need to configure that separately if
you want it running on boot.
