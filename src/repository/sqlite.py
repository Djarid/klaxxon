"""SQLite implementation of the ReminderRepository.

Single-file database, no external dependencies.
Schema is created on first connection if tables don't exist.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..models.reminder import Reminder, ReminderState
from .base import ReminderRepository

_SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    starts_at TEXT NOT NULL,
    duration_min INTEGER NOT NULL DEFAULT 90,
    link TEXT,
    source TEXT NOT NULL DEFAULT 'manual',
    profile TEXT NOT NULL DEFAULT 'meeting',
    escalate_to TEXT,
    state TEXT NOT NULL DEFAULT 'pending',
    ack_keyword TEXT,
    ack_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminder_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    reminder_id INTEGER NOT NULL REFERENCES reminders(id) ON DELETE CASCADE,
    sent_at TEXT NOT NULL,
    message TEXT NOT NULL,
    channel TEXT NOT NULL DEFAULT 'signal'
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_reminders_state ON reminders(state);
CREATE INDEX IF NOT EXISTS idx_reminders_starts_at ON reminders(starts_at);
CREATE INDEX IF NOT EXISTS idx_reminder_log_reminder ON reminder_log(reminder_id);
CREATE INDEX IF NOT EXISTS idx_auth_tokens_hash ON auth_tokens(token_hash);
"""


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    return datetime.fromisoformat(value)


def _row_to_reminder(row: sqlite3.Row) -> Reminder:
    return Reminder(
        id=row["id"],
        title=row["title"],
        description=row["description"],
        starts_at=_parse_dt(row["starts_at"]),
        duration_min=row["duration_min"],
        link=row["link"],
        source=row["source"],
        profile=row["profile"],
        escalate_to=row["escalate_to"],
        state=ReminderState(row["state"]),
        ack_keyword=row["ack_keyword"],
        ack_at=_parse_dt(row["ack_at"]),
        created_at=_parse_dt(row["created_at"]),
        updated_at=_parse_dt(row["updated_at"]),
    )


class SqliteReminderRepository(ReminderRepository):
    """SQLite-backed reminder storage."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._db_path = str(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
        return self._conn

    def _ensure_schema(self) -> None:
        conn = self._get_conn()
        conn.executescript(_SCHEMA)
        conn.commit()

    def create(self, reminder: Reminder) -> Reminder:
        now = _now_utc()
        conn = self._get_conn()
        cursor = conn.execute(
            """INSERT INTO reminders
            (title, description, starts_at, duration_min, link, source, profile, escalate_to, state, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                reminder.title,
                reminder.description,
                reminder.starts_at.isoformat() if reminder.starts_at else now,
                reminder.duration_min,
                reminder.link,
                reminder.source,
                reminder.profile,
                reminder.escalate_to,
                reminder.state.value,
                now,
                now,
            ),
        )
        conn.commit()
        reminder.id = cursor.lastrowid
        reminder.created_at = _parse_dt(now)
        reminder.updated_at = _parse_dt(now)
        return reminder

    def get(self, reminder_id: int) -> Optional[Reminder]:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
        if row is None:
            return None
        return _row_to_reminder(row)

    def list_all(
        self,
        state: Optional[ReminderState] = None,
    ) -> list[Reminder]:
        conn = self._get_conn()
        if state is not None:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE state = ? ORDER BY starts_at",
                (state.value,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM reminders ORDER BY starts_at").fetchall()
        return [_row_to_reminder(r) for r in rows]

    def list_upcoming(
        self,
        before: Optional[datetime] = None,
        states: Optional[list[ReminderState]] = None,
    ) -> list[Reminder]:
        conn = self._get_conn()
        now = _now_utc()
        conditions = []
        params: list = []

        if before is not None:
            conditions.append("starts_at <= ?")
            params.append(before.isoformat())

        if states:
            placeholders = ",".join("?" for _ in states)
            conditions.append(f"state IN ({placeholders})")
            params.extend(s.value for s in states)

        where = ""
        if conditions:
            where = "WHERE " + " AND ".join(conditions)

        rows = conn.execute(
            f"SELECT * FROM reminders {where} ORDER BY starts_at",
            params,
        ).fetchall()
        return [_row_to_reminder(r) for r in rows]

    def update_state(
        self,
        reminder_id: int,
        state: ReminderState,
        ack_keyword: Optional[str] = None,
        ack_at: Optional[datetime] = None,
    ) -> Optional[Reminder]:
        now = _now_utc()
        conn = self._get_conn()
        conn.execute(
            """UPDATE reminders
            SET state = ?, ack_keyword = ?, ack_at = ?, updated_at = ?
            WHERE id = ?""",
            (
                state.value,
                ack_keyword,
                ack_at.isoformat() if ack_at else None,
                now,
                reminder_id,
            ),
        )
        conn.commit()
        return self.get(reminder_id)

    def delete(self, reminder_id: int) -> bool:
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM reminders WHERE id = ?", (reminder_id,))
        conn.commit()
        return cursor.rowcount > 0

    def count_by_state(self, state: ReminderState) -> int:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM reminders WHERE state = ?",
            (state.value,),
        ).fetchone()
        return row["cnt"] if row else 0

    def log_reminder(
        self,
        reminder_id: int,
        message: str,
        channel: str = "signal",
    ) -> None:
        """Log a sent reminder (not part of the ABC, SQLite-specific)."""
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO reminder_log (reminder_id, sent_at, message, channel)
            VALUES (?, ?, ?, ?)""",
            (reminder_id, _now_utc(), message, channel),
        )
        conn.commit()

    def get_last_reminder_time(self, reminder_id: int) -> Optional[datetime]:
        """Get the timestamp of the last reminder sent for a reminder."""
        conn = self._get_conn()
        row = conn.execute(
            """SELECT sent_at FROM reminder_log
            WHERE reminder_id = ? ORDER BY sent_at DESC LIMIT 1""",
            (reminder_id,),
        ).fetchone()
        if row is None:
            return None
        return _parse_dt(row["sent_at"])

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
