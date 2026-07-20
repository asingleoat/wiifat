"""POC Flask dashboard with a background Wii Balance Board scale daemon."""

from __future__ import annotations

import argparse
import json
import queue
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from flask import Flask, Response, abort, jsonify, redirect, render_template_string, request, url_for

from . import chart, scale
from .colors import UNASSIGNED_COLOR, color_from_key
from .db import Database, DuplicateUserNameError, User, format_timestamp
from .recognize import RecognitionResult, UserModel, recognize, update_belief
from .statemachine import Measurement


POUNDS_PER_KG = 2.2046226218


@dataclass
class BoardStatus:
    """Small synchronized status snapshot shared with the scale thread."""

    message: str = "Scale daemon not started."
    battery_pct: int | None = None


class EventPublisher:
    """Nonblocking in-process fan-out for the small home-LAN SSE audience."""

    def __init__(self, queue_size: int = 32) -> None:
        if queue_size <= 0:
            raise ValueError("queue_size must be positive")
        self.queue_size = queue_size
        self._lock = threading.Lock()
        self._clients: set[queue.Queue[str]] = set()

    def subscribe(self) -> queue.Queue[str]:
        """Register and return one bounded client queue."""
        client: queue.Queue[str] = queue.Queue(maxsize=self.queue_size)
        with self._lock:
            self._clients.add(client)
        return client

    def unsubscribe(self, client: queue.Queue[str]) -> None:
        """Discard a disconnected client's queue."""
        with self._lock:
            self._clients.discard(client)

    def publish(self, event: str, payload: dict[str, object]) -> None:
        """Publish without blocking, dropping each slow client's oldest event."""
        message = _sse_message(event, payload)
        with self._lock:
            for client in self._clients:
                if client.full():
                    try:
                        client.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    client.put_nowait(message)
                except queue.Full:
                    pass

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)


def _sse_message(event: str, payload: dict[str, object]) -> str:
    data = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n"


def _rename_error(message: str, status: int, wants_json: bool) -> Response:
    if wants_json:
        response = jsonify({"ok": False, "error": message})
        response.status_code = status
        return response
    return Response(message, status=status, mimetype="text/plain")


_STYLE = """
<style>
body { font-family: sans-serif; max-width: 1100px; margin: 2rem auto; padding: 0 1rem; color: #222; }
h1, h2 { margin-bottom: .5rem; }
table { border-collapse: collapse; width: 100%; margin: .75rem 0 1.5rem; }
th, td { text-align: left; border-bottom: 1px solid #ddd; padding: .45rem; vertical-align: top; }
.badge { display: inline-block; border-radius: .8rem; padding: .15rem .55rem; color: #fff; }
.status { padding: .65rem; background: #f2f5f7; border-radius: .3rem; }
.live-message { font-size: 1.15rem; font-weight: 600; }
.live-detail { margin-top: .35rem; }
.progress-track { height: .7rem; margin-top: .5rem; background: #d8dde1; border-radius: .4rem; overflow: hidden; }
.progress-fill { width: 0; height: 100%; background: #3d8b5f; transition: width .15s linear; }
form.inline { display: inline; margin-right: .25rem; }
input, button { padding: .3rem .45rem; margin: .15rem; }
button.small { font-size: .8rem; padding: .15rem .35rem; }
.user-heading { display: flex; align-items: center; gap: .4rem; }
.muted { color: #666; }
img.chart { width: 100%; max-width: 900px; }
</style>
"""


_DASHBOARD = """
<!doctype html><title>WiiFat</title>{{ style|safe }}
<h1>WiiFat</h1>
<div id="live-panel" class="status">
  <div id="live-message" class="live-message">{{ status.message }}</div>
  <div id="live-detail" class="live-detail"></div>
  <div id="live-progress" class="progress-track" hidden><div id="live-progress-fill" class="progress-fill"></div></div>
  <div id="live-battery" class="muted">{% if status.battery_pct is not none %}Battery {{ status.battery_pct }}%{% endif %}</div>
</div>

<h2>Users</h2>
<form method="post" action="{{ url_for('create_user_route') }}">
  <input name="name" required placeholder="Name">
  <input name="seed_weight" type="number" min="0.01" step="0.01" placeholder="Optional seed kg">
  <button type="submit">Create user</button>
</form>
<table><tr><th>User</th><th>Latest</th><th>Assigned count</th><th>Model</th></tr>
{% for summary in summaries %}<tr>
  <td><a class="badge" style="background: {{ summary.user.color }}" href="{{ url_for('user_page', user_id=summary.user.id) }}">{{ summary.user.name }}</a>
    <form class="inline" method="post" action="{{ url_for('reroll_user_color_route', user_id=summary.user.id) }}"><button class="small" type="submit">reroll color</button></form></td>
  <td>{{ summary.latest.weight_kg|weight if summary.latest else '—' }}</td>
  <td>{{ summary.count }}</td>
  <td>{% if summary.user.mu_kg is not none %}μ {{ '%.2f'|format(summary.user.mu_kg) }} kg,
      σ {{ '%.2f'|format(summary.user.sigma_kg) }} kg{% else %}unseeded{% endif %}</td>
</tr>{% else %}<tr><td colspan="4">No users yet.</td></tr>{% endfor %}</table>

<h2>Unclaimed</h2>
<table><tr><th>Time</th><th>Weight</th><th>Claim</th></tr>
{% for item in unclaimed %}<tr><td>{{ item.timestamp|timestamp }}</td><td>{{ item.weight_kg|weight }}</td><td>
  {% for user in users %}<form class="inline" method="post" action="{{ url_for('assign_route') }}">
    <input type="hidden" name="measurement_id" value="{{ item.id }}">
    <input type="hidden" name="user_id" value="{{ user.id }}">
    <button type="submit">{{ user.name }}</button>
  </form>{% endfor %}
  <form class="inline" method="post" action="{{ url_for('create_user_route') }}">
    <input type="hidden" name="measurement_id" value="{{ item.id }}">
    <input name="name" required size="10" placeholder="New name">
    <button type="submit">Create &amp; claim</button>
  </form>
</td></tr>{% else %}<tr><td colspan="3">Nothing waiting for a claim.</td></tr>{% endfor %}</table>

<h2>Latest weigh-ins</h2>
<table><tr><th>Time</th><th>Weight</th><th>User</th><th>Assignment</th></tr>
{% for item in latest %}<tr><td>{{ item.timestamp|timestamp }}</td><td>{{ item.weight_kg|weight }}</td>
<td>{% if item.user_id %}<span class="badge" style="background: {{ users_by_id[item.user_id].color }}">{{ users_by_id[item.user_id].name }}</span>{% else %}<span class="badge" style="background: {{ unassigned_color }}">unclaimed</span>{% endif %}</td>
<td>{{ item.assign_method or '—' }}{% if item.assign_confidence is not none %} {{ '%.1f%%'|format(100 * item.assign_confidence) }}{% endif %}
{% if item.user_id %}<form class="inline" method="post" action="{{ url_for('unassign_route') }}"><input type="hidden" name="measurement_id" value="{{ item.id }}"><button type="submit">Unassign</button></form>{% endif %}</td>
</tr>{% else %}<tr><td colspan="4">No measurements yet.</td></tr>{% endfor %}</table>
<img class="chart" src="{{ url_for('chart_all') }}" alt="Weight chart">
<script>
(() => {
  const message = document.getElementById("live-message");
  const detail = document.getElementById("live-detail");
  const progress = document.getElementById("live-progress");
  const progressFill = document.getElementById("live-progress-fill");
  const battery = document.getElementById("live-battery");
  const unassignedColor = "{{ unassigned_color }}";
  let showingResult = false;

  function payload(event) {
    try { return JSON.parse(event.data); } catch (_error) { return {}; }
  }

  function showBattery(value) {
    if (value !== null && value !== undefined) battery.textContent = `Battery ${value}%`;
  }

  function setText(value) {
    message.replaceChildren(document.createTextNode(value));
  }

  const events = new EventSource("{{ url_for('events') }}");
  events.addEventListener("status", (event) => {
    const data = payload(event);
    showBattery(data.battery_pct);
    if (showingResult) return;
    const raw = String(data.message || "");
    if (/waiting|disconnected|powered off|stopped/i.test(raw)) {
      setText("Board off — press its power button");
    } else if (raw) {
      setText(raw);
    }
  });

  events.addEventListener("progress", (event) => {
    const data = payload(event);
    if (showingResult) return;
    const fill = Math.max(0, Math.min(1, Number(data.fill) || 0));
    progressFill.style.width = `${100 * fill}%`;
    progressFill.setAttribute("aria-valuenow", String(Math.round(100 * fill)));
    if (data.state === "IDLE") {
      setText("Ready — step on to weigh.");
      detail.textContent = "";
      progress.hidden = true;
    } else if (data.state === "MEASURING") {
      setText("Measuring — hold still…");
      const total = Number(data.total_kg);
      const stdev = data.stdev_kg === null ? null : Number(data.stdev_kg);
      detail.textContent = Number.isFinite(total) ? `${total.toFixed(1)} kg` : "";
      if (Number.isFinite(stdev) && stdev >= 0.2) detail.textContent += " — settling…";
      progress.hidden = false;
    } else if (data.state === "MEASURED") {
      setText("Measurement complete…");
      progressFill.style.width = "100%";
    }
  });

  events.addEventListener("measurement", (event) => {
    const data = payload(event);
    showingResult = true;
    progress.hidden = true;
    detail.textContent = "";
    const kg = Number(data.weight_kg);
    const pounds = kg * {{ pounds_per_kg }};
    setText(`${kg.toFixed(2)} kg / ${pounds.toFixed(1)} lb`);
    const badge = document.createElement("span");
    badge.className = "badge";
    if (data.user) {
      badge.style.background = data.user.color;
      badge.textContent = data.user.name;
    } else {
      badge.style.background = unassignedColor;
      badge.textContent = "unclaimed";
    }
    message.append(document.createTextNode(" "), badge);
    showBattery(data.battery_pct);
    window.setTimeout(() => window.location.reload(), 1500);
  });

  events.onerror = () => {
    if (!showingResult) setText("Live updates reconnecting…");
  };
})();
</script>
"""


_USER_PAGE = """
<!doctype html><title>{{ user.name }} — WiiFat</title>{{ style|safe }}{{ rename_style|safe }}
<p><a href="{{ url_for('dashboard') }}">← Dashboard</a></p>
<div class="user-heading"><h1><span class="badge editable-user-name" style="background: {{ user.color }}" data-user-name="{{ user.name }}" data-rename-url="{{ url_for('rename_user_route', user_id=user.id) }}" title="Double-click to edit display name">{{ user.name }}</span><span class="rename-error" aria-live="polite"></span></h1>
<form class="inline" method="post" action="{{ url_for('reroll_user_color_route', user_id=user.id) }}"><button class="small" type="submit">reroll color</button></form></div>
<form class="rename-fallback" method="post" action="{{ url_for('rename_user_route', user_id=user.id) }}">
  <label>Display name <input name="name" required value="{{ user.name }}"></label><button type="submit">Rename</button>
</form>
<form id="hidden-user-form" method="post" action="{{ url_for('set_user_hidden_route', user_id=user.id) }}">
  <label><input id="hidden-user-toggle" type="checkbox" name="hidden" value="1"{% if user.hidden %} checked{% endif %}> Hide from dashboard</label>
  <button type="submit">Save</button>
</form>
<p>{{ history|length }} assigned measurements. Model:
{% if user.mu_kg is not none %}μ {{ '%.2f'|format(user.mu_kg) }} kg,
σ {{ '%.2f'|format(user.sigma_kg) }} kg{% else %}unseeded{% endif %}.</p>
<img class="chart" src="{{ url_for('chart_user', user_id=user.id) }}" alt="{{ user.name }} chart">
<table><tr><th>Time</th><th>Weight</th><th>Method</th><th></th></tr>
{% for item in history %}<tr><td>{{ item.timestamp|timestamp }}</td><td>{{ item.weight_kg|weight }}</td>
<td>{{ item.assign_method or '—' }}</td><td><form method="post" action="{{ url_for('unassign_route') }}"><input type="hidden" name="measurement_id" value="{{ item.id }}"><button>Unassign</button></form></td></tr>
{% endfor %}</table>
{{ rename_script|safe }}
<script>
document.getElementById("hidden-user-toggle").addEventListener("change", (event) => {
  event.target.form.submit();
});
</script>
"""


_USER_RENAME_STYLE = """
<style>
.badge .rename-input { width: auto; margin: 0; padding: 0; border: 0; outline: 1px solid #fff; background: transparent; color: #fff; font: inherit; }
.rename-error { margin-left: .3rem; color: #b42318; font-size: .8rem; }
.rename-fallback { font-size: .8rem; }
</style>
"""


_RENAME_SCRIPT = """
<script>
document.querySelectorAll(".editable-user-name").forEach((badge) => {
  let editing = false;

  function errorNode() {
    return badge.parentElement.querySelector(".rename-error");
  }

  function beginEdit() {
    if (editing) return;
    editing = true;
    const originalName = badge.dataset.userName;
    const error = errorNode();
    if (error) error.textContent = "";
    const input = document.createElement("input");
    input.className = "rename-input";
    input.value = originalName;
    input.size = Math.max(4, originalName.length + 1);
    badge.replaceChildren(input);
    input.focus();
    input.select();
    let settled = false;

    function restore(name) {
      badge.replaceChildren(document.createTextNode(name));
      editing = false;
    }

    async function commit() {
      if (settled) return;
      settled = true;
      input.disabled = true;
      try {
        const response = await fetch(badge.dataset.renameUrl, {
          method: "POST",
          headers: {"Accept": "application/json", "Content-Type": "application/json"},
          body: JSON.stringify({name: input.value})
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.error || "Could not rename user.");
        badge.dataset.userName = result.name;
        restore(result.name);
      } catch (failure) {
        restore(originalName);
        if (error) error.textContent = failure.message || "Could not rename user.";
      }
    }

    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        commit();
      } else if (event.key === "Escape") {
        event.preventDefault();
        settled = true;
        restore(originalName);
      }
    });
    input.addEventListener("blur", commit);
  }

  badge.addEventListener("dblclick", (event) => {
    event.preventDefault();
    beginEdit();
  });
});
</script>
"""


def create_app(
    db_path: str | None = None,
    *,
    initial_status: str = "Scale daemon not started.",
    time_fn: Callable[[], float] = time.time,
) -> Flask:
    """Create the web app without starting hardware, suitable for test clients."""
    app = Flask(__name__)
    database = Database(db_path)
    status = BoardStatus(initial_status)
    status_lock = threading.Lock()
    progress_lock = threading.Lock()
    publisher = EventPublisher()
    last_progress_t: float | None = None
    last_progress_state: str | None = None

    def status_payload() -> dict[str, object]:
        with status_lock:
            return {
                "message": status.message,
                "battery_pct": status.battery_pct,
            }

    def set_status(message: str) -> None:
        with status_lock:
            status.message = message
        publisher.publish("status", status_payload())

    def progress_callback(
        timestamp: float,
        snapshot: dict[str, str | float | None],
        total_kg: float,
    ) -> None:
        nonlocal last_progress_t, last_progress_state
        state = str(snapshot["state"])
        with progress_lock:
            state_changed = state != last_progress_state
            due = (
                last_progress_t is None
                or timestamp < last_progress_t
                or timestamp - last_progress_t >= 0.25
            )
            if not state_changed and not due:
                return
            last_progress_t = timestamp
            last_progress_state = state
        stdev_kg = snapshot.get("stdev_kg")
        publisher.publish(
            "progress",
            {
                "state": state,
                "total_kg": float(total_kg),
                "fill": float(snapshot.get("fill") or 0.0),
                "stdev_kg": float(stdev_kg) if stdev_kg is not None else None,
            },
        )

    def measurement_callback(
        measurement_id: int, measurement: Measurement
    ) -> RecognitionResult:
        users = database.list_users()
        result = recognize(
            measurement.weight_kg,
            measurement.timestamp,
            [_user_model(user) for user in users],
        )
        assigned_user = None
        if result.assigned_user_id is not None:
            database.assign_measurement(
                measurement_id,
                result.assigned_user_id,
                method="auto",
                confidence=result.confidence,
            )
            user = database.get_user(result.assigned_user_id)
            if user is not None:
                assigned_user = _update_stored_belief(database, user, measurement)
        with status_lock:
            status.battery_pct = measurement.battery_pct
        publisher.publish("status", status_payload())
        publisher.publish(
            "measurement",
            {
                "id": measurement_id,
                "ts": format_timestamp(measurement.timestamp),
                "weight_kg": measurement.weight_kg,
                "battery_pct": measurement.battery_pct,
                "user": (
                    {
                        "id": assigned_user.id,
                        "name": assigned_user.name,
                        "color": assigned_user.color,
                    }
                    if assigned_user is not None
                    else None
                ),
                "method": "auto" if assigned_user is not None else None,
                "confidence": (
                    result.confidence if assigned_user is not None else None
                ),
            },
        )
        return result

    def inject_measurement(measurement: Measurement) -> tuple[int, RecognitionResult]:
        measurement_id = database.insert(measurement)
        return measurement_id, measurement_callback(measurement_id, measurement)

    app.extensions["wiifat"] = {
        "database": database,
        "status": status,
        "set_status": set_status,
        "on_measurement": measurement_callback,
        "on_progress": progress_callback,
        "publisher": publisher,
        "inject_measurement": inject_measurement,
    }

    @app.template_filter("weight")
    def weight_filter(weight_kg: float) -> str:
        return f"{weight_kg:.2f} kg / {weight_kg * POUNDS_PER_KG:.1f} lb"

    @app.template_filter("timestamp")
    def timestamp_filter(timestamp: float) -> str:
        return format_timestamp(timestamp)

    @app.get("/events")
    def events() -> Response:
        client = publisher.subscribe()
        initial_status = status_payload()

        def stream():
            try:
                yield _sse_message("status", initial_status)
                while True:
                    try:
                        yield client.get(timeout=15.0)
                    except queue.Empty:
                        yield ": ping\n\n"
            finally:
                publisher.unsubscribe(client)

        # Flask's threaded development server dedicates one thread to each
        # open SSE stream. That is acceptable for this home-LAN POC.
        return Response(
            stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/")
    def dashboard() -> str:
        all_users = database.list_users()
        users = [user for user in all_users if not user.hidden]
        latest = database.fetch_recent(20)
        summaries = []
        for user in users:
            history = database.fetch_for_user(user.id, newest_first=True)
            summaries.append(
                {"user": user, "latest": history[0] if history else None, "count": len(history)}
            )
        with status_lock:
            status_snapshot = BoardStatus(status.message, status.battery_pct)
        return render_template_string(
            _DASHBOARD,
            style=_STYLE,
            status=status_snapshot,
            users=users,
            summaries=summaries,
            latest=latest,
            unclaimed=database.fetch_unassigned(20),
            users_by_id={user.id: user for user in all_users},
            unassigned_color=UNASSIGNED_COLOR,
            pounds_per_kg=POUNDS_PER_KG,
        )

    @app.get("/user/<int:user_id>")
    def user_page(user_id: int) -> str:
        user = database.get_user(user_id)
        if user is None:
            abort(404)
        return render_template_string(
            _USER_PAGE,
            style=_STYLE,
            rename_style=_USER_RENAME_STYLE,
            user=user,
            history=database.fetch_for_user(user_id, newest_first=True),
            rename_script=_RENAME_SCRIPT,
        )

    @app.get("/chart.png")
    def chart_all() -> Response:
        return Response(chart.render_chart_png(database.path), mimetype="image/png")

    @app.get("/chart/<int:user_id>.png")
    def chart_user(user_id: int) -> Response:
        if database.get_user(user_id) is None:
            abort(404)
        return Response(
            chart.render_chart_png(database.path, user_id=user_id),
            mimetype="image/png",
        )

    @app.post("/users")
    def create_user_route() -> Response:
        seed_text = request.form.get("seed_weight", "").strip()
        try:
            seed = float(seed_text) if seed_text else None
            measurement_text = request.form.get("measurement_id", "").strip()
            measurement = (
                database.fetch_measurement(int(measurement_text))
                if measurement_text
                else None
            )
            if measurement_text and measurement is None:
                raise KeyError("unknown measurement")
            user = database.create_user(
                request.form.get("name", ""),
                None if measurement_text else seed,
            )
            if measurement is not None:
                database.assign_measurement(
                    measurement.id, user.id, method="manual", confidence=None
                )
                _update_stored_belief(database, user, measurement)
        except (KeyError, ValueError, sqlite3.IntegrityError) as exc:
            abort(400, str(exc))
        return redirect(url_for("dashboard"))

    @app.post("/users/<int:user_id>/reroll-color")
    def reroll_user_color_route(user_id: int) -> Response:
        try:
            database.update_user_color(user_id, color_from_key(str(time_fn())))
        except KeyError:
            abort(404)
        return redirect(request.referrer or url_for("dashboard"))

    @app.post("/users/<int:user_id>/rename")
    def rename_user_route(user_id: int) -> Response:
        wants_json = request.is_json or "application/json" in request.headers.get(
            "Accept", ""
        ).lower()
        submitted = request.get_json(silent=True) if request.is_json else request.form
        raw_name = submitted.get("name", "") if hasattr(submitted, "get") else ""
        if not isinstance(raw_name, str):
            raw_name = ""
        try:
            user = database.rename_user(user_id, raw_name)
        except DuplicateUserNameError:
            return _rename_error("That display name is already in use.", 409, wants_json)
        except ValueError:
            return _rename_error("Display name must not be empty.", 400, wants_json)
        except KeyError:
            return _rename_error("User not found.", 404, wants_json)
        if wants_json:
            return jsonify({"ok": True, "id": user.id, "name": user.name})
        return redirect(request.referrer or url_for("dashboard"))

    @app.post("/users/<int:user_id>/hidden")
    def set_user_hidden_route(user_id: int) -> Response:
        try:
            database.set_user_hidden(user_id, "hidden" in request.form)
        except KeyError:
            abort(404)
        return redirect(url_for("user_page", user_id=user_id))

    @app.post("/assign")
    def assign_route() -> Response:
        try:
            measurement_id = int(request.form["measurement_id"])
            user_id = int(request.form["user_id"])
            measurement = database.fetch_measurement(measurement_id)
            user = database.get_user(user_id)
            if measurement is None or user is None:
                raise KeyError("unknown user or measurement")
            database.assign_measurement(
                measurement_id, user_id, method="manual", confidence=None
            )
            _update_stored_belief(database, user, measurement)
        except (KeyError, ValueError) as exc:
            abort(400, str(exc))
        return redirect(request.referrer or url_for("dashboard"))

    @app.post("/unassign")
    def unassign_route() -> Response:
        try:
            database.unassign_measurement(int(request.form["measurement_id"]))
        except (KeyError, ValueError) as exc:
            abort(400, str(exc))
        return redirect(request.referrer or url_for("dashboard"))

    @app.get("/api/measurements")
    def api_measurements() -> Response:
        try:
            limit_text = request.args.get("limit", "").strip()
            limit = min(max(int(limit_text) if limit_text else 100, 0), 1000)
            selected = request.args.get("user")
            if selected is None or selected == "":
                measurements = database.fetch_recent(limit)
            elif selected == "unassigned":
                measurements = database.fetch_unassigned(limit)
            else:
                measurements = database.fetch_for_user(
                    int(selected), limit, newest_first=True
                )
        except ValueError as exc:
            abort(400, str(exc))
        return jsonify([_measurement_json(item) for item in measurements])

    @app.get("/api/users")
    def api_users() -> Response:
        return jsonify([_user_json(user) for user in database.list_users()])

    return app


def run(
    *,
    host: str = "127.0.0.1",
    port: int = 8480,
    db_path: str | None = None,
    config_path: str | None = None,
    idle_timeout_s: float = 15.0,
    no_activity_timeout_s: float = 300.0,
    scale_runner: Callable[..., int] = scale.run,
) -> int:
    """Run the POC threaded Flask server and its daemon background thread."""
    app = create_app(db_path, initial_status="Starting scale daemon…")
    extension = app.extensions["wiifat"]

    def daemon() -> None:
        try:
            scale_runner(
                db_path,
                config_path,
                idle_timeout_s=idle_timeout_s,
                no_activity_timeout_s=no_activity_timeout_s,
                on_measurement=extension["on_measurement"],
                on_status=extension["set_status"],
                on_progress=extension["on_progress"],
            )
        except Exception as exc:
            # The dashboard status alone is easy to miss on a headless
            # service; make the failure visible in the journal too.
            print(f"Scale daemon stopped: {exc}", file=sys.stderr, flush=True)
            extension["set_status"](f"Scale daemon stopped: {exc}")

    threading.Thread(target=daemon, name="wiifat-scale", daemon=True).start()
    app.run(host=host, port=port, threaded=True, use_reloader=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8480)
    parser.add_argument("--db", help="measurement database path")
    parser.add_argument("--config", help="calibration JSON path")
    parser.add_argument("--idle-timeout", type=scale.idle_timeout_arg, default=15.0)
    parser.add_argument(
        "--no-activity-timeout", type=scale.idle_timeout_arg, default=300.0
    )
    args = parser.parse_args(argv)
    return run(
        host=args.host,
        port=args.port,
        db_path=args.db,
        config_path=args.config,
        idle_timeout_s=args.idle_timeout,
        no_activity_timeout_s=args.no_activity_timeout,
    )


def _user_model(user: User) -> UserModel:
    last_seen = (
        datetime.fromisoformat(user.last_seen_ts.replace("Z", "+00:00")).timestamp()
        if user.last_seen_ts is not None
        else None
    )
    return UserModel(
        user.id,
        user.name,
        user.mu_kg,
        user.sigma_kg,
        last_seen,
        user.weigh_count,
    )


def _update_stored_belief(
    database: Database, user: User, measurement: Measurement
) -> User:
    updated = update_belief(
        _user_model(user), measurement.weight_kg, measurement.timestamp
    )
    return database.update_user_model(
        user.id,
        mu_kg=updated.mu_kg,
        sigma_kg=updated.sigma_kg,
        last_seen_ts=format_timestamp(measurement.timestamp),
        weigh_count=updated.weigh_count,
    )


def _measurement_json(item: Measurement) -> dict[str, object]:
    return {
        "id": item.id,
        "ts": format_timestamp(item.timestamp),
        "weight_kg": item.weight_kg,
        "weight_lb": item.weight_kg * POUNDS_PER_KG,
        "stdev_kg": item.stdev_kg,
        "battery_pct": item.battery_pct,
        "user_id": item.user_id,
        "assign_method": item.assign_method,
        "assign_confidence": item.assign_confidence,
    }


def _user_json(user: User) -> dict[str, object]:
    return {
        "id": user.id,
        "name": user.name,
        "color": user.color,
        "created_ts": user.created_ts,
        "mu_kg": user.mu_kg,
        "sigma_kg": user.sigma_kg,
        "last_seen_ts": user.last_seen_ts,
        "weigh_count": user.weigh_count,
        "hidden": user.hidden,
    }


if __name__ == "__main__":
    raise SystemExit(main())
