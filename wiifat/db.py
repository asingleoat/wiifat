"""SQLite storage for completed Wii Balance Board measurements."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from pathlib import Path

from .colors import user_color
from .statemachine import Measurement


DEFAULT_DB_PATH = Path("~/.local/share/wiifat/wiifat.sqlite3").expanduser()

_USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    color TEXT NOT NULL,
    created_ts TEXT NOT NULL,
    mu_kg REAL,
    sigma_kg REAL,
    last_seen_ts TEXT,
    weigh_count INTEGER NOT NULL DEFAULT 0,
    hidden INTEGER NOT NULL DEFAULT 0
)
"""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements(
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    weight_kg REAL NOT NULL,
    stdev_kg REAL NOT NULL,
    tare_kg REAL NOT NULL,
    corners_json TEXT NOT NULL,
    duration_s REAL NOT NULL,
    raw_kg REAL,
    cal_json TEXT,
    battery_pct INTEGER,
    user_id INTEGER,
    assign_method TEXT,
    assign_confidence REAL
)
"""

_MIGRATIONS = {
    "raw_kg": "ALTER TABLE measurements ADD COLUMN raw_kg REAL",
    "cal_json": "ALTER TABLE measurements ADD COLUMN cal_json TEXT",
    "battery_pct": "ALTER TABLE measurements ADD COLUMN battery_pct INTEGER",
    "user_id": "ALTER TABLE measurements ADD COLUMN user_id INTEGER",
    "assign_method": "ALTER TABLE measurements ADD COLUMN assign_method TEXT",
    "assign_confidence": "ALTER TABLE measurements ADD COLUMN assign_confidence REAL",
}

_MEASUREMENT_COLUMNS = """
    id, ts, weight_kg, stdev_kg, tare_kg, corners_json, duration_s,
    raw_kg, cal_json, battery_pct, user_id, assign_method, assign_confidence
"""


class DuplicateUserNameError(Exception):
    """Raised when a user rename conflicts with an existing display name."""


@dataclass(frozen=True)
class User:
    """One named user's persisted recognition model."""

    id: int
    name: str
    color: str
    created_ts: str
    mu_kg: float | None
    sigma_kg: float | None
    last_seen_ts: str | None
    weigh_count: int
    hidden: bool


def format_timestamp(timestamp: float) -> str:
    """Return a Unix timestamp as an ISO 8601 UTC string."""
    return _format_utc(datetime.fromtimestamp(timestamp, timezone.utc))


class Database:
    """Small connection-per-operation wrapper around the measurement database."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else DEFAULT_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(_USERS_SCHEMA)
            connection.execute(_SCHEMA)
            user_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(users)").fetchall()
            }
            if "color" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN color TEXT")
            if "hidden" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute("UPDATE users SET hidden = 0 WHERE hidden IS NULL")
            users_without_color = connection.execute(
                "SELECT id, name FROM users WHERE color IS NULL OR color = ''"
            ).fetchall()
            for user_id, name in users_without_color:
                connection.execute(
                    "UPDATE users SET color = ? WHERE id = ?",
                    (user_color(str(name)), user_id),
                )
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(measurements)").fetchall()
            }
            for column, statement in _MIGRATIONS.items():
                if column not in columns:
                    connection.execute(statement)

    def insert(self, measurement: Measurement) -> int:
        """Insert a measurement and return its row id."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO measurements(
                    ts, weight_kg, stdev_kg, tare_kg, corners_json, duration_s,
                    raw_kg, cal_json, battery_pct
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    format_timestamp(measurement.timestamp),
                    measurement.weight_kg,
                    measurement.stdev_kg,
                    measurement.tare_kg,
                    json.dumps(measurement.corners, sort_keys=True, separators=(",", ":")),
                    measurement.duration_s,
                    measurement.raw_kg,
                    measurement.cal_json,
                    measurement.battery_pct,
                ),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return an inserted row id")
            return cursor.lastrowid

    def fetch_recent(self, n: int = 10) -> list[Measurement]:
        """Return up to ``n`` measurements, newest first."""
        if n < 0:
            raise ValueError("n must be nonnegative")
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {_MEASUREMENT_COLUMNS}
                FROM measurements
                ORDER BY ts DESC, id DESC
                LIMIT ?
                """,
                (n,),
            ).fetchall()
        return [_row_to_measurement(row) for row in rows]

    def fetch_since(self, since: date | datetime | str) -> list[Measurement]:
        """Return measurements at or after a UTC date/time, oldest first."""
        since_text = _normalise_since(since)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {_MEASUREMENT_COLUMNS}
                FROM measurements
                WHERE ts >= ?
                ORDER BY ts ASC, id ASC
                """,
                (since_text,),
            ).fetchall()
        return [_row_to_measurement(row) for row in rows]

    def fetch_all(self) -> list[Measurement]:
        """Return all measurements, oldest first."""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {_MEASUREMENT_COLUMNS}
                FROM measurements
                ORDER BY ts ASC, id ASC
                """
            ).fetchall()
        return [_row_to_measurement(row) for row in rows]

    def fetch_dashboard_measurements(
        self, since: date | datetime | str | None = None
    ) -> list[Measurement]:
        """Return unassigned and visible-user measurements, oldest first."""
        conditions = [
            "(user_id IS NULL OR user_id IN (SELECT id FROM users WHERE hidden = 0))"
        ]
        parameters: list[object] = []
        if since is not None:
            conditions.append("ts >= ?")
            parameters.append(_normalise_since(since))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT {_MEASUREMENT_COLUMNS}
                FROM measurements
                WHERE {' AND '.join(conditions)}
                ORDER BY ts ASC, id ASC
                """,
                parameters,
            ).fetchall()
        return [_row_to_measurement(row) for row in rows]

    def fetch_measurement(self, measurement_id: int) -> Measurement | None:
        """Return one measurement by id, or ``None`` when absent."""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT {_MEASUREMENT_COLUMNS} FROM measurements WHERE id = ?",
                (measurement_id,),
            ).fetchone()
        return _row_to_measurement(row) if row is not None else None

    def fetch_for_user(
        self,
        user_id: int,
        limit: int | None = None,
        *,
        newest_first: bool = False,
    ) -> list[Measurement]:
        """Return measurements assigned to one user."""
        return self._fetch_filtered(
            "user_id = ?", (user_id,), limit, newest_first=newest_first
        )

    def fetch_unassigned(self, limit: int | None = None) -> list[Measurement]:
        """Return newest unassigned measurements."""
        return self._fetch_filtered(
            "user_id IS NULL", (), limit, newest_first=True
        )

    def create_user(
        self,
        name: str,
        seed_weight_kg: float | None = None,
        *,
        timestamp: float | None = None,
    ) -> User:
        """Create a named user, optionally seeding a 2 kg prior."""
        clean_name = name.strip()
        if not clean_name:
            raise ValueError("user name must not be empty")
        if seed_weight_kg is not None and seed_weight_kg <= 0.0:
            raise ValueError("seed weight must be positive")
        created_ts = format_timestamp(
            datetime.now(timezone.utc).timestamp() if timestamp is None else timestamp
        )
        mu = float(seed_weight_kg) if seed_weight_kg is not None else None
        sigma = 2.0 if seed_weight_kg is not None else None
        last_seen = created_ts if seed_weight_kg is not None else None
        weigh_count = 1 if seed_weight_kg is not None else 0
        color = user_color(clean_name)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO users(
                    name, color, created_ts, mu_kg, sigma_kg,
                    last_seen_ts, weigh_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (clean_name, color, created_ts, mu, sigma, last_seen, weigh_count),
            )
            if cursor.lastrowid is None:
                raise RuntimeError("SQLite did not return a user id")
            user_id = int(cursor.lastrowid)
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("created user could not be read back")
        return user

    def get_user(self, user_id: int) -> User | None:
        """Return one user by id."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id, name, color, created_ts, mu_kg, sigma_kg,
                       last_seen_ts, weigh_count, hidden
                FROM users WHERE id = ?
                """,
                (user_id,),
            ).fetchone()
        return _row_to_user(row) if row is not None else None

    def list_users(self) -> list[User]:
        """Return users ordered case-insensitively by name."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, color, created_ts, mu_kg, sigma_kg,
                       last_seen_ts, weigh_count, hidden
                FROM users ORDER BY name COLLATE NOCASE, id
                """
            ).fetchall()
        return [_row_to_user(row) for row in rows]

    def list_visible_users(self) -> list[User]:
        """Return users that opted into dashboard display."""
        return [user for user in self.list_users() if not user.hidden]

    def rename_user(self, user_id: int, new_name: str) -> User:
        """Rename an existing user."""
        clean_name = new_name.strip()
        if not clean_name:
            raise ValueError("user name must not be empty")
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "UPDATE users SET name = ? WHERE id = ?", (clean_name, user_id)
                )
        except sqlite3.IntegrityError as exc:
            raise DuplicateUserNameError(clean_name) from exc
        if cursor.rowcount != 1:
            raise KeyError(f"unknown user id {user_id}")
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("renamed user could not be read back")
        return user

    def update_user_color(self, user_id: int, color: str) -> User:
        """Replace a user's stored display color."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET color = ? WHERE id = ?", (color, user_id)
            )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown user id {user_id}")
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("recolored user could not be read back")
        return user

    def set_user_hidden(self, user_id: int, hidden: bool) -> User:
        """Set whether a user is omitted from dashboard-only views."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE users SET hidden = ? WHERE id = ?",
                (int(bool(hidden)), user_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown user id {user_id}")
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("updated user could not be read back")
        return user

    def delete_user(self, user_id: int) -> None:
        """Delete a user and leave their measurements unassigned."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE measurements
                SET user_id = NULL, assign_method = NULL, assign_confidence = NULL
                WHERE user_id = ?
                """,
                (user_id,),
            )
            cursor = connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
        if cursor.rowcount != 1:
            raise KeyError(f"unknown user id {user_id}")

    def update_user_model(
        self,
        user_id: int,
        *,
        mu_kg: float,
        sigma_kg: float,
        last_seen_ts: str,
        weigh_count: int,
    ) -> User:
        """Persist a recognition-model update."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE users
                SET mu_kg = ?, sigma_kg = ?, last_seen_ts = ?, weigh_count = ?
                WHERE id = ?
                """,
                (mu_kg, sigma_kg, last_seen_ts, weigh_count, user_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown user id {user_id}")
        user = self.get_user(user_id)
        if user is None:
            raise RuntimeError("updated user could not be read back")
        return user

    def assign_measurement(
        self,
        measurement_id: int,
        user_id: int,
        *,
        method: str,
        confidence: float | None,
    ) -> None:
        """Assign a measurement without changing the user's belief model."""
        if self.get_user(user_id) is None:
            raise KeyError(f"unknown user id {user_id}")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE measurements
                SET user_id = ?, assign_method = ?, assign_confidence = ?
                WHERE id = ?
                """,
                (user_id, method, confidence, measurement_id),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown measurement id {measurement_id}")

    def unassign_measurement(self, measurement_id: int) -> None:
        """Clear an assignment; prior belief updates are intentionally retained."""
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE measurements
                SET user_id = NULL, assign_method = NULL, assign_confidence = NULL
                WHERE id = ?
                """,
                (measurement_id,),
            )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown measurement id {measurement_id}")

    def _fetch_filtered(
        self,
        where: str,
        parameters: tuple[object, ...],
        limit: int | None,
        *,
        newest_first: bool,
    ) -> list[Measurement]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be nonnegative")
        direction = "DESC" if newest_first else "ASC"
        sql = f"""
            SELECT {_MEASUREMENT_COLUMNS}
            FROM measurements
            WHERE {where}
            ORDER BY ts {direction}, id {direction}
        """
        values = parameters
        if limit is not None:
            sql += " LIMIT ?"
            values += (limit,)
        with self._connect() as connection:
            rows = connection.execute(sql, values).fetchall()
        return [_row_to_measurement(row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path)


def insert(
    measurement: Measurement,
    path: str | os.PathLike[str] | None = None,
) -> int:
    """Insert a measurement in ``path`` (or the default database)."""
    return Database(path).insert(measurement)


def fetch_recent(
    n: int = 10,
    path: str | os.PathLike[str] | None = None,
) -> list[Measurement]:
    """Fetch recent measurements from ``path`` (or the default database)."""
    return Database(path).fetch_recent(n)


def fetch_since(
    since: date | datetime | str,
    path: str | os.PathLike[str] | None = None,
) -> list[Measurement]:
    """Fetch measurements since a UTC date/time from the selected database."""
    return Database(path).fetch_since(since)


def _normalise_since(since: date | datetime | str) -> str:
    if isinstance(since, datetime):
        value = since
    elif isinstance(since, date):
        value = datetime.combine(since, time.min, tzinfo=timezone.utc)
    else:
        text = since.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        value = datetime.fromisoformat(text)

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return _format_utc(value)


def _format_utc(value: datetime) -> str:
    """Use fixed-width fractional seconds so SQLite text order is time order."""
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _row_to_measurement(row: tuple[object, ...]) -> Measurement:
    (
        measurement_id,
        ts,
        weight,
        stdev,
        tare,
        corners_json,
        duration,
        raw_kg,
        cal_json,
        battery_pct,
        user_id,
        assign_method,
        assign_confidence,
    ) = row
    timestamp = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    corners = json.loads(str(corners_json))
    if not isinstance(corners, dict):
        raise ValueError("corners_json is not an object")
    return Measurement(
        timestamp=timestamp,
        weight_kg=float(weight),
        stdev_kg=float(stdev),
        tare_kg=float(tare),
        corners={str(key): float(value) for key, value in corners.items()},
        duration_s=float(duration),
        raw_kg=float(raw_kg) if raw_kg is not None else None,
        cal_json=str(cal_json) if cal_json is not None else None,
        battery_pct=int(battery_pct) if battery_pct is not None else None,
        id=int(measurement_id),
        user_id=int(user_id) if user_id is not None else None,
        assign_method=str(assign_method) if assign_method is not None else None,
        assign_confidence=(
            float(assign_confidence) if assign_confidence is not None else None
        ),
    )


def _row_to_user(row: tuple[object, ...]) -> User:
    (
        user_id,
        name,
        color,
        created_ts,
        mu_kg,
        sigma_kg,
        last_seen_ts,
        weigh_count,
        hidden,
    ) = row
    return User(
        id=int(user_id),
        name=str(name),
        color=str(color),
        created_ts=str(created_ts),
        mu_kg=float(mu_kg) if mu_kg is not None else None,
        sigma_kg=float(sigma_kg) if sigma_kg is not None else None,
        last_seen_ts=str(last_seen_ts) if last_seen_ts is not None else None,
        weigh_count=int(weigh_count),
        hidden=bool(hidden),
    )
