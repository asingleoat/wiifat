"""Live readout of the four load cells and total weight.

Usage: python -m wiifat.monitor
Needs read access to the board's /dev/input/event* node (input group or sudo).
"""

import sys

from evdev import ecodes

from .board import CELLS, find_board, read_frames


def main() -> int:
    dev = find_board()
    if dev is None:
        print("No balance board found. Pair/connect it first (scripts/pair.sh).",
              file=sys.stderr)
        return 1

    print(f"Reading from {dev.path} ({dev.name}) — Ctrl-C to stop")
    order = [ecodes.ABS_HAT1X, ecodes.ABS_HAT0X,
             ecodes.ABS_HAT1Y, ecodes.ABS_HAT0Y]
    try:
        for frame in read_frames(dev):
            total = sum(frame.values()) / 100.0
            cells = "  ".join(
                f"{CELLS[code]:>12}: {frame[code] / 100.0:6.2f}" for code in order
            )
            print(f"\r{cells}  |  total: {total:6.2f} kg ", end="", flush=True)
    except KeyboardInterrupt:
        print()
    except OSError:
        print("\nBoard disconnected.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
