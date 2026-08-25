import math
from datetime import datetime, timedelta, timezone

import pytest
from matplotlib import dates as mdates
from matplotlib.collections import PathCollection

import wiifat.chart as chart_module
from wiifat.chart import (
    CHART_WINDOWS,
    TREND_TAU_DAYS,
    chart_window,
    ewma_trend,
    render_chart,
    render_chart_figure,
    trend_slope_kg_per_week,
)
from wiifat.units import POUNDS_PER_KG
from wiifat.colors import color_from_key, user_color
from wiifat.db import Database
from wiifat.statemachine import Measurement


DAY = 86_400.0


def test_chart_window_lookup():
    assert chart_window(None).key == "month"
    assert chart_window(None).days == 30
    assert chart_window("").key == "month"
    assert chart_window("").days == 30
    assert chart_window("year").days == 365
    assert chart_window("all").days is None
    with pytest.raises(ValueError, match="bogus"):
        chart_window("bogus")
    assert [window.key for window in CHART_WINDOWS] == ["month", "year", "all"]


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


def test_chart_has_synced_pounds_axis(tmp_path):
    for days in (None, 30):
        if days is None:
            figure = render_chart_figure(tmp_path / "empty.sqlite3")
        else:
            figure = render_chart_figure(
                tmp_path / "empty.sqlite3", days, now=1_700_000_000.0
            )
        try:
            kg_axis, pounds_axis = figure.axes
            low, high = kg_axis.get_ylim()
            assert kg_axis.get_ylabel() == "Weight (kg)"
            assert pounds_axis.get_ylabel() == "Weight (lb)"
            assert pounds_axis.get_ylim() == pytest.approx(
                (low * POUNDS_PER_KG, high * POUNDS_PER_KG)
            )
        finally:
            chart_module.plt.close(figure)


def test_windowed_render_clips_warmed_trend_and_scatter(tmp_path):
    database_path = tmp_path / "windowed.sqlite3"
    database = Database(database_path)
    now = 1_700_000_000.0
    cutoff = now - 30 * DAY
    user = database.create_user("Window User", 80.0, timestamp=now)
    samples = []
    for index in range(60):
        timestamp = now - (59 - index) * DAY
        if index == 29:
            timestamp += DAY / 4.0
        weight = 90.0 if index < 25 else 70.0
        samples.append((timestamp, weight))
        measurement_id = database.insert(
            Measurement(
                timestamp=timestamp,
                weight_kg=weight,
                stdev_kg=0.05,
                tare_kg=2.5,
                corners={"top-left": 18.0, "top-right": 18.2},
                duration_s=2.6,
            )
        )
        database.assign_measurement(
            measurement_id, user.id, method="manual", confidence=None
        )

    full_trend = ewma_trend(samples)
    before, after = next(
        (before, after)
        for before, after in zip(full_trend, full_trend[1:])
        if before[0] < cutoff <= after[0]
    )
    fraction = (cutoff - before[0]) / (after[0] - before[0])
    expected_boundary = before[1] + fraction * (after[1] - before[1])
    first_in_window_weight = next(
        weight for timestamp, weight in samples if timestamp >= cutoff
    )
    slope = trend_slope_kg_per_week(full_trend)
    assert slope is not None
    slope_text = f"{slope:+.2f} kg/wk"

    windowed_figure = None
    all_time_figure = None
    try:
        windowed_figure = render_chart_figure(
            database_path, 30, user_id=user.id, now=now
        )
        kg_axis = windowed_figure.axes[0]
        expected_xlim = (
            mdates.date2num(datetime.fromtimestamp(cutoff, timezone.utc)),
            mdates.date2num(datetime.fromtimestamp(now, timezone.utc)),
        )
        assert kg_axis.get_xlim() == pytest.approx(expected_xlim)
        scatter = next(
            collection
            for collection in kg_axis.collections
            if isinstance(collection, PathCollection)
        )
        assert len(scatter.get_offsets()) == sum(
            timestamp >= cutoff for timestamp, _ in samples
        )
        trend_line = kg_axis.lines[0]
        assert mdates.date2num(trend_line.get_xdata()[0]) == pytest.approx(
            expected_xlim[0]
        )
        assert trend_line.get_ydata()[0] == pytest.approx(expected_boundary)
        assert trend_line.get_ydata()[0] != pytest.approx(first_in_window_weight)
        assert slope_text in trend_line.get_label()
        assert kg_axis.get_title() == "User weight history (last 30 days)"

        all_time_figure = render_chart_figure(
            database_path, None, user_id=user.id, now=now
        )
        all_time_axis = all_time_figure.axes[0]
        all_time_line = all_time_axis.lines[0]
        first_x = mdates.date2num(
            datetime.fromtimestamp(samples[0][0], timezone.utc)
        )
        last_x = mdates.date2num(
            datetime.fromtimestamp(samples[-1][0], timezone.utc)
        )
        assert mdates.date2num(all_time_line.get_xdata()[0]) == pytest.approx(
            first_x
        )
        assert all_time_axis.get_xlim()[0] < first_x
        assert all_time_axis.get_xlim()[1] > last_x
        assert slope_text in all_time_line.get_label()
        assert all_time_axis.get_title() == "User weight history"
    finally:
        if windowed_figure is not None:
            chart_module.plt.close(windowed_figure)
        if all_time_figure is not None:
            chart_module.plt.close(all_time_figure)


def test_windowed_render_omits_users_without_recent_samples(tmp_path):
    database_path = tmp_path / "stale.sqlite3"
    database = Database(database_path)
    now = 1_700_000_000.0
    user = database.create_user("Stale User", 80.0, timestamp=now)
    for age_days in (40, 39, 38):
        measurement_id = database.insert(
            Measurement(
                timestamp=now - age_days * DAY,
                weight_kg=80.0,
                stdev_kg=0.05,
                tare_kg=2.5,
                corners={},
                duration_s=2.6,
            )
        )
        database.assign_measurement(
            measurement_id, user.id, method="manual", confidence=None
        )

    windowed = render_chart_figure(database_path, 30, user_id=user.id, now=now)
    all_time = render_chart_figure(database_path, None, user_id=user.id, now=now)
    try:
        kg_axis = windowed.axes[0]
        assert len(kg_axis.lines) == 0
        assert all(len(c.get_offsets()) == 0 for c in kg_axis.collections)
        assert kg_axis.get_legend() is None
        assert kg_axis.get_title() == "User weight history (last 30 days)"
        assert len(all_time.axes[0].lines) == 1
    finally:
        chart_module.plt.close(windowed)
        chart_module.plt.close(all_time)


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
    single_user = database.set_user_hidden(single_user.id, True)

    plotted_colors = []
    original_plot_user = chart_module._plot_user

    def record_plot_user(axis, name, color, samples, *, cutoff=None):
        plotted_colors.append((name, color))
        return original_plot_user(axis, name, color, samples, cutoff=cutoff)

    monkeypatch.setattr(chart_module, "_plot_user", record_plot_user)

    output_path = render_chart(database_path, tmp_path / "chart.png")
    assert output_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert output_path.stat().st_size > 0
    assert (user.name, stored_color) in plotted_colors
    assert (single_user.name, single_user.color) not in plotted_colors

    user_path = render_chart(
        database_path, tmp_path / "user-chart.png", user_id=user.id
    )
    assert user_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")

    single_path = render_chart(
        database_path, tmp_path / "single-chart.png", user_id=single_user.id
    )
    assert single_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert (single_user.name, single_user.color) in plotted_colors
