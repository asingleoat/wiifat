from types import SimpleNamespace

from wiifat.source import device_mac


def fake_device(uniq, path="/dev/input/event27"):
    return SimpleNamespace(uniq=uniq, path=path)


def test_device_mac_prefers_evdev_uniq(tmp_path):
    device = fake_device("00:25:A0:38:F2:C8")
    assert device_mac(device, sysfs_input_root=tmp_path) == "00:25:a0:38:f2:c8"


def test_device_mac_falls_back_to_hid_uevent(tmp_path):
    # hid-wiimote leaves the input node's uniq empty; the address only
    # appears as HID_UNIQ in the parent HID device's uevent.
    uevent_dir = tmp_path / "event27" / "device" / "device"
    uevent_dir.mkdir(parents=True)
    (uevent_dir / "uevent").write_text(
        "DRIVER=wiimote\n"
        "HID_ID=0005:0000057E:00000306\n"
        "HID_NAME=Nintendo RVL-WBC-01\n"
        "HID_PHYS=34:6f:24:db:21:82\n"
        "HID_UNIQ=00:25:A0:38:F2:C8\n"
        "MODALIAS=hid:b0005g0000v0000057Ep00000306\n"
    )
    device = fake_device("")
    assert device_mac(device, sysfs_input_root=tmp_path) == "00:25:a0:38:f2:c8"


def test_device_mac_missing_everywhere(tmp_path):
    assert device_mac(fake_device(""), sysfs_input_root=tmp_path) is None

    uevent_dir = tmp_path / "event27" / "device" / "device"
    uevent_dir.mkdir(parents=True)
    (uevent_dir / "uevent").write_text("DRIVER=wiimote\n")
    assert device_mac(fake_device(""), sysfs_input_root=tmp_path) is None
