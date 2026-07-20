"""Access to the Wii Balance Board via the hid-wiimote evdev interface.

The kernel driver applies the board's factory calibration and reports each
of the four load cells as an absolute axis in units of 1/100 kg:

    ABS_HAT0X  top-right      ABS_HAT1X  top-left
    ABS_HAT0Y  bottom-right   ABS_HAT1Y  bottom-left

(orientation: power button facing away from you)
The front power/sync button is reported as BTN_A.
"""

from evdev import InputDevice, ecodes, list_devices

DEVICE_NAME = "Nintendo Wii Remote Balance Board"

CELLS = {
    ecodes.ABS_HAT0X: "top-right",
    ecodes.ABS_HAT0Y: "bottom-right",
    ecodes.ABS_HAT1X: "top-left",
    ecodes.ABS_HAT1Y: "bottom-left",
}


def find_board() -> InputDevice | None:
    """Return the balance board's evdev device, or None if not connected."""
    for path in list_devices():
        dev = InputDevice(path)
        if dev.name == DEVICE_NAME:
            return dev
        dev.close()
    return None


def read_frames(dev: InputDevice):
    """Yield {axis_code: centi_kg} snapshots, one per SYN_REPORT frame."""
    state = {code: 0 for code in CELLS}
    for event in dev.read_loop():
        if event.type == ecodes.EV_ABS and event.code in state:
            state[event.code] = event.value
        elif event.type == ecodes.EV_SYN and event.code == ecodes.SYN_REPORT:
            yield dict(state)
