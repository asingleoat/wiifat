import json
import threading
from datetime import datetime

import pytest


pytest.importorskip("flask")

from wiifat.calibration import CORNERS  # noqa: E402
from wiifat.colors import color_from_key, user_color  # noqa: E402
from wiifat.db import Database  # noqa: E402
from wiifat.server import EventPublisher, create_app  # noqa: E402
from wiifat.statemachine import Measurement  # noqa: E402


DAY = 86_400.0


def measurement(timestamp, weight):
    return Measurement(
        timestamp=timestamp,
        weight_kg=weight,
        stdev_kg=0.05,
        tare_kg=3.0,
        corners={corner: weight / 4.0 for corner in CORNERS},
        duration_s=3.0,
        battery_pct=80,
    )


def test_flask_user_recognition_claim_unassign_chart_and_apis(tmp_path):
    database_path = tmp_path / "server.sqlite3"
    reroll_timestamp = 1_700_123_456.789
    app = create_app(
        str(database_path), time_fn=lambda: reroll_timestamp
    )
    app.config.update(TESTING=True)
    client = app.test_client()
    extension = app.extensions["wiifat"]
    database = extension["database"]

    response = client.post("/users", data={"name": "Alice", "seed_weight": "70"})
    assert response.status_code == 302
    alice = database.list_users()[0]
    alice_seen = datetime.fromisoformat(
        alice.last_seen_ts.replace("Z", "+00:00")
    ).timestamp()

    auto_id, result = extension["inject_measurement"](
        measurement(alice_seen + DAY, 70.2)
    )
    assert result.assigned_user_id == alice.id
    auto = database.fetch_measurement(auto_id)
    assert auto is not None
    assert auto.user_id == alice.id
    assert auto.assign_method == "auto"
    assert auto.assign_confidence is not None

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert b"Alice" in dashboard.data
    assert b"70.20 kg / 154.8 lb" in dashboard.data
    assert user_color("Alice").encode() in dashboard.data
    assert b"reroll color" in dashboard.data
    assert b'new EventSource("/events")' in dashboard.data
    assert b"live-progress-fill" in dashboard.data
    assert b"window.location.reload()" in dashboard.data

    user_page = client.get(f"/user/{alice.id}")
    assert user_page.status_code == 200
    assert b'<span class="badge editable-user-name"' in user_page.data
    assert b"data-rename-url" in user_page.data
    assert b'addEventListener("dblclick"' in user_page.data
    assert b'class="rename-fallback"' in user_page.data
    assert f'action="/users/{alice.id}/rename"'.encode() in user_page.data
    assert b">Rename</button>" in user_page.data

    expected_color = color_from_key(str(reroll_timestamp))
    assert expected_color != user_color("Alice")
    response = client.post(
        f"/users/{alice.id}/reroll-color",
        headers={"Referer": f"http://localhost/user/{alice.id}"},
    )
    assert response.status_code == 302
    assert response.location.endswith(f"/user/{alice.id}")
    assert database.get_user(alice.id).color == expected_color
    assert Database(database_path).get_user(alice.id).color == expected_color
    dashboard = client.get("/")
    assert expected_color.encode() in dashboard.data

    client.post("/users", data={"name": "Bob"})
    bob = next(user for user in database.list_users() if user.name == "Bob")
    visitor_id, visitor_result = extension["inject_measurement"](
        measurement(alice_seen + 2 * DAY, 90.0)
    )
    assert visitor_result.assigned_user_id is None
    response = client.post(
        "/assign",
        data={"measurement_id": visitor_id, "user_id": bob.id},
    )
    assert response.status_code == 302
    claimed = database.fetch_measurement(visitor_id)
    assert claimed is not None
    assert claimed.user_id == bob.id
    assert claimed.assign_method == "manual"
    assert claimed.assign_confidence is None
    assert database.get_user(bob.id).mu_kg == pytest.approx(90.0)

    response = client.post("/unassign", data={"measurement_id": visitor_id})
    assert response.status_code == 302
    assert database.fetch_measurement(visitor_id).user_id is None

    chart = client.get(f"/chart/{alice.id}.png")
    assert chart.status_code == 200
    assert chart.data.startswith(b"\x89PNG\r\n\x1a\n")

    users_json = client.get("/api/users").get_json()
    assert isinstance(users_json, list)
    assert {"id", "name", "color", "mu_kg", "sigma_kg", "weigh_count"} <= set(
        users_json[0]
    )
    measurements_json = client.get(
        f"/api/measurements?user={alice.id}&limit=5"
    ).get_json()
    assert isinstance(measurements_json, list)
    assert measurements_json[0]["id"] == auto_id
    assert {"weight_kg", "weight_lb", "user_id", "assign_method"} <= set(
        measurements_json[0]
    )


def test_user_rename_route_json_form_and_failures(tmp_path):
    database_path = tmp_path / "rename.sqlite3"
    app = create_app(str(database_path))
    app.config.update(TESTING=True)
    client = app.test_client()
    database = app.extensions["wiifat"]["database"]
    alice = database.create_user("Alice", 70.0)
    bob = database.create_user("Bob", 82.0)

    response = client.post(
        f"/users/{alice.id}/rename", json={"name": "  Alicia  "}
    )
    assert response.status_code == 200
    assert response.get_json() == {"ok": True, "id": alice.id, "name": "Alicia"}
    assert Database(database_path).get_user(alice.id).name == "Alicia"

    response = client.post(
        f"/users/{alice.id}/rename",
        data={"name": "Alice Smith"},
        headers={"Referer": f"http://localhost/user/{alice.id}"},
    )
    assert response.status_code == 302
    assert response.location.endswith(f"/user/{alice.id}")
    assert database.get_user(alice.id).name == "Alice Smith"

    response = client.post(
        f"/users/{alice.id}/rename", json={"name": bob.name}
    )
    assert response.status_code == 409
    assert response.get_json() == {
        "ok": False,
        "error": "That display name is already in use.",
    }

    response = client.post(f"/users/{alice.id}/rename", json={"name": "  "})
    assert response.status_code == 400
    assert response.get_json() == {
        "ok": False,
        "error": "Display name must not be empty.",
    }

    response = client.post("/users/9999/rename", json={"name": "Nobody"})
    assert response.status_code == 404
    assert response.get_json() == {"ok": False, "error": "User not found."}

    response = client.post(
        f"/users/{alice.id}/rename",
        data={"name": "A. Smith"},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    assert response.get_json() == {
        "ok": True,
        "id": alice.id,
        "name": "A. Smith",
    }


def test_sse_measurement_event_contains_recognition_and_stored_color(tmp_path):
    app = create_app(str(tmp_path / "events.sqlite3"))
    app.config.update(TESTING=True)
    client = app.test_client()
    extension = app.extensions["wiifat"]
    database = extension["database"]
    alice = database.create_user("Alice", 70.0)
    alice = database.update_user_color(alice.id, color_from_key("chosen color"))
    alice_seen = datetime.fromisoformat(
        alice.last_seen_ts.replace("Z", "+00:00")
    ).timestamp()

    response = client.get("/events", buffered=False)
    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert extension["publisher"].client_count == 1

    measurement_id, result = extension["inject_measurement"](
        measurement(alice_seen + DAY, 70.2)
    )
    assert result.assigned_user_id == alice.id

    status_payloads = []
    measurement_payload = None
    for _ in range(4):
        chunk = next(response.response).decode()
        if chunk.startswith("event: status\n"):
            data_line = next(
                line for line in chunk.splitlines() if line.startswith("data: ")
            )
            status_payloads.append(json.loads(data_line.removeprefix("data: ")))
        if chunk.startswith("event: measurement\n"):
            data_line = next(
                line for line in chunk.splitlines() if line.startswith("data: ")
            )
            measurement_payload = json.loads(data_line.removeprefix("data: "))
            break
    response.close()

    assert extension["publisher"].client_count == 0
    assert any(item["battery_pct"] == 80 for item in status_payloads)
    assert measurement_payload is not None
    assert measurement_payload["id"] == measurement_id
    assert measurement_payload["weight_kg"] == pytest.approx(70.2)
    assert measurement_payload["battery_pct"] == 80
    assert measurement_payload["user"] == {
        "id": alice.id,
        "name": "Alice",
        "color": alice.color,
    }
    assert measurement_payload["method"] == "auto"
    assert measurement_payload["confidence"] >= 0.90


def test_progress_publication_is_throttled_but_state_transitions_are_immediate(
    tmp_path,
):
    app = create_app(str(tmp_path / "progress-events.sqlite3"))
    extension = app.extensions["wiifat"]
    publisher = extension["publisher"]
    client = publisher.subscribe()
    progress = extension["on_progress"]

    progress(10.0, {"state": "IDLE", "fill": 0.0, "stdev_kg": None}, 2.0)
    progress(10.1, {"state": "IDLE", "fill": 0.0, "stdev_kg": None}, 2.0)
    progress(
        10.11,
        {"state": "MEASURING", "fill": 0.2, "stdev_kg": 0.4},
        72.0,
    )
    progress(
        10.2,
        {"state": "MEASURING", "fill": 0.3, "stdev_kg": 0.3},
        72.0,
    )
    progress(
        10.36,
        {"state": "MEASURING", "fill": 0.4, "stdev_kg": 0.1},
        72.0,
    )

    messages = []
    while not client.empty():
        messages.append(client.get_nowait())
    publisher.unsubscribe(client)
    assert [message.splitlines()[0] for message in messages] == [
        "event: progress",
        "event: progress",
        "event: progress",
    ]
    assert '"state":"IDLE"' in messages[0]
    assert '"state":"MEASURING"' in messages[1]


def test_event_publisher_drops_oldest_for_slow_clients_without_blocking():
    publisher = EventPublisher(queue_size=2)
    client = publisher.subscribe()

    def publish_many():
        for sequence in range(100):
            publisher.publish("progress", {"sequence": sequence})

    worker = threading.Thread(target=publish_many)
    worker.start()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    messages = [client.get_nowait(), client.get_nowait()]
    assert '"sequence":98' in messages[0]
    assert '"sequence":99' in messages[1]
    publisher.unsubscribe(client)
