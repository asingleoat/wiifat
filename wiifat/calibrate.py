"""Interactive matrix calibration for the Wii Balance Board."""

from __future__ import annotations

import argparse
import os
import sys
from collections import deque
from statistics import fmean, pstdev
from typing import Callable, Iterable, Iterator, Mapping

from .calibration import (
    CORNERS,
    Calibration,
    CalibrationConvergenceError,
    apply_calibration,
    fit_calibration,
    save_calibration,
)
from .capture import StableCapture, StableWindowCapture


Frame = tuple[float, Mapping[str, float]]
InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]


def calibrate_flow(
    *,
    rounds: int = 1,
    check: bool = False,
    config_path: str | os.PathLike[str] | None = None,
    input_fn: InputFn = input,
    frame_source: Iterable[Frame] | None = None,
    output_fn: OutputFn = print,
) -> Calibration:
    """Run calibration with injectable console input and timestamped frames."""
    if rounds < 1:
        raise ValueError("rounds must be at least 1")

    if frame_source is None:
        from .source import iter_board_samples

        frames: Iterator[Frame] = iter_board_samples(status=output_fn)
        # Force connection before presenting the empty-board prompt. This first
        # potentially partial device frame is deliberately discarded.
        next(frames)
        output_fn("Balance Board connected.")
    else:
        frames = iter(frame_source)

    input_fn("Empty the board completely, then press Enter to capture its baseline: ")
    empty_capture = _capture_stable(frames, minimum_total_kg=None)
    empty_means = empty_capture.corners
    empty_total = sum(empty_means.values())
    output_fn(f"Empty-board capture stable at {empty_total:.3f} kg raw total.")
    clamped_corners = [
        corner for corner in CORNERS if empty_means[corner] == 0.0
    ]
    if clamped_corners:
        output_fn("Empty-clamped cells: " + ", ".join(clamped_corners))
    else:
        output_fn("All four cells have observable empty-board offsets.")

    weight = _prompt_weight(input_fn, output_fn)
    presence = max(0.15, 0.3 * weight)
    tagged_placements: list[
        tuple[tuple[str, ...], float, dict[str, float]]
    ] = []

    input_fn(
        "Place heavy object #1 (for example, a full water jug) roughly centered "
        "on the board; it never needs to be weighed. Press Enter: "
    )
    rebaseline_number = 1
    base_id = "X1"
    jug_capture = _capture_all_engaged(
        frames,
        minimum_total_kg=empty_total + 1.0,
        input_fn=input_fn,
        retry_message=(
            "Heavy object #1 does not engage every cell. Recenter it, then "
            "press Enter to capture again: "
        ),
    )
    jug_total = jug_capture.total_kg
    tagged_placements.append(((base_id,), 0.0, _capture_corners(jug_capture)))
    output_fn(
        f"Recorded {base_id} object-only level at {jug_total:.3f} kg raw total."
    )

    mass_on_board = False
    for round_index in range(rounds):
        for corner in CORNERS:
            if mass_on_board:
                output_fn(
                    "Remove the known mass while leaving the jug unmoved; "
                    "waiting for the jug-only level."
                )
            current_base = _capture_near_level(
                frames,
                jug_total,
                _lift_allowance(weight),
            )
            movement_limit = max(0.25, 0.1 * weight)
            if abs(current_base.total_kg - jug_total) > movement_limit:
                output_fn(
                    "Warning: the jug-only level shifted, so the jug may have "
                    "moved. Recording a fresh jug baseline as a new fitted base."
                )
                if any(current_base.corners[name] == 0.0 for name in CORNERS):
                    raise ValueError(
                        "the shifted jug baseline has a clamped cell; recenter the "
                        "jug and restart calibration"
                    )
                rebaseline_number += 1
                base_id = f"X1R{rebaseline_number}"
                jug_capture = current_base
                jug_total = current_base.total_kg
                tagged_placements.append(
                    ((base_id,), 0.0, _capture_corners(jug_capture))
                )

            input_fn(
                f"Round {round_index + 1}/{rounds}: keep the jug in place and "
                f"set the {weight:g} kg known mass near {corner}, then press Enter: "
            )
            addition = _capture_stable(
                frames, minimum_total_kg=jug_total + presence
            )
            mass_on_board = True
            placement = _capture_corners(addition)
            tagged_placements.append(((base_id,), weight, placement))
            deltas = "  ".join(
                f"{name}: {placement[name] - jug_capture.corners[name]:+.3f} kg"
                for name in CORNERS
            )
            output_fn(f"Cell deltas vs {base_id} jug baseline: {deltas}")
            if any(value == 0.0 for value in placement.values()):
                output_fn("Clamped cell detected: this placement will be excluded.")

    input_fn(
        "Remove the known mass. Now add a SECOND heavy object (another jug — "
        "its weight also never needs to be known) onto the board next to the "
        "first; keep both fully on the board. Press Enter: "
    )
    combined_capture = _capture_all_engaged(
        frames,
        minimum_total_kg=jug_total + 1.0,
        input_fn=input_fn,
        retry_message=(
            "The two-object capture does not engage every cell. Reposition both "
            "objects fully on the board, then press Enter to capture again: "
        ),
    )
    combined_total = combined_capture.total_kg
    tagged_placements.append(
        ((base_id, "X2"), 0.0, _capture_corners(combined_capture))
    )
    output_fn(
        f"Recorded {base_id}+X2 at {combined_total:.3f} kg raw total."
    )

    input_fn(
        "Remove the FIRST object, leaving only the second; keep the second "
        "roughly centered, then press Enter: "
    )
    second_capture = _capture_all_engaged(
        frames,
        minimum_total_kg=empty_total + 1.0,
        maximum_total_kg=combined_total - 1.0,
        input_fn=input_fn,
        retry_message=(
            "The second object alone still leaves a cell at zero. Recenter it and "
            "press Enter to recapture. If repositioning cannot engage every cell, "
            "it is too light to serve as the closure object; restart with a "
            "heavier second object: "
        ),
    )
    tagged_placements.append((("X2",), 0.0, _capture_corners(second_capture)))
    output_fn(
        f"Recorded X2 alone at {second_capture.total_kg:.3f} kg raw total."
    )

    input_fn(
        "Remove both heavy objects and the known mass. Leave the board completely "
        "empty, then press Enter for the final baseline capture: "
    )
    final_empty_capture = _capture_stable(
        frames,
        minimum_total_kg=None,
        maximum_total_kg=second_capture.total_kg - 1.0,
    )
    drifted = False
    for corner in CORNERS:
        drift = final_empty_capture.corners[corner] - empty_means[corner]
        output_fn(f"Empty drift {corner}: {drift:+.3f} kg")
        drifted = drifted or abs(drift) > 0.10
    if drifted:
        output_fn(
            "Warning: offsets drifted during calibration; results may be biased — "
            "consider re-running with the board warmed up."
        )

    empty_points = [
        *(sample.corners for sample in empty_capture.samples),
        *(sample.corners for sample in final_empty_capture.samples),
    ]
    calibration = fit_calibration(empty_points, tagged_placements)
    _print_fit(calibration, clamped_corners, output_fn)

    if _confirm_save(input_fn):
        path = save_calibration(calibration, config_path)
        output_fn(f"Saved calibration to {path}")
    else:
        output_fn("Calibration was not saved.")

    if check:
        _run_check(frames, calibration, empty_total, input_fn, output_fn)
    return calibration


def _capture_corners(capture: StableCapture) -> dict[str, float]:
    return {corner: capture.corners[corner] for corner in CORNERS}


def _capture_all_engaged(
    frames: Iterator[Frame],
    *,
    minimum_total_kg: float,
    input_fn: InputFn,
    retry_message: str,
    maximum_total_kg: float | None = None,
) -> StableCapture:
    while True:
        capture = _capture_stable(
            frames,
            minimum_total_kg=minimum_total_kg,
            maximum_total_kg=maximum_total_kg,
        )
        clamped = [corner for corner in CORNERS if capture.corners[corner] == 0.0]
        if not clamped:
            return capture
        input_fn(retry_message + " Still zero: " + ", ".join(clamped) + ". ")


def _capture_near_level(
    frames: Iterator[Frame],
    target_total_kg: float,
    allowance_kg: float,
) -> StableCapture:
    capture = StableWindowCapture(window_s=2.5, max_stdev_kg=0.2)
    for timestamp, readings in frames:
        corners = {corner: float(readings[corner]) for corner in CORNERS}
        total = sum(corners.values())
        if abs(total - target_total_kg) > allowance_kg:
            capture.reset()
            continue
        result = capture.update(timestamp, total, corners)
        if result is not None:
            return result
    raise RuntimeError("frame source ended while waiting for the jug-only level")


def _capture_stable(
    frames: Iterator[Frame],
    *,
    minimum_total_kg: float | None,
    maximum_total_kg: float | None = None,
) -> StableCapture:
    capture = StableWindowCapture(
        minimum_total_kg=minimum_total_kg,
        window_s=2.5,
        max_stdev_kg=0.2,
    )
    for timestamp, readings in frames:
        corners = {corner: float(readings[corner]) for corner in CORNERS}
        total = sum(corners.values())
        if maximum_total_kg is not None and total > maximum_total_kg:
            capture.reset()
            continue
        result = capture.update(timestamp, total, corners)
        if result is not None:
            return result
    raise RuntimeError("frame source ended before a stable window was captured")


def _lift_allowance(weight_kg: float) -> float:
    """Near-empty margin that stays distinguishable from the reference weight.

    A fixed allowance larger than a light reference would treat "weight still
    on the board" as "lifted" and capture the same placement twice.
    """
    return max(0.15, min(1.0, 0.5 * weight_kg))


def _wait_near_empty(
    frames: Iterator[Frame], empty_total: float, allowance_kg: float
) -> None:
    for _timestamp, readings in frames:
        if sum(float(readings[corner]) for corner in CORNERS) <= empty_total + allowance_kg:
            return
    raise RuntimeError("frame source ended while waiting for the board to become empty")


def _prompt_weight(input_fn: InputFn, output_fn: OutputFn) -> float:
    while True:
        answer = input_fn("Precisely known calibration mass W, in kg: ")
        try:
            weight = float(answer)
        except ValueError:
            output_fn("Enter a numeric mass in kilograms.")
            continue
        if weight <= 0.0:
            output_fn("Enter a positive mass in kilograms.")
            continue
        if weight < 1.0:
            output_fn(
                "Warning: masses under 1 kg work (a kitchen-scale-verified weight "
                "is a fine standard), but gain error at body weight scales "
                "inversely with the reference mass — heavier is better."
            )
        elif weight < 3.0:
            output_fn("Warning: references below 3 kg may have poor signal-to-noise.")
        return weight


def _confirm_save(input_fn: InputFn) -> bool:
    while True:
        answer = input_fn("Save this calibration? [y/n]: ").strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False


def _print_fit(
    calibration: Calibration,
    clamped_corners: Iterable[str],
    output_fn: OutputFn,
) -> None:
    output_fn(
        f"Fit converged in {calibration.iterations} iterations; final parameter "
        f"delta {calibration.final_delta:.3g}."
    )
    output_fn("Corner          gain       offset kg   residual RMS kg")
    clamped = set(clamped_corners)
    for corner in CORNERS:
        marker = "*" if corner in clamped else " "
        output_fn(
            f"{marker}{corner:<14} {calibration.gains[corner]:>9.6f}  "
            f"{calibration.offsets[corner]:>10.6f}  "
            f"{calibration.residual_rms[corner]:>15.6f}"
        )
    for base_id, weight in calibration.fitted_bases_kg.items():
        output_fn(
            f"Fitted {base_id}: {weight:.6f} kg — inferred weight of the "
            "unweighed reference object."
        )
    for corner in calibration.exactly_determined_offsets:
        output_fn(
            f"{corner} offset is exactly determined (no internal redundancy) — "
            "verify against a trusted scale."
        )
    if calibration.known_total_rms_kg is not None:
        output_fn(
            "Fully-known total consistency RMS: "
            f"{calibration.known_total_rms_kg:.6f} kg."
        )
    for warning in calibration.warnings:
        output_fn(f"Warning: {warning}")


def _run_check(
    frames: Iterator[Frame],
    calibration: Calibration,
    empty_total: float,
    input_fn: InputFn,
    output_fn: OutputFn,
) -> None:
    input_fn(
        "Stand on the board, press Enter, then shift your weight around for "
        "about 10 seconds: "
    )
    raw_totals, corrected_totals = _collect_check_totals(
        frames, calibration, empty_total
    )

    output_fn(
        f"Raw total: range {min(raw_totals):.3f}–{max(raw_totals):.3f} kg, "
        f"mean {fmean(raw_totals):.3f} kg, stdev {pstdev(raw_totals):.3f} kg"
    )
    output_fn(
        f"Corrected total: range {min(corrected_totals):.3f}–"
        f"{max(corrected_totals):.3f} kg, mean {fmean(corrected_totals):.3f} kg, "
        f"stdev {pstdev(corrected_totals):.3f} kg"
    )


def _collect_check_totals(
    frames: Iterator[Frame],
    calibration: Calibration,
    empty_total: float,
    *,
    presence_kg: float = 20.0,
    settle_s: float = 1.0,
    settle_tolerance_kg: float = 1.5,
    duration_s: float = 10.0,
) -> tuple[list[float], list[float]]:
    """Discard step-on, then collect a timestamped occupied check window."""
    settling: deque[tuple[float, float]] = deque()
    started: float | None = None
    raw_totals: list[float] = []
    corrected_totals: list[float] = []
    for timestamp, readings in frames:
        raw = {corner: float(readings[corner]) for corner in CORNERS}
        raw_total = sum(raw.values())
        if started is None:
            if raw_total < empty_total + presence_kg:
                settling.clear()
                continue
            settling.append((timestamp, raw_total))
            cutoff = timestamp - settle_s
            while len(settling) >= 2 and settling[1][0] <= cutoff:
                settling.popleft()
            if timestamp - settling[0][0] < settle_s - 1e-9:
                continue
            settled_values = [total for _time, total in settling]
            if max(settled_values) - min(settled_values) > 2 * settle_tolerance_kg:
                continue
            started = timestamp

        raw_totals.append(raw_total)
        corrected_totals.append(sum(apply_calibration(raw, calibration).values()))
        if timestamp - started >= duration_s - 1e-9:
            return raw_totals, corrected_totals
    raise RuntimeError("frame source ended during the 10-second check")


def rounds_arg(value: str) -> int:
    rounds = int(value)
    if rounds < 1:
        raise argparse.ArgumentTypeError("--rounds must be at least 1")
    return rounds


def run(
    *,
    rounds: int = 1,
    check: bool = False,
    config_path: str | os.PathLike[str] | None = None,
) -> int:
    try:
        calibrate_flow(
            rounds=rounds,
            check=check,
            config_path=config_path,
        )
    except KeyboardInterrupt:
        print("Calibration stopped.", file=sys.stderr)
        return 130
    except (ValueError, RuntimeError, CalibrationConvergenceError) as exc:
        print(f"Calibration failed: {exc}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rounds",
        type=rounds_arg,
        default=1,
        help="four-corner addition rounds (default: 1)",
    )
    parser.add_argument("--check", action="store_true", help="run a 10-second check")
    parser.add_argument("--config", help="calibration JSON output path")
    args = parser.parse_args(argv)
    return run(
        rounds=args.rounds,
        check=args.check,
        config_path=args.config,
    )


if __name__ == "__main__":
    raise SystemExit(main())
