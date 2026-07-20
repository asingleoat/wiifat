#!/usr/bin/env bash
# Pair (or re-connect) a Wii Balance Board.
#
# Usage: scripts/pair.sh [timeout-seconds]   (default 900)
#
# Press the red SYNC button inside the battery compartment while this runs.
# The four LEDs blink for ~20s per press; the board is only discoverable
# during that window, so press it again if the scan hasn't caught it yet.
# BlueZ's built-in wiimote plugin supplies the Nintendo PIN automatically.
set -uo pipefail

TIMEOUT="${1:-900}"
NAME="Nintendo RVL-WBC-01"

find_mac() {
    bluetoothctl devices | awk -v n="$NAME" '$0 ~ n {print $2; exit}'
}

MAC="$(find_mac)"

if [ -z "$MAC" ]; then
    echo "Scanning up to ${TIMEOUT}s for '$NAME' — press the red SYNC button now."
    bluetoothctl --timeout "$TIMEOUT" scan on >/dev/null 2>&1 &
    SCAN_PID=$!
    trap 'kill "$SCAN_PID" 2>/dev/null' EXIT

    for _ in $(seq "$TIMEOUT"); do
        MAC="$(find_mac)"
        [ -n "$MAC" ] && break
        kill -0 "$SCAN_PID" 2>/dev/null || break
        sleep 1
    done
    kill "$SCAN_PID" 2>/dev/null
fi

if [ -z "$MAC" ]; then
    echo "Timed out without seeing the board. Press SYNC and re-run." >&2
    exit 1
fi

echo "Found board at $MAC"

# Pairing can report failure on an already-paired board; connect is what counts.
bluetoothctl pair "$MAC"
bluetoothctl trust "$MAC"

if ! bluetoothctl connect "$MAC"; then
    echo "Connect failed. If the log mentions the connection being rejected," >&2
    echo "set ClassicBondedOnly=false in the [General] section of" >&2
    echo "/etc/bluetooth/input.conf (see README) and retry." >&2
    exit 1
fi

echo
echo "Connected. Waiting for the hid-wiimote driver to bind..."
for _ in $(seq 10); do
    dev="$(grep -l 'Balance Board' /sys/class/input/event*/device/name 2>/dev/null | head -1)"
    [ -n "$dev" ] && break
    sleep 1
done

if [ -n "${dev:-}" ]; then
    evdev="$(dirname "$(dirname "$dev")")"
    echo "Balance board is live at /dev/input/$(basename "$evdev")"
else
    echo "Connected, but no Balance Board input device appeared yet." >&2
    echo "Check: cat /sys/class/input/event*/device/name" >&2
    exit 1
fi
