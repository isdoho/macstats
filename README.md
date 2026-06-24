# macstats

Terminal dashboard for macOS — CPU, memory, power, temperatures, disk I/O,
network, Wi-Fi, processes — all in one screen.

Built for Apple Silicon (M-series). Intel Macs work for most panels, but
temperatures and per-cluster power frequencies are M-series specific.

## Requirements

- macOS (Apple Silicon recommended)
- Python 3.10+
- Xcode Command Line Tools (`xcode-select --install`) for the sensor helper
- A terminal that supports Unicode and 256 colors

## Install

```sh
git clone https://github.com/isdoho/macstats.git
cd macstats
./install.sh
```

The installer will:
1. `pip install --user` the Python deps (`psutil`, `rich`)
2. Compile `macstat-sensors` (Objective-C, reads HID temperature sensors)
3. Symlink `macstat` into `~/.local/bin`

After install:

```sh
macstat
```

Ctrl-C to exit.

## Optional: enable wattage panel

CPU/GPU/ANE/SoC power and thermal pressure come from `powermetrics`, which
requires root. To run it without typing a password each time, add a sudoers
rule (you'll be prompted for your password once):

```sh
echo "$(whoami) ALL=(ALL) NOPASSWD: /usr/bin/powermetrics" \
  | sudo tee /etc/sudoers.d/powermetrics
sudo chmod 440 /etc/sudoers.d/powermetrics
sudo visudo -c -f /etc/sudoers.d/powermetrics   # should print "parsed OK"
```

Without this the Power & Thermal panel still shows temperatures, battery
flow, and disk/network — just no per-component wattage.

## What it shows

- **CPU** — total + per-core, frequency, throttling speed-limit, 60s trend
- **Top processes** — by CPU and by MEM, side by side
- **Memory** — RAM, swap, active/wired/inactive/available, memory pressure,
  usage trend
- **Power & Thermal** — CPU/GPU/ANE/SoC wattage, DCin (computed from energy
  balance), battery ±W (charge/discharge rate), system load, temperatures
  (CPU/GPU/hottest/battery/SSD), cluster frequencies, thermal pressure
- **Disk** — usage per volume, live read/write I/O rate
- **Network** — rx/tx rate, sparkline trend
- **Wi-Fi** — SSID (may be redacted on macOS 14+), IP, RSSI, link rate
- **Battery** — %, charging state, time remaining

## Uninstall

```sh
rm ~/.local/bin/macstat
sudo rm /etc/sudoers.d/powermetrics   # if you added it
# then remove the cloned directory
```

## Notes / known limits

- **Fan RPM** is not available — Apple Silicon requires direct AppleSMC
  IOKit calls, not just HID. See `TODO.md`.
- **Wi-Fi SSID** appears as `<redacted>` on macOS 14+ unless the process has
  Location Services permission. IP, RSSI, link rate may also be redacted.
- **`SystemLoad`** wattage uses an undocumented IOKit field; treat as an
  approximation.

## License

MIT — see source for details.
