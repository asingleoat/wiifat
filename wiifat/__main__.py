"""Command-line interface for WiiFat."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

from . import calibrate, chart, monitor, scale
from .db import Database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wiifat", description="Wii Balance Board scale tools")
    subparsers = parser.add_subparsers(dest="command", required=True)

    monitor_parser = subparsers.add_parser("monitor", help="show a live load-cell readout")
    monitor_parser.set_defaults(handler=_run_monitor)

    scale_parser = subparsers.add_parser("scale", help="run the automatic logging scale")
    scale_parser.add_argument("--db", help="measurement database path")
    scale_parser.add_argument("--config", help="calibration JSON path")
    scale_parser.add_argument("--once", action="store_true", help="exit after one measurement")
    scale_parser.add_argument(
        "--idle-timeout",
        type=scale.idle_timeout_arg,
        default=60.0,
        help="seconds idle after a weigh-in before power-off; 0 disables (default: 60)",
    )
    scale_parser.add_argument(
        "--no-activity-timeout",
        type=scale.idle_timeout_arg,
        default=300.0,
        help=(
            "seconds connected without a weigh-in before power-off; "
            "0 disables (default: 300)"
        ),
    )
    scale_parser.set_defaults(handler=_run_scale)

    calibrate_parser = subparsers.add_parser(
        "calibrate", help="fit a matrix calibration interactively"
    )
    calibrate_parser.add_argument(
        "--rounds",
        type=calibrate.rounds_arg,
        default=1,
        help="four-corner addition rounds (default: 1)",
    )
    calibrate_parser.add_argument(
        "--check", action="store_true", help="run a 10-second check after fitting"
    )
    calibrate_parser.add_argument("--config", help="calibration JSON output path")
    calibrate_parser.set_defaults(handler=_run_calibrate)

    log_parser = subparsers.add_parser("log", help="print recent measurements")
    log_parser.add_argument(
        "-n", type=int, default=10, help="number of rows to show (default: 10)"
    )
    log_parser.add_argument("--db", help="measurement database path")
    log_parser.set_defaults(handler=_run_log)

    chart_parser = subparsers.add_parser("chart", help="save a weight-history PNG")
    chart_parser.add_argument("--db", help="measurement database path")
    chart_parser.add_argument(
        "--out", default="weight.png", help="output path (default: weight.png)"
    )
    chart_parser.add_argument("--days", type=int, help="only include the last N days")
    chart_parser.add_argument("--user", type=int, help="only include one user id")
    chart_parser.set_defaults(handler=_run_chart)

    serve_parser = subparsers.add_parser(
        "serve", help="run the local web UI and scale daemon"
    )
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8480)
    serve_parser.add_argument("--db", help="measurement database path")
    serve_parser.add_argument("--config", help="calibration JSON path")
    serve_parser.add_argument(
        "--idle-timeout", type=scale.idle_timeout_arg, default=60.0
    )
    serve_parser.add_argument(
        "--no-activity-timeout", type=scale.idle_timeout_arg, default=300.0
    )
    serve_parser.set_defaults(handler=_run_serve)
    return parser


def _run_monitor(_args: argparse.Namespace) -> int:
    return monitor.main()


def _run_scale(args: argparse.Namespace) -> int:
    return scale.run(
        args.db,
        args.config,
        once=args.once,
        idle_timeout_s=args.idle_timeout,
        no_activity_timeout_s=args.no_activity_timeout,
    )


def _run_calibrate(args: argparse.Namespace) -> int:
    return calibrate.run(
        rounds=args.rounds,
        check=args.check,
        config_path=args.config,
    )


def _run_log(args: argparse.Namespace) -> int:
    measurements = Database(args.db).fetch_recent(args.n)
    print(
        f"{'TIMESTAMP (UTC)':<27} {'WEIGHT':>9} {'STDEV':>8} {'TARE':>8} "
        f"{'DURATION':>9} {'BATTERY':>8}"
    )
    for item in measurements:
        timestamp = datetime.fromtimestamp(item.timestamp, timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        battery = f"{item.battery_pct}%" if item.battery_pct is not None else "—"
        print(
            f"{timestamp:<27} {item.weight_kg:8.2f}kg {item.stdev_kg:7.3f} "
            f"{item.tare_kg:7.2f} {item.duration_s:8.2f}s {battery:>8}"
        )
    return 0


def _run_chart(args: argparse.Namespace) -> int:
    chart.render_chart(args.db, args.out, args.days, user_id=args.user)
    return 0


def _run_serve(args: argparse.Namespace) -> int:
    from . import server

    return server.run(
        host=args.host,
        port=args.port,
        db_path=args.db,
        config_path=args.config,
        idle_timeout_s=args.idle_timeout,
        no_activity_timeout_s=args.no_activity_timeout,
    )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
