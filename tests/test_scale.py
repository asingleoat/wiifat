import subprocess

import pytest

from wiifat.calibration import CORNERS
from wiifat.db import Database
from wiifat.scale import IdlePowerManager, disconnect_board, read_battery_pct, run
from wiifat.statemachine import ScaleStateMachine


def test_daemon_disconnects_after_logged_occupancy_returns_idle(
    tmp_path, capsys
):
    frames = []
    timestamp = 10_000.0

    def block(duration_s, total_kg):
        nonlocal timestamp
        corners = {corner: total_kg / 4.0 for corner in CORNERS}
        for _ in range(int(duration_s * 70)):
            frames.append((timestamp, corners))
            timestamp += 1 / 70

    block(5.1, 0.0)
    block(4.0, 75.0)
    block(2.6, 0.0)

    disconnects = []
    callbacks = []

    def disconnect(mac):
        disconnects.append(mac)
        return True

    path = tmp_path / "measurements.sqlite3"
    result = run(
        path,
        tmp_path / "missing-calibration.json",
        idle_timeout_s=0.2,
        frame_source=frames,
        device_mac="AA:BB:CC:DD:EE:FF",
        disconnect_fn=disconnect,
        battery_reader=lambda mac: 12 if mac else None,
        on_measurement=lambda measurement_id, item: callbacks.append(
            (measurement_id, item)
        ),
    )

    assert result == 0
    assert disconnects == ["aa:bb:cc:dd:ee:ff"]
    assert len(callbacks) == 1
    assert callbacks[0][0] == callbacks[0][1].id
    measurement = Database(path).fetch_recent(1)[0]
    assert measurement.weight_kg == pytest.approx(75.0)
    assert measurement.battery_pct == 12
    captured = capsys.readouterr()
    assert "battery 12%" in captured.out
    assert "battery is low (12%)" in captured.err
    assert "powered off" in captured.err
    assert "power button to wake it" in captured.err


def test_disconnect_failure_reports_bluetoothctl_stderr():
    calls = []
    errors = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="not connected")

    assert not disconnect_board(
        "aa:bb:cc:dd:ee:ff", runner=runner, error=errors.append
    )
    assert calls == [
        (
            ["bluetoothctl", "disconnect", "aa:bb:cc:dd:ee:ff"],
            {"check": False, "capture_output": True, "text": True},
        )
    ]
    assert errors == [
        "Could not disconnect Balance Board aa:bb:cc:dd:ee:ff: not connected"
    ]


def test_ctrl_c_disconnects_connected_board(tmp_path, capsys):
    disconnects = []

    def interrupted_frames():
        yield 1.0, {corner: 0.0 for corner in CORNERS}
        raise KeyboardInterrupt

    result = run(
        tmp_path / "interrupt.sqlite3",
        tmp_path / "missing-calibration.json",
        frame_source=interrupted_frames(),
        device_mac="aa:bb:cc:dd:ee:ff",
        disconnect_fn=lambda mac: disconnects.append(mac) or True,
    )

    assert result == 0
    assert disconnects == ["aa:bb:cc:dd:ee:ff"]
    assert "powered off" in capsys.readouterr().err


def test_battery_reader_uses_lowercase_colon_mac_path(tmp_path):
    capacity = (
        tmp_path
        / "wiimote_battery_aa:bb:cc:dd:ee:ff"
        / "capacity"
    )
    capacity.parent.mkdir()
    capacity.write_text("87\n")

    assert read_battery_pct(
        "AA:BB:CC:DD:EE:FF", power_supply_root=tmp_path
    ) == 87
    assert read_battery_pct(None, power_supply_root=tmp_path) is None


def test_no_activity_timeout_disconnects_idle_board_without_a_weigh_in():
    manager = IdlePowerManager(timeout_s=60.0, no_activity_timeout_s=5.0)
    disconnects = []
    disconnect = lambda mac: disconnects.append(mac) or True

    assert not manager.update(100.0, ScaleStateMachine.IDLE, "aa:bb", disconnect)
    assert not manager.update(104.9, ScaleStateMachine.IDLE, "aa:bb", disconnect)
    assert manager.update(105.0, ScaleStateMachine.IDLE, "aa:bb", disconnect)
    assert disconnects == ["aa:bb"]


def test_daemon_reports_progress_for_every_frame_and_state_transition(tmp_path):
    frames = []
    timestamp = 20_000.0

    def block(duration_s, total_kg):
        nonlocal timestamp
        corners = {corner: total_kg / 4.0 for corner in CORNERS}
        for _ in range(int(duration_s * 70)):
            frames.append((timestamp, corners))
            timestamp += 1 / 70

    block(1.0, 2.0)
    block(4.0, 72.0)
    progress = []

    assert run(
        tmp_path / "progress.sqlite3",
        tmp_path / "missing-calibration.json",
        once=True,
        frame_source=frames,
        on_progress=lambda timestamp, snapshot, total: progress.append(
            (timestamp, snapshot, total)
        ),
    ) == 0

    states = [snapshot["state"] for _timestamp, snapshot, _total in progress]
    assert states[0] == ScaleStateMachine.IDLE
    assert ScaleStateMachine.MEASURING in states
    assert states[-1] == ScaleStateMachine.MEASURED
    last_frame_index = next(
        index
        for index, (timestamp, _corners) in enumerate(frames)
        if timestamp == progress[-1][0]
    )
    assert len(progress) == last_frame_index + 1
    assert len(progress) < len(frames)  # --once stops on the measurement frame.
    assert all(total in (2.0, 72.0) for _timestamp, _snapshot, total in progress)
