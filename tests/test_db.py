import sqlite3
from datetime import date

import pytest

from wiifat.colors import user_color
from wiifat.db import Database, DuplicateUserNameError
from wiifat.statemachine import Measurement


def measurement(timestamp, weight):
    return Measurement(
        timestamp=timestamp,
        weight_kg=weight,
        stdev_kg=0.08,
        tare_kg=2.4,
        corners={"top-left": 18.1, "top-right": 18.3},
        duration_s=2.7,
        raw_kg=weight - 0.4,
        cal_json='{"version":2}',
        battery_pct=73,
    )


def test_database_round_trip(tmp_path):
    database = Database(tmp_path / "nested" / "wiifat.sqlite3")
    first_id = database.insert(measurement(1_700_000_000.0, 70.2))
    second_id = database.insert(measurement(1_700_086_400.0, 69.8))

    assert first_id == 1
    assert second_id == 2
    recent = database.fetch_recent(1)
    assert len(recent) == 1
    assert recent[0].timestamp == pytest.approx(1_700_086_400.0)
    assert recent[0].weight_kg == pytest.approx(69.8)
    assert recent[0].raw_kg == pytest.approx(69.4)
    assert recent[0].cal_json == '{"version":2}'
    assert recent[0].battery_pct == 73
    assert recent[0].corners == {"top-left": 18.1, "top-right": 18.3}

    since = database.fetch_since(date(2023, 11, 15))
    assert [item.weight_kg for item in since] == [69.8]


def test_database_migrates_old_schema_and_preserves_old_rows(tmp_path):
    path = tmp_path / "old.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE measurements(
            id INTEGER PRIMARY KEY,
            ts TEXT NOT NULL,
            weight_kg REAL NOT NULL,
            stdev_kg REAL NOT NULL,
            tare_kg REAL NOT NULL,
            corners_json TEXT NOT NULL,
            duration_s REAL NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO measurements(
            ts, weight_kg, stdev_kg, tare_kg, corners_json, duration_s
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            "2023-11-14T22:13:20.000000Z",
            70.2,
            0.08,
            2.4,
            '{"top-left":18.1}',
            2.7,
        ),
    )
    connection.execute(
        """
        CREATE TABLE users(
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            created_ts TEXT NOT NULL,
            mu_kg REAL,
            sigma_kg REAL,
            last_seen_ts TEXT,
            weigh_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    connection.execute(
        """
        INSERT INTO users(
            name, created_ts, mu_kg, sigma_kg, last_seen_ts, weigh_count
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("Alice", "2023-11-14T22:13:20.000000Z", 70.0, 2.0, None, 1),
    )
    connection.commit()
    connection.close()

    database = Database(path)
    connection = sqlite3.connect(path)
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(measurements)")
    }
    connection.close()
    old_row = database.fetch_recent(1)[0]

    assert {
        "raw_kg",
        "cal_json",
        "battery_pct",
        "user_id",
        "assign_method",
        "assign_confidence",
    } <= columns
    connection = sqlite3.connect(path)
    user_column_info = connection.execute("PRAGMA table_info(users)").fetchall()
    user_columns = {row[1] for row in user_column_info}
    connection.close()
    assert {
        "id",
        "name",
        "color",
        "created_ts",
        "mu_kg",
        "sigma_kg",
        "last_seen_ts",
        "weigh_count",
        "hidden",
    } <= user_columns
    hidden_column = next(row for row in user_column_info if row[1] == "hidden")
    assert hidden_column[3] == 1
    assert str(hidden_column[4]) == "0"
    migrated_user = database.get_user(1)
    assert migrated_user is not None
    assert migrated_user.color == user_color("Alice")
    assert migrated_user.hidden is False
    assert old_row.weight_kg == pytest.approx(70.2)
    assert old_row.raw_kg is None
    assert old_row.cal_json is None
    assert old_row.battery_pct is None
    assert old_row.user_id is None
    assert old_row.assign_method is None
    assert old_row.assign_confidence is None


def test_user_crud_assignment_and_filtered_measurements(tmp_path):
    database = Database(tmp_path / "users.sqlite3")
    alice = database.create_user("Alice", 70.0, timestamp=1_700_000_000.0)
    bob = database.create_user("Bob", timestamp=1_700_000_100.0)
    assert alice.color == user_color("Alice")
    assert bob.color == user_color("Bob")
    assert alice.weigh_count == 1
    assert bob.weigh_count == 0
    assert alice.hidden is False
    assert bob.hidden is False
    alice_measurement = database.insert(measurement(1_700_001_000.0, 70.2))
    unclaimed_measurement = database.insert(measurement(1_700_002_000.0, 82.0))

    database.assign_measurement(
        alice_measurement, alice.id, method="manual", confidence=None
    )

    assigned = database.fetch_for_user(alice.id)
    assert [item.id for item in assigned] == [alice_measurement]
    assert assigned[0].user_id == alice.id
    assert assigned[0].assign_method == "manual"
    assert assigned[0].assign_confidence is None
    assert [item.id for item in database.fetch_unassigned()] == [unclaimed_measurement]

    bob_color = bob.color
    renamed = database.rename_user(bob.id, "  Robert  ")
    assert renamed.name == "Robert"
    assert renamed.color == bob_color
    assert Database(database.path).get_user(bob.id) == renamed
    with pytest.raises(DuplicateUserNameError):
        database.rename_user(bob.id, "Alice")
    with pytest.raises(ValueError, match="must not be empty"):
        database.rename_user(bob.id, "   ")
    assert database.get_user(bob.id) == renamed
    hidden_bob = database.set_user_hidden(bob.id, True)
    assert hidden_bob.hidden is True
    assert [user.id for user in database.list_visible_users()] == [alice.id]
    assert Database(database.path).get_user(bob.id).hidden is True
    visible_bob = database.set_user_hidden(bob.id, False)
    assert visible_bob.hidden is False
    database.unassign_measurement(alice_measurement)
    assert {item.id for item in database.fetch_unassigned()} == {
        alice_measurement,
        unclaimed_measurement,
    }
    database.delete_user(bob.id)
    assert database.get_user(bob.id) is None


def test_dashboard_measurements_exclude_hidden_users_without_reclassifying_them(
    tmp_path,
):
    database = Database(tmp_path / "dashboard.sqlite3")
    visible = database.create_user("Visible", 70.0)
    hidden = database.create_user("Hidden", 80.0)
    visible_id = database.insert(measurement(1_700_000_000.0, 70.1))
    hidden_id = database.insert(measurement(1_700_000_100.0, 80.1))
    unassigned_id = database.insert(measurement(1_700_000_200.0, 90.1))
    database.assign_measurement(
        visible_id, visible.id, method="manual", confidence=None
    )
    database.assign_measurement(
        hidden_id, hidden.id, method="manual", confidence=None
    )
    database.set_user_hidden(hidden.id, True)

    dashboard_ids = {
        item.id for item in database.fetch_dashboard_measurements()
    }

    assert dashboard_ids == {visible_id, unassigned_id}
