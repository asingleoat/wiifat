import pytest

import wiifat.calibrate as calibrate_module
from wiifat.calibrate import _collect_check_totals, calibrate_flow, main
from wiifat.calibration import CORNERS, Calibration


GAINS = dict(zip(CORNERS, (0.97, 1.04, 1.00, 0.95)))
OFFSETS = dict(zip(CORNERS, (2.1, 0.6, 0.3, -0.55)))
KNOWN_WEIGHT = 0.5
JUG_WEIGHT = 6.0
SECOND_JUG_WEIGHT = 4.5
JUG_LOADS = (1.5, 1.5, 1.5, 1.5)
ADDITIONS = (
    (2.0, 1.5, 1.5, 1.5),
    (1.5, 2.0, 1.5, 1.5),
    (1.5, 1.5, 2.0, 1.5),
    (1.5, 1.5, 1.5, 2.0),
)


def raw_readings(loads, offsets=OFFSETS):
    return {
        corner: max(GAINS[corner] * load + offsets[corner], 0.0)
        for corner, load in zip(CORNERS, loads)
    }


def test_injected_full_closure_protocol_retries_drifts_and_checks(
    tmp_path, monkeypatch
):
    frames = []
    timestamp = 50_000.0

    def stable_block(loads, seconds=2.5):
        nonlocal timestamp
        readings = raw_readings(loads)
        for _ in range(int(seconds * 70) + 1):
            frames.append((timestamp, readings))
            timestamp += 1 / 70

    stable_block((0.0, 0.0, 0.0, 0.0))

    stable_block(JUG_LOADS)
    for addition in ADDITIONS:
        stable_block(JUG_LOADS)
        stable_block(addition)

    # Closure: both heavy objects, then object #2 alone. Its first position
    # leaves bottom-left clamped; recentering engages all four cells.
    stable_block((2.625, 2.625, 2.625, 2.625))
    stable_block((1.4, 1.4, 1.4, 0.3))
    stable_block((1.125, 1.125, 1.125, 1.125))

    drifted_offsets = dict(OFFSETS)
    drifted_offsets["top-right"] += 0.20
    closing_empty = raw_readings((0.0, 0.0, 0.0, 0.0), drifted_offsets)
    for _ in range(int(2.5 * 70) + 1):
        frames.append((timestamp, closing_empty))
        timestamp += 1 / 70

    # One second is discarded for settling, followed by the ten-second check.
    for index in range(771):
        shift = (index % 21 - 10) / 10
        loads = (
            18.0 + shift,
            20.0 - shift,
            17.0 + shift / 2,
            20.0 - shift / 2,
        )
        frames.append((timestamp, raw_readings(loads)))
        timestamp += 1 / 70

    answers = iter(
        [
            "",  # Empty-board prompt.
            "0.5",
            "",  # Jug capture.
            "",
            "",
            "",
            "",  # Four corner additions.
            "",  # Both-object closure capture.
            "",  # Object #2 alone, first attempt.
            "",  # Recenter object #2 after clamped capture.
            "",  # Final empty-board drift capture.
            "n",  # Do not save; persistence has a separate round-trip test.
            "",  # Start occupied check.
        ]
    )
    output = []
    prompts = []
    captured_empty_points = []

    real_fit = calibrate_module.fit_calibration

    def recording_fit(empty_points, placements):
        captured_empty_points.extend(empty_points)
        return real_fit(captured_empty_points, placements)

    monkeypatch.setattr(calibrate_module, "fit_calibration", recording_fit)

    def input_fn(prompt):
        prompts.append(prompt)
        return next(answers)

    calibration = calibrate_flow(
        rounds=1,
        check=True,
        config_path=tmp_path / "unused.json",
        input_fn=input_fn,
        frame_source=frames,
        output_fn=output.append,
    )

    assert calibration.ref_weights_kg == ()
    assert calibration.fitted_bases_kg["X1"] == pytest.approx(JUG_WEIGHT, abs=0.05)
    assert calibration.fitted_bases_kg["X2"] == pytest.approx(
        SECOND_JUG_WEIGHT, abs=0.05
    )
    assert calibration.iterations < 20_000
    assert any("too light to serve as the closure object" in line for line in prompts)
    assert any(line.startswith("Fitted X1:") for line in output)
    assert any(line.startswith("Fitted X2:") for line in output)
    assert any(
        line.startswith("bottom-left offset is exactly determined") for line in output
    )
    assert not any(line.startswith("Closure consistency:") for line in output)
    assert any("offsets drifted during calibration" in line for line in output)
    assert any(point["top-right"] == pytest.approx(2.1) for point in captured_empty_points)
    assert any(point["top-right"] == pytest.approx(2.3) for point in captured_empty_points)
    assert any(line.startswith("Raw total: range") for line in output)
    assert any(line.startswith("Corrected total: range") for line in output)
    assert all("stdev" in line for line in output if line.startswith(("Raw", "Corrected")))
    assert not (tmp_path / "unused.json").exists()


def test_check_discards_step_on_ramp_before_ten_second_window():
    calibration = Calibration.identity()
    frames = []
    timestamp = 1_000.0

    def add(total):
        nonlocal timestamp
        corners = {corner: total / 4.0 for corner in CORNERS}
        frames.append((timestamp, corners))
        timestamp += 1 / 70

    for index in range(71):
        add(25.0 + 50.0 * index / 70.0)
    for _ in range(71):
        add(75.0)
    for index in range(701):
        add(74.5 if index % 2 else 75.5)

    raw, corrected = _collect_check_totals(iter(frames), calibration, 0.0)

    assert min(raw) >= 74.5
    assert max(raw) <= 75.5
    assert corrected == raw


def test_calibrate_help_only_lists_current_protocol_flags(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "--rounds" in help_text
    assert "--check" in help_text
    assert "--config" in help_text
    assert "--known-masses" not in help_text
    assert "--single-mass" not in help_text
    assert "--placements" not in help_text
