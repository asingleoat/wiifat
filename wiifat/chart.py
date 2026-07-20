"""Render weight history as a headless matplotlib chart."""

from __future__ import annotations

import argparse
import math
import os
from io import BytesIO
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

import matplotlib

matplotlib.use("Agg")
from matplotlib import pyplot as plt  # noqa: E402

from .colors import UNASSIGNED_COLOR
from .db import Database


TREND_TAU_DAYS = 10.0
_SECONDS_PER_DAY = 86_400.0
_SECONDS_PER_WEEK = 7.0 * _SECONDS_PER_DAY
_SLOPE_WINDOW_DAYS = 14.0
_MIN_SLOPE_SPAN_DAYS = 2.0


def ewma_trend(
    samples: Iterable[tuple[float, float]],
    tau_days: float = TREND_TAU_DAYS,
) -> list[tuple[float, float]]:
    """Return a continuous-time EWMA for chronological, irregular samples."""

    if tau_days <= 0.0:
        raise ValueError("tau_days must be positive")
    tau_seconds = tau_days * _SECONDS_PER_DAY
    trend: list[tuple[float, float]] = []
    previous_t: float | None = None
    previous_value: float | None = None
    for timestamp, weight in samples:
        timestamp = float(timestamp)
        weight = float(weight)
        if previous_t is None:
            value = weight
        else:
            elapsed = timestamp - previous_t
            if elapsed < 0.0:
                raise ValueError("samples must be in chronological order")
            alpha = 1.0 - math.exp(-elapsed / tau_seconds)
            assert previous_value is not None
            value = previous_value + alpha * (weight - previous_value)
        trend.append((timestamp, value))
        previous_t = timestamp
        previous_value = value
    return trend


def trend_slope_kg_per_week(
    trend: Sequence[tuple[float, float]],
    window_days: float = _SLOPE_WINDOW_DAYS,
) -> float | None:
    """Return the least-squares trend slope over the trailing time window."""

    if window_days <= 0.0:
        raise ValueError("window_days must be positive")
    if len(trend) < 2:
        return None
    if any(
        current[0] < previous[0]
        for previous, current in zip(trend, trend[1:])
    ):
        raise ValueError("trend points must be in chronological order")

    newest_t = trend[-1][0]
    full_span = newest_t - trend[0][0]
    if full_span > window_days * _SECONDS_PER_DAY:
        cutoff = newest_t - window_days * _SECONDS_PER_DAY
        points = [point for point in trend if point[0] >= cutoff]
    else:
        points = list(trend)
    if len(points) < 2:
        return None
    span = points[-1][0] - points[0][0]
    if span < _MIN_SLOPE_SPAN_DAYS * _SECONDS_PER_DAY:
        return None

    origin = points[0][0]
    times_in_weeks = [
        (timestamp - origin) / _SECONDS_PER_WEEK for timestamp, _ in points
    ]
    values = [value for _, value in points]
    mean_time = sum(times_in_weeks) / len(times_in_weeks)
    mean_value = sum(values) / len(values)
    denominator = sum((value - mean_time) ** 2 for value in times_in_weeks)
    if denominator == 0.0:
        return None
    return sum(
        (time_value - mean_time) * (weight - mean_value)
        for time_value, weight in zip(times_in_weeks, values)
    ) / denominator


def render_chart(
    db_path: str | os.PathLike[str] | None = None,
    out: str | os.PathLike[str] = "weight.png",
    days: int | None = None,
    *,
    user_id: int | None = None,
) -> Path:
    """Save weight scatter and per-user trends, returning the output path."""
    png = render_chart_png(db_path, days, user_id=user_id)
    output_path = Path(out).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(png)
    return output_path


def render_chart_png(
    db_path: str | os.PathLike[str] | None = None,
    days: int | None = None,
    *,
    user_id: int | None = None,
) -> bytes:
    """Render a general or per-user history chart as PNG bytes."""
    if days is not None and days < 0:
        raise ValueError("days must be nonnegative")

    database = Database(db_path)
    selected_user = None
    if user_id is not None:
        selected_user = database.get_user(user_id)
        measurements = database.fetch_for_user(user_id)
        if days is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
            measurements = [item for item in measurements if item.timestamp >= cutoff]
    elif days is None:
        measurements = database.fetch_all()
    else:
        measurements = database.fetch_since(datetime.now(timezone.utc) - timedelta(days=days))

    figure, axis = plt.subplots(figsize=(9, 5))
    if user_id is not None:
        name = selected_user.name if selected_user is not None else f"User {user_id}"
        _plot_user(
            axis,
            name,
            selected_user.color if selected_user is not None else UNASSIGNED_COLOR,
            [(item.timestamp, item.weight_kg) for item in measurements],
        )
    else:
        users = {user.id: user for user in database.list_users()}
        grouped: dict[int | None, list[tuple[float, float]]] = {}
        for item in measurements:
            group_id = item.user_id if item.user_id in users else None
            grouped.setdefault(group_id, []).append((item.timestamp, item.weight_kg))
        for known_user in users.values():
            if known_user.id not in grouped:
                continue
            _plot_user(
                axis,
                known_user.name,
                known_user.color,
                grouped[known_user.id],
            )
        if None in grouped:
            unassigned = grouped[None]
            axis.scatter(
                [_as_datetime(timestamp) for timestamp, _ in unassigned],
                [weight for _, weight in unassigned],
                s=18,
                alpha=0.35,
                color=UNASSIGNED_COLOR,
                label="Unassigned",
            )
    axis.set_title("Weight history" if user_id is None else "User weight history")
    axis.set_xlabel("Date (UTC)")
    axis.set_ylabel("Weight (kg)")
    axis.grid(alpha=0.25)
    handles, labels = axis.get_legend_handles_labels()
    if handles:
        axis.legend(handles, labels)
    figure.autofmt_xdate()
    figure.tight_layout()

    output = BytesIO()
    figure.savefig(output, format="png", dpi=140)
    plt.close(figure)
    return output.getvalue()


def _plot_user(
    axis,
    name: str,
    color: str,
    samples: Sequence[tuple[float, float]],
) -> None:
    trend = ewma_trend(samples)
    has_curve = len(trend) >= 2
    axis.scatter(
        [_as_datetime(timestamp) for timestamp, _ in samples],
        [weight for _, weight in samples],
        s=18,
        alpha=0.35,
        color=color,
        label="_nolegend_" if has_curve else name,
    )
    if not has_curve:
        return
    slope = trend_slope_kg_per_week(trend)
    label = name if slope is None else f"{name} ({slope:+.2f} kg/wk)"
    axis.plot(
        [_as_datetime(timestamp) for timestamp, _ in trend],
        [weight for _, weight in trend],
        linewidth=2.2,
        color=color,
        label=label,
    )


def _as_datetime(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, timezone.utc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="measurement database path")
    parser.add_argument("--out", default="weight.png", help="output PNG path")
    parser.add_argument("--days", type=int, help="only include the last N days")
    parser.add_argument("--user", type=int, help="only include one user id")
    args = parser.parse_args(argv)
    render_chart(args.db, args.out, args.days, user_id=args.user)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
