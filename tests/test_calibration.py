import pytest

from wiifat.calibration import (
    CORNERS,
    Calibration,
    apply_calibration,
    fit_calibration,
    load_calibration,
    offset_is_identifiable,
    save_calibration,
)
from wiifat.statemachine import ScaleStateMachine


GAINS = dict(zip(CORNERS, (0.97, 1.04, 1.00, 0.95)))
OFFSETS = dict(zip(CORNERS, (2.1, 0.6, 0.3, -0.55)))
LIGHT_WEIGHT = 10.0
LIGHT_DISTRIBUTIONS = (
    (4.0, 2.0, 2.0, 2.0),
    (2.0, 4.0, 2.0, 2.0),
    (2.0, 2.0, 4.0, 2.0),
    (2.0, 2.0, 2.0, 4.0),
    (2.5, 2.5, 2.5, 2.5),
)
HEAVY_WEIGHT = 25.0
HEAVY_DISTRIBUTIONS = (
    (10.0, 5.0, 5.0, 5.0),
    (5.0, 10.0, 5.0, 5.0),
    (5.0, 5.0, 10.0, 5.0),
    (5.0, 5.0, 5.0, 10.0),
    (6.25, 6.25, 6.25, 6.25),
)


def readings(loads, offsets=OFFSETS):
    return {
        corner: max(GAINS[corner] * load + offsets[corner], 0.0)
        for corner, load in zip(CORNERS, loads)
    }


def fit(placements=None):
    empty = [readings((0.0, 0.0, 0.0, 0.0)) for _ in range(5)]
    return fit_calibration(
        empty,
        placements
        or [
            *(
                (LIGHT_WEIGHT, readings(loads))
                for loads in LIGHT_DISTRIBUTIONS
            ),
            *(
                (HEAVY_WEIGHT, readings(loads))
                for loads in HEAVY_DISTRIBUTIONS
            ),
        ],
        timestamp="2026-07-14T12:00:00.000000Z",
    )


def test_als_recovers_gains_offsets_and_corrects_occupant():
    calibration = fit()

    assert calibration.iterations < 20_000
    assert calibration.final_delta < 1e-7
    for corner in CORNERS:
        assert calibration.gains[corner] == pytest.approx(GAINS[corner], rel=0.02)
        assert calibration.offsets[corner] == pytest.approx(OFFSETS[corner], abs=0.05)

    occupant = readings((18.0, 20.0, 17.0, 20.0))
    corrected = apply_calibration(occupant, calibration)
    assert sum(corrected.values()) == pytest.approx(75.0, abs=0.15)


def test_clamped_placement_is_excluded_and_warned_about():
    placements = [
        *((LIGHT_WEIGHT, readings(loads)) for loads in LIGHT_DISTRIBUTIONS),
        *((HEAVY_WEIGHT, readings(loads)) for loads in HEAVY_DISTRIBUTIONS),
    ]
    placements.append((LIGHT_WEIGHT, readings((5.0, 2.0, 2.8, 0.2))))
    calibration = fit(placements)

    assert calibration.excluded_placements == 1
    assert any("clamped" in warning for warning in calibration.warnings)
    assert len(calibration.placements) == 11


def test_single_mass_with_clamped_empty_cell_is_rejected():
    empty = [readings((0.0, 0.0, 0.0, 0.0))]
    placements = [
        (LIGHT_WEIGHT, readings(loads)) for loads in LIGHT_DISTRIBUTIONS
    ]

    with pytest.raises(ValueError, match="two different known masses"):
        fit_calibration(empty, placements)


def test_single_fully_known_mass_is_not_enough_to_break_gain_scale():
    unclamped_offsets = dict(OFFSETS)
    unclamped_offsets["bottom-left"] = 0.25
    distributions = (
        *LIGHT_DISTRIBUTIONS,
        (3.5, 3.0, 1.5, 2.0),
        (1.5, 3.0, 3.5, 2.0),
        (2.0, 1.5, 3.0, 3.5),
        (3.0, 2.0, 1.5, 3.5),
    )
    empty = [readings((0.0, 0.0, 0.0, 0.0), unclamped_offsets)] * 5
    placements = [
        (LIGHT_WEIGHT, readings(loads, unclamped_offsets))
        for loads in distributions
    ]

    with pytest.raises(ValueError, match="two different known masses"):
        fit_calibration(empty, placements, timestamp="synthetic")


def test_closure_protocol_recovers_two_unknown_bases_and_calibration():
    empty = [readings((0.0, 0.0, 0.0, 0.0))] * 5
    known_weight = 0.5
    first_object = 6.0
    second_object = 4.5
    jug = readings((1.5, 1.5, 1.5, 1.5))
    additions = (
        (2.0, 1.5, 1.5, 1.5),
        (1.5, 2.0, 1.5, 1.5),
        (1.5, 1.5, 2.0, 1.5),
        (1.5, 1.5, 1.5, 2.0),
    )
    placements = [
        (("X1",), 0.0, jug),
        *((("X1",), known_weight, readings(loads)) for loads in additions),
        (("X1", "X2"), 0.0, readings((2.625, 2.625, 2.625, 2.625))),
        (("X2",), 0.0, readings((1.125, 1.125, 1.125, 1.125))),
    ]

    calibration = fit_calibration(empty, placements, timestamp="synthetic")

    assert calibration.iterations < 20_000
    assert calibration.final_delta < 1e-7
    assert calibration.fitted_bases_kg["X1"] == pytest.approx(first_object, abs=0.05)
    assert calibration.fitted_bases_kg["X2"] == pytest.approx(second_object, abs=0.05)
    assert calibration.exactly_determined_offsets == ("bottom-left",)
    assert ("X1", "X2") in calibration.placement_bases
    for corner in CORNERS:
        assert calibration.gains[corner] == pytest.approx(GAINS[corner], rel=0.02)
        assert calibration.offsets[corner] == pytest.approx(OFFSETS[corner], abs=0.05)
    occupant = readings((18.0, 20.0, 17.0, 20.0))
    assert sum(apply_calibration(occupant, calibration).values()) == pytest.approx(
        75.0, abs=0.15
    )


def test_guard_requires_closure_for_hidden_offset():
    empty = [readings((0.0, 0.0, 0.0, 0.0))]
    placements = [
        (("X1",), 0.0, readings((1.5, 1.5, 1.5, 1.5))),
        (("X1",), 0.5, readings((2.0, 1.5, 1.5, 1.5))),
        (("X1",), 0.5, readings((1.5, 2.0, 1.5, 1.5))),
        (("X1",), 0.5, readings((1.5, 1.5, 2.0, 1.5))),
    ]

    with pytest.raises(
        ValueError,
        match="cannot separate bottom-left's hidden offset: add a closure step",
    ):
        fit_calibration(empty, placements)


@pytest.mark.parametrize(
    ("combinations", "expected"),
    [
        ((("X1",), ("X1", "X2"), ("X2",)), True),
        ((("X1",), ("X1",)), False),
        ((("X1",), ()), True),
    ],
)
def test_hidden_offset_rank_identifiability(combinations, expected):
    assert offset_is_identifiable(combinations) is expected


def test_duplicate_closure_capture_provides_internal_redundancy():
    empty = [readings((0.0, 0.0, 0.0, 0.0))] * 5
    x1 = readings((1.5, 1.5, 1.5, 1.5))
    x2 = readings((1.125, 1.125, 1.125, 1.125))
    placements = [
        (("X1",), 0.0, x1),
        (("X1",), 0.5, readings((2.0, 1.5, 1.5, 1.5))),
        (("X1",), 0.5, readings((1.5, 2.0, 1.5, 1.5))),
        (("X1", "X2"), 0.0, readings((2.625, 2.625, 2.625, 2.625))),
        (("X2",), 0.0, x2),
        (("X2",), 0.0, x2),
    ]

    calibration = fit_calibration(empty, placements, timestamp="synthetic")

    assert "bottom-left" not in calibration.exactly_determined_offsets


def test_guard_rejects_data_without_any_known_mass_anchor():
    empty = [readings((0.0, 0.0, 0.0, 0.0))]
    placements = [
        ("X", 0.0, readings((1.5, 1.5, 1.5, 1.5))),
        ("X", 0.0, readings((2.0, 1.0, 1.5, 1.5))),
        ("Y", 0.0, readings((2.0, 2.0, 2.0, 2.0))),
        ("Y", 0.0, readings((3.0, 1.5, 1.5, 2.0))),
    ]

    with pytest.raises(ValueError, match="no known mass anchor"):
        fit_calibration(empty, placements)


def test_ingestion_transform_and_zero_rule():
    calibration = Calibration(
        version=2,
        ts="2026-07-14T12:00:00.000000Z",
        ref_weights_kg=(10.0, 25.0),
        gains=GAINS,
        offsets=OFFSETS,
        residual_rms={corner: 0.0 for corner in CORNERS},
        excluded_placements=0,
        placements=(),
        placement_weights_kg=(),
    )
    measured = readings((2.0, 3.0, 4.0, 0.0))
    measured["bottom-left"] = 0.0

    corrected = apply_calibration(measured, calibration)
    assert corrected == pytest.approx(
        {
            "top-right": 2.0,
            "bottom-right": 3.0,
            "top-left": 4.0,
            "bottom-left": 0.0,
        }
    )
    assert apply_calibration(measured, None) == measured


def test_calibration_json_round_trip(tmp_path):
    calibration = fit()
    path = save_calibration(calibration, tmp_path / "nested" / "calibration.json")
    loaded = load_calibration(path)

    assert loaded is not None
    assert loaded.to_dict() == calibration.to_dict()
    assert path.read_text().startswith('{\n  "version": 2,')


def test_transform_then_state_machine_recovers_weight_with_clamped_offset():
    gains = {corner: 1.0 for corner in CORNERS}
    offsets = dict(zip(CORNERS, (2.1, 0.6, 0.3, -0.55)))
    calibration = Calibration(
        version=2,
        ts="synthetic",
        ref_weights_kg=(10.0, 25.0),
        gains=gains,
        offsets=offsets,
        residual_rms={corner: 0.0 for corner in CORNERS},
        excluded_placements=0,
        placements=(),
        placement_weights_kg=(),
    )
    machine = ScaleStateMachine()
    results = []

    def feed(start, duration, loads):
        for index in range(int(duration * 70)):
            timestamp = start + index / 70
            raw = {
                corner: max(load + offsets[corner], 0.0)
                for corner, load in zip(CORNERS, loads)
            }
            corrected = apply_calibration(raw, calibration)
            result = machine.update(
                timestamp,
                sum(corrected.values()),
                corrected,
                raw_total_kg=sum(raw.values()),
            )
            if result is not None:
                results.append(result)

    feed(10_000.0, 5.5, (0.0, 0.0, 0.0, 0.0))
    feed(10_005.5, 3.2, (18.0, 20.0, 17.0, 20.0))

    assert len(results) == 1
    assert results[0].weight_kg == pytest.approx(75.0)
    assert results[0].raw_kg == pytest.approx(74.45)
