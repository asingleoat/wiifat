"""Automatic scale daemon: board samples to SQLite measurements."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Callable, Iterable

from .calibration import apply_calibration, load_calibration, Calibration
from .db import Database, format_timestamp
from .statemachine import Measurement, ScaleStateMachine


DisconnectFn = Callable[[str], bool]
BatteryReader = Callable[[str | None], int | None]
MeasurementCallback = Callable[[int, "Measurement"], None]
StatusCallback = Callable[[str], None]
ProgressCallback = Callable[[float, dict[str, str | float | None], float], None]


class IdlePowerManager:
    """Request one disconnect after a logged occupancy returns to idle."""

    def __init__(
        self, timeout_s: float, no_activity_timeout_s: float = 300.0
    ) -> None:
        if timeout_s < 0.0 or no_activity_timeout_s < 0.0:
            raise ValueError("power timeouts must be nonnegative")
        self.timeout_s = timeout_s
        self.no_activity_timeout_s = no_activity_timeout_s
        self._measurement_logged = False
        self._idle_since: float | None = None
        self._connected_since: float | None = None

    def note_measurement(self) -> None:
        """Arm power-off for the end of the current occupancy."""
        self._measurement_logged = True
        self._idle_since = None

    def note_disconnect(self) -> None:
        """Discard an old session's pending timeout across reconnects."""
        self._measurement_logged = False
        self._idle_since = None
        self._connected_since = None

    def update(
        self,
        timestamp: float,
        state: str,
        mac: str | None,
        disconnect: DisconnectFn,
    ) -> bool:
        """Apply the timeout decision and return whether disconnect succeeded."""
        if mac is None:
            return False
        if self._connected_since is None:
            self._connected_since = timestamp
        if state != ScaleStateMachine.IDLE:
            self._idle_since = None
            return False
        if not self._measurement_logged:
            if (
                self.no_activity_timeout_s == 0.0
                or timestamp - self._connected_since < self.no_activity_timeout_s
            ):
                return False
            if disconnect(mac):
                self.note_disconnect()
                return True
            self._connected_since = timestamp
            return False
        if self.timeout_s == 0.0:
            return False
        if self._idle_since is None:
            self._idle_since = timestamp
            return False
        if timestamp - self._idle_since < self.timeout_s or mac is None:
            return False
        if disconnect(mac):
            self.note_disconnect()
            return True
        self._idle_since = timestamp
        return False


def disconnect_board(
    mac: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    error: Callable[[str], None] | None = None,
) -> bool:
    """Ask BlueZ to disconnect a board without making daemon failure fatal."""
    report = error or (lambda message: print(message, file=sys.stderr))
    try:
        result = runner(
            ["bluetoothctl", "disconnect", mac],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        report(f"Could not disconnect Balance Board {mac}: {exc}")
        return False
    if result.returncode != 0:
        detail = result.stderr.strip() or f"exit status {result.returncode}"
        report(f"Could not disconnect Balance Board {mac}: {detail}")
        return False
    return True


def read_battery_pct(
    mac: str | None,
    *,
    power_supply_root: str | os.PathLike[str] = "/sys/class/power_supply",
) -> int | None:
    """Read hid-wiimote battery capacity for a lower-case Bluetooth address."""
    if not mac:
        return None
    path = (
        Path(power_supply_root)
        / f"wiimote_battery_{mac.lower()}"
        / "capacity"
    )
    try:
        value = int(path.read_text().strip())
    except (OSError, ValueError):
        return None
    return value if 0 <= value <= 100 else None


def run(
    db_path: str | os.PathLike[str] | None = None,
    config_path: str | os.PathLike[str] | None = None,
    *,
    once: bool = False,
    poll_interval_s: float = 2.0,
    idle_timeout_s: float = 15.0,
    frame_source: Iterable[tuple[float, dict[str, float]]] | None = None,
    device_mac: str | None = None,
    disconnect_fn: DisconnectFn | None = None,
    battery_reader: BatteryReader | None = None,
    no_activity_timeout_s: float = 300.0,
    on_measurement: MeasurementCallback | None = None,
    on_status: StatusCallback | None = None,
    on_progress: ProgressCallback | None = None,
) -> int:
    """Poll for a board, log measurements, and survive device removal."""
    database = Database(db_path)
    machine = ScaleStateMachine()
    calibration = load_calibration(config_path) or Calibration.identity()
    calibration_snapshot = calibration.snapshot_json()
    power = IdlePowerManager(idle_timeout_s, no_activity_timeout_s)
    disconnect = disconnect_fn or disconnect_board
    get_battery = battery_reader or read_battery_pct
    mac_holder = {"value": device_mac.lower() if device_mac else None}

    def connected(mac: str | None) -> None:
        mac_holder["value"] = mac

    def disconnected(mac: str | None) -> None:
        if mac_holder["value"] == mac:
            mac_holder["value"] = None
        power.note_disconnect()

    def report_status(message: str) -> None:
        print(message, file=sys.stderr)
        if on_status is not None:
            on_status(message)

    if calibration.ts is None:
        report_status("No calibration file found; using identity correction.")
    else:
        report_status(f"Using calibration from {calibration.ts}")

    try:
        if frame_source is not None:
            samples = iter(frame_source)
        else:
            from .source import iter_board_samples

            samples = iter_board_samples(
                poll_interval_s=poll_interval_s,
                status=report_status,
                on_connect=connected,
                on_disconnect=disconnected,
            )
        for timestamp, raw_corners in samples:
            corrected = apply_calibration(raw_corners, calibration)
            total_kg = sum(corrected.values())
            measurement = machine.update(
                timestamp,
                total_kg,
                corrected,
                raw_total_kg=sum(raw_corners.values()),
            )
            if on_progress is not None:
                try:
                    on_progress(timestamp, machine.snapshot(), total_kg)
                except Exception as exc:
                    print(f"Progress callback failed: {exc}", file=sys.stderr)
            if measurement is not None:
                battery_pct = get_battery(mac_holder["value"])
                measurement = replace(
                    measurement,
                    cal_json=calibration_snapshot,
                    battery_pct=battery_pct,
                )
                measurement_id = database.insert(measurement)
                measurement = replace(measurement, id=measurement_id)
                if on_measurement is not None:
                    try:
                        on_measurement(measurement_id, measurement)
                    except Exception as exc:
                        print(
                            f"Measurement callback failed: {exc}", file=sys.stderr
                        )
                battery_text = (
                    f"  battery {battery_pct}%" if battery_pct is not None else ""
                )
                print(
                    f"{format_timestamp(measurement.timestamp)}  "
                    f"{measurement.weight_kg:.2f} kg{battery_text}",
                    flush=True,
                )
                if battery_pct is not None and battery_pct < 15:
                    print(
                        f"Warning: Balance Board battery is low ({battery_pct}%).",
                        file=sys.stderr,
                    )
                power.note_measurement()
                if once:
                    return 0

            if power.update(
                timestamp,
                machine.state,
                mac_holder["value"],
                disconnect,
            ):
                mac_holder["value"] = None
                report_status(
                    "Balance Board powered off. Press its power button to wake it; "
                    "waiting for the device to return."
                )
    except KeyboardInterrupt:
        mac = mac_holder["value"]
        if mac is not None and disconnect(mac):
            print(
                "Balance Board powered off. Press its power button to wake it.",
                file=sys.stderr,
            )
        print("Stopped.", file=sys.stderr)
        return 0
    return 0


def idle_timeout_arg(value: str) -> float:
    timeout = float(value)
    if timeout < 0.0:
        raise argparse.ArgumentTypeError("--idle-timeout must be nonnegative")
    return timeout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="measurement database path")
    parser.add_argument("--config", help="calibration JSON path")
    parser.add_argument("--once", action="store_true", help="exit after one measurement")
    parser.add_argument(
        "--idle-timeout",
        type=idle_timeout_arg,
        default=15.0,
        help="seconds idle after a weigh-in before power-off; 0 disables (default: 15)",
    )
    parser.add_argument(
        "--no-activity-timeout",
        type=idle_timeout_arg,
        default=300.0,
        help=(
            "seconds connected without a weigh-in before power-off; "
            "0 disables (default: 300)"
        ),
    )
    args = parser.parse_args(argv)
    return run(
        args.db,
        args.config,
        once=args.once,
        idle_timeout_s=args.idle_timeout,
        no_activity_timeout_s=args.no_activity_timeout,
    )


if __name__ == "__main__":
    raise SystemExit(main())
