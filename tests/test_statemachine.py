import math

import pytest

from wiifat.statemachine import ScaleStateMachine


HZ = 70
DT = 1 / HZ
CORNERS = ("top-right", "bottom-right", "top-left", "bottom-left")


def feed(machine, start, duration, total_fn):
    """Feed duration seconds, excluding the endpoint, and return results/time."""
    results = []
    count = int(duration * HZ)
    for index in range(count):
        t = start + index * DT
        total = total_fn(t, index)
        corners = {corner: total / 4 for corner in CORNERS}
        result = machine.update(t, total, corners)
        if result is not None:
            results.append(result)
    return results, start + count * DT


def test_idle_step_on_stable_step_off_emits_exactly_one_measurement():
    machine = ScaleStateMachine()
    results, t = feed(machine, 1_700_000_000.0, 6.0, lambda _t, _i: 2.5)
    more, t = feed(machine, t, 4.0, lambda _t, _i: 72.5)
    results.extend(more)
    more, t = feed(machine, t, 3.0, lambda _t, _i: 2.5)
    results.extend(more)

    assert len(results) == 1
    result = results[0]
    assert result.weight_kg == pytest.approx(70.0)
    assert result.tare_kg == pytest.approx(2.5)
    assert result.stdev_kg == pytest.approx(0.0)
    assert result.duration_s == pytest.approx(2.5, abs=DT)
    assert result.timestamp >= 1_700_000_008.5
    assert result.corners == pytest.approx({corner: 72.5 / 4 for corner in CORNERS})
    assert machine.state == machine.IDLE


def test_noisy_shifting_occupant_delays_until_stable_window():
    machine = ScaleStateMachine()
    results, t = feed(machine, 1000.0, 5.5, lambda _t, _i: 2.0)
    step_on = t
    more, t = feed(
        machine,
        t,
        3.0,
        lambda _t, i: 72.0 + (1.2 if i % 2 else -1.2),
    )
    results.extend(more)
    more, t = feed(machine, t, 3.0, lambda _t, i: 73.0 + 0.04 * math.sin(i))
    results.extend(more)

    assert len(results) == 1
    assert results[0].timestamp - step_on >= 5.4
    assert results[0].weight_kg == pytest.approx(71.0, abs=0.02)
    assert results[0].stdev_kg < 0.2


def test_step_off_before_stability_emits_nothing():
    machine = ScaleStateMachine()
    results, t = feed(machine, 2000.0, 5.0, lambda _t, _i: 2.5)
    more, t = feed(
        machine,
        t,
        2.0,
        lambda _t, i: 72.5 + (1.0 if i % 2 else -1.0),
    )
    results.extend(more)
    more, _ = feed(machine, t, 3.0, lambda _t, _i: 2.5)
    results.extend(more)

    assert results == []
    assert machine.state == machine.IDLE


def test_idle_baseline_drift_is_absorbed_into_tare():
    machine = ScaleStateMachine()
    results, t = feed(
        machine,
        3000.0,
        12.0,
        lambda _t, i: 2.0 + 2.0 * i / (12 * HZ - 1),
    )
    more, _ = feed(machine, t, 3.2, lambda _t, _i: 74.0)
    results.extend(more)

    assert len(results) == 1
    assert results[0].tare_kg == pytest.approx(3.58, abs=0.03)
    assert results[0].weight_kg == pytest.approx(70.42, abs=0.03)


def test_no_second_measurement_while_occupied():
    machine = ScaleStateMachine()
    results, t = feed(machine, 4000.0, 5.0, lambda _t, _i: 2.0)
    more, t = feed(machine, t, 3.2, lambda _t, _i: 72.0)
    results.extend(more)
    more, t = feed(machine, t, 5.0, lambda _t, i: 78.0 + 0.02 * math.sin(i))
    results.extend(more)

    assert len(results) == 1
    assert machine.state == machine.MEASURED

    more, t = feed(machine, t, 2.2, lambda _t, _i: 2.0)
    results.extend(more)
    more, _ = feed(machine, t, 3.2, lambda _t, _i: 67.0)
    results.extend(more)
    assert len(results) == 2


def test_fast_consecutive_weigh_ins_with_brief_step_off():
    machine = ScaleStateMachine()
    results, t = feed(machine, 5000.0, 5.0, lambda _t, _i: 2.0)
    more, t = feed(machine, t, 4.0, lambda _t, _i: 74.0)
    results.extend(more)
    more, t = feed(machine, t, 1.5, lambda _t, _i: 2.0)
    results.extend(more)
    more, t = feed(machine, t, 4.0, lambda _t, _i: 73.6)
    results.extend(more)

    assert len(results) == 2
    assert results[1].weight_kg == pytest.approx(71.6, abs=0.1)


def test_snapshot_reports_state_and_growing_stability_progress():
    machine = ScaleStateMachine()
    _results, t = feed(machine, 6000.0, 1.0, lambda _t, _i: 2.0)
    assert machine.snapshot() == {
        "state": machine.IDLE,
        "fill": 0.0,
        "stdev_kg": None,
    }

    _results, t = feed(machine, t, 0.7, lambda _t, _i: 72.0)
    first = machine.snapshot()
    assert first["state"] == machine.MEASURING
    assert 0.0 < first["fill"] < 1.0
    assert first["stdev_kg"] == pytest.approx(0.0)

    _results, t = feed(machine, t, 0.7, lambda _t, _i: 72.0)
    second = machine.snapshot()
    assert first["fill"] < second["fill"] < 1.0

    results, _t = feed(machine, t, 2.0, lambda _t, _i: 72.0)
    assert len(results) == 1
    assert machine.snapshot() == {
        "state": machine.MEASURED,
        "fill": 1.0,
        "stdev_kg": None,
    }
