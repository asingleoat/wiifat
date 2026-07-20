"""Reconnect-safe named-corner frame source shared by scale workflows."""

from __future__ import annotations

import os
import time
from contextlib import suppress
from pathlib import Path
from typing import Callable, Iterator

from evdev import InputDevice

from .board import CELLS, find_board, read_frames


Frame = tuple[float, dict[str, float]]


def device_mac(
    device: InputDevice,
    *,
    sysfs_input_root: str | os.PathLike[str] = "/sys/class/input",
) -> str | None:
    """Return the board's Bluetooth address for a connected evdev device.

    hid-wiimote does not propagate ``uniq`` to its input subdevices, and the
    uhid-backed HID parent exposes no sysfs ``uniq`` attribute either — the
    address only appears as HID_UNIQ in the HID device's uevent. Verified on
    hardware: /sys/class/input/eventN/device/device/uevent.
    """
    uniq = str(device.uniq).strip().lower() if device.uniq else ""
    if uniq:
        return uniq
    event_name = os.path.basename(device.path)
    uevent = Path(sysfs_input_root) / event_name / "device" / "device" / "uevent"
    try:
        lines = uevent.read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("HID_UNIQ="):
            value = line.split("=", 1)[1].strip().lower()
            return value or None
    return None


def iter_board_samples(
    *,
    poll_interval_s: float = 2.0,
    status: Callable[[str], None] | None = None,
    on_connect: Callable[[str | None], None] | None = None,
    on_disconnect: Callable[[str | None], None] | None = None,
) -> Iterator[Frame]:
    """Yield raw frames forever and report Bluetooth identity across reconnects."""
    report = status or (lambda _message: None)
    connected = on_connect or (lambda _mac: None)
    disconnected = on_disconnect or (lambda _mac: None)
    waiting_reported = False
    while True:
        try:
            device = find_board()
        except OSError:
            device = None

        if device is None:
            if not waiting_reported:
                report(
                    "Waiting for Wii Balance Board; press its power button if it "
                    "is powered off."
                )
                waiting_reported = True
            time.sleep(poll_interval_s)
            continue

        waiting_reported = False
        report(f"Reading from {device.path} ({device.name})")
        mac = device_mac(device)
        connected(mac)
        try:
            for frame in read_frames(device):
                corners = {
                    name: frame[code] / 100.0 for code, name in CELLS.items()
                }
                yield time.time(), corners
        except OSError:
            disconnected(mac)
            report("Balance Board disconnected; waiting for it to return.")
        finally:
            with suppress(OSError):
                device.close()
