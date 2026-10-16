# WT9011DCL-BT50 Linux Upper-Computer

**English** | [简体中文](README.md)

A Linux replacement for the WitMotion **WT9011DCL-BT50** BLE 5.0 attitude sensor's
vendor upper-computer. The vendor ships a Windows-only app (`.NET Framework 4.6.2` + WPF,
whose Bluetooth backend calls WinRT `Windows.Devices.Bluetooth`, so it cannot work on
Linux/Wine). This project re-implements the useful parts with
**Python + bleak (BlueZ) + PySide6**.

- Repository: <https://github.com/chenweidu666/APP-WT9011DCL-BT50-Linux>
- Documentation: [APP-WT9011DCL-BT50-Linux-Docs](https://github.com/chenweidu666/APP-WT9011DCL-BT50-Linux-Docs) (private)
- License: [MIT](LICENSE) — independent third-party project, not affiliated with WitMotion

```
┌───────────────┐   BLE 5.0 (GATT)   ┌──────────────────────────────┐
│ WT9011DCL-BT50│ ─────────────────► │ wt9011 (this repo)           │
│ 0x55 0x61 data │  FFE5/FFE4/FFE9    │  protocol → client → cli/gui │
└───────────────┘                    └──────────────────────────────┘
```

## Feature parity (Windows app → this project)

| Windows upper-computer | This project | Notes |
|---|---|---|
| Scan / connect over BLE | ✅ GUI + `wt9011 scan` | matches `WT`/`BWT` names or the FFE5 service |
| Live acceleration / gyro / angles | ✅ | same full scales: 16 g / 2000 °/s / 180° |
| Waveform plots | ✅ pyqtgraph, multi-channel, adjustable window | acc / gyro / angles / position / velocity / magnetometer / quaternion |
| 3D attitude view | ✅ 2D attitude indicator (roll/pitch) | the Windows app uses HelixToolkit 3D |
| Magnetometer, quaternion, temperature, battery | ✅ | magnetometer shown both as raw mG and vendor-display value (`raw/120`) |
| Rate / bandwidth / algorithm / orientation / packet content | ✅ | always "unlock → write → save" |
| Accelerometer calibration, magnetometer calibration wizard | ✅ | mag: enter → rotate about 3 axes ×3 turns → finish & save |
| Yaw zero, angle reference | ✅ | yaw zero requires 6-axis algorithm first (GUI warns) |
| Save config / restore defaults / reboot | ✅ | `FF AA 00 00 00` / `01` / `FF` |
| CSV recording | ✅ | GUI record tab / `stream --csv` |
| Rename BLE device | ✅ | `WT` + name + `\r\n`, needs reboot |
| 3D models, satellite map, vibration analysis, Word export | ❌ | Windows-only extras, not ported |

## Install

Requirements: Linux with BlueZ running and a BLE adapter. **No root needed** for scan/connect.

```bash
git clone https://github.com/chenweidu666/APP-WT9011DCL-BT50-Linux.git
cd APP-WT9011DCL-BT50-Linux

pip install -r requirements.txt      # bleak + PySide6 + pyqtgraph
pip install -e .                     # optional: provides wt9011 / wt9011-gui
```

Or an isolated env: `python3 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt`

## Usage

### GUI

```bash
./run.sh                              # == python3 -m wt9011.gui
./run.sh --addr AA:BB:CC:DD:EE:FF     # connect to a specific device
./run.sh --mock                       # offline demo with simulated data
```

Layout: scan/connect on top, live values + attitude indicator on the left, multi-channel
curves in the middle, "Configuration / Calibration / Record" tabs on the right, log at the bottom.
On startup it auto-connects to the last used device (stored in `~/.config/wt9011/config.json`)
and auto-reconnects (3 s × up to 5 attempts) if the link drops.

### CLI

```bash
./run.sh cli scan                          # list nearby WitMotion devices
./run.sh cli info                          # battery / version / temperature / magnetometer
./run.sh cli stream                        # live print (default 5 Hz refresh)
./run.sh cli stream --csv run.csv --seconds 30
./run.sh cli stream --rate 0x07            # set 20 Hz first
./run.sh cli read RATE                     # read a register (returns 8)
./run.sh cli write RATE 0x08 --verify      # write + read back (auto unlock & save)
./run.sh cli calib acc                     # accelerometer calibration (keep it level)
./run.sh cli calib mag-start               # magnetometer calibration: rotate 3 axes ×3
./run.sh cli calib mag-stop                # finish & save
./run.sh cli calib yaw-zero                # zero the yaw angle
./run.sh cli mode 1                        # output position + velocity + angles
./run.sh cli send "FF AA 27 3A 00"         # raw hex command
```

## Register as an Ubuntu app (optional)

```bash
./install-desktop.sh               # install / update
./install-desktop.sh --uninstall   # remove
```

Creates (all under `$HOME`, no sudo): an "WT9011 上位机" entry in the application menu, an
icon, and `~/.local/bin/wt9011` + `wt9011-gui` launchers. The script picks an interpreter that
has `bleak + PySide6 + pyqtgraph` and bakes it into the launcher, so the menu entry also works
with a minimal PATH.

## BLE / BlueZ troubleshooting

| Symptom | Cause & fix |
|---|---|
| `org.bluez.Error.InProgress` | A half-open connection is pending in BlueZ (usually because the device was clicked in the GNOME Bluetooth panel). The client automatically runs `bluetoothctl disconnect <addr>` and retries; manually: `bluetoothctl disconnect <addr>`, worst case `sudo systemctl restart bluetooth`. |
| Connects once, then `scan` cannot find it | WitMotion BLE modules usually stop advertising after a connection. This client connects **by address without scanning** (`BleakClient(address)`), so it still works; the address is remembered automatically. |
| Nothing works after the app was force-killed | BlueZ keeps a stale `Connected: yes` entry. The client detects and releases it before connecting; manually `bluetoothctl disconnect <addr>`. |
| After a phone app / Windows app / GNOME panel was connected | BLE allows one central only. Disconnect the other side first. |
| Battery shows 0% / 0.00 V | Modules without a battery (or a first read right after connecting) return 0 on register `0x64`; the client retries once. |
| Occasional spikes in the curves | `0x61`/`0x71` frames have no checksum, so a dropped byte can produce implausible values. The client drops out-of-range frames and the GUI offers 3-point median "denoise" for display only. |

More detail (including real-hardware session logs) in the documentation repository's
`05-Linux BLE troubleshooting` document.

## Protocol summary

| Item | Value |
|---|---|
| Service / notify / write | `0000ffe5-…` / `0000ffe4-…` / `0000ffe9-…` |
| Data frame | `0x55 0x61` + 9×int16 LE (acc XYZ, gyro XYZ, angles XYZ), 20 bytes total |
| Register reply | `0x55 0x71` + start register (2 B) + 8×int16 (16 B) |
| Read register | `FF AA 27 <reg> 00` |
| Write register | `FF AA <reg> <valL> <valH>` |
| Unlock | `FF AA 69 88 B5` (valid for 10 s) |
| Save | `FF AA 00 00 00` (`01` restore defaults, `FF` reboot) |
| Scaling | acc `/32768*16 g`; gyro `/32768*2000 °/s`; angles `/32768*180°`; temp `/100 ℃`; quaternion `/32768`; magnetometer raw = mG, vendor display = `raw/120` |

## Code layout

```
wt9011/
├── protocol.py   frames, register map, command encoding, scaling (no third-party deps, unit-tested)
├── client.py     bleak client: scan/connect/notify parsing/register rw/calibration/settings
├── sim.py        simulated device for offline development (--mock)
├── cli.py        command line interface
├── config.py     ~/.config/wt9011/config.json (last device, auto-connect flags)
└── gui.py        PySide6 GUI (asyncio loop runs in a dedicated QThread)
tests/            19 protocol unit tests, assertions taken from vendor document examples
tools/smoke_gui.py  offscreen GUI smoke test (simulated data, screenshot, real close path)
packaging/          app icon (SVG)
```

## Verification

```bash
python3 -m unittest discover -s tests -v            # 19 protocol tests
python3 tools/smoke_gui.py --screenshot /tmp/g.png  # offscreen GUI smoke test
```

- ✅ 19/19 protocol tests (command bytes match the vendor document, magnetometer sample
  `55713A00680169007A00` → 360/105/122, version sample → `10080.1.3`)
- ✅ Verified against real hardware (`WT901BLE67`): scan → connect in ~5–6 s → stable 10 Hz stream
  (`AZ≈1.00 g` at rest) → battery 100 %/4.27 V, firmware `10080.1.22`, temperature, magnetometer
  and quaternion all readable
- ✅ GUI runs on simulated data; CSV recording and window close path both checked
- ✅ CI (GitHub Actions): protocol tests on Python 3.10/3.13 + offscreen GUI smoke test

## Known limitations

1. **BLE only** — the USB-serial/adaptor path is not implemented (same protocol, `pyserial` on
   `/dev/ttyUSB*` would be an easy extension).
2. Magnetometer/quaternion/temperature/battery are polled at 1 Hz (the module never pushes them).
3. Position/velocity depend on the module's internal algorithm and motion conditions.
4. Windows-only extras (3D models, map, vibration analysis, Word export) are not ported.
5. The BLE link is exclusive: phone apps, the Windows app and the GNOME Bluetooth panel will
   block this client until they disconnect.
