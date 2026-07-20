import math
from datetime import datetime, timedelta, timezone

import pytest

import wiifat.chart as chart_module
from wiifat.chart import (
    TREND_TAU_DAYS,
    ewma_trend,
    render_chart,
    trend_slope_kg_per_week,
)
from wiifat.colors import color_from_key, user_color
from wiifat.db import Database
from wiifat.statemachine import Measurement


DAY = 86_400.0


def test_continuous_time_ewma_math():
    constant = ewma_trend([(0.0, 70.0), (DAY, 70.0), (9 * DAY, 70.0)])
    assert [value for _, value in constant] == [70.0, 70.0, 70.0]

    tau_seconds = TREND_TAU_DAYS * DAY
    step = ewma_trend([(0.0, 0.0), (tau_seconds, 10.0)])
    assert step[-1][1] == pytest.approx(10.0 * (1.0 - math.exp(-1.0)))

    irregular = ewma_trend([(0.0, 0.0), (60.0, 10.0), (1000 * tau_seconds, 20.0)])
    assert irregular[1][1] < 0.01
    assert irregular[-1][1] == pytest.approx(20.0)


def test_trend_slope_recovers_linear_drift():
    trend = [(day * DAY, 80.0 + 0.1 * day) for day in range(21)]
    assert trend_slope_kg_per_week(trend) == pytest.approx(0.7)
    assert trend_slope_kg_per_week(trend[:1]) is None
    assert trend_slope_kg_per_week([(0.0, 70.0), (DAY, 70.2)]) is None


def test_chart_smoke(tmp_path, monkeypatch):
    database_path = tmp_path / "wiifat.sqlite3"
    database = Database(database_path)
    user = database.create_user("Chart User", 70.0)
    single_user = database.create_user("Single Point", 82.0)
    stored_color = color_from_key("chart color override")
    assert stored_color != user_color(user.name)
    user = database.update_user_color(user.id, stored_color)
    start = datetime.now(timezone.utc) - timedelta(days=4)
    measurement_ids = []
    for index, weight in enumerate((70.4, 70.0, 69.8, 82.1, 76.0)):
        measurement_ids.append(
            database.insert(
                Measurement(
                    timestamp=(start + timedelta(hours=24 * index)).timestamp(),
                    weight_kg=weight,
                    stdev_kg=0.05,
                    tare_kg=2.5,
                    corners={"top-left": 18.0, "top-right": 18.2},
                    duration_s=2.6,
                )
            )
        )
    for measurement_id in measurement_ids[:3]:
        database.assign_measurement(
            measurement_id, user.id, method="manual", confidence=None
        )
    database.assign_measurement(
        measurement_ids[3], single_user.id, method="manual", confidence=None
    )

    plotted_colors = []
    original_plot_user = chart_module._plot_user

    def record_plot_user(axis, name, color, samples):
        plotted_colors.append((name, color))
        return original_plot_user(axis, name, color, samples)

    monkeypatch.setattr(chart_module, "_plot_user", record_plot_user)

    output_path = render_chart(database_path, tmp_path / "chart.png")
    assert output_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert output_path.stat().st_size > 0
    assert (user.name, stored_color) in plotted_colors

    user_path = render_chart(
        database_path, tmp_path / "user-chart.png", user_id=user.id
    )
    assert user_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    single_path = render_chart(
        database_path, tmp_path / "single-chart.png", user_id=single_user.id
    )
    assert single_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
