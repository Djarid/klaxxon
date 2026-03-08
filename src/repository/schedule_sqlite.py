"""SQLite implementation of the ScheduleRepository.

Single-file database, no external dependencies.
Schema is created on first connection if tables don't exist.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models.schedule import Schedule

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    time_of_day TEXT NOT NULL,
    duration_min INTEGER DEFAULT 0,
    link TEXT,
    source TEXT DEFAULT 'manual',
    profile TEXT DEFAULT 'meeting',
    escalate_to TEXT,
    lead_time_min INTEGER,
    nag_interval_min INTEGER,
    recurrence TEXT NOT NULL DEFAULT 'daily',
    recurrence_rule TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_schedules_active ON schedules(is_active);
"""


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _row_to_schedule(row: sqlite3.Row) -> Schedule:
    return Schedule(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        time_of_day=row["time_of_day"],
        duration_min=row["duration_min"],
        link=row["link"],
        source=row["source"],
        profile=row["profile"],
        escalate_to=row["escalate_to"],
        lead_time_min=row["lead_time_min"],
        nag_interval_min=row["nag_interval_min"],
        recurrence=row["recurrence"],
        recurrence_rule=row["recurrence_rule"],
        is_active=bool(row["is_active"]),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


class SqliteScheduleRepository:
    """SQLite-backed schedule storage."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_table()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def _ensure_table(self) -> None:
        conn = self._get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    def create(self, schedule: Schedule) -> Schedule:
        """Create a new schedule. Returns the schedule with id populated."""
        now = _now_utc()
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO schedules
            (title, description, time_of_day, duration_min, link, source, profile, escalate_to,
             lead_time_min, nag_interval_min, recurrence, recurrence_rule, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                schedule.title,
                schedule.description,
                schedule.time_of_day,
                schedule.duration_min,
                schedule.link,
                schedule.source,
                schedule.profile,
                schedule.escalate_to,
                schedule.lead_time_min,
                schedule.nag_interval_min,
                schedule.recurrence,
                schedule.recurrence_rule,
                1 if schedule.is_active else 0,
                now,
                now,
            ),
        )
        conn.commit()
        schedule.id = cursor.lastrowid
        schedule.created_at = _parse_dt(now)
        schedule.updated_at = _parse_dt(now)
        return schedule

    def get(self, schedule_id: int) -> Optional[Schedule]:
        """Retrieve a schedule by id. Returns None if not found."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_schedule(row)

    def list(self, active_only: bool = True) -> list[Schedule]:
        """List schedules, optionally filtered by active status."""
        conn = self._get_conn()
        if active_only:
            rows = conn.execute(
                "SELECT * FROM schedules WHERE is_active = 1 ORDER BY time_of_day"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM schedules ORDER BY time_of_day"
            ).fetchall()
        return [_row_to_schedule(r) for r in rows]

    def update_fields(self, schedule_id: int, fields: dict) -> Optional[Schedule]:
        """Update specific fields on a schedule. Returns updated schedule or None if not found."""
        if not fields:
            return self.get(schedule_id)

        # Whitelist of allowed column names (prevent SQL injection)
        allowed_fields = {
            "title",
            "description",
            "time_of_day",
            "duration_min",
            "link",
            "profile",
            "escalate_to",
            "lead_time_min",
            "nag_interval_min",
            "recurrence",
            "recurrence_rule",
            "is_active",
        }

        # Filter to only allowed fields
        update_fields = {k: v for k, v in fields.items() if k in allowed_fields}
        if not update_fields:
            return self.get(schedule_id)

        # Build dynamic UPDATE query
        set_clauses = []
        params = []
        for field_name, value in update_fields.items():
            set_clauses.append(f"{field_name} = ?")
            # Convert bool to int for is_active
            if field_name == "is_active" and isinstance(value, bool):
                params.append(1 if value else 0)
            else:
                params.append(value)

        # Always update updated_at
        set_clauses.append("updated_at = ?")
        params.append(_now_utc())

        # Add schedule_id to params
        params.append(schedule_id)

        query = f"UPDATE schedules SET {', '.join(set_clauses)} WHERE id = ?"

        conn = self._get_conn()
        conn.execute(query, params)
        conn.commit()

        return self.get(schedule_id)

    def deactivate(self, schedule_id: int) -> bool:
        """Deactivate a schedule (soft delete). Returns True if successful."""
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE schedules SET is_active = 0, updated_at = ? WHERE id = ?",
            (_now_utc(), schedule_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
