# wiifat

Turn a Wii Balance Board into a calibrated, multi-user bathroom scale with
automatic logging and a live web dashboard — on stock Linux, with no extra
hardware. The board pairs over Bluetooth, weigh-ins are captured
automatically when someone steps on, recognized users are assigned by a
Bayesian model, and weight history renders as per-user trend charts. The
board also remains usable as a regular input device (e.g. a real Balance
Board in Dolphin).

## Features

- **Automatic scale**: step-on detection, stable-window capture, dynamic
  tare, SQLite logging — no buttons, no app on your phone.
- **Real calibration** (`wiifat calibrate`): per-cell gains and offsets from
  one small kitchen-scale-verified mass plus two unweighed heavy objects
  (water jugs). Recovers offsets that taring cannot see — including cells
  whose readings clip to zero. Math and identifiability analysis in
  [docs/calibration.md](docs/calibration.md).
- **Multi-user**: named users with per-user colors; weigh-ins auto-assigned
  by a Bayesian recognizer (per-user weight belief + an explicit
  unknown-person hypothesis), ambiguous ones queue for a one-tap claim.
- **Live dashboard** (`wiifat serve`): readiness and measurement progress in
  real time over SSE, weigh-in results as they happen, per-user EWMA trend
  charts with kg/week slopes, battery status, JSON API.
- **Battery-aware**: the daemon powers the board off (Bluetooth disconnect)
  15 s after a weigh-in or 5 min of inactivity; the board's power button
  wakes it. Battery percentage is logged with every measurement.
- **NixOS module**: `services.wiifat` runs the whole thing as a systemd
  service.

## How it works

```
Balance Board ──BT──▶ BlueZ (wiimote plugin handles Nintendo pairing)
                         │
                         ▼
              kernel hid-wiimote driver
                   │              │
                   ▼              ▼
        /dev/input/event*    /dev/hidraw*
        (this project)       (Dolphin "Real Wii Remote")
```

The kernel driver applies the board's factory calibration and exposes the
four load cells as evdev ABS axes in units of 1/100 kg at ~70 Hz. wiifat
reads those, applies its own calibration on top (factory offsets can be off
by kilograms — see below), and runs a state machine that turns frames into
logged measurements.

## Requirements

- A Wii Balance Board (RVL-WBC-01) with batteries.
- Linux with the `hid-wiimote` kernel module and BlueZ built with its
  wiimote plugin (both standard in mainstream distro kernels/packages).
- Read access to `/dev/input/event*` for the user running wiifat — being in
  the `input` group is the usual way.
- Nix for the dev shell and package build (`nix develop` / `nix build`);
  dependencies are plain Python (evdev, flask, matplotlib) if you prefer to
  package them yourself (see `pyproject.toml`).

## Quickstart

### 1. Pair

```sh
./scripts/pair.sh
```

Press the red SYNC button inside the battery compartment while it scans
(LEDs blink ~20 s per press; press again if needed). The script pairs,
trusts, and connects; from then on the board's front power button
reconnects it. If the connect is rejected, BlueZ ≥ 5.62 defaults to
`ClassicBondedOnly=true` in `/etc/bluetooth/input.conf`, which some Wii
peripherals trip over — set it to `false` (NixOS:
`hardware.bluetooth.input.General.ClassicBondedOnly = false;`).

### 2. Sanity-check

```sh
nix develop
python -m wiifat monitor
```

Live readout of all four cells plus the total. Put a known weight on the
board; expect the total to be in the right neighborhood but potentially
biased — that's what calibration fixes.

### 3. Calibrate

```sh
python -m wiifat calibrate --check
```

You need: one precisely known mass (anything your kitchen or coffee scale
can verify — sub-kilogram is fine) and two heavy unweighed objects (full
water jugs work) that each engage all four cells when centered. The guided
flow takes about ten minutes and prints fitted gains, offsets, the inferred
jug weights, and warnings if the session is under-constrained. `--check`
ends with you standing on the board to compare raw vs corrected readings.
Why this procedure and not "put a weight on each corner": see
[docs/calibration.md](docs/calibration.md).

### 4. Run

```sh
python -m wiifat scale          # headless logging daemon
python -m wiifat serve          # daemon + web dashboard (default 127.0.0.1:8480)
python -m wiifat log            # recent measurements
python -m wiifat chart          # weight-history PNG
```

Daily use: press the board's power button, step on, walk away. The
dashboard shows readiness ("step on to weigh"), a live stability progress
bar while measuring, and the result with its user assignment; the board
powers itself off shortly after.

## NixOS deployment

```nix
# flake input: wiifat.url = "github:<you>/wiifat"; or import nix/module.nix
# directly from a checkout — it callPackages the build from your nixpkgs.
imports = [ wiifat.nixosModules.default ];
services.wiifat = {
  enable = true;
  host = "0.0.0.0";        # expose to LAN (default 127.0.0.1)
  openFirewall = true;
  # user / dbPath / calibrationPath configurable; defaults run as a
  # DynamicUser with state under /var/lib/wiifat
};
```

The unit gets `SupplementaryGroups = [ "input" ]` for evdev access and can
bind privileged ports (`port = 80`) via an ambient capability. Fronting it
with an existing nginx as a name-based vhost also works well.

**Security note**: the dashboard is plain unauthenticated HTTP by design —
anyone on your network can see weights and edit users. Keep it on a trusted
LAN, or put auth/TLS in a reverse proxy in front.

## Board behavior worth knowing

- **Power**: the board stays powered exactly as long as a Bluetooth
  connection is held — it has no idle shutoff of its own (on a Wii, the
  console closes the link). Only a Bluetooth-level disconnect powers it
  off; wiifat's daemon does this automatically. Battery level appears at
  `/sys/class/power_supply/wiimote_battery_<mac>/capacity`.
- **Offsets can be large and invisible**: cells report unsigned values, so
  a cell with a negative zero-offset reads exactly 0 until enough load
  covers it. On the development unit one cell hid −2.52 kg this way —
  weight that silently vanished from every uncalibrated reading. Taring
  cannot fix this; calibration can.
- **Steam**: if running, Steam may open the board as a "controller". It
  does not grab it exclusively and doesn't interfere; to hide it from
  Steam, set `SDL_GAMECONTROLLER_IGNORE_DEVICES=0x057e/0x0306`.

## Dolphin

Controllers → Emulate the Wii's Bluetooth adapter → Real Balance Board.
Dolphin talks hidraw, so grant access with a udev rule:

```
KERNEL=="hidraw*", ATTRS{idVendor}=="057e", MODE="0660", TAG+="uaccess"
```

## License

[MIT](LICENSE)
